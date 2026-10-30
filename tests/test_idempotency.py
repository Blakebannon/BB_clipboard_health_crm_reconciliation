"""Mandatory test 10: decisions survive rescans; changed evidence is a new, visible proposal."""
from __future__ import annotations

import json

from bellhaven.apply import apply_proposal
from bellhaven.models import ACTION_CREATE_MISSING, ACTION_REPARENT, APPLY_APPLIED, DECISION_APPROVED, DECISION_PENDING, DECISION_REJECTED
from bellhaven.pipeline import run_scan
from bellhaven.scraper import SiteScraper

from .conftest import BASE, BELL, SITE_DIR, make_fixture_fetch
from .fake_crm import FakeCRMClient
from .test_apply import key_for, writable


def scan(settings, store, client, fetch=None):
    return run_scan(settings, store, client, SiteScraper(BASE, fetch=fetch or make_fixture_fetch()))


def test_two_scans_produce_identical_queue(settings, store, fake_client):
    r1 = scan(settings, store, fake_client)
    keys1 = sorted(r["proposal_key"] for r in store.list_proposals())
    r2 = scan(settings, store, fake_client)
    keys2 = sorted(r["proposal_key"] for r in store.list_proposals())
    assert keys1 == keys2
    assert r2.upsert_counts == {"new": 0, "existing": len(keys1), "superseded": 0, "stale": 0}
    assert r1.crm_writes == r2.crm_writes == 0


def test_approved_and_rejected_stay_decided_across_two_rescans(settings, store, fake_client):
    scan(settings, store, fake_client)
    k_reject = key_for(store, ACTION_REPARENT, "001ZANESVILLE00001")
    k_apply = key_for(store, ACTION_REPARENT, "001LGFPBJY4N9MB6KL")
    store.reject(k_reject, "need to double check with ops")
    assert apply_proposal(store, writable(fake_client), BELL, k_apply).ok
    for _ in range(2):
        scan(settings, store, fake_client)
        assert store.get_proposal_row(k_reject)["decision"] == DECISION_REJECTED
        assert store.get_proposal_row(k_apply)["decision"] == DECISION_APPROVED
        assert store.get_proposal_row(k_apply)["apply_status"] == APPLY_APPLIED
        pending_keys = {r["proposal_key"] for r in store.list_proposals(decisions=[DECISION_PENDING])}
        assert k_reject not in pending_keys and k_apply not in pending_keys
    assert fake_client.successful_writes == 0  # the read-only scan client never wrote
    assert len(fake_client.accounts) == 28


def test_changed_source_evidence_yields_new_proposal_and_supersedes_pending(settings, store, fake_client):
    scan(settings, store, fake_client)
    old_key = next(r["proposal_key"] for r in store.list_proposals(action_types=[ACTION_CREATE_MISSING]) if "Goshen" in r["title"])
    base = make_fixture_fetch()

    def fetch(url: str) -> str:
        html = base(url)
        if url.endswith("/communities/bellhaven-of-goshen"):
            html = html.replace("(330) 405-6040", "(330) 405-7777")
        return html

    rep = scan(settings, store, fake_client, fetch)
    assert rep.upsert_counts["new"] == 1 and rep.upsert_counts["superseded"] == 1
    old = store.get_proposal_row(old_key)
    assert old["stale"] == 1 and old["superseded_by"]
    new = store.get_proposal_row(old["superseded_by"])
    assert new["decision"] == DECISION_PENDING and "Goshen" in new["title"]
    assert json.loads(new["data_json"])["after"]["NEW"]["phone"] == "(330) 405-7777"


def test_changed_evidence_does_not_silently_override_a_rejection(settings, store, fake_client):
    scan(settings, store, fake_client)
    old_key = next(r["proposal_key"] for r in store.list_proposals(action_types=[ACTION_CREATE_MISSING]) if "Goshen" in r["title"])
    store.reject(old_key, "do not create")
    base = make_fixture_fetch()
    fetch = lambda u: base(u).replace("(330) 405-6040", "(330) 405-7777") if u.endswith("bellhaven-of-goshen") else base(u)
    scan(settings, store, fake_client, fetch)
    old = store.get_proposal_row(old_key)
    assert old["decision"] == DECISION_REJECTED  # the rejection is preserved
    new = store.get_proposal_row(old["superseded_by"])
    assert new["decision"] == DECISION_PENDING  # the changed proposal is visibly new, not auto-rejected


def test_stale_proposal_cannot_be_applied(settings, store, fake_client):
    scan(settings, store, fake_client)
    key = key_for(store, ACTION_REPARENT, "001LGFPBJY4N9MB6KL")
    fake_client.accounts["001LGFPBJY4N9MB6KL"]["parent_id"] = BELL  # fixed out-of-band
    scan(settings, store, fake_client)
    assert store.get_proposal_row(key)["stale"] == 1
    res = apply_proposal(store, writable(fake_client), BELL, key)
    assert not res.ok and "stale" in res.message


def test_reopen_is_explicit_only(settings, store, fake_client):
    scan(settings, store, fake_client)
    key = key_for(store, ACTION_REPARENT, "001LGFPBJY4N9MB6KL")
    store.reject(key, "x")
    scan(settings, store, fake_client)
    assert store.get_proposal_row(key)["decision"] == DECISION_REJECTED
    store.reopen(key, "reviewer changed their mind")
    assert store.get_proposal_row(key)["decision"] == DECISION_PENDING
