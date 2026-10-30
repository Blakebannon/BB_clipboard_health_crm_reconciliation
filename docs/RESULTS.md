# Results

All figures below come from live, authenticated, **GET-only** runs on **2026-10-30** (runs `20261030T210400Z-5e5c6e` through the fourth scan). No CRM write has been performed by this tool at the time of writing; every number in the "after approvals" section is to be filled in from `python -m bellhaven.cli verify` once the reviewer has approved items in the app.

## Baseline (before any approval)

| Measure | Live value | Reconnaissance expectation |
|---|---|---|
| Website facilities scraped (directory 16+16+2, homepage-only Findlay) | **35**, scan complete | 35 |
| CRM accounts read (all pages, all parents) | **121** | 121 |
| CRM contacts read | 56 | – |
| Bellhaven parent (dynamically identified) | `0015QAPLGS3FVYEEEM` "Bellhaven Senior Living (Parent Account)" | `0015QAPLGS3FVYEEEM` |
| Direct Bellhaven children | **29** | 29 |
| CRM writes during scans | **0** | 0 |

Match tiers for the 35 site facilities: 29 `HIGH_ADDRESS_EXACT`, 2 `MEDIUM_ADDRESS_FUZZY` (Portsmouth ZIP typo 45626→45662; Ashtabula PO Box), 1 `LOW_NAME_ONLY` (Union Square), 3 `NO_CANDIDATE` (Amberly Manor OH, Batavia, Carlisle). 31 of 35 facilities resolve to a CRM account by address.

## Proposal queue (43 items)

| Type | Count | Items |
|---|---|---|
| `CHOW_SUCCESSOR` | 2 | Marietta (`001A34WFSUYHCRBLFT`, $51,250 / $3,800, parent Cedar Trail), Tiffin (`001U6RW32TY0WSXZZB`, $84,000 / $12,400, parent Cedar Trail) |
| `REPARENT` | 3 | Findlay (`001UKEFGADQ8YCZ4YM`, no parent, $22,000 / $0), Lima (`001LGFPBJY4N9MB6KL`, Harborview, $47,000 / $0), Zanesville (`001H1JMVZWP46D5VUF`, Cedar Trail, $0 / $0, + rename) |
| `UPDATE_FIELDS` | 15 | Renames: Chesterton, Chagrin Falls, Portage (Willow Creek), Ashland, Grove City. Address: Portsmouth ZIP, Ashtabula PO Box → street. Phone corrections on 12 accounts (bundled where a rename exists). |
| `DUPLICATE` | 1 | Owosso: `001QU150PM4Z15UA71` → Inactive, `duplicate_of_account` = `001EGU7BMJ942ZTRE6` (survivor matches site phone and administrator contact; evidence score 9 vs 2) |
| `CREATE_MISSING` | 3 | Amberly Manor (Hudson OH; the Colorado Springs "Amberly Manor" is listed as rejected, `STATE_MISMATCH`), Batavia, Carlisle |
| `MISSING_FROM_SITE` | 3 | Alliance, Coldwater, Sandusky ($130,000 / $5,200; Millstone Care of Sandusky sits at the same address) → status `Needs Review` + note |
| `MANUAL_REVIEW` | 6 | Union Square (118 Union Square Dr vs CRM 240 Market St, parent Juniper Point); Kettering (three non-Bellhaven accounts at 3313 Wilmington Pike/Pk, all $0/$0, no corroboration); cross-parent history at Monroe (×2), Erie, Port Clinton |
| `NO_CHANGE` | 10 | Already correct (informational) |

CHOW cases: **2**. Duplicates: **1**. Unresolved exceptions (manual review): **6**.

### Deviations from the reconnaissance fixtures

* **Kettering** was expected to be a direct re-parent of *Kettering Care Centre*. Live data has three accounts at that address under three different parents (Harborview, Cedar Trail, none) with identical $0/$0 financials and no phone/contact match. The engine refuses to guess and routes it to manual review; the app lets the reviewer pick the correct record, which creates a normal guarded re-parent/CHOW proposal for approval.
* **Ashtabula** (`001NXP9X46CWEPSLSV`) is a Bellhaven child whose CRM address is `PO Box 517`. Name, city, ZIP and phone match the site, so it is proposed as a medium-confidence address correction rather than as "missing from site".
* **Union Square Senior Living** is under Juniper Point, not Bellhaven; it stays a manual-review item and is not proposed as missing-from-site.
* **Phones**: 14 CRM accounts match the site phone exactly and 12 differ, so phone differences are treated as stale data and proposed as low-risk field corrections (shown separately in each diff).

## Idempotency

| Run | new | existing | superseded | stale | CRM writes |
|---|---|---|---|---|---|
| scan #1 | 43 | 0 | 0 | 0 | 0 |
| scan #2 | 0 | 43 | 0 | 0 | 0 |
| scan #3 (after the PO Box / `Pk` rule refinements) | 2 | 41 | 0 | 2 | 0 |
| scan #4 | 0 | 43 | 0 | 0 | 0 |

The two "stale" rows in scan #3 are the earlier Ashtabula and Kettering manual-review items, replaced by the refined proposals; they remain in the ledger for audit.

## After approvals

_To be completed after the reviewer approves/rejects items in the app (`python -m bellhaven.cli verify`)._

| Measure | Before | After |
|---|---|---|
| CRM accounts | 121 | – |
| Bellhaven direct children | 29 | – |
| Proposals applied / verified by GET | 0 | – |
| Proposals rejected | 0 | – |
| Proposals failed / blocked | 0 | – |

Applied changes, exceptions and remaining risks: _pending_.
