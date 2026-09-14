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

    def write_usd_to_tracker(
        self,
        spend_by_date: dict[Date, float],
        pln_per_usd: dict[Date, float],
        client_sheet_id: str,
        tab_name: str,
        dry_run: bool = False,
    ) -> None:
        """
        Трекер клієнта: одна вкладка, розділ «по днях» під шапкою «День | Дата».
        Пише ЛИШЕ колонку витрат (E) у доларах. Дата в колонці B зберігається числом
        (серійний день), тому рядок шукаємо за числом, а не за текстом.
        Будь-яка невідповідність — виняток, а не пропуск: рядки створені наперед,
        і дописати в кінець означало б покласти дані нижче формул, повз підсумки.
        """
        ws = self._gc.open_by_key(client_sheet_id).worksheet(tab_name)
        spend_col = _col_letter(self._client_spend_col)

        shown = ws.get_values("A1:B", value_render_option="FORMATTED_VALUE")
        header = [i for i, r in enumerate(shown, start=1) if r[:2] == ["День", "Дата"]]
        if len(header) != 1:
            raise RuntimeError(f"Шапка «День | Дата» знайдена {len(header)} разів у {tab_name!r}, очікував 1")

        serials = ws.get_values(f"B{header[0] + 1}:B", value_render_option="UNFORMATTED_VALUE")
        row_by_serial: dict[int, int] = {}
        for i, r in enumerate(serials, start=header[0] + 1):
            if r and isinstance(r[0], (int, float)):
                if int(r[0]) in row_by_serial:
                    raise RuntimeError(f"Дата-серійник {int(r[0])} повторюється в рядках {row_by_serial[int(r[0])]} і {i}")
                row_by_serial[int(r[0])] = i

        plan: list[tuple[int, Date, float, float]] = []
        for d in sorted(spend_by_date):
            serial = (d - Date(1899, 12, 30)).days
            if serial not in row_by_serial:
                raise RuntimeError(f"Рядок для {d:%d.%m.%Y} не знайдено у {tab_name!r} — не пишу нікуди")
            usd = round(spend_by_date[d] / pln_per_usd[d], 2)
            plan.append((row_by_serial[serial], d, spend_by_date[d], usd))

        cells = [f"{spend_col}{row}" for row, *_ in plan]
        neighbours = [f"F{row}:G{row}" for row, *_ in plan]
        before = ws.batch_get(cells, value_render_option="UNFORMATTED_VALUE")
        formulas_before = ws.batch_get(neighbours, value_render_option="FORMULA")

        for (row, d, pln, usd), old in zip(plan, before):
            old_val = old[0][0] if old and old[0] else ""
            logger.info("%s %s%d: було %r → стане %.2f $ (%.2f zł / %.4f)%s", f"{d:%d.%m.%Y}", spend_col, row,
                        old_val, usd, pln, pln_per_usd[d], "  [DRY RUN]" if dry_run else "")
        if dry_run:
            return

        ws.batch_update([{"range": c, "values": [[usd]]} for c, (_, _, _, usd) in zip(cells, plan)],
                        value_input_option="RAW")

        # Доказ запису — перечитане значення і цілі формули поруч, а не «API не впав»
        after = ws.batch_get(cells, value_render_option="UNFORMATTED_VALUE")
        formulas_after = ws.batch_get(neighbours, value_render_option="FORMULA")
        for (row, d, _, usd), got in zip(plan, after):
            got_val = got[0][0] if got and got[0] else None
            if got_val is None or abs(float(got_val) - usd) > 0.005:
                raise RuntimeError(f"{spend_col}{row} ({d}): записав {usd}, перечитав {got_val!r}")
        if [list(map(list, f)) for f in formulas_before] != [list(map(list, f)) for f in formulas_after]:
            raise RuntimeError("Формули F:G змінились після запису — перевір таблицю")
        logger.info("Перевірено після запису: %d клітинок %s, формули F:G цілі", len(plan), spend_col)

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
