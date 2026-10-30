# Matching and proposal rules

This document explains *why* the reconciliation engine decides what it decides. Everything here is deterministic and lives in `bellhaven/normalize.py`, `bellhaven/matching.py` and `bellhaven/rules.py`. No LLM is used at runtime.

## Evidence hierarchy

| Rank | Evidence | Role |
|---|---|---|
| 1 | Street number + canonical street + state + ZIP (`AddressKey.strict`) | **Primary identity.** Exact equality -> `HIGH_ADDRESS_EXACT`. |
| 1b | Same as above but city instead of ZIP when one side lacks a ZIP (`AddressKey.loose`) | Same tier, reason `ZIP_MISSING_CITY_MATCH`. |
| 2 | Same number + state + (ZIP or city), street similarity >= 80 (RapidFuzz `ratio`) | `MEDIUM_ADDRESS_FUZZY` -- catches typos like *Collegate* vs *Colegate*. A single medium candidate still produces a proposal, labelled medium. |
| 2b | CRM record has no physical street (PO Box / blank), same state + ZIP/city, name similarity >= 85, **and** a phone or administrator-contact match | `MEDIUM_ADDRESS_FUZZY`, reason `CRM_ADDRESS_NOT_A_STREET`; proposes replacing the PO Box with the site's street (Ashtabula). Without corroboration it is shown but not eligible. |
| 3 | Facility-name similarity on *distinctive* tokens (generic words such as *Bellhaven, senior, living, care, centre* are ignored) | Never sufficient on its own. Recorded as `LOW_NAME_ONLY`, `eligible=False`, and routes the facility to `MANUAL_REVIEW`. |
| 4 | Phone and administrator-contact match | Corroboration only (raises confidence, picks duplicate survivors). Cannot override an address contradiction. |

Hard negatives: a different **state**, or a different street number/street, blocks a match regardless of name. *Amberly Manor (Hudson, OH)* therefore never matches *Amberly Manor (Colorado Springs, CO)*; the Colorado record is still listed on the proposal as a rejected candidate with reason `STATE_MISMATCH` so the reviewer sees why.

## Address normalization

* Directionals and suffixes canonicalize to short USPS forms for comparison (`West`->`w`, `Northwest`->`nw`, `Avenue`->`ave`, `Drive`->`dr`, `Road`->`rd`, `Lane`->`ln`, `Street`->`st`).
* `St` is treated as a suffix **only in suffix position**. `45 St Lawrence Dr` is *Saint Lawrence Drive*: it normalizes to `45 st lawrence dr`, equal to `45 Saint Lawrence Drive`, and `expand_street()` renders it as *Saint*, never *Street Lawrence Drive*.
* Unit designators (Suite/Ste/Unit/#) are split off and ignored for identity; ordinals (`1st`) are preserved.
* Original strings are always kept on the records; normalized forms are only for comparison. Cosmetic differences (`Road` vs `Rd`) never generate an update proposal.

## Confidence tiers and reason codes

Tiers are categorical with reason codes, not probabilities: `HIGH_ADDRESS_EXACT`, `MEDIUM_ADDRESS_FUZZY`, `LOW_NAME_ONLY`, `AMBIGUOUS_MULTI_CANDIDATE`, `NO_CANDIDATE`. Typical reason codes: `ADDRESS_EXACT`, `ZIP_MATCH`, `NAME_SIMILAR`/`NAME_DIFFERS`, `PHONE_MATCH`, `ADMIN_CONTACT_MATCH`, `STATE_MISMATCH`, `STREET_MISMATCH`, `MULTIPLE_ACCOUNTS_SAME_ADDRESS`, `CRM_ACCOUNT_CONTESTED`, `WRONG_PARENT`, `CHOW_REQUIRED`, `CHOW_NOT_REQUIRED`, `FINANCIALS_UNPARSEABLE`, `CROSS_PARENT_SAME_ADDRESS_HISTORY`, `BELLHAVEN_CHILD_NOT_ON_SITE`.

## From a match to a proposal

For every website facility, the eligible same-address accounts form a group. The group is partitioned into:

* `current` -- under the Bellhaven parent and not an Inactive duplicate-loser,
* `resolved_history` -- accounts whose `chow_current_account` points at a `current` account (an already-applied CHOW),
* `others` -- accounts under other parents or with no parent.

| Situation | Proposal |
|---|---|
| One active `current` account, fields match | `NO_CHANGE` (informational) |
| One active `current` account, name / care type / phone / missing ZIP differ | `UPDATE_FIELDS` (Chesterton, Chagrin Falls, Portage renames) |
| Two or more active `current` accounts | `DUPLICATE` per loser; survivor chosen by evidence score (phone +3, administrator contact +3, billing history +2, active +1, exact name +1, exact street +1). A tie, or a loser with billing history, becomes `MANUAL_REVIEW`. (Owosso) |
| `current` exists and `others` exist | `MANUAL_REVIEW` for each other account with `CROSS_PARENT_SAME_ADDRESS_HISTORY` -- prior-ownership lineage is **not** an accidental duplicate (Erie, Monroe) |
| No `current`, exactly one active account in `others`, financials parseable | `CHOW_SUCCESSOR` if `requires_chow`, else `REPARENT` (with bundled, separately listed field corrections) |
| No `current`, one in `others`, financials unparseable | `MANUAL_REVIEW` `FINANCIALS_UNPARSEABLE` -- unknown is not zero |
| No `current`, several in `others` | `MANUAL_REVIEW` `MULTI_PARENT_HISTORY` (live Kettering: three accounts at 3313 Wilmington Pike/Pk under Harborview, Cedar Trail and no parent, all $0/$0, no corroboration). The app lets the reviewer select the record that represents the facility; that creates a normal `REPARENT`/`CHOW_SUCCESSOR` proposal (`REVIEWER_SELECTED_CANDIDATE`, origin `reviewer`, never staled by scans) which still goes through approval, preflight and the CHOW re-check. |
| No address match, name-similar account in the same state | `MANUAL_REVIEW` `NAME_MATCH_ADDRESS_CONFLICT` (Union Square: 118 Union Square Dr vs 240 Market St). If the reviewer confirms it is a different facility, the app can create a `CREATE_MISSING` proposal for the website address (`REVIEWER_CONFIRMED_DISTINCT`); the similarly named account is never touched. |
| No address match at all | `CREATE_MISSING` (Amberly Manor, Hudson OH) |
| Bellhaven child, active, not matched to any site facility and not referenced by a manual-review item, **scan complete** | `MISSING_FROM_SITE` -> status `Needs Review` + note. Nothing is detached, deleted or inactivated. Same-address accounts under other operators are listed as evidence (Sandusky / Millstone). |
| Scan incomplete | No `MISSING_FROM_SITE` proposals at all. |

The same CRM account claimed by two different website facilities is `CRM_ACCOUNT_CONTESTED` and goes to manual review for both.

## The CHOW rule

```python
requires_chow = (old_parent_id != target_parent_id) and (lifetime_revenue > 0) and (outstanding_ar > 0)
```

* Evaluated at scan time **and again at apply time on a fresh GET**. If the answer differs from what the proposal assumed, apply is blocked (`CHOW_RULE_CHANGED`).
* Revenue alone is not enough: Findlay ($22,000 / $0) and Lima ($47,000 / $0) are direct re-parents. Marietta ($51,250 / $3,800) and Tiffin ($84,000 / $12,400) are CHOWs.
* A CHOW touches the old account in exactly one field: `chow_current_account`. Parent, status, name, financials and note stay as they are. The successor is created from the website's current details and carries an idempotency marker in its note (`[bellhaven-reconciliation] chow-successor-of:<old id>`), which is how a retry after a timeout or crash finds the already-created successor instead of creating a second one.

## Phone numbers

The website phone is treated as the current public contact number. In the live sandbox 14 matched accounts carry exactly the site phone and 12 carry a different number, which reads as stale CRM data rather than noise, so a differing phone is proposed as a low-risk `UPDATE_FIELDS` change (`PHONE_DIFFERS`), listed as its own line in the diff and bundled with a rename or re-parent when one exists. A phone match never overrides an address contradiction. (Union Square: the name is similar but street, phone and administrator all differ, so it went to manual review; the reviewer confirmed it is a distinct facility and created the missing account.)

## Care offerings

Website offerings are multi-valued; CRM `care_type` is single-valued. Mapping: *Short-Term Rehabilitation & Nursing* -> *Skilled Nursing*, *Memory Support* -> *Memory Care*, *Assisted Living* -> *Assisted Living*. A single offering that differs from the CRM value is proposed as an update. With multiple offerings, an existing CRM value that is one of them is left alone (no churn on list order); an empty CRM value is filled with the first offering; a conflicting value is left unchanged with a risk note. All offerings are always kept in the evidence and, for created accounts, in the note.

## Proposal identity and idempotency

`proposal_key` = SHA-256 of (action type, site slug, sorted CRM ids, target parent, sorted non-note field changes, non-note create payload). Free-text notes are excluded so wording can change without resurrecting decided items. Rescans upsert by key: existing keys keep their decision; keys that disappear are marked *stale* (hidden, never deleted) unless they were applied, since an applied proposal is resolved history and stays visible; a changed material outcome is a new key that *supersedes* the old pending one and is clearly shown as new even if the old one was rejected. Reviewer-created proposals (origin `reviewer`) are never staled by scans.
