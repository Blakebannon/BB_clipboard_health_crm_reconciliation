# Demo checklist (~2 minutes)

> Status 2026-10-30: the actionable queue has been fully reviewed and applied (28 applied, 0 pending, 0 failed). No unapplied approval candidate remains. For the recording, show: a CHOW item (Marietta or Tiffin) under decision filter *approved* with its read-back verification JSON; the Owosso duplicate; the Kettering manual-review item as the "reject / insufficient evidence" example (its *Mark reviewed* control is local-only); and `python -m bellhaven.cli verify` or the CRM browser for the live state. Applied items are visible in the queue under decision filter *approved*.

Before recording:

- [ ] `.env` contains `BELLHAVEN_API_TOKEN` (never show the file).
- [ ] `python -m bellhaven.cli scan` ran successfully (see sidebar "Last scan").
- [ ] `streamlit run app.py` is open at http://localhost:8501.
- [ ] If you will show the sandbox's read-only CRM browser, open it in a **full-screen** window with the address bar hidden (the URL contains the token).
- [ ] Decide which items you will use (keep one straightforward approval unspent until recording starts):
  - **Evidence walk-through:** a `CHOW_SUCCESSOR` item (Marietta or Tiffin) -- show the red CHOW banner, the revenue/AR values, the two API steps, and that the old account's parent is untouched.
  - **Approval:** a `REPARENT` item (Lima or Findlay: revenue > 0 but AR = 0, so no CHOW) or an `UPDATE_FIELDS` rename (Chesterton / Chagrin Falls / Portage).
  - **Rejection:** the `MANUAL_REVIEW` Union Square item (different street) or the Amberly Manor `CREATE_MISSING` item (show the Colorado account listed as *not eligible* with `STATE_MISMATCH`).

Recording script:

1. (0:00) Top counters: website facilities, CRM accounts, pending queue. Filter to `CHOW_SUCCESSOR`; open Marietta. Point at the CHOW warning, the before/after table (old account changes only `chow_current_account`), and the successor payload. Do not approve yet (or do, if you want the CHOW to be the demonstrated write).
2. (0:40) Open the approval candidate. Show website evidence with the source link, the current CRM record, the proposed `parent_id` change, tick the confirmation box, click **Approve & apply**. Show the success banner and the *Read-back verification* JSON (parent now Bellhaven).
3. (1:15) Open the rejection candidate. Show the conflicting evidence (name similar, street different / state different). Type a reason, click **Reject**. Show it leaves the pending queue.
4. (1:35) Switch to the CRM browser (address bar hidden) or run `python -m bellhaven.cli verify` in a terminal: the approved account now has the Bellhaven parent.
5. (1:50) Click **Run read-only scan now** (or run `scan` in the terminal): counts refresh, the approved and rejected items do not reappear as pending, `crm_writes=0`.

After recording:

- [ ] Continue approving/rejecting the remaining queue in the app.
- [ ] `python -m bellhaven.cli verify` -> update `docs/RESULTS.md` with real before/after counts.
- [ ] Check the recording never shows the token (`.env`, address bar, terminal env output).
