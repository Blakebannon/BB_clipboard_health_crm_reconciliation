"""Read-only scan pipeline shared by the CLI, the scheduler and the review app.

scan = scrape site + fetch all CRM accounts/contacts + match + propose + persist.
It performs **zero** CRM writes; the client it receives is read-only and would
raise if anything tried.
"""
from __future__ import annotations

import re
import traceback
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from . import config
from .crm_api import CRMClient, CRMError
from .matching import match_all, summarize_candidates
from .models import CRMAccount, SiteFacility
from .normalize import norm_name
from .rules import RuleContext, generate_proposals
from .scraper import ScrapeResult, SiteScraper
from .store import Store


class ScanError(Exception):
    pass


@dataclass
class ScanReport:
    run_id: str
    site_count: int
    scrape_complete: bool
    scrape_notes: list[str]
    crm_count: int
    contact_count: int
    bellhaven_parent_id: str | None
    bellhaven_parent_name: str | None
    bellhaven_child_count: int
    proposals_by_action: dict[str, int]
    match_tiers: dict[str, int]
    upsert_counts: dict[str, int]
    errors: list[str] = field(default_factory=list)
    crm_writes: int = 0
    observed_statuses: list[str] = field(default_factory=list)
    observed_care_types: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def find_bellhaven_parent(accounts: list[CRMAccount], parent_name: str) -> CRMAccount:
    """Dynamically identify the Bellhaven parent: top-level account named like the operator."""
    target = norm_name(parent_name)

    def base_name(a: CRMAccount) -> str:  # "Bellhaven Senior Living (Parent Account)" -> "bellhaven senior living"
        return norm_name(re.sub(r"\s*\(.*?\)\s*$", "", a.name))

    exact = [a for a in accounts if base_name(a) == target and not a.parent_id]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        # prefer the one with the most direct children
        kids = Counter(a.parent_id for a in accounts if a.parent_id)
        exact.sort(key=lambda a: -kids.get(a.id, 0))
        if kids.get(exact[0].id, 0) > kids.get(exact[1].id, 0):
            return exact[0]
        raise ScanError(f"multiple top-level accounts named {parent_name!r}: {[a.id for a in exact]}")
    loose = [a for a in accounts if target in norm_name(a.name) and not a.parent_id]
    if len(loose) == 1:
        return loose[0]
    raise ScanError(f"could not uniquely identify the {parent_name!r} parent account (candidates: {[a.id for a in loose]})")


def fetch_crm(client: CRMClient) -> tuple[list[CRMAccount], int]:
    if client.allow_writes:
        raise ScanError("scan must use a read-only client")
    accounts = [CRMAccount.from_api(raw) for raw in client.iter_all_accounts()]
    by_id = {a.id: a for a in accounts}
    n_contacts = 0
    try:
        for c in client.iter_all_contacts():
            n_contacts += 1
            acc = by_id.get(str(c.get("account_id") or ""))
            if acc is not None:
                acc.contacts.append(c)
    except CRMError as exc:  # contacts are corroborating evidence only; never fatal
        for a in accounts:
            a.contacts = []
        raise ScanError(f"contacts fetch failed: {exc}") from exc
    return accounts, n_contacts


def run_scan(settings: config.Settings, store: Store, client: CRMClient, scraper: SiteScraper | None = None) -> ScanReport:
    scraper = scraper or SiteScraper(settings.base_url, timeout=settings.http_timeout)
    run_id = store.start_run()
    errors: list[str] = []
    prev = store.last_successful_run()
    prev_count = int(prev["site_count"]) if prev and prev["site_count"] else None

    # 1. website
    scrape: ScrapeResult = scraper.scrape(previous_count=prev_count, expected_count=settings.site_expected_count)
    errors.extend(scrape.errors)
    store.save_site_snapshot(run_id, scrape.facilities)

    # 2. CRM (read-only)
    try:
        accounts, n_contacts = fetch_crm(client)
    except (CRMError, ScanError) as exc:
        msg = f"CRM fetch failed: {exc}"
        errors.append(msg)
        store.finish_run(run_id, site_count=scrape.count, crm_count=0, bellhaven_parent_id=None, bellhaven_child_count=0, scrape_ok=scrape.complete, error_summary="; ".join(errors), notes=scrape.completeness_notes, proposal_counts={})
        raise ScanError(msg) from exc
    store.save_crm_snapshot(run_id, accounts)

    try:
        parent = find_bellhaven_parent(accounts, settings.parent_name)
    except ScanError as exc:
        errors.append(str(exc))
        store.finish_run(run_id, site_count=scrape.count, crm_count=len(accounts), bellhaven_parent_id=None, bellhaven_child_count=0, scrape_ok=scrape.complete, error_summary="; ".join(errors), notes=scrape.completeness_notes, proposal_counts={})
        raise
    children = [a for a in accounts if a.parent_id == parent.id]

    # 3. match + propose (pure)
    results = match_all(scrape.facilities, accounts)
    ctx = RuleContext(
        bellhaven_parent_id=parent.id,
        bellhaven_parent_name=parent.name,
        accounts=accounts,
        scrape_complete=scrape.complete,
        site_count=scrape.count,
    )
    proposals = generate_proposals(results, ctx)
    counts = Counter(p.action_type for p in proposals)

    # 4. persist
    upsert = store.upsert_proposals(run_id, proposals)
    store.finish_run(
        run_id,
        site_count=scrape.count,
        crm_count=len(accounts),
        bellhaven_parent_id=parent.id,
        bellhaven_child_count=len(children),
        scrape_ok=scrape.complete,
        error_summary="; ".join(errors),
        notes=scrape.completeness_notes,
        proposal_counts=dict(counts),
    )
    writes = client.successful_writes
    store.log("scan_completed", {"run_id": run_id, "crm_writes": writes, "upsert": upsert})
    if writes:
        raise ScanError(f"INVARIANT VIOLATED: scan performed {writes} CRM writes")
    return ScanReport(
        run_id=run_id,
        site_count=scrape.count,
        scrape_complete=scrape.complete,
        scrape_notes=scrape.completeness_notes,
        crm_count=len(accounts),
        contact_count=n_contacts,
        bellhaven_parent_id=parent.id,
        bellhaven_parent_name=parent.name,
        bellhaven_child_count=len(children),
        proposals_by_action=dict(counts),
        match_tiers=summarize_candidates(results),
        upsert_counts=upsert,
        errors=errors,
        crm_writes=writes,
        observed_statuses=sorted(ctx.observed_statuses),
        observed_care_types=sorted(ctx.observed_care_types),
    )


def safe_run_scan(settings: config.Settings, store: Store, client: CRMClient, scraper: SiteScraper | None = None) -> tuple[ScanReport | None, str | None]:
    try:
        return run_scan(settings, store, client, scraper), None
    except Exception as exc:  # surfaced to UI/CLI; never swallowed silently
        return None, f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}"
