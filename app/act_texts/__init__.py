"""
Тексты сюрвейерского акта (лёгкая версия, ТЗ 2.0 от 29.09.2026) на трёх языках: ru, uz (латиница), en.

Акт собирает сервер по шаблонам из структурированных данных — поэтому цифры в тексте всегда
совпадают с расчётом. Здесь только слова; числа подставляются через money() и pct().
Термины — по docs/Терминология — ru-uz-en.md (sugʻurta summasi, sugʻurta mukofoti, franshiza, anderrayter).

Тексты разложены по темам (04.10.2026): sections, scenarios, market, docs, stats, rate, fork, below_min,
factors, misc — и склеиваются здесь в один словарь TX в прежнем порядке блоков. Функции t(), money(), pct()
не менялись. Проверка разбиения — tests/test_act_texts.py.
"""
LANGS = ("ru", "uz", "en")
NBSP = " "


def lang_of(lang) -> str:
    base = str(lang or "").strip().lower().replace("_", "-").split("-")[0]
    return base if base in LANGS else "ru"


def _num(x: float, lang: str, digits: int = 0) -> str:
    s = f"{x:,.{digits}f}"
    if lang == "en":
        return s
    # ru/uz: разряды неразрывным пробелом, дробная часть через запятую
    return s.replace(",", "\x00").replace(".", ",").replace("\x00", NBSP)


def money(x, lang: str = "ru") -> str:
    if x is None:
        return t("na", lang)
    cur = {"ru": "сум", "uz": "soʻm", "en": "UZS"}[lang_of(lang)]
    return _num(round(float(x)), lang_of(lang)) + NBSP + cur


def pct(x, lang: str = "ru", max_digits: int = 4) -> str:
    """Процент без лишних нулей, но не меньше двух знаков: 0,35 %, 1,5185 %, 20 % (целые — без дроби)."""
    if x is None:
        return t("na", lang)
    lang = lang_of(lang)
    v = round(float(x), max_digits)
    if v == int(v):
        s = str(int(v))
    else:
        s = f"{v:.{max_digits}f}".rstrip("0")
        if len(s.split(".")[1]) < 2:
            s = f"{v:.2f}"
        if lang != "en":
            s = s.replace(".", ",")
    return s + ("%" if lang == "en" else NBSP + "%")


def t(key: str, lang: str = "ru", **kw) -> str:
    row = TX.get(key)
    if row is None:
        return key
    s = row.get(lang_of(lang)) or row["ru"]
    return s.format(**kw) if kw else s


def label(group: dict, code: str, lang: str) -> str:
    row = group.get(code)
    if row is None:
        return str(code)
    return row.get(lang_of(lang)) or row["ru"]


def field_label(key: str, lang: str, obj_group: str = None) -> str:
    """Подпись поля с учётом вида объекта: год у зданий — «Год постройки», у техники — «Год выпуска»."""
    if key == "year" and obj_group in YEAR_BUILT_GROUPS:
        return YEAR_BUILT.get(lang_of(lang)) or YEAR_BUILT["ru"]
    return label(FIELD_LABELS, key, lang)


ROAD_VEH_GROUPS = ("car", "truck", "bus", "trailer", "moto")


def view_label(code: str, lang: str, veh_group: str = None) -> str:
    """Подпись ракурса; у дорожного транспорта (легковые, грузовые, автобусы) счётчик — «счётчик пробега»,
    без «моточасов» спецтехники (06.10.2026)."""
    if code == "odometer" and veh_group in ROAD_VEH_GROUPS:
        return t("view_odometer_road", lang)
    return label(VIEW_LABELS, code, lang)


def pct_fixed(x, lang: str = "ru", digits: int = 1) -> str:
    """Процент с заданным числом знаков после запятой — для рядом стоящих чисел (сценарии убытка)."""
    if x is None:
        return t("na", lang)
    lang = lang_of(lang)
    return _num(float(x), lang, digits) + ("%" if lang == "en" else NBSP + "%")


def plural_ru(n, forms: tuple) -> str:
    """Форма слова для числа по-русски: (1 балл, 2 балла, 5 баллов); дробное число — родительный ед. (2,5 балла)."""
    one, few, many = forms
    v = abs(float(n))
    if v != int(v):
        return few
    k = int(v)
    if k % 10 == 1 and k % 100 != 11:
        return one
    if 2 <= k % 10 <= 4 and not 12 <= k % 100 <= 14:
        return few
    return many


# слово после числа: ru — со склонением (plural_ru), uz — одно слово, en — ед./мн. число
COUNT_WORDS = {
    "points": {"ru": ("балл", "балла", "баллов"), "uz": "ball", "en": ("point", "points")},
}
# единицы показателей stat.uz / data.egov.uz, которые склоняются по числу (ключ — единица модуля)
UNIT_FORMS_RU = {
    "случаев": ("случай", "случая", "случаев"),
    "краж": ("кража", "кражи", "краж"),
    "преступлений": ("преступление", "преступления", "преступлений"),
    "человек": ("человек", "человека", "человек"),
    "штук": ("штука", "штуки", "штук"),
}


def count_text(n, word: str, lang: str = "ru", digits: int = 0) -> str:
    """«24 балла» / «24 ball» / «24 points» — число с нужной формой слова."""
    lang = lang_of(lang)
    forms = COUNT_WORDS[word][lang]
    num = _num(float(n), lang, digits)
    if lang == "ru":
        w = plural_ru(round(float(n), digits), forms)
    elif lang == "en":
        w = forms[0] if round(float(n), digits) == 1 else forms[1]
    else:
        w = forms
    return num + " " + w


# --------------------------------------------------------------------------- #
#  Тексты по темам: подписи — именами модулей, TX — склейкой блоков ниже
# --------------------------------------------------------------------------- #
from . import below_min, compact, docs, factors, fork, market, misc, rate, scenarios, sections, stats  # noqa: E402
from .docs import _ct_from_rq  # noqa: E402,F401
from .sections import (  # noqa: E402,F401
    FIELD_LABELS,
    GROUP_LABELS,
    LEGAL_REFS,
    LEVEL_LABELS,
    LOCATION_LABELS,
    OBJECT_KINDS,
    OTYPE_LABELS,
    SCORE_BAND_LABELS,
    SOURCE_IN,
    SOURCE_LABELS,
    VIEW_LABELS,
    YEAR_BUILT,
    YEAR_BUILT_GROUPS)
from .scenarios import (  # noqa: E402,F401
    FR_TYPE_LABELS,
    RA_VALUE_LABELS)
from .docs import (  # noqa: E402,F401
    BR_ROW_LABELS,
    CR_FIELD_LABELS,
    CR_SOURCE_LABELS,
    CR_SUBJECT_LABELS,
    CT_BLANK_LABELS,
    CT_ESSENTIAL_LABELS,
    CT_FIELD_LABELS,
    CT_SCHED_LABELS,
    CT_SOURCE_LABELS,
    DOC_KIND_LABELS,
    EDIT_LABELS,
    EXCLUSION_LABELS,
    PAYMENT_MODE_LABELS,
    RISK_LABELS,
    RQ_SOURCE_LABELS,
    X_KIND_LABELS,
    X_LABELS,
    _CT_REPL,
    _ESS_NOT_FOUND,
    _ESS_NOT_FOUND_CAP)
from .market import (  # noqa: E402,F401
    CLAIMS_CAVEATS,
    CLAIMS_CAVEAT_CAPITAL_NA,
    MV_VERDICT_LABELS,
    SITE_LABELS)
from .stats import (  # noqa: E402,F401
    AN_WHATIF_LABELS,
    LEVEL5_LABELS,
    OBJECT_SUBKINDS,
    PERIL_LABELS,
    PERIL_LEVEL_LABELS,
    SCORE_COMP_LABELS,
    STAT_CAVEATS,
    STAT_LABELS,
    STAT_NOT_FOUND,
    STAT_UNITS)
from .factors import (  # noqa: E402,F401
    AN_SOURCE_LABELS,
    FACTOR_LABELS,
    OPTION_LABELS)
from .fork import (  # noqa: E402,F401
    FORK_POS_LABELS,
    FORK_WHY_LABELS)
from .misc import (  # noqa: E402,F401
    VEH_FIELD_LABELS)

# Порядок блоков — как в прежнем едином файле: тексты сверки с договором (ct_*) выводятся из текстов
# запроса филиала (rq_*) сразу после их блока, и часть из них затем уточняется блоком договора.
TX = {}
for _block in (sections.TX_SECTIONS,
               scenarios.TX_SCENARIOS,
               market.TX_VALUATION,
               docs.TX_BRANCH_REQUEST):
    TX.update(_block)
docs.derive_ct(TX)
for _block in (docs.TX_CONTRACT,
               docs.TX_TERMS_SOURCE,
               stats.TX_ANALYTICS,
               rate.TX_PARTS,
               rate.TX_CONTROLLER,
               sections.TX_SCORING,
               fork.TX_FORK,
               market.TX_NAPP,
               below_min.TX_BELOW_MIN,
               factors.TX_FACTORS,
               market.TX_EXCHANGE,
               factors.TX_TERRITORY,
               misc.TX_OBJECTS,
               misc.TX_OBJECTS_SRC,
               misc.TX_PREFILL,
               compact.TX_COMPACT):
    TX.update(_block)
del _block
