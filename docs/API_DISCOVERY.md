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

Verified 2026-10-30 with the candidate token; zero writes were made.

**`GET /api/v1/me`** → `{"candidate": "...", "message": "..."}`

**List envelope** (accounts and contacts): `{"data": [...], "page": 1, "page_size": 50, "total": 121}`. Pagination is `page`/`page_size`; the client stops on a short page, an empty page, `total` reached, or a repeated id.

**Account record** (identical shape in list and detail responses):

| Field | Type | Notes |
|---|---|---|
| `account_id` | str | identifier (not `id`) |
| `name` | str | |
| `parent_id` | str | `""` when top-level; the tool treats empty as `None` |
| `parent_name` | str | read-only convenience |
| `billing_street`, `billing_city`, `billing_state`, `billing_zip` | str | one record uses a `PO Box` street; several top-level parent accounts have blank addresses |
| `care_type` | str | observed: `""`, `Assisted Living`, `Independent Living`, `Memory Care`, `Skilled Nursing` |
| `status` | str | observed: `Active`, `Inactive` (`Needs Review` has not been observed on any existing record; it is the status the assessment specifies for site-absent accounts and will be proven on the first approved apply) |
| `phone` | str | |
| `lifetime_revenue`, `outstanding_ar` | int | parsed as `Decimal`; missing/unparseable is treated as unknown, never zero |
| `chow_current_account`, `duplicate_of_account` | str | `""` when unset |
| `note` | str | |
| `created_by_candidate` | bool | read-only |
| `updated_at` | str | read-only, e.g. `2026-10-30 20:02:46Z` |

Sanitized example:

```json
{"account_id": "001XXXXXXXXXXXXXXX", "name": "Bellhaven of Example", "parent_id": "0015QAPLGS3FVYEEEM",
 "parent_name": "Bellhaven Senior Living (Parent Account)", "billing_street": "123 Example Rd", "billing_city": "Example",
 "billing_state": "OH", "billing_zip": "43000", "care_type": "Skilled Nursing", "status": "Active", "phone": "(555) 555-0100",
 "lifetime_revenue": 0, "outstanding_ar": 0, "chow_current_account": "", "duplicate_of_account": "", "note": "",
 "created_by_candidate": false, "updated_at": "2026-10-30 20:02:46Z"}
```

**Contact record**: `contact_id`, `account_id`, `name`, `title`, `email`, `phone`, `is_active`, `created_by_candidate`, `updated_at`. 56 contacts exist; they are used only as corroborating evidence (administrator name match).

**Top-level parents observed**: Bellhaven Senior Living, Cedar Trail Communities, Harborview Care Group, Juniper Point Healthcare, Millstone Health Partners, Stonebridge Eldercare (all suffixed "(Parent Account)"), plus a number of parentless facility accounts (e.g. Bellhaven Meadows of Findlay, Kettering Nursing & Rehabilitation).

**POST / PATCH bodies**: not declared in the spec. The tool sends only the field names above (`config.ALLOWED_WRITE_FIELDS`) and reads the record back by `GET` to verify; a `422` is surfaced as a failed apply with the server's `detail`.

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
