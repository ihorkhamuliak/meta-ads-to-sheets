"""Курс PLN → USD з NBP (офіційний середній курс Нацбанку Польщі, таблиця A)."""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta

import requests

logger = logging.getLogger(__name__)

NBP_URL = "https://api.nbp.pl/api/exchangerates/rates/a/usd/{since}/{until}/?format=json"
# NBP не публікує курс у вихідні та свята — беремо останній опублікований до дати витрат.
# Довше за 10 днів без курсу не буває; якщо так — це збій, а не свята.
MAX_GAP_DAYS = 10


class FxError(RuntimeError):
    """Курс для потрібної дати недоступний."""


def pln_per_usd(dates: list[date]) -> dict[date, float]:
    """Повертає {дата витрат: скільки zł за 1 $} для кожної дати."""
    since = min(dates) - timedelta(days=MAX_GAP_DAYS)
    until = max(dates)
    url = NBP_URL.format(since=since.isoformat(), until=until.isoformat())

    for attempt in range(4):
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            published = {date.fromisoformat(r["effectiveDate"]): float(r["mid"]) for r in resp.json()["rates"]}
            break
        except (requests.RequestException, ValueError, KeyError) as exc:
            if attempt == 3:
                raise FxError(f"NBP недоступний: {exc}") from exc
            logger.warning("NBP (спроба %d/4): %s", attempt + 1, exc)
            time.sleep(5 * (attempt + 1))

    result: dict[date, float] = {}
    for d in dates:
        earlier = [p for p in published if p <= d]
        if not earlier or (d - max(earlier)).days > MAX_GAP_DAYS:
            raise FxError(f"Немає курсу NBP на {d} або раніше в межах {MAX_GAP_DAYS} днів")
        result[d] = published[max(earlier)]
        logger.info("Курс NBP для %s: %.4f zł/$ (опубліковано %s)", d, result[d], max(earlier))
    return result
