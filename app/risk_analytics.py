"""
Аналитика риска для вкладки «Аналитика» мини-аппа (задача заказчика от 21.09.2026).

Человек загружает обязательные (must) и дополнительные (optional) данные об объекте и получает
дашборд: краткий обзор (премия, страховая сумма, стоимость), техническую картину с рынком,
риски и их драйверы, три сценария максимального убытка (PML ≤ EML ≤ MFL — как в акте) и итоговый уровень риска.

Чистая модель, без HTTP: роутер пишет backend. Всё, что считает цену, берётся из движка
(app/engine.py: calculate / rate_for / min_rate); ёмкость и таблица линий — из app/capacity.py.
Модуль ничего не пишет в базу, кроме save_thresholds() (правка порогов админом).

Контракт:
    FIELDS                                  — описание формы: {"must":[...], "optional":[...]}
    validate(must, optional)                -> {"ok", "missing", "errors"}
    analyze(con, must, optional, thresholds=None, market=None)
                                            -> {"ok", "completeness", "summary", "risks", "top_drivers",
                                                "reducers", "scenarios", "retention", "level", "market",
                                                "method", ...}
    DEFAULT_THRESHOLDS, load_thresholds(con), save_thresholds(con, dict, user)
    Быстрый режим: QUICK_MUST, DEFAULTS_BY_CLASS, validate_quick(must, optional),
                   apply_defaults(con, must, optional, user=) -> (must, optional, assumptions),
                   quick_plan(con, class_code, product_code, user), presets(con),
                   load_last(con, user_id) / save_last(con, user_id, data)
    SCENARIO_PURPOSE                        — какой сценарий для чего (переключается одной правкой)

Все числа модели сценариев и шкалы уровня риска — экспертные (calibrated = 0): выгрузок договоров
и убытков компании пока нет. Нормы: ГК ст. 936, 938; Положение 1806 п. 15; Положение 1882 п. 11.
"""
import json
import math
from datetime import date
from typing import Optional

from . import capacity as cap
from . import db
from . import engine
from .engine import (Input, NEGOTIATED_MODES, calculate, factors_for, min_rate, premium_of,
                     pricing_mode_of, rate_for)

CALIBRATED = 0
EXPERT = "экспертно, calibrated=0"

# ================================================================================================
# Сценарии и их назначение. Названия совпадают с актом (задача 4.1 ТЗ 13.10.2026): PML ≤ EML ≤ MFL.
#   PML — вероятный максимальный убыток, защита сработала штатно (наименьший);
#   EML — оценочный максимальный убыток, защита сработала частично;
#   MFL — максимально предвидимый убыток, защита не сработала (катастрофа).
# До 13.10.2026 здесь было наоборот (EML — «защита сработала», PML — «частично»), и акт переставлял названия
# (act_extras.ACT_FROM_RA). Назначение — как сказал заказчик 21.09.2026: PML → стандартный тариф, EML → лимит
# удержания; теперь удержание считается от того же EML, что в акте (защита сработала частично).
# Переключение трактовки — одна правка этого словаря; формулы сценариев от назначения не зависят.
# ================================================================================================
SCENARIO_PURPOSE = {"PML": "tariff", "EML": "retention", "MFL": "catastrophe"}
PURPOSE_TITLE = {
    "tariff": "стандартный тариф",
    "retention": "лимит собственного удержания",
    "catastrophe": "катастрофический сценарий и перестрахование (оценочно)",
}
SCENARIO_TITLE = {
    "PML": "PML — вероятный максимальный убыток (защита сработала штатно)",
    "EML": "EML — оценочный максимальный убыток (защита сработала частично)",
    "MFL": "MFL — максимально предвидимый убыток (защита не сработала, катастрофа)",
}
TERMS_NOTE = (
    "Названия сценариев — как в акте: PML ≤ EML ≤ MFL (PML — защита сработала штатно, EML — частично, "
    "MFL — не сработала). Назначение — по заказчику: PML → стандартный тариф, EML → лимит собственного "
    "удержания. Заметка проекта «Максимальный убыток и собственное удержание» (континентальная трактовка) "
    "связывает их наоборот; в британской практике (учебник CII) EML — основная мера для удержания. "
    "Переключается одной правкой SCENARIO_PURPOSE."
)

LEVEL_NAMES = ["Низкий", "Умеренный", "Повышенный", "Высокий", "Критический"]

PROPERTY_CLASSES = {"8", "9"}
VEHICLE_CLASSES = {"3"}
# продукт по умолчанию, если агент указал только класс (минимальный тариф берётся по продукту)
DEFAULT_PRODUCT = {"8": "0807", "9": "0808", "3": "0308"}
DEFAULT_PRODUCT_SPECIAL = "0318"          # спецтехника: «Добровольное страхование спецтехники»

# ------------------------------------------------------------------------------------------------
# Экспертные таблицы сценариев (calibrated = 0). Источник логики — docs/Максимальный убыток и
# собственное удержание.md (отсеки, защита, сейсмика по всей площадке); сами доли — экспертные.
# ------------------------------------------------------------------------------------------------
FIRE_SHARE = {        # доля наибольшего противопожарного отсека, которая погибнет при пожаре
    "PML": {"sprinkler": 0.25, "alarm_guard": 0.40, "alarm": 0.50, "none": 0.70},
    "EML": {"sprinkler": 0.50, "alarm_guard": 0.70, "alarm": 0.80, "none": 1.00},
    "MFL": {"sprinkler": 1.00, "alarm_guard": 1.00, "alarm": 1.00, "none": 1.00},
}
FIRE_ADD = {"wood": 0.20, "flammable": 0.20, "far_station": 0.10}   # надбавки к доле (PML, EML)
EQ_SHARE = {          # доля стоимости всей площадки при землетрясении, железобетон/кирпич
    6: {"PML": 0.02, "EML": 0.05, "MFL": 0.10},
    7: {"PML": 0.05, "EML": 0.10, "MFL": 0.20},
    8: {"PML": 0.10, "EML": 0.20, "MFL": 0.40},
    9: {"PML": 0.20, "EML": 0.35, "MFL": 0.60},
    10: {"PML": 0.30, "EML": 0.50, "MFL": 0.80},
}
EQ_CONSTRUCTION = {"reinforced": 1.0, "mixed": 1.2, "wood": 1.1}
EQ_WEAR = {"new": 0.9, "mid": 1.0, "old": 1.25}
EQ_TALL_FLOORS, EQ_TALL_MULT = 5, 1.1     # от 5 этажей сейсмическая нагрузка выше
DAMAGE9_SHARE = {"PML": 0.10, "EML": 0.25, "MFL": 0.50}   # кража, вода, прочее — от наибольшего отсека
DAMAGE9_PROTECTION = {"none": 1.5, "alarm": 1.0, "alarm_guard": 0.8, "sprinkler": 0.8}
VEHICLE_SHARE = {"PML": 0.50, "EML_protected": 0.75, "EML_unprotected": 1.00, "MFL": 1.00}
BI_SHARE_MFL = 1.0

# какие риски по смыслу затрагивает фактор (для объяснения; движок применяет множитель ко всей
# нетто-ставке класса) — экспертная привязка
FACTOR_PERILS = {
    "construction": {"fire", "explosion", "earthquake", "storm", "hurricane", "collapse"},
    "activity": {"fire", "explosion"},
    "protection": {"fire", "explosion"},
    "seismic": {"earthquake"},
    "wear": {"fire", "collapse", "earthquake", "downpour"},
}

DEFAULT_THRESHOLDS = {
    "calibrated": 0,
    "source": "экспертная шкала до калибровки по убыткам компании",
    # веса составляющих итогового балла; неприменимые составляющие исключаются, веса нормируются
    "weights": {"rate": 0.25, "mfl_retention": 0.25, "losses": 0.20,
                "insurance_to_value": 0.10, "seismic": 0.20,
                # внешняя статистика региона (stat.uz, data.egov.uz; app/risk_stats.py) — задача 22.09.2026;
                # вес небольшой, нормируется вместе с остальными; нет данных по классу — не учитывается
                "external_stats": 0.10},
    # границы баллов: < 20 Низкий, < 40 Умеренный, < 60 Повышенный, < 80 Высокий, иначе Критический
    "level_bounds": [20, 40, 60, 80],
    "rate_ratio": [0.8, 2.0],          # техническая ставка / ориентир (минимум или рынок): 0 → 100 баллов
    "mfl_to_retention": [0.5, 3.0],    # MFL / лимит удержания
    "mfl_pct_of_sum": [20, 100],       # запасной вариант, если нет данных компании: MFL в % суммы
    "loss_count_points": [0, 50, 100],  # убытков за 3 года: 0 / 1 / 2 и более
    "loss_ratio": [0.3, 1.0],          # средний убыток в год / годовая премия
    "underinsurance_full_at": 0.5,     # сумма = 50% стоимости → 100 баллов
    "seismic_points": {"6": 0, "7": 20, "8": 60, "9": 100, "10": 100},
    "unknown_points": 50,              # нет данных по составляющей — средний балл
    "stop_min_level": "Высокий",       # есть «стоп» в проверках движка — уровень не ниже этого
    "scenario_bands_pct": [25, 50, 75, 90],
    "confidence_bands_pct": [34, 67],
    "wear_pct_bands": [15, 45],        # износ здания, %: до 15 → «до 10 лет», до 45 → «10–30», выше → «>30»
    "fire_station_far_km": 10,
    "neighbour_gap_m": 12,             # разрыв меньше 12 м — огонь переходит (пример CII)
    # «Внешняя статистика региона»: отношение показателя региона к республике (на 1 000 жителей или
    # коэффициент): 0,5 и ниже → 0 баллов, 1,5 и выше → 100, республиканский уровень (1,0) → 50
    "external_ratio": [0.5, 1.5],
    "external_volatility_years": 10,   # за сколько лет считается изменчивость сбора урожая (класс 16у)
    # --------------------------------------------------------------------------------------------
    # Франшиза по ситуации (app/franchise.py). Все числа экспертные (calibrated = 0), правятся
    # администратором через save_thresholds и хранятся в таблице risk_thresholds.
    # --------------------------------------------------------------------------------------------
    # вилка безусловной франшизы (% страховой суммы) по уровню риска
    "franchise_by_level": {"Низкий": [0, 1], "Умеренный": [1, 2], "Повышенный": [2, 5],
                           "Высокий": [5, 10], "Критический": [10, 90]},
    # множитель к нетто-ставке по размеру франшизы: 0/0,5/1/2% — из справочника коэффициентов
    # (class_factors → franchise), 5/10/90% — экспертное продолжение той же кривой
    "franchise_multipliers": {"0": 1.0, "0.5": 0.92, "1": 0.85, "2": 0.75,
                              "5": 0.60, "10": 0.45, "90": 0.15},
    "franchise_small_loss_pct": 1.0,      # «мелкий убыток» — меньше 1% страховой суммы
    "franchise_small_share": 0.5,         # мелких убытков больше половины → основание «поток мелких»
    "franchise_loss_count_high": 2,       # 2 и более убытков за 3 года → основание и верх вилки
    # доля одного риска в нетто-ставке, с которой он считается доминирующим: франшиза ставится
    # по этому риску (при неблагоприятном факторе), а не по всему договору (решение 27.09.2026)
    "franchise_dominant_share": 0.35,
    "franchise_class_caps": {"3": 5},     # потолок франшизы по классу (транспорт — 5%)
    "franchise_critical_peril_pct": 90,   # франшиза по одному «неприемлемому» риску
    "franchise_critical_seismic_zone": 9,  # зона, с которой землетрясение без сейсмостойкости критично
    "franchise_measures_note_days": 90,   # срок мероприятий по умолчанию, дней
}

THRESHOLDS_SQL = """
CREATE TABLE IF NOT EXISTS risk_thresholds (
    id              INTEGER PRIMARY KEY,
    created_at      TEXT NOT NULL,
    created_by      TEXT,
    thresholds_json TEXT NOT NULL,
    calibrated      INTEGER NOT NULL DEFAULT 0,
    note            TEXT
);
"""

LEGAL_REFS = [
    {"ref": "ГК РУз, ст. 936", "what": "неполное страхование: выплата в доле суммы к стоимости"},
    {"ref": "ГК РУз, ст. 938", "what": "страховая сумма выше стоимости: договор ничтожен в части превышения"},
    {"ref": "Положение № 1806, п. 15", "what": "обязательства по одному риску ≤ 20% (собственные средства + резервы)"},
    {"ref": "Положение № 1806, п. 16", "what": "пять крупнейших рисков ≤ 200% собственных средств"},
    {"ref": "Положение № 1882, п. 11", "what": "продукт из нескольких классов — несколько условных договоров"},
    {"ref": "Положение № 1882", "what": "страховые резервы, входящие в базу лимита на один риск"},
]
SOURCE_NOTES = [
    "docs/Максимальный убыток и собственное удержание.md",
    "docs/Резервы, ёмкость и собственное удержание.md",
    "docs/Фундаментальные знания из учебников CII.md",
    "app/engine.py (ставка), app/capacity.py (ёмкость, таблица линий)",
]

# ================================================================================================
# Описание формы
# ================================================================================================
PROT_8 = [{"code": "none", "label": "Нет сигнализации и охраны"},
          {"code": "alarm", "label": "Пожарная сигнализация"},
          {"code": "alarm_guard", "label": "Сигнализация и охрана"},
          {"code": "sprinkler", "label": "Сигнализация, охрана, спринклеры"}]
PROT_3 = [{"code": "none", "label": "Нет противоугонной системы"},
          {"code": "alarm", "label": "Сигнализация"},
          {"code": "immo", "label": "Сигнализация и иммобилайзер"},
          {"code": "tracker", "label": "Спутниковый поиск"}]

FIELDS = {
    "must": [
        {"key": "class_code", "label": "Класс страхования (8, 9, 8/9, 3 …)", "type": "text", "required": True},
        {"key": "product_code", "label": "Продукт (если известен)", "type": "text", "required": False},
        {"key": "object_type", "label": "Тип объекта (Склад, Производство, Легковой, Спецтехника — …)",
         "type": "text", "required": True},
        {"key": "sum_insured", "label": "Страховая сумма", "type": "number", "unit": "сум", "required": True},
        {"key": "object_value", "label": "Стоимость объекта", "type": "number", "unit": "сум", "required": True},
        {"key": "region", "label": "Регион", "type": "text", "required": True},
        {"key": "term_months", "label": "Срок страхования", "type": "number", "unit": "мес.", "required": True},
        {"key": "construction", "label": "Конструкция", "type": "select", "required": True, "when": ["8", "9"],
         "options": [{"code": "reinforced", "label": "Железобетон, кирпич"},
                     {"code": "mixed", "label": "Смешанные конструкции"},
                     {"code": "wood", "label": "Дерево, сэндвич-панели"}]},
        {"key": "activity", "label": "Деятельность на объекте", "type": "select", "required": True, "when": ["8", "9"],
         "options": [{"code": "office", "label": "Офис, торговля непродовольственная"},
                     {"code": "warehouse", "label": "Склад общего назначения"},
                     {"code": "food", "label": "Пищевое производство"},
                     {"code": "flammable", "label": "Работа с горючими материалами"}]},
        {"key": "vehicle_type", "label": "Тип транспорта", "type": "select", "required": True, "when": ["3"],
         "options": [{"code": "car", "label": "Легковой"}, {"code": "truck", "label": "Грузовой"},
                     {"code": "special", "label": "Спецтехника"}, {"code": "ev", "label": "Электромобиль"}]},
        {"key": "year", "label": "Год выпуска", "type": "number", "required": True, "when": ["3"]},
    ],
    "optional": [
        {"key": "protection", "label": "Защита объекта", "type": "select", "when": ["8", "9"], "options": PROT_8},
        {"key": "protection", "label": "Противоугонная система", "type": "select", "when": ["3"], "options": PROT_3},
        {"key": "seismic_zone", "label": "Сейсмозона", "type": "number", "unit": "баллов", "when": ["8"]},
        {"key": "wear_pct", "label": "Износ", "type": "number", "unit": "%"},
        {"key": "losses_3y", "label": "Убытки за 3 года (количество и сумма)", "type": "object",
         "fields": [{"key": "count", "type": "number"}, {"key": "amount", "type": "number", "unit": "сум"}]},
        {"key": "deductible", "label": "Франшиза", "type": "object",
         "fields": [{"key": "pct", "type": "number", "unit": "% суммы"}, {"key": "amount", "type": "number", "unit": "сум"}]},
        {"key": "floors", "label": "Этажность", "type": "number", "when": ["8", "9"]},
        {"key": "area_m2", "label": "Площадь", "type": "number", "unit": "м²", "unit_code": "m2", "when": ["8", "9"]},
        {"key": "fire_station_km", "label": "Расстояние до пожарной части", "type": "number", "unit": "км",
         "when": ["8"]},
        {"key": "compartments", "label": "Противопожарные отсеки: число и стоимость наибольшего", "type": "object",
         "when": ["8", "9"],
         "fields": [{"key": "count", "type": "number"}, {"key": "largest_value", "type": "number", "unit": "сум"}]},
        {"key": "neighbours", "label": "Соседство и кучность: разрыв до соседних зданий", "type": "object",
         "when": ["8"], "fields": [{"key": "gap_m", "type": "number", "unit": "м"},
                                   {"key": "note", "type": "text"}]},
        {"key": "bi", "label": "Перерыв в производстве: прибыль + постоянные расходы в месяц, срок восстановления",
         "type": "object", "when": ["8"],
         "fields": [{"key": "monthly_amount", "type": "number", "unit": "сум"},
                    {"key": "recovery_months", "type": "number", "unit": "мес."}]},
        {"key": "documents", "label": "Документы загружены (извлечение делает backend)", "type": "flag"},
    ],
}

# насколько поле уточняет анализ (для подсказки «что добавить»), экспертно
IMPACT = {
    "property": [("compartments", 10, "PML/EML считаются от наибольшего отсека, без него — от всего объекта"),
                 ("seismic_zone", 9, "сейсмический сценарий MFL и коэффициент ставки"),
                 ("protection", 8, "доля отсека, которая сгорит, и коэффициент ставки"),
                 ("losses_3y", 7, "коэффициент убытков и балл уровня риска"),
                 ("bi", 6, "косвенный убыток от простоя в сценариях"),
                 ("deductible", 5, "коэффициент франшизы и точный расчёт премии"),
                 ("wear_pct", 5, "коэффициент износа и сейсмическая уязвимость"),
                 ("fire_station_km", 4, "время прибытия пожарных в сценариях"),
                 ("neighbours", 4, "переход огня между зданиями в MFL"),
                 ("documents", 3, "подтверждение стоимости и характеристик"),
                 ("floors", 2, "сейсмическая нагрузка"),
                 ("area_m2", 1, "стоимость за квадратный метр")],
    "vehicle": [("losses_3y", 9, "коэффициент убытков и балл уровня риска"),
                ("protection", 8, "коэффициент противоугонной системы и сценарий угона"),
                ("deductible", 6, "коэффициент франшизы"),
                ("documents", 5, "подтверждение стоимости (техпаспорт)"),
                ("wear_pct", 3, "сверка стоимости с износом")],
    "other": [("losses_3y", 9, "коэффициент убытков и балл уровня риска"),
              ("deductible", 6, "коэффициент франшизы"),
              ("documents", 5, "подтверждение данных"),
              ("wear_pct", 2, "сверка стоимости")],
}


# ================================================================================================
# Быстрый режим (задача заказчика 22.09.2026: «интерфейс максимально лёгкий для заполнения»)
#
# В быстром режиме человек заполняет четыре поля: класс (или продукт), страховая сумма,
# стоимость объекта, регион. Остальные обязательные поля подставляются по справочнику
# DEFAULTS_BY_CLASS — консервативно, чтобы не занизить риск. Каждое подставленное значение
# помечается assumed=true и попадает в блок assumptions ответа; полнота и уверенность падают.
# Все значения умолчаний экспертные (calibrated = 0).
# ================================================================================================
QUICK_MUST = ["class_code", "sum_insured", "object_value", "region"]   # class_code можно заменить продуктом
ASSUMED_NOTE = "взято по умолчанию — уточните"

# Консервативные значения по группе класса. Логика выбора:
#   construction=mixed  — смешанные конструкции (множитель 1,0 против 0,9 у железобетона);
#   activity            — типовая для класса (склад/производство), не самая дешёвая «офис»;
#   term_months=12      — годовой договор, как в подавляющем большинстве заявок;
#   vehicle_type=car    — легковой (множитель 1,0; у грузового и спецтехники ниже);
#   year=None           — год неизвестен → возраст берётся по худшему варианту «более 15 лет».
DEFAULTS_BY_CLASS = {
    "8": {"object_type": "Производство", "construction": "mixed", "activity": "warehouse", "term_months": 12},
    "9": {"object_type": "Производство", "construction": "mixed", "activity": "warehouse", "term_months": 12},
    "3": {"object_type": "Легковой", "vehicle_type": "car", "term_months": 12, "year": None},
    "_": {"object_type": "Иное имущество", "term_months": 12},
}
# «год неизвестен» → какой вариант возраста справочника взять (худший, множитель 1,4)
UNKNOWN_AGE_OPTION = "a15p"
UNKNOWN_AGE_LABEL = "год выпуска неизвестен — возраст взят по худшему варианту «более 15 лет»"

# Тип объекта → конструкция (умная подстановка, экспертно)
CONSTRUCTION_BY_OBJECT_TYPE = {
    "Склад": "mixed", "Производство": "mixed", "Магазин": "mixed", "Офис": "reinforced",
    "Жильё": "reinforced", "Гостиница": "reinforced", "Машины и оборудование": "mixed",
}
# Тип объекта → деятельность (экспертно)
ACTIVITY_BY_OBJECT_TYPE = {
    "Склад": "warehouse", "Производство": "food", "Магазин": "office", "Офис": "office",
    "Жильё": "office", "Гостиница": "office", "Машины и оборудование": "warehouse",
}
# Ключевое слово в названии продукта → (тип объекта, деятельность). Порядок важен: ищем сверху вниз.
PRODUCT_KEYWORDS = [
    ("электромоб", "Электромобиль", None), ("спецтехник", "Спецтехника", None),
    ("экскаватор", "Спецтехника — экскаватор", None), ("трактор", "Спецтехника — трактор", None),
    ("комбайн", "Спецтехника — комбайн", None), ("кран", "Спецтехника — автокран", None),
    ("прицеп", "Прицеп и полуприцеп", None), ("грузов", "Грузовой", None), ("легков", "Легковой", None),
    ("автотранспорт", "Легковой", None), ("транспортн", "Легковой", None),
    ("склад", "Склад", "warehouse"), ("товар", "Склад", "warehouse"),
    ("гостиниц", "Гостиница", "office"),
    ("магазин", "Магазин", "office"), ("торгов", "Магазин", "office"),
    ("офис", "Офис", "office"),
    ("жил", "Жильё", "office"), ("квартир", "Жильё", "office"), ("дом", "Жильё", "office"),
    ("оборудован", "Машины и оборудование", "warehouse"), ("машин", "Машины и оборудование", "warehouse"),
    ("производств", "Производство", "food"), ("завод", "Производство", "food"),
    ("предприят", "Производство", "food"), ("имуществ", "Производство", "warehouse"),
]

PRESET_KEYS = ["warehouse", "shop", "office", "production", "house", "car", "truck", "excavator", "goods"]


def _defaults_for_classes(classes: list) -> dict:
    """Значения по умолчанию для набора классов (для 8/9 — объединение)."""
    out = {}
    for c in classes:
        for k, v in DEFAULTS_BY_CLASS.get(c, DEFAULTS_BY_CLASS["_"]).items():
            out.setdefault(k, v)
    if not classes:
        out = dict(DEFAULTS_BY_CLASS["_"])
    return out


def product_defaults(ref, product_code: str) -> dict:
    """
    Что можно понять по продукту: класс, тип объекта, деятельность. Таблица product→defaults
    строится автоматически по названию и классу продукта (все 176 продуктов), поэтому у каждого
    значения есть признак guessed: True — угадано по названию, False — взято из справочника классов.
    """
    code = str(product_code or "").strip()
    classes = list(ref.product_classes.get(code) or [])
    name = (ref.products.get(code) or {}).get("name", "")
    out = {"product_code": code, "product_name": name, "classes": classes,
           "class_code": "/".join(classes) if classes else None, "class_guessed": False,
           "object_type": None, "object_type_guessed": True, "activity": None, "activity_guessed": True}
    low = name.lower()
    for kw, otype, act in PRODUCT_KEYWORDS:
        if kw in low:
            out["object_type"], out["activity"] = otype, act
            break
    if out["object_type"] is None:
        d = _defaults_for_classes(classes)
        out["object_type"] = d.get("object_type")
    # тип объекта должен существовать в базовых ставках хотя бы одного класса продукта
    if classes and not any((c, out["object_type"]) in ref.base_rates for c in classes):
        d = _defaults_for_classes(classes)
        out["object_type"] = d.get("object_type")
    if out["activity"] is None:
        out["activity"] = ACTIVITY_BY_OBJECT_TYPE.get(out["object_type"] or "")
    return out


def region_from_branch(branch: str):
    """Филиал пользователя → регион (точное совпадение или вхождение названия/псевдонима)."""
    from . import market_picture as mp
    key, name = mp.resolve_region(branch)
    if name:
        return name
    v = mp._norm(branch)
    if not v:
        return None
    words = v.split()
    for rname, rkey, soato, aliases in mp.REGION_TABLE + [mp.REPUBLIC]:
        if rkey == "total":
            continue
        for a in (mp._norm(rname),) + tuple(aliases):
            if not a:
                continue
            if a in v:
                return rname
            # «Бухарский филиал» → «бухара»: сравниваем основы слов (окончания в русском разные)
            stem = a[:5]
            if len(a) >= 5 and any(w.startswith(stem) for w in words):
                return rname
    return None


def validate_quick(must: dict, optional: Optional[dict] = None) -> dict:
    """Проверка быстрого режима: нужны только класс (или продукт), сумма, стоимость, регион."""
    must = must or {}
    missing, errors = [], {}
    if not _present(must.get("class_code")) and not _present(must.get("product_code")):
        missing.append("class_code")
    for key in ("sum_insured", "object_value"):
        if not _present(must.get(key)):
            missing.append(key)
        else:
            v = _to_float(must.get(key))
            if v is None or v <= 0:
                errors[key] = "нужно положительное число в сумах"
    if not _present(must.get("region")):
        missing.append("region")
    return {"ok": not missing and not errors, "missing": missing, "errors": errors}


def _label_of(key: str) -> str:
    for f in FIELDS["must"] + FIELDS["optional"]:
        if f["key"] == key:
            return f["label"]
    return key


def _option_label(key: str, code) -> str:
    for f in FIELDS["must"]:
        if f["key"] == key and f.get("options"):
            for o in f["options"]:
                if o["code"] == code:
                    return o["label"]
    return str(code)


def apply_defaults(con, must: dict, optional: Optional[dict] = None, *, user: Optional[dict] = None) -> tuple:
    """
    Быстрый режим: достраивает must до полного набора. Возврат (must, optional, assumptions),
    где assumptions — [{key, label, value, value_label, why, assumed:true, calibrated:0}].
    Явно заданные поля не трогаются: при полном вводе результат совпадает с обычным режимом.
    """
    must = dict(must or {})
    optional = dict(optional or {})
    ref = db.load_reference(con)
    assumptions = []

    def assume(key, value, why, value_label=None):
        must[key] = value
        assumptions.append({"key": key, "label": _label_of(key), "value": value,
                            "value_label": value_label if value_label is not None else _option_label(key, value),
                            "why": why, "assumed": True, "note": ASSUMED_NOTE, "calibrated": CALIBRATED})

    # 1. класс из продукта
    pd = product_defaults(ref, must.get("product_code")) if _present(must.get("product_code")) else None
    if not _present(must.get("class_code")) and pd and pd["class_code"]:
        assume("class_code", pd["class_code"],
               f"класс взят по продукту {pd['product_code']} «{pd['product_name']}»", pd["class_code"])
    classes = parse_classes(must.get("class_code"))
    d = _defaults_for_classes(classes)

    # 2. тип объекта: по продукту, иначе по классу
    if not _present(must.get("object_type")):
        if pd and pd["object_type"]:
            why = (f"тип объекта угадан по названию продукта «{pd['product_name']}»" if pd["object_type_guessed"]
                   else f"тип объекта по продукту {pd['product_code']}")
            assume("object_type", pd["object_type"], why, pd["object_type"])
        elif d.get("object_type"):
            assume("object_type", d["object_type"],
                   f"типовой объект класса {'/'.join(classes) or '—'}", d["object_type"])

    # 3. конструкция — по типу объекта, иначе смешанная
    if any(c in PROPERTY_CLASSES for c in classes) and not _present(must.get("construction")):
        otype = str(must.get("object_type") or "")
        val = CONSTRUCTION_BY_OBJECT_TYPE.get(otype) or d.get("construction") or "mixed"
        why = (f"конструкция по типу объекта «{otype}»" if otype in CONSTRUCTION_BY_OBJECT_TYPE
               else "конструкция не указана — взяты смешанные конструкции (осторожнее железобетона)")
        assume("construction", val, why)

    # 4. деятельность — по продукту/типу объекта
    if any(c in PROPERTY_CLASSES for c in classes) and not _present(must.get("activity")):
        otype = str(must.get("object_type") or "")
        val = ((pd or {}).get("activity") if pd else None) or ACTIVITY_BY_OBJECT_TYPE.get(otype) \
            or d.get("activity") or "warehouse"
        assume("activity", val, f"типовая деятельность для объекта «{otype or '—'}»")

    # 5. транспорт: тип и год
    if any(c in VEHICLE_CLASSES for c in classes):
        if not _present(must.get("vehicle_type")):
            otype = str(must.get("object_type") or "")
            val = ("special" if otype.startswith("Спецтехника") else
                   "truck" if otype == "Грузовой" else "ev" if otype == "Электромобиль" else
                   d.get("vehicle_type") or "car")
            assume("vehicle_type", val, f"тип транспорта по объекту «{otype or 'Легковой'}»")
        if not _present(must.get("year")):
            assumptions.append({"key": "year", "label": _label_of("year"), "value": None,
                                "value_label": "неизвестен", "why": UNKNOWN_AGE_LABEL,
                                "assumed": True, "note": ASSUMED_NOTE, "calibrated": CALIBRATED})
            must.pop("year", None)
            must["year_unknown"] = True

    # 6. срок
    if not _present(must.get("term_months")):
        assume("term_months", d.get("term_months", 12), "срок не указан — взят годовой договор", "12 мес.")

    # 7. регион из профиля пользователя (филиал), персональные данные не используются
    if not _present(must.get("region")) and user:
        r = region_from_branch(str(user.get("branch") or ""))
        if r:
            assume("region", r, f"регион взят по филиалу пользователя", r)

    return must, optional, assumptions


def quick_plan(con, class_code: str = "", product_code: str = "", user: Optional[dict] = None) -> list:
    """«Что подставим и почему» для GET /analytics/risk/fields?mode=quick — без страховых сумм."""
    probe = {"class_code": class_code, "product_code": product_code}
    _m, _o, a = apply_defaults(con, probe, {}, user=user)
    return a


def presets(con) -> list:
    """Типовые объекты одним нажатием. Тексты ru здесь, ключи i18n — для uz/en (их даёт дизайнер)."""
    raw = [
        ("warehouse", "Склад", "9", "0808", "Склад", {"construction": "mixed", "activity": "warehouse"}),
        ("shop", "Магазин", "9", "0808", "Магазин", {"construction": "mixed", "activity": "office"}),
        ("office", "Офис", "9", "0808", "Офис", {"construction": "reinforced", "activity": "office"}),
        ("production", "Производство", "8/9", "0807", "Производство",
         {"construction": "mixed", "activity": "food"}),
        ("house", "Жилой дом", "8", "0807", "Жильё", {"construction": "reinforced", "activity": "office"}),
        ("car", "Легковой автомобиль", "3", "0308", "Легковой", {"vehicle_type": "car"}),
        ("truck", "Грузовой автомобиль", "3", "0308", "Грузовой", {"vehicle_type": "truck"}),
        ("excavator", "Экскаватор", "3", "0318", "Спецтехника — экскаватор", {"vehicle_type": "special"}),
        ("goods", "Товар в обороте", "9", "0808", "Склад", {"construction": "mixed", "activity": "warehouse"}),
    ]
    out = []
    for key, title, cls, prod, otype, extra in raw:
        must = {"class_code": cls, "product_code": prod, "object_type": otype, "term_months": 12, **extra}
        out.append({"key": key, "title": title, "i18n_key": f"preset.{key}",
                    "class_code": cls, "product_code": prod, "object_type": otype,
                    "must": must,
                    "hint": f"Заполните только страховую сумму, стоимость и регион",
                    "hint_i18n_key": "preset.hint",
                    "calibrated": CALIBRATED})
    return out


# ================================================================================================
# Последние введённые значения пользователя (форма запоминает выбор). Персональных данных нет.
# ================================================================================================
LAST_INPUT_SQL = """
CREATE TABLE IF NOT EXISTS risk_last_input (
    user_id    INTEGER PRIMARY KEY,
    updated_at TEXT NOT NULL,
    data_json  TEXT NOT NULL
);
"""
LAST_ALLOWED = ["class_code", "product_code", "object_type", "region", "term_months", "construction",
                "activity", "vehicle_type", "protection", "seismic_zone", "mode"]
LAST_MAX_LEN = 4000


def _ensure_last_table(con) -> None:
    con.execute(LAST_INPUT_SQL)


def clean_last(data: dict) -> dict:
    """Только справочные поля выбора: ни сумм, ни имён, ни телефонов — персональные данные не пишем."""
    out = {}
    for k in LAST_ALLOWED:
        v = (data or {}).get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = v
        elif isinstance(v, str):
            out[k] = v.strip()[:100]
    return out


def load_last(con, user_id) -> dict:
    try:
        r = con.execute("SELECT data_json, updated_at FROM risk_last_input WHERE user_id=?",
                        (int(user_id),)).fetchone()
    except Exception:
        return {"values": {}, "updated_at": None}
    if not r:
        return {"values": {}, "updated_at": None}
    try:
        return {"values": clean_last(json.loads(r[0])), "updated_at": r[1]}
    except ValueError:
        return {"values": {}, "updated_at": None}


def save_last(con, user_id, data: dict) -> dict:
    vals = clean_last(data)
    payload = json.dumps(vals, ensure_ascii=False)
    if len(payload) > LAST_MAX_LEN:
        raise ValueError("слишком много данных")
    _ensure_last_table(con)
    con.execute("INSERT INTO risk_last_input (user_id, updated_at, data_json) VALUES (?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET updated_at=excluded.updated_at, data_json=excluded.data_json",
                (int(user_id), db.now(), payload))
    return {"values": vals, "updated_at": db.now()}


# ================================================================================================
# Вспомогательное
# ================================================================================================
def money(x) -> str:
    if x is None:
        return "—"
    return f"{x:,.0f}".replace(",", " ")


def num(x, d: int = 2) -> str:
    """Число с запятой для формул: 0.7 → 0,70."""
    return f"{x:.{d}f}".replace(".", ",")


def years_ru(n: int) -> str:
    """222 → «222 года», 55 → «55 лет», 21 → «21 год»."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} год"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} года"
    return f"{n} лет"


PROT_LABEL = {o["code"]: o["label"] for o in PROT_8}


def _present(v) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip() != ""
    if isinstance(v, (dict, list)):
        return len(v) > 0
    return True


def _to_float(v) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(" ", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def parse_classes(class_code) -> list:
    """«8/9», «8,9», «8 и 9» → ["8","9"]; «9» → ["9"]."""
    s = str(class_code or "").replace(" и ", "/").replace(",", "/").replace(";", "/").replace("+", "/")
    out = []
    for p in s.split("/"):
        p = p.strip()
        if p and p not in out:
            out.append(p)
    return out


def _group(classes: list) -> str:
    if any(c in PROPERTY_CLASSES for c in classes):
        return "property"
    if any(c in VEHICLE_CLASSES for c in classes):
        return "vehicle"
    return "other"


def _when_ok(field: dict, classes: list) -> bool:
    w = field.get("when")
    return not w or any(c in w for c in classes)


def fields_for(class_code) -> dict:
    """Поля формы, нужные именно этому классу (для мини-аппа)."""
    cl = parse_classes(class_code)
    return {"must": [f for f in FIELDS["must"] if _when_ok(f, cl)],
            "optional": [f for f in FIELDS["optional"] if _when_ok(f, cl)]}


def _seismic_zone(v) -> Optional[int]:
    """8 → 8; «8–9» → 9 (берём худшую); None → None."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return int(round(v))
    parts = [p for p in str(v).replace("–", "-").replace("—", "-").split("-") if p.strip()]
    vals = [_to_float(p) for p in parts]
    vals = [x for x in vals if x is not None]
    return int(round(max(vals))) if vals else None


def _term_days(months: float) -> int:
    return 365 if abs(months - 12) < 1e-9 else int(round(months * 365 / 12))


# ================================================================================================
# Проверка ввода
# ================================================================================================
def validate(must: dict, optional: Optional[dict] = None) -> dict:
    must = must or {}
    optional = optional or {}
    classes = parse_classes(must.get("class_code"))
    missing, errors = [], {}
    for f in FIELDS["must"]:
        if not f.get("required"):
            continue
        if f["key"] != "class_code" and not _when_ok(f, classes):
            continue
        if f["key"] == "year" and must.get("year_unknown") is True:
            continue                      # быстрый режим: год неизвестен, возраст берётся по худшему варианту
        if not _present(must.get(f["key"])):
            missing.append(f["key"])

    for key in ("sum_insured", "object_value"):
        if _present(must.get(key)):
            v = _to_float(must.get(key))
            if v is None or v <= 0:
                errors[key] = "нужно положительное число в сумах"
    if _present(must.get("term_months")):
        t = _to_float(must.get("term_months"))
        if t is None or t <= 0 or t > 60:
            errors["term_months"] = "срок в месяцах от 1 до 60"
    if _present(must.get("year")):
        y = _to_float(must.get("year"))
        if y is None or y < 1950 or y > date.today().year + 1:
            errors["year"] = f"год выпуска от 1950 до {date.today().year + 1}"
    for key in ("construction", "activity", "vehicle_type"):
        f = next(x for x in FIELDS["must"] if x["key"] == key)
        if _present(must.get(key)) and must[key] not in [o["code"] for o in f["options"]]:
            errors[key] = "значение не из списка: " + ", ".join(o["code"] for o in f["options"])

    # дополнительные поля — только формат; отсутствие не ошибка
    if _present(optional.get("seismic_zone")):
        z = _seismic_zone(optional.get("seismic_zone"))
        if z is None or z < 5 or z > 10:
            errors["seismic_zone"] = "сейсмозона в баллах от 6 до 10 (можно «8–9»)"
    if _present(optional.get("wear_pct")):
        w = _to_float(optional.get("wear_pct"))
        if w is None or w < 0 or w > 100:
            errors["wear_pct"] = "износ от 0 до 100%"
    if _present(optional.get("protection")):
        codes = [o["code"] for o in (PROT_3 if classes and all(c in VEHICLE_CLASSES for c in classes) else PROT_8)]
        if optional["protection"] not in codes:
            errors["protection"] = "значение не из списка: " + ", ".join(codes)
    for key, subs in (("losses_3y", ("count", "amount")), ("deductible", ("pct", "amount")),
                      ("compartments", ("count", "largest_value")),
                      ("bi", ("monthly_amount", "recovery_months")), ("neighbours", ("gap_m",))):
        v = optional.get(key)
        if not _present(v):
            continue
        if key == "deductible" and not isinstance(v, dict):
            v = {"pct": v}
        if not isinstance(v, dict):
            errors[key] = "ожидается набор полей: " + ", ".join(subs)
            continue
        for s in subs:
            if _present(v.get(s)):
                x = _to_float(v.get(s))
                if x is None or x < 0:
                    errors[key] = f"{s}: нужно неотрицательное число"
    comp = optional.get("compartments")
    if isinstance(comp, dict) and _present(comp.get("largest_value")) and _present(must.get("object_value")):
        lv, ov = _to_float(comp.get("largest_value")), _to_float(must.get("object_value"))
        if lv is not None and ov and lv > ov:
            errors["compartments"] = "стоимость наибольшего отсека больше стоимости объекта"
    ded = optional.get("deductible")
    if _present(ded):
        pct = _to_float(ded.get("pct") if isinstance(ded, dict) else ded)
        if pct is not None and pct > 50:
            errors["deductible"] = "франшиза больше 50% суммы — проверьте ввод"
    return {"ok": not missing and not errors, "missing": missing, "errors": errors}


# ================================================================================================
# Пороги уровня риска: по умолчанию + правка админа в базе (история версий, правило 9)
# ================================================================================================
def _merge(base: dict, patch: dict) -> dict:
    out = json.loads(json.dumps(base))
    for k, v in (patch or {}).items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def load_thresholds(con) -> dict:
    """Действующие пороги: последняя сохранённая версия поверх DEFAULT_THRESHOLDS.
    Таблицы ещё нет (сервер не перезапускался после обновления схемы) — пороги по умолчанию."""
    try:
        r = con.execute("SELECT id, created_at, created_by, thresholds_json FROM risk_thresholds "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    except Exception:
        r = None
    if not r:
        out = _merge(DEFAULT_THRESHOLDS, {})
        out["_source"] = {"id": None, "what": "по умолчанию (экспертно)"}
        return out
    out = _merge(DEFAULT_THRESHOLDS, json.loads(r[3]))
    out["_source"] = {"id": r[0], "created_at": r[1], "created_by": r[2], "what": "правка администратора"}
    return out


def check_thresholds(t: dict) -> list:
    """Ошибки в порогах (пустой список — всё верно)."""
    errs = []
    unknown = [k for k in t if not k.startswith("_") and k not in DEFAULT_THRESHOLDS]
    if unknown:
        errs.append("неизвестные ключи: " + ", ".join(unknown))
    w = t.get("weights", {})
    if set(w) - set(DEFAULT_THRESHOLDS["weights"]):
        errs.append("веса: допустимы только " + ", ".join(DEFAULT_THRESHOLDS["weights"]))
    if any(not isinstance(x, (int, float)) or x < 0 for x in w.values()) or sum(w.values()) <= 0:
        errs.append("веса должны быть неотрицательными числами с ненулевой суммой")
    ey = t.get("external_volatility_years")
    if not isinstance(ey, (int, float)) or isinstance(ey, bool) or ey != int(ey) or not 3 <= ey <= 30:
        errs.append("external_volatility_years: целое число лет от 3 до 30")
    lb = t.get("level_bounds", [])
    if len(lb) != 4 or any(not isinstance(x, (int, float)) for x in lb) or lb != sorted(lb) \
            or lb[0] <= 0 or lb[-1] >= 100 or len(set(lb)) != 4:
        errs.append("level_bounds: четыре возрастающих числа между 0 и 100")
    for k in ("rate_ratio", "mfl_to_retention", "mfl_pct_of_sum", "loss_ratio", "confidence_bands_pct",
              "wear_pct_bands", "external_ratio"):
        v = t.get(k)
        if not (isinstance(v, list) and len(v) == 2 and all(isinstance(x, (int, float)) for x in v) and v[0] < v[1]):
            errs.append(f"{k}: пара [нижняя, верхняя], нижняя меньше верхней")
    sb = t.get("scenario_bands_pct", [])
    if len(sb) != 4 or sb != sorted(sb):
        errs.append("scenario_bands_pct: четыре возрастающих числа")
    if t.get("stop_min_level") not in LEVEL_NAMES:
        errs.append("stop_min_level: одно из " + ", ".join(LEVEL_NAMES))
    errs += _check_franchise_thresholds(t)
    return errs


def _check_franchise_thresholds(t: dict) -> list:
    """Проверка порогов франшизы (app/franchise.py)."""
    errs = []
    by = t.get("franchise_by_level") or {}
    if set(by) != set(LEVEL_NAMES):
        errs.append("franchise_by_level: нужны все уровни — " + ", ".join(LEVEL_NAMES))
    for name, v in by.items():
        if not (isinstance(v, list) and len(v) == 2 and all(isinstance(x, (int, float)) for x in v)
                and 0 <= v[0] <= v[1] <= 100):
            errs.append(f"franchise_by_level[{name}]: пара [от, до] в процентах от 0 до 100")
    mult = t.get("franchise_multipliers") or {}
    try:
        pts = sorted((float(k), float(v)) for k, v in mult.items())
    except (TypeError, ValueError):
        pts = []
        errs.append("franchise_multipliers: ключ — процент франшизы, значение — множитель")
    if pts and (pts[0][0] != 0 or pts[0][1] != 1.0):
        errs.append("franchise_multipliers: при франшизе 0% множитель должен быть 1")
    if any(b[1] > a[1] for a, b in zip(pts, pts[1:])):
        errs.append("franchise_multipliers: с ростом франшизы множитель не должен расти")
    if any(v <= 0 for _, v in pts):
        errs.append("franchise_multipliers: множители должны быть больше нуля")
    for k, lo, hi in (("franchise_small_loss_pct", 0, 100), ("franchise_small_share", 0, 1),
                      ("franchise_dominant_share", 0, 1),
                      ("franchise_loss_count_high", 1, 20), ("franchise_critical_peril_pct", 0, 100),
                      ("franchise_critical_seismic_zone", 6, 10), ("franchise_measures_note_days", 1, 365)):
        v = t.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
            errs.append(f"{k}: число от {lo} до {hi}")
    caps = t.get("franchise_class_caps")
    if not isinstance(caps, dict) or any(not isinstance(v, (int, float)) or not 0 < v <= 100
                                         for v in (caps or {}).values()):
        errs.append("franchise_class_caps: класс → потолок франшизы в процентах (0–100]")
    return errs


def save_thresholds(con, thresholds: dict, user: str) -> dict:
    """Сохраняет новую версию порогов (старые остаются в истории). Возвращает действующие пороги.
    Ошибка в порогах — ValueError с перечнем ошибок, база не меняется."""
    merged = _merge(DEFAULT_THRESHOLDS, thresholds or {})
    merged["calibrated"] = 0              # правка админа — тоже экспертная, пока нет калибровки
    errs = check_thresholds(merged)
    if errs:
        raise ValueError("; ".join(errs))
    if not _has_table(con):
        con.execute(THRESHOLDS_SQL)      # одна инструкция: executescript зафиксировал бы транзакцию
    diff = {k: v for k, v in merged.items() if DEFAULT_THRESHOLDS.get(k) != v}
    con.execute("INSERT INTO risk_thresholds (created_at, created_by, thresholds_json, calibrated, note) "
                "VALUES (?,?,?,?,?)", (db.now(), user, json.dumps(diff, ensure_ascii=False), 0,
                                       "правка порогов уровня риска"))
    db.audit(con, user, "изменены пороги уровня риска", "risk_thresholds", diff)
    return load_thresholds(con)


def _has_table(con) -> bool:
    try:
        return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='risk_thresholds'"
                           ).fetchone() is not None
    except Exception:
        return False


def _level_of(score: float, bounds: list) -> str:
    for b, name in zip(bounds, LEVEL_NAMES):
        if score < b:
            return name
    return LEVEL_NAMES[-1]


def _band_level(pct: float, bands: list) -> str:
    return _level_of(pct, bands)


def _lin(x: float, lo: float, hi: float) -> float:
    """0 баллов на lo и ниже, 100 на hi и выше, между — по прямой."""
    if x <= lo:
        return 0.0
    if x >= hi:
        return 100.0
    return (x - lo) / (hi - lo) * 100


# ================================================================================================
# Ввод → факторы движка
# ================================================================================================
def _deductible_pct(optional: dict, sum_insured: float) -> Optional[float]:
    d = optional.get("deductible")
    if not _present(d):
        return None
    if not isinstance(d, dict):
        return _to_float(d)
    if _present(d.get("pct")):
        return _to_float(d.get("pct"))
    if _present(d.get("amount")) and sum_insured:
        return _to_float(d.get("amount")) / sum_insured * 100
    return None


def _franchise_option(pct: Optional[float]) -> Optional[str]:
    """Ближайший вариант справочника не выше фактической франшизы."""
    if pct is None:
        return None
    if pct >= 2:
        return "f2"
    if pct >= 1:
        return "f1"
    if pct >= 0.5:
        return "f05"
    return "f0"


def engine_factors(classes: list, must: dict, optional: dict, th: dict, as_of: date) -> tuple:
    """Факторы движка из формы. Возврат (factors, notes). Нет данных — фактор не ставится
    (движок тогда берёт множитель 1, то есть «как у среднего объекта»)."""
    f, notes = {}, []
    S = _to_float(must.get("sum_insured")) or 0
    if any(c in PROPERTY_CLASSES for c in classes):
        f["construction"] = must.get("construction")
        f["activity"] = must.get("activity")
        if _present(optional.get("protection")):
            f["protection"] = optional["protection"]
        z = _seismic_zone(optional.get("seismic_zone"))
        if z is not None:
            opt = "z7" if z <= 7 else ("z8" if z == 8 else "z9")
            f["seismic"] = opt
            if z < 7 or z > 9:
                notes.append(f"Сейсмозона {z} баллов: в справочнике коэффициентов есть только 7–9 баллов, "
                             f"взят вариант {opt[1:]} баллов")
        wear = _to_float(optional.get("wear_pct"))
        if wear is not None:
            lo, hi = th["wear_pct_bands"]
            f["wear"] = "new" if wear <= lo else ("mid" if wear <= hi else "old")
            notes.append(f"Износ {wear:g}% переведён в возраст здания справочника "
                         f"(до {lo}% → «до 10 лет», до {hi}% → «10–30 лет», выше → «более 30 лет») — {EXPERT}")
    if any(c in VEHICLE_CLASSES for c in classes):
        vt = must.get("vehicle_type")
        if vt:
            f["veh_type"] = vt
        y = _to_float(must.get("year"))
        if y is not None:
            age = as_of.year - int(y)
            f["veh_age"] = "a3" if age <= 3 else ("a7" if age <= 7 else ("a15" if age <= 15 else "a15p"))
        elif must.get("year_unknown") is True:
            f["veh_age"] = UNKNOWN_AGE_OPTION
            notes.append(UNKNOWN_AGE_LABEL + f" — {EXPERT}")
        if _present(optional.get("protection")):
            f["antitheft"] = optional["protection"]
        # спецтехника и водители (13.10.2026): варианты справочника из фактов акта (app/act_pkg/facts.ra_optional);
        # неизвестный вариант движок не найдёт в справочнике — множитель 1, как «не указано»
        for key in ("engine_hours", "spec_site", "spec_guard", "spec_operator", "drivers"):
            if _present(optional.get(key)):
                f[key] = optional[key]
    losses = optional.get("losses_3y")
    if isinstance(losses, dict) and _present(losses.get("count")):
        n = int(_to_float(losses.get("count")) or 0)
        f["loss_history"] = "clean" if n == 0 else ("one" if n == 1 else "many")
    fr = _franchise_option(_deductible_pct(optional, S))
    if fr:
        f["franchise"] = fr
    return f, notes


def _product_for(cls: str, must: dict, ref) -> tuple:
    """Продукт для части договора. Возврат (product_code, note)."""
    pc = str(must.get("product_code") or "").strip()
    if pc and cls in (ref.product_classes.get(pc) or []):
        return pc, None
    special = must.get("vehicle_type") == "special" or str(must.get("object_type", "")).startswith("Спецтехника")
    default = DEFAULT_PRODUCT_SPECIAL if cls == "3" and special else DEFAULT_PRODUCT.get(cls)
    if default:
        why = (f"продукт {pc} не содержит класс {cls}" if pc else "продукт не указан")
        return default, f"Класс {cls}: {why} — минимальный тариф взят по типовому продукту {default}"
    return pc, (None if pc else f"Класс {cls}: продукт не указан — минимальный тариф не проверяется")


def _auto_premium(ref, inp: Input) -> float:
    """Премия по автоматической ставке (как calculate без ручной ставки)."""
    r = rate_for(ref, inp)
    mr = min_rate(ref, inp.product_code, inp.payer_type)
    floor = None if pricing_mode_of(ref, inp) in NEGOTIATED_MODES else mr["floor"]
    return premium_of(max(r["gross_pct"], floor or 0), inp.sum_insured, inp.term_days)


def _with_factors(inp: Input, factors: dict) -> Input:
    alt = Input(**{**inp.__dict__})
    alt.factors = factors
    alt.applied_rate_pct = None
    return alt


def _effect(m: float) -> str:
    d = (m - 1) * 100
    if abs(d) < 0.5:
        return "0%"
    return f"+{d:.0f}%" if d > 0 else f"−{abs(d):.0f}%"


# ================================================================================================
# Сценарии максимального убытка
# ================================================================================================
def _scenarios_class8(S, V, must, optional, factors, th) -> dict:
    """Пожар по наибольшему отсеку, землетрясение по всей площадке; берётся большее."""
    k = min(S, V) / V
    comp = optional.get("compartments") if isinstance(optional.get("compartments"), dict) else {}
    C = _to_float(comp.get("largest_value")) if comp else None
    assumptions = []
    if not C:
        C = V
        assumptions.append(f"Отсеки не указаны — весь объект считается одним отсеком ({money(V)} сум) — {EXPERT}")
    prot = optional.get("protection") if _present(optional.get("protection")) else None
    if prot is None:
        assumptions.append(f"Защита не указана — для сценариев взят худший вариант «нет сигнализации и охраны» — {EXPERT}")
    prot = prot or "none"
    adds, add_txt = 0.0, []
    if must.get("construction") == "wood":
        adds += FIRE_ADD["wood"]
        add_txt.append(f"горючие конструкции +{num(FIRE_ADD['wood'])}")
    if must.get("activity") == "flammable":
        adds += FIRE_ADD["flammable"]
        add_txt.append(f"горючие материалы +{num(FIRE_ADD['flammable'])}")
    km = _to_float(optional.get("fire_station_km"))
    if km is not None and km > th["fire_station_far_km"]:
        adds += FIRE_ADD["far_station"]
        add_txt.append(f"пожарная часть дальше {th['fire_station_far_km']} км +{num(FIRE_ADD['far_station'])}")
    nb = optional.get("neighbours") if isinstance(optional.get("neighbours"), dict) else {}
    gap = _to_float(nb.get("gap_m")) if nb else None
    spread = gap is not None and gap < th["neighbour_gap_m"]
    zone = _seismic_zone(optional.get("seismic_zone"))
    bi = optional.get("bi") if isinstance(optional.get("bi"), dict) else {}
    bi_month = _to_float(bi.get("monthly_amount")) if bi else None
    bi_months = _to_float(bi.get("recovery_months")) if bi else None
    bi_base = bi_month * bi_months if bi_month and bi_months else 0.0

    constr = must.get("construction") or "mixed"
    wear = factors.get("wear", "mid")
    floors = _to_float(optional.get("floors"))
    eq_mult = EQ_CONSTRUCTION.get(constr, 1.0) * EQ_WEAR.get(wear, 1.0)
    eq_mult_txt = f"конструкция {num(EQ_CONSTRUCTION.get(constr, 1.0))} × износ {num(EQ_WEAR.get(wear, 1.0))}"
    if floors and floors >= EQ_TALL_FLOORS:
        eq_mult *= EQ_TALL_MULT
        eq_mult_txt += f" × этажность {num(EQ_TALL_MULT)}"

    out = {}
    for s in ("PML", "EML", "MFL"):
        a = list(assumptions)
        base_C = V if (s == "MFL" and spread) else C
        if s == "MFL" and spread:
            a.append(f"Разрыв до соседних зданий {gap:g} м меньше {th['neighbour_gap_m']} м — огонь переходит, "
                     f"в MFL берётся весь объект — {EXPERT}")
        d = min(1.0, FIRE_SHARE[s][prot] + (adds if s != "MFL" else 0.0))
        fire = base_C * d
        fire_txt = (f"пожар {money(base_C)} × {num(d)}"
                    + (f" (защита «{PROT_LABEL[prot]}» {num(FIRE_SHARE[s][prot])}; {', '.join(add_txt)})"
                       if add_txt and s != "MFL" else f" (защита «{PROT_LABEL[prot]}»)"))
        a.append(f"Доля отсека, которая погибнет при пожаре, {num(d)} — {EXPERT}")
        eq, eq_txt = None, ""
        if zone is not None:
            zz = min(max(zone, 6), 10)
            share = min(1.0, EQ_SHARE[zz][s] * eq_mult)
            eq = V * share
            eq_txt = (f"землетрясение {money(V)} × {num(EQ_SHARE[zz][s])} × {eq_mult_txt} = "
                      f"{money(eq)} (вся площадка, {zone} баллов)")
            a.append(f"Сейсмический сценарий по всей площадке: доля {num(EQ_SHARE[zz][s])} для {zz} баллов — {EXPERT}")
        elif s == "MFL":
            eq = V * 1.0
            eq_txt = f"сейсмозона не указана — полное уничтожение {money(V)} × 1,00"
            a.append(f"Сейсмозона не указана — катастрофический сценарий принят как полное уничтожение — {EXPERT}")
        prop_raw = max(fire, eq or 0.0)
        prop = prop_raw * k
        d_bi = BI_SHARE_MFL if s == "MFL" else max(d, (eq / V) if eq else 0.0)
        d_bi = min(1.0, d_bi)
        bi_loss = bi_base * d_bi
        if bi_base:
            a.append(f"Простой: {money(bi_month)} в месяц × {bi_months:g} мес. × доля {num(d_bi)} — "
                     f"включён, если перерыв в производстве застрахован — {EXPERT}")
        dominant = "землетрясение" if eq is not None and eq > fire else "пожар"
        formula = (f"{s} = max({fire_txt} = {money(fire)}"
                   + (f"; {eq_txt}" if eq_txt else "") + f")"
                   + (f" × доля страхования {num(k)}" if k < 1 else "")
                   + (f" + простой {money(bi_base)} × {num(d_bi)}" if bi_base else "")
                   + f" = {money(prop + bi_loss)} сум")
        out[s] = {"amount": prop + bi_loss, "property_loss": prop, "bi_loss": bi_loss,
                  "fire_loss": fire * k, "earthquake_loss": (eq * k) if eq is not None else None,
                  "dominant": dominant, "formula": formula, "assumptions": a}
    return out


def _scenarios_class9(S, V, optional) -> dict:
    """Класс 9 (кража со взломом, вода, град и прочее): убыток ограничен наибольшим отсеком."""
    k = min(S, V) / V
    comp = optional.get("compartments") if isinstance(optional.get("compartments"), dict) else {}
    C = _to_float(comp.get("largest_value")) if comp else None
    base_a = []
    if not C:
        C = V
        base_a.append(f"Отсеки не указаны — весь объект считается одним помещением ({money(V)} сум) — {EXPERT}")
    prot = optional.get("protection") if _present(optional.get("protection")) else None
    if prot is None:
        base_a.append(f"Защита не указана — взят худший вариант «нет сигнализации и охраны» — {EXPERT}")
    prot = prot or "none"
    m = DAMAGE9_PROTECTION[prot]
    out = {}
    for s in ("PML", "EML", "MFL"):
        d = min(1.0, DAMAGE9_SHARE[s] * m)
        prop = C * d * k
        a = base_a + [f"Доля помещения, которая пострадает (кража, вода, прочее), {num(DAMAGE9_SHARE[s])} "
                      f"× защита {num(m)} = {num(d)} — {EXPERT}",
                      "Пожар и землетрясение — риски класса 8, в классе 9 их нет"]
        formula = (f"{s} = {money(C)} × {num(DAMAGE9_SHARE[s])} × {num(m)}"
                   + (f" × доля страхования {num(k)}" if k < 1 else "") + f" = {money(prop)} сум")
        out[s] = {"amount": prop, "property_loss": prop, "bi_loss": 0.0, "fire_loss": None,
                  "earthquake_loss": None, "dominant": "кража и повреждение", "formula": formula, "assumptions": a}
    return out


def _scenarios_vehicle(S, V, optional) -> dict:
    """Одна единица транспорта: полная гибель или угон реальны, поэтому доли высокие."""
    k = min(S, V) / V
    prot = optional.get("protection") if _present(optional.get("protection")) else None
    protected = prot in ("immo", "tracker")
    shares = {"PML": VEHICLE_SHARE["PML"],
              "EML": VEHICLE_SHARE["EML_protected" if protected else "EML_unprotected"],
              "MFL": VEHICLE_SHARE["MFL"]}
    why = {"PML": "крупное ДТП с ремонтом",
           "EML": ("конструктивная гибель (ремонт дороже 75% стоимости)" if protected
                   else "угон или полная гибель: противоугонной системы с иммобилайзером или поиском нет"),
           "MFL": "полная гибель или угон"}
    out = {}
    for s, d in shares.items():
        prop = min(S, V) * d
        out[s] = {"amount": prop, "property_loss": prop, "bi_loss": 0.0, "fire_loss": None,
                  "earthquake_loss": None, "dominant": why[s],
                  "formula": f"{s} = {money(min(S, V))} × {num(d)} = {money(prop)} сум",
                  "assumptions": [f"{why[s]}: доля {num(d)} страховой суммы — {EXPERT}"]}
    return out


def _scenarios_generic(S, V, cls) -> dict:
    out = {}
    base = min(S, V) if V else S
    for s in ("PML", "EML", "MFL"):
        out[s] = {"amount": base, "property_loss": base, "bi_loss": 0.0, "fire_loss": None,
                  "earthquake_loss": None, "dominant": "полная сумма",
                  "formula": f"{s} = страховая сумма {money(base)} сум",
                  "assumptions": [f"Для класса {cls} модели максимального убытка нет — осторожно взята вся "
                                  f"страховая сумма — {EXPERT}"]}
    return out


# ================================================================================================
# Главная функция
# ================================================================================================
def analyze(con, must: dict, optional: Optional[dict] = None, thresholds: Optional[dict] = None,
            market: Optional[dict] = None, *, as_of: Optional[date] = None,
            assumptions: Optional[list] = None) -> dict:
    must = dict(must or {})
    optional = dict(optional or {})
    as_of = as_of or date.today()
    v = validate(must, optional)
    if not v["ok"]:
        return {"ok": False, "validation": v,
                "message": "Анализ не запущен: заполните обязательные поля и исправьте ошибки."}
    th = _merge(DEFAULT_THRESHOLDS, thresholds) if thresholds is not None else load_thresholds(con)
    ref = db.load_reference(con)

    classes = parse_classes(must["class_code"])
    known = {r[0] for r in con.execute("SELECT code FROM classes").fetchall()}
    unknown = [c for c in classes if c not in known]
    if unknown:
        return {"ok": False, "validation": {"ok": False, "missing": [],
                                            "errors": {"class_code": "нет такого класса: " + ", ".join(unknown)}},
                "message": "Анализ не запущен: класс не найден в справочнике."}

    S = _to_float(must["sum_insured"])
    V = _to_float(must["object_value"])
    months = _to_float(must["term_months"])
    T = _term_days(months)
    factors, notes = engine_factors(classes, must, optional, th, as_of)
    if not any((c, must["object_type"]) in ref.base_rates for c in classes):
        notes.append(f"Тип объекта «{must['object_type']}» не найден в справочнике базовых ставок — "
                     f"движок взял среднюю ставку по классу")

    # ---------- расчёт по каждому классу отдельно (Положение 1882, п. 11) ----------
    parts = []
    for cls in classes:
        prod, pnote = _product_for(cls, must, ref)
        if pnote:
            notes.append(pnote)
        pname = (ref.products.get(prod) or {}).get("name", "")
        losses = optional.get("losses_3y")
        n_losses = _to_float(losses.get("count")) if isinstance(losses, dict) and _present(losses.get("count"))             else None
        # модель ставки (ТЗ 13.10.2026): убытки за 3 года — K_кл, сейсмозона — катастрофическая надбавка c
        inp = Input(product_code=prod or "", class_code=cls, object_type=must["object_type"],
                    value_amount=V, sum_insured=S, term_days=T, factors=dict(factors),
                    payer_type=optional.get("payer_type"), takaful=pname.startswith("Такафул"),
                    docs_received=list(optional.get("docs_received") or []),
                    losses_count=int(n_losses) if n_losses is not None else None,
                    seismic_zone=_seismic_zone(optional.get("seismic_zone")))
        # анализ риска: без проверок оформления договора (оплата премии, раскрытие) и без заявления-анкеты
        out = calculate(ref, inp, purpose=engine.PURPOSE_ANALYSIS)
        r = rate_for(ref, inp)
        parts.append({"class_code": cls, "product_code": prod, "product_name": pname, "inp": inp,
                      "calc": out, "rate": r})

    premium = sum(p["calc"]["premium"] for p in parts)
    tech = sum(p["calc"]["rates"]["technical_pct"] for p in parts)
    net = sum(p["calc"]["rates"]["net_pct"] for p in parts)
    mins = [p["calc"]["rates"]["min_pct"] for p in parts]
    min_total = sum(x for x in mins if x) if any(mins) else None
    applied = premium / (S * T / 365) * 100 if S else 0.0

    # ---------- краткий обзор ----------
    ratio = S / V
    if abs(ratio - 1) < 1e-9:
        itv = {"status": "ok", "title": "Страховая сумма равна стоимости", "detail": "Выплата будет полной.",
               "legal_ref": "ГК РУз, ст. 936, 938"}
    elif ratio < 1:
        itv = {"status": "warn", "title": "Недострахование",
               "detail": f"Сумма {ratio * 100:.0f}% стоимости. При убытке выплата составит ту же долю от ущерба: "
                         f"при ущербе 1 000 000 сум — {money(1_000_000 * ratio)} сум.",
               "legal_ref": "ГК РУз, ст. 936"}
    else:
        itv = {"status": "stop", "title": "Страховая сумма выше стоимости",
               "detail": f"Превышение {money(S - V)} сум: в этой части договор ничтожен, премия за превышение "
                         f"не возвращается.", "legal_ref": "ГК РУз, ст. 938"}
    summary = {
        "premium": round(premium), "sum_insured": S, "object_value": V,
        "ratio_sum_to_value": round(ratio, 4), "insurance_to_value": itv,
        "rate_applied_pct": round(applied, 4), "rate_technical_pct": round(tech, 4),
        "rate_net_pct": round(net, 4), "rate_min_pct": min_total,
        "term_months": months, "term_days": T, "region": must.get("region"),
        "object_type": must.get("object_type"), "class_code": "/".join(classes),
        "product_code": must.get("product_code") or None,
        "parts": [{"class_code": p["class_code"], "product_code": p["product_code"],
                   "product_name": p["product_name"], "premium": p["calc"]["premium"],
                   "rate_applied_pct": p["calc"]["rates"]["applied_pct"],
                   "rate_technical_pct": p["calc"]["rates"]["technical_pct"],
                   "rate_min_pct": p["calc"]["rates"]["min_pct"],
                   "min_source": p["calc"]["rates"]["min_source"],
                   "verdict": p["calc"]["verdict"], "checks": p["calc"]["checks"],
                   "explanation": p["calc"]["explanation"]} for p in parts],
        "rates_note": ("Ставка проверяется по каждому классу отдельно; ставки частей складываются, потому что "
                       "начислены на одну и ту же страховую сумму (Положение 1882, п. 11)"
                       if len(parts) > 1 else None),
        "notes": notes,
    }
    area = _to_float(optional.get("area_m2"))
    if area:
        summary["value_per_m2"] = round(V / area)

    # ---------- риски и драйверы ----------
    risks, factor_rows = [], []
    total_net_sum = sum(p["calc"]["rates"]["net_pct"] for p in parts) or 1.0
    for p in parts:
        inp, r = p["inp"], p["rate"]
        part_share = p["calc"]["rates"]["net_pct"] / total_net_sum
        base_prem = p["calc"]["premium"]
        applied_factors = []
        for f in factors_for(ref, inp):
            opt = ref.coefficients.get((f, inp.factors.get(f, "")))
            if not opt:
                continue
            applies = f != "seismic" or "earthquake" in r["included"]
            m = opt["multiplier"] if applies else 1.0
            without = {k: v2 for k, v2 in inp.factors.items() if k != f}
            alt = _with_factors(inp, without)
            rmr = r.get("rate_model") or {}
            if rmr.get("available") and f in ("loss_history", "seismic"):
                # модель ставки (ТЗ 13.10.2026): убытки — множитель K_кл, сейсмозона — надбавка c, а не коэффициенты
                if f == "loss_history":
                    alt.losses_count = None
                    m = rmr["k_client"]
                else:
                    alt.seismic_zone = None
            eff_sum = base_prem - _auto_premium(ref, alt)
            if rmr.get("available") and f == "seismic" and base_prem - eff_sum > 0:
                m = round(base_prem / (base_prem - eff_sum), 4)
            row = {"factor": opt["factor_name"], "factor_code": f, "option": opt["name"],
                   "option_code": inp.factors.get(f), "multiplier": m, "effect": _effect(m),
                   "premium_effect": round(eff_sum), "class_code": p["class_code"], "calibrated": 0}
            applied_factors.append(row)
            factor_rows.append(row)
        perils = {c: x for c, x in ref.perils.items() if x["class_code"] == p["class_code"]}
        inc = [c for c in r["included"] if c in perils]
        tot = sum(perils[c]["share"] for c in inc) or 1.0
        if perils:
            for c in sorted(inc, key=lambda c: -perils[c]["share"]):
                if perils[c]["share"] <= 0:
                    continue
                drv = [d for d in applied_factors
                       if d["factor_code"] not in FACTOR_PERILS or c in FACTOR_PERILS[d["factor_code"]]]
                risks.append({"code": c, "name": perils[c]["name"], "class_code": p["class_code"],
                              "catastrophic": perils[c]["cat"],
                              "share_of_net_pct": round(perils[c]["share"] / tot * part_share * 100, 1),
                              "drivers": [{k: d[k] for k in ("factor", "option", "multiplier", "effect")}
                                          for d in drv]})
        else:
            cname = con.execute("SELECT name FROM classes WHERE code=?", (p["class_code"],)).fetchone()
            risks.append({"code": f"class{p['class_code']}", "name": (cname[0] if cname else p["class_code"]),
                          "class_code": p["class_code"], "catastrophic": False,
                          "share_of_net_pct": round(part_share * 100, 1),
                          "drivers": [{k: d[k] for k in ("factor", "option", "multiplier", "effect")}
                                      for d in applied_factors]})
    risks.sort(key=lambda x: -x["share_of_net_pct"])
    top = sorted([d for d in factor_rows if d["multiplier"] > 1], key=lambda d: -d["premium_effect"])[:3]

    # ---------- что снижает премию ----------
    reducers = []
    for p in parts:
        inp = p["inp"]
        cur = p["calc"]["premium"]
        cur_opt = inp.factors.get("franchise", "f0")
        cur_m = (ref.coefficients.get(("franchise", cur_opt)) or {}).get("multiplier", 1.0)
        for opt in ("f05", "f1", "f2"):
            c = ref.coefficients.get(("franchise", opt))
            if not c or "franchise" not in factors_for(ref, inp) or c["multiplier"] >= cur_m:
                continue
            alt = _auto_premium(ref, _with_factors(inp, {**inp.factors, "franchise": opt}))
            if cur - alt > 1:
                pct = {"f05": 0.5, "f1": 1, "f2": 2}[opt]
                reducers.append({"kind": "франшиза", "class_code": p["class_code"],
                                 "text": f"Франшиза {c['name']} (≈ {money(S * pct / 100)} сум с каждого убытка)",
                                 "premium_delta": -round(cur - alt), "mandatory": False, "calibrated": 0})
        for t in p["calc"]["recommendations"]:
            if t["kind"] == "франшиза":
                continue
            reducers.append({"kind": t["kind"], "class_code": p["class_code"], "text": t["text"],
                             "premium_delta": t["premium_delta"], "mandatory": False, "calibrated": 0})
        for m in p["calc"]["preventive_measures"]:
            reducers.append({"kind": "мероприятие", "class_code": p["class_code"], "text": m["measure"],
                             "why": m["why"], "premium_delta": m["premium_delta"], "mandatory": m["mandatory"],
                             "deadline_days": m["deadline_days"], "calibrated": 0})
    reducers.sort(key=lambda x: (not x["mandatory"], x["premium_delta"] or 0))

    # ---------- сценарии ----------
    per_class = {}
    for cls in classes:
        if cls == "8":
            per_class[cls] = _scenarios_class8(S, V, must, optional, factors, th)
        elif cls == "9":
            per_class[cls] = _scenarios_class9(S, V, optional)
        elif cls in VEHICLE_CLASSES:
            per_class[cls] = _scenarios_vehicle(S, V, optional)
        else:
            per_class[cls] = _scenarios_generic(S, V, cls)

    retention = _retention(con, classes, S)
    scenarios = {}
    for s in ("PML", "EML", "MFL"):
        # одно событие затрагивает один класс: огонь — класс 8, кража — класс 9 — берём больший сценарий
        best_cls = max(per_class, key=lambda c: per_class[c][s]["amount"])
        sc = dict(per_class[best_cls][s])
        amount = sc["amount"]
        purpose = SCENARIO_PURPOSE[s]
        sc_assumptions = [{"text": a, "calibrated": 0} for a in sc.pop("assumptions")]
        if len(per_class) > 1:
            others = ", ".join(f"класс {c}: {money(per_class[c][s]['amount'])}" for c in per_class if c != best_cls)
            sc_assumptions.append({"text": f"Взят больший из сценариев классов (одно событие): класс {best_cls}; "
                                           f"{others}", "calibrated": 0})
        block = {"name": s, "title": SCENARIO_TITLE[s], "purpose": purpose,
                 "purpose_title": PURPOSE_TITLE[purpose], "class_code": best_cls,
                 "amount": round(amount), "pct_of_sum": round(amount / S * 100, 1),
                 **{k: (round(v2) if isinstance(v2, float) else v2) for k, v2 in sc.items() if k != "amount"},
                 "assumptions": sc_assumptions,
                 "level": _band_level(amount / S * 100, th["scenario_bands_pct"]),
                 "level_basis": "доля страховой суммы: " + ", ".join(
                     f"до {b}% — {n}" for b, n in zip(th["scenario_bands_pct"], LEVEL_NAMES)) + ", выше — Критический",
                 "calibrated": 0, "note": TERMS_NOTE}
        scenarios[s] = block

    for s, block in scenarios.items():
        if block["purpose"] == "tariff":
            block["tariff"] = _tariff_link(block, parts, S, T, premium, tech, net, applied)
        elif block["purpose"] == "retention":
            block["retention"] = _retention_link(block, retention, S)
        elif block["purpose"] == "catastrophe":
            block["catastrophe"] = _cat_link(con, block, retention, optional, classes, S)

    # ---------- рынок ----------
    mk = _market_block(market, parts, applied)

    # ---------- полнота данных ----------
    completeness = _completeness(must, optional, classes, th, assumptions)

    # ---------- внешняя статистика региона (stat.uz, data.egov.uz) ----------
    external = _external(con, classes, must.get("region"), th)

    # ---------- уровень риска ----------
    level = _level(th, parts, applied, tech, min_total, mk, scenarios, retention, optional, classes, S, V, premium, T,
                   external)

    return {
        "ok": True,
        "calibrated": 0,
        "completeness": completeness,
        "summary": summary,
        "risks": risks,
        "top_drivers": top,
        "reducers": reducers,
        "scenarios": scenarios,
        "scenario_purpose": dict(SCENARIO_PURPOSE),
        "retention": retention,
        "level": level,
        "market": mk,
        "external_stats": external,
        "assumptions": [{k: v for k, v in a.items()} for a in (assumptions or [])],
        # набор полей, на котором расчёт реально сделан (после apply_defaults быстрого режима):
        # повторный счёт (франшиза, варианты) обязан идти на этих же данных, иначе цифры разойдутся
        "inputs": {"must": dict(must), "optional": dict(optional)},
        "method": _method(),
        "thresholds_source": th.get("_source"),
    }


# ================================================================================================
# Связь сценариев с блоками
# ================================================================================================
def _tariff_link(block, parts, S, T, premium, tech, net, applied) -> dict:
    """Стандартный тариф: как сценарий соотносится с технической ставкой движка."""
    amount = block["amount"]
    exp_loss = net / 100 * S                           # ожидаемый убыток в год по нетто-ставке
    prob = exp_loss / amount if amount else None       # эквивалентная частота события размера сценария
    annual_prem = premium * 365 / T if T else premium
    return {
        "rate_technical_pct": round(tech, 4), "rate_net_pct": round(net, 4), "rate_applied_pct": round(applied, 4),
        "expected_annual_loss": round(exp_loss),
        "implied_event_probability_pct": round(prob * 100, 3) if prob else None,
        "implied_return_period_years": round(1 / prob) if prob else None,
        "rate_on_scenario_pct": round(annual_prem / amount * 100, 4) if amount else None,
        "formula": (f"ожидаемый убыток в год = нетто-ставка {num(net, 4)}% × {money(S)} = {money(exp_loss)} сум; "
                    f"частота события размера {block['name']} = {money(exp_loss)} / {money(amount)} = "
                    + (f"{num(prob * 100, 3)}% в год (в среднем раз в {years_ru(round(1 / prob))})" if prob else "—")
                    + f"; ставка к {block['name']} = годовая премия {money(annual_prem)} / {money(amount)} = "
                    + (f"{num(annual_prem / amount * 100, 4)}%" if amount else "—")),
        "explain": ("Техническая ставка движка начисляется на всю страховую сумму. Если сценарий меньше суммы, "
                    "та же ставка «на сценарий» выше: она показывает, как часто компания может позволить себе "
                    "убыток такого размера при этой цене. Раз в сотни лет — ставка покрывает сценарий с запасом; "
                    "чаще, чем раз в 20–30 лет — ставка для объекта низкая."),
        "calibrated": 0,
    }


def _retention(con, classes, S) -> dict:
    """Лимит 20% и таблица линий. Цифр компании нет — честно null."""
    c = cap.capacity(con)
    limit = c.get("limit_per_risk")
    if limit is None:
        return {"status": "нет данных компании", "limit_per_risk": None, "line_retention": None,
                "retention_limit": None, "line_class": None, "temporary": [],
                "legal_ref": "Положение № 1806, п. 15",
                "detail": "Нет собственных средств и резервов компании (company_financials / отчётность) — "
                          "лимит на один риск и удержание не считаются.",
                "data_request": c.get("data_request", [])}
    lines = cap.retention_table(con, limit)
    mine = [l for l in lines if l["class_code"] in classes]
    line = min(mine, key=lambda l: l["retention"]) if mine else None
    ret_limit = min(limit, line["retention"]) if line else limit
    temp = c.get("temporary") or []
    return {
        "status": "временно" if temp else "по отчётности",
        "own_funds": c.get("own_funds"), "reserves": c.get("reserves"),
        "limit_per_risk": round(limit), "line_retention": line["retention"] if line else None,
        "line_class": line["class_code"] if line else None,
        "line_net_rate_pct": line["net_rate_pct"] if line else None,
        "retention_limit": round(ret_limit), "temporary": temp,
        "sum_within_limit": S <= limit,
        "legal_ref": "Положение № 1806, п. 15",
        "formula": (f"лимит на один риск = 20% × ({money(c.get('own_funds'))} + {money(c.get('reserves'))}) = "
                    f"{money(limit)} сум; по таблице линий для класса {line['class_code']} "
                    f"(средняя нетто-ставка {num(line['net_rate_pct'], 3)}%) — {money(line['retention'])} сум; "
                    f"удержание не выше {money(ret_limit)} сум" if line else
                    f"лимит на один риск = 20% × (собственные средства + резервы) = {money(limit)} сум"),
        "detail": ("Внимание: " + "; ".join(temp) + ". Лимит и удержание — оценочные до отчётности компании."
                   if temp else "Цифры из отчётности компании."),
        "data_request": c.get("data_request", []),
    }


def _retention_link(block, ret, S) -> dict:
    amount = block["amount"]
    if ret["retention_limit"] is None:
        return {"status": ret["status"], "retention": None, "excess": None, "legal_ref": ret["legal_ref"],
                "formula": None, "explain": ret["detail"]}
    own = min(amount, ret["retention_limit"])
    excess = max(amount - ret["retention_limit"], 0)
    return {
        "status": ret["status"], "retention": round(own), "excess": round(excess),
        "limit_per_risk": ret["limit_per_risk"], "line_retention": ret["line_retention"],
        "sum_within_limit_20": ret["sum_within_limit"],
        "legal_ref": ret["legal_ref"],
        "formula": (f"удержание = min({block['name']} {money(amount)}; лимит 20% {money(ret['limit_per_risk'])}; "
                    f"таблица линий {money(ret['line_retention'])}) = {money(own)} сум"),
        "explain": (f"Компания может оставить на себе {money(own)} сум по этому риску. "
                    + (f"Сверх этого {money(excess)} сум — требует перестрахования (оценочно). " if excess else
                       "Сценарий укладывается в удержание. ")
                    + ("" if ret["sum_within_limit"] else
                       f"Страховая сумма {money(S)} выше лимита 20% на один риск — без перестрахования "
                       f"договор нарушает Положение 1806, п. 15. ")
                    + (ret["detail"] if ret["status"] == "временно" else "")),
        "calibrated": 0,
    }


def _cat_link(con, block, ret, optional, classes, S) -> dict:
    amount = block["amount"]
    zone = _seismic_zone(optional.get("seismic_zone"))
    acc = None
    if zone is not None and "8" in classes:
        try:
            r = con.execute("SELECT COUNT(*), COALESCE(SUM(sum_insured),0) FROM objects WHERE seismic_zone=?",
                            (zone,)).fetchone()
            acc = {"zone": zone, "objects_in_system": r[0], "sum_insured_in_system": r[1],
                   "with_this_object": r[1] + S,
                   "note": "по запросам в системе (не только заключённые договоры); выгрузки портфеля по зонам нет"}
        except Exception:
            acc = None
    base = {"status": ret["status"], "mfl": round(amount), "zone_accumulation": acc,
            "legal_ref": "Положение № 1806, п. 15, 16",
            "note": "Только аналитика: программа перестрахования вне проекта."}
    if ret["retention_limit"] is None:
        base.update({"retained": None, "reinsurance_need": None,
                     "explain": ret["detail"] + " Долю, требующую перестрахования, посчитать нельзя."})
        return base
    retained = min(amount, ret["retention_limit"])
    need = max(amount - ret["retention_limit"], 0)
    base.update({
        "retained": round(retained), "reinsurance_need": round(need),
        "reinsurance_share_pct": round(need / amount * 100, 1) if amount else 0.0,
        "formula": f"сверх удержания = {money(amount)} − {money(ret['retention_limit'])} = {money(need)} сум",
        "explain": (f"При катастрофе убыток до {money(amount)} сум; компания удерживает {money(retained)} сум, "
                    + (f"{money(need)} сум требует перестрахования (оценочно)." if need else
                       "перестрахование по сценарию не требуется (оценочно).")
                    + (" " + ret["detail"] if ret["status"] == "временно" else "")),
        "calibrated": 0,
    })
    return base


def _market_block(market, parts, applied) -> dict:
    if market:
        rate = _to_float(market.get("rate_pct") if market.get("rate_pct") is not None else market.get("avg_rate_pct"))
        return {"source": "передано (app/market_picture.py)", "data": market, "market_rate_pct": rate,
                "our_rate_pct": round(applied, 4),
                "deviation_pct": round((applied - rate) / rate * 100, 1) if rate else None}
    m = next((p["calc"]["market"] for p in parts if p["calc"]["market"]), None)
    if not m:
        return {"source": None, "data": None, "market_rate_pct": None, "our_rate_pct": round(applied, 4),
                "deviation_pct": None, "note": "рыночной статистики по классу нет"}
    m = {k: v for k, v in m.items() if k != "deviation_pct"}   # отклонение считаем по всему договору ниже
    rate = m["rate_pct"]
    out = {"source": f"НАПП (market_stats), {m.get('label', '')}", "data": m, "market_rate_pct": round(rate, 4),
           "our_rate_pct": round(applied, 4), "deviation_pct": round((applied - rate) / rate * 100, 1)}
    if m.get("row") == "cls8_9":
        out["note"] = "В отчётах НАПП классы 8 и 9 даны одной строкой — сравнение с общей ставкой «8, 9»"
    return out


def _completeness(must, optional, classes, th, assumptions: Optional[list] = None) -> dict:
    must_f = [f for f in FIELDS["must"] if f.get("required") and (f["key"] == "class_code" or _when_ok(f, classes))]
    opt_keys = []
    for f in FIELDS["optional"]:
        if _when_ok(f, classes) and f["key"] not in opt_keys:
            opt_keys.append(f["key"])
    filled = [k for k in opt_keys if _present(optional.get(k))]
    pct_opt = len(filled) / len(opt_keys) * 100 if opt_keys else 100.0
    lo, hi = th["confidence_bands_pct"]
    conf = "низкая" if pct_opt < lo else ("средняя" if pct_opt < hi else "высокая")
    if "8" in classes and not _present(optional.get("compartments")) and conf == "высокая":
        conf = "средняя"
    impact = IMPACT[_group(classes)]
    add = [{"key": k, "why": why} for k, w, why in sorted(impact, key=lambda x: -x[1])
           if k in opt_keys and k not in filled][:3]
    total = len(must_f) + len(opt_keys)
    assumed_keys = [a["key"] for a in (assumptions or []) if a.get("assumed")]
    # быстрый режим: подставленное значение не считается заполненным — полнота и уверенность ниже
    must_filled = sum(1 for f in must_f
                      if f["key"] not in assumed_keys and (_present(must.get(f["key"]))
                                                           or (f["key"] == "year" and must.get("year_unknown"))))
    if assumed_keys:
        order = ["низкая", "средняя", "высокая"]
        conf = order[max(0, order.index(conf) - (2 if len(assumed_keys) >= 3 else 1))]
        for k in assumed_keys:
            if not any(x["key"] == k for x in add):
                add.append({"key": k, "why": f"значение подставлено по умолчанию ({ASSUMED_NOTE})"})
    return {"must_filled": must_filled, "must_total": len(must_f),
            "must_assumed": len(assumed_keys), "assumed_keys": assumed_keys,
            "optional_filled": len(filled), "optional_total": len(opt_keys),
            "pct": round((must_filled + len(filled)) / total * 100) if total else 100,
            "confidence": conf, "what_to_add": add}


def _external(con, classes, region, th) -> dict:
    """Блок external_stats (app/risk_stats.py). Сбой чтения статистики не должен ронять анализ:
    тогда составляющая не учитывается, причина — в блоке."""
    try:
        from . import risk_stats
        return risk_stats.external_block(con, classes, region, th)
    except Exception as e:                                   # таблицы нет, набор не разобрался и т. п.
        return {"region": region, "classes": list(classes), "indicators": [], "not_found": {},
                "applicable": False, "points": None, "calibrated": 0,
                "why": "статистика региона не прочитана (%s: %s) — составляющая не учтена" % (type(e).__name__, e)}


def _level(th, parts, applied, tech, min_total, mk, scenarios, ret, optional, classes, S, V, premium, T,
           external: Optional[dict] = None) -> dict:
    W = th["weights"]
    comps = []

    def add(code, name, points, value, why, applicable=True):
        comps.append({"code": code, "name": name, "points": round(points, 1), "value": value,
                      "why": why, "applicable": applicable, "weight": W.get(code, 0)})

    # 1. техническая ставка к ориентиру (минимум компании или рынок — больший)
    refs = [x for x in (min_total, mk.get("market_rate_pct")) if x]
    if refs:
        ref_rate = max(refs)
        ratio = tech / ref_rate
        add("rate", "Техническая ставка к минимуму/рынку", _lin(ratio, *th["rate_ratio"]), round(ratio, 2),
            f"техническая {num(tech, 4)}% / ориентир {num(ref_rate, 4)}% = {num(ratio)}; "
            f"{num(th['rate_ratio'][0])} и ниже → 0 баллов, {num(th['rate_ratio'][1])} и выше → 100")
    else:
        add("rate", "Техническая ставка к минимуму/рынку", 0, None, "нет минимума и рынка — не учтено", False)
    # 2. MFL к удержанию
    mfl = scenarios["MFL"]["amount"]
    if ret.get("retention_limit"):
        r = mfl / ret["retention_limit"]
        add("mfl_retention", "MFL к лимиту удержания", _lin(r, *th["mfl_to_retention"]), round(r, 2),
            f"MFL {money(mfl)} / удержание {money(ret['retention_limit'])} = {num(r)}"
            + (" (цифры компании временные)" if ret["status"] == "временно" else ""))
    else:
        p = mfl / S * 100
        add("mfl_retention", "MFL к страховой сумме (нет данных компании)", _lin(p, *th["mfl_pct_of_sum"]),
            round(p, 1), f"данных компании нет — взят MFL {p:.0f}% страховой суммы")
    # 3. убытки за 3 года
    losses = optional.get("losses_3y") if isinstance(optional.get("losses_3y"), dict) else None
    if losses and _present(losses.get("count")):
        n = int(_to_float(losses.get("count")) or 0)
        pts = th["loss_count_points"][min(n, 2)]
        why = f"убытков за 3 года: {n} → {pts} баллов"
        amt = _to_float(losses.get("amount"))
        annual_prem = premium * 365 / T if T else premium
        if amt and annual_prem:
            lr = amt / 3 / annual_prem
            pts2 = _lin(lr, *th["loss_ratio"])
            why += f"; средний убыток в год / годовая премия = {num(lr)} → {pts2:.0f} баллов; взят больший"
            pts = max(pts, pts2)
        add("losses", "Убытки за 3 года", pts, n, why)
    else:
        add("losses", "Убытки за 3 года", th["unknown_points"], None,
            f"нет данных — средний балл {th['unknown_points']}")
    # 4. сумма к стоимости
    ratio = S / V
    if ratio > 1 + 1e-9:
        add("insurance_to_value", "Страховая сумма к стоимости", 100, round(ratio, 3),
            "сумма выше стоимости (ГК ст. 938) → 100 баллов")
    else:
        pts = _lin(1 - ratio, 0, 1 - th["underinsurance_full_at"])
        add("insurance_to_value", "Страховая сумма к стоимости", pts, round(ratio, 3),
            f"сумма {ratio * 100:.0f}% стоимости (ГК ст. 936); {th['underinsurance_full_at'] * 100:.0f}% и ниже → 100")
    # 5. сейсмика — только если землетрясение в покрытии (класс 8)
    if "8" in classes:
        z = _seismic_zone(optional.get("seismic_zone"))
        if z is not None:
            pts = th["seismic_points"].get(str(min(max(z, 6), 10)), th["unknown_points"])
            add("seismic", "Сейсмозона", pts, z, f"{z} баллов → {pts} баллов")
        else:
            add("seismic", "Сейсмозона", th["unknown_points"], None,
                f"не указана — средний балл {th['unknown_points']}")
    else:
        add("seismic", "Сейсмозона", 0, None, "землетрясение не входит в покрытие класса — не учтено", False)
    # 6. внешняя статистика региона: регион против республики по открытым данным (calibrated=0)
    ext = external or {}
    if ext.get("applicable"):
        add("external_stats", "Внешняя статистика региона", ext["points"], ext.get("avg_ratio"),
            ext.get("why", "") + " — экспертная шкала, calibrated=0")
    else:
        add("external_stats", "Внешняя статистика региона", 0, None,
            ext.get("why") or "нет данных внешней статистики — не учтено", False)
    comps[-1]["calibrated"] = 0
    comps[-1]["source"] = "stat.uz, data.egov.uz (блок external_stats)"

    active = [c for c in comps if c["applicable"] and c["weight"] > 0]
    wsum = sum(c["weight"] for c in active) or 1.0
    score = 0.0
    for c in comps:
        c["weight_norm"] = round(c["weight"] / wsum, 3) if c in active else 0.0
        c["contribution"] = round(c["points"] * c["weight"] / wsum, 1) if c in active else 0.0
        score += c["contribution"]
    score = round(min(100.0, max(0.0, score)), 1)
    name = _level_of(score, th["level_bounds"])
    override = None
    if any(ch["status"] == "stop" for p in parts for ch in p["calc"]["checks"]):
        need = th["stop_min_level"]
        if LEVEL_NAMES.index(name) < LEVEL_NAMES.index(need):
            stops = [ch["title"] for p in parts for ch in p["calc"]["checks"] if ch["status"] == "stop"]
            override = f"есть запрет в проверках ({'; '.join(stops)}) — уровень поднят до «{need}»"
            name = need
    lines = [f"{c['name']}: {c['points']:.0f} баллов × вес {num(c['weight_norm'])} = {num(c['contribution'], 1)}"
             f" ({c['why']})" for c in comps if c in active]
    lines += [f"{c['name']}: не учтено — {c['why']}" for c in comps if c not in active]
    b = th["level_bounds"]
    return {
        "level": name, "score": score, "components": comps, "override": override,
        "scale": [{"name": LEVEL_NAMES[i], "from": ([0] + b)[i], "to": (b + [100])[i]} for i in range(5)],
        "explanation": lines + ([override] if override else []) +
                       [f"Итого {num(score, 1)} из 100 → «{name}». Шкала экспертная (calibrated=0)."],
        "calibrated": 0,
    }


def _method() -> dict:
    return {
        "formulas": [
            "Ставка (движок): нетто = базовая ставка × доля включённых рисков × коэффициенты; "
            "+ рисковая надбавка 12% нетто; + катастрофическая 5% нетто (землетрясение, сель, оползень); "
            "÷ (1 − нагрузка); применённая = max(техническая, минимум продукта). Премия = ставка × сумма × дни / 365.",
            "Продукт из нескольких классов — отдельный расчёт по каждому классу, премии складываются.",
            "Пожар: наибольший противопожарный отсек × доля гибели (по защите, конструкции, горючести, "
            "удалённости пожарной части).",
            "Землетрясение: стоимость всей площадки × доля по сейсмозоне × конструкция × износ × этажность; "
            "отсеки от сейсмики не спасают.",
            "Сценарий = max(пожар, землетрясение) × (сумма / стоимость, не больше 1) + простой "
            "(прибыль и постоянные расходы в месяц × срок восстановления × доля).",
            "Класс 9: наибольшее помещение × доля (кража, вода, прочее) × поправка на защиту.",
            "Транспорт: PML 50% суммы, EML 75% (есть иммобилайзер или поиск) или 100%, MFL 100%.",
            "Лимит на один риск = 20% × (собственные средства + резервы); удержание = min(сценарий удержания, "
            "лимит, таблица линий класса).",
            "Сверх удержания по MFL — требует перестрахования (оценочно).",
            "Уровень риска = взвешенная сумма баллов 0–100 по составляющим; шкала в DEFAULT_THRESHOLDS.",
            "Внешняя статистика региона (вес 0,10 до нормировки, calibrated=0): по каждому показателю класса "
            "с разрезом по регионам — отношение «регион / республика» (на 1 000 жителей или коэффициент) → "
            "0 баллов при 0,5 и ниже, 100 при 1,5 и выше; балл составляющей — среднее. Нет показателей "
            "с регионами или регион не задан — составляющая не учитывается.",
        ],
        "legal_refs": LEGAL_REFS,
        "source_notes": SOURCE_NOTES,
        "scenario_purpose": dict(SCENARIO_PURPOSE),
        "terms_note": TERMS_NOTE,
        "calibrated": 0,
    }
