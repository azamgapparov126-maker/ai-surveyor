"""
Оценка стоимости V по ТЗ разработчику 13.10.2026 (раздел 3, дни 5–6): аналоги avtoelon.uz (Cobalt 2022 по сохранённой
выдаче), износ без аналогов, S/V и решение «Принять рыночную» / «Оставить с пояснением», биржевая цена VWAP для груза
класса 7, затратный метод для магазина 450 м², отсутствие цены 1 м².

Запуск из корня:
    set PYTHONIOENCODING=utf-8
    sandbox\\.venv\\Scripts\\python.exe tests\\test_valuation_tz.py

Без сети: любое обращение наружу — ошибка теста (valuation_sources._http_get и llm._post подменены). Всё — во временной
копии базы (tests/tmpdb.py). Выдача avtoelon.uz — tests/fixtures/avtoelon_cobalt_2022.html (живой ответ 13.10.2026,
7 страниц ?year[from]=2022&page=N; сохранён только список объявлений, адрес клиента заменён на 0.0.0.0).
"""
import asyncio
import json as _json
import os
import secrets
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

os.environ.pop("SURVEYOR_DEV", None)
os.environ["SURVEYOR_NO_BACKGROUND"] = "1"

from tmpdb import ensure_market_stats, ensure_stat_series, temp_db   # noqa: E402
from app import act_engine as ae, db, guest, llm                      # noqa: E402
from app import valuation as V, valuation_sources as vs               # noqa: E402
from app.main import app                                              # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "avtoelon_cobalt_2022.html"
SEP = "\n<!-- INSON-PAGE-BREAK -->\n"
RATE = 11809.82                    # курс ЦБ из базы на 22.09.2026 — тот же, что при живом запросе
TODAY = date(2026, 10, 13)

passed, failed = 0, 0
COOKIES, HEADERS = {}, []


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


# ------------------------------------------------------------------ ASGI-клиент (как в test_act.py)

def call(method, path, body=None, params=None):
    payload = _json.dumps(body, ensure_ascii=False).encode() if body is not None else b""
    hdrs = [(b"host", b"test"), (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode())]
    if COOKIES:
        hdrs.append((b"cookie", "; ".join(f"{k}={v}" for k, v in COOKIES.items()).encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "",
             "query_string": urlencode(params or {}, encoding="utf-8").encode(),
             "headers": hdrs + list(HEADERS), "client": ("203.0.113.9", 0), "server": ("test", 80)}
    out = {"status": None, "chunks": [], "headers": []}

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"], out["headers"] = msg["status"], msg.get("headers") or []
        elif msg["type"] == "http.response.body":
            out["chunks"].append(msg.get("body") or b"")

    asyncio.run(app(scope, receive, send))
    for k, v in out["headers"]:
        if k.lower() == b"set-cookie":
            name, _, value = v.decode("latin-1").split(";")[0].partition("=")
            COOKIES[name.strip()] = value.strip()
    text = b"".join(out["chunks"]).decode("utf-8", "replace")
    try:
        return out["status"], _json.loads(text)
    except ValueError:
        return out["status"], text


def admin_header() -> tuple:
    now = datetime.now().isoformat(timespec="seconds")
    token = secrets.token_urlsafe(32)
    with db.tx() as con:
        cur = con.execute("INSERT INTO users (login, full_name, role, branch, password_hash, salt, status, created_at,"
                          " approved_by, approved_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          ("val_tz_admin", "Test Admin", "админ", "тест", secrets.token_hex(32), secrets.token_hex(16),
                           "активен", now, "test", now))
        con.execute("INSERT INTO sessions (token, user_id, created_at, expires_at, ip, user_agent) VALUES (?,?,?,?,?,?)",
                    (token, cur.lastrowid, now, (datetime.now() + timedelta(hours=2)).isoformat(timespec="seconds"),
                     "127.0.0.1", "test_valuation_tz"))
    return (b"authorization", f"Bearer {token}".encode())


def hand_q(xs, p):
    xs = sorted(xs)
    pos = p * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


FX = {"rate": RATE, "date": "2026-09-22", "source": "курс ЦБ РУз на 22.09.2026 (тест)", "stale": True}


# ------------------------------------------------------------------ 1. robots.txt и адреса

ROBOTS_TEXT = """User-agent: *
Disallow: /index/br
Disallow: /*change*
Disallow: /ajax/
Disallow: /*raion=
Disallow: /my$
Disallow: /*?*
Disallow: /*?*=*=
Allow: /*/?page=
Allow: /*/?year[from]=
Allow: /*/?_txt_=
"""


def check_robots():
    print("1. robots.txt avtoelon.uz (снимок 13.10.2026) и адреса выдачи")
    u1 = vs.analog_url("Chevrolet", "Cobalt", 2022, 1)
    u2 = vs.analog_url("Chevrolet", "Cobalt", 2022, 3)
    ok("адреса: только ?year[from]= и &page=",
       u1 == "https://avtoelon.uz/avto/chevrolet/cobalt/?year%5Bfrom%5D=2022"
       and u2 == "https://avtoelon.uz/avto/chevrolet/cobalt/?year%5Bfrom%5D=2022&page=3", (u1, u2))
    ok("оба адреса разрешены robots.txt (robotparser и RFC 9309)",
       vs.robots_allowed_text(ROBOTS_TEXT, u1)[0] and vs.robots_allowed_text(ROBOTS_TEXT, u2)[0])
    bad = "https://avtoelon.uz/avto/chevrolet/cobalt/?sort=price"
    ok("чужой параметр запрещён и robots.txt, и списком ТЗ",
       not vs.robots_allowed_text(ROBOTS_TEXT, bad)[0] and not vs.query_ok(bad))
    ok("urllib.robotparser сам «*» не понимает — RFC-проверка закрывает разрыв",
       not vs.rfc_allowed(ROBOTS_TEXT, bad))
    ok("/my$ запрещён", not vs.robots_allowed_text(ROBOTS_TEXT, "https://avtoelon.uz/my")[0])
    # запрет robots.txt — адаптер в сеть за выдачей не идёт
    saved = (vs.robots_check, vs._http_get)
    vs.robots_check = lambda url: (False, "robots.txt запрещает (тест)")
    vs._http_get = no_network
    try:
        r = vs.fetch_avtoelon_analogs("Chevrolet", "Cobalt", 2022, TODAY, RATE)
    finally:
        vs.robots_check, vs._http_get = saved
    ok("запрет robots.txt — статус «запрещено robots.txt», без запроса выдачи",
       r["status"] == vs.STATUS_ROBOTS and not r["analogs"], r)


# ------------------------------------------------------------------ 2. Cobalt 2022 по сохранённой выдаче

def cobalt(con, **kw):
    pages = FIXTURE.read_text(encoding="utf-8").split(SEP)
    args = dict(brand="Chevrolet", model="Cobalt", year=2022, as_of=TODAY, sum_insured=150_000_000, pages=pages,
                fx=dict(FX))
    args.update(kw)
    return V.vehicle_valuation(con, **args)


def check_cobalt():
    print("2. Cobalt 2022: аналоги, выбросы, V, надёжность, предупреждение")
    pages = FIXTURE.read_text(encoding="utf-8").split(SEP)
    ok("fixture: 7 страниц живой выдачи", len(pages) == 7, len(pages))
    with db.tx() as con:
        r = cobalt(con)
    ok("метод analogs, 19 аналогов, 1 отсечён", r["method"] == "analogs" and r["available"] and r["n"] == 19
       and r["n_cut"] == 1 and r["n_found"] == 20, {k: r.get(k) for k in ("method", "n", "n_cut", "n_found")})
    ok("отсечён 5 000 у.е.", [c["price"] for c in r["cut"]] == [5000.0] and r["cut"][0]["currency"] == "USD",
       r["cut"])
    # руками: цены 19 аналогов 2022 года в сумах → медиана × 0,95 × 0,88^0 (Δ = 0: тот же год, ТЗ 14.10.2026 К2;
    # пробег и состояние не указаны — K = 1)
    prices = [a["price_uzs"] for a in r["analogs"]]
    all_p = prices + [5000.0 * RATE]
    q1, q3 = hand_q(all_p, 0.25), hand_q(all_p, 0.75)
    ok("границы выбросов Q1 − 1,5·IQR и Q3 + 1,5·IQR — руками",
       r["bounds"]["low"] == round(q1 - 1.5 * (q3 - q1)) and r["bounds"]["high"] == round(q3 + 1.5 * (q3 - q1)),
       r["bounds"])
    ok("все аналоги 2022 года, свежее 6 месяцев",
       all(a["year"] == 2022 for a in r["analogs"]) and all(a["published"] >= "2026-04-13" for a in r["analogs"]))
    v_hand = round(hand_q([p * 0.95 * 0.88 ** 0 for p in prices], 0.5))
    ok("V = median(P × 0,95 × 0,88^Δ), Δ = 0 — руками; 105–117 млн", r["V"] == v_hand
       and 105_000_000 <= r["V"] <= 117_000_000, (r["V"], v_hand))
    ok("k_age и Δ по каждому аналогу в act_json.valuation.k и в пояснении",
       r["k"]["k_age"] == 0.88 and "k_market" not in r["k"] and len(r["k"]["age"]) == 19
       and all(x["delta"] == 0 and x["k_age"] == 1.0 for x in r["k"]["age"])
       and all(a["delta"] == 0 and a["k_age"] == 1.0 for a in r["analogs"])
       and any("Δ = 0" in st and "k_age = 0,88" in st for st in r["steps"]), (r["k"], r["steps"]))
    a1, a3 = hand_q([p * 0.95 for p in prices], 0.25), hand_q([p * 0.95 for p in prices], 0.75)
    rel_pct = round((a3 - a1) / hand_q([p * 0.95 for p in prices], 0.5) * 100, 1)
    lvl = "high" if rel_pct <= 15 else "medium" if rel_pct <= 30 else "low"
    ok("надёжность (Q3 − Q1)/V и уровень", r["reliability_pct"] == rel_pct and r["reliability"] == lvl,
       (r["reliability_pct"], r["reliability"], rel_pct))
    pct = round(150_000_000 / r["V"] * 100)
    ok("предупреждение «%d %% от рынка» при S 150 млн" % pct,
       any(("%d %% от рынка" % pct) in w for w in r["warnings"]) and r["sv_warning"]["code"] == "sv_over", r["warnings"])
    ok("единая структура: method, V, range, n, reliability, source, date, formula",
       all(k in r for k in ("method", "V", "range", "n", "reliability", "source", "date", "formula"))
       and r["source"]["name"] == "avtoelon.uz" and r["date"] == "2026-10-13"
       and r["settings"]["version"] == "1.0" and r["settings"]["date"] == "2026-10-13" and r["calibrated"] == 0, r)
    ok("ссылки на объявления и на страницы выдачи",
       all(a["url"].startswith("https://avtoelon.uz/a/show/") for a in r["analogs"])
       and r["source"]["urls"][0] == "https://avtoelon.uz/avto/chevrolet/cobalt/?year%5Bfrom%5D=2022")
    ok("курс в расчёте и пометка о старом курсе",
       r["fx"]["rate"] == RATE and any("курс доллара старше 7 дней" in w for w in r["warnings"]))
    # пробег и состояние
    with db.tx() as con:
        r2 = cobalt(con, mileage=120_000, condition="worn")
    k = 1 - 0.15 * (120_000 / (20_000 * 4) - 1)
    ok("K_пробег = clip(1 − 0,15·(120 000/80 000 − 1)) = 0,925; K_сост «worn» = 0,95",
       abs(r2["k"]["mileage"]["k"] - 0.925) < 1e-9 and r2["k"]["condition"]["k"] == 0.95
       and r2["V"] == round(hand_q([p * 0.95 * k * 0.95 for p in prices], 0.5)), r2["k"])
    km = V.k_mileage(0, "km", 4, V.val_settings(None, {}))
    km2 = V.k_mileage(400_000, "km", 4, V.val_settings(None, {}))
    eh = V.k_mileage(3_000, "hours", 4, V.val_settings(None, {}))
    ok("K_пробег в пределах 0,85–1,15; моточасы — норма 1 500 в год",
       km["k"] == 1.15 and km2["k"] == 0.85 and eh["norm"] == 6000 and abs(eh["k"] - 1.075) < 1e-9, (km, km2, eh))
    return r


def check_depreciation():
    print("3. Износ без аналогов (запасной метод)")
    vs_ = V.val_settings(None, {})
    d = V.depreciation_value(200_000_000, 2022, TODAY, vs_=vs_)
    share = 0.8 * 0.88 ** 3
    ok("2022 → 2026: 0,8 × 0,88³", d["V"] == round(200_000_000 * share) and d["method"] == "depreciation"
       and d["reliability"] == "low", d)
    d2 = V.depreciation_value(200_000_000, 2000, TODAY, vs_=vs_)
    ok("не ниже 15 % цены нового", d2["V"] == 30_000_000 and d2["floor_applied"], d2)
    ok("новый (тот же год) — без износа", V.depreciation_value(100, 2026, TODAY, vs_=vs_)["V"] == 100)
    with db.tx() as con:
        r = cobalt(con, year=2010, price_new=150_000_000)
    ok("нет аналогов 2010 года в выдаче — износ от цены нового с причиной",
       r["method"] == "depreciation" and r["V"] == 22_500_000 and "аналогов" in (r.get("fallback_reason") or ""), r)
    with db.tx() as con:
        r = cobalt(con, year=2010)
    ok("нет аналогов и цены нового — честное «нет оценки»",
       not r["available"] and "цена нового" in r["reason"], r)


# ------------------------------------------------------------------ 4. S/V в акте и решение по нему

CAR_MUST = {"product_code": "0308", "sum_insured": 150_000_000, "object_value": 150_000_000, "region": "Ташкент"}


def save_valuation(con, res) -> int:
    cur = con.execute("INSERT INTO valuations (created_at, created_by, object_type, params_json, declared_value, "
                      "ai_value, method, method_version, explanation) VALUES (?,?,?,?,?,?,?,?,?)",
                      (db.now(), "test", "авто", "{}", 150_000_000, res["V"], "объявления", "tz-2026-10-13/1.0",
                       _json.dumps({"valuation": res}, ensure_ascii=False, default=str)))
    return cur.lastrowid


def check_sv(res):
    print("4. S/V в акте, «Принять рыночную» и «Оставить с пояснением»")
    with db.tx() as con:
        vid = save_valuation(con, res)
    Vv = res["V"]
    st, a = call("POST", "/act/make", {"lang": "ru", "must": CAR_MUST,
                                       "optional": {"object_kind": "car", "year": 2022, "valuation_id": vid}})
    ok("акт сформирован", st == 200, (st, a if st != 200 else ""))
    if st != 200:
        return
    sv = a["value_sv"]
    ok("S 150 млн при V → «страхование сверх страховой стоимости», ГК ст. 938",
       sv["verdict"] == "over" and sv["label"] == "страхование сверх страховой стоимости" and sv["legal_ref"] == "ГК РУз, ст. 938"
       and sv["ratio_pct"] == round(150_000_000 / Vv * 100, 2) and "938" in sv["text"], sv)
    ok("в акте act_json.valuation — метод analogs и формула",
       a["valuation"]["method"] == "analogs" and a["valuation"]["V"] == Vv and a["valuation"]["formula"]
       and a["valuation"]["valuation_id"] == vid
       and a["valuation"]["k"]["k_age"] == 0.88 and len(a["valuation"]["k"]["age"]) == a["valuation"]["n"],
       a["valuation"])
    ok("кнопки решения", sv["actions"] == ["accept_market", "keep_with_note"])
    p_before = a["premium"]["amount"]
    st, b = call("POST", f"/act/{a['id']}/value-decision", {"accept_market": False})
    ok("«Оставить» без пояснения — 422", st == 422, (st, b))
    st, b = call("POST", f"/act/{a['id']}/value-decision", {"accept_market": True})
    ok("«Принять рыночную» — новая версия акта", st == 200 and b["id"] != a["id"] and b["version"] == 2
       and b["parent_id"] == a["id"], (st, b if st != 200 else {k: b.get(k) for k in ("id", "version")}))
    if st != 200:
        return
    ok("S = V, стоимость = V", b["value_decision"]["after"]["sum_insured"] == Vv
       and b["value_sv"]["verdict"] == "normal" and b["value_sv"]["ratio_pct"] == 100.0, b["value_decision"])
    p_after = b["premium"]["amount"]
    ok("премия пересчитана пропорционально S (%s → %s)" % (p_before, p_after),
       p_after < p_before and abs(p_after / p_before - Vv / 150_000_000) < 0.002, (p_before, p_after))
    ok("в акте — решение, было и стало", b["value_decision"]["accept_market"] is True
       and b["value_decision"]["before"]["sum_insured"] == 150_000_000
       and b["value_decision"]["before"]["premium"] == p_before and b["value_decision"]["after"]["premium"] == p_after)
    st, old = call("GET", f"/act/{a['id']}")
    ok("прежняя версия не изменилась", st == 200 and old["premium"]["amount"] == p_before and old["version"] == 1)
    note = "Клиент подтверждает стоимость договором купли-продажи"
    st, c = call("POST", f"/act/{a['id']}/value-decision", {"accept_market": False, "note": note})
    ok("«Оставить с пояснением»: S прежняя, пояснение в акте",
       st == 200 and c["value_decision"]["note"] == note and c["premium"]["amount"] == p_before
       and c["value_sv"]["verdict"] == "over" and c["version"] == 2, (st, c.get("value_decision") if st == 200 else c))
    st, d = call("POST", f"/act/{a['id']}/value-decision", {"accept_market": False, "note": "звонить +998 90 123 45 67"})
    ok("пояснение с телефоном — 422", st == 422, (st, d))
    saved = dict(COOKIES)
    COOKIES.clear()
    st, _ = call("POST", f"/act/{a['id']}/value-decision", {"accept_market": True})
    ok("чужой акт — 404", st == 404, st)
    COOKIES.update(saved)
    st, a2 = call("POST", "/act/make", {"lang": "ru", "must": CAR_MUST, "optional": {"object_kind": "car"}})
    ok("без оценки — value_sv нет, решение 409", st == 200 and a2["value_sv"] is None and a2["valuation"] is None)
    st, _ = call("POST", f"/act/{a2['id']}/value-decision", {"accept_market": True})
    ok("решение без оценки — 409", st == 409, st)
    # S в норме ±10 %: 105 % — норма
    sv2 = V.sv_check(105, 100, V.val_settings(None, {}))
    sv3 = V.sv_check(85, 100, V.val_settings(None, {}))
    ok("S/V 105 % — норма, 85 % — недострахование (ст. 936)", sv2["verdict"] == "normal" and sv3["verdict"] == "under"
       and sv3["legal_ref"] == "ГК РУз, ст. 936", (sv2, sv3))


# ------------------------------------------------------------------ 5. Биржа: VWAP для груза класса 7

def seed_quotes(con, rows):
    for i, (days_ago, qty, unit, price_norm) in enumerate(rows):
        d = (date.today() - timedelta(days=days_ago)).isoformat()
        con.execute("INSERT INTO exchange_quotes (page, contract_no, deal_key, name, grp, lot_qty, unit, unit_norm, "
                    "price_raw, price_unit, price_unit_norm, currency, trade_date, date_basis, fetched_at, fetched_date,"
                    " url) VALUES ('List',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("T%d" % i, "test-%d-%s" % (i, secrets.token_hex(3)), "Дизельное топливо ЭКО", "diesel", qty, unit,
                     "т", price_norm, price_norm, price_norm, "UZS", d, "дата сделки", db.now(), d,
                     "https://uzex.uz/Trade/List"))


def check_exchange():
    print("5. Груз класса 7: биржевая цена VWAP")
    with db.tx() as con:
        con.execute("DELETE FROM exchange_quotes")
        seed_quotes(con, [(2, 60, "тонна", 12_000_000), (5, 20, "тонна", 12_600_000), (10, 20_000, "килограмм",
                                                                                         11_800_000),
                          (60, 100, "тонна", 9_000_000)])
    vw = (60 * 12_000_000 + 20 * 12_600_000 + 20 * 11_800_000) / 100
    st, a = call("POST", "/act/make", {"lang": "ru", "must": {"product_code": "0701", "sum_insured": 1_500_000_000,
                                                              "object_value": 1_500_000_000, "region": "Ташкент"},
                                       "optional": {"object_kind": "cargo", "class_fields": {
                                           "cargo_kind": "дизельное топливо"}, "quantity": 120, "unit": "т"}})
    v = (a or {}).get("valuation") or {}
    ok("груз 120 т получает стоимость по бирже", st == 200 and v.get("method") == "exchange"
       and v.get("V") == round(120 * vw), (st, v))
    ok("число сделок и период 30 дней", v.get("n") == 3 and v["period"]["days"] == 30 and not v["period"]["fallback"]
       and v["source"]["group"] == "diesel" and v["vwap"] == round(vw, 2), v)
    ok("кг приведены к тоннам (20 000 кг = 20 т)", abs(v["volume"] - 100) < 1e-9, v.get("volume"))
    ok("формула и S/V", "Σ(p·q)" in v["formula"] and a["value_sv"]["verdict"] in ("normal", "over", "under"))
    with db.tx() as con:
        con.execute("DELETE FROM exchange_quotes")
        seed_quotes(con, [(3, 10, "тонна", 12_000_000), (40, 30, "тонна", 10_000_000), (70, 60, "тонна", 11_000_000)])
    st, a = call("POST", "/act/make", {"lang": "ru", "must": {"product_code": "0701", "sum_insured": 1_000_000_000,
                                                              "object_value": 1_000_000_000, "region": "Ташкент"},
                                       "optional": {"object_kind": "cargo", "class_fields": {
                                           "cargo_kind": "дизельное топливо"}, "quantity": 100000, "unit": "кг"}})
    v = a["valuation"]
    vw90 = (10 * 12_000_000 + 30 * 10_000_000 + 60 * 11_000_000) / 100
    ok("меньше 3 сделок за 30 дней — период 90 дней", v["period"]["days"] == 90 and v["period"]["fallback"]
       and v["n"] == 3 and v["V"] == round(100 * vw90) and any("90 дней" in w for w in v["warnings"]), v)
    st, e = call("POST", "/act/make", {"lang": "ru", "must": {"product_code": "0701", "sum_insured": 1, "object_value": 1,
                                                              "region": "Ташкент"},
                                       "optional": {"quantity": 5}})
    ok("количество без единицы — 422", st == 422 and "unit" in (e.get("errors") or {}), (st, e))


# ------------------------------------------------------------------ 5а. Биржа: V внутри своего диапазона

DEALS_FIXTURE = ROOT / "tests" / "fixtures" / "uzex_deals_diesel_grain.json.gz"


def load_deals_fixture(con) -> int:
    """Реестр сделок uzex.uz 08–13.10.2026 (дизтопливо и зерно, 5 824 сделки, без номеров контрактов) — в
    exchange_quotes; даты сдвинуты так, чтобы последняя сделка была вчера. Цена за единицу — как в parse_page."""
    import gzip
    from app import uzex_sources as us
    data = _json.loads(gzip.decompress(DEALS_FIXTURE.read_bytes()).decode("utf-8"))
    last = max(date.fromisoformat(r[4]) for r in data["rows"])
    shift = (date.today() - timedelta(days=1)) - last
    for i, (name, unit, qty, total, td) in enumerate(data["rows"]):
        d = (date.fromisoformat(td) + shift).isoformat()
        pu = round(total / qty, 4)
        un, pun = us.norm_unit(unit, pu)
        con.execute("INSERT INTO exchange_quotes (page, contract_no, deal_key, name, grp, lot_qty, unit, unit_norm, "
                    "price_raw, price_lot, price_unit, price_unit_norm, currency, trade_date, date_basis, fetched_at,"
                    " fetched_date, url) VALUES ('List',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("F%d" % i, "fx-%d" % i, name, us.classify(name), qty, unit, un, total, total, pu, pun, "UZS", d,
                     "дата сделки", db.now(), d, data["source"]))
    return len(data["rows"])


def _raw_vwap(con, grp):
    """Прежний расчёт (до 13.10.2026): все сделки группы в тоннах, без фильтров — для документирования дефекта."""
    from app import uzex_sources as us
    rows = db.rows(con, "SELECT name, unit, lot_qty, price_unit_norm FROM exchange_quotes WHERE grp=? AND unit_norm='т'",
                   grp)
    pq = q = 0.0
    ps = []
    for r in rows:
        k = us.UNITS[r["unit"].lower()][1]
        pq += r["price_unit_norm"] * r["lot_qty"] / k
        q += r["lot_qty"] / k
        ps.append(r["price_unit_norm"])
    ps.sort()
    return pq / q, ps[len(ps) // 4], ps[3 * len(ps) // 4]


def check_exchange_range():
    print("5а. Биржа: V внутри диапазона, одна выборка для V, диапазона и надёжности")
    vs_ = V.val_settings(None, {})
    with db.tx() as con:
        con.execute("DELETE FROM exchange_quotes")
        n = load_deals_fixture(con)
        raw, rq1, rq3 = _raw_vwap(con, "diesel")
        ok("fixture: %d сделок; прежний VWAP дизтоплива %s вне [%s; %s] (форварды)" % (n, V._fmt(raw), V._fmt(rq1),
                                                                                 V._fmt(rq3)),
           n > 5000 and not (rq1 <= raw <= rq3), (raw, rq1, rq3))
        for grp, qty, unit, hint in (("diesel", 120, "т", "дизельное топливо"), ("grain", 300, "т", "пшеница"),
                                     ("grain", 300_000, "кг", "пшеница 3 класс")):
            v = V.exchange_value(con, groups=[grp], quantity=qty, unit=unit, vs_=vs_, hint=hint)
            r = v.get("range") or {}
            ok("%s %s %s: V %s внутри [%s; %s]" % (grp, V._fmt(qty), unit, V._fmt(v.get("V")), V._fmt(r.get("low")),
                                                   V._fmt(r.get("high"))),
               v.get("available") and r["low"] <= v["V"] <= r["high"], v)
            rel, pct = V.reliability_of(v["q1"], v["q3"], v["price"], vs_)
            ok("  надёжность по той же выборке (%s, %s %%)" % (v["reliability"], v["reliability_pct"]),
               (rel, pct) == (v["reliability"], v["reliability_pct"])
               and r["low"] == round(v["quantity"]["normalized"] * v["q1"]), v)
            if grp == "diesel":
                ok("  форварды исключены до расчёта (10), выбросы — по IQR",
                   v["sample"]["excluded_forward"] == 10 and v["sample"]["excluded_iqr"] > 0
                   and v["n"] == v["sample"]["deals_raw"] - 10 - v["sample"]["excluded_iqr"], v["sample"])
                ok("  цена дизтоплива 16–18 млн сум/т", 16e6 <= v["price"] <= 18e6, v["price"])
            else:
                ok("  пшеница отделена от зерноотходов и муки, цена 3–3,5 млн сум/т",
                   v["sample"]["subtype"] and v["sample"]["excluded_subtype"] > 0 and 3e6 <= v["price"] <= 3.5e6,
                   (v["price"], v["sample"]))
        v300t = V.exchange_value(con, groups=["grain"], quantity=300, unit="т", vs_=vs_, hint="пшеница")
        v300k = V.exchange_value(con, groups=["grain"], quantity=300_000, unit="кг", vs_=vs_, hint="пшеница")
        ok("300 т = 300 000 кг — одна и та же оценка", v300t["V"] == v300k["V"] and v300t["range"] == v300k["range"])
        # синтетический случай: один лот в кг среди лотов в тоннах
        con.execute("DELETE FROM exchange_quotes")
        seed_quotes(con, [(1, 10, "тонна", 12_000_000), (2, 15, "тонна", 12_100_000), (3, 20, "тонна", 11_950_000),
                          (4, 25, "тонна", 12_050_000), (5, 500_000, "килограмм", 12_020_000)])
        v = V.exchange_value(con, groups=["diesel"], quantity=120, unit="т", vs_=vs_)
        vw = (10 * 12_000_000 + 15 * 12_100_000 + 20 * 11_950_000 + 25 * 12_050_000 + 500 * 12_020_000) / 570
        ok("лот 500 000 кг = 500 т: объём 570 т, VWAP %s" % V._fmt(vw, 2),
           abs(v["volume"] - 570) < 1e-9 and abs(v["vwap"] - vw) < 0.01 and v["price_basis"] == "vwap", v)
        ok("V = 120 · VWAP внутри [Q1; Q3]", v["V"] == round(120 * vw) and v["range"]["low"] <= v["V"]
           <= v["range"]["high"], v)
        # крупный лот у края выборки: VWAP вне [Q1; Q3] — берётся медиана, V всё равно внутри диапазона
        con.execute("DELETE FROM exchange_quotes")
        seed_quotes(con, [(1, 5_000, "тонна", 11_900_000), (1, 1, "тонна", 11_950_000), (2, 1, "тонна", 12_000_000),
                          (2, 1, "тонна", 12_050_000), (3, 1, "тонна", 12_100_000), (3, 1, "тонна", 12_150_000)])
        v = V.exchange_value(con, groups=["diesel"], quantity=10, unit="т", vs_=vs_)
        ok("VWAP вне [Q1; Q3] — медиана с предупреждением, V внутри диапазона",
           v["price_basis"] == "median" and v["range"]["low"] <= v["V"] <= v["range"]["high"]
           and any("медиана" in w for w in v["warnings"]), v)
        con.execute("DELETE FROM exchange_quotes")


# ------------------------------------------------------------------ 6. Затратный метод: магазин 450 м²

SHOP_MUST = {"product_code": "0807", "sum_insured": 3_000_000_000, "object_value": 3_000_000_000,
             "region_code": "tashkent_city", "region": "город Ташкент"}
SHOP_OPT = {"object_kind": "shop", "class_fields": {"area_m2": 450}, "material": "brick", "year": 2016}


def check_cost():
    print("6. Затратный метод: магазин 450 м², справочник build_cost_m2")
    with db.tx() as con:                 # копия рабочей базы может уже нести учебную строку — начинаем с пустого
        V.ensure_build_cost(con)
        con.execute("DELETE FROM build_cost_m2")
    st, a = call("POST", "/act/make", {"lang": "ru", "must": SHOP_MUST, "optional": SHOP_OPT})
    v = (a or {}).get("valuation") or {}
    ok("без строки build_cost_m2 — «нет цены 1 м² для региона»",
       st == 200 and v.get("available") is False and "нет цены 1 м² для региона" in v.get("reason", "")
       and a["value_sv"] is None, (st, v))
    st, r = call("PUT", "/reference/build-cost", {"region": "tashkent_city", "building_type": "shop",
                                                   "price_m2": 6_500_000, "date": "2026-10-13", "source": "тест"})
    ok("гость не правит цену 1 м²", st in (401, 403), st)
    HEADERS.append(admin_header())
    with db.tx() as con:
        seeded = V.build_cost_seed(con)
    ok("учебная строка Ташкент / магазин загружена", seeded["created"], seeded)
    st, r = call("PUT", "/reference/build-cost", {"region": "tashkent_city", "building_type": "shop",
                                                   "price_m2": 7_000_000, "date": "2026-10-13",
                                                   "source": "учебная строка (тест правки)"})
    ok("администратор правит цену (PUT)", st == 200 and r["item"]["price_m2"] == 7_000_000, (st, r))
    st, g = call("GET", "/reference/build-cost", params={"region": "город Ташкент", "building_type": "магазин"})
    ok("GET по названию региона и вида", st == 200 and g["count"] == 1 and g["items"][0]["price_m2"] == 7_000_000, g)
    HEADERS.clear()
    st, a = call("POST", "/act/make", {"lang": "ru", "must": SHOP_MUST, "optional": SHOP_OPT})
    v = a["valuation"]
    T = date.today().year - 2016
    v_hand = round(450 * 7_000_000 * 1.00 * (1 - min(0.7, T / 80) * 1.0))
    ok("V = A · C · K_констр · (1 − min(0,7; T/T_сл)) = %s" % v_hand, v.get("method") == "cost" and v["V"] == v_hand,
       v)
    ok("формула на экране и источник", v["formula"].startswith("V = A · C · K_constr") and "450" in v["formula"]
       and v["source"]["table"] == "build_cost_m2" and v["n"] == 1, v)
    sv = a["value_sv"]
    ok("S/V к затратной оценке", sv and sv["ratio_pct"] == round(3_000_000_000 / v_hand * 100, 2), sv)
    st, a = call("POST", "/act/make", {"lang": "ru", "must": SHOP_MUST,
                                       "optional": dict(SHOP_OPT, material="reinforced_concrete")})
    ok("ж/б: K_констр 1,15, срок службы 100 лет",
       a["valuation"]["V"] == round(450 * 7_000_000 * 1.15 * (1 - min(0.7, T / 100))), a["valuation"])
    st, a = call("POST", "/act/make", {"lang": "ru", "must": dict(SHOP_MUST, region_code="samarkand",
                                                                  region="Самаркандская область"),
                                       "optional": SHOP_OPT})
    ok("Самарканд без строки — «нет цены 1 м² для региона»", a["valuation"]["code"] == "no_price_m2", a["valuation"])
    st, e = call("POST", "/act/make", {"lang": "ru", "must": SHOP_MUST, "optional": dict(SHOP_OPT, material="glass")})
    ok("неизвестный материал — 422", st == 422 and "material" in (e.get("errors") or {}), (st, e))


# ------------------------------------------------------------------ 7. Настройки

def check_settings():
    print("7. Константы — act_settings.valuation с версией и датой")
    d = ae.DEFAULT_SETTINGS["valuation"]
    ok("версия и дата", d["version"] == "1.0" and d["date"] == "2026-10-13" and d["calibrated"] == 0)
    ok("числа раздела 3", d["vehicle"]["bargain"] == 0.95 and d["vehicle"]["k_age"] == 0.88 and "k_market" not in d["vehicle"]
       and d["vehicle"]["mileage_norm_km"] == 20000 and d["vehicle"]["engine_hours_norm"] == 1500
       and d["vehicle"]["k_condition"]["worn"] == 0.95 and d["vehicle"]["k_condition"]["damaged"] == 0.80
       and d["depreciation"] == {"first_year_pct": 20, "next_years_pct": 12, "floor_pct": 15}
       and d["sv_band"] == [90, 110] and d["building"]["k_construction"]["adobe"] == 0.55
       and d["building"]["service_life"]["equipment"] == 12)
    ok("проверка настроек: всё верно", ae.check_settings({}) == [])
    errs = ae.check_settings({"valuation": {"vehicle": {"bargain": 3}, "sv_band": [120, 90]}})
    ok("ошибки ловятся", any("bargain" in e for e in errs) and any("sv_band" in e for e in errs), errs)

# ------------------------------------------------------------------ 8. Термины S/V (ТЗ 14.10.2026, К1)

def check_sv_terms():
    print("8. К1: «страхование сверх страховой стоимости» и «неполное страхование» (ГК ст. 938 и 936)")
    vs_ = V.val_settings(None, {})
    over = V.sv_check(150_000_000, 111_000_000, vs_)
    ok("S 150 млн, V 111 млн — ярлык «страхование сверх страховой стоимости», текст с «ГК ст. 938»",
       over["verdict"] == "over" and over["label"] == "страхование сверх страховой стоимости"
       and "ГК ст. 938" in over["text"]["ru"] and "страхование сверх страховой стоимости" in over["text"]["ru"]
       and "перестрах" not in over["text"]["ru"], over)
    under = V.sv_check(80_000_000, 111_000_000, vs_)
    ok("S 80 млн, V 111 млн — ярлык «неполное страхование», «ст. 936»",
       under["verdict"] == "under" and under["label"] == "неполное страхование" and "ст. 936" in under["text"]["ru"],
       under)
    w = V.sv_warning(150_000_000, 111_000_000, vs_)
    ok("предупреждение оценки — без слова «перестрахование»",
       "страхование сверх страховой стоимости" in w["text"] and "перестрах" not in w["text"], w)
    ok("uz/en не тронуты", "qiymatdan ortiq" in over["text"]["uz"] and "over-insurance" in over["text"]["en"], over)

# ------------------------------------------------------------------ 9. Поправка на возраст аналога (ТЗ 14.10.2026, К2)

FIXTURE_JSON = ROOT / "tests" / "fixtures" / "avtoelon_cobalt_2022.json"


def check_k_age():
    print("9. К2: V = median(P · 0,95 · k_age^Δ · K_пробег · K_сост), Δ = год аналога − год объекта")
    vs_ = V.val_settings(None, {})
    snap = _json.loads(FIXTURE_JSON.read_text(encoding="utf-8"))
    an = snap["analogs"]
    ok("снимок: 20 разобранных аналогов Cobalt 2022, все с годом и ценой в сумах",
       len(an) == 20 and all(a["year"] == 2022 and a["price_uzs"] for a in an), len(an))
    r = V.analog_value(an, as_of=TODAY, year=2022, sum_insured=150_000_000, vs_=vs_)
    ok("Cobalt 2022 офлайн: 19 аналогов, V в 105–117 млн (≈ 108 млн = 95,4 / 0,88)",
       r["available"] and r["n"] == 19 and 105_000_000 <= r["V"] <= 117_000_000
       and abs(r["V"] - 108_434_815) <= 1, (r.get("n"), r.get("V")))
    sv = V.sv_check(150_000_000, r["V"], vs_)
    ok("S 150 млн к V ≈ 108 млн — 138 %%, страхование сверх страховой стоимости (ГК ст. 938)",
       sv["verdict"] == "over" and round(sv["ratio_pct"]) == 138 and "ГК ст. 938" in sv["text"]["ru"], sv)
    k = V.k_age_of(2020, 2022, 0.88)
    ok("аналог 2020 для объекта 2022: Δ = −2, k = 0,88⁻² = 1,2913",
       k["delta"] == -2 and round(k["k"], 4) == 1.2913 and abs(k["k"] - 1 / 0.88 ** 2) < 1e-12, k)
    ok("тот же год — k = 1; аналог новее на год — 0,88", V.k_age_of(2022, 2022, 0.88)["k"] == 1.0
       and V.k_age_of(2023, 2022, 0.88)["k"] == 0.88)
    ok("нет года у аналога — None (в оценку не идёт)", V.k_age_of(None, 2022, 0.88) is None)
    base = [dict(a) for a in an[:4]]
    old = dict(base[0], year=2020, url="https://avtoelon.uz/a/show/test-2020")
    noyear = dict(base[1], year=None, url="https://avtoelon.uz/a/show/test-noyear")
    r2 = V.analog_value(base + [old, noyear], as_of=TODAY, year=2022, vs_=vs_)
    a20 = next(a for a in r2["analogs"] if a["year"] == 2020)
    ok("аналог 2020 в расчёте: цена × 1,2913, Δ = −2 в act_json.valuation.k и в пояснении",
       a20["delta"] == -2 and a20["k_age"] == 1.2913
       and any(x["delta"] == -2 and x["k_age"] == 1.2913 for x in r2["k"]["age"])
       and any("Δ = -2" in st and "0,88^-2 = 1,2913" in st for st in r2["steps"]), (a20, r2["steps"]))
    adj = sorted([float(a["price_uzs"]) for a in base] + [float(old["price_uzs"]) / 0.88 ** 2])
    ok("V = median(P_i · 0,95 · 0,88^Δ_i) — руками", r2["V"] == round(hand_q([p * 0.95 for p in adj], 0.5)),
       (r2["V"], adj))
    ok("аналог без года — в cut с причиной", any(c["code"] == "no_year" and "нет года" in c["reason"]
                                                   and c["url"].endswith("noyear") for c in r2["cut"]), r2["cut"])
    # старые настройки с k_market: ключ игнорируется, проверка не падает
    old_s = {"valuation": {"vehicle": {"k_market": 0.9, "bargain": 0.95}}}
    ok("старый ключ vehicle.k_market игнорируется: проверка без ошибок, k_age = 0,88",
       ae.check_settings(old_s) == [] and "k_market" not in V.val_settings(None, old_s)["vehicle"]
       and V.val_settings(None, old_s)["vehicle"]["k_age"] == 0.88
       and old_s["valuation"]["vehicle"]["k_market"] == 0.9, ae.check_settings(old_s))
    errs = ae.check_settings({"valuation": {"vehicle": {"k_age": 1.1}}})
    ok("k_age вне 0,5–1,0 — ошибка", any("k_age" in e and "1.0" in e for e in errs)
       and ae.check_settings({"valuation": {"vehicle": {"k_age": 0.5}}}) == [], errs)
    # поиск: год объекта → ± 1 → ± 2, адреса только ?year[from]= и &page=
    calls, saved = [], vs.fetch_avtoelon_analogs

    def fake(brand, model, year, as_of, rate, **kw):
        calls.append((kw.get("year_from"), kw.get("year_tolerance")))
        url = vs.analog_url(brand, model, kw.get("year_from"), 1)
        rows = [dict(a) for a in an[:2]]                          # 2 аналога года объекта — мало
        if kw.get("year_tolerance") >= 1:
            rows += [dict(a, year=2021, price_uzs=round(a["price_uzs"] * 0.88), url=a["url"] + "-2021")
                     for a in an[:2]]          # год назад, цена ниже на 12 %: после k_age — как у 2022
        return {"status": vs.STATUS_OK, "analogs": rows, "urls": [url], "reason": ""}
    vs.fetch_avtoelon_analogs = fake
    try:
        with db.tx() as con:
            r3 = V.vehicle_valuation(con, brand="Chevrolet", model="Cobalt", year=2022, as_of=TODAY, fx=dict(FX))
    finally:
        vs.fetch_avtoelon_analogs = saved
    ok("меньше 3 аналогов года объекта — поиск год ± 1 (?year[from]=2021), с k_age",
       calls == [(2022, 0), (2021, 1)] and r3["available"] and r3["n"] == 4
       and [s_["tolerance"] for s_ in r3["search"]] == [0, 1]
       and r3["search"][1]["url"] == "https://avtoelon.uz/avto/chevrolet/cobalt/?year%5Bfrom%5D=2021"
       and sorted(x["delta"] for x in r3["k"]["age"]) == [-1, -1, 0, 0]
       and "Поиск аналогов" in r3["steps"][0], (calls, r3.get("search"), r3.get("reason")))
    calls.clear()
    vs.fetch_avtoelon_analogs = lambda *a, **kw: (calls.append((kw.get("year_from"), kw.get("year_tolerance")))
                                                  or {"status": vs.STATUS_OK, "analogs": [dict(an[0])],
                                                      "urls": [vs.analog_url("Chevrolet", "Cobalt",
                                                                             kw.get("year_from"), 1)]})
    try:
        with db.tx() as con:
            r4 = V.vehicle_valuation(con, brand="Chevrolet", model="Cobalt", year=2022, as_of=TODAY, fx=dict(FX))
    finally:
        vs.fetch_avtoelon_analogs = saved
    ok("и при ± 1 мало — год ± 2 (?year[from]=2020); дальше не расширяем",
       calls == [(2022, 0), (2021, 1), (2020, 2)] and not r4["available"], (calls, r4.get("reason")))
    u = vs.analog_url("Chevrolet", "Cobalt", 2020, 2)
    ok("адрес ± 2: только year%5Bfrom%5D и page", u == "https://avtoelon.uz/avto/chevrolet/cobalt/"
       "?year%5Bfrom%5D=2020&page=2" and vs.query_ok(u), u)


def main():
    llm_post, http_get = llm._post, vs._http_get
    llm._post = no_network
    vs._http_get = no_network
    try:
        with temp_db("surveyor-valuation-tz.db"):
            db.ensure_schema()
            ensure_market_stats()
            ensure_stat_series()
            guest.reset()
            check_robots()
            res = check_cobalt()
            check_depreciation()
            check_sv(res)
            check_exchange()
            check_exchange_range()
            check_cost()
            check_settings()
            check_sv_terms()
            check_k_age()
    finally:
        llm._post, vs._http_get = llm_post, http_get
    print("\nитог: ок %d, плохо %d" % (passed, failed))
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
