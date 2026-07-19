"""Meta Marketing API client — insights only, no heavy SDK."""
from __future__ import annotations

import json
import logging
import random
import time
from datetime import date, timedelta
from typing import Any, Generator

import requests

from .config import (
    BASE_BACKOFF_SECONDS,
    GRAPH_API_BASE,
    INSIGHT_FIELDS,
    MAX_BACKOFF_SECONDS,
    MAX_RETRIES,
    RETRYABLE_HTTP_CODES,
    RETRYABLE_META_CODES,
    Config,
)

logger = logging.getLogger(__name__)

# Meta error codes that mean the token is invalid / expired / missing permissions
_TOKEN_ERROR_CODES: frozenset[int] = frozenset({190, 200, 294})
_TOKEN_ERROR_SUBCODES: frozenset[int] = frozenset({458, 459, 460, 463, 464, 467})


class MetaAPIError(Exception):
    """Unrecoverable Meta API error."""


class MetaTokenError(MetaAPIError):
    """Access token is expired, revoked, or lacks required permissions."""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _date_range(lookback_days: int) -> tuple[str, str]:
    """
    Return (since, until) covering the last *lookback_days* complete days.
    'Today' is intentionally excluded — current-day data is partial.
    """
    today = date.today()
    until = today - timedelta(days=1)
    since = today - timedelta(days=lookback_days)
    return since.isoformat(), until.isoformat()


def _backoff(attempt: int) -> float:
    """Exponential back-off with ±25 % jitter so parallel runs don't pile up."""
    delay = min(BASE_BACKOFF_SECONDS * (2 ** attempt), MAX_BACKOFF_SECONDS)
    jitter = delay * 0.25 * (2 * random.random() - 1)
    return max(1.0, delay + jitter)


def _classify_error(data: dict) -> tuple[bool, bool]:
    """
    Returns (is_token_error, is_retryable).
    Inspects the top-level 'error' dict from a Meta JSON response.
    """
    err = data.get("error", {})
    code = int(err.get("code", 0))
    subcode = int(err.get("error_subcode", 0))
    is_token = code in _TOKEN_ERROR_CODES or subcode in _TOKEN_ERROR_SUBCODES
    is_retryable = code in RETRYABLE_META_CODES
    return is_token, is_retryable


# ── Client ────────────────────────────────────────────────────────────────────

class MetaClient:
    def __init__(self, config: Config) -> None:
        self._token = config.access_token
        self._account_id = config.ad_account_id
        self._version = config.graph_api_version
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    # ── Internal request machinery ────────────────────────────────────────────

    def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """Single GET with retry / back-off. Never logs the token."""
        safe_params = {**params, "access_token": self._token}

        for attempt in range(MAX_RETRIES):
            try:
                resp = self._session.get(url, params=safe_params, timeout=30)
            except requests.RequestException as exc:
                if attempt == MAX_RETRIES - 1:
                    raise MetaAPIError(
                        f"Network error after {MAX_RETRIES} attempts: {exc}"
                    ) from exc
                wait = _backoff(attempt)
                logger.warning(
                    "Network error (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1, MAX_RETRIES, wait, exc,
                )
                time.sleep(wait)
                continue

            if resp.status_code in RETRYABLE_HTTP_CODES:
                if attempt == MAX_RETRIES - 1:
                    resp.raise_for_status()
                wait = _backoff(attempt)
                logger.warning(
                    "HTTP %d (attempt %d/%d), retrying in %.1fs",
                    resp.status_code, attempt + 1, MAX_RETRIES, wait,
                )
                time.sleep(wait)
                continue

            try:
                data: dict = resp.json()
            except ValueError:
                resp.raise_for_status()
                raise MetaAPIError(f"Non-JSON response: {resp.text[:300]}")

            if "error" in data:
                is_token, is_retryable = _classify_error(data)
                err_detail = data["error"]

                if is_token:
                    raise MetaTokenError(
                        "Meta token is expired or lacks required permissions. "
                        "Regenerate a System User token with ads_read + read_insights "
                        "and update the META_ACCESS_TOKEN secret. "
                        f"API error: {err_detail}"
                    )

                if is_retryable:
                    if attempt == MAX_RETRIES - 1:
                        raise MetaAPIError(
                            f"Meta API error after {MAX_RETRIES} attempts: {err_detail}"
                        )
                    wait = _backoff(attempt)
                    logger.warning(
                        "Retryable Meta error code=%s (attempt %d/%d), retrying in %.1fs",
                        err_detail.get("code"), attempt + 1, MAX_RETRIES, wait,
                    )
                    time.sleep(wait)
                    continue

                raise MetaAPIError(
                    f"Meta API error (code {err_detail.get('code')}): {err_detail}"
                )

            return data

        raise MetaAPIError(f"Exhausted {MAX_RETRIES} retries without a successful response")

    def _paginate(
        self, url: str, params: dict[str, Any]
    ) -> Generator[dict[str, Any], None, None]:
        """Yield successive pages; handles cursor-based pagination automatically."""
        while url:
            page = self._get(url, params)
            yield page
            # After the first request the full URL (with token) is in paging.next;
            # we strip the token from params to avoid doubling it.
            next_url: str | None = page.get("paging", {}).get("next")
            url = next_url or ""
            params = {}   # next_url already encodes all query parameters

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch_insights(self, lookback_days: int) -> list[dict[str, Any]]:
        """
        Fetch campaign-level insights for the last *lookback_days* complete days.
        Returns a flat list of normalised dicts ready for the sheet.
        """
        since, until = _date_range(lookback_days)
        logger.info(
            "Fetching Meta insights: account=%s  %s → %s",
            self._account_id, since, until,
        )

        url = f"{GRAPH_API_BASE}/{self._version}/{self._account_id}/insights"
        params: dict[str, Any] = {
            "level": "campaign",
            "fields": INSIGHT_FIELDS,
            "time_increment": 1,    # one row per calendar day
            "time_range": json.dumps({"since": since, "until": until}),
            "limit": 500,
        }

        rows: list[dict[str, Any]] = []
        for page in self._paginate(url, params):
            for item in page.get("data", []):
                rows.append(_normalise(item))

        logger.info("Fetched %d insight rows from Meta", len(rows))
        return rows


def _normalise(item: dict[str, Any]) -> dict[str, Any]:
    """Flatten a single insights record into the shape expected by SheetsClient."""
    actions_raw: list[dict] = item.get("actions", [])
    # Compact JSON keeps the column width manageable and is trivially parseable later.
    actions_str = (
        json.dumps(
            {a["action_type"]: a["value"] for a in actions_raw},
            ensure_ascii=False,
        )
        if actions_raw
        else ""
    )

    return {
        "date":          item.get("date_start", ""),
        "campaign_id":   item.get("campaign_id", ""),
        "campaign_name": item.get("campaign_name", ""),
        "spend":         item.get("spend", "0"),
        "impressions":   item.get("impressions", "0"),
        "clicks":        item.get("clicks", "0"),
        "ctr":           item.get("ctr", "0"),
        "cpc":           item.get("cpc", "0"),
        "cpm":           item.get("cpm", "0"),
        "reach":         item.get("reach", "0"),
        "actions":       actions_str,
    }
