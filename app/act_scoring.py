"""
Страховой скоринг объекта (01.10.2026) — первая страница акта в стиле отчёта кредитного бюро: шапка запроса,
блоки на синих полосах, полукруглая шкала 0–500 с пятью секторами и стрелкой, плашка класса.

Это представление уже посчитанного акта: балл даёт чистая функция act_engine.insurance_score (500 − 5 × балл риска
0–100 аналитики акта; без аналитики — по уровню риска), правила акта (ставка, уровень, франшиза, сценарии) не
меняются. Всё помечено «экспертно, не калибровано; это не кредитный скоринг и не оценка КАТМ».
Рядом с классом — рекомендация и уровень риска акта (при «отказать» плашка перечёркнута красным), уровень по
аналитике тем же словом, что в разделе 4; сектора шкалы — по порогам балла риска аналитики.

  view(D, out, lang, meta)  — блок scoring ответа /act/make и GET /act/{id} (слова на языке акта);
  draw_page(page, sc, ...)  — страница скоринга в PDF (pymupdf: секторы, стрелка, текст);
  gauge_png(sc, ...)        — картинка шкалы с плашкой класса (тот же код рисования → PNG) для Word и /scoring.png;
  docx_section(doc, sc, png) — первая секция Word.
Логотип и название КАТМ не используются: это документ страховщика.
"""
import math
from datetime import datetime
from typing import Optional

import pymupdf

from . import act_engine as ae
from . import act_texts as tx
from .act_texts import money, pct, t

DEFAULT_BRAND = "#0B4F8A"
# цвета секторов: E красный, D оранжевый, C жёлтый, B светло-зелёный, A зелёный
BAND_RGB = {"E": (0.80, 0.14, 0.12), "D": (0.93, 0.43, 0.10), "C": (0.97, 0.74, 0.08),
            "B": (0.62, 0.78, 0.19), "A": (0.27, 0.62, 0.20)}
GRAY = (0.45, 0.47, 0.50)
DARK = (0.10, 0.11, 0.13)
LIGHT = (0.90, 0.91, 0.92)
WHITE = (1, 1, 1)
OVERVIEW_CODES = ("sum", "value", "ratio", "tariff", "premium", "term", "losses", "docs", "disc", "views",
                  "pml", "eml", "mfl", "retention", "market", "fork", "below_min", "franchise", "checks")


def rgb(hex_color: Optional[str]) -> tuple:
    s = str(hex_color or DEFAULT_BRAND).lstrip("#")
    try:
        return tuple(int(s[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except (ValueError, IndexError):
        return rgb(DEFAULT_BRAND)


def _n(x, lang: str, digits: int = 1) -> str:
    """Число без лишних нулей: 14,2 / 0,278 / 3."""
    if x is None:
        return t("na", lang)
    s = f"{float(x):.{digits}f}".rstrip("0").rstrip(".")
    return s if lang == "en" else s.replace(".", ",")


# --------------------------------------------------------------------------- #
#  Блок scoring (JSON)
# --------------------------------------------------------------------------- #

def _eq(S: dict) -> str:
    """«=» если 500 − 5 × балл риска — целое, иначе «≈» (500 − 5 × 50,9 ≈ 246)."""
    return "=" if S.get("exact", True) else "≈"


def _components(S: dict, lang: str) -> list:
    out = []
    for c in S["components"]:
        code = c["code"]
        if code == "act_level":
            label = t("sc_comp_level", lang)
            why = t("sc_comp_level_why", lang, level=tx.label(tx.LEVEL_LABELS, S.get("level") or "moderate", lang),
                    score=c["points"])
        elif code == "risk_score":
            label = t("sc_comp_risk", lang)
            why = f"500 − 5 × {_n(c['risk_points'], lang)} {_eq(S)} {c['points']}"
        elif code == "damages":
            label = t("sc_comp_dmg", lang)
            why = t("sc_comp_dmg_why", lang, n=c.get("n") or 0, sev=t("dmg_sev_" + (c.get("severity") or "major"), lang),
                    pen=-int(c["points"]))
        else:
            label = tx.label(tx.SCORE_COMP_LABELS, code, lang) if code in tx.SCORE_COMP_LABELS else c["label"]
            why = t("sc_comp_why", lang, p=_n(c["risk_points"] or 0, lang), w=_n(c["weight"] or 0, lang, 3),
                    points=c["points"], max=c["max"]) if c["applicable"] else t("sc_comp_off", lang)
        out.append({"code": code, "label": label, "points": c["points"], "max": c["max"],
                    "applicable": c["applicable"], "risk_points": c.get("risk_points"), "weight": c.get("weight"),
                    "why": why, "calibrated": ae.CALIBRATED})
    return out


def _subject(D: dict, out: dict, lang: str) -> dict:
    from . import act as A
    NA = t("na", lang)
    must, rec = D["must"], D.get("recognized") or []
    name = None
    obj_label = A._s1_label("object_type", lang, D.get("group"))
    for r in (out.get("sections") or [{}])[0].get("rows") or []:
        if r.get("label") == obj_label and r.get("value") and r["value"] != NA:
            name = str(r["value"])
            break
    kind = None
    try:
        kind = A._kind_label(D, lang)
    except Exception:                        # подпись вида — не повод ронять страницу скоринга
        kind = None
    kind = kind or tx.label(tx.GROUP_LABELS, D.get("group") or "", lang)
    note = None
    try:
        dflt = A._default_otype(D)
        dlabel = A._otype_label(dflt, lang) if dflt else None
    except Exception:                        # пометка «по умолчанию» — не повод ронять страницу скоринга
        dflt = dlabel = None
    if dlabel and name == t("s1_kind_default", lang, v=dlabel):
        # «принят по умолчанию: …» — пометка, а не наименование: наименование — сам вид, пометка — в примечание
        name, note = dlabel, t("s1_kind_default_note", lang)
    if not name:
        name = kind or (must.get("product_name") if lang == "ru" else None) or A._class_label(
            must.get("class_code"), must.get("class_name"), lang)
    ph = A._policyholder(D.get("request"), D.get("contract"), {})
    if ph and ph.get("kind") == "legal" and ph.get("name"):
        holder, holder_kind = ph["name"], "legal"
    elif ph and ph.get("kind") == "individual":
        holder, holder_kind = t("sc_holder_individual", lang), "individual"
    else:
        holder, holder_kind = t("sc_holder_unknown", lang), None
    region = A.region_label(must, lang)
    address = (D.get("object_facts") or {}).get("address")
    ids = []
    cad = (A.preferred(rec, "cadastre_no") or {}).get("value") or (D.get("contract") or {}).get("cadastre_no") \
        or (D.get("request") or {}).get("cadastre_no")
    if cad:
        ids.append({"code": "cadastre_no", "value": cad, "text": t("sc_id_cadastre", lang, v=cad)})
    ser = (A.preferred(rec, "serial_no") or {}).get("value")
    if ser:
        ids.append({"code": "serial_no", "value": ser, "text": t("sc_id_serial", lang, v=ser)})
    ins = D.get("inspection") or {}
    src = []
    if ins.get("photos"):
        src.append(t("sc_src_photos" if ins.get("ai") else "sc_src_photos_noai", lang, n=ins["photos"]))
    kinds = []
    for k in ins.get("document_kinds") or []:
        lk = A._doc_kind_label(k, lang)
        if lk not in kinds:
            kinds.append(lk)
    if (D.get("borrower") or {}).get("available"):
        lk = tx.label(tx.DOC_KIND_LABELS, "credit_report", lang)
        if lk not in kinds:
            kinds.append(lk)
    if kinds:
        src.append(t("sc_src_docs", lang, what=", ".join(kinds)))
    src.append(t("sc_src_input", lang))
    reg = region + (f", {address}" if address else "")
    rows = [{"code": "name", "label": t("sc_s_name", lang), "value": name}]
    # вид объекта, совпадающий с наименованием, второй раз не выводится
    if not kind or str(kind).strip().lower() != str(name or "").strip().lower():
        rows.append({"code": "kind", "label": t("sc_s_kind", lang), "value": kind or NA})
    rows += [{"code": "policyholder", "label": t("sc_s_holder", lang), "value": holder},
             {"code": "region", "label": t("sc_s_region", lang), "value": reg or NA},
             {"code": "identifiers", "label": t("sc_s_ids", lang),
              "value": "; ".join(i["text"] for i in ids) or t("sc_ids_none", lang)},
             {"code": "source", "label": t("sc_s_source", lang), "value": "; ".join(src)}]
    if note:
        rows.append({"code": "note", "label": t("sc_s_note", lang), "value": note})
    return {"name": name, "kind": kind, "policyholder": holder if holder_kind == "legal" else None,
            "policyholder_kind": holder_kind, "region": region, "address": address, "identifiers": ids,
            "source": "; ".join(src), "note": note, "rows": rows}


def _scored_analytics(D: dict, out: dict, S: dict) -> dict:
    """Аналитика (на языке акта), по которой считан балл: акта или самой опасной части."""
    if S.get("contract"):
        for p in (out.get("parts") or {}).get("items") or []:
            if p.get("index") == S.get("worst_part"):
                return p.get("analytics") or {}
        return {}
    return out.get("analytics") or {}


def _overview(D: dict, out: dict, S: dict, lang: str) -> list:
    NA = t("na", lang)
    no = t("sc_v_no_data", lang)
    must, opt, ins = D["must"], D.get("optional") or {}, D.get("inspection") or {}
    rate = D.get("rate") or {}
    prem = out.get("premium") or {}
    items = {}
    items["sum"] = (money(must.get("sum_insured"), lang), must.get("sum_insured"))
    items["value"] = (money(must.get("object_value"), lang), must.get("object_value"))
    rp = (D.get("value") or {}).get("ratio_pct")
    items["ratio"] = (pct(rp, lang, 2) if rp is not None else NA, rp)
    mode = rate.get("mode")
    if mode == "multi":
        ref = rate.get("reference_pct")
        items["tariff"] = (t("sc_v_ref", lang, v=pct(ref, lang)) if ref is not None else NA, ref)
    elif mode in ("tariff", "statutory"):
        r = prem.get("rate_pct") if prem.get("rate_pct") is not None else rate.get("applied_pct")
        items["tariff"] = (pct(r, lang), r)
    else:
        items["tariff"] = (NA, None)
    items["premium"] = (money(prem.get("amount"), lang) if prem.get("amount") is not None else NA, prem.get("amount"))
    term = rate.get("term_days") or prem.get("term_days")
    items["term"] = (str(term) if term else NA, term)
    lc = opt.get("losses_count")
    items["losses"] = (str(lc) if lc is not None else no, lc)
    kinds = set(ins.get("document_kinds") or [])
    provided = max(len(kinds), int(ins.get("parsed_docs") or 0), 1 if ins.get("documents") else 0)
    if (D.get("borrower") or {}).get("available"):
        provided = max(provided, 1)
    need = len(((D.get("template") or {}).get("documents") or {}).get("items") or [])
    missing = max(0, need - provided) if need else len(D.get("missing") or [])
    items["docs"] = (f"{provided} / {missing}", {"provided": provided, "missing": missing})
    nd = len(D.get("discrepancies") or [])
    items["disc"] = (str(nd), nd)
    req = list(ins.get("required_views") or [])
    if req:
        seen = len(req) - len(ins.get("missing_views") or []) if ins.get("photos") and ins.get("ai") else 0
        items["views"] = (t("sc_v_of", lang, n=seen, m=len(req)), {"seen": seen, "required": len(req)})
    else:
        n = len([v for v in ins.get("views_seen") or [] if v not in ("document", "other")])
        items["views"] = (str(n), {"seen": n, "required": 0})
    sc = out.get("scenarios") or {}
    for code in ("pml", "eml", "mfl"):
        a = (sc.get(code) or {}).get("amount") if sc.get("available") else None
        items[code] = (money(a, lang) if a is not None else no, a)
    ret = (D.get("scenarios") or {}).get("retention") or {}
    lim = ret.get("limit") if (ret.get("known", True) and ret.get("limit") is not None) else None
    items["retention"] = (money(lim, lang) if lim is not None else t("sc_v_not_set", lang), lim)
    mk = (_scored_analytics(D, out, S).get("market") or {})
    mr = mk.get("rate_pct") if mk.get("available") else None
    items["market"] = (pct(mr, lang) if mr is not None else no, mr)
    # вилка ставки (01.10.2026): «минимум – ставка акта – рынок, %»
    rf = out.get("rate_fork") or {}
    items["fork"] = (rf.get("overview") or no, {k: (rf.get("recommended") or {}).get("rate_pct") if k == "rec" else
                                            next((m["rate_pct"] for m in rf.get("marks") or [] if m["code"] == k), None)
                                            for k in ("min", "rec", "market")})
    # запрошенная ставка ниже минимальной (01.10.2026): можно ли принять — да / да, при условиях / нет
    bm = out.get("below_min_assessment") or {}
    if bm.get("available"):
        items["below_min"] = (bm.get("verdict_label") or NA, bm.get("verdict"))
    elif bm.get("reason") == "not_below":
        items["below_min"] = (t("sc_v_bm_not_below", lang), None)
    else:
        items["below_min"] = (t("sc_v_bm_none", lang), None)
    items["franchise"] = ((out.get("franchise") or {}).get("text") or NA, None)
    nc = len((out.get("decision") or {}).get("checks") or [])
    items["checks"] = (str(nc), nc)
    labels = {c: t("sc_o_" + c, lang) for c in OVERVIEW_CODES}
    if lim is not None:
        labels["retention"] = t("sc_o_retention_est", lang)      # 20 % × (средства + резервы) — цифры временные
    return [{"code": c, "value": items[c][0], "raw": items[c][1], "label": labels[c]} for c in OVERVIEW_CODES]


def _risks(D: dict, out: dict, S: dict, lang: str) -> list:
    rk = (_scored_analytics(D, out, S).get("risks") or {})
    rows = []
    for i in rk.get("items") or []:
        rows.append({"code": i.get("code"), "name": i.get("name"), "share_pct": i.get("share_of_net_pct"),
                     "share_text": i.get("share_text") or pct(i.get("share_of_net_pct"), lang),
                     "level": i.get("level"), "level_label": i.get("level_label")
                     or tx.label(tx.PERIL_LEVEL_LABELS, i.get("level"), lang)})
    return rows


def _scenarios(out: dict, lang: str) -> list:
    sc = out.get("scenarios") or {}
    if not sc.get("available"):
        return []
    rows = []
    for code, name in (("pml", "PML"), ("eml", "EML"), ("mfl", "MFL")):
        it = sc.get(code) or {}
        if it.get("amount") is None:
            continue
        rows.append({"code": code, "name": name, "label": t("sc_o_" + code, lang), "amount": it["amount"],
                     "amount_text": money(it["amount"], lang), "pct": it.get("pct"),
                     "pct_text": it.get("pct_text") or tx.pct_fixed(it.get("pct"), lang, 0), "what": it.get("what")})
    return rows


def view(D: dict, out: dict, lang: str, meta: dict, borrower: Optional[dict] = None) -> dict:
    """Блок scoring: балл, класс, шкала, составляющие, шапка запроса, объект, общий обзор, риски, сценарии,
    проверки, заёмщик (кредитные продукты), тексты и адреса картинки и страницы."""
    from . import act as A
    S = ae.insurance_score(D)
    must = D["must"]
    NA = t("na", lang)
    created = datetime.fromisoformat(meta["created_at"])
    label = tx.label(tx.SCORE_BAND_LABELS, S["label_code"], lang)
    bands = [{"code": b["code"], "from": b["from"], "to": b["to"], "label_code": b["label_code"],
              "label": tx.label(tx.SCORE_BAND_LABELS, b["label_code"], lang)} for b in S["scale"]["bands"]]
    product = ((f"{must['product_code']} — {must['product_name']}" if lang == "ru" and must.get("product_name")
                else must["product_code"]) if must.get("product_code") else NA)
    cls = A._class_label(must.get("class_code"), must.get("class_name"), lang)
    insurer = D.get("insurer") or NA
    request = {"type": t("sc_type", lang), "number": meta["number"], "date": created.strftime("%d.%m.%Y"),
               "time": created.strftime("%H:%M"), "datetime": created.strftime("%d.%m.%Y %H:%M"),
               "insurer": insurer, "insurer_known": bool(D.get("insurer")), "product": product, "class": cls}
    request["rows"] = [{"code": "type", "label": t("sc_rq_type", lang), "value": request["type"]},
                       {"code": "number", "label": t("sc_rq_number", lang), "value": request["number"]},
                       {"code": "datetime", "label": t("sc_rq_datetime", lang), "value": request["datetime"]}]
    if request["insurer_known"]:                 # страховщик не задан — строки «Сформировал» нет вовсе
        request["rows"].append({"code": "by", "label": t("sc_rq_by", lang), "value": insurer})
    request["rows"] += [{"code": "product", "label": t("sc_rq_product", lang), "value": product},
                        {"code": "class", "label": t("sc_rq_class", lang), "value": cls}]
    if S["basis"] == "risk_score":
        text = t("sc_text_risk", lang, score=S["score"], code=S["class_code"], label=label,
                 risk=_n(S["risk_score_100"], lang), eq=_eq(S))
        method = t("sc_method_risk", lang, bands=ae.bands_text(S["scale"].get("bounds")))
    else:
        text = t("sc_text_level", lang, score=S["score"], code=S["class_code"], label=label,
                 level=tx.label(tx.LEVEL_LABELS, S.get("level") or "moderate", lang))
        method = t("sc_method_level", lang)
    if S.get("damage_penalty"):
        # повреждения с фото (06.10.2026): штраф балла — отдельной фразой, класс уже с его учётом
        text += " " + t("sc_text_dmg", lang, n=S.get("damage_count") or 0,
                        sev=t("dmg_sev_" + (S.get("damage_severity") or "major"), lang),
                        before=S.get("score_before_damages"), pen=S["damage_penalty"], score=S["score"])
    parts = []
    for p in S["parts"]:
        parts.append(dict(p, class_label=tx.label(tx.SCORE_BAND_LABELS, p["label_code"], lang),
                          text=t("sc_parts_line", lang, n=p["index"], cls=p["class_code"], score=p["score"],
                                 code=p["score_class"])))
    parts_note = t("sc_parts_note", lang, n=len(parts), k=S["worst_part"]) if parts else None
    if parts_note:
        text += " " + parts_note + "."
    checks = list((out.get("decision") or {}).get("checks") or [])
    brand = (D.get("scoring_style") or {}).get("brand_color") or DEFAULT_BRAND
    # рекомендация и уровень риска акта — рядом с классом: страница не должна читаться положительной при «отказать»
    dcode = (out.get("decision") or {}).get("code") or "accept"
    dcode = dcode if dcode in ("accept", "accept_with_clauses", "decline") else "accept_with_clauses"
    dtext = t("sc_dec_" + dcode, lang)
    decision = {"code": dcode, "text": dtext, "title": t("sc_dec_title", lang),
                "line": t("sc_line", lang, title=t("sc_dec_title", lang), v=dtext),
                "warning": t("sc_dec_see", lang) if dcode == "decline" else None}
    rk = out.get("risk") or {}
    lvl_code = rk.get("level") or (D.get("risk") or {}).get("level")
    lvl_label = rk.get("level_label") or tx.label(tx.LEVEL_LABELS, lvl_code or "moderate", lang)
    act_level = {"code": lvl_code, "label": lvl_label, "title": t("sc_act_level_title", lang),
                 "line": t("sc_line", lang, title=t("sc_act_level_title", lang), v=lvl_label)}
    an_level = None
    if S["basis"] == "risk_score":
        an_lab = ((_scored_analytics(D, out, S).get("score") or {}).get("level_label")
                  or (tx.label(tx.LEVEL5_LABELS, S.get("analytics_level"), lang) if S.get("analytics_level") else None))
        if an_lab:
            an_level = {"code": S.get("analytics_level"), "label": an_lab,
                        "text": t("sc_an_level", lang, cls=S["class"], level=an_lab)}
    return {
        "available": True, "lang": lang, "version": S["version"], "score": S["score"], "class": S["class"], "sub": S["sub"],
        "class_code": S["class_code"], "class_label": label, "label_code": S["label_code"],
        "risk_score_100": S["risk_score_100"], "basis": S["basis"], "basis_label":
            t("sc_risk100", lang) if S["basis"] == "risk_score" else t("sc_by_level", lang),
        "scale": {"min": S["scale"]["min"], "max": S["scale"]["max"], "bands": bands},
        "components": _components(S, lang),
        "parts": parts, "worst_part": S["worst_part"], "parts_note": parts_note,
        "request": request, "subject": _subject(D, out, lang),
        "overview": _overview(D, out, S, lang),
        "risks": _risks(D, out, S, lang), "scenarios": _scenarios(out, lang),
        "checks": checks[:5], "checks_total": len(checks),
        "checks_more": t("sc_checks_more", lang, n=len(checks) - 5) if len(checks) > 5 else None,
        "borrower": borrower,
        "decision": decision, "act_level": act_level, "analytics_level": an_level,
        # как получен балл одной строкой: «500 − 5 × 50,9 ≈ 246» («=», если без округления)
        "formula": (f"500 − 5 × {_n(S['risk_score_100'], lang)} {_eq(S)} {S['score']}"
                    if S["basis"] == "risk_score" else None),
        "text": text, "method_text": method, "note": t("sc_note", lang), "footer_line": t("sc_footer_line", lang),
        "gauge_caption": t("sc_caption", lang),
        "act_line": t("sc_png_act", lang, n=meta["number"], d=created.strftime("%d.%m.%Y")),
        "title": t("sc_title", lang),
        "titles": {k: t(k, lang) for k in ("sc_b1", "sc_b2", "sc_b3", "sc_b4", "sc_b5", "sc_b6", "sc_b_borrower",
                                           "sc_score", "sc_class", "sc_version", "sc_risk100", "sc_comp_title",
                                           "sc_r_risk", "sc_r_share", "sc_r_level", "sc_sc_name", "sc_sc_amount",
                                           "sc_sc_pct", "sc_type", "sc_dec_title", "sc_act_level_title")},
        "brand_color": brand, "calibrated": ae.CALIBRATED,
        "downloads": {"pdf": f"/act/{meta['id']}/scoring.pdf?lang={lang}",
                      "png": f"/act/{meta['id']}/scoring.png?lang={lang}"},
    }


# --------------------------------------------------------------------------- #
#  Рисование: шкала, плашка, страница
# --------------------------------------------------------------------------- #

class _Ink:
    """Текст поверх фигур: один TextWriter на цвет, запись — в конце (write)."""

    def __init__(self, page, regular, bold, fit):
        self.page, self.regular, self.bold, self.fit = page, regular, bold, fit
        self.writers = {}

    def font(self, bold):
        return self.bold if bold else self.regular

    def width(self, s, size, bold=False) -> float:
        return self.font(bold).text_length(self.fit(s, self.font(bold)), fontsize=size)

    def put(self, x, baseline, s, size, color=DARK, bold=False):
        f = self.font(bold)
        tw = self.writers.get(color)
        if tw is None:
            tw = self.writers[color] = pymupdf.TextWriter(self.page.rect)
        tw.append((x, baseline), self.fit(s, f), font=f, fontsize=size)

    def center(self, cx, baseline, s, size, color=DARK, bold=False):
        self.put(cx - self.width(s, size, bold) / 2, baseline, s, size, color, bold)

    def right(self, x1, baseline, s, size, color=DARK, bold=False):
        self.put(x1 - self.width(s, size, bold), baseline, s, size, color, bold)

    def rotated(self, cx, cy, s, size, angle_deg, color=DARK, bold=False):
        """Слово с центром в (cx, cy), повёрнутое на angle_deg (по часовой на экране)."""
        f = self.font(bold)
        tw = pymupdf.TextWriter(self.page.rect)
        s = self.fit(s, f)
        tw.append((cx - f.text_length(s, fontsize=size) / 2, cy + size * 0.35), s, font=f, fontsize=size)
        # Matrix(угол) в координатах страницы (y вниз) поворачивает против часовой — поэтому знак минус
        tw.write_text(self.page, color=color, morph=(pymupdf.Point(cx, cy), pymupdf.Matrix(-angle_deg)))

    def clip(self, s, size, width, bold=False) -> str:
        s = str(s)
        if self.width(s, size, bold) <= width:
            return s
        while s and self.width(s + "…", size, bold) > width:
            s = s[:-1]
        return s.rstrip() + "…"

    def wrap(self, s, size, width, bold=False, max_lines=2) -> list:
        words, lines, cur = str(s).split(), [], ""
        for w in words:
            cand = (cur + " " + w).strip()
            if self.width(cand, size, bold) <= width or not cur:
                cur = cand
            else:
                lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = self.clip(lines[-1] + " …", size, width, bold)
        return [self.clip(x, size, width, bold) for x in lines] or [""]

    def write(self):
        for color, tw in self.writers.items():
            tw.write_text(self.page, color=color)
        self.writers = {}


def _arc(cx, cy, r, a0, a1, n=24) -> list:
    """Точки дуги радиуса r от угла a0 до a1 (радианы, 0 — вправо, против часовой; y экрана — вниз)."""
    return [pymupdf.Point(cx + r * math.cos(a0 + (a1 - a0) * i / n), cy - r * math.sin(a0 + (a1 - a0) * i / n))
            for i in range(n + 1)]


def _angle(v: float) -> float:
    """Значение шкалы 0–500 → угол: 0 слева (π), 500 справа (0)."""
    return math.pi * (1 - max(0.0, min(500.0, float(v))) / 500.0)


def draw_gauge(page, rect, sc: dict, regular, bold, fit, brand=None):
    """Полукруглая шкала 0–500: пять цветных секторов с буквами, серое кольцо с подписями уровней, отметки
    0/100/…/500, стрелка на балл, балл в центре."""
    brand = brand or rgb(sc.get("brand_color"))
    ink = _Ink(page, regular, bold, fit)
    W, H = rect.width, rect.height
    R = min((W - 34) / 2, H - 20)
    cx, cy = rect.x0 + W / 2, rect.y1 - 12
    r_word_out, r_word_in = R, R - 15
    r_col_out, r_col_in = R - 16, R * 0.47
    shape = page.new_shape()
    # серое кольцо подписей
    pts = _arc(cx, cy, r_word_out, math.pi, 0, 60) + _arc(cx, cy, r_word_in, 0, math.pi, 60)
    shape.draw_polyline(pts + [pts[0]])
    shape.finish(color=None, fill=LIGHT, closePath=True)
    # цветные секторы
    for b in sc["scale"]["bands"]:
        a0, a1 = _angle(b["from"]), _angle(b["to"] + (1 if b["to"] < 500 else 0))
        pts = _arc(cx, cy, r_col_out, a0, a1) + _arc(cx, cy, r_col_in, a1, a0)
        shape.draw_polyline(pts + [pts[0]])
        shape.finish(color=WHITE, fill=BAND_RGB[b["code"]], width=1.2, closePath=True)
    # центр
    pts = _arc(cx, cy, r_col_in - 1.5, math.pi, 0, 48)
    shape.draw_polyline(pts + [pts[0]])
    shape.finish(color=LIGHT, fill=WHITE, width=0.8, closePath=True)
    # стрелка: клин от края центра до серого кольца
    a = _angle(sc["score"])
    ux, uy = math.cos(a), -math.sin(a)
    px, py = -uy, ux
    base_r, tip_r, half = r_col_in - 3, r_col_out - 19, 4.0
    b1 = pymupdf.Point(cx + ux * base_r + px * half, cy + uy * base_r + py * half)
    b2 = pymupdf.Point(cx + ux * base_r - px * half, cy + uy * base_r - py * half)
    tip = pymupdf.Point(cx + ux * tip_r, cy + uy * tip_r)
    shape.draw_polyline([b1, tip, b2, b1])
    shape.finish(color=WHITE, fill=(0.13, 0.15, 0.20), width=0.7, closePath=True)
    shape.draw_circle(pymupdf.Point(cx + ux * base_r, cy + uy * base_r), half)
    shape.finish(color=WHITE, fill=(0.13, 0.15, 0.20), width=0.7)
    # основание
    shape.draw_line(pymupdf.Point(cx - R, cy), pymupdf.Point(cx + R, cy))
    shape.finish(color=LIGHT, width=0.8)
    shape.commit()
    # буквы секторов и подписи уровней
    r_mid = r_col_out - 10                    # буквы — у внешнего края, стрелка до них не доходит
    for b in sc["scale"]["bands"]:
        am = (_angle(b["from"]) + _angle(b["to"] + (1 if b["to"] < 500 else 0))) / 2
        x, y = cx + r_mid * math.cos(am), cy - r_mid * math.sin(am)
        ink.center(x, y + 4.2, b["code"], 12, WHITE if b["code"] != "C" else DARK, True)
        rw = (r_word_out + r_word_in) / 2
        wx, wy = cx + rw * math.cos(am), cy - rw * math.sin(am)
        ink.rotated(wx, wy, b["label"], 6.2, 90 - math.degrees(am), (0.30, 0.32, 0.36), True)
    # отметки шкалы
    for v in range(0, 501, 100):
        av = _angle(v)
        if v == 0:
            ink.right(cx - R - 3, cy + 3, "0", 7.5, GRAY, True)
        elif v == 500:
            ink.put(cx + R + 3, cy + 3, "500", 7.5, GRAY, True)
        else:
            x, y = cx + (R + 9) * math.cos(av), cy - (R + 9) * math.sin(av)
            ink.center(x, y + 3, str(v), 7.5, GRAY, True)
    ink.center(cx, cy - 5, str(sc["score"]), max(12, min(26, r_col_in * 0.5)), brand, True)
    ink.write()


RED = (0.80, 0.10, 0.10)
# цвет рекомендации акта: принять — зелёный, с оговорками — оранжевый, отказать — красный
DEC_RGB = {"accept": (0.16, 0.50, 0.20), "accept_with_clauses": (0.80, 0.40, 0.04), "decline": RED}


def draw_plaque(page, rect, sc: dict, regular, bold, fit, brand=None):
    """Плашка класса: «A3» крупно и уровень словами (ОТЛИЧНЫЙ) на цвете сектора. Рекомендация акта «отказать» —
    плашка в красной рамке и перечёркнута: класс по шкале не должен читаться как положительное решение."""
    brand = brand or rgb(sc.get("brand_color"))
    fill = BAND_RGB.get(sc["class"], LIGHT)
    shape = page.new_shape()
    shape.draw_rect(rect, radius=0.18)
    shape.finish(color=brand, fill=fill, width=1.6)
    shape.commit()
    ink = _Ink(page, regular, bold, fit)
    big = min(34, rect.height * 0.5)
    cx = (rect.x0 + rect.x1) / 2
    ink.center(cx, rect.y0 + rect.height * 0.58, sc["class_code"], big, brand, True)
    lab = str(sc["class_label"]).upper()
    size = 9.5
    while size > 6 and ink.width(lab, size, True) > rect.width - 10:
        size -= 0.5
    ink.center(cx, rect.y0 + rect.height * 0.84, lab, size, WHITE if sc["class"] != "C" else DARK, True)
    ink.write()
    if (sc.get("decision") or {}).get("code") == "decline":
        shape = page.new_shape()
        shape.draw_rect(pymupdf.Rect(rect.x0 - 3, rect.y0 - 3, rect.x1 + 3, rect.y1 + 3))
        shape.finish(color=RED, width=2.2)
        shape.draw_line(pymupdf.Point(rect.x0 + 2, rect.y1 - 2), pymupdf.Point(rect.x1 - 2, rect.y0 + 2))
        shape.finish(color=RED, width=2.4)
        shape.commit()


def draw_decision(page, x0, x1, y, sc: dict, regular, bold, fit, size=6.8, ymax=None) -> float:
    """Под плашкой: пометка «см. рекомендацию акта: отказать», рекомендация и уровень риска акта, уровень по
    аналитике. Возвращает y после последней строки."""
    ink = _Ink(page, regular, bold, fit)
    dec, lvl, an = sc.get("decision") or {}, sc.get("act_level") or {}, sc.get("analytics_level") or {}
    rows = []
    if dec.get("warning"):
        rows.append((dec["warning"], RED, True))
    if dec.get("text"):
        rows += [(dec.get("title", "") + ":", GRAY, False), (dec["text"], DEC_RGB.get(dec.get("code"), DARK), True)]
    if lvl.get("label"):
        rows += [(lvl.get("title", "") + ":", GRAY, False), (lvl["label"], DARK, True)]
    if an.get("text"):
        rows.append((an["text"], GRAY, False))
    for s, col, b in rows:
        for ln in ink.wrap(s, size, x1 - x0, b, max_lines=4):
            if ymax is not None and y + size > ymax:
                break
            ink.put(x0, y + size, ln, size, col, b)
            y += size + 1.8
        y += 0.8
    ink.write()
    return y


def gauge_doc(sc: dict, regular, bold, fit):
    """Страница картинки шкалы: заголовок, страховщик (если задан), номер и дата акта, шкала, плашка, рекомендация и
    уровень риска акта, под шкалой — «экспертная шкала, не калибровано; не кредитный скоринг»."""
    brand = rgb(sc.get("brand_color"))
    W, H = 420, 236
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    ink = _Ink(page, regular, bold, fit)
    rq = sc.get("request") or {}
    title = (sc.get("titles") or {}).get("sc_type") or rq.get("type") or ""
    ink.put(8, 18, ink.clip(title, 12.5, W - 170 if rq.get("insurer_known") else W - 16, True), 12.5, brand, True)
    if rq.get("insurer_known"):
        ink.right(W - 8, 17, ink.clip(rq.get("insurer"), 8.5, 150, True), 8.5, GRAY, True)
    ink.put(8, 30, sc.get("act_line") or "", 8, GRAY)
    ink.write()
    page.draw_line((8, 35), (W - 8, 35), color=brand, width=1)
    draw_gauge(page, pymupdf.Rect(0, 42, 272, 192), sc, regular, bold, fit)
    draw_plaque(page, pymupdf.Rect(288, 46, 412, 110), sc, regular, bold, fit)
    draw_decision(page, 288, 412, 118, sc, regular, bold, fit, size=7, ymax=200)
    ink = _Ink(page, regular, bold, fit)
    y = 208
    for ln in ink.wrap(sc.get("gauge_caption") or "", 7.2, W - 16, True, max_lines=3):
        ink.put(8, y, ln, 7.2, GRAY, True)
        y += 9
    ink.write()
    return doc


def gauge_png(sc: dict, regular, bold, fit, zoom: float = 3.0) -> bytes:
    """Картинка шкалы (gauge_doc) — тем же кодом рисования, страница pymupdf → pixmap → PNG."""
    doc = gauge_doc(sc, regular, bold, fit)
    pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    data = pix.tobytes("png")
    doc.close()
    return data


def draw_page(page, sc: dict, regular, bold, fit, margin: float, bottom: float):
    """
    Страница «Страховой скоринг объекта» (A4): шапка запроса, блоки 1–6 на полосах цвета бренда, шкала и
    плашка с рекомендацией и уровнем риска акта; всё на одной странице. Что не помещается (составляющие, части,
    риски, проверки) — строкой «и ещё N — в акте», молча ничего не обрезается; полностью — в JSON и в акте.
    bottom — нижняя граница текста (над подвалом с номером страницы).
    """
    brand = rgb(sc.get("brand_color"))
    ink = _Ink(page, regular, bold, fit)
    lang = sc["lang"]
    W = page.rect.width
    x0, x1 = margin, W - margin
    cw = x1 - x0
    y = margin - 14
    # низ страницы: метод и строка о скоринге переносятся (без многоточия) — их высота задаёт предел для блоков
    meth = ink.wrap(sc["method_text"] + " " + sc["note"], 6.2, cw, max_lines=8)
    foot = ink.wrap(sc["footer_line"], 6.8, cw, True, max_lines=6)
    yfoot = bottom - 1 - 8.2 * (len(foot) - 1)            # базовая линия первой строки о скоринге
    ymeth = yfoot - 9.5 - 7.2 * (len(meth) - 1)           # базовая линия первой строки метода
    limit = ymeth - 6.2 - 8                               # отступ между последней проверкой и методом

    def band(yy, title, xa=x0, xb=x1):
        page.draw_rect(pymupdf.Rect(xa, yy, xb, yy + 13), color=None, fill=brand)
        ink.put(xa + 5, yy + 9.4, title, 7.8, WHITE, True)
        return yy + 17

    def more(n):
        return t("sc_parts_more", lang, n=n)

    # заголовок и шапка запроса
    rq = sc["request"]
    ink.put(x0, y + 13, sc["title"], 14, brand, True)
    if rq.get("insurer_known"):
        ink.right(x1, y + 12, ink.clip(rq["insurer"], 9, cw / 2), 9, GRAY, True)
    y += 20
    page.draw_line((x0, y), (x1, y), color=brand, width=1.2)
    y += 4
    rows = rq["rows"]
    half = cw / 2
    for i in range(0, len(rows), 2):
        for j, r in enumerate(rows[i:i + 2]):
            xx = x0 + j * half
            lw = ink.width(r["label"] + ": ", 7.3)
            ink.put(xx, y + 8, r["label"] + ":", 7.3, GRAY)
            ink.put(xx + lw + 2, y + 8, ink.clip(r["value"], 7.3, half - lw - 8), 7.3, DARK)
        y += 10
    y += 4

    # 1. Объект
    y = band(y, sc["titles"]["sc_b1"])
    sub = sc["subject"]["rows"]
    for i in range(0, len(sub), 2):
        hmax = 0
        for j, r in enumerate(sub[i:i + 2]):
            xx = x0 + j * half
            lw = 78
            ink.put(xx, y + 8, ink.clip(r["label"], 7.3, lw - 4), 7.3, GRAY)
            strong = r.get("code") == "name"                 # наименование объекта — жирным
            lines = ink.wrap(r["value"], 7.3, half - lw - 8, strong, max_lines=2)
            for k, ln in enumerate(lines):
                ink.put(xx + lw, y + 8 + k * 9, ln, 7.3, DARK, strong)
            hmax = max(hmax, len(lines))
        y += 9 * hmax + 2
    y += 3

    # 2. Скоринг: слева балл, класс (и уровень по аналитике), версия и составляющие; в центре шкала с подписью;
    # справа плашка, рекомендация и уровень риска акта, части договора
    H2 = 160
    y = band(y, sc["titles"]["sc_b2"])
    top = y
    lx, lw_ = x0, 140
    ly = top + 9
    kv = [(sc["titles"]["sc_score"], str(sc["score"]), None),
          (sc["titles"]["sc_class"], f"{sc['class_code']}, {sc['class_label']}",
           (sc.get("analytics_level") or {}).get("text")),
          (sc["titles"]["sc_version"], sc["version"], None),
          (sc["basis_label"], _n(sc["risk_score_100"], lang) if sc["risk_score_100"] is not None else "—",
           sc.get("formula"))]
    for lab, val, extra in kv:
        ink.put(lx, ly, ink.clip(lab.upper() + ":", 6.8, lw_ - 46), 6.8, brand, True)
        ink.right(lx + lw_, ly, ink.clip(val, 7.5, 60), 7.5, DARK, True)
        ly += 10.5
        if extra:
            for ln in ink.wrap(extra, 6.4, lw_, max_lines=2):
                ink.put(lx, ly - 2, ln, 6.4, GRAY)
                ly += 7.6
            ly += 1
    ly += 3
    ink.put(lx, ly, sc["titles"]["sc_comp_title"], 6.8, GRAY, True)
    ly += 9
    comps = sc["components"]
    for i, c in enumerate(comps):
        val = f"{c['points']} / {c['max']}" if c["applicable"] else "—"
        cl = ink.wrap(c["label"], 6.4, lw_ - 40, max_lines=2)
        h = 7.4 * len(cl) + 1.6
        left = len(comps) - i - 1
        if ly + h + (8 if left else 0) > top + H2 - 2:
            ink.put(lx, ly, more(len(comps) - i), 6.2, GRAY)
            break
        for k, ln in enumerate(cl):
            ink.put(lx, ly + k * 7.4, ln, 6.4, DARK)
        ink.right(lx + lw_, ly, val, 6.4, DARK, True)
        ly += h
    gx0 = x0 + lw_ + 8
    rcol = 114
    gw = x1 - gx0 - rcol - 8
    cap = ink.wrap(sc.get("gauge_caption") or "", 6.2, gw, True, max_lines=3)
    gy1 = top + H2 - 4 - 7.4 * len(cap)
    draw_gauge(page, pymupdf.Rect(gx0, top + 2, gx0 + gw, gy1), sc, regular, bold, fit, brand)
    for k, ln in enumerate(cap):                              # подпись под шкалой — внутри блока
        ink.center(gx0 + gw / 2, gy1 + 7 + k * 7.4, ln, 6.2, GRAY, True)
    px0 = x1 - rcol
    draw_plaque(page, pymupdf.Rect(px0 + 3, top + 6, x1 - 3, top + 64), sc, regular, bold, fit, brand)
    py = draw_decision(page, px0, x1, top + 70, sc, regular, bold, fit, size=6.6, ymax=top + H2)
    plines = ([sc["parts_note"]] if sc.get("parts_note") else []) + [p["text"] for p in sc.get("parts") or []]
    shown = 0
    for i, ln in enumerate(plines):
        wl = ink.wrap(ln, 6.2, x1 - px0, max_lines=3)
        left = len(plines) - i - 1
        if py + 7.5 * len(wl) + (7.5 if left else 0) > top + H2:
            break
        for l2 in wl:
            ink.put(px0, py + 6.2, l2, 6.2, GRAY)
            py += 7.5
        shown += 1
    if shown < len(plines):
        n_left = len([p for p in sc.get("parts") or []]) - max(0, shown - (1 if sc.get("parts_note") else 0))
        ink.put(px0, py + 6.2, more(n_left), 6.2, GRAY, True)
    y = top + H2 + 2

    # 3. Общий обзор: «число — подпись» в две колонки
    y = band(y, sc["titles"]["sc_b3"])
    ov = sc["overview"]
    nrows = (len(ov) + 1) // 2
    cols = [ov[:nrows], ov[nrows:]]
    vw = 92
    yy = [y, y]
    for j, col in enumerate(cols):
        xx = x0 + j * half
        for it in col:
            vlines = ink.wrap(it["value"], 7.3, vw, True, max_lines=2)
            for k, ln in enumerate(vlines):
                ink.right(xx + vw, yy[j] + 8 + k * 8.6, ln, 7.3, DARK, True)
            llines = ink.wrap(it["label"], 7.1, half - vw - 22, max_lines=2)
            ink.put(xx + vw + 5, yy[j] + 8, "–", 7.1, GRAY)
            for k, ln in enumerate(llines):
                ink.put(xx + vw + 13, yy[j] + 8 + k * 8.6, ln, 7.1, GRAY)
            yy[j] += 8.6 * max(len(vlines), len(llines)) + 1.6
    y = max(yy) + 3

    # 4. Риски и 5. Сценарии — рядом
    left_w = cw * 0.5 - 4
    yl = band(y, sc["titles"]["sc_b4"], x0, x0 + left_w)
    yr = band(y, sc["titles"]["sc_b5"], x0 + left_w + 8, x1)
    rx = x0 + left_w + 8
    rw = x1 - rx
    ink.put(x0, yl + 7, sc["titles"]["sc_r_risk"], 6.6, GRAY, True)
    ink.right(x0 + left_w - 62, yl + 7, sc["titles"]["sc_r_share"], 6.6, GRAY, True)
    ink.right(x0 + left_w, yl + 7, sc["titles"]["sc_r_level"], 6.6, GRAY, True)
    yl += 10
    risks = sc["risks"]
    if not risks:
        ink.put(x0, yl + 7, ink.clip(t("sc_r_none", lang), 7, left_w), 7, GRAY)
        yl += 10
    for i, r in enumerate(risks[:6]):
        ink.put(x0, yl + 7, ink.clip(r["name"] or r["code"], 7, left_w - 120), 7, DARK)
        ink.right(x0 + left_w - 62, yl + 7, r["share_text"], 7, DARK, True)
        ink.right(x0 + left_w, yl + 7, r["level_label"] or "", 7, DARK)
        page.draw_line((x0, yl + 9.5), (x0 + left_w, yl + 9.5), color=LIGHT, width=0.4)
        yl += 9.6
    if len(risks) > 6:
        ink.put(x0, yl + 7, more(len(risks) - 6), 6.6, GRAY)
        yl += 9
    ink.put(rx, yr + 7, sc["titles"]["sc_sc_name"], 6.6, GRAY, True)
    ink.right(rx + rw - 54, yr + 7, sc["titles"]["sc_sc_amount"], 6.6, GRAY, True)
    ink.right(rx + rw, yr + 7, sc["titles"]["sc_sc_pct"], 6.6, GRAY, True)
    yr += 10
    if not sc["scenarios"]:
        ink.put(rx, yr + 7, ink.clip(t("sc_sc_none", lang), 7, rw), 7, GRAY)
        yr += 10
    for s_ in sc["scenarios"]:
        ink.put(rx, yr + 7, s_["name"], 7, DARK, True)
        ink.right(rx + rw - 54, yr + 7, s_["amount_text"], 7, DARK, True)
        ink.right(rx + rw, yr + 7, s_["pct_text"] or "", 7, DARK)
        ink.put(rx, yr + 15, ink.clip(s_["what"] or s_["label"], 6.2, rw), 6.2, GRAY)
        page.draw_line((rx, yr + 18), (x1, yr + 18), color=LIGHT, width=0.4)
        yr += 19
    y = max(yl, yr) + 4

    # заёмщик (кредитное бюро) — для кредитных продуктов
    bw = sc.get("borrower")
    if bw and bw.get("rows"):
        y = band(y, sc["titles"]["sc_b_borrower"])
        brs = bw["rows"]
        n2 = (len(brs) + 1) // 2
        yy = [y, y]
        for j, col in enumerate((brs[:n2], brs[n2:])):
            xx = x0 + j * half
            for it in col:
                ink.right(xx + vw, yy[j] + 8, ink.clip(it["value"], 7.3, vw, True), 7.3, DARK, True)
                ink.put(xx + vw + 5, yy[j] + 8, "–  " + ink.clip(it["label"], 7.1, half - vw - 16), 7.1, GRAY)
                yy[j] += 10
        y = max(yy) + 3

    # 6. Что проверить андеррайтеру: что не поместилось — «и ещё N — в разделе 5 акта», всегда
    y = band(y, sc["titles"]["sc_b6"])
    items = list(sc["checks"] or [])
    total = int(sc.get("checks_total") or len(items))
    if not items:
        ink.put(x0 + 10, y + 7.5, t("sc_checks_none", lang), 7, GRAY)
        y += 10
    drawn = 0
    for c in items:
        lines = ink.wrap(c, 7, cw - 12, max_lines=3)
        h = 8.6 * len(lines) + 1.5
        if y + h + (9.5 if total - drawn - 1 > 0 else 0) > limit:
            break
        ink.put(x0 + 2, y + 7.5, "•", 7, DARK)
        for k, ln in enumerate(lines):
            ink.put(x0 + 10, y + 7.5 + k * 8.6, ln, 7, DARK)
        y += h
        drawn += 1
    if drawn < total:
        ink.put(x0 + 10, y + 7.5, t("sc_checks_more", lang, n=total - drawn), 6.8, GRAY)
        y += 9.5
    # внизу страницы: как считан балл и строка о скоринге — целиком, переносом
    for k, ln in enumerate(meth):
        ink.put(x0, ymeth + k * 7.2, ln, 6.2, GRAY)
    for k, ln in enumerate(foot):
        ink.put(x0, yfoot + k * 8.2, ln, 6.8, brand, True)
    ink.write()


# --------------------------------------------------------------------------- #
#  Word
# --------------------------------------------------------------------------- #

def docx_section(doc, sc: dict, png: bytes):
    """Первая секция Word: те же блоки, шкала — картинкой PNG (тем же кодом рисования)."""
    from .docx_lite import TEXT_WIDTH
    fill = str(sc.get("brand_color") or DEFAULT_BRAND).lstrip("#").upper()
    rq = sc["request"]
    if rq.get("insurer_known"):
        doc.para(rq["insurer"], bold=True, color="555555", size=18, align="right", after=40)
    doc.para(sc["title"], bold=True, color=fill, size=28, after=80)
    rows = rq["rows"]
    grid = [[rows[i]["label"], rows[i]["value"], rows[i + 1]["label"] if i + 1 < len(rows) else "",
             rows[i + 1]["value"] if i + 1 < len(rows) else ""] for i in range(0, len(rows), 2)]
    w4 = [1500, TEXT_WIDTH // 2 - 1500, 1500, TEXT_WIDTH - TEXT_WIDTH // 2 - 1500]
    doc.table(grid, widths=w4, header=False, size=16, borders=False, label_cols=(0, 2))
    doc.band(sc["titles"]["sc_b1"], fill)
    sub = sc["subject"]["rows"]
    grid = [[sub[i]["label"], sub[i]["value"], sub[i + 1]["label"] if i + 1 < len(sub) else "",
             sub[i + 1]["value"] if i + 1 < len(sub) else ""] for i in range(0, len(sub), 2)]
    doc.table(grid, widths=w4, header=False, size=17, borders=False, label_cols=(0, 2))
    doc.band(sc["titles"]["sc_b2"], fill)
    dec, lvl, an = sc.get("decision") or {}, sc.get("act_level") or {}, sc.get("analytics_level") or {}
    cls_v = f"{sc['class_code']}, {sc['class_label']}" + (f" ({an['text']})" if an.get("text") else "")
    grid = [[sc["titles"]["sc_score"], str(sc["score"])],
            [sc["titles"]["sc_class"], cls_v],
            [sc["titles"]["sc_version"], sc["version"]],
            [sc["basis_label"], _n(sc["risk_score_100"], sc["lang"]) if sc["risk_score_100"] is not None else "—"]]
    if dec.get("text"):
        grid.append([dec["title"], dec["text"] + (f" — {dec['warning']}" if dec.get("warning") else "")])
    if lvl.get("label"):
        grid.append([lvl["title"], lvl["label"]])
    doc.table(grid, widths=[3200, TEXT_WIDTH - 3200], header=False, size=18, borders=False, label_cols=(0,))
    doc.image(png, width_cm=15.0, descr=f"{sc['title']}: {sc['score']} — {sc['class_code']} {sc['class_label']}")
    if sc.get("gauge_caption"):
        doc.para(sc["gauge_caption"], size=15, color="555555", italic=True, after=40)
    doc.para(sc["text"], size=18, after=60)
    comp = [[sc["titles"]["sc_comp_title"], "", ""]] + [
        [c["label"], f"{c['points']} / {c['max']}" if c["applicable"] else "—", c["why"]] for c in sc["components"]]
    doc.table(comp, widths=[3300, 1300, TEXT_WIDTH - 4600], header=True, size=16)
    for p in sc.get("parts") or []:
        doc.bullet(p["text"], size=17)
    doc.band(sc["titles"]["sc_b3"], fill)
    ov = sc["overview"]
    n = (len(ov) + 1) // 2
    grid = []
    for i in range(n):
        a = ov[i]
        b = ov[n + i] if n + i < len(ov) else None
        grid.append([a["value"], a["label"], b["value"] if b else "", b["label"] if b else ""])
    doc.table(grid, widths=[2000, TEXT_WIDTH // 2 - 2000, 2000, TEXT_WIDTH - TEXT_WIDTH // 2 - 2000], header=False,
              size=16, borders=False, bold_cols=(0, 2))
    doc.band(sc["titles"]["sc_b4"], fill)
    if sc["risks"]:
        doc.table([[sc["titles"]["sc_r_risk"], sc["titles"]["sc_r_share"], sc["titles"]["sc_r_level"]]] +
                  [[r["name"] or r["code"], r["share_text"], r["level_label"] or ""] for r in sc["risks"]],
                  widths=[TEXT_WIDTH - 3600, 1600, 2000], header=True, size=17)
    else:
        doc.para(t("sc_r_none", sc["lang"]), size=17, color="555555")
    doc.band(sc["titles"]["sc_b5"], fill)
    if sc["scenarios"]:
        doc.table([[sc["titles"]["sc_sc_name"], sc["titles"]["sc_sc_amount"], sc["titles"]["sc_sc_pct"], ""]] +
                  [[s_["label"], s_["amount_text"], s_["pct_text"] or "", s_["what"] or ""] for s_ in sc["scenarios"]],
                  widths=[2800, 2200, 1000, TEXT_WIDTH - 6000], header=True, size=16)
    else:
        doc.para(t("sc_sc_none", sc["lang"]), size=17, color="555555")
    bw = sc.get("borrower")
    if bw and bw.get("rows"):
        doc.band(sc["titles"]["sc_b_borrower"], fill)
        doc.table([[r["value"], r["label"]] for r in bw["rows"]], widths=[3000, TEXT_WIDTH - 3000], header=False,
                  size=17, borders=False, bold_cols=(0,))
        if bw.get("note"):
            doc.para(bw["note"], size=15, color="555555", italic=True, after=40)
    doc.band(sc["titles"]["sc_b6"], fill)
    for c in sc["checks"] or [t("sc_checks_none", sc["lang"])]:
        doc.bullet(c, size=17)
    if sc.get("checks_more"):
        doc.para(sc["checks_more"], size=16, color="555555", italic=True)
    doc.para(sc["method_text"] + " " + sc["note"], size=15, color="555555", italic=True, after=40)
    doc.para(sc["footer_line"], size=16, color=fill, bold=True, italic=True)
    doc.page_break()
