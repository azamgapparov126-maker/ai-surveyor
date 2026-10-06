"""
Лёгкий движок сюрвейерского акта (ТЗ «Мини-приложение ИИ-сюрвейер. Лёгкая версия», 2.0 от 29.09.2026).

Чистые функции: без сети, без модели и без обращения к базе. Всё нужное приходит аргументами:
справочник движка (engine.Reference из db.load_reference), настройки акта (act_settings, см. DEFAULT_SETTINGS),
пороги франшизы (risk_analytics.load_thresholds — только чтение). Тексты по-человечески собирает app/act.py;
здесь — коды, числа и параметры.

1. risk_level — три уровня по четырём признакам ТЗ 8.1 (состояние, место, убытки за 3 года, документы).
   Каждый признак даёт +1 (повышает), −1 (снижает), 0 (не влияет) или «неизвестно».
   net = сумма; net ≤ level_rule.low_max_net → низкий; net ≥ level_rule.high_min_net → высокий;
   иначе умеренный. Если известно меньше level_rule.min_known признаков — умеренный.
   Пороги — в настройках (таблица act_settings), calibrated = 0.
2. rate — базовая ставка из того же справочника, что у калькулятора (engine.rate_for без
   коэффициентов: базовая ставка класса и типа объекта → рисковая надбавка → нагрузка), либо ставка продукта
   по тарифной политике (настройка base_source = product_rate). Поправка по уровню: adj_pct (0/20/50 %).
   Не ниже минимальной ставки продукта (engine.min_rate). Премия = сумма × ставка × дни / 365.
   Продукты «по программе», «по согласованию», «генеральный договор» — ставка не определена.
   Обязательные виды — только ставка акта (минимум регулятора или число из текста тарифа), без поправок.
3. value_check — отношение суммы к стоимости (ТЗ 8.3) и ориентир «цена минус износ».
4. franchise — только при основании (ТЗ 8.4); размер — вилка из порогов franchise_by_level.
5. clauses, required_views, discrepancies, decision — по ТЗ 5, 8.5, 8.6, 7.
9. Комплексные продукты по частям (30.09.2026): ставки частей из текста тарифа, минимум класса части, ставка части,
   деление суммы, сопоставление объектов договора с классами, сценарии, удержание и итоги договора.
10. Минимальная ставка страховщика и заниженная ставка (01.10.2026): тип ставки продукта (annual — премия = сумма ×
   ставка × дни / 365; fixed — сумма × ставка на весь срок, годовой эквивалент × 365 / дни для рынка) и
   below_min_assess — можно ли застраховать по запрошенной ставке ниже минимальной: да / да при условиях / нет,
   доводы за и против с цифрами. Правило разработчика (настройки below_min), не утверждено страховщиком.
"""
import math
import re
from datetime import date
from typing import Optional

from .engine import NEGOTIATED_MODES, STATUTORY_MODE, Input, min_rate, premium_of, rate_for

CALIBRATED = 0
LEVELS = ("low", "moderate", "high")
# уровни лёгкой версии → названия уровней в порогах франшизы (risk_analytics.DEFAULT_THRESHOLDS)
RA_LEVEL = {"low": "Низкий", "moderate": "Умеренный", "high": "Высокий"}

DEFAULT_SETTINGS = {
    "calibrated": 0,
    "source": "экспертные значения до калибровки (ТЗ лёгкой версии 2.0 от 29.09.2026)",
    "level_rule": {"low_max_net": -2, "high_min_net": 2, "min_known": 2},
    "adj_pct": {"low": 0, "moderate": 20, "high": 50},
    # technical — техническая ставка калькулятора; product_rate — ставка продукта по тарифной политике
    # По умолчанию — ставка тарифной политики: так считает заказчик (образец акта 29.09.2026:
    # спецтехника 0,35 % × 1,2 за умеренный риск = 0,42 %).
    "base_source": "product_rate",
    "new_object_years": 1,          # «объект новый»: не старше стольких лет
    "losses_high_count": 2,         # убытков за 3 года, с которых признак «повышает»
    "decline_min_up": 4,            # отказ — только когда сработали все четыре повышающих признака
    "value_ok_pct": [90, 100],      # сумма к стоимости «в норме»
    "wear_pct_per_year": {"building": 3, "vehicle": 20, "computer": 20, "equipment": 15, "other": 15},
    "insurer_name": "",
    # оценка по объявлениям со снимков экрана (30.09.2026): экспертно, calibrated = 0
    "market": {
        "min_listings": 3,              # меньше — оценка ориентировочная
        "diff_pct": 15,                 # расхождение с заявленной стоимостью, выше — «уточнить стоимость»
        "max_age_months": 6,            # объявления старше не берутся
        "outlier_low": 0.5,             # ниже доли медианы — выброс
        "outlier_high": 2.0,            # выше кратного медианы — выброс
        # объявление без видимой даты публикации в расчёт не берётся (правило valuation_sources.py)
        "allow_undated": False,
    },
    # сверка с запросом филиала (30.09.2026): дни срока — с обоими крайними днями; допуск по премии —
    # округление филиала (запрос 123 322 000 при расчёте 123 321 918)
    "request_check": {"term_inclusive": True, "premium_tolerance": 1000},
    # чтение договора страхования (30.09.2026, app/contract_read.py): если разбор текста нашёл меньше половины
    # ключевых полей (сумма, премия, срок, объект) и модель подключена — текст договора (после маскировки ПД,
    # не больше ai_max_chars знаков) уходит в модель, она дополняет только пустые поля
    "contract": {"ai_assist": True, "ai_max_chars": 30000},
    # комплексные продукты по частям (30.09.2026, ТЗ универсального шаблона 4.1в): доли страховой суммы по классам
    # из тарифной политики {код продукта: {класс: %}} — в тарифной политике INSON 23.09.2025 долей суммы нет (там
    # только ставки частей), поэтому по умолчанию пусто и сумма делится поровну с пометкой «подтвердите»;
    # sum_tolerance — допуск «сумма частей = страховая сумма договора», сумов
    "parts": {"shares": {}, "sum_tolerance": 1},
    # страховой скоринг объекта (01.10.2026, первая страница акта): цвет полос-заголовков — цвет бренда
    "scoring": {"brand_color": "#0B4F8A"},
    # отчёт кредитного бюро (КАТМ) для кредитных классов 14, 13з, 15 (01.10.2026): только проверки андеррайтеру,
    # в уровень риска и ставку не входит; low_class — класс оценки бюро, с которого (и хуже) нужна проверка;
    # max_age_days — отчёт старше стольких дней считается устаревшим. Экспертно, calibrated = 0
    # allow_scan — сканы и фото отчёта бюро отдавать языковой модели (по умолчанию нет: в отчёте кредитная история)
    "credit_report": {"low_class": "C", "max_age_days": 30, "allow_scan": False},
    # данные НАПП в разделе 4 акта (01.10.2026): branch_min_contracts — если у подразделений INSON в регионе
    # (все вместе, по отчёту НАПП) договоров меньше этого числа, у строки пометка «малая база» (убыточность и
    # средняя премия на малом числе договоров неустойчивы). Экспертно, calibrated = 0
    "napp": {"branch_min_contracts": 200},
    # вилка ставки (01.10.2026): минимум → ставка акта → с учётом региона и рынка → рынок. Экспертно, calibrated = 0.
    # mode: reference — премия акта по ставке акта, ставка с поправками показывается рядом справочно;
    #       apply — ставка с поправками становится ставкой акта (премия, франшиза, мероприятия, сверки — от неё).
    # region: поправка = среднее по подходящим показателям (отношение «регион / республика» − 1) × sensitivity,
    #         в границах [min_pct; max_pct] %; indicators — {класс: {группа объекта | "*": [id показателей
    #         app/risk_stats.py]}}: какие показатели говорят о риске этого класса и вида объекта.
    #         claims_freq (01.10.2026) — частота страховых претензий региона к республике по отчёту НАПП (листы 3.5
    #         и 3.4, всё общее страхование) — в списках классов 3, 7, 8, 9, но с ВЕСОМ 0 (замечание контролёра
    #         01.10.2026): НАПП относит претензии к месту головных офисов страховщиков и онлайн-продаж — 88,8 %
    #         претензий страны на 01.07.2026 записаны на город Ташкент, у 13 регионов отношение 0,11–0,65. Показатель
    #         виден в акте справочно; администратор может включить его, задав вес больше 0.
    #         weights — {id показателя: вес} во взвешенном среднем вкладов (нет в словаре — вес 1; вес 0 — показатель
    #         показывается, но в поправку не входит).
    # market: если ставка акта ниже рыночной (НАПП) и убыточность класса ≥ порога — надбавка steps [[порог %, +%]];
    #         loss_ratio — по последнему срезу НАПП (last) или по полному году (full_year); cap_at_market — надбавка
    #         рынка не поднимает ставку выше рыночной (потолок = рыночная ставка).
    #         basis (01.10.2026) — при loss_ratio = last: full_year_if_available — если по последнему срезу ступень
    #         выше, чем по полному году (скачок убыточности за неполный год, например пакет 3,14: 81,9 % за полугодие
    #         2026 против 2,5 % за 2025 год), берётся ступень по полному году с пометкой; last — всегда срез.
    "rate_fork": {
        "mode": "reference",
        "region": {"sensitivity": 0.5, "min_pct": -10, "max_pct": 15,
                   "indicators": {"3": {"*": ["road_accidents", "thefts", "claims_freq"]},
                                  "7": {"*": ["road_accidents", "thefts", "claims_freq"]},
                                  "8": {"property": ["vulnerable_housing", "emergencies", "claims_freq"],
                                        "*": ["emergencies", "claims_freq"]},
                                  "9": {"*": ["crimes_total", "thefts", "claims_freq"]}},
                   "weights": {"claims_freq": 0.0}},
        "market": {"steps": [[60, 10], [80, 20]], "loss_ratio": "last", "basis": "full_year_if_available",
                   "cap_at_market": True},
        "calibrated": 0,
    },
    # оценка заниженной ставки (01.10.2026, задание заказчика «если ниже тарифа — можно ли застраховать: да/нет,
    # почему»): ставка запроса филиала, договора или введённая сотрудником ниже минимальной ставки страховщика.
    # Правило разработчика, не утверждено страховщиком; окончательное решение — андеррайтер. Экспертно, calibrated = 0.
    #   min_share_pct     — запрошенная ставка не ниже стольких % минимальной (иначе «нет»);
    #   market_max_ratio  — для «да»: рыночная ставка класса не выше запрошенной (годовой) более чем во столько раз…
    #   low_loss_ratio_pct — …или убыточность класса по НАПП ниже этого порога;
    #   losses_block      — убытков за 3 года, с которых — «нет» без обсуждения;
    #   net_check         — calibrated: запрошенная ниже нетто-ставки расчётного модуля даёт «нет», только если базовая
    #                       ставка класса калибрована на статистике компании (экспертная нетто-ставка — довод «против»
    #                       и условие «подтвердить»); always — «нет» и по экспертной нетто-ставке.
    "below_min": {"min_share_pct": 60, "market_max_ratio": 2, "low_loss_ratio_pct": 40, "losses_block": 2,
                  "net_check": "calibrated", "calibrated": 0},
    # факторы объекта по подгруппам класса (02.10.2026, документ «Факторы тарифа по классам и подгруппам»; группы и
    # коэффициенты — factor_groups шаблона класса, экспертно, calibrated = 0). Итоговый множитель = произведение
    # коэффициентов заполненных групп в границах [min_product; max_product].
    #   mode: reference — ставка акта не меняется, в вилке ставки отметка «с учётом факторов объекта — справочно»
    #         (ставка акта × множитель, не ниже минимума);
    #         apply — ставка акта = тариф × поправка уровня × множитель, не ниже минимальной; премия — от неё.
    "factors": {"mode": "reference", "min_product": 0.5, "max_product": 2.5, "calibrated": 0},
    # пределы загрузки и распознавания (app/act.py): защита сервера, а не тариф
    "limits": {
        "max_image_mp": 50,             # картинка больше стольких мегапикселей отклоняется до раскрытия
        "pdf_max_pages": 10,            # PDF с большим числом страниц не принимается (сканы — в модель)
        "pdf_text_max_pages": 60,       # PDF с текстовым слоем (договор) — до стольких страниц, разбор без модели
        "guest_photos_per_hour": 60,    # фото в час на одного гостя (по числу файлов)
        "ai_calls_per_hour": 120,       # распознаваний в час на весь сервер
        "ai_timeout_sec": 20,           # ожидание ответа модели на один запрос, одна попытка
        "ai_deadline_sec": 25,          # общий срок распознавания
        "ai_max_mb": 10,                # суммарный объём вложений в одном запросе к модели
        "send_per_hour": 10,            # отправок акта ботом в час на пользователя
        # документы DOCX/XLSX (zip внутри): защита от «zip-бомб» до распаковки
        "doc_max_unzip_mb": 50,         # суммарный распакованный объём частей
        "doc_max_parts": 2000,          # частей в архиве документа
        # разбор текста документа (app/act_extras.read_limited): лишнее отбрасывается с пометкой
        "doc_max_cells": 10000,         # ячеек таблиц на файл
        "doc_max_paras": 3000,          # абзацев текста DOCX на файл
        "doc_max_rows": 200,            # строк с листа (таблицы)
        "doc_max_cols": 30,             # колонок с листа
        "doc_max_sheets": 3,            # листов книги XLSX
        "doc_max_line_chars": 500,      # знаков в ячейке и строке таблицы XLSX/PDF, в строке текста PDF
        "doc_max_row_chars": 4000,      # знаков в строке таблицы DOCX (реквизиты двух сторон в одной строке)
        "doc_max_para_chars": 4000,     # знаков в абзаце текста документа (пункт договора бывает длинным)
        "doc_max_text_chars": 200000,   # знаков текста на файл
        "doc_parse_sec": 5,             # срок разбора одного файла (DOCX, XLSX)
        "doc_file_sec_pdf": 8,          # срок разбора одного PDF с текстом (длинный договор: склейка строк)
        "doc_parse_total_sec": 12,      # срок разбора всех документов одного запроса
    },
}

LIMIT_BOUNDS = {"max_image_mp": (1, 200), "pdf_max_pages": (1, 100), "pdf_text_max_pages": (1, 300),
                "guest_photos_per_hour": (1, 10000),
                "ai_calls_per_hour": (1, 100000), "ai_timeout_sec": (5, 120), "ai_deadline_sec": (5, 180),
                "ai_max_mb": (1, 15), "send_per_hour": (1, 1000),
                "doc_max_unzip_mb": (1, 500), "doc_max_parts": (10, 100000),
                "doc_max_cells": (100, 100000), "doc_max_paras": (10, 100000),
                "doc_max_row_chars": (50, 50000), "doc_max_rows": (10, 5000), "doc_max_cols": (2, 200),
                "doc_max_sheets": (1, 50), "doc_max_line_chars": (50, 10000), "doc_max_para_chars": (50, 50000),
                "doc_max_text_chars": (1000, 1000000), "doc_parse_sec": (0.1, 60),
                "doc_file_sec_pdf": (0.1, 60), "doc_parse_total_sec": (0.1, 120)}
# пределы настроек оценки по объявлениям: (от, до, целое)
MARKET_BOUNDS = {"min_listings": (1, 20, True), "diff_pct": (1, 100, False), "max_age_months": (1, 24, True),
                 "outlier_low": (0.05, 0.95, False), "outlier_high": (1.05, 20, False)}
MARKET_FLAGS = ("allow_undated",)       # флаги оценки по объявлениям: только true/false

VIEWS = ("front", "back", "left", "right", "plate", "odometer", "document", "interior", "facade", "roof",
         "electrical", "fire_safety", "general", "installation", "packaging", "marking", "transport", "other")
# document_ai — значение из текста договора, прочитанное моделью (app/act.py, contract.ai_assist)
SOURCES = ("document", "document_ai", "plate", "marking", "input", "photo")    # порядок = приоритет показа
FIELD_KEYS = ("object_type", "brand", "model", "manufacture_date", "year", "serial_no", "manufacturer",
              "engine_no", "engine_model", "engine_power", "curb_mass", "payload", "dimensions", "color",
              "mileage", "location",
              # из разобранных документов (договор, заявление, техпаспорт, кадастр) — 29.09.2026
              "sum_insured", "object_value", "term_days", "region", "construction", "reg_no", "cadastre_no",
              # запрос филиала (app/branch_request.py) — 30.09.2026
              "product_code", "policyholder", "beneficiary", "pledger", "land_area", "useful_area", "total_area",
              "franchise", "tariff_pct", "premium", "term_from", "term_to", "contract_terms", "contracts_count",
              "additional_info")
# числовые поля документа: сверяются как числа, а не как текст
NUMBER_KEYS = ("sum_insured", "object_value", "term_days")
# в разделе 1 показываются, только если значение есть
EXTRA_ROW_KEYS = ("construction", "reg_no", "cadastre_no")
LOCATIONS = ("open_area", "construction", "port", "guarded", "closed_storage", "other")
LOC_UP = ("open_area", "construction", "port")
LOC_DOWN = ("guarded", "closed_storage")

REQUIRED_VIEWS = {
    "special": ["front", "back", "left", "right", "plate", "odometer", "document"],
    "vehicle": ["front", "back", "left", "right", "plate", "odometer", "document"],
    "property": ["facade", "roof", "interior", "electrical", "fire_safety"],
    "equipment": ["general", "plate", "installation"],
    "cargo": ["packaging", "marking", "transport"],
    "liability": [],
    "other": [],
}
# какой снимок закрывает нужный ракурс: общий вид оборудования снимают и «спереди», и «сбоку»
VIEW_COVERS = {"general": {"general", "front", "back", "left", "right"}, "facade": {"facade", "front"},
               "transport": {"transport"}}

# какие поля показывать в разделе 1 по группе объекта
FIELDS_BY_GROUP = {
    "special": ["object_type", "brand", "model", "year", "serial_no", "manufacturer", "engine_model",
                "engine_no", "engine_power", "curb_mass", "payload", "dimensions", "color", "mileage", "location"],
    "vehicle": ["object_type", "brand", "model", "year", "serial_no", "manufacturer", "engine_model",
                "engine_no", "engine_power", "curb_mass", "payload", "color", "mileage", "location"],
    "equipment": ["object_type", "brand", "model", "year", "serial_no", "manufacturer", "engine_power",
                  "dimensions", "location"],
    "property": ["object_type", "year", "dimensions", "location"],
    "cargo": ["object_type", "curb_mass", "dimensions", "location"],
    "liability": ["object_type", "location"],
    "other": ["object_type", "brand", "model", "year", "serial_no", "location"],
}
# признаки с высокой ценой ошибки: их отсутствие попадает в «что проверить»
KEY_FIELDS = {"special": ["serial_no", "year", "model"], "vehicle": ["serial_no", "year", "model"],
              "equipment": ["serial_no", "year", "model"], "property": ["year"], "cargo": [], "liability": [],
              "other": []}

SPECIAL_WORDS = ("спецтех", "кран", "экскават", "бульдоз", "погрузч", "грейдер", "каток", "автовыш", "бетон",
                 "буров", "трактор", "комбайн", "crane", "excavat", "bulldoz", "loader", "grader", "tractor",
                 "special")
EQUIP_WORDS = ("оборуд", "станок", "машины и", "генератор", "equipment", "machine")
COMPUTER_WORDS = ("компьют", "ноутбук", "сервер", "computer", "laptop", "server")
FURNITURE_WORDS = ("мебел", "furniture")


# ================================================================================================
#  Группа объекта, ракурсы, оговорки
# ================================================================================================

def object_group(class_code: Optional[str], object_type: str = "", class_hint: str = "") -> str:
    """Группа для ракурсов и оговорок: special | vehicle | property | equipment | cargo | liability | other."""
    cls = str(class_code or "").strip()
    text = f"{object_type or ''} {class_hint or ''}".lower()
    hint = str(class_hint or "").strip().lower()
    if cls in ("3", "4") or (not cls and hint in ("vehicle", "special_machinery")):
        special = hint == "special_machinery" or any(w in text for w in SPECIAL_WORDS)
        return "special" if special else "vehicle"
    if cls == "7" or (not cls and hint == "cargo"):
        return "cargo"
    if cls in ("8", "9") or (not cls and hint in ("building", "equipment")):
        return "equipment" if hint == "equipment" or any(w in text for w in EQUIP_WORDS) else "property"
    if cls in ("10", "11", "12", "13", "13з"):
        return "liability"
    return "other"


def required_views(group: str) -> list:
    return list(REQUIRED_VIEWS.get(group) or [])


def missing_views(group: str, seen, required: Optional[list] = None) -> list:
    """Каких ракурсов не хватает. required — ракурсы шаблона класса (app/class_templates.py); None — по группе."""
    seen = set(seen or [])
    out = []
    for v in (required_views(group) if required is None else required):
        if not (VIEW_COVERS.get(v, {v}) & seen):
            out.append(v)
    return out


def clauses(group: str, catalog: dict) -> list:
    """Оговорки из готового списка по группе объекта (docs/act_clauses.json). Все — экспертные."""
    items = ((catalog or {}).get("groups") or {}).get(group) or []
    return [dict(c, expert=True, calibrated=CALIBRATED) for c in items if isinstance(c, dict) and c.get("code")]


def clauses_by_codes(codes: list, catalog: dict) -> list:
    """Оговорки по кодам шаблона класса — в порядке шаблона, из любой группы списка. Неизвестный код пропускается."""
    by = {}
    for items in ((catalog or {}).get("groups") or {}).values():
        for c in items or []:
            if isinstance(c, dict) and c.get("code") and c["code"] not in by:
                by[c["code"]] = c
    out, seen = [], set()
    for code in codes or []:
        if code in by and code not in seen:
            seen.add(code)
            out.append(dict(by[code], expert=True, calibrated=CALIBRATED))
    return out


# ================================================================================================
#  Разбор значений
# ================================================================================================

def to_year(value) -> Optional[int]:
    """Год из «2026», «2026-03», «03.2026», «март 2026 г.»; неправдоподобное — None."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        y = int(value)
    else:
        m = re.search(r"(?<!\d)(19[5-9]\d|20\d\d)(?!\d)", str(value))
        if not m:
            return None
        y = int(m.group(1))
    return y if 1950 <= y <= date.today().year + 1 else None


_UNIT_MASS = {"kg": 1.0, "кг": 1.0, "t": 1000.0, "т": 1000.0, "тонн": 1000.0, "ton": 1000.0, "tonn": 1000.0}
_UNIT_POWER = {"kw": 1.0, "квт": 1.0, "hp": 0.7355, "л.с": 0.7355, "лс": 0.7355, "ps": 0.7355, "ot kuchi": 0.7355}


def to_number(value) -> Optional[float]:
    """Число из строки «36 170 кг», «38,600 kg», «248 kW», «2.5 t». Разряды пробелом или запятой."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    s = str(value).replace(" ", " ").replace(" ", " ").replace(" ", " ")
    m = re.search(r"\d[\d\s.,]*", s)
    if not m:
        return None
    raw = re.sub(r"\s+", "", m.group(0)).rstrip(".,")
    if "," in raw and "." in raw:
        raw = raw.replace(",", "") if raw.rfind(".") > raw.rfind(",") else raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        parts = raw.split(",")
        raw = raw.replace(",", "") if all(len(p) == 3 for p in parts[1:]) else raw.replace(",", ".")
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


def _unit_factor(value, table: dict) -> Optional[float]:
    s = str(value or "").lower().replace(" ", "")
    for unit in sorted(table, key=len, reverse=True):
        if unit.replace(" ", "") in s:
            return table[unit]
    return None


def _norm_text(value, drop=()) -> str:
    s = str(value or "").upper()
    for d in drop:
        if d:
            s = s.replace(str(d).upper(), " ")
    return re.sub(r"[\s\-_/.,:;·•]+", "", s)


def _same(key: str, a, b, brands=()) -> bool:
    """Одно ли это значение. Числа с единицами сравниваются после приведения; строки — по знакам."""
    if key in ("curb_mass", "payload", "engine_power", "mileage"):
        table = _UNIT_POWER if key == "engine_power" else _UNIT_MASS
        na, nb = to_number(a), to_number(b)
        if na is None or nb is None:
            return _norm_text(a) == _norm_text(b)
        fa, fb = _unit_factor(a, table), _unit_factor(b, table)
        if fa is None or fb is None or fa == fb:
            return abs(na - nb) < 1e-9
        va, vb = na * fa, nb * fb                  # разные единицы: пересчёт даёт округление — допуск 1 %
        return abs(va - vb) <= 0.01 * max(abs(va), abs(vb))
    if key == "year":
        return to_year(a) == to_year(b)
    if key in NUMBER_KEYS:
        na, nb = to_number(a), to_number(b)
        if na is None or nb is None:
            return _norm_text(a) == _norm_text(b)
        return abs(na - nb) < 0.5
    drop = brands if key in ("model", "engine_model") else ()
    return _norm_text(a, drop) == _norm_text(b, drop)


# ================================================================================================
#  1а. Повреждения с фото (06.10.2026): тяжесть, вес в уровне риска, штраф балла скоринга
# ================================================================================================

# Тяжесть повреждения по описанию модели: «существенные» — ремонт кузова и агрегатов (вес в уровне как у признака
# «состояние изношено»), «косметические» — ЛКП, мелкие царапины и сколы (слабый вес). Экспертно, calibrated = 0.
DAMAGE_COSMETIC_WEIGHT = 0.5          # косметические — половина повышающего признака
DAMAGE_MAJOR_WEIGHT = 1.0             # существенные — как «состояние изношено»
DAMAGE_SCORE_PENALTY = {"cosmetic": 10, "major": 30}      # штраф балла скоринга 0–500 (экспертно, не калибровано)
_DMG_MINOR_QUAL = ("мелк", "незначит", "небольш", "поверхност", "minor", "small", "slight", "superficial",
                   "kichik", "mayda")
_DMG_COSMETIC = ("царап", "потерт", "скол", "лкп", "краск", "полиров", "scratch", "scuff", "chip",
                 "paint", "cosmetic", "tirnal", "chizil", "qirilgan", "bo'yoq")
_DMG_MAJOR = ("вмятин", "деформ", "трещин", "разбит", "разрыв", "коррози", "ржав", "течь", "сломан", "отсутств",
              "прогни", "обгор", "оторван", "пробит", "dent", "crack", "broken", "rust", "corros", "leak", "missing",
              "deform", "burn", "torn", "ezil", "singan", "yoriq", "zang", "pachoq", "teshil", "kuyg")
DAMAGE_SEVERITIES = ("cosmetic", "major")


def damage_severity(d) -> str:
    """
    Тяжесть одного повреждения: cosmetic | major. Чистая функция. Явная тяжесть модели (severity: cosmetic | minor |
    major | substantial) важнее слов. Слова: «вмятина», «трещина», «коррозия» — существенные; «царапина», «скол» —
    косметические; уточнение «мелкая», «незначительная» делает повреждение косметическим. Непонятное описание —
    существенное (осторожно: пусть андеррайтер посмотрит). Примеры: «царапины левого крыла» → cosmetic;
    «вмятина заднего бампера» → major; «мелкая вмятина двери» → cosmetic.
    """
    if isinstance(d, str):
        d = {"what": d}
    if not isinstance(d, dict):
        return "major"
    sev = str(d.get("severity") or "").strip().lower()
    if sev in ("cosmetic", "minor", "light"):
        return "cosmetic"
    if sev in ("major", "substantial", "significant", "severe"):
        return "major"
    low = f"{d.get('what') or ''} {d.get('where') or ''}".lower().replace("ё", "е").replace("ʻ", "'").replace("ʼ", "'")
    if any(w in low for w in _DMG_MINOR_QUAL):
        return "cosmetic"
    if any(w in low for w in _DMG_MAJOR):
        return "major"
    if any(w in low for w in _DMG_COSMETIC):
        return "cosmetic"
    return "major"


def damages_summary(damages) -> dict:
    """Повреждения с фото → {count, cosmetic, major, severity (major | cosmetic | None), items[{what, where,
    severity}], weight (вес в уровне риска), penalty (штраф балла скоринга), calibrated}. Чистая функция.
    Пример: [«царапины левого крыла», «вмятина заднего бампера»] → 2 шт., 1 косметическое и 1 существенное,
    severity major, вес 1, штраф 30."""
    items = []
    for d in damages or []:
        if not d:
            continue
        dd = {"what": d} if isinstance(d, str) else dict(d)
        items.append({"what": dd.get("what"), "where": dd.get("where"), "severity": damage_severity(dd)})
    major = sum(1 for x in items if x["severity"] == "major")
    cosmetic = len(items) - major
    severity = "major" if major else ("cosmetic" if cosmetic else None)
    return {"count": len(items), "cosmetic": cosmetic, "major": major, "severity": severity, "items": items,
            "weight": DAMAGE_MAJOR_WEIGHT if major else (DAMAGE_COSMETIC_WEIGHT if cosmetic else 0.0),
            "penalty": DAMAGE_SCORE_PENALTY.get(severity, 0) if severity else 0, "calibrated": CALIBRATED}


def _whole(x):
    """1.0 → 1, 0.5 → 0.5: целые веса признаков показываются без дроби, как раньше."""
    x = round(float(x), 2)
    return int(x) if x == int(x) else x


# ================================================================================================
#  1б. Подгруппа транспорта (06.10.2026): советы, мероприятия и сценарии класса 3 по подгруппе
# ================================================================================================

VEH_GROUPS = ("car", "truck", "bus", "trailer", "special_wheeled", "special_tracked", "agro", "moto")
VEH_SPECIAL = ("special_wheeled", "special_tracked", "agro")
# вид объекта экрана (шаблон класса 3, object.kinds) → подгруппа
VEH_GROUP_BY_KIND = {
    "car": "car", "electric_car": "car", "truck": "truck", "concrete_mixer": "truck", "trailer": "trailer",
    "bus": "bus", "moto": "moto", "motorcycle": "moto",
    "truck_crane": "special_wheeled", "aerial_platform": "special_wheeled", "concrete_pump": "special_wheeled",
    "wheel_loader": "special_wheeled", "forklift": "special_wheeled", "telehandler": "special_wheeled",
    "backhoe_loader": "special_wheeled", "drilling_rig": "special_wheeled", "road_machinery": "special_wheeled",
    "special_other": "special_wheeled", "special": "special_wheeled",
    "crawler_crane": "special_tracked", "excavator": "special_tracked", "bulldozer": "special_tracked",
    "tractor": "agro", "combine": "agro",
}
# факторы справочника только для спецтехники (у легковых и грузовых их не уточняют и не советуют) и только для
# дорожного транспорта (у спецтехники машиной управляет оператор, «круг водителей» не советуется)
VEH_SPECIAL_ONLY_FACTORS = ("spec_site", "spec_guard", "spec_operator", "engine_hours")
VEH_ROAD_ONLY_FACTORS = ("drivers",)


def vehicle_group(class_code: Optional[str], class_fields: Optional[dict] = None, kind: Optional[str] = None,
                  vehicle_category: Optional[dict] = None, group: Optional[str] = None,
                  text: str = "") -> Optional[str]:
    """
    Подгруппа транспорта класса 3: car | truck | bus | trailer | special_wheeled | special_tracked | agro | moto.
    Чистая функция. Порядок: поле класса veh_group (сотрудник или техпаспорт) → вид объекта экрана → категория с фото
    (vehicle_category модели) → группа объекта special (колёсная спецтехника) → слова в описании объекта →
    по умолчанию легковой (КАСКО и прочие продукты ТС). Не класс 3 — None.
    """
    if str(class_code or "") != "3":
        return None
    cf = class_fields or {}
    v = str(cf.get("veh_group") or "").strip().lower()
    if v == "agri":
        v = "agro"
    if v in VEH_GROUPS:
        return v
    if kind and VEH_GROUP_BY_KIND.get(str(kind)):
        return VEH_GROUP_BY_KIND[str(kind)]
    vc = (vehicle_category or {}).get("code") if isinstance(vehicle_category, dict) else None
    if vc in VEH_GROUPS:
        return vc
    if text:
        from .vehicle_prefill import group_from_text
        g = group_from_text(text)
        if g in VEH_GROUPS and (g in VEH_SPECIAL or group != "special"):
            return g
    if group == "special":
        return "special_wheeled"
    return "car"


def vehicle_factor_applies(factor: str, veh_group: Optional[str]) -> bool:
    """Фактор справочника класса 3 уместен для подгруппы: спецфакторы (площадка, охрана площадки, оператор,
    моточасы) — только спецтехнике, «допущенные водители» — только дорожному транспорту. Не класс 3 — всегда да."""
    if not veh_group:
        return True
    if factor in VEH_SPECIAL_ONLY_FACTORS:
        return veh_group in VEH_SPECIAL
    if factor in VEH_ROAD_ONLY_FACTORS:
        return veh_group not in VEH_SPECIAL
    return True


# ================================================================================================
#  1. Уровень риска
# ================================================================================================

def risk_level(inputs: dict, settings: Optional[dict] = None) -> dict:
    """
    inputs: {"inspected": bool (фото распознаны), "damages": [..], "condition": "new|good|worn|damaged"|None,
             "year": int|None, "location": код LOCATIONS|None, "guard": bool|None,
             "losses_count": int|None, "documents": bool, "today": date}
    Возвращает {"level", "net", "up", "down", "known", "few", "factors": [{"code", "sign", "params"}],
                "rule", "calibrated"}. sign: up | down | neutral | unknown.
    """
    st = merge_settings(settings)
    rule = st["level_rule"]
    today = inputs.get("today") or date.today()
    factors = []

    # состояние объекта
    damages = [d for d in (inputs.get("damages") or []) if d]
    # повреждения с фото (06.10.2026): существенные — признак «повреждения» весом как «изношено»; только
    # косметические — отдельный слабый повышающий признак (вес 0,5) рядом с состоянием объекта
    dsum = damages_summary(damages)
    cond = str(inputs.get("condition") or "").lower() or None
    year = inputs.get("year")
    new = year is not None and today.year - int(year) <= int(st["new_object_years"])
    if dsum["major"] or cond == "damaged":
        factors.append({"code": "f_cond_damage", "sign": "up", "weight": DAMAGE_MAJOR_WEIGHT,
                        "params": {"n": len(damages) or 1}})
    elif cond == "worn":
        factors.append({"code": "f_cond_worn", "sign": "up", "params": {}})
    elif dsum["cosmetic"]:
        # только косметические повреждения: слабый повышающий признак вместо «новый» / «не влияет»
        factors.append({"code": "f_cond_damage_minor", "sign": "up", "weight": DAMAGE_COSMETIC_WEIGHT,
                        "params": {"n": dsum["cosmetic"]}})
    elif new and (inputs.get("inspected") or cond in ("new", "good")):
        factors.append({"code": "f_cond_new", "sign": "down", "params": {"year": year}})
    elif inputs.get("inspected") or cond in ("new", "good"):
        # год неизвестен — «не новый» утверждать нельзя
        code = "f_cond_neutral" if year is not None else "f_cond_no_year"
        factors.append({"code": code, "sign": "neutral", "params": {}})
    else:
        factors.append({"code": "f_cond_unknown", "sign": "unknown", "params": {}})

    # место эксплуатации
    loc = inputs.get("location")
    guard = inputs.get("guard")
    if loc in LOC_UP and guard:
        factors.append({"code": "f_loc_guarded_open", "sign": "neutral", "params": {"place": loc}})
    elif loc in LOC_UP:
        factors.append({"code": "f_loc_up", "sign": "up", "params": {"place": loc}})
    elif loc in LOC_DOWN:
        factors.append({"code": "f_loc_down", "sign": "down", "params": {"place": loc}})
    elif guard:
        factors.append({"code": "f_loc_down", "sign": "down", "params": {"place": "guarded"}})
    elif loc:
        factors.append({"code": "f_loc_neutral", "sign": "neutral", "params": {"place": loc}})
    else:
        factors.append({"code": "f_loc_unknown", "sign": "unknown", "params": {}})

    # история убытков
    n = inputs.get("losses_count")
    if n is None:
        factors.append({"code": "f_loss_unknown", "sign": "unknown", "params": {}})
    elif n >= int(st["losses_high_count"]):
        factors.append({"code": "f_loss_up", "sign": "up", "params": {"n": n}})
    elif n == 0:
        factors.append({"code": "f_loss_down", "sign": "down", "params": {}})
    else:
        factors.append({"code": "f_loss_neutral", "sign": "neutral", "params": {"n": n}})

    # документы: не представлены — это тоже известный факт
    if inputs.get("documents"):
        factors.append({"code": "f_docs_down", "sign": "down", "params": {}})
    else:
        factors.append({"code": "f_docs_up", "sign": "up", "params": {}})

    # вес признака — 1, у косметических повреждений — 0,5 (целые суммы показываются без дроби, как раньше)
    up = _whole(sum(float(f.get("weight", 1)) for f in factors if f["sign"] == "up"))
    down = _whole(sum(float(f.get("weight", 1)) for f in factors if f["sign"] == "down"))
    known = sum(1 for f in factors if f["sign"] != "unknown")
    net = _whole(up - down)
    few = known < int(rule["min_known"])
    if few:
        level = "moderate"
    elif net <= int(rule["low_max_net"]):
        level = "low"
    elif net >= int(rule["high_min_net"]):
        level = "high"
    else:
        level = "moderate"
    return {"level": level, "net": net, "up": up, "down": down, "known": known, "few": few,
            "factors": factors, "rule": dict(rule), "calibrated": CALIBRATED}


# ================================================================================================
#  2. Ставка и премия
# ================================================================================================

def match_object_type(ref, class_code: str, object_kind_type: Optional[str] = None,
                      object_type: Optional[str] = None) -> Optional[str]:
    """Тип объекта справочника базовых ставок: явный ввод → подсказка модели → None (средняя по классу)."""
    types = [ot for (c, ot) in ref.base_rates if c == class_code]
    low = {ot.lower(): ot for ot in types}
    for cand in (object_type, object_kind_type):
        if cand and str(cand).strip().lower() in low:
            return low[str(cand).strip().lower()]
    return None


def _percent_in(text: str) -> Optional[float]:
    """Одна ставка в тексте тарифа: «0,4% (ПКМ №532)» → 0.4. Несколько чисел или ни одного — None."""
    found = re.findall(r"(\d+(?:[.,]\d+)?)\s*%", str(text or ""))
    if len(found) != 1:
        return None
    return float(found[0].replace(",", "."))


def premium_by_type(rate_pct: float, sum_insured: float, term_days: int, rate_type: str = "annual") -> float:
    """Премия по типу ставки продукта (app/min_rates.py): annual — сумма × ставка × дни / 365 (engine.premium_of);
    fixed — сумма × ставка на весь срок, без деления на срок. 100 млн, 0,5 %, 1 095 дн.: 1 500 000 против 500 000."""
    if rate_type == "fixed":
        return rate_pct / 100 * sum_insured
    return premium_of(rate_pct, sum_insured, term_days)


def annual_pct(rate_pct: Optional[float], term_days: int, rate_type: str = "annual") -> Optional[float]:
    """Годовой эквивалент ставки для сравнения с рынком и нетто-ставкой: fixed × 365 / дни; annual — как есть."""
    if rate_pct is None:
        return None
    if rate_type == "fixed":
        return round(float(rate_pct) * 365 / max(int(term_days or 365), 1), 6)
    return float(rate_pct)


def rate(ref, product: Optional[dict], class_code: str, level: str, sum_insured: float,
         term_days: int = 365, object_type: Optional[str] = None, payer_type: Optional[str] = None,
         settings: Optional[dict] = None, rate_type: str = "annual") -> dict:
    """
    rate_type — тип ставки продукта (app/min_rates.py): annual (по умолчанию) | fixed — ставка на весь срок, премия
    = сумма × ставка без деления на срок; годовой эквивалент (× 365 / дни) — в annual_equiv_pct для сравнения с рынком.
    product: {"code", "name", "pricing_mode", "rate_text"} или None (выбран только класс).
    Возвращает {"mode": tariff|statutory|undefined, "base_pct", "base_source", "adj_pct", "calc_pct",
                "applied_pct", "min_pct", "min_applied", "premium", "term_days", "object_type",
                "class_code", "product_code", "calibrated", "how": [{"code", "params"}]}.
    """
    st = merge_settings(settings)
    code = (product or {}).get("code")
    mode_raw = (product or {}).get("pricing_mode")
    out = {"mode": "tariff", "base_pct": None, "base_source": None, "adj_pct": None, "calc_pct": None,
           "applied_pct": None, "min_pct": None, "min_applied": False, "premium": None,
           "term_days": int(term_days), "object_type": object_type, "class_code": class_code,
           "product_code": code, "pricing_mode": mode_raw, "calibrated": CALIBRATED, "how": [],
           "engine_chain": [], "rate_type": "annual", "annual_equiv_pct": None}
    rt = "fixed" if rate_type == "fixed" and mode_raw != STATUTORY_MODE else "annual"

    if mode_raw in NEGOTIATED_MODES:
        out["mode"] = "undefined"
        out["how"].append({"code": "how_undefined", "params": {"code": code,
                                                               "rate_text": product.get("rate_text") or mode_raw}})
        return out

    if mode_raw == STATUTORY_MODE:
        out["mode"] = "statutory"
        out["adj_pct"] = 0
        mr = min_rate(ref, code, payer_type)
        by_act = mr["regulator"] if mr["regulator"] is not None else _percent_in(product.get("rate_text"))
        ref_txt = product.get("rate_text") or code
        if by_act is None:
            out["mode"] = "statutory_undefined"
            out["how"].append({"code": "how_statutory_na", "params": {"ref": ref_txt}})
            return out
        out.update(base_pct=by_act, calc_pct=by_act, applied_pct=round(by_act, 4), min_pct=by_act,
                   base_source="act")
        out["premium"] = round(premium_of(out["applied_pct"], sum_insured, term_days))
        out["how"].append({"code": "how_statutory", "params": {"rate": by_act, "ref": ref_txt}})
        out["how"].append({"code": "how_premium", "params": {"sum": sum_insured, "rate": out["applied_pct"],
                                                             "days": term_days, "premium": out["premium"]}})
        return out

    # ставка: базовая → поправка по уровню → не ниже минимума продукта
    mr = min_rate(ref, code, payer_type) if code else {"floor": None, "company": None}
    floor = mr["floor"]
    if st["base_source"] == "product_rate" and code and mr.get("company") is not None:
        base = float(mr["company"])
        out["base_source"] = "product_rate"
        out["how"].append({"code": "how_base_product", "params": {"base": base, "code": code}})
    else:
        has_class = any(c == class_code for (c, _) in ref.base_rates)
        if not has_class:
            out["mode"] = "undefined"
            out["how"].append({"code": "how_no_base", "params": {"cls": class_code}})
            return out
        # тот же расчёт, что у калькулятора, но без коэффициентов: одна поправка по уровню заменяет их все
        r = rate_for(ref, Input(product_code=code or "", class_code=class_code,
                                object_type=object_type or "", value_amount=1, sum_insured=1,
                                term_days=term_days, factors={}))
        base = r["gross_pct"]
        out["engine_chain"] = r["chain"]
        out["base_source"] = "technical"
        if object_type:
            out["how"].append({"code": "how_base_tech", "params": {"base": round(base, 4), "cls": class_code,
                                                                   "otype": object_type}})
        else:
            out["how"].append({"code": "how_base_tech_avg", "params": {"base": round(base, 4),
                                                                       "cls": class_code}})
    adj = float((st["adj_pct"] or {}).get(level, 0))
    calc = base * (1 + adj / 100)
    out["base_pct"] = round(base, 4)
    out["adj_pct"] = adj
    out["calc_pct"] = round(calc, 4)
    out["how"].append({"code": "how_adj", "params": {"level": level, "adj": adj}})
    applied = calc
    if floor is not None:
        out["min_pct"] = floor
        if calc + 1e-12 < floor:
            applied = floor
            out["min_applied"] = True
            out["how"].append({"code": "how_min_applied", "params": {"calc": round(calc, 4), "min": floor}})
        else:
            out["how"].append({"code": "how_min_ok", "params": {"min": floor}})
    else:
        out["how"].append({"code": "how_min_none", "params": {}})
    out["applied_pct"] = round(applied, 4)
    # премия считается от уже округлённой ставки — так ручной пересчёт по акту даёт ту же цифру
    out["rate_type"] = rt
    out["premium"] = round(premium_by_type(out["applied_pct"], sum_insured, term_days, rt))
    if rt == "fixed":
        out["annual_equiv_pct"] = round(annual_pct(out["applied_pct"], term_days, rt), 4)
        out["how"].append({"code": "how_premium_fixed", "params": {"sum": sum_insured, "rate": out["applied_pct"],
                                                                   "premium": out["premium"]}})
        out["how"].append({"code": "how_rate_annual_equiv", "params": {"rate": out["applied_pct"], "days": term_days,
                                                                       "calc": out["annual_equiv_pct"]}})
    else:
        out["how"].append({"code": "how_premium", "params": {"sum": sum_insured, "rate": out["applied_pct"],
                                                             "days": term_days, "premium": out["premium"]}})
    return out


# ================================================================================================
#  3. Стоимость и страховая сумма
# ================================================================================================

def wear_kind(group: str, object_type: str = "") -> str:
    text = str(object_type or "").lower()
    if any(w in text for w in COMPUTER_WORDS):
        return "computer"
    if group in ("special", "vehicle"):
        return "vehicle"
    if group == "property":
        return "building"
    if group == "equipment" or any(w in text for w in FURNITURE_WORDS):
        return "equipment"
    return "other"


def value_check(sum_insured: float, value: float, settings: Optional[dict] = None,
                price_new: Optional[float] = None, purchase_year: Optional[int] = None,
                group: str = "other", object_type: str = "", today: Optional[date] = None) -> dict:
    """Отношение суммы к стоимости (ТЗ 8.3): 90–100 % в норме; < 90 % недострахование (ГК ст. 936);
    > 100 % превышение (ГК ст. 938). Ниже 100 %, но не ниже 90 % — в норме, но доля выплаты та же."""
    st = merge_settings(settings)
    lo, hi = st["value_ok_pct"]
    ratio = sum_insured / value * 100
    if ratio > hi + 1e-9:
        verdict, ref = "over", "ГК РУз, ст. 938"
    elif ratio < lo - 1e-9:
        verdict, ref = "under", "ГК РУз, ст. 936"
    else:
        verdict, ref = "normal", ("ГК РУз, ст. 936" if ratio < 100 - 1e-9 else None)
    out = {"ratio_pct": round(ratio, 2), "verdict": verdict, "legal_ref": ref,
           "diff": round(sum_insured - value) if verdict == "over" else None, "depreciated": None}
    if price_new and purchase_year:
        today = today or date.today()
        years = max(0, today.year - int(purchase_year))
        kind = wear_kind(group, object_type)
        per_year = float((st["wear_pct_per_year"] or {}).get(kind, st["wear_pct_per_year"].get("other", 15)))
        left = max(0.0, 1 - per_year / 100 * years)
        out["depreciated"] = {"price_new": price_new, "purchase_year": int(purchase_year), "years": years,
                              "kind": kind, "wear_pct_per_year": per_year, "value": round(price_new * left),
                              "calibrated": CALIBRATED}
    return out


# ================================================================================================
#  4. Франшиза
# ================================================================================================

def franchise(inputs: dict, level: str, thresholds: dict, statutory: bool = False,
              class_code: Optional[str] = None, sum_insured: float = 0) -> dict:
    """
    ТЗ 8.4: по умолчанию «не требуется». Основания: ≥ franchise_loss_count_high мелких убытков за 3 года;
    высокий уровень; явно преобладающий риск (inputs.dominant_risk); просьба клиента снизить премию.
    Размер — вилка franchise_by_level (пороги только читаются), с потолком класса franchise_class_caps.
    Вилка пустая — «рассмотреть франшизу, размер определяет андеррайтер».
    """
    if statutory:
        return {"needed": False, "code": "fr_statutory", "grounds": [], "size": None}
    th = thresholds or {}
    grounds = []
    small = inputs.get("small_count")
    need_small = int(th.get("franchise_loss_count_high", 2))
    if small is not None and small >= need_small:
        grounds.append({"code": "fr_g_small_losses", "params": {"n": small}})
    if level == "high":
        grounds.append({"code": "fr_g_level_high", "params": {}})
    if inputs.get("dominant_risk"):
        grounds.append({"code": "fr_g_dominant", "params": {"what": str(inputs["dominant_risk"])[:80]}})
    if inputs.get("want_lower_premium"):
        grounds.append({"code": "fr_g_client", "params": {}})
    if not grounds:
        return {"needed": False, "code": "fr_not_needed", "grounds": [], "size": None}
    band = (th.get("franchise_by_level") or {}).get(RA_LEVEL.get(level)) or []
    size = None
    try:
        lo, hi = float(band[0]), float(band[1])
    except (IndexError, TypeError, ValueError):
        lo = hi = 0.0
    cap = (th.get("franchise_class_caps") or {}).get(str(class_code or ""))
    if cap is not None:
        hi = min(hi, float(cap))
        lo = min(lo, hi)
    if hi > 0:
        size = {"from_pct": lo, "to_pct": hi, "from_amount": round(sum_insured * lo / 100),
                "to_amount": round(sum_insured * hi / 100), "cap_pct": cap,
                "source": "risk_thresholds.franchise_by_level", "calibrated": CALIBRATED}
    return {"needed": True, "code": "fr_advise_range" if size else "fr_advise_nosize",
            "grounds": grounds, "size": size}


# ================================================================================================
#  5. Расхождения
# ================================================================================================

COMPARE_KEYS = ("curb_mass", "model", "serial_no", "year", "engine_power", "engine_model",
                "sum_insured", "object_value", "term_days", "reg_no", "cadastre_no")


def discrepancies(recognized: list, inputs: Optional[dict] = None) -> list:
    """
    Одно поле из разных источников (табличка / документ / маркировка / фото / ввод сотрудника).
    Год берётся и из «дата изготовления». Возвращает [{"key", "values": [{"source", "value"}], "priority"}].
    Приоритет — у документа с печатью производителя (ТЗ 8.6); решение — за андеррайтером.
    """
    inputs = inputs or {}
    brands = [str(r.get("value")) for r in recognized or [] if r.get("key") in ("brand", "manufacturer")
              and r.get("value")]
    by_key = {}
    for r in recognized or []:
        key = r.get("key")
        val = r.get("value")
        if val in (None, ""):
            continue
        if key == "manufacture_date":
            key = "year"
        if key not in COMPARE_KEYS:
            continue
        by_key.setdefault(key, []).append({"source": r.get("source") or "photo", "value": str(val)})
    if inputs.get("year"):
        by_key.setdefault("year", []).append({"source": "input", "value": str(inputs["year"])})
    # суммы и срок из шага 2: сверяются с документом, только если в документе они есть
    for key in NUMBER_KEYS:
        if inputs.get(key) is not None and by_key.get(key):
            by_key[key].append({"source": "input", "value": _plain_number(inputs[key])})
    out = []
    for key in COMPARE_KEYS:
        vals = by_key.get(key) or []
        # одинаковые значения из одного источника схлопываем
        uniq, seen = [], set()
        for v in vals:
            sig = (v["source"], _norm_text(v["value"]))
            if sig not in seen:
                seen.add(sig)
                uniq.append(v)
        if len(uniq) < 2:
            continue
        # группы одинаковых значений: в строке показываем каждое значение один раз с его источниками
        groups = []
        for v in uniq:
            for g in groups:
                if _same(key, g["value"], v["value"], brands):
                    if v["source"] not in g["sources"]:
                        g["sources"].append(v["source"])
                    break
            else:
                groups.append({"value": v["value"], "sources": [v["source"]]})
        if len(groups) < 2:
            continue
        groups.sort(key=lambda g: min(SOURCES.index(s) if s in SOURCES else 9 for s in g["sources"]))
        priority = "document" if any("document" in g["sources"] for g in groups) else None
        out.append({"key": key, "values": groups, "priority": priority})
    return out


def _plain_number(x) -> str:
    """2945000000.0 → «2 945 000 000»: так число читается в строке расхождения."""
    v = float(x)
    return f"{v:,.0f}".replace(",", " ") if v == int(v) else f"{v:,.2f}".replace(",", " ")


# ================================================================================================
#  6. Решение
# ================================================================================================

def decision(risk: dict, rate_res: dict, value: dict, fr: dict, disc: list, inspection: dict,
             missing_key: list, settings: Optional[dict] = None) -> dict:
    """
    accept — низкий уровень и ни одного вопроса; decline — сработали все повышающие признаки
    (decline_min_up); иначе accept_with_clauses. checks — что проверить андеррайтеру до полиса.
    """
    st = merge_settings(settings)
    checks = []
    for d in disc:
        checks.append({"code": "c_disc", "params": {"key": d["key"]}})
    if not inspection.get("photos"):
        checks.append({"code": "c_no_inspection", "params": {}})
    elif not inspection.get("ai"):
        checks.append({"code": "c_ai_failed", "params": {}})
    elif inspection.get("missing_views"):
        checks.append({"code": "c_views", "params": {"views": inspection["missing_views"]}})
    if inspection.get("damages"):
        checks.append({"code": "c_damages", "params": {}})
    if not inspection.get("documents"):
        checks.append({"code": "c_docs", "params": {}})
    if value["verdict"] == "under":
        checks.append({"code": "c_under", "params": {}})
    elif value["verdict"] == "over":
        checks.append({"code": "c_over", "params": {}})
    if rate_res["mode"] in ("undefined", "statutory_undefined"):
        checks.append({"code": "c_rate_undefined", "params": {}})
    if rate_res["mode"] in ("statutory", "statutory_undefined"):
        checks.append({"code": "c_statutory", "params": {}})
    if fr.get("needed"):
        checks.append({"code": "c_franchise", "params": {}})
    if missing_key:
        checks.append({"code": "c_missing", "params": {"keys": list(missing_key)}})
    if inspection.get("ai") and inspection.get("recognized"):
        checks.append({"code": "c_confirm", "params": {}})

    if risk["up"] >= int(st["decline_min_up"]):
        code = "d_decline"
        checks.append({"code": "c_decline", "params": {}})
    elif risk["level"] == "low" and not [c for c in checks if c["code"] != "c_confirm"]:
        code = "d_accept"
    else:
        code = "d_accept_with_clauses"
    return {"code": code, "checks": checks}


# ================================================================================================
#  Настройки
# ================================================================================================

def deep_merge(base: Optional[dict], patch: Optional[dict]) -> dict:
    """Глубокое слияние настроек: словари сливаются по ключам на любой глубине, остальное (числа, строки, списки)
    из patch заменяет значение base. Ни base, ни patch не меняются. Частичная правка администратора
    (PUT /act/settings) так не сбрасывает прежние правки: {"rate_fork": {"market": {"cap_at_market": false}}}
    меняет один флаг, а не весь блок rate_fork."""
    out = {k: (deep_merge(v, {}) if isinstance(v, dict) else (list(v) if isinstance(v, list) else v))
           for k, v in (base or {}).items()}
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        elif isinstance(v, dict):
            out[k] = deep_merge(v, {})
        else:
            out[k] = list(v) if isinstance(v, list) else v
    return out


def merge_settings(custom: Optional[dict]) -> dict:
    """Настройки по умолчанию с правками администратора поверх (глубоко, deep_merge); неизвестные ключи верхнего
    уровня отбрасываются."""
    return deep_merge(DEFAULT_SETTINGS, {k: v for k, v in (custom or {}).items() if k in DEFAULT_SETTINGS})


def check_settings(s: dict) -> list:
    """Ошибки в настройках (пустой список — всё верно). Проверяются типы и разумные пределы."""
    errs = []
    unknown = [k for k in s if k not in DEFAULT_SETTINGS]
    if unknown:
        errs.append("неизвестные настройки: " + ", ".join(unknown))
    m = merge_settings(s)
    rule = m["level_rule"]
    try:
        lo, hi, k = int(rule["low_max_net"]), int(rule["high_min_net"]), int(rule["min_known"])
        if not (-4 <= lo < hi <= 4):
            errs.append("level_rule: нужно −4 ≤ low_max_net < high_min_net ≤ 4")
        if not (1 <= k <= 4):
            errs.append("level_rule.min_known: от 1 до 4")
    except (KeyError, TypeError, ValueError):
        errs.append("level_rule: нужны целые low_max_net, high_min_net, min_known")
    adj = m["adj_pct"]
    try:
        vals = [float(adj[x]) for x in LEVELS]
        if any(v < 0 or v > 300 for v in vals) or not (vals[0] <= vals[1] <= vals[2]):
            errs.append("adj_pct: от 0 до 300 %, и не убывает от низкого к высокому")
    except (KeyError, TypeError, ValueError):
        errs.append("adj_pct: нужны числа для low, moderate, high")
    if m["base_source"] not in ("technical", "product_rate"):
        errs.append("base_source: technical или product_rate")
    try:
        lo_v, hi_v = [float(x) for x in m["value_ok_pct"]]
        if not (0 < lo_v <= hi_v <= 150):
            errs.append("value_ok_pct: [от, до] в процентах, 0 < от ≤ до ≤ 150")
    except (TypeError, ValueError):
        errs.append("value_ok_pct: пара чисел")
    for key, lo_b, hi_b in (("new_object_years", 0, 10), ("losses_high_count", 1, 20), ("decline_min_up", 1, 5)):
        try:
            if not (lo_b <= int(m[key]) <= hi_b):
                errs.append(f"{key}: от {lo_b} до {hi_b}")
        except (TypeError, ValueError):
            errs.append(f"{key}: целое число")
    try:
        if any(not (0 <= float(v) <= 100) for v in m["wear_pct_per_year"].values()):
            errs.append("wear_pct_per_year: от 0 до 100 % в год")
    except (TypeError, ValueError, AttributeError):
        errs.append("wear_pct_per_year: словарь чисел")
    if not isinstance(m["insurer_name"], str) or len(m["insurer_name"]) > 120:
        errs.append("insurer_name: строка до 120 знаков")
    lim = m["limits"] if isinstance(m["limits"], dict) else {}
    for key, (lo_b, hi_b) in LIMIT_BOUNDS.items():
        v = lim.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (lo_b <= v <= hi_b):
            errs.append(f"limits.{key}: число от {lo_b} до {hi_b}")
    extra = [k for k in lim if k not in LIMIT_BOUNDS]
    if extra:
        errs.append("limits: неизвестные ключи " + ", ".join(extra))
    if not errs and lim.get("ai_timeout_sec", 0) > lim.get("ai_deadline_sec", 0):
        errs.append("limits: ai_timeout_sec не больше ai_deadline_sec")
    mk = m["market"] if isinstance(m["market"], dict) else {}
    if not isinstance(m["market"], dict):
        errs.append("market: словарь настроек оценки по объявлениям")
    for key, (lo_b, hi_b, whole) in MARKET_BOUNDS.items():
        v = mk.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (lo_b <= v <= hi_b) \
                or (whole and float(v) != int(v)):
            errs.append(f"market.{key}: {'целое ' if whole else ''}число от {lo_b} до {hi_b}")
    for key in MARKET_FLAGS:
        if not isinstance(mk.get(key), bool):
            errs.append(f"market.{key}: true или false")
    extra = [k for k in mk if k not in MARKET_BOUNDS and k not in MARKET_FLAGS]
    if extra:
        errs.append("market: неизвестные ключи " + ", ".join(extra))
    rq = m["request_check"] if isinstance(m["request_check"], dict) else {}
    if not isinstance(m["request_check"], dict):
        errs.append("request_check: словарь настроек сверки с запросом филиала")
    if not isinstance(rq.get("term_inclusive"), bool):
        errs.append("request_check.term_inclusive: true или false")
    tol = rq.get("premium_tolerance")
    if isinstance(tol, bool) or not isinstance(tol, (int, float)) or not (0 <= tol <= 1_000_000):
        errs.append("request_check.premium_tolerance: число сумов от 0 до 1 000 000")
    extra = [k for k in rq if k not in ("term_inclusive", "premium_tolerance")]
    if extra:
        errs.append("request_check: неизвестные ключи " + ", ".join(extra))
    ct = m["contract"] if isinstance(m["contract"], dict) else {}
    if not isinstance(m["contract"], dict):
        errs.append("contract: словарь настроек чтения договора")
    if not isinstance(ct.get("ai_assist"), bool):
        errs.append("contract.ai_assist: true или false")
    mx = ct.get("ai_max_chars")
    if isinstance(mx, bool) or not isinstance(mx, int) or not (2000 <= mx <= 100000):
        errs.append("contract.ai_max_chars: целое от 2 000 до 100 000")
    extra = [k for k in ct if k not in ("ai_assist", "ai_max_chars")]
    if extra:
        errs.append("contract: неизвестные ключи " + ", ".join(extra))
    if not errs and lim.get("pdf_text_max_pages", 0) < lim.get("pdf_max_pages", 0):
        errs.append("limits: pdf_text_max_pages не меньше pdf_max_pages")
    errs += check_parts_settings(m.get("parts"))
    sc = m["scoring"] if isinstance(m["scoring"], dict) else None
    if sc is None:
        errs.append("scoring: словарь {brand_color}")
    else:
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", str(sc.get("brand_color") or "")):
            errs.append("scoring.brand_color: цвет вида #0B4F8A")
        extra = [k for k in sc if k != "brand_color"]
        if extra:
            errs.append("scoring: неизвестные ключи " + ", ".join(extra))
    crs = m["credit_report"] if isinstance(m["credit_report"], dict) else None
    if crs is None:
        errs.append("credit_report: словарь {low_class, max_age_days, allow_scan}")
    else:
        if crs.get("low_class") not in BUREAU_CLASSES:
            errs.append("credit_report.low_class: одна из букв " + ", ".join(BUREAU_CLASSES))
        d = crs.get("max_age_days")
        if isinstance(d, bool) or not isinstance(d, int) or not 1 <= d <= 365:
            errs.append("credit_report.max_age_days: целое от 1 до 365")
        if not isinstance(crs.get("allow_scan"), bool):
            errs.append("credit_report.allow_scan: true или false")
        extra = [k for k in crs if k not in ("low_class", "max_age_days", "allow_scan")]
        if extra:
            errs.append("credit_report: неизвестные ключи " + ", ".join(extra))
    nps = m["napp"] if isinstance(m["napp"], dict) else None
    if nps is None:
        errs.append("napp: словарь {branch_min_contracts}")
    else:
        v = nps.get("branch_min_contracts")
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 1_000_000:
            errs.append("napp.branch_min_contracts: целое от 0 до 1 000 000")
        extra = [k for k in nps if k != "branch_min_contracts"]
        if extra:
            errs.append("napp: неизвестные ключи " + ", ".join(extra))
    errs += check_fork_settings((s or {}).get("rate_fork"))
    errs += check_below_min_settings(m.get("below_min"))
    errs += check_factor_settings(m.get("factors"))
    return errs


BELOW_MIN_BOUNDS = {"min_share_pct": (0, 100), "market_max_ratio": (1, 100), "low_loss_ratio_pct": (0, 200),
                    "losses_block": (1, 100)}
BELOW_MIN_NET = ("calibrated", "always")


def check_below_min_settings(bm) -> list:
    """Настройки оценки заниженной ставки: числа в пределах BELOW_MIN_BOUNDS, net_check — calibrated | always."""
    if not isinstance(bm, dict):
        return ["below_min: словарь {min_share_pct, market_max_ratio, low_loss_ratio_pct, losses_block, net_check}"]
    errs = []
    for key, (lo, hi) in BELOW_MIN_BOUNDS.items():
        v = bm.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            errs.append(f"below_min.{key}: число от {lo} до {hi}")
    if bm.get("losses_block") is not None and not isinstance(bm.get("losses_block"), bool)             and isinstance(bm.get("losses_block"), (int, float)) and float(bm["losses_block"]) != int(bm["losses_block"]):
        errs.append("below_min.losses_block: целое число")
    if bm.get("net_check") not in BELOW_MIN_NET:
        errs.append("below_min.net_check: calibrated или always")
    extra = [k for k in bm if k not in BELOW_MIN_BOUNDS and k not in ("net_check", "calibrated")]
    if extra:
        errs.append("below_min: неизвестные ключи " + ", ".join(extra))
    return errs


def check_parts_settings(pt) -> list:
    """Настройки частей: shares {продукт: {класс: %}} — каждая доля 0–100, сумма 100 ± 0,5; sum_tolerance 0–1000."""
    errs = []
    if not isinstance(pt, dict):
        return ["parts: словарь {shares, sum_tolerance}"]
    extra = [k for k in pt if k not in ("shares", "sum_tolerance")]
    if extra:
        errs.append("parts: неизвестные ключи " + ", ".join(extra))
    tol = pt.get("sum_tolerance")
    if isinstance(tol, bool) or not isinstance(tol, (int, float)) or not (0 <= tol <= 1000):
        errs.append("parts.sum_tolerance: число сумов от 0 до 1000")
    sh = pt.get("shares")
    if not isinstance(sh, dict):
        errs.append("parts.shares: словарь {код продукта: {класс: %}}")
        return errs
    for code, d in sh.items():
        if not isinstance(d, dict) or not d:
            errs.append(f"parts.shares.{code}: словарь {{класс: %}}")
            continue
        vals = list(d.values())
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not (0 <= v <= 100) for v in vals):
            errs.append(f"parts.shares.{code}: доли — числа от 0 до 100")
        elif abs(sum(float(v) for v in vals) - 100) > 0.5:
            errs.append(f"parts.shares.{code}: сумма долей {round(sum(vals), 2)} — нужно 100 ± 0,5")
    return errs


# ================================================================================================
#  7. Оценка по объявлениям (снимки экрана сотрудника, 30.09.2026)
# ================================================================================================

_MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7, "август": 8,
           "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
           "yanvar": 1, "fevral": 2, "mart": 3, "aprel": 4, "may": 5, "iyun": 6, "iyul": 7, "avgust": 8,
           "sentyabr": 9, "oktyabr": 10, "noyabr": 11, "dekabr": 12,
           "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
           "nov": 11, "dec": 12}
_TODAY_WORDS = ("сегодня", "bugun", "today")
_YESTERDAY_WORDS = ("вчера", "kecha", "yesterday")


# «N единиц назад»: ru — «назад», uz — «oldin», en — «ago». Число может не стоять («неделю назад», «a month ago»).
_AGO_UNITS = (  # (корень слова, единица)
    ("минут", "min"), ("мин", "min"), ("час", "hour"), ("ч", "hour"), ("дн", "day"), ("ден", "day"),
    ("сут", "day"), ("недел", "week"), ("нед", "week"), ("месяц", "month"), ("мес", "month"), ("год", "year"),
    ("лет", "year"),
    ("daqiqa", "min"), ("soat", "hour"), ("kun", "day"), ("hafta", "week"), ("oy", "month"), ("yil", "year"),
    ("minute", "min"), ("min", "min"), ("hour", "hour"), ("hr", "hour"), ("day", "day"), ("week", "week"),
    ("month", "month"), ("year", "year"), ("yr", "year"))
_AGO_RX = re.compile(r"(?:(\d{1,3}|an?|one)\s*)?([a-zа-яё']+)\.?\s*(назад|oldin|ago)\b")


def _ago(s: str, shot_date: date) -> Optional[date]:
    m = _AGO_RX.search(s)
    if not m:
        return None
    raw, word = m.group(1), m.group(2)
    n = int(raw) if raw and raw.isdigit() else 1
    unit = next((u for stem, u in sorted(_AGO_UNITS, key=lambda x: -len(x[0])) if word.startswith(stem)), None)
    if unit is None:
        return None
    if unit in ("min", "hour"):
        # время снимка неизвестно: «5 часов назад» — день снимка, «30 часов назад» — день раньше
        return date.fromordinal(shot_date.toordinal() - (n // 24 if unit == "hour" else n // 1440))
    if unit == "day":
        return date.fromordinal(shot_date.toordinal() - n)
    if unit == "week":
        return date.fromordinal(shot_date.toordinal() - 7 * n)
    return months_before(shot_date, n * (12 if unit == "year" else 1))


def parse_posted_ex(text, shot_date: date) -> tuple:
    """
    (дата или None, состояние): ok — дата распознана и не позже снимка; future — дата позже даты снимков
    (некорректна); none — даты нет или не распознана. Понимает ГГГГ-ММ-ДД, ДД.ММ.ГГГГ, «12 сентября 2026 г.»,
    «12 сентября» (год снимка, а если выходит позже снимка — прошлый), «сегодня/вчера» (bugun/kecha,
    today/yesterday), «N минут/часов/дней/недель/месяцев/лет назад» (N daqiqa/soat/kun/hafta/oy/yil oldin,
    N minutes/hours/days/weeks/months/years ago).
    """
    if text is None:
        return None, "none"
    if isinstance(text, date):
        d = text
    else:
        s = re.sub(r"\s+", " ", str(text)).strip().lower()
        if not s:
            return None, "none"
        d = None
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s) or None
        m2 = re.search(r"(?<!\d)(\d{1,2})[./](\d{1,2})[./](\d{4})(?!\d)", s)
        try:
            if m:
                d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            elif m2:
                d = date(int(m2.group(3)), int(m2.group(2)), int(m2.group(1)))
        except ValueError:
            return None, "none"
        if d is None:
            if any(w in s for w in _TODAY_WORDS):
                d = shot_date
            elif any(w in s for w in _YESTERDAY_WORDS):
                d = date.fromordinal(shot_date.toordinal() - 1)
            else:
                d = _ago(s, shot_date)
        if d is None:
            m3 = re.search(r"(?<!\d)(\d{1,2})\s+([a-zа-яё']+)(?:\s+(\d{4}))?", s)
            if m3:
                word = m3.group(2)
                month = next((v for k, v in sorted(_MONTHS.items(), key=lambda kv: -len(kv[0]))
                              if word.startswith(k)), None)
                if month:
                    year = int(m3.group(3)) if m3.group(3) else shot_date.year
                    try:
                        d = date(year, month, int(m3.group(1)))
                    except ValueError:
                        return None, "none"
                    if not m3.group(3) and d > shot_date:
                        d = date(year - 1, month, int(m3.group(1)))
    if d is None or d.year < 2000:
        return None, "none"
    if d > shot_date:
        return None, "future"
    return d, "ok"


def parse_posted(text, shot_date: date) -> Optional[date]:
    """Дата публикации со снимка → дата; позже снимка или не распознана — None (см. parse_posted_ex)."""
    return parse_posted_ex(text, shot_date)[0]


def round_half_up(x) -> int:
    """Округление до целого «половина — вверх»: так же, как Math.round на экране (round в Python — банковское)."""
    return int(math.floor(float(x) + 0.5))


def _price_of(v) -> Optional[float]:
    """Цена объявления: число больше нуля, иначе None (пусто, ноль, мусор — «цена не видна»)."""
    if v is None or isinstance(v, bool):
        return None
    x = float(v) if isinstance(v, (int, float)) else to_number(v)
    if x is None or x != x or x <= 0 or math.isinf(x):
        return None
    return x


def months_before(d: date, months: int) -> date:
    """Та же дата на N месяцев раньше (конец месяца — последний день)."""
    y, m = d.year, d.month - int(months)
    while m <= 0:
        m += 12
        y -= 1
    day = d.day
    while True:
        try:
            return date(y, m, day)
        except ValueError:
            day -= 1


def quantile(xs: list, p: float) -> Optional[float]:
    """Перцентиль линейной интерполяцией — тот же способ, что valuation.quantiles и valuation_sources."""
    xs = sorted(float(v) for v in xs)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    pos = p * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


USD_LIKE = ("USD", "у.е.")


def market_estimate(listings: list, *, declared: Optional[float] = None, sum_insured: Optional[float] = None,
                    settings: Optional[dict] = None, shot_date: Optional[date] = None,
                    usd_rate: Optional[float] = None) -> dict:
    """
    Оценка по объявлениям (без модели и без сети). listings — [{"id", "title", "price", "currency" (UZS|USD|у.е.),
    "posted", "relevant", ...}] после проверки ввода. Правило (настройки market, calibrated = 0):
      1) берутся только relevant, с ценой больше нуля, в сумах или в долларах при известном курсе, с видимой
         датой публикации не позже даты снимков и не старше max_age_months от неё. Дата не видна — объявление
         исключается (mx_no_date; настройка allow_undated = true — берётся дата снимка с пометкой date_assumed);
         removed — убрано сотрудником из списка загрузки; off_by = employee — снято сотрудником;
      2) выбросы — ниже outlier_low и выше outlier_high от медианы отобранных — отбрасываются, но только если
         отобранных не меньше min_listings;
      3) медиана оставшихся, вилка — 25-й и 75-й перцентили; поправок на год и пробег нет (в app/valuation.py
         для объявлений готового правила нет: mileage_factor работает только в методе износа);
      4) меньше min_listings — verdict few (ориентировочно), ни одного — none; иначе сравнение с заявленной
         стоимостью: |заявленная − медиана| / заявленная × 100 (как valuation.compare_declared) больше diff_pct —
         refine (уточнённая стоимость = медиана, страховая сумма сверяется с ней по value_check), иначе confirmed.
      Без заявленной стоимости (предпросмотр) verdict — ready.
    """
    st = merge_settings(settings)
    mk = st["market"]
    undated_ok = mk.get("allow_undated") is True
    shot_date = shot_date or date.today()
    edge = months_before(shot_date, int(mk["max_age_months"]))
    try:
        rate = float(usd_rate) if usd_rate not in (None, "") and float(usd_rate) > 0 else None
    except (TypeError, ValueError):
        rate = None
    rows, excluded, cand = [], [], []
    assumed = []
    for it in listings or []:
        if not isinstance(it, dict):
            continue
        r = dict(it)
        price = _price_of(r.get("price"))
        cur = r.get("currency") or "UZS"
        r["price_uzs"] = None
        if price is not None:
            if cur == "UZS":
                r["price_uzs"] = round_half_up(price)
            elif cur in USD_LIKE and rate:
                r["price_uzs"] = round_half_up(price * rate)
        pd_, pst = parse_posted_ex(r.get("posted_date"), shot_date)
        if pd_ is None:
            pd2, pst2 = parse_posted_ex(r.get("posted"), shot_date)
            if pd2 is not None or pst == "none":
                pd_, pst = pd2, pst2
        r["posted_date"] = pd_.isoformat() if pd_ else None
        r["date_assumed"] = pd_ is None
        r["date_status"] = pst
        reason = None
        if r.get("removed"):
            reason = ("mx_removed_by_employee", {})
        elif r.get("relevant") is False:
            # «снято сотрудником» — только если в загрузке снимков модель считала объявление подходящим
            reason = ("mx_unchecked_by_employee", {}) if r.get("off_by") == "employee" else \
                ("mx_not_relevant", {"why": r.get("why_excluded")})
        elif price is None:
            reason = ("mx_no_price", {})
        elif r["price_uzs"] is None:
            reason = ("mx_no_rate", {})
        elif pst == "future":
            reason = ("mx_bad_date", {"shot": shot_date.isoformat()})
        elif pd_ is None and not undated_ok:
            reason = ("mx_no_date", {})
        elif pd_ is not None and pd_ < edge:
            reason = ("mx_too_old", {"date": pd_.isoformat(), "months": int(mk["max_age_months"])})
        if reason:
            r["used"] = False
            excluded.append({"id": r.get("id"), "title": r.get("title"), "code": reason[0], "params": reason[1]})
        else:
            cand.append(r)
        rows.append(r)
    how = [{"code": "mh_filter_undated" if undated_ok else "mh_filter", "params": {"months": int(mk["max_age_months"])}}]
    out = {"available": False, "verdict": "none", "count": len(rows), "used": 0, "median": None, "low": None,
           "high": None, "currency": "UZS", "declared": declared, "diff_pct": None, "refined_value": None,
           "insured_check": None, "excluded": excluded, "listings": rows, "shot_date": shot_date.isoformat(),
           "window_from": edge.isoformat(), "usd_rate": rate, "how": how, "rule": dict(mk),
           "calibrated": CALIBRATED}
    if rate and any(r.get("currency") in USD_LIKE for r in rows):
        how.append({"code": "mh_fx", "params": {"rate": rate}})
    elif any(r.get("currency") in USD_LIKE and _price_of(r.get("price")) is not None for r in rows):
        how.append({"code": "mh_fx_none", "params": {}})
    if not cand:
        how.append({"code": "mh_none", "params": {}})
        return out
    lo_k, hi_k = float(mk["outlier_low"]), float(mk["outlier_high"])
    used = []
    n_out = 0
    if len(cand) < int(mk["min_listings"]):
        # при малом числе объявлений «выброс» от медианы двух-трёх цен ничего не значит — не ищем
        used = cand
        for r in cand:
            r["used"] = True
        how.append({"code": "mh_outliers_skipped", "params": {"n": len(cand), "min": int(mk["min_listings"])}})
    else:
        med0 = quantile([r["price_uzs"] for r in cand], 0.5)
        for r in cand:
            p = r["price_uzs"]
            code = "mx_outlier_low" if p < lo_k * med0 else ("mx_outlier_high" if p > hi_k * med0 else None)
            if code:
                n_out += 1
                r["used"] = False
                excluded.append({"id": r.get("id"), "title": r.get("title"), "code": code,
                                 "params": {"price": p, "median": round_half_up(med0),
                                            "k": lo_k if code.endswith("low") else hi_k}})
            else:
                r["used"] = True
                used.append(r)
        how.append({"code": "mh_outliers", "params": {"lo": lo_k, "hi": hi_k, "med0": round_half_up(med0),
                                                      "k": n_out}})
    assumed = [r.get("id") for r in used if r["date_assumed"]]
    prices = [r["price_uzs"] for r in used]
    med = round_half_up(quantile(prices, 0.5))
    q1, q3 = round_half_up(quantile(prices, 0.25)), round_half_up(quantile(prices, 0.75))
    out.update(available=True, used=len(used), median=med, low=q1, high=q3, date_assumed=assumed)
    how.append({"code": "mh_median", "params": {"n": len(used), "median": med, "low": q1, "high": q3}})
    if assumed:
        how.append({"code": "mh_dates_assumed", "params": {"k": len(assumed)}})
    how.append({"code": "mh_no_adj", "params": {}})
    few = len(used) < int(mk["min_listings"])
    if declared:
        # от округлённой медианы — так же, как экран (mkDiff): одна цифра и в акте, и в предпросмотре
        diff = (float(declared) - med) / float(declared) * 100
        out["diff_pct"] = round(diff, 1)
        how.append({"code": "mh_compare", "params": {"declared": declared, "median": med,
                                                     "diff": round(abs(diff), 1), "thr": mk["diff_pct"]}})
    if few:
        out["verdict"] = "few"
        how.append({"code": "mh_few", "params": {"n": len(used), "min": int(mk["min_listings"])}})
    elif not declared:
        out["verdict"] = "ready"
    elif abs(out["diff_pct"]) > float(mk["diff_pct"]) + 1e-9:
        out["verdict"] = "refine"
        out["refined_value"] = round(med)
        if sum_insured:
            vc = value_check(float(sum_insured), float(round(med)), st)
            out["insured_check"] = {"ratio_pct": vc["ratio_pct"], "verdict": vc["verdict"],
                                    "legal_ref": vc["legal_ref"], "diff": vc["diff"]}
    else:
        out["verdict"] = "confirmed"
    return out


# ================================================================================================
#  8. Сверка с запросом филиала (30.09.2026)
# ================================================================================================

RQ_ITEMS = ("tariff_min", "tariff_act", "premium_request", "premium_act", "sum_value", "franchise", "term")
# у договора ещё: график платежей против премии, суммы по объектам против общей, существенные условия
CT_ITEMS = RQ_ITEMS + ("payments", "items_sum", "essentials")
RQ_REFERENCE = ("premium_act", "sum_value")     # справочно: в решение не идут (сумма к стоимости — раздел 3)
RQ_ORDER = {"no_essential": 4, "below_min": 3, "differs": 2, "missing": 1, "ok": 0}
ESSENTIAL_REF = "ГК РУз, ст. 929"


def _rq_item(code, requested=None, calculated=None, verdict="missing", text_code=None, prefix="rq", **params) -> dict:
    diff = diff_pct = None
    if isinstance(requested, (int, float)) and isinstance(calculated, (int, float)):
        diff = round(float(requested) - float(calculated), 6)
        diff_pct = round(diff / float(calculated) * 100, 2) if calculated else None
    if text_code and text_code.startswith("rq_") and prefix != "rq":
        text_code = prefix + text_code[2:]            # «rq_…» → «ct_…»: тот же текст про другой документ
    return {"code": code, "requested": requested, "calculated": calculated, "diff": diff, "diff_pct": diff_pct,
            "verdict": verdict, "reference": code in RQ_REFERENCE,
            "text_code": text_code or f"{prefix}_{code}_{verdict}", "params": params}


def _rq_summary(items: list, prefix: str) -> dict:
    main = [i for i in items if not i["reference"]]
    if all(i["verdict"] == "missing" for i in main):
        worst = "missing"
    else:
        # чего-то в документе нет, но найденное сходится — итог «сходится», пропуски видны в строках
        worst = max((i["verdict"] for i in main if i["verdict"] != "missing"), key=lambda v: RQ_ORDER[v])
    out = {"verdict": worst, "code": f"{prefix}_summary_{worst}",
           "differs": sum(1 for i in main if i["verdict"] == "differs"),
           "below_min": sum(1 for i in main if i["verdict"] == "below_min"),
           "missing": sum(1 for i in main if i["verdict"] == "missing")}
    if prefix != "rq":
        out["no_essential"] = sum(1 for i in main if i["verdict"] == "no_essential")
    return out


def request_check(req: Optional[dict], *, rate_res: dict, rate_final: Optional[float],
                  premium_final: Optional[float], sum_insured: float, object_value: float, value: dict, fr: dict,
                  term_from_request: bool = False, settings: Optional[dict] = None, prefix: str = "rq",
                  rate_type: str = "annual") -> dict:
    """
    rate_type — тип ставки продукта (app/min_rates.py): annual — премия по тарифу документа = сумма × тариф × дни / 365;
    fixed — сумма × тариф (ставка на весь срок). Ставка документа сравнивается с минимумом в том же типе.
    Сверка запроса филиала с расчётом акта (коды и числа; слова — app/act_texts.py).
    req: {"tariff_pct", "premium", "franchise": {"applied", "text", "pct", "amount"}, "term_from", "term_to",
          "term_days", "sum_insured", "source"} — после проверки ввода (app/act.py validate).
    Ставка — годовая: премия = сумма × тариф / 100 × дни / 365, дни — весь срок договора (включительно,
    настройка request_check.term_inclusive применяется при разборе дат). Допуск по премии —
    request_check.premium_tolerance сумов (округление филиала).
    prefix — префикс кодов текстов: rq — запрос филиала, ct — договор (contract_check, те же проверки).
    """
    def _rq_item_p(*a, **k):
        return _rq_item(*a, prefix=prefix, **k)

    st = merge_settings(settings)["request_check"]
    tol = float(st["premium_tolerance"])
    out = {"available": False, "items": [], "summary": None, "how": [], "source": None, "tolerance": tol,
           "term_inclusive": bool(st["term_inclusive"]), "calibrated": CALIBRATED}
    if not req:
        return out
    out["available"] = True
    out["source"] = req.get("source")
    items = []
    tr = req.get("tariff_pct")
    pr = req.get("premium")
    days_req = req.get("term_days")
    days_act = rate_res.get("term_days")
    minp = rate_res.get("min_pct")
    statutory = rate_res.get("mode") in ("statutory", "statutory_undefined")

    # 1. тариф запроса против минимальной ставки продукта (тарифная политика; у обязательных — ставка НПА)
    if tr is None:
        items.append(_rq_item_p("tariff_min", None, minp, "missing", "rq_tariff_none"))
    elif minp is None:
        items.append(_rq_item_p("tariff_min", tr, None, "missing", "rq_tariff_min_na", req=tr))
    else:
        v = "below_min" if tr + 1e-9 < float(minp) else "ok"
        items.append(_rq_item_p("tariff_min", tr, minp, v, None, req=tr, min=minp, statutory=statutory))
    # 2. тариф запроса против ставки акта (рекомендуемая ставка с поправкой по уровню риска)
    if tr is None:
        items.append(_rq_item_p("tariff_act", None, rate_final, "missing", "rq_tariff_none"))
    elif rate_final is None:
        items.append(_rq_item_p("tariff_act", tr, None, "missing", "rq_tariff_act_na", req=tr))
    else:
        v = "ok" if tr + 1e-9 >= float(rate_final) else "differs"
        items.append(_rq_item_p("tariff_act", tr, rate_final, v, None, req=tr, calc=rate_final,
                              diff=round(abs(tr - float(rate_final)), 4)))
    # 3. премия запроса против расчёта по тарифу ЗАПРОСА на весь срок
    S_req = req.get("sum_insured") or sum_insured
    if pr is None:
        items.append(_rq_item_p("premium_request", None, None, "missing", "rq_premium_none"))
    elif tr is None or (not days_req and rate_type != "fixed"):
        items.append(_rq_item_p("premium_request", pr, None, "missing", "rq_premium_request_na", req=pr))
    else:
        # до тийинов: расхождение видно точно (123 322 000 − 123 321 917,81 = 82,19)
        calc = round(premium_by_type(tr, S_req, days_req or 365, rate_type), 2)
        v = "ok" if abs(pr - calc) <= tol + 1e-9 else "differs"
        items.append(_rq_item_p("premium_request", pr, calc, v, None, req=pr, calc=calc, diff=round(pr - calc, 2),
                              tol=tol, rate=tr, sum=S_req, days=days_req))
        out["how"].append({"code": f"{prefix}_how_premium" + ("_fixed" if rate_type == "fixed" else ""),
                           "params": {"sum": S_req, "rate": tr, "days": days_req, "premium": calc}})
    # 4. справочно: премия запроса против премии акта (по ставке акта, с учётом применённой франшизы)
    if pr is not None and premium_final is not None:
        v = "ok" if abs(pr - premium_final) <= tol + 1e-9 else "differs"
        items.append(_rq_item_p("premium_act", pr, premium_final, v, None, req=pr, calc=premium_final,
                              diff=round(pr - premium_final), rate=rate_final))
    else:
        items.append(_rq_item_p("premium_act", pr, premium_final, "missing", "rq_premium_act_na"))
    # 5. справочно: сумма к стоимости — вывод раздела 3 (value_check), здесь не дублируется
    items.append(_rq_item_p("sum_value", sum_insured, object_value,
                          "ok" if value.get("verdict") == "normal" else "differs", "rq_sum_value",
                          ratio=value.get("ratio_pct")))
    # 6. франшиза: запрос против вывода акта
    rf = req.get("franchise")
    status = fr.get("status") or ("proposed" if fr.get("needed") else "none")
    act_pct = fr.get("size_pct") if status in ("applied", "proposed") else None
    if not rf:
        items.append(_rq_item_p("franchise", None, act_pct, "missing", "rq_franchise_none"))
    elif not rf.get("applied"):
        if status in ("none", "statutory"):
            items.append(_rq_item_p("franchise", 0, 0, "ok", "rq_franchise_ok_none"))
        else:
            items.append(_rq_item_p("franchise", 0, act_pct, "differs",
                                  "rq_franchise_act_applied" if status == "applied" else "rq_franchise_act_proposed",
                                  pct=act_pct))
    else:
        req_pct = rf.get("pct")
        if req_pct is None and rf.get("amount") and sum_insured:
            req_pct = round(float(rf["amount"]) / sum_insured * 100, 4)
        if status in ("none", "statutory"):
            items.append(_rq_item_p("franchise", req_pct, 0, "differs", "rq_franchise_req_only",
                                  text=rf.get("text"), pct=req_pct))
        elif req_pct is not None and act_pct is not None and abs(req_pct - float(act_pct)) <= 1e-6:
            items.append(_rq_item_p("franchise", req_pct, act_pct, "ok", "rq_franchise_ok_same", pct=req_pct))
        else:
            items.append(_rq_item_p("franchise", req_pct, act_pct, "differs", "rq_franchise_size",
                                  text=rf.get("text"), pct=req_pct, act=act_pct))
    # 7. срок: весь срок договора в днях (многолетний — тоже), ставка годовая
    if not days_req:
        items.append(_rq_item_p("term", None, days_act, "missing", "rq_term_none"))
    else:
        v = "ok" if int(days_req) == int(days_act or 0) else "differs"
        code = "rq_term_from_request" if term_from_request and v == "ok" else None
        items.append(_rq_item_p("term", days_req, days_act, v, code, req=days_req, calc=days_act,
                              date_from=req.get("term_from"), date_to=req.get("term_to")))
        if req.get("term_from") and req.get("term_to"):
            out["how"].append({"code": f"{prefix}_how_days" if st["term_inclusive"] else f"{prefix}_how_days_excl",
                               "params": {"date_from": req["term_from"], "date_to": req["term_to"],
                                          "days": days_req}})
    out["how"].append({"code": f"{prefix}_how_tol", "params": {"tol": tol}})
    out["how"].append({"code": f"{prefix}_how_" + ("fixed" if rate_type == "fixed" else "annual"), "params": {}})
    out["rate_type"] = rate_type
    out["items"] = items
    out["summary"] = _rq_summary(items, prefix)
    return out


def request_checks(rc: dict, prefix: str = "rq") -> list:
    """Расхождения сверки для decision.checks (справочные строки не идут)."""
    out = []
    for i in (rc or {}).get("items") or []:
        if i["reference"] or i["verdict"] not in ("differs", "below_min", "no_essential"):
            continue
        params = {"req": i["requested"], "calc": i["calculated"], "diff": i["diff"], "verdict": i["verdict"]}
        if i["code"] == "essentials":
            params = {"missing": list((i.get("params") or {}).get("missing") or [])}
        out.append({"code": f"c_{prefix}_" + i["code"], "params": params})
    return out


def contract_check(ct: Optional[dict], *, rate_res: dict, rate_final: Optional[float],
                   premium_final: Optional[float], sum_insured: float, object_value: float, value: dict, fr: dict,
                   term_from_contract: bool = False, settings: Optional[dict] = None,
                   rate_type: str = "annual") -> dict:
    """
    Сверка договора страхования с расчётом акта: те же проверки, что у запроса филиала (request_check,
    тексты ct_*), и ещё три: сумма графика платежей против премии договора (допуск premium_tolerance),
    сумма по объектам против общей страховой суммы (до сума) и полнота договора — существенные условия
    ГК РУз, ст. 929 (объект, страховой случай, страховая сумма, премия, срок). ct — проверенный
    optional.contract: поля optional.request и covered_risks, payments, items, object_description, contract_no …
    """
    out = request_check(ct, rate_res=rate_res, rate_final=rate_final, premium_final=premium_final,
                        sum_insured=sum_insured, object_value=object_value, value=value, fr=fr,
                        term_from_request=term_from_contract, settings=settings, prefix="ct",
                        rate_type=rate_type)
    if not out["available"]:
        return out
    tol = out["tolerance"]
    items = out["items"]
    pr = ct.get("premium")
    pays = ct.get("payments") or []
    # график платежей против премии договора
    if pays:
        total = round(sum(float(p["amount"]) for p in pays), 2)
        if pr is None:
            items.append(_rq_item("payments", None, total, "missing", "ct_payments_na", prefix="ct",
                                  n=len(pays), calc=total))
        else:
            v = "ok" if abs(total - float(pr)) <= tol + 1e-9 else "differs"
            items.append(_rq_item("payments", pr, total, v, None, prefix="ct", n=len(pays), req=pr, calc=total,
                                  diff=round(float(pr) - total, 2)))
    else:
        code = "ct_payments_single" if ct.get("payment_mode") == "single" else "ct_payments_none"
        items.append(_rq_item("payments", pr, None, "missing", code, prefix="ct"))
    # суммы по объектам против общей страховой суммы договора
    parts = ct.get("items") or []
    if parts:
        total = round(sum(float(x["sum"]) for x in parts), 2)
        S = ct.get("sum_insured")
        if S is None:
            items.append(_rq_item("items_sum", None, total, "missing", "ct_items_sum_na", prefix="ct",
                                  n=len(parts), calc=total))
        else:
            v = "ok" if abs(total - float(S)) <= 1.0 else "differs"
            items.append(_rq_item("items_sum", S, total, v, None, prefix="ct", n=len(parts), req=S, calc=total,
                                  diff=round(float(S) - total, 2)))
    # существенные условия (ГК РУз, ст. 929)
    ess = contract_essentials(ct)
    missing = [e["code"] for e in ess if not e["present"]]
    items.append(_rq_item("essentials", None, None, "no_essential" if missing else "ok", None, prefix="ct",
                          missing=missing, present=[e["code"] for e in ess if e["present"]]))
    out["essentials"] = ess
    out["legal_ref"] = ESSENTIAL_REF
    out["how"].insert(0, {"code": "ct_how_essentials", "params": {}})
    out["summary"] = _rq_summary(items, "ct")
    return out


def contract_essentials(ct: dict) -> list:
    """Есть ли в договоре существенные условия (ГК РУз, ст. 929): объект, страховой случай, сумма, премия, срок."""
    have = {"object": bool(ct.get("object_description") or ct.get("items") or ct.get("cadastre_no")),
            "insured_event": bool(ct.get("covered_risks")),
            "sum_insured": ct.get("sum_insured") is not None,
            "premium": ct.get("premium") is not None,
            "term": bool(ct.get("term_days"))}
    return [{"code": c, "present": have[c]} for c in ("object", "insured_event", "sum_insured", "premium", "term")]


# ================================================================================================
#  9. Комплексные продукты: разбор по частям (ТЗ универсального шаблона, 4.1в; 30.09.2026)
# ================================================================================================
#
# Продукт из нескольких классов — несколько условных договоров (Положение 1882, п. 11). Каждая часть считается
# по своему классу: уровень, ставка по своей тарифной политике (обязательная часть — только по нормативному акту),
# премия, франшиза, сценарии. Ставка проверяется по каждому классу отдельно (правило проекта № 5): средняя по
# договору только справочно. Премии складываются, уровень — самый высокий; сценарии: части одного объекта — большее,
# разные объекты — сумма. Функции чистые: справочник и настройки приходят аргументами.

SAME_OBJECT_CLASSES = frozenset(("8", "9", "16"))     # имущество, ущерб и перерыв в деятельности одного объекта
VALUE_CLASSES = frozenset(("3", "4", "5", "6", "7", "8", "9"))   # у части есть страховая стоимость (ГК ст. 936, 938)
LEVEL_ORDER = {"low": 0, "moderate": 1, "high": 2}
MAX_PARTS = 10
SCENARIOS3 = ("PML", "EML", "MFL")

# подписи частей в тексте тарифа продукта («ТС 1,1% · НС 0,5% · ОТВ 1%») → классы-кандидаты; берётся первый
# кандидат из состава продукта. Короткие подписи (до трёх букв) — только целым словом.
RATE_TEXT_LABELS = (
    (("тс", "залог", "tv", "garov"), ("3",)),
    (("имущ", "гбо", "mol"), ("8", "9")),
    (("нс", "bh", "несчаст"), ("1",)),
    (("отв", "го", "ushoj", "javob"), ("13", "10", "11", "12")),
    (("фин", "кредит", "moliya"), ("14", "13з")),
)

# слова наименования объекта в договоре → класс (дополняют виды объектов шаблонов классов; у 8 и 9 виды одинаковые,
# различают их слова риска)
PART_WORDS = {
    "1": ("несчаст", "жизн", "здоров", "заёмщик", "заемщик", "застрахованн", "baxtsiz", "accident", "life"),
    "3": ("автомоб", "транспорт", "машин", "авто", "avtomobil", "transport", "vehicle", "car "),
    "8": ("имуществ", "здани", "сооружен", "квартир", "дом ", "недвижим", "огн", "пожар", "стихий", "mol-mulk",
          "bino", "uy ", "property", "building", "fire"),
    "9": ("ущерб", "полом", "краж", "залив", "бой стекол", "zarar", "damage", "theft", "breakdown"),
    "13": ("ответствен", "третьим лиц", "javobgar", "liabilit", "гражданск"),
    "14": ("кредит", "заём", "займ", "невозврат", "непогашен", "kredit", "qarz", "loan", "credit"),
    "16": ("простой", "перерыв", "упущен", "доход", " bi ", "business interrupt", "tanaffus"),
}


def default_same_object(classes) -> bool:
    """Один объект по умолчанию: все классы — имущество/ущерб/перерыв одного объекта (8, 9, 16)."""
    cl = [str(c) for c in classes or []]
    return bool(cl) and all(c in SAME_OBJECT_CLASSES for c in cl)


def part_rates_from_text(rate_text, classes) -> dict:
    """
    Ставки частей из текста тарифа продукта: «имущ. 0,1% · отв. 0,5%» → {"8": 0.1, "13": 0.5}.
    Одна ставка без подписи («0,25% фикс.») — {"*": 0.25}: общая ставка продукта для всех частей.
    Подпись не узнана — часть пропускается (ставка класса тогда не известна).
    """
    classes = [str(c) for c in classes or []]
    segs = [s.strip() for s in re.split(r"[·;]", str(rate_text or "")) if s.strip()]
    out = {}
    for seg in segs:
        nums = re.findall(r"(\d+(?:[.,]\d+)?)\s*%", seg)
        if len(nums) != 1:
            continue
        v = float(nums[0].replace(",", "."))
        label = re.split(r"\d", seg, maxsplit=1)[0].lower()
        words = [w for w in re.split(r"[^a-zа-яё]+", label) if w]
        if not words:
            if len(segs) == 1:
                return {"*": v}
            continue
        for stems, cands in RATE_TEXT_LABELS:
            hit = any((w == s) if len(s) <= 3 else w.startswith(s) for w in words for s in stems)
            if not hit:
                continue
            cls = next((c for c in cands if c in classes and c not in out), None)
            if cls:
                out[cls] = v
            break
    return out


def class_min(ref, product: Optional[dict], cls: str) -> dict:
    """
    Минимальная ставка класса части по тарифной политике: первый класс продукта — справочник min_rates (как у
    однопродуктового акта); остальные — ставка части из текста тарифа продукта; класс не из состава продукта или
    ставки нет — None (базой станет техническая ставка класса). source: min_rates | rate_text | rate_text_common |
    none | not_in_product | no_product.
    """
    code = (product or {}).get("code")
    if not code:
        return {"pct": None, "source": "no_product"}
    classes = list(ref.product_classes.get(code) or [])
    if cls not in classes:
        return {"pct": None, "source": "not_in_product"}
    company = min_rate(ref, code)["company"]
    if classes and cls == classes[0] and company is not None:
        return {"pct": float(company), "source": "min_rates"}
    texts = part_rates_from_text((product or {}).get("rate_text"), classes)
    if cls in texts:
        return {"pct": texts[cls], "source": "rate_text"}
    if "*" in texts:
        return {"pct": texts["*"], "source": "rate_text_common"}
    return {"pct": None, "source": "none"}


def part_rate(ref, product: Optional[dict], cls: str, level: str, sum_insured: float, term_days: int = 365,
              object_type: Optional[str] = None, payer_type: Optional[str] = None,
              settings: Optional[dict] = None, cm: Optional[dict] = None) -> dict:
    """
    Ставка части — тот же act_engine.rate, но минимум (и база тарифной политики) — своего класса (class_min):
    у второго и следующих классов продукта в справочнике min_rates ставки нет, она есть только в тексте тарифа.
    Обязательный вид и «по согласованию» — как у rate (нормативный акт без поправок / ставка не определена).
    """
    import dataclasses
    code = (product or {}).get("code")
    mode = (product or {}).get("pricing_mode")
    cm = cm if cm is not None else class_min(ref, product, cls)
    ref2 = ref
    if code and mode not in NEGOTIATED_MODES and mode != STATUTORY_MODE and cm.get("source") != "min_rates":
        slot = dict(ref.min_rates.get(code) or {"company": {}, "regulator": {}})
        slot["company"] = {None: float(cm["pct"])} if cm.get("pct") is not None else {}
        slot.setdefault("regulator", {})
        ref2 = dataclasses.replace(ref, min_rates={**ref.min_rates, code: slot})
    out = rate(ref2, product, cls, level, sum_insured, term_days, object_type, payer_type, settings)
    out["class_min"] = dict(cm)
    if out["mode"] in ("statutory", "statutory_undefined"):
        # обязательная часть: ставка и минимум — только нормативный акт, тарифная политика не участвует
        out["class_min"] = {"pct": out.get("min_pct"), "source": "act"}
    if out["mode"] == "tariff":
        src = cm.get("source")
        if src in ("rate_text", "rate_text_common"):
            # минимум взят из текста тарифа продукта: это минимум класса части, а не «ставка продукта» —
            # строки «как посчитано» называют его так (три языка — act_texts, how_part_*_text)
            rename = {"how_base_product": "how_part_base_text", "how_min_ok": "how_part_min_ok_text",
                      "how_min_applied": "how_part_min_applied_text"}
            renamed = False
            for h in out["how"]:
                if h["code"] in rename:
                    renamed = renamed or h["code"] == "how_base_product"
                    h["code"] = rename[h["code"]]
                    h["params"] = dict(h["params"], cls=cls, code=code)
            if not renamed:
                out["how"].insert(0, {"code": "how_part_min_text", "params": {"rate": cm["pct"], "cls": cls,
                                                                             "code": code}})
        elif src == "not_in_product":
            out["how"].insert(0, {"code": "how_part_outside", "params": {"cls": cls, "code": code}})
        elif src == "none":
            out["how"].insert(0, {"code": "how_part_min_none", "params": {"cls": cls, "code": code}})
    return out


def split_sum(S: float, classes: list, shares: Optional[dict] = None) -> list:
    """Сумма договора по классам: по долям {класс: %} (сумма 100) или поровну. Округление до сума, остаток —
    последней части: сумма частей точно равна S."""
    classes = [str(c) for c in classes]
    n = len(classes)
    if not n:
        return []
    if shares:
        pcts = [float(shares.get(c, 0) or 0) for c in classes]
        tot = sum(pcts) or 100.0
        pcts = [p * 100.0 / tot for p in pcts]
    else:
        pcts = [100.0 / n] * n
    sums = [float(round(S * p / 100)) for p in pcts]
    sums[-1] = round(float(S) - sum(sums[:-1]), 2)
    return [{"class_code": c, "sum_insured": s, "share_pct": round(p, 4)} for c, p, s in zip(classes, pcts, sums)]


def check_parts_sum(parts: list, S: float, tol: float = 1.0) -> Optional[dict]:
    """Сумма частей против страховой суммы договора (допуск tol сумов). None — сходится, иначе {total, diff}."""
    total = round(sum(float(p["sum_insured"]) for p in parts), 2)
    if abs(total - float(S)) <= float(tol) + 1e-6:
        return None
    return {"total": total, "sum_insured": float(S), "diff": round(float(S) - total, 2)}


def _norm_name(s) -> str:
    return " " + re.sub(r"\s+", " ", str(s or "").lower().replace("ё", "е")) + " "


def match_class(name, classes: list, words: Optional[dict] = None) -> tuple:
    """(класс, число совпавших слов) по наименованию объекта договора; только классы продукта. Нет слов — (None, 0).
    words — слова из шаблонов классов {класс: [основы]} (виды объекта); дополняются PART_WORDS."""
    text = _norm_name(name)
    best, score = None, 0
    for c in classes:
        stems = set(PART_WORDS.get(str(c), ())) | set((words or {}).get(str(c), ()))
        n = 0
        for s in stems:
            s2 = str(s or "").lower().replace("ё", "е")
            if s2.strip() and s2 in text:
                n += 1
        if n > score:
            best, score = str(c), n
    return best, score


def parts_from_items(items: list, classes: list, words: Optional[dict] = None) -> list:
    """
    Части по перечню объектов договора [{name, sum}]: класс — по наименованию (match_class). Объекты одного класса
    складываются в одну часть (один условный договор). Не узнан — к первому классу продукта с пометкой guess.
    """
    by = {}
    for it in items or []:
        cls, _score = match_class(it.get("name"), classes, words)
        guess = cls is None
        cls = cls or str(classes[0])
        p = by.setdefault(cls, {"class_code": cls, "sum_insured": 0.0, "names": [], "class_guess": False})
        p["sum_insured"] = round(p["sum_insured"] + float(it["sum"]), 2)
        if it.get("name"):
            p["names"].append(it["name"])
        p["class_guess"] = p["class_guess"] or guess
    order = {str(c): i for i, c in enumerate(classes)}
    return sorted(by.values(), key=lambda p: order.get(p["class_code"], 99))


def part_risk_inputs(common: dict, fields: Optional[dict], main: bool) -> dict:
    """
    Входы risk_level части. Часть того же объекта (main) берёт осмотр и признаки объекта договора; часть другого
    объекта — только историю убытков и документы страхователя (осмотр был не её объекта). Поля части — поверх.
    """
    f = fields or {}
    if main:
        out = dict(common)
    else:
        out = {"inspected": False, "damages": [], "condition": None, "year": None, "location": None, "guard": None,
               "losses_count": common.get("losses_count"), "documents": common.get("documents"),
               "today": common.get("today")}
    for k in ("condition", "year", "location", "guard", "losses_count"):
        if f.get(k) is not None:
            out[k] = f[k]
    if f.get("documents_provided") is not None:
        out["documents"] = bool(f["documents_provided"])
    return out


def level_max(levels) -> str:
    """Уровень договора — самый высокий среди частей."""
    lv = [x for x in levels if x in LEVEL_ORDER]
    return max(lv, key=lambda x: LEVEL_ORDER[x]) if lv else "moderate"


def aggregate_scenarios(parts: list) -> dict:
    """
    Сценарии договора из сценариев частей. parts: [{"index", "class_code", "main": bool (тот же объект, что часть 1),
    "scenarios": блок act_extras.scenarios}]. Части одного объекта (main) — большее по каждому сценарию; части других
    объектов — прибавляются. Часть без правила сценария — не считается и в итог не входит (excluded).
    """
    avail = [p for p in parts if (p.get("scenarios") or {}).get("available")]
    excluded = [{"index": p["index"], "class_code": p["class_code"],
                 "reason": (p.get("scenarios") or {}).get("reason") or "sc_na_class"}
                for p in parts if p not in avail]
    main = [p for p in avail if p.get("main")]
    other = [p for p in avail if not p.get("main")]
    if not other:
        rule = "max"
    elif len(main) <= 1:
        rule = "sum"
    else:
        rule = "mixed"
    out = {"available": bool(avail), "rule": rule, "items": {}, "excluded": excluded,
           "main": [p["index"] for p in main], "other": [p["index"] for p in other], "calibrated": CALIBRATED}
    for s in SCENARIOS3:
        mv = [(p["index"], float(p["scenarios"]["items"][s]["amount"])) for p in main]
        ov = [(p["index"], float(p["scenarios"]["items"][s]["amount"])) for p in other]
        top = max(mv, key=lambda x: x[1]) if mv else None
        amount = (top[1] if top else 0.0) + sum(v for _i, v in ov)
        out["items"][s] = {"amount": round(amount),
                           "main_max": {"index": top[0], "amount": round(top[1])} if top else None,
                           "main_values": [{"index": i, "amount": round(v)} for i, v in mv],
                           "added": [{"index": i, "amount": round(v)} for i, v in ov]}
    if avail:
        a = [out["items"][s]["amount"] for s in SCENARIOS3]
        out["order_ok"] = a[0] <= a[1] <= a[2]
    return out


def contract_retention(part_rets: list, eml: float, mfl: Optional[float] = None) -> Optional[dict]:
    """
    Удержание договора против EML договора (решение заказчика 21.09.2026). Лимит — наименьший из лимитов частей
    (строже всех — таблица линий по классу части; так же risk_analytics._retention берёт наименьшую линию по классам).
    """
    known = [r for r in part_rets if r and r.get("known") and r.get("limit") is not None]
    if not known:
        base = next((r for r in part_rets if r), None)
        return dict(base, known=False, eml_excess=None, within=None, mfl_excess=None) if base else None
    best = min(known, key=lambda r: float(r["limit"]))
    limit = float(best["limit"])
    out = dict(best)
    out.update(compared_with="eml", eml_excess=round(max(float(eml) - limit, 0)), within=float(eml) <= limit,
               mfl_excess=(round(max(float(mfl) - limit, 0)) or None) if mfl is not None else None,
               basis="min_of_parts")
    return out


def contract_value(parts: list, settings: Optional[dict] = None) -> Optional[dict]:
    """Сумма к стоимости по договору: сумма частей со страховой стоимостью к их стоимости (value_check)."""
    rows = [p for p in parts if p.get("value_applicable")]
    if not rows:
        return None
    S = sum(float(p["sum_insured"]) for p in rows)
    V = sum(float(p["object_value"]) for p in rows)
    if V <= 0:
        return None
    out = value_check(S, V, settings)
    out.update(sum_insured=round(S, 2), object_value=round(V, 2), parts=[p["index"] for p in rows])
    return out


def contract_totals(parts: list, S: float) -> dict:
    """Итоги договора: премия = сумма премий частей (если у всех частей она есть); справочная ставка договора =
    премия / сумма × 365 / срок — только справочно, минимум по ней не проверяется (правило проекта № 5)."""
    prem = [p.get("premium") for p in parts]
    known = [float(x) for x in prem if x is not None]
    complete = len(known) == len(prem) and bool(prem)
    total = round(sum(known)) if complete else None
    term = int(parts[0].get("term_days") or 365) if parts else 365
    ref_pct = round(total / float(S) * 100 * 365 / term, 4) if total is not None and S else None
    return {"premium": total, "premium_known": round(sum(known)) if known else None, "premium_complete": complete,
            "sum_insured": round(sum(float(p["sum_insured"]) for p in parts), 2),
            "level": level_max([p.get("level") for p in parts]),
            "reference_rate_pct": ref_pct, "reference_only": True, "term_days": term, "calibrated": CALIBRATED}


# ================================================================================================
#  10. Кредиты (правило проекта № 6, 30.09.2026): сумма и страхователь-банк
# ================================================================================================

# слова, по которым название юрлица — банк (узбекская кириллица и латиница, русский, английский)
_BANK_WORDS = {"банк", "банки", "банка", "банкаси", "bank", "banki", "bankasi", "акб", "akb", "атб", "atb",
               "чакб", "chakb"}


def is_bank(name) -> Optional[bool]:
    """Название организации — банк? «АКБ Хамкорбанк», «Xalq banki», «Kapitalbank ATB» → True; нет названия — None."""
    s = str(name or "").strip().lower().replace("ё", "е")
    if not s:
        return None
    words = re.findall(r"[a-zа-яўқғҳ0-9ʻʼ']+", s)
    return any(w in _BANK_WORDS or (len(w) > 5 and (w.endswith("bank") or w.endswith("банк")
                                                    or w.endswith("banki") or w.endswith("банки")))
               for w in words)


def credit_check(S: float, fields: Optional[dict], max_share: float = 0.5,
                 policyholder: Optional[dict] = None) -> list:
    """
    Проверки андеррайтеру по кредиту (класс 14 и 13з; правило проекта № 6, требования НАПП). Чистая функция.
      * сумма кредита и стоимость обеспечения не введены — credit_need_data: проверить вручную;
      * страховая сумма выше min(кредит − обеспечение; max_share × кредит) — credit_over: уменьшить до допустимой;
      * страхователь: policyholder = {"kind": legal | individual | None, "name", "source"} из запроса или
        договора. Юрлицо не банк или гражданин — credit_holder_not_bank; не известен — credit_holder_unknown.
    Возвращает [{"code", "params"}] (коды без префикса; акт добавляет c_ / c_part_).
    Пример: кредит 100 млн, залог 60 млн, сумма 50 млн → допустимо 40 млн, превышение 10 млн.
    """
    f = fields or {}
    out = []
    S = float(S)

    def num(v):
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None

    c, k = num(f.get("credit_amount")), num(f.get("collateral_value"))
    if not c or k is None:
        out.append({"code": "credit_need_data", "params": {"sum": round(S),
                                                           "share_pct": round(float(max_share) * 100)}})
    else:
        unsecured = max(c - k, 0.0)
        cap = c * float(max_share)
        insurable = min(unsecured, cap)
        if S > insurable + 0.5:
            out.append({"code": "credit_over", "params": {
                "credit": round(c), "collateral": round(k), "insurable": round(insurable),
                "by": "unsecured" if unsecured <= cap else "cap", "sum": round(S), "excess": round(S - insurable),
                "share_pct": round(float(max_share) * 100)}})
    ph = policyholder or {}
    kind, name = ph.get("kind"), ph.get("name")
    bank = is_bank(name) if kind == "legal" else (False if kind == "individual" else None)
    if bank is False:
        out.append({"code": "credit_holder_not_bank",
                    "params": {"holder": name if kind == "legal" and name else None,
                               "individual": kind == "individual", "source": ph.get("source")}})
    elif bank is None:
        out.append({"code": "credit_holder_unknown", "params": {}})
    return out


# ================================================================================================
#  11. Страховой скоринг объекта (01.10.2026): представление уже посчитанного акта шкалой 0–500
# ================================================================================================
# Это не новый расчёт: балл берётся из балла риска 0–100 аналитики акта (act_analytics.score, чем выше — тем
# хуже) и переворачивается в шкалу «чем выше — тем лучше»: балл = round(500 − 5 × балл риска). Нет балла риска
# (класс без аналитики, старый акт) — оценка по уровню риска акта. Шкала экспертная, calibrated = 0; это не
# кредитный скоринг и не оценка КАТМ.

SCORE_VERSION = "1.0"
SCORE_MIN, SCORE_MAX = 0, 500
# (класс, от, до, код подписи): E плохой … A отличный
SCORE_BANDS = (("E", 0, 99, "poor"), ("D", 100, 199, "weak"), ("C", 200, 299, "fair"),
               ("B", 300, 399, "good"), ("A", 400, 500, "excellent"))
SCORE_LABELS_RU = {"poor": "плохой", "weak": "слабый", "fair": "средний", "good": "хороший", "excellent": "отличный"}
SCORE_BY_LEVEL = {"low": 430, "moderate": 300, "high": 130}
SCORE_NOTE = "экспертно, не калибровано; это не кредитный скоринг и не оценка КАТМ"


# сектора привязаны к порогам балла риска аналитики (risk_thresholds.level_bounds, по умолчанию 20/40/60/80):
# граница сектора = 500 − 5 × порог; класс — тот же уровень, что у аналитики (A низкий … E критический)
SCORE_BOUNDS = (20.0, 40.0, 60.0, 80.0)
BAND_LEVEL5 = {"A": "low", "B": "moderate", "C": "elevated", "D": "high", "E": "critical"}


def score_bounds(bounds=None) -> tuple:
    """Пороги балла риска 0–100 (четыре возрастающих числа между 0 и 100); кривые — пороги по умолчанию."""
    try:
        b = tuple(float(x) for x in bounds)
    except (TypeError, ValueError):
        return SCORE_BOUNDS
    if len(b) != 4 or list(b) != sorted(b) or len(set(b)) != 4 or b[0] <= 0 or b[-1] >= 100:
        return SCORE_BOUNDS
    return b


def score_bands(bounds=None) -> list:
    """Сектора [(класс, от, до, подпись)] E…A по порогам аналитики: E 0 … (500 − 5 × порог4) − 1, …, A — до 500.
    По умолчанию E 0–99, D 100–199, C 200–299, B 300–399, A 400–500."""
    b = score_bounds(bounds)
    edges = [SCORE_MIN]
    for x in reversed(b):
        edges.append(max(edges[-1] + 1, round_half_up(round(SCORE_MAX - 5 * x, 6))))
    edges.append(SCORE_MAX + 1)
    return [(code, edges[i], min(edges[i + 1] - 1, SCORE_MAX), lab)
            for i, (code, _lo, _hi, lab) in enumerate(SCORE_BANDS)]


def score_band(score, bounds=None, risk=None) -> dict:
    """Класс и подкласс балла 0–500. Сектор — по неокруглённому баллу риска risk (если он есть) и порогам аналитики:
    так класс всегда совпадает с уровнем аналитики (балл риска ровно 20 — «умеренный» → B, хотя 500 − 5 × 20 = 400);
    без балла риска — по баллу. Подкласс 1 — верхняя треть сектора, 2 — средняя, 3 — нижняя (A1 = 468–500,
    A2 = 434–467, A3 = 400–433; у секторов по 100 баллов: B1 = 367–399, B2 = 334–366, B3 = 300–333)."""
    s = max(SCORE_MIN, min(SCORE_MAX, round_half_up(round(float(score), 6))))
    bands = score_bands(bounds)
    band = None
    if risk is not None:
        try:
            r = float(risk)
            idx = sum(1 for x in score_bounds(bounds) if r >= x)        # 0 — низкий (A) … 4 — критический (E)
            band = next(x for x in bands if x[0] == "ABCDE"[idx])
        except (TypeError, ValueError):
            band = None
    if band is None:
        band = next(x for x in bands if x[1] <= s <= x[2])
    code, lo, hi, lab = band
    k = (min(max(s, lo), hi) - lo) / (hi - lo + 1)      # балл на границе после округления — к краю своего сектора
    sub = 1 if k >= 2 / 3 else (2 if k >= 1 / 3 else 3)
    return {"class": code, "sub": sub, "class_code": f"{code}{sub}", "label_code": lab,
            "class_label": SCORE_LABELS_RU[lab], "from": lo, "to": hi, "level5": BAND_LEVEL5[code]}


def score_scale(bounds=None) -> dict:
    b = score_bounds(bounds)
    return {"min": SCORE_MIN, "max": SCORE_MAX, "bounds": list(b),
            "bands": [{"code": c, "from": lo, "to": hi, "label_code": lab, "label": SCORE_LABELS_RU[lab]}
                      for c, lo, hi, lab in score_bands(b)]}


def bands_text(bounds=None) -> str:
    """«E 0–99, D 100–199, C 200–299, B 300–399, A 400–500» — для объяснения метода."""
    return ", ".join(f"{c} {lo}–{hi}" for c, lo, hi, _l in score_bands(bounds))


def _apportion(raw: list, target: int) -> list:
    """Целые части с суммой ровно target (наибольшие остатки); raw — неотрицательные доли."""
    if not raw:
        return []
    base = [math.floor(x) for x in raw]
    rest = int(target) - sum(base)
    order = sorted(range(len(raw)), key=lambda i: raw[i] - base[i], reverse=True)
    i = 0
    while rest > 0:
        base[order[i % len(raw)]] += 1
        rest -= 1
        i += 1
    order = sorted(range(len(raw)), key=lambda i: raw[i] - base[i])
    i = 0
    while rest < 0 and any(base):
        j = order[i % len(raw)]
        if base[j] > 0:
            base[j] -= 1
            rest += 1
        i += 1
    return base


def _score_of(analytics: Optional[dict]) -> Optional[tuple]:
    """(балл риска 0–100, составляющие, пороги уровней) из аналитики акта или None."""
    an = analytics or {}
    sc = an.get("score") or {}
    if not an.get("available") or not sc.get("available") or sc.get("score") is None:
        return None
    try:
        return float(sc["score"]), list(sc.get("components") or []), score_bounds(sc.get("bounds"))
    except (TypeError, ValueError):
        return None


def _num_ru(x, digits: int = 3) -> str:
    s = f"{float(x):.{digits}f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")


def score_one(analytics: Optional[dict], level: Optional[str]) -> dict:
    """
    Балл одного объекта (части): из балла риска аналитики или по уровню риска акта. Составляющие — те же, что у
    балла риска, в шкале 0–500: составляющая = вес × (100 − её балл риска) × 5, «из» = вес × 500; сумма составляющих
    равна баллу (округление до целого распределено по наибольшим остаткам).
    """
    got = _score_of(analytics)
    if got is None:
        lvl = level if level in SCORE_BY_LEVEL else "moderate"
        s = SCORE_BY_LEVEL[lvl]
        return {"score": s, **score_band(s), "basis": "level", "level": lvl, "risk_score_100": None,
                "bounds": list(SCORE_BOUNDS), "exact": True, "analytics_level": None,
                "components": [{"code": "act_level", "label": "Уровень риска акта", "points": s, "max": SCORE_MAX,
                                "risk_points": None, "weight": None, "applicable": True,
                                "why": f"оценка по уровню риска: {RA_LEVEL.get(lvl, lvl).lower()} — {s}",
                                "calibrated": CALIBRATED}]}
    risk, comps, bounds = got
    # половина — вверх, как у Math.round на экране; 6 знаков — чтобы 500 − 5 × 32,7 дало ровно 336,5
    s = max(SCORE_MIN, min(SCORE_MAX, round_half_up(round(SCORE_MAX - 5 * risk, 6))))
    exact = abs(round(SCORE_MAX - 5 * risk, 6) - s) < 1e-9          # «=» или «≈» в объяснении
    on = [i for i, c in enumerate(comps) if c.get("applicable") and float(c.get("weight") or 0) > 0]
    raw_pts = [float(comps[i]["weight"]) * (100 - float(comps[i].get("points") or 0)) * 5 for i in on]
    raw_max = [float(comps[i]["weight"]) * SCORE_MAX for i in on]
    mx = _apportion(raw_max, SCORE_MAX)
    pts = _apportion(raw_pts, s)
    # составляющая не больше своего «из»: лишнее — соседям с запасом (бывает только от округления)
    for i in range(len(pts)):
        while pts[i] > mx[i]:
            room = [k for k in range(len(pts)) if pts[k] < mx[k]]
            if not room:
                break
            j = max(room, key=lambda k: mx[k] - pts[k])
            pts[i] -= 1
            pts[j] += 1
    pos = {ci: k for k, ci in enumerate(on)}
    out = []
    for ci, c in enumerate(comps):
        p_risk = c.get("points")
        if ci in pos:
            k = pos[ci]
            row = {"points": pts[k], "max": mx[k], "applicable": True,
                   "why": (f"балл риска {_num_ru(p_risk or 0, 1)} из 100, вес {_num_ru(c['weight'])}: "
                           f"{pts[k]} из {mx[k]}")}
        else:
            row = {"points": 0, "max": 0, "applicable": False,
                   "why": "не учтено: нет данных или к объекту не относится"}
        out.append({"code": c.get("code"), "label": c.get("name_ru") or c.get("name") or c.get("code"), **row,
                    "risk_points": p_risk, "weight": c.get("weight"), "calibrated": CALIBRATED})
    if not on:                                   # составляющих нет — балл объясняется одной строкой
        out.append({"code": "risk_score", "label": "Балл риска 0–100", "points": s, "max": SCORE_MAX,
                    "applicable": True, "risk_points": risk, "weight": 1.0,
                    "why": f"500 − 5 × {_num_ru(risk, 1)} {'=' if exact else '≈'} {s}", "calibrated": CALIBRATED})
    an_level = ((analytics or {}).get("score") or {}).get("level")
    return {"score": s, **score_band(s, bounds, risk), "basis": "risk_score", "level": level, "risk_score_100": risk,
            "bounds": list(bounds), "exact": exact, "analytics_level": an_level, "components": out}


def insurance_score(act_data: dict) -> dict:
    """
    Страховой скоринг объекта по данным акта (структура build_data, язык не важен). Чистая функция.
    {score 0–500 (чем выше, тем лучше), class A–E, class_label, sub 1–3, version, scale, components[{code, label,
    points, max, why}], risk_score_100, basis (risk_score | level), parts[], worst_part, calibrated 0, method_text}.
    Договор из нескольких частей: балл договора — по самой опасной части (наименьший балл), баллы частей — в parts.
    Пример: балл риска 32,7 → 500 − 5 × 32,7 = 336,5 → 337 (половина — вверх), класс B2 (хороший).
    """
    D = act_data or {}
    pblock = D.get("parts") or {}
    parts = [p for p in (pblock.get("items") or []) if isinstance(p, dict)]
    multi = pblock.get("mode") == "multi" and bool(parts)
    rows = []
    if multi:
        for p in parts:
            rows.append(dict(score_one(p.get("analytics"), p.get("level")), index=p.get("index"),
                             part_class=p.get("class_code")))
        main = min(rows, key=lambda r: r["score"])     # самая опасная часть — с наименьшим баллом
    else:
        main = score_one(D.get("analytics"), (D.get("risk") or {}).get("level"))
    # повреждения с фото (06.10.2026): штраф балла −10 (косметические) / −30 (существенные), экспертно
    dmg = damages_summary((D.get("inspection") or {}).get("damages"))
    main = damage_penalty(main, dmg["penalty"], dmg["count"], dmg["severity"])
    bounds = main.get("bounds") or list(SCORE_BOUNDS)
    method = ("балл = 500 − 5 × балл риска 0–100 аналитики акта (чем выше балл риска, тем хуже); "
              f"сектора по порогам аналитики: {bands_text(bounds)}; подкласс 1 — верхняя треть сектора"
              if main["basis"] == "risk_score" else
              "оценка по уровню риска: балла риска нет (класс без аналитики) — низкий 430, умеренный 300, высокий 130")
    return {"score": main["score"], "class": main["class"], "class_label": main["class_label"],
            "label_code": main["label_code"], "sub": main["sub"], "class_code": main["class_code"],
            "version": SCORE_VERSION, "scale": score_scale(bounds), "components": main["components"],
            "risk_score_100": main["risk_score_100"], "basis": main["basis"], "level": main.get("level"),
            "exact": main.get("exact", True), "analytics_level": main.get("analytics_level"),
            "contract": multi, "worst_part": main.get("index") if multi else None,
            "parts": [{"index": r["index"], "class_code": r["part_class"], "score": r["score"], "class": r["class"],
                       "sub": r["sub"], "score_class": r["class_code"], "class_label": r["class_label"],
                       "label_code": r["label_code"], "risk_score_100": r["risk_score_100"], "basis": r["basis"],
                       "level": r.get("level")} for r in rows],
            "method_text": method, "note": SCORE_NOTE, "calibrated": CALIBRATED,
            "damage_penalty": main.get("damage_penalty", 0), "damage_severity": main.get("damage_severity"),
            "damage_count": main.get("damage_count", 0), "score_before_damages": main.get("score_before_damages")}


def damage_penalty(row: dict, pen: int, n: int = 0, severity: Optional[str] = None) -> dict:
    """
    Штраф балла скоринга за повреждения с фото: балл − pen (не ниже 0), класс — по баллу риска + pen / 5 (так же,
    как без штрафа: 500 − 5 × балл риска), отдельная составляющая «Повреждения с фото» с минусом — сумма
    составляющих по-прежнему равна баллу. Экспертно (−10 косметические, −30 существенные), calibrated = 0.
    Пример: балл 337 (B2), существенные повреждения → 307 (B3).
    """
    if not pen:
        return dict(row, damage_penalty=0, damage_severity=None, damage_count=0, score_before_damages=None)
    s0 = int(row["score"])
    s = max(SCORE_MIN, s0 - int(pen))
    eff = s0 - s
    risk = row.get("risk_score_100")
    bounds = row.get("bounds") or list(SCORE_BOUNDS)
    band = score_band(s, bounds, round(float(risk) + eff / 5, 6)) if row.get("basis") == "risk_score" \
        and risk is not None else score_band(s, bounds)
    comp = {"code": "damages", "label": "Повреждения с фото", "points": -eff, "max": 0, "applicable": True,
            "risk_points": None, "weight": None, "n": n, "severity": severity,
            "why": f"повреждения с фото ({n}, {'существенные' if severity == 'major' else 'косметические'}): "
                   f"−{eff} (экспертно, не калибровано)", "calibrated": CALIBRATED}
    out = dict(row, score=s, **band)
    out["components"] = list(row.get("components") or []) + [comp]
    out.update(damage_penalty=eff, damage_severity=severity, damage_count=n, score_before_damages=s0)
    return out


# ================================================================================================
#  12. Отчёт кредитного бюро (КАТМ) по заёмщику (01.10.2026): только проверки андеррайтеру
# ================================================================================================

BUREAU_CLASSES = ("A", "B", "C", "D", "E")
CREDIT_REPORT_CLASSES = ("14", "13з", "15")


def bureau_letter(score_class) -> Optional[str]:
    """«A1», «a2», кириллица «А1», «C» → буква класса оценки бюро; иначе None."""
    s = str(score_class or "").strip().upper().translate(str.maketrans("АВСЕ", "ABCE"))
    return s[0] if s[:1] in BUREAU_CLASSES else None


def borrower_checks(fields: Optional[dict], credit_product: bool, settings: Optional[dict] = None,
                    today: Optional[date] = None) -> list:
    """
    Проверки андеррайтеру по отчёту кредитного бюро (классы 14, 13з, 15). Чистая функция; в уровень риска и в
    ставку не входят (пока заказчик не утвердит правило). Коды без префикса c_:
      borrower_low_class — класс оценки бюро не лучше порога (по умолчанию C и ниже);
      borrower_no_score — в отчёте нет ни балла, ни класса;
      borrower_overdue — есть действующая просрочка (просроченная часть действующих договоров больше нуля);
      borrower_stale — отчёт старше max_age_days (по умолчанию 30 дней) или без даты.
    Пример: класс D2, просрочка 5 000 000, отчёт от 01.08.2026 при сегодня 01.10.2026 → все три проверки.
    """
    if not credit_product or not fields:
        return []
    st = {**DEFAULT_SETTINGS["credit_report"], **(settings or {})}
    today = today or date.today()
    out = []
    letter = bureau_letter(fields.get("score_class"))
    low = st.get("low_class") if st.get("low_class") in BUREAU_CLASSES else "C"
    if letter and BUREAU_CLASSES.index(letter) >= BUREAU_CLASSES.index(low):
        out.append({"code": "borrower_low_class", "params": {"cls": fields.get("score_class"), "low": low,
                                                             "score": fields.get("score")}})
    elif not letter and fields.get("score") is None:
        out.append({"code": "borrower_no_score", "params": {}})
    od = (fields.get("active") or {}).get("overdue")
    if isinstance(od, (int, float)) and not isinstance(od, bool) and od > 0:
        out.append({"code": "borrower_overdue", "params": {"amount": od}})
    days = None
    try:
        days = (today - date.fromisoformat(str(fields.get("report_date")))).days
    except (TypeError, ValueError):
        pass
    mx = int(st.get("max_age_days") or 30)
    if days is None or days > mx:
        out.append({"code": "borrower_stale", "params": {"days": days, "max": mx}})
    return out


# ================================================================================================
#  12. Вилка ставки (01.10.2026): минимум → ставка акта → с учётом региона и рынка → рынок
# ================================================================================================
#
# Заказчик: «система выдаёт вилку ставки с объяснением, из чего она сложилась», и ставка должна опираться на
# подключённые данные (stat.uz / data.egov.uz — показатели региона, НАПП — рынок класса). Правило:
#   1. минимальная ставка — тарифная политика (ниже — только отступление);
#   2. ставка акта — как прежде (rate / part_rate), не меняется;
#   3. ставка с учётом региона и рынка = ставка акта × (1 + поправка региона) × (1 + поправка рынка), не ниже минимума;
#      поправка региона = среднее по подходящим показателям (отношение «регион / республика» − 1) × чувствительность,
#      в границах rate_fork.region.min_pct … max_pct; поправка рынка — ступени по убыточности класса НАПП, если
#      ставка акта ниже рыночной;
#   4. рыночная ставка — средняя по классу по НАПП (верхний ориентир; бывает ниже минимума — это показывается).
# Здесь только числа и коды; данные из базы собирает act_analytics.fork_data, слова — act.py и act_texts.py.
# Все пороги — экспертные (calibrated = 0), задаются настройкой rate_fork.

FORK_MODES = ("reference", "apply")
FORK_GROUPS = ("special", "vehicle", "property", "equipment", "cargo", "liability", "other", "*")
FORK_LR_BASIS = ("last", "full_year")
FORK_MARKET_BASIS = ("full_year_if_available", "last")
FORK_MARKS = ("min", "act", "adjusted", "market", "request", "contract", "technical", "factors")


def fork_settings(st: Optional[dict]) -> dict:
    """Настройки вилки поверх умолчаний: region и market сливаются по ключам (правка одного ключа не стирает
    остальные)."""
    d = DEFAULT_SETTINGS["rate_fork"]
    raw = (st or {}).get("rate_fork") if isinstance((st or {}).get("rate_fork"), dict) else {}
    out = {"mode": raw.get("mode", d["mode"]), "calibrated": CALIBRATED}
    for k in ("region", "market"):
        sub = raw.get(k) if isinstance(raw.get(k), dict) else {}
        out[k] = {**d[k], **sub}
    return out


def check_fork_settings(raw) -> list:
    """Ошибки настройки rate_fork (пустой список — верно). raw — то, что прислал администратор (или None)."""
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return ["rate_fork: словарь {mode, region, market}"]
    errs = []
    extra = [k for k in raw if k not in ("mode", "region", "market", "calibrated")]
    if extra:
        errs.append("rate_fork: неизвестные ключи " + ", ".join(extra))
    if "calibrated" in raw and raw["calibrated"] != 0:
        errs.append("rate_fork.calibrated: 0 — пороги экспертные, не калиброваны")
    for k in ("region", "market"):
        if k in raw and not isinstance(raw[k], dict):
            errs.append(f"rate_fork.{k}: словарь")
    fs = fork_settings({"rate_fork": raw})
    if fs["mode"] not in FORK_MODES:
        errs.append("rate_fork.mode: reference или apply")
    rg = fs["region"]
    extra = [k for k in rg if k not in ("sensitivity", "min_pct", "max_pct", "indicators", "weights")]
    if extra:
        errs.append("rate_fork.region: неизвестные ключи " + ", ".join(extra))
    for key, lo, hi in (("sensitivity", 0, 3), ("min_pct", -50, 0), ("max_pct", 0, 100)):
        v = rg.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            errs.append(f"rate_fork.region.{key}: число от {lo} до {hi}")
    ind = rg.get("indicators")
    if not isinstance(ind, dict):
        errs.append("rate_fork.region.indicators: словарь {класс: {группа объекта: [показатели]}}")
    else:
        from . import risk_stats              # справочник показателей (без базы и сети)
        for cls, by_group in ind.items():
            if not isinstance(by_group, dict):
                errs.append(f"rate_fork.region.indicators.{cls}: словарь {{группа: [показатели]}}")
                continue
            for g, ids in by_group.items():
                if g not in FORK_GROUPS:
                    errs.append(f"rate_fork.region.indicators.{cls}: группа «{g}» — одна из " + ", ".join(FORK_GROUPS))
                if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids):
                    errs.append(f"rate_fork.region.indicators.{cls}.{g}: список кодов показателей")
                    continue
                bad = [x for x in ids if x not in risk_stats.BY_ID]
                if bad:
                    errs.append(f"rate_fork.region.indicators.{cls}.{g}: нет показателей " + ", ".join(bad))
    wts = rg.get("weights", {})
    if not isinstance(wts, dict):
        errs.append("rate_fork.region.weights: словарь {показатель: вес}")
    else:
        from . import risk_stats
        for k, v in wts.items():
            if k not in risk_stats.BY_ID:
                errs.append(f"rate_fork.region.weights: нет показателя {k}")
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 5:
                errs.append(f"rate_fork.region.weights.{k}: число от 0 до 5")
    mk = fs["market"]
    extra = [k for k in mk if k not in ("steps", "loss_ratio", "basis", "cap_at_market")]
    if extra:
        errs.append("rate_fork.market: неизвестные ключи " + ", ".join(extra))
    if mk.get("basis") not in FORK_MARKET_BASIS:
        errs.append("rate_fork.market.basis: full_year_if_available или last")
    if not isinstance(mk.get("cap_at_market"), bool):
        errs.append("rate_fork.market.cap_at_market: true или false")
    if mk.get("loss_ratio") not in FORK_LR_BASIS:
        errs.append("rate_fork.market.loss_ratio: last или full_year")
    steps = mk.get("steps")
    ok_steps = isinstance(steps, list) and all(
        isinstance(s, (list, tuple)) and len(s) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                                                            for x in s) for s in steps)
    if not ok_steps:
        errs.append("rate_fork.market.steps: список пар [убыточность %, надбавка %]")
    else:
        if any(not (0 <= s[0] <= 1000 and 0 <= s[1] <= 100) for s in steps):
            errs.append("rate_fork.market.steps: убыточность 0–1000 %, надбавка 0–100 %")
        if any(b[0] <= a[0] or b[1] < a[1] for a, b in zip(steps, steps[1:])):
            errs.append("rate_fork.market.steps: пороги по возрастанию, надбавка не убывает")
    return errs


def fork_indicator_ids(fs: dict, cls: str, group: Optional[str]) -> tuple:
    """(показатели для класса и вида объекта, показатели класса, которые к этому виду не относятся)."""
    by_group = (fs["region"].get("indicators") or {}).get(str(cls)) or {}
    ids = list(by_group.get(group or "") if (group or "") in by_group else by_group.get("*") or [])
    other = []
    for xs in by_group.values():
        for x in xs:
            if x not in ids and x not in other:
                other.append(x)
    return ids, other


def fork_region(indicators: list, fs: dict, region_known: bool, class_has_rules: bool) -> dict:
    """
    Поправка региона. indicators — [{id, ratio (регион / республика) или None, used}] отобранные для класса и вида.
    Вклад показателя = (ratio − 1) × sensitivity × 100 %, поправка = среднее вкладов (взвешенное по
    rate_fork.region.weights, по умолчанию вес 1), в границах [min_pct; max_pct].
    Пример: ДТП 1,735 и кражи 1,215 при чувствительности 0,5 → (36,75 + 10,75) / 2 = 23,75 % → граница +15 %.
    reason (если поправки нет): no_rules — для класса показателей нет; kind — к виду объекта не подходят;
    region_unknown — регион не распознан; no_regional — у показателей нет сравнения региона с республикой;
    zero_weight — сравнение есть только у показателей с весом 0 (act_analytics.fork_data ставит им why =
    weight_zero и used = false: показатель виден, в поправку не входит).
    """
    rg = fs["region"]
    sens, lo, hi = float(rg["sensitivity"]), float(rg["min_pct"]), float(rg["max_pct"])
    out = {"pct": 0.0, "raw_pct": None, "clamped": None, "bounds": [lo, hi], "sensitivity": sens, "used": 0,
           "reason": None, "calibrated": CALIBRATED}
    wts = rg.get("weights") or {}
    used = [i for i in indicators if i.get("ratio") is not None and i.get("used") is not False]
    effects = [round((float(i["ratio"]) - 1) * sens * 100, 4) for i in used]
    weights = [float(wts.get(i.get("id"), 1.0)) for i in used]
    if not class_has_rules:
        out["reason"] = "no_rules"
    elif not indicators:
        out["reason"] = "kind"
    elif not region_known:
        out["reason"] = "region_unknown"
    elif not effects:
        # сравнение есть только у показателей с весом 0 (claims_freq по умолчанию) — поправки нет по настройке
        out["reason"] = "zero_weight" if any(i.get("why") == "weight_zero" for i in indicators) else "no_regional"
    if out["reason"]:
        return out
    wsum = sum(weights)
    raw = round(sum(e * w for e, w in zip(effects, weights)) / wsum, 2) if wsum else 0.0
    out["weights"] = {i.get("id"): w for i, w in zip(used, weights) if w != 1.0}
    out.update(pct=round(min(hi, max(lo, raw)), 2), raw_pct=raw, used=len(effects),
               clamped="max" if raw > hi else ("min" if raw < lo else None))
    return out


def fork_market(act_pct: Optional[float], market_pct: Optional[float], loss_ratio: Optional[float],
                fs: dict) -> dict:
    """
    Поправка рынка: ставка акта ниже рыночной и убыточность класса не ниже порога ступени → надбавка ступени
    (по умолчанию ≥ 60 % → +10 %, ≥ 80 % → +20 %). reason: no_data — нет рыночной ставки или убыточности;
    no_rate — у акта нет ставки; act_not_below — ставка акта не ниже рыночной; lr_below — убыточность ниже порогов;
    applied — надбавка применена.
    """
    steps = [[float(s[0]), float(s[1])] for s in fs["market"]["steps"]]
    out = {"pct": 0.0, "reason": None, "threshold": None, "steps": steps, "calibrated": CALIBRATED}
    if market_pct is None or loss_ratio is None:
        out["reason"] = "no_data"
    elif act_pct is None:
        out["reason"] = "no_rate"
    elif float(act_pct) + 1e-12 >= float(market_pct):
        out["reason"] = "act_not_below"
    else:
        hit = [s for s in steps if float(loss_ratio) + 1e-9 >= s[0]]
        if not hit:
            out["reason"] = "lr_below"
        else:
            out.update(pct=hit[-1][1], threshold=hit[-1][0], reason="applied")
    return out


def _step_of(lr: Optional[float], steps: list) -> float:
    if lr is None:
        return 0.0
    hit = [float(x[1]) for x in steps if float(lr) + 1e-9 >= float(x[0])]
    return hit[-1] if hit else 0.0


def fork_market_lr(market: dict, fs: dict) -> tuple:
    """
    Убыточность для поправки рынка: (значение %, база last | full_year, взят ли полный год вместо среза).
    loss_ratio = full_year — всегда полный год. loss_ratio = last и basis = full_year_if_available (по умолчанию):
    если по последнему срезу ступень надбавки выше, чем по полному году (скачок убыточности за неполный год —
    пакет 3,14 на 01.07.2026: 81,9 % против 2,5 % за 2025 год), берётся полный год с пометкой (full_year_switch).
    basis = last — всегда срез. Полного года нет — срез.
    """
    mset = fs["market"]
    last, fy = market.get("loss_ratio_pct"), market.get("loss_ratio_full_year_pct")
    if mset.get("loss_ratio") == "full_year":
        return fy, "full_year", False
    if mset.get("basis", "full_year_if_available") == "full_year_if_available" and last is not None \
            and fy is not None:
        steps = [[float(x[0]), float(x[1])] for x in mset["steps"]]
        if _step_of(fy, steps) < _step_of(last, steps):
            return fy, "full_year", True
    return last, "last", False


def fork_rate(act_pct: float, region_pct: float, market_pct: float, min_pct: Optional[float]) -> tuple:
    """(ставка с учётом региона и рынка, округлена до 4 знаков; поднята ли до минимума)."""
    r = round(float(act_pct) * (1 + float(region_pct) / 100) * (1 + float(market_pct) / 100), 4)
    if min_pct is not None and r + 1e-12 < float(min_pct):
        return round(float(min_pct), 4), True
    return r, False


def fork_position(rate_pct: Optional[float], min_pct: Optional[float], market_pct: Optional[float]) -> str:
    """Где ставка документа на шкале: below_min | above_market | inside | none."""
    if rate_pct is None:
        return "none"
    if min_pct is not None and float(rate_pct) + 1e-9 < float(min_pct):
        return "below_min"
    if market_pct is not None and float(rate_pct) > float(market_pct) + 1e-9:
        return "above_market"
    return "inside"


def fork_adjust(rate_res: dict, region: dict, market: dict, fs: dict) -> Optional[dict]:
    """
    Поправки и ставка п. 3 для ставки акта rate_res (режим tariff). region — fork_region, market — данные НАПП
    {rate_pct, loss_ratio_pct, loss_ratio_full_year_pct}. None — у акта нет тарифной ставки (обязательный вид,
    «по программе»).
    """
    if rate_res.get("mode") != "tariff" or rate_res.get("applied_pct") is None:
        return None
    act = float(rate_res["applied_pct"])
    lr, basis, fy_switch = fork_market_lr(market, fs)
    mk = fork_market(act, market.get("rate_pct"), lr, fs)
    mk.update(loss_ratio_pct=lr, market_rate_pct=market.get("rate_pct"), basis=basis, act_pct=act,
              loss_ratio_last_pct=market.get("loss_ratio_pct"),
              loss_ratio_full_year_pct=market.get("loss_ratio_full_year_pct"),
              full_year_period=market.get("full_year_period"), full_year_switch=fy_switch,
              cap_at_market=bool(fs["market"].get("cap_at_market", True)), capped=False, effective_pct=mk["pct"])
    rate, floored = fork_rate(act, region["pct"], mk["pct"], rate_res.get("min_pct"))
    mrate = market.get("rate_pct")
    if mk["cap_at_market"] and mk["reason"] == "applied" and mrate is not None and rate > float(mrate) + 1e-12:
        # потолок: надбавка рынка поднимает ставку не выше рыночной; поправка региона и минимум — как есть
        after_reg = round(act * (1 + float(region["pct"]) / 100), 4)
        rate, floored = fork_rate(max(after_reg, float(mrate)), 0, 0, rate_res.get("min_pct"))
        mk.update(capped=True, effective_pct=round((rate / after_reg - 1) * 100, 2) if after_reg else 0.0)
    return {"act_pct": act, "act_premium": rate_res.get("premium"), "region": region, "market": mk,
            "adjusted_pct": rate, "floored": floored, "mode": fs["mode"], "calibrated": CALIBRATED}


def apply_fork(rate_res: dict, adj: Optional[dict], sum_insured: float) -> dict:
    """
    Режим apply: ставка п. 3 становится ставкой акта — applied_pct, премия и строки «как посчитано» (обе поправки).
    rate_res меняется на месте; прежняя ставка и премия — fork_act_pct, fork_act_premium (отметка «ставка акта»).
    """
    if not adj or adj.get("mode") != "apply":
        return rate_res
    term = int(rate_res.get("term_days") or 365)
    rate_res["fork_act_pct"] = adj["act_pct"]
    rate_res["fork_act_premium"] = rate_res.get("premium")
    rate_res["fork_applied"] = True
    how = [h for h in rate_res["how"] if h["code"] != "how_premium"]
    after_reg = round(adj["act_pct"] * (1 + adj["region"]["pct"] / 100), 4)
    how.append({"code": "how_fork_region", "params": {"base": adj["act_pct"], "spct": adj["region"]["pct"],
                                                      "rate": after_reg}})
    how.append({"code": "how_fork_market", "params": {"base": after_reg,
                                                      "spct": adj["market"].get("effective_pct", adj["market"]["pct"]),
                                                      "rate": adj["adjusted_pct"]}})
    if adj["market"].get("capped"):
        how.append({"code": "how_fork_market_cap", "params": {"market": adj["market"].get("market_rate_pct")}})
    if adj.get("floored"):
        how.append({"code": "how_fork_min", "params": {"min": rate_res.get("min_pct")}})
    rate_res["applied_pct"] = adj["adjusted_pct"]
    rate_res["premium"] = round(premium_of(rate_res["applied_pct"], sum_insured, term))
    how.append({"code": "how_premium", "params": {"sum": sum_insured, "rate": rate_res["applied_pct"],
                                                  "days": term, "premium": rate_res["premium"]}})
    rate_res["how"] = how
    return rate_res


def _mark(code, rate, S, term, source, note=None, **params) -> dict:
    prem = round(premium_of(float(rate), S, term)) if rate is not None and S else None
    return {"code": code, "rate_pct": None if rate is None else round(float(rate), 4), "premium": prem,
            "is_recommended": False, "source": source, "note": note, "params": params}


def fork_build(*, rate_res: dict, adj: Optional[dict], sum_insured: float, market: dict, sources: dict,
               request_pct: Optional[float] = None, contract_pct: Optional[float] = None,
               technical_pct: Optional[float] = None, premium_final: Optional[float] = None,
               franchise_applied: bool = False, mode: str = "reference", factors: Optional[dict] = None) -> dict:
    """
    Вилка ставки одного класса (коды и числа). sources — источники из базы (act_analytics.fork_data):
    policy, policy_act, adjusted, market, request, contract, technical, statutory. Отметки по шкале: min, act,
    adjusted, market, затем ставки документов (request, contract) и справочная техническая (technical).
    factors — factor_adjust с effect (02.10.2026): в режиме reference и при заполненных факторах — последняя отметка
    factors «с учётом факторов объекта — справочно» = ставка акта × множитель, не ниже минимума (factor_mark).
    """
    S = float(sum_insured or 0)
    term = int(rate_res.get("term_days") or 365)
    rm = rate_res.get("mode")
    mrate = market.get("rate_pct")
    minp = rate_res.get("min_pct")
    out = {"available": False, "reason": None, "mode": mode, "unit": "% годовых", "marks": [],
           "adjustments": None, "recommended": None, "position": {"request": "none", "contract": "none"},
           "term_days": term, "sum_insured": S, "franchise_applied": bool(franchise_applied),
           "premium_final": premium_final, "calibrated": CALIBRATED}
    market_mark = None
    if mrate is not None:
        below = minp is not None and rm == "tariff" and mrate + 1e-9 < float(minp)
        market_mark = _mark("market", mrate, S, term, sources.get("market"), "market_below_min" if below else "market")
    if rm in ("statutory", "statutory_undefined"):
        out["reason"] = rm
        if rate_res.get("applied_pct") is not None:
            m = _mark("act", rate_res["applied_pct"], S, term, sources.get("statutory"), "statutory")
            m["premium"] = rate_res.get("premium")
            m["is_recommended"] = True
            out["marks"] = [m]
            out["recommended"] = {"code": "act", "rate_pct": m["rate_pct"], "premium": m["premium"]}
        return out
    if rm != "tariff" or adj is None:
        out["reason"] = "undefined"
        if market_mark:
            out["marks"] = [market_mark]
        return out
    applied = bool(rate_res.get("fork_applied"))
    act_prem = rate_res.get("fork_act_premium") if applied else rate_res.get("premium")
    marks = []
    if minp is not None:
        marks.append(_mark("min", minp, S, term, sources.get("policy"), "min"))
    a = _mark("act", adj["act_pct"], S, term, sources.get("policy_act"), "act", level=rate_res.get("adj_pct"))
    if act_prem is not None:
        a["premium"] = act_prem
    marks.append(a)
    ad = _mark("adjusted", adj["adjusted_pct"], S, term, sources.get("adjusted"),
               "adjusted_min" if adj.get("floored") else "adjusted",
               region=adj["region"]["pct"], market=adj["market"].get("effective_pct", adj["market"]["pct"]),
               capped=bool(adj["market"].get("capped")))
    if applied:
        ad["premium"] = rate_res.get("premium")
    marks.append(ad)
    if market_mark:
        marks.append(market_mark)
    for code, val in (("request", request_pct), ("contract", contract_pct)):
        if val is not None:
            pos = fork_position(val, minp, mrate)
            out["position"][code] = pos
            marks.append(_mark(code, val, S, term, sources.get(code), "pos_" + pos))
    if technical_pct is not None:
        marks.append(_mark("technical", technical_pct, S, term, sources.get("technical"), "technical"))
    fm = factor_mark(adj["act_pct"], factors, minp)
    if fm is not None:
        marks.append(_mark("factors", fm[0], S, term, {"kind": "factors"}, "factors_min" if fm[1] else "factors",
                           mult=(factors or {}).get("product")))
    rec_code = "adjusted" if applied else "act"
    for m in marks:
        m["is_recommended"] = m["code"] == rec_code
    rec = next(m for m in marks if m["code"] == rec_code)
    out.update(available=True, marks=marks,
               adjustments={"region": adj["region"], "market": adj["market"], "floored": bool(adj.get("floored"))},
               recommended={"code": rec_code, "rate_pct": rec["rate_pct"], "premium": rec["premium"]})
    return out


def fork_contract(parts: list, sum_insured: float, term_days: int, request_pct: Optional[float] = None,
                  contract_pct: Optional[float] = None, mode: str = "reference") -> dict:
    """
    Справочная вилка договора из частей: по каждой отметке min, act, adjusted — сумма премий частей и ставка
    договора = сумма премий / страховая сумма × 365 / срок (как справочная ставка договора; минимум по ней не
    проверяется — правило проекта № 5). Нет отметки у части — нет отметки договора. Рынка у договора нет
    (рынок — по классу каждой части). parts — [{"rate_fork": …}].
    """
    S = float(sum_insured or 0)
    term = int(term_days or 365)
    out = {"available": False, "reason": "parts_reference", "reference_only": True, "mode": mode,
           "unit": "% годовых", "marks": [], "adjustments": None, "recommended": None,
           "position": {"request": "none", "contract": "none"}, "term_days": term, "sum_insured": S,
           "calibrated": CALIBRATED}
    forks = [p.get("rate_fork") or {} for p in parts]
    if not forks or not S:
        return out
    marks = []
    for code in ("min", "act", "adjusted"):
        prems = []
        for f in forks:
            m = next((x for x in f.get("marks") or [] if x["code"] == code), None)
            if m is None and f.get("reason") == "statutory":
                # обязательная часть: одна ставка по нормативному акту — та же во всех отметках
                m = next((x for x in f.get("marks") or [] if x["code"] == "act"), None)
            prems.append(m.get("premium") if m else None)
        if any(x is None for x in prems):
            continue
        total = round(sum(prems))
        marks.append({"code": code, "rate_pct": round(total / S * 100 * 365 / term, 4), "premium": total,
                      "is_recommended": False, "source": {"kind": "parts"}, "note": "parts_" + code, "params": {}})
    for code, val in (("request", request_pct), ("contract", contract_pct)):
        if val is not None:
            marks.append({"code": code, "rate_pct": round(float(val), 4),
                          "premium": round(premium_of(float(val), S, term)), "is_recommended": False,
                          "source": {"kind": code}, "note": "parts_doc", "params": {}})
    rec_code = "adjusted" if mode == "apply" else "act"
    for m in marks:
        m["is_recommended"] = m["code"] == rec_code
    rec = next((m for m in marks if m["code"] == rec_code), None)
    out.update(marks=marks, available=bool(rec),
               recommended={"code": rec_code, "rate_pct": rec["rate_pct"], "premium": rec["premium"]} if rec else None)
    return out


# ================================================================================================
#  10. Оценка заниженной ставки (01.10.2026): «можно ли застраховать по ставке ниже минимальной — да / нет, почему»
# ================================================================================================

BM_VERDICTS = ("allowed", "allowed_with_conditions", "not_allowed")
BM_CONDITIONS = ("cond_franchise", "cond_measures", "cond_territory", "cond_reinspect", "cond_docs_confirm",
                 "cond_uw_authority")


def _bm_reason(code: str, sign: str, value=None, effect: str = "none", **params) -> dict:
    """Довод оценки: sign — for | against; effect — что довод делает с ответом: blocks («нет» без обсуждения),
    no_conditions (нельзя и «при условиях» — «нет»), no_yes (не выше «да при условиях»), none (справочно)."""
    return {"code": code, "sign": sign, "value": value, "effect": effect, "params": params}


def below_min_assess(*, requested_pct: Optional[float], requested_source: Optional[str], min_pct: Optional[float],
                     rate_type: str = "annual", term_days: int = 365, sum_insured: float = 0,
                     statutory: bool = False, level: Optional[str] = None, losses_count: Optional[int] = None,
                     documents: Optional[bool] = None, object_new: Optional[bool] = None,
                     net_pct: Optional[float] = None, net_calibrated: bool = False,
                     retention_within: Optional[bool] = None, eml: Optional[float] = None,
                     retention_limit: Optional[float] = None, market_rate_pct: Optional[float] = None,
                     loss_ratio_pct: Optional[float] = None, settings: Optional[dict] = None) -> dict:
    """
    Можно ли застраховать объект по запрошенной ставке ниже минимальной ставки страховщика (коды и числа; слова —
    app/act_texts.py). Правило разработчика, не утверждено страховщиком; окончательное решение — андеррайтер.

    «нет» без обсуждения (effect = blocks): обязательный вид (тариф нормативного акта); уровень риска высокий;
    убытков за 3 года ≥ losses_block; запрошенная ставка (годовая) ниже нетто-ставки расчётного модуля — если
    нетто-ставка известна и калибрована (net_check = always — и экспертная); EML выше лимита удержания; документов
    на объект нет.
    «нет» (effect = no_conditions): запрошенная ставка ниже min_share_pct % минимальной; был убыток (меньше losses_block).
    «да при условиях»: уровень не выше умеренного, убытков нет, ставка не ниже min_share_pct % минимальной и не ниже
    нетто-ставки; условия — франшиза, мероприятия, ограничение территории, повторный осмотр, подтверждение документов,
    решение андеррайтера с полномочиями по отступлению (+ справка об убытках, подтверждение нетто-ставки — если их нет).
    «да»: дополнительно уровень низкий, объект новый с документами, убытков нет (известно), ставка покрывает нетто-ставку
    (известно) и рыночная ставка класса не выше запрошенной более чем в market_max_ratio раз или убыточность класса
    ниже low_loss_ratio_pct %. Условие — решение андеррайтера с полномочиями по отступлению.
    Ставки сравниваются в одном типе: фиксированная (на весь срок) пересчитывается в годовую (× 365 / дни) для
    сравнения с рынком и нетто-ставкой. Недобор премии за срок = (минимум − запрошенная) × сумма (× дни / 365 у годовой).
    Запрошенная ставка не ниже минимальной (или чего-то нет) — available = false: оценки нет, сверка как раньше.
    """
    st = merge_settings(settings).get("below_min") or DEFAULT_SETTINGS["below_min"]
    out = {"available": False, "reason": None, "requested_pct": requested_pct, "requested_source": requested_source,
           "min_pct": min_pct, "rate_type": rate_type, "calibrated": CALIBRATED,
           "rule": {k: st.get(k) for k in ("min_share_pct", "market_max_ratio", "low_loss_ratio_pct", "losses_block",
                                           "net_check")}}
    if requested_pct is None:
        out["reason"] = "no_requested"
        return out
    if min_pct is None:
        out["reason"] = "no_min"
        return out
    req, mn = float(requested_pct), float(min_pct)
    if req + 1e-9 >= mn:
        out["reason"] = "not_below"
        return out
    term = int(term_days or 365)
    S = float(sum_insured or 0)
    gap = round(mn - req, 6)
    gap_rel = round(gap / mn * 100, 2) if mn else None
    share = round(req / mn * 100, 2) if mn else None
    req_y = annual_pct(req, term, rate_type)
    shortfall = round(premium_by_type(gap, S, term, rate_type))
    reasons, conds = [], []

    # 1. обязательный вид
    if statutory:
        reasons.append(_bm_reason("bm_statutory", "against", True, "blocks"))
    # 2. уровень риска
    if level == "high":
        reasons.append(_bm_reason("bm_level", "against", level, "blocks", level=level))
    elif level == "moderate":
        reasons.append(_bm_reason("bm_level", "against", level, "no_yes", level=level))
    elif level == "low":
        reasons.append(_bm_reason("bm_level", "for", level, "none", level=level))
    # 3. убытки за 3 года
    block = int(st.get("losses_block", 2))
    if losses_count is None:
        reasons.append(_bm_reason("bm_losses_unknown", "against", None, "no_yes"))
        conds.append("cond_losses_cert")
    elif losses_count >= block:
        reasons.append(_bm_reason("bm_losses", "against", losses_count, "blocks", n=losses_count, block=block))
    elif losses_count > 0:
        reasons.append(_bm_reason("bm_losses_some", "against", losses_count, "no_conditions", n=losses_count))
    else:
        reasons.append(_bm_reason("bm_losses_none", "for", 0, "none"))
    # 4. нетто-ставка расчётного модуля: покрывает ли запрошенная ставка ожидаемый убыток
    if net_pct is None:
        reasons.append(_bm_reason("bm_net_unknown", "against", None, "no_yes"))
        conds.append("cond_net_confirm")
    elif req_y + 1e-9 >= float(net_pct):
        reasons.append(_bm_reason("bm_net_ok", "for", round(float(net_pct), 4), "none", req=round(req_y, 4),
                                  net=round(float(net_pct), 4), calibrated=bool(net_calibrated)))
    elif net_calibrated or st.get("net_check") == "always":
        reasons.append(_bm_reason("bm_net_below", "against", round(float(net_pct), 4), "blocks", req=round(req_y, 4),
                                  net=round(float(net_pct), 4), calibrated=bool(net_calibrated)))
    else:
        reasons.append(_bm_reason("bm_net_below_expert", "against", round(float(net_pct), 4), "no_yes",
                                  req=round(req_y, 4), net=round(float(net_pct), 4), calibrated=False))
        conds.append("cond_net_confirm")
    # 5. удержание: EML против лимита на один риск (Положение 1806, п. 15)
    if retention_within is False:
        reasons.append(_bm_reason("bm_retention_over", "against", eml, "blocks", eml=eml, limit=retention_limit))
    elif retention_within is True:
        reasons.append(_bm_reason("bm_retention_ok", "for", eml, "none", eml=eml, limit=retention_limit))
    else:
        reasons.append(_bm_reason("bm_retention_unknown", "against", None, "none"))
    # 6. документы на объект
    if documents:
        reasons.append(_bm_reason("bm_docs", "for", True, "none"))
    else:
        reasons.append(_bm_reason("bm_no_docs", "against", False, "blocks"))
    # 7. насколько ставка ниже минимума
    lim_share = float(st.get("min_share_pct", 60))
    if share is not None and share + 1e-9 >= lim_share:
        reasons.append(_bm_reason("bm_share_ok", "for", share, "none", share=share, lim=lim_share))
    else:
        reasons.append(_bm_reason("bm_share_low", "against", share, "no_conditions", share=share, lim=lim_share))
    # 8. объект новый
    if object_new and documents:
        reasons.append(_bm_reason("bm_new", "for", True, "none"))
    else:
        reasons.append(_bm_reason("bm_not_new", "against", object_new, "no_yes"))
    # 9. рынок класса (НАПП): ставка и убыточность
    ratio_max = float(st.get("market_max_ratio", 2))
    lr_low = float(st.get("low_loss_ratio_pct", 40))
    market_ok = lr_ok = None
    if market_rate_pct is not None and req_y:
        ratio = round(float(market_rate_pct) / req_y, 2)
        market_ok = ratio <= ratio_max + 1e-9
        reasons.append(_bm_reason("bm_market_ok" if market_ok else "bm_market_high", "for" if market_ok else "against",
                                  ratio, "none", market=market_rate_pct, req=round(req_y, 4), ratio=ratio,
                                  lim=ratio_max))
    if loss_ratio_pct is not None:
        lr_ok = float(loss_ratio_pct) < lr_low
        reasons.append(_bm_reason("bm_lr_low" if lr_ok else "bm_lr_high", "for" if lr_ok else "against",
                                  round(float(loss_ratio_pct), 2), "none", lr=round(float(loss_ratio_pct), 2),
                                  lim=lr_low))
    if not (market_ok or lr_ok):
        reasons.append(_bm_reason("bm_market_unknown" if market_ok is None and lr_ok is None else "bm_market_no",
                                  "against", None, "no_yes", lim=ratio_max, lr_lim=lr_low))
    # 10. разрыв до минимума и недобор премии — всегда «против», справочно
    reasons.append(_bm_reason("bm_gap", "against", gap, "none", gap=gap, gap_rel=gap_rel, shortfall=shortfall,
                              days=term, rate_type=rate_type))

    effects = {r["effect"] for r in reasons}
    if "blocks" in effects or "no_conditions" in effects:
        verdict = "not_allowed"
    elif "no_yes" in effects:
        verdict = "allowed_with_conditions"
    else:
        verdict = "allowed"
    if verdict == "allowed_with_conditions":
        conditions = list(BM_CONDITIONS[:-1]) + [c for c in conds if c not in BM_CONDITIONS] + ["cond_uw_authority"]
    elif verdict == "allowed":
        conditions = ["cond_uw_authority"]
    else:
        conditions = []
    out.update(available=True, reason=None, gap_pct=gap, gap_rel_pct=gap_rel, share_pct=share,
               requested_annual_pct=round(req_y, 4) if rate_type == "fixed" else None,
               shortfall=shortfall, term_days=term, sum_insured=S, verdict=verdict,
               hard="blocks" in effects, reasons=reasons, conditions=conditions)
    return out



# ================================================================================================
#  13. Факторы объекта по подгруппам класса (02.10.2026)
# ================================================================================================
#
# Документ «Факторы тарифа по классам и подгруппам» (02.10.2026): у каждого класса — подгруппы объектов и факторы,
# которые повышают (+) или понижают (−) тариф. В шаблоне класса (class_templates, файл 1.4.0) — factor_groups:
# группа {code, label, input, options: [{code, label, coef, note}]}; значение группы — поле optional шаблона
# (optional.class_fields.<код> или уточнение акта optional.location / protection / construction). Коэффициенты
# экспертные (calibrated = 0), утверждает актуарий страховщика.
#   * множитель = произведение коэффициентов заполненных групп, в границах настроек factors.min_product …
#     max_product; незаполненная группа ставку не меняет и уходит в «уточнить»;
#   * reference (по умолчанию) — ставка акта прежняя, в вилке отметка factors = ставка акта × множитель, не ниже
#     минимума; apply — ставка акта = тариф × поправка уровня × множитель, не ниже минимальной; премия от неё.
# Пример (класс 3, автокран 0318, 2 945 000 000 сум, 365 дней, ставка акта 0,42 % = 0,35 % × 1,2): дизель 1,05 ×
# такси и аренда 1,4 × открытая площадка 1,15 = 1,6905 → 0,42 % × 1,6905 = 0,71 % (0,71001 → 0,71);
# премия 12 369 000 → 20 909 500 сум (справочно; в режиме apply — премия акта).

FACTOR_MODES = ("reference", "apply")
FACTOR_PRODUCT_LIMITS = (0.1, 10.0)       # пределы самих границ множителя в настройке (защита от опечатки)


def factor_settings(settings: Optional[dict]) -> dict:
    """Настройки факторов объекта поверх умолчаний: {mode, min_product, max_product, calibrated}."""
    d = DEFAULT_SETTINGS["factors"]
    raw = (settings or {}).get("factors") if isinstance((settings or {}).get("factors"), dict) else {}
    out = {**d, **raw}
    out["calibrated"] = CALIBRATED
    return out


def check_factor_settings(fs) -> list:
    """Настройка factors: mode — reference | apply; 0,1 ≤ min_product ≤ 1 ≤ max_product ≤ 10; calibrated — 0."""
    if not isinstance(fs, dict):
        return ["factors: словарь {mode, min_product, max_product}"]
    errs = []
    extra = [k for k in fs if k not in ("mode", "min_product", "max_product", "calibrated")]
    if extra:
        errs.append("factors: неизвестные ключи " + ", ".join(extra))
    if fs.get("mode") not in FACTOR_MODES:
        errs.append("factors.mode: reference или apply")
    lo_lim, hi_lim = FACTOR_PRODUCT_LIMITS
    lo, hi = fs.get("min_product"), fs.get("max_product")
    if isinstance(lo, bool) or not isinstance(lo, (int, float)) or not lo_lim <= lo <= 1:
        errs.append("factors.min_product: число от " + f"{lo_lim:g}".replace(".", ",") + " до 1")
    if isinstance(hi, bool) or not isinstance(hi, (int, float)) or not 1 <= hi <= hi_lim:
        errs.append("factors.max_product: число от 1 до " + f"{hi_lim:g}".replace(".", ","))
    if "calibrated" in fs and fs["calibrated"] != 0:
        errs.append("factors.calibrated: 0 — коэффициенты экспертные, не калиброваны")
    return errs


def _round4(x: float) -> float:
    """Множитель до 4 знаков по правилу «половина — вверх» (0,87975 → 0,8798, а не 0,8797 из-за двоичной записи)."""
    return round_half_up(float(x) * 10000 + 1e-9) / 10000


def _factor_value(group: dict, class_fields: dict, refinements: dict) -> Optional[str]:
    """Значение группы из ввода: optional.class_fields.<код> — поля класса, optional.<код> — уточнения акта;
    да/нет → yes / no (варианты группы для поля «да/нет»)."""
    inp = str(group.get("input") or "")
    if inp.startswith("optional.class_fields."):
        v = class_fields.get(inp[len("optional.class_fields."):])
    elif inp.startswith("optional."):
        v = refinements.get(inp[len("optional."):])
    else:
        v = None
    if isinstance(v, bool):
        return "yes" if v else "no"
    return None if v in (None, "") else str(v)


def factor_adjust(class_fields: Optional[dict], template: Optional[dict], settings: Optional[dict] = None,
                  refinements: Optional[dict] = None) -> dict:
    """
    Факторы объекта по группам шаблона класса (factor_groups). class_fields — поля класса акта
    (optional.class_fields), refinements — уточнения акта (optional: location, protection, construction …).
    Возвращает {"mode", "product", "raw_product", "clamped", "bounds", "applied": [{group, group_label, option,
    label, coef, note}], "unfilled": [{group, label}], "groups", "calibrated": 0}. Подписи — как в шаблоне
    ({ru, uz, en}), слова на языке акта собирает app/act.py.
    Пример: дизель 1,05 × такси 1,4 × улица 1,15 = 1,6905; электро 1,15 × гараж 0,85 × личное 0,9 = 0,8798.
    """
    fs = factor_settings(settings)
    cf, rf = dict(class_fields or {}), dict(refinements or {})
    groups = [g for g in (template or {}).get("factor_groups") or [] if isinstance(g, dict) and g.get("code")]
    applied, unfilled = [], []
    raw = 1.0
    for g in groups:
        v = _factor_value(g, cf, rf)
        opt = next((o for o in g.get("options") or [] if isinstance(o, dict) and o.get("code") == v), None) \
            if v is not None else None
        if opt is None:
            unfilled.append({"group": g["code"], "label": g.get("label")})
            continue
        coef = float(opt["coef"])
        raw *= coef
        item = {"group": g["code"], "group_label": g.get("label"), "option": opt["code"],
                "label": opt.get("label"), "coef": coef, "note": opt.get("note")}
        refs = option_stat_refs(g, opt["code"])
        if refs:
            item["stat_ref"] = refs              # фон региона (stat.uz) — act_analytics.factor_stats, коэффициент не меняет
        applied.append(item)
    lo, hi = float(fs["min_product"]), float(fs["max_product"])
    prod = min(hi, max(lo, raw))
    return {"mode": fs["mode"], "product": _round4(prod), "raw_product": _round4(raw),
            "clamped": "max" if raw > hi + 1e-12 else ("min" if raw < lo - 1e-12 else None), "bounds": [lo, hi],
            "applied": applied, "unfilled": unfilled, "groups": len(groups), "calibrated": CALIBRATED}


# --------------------------------------------------------------------------------------------------------------
# Фон региона к фактору объекта (stat_ref группы, шаблоны 1.4.1 от 02.10.2026): открытые наборы stat.uz
# --------------------------------------------------------------------------------------------------------------
# Пять наборов «Распределение жилищного фонда по материалу стен» (тыс. кв. м на конец года) вместе дают весь фонд
# региона (проверено 02.10.2026: сумма пяти по Ташкентской области за 2025 год = 72 439,5 тыс. кв. м = набор 1244).
# Доля материала = сумма наборов варианта / сумма пяти за тот же год. Набор вне этого списка (обеспеченность газом,
# 1243) — уже процент, показывается как есть.
WALL_FUND = ("housing_fund_by_walls_brick", "housing_walls_raw_brick", "housing_walls_panel_rc",
             "housing_walls_other", "housing_walls_adobe")
STAT_NOTE = "фон региона, коэффициент не меняет"


def option_stat_refs(group: dict, option: str) -> list:
    """Ссылки группы на наборы stat.uz (stat_ref), к которым относится вариант option: [{dataset_id, label}]."""
    out = []
    for r in group.get("stat_ref") or [] if isinstance(group.get("stat_ref"), list) else []:
        if isinstance(r, dict) and r.get("dataset_id") and option in (r.get("option_codes") or []):
            out.append({"dataset_id": r["dataset_id"], "label": r.get("label")})
    return out


def factor_stat(refs: list, series: dict, region: Optional[str], region_name: Optional[str] = None,
                datasets: Optional[dict] = None) -> dict:
    """
    Блок stat применённого фактора — фон региона по открытым данным stat.uz. Чистая функция: ряды уже прочитаны
    (act_analytics.factor_stats читает таблицу stat_series базы, в сеть не ходит).
      refs     — ссылки варианта [{dataset_id, label}] (option_stat_refs);
      series   — {dataset_id: {год: {value, unit, url, fetched_at}}} по региону;
      region   — ключ region:* (или total); datasets — реестр stat_sources.DATASETS (имя, страница, адрес данных).
    Стены: доля = сумма наборов варианта / сумма пяти наборов WALL_FUND за последний год, где есть все пять.
    Иной набор (газ): последний год, share_pct = сам показатель (%).
    Нет данных — {"available": False, "reason": код, "reason_text": по-русски}. Коэффициент фактора не меняется.
    """
    ds_reg = datasets or {}
    ids = [r["dataset_id"] for r in refs or [] if r.get("dataset_id")]
    base = {"available": False, "reason": None, "reason_text": None, "dataset": ids, "region": region,
            "region_name": region_name, "note": STAT_NOTE, "calibrated": CALIBRATED}
    if not ids:
        return dict(base, reason="no_ref", reason_text="у варианта нет ссылки на наборы stat.uz")
    names = [(ds_reg.get(i) or {}).get("name") or i for i in ids]
    meta = {"name": "; ".join(names), "source": "stat.uz",
            "source_ids": [(ds_reg.get(i) or {}).get("src_id") for i in ids],
            "data_url": [(ds_reg.get(i) or {}).get("data_url") for i in ids],
            "labels": [r.get("label") for r in refs if r.get("dataset_id")],
            "limits": sorted({x for i in ids for x in (ds_reg.get(i) or {}).get("limits") or []})}
    base.update(meta)
    base["url"] = (ds_reg.get(ids[0]) or {}).get("page")
    if not region:
        return dict(base, reason="region_unknown", reason_text="регион акта не опознан — фон региона не показан")
    walls = all(i in WALL_FUND for i in ids)
    if walls:
        need = list(WALL_FUND)
        years = set.intersection(*[set((series.get(d) or {})) for d in need])
        if not years:
            miss = [d for d in need if not series.get(d)]
            return dict(base, reason="no_data", reason_text="в базе (stat_series) нет общего года по пяти наборам "
                        "материала стен для региона" + (": нет " + ", ".join(miss) if miss else ""))
        per = max(years)
        total = sum(float(series[d][per]["value"]) for d in need)
        value = sum(float(series[d][per]["value"]) for d in dict.fromkeys(ids))
        if total <= 0:
            return dict(base, reason="no_data", reason_text="сумма жилищного фонда региона равна нулю")
        rec = series[ids[0]][per]
        return dict(base, available=True, kind="walls_share", period=per, value=round(value, 1),
                    total=round(total, 1), share_pct=round(value / total * 100, 3), unit=rec.get("unit"),
                    url=rec.get("url") or base["url"], fetched_at=max(str(series[d][per].get("fetched_at") or "")
                                                                    for d in need) or None,
                    formula="доля = сумма наборов варианта / сумма пяти наборов материала стен × 100")
    if len(ids) != 1:
        return dict(base, reason="mixed", reason_text="в варианте смешаны наборы стен и иные — доля не считается")
    ser = series.get(ids[0]) or {}
    if not ser:
        return dict(base, reason="no_data", reason_text="в базе (stat_series) нет набора для региона — "
                                                        "ежедневное обновление stat.uz ещё не загружало его")
    per = max(ser)
    rec = ser[per]
    return dict(base, available=True, kind="value", period=per, value=float(rec["value"]), total=None,
                share_pct=round(float(rec["value"]), 3), unit=rec.get("unit"), url=rec.get("url") or base["url"],
                fetched_at=rec.get("fetched_at"), formula="показатель источника как есть")


def factor_mark(act_pct: Optional[float], fa: Optional[dict], min_pct: Optional[float]) -> Optional[tuple]:
    """Отметка вилки «с учётом факторов объекта» (режим reference): (ставка акта × множитель, округлена до 4 знаков,
    не ниже минимума; поднята ли до минимума). None — режим apply, факторы не заполнены или ставки нет."""
    if not fa or fa.get("mode") != "reference" or not fa.get("applied") or act_pct is None:
        return None
    if (fa.get("effect") or {}).get("available") is False:
        return None
    r = round(float(act_pct) * float(fa["product"]), 4)
    if min_pct is not None and r + 1e-12 < float(min_pct):
        return round(float(min_pct), 4), True
    return r, False


def factor_effect(fa: dict, rate_res: dict, sum_insured: float) -> dict:
    """
    Как факторы объекта меняют ставку и премию (fa["effect"]) и, в режиме apply, ставку акта (rate_res — на месте).
    База: reference — ставка акта (applied_pct); apply — тариф × поправка уровня (calc_pct) до минимума. Шаги —
    по каждому фактору: ставка после него и вклад в премию в сумах (премии считаются от ставок, округлённых до
    4 знаков, — сумма вкладов равна разнице премий). Границы множителя и минимальная ставка — отдельные шаги.
    reason (effect недоступен): statutory — обязательный вид, ставка по нормативному акту без поправок; no_rate —
    ставки нет (по программе, по согласованию); none_filled — ни одна группа не заполнена.
    """
    eff = {"available": False, "reason": None, "applied_to_act": False, "calibrated": CALIBRATED}
    fa["effect"] = eff
    mode = rate_res.get("mode")
    if mode in ("statutory", "statutory_undefined"):
        eff["reason"] = "statutory"
        return fa
    if mode != "tariff" or rate_res.get("applied_pct") is None:
        eff["reason"] = "no_rate"
        return fa
    if not fa.get("applied"):
        eff["reason"] = "none_filled"
        return fa
    S = float(sum_insured or 0)
    term = int(rate_res.get("term_days") or 365)
    rt = rate_res.get("rate_type") or "annual"
    minp = rate_res.get("min_pct")

    def prem(x):
        return round(premium_by_type(round(float(x), 4), S, term, rt))

    apply = fa.get("mode") == "apply"
    base = float(rate_res["calc_pct"] if apply and rate_res.get("calc_pct") is not None else rate_res["applied_pct"])
    p0 = prem(base)
    steps, r, prev = [], base, p0
    for a in fa["applied"]:
        r *= a["coef"]
        p = prem(r)
        steps.append({"kind": "factor", "group": a["group"], "option": a["option"], "coef": a["coef"],
                      "rate_pct": round(r, 4), "premium": p, "premium_delta": p - prev})
        prev = p
    rate = round(base * float(fa["product"]), 4)
    if fa.get("clamped"):
        p = prem(rate)
        steps.append({"kind": "bound", "group": None, "option": fa["clamped"], "coef": fa["product"],
                      "rate_pct": rate, "premium": p, "premium_delta": p - prev})
        prev = p
    floored = False
    if minp is not None and rate + 1e-12 < float(minp):
        rate, floored = round(float(minp), 4), True
        p = prem(rate)
        steps.append({"kind": "min", "group": None, "option": None, "coef": None, "rate_pct": rate, "premium": p,
                      "premium_delta": p - prev})
        prev = p
    premium = prem(rate)
    eff.update(available=True, reason=None, base="calc" if apply else "act", base_pct=round(base, 4),
               base_premium=p0, rate_pct=rate, premium=premium, delta_premium=premium - p0, floored=floored,
               min_pct=minp, rate_type=rt, term_days=term, sum_insured=S, steps=steps)
    if apply:
        rate_res["factor_act_pct"] = rate_res["applied_pct"]
        rate_res["factor_act_premium"] = rate_res.get("premium")
        rate_res["factors_applied"] = True
        rate_res["factor_product"] = fa["product"]
        how = [h for h in rate_res["how"] if h["code"] not in ("how_premium", "how_premium_fixed", "how_min_applied", "how_min_ok",
                                                              "how_rate_annual_equiv")]
        how.append({"code": "how_factors", "params": {"calc": round(base, 4), "fmult": fa["product"],
                                                      "rate": round(base * float(fa["product"]), 4)}})
        if floored:
            how.append({"code": "how_factors_min", "params": {"min": minp}})
        rate_res["applied_pct"] = rate
        rate_res["min_applied"] = floored
        rate_res["premium"] = premium
        if rt == "fixed":
            rate_res["annual_equiv_pct"] = round(annual_pct(rate, term, rt), 4)
            how.append({"code": "how_premium_fixed", "params": {"sum": S, "rate": rate, "premium": premium}})
            how.append({"code": "how_rate_annual_equiv", "params": {"rate": rate, "days": term,
                                                                    "calc": rate_res["annual_equiv_pct"]}})
        else:
            how.append({"code": "how_premium", "params": {"sum": S, "rate": rate, "days": term, "premium": premium}})
        rate_res["how"] = how
        eff["applied_to_act"] = True
    return fa
