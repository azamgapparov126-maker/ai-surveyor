"""
Оценка стоимости объекта страхования: хранение справочников и сама методика.

Хранение (каркас разработчика): нормы износа, настройки оценки, записи оценок и источников,
подтверждение оценки андеррайтером.

Методика (актуарий, METHOD_VERSION):
  шаг 1 — рынок: объявления с площадок, медиана и квартили, скидка на торг;
  шаг 2 — только авто, если рынка нет: письмо официальному дилеру (текст, без выдуманных цен);
  шаг 3 — резерв: линейный износ от даты ввода в эксплуатацию, остаточный минимум, поправка по пробегу;
  шаг 4 — сверка расчёта с ценой, заявленной агентом, по порогу расхождения;
  шаг 5 — привязка к движку: страховая сумма сверяется с последней оценкой по объекту.

Сбор объявлений — в app/valuation_sources.py (статистик). Здесь он только вызывается;
если модуля нет, все источники честно получают статус «источник недоступен».

Все проценты — экспертные до калибровки (calibrated = 0), как требует правило 7 проекта.
Правовая рамка — docs/Оценка стоимости объекта.md: ГК ст. 932 (право страховщика на оценку),
ст. 934 (стоимость — в месте нахождения в день заключения договора), ст. 935 (стоимость
определяется соглашением сторон), ст. 936 (недострахование), ст. 938 (превышение).
"""
import json
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import auth, db

try:                                    # адаптеры площадок пишет статистик; их может ещё не быть
    from . import valuation_sources as vs
except ImportError:                     # pragma: no cover
    vs = None

router = APIRouter()

# Версия методики. Меняется при любой правке формул: старая оценка должна воспроизводиться
# теми же правилами, что были в день расчёта (правило проекта 9).
METHOD_VERSION = "1.0"
METHODS = ("объявления", "износ", "смешанный", "дилер")
# Статусы источников: первые четыре — исторические названия каркаса, вторые — контракт адаптеров
# (app/valuation_sources.py). Принимаем оба и приводим к одному виду при сохранении.
SOURCE_STATUSES = ("ок", "нет данных", "источник недоступен", "запрещено robots.txt",
                   "ok", "нет объявлений")
STATUS_ALIAS = {"ok": "ок", "нет объявлений": "нет данных"}

# Настройки, без которых модуль не считает. Значения ставятся в tools/db_build.py.
SETTING_SPREAD = "spread_threshold_pct"        # порог расхождения заявленной и расчётной стоимости
SETTING_MILEAGE = "annual_mileage_norm_km"     # нормативный годовой пробег для авто
SETTING_MIN_ADS = "market_min_ads"             # сколько объявлений нужно, чтобы верить рынку
SETTING_WINDOW = "market_window_months"        # окно свежести объявлений
SETTING_BARGAIN = "bargain_discount_pct"       # скидка на торг: объявление — цена предложения
SETTING_MILEAGE_SENS = "mileage_sensitivity"   # чувствительность стоимости к отклонению пробега
SETTING_MILEAGE_LIMIT = "mileage_adj_limit_pct"  # предел поправки по пробегу вверх и вниз
SETTING_EH_NORM = "annual_engine_hours_norm"   # нормативная наработка спецтехники за год, моточасы
SETTING_FX_SOURCE = "fx_source"                # откуда берём курс доллара для цен в у.е.
SETTING_FX_MANUAL = "fx_rate_manual"           # ручной курс заказчика (пусто — берём курс ЦБ РУз)

# Значения по умолчанию — на случай, если настройки ещё нет в базе. Все экспертные (calibrated = 0).
SETTING_DEFAULTS = {
    SETTING_SPREAD: 15.0,
    SETTING_MILEAGE: 20000.0,
    SETTING_MIN_ADS: 5.0,
    SETTING_WINDOW: 6.0,
    SETTING_BARGAIN: 5.0,
    SETTING_MILEAGE_SENS: 0.15,
    SETTING_MILEAGE_LIMIT: 15.0,
    # Спецтехника ходит не по километрам, а по моточасам. 1500 моточасов в год — экспертный
    # ориентир односменной работы (примерно 8 часов × 190 рабочих дней с поправкой на простои),
    # calibrated = 0: своей статистики по парку у компании ещё нет, цифру надо подтвердить.
    SETTING_EH_NORM: 1500.0,
}

# Курс валюты. Часть объявлений (avtoelon.uz и др.) публикуется в у.е. — без курса такие цены
# выпадают из медианы и рыночная ветка не работает. Источник курса по умолчанию — официальный
# открытый JSON ЦБ РУз на дату заключения договора; заказчик может поставить свой курс в
# настройке fx_rate_manual. Выбор источника с заказчиком ещё не согласован — вопрос 33
# в docs/Открытые вопросы.md открыт, поэтому в объяснении всегда стоит оговорка.
FX_SOURCE_DEFAULT = "ЦБ РУз на дату заключения"
FX_DISCLAIMER = ("источник курса подлежит подтверждению заказчиком "
                 "(вопрос 33 в docs/Открытые вопросы.md пока открыт)")


# ---------- нормы износа ----------

class Norm(BaseModel):
    code: str
    name: str
    rate_pct: float
    residual_min_pct: Optional[float] = None
    calibrated: int = 0
    source: Optional[str] = None
    note: Optional[str] = None


@router.get("/valuation/norms")
def get_norms():
    with db.tx() as con:
        return db.rows(con, "SELECT * FROM depreciation_norms ORDER BY code")


@router.post("/valuation/norms")
def save_norm(n: Norm, user: dict = Depends(auth.require("админ"))):
    """Создать или изменить норму износа. Правка вручную снимает отметку калибровки."""
    if not (0 <= n.rate_pct <= 100):
        raise HTTPException(400, "Процент износа должен быть от 0 до 100")
    if n.residual_min_pct is not None and not (0 <= n.residual_min_pct <= 100):
        raise HTTPException(400, "Остаточный минимум должен быть от 0 до 100")
    with db.tx() as con:
        old = db.rows(con, "SELECT * FROM depreciation_norms WHERE code=?", n.code)
        con.execute("""INSERT INTO depreciation_norms (code,name,rate_pct,residual_min_pct,calibrated,source,note)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(code) DO UPDATE SET name=excluded.name, rate_pct=excluded.rate_pct,
                         residual_min_pct=excluded.residual_min_pct, calibrated=excluded.calibrated,
                         source=excluded.source, note=excluded.note""",
                    (n.code, n.name, n.rate_pct, n.residual_min_pct, int(n.calibrated), n.source, n.note))
        db.audit(con, user["login"], "изменена норма износа" if old else "добавлена норма износа",
                 f"depreciation_norms:{n.code}",
                 {"было": old[0]["rate_pct"] if old else None, "стало": n.rate_pct})
    return {"ok": True, "code": n.code}


@router.delete("/valuation/norms/{code}")
def delete_norm(code: str, user: dict = Depends(auth.require("админ"))):
    with db.tx() as con:
        if not db.rows(con, "SELECT code FROM depreciation_norms WHERE code=?", code):
            raise HTTPException(404, "Норма не найдена")
        con.execute("DELETE FROM depreciation_norms WHERE code=?", (code,))
        db.audit(con, user["login"], "удалена норма износа", f"depreciation_norms:{code}")
    return {"ok": True, "code": code}


# ---------- настройки ----------

class Setting(BaseModel):
    key: str
    value: str
    name: Optional[str] = None
    unit: Optional[str] = None
    calibrated: int = 0
    source: Optional[str] = None
    note: Optional[str] = None


@router.get("/valuation/settings")
def get_settings():
    with db.tx() as con:
        return db.rows(con, "SELECT * FROM valuation_settings ORDER BY key")


@router.post("/valuation/settings")
def save_setting(s: Setting, user: dict = Depends(auth.require("админ"))):
    with db.tx() as con:
        old = db.rows(con, "SELECT * FROM valuation_settings WHERE key=?", s.key)
        if not old and not s.name:
            raise HTTPException(400, "Для новой настройки нужно название")
        name = s.name or old[0]["name"]
        con.execute("""INSERT INTO valuation_settings (key,value,name,unit,calibrated,source,note)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(key) DO UPDATE SET value=excluded.value, name=excluded.name,
                         unit=excluded.unit, calibrated=excluded.calibrated,
                         source=excluded.source, note=excluded.note""",
                    (s.key, str(s.value), name, s.unit or (old[0]["unit"] if old else None),
                     int(s.calibrated), s.source, s.note))
        db.audit(con, user["login"], "изменена настройка оценки", f"valuation_settings:{s.key}",
                 {"было": old[0]["value"] if old else None, "стало": str(s.value)})
    return {"ok": True, "key": s.key}


def setting(con, key: str, default=None):
    """Значение настройки строкой. Разбор типа — на стороне вызывающего."""
    r = db.rows(con, "SELECT value FROM valuation_settings WHERE key=?", key)
    return r[0]["value"] if r else default


# ---------- оценки ----------

class SourceIn(BaseModel):
    source: str
    status: str
    url: Optional[str] = None
    ads_count: Optional[int] = None
    median: Optional[float] = None
    q1: Optional[float] = None
    q3: Optional[float] = None
    samples: Optional[list] = None
    fetched_at: Optional[str] = None


class ValuationIn(BaseModel):
    request_id: Optional[int] = None
    object_type: Optional[str] = None
    params: Optional[dict] = None
    declared_value: Optional[float] = None
    ai_value: Optional[float] = None
    method: Optional[str] = None
    method_version: Optional[str] = None
    spread: Optional[dict] = None
    explanation: Optional[dict] = None
    sources: List[SourceIn] = []
    created_by: Optional[str] = "api"


@router.post("/valuations")
def create_valuation(v: ValuationIn):
    """Сохранить результат оценки. Считать модуль пока не умеет — цифры приходят извне."""
    if v.method and v.method not in METHODS:
        raise HTTPException(400, f"Метод должен быть одним из: {', '.join(METHODS)}")
    for s in v.sources:
        if s.status not in SOURCE_STATUSES:
            raise HTTPException(400, f"Статус источника должен быть одним из: {', '.join(SOURCE_STATUSES)}")
    js = lambda x: json.dumps(x, ensure_ascii=False) if x is not None else None
    with db.tx() as con:
        if v.request_id and not db.rows(con, "SELECT id FROM requests WHERE id=?", v.request_id):
            raise HTTPException(404, "Запрос не найден")
        cur = con.execute("""INSERT INTO valuations (created_at,created_by,request_id,object_type,params_json,
                             declared_value,ai_value,method,method_version,spread_json,explanation)
                             VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                          (db.now(), v.created_by, v.request_id, v.object_type, js(v.params),
                           v.declared_value, v.ai_value, v.method, v.method_version or METHOD_VERSION,
                           js(v.spread), js(v.explanation)))
        vid = cur.lastrowid
        for s in v.sources:
            con.execute("""INSERT INTO valuation_sources (valuation_id,source,status,url,ads_count,median,q1,q3,
                           samples_json,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (vid, s.source, STATUS_ALIAS.get(s.status, s.status), s.url, s.ads_count,
                         s.median, s.q1, s.q3,
                         js(s.samples), s.fetched_at or db.now()))
        db.audit(con, v.created_by or "api", "сохранена оценка стоимости", f"valuation:{vid}",
                 {"request_id": v.request_id, "ai_value": v.ai_value, "declared": v.declared_value})
    return {"ok": True, "id": vid}


@router.get("/valuations")
def list_valuations(request_id: int = None, limit: int = 50):
    with db.tx() as con:
        if request_id:
            return db.rows(con, "SELECT * FROM valuations WHERE request_id=? ORDER BY id DESC LIMIT ?",
                           request_id, limit)
        return db.rows(con, "SELECT * FROM valuations ORDER BY id DESC LIMIT ?", limit)


@router.get("/valuations/{vid}")
def get_valuation(vid: int):
    with db.tx() as con:
        r = db.rows(con, "SELECT * FROM valuations WHERE id=?", vid)
        if not r:
            raise HTTPException(404, "Оценка не найдена")
        v = r[0]
        v["sources"] = db.rows(con, "SELECT * FROM valuation_sources WHERE valuation_id=? ORDER BY id", vid)
    return v


class Confirm(BaseModel):
    who: str
    value: Optional[float] = None       # андеррайтер может поставить свою цифру


@router.post("/valuations/{vid}/confirm")
def confirm_valuation(vid: int, c: Confirm):
    with db.tx() as con:
        if not db.rows(con, "SELECT id FROM valuations WHERE id=?", vid):
            raise HTTPException(404, "Оценка не найдена")
        if c.value is not None:
            con.execute("UPDATE valuations SET ai_value=?, confirmed_by_underwriter=?, confirmed_at=? WHERE id=?",
                        (c.value, c.who, db.now(), vid))
        else:
            con.execute("UPDATE valuations SET confirmed_by_underwriter=?, confirmed_at=? WHERE id=?",
                        (c.who, db.now(), vid))
        db.audit(con, c.who, "оценка подтверждена", f"valuation:{vid}", {"value": c.value})
    return {"ok": True, "id": vid}


# =========================================================================== #
#                            М Е Т О Д И К А                                   #
# =========================================================================== #
#
# Ни одно число в методике не взято из статистики компании: выгрузок по убыткам и продажам
# ещё нет. Поэтому все настройки и остаточные минимумы помечены calibrated = 0 и попадают
# в объяснение отдельной строкой «экспертно, не калибровано».


# ---------- чтение справочников ----------

def num_setting(con, key: str) -> dict:
    """
    Числовая настройка оценки: значение, откуда взято и калибровано ли.
    Если настройки нет в базе — берём значение по умолчанию и честно это показываем.
    """
    r = db.rows(con, "SELECT * FROM valuation_settings WHERE key=?", key)
    if r:
        try:
            return {"key": key, "value": float(str(r[0]["value"]).replace(",", ".")),
                    "name": r[0]["name"], "unit": r[0]["unit"],
                    "calibrated": int(r[0]["calibrated"] or 0), "from": "valuation_settings"}
        except ValueError:
            pass
    return {"key": key, "value": float(SETTING_DEFAULTS.get(key, 0)), "name": key, "unit": None,
            "calibrated": 0, "from": "значение по умолчанию в коде (настройки нет в базе)"}


def norm_by_code(con, code: str) -> dict:
    """Норма износа из таблицы depreciation_norms. В коде норм нет — только в базе."""
    r = db.rows(con, "SELECT * FROM depreciation_norms WHERE code=?", code)
    if not r:
        raise HTTPException(404, f"Норма износа «{code}» не найдена в справочнике depreciation_norms")
    return r[0]


# ---------- служебное: даты, квантили ----------

def to_date(value) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()[:10]
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise HTTPException(400, f"Дата «{value}» непонятна: нужен вид ГГГГ-ММ-ДД или ДД.ММ.ГГГГ")


def years_between(start, end) -> float:
    """
    Дробное число лет по календарю: полные годы плюс доля года до даты договора.
    2016-01-01 → 2026-01-01 даёт ровно 10.0 (а не 10,0014, как при делении дней на 365).
    """
    d1, d2 = to_date(start), to_date(end)
    if d1 is None or d2 is None:
        raise HTTPException(400, "Для расчёта износа нужны дата ввода в эксплуатацию и дата договора")
    if d2 <= d1:
        return 0.0
    y = d2.year - d1.year

    def anniv(n):
        try:
            return d1.replace(year=d1.year + n)
        except ValueError:              # 29 февраля
            return d1.replace(year=d1.year + n, day=28)
    if anniv(y) > d2:
        y -= 1
    a, b = anniv(y), anniv(y + 1)
    return y + (d2 - a).days / (b - a).days


def quantiles(values: list) -> tuple:
    """(q1, медиана, q3) линейной интерполяцией. Тот же способ, что у адаптеров площадок."""
    xs = sorted(float(v) for v in values if v is not None)
    if not xs:
        return None, None, None

    def q(p):
        if len(xs) == 1:
            return xs[0]
        pos = p * (len(xs) - 1)
        lo = int(pos)
        hi = min(lo + 1, len(xs) - 1)
        return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)
    return q(0.25), q(0.5), q(0.75)


def months_back(as_of: date, months: int) -> date:
    """Дата на N месяцев раньше: начало окна свежести объявлений."""
    y, m = as_of.year, as_of.month - int(months)
    while m <= 0:
        m += 12
        y -= 1
    leap = 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28
    d = min(as_of.day, [31, leap, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
    return date(y, m, d)


def _src_dict(r) -> dict:
    """Результат адаптера (dataclass или словарь) — к словарю."""
    if hasattr(r, "to_dict"):
        return r.to_dict()
    return dict(r)


# ---------- шаг 1: рынок ----------

def market_aggregate(results: list, as_of, min_ads: float, bargain_pct: float,
                     window_months: int = 6) -> dict:
    """
    Свод по объявлениям всех площадок.

    Формула:
        медиана и квартили Q1/Q3 — по объединённому списку цен в сумах;
        рыночная стоимость = медиана × (1 − скидка на торг / 100);
        вилка = [Q1 × (1 − торг), Q3 × (1 − торг)].

    Скидка на торг нужна потому, что объявление — цена предложения, а не рыночная стоимость
    (Закон об оценочной деятельности, ст. 7; docs/Оценка стоимости объекта.md).
    Рынку верим только если объявлений с ценой не меньше min_ads.
    """
    as_of = to_date(as_of) or date.today()
    edge = months_back(as_of, int(window_months))
    prices, samples, srcs, notes = [], [], [], []
    for r in results:
        d = _src_dict(r)
        status = d.get("status") or "источник недоступен"
        status = STATUS_ALIAS.get(status, status)
        srcs.append({"source": d.get("source"), "status": status,
                     "reason": d.get("reason") or "", "url": d.get("url") or "",
                     "ads_count": d.get("ads_count") or 0, "median": d.get("median"),
                     "q1": d.get("q1"), "q3": d.get("q3"),
                     "samples": [dict(x) if isinstance(x, dict) else _src_dict(x)
                                 for x in (d.get("samples") or [])],
                     "fetched_at": d.get("fetched_at")})
        if status != "ок":
            notes.append(f"{d.get('source')}: {status}"
                         + (f" ({d.get('reason')})" if d.get("reason") else ""))
            continue
        prices += [float(p) for p in (d.get("prices") or []) if p]
        for x in (d.get("samples") or []):
            xd = dict(x) if isinstance(x, dict) else _src_dict(x)
            pub = xd.get("published")
            try:
                stale = bool(pub) and to_date(pub) < edge
            except HTTPException:
                stale = False
            if stale:
                notes.append(f"{d.get('source')}: объявление от {pub} старше "
                             f"{int(window_months)} мес. — в примеры не берём")
                continue
            xd["source"] = d.get("source")
            samples.append(xd)

    q1, med, q3 = quantiles(prices)
    k = 1 - float(bargain_pct) / 100
    enough = len(prices) >= int(min_ads)
    out = {"ads_count": sum(s["ads_count"] for s in srcs), "prices_count": len(prices),
           "q1": q1, "median": med, "q3": q3, "bargain_pct": bargain_pct,
           "value": round(med * k, 2) if med is not None else None,
           "spread": ({"min": round(q1 * k, 2), "median": round(med * k, 2), "max": round(q3 * k, 2)}
                      if med is not None else None),
           "samples": samples, "sources": srcs,
           "enough": bool(enough and med is not None),
           "window_months": int(window_months), "window_from": edge.isoformat(),
           "as_of": as_of.isoformat(), "notes": notes}
    if not prices:
        out["reason"] = ("ни одна площадка не дала цен в сумах: "
                         + ("; ".join(notes) if notes else "объявлений по объекту нет"))
    elif not enough:
        out["reason"] = (f"объявлений с ценой {len(prices)}, а для доверия рынку нужно "
                         f"не меньше {int(min_ads)} (экспертный порог, calibrated = 0)")
    return out


def fx_rate(con, as_of) -> dict:
    """
    Курс доллара для перевода цен объявлений в сумы.

    Порядок: ручной курс заказчика (настройка fx_rate_manual) → официальный курс ЦБ РУз
    на дату заключения договора (открытый JSON cbu.uz, vs.cbu_usd_rate). Если курс получить
    не удалось (нет сети, нет модуля адаптеров), возвращается rate = None: долларовые цены
    в медиану не берутся, и это пишется в объяснении.

    Источник курса с заказчиком ещё не согласован — в объяснении всегда стоит оговорка
    FX_DISCLAIMER (вопрос 33 в docs/Открытые вопросы.md).
    """
    as_of = to_date(as_of) or date.today()
    src_name = setting(con, SETTING_FX_SOURCE) or FX_SOURCE_DEFAULT
    manual = setting(con, SETTING_FX_MANUAL)
    out = {"rate": None, "as_of": as_of.isoformat(), "setting_source": src_name,
           "manual": False, "calibrated": 0, "disclaimer": FX_DISCLAIMER, "reason": None}
    if manual not in (None, ""):
        try:
            v = float(str(manual).replace(" ", "").replace(",", "."))
        except ValueError:
            v = 0.0
        if v > 0:
            out.update({"rate": v, "manual": True,
                        "source": "ручной курс заказчика из настройки valuation_settings."
                                  + SETTING_FX_MANUAL + " (источник: %s)" % src_name})
            return out
    if vs is None or not hasattr(vs, "cbu_usd_rate"):
        out["reason"] = "модуль адаптеров app/valuation_sources.py недоступен"
        out["source"] = src_name
        return out
    try:
        r = vs.cbu_usd_rate(as_of)
    except Exception as e:              # сеть не должна ронять оценку
        r, out["reason"] = None, "сбой запроса курса (%s: %s)" % (type(e).__name__, e)
    if r:
        out.update({"rate": float(r),
                    "source": "официальный курс ЦБ РУз, открытый JSON cbu.uz (%s)" % src_name})
    else:
        out["source"] = "официальный курс ЦБ РУз, открытый JSON cbu.uz (%s)" % src_name
        out["reason"] = out["reason"] or "ЦБ РУз курс на эту дату не отдал (нет сети или нет данных)"
    return out


def fx_line(fx: dict) -> str:
    """Строка объяснения о применённом курсе — обязательна в каждом расчёте по рынку."""
    if not fx:
        return ""
    d = to_date(fx.get("as_of"))
    when = d.strftime("%d.%m.%Y") if d else str(fx.get("as_of"))
    if fx.get("rate"):
        return ("Курс: 1 доллар = %s сум на %s, источник — %s; %s."
                % (format(float(fx["rate"]), ",.2f").replace(",", " "), when,
                   fx.get("source") or FX_SOURCE_DEFAULT, FX_DISCLAIMER))
    return ("Курс получить не удалось (%s): цены объявлений в у.е. в медиану не берутся, "
            "считаем только по ценам в сумах. Источник курса — %s; %s."
            % (fx.get("reason") or "причина не указана",
               fx.get("source") or FX_SOURCE_DEFAULT, FX_DISCLAIMER))


def collect_market(object_type: str, params: dict, as_of, rate: float = None) -> list:
    """
    Опрос площадок. Нет модуля адаптеров или нет сети — источники со статусом, расчёт не падает.

    rate — курс доллара (сумов за 1 доллар). Без него адаптеры не могут перевести цены в у.е.
    в сумы: такие объявления выпадают из медианы и рыночная ветка не срабатывает вовсе.
    """
    as_of = to_date(as_of) or date.today()
    if vs is None:
        return [{"source": "площадки объявлений", "status": "источник недоступен",
                 "reason": "модуль адаптеров app/valuation_sources.py недоступен"}]
    if object_type == OBJECT_SPECIAL:
        # раздел спецтехники на avtoelon.uz в реестре адаптеров статистика пока не заведён,
        # поэтому опрашиваем его здесь (проверено живым запросом 21.09.2026, см. SPEC_SECTIONS)
        return [fetch_avtoelon_special(params or {}, as_of, rate)]
    try:
        return vs.fetch_all(object_type, params or {}, as_of, rate)
    except TypeError:                   # старый адаптер без параметра курса
        return vs.fetch_all(object_type, params or {}, as_of)
    except Exception as e:              # сеть не должна ронять оценку
        return [{"source": "площадки объявлений", "status": "источник недоступен",
                 "reason": f"сбой опроса площадок ({type(e).__name__}: {e})"}]


# ---------- спецтехника: раздел объявлений avtoelon.uz ----------
#
# Проверено живым запросом 21.09.2026: у avtoelon.uz есть отдельный раздел
# https://avtoelon.uz/spectehnika/ (robots.txt его не запрещает), выдача отдаётся готовым HTML
# в том же виде, что и раздел легковых: рядом с карточкой лежит JSON listing.items.push
# с ценой (unitPrice, по умолчанию в у.е.), городом, маркой и датой последнего обновления.
# Ограничения те же, что у легковых: дата — это lastUpdate, а не дата публикации; наработка
# моточасов в выдаче не публикуется, поэтому отобрать «похожие по наработке» машины нельзя.

OBJECT_SPECIAL = "спецтехника"

# подтип (как в app/vehicle_class.py и в справочнике base_rates) → путь раздела на сайте
SPEC_SECTIONS = {
    "экскаватор": "gruzovaja-tehnika/ekskavator",
    "экскаватор-погрузчик": "gruzovaja-tehnika/ekskavatory-pogruzchiky",
    "бульдозер": "gruzovaja-tehnika/buldozer",
    "автокран": "gruzovaja-tehnika/avtokran",
    "кран-манипулятор": "gruzovaja-tehnika/manipulyatory",
    "автовышка": "gruzovaja-tehnika/avtovyshky",
    "бетононасос": "gruzovaja-tehnika/betononasos",
    "бетоносмеситель": "gruzovaja-tehnika/betonovoz-ili-betonsmestitel",
    "погрузчик фронтальный": "gruzovaja-tehnika/kovshovyiy-pogruzchik",
    "погрузчик вилочный": "gruzovaja-tehnika/vilochnyiy-pogruzchik",
    "грейдер": "gruzovaja-tehnika/greidery",
    "каток": "gruzovaja-tehnika/katky",
    "асфальтоукладчик": "gruzovaja-tehnika/asfaltoukladchiky",
    "буровая установка": "gruzovaja-tehnika/burovoe-oborudovanie",
    "трактор": "gruzovaja-tehnika/traktor",
    "мини-трактор": "gruzovaja-tehnika/mini-traktor",
    "комбайн": "gruzovaja-tehnika/kombayn",
    "косилка": "gruzovaja-tehnika/kosilka",
    "сеялка": "gruzovaja-tehnika/seyalka",
    "борона": "gruzovaja-tehnika/borona",
    "самосвал": "gruzoviki/samosval",
    "тягач": "gruzoviki/tyagach",
    "прицеп": "gruzoviki/pritsep",
    "полуприцеп": "gruzoviki/polu-pritsep",
    "автобус": "avtobusy/avtobus",
    "микроавтобус": "avtobusy/mikroavtobus",
    "эвакуатор": "gruzovaja-tehnika/evakuator-manipulyator",
    "мусоровоз": "gruzovaja-tehnika/musorovozy",
}

SPEC_BASE_URL = "https://avtoelon.uz/spectehnika/"


def spec_section(subtype: str) -> Optional[str]:
    """Подтип машины → путь раздела на avtoelon.uz. Незнакомый подтип — None, раздел не гадаем."""
    s = (subtype or "").strip().lower()
    if not s:
        return None
    if s in SPEC_SECTIONS:
        return SPEC_SECTIONS[s]
    for name, path in SPEC_SECTIONS.items():       # «экскаватор гусеничный» → «экскаватор»
        if name in s:
            return path
    return None


def fetch_avtoelon_special(params: dict, as_of, rate: float = None) -> dict:
    """
    Объявления по спецтехнике с avtoelon.uz.

    params: подтип (subtype) — обязателен, марка (brand), регион (region), год (year).
    Разбор выдачи берём у статистика (app/valuation_sources), потому что разметка страницы
    у раздела спецтехники та же, что у легковых. Результат — такой же словарь источника,
    как у остальных адаптеров: агрегатор market_aggregate его понимает без изменений.
    """
    as_of = to_date(as_of) or date.today()
    base = {"source": "avtoelon.uz — спецтехника", "status": "источник недоступен",
            "url": SPEC_BASE_URL, "ads_count": 0, "prices": [], "samples": [],
            "limitations": [], "fetched_at": datetime.now().isoformat(timespec="seconds")}
    if vs is None:
        base["reason"] = "модуль адаптеров app/valuation_sources.py недоступен"
        return base
    subtype = (params.get("subtype") or params.get("подтип") or params.get("тип") or "")
    path = spec_section(subtype)
    if not path:
        base["reason"] = ("подтип машины не указан или не найден в разделах сайта "
                          "(«%s») — раздел выдумывать нельзя" % (subtype or "не указан"))
        return base

    res = vs._new("avtoelon.uz — спецтехника")
    parts = [SPEC_BASE_URL.rstrip("/"), path]
    brand = vs._slug(params.get("марка") or params.get("brand") or "")
    if brand:
        parts.append(brand)
    region = (params.get("регион") or params.get("region") or "").strip().lower()
    city = vs.AVTOELON_CITIES.get(region)
    if region and not city:
        res.limitations.append("Регион «%s» не найден в справочнике слагов сайта — "
                               "поиск выполнен без фильтра по региону." % region)
    if city:
        parts.append(city)
    url = "/".join(parts) + "/"
    res.url = url
    try:
        allowed, why = vs.robots_check(url)
        if not allowed:
            res.status = vs.STATUS_ROBOTS
            res.reason = why
            return res.to_dict()
        html = vs._http_get(url)
        rows = vs._parse_avtoelon(html, as_of, rate, params, res)
    except Exception as e:                       # сеть и разметка не должны ронять оценку
        res.reason = "не удалось загрузить выдачу (%s: %s)" % (type(e).__name__, e)
        return res.to_dict()
    res.limitations.append("Дата у объявления — дата последнего обновления (lastUpdate); "
                           "дату первой публикации выдача avtoelon.uz не отдаёт.")
    res.limitations.append("Наработка моточасов в выдаче не публикуется — отбор по ней "
                           "не выполнялся; сравнение идёт по подтипу, марке и году.")
    if brand:
        res.limitations.append("Отбор по марке сделан адресом раздела сайта: «%s»." % brand)
    vs._finish(res, rows, rate)
    if res.status == vs.STATUS_EMPTY:
        res.reason = ("объявлений по подтипу «%s», подходящих по параметрам и обновлённых "
                      "не раньше %s, не найдено"
                      % (subtype, vs.window_start(as_of).isoformat()))
    return res.to_dict()


def stat_agency_sources(con, object_type: str, params: dict) -> list:
    """
    Агентство статистики как источник по недвижимости.

    Площадки недвижимости закрыты (olx — 403, uybor и joymee рисуются в браузере), а цены
    квадратного метра орган статистики не публикует — у него есть только индекс цен на жильё
    по регионам. Поэтому источник идёт со статусом «Агентство статистики»: ни одной цены
    в медиану он не даёт, его роль — привести УЖЕ известную цену (отчёт оценщика, прошлый
    договор) к кварталу договора. Метод приведения с заказчиком не утверждён (вопрос 50).
    """
    if object_type != "недвижимость":
        return []
    try:
        from . import statagency
        return [statagency.valuation_source(con, params)]
    except Exception as e:                  # модуль или база недоступны — расчёт не падает
        return [{"source": "stat.uz — индекс цен на жильё", "status": "источник недоступен",
                 "reason": "модуль app/statagency.py недоступен (%s: %s)" % (type(e).__name__, e),
                 "url": "https://stat.uz/ru/ofitsialnaya-statistika/prices-and-indexes"}]


# ---------- шаг 2: письмо дилеру (только авто) ----------

DEALER_UNKNOWN = "уточнить у заказчика"


def find_dealer(brand: str) -> Optional[dict]:
    """
    Дилера берём только из справочника статистика (vs.dealers → data/dealers_uz.json).
    Чего в справочнике нет — то остаётся «уточнить у заказчика»: адреса не придумываем.
    """
    if vs is None or not brand or not hasattr(vs, "dealers"):
        return None
    try:
        rows = vs.dealers(str(brand).strip())
    except Exception:                   # справочник не должен ронять расчёт
        return None
    if not rows:
        return None
    r = rows[0]
    keep = lambda v: v if v and str(v).strip() and str(v).strip() != DEALER_UNKNOWN else None
    return {"name": keep(r.get("официальный_представитель")) or keep(r.get("марка")),
            "email": keep(r.get("email")),
            "phone": keep(r.get("телефон")),
            "site": keep(r.get("сайт")),
            "form": keep(r.get("форма_обратной_связи")),
            "checked": r.get("проверено"),
            "brand": r.get("марка")}


def dealer_letter(params: dict, as_of=None, object_type: str = "авто") -> dict:
    """
    Текст запроса официальному дилеру. Цен дилера система не придумывает:
    письмо уходит человеку, ответ возвращается в оценку руками.

    object_type = «спецтехника» меняет только формулировки: вместо VIN и пробега
    спрашиваются заводской номер и наработка моточасов, вместо комплектации — исполнение.
    """
    p = params or {}
    as_of = to_date(as_of) or date.today()
    special = (object_type == OBJECT_SPECIAL)
    brand = p.get("brand") or p.get("марка") or DEALER_UNKNOWN
    model = p.get("model") or p.get("модель") or DEALER_UNKNOWN
    year = p.get("year") or p.get("год") or DEALER_UNKNOWN
    vin = p.get("vin") or p.get("заводской_номер") or ""
    trim = p.get("trim") or p.get("комплектация") or DEALER_UNKNOWN
    mileage = p.get("mileage_km") or p.get("пробег")
    hours = p.get("engine_hours") or p.get("моточасы") or p.get("наработка")
    if special:
        return _special_dealer_letter(p, as_of, brand, model, year, vin, hours)
    dealer = find_dealer(brand)
    to = (dealer or {}).get("name") or DEALER_UNKNOWN
    email = ((dealer or {}).get("email") or (dealer or {}).get("form")
             or (dealer or {}).get("site") or DEALER_UNKNOWN)
    phone = (dealer or {}).get("phone")
    text = (
        "Кому: %s\n"
        "Адрес: %s\n"
        "Тема: запрос стоимости автомобиля %s %s для целей страхования\n\n"
        "Уважаемые коллеги!\n\n"
        "Страховая организация АО «INSON» принимает автомобиль на страхование и определяет его "
        "страховую стоимость на дату %s.\n\n"
        "Сведения об автомобиле:\n"
        "  марка: %s\n"
        "  модель: %s\n"
        "  год выпуска: %s\n"
        "  комплектация: %s\n"
        "  VIN: %s\n"
        "%s"
        "\nПросим сообщить:\n"
        "  1) текущую цену нового автомобиля этой модели и комплектации (или ближайшего аналога, "
        "если модель снята с производства);\n"
        "  2) рекомендованную цену такого автомобиля, бывшего в эксплуатации, с указанным годом "
        "выпуска и пробегом;\n"
        "  3) дату, на которую действительны указанные цены.\n\n"
        "Ответ просим дать письменно: он будет приложен к расчёту страховой стоимости.\n\n"
        "С уважением,\nандеррайтинг АО «INSON»"
    ) % (to, email, brand, model, as_of.strftime("%d.%m.%Y"), brand, model, year, trim,
         vin or "не указан",
         ("  пробег: %s км\n" % mileage) if mileage else "")
    if phone:
        text += "\nТелефон представителя по справочнику: %s" % phone
    if dealer and not (dealer.get("email")):
        note = ("Адрес электронной почты представителя в справочнике не подтверждён — "
                "указана форма обратной связи или сайт; почту уточняет заказчик.")
    elif dealer:
        note = ""
    else:
        note = ("Дилер по марке в справочнике не найден: адресата заполняет сотрудник, "
                "адрес система не придумывает.")
    return {"to": to, "email": email, "phone": phone, "site": (dealer or {}).get("site"),
            "checked": (dealer or {}).get("checked"),
            "dealer_found": bool(dealer), "as_of": as_of.isoformat(),
            "text": text, "note": note}


def _special_dealer_letter(p: dict, as_of, brand, model, year, serial, hours) -> dict:
    """
    Письмо официальному представителю марки спецтехники.

    Справочник data/dealers_uz.json сейчас заполнен только по легковым маркам: официальных
    представителей Caterpillar, Komatsu, XCMG, SANY, JCB в нём нет, и придумывать их нельзя.
    Поэтому адресат остаётся «уточнить у заказчика», пока сведения не подтверждены
    (что проверено и что нет — в docs/Спецтехника — справочники для базы.md).
    """
    subtype = p.get("subtype") or p.get("подтип") or p.get("тип") or "спецтехника"
    dealer = find_dealer(brand)
    to = (dealer or {}).get("name") or DEALER_UNKNOWN
    email = ((dealer or {}).get("email") or (dealer or {}).get("form")
             or (dealer or {}).get("site") or DEALER_UNKNOWN)
    phone = (dealer or {}).get("phone")
    text = (
        "Кому: %s\n"
        "Адрес: %s\n"
        "Тема: запрос стоимости спецтехники %s %s для целей страхования\n\n"
        "Уважаемые коллеги!\n\n"
        "Страховая организация АО «INSON» принимает на страхование самоходную машину и "
        "определяет её страховую стоимость на дату %s.\n\n"
        "Сведения о машине:\n"
        "  вид техники: %s\n"
        "  марка: %s\n"
        "  модель: %s\n"
        "  год выпуска: %s\n"
        "  заводской номер (номер рамы): %s\n"
        "%s"
        "\nПросим сообщить:\n"
        "  1) текущую цену новой машины этой модели и исполнения (или ближайшего аналога, "
        "если модель снята с производства);\n"
        "  2) ориентировочную цену такой машины, бывшей в эксплуатации, с указанным годом "
        "выпуска и наработкой;\n"
        "  3) стоимость основных узлов (двигатель, гидравлика, рабочее оборудование) — "
        "она нужна для оценки частичного повреждения;\n"
        "  4) дату, на которую действительны указанные цены.\n\n"
        "Ответ просим дать письменно: он будет приложен к расчёту страховой стоимости.\n\n"
        "С уважением,\nандеррайтинг АО «INSON»"
    ) % (to, email, brand, model, as_of.strftime("%d.%m.%Y"), subtype, brand, model, year,
         serial or "не указан",
         ("  наработка: %s моточасов\n" % hours) if hours else "")
    if phone:
        text += "\nТелефон представителя по справочнику: %s" % phone
    note = ("" if dealer else
            "Официальный представитель марки спецтехники в справочнике data/dealers_uz.json "
            "не найден: справочник заполнен только по легковым маркам. Адресата вписывает "
            "сотрудник — адрес система не придумывает.")
    return {"to": to, "email": email, "phone": phone, "site": (dealer or {}).get("site"),
            "checked": (dealer or {}).get("checked"),
            "dealer_found": bool(dealer), "as_of": as_of.isoformat(),
            "object_type": OBJECT_SPECIAL, "text": text, "note": note}


# ---------- шаг 3: износ ----------

def mileage_factor(fact_km: Optional[float], years: float, norm_km: float,
                   sensitivity: float, limit_pct: float) -> dict:
    """
    Поправка по пробегу для авто (экспертная шкала, calibrated = 0).

        ожидаемый пробег = нормативный за год × число лет
        отклонение = (фактический − ожидаемый) / ожидаемый
        коэффициент = 1 − чувствительность × отклонение, но не выходя за ±предел

    Чувствительность 0,15 и предел 15% подобраны так, чтобы двойной пробег против норматива
    (отклонение +100%) давал ровно −15% стоимости, а нулевой пробег — +15%. Дальше коэффициент
    не двигается: пробег — не единственный признак состояния, и заменять им осмотр нельзя.
    """
    if fact_km is None or years <= 0 or norm_km <= 0:
        return {"k": 1.0, "expected_km": None, "deviation_pct": None, "capped": False,
                "reason": "пробег не указан или неприменим — поправка не применяется"}
    expected = norm_km * years
    dev = (float(fact_km) - expected) / expected
    lo, hi = 1 - limit_pct / 100, 1 + limit_pct / 100
    raw = 1 - sensitivity * dev
    k = min(hi, max(lo, raw))
    return {"k": round(k, 4), "expected_km": round(expected), "deviation_pct": round(dev * 100, 1),
            "capped": abs(raw - k) > 1e-9, "limit_pct": limit_pct, "sensitivity": sensitivity,
            "reason": ("пробег %s км против норматива %s км (%+.1f%%)"
                       % (format(float(fact_km), ",.0f").replace(",", " "),
                          format(expected, ",.0f").replace(",", " "), dev * 100))}


def engine_hours_factor(fact_hours: Optional[float], years: float, norm_hours: float,
                        sensitivity: float, limit_pct: float) -> dict:
    """
    Поправка по наработке для спецтехники — то же, что поправка по пробегу у автомобилей,
    только мерой служат моточасы: у экскаватора и погрузчика одометра нет, износ считает
    счётчик наработки (экспертная шкала, calibrated = 0).

        ожидаемая наработка = норматив за год × число лет
        отклонение = (фактическая − ожидаемая) / ожидаемая
        коэффициент = 1 − чувствительность × отклонение, но не выходя за ±предел

    Норматив (1500 моточасов в год), чувствительность и предел — те же настройки, что у авто,
    отдельной статистики по спецтехнике у компании нет. Двойная наработка против норматива
    даёт −15% стоимости, нулевая — +15%; дальше коэффициент не двигается, потому что
    наработка не заменяет осмотр машины.
    """
    if fact_hours is None or years <= 0 or norm_hours <= 0:
        return {"k": 1.0, "expected_hours": None, "deviation_pct": None, "capped": False,
                "reason": "наработка моточасов не указана или неприменима — "
                          "поправка не применяется"}
    expected = norm_hours * years
    dev = (float(fact_hours) - expected) / expected
    lo, hi = 1 - limit_pct / 100, 1 + limit_pct / 100
    raw = 1 - sensitivity * dev
    k = min(hi, max(lo, raw))
    return {"k": round(k, 4), "expected_hours": round(expected),
            "deviation_pct": round(dev * 100, 1),
            "capped": abs(raw - k) > 1e-9, "limit_pct": limit_pct, "sensitivity": sensitivity,
            "reason": ("наработка %s моточасов против норматива %s (%+.1f%%)"
                       % (format(float(fact_hours), ",.0f").replace(",", " "),
                          format(expected, ",.0f").replace(",", " "), dev * 100))}


def depreciated_value(initial: float, rate_pct: float, years: float,
                      residual_min_pct: Optional[float], mileage_k: float = 1.0) -> dict:
    """
    Линейный износ с момента, когда объект стал доступен к использованию.

        износ = норма × число лет (не больше 100%)
        стоимость = первоначальная × (1 − износ) × коэффициент пробега
        но не ниже: первоначальная × остаточный минимум

    Остаточный минимум — экспертная доля (calibrated = 0): полностью самортизированный объект
    всё равно чего-то стоит, а страховать имущество по нулевой стоимости нельзя.
    """
    if initial is None or initial <= 0:
        raise HTTPException(400, "Для расчёта износа нужна первоначальная стоимость больше нуля")
    wear = min(1.0, max(0.0, rate_pct / 100 * max(0.0, years)))
    linear = initial * (1 - wear)
    adjusted = linear * (mileage_k or 1.0)
    floor = initial * (residual_min_pct or 0) / 100
    value = max(adjusted, floor)
    return {"initial": initial, "rate_pct": rate_pct, "years": round(years, 3),
            "wear_pct": round(wear * 100, 2), "linear": round(linear, 2),
            "mileage_k": mileage_k or 1.0, "adjusted": round(adjusted, 2),
            "residual_min_pct": residual_min_pct, "floor": round(floor, 2),
            "floor_applied": value > adjusted + 1e-9, "value": round(value, 2)}


# ---------- шаг 4: сверка с ценой агента ----------

def _sum(x) -> str:
    return format(float(x), ",.0f").replace(",", " ")


def compare_declared(ai_value: Optional[float], declared: Optional[float],
                     threshold_pct: float) -> dict:
    """
    Расхождение = |оценка ИИ − заявленная| / заявленная × 100.
    Выше порога — выдаём уточнённую стоимость с объяснением; иначе принимаем цену агента.
    """
    if declared is None or declared <= 0:
        return {"declared": declared, "ai_value": ai_value, "diff_pct": None,
                "accepted": "оценка ИИ", "value": ai_value,
                "text": "Агент стоимость не заявил — в договор идёт расчётная стоимость."}
    if ai_value is None:
        return {"declared": declared, "ai_value": None, "diff_pct": None,
                "accepted": "цена агента", "value": declared,
                "text": "Рассчитать стоимость не удалось; принята цена агента. "
                        "Нужны подтверждающие документы или осмотр (ГК ст. 932, 935)."}
    diff = abs(ai_value - declared) / declared * 100
    if diff > threshold_pct:
        return {"declared": declared, "ai_value": ai_value, "diff_pct": round(diff, 1),
                "threshold_pct": threshold_pct, "accepted": "оценка ИИ", "value": ai_value,
                "text": ("Заявленная стоимость %s сум расходится с расчётом %s сум на %.1f%% — "
                         "это больше порога %g%%. В расчёт идёт уточнённая стоимость %s сум; "
                         "окончательную страховую стоимость стороны согласуют при заключении "
                         "договора (ГК ст. 935)."
                         % (_sum(declared), _sum(ai_value), diff, threshold_pct, _sum(ai_value)))}
    return {"declared": declared, "ai_value": ai_value, "diff_pct": round(diff, 1),
            "threshold_pct": threshold_pct, "accepted": "цена агента", "value": declared,
            "text": ("Заявленная стоимость %s сум отличается от расчёта %s сум на %.1f%% — "
                     "это в пределах порога %g%%. Принята цена агента."
                     % (_sum(declared), _sum(ai_value), diff, threshold_pct))}


def depreciation_vs_market_warning(market: Optional[dict], dep_value: Optional[float],
                                   threshold_pct: float) -> Optional[dict]:
    """
    Проверка честности метода «износ»: объявлений нашлось меньше порога market_min_ads,
    рынок формально отвергнут — но цены-то есть. Если расчёт по износу ниже медианы этих
    объявлений (с учётом скидки на торг) больше чем на порог расхождения, агенту и
    андеррайтеру выдаётся предупреждение, а не молчаливое занижение стоимости.

    Занижать страховую стоимость опасно: страховая сумма окажется ниже действительной
    стоимости, и выплата пойдёт пропорционально (ГК ст. 936).
    """
    if not market or dep_value is None or dep_value <= 0:
        return None
    if market.get("enough"):
        return None
    med = market.get("median")
    n = int(market.get("prices_count") or 0)
    if not med or n <= 0:
        return None
    k = 1 - float(market.get("bargain_pct") or 0) / 100
    market_value = med * k
    if market_value <= dep_value:
        return None
    diff = (market_value - dep_value) / market_value * 100
    if diff <= float(threshold_pct):
        return None
    return {"market_value": round(market_value, 2), "dep_value": dep_value,
            "prices_count": n, "diff_pct": round(diff, 1),
            "text": ("по объявлениям объект стоит примерно %s сум (объявлений с ценой найдено %d — "
                     "меньше порога доверия рынку), расчёт по износу даёт %s сум, это на %.1f%% "
                     "ниже — цифра требует проверки андеррайтером. Занижение стоимости ведёт "
                     "к недострахованию и пропорциональной выплате (ГК ст. 936)."
                     % (_sum(market_value), n, _sum(dep_value), diff))}


# ---------- сборка методики ----------

LEGAL = [
    "Страховая стоимость — действительная стоимость имущества в месте его нахождения "
    "в день заключения договора (ГК ст. 934).",
    "Окончательную страховую стоимость определяют стороны договора; расчёт системы — "
    "предварительная оценка страховщика (ГК ст. 935, право на оценку — ст. 932).",
    "Страховая сумма выше страховой стоимости — договор недействителен в части превышения, "
    "излишняя премия не возвращается (ГК ст. 938).",
    "Страховая сумма ниже страховой стоимости — выплата пропорционально отношению суммы "
    "к стоимости (ГК ст. 936); клиента предупредить до подписания (ЗРУ-730 ст. 63).",
    "Цена объявления — цена предложения, а не рыночная стоимость (Закон об оценочной "
    "деятельности, ст. 7): применяется скидка на торг.",
    "Порядок определения страховой суммы и износа должен быть в правилах страхования "
    "(ЗРУ-730 ст. 30). Правил страхования в библиотеке пока нет — вопрос заказчику.",
]

CALIBRATION_PLAN = (
    "Самообучение: подтверждённые андеррайтером оценки (valuations.confirmed_by_underwriter) "
    "копятся по типам объектов. Когда наберётся не меньше 30 подтверждений по группе, "
    "считается отношение подтверждённой цены к расчётной: медиана этого отношения станет "
    "поправочным множителем группы, а разброс (Q1–Q3) — шириной вилки. Тем же способом "
    "уточняются остаточные минимумы, скидка на торг и шкала пробега; после этого у настроек "
    "ставится calibrated = 1. Саму калибровку считает отдельный модуль (app/calibration.py), "
    "здесь только накапливаются данные."
)


def settings_block(con) -> dict:
    """Все настройки методики одним куском — чтобы объяснение показывало, откуда каждая цифра."""
    keys = [SETTING_SPREAD, SETTING_MILEAGE, SETTING_MIN_ADS, SETTING_WINDOW,
            SETTING_BARGAIN, SETTING_MILEAGE_SENS, SETTING_MILEAGE_LIMIT, SETTING_EH_NORM]
    return {k: num_setting(con, k) for k in keys}


def estimate_value(con, *, object_type: str, params: dict = None, as_of=None,
                   declared_value: float = None, norm_code: str = None,
                   initial_value: float = None, commissioned_at=None,
                   mileage_km: float = None, use_market: bool = True,
                   sources: list = None, engine_hours: float = None) -> dict:
    """
    Полная оценка: рынок → (авто и спецтехника) письмо дилеру → износ → сверка с ценой агента.
    Возвращает словарь, готовый к сохранению в valuations (шаг сохранения — отдельно).

    Спецтехника (object_type = «спецтехника») считается так же, как авто, с двумя отличиями:
    объявления берутся из раздела spectehnika на avtoelon.uz, а вместо пробега работает
    наработка моточасов (engine_hours).
    """
    params = dict(params or {})
    as_of = to_date(as_of) or date.today()
    if mileage_km is None:
        mileage_km = params.get("mileage_km") or params.get("пробег")
    if engine_hours is None:
        engine_hours = (params.get("engine_hours") or params.get("моточасы")
                        or params.get("наработка"))
    st = settings_block(con)
    lines = []
    expert = []

    def mark(s: dict):
        if not s["calibrated"]:
            tag = "%s = %g%s" % (s["name"], s["value"], (" " + s["unit"]) if s["unit"] else "")
            if tag not in expert:
                expert.append(tag)

    for s in st.values():
        mark(s)

    # ---- шаг 1: рынок
    market = None
    fx = None
    if use_market:
        if sources is not None:
            res = sources                       # повтор расчёта на готовых результатах: курс уже учтён
        else:
            fx = fx_rate(con, as_of)
            res = collect_market(object_type, params, as_of, fx.get("rate"))
            res = list(res) + stat_agency_sources(con, object_type, params)
        market = market_aggregate(res, as_of, st[SETTING_MIN_ADS]["value"],
                                  st[SETTING_BARGAIN]["value"], int(st[SETTING_WINDOW]["value"]))
        if fx is not None:
            market["fx"] = fx
        if market["enough"]:
            lines.append("Шаг 1. Рынок: %d объявлений с ценой за последние %d мес. "
                         "(с %s по %s)." % (market["prices_count"], market["window_months"],
                                            market["window_from"], market["as_of"]))
            lines.append("  медиана %s сум, Q1 %s сум, Q3 %s сум."
                         % (_sum(market["median"]), _sum(market["q1"]), _sum(market["q3"])))
            lines.append("  скидка на торг %g%% (экспертно, calibrated = 0): %s × %.2f = %s сум."
                         % (market["bargain_pct"], _sum(market["median"]),
                            1 - market["bargain_pct"] / 100, _sum(market["value"])))
            lines.append("  вилка: от %s до %s сум."
                         % (_sum(market["spread"]["min"]), _sum(market["spread"]["max"])))
        else:
            lines.append("Шаг 1. Рынок не использован: %s." % market.get("reason", "нет данных"))
            for n in market["notes"][:6]:
                lines.append("  " + n)
        if fx is not None:
            lines.append("  " + fx_line(fx))
        else:
            lines.append("  Курс: расчёт повторён на готовых результатах площадок — "
                         "цены уже приведены к сумам тем курсом, который применялся при опросе; "
                         + FX_DISCLAIMER + ".")
    else:
        lines.append("Шаг 1. Опрос площадок не запрашивался.")

    # ---- шаг 2: письмо дилеру (только авто и только если рынка нет)
    letter = None
    if (market is None or not market["enough"]) and object_type in ("авто", OBJECT_SPECIAL):
        letter = dealer_letter(params, as_of, object_type=object_type)
        lines.append("Шаг 2. Рынка нет — подготовлен запрос официальному дилеру: адресат «%s». %s"
                     % (letter["to"], letter["note"] or
                        "Ответ дилера вносится в оценку вручную, цены система не придумывает."))

    # ---- шаг 3: износ
    dep = None
    mil = None
    if market is None or not market["enough"]:
        if norm_code and initial_value and commissioned_at:
            norm = norm_by_code(con, norm_code)
            years = years_between(commissioned_at, as_of)
            if object_type == OBJECT_SPECIAL:
                # у спецтехники одометра нет: износ измеряется счётчиком наработки
                mil = engine_hours_factor(float(engine_hours) if engine_hours is not None else None,
                                          years, st[SETTING_EH_NORM]["value"],
                                          st[SETTING_MILEAGE_SENS]["value"],
                                          st[SETTING_MILEAGE_LIMIT]["value"])
            elif object_type == "авто" and mileage_km is not None:
                mil = mileage_factor(float(mileage_km), years, st[SETTING_MILEAGE]["value"],
                                     st[SETTING_MILEAGE_SENS]["value"],
                                     st[SETTING_MILEAGE_LIMIT]["value"])
            else:
                mil = mileage_factor(None, years, st[SETTING_MILEAGE]["value"],
                                     st[SETTING_MILEAGE_SENS]["value"],
                                     st[SETTING_MILEAGE_LIMIT]["value"])
            dep = depreciated_value(float(initial_value), float(norm["rate_pct"]), years,
                                    norm["residual_min_pct"], mil["k"])
            dep["norm"] = {"code": norm["code"], "name": norm["name"],
                           "rate_pct": norm["rate_pct"], "residual_min_pct": norm["residual_min_pct"],
                           "calibrated": norm["calibrated"], "source": norm["source"]}
            dep["mileage"] = mil
            lines.append("Шаг 3. Износ по норме «%s» (%s), %g%% в год — из справочника "
                         "depreciation_norms." % (norm["name"], norm["code"], norm["rate_pct"]))
            lines.append("  срок с %s по %s = %.2f года; износ %g%% × %.2f = %.2f%%."
                         % (to_date(commissioned_at).isoformat(), as_of.isoformat(),
                            dep["years"], norm["rate_pct"], dep["years"], dep["wear_pct"]))
            lines.append("  %s сум × (1 − %.4f) = %s сум."
                         % (_sum(dep["initial"]), dep["wear_pct"] / 100, _sum(dep["linear"])))
            mil_name = "наработке" if object_type == OBJECT_SPECIAL else "пробегу"
            if mil["k"] != 1.0:
                lines.append("  поправка по " + mil_name + ": %s → коэффициент %.4f; "
                             "%s × %.4f = %s сум."
                             % (mil["reason"], mil["k"], _sum(dep["linear"]), mil["k"],
                                _sum(dep["adjusted"])))
                if mil.get("capped"):
                    lines.append("  поправка упёрлась в предел ±%g%% (экспертно, calibrated = 0)."
                                 % mil["limit_pct"])
            else:
                lines.append("  поправка по " + mil_name + " не применялась: %s." % mil["reason"])
            lines.append("  остаточный минимум %g%% от первоначальной = %s сум (экспертно, "
                         "calibrated = %d)." % (norm["residual_min_pct"] or 0, _sum(dep["floor"]),
                                                norm["calibrated"]))
            if dep["floor_applied"]:
                lines.append("  расчёт дал %s сум — ниже минимума, принят минимум %s сум."
                             % (_sum(dep["adjusted"]), _sum(dep["value"])))
            else:
                lines.append("  итог износа: %s сум." % _sum(dep["value"]))
            if not norm["calibrated"]:
                expert.append("остаточный минимум по норме «%s» = %g%%"
                              % (norm["code"], norm["residual_min_pct"] or 0))
        else:
            lines.append("Шаг 3. Износ не посчитан: нужны норма износа, первоначальная стоимость "
                         "и дата ввода в эксплуатацию.")

    # ---- какой метод победил
    warnings = []
    if market is not None and market["enough"]:
        method, value, spread = "объявления", market["value"], market["spread"]
    elif dep is not None:
        method, value = "износ", dep["value"]
        spread = {"min": round(value * 0.9, 2), "median": value, "max": round(value * 1.1, 2)}
        lines.append("  вилка по износу — ±10%% от расчёта (экспертно, calibrated = 0): "
                     "от %s до %s сум." % (_sum(spread["min"]), _sum(spread["max"])))
        expert.append("вилка по методу износа ±10%")
        # Объявлений нашлось меньше порога, поэтому рынок формально отвергнут. Но если найденные
        # цены сильно выше расчёта по износу, молчать нельзя: занижение страховой стоимости
        # ведёт к недострахованию и пропорциональной выплате (ГК ст. 936).
        w = depreciation_vs_market_warning(market, value, st[SETTING_SPREAD]["value"])
        if w:
            warnings.append(w["text"])
            lines.append("  ВНИМАНИЕ. " + w["text"])
    else:
        method, value, spread = None, None, None
        lines.append("Оценку получить не удалось: ни рынка, ни данных для износа. "
                     "Стоимость принимается со слов клиента — нужен осмотр или документы "
                     "(ГК ст. 932, 935).")

    # ---- шаг 4: сверка с ценой агента
    cmp = compare_declared(value, declared_value, st[SETTING_SPREAD]["value"])
    lines.append("Шаг 4. " + cmp["text"])

    return {
        "object_type": object_type, "params": params, "as_of": as_of.isoformat(),
        "method": method, "method_version": METHOD_VERSION,
        "ai_value": value, "declared_value": declared_value,
        "final_value": cmp["value"], "accepted": cmp["accepted"],
        "spread": spread,
        "explanation": {
            "method_version": METHOD_VERSION,
            "lines": lines,
            "market": market,
            "dealer_letter": letter,
            "depreciation": dep,
            "spread_check": cmp,
            "warnings": warnings,
            "fx": fx,
            "settings": st,
            "expert_values": expert,
            "calibrated": 0,
            "legal": LEGAL,
            "calibration_plan": CALIBRATION_PLAN,
            "status_note": ("Предварительная оценка страховщика; окончательная страховая "
                            "стоимость определяется соглашением сторон на день заключения "
                            "договора (ГК ст. 934, 935)."),
        },
        "sources": (market or {}).get("sources") or [],
    }


# ---------- история оценок по объекту ----------

def object_key(object_type: str, params: dict) -> str:
    """
    Ключ объекта для истории оценок. Персональных данных в ключе нет: только характеристики
    (ЗРУ-547 ст. 19 — объём данных должен соответствовать цели).
    """
    p = params or {}
    parts = [str(object_type or "")]
    for f in ("brand", "model", "year", "vin", "region", "address_hash", "area_m2", "reg_no_hash"):
        if p.get(f):
            parts.append("%s=%s" % (f, p[f]))
    return "|".join(parts).lower()


def latest_valuation(con, key: str = None, request_id: int = None) -> Optional[dict]:
    """Последняя оценка по объекту: сначала по ключу объекта, иначе по запросу."""
    if request_id:
        r = db.rows(con, "SELECT * FROM valuations WHERE request_id=? ORDER BY id DESC LIMIT 1",
                    request_id)
        if r:
            return r[0]
    if not key:
        return None
    for v in db.rows(con, "SELECT * FROM valuations ORDER BY id DESC LIMIT 300"):
        try:
            p = json.loads(v["params_json"] or "{}")
        except ValueError:
            continue
        if p.get("object_key") == key:
            return v
    return None


def valuation_for_engine(con, key: str = None, request_id: int = None) -> Optional[dict]:
    """Короткая карточка последней оценки — то, что движок кладёт в Input.valuation."""
    v = latest_valuation(con, key, request_id)
    if not v:
        return None
    try:
        p = json.loads(v["params_json"] or "{}")
    except ValueError:
        p = {}
    return {"id": v["id"], "value": v["ai_value"], "method": v["method"],
            "method_version": v["method_version"], "as_of": p.get("as_of") or v["created_at"],
            "confirmed_by": v["confirmed_by_underwriter"], "region": p.get("region")}


# ---------- эндпоинт расчёта ----------

class EstimateIn(BaseModel):
    object_type: str = "авто"
    params: dict = {}
    as_of: Optional[str] = None                # дата заключения договора
    declared_value: Optional[float] = None     # что заявил агент
    norm_code: Optional[str] = None            # код нормы износа из depreciation_norms
    initial_value: Optional[float] = None      # первоначальная стоимость
    commissioned_at: Optional[str] = None      # когда объект стал доступен к использованию
    mileage_km: Optional[float] = None
    use_market: bool = True
    sources: Optional[list] = None             # готовые результаты адаптеров (повтор без сети)
    request_id: Optional[int] = None
    save: bool = True
    created_by: Optional[str] = "api"


@router.post("/valuations/estimate")
def estimate(body: EstimateIn):
    """
    Оценка стоимости объекта по методике METHOD_VERSION.
    Считает, объясняет построчно и (по умолчанию) сохраняет результат в историю оценок.
    """
    with db.tx() as con:
        out = estimate_value(con, object_type=body.object_type, params=body.params,
                             as_of=body.as_of, declared_value=body.declared_value,
                             norm_code=body.norm_code, initial_value=body.initial_value,
                             commissioned_at=body.commissioned_at, mileage_km=body.mileage_km,
                             use_market=body.use_market, sources=body.sources)
        if body.save:
            params = dict(out["params"])
            params["object_key"] = object_key(body.object_type, body.params)
            params["as_of"] = out["as_of"]
            for f in ("norm_code", "initial_value", "commissioned_at", "mileage_km"):
                v = getattr(body, f)
                if v is not None:
                    params[f] = v
            cur = con.execute(
                """INSERT INTO valuations (created_at,created_by,request_id,object_type,params_json,
                   declared_value,ai_value,method,method_version,spread_json,explanation)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (db.now(), body.created_by, body.request_id, body.object_type,
                 json.dumps(params, ensure_ascii=False), body.declared_value, out["ai_value"],
                 out["method"], METHOD_VERSION,
                 json.dumps(out["spread"], ensure_ascii=False) if out["spread"] else None,
                 json.dumps(out["explanation"], ensure_ascii=False)))
            vid = cur.lastrowid
            for s in out["sources"]:
                con.execute(
                    """INSERT INTO valuation_sources (valuation_id,source,status,url,ads_count,
                       median,q1,q3,samples_json,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (vid, s.get("source"), s.get("status"), s.get("url"), s.get("ads_count"),
                     s.get("median"), s.get("q1"), s.get("q3"),
                     json.dumps(s.get("samples") or [], ensure_ascii=False),
                     s.get("fetched_at") or db.now()))
            db.audit(con, body.created_by or "api", "рассчитана оценка стоимости",
                     f"valuation:{vid}", {"method": out["method"], "value": out["ai_value"],
                                          "declared": body.declared_value})
            out["id"] = vid
            out["object_key"] = params["object_key"]
    return out


@router.get("/valuations/object/latest")
def latest_by_object(object_type: str, brand: str = None, model: str = None, year: int = None,
                     vin: str = None, region: str = None):
    """Последняя оценка по объекту — для движка и для экрана агента."""
    key = object_key(object_type, {"brand": brand, "model": model, "year": year,
                                   "vin": vin, "region": region})
    with db.tx() as con:
        v = valuation_for_engine(con, key=key)
    if not v:
        raise HTTPException(404, "Оценок по этому объекту ещё нет")
    v["object_key"] = key
    return v


# =========================================================================== #
#         О Ц Е Н К А   V   (ТЗ разработчику 13.10.2026, раздел 3, дни 5–6)    #
# =========================================================================== #
#
# Одна структура результата для всех методов — act_json.valuation:
#   {method, V, range {low, high}, n, reliability, reliability_pct, source {name, url, urls}, date, formula,
#    steps, warnings, settings {version, date}, calibrated, currency}
#   method: analogs — аналоги avtoelon.uz (ТС и спецтехника); depreciation — износ без аналогов (запасной);
#           exchange — биржевая цена VWAP (грузы и запасы, кл. 7 и 9); cost — затратный метод (здания).
# Все константы — act_settings.valuation (версия и дата), значения по умолчанию — act_engine.DEFAULT_SETTINGS.
# Экспертно, calibrated = 0.

VAL_METHODS = ("analogs", "depreciation", "exchange", "cost")
RELIABILITY = ("high", "medium", "low")
RELIABILITY_RU = {"high": "высокая", "medium": "средняя", "low": "низкая"}
MATERIALS = ("reinforced_concrete", "brick", "sandwich", "adobe", "metal")
MATERIAL_RU = {"reinforced_concrete": "ж/б", "brick": "кирпич", "sandwich": "сэндвич-панели", "adobe": "сырец",
               "metal": "металл"}


def _fmt(x, nd: int = 0) -> str:
    """Число с пробелами-разделителями тысяч и запятой: 111 234 567; 0,836."""
    if x is None:
        return "—"
    s = format(float(x), ",.%df" % nd).replace(",", " ").replace(".", ",")
    return s


def val_settings(con=None, custom: Optional[dict] = None) -> dict:
    """Настройки оценки: act_settings.valuation (последняя версия) поверх значений по умолчанию."""
    from . import act_engine as ae
    if custom is None and con is not None:
        try:
            r = con.execute("SELECT settings_json FROM act_settings ORDER BY id DESC LIMIT 1").fetchone()
            custom = json.loads(r[0]) if r and r[0] else {}
        except Exception:               # таблицы ещё нет — значения по умолчанию
            custom = {}
    merged = ae.merge_settings(custom or {})
    return merged["valuation"]


def _settings_tag(vs_: dict) -> dict:
    return {"version": vs_.get("version"), "date": vs_.get("date"), "key": "act_settings.valuation",
            "calibrated": int(vs_.get("calibrated") or 0)}


# ---------- курс ЦБ ----------

def fx_for_valuation(con, as_of, network: bool = True) -> dict:
    """
    Курс доллара для цен аналогов в у.е. Порядок: ручной курс заказчика (valuation_settings.fx_rate_manual) →
    курс ЦБ РУз из базы (stat_series fx_rates, набор ЦБ на data.egov.uz) не старше 7 дней → курс ЦБ на дату с cbu.uz
    (если network) → последний курс ЦБ из базы с пометкой «устарел». Нет ничего — rate = None.
    """
    as_of = to_date(as_of) or date.today()
    out = {"rate": None, "as_of": as_of.isoformat(), "date": None, "source": None, "stale": False,
           "disclaimer": FX_DISCLAIMER}
    manual = setting(con, SETTING_FX_MANUAL)
    try:
        mv = float(str(manual).replace(" ", "").replace(",", ".")) if manual not in (None, "") else 0.0
    except ValueError:
        mv = 0.0
    if mv > 0:
        out.update(rate=mv, date=as_of.isoformat(), by="manual_setting",
                   source="ручной курс заказчика (valuation_settings.fx_rate_manual)")
        return out
    row = None
    try:
        r = db.rows(con, "SELECT period, value, url FROM stat_series WHERE dataset_id='fx_rates' AND key='USD' "
                         "AND period <= ? AND value > 0 ORDER BY period DESC LIMIT 1", as_of.isoformat())
        row = r[0] if r else None
    except Exception:
        row = None
    fresh = bool(row) and (as_of - to_date(row["period"][:10])).days <= 7
    if fresh:
        out.update(rate=float(row["value"]), date=row["period"][:10], by="cbu_db", url=row["url"],
                   source="курс ЦБ РУз на %s из базы (data.egov.uz, набор fx_rates)"
                          % to_date(row["period"][:10]).strftime("%d.%m.%Y"))
        return out
    if network and vs is not None and hasattr(vs, "cbu_usd_rate"):
        try:
            rate = vs.cbu_usd_rate(as_of)
        except Exception:
            rate = None
        if rate:
            out.update(rate=float(rate), date=as_of.isoformat(), by="cbu", url=vs.CBU_URL % as_of.isoformat(),
                       source="официальный курс ЦБ РУз на %s (cbu.uz)" % as_of.strftime("%d.%m.%Y"))
            return out
    if row:
        d = to_date(row["period"][:10])
        out.update(rate=float(row["value"]), date=d.isoformat(), by="cbu_db", url=row["url"], stale=True,
                   source="курс ЦБ РУз на %s из базы (data.egov.uz, набор fx_rates) — свежего курса нет, "
                          "курс старше 7 дней" % d.strftime("%d.%m.%Y"))
        return out
    out["reason"] = "курса нет: ни ручного курса, ни курса ЦБ в базе, сеть к cbu.uz не использовалась"
    return out


# ---------- ТС: аналоги ----------

def vehicle_years(year, as_of) -> int:
    """Возраст для нормы пробега: календарных лет с года выпуска, не меньше 1."""
    as_of = to_date(as_of) or date.today()
    try:
        return max(1, as_of.year - int(year))
    except (TypeError, ValueError):
        return 1


def k_mileage(mileage, unit: Optional[str], years: float, vs_: dict) -> dict:
    """K_пробег = clip(1 − sens·(пробег/норма − 1); lo; hi); норма = годовая норма × лет (км или моточасы)."""
    v = vs_["vehicle"]
    lo, hi = float(v["mileage_clip"][0]), float(v["mileage_clip"][1])
    hours = (unit or "").lower() in ("hours", "h", "моточасы", "мч")
    per_year = float(v["engine_hours_norm"] if hours else v["mileage_norm_km"])
    if mileage in (None, "") or years <= 0:
        return {"k": 1.0, "mileage": None, "unit": "hours" if hours else "km", "norm": None,
                "note": "пробег не указан — K_пробег = 1"}
    norm = per_year * years
    raw = 1 - float(v["mileage_sens"]) * (float(mileage) / norm - 1)
    k = min(hi, max(lo, raw))
    return {"k": round(k, 4), "raw": round(raw, 4), "mileage": float(mileage), "unit": "hours" if hours else "km",
            "norm": round(norm), "per_year": per_year, "years": years, "clipped": abs(raw - k) > 1e-9}


def k_condition(condition: Optional[str], vs_: dict) -> dict:
    kc = vs_["vehicle"]["k_condition"]
    if condition in kc:
        return {"k": float(kc[condition]), "condition": condition}
    return {"k": 1.0, "condition": condition, "note": "состояние не указано — K_сост = 1"}


def iqr_filter(values: list, k: float) -> dict:
    """Выбросы вне [Q1 − k·IQR; Q3 + k·IQR] (квартили — линейная интерполяция)."""
    q1, _, q3 = quantiles(values)
    if q1 is None:
        return {"kept": [], "cut": [], "low": None, "high": None, "q1": None, "q3": None}
    iqr = q3 - q1
    low, high = q1 - k * iqr, q3 + k * iqr
    kept = [x for x in values if low - 1e-6 <= x <= high + 1e-6]
    cut = [x for x in values if not (low - 1e-6 <= x <= high + 1e-6)]
    return {"kept": kept, "cut": cut, "low": low, "high": high, "q1": q1, "q3": q3}


def reliability_of(q1: float, q3: float, V: float, vs_: dict) -> tuple:
    """(уровень, %): (Q3 − Q1)/V ≤ первый порог — высокая, ≤ второй — средняя, иначе низкая."""
    if not V:
        return "low", None
    pct = (q3 - q1) / V * 100
    hi, mid = [float(x) for x in vs_["vehicle"]["reliability_pct"]]
    return ("high" if pct <= hi + 1e-9 else "medium" if pct <= mid + 1e-9 else "low"), round(pct, 1)


def sv_warning(S: Optional[float], V: Optional[float], vs_: dict) -> Optional[dict]:
    """«Страховая сумма — 135 % от рынка»: S вне нормы sv_band относительно рыночной оценки V."""
    if not S or not V:
        return None
    pct = S / V * 100
    lo, hi = [float(x) for x in vs_["sv_band"]]
    if lo - 1e-9 <= pct <= hi + 1e-9:
        return None
    return {"code": "sv_over" if pct > hi else "sv_under", "pct": round(pct),
            "text": "Страховая сумма %s сум — %d %% от рынка (%s сум): %s" % (
                _fmt(S), round(pct), _fmt(V),
                "страхование сверх страховой стоимости, сумма сверх стоимости недействительна (ГК ст. 938)" if pct > hi
                else "недострахование, выплата пропорционально (ГК ст. 936)")}


def k_age_of(analog_year, object_year, k_age: float) -> Optional[dict]:
    """
    Поправка на возраст аналога (ТЗ 14.10.2026, К2): k = k_age ** Δ, Δ = год аналога − год объекта.
    Тот же год — 1; аналог на 2 года старше объекта (Δ = −2) — 0,88⁻² = 1,2913 (цена делится на 0,88²).
    Нет года у аналога — None (аналог в оценку не идёт). Нет года объекта — Δ не определена: k = 1.
    """
    try:
        ay = int(analog_year)
    except (TypeError, ValueError):
        return None
    try:
        oy = int(object_year)
    except (TypeError, ValueError):
        return {"year": ay, "delta": None, "k": 1.0}
    d = ay - oy
    return {"year": ay, "delta": d, "k": float(k_age) ** d}


def _cut_row(a: dict, reason: str, code: str) -> dict:
    return {"price": a.get("price"), "currency": a.get("currency"), "price_uzs": a.get("price_uzs"),
            "year": a.get("year"), "url": a.get("url"), "code": code, "reason": reason}


def analog_value(analogs: list, *, as_of=None, year=None, mileage=None, mileage_unit: Optional[str] = None,
                 condition: Optional[str] = None, sum_insured: Optional[float] = None, vs_: dict,
                 source: Optional[dict] = None, fx: Optional[dict] = None) -> dict:
    """
    V = median(P_i × bargain × k_age^Δ_i × K_пробег × K_сост) по аналогам без выбросов (IQR), Δ_i = год аналога − год
    объекта (ТЗ 14.10.2026, К2). P — цена объявления в сумах. Аналог без года в оценку не идёт (причина в cut).
    Выбросы ищутся по цене, приведённой к году объекта (P × k_age^Δ). Меньше min_analogs аналогов — available = false
    (нужен запасной метод). Надёжность — (Q3 − Q1)/V.
    """
    v = vs_["vehicle"]
    k_age = float(v["k_age"])
    as_of = to_date(as_of) or date.today()
    priced = [a for a in analogs if a.get("price_uzs")]
    no_uzs = len(analogs) - len(priced)
    cut, dated = [], []
    for a in priced:
        ka = k_age_of(a.get("year"), year, k_age)
        if ka is None:
            cut.append(_cut_row(a, "нет года выпуска у аналога — поправку на возраст посчитать нельзя", "no_year"))
        else:
            dated.append((a, ka))
    flt = iqr_filter([float(a["price_uzs"]) * ka["k"] for a, ka in dated], float(v["iqr_k"]))
    kept_set = list(flt["kept"])
    kept = []
    for a, ka in dated:
        pa = float(a["price_uzs"]) * ka["k"]
        if pa in kept_set:
            kept_set.remove(pa)
            kept.append((a, ka))
        else:
            cut.append(_cut_row(a, "выброс: цена с поправкой на возраст %s сум вне [%s; %s]" % (
                _fmt(pa), _fmt(flt["low"]), _fmt(flt["high"])), "outlier"))
    years = vehicle_years(year, as_of)
    km = k_mileage(mileage, mileage_unit, years, vs_)
    kc = k_condition(condition, vs_)
    mult = float(v["bargain"]) * km["k"] * kc["k"]
    age_adj = [float(a["price_uzs"]) * ka["k"] for a, ka in kept]
    adj = [p * mult for p in age_adj]
    age_rows = [{"year": ka["year"], "delta": ka["delta"], "k_age": round(ka["k"], 4), "url": a.get("url")}
                for a, ka in kept]
    base = {"method": "analogs", "available": False, "n": len(kept), "n_found": len(analogs),
            "n_cut": len(cut), "currency": "UZS", "date": as_of.isoformat(), "settings": _settings_tag(vs_),
            "source": source or {"name": "avtoelon.uz"}, "fx": fx, "calibrated": 0, "warnings": [],
            "cut": cut,
            "bounds": {"low": round(flt["low"]) if flt["low"] is not None else None,
                       "high": round(flt["high"]) if flt["high"] is not None else None}}
    if no_uzs:
        base["warnings"].append("%d аналогов без цены в сумах (нет курса) — в расчёт не вошли" % no_uzs)
    if len(kept) < int(v["min_analogs"]):
        base["reason"] = ("аналогов после отсева выбросов %d, а нужно не меньше %d"
                          % (len(kept), int(v["min_analogs"])))
        return base
    q1, med, q3 = quantiles(adj)
    V = round(med)
    rel, rel_pct = reliability_of(q1, q3, med, vs_)
    pm = quantiles([float(a["price_uzs"]) for a, _ in kept])[1]
    pma = quantiles(age_adj)[1]
    formula = ("V = median(P_i · %s · %s^Δ_i · K_mil · K_cond) = %s · %s · %s · %s = %s"
               % (_fmt(v["bargain"], 2), _fmt(k_age, 2), _fmt(pma), _fmt(v["bargain"], 2),
                  _fmt(km["k"], 4), _fmt(kc["k"], 2), _fmt(V)))
    by_delta = {}
    for r in age_rows:
        by_delta.setdefault(r["delta"], []).append(r)
    age_parts = []
    for d, rs in sorted(by_delta.items(), key=lambda x: (x[0] is None, -(x[0] or 0))):
        if d is None:
            age_parts.append("год %s — год объекта не указан, k = 1, аналогов %d" % (rs[0]["year"], len(rs)))
        else:
            age_parts.append("год %s (Δ = %s) — %s^%d = %s, аналогов %d" % (
                rs[0]["year"], ("%+d" % d) if d else "0", _fmt(k_age, 2), d, _fmt(rs[0]["k_age"], 4), len(rs)))
    cut_txt = ", ".join("%s %s%s" % (_fmt(c.get("price")), "у.е." if c.get("currency") == "USD" else "сум",
                                     " (нет года)" if c["code"] == "no_year" else "") for c in cut) or "нет"
    steps = [
        "Аналогов найдено %d, с ценой в сумах %d; без года и выбросы вне [%s; %s] сум (цена с поправкой на возраст) "
        "отсечены: %d (%s)." % (len(analogs), len(priced), _fmt(flt["low"]), _fmt(flt["high"]), len(cut), cut_txt),
        "В расчёте %d аналогов; медиана цены объявления P = %s сум, с поправкой на возраст — %s сум." % (
            len(kept), _fmt(pm), _fmt(pma)),
        "Поправка на возраст k_age^Δ, k_age = %s, Δ = год аналога − год объекта%s: %s." % (
            _fmt(k_age, 2), (" (%s)" % year) if year else "", "; ".join(age_parts)),
        "Торг × %s (раздел 3 ТЗ), K_пробег = %s%s, K_сост = %s%s." % (
            _fmt(v["bargain"], 2), _fmt(km["k"], 4),
            (" (пробег %s %s при норме %s за %d лет)" % (_fmt(km["mileage"]), "моточасов" if km["unit"] == "hours"
                                                          else "км", _fmt(km["norm"]), years))
            if km.get("norm") else " (пробег не указан)",
            _fmt(kc["k"], 2), (" (%s)" % condition) if condition else " (состояние не указано)"),
        "V = %s сум; Q1 = %s, Q3 = %s; надёжность (Q3 − Q1)/V = %s %% — %s." % (
            _fmt(V), _fmt(q1), _fmt(q3), _fmt(rel_pct, 1), RELIABILITY_RU[rel]),
    ]
    if fx and fx.get("rate"):
        steps.append("Курс: 1 доллар = %s сум, %s." % (_fmt(fx["rate"], 2), fx.get("source")))
        if fx.get("stale"):
            base["warnings"].append("курс доллара старше 7 дней — проверьте курс ЦБ на дату договора")
    base.update(available=True, V=V, range={"low": round(q1), "high": round(q3)}, reliability=rel,
                reliability_pct=rel_pct, formula=formula, steps=steps, median_price=round(pm),
                median_price_age=round(pma),
                k={"bargain": float(v["bargain"]), "k_age": k_age, "object_year": year, "age": age_rows,
                   "mileage": km, "condition": kc},
                analogs=[{"price": a.get("price"), "currency": a.get("currency"), "price_uzs": a.get("price_uzs"),
                          "year": a.get("year"), "delta": ka["delta"], "k_age": round(ka["k"], 4),
                          "mileage_km": a.get("mileage_km"), "published": a.get("published"),
                          "url": a.get("url")} for a, ka in kept])
    w = sv_warning(sum_insured, V, vs_)
    if w:
        base["warnings"].append(w["text"])
        base["sv_warning"] = w
    return base


def depreciation_value(price_new: float, year, as_of=None, *, vs_: dict, sum_insured: Optional[float] = None,
                       source: Optional[dict] = None) -> dict:
    """
    Износ ТС без аналогов (запасной метод): первый год first_year_pct, далее next_years_pct в год (геометрически),
    не ниже floor_pct цены нового. V = P_new × max(floor; (1 − f1) × (1 − f2)^(лет − 1)).
    """
    d = vs_["depreciation"]
    as_of = to_date(as_of) or date.today()
    try:
        years = max(0, as_of.year - int(year))
    except (TypeError, ValueError):
        years = 0
    f1, f2, fl = float(d["first_year_pct"]) / 100, float(d["next_years_pct"]) / 100, float(d["floor_pct"]) / 100
    share = 1.0 if years == 0 else (1 - f1) * (1 - f2) ** (years - 1)
    floor_applied = share < fl
    share = max(share, fl)
    V = round(float(price_new) * share)
    out = {"method": "depreciation", "available": True, "V": V, "range": None, "n": 0, "reliability": "low",
           "reliability_pct": None, "currency": "UZS", "date": as_of.isoformat(), "settings": _settings_tag(vs_),
           "source": source or {"name": "цена нового (ввод сотрудника)"}, "calibrated": 0, "warnings": [],
           "formula": "V = P_new · max(%s; (1 − %s) · (1 − %s)^(T − 1)) = %s · %s = %s" % (
               _fmt(fl, 2), _fmt(f1, 2), _fmt(f2, 2), _fmt(price_new), _fmt(share, 4), _fmt(V)),
           "steps": ["Аналогов нет — запасной метод: износ %s %% в первый год, далее %s %% в год, не ниже %s %% "
                     "цены нового." % (_fmt(f1 * 100), _fmt(f2 * 100), _fmt(fl * 100)),
                     "Лет с выпуска: %d; остаток %s %%%s." % (years, _fmt(share * 100, 1),
                                                              " (сработал минимум)" if floor_applied else "")],
           "years": years, "share": round(share, 4), "floor_applied": floor_applied}
    w = sv_warning(sum_insured, V, vs_)
    if w:
        out["warnings"].append(w["text"])
        out["sv_warning"] = w
    return out


def _pages_for(pages, year_from, first: bool):
    """Снимок выдачи для проверки без сети: список — только для первого запроса (год объекта), словарь —
    {год в ?year[from]=: [страницы]}. None — снимка нет."""
    if pages is None:
        return None
    if isinstance(pages, dict):
        return pages.get(year_from) or pages.get(str(year_from))
    return pages if first else None


def _analog_source(raw: dict, base_url: str) -> dict:
    return {"name": "avtoelon.uz", "url": (raw.get("urls") or [base_url])[0], "urls": raw.get("urls") or [],
            "status": raw.get("status"), "reason": raw.get("reason"), "fetched_at": raw.get("fetched_at"),
            "offline": raw.get("offline"), "dropped": raw.get("dropped"), "limitations": raw.get("limitations")}


def vehicle_valuation(con, *, brand: str, model: str, year, as_of=None, mileage=None, mileage_unit=None,
                      condition=None, sum_insured=None, price_new=None, object_type: str = "авто",
                      subtype: Optional[str] = None, pages: Optional[list] = None, network_fx: bool = True,
                      keep_html: bool = False, fx: Optional[dict] = None) -> dict:
    """Аналоги avtoelon.uz → V; меньше минимума — износ от цены нового (если она есть). Результат — act_json.valuation."""
    vs_ = val_settings(con)
    v = vs_["vehicle"]
    as_of = to_date(as_of) or date.today()
    fx = fx or fx_for_valuation(con, as_of, network=network_fx)
    base_url = getattr(vs, "AVTOELON_CARS", "https://avtoelon.uz/avto")
    if object_type == OBJECT_SPECIAL:
        path = spec_section(subtype or "")
        if not path:
            raw = {"status": "источник недоступен", "analogs": [], "urls": [],
                   "reason": "подтип спецтехники не указан или не найден в разделах сайта"}
        else:
            base_url = SPEC_BASE_URL.rstrip("/") + "/" + path
    if object_type != OBJECT_SPECIAL or spec_section(subtype or ""):
        if vs is None or not hasattr(vs, "fetch_avtoelon_analogs"):
            raw = {"status": "источник недоступен", "analogs": [], "urls": [],
                   "reason": "модуль адаптеров недоступен"}
    search = []
    if object_type != OBJECT_SPECIAL or spec_section(subtype or ""):
        if vs is not None and hasattr(vs, "fetch_avtoelon_analogs"):
            # ТЗ 14.10.2026, К2: сначала год объекта; меньше min_analogs после отсева выбросов — год ± 1, затем ± 2
            # (?year[from]= год − N, аналоги других лет приводятся к году объекта множителем k_age^Δ)
            tol0 = int(v["year_tolerance"])
            plan = [tol0] + ([t for t in (1, 2) if t > tol0] if year else [])
            for tol in plan:
                y_from = (int(year) - tol) if year else None
                pg = _pages_for(pages, y_from, first=(tol == tol0))
                if pages is not None and pg is None:
                    break                   # проверка без сети: снимка выдачи с этого года нет
                raw_t = vs.fetch_avtoelon_analogs(brand, model, year, as_of, fx.get("rate"),
                                                  max_pages=int(v["max_pages"]), target=int(v["analogs_target"]),
                                                  year_tolerance=tol, base=base_url, pages=pg, keep_html=keep_html,
                                                  year_from=y_from)
                src_t = _analog_source(raw_t, base_url)
                res_t = analog_value(raw_t.get("analogs") or [], as_of=as_of, year=year, mileage=mileage,
                                     mileage_unit=mileage_unit, condition=condition, sum_insured=sum_insured,
                                     vs_=vs_, source=src_t, fx=fx)
                search.append({"tolerance": tol, "year_from": y_from, "url": (raw_t.get("urls") or [None])[0],
                               "status": raw_t.get("status"), "n_found": res_t.get("n_found"),
                               "n": res_t.get("n")})
                raw, res = raw_t, res_t
                if res_t.get("available") or raw_t.get("status") not in (vs.STATUS_OK, vs.STATUS_EMPTY):
                    break
    if not search:
        res = analog_value(raw.get("analogs") or [], as_of=as_of, year=year, mileage=mileage,
                           mileage_unit=mileage_unit, condition=condition, sum_insured=sum_insured, vs_=vs_,
                           source=_analog_source(raw, base_url), fx=fx)
    res["search"] = search
    if len(search) > 1 and res.get("steps") is not None:
        res["steps"].insert(0, "Поиск аналогов: " + "; ".join(
            "%s — %d в расчёте из %d" % ("год объекта" if s["tolerance"] == 0 else "год ± %d" % s["tolerance"],
                                         s["n"] or 0, s["n_found"] or 0) for s in search) + ".")
    res["object"] = {"brand": brand, "model": model, "year": year, "object_type": object_type, "subtype": subtype,
                     "mileage": mileage, "mileage_unit": mileage_unit, "condition": condition}
    if not res.get("available") and price_new:
        dep = depreciation_value(price_new, year, as_of, vs_=vs_, sum_insured=sum_insured)
        dep["fallback_reason"] = res.get("reason") or raw.get("reason")
        dep["analogs_tried"] = {k: res.get(k) for k in ("n", "n_found", "n_cut", "source")}
        dep["object"] = res["object"]
        res = dep
    elif not res.get("available"):
        res["reason"] = (res.get("reason") or raw.get("reason") or "аналогов нет") + \
            "; для запасного метода (износ) нужна цена нового"
    if keep_html:
        res["_html"] = raw.get("html") or []
    return res


# ---------- биржа: VWAP ----------

def exchange_value(con, *, groups: list, quantity: float, unit: str, as_of=None, vs_: dict,
                   sum_insured: Optional[float] = None, hint: Optional[str] = None) -> dict:
    """
    V = Q · p, где p — биржевая цена за единицу по ОДНОЙ выборке сделок exchange_quotes за days дней
    (< min_deals — за fallback_days): без форвардов и фьючерсов, подвид из hint (если сделок по нему достаточно),
    без выбросов по IQR (k = valuation.vehicle.iqr_k). По этой же выборке — диапазон [Q·Q1; Q·Q3] и надёжность.
    p = VWAP = Σ(p·q) / Σq; если VWAP вне [Q1; Q3] (несколько крупных лотов по краю выборки) — p = медиана,
    с предупреждением: V всегда внутри своего диапазона. Группа — первая из groups, у которой есть сделки в единице
    unit. Количество и единица — ввод сотрудника (optional.quantity, unit).
    """
    from . import uzex
    ex = vs_["exchange"]
    as_of = to_date(as_of) or date.today()
    k_iqr = float((vs_.get("vehicle") or {}).get("iqr_k") or 1.5)
    md = int(ex["min_deals"])
    tried = []
    for g in groups:
        w = uzex.vwap(con, g, unit, days=int(ex["days"]), as_of=as_of.isoformat(), iqr_k=k_iqr, hint=hint,
                      min_deals=md)
        period = int(ex["days"])
        if w["deals"] < md:
            w2 = uzex.vwap(con, g, unit, days=int(ex["fallback_days"]), as_of=as_of.isoformat(), iqr_k=k_iqr,
                           hint=hint, min_deals=md)
            if w2["deals"] > w["deals"]:
                w, period = w2, int(ex["fallback_days"])
        tried.append({"group": g, "deals": w["deals"], "days": period})
        if not w["deals"] or not w.get("vwap"):
            continue
        Qn = float(quantity) * w["qty_factor"]
        inside = w["q1"] - 1e-6 <= w["vwap"] <= w["q3"] + 1e-6
        price = w["vwap"] if inside else w["median"]
        basis = "vwap" if inside else "median"
        V = round(Qn * price)
        low, high = round(Qn * w["q1"]), round(Qn * w["q3"])
        V = min(max(V, low), high)                  # округление не выводит V за границы
        rel, rel_pct = reliability_of(w["q1"], w["q3"], price, vs_)
        sample = "%d сделок за %d дней (%s — %s), объём %s %s" % (
            w["deals"], period, w["from"], w["to"], _fmt(w["volume"], 3), w["unit"])
        excl = []
        if w.get("excluded_forward"):
            excl.append("форварды и фьючерсы — %d (сумма в реестре не равна стоимости товара)" % w["excluded_forward"])
        if w.get("excluded_subtype"):
            excl.append("другие подвиды группы — %d" % w["excluded_subtype"])
        if w.get("excluded_iqr"):
            excl.append("выбросы по цене вне [%s; %s] сум/%s (IQR, k = %s) — %d" % (
                _fmt(w["fences"][0]), _fmt(w["fences"][1]), w["unit"], _fmt(k_iqr, 1), w["excluded_iqr"]))
        out = {"method": "exchange", "available": True, "V": V, "currency": "UZS", "date": as_of.isoformat(),
               "range": {"low": low, "high": high}, "n": w["deals"],
               "reliability": rel, "reliability_pct": rel_pct, "settings": _settings_tag(vs_), "calibrated": 0,
               "source": {"name": uzex.us.SOURCE_NAME, "url": w["url"], "group": g,
                          "group_label": uzex.us.group_label(g)},
               "period": {"from": w["from"], "to": w["to"], "days": period, "fallback": period != int(ex["days"])},
               "quantity": {"value": float(quantity), "unit": unit, "normalized": round(Qn, 6),
                            "unit_norm": w["unit"]},
               "vwap": round(w["vwap"], 2), "median": round(w["median"], 2), "price": round(price, 2),
               "price_basis": basis, "q1": round(w["q1"], 2), "q3": round(w["q3"], 2), "volume": w["volume"],
               "sample": {"deals_raw": w["deals_raw"], "deals": w["deals"], "excluded_forward": w["excluded_forward"],
                          "excluded_subtype": w["excluded_subtype"], "excluded_iqr": w["excluded_iqr"],
                          "subtype": w["subtype"], "iqr_k": k_iqr, "fences": w["fences"]},
               "formula": ("V = Q · Σ(p·q) / Σq = %s %s · %s сум = %s" if inside else
                           "V = Q · медиана(p) = %s %s · %s сум = %s") % (_fmt(Qn, 3), w["unit"], _fmt(price, 2),
                                                                         _fmt(V)),
               "steps": ["Биржевая цена группы «%s»: %s." % (uzex.us.group_label(g), sample),
                         "Диапазон и надёжность — по той же выборке: Q1 %s, Q3 %s сум/%s." % (
                             _fmt(w["q1"]), _fmt(w["q3"]), w["unit"])]
                        + (["Исключено до расчёта: " + "; ".join(excl) + "."] if excl else [])
                        + ["Цены биржи — оптовые, франко-склад продавца, без доставки и наценки."],
               "warnings": [], "tried": tried}
        if not inside:
            out["warnings"].append("средневзвешенная цена %s сум/%s вне межквартильного диапазона выборки — "
                                   "взята медиана %s сум/%s" % (_fmt(w["vwap"]), w["unit"], _fmt(w["median"]),
                                                                w["unit"]))
        if period != int(ex["days"]):
            out["warnings"].append("за %d дней сделок меньше %d — взят период %d дней"
                                   % (int(ex["days"]), md, period))
        sw = sv_warning(sum_insured, V, vs_)
        if sw:
            out["warnings"].append(sw["text"])
            out["sv_warning"] = sw
        return out
    return {"method": "exchange", "available": False, "tried": tried, "date": as_of.isoformat(),
            "reason": "сделок биржи по группам %s в единице «%s» за %d дней нет"
                      % (", ".join(groups) or "—", unit, int(ex["fallback_days"]))}


# ---------- здания: затратный метод ----------

BUILD_COST_SQL = """CREATE TABLE IF NOT EXISTS build_cost_m2 (
    id            INTEGER PRIMARY KEY,
    region        TEXT NOT NULL,          -- код региона экрана (tashkent_city) или название, как ввёл администратор
    building_type TEXT NOT NULL,          -- вид здания: код вида объекта акта (shop) или название (магазин)
    price_m2      REAL NOT NULL,          -- цена строительства 1 м², сум
    date          TEXT NOT NULL,          -- на какую дату цена
    source        TEXT NOT NULL,          -- откуда цена (учебная строка — так и написано)
    created_at    TEXT NOT NULL,
    created_by    TEXT,
    UNIQUE (region, building_type, date)
)"""


def ensure_build_cost(con) -> None:
    con.execute(BUILD_COST_SQL)


def _fold(s) -> str:
    return " ".join(str(s or "").replace("ё", "е").lower().split())


def _region_keys(region: Optional[str]) -> set:
    """Код и названия региона на трёх языках — для сопоставления строки справочника с регионом акта."""
    keys = {_fold(region)}
    try:
        from .act_pkg.regions import region_code
        from . import i18n
        code = region_code(region or "")
        if code:
            keys.add(code)
            for lg in i18n.LANGS:
                keys.add(_fold(i18n.t("tg.act.reg." + code, lg)))
    except Exception:
        pass
    keys.discard("")
    return keys


def _type_keys(building_type: Optional[str]) -> set:
    keys = {_fold(building_type)}
    try:
        from .act_texts import OBJECT_KINDS
        for code, (label, names) in OBJECT_KINDS.items():
            vals = {code, _fold(label)} | {_fold(x) for x in (names or {}).values()}
            if keys & vals:
                keys |= vals
    except Exception:
        pass
    keys.discard("")
    return keys


def build_cost_lookup(con, region: Optional[str], building_type: Optional[str], as_of=None) -> Optional[dict]:
    """Строка build_cost_m2 по региону и виду здания: самая свежая на дату as_of (или раньше)."""
    ensure_build_cost(con)
    as_of = (to_date(as_of) or date.today()).isoformat()
    rk, tk = _region_keys(region), _type_keys(building_type)
    best = None
    for r in db.rows(con, "SELECT * FROM build_cost_m2 WHERE date <= ? ORDER BY date DESC, id DESC", as_of):
        if (_region_keys(r["region"]) & rk) and (_type_keys(r["building_type"]) & tk):
            best = r
            break
    return best


def cost_value(*, area: float, price_row: dict, material: str, age_years: Optional[float], vs_: dict,
               sum_insured: Optional[float] = None, as_of=None, building_type: Optional[str] = None) -> dict:
    """V = A · C · K_констр · (1 − min(wear_cap; T/T_сл) · k_wear). T неизвестен — износ не учитывается (пометка)."""
    b = vs_["building"]
    as_of = to_date(as_of) or date.today()
    kc = float(b["k_construction"].get(material, b["k_construction"].get("brick", 1.0)))
    t_sl = float(b["service_life"].get(material, b["service_life"].get("brick", 80)))
    T = float(age_years) if age_years is not None else 0.0
    wear = min(float(b["wear_cap"]), T / t_sl) * float(b["k_wear"])
    C = float(price_row["price_m2"])
    V = round(float(area) * C * kc * (1 - wear))
    out = {"method": "cost", "available": True, "V": V, "currency": "UZS", "date": as_of.isoformat(),
           "range": None, "n": 1, "reliability": None, "reliability_pct": None,
           "settings": _settings_tag(vs_), "calibrated": 0,
           "source": {"name": "build_cost_m2: %s" % price_row["source"], "table": "build_cost_m2",
                      "row_id": price_row["id"], "region": price_row["region"],
                      "building_type": price_row["building_type"], "price_date": price_row["date"]},
           "inputs": {"area_m2": float(area), "price_m2": C, "material": material, "k_construction": kc,
                      "age_years": age_years, "service_life": t_sl, "wear_cap": float(b["wear_cap"]),
                      "k_wear": float(b["k_wear"]), "wear": round(wear, 4), "building_type": building_type},
           "formula": "V = A · C · K_constr · (1 − min(%s; T/T_sl) · K_w) = %s · %s · %s · (1 − min(%s; %s/%s) · %s)"
                      " = %s" % (_fmt(b["wear_cap"], 1), _fmt(area, 1), _fmt(C), _fmt(kc, 2), _fmt(b["wear_cap"], 1),
                                 _fmt(T), _fmt(t_sl), _fmt(b["k_wear"], 2), _fmt(V)),
           "steps": ["Цена строительства 1 м² — %s сум (%s, %s, на %s)." % (
               _fmt(C), price_row["region"], price_row["building_type"], price_row["date"]),
               "Материал: %s, K_констр = %s, срок службы %s лет; возраст %s лет → износ %s %%." % (
                   MATERIAL_RU.get(material, material), _fmt(kc, 2), _fmt(t_sl),
                   _fmt(T) if age_years is not None else "не указан", _fmt(wear * 100, 1))],
           "warnings": []}
    if age_years is None:
        out["warnings"].append("год постройки не указан — износ не учтён (T = 0), оценка завышена")
    if float(b["k_wear"]) == 1.0:
        out["steps"].append("Множитель износа K_w = 1,00: индекс последнего K формулы ТЗ в PDF не читается — "
                            "значение подтверждает заказчик.")
    w = sv_warning(sum_insured, V, vs_)
    if w:
        out["warnings"].append(w["text"])
        out["sv_warning"] = w
    return out


# ---------- S/V по оценке ----------

SV_TEXT = {
    "normal": {"ru": "S/V = {r} %: в норме ({lo}–{hi} %)", "uz": "S/V = {r} %: me'yorda ({lo}–{hi} %)",
               "en": "S/V = {r} %: within the norm ({lo}–{hi} %)"},
    "under": {"ru": "S/V = {r} %: недострахование — выплата пропорционально (ГК ст. 936)",
              "uz": "S/V = {r} %: to'liq sug'urtalanmagan — to'lov mutanosib (FK 936-modda)",
              "en": "S/V = {r} %: underinsurance — claims paid pro rata (Civil Code art. 936)"},
    "over": {"ru": "S/V = {r} %: страхование сверх страховой стоимости — сумма сверх стоимости недействительна (ГК ст. 938)",
             "uz": "S/V = {r} %: qiymatdan ortiq sug'urta — ortiqcha qism haqiqiy emas (FK 938-modda)",
             "en": "S/V = {r} %: over-insurance — the excess over value is void (Civil Code art. 938)"},
}
SV_LABEL = {"normal": "норма", "under": "неполное страхование", "over": "страхование сверх страховой стоимости"}


def sv_check(S: float, V: float, vs_: dict, settings: Optional[dict] = None) -> dict:
    """S/V с выводом по ГК ст. 936/938 (act_engine.value_check с нормой valuation.sv_band)."""
    from . import act_engine as ae
    band = [float(x) for x in vs_["sv_band"]]
    vc = ae.value_check(float(S), float(V), settings, band=band)
    r = _fmt(vc["ratio_pct"], 1)
    lo, hi = _fmt(band[0]), _fmt(band[1])
    vc.update(S=S, V=V, band=band, label=SV_LABEL[vc["verdict"]],
              legal_ref=("ГК РУз, ст. 938" if vc["verdict"] == "over" else
                         "ГК РУз, ст. 936" if vc["verdict"] == "under" else None),
              text={lg: SV_TEXT[vc["verdict"]][lg].format(r=r, lo=lo, hi=hi) for lg in ("ru", "uz", "en")},
              actions=["accept_market", "keep_with_note"] if vc["verdict"] != "normal" else [])
    return vc


# ---------- оценка для акта ----------

BUILDING_KINDS = ("warehouse", "shop", "production", "office", "dwelling", "hotel")


def _stored(con, vid: int) -> Optional[dict]:
    r = db.rows(con, "SELECT * FROM valuations WHERE id=?", int(vid))
    if not r:
        return None
    try:
        ex = json.loads(r[0]["explanation"] or "{}")
    except ValueError:
        return None
    u = ex.get("valuation") if isinstance(ex, dict) else None
    if not isinstance(u, dict):
        return None
    return dict(u, valuation_id=int(vid))


def for_act(con, *, cls: str, group: Optional[str], kind: Optional[str], m: dict, o: dict, facts: dict,
            text: str = "", settings: Optional[dict] = None, today=None) -> tuple:
    """
    Оценка V для акта и проверка S/V. Возвращает (valuation | None, value_sv | None).
    Порядок: сохранённая оценка (optional.valuation_id, POST /valuation/vehicle) → биржа (кл. 7 и 9 с количеством)
    → затратный метод (здание) → износ ТС от цены нового. В сеть не ходит.
    """
    vs_ = (settings or {}).get("valuation") or val_settings(con)
    today = to_date(today) or date.today()
    S = m.get("sum_insured")
    val = None
    if o.get("valuation_id"):
        val = _stored(con, o["valuation_id"])
        if val is None:
            val = {"method": None, "available": False, "valuation_id": o["valuation_id"],
                   "reason": "оценка №%s не найдена" % o["valuation_id"]}
    if val is None and o.get("quantity") and o.get("unit") and str(cls) in [str(x) for x in
                                                                           vs_["exchange"]["classes"]]:
        from . import act_analytics as aa
        groups, _basis = aa.exchange_groups(cls, kind, o.get("class_fields"), text, o.get("object_type"))
        hint = aa._exchange_text(o.get("class_fields"), text, o.get("object_type"))
        val = exchange_value(con, groups=groups, quantity=o["quantity"], unit=o["unit"], as_of=today, vs_=vs_,
                             sum_insured=S, hint=hint) if groups else \
            {"method": "exchange", "available": False, "reason": "вид товара не распознан — группа биржи не выбрана"}
    building = group == "property" or kind in BUILDING_KINDS or o.get("building_type") or o.get("material")
    if val is None and building and str(cls) in ("8", "9") and facts.get("area_m2"):
        from .act_pkg.regions import region_for_modules
        btype = o.get("building_type") or kind
        row = build_cost_lookup(con, m.get("region_code") or m.get("region"), btype, today)
        if row is None:
            row = build_cost_lookup(con, region_for_modules(m), btype, today)
        material = o.get("material") or (vs_["building"].get("from_construction") or {}).get(o.get("construction"))
        if row is None:
            val = {"method": "cost", "available": False, "code": "no_price_m2",
                   "reason": "нет цены 1 м² для региона «%s» и вида здания «%s» (справочник build_cost_m2)"
                             % (m.get("region") or "—", btype or "—")}
        elif not material:
            val = {"method": "cost", "available": False, "code": "no_material",
                   "reason": "не указан материал стен (optional.material: %s)" % ", ".join(MATERIALS)}
        else:
            yb = o.get("year")
            age = max(0, today.year - int(yb)) if yb else None
            val = cost_value(area=facts["area_m2"], price_row=row, material=material, age_years=age, vs_=vs_,
                             sum_insured=S, as_of=today, building_type=btype)
    if val is None and group in ("vehicle", "special") and o.get("price_new") and (o.get("year") or
                                                                                   o.get("purchase_year")):
        val = depreciation_value(o["price_new"], o.get("year") or o.get("purchase_year"), today, vs_=vs_,
                                 sum_insured=S)
    if val is None:
        return None, None
    val.setdefault("settings", _settings_tag(vs_))
    sv = sv_check(S, val["V"], vs_, settings) if val.get("available") and S and val.get("V") else None
    return val, sv


# ---------- точки подключения ----------

class VehicleIn(BaseModel):
    brand: str
    model: Optional[str] = None
    year: int
    mileage: Optional[float] = None
    mileage_unit: Optional[str] = "km"          # km | hours
    condition: Optional[str] = None             # new | good | worn | damaged
    sum_insured: Optional[float] = None
    price_new: Optional[float] = None
    as_of: Optional[str] = None
    object_type: str = "авто"                   # авто | спецтехника
    subtype: Optional[str] = None               # для спецтехники: экскаватор, автокран…
    request_id: Optional[int] = None
    save: bool = True


@router.post("/valuation/vehicle")
def valuation_vehicle(body: VehicleIn):
    """
    Оценка ТС и спецтехники по аналогам avtoelon.uz (ТЗ 13.10.2026 п. 5.1): живой запрос по марке, модели и году
    (только адреса, разрешённые robots.txt: ?year[from]=, ?page=; пауза ≥ 2 с), курс ЦБ, поправки раздела 3.
    Ответ — act_json.valuation; id сохранённой оценки передаётся в /act/make как optional.valuation_id.
    """
    if body.object_type not in ("авто", OBJECT_SPECIAL):
        raise HTTPException(422, "object_type: авто или спецтехника")
    if body.object_type == "авто" and not (body.model or "").strip():
        raise HTTPException(422, "для легкового и грузового авто нужна модель")
    if body.mileage_unit not in (None, "km", "hours"):
        raise HTTPException(422, "mileage_unit: km или hours")
    if body.condition not in (None, "", "new", "good", "worn", "damaged"):
        raise HTTPException(422, "condition: new, good, worn или damaged")
    this = date.today().year
    if not 1950 <= int(body.year) <= this + 1:
        raise HTTPException(422, "year: от 1950 до %d" % (this + 1))
    with db.tx() as con:
        res = vehicle_valuation(con, brand=body.brand, model=body.model or "", year=body.year, as_of=body.as_of,
                                mileage=body.mileage, mileage_unit=body.mileage_unit, condition=body.condition or None,
                                sum_insured=body.sum_insured, price_new=body.price_new, object_type=body.object_type,
                                subtype=body.subtype)
        if body.save:
            params = {"brand": body.brand, "model": body.model, "year": body.year, "mileage": body.mileage,
                      "mileage_unit": body.mileage_unit, "condition": body.condition, "subtype": body.subtype,
                      "as_of": res.get("date")}
            params["object_key"] = object_key(body.object_type, params)
            method = {"analogs": "объявления", "depreciation": "износ"}.get(res.get("method"))
            cur = con.execute(
                """INSERT INTO valuations (created_at,created_by,request_id,object_type,params_json,declared_value,
                   ai_value,method,method_version,spread_json,explanation) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (db.now(), "api", body.request_id, body.object_type, json.dumps(params, ensure_ascii=False),
                 body.sum_insured, res.get("V"), method, "tz-2026-10-13/" + str(res["settings"]["version"]),
                 json.dumps(res.get("range"), ensure_ascii=False) if res.get("range") else None,
                 json.dumps({"valuation": res}, ensure_ascii=False, default=str)))
            res["valuation_id"] = cur.lastrowid
            db.audit(con, "api", "оценка ТС по аналогам", "valuation:%s" % cur.lastrowid,
                     {"method": res.get("method"), "V": res.get("V"), "n": res.get("n")})
    return res


class BuildCostIn(BaseModel):
    region: str
    building_type: str
    price_m2: float
    date: str
    source: str


@router.get("/reference/build-cost")
def build_cost_list(region: Optional[str] = None, building_type: Optional[str] = None):
    """Цены строительства 1 м² по регионам и видам зданий (затратный метод, ТЗ 13.10.2026 п. 6.2)."""
    with db.tx() as con:
        ensure_build_cost(con)
        rows = db.rows(con, "SELECT * FROM build_cost_m2 ORDER BY region, building_type, date DESC")
    if region:
        rk = _region_keys(region)
        rows = [r for r in rows if _region_keys(r["region"]) & rk]
    if building_type:
        tk = _type_keys(building_type)
        rows = [r for r in rows if _type_keys(r["building_type"]) & tk]
    return {"items": rows, "count": len(rows), "unit": "сум за 1 м²",
            "note": "ручной ввод администратора; учебные строки помечены в источнике"}


@router.put("/reference/build-cost")
def build_cost_put(body: BuildCostIn, user: dict = Depends(auth.require("админ"))):
    """Строка цены 1 м²: новая или правка той же (регион, вид, дата). Только администратор."""
    if not (0 < body.price_m2 < 1e10):
        raise HTTPException(422, "price_m2: число больше нуля")
    d = to_date(body.date)
    for k in ("region", "building_type", "source"):
        if not str(getattr(body, k) or "").strip() or len(str(getattr(body, k))) > 200:
            raise HTTPException(422, "%s: строка до 200 знаков" % k)
    with db.tx() as con:
        out = build_cost_save(con, body.region.strip(), body.building_type.strip(), body.price_m2, d.isoformat(),
                              body.source.strip(), user.get("login") or "админ")
    return out


def build_cost_save(con, region: str, building_type: str, price_m2: float, date_: str, source: str,
                    who: str = "админ") -> dict:
    ensure_build_cost(con)
    old = db.rows(con, "SELECT * FROM build_cost_m2 WHERE region=? AND building_type=? AND date=?",
                  region, building_type, date_)
    con.execute("""INSERT INTO build_cost_m2 (region, building_type, price_m2, date, source, created_at, created_by)
                   VALUES (?,?,?,?,?,?,?) ON CONFLICT(region, building_type, date) DO UPDATE SET
                   price_m2=excluded.price_m2, source=excluded.source, created_at=excluded.created_at,
                   created_by=excluded.created_by""",
                (region, building_type, float(price_m2), date_, source, db.now(), who))
    row = db.rows(con, "SELECT * FROM build_cost_m2 WHERE region=? AND building_type=? AND date=?",
                  region, building_type, date_)[0]
    db.audit(con, who, "цена 1 м² изменена" if old else "цена 1 м² добавлена", "build_cost_m2:%s" % row["id"],
             {"было": old[0]["price_m2"] if old else None, "стало": float(price_m2)})
    return {"ok": True, "item": row}


# Учебная строка (ТЗ 13.10.2026, раздел 2: «цена 1 м² — ручной ввод, 1 строка на регион и тип»). Цена вымышленная,
# для проверки затратного метода; настоящую цену вводит администратор.
TRAINING_ROW = {"region": "tashkent_city", "building_type": "shop", "price_m2": 6_500_000, "date": "2026-10-13",
                "source": "учебная строка (вымышленная цена для проверки, заменить ценой заказчика)"}


def build_cost_seed(con) -> dict:
    """Учебная строка Ташкент / магазин, если её ещё нет (правку администратора не перетирает)."""
    ensure_build_cost(con)
    r = TRAINING_ROW
    have = db.rows(con, "SELECT id FROM build_cost_m2 WHERE region=? AND building_type=? AND date=?",
                   r["region"], r["building_type"], r["date"])
    if have:
        return {"ok": True, "created": False, "id": have[0]["id"]}
    out = build_cost_save(con, r["region"], r["building_type"], r["price_m2"], r["date"], r["source"], "учебная")
    return {"ok": True, "created": True, "id": out["item"]["id"]}
