"""
Быстрый режим аналитики риска (задача заказчика 22.09.2026: «интерфейс максимально лёгкий
для заполнения»). Проверяем app/risk_analytics.py + app/risk_api.py.

Запуск из корня:
    set PYTHONIOENCODING=utf-8
    sandbox\\.venv\\Scripts\\python.exe tests\\test_quick_mode.py

Всё идёт в копию рабочей базы (tests/tmpdb.py), живой сервер не трогаем.

Что проверяем:
  1. Четыре поля (класс/продукт, сумма, стоимость, регион) — анализ идёт, остальное подставлено,
     каждое допущение перечислено в assumptions, полнота и уверенность ниже полного ввода.
  2. Полный ввод даёт контрольные числа: эталон ТЗ 13.10.2026 (модель ставки) — склад 3 620 694; производство 8/9
     94 604 500; спецтехника 4 200 000; прежний путь (rate_model.enabled = false) — как раньше: 6 048 000;
     612 927 966; 17 496 000.
  3. Пресет (GET /analytics/risk/presets) + суммы и регион → анализ проходит.
  4. Последние значения (GET/PUT /analytics/risk/last) читает только свой пользователь.
"""
import asyncio
import json as _json
import os
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

os.environ.pop("SURVEYOR_DEV", None)

import rm_modes  # noqa: E402
from tmpdb import temp_db                      # noqa: E402
from app import auth, db                       # noqa: E402
from app import risk_analytics as ra           # noqa: E402
from app.main import app                       # noqa: E402

PREFIX = "тест-быстрый-"
A, B = PREFIX + "агент", PREFIX + "второй"
PEOPLE = {A: ("сотрудник", "Ташкентская область"), B: ("сотрудник", "Бухарский филиал")}
TOKENS = {}
AS_OF = date(2026, 9, 21)
NO_EXT = {"weights": {"external_stats": 0}}
passed, failed = 0, 0


def ok(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ок  ", name)
    else:
        failed += 1
        print("  ПЛОХО", name, extra)


def call(method, path, body=None, params=None, who=None):
    query = urlencode(params or {}, encoding="utf-8")
    payload = _json.dumps(body, ensure_ascii=False).encode() if body is not None else b""
    hdrs = [(b"host", b"test"), (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode())]
    if who:
        hdrs.append((b"cookie", f"sid={TOKENS[who]}".encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "",
             "query_string": query.encode(), "headers": hdrs,
             "client": ("203.0.113.9", 0), "server": ("test", 80)}
    out = {"status": None, "chunks": []}

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

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


def setup():
    db.ensure_schema()
    with db.tx() as con:
        ts = db.now()
        for login, (role, branch) in PEOPLE.items():
            cur = con.execute("INSERT INTO users (login, full_name, role, branch, password_hash, salt,"
                              " status, created_at) VALUES (?,?,?,?,?,?,?,?)",
                              (login, "Тест", role, branch, "x", "y", "активен", ts))
            u = db.rows(con, "SELECT * FROM users WHERE id=?", cur.lastrowid)[0]
            TOKENS[login], _ = auth.create_session(con, u, "203.0.113.9", "test")


# ---------- 1. четыре поля ----------

QUICK_WAREHOUSE = {"must": {"class_code": "9", "sum_insured": 4.2e9, "object_value": 4.2e9,
                            "region": "Ташкентская область"},
                   "mode": "quick"}


def check_quick():
    print("1. POST /analytics/risk, mode=quick — только четыре поля")
    st, r = call("POST", "/analytics/risk", QUICK_WAREHOUSE, who=A)
    ok("четыре поля → 200", st == 200, (st, str(r)[:300]))
    ok("режим в ответе", r.get("mode") == "quick", r.get("mode"))
    a = r.get("assumptions") or []
    keys = {x["key"] for x in a}
    ok("допущения перечислены", keys >= {"object_type", "construction", "activity", "term_months"}, keys)
    ok("у каждого допущения есть подпись, значение и причина",
       all({"key", "label", "value", "why"} <= set(x) and x.get("assumed") for x in a))
    ok("текст «взято по умолчанию — уточните»", all(x["note"] == ra.ASSUMED_NOTE for x in a))
    ok("конструкция — смешанная (консервативно)",
       next(x for x in a if x["key"] == "construction")["value"] == "mixed")
    ok("срок — 12 месяцев", next(x for x in a if x["key"] == "term_months")["value"] == 12)
    c = r["completeness"]
    ok("в полноте отмечены подстановки", c["must_assumed"] == len(a) and c["must_filled"] < c["must_total"],
       (c["must_assumed"], c["must_filled"], c["must_total"]))

    full = {"must": {"class_code": "9", "object_type": "Склад", "sum_insured": 4.2e9, "object_value": 4.2e9,
                     "region": "Ташкентская область", "term_months": 12, "construction": "reinforced",
                     "activity": "warehouse"},
            "optional": {"protection": "alarm_guard", "losses_3y": {"count": 0, "amount": 0}}}
    st2, rf = call("POST", "/analytics/risk", full, who=A)
    ok("полный ввод → 200", st2 == 200, st2)
    ok("полнота быстрого режима ниже полного", c["pct"] < rf["completeness"]["pct"],
       (c["pct"], rf["completeness"]["pct"]))
    order = ["низкая", "средняя", "высокая"]
    ok("уверенность не выше, чем при полном вводе",
       order.index(c["confidence"]) <= order.index(rf["completeness"]["confidence"]),
       (c["confidence"], rf["completeness"]["confidence"]))
    ok("при полном вводе допущений нет", not rf.get("assumptions"))

    st3, rp = call("POST", "/analytics/risk", {"must": {"product_code": "0308", "sum_insured": 1.2e9,
                                                        "object_value": 1.5e9, "region": "Навоийская область"},
                                               "mode": "quick"}, who=A)
    ok("только продукт (без класса) → 200 и класс подставлен", st3 == 200 and rp["summary"]["class_code"] == "3",
       (st3, str(rp)[:200]))
    ak = {x["key"] for x in rp["assumptions"]}
    ok("для транспорта подставлены класс, тип ТС и «год неизвестен»",
       {"class_code", "vehicle_type", "year"} <= ak, ak)

    st4, b4 = call("POST", "/analytics/risk", {"must": {"class_code": "9"}, "mode": "quick"}, who=A)
    ok("без сумм и региона → 422 с тремя полями", st4 == 422
       and {"sum_insured", "object_value", "region"} <= set(b4["missing"]), (st4, b4))


# ---------- 2. контрольные числа не изменились ----------

EX1 = ({"class_code": "9", "object_type": "Склад", "sum_insured": 4.2e9, "object_value": 4.2e9,
        "region": "Ташкентская область", "term_months": 12, "construction": "reinforced",
        "activity": "warehouse"},
       {"protection": "alarm_guard", "losses_3y": {"count": 0, "amount": 0}}, 6_048_000, 3_620_694)
EX2 = ({"class_code": "8/9", "object_type": "Производство", "sum_insured": 50e9, "object_value": 50e9,
        "region": "г. Ташкент", "term_months": 12, "construction": "reinforced", "activity": "food"},
       {"protection": "alarm_guard", "seismic_zone": "8–9", "wear_pct": 30,
        "losses_3y": {"count": 1, "amount": 300e6}, "deductible": {"pct": 0.5},
        "compartments": {"count": 4, "largest_value": 18e9},
        "bi": {"monthly_amount": 1e9, "recovery_months": 4}}, 612_927_966, 94_604_500)
EX3 = ({"class_code": "3", "product_code": "0318", "object_type": "Спецтехника — экскаватор",
        "sum_insured": 1.2e9, "object_value": 1.5e9, "region": "Навоийская область", "term_months": 12,
        "vehicle_type": "special", "year": 2021},
       {"protection": "tracker", "losses_3y": {"count": 2, "amount": 150e6}}, 17_496_000, 4_200_000)


def check_numbers():
    print("2. контрольные примеры при полном вводе: эталон ТЗ 13.10.2026 и прежний путь")
    for mode in ("model", "legacy"):
        rm_modes.set_mode(mode)
        with db.tx() as con:
            for name, (must, optional, old, new) in (("склад", EX1), ("производство 8/9", EX2), ("экскаватор", EX3)):
                want = new if mode == "model" else old
                r = ra.analyze(con, must, optional, thresholds=NO_EXT, market={"rate_pct": 0.18}, as_of=AS_OF)
                ok(f"{name} ({mode}): премия {want:,}".replace(",", " "), abs(r["summary"]["premium"] - want) <= 1,
                   r["summary"]["premium"])
                ok(f"{name} ({mode}): допущений нет", r["assumptions"] == [])
    rm_modes.set_mode("model")


# ---------- 3. пресеты ----------

def check_presets():
    print("3. GET /analytics/risk/presets")
    st, b = call("GET", "/analytics/risk/presets", who=A)
    ok("200 и 9 пресетов", st == 200 and len(b["presets"]) >= 8, (st, len(b.get("presets") or [])))
    keys = [p["key"] for p in b["presets"]]
    ok("склад, магазин, офис, производство, жильё, легковой, грузовик, экскаватор, товар",
       set(ra.PRESET_KEYS) <= set(keys), keys)
    ok("ключи i18n есть", all(p["i18n_key"].startswith("preset.") for p in b["presets"])
       and "preset.hint" in b["i18n_keys"])
    p = next(p for p in b["presets"] if p["key"] == "excavator")
    body = {"must": {**p["must"], "sum_insured": 1.2e9, "object_value": 1.5e9,
                     "region": "Навоийская область"}, "mode": "quick"}
    st, r = call("POST", "/analytics/risk", body, who=A)
    ok("пресет + суммы и регион → анализ", st == 200 and r["summary"]["premium"] > 0,
       (st, str(r)[:200]))
    ok("у пресета транспорта осталось одно допущение — год", {x["key"] for x in r["assumptions"]} == {"year"},
       [x["key"] for x in r["assumptions"]])


# ---------- 4. последние значения ----------

def check_last():
    print("4. GET/PUT /analytics/risk/last")
    st, b = call("GET", "/analytics/risk/last", who=A)
    ok("пусто в начале", st == 200 and b["values"] == {}, (st, b))
    st, b = call("PUT", "/analytics/risk/last",
                 {"class_code": "9", "object_type": "Склад", "region": "Ташкентская область",
                  "sum_insured": 4.2e9, "full_name": "Иванов И. И."}, who=A)
    ok("сохранено", st == 200 and b["values"]["object_type"] == "Склад", (st, b))
    ok("страховая сумма и персональные данные не сохраняются",
       "sum_insured" not in b["values"] and "full_name" not in b["values"], b["values"])
    st, b = call("GET", "/analytics/risk/last", who=A)
    ok("читается своим пользователем", st == 200 and b["values"]["region"] == "Ташкентская область", b)
    st, b2 = call("GET", "/analytics/risk/last", who=B)
    ok("чужой пользователь своего значения не видит", st == 200 and b2["values"] == {}, b2)
    st, b = call("GET", "/analytics/risk/last")
    # 22.09.2026: гостю ничего не запоминаем — пустой ответ вместо отказа
    ok("без входа → 200, пусто", st == 200 and b.get("guest") is True, (st, b))


# ---------- 5. поля формы в быстром режиме ----------

def check_fields():
    print("5. GET /analytics/risk/fields?mode=quick")
    st, b = call("GET", "/analytics/risk/fields", params={"class_code": "8/9", "mode": "quick"}, who=A)
    keys = [f["key"] for f in b["must"]]
    ok("в must только четыре поля", st == 200 and set(keys) <= set(ra.QUICK_MUST) and len(keys) == 4,
       (st, keys))
    ok("что подставим — с причинами",
       b["will_assume"] and all({"key", "label", "value", "why"} <= set(x) for x in b["will_assume"]))
    ok("полный список полей остался в must_full", len(b["must_full"]) > len(b["must"]))
    ok("скрытые поля перечислены", "construction" in b["hidden_keys"] and "activity" in b["hidden_keys"])
    st, b = call("GET", "/analytics/risk/fields", params={"class_code": "8/9"}, who=A)
    ok("mode=full — как раньше (все обязательные поля)",
       st == 200 and "construction" in [f["key"] for f in b["must"]] and "will_assume" not in b)
    st, b = call("GET", "/analytics/risk/fields", params={"product_code": "0308", "mode": "quick"}, who=A)
    ok("по продукту 0308 подставляется класс 3",
       any(x["key"] == "class_code" and x["value"] == "3" for x in b["will_assume"]), b["will_assume"])


# ---------- 6. таблица product→defaults для всех 176 продуктов ----------

def check_product_table():
    print("6. умные подстановки по продукту (все продукты)")
    with db.tx() as con:
        ref = db.load_reference(con)
        codes = list(ref.products)
        bad = [c for c in codes if not ra.product_defaults(ref, c)["object_type"]]
        ok(f"у всех {len(codes)} продуктов есть тип объекта", not bad, bad[:5])
        guessed = [c for c in codes if ra.product_defaults(ref, c)["object_type_guessed"]]
        ok("угаданные помечены (object_type_guessed)", len(guessed) > 0)
        d = ra.product_defaults(ref, "0807")
        ok("0807 → класс 8 и деятельность заполнены", d["class_code"] and d["activity"], d)
    ok("тип объекта → конструкция: склад смешанная, офис железобетон",
       ra.CONSTRUCTION_BY_OBJECT_TYPE["Склад"] == "mixed" and ra.CONSTRUCTION_BY_OBJECT_TYPE["Офис"] == "reinforced")
    ok("филиал → регион", ra.region_from_branch("Бухарский филиал") == "Бухарская область",
       ra.region_from_branch("Бухарский филиал"))


def main():
    with temp_db("surveyor-quick-test.db"):
        setup()
        check_quick()
        check_numbers()
        check_presets()
        check_last()
        check_fields()
        check_product_table()
    print(f"\nитог: успешно {passed}, ошибок {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
