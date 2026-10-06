"""
Расчётные блоки акта: минимальная ставка, вилка ставки, аналитика, сверки с документами, решение,
оценка заниженной ставки, шаблон класса.
"""
from datetime import date
from typing import Optional

from .. import act_analytics as aa
from .. import act_engine as ae
from .. import class_templates as ctpl
from .. import contract_read as cr
from .. import db
from .. import min_rates as mrs
from ..analysis_docs import OBJECT_TYPE_WORDS, _one

from .terms import _attach_trust, _with_object


def _min_info(con, product: Optional[dict], payer_type: Optional[str] = None) -> Optional[dict]:
    """Минимальная ставка страховщика по продукту на дату акта (сегодня) с источником и типом ставки; сбой — None
    (акт формируется как раньше, тип ставки — годовая)."""
    code = (product or {}).get("code")
    if not code:
        return None
    try:
        return mrs.on_date(con, code, date.today(), payer_type)
    except Exception as e:
        print("акт: минимальная ставка страховщика не прочитана:", type(e).__name__)
        return None


def _min_applies(min_info: Optional[dict], rate_res: dict) -> bool:
    """Минимум акта — это минимальная ставка страховщика (а не минимум регулятора выше неё)."""
    return bool(min_info) and rate_res.get("min_pct") is not None and rate_res.get("mode") == "tariff" \
        and abs(float(min_info["pct"]) - float(rate_res["min_pct"])) < 1e-9


def _min_src_row(min_info: dict) -> dict:
    return {k: min_info.get(k) for k in ("source", "effective_from", "note", "document_ref", "name")}


def _min_how(rate_res: dict, min_info: Optional[dict]) -> None:
    """Строка «как посчитано»: откуда минимальная ставка (правка администратора с датой и примечанием или тарифная
    политика) — сразу после сравнения с минимумом."""
    if not _min_applies(min_info, rate_res):
        return
    how = rate_res["how"]
    at = next((i for i, h in enumerate(how) if h["code"] in ("how_min_ok", "how_min_applied")), None)
    item = {"code": "how_min_src", "params": {"min_src": _min_src_row(min_info)}}
    if at is None:
        how.append(item)
    else:
        how.insert(at + 1, item)


def _min_block(min_info: Optional[dict], rate_res: dict) -> dict:
    """Минимальная ставка акта и её источник — в данных акта (старый акт показывает минимум своей даты)."""
    applies = _min_applies(min_info, rate_res)
    out = {"min_pct": rate_res.get("min_pct"), "rate_type": rate_res.get("rate_type") or "annual",
           "insurer": applies, "calibrated": ae.CALIBRATED}
    if min_info:
        out.update(insurer_pct=min_info["pct"], source=min_info["source"], version_id=min_info["version_id"],
                   effective_from=min_info["effective_from"], note=min_info.get("note"),
                   document_ref=min_info.get("document_ref"), name=min_info.get("name"),
                   set_at=min_info.get("created_at"))
    return out


def _annual_view(rate_res: dict) -> dict:
    """Ставка акта в годовом выражении для вилки и рынка: у фиксированной (на весь срок) — × 365 / дни; премия та же."""
    if rate_res.get("rate_type") != "fixed" or rate_res.get("applied_pct") is None:
        return rate_res
    term = int(rate_res.get("term_days") or 365)
    out = dict(rate_res)
    for k in ("applied_pct", "min_pct", "calc_pct", "base_pct"):
        if out.get(k) is not None:
            out[k] = round(ae.annual_pct(out[k], term, "fixed"), 4)
    return out


def _net_calibrated(con, cls: str, analytics: Optional[dict]) -> bool:
    """Калибрована ли базовая нетто-ставка, на которой стоит нетто-ставка расчётного модуля (base_rates.calibrated)."""
    tf = (analytics or {}).get("tariff") or {}
    if not tf.get("available"):
        return False
    try:
        rows = db.rows(con, "SELECT object_type, calibrated FROM base_rates WHERE class_code=?", cls)
    except Exception:
        return False
    if tf.get("base_kind") == "object_type":
        return any(r["object_type"] == tf.get("object_type") and r["calibrated"] for r in rows)
    return bool(rows) and all(r["calibrated"] for r in rows)


def _below_min_block(D: dict, o: dict, req: Optional[dict], ct: Optional[dict], rate_res: dict,
                     analytics: Optional[dict], scen: Optional[dict], risk: dict, documents: bool, y, st: dict,
                     statutory: bool, multi: bool, net_cal: bool) -> dict:
    """Оценка заниженной ставки (act_engine.below_min_assess) по данным акта: запрошенная ставка — введённая
    сотрудником, иначе из договора, иначе из запроса филиала."""
    if o.get("requested_rate_pct") is not None:
        rq, src = o["requested_rate_pct"], "employee"
    elif (ct or {}).get("tariff_pct") is not None:
        rq, src = ct["tariff_pct"], "contract"
    elif (req or {}).get("tariff_pct") is not None:
        rq, src = req["tariff_pct"], "request"
    else:
        rq, src = None, None
    base = {"available": False, "requested_pct": rq, "requested_source": src, "min_pct": rate_res.get("min_pct"),
            "calibrated": ae.CALIBRATED}
    if multi:
        return dict(base, reason="by_parts")
    if rate_res.get("mode") not in ("tariff", "statutory"):
        return dict(base, reason="no_rate")
    an = analytics if (analytics or {}).get("available") else {}
    tf = an.get("tariff") or {}
    net = tf.get("net_pct") if tf.get("available") else None
    sc = scen or {}
    ret = sc.get("retention") or {}
    within = ret.get("within") if ret.get("known") else None
    eml = ((sc.get("items") or {}).get("EML") or {}).get("amount")
    mk = an.get("market") or {}
    mrate = mk.get("rate_pct") if mk.get("available") else None
    lr = mk.get("loss_ratio_pct") if mk.get("available") else None
    fork_mk = ((D.get("rate_fork") or {}).get("adjustments") or {}).get("market") or {}
    if fork_mk.get("loss_ratio_pct") is not None:
        lr = fork_mk["loss_ratio_pct"]            # та же база убыточности, что у поправки рынка вилки
    if mrate is None:
        mrate = ((D.get("rate_fork") or {}).get("market_data") or {}).get("rate_pct")
    new_years = int(st.get("new_object_years", 1))
    obj_new = (date.today().year - int(y) <= new_years) if y else None
    out = ae.below_min_assess(
        requested_pct=rq, requested_source=src, min_pct=rate_res.get("min_pct"),
        rate_type=rate_res.get("rate_type") or "annual", term_days=rate_res.get("term_days") or 365,
        sum_insured=D["must"]["sum_insured"], statutory=statutory, level=risk.get("level"),
        losses_count=o.get("losses_count"), documents=bool(documents), object_new=obj_new, net_pct=net,
        net_calibrated=net_cal, retention_within=within, eml=eml, retention_limit=ret.get("limit"),
        market_rate_pct=mrate, loss_ratio_pct=lr, settings=st)
    out["min_insurer"] = bool((D.get("min_rate") or {}).get("insurer"))
    return out


def _with_clauses(dec: dict, checks: list) -> None:
    """Проверки андеррайтеру — в решение; есть хоть одна — «принять без оговорок» уже нельзя."""
    dec["checks"] += checks
    if checks and dec["code"] == "d_accept":
        dec["code"] = "d_accept_with_clauses"


def _below_min_decision(D: dict) -> None:
    """Пункт «Отступление от минимальной ставки» в проверках андеррайтера; «принять без оговорок» уже нельзя."""
    bm = D.get("below_min") or {}
    if not bm.get("available"):
        return
    _with_clauses(D["decision"], [{"code": "c_below_min", "params": {"verdict": bm["verdict"],
                                                                      "req": bm["requested_pct"],
                                                                      "min": bm["min_pct"]}}])


def _no_products_rate(rate_res: dict, cls: str) -> dict:
    """Класс без продуктов страховщика (16у, 18): ставка и премия не определены — тарифной политики по классу нет;
    экспертная базовая ставка справочника (если есть) в «как посчитана ставка» не выдаётся за ставку акта."""
    out = dict(rate_res, mode="undefined", base_pct=None, base_source=None, adj_pct=None, calc_pct=None,
               applied_pct=None, min_pct=None, min_applied=False, premium=None, engine_chain=[])
    out["how"] = [{"code": "how_no_products", "params": {"cls": cls}}]
    return out


def _fork_prepare(con, fs: dict, *, cls: str, region: str, group: Optional[str], product: Optional[dict],
                  rate_res: dict, errors: list, min_source: Optional[str] = None,
                  min_info: Optional[dict] = None, scope: Optional[str] = None) -> tuple:
    """Данные региона и рынка (только чтение базы) и поправки вилки к ставке акта → (данные, поправки) или
    (None, None) при сбое: акт формируется, вилка — с reason = error. scope (region_scope): republic — вся
    республика, outside — вне Узбекистана; поправка региона у обоих 0 со своей причиной."""
    try:
        fin = aa.fork_data(con, cls=cls, region=region, group=group, fs=fs, product_code=(product or {}).get("code"),
                           min_pct=rate_res.get("min_pct"), min_source=min_source,
                           statutory_ref=(product or {}).get("rate_text"), min_info=min_info)
    except Exception as e:
        errors.append({"block": "rate_fork", "error": type(e).__name__})
        return None, None
    if scope:
        R = fin["region"]
        R.update(pct=0.0, raw_pct=None, clamped=None, used=0, reason=scope)
        for i in R.get("indicators") or []:
            if i.get("why") != "kind":
                i.update(used=False, effect_pct=None, why=scope)
    return fin, ae.fork_adjust(rate_res, fin["region"], fin["market"], fs)


def _fork_finish(fin: Optional[dict], adj: Optional[dict], fs: dict, rate_res: dict, S: float, request_pct, contract_pct,
                 analytics: Optional[dict], premium_final, fr_applied: bool, factors: Optional[dict] = None) -> dict:
    """Вилка ставки одного класса (act_engine.fork_build) с данными региона и рынка для показа; factors — факторы
    объекта (act_engine.factor_adjust с effect): в режиме reference — отметка «с учётом факторов объекта»."""
    if fin is None:
        return {"available": False, "reason": "error", "mode": fs["mode"], "unit": "% годовых", "marks": [],
                "adjustments": None, "recommended": None, "position": {"request": "none", "contract": "none"},
                "calibrated": ae.CALIBRATED}
    tech = ((analytics or {}).get("engine") or {}).get("technical_pct") if (analytics or {}).get("available") else None
    out = ae.fork_build(rate_res=rate_res, adj=adj, sum_insured=S, market=fin["market"], sources=fin["sources"],
                        request_pct=request_pct, contract_pct=contract_pct, technical_pct=tech,
                        premium_final=premium_final, franchise_applied=fr_applied, mode=fs["mode"],
                        factors=factors)
    mk = fin["market"]
    out["market_data"] = {k: mk.get(k) for k in ("rate_pct", "rate_date", "loss_ratio_pct", "loss_ratio_full_year_pct",
                                                 "rate_full_year_pct", "full_year_period", "row_key", "pack",
                                                 "pack_choice", "class_rows", "source")}
    out["region_data"] = fin["region"]
    if out.get("adjustments"):
        out["adjustments"]["market"] = dict(out["adjustments"]["market"], as_of=mk.get("rate_date"),
                                            source=mk.get("source"), row_key=mk.get("row_key"), pack=mk.get("pack"),
                                            pack_choice=mk.get("pack_choice"),
                                            rate_full_year_pct=mk.get("rate_full_year_pct"),
                                            class_rows=mk.get("class_rows") or [])
    return out


def _region_scope_stats(analytics: dict, scope: Optional[str]) -> None:
    """Статистика региона при особом регионе: вне Узбекистана открытые данные (stat.uz, НАПП по регионам) не
    применяются — блоки stats и napp пустые с причиной outside; вся республика — показатели по республике с
    пометкой republic (сравнивать регион с республикой не с чем)."""
    if not scope or not isinstance(analytics, dict) or not analytics.get("available"):
        return
    st_ = analytics.get("stats") or {}
    if scope == "outside":
        analytics["stats"] = {"available": False, "applicable": False, "reason": "outside", "indicators": [],
                              "not_found": {}, "points": None, "calibrated": ae.CALIBRATED}
        analytics["napp"] = {"available": False, "reason": "outside", "calibrated": ae.CALIBRATED}
    else:
        analytics["stats"] = dict(st_, scope="republic")


def _napp_settings(analytics: dict, fs: dict, st: Optional[dict]) -> None:
    """Блок napp аналитики: вес claims_freq в поправке региона (строка «Претензии в регионе» — справочно или в
    поправке) и порог малой базы подразделений (настройка napp.branch_min_contracts)."""
    NP = analytics.get("napp") if isinstance(analytics, dict) else None
    if not isinstance(NP, dict):
        return
    NP["claims_weight"] = float(((fs or {}).get("region") or {}).get("weights", {}).get("claims_freq", 1.0))
    NP["branch_min_contracts"] = int((ae.merge_settings(st or {}).get("napp") or {}).get("branch_min_contracts", 200))


def veh_measure_codes(tpl: dict, veh_group: Optional[str], group_ra: str) -> Optional[list]:
    """Мероприятия шаблона: своя подгруппа транспорта (car, truck, …) → спецтехника (special) → группа объекта."""
    d = (tpl or {}).get("measures")
    if veh_group and isinstance(d, dict):
        if isinstance(d.get(veh_group), list):
            return list(d[veh_group])
        if veh_group in ae.VEH_SPECIAL and isinstance(d.get("special"), list):
            return list(d["special"])
    return ctpl.for_group(d, group_ra)


def _analytics_block(con, ctx, cls, product_code, region_ra, S, V, term, rate_res, scen, meas, level, statutory, th,
                     group_ra, tpl_risks, block_errors, veh_group=None) -> dict:
    """Аналитика раздела 4 (act_analytics.build) для одного класса; сбой — блок с reason = error."""
    try:
        analytics = aa.build(con, ctx, cls=cls, product_code=product_code, region=region_ra, S=S, V=V,
                             term_days=term, rate_res=rate_res, scen=scen, meas=meas, act_level=level,
                             statutory=statutory, th=th, group=group_ra, tpl_risks=tpl_risks, veh_group=veh_group)
        for e in analytics.get("errors") or []:
            block_errors.append({"block": "analytics:" + e["block"], "error": e["error"]})
    except Exception as e:
        block_errors.append({"block": "analytics", "error": type(e).__name__})
        analytics = {"available": False, "reason": "error", "calibrated": ae.CALIBRATED}
    return analytics


def _doc_checks(st: dict, m: dict, docs: dict, rate_res: dict, rate_final, premium_final, value: dict,
                fr: dict) -> tuple:
    """Сверка запроса филиала, договора и «запрос ↔ договор» с расчётом акта → (rc, cc, xc, rq_checks, ct_checks)."""
    req, ct, upload = docs["req"], docs["ct"], docs["upload"]
    rf = rate_final if rate_res["mode"] not in ("undefined", "multi") else None
    rc = ae.request_check(req, rate_res=rate_res, rate_final=rf, premium_final=premium_final,
                          sum_insured=m["sum_insured"], object_value=m["object_value"], value=value, fr=fr,
                          term_from_request=docs["term_from_request"], settings=st,
                          rate_type=rate_res.get("rate_type") or "annual")
    _attach_trust(rc, docs["rq_trust"])
    rq_checks = ae.request_checks(rc)
    if (docs["rq_trust"] or {}).get("edits"):
        rq_checks.append({"code": "c_rq_edits", "params": {"n": len(docs["rq_trust"]["edits"])}})
    cc = ae.contract_check(ct, rate_res=rate_res, rate_final=rf, premium_final=premium_final,
                           sum_insured=m["sum_insured"], object_value=m["object_value"], value=value, fr=fr,
                           term_from_contract=docs["term_from_contract"], settings=st,
                           rate_type=rate_res.get("rate_type") or "annual")
    _attach_trust(cc, docs["ct_trust"])
    ct_checks = ae.request_checks(cc, "ct")
    if (docs["ct_trust"] or {}).get("edits"):
        ct_checks.append({"code": "c_ct_edits", "params": {"n": len(docs["ct_trust"]["edits"])}})
    # запрос филиала против договора: объект (кадастр, вид) — присланный, а при живой загрузке — сохранённый
    xc = cr.cross_check(_with_object(req, (upload.get("branch_request") or {}).get("fields")),
                        _with_object(ct, (upload.get("contract") or {}).get("fields")),
                        float(st["request_check"]["premium_tolerance"]))
    x_codes = [i["code"] for i in (xc or {}).get("items") or [] if i["verdict"] == "differs"]
    if x_codes:
        ct_checks.append({"code": "c_x", "params": {"codes": x_codes}})
    return rc, cc, xc, rq_checks, ct_checks


def _template_block(row: Optional[dict], group: str, group_ra: str, views_req: list, clause_codes: Optional[list],
                    tpl_risks: Optional[list], scen: dict, fields: Optional[dict]) -> Optional[dict]:
    """Что акт взял из шаблона класса — сохраняется в акте (подписи ru/uz/en, язык выбирается при выдаче)."""
    if not row:
        return None
    tpl = row["template"]
    sr = tpl.get("scenario_rule") or {}
    return {"class_code": row["class_code"], "requested_class": row.get("requested_class"),
            "alias_of": row.get("alias_of"), "version": row["version"], "source": row["source"],
            "variant": bool(tpl.get("variant")), "variant_of": tpl.get("variant_of"),
            "official_name": tpl.get("official_name"),
            "name": tpl.get("name"), "object": tpl.get("object"),
            "must": tpl.get("must") or [], "optional": tpl.get("optional") or [],
            "valuation_methods": tpl.get("valuation_methods") or [],
            "required_views": list(views_req or []), "clauses": list(clause_codes or []),
            "measures": ctpl.for_group(tpl.get("measures"), group_ra) or [],
            "risks_source": "template" if tpl_risks else ((tpl.get("risks") or {}).get("source")),
            "risks_used": bool(tpl_risks), "risks": tpl.get("risks"),
            "factors": tpl.get("factors") or [],
            "scenario_rule": {"code": sr.get("code"), "engine": sr.get("engine"), "text": sr.get("text"),
                              "simple_rule": sr.get("simple_rule"), "used": scen.get("source") == "template",
                              "params": sr.get("params") or {}},
            "documents": tpl.get("documents") or {}, "stats": tpl.get("stats") or [],
            "notes": tpl.get("notes") or [], "reinsurance_usually": bool(tpl.get("reinsurance_usually")),
            "class_fields": dict(fields or {}), "group": group, "calibrated": ae.CALIBRATED}


_KIND_BY_TYPE = {"Склад": "warehouse", "Офис": "office", "Магазин": "shop", "Производство": "production",
                 "Гостиница": "hotel", "Жильё": "dwelling", "Машины и оборудование": "equipment"}


def _kind_from_text(text: str) -> Optional[str]:
    """Вид объекта по словам документа — только однозначное совпадение (analysis_docs._one)."""
    return _KIND_BY_TYPE.get(_one(text, OBJECT_TYPE_WORDS) or "")


def _location_from_text(text: str) -> Optional[str]:
    """Место с фото (словами на любом из трёх языков) → код. Не узнали — None, а не догадка."""
    s = str(text or "").lower()
    table = (("guarded", ("охраня", "qoʻriqlan", "qoriqlan", "guarded", "fenced")),
             ("closed_storage", ("закрыт", "помещен", "ангар", "гараж", "yopiq", "indoor", "garage", "hangar")),
             ("construction", ("строй", "стройк", "qurilish", "construction")),
             ("port", ("порт", "port")),
             ("open_area", ("открыт", "площадк", "ochiq", "open", "yard")))
    for code, words in table:
        if any(w in s for w in words):
            return code
    return None
