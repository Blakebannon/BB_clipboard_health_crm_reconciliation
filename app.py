"""Bellhaven <-> CRM reconciliation review dashboard.

    streamlit run app.py

Safety: rendering this page never writes to the CRM. The only write path is the
``Approve & apply`` button handler for one specific proposal, which calls
``bellhaven.apply.apply_proposal`` with a writable client constructed *inside
that handler*. Everything else uses a read-only client.
"""
from __future__ import annotations

import json
from typing import Any

import streamlit as st

from bellhaven import config
from bellhaven.apply import apply_proposal
from bellhaven.crm_api import make_client
from bellhaven.models import (
    ACTION_CHOW_SUCCESSOR,
    ACTION_MANUAL_REVIEW,
    ACTION_NO_CHANGE,
    ALL_ACTIONS,
    APPLICABLE_ACTIONS,
    APPLY_APPLIED,
    APPLY_BLOCKED,
    APPLY_FAILED,
    APPLY_IN_PROGRESS,
    APPLY_NEEDS_RECOVERY,
    DECISION_ACKNOWLEDGED,
    DECISION_APPROVED,
    DECISION_PENDING,
    DECISION_REJECTED,
    Proposal,
)
from bellhaven.pipeline import safe_run_scan
from bellhaven.store import AlreadyClaimed, Store

st.set_page_config(page_title="Bellhaven CRM Reconciliation", layout="wide", page_icon="🏥")

TIERS = ["HIGH_ADDRESS_EXACT", "MEDIUM_ADDRESS_FUZZY", "LOW_NAME_ONLY", "AMBIGUOUS_MULTI_CANDIDATE", "NO_CANDIDATE"]
ACTION_HELP = {
    "CHOW_SUCCESSOR": "Change of ownership: old account keeps parent + billing; new successor account is created and linked.",
    "REPARENT": "Direct parent_id change (no CHOW because revenue and AR are not both positive).",
    "UPDATE_FIELDS": "Field corrections on an existing Bellhaven account.",
    "CREATE_MISSING": "Website facility with no CRM account at that address.",
    "DUPLICATE": "Two Bellhaven accounts at one address: loser -> Inactive + duplicate_of_account.",
    "MISSING_FROM_SITE": "Bellhaven child absent from the complete website scan -> status 'Needs Review' + note.",
    "MANUAL_REVIEW": "Conflicting or insufficient evidence. No CRM change is possible from this screen.",
    "NO_CHANGE": "Already correct. Informational.",
}


@st.cache_resource
def get_store() -> Store:
    return Store(config.get_settings().db_path)


settings = config.get_settings()
store = get_store()


def _j(row, col: str) -> Any:
    v = row[col]
    return json.loads(v) if v else None


# ------------------------------------------------------------------------------ sidebar
with st.sidebar:
    st.title("Bellhaven ↔ CRM")
    st.caption("Review queue. Nothing is written to the CRM unless you click **Approve & apply** on one proposal.")
    if settings.has_token:
        st.success("API token configured (from .env / environment)")
    else:
        st.error("BELLHAVEN_API_TOKEN not configured. Scans and approvals are disabled. See .env.example.")

    run = store.latest_run()
    if run:
        st.markdown(
            f"**Last scan** `{run['run_id']}`  \n"
            f"site: **{run['site_count']}** · CRM: **{run['crm_count']}** · Bellhaven children: **{run['bellhaven_child_count']}**  \n"
            f"scrape complete: **{'yes' if run['scrape_ok'] else 'NO'}**"
        )
        if run["error_summary"]:
            st.warning(run["error_summary"])
    else:
        st.info("No scan yet.")

    if st.button("Run read-only scan now", disabled=not settings.has_token, help="Scrapes the site, fetches every CRM account, regenerates proposals. Zero CRM writes."):
        with st.spinner("Scanning (read-only)…"):
            report, err = safe_run_scan(settings, store, make_client(settings, allow_writes=False))
        if err:
            st.error(err)
        else:
            st.success(f"run {report.run_id}: site={report.site_count} crm={report.crm_count} proposals={sum(report.proposals_by_action.values())} crm_writes={report.crm_writes}")
            st.json(report.upsert_counts)
        st.rerun()

    st.divider()
    st.subheader("Filters")
    f_actions = st.multiselect("Action type", list(ALL_ACTIONS), default=[a for a in ALL_ACTIONS if a != ACTION_NO_CHANGE])
    f_decisions = st.multiselect("Decision", [DECISION_PENDING, DECISION_APPROVED, DECISION_REJECTED, DECISION_ACKNOWLEDGED], default=[DECISION_PENDING])
    f_tiers = st.multiselect("Confidence tier", TIERS, default=[])
    f_search = st.text_input("Search (facility, city, id)")
    f_stale = st.checkbox("Include stale (not produced by latest scan)", value=False)

# ------------------------------------------------------------------------------ counters
counts = store.counts()
c1, c2, c3, c4, c5, c6, c7, c8 = st.columns(8)
c1.metric("Website facilities", run["site_count"] if run else "–")
c2.metric("CRM accounts", run["crm_count"] if run else "–")
c3.metric("Bellhaven children", run["bellhaven_child_count"] if run else "–")
c4.metric("Pending (actionable)", counts["pending_actionable"])
c5.metric("Approved", counts["by_decision"].get(DECISION_APPROVED, 0))
c6.metric("Rejected", counts["by_decision"].get(DECISION_REJECTED, 0))
c7.metric("Applied ✓", counts["by_apply"].get(APPLY_APPLIED, 0))
c8.metric("Failed/blocked", sum(counts["by_apply"].get(k, 0) for k in (APPLY_FAILED, APPLY_BLOCKED, APPLY_NEEDS_RECOVERY, APPLY_IN_PROGRESS)))

with st.expander("Proposals by type", expanded=False):
    st.table({"type": list(counts["by_action"].keys()), "count": list(counts["by_action"].values())})

# ------------------------------------------------------------------------------ queue
rows = store.list_proposals(include_stale=f_stale, action_types=f_actions or None, decisions=f_decisions or None, tiers=f_tiers or None, search=f_search)
st.subheader(f"Queue ({len(rows)})")
if not rows:
    st.info("No proposals match the filters.")
    st.stop()

labels = {r["proposal_key"]: f"[{r['action_type']}] {r['title']}  ·  {r['tier']}  ·  {r['decision']}/{r['apply_status']}" for r in rows}
table = [
    {"action": r["action_type"], "title": r["title"], "tier": r["tier"], "decision": r["decision"], "apply": r["apply_status"], "key": r["proposal_key"]}
    for r in rows
]
st.dataframe(table, width="stretch", hide_index=True, height=min(400, 38 + 35 * len(table)))

selected_key = st.selectbox("Open proposal", options=[r["proposal_key"] for r in rows], format_func=lambda k: labels[k])
row = store.get_proposal_row(selected_key)
p: Proposal = Proposal.from_dict(_j(row, "data_json"))

# ------------------------------------------------------------------------------ detail
st.divider()
st.markdown(f"## {p.title}")
st.caption(f"{ACTION_HELP.get(p.action_type, '')}  \nkey `{p.proposal_key}` · tier **{p.tier}** · decision **{row['decision']}** · apply **{row['apply_status']}** · first seen `{row['first_seen_run']}` · last seen `{row['last_seen_run']}`"
           + (f" · ⚠ stale, superseded by `{row['superseded_by']}`" if row["superseded_by"] else ""))
st.markdown("**Reason codes:** " + " ".join(f"`{r}`" for r in p.reason_codes))

if p.action_type == ACTION_CHOW_SUCCESSOR:
    st.error(
        "**CHANGE OF OWNERSHIP (CHOW) — billing-sensitive.** The old account has positive lifetime revenue **and** positive outstanding AR. "
        "Its parent, status, name, financial fields and note will **not** be modified. Approving performs exactly two API calls: "
        "(1) `POST /accounts` creates a new successor account under Bellhaven with the website's current details; "
        "(2) `PATCH /accounts/{old}` sets **only** `chow_current_account` to the new id. Both records are then read back and verified. "
        "If step 1 succeeds and step 2 fails, the created id is persisted locally and a retry links it instead of creating another."
    )
for note in p.risk_notes:
    st.warning(note)

left, right = st.columns(2)
with left:
    st.markdown("### Website evidence")
    if p.site:
        s = p.site
        st.markdown(
            f"**{s['name']}**  \n{s['street']}  \n{s['city']}, {s['state']} {s['zip']}  \n"
            f"Care offerings: {', '.join(s['care_offerings'])}  \n"
            f"Phone: {s.get('phone') or '–'} · Administrator: {s.get('administrator') or '–'}  \n"
            f"Source: [{s['source_url']}]({s['source_url']})  \nScraped {s['scraped_at']} via {', '.join(s.get('discovered_via') or [])}"
        )
    else:
        ev = p.evidence.get("site_scan", {})
        st.markdown(f"Not listed on the website. Scan complete: **{ev.get('complete')}**, facilities found: **{ev.get('facility_count')}**.")
    if p.evidence.get("chow_rule"):
        st.markdown(f"CHOW inputs: `{p.evidence['chow_rule']}` · requires_chow = **{p.requires_chow}**")

with right:
    st.markdown("### Current CRM account(s)")
    crm_accs = p.evidence.get("crm_accounts") or []
    if crm_accs:
        st.dataframe(crm_accs, width="stretch", hide_index=True)
    elif p.before:
        st.dataframe(list(p.before.values()), width="stretch", hide_index=True)
    else:
        st.markdown("_No existing CRM account._")
    same = p.evidence.get("same_address_accounts") or []
    if same:
        st.markdown("**Other accounts at the same address:**")
        st.dataframe(same, width="stretch", hide_index=True)

if p.candidates:
    st.markdown("### All candidate CRM accounts considered")
    st.dataframe(
        [
            {"eligible": c.eligible, "tier": c.tier, "id": c.account_id, "name": c.name, "parent_id": c.parent_id, "status": c.status, "address": c.address,
             "revenue": c.lifetime_revenue, "AR": c.outstanding_ar, "chow→": c.chow_current_account, "dup_of": c.duplicate_of_account,
             "name_score": c.name_score, "street_score": c.street_score, "phone": c.phone_match, "admin": c.admin_match, "reasons": ", ".join(c.reasons)}
            for c in p.candidates
        ],
        width="stretch",
        hide_index=True,
    )

st.markdown("### Proposed field-level changes")
if p.changes:
    st.dataframe([{"account": c.account_id, "field": c.field, "before": c.before, "after": c.after} for c in p.changes], width="stretch", hide_index=True)
    st.markdown("**API steps on approval:**")
    for s_ in p.steps:
        st.markdown(f"{s_.order}. `{s_.kind.upper()}` {s_.description}")
        st.code(json.dumps(s_.payload, indent=2), language="json")
else:
    st.markdown("_No CRM mutation. This item is informational or requires a human decision outside this tool._")

# ------------------------------------------------------------------------------ ledger / result
steps = store.steps(p.proposal_key)
if steps or row["result_json"] or row["error"]:
    st.markdown("### Apply ledger")
    if row["error"]:
        st.error(row["error"])
    if row["result_json"]:
        res = _j(row, "result_json")
        if row["apply_status"] == APPLY_APPLIED:
            st.success(res.get("message", "applied"))
        if res.get("verified"):
            st.markdown("**Read-back verification (GET after write):**")
            for acc_id, snap in res["verified"].items():
                st.markdown(f"`{acc_id}`")
                st.json(snap, expanded=False)
    if steps:
        st.dataframe([{k: s[k] for k in ("step_id", "step_name", "status", "crm_account_id", "resulting_id", "attempted_at", "error")} for s in steps], width="stretch", hide_index=True)

# ------------------------------------------------------------------------------ controls
st.divider()
st.markdown("### Decision")
decision, apply_status = row["decision"], row["apply_status"]
applicable = p.action_type in APPLICABLE_ACTIONS and bool(p.steps)
pending = decision == DECISION_PENDING
retryable = decision == DECISION_APPROVED and apply_status in (APPLY_FAILED, APPLY_NEEDS_RECOVERY)

if row["stale"]:
    st.warning("This proposal was not produced by the latest scan (evidence changed or it was resolved). Run a scan and review the replacement; it cannot be applied.")

if (pending or retryable) and not row["stale"]:
    col_a, col_b = st.columns(2)
    with col_a:
        if applicable:
            st.markdown("**Approve & apply** performs the API steps listed above for this proposal only, after re-fetching the account(s) and re-checking the CHOW rule.")
            confirm = st.checkbox("I have reviewed every field change and the evidence above", key=f"confirm-{p.proposal_key}")
            note = st.text_input("Reviewer note (optional)", key=f"note-{p.proposal_key}")
            label = "Retry / recover apply" if retryable else "Approve & apply"
            if st.button(label, type="primary", disabled=not (confirm and settings.has_token), key=f"approve-{p.proposal_key}"):
                with st.spinner("Preflight, apply, read-back…"):
                    client = make_client(settings, allow_writes=True)  # the ONLY writable client in the app
                    result = apply_proposal(store, client, run["bellhaven_parent_id"], p.proposal_key, note)
                if result.ok:
                    st.success(result.message)
                else:
                    st.error(result.message)
                st.rerun()
        else:
            if "MULTI_PARENT_HISTORY" in p.reason_codes and p.site and run:
                st.markdown(
                    "**Several accounts under other parents share this address.** If you can determine which record represents the "
                    "current facility, select it below. This creates a normal re-parent / CHOW proposal (the CHOW rule is applied to "
                    "that account's revenue and AR) which you then review and approve separately. Nothing is written by this step."
                )
                options = [c for c in p.candidates if c.eligible]
                pick = st.selectbox(
                    "Account that represents the current facility",
                    options=[c.account_id for c in options],
                    format_func=lambda i: next(f"{c.name} · parent {c.parent_id or '(none)'} · rev {c.lifetime_revenue} / AR {c.outstanding_ar} · {i}" for c in options if c.account_id == i),
                    key=f"pick-{p.proposal_key}",
                )
                why = st.text_input("Why this account? (required)", key=f"pickwhy-{p.proposal_key}")
                if st.button("Create re-parent/CHOW proposal for the selected account", disabled=not why.strip(), key=f"pickbtn-{p.proposal_key}"):
                    from bellhaven.models import CRMAccount, SiteFacility
                    from bellhaven.rules import RuleContext, reviewer_selected_proposal

                    accounts = []
                    for d in store.crm_snapshot(run["run_id"]):
                        a = CRMAccount.from_api(d["raw"])
                        a.contacts = d.get("contacts", [])
                        accounts.append(a)
                    ctx = RuleContext(run["bellhaven_parent_id"], next(a.name for a in accounts if a.id == run["bellhaven_parent_id"]), accounts, bool(run["scrape_ok"]), int(run["site_count"] or 0))
                    site = SiteFacility.from_dict(p.site)
                    chosen = next(a for a in accounts if a.id == pick)
                    newp = reviewer_selected_proposal(site, chosen, ctx, p.candidates, why.strip())
                    created = store.add_reviewer_proposal(run["run_id"], newp, why.strip())
                    store.acknowledge(p.proposal_key, f"reviewer selected {pick}: {why.strip()}")
                    st.success(f"{'Created' if created else 'Already exists'}: {newp.action_type} proposal {newp.proposal_key}. Select it in the queue to review and approve.")
                    st.rerun()
            st.markdown("No CRM change is possible for this item as-is. Mark it reviewed to clear it from the pending queue (local ledger only).")
            ack = st.text_input("Reviewer note", key=f"ack-{p.proposal_key}")
            if st.button("Mark reviewed (no CRM change)", key=f"ackbtn-{p.proposal_key}", disabled=not pending):
                store.acknowledge(p.proposal_key, ack)
                st.rerun()
    with col_b:
        st.markdown("**Reject** records your decision locally. It never touches the CRM and the item will not come back on future scans.")
        reason = st.text_input("Rejection reason (required)", key=f"reject-reason-{p.proposal_key}")
        if st.button("Reject", disabled=not reason.strip() or not pending, key=f"reject-{p.proposal_key}"):
            try:
                store.reject(p.proposal_key, reason.strip())
                st.rerun()
            except AlreadyClaimed as exc:
                st.error(str(exc))
elif apply_status == APPLY_BLOCKED:
    st.warning("Apply was blocked by preflight (CRM data changed since this proposal was generated). Run a read-only scan, then review the refreshed proposal.")
elif apply_status == APPLY_IN_PROGRESS:
    st.warning("An apply attempt is recorded as in progress (possibly interrupted). Use the Retry button after confirming no other apply is running.")
    if st.button("Mark interrupted attempt as needing recovery", key=f"recover-{p.proposal_key}"):
        store._conn.execute("UPDATE proposals SET apply_status=? WHERE proposal_key=? AND apply_status=?", (APPLY_NEEDS_RECOVERY, p.proposal_key, APPLY_IN_PROGRESS))
        store.log("marked_needs_recovery", key=p.proposal_key)
        st.rerun()
else:
    st.info(f"Decision: **{decision}** at {row['decided_at']} · note: {row['reviewer_note'] or '–'} · apply: **{apply_status}**")
    if decision in (DECISION_REJECTED, DECISION_ACKNOWLEDGED):
        why = st.text_input("Reason to reopen", key=f"reopen-{p.proposal_key}")
        if st.button("Reopen (explicit reviewer action)", disabled=not why.strip(), key=f"reopenbtn-{p.proposal_key}"):
            store.reopen(p.proposal_key, why.strip())
            st.rerun()

with st.expander("Audit log (latest 50)"):
    st.dataframe([dict(a) for a in store.audit_log(50)], width="stretch", hide_index=True)
