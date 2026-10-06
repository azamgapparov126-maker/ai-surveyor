"""
Адреса /act/*: роутер FastAPI и обработчики. Порядок регистрации прежний — от него зависит разбор путей
(/act/settings и /act/{id}.docx раньше /act/{id}).
"""
import json
import secrets
import shutil
from datetime import timedelta
from typing import List

from fastapi import APIRouter, Body, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute

from .. import act_engine as ae
from .. import class_templates as ctpl
from .. import act_market as am
from .. import act_texts as tx
from .. import db, guest
from ..act_texts import t

from .common import (ACT_TTL_SEC, ADMIN, current_custom, DIR, ensure_tables, _fail, GUEST_PHOTOS, _iso, _lang,
    _load_act, load_settings, MAX_BODY, MAX_FILES, _now, _reply, _user, _who)
from .validate import validate
from .photos import parse_kinds, _photos
from .market import _market_query, MAX_SHOT_BODY, _shots
from .build import build_data
from .view import render
from .export import build_docx, build_pdf, scoring_png
from .send import act_send


router = APIRouter()


class _BodyLimitRoute(APIRoute):
    """
    Размер тела проверяется по Content-Length ДО чтения: FastAPI разбирает multipart раньше, чем
    вызывает обработчик, поэтому проверка внутри обработчика опоздала бы. Нет Content-Length — 411.
    """
    max_body = MAX_BODY

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def limited(request: Request):
            raw = request.headers.get("content-length")
            lang = _lang(request)
            if raw is None:
                return _fail(request, t("ph_length_required", lang), 411)
            try:
                size = int(raw)
            except ValueError:
                return _fail(request, t("ph_length_required", lang), 400)
            if size > self.max_body:
                return _fail(request, t("ph_body_too_big", lang, mb=self.max_body // (1024 * 1024)), 413)
            return await handler(request)

        return limited


def _limit_reply(request: Request, message: str, res: dict) -> JSONResponse:
    """429 в том же виде, что у гостевого лимита app/guard.py."""
    out = _fail(request, message, 429, limit=res["limit"], window_hours=1,
                retry_after_sec=res["retry_after"], guest=True)
    out.headers["Retry-After"] = str(res["retry_after"])
    return out


def act_photos(request: Request, files: List[UploadFile] = File(...), lang: str = Form(""),
               class_code: str = Form(""), product_code: str = Form(""), kinds: str = Form("")):
    """
    Фото объекта и снимки документов (до 10 файлов, до 15 МБ; JPG, PNG, PDF). Файлы хранятся 24 часа.
    Одним запросом уходят в языковую модель; ответ проверяется по схеме.
    kinds (необязательно) — JSON {номер файла с 1: "credit_report" | "object" | "document"}: вид, который выбрал
    сотрудник. Файл «отчёт бюро» (картинка или скан без текста) при credit_report.allow_scan = false в модель не уходит.
    Обычный def: FastAPI выполняет его в пуле потоков — распознавание (сеть до 25 с) и пережатие
    не останавливают сервер с одним процессом uvicorn.
    """
    user = _user(request)
    owner = guest.owner_of(request, user)
    lang = _lang(request, lang)
    if not owner:
        return _fail(request, "Не удалось опознать сессию — откройте приложение заново", 400)
    if len(files) > MAX_FILES:
        return _fail(request, t("ph_too_many", lang, n=MAX_FILES), 413)
    kind_map, kind_err = parse_kinds(kinds, len(files))
    if kind_err:
        return _fail(request, t("ph_kinds_bad", lang), 422, errors={"kinds": kind_err})
    with db.tx() as con:
        ensure_tables(con)
        st = load_settings(con)
    limits = st["limits"]
    inclusive = bool(st["request_check"]["term_inclusive"])
    limits = dict(limits, _contract=st["contract"], _tolerance=float(st["request_check"]["premium_tolerance"]),
                  _credit_scan=bool((st.get("credit_report") or {}).get("allow_scan")), _kinds=kind_map)
    if owner.startswith("g:"):
        n_max = int(limits["guest_photos_per_hour"])
        res = GUEST_PHOTOS.take(owner, len(files), n_max)
        if not res["ok"]:
            print("акт: лимит фото гостя", guest.short(owner), res["count"], "/", n_max, flush=True)
            return _limit_reply(request, t("ph_guest_limit", lang, n=n_max), res)
    class_code = str(class_code or "").strip()[:10]
    product_code = str(product_code or "").strip()[:10]
    sid = secrets.token_hex(12)
    folder = DIR / sid
    try:
        return _photos(request, user, owner, lang, files, class_code, product_code, limits, sid, folder, inclusive)
    except Exception:
        # запись в базу или распознавание упали — папка с фото не должна остаться сиротой
        shutil.rmtree(folder, ignore_errors=True)
        raise


router.add_api_route("/act/photos", act_photos, methods=["POST"], route_class_override=_BodyLimitRoute)


@router.get("/act/market/links")
def act_market_links(request: Request, session: str = "", lang: str = "", brand: str = "", model: str = "",
                     year: str = "", object_kind: str = "", class_code: str = "", region: str = ""):
    """
    Ссылки поиска на площадках для браузера сотрудника (OLX, avtoelon.uz, uybor.uz, joymee.uz по виду объекта)
    и подсказка, что снять на снимке. Сервер только составляет адреса и сам по ним не ходит.
    """
    lang = _lang(request, lang)
    q, errs = _market_query(request, session, brand, model, year, object_kind, class_code)
    if errs:
        return _fail(request, "Проверьте поля: " + ", ".join(sorted(errs)), 422, errors=errs)
    q["group"] = am.group_of(q.get("object_kind"), q.get("class_code"))
    links = am.search_links(q, lang)
    if not links:
        return _fail(request, t("mk_links_none", lang), 422, errors={"query": "brand, model или object_kind"})
    return _reply(request, {"ok": True, "lang": lang,
                            "query": {k: q.get(k) for k in ("brand", "model", "year", "object_kind", "class_code",
                                                            "group")},
                            "region": am._s(region, 80), "links": links, "hint": t("mk_hint", lang, n=am.MAX_SHOTS),
                            "shot_tips": am.shot_tips(lang), "max_shots": am.MAX_SHOTS,
                            "note": t("mk_server_note", lang), "warning": t("mk_warn", lang)})


class _ShotsBodyLimitRoute(_BodyLimitRoute):
    max_body = MAX_SHOT_BODY


def act_market_shots(request: Request, files: List[UploadFile] = File(...), session: str = Form(""),
                     lang: str = Form(""), site: str = Form(""), brand: str = Form(""), model: str = Form(""),
                     year: str = Form(""), object_kind: str = Form(""), usd_rate: str = Form("")):
    """
    Снимки экрана со списком объявлений (до 5 файлов JPG/PNG). Модель читает их одним запросом и возвращает
    объявления; сервер пересчитывает валюту, считает предварительную медиану и хранит снимки 24 часа.
    Гостевой лимит — по числу файлов (как у /act/photos), лимит обращений к модели — общий с распознаванием фото.
    """
    user = _user(request)
    owner = guest.owner_of(request, user)
    lang = _lang(request, lang)
    if not owner:
        return _fail(request, "Не удалось опознать сессию — откройте приложение заново", 400)
    if len(files) > am.MAX_SHOTS:
        return _fail(request, t("mk_too_many", lang, n=am.MAX_SHOTS), 413)
    site = str(site or "").strip().lower() or "other"
    errs = {}
    if site not in am.SITES:
        errs["site"] = "одно из: " + ", ".join(am.SITES)
    rate, rate_err = am.usd_rate_in(usd_rate)
    if rate_err:
        errs["usd_rate"] = rate_err
    q, qerrs = _market_query(request, session, brand, model, year, object_kind, "")
    errs.update(qerrs)
    if errs:
        return _fail(request, "Проверьте поля: " + ", ".join(sorted(errs)), 422, errors=errs)
    with db.tx() as con:
        ensure_tables(con)
        st = load_settings(con)
    limits = st["limits"]
    if owner.startswith("g:"):
        n_max = int(limits["guest_photos_per_hour"])
        res = GUEST_PHOTOS.take(owner, len(files), n_max)
        if not res["ok"]:
            print("акт: лимит снимков гостя", guest.short(owner), res["count"], "/", n_max, flush=True)
            return _limit_reply(request, t("ph_guest_limit", lang, n=n_max), res)
    sid = secrets.token_hex(12)
    folder = DIR / sid
    try:
        return _shots(request, user, owner, lang, files, site, q, rate, st, sid, folder)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


router.add_api_route("/act/market/shots", act_market_shots, methods=["POST"],
                     route_class_override=_ShotsBodyLimitRoute)


def _number(aid: str, created: str) -> str:
    return f"{created[:10].replace('-', '')}-{aid[:6].upper()}"


@router.post("/act/make")
def act_make(request: Request, body: dict = Body(...)):
    """Акт из пяти разделов. Тело и ответ — см. докстринг модуля и отчёт разработчика."""
    user = _user(request)
    owner = guest.owner_of(request, user)
    if not owner:
        return _fail(request, "Не удалось опознать сессию — откройте приложение заново", 400)
    if not isinstance(body, dict):
        return _fail(request, "Тело запроса — объект JSON", 422)
    lang = _lang(request, body.get("lang"))
    with db.tx() as con:
        ensure_tables(con)
        clean, errs = validate(con, body)
        if errs:
            db.audit(con, _who(user, owner), "акт: ошибка ввода", None, {"fields": sorted(errs)})
            return _fail(request, "Проверьте поля: " + ", ".join(sorted(errs)), 422, errors=errs)
        D = build_data(con, clean, owner, lang)
        aid = secrets.token_hex(8)
        now = _now()
        meta = {"id": aid, "created_at": _iso(now), "expires_at": _iso(now + timedelta(seconds=ACT_TTL_SEC))}
        meta["number"] = _number(aid, meta["created_at"])
        out = render(D, lang, meta)
    # сеть здесь не нужна: акт собирается по шаблонам (литературная связка моделью убрана 29.09.2026)
    with db.tx() as con:
        con.execute("INSERT INTO acts (id, owner_key, user_id, lang, tariff_version_id, settings_id, act_json, "
                    "created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (aid, owner, (user or {}).get("id") or 0, lang, D.get("tariff_version_id"),
                     D.get("settings_id"), json.dumps({"meta": meta, "data": D}, ensure_ascii=False, default=str),
                     meta["created_at"], meta["expires_at"]))
        db.audit(con, _who(user, owner), "акт сформирован", f"act:{aid}",
                 {"product": D["must"]["product_code"], "class": D["must"]["class_code"],
                  "level": D["risk"]["level"], "rate_mode": D["rate"]["mode"],
                  "decision": D["decision"]["code"], "photos": D["inspection"]["photos"],
                  "ai": D["inspection"]["ai"], "discrepancies": len(D["discrepancies"]),
                  "dropped_pd": D.get("dropped_pd") or 0, "sources_downgraded": D.get("sources_downgraded") or 0,
                  "lang": lang, "franchise": D["franchise"].get("status"),
                  "scenarios": bool((D.get("scenarios") or {}).get("available")),
                  "measures": len((D.get("measures") or {}).get("items") or []),
                  "market": (D.get("market") or {}).get("verdict"),
                  "market_used": (D.get("market") or {}).get("used"),
                  "contract_check": ((D.get("contract_check") or {}).get("summary") or {}).get("verdict"),
                  "cross_differs": (D.get("cross_check") or {}).get("differs", 0),
                  # скоринг и отчёт бюро — только класс и коды проверок: ни названий, ни ИНН, ни сумм
                  "scoring": (out.get("scoring") or {}).get("class_code"),
                  # парк ТС и особый регион — только число объектов и код: ни подписей, ни территории текстом
                  "objects": len(D.get("objects") or []), "region_scope": D["must"].get("region_scope"),
                  "borrower": bool(D.get("borrower")),
                  "borrower_checks": [c["code"] for c in (D.get("borrower") or {}).get("checks") or []]})
        for e in D.get("block_errors") or []:
            db.audit(con, _who(user, owner), "акт: блок не посчитан", f"act:{aid}", e)
    return _reply(request, out)


@router.get("/act/settings")
def act_settings_get():
    """Пороги лёгкого движка (уровень риска, поправки, износ) — чтение открыто, правка — администратор."""
    with db.tx() as con:
        ensure_tables(con)
        s = load_settings(con)
    return {"settings": s, "defaults": ae.DEFAULT_SETTINGS, "calibrated": ae.CALIBRATED}


@router.put("/act/settings")
def act_settings_put(request: Request, body: dict = Body(...)):
    """Новая версия настроек (старые остаются — акты хранят settings_id). Только администратор (guard)."""
    user = _user(request)
    if (user or {}).get("role") != ADMIN:
        return JSONResponse({"detail": "нужны права администратора"}, status_code=403)
    patch = body.get("settings") if isinstance(body.get("settings"), dict) else body
    patch = {k: v for k, v in (patch or {}).items() if k != "_source"}
    with db.tx() as con:
        ensure_tables(con)
        # частичная правка сливается с действующей версией (глубоко): прежние правки администратора не сбрасываются
        new = ae.deep_merge(current_custom(con), patch)
        errs = ae.check_settings(new)
        if errs:
            return JSONResponse({"ok": False, "errors": errs}, status_code=422)
        cur = con.execute("INSERT INTO act_settings (created_at, created_by, settings_json, calibrated, note) "
                          "VALUES (?,?,?,?,?)", (db.now(), user.get("login"), json.dumps(new, ensure_ascii=False),
                                                 0, str(body.get("note") or "")[:300]))
        db.audit(con, user.get("login") or "админ", "акт: настройки изменены", f"act_settings:{cur.lastrowid}",
                 {"keys": sorted(patch)})
        return {"ok": True, "id": cur.lastrowid, "settings": load_settings(con)}


# --------------------------------------------------------------------------- #
#  Шаблоны анализа по классам (справочник class_templates, приложение А)
#  Пути объявлены раньше /act/{aid}: иначе «templates» принялось бы за номер акта.
# --------------------------------------------------------------------------- #

@router.get("/act/templates")
def act_templates_list(request: Request, lang: str = ""):
    """Все шаблоны кратко (файл 1.3.0: классы общего страхования 1–18, варианты 13з и 16у): версия, признак варианта
    (variant), название, риски с долями, правило сценария, поля; pending_file_version — новая версия файла ждёт."""
    lg = tx.lang_of(_lang(request, lang))
    with db.tx() as con:
        rows = ctpl.all_current(con)
        try:
            meta = ctpl.load_file()
        except (OSError, ValueError):
            meta = {}
        items = [ctpl.view(r, lg, con, full=False) for r in rows]
        return {"ok": True, "lang": lg, "file_version": meta.get("version"), "file_date": meta.get("date"),
                "aliases": meta.get("aliases") or {}, "calibrated": ctpl.CALIBRATED,
                "classification": ctpl.localize(meta.get("classification") or {}, lg),
                "counts": {"всего": len(items), "классов": sum(1 for x in items if not x["variant"]),
                           "вариантов": sum(1 for x in items if x["variant"])},
                "common_must": ctpl.localize(meta.get("common_must") or [], lg),
                "templates": items}


@router.get("/act/templates/{class_code}/history")
def act_templates_history(request: Request, class_code: str):
    """Все версии шаблона класса (старые не удаляются): кто, когда, источник (file | admin)."""
    admin = (_user(request) or {}).get("role") == ADMIN
    with db.tx() as con:
        if not ctpl.current(con, class_code):
            return JSONResponse({"ok": False, "detail": "нет шаблона для класса " + class_code}, status_code=404)
        hist = ctpl.history(con, class_code)
        if not admin:                        # кто правил — видит только администратор
            for h in hist:
                h["updated_by"] = "администратор" if h["source"] == "admin" else h["updated_by"]
        return {"ok": True, "class_code": ctpl.base_class(class_code), "history": hist}


@router.get("/act/templates/{class_code}")
def act_templates_get(request: Request, class_code: str, lang: str = "", raw: int = 0):
    """Шаблон класса на языке lang (подписи ru/uz/en) с документами из checklists и рисками perils (классы 8, 9).
    raw=1 — исходный JSON со всеми тремя языками (для правки администратором)."""
    lg = tx.lang_of(_lang(request, lang))
    with db.tx() as con:
        row = ctpl.current(con, class_code)
        if not row:
            return JSONResponse({"ok": False, "detail": "нет шаблона для класса " + class_code}, status_code=404)
        out = ctpl.view(row, lg, con)
        if raw:
            out["raw"] = row["template"]
        return {"ok": True, **out}


@router.put("/act/templates/{class_code}")
def act_templates_put(request: Request, class_code: str, body: dict = Body(...)):
    """Новая версия шаблона класса (история сохраняется). Только администратор. Проверка структуры: доли рисков
    100 ± 0,5, коды оговорок и мероприятий существуют, обязательных полей не больше четырёх — иначе 422."""
    user = _user(request)
    if (user or {}).get("role") != ADMIN:
        return JSONResponse({"detail": "нужны права администратора"}, status_code=403)
    tpl = body.get("template") if isinstance(body, dict) and isinstance(body.get("template"), dict) else body
    with db.tx() as con:
        if not ctpl.current(con, class_code):
            return JSONResponse({"ok": False, "detail": "нет шаблона для класса " + class_code}, status_code=404)
        errs = ctpl.validate(tpl, class_code, con)
        if errs:
            return JSONResponse({"ok": False, "errors": errs}, status_code=422)
        row = ctpl.save(con, class_code, tpl, user.get("login") or ADMIN, (body or {}).get("note") or "")
        return {"ok": True, **ctpl.view(row, tx.lang_of(_lang(request, (body or {}).get("lang"))), con)}


@router.get("/act/{aid}.docx")
def act_docx(request: Request, aid: str, lang: str = ""):
    """Акт в Word (первая секция — страховой скоринг). Права — как у GET /act/{id}."""
    got = _load_act(request, aid)
    if not got:
        return _fail(request, t("not_found", _lang(request, lang)), 404)
    D, meta, row = got
    out = render(D, _lang(request, lang or row["lang"]), meta)
    data = build_docx(out)
    return Response(content=data, media_type="application/vnd.openxmlformats-officedocument."
                                              "wordprocessingml.document",
                    headers={"Content-Disposition": f'attachment; filename="act_{meta["number"]}.docx"'})


@router.get("/act/{aid}.pdf")
def act_pdf(request: Request, aid: str, lang: str = ""):
    """Акт в PDF (первая страница — страховой скоринг). Права — как у GET /act/{id}."""
    got = _load_act(request, aid)
    if not got:
        return _fail(request, t("not_found", _lang(request, lang)), 404)
    D, meta, row = got
    out = render(D, _lang(request, lang or row["lang"]), meta)
    return Response(content=build_pdf(out), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="act_{meta["number"]}.pdf"'})


@router.get("/act/{aid}/scoring.pdf")
def act_scoring_pdf(request: Request, aid: str, lang: str = ""):
    """Только страница «Страховой скоринг объекта» (права как у акта: владелец или администратор)."""
    got = _load_act(request, aid)
    if not got:
        return _fail(request, t("not_found", _lang(request, lang)), 404)
    D, meta, row = got
    out = render(D, _lang(request, lang or row["lang"]), meta)
    if not (out.get("scoring") or {}).get("available"):
        return _fail(request, t("not_found", out["lang"]), 404)
    return Response(content=build_pdf(out, scoring_only=True), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="scoring_{meta["number"]}.pdf"'})


@router.get("/act/{aid}/scoring.png")
def act_scoring_png(request: Request, aid: str, lang: str = ""):
    """Картинка шкалы скоринга с плашкой класса (PNG; права как у акта)."""
    got = _load_act(request, aid)
    if not got:
        return _fail(request, t("not_found", _lang(request, lang)), 404)
    D, meta, row = got
    out = render(D, _lang(request, lang or row["lang"]), meta)
    if not (out.get("scoring") or {}).get("available"):
        return _fail(request, t("not_found", out["lang"]), 404)
    return Response(content=scoring_png(out), media_type="image/png",
                    headers={"Content-Disposition": f'inline; filename="scoring_{meta["number"]}.png"'})


# отправка ботом — app/act_pkg/send.py; регистрация здесь, чтобы порядок адресов не менялся
router.add_api_route("/act/{aid}/send", act_send, methods=["POST"])


@router.get("/admin/acts")
def admin_acts(request: Request, limit: int = 30):
    """Последние акты для администратора: id, дата, продукт, объект, язык, без содержимого (guard: /admin — только админ)."""
    limit = max(1, min(int(limit or 30), 200))
    with db.tx() as con:
        ensure_tables(con)
        rows = db.rows(con, "SELECT id, lang, created_at, expires_at, act_json FROM acts ORDER BY created_at DESC LIMIT ?", limit)
    items = []
    for r in rows:
        try:
            D = json.loads(r["act_json"]) or {}
        except Exception:
            D = {}
        must = D.get("must") or {}
        obj = D.get("object") or {}
        items.append({"id": r["id"], "lang": r["lang"], "created_at": r["created_at"], "expires_at": r["expires_at"],
                      "product_code": must.get("product_code") or D.get("product_code"),
                      "class_code": D.get("class_code") or (D.get("template") or {}).get("class_code"),
                      "object": str(obj.get("label") or obj.get("kind_label") or D.get("object_kind") or "")[:80],
                      "sum_insured": must.get("sum_insured"), "premium": (D.get("premium") or {}).get("amount"),
                      "objects": len(D.get("objects") or []), "files": len(D.get("files") or [])})
    return _reply(request, {"ok": True, "count": len(items), "items": items})


@router.get("/act/{aid}")
def act_get(request: Request, aid: str, lang: str = ""):
    """Свой акт (JSON) на языке ?lang= из сохранённого снимка, без пересчёта; чужой — 404."""
    got = _load_act(request, aid)
    if not got:
        return _fail(request, t("not_found", _lang(request, lang)), 404)
    D, meta, row = got
    return _reply(request, render(D, _lang(request, lang or row["lang"]), meta))
