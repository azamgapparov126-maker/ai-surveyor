"""
tools/live_check.py (ТЗ 13.10.2026, день 10, п. 10.1): проверка вживую 20 учебных осмотров.

Запуск из корня:
    set PYTHONIOENCODING=utf-8
    sandbox\.venv\Scripts\python.exe tests\test_live_check.py

Без сети и без рабочей базы: поток /act/photos → /act/make → /act/{id}.pdf идёт через ASGI-приложение во временной
копии базы (tests/tmpdb.py), модель и внешние сайты подменены. Проверяется: состав 20 осмотров, регрессия к снимку
(ставка ±0,005 п.п., стоимость в диапазоне, ≤ 2 страниц, ≤ 60 с), цели ТЗ (ТЗ на корректировку 14.10.2026, К4:
затратный метод ±1 сум, минимум 0,05 %, премия = сумма частей, ≥ 8 из 10 полей договора и 12 проверок, K1 —
«нет доступа к источнику» без avtoelon.uz), --snapshot не трогает target, отчёт с двумя строками итога, маршруты
POST /admin/live-check (403 не админу, 409 при двойном запуске) и GET /admin/live-check/last (404 без отчёта).
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

os.environ.pop("SURVEYOR_DEV", None)
os.environ["SURVEYOR_NO_BACKGROUND"] = "1"

from tmpdb import ensure_market_stats, ensure_stat_series, temp_db   # noqa: E402
from app import auth, db, guest, llm                                 # noqa: E402
from app import valuation as V, valuation_sources as vs              # noqa: E402
from app.main import app                                             # noqa: E402
from tools import live_check as lc                                   # noqa: E402

passed, failed = 0, 0


def ok(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ок  ", name)
    else:
        failed += 1
        print("  ПЛОХО", name, str(extra)[:600])


def no_network(*a, **k):
    raise AssertionError("обращение в сеть из теста: %s" % (a[:1],))


class AsgiClient:
    """Тот же интерфейс, что lc.HttpClient.request, но запрос идёт в приложение без сети; cookie — свои."""

    def __init__(self, base: str = "http://test"):
        self.cookies = {}

    def request(self, method, path, data=None, ctype=None):
        u = urlsplit(path)
        body = data or b""
        hdrs = [(b"host", b"test"), (b"content-length", str(len(body)).encode())]
        if ctype:
            hdrs.append((b"content-type", ctype.encode("latin-1")))
        if self.cookies:
            hdrs.append((b"cookie", "; ".join("%s=%s" % kv for kv in self.cookies.items()).encode()))
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
                 "scheme": "http", "path": u.path, "raw_path": u.path.encode(), "root_path": "",
                 "query_string": u.query.encode(), "headers": hdrs, "client": ("203.0.113.7", 0),
                 "server": ("test", 80)}
        out = {"status": None, "chunks": [], "headers": []}

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(msg):
            if msg["type"] == "http.response.start":
                out["status"], out["headers"] = msg["status"], msg.get("headers") or []
            elif msg["type"] == "http.response.body":
                out["chunks"].append(msg.get("body") or b"")

        asyncio.run(app(scope, receive, send))
        heads = {}
        for k, v in out["headers"]:
            k, v = k.decode("latin-1").lower(), v.decode("latin-1")
            heads[k] = v
            if k == "set-cookie":
                name, _, value = v.split(";")[0].partition("=")
                self.cookies[name.strip()] = value.strip()
        return out["status"], b"".join(out["chunks"]), heads


def _strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)


# ------------------------------------------------------------------ 1. состав

def check_cases():
    print("1. Состав 20 осмотров (п. 10.1)")
    ids = [c["id"] for c in lc.CASES]
    groups = {}
    for c in lc.CASES:
        groups.setdefault(c["group"], []).append(c)
    ok("ровно 20 осмотров, номера не повторяются", len(ids) == 20 and len(set(ids)) == 20, ids)
    classes = {"класс 3": 3, "класс 7": 1, "класс 8": 3, "класс 9": 1}
    ok("классы 3, 7, 8, 9: 3 + 1 + 3 + 1", all(len(groups.get(g, [])) == n for g, n in classes.items()),
       {g: len(groups.get(g, [])) for g in classes})
    prods = sorted(c["body"]["must"]["product_code"] for c in groups.get("комплексный", []))
    ok("5 комплексных продуктов с частями, среди них 0305, 0833, 0312",
       len(prods) == 5 and {"0305", "0833", "0312"} <= set(prods)
       and all(len(c["body"]["optional"]["parts"]) >= 2 for c in groups["комплексный"]), prods)
    tests = sorted(c["body"]["must"]["product_code"] for c in groups.get("объект теста", []))
    ok("4 объекта теста: 0301, 0318, 0821, 0701", tests == ["0301", "0318", "0701", "0821"], tests)
    ctr = [c["contract"] for c in groups.get("договор", [])]
    ok("3 учебных договора из tests/fixtures/contracts, файлы на месте",
       len(ctr) == 3 and all((lc.CONTRACTS / n).is_file() and (lc.CONTRACTS / n).stat().st_size > 0 for n in ctr), ctr)
    veh = [c for c in lc.CASES if c["body"].get("must", {}).get("product_code", "").startswith("03")
           and not c["body"]["optional"].get("parts")]
    ok("у объектов класса 3 есть price_new (запасной метод, если avtoelon.uz недоступен)",
       veh and all(c["body"]["optional"].get("price_new") for c in veh), [c["id"] for c in veh])
    sent = [{k: c.get(k) for k in ("body", "vehicle", "fallback_must")} for c in lc.CASES]
    pd = [s for s in _strings(sent) if llm.has_pd(s)]
    ok("учебные данные без ПД (llm.has_pd по всему, что уходит на сервер)", not pd, pd[:3])
    ok("склад 4,2 млрд — продукт 0807, минимум 0,05 %", any(
        c["body"]["must"].get("sum_insured") == 4_200_000_000 and c["body"]["must"]["product_code"] == "0807"
        and c.get("min_rate_pct") == 0.05 for c in lc.CASES))


# ------------------------------------------------------------------ 2. чистые функции и допуски

def check_pure():
    print("2. Ставка, стоимость, страницы, допуски")
    a = {"premium": {"rate_pct": None}, "rate": {"reference_pct": 0.8667,
                                                 "by_parts": [{"class_code": "3", "final_pct": 1.1, "min_pct": 1.1},
                                                              {"class_code": 1, "applied_pct": 0.5}]}}
    ok("ставка договора из частей — справочная (премия / сумма)", lc.rate_of(a) == 0.8667)
    ok("ставки частей по классам", lc.part_rates(a) == [{"class_code": "3", "rate_pct": 1.1, "min_pct": 1.1},
                                                        {"class_code": "1", "rate_pct": 0.5, "min_pct": None}],
       lc.part_rates(a))
    ok("одна ставка акта — premium.rate_pct", lc.rate_of({"premium": {"rate_pct": 0.0854}, "rate": {}}) == 0.0854)
    base = {"input": {"object_value": 100}, "valuation": {"available": True, "V": 110, "method": "cost"}}
    ok("V в пределах 15 % — в допуске", lc.value_check(base)[0] is True)
    ok("V на 16,5 % выше — вне допуска",
       lc.value_check(dict(base, valuation={"available": True, "V": 116.5}))[0] is False)
    rng = {"input": {"object_value": 230}, "valuation": {"available": True, "V": 230, "method": "exchange",
                                                         "range": {"low": 212, "high": 248}}}
    ok("есть диапазон — стоимость внутри", lc.value_check(rng)[0] is True)
    rng2 = dict(rng, input={"object_value": 380})
    ok("есть диапазон — стоимость вне", lc.value_check(rng2)[0] is False and "вне диапазона" in lc.value_check(rng2)[1])
    rng3 = dict(rng, valuation=dict(rng["valuation"], V=150))
    ok("стоимость в диапазоне, а V вне своего диапазона — вне допуска",
       lc.value_check(rng3)[0] is False and "вне своего диапазона" in lc.value_check(rng3)[1], lc.value_check(rng3))
    ok("нет оценки — None с причиной",
       lc.value_check({"valuation": {"available": False, "reason": "нет цены 1 м²"}}) == (None,
                                                                                         "оценки нет (нет цены 1 м²)"))
    res = {"rate_pct": 0.0854, "parts": [], "pages": 1, "seconds": 3.0, "input": {"object_value": 1}, "valuation": None}
    ok("разница ровно 0,005 п.п. — в допуске",
       lc.evaluate(res, {"rate_pct": 0.0854 + 0.005, "parts": []})["status"] == "в допуске")
    ok("разница 0,006 п.п. — вне допуска",
       lc.evaluate(res, {"rate_pct": 0.0854 + 0.006, "parts": []})["status"] == "вне допуска")
    ok("нет эталона — вне допуска", lc.evaluate(res, None)["status"] == "вне допуска")
    ok("3 страницы — вне допуска", lc.evaluate(dict(res, pages=3), {"rate_pct": 0.0854})["status"] == "вне допуска")
    ok("61 с — вне допуска", lc.evaluate(dict(res, seconds=61), {"rate_pct": 0.0854})["status"] == "вне допуска")
    ok("UZ на 3 страницы — вне допуска",
       lc.evaluate(dict(res, pages_lang={"uz": 3}), {"rate_pct": 0.0854})["status"] == "вне допуска")
    pr = dict(res, parts=[{"class_code": "3", "rate_pct": 1.1}, {"class_code": "1", "rate_pct": 0.51}])
    e = lc.evaluate(pr, {"rate_pct": 0.0854, "parts": [{"class_code": "3", "rate_pct": 1.1},
                                                       {"class_code": "1", "rate_pct": 0.5}]})
    ok("часть разошлась на 0,01 п.п. — вне допуска", e["status"] == "вне допуска"
       and any(c["code"] == "parts" and c["ok"] is False for c in e["checks"]), e)
    e = lc.evaluate(dict(res, contract_read=False), {"rate_pct": 0.0854, "contract_read": True})
    ok("договор читался в снимке, а теперь нет — регрессия", e["status"] == "вне допуска", e)
    e = lc.evaluate(res, {"rate_pct": 0.0854, "valuation_method": "cost"})
    ok("в эталоне была оценка, а теперь нет — вне допуска", e["status"] == "вне допуска", e)
    bad = [c for c in e["checks"] if c["ok"] is False]
    ok("у регрессии — поле, ожидание, факт", bad and all(c.get("field") and c.get("expected") is not None
                                                       and "actual" in c for c in bad), bad)
    ok("ошибка потока — «ошибка»", lc.evaluate({"error": "RuntimeError: x", "seconds": 1}, None)["status"] == "ошибка")


# ------------------------------------------------------------------ 2а. цели ТЗ (target) без сервера

def check_targets_pure():
    print("2а. Цели ТЗ: поле, ожидание, факт; «нет доступа к источнику»")
    T = lc.default_targets()
    ok("цели заданы у всех 20 осмотров, у всех PDF ≤ 2 и ≤ 60 с", set(T) == {c["id"] for c in lc.CASES}
       and all(t["pdf_pages_max"] == 2 and t["seconds_max"] == 60 for t in T.values()), T)
    fx = lc.load_expected(lc.EXPECTED)
    ok("файл эталонов: у каждого осмотра snapshot и target", fx and set(fx["cases"]) == set(T)
       and all(c.get("snapshot") and c.get("target") for c in fx["cases"].values()), list((fx or {}).get("cases") or []))
    diff = [k for k in T if (fx or {}).get("cases", {}).get(k, {}).get("target") != T[k]]
    ok("цели в файле совпадают с default_targets() (их берёт сервер, где tests/ нет)", not diff, diff)
    base = {"pages": 1, "seconds": 5.0, "input": {"sum_insured": 150_000_000}}
    by = {c["id"]: c for c in lc.CASES}

    # K1: avtoelon недоступен — «нет доступа к источнику», не «вне допуска»
    down = dict(base, vehicle_valuation={"method": "depreciation", "V": 89_954_304, "n": None,
                                         "source_status": "источник недоступен", "source_reason": "HTTP 404"},
                value_sv={"verdict": "over", "ratio_pct": 166.8})
    e = lc.evaluate_target(down, T["K1"], by["K1"])
    ok("K1: avtoelon недоступен — «нет доступа к источнику», пометка про офлайн-тест К2",
       e["status"] == lc.NO_SOURCE and "К2" in e.get("note", ""), e)
    e = lc.evaluate_target(dict(down, pages=3), T["K1"], by["K1"])
    ok("K1: нет источника, но PDF 3 страницы — «вне допуска»", e["status"] == "вне допуска", e)
    up = dict(base, vehicle_valuation={"method": "analogs", "V": 108_400_000, "n": 19, "source_status": "ок"},
              value_sv={"verdict": "over", "label": "страхование сверх страховой стоимости", "ratio_pct": 138.4})
    e = lc.evaluate_target(up, T["K1"], by["K1"])
    ok("K1: 108,4 млн по аналогам и вывод over — в допуске", e["status"] == "в допуске", e)
    e = lc.evaluate_target(dict(up, value_sv={"verdict": "over", "label": "перестрахование"}), T["K1"], by["K1"])
    ok("K1: старый ярлык «перестрахование» — вывод по коду over, ярлык пометкой про К1",
       e["status"] == "в допуске" and "закрывается задачей К1" in e.get("note", ""), e)
    e = lc.evaluate_target(dict(up, value_sv={"verdict": "normal"}), T["K1"], by["K1"])
    ok("K1: вывод не over — вне допуска", e["status"] == "вне допуска", e)
    e = lc.evaluate_target(dict(up, vehicle_valuation={"method": "analogs", "V": 95_400_000, "n": 19,
                                                       "source_status": "ок"}), T["K1"], by["K1"])
    bad = [c for c in e["checks"] if not c["ok"]]
    ok("K1: сайт ответил, V 95,4 млн — «вне допуска»: поле, ожидание 105–117 млн, факт",
       e["status"] == "вне допуска" and bad[0]["field"] == "V по аналогам avtoelon.uz"
       and "105 000 000–117 000 000" in bad[0]["expected"] and "95 400 000" in bad[0]["actual"], e)

    # K5: затратный метод по формуле ТЗ
    row = {"id": 1, "price_m2": 6_500_000, "date": "2026-10-13"}
    V, how = lc.cost_expected(by["K5"], T["K5"]["cost"], row, date(2026, 10, 14))
    ok("K5: 450 · 6 500 000 · 1,00 · (1 − 10/80) = 2 559 375 000", V == 2_559_375_000, (V, how))
    k5 = dict(base, valuation={"available": True, "method": "cost", "V": 2_559_375_001}, ref={"build_cost": row})
    ok("K5: V отличается на 1 сум — в допуске", lc.evaluate_target(k5, T["K5"], by["K5"])["status"] == "в допуске")
    e = lc.evaluate_target(dict(k5, valuation={"available": True, "method": "cost", "V": 2_559_375_002}),
                           T["K5"], by["K5"])
    ok("K5: на 2 сум — вне допуска", e["status"] == "вне допуска", e)
    e = lc.evaluate_target(dict(k5, ref={"build_cost": None}), T["K5"], by["K5"])
    ok("K5: нет строки цены 1 м² — вне допуска с причиной", e["status"] == "вне допуска"
       and any("нет строки цены 1 м²" in str(c["expected"]) for c in e["checks"]), e)

    # K6: минимум 0,05 %
    ok("K6: ставка 0,0854 % — в допуске",
       lc.evaluate_target(dict(base, rate_pct=0.0854), T["K6"], by["K6"])["status"] == "в допуске")
    e = lc.evaluate_target(dict(base, rate_pct=0.04), T["K6"], by["K6"])
    ok("K6: ставка 0,04 % — вне допуска: «ставка, %», «≥ 0.05», 0.04",
       e["status"] == "вне допуска" and [(c["field"], c["expected"], c["actual"]) for c in e["checks"]
                                         if not c["ok"]] == [("ставка, %", "≥ 0.05", 0.04)], e)

    # P: премия = сумма частей, ставка части ≥ минимума из справочника частей
    p = dict(base, premium=2_300_000, parts_premium=[{"class_code": "3", "premium": 1_650_000},
                                                     {"class_code": "1", "premium": 150_000},
                                                     {"class_code": "13", "premium": 500_000}],
             parts=[{"class_code": "3", "rate_pct": 1.1}, {"class_code": "1", "rate_pct": 0.5},
                    {"class_code": "13", "rate_pct": 1.0}], ref={"parts_min": {"3": 1.1, "1": 0.5, "13": 1.0}})
    ok("P1: премия = сумма частей, ставки ≥ минимумов — в допуске",
       lc.evaluate_target(p, T["P1"], by["P1"])["status"] == "в допуске")
    ok("P1: премия на 2 сум больше суммы частей — вне допуска",
       lc.evaluate_target(dict(p, premium=2_300_002), T["P1"], by["P1"])["status"] == "вне допуска")
    e = lc.evaluate_target(dict(p, ref={"parts_min": {"3": 1.2, "1": 0.5, "13": 1.0}}), T["P1"], by["P1"])
    ok("P1: часть кл. 3 ниже минимума 1,2 — вне допуска, поле части",
       e["status"] == "вне допуска" and [c["field"] for c in e["checks"] if not c["ok"]] == ["ставка части кл. 3, %"], e)
    e = lc.evaluate_target(dict(p, ref={"parts_min": None, "parts_http": 404}), T["P1"], by["P1"])
    ok("P1: справочник частей не ответил — вне допуска", e["status"] == "вне допуска", e)

    # D: договор прочитан, ≥ 8 из 10 полей, 12 проверок
    d = dict(base, contract_read=True, contract_found=9, contract_checks=list(range(1, 13)))
    ok("D1: 9 из 10 полей и 12 проверок — в допуске",
       lc.evaluate_target(d, T["D1"], by["D1"])["status"] == "в допуске")
    ok("D1: 7 из 10 полей — вне допуска",
       lc.evaluate_target(dict(d, contract_found=7), T["D1"], by["D1"])["status"] == "вне допуска")
    ok("D1: 11 проверок — вне допуска",
       lc.evaluate_target(dict(d, contract_checks=list(range(1, 12))), T["D1"], by["D1"])["status"] == "вне допуска")
    ok("D3: договор не прочитан — вне допуска",
       lc.evaluate_target(dict(d, contract_read=False), T["D3"], by["D3"])["status"] == "вне допуска")
    ok("нет цели — вне допуска", lc.evaluate_target(base, None)["status"] == "вне допуска")
    ok("61 с — вне допуска", lc.evaluate_target(dict(base, seconds=61), T["K2"], by["K2"])["status"] == "вне допуска")

# ------------------------------------------------------------------ 3. файлы, проба Cobalt, Telegram

class FakeClient:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def request(self, method, path, data=None, ctype=None):
        self.calls.append((method, path, json.loads(data) if data else None))
        return 200, json.dumps(self.reply, ensure_ascii=False).encode(), {}


def check_files_probe_tg():
    print("3. Снимки, PDF, проба Cobalt, Telegram")
    pm = lc._pymupdf()
    if pm is not None:
        doc = pm.open()
        for _ in range(3):
            doc.new_page()
        ok("страницы PDF: 3", lc.pdf_pages(doc.tobytes()) == 3)
    ok("не PDF — None", lc.pdf_pages(b"<html>") is None)
    name, png, mime = lc.training_photo("car_damage")
    ok("учебный снимок — PNG", png.startswith(b"\x89PNG") and mime == "image/png" and name == "car_damage.png")
    ok("запасной PNG без библиотек", lc._png_plain(4, 3, (1, 2, 3)).startswith(b"\x89PNG"))
    body, ctype = lc.multipart({"lang": "ru"}, [("a.png", b"123", "image/png")])
    ok("multipart: поле files и граница", b'name="files"; filename="a.png"' in body
       and ctype.startswith("multipart/form-data; boundary=") and body.endswith(b"--\r\n"))
    good = {"method": "analogs", "available": True, "V": 111_400_000, "n": 19, "n_found": 20,
            "source": {"status": None}, "warnings": ["Страховая сумма 150 000 000 сум — 135 % от рынка"]}
    fc = FakeClient(good)
    r = lc.cobalt_probe(fc)
    ok("Cobalt: 111 млн по 19 аналогам и предупреждение — выполнено", r["ok"] and r["n"] == 19, r)
    ok("Cobalt: оценка не сохраняется (save = false), без цены нового",
       fc.calls[0][2]["save"] is False and "price_new" not in fc.calls[0][2], fc.calls)
    bad = {"method": "analogs", "available": False, "V": None, "n": 0,
           "source": {"status": "источник недоступен", "reason": "сайт ответил HTTP 404"}, "warnings": []}
    r = lc.cobalt_probe(FakeClient(bad))
    ok("Cobalt: источник недоступен — не выполнено, причина в отчёте", not r["ok"] and "404" in r["source_reason"], r)
    rep = {"date": "2026-10-13T23:00:00+05:00", "summary": "1 из 1 в допуске"}
    saved = os.environ.pop("TG_OWNER_CHAT_ID", None)
    try:
        ok("нет TG_OWNER_CHAT_ID — пропуск с пометкой",
           lc.notify_telegram(rep, "x") == "не отправлено: переменной TG_OWNER_CHAT_ID нет в окружении")
        from app import tgbot
        sent = []
        old = tgbot.send, tgbot.send_file
        tgbot.send = lambda chat, text, **k: sent.append(("text", chat, text)) or {"ok": True}
        tgbot.send_file = lambda chat, fn, blob, mime, caption="", **k: sent.append(("file", chat, fn)) or {"ok": True}
        os.environ["TG_OWNER_CHAT_ID"] = "100500"
        try:
            note = lc.notify_telegram(rep, "# отчёт")
        finally:
            tgbot.send, tgbot.send_file = old
            os.environ.pop("TG_OWNER_CHAT_ID", None)
        ok("есть TG_OWNER_CHAT_ID — итог и файл отчёта владельцу ботом сервера",
           note.startswith("отправлено") and [s[0] for s in sent] == ["text", "file"]
           and sent[1][2] == "live_check_2026-10-13.md" and "1 из 1 в допуске" in sent[0][2], (note, sent))
    finally:
        if saved is not None:
            os.environ["TG_OWNER_CHAT_ID"] = saved


# ------------------------------------------------------------------ 4. поток целиком (ASGI, копия базы)

FLOW = ("K1", "K5", "K6", "P1", "P2", "T1", "T2", "T3", "D1", "D2")


def check_flow():
    print("4. Поток /act/photos → /act/make → /act/{id}.pdf, цели ТЗ, снимки и отчёт (копия базы, без сети)")
    with db.tx() as con:
        V.build_cost_seed(con)            # учебная цена 1 м² (Ташкент, магазин) — как при старте сервера
    cases = [c for c in lc.CASES if c["id"] in FLOW]
    tmp = Path(tempfile.mkdtemp(prefix="live-check-"))
    exp_path, rep_dir = tmp / "fixtures" / "expected.json", tmp / "reports"
    quiet = lambda *a, **k: None          # noqa: E731
    r1 = lc.run("http://test", snapshot=True, cases=cases, client_factory=AsgiClient, expected_path=exp_path,
                reports_dir=rep_dir, probe=False, log=quiet)
    errs = {r["id"]: r["error"] for r in r1["results"] if r.get("error")}
    ok("все %d осмотров прошли поток без ошибок" % len(FLOW), not errs, errs)
    by = {r["id"]: r for r in r1["results"]}
    ok("у каждого акта — id, ставка, премия, PDF ≤ 2 страниц",
       all(r.get("act_id") and r.get("rate_pct") is not None and r.get("premium") and r.get("pages") in (1, 2)
           for r in r1["results"]), {k: (r.get("rate_pct"), r.get("pages")) for k, r in by.items()})
    ok("шаги потока по ТЗ", [s[0] for s in by["T1"]["steps"]] == ["POST /act/photos", "POST /act/make",
                                                                   "GET /act/{id}.pdf"], by["T1"]["steps"])
    ok("PDF и на UZ, и на EN", set((by["T2"].get("pages_lang") or {})) == {"uz", "en"}, by["T2"].get("pages_lang"))
    ok("договор DOCX прочитан, поля осмотра из договора, сверка в акте",
       by["D1"].get("contract_read") and by["D1"].get("contract_check")
       and by["D1"]["input"]["product_code"] == "0807" and by["D1"]["input"]["sum_insured"] == 2_400_000_000,
       {k: by["D1"].get(k) for k in ("contract_read", "contract_check", "input", "notes")})
    # цели ТЗ на копии базы: все закрыты, K1 — «нет доступа к источнику» (сеть в тесте закрыта)
    tgt = {r["id"]: r["target_eval"] for r in r1["results"]}
    not_ok = {k: [c for c in t["checks"] if not c["ok"]] for k, t in tgt.items()
              if t["status"] != "в допуске" and k != "K1"}
    ok("цели ТЗ закрыты: K5, T3 (затратный метод ±1 сум), K6, P1, P2, D1, D2, PDF и время", not not_ok, not_ok)
    ok("K1 без avtoelon.uz — «нет доступа к источнику», не «вне допуска»", tgt["K1"]["status"] == lc.NO_SOURCE,
       tgt["K1"])
    t3 = next(c for c in tgt["T3"]["checks"] if c["field"] == "V затратным методом")
    ok("T3: V из справочника цены 1 м² и параметров объекта", t3["ok"] and "2 559 375 000" in t3["expected"], t3)
    ok("P2: премия = сумма частей, ставки частей проверены по справочнику частей",
       {c["field"] for c in tgt["P2"]["checks"]} >= {"премия договора, сум", "ставка части кл. 8, %",
                                                    "ставка части кл. 13, %"}, tgt["P2"]["checks"])
    ok("итог: «цели ТЗ: %d из %d» с пометкой про K1 и офлайн-тест К2" % (len(FLOW), len(FLOW)),
       r1["targets"]["line"].startswith("цели ТЗ: %d из %d (K1 — нет доступа к источнику" % (len(FLOW), len(FLOW)))
       and "К2" in r1["targets"]["line"], r1["targets"])
    snap = json.loads(exp_path.read_text(encoding="utf-8"))
    ok("файл снимков создан в новой папке: meta с датой, коммитом, адресом; у осмотров snapshot и target",
       snap["meta"]["date"].startswith("20") and snap["meta"]["commit"] and snap["meta"]["url"] == "http://test"
       and set(snap["cases"]) == set(FLOW)
       and all(set(c) == {"snapshot", "target"} and c["snapshot"] and c["target"] for c in snap["cases"].values()),
       snap["meta"])
    md = sorted(rep_dir.glob("live_check_*.md"))
    js = sorted(rep_dir.glob("live_check_*.json"))
    ok("отчёт .md и .json в папке отчётов", len(md) == 1 and len(js) == 1, list(rep_dir.iterdir()))
    text = md[0].read_text(encoding="utf-8")
    ok("в отчёте таблица и две строки итога: цели ТЗ и без регрессий",
       "| № | Объект | Продукт | Ставка, % |" in text and ("**%s**" % r1["targets"]["line"]) in text
       and ("**%s**" % r1["regress"]["line"]) in text
       and r1["regress"]["line"] == "без регрессий: %d из %d" % (r1["regress"]["passed"], len(FLOW)), text[:600])
    ok("из командной строки Telegram не отправляется", r1["telegram"].startswith("не отправлено"), r1["telegram"])
    r2 = lc.run("http://test", cases=cases, client_factory=AsgiClient, expected_path=exp_path, reports_dir=rep_dir,
                probe=False, log=quiet)
    bad = {r["id"]: lc.fail_lines(r) for r in r2["results"] if r["eval"]["status"] != "в допуске"}
    ok("второй прогон против снимка: %s" % r2["regress"]["line"],
       r2["regress"]["passed"] == len(FLOW) and r2["targets"]["closed"] == len(FLOW)
       and not r2["expected"]["taken_now"], bad)

    # --snapshot не трогает target
    snap["cases"]["K6"]["target"]["rate_min_pct"] = 0.06
    snap["cases"]["K6"]["target"]["note"] = "правка вручную"
    snap["cases"]["K6"]["snapshot"]["rate_pct"] = 9.9
    exp_path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    k6 = [c for c in cases if c["id"] == "K6"]
    lc.run("http://test", snapshot=True, cases=k6, only=["K6"], client_factory=AsgiClient, expected_path=exp_path,
           reports_dir=rep_dir, probe=False, save=True, log=quiet)
    snap2 = json.loads(exp_path.read_text(encoding="utf-8"))
    ok("--snapshot перезаписал snapshot, а target оставил как был (правка вручную цела)",
       snap2["cases"]["K6"]["snapshot"]["rate_pct"] == by["K6"]["rate_pct"]
       and snap2["cases"]["K6"]["target"] == snap["cases"]["K6"]["target"]
       and all(snap2["cases"][k] == snap["cases"][k] for k in FLOW if k != "K6"),
       (snap2["cases"]["K6"], snap["cases"]["K6"]["target"]))
    r3 = lc.run("http://test", cases=k6, client_factory=AsgiClient, expected_path=exp_path, reports_dir=rep_dir,
                probe=False, save=False, log=quiet)
    ok("цель из файла (≥ 0,06 %) применяется: ставка K6 0,0854 % — в допуске",
       r3["results"][0]["target_eval"]["status"] == "в допуске" and any(
           c["expected"] == "≥ 0.06" for c in r3["results"][0]["target_eval"]["checks"]), r3["results"][0]["target_eval"])
    snap2["cases"]["K6"]["snapshot"]["rate_pct"] = round(snap2["cases"]["K6"]["snapshot"]["rate_pct"] + 0.01, 4)
    exp_path.write_text(json.dumps(snap2, ensure_ascii=False), encoding="utf-8")
    r4 = lc.run("http://test", cases=k6, client_factory=AsgiClient, expected_path=exp_path, reports_dir=rep_dir,
                probe=False, save=False, log=quiet)
    k6r = r4["results"][0]
    ok("ставка ушла от снимка на 0,01 п.п. — регрессия с разницей; цель при этом закрыта",
       k6r["eval"]["status"] == "вне допуска" and k6r["target_eval"]["status"] == "в допуске"
       and any("разница -0.0100" in c["text"] for c in k6r["eval"]["checks"])
       and r4["regress"]["line"] == "без регрессий: 0 из 1" and r4["targets"]["line"] == "цели ТЗ: 1 из 1",
       (k6r["eval"], r4["summary"]))
    md4 = lc.render_md(r4)
    ok("в отчёте у осмотра вне допуска — поле, ожидание, факт",
       "### K6" in md4 and "регрессия · ставка, %: ожидание" in md4 and "; факт 0.0854" in md4, md4[-900:])


# ------------------------------------------------------------------ 5. маршруты админки

def _session(login: str, role: str) -> str:
    with db.tx() as con:
        cur = con.execute("INSERT INTO users (login, full_name, role, branch, password_hash, salt, status, created_at)"
                          " VALUES (?,?,?,?,?,?,?,?)", (login, "Тестовый " + login, role, "тест-live", "x", "y",
                                                         "активен", db.now()))
        u = db.rows(con, "SELECT * FROM users WHERE id=?", cur.lastrowid)[0]
        token, _ = auth.create_session(con, u, "203.0.113.9", "test")
    return token


def check_routes():
    print("5. POST /admin/live-check и GET /admin/live-check/last")
    from app import main as M
    admin, under = AsgiClient(), AsgiClient()
    admin.cookies["sid"] = _session("тест-live-админ", "админ")
    under.cookies["sid"] = _session("тест-live-андеррайтер", "андеррайтер")
    tmp = Path(tempfile.mkdtemp(prefix="live-check-routes-"))
    calls, gate = [], threading.Event()
    old_run, old_reports, old_get = lc.run, lc.REPORTS, llm.get

    def fake_run(url, **kw):                 # вместо 20 осмотров: ждёт сигнала и пишет отчёт
        calls.append(dict(kw, url=url))
        gate.wait(10)
        kw["reports_dir"].mkdir(parents=True, exist_ok=True)
        (kw["reports_dir"] / "live_check_2026-10-14.json").write_text(json.dumps(
            {"summary": "цели ТЗ: 20 из 20; без регрессий: 20 из 20", "targets": {"line": "цели ТЗ: 20 из 20"},
             "regress": {"line": "без регрессий: 20 из 20"}}, ensure_ascii=False), encoding="utf-8")
        (kw["reports_dir"] / "live_check_2026-10-14.md").write_text("# отчёт\n**цели ТЗ: 20 из 20**", encoding="utf-8")
        return {"summary": "цели ТЗ: 20 из 20; без регрессий: 20 из 20", "telegram": "не отправлено: нет переменной"}

    lc.run, lc.REPORTS = fake_run, tmp / "reports"
    llm.get = lambda key, default=None: "" if key == "SERVER_URL" else old_get(key, default)
    try:
        st, raw, _h = AsgiClient().request("POST", "/admin/live-check")
        ok("гость — 401", st == 401, st)
        st, raw, _h = under.request("POST", "/admin/live-check")
        ok("не администратор (андеррайтер) — 403", st == 403, (st, raw[:200]))
        st, raw, _h = under.request("GET", "/admin/live-check/last")
        ok("последний отчёт не администратору — 403", st == 403, st)
        st, raw, _h = admin.request("GET", "/admin/live-check/last")
        ok("отчёта ещё нет — 404", st == 404, (st, raw[:200]))
        st1, raw1, _h = admin.request("POST", "/admin/live-check")
        st2, raw2, _h = admin.request("POST", "/admin/live-check")
        ok("администратор запускает — 202; второй запуск, пока идёт первый, — 409",
           st1 == 202 and st2 == 409, (st1, raw1[:200], st2, raw2[:200]))
        gate.set()
        for t in list(M._live_threads):
            t.join(10)
        ok("фоновый прогон: run(url сервера из запроса, telegram=True), один вызов",
           len(calls) == 1 and calls[0]["url"] == "http://test" and calls[0]["telegram"] is True, calls)
        st, raw, _h = admin.request("GET", "/admin/live-check/last")
        body = json.loads(raw) if st == 200 else {}
        ok("последний отчёт: md и json из папки отчётов, итог двумя строками",
           st == 200 and body.get("md", "").startswith("# отчёт") and body["targets"]["line"] == "цели ТЗ: 20 из 20"
           and body["regress"]["line"] == "без регрессий: 20 из 20" and body["state"]["running"] is False,
           (st, raw[:300]))
        llm.get = lambda key, default=None: "https://example.test/" if key == "SERVER_URL" else old_get(key, default)
        gate.set()
        st, raw, _h = admin.request("POST", "/admin/live-check")
        for t in list(M._live_threads):
            t.join(10)
        ok("после завершения можно запустить снова; адрес — SERVER_URL, если задан",
           st == 202 and len(calls) == 2 and calls[1]["url"] == "https://example.test", (st, calls[-1:]))
    finally:
        lc.run, lc.REPORTS, llm.get = old_run, old_reports, old_get
        gate.set()


def main():
    llm_post, http_get = llm._post, vs._http_get
    llm._post = no_network
    vs._http_get = no_network
    try:
        check_cases()
        check_pure()
        check_targets_pure()
        check_files_probe_tg()
        with temp_db("surveyor-live-check.db"):
            db.ensure_schema()
            ensure_market_stats()
            ensure_stat_series()
            guest.reset()
            check_flow()
            check_routes()
    finally:
        llm._post, vs._http_get = llm_post, http_get
    print("\nитог: ок %d, плохо %d" % (passed, failed))
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
