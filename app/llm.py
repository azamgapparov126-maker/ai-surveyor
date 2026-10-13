"""
Единый клиент к ИИ (OpenAI-совместимый chat endpoint) — только стандартная библиотека.

Главное правило: персональные данные граждан РУз не покидают Узбекистан (правило проекта № 8).
Поэтому ЛЮБОЙ текст, уходящий во внешнюю модель, проходит через mask_pd(): ФИО, ПИНФЛ, паспорт,
ИНН, телефон, e-mail, госномер, номер карты и кадастровый номер заменяются на плейсхолдеры.
Обойти маскировку нельзя: наружу открыт только chat(), внутри которого она вызывается.

Настройки берутся так: таблица app_settings (приоритет) → переменная окружения → файлы .env и .secrets.env → значение по умолчанию.
Ключи наружу отдаются маской вида "sk-...abcd" — целиком их не показывает ни один ответ API.

Если ключа нет, status() честно говорит «ИИ не подключён», а все вызовы возвращают None:
везде, где ИИ используется, обязателен запасной вариант без него.

Провайдеры (OpenAI-совместимый /chat/completions):
  kimi      — https://api.moonshot.ai/v1, модель по умолчанию kimi-k3
              (проверено 20.09.2026 на platform.moonshot.ai/docs/api/chat — в примере официальной
               документации стоит "model": "kimi-k3"; в прайс-листе также kimi-k2.6 и kimi-k2.7-code)
  openai    — https://api.openai.com/v1
  anthropic — другой формат запроса, здесь НЕ поддержан: выбор провайдера 'anthropic' работает
              как 'none' (честная ошибка), чтобы не притворяться рабочим.
  gemini    — https://generativelanguage.googleapis.com/v1beta, модели gemini-2.5-flash и
              gemini-2.5-pro. Формат запроса свой (contents/parts), ключ — в заголовке
              x-goog-api-key. Умеет вложения inline_data: JPEG/PNG и PDF (сканы, фото объекта).
              Ключ берётся из LLM_API_KEY, а если он пуст — из GEMINI_API_KEY (.secrets.env).
  none      — ИИ выключен.
"""
import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db

router = APIRouter()
ROOT = Path(__file__).resolve().parent.parent
# файлы с настройками: .env (основной) и .secrets.env (ключи, в репозиторий не попадает);
# .secrets.env дополняет .env, но не затирает уже найденные там значения
ENV_FILES = (ROOT / ".env", ROOT / ".secrets.env")

TIMEOUT_SEC = 40
RETRIES = 1                       # одна повторная попытка (всего две), пауза растёт: 1 с, 2 с
RETRY_BASE_SEC = 1
MAX_PROMPT_CHARS = 12000          # длинные документы режем: и дешевле, и меньше риска

# Вложения (только Gemini): фото и сканы уходят inline_data в base64.
# Пределы намеренно жёсткие: один запрос не должен весить больше загрузки одного документа.
INLINE_MIME = {"image/jpeg", "image/jpg", "image/png", "application/pdf"}
INLINE_MAX_FILES = 10
INLINE_MAX_BYTES = 15 * 1024 * 1024        # суммарно на один запрос
INLINE_MAX_ONE = 10 * 1024 * 1024          # на один файл

SETTING_KEYS = ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "GEMINI_API_KEY",
                "TELEGRAM_BOT_TOKEN", "PD_MODE", "SERVER_URL",
                # бот Telegram (app/tgbot.py): секрет вебхука, код первого администратора,
                # запасной режим опроса и версия текста согласия на обработку ПД
                "TG_WEBHOOK_SECRET", "ADMIN_BOOTSTRAP_CODE", "ADMIN_BOOTSTRAP_USED",
                "TG_ADMIN_USERNAME", "TG_OWNER_CHAT_ID",   # username Telegram первого администратора: входит админом без кода
                "TG_POLLING", "CONSENT_VERSION",
                # вход через Google (app/google_auth.py): приложение в Google Cloud Console
                # и необязательное ограничение по доменам почты, через запятую
                "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_ALLOWED_DOMAINS",
                # Gemini 2.5: бюджет «размышления» в токенах (0 = выключить, пусто = по модели)
                "LLM_THINKING_BUDGET")

PROVIDERS = {
    "kimi":      {"base_url": "https://api.moonshot.ai/v1", "model": "kimi-k3",       "name": "Kimi (Moonshot)"},
    "openai":    {"base_url": "https://api.openai.com/v1",  "model": "gpt-4o-mini",   "name": "OpenAI"},
    "anthropic": {"base_url": "https://api.anthropic.com",  "model": "claude-sonnet", "name": "Anthropic (не подключён)"},
    "gemini":    {"base_url": "https://generativelanguage.googleapis.com/v1beta",
                  "model": "gemini-3.5-flash-lite", "name": "Google Gemini"},
    "none":      {"base_url": "",                           "model": "",              "name": "ИИ выключен"},
}

DEFAULTS = {"LLM_PROVIDER": "none", "PD_MODE": "test", "SERVER_URL": "http://127.0.0.1:8000",
            "LLM_BASE_URL": "", "LLM_MODEL": "", "LLM_API_KEY": "", "GEMINI_API_KEY": "",
            "TELEGRAM_BOT_TOKEN": "",
            "TG_WEBHOOK_SECRET": "", "ADMIN_BOOTSTRAP_CODE": "", "ADMIN_BOOTSTRAP_USED": "",
            "TG_POLLING": "0", "CONSENT_VERSION": "черновик-1",
            "GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": "", "GOOGLE_ALLOWED_DOMAINS": "",
            "LLM_THINKING_BUDGET": ""}

NOT_CONNECTED = "ИИ не подключён: не задан ключ API"

last_error = {"text": None}       # текст последней ошибки — показываем в админке


# --------------------------------------------------------------------------- #
#  Настройки
# --------------------------------------------------------------------------- #

def _env_file() -> dict:
    """Чтение .env и .secrets.env (без пакета python-dotenv): KEY=value, строки с # пропускаем.
    Первый файл главнее: значения из .secrets.env только дополняют.
    encoding utf-8-sig — иначе первый ключ файла с BOM читается как "﻿KEY"."""
    out = {}
    for path in ENV_FILES:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except Exception:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in out:
                out[k] = v
    return out


def _db_settings() -> dict:
    try:
        with db.tx() as con:
            return {r["key"]: r["value"] for r in db.rows(con, "SELECT key, value FROM app_settings")}
    except Exception:          # таблицы ещё нет (первый запуск до ensure_schema)
        return {}


def get(key: str, default: str = None) -> str:
    """Значение настройки: база → окружение → .env и .secrets.env → значение по умолчанию."""
    v = _db_settings().get(key)
    if v is None or v == "":
        v = os.environ.get(key) or _env_file().get(key)
    if v is None or v == "":
        v = DEFAULTS.get(key, "") if default is None else default
    return v


def set_many(values: dict, who: str = "админ") -> list:
    """Сохраняет настройки. Пустая строка = не менять (так UI не стирает ключ пустым полем)."""
    changed = []
    with db.tx() as con:
        for k, v in values.items():
            if k not in SETTING_KEYS or v is None or str(v).strip() == "":
                continue
            v = str(v).strip()
            con.execute("DELETE FROM app_settings WHERE key=?", (k,))
            con.execute("INSERT INTO app_settings (key, value, updated_at) VALUES (?,?,?)", (k, v, db.now()))
            changed.append(k)
        if changed:
            # в журнал пишем только названия изменённых настроек, значения (ключи!) — никогда
            db.audit(con, who, "изменены настройки", "app_settings", {"keys": changed})
    return changed


def mask_key(value: str) -> str:
    """Маска ключа для показа наружу: sk-...abcd."""
    if not value:
        return ""
    v = str(value)
    head = v[:3] if len(v) > 10 else ""
    return f"{head}...{v[-4:]}" if len(v) > 8 else "..."


def provider() -> str:
    p = (get("LLM_PROVIDER") or "none").strip().lower()
    return p if p in PROVIDERS else "none"


def base_url() -> str:
    return (get("LLM_BASE_URL") or PROVIDERS[provider()]["base_url"]).rstrip("/")


def model() -> str:
    return get("LLM_MODEL") or PROVIDERS[provider()]["model"]


def api_key() -> str:
    """Ключ провайдера. У Gemini общий LLM_API_KEY может быть пуст — тогда берём GEMINI_API_KEY."""
    key = get("LLM_API_KEY") or ""
    if not key and provider() == "gemini":
        key = get("GEMINI_API_KEY") or ""
    return key


def enabled() -> bool:
    return provider() in ("kimi", "openai", "gemini") and bool(api_key()) and bool(base_url())


def supports_files() -> bool:
    """Вложения (фото, PDF) умеет только Gemini; остальным отдаём извлечённый текст."""
    return provider() == "gemini" and enabled()


def status() -> dict:
    """Честный ответ о состоянии ИИ. Никогда не показывает ключ целиком."""
    p = provider()
    if p == "none":
        reason = "ИИ выключен в настройках (провайдер «none»)"
    elif p == "anthropic":
        reason = "Провайдер Anthropic в этой версии не поддержан — выберите kimi или openai"
    elif not api_key():
        reason = NOT_CONNECTED
    else:
        reason = "Ключ задан. Нажмите «Проверить», чтобы убедиться в связи"
    return {"provider": p, "provider_name": PROVIDERS[p]["name"], "model": model(),
            "base_url": base_url(), "key_mask": mask_key(api_key()),
            "connected": enabled(), "reason": reason,
            "pd_mode": get("PD_MODE"),
            "note": "Персональные данные маскируются до отправки: ФИО, ПИНФЛ, паспорт, ИНН, телефон, e-mail, "
                    "банковский счёт, карта, МФО"}


# --------------------------------------------------------------------------- #
#  Маскировка персональных данных
# --------------------------------------------------------------------------- #

# слова с заглавной буквы, которые ФИО не являются: иначе «Республика Узбекистан» станет [ФИО]
NAME_STOP = {
    "республика", "узбекистан", "ташкент", "ташкентская", "область", "город", "район", "махалля",
    "закон", "положение", "постановление", "кабинет", "министров", "министерство", "агентство",
    "страхование", "страховая", "страховщик", "компания", "общество", "акционерное", "полис",
    "договор", "приложение", "таблица", "сумма", "премия", "ставка", "объект", "здание", "квартира",
    "автомобиль", "марка", "модель", "янв", "инсон", "кадастр", "техпаспорт", "паспорт", "выписка",
    "государственный", "реестр", "недвижимости", "имущества", "кодекс", "гражданский", "статья",
}
LAT_STOP = {"inson", "insurance", "uzbekistan", "tashkent", "republic", "company", "policy",
            "model", "object", "total", "sum", "the", "and", "for", "llc", "jsc"}

_CYR_WORD = r"[А-ЯЁ][а-яё]{1,}"
_LAT_WORD = r"[A-Z][a-z]{1,}"

# Порядок важен: сначала длинные/структурные шаблоны, потом короткие цифровые.
PD_RULES = [
    # e-mail
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "[E-MAIL]"),
    # кадастровый номер: 10:09:05:01:0123:0001
    (re.compile(r"\b\d{1,4}(?::\d{1,5}){3,6}\b"), "[КАДАСТР]"),
    # банковские реквизиты: МФО (5 цифр) и счёт после подписи «р/с», «х/р», «h/r», «IBAN» …
    (re.compile(r"((?i:\bМФО|\bMFO|\bБИК|\bBIK)\s*[:№]?\s*)\d{5}(?!\d)"), r"\1[МФО]"),
    (re.compile(r"((?i:р/с|р\.\s?с\.|х/р|x/r|h/r|\bIBAN|расч[её]тный\s+сч[её]т|hisob\s+raqami|ҳисоб\s+рақами|"
                r"account\s+(?:no\.?|number))\s*[:№]?\s*)(?:[A-Z]{2}\d{2}[A-Z0-9 ]{8,34}[A-Z0-9]|\d[\d \-]{8,30}\d)"),
     r"\1[СЧЁТ]"),
    # счёт без подписи: 20 цифр подряд или группами по 4–5 (суммы денег — группы по 3, их не трогаем)
    (re.compile(r"(?<![\d:.,])\d{20}(?![\d:])|(?<![\d:.,])\d{4,5}(?:[ \-]\d{4,5}){3,4}(?![\d:])"),
     lambda m: "[СЧЁТ]" if len(re.sub(r"\D", "", m.group(0))) == 20 else m.group(0)),
    # номер карты: 16 цифр группами
    (re.compile(r"(?<!\d)\d{4}[ \-]?\d{4}[ \-]?\d{4}[ \-]?\d{4}(?!\d)"), "[НОМЕР КАРТЫ]"),
    # ПИНФЛ — 14 цифр
    # (не внутри VIN: перед цифрами латинская буква — это номер кузова, а не ПИНФЛ)
    (re.compile(r"(?<![\dA-Za-z])\d{14}(?!\d)"), "[ПИНФЛ]"),
    # серия и номер паспорта: AA 1234567 / АА1234567
    (re.compile(r"\b[A-ZА-ЯЁ]{2}\s?[\-№]?\s?\d{7}\b"), "[ПАСПОРТ]"),
    # госномер ТС Узбекистана: 01 A 123 AA и 01 123 AAA
    (re.compile(r"\b\d{2}\s?[A-ZА-Я]\s?\d{3}\s?[A-ZА-Я]{2}\b"), "[ГОСНОМЕР]"),
    (re.compile(r"\b\d{2}\s?\d{3}\s?[A-ZА-Я]{3}\b"), "[ГОСНОМЕР]"),
    # телефон: +998 90 123-45-67 и варианты с разделителями
    (re.compile(r"(?<![\d\-:])(?:\+?998|8)[\s\-()]*\d{2}[\s\-()]*\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)"), "[ТЕЛЕФОН]"),
    (re.compile(r"(?<![\d\-:])\d{2}[\s\-]\d{3}[\s\-]\d{2}[\s\-]\d{2}(?!\d)"), "[ТЕЛЕФОН]"),
    # ИНН юр./физ. лица — 9 цифр
    (re.compile(r"(?<![\d\-:])\d{9}(?!\d)"), "[ИНН]"),
]

# ФИО: 2–4 слова с заглавной подряд (кириллица или латиница) либо «Фамилия И. О.»
# _SP — пробел внутри имени: перевод строки им не считается, иначе маска перескакивает
# на следующую строку и съедает её подпись («Домашний адрес», «Адрес объекта»)
_SP = r"[^\S\r\n]+"
_SP0 = r"[^\S\r\n]*"                 # необязательный пробел — тоже без перевода строки
NAME_INITIALS = re.compile(rf"\b{_CYR_WORD}{_SP}[А-ЯЁ]\.{_SP0}[А-ЯЁ]\.|\b[А-ЯЁ]\.{_SP0}[А-ЯЁ]\.{_SP0}{_CYR_WORD}")
NAME_CYR = re.compile(rf"\b{_CYR_WORD}(?:{_SP}{_CYR_WORD}){{1,3}}\b")
NAME_LAT = re.compile(rf"\b{_LAT_WORD}(?:{_SP}{_LAT_WORD}){{1,3}}\b")


# Марки ТС и техники латиницей: «Chevrolet Cobalt 2022» — описание объекта, а не ФИО. Слова, которые бывают
# именами или фамилиями (John, Li, Man, Holland, Case, Land), сюда намеренно не входят.
LAT_BRANDS = {
    "chevrolet", "daewoo", "ravon", "lada", "kia", "hyundai", "toyota", "lexus", "nissan", "honda", "mazda",
    "mitsubishi", "subaru", "suzuki", "isuzu", "mercedes", "benz", "bmw", "audi", "volkswagen", "skoda", "opel",
    "ford", "renault", "peugeot", "citroen", "volvo", "porsche", "byd", "chery", "geely", "haval", "jac", "changan",
    "jetour", "exeed", "tesla", "zeekr", "faw", "dongfeng", "foton", "howo", "sinotruk", "shacman", "kamaz",
    "scania", "daf", "iveco", "maz", "gaz", "uaz", "jeep", "dodge", "rover", "cadillac", "infiniti", "caterpillar",
    "komatsu", "hitachi", "liebherr", "jcb", "hyster", "claas", "deere", "kubota", "sany", "xcmg", "zoomlion",
    "shantui", "lonking", "liugong", "doosan", "yutong", "higer", "otokoch", "samand", "matiz", "nexia", "spark",
    "cobalt", "lacetti", "gentra", "malibu", "captiva", "tracker", "equinox", "tahoe", "damas", "labo", "onix",
    "monza", "camry", "corolla", "sonata", "elantra", "tucson", "sportage", "sorento", "solaris", "cerato",
    "optima", "largus", "vesta", "granta", "niva",
}
# слова рядом, по которым видно, что перед нами ТС, а не человек
_VEHICLE_CTX = re.compile(r"(?i)(легков|грузов|автомоб|машин|марк|модел|\bтс\b|спецтехник|тягач|прицеп|автобус|"
                          r"трактор|погрузчик|экскаватор|\bcar\b|vehicle|avto|mashina)")
_YEAR_AFTER = re.compile(r"^[^\S\r\n]*,?[^\S\r\n]*(?:19[5-9]\d|20[0-4]\d)(?!\d)")


def _lat_not_person(m) -> bool:
    """Латинские слова с заглавной — не ФИО: есть известная марка ТС или техники; либо два слова, сразу за ними
    год выпуска (1950–2049) и рядом слово «легковой», «автомобиль», «модель» и т. п."""
    words = [w.lower().strip(".,") for w in m.group(0).split()]
    if any(w in LAT_BRANDS for w in words):
        return True
    if len(words) == 2 and _YEAR_AFTER.match(m.string[m.end():]):
        around = m.string[max(0, m.start() - 40):m.end() + 40]
        return bool(_VEHICLE_CTX.search(around))
    return False


def _mask_names(text: str) -> str:
    def repl(m, stop):
        words = m.group(0).split()
        if any(w.lower().strip(".,") in stop for w in words):
            return m.group(0)
        if stop is LAT_STOP and _lat_not_person(m):
            return m.group(0)
        return "[ФИО]"
    text = NAME_INITIALS.sub("[ФИО]", text)
    text = NAME_CYR.sub(lambda m: repl(m, NAME_STOP), text)
    text = NAME_LAT.sub(lambda m: repl(m, LAT_STOP), text)
    return text


def mask_names(text: str) -> str:
    """Только правило ФИО, без остальных правил PD_RULES — публичное имя для app/ingest.py."""
    return _mask_names(text or "")


def mask_name_initials(text: str) -> str:
    """Только «Фамилия И. О.»: в характеристиках объекта такое написание — точно человек."""
    return NAME_INITIALS.sub("[ФИО]", text or "")


def mask_pd(text) -> str:
    """
    Заменяет персональные данные на плейсхолдеры. Лучше замаскировать лишнее, чем выпустить
    данные человека за пределы страны (правило проекта № 8).
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    out = text
    for rx, placeholder in PD_RULES:
        out = rx.sub(placeholder, out)
    return _mask_names(out)


def has_pd(text: str) -> bool:
    """Проверка «в тексте ещё осталось похожее на ПД» — для тестов и самоконтроля."""
    return mask_pd(text) != (text or "")


# --------------------------------------------------------------------------- #
#  Вызов модели
# --------------------------------------------------------------------------- #

def _log_call(purpose: str, ms: int, ok: bool, usage: dict = None, error: str = None):
    usage = usage or {}
    try:
        with db.tx() as con:
            con.execute("""INSERT INTO llm_calls (ts, purpose, provider, model, prompt_tokens,
                           completion_tokens, total_tokens, ms, ok, error) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (db.now(), purpose, provider(), model(), usage.get("prompt_tokens"),
                         usage.get("completion_tokens"), usage.get("total_tokens"),
                         int(ms), 1 if ok else 0, error))
    except Exception as e:          # журнал не должен ронять основную работу
        print("журнал вызовов ИИ:", e)


def _friendly(err: Exception) -> str:
    """Понятные сообщения по-русски вместо трассировок."""
    if isinstance(err, urllib.error.HTTPError):
        code = err.code
        if code in (401, 403):
            return "Ключ API не принят: неверный, отозван или нет прав. Проверьте ключ в настройках"
        if code == 404:
            return "Адрес сервиса или название модели неверны — проверьте LLM_BASE_URL и LLM_MODEL"
        if code == 429:
            return "Превышен лимит запросов или закончились средства на счёте у поставщика ИИ"
        if code >= 500:
            return f"Сервис ИИ временно недоступен (ошибка {code}) — попробуйте позже"
        return f"Сервис ИИ отклонил запрос (ошибка {code})"
    if isinstance(err, urllib.error.URLError):
        return "Нет связи с сервисом ИИ: проверьте интернет на сервере и адрес LLM_BASE_URL"
    if isinstance(err, TimeoutError):
        return "Сервис ИИ не ответил вовремя"
    return f"Ошибка обращения к ИИ: {type(err).__name__}"


def _post(url: str, body: dict, headers: dict, timeout: Optional[float] = None) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "INSON-surveyor/1.0", **headers})
    with urllib.request.urlopen(req, timeout=timeout or TIMEOUT_SEC) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _request(messages: list, max_tokens: int, temperature: float, timeout: Optional[float] = None) -> dict:
    """OpenAI-совместимый вызов (kimi, openai)."""
    return _post(base_url() + "/chat/completions",
                 {"model": model(), "messages": messages,
                  "max_tokens": max_tokens, "temperature": temperature},
                 {"Authorization": "Bearer " + api_key()}, timeout)


# ---------- Gemini ----------

def prepare_files(files: Optional[list]) -> tuple:
    """
    Приводит вложения к виду для inline_data и отсекает лишнее.

    files — список {"name":..., "mime":..., "data": bytes} или {"path": ...}.
    Возвращает (parts, notes): parts готовы к отправке, notes — понятные причины отказа,
    которые показываем человеку («файл больше 10 МБ», «формат не принимается»).
    """
    parts, notes, total = [], [], 0
    for f in (files or []):
        name = str(f.get("name") or "файл")
        if len(parts) >= INLINE_MAX_FILES:
            notes.append(f"{name}: за один раз модели показываем не больше {INLINE_MAX_FILES} файлов")
            continue
        data = f.get("data")
        if data is None and f.get("path"):
            try:
                data = Path(f["path"]).read_bytes()
            except Exception:
                notes.append(f"{name}: файл не прочитан")
                continue
        if not data:
            notes.append(f"{name}: пустой файл")
            continue
        mime = (f.get("mime") or "").split(";")[0].strip().lower()
        if mime == "image/jpg":
            mime = "image/jpeg"
        if mime not in INLINE_MIME:
            notes.append(f"{name}: модели напрямую отдаём только JPEG, PNG и PDF — "
                         f"из остальных файлов берём текст")
            continue
        if len(data) > INLINE_MAX_ONE:
            notes.append(f"{name}: больше {INLINE_MAX_ONE // (1024 * 1024)} МБ — не отправлен")
            continue
        if total + len(data) > INLINE_MAX_BYTES:
            notes.append(f"{name}: не поместился в один запрос "
                         f"(предел {INLINE_MAX_BYTES // (1024 * 1024)} МБ) — отправьте отдельно")
            continue
        total += len(data)
        parts.append({"inline_data": {"mime_type": mime,
                                      "data": base64.b64encode(data).decode("ascii")}})
    return parts, notes


def thinking_budget() -> int:
    """
    Бюджет «размышления» Gemini 2.5 в токенах. У flash он по умолчанию включён и съедает
    maxOutputTokens: ответ приходит пустым или обрезанным при finishReason=MAX_TOKENS
    (проверено 22.09.2026: candidatesTokenCount 22–30 при total 2000+). Поэтому:
    flash — 0 (выключено), pro — небольшой бюджет (у pro отключить нельзя).
    Переопределяется настройкой LLM_THINKING_BUDGET.
    """
    raw = (get("LLM_THINKING_BUDGET") or "").strip()
    if raw:
        try:
            return max(0, int(float(raw)))
        except ValueError:
            pass
    return 512 if "pro" in (model() or "").lower() else 0


def _gemini_body(messages: list, max_tokens: int, temperature: float, file_parts: list) -> dict:
    system = "\n".join(m["content"] for m in messages if m.get("role") == "system")
    contents = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        contents.append({"role": "model" if role == "assistant" else "user",
                         "parts": [{"text": m.get("content") or ""}]})
    if not contents:
        contents = [{"role": "user", "parts": [{"text": ""}]}]
    if file_parts:                       # вложения кладём к последней реплике человека
        contents[-1]["parts"] = list(contents[-1]["parts"]) + list(file_parts)
    cfg = {"maxOutputTokens": int(max_tokens), "temperature": temperature}
    # thinkingConfig понимают только модели 2.5: у gemini-3.* это поле даёт 400 (проверено 22.09.2026)
    if "2.5" in (model() or ""):
        cfg["thinkingConfig"] = {"thinkingBudget": thinking_budget()}
    body = {"contents": contents, "generationConfig": cfg}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    return body


def _request_gemini(messages: list, max_tokens: int, temperature: float, file_parts: list,
                    timeout: Optional[float] = None) -> dict:
    url = f"{base_url()}/models/{model()}:generateContent"
    return _post(url, _gemini_body(messages, max_tokens, temperature, file_parts),
                 {"x-goog-api-key": api_key()}, timeout)


TRUNCATED = "ответ обрезан: модель не уложилась в лимит вывода"


def _gemini_text(data: dict) -> tuple:
    """(текст, finish_reason). Пустой текст при MAX_TOKENS — не успех, а обрезанный ответ."""
    cand = (data.get("candidates") or [{}])[0]
    parts = ((cand.get("content") or {}).get("parts") or [])
    text = "".join(p.get("text") or "" for p in parts)
    return (text or None), str(cand.get("finishReason") or "")


def _looks_cut(text: Optional[str], finish: str) -> bool:
    """Обрыв: лимит вывода исчерпан, а текст пуст или кончается на полуслове."""
    if finish != "MAX_TOKENS":
        return False
    t = (text or "").strip()
    return not t or t[-1] not in ".!?»)”:"


def _gemini_usage(data: dict) -> dict:
    u = data.get("usageMetadata") or {}
    return {"prompt_tokens": u.get("promptTokenCount"),
            "completion_tokens": u.get("candidatesTokenCount"),
            "thoughts_tokens": u.get("thoughtsTokenCount"),
            "total_tokens": u.get("totalTokenCount")}


def chat_raw(purpose: str, messages: list, max_tokens: int = 700, temperature: float = 0.2,
             files: Optional[list] = None, timeout: Optional[float] = None,
             retries: Optional[int] = None) -> dict:
    """
    Низкий уровень: готовый список сообщений (роли system/user/assistant) и вложения.
    Маскировка персональных данных обязательна и делается здесь — обойти её нельзя.
    timeout — секунд на запрос (по умолчанию TIMEOUT_SEC), retries — повторов (по умолчанию RETRIES);
    retries=0 — ровно одна попытка, без повтора и без второго запроса при обрыве ответа.
    Возвращает {"text", "ok", "notes", "ms", "reason"}; text=None, если ИИ недоступен.
    """
    retries = RETRIES if retries is None else max(0, int(retries))
    last_error["text"] = None
    notes = []
    if not enabled():
        last_error["text"] = status()["reason"]
        _log_call(purpose, 0, False, error=last_error["text"])
        return {"text": None, "ok": False, "notes": notes, "ms": 0, "reason": last_error["text"]}
    safe = []
    for m in messages:
        role = m.get("role") if m.get("role") in ("system", "user", "assistant") else "user"
        content = mask_pd(m.get("content"))
        safe.append({"role": role, "content": content[:MAX_PROMPT_CHARS]})
    file_parts = []
    if files:
        if supports_files():
            file_parts, notes = prepare_files(files)
        else:
            notes.append("Текущий провайдер ИИ не принимает файлы — отправлен только текст")
    last_err = None
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            if provider() == "gemini":
                data = _request_gemini(safe, max_tokens, temperature, file_parts, timeout)
                text, finish = _gemini_text(data)
                usage = _gemini_usage(data)
                if _looks_cut(text, finish) and retries == 0:
                    ms = (time.time() - t0) * 1000
                    _log_call(purpose, ms, False, usage, TRUNCATED)
                    last_error["text"] = TRUNCATED
                    return {"text": None, "ok": False, "notes": notes, "ms": int(ms), "reason": TRUNCATED}
                if _looks_cut(text, finish):
                    # одна попытка с увеличенным лимитом; если и она обрывается — не выдаём обрубок
                    data = _request_gemini(safe, min(int(max_tokens) * 3, 8192), temperature, file_parts,
                                           timeout)
                    text, finish = _gemini_text(data)
                    usage = _gemini_usage(data)
                    if _looks_cut(text, finish):
                        ms = (time.time() - t0) * 1000
                        _log_call(purpose, ms, False, usage, TRUNCATED)
                        last_error["text"] = TRUNCATED
                        return {"text": None, "ok": False, "notes": notes, "ms": int(ms),
                                "reason": TRUNCATED}
            else:
                data = _request(safe, max_tokens, temperature, timeout)
                text = (data.get("choices") or [{}])[0].get("message", {}).get("content")
                usage = data.get("usage") or {}
            ms = (time.time() - t0) * 1000
            _log_call(purpose, ms, bool(text), usage, None if text else "пустой ответ модели")
            return {"text": text, "ok": bool(text), "notes": notes, "ms": int(ms),
                    "reason": None if text else "пустой ответ модели"}
        except Exception as e:
            last_err = e
            _log_call(purpose, (time.time() - t0) * 1000, False, error=_friendly(e))
            if isinstance(e, urllib.error.HTTPError) and e.code in (400, 401, 403, 404):
                break                    # повтор не поможет
            if attempt < retries:        # 429 и 5xx: ждём с нарастающей паузой
                # 503 у Gemini — «модель перегружена»: повтор через 2 с обычно проходит
                busy = isinstance(e, urllib.error.HTTPError) and e.code == 503
                time.sleep(2 if busy else RETRY_BASE_SEC * (2 ** attempt))
    last_error["text"] = _friendly(last_err) if last_err else None
    return {"text": None, "ok": False, "notes": notes, "ms": 0, "reason": last_error["text"]}


def chat(purpose: str, system: str, user: str, max_tokens: int = 700,
         temperature: float = 0.2, files: Optional[list] = None,
         timeout: Optional[float] = None) -> Optional[str]:
    """
    Единственная точка обращения к модели. Маскирует персональные данные, логирует метрики.
    Возвращает текст ответа или None — вызывающий код обязан уметь работать без ИИ.
    """
    return chat_raw(purpose, [{"role": "system", "content": system},
                              {"role": "user", "content": user}],
                    max_tokens, temperature, files, timeout=timeout)["text"]


def ping() -> dict:
    """Короткий запрос «скажи ОК» — проверка связи и ключа."""
    if not enabled():
        return {"ok": False, "reason": status()["reason"], "provider": provider(), "model": model()}
    t0 = time.time()
    answer = chat("проверка связи", "Отвечай одним словом.", "Ответь словом: ОК", max_tokens=256)   # у Gemini 2.5 часть лимита уходит на «размышление» — 10 токенов дают пустой ответ
    ms = int((time.time() - t0) * 1000)
    if answer:
        return {"ok": True, "reason": "Связь есть", "answer": answer.strip()[:40],
                "provider": provider(), "model": model(), "ms": ms}
    return {"ok": False, "reason": last_error["text"] or "Ответ не получен",
            "provider": provider(), "model": model(), "ms": ms}


def calls(limit: int = 50) -> list:
    with db.tx() as con:
        return db.rows(con, "SELECT * FROM llm_calls ORDER BY id DESC LIMIT ?", int(limit))


# --------------------------------------------------------------------------- #
#  (а) Объяснение расчёта человеческим языком
# --------------------------------------------------------------------------- #

SYSTEM_EXPLAIN = (
    "Ты объясняешь клиенту страховой компании, из чего сложилась ставка по его объекту. "
    "Пиши по-русски, просто, без жаргона, 5–8 коротких предложений. Не придумывай цифры: "
    "используй только те, что даны. Не упоминай людей и документы, которых нет в данных.")


def _explain_data(card: dict) -> dict:
    """Что отдаём в подсказку: только объект и цифры, ничего о человеке."""
    calc = card.get("calculation") or {}
    obj = card.get("object") or {}
    req = card.get("request") or {}
    try:
        expl = json.loads(calc.get("explanation") or "{}")
    except Exception:
        expl = {}
    chain = expl.get("chain") if isinstance(expl, dict) else expl
    try:
        attrs = json.loads(obj.get("attributes") or "{}")
    except Exception:
        attrs = {}
    term = None
    if attrs.get("term_from") and attrs.get("term_to"):
        term = f"с {attrs['term_from']} по {attrs['term_to']}"
    return {
        "продукт": req.get("product_code"),
        "класс": (attrs.get("factors") or {}).get("class_code") or attrs.get("class_code"),
        "тип_объекта": obj.get("object_type"),
        "страховая_сумма": obj.get("sum_insured"),
        "страховая_стоимость": obj.get("value_amount"),
        "франшиза": obj.get("franchise"),
        "срок": term,
        "ставка_техническая_проц": calc.get("gross_rate_pct"),
        "ставка_минимальная_проц": calc.get("min_rate_pct"),
        "ставка_применённая_проц": calc.get("applied_rate_pct"),
        "премия": calc.get("premium"),
        "вердикт": calc.get("verdict"),
        "из_чего_сложилась": chain if isinstance(chain, list) else [],
        "проверки": [{"правило": c.get("rule_code"), "статус": c.get("status"), "пояснение": c.get("detail")}
                     for c in (card.get("checks") or [])],
        "что_можно_изменить": [{"вид": r.get("kind"), "текст": r.get("text"),
                                "изменение_премии": r.get("premium_delta")}
                               for r in (card.get("recommendations") or [])],
    }


def explain_template(data: dict) -> str:
    """Объяснение без ИИ. Работает всегда — это основной вариант, ИИ лишь причёсывает текст."""
    p = []
    money = lambda v: f"{round(v):,}".replace(",", " ") if isinstance(v, (int, float)) else "—"
    pct = lambda v: f"{v:.3f}%".replace(".", ",") if isinstance(v, (int, float)) else "—"
    cls = f", класс страхования {data['класс']}" if data.get("класс") else ""
    p.append(f"Объект: {data.get('тип_объекта') or 'не указан'}{cls}, "
             f"страховая сумма {money(data.get('страховая_сумма'))} сум.")
    p.append(f"Расчётная (техническая) ставка — {pct(data.get('ставка_техническая_проц'))}, "
             f"применённая — {pct(data.get('ставка_применённая_проц'))}, "
             f"премия — {money(data.get('премия'))} сум"
             + (f" за период {data.get('срок')}." if data.get("срок") else "."))
    if data.get("ставка_минимальная_проц"):
        p.append(f"Ниже {pct(data.get('ставка_минимальная_проц'))} опускаться нельзя: это минимум "
                 f"тарифной политики компании или регулятора.")
    chain = data.get("из_чего_сложилась") or []
    if chain:
        parts = []
        for step in chain:
            if "value_pct" in step:
                parts.append(f"{step['name']} — {pct(step['value_pct'])}")
            elif "mult" in step and step.get("mult") not in (1, 1.0, None):
                parts.append(f"{step['name']} — коэффициент {step['mult']}")
            elif "add_pct" in step and step["add_pct"]:
                parts.append(f"{step['name']} — плюс {pct(step['add_pct'])}")
            elif "divide_by" in step:
                parts.append(f"{step['name']}")
        if parts:
            p.append("Из чего сложилась ставка: " + "; ".join(parts) + ".")
    bad = [c for c in (data.get("проверки") or []) if c.get("статус") in ("stop", "warn", "нарушено")]
    if bad:
        p.append("Что требует внимания: " + "; ".join(str(c.get("пояснение") or c.get("правило")) for c in bad) + ".")
    recs = data.get("что_можно_изменить") or []
    if recs:
        p.append("Как можно уменьшить премию: " + "; ".join(str(r.get("текст")) for r in recs) + ".")
    p.append("Вердикт системы: " + str(data.get("вердикт") or "не рассчитан") + ".")
    return " ".join(p)


def explain_calculation(card: dict, class_code: str = None) -> dict:
    """Объяснение расчёта: шаблон всегда, ИИ — если подключён."""
    data = _explain_data(card)
    data["класс"] = data.get("класс") or class_code
    base = explain_template(data)
    text, source = base, "шаблон"
    if enabled():
        answer = chat("объяснение расчёта", SYSTEM_EXPLAIN,
                      "Данные расчёта (JSON):\n" + json.dumps(data, ensure_ascii=False) +
                      "\n\nЧерновик объяснения:\n" + base +
                      "\n\nПерепиши черновик понятным языком для клиента, сохранив все цифры.")
        if answer and answer.strip():
            text, source = answer.strip(), "ИИ"
    return {"text": text, "source": source, "template": base,
            "ai": status() | {"error": last_error["text"] if source == "шаблон" else None}}


SYSTEM_RISK = (
    "Ты андеррайтер страховой компании в Узбекистане. По результатам анализа риска объекта "
    "напиши связный разбор из 5–7 предложений по-русски: уровень риска и что его определяет, "
    "страховая сумма к стоимости, ставка к минимуму и рынку, главные сценарии убытка, каких данных "
    "и документов не хватает. Используй только цифры и факты из данных, ничего не добавляй. "
    "Без списков и заголовков, без упоминания людей.")
AI_OFF = "ИИ не подключён — анализ выполнен по правилам и справочникам"


def risk_summary_data(res: dict) -> dict:
    """Что отдаём модели из ответа /analytics/risk: только объект и цифры, ничего о человеке."""
    s = res.get("summary") or {}
    lvl = res.get("level") or {}
    docs = res.get("documents") or {}
    comp = res.get("completeness") or {}
    return {
        "класс": s.get("class_code"), "продукт": s.get("product_code"), "тип_объекта": s.get("object_type"),
        "регион": s.get("region"), "страховая_сумма": s.get("sum_insured"),
        "стоимость": s.get("object_value"), "сумма_к_стоимости": s.get("ratio_sum_to_value"),
        "ставка_применённая_проц": s.get("rate_applied_pct"), "ставка_минимальная_проц": s.get("rate_min_pct"),
        "премия": s.get("premium"),
        "уровень_риска": lvl.get("level"), "балл": lvl.get("score"),
        "составляющие": [{"что": c.get("name"), "баллы": c.get("points"), "почему": c.get("why")}
                         for c in (lvl.get("components") or []) if c.get("applicable")],
        "главные_факторы": res.get("top_drivers") or [],
        "сценарии": {k: {"что": v.get("title"), "убыток": v.get("amount"), "доля_суммы_проц": v.get("pct_of_sum"),
                         "главная_причина": v.get("dominant"), "уровень": v.get("level")}
                     for k, v in (res.get("scenarios") or {}).items() if isinstance(v, dict)},
        "риски": [{"риск": r.get("name"), "доля_проц": r.get("share_of_net_pct")}
                  for r in (res.get("risks") or []) if isinstance(r, dict)],
        "полнота_данных_проц": comp.get("pct"), "уверенность": comp.get("confidence"),
        "что_добавить": [w.get("key") for w in comp.get("what_to_add") or []],
        "не_хватает_документов": docs.get("missing") or [],
        "рынок": (res.get("market") or {}).get("notes") or [],
    }


def risk_summary(res: dict) -> dict:
    """
    Короткий разбор результатов анализа риска. Без ключа — {"text": None, "status": AI_OFF}:
    шаблонного текста здесь нет, ответ анализа и так содержит все цифры.
    """
    if not enabled():
        return {"text": None, "status": AI_OFF, "source": None}
    data = risk_summary_data(res)
    answer = chat("разбор анализа риска", SYSTEM_RISK,
                  "Результаты анализа (JSON):\n" + json.dumps(data, ensure_ascii=False, default=str),
                  max_tokens=900)
    if answer and answer.strip():
        return {"text": answer.strip(), "status": "Разбор подготовил ИИ по результатам анализа — цифры "
                                                  "сверяйте с блоками ниже", "source": "ИИ"}
    return {"text": None, "status": "ИИ не ответил (" + str(last_error["text"] or "пустой ответ") +
                                     ") — анализ выполнен по правилам и справочникам", "source": None}


# --------------------------------------------------------------------------- #
#  (б) Помощь разбору документов — только по полям, которые не нашли регэкспы
# --------------------------------------------------------------------------- #

SYSTEM_FIELDS = (
    "Ты извлекаешь значения полей из текста официального документа Узбекистана. "
    "Отвечай ТОЛЬКО объектом JSON вида {\"ключ\": \"значение\"}. Если значения в тексте нет — "
    "не добавляй ключ. Ничего не придумывай. Персональные данные уже заменены на метки "
    "вида [ФИО] — их не восстанавливай.")


def extract_fields(text: str, missing: list, doc_kind: str = "") -> dict:
    """
    Ищет в тексте документа поля, которые не нашлись регэкспами.
    missing — список {"key": ..., "name": ...}. Возвращает {key: value} (может быть пусто).
    Любое значение отсюда — «требует проверки»: источник помечается как «ИИ».
    """
    if not enabled() or not text or not missing:
        return {}
    ask = "\n".join(f"- {m.get('key')}: {m.get('name')}" for m in missing)
    answer = chat("поля документа", SYSTEM_FIELDS,
                  f"Вид документа: {doc_kind or 'не указан'}\nНужные поля:\n{ask}\n\nТекст документа:\n{text}",
                  max_tokens=500)
    if not answer:
        return {}
    m = re.search(r"\{.*\}", answer, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except Exception:
        return {}
    wanted = {x.get("key") for x in missing}
    return {k: str(v).strip() for k, v in data.items()
            if k in wanted and v not in (None, "", "нет", "не указано")}


# --------------------------------------------------------------------------- #
#  (б2) Разбор документа целиком — для app/ingest.py
# --------------------------------------------------------------------------- #
# Заказчик (21.09.2026): загруженный файл должен переводиться во внутреннее представление,
# одинаковое для русского, узбекского и английского документа. Поэтому ключи ответа всегда
# английские и всегда одни и те же, а промпт берётся по языку документа из app/llm_prompts/.

PROMPTS_DIR = ROOT / "app" / "llm_prompts"

# Файл промпта по языку документа. uz-latn и uz-cyrl — один файл; mixed и неизвестно — ru.
PROMPT_BY_LANG = {"ru": "ru.txt", "uz-latn": "uz.txt", "uz-cyrl": "uz.txt", "uz": "uz.txt",
                  "en": "en.txt", "mixed": "ru.txt"}

# Разрешённые ключи полей. Один и тот же набор для документа на любом языке.
DOC_FIELD_KEYS = (
    # транспорт
    "brand", "model", "year", "vehicle_type", "vin", "body_no", "chassis_no", "engine_no",
    "engine_cc", "seats", "color", "region",
    # недвижимость
    "cadastral_no", "address", "object_kind", "area_m2", "land_area_ha", "right_kind",
    "cadastral_value", "encumbrance", "mortgage", "rooms", "build_year", "floors", "walls",
    # оценка и деньги
    "market_value", "appraised_value", "book_value", "valuation_date", "currency", "amount",
    "sum_insured",
    # договор
    "contract_no", "contract_date", "period_from", "period_to",
    # выписка и организация
    "account_no", "balance_close", "turnover", "inn_org", "org_name",
    # штатное расписание
    "positions_count", "payroll_fund", "headcount",
)

DOC_FACT_KEYS = ("is_policy", "has_mortgage", "has_encumbrance", "is_register_extract",
                 "construction_unfinished")

DOC_SCHEMA = ('{"document_kind": "строка", "language": "ru|uz-latn|uz-cyrl|en", '
              '"fields": {"ключ": "значение"}, "facts": {"ключ": true|false}, '
              '"summary": "одно предложение о документе"}')


def prompt_file(language: str = None) -> Path:
    """Файл промпта по языку документа. Неизвестный язык — русский промпт."""
    return PROMPTS_DIR / PROMPT_BY_LANG.get((language or "").strip().lower(), "ru.txt")


def load_prompt(language: str = None) -> Optional[str]:
    """Читает промпт с диска. Файла нет — None, вызывающий код обязан это пережить."""
    try:
        return prompt_file(language).read_text(encoding="utf-8")
    except Exception as e:
        last_error["text"] = "Промпт не прочитан: %s" % type(e).__name__
        return None


SYSTEM_DOCUMENT = ("Ты извлекаешь данные из официальных документов Узбекистана. "
                   "Отвечаешь только объектом JSON с английскими ключами. "
                   "Ничего не придумываешь. Персональные данные физических лиц не извлекаешь.")


def extract_document(text: str, kind: str = "", language: str = None) -> dict:
    """
    Разбор документа ИИ: один и тот же JSON для русского, узбекского и английского документа.

    Возвращает {"ok", "source", "fields", "facts", "document_kind", "language", "summary",
                "требует_проверки", "reason"}.
    Без ключа ИИ (enabled() == False) честно отдаёт ok=False и пустые поля: вызывающий код
    (app/ingest.py) в этом случае остаётся на регулярках и помечает method = "regex".
    Маскировка персональных данных делается внутри chat() — обойти её нельзя.
    """
    empty = {"ok": False, "source": "нет", "fields": {}, "facts": {}, "document_kind": None,
             "language": language, "summary": None, "требует_проверки": False}
    if not enabled():
        return empty | {"reason": status()["reason"]}
    if not (text or "").strip():
        return empty | {"reason": "пустой текст документа"}
    template = load_prompt(language)
    if not template:
        return empty | {"reason": "нет файла промпта app/llm_prompts/"}
    user = (template
            .replace("{kind}", kind or "не определён")
            .replace("{language}", language or "не определён")
            .replace("{schema}", DOC_SCHEMA)
            .replace("{keys}", ", ".join(DOC_FIELD_KEYS))
            .replace("{text}", text))
    answer = chat("разбор документа", SYSTEM_DOCUMENT, user, max_tokens=1200)
    if not answer:
        return empty | {"reason": last_error["text"] or "ответ не получен"}
    m = re.search(r"\{.*\}", answer, re.S)
    if not m:
        return empty | {"reason": "модель ответила не JSON"}
    try:
        data = json.loads(m.group(0))
    except Exception:
        return empty | {"reason": "ответ модели не разобрался как JSON"}
    raw_fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    fields = {k: str(v).strip() for k, v in raw_fields.items()
              if k in DOC_FIELD_KEYS and v not in (None, "", "нет", "не указано", "-")}
    raw_facts = data.get("facts") if isinstance(data.get("facts"), dict) else {}
    facts = {k: bool(v) for k, v in raw_facts.items() if k in DOC_FACT_KEYS}
    return {"ok": True, "source": "ИИ", "fields": fields, "facts": facts,
            "document_kind": (data.get("document_kind") or None),
            "language": data.get("language") or language,
            "summary": (str(data.get("summary"))[:500] if data.get("summary") else None),
            "требует_проверки": True,
            "reason": "Значения предложены ИИ — проверьте по оригиналу документа"}


# --------------------------------------------------------------------------- #
#  (в) Разбор новостей и изменений законов — только интерфейс
# --------------------------------------------------------------------------- #

def analyze_news(items: list, purpose: str = "изменения законодательства") -> dict:
    """
    ЗАГОТОВКА (реализация — в другом потоке работ).

    Назначение: по списку новостей и опубликованных актов понять, что меняется для страховщика
    (ставки, обязательные виды, требования к агентам), и предложить темы для разбора.

    Вход:  items — список {"title": ..., "date": ..., "url": ..., "text": ...}.
           Источник нормы — только lex.uz (см. tools/lex_fetch.py); новости — справочно.
    Выход: {"ok": bool, "source": "ИИ"|"нет", "items": [...], "reason": "..."}
           где для каждой новости: {"title", "url", "важность", "что_меняется", "норма"}.

    Сейчас ИИ-разбора нет: функция возвращает список без оценки, чтобы вызывающий код
    (реестр знаний app/knowledge.py) продолжал работать как раньше.
    """
    plain = [{"title": (i.get("title") or "")[:300], "url": i.get("url"), "date": i.get("date"),
              "важность": None, "что_меняется": None, "норма": None} for i in (items or [])]
    return {"ok": False, "source": "нет", "items": plain,
            "reason": "Разбор новостей ИИ ещё не реализован — показан исходный список"}


# --------------------------------------------------------------------------- #
#  (г) Публичные функции ИИ-сюрвейера: диалог, перевод, разбор файлов, разбор риска
#      Системные промпты — в app/llm_prompts/system.json (правит юрист, код не трогаем).
# --------------------------------------------------------------------------- #

SYSTEM_FILE = PROMPTS_DIR / "system.json"
_sys_cache: dict = {}

# запасные формулировки на случай, если файл промптов испорчен или удалён
SYSTEM_FALLBACK = {
    "base": ("Ты ИИ-сюрвейер страховой организации Узбекистана. Не сочиняй нормы права. "
             "Франшиза и условия — по правилам страхования компании. Ставка не ниже минимума "
             "тарифной политики. Не придумывай цифры: только переданные данные."),
    "chat": "Отвечай коротко и по делу.",
    "translate": "Переведи текст, сохранив числа, суммы, даты и ссылки без изменений.",
    "extract": "Отвечай только объектом JSON по заданной схеме. Ничего не придумывай.",
    "narrative": "Напиши 5–8 предложений по результатам анализа, используя только данные из JSON.",
}


def system_prompt(name: str) -> str:
    """Системный промпт: общие правила + блок под задачу. Файл читается один раз на процесс."""
    data = _sys_cache.get("data")
    if data is None:
        try:
            data = json.loads(SYSTEM_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        _sys_cache["data"] = data
    base = data.get("base") or SYSTEM_FALLBACK["base"]
    part = data.get(name) or SYSTEM_FALLBACK.get(name, "")
    return (base + "\n" + part).strip()


def reload_prompts() -> None:
    """Сбросить кэш промптов — после правки system.json без перезапуска сервера."""
    _sys_cache.clear()


LANG_NAME = {"ru": "русском", "uz": "узбекском (латиница)", "en": "английском"}


def chat_dialog(messages: list, lang: str = "ru", max_tokens: int = 600,
                files: Optional[list] = None) -> dict:
    """
    Свободный диалог. messages — [{"role": "user"|"assistant", "content": "..."}].
    lang — язык ответа (ru/uz/en). Возвращает {"text", "ok", "notes", "reason", "source"}.
    Без ключа ИИ text = None: вызывающий код обязан иметь запасной ответ по правилам.
    Имя chat() занято низкоуровневым вызовом, поэтому публичная функция диалога — chat_dialog().
    """
    lang = lang if lang in LANG_NAME else "ru"
    system = system_prompt("chat") + f"\nОтвечай на {LANG_NAME[lang]} языке."
    msgs = [{"role": "system", "content": system}]
    for m in (messages or [])[-12:]:            # длинную историю не тащим: дороже и бесполезно
        msgs.append({"role": m.get("role") or "user", "content": str(m.get("content") or "")})
    res = chat_raw("диалог сюрвейера", msgs, max_tokens=max_tokens, files=files)
    return res | {"source": "ИИ" if res["ok"] else None}


# кэш переводов: ключ — хэш (текст + язык + модель), значение — перевод.
_tr_cache: dict = {}
TR_CACHE_MAX = 2000


def _tr_key(text: str, to_lang: str) -> str:
    return hashlib.sha256(f"{to_lang}|{model()}|{text}".encode("utf-8")).hexdigest()


def translate(text: str, to_lang: str) -> dict:
    """
    Перевод свободного текста на uz/en (и обратно на ru). Числа, суммы, даты и ссылки
    сохраняются — это прямо записано в промпте translate.
    Возвращает {"text", "lang", "source": "ИИ"|"кэш"|"исходный", "reason"}.
    Без ИИ отдаётся исходный текст: интерфейс не должен оставаться пустым.
    """
    text = (text or "").strip()
    to_lang = to_lang if to_lang in LANG_NAME else "ru"
    if not text:
        return {"text": "", "lang": to_lang, "source": "исходный", "reason": "пустой текст"}
    key = _tr_key(text, to_lang)
    if key in _tr_cache:
        return {"text": _tr_cache[key], "lang": to_lang, "source": "кэш", "reason": None}
    if not enabled():
        return {"text": text, "lang": to_lang, "source": "исходный", "reason": status()["reason"]}
    answer = chat("перевод текста", system_prompt("translate"),
                  f"Язык перевода: {LANG_NAME[to_lang]}.\nТекст:\n{text}",
                  # лимит вывода отдельно от «размышления»: перевод бывает длиннее исходника
                  max_tokens=min(4096, 2 * len(text) + 256))
    if not answer or not answer.strip():
        return {"text": text, "lang": to_lang, "source": "исходный",
                "reason": last_error["text"] or "ответ не получен"}
    out = answer.strip()
    if len(_tr_cache) > TR_CACHE_MAX:
        _tr_cache.clear()
    _tr_cache[key] = out
    return {"text": out, "lang": to_lang, "source": "ИИ", "reason": None}


def translate_cache_size() -> int:
    return len(_tr_cache)


# ---------- разбор пачки файлов ----------

# Что ждём от модели. Ключи английские и одинаковые для документа на любом языке
# (тот же принцип, что и у extract_document выше).
FILES_SCHEMA = (
    '{'
    '"documents": [{"file": "имя файла", "document_kind": "техпаспорт|кадастр|договор страхования|'
    'отчёт оценщика|фото объекта|иное", "language": "ru|uz-latn|uz-cyrl|en", '
    '"fields": {"ключ": "значение"}}], '
    '"object": {"construction": "reinforced|mixed|wood", "floors": число, '
    '"condition": "хорошее|удовлетворительное|плохое", "protection": "описание защиты", '
    '"object_kind": "строка", "wear_pct": число}, '
    '"pdf_contains": {"заявление": true|false, "паспорт объекта": true|false, '
    '"фото 4 сторон": true|false, "кадастр": true|false, "техпаспорт": true|false, '
    '"отчёт оценщика": true|false, "договор страхования": true|false}, '
    '"notes": ["короткое замечание"]'
    '}')

FILES_OBJECT_KEYS = ("construction", "floors", "condition", "protection", "object_kind", "wear_pct")
TEXT_FORMATS = {"xlsx", "docx"}              # эти файлы модели не показываем — отдаём текст


def _text_from_file(f: dict) -> str:
    """Текст Excel/Word через app/ingest (ленивый импорт: ingest сам импортирует llm)."""
    path = f.get("path")
    if not path:
        return ""
    try:
        from . import ingest                  # noqa: PLC0415 — круговой импорт разрывается здесь
        read = ingest.read_file(Path(path), f.get("mime"))
        return (read.get("text") or "")[:MAX_PROMPT_CHARS]
    except Exception:
        return ""


def extract_from_files(files: list, product_hint: str = "", checklist: Optional[list] = None) -> dict:
    """
    Разбор пачки загруженных файлов одним обращением к модели.

    files — [{"name", "mime", "data"|"path", "format"}]. Изображения (JPEG/PNG) и PDF уходят
    вложениями inline_data, Excel и Word — извлечённым текстом (app/ingest.read_file).
    checklist — названия пунктов «что должно быть в PDF» по продукту (источник — /analytics/risk/docs).

    Возвращает {"ok", "source", "documents", "object", "pdf_contains", "notes", "reason"}.
    Значения отсюда всегда «требуют проверки»: источник помечается как «ИИ».
    """
    empty = {"ok": False, "source": "нет", "documents": [], "object": {}, "pdf_contains": {},
             "notes": [], "требует_проверки": False}
    if not files:
        return empty | {"reason": "файлов нет"}
    if not enabled():
        return empty | {"reason": status()["reason"]}
    inline, texts, notes = [], [], []
    for f in files:
        fmt = (f.get("format") or "").lower()
        mime = (f.get("mime") or "").lower()
        if fmt in TEXT_FORMATS or "spreadsheet" in mime or "wordprocessing" in mime:
            text = _text_from_file(f)
            if text:
                texts.append(f"=== {f.get('name') or 'файл'} ===\n{text}")
            else:
                notes.append(f"{f.get('name') or 'файл'}: текст не извлечён")
            continue
        inline.append(f)
    if not inline and not texts:
        return empty | {"reason": "ни один файл не пригоден для разбора", "notes": notes}
    ask = ["Разбери приложенные файлы объекта страхования.",
           "Схема ответа (только JSON, без пояснений):", FILES_SCHEMA]
    if product_hint:
        ask.append(f"Продукт: {product_hint}.")
    if checklist:
        ask.append("Проверь по списку, что в файлах есть, а чего нет "
                   "(ключи pdf_contains — ровно эти названия): " + "; ".join(str(c) for c in checklist))
    if texts:
        ask.append("Текст из файлов Excel/Word:\n" + "\n".join(texts))
    res = chat_raw("разбор файлов", [{"role": "system", "content": system_prompt("extract")},
                                     {"role": "user", "content": "\n\n".join(ask)}],
                   max_tokens=2048, files=inline)
    notes += res.get("notes") or []
    if not res["text"]:
        return empty | {"reason": res.get("reason") or "ответ не получен", "notes": notes}
    m = re.search(r"\{.*\}", res["text"], re.S)
    if not m:
        return empty | {"reason": "модель ответила не JSON", "notes": notes}
    try:
        data = json.loads(m.group(0))
    except Exception:
        return empty | {"reason": "ответ модели не разобрался как JSON", "notes": notes}
    docs = []
    for d in (data.get("documents") or [])[:20]:
        if not isinstance(d, dict):
            continue
        raw = d.get("fields") if isinstance(d.get("fields"), dict) else {}
        docs.append({"file": str(d.get("file") or "")[:200],
                     "document_kind": (str(d.get("document_kind"))[:80] if d.get("document_kind") else None),
                     "language": (str(d.get("language"))[:10] if d.get("language") else None),
                     "fields": {k: str(v).strip()[:300] for k, v in raw.items()
                                if k in DOC_FIELD_KEYS and v not in (None, "", "-", "нет", "не указано")}})
    raw_obj = data.get("object") if isinstance(data.get("object"), dict) else {}
    obj = {k: raw_obj[k] for k in FILES_OBJECT_KEYS
           if raw_obj.get(k) not in (None, "", "-", "нет", "не указано")}
    raw_pdf = data.get("pdf_contains") if isinstance(data.get("pdf_contains"), dict) else {}
    pdf = {str(k)[:120]: bool(v) for k, v in raw_pdf.items()}
    notes += [str(n)[:300] for n in (data.get("notes") or [])[:10] if n]
    return {"ok": True, "source": "ИИ", "documents": docs, "object": obj, "pdf_contains": pdf,
            "notes": notes, "требует_проверки": True,
            "reason": "Значения предложены ИИ по файлам — проверьте по оригиналам"}


# ---------- разбор результатов анализа текстом ----------

def risk_narrative(analysis_json: dict, lang: str = "ru", franchise: Optional[dict] = None) -> dict:
    """
    5–8 предложений по результатам анализа: уровень риска, сумма к стоимости, ставка к минимуму,
    совет по франшизе, чего не хватает. Все цифры — только из analysis_json (и franchise).
    Без ИИ отдаётся шаблон: анализ и так содержит все числа, интерфейс не остаётся пустым.
    """
    lang = lang if lang in LANG_NAME else "ru"
    data = risk_summary_data(analysis_json or {})
    if franchise:
        data["рекомендация_по_франшизе"] = franchise
    base = explain_template({
        "тип_объекта": data.get("тип_объекта"), "класс": data.get("класс"),
        "страховая_сумма": data.get("страховая_сумма"),
        "ставка_техническая_проц": data.get("ставка_применённая_проц"),
        "ставка_применённая_проц": data.get("ставка_применённая_проц"),
        "ставка_минимальная_проц": data.get("ставка_минимальная_проц"),
        "премия": data.get("премия"),
        "вердикт": data.get("уровень_риска"),
    })
    if not enabled():
        return {"text": base, "source": "шаблон", "template": base, "reason": status()["reason"]}
    answer = chat("разбор риска текстом", system_prompt("narrative") +
                  f"\nЯзык ответа — {LANG_NAME[lang]}.",
                  "Результаты анализа (JSON):\n" +
                  json.dumps(data, ensure_ascii=False, default=str) +
                  "\n\nЧерновик по шаблону:\n" + base +
                  "\n\nНапиши 5–8 предложений, сохранив все цифры без изменений.",
                  max_tokens=1024)
    if answer and answer.strip():
        return {"text": answer.strip(), "source": "ИИ", "template": base, "reason": None}
    return {"text": base, "source": "шаблон", "template": base,
            "reason": last_error["text"] or "ответ не получен"}


# --------------------------------------------------------------------------- #
#  API
# --------------------------------------------------------------------------- #

class MaskIn(BaseModel):
    text: str


@router.get("/llm/status")
def llm_status():
    """Подключён ли ИИ, какой провайдер и модель. Ключ — только маской."""
    return status()


@router.post("/llm/ping")
def llm_ping():
    return ping()


@router.get("/llm/calls")
def llm_calls(limit: int = 50):
    return calls(limit)


@router.post("/llm/mask-preview")
def llm_mask_preview(body: MaskIn):
    """Показать, как выглядит текст после маскировки — чтобы проверить правило № 8 своими глазами."""
    return {"masked": mask_pd(body.text)}


@router.get("/requests/{rid}/explain")
def request_explain(rid: int):
    """Объяснение расчёта для клиента. Без ИИ отдаётся тот же текст, собранный по шаблону."""
    from .main import _card                     # карточка запроса собирается в main.py
    with db.tx() as con:
        card = _card(con, rid)
        cls = db.rows(con, "SELECT class_code FROM product_classes WHERE product_code=? ORDER BY part_no",
                      (card.get("request") or {}).get("product_code"))
    if not card.get("calculation"):
        raise HTTPException(404, "По этому запросу расчёта ещё нет")
    return explain_calculation(card, ", ".join(c["class_code"] for c in cls) or None)
