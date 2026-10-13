"""
Биржевые цены УзРТСБ (uzex.uz): выдача, ручное обновление и ежедневное расписание.

Слой загрузки (страницы, robots.txt, разбор, группы товаров, медианы, запись в exchange_quotes) — app/uzex_sources.py,
здесь он только вызывается.

Точки подключения (доступ — app/guard.py):
  GET  /exchange/quotes?group=&q=&days=&page=   — строки как есть, у каждой ссылка на страницу биржи; любой вошедший;
  GET  /exchange/summary?days=30                — медиана цены за единицу по группам, число сделок, дата последней;
  GET  /exchange/status                         — последнее обновление по страницам;
  POST /exchange/refresh                        — внеплановая загрузка, только администратор.
Расписание: поток внутри сервера раз в сутки (первый запуск через 5 минут после старта). Сеть или сайт недоступны —
статус страницы, сервер работает дальше.
"""
from __future__ import annotations

import re as _re
import time
from typing import Optional

from fastapi import APIRouter, HTTPException

from . import background, db
from . import uzex_sources as us

router = APIRouter()

REFRESH_EVERY_SEC = 24 * 3600
FIRST_DELAY_SEC = 300
THREAD = "exchange-refresh"
NOTE = ("Биржевые цены реальных сделок УзРТСБ — для сверки стоимости запасов, грузов и материалов; "
        "стоимость объекта не меняют. Медиана — по сделкам реестра с датой сделки; котировочный лист без даты "
        "в медиану не входит (котировка на дату загрузки).")

_state = {"last": None, "running": False, "statuses": {}, "error": None}


def refresh(pages=None) -> list:
    out = []
    for page in pages or us.DAILY_PAGES:
        try:
            st = us.fetch_and_save(page)
        except Exception as e:                       # запись, файловая система — статус, а не падение
            st = {"page": page, "status": us.STATUS_DOWN, "reason": "%s: %s" % (type(e).__name__, e),
                  "url": us.page_url(page), "fetched_at": db.now(), "rows": 0}
        _state["statuses"][page] = st
        out.append(st)
    _state["last"] = db.now()
    try:
        with db.tx() as con:
            db.audit(con, "агент-биржа", "обновление биржевых цен uzex.uz", "exchange_quotes",
                     {s["page"]: {"статус": s["status"], "строк": s.get("rows"), "сохранено": s.get("saved")}
                      for s in out})
    except Exception:
        pass
    return out


def _refresh_job():
    if _state["running"]:
        return
    _state["running"] = True
    try:
        res = refresh()
        _state["error"] = None
        if all(s["status"] == us.STATUS_DOWN for s in res):
            background.failed(THREAD, kind="источник недоступен")
        else:
            background.ok(THREAD)
    except Exception as e:
        _state["error"] = "%s: %s" % (type(e).__name__, e)
        background.failed(THREAD, e)
    finally:
        _state["running"] = False


def _scheduler():
    background.plan(THREAD, FIRST_DELAY_SEC)
    time.sleep(FIRST_DELAY_SEC)                      # не спорим за сеть с НАПП и stat.uz при старте
    while True:
        _refresh_job()
        background.plan(THREAD, REFRESH_EVERY_SEC)
        time.sleep(REFRESH_EVERY_SEC)


def start_scheduler():
    background.start(THREAD, _scheduler)


def last_statuses() -> dict:
    return {"last": _state["last"], "error": _state["error"], "statuses": dict(_state["statuses"])}


# --------------------------------------------------------------------------- #
# Точки подключения
# --------------------------------------------------------------------------- #

@router.get("/exchange/quotes")
def get_quotes(group: Optional[str] = None, q: Optional[str] = None, days: Optional[int] = None,
               page: Optional[str] = None, limit: int = 200, offset: int = 0):
    if group and group not in us.GROUPS:
        raise HTTPException(422, "группа «%s» не знакома; есть: %s" % (group, ", ".join(us.GROUPS)))
    if page and page not in us.PAGES:
        raise HTTPException(422, "страница «%s» не знакома; есть: %s" % (page, ", ".join(us.PAGES)))
    if days is not None and not 1 <= days <= 3650:
        raise HTTPException(422, "days — от 1 до 3650")
    limit = max(1, min(int(limit), 1000))
    with db.tx() as con:
        res = us.quotes(con, group=group, q=(q or "")[:100] or None, days=days, page=page, limit=limit,
                        offset=max(0, int(offset)))
    res.update(source=us.SOURCE, source_name=us.SOURCE_NAME, note=NOTE,
               pages={k: {"title": v["title"], "url": us.BASE + v["path"], "dated": v["dated"],
                          "price_basis": v["price_basis"]} for k, v in us.PAGES.items()})
    return res


@router.get("/exchange/summary")
def get_summary(days: int = 30, group: Optional[str] = None):
    if not 1 <= days <= 3650:
        raise HTTPException(422, "days — от 1 до 3650")
    with db.tx() as con:
        items = us.summary(con, days=days, groups=[group] if group else None)
        last = us.last_trade_date(con)
    return {"days": days, "items": items, "last_trade_date": last, "available": bool(items),
            "source": us.SOURCE, "source_name": us.SOURCE_NAME, "url": us.BASE + us.PAGES["List"]["path"],
            "formula": "медиана(сумма сделки / количество) по сделкам реестра за период; кг приведены к тоннам",
            "limitations": ["цены биржи — оптовые, франко-склад продавца, без доставки и наценки",
                            "в одной группе разные марки и сорта (например, разные виды дизельного топлива)",
                            "сделка учитывается один раз, хотя видна на сайте около недели",
                            "котировочный лист без даты в медиану не входит"],
            "note": NOTE}


@router.get("/exchange/status")
def get_status():
    with db.tx() as con:
        try:
            r = con.execute("SELECT COUNT(*), MAX(fetched_at), MAX(trade_date) FROM exchange_quotes").fetchone()
            rows, fetched, trade = r[0], r[1], r[2]
        except Exception:
            rows, fetched, trade = 0, None, None
    return {"rows": rows, "last_fetched_at": fetched, "last_trade_date": trade, **last_statuses(),
            "daily_pages": list(us.DAILY_PAGES), "user_agent": us.USER_AGENT}


@router.post("/exchange/refresh")
def post_refresh(page: Optional[str] = None):
    """Внеплановая загрузка (администратор). Реестр сделок — до 15 листов по 1 000 строк, пауза 2 с между листами."""
    if page and page not in us.PAGES:
        raise HTTPException(404, "страница «%s» не зарегистрирована" % page)
    res = refresh([page] if page else None)
    return {"обновлено страниц": len(res), "когда": _state["last"], "страницы": res}


# --------------------------------------------------------------------------- #
# VWAP — средневзвешенная по объёму цена сделок (ТЗ 13.10.2026, п. 6.1: оценка грузов и запасов кл. 7 и 9)
# --------------------------------------------------------------------------- #

def unit_norm(unit: str) -> tuple:
    """Единица ввода → (приведённая единица биржи, во сколько раз умножить количество). «кг» → («т», 0,001)."""
    u = str(unit or "").strip().lower().rstrip(".")
    to, k = us.UNITS.get(u, (u, 1.0))
    return to, 1.0 / k


# Форвардные и фьючерсные контракты в реестре: «сумма сделки» у них — не стоимость товара (проверено 13.10.2026 на
# реестре uzex.uz: «Дизельное топливо ЕВРО Л-К-4 (Форвард)» 600 т — 97,8 млн сум, т. е. 163 тыс. сум/т, синтетическое
# GTL (Форвард) 9 600 т — 1,81 млн сум/т при спотовой цене ~17 млн сум/т). В оценку стоимости не идут.
FORWARD_RX = _re.compile(r"форвард|forward|forvard|фьючерс|fyuchers|futures", _re.I)

# Подвиды внутри группы: если в описании груза назван подвид, а сделок по нему не меньше min_deals — выборка сужается
# до него (пшеница не смешивается с мукой, ячменём и зерноотходами). (подсказка в тексте, название сделки, исключить).
SUBTYPES = {
    "grain": [("пшениц|bugʻdoy|bug'doy", r"пшениц", r"отход|мук|отруб"),
              (r"мук|\bun\b", r"мук", r"отход"),
              ("ячмен", r"ячмен", r"отход"),
              ("кукуруз", r"кукуруз", r"отход|масл"),
              (r"\bрис\b", r"\bрис\b", r"отход")],
    "diesel": [("синтетич|gtl", r"синтетич|gtl", "")],
    "petrol": [(r"аи-?80|а-?80", r"80", ""), (r"аи-?92|а-?92", r"92", ""), (r"аи-?95|а-?95", r"95", ""),
               (r"аи-?98|а-?98", r"98", "")],
}


def subtype_filter(group: str, hint: Optional[str]) -> Optional[tuple]:
    """Подвид по тексту груза → (подсказка, regex названия, regex исключений) или None."""
    h = str(hint or "")
    for hint_rx, name_rx, excl in SUBTYPES.get(group, []):
        if h and _re.search(hint_rx, h, _re.I):
            return hint_rx, name_rx, excl
    return None


def _quantile(xs: list, p: float) -> float:
    pos = p * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def vwap(con, group: str, unit: str, days: int = 30, as_of: Optional[str] = None, iqr_k: float = 1.5,
         hint: Optional[str] = None, min_deals: int = 3) -> dict:
    """
    Биржевая цена за приведённую единицу по сделкам реестра (exchange_quotes, только с датой сделки и в сумах)
    группы group за days дней до as_of. Сделка учитывается один раз (последняя загрузка).

    Порядок — одна выборка для цены, диапазона и надёжности:
      1) цена сделки p = сумма сделки / количество, приведённая к единице биржи (кг → т: ×1000), количество q — в той
         же единице (кг → т: ÷1000);
      2) форварды и фьючерсы исключаются (сумма в реестре — не стоимость товара);
      3) подвид из hint («пшеница», «АИ-92»), если по нему не меньше min_deals сделок;
      4) выбросы по цене вне [Q1 − k·IQR; Q3 + k·IQR] исключаются ДО взвешивания;
      5) по оставшимся: VWAP = Σ(p·q) / Σq, медиана, Q1, Q3 (без весов).
    Нет таблицы или сделок — deals = 0.
    """
    from datetime import date as _d, timedelta as _td
    end = as_of or _d.today().isoformat()
    start = (_d.fromisoformat(end) - _td(days=int(days))).isoformat()
    to, qty_factor = unit_norm(unit)
    out = {"group": group, "unit": to, "qty_factor": qty_factor, "deals": 0, "vwap": None, "median": None,
           "q1": None, "q3": None, "volume": 0.0, "from": start, "to": end, "days": int(days),
           "deals_raw": 0, "excluded_forward": 0, "excluded_subtype": 0, "excluded_iqr": 0, "subtype": None,
           "iqr_k": float(iqr_k), "fences": None, "url": us.BASE + us.PAGES["List"]["path"]}
    try:
        rows = db.rows(con, "SELECT name, unit, unit_norm, lot_qty, price_unit_norm, trade_date, page FROM (%s) "
                            "WHERE grp=? AND unit_norm=? AND trade_date > ? AND trade_date <= ? "
                            "AND price_unit_norm > 0 AND lot_qty > 0 AND currency='UZS'" % us._DEALS_SQL,
                       group, to, start, end)
    except Exception:
        return out
    deals = []
    for r in rows:
        k = us.UNITS.get(str(r["unit"] or "").strip().lower(), (r["unit_norm"], 1.0))[1]
        deals.append({"name": str(r["name"] or ""), "p": float(r["price_unit_norm"]), "q": float(r["lot_qty"]) / k,
                      "date": r["trade_date"]})
    out["deals_raw"] = len(deals)
    spot = [d for d in deals if not FORWARD_RX.search(d["name"])]
    out["excluded_forward"] = len(deals) - len(spot)
    sub = subtype_filter(group, hint)
    if sub:
        _h, name_rx, excl = sub
        narrow = [d for d in spot if _re.search(name_rx, d["name"], _re.I)
                  and not (excl and _re.search(excl, d["name"], _re.I))]
        if len(narrow) >= int(min_deals):
            out["excluded_subtype"] = len(spot) - len(narrow)
            out["subtype"] = name_rx
            spot = narrow
    if not spot:
        return out
    xs = sorted(d["p"] for d in spot)
    q1, q3 = _quantile(xs, 0.25), _quantile(xs, 0.75)
    lo_f, hi_f = q1 - iqr_k * (q3 - q1), q3 + iqr_k * (q3 - q1)
    kept = [d for d in spot if lo_f - 1e-6 <= d["p"] <= hi_f + 1e-6]
    out["excluded_iqr"] = len(spot) - len(kept)
    out["fences"] = [round(lo_f, 2), round(hi_f, 2)]
    q = sum(d["q"] for d in kept)
    if not kept or q <= 0:
        return out
    ys = sorted(d["p"] for d in kept)
    out.update(deals=len(kept), vwap=sum(d["p"] * d["q"] for d in kept) / q, median=_quantile(ys, 0.5),
               q1=_quantile(ys, 0.25), q3=_quantile(ys, 0.75), volume=round(q, 6),
               first_date=min(d["date"] for d in kept), last_date=max(d["date"] for d in kept))
    return out
