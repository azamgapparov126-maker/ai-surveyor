"""
Сервер ИИ-сюрвейера. Запуск: run.bat  (или  uvicorn app.main:app --port 8000)

Точки подключения (все отдают JSON, документация — /docs):
  GET  /health                          — жив ли сервер
  GET  /reference/{products|classes|perils|coefficients|checklists|rules}
  POST /calculate                       — расчёт без сохранения (для форм и проверок «на лету»)
  POST /requests                        — запрос от филиала: сохранить, посчитать, вернуть карточку
  GET  /requests, GET /requests/{id}    — список и карточка (сотрудник и агент видят только свои)
  POST /requests/{id}/documents         — загрузить документ
  POST /requests/{id}/photos            — фото объекта; GET /requests/{id}/photos, GET/DELETE /photos/{id}
  POST /requests/{id}/documents/upload  — техпаспорт или кадастр (PDF/фото), сразу разбирается
  POST /documents/parse                 — разбор загруженного документа; GET /requests/{id}/documents/fields
  GET  /requests/{id}/checklist         — чек-лист документов: что получено
  GET  /valuation/norms, /valuation/settings — нормы износа и настройки оценки (POST — правка)
  POST /requests/{id}/decide            — решение назначенного согласующего (app/approvals.py)
  GET  /analytics/summary               — аналитика запросов
  POST /analytics/risk, GET /analytics/risk/fields, GET|PUT /analytics/risk/thresholds — аналитика риска (app/risk_api.py)
  GET  /analytics/risk/docs, POST /analytics/risk/document, GET|DELETE /analytics/risk/document/{id} —
       документы для аналитики по шагам (app/analysis_docs.py)
  GET  /analytics/risk/presets, GET|PUT /analytics/risk/last, GET /analytics/risk/fields?mode=quick —
       быстрый режим аналитики: пресеты, последний выбор формы, четыре обязательных поля
  GET  /osgor/activities, POST /osgor/quick, /osgor/assess — ОСГОР (app/osgor.py)
  POST /act/photos, /act/make, GET /act/{id}[.docx|.pdf] — сюрвейерский акт, лёгкая версия (app/act.py)
  GET  /act/market/links, POST /act/market/shots — оценка по объявлениям со снимков сотрудника (app/act_market.py)
  POST /admin/tariff-versions, /admin/min-rates, /admin/coefficients, /admin/products, PUT /admin/financials
  GET  /requests/{id}/explain           — объяснение расчёта клиенту (ИИ, без него — шаблон)
  GET  /llm/status, POST /llm/ping, GET /llm/calls — состояние и журнал обращений к ИИ
  GET  /deploy/status, /deploy/settings, /deploy/checklist, /deploy/schema-check, POST /deploy/backup
  POST /tg/auth                         — вход мини-приложения Telegram
  POST /tg/webhook/{secret}             — обновления от бота Telegram (app/tgbot.py)
  GET  /tg/me, /tg/inbox, /tg/my-requests, /tg/bot-status — данные для мини-приложения
  GET  /ui — экран агента, GET /admin — админка (то же /admin/hub), GET /admin/deploy — запуск, GET /tg — мини-апп

Доступ: всё, кроме белого списка (/health, /login, /auth/*, /tg…, /theme.js), требует сессии —
единый вход app/guard.py. Разделы админа (/admin*, /deploy/*, /tasks*, /reports*, /audit) — только «админ».
На своём компьютере guard отключает SURVEYOR_DEV=1 (run.bat), и только для адреса 127.0.0.1.
"""
import json
import shutil
import sys
import os
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import access, background, db, web
from .auth import current_user, require
from .engine import Input, calculate

ADMIN = "админ"                 # запись в справочники и финансы — только админ, и в режиме разработчика тоже

web.setup_logging()

ROOT = Path(__file__).resolve().parent.parent
UPLOADS = db.DATA_DIR / "uploads"

app = FastAPI(title="ИИ-сюрвейер INSON", version="0.1",
              description="Расчёт ставки, проверки по законодательству и тарифной политике, документы, аналитика.")


sys.path.insert(0, str(ROOT / "tools"))
import market_stats  # noqa: E402  (tools/market_stats.py)

REFRESH_EVERY_SEC = 24 * 3600
_refresh_state = {"last": None, "log": [], "running": False}
_refresh_lock = threading.Lock()          # расписание и кнопка «обновить» не запускают разбор дважды


def _refresh_job():
    """Агент-статистик: сам проверяет сайт НАПП, забирает новые отчёты и обновляет ряд."""
    if not _refresh_lock.acquire(blocking=False):
        return
    _refresh_state["running"] = True
    try:
        _refresh_state["log"] = market_stats.refresh()
        _refresh_state["last"] = db.now()
        with db.tx() as con:
            db.audit(con, "агент-статистик", "обновление рыночной статистики", "market_stats",
                     _refresh_state["log"][-1])
        background.ok("stats-refresh")
        _after_stats()
    except Exception as e:  # ошибка сети не должна ронять сервер
        _refresh_state["log"] = [f"ошибка: {e}"]
        background.failed("stats-refresh", e)
    finally:
        _refresh_state["running"] = False
        _refresh_lock.release()


def _after_stats():
    """Новый срез НАПП → пересборка знаний о рынке (в фоне, только если данные изменились)."""
    try:
        from . import market_knowledge
        market_knowledge.trigger()
    except Exception as e:                # знания — дополнение: статистика уже обновлена
        background.failed("market-knowledge", e)


def _scheduler():
    background.plan("stats-refresh", 60)
    time.sleep(60)               # даём серверу подняться
    while True:
        _refresh_job()
        background.plan("stats-refresh", REFRESH_EVERY_SEC)
        time.sleep(REFRESH_EVERY_SEC)


def _inbox_step():
    from . import history
    res = history.auto_import()
    if res:
        with db.tx() as con:
            db.audit(con, "агент-статистик", "автоимпорт выгрузок", "portfolio", res)


def _inbox_watcher():
    """Агент-статистик: раз в 10 минут смотрит папку data/inbox/portfolio — новые выгрузки договоров
    импортируются сами и связываются с прошлыми загрузками по номеру договора."""
    background.run_loop("inbox-watcher", _inbox_step, first_delay=90, every=600)


def _analysis_cleanup():
    """Договоры, загруженные во вкладке «Аналитика», живут 24 часа: раз в час убираем просроченные."""
    from . import analysis_docs
    background.run_loop("analysis-cleanup", analysis_docs.cleanup, first_delay=120, every=3600)


def _act_cleanup():
    """Сюрвейерский акт (app/act.py): фото живут 24 часа, акты — 7 дней; раз в час убираем просроченные."""
    from . import act
    background.run_loop("act-cleanup", act.cleanup, first_delay=150, every=3600)


DEMO_SEED_TIMEOUT_SEC = 600


def _demo_seed_then_bootstrap():
    """Демо-данные (только тестовый сервер) — в фоне, чтобы не задерживать старт; с пределом по времени.
    Код первого администратора — после них, как и раньше: демо-данные могут завести своих людей."""
    import subprocess
    try:
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "demo_seed.py"), "--yes"],
                           capture_output=True, text=True, timeout=DEMO_SEED_TIMEOUT_SEC,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        print("демо-данные:", (r.stdout or r.stderr).strip()[-400:])
    except subprocess.TimeoutExpired:
        background.failed("demo-seed", kind="превышено время")
        print(f"демо-данные: не уложились в {DEMO_SEED_TIMEOUT_SEC} с — процесс остановлен")
    db.invalidate_reference()             # данные менял другой процесс — справочники перечитать
    _bootstrap_code()


def _bootstrap_code():
    try:                                   # пустой сервер: код первого администратора — в журнал
        from . import guard
        guard.ensure_bootstrap_code()
    except Exception as e:
        print("код первого администратора не выдан:", e)


@app.on_event("startup")
def startup():
    from .web import install_log_filters   # журналы uvicorn: без строки запроса, секрета вебхука и ПД
    install_log_filters()
    from . import auth as _auth
    if _auth.dev_mode():                   # режим разработчика: исходники мини-аппа app/tg/ новее app/tg.html — пересобрать
        try:
            from . import tgpage
            if tgpage.ensure_fresh():
                print("мини-приложение пересобрано: app/tg.html")
        except Exception as e:             # сборка не удалась — отдаём прежний app/tg.html
            print("мини-приложение не пересобрано:", e)
    db.init_storage()
    db.ensure_schema()
    from . import refsync                 # справочники образа -> постоянный диск (если сборка новее)
    refsync.sync_on_start()
    try:                                   # рэнкинг страховщиков snsratings.uz → company_rankings (+ INSON в
        from . import rankings             # company_financials вместо временных цифр): если пусто или текст новее
        rankings.ensure_loaded()
    except Exception as e:                 # нет файла или он не разобрался — сервер всё равно поднимается
        print("рэнкинг страховщиков не загружен:", e)
    UPLOADS.mkdir(parents=True, exist_ok=True)
    (db.DATA_DIR / "photos").mkdir(parents=True, exist_ok=True)
    # фоновые потоки: каждый не больше одного, SURVEYOR_NO_BACKGROUND=1 выключает все (тесты, замеры)
    background.start("stats-refresh", _scheduler)
    background.start("inbox-watcher", _inbox_watcher)
    background.start("analysis-cleanup", _analysis_cleanup)
    background.start("act-cleanup", _act_cleanup)
    from . import team
    team.start_scheduler()
    try:                                   # открытые данные агентства статистики: раз в сутки
        from . import statagency
        statagency.start_scheduler()
    except Exception as e:                 # модуль или сеть не готовы — сервер всё равно поднимается
        print("расписание агентства статистики не запущено:", e)
    try:                                   # биржевые цены УзРТСБ (uzex.uz): раз в сутки
        from . import uzex
        uzex.start_scheduler()
    except Exception as e:
        print("расписание биржевых цен не запущено:", e)
    try:                                   # слежение за законодательством: раз в сутки, 06:30
        from . import lawwatch
        lawwatch.start_scheduler()
    except Exception as e:
        print("расписание слежения за законодательством не запущено:", e)
    try:                                   # документы конкурентов: раз в 7 дней, первая проверка через 15 минут
        from . import competitors
        competitors.start_scheduler()
    except Exception as e:
        print("расписание документов конкурентов не запущено:", e)
    try:                                   # бот Telegram: опрос getUpdates — только при TG_POLLING=1 и токене
        from . import tgbot
        if tgbot.start_polling():
            print("бот Telegram: включён запасной режим опроса (TG_POLLING=1)")
    except Exception as e:
        print("опрос Telegram не запущен:", e)
    # демо-данные для показа: только тестовый сервер, только PD_MODE=test
    if os.environ.get("DEMO_SEED") == "1" and background.start("demo-seed", _demo_seed_then_bootstrap):
        return
    _bootstrap_code()


# ---------- модели входа ----------

class Credit(BaseModel):
    loan_amount: float
    collateral_value: float = 0
    policyholder_is_bank: bool = True
    payer_is_bank: bool = True


class CalcIn(BaseModel):
    product_code: str
    class_code: Optional[str] = None          # если не задан — первый класс продукта
    object_type: str = "Склад"
    value_amount: float = Field(gt=0)
    sum_insured: float = Field(gt=0)
    term_days: int = 365
    factors: dict = {}
    perils_included: Optional[list] = None
    docs_received: list = []
    applied_rate_pct: Optional[float] = None
    manual_reason: str = ""
    premium_paid: bool = False
    disclosure_done: bool = False
    credit: Optional[Credit] = None
    takaful: bool = False
    payer_type: Optional[str] = None
    object_key: Optional[str] = None          # ключ объекта: подтянуть последнюю оценку стоимости
    valuation_id: Optional[int] = None        # или конкретная оценка


class RequestIn(CalcIn):
    external_no: Optional[str] = None         # номер договора в учётной системе
    branch: Optional[str] = None
    policyholder: Optional[str] = None
    beneficiary: Optional[str] = None
    insured_person: Optional[str] = None
    agent_eais_id: Optional[str] = None
    address: Optional[str] = None
    region: Optional[str] = None
    seismic_zone: Optional[int] = None
    term_from: Optional[str] = None
    term_to: Optional[str] = None


def to_input(con, c: CalcIn) -> Input:
    pcs = db.rows(con, "SELECT class_code FROM product_classes WHERE product_code=? ORDER BY part_no", c.product_code)
    if not pcs:
        raise HTTPException(404, f"Продукт {c.product_code} не найден")
    cls = c.class_code or pcs[0]["class_code"]
    val = None
    try:                                       # оценка стоимости — необязательный модуль
        from .valuation import valuation_for_engine, latest_valuation
        if c.valuation_id:
            v = db.rows(con, "SELECT * FROM valuations WHERE id=?", c.valuation_id)
            if v:
                val = {"id": v[0]["id"], "value": v[0]["ai_value"], "method": v[0]["method"],
                       "method_version": v[0]["method_version"], "as_of": v[0]["created_at"],
                       "confirmed_by": v[0]["confirmed_by_underwriter"]}
        elif c.object_key:
            val = valuation_for_engine(con, key=c.object_key)
    except Exception:                          # модуль оценки не должен ронять расчёт
        val = None
    return Input(product_code=c.product_code, class_code=cls, object_type=c.object_type,
                 value_amount=c.value_amount, sum_insured=c.sum_insured, term_days=c.term_days,
                 factors=c.factors, perils_included=c.perils_included, docs_received=c.docs_received,
                 applied_rate_pct=c.applied_rate_pct, manual_reason=c.manual_reason,
                 premium_paid=c.premium_paid, disclosure_done=c.disclosure_done,
                 credit=c.credit.model_dump() if c.credit else None, takaful=c.takaful, payer_type=c.payer_type, valuation=val)


# ---------- справочники ----------

@app.get("/health")
def health():
    """Проверка живости (Railway, Docker). status и products — как раньше: на них опирается проверка
    площадки. Дальше — состояние базы, фоновых потоков и даты обновлений; без секретов и данных людей."""
    with db.tx() as con:
        n = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        base = {"opens": True, "journal_mode": con.execute("PRAGMA journal_mode").fetchone()[0]}
        updates = {}
        for key, sql in (("market_stats", "SELECT MAX(loaded_at) FROM market_stats"),
                         ("stat_series", "SELECT MAX(fetched_at) FROM stat_series"),
                         ("lawwatch", "SELECT MAX(last_checked_at) FROM watched_acts"),
                         ("exchange_quotes", "SELECT MAX(fetched_at) FROM exchange_quotes")):
            try:
                updates[key] = con.execute(sql).fetchone()[0]
            except Exception:
                updates[key] = None
    sources = data_sources(updates)
    updates["market_knowledge"] = sources["market_knowledge"]["last"]
    updates["competitors"] = sources["competitors"]["last"]
    return {"status": "ok", "products": n, "db": base, "background": background.status(),
            "updated": updates, "data_sources": sources}


def _next_daily(hour: int, minute: int) -> str:
    from datetime import datetime, timedelta
    now = datetime.now()
    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if at <= now:
        at += timedelta(days=1)
    return at.isoformat(timespec="seconds")


def data_sources(updated: dict = None) -> dict:
    """Блок «Источники данных» для /health и админки (Система → Источники данных): когда обновлялось,
    когда следующий запуск, последняя ошибка (только тип, без текста). Сбой одного модуля не роняет блок."""
    updated = updated or {}

    def thread(name):
        st = background.state(name)
        return {"next": st.get("next_at"), "error": st.get("last_error"), "error_at": st.get("last_error_at"),
                "alive": st.get("alive")}

    out = {}
    t = thread("stats-refresh")
    out["napp"] = {"title": "Отчёты НАПП", "last": _refresh_state["last"] or updated.get("market_stats"),
                   "next": t["next"], "error": t["error"] or (
                       "ошибка обновления" if any(str(l).startswith("ошибка") for l in _refresh_state["log"]) else None),
                   "error_at": t["error_at"], "schedule": "раз в сутки"}
    for key, title, mod, name in (("stat_uz", "stat.uz (агентство статистики)", "statagency", "stat-agency-refresh"),
                                  ("exchange", "Биржа (uzex.uz)", "uzex", "exchange-refresh")):
        t = thread(name)
        try:
            m = __import__(f"app.{mod}", fromlist=["_state"])
            last, err = m._state.get("last"), m._state.get("error")
        except Exception:
            last, err = None, None
        db_key = "stat_series" if key == "stat_uz" else "exchange_quotes"
        out[key] = {"title": title, "last": last or updated.get(db_key), "next": t["next"],
                    "error": t["error"] or ("ошибка обновления" if err else None), "error_at": t["error_at"],
                    "schedule": "раз в сутки"}
    t = thread("lawwatch")
    try:
        from . import lawwatch
        law = lawwatch.last_status()
        nxt = _next_daily(lawwatch.CHECK_HOUR, lawwatch.CHECK_MINUTE)
    except Exception:
        law, nxt = {}, None
    out["laws"] = {"title": "Законодательство (lex.uz)", "last": law.get("last") or updated.get("lawwatch"),
                   "next": nxt, "error": t["error"] or ("ошибка прохода" if law.get("error") else None),
                   "error_at": t["error_at"], "schedule": law.get("schedule") or "раз в сутки"}
    t = thread("market-knowledge")
    try:
        from . import market_knowledge
        mk = market_knowledge.status()
    except Exception:
        mk = {}
    out["market_knowledge"] = {"title": "Знания о рынке (заметки и факты)", "last": mk.get("last_built"),
                               "next": "после обновления НАПП, если данные изменились",
                               "slice": mk.get("slice"), "where": mk.get("where"),
                               "error": t["error"] or mk.get("last_error"),
                               "error_at": t["error_at"] or mk.get("last_error_at"),
                               "schedule": mk.get("schedule")}
    t = thread("competitors-refresh")
    try:
        from . import competitors
        cs = competitors.status()
    except Exception:
        cs = {}
    out["competitors"] = {"title": "Документы конкурентов", "last": cs.get("last_run"), "next": cs.get("next_run"),
                          "checked": cs.get("checked"), "changed": cs.get("changed"),
                          "robots_closed": cs.get("robots_closed"), "errors": cs.get("errors"),
                          "error": t["error"], "error_at": t["error_at"], "schedule": cs.get("schedule")}
    return out


CLASSES_SHOWN, CLASSES_ORDER = db.CLASSES_SHOWN, db.CLASSES_ORDER
REF_SQL = {
    "products": "SELECT p.*, (SELECT group_concat(class_code) FROM product_classes pc WHERE pc.product_code=p.code) AS classes FROM products p ORDER BY code",
    "classes": f"SELECT * FROM classes WHERE {CLASSES_SHOWN} ORDER BY {CLASSES_ORDER}",
    "perils": "SELECT * FROM perils ORDER BY class_code, base_share DESC",
    "coefficients": "SELECT * FROM coefficients ORDER BY factor_code, id",
    "checklists": "SELECT * FROM checklists ORDER BY scope_type, scope_code, id",
    "rules": "SELECT * FROM rules",
    "min_rates": "SELECT m.*, v.level, v.name AS version, v.effective_from FROM min_rates m JOIN tariff_versions v ON v.id=m.tariff_version_id ORDER BY product_code",
}


@app.get("/reference/{name}")
def reference(name: str):
    if name == "min-rates":
        # минимальные ставки страховщика с версиями (app/min_rates.py); путь общий с остальными справочниками
        from . import min_rates
        return min_rates.list_min_rates()
    if name not in REF_SQL:
        raise HTTPException(404, "Нет такого справочника")
    with db.tx() as con:
        return db.rows(con, REF_SQL[name])


# ---------- расчёт ----------

def _probability(con, inp, result: dict, body) -> dict:
    """
    Вероятность подтверждения к итогу расчёта (app/analysis.py). Ничего не сохраняет:
    запись появляется, когда запрос уходит на согласование (app/approvals.assign).
    Ошибка модуля расчёт не отменяет — она идёт в журнал.
    """
    from . import analysis, outcomes
    try:
        res = analysis.probability(
            con, calc=result, valuation=inp.valuation, documents=None, history=None,
            context={"product_code": body.product_code, "branch": getattr(body, "branch", None),
                     "class_code": inp.class_code, "sum_insured": body.sum_insured,
                     "factors": body.factors or {},
                     "franchise": (body.factors or {}).get("franchise")})
        return outcomes.view(res)
    except Exception as e:
        db.audit(con, "api", "вероятность не рассчитана", None, {"ошибка": str(e)})
        return outcomes.empty("модуль вероятности вернул ошибку")


@app.post("/calculate")
def calc(body: CalcIn):
    with db.tx() as con:
        ref = db.load_reference(con)
        inp = to_input(con, body)
        result = calculate(ref, inp)
        return {**result, "probability": _probability(con, inp, result, body)}


@app.post("/requests")
def create_request(body: RequestIn, request: Request = None):
    # кто подал: берём вошедшего из сессии. Нужен роли «сотрудник» — у неё нет ID агента в ЕАИС,
    # а «свои запросы» и доступ к выгрузкам определяются именно по автору.
    author_id = None
    if request is not None:
        try:
            from . import auth as _auth
            u = request.scope.get("surveyor_user")
            if u is None:
                with db.tx() as _c:
                    u = _auth.session_user(_c, _auth.request_token(request))
            author_id = (u or {}).get("id")
        except Exception as e:                 # вход не обязателен для расчёта — ошибку не глотаем молча
            print("создание запроса: автор не определён:", e)
    with db.tx() as con:
        ref = db.load_reference(con)
        inp = to_input(con, body)
        result = calculate(ref, inp)
        agent_id = None
        if body.agent_eais_id:
            a = db.rows(con, "SELECT id FROM agents WHERE eais_id=?", body.agent_eais_id)
            agent_id = a[0]["id"] if a else None
        cur = con.execute(
            "INSERT INTO requests (external_no, branch, product_code, policyholder, beneficiary, agent_id,"
            " created_by_user_id, created_at, status) VALUES (?,?,?,?,?,?,?,?,?)",
            (body.external_no, body.branch, body.product_code, body.policyholder, body.beneficiary, agent_id,
             author_id, db.now(), "посчитан"))
        rid = cur.lastrowid
        cur = con.execute(
            "INSERT INTO objects (request_id, object_type, address, region, seismic_zone, value_amount, sum_insured, franchise, attributes)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, body.object_type, body.address, body.region, body.seismic_zone, body.value_amount, body.sum_insured,
             body.factors.get("franchise"), json.dumps({"factors": body.factors, "term_from": body.term_from,
                                                        "term_to": body.term_to, "insured_person": body.insured_person,
                                                        "credit": body.credit.model_dump() if body.credit else None,
                                                        "premium_paid": body.premium_paid,
                                                        # условия расчёта: без них запрос не пересчитать
                                                        # тем же движком (app/outcomes.py)
                                                        "term_days": body.term_days,
                                                        "payer_type": body.payer_type,
                                                        "takaful": body.takaful,
                                                        "disclosure_done": body.disclosure_done,
                                                        "object_key": body.object_key}, ensure_ascii=False)))
        oid = cur.lastrowid
        for p in result["perils_included"]:
            con.execute("INSERT INTO object_perils VALUES (?,?,1)", (oid, p))
        from . import min_rates as _mrs
        # действующая на сегодня версия уровня «компания», без версий правок по одному продукту
        tv_id = _mrs.current_version(con, "компания")
        r = result["rates"]
        cur = con.execute(
            "INSERT INTO calculations (request_id, object_id, tariff_version_id, net_rate_pct, risk_load_pct, cat_load_pct,"
            " gross_rate_pct, min_rate_pct, applied_rate_pct, premium, verdict, explanation, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, oid, tv_id, r["net_pct"], r["risk_load_pct"], r["cat_load_pct"],
             r["technical_pct"], r["min_pct"], r["applied_pct"], result["premium"], result["verdict"],
             json.dumps({"chain": result["explanation"], "manual": r["manual"], "manual_reason": body.manual_reason},
                        ensure_ascii=False), db.now()))
        cid = cur.lastrowid
        for c in result["checks"]:
            rule = c["rule"] if db.rows(con, "SELECT 1 FROM rules WHERE code=?", c["rule"]) else None
            if rule:
                con.execute("INSERT INTO check_results (calculation_id, rule_code, status, detail) VALUES (?,?,?,?)",
                            (cid, rule, "нарушено" if c["status"] != "ok" else "пройдено", c["title"] + ": " + c["detail"]))
        for t in result["recommendations"]:
            con.execute("INSERT INTO recommendations (calculation_id, kind, text, premium_delta) VALUES (?,?,?,?)",
                        (cid, t["kind"], t["text"], t["premium_delta"]))
        db.audit(con, body.branch or "api", "создан запрос", f"request:{rid}", {"verdict": result["verdict"]})
        return {"request_id": rid, "calculation_id": cid, **result,
                "probability": _probability(con, inp, result, body)}


@app.get("/requests")
def list_requests(status: Optional[str] = None, branch: Optional[str] = None, limit: int = 100,
                  user: dict = Depends(current_user)):
    """Список запросов. Кто не видит всё (сотрудник, агент) — получает только свои: фильтр стоит
    в SQL, чтобы чужая строка не попадала в выборку вообще (docs/Регистрация и роли.md, 6.1 п. 1)."""
    sql = """SELECT r.id, r.external_no, r.branch, r.product_code, r.policyholder, r.created_at, r.status,
                    c.applied_rate_pct, c.gross_rate_pct, c.min_rate_pct, c.premium, c.verdict
             FROM requests r LEFT JOIN calculations c ON c.request_id = r.id WHERE 1=1"""
    args = []
    if not access.sees_all(user):
        sql += access.own_requests_where("r"); args += access.own_requests_args(user)
    if status:
        sql += " AND r.status=?"; args.append(status)
    if branch:
        sql += " AND r.branch=?"; args.append(branch)
    sql += " ORDER BY r.id DESC LIMIT ?"; args.append(limit)
    with db.tx() as con:
        return db.rows(con, sql, *args)


def _card(con, rid: int) -> dict:
    req = db.rows(con, "SELECT * FROM requests WHERE id=?", rid)
    if not req:
        raise HTTPException(404, "Запрос не найден")
    obj = db.rows(con, "SELECT * FROM objects WHERE request_id=?", rid)
    calc = db.rows(con, "SELECT * FROM calculations WHERE request_id=? ORDER BY id DESC LIMIT 1", rid)
    checks = db.rows(con, "SELECT rule_code, status, detail FROM check_results WHERE calculation_id=?",
                     calc[0]["id"]) if calc else []
    recs = db.rows(con, "SELECT kind, text, premium_delta FROM recommendations WHERE calculation_id=?",
                   calc[0]["id"]) if calc else []
    docs = db.rows(con, "SELECT id, doc_name, received, file_path FROM documents WHERE request_id=?", rid)
    from . import outcomes
    return {"request": req[0], "object": obj[0] if obj else None, "calculation": calc[0] if calc else None,
            "checks": checks, "recommendations": recs, "documents": docs,
            "probability": outcomes.summary(con, rid)}


@app.get("/requests/{rid}")
def get_request(rid: int, user: dict = Depends(current_user)):
    """Чужой запрос для сотрудника и агента не существует: 404, а не 403 (6.1 п. 2)."""
    with db.tx() as con:
        access.ensure_can_open(con, user, rid)
        return _card(con, rid)


@app.post("/requests/{rid}/documents")
async def upload_document(rid: int, doc_name: str, file: UploadFile = File(...)):
    with db.tx() as con:
        _card(con, rid)
        folder = UPLOADS / str(rid)
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / Path(file.filename).name
        with dest.open("wb") as f:
            shutil.copyfileobj(file.file, f)
        con.execute("INSERT INTO documents (request_id, doc_name, file_path, received) VALUES (?,?,?,1)",
                    (rid, doc_name, db.stored_path(dest)))
        db.audit(con, "api", "загружен документ", f"request:{rid}", {"doc": doc_name, "file": file.filename})
    return {"ok": True, "stored": db.stored_path(dest)}


# Маршрут POST /requests/{rid}/decision убран 20.09.2026. Он менял статус любого запроса по полю
# «who» из тела — без входа под этим человеком, без роли и без проверки участия, в обход модуля
# согласования (app/approvals.py): решение не попадало ни в request_reviewers, ни в decision_outcomes.
# Решение принимает назначенный согласующий: POST /requests/{id}/decide.


# ---------- аналитика ----------

@app.get("/analytics/summary")
def analytics():
    with db.tx() as con:
        total = con.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        by_verdict = db.rows(con, "SELECT verdict, COUNT(*) n FROM calculations GROUP BY verdict")
        manual = con.execute("SELECT COUNT(*) FROM calculations WHERE explanation LIKE '%\"manual\": true%'").fetchone()[0]
        below = con.execute("SELECT COUNT(*) FROM calculations WHERE applied_rate_pct < gross_rate_pct - 1e-9").fetchone()[0]
        by_product = db.rows(con, """SELECT r.product_code, COUNT(*) n, AVG(c.applied_rate_pct) avg_rate, SUM(c.premium) premium
                                     FROM requests r JOIN calculations c ON c.request_id=r.id GROUP BY r.product_code ORDER BY n DESC""")
        by_branch = db.rows(con, """SELECT r.branch, COUNT(*) n, SUM(CASE WHEN c.applied_rate_pct < c.gross_rate_pct THEN 1 ELSE 0 END) below_tech
                                    FROM requests r JOIN calculations c ON c.request_id=r.id GROUP BY r.branch ORDER BY n DESC""")
        top_rules = db.rows(con, """SELECT rule_code, COUNT(*) n FROM check_results WHERE status='нарушено'
                                    GROUP BY rule_code ORDER BY n DESC LIMIT 10""")
    return {"requests": total, "by_verdict": by_verdict, "manual_rates": manual, "below_technical": below,
            "by_product": by_product, "by_branch": by_branch, "top_violations": top_rules}


# ---------- рыночная статистика (динамика) ----------

@app.get("/market/rows")
def market_rows():
    with db.tx() as con:
        return db.rows(con, """SELECT row_key, row_name, COUNT(*) points, MAX(report_date) last
                               FROM market_stats GROUP BY row_key ORDER BY row_key""")


@app.get("/market/series")
def market_series(row: str = "cls8_9"):
    """Ряд по строке отчёта: нарастающий итог, квартальные приросты и производные показатели."""
    with db.tx() as con:
        pts = db.rows(con, "SELECT * FROM market_stats WHERE row_key=? ORDER BY report_date", row)
        # пометки (01.10.2026): итог комплексного заменён суммой пакетов и т. п. — таблицы может ещё не быть
        try:
            notes = {r["report_date"]: r["note"] for r in db.rows(
                con, "SELECT report_date, note FROM market_stats_notes WHERE row_key=?", row)}
        except Exception:
            notes = {}
    out, prev = [], None
    # срез «01 января» — это итог за ПРЕДЫДУЩИЙ год, поэтому учётный год у него на единицу меньше
    eff_year = lambda d: int(d[:4]) - 1 if d[5:] == "01-01" else int(d[:4])
    for p in pts:
        d = p["report_date"]
        year = d[:4]
        # квартальный прирост — разница нарастающих итогов внутри одного учётного года
        q_prem = q_pay = None
        if prev and eff_year(prev["report_date"]) == eff_year(d) and prev["premiums_ytd"] is not None and p["premiums_ytd"] is not None:
            q_prem = p["premiums_ytd"] - prev["premiums_ytd"]
            q_pay = (p["payouts_ytd"] or 0) - (prev["payouts_ytd"] or 0)
        elif d[5:] in ("03-31", "04-01") and p["premiums_ytd"] is not None:
            q_prem, q_pay = p["premiums_ytd"], p["payouts_ytd"]
        months = {"03-31": 3, "04-01": 3, "07-01": 6, "10-01": 9, "01-01": 12}.get(d[5:], None)
        # 01-01 — это итог за прошлый год: относим к нему
        label = f"{int(year)-1} год" if d[5:] == "01-01" else f"{year} · {months} мес." if months else d
        ann = None
        if p["premiums_ytd"] and p["liabilities"] and months:
            ann = p["premiums_ytd"] * 12 / months / p["liabilities"] * 100    # годовая ставка, %
        out.append({"date": d, "label": label, "premiums_ytd": p["premiums_ytd"], "payouts_ytd": p["payouts_ytd"],
                    "liabilities": p["liabilities"], "q_premiums": q_prem, "q_payouts": q_pay,
                    "loss_ratio": (p["payouts_ytd"] / p["premiums_ytd"] * 100) if p["premiums_ytd"] else None,
                    "annual_rate": ann, "source": p["source_file"], "note": notes.get(d)})
        prev = p
    return {"row": row, "name": pts[0]["row_name"] if pts else row, "points": out}


@app.post("/market/refresh")
def market_refresh():
    threading.Thread(target=_refresh_job, daemon=True, name="stats-refresh-manual").start()
    return {"started": True, "note": "проверяю сайт НАПП и обновляю ряд; результат — в /market/status"}


@app.get("/market/status")
def market_status():
    with db.tx() as con:
        n = con.execute("SELECT COUNT(*) FROM market_stats").fetchone()[0]
        dates = [r["report_date"] for r in db.rows(con, "SELECT DISTINCT report_date FROM market_stats ORDER BY 1")]
    return {"points": n, "dates": dates, "last_refresh": _refresh_state["last"],
            "running": _refresh_state["running"], "log": _refresh_state["log"],
            "schedule": "каждые 24 часа, первая проверка через минуту после старта"}


@app.get("/market/branches")
def market_branches(date: str = "", company: str = "company:INSON AJ"):
    """Обособленные подразделения страховщика по регионам (листы 2.12–2.14 отчёта НАПП) на срез: премии, выплаты,
    договоры, убыточность и средняя премия; рядом — итог компании и рынок региона. Только чтение napp_branches."""
    from . import market_picture as mp
    with db.tx() as con:
        return mp.branches_table(con, date or None, company or mp.INSON_ROW)


@app.get("/market/claims")
def market_claims(region: str = ""):
    """Претензии (НАПП, листы 3.5/3.4/3.2 и 2.10/2.7/2.5) на последний срез: регион против республики и INSON
    против рынка. Только чтение napp_claims."""
    from . import market_picture as mp
    with db.tx() as con:
        return {"region": mp.region_claims(con, region or None), "company": mp.company_claims(con)}


@app.get("/stats", response_class=HTMLResponse)
def stats_page(embed: int = 0):
    return page(web.read_text(ROOT / "app" / "stats.html"), "/stats", bool(embed))


# ---------- офис агентов ----------

AGENTS = [
    ("lead", "Руководитель", "планирует работу, сводит отчёты, ведёт вопросы к заказчику", ["admin"]),
    ("law", "Юрист", "читает законы и акты, извлекает правила проверок, ищет недостающее на lex.uz", ["юрист"]),
    ("data", "Статистик", "забирает отчёты НАПП, ведёт динамику рынка, готовит калибровку", ["агент-статистик", "system"]),
    ("actuary", "Актуарий", "считает ставки, проверки, удержание и ёмкость", ["api", "экран агента"]),
    ("backend", "Разработчик", "база, сервер, интеграция с учётной системой", ["backend"]),
    ("ui", "Дизайнер", "экраны агента и админки, паутина, динамика", ["ui"]),
    ("reviewer", "Контролёр", "проверяет расчёты и отчёты перед сдачей", ["reviewer"]),
]


@app.get("/agents/status")
def agents_status():
    with db.tx() as con:
        log = db.rows(con, "SELECT ts, who, action, entity FROM audit ORDER BY id DESC LIMIT 300")
    out = []
    for code, name, role, whos in AGENTS:
        mine = [l for l in log if l["who"] in whos or (code == "actuary" and (l["action"] or "").startswith("создан запрос"))]
        last = mine[0] if mine else None
        state = "работает" if (code == "data" and _refresh_state["running"]) else ("сделал" if last else "ожидает задачи")
        out.append({"code": code, "name": name, "role": role, "state": state,
                    "last_action": last["action"] if last else None, "last_entity": last["entity"] if last else None,
                    "last_ts": last["ts"] if last else None, "count": len(mine)})
    try:                                   # слежение за законодательством: что у юриста на столе
        from . import lawwatch
        law = lawwatch.last_status()
    except Exception:
        law = None
    if law:
        for a in out:
            if a["code"] == "law":
                if law["running"]:
                    a["state"] = "работает"
                elif law["unseen_events"]:
                    a["state"] = "сделал"
                a["law_unseen"] = law["unseen_events"]
                a["law_changed_acts"] = law["changed_acts"]
    return {"agents": out, "refresh": _refresh_state, "lawwatch": law}


@app.get("/office", response_class=HTMLResponse)
def office_page(embed: int = 0):
    return page(web.read_text(ROOT / "app" / "office.html"), "/office", bool(embed))


# ---------- ёмкость, резервы, удержание ----------

from . import capacity as cap  # noqa: E402


@app.get("/capacity")
def capacity_view():
    with db.tx() as con:
        c = cap.capacity(con)
        c["retention"] = cap.retention_table(con, c["limit_per_risk"]) if c["limit_per_risk"] else []
        return c


class ReserveRow(BaseModel):
    report_date: str
    scope_type: str                            # 'группа' | 'класс' | 'вид' | 'итого'
    scope_code: str
    rnp: float = 0
    rzu: float = 0
    rpnu: float = 0
    stab: float = 0
    cat_reserve: float = 0
    other: float = 0
    base_premium_12m: Optional[float] = None
    source: str = ""


class AssetRow(BaseModel):
    report_date: str
    category: str
    amount: float
    is_liquid: int = 1


class SolvencyRow(BaseModel):
    report_date: str
    own_funds: float
    deductions: float = 0
    premiums_12m: Optional[float] = None
    claims_36m: Optional[float] = None
    claims_36m_net: Optional[float] = None
    min_capital: Optional[float] = None
    top5_liabilities: Optional[float] = None
    source: str = ""


@app.post("/admin/reserves")
def add_reserves(rows_in: list[ReserveRow], user: dict = Depends(require(ADMIN))):
    with db.tx() as con:
        for r in rows_in:
            con.execute("INSERT OR REPLACE INTO reserve_reports VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (r.report_date, r.scope_type, r.scope_code, r.rnp, r.rzu, r.rpnu, r.stab, r.cat_reserve,
                         r.other, r.base_premium_12m, r.source))
        db.audit(con, user["login"], "отчёт о резервах", rows_in[0].report_date if rows_in else None, {"rows": len(rows_in)})
    return {"ok": True, "rows": len(rows_in)}


@app.post("/admin/assets")
def add_assets(rows_in: list[AssetRow], user: dict = Depends(require(ADMIN))):
    with db.tx() as con:
        for r in rows_in:
            con.execute("INSERT OR REPLACE INTO allocated_assets VALUES (?,?,?,?)",
                        (r.report_date, r.category, r.amount, r.is_liquid))
        db.audit(con, user["login"], "выделенные активы", rows_in[0].report_date if rows_in else None, {"rows": len(rows_in)})
    return {"ok": True, "rows": len(rows_in)}


@app.put("/admin/solvency")
def set_solvency(s: SolvencyRow, user: dict = Depends(require(ADMIN))):
    with db.tx() as con:
        con.execute("INSERT OR REPLACE INTO solvency_reports VALUES (?,?,?,?,?,?,?,?,?)",
                    (s.report_date, s.own_funds, s.deductions, s.premiums_12m, s.claims_36m, s.claims_36m_net,
                     s.min_capital, s.top5_liabilities, s.source))
        db.audit(con, user["login"], "платёжеспособность", s.report_date, s.model_dump())
    return {"ok": True}


@app.get("/capacity-page", response_class=HTMLResponse)
def capacity_page(embed: int = 0):
    return page(web.read_text(ROOT / "app" / "capacity.html"), "/capacity-page", bool(embed))


# ---------- админка ----------

class TariffVersion(BaseModel):
    level: str                                 # 'компания' | 'регулятор'
    name: str
    document_ref: str = ""
    effective_from: str                        # ГГГГ-ММ-ДД


class MinRate(BaseModel):
    tariff_version_id: int
    product_code: str
    class_code: Optional[str] = None
    payer_type: Optional[str] = None
    min_rate_pct: float


class Coefficient(BaseModel):
    factor_code: str
    factor_name: str
    class_code: Optional[str] = "8"
    option_code: str
    option_name: str
    multiplier: float
    calibrated: int = 0
    source: str = ""


class Product(BaseModel):
    code: str
    name: str
    classes: list
    rate_text: str = ""
    commission_text: str = ""
    commission_pct: Optional[float] = None
    pricing_mode: str = "ставка"
    min_rate_pct: Optional[float] = None
    tariff_version_id: Optional[int] = None


class Financials(BaseModel):
    report_date: str
    own_funds: float
    reserves: float
    source: str = ""


@app.post("/admin/tariff-versions")
def add_version(v: TariffVersion, user: dict = Depends(require(ADMIN))):
    with db.tx() as con:
        cur = con.execute("INSERT INTO tariff_versions (level, name, document_ref, effective_from) VALUES (?,?,?,?)",
                          (v.level, v.name, v.document_ref, v.effective_from))
        db.audit(con, user["login"], "новая версия тарифов", f"version:{cur.lastrowid}", v.model_dump())
        db.reference_changed(con)        # расчёт должен сразу видеть новое значение
        return {"id": cur.lastrowid}


@app.post("/admin/min-rates")
def add_min_rate(m: MinRate, user: dict = Depends(require(ADMIN))):
    with db.tx() as con:
        con.execute("INSERT INTO min_rates (tariff_version_id, product_code, class_code, payer_type, min_rate_pct) VALUES (?,?,?,?,?)",
                    (m.tariff_version_id, m.product_code, m.class_code, m.payer_type, m.min_rate_pct))
        db.audit(con, user["login"], "минимальная ставка", m.product_code, m.model_dump())
        db.reference_changed(con)        # расчёт должен сразу видеть новое значение
    return {"ok": True}


@app.post("/admin/coefficients")
def add_coefficient(c: Coefficient, user: dict = Depends(require(ADMIN))):
    with db.tx() as con:
        con.execute("DELETE FROM coefficients WHERE factor_code=? AND option_code=? AND class_code IS ?",
                    (c.factor_code, c.option_code, c.class_code))
        con.execute("INSERT INTO coefficients (factor_code,factor_name,class_code,option_code,option_name,multiplier,calibrated,source)"
                    " VALUES (?,?,?,?,?,?,?,?)", (c.factor_code, c.factor_name, c.class_code, c.option_code,
                                                 c.option_name, c.multiplier, c.calibrated, c.source))
        db.audit(con, user["login"], "коэффициент", f"{c.factor_code}/{c.option_code}", c.model_dump())
        db.reference_changed(con)        # расчёт должен сразу видеть новое значение
    return {"ok": True}


@app.post("/admin/products")
def add_product(p: Product, user: dict = Depends(require(ADMIN))):
    with db.tx() as con:
        con.execute("INSERT OR REPLACE INTO products (code,name,rate_text,commission_text,commission_pct,pricing_mode,is_general,status)"
                    " VALUES (?,?,?,?,?,?,0,'тест')", (p.code, p.name, p.rate_text, p.commission_text, p.commission_pct, p.pricing_mode))
        con.execute("DELETE FROM product_classes WHERE product_code=?", (p.code,))
        for i, cl in enumerate(p.classes, start=1):
            con.execute("INSERT INTO product_classes VALUES (?,?,?)", (p.code, cl, i))
        if p.min_rate_pct is not None and p.tariff_version_id:
            con.execute("INSERT INTO min_rates (tariff_version_id, product_code, class_code, payer_type, min_rate_pct) VALUES (?,?,?,?,?)",
                        (p.tariff_version_id, p.code, p.classes[0], None, p.min_rate_pct))
        db.audit(con, user["login"], "продукт", p.code, p.model_dump())
        db.reference_changed(con)        # расчёт должен сразу видеть новое значение
    return {"ok": True, "status": "тест — до утверждения виден только андеррайтеру"}


@app.put("/admin/financials")
def set_financials(f: Financials, user: dict = Depends(require(ADMIN))):
    with db.tx() as con:
        # колонки по имени: с 03.10.2026 в таблице есть разбивка (капитал, резервы брутто/нетто, активы)
        con.execute("INSERT OR REPLACE INTO company_financials (report_date, own_funds, reserves, source, confirmed)"
                    " VALUES (?,?,?,?,?)", (f.report_date, f.own_funds, f.reserves,
                                             f.source or "введено в админке", None))
        db.audit(con, user["login"], "финансовые показатели", f.report_date, f.model_dump())
        db.reference_changed(con)        # расчёт должен сразу видеть новое значение
    return {"ok": True, "risk_limit": 0.2 * (f.own_funds + f.reserves)}


@app.get("/audit")
def audit_log(limit: int = 200):
    with db.tx() as con:
        return db.rows(con, "SELECT * FROM audit ORDER BY id DESC LIMIT ?", limit)


# ---------- экраны ----------

# разделы бокового меню: адрес, полное название, значок и короткая подпись для узкой рейки
SIDEBAR_ITEMS = [("/", "Главная", "⌂", "Главная"), ("/ui", "Новый расчёт", "₌", "Расчёт"),
                 ("/admin", "Запросы и админка", "❑", "Запросы"), ("/portfolio", "Портфель", "▤", "Портфель"),
                 ("/approvals", "Согласования", "✓", "Согл."), ("/tasks-page", "Задачи команде", "✎", "Задачи"),
                 ("/reports-page", "Ежедневный доклад", "▦", "Доклад"), ("/stats", "Динамика рынка", "↗", "Рынок"),
                 ("/capacity-page", "Ёмкость и удержание", "◍", "Ёмкость"), ("/calibration", "Калибровка", "⚖", "Калибр."),
                 ("/graph", "Паутина знаний", "◈", "Паутина"), ("/law-feed", "Законодательство", "§", "Закон"),
                 ("/office", "Офис агентов", "◉", "Офис"),
                 ("/admin/deploy", "Запуск и обслуживание", "⚙", "Запуск"), ("/docs", "API", "⌨", "API")]

# Одна раскладка для всех страниц: слева узкая рейка со значками, по «гамбургеру» — панель с названиями.
# Ту же логику повторяет мини-приложение app/tg.html (там свой файл, отдельно от сервера).
SIDEBAR_CSS = """<style>
:root{--sb-paper:#0F1418;--sb-line:#26303A;--sb-muted:#8E9BA6;--sb-accent:#2ED3A2;--sb-dim:#1E8F70;--sb-rail:60px;--sb-wide:240px}
#side{position:fixed;left:0;top:0;bottom:0;z-index:60;width:calc(var(--sb-rail) + env(safe-area-inset-left));
  background:var(--sb-paper);border-right:1px solid var(--sb-line);display:flex;flex-direction:column;
  overflow-y:auto;overscroll-behavior:contain;-webkit-overflow-scrolling:touch;
  padding:calc(env(safe-area-inset-top) + 6px) 0 calc(env(safe-area-inset-bottom) + 12px) env(safe-area-inset-left);
  font:14px Manrope,system-ui,sans-serif;transition:width .16s ease}
body.side-open #side{width:calc(var(--sb-wide) + env(safe-area-inset-left));box-shadow:0 0 44px rgba(0,0,0,.45)}
body.side-wide #side{box-shadow:none}
#sbScrim{position:fixed;inset:0;z-index:55;background:rgba(0,0,0,.5);opacity:0;pointer-events:none;transition:opacity .16s}
body.side-open:not(.side-wide) #sbScrim{opacity:1;pointer-events:auto}
body.side-wide #sbScrim{display:none}
#side .burger{background:none;border:0;width:100%;min-height:48px;padding:0;color:var(--sb-muted);cursor:pointer;
  display:flex;align-items:center;gap:12px;font:700 12.5px Manrope,system-ui,sans-serif;text-align:left}
#side .burger i{font-style:normal;font-size:20px;line-height:1;flex:none;width:var(--sb-rail);text-align:center}
#side .burger span{display:none}
body.side-open #side .burger span{display:block}
body.side-wide #side .burger{display:none}
#side .brand{display:flex;align-items:center;gap:12px;min-height:46px;color:#E6ECF0;padding:0}
#side .brand i{font-style:normal;flex:none;width:var(--sb-rail);display:grid;place-items:center}
#side .brand i b{width:32px;height:32px;border-radius:50%;background:var(--sb-dim);display:grid;place-items:center;
  font-weight:800;color:#0F1418;font-size:14px}
#side .brand div{display:none;min-width:0}
body.side-open #side .brand div{display:block}
#side .brand-logo{display:inline-flex;align-items:baseline;font:800 20px/1 Manrope,system-ui,sans-serif;letter-spacing:-.02em}
#side .brand-logo b{color:#8EA9FF} #side .brand-logo b + b{color:#3FBE74}
#side .brand-sub{display:block;font:600 10.5px Manrope,system-ui,sans-serif;color:var(--sb-muted);margin-top:3px;white-space:nowrap}
#sb{display:flex;flex-direction:column;gap:2px;padding:8px 0}
#sb a{position:relative;color:var(--sb-muted);text-decoration:none;min-height:52px;padding:6px 1px;font-weight:600;
  display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;line-height:1.1;text-align:center}
#sb a i{font-style:normal;font-size:18px;line-height:1}
#sb a u{text-decoration:none;font-size:9.5px;max-width:var(--sb-rail);padding:0 2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#sb a span{display:none;font-size:13.5px}
#sb a.on{color:var(--sb-accent);background:rgba(46,211,162,.10);box-shadow:inset 3px 0 0 var(--sb-accent)}
#sb a:focus-visible,#side .burger:focus-visible{outline:2px solid var(--sb-accent);outline-offset:-2px}
body.side-open #sb a{flex-direction:row;justify-content:flex-start;gap:12px;min-height:48px;padding:6px 12px 6px 0;text-align:left}
body.side-open #sb a i{flex:none;width:var(--sb-rail);text-align:center}
body.side-open #sb a u{display:none}
body.side-open #sb a span{display:block;flex:1 1 auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
body{padding-left:calc(var(--sb-rail) + env(safe-area-inset-left)) !important;padding-right:env(safe-area-inset-right)}
body.side-wide{padding-left:calc(var(--sb-wide) + env(safe-area-inset-left)) !important}
</style>"""

SIDEBAR_JS = """<script>
(function(){
  var wide = window.matchMedia("(min-width:900px)");
  var burger = document.getElementById("burger"), scrim = document.getElementById("sbScrim");
  function open(on){
    document.body.classList.toggle("side-open", !!on);
    burger.setAttribute("aria-expanded", on ? "true" : "false");
  }
  function sync(){ document.body.classList.toggle("side-wide", wide.matches); open(wide.matches); }
  if (wide.addEventListener) wide.addEventListener("change", sync); else wide.addListener(sync);
  sync();
  burger.addEventListener("click", function(){ open(!document.body.classList.contains("side-open")); });
  scrim.addEventListener("click", function(){ if (!wide.matches) open(false); });
  document.addEventListener("keydown", function(e){ if (e.key === "Escape" && !wide.matches) open(false); });
})();
</script>"""


def sidebar(active: str) -> str:
    links = "".join(
        f'<a href="{h}"{" class=on" if h == active else ""} title="{t}"'
        f'{" aria-current=page" if h == active else ""}>'
        f'<i aria-hidden="true">{ico}</i><u>{short}</u><span>{t}</span></a>'
        for h, t, ico, short in SIDEBAR_ITEMS)
    return (SIDEBAR_CSS
            + '<aside id="side">'
              '<button class="burger" id="burger" type="button" aria-controls="sb" aria-expanded="false">'
              '<i aria-hidden="true">&#9776;</i><span>Свернуть меню</span></button>'
              '<div class="brand"><i aria-hidden="true"><b>S</b></i>'
              '<div><span class="brand-logo"><b>INS</b><b>ON</b></span>'
              '<span class="brand-sub">Сюрвейер</span></div></div>'
              f'<nav id="sb" aria-label="Разделы">{links}</nav></aside>'
              '<div id="sbScrim"></div>'
            + SIDEBAR_JS)


# Встраивание в единую админку (/admin/hub): страница едет в <iframe> на том же домене,
# и собственное меню там лишнее — прячем и общую рейку sidebar(), и свой <aside> страницы,
# и отступ body под рейку. Права это не меняет: guard/access проверяются как обычно.
EMBED_CSS = ("<style id=\"embed-nav-off\">"
             # прячем только меню (.app>aside и общую рейку), но не панели с данными,
             # например aside.summary на экране расчёта
             "#side,#sbScrim,.app>aside{display:none!important}"
             ".app{grid-template-columns:1fr!important}"
             "body{padding-left:0!important;padding-right:0!important}"
             "</style>")


# «Выйти из админки» (22.09.2026): уводит в мини-апп в режиме обычного пользователя.
# Права не меняет — это только вид; мини-апп по ?mode=user прячет админские разделы и показывает
# плашку «Вернуться в админку». Здесь — только /admin/deploy.
EXIT_ADMIN_URL = "/tg?mode=user"
# 06.10.2026: на самой админке (/admin) кнопки нет — там «Выход» из учётной записи; осталась на /admin/deploy.
EXIT_ADMIN_PAGES = ("/admin/deploy",)
EXIT_ADMIN_CSS = (
    "<style id=\"exit-admin-css\">#exitAdmin{position:fixed;top:12px;right:16px;z-index:70;display:inline-flex;"
    "align-items:center;gap:8px;min-height:40px;padding:8px 14px;border-radius:10px;border:1px solid var(--line,#6B7883);"
    "background:var(--card,#161C21);color:var(--ink,#E6ECF0);font:600 13px Manrope,system-ui,sans-serif;"
    "text-decoration:none;box-shadow:0 6px 18px rgba(0,0,0,.25)}"
    "#exitAdmin:hover{background:var(--soft,#1C242B)}#exitAdmin:focus-visible{outline:2px solid #8EA9FF;outline-offset:2px}"
    "header #exitAdmin{position:static;box-shadow:none;margin-right:14px}"
    "@media (max-width:600px){#exitAdmin{top:auto;bottom:62px;right:14px}header #exitAdmin{margin:0 14px 0 0}}</style>")
EXIT_ADMIN_LINK = (f'<a id="exitAdmin" class="btn btn-secondary btn-sm" href="{EXIT_ADMIN_URL}" data-i18n="admin.exit" '
                   'title="Открыть мини-приложение так, как его видит обычный сотрудник">Выйти из админки</a>')
EXIT_ADMIN_HTML = EXIT_ADMIN_CSS + EXIT_ADMIN_LINK


def page(html: str, active: str = "", embed: bool = False) -> str:
    """Одна раскладка для всех экранов.
    embed=True — отдаём без меню (для iframe админки); иначе подставляем общую рейку
    вместо метки <!--SIDEBAR-->, а страницы со своим <aside> остаются как были."""
    if embed:
        return html + EMBED_CSS
    if active in EXIT_ADMIN_PAGES:
        html = html + EXIT_ADMIN_HTML
    if active and "<!--SIDEBAR-->" in html:
        return html.replace("<!--SIDEBAR-->", sidebar(active), 1)
    return html


# Мост UI_BRIDGE убран 20.09.2026: экран /ui (docs/agent_ui.html) сам показывает рынок,
# предупредительные мероприятия, сохраняет запрос и даёт ссылку на PDF.


# Картинки интерфейса (фоны разделов мини-аппа; обложка удалена 28.09.2026) отдаются как есть из app/static.
# Одна строка монтирования; адреса вида /static/bg/calc-640.jpg открыты до входа (app/guard.py, WHITE_PREFIX).
app.mount("/static", StaticFiles(directory=str(ROOT / "app" / "static")), name="static")


@app.get("/theme.js")
def theme_js(request: Request):
    return web.asset_response(request, ROOT / "app" / "theme.js", "application/javascript")


@app.get("/i18n.js")
def i18n_js(request: Request):
    """Словарь интерфейса на странице: выбор языка, подписи по data-i18n, функция T().
    Отдаётся рядом с /theme.js и так же открыт до входа (app/guard.py)."""
    return web.asset_response(request, ROOT / "app" / "i18n.js", "application/javascript")


AGENT_UI = ROOT / "docs" / "agent_ui.html"


def _ui_page(embed: bool) -> str:
    html = web.read_text(AGENT_UI)
    head = ("<!doctype html><html><meta charset='utf-8'>"
            "<link rel='stylesheet' href='https://fonts.googleapis.com/css2?family=Manrope:wght@600;800&display=swap'>")
    bar = "" if embed else sidebar("/ui")
    return page(head + bar + html + "<script src='/theme.js'></script>", embed=embed)


@app.get("/ui", response_class=HTMLResponse)
def ui(embed: int = 0):
    # склейка страницы (300 КБ) — одна на версию файла, а не на каждый запрос
    return web.build(("ui", bool(embed)), [AGENT_UI], lambda: _ui_page(bool(embed)))


ADMIN_HUB = ROOT / "app" / "admin_hub.html"


# Админка — отдельный сайт по адресу /admin (записка заказчика, 06.10.2026): без входа guard отправляет на
# /login?next=/admin, после входа страница входа возвращает сюда. Прежний адрес /admin/hub отдаёт то же самое.
# Старая страница app/admin.html больше не отдаётся: её запросы и справочники — в «Дополнительно».
@app.get("/admin", response_class=HTMLResponse)
@app.get("/admin/", response_class=HTMLResponse)
@app.get("/admin/hub", response_class=HTMLResponse)
@app.get("/admin/hub/", response_class=HTMLResponse)
def admin_hub():
    """Админка: левое меню из пяти пунктов, прежние разделы — в «Дополнительно» (часть открывается в iframe с ?embed=1).
    Файл верстает дизайнер; пока его нет — понятная 404, сервер поднимается как обычно."""
    if not ADMIN_HUB.exists():
        raise HTTPException(404, "страница app/admin_hub.html ещё не сделана")
    return page(web.read_text(ADMIN_HUB), "/admin/hub")


@app.get("/graph", response_class=HTMLResponse)
def graph(embed: int = 0):
    """Паутина знаний в стиле Obsidian: продукты → классы → учётные группы → правила РНП.

    Своего бокового меню страница не имеет, поэтому в режиме встраивания (?embed=1, внутри
    единой админки) убираем только ссылку «← к приложению»: внутри рамки она уводила бы
    пользователя из админки прямо в окне раздела.
    """
    html = web.read_text(ROOT / "docs" / "tariff_web.html")
    back = ('<a href="/stats" style="position:fixed;right:16px;bottom:14px;z-index:9;font:600 13px Manrope,system-ui;'
            'color:#2ED3A2;text-decoration:none;background:#161C21;border:1px solid #26303A;border-radius:999px;padding:7px 13px">← к приложению</a>')
    return "<!doctype html><meta charset='utf-8'>" + html + ("" if embed else back)


# модули, которые делают агенты: портфельный аудит, предложение клиенту, калибровка
for _mod, _name in (("portfolio", "portfolio_router"), ("proposal", "proposal_router"), ("calibration", "calibration_router"),
                    ("history", "history_router"), ("auth", "auth_router"), ("team", "team_router"),
                    ("knowledge", "knowledge_router"), ("office_api", "office_router"),
                    ("photos", "photos_router"), ("valuation", "valuation_router"),
                    ("docparse", "docparse_router"), ("ingest", "ingest_router"),
                    ("statagency", "statagency_router"), ("uzex", "uzex_router"),
                    ("approvals", "approvals_router"), ("lawwatch", "lawwatch_router"),
                    ("llm", "llm_router"), ("deploy", "deploy_router"), ("telegram", "telegram_router"),
                    ("tgbot", "tgbot_router"), ("registration", "registration_router"),
                    ("tg_link", "tg_link_router"), ("login_links", "login_links_router"),
                    ("exports", "exports_router"), ("i18n", "i18n_router"),
                    ("vehicle_class", "vehicle_router"), ("osgor", "osgor_router"), ("finance", "finance_router"),
                    ("risk_api", "risk_router"), ("analysis_docs", "analysis_docs_router"),
                    ("legal", "legal_router"), ("surveyor_chat", "surveyor_chat_router"),
                    ("act", "act_router"), ("min_rates", "min_rates_router"),
                    # админ-панель 02.10.2026: сотрудник вручную, импорт продуктов и страховых случаев
                    ("staff", "staff_router"), ("product_import", "product_import_router"),
                    ("claims_import", "claims_import_router"),
                    # автообновление 03.10.2026: знания о рынке после нового среза НАПП, документы конкурентов
                    ("market_knowledge", "market_knowledge_router"), ("competitors", "competitors_router")):
    try:
        _m = __import__(f"app.{_mod}", fromlist=["router"])
        app.include_router(_m.router)
    except Exception as _e:  # модуль ещё не готов — сервер всё равно поднимается
        print(f"модуль {_mod} не подключён: {_e}")


# Вход через Google (app/google_auth.py): подключаем рядом с остальными входами.
from . import google_auth  # noqa: E402

app.include_router(google_auth.router)


# Единый вход: всё, кроме белого списка, требует сессии (app/guard.py).
# Подключается последним, чтобы закрыть и маршруты модулей выше.
from . import guard  # noqa: E402

guard.install(app)

# Слои поверх guard (последний добавленный — внешний):
#   сжатие gzip — страницы 60–300 КБ уходят в 4–5 раз меньше (важно для мини-аппа в мобильной сети);
#   PDF, XLSX, ZIP и картинки не сжимаем: они уже сжаты, только потратим время;
#   Cache-Control: no-store по умолчанию; единый ответ на необработанную ошибку.
from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES, GZipMiddleware  # noqa: E402

GZIP_SKIP = DEFAULT_EXCLUDED_CONTENT_TYPES + (
    "application/pdf", "application/octet-stream", "image/*",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
app.add_middleware(web.CacheControlMiddleware)
app.add_middleware(web.PageGzipCache, compresslevel=6)     # страницы сжимаются один раз на версию файла
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6, exclude_content_types=GZIP_SKIP)
app.add_middleware(web.ErrorMiddleware)


@app.get("/accumulation")
def accumulation():
    """Накопление страховых сумм по регионам и сейсмозонам против лимита на один риск."""
    with db.tx() as con:
        c = cap.capacity(con)
        by_region = db.rows(con, """SELECT COALESCE(o.region,'не указан') region, COALESCE(o.seismic_zone, 0) zone,
                                    COUNT(*) n, SUM(o.sum_insured) total, MAX(o.sum_insured) largest
                                    FROM objects o GROUP BY region, zone ORDER BY total DESC""")
        by_product = db.rows(con, """SELECT r.product_code, COUNT(*) n, SUM(o.sum_insured) total
                                     FROM objects o JOIN requests r ON r.id=o.request_id GROUP BY r.product_code ORDER BY total DESC""")
    limit = c["limit_per_risk"]
    for r in by_region:
        r["share_of_limit"] = (r["total"] / limit) if limit else None
        r["flag"] = "стоп" if limit and r["largest"] > limit else ("внимание" if limit and r["total"] > limit else "ок")
    return {"limit_per_risk": limit, "top5_limit": c["limit_top5"], "by_region": by_region, "by_product": by_product}


@app.get("/", response_class=HTMLResponse)
def index():
    return web.read_text(ROOT / "app" / "home.html").replace("<!--SIDEBAR-->", sidebar("/"))
