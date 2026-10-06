"""
Акт для Word и PDF на один лист A4 (06.10.2026, замечание заказчика): шапка и пять разделов заказчика.

Собирается из готового ответа render (тот же JSON, что у экрана) — поэтому цифры в документе совпадают с экраном,
а сам JSON и экран не меняются. Внутренние пояснения (как сверен договор, как посчитаны франшиза и сценарии, рынок и
статистика с источниками, разбор по рискам, формулы удержания, скоринг-картинка) в документ не идут — они остаются
в JSON и на экране.

Разделы: 1. Объект и идентификация (строки распознанного); 2. Результаты осмотра и предсуществующие повреждения;
3. Стоимость и страховая сумма (одна строка с %); 4. Риск-факторы и франшиза — четыре блока по схеме андеррайтинга:
«Оценка риска», «Решение», «Условия», «Цена»; 5. Заключение и рекомендация (3–4 предложения и три мероприятия).

Решение документа — правило заказчика (экспертно, calibrated = 0): высокий уровень риска, запрошенная ставка ниже
минимальной или нет фото → «На рассмотрение специалиста»; отказ движка акта (сработали все повышающие признаки) →
«Отказать»; иначе — «Принять».
"""
from typing import Optional

from .. import act_texts as tx
from ..act_texts import money, pct, t

MAX_VALUE = 110
# порядок ракурсов в строке осмотра: как обходят объект (спереди, сзади, борта, табличка, счётчик, документ)
VIEW_ORDER = ("front", "back", "left", "right", "plate", "odometer", "interior", "facade", "roof", "electrical",
              "fire_safety", "general", "installation", "packaging", "marking", "transport", "document")          # длинное значение строки раздела 1 обрезается по слову
MAX_CLAUSE = 82


def _short(s, n: int) -> str:
    s = " ".join(str(s or "").split())
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0].rstrip(",;:—- ")
    return cut + "…"


def _row_value(rows: list, label: str) -> Optional[str]:
    for r in rows or []:
        if r.get("label") == label:
            return str(r.get("value"))
    return None


def decision(act: dict) -> dict:
    """Решение документа по правилу заказчика: {code: accept | decline | review, label, why}."""
    lang = act["lang"]
    risk = act.get("risk") or {}
    ins = act.get("inspection") or {}
    bm = act.get("below_min_assessment") or {}
    level = risk.get("level")
    level_label = risk.get("level_label") or tx.label(tx.LEVEL_LABELS, level or "moderate", lang)
    if (act.get("decision") or {}).get("code") == "decline":
        return {"code": "decline", "label": t("cp_dec_decline", lang), "why": t("cp_why_decline", lang)}
    why = []
    if level == "high":
        why.append(t("cp_why_high", lang))
    if bm.get("available"):
        why.append(t("cp_why_below", lang))
    if not ins.get("photos"):
        why.append(t("cp_why_no_photo", lang))
    if why:
        return {"code": "review", "label": t("cp_dec_review", lang), "why": "; ".join(why)}
    return {"code": "accept", "label": t("cp_dec_accept", lang),
            "why": t("cp_why_ok", lang, level=level_label.lower() if lang != "en" else level_label)}


def _s1(act: dict) -> dict:
    lang = act["lang"]
    NA = t("na", lang)
    s = act["sections"][0]
    pairs = []
    for r in s.get("rows") or []:
        v = str(r.get("value") if r.get("value") is not None else "")
        if not v or v == NA or r.get("label") == t("s1_not_specified", lang):
            continue
        pairs.append((r["label"], _short(v, MAX_VALUE)))
    table = None
    for li in s.get("lists") or []:
        tb = li.get("table")
        if tb and tb.get("rows"):
            table = {"title": li.get("title"), "columns": list(tb["columns"]), "rows": tb["rows"],
                     "widths": tb.get("widths")}
            break
    return {"n": 1, "title": t("cp_s1", lang), "pairs": pairs, "table": table, "lines": [], "blocks": []}


def _s2(act: dict) -> dict:
    lang = act["lang"]
    ins = act.get("inspection") or {}
    lines = []
    photos = ins.get("photos") or 0
    if not photos:
        lines.append(t("cp_s2_no_photo", lang))
    elif not ins.get("done"):
        lines.append(t("cp_s2_ai_failed", lang, n=photos))
    else:
        vg = act.get("veh_group")
        order = {v: i for i, v in enumerate(VIEW_ORDER)}
        key = lambda v: (order.get(v, len(order)), v)                                   # noqa: E731
        seen = [tx.view_label(v, lang, vg) for v in sorted(ins.get("views_seen") or [], key=key) if v != "other"]
        missing = [tx.view_label(v, lang, vg) for v in sorted(ins.get("missing_views") or [], key=key)]
        if missing:
            lines.append(t("cp_s2_inspected", lang, n=photos, seen=", ".join(seen) or t("na", lang),
                           missing=", ".join(missing)))
        else:
            lines.append(t("cp_s2_inspected_all", lang, n=photos, seen=", ".join(seen) or t("na", lang)))
    dmg = ins.get("damages") or []
    if dmg:
        items = [str(d.get("what") or "") + (f" ({d['where']})" if d.get("where") else "") for d in dmg]
        lines.append(t("cp_s2_damages", lang, list=", ".join(items)))
    elif ins.get("done"):
        lines.append(t("cp_s2_damages_none", lang))
    else:
        lines[-1] += "; " + t("cp_s2_damages_na", lang)[:1].lower() + t("cp_s2_damages_na", lang)[1:]
    docs = _row_value(act["sections"][1].get("rows"), t("documents", lang))
    if docs:
        lines[-1] += ". " + t("cp_s2_docs", lang, v=docs)
    return {"n": 2, "title": t("cp_s2", lang), "pairs": [], "table": None, "lines": lines, "blocks": []}


def _s3(act: dict) -> dict:
    lang = act["lang"]
    rows = act["sections"][2].get("rows") or []
    val = act.get("value") or {}
    fv = val.get("final_verdict") or val.get("verdict")
    if fv == "over":
        verdict = t("cp_v_over", lang)
    elif fv == "under":
        verdict = t("cp_v_under", lang)
    elif fv == "refine":
        verdict = t("cp_v_refine", lang)
    elif val.get("legal_ref"):
        verdict = t("cp_v_prop", lang)
    else:
        verdict = t("cp_v_normal", lang)
    line = t("cp_s3_line", lang, sum=_row_value(rows, t("sum_insured", lang)) or t("na", lang),
             value=_row_value(rows, t("object_value", lang)) or t("na", lang),
             ratio=pct(val.get("ratio_pct"), lang, 2), verdict=verdict)
    return {"n": 3, "title": t("cp_s3", lang), "pairs": [], "table": None, "lines": [line], "blocks": []}


def _hazards(act: dict) -> list:
    """Опасности 3–6 пунктов: экспертные доли шаблона класса, иначе риски аналитики (доля в нетто-ставке)."""
    lang = act["lang"]
    out = []
    tr = ((act.get("template") or {}).get("risks") or {}).get("items") or []
    for x in tr:
        if x.get("label"):
            out.append(f"{x['label']}" + (f" — {pct(x['share_pct'], lang)}" if x.get("share_pct") is not None else ""))
    if len(out) < 3:
        an = ((act.get("analytics") or {}).get("risks") or {}).get("items") or []
        alt = []
        for x in an:
            name = x.get("name") or x.get("label") or x.get("code")
            share = x.get("share_of_net_pct")
            alt.append(f"{name}" + (f" — {tx.pct_fixed(share, lang, 1)}" if share is not None else ""))
        if len(alt) > len(out):
            out = alt
    return out[:6]


def _frequency(act: dict) -> str:
    lang = act["lang"]
    risk = act.get("risk") or {}
    lvl = risk.get("level") or "moderate"
    if any(f.get("code") == "f_loss_up" for f in risk.get("factors") or []):
        lvl = "high"
    return t("cp_freq", lang, v=t("cp_freq_" + (lvl if lvl in ("low", "moderate", "high") else "moderate"), lang))


def _mln(x, lang: str) -> str:
    """Сумма в миллионах для строки «Ожидаемая тяжесть»: 1 472 500 000 → «1 472,5 млн сум» (точные суммы — в JSON)."""
    if x is None or abs(float(x)) < 1e6:
        return money(x, lang)
    v = round(float(x) / 1e6, 1)
    return t("cp_mln", lang, v=tx._num(v, lang, 0 if v == int(v) else 1))


def _severity(act: dict) -> list:
    lang = act["lang"]
    sc = act.get("scenarios") or {}
    if not sc.get("available") or not sc.get("pml"):
        return [t("cp_sev_na", lang)]
    parts = [f"{k.upper()} {_mln(sc[k]['amount'], lang)} ({sc[k].get('pct_text') or pct(sc[k].get('pct'), lang)})"
             for k in ("pml", "eml", "mfl") if sc.get(k)]
    lines = [t("cp_sev", lang, v=" / ".join(parts))]
    for k in ("pml", "eml", "mfl"):
        it = sc.get(k) or {}
        if it.get("text"):
            # суммы уже в строке «Ожидаемая тяжесть» — в строке сценария только доля
            lines.append(it["text"].replace(f" ({money(it['amount'], lang)})", ""))
    return lines


def _franchise_line(act: dict) -> str:
    lang = act["lang"]
    fr = act.get("franchise") or {}
    st = fr.get("status")
    if st == "applied":
        return t("cp_fr_applied", lang, type=fr.get("type_label") or "", pct=pct(fr.get("size_pct"), lang),
                 amount=money(fr.get("size_amount"), lang))
    if st == "statutory":
        return t("cp_fr_statutory", lang)
    if fr.get("needed") or st == "proposed":
        if fr.get("size_pct"):
            v = f"{fr.get('type_label') or ''} {pct(fr['size_pct'], lang)} ({money(fr.get('size_amount'), lang)})".strip()
        else:
            v = _short(fr.get("text"), 120)
        return t("cp_fr_proposed", lang, v=v)
    return t("cp_fr_none", lang)


def _price(act: dict, rows4: list) -> list:
    lang = act["lang"]
    r = act.get("rate") or {}
    pr = act.get("premium") or {}
    mode = r.get("mode")
    lines = []
    level_label = (act.get("risk") or {}).get("level_label") or ""
    if act.get("objects"):
        tot = act.get("objects_total") or {}
        avg = tot.get("rate_avg_pct") if tot.get("rate_avg_pct") is not None else tot.get("rate_avg_before_pct")
        lines.append(t("cp_rate_objects", lang, v=pct(avg, lang) if avg is not None else t("na", lang)))
    elif mode == "multi":
        parts = []
        for p in (act.get("parts") or {}).get("items") or []:
            pr_ = p.get("rate") or {}
            rp = pr_.get("final_pct") if pr_.get("final_pct") is not None else pr_.get("applied_pct")
            parts.append(f"{p.get('index')}) {p.get('class_code')} — {pct(rp, lang) if rp is not None else t('na', lang)}")
        lines.append(t("cp_rate_multi", lang, v="; ".join(parts) or t("na", lang)))
    elif mode == "tariff":
        k = 1 + float(r.get("adj_pct") or 0) / 100
        lines.append(t("cp_rate_tariff", lang, rate=pct(r.get("applied_pct"), lang), base=pct(r.get("base_pct"), lang),
                       k=tx._num(k, lang, 2), level=level_label.lower() if lang != "en" else level_label,
                       calc=pct(r.get("calc_pct"), lang),
                       min=pct(r.get("min_pct"), lang) if r.get("min_pct") is not None else t("na", lang),
                       floor=t("cp_floor", lang) if r.get("min_applied") else ""))
        extra = []
        if r.get("factors_applied"):
            extra.append(t("cp_w_factors", lang))
        if r.get("fork_applied"):
            extra.append(t("cp_w_fork", lang))
        if pr.get("franchise_applied"):
            extra.append(t("cp_w_franchise", lang))
        if extra and r.get("final_pct") is not None:
            lines[-1] += "; " + t("cp_rate_with", lang, what=", ".join(extra), rate=pct(r["final_pct"], lang))
    elif mode in ("statutory",):
        lines.append(t("cp_rate_statutory", lang, rate=pct(r.get("applied_pct"), lang)))
    else:
        lines.append(t("cp_rate_undefined", lang))
    if pr.get("amount") is not None:
        if mode in ("tariff", "statutory") and not act.get("objects"):
            key = "cp_premium_fixed" if r.get("rate_type") == "fixed" else "cp_premium"
            lines.append(t(key, lang, amount=money(pr["amount"], lang), days=pr.get("term_days") or 365))
        else:
            lines.append(t("cp_premium_plain", lang, amount=money(pr["amount"], lang)))
    bm = act.get("below_min_assessment") or {}
    if bm.get("available"):
        v = bm.get("verdict")
        ans = bm.get("verdict_label") or t({"not_allowed": "cp_below_no",
                                           "allowed_with_conditions": "cp_below_cond"}.get(v, "cp_below_yes"), lang)
        reasons = bm.get("reasons") or []
        if v == "not_allowed":
            why = [x["text"] for x in reasons if x.get("effect") in ("blocks", "no_conditions")]
        elif v == "allowed_with_conditions":
            why = [c["text"] for c in bm.get("conditions") or []] or [x["text"] for x in reasons]
        else:
            why = [x["text"] for x in reasons if x.get("sign") == "plus"] or [x["text"] for x in reasons]
        lines.append(t("cp_below", lang, req=pct(bm.get("requested_pct"), lang), min=pct(bm.get("min_pct"), lang),
                       answer=ans, why=_short(why[0] if why else "", 150).rstrip("."),
                       short=bm.get("shortfall_text") or money(bm.get("shortfall"), lang)))
    return lines


def _s4(act: dict, dec: dict) -> dict:
    lang = act["lang"]
    risk = act.get("risk") or {}
    rlines = []
    ups = [f["text"].split(" — ")[0] for f in risk.get("factors") or [] if f.get("sign") == "up"]
    lvl = risk.get("level_label") or ""
    rlines.append(t("cp_level", lang, v=lvl, up="; ".join(ups)) if ups else t("cp_level_plain", lang, v=lvl))
    rlines.append(_frequency(act))
    sev = _severity(act)
    blocks = [{"title": t("cp_b_risk", lang), "bullets_title": t("cp_hazards", lang), "bullets": _hazards(act),
               "bullet_cols": 3, "lines": [], "after": rlines + sev},
              {"title": t("cp_b_decision", lang), "inline": True,
               "lines": [t("cp_dec_line", lang, d=dec["label"], why=dec["why"])]}]
    terms = []
    S = _row_value(act["sections"][2].get("rows"), t("sum_insured", lang))
    if S:
        terms.append(t("cp_limit", lang, sum=S))
    terms.append(_franchise_line(act))
    cl = [_short(c.get("text"), MAX_CLAUSE).rstrip(".") for c in act.get("clauses") or []][:2]
    if len(cl) == 2 and len(cl[0]) + len(cl[1]) > 2 * MAX_CLAUSE - 40:
        cl = cl[:1]                       # две длинные оговорки не помещаются в строку — первая, остальные в JSON
    if cl:
        terms.append(t("cp_clauses", lang, v="; ".join(cl)))
    if (act.get("inspection") or {}).get("damages"):
        terms.append(t("cp_excl_damages", lang))
    elif len(terms) < 4:
        terms.append(t("cp_excl_rules", lang))
    blocks.append({"title": t("cp_b_terms", lang), "lines": terms[:4]})
    blocks.append({"title": t("cp_b_price", lang), "lines": _price(act, act["sections"][3].get("rows") or [])})
    return {"n": 4, "title": t("cp_s4", lang), "pairs": [], "table": None, "lines": [], "blocks": blocks}


def _s5(act: dict, dec: dict) -> dict:
    lang = act["lang"]
    risk = act.get("risk") or {}
    sc = act.get("scoring") or {}
    pr = act.get("premium") or {}
    r = act.get("rate") or {}
    lvl = risk.get("level_label") or ""
    lvl = lvl.lower() if lang != "en" else lvl
    sent = [t("cp_s5_dec", lang, d=dec["label"])]
    if sc.get("available"):
        sent.append(t("cp_s5_score", lang, level=lvl, score=sc.get("score"), cls=sc.get("class_code")))
    else:
        sent.append(t("cp_s5_level", lang, level=lvl))
    if pr.get("amount") is not None and r.get("final_pct") is not None:
        sent.append(t("cp_s5_price", lang, amount=money(pr["amount"], lang), rate=pct(r["final_pct"], lang)))
    checks = [c for c in (act.get("decision") or {}).get("checks") or []][:2]
    if checks:
        # первые две проверки андеррайтеру, коротко; полный перечень — в JSON и на экране
        sent.append(t("cp_s5_checks", lang, v="; ".join(_short(c, 60).rstrip(".") for c in checks)))
    ms = []
    for m in (act.get("measures") or [])[:3]:
        if m.get("premium_delta"):
            eff = t("cp_ms_eff", lang, pct=tx.pct_fixed(abs(m.get("effect_pct") or 0), lang, 1),
                    sign="−" if (m.get("effect_pct") or 0) < 0 else "+", delta=money(abs(m["premium_delta"]), lang))
        elif m.get("effect_pct"):
            eff = t("cp_ms_eff_pct", lang, pct=tx.pct_fixed(abs(m["effect_pct"]), lang, 1),
                    sign="−" if m["effect_pct"] < 0 else "+")
        else:
            eff = t("cp_ms_eff_none", lang)
        ms.append(t("cp_ms_line", lang, text=_short(str(m.get("text") or "").rstrip("."), 90), effect=eff))
    return {"n": 5, "title": t("cp_s5", lang), "pairs": [], "table": None, "lines": [" ".join(sent)],
            "blocks": [{"title": t("cp_s5_measures", lang), "lines": [], "bullets": ms or [t("cp_ms_none", lang)]}]}


def compact_view(act: dict) -> dict:
    """Документ на один лист из ответа render: шапка, пять разделов, подпись. Язык — act["lang"]."""
    lang = act["lang"]
    dec = decision(act)
    head = t("cp_head", lang, number=act.get("number"), date=act.get("date"),
             insurer=act.get("insurer") if act.get("insurer_known") else "INSON")
    return {"lang": lang, "title": act["title"], "head": head, "decision": dec,
            "sections": [_s1(act), _s2(act), _s3(act), _s4(act, dec), _s5(act, dec)],
            "sign": t("cp_sign", lang), "calibrated": 0}
