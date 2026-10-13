"""
Факты об объекте (13.10.2026, ТЗ день 1, п. 1.1): поля «Дополнительно» одним словарём object_facts.

Раньше каждый модуль брал поля сам и часть терялась: площадь и этажность из полей класса не доходили до раздела 1
(там читалось только распознанное) и до risk_analytics, износ не попадал никуда, место с фото не доходило до факторов.
Теперь build_facts собирает всё один раз, а акт (act_json.object), факторы (factor_adjust), уровень риска,
risk_analytics и строки раздела 1 читают один и тот же словарь.

Поля: address, area_m2, floors, wear_pct / wear_text / wear_level, mileage / mileage_unit / mileage_text, drivers,
drivers_profile, location, guard; sources — откуда каждое значение (input — ввод сотрудника, document — документ,
photo / plate — распознано, wear_pct — вариант износа выведен из процента). Чего нет — None, ничего не выдумывается.
"""
import re
from typing import Any, Optional

from .. import act_engine as ae
from .. import llm
from .recognize import _s, preferred
from .calc import _location_from_text

# адрес: названия улиц и махаллей часто совпадают с именами людей («ул. Амира Темура») — перед проверкой на ФИО
# их убираем; телефон, ИНН, паспорт и имя в другом месте строки по-прежнему дают отказ
_STREET_BEFORE = re.compile(
    r"(?i:\b(?:ул|улица|просп|проспект|пр-т|пр|пер|переулок|бульвар|б-р|мкр|массив|махалля|махалла|шоссе|"
    r"street|st|avenue|ave|str)\.?)\s+((?:[A-ZА-ЯЁЎҚҒҲ][\w'ʻ’`\-]*\.?\s*){1,3})")
_STREET_AFTER = re.compile(
    r"((?:[A-ZА-ЯЁЎҚҒҲ][\w'ʻ’`\-]*\s+){1,3})(?=(?i:(?:shoh\s+)?ko['ʻ’`]?chasi|кўчаси|кучаси|mahallasi|маҳалласи|"
    r"махалласи|mavzesi|мавзеси|massivi|массиви|street|avenue)\b)")
_NUM = re.compile(r"\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?")
_HOURS = ("моточас", "мото-час", "м/ч", "м.ч", "мч", "motosoat", "moto soat", "moto-soat", "engine hour", "hour",
          "hrs", "mth", "soat")
_KM = ("км", "km", "kilometr", "километр")


def address_pd(text: str) -> bool:
    """Похоже ли на персональные данные — без названий улиц и махаллей (они часто совпадают с именами)."""
    s = _STREET_BEFORE.sub(lambda m: m.group(0)[:m.start(1) - m.start(0)] + "X ", text or "")
    s = _STREET_AFTER.sub("X ", s)
    return llm.has_pd(s)


def address_in(raw: Any, max_chars: int) -> tuple:
    """optional.address (недоверенный ввод) → (адрес | None, ошибка | None). Только адрес объекта, без ФИО и телефона."""
    if raw in (None, ""):
        return None, None
    if isinstance(raw, (dict, list, bool, int, float)):
        return None, "адрес объекта — строка"
    s = re.sub(r"\s+", " ", str(raw)).strip()
    if not s:
        return None, None
    if len(s) > max_chars:
        return None, f"не длиннее {max_chars} знаков"
    if address_pd(s):
        return None, "без персональных данных: только адрес объекта (без имён людей, телефонов, ИНН и паспортов)"
    return s, None


def number_of(v) -> Optional[float]:
    """Первое число из значения: 450 → 450.0; «6 500 моточасов» → 6500.0; «15 %» → 15.0; нет числа — None."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = _NUM.search(str(v))
    if not m:
        return None
    try:
        return float(re.sub(r"[ \u00a0\u202f]", "", m.group(0)).replace(",", "."))
    except ValueError:
        return None


def mileage_unit(text: str) -> Optional[str]:
    """Единица пробега по словам значения: моточасы (hours) или километры (km); не сказано — None."""
    s = str(text or "").lower()
    if any(w in s for w in _HOURS):
        return "hours"
    if any(w in s for w in _KM):
        return "km"
    return None


def _doc_fields(upload: dict, kind: str) -> dict:
    return ((upload or {}).get(kind) or {}).get("fields") or {}


def build_facts(o: dict, recognized: list, upload: dict, st: dict) -> dict:
    """Факты об объекте из ввода (optional, поля класса), распознанного и загруженных документов."""
    fs = st.get("object_facts") or ae.DEFAULT_SETTINGS["object_facts"]
    cf = o.get("class_fields") or {}
    src = {}
    out = {"address": None, "area_m2": None, "floors": None, "wear_pct": None, "wear_text": None, "wear_level": None,
           "mileage": None, "mileage_unit": None, "mileage_text": None, "drivers": None, "drivers_profile": None,
           "location": None, "guard": None}

    # адрес: ввод сотрудника → договор → запрос филиала
    for value, where in ((o.get("address"), "input"), (_doc_fields(upload, "contract").get("address"), "document"),
                         (_doc_fields(upload, "branch_request").get("address"), "document")):
        if value:
            out["address"], src["address"] = str(value), where
            break

    # площадь: поле класса → общая → полезная площадь из документа
    if number_of(cf.get("area_m2")) is not None:
        out["area_m2"], src["area_m2"] = number_of(cf["area_m2"]), "input"
    else:
        for key in ("total_area", "useful_area"):
            best = preferred(recognized, key)
            if best and number_of(best["value"]) is not None:
                out["area_m2"], src["area_m2"] = number_of(best["value"]), best["source"]
                break

    if number_of(cf.get("floors")) is not None:
        out["floors"], src["floors"] = int(number_of(cf["floors"])), "input"

    # износ: текст поля «Износ» (обычно процент) и вариант фактора; вариант по проценту — только если не выбран сам
    if cf.get("wear") not in (None, ""):
        out["wear_text"], src["wear_text"] = str(cf["wear"]), "input"
        w = number_of(cf["wear"])
        if w is not None and 0 <= w <= 100:
            out["wear_pct"], src["wear_pct"] = w, "input"
    if cf.get("wear_level"):
        out["wear_level"], src["wear_level"] = str(cf["wear_level"]), "input"
    elif out["wear_pct"] is not None:
        out["wear_level"] = "high" if out["wear_pct"] > float(fs["wear_high_from_pct"]) else "normal"
        src["wear_level"] = "wear_pct"

    # пробег или моточасы: поле класса вводится как распознанное (recognized.mileage, источник input)
    best = preferred(recognized, "mileage")
    if best:
        out["mileage_text"], src["mileage"] = str(best["value"]), best["source"]
        out["mileage"] = number_of(best["value"])
        out["mileage_unit"] = mileage_unit(best["value"])

    if cf.get("drivers"):
        out["drivers"], src["drivers"] = str(cf["drivers"]), "input"
    if cf.get("drivers_profile"):
        out["drivers_profile"], src["drivers_profile"] = str(cf["drivers_profile"]), "input"

    # место работы и хранения: ввод → с фото словами (код или None)
    if o.get("location"):
        out["location"], src["location"] = o["location"], "input"
    else:
        rec = preferred(recognized, "location")
        code = _location_from_text((rec or {}).get("value") or "")
        if code:
            out["location"], src["location"] = code, rec["source"]
    if o.get("guard") is not None:
        out["guard"], src["guard"] = bool(o["guard"]), "input"

    out["sources"] = src
    out["settings"] = {"version": fs.get("version"), "date": fs.get("date")}
    out["calibrated"] = ae.CALIBRATED
    return out


def factor_inputs(o: dict, facts: dict) -> tuple:
    """(поля класса, уточнения) для factor_adjust из фактов: вариант износа по проценту, место с фото, охрана."""
    cf = dict(o.get("class_fields") or {})
    if facts.get("wear_level") and not cf.get("wear_level"):
        cf["wear_level"] = facts["wear_level"]
    rf = dict(o)
    for key in ("location", "guard"):
        if facts.get(key) is not None and rf.get(key) is None:
            rf[key] = facts[key]
    return cf, rf


_OP_RENT = ("аренд", "ijara", "rent", "lease")
_OP_CREW = ("экипаж", "оператор", "машинист", "водител", "ekipaj", "operator", "mashinist", "haydovchi", "crew",
            "driver")
_OP_STAFF = ("штат", "машинист", "оператор", "тракторист", "водител", "mashinist", "operator", "traktorchi",
             "haydovchi", "xodim", "staff", "employee", "driver")


def operator_of(text: str) -> Optional[str]:
    """Кто управляет спецтехникой — вариант справочника по словам поля «Водители и стаж»; не узнали — None."""
    s = str(text or "").lower()
    if any(w in s for w in _OP_RENT):
        return "op_rent_crew" if any(w in s for w in _OP_CREW) else "op_rent"
    if any(w in s for w in _OP_STAFF):
        return "op_staff"
    return None


def engine_hours_option(hours: float, bands: list) -> str:
    """Наработка, моточасов → вариант справочника: до bands[0] — eh_low, до bands[1] — eh_mid, до bands[2] — eh_high."""
    for limit, code in zip(bands, ("eh_low", "eh_mid", "eh_high")):
        if hours <= float(limit):
            return code
    return "eh_max"


def ra_optional(facts: dict, cls: str, veh_group: Optional[str], st: dict) -> tuple:
    """
    (поля, источники) для risk_analytics из фактов: износ в процентах (все классы), этажность и площадь (здания,
    классы 8 и 9); у класса 3 — варианты справочника факторов: спецтехнике наработка, площадка, охрана и оператор,
    дорожному транспорту — допущенные водители. Варианты — по словарям настройки object_facts.
    """
    fs = st.get("object_facts") or ae.DEFAULT_SETTINGS["object_facts"]
    src = facts.get("sources") or {}
    out, where = {}, {}

    def put(key, value, source):
        out[key], where[key] = value, source or "input"

    if facts.get("wear_pct") is not None:
        put("wear_pct", facts["wear_pct"], src.get("wear_pct"))
    cls = str(cls)
    if cls in ("8", "9"):
        if facts.get("floors") is not None:
            put("floors", facts["floors"], src.get("floors"))
        if facts.get("area_m2") is not None:
            put("area_m2", facts["area_m2"], src.get("area_m2"))
    if cls != "3":
        return out, where
    if veh_group in ae.VEH_SPECIAL:
        unit = facts.get("mileage_unit")
        # у гусеничной техники счётчика пробега нет — число без единицы считается моточасами
        if facts.get("mileage") is not None and (unit == "hours" or (unit is None and veh_group == "special_tracked")):
            put("engine_hours", engine_hours_option(facts["mileage"], fs["engine_hours_bands"]), src.get("mileage"))
        site = (fs.get("spec_site_by_location") or {}).get(facts.get("location") or "")
        if site:
            put("spec_site", site, src.get("location"))
        if facts.get("guard") is not None:
            g = (fs.get("spec_guard_by_guard") or {}).get("yes" if facts["guard"] else "no")
            if g:
                put("spec_guard", g, src.get("guard"))
        op = operator_of(facts.get("drivers"))
        if op:
            put("spec_operator", op, src.get("drivers"))      # по словам поля «Водители и стаж»
    else:
        drv = (fs.get("drivers_by_profile") or {}).get(facts.get("drivers_profile") or "")
        if drv:
            put("drivers", drv, src.get("drivers_profile"))
    return out, where
