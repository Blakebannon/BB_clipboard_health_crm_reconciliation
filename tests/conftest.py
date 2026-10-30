from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

import pytest

from bellhaven import config
from bellhaven.scraper import ScrapeError, SiteScraper
from bellhaven.store import Store

from .fake_crm import FakeCRMClient

FIXTURES = Path(__file__).parent / "fixtures"
SITE_DIR = FIXTURES / "site"
BASE = "https://example.test"


def make_fixture_fetch(missing: set[str] | None = None, broken: set[str] | None = None):
    """Return a fetch(url)->html function backed by tests/fixtures/site.

    ``missing``: slugs whose detail page should 404 (raise ScrapeError).
    ``broken``: slugs whose detail page should return HTML without the address block.
    """
    missing = missing or set()
    broken = broken or set()

    def fetch(url: str) -> str:
        p = urlparse(url)
        path, query = p.path.rstrip("/"), p.query
        if path in ("", "/"):
            return (SITE_DIR / "__home.html").read_text(encoding="utf-8")
        if path == "/communities":
            page = "1"
            for part in query.split("&"):
                if part.startswith("page="):
                    page = part.split("=", 1)[1]
            f = SITE_DIR / f"__communities_p{page}.html"
            if not f.exists():
                raise ScrapeError(f"GET {url} failed: 404")
            return f.read_text(encoding="utf-8")
        if path.startswith("/communities/"):
            slug = path.split("/")[-1]
            if slug in missing:
                raise ScrapeError(f"GET {url} failed: 500")
            if slug in broken:
                return "<html><body><h1>Broken</h1></body></html>"
            f = SITE_DIR / f"{slug}.html"
            if not f.exists():
                raise ScrapeError(f"GET {url} failed: 404")
            return f.read_text(encoding="utf-8")
        raise ScrapeError(f"GET {url} failed: 404")

    return fetch


@pytest.fixture
def fixture_scraper() -> SiteScraper:
    return SiteScraper(BASE, fetch=make_fixture_fetch())


@pytest.fixture
def crm_fixture() -> dict:
    return json.loads((FIXTURES / "crm_accounts.json").read_text(encoding="utf-8"))


@pytest.fixture
def fake_client(crm_fixture) -> FakeCRMClient:
    return FakeCRMClient(crm_fixture["accounts"], crm_fixture["contacts"], allow_writes=False)


@pytest.fixture
def writable_client(crm_fixture) -> FakeCRMClient:
    return FakeCRMClient(crm_fixture["accounts"], crm_fixture["contacts"], allow_writes=True)


@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(tmp_path / "ledger.sqlite3")
    yield s
    s.close()


@pytest.fixture
def settings(tmp_path) -> config.Settings:
    return config.Settings(base_url=BASE, api_token="test-token-not-real", db_path=tmp_path / "ledger.sqlite3", site_expected_count=35)


BELL = "0015QAPLGS3FVYEEEM"
