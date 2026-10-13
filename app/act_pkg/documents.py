"""
Документы загрузки: отчёт кредитного бюро, запрос филиала, договор — очистка полей, вид для экрана,
сверка запроса с договором.
"""
from typing import Any, Optional

from .. import act_engine as ae
from .. import branch_request as br
from .. import contract_read as cr
from .. import credit_report as crr
from .. import act_texts as tx
from ..act_texts import money, pct, t

from .common import _empty_v
from .recognize import pd_like, _placeholders
from .view_fmt import _ddmmyyyy


# --------------------------------------------------------------------------- #
#  Отчёт кредитного бюро (КАТМ, 01.10.2026): очистка, вид для экрана, ввод, источник
# --------------------------------------------------------------------------- #

def cr_clean(f: Optional[dict]) -> tuple:
    """Поля отчёта бюро без значений, похожих на ПД: наименование и кредиторы — только юрлица (pd_like как у
    стороны договора); у физлица наименования и ИНН нет вовсе. (поля, сколько убрано)."""
    if not f:
        return f, 0
    f = {**f, "overview": dict(f.get("overview") or {}), "active": dict(f.get("active") or {})}
    n = 0
    if f.get("subject_type") != "legal":
        n += int(bool(f.get("name"))) + int(bool(f.get("inn")))
        f["name"], f["inn"] = None, None
    if f.get("name") and pd_like("policyholder", f["name"]):
        f["name"] = None
        n += 1
    keep = []
    for c in f["active"].get("creditors") or []:
        if pd_like("policyholder", c):
            n += 1
            continue
        keep.append(c)
    f["active"]["creditors"] = keep
    return f, n


def _cr_value(code: str, v, lang: str) -> str:
    """Значение поля отчёта бюро словами: суммы — сумами, даты — ДД.ММ.ГГГГ, тип субъекта — словом."""
    if v in (None, "", []):
        return t("na", lang)
    if code == "report_date":
        return _ddmmyyyy(v)
    if code == "subject_type":
        return tx.label(tx.CR_SUBJECT_LABELS, v, lang)
    if code.split(".")[-1] in crr.MONEY_KEYS + crr.ACTIVE_MONEY:
        return money(v, lang)
    if isinstance(v, list):
        return "; ".join(str(x) for x in v)
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v)


def credit_report_view(cbr: Optional[dict], lang: str) -> dict:
    """Блок credit_report ответа /act/photos: detected, source, fields{…}, rows (подпись — значение), notes."""
    if not cbr:
        return {"detected": False, "source": None, "source_label": None, "file": None, "fields": {}, "rows": [],
                "notes": [], "calibrated": ae.CALIBRATED}
    rows = [{"code": k, "label": tx.label(tx.CR_FIELD_LABELS, k, lang), "value": _cr_value(k, v, lang), "raw": v}
            for k, v in crr.flat(cbr["fields"]).items() if v not in (None, "", [])]
    notes = [t(c, lang) for c in cbr.get("notes") or []] + [t("cb_no_direct", lang), t("cb_not_in_rate", lang)]
    return {"detected": True, "source": cbr["source"], "source_label": tx.label(tx.CR_SOURCE_LABELS, cbr["source"], lang),
            "file": cbr.get("file"), "kind_label": tx.label(tx.DOC_KIND_LABELS, crr.KIND, lang),
            "fields": cbr["fields"], "rows": rows, "notes": notes, "calibrated": ae.CALIBRATED}


CR_SOURCES = ("document", "photo", "input", "session")


def validate_credit_report(raw: Any) -> tuple:
    """
    optional.credit_report (недоверенный ввод экрана; поля как в блоке credit_report.fields ответа /act/photos) →
    (чистый отчёт | None, ошибка | None). Поле source не доверяется: источник решает сервер по своей загрузке.
    """
    if raw in (None, "", {}):
        return None, None
    if not isinstance(raw, dict):
        return None, "объект {report_date, subject_type, score, score_class, overview{…}, active{…}}"
    body = raw.get("fields") if isinstance(raw.get("fields"), dict) else raw
    got, err = crr.normalize(body, strict=True)
    if err:
        return None, err
    got, _n = cr_clean(got)
    src = str(raw.get("source") or "input").strip()
    return {"fields": got, "claimed_source": src if src in CR_SOURCES else "input"}, None


def _trust_credit(sent: Optional[dict], block: Optional[dict], upload_missing: bool) -> tuple:
    """
    Отчёт бюро для акта и источник (как _trust_doc у запроса и договора): своя живая загрузка с отчётом есть —
    присланное сравнивается с сохранённым по полям: совпало — источник загрузки (document / photo), отличается —
    input и правка «было → стало». Загрузки нет — всё input, пометка «отчёт недоступен».
    Возвращает (поля | None, {source, source_kind, edits, field_sources, doc_missing}).
    """
    if sent is None:
        if not block or not block.get("fields"):
            return None, None
        fs = {k: block["source"] for k, v in crr.flat(block["fields"]).items() if v not in (None, "", [])}
        return dict(block["fields"]), {"source": block["source"], "source_kind": "document", "edits": [],
                                       "field_sources": fs, "doc_missing": False}
    fields = sent["fields"]
    now = crr.flat(fields)
    if not block or not block.get("fields"):
        fs = {k: "input" for k, v in now.items() if v not in (None, "", [])}
        return fields, {"source": "input", "source_kind": "input", "edits": [], "field_sources": fs,
                        "doc_missing": bool(upload_missing or sent.get("claimed_source") in ("document", "photo",
                                                                                              "session"))}
    was = crr.flat(block["fields"])
    edits, fs = [], {}
    for k in sorted(set(was) | set(now)):
        a, b = was.get(k), now.get(k)
        if _empty_v(a) and _empty_v(b):
            continue
        same = a == b or (isinstance(a, (int, float)) and isinstance(b, (int, float)) and abs(a - b) <= 0.005)
        if same:
            fs[k] = block["source"]
            continue
        fs[k] = "input"
        edits.append({"code": k, "was": a, "now": b})
    return fields, {"source": block["source"], "source_kind": "document_edited" if edits else "document",
                    "edits": edits, "field_sources": fs, "doc_missing": False}


def _borrower_block(con_settings: dict, fields: dict, trust: dict, classes: set) -> dict:
    """Блок borrower акта: поля отчёта бюро, источник, возраст отчёта и проверки андеррайтеру (только кредитные
    классы 14, 13з, 15). В уровень риска и ставку не входит — пока заказчик не утвердит правило."""
    credit = any(c in ae.CREDIT_REPORT_CLASSES for c in classes)
    checks = ae.borrower_checks(fields, credit, con_settings.get("credit_report"))
    return {"available": True, "fields": fields, "source": trust["source"], "source_kind": trust["source_kind"],
            "edits": trust["edits"], "field_sources": trust["field_sources"], "doc_missing": trust["doc_missing"],
            "credit_product": credit, "age_days": crr.age_days(fields), "checks": checks,
            "in_risk_level": False, "in_rate": False, "settings": dict(con_settings.get("credit_report") or {}),
            "calibrated": ae.CALIBRATED}


# текстовые поля бланка → ключ, по правилам которого они проверяются на ПД (pd_like)
BR_TEXT_KEYS = {"object_description": "object_type", "object_description_translated": "object_type",
                "additional_info": "additional_info", "contract_terms": "contract_terms",
                "osgor_class": "additional_info", "counterparty": "policyholder", "term_text": "additional_info"}


def br_clean(f: dict) -> tuple:
    """Поля бланка без значений, похожих на данные людей (как у распознанного): (поля, сколько убрано)."""
    f = dict(f)
    n = 0
    for key, rule in BR_TEXT_KEYS.items():
        if f.get(key) and pd_like(rule, f[key]):
            f[key] = None
            n += 1
    fr = f.get("franchise")
    if fr and fr.get("text") and pd_like("franchise", fr["text"]):
        f["franchise"] = dict(fr, text=None)
        n += 1
    for code in br.PARTY_CODES:
        p = f.get(code) or {}
        if p.get("name") and pd_like(code, p["name"]):
            f[code] = {"kind": "individual", "name": None}
            n += 1
    return f, n


def branch_view(brq: Optional[dict], lang: str) -> Optional[dict]:
    """Блок «запрос филиала» для экрана: 16 строк бланка (найдена, заполнена), разобранные поля и готовый
    optional.request для /act/make. Стороны: название — только у юрлица, у гражданина — kind = individual."""
    if not brq:
        return None
    f = brq["fields"]
    notes = []
    for code in br.PARTY_CODES:
        if (f.get(code) or {}).get("kind") == "individual":
            notes.append(t("br_individual", lang, role=tx.label(tx.BR_ROW_LABELS, code, lang).lower()))
    if f.get("term_error"):
        notes.append(t("br_term_unread", lang))
    if f.get("tariff_pct") is None:
        notes.append(t("br_no_tariff", lang))
    if f.get("amount_errors"):
        notes.append(t("br_negative", lang, what=", ".join(tx.label(tx.BR_ROW_LABELS, k, lang).lower()
                                                           for k in f["amount_errors"])))
    return {"detected": True, "kind_label": tx.label(tx.DOC_KIND_LABELS, br.KIND, lang),
            "source": brq.get("source"), "file": brq.get("file"),
            "rows_found": brq.get("rows_found"), "rows_total": len(br.ROW_CODES),
            "rows": [dict(r, label=tx.label(tx.BR_ROW_LABELS, r["code"], lang)) for r in brq.get("rows") or []],
            "fields": f, "request": br.request_of(f, brq.get("source") or "document"),
            "notes": notes, "check_label": t("prefill_check", lang)}


# текстовые поля договора → ключ, по правилам которого они проверяются на ПД (pd_like)
CT_TEXT_KEYS = {"object_description": "object_type", "address": "additional_info", "construction": "construction",
                "purpose": "additional_info", "territory": "additional_info", "product_name": "additional_info",
                "place": "additional_info", "notice": "additional_info", "term_text": "additional_info",
                "contract_no": "additional_info", "insured_event": "additional_info",
                "cover_period": "additional_info", "payment_text": "additional_info",
                "brand": "brand", "model": "model", "vin": "serial_no",
                "serial_no": "serial_no", "engine_no": "engine_no", "cadastre_no": "cadastre_no"}


def ct_clean(f: dict) -> tuple:
    """Поля договора без значений, похожих на данные людей, и без меток маскировки «[ФИО]»: (поля, сколько убрано)."""
    f = dict(f)
    n = 0

    def bad(rule, v):
        return bool(_placeholders(str(v))) or pd_like(rule, str(v))

    for key, rule in CT_TEXT_KEYS.items():
        if f.get(key) and bad(rule, f[key]):
            f[key] = None
            n += 1
    fr = f.get("franchise")
    if fr:
        for k in ("text", "risk"):
            if fr.get(k) and bad("franchise", fr[k]):
                fr = dict(fr, **{k: None})
                n += 1
        f["franchise"] = fr
    for code in cr.PARTY_CODES:
        p = f.get(code) or {}
        if p.get("name") and (bad("policyholder", p["name"]) or not br.is_legal(p["name"])):
            f[code] = {"kind": "individual", "name": None}
            n += 1
    for key in ("covered_risks", "exclusions"):
        keep = []
        for x in f.get(key) or []:
            if x.get("text") and bad("additional_info", x["text"]):
                n += 1
                continue
            keep.append(x)
        f[key] = keep
    terms = [x for x in f.get("special_terms") or [] if not bad("additional_info", x)]
    n += len(f.get("special_terms") or []) - len(terms)
    f["special_terms"] = terms
    items = [x for x in f.get("items") or [] if not bad("object_type", x.get("name"))]
    n += len(f.get("items") or []) - len(items)
    f["items"] = items
    sch = f.get("schedule")
    if sch:
        rows = []
        for x in sch.get("items") or []:
            if x.get("profession") and bad("additional_info", x["profession"]):
                x = {k: v for k, v in x.items() if k != "profession"}
                n += 1
            rows.append(x)
        f["schedule"] = dict(sch, items=rows)
    f["has_beneficiary"] = (f.get("beneficiary") or {}).get("kind") is not None
    f["has_pledger"] = (f.get("pledger") or {}).get("kind") is not None
    return f, n


# служебные поля разбора: в перечень «прочитано моделью» не входят
_AI_SKIP = ("class_hint", "object_kind", "term_text", "term_inclusive", "term_error", "has_beneficiary",
            "has_pledger", "items_total", "liability_from")


def _ai_field_labels(keys, lang: str) -> list:
    """Поля, дочитанные моделью, — подписями на языке ответа (срок с, по и дни — одной подписью «срок»)."""
    out = []
    for k in keys:
        if k in _AI_SKIP:
            continue
        code = "term" if k in ("term_from", "term_to", "term_days") else k
        lab = None
        for group in (tx.EDIT_LABELS, tx.CT_FIELD_LABELS, tx.FIELD_LABELS):
            if code in group:
                lab = tx.label(group, code, lang)
                break
        if lab and lab.lower() not in out:
            out.append(lab.lower())
    return out


def _labels(group: dict, codes, lang: str) -> list:
    return [{"code": c, "label": tx.label(group, c, lang)} for c in codes]


def _risk_view(xs: list, group: dict, lang: str) -> list:
    return [{"code": x["code"], "label": x.get("text") if x["code"] == "other" else tx.label(group, x["code"], lang)}
            for x in xs or []]


def contract_view(ctr: Optional[dict], lang: str) -> Optional[dict]:
    """Блок «договор страхования» для экрана: поля договора, какие ключевые поля найдены и каких нет,
    существенные условия (ГК РУз, ст. 929), заметки и готовый optional.contract для /act/make.
    Стороны: название — только у юрлица, у гражданина — kind = individual."""
    if not ctr:
        return None
    f = ctr["fields"]
    if f.get("is_template"):
        return _template_view(ctr, lang)
    notes = []
    for code in cr.PARTY_CODES:
        if (f.get(code) or {}).get("kind") == "individual":
            notes.append(t("br_individual", lang, role=tx.label(tx.FIELD_LABELS, code, lang).lower()))
    if ctr.get("truncated"):
        notes.append(t("ct_truncated", lang))
    if not f.get("term_days"):
        notes.append(t("ct_term_unread", lang))
    if f.get("tariff_pct") is None:
        notes.append(t("ct_no_tariff", lang))
    if f.get("currency") not in (None, "UZS"):
        notes.append(t("ct_currency", lang, cur=f["currency"]))
    missing = ctr.get("missing") or []
    if missing:
        notes.append(t("ct_missing", lang, what=", ".join(tx.label(tx.CT_FIELD_LABELS, k, lang).lower()
                                                           for k in missing)))
    ess = ctr.get("essentials") or cr.essentials(f)
    lack = [e["code"] for e in ess if not e["present"]]
    if lack:
        notes.append(t("ct_missing_essential", lang, what=", ".join(tx.label(tx.CT_ESSENTIAL_LABELS, c, lang)
                                                                     for c in lack)))
    srcs = ctr.get("field_sources") or {}
    ai_labels = _ai_field_labels(srcs, lang)
    if ai_labels:
        notes.append(t("ct_ai_filled", lang, what=", ".join(ai_labels)))
    return {"detected": True, "kind_label": tx.label(tx.DOC_KIND_LABELS, cr.KIND, lang),
            "source": ctr.get("source"), "source_label": tx.label(tx.CT_SOURCE_LABELS, ctr.get("source"), lang),
            "file": ctr.get("file"), "pages": ctr.get("pages"), "truncated": bool(ctr.get("truncated")),
            "fields": f, "field_sources": srcs, "ai_fields": sorted(srcs),
            "found": _labels(tx.CT_FIELD_LABELS, ctr.get("found") or [], lang),
            "missing": _labels(tx.CT_FIELD_LABELS, missing, lang),
            "essentials": [{"code": e["code"], "label": tx.label(tx.CT_ESSENTIAL_LABELS, e["code"], lang),
                            "present": e["present"]} for e in ess],
            "legal_ref": tx.label(tx.LEGAL_REFS, ae.ESSENTIAL_REF, lang),
            "covered_risks": _risk_view(f.get("covered_risks"), tx.RISK_LABELS, lang),
            "exclusions": _risk_view(f.get("exclusions"), tx.EXCLUSION_LABELS, lang),
            "payment_mode_label": tx.label(tx.PAYMENT_MODE_LABELS, f["payment_mode"], lang)
            if f.get("payment_mode") else None,
            "request": cr.request_of(f, ctr.get("source") or "document"),
            "is_template": False, "template_hint": bool(f.get("template_hint")),
            "blank": _labels(tx.CT_BLANK_LABELS, f.get("blank") or [], lang), "blank_label": t("ct_blank", lang),
            "schedule": _schedule_view(f.get("schedule"), lang),
            **conditions_view(f, srcs, lang),
            "notes": notes, "check_label": t("prefill_check", lang)}


def _cond_label(code: str, lang: str) -> str:
    for group in (tx.CT_FIELD_LABELS, tx.FIELD_LABELS, tx.EDIT_LABELS):
        if code in group:
            return tx.label(group, code, lang)
    return code


def conditions_view(f: dict, srcs: Optional[dict], lang: str) -> dict:
    """
    Условия договора для полей осмотра (ТЗ 13.10.2026, день 7.1): conditions — [{field, label, value, source,
    source_label}] с пометкой «из договора» (дочитанное моделью — ещё «прочитано моделью»); sources — {поле: contract}.
    ПД не попадают: стороны — только название юрлица (cr.conditions), значения уже очищены ct_clean.
    """
    mark = t("from_contract", lang)
    items = []
    for c in cr.conditions(f):
        by_ai = (srcs or {}).get(cr.item_field(c["field"])) == "document_ai" or (
            c["field"] in ("term_from", "term_to", "term_days") and (srcs or {}).get(c["field"]) == "document_ai")
        items.append({"field": c["field"], "label": _cond_label(c["field"], lang), "value": c["value"],
                      "source": "contract", "by_model": by_ai,
                      "source_label": mark + (" (" + t("by_model_mark", lang) + ")" if by_ai else "")})
    return {"conditions": items, "sources": {c["field"]: "contract" for c in items}, "source_mark": mark}


def _schedule_view(sch: Optional[dict], lang: str) -> Optional[dict]:
    """Таблица застрахованных по профессиям (приложение к договору личного страхования): колонки подписями."""
    if not sch:
        return None
    return {"columns": _labels(tx.CT_SCHED_LABELS, sch.get("columns") or [], lang), "items": sch.get("items") or [],
            "blank": bool(sch.get("blank"))}


def _template_view(ctr: dict, lang: str) -> dict:
    """
    Бланк договора (поля — подчёркивания): что в нём есть (вид страхования, страховщик, страховой случай,
    исключения, порядок оплаты, таблица приложения) и какие поля не заполнены. Существенные условия
    (ГК ст. 929) и сверка с расчётом не выполняются — они проверяются по заполненному договору; готового
    optional.contract нет (request = None).
    """
    f = ctr["fields"]
    blank = f.get("blank") or []
    what = ", ".join(tx.label(tx.CT_BLANK_LABELS, k, lang).lower() for k in blank)
    notes = [t("ct_template", lang, what=what)]
    if f.get("template_hint"):
        notes.append(t("ct_template_form", lang))
    if ctr.get("truncated"):
        notes.append(t("ct_truncated", lang))
    return {"detected": True, "kind_label": tx.label(tx.DOC_KIND_LABELS, cr.KIND, lang),
            "is_template": True, "template_hint": bool(f.get("template_hint")),
            "source": ctr.get("source"), "source_label": tx.label(tx.CT_SOURCE_LABELS, ctr.get("source"), lang),
            "file": ctr.get("file"), "pages": ctr.get("pages"), "truncated": bool(ctr.get("truncated")),
            "fields": f, "field_sources": {}, "ai_fields": [],
            "found": _labels(tx.CT_FIELD_LABELS, ctr.get("found") or [], lang),
            "missing": _labels(tx.CT_FIELD_LABELS, ctr.get("missing") or [], lang),
            "blank": _labels(tx.CT_BLANK_LABELS, blank, lang), "blank_label": t("ct_blank", lang),
            "essentials": [], "legal_ref": tx.label(tx.LEGAL_REFS, ae.ESSENTIAL_REF, lang),
            "covered_risks": _risk_view(f.get("covered_risks"), tx.RISK_LABELS, lang),
            "exclusions": _risk_view(f.get("exclusions"), tx.EXCLUSION_LABELS, lang),
            "payment_mode_label": tx.label(tx.PAYMENT_MODE_LABELS, f["payment_mode"], lang)
            if f.get("payment_mode") else None,
            "schedule": _schedule_view(f.get("schedule"), lang),
            "request": None, "notes": notes, "check_label": t("prefill_check", lang)}


def _x_value(code: str, v, lang: str, by: Optional[str] = None) -> str:
    NA = t("na", lang)
    if v in (None, "", []):
        return NA
    if code in ("sum_insured", "object_value", "premium"):
        return money(v, lang)
    if code == "tariff_pct":
        return pct(v, lang)
    if code == "term":
        return t("x_term", lang, days=v.get("days"), date_from=_ddmmyyyy(v.get("from")) if v.get("from") else NA,
                 date_to=_ddmmyyyy(v.get("to")) if v.get("to") else NA)
    if code == "franchise":
        if not v.get("applied"):
            return t("x_fr_none", lang)
        if v.get("pct") is not None:
            return pct(v["pct"], lang)
        return money(v["amount"], lang) if v.get("amount") else (v.get("text") or NA)
    if code == "object":
        kind = tx.label(tx.X_KIND_LABELS, v["class_hint"], lang) if v.get("class_hint") else None
        if by == "kind":
            return kind or NA
        if by == "cadastre":
            return str(v.get("cadastre_no") or NA)
        parts = [str(v["cadastre_no"])] if v.get("cadastre_no") else []
        return ", ".join(parts + ([kind] if kind else [])) or NA
    return str(v)


def cross_view(xc: Optional[dict], lang: str) -> Optional[dict]:
    """Запрос филиала против договора: строки «совпадает / расходится» и итог (null — нет одного из документов)."""
    if not xc or not xc.get("available"):
        return None
    items, lines = [], []
    for it in xc["items"]:
        label = tx.label(tx.X_LABELS, it["code"], lang)
        by = it.get("compared")
        req_v, ct_v = _x_value(it["code"], it["request"], lang, by), _x_value(it["code"], it["contract"], lang, by)
        # объект: сравнить нечем (кадастр только в одном документе, вида нет) — так и пишем, это не расхождение
        na_obj = it["code"] == "object" and it["verdict"] == "missing" and (it["request"] or it["contract"])
        text = t("x_object_na", lang, label=label, req=req_v, ct=ct_v) if na_obj else             t("x_line", lang, label=label, verdict=t("x_v_" + it["verdict"], lang), req=req_v, ct=ct_v)
        items.append({"code": it["code"], "label": label, "request": it["request"], "contract": it["contract"],
                      "verdict": it["verdict"], "verdict_label": t("x_v_" + it["verdict"], lang), "text": text,
                      "compared": by})
        if it["verdict"] != "missing" or na_obj:
            lines.append(text)
    n = xc.get("differs", 0)
    verdict = "differs" if n else ("ok" if any(i["verdict"] == "same" for i in xc["items"]) else "missing")
    summary = t("x_summary_" + verdict, lang, n=n) if verdict == "differs" else t("x_summary_" + verdict, lang)
    return {"available": True, "items": items, "differs": n, "missing": xc.get("missing", 0),
            "summary": {"verdict": verdict, "text": summary}, "lines": [summary] + lines}
