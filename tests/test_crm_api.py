"""Real HTTP client behaviour against a mocked server: auth header, pagination, retries only on GET, token redaction."""
from __future__ import annotations

import pytest
import requests
import responses

from bellhaven.crm_api import CRMAuthError, CRMClient, CRMUncertainWrite, WritesDisabledError

API = "https://example.test/api/v1"
TOKEN = "secret-token-value-123"


@responses.activate
def test_bearer_header_and_pagination_envelope():
    responses.get(f"{API}/accounts", json={"items": [{"id": "1"}, {"id": "2"}], "total": 3, "page": 1, "page_size": 2})
    responses.get(f"{API}/accounts", json={"items": [{"id": "3"}], "total": 3, "page": 2, "page_size": 2})
    c = CRMClient("https://example.test", TOKEN, page_size=2)
    ids = [a["id"] for a in c.iter_all_accounts()]
    assert ids == ["1", "2", "3"]
    assert responses.calls[0].request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in responses.calls[0].request.url
    assert "page=1" in responses.calls[0].request.url and "page=2" in responses.calls[1].request.url


@responses.activate
def test_pagination_stops_on_repeated_records_bare_list():
    responses.get(f"{API}/accounts", json=[{"id": "1"}, {"id": "2"}])
    responses.get(f"{API}/accounts", json=[{"id": "1"}, {"id": "2"}])  # server re-serves last page
    c = CRMClient("https://example.test", TOKEN, page_size=2)
    assert [a["id"] for a in c.iter_all_accounts()] == ["1", "2"]
    assert len(responses.calls) == 2


@responses.activate
def test_get_retries_on_5xx_then_succeeds():
    responses.get(f"{API}/accounts/X", status=503)
    responses.get(f"{API}/accounts/X", json={"id": "X"})
    c = CRMClient("https://example.test", TOKEN)
    assert c.get_account("X")["id"] == "X"
    assert len(responses.calls) == 2


@responses.activate
def test_401_raises_auth_error_without_token_leak():
    responses.get(f"{API}/me", status=401, json={"detail": "Provide your candidate token"})
    c = CRMClient("https://example.test", TOKEN)
    with pytest.raises(CRMAuthError) as ei:
        c.me()
    assert TOKEN not in str(ei.value)


@responses.activate
def test_post_is_never_retried_and_timeout_is_uncertain():
    responses.post(f"{API}/accounts", body=requests.Timeout("slow"))
    c = CRMClient("https://example.test", TOKEN, allow_writes=True)
    with pytest.raises(CRMUncertainWrite):
        c.create_account({"name": "x"})
    assert len(responses.calls) == 1
    assert c.successful_writes == 0 and len(c.write_log) == 1


@responses.activate
def test_read_only_client_never_sends_a_write():
    c = CRMClient("https://example.test", TOKEN)
    with pytest.raises(WritesDisabledError):
        c.update_account("X", {"name": "y"})
    assert len(responses.calls) == 0


def test_repr_hides_token():
    c = CRMClient("https://example.test", TOKEN)
    assert TOKEN not in repr(c)
