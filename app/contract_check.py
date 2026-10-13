"""
Сверка договора страхования с актом — 12 проверок (ТЗ разработчику 13.10.2026, день 7.2).

  1 существенные условия (ГК РУз, ст. 929) — нет хотя бы одного → «На рассмотрение»;
  2 бланк договора (поля не заполнены) → сверки нет, остальные проверки не выполняются;
  3 продукт договора = продукт акта;
  4 страховая сумма договора = сумма акта (± sum_tolerance сумов);
  5 S / V в пределах [sv_min; sv_max] (ГК ст. 936 — недострахование, ст. 938 — превышение стоимости);
  6 ставка договора ≥ минимальной по каждой части продукта (product_parts.parts_on с типом клиента) →
    «Отказать / согласовать с ЦО»; одна ставка на продукт из нескольких частей — «На рассмотрение»;
  7 ставка договора ≥ r × (1 − act_rate_tolerance_pct / 100), r — ставка акта (rate_model) → «На рассмотрение»;
  8 премия = S × ставка × срок (± premium_tolerance_pct %), сумма платежей = премия;
  9 даты срока есть и идут по порядку; осмотр (дата акта) не позже начала срока и не раньше inspection_max_days
    дней до него;
 10 риски договора ⊆ риски (perils) частей продукта — по словарю risk_map;
 11 франшиза договора ≥ рекомендованной актом (справочно);
 12 выгодоприобретатель — банк → продукт «в залоге» (справочно).

Результат — act_json.data.contract_check.checks: [{no, code, verdict, reference, values, text_code, text}]
(text — по-русски для архива; экран получает текст на своём языке через view()). Вердикты: ok, violated,
review («На рассмотрение»), decline_or_agree («Отказать / согласовать с ЦО»), no_check (бланк), skipped
(не проверяется — бланк), na (нет данных). Пороги — act_settings.contract_check с версией и датой (DEFAULTS),
экспертно, calibrated = 0. Модуль без сети; база — только чтение частей продукта (attach).
"""
from datetime import date
from typing import Optional

DEFAULTS = {
    "version": "1.0",
    "date": "2026-10-13",
    "source": "ТЗ разработчику 13.10.2026, день 7.2",
    "calibrated": 0,
    "sum_tolerance": 1000,              # сумов: страховая сумма договора против суммы акта
    "sv_min": 0.9,                      # S / V ниже — недострахование (ГК ст. 936)
    "sv_max": 1.1,                      # S / V выше — сумма больше стоимости (ГК ст. 938)
    "act_rate_tolerance_pct": 10,       # ставка договора может быть ниже ставки акта не больше чем на столько %
    "premium_tolerance_pct": 1,         # премия против S × ставка × срок и платежи против премии, %
    "inspection_max_days": 30,          # осмотр — не раньше стольких дней до начала срока
    "bank_words": ["банк", "bank", "акб", "чакб", "aтб", "atb"],
    "pledge_words": ["залог", "ипотек", "garov", "гаров", "pledge", "mortgage", "collateral"],
    # риск договора (contract_read.RISK_WORDS) → риски частей продукта (product_parts.perils), любой из списка
    "risk_map": {
        "fire": ["fire", "mv_fire"], "lightning": ["lightning"], "explosion": ["explosion"],
        "earthquake": ["earthquake", "mv_nat"], "storm": ["storm", "hurricane", "mv_nat"],
        "flood": ["downpour", "groundwater", "mudflow", "mv_nat"],
        "landslide": ["landslide", "mudflow", "collapse", "subsidence", "mv_nat"], "hail": ["hail", "mv_nat"],
        "natural": ["earthquake", "storm", "hurricane", "downpour", "mudflow", "landslide", "hail", "snowfall",
                    "frost", "mv_nat"],
        "theft": ["burglary", "mv_theft"], "third_party": ["other9", "mv_malicious"],
        "vehicle_impact": ["other9"], "aircraft": ["other9"], "water": ["other9", "groundwater", "downpour"],
        "glass": ["other9", "mv_glass"], "electrical": ["other9"], "breakdown": ["other9"],
        "collision": ["mv_accident"], "death": ["pa_death"], "disability": ["pa_disability"],
        "temp_disability": ["pa_temp_disability"], "injury": ["pa_injury"],
    },
}

CODES = ("essentials", "template", "product", "sum_insured", "sum_value", "min_rate", "act_rate", "premium", "term",
         "risks", "franchise", "beneficiary")
REFERENCE = ("franchise", "beneficiary")              # справочно: в итог и решение не идут
VERDICTS = ("ok", "violated", "review", "decline_or_agree", "no_check", "skipped", "na")
ORDER = {"decline_or_agree": 5, "review": 4, "violated": 3, "na": 1, "ok": 0, "skipped": 0}
ESSENTIALS = ("object", "insured_event", "sum_insured", "premium", "term")
LEGAL = {"essentials": "ГК РУз, ст. 929", "sum_value": "ГК РУз, ст. 936, 938"}
NUM_KEYS = {"sum_tolerance": (0, 1e9), "sv_min": (0.1, 1.0), "sv_max": (1.0, 5.0),
            "act_rate_tolerance_pct": (0, 100), "premium_tolerance_pct": (0, 50), "inspection_max_days": (0, 366)}


def settings(st: Optional[dict]) -> dict:
    """Пороги из настроек акта поверх значений по умолчанию (неглубоко: словарь risk_map заменяется целиком)."""
    got = (st or {}).get("contract_check") or {}
    return {**DEFAULTS, **{k: v for k, v in got.items() if k in DEFAULTS}}


def check_settings(m: dict) -> list:
    """Ошибки настроек contract_check (пустой список — всё верно)."""
    cs = (m or {}).get("contract_check")
    if cs is None:
        return []
    if not isinstance(cs, dict):
        return ["contract_check: словарь порогов"]
    errs = []
    extra = [k for k in cs if k not in DEFAULTS]
    if extra:
        errs.append("contract_check: неизвестные ключи " + ", ".join(extra))
    for k, (lo, hi) in NUM_KEYS.items():
        v = cs.get(k, DEFAULTS[k])
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            errs.append(f"contract_check.{k}: число от {lo:g} до {hi:g}")
    if float(cs.get("sv_min", DEFAULTS["sv_min"])) > float(cs.get("sv_max", DEFAULTS["sv_max"])):
        errs.append("contract_check: sv_min не больше sv_max")
    for k in ("bank_words", "pledge_words"):
        v = cs.get(k, DEFAULTS[k])
        if not isinstance(v, list) or not v or not all(isinstance(x, str) and x.strip() for x in v):
            errs.append(f"contract_check.{k}: непустой список слов")
    rm = cs.get("risk_map", DEFAULTS["risk_map"])
    if not isinstance(rm, dict) or not all(isinstance(k, str) and isinstance(v, list) and
                                          all(isinstance(x, str) for x in v) for k, v in rm.items()):
        errs.append("contract_check.risk_map: {риск договора: [риски продукта]}")
    for k in ("version", "date"):
        if not isinstance(cs.get(k, DEFAULTS[k]), str) or not cs.get(k, DEFAULTS[k]):
            errs.append(f"contract_check.{k}: строка")
    return errs


# --------------------------------------------------------------------------- #
#  Помощники
# --------------------------------------------------------------------------- #

def _num(v) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def _date(v) -> Optional[date]:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _has(words: list, text: Optional[str]) -> bool:
    s = str(text or "").lower()
    return bool(s) and any(str(w).lower() in s for w in words)


def _risk_codes(xs) -> list:
    """Риски optional.contract: коды словаря (строки) или {code, text} разбора."""
    out = []
    for x in xs or []:
        if isinstance(x, dict):
            x = x.get("code") if x.get("code") and x.get("code") != "other" else x.get("text")
        if x and x not in out:
            out.append(str(x))
    return out


def _item(no: int, code: str, verdict: str, values: dict, text_code: Optional[str] = None) -> dict:
    return {"no": no, "code": code, "verdict": verdict, "reference": code in REFERENCE,
            "legal_ref": LEGAL.get(code), "values": values,
            "text_code": "cc_skipped" if verdict == "skipped" else (text_code or f"cc_{code}_{verdict}")}


def essentials_of(ct: dict) -> list:
    """Существенные условия договора (ГК РУз, ст. 929): объект, страховой случай, сумма, премия, срок."""
    have = {"object": bool(ct.get("object_description") or ct.get("items") or ct.get("cadastre_no")),
            "insured_event": bool(ct.get("covered_risks") or ct.get("insured_event")),
            "sum_insured": ct.get("sum_insured") is not None, "premium": ct.get("premium") is not None,
            "term": bool(ct.get("term_days"))}
    return [{"code": c, "present": have[c]} for c in ESSENTIALS]


def premium_calc(rate_pct: float, s: float, days: int, rate_type: str = "annual") -> float:
    """S × ставка × срок: annual — × дни / 365; fixed — ставка на весь срок (как act_engine.premium_by_type)."""
    if rate_type == "fixed":
        return s * rate_pct / 100
    return s * rate_pct / 100 * days / 365


# --------------------------------------------------------------------------- #
#  12 проверок
# --------------------------------------------------------------------------- #

def run(ct: Optional[dict], act: dict, parts: Optional[list] = None, st: Optional[dict] = None,
        template: bool = False) -> dict:
    """
    ct — условия договора (проверенный optional.contract или разбор загрузки), act — значения акта:
      product_code, product_name, class_code, client_type, sum_insured, object_value, value_source,
      rate_pct (ставка акта r), min_pct (минимум без частей), rate_type, term_days, inspection_date,
      franchise_pct (рекомендованная), franchise_source;
    parts — части продукта на дату (product_parts.parts_on(...)["parts"]); template — загружен бланк.
    Возвращает {available, version, date, checks, summary}. Нет договора и не бланк — available = false.
    """
    s = settings(st)
    head = {"version": s["version"], "date": s["date"], "source": s["source"], "calibrated": s["calibrated"]}
    if not ct and not template:
        return {"available": False, "checks": [], "summary": None, **head}
    if template or (ct or {}).get("is_template"):
        checks = [_item(1, "essentials", "skipped", {}), _item(2, "template", "no_check", {})]
        checks += [_item(i, c, "skipped", {}) for i, c in enumerate(CODES[2:], start=3)]
        return {"available": True, "template": True, "checks": checks,
                "summary": {"verdict": "no_check", "codes": []}, **head}
    ct = ct or {}
    checks = [_c1(ct), _item(2, "template", "ok", {}), _c3(ct, act), _c4(ct, act, s), _c5(ct, act, s),
              _c6(ct, act, parts, s), _c7(ct, act, s), _c8(ct, act, s), _c9(ct, act, s), _c10(ct, parts, s),
              _c11(ct, act), _c12(ct, act, s)]
    main = [c for c in checks if not c["reference"]]
    bad = [c for c in main if c["verdict"] in ("violated", "review", "decline_or_agree")]
    if bad:
        verdict = max((c["verdict"] for c in bad), key=lambda v: ORDER[v])
    else:
        # нарушений нет: «сходится», если хоть что-то сверено; иначе сверить не с чем
        verdict = "ok" if any(c["verdict"] == "ok" and c["code"] != "template" for c in main) else "na"
    return {"available": True, "template": False, "checks": checks,
            "summary": {"verdict": verdict, "codes": [c["code"] for c in bad],
                        "na": [c["code"] for c in main if c["verdict"] == "na"],
                        "reference": [c["code"] for c in checks if c["reference"] and c["verdict"] == "violated"]},
            **head}


def _c1(ct: dict) -> dict:
    ess = essentials_of(ct)
    miss = [e["code"] for e in ess if not e["present"]]
    return _item(1, "essentials", "review" if miss else "ok",
                 {"missing": miss, "present": [e["code"] for e in ess if e["present"]]})


def _c3(ct: dict, act: dict) -> dict:
    a, b = ct.get("product_code"), act.get("product_code")
    v = {"contract": a, "act": b}
    if not a or not b:
        return _item(3, "product", "na", v)
    return _item(3, "product", "ok" if str(a).zfill(4) == str(b).zfill(4) else "violated", v)


def _c4(ct: dict, act: dict, s: dict) -> dict:
    a, b = _num(ct.get("sum_insured")), _num(act.get("sum_insured"))
    tol = float(s["sum_tolerance"])
    v = {"contract": a, "act": b, "tol": tol}
    if a is None or b is None:
        return _item(4, "sum_insured", "na", v)
    v["diff"] = round(a - b, 2)
    return _item(4, "sum_insured", "ok" if abs(a - b) <= tol + 1e-9 else "violated", v)


def _c5(ct: dict, act: dict, s: dict) -> dict:
    S = _num(ct.get("sum_insured"))
    if S is None:
        S = _num(act.get("sum_insured"))
    V = _num(act.get("object_value"))
    src = act.get("value_source") or "act"
    if V is None or V <= 0:
        V, src = _num(ct.get("object_value")), "contract"
    v = {"sum": S, "value": V, "value_source": src, "lo": float(s["sv_min"]), "hi": float(s["sv_max"]),
         "contract_value": _num(ct.get("object_value"))}
    if S is None or not V:
        return _item(5, "sum_value", "na", v)
    ratio = round(S / V, 4)
    v["ratio"] = ratio
    if ratio < v["lo"] - 1e-9:
        v["side"] = "under"
    elif ratio > v["hi"] + 1e-9:
        v["side"] = "over"
    return _item(5, "sum_value", "violated" if v.get("side") else "ok", v,
                 f"cc_sum_value_{v['side']}" if v.get("side") else None)


def _c6(ct: dict, act: dict, parts: Optional[list], s: dict) -> dict:
    rate = _num(ct.get("tariff_pct"))
    rows = [{"part_no": p.get("part_no"), "class_code": p.get("class_code"), "min": _num(p.get("min_rate_pct"))}
            for p in parts or []]
    v = {"rate": rate, "parts": rows}
    if not rows and _num(act.get("min_pct")) is not None:
        rows = [{"part_no": 1, "class_code": act.get("class_code"), "min": _num(act.get("min_pct"))}]
        v["parts"], v["min_source"] = rows, "min_rates"
    if rate is None or not [r for r in rows if r["min"] is not None]:
        return _item(6, "min_rate", "na", v)
    # одна ставка договора: сравнивается с частью класса акта (иначе — с первой частью); ставка проверяется по
    # каждому классу отдельно (правило проекта № 5) — ставок других частей в договоре нет, их проверяет андеррайтер
    own = [r for r in rows if r["class_code"] == act.get("class_code")] or rows[:1]
    main = own[0]
    v.update(min=main["min"], part_no=main["part_no"], cls=main["class_code"])
    if main["min"] is not None and rate + 1e-9 < main["min"]:
        return _item(6, "min_rate", "decline_or_agree", v)
    others = [r for r in rows if r is not main and r["min"] is not None]
    if others:
        v["unchecked"] = [r["class_code"] for r in others]
        return _item(6, "min_rate", "review", v, "cc_min_rate_parts")
    return _item(6, "min_rate", "ok", v)


def _c7(ct: dict, act: dict, s: dict) -> dict:
    rate, r = _num(ct.get("tariff_pct")), _num(act.get("rate_pct"))
    tol = float(s["act_rate_tolerance_pct"])
    v = {"rate": rate, "r": r, "tol": tol}
    if rate is None or r is None:
        return _item(7, "act_rate", "na", v)
    floor = round(r * (1 - tol / 100), 6)
    v["floor"] = floor
    return _item(7, "act_rate", "ok" if rate + 1e-9 >= floor else "review", v)


def _c8(ct: dict, act: dict, s: dict) -> dict:
    prem, rate = _num(ct.get("premium")), _num(ct.get("tariff_pct"))
    S = _num(ct.get("sum_insured"))
    if S is None:
        S = _num(act.get("sum_insured"))
    days = ct.get("term_days") or act.get("term_days")
    rt = act.get("rate_type") or "annual"
    tol = float(s["premium_tolerance_pct"])
    pays = [_num(p.get("amount")) for p in ct.get("payments") or [] if isinstance(p, dict)]
    v = {"premium": prem, "rate": rate, "sum": S, "days": days, "rate_type": rt, "tol_pct": tol,
         "payments_total": round(sum(x for x in pays if x), 2) if pays else None, "payments_n": len(pays)}
    if prem is None or rate is None or S is None or (not days and rt != "fixed"):
        return _item(8, "premium", "na", v)
    calc = round(premium_calc(rate, S, int(days or 365), rt), 2)
    v["calc"] = calc
    issues = []
    if calc and abs(prem - calc) > calc * tol / 100 + 1e-6:
        issues.append("premium")
    if pays and abs(v["payments_total"] - prem) > prem * tol / 100 + 1e-6:
        issues.append("payments")
    v["issues"] = issues
    return _item(8, "premium", "violated" if issues else "ok", v)


def _c9(ct: dict, act: dict, s: dict) -> dict:
    a, b = _date(ct.get("term_from")), _date(ct.get("term_to"))
    insp = _date(act.get("inspection_date"))
    mx = int(s["inspection_max_days"])
    v = {"from": a.isoformat() if a else None, "to": b.isoformat() if b else None, "days": ct.get("term_days"),
         "inspection": insp.isoformat() if insp else None, "max_days": mx}
    if not a and not b and not ct.get("term_days"):
        return _item(9, "term", "na", v)
    issues = []
    if not a or not b:
        issues.append("no_dates")
    elif b < a:
        issues.append("order")
    if a and insp:
        before = (a - insp).days
        v["before_days"] = before
        if before < 0:
            issues.append("after_start")
        elif before > mx:
            issues.append("too_early")
    v["issues"] = issues
    return _item(9, "term", "violated" if issues else "ok", v)


def _c10(ct: dict, parts: Optional[list], s: dict) -> dict:
    risks = _risk_codes(ct.get("covered_risks"))
    perils = []
    for p in parts or []:
        perils += [x for x in p.get("perils") or [] if x not in perils]
    v = {"risks": risks, "perils": perils}
    if not risks or not perils:
        return _item(10, "risks", "na", v)
    rm = s["risk_map"]
    outside = [r for r in risks if r in rm and not set(rm[r]) & set(perils)]
    unknown = [r for r in risks if r not in rm]
    v.update(outside=outside, unknown=unknown)
    if outside:
        return _item(10, "risks", "violated", v)
    # незнакомые риски («все риски», свои слова договора) сверить со списком продукта нельзя — андеррайтеру
    return _item(10, "risks", "review" if unknown else "ok", v, "cc_risks_unknown" if unknown else None)


def _c11(ct: dict, act: dict) -> dict:
    fr = ct.get("franchise") or {}
    rec = _num(act.get("franchise_pct"))
    pct = _num(fr.get("pct")) if fr.get("applied") else (0.0 if fr else None)
    amt = _num(fr.get("amount")) if fr.get("applied") else None
    S = _num(ct.get("sum_insured")) or _num(act.get("sum_insured"))
    if pct is None and amt is not None and S:
        pct = round(amt / S * 100, 4)
    v = {"contract_pct": pct, "contract_amount": amt, "rec_pct": rec, "rec_source": act.get("franchise_source")}
    if rec is None or rec <= 0:
        return _item(11, "franchise", "na", v)
    if pct is None:
        return _item(11, "franchise", "na", v, "cc_franchise_na_contract")
    return _item(11, "franchise", "ok" if pct + 1e-9 >= rec else "violated", v)


def _c12(ct: dict, act: dict, s: dict) -> dict:
    b = ct.get("beneficiary") or {}
    name = b.get("name") if isinstance(b, dict) else b
    bank = ct.get("beneficiary_bank")
    if bank is None:
        bank = _has(s["bank_words"], name) if name else None
    product = act.get("product_name")
    v = {"beneficiary_bank": bank, "product": product, "product_code": act.get("product_code")}
    if not bank:
        return _item(12, "beneficiary", "ok", v, "cc_beneficiary_none")
    pledge = _has(s["pledge_words"], product)
    v["pledge_product"] = pledge
    return _item(12, "beneficiary", "ok" if pledge else "violated", v)


# --------------------------------------------------------------------------- #
#  Акт: сборка (build_data) и текст
# --------------------------------------------------------------------------- #

def act_values(D: dict, today: date) -> dict:
    """Значения акта для сверки: продукт, суммы, ставка r (rate_model), минимум, тип ставки, франшиза, дата осмотра."""
    must, rate = D.get("must") or {}, D.get("rate") or {}
    pf = D.get("premium_final") or {}
    r = rate.get("applied_pct") if rate.get("mode") not in ("multi", "undefined") else pf.get("rate_pct")
    fr = D.get("franchise") or {}
    rec = fr.get("size_pct") if fr.get("status") in ("applied", "proposed") or fr.get("needed") else None
    val = D.get("valuation") or {}
    by_val = isinstance(val, dict) and bool(_num(val.get("V")))
    V = val["V"] if by_val else must.get("object_value")
    return {"product_code": must.get("product_code"), "product_name": must.get("product_name"),
            "class_code": must.get("class_code"), "client_type": must.get("client_type"),
            "sum_insured": must.get("sum_insured"), "object_value": V,
            "value_source": "valuation" if by_val else "act",
            "rate_pct": r, "min_pct": rate.get("min_pct"), "rate_type": rate.get("rate_type") or "annual",
            "term_days": rate.get("term_days"), "inspection_date": today.isoformat(),
            "franchise_pct": rec, "franchise_source": "act" if rec is not None else None}


def attach(con, D: dict, ct: Optional[dict], upload: Optional[dict], st: dict, today: date) -> dict:
    """
    12 проверок в данные акта: D["contract_check"]["checks"], ["checks_summary"], ["checks_settings"]
    (прежние ключи сверки — available, items, summary, how … — не меняются). Итог «На рассмотрение» или
    «Отказать / согласовать с ЦО» — пункт c_cc в проверках андеррайтера и решение «На рассмотрение».
    """
    upf = ((upload or {}).get("contract") or {}).get("fields") or {}
    template = bool(upf.get("is_template")) and not ct
    act = act_values(D, today)
    ct_full = dict(ct or {})
    if ct and not ct_full.get("beneficiary") and upf.get("beneficiary"):
        ct_full["beneficiary"] = upf["beneficiary"]          # своя загрузка: выгодоприобретатель (только юрлицо)
    parts = []
    if act["product_code"] and (ct or template):
        try:
            from . import product_parts as pp
            as_of = (ct or {}).get("term_from") or today.isoformat()
            parts = pp.parts_on(con, act["product_code"], as_of, act["client_type"]
                                if act["client_type"] in ("fiz", "yur") else None)["parts"]
        except Exception as e:           # нет таблицы частей — минимум акта (min_rates), причина в журнал сервера
            print("сверка договора: части продукта не прочитаны:", type(e).__name__)
            parts = []
    res = run(ct_full if ct else None, act, parts, st, template)
    for c in res["checks"]:
        c["text"] = text(c, "ru")
    cc = D.get("contract_check")
    if not isinstance(cc, dict):
        cc = {"available": False}
    cc["checks"] = res["checks"]
    cc["checks_summary"] = res["summary"]
    cc["checks_settings"] = {k: res[k] for k in ("version", "date", "source", "calibrated")}
    cc["checks_available"] = res["available"]
    D["contract_check"] = cc
    sm = res["summary"] or {}
    if res["available"] and not res.get("template") and sm.get("codes"):
        dec = D.get("decision") or {}
        dec.setdefault("checks", []).append({"code": "c_cc", "params": {"verdict": sm["verdict"],
                                                                        "codes": list(sm["codes"])}})
        if sm["verdict"] in ("review", "decline_or_agree") and dec.get("code") in ("d_accept",
                                                                                   "d_accept_with_clauses"):
            dec["code"] = "d_review"
        elif dec.get("code") == "d_accept":
            dec["code"] = "d_accept_with_clauses"
    return cc


# подписи и тексты — app/act_texts/contract_check.py (ru, uz, en)
_MONEY = ("contract", "act", "diff", "tol", "sum", "value", "contract_value", "premium", "calc", "payments_total",
          "contract_amount")
_PCT = ("rate", "r", "floor", "min", "contract_pct", "rec_pct")


def _fmt(code: str, values: dict, lang: str) -> dict:
    from . import act_texts as tx
    from .act_texts import money, pct, t
    out = {}
    for k, v in (values or {}).items():
        if k in ("contract", "act") and code == "product":
            out[k] = v or t("na", lang)
        elif k in _MONEY:
            out[k] = money(v, lang) if v is not None else t("na", lang)
        elif k in _PCT:
            out[k] = pct(v, lang, 6) if v is not None else t("na", lang)
        elif k in ("ratio", "lo", "hi"):
            out[k] = tx._num(float(v), tx.lang_of(lang), 2) if v is not None else t("na", lang)
        elif k in ("from", "to", "inspection"):
            out[k] = ".".join(reversed(str(v).split("-"))) if v else t("na", lang)
        elif k in ("tol_pct",):
            out[k] = tx._num(float(v), tx.lang_of(lang), 0 if float(v) == int(v) else 1)
        else:
            out[k] = v
    if code == "sum_insured":
        out["tol"] = money(values.get("tol"), lang)
    if code == "act_rate":
        out["tol"] = tx._num(float(values.get("tol") or 0), tx.lang_of(lang), 0)
    if code == "essentials":
        for k in ("missing", "present"):
            out[k] = ", ".join(tx.label(tx.CT_ESSENTIAL_LABELS, x, lang) for x in values.get(k) or [])
    if code == "risks":
        for k in ("risks", "outside", "unknown"):
            out[k] = ", ".join(tx.label(tx.RISK_LABELS, x, lang) if x in tx.RISK_LABELS else str(x)
                               for x in values.get(k) or [])
    if code == "min_rate":
        out["unchecked"] = ", ".join(str(x) for x in values.get("unchecked") or [])
        out["cls"] = values.get("cls") or t("na", lang)
    if code == "beneficiary":
        out["product"] = values.get("product") or values.get("product_code") or t("na", lang)
    return out


def text(item: dict, lang: str = "ru") -> str:
    """Текст проверки на языке акта (тексты — act_texts, ключи cc_*)."""
    from .act_texts import t
    p = _fmt(item["code"], item.get("values") or {}, lang)
    issues = (item.get("values") or {}).get("issues")
    if item["code"] in ("premium", "term") and item["verdict"] == "violated" and issues:
        return "; ".join(t(f"cc_{item['code']}_{i}", lang, **p) for i in issues)
    return t(item["text_code"], lang, **p)


def view(cc: Optional[dict], lang: str) -> dict:
    """Блок 12 проверок для экрана: [{no, code, label, verdict, verdict_label, reference, values, text}] и итог."""
    from .act_texts import t, label
    from . import act_texts as tx
    cc = cc or {}
    checks = cc.get("checks") or []
    items = [{"no": c["no"], "code": c["code"], "label": label(tx.CC_LABELS, c["code"], lang),
              "verdict": c["verdict"], "verdict_label": label(tx.CC_VERDICT_LABELS, c["verdict"], lang),
              "reference": bool(c.get("reference")), "legal_ref": c.get("legal_ref"), "values": c.get("values") or {},
              "text": text(c, lang)} for c in checks]
    sm = cc.get("checks_summary") or {}
    summary = None
    if checks:
        v = sm.get("verdict") or "ok"
        summary = {"verdict": v, "verdict_label": label(tx.CC_VERDICT_LABELS, v, lang),
                   "text": t("cc_summary_" + v, lang), "codes": list(sm.get("codes") or [])}
    lines = [f"{i['no']}. {i['label']} — {i['verdict_label']}: {i['text']}" for i in items]
    return {"available": bool(checks), "items": items, "summary": summary, "lines": lines,
            "settings": cc.get("checks_settings")}


def check_text(c: dict, lang: str) -> str:
    """Пункт c_cc проверок андеррайтера: итог сверки и какие проверки не прошли."""
    from .act_texts import t, label
    from . import act_texts as tx
    p = c.get("params") or {}
    what = ", ".join(label(tx.CC_LABELS, x, lang).lower() for x in p.get("codes") or [])
    return t("c_cc", lang, verdict=label(tx.CC_VERDICT_LABELS, p.get("verdict") or "violated", lang), what=what)
