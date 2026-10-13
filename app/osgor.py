"""
ОСГОР — обязательное страхование гражданской ответственности работодателя.
Продукт компании **1323**, класс 13. Режим ценообразования — «нормативный акт».

Два разных предмета, которые нельзя путать:

1. **Премия.** Считается строго по акту и никак иначе: ПР = СС × ТБ × КСТ / 100,
   ТБ = 0,1 % годовых, КСТ — коэффициент вида деятельности из классификации (934 позиции,
   20 категорий профессионального риска). Отклоняться от базовой ставки и коэффициентов
   страховщик не вправе (ЗРУ-210 ст. 8 ч. 4) — вилки ставки, франшизы, бонус-малуса в этом
   виде нет. Всё, что модуль делает с ценой, — подбирает КСТ, считает по формуле, применяет
   минимум 0,25 БРВ и пересчитывает на срок менее года.

2. **Оценка риска работодателя.** Нужна не для цены, а для решения «как принимать риск»:
   какие предупредительные мероприятия предписать, какие документы затребовать, нужно ли
   выносить договор на андеррайтера, как планировать резерв по ст. 22 ЗРУ-210. Право на оценку
   риска и на выдачу рекомендаций прямо дано страховщику (ЗРУ-210 ст. 18; п. 30 Правил).
   Отказать работодателю нельзя — договор публичный (ЗРУ-210 ст. 6 ч. 1; п. 11 Правил),
   поэтому решений всего три: принять / принять с мероприятиями / передать андеррайтеру.

Все числа модели убыточности — **экспертные** (`calibrated = 0`): разреза по частоте случаев и
по компаниям в отчётах НАПП нет, выгрузки компании по ОСГОР нет. Источник каждого числа указан
рядом с ним в EXPERT_SOURCES. Единственный фактический ориентир — убыточность рынка по ОСГОР из
`market_stats` (ряды `osgor`, `osgor_annuity`).

Нормы: ЗРУ-210 от 16.04.2009; Правила и тарифы — приложения № 1 и № 9 к ПКМ № 177 от 24.06.2009
(в ред. ПКМ № 458 от 30.07.2024, № 443 от 15.07.2025, № 709 от 07.11.2025); КоАО ст. 49².
Разбор актов — docs/ОСГОР — оценка риска.md.

Модуль работает и без базы: классификация читается из CSV
library/05_Методология/ОСГОР — классификация видов деятельности и коэффициенты КСТ (ПКМ 177, прил. 9).csv,
если таблицы osgor_activities ещё нет.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import csv
import sqlite3

ROOT = Path(__file__).resolve().parent.parent
ACTIVITIES_CSV = (ROOT / "library" / "05_Методология" /
                  "ОСГОР — классификация видов деятельности и коэффициенты КСТ (ПКМ 177, прил. 9).csv")
# library/ в репозиторий не входит (.gitignore); копия того же справочника (открытый акт, не ПД) — db/, она есть
# и в клоне, и в образе сервера (её же читает tools/db_build.py)
if not ACTIVITIES_CSV.exists():
    ACTIVITIES_CSV = ROOT / "db" / "osgor_activities.csv"

PRODUCT_CODE = "1323"
CLASS_CODE = "13"

# --- числа из нормативного акта (не экспертные, менять только вслед за актом) --------------------
BASE_RATE_PCT = 0.1          # ТБ, % годовых от страховой суммы — прил. № 9, разд. I, п. 1; ЗРУ-210 ст. 8
KST_NOT_LISTED = 3.400       # вид деятельности не найден в классификации — прил. № 9, разд. I, п. 6
MIN_PREMIUM_BRV = 0.25       # минимальная премия по годовому договору — п. 23 Правил (ред. ПКМ № 443)
NET_SHARE_PCT = 75.0         # нетто-ставка в брутто-ставке — прил. № 9, разд. II (ред. ПКМ № 458)
EXPENSE_SHARE_PCT = 25.0     # расходы на ведение дела
BURIAL_MAX_BRV = 3           # расходы на погребение — п. 43 Правил
YEAR_DAYS = 365
KST_MIN, KST_MAX = 0.571, 7.714
CATEGORIES = 20

ACTS = {
    "sum": "ЗРУ-210 ст. 10; п. 21 Правил (прил. № 1 к ПКМ № 177)",
    "formula": "Прил. № 9 к Правилам, разд. III, пп. 1–2",
    "base": "Прил. № 9 к Правилам, разд. I, п. 1; ЗРУ-210 ст. 8",
    "kst": "Прил. № 9 к Правилам, разд. I, п. 3 (ред. ПКМ № 443 от 15.07.2025)",
    "kst_default": "Прил. № 9 к Правилам, разд. I, п. 6",
    "min": "П. 23 Правил (ред. ПКМ № 443 от 15.07.2025)",
    "structure": "Прил. № 9 к Правилам, разд. II (ред. ПКМ № 458 от 30.07.2024)",
    "term": "ЗРУ-210 ст. 6 ч. 7; п. 9 Правил",
    "cover_start": "Форма договора (прил. № 1 к Правилам), п. 5; п. 26 Правил",
    "after_payout": "ЗРУ-210 ст. 10 ч. 3; п. 27 Правил",
    "payroll_change": "ЗРУ-210 ст. 10 ч. 2 (ред. ЗРУ-1154); п. 22 Правил",
    "public": "ЗРУ-210 ст. 6 ч. 1; п. 11 Правил (ред. ПКМ № 458)",
    "no_deviation": "ЗРУ-210 ст. 8 ч. 4",
    "survey_right": "ЗРУ-210 ст. 18; п. 30 Правил",
    "burial": "П. 43 Правил; форма договора, п. 2",
    "double": "ЗРУ-210 ст. 4 ч. 2 (ред. ЗРУ-1154 от 22.06.2026)",
    "annuity": "ЗРУ-210 ст. 14, ст. 15; пп. 49–50 Правил",
    "koao": "КоАО ст. 49² (ред. ЗРУ-586 от 03.12.2019)",
    "reserve": "ЗРУ-210 ст. 22",
}

# --- экспертные величины модели убыточности (все calibrated = 0) --------------------------------
# Ни одно из этих чисел не взято из статистики компании или из отчётов НАПП: разреза по частоте
# несчастных случаев и по компаниям в отчётности регулятора нет. Это рабочие ориентиры для
# решения «как принимать риск», их надо заменить на расчётные, как только появятся выгрузки.
EXPERT_SOURCES = {
    "base_frequency": "порядок величины: учтённые несчастные случаи на производстве в стране "
                      "к численности занятых по найму; в отчётах НАПП частоты нет",
    "severity_share": "ожидаемая доля тяжёлых и смертельных случаев принята по практике "
                      "расследования несчастных случаев (форма Н-1); данных компании нет",
    "modifiers": "множители условий труда и организации охраны труда — экспертные, "
                 "по составу факторов из ЗРУ-210 ст. 18 и аттестации рабочих мест",
    "credibility": "полное доверие собственной статистике при 5 ожидаемых случаях за 3 года "
                   "(частичное доверие по корню) — экспертный порог",
    "cap": "итоговое отклонение от рынка ограничено четырьмя рыночными убыточностями: "
           "при такой малой статистике более резкие выводы недостоверны",
    "anchor": "уровень убыточности задан рынком (отчёты НАПП), а не моделью: тариф установлен "
              "государством, поэтому средняя выплата выводится из рыночной убыточности и "
              "ожидаемой частоты, а не подбирается экспертно",
}
BASE_FREQ_AT_KST_DEFAULT = 0.0003   # случаев на одного работника в год при КСТ = 3,400
CREDIBILITY_FULL_CLAIMS = 5.0       # ожидаемых случаев за 3 года для полного доверия своей статистике
FREQ_REL_MIN, FREQ_REL_MAX = 0.3, 6.0   # пределы отношения «своя частота / ожидаемая»
TOTAL_FACTOR_MIN, TOTAL_FACTOR_MAX = 0.5, 4.0
HARMFUL_LOAD = 0.5                  # доля вредных условий: +50 % к убыточности при 100 %
DANGEROUS_LOAD = 1.0                # доля опасных условий: +100 % при 100 %
MGMT_LOADS = {                      # организация охраны труда
    "no_safety_service": (1.15, "нет службы (специалиста) по охране труда"),
    "no_training": (1.10, "нет обучения и проверки знаний по охране труда"),
    "no_ppe": (1.15, "средства индивидуальной защиты выдаются не всем / не по нормам"),
    "open_orders": (1.20, "есть невыполненные предписания инспекции труда"),
    "wage_arrears": (1.10, "задолженность по заработной плате"),
}
SEVERE_SHARE_BENCHMARK = 0.15       # ожидаемая доля тяжёлых и смертельных в общем числе случаев
LR_GREEN, LR_AMBER = 0.60, 1.00     # пороги решения по ожидаемой убыточности

# --- решения ------------------------------------------------------------------------------------
DECISION_ACCEPT = "принять"
DECISION_MEASURES = "принять с предупредительными мероприятиями"
DECISION_UNDERWRITER = "передать андеррайтеру"


# ================================================================================================
# 1. Классификация видов деятельности и подбор КСТ
# ================================================================================================
def _num(text: str) -> float:
    return float(str(text).replace(" ", "").replace(" ", "").replace(",", "."))


def load_activities(con: Optional[sqlite3.Connection] = None) -> list:
    """
    Классификация 934 видов деятельности (прил. № 9, разд. I, п. 3).
    Сначала таблица osgor_activities, если она уже есть в базе; иначе — CSV библиотеки.
    Возврат: [{"no","category","kst","okved","name"}].
    """
    if con is not None:
        try:
            rows = con.execute(
                "SELECT no, category, kst, okved, name FROM osgor_activities ORDER BY no"
            ).fetchall()
            if rows:
                return [{"no": int(r[0]), "category": int(r[1]), "kst": float(r[2]),
                         "okved": str(r[3] or ""), "name": str(r[4] or "")} for r in rows]
        except sqlite3.Error:
            pass  # таблицы ещё нет — разработчик добавит её следующим шагом
    out = []
    with open(ACTIVITIES_CSV, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f, delimiter=";"):
            out.append({
                "no": int(_num(row["№"])),
                "category": int(_num(row["категория_проф_риска"])),
                "kst": _num(row["КСТ"]),
                "okved": (row["код_ОКЭД_IFUT"] or "").strip(),
                "name": (row["вид_деятельности"] or "").strip(),
            })
    return out


def _norm(s: str) -> str:
    return " ".join(str(s or "").lower().replace("ё", "е").split())


def find_activity(okved: Optional[str] = None, name: Optional[str] = None,
                  activities: Optional[list] = None, con: Optional[sqlite3.Connection] = None) -> dict:
    """
    Подбор позиции классификации по коду ОКЭД (IFUT) или по наименованию вида деятельности.
    Возврат: {"kst","category","okved","name","matched_by","listed","explain","legal_ref"}.
    Код не найден → КСТ 3,400 (прил. № 9, разд. I, п. 6), listed = False.
    """
    acts = activities if activities is not None else load_activities(con)
    code = "".join(ch for ch in str(okved or "") if ch.isdigit())
    if code:
        for a in acts:
            if a["okved"] == code:
                return _matched(a, "код ОКЭД")
        # код мог быть дан подробнее, чем в перечне (например, 5 знаков против 4) или наоборот
        by_prefix = [a for a in acts if a["okved"] and (code.startswith(a["okved"]) or a["okved"].startswith(code))]
        if by_prefix:
            a = max(by_prefix, key=lambda x: (len(x["okved"]), x["kst"]))
            return _matched(a, f"код ОКЭД по совпадению начала ({code} → {a['okved']})")
    if name:
        n = _norm(name)
        for a in acts:
            if _norm(a["name"]) == n:
                return _matched(a, "наименование вида деятельности")
        found = [a for a in acts if n and (n in _norm(a["name"]) or _norm(a["name"]) in n)]
        if len(found) == 1:
            return _matched(found[0], "наименование (частичное совпадение)")
        if len(found) > 1:
            a = max(found, key=lambda x: x["kst"])
            return _matched(a, f"наименование (подошло {len(found)} позиций, взята наибольшая категория)")
    return {
        "kst": KST_NOT_LISTED, "category": None, "okved": code or "", "name": name or "",
        "matched_by": "вид деятельности в классификации не найден", "listed": False,
        "explain": f"Вид деятельности в классификации не найден, применён коэффициент {KST_NOT_LISTED:.3f}",
        "legal_ref": ACTS["kst_default"],
    }


def _matched(a: dict, how: str) -> dict:
    return {
        "kst": a["kst"], "category": a["category"], "okved": a["okved"], "name": a["name"],
        "matched_by": how, "listed": True,
        "explain": (f"Вид деятельности «{a['name']}» (ОКЭД {a['okved']}) — категория профессионального "
                    f"риска {a['category']} из {CATEGORIES}, коэффициент КСТ {a['kst']:.3f}"),
        "legal_ref": ACTS["kst"],
    }


def pick_kst(candidates: list, activities: Optional[list] = None,
             con: Optional[sqlite3.Connection] = None) -> dict:
    """
    Несколько видов деятельности у одного работодателя. В акте порядок выбора не описан
    (см. вопрос заказчику), поэтому здесь осознанно берётся **наибольший** КСТ — вариант,
    при котором премия не занижена, а покрытие не оспаривается. Решение помечается как
    «правило компании, а не нормы».
    """
    acts = activities if activities is not None else load_activities(con)
    found = [find_activity(okved=c.get("okved"), name=c.get("name"), activities=acts) for c in candidates]
    best = max(found, key=lambda x: x["kst"])
    if len(found) > 1:
        best = dict(best)
        best["explain"] += (f". У работодателя {len(found)} видов деятельности; в акте порядок выбора "
                            f"коэффициента при нескольких видах не описан — взят наибольший КСТ")
        best["all"] = found
        best["rule_source"] = "решение компании (в акте не урегулировано)"
    return best


# ================================================================================================
# 2. Страховая сумма и премия
# ================================================================================================
def insured_sum(payroll_12m: Optional[float] = None, first_month_payroll: Optional[float] = None,
                payroll_for_period: Optional[float] = None, situation: str = "работает больше года") -> dict:
    """
    Страховая сумма = годовой фонд оплаты труда всех работников (ЗРУ-210 ст. 10; п. 21 Правил).
    Лимита «на одного работника» в ОСГОР нет, БРВ и МРЗП как страховая сумма не применяются.
    """
    if situation == "начинает деятельность":
        if first_month_payroll is None:
            raise ValueError("для нового работодателя нужна зарплата всех работников за первый месяц")
        value, how = first_month_payroll * 12, "зарплата всех работников за первый месяц × 12"
    elif situation == "деятельность меньше года":
        if payroll_for_period is None:
            raise ValueError("нужен фонд оплаты труда за срок деятельности")
        value, how = payroll_for_period, "зарплата всех работников за срок деятельности"
    else:
        if payroll_12m is None:
            raise ValueError("нужен фонд оплаты труда за предыдущие 12 месяцев")
        value, how = payroll_12m, "зарплата всех работников за 12 месяцев до месяца заключения договора"
    return {"sum_insured": float(value), "how": how, "legal_ref": ACTS["sum"],
            "explain": f"Страховая сумма — {how}: {money(value)}"}


def money(x: float) -> str:
    """Сумма в сумах для текста агенту: 19 200 000,00 сум."""
    s = f"{x:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} сум"


BRV_NOT_PASSED_NOTE = "Размер БРВ не передан — минимальная премия 0,25 БРВ (п. 23 Правил) не проверена"
BRV_MISSING_NOTE = ("Размер БРВ на дату договора не введён администратором — минимальная премия "
                    "0,25 БРВ (п. 23 Правил) не проверена")

# --- тексты расчёта премии на трёх языках (мини-апп) ----------------------------------------------
# Русские строки — ровно те, что были до перевода (сверяются тестами побайтно).
# uz — латиница, апостроф ʻ (U+02BB) как на lex.uz; en — неофициальный перевод (LEGAL_NOTE_EN).
# Названия вида: uz IFJMS, en EL; слово «ОСГОР/OSGOR» в uz и en не используется.
PREMIUM_LANGS = ("ru", "uz", "en")
LEGAL_NOTE_EN = "English wording is an unofficial translation"

_LAW_UZ = "Oʻzbekiston Respublikasining 2009-yil 16-apreldagi OʻRQ-210-son Qonuni"
_A9_UZ = "VMQ № 177, IFJMS Qoidalari, 9-ilova"
_LAW_EN = "Law LRU-210 of 16.04.2009"
_A9_EN = "CM Resolution No. 177, EL Rules, Annex 9"
ACTS_I18N = {
    "uz": {
        "sum": f"{_LAW_UZ}, 10-modda; IFJMS Qoidalari, 21-band (VMQ № 177, 1-ilova)",
        "formula": f"{_A9_UZ}, III boʻlim, 1–2-bandlar",
        "base": f"{_A9_UZ}, I boʻlim, 1-band; {_LAW_UZ}, 8-modda",
        "kst": f"{_A9_UZ}, I boʻlim, 3-band (2025-yil 15-iyuldagi VMQ № 443 tahririda)",
        "kst_default": f"{_A9_UZ}, I boʻlim, 6-band",
        "min": "IFJMS Qoidalari, 23-band (2025-yil 15-iyuldagi VMQ № 443 tahririda)",
        "structure": f"{_A9_UZ}, II boʻlim (2024-yil 30-iyuldagi VMQ № 458 tahririda)",
        "no_deviation": f"{_LAW_UZ}, 8-modda, 4-qism",
        "burial": "IFJMS Qoidalari, 43-band; shartnoma shakli, 2-band",
    },
    "en": {
        "sum": f"{_LAW_EN}, art. 10; EL Rules, cl. 21 (CM Resolution No. 177, Annex 1)",
        "formula": f"{_A9_EN}, sec. III, cl. 1–2",
        "base": f"{_A9_EN}, sec. I, cl. 1; {_LAW_EN}, art. 8",
        "kst": f"{_A9_EN}, sec. I, cl. 3 (as amended by CM Resolution No. 443 of 15.07.2025)",
        "kst_default": f"{_A9_EN}, sec. I, cl. 6",
        "min": "EL Rules, cl. 23 (as amended by CM Resolution No. 443 of 15.07.2025)",
        "structure": f"{_A9_EN}, sec. II (as amended by CM Resolution No. 458 of 30.07.2024)",
        "no_deviation": f"{_LAW_EN}, art. 8(4)",
        "burial": "EL Rules, cl. 43; contract form, cl. 2",
    },
}

# Коды: шаги sum, activity_kst, kst, rate, year, term, min_year, final;
# примечания min_term, brv_not_passed, no_deviation; note — brv_missing.
PREMIUM_TEXTS = {
    "ru": {
        "sum.step": "страховая сумма", "sum.explain": "годовой фонд оплаты труда всех работников",
        "activity_kst.step": "вид деятельности и КСТ",
        "kst.step": "КСТ", "kst.explain": "коэффициент вида деятельности из классификации",
        "rate.step": "ставка к фонду оплаты труда", "rate.value": "{rate} % в год",
        "rate.explain": "базовая ставка {base} % × КСТ {kst}",
        "year.step": "премия за год", "year.explain": "{ss} × {base} × {kst} / 100",
        "term.step": "пересчёт на срок {days} дн.", "term.explain": "{year} / 365 × {days}",
        "min_year.step": "минимальная премия",
        "min_year.applied": "расчётная премия {calc} ниже минимума {minbrv} БРВ ({brv} × {minbrv}) — применён минимум",
        "min_year.not_applied": "расчётная премия выше минимума, минимум не применяется",
        "final.step": "премия к уплате",
        "final.explain": "нетто-часть {net} ({net_pct} %), расходы на ведение дела {expense} ({exp_pct} %)",
        "note.min_term": ("Минимум {minbrv} БРВ п. 23 Правил установлен для годового договора; "
                          "для договора на {days} дн. порядок применения минимума в акте не описан — "
                          "минимум не применён, вопрос вынесен заказчику "
                          "(для сведения: {minbrv} БРВ = {min})"),
        "note.brv_not_passed": BRV_NOT_PASSED_NOTE,
        "note.brv_missing": BRV_MISSING_NOTE,
        "note.no_deviation": ("Ставка и коэффициенты установлены нормативным актом; отклоняться от них "
                              "страховщик не вправе — скидок, надбавок и франшизы в ОСГОР нет ({ref})"),
        "brv_note": "Размер БРВ не введён — его вносит администратор",
    },
    "uz": {
        "sum.step": "sugʻurta summasi", "sum.explain": "barcha xodimlarning yillik mehnatga haq toʻlash fondi",
        "activity_kst.step": "faoliyat turi va KST",
        "activity.listed": ("IFUT {okved} boʻyicha faoliyat turi — kasbiy xavf toifasi {cat} (jami {n} ta), "
                            "KST koeffitsiyenti {kst}"),
        "activity.not_listed": "Faoliyat turi tasnifda topilmadi, {kst} koeffitsiyenti qoʻllanildi",
        "match.okved": "IFUT kodi", "match.prefix": "IFUT kodi boshlanishi boʻyicha ({code} → {okved})",
        "match.none": "faoliyat turi tasnifda topilmadi",
        "kst.step": "KST", "kst.explain": "tasnifdagi faoliyat turi koeffitsiyenti",
        "rate.step": "mehnatga haq toʻlash fondiga nisbatan stavka", "rate.value": "yiliga {rate} %",
        "rate.explain": "bazaviy stavka {base} % × KST {kst}",
        "year.step": "yillik mukofot", "year.explain": "{ss} × {base} × {kst} / 100",
        "term.step": "{days} kunlik muddatga qayta hisoblash", "term.explain": "{year} / 365 × {days}",
        "min_year.step": "minimal mukofot",
        "min_year.applied": ("hisoblangan mukofot {calc} minimal miqdordan ({minbrv} BHM = {brv} × {minbrv}) "
                             "past — minimal mukofot qoʻllanildi"),
        "min_year.not_applied": "hisoblangan mukofot minimal miqdordan yuqori, minimal mukofot qoʻllanilmaydi",
        "final.step": "toʻlanadigan mukofot",
        "final.explain": "netto qism {net} ({net_pct} %), ish yuritish xarajatlari {expense} ({exp_pct} %)",
        "note.min_term": ("IFJMS Qoidalarining 23-bandidagi {minbrv} BHM minimal miqdori yillik shartnoma uchun "
                          "belgilangan; {days} kunlik shartnomaga uni qoʻllash tartibi hujjatda koʻrsatilmagan — "
                          "minimal miqdor qoʻllanilmadi, masala buyurtmachiga yuborildi "
                          "(maʼlumot uchun: {minbrv} BHM = {min})"),
        "note.brv_not_passed": "BHM miqdori berilmagan — {minbrv} BHM minimal mukofot (IFJMS Qoidalari, 23-band) tekshirilmadi",
        "note.brv_missing": ("Shartnoma sanasiga BHM miqdori administrator tomonidan kiritilmagan — "
                             "{minbrv} BHM minimal mukofot (IFJMS Qoidalari, 23-band) tekshirilmadi"),
        "note.no_deviation": ("Stavka va koeffitsiyentlar normativ hujjat bilan belgilangan; sugʻurtalovchi "
                              "ulardan chetga chiqishga haqli emas — IFJMSda chegirma, ustama va franshiza yoʻq ({ref})"),
        "brv.request": "soʻrovda berilgan",
        "brv.reference": "administrator maʼlumotnomasi: {date} dan amalda ({source})",
        "brv_note": "BHM miqdori kiritilmagan — uni administrator kiritadi",
    },
    "en": {
        "sum.step": "sum insured", "sum.explain": "annual payroll of all employees",
        "activity_kst.step": "activity and KST",
        "activity.listed": "Activity under OKED {okved} — occupational risk category {cat} of {n}, KST coefficient {kst}",
        "activity.not_listed": "Activity not found in the classification; coefficient {kst} applied",
        "match.okved": "OKED code", "match.prefix": "OKED code by leading digits ({code} → {okved})",
        "match.none": "activity not found in the classification",
        "kst.step": "KST", "kst.explain": "activity coefficient from the classification",
        "rate.step": "rate to payroll", "rate.value": "{rate}% per year",
        "rate.explain": "base rate {base}% × KST {kst}",
        "year.step": "annual premium", "year.explain": "{ss} × {base} × {kst} / 100",
        "term.step": "pro rata for {days} days", "term.explain": "{year} / 365 × {days}",
        "min_year.step": "minimum premium",
        "min_year.applied": ("calculated premium {calc} is below the minimum of {minbrv} BCV "
                             "({brv} × {minbrv}) — minimum applied"),
        "min_year.not_applied": "calculated premium is above the minimum; minimum not applied",
        "final.step": "premium payable",
        "final.explain": "net part {net} ({net_pct}%), administrative expenses {expense} ({exp_pct}%)",
        "note.min_term": ("The minimum of {minbrv} BCV (EL Rules, cl. 23) is set for an annual contract; "
                          "the regulation does not say how to apply it to a {days}-day contract — the minimum "
                          "is not applied, the question has been referred to the client "
                          "(for reference: {minbrv} BCV = {min})"),
        "note.brv_not_passed": "The BCV amount was not provided — the minimum premium of {minbrv} BCV (EL Rules, cl. 23) was not checked",
        "note.brv_missing": ("The administrator has not entered the BCV for the contract date — the minimum "
                             "premium of {minbrv} BCV (EL Rules, cl. 23) was not checked"),
        "note.no_deviation": ("The rate and coefficients are set by regulation; the insurer may not deviate "
                              "from them — EL insurance has no discounts, loadings or deductibles ({ref})"),
        "brv.request": "provided in the request",
        "brv.reference": "administrator reference: in force from {date} ({source})",
        "brv_note": "The BCV amount is not entered — the administrator enters it",
    },
}


def premium_lang(lang: Optional[str]) -> str:
    """ru | uz | en; всё остальное — ru."""
    lang = str(lang or "").strip().lower()[:2]
    return lang if lang in PREMIUM_LANGS else "ru"


def act_ref(key: str, lang: str = "ru") -> str:
    """Ссылка на норму на языке ответа; перевода нет — русская (для uz/en таких ключей в расчёте нет)."""
    return ACTS_I18N.get(lang, {}).get(key) or ACTS[key]


def ptext(lang: str, key: str, **kw) -> str:
    return PREMIUM_TEXTS[lang][key].format(**kw)


def money_l(x: float, lang: str = "ru") -> str:
    """Деньги по локали: ru — как money(); uz «4 114 800 soʻm»; en «UZS 4,114,800». Тийины — если есть."""
    if lang == "ru":
        return money(x)
    x = round(float(x), 2)
    s = f"{x:,.0f}" if abs(x - round(x)) < 0.005 else f"{x:,.2f}"
    if lang == "uz":
        return s.replace(",", " ").replace(".", ",") + " soʻm"   # неразрывный пробел, как в money()
    return "UZS " + s


def num_l(x: float, lang: str = "ru", digits: Optional[int] = None) -> str:
    """Число в пояснении. ru — как было (точка); uz — запятая; en — точка."""
    s = str(x) if digits is None else f"{x:.{digits}f}"
    return s.replace(".", ",") if lang == "uz" else s


def activity_l(activity: dict, lang: str, code: str = "") -> dict:
    """Подбор КСТ на языке ответа: explain, matched_by, legal_ref. Название вида — из справочника как есть."""
    if lang == "ru":
        return activity
    a = dict(activity)
    k = num_l(a["kst"], lang, 3)
    if a.get("listed"):
        a["explain"] = ptext(lang, "activity.listed", okved=a.get("okved", ""), cat=a.get("category"),
                             n=CATEGORIES, kst=k)
        a["matched_by"] = (ptext(lang, "match.okved") if not code or a.get("okved") == code
                           else ptext(lang, "match.prefix", code=code, okved=a.get("okved", "")))
        a["legal_ref"] = act_ref("kst", lang)
    else:
        a["explain"] = ptext(lang, "activity.not_listed", kst=k)
        a["matched_by"] = ptext(lang, "match.none")
        a["legal_ref"] = act_ref("kst_default", lang)
    return a


def premium(sum_insured: float, kst: float, term_days: int = YEAR_DAYS,
            brv: Optional[float] = None, activity: Optional[dict] = None, lang: str = "ru") -> dict:
    """
    Премия строго по акту.

      договор на год:      ПР = СС × ТБ × КСТ / 100
      договор меньше года: ПР = СС × ТБ × КСТ / 100 / 365 × Д
      минимум по годовому договору: 0,25 БРВ (п. 23 Правил)

    Возврат: {"premium","premium_base","premium_term","min_premium","min_applied",
              "rate_pct_of_payroll","net","expense","steps"[], "notes"[]}.
    Каждый шаг в steps — {"шаг","значение","пояснение","норма"}.
    """
    if sum_insured is None or sum_insured < 0:
        raise ValueError("страховая сумма не задана")
    if not (KST_MIN <= kst <= KST_MAX or abs(kst - KST_NOT_LISTED) < 1e-9):
        raise ValueError(f"КСТ {kst} вне диапазона акта {KST_MIN}…{KST_MAX} и не равен {KST_NOT_LISTED}")
    if term_days <= 0 or term_days > YEAR_DAYS:
        raise ValueError("срок договора ОСГОР — от 1 дня до 365 дней (ЗРУ-210 ст. 6 ч. 7)")

    lang = premium_lang(lang)
    L = lambda key, **kw: ptext(lang, key, **kw)          # noqa: E731
    M = lambda x: money_l(x, lang)                        # noqa: E731
    ref = lambda key: act_ref(key, lang)                  # noqa: E731
    k3, base_s, minbrv = num_l(kst, lang, 3), num_l(BASE_RATE_PCT, lang), num_l(MIN_PREMIUM_BRV, lang)

    steps, notes, note_codes = [], [], []

    def step(code, title, value, explain, norm):
        steps.append({"шаг": title, "значение": value, "пояснение": explain, "норма": norm, "code": code})

    step("sum", L("sum.step"), M(sum_insured), L("sum.explain"), ref("sum"))
    if activity:
        act = activity_l(activity, lang, activity.get("okved", ""))
        step("activity_kst", L("activity_kst.step"), k3, act.get("explain", ""),
             act.get("legal_ref", ref("kst")))
    else:
        step("kst", L("kst.step"), k3, L("kst.explain"), ref("kst"))

    rate_pct = BASE_RATE_PCT * kst           # ставка в % от ФОТ за год
    base = sum_insured * BASE_RATE_PCT * kst / 100.0
    step("rate", L("rate.step"), L("rate.value", rate=num_l(rate_pct, lang, 4)),
         L("rate.explain", base=base_s, kst=k3), ref("base"))
    step("year", L("year.step"), M(base), L("year.explain", ss=M(sum_insured), base=base_s, kst=k3), ref("formula"))

    term_premium = base
    if term_days != YEAR_DAYS:
        term_premium = base / YEAR_DAYS * term_days
        step("term", L("term.step", days=term_days), M(term_premium),
             L("term.explain", year=M(base), days=term_days), ref("formula"))

    min_premium, min_applied = None, False
    if brv:
        min_premium = MIN_PREMIUM_BRV * brv
        if term_days == YEAR_DAYS:
            if term_premium < min_premium:
                min_applied = True
                step("min_year", L("min_year.step"), M(min_premium),
                     L("min_year.applied", calc=M(term_premium), minbrv=minbrv, brv=M(brv)), ref("min"))
            else:
                step("min_year", L("min_year.step"), M(min_premium), L("min_year.not_applied"), ref("min"))
        else:
            notes.append(L("note.min_term", minbrv=minbrv, days=term_days, min=M(min_premium)))
            note_codes.append("min_term")
    else:
        notes.append(L("note.brv_not_passed", minbrv=minbrv))
        note_codes.append("brv_not_passed")

    final = max(term_premium, min_premium) if min_applied else term_premium
    net = final * NET_SHARE_PCT / 100.0
    expense = final * EXPENSE_SHARE_PCT / 100.0
    step("final", L("final.step"), M(final),
         L("final.explain", net=M(net), net_pct=f"{NET_SHARE_PCT:.0f}", expense=M(expense),
           exp_pct=f"{EXPENSE_SHARE_PCT:.0f}"), ref("structure"))
    notes.append(L("note.no_deviation", ref=ref("no_deviation")))
    note_codes.append("no_deviation")
    return {
        "premium": final, "premium_base": base, "premium_term": term_premium,
        "min_premium": min_premium, "min_applied": min_applied,
        "rate_pct_of_payroll": rate_pct, "kst": kst, "term_days": term_days,
        "sum_insured": float(sum_insured),
        "net": net, "expense": expense, "steps": steps, "notes": notes, "note_codes": note_codes,
    }


def premium_after_payout(sum_insured: float, kst: float, paid_amount: float,
                         days_left: int, previous_payouts: float = 0.0) -> dict:
    """
    После выплаты страховая сумма уменьшается на выплаченное возмещение, а работодатель
    в течение 7 рабочих дней доплачивает премию пропорционально выплате и остатку срока
    (ЗРУ-210 ст. 10 ч. 3; п. 27 Правил). Покрытие «восстанавливается» только за доплату.
    """
    remaining_before = max(sum_insured - previous_payouts, 0.0)
    paid = min(paid_amount, remaining_before)
    restored = paid                                   # восстанавливаемая часть страховой суммы
    extra = restored * BASE_RATE_PCT * kst / 100.0 / YEAR_DAYS * max(days_left, 0)
    remaining_after = max(remaining_before - paid, 0.0)
    steps = [
        {"шаг": "остаток страховой суммы до выплаты", "значение": money(remaining_before),
         "пояснение": f"страховая сумма {money(sum_insured)} минус ранее выплаченное "
                      f"{money(previous_payouts)}", "норма": ACTS["after_payout"]},
        {"шаг": "выплата", "значение": money(paid),
         "пояснение": "в пределах остатка страховой суммы (ЗРУ-210 ст. 12; п. 41 Правил)",
         "норма": ACTS["after_payout"]},
        {"шаг": "остаток страховой суммы после выплаты", "значение": money(remaining_after),
         "пояснение": "покрытие уменьшилось на выплаченное возмещение", "норма": ACTS["after_payout"]},
        {"шаг": "доплата премии", "значение": money(extra),
         "пояснение": f"{money(restored)} × {BASE_RATE_PCT} × {kst:.3f} / 100 / 365 × {days_left} дн. "
                      f"— срок уплаты 7 рабочих дней", "норма": ACTS["after_payout"]},
    ]
    return {"paid": paid, "remaining_before": remaining_before, "remaining_after": remaining_after,
            "extra_premium": extra, "days_left": days_left, "deadline_working_days": 7, "steps": steps}


def premium_after_payroll_change(old_sum: float, new_sum: float, kst: float, days_left: int) -> dict:
    """
    Изменился фонд оплаты труда — письменное допсоглашение, премия пересчитывается
    пропорционально оставшемуся периоду и по тарифу на дату заключения договора
    (ЗРУ-210 ст. 10 ч. 2 в ред. ЗРУ-1154; п. 22 Правил). Уведомить — 5 рабочих дней.
    """
    delta = new_sum - old_sum
    extra = delta * BASE_RATE_PCT * kst / 100.0 / YEAR_DAYS * max(days_left, 0)
    return {
        "delta_sum": delta, "extra_premium": extra, "days_left": days_left,
        "direction": "доплата" if extra >= 0 else "возврат",
        "legal_ref": ACTS["payroll_change"],
        "steps": [{"шаг": "изменение страховой суммы", "значение": money(delta),
                   "пояснение": f"было {money(old_sum)}, стало {money(new_sum)}",
                   "норма": ACTS["payroll_change"]},
                  {"шаг": "перерасчёт премии", "значение": money(extra),
                   "пояснение": f"{money(delta)} × {BASE_RATE_PCT} × {kst:.3f} / 100 / 365 × {days_left} дн.",
                   "норма": ACTS["payroll_change"]}],
    }


# ================================================================================================
# 3. Ориентир по рынку (market_stats, ряды osgor и osgor_annuity)
# ================================================================================================
def market_reference(con: Optional[sqlite3.Connection] = None, row_key: str = "osgor") -> dict:
    """
    Убыточность рынка по ОСГОР из отчётов НАПП. Берутся годовые срезы (01.01), то есть итог
    завершённого года. Разреза по компаниям и по частоте случаев в отчётности регулятора нет —
    ничего сверх этого модуль о рынке не утверждает.
    """
    empty = {"loss_ratio": None, "years": [], "source": "отчёты НАПП, таблица market_stats",
             "note": "данных нет"}
    if con is None:
        return empty
    try:
        rows = con.execute(
            "SELECT report_date, premiums_ytd, payouts_ytd, liabilities, source_file FROM market_stats "
            "WHERE row_key = ? AND report_date LIKE '%-01-01' ORDER BY report_date DESC", (row_key,)
        ).fetchall()
    except sqlite3.Error:
        return empty
    years = []
    for r in rows:
        prem, pay = r[1] or 0.0, r[2] or 0.0
        years.append({"as_of": r[0], "year": int(r[0][:4]) - 1,
                      "premiums_mln": prem, "payouts_mln": pay,
                      "loss_ratio": (pay / prem) if prem else None, "source_file": r[4]})
    if not years:
        return empty
    return {"loss_ratio": years[0]["loss_ratio"], "year": years[0]["year"], "years": years,
            "source": "отчёты НАПП (лист 1.4), таблица market_stats, ряд " + row_key,
            "note": "по компаниям и по частоте случаев разреза в отчётах НАПП нет"}


MARKET_LR_FALLBACK = 0.474   # итог 2025 года по ОСГОР: 135 434,461 / 285 879,358 млн сум


# ================================================================================================
# 4. Оценка риска работодателя
# ================================================================================================
@dataclass
class Employer:
    """Анкета работодателя для оценки риска (не для цены — цену меняет только акт)."""
    name: str = ""
    okved: str = ""
    activity_name: str = ""
    headcount: int = 0                      # численность работников
    payroll_12m: float = 0.0                # годовой ФОТ всех работников = страховая сумма
    share_harmful: float = 0.0              # доля работников во вредных условиях по аттестации, 0…1
    share_dangerous: float = 0.0            # доля работников в опасных условиях, 0…1
    attestation_done: bool = True           # аттестация рабочих мест проведена
    accidents_3y: int = 0                   # несчастных случаев за 3 года (все)
    severe_3y: int = 0                      # из них тяжёлых
    fatal_3y: int = 0                       # из них смертельных
    payouts_3y: float = 0.0                 # выплачено/возмещено за 3 года, сум (если известно)
    safety_service: bool = True             # служба или специалист по охране труда
    training_done: bool = True              # обучение и проверка знаний по охране труда
    ppe_provided: bool = True               # СИЗ по нормам
    labour_inspection_orders: int = 0       # невыполненные предписания инспекции труда
    wage_arrears: bool = False              # задолженность по заработной плате
    budget_funded: bool = False             # финансируется из бюджета (ЗРУ-210 ст. 4 ч. 2)
    state_insured_staff: bool = False       # есть работники с обязательным госстрахованием жизни и здоровья
    term_days: int = YEAR_DAYS
    activity_shorter_than_year: bool = False
    premium_paid: bool = False
    burial_amount: Optional[float] = None   # расходы на погребение, указанные в договоре
    docs_received: list = field(default_factory=list)
    notes: str = ""


def risk_assessment(emp: Employer, kst: float, premium_amount: float,
                    market: Optional[dict] = None) -> dict:
    """
    Ожидаемая убыточность договора. Уровень задаёт рынок (убыточность ОСГОР из отчётов НАПП),
    а модель только отклоняет его от рынка по свойствам конкретного работодателя:

        ожидаемая убыточность = убыточность рынка × множитель, где
        множитель = [ доверие × (своя частота / ожидаемая) + (1 − доверие) × (условия труда ×
                      организация охраны труда) ] × тяжесть

    Собственная частота уже вбирает в себя и условия труда, и порядок на производстве, поэтому
    априорные множители применяются только к той части, которой собственная статистика не покрывает
    (иначе одно и то же считалось бы дважды). Итоговый множитель ограничен сверху и снизу.

    Отдельно даётся тот же результат в деньгах: ожидаемые выплаты за год, ожидаемое число случаев
    и вытекающая из них средняя выплата — чтобы андеррайтер мог проверить правдоподобие.
    Выплаты ограничены страховой суммой (ЗРУ-210 ст. 12; п. 41 Правил).

    Все множители экспертные (calibrated = 0), источники — в EXPERT_SOURCES.
    """
    market = market or {}
    market_lr = market.get("loss_ratio") or MARKET_LR_FALLBACK
    headcount = max(int(emp.headcount or 0), 0)
    payroll = float(emp.payroll_12m or 0.0)
    avg_salary = (payroll / headcount) if headcount else 0.0

    # --- частота ---------------------------------------------------------------------------
    exp_freq = BASE_FREQ_AT_KST_DEFAULT * (kst / KST_NOT_LISTED)      # ожидаемая частота по категории
    exposure = headcount * 3                                          # человеко-лет за 3 года
    own_freq = (emp.accidents_3y / exposure) if exposure else None
    expected_claims_3y = exp_freq * exposure
    z = min(1.0, (expected_claims_3y / CREDIBILITY_FULL_CLAIMS) ** 0.5) if expected_claims_3y > 0 else 0.0
    if own_freq is None or exp_freq <= 0:
        freq_rel, z = 1.0, 0.0
    else:
        freq_rel = min(max(own_freq / exp_freq, FREQ_REL_MIN), FREQ_REL_MAX)

    # --- условия труда ---------------------------------------------------------------------
    cond_factor = 1.0 + HARMFUL_LOAD * float(emp.share_harmful or 0) + \
        DANGEROUS_LOAD * float(emp.share_dangerous or 0)
    if not emp.attestation_done:
        cond_factor *= 1.10   # аттестация не проведена — доли условий труда не подтверждены

    # --- организация охраны труда ------------------------------------------------------------
    mgmt_factor, mgmt_items = 1.0, []
    flags = {
        "no_safety_service": not emp.safety_service,
        "no_training": not emp.training_done,
        "no_ppe": not emp.ppe_provided,
        "open_orders": (emp.labour_inspection_orders or 0) > 0,
        "wage_arrears": bool(emp.wage_arrears),
    }
    for key, on in flags.items():
        mult, text = MGMT_LOADS[key]
        if on:
            mgmt_factor *= mult
            mgmt_items.append({"фактор": text, "множитель": mult})

    # --- тяжесть ------------------------------------------------------------------------------
    severe_share = ((emp.severe_3y + emp.fatal_3y) / emp.accidents_3y) if emp.accidents_3y else None
    if severe_share is None:
        sev_factor = 1.0
    else:
        sev_factor = 1.0 + 0.5 * (severe_share - SEVERE_SHARE_BENCHMARK) / SEVERE_SHARE_BENCHMARK
        sev_factor = min(max(sev_factor, 0.8), 1.6)

    # --- сборка множителя ----------------------------------------------------------------------
    a_priori = cond_factor * mgmt_factor
    blended = z * freq_rel + (1 - z) * a_priori           # своя статистика против априорных признаков
    total = blended * sev_factor
    capped = min(max(total, TOTAL_FACTOR_MIN), TOTAL_FACTOR_MAX)
    lr_top = market_lr * capped

    # --- то же в деньгах -------------------------------------------------------------------------
    expected_loss = min(lr_top * premium_amount, payroll) if premium_amount else 0.0
    claims_year = exp_freq * blended * headcount          # ожидаемое число случаев в год
    implied_claim = (expected_loss / claims_year) if claims_year > 0 else None

    lines = [
        f"Ориентир рынка по ОСГОР: убыточность {market_lr * 100:.1f} %"
        + (f" (итог {market['year']} года, отчёты НАПП)" if market.get("year") else " (итог 2025 года, отчёты НАПП)"),
        f"Собственная частота: {emp.accidents_3y} случ. за 3 года на {headcount} работников "
        f"= {(own_freq or 0) * 1000:.2f} на 1000 работников в год; "
        f"ожидаемая по категории (КСТ {kst:.3f}) — {exp_freq * 1000:.2f} на 1000; "
        f"отношение {freq_rel:.2f}, доверие собственным данным {z * 100:.0f} %",
        f"Условия труда: вредные {float(emp.share_harmful or 0) * 100:.0f} %, "
        f"опасные {float(emp.share_dangerous or 0) * 100:.0f} %"
        + ("" if emp.attestation_done else ", аттестация рабочих мест не проведена")
        + f" → множитель {cond_factor:.2f}",
        ("Организация охраны труда: " + "; ".join(i["фактор"] for i in mgmt_items)
         + f" → множитель {mgmt_factor:.2f}") if mgmt_items
        else "Организация охраны труда: замечаний нет → множитель 1,00",
        (f"Тяжесть: тяжёлых и смертельных {severe_share * 100:.0f} % от всех случаев "
         f"(ориентир {SEVERE_SHARE_BENCHMARK * 100:.0f} %) → множитель {sev_factor:.2f}")
        if severe_share is not None else "Тяжесть: случаев не было, множитель 1,00",
        f"Множитель к рынку: {z * 100:.0f} % × {freq_rel:.2f} (своя частота) + "
        f"{(1 - z) * 100:.0f} % × {a_priori:.2f} (условия и охрана труда) = {blended:.2f}; "
        f"× тяжесть {sev_factor:.2f} = {total:.2f}"
        + (f" → ограничен {capped:.2f}" if abs(capped - total) > 1e-9 else ""),
        f"Ожидаемая убыточность договора: {market_lr * 100:.1f} % × {capped:.2f} = {lr_top * 100:.1f} %",
        f"В деньгах: премия {money(premium_amount)} → ожидаемые выплаты {money(expected_loss)} в год; "
        f"ожидается {claims_year:.2f} случ. в год"
        + (f", то есть около {money(implied_claim)} на случай "
           f"(средняя годовая зарплата работника {money(avg_salary)}) — проверьте правдоподобие"
           if implied_claim else ""),
    ]
    return {
        "market_loss_ratio": market_lr,
        "expected_loss_ratio": lr_top,
        "total_factor": capped,
        "expected_loss_amount": expected_loss,
        "expected_claims_per_year": claims_year,
        "implied_average_claim": implied_claim,
        "average_salary": avg_salary,
        "own_frequency": own_freq,
        "expected_frequency": exp_freq,
        "credibility": z,
        "factors": {"своя частота (отношение)": freq_rel, "условия труда": cond_factor,
                    "охрана труда": mgmt_factor, "тяжесть": sev_factor,
                    "итоговый множитель": capped},
        "mgmt_items": mgmt_items,
        "calibrated": 0,
        "sources": EXPERT_SOURCES,
        "explain": lines,
        "legal_ref": ACTS["survey_right"],
    }


# ================================================================================================
# 5. Проверки OSGOR-01 … OSGOR-13
# ================================================================================================
def checks(emp: Employer, activity: dict, prem: dict, brv: Optional[float] = None,
           declared_sum: Optional[float] = None) -> list:
    """
    Правила из раздела 8 заметки юриста. Возврат: [{"code","severity","text","legal_ref"}].
    severity: 'стоп' | 'предупреждение' | 'подсказка'.
    """
    out = []

    def add(code, severity, text, ref):
        out.append({"code": code, "severity": severity, "text": text, "legal_ref": ref})

    ss = prem["sum_insured"]
    # OSGOR-01 страховая сумма = годовой ФОТ
    if declared_sum is not None and abs(declared_sum - ss) > 0.5:
        add("OSGOR-01", "стоп",
            f"Страховая сумма в заявлении {money(declared_sum)} не равна годовому фонду оплаты труда "
            f"{money(ss)}. Страховая сумма по ОСГОР — это годовой ФОТ всех работников, "
            f"а не лимит на человека", ACTS["sum"])
    else:
        add("OSGOR-01", "подсказка",
            f"Страховая сумма — годовой фонд оплаты труда всех работников: {money(ss)}", ACTS["sum"])

    # OSGOR-02 тариф только по формуле акта
    add("OSGOR-02", "подсказка",
        f"Ставка установлена ПКМ № 177: базовая {BASE_RATE_PCT} % × коэффициент вида деятельности "
        f"{prem['kst']:.3f} = {prem['rate_pct_of_payroll']:.4f} % от ФОТ. Своей вилки у страховщика нет",
        ACTS["no_deviation"])

    # OSGOR-03 / OSGOR-04 КСТ из классификации
    if activity.get("listed"):
        add("OSGOR-03", "подсказка",
            f"{activity['explain']} (подбор: {activity['matched_by']})", ACTS["kst"])
    else:
        add("OSGOR-04", "подсказка",
            f"Вид деятельности в классификации не найден, применён средний коэффициент "
            f"{KST_NOT_LISTED:.3f}. Проверьте код ОКЭД в заявлении", ACTS["kst_default"])
    if activity.get("all") and len(activity["all"]) > 1:
        add("OSGOR-03", "предупреждение",
            "У работодателя несколько видов деятельности; в акте порядок выбора коэффициента "
            "не описан — применён наибольший КСТ. Проверьте фактический основной вид деятельности",
            ACTS["kst"])

    # OSGOR-05 минимальная премия
    if brv:
        if prem["min_applied"]:
            add("OSGOR-05", "стоп",
                f"Расчётная премия {money(prem['premium_term'])} ниже минимума по годовому договору "
                f"{MIN_PREMIUM_BRV} БРВ = {money(prem['min_premium'])}; к уплате {money(prem['premium'])}",
                ACTS["min"])
        elif prem["term_days"] != YEAR_DAYS:
            add("OSGOR-05", "подсказка",
                f"Минимум {MIN_PREMIUM_BRV} БРВ установлен для годового договора; порядок применения "
                f"к договору на {prem['term_days']} дн. в акте не описан — минимум не применён", ACTS["min"])
    else:
        add("OSGOR-05", "предупреждение",
            "Размер БРВ не задан — минимальная премия 0,25 БРВ не проверена", ACTS["min"])

    # OSGOR-06 срок договора
    if prem["term_days"] == YEAR_DAYS:
        add("OSGOR-06", "подсказка", "Договор заключается на один год", ACTS["term"])
    elif emp.activity_shorter_than_year:
        add("OSGOR-06", "подсказка",
            f"Срок договора {prem['term_days']} дн. — по сроку деятельности работодателя", ACTS["term"])
    else:
        add("OSGOR-06", "стоп",
            f"Договор ОСГОР заключается на год; срок {prem['term_days']} дн. допустим, только если "
            f"деятельность работодателя короче года — подтвердите документами", ACTS["term"])

    # OSGOR-07 покрытие после оплаты
    add("OSGOR-07", "предупреждение" if not emp.premium_paid else "подсказка",
        "Ответственность страховщика начинается с 00:00 дня, следующего за днём поступления премии "
        "на расчётный счёт; премия уплачивается единовременно в течение 5 рабочих дней",
        ACTS["cover_start"])

    # OSGOR-08 погребение
    if brv:
        limit = BURIAL_MAX_BRV * brv
        if emp.burial_amount is None:
            add("OSGOR-08", "предупреждение",
                f"В договоре указать расходы на погребение отдельной суммой, не более "
                f"{BURIAL_MAX_BRV} БРВ = {money(limit)}", ACTS["burial"])
        elif emp.burial_amount > limit + 0.5:
            add("OSGOR-08", "стоп",
                f"Расходы на погребение {money(emp.burial_amount)} выше предела "
                f"{BURIAL_MAX_BRV} БРВ = {money(limit)}", ACTS["burial"])
    else:
        add("OSGOR-08", "предупреждение",
            f"Расходы на погребение указываются отдельной строкой, не более {BURIAL_MAX_BRV} БРВ",
            ACTS["burial"])

    # OSGOR-09 после выплаты
    add("OSGOR-09", "предупреждение",
        "После выплаты возмещения страховая сумма уменьшается; работодатель обязан доплатить премию "
        "в течение 7 рабочих дней, иначе покрытие останется урезанным", ACTS["after_payout"])

    # OSGOR-10 изменение ФОТ или вида деятельности
    add("OSGOR-10", "предупреждение",
        "Изменение фонда оплаты труда или вида деятельности — уведомление в 5 рабочих дней, "
        "допсоглашение и перерасчёт премии пропорционально остатку срока", ACTS["payroll_change"])

    # OSGOR-11 штраф за отсутствие договора
    add("OSGOR-11", "подсказка",
        "Без договора ОСГОР: штраф 10–15 БРВ на должностное лицо, повторно в течение года — 15–30 БРВ",
        ACTS["koao"])

    # OSGOR-12 двойное покрытие за счёт бюджета
    if emp.budget_funded and emp.state_insured_staff:
        add("OSGOR-12", "стоп",
            "Работников, для которых предусмотрено обязательное государственное страхование жизни "
            "и здоровья, нельзя одновременно страховать по ОСГОР за счёт бюджетных средств — "
            "исключить их из расчёта ФОТ", ACTS["double"])
    elif emp.budget_funded:
        add("OSGOR-12", "предупреждение",
            "Работодатель финансируется из бюджета: проверьте, нет ли работников с обязательным "
            "государственным страхованием жизни и здоровья", ACTS["double"])

    # OSGOR-13 аннуитет
    add("OSGOR-13", "подсказка",
        "Возмещение на срок более одного года оформляется трёхсторонним договором аннуитетов "
        "со страховщиком жизни", ACTS["annuity"])
    return out


# ================================================================================================
# 6. Предупредительные мероприятия и документы
# ================================================================================================
def preventive_measures(emp: Employer, assessment: dict) -> list:
    """
    Предписания по охране труда — в формате таблицы preventive_measures
    (code, trigger_kind, trigger_key, measure, why, deadline_days, mandatory).
    Эффект на цену нулевой (тариф нормативный); эффект — на ожидаемые выплаты.
    """
    out = []

    def add(code, trigger_key, trigger_val, measure, why, days, mandatory=False):
        out.append({"code": code, "class_code": CLASS_CODE, "trigger_kind": "factor",
                    "trigger_key": trigger_key, "trigger_val": trigger_val, "measure": measure,
                    "why": why, "deadline_days": days, "mandatory": mandatory})

    if not emp.safety_service:
        add("OSGOR-M01", "safety_service", "none",
            "Назначить приказом ответственного за охрану труда (при численности от 50 работников — "
            "создать службу охраны труда)",
            "Без ответственного никто не отвечает за инструктажи, расследование и устранение причин — "
            "случаи повторяются", 30, mandatory=True)
    if not emp.training_done:
        add("OSGOR-M02", "training", "none",
            "Провести обучение и проверку знаний по охране труда, оформить протоколы и журналы инструктажей",
            "Большая часть травм — от незнания порядка работ и отсутствия инструктажа на рабочем месте", 45)
    if not emp.ppe_provided:
        add("OSGOR-M03", "ppe", "none",
            "Обеспечить работников средствами индивидуальной защиты по нормам и вести ведомости выдачи",
            "СИЗ напрямую снижают тяжесть травмы: та же авария даёт лёгкое повреждение вместо инвалидности",
            30, mandatory=True)
    if not emp.attestation_done:
        add("OSGOR-M04", "attestation", "none",
            "Провести аттестацию рабочих мест по условиям труда",
            "Без аттестации неизвестно, сколько людей работает во вредных и опасных условиях, "
            "и нечем подтвердить профзаболевание", 90, mandatory=True)
    if (emp.labour_inspection_orders or 0) > 0:
        add("OSGOR-M05", "inspection", "orders",
            f"Выполнить предписания инспекции труда ({emp.labour_inspection_orders} шт.) "
            f"и представить отчёт об устранении",
            "Невыполненное предписание — это уже выявленное государством нарушение, "
            "по которому случай считается предсказуемым", 60, mandatory=True)
    if float(emp.share_dangerous or 0) > 0.2:
        add("OSGOR-M06", "dangerous_conditions", ">20%",
            "План мероприятий по снижению опасных факторов: ограждения, блокировки, наряд-допуск "
            "на работы повышенной опасности",
            "Доля работников в опасных условиях выше пятой части — основной источник тяжёлых травм", 90)
    if float(emp.share_harmful or 0) > 0.3:
        add("OSGOR-M07", "harmful_conditions", ">30%",
            "Периодические медицинские осмотры работников вредных профессий, учёт результатов",
            "Профзаболевание выявляется поздно и оплачивается долго — осмотры позволяют увести "
            "работника с вредного участка до стойкой утраты трудоспособности", 90)
    if emp.fatal_3y > 0:
        add("OSGOR-M08", "fatal_accidents", ">0",
            "Представить акты расследования по форме Н-1 по смертельным случаям и документы "
            "об устранении причин",
            "Смертельный случай даёт длительные выплаты иждивенцам через аннуитет — "
            "нужна уверенность, что причина устранена", 30, mandatory=True)
    if emp.wage_arrears:
        add("OSGOR-M09", "wage_arrears", "yes",
            "Представить справку о погашении задолженности по заработной плате",
            "Задолженность по зарплате — риск неуплаты премии в 5 рабочих дней, а без оплаты "
            "покрытие не начинается", 30)
    if assessment.get("expected_loss_ratio", 0) > LR_AMBER:
        add("OSGOR-M10", "loss_ratio", ">100%",
            "Совместный осмотр производства с инженером по охране труда страховщика и согласованная "
            "программа мероприятий на год",
            "Ожидаемые выплаты выше премии — без вмешательства договор будет убыточным, "
            "а работники продолжат травмироваться", 45)
    return out


DOCS_ALWAYS = [
    ("заявление на страхование", "п. 12 Правил"),
    ("свидетельство о государственной регистрации (для физлица — копия паспорта)", "п. 12 Правил"),
    ("штатное расписание", "подтверждение численности работников"),
    ("справка о фонде оплаты труда за 12 месяцев (или за срок деятельности)", "ЗРУ-210 ст. 10; п. 21 Правил"),
    ("сведения о видах деятельности с кодами ОКЭД (IFUT)", "прил. № 9, разд. I, п. 3"),
]
DOCS_RISK = [
    ("сведения о несчастных случаях за 3 года, акты по форме Н-1", "оценка риска — ЗРУ-210 ст. 18"),
    ("материалы аттестации рабочих мест по условиям труда", "оценка риска — ЗРУ-210 ст. 18"),
    ("приказ о назначении ответственного за охрану труда / положение о службе охраны труда",
     "оценка риска — ЗРУ-210 ст. 18"),
    ("журналы инструктажей и протоколы проверки знаний по охране труда", "оценка риска — ЗРУ-210 ст. 18"),
    ("ведомости выдачи средств индивидуальной защиты", "оценка риска — ЗРУ-210 ст. 18"),
    ("предписания инспекции труда и отчёт об их выполнении", "оценка риска — ЗРУ-210 ст. 18"),
]


def checklist(emp: Employer) -> list:
    """
    Документы. Важно: требовать документы сверх перечня ст. 13 ЗРУ-210 (п. 34 Правил) при
    урегулировании убытка запрещено, а вот при заключении договора страховщик вправе оценивать
    риск (ст. 18) — поэтому «для оценки риска» документы помечены как необязательные.
    """
    got = {str(d).lower() for d in (emp.docs_received or [])}
    out = []
    for name, ref in DOCS_ALWAYS:
        out.append({"doc_name": name, "required": 1, "legal_ref": ref,
                    "received": any(name.lower()[:18] in g for g in got)})
    for name, ref in DOCS_RISK:
        out.append({"doc_name": name, "required": 0, "legal_ref": ref,
                    "received": any(name.lower()[:18] in g for g in got)})
    return out


# ================================================================================================
# 7. Решение
# ================================================================================================
def decision(emp: Employer, assessment: dict, checks_list: list, measures: list) -> dict:
    """
    Отказать работодателю нельзя: договор публичный (ЗРУ-210 ст. 6 ч. 1; п. 11 Правил),
    а страхование обязательно (ст. 17). Поэтому решений три:
      принять / принять с предупредительными мероприятиями / передать андеррайтеру.
    """
    reasons, level = [], DECISION_ACCEPT
    lr = assessment.get("expected_loss_ratio") or 0.0
    stops = [c for c in checks_list if c["severity"] == "стоп"]

    if lr > LR_AMBER:
        level = DECISION_UNDERWRITER
        reasons.append(f"ожидаемая убыточность {lr * 100:.0f} % выше 100 %: выплаты ожидаются больше премии, "
                       f"а тариф изменить нельзя")
    elif lr > LR_GREEN:
        level = DECISION_MEASURES
        reasons.append(f"ожидаемая убыточность {lr * 100:.0f} % выше ориентира {LR_GREEN * 100:.0f} % "
                       f"(рынок — {assessment.get('market_loss_ratio', 0) * 100:.1f} %)")
    if emp.fatal_3y > 0:
        level = DECISION_UNDERWRITER
        reasons.append(f"смертельных случаев за 3 года: {emp.fatal_3y} — вероятны длительные выплаты "
                       f"через аннуитет (пп. 49–50 Правил)")
    if stops:
        level = DECISION_UNDERWRITER
        reasons.append("есть нарушения-«стоп»: " + "; ".join(f"{c['code']} {c['text']}" for c in stops))
    if any(m.get("mandatory") for m in measures) and level == DECISION_ACCEPT:
        level = DECISION_MEASURES
        reasons.append("есть обязательные предупредительные мероприятия по охране труда")
    if emp.wage_arrears:
        reasons.append("задолженность по заработной плате: риск неуплаты премии в 5 рабочих дней, "
                       "до оплаты покрытие не начинается (п. 26 Правил)")
        if level == DECISION_ACCEPT:
            level = DECISION_MEASURES
    if not reasons:
        reasons.append(f"ожидаемая убыточность {lr * 100:.0f} % не выше ориентира, нарушений нет")

    return {"decision": level, "reasons": reasons,
            "note": "Отказ по ОСГОР невозможен: договор публичный, страховщик не вправе отказать "
                    "работодателю, подавшему заявление и документы",
            "legal_ref": ACTS["public"]}


# ================================================================================================
# 8. Сквозной расчёт
# ================================================================================================
def assess(emp: Employer, brv: Optional[float] = None, con: Optional[sqlite3.Connection] = None,
           declared_sum: Optional[float] = None, activities: Optional[list] = None,
           situation: str = "работает больше года", first_month_payroll: Optional[float] = None) -> dict:
    """Премия по акту + оценка риска + проверки + мероприятия + документы + решение."""
    activity = find_activity(okved=emp.okved, name=emp.activity_name, activities=activities, con=con)
    ss = insured_sum(payroll_12m=emp.payroll_12m, first_month_payroll=first_month_payroll,
                     payroll_for_period=emp.payroll_12m, situation=situation)
    prem = premium(ss["sum_insured"], activity["kst"], term_days=emp.term_days,
                   brv=brv, activity=activity)
    market = market_reference(con)
    assessment = risk_assessment(emp, activity["kst"], prem["premium"], market)
    ch = checks(emp, activity, prem, brv=brv, declared_sum=declared_sum)
    measures = preventive_measures(emp, assessment)
    docs = checklist(emp)
    dec = decision(emp, assessment, ch, measures)
    return {"product_code": PRODUCT_CODE, "class_code": CLASS_CODE,
            "activity": activity, "insured_sum": ss, "premium": prem, "market": market,
            "assessment": assessment, "checks": ch, "measures": measures,
            "checklist": docs, "decision": dec}


def explain(result: dict) -> str:
    """Построчное объяснение для агента: премия по шагам, затем оценка риска и решение."""
    lines = [f"ОСГОР, продукт {result['product_code']}, класс {result['class_code']}", "", "Премия:"]
    for s in result["premium"]["steps"]:
        lines.append(f"  {s['шаг']}: {s['значение']} — {s['пояснение']} [{s['норма']}]")
    for n in result["premium"]["notes"]:
        lines.append(f"  * {n}")
    lines += ["", "Оценка риска (экспертная, calibrated = 0):"]
    lines += [f"  {t}" for t in result["assessment"]["explain"]]
    lines += ["", f"Решение: {result['decision']['decision']}"]
    lines += [f"  — {r}" for r in result["decision"]["reasons"]]
    if result["measures"]:
        lines += ["", "Предупредительные мероприятия:"]
        lines += [f"  {m['measure']} (срок {m['deadline_days']} дн."
                  + (", условие договора" if m.get("mandatory") else "") + f") — {m['why']}"
                  for m in result["measures"]]
    stops = [c for c in result["checks"] if c["severity"] == "стоп"]
    if stops:
        lines += ["", "Стоп-проверки:"] + [f"  {c['code']}: {c['text']} [{c['legal_ref']}]" for c in stops]
    return "\n".join(lines)


def quick_premium(con: Optional[sqlite3.Connection], okved: str, payroll: float, term_days: int = YEAR_DAYS,
                  brv: Optional[float] = None, on_date: Optional[str] = None, lang: str = "ru") -> dict:
    """
    Калькулятор ОСГОР для мини-аппа: ОКЭД + ФОТ за 12 мес. + срок в днях → премия построчно.
    Ничего не считает сам: find_activity() + premium() по акту. БРВ: из параметра, иначе действующий
    размер из справочника администратора (brv_on, таблица osgor_brv); нет ни того, ни другого —
    минимум 0,25 БРВ не проверяется, об этом строка в notes.
    Возврат: {"lines","premium","min_applied","min_premium","kst","activity","brv","brv_source",
              "notes","legal_ref"}.
    """
    lang = premium_lang(lang)
    activity = find_activity(okved=okved, con=con)
    brv_source = brv_source_text(lang) if brv else None
    if not brv and con is not None:
        try:
            row = brv_on(con, on_date)
        except Exception:               # таблицы osgor_brv ещё нет — БРВ не задан
            row = None
        if row:
            brv = row["value"]
            brv_source = (f"справочник администратора, с {row['effective_from']} ({row['source']})"
                          if lang == "ru" else brv_source_text(lang, row))
    prem = premium(float(payroll), activity["kst"], term_days=int(term_days), brv=brv, activity=activity,
                   lang=lang)
    lines = [{"step": s["шаг"], "value": s["значение"], "explain": s["пояснение"], "legal_ref": s["норма"],
              "code": s["code"]} for s in prem["steps"]]
    act = activity_l(activity, lang, "".join(ch for ch in str(okved or "") if ch.isdigit()))
    if lang == "ru":
        legal = {"формула": ACTS["formula"], "минимум": ACTS["min"], "КСТ": activity["legal_ref"]}
    else:
        # ключи латиницей: в uz/en в ответе не должно быть кириллицы
        legal = {"formula": act_ref("formula", lang), "min": act_ref("min", lang), "kst": act["legal_ref"]}
    out = {"lines": lines, "premium": round(prem["premium"], 2), "min_applied": prem["min_applied"],
           "min_premium": prem["min_premium"], "kst": activity["kst"], "activity": act,
           "term_days": int(term_days), "sum_insured": prem["sum_insured"],
           "brv": brv, "brv_source": brv_source, "notes": prem["notes"], "note_codes": prem["note_codes"],
           "legal_ref": legal, "lang": lang}
    if lang == "en":
        out["legal_note_en"] = LEGAL_NOTE_EN
    return out


# ================================================================================================
# 9. Вход по сети: экран агента (/ui) считает ОСГОР здесь
# ================================================================================================
# Роутер ничего не считает сам: он принимает анкету работодателя, вызывает assess() и отдаёт
# результат как есть. Размер БРВ в системе не задан (вопрос заказчику) — его передаёт экран.
from fastapi import APIRouter          # noqa: E402
from pydantic import BaseModel         # noqa: E402

from . import db                       # noqa: E402

router = APIRouter()

SITUATIONS = ("работает больше года", "деятельность меньше года", "начинает деятельность")


class EmployerIn(BaseModel):
    """Анкета работодателя с экрана агента."""
    name: str = ""
    okved: str = ""
    activity_name: str = ""
    headcount: int = 0
    payroll_12m: float = 0.0
    share_harmful: float = 0.0
    share_dangerous: float = 0.0
    attestation_done: bool = True
    accidents_3y: int = 0
    severe_3y: int = 0
    fatal_3y: int = 0
    payouts_3y: float = 0.0
    safety_service: bool = True
    training_done: bool = True
    ppe_provided: bool = True
    labour_inspection_orders: int = 0
    wage_arrears: bool = False
    budget_funded: bool = False
    state_insured_staff: bool = False
    term_days: int = YEAR_DAYS
    activity_shorter_than_year: bool = False
    premium_paid: bool = False
    burial_amount: Optional[float] = None
    docs_received: list = []
    notes: str = ""
    # что не относится к самому работодателю
    brv: Optional[float] = None                     # размер БРВ на день заключения договора
    declared_sum: Optional[float] = None            # страховая сумма из заявления
    situation: str = "работает больше года"
    first_month_payroll: Optional[float] = None
    contract_date: Optional[str] = None             # ГГГГ-ММ-ДД: по ней берётся БРВ, пусто — сегодня


# --- поиск по классификации: индекс в памяти -----------------------------------------------------
# 934 строки держим в памяти; индекс пересобирается, когда меняется поколение справочников
# (db.reference_changed при правке админа, db.invalidate_reference после refsync) или файл базы
# (тесты подменяют db.DB_PATH), и не реже раза в ACT_INDEX_TTL_SEC — на случай правки базы мимо сервера.
import math                            # noqa: E402
import re                              # noqa: E402
import threading                       # noqa: E402
import time                            # noqa: E402
from datetime import date              # noqa: E402

from fastapi import HTTPException      # noqa: E402

ACT_INDEX_TTL_SEC = 600
ACT_LIMIT_MAX = 100
_act_index = {"key": None, "at": 0.0, "items": []}
_act_lock = threading.Lock()
_WORD = re.compile(r"[0-9a-zа-яўқғҳ]+")
_CODE_ONLY = re.compile(r"[\d.\s\-]+")
# узбекская латиница и русская транслитерация → кириллица: агент может набрать «stroit» или «qurilish».
# Названия в классификации русские; узбекских названий в акте нет — если появятся в базе
# (колонка name_uz), они попадут в индекс тем же путём.
_LAT2CYR = [("o'", "о"), ("g'", "г"), ("sh", "ш"), ("ch", "ч"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"),
            ("yo", "е"), ("yu", "ю"), ("ya", "я"), ("a", "а"), ("b", "б"), ("c", "ц"), ("d", "д"),
            ("e", "е"), ("f", "ф"), ("g", "г"), ("h", "х"), ("i", "и"), ("j", "ж"), ("k", "к"), ("l", "л"),
            ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"), ("q", "к"), ("r", "р"), ("s", "с"), ("t", "т"),
            ("u", "у"), ("v", "в"), ("w", "в"), ("x", "х"), ("y", "й"), ("z", "з")]
_APOS = "‘’ʻʼ`´′"


def _qnorm(s: str) -> str:
    s = str(s or "").lower().replace("ё", "е")
    for a in _APOS:
        s = s.replace(a, "'")
    return s


def _lat2cyr(word: str) -> str:
    out, i = [], 0
    while i < len(word):
        for lat, cyr in _LAT2CYR:
            if word.startswith(lat, i):
                out.append(cyr)
                i += len(lat)
                break
        else:
            out.append(word[i])
            i += 1
    return "".join(out)


def invalidate_activities() -> None:
    with _act_lock:
        _act_index["key"] = None


def activity_index() -> list:
    """Классификация с разобранными словами названий; кэш в памяти."""
    key = (str(db.DB_PATH), db.reference_generation())
    now_t = time.monotonic()
    with _act_lock:
        if _act_index["key"] == key and now_t - _act_index["at"] < ACT_INDEX_TTL_SEC:
            return _act_index["items"]
    con = db.connect()
    try:
        acts = load_activities(con)
        try:
            extra = {r[0]: r[1] for r in con.execute("SELECT no, name_uz FROM osgor_activities").fetchall()}
        except sqlite3.Error:
            extra = {}                  # колонки name_uz нет — ищем по русскому названию
    finally:
        con.close()
    items = []
    for a in acts:
        text = _qnorm(a["name"] + " " + (extra.get(a["no"]) or ""))
        items.append({**a, "_words": _WORD.findall(text.replace("'", ""))})
    with _act_lock:
        _act_index.update(key=key, at=now_t, items=items)
    return items


def _public_act(a: dict) -> dict:
    return {"okved": a["okved"], "name": a["name"], "category": a["category"], "kst": a["kst"], "no": a["no"]}


def search_activities(q: str = "", limit: int = 20) -> dict:
    """
    Поиск по коду ОКЭД (начало кода: «01.11», «0111») и по словам названия (по началу слов,
    регистр не важен, латиница переводится в кириллицу). Все слова запроса должны найтись.
    Пустой запрос — первые позиции классификации.
    """
    acts = activity_index()
    limit = max(1, min(int(limit or 20), ACT_LIMIT_MAX))
    q = _qnorm(q).strip()
    if not q:
        return {"items": [_public_act(a) for a in acts[:limit]], "found": len(acts), "how": "первые позиции"}
    if _CODE_ONLY.fullmatch(q):
        code, words = "".join(ch for ch in q if ch.isdigit()), []
    else:
        toks = _WORD.findall(q.replace("'", ""))
        code = "".join(t for t in toks if t.isdigit())
        words = [t for t in toks if not t.isdigit()]
    variants = [[w, _lat2cyr(w)] if re.search(r"[a-z]", w) else [w] for w in words]

    def word_hit(a) -> Optional[int]:
        # позиция слова названия, с которого начинается первое слово запроса; None — не подошло
        first = None
        for vs in variants:
            pos = next((i for i, w in enumerate(a["_words"]) if any(w.startswith(v) for v in vs)), None)
            if pos is None:
                return None
            first = pos if first is None else first
        return first or 0

    found = []
    for a in acts:
        if code and not a["okved"].startswith(code):
            continue
        pos = word_hit(a) if variants else 0
        if pos is None:
            continue
        found.append((0 if a["okved"] == code else 1, pos, len(a["_words"]), a["okved"], a))
    how = "код ОКЭД" if code and not words else ("слова названия" if words and not code else "код и слова")
    if not found and variants:
        # по началу слов пусто — пробуем вхождение в середине слова («монтаж» в «электромонтажные»)
        for a in acts:
            if code and not a["okved"].startswith(code):
                continue
            text = " ".join(a["_words"])
            if all(any(v in text for v in vs) for vs in variants):
                found.append((1, 99, len(a["_words"]), a["okved"], a))
        how += ", часть слова"
    found.sort(key=lambda x: x[:4])
    return {"items": [_public_act(x[4]) for x in found[:limit]], "found": len(found), "how": how}


@router.get("/osgor/activities")
def osgor_activities(q: str = "", limit: int = 20) -> dict:
    """
    Подсказка по классификации видов деятельности (934 позиции, прил. № 9, разд. I, п. 3).
    items — до limit (по умолчанию 20) вариантов {okved, name, category, kst, no}.
    """
    if len(q) > 200:
        raise HTTPException(422, "Запрос слишком длинный")
    res = search_activities(q, limit)
    return {"total": len(activity_index()), "q": q, **res, "kst_min": KST_MIN, "kst_max": KST_MAX,
            "categories": CATEGORIES, "legal_ref": ACTS["kst"], "not_listed_kst": KST_NOT_LISTED}


def _contract_date(value: Optional[str]) -> str:
    """Дата договора ГГГГ-ММ-ДД (по ней берётся БРВ); пусто — сегодня."""
    if not value:
        return date.today().isoformat()
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except ValueError:
        raise HTTPException(422, "Дата договора — в формате ГГГГ-ММ-ДД")


def brv_source_text(lang: str = "ru", row: Optional[dict] = None) -> str:
    """Откуда взят БРВ. Текст источника (row['source']) вводит админ — это данные, не переводятся."""
    if row is None:
        return "передан в запросе" if lang == "ru" else ptext(lang, "brv.request")
    if lang == "ru":
        return f"справочник администратора: действует с {row['effective_from']} ({row['source']})"
    return ptext(lang, "brv.reference", date=row["effective_from"], source=row["source"])


def _brv_for(con, brv: Optional[float], on: str, lang: str = "ru") -> tuple:
    """(размер БРВ или None, откуда взят). Переданный в запросе проверяем как недоверенный ввод."""
    if brv is not None:
        if not isinstance(brv, (int, float)) or not math.isfinite(brv) or brv <= 0 or brv > BRV_MAX:
            raise HTTPException(422, "Размер БРВ — положительное число в сумах")
        return float(brv), brv_source_text(lang)
    try:
        row = brv_on(con, on)
    except Exception:                   # таблицы osgor_brv ещё нет — БРВ не задан
        row = None
    if not row:
        return None, None
    return row["value"], brv_source_text(lang, row)


def _brv_note(holder: dict, brv, lang: str = "ru") -> str:
    """БРВ нет — одно предупреждение в note; такую же строку из notes убираем, чтобы не было дубля.
    holder — словарь с notes и note_codes (ответ quick или premium из assess)."""
    if brv:
        return ""
    codes = holder.get("note_codes") or [None] * len(holder["notes"])
    keep = [i for i, c in enumerate(codes) if c != "brv_not_passed"
            and holder["notes"][i] != BRV_NOT_PASSED_NOTE]
    holder["notes"][:] = [holder["notes"][i] for i in keep]
    if "note_codes" in holder:
        holder["note_codes"][:] = [codes[i] for i in keep]
    return ptext(premium_lang(lang), "note.brv_missing", minbrv=num_l(MIN_PREMIUM_BRV, premium_lang(lang)))


@router.post("/osgor/assess")
def osgor_assess(body: EmployerIn) -> dict:
    """
    Премия по акту, оценка риска, решение, мероприятия, документы и проверки OSGOR-01…13.
    Отказа в этом виде нет: договор публичный (ЗРУ-210 ст. 6 ч. 1).
    БРВ не передан — берётся действующий на дату договора из справочника администратора.
    """
    fields = {f: getattr(body, f) for f in Employer.__dataclass_fields__}
    emp = Employer(**fields)
    situation = body.situation if body.situation in SITUATIONS else SITUATIONS[0]
    on = _contract_date(body.contract_date)
    con = db.connect()
    try:
        brv, brv_source = _brv_for(con, body.brv, on)
        result = assess(emp, brv=brv, con=con, declared_sum=body.declared_sum,
                        situation=situation, first_month_payroll=body.first_month_payroll)
    finally:
        con.close()
    result["note"] = _brv_note(result["premium"], brv)
    result["explain"] = explain(result)
    result["brv"] = brv
    result["brv_source"] = brv_source
    result["contract_date"] = on
    result["thresholds"] = {"зелёный": LR_GREEN, "жёлтый": LR_AMBER,
                            "минимум БРВ": MIN_PREMIUM_BRV, "погребение БРВ": BURIAL_MAX_BRV}
    result["market_fallback"] = MARKET_LR_FALLBACK
    return result


class QuickIn(BaseModel):
    """Калькулятор ОСГОР мини-аппа."""
    okved: str
    payroll: float                                  # ФОТ за 12 месяцев = страховая сумма, сум
    term_days: int = YEAR_DAYS
    brv: Optional[float] = None                     # пусто — действующий на дату договора
    contract_date: Optional[str] = None             # ГГГГ-ММ-ДД, пусто — сегодня
    lang: Optional[str] = None                      # ru | uz | en; иначе ru (или ?lang=)


PAYROLL_MAX = 1e15                     # защита от опечатки в разрядах, а не норма


@router.post("/osgor/quick")
def osgor_quick(body: QuickIn, lang: str = "") -> dict:
    """ОКЭД + ФОТ + срок → премия построчно (quick_premium). Ничего не сохраняет.
    Язык текстов — lang в теле или ?lang= (ru | uz | en, иначе ru); тело главнее."""
    lang = premium_lang(body.lang or lang)
    code = "".join(ch for ch in (body.okved or "") if ch.isdigit())
    if not code or len(code) > 6:
        raise HTTPException(422, "Код ОКЭД — от 1 до 6 цифр, например 41100 или 01.11")
    if not math.isfinite(body.payroll) or body.payroll <= 0 or body.payroll > PAYROLL_MAX:
        raise HTTPException(422, "Фонд оплаты труда за 12 месяцев — положительное число в сумах")
    if not 1 <= body.term_days <= YEAR_DAYS:
        raise HTTPException(422, "Срок договора ОСГОР — от 1 до 365 дней (ЗРУ-210 ст. 6 ч. 7)")
    on = _contract_date(body.contract_date)
    con = db.connect()
    try:
        brv, brv_source = _brv_for(con, body.brv, on, lang)
        res = quick_premium(con, code, body.payroll, term_days=body.term_days, brv=brv, on_date=on, lang=lang)
    except ValueError as e:
        raise HTTPException(422, str(e))
    finally:
        con.close()
    res["brv_source"] = brv_source
    res["contract_date"] = on
    res["note"] = _brv_note(res, brv, lang)
    res["note_code"] = "brv_missing" if res["note"] else ""
    return res


# ================================================================================================
# 10. Размер БРВ — справочник администратора (задача 144)
# ================================================================================================
# Значение в коде не зашито и не засевается: пока админ не ввёл, GET отвечает value = null
# (вопрос 110 заказчику). Версии по дате вступления в силу — правило 9 CLAUDE.md, таблица osgor_brv.
# Запись закрыта для не-админов дважды: guard (ADMIN_METHOD_PATH) и require(ADMIN) здесь.
import math                            # noqa: E402
from datetime import date              # noqa: E402

from fastapi import Depends, HTTPException, Request    # noqa: E402

from . import auth                     # noqa: E402

BRV_MAX = 100_000_000                  # защита от опечатки в разрядах, а не норма


class BrvIn(BaseModel):
    value: float
    effective_from: str                 # ГГГГ-ММ-ДД
    source: str                         # акт, которым установлен размер, или ссылка на него
    note: str = ""


def _brv_row(r: Optional[dict]) -> Optional[dict]:
    if not r:
        return None
    return {k: r[k] for k in ("id", "value", "effective_from", "source", "note", "entered_by", "entered_at")}


def brv_on(con, on_date: Optional[str] = None) -> Optional[dict]:
    """Действующий на дату размер БРВ или None, если админ его ещё не вводил."""
    d = on_date or date.today().isoformat()
    rows = db.rows(con, "SELECT * FROM osgor_brv WHERE effective_from <= ?"
                        " ORDER BY effective_from DESC, id DESC LIMIT 1", d)
    return _brv_row(rows[0] if rows else None)


def check_brv(body: BrvIn) -> dict:
    """Недоверенный ввод: число, дата, источник. Ошибка — 422 с понятным текстом."""
    v = body.value
    if not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 or v > BRV_MAX:
        raise HTTPException(422, "Размер БРВ — положительное число в сумах")
    try:
        d = date.fromisoformat((body.effective_from or "").strip())
    except ValueError:
        raise HTTPException(422, "Дата вступления в силу — в формате ГГГГ-ММ-ДД")
    source = " ".join((body.source or "").split())
    if len(source) < 3:
        raise HTTPException(422, "Укажите источник: акт, которым установлен размер БРВ, или ссылку на него")
    return {"value": float(v), "effective_from": d.isoformat(), "source": source[:500],
            "note": " ".join((body.note or "").split())[:500]}


def save_brv(con, actor: dict, body: BrvIn) -> dict:
    data = check_brv(body)
    before = brv_on(con, data["effective_from"])
    cur = con.execute("INSERT INTO osgor_brv (value, effective_from, source, note, entered_by, entered_at)"
                      " VALUES (?,?,?,?,?,?)",
                      (data["value"], data["effective_from"], data["source"], data["note"] or None,
                       actor["login"], db.now()))
    db.audit(con, actor["login"], "размер БРВ", f"osgor_brv:{cur.lastrowid}",
             {"было на эту дату": before["value"] if before else None} | data)
    return {"ok": True, "id": cur.lastrowid, **data}


@router.get("/osgor/brv")
def get_brv(request: Request, on: str = "", lang: str = "") -> dict:
    """Действующий размер БРВ (на сегодня или на дату ?on=ГГГГ-ММ-ДД), ближайший будущий и история.
    ?lang=uz|en — note и legal_ref на этом языке (по умолчанию ru)."""
    lang = premium_lang(lang)
    if on:
        try:
            on = date.fromisoformat(on).isoformat()
        except ValueError:
            raise HTTPException(422, "Дата — в формате ГГГГ-ММ-ДД")
    with db.tx() as con:
        current = brv_on(con, on or None)
        d = on or date.today().isoformat()
        nxt = db.rows(con, "SELECT * FROM osgor_brv WHERE effective_from > ?"
                           " ORDER BY effective_from, id DESC LIMIT 1", d)
        history = [_brv_row(r) for r in db.rows(con, "SELECT * FROM osgor_brv"
                                                     " ORDER BY effective_from DESC, id DESC")]
    return {"on": d, "value": current["value"] if current else None, "current": current,
            "next": _brv_row(nxt[0]) if nxt else None, "history": history,
            "can_edit": (auth.optional_user(request) or {}).get("role") == "админ",
            "legal_ref": ({"минимум": ACTS["min"], "погребение": ACTS["burial"]} if lang == "ru"
                          else {"min": act_ref("min", lang), "burial": act_ref("burial", lang)}),
            "note": "" if current else ptext(lang, "brv_note")}


@router.put("/osgor/brv")
def put_brv(body: BrvIn, user: dict = Depends(auth.require("админ"))) -> dict:
    with db.tx() as con:
        return save_brv(con, user, body)
