# Bellhaven ↔ CRM reconciliation — write-up

## What this is

A read-only reconciliation scan plus a human-in-the-loop review app. The scan scrapes every Bellhaven community page (directory pages 1–3 and the homepage-only Findlay feature), reads every CRM account and contact, resolves facilities to accounts by physical address, and writes evidence-backed proposals into a local SQLite ledger. A reviewer approves or rejects each proposal in a Streamlit app; approval is the only path that writes to the CRM, and every write is re-checked against fresh data, applied minimally, and verified by a read-back.

## Evidence hierarchy and why

1. **Street number + canonical street + state + ZIP.** Facilities move rarely and get renamed often (Kettering Care Centre → Bellhaven of Kettering; Sunny Acres → Bellhaven Willow Creek). Address is the stable identity.
2. **Fuzzy street** only with the same number, state and ZIP/city, to absorb typos without inventing matches.
3. **Name similarity** is recorded but never sufficient: *Amberly Manor* exists in Hudson, OH (website) and Colorado Springs, CO (CRM); *Bellhaven at Union Square* (118 Union Square Dr) and *Union Square Senior Living* (240 Market St, Juniper Point) share a city and two words of a name but not a street, phone, administrator or operator. Both go to manual review / create with the rejected candidate shown.
4. **Phone and administrator contact** corroborate and break ties (Owosso survivor), but cannot override an address contradiction.

Tiers are categorical (`HIGH_ADDRESS_EXACT`, `MEDIUM_ADDRESS_FUZZY`, `LOW_NAME_ONLY`, `AMBIGUOUS_MULTI_CANDIDATE`, `NO_CANDIDATE`) with reason codes rather than invented probabilities, so a reviewer can see exactly which rule fired. Full rules: `docs/MATCHING_DECISIONS.md`.

## CHOW standard operating procedure

`requires_chow = old_parent != Bellhaven and lifetime_revenue > 0 and outstanding_ar > 0`

* Evaluated at scan time and **again on a fresh GET immediately before the write**. A changed answer blocks the apply.
* Unparseable or missing amounts are *unknown*, not zero, and block any ownership write.
* False → PATCH parent_id on the existing account (Findlay $22,000/$0, Lima $47,000/$0, Zanesville $0/$0). Kettering also satisfies the financial criteria for direct re-parenting but remains unresolved due to ambiguous CRM matches.
* True → `POST` a successor under Bellhaven from the website's current details, then `PATCH` **only** `chow_current_account` on the old account (Marietta $51,250/$3,800, Tiffin $84,000/$12,400). Old parent, status, name, financials and note are untouched and verified untouched afterwards.
* Recovery: the successor carries a note marker `chow-successor-of:<old id>`; the ledger stores the created id the moment the POST returns. A timeout, a crash between POST and PATCH, or a restart all resolve to *find the successor, link it* — never a second POST. Tests cover each case.
* Duplicates (two Bellhaven accounts at one address) are handled separately: loser → `Inactive` + `duplicate_of_account` + note, nothing deleted. A loser with billing history, or a survivor tie, is a manual decision. Cross-parent accounts at one address (Erie, Monroe, Sandusky/Millstone) are ownership lineage, surfaced for review, never auto-marked duplicate.
* Bellhaven children absent from a **complete** site scan get `Needs Review` + note. An incomplete scan produces no absence proposals at all.

## Idempotency

A proposal's key hashes its material outcome (action, site, accounts, target parent, non-note field changes). Rescans upsert by key: decisions persist, vanished keys go stale (hidden, never deleted), changed evidence produces a new key that visibly supersedes the old one even when the old one was rejected. An applied CHOW is recognized on the next scan because the old record's `chow_current_account` points at the Bellhaven successor, so it is neither re-proposed nor treated as a duplicate. Approval uses an atomic SQLite claim, so a double click or second tab cannot start a second apply.

## How AI tools were used

Claude Code (Fable 5.1) was used as the implementation assistant: it read the engineering plan, discovered the API, wrote the scraper, matcher, rules, ledger, protected writer, Streamlit app, tests and docs, and ran the scans and test suite. The matching and CHOW decisions are deterministic code; no model is called at runtime. ChatGPT/Claude chat were used earlier for the reconnaissance and plan documents that this implementation follows. Every CRM mutation was approved by the human reviewer in the app.

## What I would build next

* Field-level provenance in the CRM note (which site snapshot justified a change) and a per-run diff report.
* Contact reconciliation (administrator names from the site vs CRM contacts) as proposals rather than evidence only.
* A `reopen`/bulk-acknowledge workflow for informational items and an export of the audit log.
* Geocoding as an extra, non-overriding signal for near-identical addresses.

## Where live data differed from the plan

* Kettering has three same-address accounts under three parents, not one; the tool refuses to guess and gives the reviewer a selection control that still routes through the guarded apply path.
* Ashtabula's CRM address is a PO Box; with name, ZIP and phone corroboration it becomes an address correction instead of a false "missing from site".
* `Union Square Senior Living` belongs to Juniper Point, not Bellhaven.
* The id field is `account_id`, the list envelope is `data`, and the parent is literally named "Bellhaven Senior Living (Parent Account)"; all handled by the tolerant field map and the parent finder.

## Known limitations

* The OpenAPI document declares no schemas; field names and enumerations were verified empirically from live GETs (see `docs/API_DISCOVERY.md`) and are centralized in `bellhaven/config.py`.
* `care_type` is single-valued in the CRM while the site lists multiple offerings for some facilities; the tool never overwrites a valid value on list order and keeps all offerings in evidence/notes.
* The status value `Needs Review` had never been observed on an existing record before the first write; the API accepted it on all three approved applies and it reads back correctly. The tool would have surfaced a 422 as a failed apply rather than hiding it.
* `verify` and the dashboard read-back prove the CRM state at that moment; they are not a continuous monitor.

## Results

Live outcome (details and account ids in `docs/RESULTS.md`): 35 website facilities, 121 → 127 CRM accounts, 29 → 38 Bellhaven children. 43 proposals generated by the scan plus one created by the reviewer; the reviewer approved 28 in the app (2 CHOW successors, 3 direct re-parents, 4 new accounts, 1 duplicate, 3 Needs Review, 15 field updates), every one verified by GET read-back; 0 failed, 0 pending. Of the six manual-review cases, Union Square was resolved by the reviewer confirming it is a distinct facility and creating the missing account; four are prior-operator records at Bellhaven addresses, kept unchanged by design; Kettering needs business input (three same-address candidates, no corroboration). Thirteen scans were run in total; after approvals every pair of consecutive scans was identical and no decided item ever reappeared. Zero CRM writes happened outside explicit approvals.

## Time

Reconnaissance and planning were done beforehand (the plan documents). The implementation session with Claude Code, including live verification, the reviewer's approvals and the audits, ran for roughly two and a quarter hours of wall-clock time on October 30, 2026, more than the suggested two hours; the extra time went into the live-data refinements (PO Box, multi-candidate Kettering, contact pagination bug) and the fault-injection tests for the CHOW recovery path.
