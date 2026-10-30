"""Mandatory test 1: discovery of all directory pages + homepage-only Findlay; fail-closed behaviour."""
from __future__ import annotations

from bellhaven.scraper import SiteScraper, canonical_url

from .conftest import BASE, make_fixture_fetch


def test_discovers_16_16_2_plus_homepage_findlay(fixture_scraper: SiteScraper):
    d = fixture_scraper.discover()
    assert d.pages_visited == [1, 2, 3]
    assert d.total_pages_declared == 3
    assert d.directory_count_declared == 34
    assert len(d.directory_urls) == 34
    per_page = {p: sum(1 for u, srcs in d.discovered_via.items() if f"directory:p{p}" in srcs) for p in (1, 2, 3)}
    assert per_page == {1: 16, 2: 16, 3: 2}
    findlay = f"{BASE}/communities/bellhaven-meadows-of-findlay"
    assert findlay in d.homepage_urls
    assert findlay not in d.directory_urls  # homepage-only
    assert len(d.all_urls) == 35
    assert d.structurally_complete


def test_full_scrape_is_complete_with_required_fields(fixture_scraper: SiteScraper):
    r = fixture_scraper.scrape(expected_count=35)
    assert r.complete, r.completeness_notes
    assert r.count == 35
    assert r.errors == []
    for f in r.facilities:
        assert f.name and f.street and f.city and f.state and f.zip and f.care_offerings and f.source_url and f.scraped_at
        assert len(f.state) == 2 and f.zip.isdigit() and len(f.zip) == 5
    by_slug = {f.slug: f for f in r.facilities}
    findlay = by_slug["/communities/bellhaven-meadows-of-findlay"]
    assert findlay.care_offerings == ["Assisted Living", "Memory Support"]
    assert findlay.administrator == "Sam Pruitt" and findlay.phone == "(231) 533-2969"
    assert findlay.discovered_via == ["homepage"]
    erie = by_slug["/communities/bellhaven-shores-of-erie"]
    assert erie.care_offerings == ["Assisted Living", "Memory Support"]
    tiffin = by_slug["/communities/bellhaven-of-tiffin"]
    assert tiffin.street == "45 St Lawrence Dr" and tiffin.zip == "44883"
    # unique by canonical URL and by physical address
    assert len({f.source_url for f in r.facilities}) == 35
    assert len({f.addr.strict for f in r.facilities}) == 35


def test_detail_page_failure_marks_scan_incomplete():
    scraper = SiteScraper(BASE, fetch=make_fixture_fetch(missing={"bellhaven-of-coldwater", "bellhaven-of-tiffin"}))
    r = scraper.scrape(expected_count=35)
    assert not r.complete
    assert r.count == 34
    assert any("bellhaven-of-tiffin" in e for e in r.errors)
    assert any("site-absence proposals are disabled" in n for n in r.completeness_notes)


def test_missing_fields_marks_scan_incomplete():
    scraper = SiteScraper(BASE, fetch=make_fixture_fetch(broken={"bellhaven-of-marietta"}))
    r = scraper.scrape(expected_count=35)
    assert not r.complete and r.count == 34
    assert any("marietta" in e for e in r.errors)


def test_pagination_truncation_marks_scan_incomplete():
    base_fetch = make_fixture_fetch()

    def fetch(url: str) -> str:
        if "page=3" in url:
            raise Exception("boom")  # not a ScrapeError: must still fail closed
        return base_fetch(url)

    scraper = SiteScraper(BASE, fetch=lambda u: _wrap(fetch, u))
    r = scraper.scrape(expected_count=35)
    assert not r.complete
    assert r.discovery.pages_visited == [1, 2]
    assert r.count == 33


def _wrap(fetch, url):
    from bellhaven.scraper import ScrapeError

    try:
        return fetch(url)
    except ScrapeError:
        raise
    except Exception as exc:
        raise ScrapeError(str(exc))


def test_suspicious_drop_against_previous_run_marks_incomplete(fixture_scraper: SiteScraper):
    r = fixture_scraper.scrape(previous_count=60)
    assert r.count == 35 and not r.complete
    assert any("dropped suspiciously" in n for n in r.completeness_notes)


def test_canonical_url_strips_query_and_slash():
    assert canonical_url(BASE, "/communities/x/?utm=1#top") == f"{BASE}/communities/x"
    assert canonical_url(BASE, f"{BASE}/communities/x") == f"{BASE}/communities/x"
