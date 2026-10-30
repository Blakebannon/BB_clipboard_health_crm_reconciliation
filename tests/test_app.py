"""Headless Streamlit test: rendering never writes; Reject never writes; Approve & apply writes exactly once."""
from __future__ import annotations

import json
import os

import pytest
from streamlit.testing.v1 import AppTest

from bellhaven import config, crm_api
from bellhaven.models import ACTION_REPARENT, APPLY_APPLIED, DECISION_REJECTED
from bellhaven.pipeline import run_scan
from bellhaven.scraper import SiteScraper
from bellhaven.store import Store

from .conftest import BASE, FIXTURES, make_fixture_fetch
from .fake_crm import FakeCRMClient

APP = str(config.REPO_ROOT / "app.py")


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    db = tmp_path / "ui.sqlite3"
    monkeypatch.setenv("BELLHAVEN_DB_PATH", str(db))
    monkeypatch.setenv("BELLHAVEN_API_TOKEN", "dummy-token-for-ui-test")
    monkeypatch.setenv("BELLHAVEN_BASE_URL", BASE)
    fx = json.loads((FIXTURES / "crm_accounts.json").read_text(encoding="utf-8"))
    remote = FakeCRMClient(fx["accounts"], fx["contacts"])  # shared "remote" state
    clients: list[FakeCRMClient] = []

    def fake_make_client(settings, allow_writes=False):
        c = FakeCRMClient([], allow_writes=allow_writes)
        c.accounts, c.contacts = remote.accounts, remote.contacts
        clients.append(c)
        return c

    monkeypatch.setattr(crm_api, "make_client", fake_make_client)
    settings = config.get_settings()
    store = Store(db)
    run_scan(settings, store, fake_make_client(settings), SiteScraper(BASE, fetch=make_fixture_fetch()))
    store.close()
    clients.clear()
    import streamlit as st

    st.cache_resource.clear()
    return {"db": db, "remote": remote, "clients": clients}


def _key(db, action, account_id):
    s = Store(db)
    try:
        for r in s.list_proposals(action_types=[action]):
            if account_id in json.loads(r["crm_account_ids_json"]):
                return r["proposal_key"]
    finally:
        s.close()
    raise AssertionError


def test_page_load_performs_no_writes(app_env):
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    assert app_env["clients"] == []  # no CRM client even constructed on render
    assert app_env["remote"].successful_writes == 0


def test_reject_writes_only_locally(app_env):
    key = _key(app_env["db"], ACTION_REPARENT, "001LGFPBJY4N9MB6KL")
    at = AppTest.from_file(APP, default_timeout=60).run()
    at.selectbox[0].set_value(key).run()
    at.text_input(key=f"reject-reason-{key}").set_value("not convinced yet").run()
    at.button(key=f"reject-{key}").click().run()
    assert not at.exception
    s = Store(app_env["db"])
    assert s.get_proposal_row(key)["decision"] == DECISION_REJECTED
    s.close()
    assert app_env["clients"] == [] and app_env["remote"].successful_writes == 0


def test_approve_and_apply_writes_exactly_once_and_verifies(app_env):
    key = _key(app_env["db"], ACTION_REPARENT, "001LGFPBJY4N9MB6KL")
    at = AppTest.from_file(APP, default_timeout=60).run()
    at.selectbox[0].set_value(key).run()
    btn = at.button(key=f"approve-{key}")
    assert btn.disabled  # confirmation checkbox not ticked
    at.checkbox(key=f"confirm-{key}").check().run()
    at.button(key=f"approve-{key}").click().run()
    assert not at.exception
    writers = [c for c in app_env["clients"] if c.allow_writes]
    assert len(writers) == 1 and len(writers[0].write_log) == 1
    assert writers[0].write_log[0].method == "PATCH" and writers[0].write_log[0].payload == {config.FIELD_PARENT: "0015QAPLGS3FVYEEEM"}
    assert app_env["remote"].accounts["001LGFPBJY4N9MB6KL"][config.FIELD_PARENT] == "0015QAPLGS3FVYEEEM"
    s = Store(app_env["db"])
    assert s.get_proposal_row(key)["apply_status"] == APPLY_APPLIED
    s.close()
    assert writers[0].successful_writes == 1
