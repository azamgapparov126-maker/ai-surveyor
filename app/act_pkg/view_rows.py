"""Текст акта: подписи и строки раздела 1, тексты проверок и расхождений, подписи частей."""
from typing import Optional

from .. import act_engine as ae
from .. import act_extras as ax
from .. import class_templates as ctpl
from .. import act_market as am
from .. import act_texts as tx
from .. import i18n
from .. import min_rates as mrs
from ..act_texts import money, pct, t

from .recognize import preferred
from .view_fmt import _CYR, _ddmmyyyy, _lower_first, money_k, _mult, ROWS_BY_GROUP, _signed, _spct


# --------------------------------------------------------------------------- #
#  Сборка акта: текст на нужном языке
# --------------------------------------------------------------------------- #

def _and(lang: str) -> str:
    return {"ru": " и ", "uz": " va ", "en": " and "}[tx.lang_of(lang)]


def _class_label(code: Optional[str], name_ru: Optional[str], lang: str) -> str:
    if not code:
        return t("na", lang)
    name = name_ru if lang == "ru" else i18n.t(f"tg.wz.cls.{code}", lang)
    if not name or name.startswith("tg.wz"):
        name = name_ru or ""
    return f"{code} — {name}" if name else code


def _fmt_param(key: str, v, lang: str):
    if key == "min_src":                    # источник минимальной ставки (app/min_rates.py) на языке акта
        return mrs.source_label(v or {}, lang) or t("na", lang)
    if key in ("base", "rate", "min", "calc"):
        return pct(v, lang)
    if key == "adj":
        return pct(v, lang)
    if key in ("sum", "premium", "diff", "price"):
        return money(v, lang)
    if key == "level":
        return tx.label(tx.LEVEL_LABELS, v, lang)
    if key == "place":
        return tx.label(tx.LOCATION_LABELS, v, lang)
    if key == "otype":
        return _otype_label(v, lang)
    if key == "spct":                       # поправка со знаком: +15 % / −3,2 % (вилка ставки)
        return _spct(v, lang, 2)
    if key == "fmult":                      # множитель факторов объекта: 1,6905
        return _mult(v, lang)
    return v


def _otype_label(otype: str, lang: str) -> str:
    """Тип объекта справочника (по-русски) → подпись на языке акта через OBJECT_KINDS, затем OTYPE_LABELS
    (типы базовых ставок и умолчания risk_analytics, у которых нет вида объекта на экране)."""
    for _code, (ref_type, labels) in tx.OBJECT_KINDS.items():
        if ref_type and ref_type == otype:
            return labels.get(lang) or labels["ru"]
    if otype in tx.OTYPE_LABELS:
        return tx.label(tx.OTYPE_LABELS, otype, lang)
    return otype


def _sc_part_local(p: dict, lang: str) -> dict:
    """Слагаемое сценария аналитики: формула простого правила шаблона в снимке — по-русски; на узбекском и
    английском её заменяет строка «как посчитано» (formula = None), подпись what — на языке акта (what_text)."""
    out = dict(p)
    if isinstance(p.get("what"), dict):
        out["what_text"] = p["what"].get(lang) or p["what"].get("ru")
    if tx.lang_of(lang) != "ru" and p.get("formula") and _CYR.search(str(p["formula"])):
        out["formula"] = None
    return out


def _text(item: dict, lang: str) -> str:
    params = {k: _fmt_param(k, v, lang) for k, v in (item.get("params") or {}).items()}
    return t(item["code"], lang, **params)


def _check_text(c: dict, lang: str, group: Optional[str] = None, veh_group: Optional[str] = None) -> str:
    p = c.get("params") or {}
    if c["code"] == "c_disc":
        return t("c_disc", lang, label=tx.field_label(p["key"], lang, group))
    if c["code"] == "c_views":
        return t("c_views", lang, views=", ".join(tx.view_label(v, lang, veh_group) for v in p["views"]))
    if c["code"] == "c_missing":
        return t("c_missing", lang, what=", ".join(tx.field_label(k, lang, group).lower() for k in p["keys"]))
    if c["code"].startswith("c_market_"):
        return am.check_text(c, lang) or t(c["code"], lang)
    if c["code"].startswith("c_rq_"):
        return t(c["code"], lang, **_rq_params(c["code"][2:], p, lang))
    if c["code"] == "c_ct_essentials":
        return t(c["code"], lang, what=", ".join(tx.label(tx.CT_ESSENTIAL_LABELS, x, lang) for x in p.get("missing") or []))
    if c["code"].startswith("c_ct_"):
        return t(c["code"], lang, **_rq_params(c["code"][2:], p, lang))
    if c["code"] == "c_x":
        return t("c_x", lang, what=", ".join(tx.label(tx.X_LABELS, x, lang).lower() for x in p.get("codes") or []))
    if c["code"] in ("c_tpl_credit_over", "c_part_credit_over", "c_credit_need_data", "c_part_credit_need_data"):
        return t(c["code"], lang, n=p.get("n"), cls=p.get("cls"), share=p.get("share_pct", 50),
                 **{k: money(p.get(k), lang) for k in ("sum", "insurable", "excess", "credit", "collateral")})
    if c["code"] == "c_below_min":
        return t("c_below_min", lang, req=pct(p.get("req"), lang), min=pct(p.get("min"), lang),
                 verdict=t("bm_v_" + str(p.get("verdict")), lang))
    if c["code"] == "c_tpl_crop_over":
        return t(c["code"], lang, **{k: money(p.get(k), lang) for k in ("sum", "value")})
    if c["code"] in ("c_credit_holder_not_bank", "c_part_credit_holder_not_bank"):
        who = p.get("holder") or t("credit_holder_individual" if p.get("individual") else "credit_holder_noname",
                                   lang)
        return t(c["code"], lang, n=p.get("n"), cls=p.get("cls"), holder=who,
                 where=t("credit_src_" + str(p.get("source") or "contract"), lang))
    if c["code"] == "c_borrower_overdue":
        return t(c["code"], lang, amount=money(p.get("amount"), lang))
    if c["code"] == "c_borrower_stale":
        if p.get("days") is None:
            return t("c_borrower_stale_nodate", lang)
        return t(c["code"], lang, days=p["days"], max=p.get("max"))
    if c["code"] == "c_borrower_low_class":
        return t(c["code"], lang, cls=p.get("cls"), low=p.get("low"))
    if c["code"] == "c_part_missing":
        what = ", ".join(_lower_first(x.get(lang) or x.get("ru") or "") for x in p.get("labels") or []) \
            or ", ".join(p.get("codes") or [])
        return t(c["code"], lang, n=p.get("n"), cls=p.get("cls"), what=what)
    if c["code"] == "c_parts_confirm":
        return t(c["code"], lang, source=t("pt_src_" + str(p.get("source") or "default"), lang))
    if c["code"].startswith("c_part_"):
        return t(c["code"], lang, n=p.get("n"), cls=p.get("cls"))
    if c["code"].startswith("c_obj_"):
        return t(c["code"], lang, n=p.get("n"), label=p.get("label") or "", req=pct(p.get("req"), lang),
                 min=pct(p.get("min"), lang))
    return t(c["code"], lang)


def _disc_text(d: dict, lang: str, group: Optional[str] = None) -> str:
    parts = []
    for g in d["values"]:
        where = _and(lang).join(tx.label(tx.SOURCE_IN, s, lang) for s in g["sources"])
        parts.append(f"{where} {g['value']}")
    return t("disc_line", lang, label=tx.field_label(d["key"], lang, group), values=", ".join(parts))


def _row(label: str, value, note=None) -> dict:
    return {"label": label, "value": value, "note": note}
EXTRA_IF_PRESENT = {"equipment": ["brand", "engine_power", "dimensions"], "property": ["reg_no"]}
MAX_NA_ROWS = 3
_GROUP_LABELS = {"equipment": {"object_type": "lbl_equipment_name", "location": "lbl_install_place"},
                 "property": {"object_type": "lbl_building_kind", "location": "lbl_address", "floors": "lbl_floors"}}


def _s1_label(key: str, lang: str, group: str) -> str:
    code = (_GROUP_LABELS.get(group) or {}).get(key)
    return t(code, lang) if code else tx.field_label(key, lang, group)


def _kind_label(D: dict, lang: str) -> Optional[str]:
    """Вид объекта на языке акта по словарю: вид (склад, оборудование…) + уточнение (склад-холодильник) +
    деятельность по описанию (пищевое производство). Перевод текста документа это не заменяет."""
    kind = D.get("object_kind")
    doc = D.get("object_doc") or {}
    an = D.get("analytics") or {}
    if not kind:
        return None
    tkinds = ctpl.kind_labels((D.get("template") or {}).get("object") and {"object": D["template"]["object"]})
    if kind == "warehouse" and ax.cold_store(" ".join(x for x in (doc.get("original"), doc.get("translated")) if x)):
        base = tx.label(tx.OBJECT_SUBKINDS, "cold_store", lang)
    elif kind not in tx.OBJECT_KINDS and kind in tkinds:
        base = tkinds[kind].get(lang) or tkinds[kind].get("ru")      # вид объекта из шаблона класса
    else:
        base = tx.label({k: v[1] for k, v in tx.OBJECT_KINDS.items()}, kind, lang)
    if an.get("activity_source") == "text" and an.get("activity") and kind in ("equipment", "production", "other"):
        base += " — " + tx.label(tx.OPTION_LABELS, "activity:" + an["activity"], lang)
    return base


def _object_type_row(D: dict, lang: str, label: str, best: Optional[dict]) -> Optional[dict]:
    """
    Описание объекта из документа на языке акта: перевод модели (скан) → вид объекта по словарю, исходный текст
    в примечании → исходный текст с пометкой «текст документа». Текст на языке акта показывается как есть.
    """
    doc = D.get("object_doc") or {}
    orig = doc.get("original")
    if not orig or not doc.get("from_document") or (best and best.get("value") != orig):
        return None
    src_lang = doc.get("original_lang")
    if src_lang == lang or (src_lang is None):
        return None
    cut = orig.strip(" «»\"'“”")
    cut = cut if len(cut) <= 200 else cut[:199] + "…"
    if doc.get("translated") and doc.get("translated_lang") == lang:
        return _row(label, doc["translated"], t("s1_by_model", lang) + "; " + t("s1_doc_text", lang, text=cut))
    kl = _kind_label(D, lang)
    if kl:
        return _row(label, kl, t("s1_by_dict", lang) + "; " + t("s1_doc_text", lang, text=cut))
    return _row(label, orig, t("s1_doc_text_mark", lang) + ", " + t("check_mark", lang))


def _doc_kind_label(kind: str, lang: str) -> str:
    """
    Вид документа на языке акта. Загрузка сохраняет подпись на своём языке («запрос филиала», «filial soʻrovi»)
    или код (branch_request) — ищем его в act_texts.DOC_KIND_LABELS по коду и по подписи на любом языке;
    не нашли (вид назвала модель своими словами) — как есть.
    """
    s = str(kind or "").strip()
    low = s.lower()
    for code, by_lang in tx.DOC_KIND_LABELS.items():
        if low == code.lower() or any(low == str(v).lower() for v in by_lang.values()):
            return tx.label(tx.DOC_KIND_LABELS, code, lang)
    return s


def _default_otype(D: dict) -> Optional[str]:
    """Тип объекта, принятый по умолчанию для расчёта аналитики (risk_analytics), или None."""
    an = D.get("analytics") or {}
    if an.get("object_type_source") == "default" and an.get("object_type"):
        return an["object_type"]
    return None


def _object_rows(D: dict, lang: str) -> list:
    group = D["group"]
    opt, rec = D["optional"], D["recognized"]
    kind = D.get("object_kind")
    NA = t("na", lang)
    keys = ROWS_BY_GROUP.get(group) or ae.FIELDS_BY_GROUP.get(group, [])
    keys = list(keys) + [k for k in EXTRA_IF_PRESENT.get(group, []) if preferred(rec, k)]
    key_first = ae.KEY_FIELDS.get(group, [])
    rows, missing = [], []
    for key in keys:
        label = _s1_label(key, lang, group)
        if key == "location" and opt.get("location"):
            rows.append(_row(label, tx.label(tx.LOCATION_LABELS, opt["location"], lang),
                             tx.label(tx.SOURCE_LABELS, "input", lang)))
            continue
        if key == "location" and group == "property" and (D.get("object_facts") or {}).get("address"):
            rows.append(_row(label, D["object_facts"]["address"],
                             tx.label(tx.SOURCE_LABELS, "document", lang) + ", " + t("check_mark", lang)))
            continue
        if key == "year" and opt.get("year"):
            rows.append(_row(label, str(opt["year"]), tx.label(tx.SOURCE_LABELS, "input", lang)))
            continue
        if key == "construction" and opt.get("construction"):
            rows.append(_row(label, tx.label(tx.RA_VALUE_LABELS, opt["construction"], lang),
                             tx.label(tx.SOURCE_LABELS, "input", lang)))
            continue
        best = preferred(rec, key)
        if key == "object_type":
            doc_row = _object_type_row(D, lang, label, best)
            if doc_row:
                rows.append(doc_row)
                continue
        if key == "object_type" and not best and kind:
            rows.append(_row(label, _kind_label(D, lang) or NA,
                             tx.label(tx.SOURCE_LABELS, "input" if opt.get("object_kind") else "photo", lang)
                             + ", " + t("check_mark", lang)))
            continue
        dflt = _default_otype(D)
        if key == "object_type" and not best and dflt:
            # вида объекта нет ни во вводе, ни в документе: аналитика посчитана на типе по умолчанию — так и пишем
            rows.append(_row(label, t("s1_kind_default", lang, v=_otype_label(dflt, lang)),
                             t("s1_kind_default_note", lang)))
            continue
        if not best:
            rows.append(None)
            missing.append((key, label))
            continue
        note = tx.label(tx.SOURCE_LABELS, best["source"], lang) + ", " + t("check_mark", lang)
        others = [r for r in rec if (r["key"] == key or (key == "year" and r["key"] == "manufacture_date"))
                  and r is not best and not ae._same(key, r["value"], best["value"])]
        if others:
            note += "; " + t("also_in", lang, what="; ".join(
                f"{r['value']} ({tx.label(tx.SOURCE_LABELS, r['source'], lang)})" for r in others[:3]))
        rows.append(_row(label, best["value"], note))
    if group not in ROWS_BY_GROUP:
        for key in ae.EXTRA_ROW_KEYS:
            best = preferred(rec, key)
            if best:
                rows.append(_row(tx.field_label(key, lang, group), best["value"],
                                 tx.label(tx.SOURCE_LABELS, best["source"], lang) + ", " + t("check_mark", lang)))
    # «данные недоступны» — не больше трёх строк: сначала ключевые признаки, остальное одной строкой
    order = sorted(missing, key=lambda kl: (kl[0] not in key_first, keys.index(kl[0])))
    keep = {k for k, _l in order[:MAX_NA_ROWS]}
    out, rest = [], []
    mi = 0
    for row in rows:
        if row is not None:
            out.append(row)
            continue
        key, label = missing[mi]
        mi += 1
        if key in keep:
            out.append(_row(label, NA))
        else:
            rest.append(label.lower() if lang != "en" else label[:1].lower() + label[1:])
    if rest:
        out.append(_row(t("s1_not_specified", lang), ", ".join(rest)))
    return out


def _part_label(p: dict, lang: str) -> str:
    """«Часть 2 — класс 14 Кредиты»."""
    return t("pt_part", lang, n=p["index"], cls=_class_label(p["class_code"], p.get("class_name"), lang))


def _value_text(value: dict, lang: str) -> str:
    """Вывод «сумма к стоимости» (раздел 3) для одного значения value_check."""
    ratio = pct(value["ratio_pct"], lang, 2)
    legal = tx.label(tx.LEGAL_REFS, value["legal_ref"], lang) if value.get("legal_ref") else None
    if value["verdict"] == "over":
        return t("v_over", lang, diff=money(value["diff"], lang), ref=legal)
    if value["verdict"] == "under":
        return t("v_under", lang, ratio=ratio, ref=legal)
    if value.get("legal_ref"):
        return t("v_normal", lang, ratio=ratio) + " " + t("v_under_small", lang, ratio=ratio, ref=legal)
    return t("v_normal", lang, ratio=ratio)


def _fr_short(fr: dict, lang: str) -> str:
    """Франшиза части одной строкой для таблицы частей."""
    st_ = fr.get("status")
    if st_ == "statutory":
        return t("pt_fr_statutory", lang)
    if st_ == "applied":
        return t("pt_fr_applied", lang, pct=pct(fr.get("size_pct"), lang))
    if st_ == "proposed":
        return t("pt_fr_proposed", lang, pct=pct(fr["size_pct"], lang)) if fr.get("size_pct") \
            else t("pt_fr_proposed_nosize", lang)
    return t("pt_fr_none", lang)


def _part_rate_text(r: dict, lang: str) -> str:
    if r["mode"] in ("tariff", "statutory") and r.get("applied_pct") is not None:
        return pct(r["applied_pct"], lang)
    return t("rate_undefined", lang)


def _parts_sc_how(sc: dict, lang: str) -> list:
    """Как сложены сценарии договора: один объект — большее из частей, разные объекты — сумма."""
    agg = sc.get("aggregate") or {}
    out = [t("pt_sc_rule_" + str(agg.get("rule") or "max"), lang)]
    for s in ("PML", "EML", "MFL"):
        it = (agg.get("items") or {}).get(s) or {}
        mv = it.get("main_values") or []
        terms = []
        if len(mv) > 1:
            terms.append(t("pt_sc_max", lang, vals="; ".join(
                t("pt_part_short", lang, n=x["index"]) + " " + money(x["amount"], lang) for x in mv)))
        elif mv:
            terms.append(t("pt_part_short", lang, n=mv[0]["index"]) + " " + money(mv[0]["amount"], lang))
        terms += [t("pt_part_short", lang, n=x["index"]) + " " + money(x["amount"], lang) for x in it.get("added") or []]
        out.append(t("pt_sc_line", lang, s=s, expr=" + ".join(terms), total=money(it.get("amount"), lang)))
    for x in agg.get("excluded") or []:
        out.append(t("pt_sc_excluded", lang, n=x["index"], cls=x["class_code"]))
    out.append(t("pt_sc_ret", lang))
    return out


def _rq_params(code: str, p: dict, lang: str, how: bool = False) -> dict:
    """Параметры строки сверки в словах языка акта: тарифы — процентом, премии — сумами, даты — ДД.ММ.ГГГГ.
    how — «как считали»: сумма и премия с копейками, если они есть."""
    NA = t("na", lang)
    out = {}
    # что сравнивается: тарифы и франшиза — проценты, срок — дни, остальное — сумы
    kind = "pct" if ("tariff" in code or "franchise" in code) else "days" if "term" in code else "money"
    for k, v in (p or {}).items():
        if v is None:
            out[k] = NA
        elif k in ("req", "calc", "min", "diff") and kind == "pct":
            out[k] = pct(abs(v) if k == "diff" else v, lang)
        elif k in ("req", "calc", "diff") and kind == "days":
            out[k] = str(int(v))
        elif k == "diff":
            out[k] = _signed(v, lang)
        elif how and k in ("sum", "premium"):
            out[k] = money_k(v, lang)
        elif k in ("req", "calc", "tol", "sum", "premium"):
            out[k] = money(v, lang)
        elif k in ("rate", "pct", "act"):
            out[k] = pct(v, lang)
        elif k == "ratio":
            out[k] = pct(v, lang, 2)
        elif k in ("date_from", "date_to"):
            out[k] = _ddmmyyyy(v)
        elif k == "days":
            out[k] = str(int(v))
        else:
            out[k] = v
    return out
