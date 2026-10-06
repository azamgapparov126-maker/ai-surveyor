"""
Единый вход в систему.

С 22.09.2026 (решение заказчика) мини-приложение открыто для всех: чтение справочников,
аналитика риска, расчёт, ОСГОР и юридические ответы работают без входа — их адреса перечислены
в GUEST_* ниже. Вход нужен только администратору: всё изменяющее (справочники, БРВ, пороги,
/admin*, /deploy/*, /tasks*, /audit, /users, финансы, портфель, refresh) по-прежнему закрыто.
Для гостя заводится анонимный guest_id (cookie «gid», 24 часа, app/guest.py): по нему видны
его собственные загруженные файлы и считается лимит обращений (429 при превышении).

Всё остальное без сессии сервер наружу не отдаёт.

Подключение (app/main.py):

    from . import guard
    guard.install(app)            # middleware + маршруты первого администратора

Что происходит с каждым запросом:
  1. Путь в белом списке (WHITE_EXACT / WHITE_PREFIX) — пропускаем. Это то, без чего нельзя войти:
     /health, /theme.js, страница /login, заявка и вход /auth/*, страница мини-аппа /tg и её вход,
     вебхук бота /tg/webhook/{секрет} (секрет проверяет сам обработчик в app/tgbot.py),
     вход через Google /auth/google и его продолжения (status, callback, exchange, register):
     сессии там ещё нет, а подлинность подтверждают state, cookie «gstate» и сам Google.
  2. /docs, /redoc, /openapi.json — только в режиме разработчика (SURVEYOR_DEV=1 с локального адреса).
  3. Режим разработчика: SURVEYOR_DEV=1 И соединение пришло прямо с 127.0.0.1 (::1) И в запросе нет
     заголовков прокси (X-Forwarded-For и родня). Заголовок подделывается кем угодно, поэтому он не
     разрешает доступ, а наоборот — запрещает обход: на Railway запрос всегда идёт через прокси.
     Проверяем адрес из scope["client"] — это реальный собеседник сокета, а не то, что он о себе пишет.
  3а. Гостевой режим: guest_allowed(метод, путь) — адрес из списков GUEST_GET/POST/PUT/DELETE.
     Сессии не требуется, но считается лимит обращений (guest_limit → 429).
  4. Иначе нужна сессия: cookie «sid» или заголовок Authorization: Bearer <токен>.
     Нет сессии → API отвечает 401 {"detail":"нужен вход"}, страница — редирект на /login?next=…
  5. Роли: ADMIN_PREFIX, ADMIN_METHOD_PATH и ADMIN_METHOD_PREFIX — только «админ»; ROLE_PREFIX —
     перечисленным ролям (портфель, калибровка, ёмкость, аналитика), кроме точных путей ANY_ROLE_EXACT
     (аналитика риска для мини-приложения); остальное — любая подтверждённая роль.
     Роль «сотрудник» не попадает ни в один из этих разделов: ей открыты только расчёт, тарифы
     и свои запросы (docs/Регистрация и роли.md, раздел 8 и 6.1).
     Точечные проверки внутри модулей (require(...), «решение принимает назначенный», «вижу только
     своё» — app/access.py) остаются как были: guard — нижняя граница, а не замена.

Первый администратор на пустом сервере: GET /auth/bootstrap-needed и POST /auth/bootstrap
(код + ФИО + логин + пароль). Код — тот же ADMIN_BOOTSTRAP_CODE, что у бота (app/tgbot.py):
одноразовый, после использования гасится отметкой ADMIN_BOOTSTRAP_USED и удаляется из настроек.
Если код не задан, а пользователей ноль — при старте генерируем и печатаем его в журнал сервера
(только в журнал: страницы админки закрыты guard'ом, показать там некому).
"""
import hashlib
import hmac
import os
import re
import secrets
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import auth, db, guest, llm

router = APIRouter()

ADMIN = "админ"
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
# заголовки, которые ставит прокси: если хоть один есть — запрос пришёл не с локального компьютера
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip", "forwarded")

NEED_LOGIN = "нужен вход"

# --- белый список: без сессии ---
WHITE_EXACT = {
    "/health",                       # проверка живости площадки
    "/theme.js", "/favicon.ico", "/i18n.js",     # статика страниц входа
    "/login",
    "/auth/register", "/auth/login", "/auth/verify-code", "/auth/logout",
    "/auth/bootstrap", "/auth/bootstrap-needed",
    "/tg",                           # страница мини-аппа: сама делает вход через /tg/auth
    "/tg/auth", "/tg/status", "/tg/me",
    # регистрация в мини-приложении: у человека ещё нет сессии, зато есть подписанный initData,
    # который каждая точка проверяет сама (app/registration.py)
    "/tg/register/send-code", "/tg/register/verify-code", "/tg/register/submit",
    "/tg/register/departments", "/tg/register/positions", "/tg/consent",
    # вход из обычного браузера через код боту (app/tg_link.py): сессии ещё нет, подлинность
    # подтверждает сам Telegram — код приходит боту от конкретного telegram_id
    "/auth/tg-link/start", "/auth/tg-link/status",
    # вход через Google (app/google_auth.py): перечисляем точно, а не префиксом, чтобы
    # будущий путь вида /auth/google-что-нибудь не открылся наружу молча
    "/auth/google", "/auth/google/status", "/auth/google/callback",
    "/auth/google/exchange", "/auth/google/register",
    # «Запросить доступ в админку» из мини-приложения (app/login_links.py): сессии у гостя ещё нет,
    # подлинность подтверждает подписанный initData — его проверяет сама точка. Решение по запросу
    # принимает владелец, сюда оно не относится: /auth/admin-request/{id}/approve закрыт сессией.
    "/auth/admin-request",
}
# "/i18n/" — словарь интерфейса: подписи экранов, данных клиентов в нём нет, а нужен он
# до входа: на /login и на экране заявки мини-аппа
WHITE_PREFIX = ("/tg/webhook/", "/i18n/", "/static/")     # секрет вебхука проверяет app/tgbot.py;
# /static/ — картинки интерфейса (фоны обложки мини-аппа), данных в них нет и вход для них не нужен

# --------------------------------------------------------------------------- #
#  Гостевой режим (решение заказчика 22.09.2026): приложение открыто для всех
# --------------------------------------------------------------------------- #
# Ниже — ровно те адреса, которые работают без входа. Перечисляем методом и точным путём,
# а не префиксом: новый путь вида /analytics/risk-что-нибудь не должен открыться наружу молча.
# Всё изменяющее (справочники, БРВ, пороги, админка, задачи, журнал, пользователи, финансы,
# портфель, refresh) в этих списках отсутствует — значит, требует сессии администратора.
GUEST_GET_EXACT = {
    "/tg/me", "/tg/status", "/tg/bot-status",
    "/osgor/activities", "/osgor/brv",                 # БРВ читают все, правит PUT только админ
    "/legal/faq", "/legal/acts", "/legal/suggest",
    "/valuation/norms",                                # нормы износа: чтение открыто, запись — админ
    "/market/rows", "/market/series", "/market/status",
    "/market/branches", "/market/claims",              # подразделения и претензии из открытых отчётов НАПП
    "/stat/indicators", "/stat/risk-indicators",
    "/analytics/risk/fields", "/analytics/risk/thresholds", "/analytics/risk/docs",
    "/analytics/risk/presets", "/analytics/risk/last",
    "/analytics/risk/documents",       # свои файлы для анализа: вкладка «Фото» гостя
    "/chat/state",                     # диалог ИИ-сюрвейера: своё состояние по cookie gid
}
GUEST_GET_PREFIX = ("/reference/", "/stat/indicators/", "/analytics/risk/document/",
                    "/act/")                         # сюрвейерский акт: свой — проверяет app/act.py
GUEST_POST_EXACT = {"/calculate", "/osgor/quick", "/osgor/assess", "/legal/ask",
                    "/analytics/risk", "/analytics/risk/document",
                    # диалог ИИ-сюрвейера (app/surveyor_chat.py): один сценарий вместо трёх вкладок
                    "/chat/start", "/chat/message", "/chat/upload", "/chat/answer",
                    "/chat/analyze", "/chat/lang",
                    # сюрвейерский акт, лёгкая версия (app/act.py): фото и формирование акта
                    "/act/photos", "/act/make",
                    # оценка по объявлениям: снимки экрана сотрудника (лимит по файлам — в app/act.py)
                    "/act/market/shots"}
# отправка своего акта ботом (app/act.py): владельца и подпись Telegram проверяет обработчик
GUEST_POST_RE = re.compile(r"/act/[0-9a-f]{16}/send")
GUEST_PUT_EXACT = {"/analytics/risk/last"}             # свой последний выбор формы, без сумм и ПД
GUEST_DELETE_PREFIX = ("/analytics/risk/document/",)   # удалить свой файл раньше срока

# какие обращения гостя считаем (app/guest.py): бакет -> (метод, путь)
GUEST_BUCKET = {
    ("POST", "/analytics/risk"): "analysis",
    ("POST", "/calculate"): "analysis",
    ("POST", "/osgor/quick"): "analysis",
    ("POST", "/osgor/assess"): "analysis",
    ("POST", "/analytics/risk/document"): "upload",
    ("POST", "/legal/ask"): "legal",
    ("POST", "/chat/analyze"): "analysis",
    ("POST", "/chat/upload"): "upload",
    ("POST", "/chat/message"): "legal",
    # /act/photos здесь не считаем: app/act.py считает фото гостя по числу файлов (limits.guest_photos_per_hour)
    ("POST", "/act/make"): "analysis",
}
# страницы, на которых гостю выдаётся cookie заранее: файл он загрузит уже с ней
GUEST_COOKIE_PATHS = {"/tg", "/tg/me", "/tg/auth", "/tg/status"}

# --- только в режиме разработчика ---
DEV_ONLY = {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}

# --- только администратор ---
ADMIN_PREFIX = ("/admin", "/deploy/", "/tasks", "/reports", "/audit", "/users", "/approvals/admin",
                # генеральные соглашения, реестр знаний команды и нормативы — разделы администратора
                # (docs/Регистрация и роли.md, раздел 8 и 6.1 п. 4)
                "/general-agreements", "/knowledge", "/law-events")
# точечно: правка норм износа — админ, а чтение норм открыто любой роли
ADMIN_METHOD_PATH = {("POST", "/valuation/norms"), ("DELETE", "/valuation/norms"),
                     ("POST", "/valuation/settings"),
                     ("POST", "/lawwatch/check"),      # внеплановая сверка актов на lex.uz
                     ("POST", "/market/refresh"),      # перезабор отчётов НАПП
                     ("POST", "/market/knowledge/rebuild"),    # пересборка знаний о рынке (app/market_knowledge.py)
                     ("POST", "/market/competitors/refresh"),  # проход по сайтам конкурентов (app/competitors.py)
                     ("POST", "/exchange/refresh"),    # перезабор биржевых цен uzex.uz (app/uzex.py)
                     # справочники, которые админ правит из мини-приложения (задача 144)
                     ("PUT", "/osgor/brv"),            # размер БРВ для ОСГОР
                     ("PUT", "/analytics/risk/thresholds"),   # пороги уровня риска аналитики
                     ("PUT", "/act/settings"),         # пороги лёгкого движка акта (app/act.py)
                     ("POST", "/legal/reindex"),       # пересборка индекса законодательства (app/legal.py)
                     # админ-панель 02.10.2026: сотрудник вручную, импорт продуктов и страховых случаев
                     ("POST", "/tg/users/manual"),
                     ("POST", "/reference/products/import"), ("POST", "/reference/products/import/apply"),
                     ("POST", "/claims/import"), ("POST", "/claims/import/apply"),
                     ("PUT", "/claims/yearly")}             # итоги по году и продукту — ручной ввод (06.10.2026)
# то же по началу пути: у удаления нормы износа код в адресе (/valuation/norms/{code}),
# и точное совпадение из ADMIN_METHOD_PATH его не ловило
ADMIN_METHOD_PREFIX = {("DELETE", "/valuation/norms/"),
                       ("PUT", "/act/templates/"),           # шаблоны анализа по классам (app/act.py)
                       # минимальные ставки страховщика: правка и импорт из Excel (app/min_rates.py)
                       ("PUT", "/reference/min-rates/"), ("POST", "/reference/min-rates/"),
                       # карточка сотрудника и сброс пароля (/tg/users/{uid}, /tg/users/{uid}/reset-password);
                       # make-admin и revoke-admin под тот же префикс — они и так только для админа
                       ("PUT", "/tg/users/"), ("POST", "/tg/users/")}

# --- разделы, закрытые ролью (таблица прав: docs/Регистрация и роли.md, раздел 8) ---
# Админ проходит везде (auth.check_role), поэтому в списках его можно не повторять.
UNDERWRITING = ("андеррайтер", "актуарий", ADMIN)   # портфельный аудит, аналитика запросов
ACTUARY = ("актуарий", ADMIN)                       # калибровка, убытки, ёмкость и удержание
ROLE_PREFIX = (
    ("/portfolio", UNDERWRITING),        # портфельный аудит и история загрузок
    ("/analytics", UNDERWRITING),        # сводка по всем запросам компании
    ("/office/shelves", UNDERWRITING),   # библиотека документов компании
    ("/calibration", ACTUARY),           # калибровка коэффициентов по убыткам
    ("/claims", ACTUARY),                # убытки — исходные данные калибровки
    ("/capacity", ACTUARY),              # ёмкость и удержание
    ("/capacity-page", ACTUARY),         # та же ёмкость, страницей
    ("/accumulation", ACTUARY),          # накопление сумм против лимита на один риск
)
# Исключения из ROLE_PREFIX: аналитика риска открыта любой активной роли (задача 144, мини-приложение).
# Точные пути, а не префикс: /analytics/summary (сводка по всем запросам компании) остаётся закрытой.
# Запись порогов закрыта отдельно — ADMIN_METHOD_PATH.
ANY_ROLE_EXACT = {"/analytics/risk", "/analytics/risk/fields", "/analytics/risk/thresholds",
                  # задача 150: документы по продукту и договор для анализа (только свой — проверяет модуль)
                  "/analytics/risk/docs", "/analytics/risk/document",
                  # быстрый режим (22.09.2026): пресеты и последний выбор формы — любой активной роли
                  "/analytics/risk/presets", "/analytics/risk/last",
    "/analytics/risk/documents",       # свои файлы для анализа: вкладка «Фото» гостя
                  # юридические ответы (app/legal.py): читать может любой вошедший, включая сотрудника;
                  # перечисляем точно — пересборка индекса /legal/reindex закрыта администратором
                  "/legal/ask", "/legal/faq", "/legal/acts", "/legal/suggest"}
ANY_ROLE_PREFIX = ("/analytics/risk/document/",)
# Утверждение и отклонение расчёта калибровки меняет действующие коэффициенты — это запись
# в справочники, а она только у администратора (раздел 8, строка «Справочники, тарифы, версии»).
ADMIN_SUFFIX_UNDER = {"/calibration/runs": ("/approve", "/reject")}


# --------------------------------------------------------------------------- #
#  Режим разработчика
# --------------------------------------------------------------------------- #

def client_host(request: Request) -> str:
    """Реальный адрес собеседника из ASGI-scope. Заголовкам не верим."""
    c = request.scope.get("client")
    return (c[0] if c else "") or ""


def dev_bypass(request: Request) -> bool:
    """Локальная разработка: guard отключён только для соединения с самого компьютера."""
    if os.environ.get("SURVEYOR_DEV") != "1":
        return False
    if any(request.headers.get(h) for h in PROXY_HEADERS):
        return False                                   # пришли через прокси — это не «свой компьютер»
    return client_host(request) in LOCAL_HOSTS


# --------------------------------------------------------------------------- #
#  Кто пришёл
# --------------------------------------------------------------------------- #

def user_of(request: Request) -> Optional[dict]:
    """Пользователь по сессии (cookie или Bearer). Ошибка базы не должна открывать доступ."""
    token = auth.request_token(request)
    if not token:
        return None
    try:
        with db.tx() as con:
            return auth.session_user(con, token)
    except Exception as e:
        print("guard: не удалось проверить сессию:", e)
        return None


# --------------------------------------------------------------------------- #
#  Решение по пути
# --------------------------------------------------------------------------- #

def is_open(path: str, dev: bool) -> bool:
    if path in WHITE_EXACT or path.startswith(WHITE_PREFIX):
        return True
    return dev and path in DEV_ONLY


def guest_allowed(method: str, path: str) -> bool:
    """Открыт ли адрес без входа. Метод важен: GET /osgor/brv — всем, PUT — администратору."""
    m = method.upper()
    if m in ("GET", "HEAD"):
        return path in GUEST_GET_EXACT or path.startswith(GUEST_GET_PREFIX)
    if m == "POST":
        return path in GUEST_POST_EXACT or bool(GUEST_POST_RE.fullmatch(path))
    if m == "PUT":
        return path in GUEST_PUT_EXACT
    if m == "DELETE":
        return path.startswith(GUEST_DELETE_PREFIX)
    return False


def guest_limit(request: Request, method: str, path: str, gid: str):
    """Ограничение злоупотреблений для гостя. Вернёт готовый 429 или None."""
    bucket = GUEST_BUCKET.get((method.upper(), path))
    if not bucket:
        return None
    key = gid or client_host(request)          # нет cookie — считаем по адресу соединения
    res = guest.hit(bucket, key)
    if res["ok"]:
        return None
    # в журнал — только хэш ключа и счётчик: ни guest_id, ни IP в открытом виде
    print("guard: лимит гостя", bucket, guest.short(key), res["count"], "/", res["limit"], flush=True)
    return JSONResponse({"detail": guest.message(bucket, res["limit"]),
                         "limit": res["limit"], "window_hours": 1,
                         "retry_after_sec": res["retry_after"], "guest": True},
                        status_code=429, headers={"Retry-After": str(res["retry_after"])})


def needs_admin(method: str, path: str) -> bool:
    m = method.upper()
    if path.startswith(ADMIN_PREFIX) or (m, path) in ADMIN_METHOD_PATH:
        return True
    if any(m == pm and path.startswith(pp) for pm, pp in ADMIN_METHOD_PREFIX):
        return True
    for base, tails in ADMIN_SUFFIX_UNDER.items():     # /calibration/runs/{id}/approve и /reject
        if path.startswith(base + "/") and path.endswith(tails):
            return True
    return False


def allowed_roles(path: str):
    """Какие роли пускаем в раздел. None — ограничения по роли нет (нужен только вход)."""
    if path in ANY_ROLE_EXACT or path.startswith(ANY_ROLE_PREFIX):
        return None
    for prefix, roles in ROLE_PREFIX:
        if path == prefix or path.startswith(prefix + "/"):
            return roles
    return None


def wants_html(request: Request) -> bool:
    """Страница это или вызов API: браузер в навигации просит text/html."""
    if request.method.upper() not in ("GET", "HEAD"):
        return False
    return "text/html" in (request.headers.get("accept") or "")


def _deny(request: Request, code: int, detail: str):
    if code == 401 and wants_html(request):
        nxt = request.url.path + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse("/login?next=" + quote(nxt, safe=""), status_code=302)
    return JSONResponse({"detail": detail}, status_code=code)


# Временный пароль (app/staff.py): пока он не сменён, сессия открывает только эти адреса — проверка идёт
# ДО белого списка, иначе с временным паролем оставались бы доступны /auth/admin-request и подобные.
# Страницы /tg и /login, статика и вход заново — чтобы человек вообще мог дойти до смены пароля.
MUST_CHANGE_OPEN_EXACT = {"/tg", "/login", "/health", "/theme.js", "/favicon.ico", "/i18n.js",
                          "/auth/login", "/auth/verify-code", "/tg/me"}
MUST_CHANGE_OPEN_PREFIX = ("/static/", "/i18n/")


def must_change_blocks(path: str) -> bool:
    return not (path in auth.MUST_CHANGE_ALLOWED or path in MUST_CHANGE_OPEN_EXACT
                or path.startswith(MUST_CHANGE_OPEN_PREFIX))


def _must_change_deny():
    return JSONResponse({"detail": auth.MUST_CHANGE_DETAIL, "must_change_password": True}, status_code=403)


async def check(request: Request):
    """None — пропустить дальше; иначе готовый ответ-отказ."""
    path = request.url.path.rstrip("/") or "/"
    dev = dev_bypass(request)
    if dev:
        return None
    user = None
    if must_change_blocks(path) and auth.request_token(request):
        user = await run_in_threadpool(user_of, request)
        if user and user.get("must_change_password"):
            return _must_change_deny()
    if is_open(path, dev):
        return None
    if path in DEV_ONLY:                                  # документация API на рабочем сервере закрыта
        return _deny(request, 404, "страница недоступна")
    if user is None:
        user = await run_in_threadpool(user_of, request)
    if not user:
        # гостевой режим: приложение открыто для чтения и расчёта, менять данные нельзя
        if guest_allowed(request.method, path):
            return guest_limit(request, request.method, path, request.scope.get("surveyor_guest") or "")
        return _deny(request, 401, NEED_LOGIN)
    if user.get("must_change_password") and must_change_blocks(path):
        return _must_change_deny()
    if needs_admin(request.method, path) and user["role"] != ADMIN:
        return _deny(request, 403, "нужны права администратора")
    roles = allowed_roles(path)
    if roles is not None and user["role"] not in roles:
        return _deny(request, 403, "раздел доступен ролям: " + ", ".join(roles))
    request.scope["surveyor_user"] = user                  # чтобы обработчик не ходил в базу второй раз
    return None


class GuardMiddleware:
    """
    Проверка входа как «чистый» ASGI-слой. Раньше был @app.middleware("http") (BaseHTTPMiddleware):
    он перекладывает каждый ответ через дополнительную очередь и заметно добавляет к каждому запросу.
    Логика та же — функция check(); пользователь кладётся в scope, обработчик базу второй раз не читает.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        path = request.url.path.rstrip("/") or "/"
        # анонимный guest_id: нужен до обработчика (по нему ищутся «мои» загруженные файлы)
        gid = guest.from_request(request)
        fresh = ""
        if not gid and (guest_allowed(request.method, path) or path in GUEST_COOKIE_PATHS):
            gid = fresh = guest.new_id()
        if gid:
            scope["surveyor_guest"] = gid
        denied = await check(request)
        target = send
        if fresh:
            secure = guest.cookie_secure()

            async def target(msg, _send=send, _gid=fresh, _secure=secure):
                if msg["type"] == "http.response.start":
                    msg = dict(msg)
                    msg["headers"] = list(msg.get("headers") or []) + [
                        (b"set-cookie", guest.cookie_header(_gid, _secure))]
                await _send(msg)
        if denied is not None:
            await denied(scope, receive, target)
            return
        await self.app(scope, receive, target)


def install(app):
    """Подключает middleware и маршруты первого администратора."""
    app.add_middleware(GuardMiddleware)
    app.include_router(router)


# --------------------------------------------------------------------------- #
#  Первый администратор
# --------------------------------------------------------------------------- #

def _digest(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _same(a: str, b: str) -> bool:
    """Секрет может быть на кириллице — сравниваем отпечатки, а не строки."""
    return bool(a) and bool(b) and hmac.compare_digest(_digest(a), _digest(b))


def users_count(con) -> int:
    """Сколько АДМИНИСТРАТОРОВ на сервере. Первый вход по коду нужен, пока нет ни одного админа:
    обычные или демо-пользователи могут появиться раньше (демо-данные при старте), это не должно
    закрывать дорогу первому администратору."""
    return con.execute("SELECT COUNT(*) FROM users WHERE role=?", (ADMIN,)).fetchone()[0]


def _setting_set(con, key: str, value: str):
    con.execute("DELETE FROM app_settings WHERE key=?", (key,))
    con.execute("INSERT INTO app_settings (key, value, updated_at) VALUES (?,?,?)", (key, value, db.now()))


def ensure_bootstrap_code() -> Optional[str]:
    """
    Пустой сервер без заданного ADMIN_BOOTSTRAP_CODE: генерируем код и печатаем его в журнал сервера.
    Идемпотентно: если код уже задан (переменной или в настройках) или пользователи есть — ничего.
    Возвращает сгенерированный код или None.
    """
    try:
        with db.tx() as con:
            if users_count(con) > 0:
                return None
            if (llm.get("ADMIN_BOOTSTRAP_CODE") or "").strip():
                return None
            code = secrets.token_urlsafe(12)
            _setting_set(con, "ADMIN_BOOTSTRAP_CODE", code)
            con.execute("DELETE FROM app_settings WHERE key='ADMIN_BOOTSTRAP_USED'")
    except Exception as e:
        print("guard: код первого администратора не создан:", e)
        return None
    print("=" * 72)
    print("ПЕРВЫЙ ЗАПУСК: пользователей нет. Код первого администратора (одноразовый):")
    print("    " + code)
    print("Откройте /login → «Первый вход» и введите этот код. После входа код перестанет работать.")
    print("=" * 72, flush=True)
    return code


class BootstrapIn(BaseModel):
    code: str = ""
    full_name: str = Field(min_length=3)
    login: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=8)


def bootstrap_admin(con, data: BootstrapIn) -> dict:
    """Создаёт первого администратора по одноразовому коду. Код в журнал не пишем — только маску."""
    if users_count(con) > 0:
        raise HTTPException(409, "Администратор уже создан. Войдите по логину и паролю или попросите "
                                 "действующего администратора подтвердить вашу заявку")
    code = (data.code or "").strip()
    setting = (llm.get("ADMIN_BOOTSTRAP_CODE") or "").strip()
    used = (llm.get("ADMIN_BOOTSTRAP_USED") or "").strip()
    digest = _digest(code) if code else ""
    if not code:
        raise HTTPException(422, "Укажите код первого администратора")
    if used and digest and hmac.compare_digest(used, digest):
        raise HTTPException(403, "Этот код уже использован")
    if not setting:
        raise HTTPException(403, "Код первого администратора не задан. Он печатается в журнале сервера "
                                 "при первом запуске или задаётся переменной ADMIN_BOOTSTRAP_CODE")
    if not _same(setting, code):
        raise HTTPException(403, "Код не подходит")

    out = auth.register_user(con, auth.RegisterIn(full_name=data.full_name, login=data.login,
                                                  password=data.password, role=ADMIN))
    # регистрация делает первого пользователя активным только на пустом сервере; здесь администратор
    # создаётся по коду, поэтому активируем его явно — даже если демо- или обычные пользователи уже есть
    con.execute("UPDATE users SET status=?, approved_by='bootstrap', approved_at=? WHERE id=?",
                (auth.STATUS_ACTIVE, db.now(), out["id"]))
    _setting_set(con, "ADMIN_BOOTSTRAP_USED", digest)
    con.execute("DELETE FROM app_settings WHERE key='ADMIN_BOOTSTRAP_CODE'")
    db.audit(con, out["login"], "первый администратор по коду", f"user:{out['id']}",
             {"код": llm.mask_key(code)})
    return out | {"message": "Администратор создан, можно входить"}


@router.get("/auth/bootstrap-needed")
def bootstrap_needed():
    """Пуст ли сервер. Наружу уходит только «да/нет» — ни кода, ни имён."""
    with db.tx() as con:
        return {"needed": users_count(con) == 0}


@router.post("/auth/bootstrap")
def bootstrap(body: BootstrapIn):
    with db.tx() as con:
        return bootstrap_admin(con, body)
