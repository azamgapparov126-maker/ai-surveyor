"""POST /act/{id}/send — акт файлом в чат с ботом; чей Telegram ID (подпись initData или привязка)."""
from typing import Optional

from fastapi import Body, Request

from .. import db, guest, telegram, tgbot
from ..act_texts import t

from .common import ensure_tables, _fail, _lang, _load_act, load_settings, _reply, SEND_FORMATS, SENDS, _user, _who
from .view import render
from .export import build_docx, build_pdf, pdf_extras


def _telegram_id(request: Request, user: Optional[dict], init_data: str) -> tuple:
    """
    Подтверждённый telegram id: подписанный initData мини-приложения (проверка app/telegram.py)
    или привязка вошедшего пользователя (users.telegram_id). (id или None, код отказа или None).
    """
    bad = None
    if init_data:
        res = telegram.check_init_data(init_data, telegram.bot_token())
        tid = str(((res.get("user") or {}).get("id")) or "") if res.get("ok") else ""
        if tid.isdigit():
            return tid, None
        # подпись не сошлась или устарела (мини-приложение открыто больше суток): запасной путь —
        # привязка вошедшего пользователя; «bad_init» только если и её нет
        bad = "bad_init"
    if user and user.get("id"):
        with db.tx() as con:
            r = db.rows(con, "SELECT telegram_id FROM users WHERE id=?", user["id"])
        tid = str((r[0]["telegram_id"] if r else "") or "").strip()
        if tid.isdigit():
            return tid, None
    return None, bad or "no_telegram"


def act_send(request: Request, aid: str, body: dict = Body(default={})):
    """
    Акт файлом в чат с ботом (в Telegram на телефоне скачивание из WebView не работает).
    Тело: {"format": "docx"|"pdf", "lang": "ru"|"uz"|"en" (необязательно), "initData": "..." (из мини-приложения)}.
    Только владелец акта; не больше limits.send_per_hour отправок в час на пользователя Telegram.
    """
    body = body if isinstance(body, dict) else {}
    lang = _lang(request, body.get("lang"))
    user = _user(request)
    owner = guest.owner_of(request, user)
    got = _load_act(request, aid)
    # только владелец: администратору чужой акт ботом не отправляется
    if not got or got[2]["owner_key"] != owner:
        return _fail(request, t("not_found", lang), 404)
    fmt = str(body.get("format") or "").strip().lower()
    if fmt not in SEND_FORMATS:
        return _fail(request, t("send_format", lang), 422, errors={"format": "docx или pdf"})
    if not tgbot.connected():
        return _fail(request, t("send_bot_off", lang), 503, code="bot_off")
    tid, why = _telegram_id(request, user, str(body.get("initData") or "").strip()[:4096])
    if why == "bad_init":
        return _fail(request, t("send_bad_init", lang), 403, code="bad_init_data")
    if not tid:
        return _fail(request, t("send_no_tg", lang), 409, code="no_telegram")
    with db.tx() as con:
        ensure_tables(con)
        n_max = int(load_settings(con)["limits"]["send_per_hour"])
    res = SENDS.take("tg:" + tid, 1, n_max)
    if not res["ok"]:
        out = _fail(request, t("send_limit", lang, n=n_max), 429, limit=n_max, window_hours=1,
                    retry_after_sec=res["retry_after"], code="limit")
        out.headers["Retry-After"] = str(res["retry_after"])
        return out
    D, meta, row = got
    act_lang = _lang(request, body.get("lang") or row["lang"])
    act = render(D, act_lang, meta)
    ex = pdf_extras(D, row, photos=fmt == "pdf")
    blob = build_docx(act, extras=ex) if fmt == "docx" else build_pdf(act, extras=ex)
    filename = f"act_{meta['number']}.{fmt}"
    sent = tgbot.send_file(tid, filename, blob, SEND_FORMATS[fmt], caption=t("send_caption", act_lang,
                                                                             number=meta["number"]),
                           kind="акт")
    with db.tx() as con:
        # в журнал — без telegram id и без содержимого: формат, язык и итог
        db.audit(con, _who(user, owner), "акт: отправка ботом", f"act:{aid}",
                 {"format": fmt, "lang": act_lang, "ok": bool(sent.get("ok"))})
    if not sent.get("ok"):
        reason = str(sent.get("reason") or "")
        low = reason.lower()
        if "blocked" in low or "initiate" in low or "chat not found" in low:
            return _fail(request, t("send_start_bot", lang), 502, code="start_bot")
        return _fail(request, t("send_failed", lang), 502, code="telegram_error")
    return _reply(request, {"ok": True, "sent": True, "format": fmt, "lang": act_lang, "filename": filename,
                            "message": t("send_ok", lang), "left_this_hour": res.get("left", 0)})
