"""Configuration loaded from environment / local .env.

The API token is read from ``BELLHAVEN_API_TOKEN`` only. It is never logged,
never placed in URLs, and ``Settings.__repr__`` redacts it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

# Load .env from the repository root (if present). Existing process env wins.
load_dotenv(REPO_ROOT / ".env", override=False)

DEFAULT_BASE_URL = "https://analyst-assessment-production.up.railway.app"

# CRM field names as observed in the sandbox UI / documented in the plan.
# They are localized here so a rename after live verification is one edit.
# See docs/API_DISCOVERY.md for the verified list.
FIELD_ID = "id"
FIELD_NAME = "name"
FIELD_PARENT = "parent_id"
FIELD_STATUS = "status"
FIELD_CARE_TYPE = "care_type"
FIELD_PHONE = "phone"
FIELD_STREET = "billing_street"
FIELD_CITY = "billing_city"
FIELD_STATE = "billing_state"
FIELD_ZIP = "billing_zip"
FIELD_REVENUE = "lifetime_revenue"
FIELD_AR = "outstanding_ar"
FIELD_CHOW = "chow_current_account"
FIELD_DUP = "duplicate_of_account"
FIELD_NOTE = "note"

# Alternate spellings seen in the wild for the same concepts; the model loader
# tolerates these so a field-name surprise degrades gracefully instead of
# silently reading None.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    FIELD_ID: ("id", "account_id"),
    FIELD_NAME: ("name", "account_name"),
    FIELD_PARENT: ("parent_id", "parent_account_id", "parent"),
    FIELD_STATUS: ("status", "account_status"),
    FIELD_CARE_TYPE: ("care_type", "caretype", "care"),
    FIELD_PHONE: ("phone", "phone_number"),
    FIELD_STREET: ("billing_street", "street", "address", "address_street"),
    FIELD_CITY: ("billing_city", "city"),
    FIELD_STATE: ("billing_state", "state"),
    FIELD_ZIP: ("billing_zip", "zip", "postal_code", "zip_code"),
    FIELD_REVENUE: ("lifetime_revenue", "revenue"),
    FIELD_AR: ("outstanding_ar", "ar", "accounts_receivable"),
    FIELD_CHOW: ("chow_current_account", "chow_current_account_id"),
    FIELD_DUP: ("duplicate_of_account", "duplicate_of_account_id", "duplicate_of"),
    FIELD_NOTE: ("note", "notes"),
}

# Fields this tool is ever allowed to send in a PATCH/POST body.
ALLOWED_WRITE_FIELDS = frozenset(
    {
        FIELD_NAME,
        FIELD_PARENT,
        FIELD_STATUS,
        FIELD_CARE_TYPE,
        FIELD_PHONE,
        FIELD_STREET,
        FIELD_CITY,
        FIELD_STATE,
        FIELD_ZIP,
        FIELD_CHOW,
        FIELD_DUP,
        FIELD_NOTE,
    }
)

# Fields that must NEVER be written by this tool.
FORBIDDEN_WRITE_FIELDS = frozenset({FIELD_ID, FIELD_REVENUE, FIELD_AR})

STATUS_ACTIVE = "Active"
STATUS_INACTIVE = "Inactive"
STATUS_NEEDS_REVIEW = "Needs Review"

NOTE_TAG = "[bellhaven-reconciliation]"


@dataclass
class Settings:
    base_url: str = DEFAULT_BASE_URL
    api_token: str | None = None
    db_path: Path = field(default_factory=lambda: REPO_ROOT / "data" / "bellhaven.sqlite3")
    parent_name: str = "Bellhaven Senior Living"
    site_expected_count: int = 35
    http_timeout: float = 20.0
    page_size: int = 50

    @property
    def api_base(self) -> str:
        return self.base_url.rstrip("/") + "/api/v1"

    @property
    def has_token(self) -> bool:
        return bool(self.api_token and self.api_token.strip())

    def require_token(self) -> str:
        if not self.has_token:
            raise RuntimeError(
                "BELLHAVEN_API_TOKEN is not configured. Put it in a local .env file "
                "(see .env.example) or set it in your environment. It is never stored in the repo."
            )
        return self.api_token.strip()  # type: ignore[union-attr]

    def __repr__(self) -> str:  # never leak the token
        return (
            f"Settings(base_url={self.base_url!r}, api_token={'<set>' if self.has_token else None}, "
            f"db_path={str(self.db_path)!r}, parent_name={self.parent_name!r})"
        )


def get_settings() -> Settings:
    base = os.environ.get("BELLHAVEN_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
    token = os.environ.get("BELLHAVEN_API_TOKEN")
    db_env = os.environ.get("BELLHAVEN_DB_PATH")
    db_path = Path(db_env) if db_env else REPO_ROOT / "data" / "bellhaven.sqlite3"
    if not db_path.is_absolute():
        db_path = REPO_ROOT / db_path
    expected = int(os.environ.get("BELLHAVEN_SITE_EXPECTED_COUNT", "35") or 35)
    return Settings(
        base_url=base,
        api_token=token,
        db_path=db_path,
        parent_name=os.environ.get("BELLHAVEN_PARENT_NAME", "Bellhaven Senior Living"),
        site_expected_count=expected,
    )
