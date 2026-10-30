"""Proposal generation: turn match results + CRM state into reviewable, atomic proposals.

Nothing in this module talks to the network. It is pure: (site, CRM) -> proposals.

The CHOW invariant lives in :func:`requires_chow` and is re-evaluated by
``bellhaven.apply`` against freshly fetched data immediately before any write.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from . import config
from .matching import MatchResult
from .models import (
    ACTION_CHOW_SUCCESSOR,
    ACTION_CREATE_MISSING,
    ACTION_DUPLICATE,
    ACTION_MANUAL_REVIEW,
    ACTION_MISSING_FROM_SITE,
    ACTION_NO_CHANGE,
    ACTION_REPARENT,
    ACTION_UPDATE_FIELDS,
    TIER_AMBIGUOUS,
    TIER_HIGH_ADDRESS_EXACT,
    TIER_LOW_NAME_ONLY,
    TIER_MEDIUM_ADDRESS_FUZZY,
    TIER_NO_CANDIDATE,
    Candidate,
    CRMAccount,
    FieldChange,
    MutationStepPlan,
    Proposal,
    SiteFacility,
)
from .normalize import map_care_offerings, norm_name, norm_text

CHOW_MARKER_PREFIX = "chow-successor-of:"


class UnsafeFinancials(Exception):
    """Financial fields are missing / unparseable; no ownership write is safe."""


def requires_chow(account: CRMAccount, target_parent_id: str) -> bool:
    """The non-negotiable billing rule.

    ``requires_chow = (old_parent_id != target_parent_id) and (lifetime_revenue > 0) and (outstanding_ar > 0)``

    Raises :class:`UnsafeFinancials` when either amount is missing or
    unparseable -- "unknown" is not zero.
    """
    if (account.parent_id or "") == (target_parent_id or ""):
        return False
    rev = account.lifetime_revenue
    ar = account.outstanding_ar
    if rev is None or ar is None:
        raise UnsafeFinancials(
            f"account {account.id}: lifetime_revenue={account.lifetime_revenue_raw!r} "
            f"outstanding_ar={account.outstanding_ar_raw!r} not parseable as amounts"
        )
    return rev > 0 and ar > 0


def chow_marker(old_account_id: str) -> str:
    return f"{config.NOTE_TAG} {CHOW_MARKER_PREFIX}{old_account_id}"


def append_note(existing: str, text: str) -> str:
    existing = (existing or "").rstrip()
    return f"{existing}\n{text}".strip() if existing else text


@dataclass
class RuleContext:
    bellhaven_parent_id: str
    bellhaven_parent_name: str
    accounts: list[CRMAccount]
    scrape_complete: bool
    site_count: int
    observed_statuses: set[str] = field(default_factory=set)
    observed_care_types: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.by_id = {a.id: a for a in self.accounts}
        if not self.observed_statuses:
            self.observed_statuses = {a.status for a in self.accounts if a.status}
        if not self.observed_care_types:
            self.observed_care_types = {a.care_type for a in self.accounts if a.care_type}

    def parent_name(self, parent_id: str | None) -> str:
        if not parent_id:
            return "(none)"
        p = self.by_id.get(parent_id)
        return p.name if p else f"(unknown {parent_id})"


# --- field-level comparisons -----------------------------------------------------


def _care_type_change(site: SiteFacility, acc: CRMAccount, risks: list[str]) -> str | None:
    mapped = map_care_offerings(site.care_offerings)
    unmapped = [o for o in site.care_offerings if o not in mapped and map_care_offerings([o]) == []]
    if unmapped:
        risks.append(f"site offerings without a CRM mapping kept as evidence only: {unmapped}")
    if not mapped:
        return None
    current = (acc.care_type or "").strip()
    if len(mapped) == 1:
        return mapped[0] if current != mapped[0] else None
    # multi-valued site offering vs single-valued CRM field
    if current in mapped:
        return None  # already a valid value; do not churn on list ordering
    if not current:
        risks.append(f"site lists {mapped}; CRM care_type empty, proposing first listed value {mapped[0]!r}")
        return mapped[0]
    risks.append(
        f"site lists multiple offerings {mapped} but CRM care_type is {current!r}; "
        "left unchanged (no single defensible mapping) -- all offerings kept in evidence"
    )
    return None


def field_updates(site: SiteFacility, acc: CRMAccount, address_differs: bool) -> tuple[list[FieldChange], list[str], list[str]]:
    changes: list[FieldChange] = []
    reasons: list[str] = []
    risks: list[str] = []
    if norm_name(site.name) != norm_name(acc.name):
        changes.append(FieldChange(acc.id, "name", acc.name, site.name))
        reasons.append("NAME_OUTDATED")
    ct = _care_type_change(site, acc, risks)
    if ct is not None:
        changes.append(FieldChange(acc.id, "care_type", acc.care_type, ct))
        reasons.append("CARE_TYPE_DIFFERS")
    if site.phone_norm and site.phone_norm != acc.phone_norm:
        changes.append(FieldChange(acc.id, "phone", acc.phone, site.phone))
        reasons.append("PHONE_DIFFERS" if acc.phone_norm else "PHONE_MISSING")
    if address_differs:
        for fld, sval, aval in (
            ("street", site.street, acc.street),
            ("city", site.city, acc.city),
            ("state", site.state, acc.state),
            ("zip", site.zip, acc.zip),
        ):
            if norm_text(sval) != norm_text(aval):
                changes.append(FieldChange(acc.id, fld, aval, sval))
        reasons.append("ADDRESS_CORRECTION")
    elif not acc.addr.zip5 and site.zip:
        changes.append(FieldChange(acc.id, "zip", acc.zip, site.zip))
        reasons.append("ZIP_MISSING")
    return changes, reasons, risks


def _apply_changes(snapshot: dict[str, Any], changes: list[FieldChange]) -> dict[str, Any]:
    after = dict(snapshot)
    for c in changes:
        after[c.field] = c.after
    return after


def _patch_step(acc_id: str, changes: list[FieldChange], order: int, description: str) -> MutationStepPlan:
    return MutationStepPlan(
        order=order,
        kind="patch",
        account_id=acc_id,
        payload={c.field: c.after for c in changes},
        description=description,
    )


def _site_evidence(site: SiteFacility) -> dict[str, Any]:
    return {
        "site_name": site.name,
        "site_address": f"{site.street}, {site.city}, {site.state} {site.zip}",
        "site_care_offerings": list(site.care_offerings),
        "site_phone": site.phone,
        "site_administrator": site.administrator,
        "source_url": site.source_url,
        "scraped_at": site.scraped_at,
        "discovered_via": list(site.discovered_via),
    }


def _acc_summary(ctx: RuleContext, acc: CRMAccount) -> dict[str, Any]:
    return {
        "id": acc.id,
        "name": acc.name,
        "parent_id": acc.parent_id,
        "parent_name": ctx.parent_name(acc.parent_id),
        "status": acc.status,
        "address": f"{acc.street}, {acc.city}, {acc.state} {acc.zip}",
        "lifetime_revenue": None if acc.lifetime_revenue is None else str(acc.lifetime_revenue),
        "outstanding_ar": None if acc.outstanding_ar is None else str(acc.outstanding_ar),
        "chow_current_account": acc.chow_current_account,
        "duplicate_of_account": acc.duplicate_of_account,
    }


# --- per-site proposal building ----------------------------------------------------


def _successor_payload(site: SiteFacility, old: CRMAccount, ctx: RuleContext) -> tuple[dict[str, Any], list[str]]:
    risks: list[str] = []
    mapped = map_care_offerings(site.care_offerings)
    care = mapped[0] if mapped else ""
    if len(mapped) > 1:
        risks.append(f"site lists {mapped}; successor care_type set to first value {care!r}, all offerings recorded in note")
    note = chow_marker(old.id) + f" successor of {old.name!r} created from {site.source_url}"
    if len(site.care_offerings) > 1:
        note += f"; site care offerings: {', '.join(site.care_offerings)}"
    payload = {
        "name": site.name,
        "parent_id": ctx.bellhaven_parent_id,
        "status": config.STATUS_ACTIVE,
        "care_type": care,
        "phone": site.phone,
        "street": site.street,
        "city": site.city,
        "state": site.state,
        "zip": site.zip,
        "note": note,
    }
    return payload, risks


def _build_for_site(mr: MatchResult, ctx: RuleContext) -> list[Proposal]:
    site = mr.site
    proposals: list[Proposal] = []
    bell = ctx.bellhaven_parent_id
    evidence = {"site": _site_evidence(site), "match_tier": mr.tier, "match_reasons": list(mr.reason_codes)}
    cands = mr.candidates

    def manual(title: str, reasons: list[str], risks: list[str], ids: list[str], tier: str) -> Proposal:
        return Proposal(
            action_type=ACTION_MANUAL_REVIEW,
            tier=tier,
            reason_codes=reasons,
            title=title,
            site=site.to_dict(),
            crm_account_ids=ids,
            before={i: ctx.by_id[i].snapshot() for i in ids if i in ctx.by_id},
            after={},
            changes=[],
            steps=[],
            candidates=cands,
            evidence={**evidence, "crm_accounts": [_acc_summary(ctx, ctx.by_id[i]) for i in ids if i in ctx.by_id]},
            risk_notes=risks,
            target_parent_id=bell,
        )

    if "CRM_ACCOUNT_CONTESTED" in mr.reason_codes:
        ids = [c.account_id for c in mr.eligible]
        return [manual(f"{site.name}: CRM account claimed by more than one website facility", ["CRM_ACCOUNT_CONTESTED"], ["The same CRM record matches two different website locations; resolve manually."], ids, TIER_AMBIGUOUS)]

    eligible = mr.high or (mr.medium if len(mr.medium) == 1 else [])
    if mr.tier == TIER_AMBIGUOUS:
        return [manual(f"{site.name}: several fuzzy address candidates", mr.reason_codes, ["Multiple CRM accounts have similar but not identical addresses."], [c.account_id for c in mr.eligible], TIER_AMBIGUOUS)]

    # ---- no address match ------------------------------------------------------------
    if not eligible:
        if mr.tier == TIER_LOW_NAME_ONLY:
            ids = [c.account_id for c in cands if not c.eligible and "STATE_MISMATCH" not in c.reasons]
            return [
                manual(
                    f"{site.name}: name matches a CRM account at a different street",
                    ["NAME_MATCH_ADDRESS_CONFLICT"],
                    [
                        "Website street address does not match the similarly named CRM account. "
                        "This could be a relocation, a different facility, or a CRM address error. "
                        "No automatic match; do not assume an ownership correction.",
                    ],
                    ids,
                    TIER_LOW_NAME_ONLY,
                )
            ]
        # CREATE_MISSING
        mapped = map_care_offerings(site.care_offerings)
        risks: list[str] = []
        if any("STATE_MISMATCH" in c.reasons for c in cands):
            others = [f"{c.name} ({c.address}) id={c.account_id}" for c in cands if "STATE_MISMATCH" in c.reasons]
            risks.append(f"Same-name CRM account(s) exist in a different state and were NOT matched: {others}")
        care = mapped[0] if mapped else ""
        if len(mapped) > 1:
            risks.append(f"site lists {mapped}; new account care_type will be first value {care!r}")
        payload = {
            "name": site.name,
            "parent_id": bell,
            "status": config.STATUS_ACTIVE,
            "care_type": care,
            "phone": site.phone,
            "street": site.street,
            "city": site.city,
            "state": site.state,
            "zip": site.zip,
            "note": f"{config.NOTE_TAG} created from {site.source_url}; no CRM account matched this address"
            + (f"; site care offerings: {', '.join(site.care_offerings)}" if len(site.care_offerings) > 1 else ""),
        }
        return [
            Proposal(
                action_type=ACTION_CREATE_MISSING,
                tier=TIER_NO_CANDIDATE,
                reason_codes=mr.reason_codes + ["NO_ADDRESS_MATCH"],
                title=f"{site.name}: create new account under {ctx.bellhaven_parent_name}",
                site=site.to_dict(),
                crm_account_ids=[],
                before={},
                after={"NEW": payload},
                changes=[FieldChange("NEW", k, None, v) for k, v in payload.items()],
                steps=[MutationStepPlan(1, "create", None, payload, f"POST /accounts: new account {site.name!r} under {ctx.bellhaven_parent_name}")],
                candidates=cands,
                evidence=evidence,
                risk_notes=risks,
                target_parent_id=bell,
            )
        ]

    # ---- address match(es) -----------------------------------------------------------
    address_fuzzy = eligible[0].tier == TIER_MEDIUM_ADDRESS_FUZZY
    group = [ctx.by_id[c.account_id] for c in eligible if c.account_id in ctx.by_id]

    # resolved duplicates: follow duplicate_of pointers into the group / Bellhaven
    losers = [a for a in group if a.is_inactive and a.duplicate_of_account]
    current = [a for a in group if (a.parent_id or "") == bell and a not in losers]
    others = [a for a in group if a not in current and a not in losers]
    # CHOW already applied: an old record whose chow_current_account points at a current Bellhaven account
    current_ids = {a.id for a in current}
    resolved_history = [a for a in others if a.chow_current_account and a.chow_current_account in current_ids]
    others = [a for a in others if a not in resolved_history]

    cand_by_id = {c.account_id: c for c in cands}

    # Case: current Bellhaven account(s) exist -------------------------------------------
    if current:
        active_current = [a for a in current if not a.is_inactive]
        inactive_current = [a for a in current if a.is_inactive]
        if not active_current:
            ids = [a.id for a in inactive_current]
            proposals.append(manual(f"{site.name}: matching Bellhaven account is Inactive but facility is listed on the website", ["INACTIVE_BUT_LISTED_ON_SITE"], ["Reactivation is a human decision; verify before changing status."], ids, mr.tier))
            return proposals
        if len(active_current) > 1:
            proposals.extend(_duplicate_proposals(site, active_current, ctx, cands, evidence))
            survivor = _choose_survivor(site, active_current, cand_by_id)
            acc = survivor if survivor else active_current[0]
        else:
            acc = active_current[0]

        changes, reasons, risks = field_updates(site, acc, address_fuzzy)
        if changes:
            tier = TIER_MEDIUM_ADDRESS_FUZZY if address_fuzzy else TIER_HIGH_ADDRESS_EXACT
            proposals.append(
                Proposal(
                    action_type=ACTION_UPDATE_FIELDS,
                    tier=tier,
                    reason_codes=reasons,
                    title=f"{site.name}: update {', '.join(c.field for c in changes)} on {acc.name!r}",
                    site=site.to_dict(),
                    crm_account_ids=[acc.id],
                    before={acc.id: acc.snapshot()},
                    after={acc.id: _apply_changes(acc.snapshot(), changes)},
                    changes=changes,
                    steps=[_patch_step(acc.id, changes, 1, f"PATCH /accounts/{acc.id}: {', '.join(c.field for c in changes)}")],
                    candidates=cands,
                    evidence={**evidence, "crm_accounts": [_acc_summary(ctx, acc)]},
                    risk_notes=risks,
                    target_parent_id=bell,
                    requires_chow=False,
                )
            )
        else:
            proposals.append(
                Proposal(
                    action_type=ACTION_NO_CHANGE,
                    tier=TIER_HIGH_ADDRESS_EXACT,
                    reason_codes=["ALREADY_CORRECT"],
                    title=f"{site.name}: CRM account {acc.name!r} already correct",
                    site=site.to_dict(),
                    crm_account_ids=[acc.id],
                    before={acc.id: acc.snapshot()},
                    after={},
                    changes=[],
                    steps=[],
                    candidates=cands,
                    evidence={**evidence, "crm_accounts": [_acc_summary(ctx, acc)]},
                    risk_notes=risks,
                    target_parent_id=bell,
                    requires_chow=False,
                )
            )
        # cross-parent accounts at the same address: historical lineage, surface for review only
        for other in others:
            proposals.append(
                manual(
                    f"{site.name}: historical/cross-parent account {other.name!r} shares this address",
                    ["CROSS_PARENT_SAME_ADDRESS_HISTORY"],
                    [
                        f"{other.name!r} (parent {ctx.parent_name(other.parent_id)}) sits at the same address as the current "
                        f"Bellhaven account {acc.name!r}. This is likely prior-ownership history, NOT an accidental duplicate. "
                        "No automatic change. Revenue/AR on the historical record must be preserved.",
                    ],
                    [other.id, acc.id],
                    TIER_HIGH_ADDRESS_EXACT,
                )
            )
        return proposals

    # Case: no current Bellhaven account, but account(s) under other parents ------------------
    if len(others) > 1:
        return [
            manual(
                f"{site.name}: multiple non-Bellhaven accounts share this address",
                ["MULTI_PARENT_HISTORY"],
                ["More than one account under other parents sits at this address; choose which record represents the facility manually."],
                [a.id for a in others],
                TIER_AMBIGUOUS,
            )
        ]
    if not others:
        # only losers / resolved history remain -- nothing current. Treat as needing a human.
        return [manual(f"{site.name}: only inactive/duplicate-marked accounts match this address", ["ONLY_INACTIVE_MATCHES"], ["Matching accounts are Inactive duplicates; decide whether to reactivate or create."], [a.id for a in losers], mr.tier)]

    return [ownership_proposal(site, others[0], ctx, cands, evidence, address_fuzzy, current_ids)]


def ownership_proposal(
    site: SiteFacility,
    old: CRMAccount,
    ctx: RuleContext,
    cands: list[Candidate],
    evidence: dict[str, Any],
    address_fuzzy: bool = False,
    current_ids: set[str] | None = None,
    extra_reasons: list[str] | None = None,
    extra_risks: list[str] | None = None,
) -> Proposal:
    """REPARENT or CHOW_SUCCESSOR for one wrong-parent account (or MANUAL_REVIEW when unsafe).

    Also used when a reviewer explicitly selects which of several same-address accounts
    represents the current facility; the CHOW rule is applied identically.
    """
    bell = ctx.bellhaven_parent_id
    current_ids = current_ids or set()
    extra_reasons = extra_reasons or []
    extra_risks = extra_risks or []
    tier_in = TIER_MEDIUM_ADDRESS_FUZZY if address_fuzzy else TIER_HIGH_ADDRESS_EXACT

    def manual(title: str, reasons: list[str], risks: list[str], ids: list[str], tier: str) -> Proposal:
        return Proposal(
            action_type=ACTION_MANUAL_REVIEW,
            tier=tier,
            reason_codes=reasons + extra_reasons,
            title=title,
            site=site.to_dict(),
            crm_account_ids=ids,
            before={i: ctx.by_id[i].snapshot() for i in ids if i in ctx.by_id},
            after={},
            changes=[],
            steps=[],
            candidates=cands,
            evidence={**evidence, "crm_accounts": [_acc_summary(ctx, ctx.by_id[i]) for i in ids if i in ctx.by_id]},
            risk_notes=risks + extra_risks,
            target_parent_id=bell,
        )

    if old.chow_current_account and old.chow_current_account not in current_ids:
        target = ctx.by_id.get(old.chow_current_account)
        return manual(
            f"{site.name}: {old.name!r} already has a CHOW link to {old.chow_current_account}",
            ["CHOW_LINK_TO_UNEXPECTED_ACCOUNT"],
            [f"chow_current_account points at {target.name if target else 'an unknown account'} which is not a current Bellhaven account at this address."],
            [old.id] + ([target.id] if target else []),
            tier_in,
        )
    if old.is_inactive:
        return manual(f"{site.name}: wrong-parent account {old.name!r} is Inactive", ["INACTIVE_WRONG_PARENT"], ["An inactive account under another parent matches the address; human must decide reactivation vs new account."], [old.id], tier_in)

    try:
        chow = requires_chow(old, bell)
    except UnsafeFinancials as exc:
        return manual(f"{site.name}: financial fields unparseable on {old.name!r}", ["FINANCIALS_UNPARSEABLE"], [str(exc), "No ownership write is safe until revenue/AR are known."], [old.id], tier_in)

    tier = tier_in
    fin = f"lifetime_revenue={old.lifetime_revenue} outstanding_ar={old.outstanding_ar}"
    if chow:
        payload, risks = _successor_payload(site, old, ctx)
        risks.extend(extra_risks)
        risks.insert(
            0,
            f"CHOW: {old.name!r} has positive lifetime revenue AND positive outstanding AR ({fin}). "
            f"Its parent ({ctx.parent_name(old.parent_id)}), status, name, financials and note are left untouched. "
            "A new successor account is created under Bellhaven and only chow_current_account on the old record is set.",
        )
        after_old = dict(old.snapshot())
        after_old["chow_current_account"] = "<successor id>"
        return Proposal(
                action_type=ACTION_CHOW_SUCCESSOR,
                tier=tier,
                reason_codes=["WRONG_PARENT", "REVENUE_POSITIVE", "AR_POSITIVE", "CHOW_REQUIRED"] + (["NAME_OUTDATED"] if norm_name(old.name) != norm_name(site.name) else []) + extra_reasons,
                title=f"{site.name}: CHOW -- create successor under {ctx.bellhaven_parent_name}, link from {old.name!r}",
                site=site.to_dict(),
                crm_account_ids=[old.id],
                before={old.id: old.snapshot()},
                after={old.id: after_old, "NEW": payload},
                changes=[FieldChange(old.id, "chow_current_account", old.chow_current_account, "<successor id>")]
                + [FieldChange("NEW", k, None, v) for k, v in payload.items()],
                steps=[
                    MutationStepPlan(1, "create", None, payload, f"POST /accounts: successor {site.name!r} under {ctx.bellhaven_parent_name}"),
                    MutationStepPlan(2, "patch", old.id, {"chow_current_account": "<successor id>"}, f"PATCH /accounts/{old.id}: chow_current_account = <successor id> (nothing else)"),
                ],
                candidates=cands,
                evidence={**evidence, "crm_accounts": [_acc_summary(ctx, old)], "chow_rule": fin},
                risk_notes=risks,
                target_parent_id=bell,
                requires_chow=True,
            )

    # direct re-parent (+ bundled, separately listed field corrections)
    changes, reasons, risks = field_updates(site, old, address_fuzzy)
    changes.insert(0, FieldChange(old.id, "parent_id", old.parent_id, bell))
    reasons = ["WRONG_PARENT" if old.parent_id else "PARENT_MISSING", "CHOW_NOT_REQUIRED"] + reasons + extra_reasons
    risks.insert(0, f"Direct re-parent allowed: {fin} (CHOW needs BOTH revenue > 0 AND AR > 0).")
    risks.extend(extra_risks)
    return Proposal(
            action_type=ACTION_REPARENT,
            tier=tier,
            reason_codes=reasons,
            title=f"{site.name}: re-parent {old.name!r} from {ctx.parent_name(old.parent_id)} to {ctx.bellhaven_parent_name}"
            + (f" (+ {', '.join(c.field for c in changes[1:])})" if len(changes) > 1 else ""),
            site=site.to_dict(),
            crm_account_ids=[old.id],
            before={old.id: old.snapshot()},
            after={old.id: _apply_changes(old.snapshot(), changes)},
            changes=changes,
            steps=[_patch_step(old.id, changes, 1, f"PATCH /accounts/{old.id}: {', '.join(c.field for c in changes)}")],
            candidates=cands,
            evidence={**evidence, "crm_accounts": [_acc_summary(ctx, old)], "chow_rule": fin},
            risk_notes=risks,
            target_parent_id=bell,
            requires_chow=False,
        )


def reviewer_selected_proposal(site: SiteFacility, account: CRMAccount, ctx: RuleContext, candidates: list[Candidate], reviewer_reason: str) -> Proposal:
    """A reviewer picked which of several same-address accounts is the current facility.

    Produces the same guarded REPARENT / CHOW_SUCCESSOR proposal the scan would have produced
    for a single candidate. Still requires Approve & apply, preflight and the CHOW re-check.
    """
    evidence = {"site": _site_evidence(site), "match_tier": TIER_AMBIGUOUS, "match_reasons": ["MULTI_PARENT_HISTORY"], "reviewer_reason": reviewer_reason}
    return ownership_proposal(
        site,
        account,
        ctx,
        candidates,
        evidence,
        address_fuzzy=False,
        extra_reasons=["REVIEWER_SELECTED_CANDIDATE"],
        extra_risks=[f"Reviewer selected {account.name!r} ({account.id}) among several same-address accounts. Reason: {reviewer_reason}"],
    )


# --- duplicates -------------------------------------------------------------------------


def _survivor_score(site: SiteFacility, acc: CRMAccount, cand: Candidate | None) -> int:
    score = 0
    if cand and cand.phone_match:
        score += 3
    if cand and cand.admin_match:
        score += 3
    rev, ar = acc.lifetime_revenue or Decimal(0), acc.outstanding_ar or Decimal(0)
    if rev > 0 or ar > 0:
        score += 2
    if not acc.is_inactive:
        score += 1
    if norm_name(acc.name) == norm_name(site.name):
        score += 1
    if norm_text(acc.street) == norm_text(site.street):
        score += 1
    return score


def _choose_survivor(site: SiteFacility, group: list[CRMAccount], cand_by_id: dict[str, Candidate]) -> CRMAccount | None:
    scored = sorted(((_survivor_score(site, a, cand_by_id.get(a.id)), a) for a in group), key=lambda t: (-t[0], t[1].id))
    if len(scored) >= 2 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


def _duplicate_proposals(site: SiteFacility, group: list[CRMAccount], ctx: RuleContext, cands: list[Candidate], evidence: dict[str, Any]) -> list[Proposal]:
    cand_by_id = {c.account_id: c for c in cands}
    survivor = _choose_survivor(site, group, cand_by_id)
    ids = [a.id for a in group]
    scores = {a.id: _survivor_score(site, a, cand_by_id.get(a.id)) for a in group}
    ev = {**evidence, "crm_accounts": [_acc_summary(ctx, a) for a in group], "survivor_scores": scores}
    if survivor is None:
        return [
            Proposal(
                action_type=ACTION_MANUAL_REVIEW,
                tier=TIER_AMBIGUOUS,
                reason_codes=["DUPLICATE_SURVIVOR_TIE"],
                title=f"{site.name}: {len(group)} Bellhaven accounts at this address; survivor not decidable",
                site=site.to_dict(),
                crm_account_ids=ids,
                before={a.id: a.snapshot() for a in group},
                after={},
                changes=[],
                steps=[],
                candidates=cands,
                evidence=ev,
                risk_notes=["Phone/administrator/billing evidence does not single out one record."],
                target_parent_id=ctx.bellhaven_parent_id,
            )
        ]
    out: list[Proposal] = []
    for loser in group:
        if loser.id == survivor.id:
            continue
        risks = [
            f"Survivor {survivor.name!r} ({survivor.id}) chosen on evidence score {scores[survivor.id]} vs {scores[loser.id]}: "
            + ", ".join(cand_by_id[survivor.id].reasons if survivor.id in cand_by_id else []),
            "Loser is marked Inactive and linked via duplicate_of_account; nothing is deleted or merged.",
        ]
        lrev, lar = loser.lifetime_revenue or Decimal(0), loser.outstanding_ar or Decimal(0)
        if lrev > 0 or lar > 0:
            out.append(
                Proposal(
                    action_type=ACTION_MANUAL_REVIEW,
                    tier=TIER_AMBIGUOUS,
                    reason_codes=["DUPLICATE_LOSER_HAS_BILLING"],
                    title=f"{site.name}: apparent duplicate {loser.name!r} carries billing history",
                    site=site.to_dict(),
                    crm_account_ids=[loser.id, survivor.id],
                    before={a.id: a.snapshot() for a in group},
                    after={},
                    changes=[],
                    steps=[],
                    candidates=cands,
                    evidence=ev,
                    risk_notes=risks + [f"Loser has lifetime_revenue={lrev} outstanding_ar={lar}; preserve billing history, decide manually."],
                    target_parent_id=ctx.bellhaven_parent_id,
                )
            )
            continue
        note = append_note(
            loser.note,
            f"{config.NOTE_TAG} duplicate of {survivor.id} ({survivor.name}) at the same address; marked Inactive after reviewer approval",
        )
        changes = [
            FieldChange(loser.id, "status", loser.status, config.STATUS_INACTIVE),
            FieldChange(loser.id, "duplicate_of_account", loser.duplicate_of_account, survivor.id),
            FieldChange(loser.id, "note", loser.note, note),
        ]
        out.append(
            Proposal(
                action_type=ACTION_DUPLICATE,
                tier=TIER_HIGH_ADDRESS_EXACT,
                reason_codes=["DUPLICATE_SAME_ADDRESS_SAME_PARENT", "SURVIVOR_BY_EVIDENCE"],
                title=f"{site.name}: mark {loser.name!r} ({loser.id}) as duplicate of {survivor.id}",
                site=site.to_dict(),
                crm_account_ids=[loser.id, survivor.id],
                before={loser.id: loser.snapshot(), survivor.id: survivor.snapshot()},
                after={loser.id: _apply_changes(loser.snapshot(), changes)},
                changes=changes,
                steps=[_patch_step(loser.id, changes, 1, f"PATCH /accounts/{loser.id}: status, duplicate_of_account, note")],
                candidates=cands,
                evidence=ev,
                risk_notes=risks,
                target_parent_id=ctx.bellhaven_parent_id,
                requires_chow=False,
            )
        )
    return out


# --- Bellhaven children absent from the site --------------------------------------------


def _missing_from_site(ctx: RuleContext, matched_ids: set[str], site_facilities: list[SiteFacility]) -> list[Proposal]:
    out: list[Proposal] = []
    if not ctx.scrape_complete:
        return out
    addr_index: dict[tuple, list[CRMAccount]] = {}
    for a in ctx.accounts:
        if a.addr.is_usable:
            addr_index.setdefault(a.addr.strict, []).append(a)
    for acc in ctx.accounts:
        if (acc.parent_id or "") != ctx.bellhaven_parent_id or acc.id in matched_ids or acc.is_inactive:
            continue
        same_addr_others = [o for o in addr_index.get(acc.addr.strict, []) if o.id != acc.id]
        note_text = f"{config.NOTE_TAG} not listed on the Bellhaven website (complete directory + homepage scan); ownership/operating status needs review"
        already = acc.status == config.STATUS_NEEDS_REVIEW and config.NOTE_TAG in (acc.note or "")
        if already:
            continue  # applied earlier; resolved
        changes = [FieldChange(acc.id, "status", acc.status, config.STATUS_NEEDS_REVIEW)]
        if config.NOTE_TAG not in (acc.note or ""):
            changes.append(FieldChange(acc.id, "note", acc.note, append_note(acc.note, note_text)))
        risks = [
            "Absence from the website suggests the facility may have changed hands or closed, but does not prove a new owner. "
            "Default is 'Needs Review' + note; nothing is deleted, detached or inactivated.",
        ]
        rev, ar = acc.lifetime_revenue, acc.outstanding_ar
        if (rev or 0) > 0 or (ar or 0) > 0:
            risks.append(f"Billing history present (lifetime_revenue={rev}, outstanding_ar={ar}); preserve it.")
        if same_addr_others:
            risks.append(
                "Other CRM account(s) at this address: "
                + "; ".join(f"{o.name!r} (parent {ctx.parent_name(o.parent_id)}, status {o.status})" for o in same_addr_others)
                + " -- possible change of operator."
            )
        if config.STATUS_NEEDS_REVIEW not in ctx.observed_statuses:
            risks.append(f"Status value {config.STATUS_NEEDS_REVIEW!r} has not been observed on any existing account; the API may reject it (visible as a failed apply).")
        out.append(
            Proposal(
                action_type=ACTION_MISSING_FROM_SITE,
                tier=TIER_NO_CANDIDATE,
                reason_codes=["BELLHAVEN_CHILD_NOT_ON_SITE"] + (["OTHER_OPERATOR_AT_ADDRESS"] if same_addr_others else []),
                title=f"{acc.name}: Bellhaven child not on website -> status 'Needs Review'",
                site=None,
                crm_account_ids=[acc.id],
                before={acc.id: acc.snapshot()},
                after={acc.id: _apply_changes(acc.snapshot(), changes)},
                changes=changes,
                steps=[_patch_step(acc.id, changes, 1, f"PATCH /accounts/{acc.id}: status, note")],
                candidates=[],
                evidence={
                    "crm_accounts": [_acc_summary(ctx, acc)],
                    "same_address_accounts": [_acc_summary(ctx, o) for o in same_addr_others],
                    "site_scan": {"complete": True, "facility_count": ctx.site_count},
                },
                risk_notes=risks,
                target_parent_id=ctx.bellhaven_parent_id,
                requires_chow=False,
            )
        )
    return out


# --- entry point ------------------------------------------------------------------------


def generate_proposals(results: list[MatchResult], ctx: RuleContext) -> list[Proposal]:
    proposals: list[Proposal] = []
    matched_ids: set[str] = set()
    for mr in results:
        ps = _build_for_site(mr, ctx)
        proposals.extend(ps)
        for p in ps:
            if p.action_type == ACTION_MANUAL_REVIEW:
                # accounts already routed to a human are not re-proposed as "missing from site"
                matched_ids.update(p.crm_account_ids)
        for c in mr.eligible:
            matched_ids.add(c.account_id)
            # accounts linked to the matched ones are "accounted for" too
            acc = ctx.by_id.get(c.account_id)
            if acc and acc.chow_current_account:
                matched_ids.add(acc.chow_current_account)
            if acc and acc.duplicate_of_account:
                matched_ids.add(acc.duplicate_of_account)
    proposals.extend(_missing_from_site(ctx, matched_ids, [r.site for r in results]))
    # stable ordering: by action then title
    proposals.sort(key=lambda p: (p.action_type, p.title))
    return proposals
