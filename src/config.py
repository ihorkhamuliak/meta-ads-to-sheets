"""Central config: all tunables and constants live here."""
from __future__ import annotations

import os
from dataclasses import dataclass

# ── Meta ──────────────────────────────────────────────────────────────────────
GRAPH_API_BASE = "https://graph.facebook.com"
DEFAULT_GRAPH_VERSION = "v21.0"

# Meta error codes that are transient / safe to retry
RETRYABLE_META_CODES: frozenset[int] = frozenset({1, 2, 613, 80004})
# HTTP status codes to retry
RETRYABLE_HTTP_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

MAX_RETRIES = 7              # покриває ~2 хв ретраїв замість ~30 с (Meta code=2 блимає до кількох хв)
BASE_BACKOFF_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 60.0   # стеля на одну паузу між спробами

# Fields requested from the insights endpoint (campaign level, per day)
INSIGHT_FIELDS = (
    "campaign_id,"
    "campaign_name,"
    "spend,"
    "impressions,"
    "clicks,"
    "ctr,"
    "cpc,"
    "cpm,"
    "reach,"
    "actions"
)

# ── Sheet ─────────────────────────────────────────────────────────────────────
# Column order in the raw tab — edit here only if you need to add/remove columns.
RAW_HEADERS: list[str] = [
    "date",
    "campaign_id",
    "campaign_name",
    "spend",
    "impressions",
    "clicks",
    "ctr",
    "cpc",
    "cpm",
    "reach",
    "actions",        # JSON string: {"purchase": "3", "lead": "7", ...}
]

# Upsert key columns (0-based indices into RAW_HEADERS)
KEY_COL_DATE = 0        # "date"
KEY_COL_CAMPAIGN = 1    # "campaign_id"


@dataclass(frozen=True)
class Config:
    # Meta
    access_token: str
    ad_account_id: str          # e.g. act_123456789
    graph_api_version: str

    # Google
    google_service_account_json: str   # JSON string OR path to .json file
    google_sheet_id: str               # залишено порожнім — більше не використовується

    # Tunables
    lookback_days: int
    raw_tab_name: str
    summary_tab_name: str

    # Client tracker sheet (optional — leave empty to skip)
    client_sheet_id: str        # ID таблиці клієнта (AD tracker)
    client_date_col: int        # номер колонки з датою (B=2)
    client_spend_col: int       # номер колонки Ad spend (E=5)
    client_tab_name_format: str # формат назви вкладки, напр. "{month} ({year})"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            access_token=_require("META_ACCESS_TOKEN"),
            ad_account_id=_require("META_AD_ACCOUNT_ID"),
            graph_api_version=os.getenv("GRAPH_API_VERSION", DEFAULT_GRAPH_VERSION).strip(),
            google_service_account_json=_require("GOOGLE_SERVICE_ACCOUNT_JSON"),
            google_sheet_id=os.getenv("GOOGLE_SHEET_ID", "").strip(),
            lookback_days=int(os.getenv("LOOKBACK_DAYS", "3")),
            raw_tab_name=os.getenv("RAW_TAB_NAME", "raw").strip(),
            summary_tab_name=os.getenv("SUMMARY_TAB_NAME", "summary").strip(),
            client_sheet_id=os.getenv("CLIENT_SHEET_ID", "").strip(),
            client_date_col=int(os.getenv("CLIENT_DATE_COL", "2")),
            client_spend_col=int(os.getenv("CLIENT_SPEND_COL", "5")),
            client_tab_name_format=os.getenv("CLIENT_TAB_NAME_FORMAT", "{month} ({year})").strip(),
        )


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise EnvironmentError(
            f"Required environment variable {name!r} is not set or empty. "
            "Check your .env file or GitHub Actions secrets."
        )
    return value
