"""POST /act/photos: приём файлов, разбор документов с текстом, распознавание сканов, запись загрузки, ответ."""
import json
import shutil
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pymupdf

from .. import act_engine as ae
from .. import act_extras as ax
from .. import branch_request as br
from .. import class_templates as ctpl
from .. import contract_read as cr
from .. import credit_report as crr
from .. import act_texts as tx
from .. import db, llm
from ..act_texts import t

from .common import (cleanup, DOC_FMTS, ensure_tables, _fail, FMT_MIME, HINT_CLASSES, _iso, MAX_BYTES, _now,
    PHOTO_TTL_SEC, _reply, _who)
from .files import check_content, _format_of
from .recognize import _class_of, contract_text_model, pd_like, recognize, recognized_view, vehicle_autofill
from .regions import region_code
from .documents import br_clean, branch_view, contract_view, cr_clean, credit_report_view, cross_view, ct_clean


FILE_KINDS = ("credit_report", "object", "document")


def parse_kinds(raw: Any, n: int) -> tuple:
    """Поле формы kinds (недоверенный ввод) → ({номер файла: вид}, ошибка | None). Пусто — {}."""
    raw = str(raw or "").strip()
    if not raw:
        return {}, None
    if len(raw) > 2000:
        return {}, "слишком длинно"
    try:
        data = json.loads(raw)
    except ValueError:
        return {}, "не JSON"
    if not isinstance(data, dict):
        return {}, "объект {номер файла: вид}"
    out = {}
    for k, v in data.items():
        try:
            i = int(str(k).strip())
        except ValueError:
            return {}, f"номер файла «{str(k)[:10]}» — целое"
        if not 1 <= i <= n:
            return {}, f"номер файла {i} — от 1 до {n}"
        if v not in FILE_KINDS:
            return {}, f"вид файла {i} — " + ", ".join(FILE_KINDS)
        out[i] = v
    return out, None


def _photos(request, user, owner, lang, files, class_code, product_code, limits, sid, folder, inclusive=True):
    saved, rejected = _save_files(files, folder, limits, lang)
    if not saved:
        shutil.rmtree(folder, ignore_errors=True)
        return _fail(request, t("ph_none", lang), 422, rejected=rejected, ai=False,
                     warning=t("warn_pd", lang))

    with db.tx() as con:
        ensure_tables(con)
        cls = _class_of(con, class_code, product_code)
    parsed, parse_errors, t_docs = _parse_docs(saved, cls, limits, inclusive)
    m = _model_pass(saved, parsed, limits, lang, inclusive)          # m — ответ модели, привязанный к файлам
    doc_fields, prefill, doc_notes, dropped_doc, doc_list = _doc_values(saved, parsed, m.views, m.doc_kinds, lang)
    brq, dropped_br = _branch_from(saved, parsed, m.rec, m.model_to_id, m.fields, prefill)
    ctr, ct_ai, n = _contract_from(saved, parsed, m.rec, m.model_to_id, m.fields, prefill, doc_fields, limits,
                                   lang, inclusive, t_docs)
    dropped_br += n
    cbr, n = _credit_from(saved, parsed, m.rec, m.model_to_id)
    dropped_br += n
    if ctr:
        # подсказки полей осмотра, взятые из файла договора, — с пометкой «из договора» (день 7.1)
        for v in prefill.values():
            if ctr.get("file") and v.get("file") == ctr["file"]:
                v["doc"] = cr.KIND
        ctr["found"], ctr["missing"] = cr.found_missing(ctr["fields"])
        ctr["essentials"] = cr.essentials(ctr["fields"])
        ctr["ai"] = ct_ai
    # бланк договора (пустые поля) с запросом не сверяется: сверять нечего — как с заявлением
    cross = cr.cross_check(brq["fields"], ctr["fields"], float(limits.get("_tolerance") or 1000)) \
        if brq and ctr and not ctr["fields"].get("is_template") else None
    if "region" in prefill:
        prefill["region"]["code"] = region_code(prefill["region"]["value"])
    all_fields = m.fields + doc_fields
    # автозаполнение по ТС (02.10.2026): техпаспорт → подпись, год, подгруппа, топливо, характеристики; фото →
    # категория и топливо с уверенностью; ПД владельца сюда не попадают; модели нет — только техпаспорт
    with db.tx() as con:
        veh_view, veh_items = vehicle_autofill(con, parsed, m.rec, all_fields, m.views, m.model_to_id, lang)
    parsed_ok = sum(1 for r in parsed.values() if r.get("text_layer"))

    with db.tx() as con:
        ensure_tables(con)
        cleanup(con)
        kind, hint, group, need, missing = _object_class(con, cls, m.rec, brq, ctr, m.views)
        stored = {"ai": bool(m.rec.get("ok")), "reason": m.rec.get("reason"), "views": m.views,
                  "document_kinds": m.doc_kinds, "fields": all_fields, "damages": m.damages,
                  "object_kind": kind, "class_hint": hint, "condition": m.rec.get("condition"),
                  "files": len(saved), "photo_files": len(m.model_files), "parsed_docs": parsed_ok,
                  "prefill": prefill, "doc_notes": doc_notes, "not_sent": m.not_sent, "lang": lang,
                  "branch_request": brq, "contract": ctr, "credit_report": cbr,
                  # только коды и характеристики ТС (без ПД): категория и подсказки автозаполнения
                  "vehicle_category": veh_view, "prefill_fields": veh_items}
        now = _now()
        # в базе о файле — только порядковый номер, формат, размер и путь: имени файла нет
        keep = ("id", "index", "fmt", "mime", "size", "path")
        con.execute("INSERT INTO act_uploads (id, owner_key, user_id, files_json, result_json, created_at, "
                    "expires_at) VALUES (?,?,?,?,?,?,?)",
                    (sid, owner, (user or {}).get("id") or 0,
                     json.dumps([{k: f[k] for k in keep} for f in saved], ensure_ascii=False),
                     json.dumps(stored, ensure_ascii=False), _iso(now),
                     _iso(now + timedelta(seconds=PHOTO_TTL_SEC))))
        view_count = {}
        for v in m.views.values():
            view_count[v] = view_count.get(v, 0) + 1
        # в журнал — только счётчики: ни имён файлов, ни распознанных значений
        db.audit(con, _who(user, owner), "акт: фото загружены", f"act_upload:{sid}",
                 {"files": len(saved), "rejected": len(rejected), "ai": bool(m.rec.get("ok")),
                  "fields": len(m.fields), "damages": len(m.damages), "views": view_count,
                  "not_sent": len(m.not_sent), "dropped_pd": (m.rec.get("dropped") or 0) + dropped_doc + dropped_br,
                  "parsed_docs": parsed_ok, "doc_fields": len(doc_fields),
                  # запрос филиала — только признак и число строк: ни названий сторон, ни сумм
                  "branch_request": bool(brq), "branch_rows": (brq or {}).get("rows_found") or 0,
                  # договор — только признак, источник и счётчики: ни сторон, ни сумм, ни номера
                  "contract": bool(ctr), "contract_source": (ctr or {}).get("source"),
                  "contract_found": len((ctr or {}).get("found") or []),
                  "contract_ai": len((ctr or {}).get("field_sources") or {}),
                  "cross_differs": (cross or {}).get("differs", 0),
                  # отчёт бюро — только признак и источник: ни названия, ни ИНН, ни сумм
                  "credit_report": bool(cbr), "credit_report_source": (cbr or {}).get("source"),
                  "credit_scan_withheld": len(m.withheld), "credit_scan_dropped": m.scan_dropped})
        for e in parse_errors:
            db.audit(con, _who(user, owner), "акт: документ не разобран", f"act_upload:{sid}", e)
    message, notes = _photos_notes(lang, limits, m, parsed, parsed_ok, doc_fields, doc_notes, cbr, ct_ai)
    model_ids = {f["id"] for f in m.model_files}
    return _reply(request, {
        "ok": True, "session": sid, "lang": lang,
        "files": [{"id": f["id"], "index": f["index"], "name": f["name"], "view": m.views.get(f["id"]),
                   "view_label": tx.label(tx.VIEW_LABELS, m.views[f["id"]], lang) if f["id"] in m.views else None,
                   "document_kind": m.doc_kinds.get(f["id"]),
                   "read_by_ai": bool(m.rec.get("ok")) and f["id"] in model_ids and f["index"] not in m.not_sent,
                   "parsed": bool((parsed.get(f["id"]) or {}).get("text_layer")),
                   "format": f["fmt"], "kind_marked": m.marked.get(f["index"]),
                   "credit_scan_withheld": f in m.withheld} for f in saved],
        "rejected": rejected,
        "recognized": recognized_view(all_fields, lang, group=group),
        "damages": m.damages,
        "object_kind": ({"code": kind, "label": tx.label({k: v[1] for k, v in tx.OBJECT_KINDS.items()}, kind, lang)}
                        if kind else None),
        "class_hint": hint,
        "suggest_classes": HINT_CLASSES.get(hint or "", []),
        "condition": m.rec.get("condition"),
        "group": group,
        "required_views": [{"code": v, "label": tx.label(tx.VIEW_LABELS, v, lang)} for v in need],
        "missing_views": [{"code": v, "label": tx.label(tx.VIEW_LABELS, v, lang)} for v in missing],
        "ai": bool(m.rec.get("ok")),
        "message": message,
        "notes": notes,
        "not_sent": m.not_sent,
        "documents": doc_list,
        "prefill": _with_vehicle(prefill_view(prefill, lang) if prefill else None, veh_items),
        # автозаполнение по ТС: тот же перечень списком; vehicle_category — категория и топливо с фото
        "prefill_fields": veh_items,
        "vehicle_category": veh_view,
        "branch_request": branch_view(brq, lang),
        "contract": contract_view(ctr, lang),
        "cross_check": cross_view(cross, lang),
        "credit_report": credit_report_view(cbr, lang),
        "warning": t("warn_pd", lang),
        "expires_in_hours": PHOTO_TTL_SEC // 3600,
    })


def _model_file(model_to_id: dict, block: dict) -> Optional[str]:
    """id загруженного файла, с которого модель прочитала блок; номер не указан, а файл один — этот файл."""
    return model_to_id.get(block.get("file")) or (model_to_id.get(1) if len(model_to_id) == 1 else None)


def _scan_rows(items: list, pre: dict, file_no, fid: Optional[str], fields: list, prefill: dict) -> int:
    """
    Строки документа со скана (запрос филиала, договор) → распознанное с источником «документ» и предзаполнение.
    Похожее на ПД отбрасывается, повтор (тот же ключ и значение) пропускается. Возвращает число отброшенных ПД.
    """
    dropped = 0
    for it in items:
        if pd_like(it["key"], it["value"]):
            dropped += 1
            continue
        if any(x["key"] == it["key"] and x["value"] == it["value"] for x in fields):
            continue
        fields.append({"key": it["key"], "value": it["value"], "source": "document", "file": file_no,
                       "note": None, "file_id": fid})
    for k, v in pre.items():
        prefill.setdefault(k, {"value": v, "source": "document", "file": fid})
    return dropped


def _save_files(files: list, folder: Path, limits: dict, lang: str) -> tuple:
    """Принимает файлы: размер, формат, содержимое; годные — на диск. Возвращает (принятые, отклонённые)."""
    saved, rejected = [], []
    for i, up in enumerate(files, start=1):
        orig = Path(up.filename or "").name
        name = llm.mask_pd(orig)[:120] or f"file {i}"        # только для ответа, в базу не пишется
        blob = up.file.read(MAX_BYTES + 1)
        if not blob:
            rejected.append({"index": i, "name": name, "error": t("ph_empty_file", lang)})
            continue
        if len(blob) > MAX_BYTES:
            rejected.append({"index": i, "name": name,
                             "error": t("ph_too_big", lang, mb=MAX_BYTES // (1024 * 1024))})
            continue
        fmt = _format_of(blob)
        info = {}
        if fmt in DOC_FMTS:
            code, info = ax.zip_check(blob, limits)
            err = t(code, lang) if code else None
        else:
            err = check_content(blob, fmt, limits, lang) if fmt else t("ph_format", lang)
        if err:
            rejected.append({"index": i, "name": name, "error": err})
            continue
        folder.mkdir(parents=True, exist_ok=True)
        fid = f"f{len(saved) + 1}"
        path = folder / f"{fid}.{fmt}"
        path.write_bytes(blob)
        pages = None
        if fmt == "pdf":
            with pymupdf.open(stream=blob, filetype="pdf") as pdoc:
                pages = pdoc.page_count
        saved.append({"id": fid, "index": i, "name": name, "orig_name": orig, "fmt": fmt, "mime": FMT_MIME[fmt],
                      "size": len(blob), "path": db.stored_path(path), "blob": blob, "full": path,
                      "macros": bool(info.get("macros")), "pages": pages})
    return saved, rejected


def _parse_docs(saved: list, cls: Optional[str], limits: dict, inclusive: bool) -> tuple:
    """Документы с текстовым слоем — парсерами в пределах limits.doc_*.
    Возвращает (разбор по id файла, ошибки разбора, начало отсчёта сроков)."""
    # документы с текстовым слоем (DOCX, XLSX, PDF с текстом) — парсерами, без модели; сканы — модели.
    # Пределы текста и сроки (limits.doc_*): файл — doc_parse_sec (PDF с текстом — doc_file_sec_pdf: длинный
    # договор), все документы запроса — doc_parse_total_sec; одновременно на сервере разбирается не больше
    # ax.PARSE_SLOTS документов.
    parsed, parse_errors = {}, []
    dl = ax.doc_limits(limits)
    t_docs = time.monotonic()
    for f in saved:
        if f["fmt"] not in DOC_FMTS and f["fmt"] != "pdf":
            continue
        left = float(dl["doc_parse_total_sec"]) - (time.monotonic() - t_docs)
        res = None
        if left <= 0:
            res = {"text_layer": False, "status": "timeout", "kind": None, "items": [], "prefill": {},
                   "notes": ["doc_timeout"]}
        elif not ax.parse_slot(left):
            res = {"text_layer": False, "status": "busy", "kind": None, "items": [], "prefill": {},
                   "notes": ["doc_busy"]}
        else:
            try:
                left = float(dl["doc_parse_total_sec"]) - (time.monotonic() - t_docs)
                with db.tx() as con:
                    per_file = float(dl["doc_file_sec_pdf"] if f["fmt"] == "pdf" else dl["doc_parse_sec"])
                    res = ax.parse_document_limited(con, f["full"], cls or "", dl,
                                                    min(per_file, max(left, 0.0)), inclusive)
            except Exception as e:       # ошибка разбора не роняет загрузку, но и не глотается
                parse_errors.append({"format": f["fmt"], "error": type(e).__name__})
                res = {"text_layer": False, "kind": None, "items": [], "prefill": {}, "notes": ["doc_unreadable"]}
            finally:
                ax.parse_slot_release()
        if res.get("status") in ("timeout", "busy"):
            parse_errors.append({"format": f["fmt"], "error": res["status"]})
        if f["fmt"] == "pdf" and not res["text_layer"] and not set(res["notes"]) & {
                "doc_unreadable", "doc_timeout", "doc_busy"}:
            if (f.get("pages") or 0) <= int(limits["pdf_max_pages"]):
                continue                 # скан без текста — его читает модель
            res["notes"].append("doc_scan_pages")   # текст есть не на первых страницах: длинный скан модели не отдаём
        if f["macros"]:
            res["notes"].append("doc_macros")
        parsed[f["id"]] = res
    return parsed, parse_errors, t_docs


def _model_pass(saved: list, parsed: dict, limits: dict, lang: str, inclusive: bool) -> SimpleNamespace:
    """Сканы и фото — одним запросом к модели; ответ привязан к id файлов; скан отчёта бюро без разрешения отброшен."""
    model_files = [f for f in saved if f["id"] not in parsed]
    # сканы и фото, которые сотрудник пометил «отчёт бюро»: в отчёте кредитная история — модели не отдаём,
    # пока администратор не разрешил (credit_report.allow_scan)
    credit_scan = bool(limits.get("_credit_scan"))
    marked = limits.get("_kinds") or {}
    withheld = [] if credit_scan else [f for f in model_files if marked.get(f["index"]) == crr.KIND]
    model_files = [f for f in model_files if f not in withheld]
    if model_files:
        rec = recognize(model_files, lang, limits, inclusive)
    else:
        rec = {"ok": False, "reason": None, "sent": [], "not_sent": []}
    sent = rec.get("sent") or []
    # номер файла в запросе к модели → id загруженного файла
    model_to_id = {k + 1: model_files[i]["id"] for k, i in enumerate(sent)}
    # модель узнала отчёт бюро на снимке, а сканы не разрешены: значения отчёта и всё, что пришло с этого снимка,
    # отбрасываются (картинка уже ушла вместе с остальными — поэтому экран просит пометить такой файл заранее)
    scan_dropped = bool(withheld)
    if not credit_scan:
        cr_files = {k for k, v in (rec.get("document_kinds") or {}).items() if v == crr.KIND}
        if rec.get("credit_report") and (rec["credit_report"].get("file") or 0) > 0:
            cr_files.add(rec["credit_report"]["file"])
        if cr_files or rec.get("credit_report"):
            scan_dropped = True
            rec = dict(rec, credit_report=None,
                       fields=[x for x in rec.get("fields") or [] if x.get("file") not in cr_files],
                       damages=[x for x in rec.get("damages") or [] if x.get("file") not in cr_files])
    views = {model_to_id[k]: v for k, v in (rec.get("views") or {}).items() if k in model_to_id}
    doc_kinds = {model_to_id[k]: v for k, v in (rec.get("document_kinds") or {}).items() if k in model_to_id}
    fields = []
    for f in rec.get("fields") or []:
        fields.append({**f, "file_id": model_to_id.get(f.get("file"))})
    damages = [{"what": d["what"], "where": d.get("where"), "file": model_to_id.get(d.get("file"))}
               for d in rec.get("damages") or []]
    not_sent = [model_files[i]["index"] for i in rec.get("not_sent") or []]
    doc_kinds = {k: (tx.label(tx.DOC_KIND_LABELS, v, lang) if v in (br.KIND, cr.KIND, crr.KIND) else v)
                 for k, v in doc_kinds.items()}
    for f in withheld:
        doc_kinds[f["id"]] = tx.label(tx.DOC_KIND_LABELS, crr.KIND, lang)
    return SimpleNamespace(model_files=model_files, withheld=withheld, marked=marked, rec=rec, model_to_id=model_to_id,
                           scan_dropped=scan_dropped, views=views, doc_kinds=doc_kinds, fields=fields, damages=damages,
                           not_sent=not_sent)


def _doc_values(saved: list, parsed: dict, views: dict, doc_kinds: dict, lang: str) -> tuple:
    """Значения разобранных документов (источник «документ», ПД отбрасываются), предзаполнение, пометки, перечень."""
    # значения из разобранных документов: источник «документ», пометка «проверьте», ПД отбрасываются
    doc_fields, prefill, doc_notes, dropped_doc, doc_list = [], {}, [], 0, []
    for f in saved:
        res = parsed.get(f["id"])
        if res is None:
            continue
        n_before = len(doc_fields)
        if res.get("text_layer"):
            views[f["id"]] = "document"
            if res.get("kind"):
                doc_kinds[f["id"]] = tx.label(tx.DOC_KIND_LABELS, res["kind"], lang)
        for it in res.get("items") or []:
            if pd_like(it["key"], it["value"]):
                dropped_doc += 1
                continue
            if any(d["key"] == it["key"] and d["value"] == it["value"] for d in doc_fields):
                continue
            doc_fields.append({"key": it["key"], "value": it["value"], "source": "document", "file": None,
                               "file_id": f["id"], "note": t("doc_parsed_note", lang), "parsed": True})
        for k, v in (res.get("prefill") or {}).items():
            if k not in prefill:
                prefill[k] = {"value": v, "source": "document", "file": f["id"]}
        for c in res.get("notes") or []:
            if c not in doc_notes:
                doc_notes.append(c)
        doc_list.append({"file": f["id"], "index": f["index"], "kind": res.get("kind"),
                         "kind_label": tx.label(tx.DOC_KIND_LABELS, res["kind"], lang) if res.get("kind") else None,
                         "text_layer": bool(res.get("text_layer")), "values": len(doc_fields) - n_before,
                         "notes": [t(c, lang) for c in res.get("notes") or []]})
    return doc_fields, prefill, doc_notes, dropped_doc, doc_list


def _branch_from(saved: list, parsed: dict, rec: dict, model_to_id: dict, fields: list, prefill: dict) -> tuple:
    """Запрос филиала: из файла с текстом или со скана, первый найденный. Возвращает (блок | None, отброшено ПД)."""
    # запрос филиала: из файла с текстом (разобран выше) или со скана (ответ модели); первый найденный
    brq, dropped_br = None, 0
    for f in saved:
        got = (parsed.get(f["id"]) or {}).get("branch_request")
        if got:
            brq = dict(got, source="document", file=f["id"])
            break
    if not brq and rec.get("branch_request"):
        mb = rec["branch_request"]
        fid = _model_file(model_to_id, mb)
        brq = dict(mb, source="photo", file=fid)
        # строки бланка со скана — в распознанное (источник «документ»), с той же проверкой на ПД
        dropped_br += _scan_rows(br.items(mb["fields"]), br.prefill(mb["fields"], br.region_in(mb["fields"])),
                                 mb.get("file"), fid, fields, prefill)
    if brq:
        brq["fields"], n = br_clean(brq["fields"])
        dropped_br += n
    return brq, dropped_br


def _contract_from(saved: list, parsed: dict, rec: dict, model_to_id: dict, fields: list, prefill: dict,
                   doc_fields: list, limits: dict, lang: str, inclusive: bool, t_docs: float) -> tuple:
    """Договор страхования: из файла с текстом (мало полей — дочитывает модель) или со скана.
    Возвращает (блок | None, сведения о дочитывании моделью, отброшено ПД)."""
    dropped_br = 0
    # договор страхования: из файла с текстом (разобран выше) или со скана (ответ модели); первый найденный
    ctr, texts = None, {}
    for f in saved:
        res = parsed.get(f["id"]) or {}
        if res.get("_text") is not None:
            texts[f["id"]] = res.pop("_text")        # текст договора в базу не пишется: только для модели
        if not ctr and res.get("contract"):
            ctr = dict(res["contract"], source="document", file=f["id"], pages=f.get("pages"))
    if not ctr and rec.get("contract"):
        mc = rec["contract"]
        fid = _model_file(model_to_id, mc)
        ctr = dict(mc, source="photo", file=fid, pages=None, truncated=False)
        # условия договора со скана — в распознанное (источник «документ»), с той же проверкой на ПД
        dropped_br += _scan_rows(cr.items(mc["fields"]), cr.prefill(mc["fields"]), mc.get("file"), fid, fields,
                                 prefill)
    ct_ai = {"asked": False, "ok": None, "reason": None}
    if ctr and ctr["source"] == "document":
        ctr["fields"], n = ct_clean(ctr["fields"])
        dropped_br += n
        opts = limits.get("_contract") or ae.DEFAULT_SETTINGS["contract"]
        text = texts.get(ctr["file"])
        if opts.get("ai_assist") and text and not ctr["fields"].get("is_template") and cr.need_assist(ctr["fields"]) \
                and llm.enabled():
            left = float(limits["ai_deadline_sec"]) - (time.monotonic() - t_docs)
            ct_ai["asked"] = True
            got = contract_text_model(text, lang, limits, int(opts.get("ai_max_chars") or 30000), inclusive,
                                      deadline=max(5.0, left))
            ct_ai.update(ok=bool(got.get("ok")), reason=got.get("reason"))
            if got.get("ok"):
                filled = cr.merge_missing(ctr["fields"], got["contract"]["fields"])
                ctr["fields"], n = ct_clean(ctr["fields"])
                dropped_br += n
                filled = [k for k in filled if ctr["fields"].get(k) not in (None, "", [], {})]
                ctr["field_sources"] = {k: "document_ai" for k in filled}
                if filled:
                    ctr["source"] = "document_ai"
                    have = {(d["key"], d["value"]) for d in doc_fields}
                    for it in cr.items(ctr["fields"]):
                        if cr.item_field(it["key"]) not in filled or (it["key"], it["value"]) in have \
                                or pd_like(it["key"], it["value"]):
                            continue
                        doc_fields.append({"key": it["key"], "value": it["value"], "source": "document_ai",
                                           "file": None, "file_id": ctr["file"], "note": t("ct_ai_note", lang),
                                           "parsed": True})
                    for k, v in cr.prefill(ctr["fields"]).items():
                        if cr.item_field(k) in filled:
                            prefill.setdefault(k, {"value": v, "source": "document_ai", "file": ctr["file"]})
    elif ctr:
        ctr["fields"], n = ct_clean(ctr["fields"])
        dropped_br += n
    return ctr, ct_ai, dropped_br


def _credit_from(saved: list, parsed: dict, rec: dict, model_to_id: dict) -> tuple:
    """Отчёт кредитного бюро (КАТМ): из файла с текстом или со скана. Возвращает (блок | None, отброшено ПД)."""
    dropped = 0
    # отчёт кредитного бюро (КАТМ): файл с текстом (разобран правилами) или скан (ответ модели); первый найденный
    cbr = None
    for f in saved:
        got = (parsed.get(f["id"]) or {}).get("credit_report")
        if got:
            cbr = {"source": "document", "file": f["id"], "fields": got["fields"], "notes": list(got["notes"])}
            break
    if not cbr and rec.get("credit_report"):
        mk_ = rec["credit_report"]
        fid = _model_file(model_to_id, mk_)
        cbr = {"source": "photo", "file": fid, "fields": mk_["fields"],
               "notes": ["cr_individual"] if mk_["fields"].get("subject_type") == "individual" else []}
    if cbr:
        cbr["fields"], n = cr_clean(cbr["fields"])
        dropped += n
    return cbr, dropped


def _object_class(con, cls: Optional[str], rec: dict, brq: Optional[dict], ctr: Optional[dict], views: dict) -> tuple:
    """Вид объекта, подсказка класса, группа, нужные и недостающие ракурсы (по шаблону класса или группе объекта)."""
    kind = rec.get("object_kind")
    hint = rec.get("class_hint")
    if brq:
        # скан бланка модель видит как «документ»; вид объекта — из строки «объект страхования»
        bf = brq["fields"]
        if (not kind or kind == "other") and bf.get("object_kind"):
            kind = bf["object_kind"]
        if (not hint or hint == "other") and bf.get("class_hint"):
            hint = bf["class_hint"]
    if ctr and (not hint or hint == "other") and ctr["fields"].get("class_hint") in cr.PERSON_HINTS:
        hint = ctr["fields"]["class_hint"]    # договор личного страхования: подсказка класса 1 или 2
    kind_text = tx.OBJECT_KINDS[kind][0] if kind and tx.OBJECT_KINDS[kind][0] else ""
    group = ae.object_group(cls, kind_text, hint or "")
    seen = sorted(set(views.values()))
    # ракурсы — из шаблона класса (справочник class_templates); класса нет — по группе объекта
    need = ctpl.for_group(((ctpl.current(con, cls) or {}).get("template") or {}).get("required_views"), group) \
        if cls else None
    need = ae.required_views(group) if need is None else need
    missing = ae.missing_views(group, seen, need) if rec.get("ok") else list(need)
    return kind, hint, group, need, missing


def _photos_notes(lang: str, limits: dict, m: SimpleNamespace, parsed: dict, parsed_ok: int, doc_fields: list,
                  doc_notes: list, cbr: Optional[dict], ct_ai: dict) -> tuple:
    """Сообщение и примечания ответа /act/photos: что прочитала модель, что разобрано, что не отправлено."""
    notes = []
    if m.model_files:
        if m.rec.get("ok"):
            message = t("ph_ok", lang, n=len(m.fields)) if m.fields else t("ph_empty", lang)
        else:
            message = t("ph_ai_off", lang, reason=m.rec.get("reason") or t("ai_not_connected", lang))
        if parsed:
            notes.append(t("ph_docs_ok", lang, n=parsed_ok, k=len(doc_fields)))
    else:
        message = t("ph_docs_ok", lang, n=parsed_ok, k=len(doc_fields))
    if m.rec.get("ok") and m.not_sent:
        notes.append(t("ph_not_sent", lang, files=", ".join(str(n) for n in m.not_sent), mb=limits["ai_max_mb"]))
    notes += [t(c, lang) for c in doc_notes]
    if cbr:
        notes.append(t("cr_found", lang))
        if "cr_individual" in cbr["notes"] and t("cr_individual", lang) not in notes:
            notes.append(t("cr_individual", lang))
    if m.scan_dropped:
        notes.append(t("cr_scan_off", lang))
    if ct_ai["asked"] and not ct_ai["ok"]:
        notes.append(t("ct_ai_failed", lang, reason=ct_ai["reason"] or t("ai_error", lang)))
    return message, notes


def _with_vehicle(pf: Optional[dict], items: list) -> Optional[dict]:
    """Подсказки ТС в общий prefill под своими ключами (object_label, year, class_fields.veh_group, …): что уже
    подсказал документ (запрос, договор), не заменяется."""
    if not items:
        return pf
    out = dict(pf or {})
    for it in items:
        out.setdefault(it["field"], dict(it))
    return out


def prefill_view(prefill: dict, lang: str) -> dict:
    """Подсказка для шага 2: значения из документа с источником и пометкой «из документа, проверьте»
    (прочитанное моделью из текста договора — «прочитано моделью из текста, проверьте»)."""
    out = {}
    for k, v in prefill.items():
        mark = t("ct_ai_note", lang) if v.get("source") == "document_ai" else t("prefill_check", lang)
        out[k] = {**v, "label": tx.label(tx.FIELD_LABELS, k, lang), "check_label": mark}
        if v.get("doc") == cr.KIND:
            out[k]["source_label"] = t("from_contract", lang)
    return out
