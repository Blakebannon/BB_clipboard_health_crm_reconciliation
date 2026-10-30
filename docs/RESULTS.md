# Results

All figures come from live, authenticated runs on **2026-10-30**. Scans, `verify` and the audit scripts are GET-only; the only writes were the 27 proposals the reviewer approved one by one in the Streamlit app. Every write was followed by a GET read-back at apply time and re-verified independently afterwards.

## Before (first live scan, run `20261030T210400Z-5e5c6e`)

| Measure | Live value | Reconnaissance expectation |
|---|---|---|
| Website facilities (directory 16+16+2 + homepage-only Findlay) | **35**, scan complete | 35 |
| CRM accounts (all pages, all parents) | **121** | 121 |
| CRM contacts | 67 | – |
| Bellhaven parent (found dynamically) | `0015QAPLGS3FVYEEEM` "Bellhaven Senior Living (Parent Account)" | same |
| Direct Bellhaven children | **29** | 29 |
| Site facilities resolved to a CRM account by address | 31 of 35 (29 exact, 2 fuzzy: Portsmouth ZIP typo, Ashtabula PO Box) | – |

Queue generated: 43 proposals (2 CHOW, 3 re-parent, 15 field updates, 1 duplicate, 3 create, 3 missing-from-site, 6 manual review, 10 no-change).

## After (final scans, runs `20261030T220558Z-d0de7a` and `20261030T220603Z-07788e`, verify at 22:06 UTC)

| Measure | Before | After | Check |
|---|---|---|---|
| CRM accounts | 121 | **126** | 121 + 2 CHOW successors + 3 created = 126 ✓ |
| Bellhaven direct children | 29 | **37** | 29 + 2 successors + 3 re-parents + 3 created = 37 ✓ (includes the Inactive Owosso duplicate, which keeps its parent) |
| Children with status `Needs Review` | 0 | 3 | Alliance, Coldwater, Sandusky ✓ |
| Accounts flagged `created_by_candidate` | 0 | 5 | 2 successors + 3 created ✓ |
| Proposals approved and applied | 0 | **27** | `verify`: ok=27, fail=0 |
| Proposals rejected / acknowledged | 0 | 1 acknowledged | Union Square manual-review item (first rejected, then reopened and acknowledged after the reviewer chose the create path) |
| Pending actionable proposals | 43 | **1** | Union Square `CREATE_MISSING` (reviewer-created, key `c9ac15470e7ce04054fb8b2d`), deliberately left unapplied |
| Remaining manual-review items | 6 | 5 pending | see below |
| CRM writes by scans / verify / audits | 0 | 0 | |

### Applied changes (27), each verified by GET against the proposal's before-snapshot

* **CHOW successors (2)** — Marietta `001A34WFSUYHCRBLFT` → successor `001D0F65A667AB179E`; Tiffin `001U6RW32TY0WSXZZB` → successor `001461D68A924CD169`. On both old accounts the parent (Cedar Trail), status, name, note, `lifetime_revenue` ($51,250 / $84,000) and `outstanding_ar` ($3,800 / $12,400) are byte-for-byte unchanged; only `chow_current_account` was set and it points at the successor. Both successors are Active under Bellhaven with the website's address, phone and care type and carry the `chow-successor-of:` marker.
* **Direct re-parents (3)** — Findlay `001UKEFGADQ8YCZ4YM` ($22,000 / $0, had no parent), Lima `001LGFPBJY4N9MB6KL` ($47,000 / $0, was Harborview), Zanesville `001H1JMVZWP46D5VUF` ($0 / $0, was Cedar Trail, renamed from "Cedar Trail of Zanesville"). Financials unchanged, no successor created.
* **New accounts (3)** — Amberly Manor (Hudson OH) `001F439900F2F30C89`, Bellhaven of Batavia `0011EFBF6122B5240D`, Bellhaven of Carlisle `001792440B34727CFE`; each is the only account at its address.
* **Duplicate (1)** — Owosso `001QU150PM4Z15UA71` is Inactive with `duplicate_of_account` = `001EGU7BMJ942ZTRE6` and an explanatory note; the survivor is unchanged.
* **Missing from site (3)** — Alliance, Coldwater, Sandusky now `Needs Review` with a note; parent, name and financials (Sandusky $130,000 / $5,200) preserved. The API accepted the `Needs Review` status value.
* **Field updates (15)** — renames (Chesterton, Chagrin Falls, Willow Creek/Portage, Ashland, Grove City), Portsmouth ZIP 45626→45662, Ashtabula PO Box → 3156 W Prospect Rd, and 12 phone corrections. All remain under Bellhaven with unchanged financials.

## Idempotency

| Run | new | existing | superseded | stale | CRM writes |
|---|---|---|---|---|---|
| scan #1 (baseline) | 43 | 0 | 0 | 0 | 0 |
| scan #2 | 0 | 43 | 0 | 0 | 0 |
| scan #3 (after PO Box / `Pk` refinements) | 2 | 41 | 0 | 2 | 0 |
| scan #4 | 0 | 43 | 0 | 0 | 0 |
| scan after 5 approvals | 5 | 38 | 0 | 5 | 0 |
| after all 27 approvals, scans #1–#2 | 0 | 39 | 0 | 0 | 0 |
| final submission scans #1–#2 (after the Union Square reviewer action) | 0 | 39 | 0 | 0 | 0 |

After approvals the scan regenerates only `NO_CHANGE` (33) and `MANUAL_REVIEW` (6) items; applied proposals keep `approved/applied` and are never re-proposed (the applied CHOW pairs are recognized through `chow_current_account`, the duplicate through `duplicate_of_account`, the Needs Review items through status + note marker). The acknowledged Union Square item stays acknowledged and the reviewer-created proposal is never staled.

## Remaining exceptions (manual review)

| Case | Evidence (live) | Recommendation |
|---|---|---|
| Monroe — Cedar Trail of Monroe and Monroe Gardens Care Center (Harborview) at 750 Stewart Rd | Bellhaven Gardens of Monroe is Active under Bellhaven with the site phone; the two other records are $0/$0, no contacts, no CHOW/duplicate links, other operators' parents | Keep unchanged: prior-operator records of another parent; the Bellhaven record is already correct |
| Erie — Harborview Shores of Erie at 4930 W Lake Rd | Bellhaven Shores of Erie is Active under Bellhaven; site administrator Angela Quinn is its CRM contact; Harborview record $0/$0, no contacts | Keep unchanged |
| Port Clinton — Harborview Nursing & Rehab of Port Clinton at 1420 Harbor Point Dr | Bellhaven of Port Clinton is Active under Bellhaven; administrator Brenda Whitfield is its CRM contact; Harborview record $0/$0 | Keep unchanged |
| Kettering — three accounts at 3313 Wilmington Pike/Pk (Harborview, Cedar Trail, no parent) | All $0/$0, no contacts, none has the site phone; site administrator Priya Ashby is not in the CRM | Needs investigation: which record is the current operator's cannot be determined from available data. Any choice is a direct re-parent (no CHOW: AR = 0); the app's selection control creates the guarded proposal once known |
| Union Square — website 118 Union Square Dr vs CRM Union Square Senior Living, 240 Market St (Juniper Point) | Different street, different phone, different administrator (Dale Croft vs Phil Holloway), different operator; no account exists at the website address | Correct: create a new Bellhaven account at 118 Union Square Dr (same treatment as Amberly Manor); leave the Juniper Point record untouched. The reviewer created this `CREATE_MISSING` proposal in the app; it remains **pending (not applied)** at submission |

## Remaining risks

* The three `Needs Review` accounts need a business decision (sold to another operator, closed, or merely unlisted); nothing was detached or inactivated.
* Phone numbers were corrected to the website's values on 12 accounts; if the CRM phones were deliberately different (e.g. billing lines), those can be reverted from the ledger's before-snapshots.
* Historical records of other operators (Cedar Trail, Harborview) at Bellhaven addresses were intentionally left alone.
