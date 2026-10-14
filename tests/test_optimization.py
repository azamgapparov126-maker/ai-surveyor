"""
Оптимизация сервера (задача 123) — на ВРЕМЕННОЙ копии базы (tests/tmpdb.py), сеть не используется.

Запуск из корня проекта:
    set PYTHONIOENCODING=utf-8
    sandbox\\.venv\\Scripts\\python.exe tests\\test_optimization.py

Что проверяется:
   1  база: WAL, synchronous=NORMAL, busy_timeout 5000, foreign_keys — у каждого соединения;
   2  справочники в памяти: два расчёта подряд — одна сборка из базы; движок справочник не меняет;
   3  правка коэффициента и минимальной ставки через админ-API — следующий /calculate видит новое сразу;
   4  сессия продлевается записью в базу не чаще раза в минуту, срок короче не более чем на минуту;
   5  Telegram внутри пишущей транзакции уходит только после фиксации, при откате — не уходит;
   6  gzip: страница сжимается; PDF и XLSX — нет, приходят целыми (%PDF, PK + zipfile), с Content-Disposition;
   7  /theme.js: ETag и 304; данные API — Cache-Control: no-store;
   8  необработанная ошибка — 500 с кодом ошибки, код есть в журнале; 404/422 — как раньше;
   9  /health: прежние поля + база, фоновые потоки, даты обновлений;
  10  резервная копия берёт данные из журнала WAL, восстановление не портит базу при открытом соединении.
"""
import asyncio
import gzip
import io
import json
import logging
import os
import pickle
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.pop("SURVEYOR_DEV", None)             # вход по сессии, как на Railway

from tmpdb import temp_db                        # noqa: E402
from app import auth, db, fonts, tgbot           # noqa: E402
from app.main import app                         # noqa: E402

PASSED = []
TOKEN = {}
WAREHOUSE = {"product_code": "0807", "class_code": "8", "object_type": "Склад",
             "value_amount": 4.2e9, "sum_insured": 4.2e9,
             "factors": {"construction": "mixed", "activity": "warehouse", "protection": "alarm",
                         "seismic": "z8", "wear": "mid", "loss_history": "clean", "franchise": "f0"}}


def ok(name, cond, hint=""):
    assert cond, f"ПРОВАЛ: {name} {hint}"
    PASSED.append(name)
    print("  ✓", name)


def call(method, path, body=None, headers=None, token=True):
    """Минимальный ASGI-клиент: (код, заголовки, тело как пришло по сети)."""
    payload = json.dumps(body, ensure_ascii=False).encode() if body is not None else b""
    hdr = [(b"host", b"test"), (b"content-type", b"application/json"),
           (b"content-length", str(len(payload)).encode())]
    if token and TOKEN.get("admin"):
        hdr.append((b"authorization", f"Bearer {TOKEN['admin']}".encode()))
    for k, v in (headers or {}).items():
        hdr.append((k.lower().encode(), v.encode()))
    path_only, _, query = path.partition("?")
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path_only, "raw_path": path_only.encode(), "root_path": "",
             "query_string": query.encode(), "headers": hdr,
             "client": ("10.0.0.5", 0), "server": ("test", 80)}
    out = {"status": None, "headers": {}, "chunks": []}

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
            out["headers"] = {k.decode().lower(): v.decode("latin-1") for k, v in msg.get("headers", [])}
        elif msg["type"] == "http.response.body":
            out["chunks"].append(msg.get("body") or b"")

    asyncio.run(app(scope, receive, send))
    return out["status"], out["headers"], b"".join(out["chunks"])


def jcall(method, path, body=None, **kw):
    st, h, raw = call(method, path, body, **kw)
    if h.get("content-encoding") == "gzip":
        raw = gzip.decompress(raw)
    try:
        return st, json.loads(raw.decode("utf-8"))
    except ValueError:
        return st, raw


def make_admin():
    with db.tx() as con:
        now = db.now()
        cur = con.execute("INSERT INTO users (login, full_name, role, branch, password_hash, salt, status,"
                          " created_at) VALUES (?,?,?,?,?,?,?,?)",
                          ("тест-опт-админ", "Тест Оптимизации", "админ", "тест", "x" * 64, "0" * 32,
                           auth.STATUS_ACTIVE, now))
        u = db.rows(con, "SELECT * FROM users WHERE id=?", cur.lastrowid)[0]
        TOKEN["admin"], _ = auth.create_session(con, u, ip="127.0.0.1", user_agent="test")
    return u


# --------------------------------------------------------------------------- #

def check_pragmas():
    with db.tx() as con:
        p = {k: con.execute(f"PRAGMA {k}").fetchone()[0]
             for k in ("journal_mode", "synchronous", "busy_timeout", "foreign_keys")}
    ok("1. PRAGMA: WAL, synchronous=NORMAL(1), busy_timeout=5000, foreign_keys=1 — %s" % p,
       p == {"journal_mode": "wal", "synchronous": 1, "busy_timeout": 5000, "foreign_keys": 1})


def check_reference_cache():
    loads = {"n": 0}
    real = db._load_reference

    def counting(con, today):
        loads["n"] += 1
        return real(con, today)

    db.invalidate_reference()
    db._load_reference = counting
    try:
        a = jcall("POST", "/calculate", WAREHOUSE)
        b = jcall("POST", "/calculate", WAREHOUSE)
    finally:
        db._load_reference = real
    ok("2. два расчёта подряд — справочники собраны из базы один раз (%d)" % loads["n"],
       a[0] == b[0] == 200 and loads["n"] == 1 and a[1] == b[1])

    with db.tx() as con:
        ref = db.load_reference(con)
    before = pickle.dumps(ref)
    from app.engine import Input, calculate
    for body in (WAREHOUSE, {**WAREHOUSE, "sum_insured": 5e9, "perils_included": ["fire"]},
                 {"product_code": "0311", "class_code": "3", "object_type": "Легковой",
                  "value_amount": 1e8, "sum_insured": 1e8, "payer_type": "юр",
                  "factors": {"veh_age": "a3", "franchise": "f1"}}):
        calculate(ref, Input(**{k: v for k, v in body.items()}))
    ok("2а. движок не меняет справочник на месте (общий объект без копии безопасен)",
       pickle.dumps(ref) == before)


def check_admin_edit_visible():
    st, base = jcall("POST", "/calculate", WAREHOUSE)
    with db.tx() as con:
        c = db.rows(con, "SELECT * FROM coefficients WHERE factor_code='construction' AND option_code='mixed'"
                         " AND class_code='8'")[0]
    st, r = jcall("POST", "/admin/coefficients", {
        "factor_code": "construction", "factor_name": c["factor_name"], "class_code": "8",
        "option_code": "mixed", "option_name": c["option_name"], "multiplier": c["multiplier"] * 2,
        "calibrated": 0, "source": "тест оптимизации"})
    ok("3. админ-API: коэффициент сохранён", st == 200, r)
    st, after = jcall("POST", "/calculate", WAREHOUSE)
    # множитель конструкции в цепочке расчёта: у модели ставки ТЗ 13.10.2026 коэффициенты — в K_об, а не в нетто
    # (нетто — рынок НАПП), поэтому сравнивается сам множитель строки «Конструкция …»
    def _m(x):
        return next(r["mult"] for r in x["explanation"] if str(r.get("name", "")).startswith(c["factor_name"]))
    ratio = _m(after) / _m(base)
    ok("3а. следующий /calculate видит новый коэффициент сразу (множитель ×%.3f)" % ratio,
       st == 200 and abs(ratio - 2.0) < 1e-6)

    today = datetime.now().date().isoformat()
    st, v = jcall("POST", "/admin/tariff-versions", {"level": "компания", "name": "тест оптимизации",
                                                     "effective_from": today})
    st2, _ = jcall("POST", "/admin/min-rates", {"tariff_version_id": v["id"], "product_code": "0807",
                                               "min_rate_pct": 7.77})
    st3, after2 = jcall("POST", "/calculate", WAREHOUSE)
    ok("3б. новая версия тарифа с минимальной ставкой 7,77%% — в следующем расчёте (min_pct=%s)"
       % after2["rates"]["min_pct"], st == st2 == st3 == 200 and after2["rates"]["min_pct"] == 7.77)


def check_session_throttle(uid):
    with db.tx() as con:
        token, _ = auth.create_session(con, {"id": uid, "login": "тест-опт-админ"}, ip="", user_agent="t")
        # сессия, продлённая 30 с назад: записи быть не должно
        fresh = (datetime.now() + timedelta(hours=auth.SESSION_HOURS) - timedelta(seconds=30))
        con.execute("UPDATE sessions SET expires_at=? WHERE token=?", (auth._ts(fresh), token))
    with db.tx() as con:
        n0 = con.total_changes
        auth.session_user(con, token)
        no_write = con.total_changes == n0
    with db.tx() as con:                                     # продлена 2 минуты назад — продлеваем
        old = datetime.now() + timedelta(hours=auth.SESSION_HOURS) - timedelta(seconds=120)
        con.execute("UPDATE sessions SET expires_at=? WHERE token=?", (auth._ts(old), token))
    with db.tx() as con:
        n0 = con.total_changes
        auth.session_user(con, token)
        wrote = con.total_changes == n0 + 1
        exp = datetime.fromisoformat(db.rows(con, "SELECT expires_at FROM sessions WHERE token=?", token)[0]
                                     ["expires_at"])
    gap = (datetime.now() + timedelta(hours=auth.SESSION_HOURS) - exp).total_seconds()
    ok("4. продление сессии: 30 с назад — без записи; 2 мин назад — запись, срок полный (разница %.0f с)" % gap,
       no_write and wrote and -2 <= gap <= 2)


def check_deferred_telegram():
    sent = []
    real = (tgbot.bot_token, tgbot._deliver)
    tgbot.bot_token = lambda: "123:TEST"
    tgbot._deliver = lambda method, payload: sent.append((method, payload)) or {"ok": True, "result": {}}
    try:
        with db.tx() as con:
            db.audit(con, "тест", "запись до отправки")          # пишущая транзакция открыта
            res = tgbot.send("555", "после фиксации", con=con)
            inside = list(sent)
        ok("5. внутри пишущей транзакции отправка отложена (%s), до commit ничего не ушло" % res.get("deferred"),
           res.get("deferred") and not inside and len(sent) == 1)
        sent.clear()
        try:
            with db.tx() as con:
                db.audit(con, "тест", "запись, которая откатится")
                tgbot.send("555", "не должно уйти", con=con)
                raise RuntimeError("откат")
        except RuntimeError:
            pass
        ok("5а. транзакция откатилась — сообщение не ушло", not sent)
        with db.tx() as con:                                     # без записи — сразу, как раньше
            res = tgbot.send("555", "сразу", con=con)
        ok("5б. без пишущей транзакции — отправка сразу, результат настоящий", res.get("ok") and
           not res.get("deferred") and len(sent) == 1)
    finally:
        tgbot.bot_token, tgbot._deliver = real


def check_gzip_and_downloads():
    st, h, raw = call("GET", "/ui", headers={"accept": "text/html", "accept-encoding": "gzip, deflate"})
    st2, h2, plain = call("GET", "/ui", headers={"accept": "text/html"})
    ok("6. /ui сжимается: %d → %d байт, распакованное совпадает с несжатым" % (len(plain), len(raw)),
       st == st2 == 200 and h.get("content-encoding") == "gzip" and gzip.decompress(raw) == plain
       and h.get("vary", "").lower().find("accept-encoding") >= 0)

    st, r = jcall("POST", "/requests", {**WAREHOUSE, "branch": "тест", "policyholder": "ООО «Тест»"})
    rid = r["request_id"]
    for path, magic, disp in ((f"/requests/{rid}/analysis.pdf", b"%PDF", "inline"),
                              (f"/requests/{rid}/proposal.pdf", b"%PDF", "inline"),
                              (f"/requests/{rid}/analysis.xlsx", b"PK", "attachment")):
        st, h, blob = call("GET", path, headers={"accept-encoding": "gzip, deflate"})
        if magic == b"%PDF" and fonts.find_pair() is None:
            # на машине нет шрифта из app/fonts.py: PDF не проверить, но ответ обязан быть понятной 503
            print(f"  ПРОПУСК 6а/6б {path.split('/')[-1]}: нет шрифта для PDF (проверенные пути — в журнале)")
            ok(f"6а. {path.split('/')[-1]}: нет шрифта — 503 «На сервере нет шрифта»",
               st == 503 and fonts.NO_FONT.encode("utf-8") in blob, (st, blob[:200]))
            continue
        good = st == 200 and not h.get("content-encoding") and blob[:len(magic)] == magic \
            and h.get("content-disposition", "").startswith(disp)
        if magic == b"PK":
            good = good and bool(zipfile.ZipFile(io.BytesIO(blob)).namelist())
        ok(f"6а. {path.split('/')[-1]}: без gzip, начинается с {magic.decode()}, "
           f"Content-Disposition «{h.get('content-disposition')}», {len(blob)} байт", good)
        ok(f"6б. {path.split('/')[-1]}: Cache-Control {h.get('cache-control')}", h.get("cache-control") == "no-store")
    return rid


def check_cache_headers():
    st, h, _ = call("GET", "/theme.js", token=False)
    et = h.get("etag")
    st2, h2, body2 = call("GET", "/theme.js", headers={"if-none-match": et}, token=False)
    ok("7. /theme.js: ETag %s, no-cache; повтор с If-None-Match → 304 без тела" % et,
       st == 200 and et and h.get("cache-control") == "no-cache" and st2 == 304 and body2 == b"")
    st, h, _ = call("GET", "/i18n/ru.json", token=False)
    st2, _, body2 = call("GET", "/i18n/ru.json", headers={"if-none-match": h.get("etag", "")}, token=False)
    ok("7а. /i18n/ru.json: ETag и max-age как раньше, повтор → 304",
       st == 200 and h.get("etag") and "max-age" in h.get("cache-control", "") and st2 == 304)
    st, h, _ = call("GET", "/requests")
    ok("7б. /requests (данные пользователя): Cache-Control no-store", st == 200 and h.get("cache-control") == "no-store")


def check_errors():
    from fastapi import HTTPException

    @app.get("/__test_boom")
    def boom():
        raise RuntimeError("секрет-в-тексте-исключения")

    @app.get("/__test_http")
    def http_err():
        raise HTTPException(409, "как раньше")

    records = []
    h = logging.Handler()
    h.emit = lambda rec: records.append(rec)
    logging.getLogger("surveyor.web").addHandler(h)
    try:
        st, body = jcall("GET", "/__test_boom?phone=%2B998900000001")
        st_html, hh, raw = call("GET", "/__test_boom", headers={"accept": "text/html"})
    finally:
        logging.getLogger("surveyor.web").removeHandler(h)
    detail = body.get("detail", "") if isinstance(body, dict) else ""
    code = detail.split("Код ошибки: ")[-1].split(".")[0] if "Код ошибки: " in detail else ""
    logged = [r for r in records if code and code in r.getMessage()]
    ok("8. ошибка → 500 {detail: «Внутренняя ошибка сервера. Код ошибки: %s…»}, код в журнале, "
       "строки запроса в журнале нет" % code,
       st == 500 and detail.startswith("Внутренняя ошибка сервера. Код ошибки: ")
       and detail.endswith("Сообщите администратору.") and logged and logged[0].exc_info
       and "998900000001" not in logged[0].getMessage())
    ok("8а. страница → 500 с тем же текстом в HTML", st_html == 500 and "Код ошибки" in raw.decode("utf-8"))
    st, body = jcall("GET", "/__test_http")
    st2, body2 = jcall("POST", "/calculate", {"product_code": "0807"})
    st3, body3 = jcall("GET", "/requests/999999999")
    ok("8б. HTTPException и ошибки ввода — как раньше: %d, %d, %d" % (st, st2, st3),
       st == 409 and body["detail"] == "как раньше" and st2 == 422 and st3 == 404)
    # журнал: секрет вебхука в пути и ПД в тексте исключения маскируются, стек остаётся
    import logging as _lg
    from app import web as _web
    try:
        raise ValueError("телефон +998 90 123-45-67")
    except ValueError as e:
        info = _web._masked_exc_info(e)
    text = _lg.Formatter().formatException(info)
    ok("8в. журнал ошибок: /tg/webhook/<секрет> → %s; телефон в тексте исключения скрыт, стек на месте"
       % _web._safe_path("/tg/webhook/abc123"),
       # последняя строка — текст исключения; строки кода в стеке (там литерал) — не данные людей
       _web._safe_path("/tg/webhook/abc123") == "/tg/webhook/***"
       and text.splitlines()[-1].endswith("ValueError: телефон [ТЕЛЕФОН]") and "test_optimization.py" in text,
       text[-300:])


def check_health():
    st, h = jcall("GET", "/health", token=False)
    ok("9. /health: status/products на месте, база %s, потоки и даты обновлений" % h.get("db"),
       st == 200 and h["status"] == "ok" and h["products"] > 0 and h["db"]["journal_mode"] == "wal"
       and "threads" in h["background"] and set(h["updated"]) == {"market_stats", "stat_series", "lawwatch", "exchange_quotes",
                                   "market_knowledge", "competitors"}
       and set(h["data_sources"]) == {"napp", "stat_uz", "exchange", "laws", "market_knowledge", "competitors"})


def check_backup_restore():
    sys.path.insert(0, str(ROOT / "tools"))
    import backup as backup_tool
    import restore as restore_tool
    folder = Path(tempfile.mkdtemp(prefix="bkp-test-"))
    saved = (backup_tool.DATA, backup_tool.DB, backup_tool.BACKUPS, restore_tool.DATA, restore_tool.BACKUPS,
             backup_tool.FOLDERS)
    try:
        # только база: папки файлов (отчёты НАПП, доклады) лежат в проекте — тест их не копирует и не пишет
        backup_tool.FOLDERS = ()
        backup_tool.DATA = restore_tool.DATA = folder
        backup_tool.DB = folder / "surveyor.db"
        backup_tool.BACKUPS = restore_tool.BACKUPS = folder / "backups"
        live = db.connect_path(backup_tool.DB)                  # WAL
        live.execute("CREATE TABLE t (v TEXT)")
        live.execute("INSERT INTO t VALUES ('в журнале WAL')")
        live.commit()                                           # соединение открыто: строка лежит в -wal
        wal = Path(str(backup_tool.DB) + "-wal")
        res = backup_tool.make("тест")
        with zipfile.ZipFile(res["path"]) as z:
            snap = folder / "snap.db"
            snap.write_bytes(z.read("data/surveyor.db"))
        c = sqlite3.connect(snap)
        got = c.execute("SELECT v FROM t").fetchall()
        c.close()
        ok("10. копия при открытом соединении (-wal %d байт) содержит строку из журнала WAL"
           % (wal.stat().st_size if wal.exists() else 0), got == [("в журнале WAL",)])
        live.execute("INSERT INTO t VALUES ('после копии')")
        live.commit()
        with zipfile.ZipFile(res["path"]) as z:
            assert [n for n in z.namelist() if n != "КОПИЯ.txt"] == ["data/surveyor.db"], z.namelist()
        out = restore_tool.restore(res["file"], confirm=True)
        rows = [r[0] for r in live.execute("SELECT v FROM t ORDER BY rowid")]
        integ = live.execute("PRAGMA integrity_check").fetchone()[0]
        live.close()
        ok("10а. восстановление при открытом соединении: база целая (%s), строки как в копии %s" % (integ, rows),
           out["applied"] and integ == "ok" and rows == ["в журнале WAL"])
    finally:
        (backup_tool.DATA, backup_tool.DB, backup_tool.BACKUPS, restore_tool.DATA, restore_tool.BACKUPS,
         backup_tool.FOLDERS) = saved
        import shutil
        shutil.rmtree(folder, ignore_errors=True)


def main():
    with temp_db("surveyor-test-opt.db"):
        db.invalidate_reference()
        u = make_admin()
        check_pragmas()
        check_reference_cache()
        check_admin_edit_visible()
        check_session_throttle(u["id"])
        check_deferred_telegram()
        check_gzip_and_downloads()
        check_cache_headers()
        check_errors()
        check_health()
        check_backup_restore()
    db.invalidate_reference()
    print(f"Все проверки пройдены: {len(PASSED)}")


if __name__ == "__main__":
    main()
