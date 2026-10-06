"""Сборка данных акта (язык не важен): build_data — шаги от загрузки фото до решения; сессия, рынок, курс."""
import json
import re
from datetime import date
from types import SimpleNamespace
from typing import Optional

from .. import act_analytics as aa
from .. import act_engine as ae
from .. import act_extras as ax
from .. import act_scoring as asc
from .. import class_templates as ctpl
from .. import act_market as am
from .. import act_texts as tx
from .. import db, valuation
from ..risk_analytics import load_thresholds

from .common import clause_catalog, insurer_name, _iso, load_settings, _now, tariff_version
from .recognize import preferred
from .regions import region_for_modules, region_for_stats, region_scope
from .view_fmt import ROWS_BY_GROUP
from .documents import _borrower_block, _trust_credit
from .terms import credit_rule, _policyholder, _trust_doc, trust_sources
from .market import fx_verify
from .calc import (veh_measure_codes,
    _analytics_block, _annual_view, _below_min_block, _below_min_decision, _doc_checks, _fork_finish,
    _fork_prepare, _kind_from_text, _location_from_text, _min_block, _min_how, _min_info, _napp_settings,
    _net_calibrated, _no_products_rate, _region_scope_stats, _template_block, _with_clauses)
from .build_parts import _apply_objects, _apply_parts


# --------------------------------------------------------------------------- #
#  Сборка акта: структурированные данные (язык не важен)
# --------------------------------------------------------------------------- #

def build_data(con, clean: dict, owner: str, lang: str) -> dict:
    """
    Данные акта из проверенного ввода (validate): загрузка фото, объект, уровень риска, ставка, франшиза, сценарии,
    мероприятия, аналитика, сверки, решение; части и парк объектов. Язык не важен — текст собирает render.
    Шаги работают с общим состоянием B (одна область, как раньше внутри одной функции).
    """
    ref = db.load_reference(con)
    st = load_settings(con)
    m, o = clean["must"], clean["optional"]
    product = m.get("product")
    cls = m["class_code"]

    B = SimpleNamespace(clean=clean, cls=cls, m=m, o=o, owner=owner, product=product, ref=ref, st=st)
    _session_upload(con, B)
    _inspection_in(B)
    _object_in(con, B)
    _risk_in(B)
    _docs_in(B)
    _rate_in(con, B)
    _value_in(con, B)
    _modules_in(con, B)
    _missing_in(B)
    _decision_in(con, B)
    _assemble(con, B)
    _finish_single(con, B)
    _finish_parts(con, B)
    _finish_borrower(B)
    _finish_objects(con, B)
    return B.D


def _session_upload(con, B) -> None:
    """Своя живая загрузка фото по clean.session (снимки объявлений — не осмотр)."""
    clean, owner = B.clean, B.owner
    # сессия фото: только своя и живая
    upload, session_missing, upload_files = None, False, []
    if clean.get("session"):
        rows = db.rows(con, "SELECT * FROM act_uploads WHERE id=? AND owner_key=? AND expires_at > ?",
                       clean["session"], owner, _iso(_now()))
        if rows:
            upload = json.loads(rows[0]["result_json"] or "{}")
            upload_files = json.loads(rows[0].get("files_json") or "[]")
        if not rows or upload.get("kind") == "market":   # снимки объявлений — не осмотр объекта
            upload, session_missing = None, True
    upload = upload or {}
    B.session_missing, B.upload, B.upload_files = session_missing, upload, upload_files


def _inspection_in(B) -> None:
    """Распознанное (источники проверены по загрузке), повреждения, число фото, ракурсы."""
    clean, upload = B.clean, B.upload
    downgraded = 0
    if clean.get("recognized") is not None:
        recognized = [dict(r) for r in clean["recognized"]]
        downgraded = trust_sources(recognized, upload)
    else:
        recognized = [{"key": f["key"], "value": f["value"], "source": f["source"], "note": f.get("note"),
                       "file_id": f.get("file_id")} for f in upload.get("fields") or []]
    damages = clean["damages"] if clean.get("damages") is not None else (upload.get("damages") or [])
    # фото и сканы, ушедшие в модель; документы с текстом разобраны отдельно и осмотром не считаются
    photos = int(upload.get("photo_files", upload.get("files")) or 0)
    ai_ok = bool(upload.get("ai")) and photos > 0
    views_seen = sorted(set((upload.get("views") or {}).values()))
    B.ai_ok, B.damages, B.downgraded, B.photos, B.recognized = ai_ok, damages, downgraded, photos, recognized
    B.views_seen = views_seen


def _object_in(con, B) -> None:
    """Вид объекта, группа, тип для ставки и для аналитики, шаблон класса, нужные ракурсы."""
    cls, o, product, recognized, ref, upload = B.cls, B.o, B.product, B.recognized, B.ref, B.upload
    kind = o.get("object_kind") or upload.get("object_kind")
    kind_type = tx.OBJECT_KINDS[kind][0] if kind in tx.OBJECT_KINDS else None
    obj_text = (preferred(recognized, "object_type") or {}).get("value") or ""
    group = ae.object_group(cls, f"{kind_type or ''} {obj_text} {o.get('object_type') or ''}",
                            upload.get("class_hint") or "")
    otype = ae.match_object_type(ref, cls, kind_type, o.get("object_type"))
    # для сценариев и мероприятий вид объекта можно понять и по тексту документа («склад готовой продукции»)
    kind_ra = kind or _kind_from_text(f"{obj_text} {o.get('object_type') or ''}")
    otype_ra = otype or ae.match_object_type(ref, cls, (tx.OBJECT_KINDS.get(kind_ra) or (None,))[0], None)
    # продукт «спецтехника» без фото: для сценариев и мероприятий это спецтехника, а не легковой транспорт
    special_product = "спецтехник" in str((product or {}).get("name") or "").lower()
    group_ra = "special" if group == "vehicle" and special_product else group
    # шаблон анализа класса (справочник class_templates, приложение А): ракурсы, оговорки, мероприятия, риски,
    # правило сценария; у классов 3, 8 и 9 шаблон повторяет прежний выбор по группе объекта
    tpl_row = ctpl.current(con, cls)
    tpl = (tpl_row or {}).get("template") or {}
    views_req = ctpl.for_group(tpl.get("required_views"), group)
    if views_req is None:
        views_req = ae.required_views(group)
    # подгруппа транспорта класса 3 (06.10.2026): поле класса veh_group → вид объекта → категория с фото → описание
    # и название продукта → по умолчанию легковой; по ней — мероприятия шаблона, советы и сценарии словами
    veh_group = ae.vehicle_group(cls, o.get("class_fields"), kind, upload.get("vehicle_category"), group_ra,
                                 text=f"{obj_text} {o.get('object_type') or ''} {(product or {}).get('name') or ''}")
    B.group, B.group_ra, B.kind, B.kind_ra, B.kind_type = group, group_ra, kind, kind_ra, kind_type
    B.veh_group = veh_group
    B.obj_text = obj_text
    B.otype, B.otype_ra, B.special_product, B.tpl, B.tpl_row = otype, otype_ra, special_product, tpl, tpl_row
    B.views_req = views_req


def _risk_in(B) -> None:
    """Год, документы, место хранения и уровень риска по правилу."""
    ai_ok, damages, o, recognized, st, upload = B.ai_ok, B.damages, B.o, B.recognized, B.st, B.upload
    views_seen = B.views_seen
    y = o.get("year")
    if y is None:
        pr = preferred(recognized, "year")
        y = ae.to_year(pr["value"]) if pr else None
    documents = "document" in views_seen
    if o.get("documents_provided") is not None:
        documents = bool(o["documents_provided"])
    location = o.get("location")
    if not location:
        loc_rec = (preferred(recognized, "location") or {}).get("value") or ""
        location = _location_from_text(loc_rec)

    # входы правила уровня; части и объекты считают свой уровень от копии этих же входов
    risk_in = {"inspected": ai_ok, "damages": damages,
               "condition": o.get("condition") or (upload.get("condition") if ai_ok else None),
               "year": y, "location": location, "guard": o.get("guard"),
               "losses_count": o.get("losses_count"), "documents": documents, "today": date.today()}
    risk = ae.risk_level(risk_in, st)
    B.documents, B.location, B.risk, B.risk_in, B.y = documents, location, risk, risk_in, y


def _docs_in(B) -> None:
    """Запрос филиала и договор (источник решает сервер), срок страхования из них."""
    clean, o, session_missing, st, upload = B.clean, B.o, B.session_missing, B.st, B.upload
    # запрос филиала: из ввода (экран подставил распознанное) или из своей загрузки бланка; источник каждого
    # поля и правки сотрудника решает сервер по своей загрузке (_trust_doc), поле source экрана не доверяется
    inclusive = bool(st["request_check"]["term_inclusive"])
    req, rq_trust = _trust_doc("rq", clean.get("request"), upload.get("branch_request"), session_missing, inclusive)
    term_from_request = False
    if o.get("term_days") is None and req and req.get("term_days"):
        # срок сотрудник не ввёл — берём весь срок договора из запроса (многолетний тоже; ставка годовая)
        o["term_days"] = int(req["term_days"])
        o["term_source"] = "request"
        term_from_request = True
    # договор: из ввода (экран подставил распознанное) или из своей загрузки договора
    ct, ct_trust = _trust_doc("ct", clean.get("contract"), upload.get("contract"), session_missing, inclusive)
    term_from_contract = False
    if o.get("term_days") is None and ct and ct.get("term_days"):
        o["term_days"] = int(ct["term_days"])
        o["term_source"] = "contract"
        term_from_contract = True
    term = o.get("term_days") or 365
    B.ct, B.ct_trust, B.req, B.rq_trust, B.term = ct, ct_trust, req, rq_trust, term
    B.term_from_contract = term_from_contract
    B.term_from_request = term_from_request


def _rate_in(con, B) -> None:
    """Минимальная ставка, ставка акта, факторы объекта, вилка ставки (поправки региона и рынка)."""
    clean, cls, group_ra, m, o, otype = B.clean, B.cls, B.group_ra, B.m, B.o, B.otype
    product, ref, risk, st, term, tpl = B.product, B.ref, B.risk, B.st, B.term, B.tpl
    # минимальная ставка страховщика на дату акта (app/min_rates.py): источник (правка администратора или тарифная
    # политика) и тип ставки продукта (annual | fixed); тот же минимум движок берёт из справочника (db.load_reference)
    min_info = _min_info(con, product, o.get("payer_type"))
    rate_type = (min_info or {}).get("rate_type") or "annual"
    rate_res = ae.rate(ref, product, cls, risk["level"], m["sum_insured"], term, otype, o.get("payer_type"), st,
                       rate_type=rate_type)
    _min_how(rate_res, min_info)
    # класс без продуктов страховщика (16у, 18) — акт по шаблону, ставка не определена: тарифной политики по классу
    # нет, экспертная базовая ставка справочника (если есть) ставкой акта не становится
    if not product and ctpl.products_count(con, cls) == 0:
        rate_res = _no_products_rate(rate_res, cls)
    statutory = rate_res["mode"] in ("statutory", "statutory_undefined")
    multi = bool(clean.get("parts")) or len(m.get("product_classes") or []) > 1
    # факторы объекта по подгруппам класса (02.10.2026, factor_groups шаблона): режим reference — ставка акта прежняя,
    # множитель справочно (отметка вилки); apply — ставка акта = тариф × поправка уровня × множитель, не ниже минимума,
    # до вилки, франшизы и сверок. Комплексный продукт — по шаблону класса каждой части (_part_calc)
    fa = ae.factor_adjust(o.get("class_fields"), tpl, st, o)
    # фон региона к факторам со ссылкой на stat.uz (stat_ref, 02.10.2026): только чтение stat_series, ставку не меняет
    aa.factor_stats(con, fa, region_for_stats(m))
    if not multi:
        ae.factor_effect(fa, rate_res, m["sum_insured"])
    # вилка ставки (01.10.2026): поправки региона (stat.uz) и рынка (НАПП) к ставке акта; в режиме apply ставка с
    # поправками становится ставкой акта до франшизы, мероприятий и сверок — всё дальше считается от неё
    fork_errors = []
    fs = ae.fork_settings(st)
    fork_in, fork_adj = _fork_prepare(con, fs, cls=cls, region=region_for_modules(m), group=group_ra,
                                      product=product, rate_res=_annual_view(rate_res), errors=fork_errors,
                                      min_info=min_info, scope=region_scope(m))
    if not multi and rate_res.get("rate_type") != "fixed":
        # у фиксированной ставки (на весь срок) вилка — справочно в годовом выражении, ставку акта не меняет
        ae.apply_fork(rate_res, fork_adj, m["sum_insured"])
    B.fa, B.fork_adj, B.fork_errors, B.fork_in, B.fs, B.min_info = fa, fork_adj, fork_errors, fork_in, fs, min_info
    B.multi, B.rate_res, B.rate_type, B.statutory = multi, rate_res, rate_type, statutory


def _value_in(con, B) -> None:
    """Сумма к стоимости, франшиза по правилу, оговорки, расхождения распознанного с вводом."""
    cls, group, kind_type, m, o, obj_text = B.cls, B.group, B.kind_type, B.m, B.o, B.obj_text
    recognized, risk, st, statutory, term_from_contract = B.recognized, B.risk, B.st, B.statutory, B.term_from_contract
    term_from_request = B.term_from_request
    tpl = B.tpl
    value = ae.value_check(m["sum_insured"], m["object_value"], st, o.get("price_new"), o.get("purchase_year"),
                           group, kind_type or obj_text)
    th = load_thresholds(con)
    fr = ae.franchise({"small_count": o.get("small_count"), "dominant_risk": o.get("dominant_risk"),
                       "want_lower_premium": o.get("want_lower_premium")},
                      risk["level"], th, statutory, cls, m["sum_insured"])
    fr["thresholds_source"] = {k: v for k, v in (th.get("_source") or {}).items() if k in ("id", "what")}
    clause_codes = ctpl.for_group(tpl.get("clauses"), group)
    clauses = ae.clauses_by_codes(clause_codes, clause_catalog()) if clause_codes is not None \
        else ae.clauses(group, clause_catalog())
    disc = ae.discrepancies(recognized, {"year": o.get("year"), "sum_insured": m["sum_insured"],
                                         "object_value": m["object_value"],
                                         "term_days": None if (term_from_request or term_from_contract)
                                         else o.get("term_days")})
    B.clause_codes, B.clauses, B.disc, B.fr, B.th, B.value = clause_codes, clauses, disc, fr, th, value


def _modules_in(con, B) -> None:
    """Сценарии, франшиза, мероприятия, альтернативы, аналитика раздела 4, справка биржи."""
    clean, cls, ct, fork_errors, fr, fs = B.clean, B.cls, B.ct, B.fork_errors, B.fr, B.fs
    group_ra, kind, kind_ra, location, m, multi = B.group_ra, B.kind, B.kind_ra, B.location, B.m, B.multi
    o, otype_ra, rate_res, recognized, ref, req = B.o, B.otype_ra, B.rate_res, B.recognized, B.ref, B.req
    risk, st, statutory, term, th, tpl = B.risk, B.st, B.statutory, B.term, B.th, B.tpl
    upload, y = B.upload, B.y
    # сценарии, франшиза и мероприятия — существующими модулями на тех же входных данных, что акт
    block_errors = list(fork_errors)
    # описание объекта в документе (запрос филиала, договор, распознанное): по нему — деятельность на объекте
    obj_doc = object_text_info(recognized, upload, req, ct)
    # регион для модулей аналитики — названием по-русски: код экрана (tashkent_region) они не знают
    region_ra = region_for_modules(m)
    ctx = ax.ra_context(con, cls=cls, product_code=m.get("product_code"), otype=otype_ra, group=group_ra, kind=kind_ra,
                        S=m["sum_insured"], V=m["object_value"], region=region_ra, term_days=o.get("term_days"),
                        year=y, o=o, recognized=recognized,
                        text=" ".join(x for x in (obj_doc.get("original"), obj_doc.get("translated"),
                                                  o.get("object_type")) if x))
    if not ctx.get("ok"):
        block_errors.append({"block": "risk_analytics", "error": ctx.get("error")})
    scen = ax.scenarios(ctx, cls, m["sum_insured"], template=tpl, V=m["object_value"], fields=o.get("class_fields"))
    if o.get("protection_ignored"):
        scen["assumptions"] = list(scen.get("assumptions") or []) + [{"code": "as_protection_ignored", "params": {}}]
    try:
        fr = ax.franchise(con, ctx, fr, rate_res, cls=cls, S=m["sum_insured"], level=risk["level"],
                          statutory=statutory, requested=o.get("deductible"), th=th)
    except Exception as e:               # блок не посчитан — акт всё равно формируется
        block_errors.append({"block": "franchise", "error": type(e).__name__})
        fr.update(status="error", applied=False, how=[], alternatives=[], error=type(e).__name__)
    premium_final = fr["premium_after"] if fr.get("applied") and fr.get("premium_after") is not None \
        else rate_res["premium"]
    rate_final = fr["rate_after"] if fr.get("applied") and fr.get("rate_after") is not None \
        else rate_res["applied_pct"]
    try:
        meas = ax.measures(con, ctx, rate_res, cls=cls, group=group_ra, kind=kind_ra, S=m["sum_insured"],
                           V=m["object_value"], o=o, location=location, statutory=statutory, th=th,
                           premium=premium_final, codes=veh_measure_codes(tpl, B.veh_group, group_ra))
    except Exception as e:
        block_errors.append({"block": "measures", "error": type(e).__name__})
        meas = {"items": [], "total": {"count": 0}, "error": type(e).__name__}
    if fr.get("status") not in ("error",):
        try:
            fr["alternatives"] = ax.alternatives(ctx, rate_res, m["sum_insured"], meas, th)
        except Exception as e:
            block_errors.append({"block": "alternatives", "error": type(e).__name__})
    # аналитика раздела 4 (30.09.2026): детализация и справочная техническая ставка — тариф акта не меняет
    # риски: классы с рисками в справочнике perils (8, 9) и с правилом в модуле аналитики (3) — как раньше;
    # остальные — экспертные доли шаблона класса вместо одной строки «весь класс»
    tpl_risks = None
    if cls not in ax.RULE_CLASSES and not any(p["class_code"] == cls for p in ref.perils.values()):
        tpl_risks = ctpl.template_risks(tpl, cls) or None
    # комплексный продукт (несколько классов) или явные части: аналитика считается по каждой части (_apply_parts)
    if multi:
        analytics = {"available": False, "reason": "by_parts", "calibrated": ae.CALIBRATED}
    else:
        analytics = _analytics_block(con, ctx, cls, m.get("product_code"), region_ra, m["sum_insured"],
                                     m["object_value"], term, rate_res, scen, meas, risk["level"], statutory, th,
                                     group_ra, tpl_risks, block_errors, veh_group=B.veh_group)
        _napp_settings(analytics, fs, st)
        _region_scope_stats(analytics, region_scope(m))
    analytics["activity"] = (ctx.get("must") or {}).get("activity") if ctx.get("ok") else None
    analytics["activity_source"] = (ctx.get("sources") or {}).get("activity")
    # тип объекта, на котором посчитана аналитика, и откуда он (default — принят по умолчанию)
    analytics["object_type"] = (ctx.get("must") or {}).get("object_type") if ctx.get("ok") else None
    analytics["object_type_source"] = (ctx.get("sources") or {}).get("object_type")
    # справка биржи УзРТСБ (02.10.2026): классы 7, 8, 9, 16 — медианы сделок uzex.uz из exchange_quotes (только
    # чтение); стоимость объекта и ставку не меняет. Нет данных — available = false, в акте ничего не пишется
    try:
        exchange = aa.exchange_background(
            con, cls, kind, clean.get("parts"), o.get("class_fields"),
            text=" ".join(str(x) for x in (obj_doc.get("original"), obj_doc.get("translated"), o.get("object_type"))
                          if x))
    except Exception as e:               # справка не собрана — акт всё равно формируется
        block_errors.append({"block": "exchange", "error": type(e).__name__})
        exchange = {"available": False, "reason": "error", "items": []}
    analytics["exchange"] = exchange
    B.analytics, B.block_errors, B.ctx, B.exchange, B.fr, B.meas = analytics, block_errors, ctx, exchange, fr, meas
    B.obj_doc, B.premium_final, B.rate_final, B.region_ra, B.scen = obj_doc, premium_final, rate_final, region_ra, scen
    B.tpl_risks = tpl_risks


def _missing_in(B) -> None:
    """Чего не хватает (поля и ракурсы) и сводка осмотра."""
    ai_ok, clean, damages, documents, group, kind = B.ai_ok, B.clean, B.damages, B.documents, B.group, B.kind
    o, photos, recognized, session_missing, upload = B.o, B.photos, B.recognized, B.session_missing, B.upload
    views_req = B.views_req
    views_seen = B.views_seen
    present = {r["key"] for r in recognized if r.get("value")}
    if any(r["key"] == "manufacture_date" for r in recognized) or o.get("year"):
        present.add("year")
    if o.get("location"):
        present.add("location")
    if kind:
        present.add("object_type")
    if o.get("construction"):
        present.add("construction")
    if ((upload.get("contract") or {}).get("fields") or {}).get("address"):
        present.add("location")
    # набор полей — тот же, что строки раздела 1 (у оборудования и зданий — свои, 30.09.2026)
    missing = [k for k in (ROWS_BY_GROUP.get(group) or ae.FIELDS_BY_GROUP.get(group, [])) if k not in present]
    missing_key = [k for k in ae.KEY_FIELDS.get(group, []) if k in missing]
    missing_v = ae.missing_views(group, views_seen, views_req) if ai_ok else (list(views_req) if photos else [])
    inspection = {"photos": photos, "ai": ai_ok, "ai_reason": upload.get("reason") if photos and not ai_ok else None,
                  "views_seen": views_seen, "required_views": list(views_req),
                  "missing_views": missing_v, "damages": damages, "documents": documents,
                  "document_kinds": sorted(set((upload.get("document_kinds") or {}).values())),
                  "recognized": bool(recognized), "session": clean.get("session"),
                  "session_missing": session_missing, "upload_lang": upload.get("lang"),
                  "parsed_docs": int(upload.get("parsed_docs") or 0)}
    B.inspection, B.missing, B.missing_key = inspection, missing, missing_key


def _decision_in(con, B) -> None:
    """Решение и проверки андеррайтеру: кредит, урожай, франшиза, рынок, сверки с документами."""
    clean, cls, ct, ct_trust, disc, fr = B.clean, B.cls, B.ct, B.ct_trust, B.disc, B.fr
    group, inspection, kind, m, missing_key, o = B.group, B.inspection, B.kind, B.m, B.missing_key, B.o
    owner, premium_final, rate_final, rate_res = B.owner, B.premium_final, B.rate_final, B.rate_res
    recognized, req = B.recognized, B.req
    risk, rq_trust, scen, st, term_from_contract = B.risk, B.rq_trust, B.scen, B.st, B.term_from_contract
    term_from_request = B.term_from_request
    tpl, upload, value, y = B.tpl, B.upload, B.value, B.y
    dec = ae.decision(risk, rate_res, value, fr, disc, inspection, missing_key, st)
    share = credit_rule(tpl, cls)
    if share is not None:
        # кредит (правило проекта № 6): сумма ≤ min(кредит − обеспечение; 50 % кредита), страхователь — банк;
        # любая из этих проверок — «принять без оговорок» уже нельзя
        cks = ae.credit_check(m["sum_insured"], o.get("class_fields"), share, _policyholder(req, ct, upload))
        _with_clauses(dec, [{"code": "c_" + ("tpl_credit_over" if c["code"] == "credit_over" else c["code"]),
                             "params": c["params"]} for c in cks])
    # урожай (16у): страховая сумма выше стоимости урожая (площадь × урожайность × цена) — ГК ст. 938
    crop_over = [c for c in scen.get("checks") or [] if c.get("code") == "tpl_crop_over"]
    if crop_over:
        _with_clauses(dec, [{"code": "c_tpl_crop_over", "params": dict(crop_over[0]["params"])}])
    if fr.get("applied"):
        # франшиза сотрудника — условие договора: андеррайтер подтверждает, «принять без оговорок» уже нельзя
        _with_clauses(dec, [{"code": "c_fr_applied", "params": {}}])
    market = None
    mchecks = []
    if clean.get("market"):
        market = market_block(con, clean["market"], owner, st, m, o, recognized, kind, cls, group, y)
        mchecks = am.decision_checks(market)
        _with_clauses(dec, mchecks)
    docs = {"req": req, "ct": ct, "rq_trust": rq_trust, "ct_trust": ct_trust, "upload": upload,
            "term_from_request": term_from_request, "term_from_contract": term_from_contract}
    rc, cc, xc, rq_checks, ct_checks = _doc_checks(st, m, docs, rate_res, rate_final, premium_final, value, fr)
    doc_checks = list(rq_checks) + list(ct_checks)
    # тариф ниже минимума, расхождение с запросом или договором, нет существенного условия
    _with_clauses(dec, rq_checks)
    _with_clauses(dec, ct_checks)
    B.cc, B.dec, B.doc_checks, B.docs, B.market, B.mchecks = cc, dec, doc_checks, docs, market, mchecks
    B.rc, B.xc = rc, xc


def _assemble(con, B) -> None:
    """Данные акта D: всё посчитанное выше одним снимком (язык не важен)."""
    cls_row = db.rows(con, "SELECT name FROM classes WHERE code=?", B.cls)
    B.D = {
        "insurer": insurer_name(B.st),
        "must": {"product_code": B.m.get("product_code"), "product_name": (B.product or {}).get("name"),
                 "class_code": B.cls, "class_name": cls_row[0]["name"] if cls_row else None,
                 "sum_insured": B.m["sum_insured"], "object_value": B.m["object_value"], "region": B.m["region"],
                 "region_code": B.m.get("region_code"), "product_classes": B.m.get("product_classes") or [],
                 # территория текстом и особый регион (02.10.2026): republic | outside | None
                 "region_text": B.m.get("region_text"), "region_scope": region_scope(B.m)},
        "multi_class": len(B.m.get("product_classes") or []) > 1,
        "sources_downgraded": B.downgraded,
        "optional": {k: v for k, v in B.o.items() if v is not None},
        "group": B.group, "object_kind": B.kind, "object_type_ref": B.otype,
        # подгруппа транспорта класса 3 (06.10.2026); у других классов — None
        "veh_group": B.veh_group,
        "recognized": B.recognized, "inspection": B.inspection, "risk": B.risk, "rate": B.rate_res,
        "value": B.value, "franchise": B.fr, "clauses": B.clauses, "discrepancies": B.disc, "decision": B.dec,
        "missing": B.missing, "tariff_version_id": tariff_version(con, "регулятор" if B.statutory else "компания"),
        "settings_id": (B.st.get("_source") or {}).get("id"), "calibrated": ae.CALIBRATED,
        "dropped_pd": B.clean.get("dropped_pd") or 0,
        # дополнения 29.09.2026: одна премия на весь акт — с учётом применённой франшизы
        "premium_final": {"amount": B.premium_final, "rate_pct": B.rate_final,
                          "franchise_applied": bool(B.fr.get("applied"))},
        "scenarios": B.scen, "measures": B.meas, "block_errors": B.block_errors,
        "market": B.market,
        "request": B.req, "request_check": B.rc,
        "contract": B.ct, "contract_check": B.cc, "cross_check": B.xc,
        # дополнения 30.09.2026: аналитика раздела 4, описание объекта из документа, адрес объекта
        "analytics": B.analytics, "object_doc": B.obj_doc,
        "exchange": B.exchange,
        "object_facts": {"address": ((B.upload.get("contract") or {}).get("fields") or {}).get("address")},
        # шаблон анализа класса (30.09.2026): версия и то, что акт из него взял; подписи — на языке при выдаче
        "template": _template_block(B.tpl_row, B.group, B.group_ra, B.views_req, B.clause_codes, B.tpl_risks, B.scen,
                                    B.o.get("class_fields")),
        # комплексный продукт по частям (30.09.2026): для продукта с одним классом — mode single
        "parts": {"mode": "single", "source": None, "confirmed": True, "items": [], "totals": None, "notes": []},
    }


def _finish_single(con, B) -> None:
    """Шаблон без продуктов, вилка и факторы (один класс), минимальная ставка, оценка заниженной ставки."""
    D, analytics, cls, ct, documents, fa = B.D, B.analytics, B.cls, B.ct, B.documents, B.fa
    fork_adj, fork_in, fr, fs, m, min_info = B.fork_adj, B.fork_in, B.fr, B.fs, B.m, B.min_info
    multi, o, premium_final, rate_res, req, risk = B.multi, B.o, B.premium_final, B.rate_res, B.req, B.risk
    scen, st, statutory, term, y = B.scen, B.st, B.statutory, B.term, B.y
    if D.get("template") is not None:
        # класс без продуктов страховщика (16у, 18): пометка в шаблоне акта
        n_prod = ctpl.products_count(con, cls)
        D["template"]["products_count"] = n_prod
        D["template"]["no_products_note"] = dict(ctpl.NO_PRODUCTS_NOTE) if n_prod == 0 else None
    if not multi:
        rt_ = rate_res.get("rate_type") or "annual"
        D["rate_fork"] = _fork_finish(fork_in, fork_adj, fs, _annual_view(rate_res), m["sum_insured"],
                                      ae.annual_pct((req or {}).get("tariff_pct"), term, rt_),
                                      ae.annual_pct((ct or {}).get("tariff_pct"), term, rt_), analytics,
                                      premium_final, bool(fr.get("applied")), factors=fa)
        D["factor_adjustment"] = fa
        if rt_ == "fixed":
            D["rate_fork"]["rate_type"] = "fixed"         # отметки — годовой эквивалент фиксированной ставки
    # минимальная ставка страховщика и её источник — в данных акта: старый акт показывает минимум своей даты
    D["min_rate"] = _min_block(min_info, rate_res)
    # оценка заниженной ставки (01.10.2026): запрошенная ставка ниже минимальной — можно ли застраховать
    D["below_min"] = _below_min_block(D, o, req, ct, rate_res, analytics, scen, risk, documents, y, st,
                                      statutory, multi, _net_calibrated(con, cls, analytics))
    _below_min_decision(D)


def _finish_parts(con, B) -> None:
    """Комплексный продукт: расчёт по частям и факторы каждой части."""
    D, block_errors, clean, disc, docs, fs = B.D, B.block_errors, B.clean, B.disc, B.docs, B.fs
    inspection, kind, location = B.inspection, B.kind, B.location
    m, mchecks, missing_key, multi, o, obj_doc = B.m, B.mchecks, B.missing_key, B.multi, B.o, B.obj_doc
    product, recognized, ref, region_ra, st, term = B.product, B.recognized, B.ref, B.region_ra, B.st, B.term
    th, upload, y = B.th, B.upload, B.y
    if multi:
        C = {"ref": ref, "st": st, "th": th, "m": m, "o": o, "product": product, "recognized": recognized,
             "upload": upload, "kind": kind, "obj_text": " ".join(x for x in (obj_doc.get("original"),
                                                                              obj_doc.get("translated")) if x),
             "class_hint": upload.get("class_hint") or "", "y": y, "location": location,
             "region_ra": region_ra, "term": term, "fs": fs,
             "risk_in": dict(B.risk_in),
             "docs": docs, "mchecks": mchecks, "disc": disc, "inspection": inspection, "missing_key": missing_key,
             "block_errors": block_errors}
        _apply_parts(con, clean, D, C)
        # факторы объекта комплексного продукта — по каждой части (шаблон класса части)
        D["factor_adjustment"] = {"by_parts": True, "mode": ae.factor_settings(st)["mode"],
                                  "parts": [dict(p.get("factor_adjustment") or {}, index=p["index"],
                                                 class_code=p["class_code"]) for p in D["parts"]["items"]],
                                  "calibrated": ae.CALIBRATED}


def _finish_borrower(B) -> None:
    """Цвет скоринга, заёмщик по отчёту бюро (проверки андеррайтеру)."""
    D, clean, cls, m, session_missing, st = B.D, B.clean, B.cls, B.m, B.session_missing, B.st
    upload = B.upload
    # страховой скоринг (01.10.2026): цвет полос страницы скоринга — из настроек
    D["scoring_style"] = {"brand_color": (st.get("scoring") or {}).get("brand_color") or asc.DEFAULT_BRAND}
    # отчёт кредитного бюро (КАТМ): проверки андеррайтеру для кредитных классов; уровень и ставку не меняет
    cbf, cb_trust = _trust_credit(clean.get("credit_report"), upload.get("credit_report"), session_missing)
    if cbf:
        classes = {cls} | set(m.get("product_classes") or []) | {p["class_code"] for p in D["parts"]["items"]}
        D["borrower"] = _borrower_block(st, cbf, cb_trust, classes)
        _with_clauses(D["decision"], [{"code": "c_" + c["code"], "params": c["params"]}
                                      for c in D["borrower"]["checks"]])


def _finish_objects(con, B) -> None:
    """Парк ТС: расчёт по каждому объекту и итоги договора; иначе — без объектов."""
    if B.clean.get("objects") and not B.multi:
        # парк ТС (02.10.2026): договор посчитан как один объект выше (осмотр, документы, сверки, аналитика),
        # дальше — каждый объект своим расчётом и итоги договора по объектам
        model_ids = set(B.upload.get("views") or {}) if B.ai_ok else set()
        X = {"ref": B.ref, "st": B.st, "m": B.m, "o": B.o, "cls": B.cls, "product": B.product, "tpl": B.tpl,
             "kind": B.kind,
             "class_hint": B.upload.get("class_hint") or "", "special": B.special_product, "y": B.y, "term": B.term,
             "rate_type": B.rate_type, "min_info": B.min_info,
             "no_products": not B.product and ctpl.products_count(con, B.cls) == 0,
             "fork_in": B.fork_in, "fs": B.fs, "fr": B.fr, "region_ra": B.region_ra, "recognized": B.recognized,
             "ai_ok": B.ai_ok, "model_ids": model_ids, "damages": B.damages, "upload_files": B.upload_files,
             "risk_in": dict(B.risk_in),
             "docs": B.docs, "doc_checks": B.doc_checks, "errors": B.block_errors,
             # для пересчёта мероприятий от премии договора по объектам
             "ctx": B.ctx, "group_ra": B.group_ra, "kind_ra": B.kind_ra, "location": B.location,
             "statutory": B.statutory,
             "th": B.th, "meas_codes": veh_measure_codes(B.tpl, B.veh_group, B.group_ra)}
        _apply_objects(con, B.clean, B.D, X)
    else:
        B.D["objects"], B.D["objects_total"] = [], None


def _text_lang(s: str) -> Optional[str]:
    """Язык короткого описания: узбекские буквы (ў қ ғ ҳ, oʻ gʻ) — uz; кириллица — ru; латиница — en/uz."""
    s = str(s or "")
    if re.search(r"[ўқғҳЎҚҒҲ]", s):
        return "uz-cyrl"                     # узбекская кириллица: для акта на латинице — другой алфавит
    if re.search(r"[А-Яа-яЁё]", s):
        return "ru"
    if re.search(r"(o|g)[ʻ'‘`]|\b(uchun|va|mahsulot\w*|bino\w*|ombor\w*|uskuna\w*|ishlab|chiqarish|yer|maydon\w*)\b",
                 s, re.I):
        return "uz"
    if re.search(r"[A-Za-z]", s):
        return "en"
    return None


def object_text_info(recognized: list, upload: dict, req: Optional[dict], ct: Optional[dict]) -> dict:
    """
    Описание объекта из документа: исходный текст, перевод модели (для скана запроса филиала — если модель
    его вернула; язык перевода = язык загрузки), откуда текст. Перевод правилами не выдумывается.
    """
    best = preferred(recognized, "object_type") or {}
    doc_src = best.get("source") in ("document", "document_ai")
    brf = (upload.get("branch_request") or {}).get("fields") or {}
    ctf = (upload.get("contract") or {}).get("fields") or {}
    original = (best.get("value") if doc_src else None) or brf.get("object_description") \
        or ctf.get("object_description") or (req or {}).get("object_description") \
        or (ct or {}).get("object_description")
    translated = brf.get("object_description_translated") or ctf.get("object_description_translated")
    return {"original": original, "translated": translated,
            "translated_lang": tx.lang_of(upload.get("lang")) if translated else None,
            "original_lang": _text_lang(original) if original else None,
            "from_document": bool(original) and (doc_src or not best),
            "scan": (upload.get("branch_request") or {}).get("source") == "photo"}


def _shots_upload(con, sid: Optional[str], owner: str) -> Optional[dict]:
    """Своя живая загрузка снимков объявлений (kind = market) или None."""
    if not sid:
        return None
    rows = db.rows(con, "SELECT result_json FROM act_uploads WHERE id=? AND owner_key=? AND expires_at > ?",
                   sid, owner, _iso(_now()))
    res = json.loads(rows[0]["result_json"] or "{}") if rows else {}
    return res if res.get("kind") == "market" else None


def fx_offline(con, as_of: date) -> Optional[dict]:
    """Курс без сети: только ручной курс заказчика из настроек оценки (valuation_settings.fx_rate_manual)."""
    raw = valuation.setting(con, valuation.SETTING_FX_MANUAL)
    try:
        v = float(str(raw).replace(" ", "").replace(",", ".")) if raw not in (None, "") else 0.0
    except ValueError:
        v = 0.0
    return {"rate": v, "by": "manual_setting", "as_of": as_of.isoformat()} if v > 0 else None


def market_block(con, mk: dict, owner: str, st: dict, m: dict, o: dict, recognized: list, kind: Optional[str],
                 cls: str, group: str, y: Optional[int]) -> dict:
    """
    Оценка по объявлениям для акта (в сеть не ходит). Объявления — из ввода сотрудника; источник каждого
    сверяется со своей загрузкой снимков (am.trust_listings), правки сотрудника считаются по всей загрузке.
    Курс: из загрузки снимков (он получен из источника проекта) → курс ЦБ, который экран получил от сервера
    (fx.by = cbu, сверка с памятью сервера) → ручной курс заказчика в настройках → курс, введённый сотрудником;
    иначе доллары не считаются.
    """
    shots = _shots_upload(con, mk.get("shots_session"), owner)
    items = [dict(r) for r in mk["listings"]]
    src = am.trust_listings(items, shots)
    edits = src.pop("edits")
    shot_date = date.today()
    if shots and shots.get("shot_date"):
        try:
            shot_date = date.fromisoformat(shots["shot_date"])
        except ValueError:
            pass
    claim = mk.get("fx_claim")
    fx = (shots or {}).get("fx") if ((shots or {}).get("fx") or {}).get("by") in ("cbu", "manual_setting") else None
    if not fx and claim and claim.get("by") == "cbu":
        fx = fx_verify(claim)
        fx = fx if fx["by"] == "cbu" else (fx_offline(con, shot_date) or fx)
    # ручной курс настроек сверяется с настройками же: если его там уже нет, курс экрана не берём
    fx = fx or fx_offline(con, shot_date)
    # курс сотрудника — только тот, что он ввёл сам (fx.by = employee или старое поле usd_rate)
    emp_rate = claim["rate"] if claim and claim.get("by") == "employee" else mk.get("usd_rate")
    if not fx and emp_rate:
        fx = {"rate": emp_rate, "by": "employee", "as_of": (claim or {}).get("as_of") or date.today().isoformat()}
    rate = (fx or {}).get("rate")
    est = ae.market_estimate(items, declared=m["object_value"], sum_insured=m["sum_insured"], settings=st,
                             shot_date=shot_date, usd_rate=rate)
    if shots:
        # медиана без правок: объявления, как их прочитала модель, по тем же правилам и курсу
        orig = [dict(r, source="shot") for r in shots.get("listings") or []]
        est0 = ae.market_estimate(orig, settings=st, shot_date=shot_date, usd_rate=rate)
        est["median_original"] = est0["median"]
    q = {"brand": (preferred(recognized, "brand") or {}).get("value"),
         "model": (preferred(recognized, "model") or {}).get("value"),
         "year": y, "object_kind": kind, "class_code": cls, "group": group}
    q.update({k: v for k, v in ((shots or {}).get("query") or {}).items() if v and not q.get(k)})
    # ссылки поиска собираются при показе акта на его языке (по запросу query); сервер по ним не ходит
    est.update(fx=fx, query=q, shots_session=mk.get("shots_session"),
               shots_missing=bool(mk.get("shots_session")) and shots is None,
               source={**src, "shots": sum(1 for r in items if r.get("source") == "shot" and not r.get("removed"))},
               edits=edits, dropped_pd=mk.get("dropped_pd") or 0)
    return est
