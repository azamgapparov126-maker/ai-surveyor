"""
Вход по сети аналитики риска и калькулятора ОСГОР (задача 144, этап 2): app/risk_api.py, app/osgor.py.

Запуск из корня (свой ASGI-клиент, живой сервер не трогаем):
    set PYTHONIOENCODING=utf-8
    sandbox\\.venv\\Scripts\\python.exe tests\\test_risk_api.py

Вся работа — во временной копии базы (tests/tmpdb.py).

Контрольные числа:
  склад, класс 9, 4,2 млрд, кирпич, сигнализация и охрана, убытков не было (как пример 1 актуария,
  tests/test_risk_analytics.py), эталон ТЗ 13.10.2026 (модель ставки): r_рын НАПП 0,022356 % × 1,10 / 0,271 ×
  K_кл 0,95 = 0,0862 %, премия 3 620 694 сум; прежний путь (rate_model.enabled = false): нетто 0,10 × 0,9 = 0,09%;
  техническая 0,09 × 1,12 / 0,7 = 0,144%; премия 0,144% × 4 200 000 000 = 6 048 000 сум.
  ОСГОР: ФОТ 4 800 000 000, ОКЭД 01000 (КСТ 4,000), год: 4 800 000 000 × 0,1 × 4 / 100 = 19 200 000 сум.
  Минимум: ФОТ 30 000 000, ОКЭД 41100 (КСТ 0,571) = 17 130 < 0,25 × 412 000 = 103 000 → 103 000 сум.
"""
import asyncio
import json as _json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.pop("SURVEYOR_DEV", None)     # guard проверяем целиком

from tmpdb import temp_db            # noqa: E402
import rm_modes                      # noqa: E402
from app import auth, db             # noqa: E402
from app.main import app             # noqa: E402

PREFIX = "тест-аналитика-"
EMP, ADM = PREFIX + "сотрудник", PREFIX + "админ"
PEOPLE = {EMP: "сотрудник", ADM: "админ"}
TOKENS = {}
passed, failed = 0, 0

WAREHOUSE = {"must": {"class_code": "9", "object_type": "Склад", "sum_insured": 4.2e9, "object_value": 4.2e9,
                      "region": "Ташкентская область", "term_months": 12, "construction": "reinforced",
                      "activity": "warehouse"},
             "optional": {"protection": "alarm_guard", "losses_3y": {"count": 0, "amount": 0}}}
PRODUCTION = {"must": {"class_code": "8/9", "object_type": "Производство", "sum_insured": 50e9,
                       "object_value": 50e9, "region": "г. Ташкент", "term_months": 12,
                       "construction": "reinforced", "activity": "food"},
              "optional": {"protection": "alarm_guard", "seismic_zone": "8–9", "wear_pct": 30,
                           "losses_3y": {"count": 1, "amount": 300e6}, "deductible": {"pct": 0.5},
                           "compartments": {"count": 4, "largest_value": 18e9},
                           "bi": {"monthly_amount": 1e9, "recovery_months": 4}}}


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
        for login, role in PEOPLE.items():
            cur = con.execute("INSERT INTO users (login, full_name, role, branch, password_hash, salt,"
                              " status, created_at) VALUES (?,?,?,?,?,?,?,?)",
                              (login, "Тест", role, "тест-аналитика", "x", "y", "активен", ts))
            u = db.rows(con, "SELECT * FROM users WHERE id=?", cur.lastrowid)[0]
            TOKENS[login], _ = auth.create_session(con, u, "203.0.113.9", "test")
        con.execute("DELETE FROM osgor_brv")      # БРВ задаёт сам тест


def last_audit(action_like: str) -> dict:
    with db.tx() as con:
        r = db.rows(con, "SELECT who, action, entity FROM audit WHERE action LIKE ? ORDER BY id DESC LIMIT 1",
                    action_like)
    return r[0] if r else {}


# ---------- 1. поля ----------

def check_fields():
    print("1. GET /analytics/risk/fields")
    st, b = call("GET", "/analytics/risk/fields", params={"class_code": "8/9"}, who=EMP)
    ok("8/9 → 200", st == 200, (st, b))
    keys = [f["key"] for f in b["must"]]
    ok("8/9: конструкция и деятельность обязательны, транспорта нет",
       "construction" in keys and "activity" in keys and "vehicle_type" not in keys, keys)
    okeys = [f["key"] for f in b["optional"]]
    ok("8/9: сейсмозона и отсеки в дополнительных", "seismic_zone" in okeys and "compartments" in okeys, okeys)
    ok("регионы есть", len(b["regions"]) >= 1 and all("key" in r and "name" in r for r in b["regions"]))
    ok("классы: 8, 9, 3 в списке", {"8", "9", "3"} <= {c["code"] for c in b["classes"]})
    ok("продукты по классам 8/9 (есть 0807)", any(p["code"] == "0807" for p in b["products"]))
    ok("типы объектов: Склад", "Склад" in b["object_types"], b["object_types"][:10])

    st, b = call("GET", "/analytics/risk/fields", params={"class_code": "3"}, who=EMP)
    keys = [f["key"] for f in b["must"]]
    ok("3: тип транспорта и год обязательны, конструкции нет",
       st == 200 and "vehicle_type" in keys and "year" in keys and "construction" not in keys, keys)
    prot = next(f for f in b["optional"] if f["key"] == "protection")
    ok("3: защита — противоугонные варианты", any(o["code"] == "tracker" for o in prot["options"]))
    ok("3: продукты только с классом 3", b["products"] and all("3" in p["classes"] for p in b["products"]))


# ---------- 2. анализ ----------

def check_analyze():
    print("2. POST /analytics/risk")
    with db.tx() as con:
        n_req = con.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        n_calc = con.execute("SELECT COUNT(*) FROM calculations").fetchone()[0]
    t0 = time.perf_counter()
    st, r = call("POST", "/analytics/risk", WAREHOUSE, who=EMP)
    cold = (time.perf_counter() - t0) * 1000
    ok("склад → 200", st == 200, (st, str(r)[:300]))
    s = r["summary"]
    # эталон ТЗ 13.10.2026; было 6 048 000 и 0,144 % — проверяется ниже прежним путём (переключатель)
    ok("премия 3 620 694 (модель ставки)", abs(s["premium"] - 3_620_694) <= 1, s["premium"])
    ok("ставка применённая 0,0862%", abs(s["rate_applied_pct"] - 0.0862) < 1e-6, s["rate_applied_pct"])
    for k in ("completeness", "risks", "top_drivers", "reducers", "scenarios", "retention", "level",
              "market", "market_compare", "method"):
        ok(f"в ответе есть {k}", k in r)
    ok("сценарии PML/EML/MFL", set(r["scenarios"]) == {"EML", "PML", "MFL"})
    # названия как в акте (13.10.2026): PML — защита сработала штатно (было «EML 336 000 000»)
    ok("PML 336 000 000 (как у актуария)", r["scenarios"]["PML"]["amount"] == 336_000_000,
       r["scenarios"]["PML"]["amount"])
    m = r["market"]
    ok("market — картина рынка: market/region/sources/notes",
       all(k in m for k in ("market", "region", "sources", "notes")), list(m))
    ok("market.market.our_rate_pct = применённая ставка", m["market"]["our_rate_pct"] == s["rate_applied_pct"])
    ok("регион опознан", m["region"]["key"] not in (None, "total"), m["region"])

    calc_body = {"product_code": "0808", "class_code": "9", "object_type": "Склад", "value_amount": 4.2e9,
                 "sum_insured": 4.2e9, "term_days": 365,
                 "factors": {"construction": "reinforced", "activity": "warehouse", "protection": "alarm_guard",
                             "loss_history": "clean"}}
    st, c = call("POST", "/calculate", calc_body, who=EMP)
    ok("summary.premium = премия /calculate", st == 200 and abs(c["premium"] - s["premium"]) <= 1,
       (st, c.get("premium") if isinstance(c, dict) else c))

    rm_modes.set_mode("legacy")
    st, rl = call("POST", "/analytics/risk", WAREHOUSE, who=EMP)
    ok("прежний путь (rate_model.enabled = false): премия 6 048 000, ставка 0,144%",
       abs(rl["summary"]["premium"] - 6_048_000) <= 1 and abs(rl["summary"]["rate_applied_pct"] - 0.144) < 1e-6,
       rl["summary"]["premium"])
    rm_modes.set_mode("model")
    t0 = time.perf_counter()
    st, r2 = call("POST", "/analytics/risk", WAREHOUSE, who=EMP)
    warm = (time.perf_counter() - t0) * 1000
    print(f"       время ответа: первый {cold:.0f} мс, повторный {warm:.0f} мс")
    ok("повторный ответ быстрее 300 мс", warm < 300, f"{warm:.0f} мс")

    st, r3 = call("POST", "/analytics/risk", PRODUCTION, who=EMP)
    ok("8/9 производство → 200, две части", st == 200 and len(r3["summary"]["parts"]) == 2, (st, str(r3)[:200]))
    ok("8/9: пояснение, что рынок по классу 8", any("рынок показан по классу 8" in n for n in r3["market"]["notes"]))

    with db.tx() as con:
        ok("ничего не сохранено в запросы и расчёты",
           con.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == n_req
           and con.execute("SELECT COUNT(*) FROM calculations").fetchone()[0] == n_calc)

    st, b = call("POST", "/analytics/risk", {"must": {"class_code": "8"}, "optional": {}}, who=EMP)
    ok("без обязательных → 422", st == 422, st)
    ok("422: missing с конструкцией и суммой", "construction" in b["missing"] and "sum_insured" in b["missing"],
       b)
    ok("422: подписи по-русски", b["labels"]["sum_insured"] == "Страховая сумма" and b["missing_text"])
    st, b = call("POST", "/analytics/risk",
                 {"must": {**WAREHOUSE["must"], "sum_insured": -1}, "optional": {"wear_pct": 150}}, who=EMP)
    ok("ошибки формата → 422 с errors", st == 422 and {"sum_insured", "wear_pct"} <= set(b["errors"]), b)
    st, b = call("POST", "/analytics/risk", {"must": {**WAREHOUSE["must"], "class_code": "99"}}, who=EMP)
    ok("нет такого класса → 422 errors.class_code", st == 422 and "class_code" in b["errors"], b)
    st, b = call("POST", "/analytics/risk", WAREHOUSE)
    # 22.09.2026: анализ риска открыт гостю (лимит обращений — app/guest.py)
    ok("без входа → 200 (гость)", st == 200, st)


# ---------- 3. пороги ----------

def check_thresholds():
    print("3. Пороги уровня риска")
    st, b = call("GET", "/analytics/risk/thresholds", who=EMP)
    ok("сотрудник GET → 200, can_edit = false", st == 200 and b["can_edit"] is False, (st, b))
    ok("действующие границы — по умолчанию", b["thresholds"]["level_bounds"] == [20, 40, 60, 80])
    st, b = call("PUT", "/analytics/risk/thresholds", {"level_bounds": [1, 2, 3, 4]}, who=EMP)
    ok("сотрудник PUT → 403", st == 403, (st, b))
    st, b = call("PUT", "/analytics/risk/thresholds", {"level_bounds": [5, 1, 3, 4]}, who=ADM)
    ok("админ PUT с ошибкой → 422", st == 422, (st, b))
    st, b = call("PUT", "/analytics/risk/thresholds", {"level_bounds": [1, 2, 3, 4]}, who=ADM)
    ok("админ PUT → 200", st == 200 and b["thresholds"]["level_bounds"] == [1, 2, 3, 4], (st, b))
    ok("в истории логин админа", b["thresholds"]["_source"]["created_by"] == ADM, b["thresholds"]["_source"])
    ok("журнал: логин из сессии", last_audit("изменены пороги%").get("who") == ADM)
    st, b = call("PUT", "/analytics/risk/thresholds", {"stop_min_level": "Повышенный"}, who=ADM)
    ok("вторая правка не сбрасывает первую", st == 200 and b["thresholds"]["level_bounds"] == [1, 2, 3, 4]
       and b["thresholds"]["stop_min_level"] == "Повышенный", b)
    st, r = call("POST", "/analytics/risk", PRODUCTION, who=EMP)
    ok("анализ применяет новые пороги", st == 200 and r["level"]["level"] == "Критический"
       and r["thresholds_source"]["created_by"] == ADM, (r["level"].get("score"), r["level"].get("level")))
    st, b = call("GET", "/analytics/risk/thresholds", who=ADM)
    ok("админ GET: can_edit, история из 2 правок", b["can_edit"] is True and len(b["history"]) == 2)
    st, b = call("PUT", "/analytics/risk/thresholds", {"reset": True}, who=ADM)
    ok("reset → значения по умолчанию", st == 200 and b["thresholds"]["level_bounds"] == [20, 40, 60, 80], b)


# ---------- 4–6. ОСГОР ----------

def check_osgor():
    print("4. GET /osgor/activities")
    for q in ("01.11", "0111"):
        st, b = call("GET", "/osgor/activities", params={"q": q}, who=EMP)
        ok(f"«{q}» → варианты на 0111", st == 200 and b["items"]
           and all(i["okved"].startswith("0111") for i in b["items"]), b.get("items", [])[:2])
    it = b["items"][0]
    ok("вариант: код, название, категория, КСТ", {"okved", "name", "category", "kst"} <= set(it), it)
    st, b = call("GET", "/osgor/activities", params={"q": "строит"}, who=EMP)
    ok("«строит» → варианты", st == 200 and 0 < len(b["items"]) <= 20, b.get("found"))
    ok("«строит» — слово начинается на «строит»",
       all(any(w.startswith("строит") for w in i["name"].lower().split()) for i in b["items"]))
    st, b2 = call("GET", "/osgor/activities", params={"q": "СТРОИТ"}, who=EMP)
    ok("регистр не важен", [i["okved"] for i in b2["items"]] == [i["okved"] for i in b["items"]])
    st, b3 = call("GET", "/osgor/activities", params={"q": "stroit"}, who=EMP)
    ok("латиница «stroit» находит то же", [i["okved"] for i in b3["items"]] == [i["okved"] for i in b["items"]])
    st, b = call("GET", "/osgor/activities", params={"q": "выращивание зерн"}, who=EMP)
    ok("два слова — оба по началу", b["items"] and all("зерн" in i["name"].lower() for i in b["items"]), b["items"][:2])
    st, b = call("GET", "/osgor/activities", who=EMP)
    ok("пустой запрос → 20 вариантов и размер справочника", len(b["items"]) == 20 and b["total"] == 934,
       (len(b["items"]), b.get("total")))
    st, b = call("GET", "/osgor/activities", params={"q": "ыыыщщщ"}, who=EMP)
    ok("ничего не найдено → пусто, не ошибка", st == 200 and b["items"] == [])

    print("5. POST /osgor/quick")
    st, b = call("POST", "/osgor/quick", {"okved": "01000", "payroll": 4_800_000_000, "term_days": 365}, who=EMP)
    ok("ФОТ 4,8 млрд, КСТ 4,0 → 19 200 000", st == 200 and b["premium"] == 19_200_000 and b["kst"] == 4.0,
       (st, b.get("premium") if isinstance(b, dict) else b))
    ok("БРВ не введён → note про 0,25 БРВ", b["brv"] is None and "0,25 БРВ" in b["note"], b.get("note"))
    ok("строки расчёта со ссылкой на норму", b["lines"] and all(l["legal_ref"] for l in b["lines"]))
    st, b = call("POST", "/osgor/quick", {"okved": "41100", "payroll": 30_000_000, "brv": 412_000}, who=EMP)
    ok("минимум 0,25 БРВ применён → 103 000", st == 200 and b["premium"] == 103_000 and b["min_applied"],
       b.get("premium"))
    ok("БРВ из запроса — note пуст", b["note"] == "" and b["brv_source"] == "передан в запросе")
    for bad in ({"okved": "", "payroll": 1e9}, {"okved": "01000", "payroll": -5},
                {"okved": "01000", "payroll": 1e9, "term_days": 400},
                {"okved": "01000", "payroll": 1e9, "contract_date": "21.09.2026"},
                {"okved": "01000", "payroll": 1e9, "brv": -1}):
        st, b = call("POST", "/osgor/quick", bad, who=EMP)
        ok(f"неверный ввод {bad} → 422", st == 422, (st, b))

    # БРВ из справочника администратора — по дате договора
    st, b = call("PUT", "/osgor/brv", {"value": 412_000, "effective_from": "2026-01-01", "source": "тестовый акт"},
                 who=ADM)
    ok("админ ввёл БРВ", st == 200, (st, b))
    st, b = call("POST", "/osgor/quick", {"okved": "41100", "payroll": 30_000_000}, who=EMP)
    ok("без brv — берётся действующий (сегодня) → 103 000", b["brv"] == 412_000 and b["premium"] == 103_000
       and "справочник администратора" in (b["brv_source"] or ""), (b.get("brv"), b.get("premium")))
    st, b = call("POST", "/osgor/quick", {"okved": "41100", "payroll": 30_000_000, "contract_date": "2025-06-01"},
                 who=EMP)
    ok("договор до вступления БРВ в силу → БРВ нет, note", b["brv"] is None and b["note"], b.get("brv"))

    print("6. POST /osgor/assess")
    emp = {"name": "ООО Тест", "okved": "01000", "headcount": 50, "payroll_12m": 4_800_000_000}
    st, b = call("POST", "/osgor/assess", emp, who=EMP)
    ok("БРВ подставлен по дате договора", st == 200 and b["brv"] == 412_000 and b["premium"]["min_premium"] == 103_000,
       (st, b.get("brv") if isinstance(b, dict) else b))
    st, b = call("POST", "/osgor/assess", {**emp, "contract_date": "2025-06-01"}, who=EMP)
    ok("до вступления БРВ — brv null и note", st == 200 and b["brv"] is None and b["note"], b.get("note"))
    st, b = call("POST", "/osgor/assess", {**emp, "brv": 500_000}, who=EMP)
    ok("переданный brv главнее справочника", st == 200 and b["brv"] == 500_000)


# ---------- 7. журнал ----------

def check_audit():
    print("7. Журнал: логин из сессии")
    coef = {"factor_code": "тест_аудит", "factor_name": "Тест", "class_code": "8", "option_code": "x",
            "option_name": "Вариант", "multiplier": 1.0, "calibrated": 0, "source": "тест"}
    st, _ = call("POST", "/admin/coefficients", coef, who=ADM)
    ok("POST /admin/coefficients → журнал с логином", st == 200 and last_audit("коэффициент").get("who") == ADM)
    st, v = call("POST", "/admin/tariff-versions", {"level": "компания", "name": "тест-аудит",
                                                    "effective_from": "2026-09-21"}, who=ADM)
    ok("POST /admin/tariff-versions → логин", st == 200 and last_audit("новая версия тарифов").get("who") == ADM)
    st, _ = call("POST", "/admin/min-rates", {"tariff_version_id": v["id"], "product_code": "0807",
                                             "min_rate_pct": 0.05}, who=ADM)
    ok("POST /admin/min-rates → логин", st == 200 and last_audit("минимальная ставка").get("who") == ADM)
    st, _ = call("POST", "/admin/products", {"code": "Т999", "name": "тест-аудит", "classes": ["8"]}, who=ADM)
    ok("POST /admin/products → логин", st == 200 and last_audit("продукт").get("who") == ADM)
    st, _ = call("POST", "/valuation/norms", {"code": "тест-аудит", "name": "т", "rate_pct": 5}, who=ADM,
                 params={"who": "подмена"})
    ok("POST /valuation/norms → логин, ?who= игнорируется",
       st == 200 and last_audit("%норма износа").get("who") == ADM, last_audit("%норма износа"))
    st, _ = call("DELETE", "/valuation/norms/тест-аудит", who=ADM, params={"who": "подмена"})
    ok("DELETE /valuation/norms → логин", st == 200 and last_audit("удалена норма износа").get("who") == ADM)


def main():
    with temp_db():
        setup()
        check_fields()
        check_analyze()
        check_thresholds()
        check_osgor()
        check_audit()
    print(f"\nИтого: {passed} ок, {failed} плохо")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
