"""Части комплексного продукта, парк объектов, минимальная ставка и оценка заниженной ставки на языке акта."""
from typing import Optional

from .. import act_engine as ae
from .. import act_texts as tx
from .. import min_rates as mrs
from ..act_texts import money, pct, t

from .view_fmt import _li, _money_na, _mult, _pct_na
from .view_rows import _class_label, _fr_short, _part_label, _part_rate_text, _parts_sc_how, _row, _text, _value_text
from .view_blocks import _alt_view, _fr_how_text, _fr_text, _franchise_extra, _measures_view, _scenarios_view, scenario_facts
from .view_analytics import _analytics_view


def _parts_view(D: dict, lang: str) -> dict:
    """
    Комплексный продукт (30.09.2026): строки разделов 1, 3, 4, абзацы, списки и JSON блока parts на языке акта.
    Каждая часть — своим шаблоном класса; аналитика, сценарии и франшиза — те же показы, что у одного класса.
    """
    PT = D["parts"]
    must = D["must"]
    NA = t("na", lang)
    items = PT["items"]
    totals = PT["totals"] or {}
    s1_rows, s3_rows, table, lists, pjs, missing_lines = [], [], [], [], [], []
    for p in items:
        lab = _part_label(p, lang)
        n = p["index"]
        kl = tx.OBJECT_KINDS.get(p.get("object_kind") or "")
        obj = p.get("object_description") or ((kl[1].get(lang) or kl[1].get("ru")) if kl else None)
        where = t("pt_same_object" if p["same_object"] else "pt_other_object", lang)
        s1_rows.append(_row(lab, t("pt_s1_value", lang, sum=money(p["sum_insured"], lang),
                                   share=pct(p["share_pct"], lang, 2) if p.get("share_pct") is not None else NA),
                            "; ".join(x for x in (obj, where, t("pt_outside", lang) if p["class_outside"] else None,
                                                  t("pt_guess", lang) if p["class_guess"] else None) if x)))
        v = p["value"]
        if v.get("applicable"):
            vtext = _value_text(v, lang)
            if p.get("object_value_default"):
                vtext += " " + t("pt_value_default", lang)
            s3_rows.append(_row(lab, pct(v["ratio_pct"], lang, 2),
                                t("pt_s3_note", lang, sum=money(p["sum_insured"], lang),
                                  value=money(p["object_value"], lang)) + ". " + vtext))
        else:
            vtext = t("pt_value_na", lang)
            s3_rows.append(_row(lab, t("pt_value_na_short", lang), vtext))
        r = p["rate"]
        lvl = tx.label(tx.LEVEL_LABELS, p["level"], lang)
        prem = p["premium_final"]["amount"]
        table.append([str(n), _class_label(p["class_code"], p.get("class_name"), lang),
                      money(p["sum_insured"], lang), lvl, _part_rate_text(r, lang),
                      _money_na(prem, lang), _fr_short(p["franchise"], lang)])
        # подраздел части: уровень, как посчитан тариф, франшиза, сценарии, аналитика
        Dp = {"must": {"class_code": p["class_code"], "class_name": p.get("class_name"),
                       "product_code": p.get("product_code"), "product_name": p.get("product_name"),
                       "sum_insured": p["sum_insured"], "object_value": p["object_value"],
                       "region": must.get("region"), "region_code": must.get("region_code")},
              "rate": r, "risk": p["risk"], "franchise": p["franchise"], "analytics": p.get("analytics"),
              "measures": p.get("measures"), "template": p.get("template"), "contract": D.get("contract")}
        rrule = p["risk"]["rule"]
        minus = lambda x: (str(x) if lang == "en" else str(x).replace(".", ",")).replace("-", "−")
        lists.append({"title": t("pt_sub_level", lang, part=lab, level=lvl),
                      "items": [_text(f, lang) for f in p["risk"]["factors"]] +
                      [t("level_rule", lang, net=minus(p["risk"]["net"]), low=minus(rrule["low_max_net"]),
                         high=minus(rrule["high_min_net"]), k=rrule["min_known"])]})
        how = [_text(h, lang) for h in r["how"]]
        if r["mode"] == "statutory":
            how.append(t("pt_statutory_note", lang))
        lists.append({"title": t("pt_sub_rate", lang, part=lab), "items": how})
        fr = p["franchise"]
        fr_text = _fr_text(fr, lang)
        fr_items = [fr_text] + [_text(g, lang) for g in fr.get("grounds") or []] + \
            [_fr_how_text(h, lang) for h in fr.get("how") or []]
        fr_alts = [_alt_view(a, lang) for a in fr.get("alternatives") or []]
        lists.append({"title": t("pt_sub_fr", lang, part=lab), "items": fr_items + [a["text"] for a in fr_alts]})
        scv = _scenarios_view(p["scenarios"], Dp["must"], lang, scenario_facts(Dp))
        sc_items = [f"{x['label']}: {x['value']}" + (f" ({x['note']})" if x.get("note") else "") for x in scv["rows"]]
        lists.append({"title": t("pt_sub_sc", lang, part=lab), "items": sc_items + scv["json"]["how"]})
        try:
            anv = _analytics_view(Dp, lang)
        except Exception as e:               # сбой показа аналитики части не роняет акт
            print("акт: аналитика части не показана:", type(e).__name__, e)
            anv = {"summary": None, "lists": [], "json": {"available": False, "reason": "render_error",
                                                          "calibrated": ae.CALIBRATED}}
        if anv.get("summary"):
            lists.append({"title": t("pt_sub_summary", lang, part=lab), "items": [anv["summary"]]})
        for li in anv["lists"]:
            lists.append(dict(li, title=f"{lab}. {li['title']}"))
        if p.get("missing"):
            missing_lines.append(lab + ": " + ", ".join((x["label"].get(lang) or x["label"].get("ru") or x["code"])
                                                        for x in p["missing"]))
        msv = _measures_view(p.get("measures"), lang)
        pjs.append({
            "index": n, "class_code": p["class_code"],
            "class_name": _class_label(p["class_code"], p.get("class_name"), lang), "label": lab,
            "product_code": p.get("product_code"), "product_own": p.get("product_own"),
            "pricing_mode": p.get("pricing_mode"), "template_version": p.get("template_version"),
            "template_class": p.get("template_class"), "class_outside": p["class_outside"],
            "class_guess": p["class_guess"], "sum_insured": p["sum_insured"], "share_pct": p.get("share_pct"),
            "object_value": p["object_value"], "object_value_default": p.get("object_value_default"),
            "object_kind": p.get("object_kind"), "object_kind_default": p.get("object_kind_default"),
            "object_description": p.get("object_description"),
            "same_object": p["same_object"], "level": p["level"], "level_label": lvl,
            "risk": {"level": p["level"], "net": p["risk"]["net"], "up": p["risk"]["up"],
                     "down": p["risk"]["down"], "factors": [{"code": f["code"], "sign": f["sign"],
                                                             "text": _text(f, lang)} for f in p["risk"]["factors"]],
                     "calibrated": ae.CALIBRATED},
            "rate": {"mode": r["mode"], "base_pct": r.get("base_pct"), "base_source": r.get("base_source"),
                     "adj_pct": r.get("adj_pct"), "calc_pct": r.get("calc_pct"), "applied_pct": r.get("applied_pct"),
                     "min_pct": r.get("min_pct"), "min_applied": r.get("min_applied"),
                     "min_source": (p.get("class_min") or {}).get("source"),
                     "final_pct": p["premium_final"]["rate_pct"], "statutory": p["statutory"],
                     "how": how, "calibrated": ae.CALIBRATED},
            "premium": prem, "premium_text": _money_na(prem, lang),
            "premium_before_franchise": p.get("premium_before_franchise"), "term_days": p.get("term_days"),
            "franchise": {"needed": bool(fr.get("needed")), "text": fr_text, "short": _fr_short(fr, lang),
                          "requested_from": fr.get("requested_from"),
                          "grounds": [{"code": g["code"], "text": _text(g, lang)} for g in fr.get("grounds") or []],
                          **({"size": fr["size"]} if fr.get("size") else {}),
                          **_franchise_extra(fr, [_fr_how_text(h, lang) for h in fr.get("how") or []], fr_alts,
                                             lang)},
            "scenarios": scv["json"],
            "value": {"applicable": bool(v.get("applicable")), "ratio_pct": v["ratio_pct"],
                      "verdict": v["verdict"] if v.get("applicable") else "na", "text": vtext,
                      "legal_ref": v.get("legal_ref") if v.get("applicable") else None,
                      "depreciated": v.get("depreciated")},
            "analytics": anv["json"],
            "clauses": [{"code": c["code"], "text": c.get(lang) or c.get("ru"), "expert": True,
                         "calibrated": ae.CALIBRATED} for c in p.get("clauses") or []],
            "measures": [x["json"] for x in msv["items"]],
            "missing": [{"code": x["code"], "label": x["label"].get(lang) or x["label"].get("ru")}
                        for x in p.get("missing") or []],
        })
    cols = [t("pt_col_n", lang), t("pt_col_class", lang), t("pt_col_sum", lang), t("pt_col_level", lang),
            t("pt_col_rate", lang), t("pt_col_premium", lang), t("pt_col_fr", lang)]
    total_prem = totals.get("premium")
    table.append(["", t("pt_total", lang), money(totals.get("sum_insured"), lang),
                  tx.label(tx.LEVEL_LABELS, totals.get("level"), lang), "—",
                  _money_na(total_prem, lang), "—"])
    parts_list = {"title": t("pt_table_title", lang), "items": [" | ".join(r) for r in table],
                  "table": {"columns": cols, "rows": table, "widths": [5, 25, 17, 11, 10, 17, 15]},
                  "notes": [t("pt_table_note", lang)]}
    worst = next(p for p in items if p["index"] == totals.get("worst_index"))
    level_label = tx.label(tx.LEVEL_LABELS, totals.get("level"), lang)
    ref_pct = totals.get("reference_rate_pct")
    s4_rows = [_row(t("level", lang), level_label, t("pt_level_note", lang, part=_part_label(worst, lang)) + "; " +
                    t("uncalibrated", lang)),
               _row(t("premium", lang), _money_na(total_prem, lang),
                    t("pt_premium_note", lang, n=len(items), days=totals.get("term_days"))
                    if total_prem is not None else t("pt_premium_incomplete", lang,
                                                     known=money(totals.get("premium_known"), lang))),
               _row(t("pt_ref_rate", lang), _pct_na(ref_pct, lang),
                    t("pt_ref_note", lang))]
    fr_parts = "; ".join(f"{t('pt_part_short', lang, n=p['index'])} — {_fr_short(p['franchise'], lang)}"
                         for p in items)
    fr_text = t("pt_fr_by_parts", lang, list=fr_parts)
    s4_rows.append(_row(t("franchise", lang), fr_text))
    src = PT.get("source") or "default"
    notes = [t(x["code"], lang, **{k: (money(v, lang) if k in ("sum", "total") else v)
                                   for k, v in (x.get("params") or {}).items()}) for x in PT.get("notes") or []]
    s5 = t("pt_s5_confirmed", lang) if PT.get("confirmed") else t("pt_s5_default", lang,
                                                                   source=t("pt_src_" + src, lang))
    agg = totals.get("scenarios") or {}
    sc = D.get("scenarios") or {}
    ret = totals.get("retention") or {}
    summary_bits = [t("pt_sum_intro", lang, n=len(items), premium=money(total_prem, lang)
                      if total_prem is not None else NA, level=level_label, part=_part_label(worst, lang))]
    if sc.get("available"):
        summary_bits.append(t("pt_sum_sc", lang, eml=money(sc["items"]["EML"]["amount"], lang),
                              mfl=money(sc["items"]["MFL"]["amount"], lang),
                              rule=t("pt_sc_rule_" + str(agg.get("rule") or "max"), lang)))
    if ret.get("known"):
        summary_bits.append(t("pt_sum_ret_ok" if ret.get("within") else "pt_sum_ret_over", lang,
                              limit=money(ret.get("limit"), lang), x=money(ret.get("eml_excess"), lang)))
    summary = " ".join(summary_bits)
    obj_mode = PT.get("object_mode") or "different"
    js = {"mode": "multi", "source": src, "source_label": t("pt_src_" + src, lang),
          "confirmed": bool(PT.get("confirmed")), "object_mode": obj_mode,
          "object_mode_label": t("pt_mode_" + obj_mode, lang), "same_object_default": PT.get("same_object_default"),
          "items": pjs,
          "totals": {"premium": total_prem, "premium_text": _money_na(total_prem, lang),
                     "premium_complete": totals.get("premium_complete"), "premium_known": totals.get("premium_known"),
                     "sum_insured": totals.get("sum_insured"), "level": totals.get("level"),
                     "level_label": level_label, "worst_index": totals.get("worst_index"),
                     "reference_rate_pct": ref_pct, "reference_note": t("pt_ref_note", lang),
                     "scenarios": {"available": bool(sc.get("available")), "rule": agg.get("rule"),
                                   "rule_text": t("pt_sc_rule_" + str(agg.get("rule") or "max"), lang),
                                   **{s.lower(): (sc.get("items") or {}).get(s, {}).get("amount")
                                      for s in ("PML", "EML", "MFL")},
                                   "excluded": agg.get("excluded") or [], "how": _parts_sc_how(sc, lang)
                                   if sc.get("available") else []},
                     "retention": {"known": bool(ret.get("known")), "limit": ret.get("limit"),
                                   "compared_with": "eml", "eml_excess": ret.get("eml_excess"),
                                   "within": ret.get("within"), "mfl_excess": ret.get("mfl_excess"),
                                   "status": ret.get("status"), "legal_ref": ret.get("legal_ref")},
                     "value": totals.get("value"), "calibrated": ae.CALIBRATED},
          "notes": notes, "summary": summary, "table": {"columns": cols, "rows": table},
          "suggested_parts": [dict(x, class_name=_class_label(x["class_code"], next(
              (p.get("class_name") for p in items if p["class_code"] == x["class_code"]), None), lang))
              for x in PT.get("suggested") or []],
          "calibrated": ae.CALIBRATED}
    rate_extra = {"reference_pct": ref_pct, "reference_only": True, "reference_note": t("pt_ref_note", lang),
                  "by_parts": [{"index": x["index"], "class_code": x["class_code"], "mode": x["rate"]["mode"],
                                "applied_pct": x["rate"]["applied_pct"], "min_pct": x["rate"]["min_pct"],
                                "min_source": x["rate"]["min_source"], "final_pct": x["rate"]["final_pct"]}
                               for x in pjs]}
    return {"s1_rows": s1_rows, "s1_paragraph": t("pt_s1_par", lang, n=len(items),
                                                  mode=t("pt_mode_" + obj_mode, lang)) + (" " + " ".join(notes)
                                                                                          if notes else ""),
            "s3_rows": s3_rows, "s3_paragraphs": [t("pt_s3_par", lang)],
            "s4_rows": s4_rows, "s4_lists": [parts_list] + lists, "fr_text": fr_text,
            "s5_paragraph": s5, "missing_lines": missing_lines, "summary": t("an_summary", lang, text=summary),
            "analytics_json": {"available": False, "reason": "by_parts", "text": t("pt_an_by_parts", lang),
                               "summary": {"text": summary, "sentences": summary_bits}, "calibrated": ae.CALIBRATED},
            "worst": worst["index"], "rate_extra": rate_extra, "json": js}


def _objects_view(D: dict, lang: str) -> dict:
    """Парк ТС на языке акта: таблицы разделов 1 и 3, строки и перечень раздела 4, пометки раздела 5, JSON objects
    и objects_total. Подпись объекта — как ввёл сотрудник (без ПД, проверено при вводе)."""
    lang = tx.lang_of(lang)
    NA = t("na", lang)
    items, T = D.get("objects") or [], D.get("objects_total") or {}
    cols = [t("obj_col_n", lang), t("obj_col_object", lang), t("obj_col_year", lang), t("obj_col_sum", lang),
            t("obj_col_value", lang), t("obj_col_level", lang), t("obj_col_rate", lang), t("obj_col_premium", lang)]
    rows1, rows3, lines4, js = [], [], [], []
    for p in items:
        lv = tx.label(tx.LEVEL_LABELS, p["level"], lang)
        rate_txt = _pct_na(p.get("rate_pct"), lang)
        prem_txt = _money_na(p.get("premium"), lang)
        year_txt = str(p["year"]) if p.get("year") is not None else NA
        rows1.append([str(p["index"]), p["label"], year_txt, money(p["sum_insured"], lang),
                      money(p["object_value"], lang), lv, rate_txt, prem_txt])
        v = p["value"]
        vtxt = t("obj_v_" + v["verdict"], lang, ratio=pct(v["ratio_pct"], lang, 2)) if v.get("applicable") \
            else t("obj_v_na", lang)
        rows3.append(rows1[-1] + [vtxt])
        fa = p.get("factor_adjustment") or {}
        line = t("obj_line", lang, n=p["index"], label=p["label"], level=lv,
                 rate=_pct_na(p["rate"].get("applied_pct"), lang),
                 premium=money(p.get("premium_before_franchise"), lang)
                 if p.get("premium_before_franchise") is not None else NA)
        extra = []
        if p.get("franchise_applied"):
            extra.append(t("obj_fr", lang, rate=rate_txt, premium=prem_txt))
        if (fa.get("effect") or {}).get("available"):
            extra.append(t("obj_fa", lang, mult=_mult(fa.get("product") or 1, lang)))
        if p["rate"].get("min_applied"):
            extra.append(t("min_applied", lang))
        if p.get("below_min"):
            extra.append(t("obj_below_min", lang, req=pct(p["requested_rate_pct"], lang),
                           min=pct(p["rate"].get("min_pct"), lang)))
        lines4.append(line + ("; " + "; ".join(extra) if extra else ""))
        sc = p.get("scenarios") or {}
        js.append({
            "index": p["index"], "label": p["label"], "object_kind": p.get("object_kind"),
            "kind_label": tx.label({k: v_[1] for k, v_ in tx.OBJECT_KINDS.items()}, p["object_kind"], lang)
            if p.get("object_kind") in tx.OBJECT_KINDS else None,
            "year": p.get("year"), "year_source": p.get("year_source"), "mileage": p.get("mileage"),
            "plate_hint": p.get("plate_hint"), "photo_ids": p.get("photo_ids") or [],
            "photo_refs_unknown": p.get("photo_refs_unknown") or [], "inspected": bool(p.get("inspected")),
            "damages": len(p.get("damages") or []), "recognized": p.get("recognized") or [],
            "class_fields": p.get("class_fields") or {},
            "sum_insured": p["sum_insured"], "object_value": p["object_value"],
            "level": p["level"], "level_label": lv,
            "risk_factors": [{"code": f["code"], "sign": f["sign"], "text": _text(f, lang)}
                             for f in p["risk"]["factors"]],
            "rate": {"mode": p["rate"]["mode"], "base_pct": p["rate"].get("base_pct"),
                     "adj_pct": p["rate"].get("adj_pct"), "calc_pct": p["rate"].get("calc_pct"),
                     "applied_pct": p["rate"].get("applied_pct"), "min_pct": p["rate"].get("min_pct"),
                     "min_applied": bool(p["rate"].get("min_applied")),
                     "factors_applied": bool(p["rate"].get("factors_applied")),
                     "factor_product": fa.get("product"), "fork_applied": bool(p["rate"].get("fork_applied")),
                     "rate_type": p["rate"].get("rate_type") or "annual",
                     "how": [_text(h, lang) for h in p["rate"].get("how") or []]},
            "rate_pct": p.get("rate_pct"), "premium_before_franchise": p.get("premium_before_franchise"),
            "premium": p.get("premium"), "premium_text": prem_txt, "franchise_applied": bool(p.get("franchise_applied")),
            "requested_rate_pct": p.get("requested_rate_pct"), "below_min": bool(p.get("below_min")),
            "value": {"ratio_pct": v.get("ratio_pct"), "verdict": v.get("verdict"), "applicable": v.get("applicable"),
                      "text": vtxt, "legal_ref_text": tx.label(tx.LEGAL_REFS, v["legal_ref"], lang)
                      if v.get("legal_ref") else None},
            "scenarios": {"available": bool(sc.get("available")),
                          **({s.lower(): sc["items"][s]["amount"] for s in ae.SCENARIOS3} if sc.get("available")
                             else {})},
            "text": lines4[-1], "calibrated": ae.CALIBRATED})
    cnt = len(items)
    sd = T.get("scenarios") or {}
    total_txt = _money_na(T.get("premium"), lang)
    s1_par = t("obj_s1_par", lang, n=cnt, premium=total_txt, sum=money(T.get("sum_insured"), lang),
               value=money(T.get("object_value"), lang))
    if T.get("sums_from_objects"):
        s1_par += " " + t("obj_sums_auto", lang)
    vb = T.get("value_by_verdict") or {}
    s3_par = t("obj_s3_par", lang, n=cnt, under=len(vb.get("under") or []), over=len(vb.get("over") or []))
    rows4 = [_row(t("obj_rate_avg", lang), pct(T.get("rate_avg_before_pct"), lang)
                  if T.get("rate_avg_before_pct") is not None else NA,
                  t("obj_rate_avg_note", lang, days=T.get("term_days") or 365)),
             _row(t("min_rate", lang), _pct_na(D["rate"].get("min_pct"), lang), _min_src_text(D, lang))]
    if T.get("premium_before_franchise") is not None and T.get("premium") != T.get("premium_before_franchise"):
        rows4.append(_row(t("rate_with_fr", lang), pct(T.get("rate_avg_pct"), lang),
                          t("premium_no_fr", lang, before=money(T["premium_before_franchise"], lang))))
    rows4.append(_row(t("premium", lang), total_txt, t("obj_premium_note", lang, n=cnt,
                                                       days=T.get("term_days") or 365)))
    notes = [t(n_["code"], lang, **{k: (_mult(v_, lang) if k == "mult" and v_ is not None else v_)
                                    for k, v_ in (n_.get("params") or {}).items()}) for n_ in T.get("notes") or []]
    lg = sd.get("largest") or {}
    sm = sd.get("sum") or {}
    total = {"count": cnt, "sum_insured": T.get("sum_insured"), "object_value": T.get("object_value"),
             "premium": T.get("premium"), "premium_text": total_txt,
             "premium_before_franchise": T.get("premium_before_franchise"),
             "rate_avg_pct": T.get("rate_avg_pct"), "rate_avg_before_pct": T.get("rate_avg_before_pct"),
             "rate_type": T.get("rate_type"), "term_days": T.get("term_days"), "reference_only": True,
             "level": T.get("level"), "level_label": tx.label(tx.LEVEL_LABELS, T.get("level") or "moderate", lang),
             "worst_index": T.get("worst_index"), "levels": T.get("levels"),
             "value": {k: (T.get("value") or {}).get(k) for k in ("ratio_pct", "verdict", "sum_insured",
                                                                  "object_value")} if T.get("value") else None,
             "value_by_verdict": vb,
             "scenarios": {"largest": {k: lg.get(k) for k in ("index", "label", "PML", "EML", "MFL")} if lg else None,
                           "sum": {k: sm.get(k) for k in ("PML", "EML", "MFL", "count")} if sm else None,
                           "rule_text": t("obj_sc_rule", lang)},
             "bound": bool(T.get("bound")), "sums_from_objects": bool(T.get("sums_from_objects")), "notes": notes,
             "calibrated": ae.CALIBRATED}
    w1 = [5, 27, 7, 15, 15, 10, 9, 12]
    return {
        "s1_paragraph": s1_par,
        "s1_list": _li(t("obj_title", lang, n=cnt), [], {"columns": cols, "rows": rows1, "widths": w1}),
        "s3_paragraph": s3_par,
        "s3_list": _li(t("obj_s3_title", lang), [], {"columns": cols + [t("obj_col_ratio", lang)], "rows": rows3,
                                                     "widths": [4, 20, 6, 13, 13, 9, 8, 11, 16]}),
        "s4_rows": rows4,
        "s4_list": {"title": t("obj_s4_title", lang), "items": lines4},
        "notes": notes, "json": js, "total": total}


def _min_src_text(D: dict, lang: str) -> Optional[str]:
    """Источник минимальной ставки акта словами; акты до 01.10.2026 (без D["min_rate"]) — None."""
    mb = D.get("min_rate") or {}
    if not mb.get("insurer") or not mb.get("source"):
        return None
    return mrs.source_label({k: mb.get(k) for k in ("source", "effective_from", "note", "document_ref", "name")},
                            lang)


def _min_rate_view(D: dict, lang: str) -> dict:
    mb = D.get("min_rate")
    if not mb:
        return {"available": False, "reason": "old_act"}
    rt = mb.get("rate_type") or "annual"
    return {"available": mb.get("min_pct") is not None, "min_pct": mb.get("min_pct"), "rate_type": rt,
            "rate_type_label": t("rate_type_" + rt, lang), "insurer": bool(mb.get("insurer")),
            "source": mb.get("source"), "effective_from": mb.get("effective_from"), "note": mb.get("note"),
            "version_id": mb.get("version_id"), "source_text": _min_src_text(D, lang)}


def _bm_params(code: str, p: dict, lang: str) -> dict:
    out = {}
    for k, v in (p or {}).items():
        if v is None:
            out[k] = t("na", lang)
        elif k in ("req", "net", "market", "gap"):
            out[k] = pct(v, lang)
        elif k in ("share", "lim", "lr", "gap_rel") and code not in ("bm_market_ok", "bm_market_high"):
            v1 = round(float(v), 1)
            out[k] = tx.pct_fixed(v1, lang, 0 if v1 == int(v1) else 1)
        elif k in ("eml", "limit", "shortfall"):
            out[k] = money(v, lang)
        elif k in ("ratio", "lim"):
            out[k] = tx._num(float(v), lang, 2).rstrip("0").rstrip(",.") if float(v) != int(float(v)) \
                else str(int(float(v)))
        else:
            out[k] = v
    if code in ("bm_net_ok", "bm_net_below"):
        out["cal"] = "" if p.get("calibrated") else t("bm_cal_expert", lang)
    return out


def _bm_reason_text(r: dict, lang: str) -> str:
    code = r["code"]
    p = r.get("params") or {}
    if code == "bm_level":
        return t("bm_level_" + str(p.get("level")), lang)
    if code == "bm_gap" and p.get("rate_type") == "fixed":
        code = "bm_gap_fixed"
    return t(code, lang, **_bm_params(r["code"], p, lang))


def _below_min_view(D: dict, lang: str) -> dict:
    """Оценка заниженной ставки на языке акта: строки раздела 4, абзац раздела 5 и JSON below_min_assessment."""
    bm = D.get("below_min") or {}
    js = {"available": bool(bm.get("available")), "reason": bm.get("reason") if bm else "old_act",
          "requested_pct": bm.get("requested_pct"), "requested_source": bm.get("requested_source"),
          "requested_source_label": t("bm_src_" + bm["requested_source"], lang) if bm.get("requested_source") else None,
          "min_pct": bm.get("min_pct"), "calibrated": ae.CALIBRATED, "note": t("bm_note", lang)}
    if not js["available"]:
        return {"lines": [], "s5": None, "json": js}
    verdict = bm["verdict"]
    vlabel = t("bm_v_" + verdict, lang)
    reasons = [{"code": r["code"], "sign": r["sign"], "effect": r["effect"], "value": r.get("value"),
                "text": _bm_reason_text(r, lang)} for r in bm["reasons"]]
    conds = [{"code": c, "text": t(c, lang)} for c in bm.get("conditions") or []]
    min_src = _min_src_text(D, lang) or t("bm_min_insurer" if bm.get("min_insurer") else "bm_min_regulator", lang)
    head = t("bm_head", lang, req=pct(bm["requested_pct"], lang), src=js["requested_source_label"],
             min=pct(bm["min_pct"], lang), min_src=min_src, verdict=vlabel)
    parts = [head]
    if verdict == "not_allowed":
        why = [r["text"] for r in reasons if r["effect"] in ("blocks", "no_conditions")]
        parts.append(t("bm_why_no", lang, why="; ".join(why)))
    elif verdict == "allowed_with_conditions":
        why = [r["text"] for r in reasons if r["effect"] == "no_yes"]
        parts.append(t("bm_why_cond", lang, why="; ".join(why)))
        parts.append(t("bm_conds", lang, what="; ".join(c["text"] for c in conds)))
    else:
        parts.append(t("bm_why_yes", lang))
        parts.append(t("bm_conds", lang, what="; ".join(c["text"] for c in conds)))
    sf = t("bm_shortfall_fixed" if bm.get("rate_type") == "fixed" else "bm_shortfall", lang,
           days=bm.get("term_days"), sum=money(bm.get("shortfall"), lang))
    parts.append(sf)
    text = " ".join(parts)
    js.update(gap_pct=bm.get("gap_pct"), gap_rel_pct=bm.get("gap_rel_pct"), share_pct=bm.get("share_pct"),
              shortfall=bm.get("shortfall"), shortfall_text=money(bm.get("shortfall"), lang),
              term_days=bm.get("term_days"), rate_type=bm.get("rate_type"),
              requested_annual_pct=bm.get("requested_annual_pct"), verdict=verdict, verdict_label=vlabel,
              hard=bool(bm.get("hard")), reasons=reasons, conditions=conds, text=text, min_source_text=min_src,
              rule=bm.get("rule"))
    lines = [text]
    lines += [t("bm_line", lang, sign=t("bm_" + r["sign"], lang), text=r["text"]) for r in reasons]
    lines.append(js["note"])
    rate = ((D.get("premium_final") or {}).get("rate_pct"))
    s5 = t("bm_s5_" + verdict, lang, req=pct(bm["requested_pct"], lang), min=pct(bm["min_pct"], lang),
           rate=pct(rate, lang))
    return {"lines": lines, "s5": s5, "json": js}
