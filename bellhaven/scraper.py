"""Fail-closed scraper for the Bellhaven Senior Living website.

Discovery sources (all of them, every run):
* the homepage (featured / "new this year" community links), and
* every page of ``/communities`` reached by following the *Next* link.

``ScrapeResult.complete`` is only True when every discovered detail page was
fetched and parsed with all required fields, the directory pagination was
walked to its declared last page, the number of directory URLs equals the
directory's own declared count, and the total did not drop suspiciously
against the previous successful run (or the configured first-run expectation).
When it is False, callers must not generate any "missing from site" proposals.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .models import SiteFacility

USER_AGENT = "bellhaven-reconciliation/1.0 (+read-only scraper)"
MAX_DIRECTORY_PAGES = 50  # loop guard

_PAGE_OF = re.compile(r"Page\s+(\d+)\s+(?:of|/)\s+(\d+)", re.I)
_LISTED = re.compile(r"(\d+)\s+communities\s+listed", re.I)
_CITY_STATE_ZIP = re.compile(r"^(.*?),\s*([A-Za-z]{2})\s+(\d{5}(?:-\d{4})?)\s*$")


class ScrapeError(Exception):
    pass


@dataclass
class DiscoveryResult:
    directory_urls: list[str] = field(default_factory=list)  # in page order, unique
    homepage_urls: list[str] = field(default_factory=list)
    pages_visited: list[int] = field(default_factory=list)
    total_pages_declared: int | None = None
    directory_count_declared: int | None = None
    errors: list[str] = field(default_factory=list)
    discovered_via: dict[str, list[str]] = field(default_factory=dict)  # url -> sources

    @property
    def all_urls(self) -> list[str]:
        out: list[str] = []
        for u in self.directory_urls + self.homepage_urls:
            if u not in out:
                out.append(u)
        return out

    @property
    def structurally_complete(self) -> bool:
        if self.errors:
            return False
        if self.total_pages_declared is None or self.directory_count_declared is None:
            return False
        if sorted(self.pages_visited) != list(range(1, self.total_pages_declared + 1)):
            return False
        if len(self.directory_urls) != self.directory_count_declared:
            return False
        return True


@dataclass
class ScrapeResult:
    facilities: list[SiteFacility]
    discovery: DiscoveryResult
    errors: list[str]
    complete: bool
    completeness_notes: list[str]
    scraped_at: str

    @property
    def count(self) -> int:
        return len(self.facilities)


def canonical_url(base_url: str, href: str) -> str:
    """Absolute URL with query/fragment stripped and no trailing slash."""
    absolute = urljoin(base_url.rstrip("/") + "/", href)
    p = urlparse(absolute)
    path = p.path.rstrip("/") or "/"
    return f"{p.scheme}://{p.netloc}{path}"


def slug_from_url(url: str) -> str:
    return urlparse(url).path.rstrip("/")


class SiteScraper:
    def __init__(
        self,
        base_url: str,
        session: requests.Session | None = None,
        timeout: float = 20.0,
        fetch: Callable[[str], str] | None = None,
        polite_delay: float = 0.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.polite_delay = polite_delay
        self._session = session or requests.Session()
        self._session.headers.setdefault("User-Agent", USER_AGENT)
        self._fetch_override = fetch

    # --- HTTP ----------------------------------------------------------------

    def fetch(self, url: str) -> str:
        if self._fetch_override is not None:
            return self._fetch_override(url)
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = self._session.get(url, timeout=self.timeout)
                if resp.status_code >= 500 and attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                if self.polite_delay:
                    time.sleep(self.polite_delay)
                return resp.text
            except requests.RequestException as exc:  # pragma: no cover - network
                last_exc = exc
                time.sleep(0.5 * (attempt + 1))
        raise ScrapeError(f"GET {url} failed: {last_exc}")

    # --- discovery -----------------------------------------------------------

    def _community_links(self, soup: BeautifulSoup) -> list[str]:
        out: list[str] = []
        for a in soup.select("a[href]"):
            href = a["href"]
            if not href.startswith("/communities/") and "/communities/" not in href:
                continue
            url = canonical_url(self.base_url, href)
            if urlparse(url).path.count("/") != 2:  # exactly /communities/<slug>
                continue
            if url not in out:
                out.append(url)
        return out

    def discover(self) -> DiscoveryResult:
        res = DiscoveryResult()

        # Homepage featured links
        try:
            home = BeautifulSoup(self.fetch(self.base_url + "/"), "html.parser")
            for u in self._community_links(home):
                res.homepage_urls.append(u)
                res.discovered_via.setdefault(u, []).append("homepage")
        except ScrapeError as exc:
            res.errors.append(f"homepage: {exc}")

        # Directory: follow Next links from page 1
        next_url: str | None = self.base_url + "/communities"
        seen_pages: set[str] = set()
        hops = 0
        while next_url and hops < MAX_DIRECTORY_PAGES:
            hops += 1
            if next_url in seen_pages:
                res.errors.append(f"pagination loop at {next_url}")
                break
            seen_pages.add(next_url)
            try:
                html = self.fetch(next_url)
            except ScrapeError as exc:
                res.errors.append(f"directory: {exc}")
                break
            soup = BeautifulSoup(html, "html.parser")
            text = soup.get_text(" ", strip=True)
            m = _PAGE_OF.search(text)
            page_no = int(m.group(1)) if m else None
            if m:
                res.total_pages_declared = int(m.group(2))
            lm = _LISTED.search(text)
            if lm and res.directory_count_declared is None:
                res.directory_count_declared = int(lm.group(1))
            if page_no is None:
                res.errors.append(f"directory: could not find 'Page N of M' marker at {next_url}")
                break
            if page_no in res.pages_visited:
                res.errors.append(f"directory: page {page_no} served twice")
                break
            res.pages_visited.append(page_no)
            links = self._community_links(soup)
            if not links:
                res.errors.append(f"directory: page {page_no} had no community links")
            for u in links:
                tag = f"directory:p{page_no}"
                if u not in res.directory_urls:
                    res.directory_urls.append(u)
                res.discovered_via.setdefault(u, []).append(tag)

            next_url = None
            for a in soup.select(".pager a[href], nav a[href], a[rel=next]"):
                label = a.get_text(" ", strip=True).lower()
                if "next" in label or a.get("rel") == ["next"]:
                    next_url = urljoin(self.base_url + "/", a["href"])
                    break
        if hops >= MAX_DIRECTORY_PAGES:
            res.errors.append("directory: exceeded page limit")
        return res

    # --- detail pages --------------------------------------------------------

    def parse_detail(self, url: str, html: str, scraped_at: str) -> SiteFacility:
        soup = BeautifulSoup(html, "html.parser")
        h1 = soup.find("h1")
        name = h1.get_text(" ", strip=True) if h1 else ""
        fields: dict[str, object] = {}
        dl = soup.select_one("dl.detail") or soup.find("dl")
        if dl is None:
            raise ScrapeError(f"{url}: no <dl> detail block")
        dts = dl.find_all("dt")
        for dt in dts:
            label = dt.get_text(" ", strip=True).lower()
            dd = dt.find_next_sibling("dd")
            if dd is None:
                continue
            if label == "address":
                # street<br>City, ST ZIP
                parts = [p.strip() for p in dd.get_text("\n", strip=True).split("\n") if p.strip()]
                fields["address_lines"] = parts
            elif label.startswith("care"):
                badges = [b.get_text(" ", strip=True) for b in dd.select(".badge")]
                if not badges:
                    badges = [t.strip() for t in dd.get_text("|", strip=True).split("|") if t.strip()]
                fields["care"] = badges
            elif label == "administrator":
                fields["administrator"] = dd.get_text(" ", strip=True)
            elif label == "phone":
                fields["phone"] = dd.get_text(" ", strip=True)

        lines = fields.get("address_lines") or []
        if not name or len(lines) < 2:  # type: ignore[arg-type]
            raise ScrapeError(f"{url}: missing name or address ({name!r}, {lines!r})")
        street = " ".join(lines[:-1])  # type: ignore[index]
        m = _CITY_STATE_ZIP.match(lines[-1])  # type: ignore[index]
        if not m:
            raise ScrapeError(f"{url}: unparseable city/state/zip line {lines[-1]!r}")  # type: ignore[index]
        city, state, zip_code = m.group(1).strip(), m.group(2).upper(), m.group(3)
        care = list(fields.get("care") or [])  # type: ignore[arg-type]
        if not care:
            raise ScrapeError(f"{url}: no care offerings found")
        return SiteFacility(
            name=name,
            street=street,
            city=city,
            state=state,
            zip=zip_code,
            care_offerings=care,
            source_url=url,
            scraped_at=scraped_at,
            phone=str(fields.get("phone", "")),
            administrator=str(fields.get("administrator", "")),
            slug=slug_from_url(url),
        )

    def fetch_facility(self, url: str, scraped_at: str | None = None) -> SiteFacility:
        scraped_at = scraped_at or datetime.now(timezone.utc).isoformat()
        return self.parse_detail(url, self.fetch(url), scraped_at)

    # --- orchestration -------------------------------------------------------

    def scrape(self, previous_count: int | None = None, expected_count: int | None = None) -> ScrapeResult:
        scraped_at = datetime.now(timezone.utc).isoformat()
        discovery = self.discover()
        errors: list[str] = list(discovery.errors)
        notes: list[str] = []
        facilities: list[SiteFacility] = []
        by_addr: dict[tuple, str] = {}

        for url in discovery.all_urls:
            try:
                fac = self.fetch_facility(url, scraped_at)
            except ScrapeError as exc:
                errors.append(str(exc))
                continue
            fac.discovered_via = list(discovery.discovered_via.get(url, []))
            key = fac.addr.strict
            if key in by_addr:
                notes.append(
                    f"two site pages share one physical address: {by_addr[key]} and {url} (kept both)"
                )
            else:
                by_addr[key] = url
            facilities.append(fac)

        complete = discovery.structurally_complete and not errors
        if not discovery.structurally_complete:
            notes.append(
                "directory pagination/count check failed: "
                f"pages={discovery.pages_visited} declared_pages={discovery.total_pages_declared} "
                f"urls={len(discovery.directory_urls)} declared_count={discovery.directory_count_declared}"
            )
        if errors:
            notes.append(f"{len(errors)} fetch/parse error(s); site-absence proposals are disabled")

        n = len(facilities)
        baseline = previous_count if previous_count else expected_count
        if baseline and n < baseline * 0.9:
            complete = False
            notes.append(
                f"facility count {n} dropped suspiciously below baseline {baseline}; "
                "site-absence proposals are disabled"
            )
        elif expected_count and n != expected_count:
            notes.append(f"facility count {n} differs from configured expectation {expected_count} (informational)")

        return ScrapeResult(
            facilities=facilities,
            discovery=discovery,
            errors=errors,
            complete=complete,
            completeness_notes=notes,
            scraped_at=scraped_at,
        )
