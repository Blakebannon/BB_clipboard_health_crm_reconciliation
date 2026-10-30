# CRM API discovery

Source of truth: `GET https://analyst-assessment-production.up.railway.app/api/openapi.json` (served by the Swagger UI at `/api/docs`), plus GET-only probing with the candidate token. All examples below are sanitized; no token appears anywhere in this repository.

## Verified from the OpenAPI document (no token needed)

| Method | Path | operationId | Notes |
|---|---|---|---|
| GET | `/api/v1/accounts` | `list_accounts` | query: `q`, `city`, `state`, `zip`, `street`, `parent_id`, `page` (default 1), `page_size` (default 50) |
| POST | `/api/v1/accounts` | `create_account` | 201 on success; request body schema **not** declared in the spec |
| GET | `/api/v1/accounts/{account_id}` | `get_account` | |
| PATCH | `/api/v1/accounts/{account_id}` | `update_account` | partial update; body schema not declared |
| GET | `/api/v1/contacts` | `list_contacts` | query: `account_id`, `q`, `page`, `page_size` |
| POST | `/api/v1/contacts` | `create_contact` | not used by this tool |
| GET | `/api/v1/contacts/{contact_id}` | `get_contact` | |
| PATCH | `/api/v1/contacts/{contact_id}` | `update_contact` | not used by this tool |
| GET | `/api/v1/me` | `me` | identifies the candidate token |

* No DELETE routes exist. Duplicates can only be marked, never merged or deleted.
* The OpenAPI document declares **no** response or request schemas (`"schema": {}`), only the `HTTPValidationError` (422) shape. Field names therefore had to be verified against live responses (next section).
* Authentication: `Authorization: Bearer <token>`. Without it every `/api/v1/*` route returns `401 {"detail": "Provide your candidate token as: Authorization: Bearer <token>"}`.
* Error shapes: `{"detail": "..."}` for 401/404, FastAPI `{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}` for 422.

## Verified against live responses (GET only) -- `python -m bellhaven.cli probe`

> **Status: pending.** This section is filled in from the first authenticated `probe` / `scan`. Until then the client reads field names through `bellhaven/config.py` (`FIELD_*` constants with tolerant aliases) so a rename is a one-line change.

Expected account fields (from the sandbox UI labels): `id`, `name`, `parent_id`, `status`, `care_type`, `phone`, `billing_street`, `billing_city`, `billing_state`, `billing_zip`, `lifetime_revenue`, `outstanding_ar`, `chow_current_account`, `duplicate_of_account`, `note`.

_List envelope_: to be recorded (bare list vs `{items,total,page,page_size}`; the client accepts either).

_Enumerations observed_: `status` values and `care_type` values are recorded by `scan` (`observed_statuses`, `observed_care_types` in the scan report) and copied here after the first live run.

## How this tool uses the API

| Purpose | Call | Writes? |
|---|---|---|
| Full CRM read | `GET /accounts?page=N&page_size=50` until a short/empty/repeated page, then `GET /contacts` the same way | no |
| Preflight before any write | `GET /accounts/{id}` for every affected account + `GET /accounts/{bellhaven_parent}` | no |
| Direct re-parent / field update / duplicate / needs-review | one `PATCH /accounts/{id}` with only the changed fields | yes, on approval |
| CHOW | `POST /accounts` (successor) then `PATCH /accounts/{old}` `{"chow_current_account": "<new id>"}` | yes, on approval |
| Read-back verification | `GET /accounts/{id}` for each touched record | no |
| Successor recovery | `GET /accounts?parent_id=<bellhaven>&street=<street>` and scan notes for the marker `[bellhaven-reconciliation] chow-successor-of:<old id>` | no |

Writable fields this tool will ever send (`config.ALLOWED_WRITE_FIELDS`): `name`, `parent_id`, `status`, `care_type`, `phone`, `billing_street`, `billing_city`, `billing_state`, `billing_zip`, `chow_current_account`, `duplicate_of_account`, `note`. Hard-forbidden: `id`, `lifetime_revenue`, `outstanding_ar`.

## Client behaviour

* Explicit timeouts (20 s). GET retried up to 3x on timeouts / 429 / 5xx with backoff and `Retry-After`.
* POST and PATCH are **never** retried automatically. A timeout raises `CRMUncertainWrite`; the apply path then inspects remote state (ledger id, old account's `chow_current_account`, note marker) before deciding anything.
* The client is read-only unless constructed with `allow_writes=True`, which only `bellhaven.apply` does, only inside an approval.
* The token never appears in URLs, logs, exceptions (`_redact`) or `repr`.
