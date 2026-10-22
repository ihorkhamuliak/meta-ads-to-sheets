"""Офлайн-перевірка двох запобіжників (22.10.2026), без мережі й секретів. Запуск: python tests/test_guards.py"""
import sys, types
from pathlib import Path
from datetime import date
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gspread
from src import sheets_client, main

# ── Фейкова вкладка: B = серійники дат, E = витрати, F:G = формули ─────────────
class FakeWs:
    def __init__(self, e):
        self.e = dict(e)          # row -> value
        self.reads = 0
    def get_values(self, rng, value_render_option=None):
        if rng == "A1:B":
            return [["День", "Дата"]]
        return [[(d - date(1899, 12, 30)).days] for d in DAYS]
    def batch_get(self, ranges, value_render_option=None):
        out = []
        for r in ranges:
            if r.startswith("E"):
                v = self.e.get(int(r[1:]), "")
                out.append([[v]] if v != "" else [])
            else:
                out.append([["=E/13", "=E*2"]])
        return out
    def batch_update(self, data, value_input_option=None):
        for x in data:
            self.e[int(x["range"][1:])] = x["values"][0][0]

DAYS = [date(2026, 10, 18), date(2026, 10, 19), date(2026, 10, 20)]   # рядки 2, 3, 4
RATES = {d: 3.9 for d in DAYS}

def tracker(ws):
    sc = object.__new__(sheets_client.SheetsClient)
    sc._client_spend_col = 5
    sc._gc = types.SimpleNamespace(open_by_key=lambda _: types.SimpleNamespace(worksheet=lambda _: ws))
    return sc

fails = 0
def check(name, ok):
    global fails
    print(("OK   " if ok else "FAIL ") + name)
    fails += not ok

# 1. Повтор 21.10: Meta дала нулі, у 18.10 вже 30.26
ws = FakeWs({2: 30.26})
err = None
try:
    tracker(ws).write_usd_to_tracker({d: 0.0 for d in DAYS}, RATES, "x", "Реклама")
except RuntimeError as e:
    err = e
check("18.10 не затерто нулем (лишилось 30.26)", ws.e.get(2) == 30.26)
check("порожні 19-20.10 записано як 0", ws.e.get(3) == 0 and ws.e.get(4) == 0)
check("прогін впав гучно", err is not None and "18.10.2026" in str(err))

# 2. Звичайний день не зачеплено
ws = FakeWs({2: 0, 3: ""})
tracker(ws).write_usd_to_tracker({DAYS[0]: 39.0, DAYS[1]: 0.0, DAYS[2]: 78.0}, RATES, "x", "Реклама")
check("0 → гроші і '' → 0 пишуться без падіння", ws.e == {2: 10.0, 3: 0, 4: 20.0})

# 3. Повтор при 503 і без повтору при 403
def api_error(code):
    resp = mock.Mock(status_code=code)
    resp.json.return_value = {"error": {"code": code, "message": "x", "status": "x"}}
    return gspread.exceptions.APIError(resp)

def run_main(side_effect):
    cfg = types.SimpleNamespace(ad_account_id="a", lookback_days=3, google_sheet_id="", client_sheet_id="s",
                                client_mode="tracker_usd", client_tab_name="Реклама", dry_run=False)
    meta = mock.Mock()
    meta.fetch_insights.return_value = [{"date": "2026-10-18", "spend": "39"}]
    meta.fetch_account_currency.return_value = "PLN"
    sheets = mock.Mock()
    sheets.write_usd_to_tracker.side_effect = side_effect
    with mock.patch.object(main.Config, "from_env", return_value=cfg), \
         mock.patch.object(main, "MetaClient", return_value=meta), \
         mock.patch.object(main, "SheetsClient", return_value=sheets), \
         mock.patch.object(main, "pln_per_usd", return_value=RATES), \
         mock.patch.object(main.time, "sleep") if hasattr(main, "time") else mock.MagicMock():
        try:
            main.run()
            code = 0
        except SystemExit as e:
            code = e.code
    return code, sheets.write_usd_to_tracker.call_count

code, calls = run_main([api_error(503), None])
check(f"503 один раз → повтор і успіх (exit {code}, викликів {calls})", code in (0, None) and calls == 2)
code, calls = run_main([api_error(403)])
check(f"403 → без повтору, exit 4 (exit {code}, викликів {calls})", code == 4 and calls == 1)
code, calls = run_main([api_error(503)] * 10)
check(f"503 постійно → здається після 4 спроб, exit 4 (exit {code}, викликів {calls})", code == 4 and calls == 4)

print("\nпадінь:", fails)
sys.exit(1 if fails else 0)
