"""Typed records shared across scraper, matcher, rules, store and apply."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from . import config
from .normalize import (
    AddressKey,
    address_key,
    norm_name,
    norm_phone,
    parse_money,
)

# --- enumerations -------------------------------------------------------------

ACTION_NO_CHANGE = "NO_CHANGE"
ACTION_UPDATE_FIELDS = "UPDATE_FIELDS"
ACTION_REPARENT = "REPARENT"
ACTION_CHOW_SUCCESSOR = "CHOW_SUCCESSOR"
ACTION_CREATE_MISSING = "CREATE_MISSING"
ACTION_DUPLICATE = "DUPLICATE"
ACTION_MISSING_FROM_SITE = "MISSING_FROM_SITE"
ACTION_MANUAL_REVIEW = "MANUAL_REVIEW"

ALL_ACTIONS = (
    ACTION_NO_CHANGE,
    ACTION_UPDATE_FIELDS,
    ACTION_REPARENT,
    ACTION_CHOW_SUCCESSOR,
    ACTION_CREATE_MISSING,
    ACTION_DUPLICATE,
    ACTION_MISSING_FROM_SITE,
    ACTION_MANUAL_REVIEW,
)

# Actions that have a CRM mutation to apply on approval.
APPLICABLE_ACTIONS = frozenset(
    {
        ACTION_UPDATE_FIELDS,
        ACTION_REPARENT,
        ACTION_CHOW_SUCCESSOR,
        ACTION_CREATE_MISSING,
        ACTION_DUPLICATE,
        ACTION_MISSING_FROM_SITE,
    }
)

TIER_HIGH_ADDRESS_EXACT = "HIGH_ADDRESS_EXACT"
TIER_MEDIUM_ADDRESS_FUZZY = "MEDIUM_ADDRESS_FUZZY"
TIER_LOW_NAME_ONLY = "LOW_NAME_ONLY"
TIER_AMBIGUOUS = "AMBIGUOUS_MULTI_CANDIDATE"
TIER_NO_CANDIDATE = "NO_CANDIDATE"

DECISION_PENDING = "pending"
DECISION_APPROVED = "approved"
DECISION_REJECTED = "rejected"
DECISION_ACKNOWLEDGED = "acknowledged"  # for MANUAL_REVIEW / NO_CHANGE: reviewed, no CRM change

APPLY_NONE = "none"
APPLY_IN_PROGRESS = "in_progress"
APPLY_APPLIED = "applied"
APPLY_FAILED = "failed"
APPLY_BLOCKED = "blocked"  # preflight refused; needs refreshed review
APPLY_NEEDS_RECOVERY = "needs_recovery"  # uncertain remote state; rerun apply to reconcile


# --- website -------------------------------------------------------------------


@dataclass
class SiteFacility:
    name: str
    street: str
    city: str
    state: str
    zip: str
    care_offerings: list[str]
    source_url: str
    scraped_at: str
    phone: str = ""
    administrator: str = ""
    slug: str = ""
    discovered_via: list[str] = field(default_factory=list)  # e.g. ["directory:p2", "homepage"]

    @property
    def addr(self) -> AddressKey:
        return address_key(self.street, self.city, self.state, self.zip)

    @property
    def name_norm(self) -> str:
        return norm_name(self.name)

    @property
    def phone_norm(self) -> str:
        return norm_phone(self.phone)

    @property
    def site_key(self) -> str:
        """Stable identity for proposal keys: canonical URL path."""
        return self.slug or self.source_url

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SiteFacility":
        return cls(**{k: d.get(k, "" if k not in {"care_offerings", "discovered_via"} else []) for k in cls.__dataclass_fields__})


# --- CRM -----------------------------------------------------------------------


def _pick(raw: dict[str, Any], canonical: str):
    for alias in config.FIELD_ALIASES.get(canonical, (canonical,)):
        if alias in raw:
            return raw[alias]
    return None


@dataclass
class CRMAccount:
    id: str
    name: str
    parent_id: str | None
    status: str
    care_type: str
    phone: str
    street: str
    city: str
    state: str
    zip: str
    lifetime_revenue_raw: Any
    outstanding_ar_raw: Any
    chow_current_account: str | None
    duplicate_of_account: str | None
    note: str
    raw: dict[str, Any] = field(default_factory=dict)
    contacts: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "CRMAccount":
        def s(canonical: str) -> str:
            v = _pick(raw, canonical)
            return "" if v is None else str(v)

        def opt(canonical: str) -> str | None:
            v = _pick(raw, canonical)
            if v is None:
                return None
            v = str(v).strip()
            return v or None

        return cls(
            id=s(config.FIELD_ID),
            name=s(config.FIELD_NAME),
            parent_id=opt(config.FIELD_PARENT),
            status=s(config.FIELD_STATUS),
            care_type=s(config.FIELD_CARE_TYPE),
            phone=s(config.FIELD_PHONE),
            street=s(config.FIELD_STREET),
            city=s(config.FIELD_CITY),
            state=s(config.FIELD_STATE),
            zip=s(config.FIELD_ZIP),
            lifetime_revenue_raw=_pick(raw, config.FIELD_REVENUE),
            outstanding_ar_raw=_pick(raw, config.FIELD_AR),
            chow_current_account=opt(config.FIELD_CHOW),
            duplicate_of_account=opt(config.FIELD_DUP),
            note=s(config.FIELD_NOTE),
            raw=dict(raw),
        )

    @property
    def lifetime_revenue(self) -> Decimal | None:
        return parse_money(self.lifetime_revenue_raw)

    @property
    def outstanding_ar(self) -> Decimal | None:
        return parse_money(self.outstanding_ar_raw)

    @property
    def financials_known(self) -> bool:
        return self.lifetime_revenue is not None and self.outstanding_ar is not None

    @property
    def addr(self) -> AddressKey:
        return address_key(self.street, self.city, self.state, self.zip)

    @property
    def name_norm(self) -> str:
        return norm_name(self.name)

    @property
    def phone_norm(self) -> str:
        return norm_phone(self.phone)

    @property
    def is_inactive(self) -> bool:
        return self.status.strip().lower() == config.STATUS_INACTIVE.lower()

    def snapshot(self) -> dict[str, Any]:
        """The guarded view of this account used for before/after and preflight."""
        return {
            "id": self.id,
            "name": self.name,
            "parent_id": self.parent_id,
            "status": self.status,
            "care_type": self.care_type,
            "phone": self.phone,
            "street": self.street,
            "city": self.city,
            "state": self.state,
            "zip": self.zip,
            "lifetime_revenue": None if self.lifetime_revenue is None else str(self.lifetime_revenue),
            "outstanding_ar": None if self.outstanding_ar is None else str(self.outstanding_ar),
            "chow_current_account": self.chow_current_account,
            "duplicate_of_account": self.duplicate_of_account,
            "note": self.note,
        }

    def to_dict(self) -> dict[str, Any]:
        d = self.snapshot()
        d["raw"] = self.raw
        d["contacts"] = self.contacts
        return d


# Map from snapshot/proposal field names to the real API field names.
SNAPSHOT_TO_API = {
    "name": config.FIELD_NAME,
    "parent_id": config.FIELD_PARENT,
    "status": config.FIELD_STATUS,
    "care_type": config.FIELD_CARE_TYPE,
    "phone": config.FIELD_PHONE,
    "street": config.FIELD_STREET,
    "city": config.FIELD_CITY,
    "state": config.FIELD_STATE,
    "zip": config.FIELD_ZIP,
    "chow_current_account": config.FIELD_CHOW,
    "duplicate_of_account": config.FIELD_DUP,
    "note": config.FIELD_NOTE,
}
API_TO_SNAPSHOT = {v: k for k, v in SNAPSHOT_TO_API.items()}


# --- matching ------------------------------------------------------------------


@dataclass
class Candidate:
    account_id: str
    name: str
    parent_id: str | None
    tier: str
    reasons: list[str]
    address: str
    status: str
    lifetime_revenue: str | None
    outstanding_ar: str | None
    chow_current_account: str | None
    duplicate_of_account: str | None
    name_score: float
    street_score: float
    phone_match: bool
    admin_match: bool
    eligible: bool  # False => shown for transparency but excluded from automatic matching

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- proposals -----------------------------------------------------------------


@dataclass
class FieldChange:
    account_id: str
    field: str
    before: Any
    after: Any

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MutationStepPlan:
    """Human-readable description of one API step the apply path will run."""

    order: int
    kind: str  # "create" | "patch"
    account_id: str | None  # None for create (id unknown until created)
    payload: dict[str, Any]
    description: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Proposal:
    action_type: str
    tier: str
    reason_codes: list[str]
    title: str
    site: dict[str, Any] | None  # SiteFacility.to_dict() or None
    crm_account_ids: list[str]
    before: dict[str, dict[str, Any]]  # account_id -> snapshot
    after: dict[str, dict[str, Any]]  # account_id -> intended snapshot (or "NEW" for create)
    changes: list[FieldChange]
    steps: list[MutationStepPlan]
    candidates: list[Candidate]
    evidence: dict[str, Any]
    risk_notes: list[str]
    target_parent_id: str | None
    requires_chow: bool | None = None
    proposal_key: str = ""

    def __post_init__(self) -> None:
        if not self.proposal_key:
            self.proposal_key = self.compute_key()

    def material_outcome(self) -> dict[str, Any]:
        """The part of the proposal that, if changed, makes it a *different* proposal."""
        return {
            "action_type": self.action_type,
            "site_key": (self.site or {}).get("slug") or (self.site or {}).get("source_url"),
            "crm_account_ids": sorted(self.crm_account_ids),
            "target_parent_id": self.target_parent_id,
            # free-text notes are explanatory and excluded from identity
            "changes": sorted(
                (c.account_id, c.field, _json_safe(c.after)) for c in self.changes if c.field != "note"
            ),
            "create": [
                _json_safe({k: v for k, v in s.payload.items() if k != "note"}) for s in self.steps if s.kind == "create"
            ],
        }

    def compute_key(self) -> str:
        blob = json.dumps(self.material_outcome(), sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]

    @property
    def is_applicable(self) -> bool:
        return self.action_type in APPLICABLE_ACTIONS and bool(self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_key": self.proposal_key,
            "action_type": self.action_type,
            "tier": self.tier,
            "reason_codes": list(self.reason_codes),
            "title": self.title,
            "site": self.site,
            "crm_account_ids": list(self.crm_account_ids),
            "before": self.before,
            "after": self.after,
            "changes": [c.to_dict() for c in self.changes],
            "steps": [s.to_dict() for s in self.steps],
            "candidates": [c.to_dict() for c in self.candidates],
            "evidence": self.evidence,
            "risk_notes": list(self.risk_notes),
            "target_parent_id": self.target_parent_id,
            "requires_chow": self.requires_chow,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Proposal":
        return cls(
            action_type=d["action_type"],
            tier=d["tier"],
            reason_codes=list(d.get("reason_codes", [])),
            title=d.get("title", ""),
            site=d.get("site"),
            crm_account_ids=list(d.get("crm_account_ids", [])),
            before=d.get("before", {}),
            after=d.get("after", {}),
            changes=[FieldChange(**c) for c in d.get("changes", [])],
            steps=[MutationStepPlan(**s) for s in d.get("steps", [])],
            candidates=[Candidate(**c) for c in d.get("candidates", [])],
            evidence=d.get("evidence", {}),
            risk_notes=list(d.get("risk_notes", [])),
            target_parent_id=d.get("target_parent_id"),
            requires_chow=d.get("requires_chow"),
            proposal_key=d.get("proposal_key", ""),
        )


def _json_safe(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, dict):
        return {k: _json_safe(x) for k, x in sorted(v.items())}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    return v
