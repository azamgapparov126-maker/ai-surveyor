"""
Актуарный отчёт по субпортфелям (ТЗ разработчику 13.10.2026, день 2.3): разбор отчёта INSON за 2022 год со сверкой
сумм, таблица actuarial_reports, GET/PUT /reference/actuarial-reports, нагрузка L в act_settings.load.
Всё — на ВРЕМЕННОЙ копии базы (tests/tmpdb.py).

Запуск из корня проекта:
    set PYTHONIOENCODING=utf-8
    sandbox\\.venv\\Scripts\\python.exe tests\\test_actuarial.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.pop("SURVEYOR_DEV", None)

from tmpdb import temp_db                              # noqa: E402
from app import act_engine as ae, actuarial, auth, db  # noqa: E402
from app.main import app                               # noqa: E402

PASSED = []
TOK = {}
TEXT = actuarial.REPORT_FILE.read_text(encoding="utf-8")


def ok(name, cond, hint=""):
    assert cond, f"ПРОВАЛ: {name} {hint}"
    PASSED.append(name)
    print("  ✓", name)


def call(method, path, body=None, who=None):
    payload = json.dumps(body, ensure_ascii=False).encode() if body is not None else b""
    path, _, query = path.partition("?")
    hdrs = [(b"host", b"test"), (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode())]
    if who:
        hdrs.append((b"authorization", f"Bearer {TOK[who]}".encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "",
             "query_string": query.encode(), "headers": hdrs, "client": ("203.0.113.9", 0), "server": ("test", 80)}
    out = {"status": None, "chunks": []}

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
        elif msg["type"] == "http.response.body":
            out["chunks"].append(msg.get("body") or b"")

    asyncio.run(app(scope, receive, send))
    data = b"".join(out["chunks"])
    try:
        return out["status"], json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return out["status"], data


def setup():
    with db.tx() as con:
        for login, role in (("тест-актуарий-админ", "админ"), ("тест-актуарий-сотр", "сотрудник")):
            cur = con.execute(
                "INSERT INTO users (login, full_name, phone, role, branch, password_hash, salt, status, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (login, "Тест " + role, "", role, "тест", "x" * 64, "0" * 32, auth.STATUS_ACTIVE, db.now()))
            u = db.rows(con, "SELECT * FROM users WHERE id=?", cur.lastrowid)[0]
            TOK[role], _ = auth.create_session(con, u, ip="203.0.113.9", user_agent="test")


def check_parse():
    print("Разбор отчёта 2022 и сверка сумм")
    res = actuarial.parse_2022(TEXT)
    it, tot = res["items"], res["totals"]
    ok("сверка прошла без ошибок", not res["errors"], res["errors"])
    ok("8 субпортфелей: ТС, Авиа, Имущество + СМР, Ответственность, Фин. риски, ОСГОВТС, ОСГОР, Прочие",
       set(it) == set(actuarial.SUBPORTFOLIOS), sorted(it))
    s = sum(r["premium_earned"] for r in it.values())
    ok(f"Σ заработанной премии {s:.2f} = итог 73 566,68 (±0,05)",
       tot["premium_earned"] == 73566.68 and abs(s - 73566.68) <= 0.05, (s, tot))
    ok("Σ выплат = 11 302,42, Σ расходов = 47 583,04 (стр. 15)",
       tot["payouts"] == 11302.42 and abs(sum(r["payouts"] for r in it.values()) - 11302.42) <= 0.05
       and tot["expenses"] == 47583.04 and abs(sum(r["expenses"] for r in it.values()) - 47583.04) <= 0.05, tot)
    exp = {"vehicle": (22051.18, 3918.48, 13037.73, 8.2, 1629.7), "property": (9063.76, 111.73, 6610.11, 10.5, 143.6),
           "financial": (22499.32, 5970.51, 14071.30, 1.8, 6595.0), "aviation": (13012.87, 0, 9088.66, 0, None),
           "liability": (1676.06, 0, 1249.99, 7.8, None), "osgovts": (882.44, 569.88, 481.71, 14.0, 860.4),
           "osgor": (580.83, 196.61, 371.32, 10.3, 462.7), "other": (3800.23, 535.20, 2672.21, 3.4, 272.1)}
    bad = {k: (it[k]["premium_earned"], it[k]["payouts"], it[k]["expenses"], it[k]["commission_pct"],
               it[k]["risk_margin"]) for k in exp if (it[k]["premium_earned"], it[k]["payouts"], it[k]["expenses"],
                                                      it[k]["commission_pct"], it[k]["risk_margin"]) != exp[k]}
    ok("премия, выплаты, расходы, комиссия и рисковая надбавка — как в отчёте (стр. 20–39)", not bad, bad)
    L = {k: it[k]["load_L"] for k in ("property", "vehicle", "financial", "other")}
    ok("L = расходы / заработанная премия: имущество 0,729; ТС 0,591; фин. риски 0,625; прочие 0,703",
       L == {"property": 0.729, "vehicle": 0.591, "financial": 0.625, "other": 0.703}, L)
    ld = ae.merge_settings({})["load"]
    ok("act_settings.load совпадает с отчётом и помечен «временно», с версией и датой",
       {k: ld["by_group"][k] for k in L} == L and ld["commission"] == 0.10 and ld.get("version")
       and ld.get("date") and "временно" in json.dumps(ld, ensure_ascii=False)
       and "2022" in json.dumps(ld, ensure_ascii=False), ld)

    broken = TEXT.replace("22 051,18", "22 151,18", 1)
    res2 = actuarial.parse_2022(broken)
    ok("испорченная сумма (стр. 8) — сверка не проходит", any("≠ итог" in e or "расходятся" in e
                                                               for e in res2["errors"]), res2["errors"])
    ok("число: «3 918,48», «- 2 020,84», «-»",
       actuarial.num("3 918,48") == 3918.48 and actuarial.num("- 2 020,84") == -2020.84 and actuarial.num("-") == 0.0
       and actuarial.num("Итого") is None)
    return res


def check_load():
    print("Загрузка в базу (tools/actuarial_load.py)")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import actuarial_load
    with db.tx() as con:
        con.execute("DELETE FROM actuarial_reports")
    out = actuarial_load.run()
    ok("первый запуск: 8 строк добавлено", out["added"] == 8 and out["updated"] == 0, out)
    out2 = actuarial_load.run()
    ok("повторный запуск: те же 8 строк обновлены, новых нет", out2["added"] == 0 and out2["updated"] == 8, out2)
    with db.tx() as con:
        rows = db.rows(con, "SELECT * FROM actuarial_reports WHERE year=2022")
    ok("в таблице 8 строк 2022 года с источником", len(rows) == 8 and all("Актуарный отчёт" in r["source"]
                                                                           for r in rows), len(rows))
    folder = Path(tempfile.mkdtemp(prefix="actuarial-"))
    bad = folder / "отчёт.txt"
    bad.write_text(TEXT.replace("3 918,48", "3 999,48"), encoding="utf-8")
    before = rows
    try:
        with db.tx() as con:
            actuarial.load_2022(con, "тест", bad)
        failed = False
    except ValueError as e:
        failed = "сверка" in str(e)
    with db.tx() as con:
        after = db.rows(con, "SELECT * FROM actuarial_reports WHERE year=2022")
    ok("сверка выплат не прошла — ошибка, таблица не изменилась", failed and before == after)


def check_api():
    print("GET / PUT /reference/actuarial-reports")
    st, out = call("GET", "/reference/actuarial-reports")
    ok("без входа → 401", st == 401, st)
    st, out = call("GET", "/reference/actuarial-reports", who="сотрудник")
    ok("не администратор → 403", st == 403, st)
    st, out = call("GET", "/reference/actuarial-reports?year=2022", who="админ")
    ok("администратор видит таблицу 2022: 8 строк и итоги",
       st == 200 and out["year"] == 2022 and len(out["items"]) == 8
       and abs(out["totals"]["premium_earned"] - 73566.68) <= 0.05, (st, out.get("totals")))
    ok("в ответе — настройка нагрузки (act_settings.load)", out["load_settings"]["by_group"]["property"] == 0.729,
       out.get("load_settings"))

    item = {"subportfolio": "property", "premium_earned": 9063.76, "payouts": 111.73, "expenses": 6610.11,
            "commission_pct": 10.5, "risk_margin": 143.6}
    for name, body in (("неизвестный субпортфель", {"year": 2022, "source": "т", "items": [dict(item, subportfolio="x")]}),
                       ("отрицательные выплаты", {"year": 2022, "source": "т", "items": [dict(item, payouts=-1)]}),
                       ("L больше 1", {"year": 2022, "source": "т", "items": [dict(item, load_L=1.2)]}),
                       ("нет источника", {"year": 2022, "items": [item]}),
                       ("год в будущем", {"year": 2099, "source": "т", "items": [item]}),
                       ("сумма не сходится с итогом", {"year": 2022, "source": "т", "items": [item],
                                                       "total_premium_earned": 10000})):
        st, out = call("PUT", "/reference/actuarial-reports", body, who="админ")
        ok(f"PUT: {name} → 422 с текстом", st == 422 and out.get("detail"), (st, out))
    st, out = call("PUT", "/reference/actuarial-reports", {"year": 2022, "source": "т", "items": [item]},
                   who="сотрудник")
    ok("PUT не администратором → 403", st == 403, st)
    st, out = call("PUT", "/reference/actuarial-reports",
                   {"year": 2021, "source": "Актуарный отчёт 2022, столбец 2021 (тест)",
                    "items": [{"subportfolio": "vehicle", "premium_earned": 4852.3, "payouts": 2130.2,
                               "expenses": 4506.1, "commission_pct": 13.8, "risk_margin": 1475.5}],
                    "total_premium_earned": 4852.3}, who="админ")
    ok("PUT корректной строки 2021 → 200, L посчитана",
       st == 200 and out["saved"]["added"] == 1 and out["items"][0]["load_L"] == round(4506.1 / 4852.3, 3),
       (st, out))
    ok("годы в таблице: 2021 и 2022", out["years"] == [2021, 2022], out.get("years"))
    with db.tx() as con:
        a = db.rows(con, "SELECT * FROM audit WHERE action='актуарный отчёт' ORDER BY id DESC LIMIT 1")
    ok("правка — в журнале audit", a and a[0]["entity"] == "year:2021", a)


def main():
    check_parse()
    with temp_db():
        db.ensure_schema()
        setup()
        check_load()
        check_api()
    print(f"\nхорошо {len(PASSED)}, плохо 0")


if __name__ == "__main__":
    main()
