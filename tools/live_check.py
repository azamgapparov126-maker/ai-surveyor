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

Допуски (п. 10.1): ставка ±0,005 п.п. к эталону (у комплексного продукта — и каждая часть), стоимость в диапазоне
(valuation.range, иначе V в пределах ±15 % от стоимости объекта), PDF ≤ 2 страниц, ≤ 60 с на осмотр; у договоров —
договор прочитан и сверен (contract_check); у склада 4,2 млрд — ставка не ниже минимума 0,05 %.
Эталоны — tests/fixtures/live_check_expected.json (снимаются прогоном на текущем коде: --snapshot или если файла
нет; с датой, коммитом и адресом сервера). Расхождение с эталоном — «вне допуска» с разницей в отчёте.

Отчёт: data/reports/live_check_<дата>.md и .json, итог «N из 20 в допуске».
Telegram: только run(..., telegram=True) внутри сервера (бот app/tgbot.send и send_file, чат TG_OWNER_CHAT_ID из
окружения; нет переменной — пропуск с пометкой). Из командной строки отчёт в Telegram не уходит: токен бота с
локальной машины не берётся. Запуск из админки — нужен маршрут (например, POST /admin/live-check), который вызывает
run(url, telegram=True) в фоновом потоке; в app/main.py его пока нет.

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
import urllib.request
import uuid
import zlib
from datetime import datetime, timedelta, timezone
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
    res["premium"] = (a.get("premium") or {}).get("amount")
    res["decision"] = (a.get("decision") or {}).get("code")
    res["risk_level"] = (a.get("risk") or {}).get("level")
    val = a.get("valuation") or {}
    res["valuation"] = {k: val.get(k) for k in ("method", "available", "V", "range", "n", "reliability", "reason")} \
        if val else None
    sv = a.get("value_sv") or {}
    res["value_sv"] = {k: sv.get(k) for k in ("ratio_pct", "verdict", "legal_ref")} if sv else None
    cc = a.get("contract_check") if isinstance(a.get("contract_check"), dict) else {}
    res["contract_check"] = bool(cc.get("available"))
    res["contract_verdict"] = (cc.get("summary") or {}).get("verdict")


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
                src = v.get("source") or {}
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
    """Проверки одного осмотра: ставка к эталону, части, стоимость, страницы, время, договор, минимум."""
    checks = []
    case = case or {}

    def add(code, ok, text):
        checks.append({"code": code, "ok": ok, "text": text})

    if res.get("error"):
        add("flow", False, "поток не прошёл: " + res["error"])
    else:
        r = res.get("rate_pct")
        if exp is None:
            add("rate", False, "нет эталона ставки (снимите --snapshot)")
        elif r is None or exp.get("rate_pct") is None:
            add("rate", r is None and exp.get("rate_pct") is None, "ставка %s, эталон %s" % (r, exp.get("rate_pct")))
        else:
            d = r - float(exp["rate_pct"])
            add("rate", abs(d) <= RATE_TOL_PP + 1e-9, "ставка %.4f %%, эталон %.4f %%, разница %+.4f п.п."
                % (r, float(exp["rate_pct"]), d))
        if exp is not None and (exp.get("parts") or res.get("parts")):
            got = {p["class_code"]: p["rate_pct"] for p in res.get("parts") or []}
            want = {p["class_code"]: p["rate_pct"] for p in exp.get("parts") or []}
            bad = [c for c in sorted(set(got) | set(want)) if got.get(c) is None or want.get(c) is None
                   or abs(float(got[c]) - float(want[c])) > RATE_TOL_PP + 1e-9]
            add("parts", not bad, "части: " + ", ".join("кл. %s %s %%" % (c, got.get(c)) for c in sorted(got))
                + ("" if not bad else " — расходятся с эталоном: " + ", ".join(
                    "кл. %s %s → %s" % (c, want.get(c), got.get(c)) for c in bad)))
        ok, text = value_check(res)
        if ok is None and exp is not None and exp.get("valuation_method"):
            ok, text = False, text + "; в эталоне была оценка «%s»" % exp["valuation_method"]
        add("value", ok, text)
        pg = res.get("pages")
        add("pages", pg is not None and pg <= MAX_PAGES, "PDF %s стр." % pg)
        for lg, n in (res.get("pages_lang") or {}).items():
            add("pages_" + lg, n is not None and n <= MAX_PAGES, "PDF %s: %s стр." % (lg.upper(), n))
        if case.get("min_rate_pct") is not None:
            m = float(case["min_rate_pct"])
            add("min_rate", r is not None and r + 1e-9 >= m, "ставка %s %% не ниже минимума %s %%" % (r, m))
        if case.get("contract"):
            add("contract", bool(res.get("contract_read")) and bool(res.get("contract_check")),
                "договор прочитан: %s, сверка в акте: %s" % ("да" if res.get("contract_read") else "нет",
                                                               "да" if res.get("contract_check") else "нет"))
    sec = res.get("seconds")
    add("time", sec is not None and sec <= MAX_SECONDS, "%s с" % sec)
    status = "в допуске" if all(c["ok"] is not False for c in checks) else "вне допуска"
    if res.get("error"):
        status = "ошибка"
    return {"status": status, "checks": checks}


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


def render_md(rep: dict) -> str:
    L = ["# Проверка вживую: 20 учебных осмотров (ТЗ 13.10.2026, п. 10.1)", "",
         "Сервер: %s · %s (Ташкент) · коммит %s · эталон: %s" % (rep["url"], rep["date"], rep["commit"],
                                                                rep["expected"]["note"]), "",
         "**Итог: %s.**" % rep["summary"], "",
         "| № | Объект | Продукт | Ставка, % | Премия, сум | Стр. | Сек. | Статус | Что не так |",
         "|---|---|---|---|---|---|---|---|---|"]
    for r in rep["results"]:
        bad = "; ".join(c["text"] for c in r["eval"]["checks"] if c["ok"] is False)
        rate = "—" if r.get("rate_pct") is None else ("%.4f" % r["rate_pct"]).replace(".", ",")
        if r.get("parts"):
            rate += " справочно (части: %s)" % ", ".join("кл. %s — %s" % (p["class_code"], p["rate_pct"])
                                                       for p in r["parts"])
        L.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            r["id"], r["title"], (r.get("input") or {}).get("product_code") or "—", rate, _n(r.get("premium")),
            r.get("pages") if r.get("pages") is not None else "—", r.get("seconds"), r["eval"]["status"], bad))
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
        L += ["", "## Cobalt 2022 по аналогам avtoelon.uz (критерий 5: ≈ 111 млн, предупреждение о перестраховании)",
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
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _rel(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(ROOT))
    except ValueError:
        return str(p)


def cobalt_probe(client) -> dict:
    """
    Критерий 5 таблицы приёмки: Cobalt 2022 при S 150 млн — ≈ 111 млн сум по аналогам avtoelon.uz и предупреждение
    о перестраховании. Без цены нового: запасной метод не подменяет аналоги. Оценка не сохраняется (save = false).
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
    Осмотры против сервера url. snapshot — снять эталоны заново (их снимает и первый прогон, если файла нет);
    client_factory(url) — свой транспорт (тест подставляет ASGI), по умолчанию HttpClient. Возвращает отчёт (dict).
    """
    cases = [c for c in (cases or CASES) if not only or c["id"] in only]
    make = client_factory or HttpClient
    expected = None if snapshot else load_expected(expected_path)
    take = snapshot or expected is None
    now = datetime.now(TASHKENT)
    results = []
    for case in cases:
        client = make(url)                   # у каждого осмотра свой гость: лимиты и акты не смешиваются
        client.request("GET", "/tg/status")  # cookie gid выдаётся заранее, как в мини-аппе
        res = run_case(client, case)
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
        snap = {"meta": {"date": now.isoformat(timespec="seconds"), "commit": commit, "url": url,
                         "note": "эталоны сняты tools/live_check.py на текущем коде сервера; ставка ±0,005 п.п."},
                "cases": {r["id"]: snapshot_of(r) for r in results if not r.get("error")}}
        if only:                             # частичный прогон дописывает только свои осмотры
            merged = dict((load_expected(expected_path) or {}).get("cases") or {})
            merged.update(snap["cases"])
            snap["cases"] = merged
        if save:
            expected_path.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
        expected = snap
    exp_cases = (expected or {}).get("cases") or {}
    by_id = {c["id"]: c for c in cases}
    for r in results:
        r["eval"] = evaluate(r, exp_cases.get(r["id"]), by_id.get(r["id"]))
    passed = sum(1 for r in results if r["eval"]["status"] == "в допуске")
    meta = (expected or {}).get("meta") or {}
    rep = {"url": url, "date": now.isoformat(timespec="seconds"), "commit": commit, "total": len(results),
           "passed": passed, "summary": "%d из %d в допуске" % (passed, len(results)),
           "expected": {"path": _rel(expected_path), "taken_now": take,
                        "note": "снят этим прогоном" if take else "от %s, коммит %s" % (meta.get("date"),
                                                                                       meta.get("commit"))},
           "results": results, "cobalt": cob}
    rep["telegram"] = notify_telegram(rep, render_md(rep)) if telegram else \
        "не отправлено: запуск из командной строки (отправку делает сервер — нужен маршрут в админке)"
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
    ap.add_argument("--snapshot", action="store_true", help="снять эталоны заново (перезаписать файл эталонов)")
    ap.add_argument("--only", default="", help="через запятую: K1,T2,…")
    ap.add_argument("--no-save", action="store_true", help="не писать отчёт и эталоны")
    a = ap.parse_args(argv)
    only = [x.strip() for x in a.only.split(",") if x.strip()] or None
    print("Проверка вживую:", a.url)
    rep = run(a.url, snapshot=a.snapshot, save=not a.no_save, only=only)
    print()
    print(render_md(rep))
    for f in rep.get("files") or []:
        print("файл:", f)
    return 0 if rep["passed"] == rep["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
