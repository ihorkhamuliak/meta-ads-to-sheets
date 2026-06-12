"""Entry point — run as:  python -m src.main"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Load .env for local runs; in CI the env vars are injected directly by the runner.
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=False)
except ImportError:
    pass   # python-dotenv optional; not installed in some minimal envs

from .config import Config
from .meta_client import MetaAPIError, MetaClient, MetaTokenError
from .sheets_client import SheetsClient


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )


def run() -> None:
    _setup_logging()
    log = logging.getLogger(__name__)

    # ── Config ────────────────────────────────────────────────────────────────
    try:
        config = Config.from_env()
    except EnvironmentError as exc:
        logging.critical("Configuration error: %s", exc)
        sys.exit(1)

    log.info(
        "Starting Meta → Sheets sync  account=%s  lookback=%d days  sheet=%s",
        config.ad_account_id,
        config.lookback_days,
        config.google_sheet_id,
    )

    # ── Fetch from Meta ───────────────────────────────────────────────────────
    meta = MetaClient(config)
    try:
        rows = meta.fetch_insights(config.lookback_days)
    except MetaTokenError as exc:
        log.critical("Token error — ACTION REQUIRED: %s", exc)
        sys.exit(2)
    except MetaAPIError as exc:
        log.critical("Meta API error: %s", exc)
        sys.exit(3)

    if not rows:
        log.warning(
            "Meta returned zero rows for the requested window. "
            "Check that the ad account has active campaigns in this period."
        )
        # Exit 0 — not an error; nothing to write.
        return

    try:
        sheets = SheetsClient(config)

        # ── Власна таблиця raw/summary (необов'язково) ────────────────────
        if config.google_sheet_id:
            sheets.ensure_tabs()
            sheets.upsert_rows(rows)

        # ── Таблиця клієнта ───────────────────────────────────────────────
        if config.client_sheet_id:
            sheets.write_daily_totals_to_client_sheet(rows, config.client_sheet_id)

    except Exception as exc:
        log.critical("Помилка запису в таблицю: %s", exc, exc_info=True)
        sys.exit(4)

    log.info("Sync complete.")


if __name__ == "__main__":
    run()
