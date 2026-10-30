"""SQLite decision ledger.

Tables
------
runs              one row per ``scan`` (immutable metadata + counts + completeness)
site_facilities   normalized website snapshot per run
crm_accounts      CRM snapshot per run (guarded fields + raw JSON)
proposals         one row per stable ``proposal_key``; decision + apply state live here
mutation_steps    durable step ledger for apply (create/patch/verify), incl. uncertain outcomes
audit             append-only log of reviewer actions and apply outcomes

Concurrency: approval uses an atomic conditional UPDATE as a claim, so a
double-click or two browser tabs cannot both start an apply. Network calls
are never made inside a transaction; the step ledger is the durable record.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import (
    APPLY_APPLIED,
    APPLY_BLOCKED,
    APPLY_FAILED,
    APPLY_IN_PROGRESS,
    APPLY_NEEDS_RECOVERY,
    APPLY_NONE,
    DECISION_ACKNOWLEDGED,
    DECISION_APPROVED,
    DECISION_PENDING,
    DECISION_REJECTED,
    CRMAccount,
    Proposal,
    SiteFacility,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    site_count INTEGER,
    crm_count INTEGER,
    bellhaven_parent_id TEXT,
    bellhaven_child_count INTEGER,
    scrape_ok INTEGER,
    error_summary TEXT,
    notes_json TEXT,
    proposal_counts_json TEXT
);
CREATE TABLE IF NOT EXISTS site_facilities (
    run_id TEXT NOT NULL,
    slug TEXT NOT NULL,
    data_json TEXT NOT NULL,
    PRIMARY KEY (run_id, slug)
);
CREATE TABLE IF NOT EXISTS crm_accounts (
    run_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    name TEXT,
    parent_id TEXT,
    status TEXT,
    data_json TEXT NOT NULL,
    PRIMARY KEY (run_id, account_id)
);
CREATE TABLE IF NOT EXISTS proposals (
    proposal_key TEXT PRIMARY KEY,
    action_type TEXT NOT NULL,
    tier TEXT NOT NULL,
    title TEXT NOT NULL,
    site_key TEXT,
    crm_account_ids_json TEXT NOT NULL,
    data_json TEXT NOT NULL,
    decision TEXT NOT NULL DEFAULT 'pending',
    decided_at TEXT,
    reviewer_note TEXT,
    apply_status TEXT NOT NULL DEFAULT 'none',
    claimed_at TEXT,
    applied_at TEXT,
    error TEXT,
    result_json TEXT,
    first_seen_run TEXT NOT NULL,
    last_seen_run TEXT NOT NULL,
    stale INTEGER NOT NULL DEFAULT 0,
    superseded_by TEXT,
    origin TEXT NOT NULL DEFAULT 'scan'
);
CREATE INDEX IF NOT EXISTS idx_proposals_site ON proposals(site_key);
CREATE INDEX IF NOT EXISTS idx_proposals_decision ON proposals(decision, apply_status);
CREATE TABLE IF NOT EXISTS mutation_steps (
    step_id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_key TEXT NOT NULL,
    step_name TEXT NOT NULL,
    crm_account_id TEXT,
    resulting_id TEXT,
    status TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    payload_json TEXT,
    response_json TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_steps_key ON mutation_steps(proposal_key);
CREATE TABLE IF NOT EXISTS audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    proposal_key TEXT,
    event TEXT NOT NULL,
    detail_json TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AlreadyClaimed(Exception):
    pass


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=10, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if str(self.path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(proposals)")}
        if "origin" not in cols:
            self._conn.execute("ALTER TABLE proposals ADD COLUMN origin TEXT NOT NULL DEFAULT 'scan'")

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # --- runs ---------------------------------------------------------------------

    def start_run(self) -> str:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        self._conn.execute("INSERT INTO runs(run_id, started_at) VALUES (?, ?)", (run_id, utcnow()))
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        site_count: int,
        crm_count: int,
        bellhaven_parent_id: str | None,
        bellhaven_child_count: int,
        scrape_ok: bool,
        error_summary: str,
        notes: list[str],
        proposal_counts: dict[str, int],
    ) -> None:
        self._conn.execute(
            """UPDATE runs SET completed_at=?, site_count=?, crm_count=?, bellhaven_parent_id=?, bellhaven_child_count=?,
               scrape_ok=?, error_summary=?, notes_json=?, proposal_counts_json=? WHERE run_id=?""",
            (
                utcnow(),
                site_count,
                crm_count,
                bellhaven_parent_id,
                bellhaven_child_count,
                1 if scrape_ok else 0,
                error_summary,
                json.dumps(notes),
                json.dumps(proposal_counts),
                run_id,
            ),
        )

    def last_successful_run(self) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM runs WHERE completed_at IS NOT NULL AND scrape_ok=1 ORDER BY started_at DESC LIMIT 1"
        ).fetchone()

    def latest_run(self) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM runs WHERE completed_at IS NOT NULL ORDER BY started_at DESC LIMIT 1").fetchone()

    def runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()

    # --- snapshots --------------------------------------------------------------

    def save_site_snapshot(self, run_id: str, facilities: list[SiteFacility]) -> None:
        with self.tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO site_facilities(run_id, slug, data_json) VALUES (?,?,?)",
                [(run_id, f.slug or f.source_url, json.dumps(f.to_dict())) for f in facilities],
            )

    def save_crm_snapshot(self, run_id: str, accounts: list[CRMAccount]) -> None:
        with self.tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO crm_accounts(run_id, account_id, name, parent_id, status, data_json) VALUES (?,?,?,?,?,?)",
                [(run_id, a.id, a.name, a.parent_id, a.status, json.dumps(a.to_dict(), default=str)) for a in accounts],
            )

    def crm_snapshot(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT data_json FROM crm_accounts WHERE run_id=?", (run_id,)).fetchall()
        return [json.loads(r["data_json"]) for r in rows]

    def site_snapshot(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT data_json FROM site_facilities WHERE run_id=?", (run_id,)).fetchall()
        return [json.loads(r["data_json"]) for r in rows]

    # --- proposals ---------------------------------------------------------------

    def upsert_proposals(self, run_id: str, proposals: list[Proposal]) -> dict[str, int]:
        """Insert new proposals; refresh evidence on existing ones; never touch decisions.

        Returns counts: new / existing / superseded / stale.
        """
        counts = {"new": 0, "existing": 0, "superseded": 0, "stale": 0}
        keys_now = {p.proposal_key for p in proposals}
        with self.tx() as c:
            for p in proposals:
                row = c.execute("SELECT proposal_key, decision FROM proposals WHERE proposal_key=?", (p.proposal_key,)).fetchone()
                if row:
                    c.execute(
                        "UPDATE proposals SET data_json=?, title=?, tier=?, last_seen_run=?, stale=0 WHERE proposal_key=?",
                        (json.dumps(p.to_dict(), default=str), p.title, p.tier, run_id, p.proposal_key),
                    )
                    counts["existing"] += 1
                    continue
                c.execute(
                    """INSERT INTO proposals(proposal_key, action_type, tier, title, site_key, crm_account_ids_json, data_json,
                       decision, apply_status, first_seen_run, last_seen_run, stale)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,0)""",
                    (
                        p.proposal_key,
                        p.action_type,
                        p.tier,
                        p.title,
                        (p.site or {}).get("slug") or (p.site or {}).get("source_url"),
                        json.dumps(sorted(p.crm_account_ids)),
                        json.dumps(p.to_dict(), default=str),
                        DECISION_PENDING,
                        APPLY_NONE,
                        run_id,
                        run_id,
                    ),
                )
                counts["new"] += 1
                # A *pending* earlier proposal for the same site/accounts+action that no longer
                # appears is superseded by this one (evidence changed). Decided ones are left alone
                # but linked for transparency.
                site_key = (p.site or {}).get("slug") or (p.site or {}).get("source_url")
                olds = c.execute(
                    "SELECT proposal_key, decision FROM proposals WHERE proposal_key<>? AND action_type=? AND crm_account_ids_json=? AND IFNULL(site_key,'')=IFNULL(?,'') AND superseded_by IS NULL",
                    (p.proposal_key, p.action_type, json.dumps(sorted(p.crm_account_ids)), site_key),
                ).fetchall()
                for old in olds:
                    if old["proposal_key"] in keys_now:
                        continue
                    c.execute("UPDATE proposals SET superseded_by=?, stale=1 WHERE proposal_key=?", (p.proposal_key, old["proposal_key"]))
                    counts["superseded"] += 1
            # proposals not produced by this run: mark stale (hidden by default, never deleted)
            rows = c.execute("SELECT proposal_key FROM proposals WHERE last_seen_run<>? AND stale=0 AND origin='scan'", (run_id,)).fetchall()
            for r in rows:
                c.execute("UPDATE proposals SET stale=1 WHERE proposal_key=?", (r["proposal_key"],))
                counts["stale"] += 1
        return counts

    def add_reviewer_proposal(self, run_id: str, p: Proposal, reviewer_reason: str) -> bool:
        """Insert a proposal created by an explicit reviewer selection. Returns False if it already exists."""
        with self.tx() as c:
            if c.execute("SELECT 1 FROM proposals WHERE proposal_key=?", (p.proposal_key,)).fetchone():
                return False
            c.execute(
                """INSERT INTO proposals(proposal_key, action_type, tier, title, site_key, crm_account_ids_json, data_json,
                   decision, apply_status, first_seen_run, last_seen_run, stale, origin)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,0,'reviewer')""",
                (p.proposal_key, p.action_type, p.tier, p.title, (p.site or {}).get("slug") or (p.site or {}).get("source_url"), json.dumps(sorted(p.crm_account_ids)), json.dumps(p.to_dict(), default=str), DECISION_PENDING, APPLY_NONE, run_id, run_id),
            )
            self._audit(c, p.proposal_key, "reviewer_created_proposal", {"reason": reviewer_reason, "accounts": p.crm_account_ids})
        return True

    def get_proposal_row(self, key: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM proposals WHERE proposal_key=?", (key,)).fetchone()

    def get_proposal(self, key: str) -> Proposal | None:
        row = self.get_proposal_row(key)
        return Proposal.from_dict(json.loads(row["data_json"])) if row else None

    def list_proposals(
        self,
        *,
        include_stale: bool = False,
        action_types: list[str] | None = None,
        decisions: list[str] | None = None,
        tiers: list[str] | None = None,
        search: str = "",
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM proposals WHERE 1=1"
        args: list[Any] = []
        if not include_stale:
            sql += " AND stale=0"
        if action_types:
            sql += f" AND action_type IN ({','.join('?' * len(action_types))})"
            args += action_types
        if decisions:
            sql += f" AND decision IN ({','.join('?' * len(decisions))})"
            args += decisions
        if tiers:
            sql += f" AND tier IN ({','.join('?' * len(tiers))})"
            args += tiers
        if search:
            sql += " AND (title LIKE ? OR data_json LIKE ?)"
            args += [f"%{search}%", f"%{search}%"]
        sql += " ORDER BY CASE decision WHEN 'pending' THEN 0 ELSE 1 END, action_type, title"
        return self._conn.execute(sql, args).fetchall()

    def counts(self) -> dict[str, Any]:
        out: dict[str, Any] = {"by_action": {}, "by_decision": {}, "by_apply": {}}
        for r in self._conn.execute("SELECT action_type, COUNT(*) n FROM proposals WHERE stale=0 GROUP BY action_type"):
            out["by_action"][r["action_type"]] = r["n"]
        for r in self._conn.execute("SELECT decision, COUNT(*) n FROM proposals WHERE stale=0 GROUP BY decision"):
            out["by_decision"][r["decision"]] = r["n"]
        for r in self._conn.execute("SELECT apply_status, COUNT(*) n FROM proposals WHERE stale=0 GROUP BY apply_status"):
            out["by_apply"][r["apply_status"]] = r["n"]
        out["pending_actionable"] = self._conn.execute(
            "SELECT COUNT(*) FROM proposals WHERE stale=0 AND decision='pending' AND action_type IN ('UPDATE_FIELDS','REPARENT','CHOW_SUCCESSOR','CREATE_MISSING','DUPLICATE','MISSING_FROM_SITE')"
        ).fetchone()[0]
        return out

    # --- decisions ------------------------------------------------------------------

    def reject(self, key: str, reason: str) -> None:
        with self.tx() as c:
            row = c.execute("SELECT decision, apply_status FROM proposals WHERE proposal_key=?", (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            if row["apply_status"] in (APPLY_IN_PROGRESS, APPLY_APPLIED):
                raise AlreadyClaimed(f"proposal {key} is {row['apply_status']}; cannot reject")
            c.execute(
                "UPDATE proposals SET decision=?, decided_at=?, reviewer_note=? WHERE proposal_key=?",
                (DECISION_REJECTED, utcnow(), reason, key),
            )
            self._audit(c, key, "rejected", {"reason": reason})

    def acknowledge(self, key: str, note: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE proposals SET decision=?, decided_at=?, reviewer_note=? WHERE proposal_key=? AND decision='pending'",
                (DECISION_ACKNOWLEDGED, utcnow(), note, key),
            )
            self._audit(c, key, "acknowledged", {"note": note})

    def reopen(self, key: str, reason: str) -> None:
        """Explicit reviewer action only. Never called by scan."""
        with self.tx() as c:
            row = c.execute("SELECT apply_status FROM proposals WHERE proposal_key=?", (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            if row["apply_status"] in (APPLY_IN_PROGRESS, APPLY_APPLIED):
                raise AlreadyClaimed(f"proposal {key} is {row['apply_status']}; cannot reopen")
            c.execute(
                "UPDATE proposals SET decision='pending', decided_at=NULL, reviewer_note=?, apply_status='none', error=NULL WHERE proposal_key=?",
                (reason, key),
            )
            self._audit(c, key, "reopened", {"reason": reason})

    def claim_for_apply(self, key: str, reviewer_note: str) -> None:
        """Atomically record approval and claim the proposal for a single apply attempt.

        Succeeds only if the proposal is pending (or approved-but-recoverable) and not already
        in progress / applied. Raises AlreadyClaimed otherwise.
        """
        with self.tx() as c:
            row = c.execute("SELECT decision, apply_status, stale FROM proposals WHERE proposal_key=?", (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            if row["stale"]:
                raise AlreadyClaimed("proposal is stale (not produced by the latest scan); rescan before applying")
            ok = c.execute(
                """UPDATE proposals SET decision=?, decided_at=COALESCE(decided_at, ?), reviewer_note=?, apply_status=?, claimed_at=?, error=NULL
                   WHERE proposal_key=? AND apply_status IN (?,?,?,?) AND decision IN (?,?)""",
                (
                    DECISION_APPROVED,
                    utcnow(),
                    reviewer_note,
                    APPLY_IN_PROGRESS,
                    utcnow(),
                    key,
                    APPLY_NONE,
                    APPLY_FAILED,
                    APPLY_NEEDS_RECOVERY,
                    APPLY_BLOCKED,
                    DECISION_PENDING,
                    DECISION_APPROVED,
                ),
            ).rowcount
            if ok != 1:
                raise AlreadyClaimed(f"proposal {key} is {row['decision']}/{row['apply_status']}; not claimable")
            self._audit(c, key, "approved_and_claimed", {"reviewer_note": reviewer_note})

    def finish_apply(self, key: str, status: str, error: str | None, result: dict[str, Any] | None, *, reset_to_pending: bool = False) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE proposals SET apply_status=?, applied_at=?, error=?, result_json=?, decision=CASE WHEN ? THEN 'pending' ELSE decision END WHERE proposal_key=?",
                (status, utcnow() if status == APPLY_APPLIED else None, error, json.dumps(result, default=str) if result else None, 1 if reset_to_pending else 0, key),
            )
            self._audit(c, key, f"apply_{status}", {"error": error, "result": result})

    # --- steps ----------------------------------------------------------------------

    def record_step(
        self,
        key: str,
        step_name: str,
        status: str,
        *,
        account_id: str | None = None,
        resulting_id: str | None = None,
        payload: dict[str, Any] | None = None,
        response: Any = None,
        error: str | None = None,
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO mutation_steps(proposal_key, step_name, crm_account_id, resulting_id, status, attempted_at, payload_json, response_json, error)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (key, step_name, account_id, resulting_id, status, utcnow(), json.dumps(payload, default=str) if payload is not None else None, json.dumps(response, default=str) if response is not None else None, error),
        )
        return int(cur.lastrowid)

    def update_step(self, step_id: int, status: str, *, resulting_id: str | None = None, response: Any = None, error: str | None = None) -> None:
        self._conn.execute(
            "UPDATE mutation_steps SET status=?, resulting_id=COALESCE(?, resulting_id), response_json=COALESCE(?, response_json), error=? WHERE step_id=?",
            (status, resulting_id, json.dumps(response, default=str) if response is not None else None, error, step_id),
        )

    def steps(self, key: str) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM mutation_steps WHERE proposal_key=? ORDER BY step_id", (key,)).fetchall()

    def all_steps(self) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM mutation_steps ORDER BY step_id").fetchall()

    # --- audit ------------------------------------------------------------------------

    def _audit(self, c: sqlite3.Connection, key: str | None, event: str, detail: dict[str, Any] | None) -> None:
        c.execute("INSERT INTO audit(at, proposal_key, event, detail_json) VALUES (?,?,?,?)", (utcnow(), key, event, json.dumps(detail, default=str) if detail else None))

    def audit_log(self, limit: int = 200) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM audit ORDER BY audit_id DESC LIMIT ?", (limit,)).fetchall()

    def log(self, event: str, detail: dict[str, Any] | None = None, key: str | None = None) -> None:
        with self.tx() as c:
            self._audit(c, key, event, detail)
