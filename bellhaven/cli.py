"""Command-line interface.

    python -m bellhaven.cli scan      # read-only: scrape + fetch CRM + propose (safe for a scheduler)
    python -m bellhaven.cli summary   # counts and the proposal queue from the local ledger
    python -m bellhaven.cli verify    # GET-only re-check of every applied proposal + live counts
    python -m bellhaven.cli probe     # GET-only: show API envelope/field names (sanitized) for docs
    python -m bellhaven.cli show KEY  # print one proposal in full

None of these commands can write to the CRM: they construct a read-only client.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import config
from .crm_api import CRMClient, make_client
from .models import APPLY_APPLIED, CRMAccount, Proposal
from .pipeline import find_bellhaven_parent, run_scan
from .store import Store


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_scan(args: argparse.Namespace) -> int:
    settings = config.get_settings()
    store = Store(settings.db_path)
    client: CRMClient = make_client(settings, allow_writes=False)
    report = run_scan(settings, store, client)
    _print(report.to_dict())
    print(f"\nrun {report.run_id}: site={report.site_count} (complete={report.scrape_complete}) crm={report.crm_count} "
          f"bellhaven_children={report.bellhaven_child_count} proposals={sum(report.proposals_by_action.values())} "
          f"crm_writes={report.crm_writes}")
    return 0 if not report.errors else 1


def cmd_summary(args: argparse.Namespace) -> int:
    settings = config.get_settings()
    store = Store(settings.db_path)
    run = store.latest_run()
    if run is None:
        print("no completed scan yet; run: python -m bellhaven.cli scan")
        return 1
    print(f"latest run {run['run_id']} started {run['started_at']} site={run['site_count']} crm={run['crm_count']} "
          f"bellhaven_children={run['bellhaven_child_count']} scrape_ok={bool(run['scrape_ok'])}")
    if run["error_summary"]:
        print(f"errors: {run['error_summary']}")
    _print(store.counts())
    rows = store.list_proposals(include_stale=args.all)
    print(f"\n{'KEY':26} {'ACTION':18} {'TIER':26} {'DECISION':13} {'APPLY':15} TITLE")
    for r in rows:
        print(f"{r['proposal_key']:26} {r['action_type']:18} {r['tier']:26} {r['decision']:13} {r['apply_status']:15} {r['title'][:90]}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    settings = config.get_settings()
    store = Store(settings.db_path)
    row = store.get_proposal_row(args.key)
    if row is None:
        print("no such proposal")
        return 1
    d = dict(row)
    d["data"] = json.loads(d.pop("data_json"))
    d["steps_ledger"] = [dict(s) for s in store.steps(args.key)]
    _print(d)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """GET-only. Re-reads every applied proposal's accounts and reports whether the intended state holds."""
    settings = config.get_settings()
    store = Store(settings.db_path)
    client = make_client(settings, allow_writes=False)
    accounts = [CRMAccount.from_api(a) for a in client.iter_all_accounts()]
    parent = find_bellhaven_parent(accounts, settings.parent_name)
    children = [a for a in accounts if a.parent_id == parent.id]
    print(f"live CRM: {len(accounts)} accounts; Bellhaven parent {parent.id} ({parent.name!r}) has {len(children)} direct children")
    rows = store.list_proposals(include_stale=True, decisions=["approved"])
    ok = fail = 0
    for r in rows:
        if r["apply_status"] != APPLY_APPLIED:
            print(f"- {r['proposal_key']} {r['action_type']}: apply_status={r['apply_status']} error={r['error']}")
            continue
        p = Proposal.from_dict(json.loads(r["data_json"]))
        result = json.loads(r["result_json"] or "{}")
        problems: list[str] = []
        for acc_id, intended in p.after.items():
            if acc_id == "NEW":
                acc_id = result.get("created_id") or ""
                if not acc_id:
                    problems.append("no created id recorded")
                    continue
            fresh = CRMAccount.from_api(client.get_account(acc_id)).snapshot()
            for f, v in intended.items():
                if f in ("lifetime_revenue", "outstanding_ar", "note"):
                    continue
                if v == "<successor id>":
                    v = result.get("created_id")
                if (fresh.get(f) or None) != (v or None):
                    problems.append(f"{acc_id}.{f}: expected {v!r} got {fresh.get(f)!r}")
        if problems:
            fail += 1
            print(f"FAIL {r['proposal_key']} {r['action_type']}: {problems}")
        else:
            ok += 1
            print(f"OK   {r['proposal_key']} {r['action_type']}: {r['title'][:80]}")
    print(f"\nverified applied proposals: ok={ok} fail={fail}; CRM writes by this command: {client.successful_writes}")
    return 0 if fail == 0 else 1


def cmd_probe(args: argparse.Namespace) -> int:
    """GET-only: report response envelope and field names/types without dumping values."""
    settings = config.get_settings()
    client = make_client(settings, allow_writes=False)
    me = client.me()
    print("GET /me keys:", sorted(me.keys()) if isinstance(me, dict) else type(me).__name__)
    page = client.list_accounts(page=1, page_size=2)
    if isinstance(page, dict):
        print("GET /accounts envelope keys:", {k: type(v).__name__ for k, v in page.items()})
    else:
        print("GET /accounts returns a bare list")
    items = client.extract_items(page)
    if items:
        print("account field types:", {k: type(v).__name__ for k, v in items[0].items()})
        detail = client.get_account(str(items[0]["id"]))
        print("GET /accounts/{id} field types:", {k: type(v).__name__ for k, v in detail.items()})
        extra = set(detail) - set(items[0])
        if extra:
            print("detail-only fields:", sorted(extra))
    cpage = client.list_contacts(page=1, page_size=2) if hasattr(client, "list_contacts") else client._get("contacts", {"page": 1, "page_size": 2})
    citems = client.extract_items(cpage)
    if citems:
        print("contact field types:", {k: type(v).__name__ for k, v in citems[0].items()})
    print("writes performed:", client.successful_writes)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bellhaven.cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan", help="read-only scan: scrape + fetch CRM + generate proposals")
    s = sub.add_parser("summary", help="show counts and queue from the local ledger")
    s.add_argument("--all", action="store_true", help="include stale proposals")
    sub.add_parser("verify", help="GET-only verification of applied proposals")
    sub.add_parser("probe", help="GET-only API shape discovery (sanitized)")
    sh = sub.add_parser("show", help="print one proposal")
    sh.add_argument("key")
    args = parser.parse_args(argv)
    return {"scan": cmd_scan, "summary": cmd_summary, "verify": cmd_verify, "probe": cmd_probe, "show": cmd_show}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
