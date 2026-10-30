"""Mandatory tests 7-9, 11: write guard, preflight refusal, CHOW recovery, minimal verified patches."""
from __future__ import annotations

import json

import pytest

from bellhaven import config
from bellhaven.apply import Applier, ApplyError, apply_proposal
from bellhaven.crm_api import WritesDisabledError
from bellhaven.models import (
    ACTION_CHOW_SUCCESSOR,
    ACTION_DUPLICATE,
    ACTION_MISSING_FROM_SITE,
    ACTION_REPARENT,
    ACTION_UPDATE_FIELDS,
    APPLY_APPLIED,
    APPLY_BLOCKED,
    APPLY_FAILED,
    APPLY_NEEDS_RECOVERY,
    DECISION_APPROVED,
    DECISION_PENDING,
    DECISION_REJECTED,
)
from bellhaven.pipeline import run_scan
from bellhaven.scraper import SiteScraper
from bellhaven.store import Store

from .conftest import BASE, BELL, make_fixture_fetch
from .fake_crm import CrashSimulated, FakeCRMClient


def scan(settings, store, client):
    return run_scan(settings, store, client, SiteScraper(BASE, fetch=make_fixture_fetch()))


def key_for(store: Store, action: str, account_id: str) -> str:
    for r in store.list_proposals(action_types=[action]):
        if account_id in json.loads(r["crm_account_ids_json"]):
            return r["proposal_key"]
    raise AssertionError(f"no {action} proposal for {account_id}")


def writable(client: FakeCRMClient) -> FakeCRMClient:
    w = FakeCRMClient([], allow_writes=True)
    w.accounts = client.accounts  # share state with the read-only client
    w.contacts = client.contacts
    return w


# --- 7. no writes on scan / reject / verify ----------------------------------------------------------


def test_scan_and_reject_perform_zero_writes(settings, store, fake_client):
    rep = scan(settings, store, fake_client)
    assert rep.crm_writes == 0 and fake_client.write_log == []
    key = key_for(store, ACTION_REPARENT, "001LGFPBJY4N9MB6KL")
    store.reject(key, "not convinced")
    row = store.get_proposal_row(key)
    assert row["decision"] == DECISION_REJECTED and fake_client.write_log == []
    scan(settings, store, fake_client)
    assert fake_client.write_log == []
    assert store.get_proposal_row(key)["decision"] == DECISION_REJECTED


def test_read_only_client_refuses_writes(fake_client):
    with pytest.raises(WritesDisabledError):
        fake_client.update_account("001LGFPBJY4N9MB6KL", {config.FIELD_PARENT: BELL})
    with pytest.raises(WritesDisabledError):
        fake_client.create_account({config.FIELD_NAME: "x"})
    assert fake_client.successful_writes == 0


def test_applier_rejects_read_only_client(store, fake_client):
    with pytest.raises(ApplyError):
        Applier(store, fake_client, BELL)


# --- 6. direct re-parent applies a minimal patch and verifies ------------------------------------------


def test_reparent_lima_minimal_patch_and_readback(settings, store, fake_client):
    scan(settings, store, fake_client)
    key = key_for(store, ACTION_REPARENT, "001LGFPBJY4N9MB6KL")
    w = writable(fake_client)
    res = apply_proposal(store, w, BELL, key, "looks right")
    assert res.ok, res.message
    assert len(w.write_log) == 1 and w.write_log[0].method == "PATCH"
    assert w.write_log[0].payload == {config.FIELD_PARENT: BELL}
    acc = w.accounts["001LGFPBJY4N9MB6KL"]
    assert acc[config.FIELD_PARENT] == BELL and acc[config.FIELD_REVENUE] == 47000  # no successor, no financial change
    assert len(w.accounts) == 28  # nothing created
    row = store.get_proposal_row(key)
    assert row["decision"] == DECISION_APPROVED and row["apply_status"] == APPLY_APPLIED
    assert [s["step_name"] for s in store.steps(key)] == ["patch", "verify"]


def test_zanesville_bundled_rename_and_reparent(settings, store, fake_client):
    scan(settings, store, fake_client)
    key = key_for(store, ACTION_REPARENT, "001ZANESVILLE00001")
    w = writable(fake_client)
    res = apply_proposal(store, w, BELL, key)
    assert res.ok
    assert w.write_log[0].payload == {config.FIELD_PARENT: BELL, config.FIELD_NAME: "Bellhaven of Zanesville"}


def test_reviewer_created_proposal_survives_rescan_and_applies(settings, store, fake_client):
    from bellhaven.matching import match_all
    from bellhaven.models import ACTION_MANUAL_REVIEW, CRMAccount, SiteFacility
    from bellhaven.rules import RuleContext, reviewer_selected_proposal

    rep = scan(settings, store, fake_client)
    mr_key = key_for(store, ACTION_MANUAL_REVIEW, "001KETTERINGCAMP01")
    accounts = []
    for d in store.crm_snapshot(rep.run_id):
        a = CRMAccount.from_api(d["raw"])
        a.contacts = d.get("contacts", [])
        accounts.append(a)
    ctx = RuleContext(BELL, "Bellhaven Senior Living", accounts, True, 35)
    site = SiteFacility.from_dict(store.get_proposal(mr_key).site)
    cands = match_all([site], accounts)[0].candidates
    newp = reviewer_selected_proposal(site, next(a for a in accounts if a.id == "001KETTERINGCAMP01"), ctx, cands, "ops confirmed campus record")
    assert store.add_reviewer_proposal(rep.run_id, newp, "ops confirmed") is True
    assert store.add_reviewer_proposal(rep.run_id, newp, "again") is False
    scan(settings, store, fake_client)  # rescan must not stale-out the reviewer's proposal
    row = store.get_proposal_row(newp.proposal_key)
    assert row["stale"] == 0 and row["origin"] == "reviewer" and row["decision"] == "pending"
    w = writable(fake_client)
    res = apply_proposal(store, w, BELL, newp.proposal_key, "approved")
    assert res.ok, res.message
    assert w.accounts["001KETTERINGCAMP01"][config.FIELD_PARENT] == BELL
    assert w.accounts["001KETTERINGCAMP01"][config.FIELD_NAME] == "Bellhaven of Kettering"
    scan(settings, store, fake_client)
    assert store.get_proposal_row(newp.proposal_key)["apply_status"] == APPLY_APPLIED
    pending = [r for r in store.list_proposals(decisions=["pending"]) if "Kettering" in r["title"]]
    assert not [r for r in pending if r["action_type"] in ("REPARENT", "CHOW_SUCCESSOR")]  # ownership resolved; no re-proposal


# --- 5. CHOW: successor created, old preserved, link set -----------------------------------------------


@pytest.mark.parametrize("old_id", ["001A34WFSUYHCRBLFT", "001U6RW32TY0WSXZZB"])
def test_chow_apply_creates_successor_and_preserves_old(settings, store, fake_client, old_id):
    scan(settings, store, fake_client)
    before = dict(fake_client.accounts[old_id])
    key = key_for(store, ACTION_CHOW_SUCCESSOR, old_id)
    w = writable(fake_client)
    res = apply_proposal(store, w, BELL, key)
    assert res.ok, res.message
    assert [x.method for x in w.write_log] == ["POST", "PATCH"]
    new_id = res.created_id
    assert new_id and new_id in w.accounts
    new = w.accounts[new_id]
    assert new[config.FIELD_PARENT] == BELL and new[config.FIELD_STATUS] == "Active"
    old = w.accounts[old_id]
    assert old[config.FIELD_CHOW] == new_id
    for f in (config.FIELD_PARENT, config.FIELD_NAME, config.FIELD_STATUS, config.FIELD_REVENUE, config.FIELD_AR, config.FIELD_NOTE):
        assert old[f] == before[f], f
    assert w.write_log[1].payload == {config.FIELD_CHOW: new_id}
    assert old[config.FIELD_DUP] is None


# --- 8. preflight refusal when CRM changed ---------------------------------------------------------------


def test_preflight_blocks_when_financials_changed(settings, store, fake_client):
    scan(settings, store, fake_client)
    key = key_for(store, ACTION_REPARENT, "001LGFPBJY4N9MB6KL")
    fake_client.accounts["001LGFPBJY4N9MB6KL"][config.FIELD_AR] = 250  # AR appeared after the scan -> CHOW now required
    w = writable(fake_client)
    res = apply_proposal(store, w, BELL, key)
    assert res.status == APPLY_BLOCKED and "PRECONDITION_CHANGED" in res.message
    assert w.successful_writes == 0
    row = store.get_proposal_row(key)
    assert row["decision"] == DECISION_PENDING and row["apply_status"] == APPLY_BLOCKED


def test_preflight_blocks_when_parent_changed_elsewhere(settings, store, fake_client):
    scan(settings, store, fake_client)
    key = key_for(store, ACTION_CHOW_SUCCESSOR, "001A34WFSUYHCRBLFT")
    fake_client.accounts["001A34WFSUYHCRBLFT"][config.FIELD_PARENT] = "001MILLSTONE000001"
    w = writable(fake_client)
    res = apply_proposal(store, w, BELL, key)
    assert res.status == APPLY_BLOCKED and w.successful_writes == 0


def test_chow_recheck_uses_fresh_financials(settings, store, fake_client):
    """Proposal says REPARENT; data unchanged on guarded fields except AR flips via a non-guarded path is impossible,
    so simulate by tampering the stored before-snapshot: fresh data must win."""
    scan(settings, store, fake_client)
    key = key_for(store, ACTION_REPARENT, "001UKEFGADQ8YCZ4YM")
    p = store.get_proposal(key)
    # tamper: pretend the proposal was generated when AR was 999 (so preflight passes if CRM also says 999)
    fake_client.accounts["001UKEFGADQ8YCZ4YM"][config.FIELD_AR] = 999
    p.before["001UKEFGADQ8YCZ4YM"]["outstanding_ar"] = "999"
    store._conn.execute("UPDATE proposals SET data_json=? WHERE proposal_key=?", (json.dumps(p.to_dict()), key))
    w = writable(fake_client)
    res = apply_proposal(store, w, BELL, key)
    assert res.status == APPLY_BLOCKED and "CHOW_RULE_CHANGED" in res.message
    assert w.successful_writes == 0


def test_unparseable_financials_block_at_apply_time(settings, store, fake_client):
    scan(settings, store, fake_client)
    key = key_for(store, ACTION_REPARENT, "001LGFPBJY4N9MB6KL")
    p = store.get_proposal(key)
    fake_client.accounts["001LGFPBJY4N9MB6KL"][config.FIELD_REVENUE] = "unknown"
    p.before["001LGFPBJY4N9MB6KL"]["lifetime_revenue"] = None
    store._conn.execute("UPDATE proposals SET data_json=? WHERE proposal_key=?", (json.dumps(p.to_dict()), key))
    w = writable(fake_client)
    res = apply_proposal(store, w, BELL, key)
    assert res.status == APPLY_BLOCKED and "FINANCIALS_UNSAFE" in res.message and w.successful_writes == 0


# --- 9. double approval / timeout after POST / crash / restart recovery ------------------------------------


def test_double_approval_is_rejected_second_time(settings, store, fake_client):
    scan(settings, store, fake_client)
    key = key_for(store, ACTION_REPARENT, "001LGFPBJY4N9MB6KL")
    w = writable(fake_client)
    assert apply_proposal(store, w, BELL, key).ok
    second = apply_proposal(store, w, BELL, key)
    assert second.status == APPLY_BLOCKED and "not claimable" in second.message
    assert w.successful_writes == 1


def test_concurrent_claim_blocks_second_tab(settings, store, fake_client):
    scan(settings, store, fake_client)
    key = key_for(store, ACTION_REPARENT, "001LGFPBJY4N9MB6KL")
    store.claim_for_apply(key, "tab 1")
    w = writable(fake_client)
    res = apply_proposal(store, w, BELL, key, "tab 2")
    assert res.status == APPLY_BLOCKED and w.successful_writes == 0


def test_timeout_after_successor_post_does_not_create_second_successor(settings, store, fake_client):
    scan(settings, store, fake_client)
    key = key_for(store, ACTION_CHOW_SUCCESSOR, "001A34WFSUYHCRBLFT")
    w = writable(fake_client)
    w.timeout_after_create = True
    res = apply_proposal(store, w, BELL, key)
    # the applier found the created successor by its note marker and finished the CHOW
    assert res.ok, res.message
    successors = [a for a in w.accounts.values() if "chow-successor-of:001A34WFSUYHCRBLFT" in (a.get(config.FIELD_NOTE) or "")]
    assert len(successors) == 1
    assert w.accounts["001A34WFSUYHCRBLFT"][config.FIELD_CHOW] == successors[0][config.FIELD_ID]
    assert [s["status"] for s in store.steps(key) if s["step_name"] == "create"] == ["done_after_uncertain"]


def test_timeout_before_post_then_retry_creates_exactly_one(settings, store, fake_client):
    scan(settings, store, fake_client)
    key = key_for(store, ACTION_CHOW_SUCCESSOR, "001U6RW32TY0WSXZZB")
    w = writable(fake_client)
    w.timeout_before_create = True
    res = apply_proposal(store, w, BELL, key)
    assert res.status == APPLY_NEEDS_RECOVERY
    assert store.get_proposal_row(key)["apply_status"] == APPLY_NEEDS_RECOVERY
    res2 = apply_proposal(store, w, BELL, key)  # reviewer clicks again -> reconcile, then create once
    assert res2.ok, res2.message
    successors = [a for a in w.accounts.values() if "chow-successor-of:001U6RW32TY0WSXZZB" in (a.get(config.FIELD_NOTE) or "")]
    assert len(successors) == 1


def test_crash_after_create_then_restart_recovers_without_second_successor(settings, store, fake_client, tmp_path):
    scan(settings, store, fake_client)
    key = key_for(store, ACTION_CHOW_SUCCESSOR, "001A34WFSUYHCRBLFT")
    w = writable(fake_client)
    w.crash_after_create = True
    with pytest.raises(CrashSimulated):
        apply_proposal(store, w, BELL, key)
    assert store.get_proposal_row(key)["apply_status"] == "in_progress"  # claim left behind by the crash
    assert len([a for a in w.accounts.values() if "chow-successor-of:001A34WFSUYHCRBLFT" in (a.get(config.FIELD_NOTE) or "")]) == 1
    # "restart": new Store on the same file, new client sharing remote state
    store2 = Store(store.path)
    store2._conn.execute("UPDATE proposals SET apply_status='needs_recovery' WHERE proposal_key=? AND apply_status='in_progress'", (key,))
    w2 = writable(fake_client)
    res = apply_proposal(store2, w2, BELL, key)
    assert res.ok, res.message
    successors = [a for a in w2.accounts.values() if "chow-successor-of:001A34WFSUYHCRBLFT" in (a.get(config.FIELD_NOTE) or "")]
    assert len(successors) == 1
    assert [x.method for x in w2.write_log] == ["PATCH"]  # link only; no second POST
    assert w2.accounts["001A34WFSUYHCRBLFT"][config.FIELD_CHOW] == successors[0][config.FIELD_ID]
    store2.close()


def test_rescan_after_applied_chow_does_not_repropose(settings, store, fake_client):
    scan(settings, store, fake_client)
    key = key_for(store, ACTION_CHOW_SUCCESSOR, "001A34WFSUYHCRBLFT")
    w = writable(fake_client)
    assert apply_proposal(store, w, BELL, key).ok
    rep = scan(settings, store, fake_client)
    pending = [r for r in store.list_proposals(decisions=["pending"], action_types=[ACTION_CHOW_SUCCESSOR])]
    assert all("Marietta" not in r["title"] for r in pending)
    assert store.get_proposal_row(key)["apply_status"] == APPLY_APPLIED
    assert rep.crm_writes == 0


# --- 11. supported fields only, partial errors visible ---------------------------------------------------


def test_invalid_status_value_surfaces_as_failed_not_success(settings, store, fake_client):
    scan(settings, store, fake_client)
    key = key_for(store, ACTION_MISSING_FROM_SITE, "001ALLIANCE0000001")
    w = writable(fake_client)
    w.valid_statuses = {"Active", "Inactive"}
    res = apply_proposal(store, w, BELL, key)
    assert res.status == APPLY_FAILED and "422" in res.message
    assert store.get_proposal_row(key)["apply_status"] == APPLY_FAILED
    assert w.accounts["001ALLIANCE0000001"][config.FIELD_STATUS] == "Active"
    assert [s["status"] for s in store.steps(key)] == ["failed"]


def test_duplicate_apply_marks_loser_only(settings, store, fake_client):
    scan(settings, store, fake_client)
    key = key_for(store, ACTION_DUPLICATE, "001QU150PM4Z15UA71")
    w = writable(fake_client)
    res = apply_proposal(store, w, BELL, key)
    assert res.ok, res.message
    loser, survivor = w.accounts["001QU150PM4Z15UA71"], w.accounts["001EGU7BMJ942ZTRE6"]
    assert loser[config.FIELD_STATUS] == "Inactive" and loser[config.FIELD_DUP] == "001EGU7BMJ942ZTRE6"
    assert config.NOTE_TAG in loser[config.FIELD_NOTE]
    assert survivor[config.FIELD_STATUS] == "Active" and survivor[config.FIELD_DUP] is None
    assert len(w.write_log) == 1 and set(w.write_log[0].payload) == {config.FIELD_STATUS, config.FIELD_DUP, config.FIELD_NOTE}


def test_update_fields_patch_then_rescan_shows_no_change(settings, store, fake_client):
    scan(settings, store, fake_client)
    key = key_for(store, ACTION_UPDATE_FIELDS, "001CHESTERTON00001")
    w = writable(fake_client)
    assert apply_proposal(store, w, BELL, key).ok
    assert w.accounts["001CHESTERTON00001"][config.FIELD_NAME] == "Bellhaven of Chesterton"
    scan(settings, store, fake_client)
    assert not [r for r in store.list_proposals(decisions=["pending"], action_types=[ACTION_UPDATE_FIELDS]) if "Chesterton" in r["title"]]


def test_forbidden_fields_can_never_be_written(writable_client):
    from bellhaven.crm_api import CRMValidationError

    with pytest.raises(CRMValidationError):
        writable_client.update_account("001LGFPBJY4N9MB6KL", {config.FIELD_REVENUE: 0})
    with pytest.raises(CRMValidationError):
        writable_client.update_account("001LGFPBJY4N9MB6KL", {"made_up_field": 1})


def test_reviewer_create_for_name_conflict_creates_once_and_leaves_other_account(settings, store, fake_client):
    from bellhaven.matching import match_all
    from bellhaven.models import ACTION_CREATE_MISSING, ACTION_MANUAL_REVIEW, CRMAccount, SiteFacility
    from bellhaven.rules import RuleContext, reviewer_create_proposal

    rep = scan(settings, store, fake_client)
    mr_key = key_for(store, ACTION_MANUAL_REVIEW, "001UNIONSQUARE0001")
    accounts = []
    for d in store.crm_snapshot(rep.run_id):
        a = CRMAccount.from_api(d["raw"])
        a.contacts = d.get("contacts", [])
        accounts.append(a)
    ctx = RuleContext(BELL, "Bellhaven Senior Living", accounts, True, 35)
    site = SiteFacility.from_dict(store.get_proposal(mr_key).site)
    cands = match_all([site], accounts)[0].candidates
    newp = reviewer_create_proposal(site, ctx, cands, "different street, phone, administrator and operator")
    assert newp.action_type == ACTION_CREATE_MISSING and newp.crm_account_ids == []
    assert store.add_reviewer_proposal(rep.run_id, newp, "x") is True
    before_other = dict(fake_client.accounts["001UNIONSQUARE0001"])
    w = writable(fake_client)
    res = apply_proposal(store, w, BELL, newp.proposal_key, "approved")
    assert res.ok and res.created_id in w.accounts
    assert w.accounts[res.created_id][config.FIELD_STREET] == "118 Union Square Dr" and w.accounts[res.created_id][config.FIELD_PARENT] == BELL
    assert fake_client.accounts["001UNIONSQUARE0001"] == before_other  # untouched
    scan(settings, store, fake_client)
    assert store.get_proposal_row(newp.proposal_key)["apply_status"] == APPLY_APPLIED
    assert not [r for r in store.list_proposals(decisions=["pending"]) if "Union Square" in r["title"] and r["action_type"] == ACTION_CREATE_MISSING]
    # second apply attempt cannot create a second account
    assert not apply_proposal(store, w, BELL, newp.proposal_key).ok
    assert sum(1 for a in w.accounts.values() if a.get(config.FIELD_STREET) == "118 Union Square Dr") == 1
