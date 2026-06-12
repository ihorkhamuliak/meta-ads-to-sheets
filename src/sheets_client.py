"""Google Sheets client — upsert raw data and initialise summary formulas."""
from __future__ import annotations

import json
import logging
import os
from datetime import date as Date
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

from .config import Config, KEY_COL_CAMPAIGN, KEY_COL_DATE, RAW_HEADERS

_MONTH_EN = {
    1: "January", 2: "February", 3: "March",    4: "April",
    5: "May",     6: "June",     7: "July",      8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ── Summary tab formulas ──────────────────────────────────────────────────────
# QUERY column references match RAW_HEADERS:
#   Col A = date, B = campaign_id, C = campaign_name, D = spend
#
# Two tables placed SIDE BY SIDE (A vs D) so their spill ranges never overlap.

_q_totals = (
    "SELECT A, SUM(D) WHERE A IS NOT NULL "
    "GROUP BY A ORDER BY A DESC "
    "LABEL A 'Date', SUM(D) 'Total Spend'"
)
_FORMULA_DAILY = f'=QUERY(raw!A:K,"{_q_totals}",1)'

_q_campaign = (
    "SELECT A, B, C, SUM(D) WHERE A IS NOT NULL "
    "GROUP BY A, B, C ORDER BY A DESC, SUM(D) DESC "
    "LABEL A 'Date', B 'Campaign ID', C 'Campaign', SUM(D) 'Spend'"
)
_FORMULA_CAMPAIGN = f'=QUERY(raw!A:K,"{_q_campaign}",1)'


# ── Credential loading ────────────────────────────────────────────────────────

def _load_credentials(json_val: str) -> Credentials:
    """
    Accept either:
    - a file-system path ending in .json  →  read the file
    - a raw JSON string                   →  parse directly
    This covers both local dev (file path) and CI (JSON pasted into a secret).
    """
    stripped = json_val.strip()
    if os.path.isfile(stripped):
        with open(stripped, encoding="utf-8") as fh:
            info = json.load(fh)
    else:
        info = json.loads(stripped)
    return Credentials.from_service_account_info(info, scopes=SCOPES)


# ── Sheet helpers ─────────────────────────────────────────────────────────────

def _col_letter(n: int) -> str:
    """1-based column index → letter(s).  1→A, 26→Z, 27→AA, …"""
    result = ""
    while n:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


def _get_or_create_worksheet(
    spreadsheet: gspread.Spreadsheet, title: str, rows: int = 5000
) -> gspread.Worksheet:
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(
            title=title, rows=rows, cols=len(RAW_HEADERS) + 4
        )


def _record_to_row(record: dict[str, Any]) -> list[str]:
    return [str(record.get(h, "")) for h in RAW_HEADERS]


# ── Main client ───────────────────────────────────────────────────────────────

class SheetsClient:
    def __init__(self, config: Config) -> None:
        creds = _load_credentials(config.google_service_account_json)
        self._gc = gspread.authorize(creds)
        self._spreadsheet = (
            self._gc.open_by_key(config.google_sheet_id)
            if config.google_sheet_id else None
        )
        self._raw_name = config.raw_tab_name
        self._summary_name = config.summary_tab_name
        self._client_date_col = config.client_date_col
        self._client_spend_col = config.client_spend_col
        self._client_tab_fmt = config.client_tab_name_format

    def ensure_tabs(self) -> None:
        """Create tabs with headers / formulas on first run; no-op afterwards."""
        self._ensure_raw_tab()
        self._ensure_summary_tab()

    def upsert_rows(self, rows: list[dict[str, Any]]) -> None:
        """
        Idempotent upsert keyed on (date, campaign_id).
        - Matching rows are overwritten in-place (captures Meta attribution updates).
        - New (date, campaign_id) pairs are appended.
        - Batches all writes to minimise API round-trips.
        """
        if not rows:
            logger.info("No rows to upsert — skipping sheet write.")
            return

        ws = self._spreadsheet.worksheet(self._raw_name)

        # Read the entire sheet once
        existing: list[list[str]] = ws.get_all_values()
        if not existing:
            ws.append_row(RAW_HEADERS, value_input_option="RAW")
            existing = [RAW_HEADERS]

        # Build a (date, campaign_id) → 1-based row-number index (skip header row 1)
        key_to_row: dict[tuple[str, str], int] = {}
        for sheet_row_idx, row in enumerate(existing[1:], start=2):
            if len(row) > KEY_COL_CAMPAIGN:
                key = (row[KEY_COL_DATE], row[KEY_COL_CAMPAIGN])
                key_to_row[key] = sheet_row_idx

        last_col = _col_letter(len(RAW_HEADERS))
        updates: list[dict] = []
        appends: list[list[str]] = []

        for record in rows:
            key = (record["date"], record["campaign_id"])
            flat = _record_to_row(record)

            if key in key_to_row:
                row_num = key_to_row[key]
                updates.append({
                    "range": f"A{row_num}:{last_col}{row_num}",
                    "values": [flat],
                })
                logger.debug("Will update row %d  key=%s", row_num, key)
            else:
                appends.append(flat)
                logger.debug("Will append key=%s", key)

        if updates:
            ws.batch_update(updates, value_input_option="USER_ENTERED")
            logger.info("Updated %d existing rows", len(updates))

        if appends:
            ws.append_rows(appends, value_input_option="USER_ENTERED")
            logger.info("Appended %d new rows", len(appends))

    def write_daily_totals_to_client_sheet(
        self, rows: list[dict[str, Any]], client_sheet_id: str
    ) -> None:
        """
        Записує суму витрат по всіх кампаніях за кожен день
        у таблицю клієнта (колонка Ad spend, пошук рядка по даті).
        """
        # Сумуємо spend по датах
        daily: dict[str, float] = {}
        for row in rows:
            daily[row["date"]] = daily.get(row["date"], 0.0) + float(row["spend"] or 0)

        client_sheet = self._gc.open_by_key(client_sheet_id)

        for date_iso, total in daily.items():
            dt = Date.fromisoformat(date_iso)
            tab_name = self._client_tab_fmt.format(
                month=_MONTH_EN[dt.month], year=dt.year
            )
            # Формат дати як у таблиці клієнта: 09.06.2026
            date_formatted = dt.strftime("%d.%m.%Y")

            try:
                ws = client_sheet.worksheet(tab_name)
            except gspread.WorksheetNotFound:
                logger.warning("Вкладка %r не знайдена в таблиці клієнта — пропускаємо %s", tab_name, date_iso)
                continue

            # Шукаємо рядок де колонка B = потрібна дата
            date_col_values = ws.col_values(self._client_date_col)
            try:
                row_idx = date_col_values.index(date_formatted) + 1  # 1-based
            except ValueError:
                logger.warning("Дата %s не знайдена у вкладці %r — пропускаємо", date_formatted, tab_name)
                continue

            ws.update_cell(row_idx, self._client_spend_col, round(total, 2))
            logger.info("Записано %.2f zł → %s!%s%d (%s)", total, tab_name,
                        _col_letter(self._client_spend_col), row_idx, date_iso)

    # ── Private tab initialisation ────────────────────────────────────────────

    def _ensure_raw_tab(self) -> None:
        ws = _get_or_create_worksheet(self._spreadsheet, self._raw_name)
        if not ws.row_values(1):
            ws.append_row(RAW_HEADERS, value_input_option="RAW")
            logger.info("Initialised raw tab with headers")

    def _ensure_summary_tab(self) -> None:
        ws = _get_or_create_worksheet(self._spreadsheet, self._summary_name)
        # Always rewrite — formulas are idempotent and this fixes layout on re-runs.
        ws.clear()
        # Daily totals in col A, per-campaign breakdown in col D — side by side
        # so the spill ranges of the two QUERY formulas never overlap.
        ws.update("A1", [["Daily Spend Totals (auto-updated)"], [_FORMULA_DAILY]],
                  value_input_option="USER_ENTERED")
        ws.update("D1", [["Per-Campaign Breakdown (auto-updated)"], [_FORMULA_CAMPAIGN]],
                  value_input_option="USER_ENTERED")
        logger.info("Initialised summary tab with QUERY formulas")
