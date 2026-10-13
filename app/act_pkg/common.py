"""
Общее для акта: константы и пределы, таблицы, окна лимитов, настройки и справочники, кто пришёл,
ответы с cookie гостя, очистка загрузок, чтение своего акта.
"""
import json
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Request
from fastapi.responses import JSONResponse

from .. import act_engine as ae
from .. import auth, db, guest, i18n, llm
from .. import min_rates as mrs


DIR = db.DATA_DIR / "act"                 # файлы загрузок; тесты подменяют
CLAUSES_FILE = db.ROOT / "docs" / "act_clauses.json"
PHOTO_TTL_SEC = 24 * 3600
ACT_TTL_SEC = 7 * 24 * 3600
ORPHAN_SEC = 3600                         # папка без записи в базе старше часа — сирота, удаляется
MAX_FILES = 10
MAX_BYTES = 15 * 1024 * 1024
# тело multipart: файлы + заголовки частей и поля формы
MAX_BODY = MAX_FILES * MAX_BYTES + 1024 * 1024
AI_MAX_SIDE = 2048                        # длинная сторона снимка для модели: табличка ещё читается
# если вложения не укладываются в предел — пережимаем сильнее (сторона, качество JPEG)
AI_SHRINK_STEPS = ((AI_MAX_SIDE, 85), (1600, 75), (1280, 65))
MAX_SUM = 1e15                            # защита от опечатки в разрядах, а не норма
MAX_RECOGNIZED = 100
MAX_REGION_TEXT = 120                     # территория страхования текстом (регион «Другое»)
REGION_ALL, REGION_OTHER = "uz_all", "other"   # вся республика; территория вне списка (вне Узбекистана)
ADMIN = "админ"
SEND_FORMATS = {"docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "pdf": "application/pdf"}
# подсказка приоритета до распознавания: такие имена скорее документ или табличка (имя нигде не хранится)
DOC_NAME_HINTS = ("doc", "scan", "sheet", "plate", "passport", "pasport", "скан", "док", "лист", "таблич",
                  "паспорт", "техпас", "hujjat")

FMT_MIME = {"jpg": "image/jpeg", "png": "image/png", "pdf": "application/pdf",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
DOC_FMTS = ("docx", "xlsx")               # документы с текстом: разбираются парсерами, в модель не уходят
# значения документа, которые похожи на ПД по шаблону, но являются данными объекта
PD_KEEP = {"reg_no": "[ГОСНОМЕР]", "cadastre_no": "[КАДАСТР]"}
MODEL_SOURCES = ("photo", "plate", "document", "marking")
CONDITIONS = ("new", "good", "worn", "damaged")
CLASS_HINTS = ("special_machinery", "vehicle", "building", "equipment", "cargo", "other")
HINT_CLASSES = {"special_machinery": ["3"], "vehicle": ["3"], "building": ["8", "9"], "equipment": ["8", "9"],
                "cargo": ["7"], "other": [],
                # личное страхование по договору (app/contract_read.personal_hint): несчастные случаи, болезни
                "accident": ["1"], "health": ["2"]}
EMPTY_VALUES = {"", "null", "none", "n/a", "na", "-", "—", "unknown", "нет", "не видно", "неизвестно",
                "не указано", "yoʻq", "not visible"}
COMPANY_MARKERS = ("co.", "co,", "ltd", "llc", "inc", "gmbh", "group", "machinery", "corporation", "corp",
                   "industr", "heavy", "motor", "company", "завод", "компания", "ооо", "ао ", "холдинг")

SCHEMA_SQL = [
    """CREATE TABLE IF NOT EXISTS act_settings (
        id            INTEGER PRIMARY KEY,
        created_at    TEXT NOT NULL,
        created_by    TEXT,
        settings_json TEXT NOT NULL,
        calibrated    INTEGER NOT NULL DEFAULT 0,
        note          TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS act_uploads (
        id           TEXT PRIMARY KEY,
        owner_key    TEXT NOT NULL,
        user_id      INTEGER NOT NULL DEFAULT 0,
        files_json   TEXT NOT NULL DEFAULT '[]',
        result_json  TEXT NOT NULL DEFAULT '{}',
        created_at   TEXT NOT NULL,
        expires_at   TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS acts (
        id                TEXT PRIMARY KEY,
        owner_key         TEXT NOT NULL,
        user_id           INTEGER NOT NULL DEFAULT 0,
        lang              TEXT NOT NULL DEFAULT 'ru',
        tariff_version_id INTEGER,
        settings_id       INTEGER,
        act_json          TEXT NOT NULL,
        created_at        TEXT NOT NULL,
        expires_at        TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_act_uploads_expires ON act_uploads (expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_acts_expires ON acts (expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_acts_owner ON acts (owner_key, created_at)",
]


def ensure_tables(con) -> None:
    """Таблицы акта (act_uploads, acts, act_settings), если их ещё нет."""
    for sql in SCHEMA_SQL:
        con.execute(sql)


def _utcnow() -> datetime:
    """Часы сервера в UTC (тесты подменяют эту функцию, чтобы проверить переход даты)."""
    return datetime.now(timezone.utc)


@lru_cache(maxsize=8)
def _zone(name: str, offset_hours: int):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # на Windows без пакета tzdata базы поясов нет — постоянный сдвиг из настроек (у Ташкента он не меняется)
        return timezone(timedelta(hours=offset_hours), name)


def act_zone(settings: Optional[dict] = None):
    """Пояс акта из настроек clock (по умолчанию Asia/Tashkent)."""
    ck = (settings or {}).get("clock") or ae.DEFAULT_SETTINGS["clock"]
    return _zone(str(ck["tz"]), int(ck["utc_offset_hours"]))


def _now() -> datetime:
    """
    Время акта по Ташкенту без пояса в записи: created_at, expires_at и номер акта пишутся в том же виде, что раньше
    (ГГГГ-ММ-ДДTчч:мм:сс), поэтому сравнение строк в очистке и чтении акта не меняется — все стороны берут _now().
    """
    return _utcnow().astimezone(act_zone()).replace(tzinfo=None)


def _today() -> date:
    """Дата по Ташкенту: возраст объекта, дата снимков объявлений, «сегодня» в правилах акта."""
    return _now().date()


class _Window:
    """
    Скользящее окно в час (в памяти процесса, как app/guest.py): перезапуск обнуляет — это защита
    от злоупотреблений, а не учёт. take(key, n) засчитывает сразу n единиц (например, n фото).
    """

    def __init__(self, window_sec: int = 3600):
        self.window = window_sec
        self.lock = threading.Lock()
        self.hits = {}

    def take(self, key: str, n: int, limit: int, now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        with self.lock:
            marks = [x for x in self.hits.get(key) or [] if now - x < self.window]
            if len(marks) + n > limit:
                self.hits[key] = marks
                retry = int(self.window - (now - marks[0])) + 1 if marks else self.window
                return {"ok": False, "count": len(marks), "limit": limit, "retry_after": max(retry, 1)}
            marks += [now] * n
            self.hits[key] = marks
            return {"ok": True, "count": len(marks), "limit": limit, "left": limit - len(marks),
                    "retry_after": 0}

    def reset(self) -> None:
        with self.lock:
            self.hits.clear()


GUEST_PHOTOS = _Window()                  # фото гостя в час (по числу файлов, а не запросов)
AI_CALLS = _Window()                      # обращения к модели за распознаванием — на весь сервер
SENDS = _Window()                         # отправки акта ботом на одного пользователя Telegram
# распознавание идёт в отдельном потоке: так общий срок ответа соблюдается, даже если сеть «тянет»
_AI_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="act-ai")
# раскрытых картинок в памяти одновременно не больше двух: до 50 Мп × 4 байта каждая
_DECODE = threading.BoundedSemaphore(2)


def reset_limits() -> None:
    """Сброс счётчиков — для тестов и ручного снятия блокировки."""
    for w in (GUEST_PHOTOS, AI_CALLS, SENDS):
        w.reset()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
#  Настройки и справочники
# --------------------------------------------------------------------------- #

def load_settings(con) -> dict:
    """Действующие настройки: последняя версия act_settings поверх ae.DEFAULT_SETTINGS."""
    try:
        r = con.execute("SELECT id, created_at, created_by, settings_json FROM act_settings "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    except Exception:
        r = None
    if not r:
        s = ae.merge_settings({})
        s["_source"] = {"id": None, "what": "по умолчанию (экспертно)"}
        return s
    s = ae.merge_settings(json.loads(r[3]))
    s["_source"] = {"id": r[0], "created_at": r[1], "created_by": r[2], "what": "правка администратора"}
    return s


def current_custom(con) -> dict:
    """Правки администратора действующей версии act_settings (без значений по умолчанию); нет версии — {}."""
    try:
        r = con.execute("SELECT settings_json FROM act_settings ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(r[0]) if r else {}
    except Exception:
        return {}


_clauses_cache = {"mtime": None, "data": {}}


def clause_catalog() -> dict:
    """docs/act_clauses.json; файл перечитывается, только если изменился."""
    try:
        m = CLAUSES_FILE.stat().st_mtime
        if _clauses_cache["mtime"] != m:
            _clauses_cache["data"] = json.loads(CLAUSES_FILE.read_text(encoding="utf-8"))
            _clauses_cache["mtime"] = m
    except (OSError, ValueError) as e:
        print("акт: список оговорок не прочитан:", type(e).__name__)
        return {}
    return _clauses_cache["data"]


def insurer_name(settings: dict) -> Optional[str]:
    """Название страховщика — из настроек (INSURER_NAME или act_settings.insurer_name), в коде его нет."""
    name = (llm.get("INSURER_NAME", "") or "").strip() or str(settings.get("insurer_name") or "").strip()
    return name[:120] or None


def tariff_version(con, level: str) -> Optional[int]:
    """Действующая версия тарифов уровня (без версий правок по одному продукту — минимальной ставки, её id
    в D["min_rate"], и базовой ставки из импорта продуктов)."""
    return mrs.current_version(con, level)


# --------------------------------------------------------------------------- #
#  Кто пришёл, ответы
# --------------------------------------------------------------------------- #

def _user(request: Request):
    u = request.scope.get("surveyor_user")
    if u is None:
        token = auth.request_token(request)
        if token:
            with db.tx() as con:
                u = auth.session_user(con, token)
    return u


def _who(user, owner: str) -> str:
    """Для журнала: пользователь по id, гость — коротким хэшем (ни gid, ни IP в журнал не пишем)."""
    if user and user.get("id"):
        return "u:%s" % user["id"]
    return "гость:" + guest.short(owner or "")


def _cookie_7d(gid: str) -> bytes:
    """cookie гостя на 7 дней: акт живёт неделю и должен открываться тем же гостем."""
    parts = [f"{guest.COOKIE}={gid}", "Path=/", f"Max-Age={ACT_TTL_SEC}", "HttpOnly", "SameSite=Lax"]
    if guest.cookie_secure():
        parts.append("Secure")
    return "; ".join(parts).encode("latin-1")


def _reply(request: Request, payload: dict, status: int = 200) -> JSONResponse:
    res = JSONResponse(status_code=status, content=payload)
    gid = guest.from_request(request)
    if gid:
        res.raw_headers.append((b"set-cookie", _cookie_7d(gid)))
    return res


def _fail(request: Request, message: str, status: int, **extra) -> JSONResponse:
    return _reply(request, {"ok": False, "detail": message, **extra}, status)


def _lang(request: Request, given=None) -> str:
    if given and i18n.normalize_lang(given):
        return i18n.normalize_lang(given)
    return i18n.pick_lang(None, request.cookies.get(i18n.COOKIE))


# --------------------------------------------------------------------------- #
#  Очистка
# --------------------------------------------------------------------------- #

def cleanup(con=None) -> int:
    """Удаляет просроченные загрузки (24 часа, вместе с файлами) и акты (7 дней)."""
    def _run(c):
        ensure_tables(c)
        now = _iso(_now())
        old = db.rows(c, "SELECT id FROM act_uploads WHERE expires_at <= ?", now)
        for r in old:
            shutil.rmtree(DIR / r["id"], ignore_errors=True)
            c.execute("DELETE FROM act_uploads WHERE id=?", (r["id"],))
        n = c.execute("DELETE FROM acts WHERE expires_at <= ?", (now,)).rowcount or 0
        return len(old) + n + _drop_orphans(c)
    if con is not None:
        return _run(con)
    with db.tx() as c:
        return _run(c)


def _drop_orphans(con) -> int:
    """Страховка: папки загрузок без записи в базе (запись упала) старше ORPHAN_SEC удаляются по времени изменения."""
    if not DIR.is_dir():
        return 0
    known = {r["id"] for r in db.rows(con, "SELECT id FROM act_uploads")}
    edge = time.time() - ORPHAN_SEC
    n = 0
    for p in DIR.iterdir():
        try:
            if p.is_dir() and p.name not in known and p.stat().st_mtime < edge:
                shutil.rmtree(p, ignore_errors=True)
                n += 1
        except OSError:
            continue
    return n
PD_KEEP["object_type"] = "[КАДАСТР]"      # описание объекта из запроса филиала содержит кадастровый номер


def _empty_v(v) -> bool:
    return v in (None, "", [], {})


def _load_act(request: Request, aid: str):
    """(данные, meta) своего живого акта или None. Чужой акт не отличается от несуществующего."""
    user = _user(request)
    owner = guest.owner_of(request, user)
    if not re.fullmatch(r"[0-9a-f]{16}", aid or ""):
        return None
    with db.tx() as con:
        ensure_tables(con)
        rows = db.rows(con, "SELECT * FROM acts WHERE id=? AND expires_at > ?", aid, _iso(_now()))
    if not rows:
        return None
    row = rows[0]
    if row["owner_key"] != owner and (user or {}).get("role") != ADMIN:
        return None
    stored = json.loads(row["act_json"])
    return stored["data"], stored["meta"], row
