"""
Биржевые цены УзРТСБ (app/uzex_sources.py, app/uzex.py, act_analytics.exchange_background) — без сети.

Запуск из корня проекта:
    set PYTHONIOENCODING=utf-8
    sandbox\\.venv\\Scripts\\python.exe tests\\test_uzex.py

Что проверяется:
  1. разбор копии котировочного листа tests/fixtures/uzex_c.html: 10 строк, цена лота и цена за единицу, группы;
  2. разбор реестра сделок (короткая копия таблицы ниже): дата сделки, цена за тонну у сделок разного объёма,
     одинаковые сделки не склеиваются;
  3. числа с запятой, даты, группы товаров по названию и по свободному тексту;
  4. robots.txt uzex.uz (копия ниже, с BOM и двумя группами «*»): закрытые пути отказывают, сеть не трогается;
  5. запись с историей и медианы на копии базы (tests/tmpdb.py): сделка, видимая несколько дней, считается один раз;
  6. API /exchange/*: без входа 401, агент читает, refresh — только админ (сеть подменена разобранной копией);
  7. справка для акта: класс 7 (наливной груз, дизель), класс 8 офис — не нужна, склад — типовой набор, нет данных.
Рабочая data/surveyor.db не открывается.
"""
import asyncio
import json as _json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.pop("SURVEYOR_DEV", None)
os.environ["SURVEYOR_NO_BACKGROUND"] = "1"

from tmpdb import temp_db  # noqa: E402
from app import act_analytics as aa, auth, db  # noqa: E402
from app import uzex_sources as us  # noqa: E402

passed, failed = 0, 0


def ok(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ок  ", name)
    else:
        failed += 1
        print("  ПЛОХО", name, str(extra)[:400])


# robots.txt uzex.uz дословно (02.10.2026), с BOM в начале
ROBOTS = ("﻿User-agent: *\nAllow: /\n\nUser-agent: *\nDisallow: /Trade/Offer\nDisallow: /Servis/Cabinet\n"
          "Disallow: /Servis/CabinetOld\nDisallow: /Servis/CabinetAll\nDisallow: /Search/Index\nDisallow: /page\n"
          "Disallow: /Login\nDisallow: /Tps/TpsDetails\nDisallow: /Edit\nDisallow: /Delete\nDisallow: /Create\n"
          "Disallow: /id\nDisallow: /Account\nDisallow: /Trade/OffersSum\n\nSitemap: https://uzex.uz/sitemap.xml\n")

# реестр сделок: шапка и строки как на /Trade/List (30.09.2026)
REG_HTML = """<table class="table"><thead><tr>
<th>Shartnoma sanasi</th><th>Bitim raqami</th><th>Mahsulot narxi (so'm)</th><th>Shartnoma №</th>
<th>Mahsulot markasi</th><th>Miqdori</th><th>O'lchov birligi</th><th>Shartnoma turi</th>
<th>Bitimning bajarilish holati</th></tr></thead><tbody>
<tr><td>30/09/2026 00:00:00</td><td>0</td><td>1471782535.9200</td><td>1692</td><td>Медь катодная</td><td>8</td>
<td>&#x442;&#x43E;&#x43D;&#x43D;&#x430;</td><td>внутренний</td><td></td></tr>
<tr><td>30/09/2026 00:00:00</td><td>0</td><td>4231374791.0000</td><td>1692</td><td>Медь катодная</td><td>23</td>
<td>тонна</td><td>внутренний</td><td></td></tr>
<tr><td>29/09/2026 00:00:00</td><td>0</td><td>870000.0000</td><td>2001</td><td>Дизельное топливо ЭКО</td><td>60</td>
<td>килограмм</td><td>внутренний</td><td></td></tr>
<tr><td>29/09/2026 00:00:00</td><td>0</td><td>870000.0000</td><td>2001</td><td>Дизельное топливо ЭКО</td><td>60</td>
<td>килограмм</td><td>внутренний</td><td></td></tr>
<tr><td>25/09/2026 00:00:00</td><td>0</td><td>471923997.0000</td><td>20532</td>
<td>Слиток из золота Au-1 OzDst 610:2009 (грамм)</td><td>300</td><td>грамм</td><td>внутренний</td><td></td></tr>
</tbody></table><p class="mb-0">5 dan 1 dan 10 gacha yozuvlar aks ettirildi</p>"""


# ------------------------------------------------------------------ 1–3. разбор
def check_parse():
    print("1. Котировочный лист (tests/fixtures/uzex_c.html)")
    html = (ROOT / "tests" / "fixtures" / "uzex_c.html").read_text(encoding="utf-8")
    rows = us.parse_page("ContractsSumNew", html, "2026-10-02T02:00:00", us.page_url("ContractsSumNew"))
    by = {r["contract_no"]: r for r in rows}
    ok("10 строк, у всех ссылка на страницу биржи и пометка «на дату загрузки», даты сделки нет",
       len(rows) == 10 and all(r["url"].startswith("https://uzex.uz/Trade/ContractsSumNew") for r in rows)
       and all(r["trade_date"] is None and r["date_basis"] == us.DATE_FETCH for r in rows), len(rows))
    d = by["106"]
    ok("106 «Дизельное топливо ЭКО-Л»: лот 60 т, цена лота 870 000 000, за тонну 14 500 000, группа дизель, склад Навои",
       d["lot_qty"] == 60 and d["price_lot"] == 870_000_000 and d["price_unit"] == 14_500_000
       and d["unit"] == "тонна" and d["unit_norm"] == "т" and d["grp"] == "diesel" and d["warehouse"] == "Навои", d)
    m = by["265"]
    ok("265 мука: лот 100 кг, 427 000 за лот → 4 270 за кг → 4 270 000 за тонну, группа «мука и зерно»",
       m["price_unit"] == 4270 and m["unit_norm"] == "т" and m["price_unit_norm"] == 4_270_000 and m["grp"] == "grain", m)
    ok("группы: печное ×2, масло моторное, сода — химия, арматура ×3 — металл",
       [by[k]["grp"] for k in ("102", "205", "245", "163", "275", "278", "279")] ==
       ["heating_oil", "heating_oil", "oils", "chemicals", "metal", "metal", "metal"],
       [(k, by[k]["grp"]) for k in by])
    ok("сохранено и сырое число страницы (price_raw), и основание цены (price_basis — цена лота)",
       d["price_raw"] == 870_000_000 and "цена лота" in d["price_basis"], d["price_basis"])
    ok("всего записей на сайте по подписи под таблицей — 148 654", us.total_on_site(html) == 148654)

    print("2. Реестр сделок (дата сделки, сумма за всё количество)")
    reg = us.parse_page("List", REG_HTML, "2026-10-02T02:00:00", us.page_url("List"))
    ok("5 строк, у всех дата сделки", len(reg) == 5 and all(r["trade_date"] for r in reg)
       and reg[0]["trade_date"] == "2026-09-30" and reg[0]["date_basis"] == us.DATE_DEAL, [r["trade_date"] for r in reg])
    ok("медь 8 т и 23 т — одна цена за тонну ≈ 183,97 млн (сумма — за всё количество)",
       abs(reg[0]["price_unit"] - 183_972_816.99) < 1 and abs(reg[1]["price_unit"] - 183_972_816.99) < 1
       and reg[0]["grp"] == "nonferrous", (reg[0]["price_unit"], reg[1]["price_unit"]))
    ok("две одинаковые сделки по дизелю — разные ключи (…|1 и …|2), килограмм → тонна: 14 500 за кг = 14 500 000 за т",
       reg[2]["deal_key"] != reg[3]["deal_key"] and reg[2]["deal_key"].endswith("|1")
       and reg[3]["deal_key"].endswith("|2") and reg[2]["price_unit_norm"] == 14_500_000, (reg[2], reg[3]))
    two = us.rekey(reg[2:3] + [dict(r) for r in us.parse_page("List", REG_HTML, "x", "u")][2:4])
    ok("одинаковые сделки по разные стороны границы листа: нумерация по всей загрузке (|1, |2, |3), не склеиваются",
       [r["deal_key"].rsplit("|", 1)[1] for r in two] == ["1", "2", "3"], [r["deal_key"] for r in two])
    ok("золото — «прочее», не металл", reg[4]["grp"] == "other" and reg[4]["unit_norm"] == "г")

    print("3. Числа, даты, группы")
    ok("числа: 870000000,0000 · 315341645.0000 · 1 471 782 535,92 · 1.234,5 · пусто · мусор",
       [us.parse_number(x) for x in ("870000000,0000", "315341645.0000", "1 471 782 535,92", "1.234,5", "", "abc")]
       == [870000000.0, 315341645.0, 1471782535.92, 1234.5, None, None])
    ok("даты: 30/09/2026 00:00:00 → 2026-09-30, 01.10.2026, мусор → None",
       [us.parse_date(x) for x in ("30/09/2026 00:00:00", "01.10.2026", "31/02/2026", "")] ==
       ["2026-09-30", "2026-10-01", None, None])
    cases = {"Автобензин А-92 К2-Л": "petrol", "Сжиженный газ": "fuel_other", "Уголь 2 БР (Форвард)": "fuel_other",
             "Портландцемент II/А-К 32,5 Н (в мешках)": "cement", "Пшеница 3 класс": "grain",
             "Семена хлопковые технические 1 сорт": "cotton", "Масло хлопковое рафинированное": "other",
             "Аммофос 46:11 (в мешках)": "chemicals", "Медь катодная": "nonferrous",
             "Препарат медицинский": "chemicals", "Уголок 50 ст 3 СП мерной длины": "metal",
             "Дизтопливо,  Денау н/б": "diesel", "Масло индустриальное И-20А": "oils", "": "other",
             "Доска обрезная": "other"}
    got = {k: us.classify(k) for k in cases}
    ok("группы по названию (бензин, газ, уголь, цемент, пшеница, хлопок, пищевое масло — прочее, удобрение, медь, "
       "«медицинский» — не медь, уголок — металл, дизтопливо, индустриальное масло, пусто)", got == cases,
       {k: v for k, v in got.items() if v != cases[k]})
    ok("группы в свободном тексте: «склад ГСМ» → дизель и бензин; «арматура и цемент» → металл, цемент; пусто → []",
       us.groups_in_text("склад ГСМ") == ["diesel", "petrol"]
       and us.groups_in_text("арматура и цемент на складе") == ["metal", "cement"] and us.groups_in_text("") == [])
    ok("медиана: 1, 3, 2 → 2; 1, 2, 3, 4 → 2,5; пусто → None",
       us.median([1, 3, 2]) == 2 and us.median([1, 2, 3, 4]) == 2.5 and us.median([]) is None)


# ------------------------------------------------------------------ 4. robots
def check_robots():
    print("4. robots.txt uzex.uz (две группы «*», BOM)")
    deny = ["https://uzex.uz/Trade/Offer/106", "https://uzex.uz/trade/offer/106", "https://uzex.uz/Trade/OffersSumNew",
            "https://uzex.uz/Login", "https://uzex.uz/Servis/Cabinet", "https://uzex.uz/Search/Index?q=1"]
    allow = ["https://uzex.uz/Trade/List?page=1&status=1&length=1000", "https://uzex.uz/Trade/ContractsSumNew",
             "https://uzex.uz/Trade/NewSpotTable", "https://uzex.uz/Trade/ContractsCurrencyNew"]
    ok("закрыто: карточка лота (в любом регистре), OffersSumNew, вход, кабинет, поиск",
       all(not us.robots_allowed(ROBOTS, u)[0] for u in deny), [u for u in deny if us.robots_allowed(ROBOTS, u)[0]])
    ok("открыто: реестр сделок, котировочные листы, заявки дня", all(us.robots_allowed(ROBOTS, u)[0] for u in allow),
       [u for u in allow if not us.robots_allowed(ROBOTS, u)[0]])
    ok("причина отказа называет домен и адрес",
       "uzex.uz" in us.robots_allowed(ROBOTS, deny[0])[1] and deny[0] in us.robots_allowed(ROBOTS, deny[0])[1])
    # fetch_html на закрытом адресе: отказ до обращения к сайту
    calls = []
    orig = us._http_get
    us._http_get = lambda url, timeout=0: calls.append(url) or ""
    us._robots.update(text=ROBOTS, reason="", at=time.monotonic())
    try:
        try:
            us.fetch_html("https://uzex.uz/Trade/Offer/106")
            refused = False
        except PermissionError:
            refused = True
        ok("fetch_html('/Trade/Offer/106') — PermissionError, на сайт ни одного обращения", refused and not calls, calls)
        us._robots.update(text=None, reason="robots.txt недоступен (URLError)", at=time.monotonic())
        try:
            us.fetch_html("https://uzex.uz/Trade/List")
            closed = False
        except PermissionError:
            closed = True
        ok("robots.txt не прочитан — сайт считается закрытым", closed and not calls)
    finally:
        us._http_get = orig
        us._robots.update(text=None, reason="", at=0.0)
    ok("User-Agent честный, с адресом для связи", us.USER_AGENT == "ai-surveyor (INSON) contact: "
                                                                 "j.mirzaumarov@insuranceon.uz")
    ok("пауза между обращениями не меньше 2 с", us.MIN_DELAY_SEC >= 2.0)


# ------------------------------------------------------------------ 5. база
def _row(name, price_unit_t, days_ago, key, fetched="2026-10-02", unit="тонна", qty=1.0, page="List"):
    td = (date.today() - timedelta(days=days_ago)).isoformat()
    un, pun = us.norm_unit(unit, price_unit_t)
    return {"page": page, "contract_no": key.split("|")[0], "deal_key": key, "name": name, "grp": us.classify(name),
            "lot_qty": qty, "unit": unit, "unit_norm": un, "price_raw": price_unit_t * qty,
            "price_lot": price_unit_t * qty, "price_unit": price_unit_t, "price_unit_norm": pun,
            "price_basis": us.PAGES[page]["price_basis"], "currency": "UZS", "warehouse": None,
            "trade_date": td if page == "List" else None, "date_basis": us.DATE_DEAL,
            "contract_type": "внутренний", "deal_status": None, "fetched_at": fetched + "T03:00:00",
            "fetched_date": fetched, "url": us.page_url(page)}


TEST_ROWS = [
    _row("Дизельное топливо ЭКО", 14_000_000, 2, "d1|1"),
    _row("Дизельное топливо ЭКО", 15_000_000, 3, "d2|1"),
    _row("Дизельное топливо ЕВРО", 14_600_000, 5, "d3|1"),
    _row("Дизельное топливо ЭКО", 14_500, 4, "d4|1", unit="килограмм"),      # 14 500 за кг = 14,5 млн за т
    _row("Дизельное топливо ЭКО", 9_000_000, 45, "d5|1"),                    # старше 30 дней — в медиану не идёт
    _row("Арматура 12 ст 35 ГС", 8_610_000, 1, "m1|1"),
    _row("Портландцемент II/А-И 32,5 Н", 900_000, 6, "c1|1"),
]


def check_db():
    print("5. Запись с историей и медианы (копия базы)")
    db.ensure_schema()
    with db.tx() as con:
        con.execute("DELETE FROM exchange_quotes")
        s1 = us.save_rows(con, TEST_ROWS)
        # тот же реестр на следующий день: сделки те же — история растёт, медиана нет
        s2 = us.save_rows(con, [dict(r, fetched_date="2026-10-03", fetched_at="2026-10-03T03:00:00")
                                for r in TEST_ROWS])
        s3 = us.save_rows(con, [dict(TEST_ROWS[0], fetched_at="2026-10-03T09:00:00", fetched_date="2026-10-03")])
        n = con.execute("SELECT COUNT(*) FROM exchange_quotes").fetchone()[0]
        summ = us.summary(con, days=30)
        d = next(x for x in summ if x["group"] == "diesel")
        empty = us.summary(con, days=30, as_of="2020-01-01")
        q = us.quotes(con, group="diesel")
        q2 = us.quotes(con, q="арматура")
    ok("7 строк вставлено, на следующий день ещё 7 (история не затирается), повтор в тот же день — обновление",
       s1["вставлено"] == 7 and s2["вставлено"] == 7 and s3 == {"вставлено": 0, "обновлено": 1, "всего": 1}
       and n == 14, (s1, s2, s3, n))
    ok("дизель за 30 дней: 4 сделки (каждая один раз, хотя загружена дважды), медиана 14 550 000 сум/т, "
       "килограммы приведены к тоннам, сделка 45-дневной давности не учтена",
       d["deals"] == 4 and d["median_unit_price"] == 14_550_000 and d["unit"] == "т"
       and d["min"] == 14_000_000 and d["max"] == 15_000_000
       and d["last_date"] == (date.today() - timedelta(days=2)).isoformat() and d["url"].startswith("https://uzex.uz"),
       d)
    ok("группы сводки: дизель, металл, цемент", [x["group"] for x in summ] == ["diesel", "metal", "cement"],
       [x["group"] for x in summ])
    ok("на дату без сделок — пусто", empty == [])
    ok("выдача строк: дизель — 5 строк (последняя загрузка каждой), поиск «арматура» — 1",
       q["total"] == 5 and q2["total"] == 1 and q2["rows"][0]["group_label"].startswith("металл"), (q["total"], q2))


# ------------------------------------------------------------------ 6. API
TOKENS = {}
PEOPLE = [("тест-uzex-агент", "агент"), ("тест-uzex-админ", "админ")]


def call(method, path, params=None, who=None):
    from app.main import app
    query = urlencode(params or {}, encoding="utf-8")
    hdrs = [(b"host", b"test"), (b"content-type", b"application/json"), (b"content-length", b"0")]
    if who:
        hdrs.append((b"cookie", f"sid={TOKENS[who]}".encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": path.encode(), "root_path": "", "query_string": query.encode(),
             "headers": hdrs, "client": ("10.0.0.5", 0), "server": ("test", 80)}
    out = {"status": None, "chunks": []}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
        elif msg["type"] == "http.response.body":
            out["chunks"].append(msg.get("body") or b"")

    asyncio.run(app(scope, receive, send))
    raw = b"".join(out["chunks"]).decode("utf-8", "replace")
    try:
        return out["status"], _json.loads(raw)
    except ValueError:
        return out["status"], raw


def check_api():
    print("6. API /exchange/* (копия базы, сеть подменена)")
    with db.tx() as con:
        ts = db.now()
        for login, role in PEOPLE:
            cur = con.execute("INSERT INTO users (login, full_name, role, branch, password_hash, salt, status, "
                              "created_at) VALUES (?,?,?,?,?,?,?,?)",
                              (login, "Тестовый " + role, role, "тест-uzex", "x", "y", "активен", ts))
            u = db.rows(con, "SELECT * FROM users WHERE id=?", cur.lastrowid)[0]
            TOKENS[login], _ = auth.create_session(con, u, "127.0.0.1", "test")
    st0, _ = call("GET", "/exchange/summary")
    ok("без входа — 401", st0 == 401, st0)
    st, s = call("GET", "/exchange/summary", who="тест-uzex-агент")
    ok("агент читает сводку: дизель 14 550 000 сум/т по 4 сделкам, ссылка на реестр, ограничения перечислены",
       st == 200 and s["available"] and s["items"][0]["group"] == "diesel"
       and s["items"][0]["median_unit_price"] == 14_550_000 and s["url"] == "https://uzex.uz/Trade/List"
       and len(s["limitations"]) >= 3, (st, s))
    st, q = call("GET", "/exchange/quotes", {"group": "metal", "days": 30}, who="тест-uzex-агент")
    ok("агент читает строки по группе: 1 арматура, у строки url", st == 200 and q["total"] == 1
       and q["rows"][0]["url"].startswith("https://uzex.uz/"), (st, q))
    st, _ = call("GET", "/exchange/quotes", {"group": "золото"}, who="тест-uzex-агент")
    ok("незнакомая группа — 422", st == 422, st)
    st, _ = call("POST", "/exchange/refresh", who="тест-uzex-агент")
    ok("refresh агентом — 403", st == 403, st)

    html = (ROOT / "tests" / "fixtures" / "uzex_c.html").read_text(encoding="utf-8")
    orig = us.fetch_quotes
    seen = []

    def fake(page, max_pages=None, stop_before=None):
        seen.append(page)
        if page == "List":
            raise PermissionError("robots.txt uzex.uz запрещает (проверка теста)")
        return us.parse_page(page, html, db.now(), us.page_url(page))
    us.fetch_quotes = fake
    try:
        st, r = call("POST", "/exchange/refresh", who="тест-uzex-админ")
    finally:
        us.fetch_quotes = orig
    by = {x["page"]: x for x in (r.get("страницы") if isinstance(r, dict) else []) or []}
    ok("refresh админом — 200; реестр «запрещено robots.txt» (статус, а не падение), лист — 10 строк, "
       "пометка «на дату загрузки»",
       st == 200 and seen == ["List", "ContractsSumNew"] and by["List"]["status"] == us.STATUS_ROBOTS
       and by["ContractsSumNew"]["status"] == us.STATUS_OK and by["ContractsSumNew"]["rows"] == 10
       and "на дату загрузки" in by["ContractsSumNew"]["date_note"], (st, r))
    st, s2 = call("GET", "/exchange/summary", who="тест-uzex-агент")
    ok("котировки без даты в медиану не попали: дизель по-прежнему 4 сделки",
       st == 200 and s2["items"][0]["deals"] == 4, s2.get("items", [None])[0] if isinstance(s2, dict) else s2)
    st, stt = call("GET", "/exchange/status", who="тест-uzex-агент")
    ok("статус: строки, дата последней сделки, страницы ежедневной загрузки", st == 200 and stt["rows"] > 0
       and stt["daily_pages"] == ["List", "ContractsSumNew"], stt)


# ------------------------------------------------------------------ 7. справка акта
def check_act_background():
    print("7. Справка биржи для акта (act_analytics.exchange_background, только чтение)")
    with db.tx() as con:
        a7 = aa.exchange_background(con, "7", None, None, {"cargo_kind": "дизельное топливо", "cargo_group": "bulk"})
        a7b = aa.exchange_background(con, "7", None, None, {"cargo_group": "bulk"})
        a7f = aa.exchange_background(con, "7", None, None, {"cargo_kind": "смартфоны", "cargo_group": "valuable"})
        a8o = aa.exchange_background(con, "8", "office", None, {})
        a9w = aa.exchange_background(con, "9", "warehouse", None, {})
        a3 = aa.exchange_background(con, "3", "car", None, {})
        parts = aa.exchange_background(con, "13", None, [{"class_code": "8", "object_kind": "warehouse",
                                                          "object_description": "склад цемента", "fields": {}}], {})
        old = aa.exchange_background(con, "7", None, None, {"cargo_kind": "дизельное топливо"}, as_of="2020-01-01")
        n_before = con.execute("SELECT COUNT(*) FROM exchange_quotes").fetchone()[0]
    it = (a7.get("items") or [{}])[0]
    ok("класс 7, груз «дизельное топливо»: справка есть — дизель, медиана 14 550 000 сум/т, 4 сделки, url; пометка",
       a7["available"] and a7["basis"] == "text" and it.get("group") == "diesel"
       and it.get("median_unit_price") == 14_550_000 and it.get("deals") == 4 and it.get("url")
       and a7["note"] == aa.EXCHANGE_NOTE, a7)
    ok("класс 7, только вид груза «наливной/насыпной»: дизель и цемент из набора (бензина и зерна в базе нет)",
       a7b["available"] and a7b["basis"] == "cargo_group" and [i["group"] for i in a7b["items"]] == ["diesel", "cement"],
       a7b)
    ok("класс 7, смартфоны (дорогой груз) — справка не нужна", not a7f["available"] and a7f["reason"] == "not_relevant")
    ok("класс 8, офис — не нужна; класс 3 — не нужна",
       not a8o["available"] and a8o["reason"] == "not_relevant" and not a3["available"])
    ok("класс 9, склад без вида запасов — типовой набор (дизель, металл, цемент) с пометкой default",
       a9w["available"] and a9w["basis"] == "default"
       and [i["group"] for i in a9w["items"]] == ["diesel", "metal", "cement"], a9w)
    ok("часть комплексного продукта «склад цемента» (класс 8) — цемент",
       parts["available"] and [i["group"] for i in parts["items"]] == ["cement"], parts)
    ok("нет сделок за период — available false, reason no_data", not old["available"] and old["reason"] == "no_data",
       old)
    ok("справка только читает базу: строк не прибавилось", n_before > 0)


def main():
    print("Биржа УзРТСБ (uzex.uz)")
    check_parse()
    check_robots()
    with temp_db("surveyor-uzex.db"):
        check_db()
        check_api()
        check_act_background()
    print(f"\nитог: ок {passed}, плохо {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
