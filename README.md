# Bellhaven Senior Living ↔ Clipboard Health CRM reconciliation

A small, explainable pipeline that reconciles the [Bellhaven Senior Living website](https://analyst-assessment-production.up.railway.app/) with the Clipboard Health CRM sandbox, and a local review app in which a human approves every CRM change one proposal at a time.

```
website (35 facilities)  ─┐
                          ├─► normalize ─► match by address ─► rule engine ─► SQLite proposal ledger ─► Streamlit review ─► (human click) ─► CRM write + read-back
CRM (every account)      ─┘
```

* `scan` is **read-only** toward the CRM and safe to schedule daily.
* The **only** code path that writes to the CRM is `bellhaven/apply.py`, reached exclusively from the **Approve & apply** button for one proposal in `app.py`. Rejections, scans, verification, tests and page loads never write.
* Change-of-ownership (CHOW) rule enforced at scan time *and* re-checked on fresh data immediately before every write:
  `requires_chow = old_parent != Bellhaven and lifetime_revenue > 0 and outstanding_ar > 0`.

## Setup (Windows PowerShell)

```powershell
cd F:\AppForge\BB_clipboard_health_crm_reconciliation
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env          # paste your token after BELLHAVEN_API_TOKEN= and save
```

The token is read **only** from `.env` or the environment. It is never printed, logged, put in a URL or committed (`.env` is git-ignored).

Unix equivalent: `python3.11 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && cp .env.example .env`.

## Run

```powershell
python -m bellhaven.cli probe     # GET-only: prints API envelope / field names (no values) -- used for docs/API_DISCOVERY.md
python -m bellhaven.cli scan      # read-only: scrape site + fetch all CRM accounts + generate proposals -> data\bellhaven.sqlite3
python -m bellhaven.cli summary   # counts and the queue
streamlit run app.py              # review dashboard at http://localhost:8501
python -m bellhaven.cli verify    # GET-only re-check of every applied proposal + live counts
python -m bellhaven.cli show <proposal_key>
python -m pytest                  # offline test suite (fixtures + fake CRM); never touches the live API
```

### Reviewing in the app

1. Open a proposal from the queue. Read the website evidence (with source link), the current CRM record(s), every candidate that was considered (including rejected ones and why), the financial values, and the exact field-level before/after table plus the API steps that will run.
2. **Approve & apply** (after ticking the confirmation box) re-fetches the affected account(s), refuses if anything guarded changed since the scan (`PRECONDITION_CHANGED`), re-evaluates the CHOW rule, sends the minimal PATCH/POST, then GETs the record(s) back and shows the verified state. Failures are shown as failures.
3. **Reject** (reason required) records the decision locally only. **Mark reviewed** is the equivalent for items that have no CRM mutation (manual review / no change).
4. Decisions persist in `data\bellhaven.sqlite3`; rescans never resurrect decided proposals. A proposal whose evidence changed appears as a clearly new item that supersedes the old one.

### Proposal types

`NO_CHANGE`, `UPDATE_FIELDS`, `REPARENT`, `CHOW_SUCCESSOR`, `CREATE_MISSING`, `DUPLICATE`, `MISSING_FROM_SITE`, `MANUAL_REVIEW` -- see [docs/MATCHING_DECISIONS.md](docs/MATCHING_DECISIONS.md) for the exact rules and [docs/API_DISCOVERY.md](docs/API_DISCOVERY.md) for the verified API contract.

## Daily scheduled scan (read-only)

* Windows: `.\cron\Register-DailyScan.ps1` registers a Task Scheduler job "Bellhaven CRM daily scan" at 09:00 that runs `python -m bellhaven.cli scan` and appends to `logs\daily-scan.log`. Remove with `Unregister-ScheduledTask -TaskName "Bellhaven CRM daily scan" -Confirm:$false`.
* Linux/macOS: `cron/bellhaven.cron` (`CRON_TZ=America/Denver`, `0 9 * * *`).

The scheduled job only refreshes the proposal queue. It cannot apply anything. Keep `data\bellhaven.sqlite3` on persistent storage (not an ephemeral CI runner) because it holds the reviewer decisions and the mutation ledger.

## Layout

```
app.py                      Streamlit review dashboard (only place a CRM write can be triggered)
bellhaven/
  config.py                 env/.env settings, CRM field names, allowed write fields
  scraper.py                homepage + paginated directory + detail pages; fail-closed completeness
  crm_api.py                authenticated client; read-only by default; GET retries; POST/PATCH never auto-retried
  normalize.py              names, addresses (St Lawrence != Street Lawrence), phones, money (unknown != 0), care mapping
  matching.py               address-first candidate generation, tiers + reason codes, contested detection
  rules.py                  proposal generation incl. requires_chow(), duplicates, missing-from-site
  store.py                  SQLite ledger: runs, snapshots, proposals, decisions, mutation steps, audit
  apply.py                  protected writer: claim -> preflight -> minimal writes -> read-back verify -> ledger
  pipeline.py               run_scan (read-only)
  cli.py                    scan / summary / verify / probe / show
tests/                      pytest suite with website fixtures and an in-memory fake CRM (fault injection)
cron/                       cron example + Windows Task Scheduler registration script
docs/                       API_DISCOVERY, MATCHING_DECISIONS, RESULTS, DEMO_CHECKLIST, WRITEUP
```

## Safety properties (all covered by tests)

* Scan, summary, verify, page render, reject: **zero** CRM writes (the client raises if anything tries).
* Only fields in `config.ALLOWED_WRITE_FIELDS` can ever be sent; `lifetime_revenue`, `outstanding_ar`, `id` are hard-forbidden.
* CHOW: old account keeps parent/status/name/financials/note; only `chow_current_account` is set; successor carries an idempotency marker; timeouts, crashes and restarts never create a second successor.
* Double-click / second tab: atomic SQLite claim; the second attempt is refused.
* Apply blocked when the CRM record changed since the proposal (`PRECONDITION_CHANGED`), when fresh financials flip the CHOW answer, or when financials are unparseable.
* Decisions survive rescans; changed evidence produces a visibly new proposal instead of silently overriding a past decision.
