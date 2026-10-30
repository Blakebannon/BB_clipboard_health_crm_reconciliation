"""The ONLY code path that mutates the CRM.

``apply_proposal`` is called from the Streamlit review app when the reviewer
clicks *Approve & apply* on one specific proposal. It:

1. atomically claims the proposal in SQLite (double-click / second tab safe),
2. re-fetches every affected account and refuses if guarded fields changed
   since the proposal was generated (``PRECONDITION_CHANGED``),
3. re-evaluates the CHOW rule on the *fresh* financials,
4. performs the minimal, step-ledgered writes (never retrying a POST blindly),
5. GETs everything back and verifies the exact intended state,
6. records applied / failed / needs_recovery with full detail.

Recovery: if a previous attempt died after creating a successor but before
linking it, the step ledger, the old account's ``chow_current_account`` and a
note marker on the successor are all consulted before any new POST.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from . import config
from .crm_api import CRMClient, CRMError, CRMNotFound, CRMUncertainWrite, record_id
from .models import (
    ACTION_CHOW_SUCCESSOR,
    ACTION_CREATE_MISSING,
    ACTION_DUPLICATE,
    ACTION_MISSING_FROM_SITE,
    ACTION_REPARENT,
    ACTION_UPDATE_FIELDS,
    APPLY_APPLIED,
    APPLY_BLOCKED,
    APPLY_FAILED,
    APPLY_NEEDS_RECOVERY,
    SNAPSHOT_TO_API,
    CRMAccount,
    Proposal,
)
from .rules import UnsafeFinancials, chow_marker, requires_chow
from .store import AlreadyClaimed, Store

# Fields compared between the proposal-time snapshot and the fresh GET.
GUARDED_FIELDS = (
    "name",
    "parent_id",
    "status",
    "care_type",
    "phone",
    "street",
    "city",
    "state",
    "zip",
    "lifetime_revenue",
    "outstanding_ar",
    "chow_current_account",
    "duplicate_of_account",
    "note",
)


class ApplyError(Exception):
    def __init__(self, code: str, message: str, status: str = APPLY_FAILED):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


@dataclass
class ApplyResult:
    proposal_key: str
    status: str
    message: str
    writes: list[dict[str, Any]] = field(default_factory=list)
    verified: dict[str, Any] = field(default_factory=dict)
    created_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == APPLY_APPLIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_key": self.proposal_key,
            "status": self.status,
            "message": self.message,
            "writes": self.writes,
            "verified": self.verified,
            "created_id": self.created_id,
        }


def to_api_patch(changes: dict[str, Any]) -> dict[str, Any]:
    """Snapshot field names -> API field names; drops anything not writable."""
    patch: dict[str, Any] = {}
    for k, v in changes.items():
        api = SNAPSHOT_TO_API.get(k)
        if api is None or api not in config.ALLOWED_WRITE_FIELDS:
            raise ApplyError("UNSUPPORTED_FIELD", f"field {k!r} is not writable via this tool")
        patch[api] = v
    return patch


def _fetch(client: CRMClient, account_id: str) -> CRMAccount:
    return CRMAccount.from_api(client.get_account(account_id))


def _diff_snapshot(expected: dict[str, Any], actual: dict[str, Any], fields: tuple[str, ...] = GUARDED_FIELDS) -> dict[str, tuple[Any, Any]]:
    out: dict[str, tuple[Any, Any]] = {}
    for f in fields:
        e, a = expected.get(f), actual.get(f)
        if f in ("lifetime_revenue", "outstanding_ar"):
            e = None if e is None else str(e).rstrip("0").rstrip(".") if "." in str(e) else str(e)
            a = None if a is None else str(a).rstrip("0").rstrip(".") if "." in str(a) else str(a)
        if (e or None) != (a or None):
            out[f] = (expected.get(f), actual.get(f))
    return out


def _verify_fields(client: CRMClient, account_id: str, intended: dict[str, Any]) -> tuple[CRMAccount, dict[str, tuple[Any, Any]]]:
    fresh = _fetch(client, account_id)
    mism = _diff_snapshot(intended, fresh.snapshot(), tuple(intended.keys()))
    return fresh, mism


class Applier:
    def __init__(self, store: Store, client: CRMClient, bellhaven_parent_id: str) -> None:
        if not client.allow_writes:
            raise ApplyError("CLIENT_READ_ONLY", "apply requires a client constructed with allow_writes=True")
        self.store = store
        self.client = client
        self.bell = bellhaven_parent_id

    # --- public ----------------------------------------------------------------------

    def apply(self, key: str, reviewer_note: str = "") -> ApplyResult:
        proposal = self.store.get_proposal(key)
        if proposal is None:
            raise KeyError(key)
        if not proposal.is_applicable:
            raise ApplyError("NOT_APPLICABLE", f"{proposal.action_type} has no CRM mutation to apply")
        try:
            self.store.claim_for_apply(key, reviewer_note)
        except AlreadyClaimed as exc:
            return ApplyResult(key, APPLY_BLOCKED, f"not applied: {exc}")

        result = ApplyResult(key, APPLY_FAILED, "")
        try:
            self._verify_target_parent()
            if proposal.action_type == ACTION_CHOW_SUCCESSOR:
                self._apply_chow(proposal, result)
            elif proposal.action_type == ACTION_CREATE_MISSING:
                self._apply_create(proposal, result)
            elif proposal.action_type in (ACTION_REPARENT, ACTION_UPDATE_FIELDS, ACTION_DUPLICATE, ACTION_MISSING_FROM_SITE):
                self._apply_patch(proposal, result)
            else:  # pragma: no cover
                raise ApplyError("NOT_APPLICABLE", proposal.action_type)
            result.status = APPLY_APPLIED
            self.store.finish_apply(key, APPLY_APPLIED, None, result.to_dict())
        except ApplyError as exc:
            result.status = exc.status
            result.message = str(exc)
            self.store.finish_apply(key, exc.status, str(exc), result.to_dict(), reset_to_pending=(exc.status == APPLY_BLOCKED))
        except CRMUncertainWrite as exc:
            result.status = APPLY_NEEDS_RECOVERY
            result.message = f"UNCERTAIN_WRITE: {exc}. Remote state unknown; click Approve & apply again to reconcile (no blind re-POST)."
            self.store.finish_apply(key, APPLY_NEEDS_RECOVERY, result.message, result.to_dict())
        except CRMError as exc:
            result.status = APPLY_FAILED
            result.message = f"CRM_ERROR: {exc}"
            self.store.finish_apply(key, APPLY_FAILED, result.message, result.to_dict())
        return result

    # --- preflight ----------------------------------------------------------------------

    def _verify_target_parent(self) -> None:
        try:
            parent = _fetch(self.client, self.bell)
        except CRMNotFound as exc:
            raise ApplyError("TARGET_PARENT_MISSING", f"Bellhaven parent {self.bell} not found: {exc}") from exc
        if parent.is_inactive:
            raise ApplyError("TARGET_PARENT_INACTIVE", f"Bellhaven parent {self.bell} is Inactive")

    def _preflight(self, proposal: Proposal, account_id: str, *, ignore: tuple[str, ...] = ()) -> CRMAccount:
        before = proposal.before.get(account_id)
        if before is None:
            raise ApplyError("NO_SNAPSHOT", f"proposal has no before-snapshot for {account_id}")
        fresh = _fetch(self.client, account_id)
        diff = _diff_snapshot(before, fresh.snapshot())
        for f in ignore:
            diff.pop(f, None)
        if diff:
            pretty = "; ".join(f"{k}: proposal saw {a!r}, CRM now {b!r}" for k, (a, b) in diff.items())
            raise ApplyError(
                "PRECONDITION_CHANGED",
                f"account {account_id} changed since the proposal was generated ({pretty}). Rescan and re-review.",
                status=APPLY_BLOCKED,
            )
        return fresh

    def _recheck_chow(self, fresh: CRMAccount, expected: bool) -> None:
        try:
            now = requires_chow(fresh, self.bell)
        except UnsafeFinancials as exc:
            raise ApplyError("FINANCIALS_UNSAFE", str(exc), status=APPLY_BLOCKED) from exc
        if now != expected:
            raise ApplyError(
                "CHOW_RULE_CHANGED",
                f"fresh financials say requires_chow={now} but proposal assumed {expected} "
                f"(lifetime_revenue={fresh.lifetime_revenue}, outstanding_ar={fresh.outstanding_ar}, parent={fresh.parent_id}). Rescan.",
                status=APPLY_BLOCKED,
            )

    # --- simple PATCH actions ---------------------------------------------------------

    def _apply_patch(self, proposal: Proposal, result: ApplyResult) -> None:
        step = proposal.steps[0]
        account_id = step.account_id
        assert account_id
        fresh = self._preflight(proposal, account_id)
        if proposal.action_type == ACTION_REPARENT:
            self._recheck_chow(fresh, expected=False)
            if step.payload.get("parent_id") != self.bell:
                raise ApplyError("TARGET_MISMATCH", "re-parent target is not the verified Bellhaven parent")
        if proposal.action_type == ACTION_DUPLICATE:
            survivor_id = step.payload.get("duplicate_of_account")
            survivor = _fetch(self.client, str(survivor_id))
            if survivor.is_inactive or (survivor.parent_id or "") != self.bell:
                raise ApplyError("SURVIVOR_INVALID", f"survivor {survivor_id} is inactive or not under Bellhaven anymore", status=APPLY_BLOCKED)
            if (fresh.lifetime_revenue or 0) > 0 or (fresh.outstanding_ar or 0) > 0:
                raise ApplyError("LOSER_HAS_BILLING", "losing account now carries billing history; manual decision required", status=APPLY_BLOCKED)
        # minimal patch: only fields that still differ from the intended state
        intended = {k: v for k, v in step.payload.items()}
        snap = fresh.snapshot()
        minimal = {k: v for k, v in intended.items() if (snap.get(k) or None) != (v or None)}
        if not minimal:
            # already in the intended state (e.g. applied by a previous crashed attempt) -> verify only
            result.message = "no-op: CRM already in intended state"
        else:
            patch = to_api_patch(minimal)
            sid = self.store.record_step(proposal.proposal_key, "patch", "in_progress", account_id=account_id, payload=patch)
            try:
                resp = self.client.update_account(account_id, patch)
            except CRMUncertainWrite as exc:
                self.store.update_step(sid, "uncertain", error=str(exc))
                # PATCH is idempotent for these fields: verify and finish if it landed
                verified, mism = _verify_fields(self.client, account_id, intended)
                if mism:
                    raise
                self.store.update_step(sid, "done_after_uncertain", response=verified.snapshot())
            except CRMError as exc:
                self.store.update_step(sid, "failed", error=str(exc))
                raise
            else:
                self.store.update_step(sid, "done", response=resp)
            result.writes.append({"method": "PATCH", "account_id": account_id, "payload": patch})
        verified, mism = _verify_fields(self.client, account_id, intended)
        vsid = self.store.record_step(proposal.proposal_key, "verify", "done" if not mism else "failed", account_id=account_id, response=verified.snapshot(), error=json.dumps(mism, default=str) if mism else None)
        if mism:
            raise ApplyError("VERIFY_MISMATCH", f"read-back of {account_id} does not match intended state: {mism}")
        result.verified[account_id] = verified.snapshot()
        if not result.message:
            result.message = f"PATCH applied and verified on {account_id}"

    # --- CREATE_MISSING -----------------------------------------------------------------

    def _existing_created(self, proposal: Proposal, marker: str, payload: dict[str, Any]) -> str | None:
        """Find an account this proposal already created (ledger, then remote search by marker/address)."""
        for s in self.store.steps(proposal.proposal_key):
            if s["step_name"] == "create" and s["resulting_id"]:
                return str(s["resulting_id"])
        # remote search: same parent + same street; confirm by marker in note or exact address
        for acc_raw in self.client.iter_all_accounts(parent_id=self.bell, street=payload.get("street", "")):
            acc = CRMAccount.from_api(acc_raw)
            if marker in (acc.note or ""):
                return acc.id
        # broader: any Bellhaven child with the marker (street filter may be unsupported)
        for acc_raw in self.client.iter_all_accounts(parent_id=self.bell):
            acc = CRMAccount.from_api(acc_raw)
            if marker in (acc.note or ""):
                return acc.id
        return None

    def _create(self, proposal: Proposal, payload_snapshot: dict[str, Any], marker: str, result: ApplyResult) -> str:
        existing = self._existing_created(proposal, marker, payload_snapshot)
        if existing:
            self.store.record_step(proposal.proposal_key, "create", "reused_existing", resulting_id=existing)
            result.message = f"reused existing account {existing} (found via ledger/marker); no new POST"
            return existing
        api_payload = to_api_patch(payload_snapshot)
        sid = self.store.record_step(proposal.proposal_key, "create", "in_progress", payload=api_payload)
        try:
            resp = self.client.create_account(api_payload)
        except CRMUncertainWrite as exc:
            self.store.update_step(sid, "uncertain", error=str(exc))
            found = self._existing_created(proposal, marker, payload_snapshot)
            if found:
                self.store.update_step(sid, "done_after_uncertain", resulting_id=found)
                result.writes.append({"method": "POST", "payload": api_payload, "created_id": found, "note": "confirmed after uncertain response"})
                return found
            raise
        except CRMError as exc:
            self.store.update_step(sid, "failed", error=str(exc))
            raise
        new_id = record_id(resp)
        if not new_id:
            self.store.update_step(sid, "uncertain", response=resp, error="create response had no id")
            found = self._existing_created(proposal, marker, payload_snapshot)
            if not found:
                raise CRMUncertainWrite("create returned no id and successor not found by marker")
            new_id = found
        self.store.update_step(sid, "done", resulting_id=new_id, response=resp)
        result.writes.append({"method": "POST", "payload": api_payload, "created_id": new_id})
        return new_id

    def _apply_create(self, proposal: Proposal, result: ApplyResult) -> None:
        payload = proposal.after["NEW"]
        marker = f"created from {proposal.site['source_url']}" if proposal.site else config.NOTE_TAG
        new_id = self._create(proposal, payload, marker, result)
        result.created_id = new_id
        verified, mism = _verify_fields(self.client, new_id, {k: v for k, v in payload.items() if k in ("name", "parent_id", "street", "city", "state", "zip")})
        self.store.record_step(proposal.proposal_key, "verify", "done" if not mism else "failed", account_id=new_id, response=verified.snapshot(), error=json.dumps(mism, default=str) if mism else None)
        if mism:
            raise ApplyError("VERIFY_MISMATCH", f"created account {new_id} does not match intended fields: {mism}")
        result.verified[new_id] = verified.snapshot()
        result.message = result.message or f"created {new_id} and verified"

    # --- CHOW ---------------------------------------------------------------------------

    def _apply_chow(self, proposal: Proposal, result: ApplyResult) -> None:
        old_id = proposal.crm_account_ids[0]
        payload = proposal.after["NEW"]
        marker = chow_marker(old_id)

        # Preflight on the OLD account. chow_current_account may already be set if a prior
        # attempt crashed after linking -- that is handled below, so it is excluded here.
        fresh_old = self._preflight(proposal, old_id, ignore=("chow_current_account",))
        self._recheck_chow(fresh_old, expected=True)

        # 1. Successor: already linked? already created (ledger / marker)? else create.
        successor_id: str | None = None
        if fresh_old.chow_current_account:
            linked = _fetch(self.client, fresh_old.chow_current_account)
            if (linked.parent_id or "") == self.bell:
                successor_id = linked.id
                self.store.record_step(proposal.proposal_key, "create", "reused_linked", resulting_id=successor_id)
                result.message = f"old account already linked to successor {successor_id}; verifying only"
            else:
                raise ApplyError("CHOW_LINK_UNEXPECTED", f"old account already links to {linked.id} which is not under Bellhaven", status=APPLY_BLOCKED)
        if successor_id is None:
            successor_id = self._create(proposal, payload, marker, result)
        result.created_id = successor_id

        # 2. Link: PATCH only chow_current_account on the old account (if not already).
        if fresh_old.chow_current_account != successor_id:
            patch = to_api_patch({"chow_current_account": successor_id})
            sid = self.store.record_step(proposal.proposal_key, "link", "in_progress", account_id=old_id, resulting_id=successor_id, payload=patch)
            try:
                resp = self.client.update_account(old_id, patch)
            except CRMUncertainWrite as exc:
                self.store.update_step(sid, "uncertain", error=str(exc))
                check = _fetch(self.client, old_id)
                if check.chow_current_account != successor_id:
                    raise
                self.store.update_step(sid, "done_after_uncertain", response=check.snapshot())
            except CRMError as exc:
                self.store.update_step(sid, "failed", error=str(exc))
                raise
            else:
                self.store.update_step(sid, "done", response=resp)
            result.writes.append({"method": "PATCH", "account_id": old_id, "payload": patch})

        # 3. Verify both sides.
        old_after = _fetch(self.client, old_id)
        before = proposal.before[old_id]
        untouched = _diff_snapshot(before, old_after.snapshot())
        untouched.pop("chow_current_account", None)
        problems: dict[str, Any] = {}
        if untouched:
            problems["old_account_changed_unexpectedly"] = untouched
        if old_after.chow_current_account != successor_id:
            problems["old_chow_link"] = (successor_id, old_after.chow_current_account)
        new_after, mism = _verify_fields(self.client, successor_id, {k: v for k, v in payload.items() if k in ("name", "parent_id", "street", "city", "state", "zip")})
        if mism:
            problems["successor_fields"] = mism
        self.store.record_step(
            proposal.proposal_key,
            "verify",
            "done" if not problems else "failed",
            account_id=old_id,
            resulting_id=successor_id,
            response={"old": old_after.snapshot(), "new": new_after.snapshot()},
            error=json.dumps(problems, default=str) if problems else None,
        )
        if problems:
            raise ApplyError("VERIFY_MISMATCH", f"post-CHOW verification failed: {problems}")
        result.verified[old_id] = old_after.snapshot()
        result.verified[successor_id] = new_after.snapshot()
        if not result.message or result.message.startswith("reused"):
            result.message = (
                f"CHOW complete: successor {successor_id} under Bellhaven; old {old_id} unchanged "
                f"(parent={old_after.parent_id}, revenue={old_after.lifetime_revenue}, AR={old_after.outstanding_ar}) "
                f"and chow_current_account -> {successor_id}"
            )


def apply_proposal(store: Store, client: CRMClient, bellhaven_parent_id: str, key: str, reviewer_note: str = "") -> ApplyResult:
    """Entry point used by the review app. Requires a writable client."""
    return Applier(store, client, bellhaven_parent_id).apply(key, reviewer_note)
