"""Authenticated CRM client.

Safety model
------------
* ``CRMClient`` is **read-only by default**. ``create_account`` and
  ``update_account`` raise ``WritesDisabledError`` unless the client was
  constructed with ``allow_writes=True``. Only ``bellhaven.apply`` constructs a
  writable client, and only inside an explicit reviewer-approval call.
* Every write attempt (even a refused one) is appended to ``write_log`` so
  tests and audits can prove the count.
* GETs are retried on timeouts / 429 / 5xx. POST and PATCH are **never**
  retried automatically: a timeout raises ``CRMUncertainWrite`` and the caller
  must reconcile against remote state before trying again.
* The bearer token is never logged, never placed in a URL, never in errors.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterator

import requests

from . import config

LIST_ITEM_KEYS = ("items", "results", "data", "accounts", "contacts")


class CRMError(Exception):
    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class CRMAuthError(CRMError):
    pass


class CRMNotFound(CRMError):
    pass


class CRMValidationError(CRMError):
    pass


class CRMUncertainWrite(CRMError):
    """A POST/PATCH did not return a definitive answer (timeout / connection drop).

    The remote state may or may not have changed. Callers must inspect remote
    state before retrying.
    """


class WritesDisabledError(CRMError):
    pass


@dataclass
class WriteRecord:
    method: str
    path: str
    payload: dict[str, Any]
    status: int | None
    ok: bool
    at: float = field(default_factory=time.time)


def record_id(item: Any) -> str:
    """Identifier of a list/detail/create record; the live API uses ``account_id`` / ``contact_id``."""
    if not isinstance(item, dict):
        return ""
    for k in (config.FIELD_ID, "id", "contact_id"):
        v = item.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


def _redact(text: str, token: str | None) -> str:
    if token and token in text:
        return text.replace(token, "<redacted>")
    return text


class CRMClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 20.0,
        page_size: int = 50,
        allow_writes: bool = False,
        session: requests.Session | None = None,
        max_get_retries: int = 3,
        polite_delay: float = 0.0,
    ) -> None:
        if not token or not token.strip():
            raise CRMAuthError("empty API token")
        self.api_base = base_url.rstrip("/")
        if not self.api_base.endswith("/api/v1"):
            self.api_base += "/api/v1"
        self._token = token.strip()
        self.timeout = timeout
        self.page_size = page_size
        self.allow_writes = allow_writes
        self.max_get_retries = max_get_retries
        self.polite_delay = polite_delay
        self.write_log: list[WriteRecord] = []
        self.get_count = 0
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "User-Agent": "bellhaven-reconciliation/1.0",
            }
        )

    def __repr__(self) -> str:
        return f"CRMClient(api_base={self.api_base!r}, allow_writes={self.allow_writes}, writes={len(self.write_log)})"

    # --- low level -------------------------------------------------------------

    def _url(self, path: str) -> str:
        return self.api_base + "/" + path.lstrip("/")

    def _raise_for(self, resp: requests.Response, method: str, path: str) -> None:
        if resp.status_code < 400:
            return
        try:
            body = resp.json()
        except ValueError:
            body = _redact(resp.text[:500], self._token)
        msg = f"{method} {path} -> HTTP {resp.status_code}: {body}"
        if resp.status_code in (401, 403):
            raise CRMAuthError(msg, resp.status_code, body)
        if resp.status_code == 404:
            raise CRMNotFound(msg, resp.status_code, body)
        if resp.status_code == 422:
            raise CRMValidationError(msg, resp.status_code, body)
        raise CRMError(msg, resp.status_code, body)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        last: Exception | None = None
        for attempt in range(self.max_get_retries):
            try:
                self.get_count += 1
                resp = self._session.get(self._url(path), params=params, timeout=self.timeout)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last = exc
                time.sleep(min(2.0, 0.5 * (2**attempt)))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else min(4.0, 0.5 * (2**attempt))
                last = CRMError(f"GET {path} -> HTTP {resp.status_code}", resp.status_code)
                time.sleep(delay)
                continue
            self._raise_for(resp, "GET", path)
            if self.polite_delay:
                time.sleep(self.polite_delay)
            return resp.json()
        raise CRMError(f"GET {path} failed after {self.max_get_retries} attempts: {_redact(str(last), self._token)}")

    def _write(self, method: str, path: str, payload: dict[str, Any]) -> Any:
        bad = set(payload) & set(config.FORBIDDEN_WRITE_FIELDS)
        if bad:
            raise CRMValidationError(f"refusing to write forbidden fields: {sorted(bad)}")
        unknown = set(payload) - set(config.ALLOWED_WRITE_FIELDS)
        if unknown:
            raise CRMValidationError(f"refusing to write unsupported fields: {sorted(unknown)}")
        if not self.allow_writes:
            self.write_log.append(WriteRecord(method, path, dict(payload), None, False))
            raise WritesDisabledError(
                f"{method} {path} refused: this client is read-only. Writes happen only via reviewer approval."
            )
        rec = WriteRecord(method, path, dict(payload), None, False)
        self.write_log.append(rec)
        try:
            resp = self._session.request(method, self._url(path), json=payload, timeout=self.timeout)
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise CRMUncertainWrite(
                f"{method} {path}: no definitive response ({type(exc).__name__}); remote state unknown"
            ) from exc
        rec.status = resp.status_code
        self._raise_for(resp, method, path)
        rec.ok = True
        try:
            return resp.json()
        except ValueError:
            return {}

    # --- helpers ---------------------------------------------------------------

    @staticmethod
    def extract_items(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [p for p in payload if isinstance(p, dict)]
        if isinstance(payload, dict):
            for k in LIST_ITEM_KEYS:
                v = payload.get(k)
                if isinstance(v, list):
                    return [p for p in v if isinstance(p, dict)]
        raise CRMError(f"unrecognized list response shape: {type(payload).__name__} keys={list(payload)[:8] if isinstance(payload, dict) else '-'}")

    @staticmethod
    def extract_total(payload: Any) -> int | None:
        if isinstance(payload, dict):
            for k in ("total", "count", "total_count"):
                v = payload.get(k)
                if isinstance(v, int):
                    return v
        return None

    # --- reads -----------------------------------------------------------------

    def me(self) -> dict[str, Any]:
        return self._get("me")

    def list_accounts(self, page: int = 1, page_size: int | None = None, **filters: str) -> Any:
        params: dict[str, Any] = {"page": page, "page_size": page_size or self.page_size}
        for k, v in filters.items():
            if v:
                params[k] = v
        return self._get("accounts", params)

    def iter_all_accounts(self, **filters: str) -> Iterator[dict[str, Any]]:
        yield from self._iter_pages("accounts", **filters)

    def iter_all_contacts(self, **filters: str) -> Iterator[dict[str, Any]]:
        yield from self._iter_pages("contacts", **filters)

    def _iter_pages(self, path: str, **filters: str) -> Iterator[dict[str, Any]]:
        page = 1
        seen_ids: set[str] = set()
        total: int | None = None
        yielded = 0
        while True:
            params: dict[str, Any] = {"page": page, "page_size": self.page_size}
            params.update({k: v for k, v in filters.items() if v})
            payload = self._get(path, params)
            items = self.extract_items(payload)
            if total is None:
                total = self.extract_total(payload)
            new = 0
            for item in items:
                item_id = record_id(item)
                if item_id and item_id in seen_ids:
                    continue  # server re-served a record (e.g. page past the end)
                if item_id:
                    seen_ids.add(item_id)
                new += 1
                yielded += 1
                yield item
            if not items or new == 0:
                break
            if total is not None and yielded >= total:
                break
            if len(items) < self.page_size:
                break
            page += 1
            if page > 10_000:  # loop guard
                raise CRMError("pagination exceeded 10000 pages")

    def get_account(self, account_id: str) -> dict[str, Any]:
        if not account_id:
            raise CRMValidationError("get_account: empty id")
        return self._get(f"accounts/{account_id}")

    def get_contact(self, contact_id: str) -> dict[str, Any]:
        return self._get(f"contacts/{contact_id}")

    # --- writes (guarded) --------------------------------------------------------

    def create_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._write("POST", "accounts", payload)

    def update_account(self, account_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        if not account_id:
            raise CRMValidationError("update_account: empty id")
        if not patch:
            raise CRMValidationError("update_account: empty patch")
        return self._write("PATCH", f"accounts/{account_id}", patch)

    @property
    def successful_writes(self) -> int:
        return sum(1 for w in self.write_log if w.ok)


def make_client(settings: config.Settings, allow_writes: bool = False) -> CRMClient:
    return CRMClient(
        settings.base_url,
        settings.require_token(),
        timeout=settings.http_timeout,
        page_size=settings.page_size,
        allow_writes=allow_writes,
    )
