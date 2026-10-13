"""
tools/live_check.py (ТЗ 13.10.2026, день 10, п. 10.1): проверка вживую 20 учебных осмотров.

Запуск из корня:
    set PYTHONIOENCODING=utf-8
    sandbox\.venv\Scripts\python.exe tests\test_live_check.py

Без сети и без рабочей базы: поток /act/photos → /act/make → /act/{id}.pdf идёт через ASGI-приложение во временной
копии базы (tests/tmpdb.py), модель и внешние сайты подменены. Проверяется: состав 20 осмотров, допуски (ставка
±0,005 п.п., стоимость в диапазоне, ≤ 2 страниц, ≤ 60 с, договор прочитан, минимум), эталоны с датой и коммитом,
отчёт .md и .json с итогом «N из M в допуске», Telegram без TG_OWNER_CHAT_ID пропускается с пометкой.
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

os.environ.pop("SURVEYOR_DEV", None)
os.environ["SURVEYOR_NO_BACKGROUND"] = "1"

from tmpdb import ensure_market_stats, ensure_stat_series, temp_db   # noqa: E402
from app import db, guest, llm                                       # noqa: E402
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
    e = lc.evaluate(dict(res, rate_pct=0.04), {"rate_pct": 0.04}, {"min_rate_pct": 0.05})
    ok("ставка ниже минимума 0,05 % — вне допуска", e["status"] == "вне допуска", e)
    e = lc.evaluate(dict(res, contract_read=False), {"rate_pct": 0.0854}, {"contract": "x.pdf"})
    ok("договор не прочитан — вне допуска", e["status"] == "вне допуска", e)
    e = lc.evaluate(res, {"rate_pct": 0.0854, "valuation_method": "cost"})
    ok("в эталоне была оценка, а теперь нет — вне допуска", e["status"] == "вне допуска", e)
    ok("ошибка потока — «ошибка»", lc.evaluate({"error": "RuntimeError: x", "seconds": 1}, None)["status"] == "ошибка")


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

FLOW = ("K5", "K6", "P1", "T1", "T2", "D1")


def check_flow():
    print("4. Поток /act/photos → /act/make → /act/{id}.pdf, эталоны и отчёт (копия базы, без сети)")
    with db.tx() as con:
        V.build_cost_seed(con)            # учебная цена 1 м² (Ташкент, магазин) — как при старте сервера
    cases = [c for c in lc.CASES if c["id"] in FLOW]
    tmp = Path(tempfile.mkdtemp(prefix="live-check-"))
    exp_path, rep_dir = tmp / "expected.json", tmp / "reports"
    quiet = lambda *a, **k: None          # noqa: E731
    r1 = lc.run("http://test", snapshot=True, cases=cases, client_factory=AsgiClient, expected_path=exp_path,
                reports_dir=rep_dir, probe=False, log=quiet)
    errs = {r["id"]: r["error"] for r in r1["results"] if r.get("error")}
    ok("все %d осмотров прошли поток без ошибок" % len(FLOW), not errs, errs)
    by = {r["id"]: r for r in r1["results"]}
    ok("у каждого акта — id, ставка, премия, PDF ≤ 2 страниц",
       all(r.get("act_id") and r.get("rate_pct") and r.get("premium") and r.get("pages") in (1, 2)
           for r in r1["results"]), {k: (r.get("rate_pct"), r.get("pages")) for k, r in by.items()})
    ok("шаги потока по ТЗ", [s[0] for s in by["T1"]["steps"]] == ["POST /act/photos", "POST /act/make",
                                                                   "GET /act/{id}.pdf"], by["T1"]["steps"])
    ok("PDF и на UZ, и на EN", set((by["T2"].get("pages_lang") or {})) == {"uz", "en"}, by["T2"].get("pages_lang"))
    ok("договор DOCX прочитан, поля осмотра из договора, сверка в акте",
       by["D1"].get("contract_read") and by["D1"].get("contract_check")
       and by["D1"]["input"]["product_code"] == "0807" and by["D1"]["input"]["sum_insured"] == 2_400_000_000,
       {k: by["D1"].get(k) for k in ("contract_read", "contract_check", "input", "notes")})
    ok("магазин — затратная оценка в диапазоне", (by["K5"].get("valuation") or {}).get("method") == "cost"
       and any(c["code"] == "value" and c["ok"] for c in by["K5"]["eval"]["checks"]), by["K5"].get("valuation"))
    ok("комплексный 0305 — ставки трёх частей", [p["class_code"] for p in by["P1"]["parts"]] == ["3", "1", "13"],
       by["P1"]["parts"])
    ok("склад 4,2 млрд — ставка не ниже 0,05 %", by["K6"]["rate_pct"] >= 0.05, by["K6"]["rate_pct"])
    snap = json.loads(exp_path.read_text(encoding="utf-8"))
    ok("эталоны записаны с датой, коммитом и адресом", snap["meta"]["date"].startswith("20")
       and snap["meta"]["commit"] and snap["meta"]["url"] == "http://test" and set(snap["cases"]) == set(FLOW),
       snap["meta"])
    md = sorted(rep_dir.glob("live_check_*.md"))
    js = sorted(rep_dir.glob("live_check_*.json"))
    ok("отчёт .md и .json в папке отчётов", len(md) == 1 and len(js) == 1, list(rep_dir.iterdir()))
    text = md[0].read_text(encoding="utf-8")
    ok("в отчёте таблица и итог «N из M в допуске»", "| № | Объект | Продукт | Ставка, % |" in text
       and ("Итог: %s" % r1["summary"]) in text and r1["summary"].endswith("из %d в допуске" % len(FLOW)), text[:300])
    ok("из командной строки Telegram не отправляется", r1["telegram"].startswith("не отправлено"), r1["telegram"])
    r2 = lc.run("http://test", cases=cases, client_factory=AsgiClient, expected_path=exp_path, reports_dir=rep_dir,
                probe=False, log=quiet)
    bad = {r["id"]: [c["text"] for c in r["eval"]["checks"] if c["ok"] is False] for r in r2["results"]
           if r["eval"]["status"] != "в допуске"}
    ok("второй прогон против эталона: %s" % r2["summary"], r2["passed"] == len(FLOW) and not r2["expected"]["taken_now"],
       bad)
    snap["cases"]["K6"]["rate_pct"] = round(snap["cases"]["K6"]["rate_pct"] + 0.01, 4)
    exp_path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    r3 = lc.run("http://test", cases=cases, client_factory=AsgiClient, expected_path=exp_path, reports_dir=rep_dir,
                probe=False, save=False, log=quiet)
    k6 = next(r for r in r3["results"] if r["id"] == "K6")
    ok("ставка ушла от эталона на 0,01 п.п. — «вне допуска» с разницей",
       k6["eval"]["status"] == "вне допуска" and any("разница -0,0100".replace(",", ".") in c["text"]
                                                     for c in k6["eval"]["checks"]), k6["eval"])


def main():
    llm_post, http_get = llm._post, vs._http_get
    llm._post = no_network
    vs._http_get = no_network
    try:
        check_cases()
        check_pure()
        check_files_probe_tg()
        with temp_db("surveyor-live-check.db"):
            db.ensure_schema()
            ensure_market_stats()
            ensure_stat_series()
            guest.reset()
            check_flow()
    finally:
        llm._post, vs._http_get = llm_post, http_get
    print("\nитог: ок %d, плохо %d" % (passed, failed))
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
