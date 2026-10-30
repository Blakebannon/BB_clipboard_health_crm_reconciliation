"""Mandatory tests 2-4: address-first matching, negative controls, Owosso grouping."""
from __future__ import annotations

from bellhaven.matching import CRMIndex, find_candidates, match_all, name_similarity
from bellhaven.models import TIER_HIGH_ADDRESS_EXACT, TIER_LOW_NAME_ONLY, TIER_MEDIUM_ADDRESS_FUZZY, TIER_NO_CANDIDATE, CRMAccount, SiteFacility


def _site(name, street, city, state, zip_, phone="", admin="", slug=None):
    return SiteFacility(name=name, street=street, city=city, state=state, zip=zip_, care_offerings=["Assisted Living"], source_url=f"https://x/communities/{slug or name.lower().replace(' ', '-')}", scraped_at="t", phone=phone, administrator=admin, slug=f"/communities/{slug or name.lower().replace(' ', '-')}")


def _accounts(crm_fixture):
    accs = [CRMAccount.from_api(a) for a in crm_fixture["accounts"]]
    by_id = {a.id: a for a in accs}
    for c in crm_fixture["contacts"]:
        by_id[c["account_id"]].contacts.append(c)
    return accs


def test_renamed_facility_matches_by_exact_address(crm_fixture):
    idx = CRMIndex(_accounts(crm_fixture))
    site = _site("Bellhaven of Kettering", "3313 Wilmington Pike", "Kettering", "OH", "45429")
    cands = [c for c in find_candidates(site, idx) if c.eligible]
    assert [c.account_id for c in cands] == ["001WR41PYNWXCAE2X4"]
    assert cands[0].tier == TIER_HIGH_ADDRESS_EXACT
    assert cands[0].name != "Bellhaven of Kettering"  # matched on address despite the rename


def test_st_lawrence_matches_saint_lawrence(crm_fixture):
    idx = CRMIndex(_accounts(crm_fixture))
    site = _site("Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", "OH", "44883")
    cands = [c for c in find_candidates(site, idx) if c.eligible]
    assert [c.account_id for c in cands] == ["001U6RW32TY0WSXZZB"]
    assert cands[0].tier == TIER_HIGH_ADDRESS_EXACT


def test_amberly_manor_hudson_oh_does_not_match_colorado(crm_fixture):
    idx = CRMIndex(_accounts(crm_fixture))
    site = _site("Amberly Manor", "4390 Darrow Rd", "Hudson", "OH", "44236")
    cands = find_candidates(site, idx)
    eligible = [c for c in cands if c.eligible]
    assert eligible == []
    shown = [c for c in cands if c.account_id == "001AMBERLYCO000001"]
    assert shown and not shown[0].eligible and "STATE_MISMATCH" in shown[0].reasons
    results = match_all([site], _accounts(crm_fixture))
    assert results[0].tier == TIER_NO_CANDIDATE


def test_union_square_different_street_is_not_an_automatic_match(crm_fixture):
    idx = CRMIndex(_accounts(crm_fixture))
    site = _site("Bellhaven at Union Square", "118 Union Square Dr", "New Albany", "OH", "43054", phone="(330) 519-9639")
    cands = find_candidates(site, idx)
    assert all(not c.eligible for c in cands)
    us = [c for c in cands if c.account_id == "001UNIONSQUARE0001"][0]
    assert "STREET_MISMATCH" in us.reasons and us.phone_match  # phone corroborates but cannot override the address
    results = match_all([site], _accounts(crm_fixture))
    assert results[0].tier == TIER_LOW_NAME_ONLY


def test_owosso_both_accounts_grouped_with_evidence(crm_fixture):
    idx = CRMIndex(_accounts(crm_fixture))
    site = _site("Bellhaven of Owosso", "1120 W Main St", "Owosso", "MI", "48867", phone="(734) 503-1363", admin="Gloria Lambert")
    cands = [c for c in find_candidates(site, idx) if c.eligible]
    assert {c.account_id for c in cands} == {"001EGU7BMJ942ZTRE6", "001QU150PM4Z15UA71"}
    first = [c for c in cands if c.account_id == "001EGU7BMJ942ZTRE6"][0]
    second = [c for c in cands if c.account_id == "001QU150PM4Z15UA71"][0]
    assert first.phone_match and first.admin_match
    assert not second.phone_match and not second.admin_match
    results = match_all([site], _accounts(crm_fixture))
    assert "MULTIPLE_ACCOUNTS_SAME_ADDRESS" in results[0].reason_codes


def test_fuzzy_street_typo_is_medium_not_high(crm_fixture):
    accs = _accounts(crm_fixture)
    idx = CRMIndex(accs)
    site = _site("Bellhaven of Marietta", "805 Collegate Dr", "Marietta", "OH", "45750")
    cands = [c for c in find_candidates(site, idx) if c.eligible]
    assert [c.account_id for c in cands] == ["001A34WFSUYHCRBLFT"]
    assert cands[0].tier == TIER_MEDIUM_ADDRESS_FUZZY


def test_cross_parent_same_address_both_surfaced(crm_fixture):
    idx = CRMIndex(_accounts(crm_fixture))
    site = _site("Bellhaven Shores of Erie", "4930 W Lake Rd", "Erie", "PA", "16505")
    cands = [c for c in find_candidates(site, idx) if c.eligible]
    assert {c.account_id for c in cands} == {"001ERIEBELL0000001", "001ERIEHARBOR00001"}


def test_same_account_claimed_by_two_sites_is_contested(crm_fixture):
    accs = _accounts(crm_fixture)
    a = _site("Bellhaven of Wooster", "2301 Cleveland Road", "Wooster", "OH", "44691", slug="a")
    b = _site("Bellhaven Wooster East", "2301 Cleveland Rd", "Wooster", "OH", "44691", slug="b")
    results = match_all([a, b], accs)
    assert all("CRM_ACCOUNT_CONTESTED" in r.reason_codes for r in results)


def test_name_similarity_ignores_generic_tokens():
    assert name_similarity("Bellhaven at Union Square", "Union Square Senior Living") >= 85
    assert name_similarity("Bellhaven of Kettering", "Kettering Care Centre") >= 85
    assert name_similarity("Bellhaven of Marietta", "Bellhaven of Tiffin") < 85
