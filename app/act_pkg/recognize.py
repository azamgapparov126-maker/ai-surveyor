"""
Распознавание снимков языковой моделью: подсказки, разбор ответа по схеме, отсев ПД, автозаполнение ТС,
подписи распознанного.
"""
import json
import re
import time
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Callable, Optional

from .. import act_engine as ae
from .. import branch_request as br
from .. import class_templates as ctpl
from .. import contract_read as cr
from .. import credit_report as crr
from .. import act_market as am
from .. import act_texts as tx
from .. import llm
from .. import vehicle_prefill as vp
from ..act_texts import t

from .common import (AI_CALLS, _AI_POOL, AI_SHRINK_STEPS, CLASS_HINTS, COMPANY_MARKERS, CONDITIONS, DOC_NAME_HINTS,
    EMPTY_VALUES, MODEL_SOURCES, PD_KEEP)
from .files import _for_model


SYSTEM_PROMPT = (
    "ты — сюрвейер страховой компании. по фотографиям объекта и снимкам документов ты записываешь, что на них "
    "видно. отвечай только объектом json строго по схеме, без пояснений и без markdown. "
    "не выдумывай: чего не видно или не читается, того нет — ставь null или не добавляй запись. "
    "не извлекай и не возвращай имена, фамилии, лица, паспортные данные, адреса и телефоны людей, "
    "а также номера посторонних машин и государственные номера. "
    "если одно и то же поле видно в нескольких источниках (табличка, документ, надпись на кузове или стреле, "
    "само фото) — верни каждое значение отдельной записью со своим source. "
    "значения с таблички и из документа переписывай в точности, со всеми знаками и единицами. "
    "названия организаций (с формой собственности: мчж, аж, атб, ооо, ао, банк, филиал) в запросе филиала "
    "и в договоре страхования переписывай как есть; если сторона договора — гражданин (фамилия и имя без "
    "формы собственности), её имя не пиши, только признак is_legal = false.")



# договор страхования: те же поля, что у разбора текста (app/contract_read.fields); госномер модель не читает
CONTRACT_SCHEMA = (
    '{"file": 1, "contract_no": "номер договора или null", "contract_date": "ГГГГ-ММ-ДД или null", '
    '"place": "место заключения или null", "product_name": "вид страхования или название продукта", '
    '"product_code": "код вида страхования или null", '
    '"insurer": {"is_legal": true, "name": "название организации или null"}, '
    '"policyholder": {"is_legal": true, "name": "…"}, "beneficiary": {"is_legal": true, "name": "…"}, '
    '"pledger": {"is_legal": true, "name": "…"}, "object": "описание объекта страхования как в документе", '
    '"class_hint": "building|equipment|vehicle|special_machinery|cargo|other или null", '
    '"address": "адрес объекта или null", "cadastre_no": "…", "brand": "…", "model": "…", "year": "…", '
    '"vin": "…", "serial_no": "…", "land_area": "…", "useful_area": "…", "total_area": "…", '
    '"construction": "…", "purpose": "назначение или деятельность", "year_built": "…", '
    '"object_value": "сумма цифрами как в документе", "sum_insured": "…", "currency": "UZS|USD|EUR|RUB или null", '
    '"items": [{"name": "часть объекта", "sum": "страховая сумма части"}], '
    '"tariff": "тариф как в документе, например 0,1 %", "premium": "…", "payment_mode": "single|installments|null", '
    '"payments": [{"date": "ГГГГ-ММ-ДД", "amount": "сумма"}], "term": "срок как в документе", '
    '"term_from": "ГГГГ-ММ-ДД", "term_to": "ГГГГ-ММ-ДД", "liability_from": "ГГГГ-ММ-ДД или null", '
    '"franchise": "франшиза как в документе или null", "franchise_type": "unconditional|conditional|null", '
    '"franchise_risk": "риск, к которому относится франшиза, или null", '
    '"covered_risks": ["короткие названия застрахованных рисков"], '
    '"exclusions": ["короткие названия исключений, не больше 20"], "territory": "территория страхования или null", '
    '"special_terms": ["особые условия и оговорки коротко"], '
    '"notice": "срок уведомления о страховом случае или null"}')
CONTRACT_HINT = (
    "договор страхования или полис (договор страхования, суғурта шартномаси, sugʻurta shartnomasi, полис, "
    "insurance contract, insurance policy; разделы «предмет договора», «страховая сумма», «страховая премия»): "
    "для такого снимка document_kind = contract и заполни contract — значения как в документе, суммы цифрами, "
    "даты в виде ГГГГ-ММ-ДД; чего в документе нет — null, не выдумывай; стороны — только организации, "
    "гражданин — is_legal = false без имени; имена, подписи и паспортные данные людей не пиши; "
    "если договора нет — contract = null.")
CONTRACT_TEXT_SYSTEM = (
    "ты — андеррайтер страховой компании. тебе дан текст договора страхования; персональные данные в нём "
    "заменены метками в квадратных скобках. извлеки условия договора. отвечай только объектом json строго по "
    "схеме, без пояснений и без markdown. не выдумывай: чего нет в тексте — null. стороны договора — только "
    "организации с формой собственности; гражданин — is_legal = false без имени. имена, подписи, паспортные "
    "данные, телефоны и адреса людей не возвращай, метки в квадратных скобках не переписывай.")

SCHEMA_HINT = (
    '{"files": [{"n": 1, "view": "front|back|left|right|plate|odometer|document|interior|facade|roof|'
    'electrical|fire_safety|general|installation|packaging|marking|transport|other", '
    '"document_kind": "строка или null"}], '
    '"object_kind": "truck_crane|crawler_crane|excavator|backhoe_loader|bulldozer|wheel_loader|forklift|'
    'telehandler|aerial_platform|concrete_pump|concrete_mixer|drilling_rig|road_machinery|tractor|combine|'
    'special_other|trailer|car|truck|electric_car|warehouse|shop|production|office|dwelling|hotel|equipment|'
    'cargo|other или null", '
    '"class_hint": "special_machinery|vehicle|building|equipment|cargo|other или null", '
    '"condition": "new|good|worn|damaged или null", '
    '"vehicle_category": null или {"code": "' + "|".join(vp.VEH_GROUPS) + ' или null", "confidence": 0.0, '
    '"why": "коротко, по каким признакам", "fuel": "' + "|".join(vp.FUELS) + ' или null", "fuel_confidence": 0.0, '
    '"fuel_why": "коротко, что видно", "file": 1}, '
    '"fields": [{"key": "ключ", "value": "значение", "source": "photo|plate|document|marking", '
    '"file": 1, "note": "строка или null"}], '
    '"damages": [{"what": "что повреждено", "where": "где", "severity": "cosmetic|major", "file": 1}], '
    '"branch_request": null или {"file": 1, ' + ", ".join(
        f'"{c}": "строка как в документе или null"' for c in br.ROW_CODES if c not in br.PARTY_CODES) +
    ', "policyholder": {"is_legal": true, "name": "название организации или null"}, '
    '"beneficiary": {"is_legal": true, "name": "…"}, "pledger": {"is_legal": true, "name": "…"}, '
    '"term_from": "ГГГГ-ММ-ДД или null", "term_to": "ГГГГ-ММ-ДД или null", '
    '"object_description_translated": "описание объекта в переводе или null", '
    '"class_hint": "building|equipment|vehicle|special_machinery|cargo|other или null"}, '
    '"contract": null или ' + CONTRACT_SCHEMA + ', "credit_report": null или ' + crr.MODEL_SCHEMA + '}')
# сканы отчёта бюро модели не читаются (credit_report.allow_scan = false): в схеме блока credit_report нет,
# вид credit_report модель только называет — чтобы сервер отбросил всё, что пришло с такого снимка
SCHEMA_HINT_NO_CR = SCHEMA_HINT.replace(', "credit_report": null или ' + crr.MODEL_SCHEMA, "")

FIELD_HINTS = (
    "ключи fields: object_type (что за объект, словами), brand (марка), model (модель), "
    "manufacture_date (дата изготовления), year (год выпуска), serial_no (заводской или серийный номер, vin), "
    "manufacturer (изготовитель), engine_no (номер двигателя), engine_model (модель двигателя), "
    "engine_power (мощность с единицами), curb_mass (снаряжённая масса с единицами), "
    "payload (грузоподъёмность с единицами), dimensions (габариты), color (цвет), "
    "mileage (пробег или моточасы), location (место эксплуатации, что видно на фото: открытая площадка, "
    "стройка, порт, охраняемая территория, закрытое помещение). "
    "view: front — спереди, back — сзади, left/right — борта, plate — заводская табличка, "
    "odometer — счётчик пробега или моточасов, document — снимок документа (техпаспорт, паспорт самоходной "
    "машины, лист технических параметров, кадастровый документ). source: plate — заводская табличка, "
    "document — документ, marking — надпись или маркировка на кузове, стреле, двери, photo — сам вид объекта. "
    "запрос филиала — таблица из шестнадцати строк (суғурта тури, суғурта қилдирувчи, наф олувчи, гаровга "
    "қўювчи, суғурта объекти, суғурта қиймати, суғурта суммаси, франшиза, суғурта тарифи, суғурта мукофоти, "
    "суғурта муддати, стандарт шартлар, контрагент, шартнома миқдори, класс, қўшимча маълумот; бывает на "
    "латинице и по-русски): для такого снимка document_kind = branch_request и заполни branch_request — "
    "каждую строку перепиши как в документе, без перевода и без пересчёта, суммы вместе с суммой прописью, "
    "срок целиком (с какого и по какое число); product_code — код вида страхования; object — вся строка "
    "объекта (описание, площади, кадастровый номер); пустая строка бланка — null. term_from и term_to — те же "
    "даты срока в виде ГГГГ-ММ-ДД. object_description_translated — описание объекта в переводе.")

# категория ТС по подгруппам шаблона класса 3 (factor_groups veh_group, fuel) — для автозаполнения акта (02.10.2026)
VEHICLE_HINT = (
    "если на снимках транспортное средство или самоходная техника — заполни vehicle_category: code — подгруппа: "
    "car — легковой автомобиль, truck — грузовой автомобиль (самосвал, тягач, фургон), bus — автобус или "
    "микроавтобус, trailer — прицеп или полуприцеп, special_wheeled — колёсная спецтехника (автокран, погрузчик, "
    "автовышка, бетононасос, грейдер), special_tracked — гусеничная спецтехника (экскаватор, бульдозер, гусеничный "
    "кран), agro — сельхозтехника (трактор, комбайн), moto — мотоцикл, мопед; fuel — только если видно: electric "
    "(зарядный порт, шильдик ev или electric, нет выхлопной трубы), cng или lpg (газовые баллоны в багажнике, на "
    "крыше или раме, надписи метан, cng, пропан, lpg), diesel или petrol (надпись на лючке бака, табличка, "
    "документ), hybrid (надпись hybrid); не видно — fuel = null. confidence и fuel_confidence — уверенность от 0 до "
    "1; why и fuel_why — коротко, по каким признакам на снимке (без номеров и людей); file — номер снимка. если "
    "транспорта нет — vehicle_category = null.")

LANG_NAME = {"ru": "русском", "uz": "узбекском (латиница)", "en": "английском"}


def model_prompt(n: int, lang: str, credit_scan: bool = False) -> str:
    """credit_scan — модель читает сканы отчёта бюро (настройка credit_report.allow_scan); иначе только называет вид."""
    return (f"приложено файлов: {n}, они пронумерованы по порядку от 1 до {n}. "
            f"для каждого файла укажи ракурс (view). {FIELD_HINTS} "
            f"описания (object_type, location, damages, note, document_kind) пиши на {LANG_NAME[lang]} языке "
            f"строчными буквами; марки, модели, номера и единицы — как написано на объекте. "
            f"видимые повреждения перечисли в damages; если повреждений не видно — пустой список; "
            f"severity: cosmetic — царапины, сколы, потёртости краски; major — вмятины, трещины, деформация, "
            f"коррозия, разбитые или отсутствующие детали. "
            f"перевод описания объекта из запроса филиала (object_description_translated) — на {LANG_NAME[lang]} "
            f"языке; если запроса филиала нет — branch_request = null. {VEHICLE_HINT} "
            f"пояснения why и fuel_why — на {LANG_NAME[lang]} языке. {CONTRACT_HINT} "
            f"{crr.MODEL_HINT if credit_scan else crr.MODEL_HINT_OFF} "
            f"схема ответа: {SCHEMA_HINT if credit_scan else SCHEMA_HINT_NO_CR}")


def _placeholders(s: str) -> set:
    return set(re.findall(r"\[[А-ЯЁ\- ]+\]", s or ""))


# отчество или «Фамилия И. О.» — внутри названия компании это уже человек (правило общее с app/act_market.py)
_PATRONYMIC = am.PATRONYMIC


def _has_marker(text: str) -> bool:
    low = text.lower() + " "
    return any(m in low for m in COMPANY_MARKERS)


def _company_only(value: str) -> bool:
    """
    Вся строка — название компании: в каждой её части (через запятую, точку с запятой, скобки, тире)
    есть маркер компании, а похожего на ФИО (2–3 слова с заглавной) вне таких частей нет. В частях
    с маркером человеком считаем только «Фамилия И. О.» и слова с отчеством.
    """
    parts = [p.strip(" .") for p in re.split(r"[,;()\[\]/]|\s[-—–]\s|\n", value) if p.strip(" .")]
    if not parts or not any(_has_marker(p) for p in parts):
        return False
    for p in parts:
        if _has_marker(p):
            if llm.NAME_INITIALS.search(p) or _PATRONYMIC.search(p):
                return False
        elif llm.NAME_INITIALS.search(p) or "[ФИО]" in llm.mask_names(p) or _PATRONYMIC.search(p):
            return False
    return True


# поля запроса филиала: числа, даты и коды — не персональные данные; длинные строки бланка
NUMERIC_KEYS = ae.NUMBER_KEYS + ("premium", "tariff_pct", "contracts_count", "land_area", "useful_area", "total_area",
                                 "product_code", "term_from", "term_to")
LONG_KEYS = ("object_type", "additional_info", "contract_terms", "policyholder", "beneficiary", "pledger",
             "franchise")


def value_limit(key: str) -> int:
    """Предел длины значения поля: длинные (описание, условия, стороны) — как у бланка запроса, прочие — 120."""
    return br.MAX_TEXT if key in LONG_KEYS else 120


def pd_like(key: str, value: str) -> bool:
    """
    Похоже ли значение на персональные данные (llm.has_pd). Два известных ложных срабатывания снимаются:
    изготовитель/марка, если вся строка — название компании (_company_only), и девятизначное число
    в номере или модели агрегата, если в значении есть буквы (чистые 9 цифр — возможный ИНН или телефон).
    """
    value = str(value or "")
    if key in ae.NUMBER_KEYS and re.fullmatch(r"[\d\s.,]+", value):
        return False                     # сумма или срок из документа — число, а не телефон или ИНН
    if key in NUMERIC_KEYS and re.fullmatch(r"[\d\s.,:\-м²m2]+", value):
        return False
    if key in br.PARTY_CODES:
        # сторона договора: только название юрлица (маркер МЧЖ/АЖ/банк …); гражданин — всегда ПД
        if not br.is_legal(value):
            return True
        found = _placeholders(llm.mask_pd(value)) - _placeholders(value)
        found.discard("[ФИО]")           # «Namunabank Sinov» — не ФИО, если есть маркер юрлица
        return bool(found)
    if not llm.has_pd(value):
        return False
    found = _placeholders(llm.mask_pd(value)) - _placeholders(value)
    if key in PD_KEEP:
        found.discard(PD_KEEP[key])      # госномер и кадастровый номер — данные объекта
    if key in ("manufacturer", "brand") and _company_only(value):
        found.discard("[ФИО]")
    if key in ("serial_no", "engine_no", "model", "engine_model") \
            and re.fullmatch(r"[\dA-Za-z\-/ ]+", value) and re.search(r"[A-Za-z]", value):
        found.discard("[ИНН]")
    return bool(found)


def _s(v, limit: int) -> Optional[str]:
    if v is None or isinstance(v, (dict, list, bool)):
        return None
    s = re.sub(r"\s+", " ", str(v)).strip()
    if s.lower() in EMPTY_VALUES:
        return None
    return s[:limit] or None


def parse_model(text: str, n: int, inclusive: bool = True) -> Optional[dict]:
    """Ответ модели → проверенная структура. Не JSON или не та схема — None (честный отказ, не догадка).
    Блок branch_request (запрос филиала) разбирается сервером (app/branch_request.from_model); inclusive —
    считать ли в сроке оба крайних дня (настройка request_check.term_inclusive)."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict) or not any(k in data for k in ("files", "fields", "damages")):
        return None
    if not isinstance(data.get("files", []), list) or not isinstance(data.get("fields", []), list) \
            or not isinstance(data.get("damages", []), list):
        return None
    views = {}
    kinds = {}
    for f in data.get("files") or []:
        if not isinstance(f, dict):
            continue
        try:
            i = int(f.get("n"))
        except (TypeError, ValueError):
            continue
        if not 1 <= i <= n:
            continue
        v = str(f.get("view") or "other").strip().lower()
        views[i] = v if v in ae.VIEWS else "other"
        dk = _s(f.get("document_kind"), 60)
        if dk and dk.lower() in ("branch_request", "branch request"):
            dk = "branch_request"
        elif dk and dk.lower() in ("contract", "insurance contract", "insurance policy", "договор страхования"):
            dk = cr.KIND
        elif dk and dk.lower() in ("credit_report", "credit report", "отчёт кредитного бюро", "кредитный отчёт"):
            dk = crr.KIND
        if dk and not pd_like("document_kind", dk):
            kinds[i] = dk
    dropped = 0
    fields, seen = [], set()
    for f in (data.get("fields") or [])[:120]:
        if not isinstance(f, dict):
            continue
        key = str(f.get("key") or "").strip()
        val = _s(f.get("value"), value_limit(key))
        if key not in ae.FIELD_KEYS or not val:
            continue
        if key == "reg_no" or pd_like(key, val):
            dropped += 1                 # госномер модели не принимаем: ей запрещено его читать
            continue
        src = str(f.get("source") or "photo").strip().lower()
        src = src if src in MODEL_SOURCES else "photo"
        try:
            fi = int(f.get("file"))
            fi = fi if 1 <= fi <= n else None
        except (TypeError, ValueError):
            fi = None
        note = _s(f.get("note"), 200)
        if note and llm.has_pd(note):
            note = None
        sig = (key, src, val.upper())
        if sig in seen:
            continue
        seen.add(sig)
        fields.append({"key": key, "value": val, "source": src, "file": fi, "note": note})
    damages = []
    for d in (data.get("damages") or [])[:20]:
        if isinstance(d, str):
            d = {"what": d}
        if not isinstance(d, dict):
            continue
        what = _s(d.get("what"), 160)
        if not what:
            continue
        where = _s(d.get("where"), 80)
        if llm.has_pd(what) or (where and llm.has_pd(where)):
            dropped += 1
            continue
        try:
            fi = int(d.get("file"))
            fi = fi if 1 <= fi <= n else None
        except (TypeError, ValueError):
            fi = None
        item = {"what": what, "where": where, "file": fi}
        sev = str(d.get("severity") or "").strip().lower()
        if sev in ("cosmetic", "major"):
            item["severity"] = sev          # тяжесть модели; нет — по словам описания (act_engine.damage_severity)
        damages.append(item)
    kind = str(data.get("object_kind") or "").strip().lower()
    hint = str(data.get("class_hint") or "").strip().lower()
    cond = str(data.get("condition") or "").strip().lower()
    brq = br.from_model(data.get("branch_request"), inclusive)
    if brq:
        try:
            bf = int((data.get("branch_request") or {}).get("file"))
            bf = bf if 1 <= bf <= n else None
        except (TypeError, ValueError, AttributeError):
            bf = None
        brq["file"] = bf or next((i for i, k in sorted(kinds.items()) if k == "branch_request"), None)
    ctr = cr.from_model(data.get("contract"), inclusive)
    if ctr:
        try:
            cf = int((data.get("contract") or {}).get("file"))
            cf = cf if 1 <= cf <= n else None
        except (TypeError, ValueError, AttributeError):
            cf = None
        ctr["file"] = cf or next((i for i, k in sorted(kinds.items()) if k == cr.KIND), None)
    cbr = crr.from_model(data.get("credit_report"))
    if cbr:
        try:
            kf = int((data.get("credit_report") or {}).get("file"))
            kf = kf if 1 <= kf <= n else None
        except (TypeError, ValueError, AttributeError):
            kf = None
        cbr = {"fields": cbr, "file": kf or next((i for i, k in sorted(kinds.items()) if k == crr.KIND), None)}
    return {"views": views, "document_kinds": kinds, "fields": fields, "damages": damages, "branch_request": brq,
            "contract": ctr, "credit_report": cbr, "vehicle_category": vehicle_category_in(data.get("vehicle_category"), n),
            "object_kind": kind if kind in tx.OBJECT_KINDS else None,
            "class_hint": hint if hint in CLASS_HINTS else None,
            "condition": cond if cond in CONDITIONS else None, "dropped": dropped}


def _conf(v) -> float:
    """Уверенность модели 0–1 (проценты 0–100 приводятся); не число — 0."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    if x != x:
        return 0.0
    x = x / 100 if 1 < x <= 100 else x
    return round(min(1.0, max(0.0, x)), 2)


def vehicle_category_in(raw: Any, n: int) -> Optional[dict]:
    """Блок vehicle_category ответа модели → {code, confidence, why, fuel, fuel_confidence, fuel_why, file} или
    None. Коды — только из подгрупп шаблона класса 3; пояснение с ПД отбрасывается."""
    if not isinstance(raw, dict):
        return None
    code = str(raw.get("code") or "").strip().lower()
    fuel = str(raw.get("fuel") or "").strip().lower()
    code = code if code in vp.VEH_GROUPS else None
    fuel = fuel if fuel in vp.FUELS else None
    if not code and not fuel:
        return None
    why, fwhy = _s(raw.get("why"), 200), _s(raw.get("fuel_why"), 200)
    try:
        fi = int(raw.get("file"))
        fi = fi if 1 <= fi <= n else None
    except (TypeError, ValueError):
        fi = None
    return {"code": code, "confidence": _conf(raw.get("confidence")) if code else None,
            "why": None if (not why or llm.has_pd(why)) else why, "fuel": fuel,
            "fuel_confidence": _conf(raw.get("fuel_confidence", raw.get("confidence"))) if fuel else None,
            "fuel_why": None if (not fwhy or llm.has_pd(fwhy)) else fwhy, "file": fi}


def _veh_option_label(con, group: str, code: Optional[str], lang: str) -> Optional[str]:
    """Подпись варианта группы факторов шаблона класса 3 (veh_group, fuel) на языке ответа."""
    if not code:
        return None
    tpl = (ctpl.current(con, "3") or {}).get("template") or {}
    for g in tpl.get("factor_groups") or []:
        if g.get("code") == group:
            for o in g.get("options") or []:
                if o.get("code") == code:
                    return ctpl.localize(o.get("label"), lang)
    return code



def vehicle_autofill(con, parsed: dict, rec: dict, fields: list, views: dict, model_to_id: dict, lang: str) -> tuple:
    """
    Автозаполнение по ТС для ответа /act/photos: (vehicle_category для экрана или None, список prefill_fields).
    Техпаспорт с текстом — docparse (блок vehicle разбора); скан техпаспорта — поля модели с источником
    «документ»; категория и топливо по фото — блок vehicle_category модели. Модели нет — только техпаспорт.
    """
    passport, pfile = None, None
    for fid, res in parsed.items():
        if (res or {}).get("vehicle"):
            passport, pfile = res["vehicle"], fid
            break
    if passport is None:
        doc = {k: (preferred([x for x in fields if x.get("source") == "document"], k) or {}) for k in
               ("brand", "model", "year", "engine_power")}
        by = {k: v.get("value") for k, v in doc.items() if v.get("value")}
        if by.get("brand") or by.get("model"):
            passport = vp.from_passport(by)
            pfile = next((v.get("file_id") for v in doc.values() if v.get("file_id")), None)
    vc = rec.get("vehicle_category") if rec.get("ok") else None
    cat = None
    if vc:
        fid = model_to_id.get(vc.get("file")) if vc.get("file") else None
        src = "techpassport" if fid and views.get(fid) == "document" else "photo"
        cat = dict(vc, file=fid, source=src)
    items = vp.prefill(passport, cat, lang, pfile)
    view = None
    if cat and (cat.get("code") or cat.get("fuel")):
        view = {"code": cat.get("code"), "label": _veh_option_label(con, "veh_group", cat.get("code"), lang),
                "confidence": cat.get("confidence"), "why": cat.get("why"),
                "fuel": cat.get("fuel"), "fuel_label": _veh_option_label(con, "fuel", cat.get("fuel"), lang),
                "fuel_confidence": cat.get("fuel_confidence"), "fuel_why": cat.get("fuel_why"),
                "file": cat.get("file"), "source": cat["source"],
                "source_label": vp.SOURCE_LABELS[cat["source"]].get(lang) or vp.SOURCE_LABELS[cat["source"]]["ru"]}
    for it in items:
        if it["field"] in ("class_fields.veh_group", "class_fields.fuel"):
            it["value_label"] = _veh_option_label(con, it["field"].split(".", 1)[1], it["value"], lang)
        it["label"] = tx.label(tx.VEH_FIELD_LABELS, it["field"], lang)
        it["check_label"] = t("prefill_photo_check" if it["source"] == "photo" else "prefill_passport_check", lang)
    return view, items


def _priority(f: dict) -> int:
    """Кого показывать модели в первую очередь, если всё не помещается: документы и таблички."""
    if f["fmt"] == "pdf":
        return 0
    name = str(f.get("orig_name") or "").lower()
    return 1 if any(h in name for h in DOC_NAME_HINTS) else 2


def pick_for_model(saved: list, budget: int) -> tuple:
    """
    Вложения для модели не больше budget байт. Сначала обычное сжатие, не помещается — сильнее
    (AI_SHRINK_STEPS), всё равно не помещается — не все файлы: в приоритете PDF и снимки, похожие на
    документ или табличку, дальше — по порядку загрузки. Возвращает (payload в порядке загрузки,
    индексы saved отправленных, индексы saved не отправленных).
    """
    one = min(budget, llm.INLINE_MAX_ONE)
    prepared = {}
    for side, quality in AI_SHRINK_STEPS:
        prepared = {i: _for_model(f["blob"], f["fmt"], side, quality) for i, f in enumerate(saved)}
        if sum(len(d) for d, _ in prepared.values()) <= budget:
            break
    order = sorted(range(len(saved)), key=lambda i: (_priority(saved[i]), i))
    chosen, total = set(), 0
    for i in order:
        size = len(prepared[i][0])
        # пределы app/llm.py проверяем заранее: иначе модель увидит меньше файлов и нумерация съедет
        if size > one or total + size > budget or len(chosen) >= llm.INLINE_MAX_FILES:
            continue
        chosen.add(i)
        total += size
    sent = sorted(chosen)
    payload = [{"name": f"file {k + 1}", "mime": prepared[i][1], "data": prepared[i][0]}
               for k, i in enumerate(sent)]
    return payload, sent, [i for i in range(len(saved)) if i not in chosen]


def recognize(saved: list, lang: str, limits: Optional[dict] = None, inclusive: bool = True) -> dict:
    """
    Одно обращение к модели со всеми снимками. saved — [{"blob", "fmt"}] в порядке загрузки.
    Одна попытка, таймаут запроса limits.ai_timeout_sec, общий срок limits.ai_deadline_sec: не уложились —
    ok=False с честной причиной (фото остаются, акт формируется).
    Возвращает {"ok", "reason", "sent": [индексы saved], "not_sent": [...], ...поля parse_model}.
    """
    scan = bool((limits or {}).get("_credit_scan"))
    return ask_model(saved, lang, limits, "акт: распознавание фото", SYSTEM_PROMPT,
                     lambda n: model_prompt(n, lang, scan), lambda text, n: parse_model(text, n, inclusive))


def ask_model(saved: list, lang: str, limits: Optional[dict], purpose: str, system: str,
              prompt_of: Callable[[int], str], parse: Callable[[str, int], Optional[dict]]) -> dict:
    """
    Общий путь к модели для фото объекта и снимков объявлений: пределы вложений, общий лимит обращений
    на сервер (AI_CALLS), один запрос без повторов с таймаутом ai_timeout_sec, общий срок ai_deadline_sec,
    ответ проверяется parse(text, n) по строгой схеме (None — честный отказ).
    """
    lim = {**ae.DEFAULT_SETTINGS["limits"], **(limits or {})}
    t0 = time.monotonic()
    if not llm.enabled():
        return {"ok": False, "reason": t("ai_not_connected", lang), "sent": [], "not_sent": []}
    if not llm.supports_files():
        return {"ok": False, "reason": t("ai_no_files", lang), "sent": [], "not_sent": []}
    payload, sent, not_sent = pick_for_model(saved, int(float(lim["ai_max_mb"]) * 1024 * 1024))
    if not payload:
        return {"ok": False, "reason": t("ph_too_big", lang, mb=lim["ai_max_mb"]), "sent": [],
                "not_sent": not_sent}
    return _model_call(purpose, [{"role": "system", "content": system},
                                 {"role": "user", "content": prompt_of(len(payload))}], payload, lim, t0, lang,
                       lambda text: parse(text, len(payload)), sent, not_sent)


def _model_call(purpose: str, messages: list, files: Optional[list], lim: dict, t0: float, lang: str, parse,
                sent=(), not_sent=(), deadline: Optional[float] = None) -> dict:
    """Одно обращение к модели в отдельном потоке: общий лимит AI_CALLS, таймаут запроса ai_timeout_sec,
    общий срок deadline (по умолчанию ai_deadline_sec от t0), ответ — через parse(text) (None — отказ)."""
    sent, not_sent = list(sent), list(not_sent)
    if not AI_CALLS.take("server", 1, int(lim["ai_calls_per_hour"]))["ok"]:
        return {"ok": False, "reason": t("ai_busy", lang, n=int(lim["ai_calls_per_hour"])), "sent": [],
                "not_sent": []}
    deadline = float(lim["ai_deadline_sec"]) if deadline is None else deadline
    fut = _AI_POOL.submit(llm.chat_raw, purpose, messages, max_tokens=4096, temperature=0.1, files=files,
                          timeout=min(float(lim["ai_timeout_sec"]), max(1.0, deadline)), retries=0)
    try:
        res = fut.result(timeout=max(0.5, deadline - (time.monotonic() - t0)))
    except FutureTimeout:
        # поток сам завершится по таймауту запроса; его ответ уже никому не нужен
        return {"ok": False, "reason": t("ai_timeout", lang, sec=int(deadline)), "sent": sent,
                "not_sent": not_sent}
    except Exception as e:
        print("акт: распознавание упало:", type(e).__name__)
        return {"ok": False, "reason": t("ai_error", lang), "sent": sent, "not_sent": not_sent}
    if not res.get("text"):
        return {"ok": False, "reason": res.get("reason") or t("ph_bad_json", lang), "sent": sent,
                "not_sent": not_sent}
    parsed = parse(res["text"])
    if parsed is None:
        return {"ok": False, "reason": t("ph_bad_json", lang), "sent": sent, "not_sent": not_sent}
    return {"ok": True, "reason": None, "sent": sent, "not_sent": not_sent, **parsed}


# кусок текста договора в одном сообщении: llm.chat_raw обрезает сообщение до MAX_PROMPT_CHARS
CT_CHUNK = 11000


def contract_text_model(text: str, lang: str, limits: Optional[dict], max_chars: int, inclusive: bool,
                        deadline: Optional[float] = None) -> dict:
    """
    Текст договора (не файл) — в модель, когда разбор правилами нашёл меньше половины ключевых полей.
    Текст сокращается до max_chars (начало и строки у подписей суммы, премии, срока, объекта) и
    маскируется llm.mask_pd ДО отправки; ответ — по схеме CONTRACT_SCHEMA, поля — contract_read.fields.
    Срок и лимит обращений — общие с распознаванием фото (limits.ai_*, AI_CALLS).
    """
    lim = {**ae.DEFAULT_SETTINGS["limits"], **(limits or {})}
    t0 = time.monotonic()
    if not llm.enabled():
        return {"ok": False, "reason": t("ai_not_connected", lang)}
    part, cut = cr.excerpt(text, max_chars)
    masked = llm.mask_pd(part)
    chunks = [masked[k:k + CT_CHUNK] for k in range(0, len(masked), CT_CHUNK)] or [""]
    messages = [{"role": "system", "content": CONTRACT_TEXT_SYSTEM},
                {"role": "user", "content": 'схема ответа: {"contract": ' + CONTRACT_SCHEMA + "}"}]
    messages += [{"role": "user", "content": f"текст договора, часть {k + 1} из {len(chunks)}:\n{c}"}
                 for k, c in enumerate(chunks)]

    def parse(reply):
        m = re.search(r"\{.*\}", reply or "", re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except ValueError:
            return None
        raw = data.get("contract") if isinstance(data, dict) and isinstance(data.get("contract"), dict) else data
        got = cr.from_model(raw, inclusive, min_found=0)
        return {"contract": got} if got else None

    res = _model_call("акт: договор по тексту", messages, None, lim, t0, lang, parse, deadline=deadline)
    res["excerpt_cut"] = cut
    return res


# --------------------------------------------------------------------------- #
#  Распознанное: подписи и выбор значения
# --------------------------------------------------------------------------- #

def recognized_view(items: list, lang: str, file_ids: Optional[dict] = None, group: Optional[str] = None) -> list:
    """Распознанное для экрана: подпись поля, источник словами и пометка «проверьте»."""
    out = []
    for r in items:
        out.append({"key": r["key"], "label": tx.field_label(r["key"], lang, group), "value": r["value"],
                    "source": r["source"], "source_label": tx.label(tx.SOURCE_LABELS, r["source"], lang),
                    "note": r.get("note"), "file": (file_ids or {}).get(r.get("file"), r.get("file_id")),
                    "check": True, "check_label": t("check_mark", lang)})
    return out


def preferred(items: list, key: str) -> Optional[dict]:
    """Значение поля для раздела 1: документ → табличка → маркировка → ввод → фото."""
    cand = [r for r in items if r["key"] == key and r.get("value")]
    if key == "year":
        cand += [dict(r, value=str(ae.to_year(r["value"]))) for r in items
                 if r["key"] == "manufacture_date" and ae.to_year(r.get("value"))]
    if not cand:
        return None
    cand.sort(key=lambda r: ae.SOURCES.index(r["source"]) if r["source"] in ae.SOURCES else 9)
    return cand[0]


# --------------------------------------------------------------------------- #
#  POST /act/photos
# --------------------------------------------------------------------------- #

def _class_of(con, class_code: str, product_code: str) -> Optional[str]:
    if class_code:
        return class_code
    if product_code:
        r = con.execute("SELECT class_code FROM product_classes WHERE product_code=? ORDER BY part_no LIMIT 1",
                        (product_code,)).fetchone()
        return r[0] if r else None
    return None
