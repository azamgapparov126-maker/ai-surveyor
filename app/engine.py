"""
Движок расчёта ставки и проверок. Чистые функции без обращения к сети и файлам:
всё, что нужно, приходит в аргументах (справочники — из базы, ввод — из запроса).

Схема: нетто = база × доля включённых рисков × коэффициенты
       → + рисковая надбавка → ÷ (1 − нагрузка) → техническая брутто-ставка
       → сравнение с минимумом компании и минимумом регулятора → применённая ставка.
Премия = ставка × страховая сумма × срок / 365.

Коэффициенты не зашиты в код: набор факторов берётся из справочника по классу, который считается
(Reference.class_factors или class_code у самих коэффициентов). Риски (perils) есть только у классов 8 и 9,
у остальных классов доля покрытия равна 1. У продуктов «по согласованию с ЦО», «по программе» и
«по генеральному договору» минимальный тариф политики не применяется — расчёт даёт техническую ставку
и подсказку в проверках.
"""
from dataclasses import dataclass, field
from typing import Optional

from . import rate_model as rm

RISK_LOAD = 0.12          # рисковая надбавка до калибровки (γ ≈ 0,95)
CAT_PERILS = {"earthquake", "mudflow", "landslide"}
# Исторический набор факторов класса 8. Движок им больше не ограничен: факторы берутся
# из справочника по классу (см. class_factors). Список оставлен для калибровки (app/calibration.py).
FACTORS = ["construction", "activity", "protection", "seismic", "wear", "loss_history", "franchise"]
# Режимы ценообразования, при которых минимальный тариф политики не применяется:
# ставка согласуется отдельно, а расчёт даёт только техническую (ориентировочную) ставку.
NEGOTIATED_MODES = {
    "по согласованию": "Ставка по согласованию с ЦО",
    "программа": "Ставка по программе страхования",
    "генеральный договор": "Ставка по генеральному договору",
}
# Режим обязательных видов: тариф установлен нормативным актом (постановление ПКМ, закон).
# Расчёт здесь не создаёт ставку, а только показывает ориентир; ставку берут из акта.
STATUTORY_MODE = "нормативный акт"


@dataclass
class Reference:
    """Справочники, загруженные из базы."""
    base_rates: dict            # (class_code, object_type) -> net_rate_pct
    coefficients: dict          # (factor_code, option_code) -> {"name","multiplier","factor_name"}
    perils: dict                # peril_code -> {"class_code","share","name","cat"}
    load_share: float           # суммарная доля нагрузки (без РПМ)
    min_rates: dict             # product_code -> {"company": pct|None, "regulator": pct|None}
    product_classes: dict       # product_code -> [class_code, ...]
    checklists: list            # [{"scope_type","scope_code","doc_name","required"}]
    financials: Optional[dict]  # {"own_funds","reserves","report_date"} или None
    measures: list = field(default_factory=list)   # справочник предупредительных мероприятий
    market: dict = field(default_factory=dict)     # class_code -> {"rate_pct","loss_ratio","label","row"} из отчётов НАПП
    class_factors: dict = field(default_factory=dict)  # class_code -> [factor_code, ...] — факторы класса из справочника
    products: dict = field(default_factory=dict)       # product_code -> {"name","pricing_mode"}
    # модель ставки ТЗ 13.10.2026 (app/rate_model.py): {класс: {"market": r_рын НАПП, "experience": опыт INSON}}
    # и настройки {"rate_model", "load"} из act_settings; пусто — значения по умолчанию, рынка и опыта нет
    rate_inputs: dict = field(default_factory=dict)
    rate_settings: dict = field(default_factory=dict)


@dataclass
class Input:
    product_code: str
    class_code: str
    object_type: str
    value_amount: float
    sum_insured: float
    term_days: int = 365
    factors: dict = field(default_factory=dict)      # factor_code -> option_code
    perils_included: Optional[list] = None            # None = все риски класса, кроме ядерного
    docs_received: list = field(default_factory=list)
    applied_rate_pct: Optional[float] = None          # ручная ставка
    manual_reason: str = ""
    premium_paid: bool = False
    disclosure_done: bool = False
    credit: Optional[dict] = None                     # {"loan_amount","collateral_value","policyholder_is_bank","payer_is_bank"}
    takaful: bool = False
    payer_type: Optional[str] = None                  # 'юр' | 'физ' — для тарифов с разными ставками
    pricing_mode: Optional[str] = None                # 'ставка' | 'по согласованию' | 'программа' | 'генеральный договор'
    valuation: Optional[dict] = None                  # последняя оценка стоимости по объекту:
    # {"id","value","method","method_version","as_of","confirmed_by","region"} — из app/valuation.py.
    # None = оценки по объекту нет, проверка не выполняется и расчёт идёт как раньше.
    losses_count: Optional[int] = None                # убытков за 3 года (K_кл модели ставки); None — из loss_history
    seismic_zone: Optional[int] = None                # сейсмозона, баллов (c модели ставки); None — из фактора seismic


def _perils_of_class(ref: Reference, class_code: str) -> dict:
    return {c: p for c, p in ref.perils.items() if p["class_code"] == class_code}


def class_factors(ref: Reference, class_code: str) -> list:
    """
    Факторы класса из справочника, без жёсткого списка в коде.
    Берётся ref.class_factors (если база его отдаёт), иначе class_code у самих коэффициентов.
    Пустой список = справочник не знает факторов класса; тогда применяются те, что пришли в запросе.
    """
    known = list(ref.class_factors.get(class_code) or [])
    if known:
        return known
    for (f, _), v in ref.coefficients.items():
        if v.get("class_code") == class_code and f not in known:
            known.append(f)
    return known


def factors_for(ref: Reference, inp: Input) -> list:
    """Порядок факторов расчёта: сначала факторы класса, затем присланные агентом сверх них."""
    known = class_factors(ref, inp.class_code)
    if known:
        # факторы чужого класса не применяем: расчёт идёт по одному классу (Положение 1882, п. 11)
        return known
    return list(inp.factors)


def model_enabled(ref: Reference) -> bool:
    """Модель ставки ТЗ 13.10.2026 включена (act_settings.rate_model.enabled, по умолчанию да)."""
    return rm.enabled(ref.rate_settings)


LOSS_OPTION_COUNT = {"clean": 0, "one": 1, "many": 2}     # варианты coefficients.loss_history → число убытков
SEISMIC_OPTION_ZONE = {"z7": 7, "z8": 8, "z9": 9}         # варианты coefficients.seismic → баллы


def rate_for(ref: Reference, inp: Input) -> dict:
    """Техническая ставка с построчным объяснением. Включена модель ставки (rate_model) — ставка по разделу 3
    ТЗ 13.10.2026 (_rate_for_model), иначе — прежняя схема «база × доля рисков × коэффициенты + надбавки ÷ (1 − L)»."""
    if model_enabled(ref):
        return _rate_for_model(ref, inp)
    return rate_for_legacy(ref, inp)


def _rate_for_model(ref: Reference, inp: Input) -> dict:
    """
    Ставка калькулятора по модели ставки (app/rate_model.compute): r_нетто = Z·r_опыт + (1 − Z)·r_рын (НАПП) ×
    доля включённых рисков; r_брутто = r_нетто·(1 + θ + c)/(1 − L); × K_об (коэффициенты класса без убытков и
    сейсмозоны) × K_рег × K_кл; clip(r_нетто; 3 × тариф). gross_pct — расчётная ставка до минимума продукта
    (минимум применяет calculate). Ключи ответа — как у прежней схемы, плюс rate_model (все шаги и источники).
    """
    base = ref.base_rates.get((inp.class_code, inp.object_type))
    if base is None:
        same = [v for (c, _), v in ref.base_rates.items() if c == inp.class_code]
        base = sum(same) / len(same) if same else None
    perils = _perils_of_class(ref, inp.class_code)
    included = set(inp.perils_included) if inp.perils_included is not None         else {c for c in perils if c != "nuclear"}
    excluded = [perils[c]["name"] for c in perils if c not in included]
    if perils:
        total = sum(p["share"] for p in perils.values()) or 1.0
        share = sum(p["share"] for c, p in perils.items() if c in included) / total
        quake = any(c in included for c in CAT_PERILS)
    else:
        share, quake = 1.0, True
    mr = min_rate(ref, inp.product_code, inp.payer_type)
    negotiated = pricing_mode_of(ref, inp) in NEGOTIATED_MODES
    tariff = None if negotiated else mr["company"]
    floor = None if negotiated else mr["floor"]
    data = ref.rate_inputs.get(inp.class_code) or {}
    losses = inp.losses_count if inp.losses_count is not None         else LOSS_OPTION_COUNT.get(inp.factors.get("loss_history"))
    zone = inp.seismic_zone if inp.seismic_zone is not None         else SEISMIC_OPTION_ZONE.get(inp.factors.get("seismic"))
    facs = rm.engine_k_object(ref.coefficients, inp.factors, factors_for(ref, inp), ref.rate_settings)
    res = rm.compute({"class_code": inp.class_code, "sum_insured": inp.sum_insured, "term_days": inp.term_days,
                      "experience": data.get("experience"), "market": data.get("market"),
                      "tariff_pct": tariff, "min_pct": floor, "base_net_pct": base,
                      "peril_share": max(share, 0.001), "quake_covered": quake, "seismic_zone": zone,
                      "k_object_factors": facs, "losses_count": losses}, ref.rate_settings)
    if not res.get("available"):
        # ни рынка, ни тарифа, ни базовой ставки — прежняя схема (запасная базовая ставка движка)
        out = rate_for_legacy(ref, inp)
        out["rate_model"] = res
        return out
    net = res["r_net"]
    chain = [{"name": "Нетто-ставка: Z · опыт INSON + (1 − Z) · рынок НАПП"
              if res["r_market_kind"] == "market" else "Нетто-ставка: тариф INSON × (1 − L) (временно)"
              if res["r_market_kind"] == "tariff" else "Нетто-ставка: базовая ставка справочника (экспертно)",
              "value_pct": round(net, 4)}]
    if perils:
        chain.append({"name": "Набор рисков", "mult": round(share, 3), "excluded": excluded})
    chain.append({"name": f"Рисковая надбавка θ {res['theta']:g}", "add_pct": round(net * res["theta"], 4)})
    if res["c"]:
        chain.append({"name": f"Катастрофическая надбавка c {res['c']:g}", "add_pct": round(net * res["c"], 4)})
    chain.append({"name": f"Нагрузка L {res['L']:g}", "divide_by": round(1 - res["L"], 3)})
    for f in res["factors"]:
        # факторы объекта — все (и с множителем 1: видно, что учтено); K_рег, K_кл и границы — если не 1
        if f["kind"] == "object" or abs(f["coef"] - 1) > 1e-12:
            chain.append({"name": f["label"] or {"k_region": "K_рег", "k_client": "K_кл",
                                                 "k_object_bound": "Границы K_об"}.get(f["code"], f["code"]),
                          "mult": f["coef"], "applies": True})
    return {"base_pct": net / share if share else net, "share": share, "net_pct": net,
            "risk_pct": net * res["theta"], "cat_pct": net * res["c"], "gross_pct": res["r_calc"],
            "included": sorted(included), "chain": chain, "rate_model": res}


def rate_for_legacy(ref: Reference, inp: Input) -> dict:
    """Прежняя техническая ставка (до модели ТЗ 13.10.2026) с построчным объяснением."""
    base = ref.base_rates.get((inp.class_code, inp.object_type))
    if base is None:
        # нет базовой ставки для типа объекта — берём среднюю по классу
        same = [v for (c, _), v in ref.base_rates.items() if c == inp.class_code]
        base = sum(same) / len(same) if same else 0.2
    chain = [{"name": f"Базовая нетто-ставка · {inp.object_type}", "value_pct": round(base, 4)}]

    perils = _perils_of_class(ref, inp.class_code)
    included = set(inp.perils_included) if inp.perils_included is not None \
        else {c for c in perils if c != "nuclear"}
    if perils:
        total = sum(p["share"] for p in perils.values()) or 1.0
        share = sum(p["share"] for c, p in perils.items() if c in included) / total
        chain.append({"name": "Набор рисков", "mult": round(share, 3),
                      "excluded": [perils[c]["name"] for c in perils if c not in included]})
    else:
        share = 1.0   # у класса нет перечня отключаемых рисков (кредиты, ответственность) — покрытие целиком
    net = base * max(share, 0.001)

    for f in factors_for(ref, inp):
        opt = ref.coefficients.get((f, inp.factors.get(f, "")))
        if not opt:
            continue
        applies = f != "seismic" or "earthquake" in included
        m = opt["multiplier"] if applies else 1.0
        net *= m
        chain.append({"name": f"{opt['factor_name']} · {opt['name']}", "mult": m, "applies": applies})

    risk = net * RISK_LOAD
    cat = 0.0
    if any(c in included for c in CAT_PERILS):
        cat = net * 0.05   # катастрофическая надбавка до калибровки по сценарию сейсмозоны
    load = ref.load_share if not inp.takaful else max(ref.load_share - 0.05, 0.0)  # в такафуле нет прибыли компании
    gross = (net + risk + cat) / (1 - load)
    chain.append({"name": f"Рисковая надбавка {int(RISK_LOAD*100)}%", "add_pct": round(risk, 4)})
    if cat:
        chain.append({"name": "Катастрофическая надбавка", "add_pct": round(cat, 4)})
    chain.append({"name": f"Нагрузка {int(load*100)}%" + (" (профиль такафул)" if inp.takaful else ""),
                  "divide_by": round(1 - load, 2)})
    return {"base_pct": base, "share": share, "net_pct": net, "risk_pct": risk, "cat_pct": cat,
            "gross_pct": gross, "included": sorted(included), "chain": chain}


def _pick(rates: dict, payer_type):
    """rates: {payer_type|None: pct}. Ставка для типа клиента, иначе общая, иначе наибольшая."""
    if not rates:
        return None
    if payer_type in rates:
        return rates[payer_type]
    if None in rates:
        return rates[None]
    return max(rates.values())


def min_rate(ref: Reference, product_code: str, payer_type: Optional[str] = None) -> dict:
    mr = ref.min_rates.get(product_code, {})
    company = _pick(mr.get("company", {}), payer_type)
    regulator = _pick(mr.get("regulator", {}), payer_type)
    floor = max([x for x in (company, regulator) if x is not None], default=None)
    which = None
    if floor is not None:
        which = "регулятор" if regulator is not None and floor == regulator else "компания"
    return {"company": company, "regulator": regulator, "floor": floor, "source": which}


def pricing_mode_of(ref: Reference, inp: Input) -> Optional[str]:
    """Режим ценообразования продукта: из запроса или из справочника продуктов."""
    return inp.pricing_mode or (ref.products.get(inp.product_code) or {}).get("pricing_mode")


def premium_of(rate_pct: float, sum_insured: float, term_days: int) -> float:
    return rate_pct / 100 * sum_insured * term_days / 365


def _obj_match(c: dict, object_type: Optional[str]) -> bool:
    # Строка справочника «Спецтехника» покрывает все подтипы («Спецтехника — экскаватор» и т. д.),
    # поэтому сравнение идёт по началу названия.
    if c["scope_type"] != "тип_объекта" or not c.get("scope_code"):
        return False
    obj = (object_type or "").strip().lower()
    return bool(obj) and obj.startswith(str(c["scope_code"]).strip().lower())


PURPOSE_CONTRACT = "contract"      # расчёт под договор: всё как раньше
PURPOSE_ANALYSIS = "analysis"      # анализ риска: без документов и проверок оформления договора
# Правила оформления договора: к оценке риска отношения не имеют, в анализе не показываются.
CONTRACT_ONLY_CHECKS = {"premium_unpaid", "disclosure"}


def checklist_items(checklists: list, class_code: Optional[str], object_type: Optional[str] = None,
                    product_code: Optional[str] = None, purpose: str = PURPOSE_CONTRACT) -> list:
    """
    Строки чек-листа, которые относятся к договору. Собирается по признакам: «всегда», по классу
    и по типу объекта (тип нужен спецтехнике: документы у экскаватора и у легкового автомобиля
    разные, а класс у них один — 3). С product_code — ещё строки продукта (scope_type «продукт»).
    Движок в проверке docs_missing продукт не передаёт — поведение расчёта прежнее.
    Возвращает строки справочника как есть (с required и condition), без дублей по doc_name.
    purpose='analysis' — строки со scope 'оформление' (заявление-анкета) не возвращаются.
    """
    out, seen = [], set()
    for c in checklists:
        if purpose == PURPOSE_ANALYSIS and (c.get("scope") or "анализ") != "анализ":
            continue
        st = c["scope_type"]
        hit = (st == "всегда"
               or (st != "продукт" and class_code is not None and c["scope_code"] == class_code)
               or (st == "продукт" and product_code is not None and c["scope_code"] == product_code)
               or _obj_match(c, object_type))
        if hit and c["doc_name"] not in seen:
            seen.add(c["doc_name"])
            out.append(c)
    return out


def checks_for(ref: Reference, inp: Input, r: dict, applied: float, mr: dict,
               purpose: str = PURPOSE_CONTRACT) -> list:
    out = []
    add = lambda code, s, t, d: out.append({"rule": code, "status": s, "title": t, "detail": d})

    # ручная ставка
    if inp.applied_rate_pct is not None:
        auto = max(r["gross_pct"], mr["floor"] or 0)
        if inp.manual_reason:
            add("manual_rate", "warn", "Ставка введена вручную",
                f"Расчёт даёт {auto:.3f}%, поставлено {applied:.3f}%. Причина: {inp.manual_reason}.")
        else:
            add("manual_rate", "stop", "Ручная ставка без причины", "Укажите причину отступления от расчёта.")

    # сумма и стоимость (ГК ст. 936, 938). Для кредита страховая стоимость — это допустимая
    # необеспеченная часть, её проверяет кредитное правило ниже, а не правило о недостраховании.
    if inp.credit:
        pass
    elif inp.sum_insured > inp.value_amount:
        add("sum_over_value", "stop", "Страховая сумма выше страховой стоимости",
            f"Превышение {inp.sum_insured - inp.value_amount:,.0f} сум. По ст. 938 ГК договор в этой части ничтожен.")
    elif inp.sum_insured < inp.value_amount:
        add("underinsurance", "warn", "Неполное страхование",
            f"Сумма {inp.sum_insured / inp.value_amount * 100:.0f}% стоимости. По ст. 936 ГК выплата уменьшится в той же доле.")
    else:
        add("sum_ok", "ok", "Страховая сумма равна стоимости", "Выплата будет полной.")

    # сверка с оценкой стоимости (модуль оценки). Юридическая рамка: стоимость — действительная
    # стоимость в месте нахождения в день заключения договора (ГК ст. 934), определяется
    # соглашением сторон (ст. 935); превышение — ничтожность в части (ст. 938),
    # занижение — пропорциональная выплата (ст. 936).
    val = inp.valuation if isinstance(inp.valuation, dict) else None
    if val and val.get("value"):
        vv = float(val["value"])
        who = f" Оценку подтвердил андеррайтер ({val['confirmed_by']})." if val.get("confirmed_by") else               " Оценка андеррайтером ещё не подтверждена."
        src = f"Оценка {val.get('method') or 'системы'} на {val.get('as_of') or 'дату расчёта'}"               f" (методика {val.get('method_version') or '—'})"
        if inp.sum_insured > vv:
            add("sum_vs_valuation", "warn", "Страховая сумма выше оценки стоимости",
                f"{src}: {vv:,.0f} сум, страховая сумма {inp.sum_insured:,.0f} сум — "
                f"выше на {inp.sum_insured - vv:,.0f} сум. Страховая стоимость — действительная "
                f"стоимость имущества в месте его нахождения в день заключения договора "
                f"(ГК ст. 934); в части превышения стоимости договор недействителен, а излишне "
                f"уплаченная премия не возвращается (ГК ст. 938). Снизьте сумму до оценки или "
                f"подтвердите стоимость документами (договор купли-продажи, счёт, акт осмотра)."
                + who)
        elif inp.sum_insured < vv:
            add("sum_vs_valuation", "warn", "Страховая сумма ниже оценки стоимости",
                f"{src}: {vv:,.0f} сум, страховая сумма — {inp.sum_insured / vv * 100:.0f}% от неё. "
                f"При убытке выплата составит ту же долю от ущерба (ГК ст. 936); клиента нужно "
                f"предупредить до подписания (Закон о страховой деятельности, ст. 63)." + who)
        else:
            add("sum_vs_valuation", "ok", "Страховая сумма равна оценке стоимости",
                f"{src}: {vv:,.0f} сум. Окончательную страховую стоимость стороны определяют "
                f"соглашением при заключении договора (ГК ст. 935)." + who)
        if abs(vv - inp.value_amount) > 0.01:
            add("value_agreed", "warn", "Стоимость в расчёте не совпадает с оценкой",
                f"В расчёте указана стоимость {inp.value_amount:,.0f} сум, оценка системы — "
                f"{vv:,.0f} сум. Расхождение нужно снять до выпуска полиса: позже стоимость "
                f"оспорить нельзя, кроме случая, когда страховщик не воспользовался правом "
                f"на оценку и был умышленно введён в заблуждение (ГК ст. 932, 935).")

    # режим ценообразования: у продуктов «по согласованию с ЦО» и «по программе»
    # минимального тарифа в политике нет — расчёт даёт только техническую ставку
    mode = pricing_mode_of(ref, inp)
    if mode in NEGOTIATED_MODES:
        add("pricing_mode", "ok", NEGOTIATED_MODES[mode],
            f"Минимальный тариф по продукту {inp.product_code} не установлен. "
            f"Техническая ставка {r['gross_pct']:.3f}% — ориентир; окончательная ставка "
            + ("согласуется с центральным офисом." if mode == "по согласованию"
               else "берётся из программы страхования." if mode == "программа"
               else "берётся из генерального договора."))

    # обязательный вид: тариф установлен нормативным актом.
    # Пока ставки из акта нет в справочнике min_rates, расчёт не может быть основанием для оформления.
    if mode == STATUTORY_MODE and mr["floor"] is None:
        add("statutory_rate", "warn", "Тариф установлен нормативным актом",
            f"Это обязательный вид страхования (продукт {inp.product_code}). "
            f"Ставка берётся из нормативного акта (постановление ПКМ по этому виду), "
            f"расчётная ставка основанием не является. Техническая ставка {r['gross_pct']:.3f}% — "
            f"только ориентир для оценки убыточности. Ставка из акта в справочник пока не внесена, "
            f"поэтому автоматическая проверка минимума невозможна: сверьте ставку с актом "
            f"и не оформляйте договор по расчётной цифре.")

    # продукт из нескольких классов: считается первый, остальные — отдельные условные договоры
    parts = list(ref.product_classes.get(inp.product_code) or [])
    if len(parts) > 1:
        first, others = parts[0], parts[1:]
        if inp.class_code == first:
            add("multi_class", "ok", "Продукт из нескольких частей",
                f"Расчёт идёт по классу {first}. Классы {', '.join(others)} — отдельный расчёт "
                f"(Положение 1882, п. 11: каждая часть — условный договор).")
        elif inp.class_code in others:
            add("multi_class", "ok", "Часть продукта из нескольких классов",
                f"Считается класс {inp.class_code}. Основная часть продукта — класс {first}, она считается отдельно.")
        else:
            add("multi_class", "warn", "Класс не относится к продукту",
                f"У продукта {inp.product_code} классы {', '.join(parts)}, а расчёт запрошен по классу {inp.class_code}.")

    # минимальный тариф — по классу продукта, не по средней по договору
    if mr["floor"] is not None:
        if applied + 1e-9 >= mr["floor"]:
            add("min_rate", "ok", "Минимальный тариф соблюдён",
                f"Минимум по продукту {inp.product_code} — {mr['floor']:.3f}% ({mr['source']}).")
        else:
            add("min_rate", "stop", "Ставка ниже минимальной",
                f"Минимум {mr['floor']:.3f}% ({mr['source']}).")

    # техническая ставка
    if applied + 1e-9 >= r["gross_pct"]:
        add("below_technical", "ok", "Ставка покрывает ожидаемые убытки", f"Техническая ставка {r['gross_pct']:.3f}%.")
    else:
        add("below_technical", "warn", "Ставка ниже технической",
            f"Не хватает {r['gross_pct'] - applied:.3f} п.п.; договор убыточен — решение за андеррайтером.")

    # лимит 20% на один риск (Положение 1806, п. 15)
    if ref.financials:
        limit = 0.2 * (ref.financials["own_funds"] + ref.financials["reserves"])
        if inp.sum_insured <= limit:
            add("risk_limit_20", "ok", "Лимит на один риск соблюдён",
                f"Предел {limit:,.0f} сум по данным на {ref.financials['report_date']}.")
        else:
            add("risk_limit_20", "stop", "Превышен лимит 20% на один риск",
                f"Предел {limit:,.0f} сум (Положение 1806, п. 15). Нужно перестрахование.")
    else:
        add("risk_limit_20", "warn", "Лимит на один риск не проверен",
            "Нет данных о собственных средствах и резервах компании.")

    # кредитные правила (требование регулятора)
    if inp.credit:
        loan = float(inp.credit.get("loan_amount") or 0)
        coll = float(inp.credit.get("collateral_value") or 0)
        cap = min(max(loan - coll, 0), 0.5 * loan)
        if inp.sum_insured > cap + 1e-6:
            add("credit_uncovered", "stop", "Страховая сумма выше допустимой по кредиту",
                f"Допустимо не более {cap:,.0f} сум: необеспеченная часть и не выше 50% суммы кредита.")
        else:
            add("credit_uncovered", "ok", "Страховая сумма в пределах необеспеченной части", f"Допустимо до {cap:,.0f} сум.")
        if not (inp.credit.get("policyholder_is_bank") and inp.credit.get("payer_is_bank")):
            add("credit_payer", "stop", "Страхователь или плательщик — не банк",
                "Страхователем выступает банк-кредитор, премия принимается только от банка.")

    # документы
    need = [c["doc_name"] for c in checklist_items(ref.checklists, inp.class_code, inp.object_type,
                                                   purpose=purpose)
            if c["required"]]
    missing = [d for d in need if d not in set(inp.docs_received)]
    if missing:
        add("docs_missing", "warn", f"Не хватает документов: {len(missing)}", "; ".join(missing))
    else:
        add("docs_ok", "ok", "Обязательные документы собраны", "Расчёт можно передавать в договор.")

    # подсказка о франшизе — только если франшиза вообще есть в наборе факторов этого класса
    known = class_factors(ref, inp.class_code)
    franchise_applies = "franchise" in known if known else "franchise" in inp.factors
    if franchise_applies and inp.factors.get("franchise", "f0") == "f0":
        add("no_franchise", "warn", "Франшиза не установлена", "Мелкие убытки ложатся на компанию целиком.")
    if "earthquake" in r["included"] and inp.factors.get("seismic", "z7") != "z7":
        add("cat_accumulation", "warn", "Катастрофический риск в сейсмозоне",
            "Страховая сумма попадает в накопление по зоне; нужен контроль лимита по зоне.")
    # оплата премии и раскрытие информации — правила оформления договора, а не оценки риска
    if purpose != PURPOSE_ANALYSIS:
        if not inp.premium_paid:
            add("premium_unpaid", "warn", "Премия не поступила",
                "По ст. 33¹ полис без уплаты премии обязывает страховщика полностью.")
        if not inp.disclosure_done:
            add("disclosure", "warn", "Клиенту не раскрыта информация",
                "Ст. 63: цена, покрытые и исключённые риски, возврат премии, порядок претензий.")
    return [c for c in out if c["rule"] not in CONTRACT_ONLY_CHECKS] \
        if purpose == PURPOSE_ANALYSIS else out


def recommendations(ref: Reference, inp: Input, current_premium: float) -> list:
    """Что снизит премию, с экономией в сумах. Считается перебором вариантов."""
    tips = []

    def trial(patch_factors=None, remove_peril=None, new_sum=None, text="", kind=""):
        alt = Input(**{**inp.__dict__})
        alt.factors = {**inp.factors, **(patch_factors or {})}
        if remove_peril:
            base_set = set(rate_for(ref, inp)["included"])
            base_set.discard(remove_peril)
            alt.perils_included = sorted(base_set)
        if new_sum is not None:
            alt.sum_insured = new_sum
        alt.applied_rate_pct = None
        r = rate_for(ref, alt)
        applied = max(r["gross_pct"], min_rate(ref, alt.product_code, alt.payer_type)["floor"] or 0)
        save = current_premium - premium_of(applied, alt.sum_insured, alt.term_days)
        if save > 1:
            tips.append({"kind": kind, "text": text, "premium_delta": -round(save),
                         "patch": {"factors": patch_factors, "remove_peril": remove_peril, "sum_insured": new_sum}})

    f = inp.factors
    known = class_factors(ref, inp.class_code)
    has = lambda code: (code in known) if known else (code in f)   # фактор применим к этому классу
    if has("franchise") and f.get("franchise") != "f2":
        trial({"franchise": "f1"}, text="Франшиза 1% страховой суммы", kind="франшиза")
    if has("protection") and f.get("protection") != "sprinkler":
        nxt = "alarm_guard" if f.get("protection") in (None, "none", "alarm") else "sprinkler"
        trial({"protection": nxt}, text="Договор с охраной" if nxt == "alarm_guard" else "Спринклерная система", kind="защита")
    if "earthquake" in rate_for(ref, inp)["included"]:
        trial(remove_peril="earthquake", text="Исключить землетрясение", kind="риск")
    if inp.sum_insured > inp.value_amount:
        trial(new_sum=inp.value_amount, text="Снизить сумму до страховой стоимости", kind="сумма")
    return sorted(tips, key=lambda t: t["premium_delta"])[:4]


def preventive_measures(ref: Reference, inp: Input, r: dict, current_premium: float) -> list:
    """
    Предписания страхователю: что сделать, чтобы риск снизился. Подбираются по факторам и рискам объекта.
    Для каждого считается эффект на премию (если мероприятие меняет фактор) и признак «условие договора»
    — когда страховая сумма выше порога, мероприятие вписывается в договор со сроком исполнения.
    """
    out = []
    included = set(r["included"])
    for m in ref.measures:
        if m["class_code"] not in (inp.class_code, None):
            continue
        hit = False
        if m["trigger_kind"] == "factor":
            hit = inp.factors.get(m["trigger_key"]) == m["trigger_val"]
        elif m["trigger_kind"] == "peril":
            hit = m["trigger_key"] in included and (m["trigger_val"] is None or inp.factors.get("seismic") == m["trigger_val"])
        if not hit:
            continue
        delta = None
        if m["effect_option"]:
            alt = Input(**{**inp.__dict__})
            alt.factors = {**inp.factors, m["trigger_key"] if m["trigger_kind"] == "factor" else "protection": m["effect_option"]}
            alt.applied_rate_pct = None
            ra = rate_for(ref, alt)
            applied = max(ra["gross_pct"], min_rate(ref, alt.product_code, alt.payer_type)["floor"] or 0)
            delta = round(premium_of(applied, alt.sum_insured, alt.term_days) - current_premium)
        mandatory = m["mandatory_over"] is not None and inp.sum_insured >= m["mandatory_over"]
        out.append({"code": m["code"], "measure": m["measure"], "why": m["why"],
                    "premium_delta": delta, "mandatory": mandatory,
                    "deadline_days": m["deadline_days"] if mandatory else None})
    out.sort(key=lambda x: (not x["mandatory"], x["premium_delta"] or 0))
    return out


def calculate(ref: Reference, inp: Input, purpose: str = PURPOSE_CONTRACT) -> dict:
    """purpose='analysis' — расчёт для анализа риска: без проверок и документов оформления договора."""
    r = rate_for(ref, inp)
    mr = min_rate(ref, inp.product_code, inp.payer_type)
    if pricing_mode_of(ref, inp) in NEGOTIATED_MODES:
        # ставка согласуется с ЦО или берётся из программы — минимум политики не применяем
        mr = {**mr, "floor": None, "source": None}
    auto = max(r["gross_pct"], mr["floor"] or 0)
    applied = inp.applied_rate_pct if inp.applied_rate_pct else auto
    premium = premium_of(applied, inp.sum_insured, inp.term_days)
    checks = checks_for(ref, inp, r, applied, mr, purpose)
    worst = "stop" if any(c["status"] == "stop" for c in checks) else \
            "warn" if any(c["status"] == "warn" for c in checks) else "ok"
    verdict = {"ok": "ок", "warn": "на утверждение", "stop": "отклонено"}[worst]
    # сравнение с рынком: средняя годовая ставка по классу из отчётов НАПП
    mk = ref.market.get(inp.class_code)
    market = None
    if mk and mk.get("rate_pct"):
        market = {**mk, "deviation_pct": round((applied - mk["rate_pct"]) / mk["rate_pct"] * 100, 1)}
    return {
        "market": market,
        "rates": {"net_pct": round(r["net_pct"], 4), "risk_load_pct": round(r["risk_pct"], 4),
                  "cat_load_pct": round(r["cat_pct"], 4), "technical_pct": round(r["gross_pct"], 4),
                  "min_pct": mr["floor"], "min_source": mr["source"],
                  "auto_pct": round(auto, 4), "applied_pct": round(applied, 4), "manual": inp.applied_rate_pct is not None},
        "premium": round(premium),
        "explanation": r["chain"],
        "perils_included": r["included"],
        "checks": checks,
        "verdict": verdict,
        "recommendations": recommendations(ref, inp, premium),
        "preventive_measures": preventive_measures(ref, inp, r, premium),
    }
