"""In-memory stand-in for ``CRMClient`` used by the tests.

It mirrors the real client's contract: read-only unless ``allow_writes=True``,
a ``write_log`` of every attempted write, POST/PATCH that can be made to time
out *after* the server has applied the change (the dangerous case), and a
``crash_after_create`` hook to simulate a process dying between the successor
POST and the link PATCH.
"""
from __future__ import annotations

import copy
import itertools
from typing import Any, Iterator

from bellhaven import config
from bellhaven.crm_api import (
    CRMNotFound,
    CRMUncertainWrite,
    CRMValidationError,
    WriteRecord,
    WritesDisabledError,
)


class CrashSimulated(RuntimeError):
    pass


class FakeCRMClient:
    def __init__(self, accounts: list[dict[str, Any]], contacts: list[dict[str, Any]] | None = None, allow_writes: bool = False, page_size: int = 50):
        self.accounts: dict[str, dict[str, Any]] = {str(a["id"]): copy.deepcopy(a) for a in accounts}
        self.contacts: list[dict[str, Any]] = copy.deepcopy(contacts or [])
        self.allow_writes = allow_writes
        self.page_size = page_size
        self.write_log: list[WriteRecord] = []
        self.get_count = 0
        self._ids = itertools.count(1)
        # fault injection
        self.timeout_after_create = False  # server applies the POST, client sees a timeout
        self.timeout_before_create = False  # nothing applied, client sees a timeout
        self.crash_after_create = False  # raise CrashSimulated after a successful POST (process dies)
        self.timeout_after_patch = False
        self.valid_statuses: set[str] | None = None  # None = accept anything

    # --- reads ---------------------------------------------------------------

    def me(self) -> dict[str, Any]:
        return {"candidate": "test"}

    def _filtered(self, **filters: str) -> list[dict[str, Any]]:
        out = []
        for a in self.accounts.values():
            ok = True
            for k, v in filters.items():
                if not v:
                    continue
                if k == "q":
                    if v.lower() not in str(a.get(config.FIELD_NAME, "")).lower():
                        ok = False
                elif k == "street":
                    if v.lower() not in str(a.get(config.FIELD_STREET, "")).lower():
                        ok = False
                elif str(a.get(k if k != "zip" else config.FIELD_ZIP, "") or "") != v:
                    ok = False
            if ok:
                out.append(copy.deepcopy(a))
        return out

    def list_accounts(self, page: int = 1, page_size: int | None = None, **filters: str) -> dict[str, Any]:
        items = self._filtered(**filters)
        ps = page_size or self.page_size
        self.get_count += 1
        return {"items": items[(page - 1) * ps : page * ps], "total": len(items), "page": page, "page_size": ps}

    def iter_all_accounts(self, **filters: str) -> Iterator[dict[str, Any]]:
        self.get_count += 1
        for a in self._filtered(**filters):
            yield a

    def iter_all_contacts(self, **filters: str) -> Iterator[dict[str, Any]]:
        self.get_count += 1
        for c in self.contacts:
            if filters.get("account_id") and c.get("account_id") != filters["account_id"]:
                continue
            yield copy.deepcopy(c)

    def get_account(self, account_id: str) -> dict[str, Any]:
        self.get_count += 1
        if account_id not in self.accounts:
            raise CRMNotFound(f"GET accounts/{account_id} -> 404", 404)
        return copy.deepcopy(self.accounts[account_id])

    @staticmethod
    def extract_items(payload: Any) -> list[dict[str, Any]]:
        return list(payload["items"]) if isinstance(payload, dict) else list(payload)

    @property
    def successful_writes(self) -> int:
        return sum(1 for w in self.write_log if w.ok)

    # --- writes ---------------------------------------------------------------

    def _guard(self, method: str, path: str, payload: dict[str, Any]) -> WriteRecord:
        bad = set(payload) & set(config.FORBIDDEN_WRITE_FIELDS)
        if bad:
            raise CRMValidationError(f"forbidden fields {sorted(bad)}")
        unknown = set(payload) - set(config.ALLOWED_WRITE_FIELDS)
        if unknown:
            raise CRMValidationError(f"unsupported fields {sorted(unknown)}")
        rec = WriteRecord(method, path, dict(payload), None, False)
        self.write_log.append(rec)
        if not self.allow_writes:
            raise WritesDisabledError(f"{method} {path} refused: read-only client")
        if self.valid_statuses is not None and config.FIELD_STATUS in payload and payload[config.FIELD_STATUS] not in self.valid_statuses:
            rec.status = 422
            raise CRMValidationError(f"{method} {path} -> HTTP 422: invalid status {payload[config.FIELD_STATUS]!r}", 422)
        return rec

    def create_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        rec = self._guard("POST", "accounts", payload)
        if self.timeout_before_create:
            self.timeout_before_create = False
            raise CRMUncertainWrite("POST accounts: timeout (nothing applied)")
        new_id = f"001FAKE{next(self._ids):010d}"
        record = {config.FIELD_ID: new_id, config.FIELD_REVENUE: 0, config.FIELD_AR: 0, config.FIELD_CHOW: None, config.FIELD_DUP: None, config.FIELD_NOTE: ""}
        record.update(payload)
        self.accounts[new_id] = record
        rec.status, rec.ok = 201, True
        if self.timeout_after_create:
            self.timeout_after_create = False
            raise CRMUncertainWrite("POST accounts: timeout (server applied it)")
        if self.crash_after_create:
            self.crash_after_create = False
            raise CrashSimulated("process died after POST, before link")
        return copy.deepcopy(record)

    def update_account(self, account_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        rec = self._guard("PATCH", f"accounts/{account_id}", patch)
        if account_id not in self.accounts:
            rec.status = 404
            raise CRMNotFound(f"PATCH accounts/{account_id} -> 404", 404)
        self.accounts[account_id].update(patch)
        rec.status, rec.ok = 200, True
        if self.timeout_after_patch:
            self.timeout_after_patch = False
            raise CRMUncertainWrite("PATCH: timeout (server applied it)")
        return copy.deepcopy(self.accounts[account_id])
