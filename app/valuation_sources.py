"""
Адаптеры источников объявлений для оценки рыночной стоимости объекта страхования.

Назначение: собрать открытые объявления по похожим объектам (авто, недвижимость),
чтобы у андеррайтера была рыночная справка о стоимости на дату заключения договора.

Жёсткие правила модуля (проверяются кодом, не только на словах):
  * только стандартная библиотека Python (urllib, json, re, html, statistics, gzip);
  * robots.txt каждого домена читается и соблюдается (urllib.robotparser);
    если robots.txt недоступен (403/блокировка) — источник считается закрытым;
  * пауза не менее 2 секунд между обращениями к одному домену;
  * честный User-Agent с указанием, кто мы и как с нами связаться;
  * никакого обхода защит, капч, приватных API и скрытых ключей: если сайт не
    отдаёт данные — адаптер возвращает status «источник недоступен» с причиной.
    Это нормальный результат, а не ошибка;
  * ничего не выдумывается: если объявления нет — его нет, пустая выборка так и
    показывается (ads_count = 0).

Фактическое состояние источников на 20.09.2026 (проверено обращением к сайтам):
  * avtoelon.uz  — РАБОТАЕТ. robots.txt открыт, выдача отдаётся готовым HTML,
                   рядом с каждой карточкой лежит JSON (listing.items.push)
                   с ценой, городом, маркой/моделью и датой последнего обновления.
  * olx.uz       — ЗАКРЫТ. CloudFront отдаёт 403 даже на /robots.txt.
                   Читать robots нельзя → считаем источник запрещённым.
  * uybor.uz     — robots.txt открыт (запрещены только /admin/, /cgi-bin/, /tmp/),
                   но страница выдачи — Next.js без серверной отрисовки:
                   в HTML нет ни объявлений, ни цен, ни дат.
  * joymee.uz    — robots.txt открыт (выдача разрешена), но список объявлений
                   тоже рисуется в браузере. На карточке отдельного объявления
                   есть schema.org с ценой, но НЕТ даты публикации, поэтому
                   фильтр «6 месяцев» выполнить невозможно.

Валюта: цены встречаются в сумах и в у.е. (долларах). Пересчёт в сумы делается
ТОЛЬКО по курсу, который передан снаружи параметром `rate`. Курса в данных проекта
нет — вопрос заказчику задан (см. task 39). Пока курс не согласован, долларовые
цены остаются без пересчёта и попадают в prices_usd, а не в prices.
"""

from __future__ import annotations

import gzip
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

# --------------------------------------------------------------------------- #
# Общие настройки
# --------------------------------------------------------------------------- #

CONTACT_EMAIL = "j.mirzaumarov@insuranceon.uz"
# HTTP-заголовки передаются только латиницей (latin-1), поэтому User-Agent — по-английски.
USER_AGENT = (
    "INSON-Surveyor/1.0 (AI surveyor bot of INSON insurance company, Uzbekistan; "
    "market value check for insurance underwriting; contact: %s)" % CONTACT_EMAIL
)

MIN_DELAY_SEC = 2.0          # пауза между обращениями к одному домену
WINDOW_MONTHS = 6            # окно свежести объявлений
HTTP_TIMEOUT = 40

STATUS_OK = "ok"
STATUS_EMPTY = "нет объявлений"
STATUS_DOWN = "источник недоступен"
STATUS_ROBOTS = "запрещено robots.txt"

_last_request: Dict[str, float] = {}
_robots_cache: Dict[str, Any] = {}          # домен → (правила или None, причина, когда прочитано)
_robots_text: Dict[str, str] = {}           # домен → текст robots.txt (для проверки шаблонов «*» по RFC 9309)
ROBOTS_FAIL_TTL_SEC = 600                    # сбой чтения robots.txt не запоминаем навсегда: повтор через 10 минут

_SSL_CTX = ssl.create_default_context()


# --------------------------------------------------------------------------- #
# Результат адаптера
# --------------------------------------------------------------------------- #

@dataclass
class Sample:
    """Пример объявления для показа андеррайтеру (без персональных данных)."""
    title: str
    price: Optional[float]          # цена как на сайте
    currency: str                   # "UZS" | "USD"
    price_uzs: Optional[float]      # пересчёт в сумы (None, если курс не задан)
    published: Optional[str]        # ISO-дата публикации/обновления
    url: str = ""


@dataclass
class SourceResult:
    source: str
    status: str
    reason: str = ""
    url: str = ""
    ads_count: int = 0
    prices: List[float] = field(default_factory=list)       # в сумах
    prices_usd: List[float] = field(default_factory=list)   # не пересчитанные, если курса нет
    median: Optional[float] = None
    q1: Optional[float] = None
    q3: Optional[float] = None
    currency: str = "UZS"
    rate: Optional[float] = None
    samples: List[Sample] = field(default_factory=list)
    fetched_at: str = ""
    limitations: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# --------------------------------------------------------------------------- #
# Сеть: robots.txt, задержка, загрузка
# --------------------------------------------------------------------------- #

def _domain(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc.lower()


def _throttle(domain: str) -> None:
    """Не чаще одного запроса в MIN_DELAY_SEC к одному домену."""
    last = _last_request.get(domain)
    now = time.monotonic()
    if last is not None:
        wait = MIN_DELAY_SEC - (now - last)
        if wait > 0:
            time.sleep(wait)
    _last_request[domain] = time.monotonic()


def _http_get(url: str, timeout: int = HTTP_TIMEOUT) -> str:
    """GET с честным User-Agent и соблюдением паузы. Возвращает текст."""
    _throttle(_domain(url))
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,uz;q=0.8",
            "Accept-Encoding": "gzip",
            "From": CONTACT_EMAIL,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", "replace")


def robots_check(url: str) -> tuple:
    """
    (allowed: bool, reason: str). Читает robots.txt домена своим User-Agent.
    Если robots.txt не отдаётся (403 и т.п.) — считаем, что доступ закрыт:
    не читая правил, лезть на сайт нельзя.
    """
    dom = _domain(url)
    scheme = urllib.parse.urlsplit(url).scheme or "https"
    got = _robots_cache.get(dom)
    # прочитанные правила помним до перезапуска; сбой чтения — только ROBOTS_FAIL_TTL_SEC, потом спрашиваем снова
    if got is not None and (got[0] is not None or time.monotonic() - got[2] < ROBOTS_FAIL_TTL_SEC):
        rp, reason = got[0], got[1]
        text = _robots_text.get(dom, "")
    else:
        robots_url = "%s://%s/robots.txt" % (scheme, dom)
        rp, reason, text = None, "", ""
        try:
            text = _http_get(robots_url, timeout=25)
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(text.splitlines())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                reason = "robots.txt не отдаётся (HTTP %d) — доступ к сайту закрыт защитой" % e.code
            elif e.code == 404:
                rp = urllib.robotparser.RobotFileParser()
                rp.parse([])            # нет robots.txt → ограничений нет
            else:
                reason = "robots.txt недоступен (HTTP %d)" % e.code
        except Exception as e:                       # сеть, таймаут, TLS
            reason = "robots.txt недоступен (%s)" % type(e).__name__
        _robots_cache[dom] = (rp, reason, time.monotonic())
        _robots_text[dom] = text or ""
    if rp is None:
        return False, reason or "robots.txt недоступен"
    return robots_allowed_text(text or "", url, rp)


def _rfc_rules(text: str, agent: str) -> List[tuple]:
    """Правила (allow|disallow, путь) групп, подходящих агенту; своих групп нет — групп «*»."""
    groups, cur, in_agents = [], None, False
    for raw in (text or "").splitlines():
        line = raw.replace("﻿", "").split("#", 1)[0].strip()
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip().lower(), v.strip()
        if k == "user-agent":
            if not in_agents:
                cur = {"agents": [], "rules": []}
                groups.append(cur)
                in_agents = True
            cur["agents"].append(v.lower())
        elif k in ("allow", "disallow"):
            in_agents = False
            if cur is not None:
                cur["rules"].append((k, v))
    own = [g for g in groups if any(a not in ("", "*") and a in agent.lower() for a in g["agents"])]
    use = own or [g for g in groups if "*" in g["agents"]]
    return [r for g in use for r in g["rules"]]


def rfc_allowed(text: str, url: str, agent: str = USER_AGENT) -> bool:
    """
    RFC 9309: шаблоны со «*» и «$», самое длинное совпадение побеждает, при равной длине — allow.
    urllib.robotparser шаблонов «*» не понимает (сравнивает начало пути), поэтому у avtoelon.uz правило
    «Disallow: /*?*» он молча пропускает; эта проверка закрывает разрыв.
    """
    sp = urllib.parse.urlsplit(url)
    path = (sp.path or "/") + ("?" + sp.query if sp.query else "")
    lp = urllib.parse.unquote(path).lower()
    best, verdict = -1, True
    for kind, rule in _rfc_rules(text, agent):
        if not rule:
            continue
        pat = re.escape(rule.lower()).replace(r"\*", ".*")
        if pat.endswith(r"\$"):
            pat = pat[:-2] + "$"
        if re.match(pat, lp):
            n = len(rule)
            if n > best or (n == best and kind == "allow"):
                best, verdict = n, kind == "allow"
    return verdict


def robots_allowed_text(text: str, url: str, rp=None) -> tuple:
    """(разрешено, причина) по тексту robots.txt: разрешено, только если согласны urllib.robotparser и RFC 9309."""
    if rp is None:
        rp = urllib.robotparser.RobotFileParser()
        rp.parse((text or "").splitlines())
    a = rp.can_fetch(USER_AGENT, url) or rp.can_fetch("*", url)
    b = rfc_allowed(text or "", url)
    if a and b:
        return True, ""
    return False, "robots.txt домена %s запрещает загрузку %s" % (_domain(url), url)


# --------------------------------------------------------------------------- #
# Цены и валюта
# --------------------------------------------------------------------------- #

_SPACES = "\u00a0\u202f\u2009\u2007 "
_USD_MARKS = ("y.e", "у.е", "у.e", "y.у", "$", "usd", "доллар")
_UZS_MARKS = ("сум", "so'm", "so‘m", "som", "uzs", "сўм")


def parse_price(text: str) -> tuple:
    """
    '~13 954 y.e.' -> (13954.0, 'USD');  '850 000 000 сум' -> (850000000.0, 'UZS').
    Возвращает (None, '') если числа нет.
    """
    if not text:
        return None, ""
    t = text
    for sp in _SPACES:
        t = t.replace(sp, " ")
    t = t.replace("&nbsp;", " ").strip()
    low = t.lower()
    currency = ""
    if any(m in low for m in _USD_MARKS):
        currency = "USD"
    elif any(m in low for m in _UZS_MARKS):
        currency = "UZS"
    m = re.search(r"\d[\d ]*(?:[.,]\d+)?", t)
    if not m:
        return None, currency
    num = m.group(0).replace(" ", "").replace(",", ".")
    try:
        value = float(num)
    except ValueError:
        return None, currency
    return value, currency


def to_uzs(amount: Optional[float], currency: str, rate: Optional[float]) -> Optional[float]:
    """
    Пересчёт в сумы. rate — сумов за 1 доллар (курс ЦБ РУз на дату заключения).
    Без курса долларовая цена НЕ пересчитывается (возвращается None) — выдумывать курс нельзя.
    """
    if amount is None:
        return None
    if currency == "UZS":
        return float(amount)
    if currency == "USD":
        if not rate:
            return None
        return float(amount) * float(rate)
    return None


# --------------------------------------------------------------------------- #
# Даты и статистика
# --------------------------------------------------------------------------- #

def window_start(as_of: date, months: int = WINDOW_MONTHS) -> date:
    """Начало окна: as_of минус `months` месяцев (по календарю)."""
    y, m = as_of.year, as_of.month - months
    while m <= 0:
        m += 12
        y -= 1
    day = as_of.day
    while True:
        try:
            return date(y, m, day)
        except ValueError:
            day -= 1


def in_window(published: Optional[date], as_of: date, months: int = WINDOW_MONTHS) -> bool:
    """Объявление берём, только если дата публикации известна и попадает в окно."""
    if published is None:
        return False
    return window_start(as_of, months) <= published <= as_of


def parse_iso_date(value: str) -> Optional[date]:
    """'2026-09-20T12:19:00+05:00' | '2026-09-20' -> date. Иначе None."""
    if not value:
        return None
    v = value.strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", v)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def quantiles(values: List[float]) -> tuple:
    """(q1, median, q3) методом линейной интерполяции. Пустой список -> (None, None, None)."""
    xs = sorted(float(v) for v in values if v is not None)
    n = len(xs)
    if n == 0:
        return None, None, None
    if n == 1:
        return xs[0], xs[0], xs[0]

    def q(p: float) -> float:
        pos = (n - 1) * p
        lo = int(pos)
        hi = min(lo + 1, n - 1)
        frac = pos - lo
        return xs[lo] + (xs[hi] - xs[lo]) * frac

    return q(0.25), q(0.5), q(0.75)


def _finish(res: SourceResult, rows: List[Sample], rate: Optional[float]) -> SourceResult:
    """Считает статистику по сумовым ценам и заполняет результат."""
    res.samples = rows   # ссылка сохраняется на каждое объявление, вошедшее в расчёт
    res.ads_count = len(rows)
    res.prices = [r.price_uzs for r in rows if r.price_uzs is not None]
    res.prices_usd = [r.price for r in rows if r.currency == "USD" and r.price is not None]
    res.rate = rate
    base = res.prices if res.prices else []
    res.q1, res.median, res.q3 = quantiles(base)
    if not rows:
        res.status = STATUS_EMPTY
    elif not res.prices:
        res.status = STATUS_OK
        res.limitations.append(
            "Цены в объявлениях указаны в у.е.; курс сум/доллар не задан — "
            "пересчёт в сумы не выполнен, медиана и квартили не рассчитаны."
        )
    else:
        res.status = STATUS_OK
    return res


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _new(source: str, url: str = "") -> SourceResult:
    return SourceResult(source=source, status=STATUS_DOWN, url=url, fetched_at=_now())


# --------------------------------------------------------------------------- #
# Справочники слагов
# --------------------------------------------------------------------------- #

AVTOELON_CITIES = {
    "ташкент": "gorod-tashkent",
    "ташкентская область": "tashkentskiy-rayon",
    "самарканд": "gorod-samarkand",
    "бухара": "gorod-buhara",
    "фергана": "gorod-fergana",
    "андижан": "gorod-andizhan",
    "наманган": "gorod-namangan",
    "нукус": "gorod-nukus",
    "карши": "gorod-karshi",
    "термез": "gorod-termez",
    "навои": "gorod-navoi",
    "джизак": "gorod-dzhizak",
    "гулистан": "gorod-gulistan",
    "ургенч": "gorod-urgench",
}


def _slug(text: str) -> str:
    """Латинское название марки/модели в слаг сайта: 'Chevrolet Cobalt' -> 'chevrolet-cobalt'."""
    t = (text or "").strip().lower()
    t = re.sub(r"[^a-z0-9]+", "-", t)
    return t.strip("-")


# --------------------------------------------------------------------------- #
# Адаптер 1. avtoelon.uz — авто (единственный реально работающий источник)
# --------------------------------------------------------------------------- #

def fetch_avtoelon(object_type: str, params: dict, as_of: date,
                   rate: Optional[float] = None, max_pages: int = 1) -> SourceResult:
    """
    Авто. params: марка (brand), модель (model), год (year), пробег (mileage), регион (region).

    Что реально отдаёт сайт: страница выдачи — готовый HTML, к каждой карточке
    приложен JSON `listing.items.push({...})` с полями unitPrice, lastUpdate,
    url, attributes.brand/model, city.

    Ограничения (обязательны к показу андеррайтеру):
      * lastUpdate — дата последнего обновления объявления, а не дата первой
        публикации: даты публикации выдача не отдаёт;
      * пробег в выдаче не публикуется — фильтр по пробегу применить нельзя;
      * цена по умолчанию в у.е. (доллары) — нужен курс для пересчёта в сумы.
    """
    res = _new("avtoelon.uz")
    if object_type != "авто":
        res.status = STATUS_DOWN
        res.reason = "avtoelon.uz — источник только по автотранспорту"
        return res

    brand = _slug(params.get("марка") or params.get("brand") or "")
    model = _slug(params.get("модель") or params.get("model") or "")
    region = (params.get("регион") or params.get("region") or "").strip().lower()
    year = params.get("год") or params.get("year")

    if not brand:
        res.reason = "не указана марка — запрос к выдаче построить нельзя"
        return res

    parts = ["https://avtoelon.uz/avto", brand]
    if model:
        parts.append(model)
    city = AVTOELON_CITIES.get(region)
    if region and not city:
        res.limitations.append("Регион «%s» не найден в справочнике слагов сайта — "
                               "поиск выполнен без фильтра по региону." % region)
    if city:
        parts.append(city)
    base_url = "/".join(parts) + "/"
    res.url = base_url

    rows: List[Sample] = []
    try:
        for page in range(1, max(1, max_pages) + 1):
            url = base_url if page == 1 else base_url + "?page=%d" % page
            allowed, why = robots_check(url)
            if not allowed:
                res.status = STATUS_ROBOTS
                res.reason = why
                return res
            html = _http_get(url)
            rows.extend(_parse_avtoelon(html, as_of, rate, params, res))
            if "list-item" not in html:
                break
    except urllib.error.HTTPError as e:
        res.reason = "сайт ответил HTTP %d на %s" % (e.code, res.url)
        return res
    except Exception as e:
        res.reason = "не удалось загрузить выдачу (%s)" % type(e).__name__
        return res

    res.limitations.append(
        "Дата у объявления — дата последнего обновления (lastUpdate); "
        "дату первой публикации выдача avtoelon.uz не отдаёт."
    )
    res.limitations.append("Пробег в выдаче не публикуется — отбор по пробегу не выполнялся.")
    if year:
        res.limitations.append("Отбор по году выпуска сделан по значению из карточки объявления.")
    _finish(res, rows, rate)
    if res.status == STATUS_EMPTY:
        res.reason = ("объявлений, подходящих по параметрам и опубликованных не раньше %s, "
                      "не найдено" % window_start(as_of).isoformat())
    return res


_AE_ITEM = re.compile(r"listing\.items\.push\((\{.*?\})\);", re.S)
_AE_CARD = re.compile(
    r'data-id="(?P<id>\d+)".*?js__advert-link">(?P<title>[^<]*)</a>'
    r'.*?<span class="price">(?P<price>.*?)</span>'
    r'(?:.*?<span class="year">\s*(?P<year>\d{4}))?',
    re.S,
)


def _parse_avtoelon(html: str, as_of: date, rate: Optional[float],
                    params: dict, res: SourceResult) -> List[Sample]:
    """Разбор выдачи: JSON рядом с карточкой + заголовок/год из HTML карточки."""
    cards: Dict[str, dict] = {}
    for m in _AE_CARD.finditer(html):
        cards[m.group("id")] = {
            "title": re.sub(r"\s+", " ", m.group("title") or "").strip(),
            "price_text": re.sub(r"<[^>]+>", " ", m.group("price") or ""),
            "year": int(m.group("year")) if m.group("year") else None,
        }
    # разбор по блоку карточки (13.10.2026): регулярка выше на живой выдаче цепляла чужой data-id, и год первой
    # карточки терялся; блок от data-id до следующей карточки надёжнее — из него же берётся пробег
    for ad_id, c in parse_avtoelon_cards(html).items():
        cards[ad_id] = {**cards.get(ad_id, {}), **{k: v for k, v in c.items() if v not in (None, "")}}

    want_year = params.get("год") or params.get("year")
    try:
        want_year = int(want_year) if want_year else None
    except (TypeError, ValueError):
        want_year = None
    tol = int(params.get("допуск_по_году") or params.get("year_tolerance") or 1)

    no_date = 0
    rows: List[Sample] = []
    for m in _AE_ITEM.finditer(html):
        try:
            item = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        ad_id = (item.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
        card = cards.get(ad_id, {})
        pub = parse_iso_date(item.get("lastUpdate") or "")
        if pub is None:
            no_date += 1
            continue
        if not in_window(pub, as_of):
            continue
        if want_year and card.get("year") and abs(card["year"] - want_year) > tol:
            continue
        amount, currency = parse_price(card.get("price_text") or "")
        if amount is None and item.get("unitPrice") is not None:
            amount, currency = float(item["unitPrice"]), "USD"
        if not currency:
            currency = "USD"          # на сайте цены по умолчанию в у.е.
        smp = Sample(
            title=card.get("title") or "%s %s" % (
                (item.get("attributes") or {}).get("brand", ""),
                (item.get("attributes") or {}).get("model", "")),
            price=amount,
            currency=currency,
            price_uzs=to_uzs(amount, currency, rate),
            published=pub.isoformat(),
            url=item.get("url") or "",
        )
        # год и пробег карточки — для отбора аналогов (fetch_avtoelon_analogs); в контракт Sample (asdict) не входят
        smp.year = card.get("year")
        smp.mileage_km = card.get("mileage_km")
        rows.append(smp)
    if no_date:
        res.limitations.append(
            "%d объявлений отброшено: дату получить не удалось (без даты в выборку не берём)." % no_date
        )
    return rows


# --------------------------------------------------------------------------- #
# Аналоги для оценки ТС (ТЗ 13.10.2026, п. 5.1)
# --------------------------------------------------------------------------- #

_AE_BLOCK = re.compile(r'<div\s+data-id="(\d+)"[^>]*class="[^"]*list-item a-elem[^"]*"', re.S)
_AE_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*г\.")
_AE_KM = re.compile(r"(\d[\d ]*)\s*км\b")
_AE_PRICE = re.compile(r"Цена:\s*(~?\s*[\d ]+(?:[.,]\d+)?\s*(?:y\.e\.|у\.е\.|сум|so.m|\$))")


def _plain_text(fragment: str) -> str:
    t = re.sub(r"<[^>]+>", " ", fragment or "")
    t = t.replace("&nbsp;", " ")
    for sp in _SPACES:
        t = t.replace(sp, " ")
    return re.sub(r"\s+", " ", t).strip()


def parse_avtoelon_cards(html: str) -> Dict[str, dict]:
    """
    Карточки выдачи avtoelon.uz по блокам: id → {title, price_text, year, mileage_km}.
    Блок — от <div data-id=… class="… list-item a-elem"> до следующей карточки. Год и пробег — из строки описания
    («2022 г., 1.5 л, Бензин, 64 000 км»); подписи картинок (alt) не читаются.
    """
    out: Dict[str, dict] = {}
    html = html or ""
    marks = list(_AE_BLOCK.finditer(html))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(html)
        blk = html[m.start():end]
        nxt = blk.find("listing.items.push")
        if nxt > 0:
            blk = blk[:nxt]
        title = re.search(r'js__advert-link"[^>]*>([^<]*)</a>', blk)
        k = blk.find('class="price"')
        body = _plain_text(re.sub(r"<img[^>]*>", " ", blk[k:] if k >= 0 else blk))
        price = _AE_PRICE.search(body)
        y = _AE_YEAR.search(body)
        km = _AE_KM.search(body)
        try:
            km_v = float(km.group(1).replace(" ", "")) if km else None
        except ValueError:
            km_v = None
        out[m.group(1)] = {"title": re.sub(r"\s+", " ", title.group(1)).strip() if title else "",
                           "price_text": price.group(1) if price else "",
                           "year": int(y.group(1)) if y else None, "mileage_km": km_v}
    return out


ANALOG_QUERY_KEYS = ("page", "year[from]")      # ТЗ 13.10.2026 п. 5.1: только эти параметры (их разрешает robots.txt)
AVTOELON_CARS = "https://avtoelon.uz/avto"


def analog_url(brand: str, model: str, year: Optional[int], page: int, base: str = AVTOELON_CARS) -> str:
    """Адрес выдачи: <раздел>/<марка>/<модель>/?year[from]=<год>[&page=N]. Других параметров не бывает."""
    parts = [base.rstrip("/"), _slug(brand)]
    if model:
        parts.append(_slug(model))
    url = "/".join(p for p in parts if p) + "/"
    q = []
    if year:
        q.append("year%%5Bfrom%%5D=%d" % int(year))   # скобки кодируем: с сырыми «[]» сайт из некоторых сетей отвечает 404
    if page > 1:
        q.append("page=%d" % int(page))
    return url + ("?" + "&".join(q) if q else "")


def query_ok(url: str) -> bool:
    """В адресе только разрешённые ТЗ параметры (?page=, ?year[from]=)."""
    qs = urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query, keep_blank_values=True)
    return all(k in ANALOG_QUERY_KEYS for k, _ in qs)


def fetch_avtoelon_analogs(brand: str, model: str, year: Optional[int], as_of: date, rate: Optional[float],
                           max_pages: int = 15, target: int = 20, year_tolerance: int = 0,
                           base: str = AVTOELON_CARS, pages: Optional[List[str]] = None,
                           keep_html: bool = False) -> dict:
    """
    Аналоги для оценки ТС: выдача avtoelon.uz по марке и модели с ?year[from]=<год>, дальше &page=N, пока не
    наберётся target аналогов нужного года (± year_tolerance) или не кончатся страницы (max_pages).
    Каждый адрес проверяется по списку параметров и по robots.txt (urllib.robotparser и RFC 9309); пауза между
    обращениями — MIN_DELAY_SEC, User-Agent — честный. pages — сохранённые страницы (проверка без сети): тогда в сеть
    не ходим вовсе. Окно свежести — WINDOW_MONTHS по lastUpdate (как у остальных адаптеров).
    Возвращает {source, status, reason, urls, pages_read, analogs: [{title, price, currency, price_uzs, year,
    mileage_km, published, url}], dropped, limitations, fetched_at, offline, html?}.
    """
    out = {"source": "avtoelon.uz", "status": STATUS_DOWN, "reason": "", "urls": [], "pages_read": 0,
           "analogs": [], "dropped": {"other_year": 0, "no_year": 0, "stale_or_undated": 0, "no_price": 0,
                                      "duplicate": 0},
           "limitations": [], "fetched_at": _now(), "offline": pages is not None, "rate": rate}
    if not _slug(brand) or not _slug(model):
        out["reason"] = "нужны марка и модель латиницей — запрос к выдаче построить нельзя"
        return out
    want = int(year) if year else None
    seen, html_all = set(), []
    n_pages = len(pages) if pages is not None else max(1, int(max_pages))
    for page in range(1, n_pages + 1):
        url = analog_url(brand, model, want, page, base)
        out["urls"].append(url)
        if pages is not None:
            html = pages[page - 1]
        else:
            if not query_ok(url):
                out.update(status=STATUS_ROBOTS, reason="параметр адреса вне разрешённых (?page=, ?year[from]=): "
                                                        + url)
                return out
            allowed, why = robots_check(url)
            if not allowed:
                out.update(status=STATUS_ROBOTS, reason=why)
                return out
            try:
                html = _http_get(url)
            except urllib.error.HTTPError as e:
                out["reason"] = "сайт ответил HTTP %d на %s" % (e.code, url)
                break
            except Exception as e:
                out["reason"] = "не удалось загрузить выдачу (%s)" % type(e).__name__
                break
        out["pages_read"] += 1
        if keep_html:
            html_all.append(html)
        res = SourceResult(source="avtoelon.uz", status=STATUS_DOWN)
        rows = _parse_avtoelon(html, as_of, rate, {}, res)
        total_cards = len(_AE_ITEM.findall(html))
        out["dropped"]["stale_or_undated"] += max(0, total_cards - len(rows))
        for smp in rows:
            if smp.url in seen:
                out["dropped"]["duplicate"] += 1
                continue
            seen.add(smp.url)
            y = getattr(smp, "year", None)
            if want and y is None:
                out["dropped"]["no_year"] += 1
                continue
            if want and abs(int(y) - want) > int(year_tolerance):
                out["dropped"]["other_year"] += 1
                continue
            if smp.price is None:
                out["dropped"]["no_price"] += 1
                continue
            out["analogs"].append({"title": smp.title, "price": smp.price, "currency": smp.currency,
                                   "price_uzs": smp.price_uzs, "year": y,
                                   "mileage_km": getattr(smp, "mileage_km", None),
                                   "published": smp.published, "url": smp.url})
        if len(out["analogs"]) >= int(target) or total_cards == 0 or ("page=%d" % (page + 1)) not in html:
            break
    if keep_html:
        out["html"] = html_all
    out["status"] = STATUS_OK if out["analogs"] else (STATUS_EMPTY if out["pages_read"] else STATUS_DOWN)
    if not out["analogs"] and out["pages_read"] and not out["reason"]:
        out["reason"] = "аналогов %s %s %s года за %d мес. не найдено" % (brand, model, want or "", WINDOW_MONTHS)
    out["limitations"] = [
        "Дата у объявления — дата последнего обновления (lastUpdate); дату первой публикации выдача не отдаёт.",
        "Пробег аналога взят из строки описания карточки и показан справочно; поправка по пробегу — по пробегу "
        "оцениваемого объекта.",
        "?year[from]= отбирает машины этого года и новее; точный год аналога отбирается по карточке.",
        "Цена объявления — цена предложения, а не сделки: в оценке применяется скидка на торг.",
    ]
    return out


# --------------------------------------------------------------------------- #
# Адаптер 2. olx.uz — авто и недвижимость
# --------------------------------------------------------------------------- #

OLX_SECTIONS = {
    "авто": "https://www.olx.uz/transport/legkovye-avtomobili/",
    "недвижимость": "https://www.olx.uz/nedvizhimost/",
}


def fetch_olx(object_type: str, params: dict, as_of: date,
              rate: Optional[float] = None, max_pages: int = 1) -> SourceResult:
    """
    OLX Узбекистан. Проверка 20.09.2026: CloudFront отдаёт 403 Forbidden даже на
    /robots.txt. Правила сайта прочитать нельзя, значит загружать выдачу нельзя.
    Обход защиты запрещён правилами проекта, поэтому адаптер честно сообщает,
    что источник закрыт. Код обращения оставлен: если сайт откроется, адаптер
    начнёт работать без переделки.
    """
    url = OLX_SECTIONS.get(object_type, "https://www.olx.uz/")
    res = _new("olx.uz", url)
    allowed, why = robots_check(url)
    if not allowed:
        res.status = STATUS_ROBOTS
        res.reason = (why + ". OLX закрыт защитой CloudFront: правила robots.txt "
                      "прочитать невозможно, обход защиты правилами проекта запрещён.")
        return res
    try:
        html = _http_get(url)
    except urllib.error.HTTPError as e:
        res.reason = "OLX ответил HTTP %d (защита от автоматических запросов)" % e.code
        return res
    except Exception as e:
        res.reason = "OLX недоступен (%s)" % type(e).__name__
        return res
    if "data-testid=\"l-card\"" not in html and "offer-wrapper" not in html:
        res.reason = ("страница загрузилась, но карточек объявлений в HTML нет — "
                      "выдача рисуется в браузере")
        return res
    res.reason = ("HTML получен, но разбор выдачи OLX не реализован: до 20.09.2026 "
                  "сайт ни разу не отдал страницу, структуру проверить не на чем")
    res.limitations.append("Требуется повторная проверка доступности источника.")
    return res


# --------------------------------------------------------------------------- #
# Адаптер 3. uybor.uz — недвижимость
# --------------------------------------------------------------------------- #

UYBOR_PAGES = {
    ("жилое", "продажа"): "https://uybor.uz/prodaja-kvartir-tashkent",
    ("жилое", "новостройка"): "https://uybor.uz/prodaja-novostroyka-tashkent",
}


def fetch_uybor(object_type: str, params: dict, as_of: date,
                rate: Optional[float] = None, max_pages: int = 1) -> SourceResult:
    """
    uybor.uz. Проверка 20.09.2026: robots.txt открыт и выдачу не запрещает
    (Disallow только /admin/, /cgi-bin/, /tmp/), сайт отвечает HTTP 200,
    НО страница построена на Next.js без серверной отрисовки списка:
    в HTML (и в __NEXT_DATA__) нет ни одного объявления, ни цены, ни даты —
    только переводы интерфейса и SEO-текст. Объявления подгружаются
    в браузере. Публичного описанного API у сайта не опубликовано.
    """
    rooms = params.get("комнаты") or params.get("rooms")
    kind = (params.get("тип") or params.get("kind") or "жилое").lower()
    url = UYBOR_PAGES.get((kind, "продажа"), "https://uybor.uz/prodaja-kvartir-tashkent")
    if rooms:
        url = "https://uybor.uz/%s-komnatnaya-prodaja-vtorichka-tashkent" % rooms
    res = _new("uybor.uz", url)

    allowed, why = robots_check(url)
    if not allowed:
        res.status = STATUS_ROBOTS
        res.reason = why
        return res
    try:
        html = _http_get(url)
    except urllib.error.HTTPError as e:
        res.reason = "сайт ответил HTTP %d" % e.code
        return res
    except Exception as e:
        res.reason = "сайт недоступен (%s)" % type(e).__name__
        return res

    rows = _parse_next_data_ads(html, as_of, rate)
    if rows:
        _finish(res, rows, rate)
        return res
    res.status = STATUS_DOWN
    res.reason = ("robots.txt выдачу разрешает, сайт отвечает HTTP 200, но список "
                  "объявлений рендерится в браузере (Next.js): в HTML нет ни цен, "
                  "ни дат публикации. Без стороннего браузерного движка или "
                  "официального доступа к API данные получить нельзя.")
    res.limitations.append("Нужно решение заказчика: запрашивать ли у uybor.uz официальный доступ к данным.")
    return res


# --------------------------------------------------------------------------- #
# Адаптер 4. joymee.uz — недвижимость
# --------------------------------------------------------------------------- #

def fetch_joymee(object_type: str, params: dict, as_of: date,
                 rate: Optional[float] = None, max_pages: int = 1) -> SourceResult:
    """
    joymee.uz. Проверка 20.09.2026: robots.txt открыт (Allow: /, закрыты только
    /auth/, /profile, /components и карта), сайт отвечает HTTP 200.
    Список объявлений (/ru/announcements) рендерится в браузере — в HTML его нет.
    На странице ОТДЕЛЬНОГО объявления есть разметка schema.org с ценой и валютой
    (например price 39, priceCurrency USD) и районом, но НЕТ ни даты публикации,
    ни площади, ни числа комнат. Значит обязательный фильтр «не раньше чем за
    6 месяцев до даты заключения» выполнить невозможно → объявления в выборку
    не берутся.
    """
    url = "https://joymee.uz/ru/announcements"
    res = _new("joymee.uz", url)
    allowed, why = robots_check(url)
    if not allowed:
        res.status = STATUS_ROBOTS
        res.reason = why
        return res
    try:
        html = _http_get(url)
    except urllib.error.HTTPError as e:
        res.reason = "сайт ответил HTTP %d" % e.code
        return res
    except Exception as e:
        res.reason = "сайт недоступен (%s)" % type(e).__name__
        return res

    rows = _parse_next_data_ads(html, as_of, rate)
    if rows:
        _finish(res, rows, rate)
        return res
    res.status = STATUS_DOWN
    res.reason = ("robots.txt разрешает, сайт отвечает HTTP 200, но список объявлений "
                  "рисуется в браузере; на карточке отдельного объявления цена есть, "
                  "а даты публикации нет — фильтр «6 месяцев» выполнить нечем, "
                  "поэтому объявления в выборку не берутся.")
    res.limitations.append("Без даты публикации объявление в расчёт не включается (правило модуля).")
    return res


_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def _parse_next_data_ads(html: str, as_of: date, rate: Optional[float]) -> List[Sample]:
    """
    Если сайт на Next.js однажды начнёт отдавать объявления прямо в __NEXT_DATA__,
    разбор подхватится сам. Сегодня (20.09.2026) там только переводы и SEO —
    функция возвращает пустой список, и адаптер честно пишет «источник недоступен».
    """
    m = _NEXT_DATA.search(html)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    found: List[dict] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            keys = set(node.keys())
            if {"price"} & keys and ({"id"} & keys or {"slug"} & keys):
                found.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data.get("props", {}))
    rows: List[Sample] = []
    for ad in found:
        pub = None
        for key in ("publishedAt", "published_at", "createdAt", "created_at", "date"):
            pub = parse_iso_date(str(ad.get(key) or ""))
            if pub:
                break
        if not in_window(pub, as_of):
            continue
        amount = ad.get("price")
        currency = str(ad.get("currency") or ad.get("priceCurrency") or "UZS").upper()
        if currency not in ("UZS", "USD"):
            currency = "UZS"
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            continue
        rows.append(Sample(
            title=str(ad.get("title") or ad.get("name") or ""),
            price=amount,
            currency=currency,
            price_uzs=to_uzs(amount, currency, rate),
            published=pub.isoformat() if pub else None,
            url=str(ad.get("url") or ""),
        ))
    return rows


# --------------------------------------------------------------------------- #
# Реестр адаптеров
# --------------------------------------------------------------------------- #

ADAPTERS: Dict[str, Callable[..., SourceResult]] = {
    "avtoelon.uz": fetch_avtoelon,
    "olx.uz": fetch_olx,
    "uybor.uz": fetch_uybor,
    "joymee.uz": fetch_joymee,
}

BY_OBJECT = {
    "авто": ["avtoelon.uz", "olx.uz"],
    "недвижимость": ["uybor.uz", "joymee.uz", "olx.uz"],
}


def fetch(source: str, object_type: str, params: dict, as_of: date,
          rate: Optional[float] = None) -> SourceResult:
    """Общий вход: fetch('avtoelon.uz', 'авто', {...}, date(2026, 9, 20), rate=12000)."""
    adapter = ADAPTERS.get(source)
    if adapter is None:
        res = _new(source)
        res.reason = "адаптер источника «%s» не зарегистрирован" % source
        return res
    return adapter(object_type, params, as_of, rate)


def fetch_all(object_type: str, params: dict, as_of: date,
              rate: Optional[float] = None) -> List[SourceResult]:
    """Опрос всех источников по типу объекта. Недоступные источники остаются в списке
    со своим статусом — агент должен видеть, что именно не получилось."""
    out = []
    for name in BY_OBJECT.get(object_type, []):
        try:
            out.append(fetch(name, object_type, params, as_of, rate))
        except Exception as e:                       # адаптер не должен ронять расчёт
            r = _new(name)
            r.reason = "сбой адаптера (%s: %s)" % (type(e).__name__, e)
            out.append(r)
    return out


# --------------------------------------------------------------------------- #
# Курс валюты
# --------------------------------------------------------------------------- #

CBU_URL = "https://cbu.uz/ru/arkhiv-kursov-valyut/json/USD/%s/"


def cbu_usd_rate(as_of: date) -> Optional[float]:
    """
    Официальный курс ЦБ РУз (открытый JSON cbu.uz) на дату as_of — сумов за 1 доллар.

    ВНИМАНИЕ: использовать этот курс в расчёте можно только после согласования
    с заказчиком (вопрос задан в задаче 39). Пока согласования нет, функция
    вызывается вручную и результат в расчёт автоматически не подставляется.
    """
    url = CBU_URL % as_of.isoformat()
    allowed, why = robots_check(url)
    if not allowed:
        return None
    try:
        data = json.loads(_http_get(url, timeout=25))
    except Exception:
        return None
    if isinstance(data, list) and data:
        try:
            return float(data[0].get("Rate"))
        except (TypeError, ValueError):
            return None
    return None


# --------------------------------------------------------------------------- #
# Официальные представители марок (когда объявлений нет — цену запрашиваем у дилера)
# --------------------------------------------------------------------------- #

DEALERS_FILE = "data/dealers_uz.json"


def dealers(brand: Optional[str] = None) -> List[dict]:
    """
    Справочник официальных представителей марок в Узбекистане из data/dealers_uz.json.
    Сведения взяты только с официальных сайтов марок и проверены обращением к сайту;
    где данных нет — в поле стоит «уточнить у заказчика».
    """
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, *DEALERS_FILE.split("/"))
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f).get("дилеры", [])
    except (OSError, json.JSONDecodeError):
        return []
    if brand:
        b = brand.strip().lower()
        rows = [r for r in rows if b in str(r.get("марка", "")).lower()]
    return rows
