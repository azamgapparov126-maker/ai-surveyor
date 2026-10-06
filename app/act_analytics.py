"""
Раздел 4 акта — аналитика риска (заказчик 30.09.2026: «нужно дать более детализированную аналитику»).

Второго калькулятора здесь нет. Всё считают существующие модули, отсюда они только вызываются:
  risk_analytics.analyze (уже посчитан в act_extras.ra_context) — риски с долей в нетто-ставке, балл 0–100 и его
      составляющие, сценарии PML/EML/MFL, лимит удержания, внешняя статистика региона (risk_stats.external_block);
      analyze с изменённым входом — сценарии «что если» (защита, сейсмозона);
  risk_analytics.engine_factors / _product_for / _term_days — тот же вход движка, что у analyze;
  engine.calculate / rate_for / factors_for / min_rate / premium_of — состав технической ставки, вклад каждого
      фактора (по цепочке движка), чувствительность (та же цепочка с другим значением фактора);
  act_extras._engine_multiplier (franchise.what_if) и act_extras.apply_multiplier — таблица франшиз как множитель
      к премии акта, не ниже минимума продукта;
  market_picture.picture — рыночная ставка и убыточность класса по отчётам НАПП со ссылкой на источник.

Правила расчёта акта не меняются: тариф акта = ставка тарифной политики × поправка по уровню, не ниже минимума
(act_engine.rate); техническая ставка движка — справка рядом. Внешние данные — только из базы (market_stats,
stat_series), в сеть модуль не ходит. Возвращает коды и числа; слова — app/act_texts.py и act.py (три языка).
Все пороги и доли — экспертные (calibrated = 0).
"""
import math
import re
from datetime import date
from typing import Optional

from . import act_engine as ae
from . import act_extras as ax
from . import db
from . import engine
from . import franchise as frm
from . import market_picture as mp
from . import risk_analytics as ra

CALIBRATED = 0
# уровень отдельного риска: произведение множителей факторов, которые на него действуют (экспертно)
PERIL_LEVEL = {"low_max": 0.95, "high_min": 1.2}
# что может сделать страхователь (меры); остальные факторы — данные, их можно только уточнить
CONTROLLABLE = ("protection", "antitheft", "spec_guard", "spec_site", "spec_operator", "drivers")
SKIP_SENS = ("franchise", "veh_type")          # франшиза — отдельной таблицей; тип ТС не «меняется»
MAX_SENS = 5
MAX_SENS_DOWN = 3
SENS_MIN_PCT = 0.5                             # меньше 0,5 % — эффект не показывается
FRANCHISE_VARIANTS = (0.5, 1.0, 2.0, 5.0)
PROT_WHATIF = {"8": ("alarm", "alarm_guard", "sprinkler"), "9": ("alarm", "alarm_guard", "sprinkler"),
               "3": ("immo", "tracker")}
SEISMIC_WHATIF = (7, 8, 9)
# показатели региона, которые говорят только о зданиях (жилой фонд): в балл — только у зданий и складов
HOUSING_ONLY = ("vulnerable_housing",)
BUILDING_GROUPS = ("property",)
LEVEL5 = {"Низкий": "low", "Умеренный": "moderate", "Повышенный": "elevated", "Высокий": "high",
          "Критический": "critical"}


def _r(x, n=4):
    return None if x is None else round(float(x), n)


# ================================================================================================
#  Вход движка — тот же, что у risk_analytics.analyze
# ================================================================================================

def engine_input(ref, ctx: dict, th: dict) -> Optional[engine.Input]:
    """engine.Input на тех же данных, на которых analyze посчитал акт (после подстановок быстрого режима)."""
    if not ctx.get("ok"):
        return None
    m, op = ctx["must"], ctx["optional"]
    classes = ra.parse_classes(m.get("class_code"))
    if not classes:
        return None
    cls = classes[0]
    factors, _notes = ra.engine_factors([cls], m, op, th, date.today())
    prod, _note = ra._product_for(cls, m, ref)
    pname = (ref.products.get(prod) or {}).get("name", "")
    return engine.Input(product_code=prod or "", class_code=cls, object_type=m.get("object_type") or "",
                        value_amount=float(m["object_value"]), sum_insured=float(m["sum_insured"]),
                        term_days=ra._term_days(float(m.get("term_months") or 12)), factors=dict(factors),
                        payer_type=op.get("payer_type"), takaful=pname.startswith("Такафул"))


def _alt(inp: engine.Input, **factors) -> engine.Input:
    alt = engine.Input(**{**inp.__dict__})
    alt.factors = {**inp.factors, **factors}
    alt.applied_rate_pct = None
    return alt


def _factor_name(ref, f: str) -> str:
    return next((v["factor_name"] for (ff, _o), v in ref.coefficients.items() if ff == f), f)


def _options(ref, f: str) -> list:
    return [o for (ff, o) in ref.coefficients if ff == f]


def _source(f: str, sources: dict, set_: bool) -> str:
    """Откуда значение фактора: input | document | photo | plate | text | kind | default | not_set."""
    if not set_:
        return "not_set"
    key = {"veh_type": "vehicle_type", "veh_age": "year", "seismic": "seismic_zone", "antitheft": "protection",
           "loss_history": "losses_3y"}.get(f, f)
    src = sources.get(key)
    if src:
        return "document" if src == "document_ai" else src
    return "default" if f in ("construction", "activity", "veh_type") else "input"


# ================================================================================================
#  2. Факторы: значение, источник, вклад в техническую ставку
# ================================================================================================

def factors(ref, inp: engine.Input, r: dict, sources: dict, S: float, term: int) -> list:
    """
    Все факторы класса из справочника. Вклад — по цепочке движка (как в engine.rate_for): каждый множитель
    применяется к нетто-ставке после предыдущих, вклад пересчитан в брутто (× рисковая и катастрофическая
    надбавки ÷ (1 − нагрузка)) — поэтому вклады складываются ровно в техническую ставку сверх базовой.
    Премия — по технической ставке за срок акта: п. п. / 100 × сумма × дни / 365.
    """
    net = r["net_pct"]
    g = r["gross_pct"] / net if net else 0.0
    running = r["base_pct"] * max(r["share"], 0.001)
    included = set(r["included"])
    out = []
    for f in engine.factors_for(ref, inp):
        opt = inp.factors.get(f)
        coef = ref.coefficients.get((f, opt or ""))
        if coef:
            applies = f != "seismic" or "earthquake" in included
            m = float(coef["multiplier"]) if applies else 1.0
            d_net = running * (m - 1)
            running *= m
            pp = d_net * g
            out.append({"factor": f, "option": opt, "name_ru": coef["factor_name"], "option_ru": coef["name"],
                        "status": "set", "applies": applies, "multiplier": m,
                        "direction": "up" if m > 1 + 1e-9 else ("down" if m < 1 - 1e-9 else "neutral"),
                        "effect_pct": _r((m - 1) * 100, 1), "rate_pp": _r(pp),
                        "premium_effect": round(engine.premium_of(pp, S, term)),
                        "source": _source(f, sources, True), "calibrated": CALIBRATED})
        elif f == "franchise":
            # франшиза — условие договора, а не неизвестный признак: в акте её нет, множитель 1 («без франшизы»)
            f0 = ref.coefficients.get(("franchise", "f0")) or {}
            out.append({"factor": f, "option": "f0", "name_ru": _factor_name(ref, f), "option_ru": f0.get("name"),
                        "status": "terms", "applies": True, "multiplier": 1.0, "direction": "neutral",
                        "effect_pct": 0.0, "rate_pp": 0.0, "premium_effect": 0, "source": "act_terms",
                        "calibrated": CALIBRATED})
        else:
            out.append({"factor": f, "option": None, "name_ru": _factor_name(ref, f), "option_ru": None,
                        "status": "not_set", "applies": True, "multiplier": 1.0, "direction": "unknown",
                        "effect_pct": 0.0, "rate_pp": 0.0, "premium_effect": 0, "source": "not_set",
                        "calibrated": CALIBRATED})
    return out


# ================================================================================================
#  «Что изменит ставку» — чувствительность, посчитанная движком
# ================================================================================================

def sensitivity(ref, inp: engine.Input, rows: list, rate_res: dict, S: float, term: int,
                statutory: bool) -> list:
    """
    Для каждого фактора — другое значение из справочника и техническая ставка движка с ним (engine.rate_for).
    Меры страхователя (защита, охрана, условия эксплуатации) — лучший вариант; данные, которые приняты по
    умолчанию или не указаны, — лучший и худший варианты («если окажется …»). Эффект мер на премию акта —
    как у мероприятий: отношение ставок движка × ставка акта, не ниже минимума продукта.
    """
    g0 = engine.rate_for(ref, inp)["gross_pct"]
    if not g0:
        return []
    downs, ups = [], []
    for row in rows:
        f = row["factor"]
        if f in SKIP_SENS:
            continue
        measure = f in CONTROLLABLE
        unknown = row["status"] == "not_set" or row["source"] == "default"
        if not measure and not unknown:
            continue
        best = worst = None
        for o in _options(ref, f):
            if o == row["option"] or o.endswith("_na"):
                continue
            g1 = engine.rate_for(ref, _alt(inp, **{f: o}))["gross_pct"]
            if best is None or g1 < best[1]:
                best = (o, g1)
            if worst is None or g1 > worst[1]:
                worst = (o, g1)
        for pick, direction in ((best, "down"), (worst, "up")):
            if not pick:
                continue
            o, g1 = pick
            d = (g1 / g0 - 1) * 100
            if (direction == "down" and d > -SENS_MIN_PCT) or (direction == "up" and (d < SENS_MIN_PCT or not unknown)):
                continue
            item = {"factor": f, "option_from": row["option"], "option_to": o,
                    "option_to_ru": (ref.coefficients.get((f, o)) or {}).get("name"),
                    "name_ru": row["name_ru"], "kind": "measure" if measure and direction == "down" else "clarify",
                    "direction": direction, "tech_before": _r(g0), "tech_after": _r(g1),
                    "delta_pct": _r(d, 1), "delta_pp": _r(g1 - g0),
                    "tech_premium_delta": round(engine.premium_of(g1 - g0, S, term)),
                    "ratio": round(g1 / g0, 6), "act_rate_after": None, "act_premium_after": None,
                    "act_premium_delta": None, "act_floored": False, "calibrated": CALIBRATED}
            if direction == "down" and measure and not statutory and rate_res.get("applied_pct") is not None:
                rate, prem, floored = ax.apply_multiplier(rate_res, g1 / g0, S)
                item.update(act_rate_after=rate, act_premium_after=prem,
                            act_premium_delta=prem - rate_res["premium"] if rate_res.get("premium") is not None
                            else None, act_floored=floored)
            (downs if direction == "down" else ups).append(item)
    downs.sort(key=lambda x: (x["kind"] != "measure", x["delta_pct"]))
    ups.sort(key=lambda x: -x["delta_pct"])
    picked = downs[:MAX_SENS_DOWN]
    picked += ups[:MAX_SENS - len(picked)]
    if len(picked) < MAX_SENS:
        picked += downs[MAX_SENS_DOWN:MAX_SENS_DOWN + MAX_SENS - len(picked)]
    return picked


# ================================================================================================
#  1. Разбор по рискам
# ================================================================================================

def _relevant(f: str, peril: str) -> bool:
    return f not in ra.FACTOR_PERILS or peril in ra.FACTOR_PERILS[f]


def risks(an: dict, rows: list, sens: list, source: str = "perils") -> dict:
    """Риски класса с долей в нетто-ставке (risk_analytics) и уровнем по каждому риску (экспертное правило).
    source = template — риски и доли из шаблона класса (app/class_templates.py): в справочнике perils класса нет,
    доли экспертные (приложение А), уровень считается тем же правилом по факторам класса."""
    items = []
    for rk in an.get("risks") or []:
        code = rk["code"]
        whole = code.startswith("class")
        rel = [x for x in rows if whole or _relevant(x["factor"], code)]
        drivers = [x for x in rel if x["status"] == "set" and x["applies"]]
        mult = 1.0
        for x in drivers:
            mult *= x["multiplier"]
        unknown = [x["factor"] for x in rel if x["status"] == "not_set" or x["source"] == "default"]
        moved = [x for x in drivers if abs(x["multiplier"] - 1) > 1e-9]
        key = max(moved, key=lambda x: abs(math.log(x["multiplier"]))) if moved else None
        seismic_row = next((x for x in rows if x["factor"] == "seismic"), None)
        if code == "earthquake" and seismic_row and seismic_row["status"] == "not_set":
            level, reason = "moderate", {"code": "zone_unknown"}
        else:
            level = "low" if mult <= PERIL_LEVEL["low_max"] + 1e-9 else \
                "high" if mult >= PERIL_LEVEL["high_min"] - 1e-9 else "moderate"
            reason = {"code": "by_factor", "factor": key["factor"], "option": key["option"],
                      "multiplier": key["multiplier"], "assumed": key["source"] == "default"} \
                if key else {"code": "all_average"}
        helps = [{"factor": s["factor"], "option_to": s["option_to"], "delta_pct": s["delta_pct"]}
                 for s in sens if s["direction"] == "down" and s["kind"] == "measure"
                 and (whole or _relevant(s["factor"], code))]
        items.append({"code": code, "name_ru": rk["name"], "class_code": rk["class_code"],
                      "catastrophic": bool(rk.get("catastrophic")), "share_of_net_pct": rk["share_of_net_pct"],
                      "whole_class": whole, "multiplier": _r(mult), "level": level, "reason": reason,
                      "raises": [{"factor": x["factor"], "option": x["option"], "multiplier": x["multiplier"],
                                  "assumed": x["source"] == "default"}
                                 for x in drivers if x["multiplier"] > 1 + 1e-9],
                      "lowers": [{"factor": x["factor"], "option": x["option"], "multiplier": x["multiplier"],
                                  "assumed": x["source"] == "default"}
                                 for x in drivers if x["multiplier"] < 1 - 1e-9],
                      "unknown": unknown, "measures": helps, "calibrated": CALIBRATED})
        if rk.get("labels"):
            items[-1]["labels"] = dict(rk["labels"])
    total = round(sum(i["share_of_net_pct"] for i in items), 1)
    # доли risk_analytics округлены до 0,1 п. п.: сумма может отличаться от 100 % не больше, чем на 0,05 на риск
    rounding = bool(items) and abs(total - 100) > 1e-9 and abs(total - 100) <= 0.05 * len(items) + 1e-9
    out = {"available": bool(items), "items": items, "total_pct": total, "rounding": rounding,
           "whole_class": any(i["whole_class"] for i in items), "thresholds": dict(PERIL_LEVEL),
           "calibrated": CALIBRATED}
    if source == "template":
        out["source"] = "template"
    return out


# ================================================================================================
#  3. Состав тарифа
# ================================================================================================

def tariff(ref, inp: engine.Input, calc: dict, r: dict, rate_res: dict, S: float, term: int,
           market_rate: Optional[float], object_type_source: Optional[str] = None) -> dict:
    """Базовая нетто → риски → коэффициенты → надбавки → нагрузка → техническая ставка; рядом — ставка акта."""
    if not any(c == inp.class_code for (c, _o) in ref.base_rates):
        return {"available": False, "reason": "no_base", "class_code": inp.class_code, "calibrated": CALIBRATED}
    rates = calc["rates"]
    base, share, net = r["base_pct"], r["share"], r["net_pct"]
    gross = r["gross_pct"]
    perils = {c: p for c, p in ref.perils.items() if p["class_code"] == inp.class_code}
    excluded = [c for c in perils if c not in set(r["included"])]
    load = 1 - (net + r["risk_pct"] + r["cat_pct"]) / gross if gross else None
    act = rate_res.get("applied_pct")
    tech = rates["technical_pct"]
    out = {"available": True, "reason": None, "class_code": inp.class_code, "product_code": inp.product_code,
           "object_type": inp.object_type, "object_type_source": object_type_source,
           "base_kind": "object_type" if (inp.class_code, inp.object_type) in ref.base_rates else "class_average",
           "base_net_pct": _r(base), "perils_share": _r(share, 3), "excluded": excluded,
           "factors_mult": _r(net / (base * max(share, 0.001)), 4) if base else None,
           "net_pct": rates["net_pct"], "risk_load_rate": engine.RISK_LOAD, "risk_load_pct": rates["risk_load_pct"],
           "cat_load_pct": rates["cat_load_pct"], "load_share": _r(load, 4), "takaful": bool(inp.takaful),
           "technical_pct": tech, "technical_premium": round(engine.premium_of(tech, S, term)),
           "engine_min_pct": rates["min_pct"], "engine_applied_pct": rates["applied_pct"],
           "min_pct": rate_res.get("min_pct"), "act_mode": rate_res.get("mode"),
           "base_source": rate_res.get("base_source"),
           "policy_rate_pct": rate_res.get("base_pct") if rate_res.get("base_source") == "product_rate" else None,
           "act_base_pct": rate_res.get("base_pct"), "adj_pct": rate_res.get("adj_pct"),
           "act_calc_pct": rate_res.get("calc_pct"), "act_rate_pct": act, "min_applied": bool(rate_res.get("min_applied")),
           "act_premium": rate_res.get("premium"), "term_days": term, "market_rate_pct": market_rate,
           "act_vs_technical": _r(act / tech, 3) if act and tech else None,
           "act_vs_market_pct": _r((act / market_rate - 1) * 100, 1) if act and market_rate else None,
           "technical_vs_market_pct": _r((tech / market_rate - 1) * 100, 1) if tech and market_rate else None,
           "calibrated": CALIBRATED}
    return out


# ================================================================================================
#  4. Сценарии подробно, «что если», удержание
# ================================================================================================

def _act_amounts(an: dict) -> dict:
    return {s: float(an["scenarios"][src]["amount"]) for s, src in ax.ACT_FROM_RA.items()}


def scenarios(con, ctx: dict, scen: dict, S: float, V: float, th: dict) -> dict:
    """Формула каждого сценария с числами (доля × база, какой риск и какая база) и варианты «что если»."""
    if not scen or not scen.get("available") or not ctx.get("ok"):
        return {"available": False, "reason": (scen or {}).get("reason") or "sc_na_error", "items": [],
                "whatif": [], "calibrated": CALIBRATED}
    if scen.get("source") == "template":
        # простое правило шаблона класса (act_extras.simple_scenarios): формула — одна строка на сценарий
        items = []
        for s in ("PML", "EML", "MFL"):
            it = scen["items"][s]
            items.append({"name": s, "source_scenario": s, "amount": round(it["amount"]), "pct": it["pct"],
                          "rule": scen["rule"], "source": "template", "k": 1.0,
                          "parts": [{"peril": "template", "amount": round(it["amount"]),
                                     "what": dict(it.get("what_text") or {}), "formula": it.get("formula")}],
                          "chosen": "template", "protection": None, "protection_assumed": False, "bi_loss": 0,
                          "calibrated": CALIBRATED})
        return {"available": True, "reason": None, "rule": scen["rule"], "source": "template", "items": items,
                "whatif": [], "calibrated": CALIBRATED}
    an, op = ctx["analysis"], ctx["optional"]
    rule = scen["rule"]
    k = min(S, V) / V if V else 1.0
    comp = op.get("compartments") if isinstance(op.get("compartments"), dict) else {}
    C = ra._to_float(comp.get("largest_value")) if comp else None
    base_kind = "compartment" if C else "whole"
    C = C or V
    prot = op.get("protection") or None
    zone = ra._seismic_zone(op.get("seismic_zone"))
    items = []
    for s in ("PML", "EML", "MFL"):
        src = ax.ACT_FROM_RA[s]
        b = an["scenarios"][src]
        amount = float(b["amount"])
        parts, chosen = [], None
        if rule == "property8":
            fire = float(b.get("fire_loss") or 0.0)
            fb, fk = (C, base_kind)
            if k and fb and fire / (k * fb) > 1 + 1e-9:          # MFL при переходе огня — весь объект
                fb, fk = V, "whole_spread"
            parts.append({"peril": "fire", "base": round(fb), "base_kind": fk,
                          "share": _r(fire / (k * fb), 4) if k and fb else None, "amount": round(fire)})
            eq = b.get("earthquake_loss")
            if eq is not None:
                parts.append({"peril": "earthquake", "base": round(V), "base_kind": "site", "zone": zone,
                              "zone_assumed": zone is None, "share": _r(float(eq) / (k * V), 4) if k and V else None,
                              "amount": round(float(eq))})
            else:
                parts.append({"peril": "earthquake", "amount": None, "reason": "zone_unknown"})
            chosen = "earthquake" if eq is not None and float(eq) > fire else "fire"
        elif rule == "property9":
            parts.append({"peril": "damage9", "base": round(C), "base_kind": base_kind,
                          "share": _r(amount / (k * C), 4) if k and C else None, "amount": round(amount)})
            chosen = "damage9"
        else:
            base = min(S, V)
            parts.append({"peril": "vehicle", "base": round(base), "base_kind": "unit",
                          "share": _r(amount / base, 4) if base else None, "amount": round(amount)})
            chosen = "vehicle"
        items.append({"name": s, "source_scenario": src, "amount": round(amount),
                      "pct": scen["items"][s]["pct"], "rule": rule, "k": _r(k, 4), "parts": parts,
                      "chosen": chosen, "protection": prot, "protection_assumed": prot is None and rule != "vehicle",
                      "bi_loss": round(float(b.get("bi_loss") or 0)), "calibrated": CALIBRATED})
    # «что если»: тот же analyze с другим значением защиты или сейсмозоны
    whatif = []
    limit = (scen.get("retention") or {}).get("limit")
    cls = scen.get("class_code")
    variants = [("protection", p) for p in PROT_WHATIF.get(cls, ()) if p != prot]
    if rule == "property8" and zone is None:
        variants += [("seismic_zone", z) for z in SEISMIC_WHATIF]
    for key, val in variants:
        try:
            an2 = ra.analyze(con, ctx["must"], {**op, key: val}, thresholds=th,
                             assumptions=an.get("assumptions"))
        except Exception as e:                  # вариант не посчитан — остальные остаются
            whatif.append({"change": key, "value": val, "ok": False, "error": type(e).__name__})
            continue
        if not an2.get("ok"):
            whatif.append({"change": key, "value": val, "ok": False, "error": "validation"})
            continue
        a2 = _act_amounts(an2)
        whatif.append({"change": key, "value": val, "ok": True,
                       "pml": round(a2["PML"]), "eml": round(a2["EML"]), "mfl": round(a2["MFL"]),
                       "pml_delta": round(a2["PML"] - items[0]["amount"]),
                       "eml_delta": round(a2["EML"] - items[1]["amount"]),
                       "mfl_delta": round(a2["MFL"] - items[2]["amount"]),
                       "eml_excess": round(max(a2["EML"] - limit, 0)) if limit is not None else None,
                       "calibrated": CALIBRATED})
    return {"available": True, "reason": None, "rule": rule, "items": items, "whatif": whatif,
            "calibrated": CALIBRATED}


def retention(ctx: dict, scen: dict, S: float) -> dict:
    """
    Лимит удержания против EML и MFL акта — ОЦЕНКА, а не факт (30.09.2026): лимит на один риск по Положению
    1806, п. 15 = 20 % × (собственные средства + резервы) — цифры из company_financials, пока временные (до данных
    бухгалтерии); лимит по таблице линий класса (capacity.retention_table) — внутреннее экспертное правило, не
    норма. Расчётное удержание — меньшее из двух. Нет цифр — что ввести в админке.
    """
    ret = (scen or {}).get("retention") or {}
    items = (scen or {}).get("items") or {}
    eml = (items.get("EML") or {}).get("amount")
    mfl = (items.get("MFL") or {}).get("amount")
    an_ret = ((ctx.get("analysis") or {}).get("retention") or {}) if ctx.get("ok") else {}
    out = {"known": bool(ret.get("known")), "legal_ref": ret.get("legal_ref") or "Положение № 1806, п. 15",
           "eml": round(eml) if eml is not None else None, "mfl": round(mfl) if mfl is not None else None,
           "sum_insured": round(S) if S is not None else None, "estimate": True,
           "funds_source": "company_financials", "line_rule": "capacity.retention_table",
           "calibrated": CALIBRATED}
    if not ret.get("known"):
        out.update(verdict="unknown", need=[d.get("code") for d in an_ret.get("data_request") or []] or
                   ["own_funds", "reserves"])
        return out
    lpr = ret.get("limit_per_risk")
    out.update(limit=ret.get("limit"), limit_per_risk=lpr, line_retention=ret.get("line_retention"),
               line_class=ret.get("line_class"), own_funds=ret.get("own_funds"), reserves=ret.get("reserves"),
               status=ret.get("status"), eml_excess=ret.get("eml_excess"), mfl_excess=ret.get("mfl_excess"),
               sum_within_limit_20=(S <= lpr) if lpr is not None else None,
               verdict="eml_excess" if ret.get("eml_excess") else ("mfl_excess" if ret.get("mfl_excess") else "within"))
    return out


# ================================================================================================
#  5. Балл риска 0–100 (справочно)
# ================================================================================================

def housing_excluded(an: dict, group: Optional[str]) -> list:
    """
    Показатели жилого фонда (доля глинобитного жилья) говорят о зданиях: для оборудования, транспорта и
    прочего они в балл не входят — только для зданий и складов (группа property). Возвращает их id.
    """
    if group in BUILDING_GROUPS:
        return []
    ext = an.get("external_stats") or {}
    return [i["id"] for i in ext.get("indicators") or [] if i.get("used_in_score") and i["id"] in HOUSING_ONLY]


def _ext_reason(an: dict, excluded: list) -> Optional[str]:
    """Почему составляющая «внешняя статистика» не учтена: код для подписи на языке акта."""
    ext = an.get("external_stats") or {}
    inds = ext.get("indicators") or []
    if not inds:
        return "no_class_data"
    if not ext.get("region_key") or ext.get("region_key") == "total":
        return "region_unknown"
    scored = [i for i in inds if i.get("used_in_score") and i["id"] not in excluded]
    if not scored:
        return "kind" if excluded else "no_regional"
    return None


def score(an: dict, act_level: str, th: dict, excluded: Optional[list] = None) -> dict:
    """
    Балл 0–100 модуля risk_analytics с его составляющими. Если показатели жилого фонда к виду объекта не
    относятся (housing_excluded), составляющая «внешняя статистика» пересчитывается без них (или не
    учитывается), а балл — той же формулой модуля: сумма баллов × вес / сумма весов учтённых составляющих.
    """
    lv = an.get("level") or {}
    excluded = list(excluded or [])
    ret_known = (an.get("retention") or {}).get("retention_limit") is not None
    ext = an.get("external_stats") or {}
    reason = _ext_reason(an, excluded)
    comps = []
    for c in lv.get("components") or []:
        comps.append({"code": c["code"], "name_ru": c["name"], "points": c["points"], "value": c.get("value"),
                      "weight": c.get("weight_norm"), "raw_weight": c.get("weight") or 0,
                      "contribution": c.get("contribution"),
                      "applicable": bool(c.get("applicable")) and bool(c.get("weight")),
                      "basis": ("retention" if ret_known else "sum") if c["code"] == "mfl_retention" else None,
                      "reason": reason if c["code"] == "external_stats" and not c.get("applicable") else None,
                      "calibrated": CALIBRATED})
    sc, name = lv.get("score"), lv.get("level")
    if excluded and comps:
        keep = [i for i in ext.get("indicators") or [] if i.get("used_in_score") and i["id"] not in excluded]
        for c in comps:
            if c["code"] != "external_stats":
                continue
            if keep:
                c["points"] = round(sum(float(i["points"]) for i in keep) / len(keep), 1)
                c["value"] = round(sum(float(i["level_vs_country"]["ratio"]) for i in keep) / len(keep), 3)
            else:
                c.update(applicable=False, points=0, value=None, reason="kind")
        active = [c for c in comps if c["applicable"] and c["raw_weight"] > 0]
        wsum = sum(c["raw_weight"] for c in active) or 1.0
        total = 0.0
        for c in comps:
            on = c in active
            c["weight"] = round(c["raw_weight"] / wsum, 3) if on else 0.0
            c["contribution"] = round(c["points"] * c["raw_weight"] / wsum, 1) if on else 0.0
            total += c["contribution"]
        sc = round(min(100.0, max(0.0, total)), 1)
        name = ra._level_of(sc, th["level_bounds"])
        if lv.get("override"):              # запрет в проверках модуля поднимает уровень — так же, как в модуле
            need = th.get("stop_min_level")
            if need in ra.LEVEL_NAMES and ra.LEVEL_NAMES.index(name) < ra.LEVEL_NAMES.index(need):
                name = need
    for c in comps:
        c.pop("raw_weight", None)
    return {"available": bool(lv), "score": sc, "level": LEVEL5.get(name, "moderate"),
            "level_ru": name, "override": bool(lv.get("override")), "act_level": act_level,
            "housing_excluded": excluded,
            "components": comps, "bounds": list(th.get("level_bounds") or []),
            "params": {k: th.get(k) for k in ("rate_ratio", "mfl_to_retention", "mfl_pct_of_sum", "loss_ratio",
                                               "underinsurance_full_at", "external_ratio", "unknown_points")},
            "calibrated": CALIBRATED}


# ================================================================================================
#  6. Рынок (НАПП) и статистика региона (stat.uz, data.egov.uz)
# ================================================================================================

def market(con, cls: str, product_code: Optional[str], region: str, act_rate: Optional[float],
           tech: Optional[float]) -> dict:
    """Рыночная ставка и убыточность класса по НАПП (market_picture) — только чтение базы."""
    p = mp.picture(con, cls, product_code, region, our_rate_pct=act_rate)
    mk = p["market"]
    srcs = {s["id"]: s for s in p["sources"]}

    def src(sid):
        s = srcs.get(sid)
        if not s:
            return None
        return {"id": s["id"], "title_ru": s["title"], "url": s["url"], "domain": s["domain"],
                "as_of": s.get("as_of"), "slice": None}

    rate = mk.get("rate_pct")
    out = {"available": rate is not None, "class_code": cls, "row_key": mk.get("row_key"),
           "pack": mk.get("row_kind") == "пакет классов", "rate_pct": rate, "rate_date": mk.get("rate_date"),
           "months": mp.MONTHS.get((mk.get("rate_date") or "")[5:]),
           "loss_ratio_pct": mk.get("loss_ratio_pct"),
           "rate_full_year_pct": mk.get("rate_full_year_pct"),
           "loss_ratio_full_year_pct": mk.get("loss_ratio_full_year_pct"),
           "full_year": None, "act_rate_pct": act_rate, "technical_pct": tech,
           "act_vs_market_pp": _r(act_rate - rate) if act_rate is not None and rate else None,
           "act_vs_market_pct": _r((act_rate / rate - 1) * 100, 1) if act_rate is not None and rate else None,
           "tech_vs_market_pct": _r((tech / rate - 1) * 100, 1) if tech and rate else None,
           "source": src(mk.get("rate_src")), "full_year_source": src(mk.get("full_year_src")),
           "no_row": not mk.get("row_key"), "missing_quarters": list(mk.get("missing_quarters") or []),
           "calibrated": CALIBRATED}
    if out["source"]:
        out["source"]["slice"] = mk.get("rate_date")
    # комплексный продукт (01.10.2026): строка — пакет НАПП с классами продукта (точный или ближайший) или,
    # если такого пакета нет, строка класса; how — exact | nearest | class, слова — act.py
    pcx = mk.get("pack_choice")
    out["pack_choice"] = dict(pcx) if pcx else None
    if pcx:
        out["pack_classes"] = list(pcx.get("pack_classes") or [])
        prem = (mk.get("premiums") or {}).get("value")
        out["pack_premiums"] = _r(prem, 1) if prem is not None else None
    # одиночные строки классов продукта рядом с пакетом (правило проекта № 5 — ставка по каждому классу)
    out["class_rows"] = list(mk.get("class_rows") or [])
    # какая строка отчёта взята: у 8 и 9 — строка пакета «8, 9»; по данным market_stats сравниваем её объём
    # с отдельной строкой класса (market_picture.ALTERNATIVES) — почему взят пакет, видно по цифрам
    if out["pack"] and not pcx:
        prem = (mk.get("premiums") or {}).get("value")
        alt = next((a for a in mk.get("alternatives") or [] if a.get("row_key") == "cls" + str(cls)), None)
        out["pack_classes"] = [x for x in (mk.get("row_key") or "")[3:].split("_") if x]
        out["pack_premiums"] = _r(prem, 1) if prem is not None else None
        out["alt_row_key"] = alt.get("row_key") if alt else None
        out["alt_premiums"] = _r(alt.get("premiums"), 1) if alt and alt.get("premiums") is not None else None
        out["alt_share_pct"] = _r(float(alt["premiums"]) / float(prem) * 100, 1) \
            if alt and alt.get("premiums") is not None and prem else None
    fy_period = mk.get("rate_full_year_period") or ""
    digits = "".join(ch for ch in fy_period if ch.isdigit())[:4]
    out["full_year"] = int(digits) if digits else None
    return out


def napp_extra(con, region: Optional[str]) -> dict:
    """Претензии региона и рынка/INSON, подразделения INSON в регионе (НАПП) — для раздела 4 «Рынок и статистика».
    Только чтение napp_claims / napp_branches; нет листа за последний срез — блок с reason и note (пропуск).
    Вес claims_freq в поправке региона и порог малой базы подразделений act.py дописывает из настроек
    (claims_weight, branch_min_contracts)."""
    return {"available": True, "region_claims": mp.region_claims(con, region),
            "company_claims": mp.company_claims(con), "branches": mp.branches(con, region),
            "calibrated": CALIBRATED}


def stats(an: dict, excluded: Optional[list] = None) -> dict:
    """Показатели региона по классу из блока external_stats (risk_stats) — со ссылкой на набор.
    excluded — показатели жилого фонда, которые к виду объекта не относятся: показываются, но в балл не входят."""
    ext = an.get("external_stats") or {}
    excluded = list(excluded or [])
    inds = []
    for i in ext.get("indicators") or []:
        kind_out = i["id"] in excluded
        srcs = [{"name_ru": s.get("name"), "id": s.get("id"), "source": s.get("source"), "url": s.get("url"),
                 "period": s.get("period"), "fetched_at": s.get("fetched_at")} for s in i.get("src") or []]
        lv = i.get("level_vs_country") or None
        inds.append({"id": i["id"], "name_ru": i["name"], "class_code": i.get("class_code"),
                     "status": "ok" if i.get("value") is not None else "no_data",
                     "value": i.get("value"), "unit_ru": i.get("unit"), "period": i.get("period"),
                     "scope": "region" if i.get("scope") == "регион" else "republic",
                     "per_1000": i.get("per_1000"), "trend_pct": i.get("trend_pct"),
                     "vs_country": {"ratio": lv["ratio"], "diff_pct": lv["diff_pct"]} if lv else None,
                     "used_in_score": bool(i.get("used_in_score")) and not kind_out,
                     "points": None if kind_out else i.get("points"), "excluded_for_kind": kind_out,
                     "sources": srcs, "calibrated": CALIBRATED})
    return {"available": any(x["status"] == "ok" for x in inds), "region_key": ext.get("region_key"),
            "region_name_ru": ext.get("region_name"), "indicators": inds,
            "not_found": dict(ext.get("not_found") or {}),
            "applicable": bool(ext.get("applicable")) and any(x["used_in_score"] for x in inds),
            "points": (round(sum(float(x["points"]) for x in inds if x["used_in_score"])
                             / max(1, sum(1 for x in inds if x["used_in_score"])), 1)
                       if excluded and any(x["used_in_score"] for x in inds) else
                       (None if excluded else ext.get("points"))),
            "excluded_for_kind": excluded, "calibrated": CALIBRATED}


# ================================================================================================
#  7. Франшиза: таблица вариантов (справочно)
# ================================================================================================

def franchise_table(con, ctx: dict, rate_res: dict, S: float, cls: str, statutory: bool, th: dict) -> dict:
    """0,5 / 1 / 2 / 5 % (до потолка класса): множитель franchise.what_if × ставка акта, не ниже минимума."""
    if statutory:
        return {"available": False, "reason": "statutory", "rows": [], "calibrated": CALIBRATED}
    if rate_res.get("applied_pct") is None or rate_res.get("premium") is None:
        return {"available": False, "reason": "no_rate", "rows": [], "calibrated": CALIBRATED}
    if not ctx.get("ok"):
        return {"available": False, "reason": "no_engine", "rows": [], "calibrated": CALIBRATED}
    cap, _why = frm._cap([cls], th)
    P = rate_res["premium"]
    rows = []
    for pct in FRANCHISE_VARIANTS:
        if cap is not None and pct > cap + 1e-9:
            continue
        em = ax._engine_multiplier(con, ctx, pct)
        if not em.get("ok"):
            rows.append({"pct": pct, "ok": False, "amount": round(S * pct / 100)})
            continue
        rate, prem, floored = ax.apply_multiplier(rate_res, em["mult"], S)
        rows.append({"pct": pct, "ok": True, "amount": round(S * pct / 100), "multiplier": em["mult"],
                     "rate_pct": rate, "premium": prem, "saving": P - prem,
                     "saving_pct": _r((P - prem) / P * 100, 1) if P else None, "floored": floored,
                     "extrapolated": bool(em.get("extrapolated")), "calibrated": CALIBRATED})
    return {"available": True, "reason": None, "base_premium": P, "base_rate_pct": rate_res["applied_pct"],
            "cap_pct": cap, "rows": rows, "reference_only": True, "calibrated": CALIBRATED}


# ================================================================================================
#  8. Мероприятия: эффект на техническую ставку и на премию акта
# ================================================================================================

def measures_effect(meas: dict, tech: Optional[float]) -> dict:
    items, k = [], 1.0
    for it in (meas or {}).get("items") or []:
        ratio = it.get("ratio")
        if ratio:
            k *= ratio
        items.append({"code": it["code"], "ratio": ratio, "effect_pct": it.get("effect_pct"),
                      "tech_before": tech, "tech_after": _r(tech * ratio) if ratio and tech else None,
                      "tech_delta_pp": _r(tech * (ratio - 1)) if ratio and tech else None,
                      "premium_delta": it.get("premium_delta"), "calibrated": CALIBRATED})
    tot = (meas or {}).get("total") or {}
    return {"items": items, "tech_before": tech, "tech_after_all": _r(tech * k) if tech and k != 1 else None,
            "tech_ratio_all": round(k, 6) if k != 1 else None,
            "premium_before": tot.get("premium_before"), "premium_after": tot.get("premium_after"),
            "premium_delta": tot.get("delta"), "floor_applied": bool(tot.get("floor_applied")),
            "calibrated": CALIBRATED}


# ================================================================================================
#  9. Вилка ставки (01.10.2026): данные региона (stat.uz, data.egov.uz) и рынка (НАПП) для поправок
# ================================================================================================

def _policy_source(con, product_code: Optional[str], min_pct: Optional[float], min_source: Optional[str],
                   min_info: Optional[dict] = None) -> dict:
    """Источник минимальной ставки: правка администратора (минимальная ставка страховщика, app/min_rates.py),
    действующая версия тарифной политики (или ставка части из текста тарифа)."""
    if min_info and min_info.get("source") in ("admin", "import") and min_source not in ("rate_text",
                                                                                         "rate_text_common"):
        return {"kind": "insurer_min", "title_ru": min_info.get("label_ru"), "url": None,
                "as_of": min_info.get("effective_from"), "product_code": product_code, "part_text": False,
                "row": {k: min_info.get(k) for k in ("source", "effective_from", "note", "document_ref", "name")}}
    row = None
    if product_code and min_pct is not None:
        rows = db.rows(con, "SELECT v.level, v.name, v.document_ref, v.effective_from FROM min_rates m "
                            "JOIN tariff_versions v ON v.id = m.tariff_version_id WHERE m.product_code=? "
                            "AND abs(m.min_rate_pct - ?) < 1e-9 ORDER BY v.effective_from DESC LIMIT 1",
                       product_code, float(min_pct))
        row = rows[0] if rows else None
    if row is None:
        rows = db.rows(con, "SELECT level, name, document_ref, effective_from FROM tariff_versions "
                            "WHERE level='компания' ORDER BY effective_from DESC LIMIT 1")
        row = rows[0] if rows else {}
    title = " — ".join(x for x in (row.get("name"), row.get("document_ref")) if x) or "тарифная политика"
    return {"kind": "regulator" if row.get("level") == "регулятор" else "policy", "title_ru": title, "url": None,
            "as_of": row.get("effective_from"), "product_code": product_code,
            "part_text": min_source in ("rate_text", "rate_text_common")}


def _act_ref(text: Optional[str]) -> Optional[str]:
    """«0,4% (ПКМ №532)» → «ПКМ №532»: ставка показывается отдельно, в источнике — только нормативный акт."""
    if not text:
        return None
    m = re.search(r"\(([^()]+)\)", str(text))
    return m.group(1).strip() if m else str(text).strip()


def _stat_source(i: dict) -> Optional[dict]:
    s = (i.get("src") or [None])[0]
    if not s:
        return None
    return {"kind": "stat", "title_ru": "%s — %s" % (s.get("source") or "stat.uz", s.get("name") or s.get("id")),
            "url": s.get("url") or s.get("data_url"), "as_of": s.get("period"), "fetched_at": s.get("fetched_at"),
            "dataset": s.get("id")}


def factor_stats(con, fa: Optional[dict], region: Optional[str]) -> Optional[dict]:
    """
    Фон региона к факторам объекта (stat_ref групп шаблона 1.4.1, 02.10.2026): у каждого применённого фактора со
    ссылкой на наборы stat.uz — блок stat (act_engine.factor_stat). Только чтение таблицы stat_series базы, в сеть
    не ходит; набор не загружен или регион не опознан — stat.available = false с причиной. Коэффициенты и
    множитель не меняются. Ошибка чтения базы — тоже available = false (акт формируется дальше).
    """
    from . import act_engine as ae
    from . import risk_stats as rs
    from . import stat_sources as ss
    items = [a for a in (fa or {}).get("applied") or [] if a.get("stat_ref")]
    if not items:
        return fa
    key = rs._region_key(region) if region else None
    name = ss.REGION_NAMES_RU.get(key) if key else None
    ids = sorted({r["dataset_id"] for a in items for r in a["stat_ref"]} | set(ae.WALL_FUND))
    series = {}
    err = None
    if key:
        try:
            q = ("SELECT dataset_id, period, value, unit, url, fetched_at FROM stat_series WHERE region=? AND "
                 "value IS NOT NULL AND dataset_id IN (%s)" % ",".join("?" * len(ids)))
            for d, per, v, unit, url, fetched in con.execute(q, [key] + ids).fetchall():
                if re.fullmatch(r"\d{4}", str(per)):           # только годовые значения (наборы — на конец года)
                    series.setdefault(d, {})[per] = {"value": v, "unit": unit, "url": url, "fetched_at": fetched}
        except Exception as e:                                  # таблицы нет (старая база) — фон не показываем
            err = type(e).__name__
    for a in items:
        if err:
            a["stat"] = {"available": False, "reason": "db_error", "reason_text": f"stat_series не прочитана ({err})",
                         "dataset": [r["dataset_id"] for r in a["stat_ref"]], "note": ae.STAT_NOTE,
                         "calibrated": ae.CALIBRATED}
        else:
            a["stat"] = ae.factor_stat(a["stat_ref"], series, key, name, ss.DATASETS)
    return fa


def fork_data(con, *, cls: str, region: str, group: Optional[str], fs: dict, product_code: Optional[str] = None,
              min_pct: Optional[float] = None, min_source: Optional[str] = None,
              statutory_ref: Optional[str] = None, min_info: Optional[dict] = None) -> dict:
    """
    Данные для поправок вилки ставки — только чтение базы (stat_series, market_stats, tariff_versions):
      region — показатели региона для класса и вида объекта (risk_stats, отношение «регион / республика»)
               и поправка ae.fork_region;
      market — рыночная ставка и убыточность класса (market_picture, НАПП) с датой среза и ссылкой;
      sources — источники отметок вилки.
    """
    from . import act_engine as ae
    from . import risk_stats as rs
    key = rs._region_key(region) if region else None
    known = bool(key) and key != "total"
    ids, other = ae.fork_indicator_ids(fs, cls, group)
    sens = float(fs["region"]["sensitivity"])
    wts = fs["region"].get("weights") or {}
    inds = []
    for iid in ids + other:
        spec = rs.BY_ID.get(iid)
        if not spec:
            continue
        for_kind = iid in ids
        try:
            i = rs._one(con, spec, cls, key, {}) if for_kind else {"name": spec["name"], "src": []}
        except Exception as e:                  # один показатель не прочитан — остальные считаются
            i = {"name": spec["name"], "src": [], "error": type(e).__name__}
        lv = i.get("level_vs_country") or {}
        ratio = lv.get("ratio")
        why = None
        if not for_kind:
            why = "kind"
        elif not known:
            why = "region_unknown"
        elif i.get("value") is None:
            why = "no_data"
        elif ratio is None:
            why = "no_regional"
        elif float(wts.get(iid, 1.0)) == 0:
            # вес 0 в настройке (claims_freq по умолчанию): значение и сравнение показываются, в поправку не входят
            why = "weight_zero"
        used = why is None
        per1000 = spec["kind"] == rs.COUNT_PC
        src_i = _stat_source(i)
        if spec["kind"] == rs.NAPP_CLAIMS and src_i:
            # частота претензий — из отчёта НАПП (листы 3.5 и 3.4), подпись источника — act.py (rf_src_napp_claims)
            src_i = dict(src_i, kind="napp_claims", as_of=i.get("date") or src_i.get("as_of"))
        inds.append({"id": iid, "name_ru": i.get("name") or spec["name"], "kind": spec["kind"],
                     "unit_ru": ("%s на 1 000 жителей" % spec["unit"]) if per1000 else spec["unit"],
                     "region_value": lv.get("region"), "country_value": lv.get("country"), "ratio": ratio,
                     "period": i.get("period"), "value": i.get("value"), "value_unit_ru": spec["unit"],
                     "effect_pct": round((ratio - 1) * sens * 100, 2) if used else None,
                     "weight": float(wts.get(iid, 1.0)),
                     "used": used, "why": why, "source": src_i,
                     "scope": i.get("scope"), "calibrated": CALIBRATED})
    applicable = [x for x in inds if x["why"] != "kind"]
    reg = ae.fork_region(applicable, fs, known, bool(ids or other))
    reg.update(region_key=key, region_name_ru=(mp.resolve_region(region)[1] if region else None),
               region_requested=region or None, indicators=inds,
               not_found_ru=rs.NOT_FOUND.get(str(cls)))
    # рынок класса (НАПП)
    mk = {"rate_pct": None, "rate_date": None, "loss_ratio_pct": None, "loss_ratio_full_year_pct": None,
          "row_key": None, "source": None, "pack": False}
    try:
        # комплексный продукт: поправка рынка — по пакету НАПП продукта (и для частей договора), а не по классу
        p = mp.picture(con, cls, product_code, None)
        m = p["market"]
        srcs = {s["id"]: s for s in p["sources"]}
        s = srcs.get(m.get("rate_src")) or {}
        mk.update(rate_pct=m.get("rate_pct"), rate_date=m.get("rate_date"), loss_ratio_pct=m.get("loss_ratio_pct"),
                  loss_ratio_full_year_pct=m.get("loss_ratio_full_year_pct"),
                  full_year_period=m.get("rate_full_year_period"), row_key=m.get("row_key"),
                  pack=m.get("row_kind") == "пакет классов", pack_choice=m.get("pack_choice"),
                  class_rows=list(m.get("class_rows") or []),
                  source={"kind": "napp", "title_ru": s.get("title") or mp.NAPP_TITLE,
                          "url": s.get("url") or mp.NAPP_PAGE, "as_of": m.get("rate_date")}
                  if m.get("rate_pct") is not None else None)
    except Exception as e:                      # рынок не прочитан — поправки рынка нет, причина видна
        mk["error"] = type(e).__name__
    policy = _policy_source(con, product_code, min_pct, min_source, min_info)
    stat_srcs = [x["source"] for x in inds if x["used"] and x["source"]]
    adjusted_src = {"kind": "adjusted", "title_ru": "; ".join(dict.fromkeys(
        [s["title_ru"] for s in stat_srcs] + ([mk["source"]["title_ru"]] if mk.get("source") else []))) or None,
        "url": (stat_srcs[0]["url"] if stat_srcs else (mk.get("source") or {}).get("url")),
        "as_of": max([s["as_of"] for s in stat_srcs if s.get("as_of")] or [None], key=lambda x: x or ""),
        "items": stat_srcs + ([mk["source"]] if mk.get("source") else [])}
    sources = {"policy": policy, "policy_act": dict(policy, kind="policy_act"), "adjusted": adjusted_src,
               "market": mk.get("source"), "request": {"kind": "request", "title_ru": None, "url": None,
                                                        "as_of": None},
               "contract": {"kind": "contract", "title_ru": None, "url": None, "as_of": None},
               "technical": {"kind": "technical", "title_ru": None, "url": None, "as_of": None},
               "statutory": {"kind": "statutory", "title_ru": _act_ref(statutory_ref), "url": None, "as_of": None}}
    return {"region": reg, "market": mk, "sources": sources, "calibrated": CALIBRATED}


# ================================================================================================
#  Сборка
# ================================================================================================

def build(con, ctx: dict, *, cls: str, product_code: Optional[str], region: str, S: float, V: float,
          term_days: int, rate_res: dict, scen: dict, meas: dict, act_level: str, statutory: bool,
          th: dict, group: Optional[str] = None, tpl_risks: Optional[list] = None,
          veh_group: Optional[str] = None) -> dict:
    """
    Аналитика раздела 4: коды и числа (язык не важен). Каждый блок считается отдельно — сбой одного не роняет
    остальные (блок получает available = false, reason = error). Без старого движка — available = false.
    tpl_risks — риски шаблона класса (class_templates.template_risks), когда в справочнике perils класса нет:
    вместо одной строки «весь класс» — экспертные доли шаблона.
    """
    out = {"available": False, "reason": None, "calibrated": CALIBRATED, "errors": []}
    if not ctx.get("ok"):
        out["reason"] = "no_engine"
        out["error"] = ctx.get("error")
        return out
    ref = db.load_reference(con)
    an = ctx["analysis"]
    inp = engine_input(ref, ctx, th)
    if inp is None:
        out["reason"] = "no_engine"
        return out
    calc = engine.calculate(ref, inp, purpose=engine.PURPOSE_ANALYSIS)
    r = engine.rate_for(ref, inp)
    term = int(term_days or 365)
    sources = ctx.get("sources") or {}

    def safe(name, fn, default):
        try:
            return fn()
        except Exception as e:           # блок не посчитан — акт формируется, причина в errors
            out["errors"].append({"block": name, "error": type(e).__name__})
            return dict(default, available=False, reason="error")

    rows = safe("factors", lambda: {"items": factors(ref, inp, r, sources, S, term)}, {"items": []})["items"]
    # подгруппа транспорта (06.10.2026): не заданные факторы чужой подгруппы (площадка и моточасы спецтехники у
    # легкового, «допущенные водители» у крана) не показываются, не советуются и не уточняются; вклад их — 0
    if veh_group:
        rows = [x for x in rows if x["status"] != "not_set" or ae.vehicle_factor_applies(x["factor"], veh_group)]
    sens = safe("sensitivity", lambda: {"items": sensitivity(ref, inp, rows, rate_res, S, term, statutory)},
                {"items": []})["items"]
    mk = safe("market", lambda: market(con, cls, product_code, region, rate_res.get("applied_pct"),
                                       calc["rates"]["technical_pct"]), {})
    napp = safe("napp", lambda: napp_extra(con, region), {})
    excluded = housing_excluded(an, group)
    out.update(
        available=True,
        engine={"product_code": inp.product_code, "class_code": inp.class_code, "object_type": inp.object_type,
                "factors": dict(inp.factors), "term_days": inp.term_days, "takaful": bool(inp.takaful),
                "technical_pct": calc["rates"]["technical_pct"]},
        risks=safe("risks", lambda: risks({"risks": tpl_risks}, rows, sens, "template") if tpl_risks
                   else risks(an, rows, sens), {"items": []}),
        factors={"items": rows, "base_pct": _r(r["base_pct"] * max(r["share"], 0.001) * (r["gross_pct"] / r["net_pct"]
                                                                                     if r["net_pct"] else 0)),
                 "technical_pct": _r(r["gross_pct"]), "calibrated": CALIBRATED},
        sensitivity={"items": sens, "calibrated": CALIBRATED},
        tariff=safe("tariff", lambda: tariff(ref, inp, calc, r, rate_res, S, term, mk.get("rate_pct"),
                                             sources.get("object_type")), {}),
        scenarios=safe("scenarios", lambda: scenarios(con, ctx, scen, S, V, th), {"items": [], "whatif": []}),
        retention=safe("retention", lambda: retention(ctx, scen, S), {}),
        score=safe("score", lambda: score(an, act_level, th, excluded), {}),
        market=mk,
        stats=safe("stats", lambda: stats(an, excluded), {"indicators": []}),
        napp=napp,
        group=group,
        franchise=safe("franchise", lambda: franchise_table(con, ctx, rate_res, S, cls, statutory, th), {"rows": []}),
        measures=safe("measures", lambda: measures_effect(meas, calc["rates"]["technical_pct"]), {"items": []}),
        assumptions=list(ctx.get("assumptions") or []),
    )
    return out


# ================================================================================================
#  Справка биржи УзРТСБ (uzex.uz) для запасов, грузов и материалов — только чтение exchange_quotes
# ================================================================================================
EXCHANGE_CLASSES = ("7", "8", "9", "16")
EXCHANGE_DAYS = 30
EXCHANGE_MAX_ITEMS = 4
EXCHANGE_NOTE = "биржевые цены реальных сделок, для сверки стоимости запасов/грузов; стоимость объекта не меняет"
# вид груза класса 7 (шаблон, поле cargo_group) → группы товаров биржи; хрупкий, скоропортящийся и дорогой
# груз (техника, продукты, табак) на бирже не торгуется — справки нет
EXCHANGE_CARGO = {"bulk": ["diesel", "petrol", "cement", "grain"], "dangerous": ["chemicals", "fuel_other"],
                  "general": ["metal"], "oversized": ["metal"],
                  "fuel": ["diesel", "petrol", "heating_oil"], "топливо": ["diesel", "petrol", "heating_oil"]}
# подгруппа объекта классов 8/9 (поле object_group) → группы товаров
EXCHANGE_OBJECT_GROUP = {"agro": ["grain", "cotton"], "energy": ["diesel", "fuel_other"]}
EXCHANGE_STOCK_KINDS = ("warehouse", "production")
EXCHANGE_STOCK_GROUPS = ("warehouse", "production", "agro", "energy", "liquid_goods")
EXCHANGE_DEFAULT = ["diesel", "metal", "cement"]      # вид запасов не указан — основные биржевые материалы
_STOCK_TEXT = re.compile(r"склад|производ|цех|завод|фабрик|запас|сырь|ombor|ishlab chiqarish", re.I)


def _exchange_text(fields: Optional[dict], *extra) -> str:
    vals = [str(v) for v in (fields or {}).values() if isinstance(v, str)]
    return " ".join(vals + [str(x) for x in extra if x])


def _exchange_wanted(cls: str, kind: Optional[str], fields: Optional[dict], text: str) -> tuple:
    """(группы, на чём основан выбор) для одного класса; ([], None) — справка классу не нужна."""
    from . import uzex_sources as us
    cls = str(cls or "").strip()
    if cls not in EXCHANGE_CLASSES:
        return [], None
    fields = fields or {}
    named = us.groups_in_text(text)
    if cls == "7":
        if named:
            return named, "text"
        cg = str(fields.get("cargo_group") or "").strip().lower()
        return (list(EXCHANGE_CARGO[cg]), "cargo_group") if cg in EXCHANGE_CARGO else ([], None)
    if cls in ("8", "9"):
        og = str(fields.get("object_group") or "").strip().lower()
        stock = kind in EXCHANGE_STOCK_KINDS or og in EXCHANGE_STOCK_GROUPS or bool(_STOCK_TEXT.search(text))
        if not stock:
            return [], None
        if named:
            return named, "text"
        if og in EXCHANGE_OBJECT_GROUP:
            return list(EXCHANGE_OBJECT_GROUP[og]), "object_group"
        return list(EXCHANGE_DEFAULT), "default"
    # 16 — перерыв в производстве: сырьё и топливо производства
    if named:
        return named, "text"
    return list(EXCHANGE_DEFAULT), "default"


def exchange_background(con, cls, object_kind: Optional[str] = None, parts: Optional[list] = None,
                        class_fields: Optional[dict] = None, text: str = "", days: int = EXCHANGE_DAYS,
                        as_of: Optional[str] = None) -> dict:
    """
    Справка биржи УзРТСБ к стоимости запасов, грузов и материалов (классы 7, 8, 9, 16). Только чтение базы
    (exchange_quotes, загрузка — app/uzex.py); в сеть не ходит, ставку и стоимость объекта не меняет.
    parts — части комплексного продукта ({class_code, object_kind, object_description, fields}): справка по каждой.
    Нет подходящего класса или нет сделок за days дней — available = false, без пояснений в акте.
    """
    from . import uzex_sources as us
    targets = [(str(cls or ""), object_kind, class_fields or {}, _exchange_text(class_fields, text))]
    for p in parts or []:
        targets.append((str(p.get("class_code") or ""), p.get("object_kind"), p.get("fields") or {},
                        _exchange_text(p.get("fields"), p.get("object_description"), text)))
    wanted, basis = [], None
    for c, k, f, tx_ in targets:
        gs, b = _exchange_wanted(c, k, f, tx_)
        for g in gs:
            if g not in wanted:
                wanted.append(g)
        if gs and (basis is None or basis == "default"):
            basis = b
    out = {"available": False, "items": [], "basis": basis, "days": int(days), "note": EXCHANGE_NOTE,
           "source": us.SOURCE, "source_name": us.SOURCE_NAME, "url": us.BASE + us.PAGES["List"]["path"],
           "calibrated": CALIBRATED}
    if not wanted:
        out["reason"] = "not_relevant"
        return out
    rows = us.summary(con, days=days, as_of=as_of, groups=wanted)
    best = {}
    for r in rows:                                   # у группы — единица с наибольшим числом сделок
        if r["group"] not in best or r["deals"] > best[r["group"]]["deals"]:
            best[r["group"]] = r
    items = [{"group": g, "group_label": best[g]["group_label"], "median_unit_price": best[g]["median_unit_price"],
              "unit": best[g]["unit"], "deals": best[g]["deals"], "last_date": best[g]["last_date"],
              "first_date": best[g]["first_date"], "min": best[g]["min"], "max": best[g]["max"],
              "currency": "UZS", "url": best[g]["url"]}
             for g in wanted if g in best][:EXCHANGE_MAX_ITEMS]
    if not items:
        out["reason"] = "no_data"
        return out
    out.update(available=True, items=items, groups_requested=wanted,
               period={"days": int(days), "to": as_of or date.today().isoformat()})
    return out
