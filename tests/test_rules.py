"""Mandatory tests 4-6, 8: CHOW rule, direct re-parent, duplicates, unsafe financials, missing-from-site."""
from __future__ import annotations

import json

import pytest

from bellhaven import config
from bellhaven.matching import match_all
from bellhaven.models import (
    ACTION_CHOW_SUCCESSOR,
    ACTION_CREATE_MISSING,
    ACTION_DUPLICATE,
    ACTION_MANUAL_REVIEW,
    ACTION_MISSING_FROM_SITE,
    ACTION_NO_CHANGE,
    ACTION_REPARENT,
    ACTION_UPDATE_FIELDS,
    CRMAccount,
)
from bellhaven.rules import RuleContext, UnsafeFinancials, generate_proposals, requires_chow
from bellhaven.scraper import SiteScraper

from .conftest import BASE, BELL


def _accounts(crm_fixture):
    accs = [CRMAccount.from_api(a) for a in crm_fixture["accounts"]]
    by_id = {a.id: a for a in accs}
    for c in crm_fixture["contacts"]:
        by_id[c["account_id"]].contacts.append(c)
    return accs


@pytest.fixture
def proposals(crm_fixture, fixture_scraper: SiteScraper):
    accs = _accounts(crm_fixture)
    scrape = fixture_scraper.scrape(expected_count=35)
    assert scrape.complete
    results = match_all(scrape.facilities, accs)
    ctx = RuleContext(bellhaven_parent_id=BELL, bellhaven_parent_name="Bellhaven Senior Living", accounts=accs, scrape_complete=True, site_count=scrape.count)
    return generate_proposals(results, ctx)


def by_action(props, action):
    return [p for p in props if p.action_type == action]


def find(props, action, account_id):
    return [p for p in props if p.action_type == action and account_id in p.crm_account_ids]


# --- the CHOW rule itself ---------------------------------------------------------------


def test_requires_chow_rule_table(crm_fixture):
    accs = {a.id: a for a in _accounts(crm_fixture)}
    assert requires_chow(accs["001A34WFSUYHCRBLFT"], BELL) is True  # Marietta 51,250 / 3,800
    assert requires_chow(accs["001U6RW32TY0WSXZZB"], BELL) is True  # Tiffin 84,000 / 12,400 (strings)
    assert requires_chow(accs["001UKEFGADQ8YCZ4YM"], BELL) is False  # Findlay 22,000 / 0
    assert requires_chow(accs["001LGFPBJY4N9MB6KL"], BELL) is False  # Lima 47,000 / 0
    assert requires_chow(accs["001WR41PYNWXCAE2X4"], BELL) is True  # Kettering Care Centre 15,000 / 900
    assert requires_chow(accs["001ZANESVILLE00001"], BELL) is False  # Zanesville 0 / 0
    assert requires_chow(accs["001CHESTERTON00001"], BELL) is False  # already under Bellhaven -> never CHOW
    with pytest.raises(UnsafeFinancials):
        requires_chow(accs["001BADMONEY0000001"], BELL)


# --- Marietta / Tiffin: CHOW successor --------------------------------------------------


@pytest.mark.parametrize("old_id,rev,ar", [("001A34WFSUYHCRBLFT", "51250", "3800"), ("001U6RW32TY0WSXZZB", "84000.00", "12400.00")])
def test_chow_cases_preserve_old_and_create_successor(proposals, old_id, rev, ar):
    ps = find(proposals, ACTION_CHOW_SUCCESSOR, old_id)
    assert len(ps) == 1
    p = ps[0]
    assert p.requires_chow is True
    assert p.before[old_id]["lifetime_revenue"] == rev and p.before[old_id]["outstanding_ar"] == ar
    # old account: ONLY chow_current_account changes
    old_changes = [c for c in p.changes if c.account_id == old_id]
    assert [c.field for c in old_changes] == ["chow_current_account"]
    assert p.after[old_id]["parent_id"] == p.before[old_id]["parent_id"] != BELL
    assert p.after[old_id]["status"] == p.before[old_id]["status"]
    assert p.after[old_id]["lifetime_revenue"] == rev
    # successor payload
    new = p.after["NEW"]
    assert new["parent_id"] == BELL and new["status"] == config.STATUS_ACTIVE
    assert new["name"].startswith("Bellhaven of ")
    assert f"chow-successor-of:{old_id}" in new["note"]
    assert [s.kind for s in p.steps] == ["create", "patch"]
    assert p.steps[1].payload == {"chow_current_account": "<successor id>"}
    assert not find(proposals, ACTION_REPARENT, old_id)
    assert not find(proposals, ACTION_DUPLICATE, old_id)


# --- Findlay / Lima / Kettering / Zanesville: direct re-parent -----------------------------


@pytest.mark.parametrize("old_id", ["001UKEFGADQ8YCZ4YM", "001LGFPBJY4N9MB6KL", "001ZANESVILLE00001"])
def test_direct_reparent_without_successor(proposals, old_id):
    ps = find(proposals, ACTION_REPARENT, old_id)
    assert len(ps) == 1
    p = ps[0]
    assert p.requires_chow is False
    assert p.after[old_id]["parent_id"] == BELL
    assert "NEW" not in p.after
    assert all(s.kind == "patch" for s in p.steps) and len(p.steps) == 1
    assert p.steps[0].payload["parent_id"] == BELL
    assert not find(proposals, ACTION_CHOW_SUCCESSOR, old_id)
    assert not any(p.action_type == ACTION_CREATE_MISSING and (p.site or {}).get("name") == p.title.split(":")[0] for p in find(proposals, ACTION_CREATE_MISSING, old_id))


def test_zanesville_bundles_rename_with_reparent(proposals):
    z = find(proposals, ACTION_REPARENT, "001ZANESVILLE00001")[0]
    assert {c.field for c in z.changes} == {"parent_id", "name"}
    assert z.after["001ZANESVILLE00001"]["name"] == "Bellhaven of Zanesville"


def test_kettering_multiple_same_address_accounts_is_manual_review(proposals):
    """Live sandbox: several non-Bellhaven accounts at 3313 Wilmington Pike/Pk with no corroboration -> no confident write."""
    ps = find(proposals, ACTION_MANUAL_REVIEW, "001WR41PYNWXCAE2X4")
    assert len(ps) == 1 and "MULTI_PARENT_HISTORY" in ps[0].reason_codes
    assert set(ps[0].crm_account_ids) == {"001WR41PYNWXCAE2X4", "001KETTERINGCAMP01"}  # Pike == Pk
    assert not find(proposals, ACTION_REPARENT, "001WR41PYNWXCAE2X4")
    assert not find(proposals, ACTION_CHOW_SUCCESSOR, "001WR41PYNWXCAE2X4")


def test_reviewer_selected_candidate_gets_guarded_ownership_proposal(crm_fixture, fixture_scraper):
    from bellhaven.rules import reviewer_selected_proposal

    accs = _accounts(crm_fixture)
    ctx = RuleContext(bellhaven_parent_id=BELL, bellhaven_parent_name="Bellhaven Senior Living", accounts=accs, scrape_complete=True, site_count=35)
    scrape = fixture_scraper.scrape(expected_count=35)
    site = next(f for f in scrape.facilities if "kettering" in f.slug)
    results = match_all([site], accs)
    by_id = {a.id: a for a in accs}
    # Care Centre carries revenue AND AR -> the reviewer's pick must still obey the CHOW rule
    p = reviewer_selected_proposal(site, by_id["001WR41PYNWXCAE2X4"], ctx, results[0].candidates, "ops confirmed")
    assert p.action_type == ACTION_CHOW_SUCCESSOR and "REVIEWER_SELECTED_CANDIDATE" in p.reason_codes
    p2 = reviewer_selected_proposal(site, by_id["001KETTERINGCAMP01"], ctx, results[0].candidates, "ops confirmed")
    assert p2.action_type == ACTION_REPARENT and p2.after["001KETTERINGCAMP01"]["name"] == "Bellhaven of Kettering"


def test_ashtabula_po_box_is_corroborated_address_correction(proposals):
    ps = find(proposals, ACTION_UPDATE_FIELDS, "001ASHTABULAPOBOX1")
    assert len(ps) == 1 and ps[0].tier == "MEDIUM_ADDRESS_FUZZY"
    assert {c.field for c in ps[0].changes} == {"street"}
    assert ps[0].after["001ASHTABULAPOBOX1"]["street"] == "3156 W Prospect Rd"
    assert any("CRM_ADDRESS_NOT_A_STREET" in c.reasons and c.phone_match for c in ps[0].candidates)
    assert not find(proposals, ACTION_MANUAL_REVIEW, "001ASHTABULAPOBOX1")
    assert not find(proposals, ACTION_MISSING_FROM_SITE, "001ASHTABULAPOBOX1")


def test_findlay_homepage_only_is_linked_not_created(proposals):
    assert find(proposals, ACTION_REPARENT, "001UKEFGADQ8YCZ4YM")
    assert not [p for p in by_action(proposals, ACTION_CREATE_MISSING) if "Findlay" in p.title]


# --- renames ------------------------------------------------------------------------------


@pytest.mark.parametrize("acc_id,new_name", [("001CHESTERTON00001", "Bellhaven of Chesterton"), ("001CHAGRINFALLS001", "Bellhaven of Chagrin Falls"), ("001PORTAGE00000001", "Bellhaven Willow Creek")])
def test_renames_are_updates_not_creates(proposals, acc_id, new_name):
    ps = find(proposals, ACTION_UPDATE_FIELDS, acc_id)
    assert len(ps) == 1
    assert [c.field for c in ps[0].changes] == ["name"]
    assert ps[0].after[acc_id]["name"] == new_name
    assert not [p for p in by_action(proposals, ACTION_CREATE_MISSING) if new_name in p.title]


# --- Owosso duplicate ---------------------------------------------------------------------


def test_owosso_survivor_and_loser(proposals):
    dups = [p for p in by_action(proposals, ACTION_DUPLICATE) if "Owosso" in p.title]
    assert len(dups) == 1
    p = dups[0]
    loser, survivor = "001QU150PM4Z15UA71", "001EGU7BMJ942ZTRE6"
    assert p.crm_account_ids == [loser, survivor]
    assert p.after[loser]["status"] == config.STATUS_INACTIVE
    assert p.after[loser]["duplicate_of_account"] == survivor
    assert config.NOTE_TAG in p.after[loser]["note"]
    assert survivor not in p.after  # survivor untouched
    assert p.steps[0].account_id == loser
    assert set(p.steps[0].payload) == {"status", "duplicate_of_account", "note"}
    # CHOW history is not confused with duplicates
    assert not find(proposals, ACTION_DUPLICATE, "001A34WFSUYHCRBLFT")
    assert not find(proposals, ACTION_DUPLICATE, "001ERIEHARBOR00001")


def test_cross_parent_history_is_manual_review_not_duplicate(proposals):
    ps = find(proposals, ACTION_MANUAL_REVIEW, "001ERIEHARBOR00001")
    assert len(ps) == 1 and "CROSS_PARENT_SAME_ADDRESS_HISTORY" in ps[0].reason_codes
    assert ps[0].steps == []


# --- negative controls -----------------------------------------------------------------------


def test_amberly_manor_creates_new_and_shows_colorado_as_rejected(proposals):
    ps = [p for p in by_action(proposals, ACTION_CREATE_MISSING) if p.title.startswith("Amberly Manor")]
    assert len(ps) == 1
    p = ps[0]
    assert p.after["NEW"]["state"] == "OH" and p.after["NEW"]["city"] == "Hudson"
    assert any("different state" in r for r in p.risk_notes)
    assert any(c.account_id == "001AMBERLYCO000001" and not c.eligible for c in p.candidates)
    assert not find(proposals, ACTION_UPDATE_FIELDS, "001AMBERLYCO000001")
    assert not find(proposals, ACTION_REPARENT, "001AMBERLYCO000001")


def test_union_square_is_manual_review_only(proposals):
    ps = find(proposals, ACTION_MANUAL_REVIEW, "001UNIONSQUARE0001")
    assert len(ps) == 1 and "NAME_MATCH_ADDRESS_CONFLICT" in ps[0].reason_codes
    assert ps[0].steps == []
    assert not find(proposals, ACTION_UPDATE_FIELDS, "001UNIONSQUARE0001")
    assert not find(proposals, ACTION_MISSING_FROM_SITE, "001UNIONSQUARE0001")
    assert not [p for p in by_action(proposals, ACTION_CREATE_MISSING) if "Union Square" in p.title]


def test_unparseable_financials_block_ownership_write(proposals):
    ps = find(proposals, ACTION_MANUAL_REVIEW, "001BADMONEY0000001")
    assert len(ps) == 1 and "FINANCIALS_UNPARSEABLE" in ps[0].reason_codes
    assert not find(proposals, ACTION_REPARENT, "001BADMONEY0000001")
    assert not find(proposals, ACTION_CHOW_SUCCESSOR, "001BADMONEY0000001")


# --- missing from site ---------------------------------------------------------------------------


def test_missing_from_site_is_needs_review_with_billing_preserved(proposals):
    ids = {p.crm_account_ids[0] for p in by_action(proposals, ACTION_MISSING_FROM_SITE)}
    assert ids == {"001ALLIANCE0000001", "001COLDWATER000001", "001SANDUSKY0000001"}
    s = find(proposals, ACTION_MISSING_FROM_SITE, "001SANDUSKY0000001")[0]
    assert s.after["001SANDUSKY0000001"]["status"] == config.STATUS_NEEDS_REVIEW
    assert s.after["001SANDUSKY0000001"]["parent_id"] == BELL  # not detached
    assert s.after["001SANDUSKY0000001"]["lifetime_revenue"] == "130000"
    assert set(s.steps[0].payload) == {"status", "note"}
    assert "OTHER_OPERATOR_AT_ADDRESS" in s.reason_codes
    assert any("Millstone Care of Sandusky" in r for r in s.risk_notes)


def test_incomplete_scrape_disables_missing_from_site(crm_fixture, fixture_scraper):
    accs = _accounts(crm_fixture)
    scrape = fixture_scraper.scrape(expected_count=35)
    results = match_all(scrape.facilities, accs)
    ctx = RuleContext(bellhaven_parent_id=BELL, bellhaven_parent_name="Bellhaven Senior Living", accounts=accs, scrape_complete=False, site_count=scrape.count)
    props = generate_proposals(results, ctx)
    assert by_action(props, ACTION_MISSING_FROM_SITE) == []
    assert by_action(props, ACTION_CHOW_SUCCESSOR)  # other proposals still produced


# --- idempotency of keys / already-applied recognition ------------------------------------------


def test_proposal_keys_are_stable_and_ignore_note_text(proposals):
    p = find(proposals, ACTION_MISSING_FROM_SITE, "001SANDUSKY0000001")[0]
    d = p.to_dict()
    d["changes"] = [dict(c, after=c["after"] + " extra") if c["field"] == "note" else c for c in d["changes"]]
    from bellhaven.models import Proposal

    assert Proposal.from_dict({**d, "proposal_key": ""}).proposal_key == p.proposal_key
    d2 = json.loads(json.dumps(p.to_dict()))
    assert Proposal.from_dict(d2).compute_key() == p.proposal_key


def test_applied_chow_is_recognized_as_resolved(crm_fixture, fixture_scraper):
    """After a CHOW: old record stays under Cedar Trail with chow link; successor under Bellhaven -> no new proposal."""
    raw = json.loads(json.dumps(crm_fixture["accounts"]))
    old = next(a for a in raw if a["account_id"] == "001A34WFSUYHCRBLFT")
    old["chow_current_account"] = "001NEWMARIETTA0001"
    raw.append({**old, "account_id": "001NEWMARIETTA0001", "parent_id": BELL, "lifetime_revenue": 0, "outstanding_ar": 0, "chow_current_account": None, "note": "[bellhaven-reconciliation] chow-successor-of:001A34WFSUYHCRBLFT"})
    accs = [CRMAccount.from_api(a) for a in raw]
    scrape = fixture_scraper.scrape(expected_count=35)
    results = match_all(scrape.facilities, accs)
    ctx = RuleContext(bellhaven_parent_id=BELL, bellhaven_parent_name="Bellhaven Senior Living", accounts=accs, scrape_complete=True, site_count=scrape.count)
    props = generate_proposals(results, ctx)
    assert not find(props, ACTION_CHOW_SUCCESSOR, "001A34WFSUYHCRBLFT")
    assert not find(props, ACTION_DUPLICATE, "001A34WFSUYHCRBLFT")
    assert not find(props, ACTION_MANUAL_REVIEW, "001A34WFSUYHCRBLFT")
    assert find(props, ACTION_NO_CHANGE, "001NEWMARIETTA0001")
    assert not find(props, ACTION_MISSING_FROM_SITE, "001NEWMARIETTA0001")


def test_applied_duplicate_is_recognized_as_resolved(crm_fixture, fixture_scraper):
    raw = json.loads(json.dumps(crm_fixture["accounts"]))
    loser = next(a for a in raw if a["account_id"] == "001QU150PM4Z15UA71")
    loser["status"] = config.STATUS_INACTIVE
    loser["duplicate_of_account"] = "001EGU7BMJ942ZTRE6"
    accs = [CRMAccount.from_api(a) for a in raw]
    scrape = fixture_scraper.scrape(expected_count=35)
    results = match_all(scrape.facilities, accs)
    ctx = RuleContext(bellhaven_parent_id=BELL, bellhaven_parent_name="Bellhaven Senior Living", accounts=accs, scrape_complete=True, site_count=scrape.count)
    props = generate_proposals(results, ctx)
    assert not by_action([p for p in props if "Owosso" in p.title], ACTION_DUPLICATE)
    assert find(props, ACTION_NO_CHANGE, "001EGU7BMJ942ZTRE6")
