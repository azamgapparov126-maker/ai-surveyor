"""Оценка по объявлениям: ссылки поиска, курс ЦБ (кэш и свой пул), снимки экрана сотрудника."""
import json
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from fastapi import Request

from .. import act_engine as ae
from .. import act_market as am
from .. import act_texts as tx
from .. import db, guest, llm, valuation
from ..act_texts import t

from .common import cleanup, ensure_tables, _fail, FMT_MIME, _iso, MAX_BYTES, _now, PHOTO_TTL_SEC, _reply, _today, _user, _who
from .files import check_content, _format_of
from .recognize import ask_model, preferred
from .terms import _int_in


# --------------------------------------------------------------------------- #
#  Оценка по объявлениям: ссылки поиска и снимки экрана сотрудника (30.09.2026)
#  Сервер к площадкам (в том числе olx.uz) не обращается: только адреса и чтение снимков моделью.
# --------------------------------------------------------------------------- #

MAX_SHOT_BODY = am.MAX_SHOTS * MAX_BYTES + 1024 * 1024
_FX_CACHE = {}                            # курс ЦБ на дату: (ответ, срок в time.monotonic() или None — навсегда)
FX_DEADLINE_SEC = 5                       # общий срок ожидания курса при загрузке снимков
FX_FAIL_TTL_SEC = 600                     # неудачу (нет сети, cbu.uz молчит) повторяем не раньше чем через 10 минут
# свой пул для курса: медленный cbu.uz не должен занимать потоки, которые ждут ответа модели
_FX_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="act-fx")
_FX_INFLIGHT = {}
_FX_LOCK = threading.Lock()


def _photo_query(con, sid: Optional[str], owner: str) -> dict:
    """Марка, модель, год и вид объекта из своей живой загрузки фото объекта (если она указана)."""
    if not sid:
        return {}
    rows = db.rows(con, "SELECT result_json FROM act_uploads WHERE id=? AND owner_key=? AND expires_at > ?",
                   str(sid)[:40], owner, _iso(_now()))
    up = json.loads(rows[0]["result_json"] or "{}") if rows else {}
    if not up or up.get("kind") == "market":
        return {}
    fields = up.get("fields") or []
    yr = preferred(fields, "year")
    return {"brand": (preferred(fields, "brand") or {}).get("value"),
            "model": (preferred(fields, "model") or {}).get("value"),
            "year": ae.to_year(yr["value"]) if yr else None, "object_kind": up.get("object_kind")}


def _market_query(request: Request, session: str, brand: str, model: str, year, object_kind: str,
                  class_code: str) -> tuple:
    """(запрос, ошибки): явные параметры сильнее значений из загрузки фото."""
    user = _user(request)
    owner = guest.owner_of(request, user)
    errs = {}
    q = {}
    if session:
        with db.tx() as con:
            ensure_tables(con)
            q = _photo_query(con, session, owner or "")
    for key, v, lim in (("brand", brand, 60), ("model", model, 60)):
        v = am._s(v, lim)
        if v:
            if llm.has_pd(v) and not re.fullmatch(r"[\w\-./ ]+", v, re.A):
                errs[key] = "похоже на данные человека"
            else:
                q[key] = v
    if year not in (None, ""):
        try:
            q["year"] = _int_in(year, 1950, _today().year + 1)
        except ValueError:
            errs["year"] = f"целое число от 1950 до {_today().year + 1}"
    if object_kind:
        if object_kind not in tx.OBJECT_KINDS:
            errs["object_kind"] = "неизвестный вид объекта"
        else:
            q["object_kind"] = object_kind
    if class_code:
        q["class_code"] = str(class_code).strip()[:10]
    return q, errs


def _fx_cached(key: str) -> Optional[dict]:
    """Курс ЦБ из памяти: удачный — на весь день, неудача — FX_FAIL_TTL_SEC (не долбим cbu.uz на каждой загрузке)."""
    got = _FX_CACHE.get(key)
    if not got:
        return None
    res, until = got
    if until is not None and time.monotonic() > until:
        _FX_CACHE.pop(key, None)
        return None
    return res


def _fx_lookup(today: date) -> dict:
    """Курс из существующего источника проекта: valuation.fx_rate (ручной курс заказчика → ЦБ РУз, cbu.uz)."""
    key = today.isoformat()
    hit = _fx_cached(key)
    if hit is not None:
        return hit
    try:
        with db.tx() as con:
            fx = valuation.fx_rate(con, today)
    except Exception as e:                   # сбой курса не роняет загрузку, но запоминается на 10 минут
        fx = {"rate": None, "as_of": key, "reason": type(e).__name__}
    by = "manual_setting" if fx.get("manual") else ("cbu" if fx.get("rate") else None)
    res = {"rate": fx.get("rate"), "by": by, "as_of": fx.get("as_of"), "reason": fx.get("reason")}
    if by == "cbu":
        _FX_CACHE[key] = (res, None)
    elif by is None:
        _FX_CACHE[key] = (res, time.monotonic() + FX_FAIL_TTL_SEC)
    return res


def _fx_submit(today: date):
    """Запрос курса в своём пуле (не в пуле модели); одновременные загрузки ждут один и тот же запрос."""
    key = today.isoformat()
    with _FX_LOCK:
        fut = _FX_INFLIGHT.get(key)
        if fut is None or fut.done():
            fut = _FX_POOL.submit(_fx_lookup, today)
            _FX_INFLIGHT[key] = fut
        return fut


def fx_verify(claim: Optional[dict]) -> Optional[dict]:
    """
    Курс, который экран получил от сервера (fx.by = cbu), сверяется с памятью сервера без сети: совпал — это
    курс ЦБ; не с чем сверить — курс ЦБ по данным экрана, сервером не перепроверен (не «введён сотрудником»).
    """
    if not claim or claim.get("by") != "cbu":
        return None
    key = claim.get("as_of") or _today().isoformat()
    hit = _fx_cached(key)
    if hit and hit.get("by") == "cbu" and hit.get("rate") and abs(float(hit["rate"]) - float(claim["rate"])) < 0.005:
        return {"rate": float(hit["rate"]), "by": "cbu", "as_of": hit.get("as_of") or key}
    return {"rate": float(claim["rate"]), "by": "cbu_unverified", "as_of": key}


def _shots(request, user, owner, lang, files, site, q, emp_rate, st, sid, folder):
    limits = st["limits"]
    saved, rejected = [], []
    for i, up in enumerate(files, start=1):
        name = llm.mask_pd(Path(up.filename or "").name)[:120] or f"file {i}"   # только для ответа
        blob = up.file.read(MAX_BYTES + 1)
        if not blob:
            rejected.append({"index": i, "name": name, "error": t("ph_empty_file", lang)})
            continue
        if len(blob) > MAX_BYTES:
            rejected.append({"index": i, "name": name, "error": t("ph_too_big", lang, mb=MAX_BYTES // (1024 * 1024))})
            continue
        fmt = _format_of(blob)
        err = check_content(blob, fmt, limits, lang) if fmt in ("jpg", "png") else t("mk_format", lang)
        if err:
            rejected.append({"index": i, "name": name, "error": err})
            continue
        folder.mkdir(parents=True, exist_ok=True)
        fid = f"s{len(saved) + 1}"
        path = folder / f"{fid}.{fmt}"
        path.write_bytes(blob)
        saved.append({"id": fid, "index": i, "name": name, "orig_name": "", "fmt": fmt, "mime": FMT_MIME[fmt],
                      "size": len(blob), "path": db.stored_path(path), "blob": blob})
    if not saved:
        shutil.rmtree(folder, ignore_errors=True)
        return _fail(request, t("ph_none", lang), 422, rejected=rejected, ai=False, warning=t("mk_warn", lang))

    today = _today()
    # курс ищем параллельно с чтением снимков в своём пуле: к cbu.uz ходит существующий модуль оценки,
    # не к площадкам; общий срок ожидания — FX_DEADLINE_SEC от начала загрузки
    fx_fut = _fx_submit(today)
    t0 = time.monotonic()
    rec = ask_model(saved, lang, limits, "акт: снимки объявлений", am.SYSTEM_PROMPT,
                    lambda n: am.model_prompt(n, lang, q, today),
                    lambda text, n: am.parse_model(text, n, today, site))
    try:
        fx = fx_fut.result(timeout=max(0.0, FX_DEADLINE_SEC - (time.monotonic() - t0)))
    except FutureTimeout:
        fx = {"rate": None, "by": None, "as_of": today.isoformat(),
              "reason": f"курс не получен за {FX_DEADLINE_SEC} с"}
    except Exception as e:                   # курс не должен ронять загрузку, но причина видна
        fx = {"rate": None, "by": None, "as_of": today.isoformat(), "reason": type(e).__name__}
    if not fx.get("rate") and emp_rate:
        fx = {"rate": emp_rate, "by": "employee", "as_of": today.isoformat()}
    fx_used = fx if fx.get("rate") else None
    sent = rec.get("sent") or []
    model_to_id = {k + 1: saved[i]["id"] for k, i in enumerate(sent)}
    listings = []
    for r in rec.get("listings") or []:
        listings.append({**r, "file": model_to_id.get(r.get("file")), "source": "shot", "edited": False})
    est = ae.market_estimate(listings, settings=st, shot_date=today, usd_rate=(fx_used or {}).get("rate"))
    usd_needed = not fx_used and any(r.get("currency") in ae.USD_LIKE for r in listings)
    stored_listings = [{k: r.get(k) for k in am.LISTING_KEYS if k in r} for r in listings]
    with db.tx() as con:
        ensure_tables(con)
        cleanup(con)
        now = _now()
        stored = {"kind": "market", "lang": lang, "ai": bool(rec.get("ok")), "reason": rec.get("reason"),
                  "shot_date": today.isoformat(), "site": site, "query": q, "listings": stored_listings,
                  "fx": fx_used, "files": len(saved)}
        keep = ("id", "index", "fmt", "mime", "size", "path")
        con.execute("INSERT INTO act_uploads (id, owner_key, user_id, files_json, result_json, created_at, "
                    "expires_at) VALUES (?,?,?,?,?,?,?)",
                    (sid, owner, (user or {}).get("id") or 0,
                     json.dumps([{k: f[k] for k in keep} for f in saved], ensure_ascii=False),
                     json.dumps(stored, ensure_ascii=False), _iso(now),
                     _iso(now + timedelta(seconds=PHOTO_TTL_SEC))))
        # в журнал — только счётчики: ни названий, ни цен, ни данных продавцов
        db.audit(con, _who(user, owner), "акт: снимки объявлений", f"act_upload:{sid}",
                 {"files": len(saved), "rejected": len(rejected), "ai": bool(rec.get("ok")),
                  "listings": len(listings), "relevant": sum(1 for r in listings if r.get("relevant")),
                  "dropped_pd": rec.get("dropped") or 0, "duplicates": rec.get("duplicates") or 0,
                  "site": site, "fx": (fx_used or {}).get("by")})
    notes = []
    if rec.get("ok"):
        message = t("mk_ok", lang, n=len(listings), k=sum(1 for r in listings if r.get("relevant"))) \
            if listings else t("mk_empty", lang)
    else:
        message = t("mk_ai_off", lang, reason=rec.get("reason") or t("ai_not_connected", lang))
    if rec.get("dropped"):
        notes.append(t("mk_pd_dropped", lang, n=rec["dropped"]))
    if usd_needed:
        notes.append(t("mk_need_rate", lang))
    fxv = am.fx_label(fx_used, lang)
    if fxv:
        notes.append(fxv["text"])
    if rec.get("ok") and rec.get("not_sent"):
        notes.append(t("ph_not_sent", lang, files=", ".join(str(saved[i]["index"]) for i in rec["not_sent"]),
                       mb=limits["ai_max_mb"]))
    by_id = {r.get("id"): r for r in est["listings"]}
    q_links = dict(q, group=am.group_of(q.get("object_kind")))
    return _reply(request, {
        "ok": True, "shots_session": sid, "lang": lang, "shot_date": today.isoformat(), "site": site,
        "site_label": tx.label(tx.SITE_LABELS, site, lang),
        "files": [{"id": f["id"], "index": f["index"], "name": f["name"], "format": f["fmt"],
                   "read_by_ai": bool(rec.get("ok")) and k in sent} for k, f in enumerate(saved)],
        "rejected": rejected,
        "listings": [am.listing_view(by_id.get(r["id"], r), lang) for r in listings],
        "estimate": am.estimate_view(est, lang),
        "fx": fxv, "usd_rate_needed": usd_needed,
        "query": {k: q.get(k) for k in ("brand", "model", "year", "object_kind")},
        "links": am.search_links(q_links, lang) if (q.get("brand") or q.get("model") or q.get("object_kind")) else [],
        "ai": bool(rec.get("ok")), "message": message, "notes": notes,
        "warning": t("mk_warn", lang), "expires_in_hours": PHOTO_TTL_SEC // 3600,
    })
