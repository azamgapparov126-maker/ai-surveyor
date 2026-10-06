"""Комплексный продукт по частям и парк объектов: расчёт каждой части или объекта и итоги договора."""
import re

from .. import act_analytics as aa
from .. import act_engine as ae
from .. import act_extras as ax
from .. import class_templates as ctpl
from .. import act_texts as tx
from .. import db

from .common import clause_catalog
from .recognize import preferred
from .regions import region_for_stats, region_scope
from .terms import credit_rule, _policyholder
from .calc import (veh_measure_codes,
    _analytics_block, _annual_view, _doc_checks, _fork_finish, _fork_prepare, _kind_from_text, _min_how,
    _napp_settings, _no_products_rate, _region_scope_stats, _template_block)


# --------------------------------------------------------------------------- #
#  Комплексный продукт по частям (30.09.2026, ТЗ универсального шаблона 4.1в)
# --------------------------------------------------------------------------- #

def _template_words(con, classes: list) -> dict:
    """Слова видов объекта шаблонов классов (ru/uz/en, основы от 5 букв) — для сопоставления объектов договора."""
    out = {}
    for c in classes:
        tpl = (ctpl.current(con, c) or {}).get("template") or {}
        stems = set()
        for k in (tpl.get("object") or {}).get("kinds") or []:
            for lab in (k.get("label") or {}).values():
                for w in re.split(r"[^\wʻ'-]+", str(lab).lower()):
                    if len(w) >= 5:
                        stems.add(w[:5])
        out[str(c)] = sorted(stems)
    return out


def _parts_plan(con, clean: dict, C: dict) -> dict:
    """
    Какие части и с какими суммами: явные части сотрудника (source = employee) → перечень объектов договора
    (contract; сумма объектов должна сойтись со страховой суммой) → доли тарифной политики (policy_shares,
    настройка parts.shares) → поровну (default). Всё, кроме частей сотрудника, — предложение: confirmed = false.
    """
    m, st = C["m"], C["st"]
    classes = list(m.get("product_classes") or [])
    S = float(m["sum_insured"])
    tol = float(st["parts"]["sum_tolerance"])
    notes = []
    if clean.get("parts"):
        conf = clean.get("parts_confirmed")
        return {"source": "employee", "confirmed": True if conf is None else bool(conf),
                "items": [dict(p) for p in clean["parts"]], "notes": notes}
    ct = C["docs"].get("ct") or {}
    items = [x for x in ct.get("items") or [] if x.get("sum")]
    if items:
        rows = ae.parts_from_items(items, classes, _template_words(con, classes))
        if ae.check_parts_sum(rows, S, tol) is None:
            for r in rows:
                if r["class_guess"]:
                    notes.append({"code": "pt_n_class_guess", "params": {"cls": r["class_code"],
                                                                         "names": ", ".join(r["names"])}})
            return {"source": "contract", "confirmed": False, "notes": notes,
                    "items": [{"class_code": r["class_code"], "sum_insured": r["sum_insured"],
                               "share_pct": round(r["sum_insured"] / S * 100, 4),
                               "object_description": "; ".join(r["names"])[:200] or None,
                               "class_guess": r["class_guess"]} for r in rows]}
        notes.append({"code": "pt_n_items_sum", "params": {"sum": round(sum(float(x["sum"]) for x in items), 2),
                                                           "total": S}})
    shares = ((st["parts"].get("shares") or {}).get(m.get("product_code") or "")) or None
    if shares and all(c in shares for c in classes):
        return {"source": "policy_shares", "confirmed": False, "notes": notes,
                "items": ae.split_sum(S, classes, shares)}
    notes.append({"code": "pt_n_default", "params": {"n": len(classes)}})
    return {"source": "default", "confirmed": False, "notes": notes, "items": ae.split_sum(S, classes)}


def _part_missing(tpl: dict, op: dict, kind, main: bool, recognized: list) -> list:
    """Обязательные поля шаблона класса части, которых нет (коды и подписи ru/uz/en)."""
    have_rec = {r["key"] for r in recognized or [] if r.get("value")} if main else set()
    cf = op.get("class_fields") or {}
    out = []
    for f in tpl.get("must") or []:
        code, inp = f.get("code"), str(f.get("input") or "")
        if inp.startswith("optional.class_fields."):
            ok_ = code in cf
        elif code == "object_kind":
            ok_ = bool(kind)
        elif code in ("brand", "model", "year"):
            ok_ = code in have_rec or (code == "year" and op.get("year") is not None)
        else:
            ok_ = op.get(code) not in (None, "")
        if not ok_:
            out.append({"code": code, "label": dict(f.get("label") or {})})
    return out


def _part_calc(con, P: dict, idx: int, C: dict) -> dict:
    """
    Одна часть договора по шаблону своего класса: уровень риска по своим признакам, ставка по своей тарифной
    политике (обязательный вид — только нормативный акт, без поправок и франшизы), премия на весь срок, сумма к
    стоимости, франшиза, сценарии, мероприятия, аналитика. Те же функции, что у однопродуктового акта.
    """
    ref, st, th, m, o = C["ref"], C["st"], C["th"], C["m"], C["o"]
    errs = C["block_errors"]
    cls = P["class_code"]
    main = bool(P["same_object"])
    product = P.get("product") or C["product"]
    pcode = (product or {}).get("code")
    tpl_row = ctpl.current(con, cls)
    tpl = (tpl_row or {}).get("template") or {}
    f = P.get("fields") or {}
    # признаки части: того же объекта — ввод договора и осмотр; другого объекта — история убытков, документы и
    # пожелания страхователя; поля части — поверх
    if main:
        op = {k: v for k, v in o.items() if k not in ("class_fields", "deductible")}
        if cls == m["class_code"] and o.get("class_fields"):
            op["class_fields"] = dict(o["class_fields"])
    else:
        op = {k: o.get(k) for k in ("losses_count", "small_count", "losses_amount", "documents_provided",
                                    "payer_type", "want_lower_premium", "term_days")}
    op.update({k: v for k, v in f.items() if v is not None})
    if op.get("protection") and op["protection"] not in (ax.PROT_CODES.get(cls) or []):
        op.pop("protection")              # защита другого класса (например, у техники) к этой части не относится
    kind = P.get("object_kind") or (C["kind"] if main else None)
    kind_type = tx.OBJECT_KINDS[kind][0] if kind in tx.OBJECT_KINDS else None
    obj_text = P.get("object_description") or (C["obj_text"] if main else "") or ""
    group = ae.object_group(cls, f"{kind_type or ''} {obj_text} {op.get('object_type') or ''}",
                            C["class_hint"] if main else "")
    otype = ae.match_object_type(ref, cls, kind_type, op.get("object_type"))
    kind_ra = kind or _kind_from_text(f"{obj_text} {op.get('object_type') or ''}")
    otype_ra = otype or ae.match_object_type(ref, cls, (tx.OBJECT_KINDS.get(kind_ra) or (None,))[0], None)
    special = "спецтехник" in str((product or {}).get("name") or "").lower()
    group_ra = "special" if group == "vehicle" and special else group
    # подгруппа транспорта части класса 3 (06.10.2026): мероприятия по подгруппе, как у однопродуктового акта
    veh_group = ae.vehicle_group(cls, op.get("class_fields"), kind, None, group_ra,
                                 text=f"{obj_text} {op.get('object_type') or ''} {(product or {}).get('name') or ''}")
    y = op.get("year") if op.get("year") is not None else (C["y"] if main else None)
    location = op.get("location") or (C["location"] if main else None)
    S, V = float(P["sum_insured"]), float(P["object_value"])
    term = C["term"]

    risk = ae.risk_level(ae.part_risk_inputs(C["risk_in"], f, main), st)
    cm = ae.class_min(ref, product, cls)
    rate_res = ae.part_rate(ref, product, cls, risk["level"], S, term, otype, op.get("payer_type"), st, cm)
    statutory = rate_res["mode"] in ("statutory", "statutory_undefined")
    # факторы объекта части (02.10.2026): по шаблону класса части, до вилки и франшизы
    fa = ae.factor_effect(aa.factor_stats(con, ae.factor_adjust(op.get("class_fields"), tpl, st, op),
                                          region_for_stats(m)), rate_res, S)
    # вилка ставки части (01.10.2026): поправки региона и рынка по классу и виду объекта части
    fs = C.get("fs") or ae.fork_settings(st)
    ferrs = []
    fork_in, fork_adj = _fork_prepare(con, fs, cls=cls, region=C["region_ra"], group=group_ra, product=product,
                                      rate_res=rate_res, errors=ferrs, min_source=cm.get("source"),
                                      scope=region_scope(m))
    errs += [dict(e, block=f"part{idx}:" + e["block"]) for e in ferrs]
    ae.apply_fork(rate_res, fork_adj, S)
    applicable = cls in ae.VALUE_CLASSES
    value = ae.value_check(S, V, st, op.get("price_new"), op.get("purchase_year"), group, kind_type or obj_text)
    value["applicable"] = applicable
    fr = ae.franchise({"small_count": op.get("small_count"), "dominant_risk": op.get("dominant_risk"),
                       "want_lower_premium": op.get("want_lower_premium")}, risk["level"], th, statutory, cls, S)
    ctx = ax.ra_context(con, cls=cls, product_code=pcode, otype=otype_ra, group=group_ra, kind=kind_ra, S=S, V=V,
                        region=C["region_ra"], term_days=o.get("term_days"), year=y, o=op,
                        recognized=C["recognized"] if main else [], text=" ".join(x for x in (
                            obj_text, op.get("object_type")) if x))
    if not ctx.get("ok"):
        errs.append({"block": f"part{idx}:risk_analytics", "error": ctx.get("error")})
    scen = ax.scenarios(ctx, cls, S, template=tpl, V=V, fields=op.get("class_fields"))
    requested = P.get("deductible") or o.get("deductible")
    try:
        fr = ax.franchise(con, ctx, fr, rate_res, cls=cls, S=S, level=risk["level"], statutory=statutory,
                          requested=requested, th=th)
    except Exception as e:
        errs.append({"block": f"part{idx}:franchise", "error": type(e).__name__})
        fr.update(status="error", applied=False, how=[], alternatives=[], error=type(e).__name__)
    fr["requested_from"] = "part" if P.get("deductible") else ("contract" if o.get("deductible") else None)
    premium_final = fr["premium_after"] if fr.get("applied") and fr.get("premium_after") is not None \
        else rate_res["premium"]
    rate_final = fr["rate_after"] if fr.get("applied") and fr.get("rate_after") is not None \
        else rate_res["applied_pct"]
    try:
        meas = ax.measures(con, ctx, rate_res, cls=cls, group=group_ra, kind=kind_ra, S=S, V=V, o=op,
                           location=location, statutory=statutory, th=th, premium=premium_final,
                           codes=veh_measure_codes(tpl, veh_group, group_ra))
    except Exception as e:
        errs.append({"block": f"part{idx}:measures", "error": type(e).__name__})
        meas = {"items": [], "total": {"count": 0}, "error": type(e).__name__}
    if fr.get("status") != "error":
        try:
            fr["alternatives"] = ax.alternatives(ctx, rate_res, S, meas, th)
        except Exception as e:
            errs.append({"block": f"part{idx}:alternatives", "error": type(e).__name__})
    tpl_risks = None
    if cls not in ax.RULE_CLASSES and not any(p["class_code"] == cls for p in ref.perils.values()):
        tpl_risks = ctpl.template_risks(tpl, cls) or None
    perrs = []
    analytics = _analytics_block(con, ctx, cls, pcode, C["region_ra"], S, V, term, rate_res, scen, meas,
                                 risk["level"], statutory, th, group_ra, tpl_risks, perrs)
    _napp_settings(analytics, fs, st)
    _region_scope_stats(analytics, region_scope(m))
    errs += [dict(e, block=f"part{idx}:" + e["block"]) for e in perrs]
    analytics["activity"] = (ctx.get("must") or {}).get("activity") if ctx.get("ok") else None
    analytics["activity_source"] = (ctx.get("sources") or {}).get("activity")
    analytics["object_type"] = (ctx.get("must") or {}).get("object_type") if ctx.get("ok") else None
    analytics["object_type_source"] = (ctx.get("sources") or {}).get("object_type")
    clause_codes = ctpl.for_group(tpl.get("clauses"), group)
    clauses = ae.clauses_by_codes(clause_codes, clause_catalog()) if clause_codes is not None \
        else ae.clauses(group, clause_catalog())
    views_req = ctpl.for_group(tpl.get("required_views"), group)
    if views_req is None:
        views_req = ae.required_views(group)
    # кредитная часть (класс 14, 13з): сумма и страхователь-банк — правило проекта № 6
    share = credit_rule(tpl, cls)
    docs = C.get("docs") or {}
    checks = ae.credit_check(S, op.get("class_fields"), share, _policyholder(docs.get("req"), docs.get("ct"),
                                                                            docs.get("upload") or {})) \
        if share is not None else []
    if kind is None and ctpl.single_kind(tpl):
        # единственный вид объекта класса (кредит, груз …) — по умолчанию; в расчёт не идёт, только в описание части
        kind_default = ctpl.single_kind(tpl)
    else:
        kind_default = None
    crow = db.rows(con, "SELECT name FROM classes WHERE code=?", cls)
    return {
        "index": idx, "class_code": cls, "class_name": crow[0]["name"] if crow else P.get("class_name"),
        "product_code": pcode, "product_name": (product or {}).get("name"), "product_own": bool(P.get("product")),
        "pricing_mode": (product or {}).get("pricing_mode"), "class_outside": bool(P.get("class_outside")),
        "class_guess": bool(P.get("class_guess")),
        "template_version": (tpl_row or {}).get("version"), "template_class": (tpl_row or {}).get("class_code"),
        "sum_insured": S, "share_pct": P.get("share_pct"), "object_value": V,
        "object_value_default": bool(P.get("object_value_default")), "object_kind": kind,
        "object_kind_default": kind_default,
        "object_description": P.get("object_description"), "same_object": main, "group": group,
        "object_type_ref": otype, "term_days": term, "level": risk["level"], "risk": risk, "rate": rate_res,
        "class_min": rate_res.get("class_min") or cm, "statutory": statutory, "value": value, "franchise": fr,
        "premium": premium_final, "premium_before_franchise": rate_res.get("premium"),
        "premium_final": {"amount": premium_final, "rate_pct": rate_final,
                          "franchise_applied": bool(fr.get("applied"))},
        "scenarios": scen, "measures": meas, "analytics": analytics, "clauses": clauses, "checks": checks,
        "missing": _part_missing(tpl, op, kind, main, C["recognized"]),
        "rate_fork": _fork_finish(fork_in, fork_adj, fs, rate_res, S, None, None, analytics, premium_final,
                                  bool(fr.get("applied")), factors=fa),
        "factor_adjustment": fa,
        "optional": {k: v for k, v in op.items() if v is not None},
        "template": _template_block(tpl_row, group, group_ra, views_req, clause_codes, tpl_risks, scen,
                                    op.get("class_fields")),
    }


def _apply_parts(con, clean: dict, D: dict, C: dict) -> None:
    """
    Комплексный продукт: части считаются по отдельности (_part_calc), верхние поля акта (премия, ставка, уровень,
    сценарии, франшиза, сумма к стоимости, решение, сверки с запросом и договором) — итоги договора. Средняя ставка
    договора — только справочно (правило проекта № 5).
    """
    m, st = C["m"], C["st"]
    S, V = float(m["sum_insured"]), float(m["object_value"])
    plan = _parts_plan(con, clean, C)
    classes = list(m.get("product_classes") or [])
    all_cls = [p["class_code"] for p in plan["items"]]
    notes = list(plan["notes"])
    same_default = ae.default_same_object(all_cls)
    items = []
    v_default = False
    # стоимость объекта договора делится между частями со страховой стоимостью (у ответственности, НС, кредита её нет)
    s_val = sum(float(p["sum_insured"]) for p in plan["items"] if p["class_code"] in ae.VALUE_CLASSES)
    for i, p in enumerate(plan["items"], 1):
        P = dict(p)
        P.setdefault("fields", {})
        if i == 1:
            P["same_object"] = True           # часть 1 — объект договора (осмотр, документы)
        elif P.get("same_object") is None:
            P["same_object"] = clean.get("same_object") if clean.get("same_object") is not None else same_default
        if P.get("object_value") is None:
            # стоимость части не введена: доля стоимости договора по доле суммы (у частей без страховой стоимости —
            # сама сумма части, сумма к стоимости у них не проверяется)
            if P["class_code"] in ae.VALUE_CLASSES:
                P["object_value"] = round(V * float(P["sum_insured"]) / s_val, 2) if s_val else \
                    float(P["sum_insured"])
                P["object_value_default"] = True
                v_default = True
            else:
                P["object_value"] = float(P["sum_insured"])
        if P.get("share_pct") is None:
            P["share_pct"] = round(float(P["sum_insured"]) / S * 100, 4) if S else None
        if P.get("class_outside"):
            notes.append({"code": "pt_n_outside", "params": {"n": i, "cls": P["class_code"],
                                                             "classes": ", ".join(classes)}})
        items.append(_part_calc(con, P, i, C))
    if v_default:
        notes.append({"code": "pt_n_value_default", "params": {}})
    if not plan["confirmed"]:
        notes.append({"code": "pt_n_confirm", "params": {}})
    totals = ae.contract_totals(items, S)
    agg = ae.aggregate_scenarios([{"index": p["index"], "class_code": p["class_code"], "main": p["same_object"],
                                   "scenarios": p["scenarios"]} for p in items])
    eml = (agg["items"].get("EML") or {}).get("amount") if agg["available"] else None
    mfl = (agg["items"].get("MFL") or {}).get("amount") if agg["available"] else None
    retention = ae.contract_retention([(p["scenarios"] or {}).get("retention") for p in items
                                       if (p["scenarios"] or {}).get("available")], eml or 0, mfl) \
        if agg["available"] else None
    same_all = all(p["same_object"] for p in items)
    diff_all = all(not p["same_object"] for p in items[1:])
    object_mode = "one" if same_all else ("different" if diff_all else "mixed")
    worst = max(items, key=lambda p: ae.LEVEL_ORDER.get(p["level"], 1))
    D["parts"] = {"mode": "multi", "source": plan["source"], "confirmed": bool(plan["confirmed"]),
                  "object_mode": object_mode, "same_object_default": same_default, "items": items,
                  "totals": dict(totals, scenarios=agg, retention=retention, worst_index=worst["index"],
                                 value=ae.contract_value([{"index": p["index"], "sum_insured": p["sum_insured"],
                                                           "object_value": p["object_value"],
                                                           "value_applicable": p["value"]["applicable"]}
                                                          for p in items], st)),
                  "notes": notes, "calibrated": ae.CALIBRATED,
                  "suggested": [{"class_code": p["class_code"], "product_code": p["product_code"]
                                 if p["product_own"] else None, "sum_insured": p["sum_insured"],
                                 "share_pct": p["share_pct"], "object_value": p["object_value"],
                                 "same_object": p["same_object"], "object_description": p["object_description"],
                                 "class_guess": p["class_guess"]} for p in items]}
    D["multi_class"] = True
    # верхние поля — итоги договора
    D["risk"] = dict(worst["risk"], contract=True, part_index=worst["index"])
    fr_any = [p for p in items if p["franchise"].get("applied")]
    D["premium_final"] = {"amount": totals["premium"], "rate_pct": None, "franchise_applied": bool(fr_any)}
    before = [p["premium_before_franchise"] for p in items]
    D["rate"] = {"mode": "multi", "base_pct": None, "base_source": "parts", "adj_pct": None, "calc_pct": None,
                 "applied_pct": None, "min_pct": None, "min_applied": False,
                 "premium": round(sum(before)) if all(x is not None for x in before) else None,
                 "term_days": C["term"], "object_type": None, "class_code": m["class_code"],
                 "product_code": m.get("product_code"), "pricing_mode": (C["product"] or {}).get("pricing_mode"),
                 "calibrated": ae.CALIBRATED, "engine_chain": [],
                 "reference_pct": totals["reference_rate_pct"], "reference_only": True,
                 "how": [{"code": "how_multi", "params": {"n": len(items)}}]}
    statuses = [p["franchise"].get("status") for p in items]
    D["franchise"] = {"needed": any(p["franchise"].get("needed") for p in items), "code": "fr_parts",
                      "status": "applied" if fr_any else ("proposed" if "proposed" in statuses else "none"),
                      "grounds": [], "size": None, "size_pct": None, "applied": bool(fr_any), "how": [],
                      "alternatives": [], "by_parts": True}
    sc_items = {}
    if agg["available"]:
        for s in ae.SCENARIOS3:
            a = agg["items"][s]["amount"]
            sc_items[s] = {"amount": a, "pct": round(a / S * 100, 1) if S else None, "what": "sc_w_parts_" + agg["rule"],
                           "what_params": {}, "state": None, "formula": None, "level": None, "source_scenario": s}
    D["scenarios"] = {"available": agg["available"], "reason": None if agg["available"] else "pt_sc_na",
                      "class_code": None, "rule": "parts", "items": sc_items, "retention": retention,
                      "assumptions": [], "calibrated": ae.CALIBRATED, "order": ax.SCENARIO_ORDER, "source": "parts",
                      "aggregate": agg, "order_ok": agg.get("order_ok")}
    cv = D["parts"]["totals"]["value"]
    if cv:
        D["value"] = dict(D["value"], **{k: cv[k] for k in ("ratio_pct", "verdict", "legal_ref", "diff")},
                          contract=True)
    # оговорки и мероприятия — все части (без повторов), с номером части
    seen, cl = set(), []
    for p in items:
        for c in p["clauses"]:
            if c["code"] not in seen:
                seen.add(c["code"])
                cl.append(c)
    D["clauses"] = cl
    ms, mseen = [], set()
    for p in items:
        for it in (p["measures"] or {}).get("items") or []:
            if it.get("code") not in mseen:
                mseen.add(it.get("code"))
                ms.append(dict(it, part_index=p["index"]))
    D["measures"] = {"items": ms, "total": {"count": len(ms), "by_parts": True}}
    # решение: осмотр и документы — как у договора, остальное — по частям; признак, введённый в части 1 (объект
    # договора), не считается недостающим
    own = items[0]["optional"]
    D["missing"] = [k for k in D["missing"] if own.get(k) in (None, "")]
    missing_key = [k for k in C["missing_key"] if own.get(k) in (None, "")]
    dec = ae.decision(D["risk"], {"mode": "tariff"}, D["value"], {"needed": False}, C["disc"], C["inspection"],
                      missing_key, st)
    chk = dec["checks"]
    if not plan["confirmed"]:
        chk.append({"code": "c_parts_confirm", "params": {"source": plan["source"]}})
    for p in items:
        n, c_ = p["index"], p["class_code"]
        base = {"n": n, "cls": c_}
        if p["rate"]["mode"] in ("undefined", "statutory_undefined"):
            chk.append({"code": "c_part_rate_undefined", "params": base})
        if p["statutory"]:
            chk.append({"code": "c_part_statutory", "params": base})
        if p["value"]["applicable"] and p["value"]["verdict"] in ("under", "over"):
            chk.append({"code": "c_part_" + p["value"]["verdict"], "params": base})
        if p["franchise"].get("needed"):
            chk.append({"code": "c_part_franchise", "params": base})
        if p["franchise"].get("applied"):
            chk.append({"code": "c_part_fr_applied", "params": base})
        if p["class_outside"] or p["class_guess"]:
            chk.append({"code": "c_part_class_check", "params": base})
        for c in p["checks"]:
            chk.append({"code": "c_part_" + c["code"], "params": dict(c["params"], **base)})
        if n > 1 and p.get("missing"):
            # обязательные поля шаблона класса у частей 2 и далее (у части 1 — в «чего не хватает» акта)
            chk.append({"code": "c_part_missing", "params": dict(base, labels=[dict(x["label"]) for x in p["missing"]],
                                                                  codes=[x["code"] for x in p["missing"]])})
    chk += C["mchecks"]
    fr_c = {"status": D["franchise"]["status"], "needed": D["franchise"]["needed"], "size_pct": None}
    rc, cc, xc, rq_checks, ct_checks = _doc_checks(st, m, C["docs"], D["rate"], None, totals["premium"],
                                                   D["value"], fr_c)
    chk += rq_checks + ct_checks
    if dec["code"] == "d_accept" and [c for c in chk if c["code"] != "c_confirm"]:
        dec["code"] = "d_accept_with_clauses"
    D["decision"] = dec
    D["request_check"], D["contract_check"], D["cross_check"] = rc, cc, xc
    # вилка ставки договора — справочно (сумма премий частей по каждой отметке); у частей — своя вилка
    D["rate_fork"] = ae.fork_contract(items, S, C["term"], (C["docs"].get("req") or {}).get("tariff_pct"),
                                      (C["docs"].get("ct") or {}).get("tariff_pct"), C["fs"]["mode"])


def _object_calc(con, ob: dict, X: dict) -> dict:
    """
    Один объект перечня (парк ТС) тем же расчётом, что однопродуктовый акт: уровень риска по своим признакам
    (год, состояние, осмотр своих фото), ставка act_engine.rate (тариф × поправка уровня, не ниже минимума продукта),
    факторы объекта по своим полям класса, вилка — поправки региона и рынка договора (данные читаются один раз),
    франшиза договора — тем же множителем, сумма к стоимости, сценарии PML/EML/MFL по своей сумме.
    """
    ref, st, o, cls = X["ref"], X["st"], X["o"], X["cls"]
    S, V = float(ob["sum_insured"]), float(ob["object_value"])
    term = X["term"]
    ids = ob.get("file_ids") or []
    rec = [r for r in X["recognized"] if r.get("file_id") in ids] if ids else []
    y, y_src = ob.get("year"), "input" if ob.get("year") is not None else None
    if y is None and rec:
        pr = preferred(rec, "year")
        y = ae.to_year(pr["value"]) if pr else None
        y_src = "photo" if y is not None else None
    if y is None and X["n"] == 1 and X["y"] is not None:
        y, y_src = X["y"], "contract"
    cf = dict(o.get("class_fields") or {})
    cf.update(ob.get("class_fields") or {})             # поля объекта сильнее общих полей договора
    op = {k: v for k, v in o.items() if k not in ("class_fields", "object_kind", "requested_rate_pct", "deductible",
                                                  "year", "condition")}
    if cf:
        op["class_fields"] = cf
    if y is not None:
        op["year"] = y
    cond = ob.get("condition") or o.get("condition")
    if cond:
        op["condition"] = cond
    kind = ob.get("object_kind") or X["kind"]
    kind_type = tx.OBJECT_KINDS[kind][0] if kind in tx.OBJECT_KINDS else None
    group = ae.object_group(cls, f"{kind_type or ''} {ob['label']}", X["class_hint"])
    otype = ae.match_object_type(ref, cls, kind_type, o.get("object_type"))
    kind_ra = kind or _kind_from_text(ob["label"])
    otype_ra = otype or ae.match_object_type(ref, cls, (tx.OBJECT_KINDS.get(kind_ra) or (None,))[0], None)
    group_ra = "special" if group == "vehicle" and X["special"] else group
    # осмотр: фото привязаны к объектам — у объекта только свои снимки и повреждения; не привязаны — осмотр парка
    # учитывается у всех объектов (так и пишется в акте)
    ri = dict(X["risk_in"], year=y)
    if X["bound"]:
        mine = set(ids)
        ri["inspected"] = bool(X["ai_ok"]) and bool(mine & X["model_ids"])
        ri["damages"] = [d for d in X["damages"] if d.get("file") in mine]
        ri["condition"] = cond if (ri["inspected"] or ob.get("condition") or o.get("condition")) else None
    elif cond:
        ri["condition"] = cond
    risk = ae.risk_level(ri, st)
    rate_res = ae.rate(ref, X["product"], cls, risk["level"], S, term, otype, o.get("payer_type"), st,
                       rate_type=X["rate_type"])
    _min_how(rate_res, X["min_info"])
    if X["no_products"]:
        rate_res = _no_products_rate(rate_res, cls)
    rate_res.pop("engine_chain", None)                 # цепочка калькулятора — у договора, у объекта не нужна
    fa = ae.factor_effect(aa.factor_stats(con, ae.factor_adjust(cf, X["tpl"], st, op), region_for_stats(X["m"])),
                          rate_res, S)
    adj = None
    if X["fork_in"] is not None:
        adj = ae.fork_adjust(_annual_view(rate_res), X["fork_in"]["region"], X["fork_in"]["market"], X["fs"])
        if rate_res.get("rate_type") != "fixed":
            ae.apply_fork(rate_res, adj, S)
    # франшиза договора (сотрудника) — тем же множителем к ставке объекта, не ниже минимума
    fr = X["fr"]
    fr_on = bool(fr.get("applied")) and fr.get("multiplier") is not None and rate_res.get("applied_pct") is not None
    if fr_on:
        rate_f, prem_f, floored = ax.apply_multiplier(rate_res, float(fr["multiplier"]), S)
    else:
        rate_f, prem_f, floored = rate_res.get("applied_pct"), rate_res.get("premium"), False
    value = ae.value_check(S, V, st, None, None, group, kind_type or ob["label"])
    value["applicable"] = cls in ae.VALUE_CLASSES
    ctx = ax.ra_context(con, cls=cls, product_code=(X["product"] or {}).get("code"), otype=otype_ra, group=group_ra,
                        kind=kind_ra, S=S, V=V, region=X["region_ra"], term_days=o.get("term_days"), year=y, o=op,
                        recognized=rec, text=ob["label"])
    scen = ax.scenarios(ctx, cls, S, template=X["tpl"], V=V, fields=cf)
    if not ctx.get("ok"):
        X["errors"].append({"block": f"object{ob['index']}:risk_analytics", "error": ctx.get("error")})
    req = ob.get("requested_rate_pct")
    minp = rate_res.get("min_pct")
    return {
        "index": ob["index"], "label": ob["label"], "object_kind": kind, "object_kind_own": bool(ob.get("object_kind")),
        "group": group, "object_type_ref": otype, "year": y, "year_source": y_src, "mileage": ob.get("mileage"),
        "plate_hint": ob.get("plate_hint"), "condition": ob.get("condition"), "photo_ids": list(ids),
        "photo_refs_unknown": list(ob.get("refs_unknown") or []), "inspected": bool(ri.get("inspected")),
        "damages": ri.get("damages") or [], "recognized": [{k: r.get(k) for k in ("key", "value", "source", "file_id")}
                                                         for r in rec[:30]],
        "class_fields": cf, "sum_insured": S, "object_value": V, "term_days": term, "level": risk["level"],
        "risk": risk, "rate": rate_res, "statutory": rate_res["mode"] in ("statutory", "statutory_undefined"),
        "premium_before_franchise": rate_res.get("premium"), "premium": prem_f, "rate_pct": rate_f,
        "franchise_applied": fr_on, "franchise_floor": bool(floored), "value": value, "factor_adjustment": fa,
        "scenarios": scen, "requested_rate_pct": req,
        "below_min": req is not None and minp is not None and float(req) + 1e-12 < float(minp),
        "rate_fork": _fork_finish(X["fork_in"], adj, X["fs"], _annual_view(rate_res), S, None, None, None, prem_f,
                                  fr_on, factors=fa),
    }


def _objects_scen(items: list, S: float, base: dict) -> tuple:
    """Сценарии договора из объектов: PML и EML — по самому крупному объекту (одно событие — один объект), MFL —
    сумма по всем объектам (накопление: весь парк в одном месте). Сложение — тем же act_engine.aggregate_scenarios,
    что у частей комплексного продукта (все объекты — «разные объекты»). (блок scenarios договора, разбор)."""
    rows = [{"index": p["index"], "class_code": base.get("class_code"), "main": False, "scenarios": p["scenarios"]}
            for p in items]
    agg_sum = ae.aggregate_scenarios(rows)
    avail = [p for p in items if (p["scenarios"] or {}).get("available")]
    largest = max(avail, key=lambda p: (p["sum_insured"], -p["index"])) if avail else None
    detail = {"largest": None, "sum": None, "excluded": agg_sum.get("excluded") or [], "calibrated": ae.CALIBRATED}
    if not largest:
        return {"available": False, "reason": "obj_sc_na", "class_code": base.get("class_code"), "rule": "objects",
                "items": {}, "retention": None, "assumptions": [], "calibrated": ae.CALIBRATED,
                "order": ax.SCENARIO_ORDER, "source": "objects", "objects": detail}, detail
    L = {s: round(float(largest["scenarios"]["items"][s]["amount"])) for s in ae.SCENARIOS3}
    Sm = {s: agg_sum["items"][s]["amount"] for s in ae.SCENARIOS3}
    detail["largest"] = dict(L, index=largest["index"], label=largest["label"], sum_insured=largest["sum_insured"])
    detail["sum"] = dict(Sm, count=len(avail))
    pick = {"PML": L["PML"], "EML": L["EML"], "MFL": max(Sm["MFL"], L["EML"])}
    items_ = {}
    for s in ae.SCENARIOS3:
        a = pick[s]
        items_[s] = {"amount": a, "pct": round(a / S * 100, 1) if S else None,
                     "what": "sc_w_obj_sum" if s == "MFL" else "sc_w_obj_largest",
                     "what_params": {} if s == "MFL" else {"n": largest["index"], "label": largest["label"]},
                     "state": None, "formula": None, "level": None, "source_scenario": s}
    ret = ae.contract_retention([(largest["scenarios"] or {}).get("retention")], pick["EML"], pick["MFL"])
    return {"available": True, "reason": None, "class_code": base.get("class_code"), "rule": "objects",
            "items": items_, "retention": ret, "assumptions": list(base.get("assumptions") or []),
            "calibrated": ae.CALIBRATED, "order": ax.SCENARIO_ORDER, "source": "objects", "objects": detail,
            "order_ok": pick["PML"] <= pick["EML"] <= pick["MFL"]}, detail


def _apply_objects(con, clean: dict, D: dict, X: dict) -> None:
    """
    Несколько объектов в одном акте (парк ТС, 02.10.2026): каждый объект — _object_calc; премия договора — сумма
    премий объектов, средняя ставка договора — справочно (класс один, ставка проверена по каждому объекту);
    уровень договора — самый высокий из объектов; сумма к стоимости — по договору и по каждому объекту; сценарии —
    _objects_scen; вилка договора — сумма премий объектов по каждой отметке (act_engine.fork_contract, как у частей).
    """
    st, m = X["st"], X["m"]
    S, V = float(m["sum_insured"]), float(m["object_value"])
    term = X["term"]
    # ссылки на файлы загрузки: id «f3» или номер файла в запросе «#3»; чужие и неизвестные — в пометку
    files = X["upload_files"]
    by_id = {f.get("id"): f.get("id") for f in files}
    by_index = {f"#{f.get('index')}": f.get("id") for f in files}
    objs = []
    for ob in clean["objects"]:
        ids, unknown = [], []
        for r in ob.get("photo_ids") or []:
            fid = by_id.get(r) if not r.startswith("#") else by_index.get(r)
            if fid and fid not in ids:
                ids.append(fid)
            elif not fid:
                unknown.append(r)
        objs.append(dict(ob, file_ids=ids, refs_unknown=unknown))
    X["bound"] = any(ob["file_ids"] for ob in objs)
    X["n"] = len(objs)
    items = [_object_calc(con, ob, X) for ob in objs]
    prem = [p["premium"] for p in items]
    before = [p["premium_before_franchise"] for p in items]
    total = round(sum(prem)) if all(x is not None for x in prem) else None
    total_before = round(sum(before)) if all(x is not None for x in before) else None
    rt = (D["rate"] or {}).get("rate_type") or "annual"

    def avg(x):
        if x is None or not S:
            return None
        return round(x / S * 100 * (1 if rt == "fixed" else 365 / term), 4)
    worst = max(items, key=lambda p: (ae.LEVEL_ORDER.get(p["level"], 1), p["sum_insured"]))
    cv = ae.contract_value([{"index": p["index"], "sum_insured": p["sum_insured"], "object_value": p["object_value"],
                             "value_applicable": p["value"]["applicable"]} for p in items], st)
    scen, sdetail = _objects_scen(items, S, D.get("scenarios") or {"class_code": X["cls"]})
    notes = []
    if not X["bound"] and len(items) > 1 and D["inspection"].get("photos"):
        notes.append({"code": "obj_n_unbound", "params": {}})
    if any(p["photo_refs_unknown"] for p in items):
        notes.append({"code": "obj_n_refs_unknown", "params": {"n": ", ".join(str(p["index"]) for p in items
                                                                           if p["photo_refs_unknown"])}})
    if X["fr"].get("applied"):
        notes.append({"code": "obj_n_franchise", "params": {"mult": X["fr"].get("multiplier")}})
    if m.get("sum_insured_from_objects") or m.get("object_value_from_objects"):
        notes.append({"code": "obj_n_sums_auto", "params": {}})
    D["objects"] = items
    D["objects_total"] = {
        "count": len(items), "sum_insured": round(sum(p["sum_insured"] for p in items), 2),
        "object_value": round(sum(p["object_value"] for p in items), 2), "premium": total,
        "premium_before_franchise": total_before, "rate_avg_pct": avg(total), "rate_avg_before_pct": avg(total_before),
        "rate_type": rt, "term_days": term, "level": worst["level"], "worst_index": worst["index"],
        "levels": {lv: sum(1 for p in items if p["level"] == lv) for lv in ae.LEVELS},
        "value": cv, "value_by_verdict": {v: [p["index"] for p in items if p["value"]["verdict"] == v]
                                          for v in ("under", "normal", "over")},
        "scenarios": sdetail, "bound": X["bound"], "notes": notes,
        "sums_from_objects": bool(m.get("sum_insured_from_objects")), "reference_only": True,
        "calibrated": ae.CALIBRATED}
    # верхние поля — итоги договора: уровень — самый высокий, премия — сумма, средняя ставка — справочно
    contract_risk = D["risk"]
    if ae.LEVEL_ORDER.get(worst["level"], 1) > ae.LEVEL_ORDER.get(contract_risk["level"], 1):
        D["risk"] = dict(worst["risk"], contract=True, object_index=worst["index"])
    rr = dict(D["rate"])
    same = lambda k: len({p["rate"].get(k) for p in items}) == 1   # noqa: E731
    rr.update(applied_pct=avg(total_before), premium=total_before, calc_pct=None, objects=True,
              base_pct=items[0]["rate"].get("base_pct") if same("base_pct") else None,
              adj_pct=items[0]["rate"].get("adj_pct") if same("adj_pct") else None,
              min_applied=any(p["rate"].get("min_applied") for p in items),
              fork_applied=any(p["rate"].get("fork_applied") for p in items),
              factors_applied=any(p["rate"].get("factors_applied") for p in items),
              annual_equiv_pct=round(total_before / S * 100 * 365 / term, 4) if rt == "fixed" and total_before
              is not None and S else None,
              how=[{"code": "how_objects", "params": {"n": len(items), "premium": total_before,
                                                      "rate": avg(total_before), "days": term}}] +
              [{"code": "how_object_line", "params": {"n": p["index"], "label": p["label"], "level": p["level"],
                                                      "rate": p["rate"].get("applied_pct"),
                                                      "premium": p["premium_before_franchise"]}} for p in items])
    D["rate"] = rr
    D["premium_final"] = {"amount": total, "rate_pct": avg(total), "franchise_applied": bool(X["fr"].get("applied"))}
    if X["fr"].get("applied"):
        D["franchise"] = dict(D["franchise"], premium_before=total_before, premium_after=total, rate_after=avg(total),
                              objects=True)
    D["scenarios"] = scen
    if cv:
        D["value"] = dict(D["value"], **{k: cv[k] for k in ("ratio_pct", "verdict", "legal_ref", "diff")},
                          contract=True)
    # вилка договора — справочно: сумма премий объектов по каждой отметке; рынок класса — как у договора
    cf_ = D.get("rate_fork") or {}
    fk = ae.fork_contract(items, S, term, (X["docs"].get("req") or {}).get("tariff_pct"),
                          (X["docs"].get("ct") or {}).get("tariff_pct"), X["fs"]["mode"])
    for mk in fk.get("marks") or []:
        if str(mk.get("note") or "").startswith("parts_"):
            mk["note"] = "objects_" + mk["note"][len("parts_"):]
        if (mk.get("source") or {}).get("kind") == "parts":
            mk["source"] = {"kind": "objects"}
    mkt = next((x for x in cf_.get("marks") or [] if x.get("code") == "market"), None)
    if mkt:
        fk["marks"].append(dict(mkt, premium=round(ae.premium_of(float(mkt["rate_pct"]), S, term))
                                if mkt.get("rate_pct") is not None else None))
    fk.update(objects=True, count=len(items), adjustments=cf_.get("adjustments"), region_data=cf_.get("region_data"),
              market_data=cf_.get("market_data"), rate_type=rt)
    D["rate_fork"] = fk
    # факторы объекта — по каждому объекту (свои поля класса), как у частей комплексного продукта
    D["factor_adjustment"] = {"by_objects": True, "mode": ae.factor_settings(st)["mode"],
                              "objects": [dict(p["factor_adjustment"], index=p["index"], label=p["label"])
                                          for p in items], "calibrated": ae.CALIBRATED}
    # мероприятия: та же функция, что у акта, от премии договора по объектам и средней ставки (не ниже минимума)
    try:
        D["measures"] = ax.measures(con, X["ctx"], D["rate"], cls=X["cls"], group=X["group_ra"], kind=X["kind_ra"],
                                    S=S, V=V, o=X["o"], location=X["location"], statutory=X["statutory"], th=X["th"],
                                    premium=total, codes=X["meas_codes"])
    except Exception as e:
        X["errors"].append({"block": "objects:measures", "error": type(e).__name__})
    # чего не хватает: подпись объекта — его вид, марка и модель; год и пробег — если введены у всех объектов
    have = {"object_type", "brand", "model"}
    if all(p["year"] is not None for p in items):
        have.add("year")
    if all(p["mileage"] is not None for p in items):
        have.add("mileage")
    D["missing"] = [k for k in D["missing"] if k not in have]
    for c in D["decision"]["checks"]:
        if c["code"] == "c_missing":
            c["params"] = dict(c["params"], keys=[k for k in c["params"].get("keys") or [] if k not in have])
    D["decision"]["checks"] = [c for c in D["decision"]["checks"]
                               if not (c["code"] == "c_missing" and not c["params"].get("keys"))]
    # сверки с запросом и договором — по итогам договора (средняя ставка, сумма премий)
    fr_c = {"status": D["franchise"].get("status"), "needed": D["franchise"].get("needed"),
            "size_pct": D["franchise"].get("size_pct")}
    rc, cc, xc, rq_checks, ct_checks = _doc_checks(st, m, X["docs"], D["rate"], avg(total), total, D["value"], fr_c)
    dec = D["decision"]
    # сверки, посчитанные по договору как по одному объекту, заменяются сверками по итогам объектов
    dec["checks"] = [c for c in dec["checks"] if not any(c is d for d in X["doc_checks"])] + rq_checks + ct_checks
    D["request_check"], D["contract_check"], D["cross_check"] = rc, cc, xc
    # проверки по объектам: сумма к стоимости, самый высокий уровень, все повышающие признаки, ставка ниже минимума
    for p in items:
        base = {"n": p["index"], "label": p["label"]}
        if p["value"]["applicable"] and p["value"]["verdict"] in ("under", "over"):
            dec["checks"].append({"code": "c_obj_" + p["value"]["verdict"], "params": base})
        if p["risk"]["up"] >= int(st["decline_min_up"]):
            dec["checks"].append({"code": "c_obj_decline", "params": base})
        elif p["level"] == "high":
            dec["checks"].append({"code": "c_obj_high", "params": base})
        if p["below_min"]:
            dec["checks"].append({"code": "c_obj_below_min", "params": dict(base, req=p["requested_rate_pct"],
                                                                             min=p["rate"].get("min_pct"))})
        if X["bound"] and not p["inspected"]:
            dec["checks"].append({"code": "c_obj_no_photos", "params": base})
    if dec["code"] == "d_accept" and (D["risk"]["level"] != "low" or [c for c in dec["checks"]
                                                                    if c["code"] != "c_confirm"]):
        dec["code"] = "d_accept_with_clauses"
