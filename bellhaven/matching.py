"""Deterministic, explainable entity resolution between site facilities and CRM accounts.

Evidence hierarchy (strongest first):
1. Street number + canonical street + state + ZIP (or city when a ZIP is missing).
2. Street number + state + (ZIP or city) + *fuzzy* street (typos only).
3. Name similarity -- never sufficient on its own; it is recorded for
   transparency and routes the facility to manual review.
4. Phone / administrator-contact corroboration -- raises confidence, never
   overrides an address contradiction.

Hard negatives: a different state, or a different street number / street,
blocks a match no matter how similar the names are.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz

from .models import (
    TIER_AMBIGUOUS,
    TIER_HIGH_ADDRESS_EXACT,
    TIER_LOW_NAME_ONLY,
    TIER_MEDIUM_ADDRESS_FUZZY,
    TIER_NO_CANDIDATE,
    Candidate,
    CRMAccount,
    SiteFacility,
)
from .normalize import norm_name, norm_text

STREET_FUZZY_MIN = 80.0
NAME_FUZZY_MIN = 85.0

GENERIC_NAME_TOKENS = {
    "bellhaven",
    "senior",
    "living",
    "care",
    "center",
    "centre",
    "home",
    "homes",
    "health",
    "healthcare",
    "nursing",
    "rehabilitation",
    "rehab",
    "community",
    "retirement",
    "assisted",
    "memory",
    "village",
    "residence",
    "and",
    "of",
    "the",
    "at",
    "inc",
    "llc",
}


def distinctive_tokens(name: str) -> set[str]:
    return {t for t in norm_name(name).split() if t not in GENERIC_NAME_TOKENS}


def name_similarity(a: str, b: str) -> float:
    """0..100 similarity on the *distinctive* parts of two facility names."""
    da, db = distinctive_tokens(a), distinctive_tokens(b)
    if not da or not db:
        return float(fuzz.token_sort_ratio(norm_name(a), norm_name(b)))
    return float(fuzz.token_sort_ratio(" ".join(sorted(da)), " ".join(sorted(db))))


@dataclass
class CRMIndex:
    accounts: list[CRMAccount]
    by_id: dict[str, CRMAccount] = field(default_factory=dict)
    by_state: dict[str, list[CRMAccount]] = field(default_factory=lambda: defaultdict(list))
    by_strict_addr: dict[tuple, list[CRMAccount]] = field(default_factory=lambda: defaultdict(list))
    by_loose_addr: dict[tuple, list[CRMAccount]] = field(default_factory=lambda: defaultdict(list))
    by_phone: dict[str, list[CRMAccount]] = field(default_factory=lambda: defaultdict(list))
    children: dict[str | None, list[CRMAccount]] = field(default_factory=lambda: defaultdict(list))

    def __post_init__(self) -> None:
        for a in self.accounts:
            self.by_id[a.id] = a
            self.by_state[a.addr.state].append(a)
            if a.addr.is_usable:
                if a.addr.zip5:
                    self.by_strict_addr[a.addr.strict].append(a)
                self.by_loose_addr[a.addr.loose].append(a)
            if a.phone_norm:
                self.by_phone[a.phone_norm].append(a)
            self.children[a.parent_id].append(a)


@dataclass
class MatchResult:
    site: SiteFacility
    candidates: list[Candidate]
    tier: str
    reason_codes: list[str]

    @property
    def eligible(self) -> list[Candidate]:
        return [c for c in self.candidates if c.eligible]

    @property
    def high(self) -> list[Candidate]:
        return [c for c in self.eligible if c.tier == TIER_HIGH_ADDRESS_EXACT]

    @property
    def medium(self) -> list[Candidate]:
        return [c for c in self.eligible if c.tier == TIER_MEDIUM_ADDRESS_FUZZY]


def _admin_matches(site: SiteFacility, account: CRMAccount) -> bool:
    admin = norm_text(site.administrator)
    if not admin:
        return False
    for c in account.contacts:
        full = norm_text(
            c.get("name")
            or " ".join(str(c.get(k, "")) for k in ("first_name", "last_name") if c.get(k))
        )
        if full and full == admin:
            return True
    return False


def _candidate(site: SiteFacility, acc: CRMAccount, tier: str, reasons: list[str], eligible: bool, street_score: float) -> Candidate:
    return Candidate(
        account_id=acc.id,
        name=acc.name,
        parent_id=acc.parent_id,
        tier=tier,
        reasons=list(reasons),
        address=f"{acc.street}, {acc.city}, {acc.state} {acc.zip}".strip(", "),
        status=acc.status,
        lifetime_revenue=None if acc.lifetime_revenue is None else str(acc.lifetime_revenue),
        outstanding_ar=None if acc.outstanding_ar is None else str(acc.outstanding_ar),
        chow_current_account=acc.chow_current_account,
        duplicate_of_account=acc.duplicate_of_account,
        name_score=round(name_similarity(site.name, acc.name), 1),
        street_score=round(street_score, 1),
        phone_match=bool(site.phone_norm) and site.phone_norm == acc.phone_norm,
        admin_match=_admin_matches(site, acc),
        eligible=eligible,
    )


def find_candidates(site: SiteFacility, index: CRMIndex) -> list[Candidate]:
    saddr = site.addr
    out: list[Candidate] = []
    seen: set[str] = set()

    def add(c: Candidate) -> None:
        if c.account_id not in seen:
            seen.add(c.account_id)
            out.append(c)

    # 1. exact address
    exact: list[CRMAccount] = []
    if saddr.is_usable:
        if saddr.zip5:
            exact.extend(index.by_strict_addr.get(saddr.strict, []))
        # ZIP missing on one side: fall back to city
        for acc in index.by_loose_addr.get(saddr.loose, []):
            if acc not in exact and (not acc.addr.zip5 or not saddr.zip5):
                exact.append(acc)
    for acc in exact:
        reasons = ["ADDRESS_EXACT"]
        if acc.addr.zip5 and saddr.zip5 and acc.addr.zip5 == saddr.zip5:
            reasons.append("ZIP_MATCH")
        else:
            reasons.append("ZIP_MISSING_CITY_MATCH")
        c = _candidate(site, acc, TIER_HIGH_ADDRESS_EXACT, reasons, True, 100.0)
        if c.phone_match:
            c.reasons.append("PHONE_MATCH")
        if c.admin_match:
            c.reasons.append("ADMIN_CONTACT_MATCH")
        if c.name_score >= NAME_FUZZY_MIN:
            c.reasons.append("NAME_SIMILAR")
        else:
            c.reasons.append("NAME_DIFFERS")
        add(c)

    # 2. fuzzy street, same number + state + (zip or city)
    for acc in index.by_state.get(saddr.state, []):
        if acc.id in seen or not acc.addr.is_usable or not saddr.is_usable:
            continue
        if acc.addr.number != saddr.number:
            continue
        same_locality = (saddr.zip5 and acc.addr.zip5 == saddr.zip5) or (acc.addr.city == saddr.city)
        if not same_locality:
            continue
        score = float(fuzz.ratio(acc.addr.street, saddr.street))
        if score >= STREET_FUZZY_MIN:
            c = _candidate(site, acc, TIER_MEDIUM_ADDRESS_FUZZY, ["ADDRESS_FUZZY", f"STREET_SIMILARITY_{int(score)}"], True, score)
            if c.phone_match:
                c.reasons.append("PHONE_MATCH")
            if c.admin_match:
                c.reasons.append("ADMIN_CONTACT_MATCH")
            add(c)

    # 3. name-similar accounts anywhere (transparency; never eligible on their own)
    for acc in index.accounts:
        if acc.id in seen:
            continue
        score = name_similarity(site.name, acc.name)
        phone_hit = bool(site.phone_norm) and site.phone_norm == acc.phone_norm
        if score < NAME_FUZZY_MIN and not phone_hit:
            continue
        reasons = []
        if score >= NAME_FUZZY_MIN:
            reasons.append("NAME_SIMILAR")
        if phone_hit:
            reasons.append("PHONE_MATCH")
        if acc.addr.state != saddr.state:
            reasons.append("STATE_MISMATCH")
        elif acc.addr.city != saddr.city:
            reasons.append("CITY_MISMATCH")
        if acc.addr.number != saddr.number or acc.addr.street != saddr.street:
            reasons.append("STREET_MISMATCH")
        reasons.append("NOT_ELIGIBLE_ADDRESS_CONTRADICTS")
        add(_candidate(site, acc, TIER_LOW_NAME_ONLY, reasons, False, float(fuzz.ratio(acc.addr.street, saddr.street))))

    return out


def classify(site: SiteFacility, candidates: list[Candidate]) -> tuple[str, list[str]]:
    high = [c for c in candidates if c.eligible and c.tier == TIER_HIGH_ADDRESS_EXACT]
    medium = [c for c in candidates if c.eligible and c.tier == TIER_MEDIUM_ADDRESS_FUZZY]
    low = [c for c in candidates if not c.eligible]
    reasons: list[str] = []
    if high:
        if len(high) > 1:
            reasons.append("MULTIPLE_ACCOUNTS_SAME_ADDRESS")
        return TIER_HIGH_ADDRESS_EXACT, reasons
    if medium:
        if len(medium) > 1:
            return TIER_AMBIGUOUS, ["MULTIPLE_FUZZY_ADDRESS_CANDIDATES"]
        return TIER_MEDIUM_ADDRESS_FUZZY, ["STREET_SPELLING_DIFFERS"]
    if low:
        same_state = [c for c in low if "STATE_MISMATCH" not in c.reasons]
        if same_state:
            return TIER_LOW_NAME_ONLY, ["NAME_MATCH_ADDRESS_CONFLICT"]
        return TIER_NO_CANDIDATE, ["NAME_MATCH_OTHER_STATE_ONLY"]
    return TIER_NO_CANDIDATE, ["NO_CRM_CANDIDATE"]


def match_all(facilities: list[SiteFacility], accounts: list[CRMAccount]) -> list[MatchResult]:
    index = CRMIndex(accounts)
    results: list[MatchResult] = []
    claims: dict[str, list[str]] = defaultdict(list)  # account id -> site keys claiming it (eligible)
    for site in facilities:
        cands = find_candidates(site, index)
        tier, reasons = classify(site, cands)
        results.append(MatchResult(site=site, candidates=cands, tier=tier, reason_codes=reasons))
        for c in cands:
            if c.eligible:
                claims[c.account_id].append(site.site_key)
    # An account claimed by two different site facilities is contested.
    for r in results:
        for c in r.eligible:
            others = [s for s in claims[c.account_id] if s != r.site.site_key]
            if others:
                c.reasons.append("CRM_ACCOUNT_CONTESTED")
                if "CRM_ACCOUNT_CONTESTED" not in r.reason_codes:
                    r.reason_codes.append("CRM_ACCOUNT_CONTESTED")
    return results


def summarize_candidates(results: list[MatchResult]) -> dict[str, Any]:
    tiers: dict[str, int] = defaultdict(int)
    for r in results:
        tiers[r.tier] += 1
    return dict(tiers)
