"""
Проверка вживую (ТЗ разработчику 13.10.2026, день 10, п. 10.1): 20 учебных осмотров через тестовый сервер.

Состав (только учебные данные, без ФИО, паспортов и адресов граждан — правило проекта № 8):
  * классы 3, 7, 8, 9 — 8 объектов: легковой с фото, автокран, грузовик; груз по бирже; магазин с затратной оценкой;
    склад 4,2 млрд (контрольный пример, ставка не ниже минимума 0,05 %); квартира физлица; товарные запасы по бирже;
  * 5 комплексных продуктов (части из product_parts): 0305, 0833, 0312, 0805, 0825;
  * 4 объекта теста: легковой 0301 с повреждениями, автокран 0318, магазин 0821 (450 м², 2 этажа, износ 15 %),
    груз 0701;
  * 3 учебных договора tests/fixtures/contracts (DOCX, PDF с текстом, скан) — через /act/photos файлом договора.

Поток одного осмотра: [POST /valuation/vehicle] → [POST /act/photos (файлы)] → POST /act/make → GET /act/{id}.pdf.
Время — от первого запроса до получения PDF. Гостевой доступ (cookie gid), только стандартная библиотека
(urllib + cookie-jar): TestClient без httpx недоступен.

Эталоны — tests/fixtures/live_check_expected.json, у каждого осмотра два блока (ТЗ на корректировку 14.10.2026, К4):
  * snapshot — снимок прошлого прогона (поиск регрессий): ставка ±0,005 п.п. (у комплексного продукта — и каждая
    часть), стоимость в диапазоне (valuation.range, иначе ±15 % от стоимости объекта), оценка не пропала, договор
    не перестал читаться. Перезаписывается --snapshot (или первым прогоном, если файла нет);
  * target — цель из ТЗ, задаётся вручную, --snapshot её не трогает: K1 — V 105–117 млн по аналогам и вывод
    «страхование сверх страховой стоимости» при S 150 млн; K5, T3 — V по формуле затратного метода (±1 сум, цена
    1 м² из GET /reference/build-cost); K6 — ставка ≥ 0,05 %; P1–P5 — премия = сумма частей (±1 сум), ставка каждой
    части ≥ её минимума (GET /reference/product-parts/{код}); D1–D3 — договор прочитан, ≥ 8 из 10 ключевых полей,
    12 проверок сверки; у всех — PDF ≤ 2 страниц, ≤ 60 с.
avtoelon.uz недоступен с сервера → K1 получает «нет доступа к источнику» (не «вне допуска»), цель K1 закрывается
офлайн-тестом К2. Файла эталонов нет (в образ Docker tests/ не входит) — цели берутся из default_targets().

Отчёт: data/reports/live_check_<дата>.md и .json, итог двумя строками: «цели ТЗ: N из 20» (по ней приёмка) и
«без регрессий: N из 20»; у каждого осмотра вне допуска — поле, ожидание и факт.
Telegram: только run(..., telegram=True) внутри сервера (бот app/tgbot.send и send_file, чат TG_OWNER_CHAT_ID из
окружения; нет переменной — пропуск с пометкой). Из командной строки отчёт в Telegram не уходит: токен бота с
локальной машины не берётся. Из админки: POST /admin/live-check (app/main.py) — run(url, telegram=True) в фоновом
потоке, GET /admin/live-check/last — последний отчёт.

Запуск из корня:
    set PYTHONIOENCODING=utf-8
    sandbox\.venv\Scripts\python.exe tools\live_check.py [--url https://...] [--snapshot] [--only K1,T2] [--no-save]
Импорт: from tools.live_check import run; run(url) → отчёт (dict).
"""
import argparse
import http.cookiejar
import json
import mimetypes
import os
import re
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_URL = "https://ai-surveyor-production.up.railway.app"
EXPECTED = ROOT / "tests" / "fixtures" / "live_check_expected.json"
CONTRACTS = ROOT / "tests" / "fixtures" / "contracts"
REPORTS = ROOT / "data" / "reports"
TASHKENT = timezone(timedelta(hours=5))

RATE_TOL_PP = 0.005          # ставка ±0,005 п.п. к эталону
VALUE_TOL = 0.15             # стоимость: ±15 % от стоимости объекта, если у оценки нет диапазона
MAX_PAGES = 2
MAX_SECONDS = 60.0
TIMEOUT = 90

M = 1_000_000
TASHKENT_CITY = "город Ташкент"

# --------------------------------------------------------------------------- #
#  20 учебных осмотров
# --------------------------------------------------------------------------- #
# photos — учебные рисунки (без людей и номеров); vehicle — запрос POST /valuation/vehicle; contract — файл договора;
# fallback_must — поля, которые сотрудник ввёл бы руками, если договор не прочитан; langs — PDF ещё на этих языках.
# Стоимость грузов и запасов (K4, K8, T4) задана по биржевым ценам uzex.uz на 13.10.2026 (середина диапазона Q1–Q3
# прогона): цены биржи меняются, и через несколько недель эти суммы могут выйти из диапазона — тогда обновить.
CASES = [
    # ---- классы 3, 7, 8, 9
    {"id": "K1", "group": "класс 3", "title": "Легковой Chevrolet Cobalt 2022, 3 фото",
     "photos": ["car_front", "car_left", "car_rear"],
     "vehicle": {"brand": "Chevrolet", "model": "Cobalt", "year": 2022, "sum_insured": 150 * M, "price_new": 165 * M},
     "body": {"must": {"product_code": "0308", "sum_insured": 150 * M, "object_value": 95 * M, "region": TASHKENT_CITY},
              "optional": {"object_kind": "car", "year": 2022, "price_new": 165 * M, "location": "guarded",
                           "class_fields": {"brand": "Chevrolet", "model": "Cobalt"}}}},
    {"id": "K2", "group": "класс 3", "title": "Автокран XCMG QY25K 2018",
     "body": {"must": {"product_code": "0318", "sum_insured": 700 * M, "object_value": 720 * M,
                       "region": "Ташкентская область"},
              "optional": {"object_kind": "truck_crane", "year": 2018, "price_new": 2200 * M, "location": "construction",
                           "class_fields": {"brand": "XCMG", "model": "QY25K"}}}},
    {"id": "K3", "group": "класс 3", "title": "Грузовик Isuzu NPR 2020",
     "body": {"must": {"product_code": "0308", "sum_insured": 250 * M, "object_value": 255 * M,
                       "region": "Самаркандская область"},
              "optional": {"object_kind": "truck", "year": 2020, "price_new": 600 * M,
                           "class_fields": {"brand": "Isuzu", "model": "NPR"}}}},
    {"id": "K4", "group": "класс 7", "title": "Груз: цемент 400 т (биржа)",
     "body": {"must": {"product_code": "0702", "sum_insured": 230 * M, "object_value": 230 * M, "region": TASHKENT_CITY},
              "optional": {"object_kind": "cargo", "quantity": 400, "unit": "т",
                           "class_fields": {"cargo_kind": "цемент", "route": "Ахангаран — Ташкент"}}}},
    {"id": "K5", "group": "класс 8", "title": "Магазин 450 м², кирпич, 2016 (затратная оценка)",
     "body": {"must": {"product_code": "0807", "sum_insured": 2500 * M, "object_value": 2500 * M,
                       "region": TASHKENT_CITY},
              "optional": {"object_kind": "shop", "material": "brick", "year": 2016, "construction": "reinforced",
                           "activity": "office", "class_fields": {"area_m2": 450, "floors": 1}}}},
    {"id": "K6", "group": "класс 8", "title": "Склад 4,2 млрд (контрольный пример, минимум 0,05 %)",
     "min_rate_pct": 0.05,
     "body": {"must": {"product_code": "0807", "sum_insured": 4200 * M, "object_value": 4200 * M,
                       "region": TASHKENT_CITY},
              "optional": {"object_kind": "warehouse", "construction": "reinforced", "activity": "warehouse",
                           "class_fields": {"area_m2": 3000}}}},
    {"id": "K7", "group": "класс 8", "title": "Квартира физлица 65 м²",
     "body": {"must": {"product_code": "0807", "client_type": "fiz", "sum_insured": 900 * M, "object_value": 900 * M,
                       "region": TASHKENT_CITY},
              "optional": {"object_kind": "dwelling", "construction": "reinforced", "activity": "office",
                           "class_fields": {"area_m2": 65, "floors": 9}}}},
    {"id": "K8", "group": "класс 9", "title": "Товарные запасы: пшеница 300 т (биржа)",
     "body": {"must": {"product_code": "0808", "sum_insured": 970 * M, "object_value": 970 * M,
                       "region": "Ташкентская область"},
              "optional": {"object_kind": "stock", "object_type": "пшеница продовольственная", "quantity": 300,
                           "unit": "т", "class_fields": {"activity": "warehouse"}}}},
    # ---- комплексные продукты (части из product_parts)
    {"id": "P1", "group": "комплексный", "title": "0305 Комплексное ТС: ТС + НС + ОТВ",
     "body": {"must": {"product_code": "0305", "sum_insured": 230 * M, "object_value": 230 * M, "region": TASHKENT_CITY},
              "optional": {"parts": [{"class_code": "3", "sum_insured": 150 * M, "object_value": 160 * M,
                                      "object_kind": "car"},
                                     {"class_code": "1", "sum_insured": 30 * M},
                                     {"class_code": "13", "sum_insured": 50 * M}], "parts_confirmed": True}}},
    {"id": "P2", "group": "комплексный", "title": "0833 Имущество и ГО физлица: дом + ГО",
     "body": {"must": {"product_code": "0833", "client_type": "fiz", "sum_insured": 700 * M, "object_value": 700 * M,
                       "region": TASHKENT_CITY},
              "optional": {"parts": [{"class_code": "8", "sum_insured": 600 * M, "object_value": 600 * M,
                                      "object_kind": "dwelling"},
                                     {"class_code": "13", "sum_insured": 100 * M}], "parts_confirmed": True}}},
    {"id": "P3", "group": "комплексный", "title": "0312 Автокредит: залог ТС + фин. риск (необеспеченная часть)",
     "body": {"must": {"product_code": "0312", "sum_insured": 240 * M, "object_value": 240 * M, "region": TASHKENT_CITY},
              "optional": {"parts": [{"class_code": "3", "sum_insured": 200 * M, "object_value": 200 * M,
                                      "object_kind": "car"},
                                     {"class_code": "14", "sum_insured": 40 * M}], "parts_confirmed": True}}},
    {"id": "P4", "group": "комплексный", "title": "0805 Ипотека: квартира + НС",
     "body": {"must": {"product_code": "0805", "client_type": "fiz", "sum_insured": 1000 * M, "object_value": 1000 * M,
                       "region": TASHKENT_CITY},
              "optional": {"parts": [{"class_code": "8", "sum_insured": 800 * M, "object_value": 800 * M,
                                      "object_kind": "dwelling"},
                                     {"class_code": "1", "sum_insured": 200 * M}], "parts_confirmed": True}}},
    {"id": "P5", "group": "комплексный", "title": "0825 Имущество + общая ГО: офис + ГО",
     "body": {"must": {"product_code": "0825", "sum_insured": 1800 * M, "object_value": 1800 * M,
                       "region": TASHKENT_CITY},
              "optional": {"parts": [{"class_code": "8", "sum_insured": 1500 * M, "object_value": 1500 * M,
                                      "object_kind": "office"},
                                     {"class_code": "13", "sum_insured": 300 * M}], "parts_confirmed": True}}},
    # ---- 4 объекта теста
    {"id": "T1", "group": "объект теста", "title": "0301 Легковой в залоге, повреждения, 2 фото",
     "photos": ["car_front", "car_damage"], "langs": ["uz", "en"],
     "body": {"must": {"product_code": "0301", "sum_insured": 190 * M, "object_value": 190 * M, "region": TASHKENT_CITY},
              "optional": {"object_kind": "car", "year": 2021, "price_new": 400 * M,
                           "class_fields": {"brand": "Chevrolet", "model": "Malibu"}},
              "damages": [{"what": "вмятина и царапины", "where": "заднее левое крыло", "severity": "cosmetic"},
                          {"what": "трещина фары", "where": "передняя правая фара", "severity": "major"}]}},
    {"id": "T2", "group": "объект теста", "title": "0318 Автокран 2019, стройплощадка, 6 500 моточасов",
     "langs": ["uz", "en"],
     "body": {"must": {"product_code": "0318", "sum_insured": 560 * M, "object_value": 560 * M,
                       "region": "Ташкентская область"},
              "optional": {"object_kind": "truck_crane", "year": 2019, "price_new": 1500 * M, "location": "construction",
                           "guard": True, "class_fields": {"brand": "Ivanovets", "model": "KS-45717",
                                                           "drivers": "2 машиниста, стаж от 10 лет"}},
              "recognized": [{"key": "mileage", "value": "6 500 моточасов", "source": "input"}]}},
    {"id": "T3", "group": "объект теста", "title": "0821 Магазин 450 м², 2 этажа, износ 15 %", "langs": ["uz", "en"],
     "body": {"must": {"product_code": "0821", "sum_insured": 2400 * M, "object_value": 2500 * M,
                       "region": TASHKENT_CITY},
              "optional": {"object_kind": "shop", "construction": "reinforced", "activity": "office",
                           "material": "brick", "year": 2016, "address": "г. Ташкент, Учебный район, учебная улица, 1",
                           "class_fields": {"area_m2": 450, "floors": 2, "wear": "15 %"}}}},
    {"id": "T4", "group": "объект теста", "title": "0701 Груз: дизельное топливо 120 т", "langs": ["uz", "en"],
     "body": {"must": {"product_code": "0701", "sum_insured": 2030 * M, "object_value": 2030 * M,
                       "region": TASHKENT_CITY},
              "optional": {"object_kind": "cargo", "quantity": 120, "unit": "т",
                           "class_fields": {"cargo_kind": "дизельное топливо", "route": "Бухара — Ташкент"}}}},
    # ---- 3 учебных договора
    {"id": "D1", "group": "договор", "title": "Договор DOCX: огонь, 0807", "contract": "contract_fire.docx",
     "langs": ["uz", "en"],
     "fallback_must": {"product_code": "0807", "sum_insured": 2400 * M, "object_value": 2500 * M,
                       "region": TASHKENT_CITY},
     "body": {"optional": {"object_kind": "production", "construction": "reinforced", "activity": "office"}}},
    {"id": "D2", "group": "договор", "title": "Договор PDF с текстом: залог, 0801", "contract": "contract_pledge.pdf",
     "langs": ["uz", "en"],
     "fallback_must": {"product_code": "0801", "sum_insured": 1800 * M, "object_value": 2000 * M,
                       "region": "Ташкентская область"},
     "body": {"optional": {"object_kind": "warehouse", "construction": "reinforced", "activity": "warehouse"}}},
    {"id": "D3", "group": "договор", "title": "Договор-скан (PDF без текста): ущерб, 0808",
     "contract": "contract_scan.pdf", "langs": ["uz", "en"],
     "fallback_must": {"product_code": "0808", "sum_insured": 900 * M, "object_value": 950 * M,
                       "region": TASHKENT_CITY},
     "body": {"optional": {"object_kind": "shop", "class_fields": {"activity": "office"}}}},
]


# --------------------------------------------------------------------------- #
#  HTTP: urllib + cookie-jar (гость получает cookie gid при первом обращении)
# --------------------------------------------------------------------------- #

class HttpClient:
    """Клиент одного «сотрудника»: свой cookie-jar, значит, свой гость и свои акты."""

    def __init__(self, base: str, timeout: int = TIMEOUT):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method: str, path: str, data: Optional[bytes] = None, ctype: Optional[str] = None) -> tuple:
        headers = {"Accept": "application/json, application/pdf", "User-Agent": "inson-live-check/1.0"}
        if ctype:
            headers["Content-Type"] = ctype
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                return r.status, r.read(), {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            return e.code, e.read(), {k.lower(): v for k, v in (e.headers or {}).items()}


def _json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return None


def post_json(client, path: str, body: dict) -> tuple:
    st, raw, _h = client.request("POST", path, json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 "application/json")
    return st, _json(raw)


def multipart(fields: dict, files: list) -> tuple:
    """(тело, Content-Type) для multipart/form-data: files — [(имя, байты, mime)] в поле files."""
    b = "----inson-live-" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, k, v)).encode("utf-8")
    for name, data, mime in files:
        out += ('--%s\r\nContent-Disposition: form-data; name="files"; filename="%s"\r\nContent-Type: %s\r\n\r\n'
                % (b, name, mime)).encode("utf-8")
        out += data + b"\r\n"
    out += ("--%s--\r\n" % b).encode("utf-8")
    return bytes(out), "multipart/form-data; boundary=" + b


# --------------------------------------------------------------------------- #
#  Учебные снимки: рисунок автомобиля, без людей, номеров и надписей с ПД
# --------------------------------------------------------------------------- #

def _png_plain(w: int, h: int, rgb: tuple) -> bytes:
    """PNG одного цвета без сторонних библиотек (запасной вариант, если нет pymupdf)."""
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _pymupdf():
    try:
        import pymupdf
        return pymupdf
    except ImportError:
        return None


def training_photo(kind: str) -> tuple:
    """(имя файла, байты PNG, mime) — учебный рисунок: car_front, car_rear, car_left, car_damage."""
    pm = _pymupdf()
    if pm is None:
        return kind + ".png", _png_plain(64, 48, (90, 110, 130)), "image/png"
    doc = pm.open()
    page = doc.new_page(width=480, height=320)
    page.draw_rect(pm.Rect(0, 0, 480, 320), color=None, fill=(0.78, 0.84, 0.9))
    page.draw_rect(pm.Rect(0, 230, 480, 320), color=None, fill=(0.45, 0.45, 0.45))
    body = (0.85, 0.85, 0.88)
    if kind in ("car_left", "car_damage"):
        page.draw_rect(pm.Rect(70, 150, 410, 230), color=(0.2, 0.2, 0.2), fill=body)
        page.draw_rect(pm.Rect(140, 105, 330, 152), color=(0.2, 0.2, 0.2), fill=(0.55, 0.7, 0.8))
        for x in (140, 340):
            page.draw_circle(pm.Point(x, 232), 28, color=(0, 0, 0), fill=(0.1, 0.1, 0.1))
        if kind == "car_damage":
            page.draw_polyline([pm.Point(330, 170), pm.Point(360, 190), pm.Point(345, 210), pm.Point(375, 220)],
                               color=(0.6, 0.1, 0.1), width=4)
    else:
        page.draw_rect(pm.Rect(130, 120, 350, 235), color=(0.2, 0.2, 0.2), fill=body)
        page.draw_rect(pm.Rect(155, 130, 325, 175), color=(0.2, 0.2, 0.2), fill=(0.55, 0.7, 0.8))
        light = (1, 0.95, 0.6) if kind == "car_front" else (0.8, 0.1, 0.1)
        page.draw_rect(pm.Rect(140, 190, 175, 205), color=None, fill=light)
        page.draw_rect(pm.Rect(305, 190, 340, 205), color=None, fill=light)
    page.insert_text(pm.Point(12, 22), "TRAINING PHOTO / no personal data", fontsize=12, color=(0.1, 0.1, 0.3))
    png = page.get_pixmap(dpi=96).tobytes("png")
    doc.close()
    return kind + ".png", png, "image/png"


def pdf_pages(blob: bytes) -> Optional[int]:
    """Число страниц PDF: pymupdf, иначе наибольший /Count дерева страниц."""
    if not blob or not blob.startswith(b"%PDF"):
        return None
    pm = _pymupdf()
    if pm is not None:
        try:
            with pm.open(stream=blob, filetype="pdf") as d:
                return d.page_count
        except Exception:
            return None
    counts = [int(x) for x in re.findall(rb"/Type\s*/Pages\b[^>]*?/Count\s+(\d+)", blob)]
    return max(counts) if counts else None


# --------------------------------------------------------------------------- #
#  Один осмотр
# --------------------------------------------------------------------------- #

def rate_of(a: dict) -> Optional[float]:
    """Ставка акта, % годовых: одна ставка акта; у договора из частей — премия / страховая сумма (справочно)."""
    p, r = a.get("premium") or {}, a.get("rate") or {}
    for v in (p.get("rate_pct"), r.get("final_pct"), r.get("applied_pct"), r.get("reference_pct")):
        if isinstance(v, (int, float)):
            return float(v)
    return None


def part_rates(a: dict) -> list:
    r = a.get("rate") or {}
    return [{"class_code": str(x.get("class_code")),
             "rate_pct": x.get("final_pct") if x.get("final_pct") is not None else x.get("applied_pct"),
             "min_pct": x.get("min_pct")} for x in r.get("by_parts") or []]


def _photos_step(client, case: dict, body: dict, res: dict, lang: str) -> None:
    """POST /act/photos: учебные снимки и файл договора; договор → поля осмотра и optional.contract."""
    files = [training_photo(k) for k in case.get("photos") or []]
    if case.get("contract"):
        p = CONTRACTS / case["contract"]
        files.append((p.name, p.read_bytes(), mimetypes.guess_type(p.name)[0] or "application/octet-stream"))
    if not files:
        return
    kinds = {str(i): ("document" if f[0] == case.get("contract") else "object") for i, f in enumerate(files, 1)}
    must = body["must"]
    data, ctype = multipart({"lang": lang, "kinds": json.dumps(kinds), "product_code": must.get("product_code") or "",
                             "class_code": must.get("class_code") or ""}, files)
    st, raw, _h = client.request("POST", "/act/photos", data, ctype)
    ph = _json(raw) or {}
    res["steps"].append(["POST /act/photos", st])
    if st != 200:
        raise RuntimeError("/act/photos: HTTP %s %s" % (st, str(ph.get("detail") or raw[:200])))
    body["session"] = ph.get("session")
    res["photos"] = {"files": len(files), "ai": bool(ph.get("ai")), "message": ph.get("message")}
    if not ph.get("ai") and case.get("photos"):
        res["notes"].append("фото не распознаны моделью: " + str(ph.get("message") or "—")[:160])
    if not case.get("contract"):
        return
    ctr = ph.get("contract") or {}
    pre = ph.get("prefill") or {}
    res["contract_read"] = bool(ctr.get("detected")) and not ctr.get("is_template")
    if res["contract_read"]:
        for k in ("product_code", "sum_insured", "object_value", "region"):
            if (pre.get(k) or {}).get("value") not in (None, ""):
                must[k] = pre[k]["value"]
        body["optional"]["contract"] = ctr.get("request")
        res["contract_found"] = len(ctr.get("found") or [])
    else:
        res["notes"].append("договор не прочитан: " + str(ph.get("message") or "нет блока contract")[:200]
                            + " — поля введены вручную")


def _act_fields(a: dict, res: dict) -> None:
    """Измерения из ответа /act/make."""
    res["act_id"] = a.get("id")
    res["number"] = a.get("number")
    res["rate_pct"] = rate_of(a)
    res["rate_mode"] = (a.get("rate") or {}).get("mode")
    res["min_pct"] = (a.get("rate") or {}).get("min_pct")
    res["parts"] = part_rates(a)
    items = (a.get("parts") or {}).get("items") if isinstance(a.get("parts"), dict) else None
    res["parts_premium"] = [{"class_code": str(x.get("class_code")), "premium": x.get("premium")}
                            for x in items or []]
    res["premium"] = (a.get("premium") or {}).get("amount")
    res["decision"] = (a.get("decision") or {}).get("code")
    res["risk_level"] = (a.get("risk") or {}).get("level")
    val = a.get("valuation") or {}
    res["valuation"] = {k: val.get(k) for k in ("method", "available", "V", "range", "n", "reliability", "reason")} \
        if val else None
    sv = a.get("value_sv") or {}
    res["value_sv"] = {k: sv.get(k) for k in ("ratio_pct", "verdict", "label", "legal_ref")} if sv else None
    cc = a.get("contract_check") if isinstance(a.get("contract_check"), dict) else {}
    res["contract_check"] = bool(cc.get("available"))
    res["contract_verdict"] = (cc.get("summary") or {}).get("verdict")
    res["contract_checks"] = sorted(c.get("no") for c in cc.get("checks") or [] if isinstance(c, dict)
                                    and isinstance(c.get("no"), int))


def run_case(client, case: dict, lang: str = "ru") -> dict:
    """Один осмотр по потоку ТЗ. Возвращает измерения; ошибка шага — в error, исключения наружу не выходят."""
    body = json.loads(json.dumps(case.get("body") or {}))
    body.setdefault("must", {})
    body.setdefault("optional", {})
    body["lang"] = lang
    res = {"id": case["id"], "group": case["group"], "title": case["title"], "steps": [], "error": None, "notes": []}
    t0 = time.monotonic()
    try:
        # 1. оценка ТС по аналогам (avtoelon.uz с сервера; нет аналогов — износ от цены нового)
        if case.get("vehicle"):
            st, v = post_json(client, "/valuation/vehicle", case["vehicle"])
            res["steps"].append(["POST /valuation/vehicle", st])
            if st == 200 and isinstance(v, dict):
                # запасной метод (износ): статус сайта — в analogs_tried.source, а source — «цена нового»
                src = (v.get("analogs_tried") or {}).get("source") or v.get("source") or {}
                res["vehicle_valuation"] = {k: v.get(k) for k in ("method", "available", "V", "n", "n_found",
                                                                  "reliability", "fallback_reason", "valuation_id")}
                res["vehicle_valuation"].update(source_status=src.get("status"), source_reason=src.get("reason"),
                                                warnings=v.get("warnings") or [])
                if v.get("valuation_id"):
                    body["optional"]["valuation_id"] = v["valuation_id"]
            else:
                res["notes"].append("оценка ТС не получена: HTTP %s" % st)
        # 2. фото и документы
        _photos_step(client, case, body, res, lang)
        for k, v in (case.get("fallback_must") or {}).items():
            body["must"].setdefault(k, v)
        res["input"] = {k: body["must"].get(k) for k in ("product_code", "sum_insured", "object_value")}
        # 3. акт
        st, a = post_json(client, "/act/make", body)
        res["steps"].append(["POST /act/make", st])
        if st != 200 or not isinstance(a, dict):
            a = a if isinstance(a, dict) else {}
            raise RuntimeError("/act/make: HTTP %s %s" % (st, json.dumps(a.get("errors") or a.get("detail"),
                                                                         ensure_ascii=False)[:300]))
        _act_fields(a, res)
        # 4. PDF
        st, pdf, _h = client.request("GET", "/act/%s.pdf" % a["id"])
        res["steps"].append(["GET /act/{id}.pdf", st])
        res["seconds"] = round(time.monotonic() - t0, 1)
        res["pages"] = pdf_pages(pdf) if st == 200 else None
        if st != 200:
            raise RuntimeError("/act/{id}.pdf: HTTP %s" % st)
        # тот же акт на других языках — вне времени осмотра
        for lg in case.get("langs") or []:
            st, pdf, _h = client.request("GET", "/act/%s.pdf?lang=%s" % (a["id"], lg))
            res.setdefault("pages_lang", {})[lg] = pdf_pages(pdf) if st == 200 else None
    except Exception as e:                          # осмотр не прошёл — в отчёт, остальные идут дальше
        res["error"] = "%s: %s" % (type(e).__name__, str(e)[:400])
        res.setdefault("seconds", round(time.monotonic() - t0, 1))
    return res


# --------------------------------------------------------------------------- #
#  Сравнение с эталоном и допусками
# --------------------------------------------------------------------------- #

def _n(x) -> str:
    if x is None:
        return "—"
    return "{:,.0f}".format(float(x)).replace(",", " ")


def snapshot_of(res: dict) -> dict:
    """Эталон одного осмотра — то, что сравнивается в следующих прогонах."""
    v = res.get("valuation") or {}
    return {"title": res["title"], "rate_pct": res.get("rate_pct"),
            "parts": [{"class_code": p["class_code"], "rate_pct": p["rate_pct"]} for p in res.get("parts") or []],
            "premium": res.get("premium"), "valuation_method": v.get("method") if v.get("available") else None,
            "V": v.get("V") if v.get("available") else None, "contract_read": res.get("contract_read")}


def value_check(res: dict) -> tuple:
    """
    (True | False | None, текст): стоимость в диапазоне. Есть valuation.range — внутри должны быть стоимость объекта
    и сама V; диапазона нет — V в пределах ±15 % от стоимости объекта. None — у объекта нет оценки.
    """
    v = res.get("valuation") or {}
    ov = (res.get("input") or {}).get("object_value")
    if not v.get("available") or not v.get("V"):
        why = v.get("reason") if v else "метод оценки к объекту не применяется"
        return None, "оценки нет (%s)" % (why or "—")
    V = float(v["V"])
    rng = v.get("range") if isinstance(v.get("range"), dict) else None
    if rng and rng.get("low") is not None and rng.get("high") is not None and ov:
        # в диапазоне должны быть и стоимость объекта, и сама оценка V: иначе S/V акта противоречит диапазону
        lo, hi = float(rng["low"]), float(rng["high"])
        ov_in, v_in = lo <= float(ov) <= hi, lo <= V <= hi
        text = "%s: стоимость %s %s диапазона %s–%s" % (v.get("method"), _n(ov), "внутри" if ov_in else "вне",
                                                        _n(lo), _n(hi))
        if not v_in:
            text += ", но сама оценка V %s вне своего диапазона" % _n(V)
        return ov_in and v_in, text
    if not ov:
        return False, "нет стоимости объекта для сравнения"
    dev = abs(V - float(ov)) / float(ov)
    return dev <= VALUE_TOL + 1e-12, "%s: V %s, отклонение от стоимости %s — %.1f %% (допуск 15 %%)" % (
        v.get("method"), _n(V), _n(ov), dev * 100)


def evaluate(res: dict, exp: Optional[dict], case: Optional[dict] = None) -> dict:
    """
    Регрессия к снимку (snapshot): ставка и части ±0,005 п.п., стоимость в диапазоне, оценка не пропала, договор не
    перестал читаться, PDF ≤ 2 страниц, ≤ 60 с. Цели ТЗ — отдельно, evaluate_target.
    """
    checks = []

    def add(code, ok, text, field, expected, actual):
        checks.append({"code": code, "ok": ok, "text": text, "field": field, "expected": expected, "actual": actual})

    if res.get("error"):
        add("flow", False, "поток не прошёл: " + res["error"], "поток осмотра", "акт и PDF без ошибок", res["error"])
    else:
        r = res.get("rate_pct")
        if exp is None:
            add("rate", False, "нет эталона ставки (снимите --snapshot)", "ставка, %", "снимок есть", "снимка нет")
        elif r is None or exp.get("rate_pct") is None:
            add("rate", r is None and exp.get("rate_pct") is None, "ставка %s, эталон %s" % (r, exp.get("rate_pct")),
                "ставка, %", exp.get("rate_pct"), r)
        else:
            d = r - float(exp["rate_pct"])
            add("rate", abs(d) <= RATE_TOL_PP + 1e-9, "ставка %.4f %%, эталон %.4f %%, разница %+.4f п.п."
                % (r, float(exp["rate_pct"]), d), "ставка, %", "%.4f ± 0,005" % float(exp["rate_pct"]), "%.4f" % r)
        if exp is not None and (exp.get("parts") or res.get("parts")):
            got = {p["class_code"]: p["rate_pct"] for p in res.get("parts") or []}
            want = {p["class_code"]: p["rate_pct"] for p in exp.get("parts") or []}
            bad = [c for c in sorted(set(got) | set(want)) if got.get(c) is None or want.get(c) is None
                   or abs(float(got[c]) - float(want[c])) > RATE_TOL_PP + 1e-9]
            add("parts", not bad, "части: " + ", ".join("кл. %s %s %%" % (c, got.get(c)) for c in sorted(got))
                + ("" if not bad else " — расходятся с эталоном: " + ", ".join(
                    "кл. %s %s → %s" % (c, want.get(c), got.get(c)) for c in bad)),
                "ставки частей, %", ", ".join("кл. %s %s" % (c, want.get(c)) for c in (bad or sorted(want))),
                ", ".join("кл. %s %s" % (c, got.get(c)) for c in (bad or sorted(got))))
        ok, text = value_check(res)
        if ok is None and exp is not None and exp.get("valuation_method"):
            ok, text = False, text + "; в эталоне была оценка «%s»" % exp["valuation_method"]
        v = res.get("valuation") or {}
        add("value", ok, text, "стоимость V", "в диапазоне оценки или ±15 % от стоимости объекта"
            + ("; метод «%s»" % exp["valuation_method"] if exp and exp.get("valuation_method") else ""),
            "метод %s, V %s" % (v.get("method"), _n(v.get("V"))) if v.get("available") else text)
        pg = res.get("pages")
        add("pages", pg is not None and pg <= MAX_PAGES, "PDF %s стр." % pg, "PDF, страниц", "≤ %d" % MAX_PAGES, pg)
        for lg, n in (res.get("pages_lang") or {}).items():
            add("pages_" + lg, n is not None and n <= MAX_PAGES, "PDF %s: %s стр." % (lg.upper(), n),
                "PDF %s, страниц" % lg.upper(), "≤ %d" % MAX_PAGES, n)
        if exp is not None and exp.get("contract_read"):
            got = bool(res.get("contract_read"))
            add("contract", got, "договор прочитан: %s (в снимке — да)" % ("да" if got else "нет"),
                "договор прочитан", "да (как в снимке)", "да" if got else "нет")
    sec = res.get("seconds")
    add("time", sec is not None and sec <= MAX_SECONDS, "%s с" % sec, "время осмотра, с", "≤ %d" % MAX_SECONDS, sec)
    status = "в допуске" if all(c["ok"] is not False for c in checks) else "вне допуска"
    if res.get("error"):
        status = "ошибка"
    return {"status": status, "checks": checks}


# --------------------------------------------------------------------------- #
#  Цели ТЗ (target): задаются вручную, --snapshot их не перезаписывает
# --------------------------------------------------------------------------- #

NO_SOURCE = "нет доступа к источнику"
# source.status ответа /valuation/vehicle (app/valuation.py), при которых сайт серверу недоступен: не ответил или
# не отдал robots.txt (адаптер тогда сайт не читает)
SOURCE_DOWN = ("источник недоступен", "запрещено robots.txt")
K2_OFFLINE = "tests/test_valuation_tz.py (К2: снимок 19 аналогов Cobalt 2022, V 105–117 млн)"
TARGET_META = {"source": "ТЗ на корректировку 14.10.2026, К4",
               "note": "цели задаются вручную; --snapshot их не перезаписывает; приёмка — по строке «цели ТЗ»"}
COMMON_TARGET = {"pdf_pages_max": MAX_PAGES, "seconds_max": int(MAX_SECONDS)}
# Затратный метод — ТЗ 13.10.2026, раздел 3: K_констр и T_сл оттуда же; k_wear = 1,00 (индекс в PDF ТЗ не читается,
# то же значение в настройках valuation.building). Цена 1 м² C берётся с сервера (GET /reference/build-cost).
COST_TARGET = {"formula": "V = A · C · K_констр · (1 − min(wear_cap; T/T_сл) · k_wear), T = текущий год − год постройки",
               "k_construction": {"reinforced_concrete": 1.15, "brick": 1.0, "sandwich": 0.7, "adobe": 0.55},
               "service_life": {"reinforced_concrete": 100, "brick": 80, "metal": 50},
               "wear_cap": 0.7, "k_wear": 1.0, "tol_sum": 1}


def default_targets() -> dict:
    """Цели ТЗ по осмотрам — то же, что блоки target в файле эталонов (тест следит, чтобы они совпадали)."""
    t = {c["id"]: dict(COMMON_TARGET) for c in CASES}
    t["K1"].update(vehicle_method="analogs", vehicle_V_min=105 * M, vehicle_V_max=117 * M, sum_insured=150 * M,
                   sv_verdict="over", sv_label="страхование сверх страховой стоимости", offline_test=K2_OFFLINE)
    for k in ("K5", "T3"):
        t[k]["cost"] = json.loads(json.dumps(COST_TARGET))
    t["K6"]["rate_min_pct"] = 0.05
    for k in ("P1", "P2", "P3", "P4", "P5"):
        t[k].update(premium_sum_of_parts_tol=1, parts_rate_min=True)
    for k in ("D1", "D2", "D3"):
        t[k].update(contract_read=True, key_fields_min=8, key_fields_total=10, contract_checks=12)
    return t


def today_tashkent() -> date:
    return datetime.now(TASHKENT).date()


def fetch_refs(client, case: dict, target: Optional[dict]) -> dict:
    """Справочники для целей с того же сервера: цена 1 м² (затратный метод) и минимумы ставок частей продукта."""
    ref = {}
    if not target:
        return ref
    body = case.get("body") or {}
    must, opt = body.get("must") or {}, body.get("optional") or {}
    if target.get("cost"):
        q = urllib.parse.urlencode({"region": must.get("region") or "", "building_type": opt.get("object_kind") or ""})
        st, raw, _h = client.request("GET", "/reference/build-cost?" + q)
        items = (_json(raw) or {}).get("items") if st == 200 else None
        day = today_tashkent().isoformat()
        rows = [r for r in items or [] if isinstance(r, dict) and str(r.get("date") or "") <= day]
        rows.sort(key=lambda r: (str(r.get("date")), r.get("id") or 0), reverse=True)
        ref["build_cost_http"], ref["build_cost"] = st, (rows[0] if rows else None)
    if target.get("parts_rate_min"):
        q = urllib.parse.urlencode({"client_type": must.get("client_type") or "yur"})
        st, raw, _h = client.request("GET", "/reference/product-parts/%s?%s" % (
            urllib.parse.quote(str(must.get("product_code") or "")), q))
        parts = (_json(raw) or {}).get("parts") if st == 200 else None
        ref["parts_http"] = st
        ref["parts_min"] = {str(p.get("class_code")): p.get("min_rate_pct") for p in parts or []
                            if isinstance(p, dict)} if parts is not None else None
    return ref


def cost_expected(case: dict, cost: dict, row: Optional[dict], today: Optional[date] = None) -> tuple:
    """(V по формуле ТЗ | None, расшифровка): площадь, материал и год — из осмотра, цена 1 м² — из справочника."""
    if not row or not isinstance(row.get("price_m2"), (int, float)):
        return None, "нет строки цены 1 м² в GET /reference/build-cost"
    opt = (case.get("body") or {}).get("optional") or {}
    area = (opt.get("class_fields") or {}).get("area_m2")
    material, year = opt.get("material"), opt.get("year")
    kc, t_sl = cost["k_construction"].get(material), cost["service_life"].get(material)
    if not area or kc is None or t_sl is None:
        return None, "нет площади или материала «%s» в таблице ТЗ" % material
    today = today or today_tashkent()
    T = max(0, today.year - int(year)) if year else 0
    wear = min(float(cost["wear_cap"]), T / float(t_sl)) * float(cost["k_wear"])
    C = float(row["price_m2"])
    V = round(float(area) * C * float(kc) * (1 - wear))
    return V, "%s · %s · %s · (1 − min(%s; %s/%s) · %s) = %s (строка build_cost_m2 №%s от %s)" % (
        _n(area), _n(C), kc, cost["wear_cap"], T, t_sl, cost["k_wear"], _n(V), row.get("id"), row.get("date"))


def source_down(res: dict) -> bool:
    """avtoelon.uz недоступен серверу: статус «источник недоступен» / «запрещено robots.txt» или HTTP 404."""
    vv = res.get("vehicle_valuation") or {}
    return vv.get("source_status") in SOURCE_DOWN or "404" in str(vv.get("source_reason") or "")


def evaluate_target(res: dict, target: Optional[dict], case: Optional[dict] = None) -> dict:
    """
    Цель ТЗ одного осмотра; каждая проверка — поле, ожидание, факт. «Нет доступа к источнику» — только если не
    сошлось лишь то, что зависит от avtoelon.uz, а сайт серверу не ответил.
    """
    checks = []
    case = case or {}
    ref = res.get("ref") or {}
    notes = []

    def add(field, expected, actual, ok, source=False):
        checks.append({"field": field, "expected": expected, "actual": actual, "ok": bool(ok), "source": source})

    if not target:
        add("цель ТЗ", "блок target в файле эталонов", "не задан", False)
        return {"status": "вне допуска", "checks": checks}
    if res.get("error"):
        add("поток осмотра", "акт и PDF без ошибок", res["error"], False)
    else:
        mx = target.get("pdf_pages_max")
        if mx is not None:
            pg = res.get("pages")
            add("PDF, страниц", "≤ %s" % mx, pg, pg is not None and pg <= mx)
            for lg, n in (res.get("pages_lang") or {}).items():
                add("PDF %s, страниц" % lg.upper(), "≤ %s" % mx, n, n is not None and n <= mx)
        if target.get("vehicle_V_min") is not None:
            vv = res.get("vehicle_valuation") or {}
            V, meth = vv.get("V"), vv.get("method")
            lo, hi = float(target["vehicle_V_min"]), float(target["vehicle_V_max"])
            add("V по аналогам avtoelon.uz", "%s–%s сум, метод %s" % (_n(lo), _n(hi), target.get("vehicle_method")),
                "метод %s, V %s, аналогов %s; источник: %s %s" % (
                    meth, _n(V), vv.get("n"), vv.get("source_status") or "ответил", vv.get("source_reason") or ""),
                meth == target.get("vehicle_method") and V is not None and lo <= float(V) <= hi, source=True)
        if target.get("sv_verdict"):
            sv = res.get("value_sv") or {}
            S = (res.get("input") or {}).get("sum_insured")
            # вывод проверяется по коду verdict; ярлык — только пометкой, пока К1 (термин) не выложен на сервер
            add("вывод S/V при S %s" % _n(target.get("sum_insured")),
                "%s — «%s»" % (target["sv_verdict"], target.get("sv_label")),
                "%s — «%s», S/V %s %%, S %s" % (sv.get("verdict"), sv.get("label") or "—", sv.get("ratio_pct"), _n(S)),
                sv.get("verdict") == target["sv_verdict"]
                and (target.get("sum_insured") is None or S == target["sum_insured"]), source=True)
            if sv.get("label") and target.get("sv_label") and sv["label"] != target["sv_label"]:
                notes.append("ярлык S/V на сервере «%s», по ТЗ «%s» — закрывается задачей К1; вывод проверен по коду %s"
                             % (sv["label"], target["sv_label"], sv.get("verdict")))
        if target.get("cost"):
            want, how = cost_expected(case, target["cost"], ref.get("build_cost"))
            v = res.get("valuation") or {}
            got = v.get("V") if v.get("available") else None
            tol = float(target["cost"].get("tol_sum", 1))
            add("V затратным методом", ("%s ± %s сум: %s" % (_n(want), _n(tol), how)) if want is not None else how,
                "метод %s, V %s" % (v.get("method"), _n(got)),
                want is not None and got is not None and v.get("method") == "cost" and abs(float(got) - want) <= tol)
        if target.get("rate_min_pct") is not None:
            r = res.get("rate_pct")
            add("ставка, %", "≥ %s" % target["rate_min_pct"], r, r is not None and r + 1e-9 >= target["rate_min_pct"])
        if target.get("premium_sum_of_parts_tol") is not None:
            pp = res.get("parts_premium") or []
            vals = [p.get("premium") for p in pp]
            total = res.get("premium")
            ok = len(pp) >= 2 and all(isinstance(x, (int, float)) for x in vals) and isinstance(total, (int, float)) \
                and abs(float(total) - sum(vals)) <= float(target["premium_sum_of_parts_tol"])
            add("премия договора, сум", "сумма частей %s ± %s" % (
                " + ".join(_n(x) for x in vals) or "—", target["premium_sum_of_parts_tol"]), _n(total), ok)
        if target.get("parts_rate_min"):
            mins = ref.get("parts_min")
            if mins is None:
                add("минимумы частей", "GET /reference/product-parts/{код} — 200", "HTTP %s" % ref.get("parts_http"),
                    False)
            elif not res.get("parts"):
                add("ставки частей", "у каждой части своя ставка", "частей в акте нет", False)
            else:
                for p in res["parts"]:
                    m, r = mins.get(p["class_code"]), p.get("rate_pct")
                    add("ставка части кл. %s, %%" % p["class_code"],
                        "≥ %s (справочник частей)" % m if m is not None else "минимум в справочнике не задан",
                        r, r is not None and (m is None or float(r) + 1e-9 >= float(m)))
        if target.get("contract_read"):
            got = bool(res.get("contract_read"))
            add("договор прочитан", "да", "да" if got else "нет", got)
            n = res.get("contract_found") or 0
            add("ключевые поля договора", "≥ %s из %s" % (target["key_fields_min"], target["key_fields_total"]),
                "%s из %s" % (n, target["key_fields_total"]), got and n >= target["key_fields_min"])
            nos = res.get("contract_checks") or []
            need = list(range(1, int(target["contract_checks"]) + 1))
            add("проверки сверки договора", "%s (№ 1–%s)" % (len(need), need[-1]),
                "%s%s" % (len(nos), (" (№ " + ", ".join(map(str, nos)) + ")") if nos else ""), nos == need)
    mx = target.get("seconds_max")
    if mx is not None:
        sec = res.get("seconds")
        add("время осмотра, с", "≤ %s" % mx, sec, sec is not None and sec <= mx)
    failed = [c for c in checks if not c["ok"]]
    if res.get("error"):
        status = "ошибка"
    elif not failed:
        status = "в допуске"
    elif all(c["source"] for c in failed) and source_down(res):
        status = NO_SOURCE
    else:
        status = "вне допуска"
    if status == NO_SOURCE:
        notes.insert(0, "avtoelon.uz недоступен с сервера; цель закрыта офлайн-тестом К2: %s" % (
            target.get("offline_test") or K2_OFFLINE))
    out = {"status": status, "checks": checks}
    if notes:
        out["note"] = "; ".join(notes)
    return out


# --------------------------------------------------------------------------- #
#  Отчёт
# --------------------------------------------------------------------------- #

def _git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT), capture_output=True, text=True,
                             timeout=10)
        if out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "")[:7] or "неизвестно"


def fail_lines(r: dict) -> list:
    """Что не сошлось у осмотра: поле, ожидание, факт — сначала цели ТЗ, потом регрессии к снимку."""
    out = []
    for c in (r.get("target_eval") or {}).get("checks") or []:
        if not c["ok"]:
            out.append("цель ТЗ · %s: ожидание %s; факт %s" % (c["field"], c["expected"], c["actual"]))
    for c in (r.get("eval") or {}).get("checks") or []:
        if c["ok"] is False:
            out.append("регрессия · %s: ожидание %s; факт %s" % (c.get("field") or c["code"], c.get("expected"),
                                                                 c.get("actual")))
    return out


def render_md(rep: dict) -> str:
    L = ["# Проверка вживую: 20 учебных осмотров (ТЗ 13.10.2026, п. 10.1; ТЗ на корректировку 14.10.2026, К4)", "",
         "Сервер: %s · %s (Ташкент) · коммит %s" % (rep["url"], rep["date"], rep["commit"]), "",
         "Цели (target): %s. Снимок (snapshot): %s." % (rep["expected"]["targets_note"], rep["expected"]["note"]), "",
         "**%s** — приёмка по этой строке.  " % rep["targets"]["line"],
         "**%s**" % rep["regress"]["line"], "",
         "| № | Объект | Продукт | Ставка, % | Премия, сум | Стр. | Сек. | Цель ТЗ | Без регрессий |",
         "|---|---|---|---|---|---|---|---|---|"]
    for r in rep["results"]:
        rate = "—" if r.get("rate_pct") is None else ("%.4f" % r["rate_pct"]).replace(".", ",")
        if r.get("parts"):
            rate += " справочно (части: %s)" % ", ".join("кл. %s — %s" % (p["class_code"], p["rate_pct"])
                                                       for p in r["parts"])
        L.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            r["id"], r["title"], (r.get("input") or {}).get("product_code") or "—", rate, _n(r.get("premium")),
            r.get("pages") if r.get("pages") is not None else "—", r.get("seconds"),
            (r.get("target_eval") or {}).get("status", "—"), r["eval"]["status"]))
    L += ["", "## Что не сошлось (поле, ожидание, факт)", ""]
    bad_any = False
    for r in rep["results"]:
        lines, note = fail_lines(r), (r.get("target_eval") or {}).get("note")
        if not lines and not note:
            continue
        bad_any = True
        L.append("### %s — %s (цель ТЗ: %s; без регрессий: %s)" % (
            r["id"], r["title"], (r.get("target_eval") or {}).get("status", "—"), r["eval"]["status"]))
        L += (["- " + note] if note else []) + ["- " + x for x in lines] + [""]
    if not bad_any:
        L.append("Всё сошлось.")
    L += ["", "## Стоимость и пометки", ""]
    for r in rep["results"]:
        v = next((c["text"] for c in r["eval"]["checks"] if c["code"] == "value"), "")
        extra = list(r.get("notes") or [])
        vv = r.get("vehicle_valuation")
        if vv:
            extra.append("оценка ТС: метод %s, V %s, аналогов %s; %s" % (
                vv.get("method"), _n(vv.get("V")), vv.get("n"), vv.get("fallback_reason") or vv.get("source_reason")
                or "аналоги avtoelon.uz"))
        if r.get("pages_lang"):
            extra.append("PDF UZ/EN: " + ", ".join("%s — %s стр." % (k.upper(), n) for k, n in r["pages_lang"].items()))
        L.append("- %s: %s%s" % (r["id"], v, ("; " + "; ".join(extra)) if extra else ""))
    cob = rep.get("cobalt")
    if cob:
        L += ["", "## Cobalt 2022 по аналогам avtoelon.uz (критерий 5: ≈ 111 млн, предупреждение о страховании сверх страховой стоимости)",
              "", "%s: метод %s, V %s, аналогов %s из %s; источник: %s %s; предупреждение: %s" % (
                  "выполнено" if cob["ok"] else "НЕ выполнено", cob.get("method"), _n(cob.get("V")), cob.get("n"),
                  cob.get("n_found"), cob.get("source_status") or "ответил", cob.get("source_reason") or "",
                  "; ".join(cob.get("warnings") or []) or "нет")]
    L += ["", "## Telegram", "", rep.get("telegram") or "—", ""]
    return "\n".join(L)


def notify_telegram(rep: dict, md_text: str) -> str:
    """Отчёт владельцу ботом сервера. Вызывается только внутри сервера (run(..., telegram=True))."""
    chat = (os.environ.get("TG_OWNER_CHAT_ID") or "").strip()
    if not chat:
        return "не отправлено: переменной TG_OWNER_CHAT_ID нет в окружении"
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from app import tgbot
    except Exception as e:
        return "не отправлено: бот недоступен (%s)" % type(e).__name__
    head = "Проверка вживую %s: %s" % (rep["date"][:16].replace("T", " "), rep["summary"])
    r1 = tgbot.send(chat, head, kind="live_check")
    r2 = tgbot.send_file(chat, "live_check_%s.md" % rep["date"][:10], md_text.encode("utf-8"), "text/markdown",
                         caption=head, kind="live_check")
    if r1.get("ok") and r2.get("ok"):
        return "отправлено владельцу (TG_OWNER_CHAT_ID)"
    return "не отправлено: %s" % (r1.get("reason") or r2.get("reason"))


def load_expected(path: Path = EXPECTED) -> Optional[dict]:
    """
    Файл эталонов: {"meta", "target_meta", "cases": {id: {"snapshot", "target"}}}. Старый плоский вид (снимок прямо
    в осмотре) читается как snapshot без цели.
    """
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = {}
    for k, v in (data.get("cases") or {}).items():
        if isinstance(v, dict) and ("snapshot" in v or "target" in v):
            cases[k] = {"snapshot": v.get("snapshot"), "target": v.get("target")}
        else:
            cases[k] = {"snapshot": v, "target": None}
    data["cases"] = cases
    return data


def write_snapshot(path: Path, results: list, meta: dict, save: bool = True) -> dict:
    """
    Новые снимки поверх файла: меняются только блоки snapshot и meta, блоки target остаются как были (цель задаётся
    вручную). Цель подставляется из default_targets() только туда, где её ещё нет (новый файл, старый формат).
    Осмотр с ошибкой прежний снимок не затирает.
    """
    data = load_expected(path) or {"meta": {}, "target_meta": dict(TARGET_META), "cases": {}}
    cases = data["cases"]
    targets = default_targets()
    for r in results:
        if not r.get("error"):
            cases.setdefault(r["id"], {"snapshot": None, "target": None})["snapshot"] = snapshot_of(r)
    for k, c in cases.items():
        if c.get("target") is None and k in targets:
            c["target"] = targets[k]
    order = [c["id"] for c in CASES if c["id"] in cases] + sorted(k for k in cases if k not in targets)
    out = {"meta": meta, "target_meta": data.get("target_meta") or dict(TARGET_META),
           "cases": {k: {"snapshot": cases[k].get("snapshot"), "target": cases[k].get("target")} for k in order}}
    if save:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def _rel(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(ROOT))
    except ValueError:
        return str(p)


def cobalt_probe(client) -> dict:
    """
    Критерий 5 таблицы приёмки: Cobalt 2022 при S 150 млн — ≈ 111 млн сум по аналогам avtoelon.uz и предупреждение
    о страховании сверх страховой стоимости. Без цены нового: запасной метод не подменяет аналоги. Оценка не сохраняется (save = false).
    """
    st, v = post_json(client, "/valuation/vehicle", {"brand": "Chevrolet", "model": "Cobalt", "year": 2022,
                                                     "sum_insured": 150 * M, "save": False})
    v = v if isinstance(v, dict) else {}
    src = v.get("source") or {}
    V = v.get("V")
    warn = [w for w in v.get("warnings") or [] if "от рынка" in w]
    ok = st == 200 and v.get("method") == "analogs" and bool(v.get("available")) and V is not None         and abs(float(V) - 111 * M) <= 0.15 * 111 * M and bool(warn)
    return {"ok": ok, "http": st, "method": v.get("method"), "available": v.get("available"), "V": V,
            "n": v.get("n"), "n_found": v.get("n_found"), "source_status": src.get("status"),
            "source_reason": src.get("reason"), "warnings": warn, "reason": v.get("reason")}


def run(url: str = DEFAULT_URL, *, snapshot: bool = False, save: bool = True, telegram: bool = False,
        only: Optional[list] = None, client_factory: Optional[Callable] = None, expected_path: Path = EXPECTED,
        reports_dir: Path = REPORTS, cases: Optional[list] = None, log: Callable = print,
        probe: bool = True) -> dict:
    """
    Осмотры против сервера url. snapshot — снять снимки заново (их снимает и первый прогон, если файла нет; цели
    target при этом не меняются); client_factory(url) — свой транспорт (тест подставляет ASGI), по умолчанию
    HttpClient. Возвращает отчёт (dict): rep["targets"]["line"] — «цели ТЗ: N из M», rep["regress"]["line"] —
    «без регрессий: N из M».
    """
    cases = [c for c in (cases or CASES) if not only or c["id"] in only]
    make = client_factory or HttpClient
    loaded = load_expected(expected_path)
    take = snapshot or loaded is None
    if loaded is None:                       # на сервере файла нет: tests/ в образ Docker не входит
        targets = default_targets()
        targets_note = "файла эталонов нет — цели из tools/live_check.py (default_targets)"
    else:
        targets = {k: v.get("target") for k, v in loaded["cases"].items()}
        targets_note = "блоки target файла %s (%s)" % (_rel(expected_path),
                                                     (loaded.get("target_meta") or {}).get("source") or "—")
    now = datetime.now(TASHKENT)
    results = []
    for case in cases:
        client = make(url)                   # у каждого осмотра свой гость: лимиты и акты не смешиваются
        client.request("GET", "/tg/status")  # cookie gid выдаётся заранее, как в мини-аппе
        res = run_case(client, case)
        try:
            res["ref"] = fetch_refs(client, case, targets.get(case["id"]))
        except Exception as e:               # справочник не прочитан — цель не закроется, причина в отчёте
            res["ref"] = {}
            res["notes"].append("справочники для цели не получены: %s: %s" % (type(e).__name__, str(e)[:200]))
        results.append(res)
        log("  %s %-62s %s с%s" % (res["id"], res["title"][:62], res.get("seconds"),
                                   ("  ОШИБКА " + res["error"][:120]) if res.get("error") else ""))
    cob = None
    if probe:
        client = make(url)
        client.request("GET", "/tg/status")
        cob = cobalt_probe(client)
    commit = _git_commit()
    if take:
        meta = {"date": now.isoformat(timespec="seconds"), "commit": commit, "url": url,
                "note": "снимки сняты tools/live_check.py на текущем коде сервера; ставка ±0,005 п.п."}
        data = write_snapshot(expected_path, results, meta, save=save)
    else:
        data = loaded
    exp_cases = data.get("cases") or {}
    by_id = {c["id"]: c for c in cases}
    for r in results:
        r["eval"] = evaluate(r, (exp_cases.get(r["id"]) or {}).get("snapshot"), by_id.get(r["id"]))
        r["target_eval"] = evaluate_target(r, targets.get(r["id"]), by_id.get(r["id"]))
    n = len(results)
    closed = sum(1 for r in results if r["target_eval"]["status"] in ("в допуске", NO_SOURCE))
    no_src = [r["id"] for r in results if r["target_eval"]["status"] == NO_SOURCE]
    line1 = "цели ТЗ: %d из %d" % (closed, n)
    if no_src:
        line1 += " (%s — %s: avtoelon.uz недоступен с сервера, цель закрыта офлайн-тестом К2)" % (
            ", ".join(no_src), NO_SOURCE)
    reg = sum(1 for r in results if r["eval"]["status"] == "в допуске")
    line2 = "без регрессий: %d из %d" % (reg, n)
    meta = data.get("meta") or {}
    rep = {"url": url, "date": now.isoformat(timespec="seconds"), "commit": commit, "total": n,
           "passed": closed, "summary": "%s; %s" % (line1, line2),
           "targets": {"closed": closed, "total": n, "no_source": no_src, "line": line1},
           "regress": {"passed": reg, "total": n, "line": line2},
           "expected": {"path": _rel(expected_path), "taken_now": take, "targets_note": targets_note,
                        "note": "снят этим прогоном" if take else "от %s, коммит %s" % (meta.get("date"),
                                                                                       meta.get("commit"))},
           "results": results, "cobalt": cob}
    rep["telegram"] = notify_telegram(rep, render_md(rep)) if telegram else \
        "не отправлено: запуск из командной строки (отправляет сервер: POST /admin/live-check)"
    md = render_md(rep)
    if save:
        reports_dir.mkdir(parents=True, exist_ok=True)
        stem = "live_check_%s" % now.strftime("%Y-%m-%d")
        md_path, js_path = reports_dir / (stem + ".md"), reports_dir / (stem + ".json")
        md_path.write_text(md, encoding="utf-8")
        js_path.write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        rep["files"] = [str(md_path), str(js_path)]
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="20 учебных осмотров на тестовом сервере (ТЗ 13.10.2026, п. 10.1)")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--snapshot", action="store_true",
                    help="снять снимки заново (в файле эталонов меняются только блоки snapshot, цели target — нет)")
    ap.add_argument("--only", default="", help="через запятую: K1,T2,…")
    ap.add_argument("--no-save", action="store_true", help="не писать отчёт и снимки")
    a = ap.parse_args(argv)
    only = [x.strip() for x in a.only.split(",") if x.strip()] or None
    print("Проверка вживую:", a.url)
    rep = run(a.url, snapshot=a.snapshot, save=not a.no_save, only=only)
    print()
    print(render_md(rep))
    for f in rep.get("files") or []:
        print("файл:", f)
    return 0 if rep["targets"]["closed"] == rep["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
