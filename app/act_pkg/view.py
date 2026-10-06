"""render — акт из пяти разделов на нужном языке; заёмщик, сверки с запросом и договором, справка биржи."""
from datetime import datetime
from types import SimpleNamespace
from typing import Optional

from .. import act_engine as ae
from .. import act_scoring as asc
from .. import class_templates as ctpl
from .. import act_market as am
from .. import act_texts as tx
from ..act_texts import money, pct, t
from .. import uzex_sources as us

from .recognize import recognized_view
from .regions import region_label
from .view_fmt import _ddmmyyyy, money_k, _money_na, _pct_na
from .documents import _cr_value, cross_view, _risk_view, _x_value
from .view_rows import (_check_text, _class_label, _disc_text, _doc_kind_label, _object_rows, _row, _rq_params,
    _s1_label, _text)
from .view_factors import _factor_view
from .view_fork import _fork_parts_view, _fork_view, _territory
from .view_blocks import (_alt_view, _fr_how_text, _fr_text, _franchise_extra, _measures_view, _scenarios_view,
    scenario_facts)
from .view_analytics import _analytics_view
from .view_parts import _below_min_view, _min_rate_view, _min_src_text, _objects_view, _parts_view


def render(D: dict, lang: str, meta: dict) -> dict:
    """Акт на языке lang из структурированных данных. Все цифры — из расчёта, слова — из act_texts."""
    lang = tx.lang_of(lang)
    NA = t("na", lang)
    ins, risk, rate_res, value, fr = D["inspection"], D["risk"], D["rate"], D["value"], D["franchise"]
    must, opt = D["must"], D["optional"]
    rec = D["recognized"]

    # комплексный продукт (30.09.2026): перечень частей договора
    PV = _parts_view(D, lang) if (D.get("parts") or {}).get("mode") == "multi" else None
    OV = _objects_view(D, lang) if D.get("objects") else None
    # разделы 1–5 по порядку; v3–v5 — раздел и представления, нужные ответу ниже
    s1 = _render_s1(D, lang, PV, OV)
    s2 = _render_s2(D, lang)
    v3 = _render_s3(D, lang, PV, OV)
    v4 = _render_s4(D, lang, PV, OV)
    v5 = _render_s5(D, lang, PV, OV, v4.FAV, v4.bmv)

    sections = [s1, s2, v3.s3, v4.s4, v5.s5]
    for s in sections:
        s.setdefault("lists", [])
        s.setdefault("source_lines", [])      # строки источника сразу под строками раздела
    created = meta["created_at"]
    out = {
        "ok": True, "id": meta["id"], "number": meta["number"], "lang": lang,
        "title": t("title", lang), "insurer": D.get("insurer") or NA, "insurer_known": bool(D.get("insurer")),
        "date": datetime.fromisoformat(created).strftime("%d.%m.%Y"),
        "created_at": created, "expires_at": meta["expires_at"],
        "header": [_row(t("number", lang), meta["number"]),
                   _row(t("date", lang), datetime.fromisoformat(created).strftime("%d.%m.%Y")),
                   _row(t("insurer", lang), D.get("insurer") or NA)],
        "sections": sections,
        "risk": {"level": risk["level"], "level_label": v4.level_label, "net": risk["net"], "up": risk["up"],
                 "down": risk["down"], "calibrated": ae.CALIBRATED, "rule": v4.rule_text,
                 "factors": [{"code": f["code"], "sign": f["sign"], "text": _text(f, lang)}
                             for f in risk["factors"]]},
        "rate": _rate_json(D, lang, PV, v4.mode, v4.pf),
        "premium": {"amount": v4.pf["amount"], "term_days": rate_res["term_days"], "currency": "UZS",
                    "text": _money_na(v4.pf["amount"], lang),
                    "before_franchise": rate_res["premium"], "rate_pct": v4.pf["rate_pct"],
                    "franchise_applied": v4.pf["franchise_applied"]},
        "value": {"ratio_pct": value["ratio_pct"], "verdict": value["verdict"], "text": v3.vtext,
                  "legal_ref": value["legal_ref"], "legal_ref_text": v3.legal,
                  "depreciated": value.get("depreciated"),
                  # итоговый вывод раздела 3 и плитки сводки: normal | under | over | refine
                  "final_verdict": v3.final, "final_text": v3.vtext,
                  "refined_ratio_pct": ((v3.M or {}).get("insured_check") or {}).get("ratio_pct")
                  if v3.final == "refine" else None,
                  "declared_original": opt.get("declared_value_original"),
                  "value_source": v3.decl[2] if v3.decl else None},
        "market_value": v3.mv["json"],
        "request_check": v4.rcv["json"],
        "contract_check": v4.ccv["json"],
        # оценка заниженной ставки (01.10.2026); ставка не ниже минимума или её нет — available = false
        "below_min_assessment": v4.bmv["json"],
        # минимальная ставка страховщика на дату акта и её источник (app/min_rates.py)
        "min_rate": _min_rate_view(D, lang),
        "cross_check": v4.xv or {"available": False},
        "franchise": {"needed": bool(fr.get("needed")), "text": v4.fr_text,
                      "grounds": [{"code": g["code"], "text": _text(g, lang)} for g in fr.get("grounds") or []],
                      **({"size": fr["size"]} if fr.get("size") else {}),
                      **_franchise_extra(fr, v4.fr_how, v4.fr_alts, lang)},
        "scenarios": v4.scv["json"],
        # аналитика раздела 4 (30.09.2026): риски, факторы, чувствительность, состав тарифа, сценарии подробно,
        # удержание, балл, рынок и статистика с источниками, франшиза справочно, мероприятия, резюме
        "analytics": dict(v4.anv["json"] or {}, exchange=v3.EX["json"]),
        "measures": [m["json"] for m in v5.msv["items"]],
        "measures_summary": v5.msv["summary"],
        "clauses": [{"code": c["code"], "text": c.get(lang) or c.get("ru"), "expert": True,
                     "calibrated": ae.CALIBRATED} for c in D["clauses"]],
        "discrepancies": [{"key": d["key"], "label": tx.field_label(d["key"], lang, D["group"]),
                           "values": [{"value": g["value"], "sources": g["sources"],
                                       "source_labels": [tx.label(tx.SOURCE_LABELS, s, lang) for s in g["sources"]]}
                                      for g in d["values"]],
                           "priority": d["priority"], "text": _disc_text(d, lang, D["group"])} for d in D["discrepancies"]],
        "decision": {"code": v5.dec["code"].replace("d_", ""), "text": t(v5.dec["code"], lang), "checks": v5.checks},
        "missing": v5.missing_labels,
        "inspection": {"done": bool(ins["photos"] and ins["ai"]), "photos": ins["photos"],
                       "views_seen": ins["views_seen"], "missing_views": ins["missing_views"],
                       "damages": [dict(d, severity=ae.damage_severity(d)) for d in ins["damages"] or []],
                       "damages_summary": _damages_json(ins["damages"], lang), "documents": ins["documents"]},
        "recognized": recognized_view(rec, lang, group=D["group"]),
        # шаблон анализа класса, по которому собран акт (справочник class_templates): версия, поля класса,
        # документы и статистика класса — на языке акта; акты до 30.09.2026 — без шаблона
        "template": ctpl.localize(D["template"], lang) if D.get("template") else None,
        # комплексный продукт по частям (30.09.2026); акты до этой даты и однопродуктовые — mode single
        "parts": PV["json"] if PV else {"mode": "single", "source": None, "confirmed": True, "items": [],
                                        "totals": None, "notes": []},
        **({"suggested_parts": PV["json"]["suggested_parts"]} if PV and not PV["json"]["confirmed"] else {}),
        "footer": t("footer", lang),
        # подгруппа транспорта класса 3 (06.10.2026): car | truck | bus | trailer | special_* | agro | moto; иначе None
        "veh_group": scenario_facts(D)["veh_group"],
        # регион и территория страхования (02.10.2026): особые регионы uz_all (вся республика) и other (текстом)
        "region": {"code": must.get("region_code"), "label": region_label(must, lang),
                   "scope": must.get("region_scope"), "text": must.get("region_text"),
                   "note": t("reg_note_" + must["region_scope"], lang) if must.get("region_scope") else None},
        "territory": _territory(D, lang),
        # несколько объектов в одном акте (парк ТС, 02.10.2026); однообъектный акт — пустой список и None
        "objects": OV["json"] if OV else [],
        "objects_total": OV["total"] if OV else None,
        # акт хранится одним снимком и выдаётся на любом из трёх языков без пересчёта (GET /act/{id}?lang=)
        "langs_available": list(tx.LANGS),
        "downloads": {"docx": f"/act/{meta['id']}.docx?lang={lang}", "pdf": f"/act/{meta['id']}.pdf?lang={lang}",
                      "scoring_pdf": f"/act/{meta['id']}/scoring.pdf?lang={lang}",
                      "scoring_png": f"/act/{meta['id']}/scoring.png?lang={lang}"},
        # отчёт кредитного бюро по заёмщику (01.10.2026); нет отчёта — available = false
        "borrower": v4.bv["json"],
        # вилка ставки (01.10.2026): отметки, поправки региона и рынка с источниками, вывод одной фразой
        "rate_fork": dict(v4.FV["json"], overview=v4.FV.get("overview")),
        # факторы объекта по подгруппам класса (02.10.2026): множитель, применённые и незаполненные группы, вклад
        # каждого фактора в сумах, строки пояснения на языке акта; экспертно, calibrated = 0
        "factor_adjustment": v4.FAV["json"],
    }
    # страховой скоринг объекта (01.10.2026): представление посчитанного акта; сбой показа не роняет акт
    try:
        out["scoring"] = asc.view(D, out, lang, meta, borrower=v4.bv["scoring"])
    except Exception as e:
        print("акт: скоринг не показан:", type(e).__name__, e)
        out["scoring"] = {"available": False, "reason": "render_error", "calibrated": ae.CALIBRATED}
    return out


def damages_text(damages: list, lang: str) -> str:
    """«царапины (левое крыло), косметические; вмятина (задний бампер), существенные»."""
    out = []
    for d in damages or []:
        sev = t("dmg_sev_" + ae.damage_severity(d), lang)
        out.append(str(d.get("what") or "") + (f" ({d['where']})" if d.get("where") else "") + f", {sev}")
    return "; ".join(out)


def _damages_json(damages: list, lang: str) -> dict:
    """Повреждения с фото для ответа: число, тяжесть, вес в уровне риска, штраф балла скоринга (экспертно)."""
    s = ae.damages_summary(damages)
    return {"count": s["count"], "cosmetic": s["cosmetic"], "major": s["major"], "severity": s["severity"],
            "severity_label": t("dmg_sev_" + s["severity"], lang) if s["severity"] else None,
            "level_weight": s["weight"], "score_penalty": s["penalty"],
            "excluded_as_preexisting": bool(s["count"]),
            "text": t("cp_s2_damages", lang, list=damages_text(damages, lang)) if s["count"] else None,
            "calibrated": ae.CALIBRATED}


def _rate_json(D: dict, lang: str, PV: Optional[dict], mode: str, pf: dict) -> dict:
    """Блок rate ответа: ставки расчёта, источник, тип ставки, вилка и факторы в режиме apply."""
    rate_res = D["rate"]
    must = D["must"]
    return {"mode": mode, "base_pct": rate_res["base_pct"], "adj_pct": rate_res["adj_pct"],
            "calc_pct": rate_res["calc_pct"], "applied_pct": rate_res["applied_pct"],
            "min_pct": rate_res["min_pct"], "min_applied": rate_res["min_applied"],
            "base_source": rate_res["base_source"], "object_type": rate_res["object_type"],
            "product_code": rate_res["product_code"], "class_code": rate_res["class_code"],
            "calibrated": ae.CALIBRATED, "how": [_text(h, lang) for h in rate_res["how"]],
            "tariff_version_id": D.get("tariff_version_id"), "final_pct": pf["rate_pct"],
            "multi_class": bool(D.get("multi_class")),
            "product_classes": (must.get("product_classes") or []),
            # вилка ставки, режим apply: ставка акта уже с поправками региона и рынка (01.10.2026)
            "fork_applied": bool(rate_res.get("fork_applied")), "fork_act_pct": rate_res.get("fork_act_pct"),
            # факторы объекта, режим apply (02.10.2026): ставка акта уже с множителем факторов
            "factors_applied": bool(rate_res.get("factors_applied")),
            "factor_act_pct": rate_res.get("factor_act_pct"),
            # тип ставки продукта (01.10.2026): annual | fixed; у fixed — годовой эквивалент для рынка
            "rate_type": rate_res.get("rate_type") or "annual",
            "rate_type_label": t("rate_type_" + (rate_res.get("rate_type") or "annual"), lang),
            "annual_equiv_pct": rate_res.get("annual_equiv_pct"),
            "min_source": _min_src_text(D, lang),
            **(PV["rate_extra"] if PV else {})}


def _render_s1(D: dict, lang: str, PV: Optional[dict], OV: Optional[dict]) -> dict:
    """Раздел 1 «Объект»: класс, продукт, договор, строки объекта, регион, части и парк."""
    NA = t("na", lang)
    must = D["must"]
    opt = D["optional"]
    rows1 = [_row(t("class", lang), _class_label(must["class_code"], must.get("class_name"), lang)),
             _row(t("product", lang), (f"{must['product_code']} — {must['product_name']}" if lang == "ru"
                                       else must["product_code"]) if must.get("product_code") else NA)]
    ctd = D.get("contract") or {}
    if ctd.get("contract_no") or ctd.get("contract_date"):
        # номер и дата договора страхования — из загруженного договора или ввода сотрудника
        no, dt = ctd.get("contract_no"), _ddmmyyyy(ctd["contract_date"]) if ctd.get("contract_date") else None
        val = t("ct_row_value", lang, no=no, date=dt) if no and dt else \
            t("ct_row_no", lang, no=no) if no else t("ct_row_date", lang, date=dt)
        # источник номера — по полю (номер исправлен сотрудником — «введено сотрудником»)
        fs = (D.get("contract_check") or {}).get("field_sources") or {}
        src = fs.get("contract_no") or fs.get("contract_date") or ctd.get("source") or "input"
        rows1.append(_row(t("ct_row", lang), val, tx.label(tx.CT_SOURCE_LABELS, src, lang)))
    rows1 += _object_rows(D, lang)
    scope = must.get("region_scope")
    rows1.append(_row(t("region", lang), region_label(must, lang),
                      t("reg_note_" + scope, lang) if scope else None))
    terr = _territory(D, lang)
    if terr:
        # класс 7 (грузы): территория страхования отдельной строкой — как ввёл сотрудник или регион из списка
        rows1.append(_row(t("territory", lang), terr["text"], terr["note"]))
    if opt.get("guard") is not None:
        rows1.append(_row(t("guard", lang), t("yes" if opt["guard"] else "no", lang)))
    p1 = []
    if PV:
        rows1 += PV["s1_rows"]
        p1.append(PV["s1_paragraph"])
    s1 = {"n": 1, "title": t("s1", lang), "paragraphs": p1, "rows": rows1}
    if OV:
        # парк ТС: перечень объектов таблицей (№, объект, год, сумма, стоимость, уровень, ставка, премия)
        p1.append(OV["s1_paragraph"])
        s1["lists"] = [OV["s1_list"]]
    return s1


def _render_s2(D: dict, lang: str) -> dict:
    """Раздел 2 «Осмотр»: фото, ракурсы, повреждения, документы."""
    NA = t("na", lang)
    ins = D["inspection"]
    p2, rows2 = [], []
    if not ins["photos"]:
        p2.append(t("p_no_photos", lang))
        rows2.append(_row(t("views_seen", lang), t("no_inspection", lang)))
        rows2.append(_row(t("damages", lang), t("no_inspection", lang)))
    elif not ins["ai"]:
        p2.append(t("p_ai_failed", lang, n=ins["photos"], reason=ins.get("ai_reason") or NA))
        rows2.append(_row(t("files_count", lang), str(ins["photos"])))
        rows2.append(_row(t("views_seen", lang), t("no_inspection", lang)))
        rows2.append(_row(t("damages", lang), t("no_inspection", lang)))
    else:
        p2.append(t("p_inspected", lang, n=ins["photos"]))
        vg = scenario_facts(D)["veh_group"]
        seen = [tx.view_label(v, lang, vg) for v in ins["views_seen"] if v != "other"]
        rows2.append(_row(t("views_seen", lang), ", ".join(seen) or NA))
        if ins["required_views"]:
            rows2.append(_row(t("views_missing", lang),
                              ", ".join(tx.view_label(v, lang, vg) for v in ins["missing_views"])
                              or t("views_all", lang)))
        else:
            rows2.append(_row(t("views_missing", lang), t("views_none_needed", lang)))
        if ins["damages"]:
            # повреждения с фото (06.10.2026): тяжесть каждого и пометка — исключаются как предсуществующие
            rows2.append(_row(t("damages", lang), damages_text(ins["damages"], lang), t("dmg_preexisting", lang)))
        else:
            rows2.append(_row(t("damages", lang), t("damages_none", lang)))
    if ins.get("parsed_docs"):
        rows2.append(_row(t("docs_parsed", lang), str(ins["parsed_docs"])))
    kinds2 = []
    for k in ins.get("document_kinds") or []:
        lk = _doc_kind_label(k, lang)
        if lk not in kinds2:
            kinds2.append(lk)
    rows2.append(_row(t("documents", lang), (t("docs_given", lang) + (
        " (" + ", ".join(kinds2) + ")" if kinds2 else ""))
        if ins["documents"] else t("docs_not_given", lang)))
    s2 = {"n": 2, "title": t("s2", lang), "paragraphs": p2, "rows": rows2}
    return s2


def _render_s3(D: dict, lang: str, PV: Optional[dict], OV: Optional[dict]) -> SimpleNamespace:
    """Раздел 3 «Стоимость»: сумма к стоимости, износ, оценка по объявлениям, справка биржи."""
    value = D["value"]
    must = D["must"]
    opt = D["optional"]
    ratio = pct(value["ratio_pct"], lang, 2)
    legal = tx.label(tx.LEGAL_REFS, value["legal_ref"], lang) if value.get("legal_ref") else None
    if value["verdict"] == "over":
        vtext = t("v_over", lang, diff=money(value["diff"], lang), ref=legal)
    elif value["verdict"] == "under":
        vtext = t("v_under", lang, ratio=ratio, ref=legal)
    elif value.get("legal_ref"):
        vtext = t("v_normal", lang, ratio=ratio) + " " + t("v_under_small", lang, ratio=ratio, ref=legal)
    else:
        vtext = t("v_normal", lang, ratio=ratio)
    M = D.get("market")
    # один итоговый вывод: при «уточнить стоимость» — сумма к заявленной и к уточнённой стоимости и итог
    final = am.final_verdict(value, M)
    vtext = am.final_text(value, M, lang, vtext)
    decl = am.declared_text(opt.get("declared_value_original"), must["object_value"], M, lang)
    rows3 = [_row(t("sum_insured", lang), money(must["sum_insured"], lang)),
             _row(t("object_value", lang), money(must["object_value"], lang))]
    if decl:
        rows3.append(_row(decl[0], money(opt["declared_value_original"], lang), decl[1]))
    cvp = ((D.get("parts") or {}).get("totals") or {}).get("value") if PV else None
    rows3 += [_row(t("ratio", lang), ratio,
                   t("pt_ratio_note", lang, sum=money(cvp["sum_insured"], lang), value=money(cvp["object_value"], lang))
                   if cvp else None),
              _row(t("verdict", lang), vtext)]
    p3 = []
    dep = value.get("depreciated")
    if dep:
        rows3.append(_row(t("depreciated", lang), money(dep["value"], lang),
                          t("depr_how", lang, price=money(dep["price_new"], lang),
                            rate=pct(dep["wear_pct_per_year"], lang), years=dep["years"])))
        p3.append(t("depr_note", lang))
    if M and M.get("query") is not None:
        M = dict(M, links=am.search_links(M["query"], lang))
    mv = am.market_view(M, lang)
    rows3 += mv["rows"]
    if PV:
        rows3 += PV["s3_rows"]
        p3 += PV["s3_paragraphs"]
    if OV:
        p3.append(OV["s3_paragraph"])
    EX = _exchange_view(D, lang)
    src3 = list(mv["source_lines"])
    if EX["row"]:
        rows3.append(EX["row"])
        src3.append(EX["source_line"])
    s3 = {"n": 3, "title": t("s3", lang), "paragraphs": p3, "rows": rows3, "source_lines": src3,
          "lists": ([OV["s3_list"]] if OV else []) + mv["lists"]}
    return SimpleNamespace(EX=EX, M=M, decl=decl, final=final, legal=legal, mv=mv, s3=s3, vtext=vtext)


def _render_s4(D: dict, lang: str, PV: Optional[dict], OV: Optional[dict]) -> SimpleNamespace:
    """Раздел 4 «Риск и тариф»: уровень, ставка, вилка, факторы, франшиза, сценарии, аналитика, сверки."""
    risk = D["risk"]
    rate_res = D["rate"]
    fr = D["franchise"]
    must = D["must"]
    level_label = tx.label(tx.LEVEL_LABELS, risk["level"], lang)
    factors = [_text(f, lang) for f in risk["factors"]]
    rule = risk["rule"]
    minus = lambda x: (str(x) if lang == "en" else str(x).replace(".", ",")).replace("-", "−")
    rule_text = t("level_rule", lang, net=minus(risk["net"]), low=minus(rule["low_max_net"]),
                  high=minus(rule["high_min_net"]), k=rule["min_known"])
    mode = rate_res["mode"]
    pf = _premium_final(D)
    rows4 = _s4_rate_rows(D, lang, PV, OV, level_label, pf)
    # вилка ставки (01.10.2026): минимум → ставка акта → с учётом региона и рынка → рынок; у частей — по каждой части
    FV = _fork_parts_view(D, PV, lang) if PV and D.get("rate_fork") else \
        _fork_view(D.get("rate_fork"), lang, must["class_code"])
    if not PV and FV.get("row") and mode == "tariff":
        rows4.append(FV["row"])
    # факторы объекта по подгруппам класса (02.10.2026): строка (у частей — по строке на часть) и перечень ниже
    FAV = _factor_view(D, lang)
    rows4 += FAV["rows"]
    if OV:
        lists_obj4 = [OV["s4_list"]]
    else:
        lists_obj4 = []
    fr_text = PV["fr_text"] if PV else _fr_text(fr, lang)
    if not PV:
        rows4.append(_row(t("franchise", lang), fr_text))
    scv = _scenarios_view(D.get("scenarios"), must, lang, scenario_facts(D))
    rows4 += scv["rows"]
    if PV:
        # комплексный продукт: уровень договора — по самой опасной части; дальше таблица частей и разбор каждой
        lists4 = [{"title": t("pt_factors_title", lang, n=PV["worst"]), "items": factors + [rule_text]},
                  {"title": t("how_title", lang), "items": [_text(h, lang) for h in rate_res["how"]]}]
        anv = {"summary": PV["summary"], "lists": PV["s4_lists"], "json": PV["analytics_json"]}
    else:
        # парк ТС: уровень договора — по самому опасному объекту, его признаки
        f_title = t("obj_factors_title", lang, n=risk["object_index"]) if OV and risk.get("object_index") \
            else t("factors", lang)
        lists4 = [{"title": f_title, "items": factors + [rule_text]},
                  {"title": t("how_title", lang),
                   "items": [_text(h, lang) for h in rate_res["how"]]}]
        # аналитика риска (30.09.2026): резюме первым абзацем, таблицы — после «Как посчитан тариф»
        try:
            anv = _analytics_view(D, lang)
        except Exception as e:               # сбой показа аналитики не роняет акт: остальные разделы на месте
            print("акт: аналитика раздела 4 не показана:", type(e).__name__, e)
            anv = {"summary": None, "lists": [], "json": {"available": False, "reason": "render_error",
                                                          "calibrated": ae.CALIBRATED}}
    lists4 += lists_obj4                     # парк ТС: ставка и премия по каждому объекту
    if FV.get("list"):
        lists4.append(FV["list"])            # «Вилка ставки» — сразу после «Как посчитан тариф»
    if FAV["lines"]:
        # «Факторы объекта по подгруппам класса» — после вилки: построчно, вклад каждого фактора в сумах
        lists4.append({"title": t("fa_title", lang), "items": FAV["lines"]})
    lists4 += anv["lists"]
    if fr.get("grounds"):
        lists4.append({"title": t("fr_grounds", lang), "items": [_text(g, lang) for g in fr["grounds"]]})
    fr_how = [_fr_how_text(h, lang) for h in fr.get("how") or []]
    if fr_how:
        lists4.append({"title": t("fr_how_title", lang), "items": fr_how})
    fr_alts = [_alt_view(a, lang) for a in fr.get("alternatives") or []]
    if fr_alts:
        lists4.append({"title": t("fr_alt_title", lang), "items": [a["text"] for a in fr_alts]})
    lists4 += scv["lists"]
    p4 = [anv["summary"]] if anv.get("summary") else []
    if OV:
        p4.append(t("obj_analytics_note", lang))
    if D.get("multi_class"):
        p4.append(t("multi_class_note", lang, classes=", ".join(must.get("product_classes") or [])))
    if not fr.get("needed") and fr.get("code") == "fr_not_needed" and fr.get("status") in (None, "none"):
        p4.append(fr_text + ": " + t("fr_none_grounds", lang) + ". " + t("fr_alt", lang))
    if D["clauses"]:
        lists4.append({"title": t("clauses", lang),
                       "items": [f"{c.get(lang) or c.get('ru')} ({t('expert', lang)})" for c in D["clauses"]]})
    p4.append(scv["paragraph"])
    rcv = _request_check_view(D.get("request_check"), lang)
    if rcv["json"]["available"]:
        # подраздел «Сверка с запросом филиала»: итог, строки сверки, как считали
        lists4.append({"title": t("rq_title", lang), "items": rcv["lines"]})
        lists4.append({"title": t("rq_how_title", lang), "items": rcv["json"]["how"]})
    ccv = _request_check_view(D.get("contract_check"), lang, "ct", D.get("contract"))
    if ccv["json"]["available"]:
        # подраздел «Сверка с договором»: итог, строки сверки, риски и исключения, как сверено
        lists4.append({"title": t("ct_title", lang), "items": ccv["lines"]})
        lists4.append({"title": t("ct_how_title", lang), "items": ccv["json"]["how"]})
    xv = cross_view(D.get("cross_check"), lang)
    if xv:
        lists4.append({"title": t("x_title", lang), "items": xv["lines"]})
    bv = _borrower_view(D.get("borrower"), lang)
    if bv["available"]:
        # «Заёмщик: данные кредитного бюро» (01.10.2026): в уровень риска и ставку не входит
        lists4.append({"title": t("cb_title", lang), "items": bv["lines"]})
    bmv = _below_min_view(D, lang)
    if bmv["json"]["available"]:
        # «Ставка ниже минимальной: можно ли застраховать» (01.10.2026): ответ, доводы за и против, условия
        lists4.append({"title": t("bm_title", lang), "items": bmv["lines"]})
    s4 = {"n": 4, "title": t("s4", lang), "paragraphs": p4, "rows": rows4, "lists": lists4}
    return SimpleNamespace(FAV=FAV, FV=FV, anv=anv, bmv=bmv, bv=bv, ccv=ccv, fr_alts=fr_alts, fr_how=fr_how,
                           fr_text=fr_text, level_label=level_label, mode=mode, pf=pf, rcv=rcv, rule_text=rule_text,
                           s4=s4, scv=scv, xv=xv)


def _s4_rate_rows(D: dict, lang: str, PV: Optional[dict], OV: Optional[dict], level_label: str, pf: dict) -> list:
    """Строки раздела 4 об уровне, ставке и премии — по режиму ставки (тариф, норматив, части, не определена)."""
    NA = t("na", lang)
    rate_res = D["rate"]
    fr = D["franchise"]
    mode = rate_res["mode"]
    rows4 = [_row(t("level", lang), level_label, t("uncalibrated", lang))]
    if OV and mode in ("tariff", "statutory"):
        # парк ТС: ставка — по каждому объекту (таблица ниже), у договора — средняя справочно и сумма премий
        rows4 = [_row(t("level", lang), level_label, t("obj_level_note", lang, n=D["objects_total"]["worst_index"]))]
        rows4 += OV["s4_rows"]
    elif mode == "tariff":
        rows4 += [_row(t("applied_rate", lang), pct(rate_res["applied_pct"], lang),
                       t("min_applied", lang) if rate_res["min_applied"] else None)]
        if rate_res.get("rate_type") == "fixed":
            # фиксированная ставка (на весь срок): тип и годовой эквивалент для сравнения с рынком — обе ставки
            rows4 += [_row(t("rate_type_row", lang), t("rate_type_fixed", lang)),
                      _row(t("rate_annual_equiv_row", lang), pct(rate_res.get("annual_equiv_pct"), lang),
                           f"{pct(rate_res['applied_pct'], lang)} × 365 / {rate_res['term_days']}")]
        if pf["franchise_applied"]:
            rows4.append(_row(t("rate_with_fr", lang), pct(pf["rate_pct"], lang),
                              t("min_applied", lang) if fr.get("floor_applied") else None))
        prem_note = t("premium_term", lang, days=rate_res["term_days"])
        if pf["franchise_applied"]:
            prem_note += "; " + t("premium_no_fr", lang, before=money(rate_res["premium"], lang))
        rows4 += [_row(t("base_rate", lang), pct(rate_res["base_pct"], lang)),
                  _row(t("adj", lang), "+" + pct(rate_res["adj_pct"], lang), t("uncalibrated", lang)),
                  _row(t("min_rate", lang), _pct_na(rate_res["min_pct"], lang), _min_src_text(D, lang)),
                  _row(t("premium", lang), money(pf["amount"], lang), prem_note)]
    elif mode == "statutory":
        rows4 += [_row(t("applied_rate", lang), pct(rate_res["applied_pct"], lang), t("rate_by_act", lang)),
                  _row(t("adj", lang), t("adj_none_statutory", lang)),
                  _row(t("premium", lang), money(rate_res["premium"], lang),
                       t("premium_term", lang, days=rate_res["term_days"]))]
    elif mode == "multi":
        rows4 = PV["s4_rows"]
    else:
        rows4 += [_row(t("applied_rate", lang), t("rate_undefined", lang)),
                  _row(t("premium", lang), NA)]
    return rows4


def _render_s5(D: dict, lang: str, PV: Optional[dict], OV: Optional[dict], FAV: dict, bmv: dict) -> SimpleNamespace:
    """Раздел 5 «Решение»: рекомендация, расхождения, проверки, чего не хватает, мероприятия."""
    ins = D["inspection"]
    dec = D["decision"]
    p5 = [t(dec["code"], lang)]
    if bmv["json"]["available"]:
        # рекомендация акта — по ставке акта; по запрошенной ставке ниже минимума — ответ оценки
        p5.append(bmv["s5"])
    lists5 = []
    if D["discrepancies"]:
        lists5.append({"title": t("disc_title", lang), "items": [_disc_text(d, lang, D["group"]) for d in D["discrepancies"]]})
        p5.append(t("disc_priority", lang))
    else:
        p5.append(t("disc_none", lang))
    checks = [_check_text(c, lang, D["group"], scenario_facts(D)["veh_group"]) for c in dec["checks"]]
    if checks:
        lists5.append({"title": t("checks_title", lang), "items": checks})
    missing_labels = [_s1_label(k, lang, D["group"]) for k in D["missing"]]
    if ins.get("session_missing"):
        missing_labels.append(t("session_not_found", lang))
    if missing_labels:
        lists5.append({"title": t("missing_title", lang), "items": missing_labels})
    p5 += FAV["s5"]
    if FAV["unfilled"]:
        lists5.append({"title": t("fa_unfilled_title", lang), "items": FAV["unfilled"]})
    if OV and OV["notes"]:
        p5 += OV["notes"]
    if PV:
        # распределение суммы по классам: подтверждено сотрудником или принято по умолчанию; поля частей
        p5.append(PV["s5_paragraph"])
        if PV["missing_lines"]:
            lists5.append({"title": t("pt_missing_title", lang), "items": PV["missing_lines"]})
    msv = _measures_view(D.get("measures"), lang)
    lists5.append({"title": t("ms_title", lang), "items": [m["line"] for m in msv["items"]] or [t("ms_none", lang)]})
    if msv["summary"].get("text"):
        p5.append(msv["summary"]["text"])
    s5 = {"n": 5, "title": t("s5", lang), "paragraphs": p5, "rows": [], "lists": lists5}
    return SimpleNamespace(checks=checks, dec=dec, missing_labels=missing_labels, msv=msv, s5=s5)


def _borrower_view(b: Optional[dict], lang: str) -> dict:
    """Заёмщик по отчёту кредитного бюро: строки раздела 4, блок borrower ответа и строки для страницы скоринга."""
    if not b or not b.get("available"):
        return {"available": False, "lines": [], "json": {"available": False}, "scoring": None}
    f = b["fields"]
    ov, ac = f.get("overview") or {}, f.get("active") or {}
    NA = t("na", lang)
    num = lambda v: NA if v is None else str(int(v)) if float(v) == int(v) else str(v)   # noqa: E731
    mon = lambda v: NA if v is None else money(v, lang)                                     # noqa: E731
    src = tx.label(tx.CR_SOURCE_LABELS, "input" if b["source_kind"] == "input" else b["source"], lang)
    lines = [t("cb_date", lang, date=_ddmmyyyy(f["report_date"]), days=b["age_days"], src=src)
             if f.get("report_date") and b.get("age_days") is not None else t("cb_date_unknown", lang, src=src)]
    if f.get("subject_type") == "legal":
        rest = "".join(x for x in ((f" «{f['name']}»" if f.get("name") else ""),
                                   (", " + t("cb_inn", lang, v=f["inn"]) if f.get("inn") else ""),
                                   (", " + t("cb_oked", lang, v=f["oked"]) if f.get("oked") else "")))
        lines.append(t("cb_subject_legal", lang, rest=rest))
    elif f.get("subject_type") == "individual":
        lines.append(t("cb_subject_individual", lang))
    else:
        lines.append(t("cb_subject_unknown", lang))
    lines.append(t("cb_score", lang, score=num(f.get("score")), cls=f.get("score_class") or NA,
                   ver=f.get("score_version") or NA))
    lines.append(t("cb_overview", lang, a=num(ov.get("applications")), c=num(ov.get("contracts")),
                   u=num(ov.get("contingent")), q=num(ov.get("inquiries")), p=mon(ov.get("avg_monthly_payment"))))
    lines.append(t("cb_overdue", lang, n=num(ov.get("overdue_principal_count")),
                   days=num(ov.get("max_overdue_principal_days")), amount=mon(ov.get("max_overdue_principal_amount")),
                   idays=num(ov.get("max_overdue_interest_days")), itotal=mon(ov.get("overdue_interest_total"))))
    lines.append(t("cb_active", lang, n=num(ac.get("count")), debt=mon(ac.get("total_debt")), od=mon(ac.get("overdue")),
                   pay=mon(ac.get("monthly_payment"))))
    if ac.get("creditors"):
        lines.append(t("cb_creditors", lang, v="; ".join(ac["creditors"])))
    if b.get("edits"):
        lines.append(t("cb_edits", lang, what="; ".join(
            f"{tx.label(tx.CR_FIELD_LABELS, e['code'], lang)}: {_cr_value(e['code'], e['was'], lang)} → "
            f"{_cr_value(e['code'], e['now'], lang)}" for e in b["edits"])))
    if b.get("doc_missing"):
        lines.append(t("cb_doc_missing", lang))
    checks = [_check_text({"code": "c_" + c["code"], "params": c["params"]}, lang) for c in b.get("checks") or []]
    if not b.get("credit_product"):
        lines.append(t("cb_not_credit", lang))
    lines += checks
    lines += [t("cb_not_in_rate", lang), t("cb_keep", lang), t("cb_no_direct", lang)]
    rows = [{"code": "score", "label": t("cb_s_score", lang),
             "value": f"{num(f.get('score'))} / {f.get('score_class') or NA}"},
            {"code": "overdue", "label": t("cb_s_overdue", lang), "value": mon(ac.get("overdue"))},
            {"code": "max_overdue_days", "label": t("cb_s_max_overdue", lang),
             "value": num(ov.get("max_overdue_principal_days"))},
            {"code": "total_debt", "label": t("cb_s_debt", lang), "value": mon(ac.get("total_debt"))},
            {"code": "monthly_payment", "label": t("cb_s_payment", lang),
             "value": mon(ac.get("monthly_payment") if ac.get("monthly_payment") is not None
                          else ov.get("avg_monthly_payment"))},
            {"code": "report_date", "label": t("cb_s_date", lang), "value": _ddmmyyyy(f["report_date"])
             if f.get("report_date") else NA}]
    js = {"available": True, "fields": f, "source": b["source"], "source_kind": b["source_kind"],
          "source_label": src, "field_sources": b.get("field_sources") or {}, "edits": b.get("edits") or [],
          "doc_missing": bool(b.get("doc_missing")), "age_days": b.get("age_days"),
          "credit_product": bool(b.get("credit_product")), "checks": checks,
          "check_codes": [c["code"] for c in b.get("checks") or []], "lines": lines, "rows": rows,
          "in_risk_level": False, "in_rate": False,
          "note": t("cb_not_in_rate", lang) + " " + t("cb_keep", lang), "direct_note": t("cb_no_direct", lang),
          "keep_note": t("cb_keep", lang), "calibrated": ae.CALIBRATED}
    sc = {"available": True, "score": f.get("score"), "score_class": f.get("score_class"),
          "overdue": ac.get("overdue"), "max_overdue_principal_days": ov.get("max_overdue_principal_days"),
          "total_debt": ac.get("total_debt"), "rows": rows, "note": js["note"]} if b.get("credit_product") else None
    return {"available": True, "lines": lines, "json": js, "scoring": sc}


def _request_check_view(rc: Optional[dict], lang: str, prefix: str = "rq", contract: Optional[dict] = None) -> dict:
    """Сверка с запросом филиала (prefix rq) или с договором (prefix ct, общая функция): строки для раздела 4
    и JSON request_check / contract_check. Старые акты — available = false."""
    rc = rc or {}
    sources = tx.CT_SOURCE_LABELS if prefix == "ct" else tx.RQ_SOURCE_LABELS
    js = {"available": bool(rc.get("available")), "source": rc.get("source"),
          "source_label": tx.label(sources, rc["source"], lang) if rc.get("source") else None,
          "items": [], "summary": None, "how": [], "tolerance": rc.get("tolerance"),
          "term_inclusive": rc.get("term_inclusive"), "calibrated": ae.CALIBRATED}
    trusted = "source_kind" in rc                  # акты до 30.09.2026 (вечер) — без источника по полям и правок
    if trusted:
        js.update(_trust_view(rc, prefix, lang, sources))
    if prefix == "ct":
        ess = rc.get("essentials") or []
        js["essentials"] = [{"code": e["code"], "label": tx.label(tx.CT_ESSENTIAL_LABELS, e["code"], lang),
                             "present": e["present"]} for e in ess]
        js["legal_ref"] = tx.label(tx.LEGAL_REFS, rc["legal_ref"], lang) if rc.get("legal_ref") else None
        c = contract or {}
        js["contract_no"], js["contract_date"] = c.get("contract_no"), c.get("contract_date")
        js["covered_risks"] = _risk_view(c.get("covered_risks"), tx.RISK_LABELS, lang)
        js["exclusions"] = _risk_view(c.get("exclusions"), tx.EXCLUSION_LABELS, lang)
        js["payments"] = list(c.get("payments") or [])
        js["payment_mode"] = c.get("payment_mode")
    if not js["available"]:
        return {"lines": [], "json": js}
    lines = []
    for it in rc.get("items") or []:
        params = it.get("params") or {}
        if it["code"] == "essentials":
            codes = params.get("missing") if it["verdict"] == "no_essential" else params.get("present")
            text = t(it["text_code"], lang, what=", ".join(tx.label(tx.CT_ESSENTIAL_LABELS, x, lang)
                                                           for x in codes or []))
        else:
            text = t(it["text_code"], lang, **_rq_params(it["code"], params, lang))
        label = t(prefix + "_l_" + it["code"], lang)
        js["items"].append({"code": it["code"], "label": label, "requested": it["requested"],
                            "calculated": it["calculated"], "diff": it["diff"], "diff_pct": it["diff_pct"],
                            "verdict": it["verdict"], "verdict_label": t("rq_v_" + it["verdict"], lang),
                            "reference": bool(it["reference"]), "text": text})
        lines.append(f"{label} — {t('rq_v_' + it['verdict'], lang)}: {text}")
    sm = rc.get("summary") or {}
    js["summary"] = {"verdict": sm.get("verdict"), "text": t(sm.get("code") or f"{prefix}_summary_missing", lang),
                     "differs": sm.get("differs", 0), "below_min": sm.get("below_min", 0),
                     "missing": sm.get("missing", 0)}
    if prefix == "ct":
        js["summary"]["no_essential"] = sm.get("no_essential", 0)
        fs = rc.get("field_sources") or {}
        for key, line in (("covered_risks", "ct_risks_line"), ("exclusions", "ct_excl_line")):
            if not js[key]:
                continue
            # риски со скана или дочитанные моделью — с пометкой: модель могла ошибиться
            by_model = fs.get(key) in ("photo", "document_ai")
            js[key + "_by_model"] = by_model
            lines.append(t(line, lang, what=", ".join(x["label"] for x in js[key]))
                         + (" (" + t("by_model_mark", lang) + ")" if by_model else ""))
    head = [js["summary"]["text"]]
    if trusted:
        head.append(t("tr_src_line", lang, v=js["source_label"]))
        lines += [js["edits"]["line"]] + [e["text"] for e in js["edits"]["items"]]
    js["how"] = [t(h["code"], lang, **_rq_params(h["code"], h.get("params") or {}, lang, how=True))
                 for h in rc.get("how") or []]
    return {"lines": head + lines, "json": js}


def _trust_view(rc: dict, prefix: str, lang: str, sources: dict) -> dict:
    """Источник условий (из документа / с правками сотрудника / введено сотрудником) и правки «было → стало»."""
    kind, edits = rc.get("source_kind"), rc.get("edits") or []
    detail = tx.label(sources, rc.get("origin"), lang) if rc.get("origin") else None
    if kind == "input":
        label = t("tr_src_input", lang) + (" — " + t("tr_doc_missing", lang) if rc.get("doc_missing") else "")
    elif kind == "document_edited":
        label = t("tr_src_edited", lang, n=len(edits)) + (" — " + detail if detail else "")
    else:
        label = t("tr_src_doc", lang) + (" — " + detail if detail else "")
    items = []
    for e in edits:
        lab = tx.label(tx.EDIT_LABELS, e["code"], lang)
        was, now = _edit_value(e["code"], e.get("was"), lang), _edit_value(e["code"], e.get("now"), lang)
        items.append({"code": e["code"], "label": lab, "was": e.get("was"), "now": e.get("now"),
                      "text": t("tr_edit", lang, label=lab, was=was, now=now)})
    line = t(prefix + "_edits_line", lang, what=str(len(items)) if items else t("tr_no_edits", lang))
    return {"source_kind": kind, "source_label": label, "document_missing": bool(rc.get("doc_missing")),
            "field_sources": dict(rc.get("field_sources") or {}),
            "edits": {"count": len(items), "items": items, "line": line}}


def _edit_value(code: str, v, lang: str) -> str:
    """Значение условия в строке правки: суммы с копейками, тариф процентом, даты ДД.ММ.ГГГГ."""
    NA = t("tr_none", lang)
    if v in (None, "", [], {}):
        return NA
    if code in ("sum_insured", "object_value", "premium"):
        return money_k(v, lang)
    if code == "tariff_pct":
        return pct(v, lang)
    if code in ("term_from", "term_to", "contract_date"):
        return _ddmmyyyy(v)
    if code == "term_days":
        return t("x_days", lang, n=int(v))
    if code == "franchise":
        return _x_value("franchise", v, lang)
    if code in ("covered_risks", "exclusions"):
        group = tx.RISK_LABELS if code == "covered_risks" else tx.EXCLUSION_LABELS
        return ", ".join(x["label"] for x in _risk_view(v, group, lang)) or NA
    if code == "payment_mode":
        return tx.label(tx.PAYMENT_MODE_LABELS, v, lang)
    if code == "payments":
        return t("tr_payments", lang, n=len(v), total=money_k(sum(float(p["amount"]) for p in v), lang))
    if code == "items":
        return t("tr_items", lang, n=len(v), total=money_k(sum(float(x["sum"]) for x in v), lang))
    s = str(v)
    return s if len(s) <= 80 else s[:79] + "…"


def _exchange_view(D: dict, lang: str) -> dict:
    """Справка биржи УзРТСБ для раздела 3: строка, строка источника (ссылка обязательна) и блок для ответа.
    Акты до 02.10.2026 справки не имеют — available = false."""
    ex = D.get("exchange") or (D.get("analytics") or {}).get("exchange") or {"available": False, "items": []}
    items = [i for i in ex.get("items") or [] if i.get("url")] if ex.get("available") else []
    # пометка и название источника — на языке акта (в снимке акта они по-русски)
    loc = {"note": t("ex_note", lang), "source_name": t("ex_source_name", lang)}
    if not items:
        return {"row": None, "source_line": None, "json": dict(ex, available=False, text=None, **loc)}
    parts = []
    for i in items:
        unit = (us.UNIT_LABELS.get(i["unit"]) or {}).get(tx.lang_of(lang)) or i["unit"]
        parts.append(t("ex_item", lang, group=us.group_label(i["group"], tx.lang_of(lang)),
                       price=money(i["median_unit_price"], lang), unit=unit, n=i["deals"],
                       date=_ddmmyyyy(i["last_date"])))
    text = t("ex_text", lang, items="; ".join(parts))
    url = ex.get("url") or items[0]["url"]
    line = t("ex_src", lang, url=url, days=ex.get("days") or 30)
    return {"row": _row(t("ex_label", lang), text, t("ex_note", lang)), "source_line": line,
            "json": dict(ex, text=text, source_line=line, **loc)}


def _premium_final(D: dict) -> dict:
    """Премия акта: с учётом применённой франшизы; в старых актах (до 29.09.2026) — премия ставки."""
    pf = D.get("premium_final")
    if pf:
        return pf
    return {"amount": D["rate"]["premium"], "rate_pct": D["rate"]["applied_pct"], "franchise_applied": False}
