"""
Сюрвейерский акт, лёгкая версия (app/act.py, app/act_engine.py, app/docx_lite.py) — ТЗ 2.0 от 29.09.2026.

Запуск из корня (свой ASGI-клиент, живой сервер не трогаем):
    set PYTHONIOENCODING=utf-8
    sandbox\\.venv\\Scripts\\python.exe tests\\test_act.py

Всё — во временной копии базы (tests/tmpdb.py) и во временной папке файлов.
Дополнения 29.09.2026 (проверки 21–27): сценарии PML/EML/MFL, разбор документов, франшиза, рекомендации. Сеть и модель подменяются:
llm.chat_raw отдаёт заготовленный ответ, llm._post бросает исключение (любой выход в сеть = ошибка теста).
Проверки 37а–37г (30.09.2026): запрос филиала — разбор DOCX/XLSX/PDF с 16 строками (оба образца заказчика),
ответ модели по скану, сверка с расчётом акта (продукт 0832), многолетний срок, физлицо, три языка, Word и PDF.
Проверки 38а–38ж (30.09.2026): договор страхования — учебный договор, договоры на узбекской кириллице и латинице,
русском и английском (DOCX и PDF с текстом), длинный договор, скан (ответ модели подменён), дочитывание текста
моделью с маскировкой ПД, сверка с расчётом акта (график, существенные условия ГК ст. 929), запрос филиала + договор.
Проверки 39а–39з (30.09.2026, вечер): источник условий решает сервер (правки «было → стало» в акте), вид документа по
заголовку (полис, заявление), существенные условия без категоричности, сверка «запрос ↔ договор» как при загрузке,
границы ввода, счёт не уходит в модель, стороны-юрлица, отрицательные суммы, срок разбора PDF.
Проверки 40а–40в (30.09.2026, шаблон договора 0102): чтение DOCX деревом XML (прогоны, w:tab, w:br, w:sdt) и новые
пределы (10 000 ячеек, 3 000 абзацев, 4 000 знаков в строке таблицы DOCX); бланк договора личного страхования на
выдуманных данных (заголовок в две строки, подчёркивания, таблица приложения 1, пп. 2.6 и 5.4) — is_template, «не
заполнено», без ст. 929 и сверки, модель не вызывается; тот же шаблон заполненный — полноценный договор со сверкой.
Проверки 43а–43д (01.10.2026): страховой скоринг объекта (балл 0–500 = 500 − 5 × балл риска, классы и подклассы,
договор из частей, класс без аналитики), первая страница PDF и секция Word с картинкой, scoring.pdf/png и права,
отчёт кредитного бюро КАТМ (PDF с текстом на выдуманных данных, физлицо без ФИО, скан с подменённой моделью,
проверки заёмщика, правки сотрудника, классы 14/15 и не кредитный), три языка.
Проверки 46 (02.10.2026): факторы объекта по подгруппам класса (шаблоны 1.4.1, factor_groups) — множитель, режимы
reference (премии прежние) и apply (не ниже минимума), незаполненное — в «уточнить», проверка шаблона, части 0305.
Ставки в проверках берутся из справочника копии базы (engine.rate_for / engine.min_rate), а не из головы.
"""
import asyncio
import io
import json as _json
import os
import re
import secrets
import shutil
import sys
import tempfile
import zipfile
from datetime import date, datetime, timedelta
from time import monotonic
from pathlib import Path
from urllib.parse import urlencode
from xml.dom import minidom

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.pop("SURVEYOR_DEV", None)      # guard проверяем целиком, без режима разработчика
os.environ["SURVEYOR_NO_BACKGROUND"] = "1"

import pymupdf                            # noqa: E402

from tmpdb import temp_db                 # noqa: E402
from app import act, act_engine as ae, db, guest, llm   # noqa: E402
from app import act_market as am, act_texts as tx       # noqa: E402
from app.engine import Input, min_rate, premium_of, rate_for   # noqa: E402
from app.main import app                  # noqa: E402

passed, failed = 0, 0
COOKIES = {}
HEADERS = []                              # дополнительные заголовки (Bearer администратора)

CRANE_MUST = {"product_code": "0318", "sum_insured": 2_945_000_000, "object_value": 3_100_000_000,
              "region": "Ташкентская область"}
CRANE_OPT = {"location": "open_area", "losses_3y": {"count": 0, "small_count": 0, "amount": 0}}
CRANE_TYPE = "Спецтехника — автокран"


def ok(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ок  ", name)
    else:
        failed += 1
        print("  ПЛОХО", name, str(extra)[:400])


# ------------------------------------------------------------------ ASGI-клиент

def _send(method, path, params, headers, payload, raw=False):
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "",
             "query_string": urlencode(params or {}, encoding="utf-8").encode(),
             "headers": headers + list(HEADERS), "client": ("203.0.113.9", 0), "server": ("test", 80)}
    out = {"status": None, "chunks": [], "headers": []}

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"], out["headers"] = msg["status"], msg.get("headers") or []
        elif msg["type"] == "http.response.body":
            out["chunks"].append(msg.get("body") or b"")

    asyncio.run(app(scope, receive, send))
    for k, v in out["headers"]:
        if k.lower() == b"set-cookie":
            pair = v.decode("latin-1").split(";")[0]
            name, _, value = pair.partition("=")
            COOKIES[name.strip()] = value.strip()
    body = b"".join(out["chunks"])
    if raw:
        return out["status"], body, dict((k.decode().lower(), v.decode("latin-1")) for k, v in out["headers"])
    text = body.decode("utf-8", "replace")
    try:
        return out["status"], _json.loads(text)
    except ValueError:
        return out["status"], text


def _cookie_hdr():
    return [(b"cookie", "; ".join(f"{k}={v}" for k, v in COOKIES.items()).encode())] if COOKIES else []


def call(method, path, body=None, params=None, raw=False):
    payload = _json.dumps(body, ensure_ascii=False).encode() if body is not None else b""
    hdrs = [(b"host", b"test"), (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode())] + _cookie_hdr()
    return _send(method, path, params, hdrs, payload, raw)


def upload(files, fields=None):
    boundary = "----insonact"
    parts = []
    for k, v in (fields or {}).items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for name, mime, blob in files:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
                     f'filename="{name}"\r\nContent-Type: {mime}\r\n\r\n'.encode() + blob + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    payload = b"".join(parts)
    hdrs = [(b"host", b"test"), (b"content-type", f"multipart/form-data; boundary={boundary}".encode()),
            (b"content-length", str(len(payload)).encode())] + _cookie_hdr()
    return _send("POST", "/act/photos", None, hdrs, payload)


def image(color=(200, 150, 0), kind="png", w=320, h=240) -> bytes:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h), False)
    pix.set_rect(pix.irect, color)
    return pix.tobytes("jpg" if kind == "jpg" else "png")


# ------------------------------------------------------------------ подмена модели

CALLS = []
REPLY = {"text": None}


def fake_chat_raw(purpose, messages, max_tokens=700, temperature=0.2, files=None, timeout=None, retries=None):
    CALLS.append({"purpose": purpose, "messages": messages, "files": files, "temperature": temperature,
                  "timeout": timeout, "retries": retries})
    if REPLY.get("sleep"):
        import time as _t
        _t.sleep(REPLY["sleep"])
    text = REPLY["text"](messages) if callable(REPLY["text"]) else REPLY["text"]
    return {"text": text, "ok": bool(text), "notes": [], "ms": 1, "reason": None if text else "пустой ответ"}


def no_network(*a, **kw):
    raise AssertionError("тест не должен ходить в сеть")


ORIG = {}


def model_on(on=True, files=True):
    llm.enabled = lambda: on
    llm.supports_files = lambda: on and files


CRANE_REPLY = "```json\n" + _json.dumps({
    "files": [{"n": 1, "view": "front"}, {"n": 2, "view": "left"}, {"n": 3, "view": "plate"},
              {"n": 4, "view": "document", "document_kind": "лист технических параметров"}],
    "object_kind": "truck_crane", "class_hint": "special_machinery", "condition": "new",
    "fields": [
        {"key": "object_type", "value": "автокран", "source": "photo", "file": 1},
        {"key": "brand", "value": "XCMG", "source": "marking", "file": 2},
        {"key": "model", "value": "QY50K5D", "source": "marking", "file": 2},
        {"key": "model", "value": "XCMG QY50K5D", "source": "document", "file": 4},
        {"key": "year", "value": "2026", "source": "document", "file": 4},
        {"key": "manufacture_date", "value": "2026-03", "source": "plate", "file": 3},
        {"key": "serial_no", "value": "LXGCPA393TA006921", "source": "plate", "file": 3},
        {"key": "serial_no", "value": "LXGCPA393TA006921", "source": "document", "file": 4},
        {"key": "manufacturer", "value": "Xuzhou Construction Machinery Group Co., Ltd.", "source": "plate",
         "file": 3},
        {"key": "curb_mass", "value": "36 170 кг", "source": "plate", "file": 3},
        {"key": "curb_mass", "value": "38 600 кг", "source": "document", "file": 4},
        {"key": "engine_power", "value": "248 кВт", "source": "plate", "file": 3},
        {"key": "engine_power", "value": "251 кВт", "source": "document", "file": 4},
        {"key": "location", "value": "открытая площадка", "source": "photo", "file": 1},
        {"key": "color", "value": None, "source": "photo", "file": 1},
        {"key": "owner", "value": "что-то чужое", "source": "document", "file": 4},
    ],
    "damages": []}, ensure_ascii=False) + "\n```"

CRANE_FILES = [("front.jpg", "image/jpeg", image(kind="jpg")), ("left.png", "image/png", image((250, 200, 0))),
               ("plate.png", "image/png", image((90, 90, 90))), ("sheet.png", "image/png", image((255, 255, 255)))]


def expected_rate(con, level_adj: float, product="0318", cls="3", otype=CRANE_TYPE):
    """Ручной расчёт по справочнику копии базы: ставка продукта по тарифной политике (если её нет —
    техническая ставка без коэффициентов), поправка по уровню, минимум."""
    ref = db.load_reference(con)
    mr = min_rate(ref, product)
    base = mr.get("company")
    if base is None:
        base = rate_for(ref, Input(product_code=product, class_code=cls, object_type=otype, value_amount=1,
                                   sum_insured=1, factors={}))["gross_pct"]
    floor = mr["floor"]
    applied = round(max(base * (1 + level_adj / 100), floor or 0), 4)
    return round(base, 4), floor, applied


def all_text(a: dict) -> str:
    out = [a["title"], a["footer"]]
    for s in a["sections"]:
        out += [s["title"]] + s["paragraphs"]
        out += [f"{r['label']} {r['value']} {r.get('note') or ''}" for r in s["rows"]]
        for li in s.get("lists") or []:
            out += [li["title"]] + li["items"]
    return "\n".join(out)


# ------------------------------------------------------------------ 1. пример заказчика

def check_crane():
    print("1. Пример заказчика: автокран XCMG QY50K5D")
    REPLY["text"] = CRANE_REPLY
    model_on(True)
    CALLS.clear()
    st, b = upload(CRANE_FILES, {"lang": "ru", "product_code": "0318"})
    ok("фото приняты", st == 200 and b.get("ok"), (st, b))
    ok("гостю выдана cookie", bool(COOKIES.get("gid")))
    ok("один запрос к модели со всеми файлами", len(CALLS) == 1 and len(CALLS[0]["files"]) == 4, len(CALLS))
    ok("низкая температура", CALLS[0]["temperature"] <= 0.2)
    sys_prompt = CALLS[0]["messages"][0]["content"] + CALLS[0]["messages"][1]["content"]
    ok("в инструкции «не выдумывай» и запрет на данные людей",
       "не выдумывай" in sys_prompt and "не извлекай" in sys_prompt and "посторонних машин" in sys_prompt)
    ok("маскировка ПД не портит инструкцию", llm.mask_pd(sys_prompt) == sys_prompt)
    ok("имена файлов в модель не уходят",
       all(f["name"].startswith("file ") for f in CALLS[0]["files"]) and "front.jpg" not in sys_prompt)
    ok("PNG пережат в JPEG перед отправкой", all(f["mime"] == "image/jpeg" for f in CALLS[0]["files"]))
    views = {f["index"]: f["view"] for f in b["files"]}
    ok("ракурсы распознаны (сопоставление по номеру файла в запросе)",
       views == {1: "front", 2: "left", 3: "plate", 4: "document"}, views)
    ok("распознавание — одна попытка с таймаутом 20 с", CALLS[0]["retries"] == 0 and CALLS[0]["timeout"] == 20,
       (CALLS[0]["retries"], CALLS[0]["timeout"]))
    keys = [(r["key"], r["source"]) for r in b["recognized"]]
    ok("оба значения массы вернулись (табличка и документ)",
       ("curb_mass", "plate") in keys and ("curb_mass", "document") in keys, keys)
    ok("пустые и неизвестные поля отброшены",
       not any(k == "color" for k, _ in keys) and not any(k == "owner" for k, _ in keys))
    ok("название изготовителя не принято за ФИО",
       any(r["key"] == "manufacturer" for r in b["recognized"]), keys)
    ok("у каждого значения источник словами и «проверьте»",
       all(r["source_label"] and r["check_label"] == "проверьте" for r in b["recognized"]))
    miss = {v["code"] for v in b["missing_views"]}
    ok("не хватает ракурсов: сзади, правый борт, счётчик", miss == {"back", "right", "odometer"}, miss)
    ok("предупреждение про данные людей и тестовый сервер",
       "данными людей" in b["warning"] and "тестовый" in b["warning"])
    ok("ai = true", b["ai"] is True)
    ok("вид объекта — автокран", (b.get("object_kind") or {}).get("code") == "truck_crane")
    sid = b["session"]

    CALLS.clear()
    st, a = call("POST", "/act/make", {"session": sid, "lang": "ru", "must": CRANE_MUST, "optional": CRANE_OPT,
                                       "recognized": b["recognized"]})
    ok("/act/make к модели не обращается", not CALLS, len(CALLS))
    ok("акт сформирован", st == 200 and a.get("ok"), (st, a))
    ok("пять разделов", [s["n"] for s in a["sections"]] == [1, 2, 3, 4, 5])
    ok("строка о подтверждении андеррайтером",
       a["footer"] == "Акт сформирован ИИ-сюрвейером, подлежит подтверждению андеррайтером")
    ok("шапка акта", a["title"] == "СЮРВЕЙЕРСКИЙ АКТ ПРЕДСТРАХОВОГО ОСМОТРА" and a["number"] and a["date"])
    ok("уровень риска по правилу — низкий (1 повышает, 3 снижают)",
       a["risk"]["level"] == "low" and a["risk"]["up"] == 1 and a["risk"]["down"] == 3, a["risk"])
    ok("признаки с объяснением", len(a["risk"]["factors"]) == 4 and all(f["text"] for f in a["risk"]["factors"]))
    with db.tx() as con:
        base, floor, applied = expected_rate(con, 0)
    ok("базовая ставка = ставка продукта по тарифной политике",
       abs(a["rate"]["base_pct"] - base) < 1e-9, (a["rate"]["base_pct"], base))
    ok("поправка низкого уровня 0 %", a["rate"]["adj_pct"] == 0)
    ok("итоговая ставка совпадает с ручным расчётом", abs(a["rate"]["applied_pct"] - applied) < 1e-9,
       (a["rate"]["applied_pct"], applied))
    ok("минимум продукта из справочника", a["rate"]["min_pct"] == floor, (a["rate"]["min_pct"], floor))
    prem = round(applied / 100 * CRANE_MUST["sum_insured"])
    ok("премия = сумма × ставка", a["premium"]["amount"] == prem, (a["premium"]["amount"], prem))
    ok("калибровка помечена", a["rate"]["calibrated"] == 0 and a["risk"]["calibrated"] == 0)
    ok("тип объекта для ставки — автокран", a["rate"]["object_type"] == CRANE_TYPE, a["rate"]["object_type"])
    ok("версия тарифа сохранена", a["rate"]["tariff_version_id"] is not None)
    ok("сумма ≈95 % стоимости — «в норме»",
       a["value"]["verdict"] == "normal" and abs(a["value"]["ratio_pct"] - 95.0) < 0.01, a["value"])
    ok("текст раздела 3 «В норме»", "В норме" in a["value"]["text"], a["value"]["text"])
    ok("«Франшиза не требуется»", a["franchise"]["needed"] is False and a["franchise"]["text"] ==
       "Франшиза не требуется", a["franchise"])
    codes = {c["code"] for c in a["clauses"]}
    ok("оговорки спецтехники", {"sp_attachments_storage", "sp_territory", "sp_reinspection"} <= codes, codes)
    ok("оговорки помечены экспертными", all(c["expert"] for c in a["clauses"]))
    ok("повторный осмотр через 12 месяцев или 2 000 моточасов",
       any("12 месяцев" in c["text"] and "2 000 моточасов" in c["text"] for c in a["clauses"]))
    dk = {d["key"]: d for d in a["discrepancies"]}
    ok("расхождение по массе и мощности", set(dk) == {"curb_mass", "engine_power"}, list(dk))
    ok("серийный номер и модель (с маркой в документе) не считаются расхождением",
       "serial_no" not in dk and "model" not in dk)
    s5 = all_text({"title": "", "footer": "", "sections": [a["sections"][4]]})
    ok("в заключении: масса 36 170 кг на табличке и 38 600 кг в документе",
       "на табличке 36 170 кг" in s5 and "в документе 38 600 кг" in s5, s5[:600])
    ok("в заключении: мощность 248 кВт и 251 кВт", "248 кВт" in s5 and "251 кВт" in s5)
    ok("приоритет у документа с печатью производителя", "печатью производителя" in s5)
    ok("решение: принять с оговорками", a["decision"]["code"] == "accept_with_clauses", a["decision"])
    ok("андеррайтеру: снять расхождение и дозапросить фото",
       any("Снаряжённая масса" in c for c in a["decision"]["checks"])
       and any("правый борт" in c for c in a["decision"]["checks"]), a["decision"]["checks"])
    s1 = {r["label"]: r for r in a["sections"][0]["rows"]}
    ok("в разделе 1 заводской номер из документа",
       s1["Заводской (серийный) номер"]["value"] == "LXGCPA393TA006921"
       and "из документа" in s1["Заводской (серийный) номер"]["note"], s1.get("Заводской (серийный) номер"))
    ok("в разделе 1 масса из документа и вариант с таблички",
       s1["Снаряжённая масса"]["value"] == "38 600 кг" and "36 170 кг" in s1["Снаряжённая масса"]["note"])
    # 30.09.2026: «данные недоступны» — не больше трёх строк, остальное одной строкой «Не указано: …»
    na_rows = [r for r in a["sections"][0]["rows"] if r["value"] == "данные недоступны"]
    ok("чего нет — «данные недоступны» не больше трёх строк, остальное — «Не указано»",
       len(na_rows) <= 3 and "Цвет" not in s1 and "цвет" in (s1.get("Не указано") or {}).get("value", ""),
       ([r["label"] for r in na_rows], s1.get("Не указано")))
    ok("цвет в списке недоступных данных", "Цвет" in a["missing"], a["missing"])
    text = all_text(a)
    ok("цифры в тексте совпадают с расчётом",
       act.pct(applied, "ru") in text and act.money(prem, "ru") in text)
    return a, sid


# ------------------------------------------------------------------ 2. сумма и стоимость

def check_value():
    print("2. Недострахование и превышение")
    st, a = call("POST", "/act/make", {"lang": "ru", "must": dict(CRANE_MUST, sum_insured=2_000_000_000),
                                       "optional": CRANE_OPT})
    ok("< 90 % — недострахование, ГК ст. 936",
       st == 200 and a["value"]["verdict"] == "under" and a["value"]["legal_ref"] == "ГК РУз, ст. 936"
       and "пропорциональной" in a["value"]["text"], a.get("value"))
    ok("андеррайтеру — про пропорциональную выплату",
       any("пропорциональн" in c for c in a["decision"]["checks"]))
    st, a = call("POST", "/act/make", {"lang": "ru", "must": dict(CRANE_MUST, sum_insured=3_500_000_000),
                                       "optional": CRANE_OPT})
    ok("> 100 % — превышение, ГК ст. 938",
       st == 200 and a["value"]["verdict"] == "over" and a["value"]["legal_ref"] == "ГК РУз, ст. 938"
       and "снизить" in a["value"]["text"], a.get("value"))
    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                       "optional": dict(CRANE_OPT, price_new=3_000_000_000, purchase_year=
                                                        date.today().year - 2)})
    dep = a["value"]["depreciated"]
    ok("ориентир с износом: транспорт 20 % в год", dep and dep["wear_pct_per_year"] == 20
       and dep["value"] == round(3_000_000_000 * (1 - 0.2 * 2)), dep)
    st, a = call("POST", "/act/make", {"lang": "ru", "must": dict(CRANE_MUST, sum_insured="12abc"),
                                       "optional": dict(CRANE_OPT, year=1800)})
    ok("мусор во вводе — 422 с полями", st == 422 and "sum_insured" in a.get("errors", {})
       and "year" in a.get("errors", {}), a)
    with db.tx() as con:
        row = db.rows(con, "SELECT detail FROM audit WHERE action='акт: ошибка ввода' ORDER BY id DESC LIMIT 1")
    ok("ошибка ввода — в журнале, без значений", row and "sum_insured" in row[0]["detail"]
       and "12abc" not in row[0]["detail"], row)


# ------------------------------------------------------------------ 3. франшиза

def check_franchise():
    print("3. Франшиза — только при основании")
    from app.risk_analytics import load_thresholds
    with db.tx() as con:
        th = load_thresholds(con)
    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                       "optional": dict(CRANE_OPT, losses_3y={"count": 2, "small_count": 2})})
    fr = a["franchise"]
    band = th["franchise_by_level"][ae.RA_LEVEL[a["risk"]["level"]]]
    cap = th["franchise_class_caps"].get("3")
    hi = min(band[1], cap) if cap is not None else band[1]
    ok("2 мелких убытка — франшиза советуется", fr["needed"] and any(g["code"] == "fr_g_small_losses"
                                                                     for g in fr["grounds"]), fr)
    ok("размер — вилка из порогов (не выдуман)", fr.get("size") and fr["size"]["to_pct"] == hi
       and fr["size"]["from_pct"] == min(band[0], hi), (fr.get("size"), band, cap))
    ok("размер определяет андеррайтер", "андеррайтер" in fr["text"], fr["text"])
    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                       "optional": dict(CRANE_OPT, want_lower_premium=True)})
    ok("просьба клиента — основание", a["franchise"]["needed"] and
       [g["code"] for g in a["franchise"]["grounds"]] == ["fr_g_client"], a["franchise"])
    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST, "optional": CRANE_OPT})
    ok("без оснований — «Франшиза не требуется»", a["franchise"]["text"] == "Франшиза не требуется")
    # обязательный вид: строительно-монтажные риски, обязательное (0820) — тариф по ПКМ, без поправок
    must = {"product_code": "0820", "sum_insured": 1_000_000_000, "object_value": 1_000_000_000,
            "region": "Ташкент"}
    st, a = call("POST", "/act/make", {"lang": "ru", "must": must,
                                       "optional": {"losses_3y": {"count": 3, "small_count": 3},
                                                    "want_lower_premium": True, "location": "construction"}})
    with db.tx() as con:
        rt = db.rows(con, "SELECT rate_text FROM products WHERE code='0820'")[0]["rate_text"]
    act_rate = float(re.search(r"(\d+(?:,\d+)?)\s*%", rt).group(1).replace(",", "."))
    ok("обязательный вид: ставка по акту без поправок",
       st == 200 and a["rate"]["mode"] == "statutory" and a["rate"]["applied_pct"] == act_rate
       and a["rate"]["adj_pct"] == 0, a.get("rate"))
    ok("обязательный вид: премия по ставке акта",
       a["premium"]["amount"] == round(act_rate / 100 * 1_000_000_000), a["premium"])
    ok("обязательный вид: франшиза не применяется",
       a["franchise"]["needed"] is False and "обязательный вид" in a["franchise"]["text"], a["franchise"])
    must["product_code"] = "1002"            # ОСГО владельцев ТС: в тексте тарифа числа нет
    st, a = call("POST", "/act/make", {"lang": "ru", "must": must, "optional": {}})
    ok("обязательный вид без числа в справочнике — ставка не выдумана",
       st == 200 and a["rate"]["applied_pct"] is None and a["premium"]["amount"] is None, a.get("rate"))


# ------------------------------------------------------------------ 4. продукт без ставки

def check_no_rate():
    print("4. Продукт «по программе» — ставка не определена")
    must = dict(CRANE_MUST, product_code="0321")
    st, a = call("POST", "/act/make", {"lang": "ru", "must": must, "optional": CRANE_OPT})
    ok("режим undefined, цифр нет", st == 200 and a["rate"]["mode"] == "undefined"
       and a["rate"]["applied_pct"] is None and a["rate"]["base_pct"] is None
       and a["premium"]["amount"] is None, a.get("rate"))
    rows = {r["label"]: r["value"] for r in a["sections"][3]["rows"]}
    ok("в акте: «ставка не определена — нужен расчёт андеррайтера»",
       rows.get("Рекомендуемый тариф") == "ставка не определена — нужен расчёт андеррайтера", rows)
    ok("премия — «данные недоступны»", rows.get("Страховая премия") == "данные недоступны")
    ok("андеррайтеру — определить ставку", any("Определить ставку" in c for c in a["decision"]["checks"]))
    ok("не «принять» без оговорок", a["decision"]["code"] != "accept")


# ------------------------------------------------------------------ 5. модель недоступна / мусор

def check_no_model():
    print("5. Модель недоступна или вернула мусор")
    model_on(False)
    CALLS.clear()
    st, b = upload(CRANE_FILES[:2], {"lang": "ru"})
    ok("фото сохранены, ai = false", st == 200 and b["ai"] is False and b["session"], b)
    ok("к модели не обращались", not CALLS)
    ok("честное сообщение", "недоступно" in b["message"], b["message"])
    st, a = call("POST", "/act/make", {"session": b["session"], "lang": "ru", "must": CRANE_MUST,
                                       "optional": CRANE_OPT})
    s2 = all_text({"title": "", "footer": "", "sections": [a["sections"][1]]})
    ok("акт сформирован без модели", st == 200 and len(a["sections"]) == 5)
    ok("раздел осмотра честный: распознать не удалось, осмотр не проводился",
       "не удалось" in s2 and "осмотр не проводился" in s2, s2)
    ok("андеррайтеру — проверить фото вручную", any("вручную" in c for c in a["decision"]["checks"]))

    model_on(True)
    REPLY["text"] = "Извините, я не могу помочь с этим запросом."
    st, b = upload(CRANE_FILES[:1], {"lang": "ru"})
    ok("не-JSON — ai = false и причина", st == 200 and b["ai"] is False and "не по схеме" in b["message"], b)
    REPLY["text"] = '{"files": "не список", "fields": 5}'
    st, b = upload(CRANE_FILES[:1], {"lang": "ru"})
    ok("JSON не по схеме — ai = false", b["ai"] is False, b)
    st, a = call("POST", "/act/make", {"session": b["session"], "lang": "ru", "must": CRANE_MUST,
                                       "optional": CRANE_OPT})
    ok("акт после мусора формируется", st == 200 and a["inspection"]["done"] is False)

    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST, "optional": CRANE_OPT})
    s2 = all_text({"title": "", "footer": "", "sections": [a["sections"][1]]})
    ok("без фото — «осмотр не проводился»", "Осмотр не проводился" in s2 and "осмотр не проводился" in s2, s2)
    ok("без фото — документы не представлены повышают риск",
       any(f["code"] == "f_docs_up" for f in a["risk"]["factors"]))

    st, b = upload([("x.webp", "image/webp", b"RIFF\x00\x00\x00\x00WEBPVP8 ")], {"lang": "ru"})
    ok("WEBP вежливо отклонён", st == 422 and b["rejected"] and "JPG, PNG или PDF" in b["rejected"][0]["error"], b)
    st, b = upload([(f"{i}.png", "image/png", image()) for i in range(11)], {"lang": "ru"})
    ok("больше 10 файлов — 413", st == 413, st)


# ------------------------------------------------------------------ 6. ПД

def check_pd():
    print("6. Персональные данные")
    model_on(True)
    name = "Иванов Иван Иванович"
    REPLY["text"] = _json.dumps({
        "files": [{"n": 1, "view": "document"}],
        "fields": [{"key": "location", "value": name, "source": "document", "file": 1},
                   {"key": "manufacturer", "value": "Петров П. С.", "source": "document", "file": 1},
                   {"key": "serial_no", "value": "AA1234567", "source": "document", "file": 1},
                   {"key": "model", "value": "QY50K5D", "source": "document", "file": 1}],
        "damages": [{"what": "вмятина, владелец Сидоров Сидор", "file": 1}]}, ensure_ascii=False)
    st, b = upload([("Иванов Иван.jpg", "image/jpeg", image(kind="jpg"))], {"lang": "ru"})
    dump = _json.dumps(b, ensure_ascii=False)
    ok("ФИО из ответа модели отброшено", name not in dump and "Петров" not in dump and "Сидоров" not in dump, dump)
    ok("паспорт отброшен", "AA1234567" not in dump)
    ok("остальное сохранилось", any(r["key"] == "model" for r in b["recognized"]))
    ok("имя файла замаскировано", "Иванов" not in b["files"][0]["name"], b["files"])
    st, a = call("POST", "/act/make", {"session": b["session"], "lang": "ru", "must": CRANE_MUST,
                                       "optional": CRANE_OPT,
                                       "recognized": b["recognized"] + [
                                           {"key": "location", "value": "Сидоров Сидор Сидорович",
                                            "source": "input"}]})
    ok("ФИО во вводе сотрудника тоже отброшено", "Сидоров" not in _json.dumps(a, ensure_ascii=False))
    with db.tx() as con:
        rows = db.rows(con, "SELECT result_json, files_json FROM act_uploads WHERE id=?", b["session"])
        audit = db.rows(con, "SELECT who, action, entity, detail FROM audit WHERE action LIKE 'акт%'")
        calls = db.rows(con, "SELECT purpose, error FROM llm_calls")
        acts = db.rows(con, "SELECT act_json FROM acts")
    stored = _json.dumps(rows, ensure_ascii=False) + _json.dumps(acts, ensure_ascii=False)
    ok("в базе загрузок и актов ФИО нет", name not in stored and "Петров" not in stored
       and "Сидоров" not in stored and "Иванов" not in stored)
    journal = _json.dumps(audit, ensure_ascii=False) + _json.dumps(calls, ensure_ascii=False)
    ok("в журнал ФИО не попадает", "Иванов" not in journal and "Сидоров" not in journal and "Петров" not in journal)
    ok("в журнал не попадают распознанные значения", "QY50K5D" not in journal and "LXGCPA" not in journal)
    ok("в журнале есть счётчик отброшенного", any('"dropped_pd"' in (r["detail"] or "") for r in audit))


# ------------------------------------------------------------------ 7. доступ и лимит

def check_access(aid):
    print("7. Чужой акт и гостевой лимит")
    st, a = call("GET", f"/act/{aid}")
    ok("свой акт открывается", st == 200 and a["id"] == aid, st)
    saved = dict(COOKIES)
    COOKIES.clear()
    st, a = call("GET", f"/act/{aid}")
    ok("чужой гость — 404", st == 404, (st, a))
    st, _b, _h = call("GET", f"/act/{aid}.pdf", raw=True)
    ok("чужой PDF — 404", st == 404, st)
    st, _b, _h = call("GET", f"/act/{aid}.docx", raw=True)
    ok("чужой DOCX — 404", st == 404, st)
    # администратор видит любой
    now = datetime.now().isoformat(timespec="seconds")
    token = secrets.token_urlsafe(32)
    with db.tx() as con:
        cur = con.execute("INSERT INTO users (login, full_name, role, branch, password_hash, salt, status, created_at,"
                          " approved_by, approved_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          ("act_test_admin", "Test Admin", "админ", "тест", secrets.token_hex(32),
                           secrets.token_hex(16), "активен", now, "test", now))
        con.execute("INSERT INTO sessions (token, user_id, created_at, expires_at, ip, user_agent) VALUES (?,?,?,?,?,?)",
                    (token, cur.lastrowid, now, (datetime.now() + timedelta(hours=2)).isoformat(timespec="seconds"),
                     "127.0.0.1", "test_act"))
    HEADERS.append((b"authorization", f"Bearer {token}".encode()))
    st, a = call("GET", f"/act/{aid}")
    ok("администратор открывает любой акт", st == 200, st)
    st, s = call("PUT", "/act/settings", {"settings": {"adj_pct": {"moderate": 25}}, "note": "тест"})
    ok("администратор меняет поправки", st == 200 and s["settings"]["adj_pct"]["moderate"] == 25, (st, s))
    st, s = call("PUT", "/act/settings", {"settings": {"adj_pct": {"low": -5}}})
    ok("кривые настройки — 422", st == 422, (st, s))
    HEADERS.clear()
    st, s = call("PUT", "/act/settings", {"settings": {"adj_pct": {"moderate": 30}}})
    ok("гость настройки не меняет", st in (401, 403), st)
    st, s = call("GET", "/act/settings")
    ok("настройки читаются", st == 200 and s["settings"]["adj_pct"]["moderate"] == 25, (st, s))
    with db.tx() as con:
        con.execute("DELETE FROM act_settings")
    COOKIES.clear()
    COOKIES.update(saved)

    guest.reset()
    old = guest.LIMITS["analysis"]
    guest.LIMITS["analysis"] = 2
    try:
        codes = [call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST, "optional": CRANE_OPT})[0]
                 for _ in range(3)]
    finally:
        guest.LIMITS["analysis"] = old
        guest.reset()
    ok("гостевой лимит на акты срабатывает (429)", codes == [200, 200, 429], codes)


# ------------------------------------------------------------------ 8. Word и PDF

def pdf_text(doc) -> str:
    """Текст PDF: извлечение отдаёт пробел как NBSP, а дефис как мягкий перенос — приводим к обычным."""
    raw = "".join(p.get_text() for p in doc).replace(" ", " ").replace("­", "-")
    return re.sub(r"\s+", " ", raw)


RU_TITLES = ["Объект и идентификация", "Результаты осмотра", "Стоимость и страховая сумма",
             "Риск-факторы и франшиза", "Заключение и рекомендация"]


def check_files(aid):
    print("8. Word и PDF")
    st, blob, h = call("GET", f"/act/{aid}.docx", raw=True)
    ok("DOCX отдаётся", st == 200 and "wordprocessingml" in h.get("content-type", ""), (st, h))
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
        names = set(z.namelist())
        xml = z.read("word/document.xml").decode("utf-8")
        minidom.parseString(xml.encode("utf-8"))
        good = {"[Content_Types].xml", "word/document.xml", "word/styles.xml"} <= names
        for n in names:
            if n.endswith(".xml") or n.endswith(".rels"):
                minidom.parseString(z.read(n))
    except Exception as e:
        good, xml = False, str(e)
    ok("DOCX — zip с корректным XML", good, xml[:200])
    plain = re.sub(r"<[^>]+>", "", xml)
    ok("в DOCX пять разделов", all(f"{i}. {tt}" in plain for i, tt in enumerate(RU_TITLES, 1)))
    ok("в DOCX строка о подтверждении андеррайтером (подпись документа с 06.10.2026 — «ИИ-сюрвейером INSON»)",
       "Акт сформирован ИИ-сюрвейером INSON, подлежит подтверждению андеррайтером" in plain)
    ok("в DOCX шапка", "СЮРВЕЙЕРСКИЙ АКТ ПРЕДСТРАХОВОГО ОСМОТРА" in plain)
    st, blob, h = call("GET", f"/act/{aid}.pdf", raw=True)
    ok("PDF отдаётся", st == 200 and h.get("content-type") == "application/pdf", (st, h))
    try:
        doc = pymupdf.open(stream=blob, filetype="pdf")
        text = pdf_text(doc)
        pages = doc.page_count
    except Exception as e:
        text, pages = str(e), 0
    ok("PDF открывается pymupdf", pages >= 1, text[:200])
    ok("в PDF пять разделов", all(f"{i}. {tt}" in text for i, tt in enumerate(RU_TITLES, 1)), text[:300])
    ok("в PDF строка о подтверждении андеррайтером (подпись документа с 06.10.2026 — «ИИ-сюрвейером INSON»)",
       "Акт сформирован ИИ-сюрвейером INSON, подлежит подтверждению андеррайтером" in text)
    st, blob, h = call("GET", f"/act/{aid}.pdf", params={"lang": "uz"}, raw=True)
    doc = pymupdf.open(stream=blob, filetype="pdf")
    text = pdf_text(doc)
    ok("PDF на узбекском", "Koʻrik natijalari" in text or "Ko'rik natijalari" in text, text[:300])


# ------------------------------------------------------------------ 9. три языка

def check_langs(aid):
    print("9. Три языка")
    want = {"ru": RU_TITLES,
            "uz": ["Obyekt va uni identifikatsiya qilish", "Koʻzdan kechirish natijalari", "Qiymat va sugʻurta summasi",
                   "Xavf omillari va franshiza", "Xulosa va tavsiya"],
            "en": ["Object and identification", "Inspection results", "Value and sum insured",
                   "Risk factors and deductible", "Conclusion and recommendation"]}
    for lang, titles in want.items():
        st, a = call("GET", f"/act/{aid}", params={"lang": lang})
        ok(f"{lang}: заголовки разделов", st == 200 and [s["title"] for s in a["sections"]] == titles,
           [s.get("title") for s in a.get("sections") or []])
        if lang != "ru":
            labels = [a["title"], a["footer"]] + [r["label"] for s in a["sections"] for r in s["rows"]] + \
                     [li["title"] for s in a["sections"] for li in s.get("lists") or []] + \
                     [f["text"] for f in a["risk"]["factors"]] + a["decision"]["checks"] + [a["franchise"]["text"]] + \
                     a["rate"]["how"] + [a["value"]["text"], a["risk"]["rule"]] + \
                     [p for s in a["sections"] for p in s["paragraphs"]]
            cyr = [x for x in labels if re.search(r"[А-Яа-яЁё]", x or "")]
            ok(f"{lang}: в подписях и выводах нет кириллицы", not cyr, cyr[:5])
    st, a = call("POST", "/act/make", {"lang": "en", "must": CRANE_MUST, "optional": CRANE_OPT})
    ok("en: сумма в формате UZS", "2,945,000,000 UZS" in all_text(a), all_text(a)[:300])


# ------------------------------------------------------------------ 10. движок: чистые функции

def check_engine():
    print("10. Лёгкий движок (чистые функции)")
    today = date(2026, 9, 29)
    r = ae.risk_level({"inspected": True, "damages": ["вмятина"], "location": "construction", "losses_count": 3,
                       "documents": False, "today": today})
    ok("все четыре признака «повышают» — высокий", r["level"] == "high" and r["up"] == 4, r)
    r = ae.risk_level({"inspected": False, "documents": True, "today": today})
    ok("мало известного — умеренный", r["level"] == "moderate", r)
    r = ae.risk_level({"inspected": True, "year": 2026, "location": "guarded", "losses_count": 0,
                       "documents": True, "today": today})
    ok("всё снижает — низкий", r["level"] == "low" and r["down"] == 4, r)
    r = ae.risk_level({"inspected": True, "year": 2015, "location": "open_area", "guard": True,
                       "losses_count": 1, "documents": True, "today": today})
    ok("открытая площадка под охраной и один убыток — не влияют", r["up"] == 0 and r["down"] == 1, r)
    r = ae.risk_level({"inspected": True, "documents": True, "today": today})
    ok("год неизвестен — не пишем «не новый»", r["factors"][0]["code"] == "f_cond_no_year", r["factors"][0])
    d = ae.discrepancies([{"key": "curb_mass", "value": "36 170 кг", "source": "plate"},
                          {"key": "curb_mass", "value": "36,17 т", "source": "document"}])
    ok("масса в тоннах и килограммах — одно и то же", not d, d)
    d = ae.discrepancies([{"key": "serial_no", "value": "LXGCPA393TA006921", "source": "plate"},
                          {"key": "serial_no", "value": "LXGCPA393TA006927", "source": "document"}])
    ok("номер отличается одним знаком — расхождение", len(d) == 1 and d[0]["priority"] == "document", d)
    d = ae.discrepancies([{"key": "year", "value": "2025", "source": "document"}], {"year": 2026})
    ok("год во вводе и в документе — расхождение", len(d) == 1 and d[0]["key"] == "year", d)
    d = ae.discrepancies([{"key": "model", "value": "QY50K5D", "source": "document"},
                          {"key": "model", "value": "QY50K", "source": "marking"}])
    ok("модель на стреле и в документе — расхождение", len(d) == 1, d)
    ok("ракурсы здания", ae.required_views("property") == ["facade", "roof", "interior", "electrical",
                                                           "fire_safety"])
    ok("группа по классу и типу", ae.object_group("3", "Спецтехника — автокран") == "special"
       and ae.object_group("8", "Склад") == "property" and ae.object_group("7") == "cargo")
    ok("ошибки настроек ловятся", ae.check_settings({"level_rule": {"low_max_net": 3, "high_min_net": 1}}))
    ok("верные настройки проходят", not ae.check_settings({"adj_pct": {"moderate": 25}}))
    ok("число из «38,600 kg» и «2 945 000 000»", ae.to_number("38,600 kg") == 38600
       and ae.to_number("2 945 000 000") == 2945000000)


# ------------------------------------------------------------------ 11. связка текста моделью

def check_polish():
    print("11. Литературная связка моделью убрана: решение и списки — только из шаблонов")
    model_on(True)
    REPLY["text"] = "Объект осмотрен, выявлено 999 замечаний. Рекомендация: отказать."
    CALLS.clear()
    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST, "optional": CRANE_OPT, "polish": True})
    ok("polish в теле игнорируется: к модели не обращались", st == 200 and not CALLS, len(CALLS))
    ok("ни один раздел не переписан моделью",
       not any(s.get("polished") for s in a["sections"]) and "999" not in all_text(a))
    ok("решение — из шаблона", a["decision"]["text"] == act.t("d_" + a["decision"]["code"], "ru"), a["decision"])
    ok("функции polish больше нет", not hasattr(act, "polish"))
    model_on(False)


# ------------------------------------------------------------------ 12. хранение

def check_cleanup(sid, aid):
    print("12. Хранение: фото 24 часа, акт 7 дней")
    with db.tx() as con:
        up = db.rows(con, "SELECT created_at, expires_at, files_json FROM act_uploads WHERE id=?", sid)[0]
        ac = db.rows(con, "SELECT created_at, expires_at FROM acts WHERE id=?", aid)[0]
    h = (datetime.fromisoformat(up["expires_at"]) - datetime.fromisoformat(up["created_at"])).total_seconds() / 3600
    d = (datetime.fromisoformat(ac["expires_at"]) - datetime.fromisoformat(ac["created_at"])).days
    ok("фото — 24 часа", h == 24, h)
    ok("акт — 7 дней", d == 7, d)
    files = _json.loads(up["files_json"])
    ok("в базе у файла нет имени — только номер, формат и путь",
       all("name" not in f and "orig_name" not in f and f.get("index") and f.get("fmt") for f in files)
       and "front.jpg" not in up["files_json"], files[:1])
    ok("путь файла — через db.stored_path", all(f["path"] == db.stored_path(act.DIR / sid / Path(f["path"]).name)
                                                for f in files), files[:1])
    ok("файлы на диске", (act.DIR / sid).is_dir())
    with db.tx() as con:
        con.execute("UPDATE act_uploads SET expires_at='2020-01-01T00:00:00' WHERE id=?", (sid,))
        con.execute("UPDATE acts SET expires_at='2020-01-01T00:00:00' WHERE id=?", (aid,))
    removed = act.cleanup()
    ok("просроченное удаляется", removed >= 2, removed)
    ok("папка с фото удалена", not (act.DIR / sid).exists())
    st, a = call("GET", f"/act/{aid}")
    ok("просроченный акт не открывается", st == 404, st)


# ------------------------------------------------------------------ 13–20. замечания контролёра 29.09.2026

def set_limits(**kw):
    """Версия настроек акта с другими пределами (как правка администратора), без API."""
    with db.tx() as con:
        con.execute("INSERT INTO act_settings (created_at, created_by, settings_json, calibrated, note) "
                    "VALUES (?,?,?,?,?)", (db.now(), "тест", _json.dumps({"limits": kw}), 0, "тест"))


def clear_settings():
    with db.tx() as con:
        con.execute("DELETE FROM act_settings")


def png_bomb(w=14000, h=14000) -> bytes:
    """Настоящая «бомба»: PNG 14000×14000 оттенков серого из нулей — сотни килобайт в файле, ~200 МБ в памяти."""
    import struct
    import zlib

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    comp = zlib.compressobj(9)
    row = b"\x00" * (w + 1)
    parts = [comp.compress(row * 500) for _ in range(h // 500)] + [comp.flush()]
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", b"".join(parts)) + chunk(b"IEND", b""))


def jpeg_claiming(w, h) -> bytes:
    """JPEG, у которого в SOF0 записаны огромные размеры (данные кадра — мусор)."""
    import struct
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, h, w, 3) + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8" + app0 + sof + b"\xff\xda\x00\x02" + b"\x00" * 64 + b"\xff\xd9"


def pdf_pages(n) -> bytes:
    doc = pymupdf.open()
    for _ in range(n):
        doc.new_page()
    return doc.tobytes()


def noise(w, h, kind="png") -> bytes:
    pix = pymupdf.Pixmap(pymupdf.csRGB, w, h, os.urandom(w * h * 3), False)
    return pix.tobytes("jpg" if kind == "jpg" else "png")


def check_bomb():
    print("13. Картинка-бомба и PDF: размеры из заголовка, без раскрытия")
    model_on(True)
    REPLY["text"] = CRANE_REPLY
    act.reset_limits()
    bomb = png_bomb()
    opened = []
    orig_pix = pymupdf.Pixmap

    class spy(orig_pix):                     # подкласс: внутренние проверки isinstance в pymupdf не ломаются
        def __init__(self, *a, **kw):
            opened.append(a[:1] if a and isinstance(a[0], (bytes, bytearray)) else "other")
            super().__init__(*a, **kw)
    ok("бомба маленькая на диске", len(bomb) < 1024 * 1024, len(bomb))
    ok("размеры PNG читаются из IHDR", act.image_size(bomb, "png") == (14000, 14000))
    ok("размеры JPEG — из SOF", act.image_size(image(kind="jpg", w=321, h=123), "jpg") == (321, 123))
    ok("размеры JPEG с мусором в SOF", act.image_size(jpeg_claiming(12000, 9000), "jpg") == (12000, 9000))
    ok("битый заголовок — размеров нет", act.image_size(b"\x89PNG\r\n\x1a\n" + b"\x00" * 30, "png") is None
       and act.image_size(b"\xff\xd8\xff\xda\x00\x02", "jpg") is None)
    pymupdf.Pixmap = spy
    try:
        CALLS.clear()
        st, b = upload([("bomb.png", "image/png", bomb), ("big.jpg", "image/jpeg", jpeg_claiming(12000, 9000)),
                        ("broken.png", "image/png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64),
                        ("ok.png", "image/png", image())], {"lang": "ru"})
    finally:
        pymupdf.Pixmap = orig_pix
    rej = {r["index"]: r["error"] for r in b.get("rejected") or []}
    ok("бомба 14000×14000 отклонена с понятным сообщением",
       st == 200 and "14000×14000" in rej.get(1, "") and "50 Мп" in rej.get(1, ""), rej)
    ok("JPEG 12000×9000 (108 Мп) отклонён", "Мп" in rej.get(2, ""), rej)
    ok("размеры не прочитались — отклонено", "размеры" in rej.get(3, ""), rej)
    ok("нормальный снимок принят (index 4)", [f["index"] for f in b["files"]] == [4], b.get("files"))
    ok("отклонённые картинки не раскрывались (Pixmap только для принятой)",
       len([x for x in opened if x != "other"]) == 1, len(opened))
    ok("к модели ушёл один файл", len(CALLS) == 1 and len(CALLS[0]["files"]) == 1)
    st, b = upload([("many.pdf", "application/pdf", pdf_pages(11)), ("one.pdf", "application/pdf", pdf_pages(1))],
                   {"lang": "ru"})
    rej = {r["index"]: r["error"] for r in b.get("rejected") or []}
    ok("PDF больше 10 страниц отклонён", "10 страниц" in rej.get(1, ""), rej)
    ok("PDF в 1 страницу принят и не растеризуется (уходит модели как PDF)",
       [f["index"] for f in b["files"]] == [2] and CALLS[-1]["files"][0]["mime"] == "application/pdf")
    set_limits(max_image_mp=0.05)             # 320×240 = 0,077 Мп — теперь больше предела
    try:
        st, b = upload([("ok.png", "image/png", image())], {"lang": "ru"})
        ok("предел мегапикселей — из настроек", st == 422 and "Мп" in b["rejected"][0]["error"], b)
    finally:
        clear_settings()


async def _asend(method, path, headers, payload, out):
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "", "query_string": b"",
             "headers": headers, "client": ("203.0.113.9", 0), "server": ("test", 80)}
    res = {"status": None}

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            res["status"] = msg["status"]
    await app(scope, receive, send)
    import time as _t
    out.append((path, res["status"], _t.monotonic()))


def multipart(files, fields=None):
    boundary = "----insonact"
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
             for k, v in (fields or {}).items()]
    for name, mime, blob in files:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
                     f'filename="{name}"\r\nContent-Type: {mime}\r\n\r\n'.encode() + blob + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return boundary, b"".join(parts)


def check_threadpool():
    print("14. Распознавание не останавливает сервер; срок 25 с; одна попытка")
    import inspect
    import time as _t
    from app import act as act_mod
    eps = {}
    for r in act_mod.router.routes:
        eps[(r.path, tuple(sorted(r.methods)))] = r.endpoint
    heavy = [eps[("/act/photos", ("POST",))], eps[("/act/make", ("POST",))], eps[("/act/{aid}.docx", ("GET",))],
             eps[("/act/{aid}.pdf", ("GET",))], eps[("/act/{aid}/send", ("POST",))]]
    ok("обработчики фото, акта, выгрузки и отправки — обычные def (пул потоков)",
       not any(inspect.iscoroutinefunction(f) for f in heavy), [f.__name__ for f in heavy
                                                                if inspect.iscoroutinefunction(f)])
    # пока идёт медленное распознавание (1,5 с), другой запрос отвечает сразу
    model_on(True)
    REPLY["text"], REPLY["sleep"] = CRANE_REPLY, 1.5
    act.reset_limits()
    boundary, payload = multipart(CRANE_FILES[:1], {"lang": "ru"})
    up_hdrs = [(b"host", b"test"), (b"content-type", f"multipart/form-data; boundary={boundary}".encode()),
               (b"content-length", str(len(payload)).encode())] + _cookie_hdr()
    get_hdrs = [(b"host", b"test")] + _cookie_hdr()
    done = []

    async def both():
        import asyncio as _a
        t0 = _t.monotonic()
        up = _a.ensure_future(_asend("POST", "/act/photos", up_hdrs, payload, done))
        await _a.sleep(0.2)
        await _asend("GET", "/act/settings", get_hdrs, b"", done)
        await up
        return t0
    try:
        t0 = asyncio.run(both())
    finally:
        REPLY["sleep"] = 0
    order = [p for p, _s, _ in done]
    ok("GET /act/settings ответил раньше, чем закончилось распознавание",
       order == ["/act/settings", "/act/photos"] and done[0][2] - t0 < 1.2, [(p, round(x - t0, 2)) for p, _s, x in done])
    ok("загрузка при этом прошла", done[-1][1] == 200, done)
    # общий срок: модель «висит» дольше срока — ai=false, честная причина
    REPLY["sleep"] = 1.5
    try:
        t1 = _t.monotonic()
        rec = act.recognize([{"blob": image(), "fmt": "png"}], "ru", {"ai_deadline_sec": 0.5})
        spent = _t.monotonic() - t1
    finally:
        REPLY["sleep"] = 0
    ok("не уложились в срок — ai=false и сообщение", rec["ok"] is False and "не ответила" in rec["reason"], rec)
    ok("ответ не ждёт модель дольше срока", spent < 1.2, spent)
    # сервер-подобный путь: модель падает — фото сохранены, акт формируется
    def boom(*a, **kw):
        raise RuntimeError("сеть")
    fake_raw = llm.chat_raw
    llm.chat_raw = boom
    try:
        st, b = upload(CRANE_FILES[:2], {"lang": "ru"})
    finally:
        llm.chat_raw = fake_raw
    ok("ошибка модели — фото сохранены, ai=false", st == 200 and b["ai"] is False and b["session"]
       and (act.DIR / b["session"]).is_dir(), b)
    st, a = call("POST", "/act/make", {"session": b["session"], "lang": "ru", "must": CRANE_MUST,
                                       "optional": CRANE_OPT})
    ok("акт после сбоя модели формируется", st == 200 and a["inspection"]["done"] is False, st)
    # llm.chat_raw: retries=0 — ровно один запрос к сети, таймаут передан
    posts = []

    def fake_post(url, body, headers, timeout=None):
        posts.append(timeout)
        raise TimeoutError("медленно")
    old = (llm._post, llm.enabled, llm.provider)
    llm._post, llm.enabled, llm.provider = fake_post, (lambda: True), (lambda: "openai")
    try:
        res = ORIG["chat_raw"]("тест", [{"role": "user", "content": "x"}], timeout=20, retries=0)
    finally:
        llm._post, llm.enabled, llm.provider = old
    ok("llm.chat_raw(retries=0): одна попытка, таймаут 20 с", posts == [20] and res["text"] is None, posts)


def check_body_limit():
    print("15. Размер тела /act/photos — по Content-Length до чтения")
    model_on(True)
    REPLY["text"] = CRANE_REPLY
    CALLS.clear()
    before = set(p.name for p in act.DIR.iterdir()) if act.DIR.exists() else set()
    boundary, payload = multipart(CRANE_FILES[:1], {"lang": "ru"})
    read = []

    async def run(headers):
        out = {}

        async def receive():
            read.append(1)
            return {"type": "http.request", "body": payload, "more_body": False}

        async def send(msg):
            if msg["type"] == "http.response.start":
                out["status"] = msg["status"]
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                 "scheme": "http", "path": "/act/photos", "raw_path": b"/act/photos", "root_path": "",
                 "query_string": b"", "headers": headers, "client": ("203.0.113.9", 0), "server": ("test", 80)}
        await app(scope, receive, send)
        return out.get("status")
    ctype = (b"content-type", f"multipart/form-data; boundary={boundary}".encode())
    st = asyncio.run(run([(b"host", b"test"), ctype, (b"content-length", str(act.MAX_BODY + 1).encode())]
                         + _cookie_hdr()))
    ok("больше 10 × 15 МБ + запас — 413", st == 413, st)
    ok("тело при этом не читалось", not read, len(read))
    st = asyncio.run(run([(b"host", b"test"), ctype] + _cookie_hdr()))
    ok("без Content-Length — 411", st == 411, st)
    after = set(p.name for p in act.DIR.iterdir()) if act.DIR.exists() else set()
    ok("ни файлов, ни обращений к модели", after == before and not CALLS)
    st = asyncio.run(run([(b"host", b"test"), ctype, (b"content-length", str(len(payload)).encode())]
                         + _cookie_hdr()))
    ok("обычный размер проходит", st == 200, st)


def check_limits():
    print("16. Лимиты: фото гостя по числу файлов, распознавания на сервер")
    model_on(True)
    REPLY["text"] = CRANE_REPLY
    act.reset_limits()
    guest.reset()
    set_limits(guest_photos_per_hour=3)
    try:
        codes = [upload(CRANE_FILES[:2], {"lang": "ru"})[0], upload(CRANE_FILES[:2], {"lang": "ru"}),
                 upload(CRANE_FILES[:1], {"lang": "ru"})[0]]
    finally:
        clear_settings()
        act.reset_limits()
    st2, b2 = codes[1]
    ok("2 фото + 2 фото при пределе 3 — второй запрос 429, третий (1 фото) проходит",
       [codes[0], st2, codes[2]] == [200, 429, 200], [codes[0], st2, codes[2]])
    ok("429 с понятным сообщением и Retry-After", "фото в час" in b2.get("detail", "") and b2.get("limit") == 3
       and b2.get("retry_after_sec"), b2)
    ok("по умолчанию 60 фото в час", ae.DEFAULT_SETTINGS["limits"]["guest_photos_per_hour"] == 60
       and ae.DEFAULT_SETTINGS["limits"]["ai_calls_per_hour"] == 120)
    # guard больше не считает /act/photos запросами (иначе 30 запросов в час перекрыли бы счёт по фото)
    from app import guard
    ok("guard не считает /act/photos запросами", ("POST", "/act/photos") not in guard.GUEST_BUCKET)
    set_limits(ai_calls_per_hour=1)
    CALLS.clear()
    try:
        _s1, b1 = upload(CRANE_FILES[:1], {"lang": "ru"})
        _s2, b2 = upload(CRANE_FILES[:1], {"lang": "ru"})
    finally:
        clear_settings()
        act.reset_limits()
    ok("общий предел распознаваний: второй раз ai=false с честным сообщением",
       b1["ai"] is True and b2["ai"] is False and "лимит" in b2["message"] and len(CALLS) == 1, (b2["message"],
                                                                                                  len(CALLS)))
    ok("фото при этом сохранены", _s2 == 200 and b2["session"])
    ok("кривые пределы в настройках ловятся",
       ae.check_settings({"limits": {"max_image_mp": 0}}) and ae.check_settings({"limits": {"x": 1}})
       and not ae.check_settings({"limits": {"max_image_mp": 40}}))


def check_budget():
    print("17. Вложения в модель — не больше 10 МБ (настройка), о непрочитанном — честно")
    model_on(True)
    REPLY["text"] = _json.dumps({"files": [], "fields": [], "damages": []})
    act.reset_limits()
    big = [noise(1200, 1200, "png") for _ in range(3)]
    set_limits(ai_max_mb=1)
    CALLS.clear()
    try:
        st, b = upload([("a.png", "image/png", big[0]), ("b.png", "image/png", big[1]),
                        ("c.png", "image/png", big[2]), ("d.pdf", "application/pdf", pdf_pages(1))], {"lang": "ru"})
    finally:
        clear_settings()
    sent = CALLS[0]["files"] if CALLS else []
    total = sum(len(f["data"]) for f in sent)
    ok("суммарно в модель не больше предела", st == 200 and 0 < total <= 1024 * 1024, total)
    ok("PDF (документ) — в приоритете", any(f["mime"] == "application/pdf" for f in sent))
    ok("непрочитанные перечислены номерами", b.get("not_sent") and b["notes"]
       and all(str(n) in b["notes"][0] for n in b["not_sent"]), (b.get("not_sent"), b.get("notes")))
    ok("у непрочитанных read_by_ai = false",
       all(not f["read_by_ai"] for f in b["files"] if f["index"] in b["not_sent"]))
    ok("по умолчанию предел 10 МБ", ae.DEFAULT_SETTINGS["limits"]["ai_max_mb"] == 10)
    # сильнее пережать: 4 шума 2000×2000 не влезают в 10 МБ при обычном сжатии, но влезают после второго шага
    many = [{"blob": noise(2000, 2000, "jpg"), "fmt": "jpg"} for _ in range(4)]
    first = sum(len(act._for_model(f["blob"], "jpg")[0]) for f in many)
    payload, sent_i, left = act.pick_for_model(many, 10 * 1024 * 1024)
    ok("не поместились — пережаты сильнее, а не выброшены" if first > 10 * 1024 * 1024 else
       "поместились при обычном сжатии", len(sent_i) == 4 and not left
       and sum(len(p["data"]) for p in payload) <= 10 * 1024 * 1024, (first, [len(p["data"]) for p in payload]))


def check_sources():
    print("18. Источник значения нельзя подменить")
    model_on(True)
    REPLY["text"] = CRANE_REPLY
    act.reset_limits()
    st, b = upload(CRANE_FILES, {"lang": "ru", "product_code": "0318"})
    sid = b["session"]
    fake = [{"key": "serial_no", "value": "FAKE0000000000001", "source": "document", "file": "f4"}]
    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST, "optional": CRANE_OPT,
                                       "recognized": fake})
    rec = {(r["key"], r["value"]): r for r in a["recognized"]}
    ok("без загрузки «из документа» превращается во «введено сотрудником»",
       rec[("serial_no", "FAKE0000000000001")]["source"] == "input", a["recognized"])
    s1 = {r["label"]: r for r in a["sections"][0]["rows"]}
    ok("в разделе 1 — «введено сотрудником»", "введено сотрудником" in (s1["Заводской (серийный) номер"]["note"]
                                                                         or ""), s1["Заводской (серийный) номер"])
    edited = [dict(r) for r in b["recognized"]]
    for r in edited:
        if r["key"] == "serial_no" and r["source"] == "document":
            r["value"] = "LXGCPA393TA000000"            # сотрудник исправил значение, источник оставил
    st, a = call("POST", "/act/make", {"session": sid, "lang": "ru", "must": CRANE_MUST, "optional": CRANE_OPT,
                                       "recognized": edited + fake})
    rec = {(r["key"], r["value"]): r["source"] for r in a["recognized"]}
    ok("изменённое значение — input", rec.get(("serial_no", "LXGCPA393TA000000")) == "input", rec)
    ok("чужое значение с source=document в живой сессии — input",
       rec.get(("serial_no", "FAKE0000000000001")) == "input", rec)
    ok("неизменённые значения сохраняют источник", rec.get(("serial_no", "LXGCPA393TA006921")) == "plate"
       and rec.get(("curb_mass", "38 600 кг")) == "document", rec)
    ok("расхождение по номеру не выдумано из подмены",
       not any(d["key"] == "serial_no" and any("document" in g["sources"] and "FAKE" in g["value"]
                                               for g in d["values"]) for d in a["discrepancies"]), a["discrepancies"])
    with db.tx() as con:
        row = db.rows(con, "SELECT detail FROM audit WHERE action='акт сформирован' ORDER BY id DESC LIMIT 1")
    ok("в журнале — счётчик понижённых источников", '"sources_downgraded": 2' in row[0]["detail"], row)
    saved = dict(COOKIES)
    COOKIES.clear()
    st, a = call("POST", "/act/make", {"session": sid, "lang": "ru", "must": CRANE_MUST, "optional": CRANE_OPT,
                                       "recognized": b["recognized"]})
    COOKIES.clear()
    COOKIES.update(saved)
    ok("чужая сессия — все источники input", st == 200 and all(r["source"] == "input" for r in a["recognized"]),
       {r["source"] for r in a["recognized"]})


def init_data(tg_id: int, token: str, auth_date=None) -> str:
    import hashlib
    import hmac
    import time as _t
    from urllib.parse import urlencode as _ue
    pairs = [("auth_date", str(int(auth_date or _t.time()))), ("query_id", "AAHtest"),
             ("user", _json.dumps({"id": tg_id, "first_name": "Test"}, separators=(",", ":")))]
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(pairs))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return _ue(pairs + [("hash", h)])


def check_send(aid):
    print("19. Отправка акта ботом: POST /act/{id}/send")
    from app import telegram, tgbot
    token = "123456:TEST-token-не-для-журнала"
    sent = []
    reply = {"ok": True, "result": {"message_id": 5}}

    def fake_file(method, fields, field, filename, blob, mime):
        sent.append({"method": method, "chat_id": fields.get("chat_id"), "field": field, "filename": filename,
                     "head": blob[:4], "mime": mime, "caption": fields.get("caption")})
        return dict(reply)
    old = (tgbot.bot_token, telegram.bot_token, tgbot._deliver_file, tgbot._deliver)
    tgbot.bot_token = telegram.bot_token = lambda: token
    tgbot._deliver_file = fake_file
    tgbot._deliver = no_network
    act.reset_limits()
    try:
        good = init_data(777000111, token)
        st, r = call("POST", f"/act/{aid}/send", {"format": "pdf"})
        ok("гость без Telegram — 409 с понятным сообщением",
           st == 409 and r.get("code") == "no_telegram" and "Telegram" in r["detail"]
           and "компьютере" in r["detail"], (st, r))
        st, r = call("POST", f"/act/{aid}/send", {"format": "pdf", "initData": init_data(777000111, "чужой:токен")})
        ok("поддельный initData — 403", st == 403 and r.get("code") == "bad_init_data", (st, r))
        st, r = call("POST", f"/act/{aid}/send", {"format": "xls", "initData": good})
        ok("неизвестный формат — 422", st == 422, (st, r))
        st, r = call("POST", f"/act/{aid}/send", {"format": "pdf", "lang": "uz", "initData": good})
        ok("PDF отправлен: 200 и sendDocument этому пользователю",
           st == 200 and r.get("sent") and sent and sent[-1]["method"] == "sendDocument"
           and sent[-1]["chat_id"] == "777000111" and sent[-1]["head"] == b"%PDF"
           and sent[-1]["filename"].endswith(".pdf"), (st, r, sent[-1:]))
        ok("язык файла — из запроса", r.get("lang") == "uz" and "Syurveyer" in (sent[-1]["caption"] or ""), r)
        st, r = call("POST", f"/act/{aid}/send", {"format": "docx", "initData": good})
        ok("DOCX отправлен документом", st == 200 and sent[-1]["head"][:2] == b"PK"
           and sent[-1]["filename"].endswith(".docx") and sent[-1]["method"] == "sendDocument", (st, sent[-1:]))
        saved = dict(COOKIES)
        COOKIES.clear()
        n_before = len(sent)
        st, r = call("POST", f"/act/{aid}/send", {"format": "pdf", "initData": good})
        COOKIES.clear()
        COOKIES.update(saved)
        ok("не владелец — 404 и ничего не отправлено", st == 404 and len(sent) == n_before, (st, r))
        reply.update(ok=False, description="Forbidden: bot can't initiate conversation with a user")
        st, r = call("POST", f"/act/{aid}/send", {"format": "pdf", "initData": good})
        ok("бот не может написать первым — 502 и подсказка нажать Start",
           st == 502 and r.get("code") == "start_bot" and "Start" in r["detail"], (st, r))
        reply.clear()
        reply.update(ok=True, result={"message_id": 6})
        codes = [call("POST", f"/act/{aid}/send", {"format": "pdf", "initData": good})[0] for _ in range(8)]
        ok("лимит: 10 отправок в час, 11-я — 429", codes[:7] == [200] * 7 and codes[7] == 429, codes)
        tgbot.bot_token = telegram.bot_token = lambda: ""
        act.reset_limits()
        st, r = call("POST", f"/act/{aid}/send", {"format": "pdf", "initData": good})
        ok("бот не подключён — 503", st == 503 and r.get("code") == "bot_off", (st, r))
        with db.tx() as con:
            dump = _json.dumps(db.rows(con, "SELECT * FROM audit WHERE action LIKE 'акт%'"), ensure_ascii=False) + \
                _json.dumps(db.rows(con, "SELECT * FROM tg_messages"), ensure_ascii=False)
        ok("токен бота не попал ни в журнал, ни в журнал бота", "TEST-token" not in dump)
    finally:
        tgbot.bot_token, telegram.bot_token, tgbot._deliver_file, tgbot._deliver = old
        act.reset_limits()


def check_misc():
    print("20. Мелочи: ФИО и номера, регион, несколько классов, папки-сироты")
    pl = act.pd_like
    ok("изготовитель — название компании целиком: не ФИО",
       not pl("manufacturer", "Xuzhou Construction Machinery Group Co., Ltd.")
       and not pl("manufacturer", "XUZHOU CONSTRICTION MACHINERY GROUP IMPORT & EXPORT CO., LTD (XCMG)"))
    ok("компания + ФИО вне названия — ФИО", pl("manufacturer", "Xuzhou Machinery Group Co., Ltd., Ivan Petrov")
       and pl("manufacturer", "ООО «Техника», директор Иванов Иван Иванович")
       and pl("brand", "Иванов И. И. Group"))
    ok("9 цифр без букв в номере — отброшено (возможный ИНН/телефон)", pl("serial_no", "123456789")
       and pl("engine_no", "301 234 567".replace(" ", "")))
    ok("номер с буквами — проходит", not pl("engine_no", "D123456789") and not pl("serial_no", "A123456789"))
    ok("лист параметров заказчика: «Паспорт № 20102600523» и «D9264002842» проходят",
       not pl("serial_no", "Паспорт № 20102600523") and not pl("engine_no", "D9264002842")
       and not pl("engine_model", "SC9DF340Q6") and not pl("serial_no", "LXGCPA393TA006921"))
    # регион кодом и названием
    st, a = call("POST", "/act/make", {"lang": "ru", "must": dict(CRANE_MUST, region="tashkent_region"),
                                       "optional": CRANE_OPT})
    reg = {r["label"]: r["value"] for r in a["sections"][0]["rows"]}.get("Регион")
    ok("регион кодом печатается по-русски", reg == "Ташкентская область", reg)
    st, u = call("GET", f"/act/{a['id']}", params={"lang": "uz"})
    reg_uz = [r["value"] for r in u["sections"][0]["rows"]][-1] if u.get("sections") else None
    ok("тот же акт на узбекском — регион по-узбекски",
       any(r["value"] == "Toshkent viloyati" for r in u["sections"][0]["rows"]), reg_uz)
    st, a = call("POST", "/act/make", {"lang": "en", "must": dict(CRANE_MUST, region="Ташкентская область"),
                                       "optional": CRANE_OPT})
    ok("каноническое название → перевод на язык акта",
       any(r["value"] == "Tashkent region" for r in a["sections"][0]["rows"]))
    st, a = call("POST", "/act/make", {"lang": "en", "must": dict(CRANE_MUST, region="Чирчик, промзона"),
                                       "optional": CRANE_OPT})
    ok("незнакомый регион — как ввели", any(r["value"] == "Чирчик, промзона" for r in a["sections"][0]["rows"]))
    # продукт с несколькими классами
    with db.tx() as con:
        multi = db.rows(con, "SELECT pc.product_code AS code FROM product_classes pc JOIN products p "
                             "ON p.code = pc.product_code WHERE p.pricing_mode NOT IN ('по программе') "
                             "GROUP BY pc.product_code HAVING COUNT(DISTINCT pc.class_code) > 1 LIMIT 1")
    if multi:
        st, a = call("POST", "/act/make", {"lang": "ru", "must": dict(CRANE_MUST, product_code=multi[0]["code"]),
                                           "optional": CRANE_OPT})
        text = all_text(a) if st == 200 else str(a)
        ok(f"продукт {multi[0]['code']} с несколькими классами — пометка в акте и разбор по частям (30.09.2026)",
           st == 200 and a["rate"]["multi_class"] and "нескольким классам" in text
           and "договор разобран по частям" in text and a["parts"]["mode"] == "multi", text[:300])
    else:
        ok("в справочнике нашёлся продукт с несколькими классами", False)
    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST, "optional": CRANE_OPT})
    ok("у продукта с одним классом пометки нет", not a["rate"]["multi_class"]
       and "нескольким классам" not in all_text(a))
    # папка-сирота: запись в базу упала — папки нет
    model_on(False)
    act.reset_limits()
    before = set(p.name for p in act.DIR.iterdir()) if act.DIR.exists() else set()
    orig_cleanup = act.cleanup

    def broken(con=None):
        raise RuntimeError("база недоступна")
    act.cleanup = broken
    try:
        try:
            st, _b = upload(CRANE_FILES[:1], {"lang": "ru"})
        except Exception as e:                     # ASGI-приложение пробрасывает исключение после 500
            st = type(e).__name__
    finally:
        act.cleanup = orig_cleanup
    after = set(p.name for p in act.DIR.iterdir()) if act.DIR.exists() else set()
    ok("запись в базу упала — папка с фото удалена", after == before, (st, after - before))
    old_dir = act.DIR / "deadbeefdeadbeefdeadbeef"
    new_dir = act.DIR / "cafecafecafecafecafecafe"
    old_dir.mkdir(parents=True, exist_ok=True)
    new_dir.mkdir(parents=True, exist_ok=True)
    (old_dir / "f1.png").write_bytes(image())
    past = datetime.now().timestamp() - 3 * 3600
    os.utime(old_dir, (past, past))
    act.cleanup()
    ok("очистка: старая папка без записи удалена, свежая оставлена", not old_dir.exists() and new_dir.exists())
    shutil.rmtree(new_dir, ignore_errors=True)


# ------------------------------------------------------------------ 21–27. дополнения 29.09.2026

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CONTRACT = Path(__file__).resolve().parent.parent / "sandbox" / "flow150_contract.docx"
WH_MUST = {"product_code": "0808", "sum_insured": 1_000_000_000, "object_value": 1_200_000_000,
           "region": "Ташкент"}
WH_OPT = {"object_kind": "warehouse", "protection": "alarm", "losses_3y": {"count": 0, "small_count": 0}}


def fresh():
    guest.reset()
    act.reset_limits()


def docx_bytes(lines, extra_parts=None) -> bytes:
    """Минимальный DOCX: абзацы из строк (и лишние части архива, если нужны)."""
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{ln}</w:t></w:r></w:p>" for ln in lines)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr("word/document.xml", xml)
        for name, data in (extra_parts or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def docx_bomb(mb=60) -> bytes:
    """DOCX с частью из нулей: в файле — десятки килобайт, после распаковки — mb мегабайт."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", '<?xml version="1.0"?><w:document/>')
        z.writestr("word/media/zero.bin", b"\x00" * (mb * 1024 * 1024))
    return buf.getvalue()


def ra_same(con, must, optional):
    """Расчёт risk_analytics напрямую — тем же быстрым режимом, что и акт."""
    from app import risk_analytics as ra
    m, o, a = ra.apply_defaults(con, must, optional)
    return ra.analyze(con, m, o, assumptions=a)


# сценарий акта ← сценарий risk_analytics (там названия EML и PML переставлены, модуль не меняем)
MAP_RA = {"PML": "EML", "EML": "PML", "MFL": "MFL"}
SCEN_REPORT = {}                          # цифры сценариев для отчёта (печатаются в конце)


def order_ok(sc: dict) -> bool:
    return sc["pml"]["amount"] <= sc["eml"]["amount"] <= sc["mfl"]["amount"]


def check_scenarios():
    print("21. Сценарии PML / EML / MFL — сверка с risk_analytics (порядок заказчика PML ≤ EML ≤ MFL)")
    fresh()
    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                       "optional": dict(CRANE_OPT, object_kind="truck_crane")})
    sc = a.get("scenarios") or {}
    ok("автокран: блок сценариев есть", st == 200 and sc.get("available") and sc.get("rule") == "vehicle", sc)
    with db.tx() as con:
        an = ra_same(con, {"class_code": "3", "product_code": "0318", "object_type": CRANE_TYPE,
                           "sum_insured": CRANE_MUST["sum_insured"], "object_value": CRANE_MUST["object_value"],
                           "region": CRANE_MUST["region"], "vehicle_type": "special"},
                     {"losses_3y": {"count": 0, "amount": 0, "small_count": 0}})
    # порядок заказчика PML ≤ EML ≤ MFL: PML акта = EML модуля («защита сработала»), EML акта = PML модуля
    for s in ("PML", "EML", "MFL"):
        src = MAP_RA[s]
        ok(f"автокран: {s} акта = {src} risk_analytics", sc[s.lower()]["amount"] == round(an["scenarios"][src]["amount"])
           and sc[s.lower()]["pct"] == round(an["scenarios"][src]["amount"] / CRANE_MUST["sum_insured"] * 100, 1)
           and sc[s.lower()]["source_scenario"] == src, (sc.get(s.lower()), an["scenarios"][src]["amount"]))
    ok("автокран: PML ≤ EML ≤ MFL", order_ok(sc), [sc[k]["amount"] for k in ("pml", "eml", "mfl")])
    ok("автокран: PML — крупная авария с ремонтом (50 %), без слов про отсутствие защиты",
       sc["pml"]["pct"] == 50.0 and "авария" in sc["pml"]["what"] and "нет" not in sc["pml"]["what"].split("—")[0]
       and "противоугон" not in sc["pml"]["what"], sc["pml"])
    ok("автокран без противоугонной: EML 100 % — угон или гибель, подпись про отсутствие системы",
       sc["eml"]["pct"] == 100.0 and "угон" in sc["eml"]["what"] and "нет" in sc["eml"]["what"], sc["eml"])
    ok("автокран: MFL 100 % — защита не сработала", sc["mfl"]["pct"] == 100.0 and "не сработала" in sc["mfl"]["what"])
    ret = an["retention"]
    ok("лимит удержания = risk_analytics", sc["retention"]["limit"] == ret["retention_limit"]
       and sc["retention"]["known"] is (ret["retention_limit"] is not None), (sc["retention"], ret.get("retention_limit")))
    lim = sc["retention"]["limit"]
    ok("удержание сравнивается с EML: compared_with = eml, превышение = EML − лимит",
       sc["retention"]["compared_with"] == "eml"
       and (lim is None or sc["retention"]["eml_excess"] == max(sc["eml"]["amount"] - lim, 0)), sc["retention"])
    ok("MFL сверх удержания — отдельной справкой (mfl_excess)",
       lim is None or sc["retention"]["mfl_excess"] == max(sc["mfl"]["amount"] - lim, 0), sc["retention"])
    ok("что взято по умолчанию — в assumptions с пометкой (противоугонная влияет на EML)",
       any(x["code"] == "as_protection_veh_eml" and "EML" in x["text"] for x in sc["assumptions"])
       and all("по умолчанию" in x["text"] for x in sc["assumptions"]), sc["assumptions"])
    ok("how: честно о перестановке названий в risk_analytics",
       any("переставлены" in h and "PML акта = EML модуля" in h for h in sc["how"]), sc["how"])
    ok("calibrated = 0", sc["calibrated"] == 0 and sc["pml"]["calibrated"] == 0)
    rows = {r["label"]: r for r in a["sections"][3]["rows"]}
    ok("раздел 4: строки PML, EML, MFL и лимит удержания",
       {"PML — вероятный максимальный убыток", "EML — оценочный максимальный убыток",
        "MFL — максимально возможный убыток", "Лимит собственного удержания"} <= set(rows), list(rows))
    ok("раздел 4: определения заказчика (штатно / частично / отказ; удержание — с EML)",
       any("сработала штатно" in p and "сработала частично" in p and "отказе защиты" in p
           and "сравнивается лимит собственного удержания" in p for p in a["sections"][3]["paragraphs"]))
    ok("раздел 4: порядок строк PML, EML, MFL и плитки для экрана в том же порядке",
       [r["label"][:3] for r in a["sections"][3]["rows"] if r["label"][:3] in ("PML", "EML", "MFL")] == ["PML", "EML", "MFL"]
       and [x["name"] for x in sc["tiles"]] == ["PML", "EML", "MFL"] and sc["tiles"][0]["label"].startswith("PML —"))
    if lim is not None:
        note = rows["Лимит собственного удержания"]["note"]
        ok("строка удержания: сравнение с EML и справка по MFL", "EML" in note and "MFL" in note, note)
    st, a2 = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                        "optional": dict(CRANE_OPT, object_kind="truck_crane", protection="tracker")})
    sc2 = a2["scenarios"]
    ok("спутниковый поиск — EML ниже (75 %), PML 50 %, MFL 100 %",
       sc2["eml"]["pct"] == 75.0 and sc2["pml"]["pct"] == 50.0 and sc2["mfl"]["pct"] == 100.0
       and "противоугонная система есть" in sc2["eml"]["what"], sc2["eml"])
    ok("спутниковый поиск: PML ≤ EML ≤ MFL", order_ok(sc2))
    SCEN_REPORT["crane"] = {k: (sc[k]["amount"], sc[k]["pct"]) for k in ("pml", "eml", "mfl")}
    SCEN_REPORT["crane_tracker"] = {k: (sc2[k]["amount"], sc2[k]["pct"]) for k in ("pml", "eml", "mfl")}

    st, a = call("POST", "/act/make", {"lang": "ru", "must": WH_MUST, "optional": WH_OPT})
    sc = a["scenarios"]
    with db.tx() as con:
        an = ra_same(con, {"class_code": "9", "product_code": "0808", "object_type": "Склад",
                           "sum_insured": WH_MUST["sum_insured"], "object_value": WH_MUST["object_value"],
                           "region": WH_MUST["region"], "activity": "warehouse"},
                     {"protection": "alarm", "losses_3y": {"count": 0, "small_count": 0}})
    ok("склад класса 9: правило класса 9", sc["available"] and sc["rule"] == "property9", sc)
    ok("склад класса 9: PML/EML/MFL акта = EML/PML/MFL risk_analytics",
       all(sc[s.lower()]["amount"] == round(an["scenarios"][MAP_RA[s]]["amount"]) for s in ("PML", "EML", "MFL")),
       ([sc[s.lower()]["amount"] for s in ("PML", "EML", "MFL")],
        [an["scenarios"][s]["amount"] for s in ("PML", "EML", "MFL")]))
    ok("склад класса 9: PML ≤ EML ≤ MFL", order_ok(sc), [sc[k]["amount"] for k in ("pml", "eml", "mfl")])
    ok("склад класса 9: подписи — состояние защиты и «помещения не указаны»",
       sc["pml"]["what"].startswith("защита сработала штатно") and sc["eml"]["what"].startswith("защита сработала частично")
       and sc["mfl"]["what"].startswith("защита не сработала") and "не указаны" in sc["pml"]["what"],
       [sc[k]["what"] for k in ("pml", "eml", "mfl")])
    ok("склад: сумма ниже стоимости — доля в объяснении", any("0,8333" in h for h in sc["how"]), sc["how"])
    SCEN_REPORT["wh9"] = {k: (sc[k]["amount"], sc[k]["pct"]) for k in ("pml", "eml", "mfl")}
    must8 = {"product_code": "0807", "sum_insured": 5_000_000_000, "object_value": 5_000_000_000, "region": "Ташкент"}
    st, a = call("POST", "/act/make", {"lang": "ru", "must": must8,
                                       "optional": {"object_kind": "warehouse", "seismic_zone": 9,
                                                    "construction": "reinforced"}})
    with db.tx() as con:
        an = ra_same(con, {"class_code": "8", "product_code": "0807", "object_type": "Склад", "sum_insured": 5e9,
                           "object_value": 5e9, "region": "Ташкент", "construction": "reinforced",
                           "activity": "warehouse"}, {"seismic_zone": 9})
    sc = a["scenarios"]
    ok("склад класса 8 в 9-балльной зоне: сценарии = risk_analytics (со сменой названий)",
       sc["rule"] == "property8"
       and all(sc[s.lower()]["amount"] == round(an["scenarios"][MAP_RA[s]]["amount"]) for s in ("PML", "EML", "MFL")),
       sc)
    ok("склад класса 8 (9 баллов): PML ≤ EML ≤ MFL", order_ok(sc), [sc[k]["amount"] for k in ("pml", "eml", "mfl")])
    ok("склад класса 8 (9 баллов): формула названа сценарием акта",
       all((sc[k]["formula"] or "").startswith(k.upper() + " =") for k in ("pml", "eml", "mfl")),
       [sc[k]["formula"] for k in ("pml", "eml", "mfl")])
    ok("склад класса 8 (9 баллов): MFL — пожар всего объекта (отсеки не указаны)",
       "пожар" in sc["mfl"]["what"] and "отсеки не указаны" in sc["mfl"]["what"], sc["mfl"]["what"])
    SCEN_REPORT["wh8_zone9"] = {k: (sc[k]["amount"], sc[k]["pct"]) for k in ("pml", "eml", "mfl")}
    # сейсмозона не указана: MFL — полное уничтожение, подпись говорит именно это
    st, a = call("POST", "/act/make", {"lang": "ru", "must": must8,
                                       "optional": {"object_kind": "warehouse", "construction": "reinforced",
                                                    "protection": "sprinkler"}})
    sc = a["scenarios"]
    ok("склад класса 8 без сейсмозоны: PML ≤ EML ≤ MFL", order_ok(sc), [sc[k]["amount"] for k in ("pml", "eml", "mfl")])
    ok("склад класса 8 без сейсмозоны: MFL — «полное уничтожение: сейсмозона не указана», 100 %",
       "полное уничтожение" in sc["mfl"]["what"] and "сейсмозона не указана" in sc["mfl"]["what"]
       and sc["mfl"]["pct"] == 100.0 and "полное уничтожение" in sc["mfl"]["formula"], sc["mfl"])
    ok("склад класса 8 без сейсмозоны: PML и EML — пожар, отсеки не указаны (весь объект)",
       all("пожар" in sc[k]["what"] and "отсеки не указаны" in sc[k]["what"] for k in ("pml", "eml")),
       [sc[k]["what"] for k in ("pml", "eml")])
    SCEN_REPORT["wh8_sprinkler"] = {k: (sc[k]["amount"], sc[k]["pct"]) for k in ("pml", "eml", "mfl")}
    pcts = [sc[k]["pct_text"] for k in ("pml", "eml", "mfl")]
    ok("проценты сценариев — одинаковое число знаков", len({len(re.sub(r"[^\d,.]", "", p).partition(",")[2]) for p in pcts}) == 1,
       pcts)
    st, a = call("POST", "/act/make", {"lang": "ru", "must": dict(WH_MUST, product_code="0701"), "optional": {}})
    # с 30.09.2026 у класса 7 — простое правило шаблона класса (одна отправка / накопление), а не «не считается»
    ok("груз (класс 7): сценарий по правилу шаблона — одна отправка; без полей класса — страховая сумма",
       st == 200 and a["scenarios"]["available"] is True and a["scenarios"]["source"] == "template"
       and a["scenarios"]["rule"] == "shipment" and a["scenarios"]["pml"]["amount"] == WH_MUST["sum_insured"]
       and {x["code"] for x in a["scenarios"]["assumptions"]} == {"as_tpl_shipment", "as_tpl_accumulation"},
       a.get("scenarios"))
    ok("груз: в разделе 4 строки PML, EML, MFL (подпись из шаблона), строки «не считается» нет",
       any(r["label"].startswith("PML") and r.get("note") == "одна отправка" for r in a["sections"][3]["rows"])
       and not any(r["value"] == "не считается" for r in a["sections"][3]["rows"]), a["sections"][3]["rows"][:6])
    # собственных средств нет — удержание «не задан»
    with db.tx() as con:
        saved = db.rows(con, "SELECT * FROM company_financials")
        con.execute("DELETE FROM company_financials")
    try:
        st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST, "optional": CRANE_OPT})
    finally:
        with db.tx() as con:
            for r in saved:
                cols = list(r)
                con.execute(f"INSERT INTO company_financials ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                            [r[c] for c in cols])
    ret = a["scenarios"]["retention"]
    ok("без собственных средств и резервов — лимит «не задан»",
       ret["known"] is False and ret["limit"] is None and "не задан" in ret["basis"], ret)


def check_documents():
    print("22. Разбор документов DOCX/XLSX/PDF с текстом — без модели")
    fresh()
    model_on(True)
    REPLY["text"] = CRANE_REPLY
    CALLS.clear()
    st, b = upload([("contract.docx", DOCX_MIME, CONTRACT.read_bytes())], {"lang": "ru", "product_code": "0808"})
    ok("учебный договор принят", st == 200 and b.get("ok"), (st, b))
    ok("модель не вызывалась", not CALLS, len(CALLS))
    rec = {(r["key"], r["value"]): r for r in b["recognized"]}
    ok("recognized: страховая сумма, стоимость, срок, регион, тип, конструкция, год",
       {("sum_insured", "4 200 000 000"), ("object_value", "5 000 000 000"), ("term_days", "365"),
        ("region", "город Ташкент"), ("object_type", "склад готовой продукции"), ("construction", "кирпич"),
        ("year", "2012")} <= set(rec), list(rec))
    ok("у каждого значения source = document и «проверьте»",
       all(r["source"] == "document" and r["check_label"] == "проверьте" and "из документа" in r["note"]
           for r in b["recognized"]))
    pf = b.get("prefill") or {}
    ok("prefill: сумма, стоимость, регион, срок — с источником",
       pf.get("sum_insured", {}).get("value") == 4_200_000_000 and pf.get("object_value", {}).get("value") == 5e9
       and pf.get("region", {}).get("code") == "tashkent_city" and pf.get("term_days", {}).get("value") == 365
       and all(v["source"] == "document" and v["check_label"] == "из документа, проверьте" for v in pf.values()), pf)
    ok("файл помечен как разобранный, в модель не ушёл",
       b["files"][0]["parsed"] and not b["files"][0]["read_by_ai"] and b["files"][0]["view"] == "document")
    ok("ИНН организации из договора не взят", "301234567" not in _json.dumps(b, ensure_ascii=False))
    st, a = call("POST", "/act/make", {"session": b["session"], "lang": "ru",
                                       "must": dict(WH_MUST, sum_insured=4_000_000_000, object_value=5_000_000_000),
                                       "optional": {}})
    dk = {d["key"]: d for d in a["discrepancies"]}
    ok("расхождение: страховая сумма в документе и во вводе", "sum_insured" in dk
       and dk["sum_insured"]["priority"] == "document" and "4 200 000 000" in dk["sum_insured"]["text"], dk)
    ok("стоимость совпала — расхождения нет", "object_value" not in dk)
    ok("документ засчитан как представленный", a["inspection"]["documents"] is True)
    s1 = {r["label"]: r for r in a["sections"][0]["rows"]}
    ok("раздел 1: конструкция из документа", s1.get("Конструкция, материал стен", {}).get("value") == "кирпич", s1)

    # ФИО, паспорт, ПИНФЛ, адрес проживания — не извлекаются и не хранятся
    lines = ["ЗАЯВЛЕНИЕ НА СТРАХОВАНИЕ ИМУЩЕСТВА", "Страхователь: Иванов Иван Иванович",
             "Паспорт: AA1234567", "ПИНФЛ: 31234567890123", "Адрес проживания: г. Ташкент, ул. Навои, 5",
             "Телефон: +998 90 123 45 67", "Страховая сумма: 1 000 000 000 сум", "Материал стен: кирпич"]
    st, b = upload([("zayavlenie.docx", DOCX_MIME, docx_bytes(lines))], {"lang": "ru", "product_code": "0808"})
    dump = _json.dumps(b, ensure_ascii=False)
    ok("заявление разобрано: сумма взята", any(r["key"] == "sum_insured" for r in b.get("recognized") or []), dump[:400])
    ok("ФИО, паспорт, ПИНФЛ, адрес проживания и телефон не извлечены",
       not any(x in dump for x in ("Иванов", "AA1234567", "31234567890123", "Навои", "123 45 67")), dump[:600])
    with db.tx() as con:
        stored = _json.dumps(db.rows(con, "SELECT result_json, files_json FROM act_uploads WHERE id=?", b["session"]),
                             ensure_ascii=False)
        journal = _json.dumps(db.rows(con, "SELECT detail FROM audit WHERE entity=?", f"act_upload:{b['session']}"),
                              ensure_ascii=False)
    ok("в базе и в журнале данных людей нет",
       not any(x in stored + journal for x in ("Иванов", "AA1234567", "31234567890123", "Навои")))
    ok("в журнале — только счётчики", "parsed_docs" in journal and "1 000 000 000" not in journal, journal[:300])

    # XLSX-выгрузка по технике: марка, модель, год, VIN, госномер
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    for row in (["Заявление на страхование спецтехники"], ["Страховая сумма", "2 945 000 000 сум"],
                ["Стоимость имущества", "3 100 000 000 сум"], ["Марка", "XCMG"], ["Модель", "QY50K5D"],
                ["Год выпуска", "2026"], ["Заводской номер (VIN)", "LXGCPA393TA006921"],
                ["Государственный номер", "01 A 123 BC"]):
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    CALLS.clear()
    st, b = upload([("vygruzka.xlsx", XLSX_MIME, buf.getvalue())], {"lang": "ru", "product_code": "0318"})
    got = {(r["key"], r["value"]) for r in b.get("recognized") or []}
    ok("XLSX: марка, модель, год, VIN, госномер, сумма — из документа",
       {("brand", "XCMG"), ("model", "QY50K5D"), ("year", "2026"), ("serial_no", "LXGCPA393TA006921"),
        ("reg_no", "01 A 123 BC"), ("sum_insured", "2 945 000 000")} <= got and not CALLS, got)

    # PDF с текстовым слоем — парсером; скан — модели
    doc = pymupdf.open()
    page = doc.new_page()
    font = act._fonts()[0]
    tw = pymupdf.TextWriter(page.rect)
    for k, ln in enumerate(["ДОГОВОР СТРАХОВАНИЯ ИМУЩЕСТВА № 7/2026", "Страховая сумма: 1 000 000 000 сум",
                            "Стоимость имущества: 1 100 000 000 сум",
                            "Адрес объекта: Самаркандская область, г. Самарканд", "Срок страхования: 12 месяцев"]):
        tw.append((72, 72 + 18 * k), ln, font=font, fontsize=11)
    tw.write_text(page)
    CALLS.clear()
    st, b = upload([("dogovor.pdf", "application/pdf", doc.tobytes())], {"lang": "ru", "product_code": "0808"})
    ok("PDF с текстом разобран без модели", st == 200 and not CALLS and b["files"][0]["parsed"]
       and (b.get("prefill") or {}).get("region", {}).get("code") == "samarkand", b.get("prefill"))
    CALLS.clear()
    st, b = upload([("scan.pdf", "application/pdf", pdf_pages(1))], {"lang": "ru"})
    ok("скан PDF без текста — читает модель", st == 200 and len(CALLS) == 1 and not b["files"][0]["parsed"])

    # ограничения: zip-бомба, DTD, макросы
    bomb = docx_bomb(60)
    st, b = upload([("bomb.docx", DOCX_MIME, bomb)], {"lang": "ru"})
    ok("zip-бомба в DOCX отклонена до распаковки", st == 422 and b["rejected"]
       and "слишком большой" in b["rejected"][0]["error"] and len(bomb) < 1024 * 1024, (st, b, len(bomb)))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><w:document/>')
    st, b = upload([("dtd.docx", DOCX_MIME, buf.getvalue())], {"lang": "ru"})
    ok("DOCX с DTD/ENTITY отклонён", st == 422 and "DTD" in b["rejected"][0]["error"], b)
    st, b = upload([("macro.docx", DOCX_MIME, docx_bytes(["Страховая сумма: 1 000 000 000 сум"],
                                                          {"word/vbaProject.bin": b"\x00" * 100}))], {"lang": "ru"})
    ok("макросы не исполняются — только пометка", st == 200 and any("макрос" in n for n in b["notes"]), b.get("notes"))
    set_limits(doc_max_unzip_mb=1)
    try:
        st, b = upload([("contract.docx", DOCX_MIME, docx_bomb(2))], {"lang": "ru"})
        ok("предел распаковки — из настроек (1 МБ)", st == 422, st)
    finally:
        clear_settings()
    st, b = upload([("x.zip", "application/zip", docx_bomb(1).replace(b"word/document.xml", b"other/documen.xml"))],
                   {"lang": "ru"})
    ok("прочий zip — формат не принимается", st == 422 and "DOCX или XLSX" in b["rejected"][0]["error"], b)


def check_franchise_apply():
    print("23. Франшиза: предложение по основанию, франшиза сотрудника, обязательный вид")
    from app import franchise as frm
    from app.risk_analytics import load_thresholds
    fresh()
    with db.tx() as con:
        th = load_thresholds(con)
        base, floor, applied = expected_rate(con, 20)            # умеренный уровень: без фото, открытая площадка
    S = CRANE_MUST["sum_insured"]
    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST, "optional": CRANE_OPT})
    fr = a["franchise"]
    ok("без оснований: «Франшиза не требуется», статус none, премия не меняется",
       fr["text"] == "Франшиза не требуется" and fr["status"] == "none" and not fr["applied"]
       and fr["premium_after"] == fr["premium_before"] == a["premium"]["amount"] and fr["delta"] == 0, fr)
    ok("без оснований: альтернативы есть (мероприятия, сумма к стоимости)",
       {x["code"] for x in fr["alternatives"]} >= {"alt_sum_up"} and fr["how"], fr["alternatives"])

    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                       "optional": dict(CRANE_OPT, losses_3y={"count": 2, "small_count": 2})})
    fr = a["franchise"]
    ok("основание есть: статус proposed, тип безусловная", fr["status"] == "proposed"
       and fr["type"] == "unconditional" and fr["needed"], fr)
    ok("размер — внутри вилки порогов", fr["size"]["from_pct"] <= fr["size_pct"] <= fr["size"]["to_pct"]
       and fr["size_amount"] == round(S * fr["size_pct"] / 100), fr)
    ok("множитель — из franchise.what_if (экспертная кривая)", abs(fr["multiplier"] - frm._mult(th, fr["size_pct"])) < 1e-4,
       (fr["multiplier"], frm._mult(th, fr["size_pct"])))
    rate = a["rate"]["applied_pct"]
    want_rate = max(round(rate * fr["multiplier"], 4), a["rate"]["min_pct"] or 0)
    ok("премия с франшизой = ставка акта × множитель, не ниже минимума",
       fr["premium_after"] == round(want_rate / 100 * S) and fr["rate_after"] == want_rate, (fr, want_rate))
    ok("предложенная франшиза в премию акта не включена", a["premium"]["amount"] == fr["premium_before"]
       and a["premium"]["franchise_applied"] is False and "андеррайтер" in fr["text"])

    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                       "optional": dict(CRANE_OPT, deductible={"pct": 1, "type": "unconditional"})})
    fr = a["franchise"]
    hand_rate = max(round(applied * 0.85, 4), floor)          # 1 % по справочнику коэффициентов: ×0,85
    hand = round(hand_rate / 100 * S)
    ok("своя франшиза сотрудника 1 %: премия пересчитана руками", a["premium"]["amount"] == hand
       and fr["premium_after"] == hand and fr["premium_before"] == round(applied / 100 * S), (a["premium"], hand))
    ok("текст «Франшиза применена по решению сотрудника»",
       fr["text"].startswith("Франшиза применена по решению сотрудника") and fr["applied"]
       and fr["applied_by"] == "employee", fr["text"])
    rows = {r["label"]: r for r in a["sections"][3]["rows"]}
    ok("раздел 4: та же премия и ставка с франшизой",
       rows["Страховая премия"]["value"] == act.money(hand, "ru")
       and rows["Ставка с учётом франшизы"]["value"] == act.pct(hand_rate, "ru")
       and act.money(round(applied / 100 * S), "ru") in rows["Страховая премия"]["note"], rows.get("Страховая премия"))
    ok("андеррайтеру — подтвердить франшизу", any("франшизу, применённую" in c for c in a["decision"]["checks"]))
    # служебного имени функции (what_if) в тексте нет — проверяем смысл: множитель модуля франшизы и ставка акта
    mult_txt = act._mult(fr["multiplier"], "ru")
    ok("how: множитель модуля франшизы и ставка акта × множитель, премия модуля расчёта ставок не переносится",
       any("из модуля франшизы" in h and "множитель " + mult_txt in h for h in fr["how"])
       and any(h.startswith("Ставка акта") and act.pct(applied, "ru") + " × " + mult_txt in h
               and act.pct(hand_rate, "ru") in h and "не переносится" in h for h in fr["how"])
       and not any("what_if" in h or "движ" in h for h in fr["how"]), fr["how"])
    applied_id = a["id"]
    st, a2 = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                        "optional": dict(CRANE_OPT, deductible={"amount": 29_450_000})})
    ok("франшиза суммой = 1 %", a2["franchise"]["size_pct"] == 1.0 and a2["premium"]["amount"] == hand, a2["franchise"])
    st, a2 = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                        "optional": dict(CRANE_OPT, deductible={"pct": 7})})
    ok("выше потолка класса 3 (5 %) — предупреждение, решение андеррайтера",
       a2["franchise"]["warning"] and "потолка" in a2["franchise"]["warning"] and a2["franchise"]["applied"], a2["franchise"])
    st, a2 = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                        "optional": dict(CRANE_OPT, deductible={"pct": 80})})
    ok("франшиза 80 % — ошибка ввода 422", st == 422 and "deductible" in a2.get("errors", {}), a2)
    st, a2 = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                        "optional": dict(CRANE_OPT, deductible={"pct": 1, "amount": 5})})
    ok("pct и amount вместе — 422", st == 422, a2)
    must = {"product_code": "0820", "sum_insured": 1_000_000_000, "object_value": 1_000_000_000, "region": "Ташкент"}
    st, a2 = call("POST", "/act/make", {"lang": "ru", "must": must,
                                        "optional": {"deductible": {"pct": 1}, "location": "construction"}})
    fr = a2["franchise"]
    ok("обязательный вид: франшиза сотрудника не применена, премия по акту",
       st == 200 and fr["status"] == "statutory" and not fr["applied"]
       and a2["premium"]["amount"] == a2["premium"]["before_franchise"] and "обязательным видам" in fr["warning"], fr)
    # низкий уровень: ставка уже на минимуме продукта — франшиза премию не снижает, и акт говорит об этом
    low = dict(CRANE_OPT, object_kind="truck_crane", year=date.today().year, condition="new",
               documents_provided=True, deductible={"pct": 1})
    st, a2 = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST, "optional": low})
    fr = a2["franchise"]
    ok("ставка на минимуме: премия с франшизой не ниже минимальной ставки",
       a2["risk"]["level"] == "low" and fr["floor_applied"] and a2["premium"]["amount"] == round(floor / 100 * S)
       and "минимальную ставку" in fr["text"], (a2["risk"]["level"], fr))
    return applied_id, hand


def check_measures():
    print("24. Рекомендации страхователю")
    fresh()
    with db.tx() as con:
        _b, floor, applied = expected_rate(con, 20)
    S = CRANE_MUST["sum_insured"]
    P = round(applied / 100 * S)
    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                       "optional": dict(CRANE_OPT, object_kind="truck_crane")})
    ms = {m["code"]: m for m in a["measures"]}
    ok("спецтехника на открытой площадке: охраняемая стоянка и спутниковый мониторинг",
       {"sp_parking_guarded", "sp_gps", "sp_crane_setup", "sp_operator"} <= set(ms), list(ms))
    ok("эффект мероприятия — из справочника коэффициентов (−10 %)",
       ms["sp_parking_guarded"]["effect_pct"] == -10.0 and ms["sp_gps"]["effect_pct"] == -10.0, ms["sp_gps"])
    ok("без эффекта — «на ставку не влияет, снижает вероятность убытка»",
       ms["sp_operator"]["premium_delta"] is None and "снижает вероятность" in ms["sp_operator"]["effect_text"])
    want = max(round(P * 0.9 * 0.9), round(floor / 100 * S))
    summ = a["measures_summary"]
    ok("скидки перемножаются и не ниже минимальной ставки продукта",
       summ["premium_after"] == want and summ["premium_before"] == P, (summ, want))
    ok("каждое мероприятие: что, зачем, срок, calibrated=0",
       all(m["text"] and m["why"] and m["deadline_days"] and m["calibrated"] == 0 for m in a["measures"]))
    s5 = a["sections"][4]
    li = next((x for x in s5["lists"] if x["title"] == "Рекомендации страхователю"), None)
    ok("раздел 5: подраздел «Рекомендации страхователю»", li and len(li["items"]) == len(a["measures"])
       and "Зачем:" in li["items"][0] and "Срок:" in li["items"][0], li)
    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                       "optional": dict(CRANE_OPT, object_kind="truck_crane", guard=True)})
    ok("под охраной — охраняемая стоянка не нужна", "sp_parking_guarded" not in {m["code"] for m in a["measures"]})

    st, a = call("POST", "/act/make", {"lang": "ru", "must": WH_MUST, "optional": WH_OPT})
    ms = {m["code"]: m for m in a["measures"]}
    ok("склад класса 9: мероприятия таблицы preventive_measures и хранение на стеллажах",
       {"burglary_protection", "inventory_control", "wh_storage"} <= set(ms)
       and ms["burglary_protection"]["source"] == "preventive_measures", list(ms))
    ok("склад: сумма 1 млрд — охранная сигнализация обязательна, срок 60 дней",
       ms["burglary_protection"]["mandatory"] and ms["burglary_protection"]["deadline_days"] == 60,
       ms["burglary_protection"])
    st, u = call("GET", f"/act/{a['id']}", params={"lang": "uz"})
    ok("uz: мероприятия таблицы переведены", not re.search(r"[А-Яа-яЁё]", " ".join(
        m["text"] + m["why"] for m in u["measures"])), [m["text"] for m in u["measures"]])
    must8 = {"product_code": "0807", "sum_insured": 5_000_000_000, "object_value": 5_000_000_000, "region": "Ташкент"}
    st, a = call("POST", "/act/make", {"lang": "ru", "must": must8,
                                       "optional": {"object_kind": "warehouse", "construction": "wood"}})
    ms = {m["code"]: m for m in a["measures"]}
    ok("склад класса 8 из дерева: огнезащитная обработка со скидкой (из движка)",
       "fire_treatment" in ms and ms["fire_treatment"]["effect_pct"] and ms["fire_treatment"]["effect_pct"] < 0
       and ms["fire_treatment"]["mandatory"], ms.get("fire_treatment"))
    must = {"product_code": "0820", "sum_insured": 1_000_000_000, "object_value": 1_000_000_000, "region": "Ташкент"}
    st, a = call("POST", "/act/make", {"lang": "ru", "must": must, "optional": {}})
    ok("обязательный вид: скидок на премию нет", all(m["premium_delta"] is None for m in a["measures"]))


def check_new_langs(aid):
    print("25. Новые блоки на трёх языках")
    for lang in ("uz", "en"):
        st, a = call("GET", f"/act/{aid}", params={"lang": lang})
        fr = a["franchise"]
        texts = ([r["label"] for s in a["sections"] for r in s["rows"]]
                 + [r.get("note") or "" for r in a["sections"][3]["rows"]]
                 + [li["title"] for s in a["sections"] for li in s["lists"]]
                 + [p for s in a["sections"] for p in s["paragraphs"]]
                 + [fr["text"], fr["type_label"] or ""] + fr["how"] + [x["text"] for x in fr["alternatives"]]
                 + [a["scenarios"][k]["what"] for k in ("pml", "eml", "mfl")] + a["scenarios"]["how"]
                 + [x["text"] for x in a["scenarios"]["assumptions"]] + [a["scenarios"]["retention"]["basis"]]
                 + [m["text"] + " " + m["why"] + " " + m["effect_text"] for m in a["measures"]]
                 + [a["measures_summary"]["text"] or ""])
        cyr = [x for x in texts if re.search(r"[А-Яа-яЁё]", x or "")]
        ok(f"{lang}: сценарии, франшиза, мероприятия без кириллицы", not cyr, cyr[:4])
    st, a = call("GET", f"/act/{aid}", params={"lang": "en"})
    ok("en: франшиза применена сотрудником", a["franchise"]["text"].startswith("Deductible applied by staff"))


def check_new_files(aid, hand):
    print("26. Word и PDF содержат новые блоки и ту же премию")
    st, blob, h = call("GET", f"/act/{aid}.docx", raw=True)
    xml = zipfile.ZipFile(io.BytesIO(blob)).read("word/document.xml").decode("utf-8")
    plain = re.sub(r"<[^>]+>", "", xml)
    # акт на один лист (06.10.2026): сценарии словами, франшиза в «Условиях», ставка с франшизой в «Цене»,
    # мероприятия в разделе 5; лимит удержания и «как посчитано» — только в JSON и на экране
    need = ["PML (вероятный максимум)", "EML (при отказе защиты)", "MFL (полная потеря)",
            "Предупредительные мероприятия", "— применена", "с учётом франшизы", act.money(hand, "ru")]
    gone = ["Лимит собственного удержания", "Франшиза: как посчитано", "Как посчитаны сценарии"]
    ok("DOCX: сценарии, франшиза, рекомендации, премия с франшизой; удержания и «как посчитано» нет",
       all(x in plain for x in need) and not any(x in plain for x in gone),
       ([x for x in need if x not in plain], [x for x in gone if x in plain]))
    st, blob, h = call("GET", f"/act/{aid}.pdf", raw=True)
    text = pdf_text(pymupdf.open(stream=blob, filetype="pdf"))
    flat = [x.replace(" ", " ") for x in need]           # pdf_text приводит неразрывный пробел к обычному
    ok("PDF: те же блоки и премия", all(x in text for x in flat), [x for x in flat if x not in text])
    st, a = call("GET", f"/act/{aid}")
    ok("премия в JSON, в разделе 4 и в файлах одна", a["premium"]["amount"] == hand
       and a["premium"]["text"] == act.money(hand, "ru"))


def check_speed():
    print("27. Акт формируется быстро и без сети")
    import time as _t
    fresh()
    CALLS.clear()
    t0 = _t.monotonic()
    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                       "optional": dict(CRANE_OPT, deductible={"pct": 1})})
    sec = _t.monotonic() - t0
    ok("акт со всеми блоками — меньше 2 секунд, без модели", st == 200 and sec < 2 and not CALLS, sec)
    with db.tx() as con:
        rows = db.rows(con, "SELECT detail FROM audit WHERE entity=?", f"act:{a['id']}")
    ok("в журнале — статус франшизы и счётчики, без значений",
       rows and '"franchise": "applied"' in rows[0]["detail"] and "29 450 000" not in rows[0]["detail"], rows)


# ------------------------------------------------------------------ 28–35. замечания контролёра (30.09.2026)

def xlsx_big(rows, cols, sheets=1) -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    words = ["склад", "сумма", "Ташкент", "объект", "qiymat", "ombor", "value", "итого", "страхование"]
    for s in range(sheets):
        if s:
            ws = wb.create_sheet(f"Лист {s + 1}")
        for r in range(rows):
            ws.append([f"{words[(r + c) % len(words)]} {r}-{c} {words[(r * c) % len(words)]}" for c in range(cols)])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def check_doc_limits():
    print("28. Разбор документов: пределы до разбора, срок внутри циклов, не больше двух одновременно")
    import threading
    import time as _t
    from app import act_extras as ax, docparse as D, ingest
    fresh()
    CALLS.clear()
    for rows, cols, sheets, name in ((500, 60, 1, "500 × 60"), (2000, 3, 3, "2000 строк × 3 листа")):
        blob = xlsx_big(rows, cols, sheets)
        t0 = _t.monotonic()
        st, b = upload([("big.xlsx", XLSX_MIME, blob)], {"lang": "ru", "product_code": "0808"})
        sec = _t.monotonic() - t0
        ok(f"XLSX {name} ({len(blob) // 1024} КБ) — быстрее 3 с", st == 200 and sec < 3, (st, round(sec, 2)))
        ok(f"XLSX {name}: честная пометка «прочитана только часть»",
           any("только часть документа" in n for n in b.get("notes") or []) and b["files"][0]["parsed"], b.get("notes"))
    ok("большие XLSX в модель не уходили", not CALLS, len(CALLS))
    # пределы работают до разбора: листов, строк, колонок, длина ячейки
    folder = Path(tempfile.mkdtemp(prefix="act-lim-"))
    try:
        p = folder / "w.xlsx"
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(["x" * 900, "короткая"])
        ws.append([f"c{c}" for c in range(40)])
        for r in range(300):
            ws.append([f"r{r}", "a", "b"])
        for s in range(4):
            wb.create_sheet(f"S{s}").append(["лист", s])
        wb.save(p)
        got = ax.read_limited(p, {})
        t1 = got["tables"][0]["rows"]
        ok("предел: не больше 3 листов", len(got["tables"]) == 3, [t["name"] for t in got["tables"]])
        ok("предел: не больше 200 строк и 30 колонок с листа",
           len(t1) == 200 and max(len(r) for r in t1) == 30 and got["tables"][0]["обрезан"],
           (len(t1), max(len(r) for r in t1)))
        ok("предел: ячейка и строка не длиннее 500 знаков",
           all(len(" | ".join(r)) <= 500 for r in t1) and all(len(ln) <= 500 for ln in got["text"].splitlines()))
        ok("предел: отметка truncated", got["truncated"] is True)
        small = ax.read_limited(p, {"doc_max_cells": 100})
        ok("предел ячеек на файл — из настроек (100)", sum(len([c for c in r if c]) for t in small["tables"]
                                                             for r in t["rows"]) <= 100, small["tables"][0]["rows"][:2])
        # учебный договор: тот же prefill и те же значения, что у прежнего разбора без пределов
        with db.tx() as con:
            old = ax.parse_document(con, CONTRACT, "8")
            new = ax.parse_document_limited(con, CONTRACT, "8", {})
        ok("flow150_contract.docx: prefill и значения те же, что без пределов",
           old["prefill"] == new["prefill"] and old["items"] == new["items"] and not new["notes"], (old, new))
        r_old, r_new = ingest.read_file(CONTRACT), ax.read_limited(CONTRACT, {})
        ok("потоковое чтение DOCX = ingest.read_docx (текст и таблицы)",
           r_old["text"] == r_new["text"] and r_old["tables"] == r_new["tables"])
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    # кэш свёртки: поведение прежнее, список карты — свой у каждого вызова
    s = "Sugʻurta summasi: 1 000 000 сум"
    f1, i1 = D.fold_map(s)
    f2, i2 = D._fold_map_raw(s)
    i1.append(-1)
    ok("fold_map из кэша = прежний расчёт, карта не делится между вызовами",
       (f1, i1[:-1]) == (f2, i2) and D.fold_map(s)[1] == i2 and D.fold(s) == f2)
    # срок: проверка внутри цикла освобождает поток — разбор возвращается, поток не остаётся висеть
    orig = ingest.detect_language
    ingest.detect_language = lambda text: (_t.sleep(0.4), orig(text))[1]
    set_limits(doc_parse_sec=0.2)
    try:
        before = threading.active_count()
        own_before = [x.name for x in threading.enumerate() if not x.name.startswith("AnyIO worker")]
        t0 = _t.monotonic()
        st, b = upload([("contract.docx", DOCX_MIME, CONTRACT.read_bytes())], {"lang": "ru", "product_code": "0808"})
        sec = _t.monotonic() - t0
        ok("срок одного файла: «не разобран: слишком большой», запрос продолжается",
           st == 200 and not b["files"][0]["parsed"] and any("слишком большой" in n for n in b["notes"])
           and sec < 2, (st, b.get("notes"), sec))
        # ждём с пределом, а не проверяем мгновенно: потоки пула сервера (AnyIO worker) живут своим сроком и к
        # разбору не относятся; поток разбора должен закончиться за время ожидания
        def own():
            return [x.name for x in threading.enumerate() if not x.name.startswith("AnyIO worker")]
        deadline = _t.monotonic() + 5
        while len(own()) > len(own_before) and _t.monotonic() < deadline:
            _t.sleep(0.05)
        ok("поток разбора освобождён (лишних потоков нет)", len(own()) <= len(own_before),
           (before, threading.active_count(), sorted(set(own()) - set(own_before))))
        with db.tx() as con:
            j = db.rows(con, "SELECT detail FROM audit WHERE entity=? AND action='акт: документ не разобран'",
                        f"act_upload:{b['session']}")
        ok("превышение срока — в журнале", j and "timeout" in j[0]["detail"], j)
    finally:
        ingest.detect_language = orig
        clear_settings()
    # срок на все документы запроса: второй файл уже не разбирается
    ingest.detect_language = lambda text: (_t.sleep(0.35), orig(text))[1]
    set_limits(doc_parse_sec=5, doc_parse_total_sec=0.3)
    try:
        st, b = upload([("a.docx", DOCX_MIME, CONTRACT.read_bytes()), ("b.docx", DOCX_MIME, CONTRACT.read_bytes())],
                       {"lang": "ru", "product_code": "0808"})
        ok("общий срок запроса: оба файла помечены, ответ есть",
           st == 200 and not any(f["parsed"] for f in b["files"])
           and len(b["documents"]) == 2 and all(d["notes"] for d in b["documents"]), (st, b.get("documents")))
    finally:
        ingest.detect_language = orig
        clear_settings()
    # не больше двух документов одновременно: оба места заняты — файл честно «сервер занят»
    ok("мест для разбора — два", ax.PARSE_SLOTS == 2)
    took = [ax.parse_slot(0), ax.parse_slot(0)]
    ok("третий документ места не получает", not ax.parse_slot(0))
    set_limits(doc_parse_total_sec=0.3)
    try:
        st, b = upload([("contract.docx", DOCX_MIME, CONTRACT.read_bytes())], {"lang": "ru", "product_code": "0808"})
        ok("семафор занят: «сервер занят разбором», запрос не падает",
           st == 200 and any("сервер занят" in n for n in b["notes"]) and not b["files"][0]["parsed"], b.get("notes"))
    finally:
        for x in took:
            if x:
                ax.parse_slot_release()
        clear_settings()
    st, b = upload([("contract.docx", DOCX_MIME, CONTRACT.read_bytes())], {"lang": "ru", "product_code": "0808"})
    ok("места освобождены — договор снова разбирается", st == 200 and b["files"][0]["parsed"])
    errs = ae.check_settings({"limits": dict(ae.DEFAULT_SETTINGS["limits"], doc_max_rows=5)})
    ok("настройки: предел строк проверяется", any("doc_max_rows" in e for e in errs), errs)
    ok("настройки по умолчанию: 10 000 ячеек, 3 000 абзацев, 200 строк, 30 колонок, 3 листа, 500 знаков, "
       "4 000 знаков в строке DOCX, 200 000 знаков, 5 с и 8 с",
       {k: ae.DEFAULT_SETTINGS["limits"][k] for k in ax.DOC_LIMITS} == ax.DOC_LIMITS
       and not ae.check_settings({"limits": dict(ae.DEFAULT_SETTINGS["limits"])}))


def check_dtd_prolog():
    print("29. DTD/ENTITY ищется во всём прологе XML, а не в первых 4096 байтах")
    from app import act_extras as ax
    fresh()
    xml = ('<?xml version="1.0"?><!--' + "x" * 5000 + '--><!DOCTYPE x [<!ENTITY a "aaaa">]>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", xml)
    st, b = upload([("dtd5000.docx", DOCX_MIME, buf.getvalue())], {"lang": "ru"})
    ok("комментарий 5000 байт перед DOCTYPE — файл отклонён", st == 422 and "DTD" in b["rejected"][0]["error"], (st, b))
    long_c = b'<?xml version="1.0"?><!--' + b"-" * 3 + b"y" * 200_000 + b'-->\n<!ENTITY e "x"><r/>'
    ok("комментарий 200 КБ (через границы кусков) — ENTITY найдена", ax.xml_prolog_has_dtd(io.BytesIO(long_c), 10 ** 7))
    ok("обычный XML с комментарием и инструкцией — годится",
       not ax.xml_prolog_has_dtd(io.BytesIO(b'\xef\xbb\xbf<?xml version="1.0"?>\n<!-- c --><?pi x?><r><!-- <!DOCTYPE --></r>'),
                                 10 ** 6))
    ok("UTF-16 с DOCTYPE — отклоняется", ax.xml_prolog_has_dtd(
        io.BytesIO('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE r><r/>'.encode("utf-16")), 10 ** 6))
    ok("DOCTYPE после корня (в тексте) не ищется — пролог закончился", not ax.xml_prolog_has_dtd(
        io.BytesIO(b"<r>" + b"z" * 10 + b"&lt;!DOCTYPE</r>"), 10 ** 6))
    st, b = upload([("contract.docx", DOCX_MIME, CONTRACT.read_bytes())], {"lang": "ru", "product_code": "0808"})
    ok("настоящий договор проверку проходит", st == 200 and b["files"][0]["parsed"])


def check_fr_proposed():
    print("30. Предложенная франшиза: минимум ставки и множитель выше 2 %")
    fresh()
    with db.tx() as con:
        _b, floor, _a = expected_rate(con, 0)
    S = CRANE_MUST["sum_insured"]
    low = dict(CRANE_OPT, object_kind="truck_crane", year=date.today().year, condition="new",
               documents_provided=True, want_lower_premium=True)
    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST, "optional": low})
    fr = a["franchise"]
    ok("низкий уровень + просьба клиента: франшиза предложена", fr["status"] == "proposed" and fr["size_pct"] > 0, fr)
    ok("предложенная упирается в минимум: floor_applied и то же пояснение, что у применённой",
       fr["floor_applied"] is True and fr["premium_after"] == round(floor / 100 * S)
       and "минимальную ставку продукта" in fr["text"], (fr["floor_applied"], fr["text"]))
    # высокий уровень (вилка 5–10 %, потолок класса 3 — 5 %): вилка 5 % — одно число, множитель — продолжение кривой
    fr_hi = {"code": "fr_advise_range", "needed": True, "status": "proposed", "type": "unconditional",
             "size": {"from_pct": 5.0, "to_pct": 5.0, "from_amount": S * 0.05, "to_amount": S * 0.05},
             "size_pct": 5.0, "size_amount": round(S * 0.05), "premium_before": 100, "premium_after": 90,
             "floor_applied": True, "engine": {"extrapolated": True},
             "how": [{"code": "frh_size", "params": {"from": 5.0, "to": 5.0, "pct": 5.0, "losses": 0}},
                     {"code": "frh_apply", "params": {}}]}
    text = act._fr_text(fr_hi, "ru")
    ok("вилка с равными границами — «5 %», а не «от 5 % до 5 %»", "франшизу 5 %" in text and "от 5" not in text, text)
    ok("предложенная выше 2 %: «экспертное продолжение», и пояснение о минимуме", "экспертное продолжение" in text
       and "минимальную ставку продукта" in text, text)
    how = act._fr_how_text(fr_hi["how"][0], "ru")
    ok("как посчитано: вилка «ровно 5 %»", "ровно 5 %" in how and "–" not in how, how)
    for lang in ("uz", "en"):
        tx_ = act._fr_text(fr_hi, lang)
        ok(f"{lang}: пояснения о минимуме и о 2 % переведены", not re.search(r"[А-Яа-яЁё]", tx_), tx_)


def check_alt_base():
    print("31. «Вместо франшизы можно»: все варианты — от премии без франшизы")
    fresh()
    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                       "optional": dict(CRANE_OPT, object_kind="truck_crane",
                                                        deductible={"pct": 1, "type": "unconditional"})})
    fr = a["franchise"]
    P = a["premium"]["before_franchise"]
    alts = fr["alternatives"]
    ok("франшиза применена, премия акта с ней ниже базы", fr["applied"] and a["premium"]["amount"] < P, a["premium"])
    ok("у каждого варианта база — премия без франшизы",
       alts and all(x["base_premium"] == P for x in alts), [(x["code"], x.get("base_premium")) for x in alts])
    ok("premium_delta = premium − премия без франшизы",
       all(x["premium"] is None or x["premium_delta"] == x["premium"] - P for x in alts),
       [(x["code"], x["premium"], x["premium_delta"]) for x in alts])
    m = next((x for x in alts if x["code"] == "alt_measures"), None)
    want = a["measures_summary"]
    ok("мероприятия вместо франшизы: скидки от базы без франшизы",
       m is not None and m["premium"] == P + m["premium_delta"] and m["premium_delta"] < 0, (m, want))
    ok("в тексте каждого варианта — «Вместо франшизы»", all(x["text"].startswith("Вместо франшизы") for x in alts),
       [x["text"] for x in alts])
    st, u = call("GET", f"/act/{a['id']}", params={"lang": "en"})
    ok("en: «Instead of a deductible», ссылка на норму — по-английски",
       all(x["text"].startswith("Instead of a deductible") for x in u["franchise"]["alternatives"])
       and all(not re.search(r"[А-Яа-яЁё]", x["legal_ref"] or "") for x in u["franchise"]["alternatives"]),
       u["franchise"]["alternatives"])


def check_protection_other():
    print("32. Защита для класса без списка — не ошибка, а пометка")
    fresh()
    st, a = call("POST", "/act/make", {"lang": "ru", "must": dict(WH_MUST, product_code="0701"),
                                       "optional": {"protection": "alarm"}})
    ok("груз (класс 7) с protection — 200, не ошибка ввода", st == 200 and a.get("ok"), (st, a.get("errors")))
    asm = [x["text"] for x in a["scenarios"]["assumptions"]]
    ok("пометка в «принято по умолчанию»", any("для этого класса" in x and "не учтено" in x for x in asm), asm)
    ok("в разделе 4 есть список «Принято по умолчанию»",
       any(li["title"] == "Принято по умолчанию (уточните)" for li in a["sections"][3]["lists"]))
    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST, "optional": dict(CRANE_OPT, protection="sprinkler")})
    ok("класс 3 с кодом защиты имущества (sprinkler) — по-прежнему 422", st == 422 and "protection" in a.get("errors", {}), a)


def check_lang_fields():
    print("33. uz/en: формула и ссылка на норму — не на чужом языке")
    fresh()
    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST, "optional": dict(CRANE_OPT, object_kind="truck_crane")})
    ok("ru: формула есть, ссылка — «Положение № 1806, п. 15»", a["scenarios"]["pml"]["formula"]
       and a["scenarios"]["retention"]["legal_ref"] == "Положение № 1806, п. 15", a["scenarios"]["retention"])
    for lang, ref in (("uz", "1806-son Nizom, 15-band"), ("en", "Regulation No. 1806, para. 15")):
        st, u = call("GET", f"/act/{a['id']}", params={"lang": lang})
        sc = u["scenarios"]
        ok(f"{lang}: formula = null у всех трёх сценариев", all(sc[k]["formula"] is None for k in ("pml", "eml", "mfl")))
        ok(f"{lang}: legal_ref удержания — на языке акта", sc["retention"]["legal_ref"] == ref, sc["retention"]["legal_ref"])
        texts = [sc["definitions"], sc["retention"]["text"] or ""] + [x["label"] + " " + x["what"] + " " + x["pct_text"]
                                                                        for x in sc["tiles"]]
        ok(f"{lang}: определения, плитки и удержание без кириллицы", not any(re.search(r"[А-Яа-яЁё]", x) for x in texts),
           texts)


def check_minor():
    print("34. Мелочи: управляющие байты, проценты, подпись года")
    # app/act.py — фасад, код акта — в пакете app/act_pkg/ (04.10.2026): проверяем оба
    pkg = sorted((Path(act.__file__).parent / "act_pkg").glob("*.py"))
    src = b"".join(f.read_bytes() for f in [Path(act.__file__)] + pkg)
    bad = [b for b in src if (b < 32 and b not in (9, 10, 13)) or b == 127]
    ok("в app/act.py и app/act_pkg/ нет сырых управляющих символов", not bad and len(pkg) > 1, bad[:5])
    ok("сигнатура zip записана как b\"PK\\x03\\x04\"", b'b"PK\\x03\\x04"' in src)
    ok("_format_of по-прежнему узнаёт DOCX", act._format_of(docx_bytes(["Страховая сумма: 1 сум"])) == "docx")
    sc = {"available": True, "order": "classic", "class_code": "9", "rule": "property9", "k": 1,
          "items": {s: {"amount": p * 10, "pct": p, "what": "sc_w_c9", "state": "sc_state_" + s.lower()}
                    for s, p in (("PML", 15.0), ("EML", 37.5), ("MFL", 100.0))},
          "retention": {"known": False}, "assumptions": []}
    v = act._scenarios_view(sc, {"class_code": "9"}, "ru")["json"]
    ok("проценты рядом — одинаково: 15,0 % · 37,5 % · 100,0 %",
       [v[k]["pct_text"] for k in ("pml", "eml", "mfl")] == ["15,0 %", "37,5 %", "100,0 %"],
       [v[k]["pct_text"] for k in ("pml", "eml", "mfl")])
    sc["items"]["EML"]["pct"] = 40.0
    v = act._scenarios_view(sc, {"class_code": "9"}, "en")["json"]
    ok("все целые — без дроби: 15% · 40% · 100%", [v[k]["pct_text"] for k in ("pml", "eml", "mfl")] == ["15%", "40%", "100%"])
    fresh()
    st, a = call("POST", "/act/make", {"lang": "ru", "must": WH_MUST, "optional": WH_OPT})
    labels = [r["label"] for r in a["sections"][0]["rows"]]
    ok("здание: «Год постройки»", "Год постройки" in labels and "Год выпуска" not in labels, labels)
    for lang, want in (("uz", "Qurilgan yili"), ("en", "Year built")):
        st, u = call("GET", f"/act/{a['id']}", params={"lang": lang})
        ok(f"{lang}: здание — «{want}»", want in [r["label"] for r in u["sections"][0]["rows"]])
    st, a = call("POST", "/act/make", {"lang": "en", "must": CRANE_MUST, "optional": CRANE_OPT})
    labels = [r["label"] for r in a["sections"][0]["rows"]]
    ok("техника: «Year of manufacture»", "Year of manufacture" in labels and "Year built" not in labels, labels)


def check_screen():
    print("35. Экран: плитки сценариев из ответа сервера, сброс пометок «из документа»")
    html = (Path(act.__file__).parent / "tg.html").read_text(encoding="utf-8")
    body = html[html.index("function actScenHtml"):html.index("function actMeasuresHtml")]
    ok("плитки берутся из scenarios.tiles (порядок и подписи сервера)", "s.tiles" in body and "x.label" in body
       and "x.pct_text" in body, body[:300])
    ok("удержание: текст сервера (сравнение с EML)", "r.text" in body)
    pre = html[html.index("function wzApplyPrefill"):html.index("function preTag")]
    ok("wzApplyPrefill: пометка остаётся только у значений из нового ответа",
       "delete CH.pre[k]" in pre and "CH.preDoc" in pre, pre[-300:])
    call_site = html[html.index("CH.preDoc = {};"):html.index("function wzApplyPrefill")]
    ok("новая загрузка без prefill тоже сбрасывает пометки", "wzApplyPrefill(d.prefill && typeof d.prefill" in call_site
       and ": {})" in call_site, call_site)


# ------------------------------------------------------------------ 36. оценка по объявлениям (снимки экрана)

def upload_to(path, files, fields=None):
    boundary = "----insonmk"
    parts = []
    for k, v in (fields or {}).items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for name, mime, blob in files:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
                     f'filename="{name}"\r\nContent-Type: {mime}\r\n\r\n'.encode() + blob + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    payload = b"".join(parts)
    hdrs = [(b"host", b"test"), (b"content-type", f"multipart/form-data; boundary={boundary}".encode()),
            (b"content-length", str(len(payload)).encode())] + _cookie_hdr()
    return _send("POST", path, None, hdrs, payload)


RATE = 12650.0                            # подменённый курс ЦБ в тесте (сеть не используется)
TODAY = date.today()


def _days_ago(n):
    return (TODAY - timedelta(days=n)).isoformat()


def _days_ahead(n):
    return (TODAY + timedelta(days=n)).isoformat()


# 7 объявлений автокрана XCMG QY50K5D: одно другой модели, один выброс, одно в долларах, у одного даты нет.
# Лишние поля продавца и телефон в названии — модель нарушила запрет, сервер должен их убрать.
MARKET_REPLY = _json.dumps({"listings": [
    {"file": 1, "title": "XCMG QY50K5D 2021, звоните +998 90 123-45-67", "price": 2_650_000_000, "currency": "UZS",
     "year": 2021, "hours": 4200, "region": "Ташкент", "posted": "Сегодня 10:15", "posted_date": None,
     "site": "olx", "relevant": True, "seller": "Иванов Иван Иванович", "phone": "+998 90 123-45-67"},
    {"file": 1, "title": "Автокран XCMG QY50K5D", "price": "2 500 000 000 сум", "currency": "UZS", "year": 2020,
     "region": "Каримов Алишер Бахтиёрович", "posted": _days_ago(18), "site": "olx", "relevant": True},
    {"file": 2, "title": "XCMG QY50K5D 50 тонн", "price": 205000, "currency": "USD", "year": 2022,
     "region": "Самарканд", "posted": "вчера", "site": "olx", "relevant": True},
    {"file": 2, "title": "XCMG QY50K5D", "price": 2_400_000_000, "currency": "UZS", "year": 2019,
     "region": "Навои", "posted_date": _days_ago(41), "site": "olx", "relevant": True},
    {"file": 2, "title": "XCMG QY25K5D 25 тонн", "price": 1_500_000_000, "currency": "UZS", "year": 2021,
     "posted": "вчера", "site": "olx", "relevant": False, "why_excluded": "другая модель: QY25K5D"},
    {"file": 3, "title": "XCMG QY50K5D срочно", "price": 900_000_000, "currency": "UZS", "year": 2021,
     "posted": _days_ago(5), "site": "olx", "relevant": True},
    {"file": 3, "title": "XCMG QY50K5D 2023", "price": 2_800_000_000, "currency": "UZS", "year": 2023,
     "posted": "3 дня назад", "site": "olx", "relevant": True},
    # дубль с перекрывающегося снимка — должен схлопнуться
    {"file": 3, "title": "XCMG QY50K5D 2023", "price": 2_800_000_000, "currency": "UZS", "year": 2023,
     "posted": None, "site": "olx", "relevant": True},
    # L8: даты публикации на снимке нет — в расчёт не берётся; телефон в названии с хвостом «тел.»
    {"file": 3, "title": "XCMG QY50K5D, тел. +998 93 555 66 77", "price": 2_550_000_000, "currency": "UZS",
     "year": 2020, "posted": None, "site": "olx", "relevant": True},
    # L9: «7 месяцев назад» — старше 6 месяцев
    {"file": 3, "title": "XCMG QY50K5D 2018", "price": 2_000_000_000, "currency": "UZS", "year": 2018,
     "posted": "7 месяцев назад", "site": "olx", "relevant": True},
    # L10: дата позже даты снимков — некорректна
    {"file": 3, "title": "XCMG QY50K5D 2022 новый", "price": 2_700_000_000, "currency": "UZS", "year": 2022,
     "posted_date": _days_ahead(10), "site": "olx", "relevant": True},
]}, ensure_ascii=False)

SHOT_FILES = [("shot1.png", "image/png", image((240, 240, 240), w=390, h=844)),
              ("shot2.jpg", "image/jpeg", image((230, 230, 230), kind="jpg", w=390, h=844)),
              ("shot3.png", "image/png", image((250, 250, 250), w=390, h=844))]
PD_BITS = ("Иванов", "123-45-67", "123 45 67", "Бахтиёрович", "Каримов", "555 66 77")


def hand_q(xs, p):
    """Перцентиль «руками»: позиция p × (n − 1), линейно между соседями."""
    xs = sorted(xs)
    pos = p * (len(xs) - 1)
    lo = int(pos)
    return xs[lo] + (xs[min(lo + 1, len(xs) - 1)] - xs[lo]) * (pos - lo)


class NetSpy:
    """Подмена сетевого слоя: любое обращение записывается и отклоняется (сервер не должен ходить в сеть)."""

    def __init__(self):
        self.hits = []

    def __enter__(self):
        import socket
        import urllib.request
        from app import valuation_sources as vs
        self.saved = (socket.socket.connect, socket.create_connection, urllib.request.urlopen, vs._http_get,
                      vs.robots_check, llm._post)

        def rec(kind):
            def f(*a, **kw):
                self.hits.append((kind, repr(a[:2])[:200]))
                raise AssertionError("сеть запрещена в тесте: " + kind)
            return f
        orig_connect = self.saved[0]

        def connect(s, addr, *a):
            # asyncio на Windows соединяет внутренний socketpair через 127.0.0.1 — это не сеть
            if isinstance(addr, tuple) and addr[0] in ("127.0.0.1", "::1"):
                return orig_connect(s, addr, *a)
            return rec("socket")(addr)
        socket.socket.connect = connect
        socket.create_connection = rec("create_connection")
        urllib.request.urlopen = rec("urlopen")
        vs._http_get = rec("valuation_sources._http_get")
        vs.robots_check = rec("valuation_sources.robots_check")
        llm._post = rec("llm._post")
        return self

    def __exit__(self, *exc):
        import socket
        import urllib.request
        from app import valuation_sources as vs
        (socket.socket.connect, socket.create_connection, urllib.request.urlopen, vs._http_get,
         vs.robots_check, llm._post) = self.saved


def check_market_engine():
    print("36а. Оценка по объявлениям: чистая функция")
    sd = date(2026, 9, 30)
    P = ae.parse_posted
    ok("даты публикации: ISO, ДД.ММ.ГГГГ, «сегодня», «вчера», «3 дня назад», «12 сентября», bugun/kecha",
       P("2026-09-12", sd) == date(2026, 9, 12) and P("12.09.2026", sd) == date(2026, 9, 12)
       and P("Сегодня 10:15", sd) == sd and P("вчера", sd) == date(2026, 9, 29)
       and P("3 дня назад", sd) == date(2026, 9, 27) and P("12 сентября", sd) == date(2026, 9, 12)
       and P("15 октября", sd) == date(2025, 10, 15) and P("bugun", sd) == sd and P("kecha", sd) == date(2026, 9, 29)
       and P("2 мая 2026 г.", sd) == date(2026, 5, 2) and P("5 March 2026", sd) == date(2026, 3, 5))
    ok("дата позже снимка и мусор — не дата", P("2026-10-05", sd) is None and P("срочно", sd) is None
       and P(None, sd) is None)
    PX = ae.parse_posted_ex
    ok("«N … назад» по-русски: минуты, часы, дни, недели, месяцы, годы",
       P("10 минут назад", sd) == sd and P("5 часов назад", sd) == sd and P("30 часов назад", sd) == date(2026, 9, 29)
       and P("неделю назад", sd) == date(2026, 9, 23) and P("2 недели назад", sd) == date(2026, 9, 16)
       and P("7 месяцев назад", sd) == date(2026, 2, 28) and P("месяц назад", sd) == date(2026, 8, 30)
       and P("2 года назад", sd) == date(2024, 9, 30) and P("5 лет назад", sd) == date(2021, 9, 30))
    ok("«N … oldin» по-узбекски и «N … ago» по-английски",
       P("15 daqiqa oldin", sd) == sd and P("3 soat oldin", sd) == sd and P("3 kun oldin", sd) == date(2026, 9, 27)
       and P("2 hafta oldin", sd) == date(2026, 9, 16) and P("2 oy oldin", sd) == date(2026, 7, 30)
       and P("1 yil oldin", sd) == date(2025, 9, 30) and P("20 minutes ago", sd) == sd
       and P("an hour ago", sd) == sd and P("3 days ago", sd) == date(2026, 9, 27)
       and P("2 weeks ago", sd) == date(2026, 9, 16) and P("3 months ago", sd) == date(2026, 6, 30)
       and P("a year ago", sd) == date(2025, 9, 30))
    ok("дата позже снимка — «некорректна» (future), не «не видна»",
       PX("2026-10-05", sd) == (None, "future") and PX("срочно", sd) == (None, "none") and PX(None, sd) == (None, "none"))
    L = [{"id": "a", "title": "x", "price": 100, "currency": "UZS", "posted": "2026-09-01", "relevant": True},
         {"id": "b", "title": "x", "price": 110, "currency": "UZS", "posted": "2026-03-01", "relevant": True},
         {"id": "c", "title": "x", "price": 10, "currency": "USD", "posted": "вчера", "relevant": True},
         {"id": "d", "title": "x", "price": None, "currency": "UZS", "posted": "2026-09-01", "relevant": True}]
    r = ae.market_estimate(L, declared=100, shot_date=sd)
    codes = {e["id"]: e["code"] for e in r["excluded"]}
    ok("старше 6 месяцев, без цены, в долларах без курса — исключены с причиной",
       codes == {"b": "mx_too_old", "c": "mx_no_rate", "d": "mx_no_price"}, codes)
    ok("одно объявление — оценка ориентировочная (few)", r["verdict"] == "few" and r["median"] == 100, r["verdict"])
    r = ae.market_estimate(L, declared=100, shot_date=sd, usd_rate=11)
    ok("с курсом доллар пересчитан и вошёл", r["used"] == 2 and r["median"] == 105 and not r["date_assumed"],
       (r["used"], r.get("date_assumed")))
    # правило проекта (valuation_sources): объявление без даты публикации в расчёт не берётся
    U = [dict(L[0]), {"id": "u", "title": "x", "price": 104, "currency": "UZS", "posted": None, "relevant": True},
         {"id": "f", "title": "x", "price": 102, "currency": "UZS", "posted_date": "2026-10-03", "relevant": True},
         {"id": "o", "title": "x", "price": 101, "currency": "UZS", "posted": "7 месяцев назад", "relevant": True}]
    r = ae.market_estimate(U, declared=100, shot_date=sd)
    codes = {e["id"]: e["code"] for e in r["excluded"]}
    ok("без даты — «дата публикации не видна»; позже снимка — «некорректна»; «7 месяцев назад» — старше 6 мес.",
       codes == {"u": "mx_no_date", "f": "mx_bad_date", "o": "mx_too_old"} and r["used"] == 1, codes)
    ok("причины словами", tx.t("mx_no_date", "ru") == "дата публикации не видна"
       and "некорректна" in tx.t("mx_bad_date", "ru", shot="30.09.2026"))
    r = ae.market_estimate(U, declared=100, shot_date=sd, settings={"market": {"allow_undated": True}})
    ok("настройка allow_undated = true: без даты — дата снимка с пометкой",
       r["used"] == 2 and r["date_assumed"] == ["u"] and r["how"][0]["code"] == "mh_filter_undated", r["date_assumed"])
    ok("allow_undated по умолчанию false и проверяется как true/false",
       ae.DEFAULT_SETTINGS["market"]["allow_undated"] is False and ae.check_settings({"market": {"allow_undated": 1}})
       and not ae.check_settings({"market": {"allow_undated": True}}))
    # выбросы при числе подходящих меньше min_listings не ищутся
    two = [{"id": "p", "price": 100, "posted": "вчера"}, {"id": "q", "price": 1000, "posted": "вчера"}]
    r = ae.market_estimate(two, shot_date=sd)
    ok("2 объявления < 3: выбросы не ищутся (1000 при медиане 550 не выброс)",
       r["used"] == 2 and not r["excluded"] and any(h["code"] == "mh_outliers_skipped" for h in r["how"]), r["how"])
    # пустые и нулевые цены не роняют расчёт
    bad = [{"id": "z0", "price": 0}, {"id": "z1", "price": ""}, {"id": "z2", "price": "abc"},
           {"id": "z3", "price": -5}, {"id": "z4"}, "мусор"]
    r = ae.market_estimate(bad, declared=10, shot_date=sd, usd_rate="x")
    ok("пустые, нулевые и мусорные цены — «цена не видна», без падения",
       r["verdict"] == "none" and {e["code"] for e in r["excluded"]} == {"mx_no_price"} and len(r["excluded"]) == 5, r)
    # округление медианы — «половина вверх» (как Math.round на экране), а не банковское round()
    r = ae.market_estimate([{"id": "h1", "price": 2, "posted": "вчера"}, {"id": "h2", "price": 3, "posted": "вчера"}],
                           shot_date=sd)
    ok("медиана 2,5 → 3 (половина вверх; round() дал бы 2)", r["median"] == 3 and ae.round_half_up(2_593_249_999.5)
       == 2_593_250_000 and ae.round_half_up(0.5) == 1, r["median"])
    r = ae.market_estimate([dict(L[0], relevant=False)], declared=100, shot_date=sd)
    ok("ни одного подходящего — none, оценки нет", r["verdict"] == "none" and not r["available"], r["verdict"])
    st = ae.merge_settings({"market": {"min_listings": 1, "diff_pct": 5}})
    r = ae.market_estimate(L[:1], declared=104, shot_date=sd, settings=st)
    ok("порог расхождения — настройка (4 % < 5 % — confirmed)", r["verdict"] == "confirmed", r)
    r = ae.market_estimate(L[:1], declared=120, sum_insured=120, shot_date=sd, settings=st)
    ok("расхождение 16,7 % > 5 % — refine; страховая сумма сверяется с уточнённой (ГК 938)",
       r["verdict"] == "refine" and r["refined_value"] == 100 and r["insured_check"]["verdict"] == "over"
       and r["insured_check"]["legal_ref"] == "ГК РУз, ст. 938", r)
    ok("настройки оценки проверяются", ae.check_settings({"market": {"min_listings": 0}})
       and ae.check_settings({"market": {"outlier_low": 1.5}}) and ae.check_settings({"market": {"zzz": 1}})
       and not ae.check_settings({"market": {"min_listings": 4, "diff_pct": 20}}))
    ok("по умолчанию: 3 объявления, 15 %, 6 месяцев, выбросы 0,5 и 2, без даты — нельзя",
       ae.DEFAULT_SETTINGS["market"] == {"min_listings": 3, "diff_pct": 15, "max_age_months": 6, "outlier_low": 0.5,
                                         "outlier_high": 2.0, "allow_undated": False})
    # чистка названия: после вырезанного телефона не остаётся «, звоните» / «тел.»
    ok("хвосты «звоните», «тел.», «звонить» убираются вместе с телефоном",
       am.clean_text("XCMG QY50K5D 2021, звоните +998 90 123-45-67", 160)[0] == "XCMG QY50K5D 2021"
       and am.clean_text("XCMG, тел. +998 91 555 44 33", 160)[0] == "XCMG"
       and am.clean_text("Автокран +998 90 111 22 33 звонить", 160)[0] == "Автокран"
       and am.clean_text("Кран Мотель", 160)[0] == "Кран Мотель")
    ok("ссылка объявления: только https на четырёх площадках",
       am.safe_url("https://www.olx.uz/d/obyavlenie/x") and am.safe_url("https://m.avtoelon.uz/a/1")
       and not am.safe_url("http://169.254.169.254/latest/meta-data") and not am.safe_url("https://olx.uz.evil.com/")
       and not am.safe_url("https://u:p@olx.uz/") and not am.safe_url("https://example.uz/ad/1")
       and not am.safe_url("https://olx.uz:8443/"))
    # сбой robots.txt помнится 10 минут, а не до перезапуска (app/valuation_sources.py)
    from app import valuation_sources as vs
    hits = []

    def refuse(url, timeout=0):
        hits.append(url)
        raise OSError("нет сети")
    saved = vs._http_get
    vs._http_get = refuse
    try:
        vs._robots_cache.pop("robots-test.example", None)
        a1 = vs.robots_check("https://robots-test.example/x")
        a2 = vs.robots_check("https://robots-test.example/y")
        rp, why, at = vs._robots_cache["robots-test.example"]
        vs._robots_cache["robots-test.example"] = (rp, why, at - vs.ROBOTS_FAIL_TTL_SEC - 1)
        vs.robots_check("https://robots-test.example/z")
        ok("robots.txt: сбой запомнен на 10 минут, потом повтор", not a1[0] and not a2[0] and len(hits) == 2
           and vs.ROBOTS_FAIL_TTL_SEC == 600, hits)
    finally:
        vs._http_get = saved
        vs._robots_cache.pop("robots-test.example", None)


def check_market_links():
    print("36б. Ссылки поиска: сервер только составляет адреса")
    st, b = call("GET", "/act/market/links", params={"brand": "XCMG", "model": "QY50K5D", "year": "2021",
                                                     "object_kind": "truck_crane", "lang": "ru"})
    urls = {(ln["site"], ln["url"]) for ln in b.get("links") or []}
    ok("OLX: общий поиск по марке и модели (раздела спецтехники нет в valuation_sources)",
       ("olx", "https://www.olx.uz/list/q-xcmg-qy50k5d/") in urls, urls)
    ok("OLX: вторая ссылка с годом", ("olx", "https://www.olx.uz/list/q-xcmg-qy50k5d-2021/") in urls)
    ok("avtoelon.uz: раздел автокранов по марке (valuation.SPEC_SECTIONS)",
       ("avtoelon", "https://avtoelon.uz/spectehnika/gruzovaja-tehnika/avtokran/xcmg/") in urls, urls)
    ok("у каждой ссылки подпись и подсказка", all(ln["label"] and ln["hint"] for ln in b["links"]))
    ok("подсказка: что снять — 5–10 объявлений, до 5 снимков", "5–10" in b["hint"] and b["max_shots"] == 5
       and len(b["shot_tips"]) == 4 and any("телефон" in x for x in b["shot_tips"]), b.get("hint"))
    ok("сервер по ссылкам не ходит — сказано явно", "не ходит" in b["note"])
    st, b = call("GET", "/act/market/links", params={"object_kind": "warehouse", "lang": "ru"})
    urls = [(ln["site"], ln["url"]) for ln in b["links"]]
    ok("недвижимость: OLX в разделе «Недвижимость», запрос по-русски в кодировке, uybor и joymee",
       urls[0] == ("olx", "https://www.olx.uz/nedvizhimost/q-%D1%81%D0%BA%D0%BB%D0%B0%D0%B4/")
       and {"uybor", "joymee"} <= {s for s, _ in urls} and "avtoelon" not in {s for s, _ in urls}, urls)
    st, b = call("GET", "/act/market/links", params={"brand": "Chevrolet", "model": "Cobalt", "object_kind": "car"})
    urls = {(ln["site"], ln["url"]) for ln in b["links"]}
    ok("легковой: OLX в разделе легковых, avtoelon /avto/марка/модель/",
       ("olx", "https://www.olx.uz/transport/legkovye-avtomobili/q-chevrolet-cobalt/") in urls
       and ("avtoelon", "https://avtoelon.uz/avto/chevrolet/cobalt/") in urls, urls)
    st, b = call("GET", "/act/market/links", params={"brand": "Шакман", "model": "SX3258", "object_kind": "truck"})
    urls = {(ln["site"], ln["url"]) for ln in b["links"]}
    ok("кириллица: OLX — кодирование, avtoelon — транслитерация",
       ("olx", "https://www.olx.uz/list/q-%D1%88%D0%B0%D0%BA%D0%BC%D0%B0%D0%BD-sx3258/") in urls
       and ("avtoelon", "https://avtoelon.uz/spectehnika/") in urls, urls)
    st, b = call("GET", "/act/market/links", params={"lang": "ru"})
    ok("искать не по чему — 422 с понятной причиной", st == 422 and "марку" in b["detail"], (st, b))
    st, b = call("GET", "/act/market/links", params={"year": "1800", "brand": "XCMG"})
    ok("год проверяется", st == 422 and "year" in b["errors"], (st, b))
    for lang in ("uz", "en"):
        st, b = call("GET", "/act/market/links", params={"brand": "XCMG", "model": "QY50K5D", "year": "2021",
                                                         "object_kind": "truck_crane", "lang": lang})
        txt = " ".join([b["hint"], b["note"]] + b["shot_tips"] + [ln["label"] + ln["hint"] for ln in b["links"]])
        ok(f"{lang}: подписи и подсказки без кириллицы", st == 200 and not re.search(r"[А-Яа-яЁё]", txt), txt[:300])


def market_setup(rate=RATE):
    from app import valuation_sources as vs
    act._FX_CACHE.clear()
    ORIG.setdefault("cbu", vs.cbu_usd_rate)
    vs.cbu_usd_rate = (lambda d: rate)


def check_market_shots():
    print("36в. Снимки объявлений: одно чтение моделью, валюта, ПД, лимиты")
    fresh()
    clear_settings()
    market_setup()
    model_on(True)
    REPLY["text"] = MARKET_REPLY
    CALLS.clear()
    with NetSpy() as spy:
        st, b = upload_to("/act/market/shots", SHOT_FILES, {"lang": "ru", "site": "olx", "brand": "XCMG",
                                                            "model": "QY50K5D", "object_kind": "truck_crane"})
        st_l, _ = call("GET", "/act/market/links", params={"brand": "XCMG", "model": "QY50K5D"})
    ok("снимки приняты", st == 200 and b.get("ok") and b.get("shots_session"), (st, b))
    ok("ни одного сетевого запроса (к olx.uz — тем более)", not spy.hits and st_l == 200, spy.hits)
    ok("один запрос к модели со всеми снимками, 20 с, без повторов",
       len(CALLS) == 1 and len(CALLS[0]["files"]) == 3 and CALLS[0]["timeout"] == 20 and CALLS[0]["retries"] == 0,
       [(c["purpose"], c["timeout"], c["retries"]) for c in CALLS])
    prompt = CALLS[0]["messages"][0]["content"] + CALLS[0]["messages"][1]["content"]
    ok("в инструкции: не выдумывай, null, запрет на имена и телефоны, строгая схема",
       "не выдумывай" in prompt and "null" in prompt and "телефоны" in prompt and '"listings"' in prompt)
    ok("в инструкции дата снимка и что ищем", TODAY.isoformat() in prompt and "qy50k5d" in prompt.lower())
    ok("маскировка ПД не портит инструкцию", llm.mask_pd(prompt) == prompt)
    L = {r["id"]: r for r in b["listings"]}
    ok("10 объявлений (дубль с перекрывающегося снимка схлопнут)", len(L) == 10, list(L))
    ok("доллары пересчитаны по курсу ЦБ", L["L3"]["price_uzs"] == round(205000 * RATE) and L["L3"]["currency"] == "USD",
       L["L3"])
    ok("курс показан явно: ЦБ РУз, с датой", b["fx"] and b["fx"]["by"] == "cbu" and b["fx"]["rate"] == RATE
       and "cbu.uz" in b["fx"]["text"], b.get("fx"))
    ok("цена «2 500 000 000 сум» строкой — число", L["L2"]["price"] == 2_500_000_000)
    ok("другая модель — relevant = false с причиной", L["L5"]["relevant"] is False and "QY25K5D" in
       (L["L5"]["why_excluded"] or ""))
    ok("«сегодня» и «вчера» — даты от даты снимка", L["L1"]["posted_date"] == TODAY.isoformat()
       and L["L3"]["posted_date"] == _days_ago(1))
    ok("«3 дня назад» — дата от даты снимка", L["L7"]["posted_date"] == _days_ago(3), L["L7"])
    ok("дата не видна — «в расчёт не берётся»", L["L8"]["date_assumed"] and not L["L8"]["used"]
       and L["L8"]["date_note"] == "дата публикации не видна — в расчёт не берётся", L["L8"])
    ok("дата позже снимка — «некорректна»", L["L10"]["date_status"] == "future" and "некорректна" in L["L10"]["date_note"],
       L["L10"])
    ok("«7 месяцев назад» — дата посчитана", L["L9"]["posted_date"] == ae.months_before(TODAY, 7).isoformat(), L["L9"])
    dump = _json.dumps(b, ensure_ascii=False)
    ok("имена и телефоны продавцов не возвращаются", not any(x in dump for x in PD_BITS),
       [x for x in PD_BITS if x in dump])
    ok("название осталось, телефон и «звоните» вырезаны", L["L1"]["title"] == "XCMG QY50K5D 2021", L["L1"])
    ok("«тел.» без номера тоже убрано", L["L8"]["title"] == "XCMG QY50K5D", L["L8"])
    ok("предупреждение правдивое: не извлекаются, хранятся 24 часа",
       "не извлекаются" in b["warning"] and "24 часа" in b["warning"] and "не сохраняются" not in b["warning"],
       b["warning"])
    ok("регион с ФИО — отброшен", L["L2"]["region"] is None)
    ok("сказано, что данные продавцов убраны", any("убрано" in n for n in b["notes"]), b["notes"])
    # предварительная оценка — руками: L5 другой модели, L6 (900 млн) — выброс
    cand = [2_650_000_000, 2_500_000_000, round(205000 * RATE), 2_400_000_000, 900_000_000, 2_800_000_000]
    med0 = (sorted(cand)[2] + sorted(cand)[3]) / 2
    used = [x for x in cand if 0.5 * med0 <= x <= 2 * med0]
    ok("выброс руками: 900 млн < 0,5 × медианы", 900_000_000 < 0.5 * med0 and len(used) == 5, (med0, used))
    e = b["estimate"]
    ok("медиана и вилка пересчитаны руками", e["median"] == round(hand_q(used, 0.5)) == 2_593_250_000
       and e["low"] == round(hand_q(used, 0.25)) == 2_500_000_000
       and e["high"] == round(hand_q(used, 0.75)) == 2_650_000_000, e)
    ok("исключены: L5 (другая модель), L6 (выброс), L8 (без даты), L9 (старше 6 мес.), L10 (дата позже снимка)",
       sorted((x["id"], x["code"]) for x in e["excluded"]) == [
           ("L10", "mx_bad_date"), ("L5", "mx_not_relevant"), ("L6", "mx_outlier_low"), ("L8", "mx_no_date"),
           ("L9", "mx_too_old")], e["excluded"])
    ok("без заявленной стоимости — verdict ready", e["verdict"] == "ready" and e["calibrated"] == 0)
    ok("ссылки поиска в ответе", any(ln["site"] == "olx" for ln in b["links"]))
    with db.tx() as con:
        row = db.rows(con, "SELECT result_json, expires_at, created_at FROM act_uploads WHERE id=?", b["shots_session"])
        j = db.rows(con, "SELECT detail FROM audit WHERE entity=?", "act_upload:" + b["shots_session"])
    stored = row[0]["result_json"]
    ok("в базе нет данных продавцов", not any(x in stored for x in PD_BITS), stored[:300])
    ok("снимки хранятся 24 часа", (datetime.fromisoformat(row[0]["expires_at"]) -
                                   datetime.fromisoformat(row[0]["created_at"])) == timedelta(hours=24))
    detail = j[0]["detail"] if j else ""
    ok("в журнале только счётчики", j and "XCMG" not in detail and "2650000000" not in detail
       and '"listings": 10' in detail, detail)
    sid = b["shots_session"]

    # курс ЦБ: свой пул (не пул модели), общий срок 5 с, неудача помнится 10 минут
    from app import valuation_sources as vs
    ok("курс — в своём пуле, срок 5 с, неудача — 10 минут", act._FX_POOL is not act._AI_POOL
       and act.FX_DEADLINE_SEC == 5 and act.FX_FAIL_TTL_SEC == 600)
    calls, names = [], []

    def cbu_down(d):
        import threading as _thr
        calls.append(d)
        names.append(_thr.current_thread().name)
        return None
    vs.cbu_usd_rate = cbu_down
    act._FX_CACHE.clear()
    r1 = act._fx_submit(TODAY).result(5)
    r2 = act._fx_submit(TODAY).result(5)
    ok("неудача курса закэширована: второй раз cbu.uz не спрашивается", r1["rate"] is None and r2["rate"] is None
       and len(calls) == 1, calls)
    res, until = act._FX_CACHE[TODAY.isoformat()]
    act._FX_CACHE[TODAY.isoformat()] = (res, until - act.FX_FAIL_TTL_SEC - 1)
    act._fx_submit(TODAY).result(5)
    ok("через 10 минут курс спрашивается снова", len(calls) == 2, calls)
    ok("курс запрашивается в потоке act-fx, а не в потоке модели", names and all(n.startswith("act-fx") for n in names),
       names)
    import threading as _th
    import time as _time
    slow_gate = _th.Event()

    def cbu_slow(d):
        slow_gate.wait(8)
        return RATE
    vs.cbu_usd_rate = cbu_slow
    act._FX_CACHE.clear()
    fresh()
    t0 = _time.monotonic()
    st, b2 = upload_to("/act/market/shots", SHOT_FILES[:1], {"lang": "ru", "brand": "XCMG", "model": "QY50K5D"})
    spent = _time.monotonic() - t0
    slow_gate.set()
    act._FX_INFLIGHT[TODAY.isoformat()].result(10)        # запоздавший ответ ЦБ ложится в кэш — дождёмся его
    ok("медленный cbu.uz: загрузка не ждёт дольше 5 с, курс не выдуман", st == 200 and b2["fx"] is None
       and spent < 7.5 and b2["usd_rate_needed"], (st, spent, b2.get("fx")))
    market_setup()

    # курс не получен: доллары не считаются, нужен курс сотрудника; с ним — «введён сотрудником»
    market_setup(rate=None)
    fresh()
    st, b = upload_to("/act/market/shots", SHOT_FILES[:1], {"lang": "ru", "brand": "XCMG", "model": "QY50K5D"})
    L = {r["id"]: r for r in b["listings"]}
    ok("без курса: цена в долларах не пересчитана, просьба указать курс", L["L3"]["price_uzs"] is None
       and b["usd_rate_needed"] and any("курс" in n for n in b["notes"]) and b["fx"] is None, b.get("notes"))
    ok("без курса: объявление в долларах — причина «курс не задан»",
       ("L3", "mx_no_rate") in [(x["id"], x["code"]) for x in b["estimate"]["excluded"]])
    st, b = upload_to("/act/market/shots", SHOT_FILES[:1], {"lang": "ru", "usd_rate": "12 600"})
    ok("курс сотрудника принят и показан", b["fx"] and b["fx"]["by"] == "employee" and b["fx"]["rate"] == 12600
       and {r["id"]: r for r in b["listings"]}["L3"]["price_uzs"] == 205000 * 12600, b.get("fx"))
    st, b = upload_to("/act/market/shots", SHOT_FILES[:1], {"lang": "ru", "usd_rate": "12"})
    ok("курс с опечаткой — 422", st == 422 and "usd_rate" in b["errors"], (st, b))
    market_setup()

    # пределы: больше 5 снимков, не картинка, гостевой лимит по файлам, модель выключена
    st, b = upload_to("/act/market/shots", SHOT_FILES * 2, {"lang": "ru"})
    ok("больше 5 снимков — 413", st == 413 and "5" in b["detail"], (st, b))
    st, b = upload_to("/act/market/shots", [("x.pdf", "application/pdf", pdf_pages(1))], {"lang": "ru"})
    ok("PDF как снимок не принимается", st == 422 and "JPG" in b["rejected"][0]["error"], (st, b))
    st, b = upload_to("/act/market/shots", [("big.png", "image/png", png_bomb(9000, 9000))], {"lang": "ru"})
    ok("размер в пикселях проверяется до раскрытия", st == 422 and "Мп" in b["rejected"][0]["error"], (st, b))
    st, b = upload_to("/act/market/shots", SHOT_FILES[:1], {"lang": "ru", "site": "avito"})
    ok("площадка — из списка", st == 422 and "site" in b["errors"])
    fresh()
    set_limits(guest_photos_per_hour=4)
    try:
        st1, _ = upload_to("/act/market/shots", SHOT_FILES, {"lang": "ru"})
        st2, b2 = upload_to("/act/market/shots", SHOT_FILES[:2], {"lang": "ru"})
        ok("гостевой лимит считается по файлам (3 + 2 > 4)", st1 == 200 and st2 == 429, (st1, st2))
    finally:
        clear_settings()
        fresh()
    set_limits(ai_calls_per_hour=1)
    try:
        CALLS.clear()
        call_ok, _ = upload_to("/act/market/shots", SHOT_FILES[:1], {"lang": "ru"})
        st, b = upload_to("/act/market/shots", SHOT_FILES[:1], {"lang": "ru"})
        ok("общий предел обращений к модели — общий с распознаванием фото",
           st == 200 and not b["ai"] and "лимит распознаваний" in b["message"] and len(CALLS) == 1, b.get("message"))
    finally:
        clear_settings()
        fresh()
    model_on(False)
    st, b = upload_to("/act/market/shots", SHOT_FILES[:1], {"lang": "ru"})
    ok("модель выключена: снимки сохранены, честная причина, можно ввести вручную",
       st == 200 and not b["ai"] and "вручную" in b["message"] and b["listings"] == [], b.get("message"))
    model_on(True)
    return sid


def mk_make(listings, sid=None, must=None, lang="ru", usd_rate=None, optional=None, fx=None):
    opt = dict(optional or CRANE_OPT, object_kind="truck_crane")
    opt["market"] = {"listings": listings, "shots_session": sid}
    if usd_rate is not None:
        opt["market"]["usd_rate"] = usd_rate
    if fx is not None:
        opt["market"]["fx"] = fx
    return call("POST", "/act/make", {"lang": lang, "must": must or CRANE_MUST, "optional": opt,
                                       "recognized": [{"key": "brand", "value": "XCMG", "source": "input"},
                                                      {"key": "model", "value": "QY50K5D", "source": "input"}]})


def sec3(a):
    return a["sections"][2]


def flat(s):
    """Текст без неразрывных пробелов: так сравнивать суммы в строках акта удобнее."""
    return re.sub(r"\s+", " ", str(s or "").replace(" ", " "))


def docx_plain(aid, lang="ru"):
    st, blob, h = call("GET", f"/act/{aid}.docx", params={"lang": lang}, raw=True)
    return flat(re.sub(r"<[^>]+>", "", zipfile.ZipFile(io.BytesIO(blob)).read("word/document.xml").decode("utf-8")))


def pdf_plain(aid, lang="ru"):
    st, blob, h = call("GET", f"/act/{aid}.pdf", params={"lang": lang}, raw=True)
    return flat(pdf_text(pymupdf.open(stream=blob, filetype="pdf")))


def screen_rows(stored):
    """Объявления так, как их отдаёт экран в /act/make (mkBody): с датой, площадкой и происхождением."""
    out = []
    for r in stored["listings"]:
        o = {k: r.get(k) for k in ("id", "title", "price", "currency", "year", "relevant", "region")}
        if r.get("posted_date"):
            o["posted_date"] = r["posted_date"]
        o.update(date_assumed=not r.get("posted_date"), site=r.get("site") or "olx", source="shot")
        out.append(o)
    return out


def check_market_make(sid):
    print("36г. Акт: блок оценки по объявлениям, решение, правки сотрудника, языки, Word и PDF")
    fresh()
    market_setup()
    CALLS.clear()
    with db.tx() as con:
        stored = _json.loads(db.rows(con, "SELECT result_json FROM act_uploads WHERE id=?", sid)[0]["result_json"])
    listings = screen_rows(stored)
    with NetSpy() as spy:
        st, a = mk_make(listings, sid)
    ok("акт с оценкой сформирован без сети и без модели", st == 200 and not spy.hits and not CALLS, (st, spy.hits))
    mv = a["market_value"]
    used = [2_650_000_000, 2_500_000_000, round(205000 * RATE), 2_400_000_000, 2_800_000_000]
    med = hand_q(used, 0.5)
    diff = (3_100_000_000 - med) / 3_100_000_000 * 100
    ok("медиана, вилка, число объявлений — руками", mv["median"] == round(med) and mv["low"] == 2_500_000_000
       and mv["high"] == 2_650_000_000 and mv["count"] == 10 and mv["used"] == 5, mv)
    ok("расхождение 16,3 % > 15 % — refine, уточнённая стоимость = медиана",
       mv["verdict"] == "refine" and mv["diff_pct"] == round(diff, 1) == 16.3 and mv["refined_value"] == round(med),
       (mv["verdict"], mv["diff_pct"]))
    ic = mv["insured_check"]
    ok("страховая сумма сверяется с уточнённой: превышение, ГК ст. 938",
       ic["verdict"] == "over" and ic["diff"] == 2_945_000_000 - round(med) and "938" in ic["legal_ref"]
       and ic["ratio_pct"] == round(2_945_000_000 / round(med) * 100, 2) == 113.56, ic)
    ok("источник: OLX, дата снимков, загружены сотрудником",
       mv["source_label"] == f"Источник: OLX, объявления на {TODAY.strftime('%d.%m.%Y')}, снимки загружены "
                             f"сотрудником.", mv["source_label"])
    ok("ссылки поиска и «как посчитано», calibrated = 0", mv["links"] and len(mv["how"]) >= 5
       and mv["calibrated"] == 0 and any("перцентил" in h for h in mv["how"]), mv["how"])
    ok("отброшенные с причинами", sorted(e["id"] for e in mv["excluded"]) == ["L10", "L5", "L6", "L8", "L9"]
       and all(e["reason"] for e in mv["excluded"]))
    ok("без правок: «Правки сотрудника: правок нет», медиана без правок та же, проверки правок нет",
       "Правки сотрудника: правок нет." in mv["source_lines"] and mv["edits"]["items"] == []
       and mv["median_original"] == mv["median"]
       and not any(c.startswith("Проверить правки") for c in a["decision"]["checks"]), mv["source_lines"])
    s3 = sec3(a)
    labels = [r["label"] for r in s3["rows"]]
    ok("раздел 3: «Оценка по объявлениям», медиана, вилка, вывод, уточнённая стоимость",
       {"Оценка по объявлениям", "Медиана цен объявлений", "Вилка (25–75-й перцентиль)", "Вывод по объявлениям",
        "Уточнённая стоимость (по объявлениям)", "Страховая сумма к уточнённой стоимости"} <= set(labels), labels)
    ok("раздел 3: строка источника и ссылка поиска под оценкой",
       s3["source_lines"][0] == mv["source_label"] and any(x.startswith("Ссылка поиска: https://www.olx.uz/")
                                                           for x in s3["source_lines"]), s3["source_lines"])
    ok("раздел 3: прежние строки на месте", labels[:4] == ["Страховая сумма", "Стоимость объекта",
                                                           "Отношение суммы к стоимости", "Вывод"], labels)
    # один итоговый вывод вместо «в норме» рядом с «превышением» (п. 7 замечаний)
    want = ("К заявленной стоимости страховая сумма составляет 95 % — в норме. Но по объявлениям стоимость ниже "
            "заявленной на 16,3 %: к уточнённой стоимости страховая сумма составляет 113,56 % — превышение (ГК РУз, "
            "ст. 938). Итог: стоимость нужно уточнить.")
    ok("вывод раздела 3 — один: к заявленной, к уточнённой, итог", flat(s3["rows"][3]["value"]) == want,
       flat(s3["rows"][3]["value"]))
    ok("value.final_verdict = refine, text — тот же итог; поле verdict прежнее",
       a["value"]["final_verdict"] == "refine" and flat(a["value"]["text"]) == want and a["value"]["verdict"] == "normal"
       and a["value"]["refined_ratio_pct"] == 113.56, a["value"])
    ok("в разделе 3 нет второго, противоположного вывода «В норме: …»",
       not any(flat(r["value"]).startswith("В норме") for r in s3["rows"]), [r["value"] for r in s3["rows"]])
    chk = a["decision"]["checks"]
    ok("решение: «уточнить стоимость объекта: по объявлениям …» и снизить сумму",
       any(c.startswith("Уточнить стоимость объекта: по объявлениям (5 шт.)") for c in chk)
       and any("выше уточнённой стоимости" in c for c in chk) and a["decision"]["code"] != "accept", chk)
    aid = a["id"]

    # Word и PDF
    plain = docx_plain(aid)
    ok("DOCX: раздел 3 одной строкой — «стоимость нужно уточнить», проверка в заключении; медиана, источник и "
       "правки — только в JSON и на экране", "Стоимость нужно уточнить" in plain
       and any(c.startswith("Уточнить стоимость объекта") for c in a["decision"]["checks"])
       and "Медиана цен объявлений" not in plain
       and "Правки сотрудника" not in plain and "Правки сотрудника: правок нет." in sec3(a)["source_lines"], plain[:300])
    text = pdf_plain(aid)
    ok("PDF: то же — короткий вывод, без медианы и строки источника", "Стоимость нужно уточнить" in text
       and "Медиана цен объявлений" not in text and "снимки загружены сотрудником" not in text, text[:200])

    # три языка
    for lang, word in (("uz", "Eʼlonlar boʻyicha baholash"), ("en", "Valuation by listings")):
        st, b = call("GET", f"/act/{aid}", params={"lang": lang})
        s = sec3(b)
        txt = [r["label"] for r in s["rows"]] + [str(r["value"]) for r in s["rows"][3:]] + s["source_lines"] + \
            [li["title"] for li in s["lists"]] + s["lists"][0]["items"] + b["decision"]["checks"] + \
            [b["market_value"]["verdict_text"], b["market_value"]["source_label"], b["value"]["text"]]
        cyr = [x for x in txt if re.search(r"[А-Яа-яЁё]", x or "")]
        ok(f"{lang}: блок оценки и итоговый вывод на языке акта, без кириллицы",
           word in [r["label"] for r in s["rows"]] and not cyr, cyr[:4])
    st, blob, h = call("GET", f"/act/{aid}.pdf", params={"lang": "en"}, raw=True)
    ok("PDF en: короткий вывод раздела 3", "The value needs checking" in pdf_text(pymupdf.open(stream=blob,
                                                                                               filetype="pdf")))

    # п. 1: сотрудник снял галочки с двух самых дорогих объявлений (L7 — 2,8 млрд, L1 — 2,65 млрд)
    off2 = [dict(r, relevant=False) if r["id"] in ("L1", "L7") else dict(r) for r in listings]
    st, a = mk_make(off2, sid)
    mv = a["market_value"]
    used2 = [2_500_000_000, round(205000 * RATE), 2_400_000_000]
    reasons = {e["id"]: e for e in mv["excluded"]}
    ok("снятые галочки: причина «снято сотрудником», а не «другое изделие»",
       reasons["L1"]["code"] == reasons["L7"]["code"] == "mx_unchecked_by_employee"
       and "снято сотрудником" in reasons["L1"]["reason"] and "другое изделие" not in reasons["L1"]["reason"]
       and reasons["L5"]["code"] == "mx_not_relevant" and "другое изделие" in reasons["L5"]["reason"], reasons)
    ok("медиана без двух дорогих — руками; медиана без правок — как прочитала модель",
       mv["median"] == round(hand_q(used2, 0.5)) == 2_500_000_000 and mv["median_original"] == round(med), mv)
    ok("market_value.edits: снято 2, остальное 0, список правок",
       {k: mv["edits"][k] for k in ("unchecked", "checked", "price_changed", "removed", "manual")}
       == {"unchecked": 2, "checked": 0, "price_changed": 0, "removed": 0, "manual": 0}
       and sorted((e["id"], e["what"]) for e in mv["edits"]["items"]) == [("L1", "unchecked"), ("L7", "unchecked")],
       mv["edits"])
    line = "Правки сотрудника: снято 2, включено 0, исправлено цен 0, убрано 0, добавлено вручную 0."
    ok("раздел 3: строка «Правки сотрудника: снято 2, …» и список правок",
       line in sec3(a)["source_lines"] and any(li["title"] == "Правки сотрудника в объявлениях"
                                                for li in sec3(a)["lists"]), sec3(a)["source_lines"])
    lv = {r["id"]: r for r in mv["listings"]}
    ok("у объявления видно «снято сотрудником»", lv["L1"]["off_by"] == "employee"
       and lv["L1"]["edit_note"] == "снято сотрудником", lv["L1"])
    chk = [flat(c) for c in a["decision"]["checks"]]
    ok("решение: «проверить правки сотрудника» с медианой без правок рядом с итоговой",
       any(c.startswith("Проверить правки сотрудника в объявлениях (снято 2") and "2 500 000 000 сум" in c
           and "2 593 250 000 сум" in c for c in chk), chk)
    med_row = next(r for r in sec3(a)["rows"] if r["label"] == "Медиана цен объявлений")
    ok("раздел 3: у медианы — «без правок сотрудника — 2 593 250 000»",
       "без правок сотрудника — 2 593 250 000 сум" in flat(med_row["note"]), med_row)
    plain, text = docx_plain(a["id"]), pdf_plain(a["id"])
    ok("документ на один лист: правки объявлений — только в JSON и на экране (строка правок в разделе 3 JSON)",
       line in sec3(a)["source_lines"] and "Правки сотрудника" not in plain and "снято сотрудником" not in text,
       plain[-600:])

    # правка цены (было → стало), включение исключённого моделью и объявление вручную
    ed = [dict(r) for r in listings]
    ed[0]["price"] = 1_000_000_000                        # L1: 2 650 000 000 → 1 000 000 000
    ed[4]["relevant"] = True                              # L5: модель исключила, сотрудник включил
    ed.append({"title": "XCMG QY50K5D 2021 (у дилера)", "price": 2_550_000_000, "currency": "UZS", "year": 2021,
               "posted_date": _days_ago(2), "url": "https://www.olx.uz/d/obyavlenie/xcmg-qy50k5d-ID1.html",
               "source": "manual"})
    st, a = mk_make(ed, sid)
    mv = a["market_value"]
    cand = [1_000_000_000, 2_500_000_000, round(205000 * RATE), 2_400_000_000, 1_500_000_000, 900_000_000,
            2_800_000_000, 2_550_000_000]
    m0 = hand_q(cand, 0.5)
    used3 = [x for x in cand if 0.5 * m0 <= x <= 2 * m0]
    ok("правки: медиана и вилка руками", mv["used"] == len(used3) == 6 and mv["median"] == round(hand_q(used3, 0.5))
       and mv["low"] == round(hand_q(used3, 0.25)) and mv["high"] == round(hand_q(used3, 0.75)), mv)
    src = {r["id"]: (r["source"], r["edited"]) for r in mv["listings"]}
    ok("исправленные помечены, ручное — «введено сотрудником»", src["L1"] == ("shot", True)
       and src["L5"] == ("shot", True) and src["L2"] == ("shot", False) and src["m11"] == ("manual", False), src)
    items = {(e["id"], e["what"]): flat(e["text"]) for e in mv["edits"]["items"]}
    ok("цена: «цена исправлена сотрудником: было 2 650 000 000, стало 1 000 000 000»",
       items.get(("L1", "price")) == "XCMG QY50K5D 2021 — цена исправлена сотрудником: было 2 650 000 000, "
                                     "стало 1 000 000 000", items)
    ok("включено сотрудником (модель исключила) — с причиной модели",
       "включено сотрудником" in items.get(("L5", "checked"), "") and "QY25K5D" in items.get(("L5", "checked"), ""),
       items)
    ok("счёт правок: включено 1, исправлено цен 1, добавлено вручную 1",
       (mv["edits"]["checked"], mv["edits"]["price_changed"], mv["edits"]["manual"], mv["edits"]["unchecked"]) ==
       (1, 1, 1, 0), mv["edits"])
    ok("строки источника: снимки + введено сотрудником + строка правок",
       any("снимки загружены" in x for x in mv["source_lines"]) and any("введено сотрудником" in x
                                                                         for x in mv["source_lines"])
       and "Правки сотрудника: снято 0, включено 1, исправлено цен 1, убрано 0, добавлено вручную 1." in
       mv["source_lines"], mv["source_lines"])
    ok("адрес объявления сохранён, но не открывается сервером",
       mv["listings"][-1]["url"] == "https://www.olx.uz/d/obyavlenie/xcmg-qy50k5d-ID1.html")
    plain = docx_plain(a["id"])
    ok("правка цены «было → стало» — в JSON объявления, в документ не выносится",
       "было 2 650 000 000, стало 1 000 000 000" in flat(_json.dumps(a["market_value"], ensure_ascii=False))
       and "было 2 650 000 000" not in plain)

    # правка валюты, года и даты публикации — тоже «было → стало»
    ed2 = [dict(r) for r in listings]
    ed2[1].update(year=2015, posted_date=_days_ago(20))
    st, a = mk_make(ed2, sid)
    whats = {(e["id"], e["what"]) for e in a["market_value"]["edits"]["items"]}
    ok("правка года и даты публикации видна", {("L2", "year"), ("L2", "posted_date")} <= whats, whats)

    # убранные из списка объявления загрузки: «убрано сотрудником», в «Не вошли в расчёт»
    cut = [dict(r) for r in listings if r["id"] not in ("L2", "L4")]
    st, a = mk_make(cut, sid)
    mv = a["market_value"]
    reasons = {e["id"]: e for e in mv["excluded"]}
    excl = next(li for li in sec3(a)["lists"] if li["title"] == "Не вошли в расчёт")
    ok("убранные: «убрано сотрудником», в списке «Не вошли в расчёт», счёт правок removed = 2",
       reasons["L2"]["code"] == reasons["L4"]["code"] == "mx_removed_by_employee" and mv["edits"]["removed"] == 2
       and mv["count"] == 10 and any("убрано сотрудником" in x for x in excl["items"]), (mv["edits"], excl))
    ok("убранные: проверка правок с медианой без правок",
       any(c.startswith("Проверить правки сотрудника") for c in a["decision"]["checks"]), a["decision"]["checks"])

    # п. 2: загрузка снимков недоступна — даты с экрана учитываются, всё «введено сотрудником», курс ЦБ не «сотрудника»
    act._FX_CACHE.clear()
    act._fx_lookup(TODAY)                                 # сервер уже выдавал курс ЦБ на сегодня
    dead = "0" * 24
    st, a = mk_make(listings, dead, fx={"rate": RATE, "by": "cbu", "as_of": TODAY.isoformat()})
    mv = a["market_value"]
    miss = ("Снимки объявлений недоступны (прошло больше 24 часов или сменилась сессия) — объявления учтены как "
            "введённые сотрудником.")
    codes = {e["id"]: e["code"] for e in mv["excluded"]}
    ok("недоступная загрузка: shots_missing и строка в акте", mv["shots_missing"] is True
       and mv["shots_missing_text"] == miss and miss in sec3(a)["source_lines"], mv["source_lines"])
    ok("недоступная загрузка: все «введено сотрудником», даты экрана учтены (6 месяцев работают)",
       all(r["source"] == "manual" for r in mv["listings"]) and codes.get("L9") == "mx_too_old"
       and codes.get("L8") == "mx_no_date" and mv["median"] == round(med) and mv["used"] == 5, codes)
    ok("курс ЦБ, полученный экраном от сервера и сверенный, — «ЦБ РУз», не «введён сотрудником»",
       mv["fx"]["by"] == "cbu" and "cbu.uz" in mv["fx"]["text"] and "сотрудник" not in mv["fx"]["text"], mv["fx"])
    plain = docx_plain(a["id"])
    ok("строка о недоступных снимках — в JSON раздела 3, в документ не выносится",
       "Снимки объявлений недоступны" in flat(_json.dumps(a["market_value"], ensure_ascii=False))
       and "Снимки объявлений недоступны" not in plain)
    act._FX_CACHE.clear()
    st, a = mk_make(listings, dead, fx={"rate": RATE, "by": "cbu", "as_of": TODAY.isoformat()})
    fx = a["market_value"]["fx"]
    ok("курс ЦБ не с чем сверить — «ЦБ РУз, повторно не сверен», всё равно не «сотрудник»",
       fx["by"] == "cbu_unverified" and "не сверен" in fx["text"] and "введён сотрудником" not in fx["text"], fx)
    st, a = mk_make(listings, dead, fx={"rate": 12_700, "by": "employee", "as_of": TODAY.isoformat()})
    fx = a["market_value"]["fx"]
    ok("курс, введённый сотрудником, — «введён сотрудником»", fx["by"] == "employee" and fx["rate"] == 12_700
       and "введён сотрудником" in fx["text"], fx)
    st, b = mk_make(listings, dead, fx={"rate": RATE, "by": "robot"})
    ok("источник курса проверяется", st == 422 and "fx.by" in b["errors"]["market"], (st, b))
    market_setup()

    # п. 5: стоимость объекта заменена медианой — в акте видно, что заявил клиент
    must = dict(CRANE_MUST, object_value=round(med))
    st, a = mk_make(listings, sid, must=must, optional=dict(CRANE_OPT, declared_value_original=3_100_000_000))
    rows = {r["label"]: r for r in sec3(a)["rows"]}
    ok("замена медианой: «Заявлено клиентом: …; стоимость принята по объявлениям: …»",
       "Заявлено клиентом" in rows and flat(rows["Заявлено клиентом"]["note"]) ==
       "Заявлено клиентом: 3 100 000 000 сум; стоимость принята по объявлениям: 2 593 250 000 сум."
       and a["value"]["value_source"] == "listings" and a["value"]["declared_original"] == 3_100_000_000, rows)
    ok("после замены: объявления подтверждают стоимость, итог — по сумме к стоимости (превышение)",
       a["market_value"]["verdict"] == "confirmed" and a["value"]["final_verdict"] == "over", a["value"])
    ok("DOCX: раздел 3 одной строкой — итог «превышение» (ГК ст. 938); «заявлено клиентом» — в JSON",
       "Превышение" in docx_plain(a["id"]) and "стоимость принята по объявлениям" not in docx_plain(a["id"]))
    st, b = mk_make(listings, sid, optional=dict(CRANE_OPT, declared_value_original="abc"))
    ok("declared_value_original проверяется", st == 422 and "declared_value_original" in b["errors"], (st, b))

    # только ручной ввод, без снимков: источник — «введено сотрудником»; курс сотрудника
    manual = [{"title": "XCMG QY50K5D", "price": 2_900_000_000, "posted_date": _days_ago(4)},
              {"title": "XCMG QY50K5D", "price": 3_000_000_000, "posted_date": _days_ago(9)},
              {"title": "XCMG QY50K5D", "price": 240_000, "currency": "USD", "posted_date": _days_ago(1)}]
    market_setup(rate=None)
    st, a = mk_make(manual, None, usd_rate=12_500)
    mv = a["market_value"]
    ok("ручной ввод: «Источник: введено сотрудником», курс сотрудника, confirmed",
       mv["source_label"] == "Источник: введено сотрудником (объявлений: 3)." and mv["fx"]["by"] == "employee"
       and mv["median"] == 3_000_000_000 and mv["verdict"] == "confirmed"
       and not any(c.startswith("Уточнить стоимость") for c in a["decision"]["checks"]), mv)
    st, a = mk_make(manual, None)
    ok("ручной ввод без курса: доллары не вошли, курс не выдуман", a["market_value"]["used"] == 2
       and a["market_value"]["fx"] is None, a["market_value"]["fx"])
    st, a = mk_make([dict(r, posted_date=None) for r in manual[:2]], None)
    ok("ручной ввод без даты — не в расчёте («дата публикации не видна»)",
       not a["market_value"]["available"] and {e["code"] for e in a["market_value"]["excluded"]} == {"mx_no_date"})
    market_setup()

    # мало объявлений и ни одного
    st, a = mk_make(listings[:2], sid)
    mv = a["market_value"]
    ok("два объявления — «мало, ориентировочно», стоимость не уточняется, выбросы не искались",
       mv["verdict"] == "few" and mv["refined_value"] is None and "ориентировочная" in mv["verdict_text"]
       and not any(c.startswith("Уточнить стоимость") for c in a["decision"]["checks"])
       and any("выбросы не искались" in h for h in mv["how"]), mv["verdict"])
    none = [dict(r, relevant=False) for r in listings]
    st, a = mk_make(none, sid)
    st0, a0 = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                         "optional": dict(CRANE_OPT, object_kind="truck_crane")})
    n_rel = sum(1 for r in stored["listings"] if r.get("relevant") is not False)
    ok("ни одного — оценки нет, строки раздела 3 как без неё, но правки видны",
       not a["market_value"]["available"] and a["market_value"]["verdict"] == "none"
       and [r["label"] for r in sec3(a)["rows"]] == [r["label"] for r in sec3(a0)["rows"]]
       and f"Правки сотрудника: снято {n_rel}, включено 0, исправлено цен 0, убрано 0, добавлено вручную 0."
       in sec3(a)["source_lines"], sec3(a)["source_lines"])
    ok("акт без блока market: market_value.available = false", a0["market_value"]["available"] is False
       and a0["market_value"]["verdict"] == "none" and not sec3(a0)["source_lines"])

    # минимум объявлений — настройка
    with db.tx() as con:
        con.execute("INSERT INTO act_settings (created_at, created_by, settings_json, calibrated, note) "
                    "VALUES (?,?,?,?,?)", (db.now(), "тест", _json.dumps({"market": {"min_listings": 6}}), 0, "тест"))
    try:
        st, a = mk_make(listings, sid)
        ok("min_listings = 6 из настроек — 5 объявлений уже «мало»", a["market_value"]["verdict"] == "few")
    finally:
        clear_settings()

    # недоверенный ввод и ПД в ручном вводе
    for bad, key in (({"price": "abc"}, "цена"), ({"currency": "EUR", "price": 1}, "currency"),
                     ({"price": 1, "url": "javascript:alert(1)"}, "адрес"), ({"price": 1, "year": 1700}, "год"),
                     ({"price": 1, "url": "http://169.254.169.254/latest/meta-data/"}, "адрес"),
                     ({"price": 1, "url": "https://example.uz/ad/1"}, "адрес"),
                     ({"price": 1, "posted_date": "31.02.2026"}, "дата")):
        st, b = mk_make([bad], sid)
        ok(f"ввод проверяется: {key} ({bad.get('url') or bad.get('posted_date') or ''})",
           st == 422 and "market" in b["errors"] and key in b["errors"]["market"], (st, b))
    st, a = mk_make([{"title": "Кран, продаёт Петров Пётр Петрович", "price": 2_700_000_000,
                      "region": "Каримов Алишер", "why_excluded": None},
                     {"title": "XCMG, тел. +998 91 555 44 33", "price": 2_600_000_000}], None)
    dump = _json.dumps(a, ensure_ascii=False)
    with db.tx() as con:
        saved = db.rows(con, "SELECT act_json FROM acts WHERE id=?", a["id"])[0]["act_json"]
    ok("ПД в ручном вводе отброшены — ни в ответе, ни в базе",
       not any(x in dump or x in saved for x in ("Петрович", "Петров", "555 44 33", "Каримов")), dump[:300])
    st, b = mk_make(listings, "не-та-сессия")
    ok("чужая или неизвестная загрузка снимков — все объявления «введено сотрудником»",
       all(r["source"] == "manual" for r in b["market_value"]["listings"]) and b["market_value"]["shots_missing"])
    return aid


# ------------------------------------------------------------------ 37. запрос филиала (30.09.2026)

BR_HEAD = ["ОСГОР бўйича белгиланган чегарадан ошиб кетиш***", "бошқа: ______________"]
BR_SUM1 = "81 250 000 000,00 (саксон бир миллиард икки юз эллик миллион сўм ва 00 тийин) сўм"
BR_SUM2 = ("47 397 852 345,04 (қирқ етти миллиард уч юз тўқсон етти миллион саккиз юз эллик икки минг уч юз "
           "қирқ беш сўм ва 04 тийин) сўм")
# два образца заказчика (сканы 24.png и 25.png) — строки как в бланке, узбекская кириллица
BR_SAMPLE1 = [
    ("1.", "Суғурта тури (буйруқ бўйича код):", "0832"),
    ("2.", "Суғурта қилдирувчи номи:", '"NAMUNA SAVDO" MCHJ'),
    ("3.", "Наф олувчи:", 'CHEKI "Namuna Bank" ATB Sinov universal BXO'),
    ("4.", "Гаровга қўювчи", '"OMAD" AJ'),
    ("5.", "Суғурта объекти:", ["«Кўчмас мулк нотурар бино қишлоқ хўжалиги махсулотларини сақлаш учун музлатгич»",
                                "Ер участкасининг умумий майдони 8 640,00 кв.м.",
                                "Умумий фойдали майдони 11 898.42 кв.м", "Умумий майдони 13 344.00 кв.м.",
                                "кадастр рақами 10:00:00:00:00:00001"]),
    ("6.", "Суғурта қиймати:", BR_SUM1),
    ("7.", "Суғурта суммаси:", BR_SUM1),
    ("8.", "Франшиза:", "Қўлланилинмайди"),
    ("9.", "Суғурта тарифи :", "0.05"),
    ("10.", "Суғурта мукофоти:", "123 322 000,00 (бир юз йигирма уч миллион уч юз йигирма икки минг сўм ва 00 тийин) сўм"),
    ("11.", "Суғурта муддати:", ["2026 йил «29» сентябрдан", "2029 йил «10» октябргача"]),
    ("12.", "Стандарт суғурта шартномаси шартларини ўзгартириш/қўшиш:*", "Стандарт"),
    ("13.", "Контрагент:**", ""),
    ("14.", "Шартнома миқдори:**", "1 дона"),
    ("15.", "Класс*** (ОСГОР бўйича)", ""),
    ("16.", "Қўшимча маълумот:", ""),
]
BR_SAMPLE2 = [
    ("1.", "Суғурта тури (буйруқ бўйича код):", "0832"),
    ("2.", "Суғурта қилдирувчи номи:", '"Namuna Bank" АТБ Намуна УБХО'),
    ("3.", "Наф олувчи:", '"SINOV" MCHJ'),
    ("4.", "Гаровга қўювчи", ""),
    ("5.", "Суғурта объекти:", "Технологик асбоб ускуна нон махсулотлари ишлаб чиқариш учун"),
    ("6.", "Суғурта қиймати:", BR_SUM2),
    ("7.", "Суғурта суммаси:", BR_SUM2),
    ("8.", "Франшиза:", "Қўлланилинмайди"),
    ("9.", "Суғурта тарифи :", "0.05"),
    ("10.", "Суғурта мукофоти:", "122 589 000,00 (бир юз йигирма икки миллион беш юз саксон тўққиз минг сўм ва 00 "
                                "тийин) сўм"),
    ("11.", "Суғурта муддати:", ["2026 йилнинг «07» сентябрдан", "2031 йилнинг «07» ноябр мобайнида"]),
    ("12.", "Стандарт суғурта шартномаси шартларини ўзгартириш/қўшиш:*", "Стандарт"),
    ("13.", "Контрагент:**", ""),
    ("14.", "Шартнома миқдори:**", "1 дона"),
    ("15.", "Класс*** (ОСГОР бўйича)", ""),
    ("16.", "Қўшимча маълумот:", ""),
]
BR_S1 = 81_250_000_000.0
BR_S2 = 47_397_852_345.04


def _x(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def docx_table(rows, head=BR_HEAD) -> bytes:
    """DOCX с абзацами-шапкой и таблицей из 16 строк (ячейка с несколькими строками — несколько абзацев)."""
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    para = lambda t: f"<w:p><w:r><w:t xml:space=\"preserve\">{_x(t)}</w:t></w:r></w:p>"   # noqa: E731
    body = "".join(para(h) for h in head) + "<w:tbl>"
    for n, lab, val in rows:
        vals = val if isinstance(val, list) else [val]
        body += "<w:tr>" + "".join(f"<w:tc>{c}</w:tc>" for c in (
            para(n), para(lab), "".join(para(v) for v in vals))) + "</w:tr>"
    body += "</w:tbl>"
    xml = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def xlsx_table(rows) -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Сўров"
    for h in BR_HEAD:
        ws.append([h])
    for n, lab, val in rows:
        ws.append([n, lab, "\n".join(val) if isinstance(val, list) else val])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def pdf_table(rows) -> bytes:
    """PDF с текстовым слоем: каждая ячейка — своя строка (так pymupdf отдаёт текст таблицы)."""
    doc = pymupdf.open()
    page = doc.new_page()
    y = 40
    font = r"C:\Windows\Fonts\arial.ttf"
    for n, lab, val in rows:
        for t in [n, lab] + (val if isinstance(val, list) else [val]):
            if t:
                page.insert_text((40, y), t[:95], fontsize=7, fontname="arl", fontfile=font)
                y += 9
    return doc.tobytes()


def br_request(b):
    return (b.get("branch_request") or {}).get("request")


def nb(s) -> str:
    return str(s).replace(" ", " ")


def check_br_fields(tag, f, sample):
    """Поля бланка против расшифровки заказчика (оба образца)."""
    if sample == 1:
        ok(f"{tag}: код продукта, стороны (юрлица), залог",
           f["product_code"] == "0832" and f["policyholder"] == {"kind": "legal", "name": '"NAMUNA SAVDO" MCHJ'}
           and f["beneficiary"]["name"] == 'CHEKI "Namuna Bank" ATB Sinov universal BXO'
           and f["pledger"] == {"kind": "legal", "name": '"OMAD" AJ'} and f["has_pledger"] is True
           and f["has_beneficiary"] is True, f)
        ok(f"{tag}: объект — здание, площади, кадастр",
           f["class_hint"] == "building" and f["object_kind"] == "warehouse"
           and f["areas"] == {"land_m2": 8640.0, "useful_m2": 11898.42, "total_m2": 13344.0}
           and f["cadastre_no"] == "10:00:00:00:00:00001" and "музлатгич" in f["object_description"], f)
        ok(f"{tag}: стоимость и сумма — число без суммы прописью",
           f["object_value"] == BR_S1 and f["sum_insured"] == BR_S1, (f["object_value"], f["sum_insured"]))
        ok(f"{tag}: тариф 0,05, премия 123 322 000, франшиза не применяется",
           f["tariff_pct"] == 0.05 and f["premium"] == 123_322_000.0
           and f["franchise"] == {"applied": False, "text": "Қўлланилинмайди", "pct": None, "amount": None}, f)
        ok(f"{tag}: срок 29.09.2026–10.10.2029, 1 108 дн. включительно",
           f["term_from"] == "2026-09-29" and f["term_to"] == "2029-10-10" and f["term_days"] == 1108
           and f["term_inclusive"] is True, (f["term_from"], f["term_to"], f["term_days"]))
    else:
        ok(f"{tag}: код продукта, стороны, залога нет",
           f["product_code"] == "0832" and f["policyholder"]["name"] == '"Namuna Bank" АТБ Намуна УБХО'
           and f["beneficiary"] == {"kind": "legal", "name": '"SINOV" MCHJ'}
           and f["pledger"] == {"kind": None, "name": None} and f["has_pledger"] is False, f)
        ok(f"{tag}: объект — оборудование",
           f["class_hint"] == "equipment" and f["object_kind"] == "equipment" and f["cadastre_no"] is None
           and f["areas"] == {"land_m2": None, "useful_m2": None, "total_m2": None}, f)
        ok(f"{tag}: стоимость и сумма 47 397 852 345,04", f["object_value"] == BR_S2 and f["sum_insured"] == BR_S2,
           (f["object_value"], f["sum_insured"]))
        ok(f"{tag}: тариф, премия 122 589 000, франшиза не применяется",
           f["tariff_pct"] == 0.05 and f["premium"] == 122_589_000.0 and f["franchise"]["applied"] is False, f)
        ok(f"{tag}: срок 07.09.2026–07.11.2031 («йилнинг … мобайнида»), 1 888 дн.",
           f["term_from"] == "2026-09-07" and f["term_to"] == "2031-11-07" and f["term_days"] == 1888,
           (f["term_from"], f["term_to"], f["term_days"]))
    ok(f"{tag}: стандартные условия, 1 договор, пустые строки — null",
       f["contract_terms"] == "Стандарт" and f["contract_terms_standard"] is True and f["contracts_count"] == 1
       and f["counterparty"] is None and f["osgor_class"] is None and f["additional_info"] is None, f)


def check_branch_text():
    print("37а. Запрос филиала: файл с текстом (DOCX, XLSX, PDF) — без модели")
    fresh()
    model_on(True)
    REPLY["text"] = CRANE_REPLY
    CALLS.clear()
    st, b = upload([("sorov1.docx", DOCX_MIME, docx_table(BR_SAMPLE1))], {"lang": "ru"})
    ok("DOCX образца 1 принят, модель не вызывалась", st == 200 and b.get("ok") and not CALLS, (st, len(CALLS)))
    brq = b.get("branch_request") or {}
    ok("узнан как «запрос филиала»: 16 строк из 16, источник — документ",
       brq.get("detected") and brq.get("rows_found") == 16 and brq.get("rows_total") == 16
       and brq.get("source") == "document" and brq.get("kind_label") == "запрос филиала"
       and b["files"][0]["document_kind"] == "запрос филиала", brq)
    ok("строки бланка подписаны: «Страховой тариф», пустые отмечены",
       brq["rows"][8]["label"] == "Страховой тариф" and brq["rows"][8]["filled"]
       and not brq["rows"][12]["filled"] and all(r["found"] for r in brq["rows"]), brq["rows"])
    check_br_fields("DOCX 1", brq["fields"], 1)
    rq = br_request(b)
    ok("готовый optional.request для /act/make",
       rq["tariff_pct"] == 0.05 and rq["premium"] == 123_322_000.0 and rq["term_days"] == 1108
       and rq["franchise"]["applied"] is False and rq["source"] == "document", rq)
    pf = b.get("prefill") or {}
    ok("prefill шага 2: код продукта, сумма, стоимость, срок и даты — «из документа, проверьте»",
       pf.get("product_code", {}).get("value") == "0832" and pf["sum_insured"]["value"] == BR_S1
       and pf["object_value"]["value"] == BR_S1 and pf["term_days"]["value"] == 1108
       and pf["term_from"]["value"] == "2026-09-29" and pf["term_to"]["value"] == "2029-10-10"
       and "region" not in pf and all(v["check_label"] == "из документа, проверьте" for v in pf.values()), pf)
    rec = {(r["key"], r["value"]) for r in b["recognized"]}
    ok("распознанное: премия, тариф, срок, стороны-юрлица, площади, кадастр",
       {("premium", "123 322 000"), ("tariff_pct", "0.05"), ("term_days", "1108"), ("term_from", "2026-09-29"),
        ("pledger", '"OMAD" AJ'), ("land_area", "8 640 м²"), ("cadastre_no", "10:00:00:00:00:00001"),
        ("sum_insured", "81 250 000 000"), ("product_code", "0832")} <= rec, sorted(rec))
    ok("подписи новых полей есть", all(r["label"] != r["key"] for r in b["recognized"]),
       [r["key"] for r in b["recognized"] if r["label"] == r["key"]])
    ok("класс-подсказка из строки объекта: здание → 8 или 9", b["class_hint"] == "building"
       and b["suggest_classes"] == ["8", "9"], b["class_hint"])
    with db.tx() as con:
        audit = db.rows(con, "SELECT detail FROM audit WHERE entity=?", "act_upload:" + b["session"])
    dump = _json.dumps(audit, ensure_ascii=False)
    det = _json.loads(audit[0]["detail"]) if audit else {}
    ok("в журнале — только признак и число строк, без названий сторон и сумм",
       det.get("branch_request") is True and det.get("branch_rows") == 16 and "OMAD" not in dump and "NAMUNA" not in dump and "81250" not in dump,
       dump[:300])
    sid1 = b["session"]

    CALLS.clear()
    st, b2 = upload([("sorov2.xlsx", XLSX_MIME, xlsx_table(BR_SAMPLE2))], {"lang": "ru"})
    ok("XLSX образца 2 принят, модель не вызывалась", st == 200 and not CALLS and b2.get("branch_request"), (st, b2))
    check_br_fields("XLSX 2", b2["branch_request"]["fields"], 2)
    ok("образец 2: класс-подсказка — оборудование", b2["class_hint"] == "equipment" and b2["group"] == "equipment",
       (b2["class_hint"], b2["group"]))

    st, b3 = upload([("sorov1.pdf", "application/pdf", pdf_table(BR_SAMPLE1))], {"lang": "ru"})
    ok("PDF с текстом (ячейки построчно) — тот же разбор без модели",
       st == 200 and not CALLS and (b3.get("branch_request") or {}).get("rows_found") == 16, (st, b3))
    if b3.get("branch_request"):
        check_br_fields("PDF 1", b3["branch_request"]["fields"], 1)

    # русские подписи и русский срок
    ru_rows = [("1.", "Вид страхования (код):", "0832"), ("2.", "Страхователь:", 'ООО "Ромашка"'),
               ("3.", "Выгодоприобретатель:", 'АКБ "Капиталбанк"'), ("4.", "Залогодатель:", ""),
               ("5.", "Объект страхования:", "Нежилое здание — склад, общая площадь 1 200 кв.м"),
               ("6.", "Страховая стоимость:", "1 000 000 000,00 (один миллиард) сум"),
               ("7.", "Страховая сумма:", "1 000 000 000,00 сум"), ("8.", "Франшиза:", "не применяется"),
               ("9.", "Страховой тариф:", "0,1"), ("10.", "Страховая премия:", "3 000 000,00 сум"),
               ("11.", "Срок страхования:", "с 29.09.2026 по 10.10.2029")]
    st, b4 = upload([("ru.docx", DOCX_MIME, docx_table(ru_rows, head=[]))], {"lang": "ru"})
    f4 = (b4.get("branch_request") or {}).get("fields") or {}
    ok("русские подписи: тариф 0,1, срок «с … по …» → 1 108 дн., франшиза не применяется, общая площадь",
       f4.get("tariff_pct") == 0.1 and f4.get("term_days") == 1108 and f4.get("franchise", {}).get("applied") is False
       and f4.get("areas", {}).get("total_m2") == 1200.0 and f4.get("beneficiary", {}).get("kind") == "legal", f4)

    # обычный договор с похожими подписями, но без кода продукта — не запрос филиала
    contract = [(str(i) + ".", lab, val) for i, (lab, val) in enumerate((
        ("Вид страхования:", "страхование имущества"), ("Страхователь:", 'ООО "Ромашка"'),
        ("Выгодоприобретатель:", 'АКБ "Капиталбанк"'), ("Объект страхования:", "склад"),
        ("Страховая стоимость:", "1 000 000 000 сум"), ("Страховая сумма:", "1 000 000 000 сум"),
        ("Франшиза:", "нет"), ("Страховой тариф:", "0,1 %"), ("Страховая премия:", "1 000 000 сум"),
        ("Срок страхования:", "с 01.10.2026 по 30.09.2027")), 1)]
    st, b6 = upload([("dogovor.docx", DOCX_MIME, docx_table(contract, head=["ДОГОВОР СТРАХОВАНИЯ ИМУЩЕСТВА"]))],
                    {"lang": "ru"})
    ok("договор с похожими подписями без кода продукта — не запрос филиала",
       st == 200 and b6.get("branch_request") is None, (st, b6.get("branch_request")))
    # срок без последнего дня — настройка request_check.term_inclusive = false
    with db.tx() as con:
        con.execute("INSERT INTO act_settings (created_at, created_by, settings_json, calibrated, note) "
                    "VALUES (?,?,?,?,?)", (db.now(), "тест", _json.dumps({"request_check": {"term_inclusive": False,
                                                                                            "premium_tolerance": 1000}}),
                                           0, "тест"))
    st, b5 = upload([("sorov1.docx", DOCX_MIME, docx_table(BR_SAMPLE1))], {"lang": "ru"})
    ok("настройка term_inclusive = false: 1 107 дн.",
       b5["branch_request"]["fields"]["term_days"] == 1107 and b5["branch_request"]["fields"]["term_inclusive"] is False,
       b5["branch_request"]["fields"]["term_days"])
    clear_settings()
    ok("ae.check_settings: request_check проверяется",
       ae.check_settings({"request_check": {"term_inclusive": "да", "premium_tolerance": -1}}) and
       not ae.check_settings({"request_check": {"term_inclusive": True, "premium_tolerance": 500}}))
    return sid1, b


def br_model_reply(sample=1, policyholder=None):
    rows = BR_SAMPLE1 if sample == 1 else BR_SAMPLE2
    v = {br_code: (val if not isinstance(val, list) else " ".join(val)) or None
         for br_code, (_n, _l, val) in zip(
             ["product_code", "policyholder", "beneficiary", "pledger", "object", "object_value", "sum_insured",
              "franchise", "tariff", "premium", "term", "contract_terms", "counterparty", "contracts_count",
              "osgor_class", "additional_info"], rows)}
    for k in ("policyholder", "beneficiary", "pledger"):
        v[k] = {"is_legal": bool(v[k]), "name": v[k]}
    if policyholder:
        v["policyholder"] = {"is_legal": True, "name": policyholder}      # модель ошиблась: гражданин как юрлицо
    v.update(file=1, term_from="2026-09-29" if sample == 1 else "2026-09-07",
             term_to="2029-10-10" if sample == 1 else "2031-11-07",
             object_description_translated=("недвижимость: нежилое здание — холодильник для хранения "
                                            "сельхозпродукции" if sample == 1 else
                                            "технологическое оборудование для производства хлебобулочных изделий"),
             class_hint="building" if sample == 1 else "equipment")
    return "```json\n" + _json.dumps({"files": [{"n": 1, "view": "document", "document_kind": "branch_request"}],
                                      "object_kind": None, "class_hint": None, "condition": None, "fields": [],
                                      "damages": [], "branch_request": v}, ensure_ascii=False) + "\n```"


def check_branch_scan():
    print("37б. Запрос филиала: скан — ответ модели (подменён) по строгой схеме")
    fresh()
    model_on(True)
    CALLS.clear()
    REPLY["text"] = br_model_reply(1)
    st, b = upload([("scan.png", "image/png", image((250, 250, 250)))], {"lang": "ru"})
    ok("скан принят, одно обращение к модели", st == 200 and len(CALLS) == 1, (st, len(CALLS)))
    prompt = CALLS[0]["messages"][0]["content"] + CALLS[0]["messages"][1]["content"]
    ok("в инструкции модели — запрос филиала, branch_request и запрет имён граждан",
       "branch_request" in prompt and "запрос филиала" in prompt and "is_legal" in prompt
       and "суғурта мукофоти" in prompt, prompt[-400:])
    ok("инструкция не портится маскировкой ПД", llm.mask_pd(prompt) == prompt)
    brq = b.get("branch_request") or {}
    ok("вид документа — запрос филиала, источник — скан", brq.get("source") == "photo"
       and b["files"][0]["document_kind"] == "запрос филиала" and brq.get("file") == "f1", brq)
    check_br_fields("скан 1", brq["fields"], 1)
    ok("перевод описания объекта — от модели, исходный текст сохранён",
       "холодильник" in (brq["fields"]["object_description_translated"] or "")
       and "музлатгич" in brq["fields"]["object_description"], brq["fields"]["object_description_translated"])
    rec = {(r["key"], r["value"], r["source"]) for r in b["recognized"]}
    ok("распознанное со скана — источник «документ»",
       ("premium", "123 322 000", "document") in rec and ("beneficiary", 'CHEKI "Namuna Bank" ATB Sinov universal BXO',
                                                          "document") in rec, sorted(rec))
    ok("prefill со скана: код продукта и срок", (b.get("prefill") or {}).get("product_code", {}).get("value") == "0832"
       and b["prefill"]["term_days"]["value"] == 1108, b.get("prefill"))
    sid = b["session"]

    CALLS.clear()
    REPLY["text"] = br_model_reply(2)
    st, b2 = upload([("scan2.jpg", "image/jpeg", image(kind="jpg"))], {"lang": "ru"})
    check_br_fields("скан 2", (b2.get("branch_request") or {}).get("fields") or {}, 2)

    # гражданин в строке страхователя: имени нет ни в ответе, ни в базе, ни в журнале
    CALLS.clear()
    REPLY["text"] = br_model_reply(1, policyholder="Каримов Алишер Анварович")
    st, b3 = upload([("scan3.png", "image/png", image((240, 240, 240)))], {"lang": "ru"})
    f3 = b3["branch_request"]["fields"]
    dump = _json.dumps(b3, ensure_ascii=False)
    with db.tx() as con:
        saved = db.rows(con, "SELECT result_json FROM act_uploads WHERE id=?", b3["session"])[0]["result_json"]
        audit = _json.dumps(db.rows(con, "SELECT detail FROM audit WHERE entity=?", "act_upload:" + b3["session"]),
                            ensure_ascii=False)
    ok("физлицо в строке страхователя: kind = individual, имени нет",
       f3["policyholder"] == {"kind": "individual", "name": None} and "Каримов" not in dump
       and "Каримов" not in saved and "Каримов" not in audit, f3["policyholder"])
    ok("пометка «физическое лицо — данные не извлекаются»",
       any("физическое лицо" in n for n in b3["branch_request"]["notes"]), b3["branch_request"]["notes"])
    st, b4 = upload([("s.docx", DOCX_MIME, docx_table([(n, l, "Каримов Алишер" if n == "2." else v)
                                                        for n, l, v in BR_SAMPLE1]))], {"lang": "ru"})
    dump = _json.dumps(b4, ensure_ascii=False)
    ok("файл с текстом: гражданин-страхователь не извлекается",
       b4["branch_request"]["fields"]["policyholder"] == {"kind": "individual", "name": None}
       and "Каримов" not in dump, b4["branch_request"]["fields"]["policyholder"])
    return sid


def br_make(sid, sample=1, request=None, optional=None, lang="ru", must=None):
    S = BR_S1 if sample == 1 else BR_S2
    body = {"session": sid, "lang": lang,
            "must": must or {"product_code": "0832", "sum_insured": S, "object_value": S,
                             "region": "Ташкентская область"},
            "optional": dict(optional or {})}
    if request is not None:
        body["optional"]["request"] = request
    return call("POST", "/act/make", body)


def rq_items(a):
    return {i["code"]: i for i in (a.get("request_check") or {}).get("items") or []}


def check_branch_make(sid_text, b_text, sid_scan):
    print("37в. Сверка запроса филиала с расчётом акта (продукт 0832, оба образца)")
    fresh()
    with db.tx() as con:
        ref = db.load_reference(con)
        mr = min_rate(ref, "0832")
        cls = [r["class_code"] for r in db.rows(con, "SELECT class_code FROM product_classes WHERE product_code='0832'")]
    ok("0832 в справочнике копии базы: класс 8, минимальная ставка компании 0,08 %",
       cls == ["8"] and mr["company"] == 0.08 and mr["floor"] == 0.08, (cls, mr))
    req1 = br_request(b_text)
    st, a = br_make(sid_text, 1, req1)
    ok("акт по образцу 1 сформирован", st == 200 and a.get("ok"), (st, a))
    lvl = a["risk"]["level"]
    adj = ae.DEFAULT_SETTINGS["adj_pct"][lvl]
    applied = round(max(0.08 * (1 + adj / 100), 0.08), 4)
    prem = round(BR_S1 * applied / 100 * 1108 / 365)
    BR_REPORT["образец 1"] = {"level": lvl, "rate": applied, "premium": a["premium"]["amount"], "days": 1108}
    ok("многолетний срок: премия акта на 1 108 дн. по годовой ставке",
       a["premium"]["term_days"] == 1108 and a["rate"]["applied_pct"] == applied
       and a["premium"]["amount"] == prem, (a["premium"], a["rate"]["applied_pct"], prem))
    it = rq_items(a)
    rc = a["request_check"]
    ok("request_check: доступна, источник — файл запроса (сервер сверил со своей загрузкой, правок нет)",
       rc["available"] and rc["source"] == "document" and rc["source_kind"] == "document"
       and rc["source_label"] == "из документа — из файла запроса (разбор текста)" and rc["edits"]["count"] == 0,
       (rc.get("source"), rc.get("source_label")))
    ok("тариф 0,05 ниже минимума 0,08 — below_min",
       it["tariff_min"]["verdict"] == "below_min" and it["tariff_min"]["requested"] == 0.05
       and it["tariff_min"]["calculated"] == 0.08 and "ниже минимального" in it["tariff_min"]["text"], it["tariff_min"])
    ok("тариф ниже ставки акта — differs", it["tariff_act"]["verdict"] == "differs"
       and it["tariff_act"]["calculated"] == applied, it["tariff_act"])
    ok("образец 1: премия по тарифу запроса сходится (123 322 000 против 123 321 917,81)",
       it["premium_request"]["verdict"] == "ok" and it["premium_request"]["calculated"] == 123_321_917.81
       and abs(it["premium_request"]["diff"] - 82.19) < 0.01, it["premium_request"])
    ok("премия акта — справочно, в решение не идёт", it["premium_act"]["reference"]
       and it["premium_act"]["calculated"] == prem and it["premium_act"]["verdict"] == "differs", it["premium_act"])
    ok("сумма к стоимости — ссылка на раздел 3, без дубля", it["sum_value"]["reference"]
       and "раздел" in it["sum_value"]["text"] and it["sum_value"]["verdict"] == "ok", it["sum_value"])
    ok("франшиза: в запросе не применяется, акт не требует — ok",
       it["franchise"]["verdict"] == "ok", it["franchise"])
    ok("срок: 1 108 дн. взят из запроса — ok", it["term"]["verdict"] == "ok" and it["term"]["requested"] == 1108
       and "взят из запроса" in it["term"]["text"], it["term"])
    ok("итог сверки — ниже минимума", rc["summary"]["verdict"] == "below_min" and rc["summary"]["below_min"] == 1,
       rc["summary"])
    ok("как сверено: дни включительно, формула, допуск 1 000 сум",
       any("29.09.2026" in h and "1108" in h and "включены" in h for h in rc["how"])
       and any("× 0,05 %" in nb(h) for h in rc["how"]) and any("1 000 сум" in nb(h) for h in rc["how"]), rc["how"])
    ok("решение не «принять без оговорок»; в проверках — тариф ниже минимума",
       a["decision"]["code"] != "accept" and any("ниже минимального" in c for c in a["decision"]["checks"]),
       a["decision"])
    s4 = a["sections"][3]
    titles = [li["title"] for li in s4["lists"]]
    ok("раздел 4: подраздел «Сверка с запросом филиала»", "Сверка с запросом филиала" in titles, titles)
    aid1 = a["id"]

    # образец 2 со скана (запрос берётся из своей загрузки, если экран его не прислал)
    st, a2 = br_make(sid_scan, 1, None)
    ok("без optional.request — запрос из своей загрузки (source = session)",
       st == 200 and a2["request_check"]["source"] == "session", a2.get("request_check", {}).get("source"))
    req2 = {"tariff_pct": 0.05, "premium": "122 589 000,00", "franchise": {"applied": False, "text": "Қўлланилинмайди"},
            "term_from": "07.09.2026", "term_to": "2031-11-07", "source": "photo"}
    st, a2 = br_make(None, 2, req2)
    it2 = rq_items(a2)
    lvl2 = a2["risk"]["level"]
    applied2 = round(0.08 * (1 + ae.DEFAULT_SETTINGS["adj_pct"][lvl2] / 100), 4)
    BR_REPORT["образец 2"] = {"level": lvl2, "rate": applied2, "premium": a2["premium"]["amount"], "days": 1888}
    ok("образец 2: срок 1 888 дн., премия акта на весь срок",
       a2["premium"]["term_days"] == 1888 and a2["premium"]["amount"] == round(BR_S2 * applied2 / 100 * 1888 / 365),
       a2["premium"])
    ok("образец 2: премия расходится на 3 869,55 сум (differs)",
       it2["premium_request"]["verdict"] == "differs" and it2["premium_request"]["calculated"] == 122_585_130.45
       and abs(it2["premium_request"]["diff"] - 3869.55) < 0.01 and int(it2["premium_request"]["diff"]) == 3869,
       it2["premium_request"])
    ok("образец 2: в проверках андеррайтера — расхождение премии",
       any("не сходится" in c and "3 870" in nb(c) for c in a2["decision"]["checks"]), a2["decision"]["checks"])
    ok("образец 2: тариф ниже минимума", it2["tariff_min"]["verdict"] == "below_min")
    BR_REPORT["сверка 1"] = {k: (v["verdict"], v["requested"], v["calculated"], v["diff"]) for k, v in it.items()}
    BR_REPORT["сверка 2"] = {k: (v["verdict"], v["requested"], v["calculated"], v["diff"]) for k, v in it2.items()}

    # тариф не ниже минимума и не ниже ставки акта, премия сходится — расхождений сверки нет
    good = {"tariff_pct": 0.2, "premium": round(BR_S1 * 0.2 / 100 * 1108 / 365), "franchise": {"applied": False},
            "term_days": 1108}
    st, a3 = br_make(None, 1, good)
    it3 = rq_items(a3)
    ok("тариф 0,2 — не ниже минимума и ставки акта; премия сходится",
       it3["tariff_min"]["verdict"] == "ok" and it3["tariff_act"]["verdict"] == "ok"
       and it3["premium_request"]["verdict"] == "ok" and a3["request_check"]["summary"]["verdict"] == "ok",
       {k: v["verdict"] for k, v in it3.items()})
    ok("без расхождений сверка ничего не добавляет в проверки",
       not any(c.startswith(("Тариф в запросе", "Премия в запросе")) for c in a3["decision"]["checks"]),
       a3["decision"]["checks"])
    # срок сотрудника расходится с запросом
    st, a4 = br_make(None, 1, dict(good), {"term_days": 365})
    ok("срок сотрудника 365 против 1 108 в запросе — differs",
       rq_items(a4)["term"]["verdict"] == "differs" and a4["premium"]["term_days"] == 365, rq_items(a4)["term"])
    # франшиза: в запросе не применяется, а акт предлагает (клиент просит снизить премию)
    st, a5 = br_make(None, 1, dict(good), {"want_lower_premium": True})
    f5 = rq_items(a5)["franchise"]
    ok("франшиза: в запросе нет, акт предлагает — differs", f5["verdict"] == "differs"
       and "предлагает франшизу" in f5["text"], f5)
    fr5 = a5["franchise"]
    if fr5.get("premium_after") is not None and fr5.get("rate_after") is not None:
        ok("франшиза на многолетнем сроке: премия с франшизой — на все 1 108 дн.",
           fr5["premium_after"] == round(BR_S1 * fr5["rate_after"] / 100 * 1108 / 365), fr5)
    ms = a5["measures_summary"]
    ok("мероприятия считаются от премии акта на весь срок", ms["premium_before"] in (None, a5["premium"]["amount"]),
       ms)
    sc = a2["scenarios"]
    ok("сценарии PML/EML/MFL для срока 1 888 дн. (> 60 мес.) — посчитаны с пометкой о сроке",
       sc["available"] and any("1888" in x["text"] for x in sc["assumptions"]), sc.get("assumptions"))
    # проверка ввода
    for bad, key in (({"tariff_pct": 0}, "тариф 0"), ({"tariff_pct": "abc"}, "тариф не число"),
                     ({"premium": -5}, "премия < 0"), ({"term_from": "2029-10-10", "term_to": "2026-09-29"}, "даты наоборот"),
                     ({"term_from": "29.09.2026", "term_to": "10.10.2029", "term_days": 1107}, "дни не по датам"),
                     ({"franchise": {"applied": "нет"}}, "франшиза без applied"), ("строка", "не объект")):
        st, e = br_make(None, 1, bad)
        ok(f"optional.request проверяется: {key}", st == 422 and "request" in (e.get("errors") or {}), (st, e))
    st, a6 = br_make(None, 1, {"franchise": "не применяется", "term_from": "29.09.2026", "term_to": "10.10.2029"})
    ok("франшиза текстом и даты ДД.ММ.ГГГГ принимаются", st == 200 and rq_items(a6)["term"]["requested"] == 1108
       and rq_items(a6)["tariff_min"]["verdict"] == "missing", (st, a6.get("request_check")))
    return aid1, a2["id"]


BR_REPORT = {}


def check_branch_langs_files(aid):
    print("37г. Сверка: три языка, Word и PDF")
    for lang, title, word in (("ru", "Сверка с запросом филиала", "ниже минимального"),
                              ("uz", "Filial soʻrovi bilan solishtirish", "eng kam stavka"),
                              ("en", "Check against the branch request", "below the tariff-policy minimum")):
        st, a = call("GET", f"/act/{aid}", params={"lang": lang})
        s4 = a["sections"][3]
        lines = [x for li in s4["lists"] if li["title"] == title for x in li["items"]]
        ok(f"{lang}: подраздел сверки и текст строки", bool(lines) and any(word in x for x in lines), lines[:3])
        rc = a["request_check"]
        ok(f"{lang}: request_check на языке акта", rc["items"][0]["label"] and rc["summary"]["text"]
           and all(i["verdict_label"] for i in rc["items"]), rc["items"][0])
        if lang != "ru":
            txt = " ".join(lines + [c for c in a["decision"]["checks"]] + rc["how"])
            ok(f"{lang}: в сверке нет кириллицы", not re.search(r"[А-Яа-яЁё]", txt), txt[:300])
        st, blob, h = call("GET", f"/act/{aid}.docx", params={"lang": lang}, raw=True)
        xml = zipfile.ZipFile(io.BytesIO(blob)).read("word/document.xml").decode("utf-8")
        plain = re.sub(r"<[^>]+>", "", xml)
        bm_word = tx.t("cp_below", lang, req="", min="", answer="", why="", short="").split(" ")[0]
        bm_ok = not a["below_min_assessment"]["available"] or bm_word in plain
        ok(f"{lang}: сверка в DOCX — подраздела нет (он в JSON и на экране), заниженная ставка — строкой «Цены»",
           title not in plain and tx.t("cp_b_price", lang) in plain and bm_ok, plain[-300:])
        st, blob, h = call("GET", f"/act/{aid}.pdf", params={"lang": lang}, raw=True)
        text = pdf_text(pymupdf.open(stream=blob, filetype="pdf"))
        ok(f"{lang}: сверка в PDF — то же", title.replace("ʻ", "'") not in text.replace("ʻ", "'")
           and (not a["below_min_assessment"]["available"] or bm_word in text), text[-300:])
    # старый акт без сверки — блок недоступен, ничего не падает
    with db.tx() as con:
        row = db.rows(con, "SELECT act_json FROM acts WHERE id=?", aid)[0]
        stored = _json.loads(row["act_json"])
    stored["data"].pop("request_check", None)
    stored["data"].pop("request", None)
    D = stored["data"]
    out = act.render(D, "ru", stored["meta"])
    ok("старый акт без сверки: request_check.available = false", out["request_check"]["available"] is False
       and "Сверка с запросом филиала" not in [li["title"] for li in out["sections"][3]["lists"]])


# ================================================================================================
#  38. Договор страхования: чтение (файл, скан, модель по тексту) и сверка с расчётом акта (30.09.2026)
# ================================================================================================

CT_UZC = [
    "МОЛ-МУЛКНИ СУҒУРТА ҚИЛИШ ШАРТНОМАСИ № 45-ИМ/2026",
    "Тошкент шаҳри                                   2026 йил «1» октябрь",
    "\"INSON\" АЖ, бундан буён «Суғурталовчи» деб юритилади, директор Каримов Алишер Анварович номидан, "
    "бир томондан, ва \"ALFA TEXTILE\" МЧЖ, бундан буён «Суғурта қилдирувчи» деб юритилади, директор "
    "Тошпўлатов Бахтиёр Равшанович номидан, иккинчи томондан, мазкур шартномани туздилар.",
    "1. ШАРТНОМА ПРЕДМЕТИ",
    "1.1. Суғурта объекти: нотурар бино — омбор, умумий майдони 2 400 кв.м, кадастр рақами 10:09:05:01:02:0033.",
    "1.2. Объект манзили: Тошкент вилояти, Чирчиқ шаҳри, Саноат кўчаси, 7.",
    "2. СУҒУРТА СУММАСИ ВА МУКОФОТИ",
    "2.1. Суғурта қиймати: 12 000 000 000 (ўн икки миллиард) сўм.",
    "2.2. Суғурта суммаси: 12 000 000 000 (ўн икки миллиард) сўм.",
    "2.3. Суғурта тарифи: йиллик 0,1 %.",
    "2.4. Суғурта мукофоти: 36 032 877 (ўттиз олти миллион ўттиз икки минг саккиз юз етмиш етти) сўм.",
    "2.5. Суғурта мукофоти бўлиб-бўлиб тўланади: биринчи тўлов 18 016 438 сўм — 2026 йил 10 октябргача; "
    "иккинчи тўлов 18 016 439 сўм — 2027 йил 10 октябргача.",
    "3. СУҒУРТА МУДДАТИ",
    "3.1. Суғурта муддати: 2026 йил 1 октябрдан 2029 йил 30 сентябргача.",
    "4. ФРАНШИЗА",
    "4.1. Франшиза: қўлланилмайди.",
    "5. СУҒУРТА ХАВФЛАРИ",
    "5.1. Суғурта хавфлари: ёнғин, чақмоқ уриши, портлаш, сув босиши, табиий офатлар, учинчи шахсларнинг "
    "ғайриқонуний ҳаракатлари.",
    "6. ИСТИСНОЛАР",
    "6.1. Қуйидагилар суғурта ҳодисаси ҳисобланмайди: уруш ҳаракатлари, ядро портлаши, суғурта "
    "қилдирувчининг қасддан қилган ҳаракатлари.",
    "7. ЯКУНИЙ ҚОИДАЛАР",
    "7.1. Суғурта қилдирувчи суғурта ҳодисаси юз берганлиги ҳақида 3 (уч) иш куни ичида хабар беради.",
]
CT_UZL = [
    "MOL-MULKNI SUGʻURTA QILISH SHARTNOMASI № 46-IM/2026",
    "Toshkent shahri                                   2026-yil 1-oktyabr",
    "\"INSON\" AJ, bundan buyon «Sugʻurtalovchi» deb yuritiladi, direktor Karimov Alisher Anvarovich nomidan, "
    "bir tomondan, va \"ALFA TEXTILE\" MCHJ, bundan buyon «Sugʻurta qildiruvchi» deb yuritiladi, direktor "
    "Toshpoʻlatov Baxtiyor Ravshanovich nomidan, ikkinchi tomondan, mazkur shartnomani tuzdilar.",
    "1. SHARTNOMA PREDMETI",
    "1.1. Sugʻurta obyekti: noturar bino — ombor, umumiy maydoni 2 400 kv.m, kadastr raqami 10:09:05:01:02:0033.",
    "1.2. Obyekt manzili: Toshkent viloyati, Chirchiq shahri, Sanoat koʻchasi, 7.",
    "2. SUGʻURTA SUMMASI VA MUKOFOTI",
    "2.1. Sugʻurta qiymati: 12 000 000 000 (oʻn ikki milliard) soʻm.",
    "2.2. Sugʻurta summasi: 12 000 000 000 (oʻn ikki milliard) soʻm.",
    "2.3. Sugʻurta tarifi: yillik 0,1 %.",
    "2.4. Sugʻurta mukofoti: 36 032 877 (oʻttiz olti million oʻttiz ikki ming sakkiz yuz yetmish yetti) soʻm.",
    "2.5. Sugʻurta mukofoti boʻlib-boʻlib toʻlanadi: birinchi toʻlov 18 016 438 soʻm — 2026-yil 10-oktyabrgacha; "
    "ikkinchi toʻlov 18 016 439 soʻm — 2027-yil 10-oktyabrgacha.",
    "3. SUGʻURTA MUDDATI",
    "3.1. Sugʻurta muddati: 2026-yil 1-oktyabrdan 2029-yil 30-sentyabrgacha.",
    "4. FRANSHIZA",
    "4.1. Franshiza: qoʻllanilmaydi.",
    "5. SUGʻURTA XAVFLARI",
    "5.1. Sugʻurta xavflari: yongʻin, chaqmoq urishi, portlash, suv bosishi, tabiiy ofatlar, uchinchi shaxslarning "
    "gʻayriqonuniy harakatlari.",
    "6. ISTISNOLAR",
    "6.1. Quyidagilar sugʻurta hodisasi hisoblanmaydi: urush harakatlari, yadro portlashi, sugʻurta "
    "qildiruvchining qasddan qilgan harakatlari.",
    "7. YAKUNIY QOIDALAR",
    "7.1. Sugʻurta qildiruvchi sugʻurta hodisasi yuz berganligi haqida 3 (uch) ish kuni ichida xabar beradi.",
]
CT_RU = [
    "ДОГОВОР СТРАХОВАНИЯ ИМУЩЕСТВА ЮРИДИЧЕСКИХ ЛИЦ № 77/2026",
    "г. Ташкент                                              «1» октября 2026 г.",
    "Акционерное общество «INSON», именуемое в дальнейшем «Страховщик», в лице директора Иванова Ивана "
    "Ивановича, действующего на основании Устава, с одной стороны, и ООО «Ромашка Трейд», именуемое в "
    "дальнейшем «Страхователь», в лице генерального директора Петрова Петра Петровича, с другой стороны, "
    "заключили настоящий договор о нижеследующем:",
    "1. ПРЕДМЕТ ДОГОВОРА",
    "1.1. Объектом страхования являются имущественные интересы Страхователя, связанные с владением нежилым "
    "зданием склада готовой продукции, общая площадь 1 500 кв.м, кадастровый номер 10:00:00:00:00:0077.",
    "1.2. Адрес места страхования: Самаркандская область, г. Самарканд, ул. Навои, 15.",
    "2. СТРАХОВАЯ СУММА. СТРАХОВАЯ ПРЕМИЯ",
    "2.1. Страховая стоимость имущества: 5 000 000 000 (пять миллиардов) сум.",
    "2.2. Страховая сумма по настоящему договору составляет 5 000 000 000 (пять миллиардов) сум.",
    "2.3. Страховой тариф: 0,2 % годовых.",
    "2.4. Страховая премия составляет 10 000 000 (десять миллионов) сум и уплачивается единовременно "
    "до 10.10.2026.",
    "3. СРОК ДЕЙСТВИЯ ДОГОВОРА",
    "3.1. Срок страхования: с 01.10.2026 по 30.09.2027.",
    "4. ФРАНШИЗА",
    "4.1. Франшиза безусловная, 1 % от страховой суммы по каждому страховому случаю.",
    "5. СТРАХОВЫЕ РИСКИ",
    "5.1. Страховыми случаями являются гибель или повреждение имущества в результате:",
    "5.1.1. пожара, удара молнии, взрыва газа;",
    "5.1.2. стихийных бедствий: землетрясения, наводнения, бури;",
    "5.1.3. кражи со взломом, грабежа.",
    "6. ИСКЛЮЧЕНИЯ",
    "6.1. Не являются страховыми случаями события, произошедшие вследствие:",
    "6.1.1. военных действий, террористических актов;",
    "6.1.2. ядерного взрыва, радиации;",
    "6.1.3. умышленных действий Страхователя; износа и коррозии.",
    "7. ПРОЧИЕ УСЛОВИЯ",
    "7.1. Страхователь обязан уведомить Страховщика о наступлении страхового случая в течение 2 (двух) "
    "рабочих дней.",
    "7.2. Территория страхования: Республика Узбекистан.",
]
CT_EN = [
    "PROPERTY INSURANCE POLICY No. INS-2026/0045",
    "Tashkent, October 1, 2026",
    "INSON JSC, hereinafter the Insurer, and ALFA TEXTILE LLC, hereinafter the Policyholder, have agreed:",
    "1. Insured property: warehouse building, total area 2 400 sq.m, cadastral number 10:09:05:01:02:0033.",
    "2. Sum insured: UZS 12,000,000,000.",
    "3. Insured value: UZS 12,000,000,000.",
    "4. Premium rate: 0.1% per annum.",
    "5. Insurance premium: UZS 12,000,000, payable in one payment by October 10, 2026.",
    "6. Period of insurance: from October 1, 2026 to September 30, 2027.",
    "7. Deductible: not applicable.",
    "8. Insured perils: fire, lightning, explosion, earthquake, theft.",
    "9. Exclusions: war, terrorism, nuclear risks, wear and tear.",
]
CT_S = 12_000_000_000.0
CT_PREMIUM = 36_032_877.0
CT_PD = ("Каримов", "Тошпўлатов", "Бахтиёр", "Karimov", "Toshpoʻlatov", "Baxtiyor", "Иванов", "Петров", "Петра")


def pdf_lines(lines, per_page=46, width=92) -> bytes:
    """PDF с текстовым слоем: строки переносятся по ширине страницы (как в настоящем договоре)."""
    import textwrap
    doc = pymupdf.open()
    font = act._fonts()[0]
    wrapped = [w for ln in lines for w in (textwrap.wrap(ln, width) or [""])]
    for k in range(0, len(wrapped), per_page):
        page = doc.new_page()
        tw = pymupdf.TextWriter(page.rect)
        for j, ln in enumerate(wrapped[k:k + per_page]):
            if ln:
                tw.append((40, 50 + 16 * j), ln, font=font, fontsize=9)
        tw.write_text(page)
    return doc.tobytes()


def ctb(b):
    return b.get("contract") or {}


def docx_mixed(paras, tables) -> bytes:
    """DOCX: абзацы, затем таблицы (строки — списки ячеек)."""
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    para = lambda t: f"<w:p><w:r><w:t xml:space=\"preserve\">{_x(t)}</w:t></w:r></w:p>"   # noqa: E731
    body = "".join(para(x) for x in paras)
    for rows in tables:
        body += "<w:tbl>" + "".join("<w:tr>" + "".join(f"<w:tc>{para(c)}</w:tc>" for c in r) + "</w:tr>"
                                    for r in rows) + "</w:tbl>"
    xml = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def check_ct_uz(tag, f, latin=False):
    ok(f"{tag}: номер, дата, место",
       f["contract_no"] == ("46-IM/2026" if latin else "45-ИМ/2026") and f["contract_date"] == "2026-10-01"
       and f["place"] == ("Toshkent shahri" if latin else "Тошкент шаҳри"), (f["contract_no"], f["contract_date"], f["place"]))
    ok(f"{tag}: стороны — юрлица из преамбулы",
       f["insurer"] == {"kind": "legal", "name": '"INSON" AJ' if latin else '"INSON" АЖ'}
       and f["policyholder"] == {"kind": "legal", "name": '"ALFA TEXTILE" MCHJ' if latin else '"ALFA TEXTILE" МЧЖ'},
       (f["insurer"], f["policyholder"]))
    ok(f"{tag}: объект — здание, кадастр, площадь, регион",
       f["class_hint"] == "building" and f["object_kind"] == "warehouse" and f["cadastre_no"] == "10:09:05:01:02:0033"
       and f["areas"]["total_m2"] == 2400.0 and f["region"] == "Ташкентская область"
       and "7" not in (f["address"] or "x"), (f["object_description"], f["address"], f["region"]))
    ok(f"{tag}: суммы, тариф, премия",
       f["object_value"] == CT_S and f["sum_insured"] == CT_S and f["tariff_pct"] == 0.1 and f["premium"] == CT_PREMIUM
       and f["currency"] == "UZS", (f["object_value"], f["sum_insured"], f["tariff_pct"], f["premium"]))
    ok(f"{tag}: рассрочка, график из 2 платежей",
       f["payment_mode"] == "installments" and f["payments"] == [{"date": "2026-10-10", "amount": 18016438.0},
                                                                   {"date": "2027-10-10", "amount": 18016439.0}],
       (f["payment_mode"], f["payments"]))
    ok(f"{tag}: срок 01.10.2026–30.09.2029 — 1 096 дн. включительно",
       f["term_from"] == "2026-10-01" and f["term_to"] == "2029-09-30" and f["term_days"] == 1096,
       (f["term_from"], f["term_to"], f["term_days"]))
    ok(f"{tag}: франшиза не применяется", (f["franchise"] or {}).get("applied") is False, f["franchise"])
    ok(f"{tag}: риски и исключения — короткими кодами",
       {"fire", "lightning", "explosion", "water", "natural"} <= {x["code"] for x in f["covered_risks"]}
       and [x["code"] for x in f["exclusions"]] == ["war", "nuclear", "intent"],
       (f["covered_risks"], f["exclusions"]))
    ok(f"{tag}: срок уведомления о страховом случае", "3" in (f["notice"] or ""), f["notice"])


def check_contract_text():
    print("38а. Договор страхования: файл с текстом (DOCX, PDF) — без модели, три языка")
    fresh()
    model_on(True)
    REPLY["text"] = CRANE_REPLY
    CALLS.clear()
    st, b = upload([("contract.docx", DOCX_MIME, CONTRACT.read_bytes())], {"lang": "ru", "product_code": "0808"})
    c = ctb(b)
    f = c.get("fields") or {}
    ok("учебный договор узнан: блок contract, источник — документ, модель не вызывалась",
       st == 200 and c.get("detected") and c["source"] == "document" and c["kind_label"] == "договор страхования"
       and b["files"][0]["document_kind"] == "договор страхования" and not CALLS, (st, c.get("source"), len(CALLS)))
    ok("учебный договор: номер, дата, место, страхователь-юрлицо",
       f.get("contract_no") == "15/2026" and f["contract_date"] == "2026-09-21" and f["place"] == "г. Ташкент"
       and f["policyholder"] == {"kind": "legal", "name": "ООО «Тестовый склад»"}, f)
    ok("учебный договор: объект, адрес без улицы, регион, конструкция, год постройки",
       f["object_description"] == "склад готовой продукции" and f["object_kind"] == "warehouse"
       and f["address"] == "г. Ташкент, Юнусабадский район" and f["region"] == "город Ташкент"
       and f["construction"] == "кирпич" and f["year_built"] == "2012", f)
    ok("учебный договор: сумма 4,2 млрд, стоимость 5 млрд, срок 12 мес. = 365 дн.",
       f["sum_insured"] == 4.2e9 and f["object_value"] == 5e9 and f["term_days"] == 365 and f["term_from"] is None, f)
    ok("учебный договор: чего нет — null (тариф, премия, франшиза, риски)",
       f["tariff_pct"] is None and f["premium"] is None and f["franchise"] is None and f["covered_risks"] == [])
    fk = [x["code"] for x in c["found"]]
    mk = [x["code"] for x in c["missing"]]
    ok("found / missing с подписями",
       fk == ["contract_no", "contract_date", "policyholder", "object", "sum_insured", "term"]
       and mk == ["tariff_pct", "premium", "franchise", "covered_risks"]
       and c["missing"][0]["label"] == "Тариф", (fk, mk))
    ess = {e["code"]: e["present"] for e in c["essentials"]}
    ok("существенные условия (ГК ст. 929): нет премии и страхового случая",
       ess == {"object": True, "insured_event": False, "sum_insured": True, "premium": False, "term": True}
       and c["legal_ref"] == "ГК РУз, ст. 929" and any("ст. 929" in n for n in c["notes"]), (ess, c["notes"]))
    rq = c["request"]
    ok("contract.request — в формате request (+ условия договора)",
       rq["sum_insured"] == 4.2e9 and rq["term_days"] == 365 and rq["tariff_pct"] is None and rq["source"] == "document"
       and rq["contract_no"] == "15/2026" and rq["contract_date"] == "2026-09-21" and rq["covered_risks"] == []
       and {"tariff_pct", "premium", "franchise", "term_from", "term_to", "term_days", "sum_insured",
            "product_code", "source"} <= set(rq), rq)
    pf = b["prefill"]
    ok("prefill: сумма, стоимость, срок, регион",
       pf["sum_insured"]["value"] == 4.2e9 and pf["object_value"]["value"] == 5e9 and pf["term_days"]["value"] == 365
       and pf["region"]["code"] == "tashkent_city", pf)
    dump = _json.dumps(b, ensure_ascii=False)
    ok("ИНН организации в блок договора не попал", "301234567" not in dump)
    with db.tx() as con:
        audit = db.rows(con, "SELECT detail FROM audit WHERE entity=?", "act_upload:" + b["session"])
    det = _json.loads(audit[0]["detail"])
    ok("журнал: только признак, источник и счётчики договора",
       det.get("contract") is True and det.get("contract_source") == "document" and det.get("contract_found") == 6
       and "Тестовый" not in audit[0]["detail"] and "15/2026" not in audit[0]["detail"], det)
    CT_REPORT["учебный договор"] = {k: f[k] for k in ("contract_no", "contract_date", "place", "policyholder",
                                                    "object_description", "address", "region", "construction",
                                                    "year_built", "sum_insured", "object_value", "term_days")}

    # узбекская кириллица и латиница: DOCX и PDF с текстом
    for tag, lines, latin in (("узб. кириллица DOCX", CT_UZC, False), ("узб. латиница DOCX", CT_UZL, True)):
        CALLS.clear()
        st, b = upload([("shartnoma.docx", DOCX_MIME, docx_bytes([_x(x) for x in lines]))], {"lang": "uz"})
        c = ctb(b)
        ok(f"{tag}: узнан, модель не вызывалась", st == 200 and c.get("detected") and not CALLS
           and c["kind_label"] == "sugʻurta shartnomasi", (st, len(CALLS)))
        check_ct_uz(tag, c["fields"], latin)
        dump = _json.dumps(b, ensure_ascii=False)
        ok(f"{tag}: ФИО директоров не извлечены", not any(x in dump for x in CT_PD), [x for x in CT_PD if x in dump])
        ok(f"{tag}: все ключевые поля найдены, существенные условия есть",
           not c["missing"] and all(e["present"] for e in c["essentials"]), c["missing"])
    for tag, lines, latin in (("узб. кириллица PDF", CT_UZC, False), ("узб. латиница PDF", CT_UZL, True)):
        CALLS.clear()
        st, b = upload([("shartnoma.pdf", "application/pdf", pdf_lines(lines))], {"lang": "uz"})
        c = ctb(b)
        ok(f"{tag}: узнан по тексту (строки перенесены), модель не вызывалась",
           st == 200 and c.get("detected") and not CALLS and b["files"][0]["parsed"], (st, len(CALLS)))
        if c:
            check_ct_uz(tag, c["fields"], latin)

    # русский и английский
    st, b = upload([("dogovor.docx", DOCX_MIME, docx_bytes([_x(x) for x in CT_RU]))], {"lang": "ru"})
    f = ctb(b).get("fields") or {}
    ok("русский: стороны из преамбулы (без представителей), объект, адрес без улицы",
       f.get("insurer", {}).get("name") == "Акционерное общество «INSON»"
       and f["policyholder"]["name"] == "ООО «Ромашка Трейд»" and f["cadastre_no"] == "10:00:00:00:00:0077"
       and f["address"] == "Самаркандская область, г. Самарканд" and f["region"] == "Самаркандская область"
       and not any(x in _json.dumps(b, ensure_ascii=False) for x in ("Иванов", "Петров", "Навои")), f)
    ok("русский: премия 10 млн единовременно до 10.10.2026, тариф 0,2 %, срок 365 дн.",
       f["premium"] == 10_000_000 and f["payment_mode"] == "single"
       and f["payments"] == [{"date": "2026-10-10", "amount": 10_000_000.0}] and f["tariff_pct"] == 0.2
       and f["term_days"] == 365, (f["premium"], f["payments"], f["tariff_pct"], f["term_days"]))
    ok("русский: франшиза безусловная 1 %, риски, исключения, территория, уведомление",
       f["franchise"] == {"applied": True, "text": "безусловная, 1 % от страховой суммы по каждому страховому случаю.",
                          "pct": 1.0, "amount": None, "type": "unconditional", "risk": None}
       and {"fire", "natural", "earthquake", "theft"} <= {x["code"] for x in f["covered_risks"]}
       and {"war", "terrorism", "nuclear", "intent", "wear"} == {x["code"] for x in f["exclusions"]}
       and f["territory"] == "Республика Узбекистан." and "2" in f["notice"], f)
    st, b = upload([("policy.docx", DOCX_MIME, docx_bytes(CT_EN))], {"lang": "en"})
    f = ctb(b).get("fields") or {}
    ok("английский: номер, дата «October 1, 2026», стороны, суммы, тариф, срок, франшиза",
       f.get("contract_no") == "INS-2026/0045" and f["contract_date"] == "2026-10-01" and f["place"] == "Tashkent"
       and f["insurer"]["name"] == "INSON JSC" and f["policyholder"]["name"] == "ALFA TEXTILE LLC"
       and f["sum_insured"] == CT_S and f["premium"] == 12e6 and f["tariff_pct"] == 0.1 and f["term_days"] == 365
       and f["franchise"]["applied"] is False and ctb(b)["kind_label"] == "insurance contract", f)

    # не договор: счёт, письмо; запрос филиала — не договор
    inv = ["СЧЁТ НА ОПЛАТУ № 45 от 01.10.2026", "Плательщик: ООО «Ромашка Трейд»",
           "Назначение платежа: страховая премия по договору страхования № 77/2026",
           "Сумма к оплате: 10 000 000 сум", "Страховая сумма по договору: 5 000 000 000 сум"]
    letter = ["Директору ООО «Ромашка Трейд»", "Просим заключить договор страхования имущества на следующих условиях.",
              "Страховая сумма 5 000 000 000 сум, страховая премия 10 000 000 сум.", "С уважением, отдел продаж"]
    for tag, lines in (("счёт на оплату", inv), ("письмо", letter)):
        st, b = upload([("x.docx", DOCX_MIME, docx_bytes(lines))], {"lang": "ru"})
        ok(f"{tag} — не договор", st == 200 and b.get("contract") is None, ctb(b).get("fields"))
    st, b = upload([("sorov1.docx", DOCX_MIME, docx_table(BR_SAMPLE1))], {"lang": "ru"})
    ok("запрос филиала — не договор (блок branch_request есть, contract нет)",
       b.get("branch_request") and b.get("contract") is None and b.get("cross_check") is None)

    # физлицо-страхователь: признак без имени — нигде
    lines = list(CT_RU)
    lines[2] = ("Акционерное общество «INSON», именуемое в дальнейшем «Страховщик», и гражданин Иванов Иван "
                "Иванович, именуемый в дальнейшем «Страхователь», заключили настоящий договор:")
    lines.insert(3, "Страхователь: Иванов Иван Иванович, паспорт AA1234567")
    st, b = upload([("fiz.docx", DOCX_MIME, docx_bytes(lines))], {"lang": "ru"})
    c = ctb(b)
    dump = _json.dumps(b, ensure_ascii=False)
    with db.tx() as con:
        saved = db.rows(con, "SELECT result_json FROM act_uploads WHERE id=?", b["session"])[0]["result_json"]
        journal = _json.dumps(db.rows(con, "SELECT detail FROM audit WHERE entity=?", "act_upload:" + b["session"]),
                              ensure_ascii=False)
    ok("физлицо-страхователь: kind = individual, имени и паспорта нет в ответе, базе и журнале",
       c["fields"]["policyholder"] == {"kind": "individual", "name": None}
       and not any(x in dump + saved + journal for x in ("Иванов", "AA1234567")), c["fields"]["policyholder"])
    ok("пометка «физическое лицо — данные не извлекаются»", any("физическое лицо" in n for n in c["notes"]), c["notes"])

    # транспорт: марка, модель, год, VIN, госномер; франшиза суммой по риску; срок словами на скане — в тесте скана
    kasko = ["ДОГОВОР СТРАХОВАНИЯ ТРАНСПОРТНОГО СРЕДСТВА (КАСКО) № К-12/2026", "г. Ташкент, 5 октября 2026 г.",
             "Страхователь: ООО «Автолизинг Плюс»", "Объект страхования: легковой автомобиль", "Марка: Chevrolet",
             "Модель: Cobalt", "Год выпуска: 2022", "VIN: XWBJA69V9LA123456", "Государственный номер: 01 A 123 BC",
             "Страховая сумма: 150 000 000 сум", "Страховая премия: 4 500 000 сум",
             "Срок страхования: с 05.10.2026 по 04.10.2027", "Франшиза: безусловная 500 000 сум по риску «ущерб»"]
    st, b = upload([("kasko.docx", DOCX_MIME, docx_bytes(kasko))], {"lang": "ru", "product_code": "0318"})
    f = ctb(b).get("fields") or {}
    rec = {(r["key"], r["value"]) for r in b["recognized"]}
    ok("транспорт: марка, модель, год, VIN, госномер; франшиза 500 000 сум по риску «ущерб»",
       f.get("class_hint") == "vehicle" and f["brand"] == "Chevrolet" and f["model"] == "Cobalt" and f["year"] == "2022"
       and f["vin"] == "XWBJA69V9LA123456" and f["reg_no"] == "01 A 123 BC"
       and f["franchise"] == {"applied": True, "text": "безусловная 500 000 сум по риску «ущерб»", "pct": None,
                              "amount": 500000.0, "type": "unconditional", "risk": "ущерб"}
       and {("serial_no", "XWBJA69V9LA123456"), ("reg_no", "01 A 123 BC"), ("brand", "Chevrolet")} <= rec, f)

    # перечень имущества и график платежей таблицами
    paras = [x for x in CT_RU if not x.startswith("2.4.")] + ["Страховая премия: 8 000 000 сум, уплачивается в рассрочку "
                                                              "по графику."]
    items_t = [["№", "Наименование имущества", "Страховая сумма, сум"], ["1", "Здание склада", "3 000 000 000"],
               ["2", "Стеллажи и погрузчики", "1 500 000 000"], ["", "Итого", "4 500 000 000"]]
    pays_t = [["№", "Дата платежа", "Сумма платежа, сум"], ["1", "10.10.2026", "4 000 000"], ["2", "10.04.2027", "4 000 000"]]
    st, b = upload([("tables.docx", DOCX_MIME, docx_mixed(paras, [items_t, pays_t]))], {"lang": "ru"})
    f = ctb(b).get("fields") or {}
    ok("таблицы: страховая сумма по частям (итог отдельно) и график платежей",
       f.get("items") == [{"name": "Здание склада", "sum": 3e9}, {"name": "Стеллажи и погрузчики", "sum": 1.5e9}]
       and f["items_total"] == 4.5e9 and f["payments"] == [{"date": "2026-10-10", "amount": 4e6},
                                                            {"date": "2027-04-10", "amount": 4e6}]
       and f["payment_mode"] == "installments" and f["premium"] == 8e6, (f.get("items"), f.get("payments")))
    # PDF: текст только на части страниц — разбирается правилами, в модель не уходит
    pdf = pymupdf.open(stream=pdf_lines(CT_RU), filetype="pdf")
    pdf.new_page()
    CALLS.clear()
    st, b = upload([("part.pdf", "application/pdf", pdf.tobytes())], {"lang": "ru"})
    ok("PDF с текстом не на всех страницах — разбор правилами, в модель не отправлен",
       st == 200 and ctb(b).get("detected") and not CALLS and b["files"][0]["parsed"]
       and not b["files"][0]["read_by_ai"], (st, len(CALLS)))


def check_contract_long():
    print("38б. Длинный договор (30 страниц): в пределах срока разбора, PDF с текстом до 60 страниц")
    fresh()
    model_on(True)
    CALLS.clear()
    body = [f"8.{k}. Страховщик обязан в течение 10 рабочих дней рассмотреть документы, представленные "
            f"Страхователем, и принять решение о выплате страхового возмещения либо об отказе в выплате, о чём "
            f"письменно уведомить Страхователя с указанием причин, если иное не предусмотрено правилами страхования "
            f"имущества юридических лиц, утверждёнными Страховщиком (пункт {k})." for k in range(1, 330)]
    lines = CT_RU[:26] + body + CT_RU[26:]
    text_len = sum(len(x) for x in lines)
    import time as _t
    t0 = _t.monotonic()
    st, b = upload([("long.docx", DOCX_MIME, docx_bytes([_x(x) for x in lines]))], {"lang": "ru"})
    dt_docx = _t.monotonic() - t0
    c = ctb(b)
    ok(f"DOCX ≈{text_len // 1000} тыс. знаков: разобран за {dt_docx:.1f} с, договор узнан, без пометки о сроке",
       st == 200 and c.get("detected") and dt_docx < 5 and not CALLS
       and not any("не уложился" in n or "время" in n for n in b["notes"]), (dt_docx, b["notes"]))
    ok("длинный договор: условия из начала и конца найдены",
       c["fields"]["premium"] == 10_000_000 and c["fields"]["territory"] == "Республика Узбекистан."
       and [x["code"] for x in c["fields"]["exclusions"]][:3] == ["war", "terrorism", "nuclear"], c.get("fields"))
    pdf = pdf_lines(lines, per_page=40)
    pages = pymupdf.open(stream=pdf, filetype="pdf").page_count
    t0 = _t.monotonic()
    st, b = upload([("long.pdf", "application/pdf", pdf)], {"lang": "ru"})
    dt_pdf = _t.monotonic() - t0
    c = ctb(b)
    CT_REPORT["длинный договор"] = {"знаков": text_len, "DOCX, с": round(dt_docx, 2), "PDF страниц": pages,
                                    "PDF, с": round(dt_pdf, 2)}
    ok(f"PDF {pages} стр. с текстом (больше 10) принят и разобран за {dt_pdf:.1f} с без модели",
       pages > 10 and st == 200 and not b["rejected"] and c.get("detected") and not CALLS and dt_pdf < 12
       and c["fields"]["premium"] == 10_000_000 and c.get("pages") == pages, (st, b.get("rejected"), dt_pdf))
    ok("PDF-скан больше 10 страниц по-прежнему отклоняется",
       "10 страниц" in (upload([("scan.pdf", "application/pdf", pdf_pages(12))], {"lang": "ru"})[1].get("rejected")
                        or [{}])[0].get("error", ""))
    set_limits(doc_max_text_chars=20000)
    try:
        st, b = upload([("long.docx", DOCX_MIME, docx_bytes([_x(x) for x in lines]))], {"lang": "ru"})
    finally:
        clear_settings()
    c = ctb(b)
    ok("предел текста: договор обрезан — честная пометка truncated и заметка",
       c.get("truncated") is True and any("предела разбора" in n for n in c["notes"]), (c.get("truncated"), c.get("notes")))


def ct_scan_reply(policyholder=None):
    v = {"file": 1, "contract_no": "45-ИМ/2026", "contract_date": "2026-10-01", "place": "Тошкент шаҳри",
         "product_name": "мол-мулкни суғурта қилиш", "product_code": None,
         "insurer": {"is_legal": True, "name": '"INSON" АЖ'},
         "policyholder": {"is_legal": True, "name": '"ALFA TEXTILE" МЧЖ'},
         "beneficiary": None, "pledger": None,
         "object": "нотурар бино — омбор, умумий майдони 2 400 кв.м", "class_hint": "building",
         "address": "Тошкент вилояти, Чирчиқ шаҳри", "cadastre_no": "10:09:05:01:02:0033",
         "object_value": "12 000 000 000", "sum_insured": "12 000 000 000 сўм", "currency": "UZS",
         "tariff": "0,1 %", "premium": "36 032 877", "payment_mode": "installments",
         "payments": [{"date": "2026-10-10", "amount": "18 016 438"}, {"date": "2027-10-10", "amount": 18016439}],
         "term": "2026 йил 1 октябрдан 2029 йил 30 сентябргача", "term_from": "2026-10-01", "term_to": "2029-09-30",
         "franchise": "қўлланилмайди", "covered_risks": ["ёнғин", "портлаш", "табиий офатлар", "сув босиши"],
         "exclusions": ["уруш ҳаракатлари", "ядро портлаши"], "territory": None, "special_terms": [],
         "notice": "3 иш куни"}
    if policyholder:
        v["policyholder"] = {"is_legal": True, "name": policyholder}
    return "```json\n" + _json.dumps({"files": [{"n": 1, "view": "document", "document_kind": "contract"}],
                                      "object_kind": None, "class_hint": None, "condition": None, "fields": [],
                                      "damages": [], "branch_request": None, "contract": v},
                                     ensure_ascii=False) + "\n```"


def check_contract_scan():
    print("38в. Договор: скан — ответ модели (подменён) по строгой схеме")
    fresh()
    model_on(True)
    CALLS.clear()
    REPLY["text"] = ct_scan_reply()
    st, b = upload([("scan.png", "image/png", image((250, 250, 250)))], {"lang": "ru"})
    prompt = CALLS[0]["messages"][0]["content"] + CALLS[0]["messages"][1]["content"]
    ok("в инструкции модели — договор, схема contract, «не выдумывай», запрет имён",
       '"contract": null или' in prompt and "document_kind = contract" in prompt and "не выдумывай" in prompt
       and "is_legal = false" in prompt and llm.mask_pd(prompt) == prompt, prompt[-300:])
    c = ctb(b)
    f = c.get("fields") or {}
    ok("скан: источник photo, файл f1, вид — договор страхования",
       c.get("source") == "photo" and c.get("file") == "f1" and b["files"][0]["document_kind"] == "договор страхования", c)
    ok("скан: те же поля, что у разбора текста (суммы, срок, график, риски)",
       f["sum_insured"] == CT_S and f["premium"] == CT_PREMIUM and f["tariff_pct"] == 0.1 and f["term_days"] == 1096
       and f["payments"] == [{"date": "2026-10-10", "amount": 18016438.0}, {"date": "2027-10-10", "amount": 18016439.0}]
       and [x["code"] for x in f["covered_risks"]] == ["fire", "explosion", "natural", "water"]
       and [x["code"] for x in f["exclusions"]] == ["war", "nuclear"] and f["franchise"]["applied"] is False, f)
    rec = {(r["key"], r["value"], r["source"]) for r in b["recognized"]}
    ok("распознанное со скана — источник «документ», prefill — код, сумма, срок",
       ("premium", "36 032 877", "document") in rec and ("policyholder", '"ALFA TEXTILE" МЧЖ', "document") in rec
       and b["prefill"]["term_days"]["value"] == 1096 and b["prefill"]["sum_insured"]["value"] == CT_S, sorted(rec))
    REPLY["text"] = ct_scan_reply(policyholder="Каримов Алишер Анварович")
    st, b = upload([("scan2.png", "image/png", image((240, 240, 240)))], {"lang": "ru"})
    dump = _json.dumps(b, ensure_ascii=False)
    with db.tx() as con:
        saved = db.rows(con, "SELECT result_json FROM act_uploads WHERE id=?", b["session"])[0]["result_json"]
    REPLY["text"] = ct_scan_reply().replace('"2026 йил 1 октябрдан 2029 йил 30 сентябргача"', '"36 ой"').replace(
        '"term_from": "2026-10-01", "term_to": "2029-09-30"', '"term_from": null, "term_to": null')
    st, b3 = upload([("scan3.png", "image/png", image((230, 230, 230)))], {"lang": "ru"})
    f3 = ctb(b3).get("fields") or {}
    ok("скан: срок словами без дат («36 ой») — 1 095 дн., даты не выдумываются",
       f3.get("term_days") == 1095 and f3["term_from"] is None and f3["term_to"] is None, f3.get("term_days"))
    ok("скан: гражданин вместо организации — kind = individual, имени нет",
       ctb(b)["fields"]["policyholder"] == {"kind": "individual", "name": None}
       and "Каримов" not in dump and "Каримов" not in saved, ctb(b)["fields"]["policyholder"])


CT_AI_LINES = [
    "ДОГОВОР СТРАХОВАНИЯ ИМУЩЕСТВА № 9/2026",
    "г. Ташкент, 1 октября 2026 г.",
    "Акционерное общество «INSON», именуемое в дальнейшем «Страховщик», в лице директора Иванова Ивана Ивановича, "
    "и ООО «Бета Логистик», именуемое в дальнейшем «Страхователь», в лице директора Сидорова Сидора Сидоровича, "
    "заключили настоящий договор.",
    "Страховая сумма: 1 000 000 000 сум.",
    "Страховщик принимает на себя обязательство возместить ущерб, причинённый складскому комплексу Страхователя, "
    "а Страхователь уплачивает Страховщику двенадцать миллионов сумов в течение десяти дней с даты подписания.",
    "Договор действует три года со дня, следующего за днём уплаты первого взноса.",
]


def ct_ai_reply(messages):
    # «модель» видит только текст после маскировки и возвращает условия, которых правила не нашли
    return _json.dumps({"contract": {
        "sum_insured": "2 000 000 000", "premium": "12 000 000", "tariff": "0,4 %",
        "term_from": "2026-10-11", "term_to": "2029-10-10", "object": "складской комплекс",
        "covered_risks": ["пожар", "кража"], "policyholder": {"is_legal": True, "name": "[ФИО]"}}}, ensure_ascii=False)


def check_contract_ai_assist():
    print("38г. Договор: правила нашли мало — текст (после маскировки) дочитывает модель, только пустые поля")
    fresh()
    model_on(True)
    CALLS.clear()
    REPLY["text"] = ct_ai_reply
    st, b = upload([("dogovor9.docx", DOCX_MIME, docx_bytes(CT_AI_LINES))], {"lang": "ru"})
    c = ctb(b)
    f = c.get("fields") or {}
    ok("одно обращение к модели — текстом, без файлов", len(CALLS) == 1 and not CALLS[0]["files"]
       and CALLS[0]["purpose"] == "акт: договор по тексту", [x["purpose"] for x in CALLS])
    sent = " ".join(m["content"] for m in CALLS[0]["messages"]) if CALLS else ""
    ok("в модель ушёл замаскированный текст: ФИО директоров нет, метка [ФИО] есть, сумма и слова договора есть",
       "Иванов" not in sent and "Ивана Ивановича" not in sent and "Сидоров" not in sent and "[ФИО]" in sent
       and "1 000 000 000" in sent and "двенадцать миллионов" in sent, sent[:500])
    ok("найденное правилами не заменено: страховая сумма 1 млрд, а не 2 млрд из ответа модели",
       f.get("sum_insured") == 1e9 and "sum_insured" not in c["field_sources"], (f.get("sum_insured"), c["field_sources"]))
    ok("пустые поля дополнены моделью: премия, срок, объект, риски — source = document_ai",
       f["premium"] == 12e6 and f["term_days"] == 1096 and f["object_description"] == "складской комплекс"
       and set(c["field_sources"]) >= {"premium", "term_days", "object_description", "covered_risks", "tariff_pct"}
       and set(c["field_sources"].values()) == {"document_ai"} and c["source"] == "document_ai", c.get("field_sources"))
    ok("страхователь-юрлицо из правил не заменён меткой [ФИО]",
       f["policyholder"] == {"kind": "legal", "name": "ООО «Бета Логистик»"}, f["policyholder"])
    rec = {(r["key"], r["value"]): r for r in b["recognized"]}
    ok("распознанное: премия от модели — source document_ai и пометка «прочитано моделью из текста, проверьте»",
       rec.get(("premium", "12 000 000"), {}).get("source") == "document_ai"
       and rec[("premium", "12 000 000")]["note"] == "прочитано моделью из текста, проверьте"
       and rec.get(("sum_insured", "1 000 000 000"), {}).get("source") == "document", sorted(rec))
    ok("prefill: срок от модели — с пометкой модели", b["prefill"]["term_days"]["source"] == "document_ai"
       and b["prefill"]["term_days"]["check_label"] == "прочитано моделью из текста, проверьте"
       and b["prefill"]["sum_insured"]["source"] == "document", b["prefill"])
    ok("заметка о полях, прочитанных моделью", any("прочитана моделью" in n for n in c["notes"]), c["notes"])
    with db.tx() as con:
        saved = db.rows(con, "SELECT result_json FROM act_uploads WHERE id=?", b["session"])[0]["result_json"]
    ok("текст договора в базу не сохранён", "двенадцать миллионов" not in saved and "Иванов" not in saved)
    sid_ai = b["session"]
    # настройка contract.ai_assist = false — модель не вызывается
    with db.tx() as con:
        con.execute("INSERT INTO act_settings (created_at, created_by, settings_json, calibrated, note) VALUES "
                    "(?,?,?,?,?)", (db.now(), "тест", _json.dumps({"contract": {"ai_assist": False,
                                                                              "ai_max_chars": 30000}}), 0, "тест"))
    try:
        CALLS.clear()
        st, b = upload([("dogovor9.docx", DOCX_MIME, docx_bytes(CT_AI_LINES))], {"lang": "ru"})
        ok("contract.ai_assist = false — модель не вызывается, поля остаются пустыми",
           not CALLS and ctb(b)["fields"]["premium"] is None and ctb(b)["source"] == "document", len(CALLS))
    finally:
        clear_settings()
    # модель не подключена — договор разобран правилами, без ошибок
    model_on(False)
    CALLS.clear()
    st, b = upload([("dogovor9.docx", DOCX_MIME, docx_bytes(CT_AI_LINES))], {"lang": "ru"})
    ok("модель не подключена — разбор правилами, без обращения", st == 200 and not CALLS and ctb(b)["detected"])
    model_on(True)
    # длинный текст: в модель уходит не больше ai_max_chars, кусками не длиннее 11 000 знаков
    CALLS.clear()
    many = CT_AI_LINES + [f"Пункт {k}. Стороны руководствуются законодательством Республики Узбекистан." * 3
                          for k in range(1, 700)]
    st, b = upload([("dogovor_long.docx", DOCX_MIME, docx_bytes(many))], {"lang": "ru"})
    parts = [m["content"] for m in CALLS[0]["messages"][2:]] if CALLS else []
    ok("длинный текст: в модель — не больше 30 000 знаков, куски по ≤ 11 000",
       CALLS and sum(len(p) for p in parts) <= 30000 + 200 and all(len(p) <= 11100 for p in parts)
       and len(parts) >= 2, [len(p) for p in parts])
    ok("ae.check_settings: contract проверяется",
       ae.check_settings({"contract": {"ai_assist": "да", "ai_max_chars": 10}})
       and not ae.check_settings({"contract": {"ai_assist": False, "ai_max_chars": 20000}}))
    return sid_ai


def ct_make(sid=None, contract=None, optional=None, lang="ru", must=None, request=None):
    body = {"session": sid, "lang": lang,
            "must": must or {"product_code": "0832", "sum_insured": CT_S, "object_value": CT_S,
                             "region": "Ташкентская область"},
            "optional": dict(optional or {})}
    if contract is not None:
        body["optional"]["contract"] = contract
    if request is not None:
        body["optional"]["request"] = request
    return call("POST", "/act/make", body)


def ct_items(a):
    return {i["code"]: i for i in (a.get("contract_check") or {}).get("items") or []}


def check_contract_make():
    print("38д. Сверка договора с расчётом акта (продукт 0832): премия, график, существенные условия, запрос филиала")
    fresh()
    model_on(True)
    REPLY["text"] = CRANE_REPLY
    st, b = upload([("shartnoma.docx", DOCX_MIME, docx_bytes([_x(x) for x in CT_UZC]))], {"lang": "ru"})
    ct = ctb(b)["request"]
    st, a = ct_make(b["session"], ct)
    it = ct_items(a)
    cc = a.get("contract_check") or {}
    lvl = a["risk"]["level"]
    applied = round(max(0.08 * (1 + ae.DEFAULT_SETTINGS["adj_pct"][lvl] / 100), 0.08), 4)
    ok("акт с договором сформирован; contract_check доступна, источник — файл договора",
       st == 200 and cc.get("available") and cc["source"] == "document"
       and cc["source_label"] == "из документа — из файла договора (разбор текста)", (st, cc.get("source_label")))
    ok("срок акта взят из договора: 1 096 дн.",
       a["premium"]["term_days"] == 1096 and it["term"]["verdict"] == "ok" and "взят из договора" in it["term"]["text"],
       it.get("term"))
    ok("тариф договора 0,1 % не ниже минимума 0,08 %",
       it["tariff_min"]["verdict"] == "ok" and it["tariff_min"]["calculated"] == 0.08, it["tariff_min"])
    ok(f"тариф договора против ставки акта {applied}", it["tariff_act"]["verdict"] == ("ok" if 0.1 >= applied else
                                                                                      "differs"), it["tariff_act"])
    ok("премия договора 36 032 877 против расчёта 12 млрд × 0,1 % × 1096/365 = 36 032 876,71 — в допуске",
       it["premium_request"]["verdict"] == "ok" and it["premium_request"]["calculated"] == 36_032_876.71
       and "по тарифу договора" in it["premium_request"]["text"], it["premium_request"])
    ok("график 2 платежей = премия", it["payments"]["verdict"] == "ok" and it["payments"]["calculated"] == CT_PREMIUM,
       it["payments"])
    ok("существенные условия — все есть (ГК РУз, ст. 929)",
       it["essentials"]["verdict"] == "ok" and all(e["present"] for e in cc["essentials"])
       and cc["legal_ref"] == "ГК РУз, ст. 929", it["essentials"])
    ok("как сверено: дословно ст. 929 и формула премии",
       any("должно быть достигнуто соглашение" in h and "ст. 929" in h for h in cc["how"])
       and any("× 0,10 %" in nb(h) and "1096" in h for h in cc["how"]), cc["how"][:2])
    s1 = {r["label"]: r for r in a["sections"][0]["rows"]}
    ok("раздел 1: номер и дата договора", s1.get("Договор страхования", {}).get("value") == "№ 45-ИМ/2026 от 01.10.2026",
       list(s1))
    s4 = {li["title"]: li["items"] for li in a["sections"][3]["lists"]}
    ok("раздел 4: «Сверка с договором», риски и исключения, «Как сверен договор»",
       "Сверка с договором" in s4 and any("Застрахованные риски по договору: пожар" in x for x in s4["Сверка с договором"])
       and any("Исключения по договору: военные действия" in x for x in s4["Сверка с договором"])
       and "Как сверен договор" in s4, list(s4))
    CT_REPORT["сверка УЗ"] = {k: (v["verdict"], v["requested"], v["calculated"]) for k, v in it.items()}
    aid = a["id"]
    st, a0 = ct_make(b["session"], None)
    ok("без optional.contract — договор из своей загрузки (source = session)",
       st == 200 and a0["contract_check"]["source"] == "session" and ct_items(a0)["premium_request"]["verdict"] == "ok",
       a0.get("contract_check", {}).get("source"))

    # график не сходится с премией
    bad = [x.replace("18 016 439", "18 000 000") for x in CT_UZC]
    st, b2 = upload([("shartnoma2.docx", DOCX_MIME, docx_bytes([_x(x) for x in bad]))], {"lang": "ru"})
    st, a2 = ct_make(b2["session"], ctb(b2)["request"])
    p2 = ct_items(a2)["payments"]
    ok("график платежей не сходится с премией — differs, разница 16 439",
       p2["verdict"] == "differs" and p2["diff"] == 16439.0 and "разница" in p2["text"], p2)
    ok("в проверках андеррайтера — график; «принять без оговорок» нельзя",
       any("График платежей" in c for c in a2["decision"]["checks"]) and a2["decision"]["code"] != "accept",
       a2["decision"])

    # договор без срока → нет существенного условия
    no_term = [x for x in CT_RU if not x.startswith(("3.", "3 "))]
    st, b3 = upload([("noterm.docx", DOCX_MIME, docx_bytes([_x(x) for x in no_term]))], {"lang": "ru"})
    c3 = ctb(b3)
    ok("договор без срока: срок не найден, существенного условия нет",
       c3["fields"]["term_days"] is None and not {e["code"]: e["present"] for e in c3["essentials"]}["term"],
       c3["fields"].get("term_days"))
    st, a3 = ct_make(b3["session"], c3["request"], must={"product_code": "0832", "sum_insured": 5e9,
                                                        "object_value": 5e9, "region": "Самаркандская область"})
    e3 = ct_items(a3)["essentials"]
    ok("сверка: essentials — no_essential, «в договоре нет: срок действия договора»",
       e3["verdict"] == "no_essential" and "срок действия договора" in e3["text"]
       and a3["contract_check"]["summary"]["verdict"] == "no_essential", e3)
    ok("решение: не «принять без оговорок», в проверках — существенное условие по ст. 929",
       a3["decision"]["code"] != "accept" and any("ст. 929" in c and "срок" in c for c in a3["decision"]["checks"]),
       a3["decision"])

    # проверка ввода optional.contract
    for badc, key in (({"payments": [{"date": "31.02.2026", "amount": 5}]}, "дата платежа"),
                      ({"payments": "раз"}, "платежи не список"), ({"items": [{"name": "x", "sum": -1}]}, "сумма части"),
                      ({"currency": "XYZ", "premium": 1}, "валюта"), ({"franchise": {"applied": True, "type": "x"}},
                                                                      "тип франшизы"),
                      ({"contract_date": "вчера", "premium": 5}, "дата договора"), ("строка", "не объект"),
                      ({"covered_risks": []}, "пусто")):
        st, e = ct_make(None, badc)
        ok(f"optional.contract проверяется: {key}", st == 422 and "contract" in (e.get("errors") or {}), (st, e))
    st, a4 = ct_make(None, {"premium": "36 032 877", "tariff_pct": "0,1", "sum_insured": CT_S,
                            "term_from": "01.10.2026", "term_to": "30.09.2029", "covered_risks": ["fire", "кража"],
                            "items": [{"name": "склад", "sum": 7e9}, {"name": "оборудование", "sum": 4e9}],
                            "object_description": "склад и оборудование", "source": "input"})
    i4 = ct_items(a4)
    ok("ввод сотрудника: суммы по частям 11 млрд ≠ 12 млрд — items_sum differs, риски из кодов и слов",
       st == 200 and i4["items_sum"]["verdict"] == "differs" and i4["items_sum"]["calculated"] == 11e9
       and [x["code"] for x in a4["contract_check"]["covered_risks"]] == ["fire", "theft"]
       and any("Суммы по объектам" in c for c in a4["decision"]["checks"]), (i4.get("items_sum"), a4.get("contract_check")))
    return aid


def check_contract_cross():
    print("38е. Запрос филиала и договор в одной загрузке: расхождения (cross_check)")
    fresh()
    model_on(True)
    CALLS.clear()
    st, b = upload([("sorov1.docx", DOCX_MIME, docx_table(BR_SAMPLE1)),
                    ("shartnoma.docx", DOCX_MIME, docx_bytes([_x(x) for x in CT_UZC]))], {"lang": "ru"})
    x = b.get("cross_check") or {}
    xi = {i["code"]: i for i in x.get("items") or []}
    ok("оба блока есть: branch_request и contract; модель не вызывалась",
       st == 200 and b.get("branch_request") and b.get("contract") and not CALLS, (st, len(CALLS)))
    ok("cross_check: сумма, стоимость, тариф, премия, срок, объект расходятся; франшиза и код совпадают",
       xi["sum_insured"]["verdict"] == "differs" and xi["sum_insured"]["request"] == BR_S1
       and xi["sum_insured"]["contract"] == CT_S and xi["tariff_pct"]["verdict"] == "differs"
       and xi["premium"]["verdict"] == "differs" and xi["term"]["verdict"] == "differs"
       and xi["object"]["verdict"] == "differs" and xi["franchise"]["verdict"] == "same"
       and xi["product_code"]["verdict"] == "missing", {k: v["verdict"] for k, v in xi.items()})
    ok("cross_check: итог и строки на языке экрана",
       x["summary"]["verdict"] == "differs" and "расходятся" in x["summary"]["text"]
       and any(ln.startswith("Страховая сумма — расходится: в запросе 81") for ln in x["lines"]), x.get("lines"))
    st, a = call("POST", "/act/make", {"session": b["session"], "lang": "ru",
                                       "must": {"product_code": "0832", "sum_insured": BR_S1, "object_value": BR_S1,
                                                "region": "Ташкентская область"},
                                       "optional": {"request": br_request(b), "contract": ctb(b)["request"]}})
    s4 = {li["title"]: li["items"] for li in a["sections"][3]["lists"]}
    ok("акт: подразделы «Сверка с запросом филиала», «Сверка с договором», «Запрос филиала и договор: расхождения»",
       {"Сверка с запросом филиала", "Сверка с договором", "Запрос филиала и договор: расхождения"} <= set(s4), list(s4))
    ok("акт: cross_check в JSON и в проверках андеррайтера",
       a["cross_check"]["available"] and a["cross_check"]["differs"] >= 5
       and any(c.startswith("Запрос филиала и договор расходятся: страховая сумма") for c in a["decision"]["checks"])
       and a["decision"]["code"] != "accept", a["decision"]["checks"])
    return a["id"]


CT_REPORT = {}


def check_contract_langs_files(aid, x_aid):
    print("38ж. Сверка с договором: три языка, Word и PDF")
    for lang, title, word, xt in (("ru", "Сверка с договором", "по тарифу договора", "Запрос филиала и договор: расхождения"),
                                  ("uz", "Shartnoma bilan solishtirish", "shartnoma tarifi", "Filial soʻrovi va shartnoma: farqlar"),
                                  ("en", "Check against the contract", "contract rate", "Branch request vs contract: differences")):
        st, a = call("GET", f"/act/{aid}", params={"lang": lang})
        lines = [x for li in a["sections"][3]["lists"] if li["title"] == title for x in li["items"]]
        ok(f"{lang}: подраздел сверки с договором и текст строки", bool(lines) and any(word in x for x in lines), lines[:3])
        cc = a["contract_check"]
        ok(f"{lang}: contract_check на языке акта (подписи, итог, существенные условия)",
           cc["items"][0]["label"] and cc["summary"]["text"] and all(i["verdict_label"] for i in cc["items"])
           and cc["essentials"][0]["label"], cc["items"][0])
        row1 = [r for r in a["sections"][0]["rows"] if r["label"] == tx.t("ct_row", lang)]
        ok(f"{lang}: раздел 1 — номер и дата договора", row1 and "45-ИМ/2026" in row1[0]["value"]
           and "01.10.2026" in row1[0]["value"], row1)
        if lang != "ru":
            txt = " ".join(lines + cc["how"] + [c for c in a["decision"]["checks"]])
            ok(f"{lang}: в сверке с договором нет кириллицы", not re.search(r"[А-Яа-яЁё]", txt),
               re.findall(r".{20}[А-Яа-яЁё].{20}", txt)[:3])
        st, blob, h = call("GET", f"/act/{aid}.docx", params={"lang": lang}, raw=True)
        plain = re.sub(r"<[^>]+>", "", zipfile.ZipFile(io.BytesIO(blob)).read("word/document.xml").decode("utf-8"))
        ok(f"{lang}: сверка с договором — в JSON и на экране, в документе её нет (номер договора — в разделе 1)",
           title not in plain and "45-ИМ/2026" in plain, plain[-300:])
        st, blob, h = call("GET", f"/act/{aid}.pdf", params={"lang": lang}, raw=True)
        text = pdf_text(pymupdf.open(stream=blob, filetype="pdf")).replace("ʻ", "'")
        ok(f"{lang}: сверка с договором в PDF — то же", title.replace("ʻ", "'") not in text and "45-ИМ/2026" in text,
           text[-300:])
        st, blob, h = call("GET", f"/act/{x_aid}.docx", params={"lang": lang}, raw=True)
        plain = re.sub(r"<[^>]+>", "", zipfile.ZipFile(io.BytesIO(blob)).read("word/document.xml").decode("utf-8"))
        st, blob, h = call("GET", f"/act/{x_aid}.pdf", params={"lang": lang}, raw=True)
        text = pdf_text(pymupdf.open(stream=blob, filetype="pdf")).replace("ʻ", "'")
        ok(f"{lang}: «запрос филиала и договор» — в JSON (cross_check), в DOCX и PDF не выносится",
           xt not in plain and xt.replace("ʻ", "'") not in text
           and call("GET", f"/act/{x_aid}", params={"lang": lang})[1]["cross_check"])
    with db.tx() as con:
        row = db.rows(con, "SELECT act_json FROM acts WHERE id=?", aid)[0]
        stored = _json.loads(row["act_json"])
    for k in ("contract", "contract_check", "cross_check"):
        stored["data"].pop(k, None)
    out = act.render(stored["data"], "ru", stored["meta"])
    ok("старый акт без договора: contract_check.available = false, cross_check.available = false",
       out["contract_check"]["available"] is False and out["cross_check"]["available"] is False
       and "Сверка с договором" not in [li["title"] for li in out["sections"][3]["lists"]])


# ================================================================================================
#  39. Замечания контролёра 30.09.2026 (вечер): источник условий решает сервер, правки в акте, вид документа,
#      существенные условия, сверка «запрос ↔ договор», проверка ввода, маскировка счёта, стороны-юрлица
# ================================================================================================

def _s4_lines(a, title):
    return [x for li in a["sections"][3]["lists"] if li["title"] == title for x in li["items"]]


def check_trust_edits():
    print("39а. Источник условий решает сервер: совпало с загрузкой — «из документа», иначе — правка «было → стало»")
    fresh()
    model_on(True)
    REPLY["text"] = CRANE_REPLY
    st, b = upload([("sorov1.docx", DOCX_MIME, docx_table(BR_SAMPLE1))], {"lang": "ru"})
    sid = b["session"]
    req = br_request(b)
    ok("optional.request загрузки: стоимость и объект для сверки запроса с договором",
       req.get("object_value") == BR_S1 and req.get("cadastre_no") == "10:00:00:00:00:00001"
       and req.get("class_hint") == "building" and "музлатгич" in (req.get("object_description") or ""), req)
    # 1) как прочитано — «из документа», правок нет
    st, a = br_make(sid, 1, dict(req, source="input"))          # source экрана не доверяется ни в какую сторону
    rc = a["request_check"]
    ok("условия как в загрузке: источник «из документа», правок нет (source экрана не учитывается)",
       st == 200 and rc["source_kind"] == "document" and rc["source"] == "document"
       and rc["source_label"].startswith("из документа") and rc["edits"]["count"] == 0
       and rc["edits"]["line"] == "Правки сотрудника в условиях запроса: правок нет"
       and set(rc["field_sources"].values()) == {"document"} and rc["field_sources"]["tariff_pct"] == "document",
       (rc.get("source_kind"), rc.get("field_sources")))
    lines = _s4_lines(a, "Сверка с запросом филиала")
    ok("раздел 4: источник условий и «правок нет»",
       "Источник условий: из документа — из файла запроса (разбор текста)" in lines
       and "Правки сотрудника в условиях запроса: правок нет" in lines, lines)
    ok("без правок в проверках нет пункта о правках",
       not any("правки сотрудника" in c for c in a["decision"]["checks"]), a["decision"]["checks"])
    # 2) сотрудник исправил тариф и премию, экран утверждает «из документа» — сервер видит правки
    edited = dict(req, tariff_pct=0.1, premium=246_644_000, source="document")
    st, a2 = br_make(sid, 1, edited)
    rc2 = a2["request_check"]
    codes = [e["code"] for e in rc2["edits"]["items"]]
    ok("правки тарифа и премии: «из документа с правками сотрудника (2)», поля — «введено сотрудником»",
       st == 200 and rc2["source_kind"] == "document_edited" and codes == ["tariff_pct", "premium"]
       and rc2["source_label"].startswith("из документа с правками сотрудника (2)")
       and rc2["field_sources"]["tariff_pct"] == "input" and rc2["field_sources"]["premium"] == "input"
       and rc2["field_sources"]["term_days"] == "document", (rc2.get("source_label"), codes, rc2.get("field_sources")))
    ed = {e["code"]: e for e in rc2["edits"]["items"]}
    ok("правка: что, было, стало — числа и текст на языке акта",
       ed["tariff_pct"]["was"] == 0.05 and ed["tariff_pct"]["now"] == 0.1
       and nb(ed["tariff_pct"]["text"]) == "Тариф: было 0,05 % → стало 0,10 %"
       and nb(ed["premium"]["text"]) == "Страховая премия: было 123 322 000 сум → стало 246 644 000 сум"
       and rc2["edits"]["line"] == "Правки сотрудника в условиях запроса: 2", rc2["edits"])
    lines2 = [nb(x) for x in _s4_lines(a2, "Сверка с запросом филиала")]
    ok("раздел 4: строка правок и список «было → стало»",
       "Правки сотрудника в условиях запроса: 2" in lines2 and "Тариф: было 0,05 % → стало 0,10 %" in lines2, lines2)
    ok("решение: «проверить правки сотрудника в условиях запроса», без оговорок принять нельзя",
       any(c.startswith("Проверить правки сотрудника в условиях запроса (2)") for c in a2["decision"]["checks"])
       and a2["decision"]["code"] != "accept", a2["decision"])
    st, blob, h = call("GET", f"/act/{a2['id']}.docx", params={"lang": "ru"}, raw=True)
    plain = nb(re.sub(r"<[^>]+>", "", zipfile.ZipFile(io.BytesIO(blob)).read("word/document.xml").decode("utf-8")))
    ok("Word: правки сотрудника «было → стало» — в JSON и на экране (раздел 4), в документ не выносятся",
       "Правки сотрудника в условиях запроса: 2" not in plain and "было 0,05" not in plain, plain[-400:])
    st, blob, h = call("GET", f"/act/{a2['id']}.pdf", params={"lang": "ru"}, raw=True)
    text = nb(pdf_text(pymupdf.open(stream=blob, filetype="pdf")))
    ok("PDF: правок «было → стало» в документе нет", "было 0,05" not in text, text[-400:])
    for lang, word in (("uz", "Soʻrov shartlaridagi xodim tuzatishlari: 2"), ("en", "Staff edits to the request terms: 2")):
        st, al = call("GET", f"/act/{a2['id']}", params={"lang": lang})
        ok(f"{lang}: строка правок на языке акта, без кириллицы",
           al["request_check"]["edits"]["line"] == word
           and not re.search(r"[А-Яа-яЁё]", " ".join(e["text"] for e in al["request_check"]["edits"]["items"])),
           al["request_check"]["edits"])
    # 3) экран говорит «из документа», а загрузки нет (истекла / сменилась сессия) — всё «введено сотрудником»
    for sess, why in ((None, "сессии нет"), ("нет-такой-загрузки", "загрузка чужая или истекла")):
        st, a3 = br_make(sess, 1, dict(req, source="document"))
        rc3 = a3["request_check"]
        ok(f"{why}: все поля «введено сотрудником», пометка «документ недоступен»",
           st == 200 and rc3["source_kind"] == "input" and rc3["source"] == "input" and rc3["document_missing"]
           and set(rc3["field_sources"].values()) == {"input"}
           and rc3["source_label"] == "введено сотрудником — документ недоступен (прошло больше 24 часов или "
                                      "сменилась сессия)", rc3.get("source_label"))
    st, a4 = br_make(None, 1, {"tariff_pct": 0.1, "term_days": 365})
    ok("ввод сотрудника без документа: «введено сотрудником», без пометки о недоступном документе",
       a4["request_check"]["source_label"] == "введено сотрудником" and not a4["request_check"]["document_missing"],
       a4["request_check"].get("source_label"))
    # старый акт (без источника по полям) рисуется как раньше
    with db.tx() as con:
        stored = _json.loads(db.rows(con, "SELECT act_json FROM acts WHERE id=?", a2["id"])[0]["act_json"])
    for k in ("source_kind", "origin", "edits", "field_sources", "doc_missing"):
        stored["data"]["request_check"].pop(k, None)
    out = act.render(stored["data"], "ru", stored["meta"])
    ok("старый акт без источника по полям: без строки правок, подпись источника прежняя",
       out["request_check"]["available"] and "edits" not in out["request_check"]
       and not any("Правки сотрудника" in x for x in _s4_lines(out, "Сверка с запросом филиала"))
       and out["request_check"]["source_label"] == "из файла запроса (разбор текста)", out["request_check"].get("source_label"))

    # договор: правка премии; риски со скана — «прочитано моделью»
    st, b = upload([("shartnoma.docx", DOCX_MIME, docx_bytes([_x(x) for x in CT_UZC]))], {"lang": "ru"})
    ct = ctb(b)["request"]
    st, a5 = ct_make(b["session"], dict(ct, premium=36_000_000.5))
    cc = a5["contract_check"]
    ok("договор: правка премии — «было → стало» с тийинами, остальное из документа",
       cc["source_kind"] == "document_edited" and [e["code"] for e in cc["edits"]["items"]] == ["premium"]
       and nb(cc["edits"]["items"][0]["text"]) == "Страховая премия: было 36 032 877 сум → стало 36 000 000,50 сум"
       and cc["field_sources"]["covered_risks"] == "document"
       and any(c.startswith("Проверить правки сотрудника в условиях договора (1)") for c in a5["decision"]["checks"]),
       cc.get("edits"))
    ok("договор: строка правок в разделе 4",
       "Правки сотрудника в условиях договора: 1" in _s4_lines(a5, "Сверка с договором"), _s4_lines(a5, "Сверка с договором"))
    REPLY["text"] = ct_scan_reply()
    st, bs = upload([("scan.png", "image/png", image((250, 250, 250)))], {"lang": "ru"})
    st, a6 = ct_make(bs["session"], ctb(bs)["request"])
    cc6 = a6["contract_check"]
    risks = [x for x in _s4_lines(a6, "Сверка с договором") if x.startswith("Застрахованные риски по договору")]
    ok("скан договора: источник «из документа — со скана», риски в акте помечены «прочитано моделью»",
       cc6["source_kind"] == "document" and cc6["field_sources"]["covered_risks"] == "photo"
       and cc6["source_label"] == "из документа — со скана договора (распознано моделью)"
       and risks and risks[0].endswith("(прочитано моделью)") and cc6["covered_risks_by_model"] is True, (risks, cc6.get("source_label")))
    ok("договор из файла: риски без пометки модели",
       all(not x.endswith("(прочитано моделью)") for x in _s4_lines(a5, "Сверка с договором")))
    REPLY["text"] = CRANE_REPLY


def check_doc_kind_title():
    print("39б. Вид документа: заголовок сильнее строк; заявление — без сверки договора")
    fresh()
    model_on(True)
    REPLY["text"] = CRANE_REPLY
    CALLS.clear()
    ru_rows = [("1.", "Вид страхования (код):", "0832"), ("2.", "Страхователь:", 'ООО "Ромашка"'),
               ("3.", "Выгодоприобретатель:", 'АКБ "Намунабанк"'), ("4.", "Залогодатель:", ""),
               ("5.", "Объект страхования:", "Нежилое здание — склад, общая площадь 1 200 кв.м"),
               ("6.", "Страховая стоимость:", "1 000 000 000,00 сум"),
               ("7.", "Страховая сумма:", "1 000 000 000,00 сум"), ("8.", "Франшиза:", "не применяется"),
               ("9.", "Страховой тариф:", "0,1"), ("10.", "Страховая премия:", "1 000 000,00 сум"),
               ("11.", "Срок страхования:", "с 01.10.2026 по 30.09.2027")]
    st, b0 = upload([("zapros.docx", DOCX_MIME, docx_table(ru_rows, head=[]))], {"lang": "ru"})
    ok("те же строки без заголовка — запрос филиала", bool(b0.get("branch_request")) and not b0.get("contract"))
    st, b = upload([("polis.docx", DOCX_MIME, docx_table(ru_rows, head=["СТРАХОВОЙ ПОЛИС № 7/2026"]))], {"lang": "ru"})
    ok("«СТРАХОВОЙ ПОЛИС» со строками «подпись: значение» — договор/полис, не запрос филиала",
       st == 200 and b.get("branch_request") is None and ctb(b).get("detected")
       and b["documents"][0]["kind"] == "contract", (b.get("branch_request"), b.get("documents")))
    for head in ("SUGʻURTA POLISI № 12", "СУҒУРТА ПОЛИСИ", "ПОЛИС", "ДОГОВОР СТРАХОВАНИЯ ИМУЩЕСТВА № 9"):
        st, b = upload([("p.docx", DOCX_MIME, docx_table(BR_SAMPLE1, head=[head]))], {"lang": "ru"})
        ok(f"«{head}» + строки бланка — договор/полис", b.get("branch_request") is None and ctb(b).get("detected"),
           (head, b.get("documents")))
    app_lines = ["ЗАЯВЛЕНИЕ НА СТРАХОВАНИЕ ИМУЩЕСТВА", "Страхователь: ООО «Ромашка»",
                 "Объект страхования: нежилое здание — склад, общая площадь 1 200 кв.м",
                 "Страховая стоимость: 1 200 000 000 сум", "Страховая сумма: 1 000 000 000 сум",
                 "Страховой тариф: 0,2 %", "Срок страхования: с 01.10.2026 по 30.09.2027"]
    for name, lines in (("ЗАЯВЛЕНИЕ НА СТРАХОВАНИЕ", app_lines),
                        ("АРИЗА", ["АРИЗА"] + app_lines[1:]), ("ARIZA", ["Sugʻurta qilish uchun ARIZA"] + app_lines[1:])):
        st, b = upload([("z.docx", DOCX_MIME, docx_bytes([_x(x) for x in lines]))], {"lang": "ru"})
        rec = {(r["key"], r["value"]) for r in b["recognized"]}
        ok(f"{name}: вид «заявление», данные в распознанном и подсказке, сверки договора нет",
           st == 200 and b["documents"][0]["kind"] == "application"
           and b["files"][0]["document_kind"] == "заявление на страхование"
           and b.get("contract") is None and b.get("branch_request") is None
           and ("sum_insured", "1 000 000 000") in rec and (b.get("prefill") or {}).get("sum_insured", {}).get("value") == 1e9
           and any("это заявление, а не договор" in n.lower() for n in b["notes"]), (b.get("documents"), b.get("notes")))
    st, a = call("POST", "/act/make", {"session": b["session"], "lang": "ru",
                                       "must": {"product_code": "0808", "sum_insured": 1e9, "object_value": 1.2e9,
                                                "region": "Ташкентская область"}})
    ok("акт по заявлению: сверки с договором и существенных условий нет",
       st == 200 and a["contract_check"]["available"] is False
       and not any("929" in c for c in a["decision"]["checks"]), a.get("contract_check"))


def check_essentials_wording():
    print("39в. Существенные условия: «в тексте договора не найдено … проверьте договор» (три языка)")
    fresh()
    no_term = [x for x in CT_RU if not x.startswith(("3.", "3 "))]
    st, b = upload([("noterm.docx", DOCX_MIME, docx_bytes([_x(x) for x in no_term]))], {"lang": "ru"})
    ok("экран: заметка не утверждает, что условия нет",
       any(n.startswith("В тексте договора не найдено условие: срок действия договора (ГК РУз, ст. 929). "
                        "Проверьте договор") for n in ctb(b)["notes"]), ctb(b)["notes"])
    st, a = ct_make(b["session"], ctb(b)["request"], must={"product_code": "0832", "sum_insured": 5e9,
                                                          "object_value": 5e9, "region": "Самаркандская область"})
    e = ct_items(a)["essentials"]
    ok("акт: строка сверки и проверка андеррайтера — «не найдено … если условия действительно нет — дополнить»",
       e["verdict"] == "no_essential" and e["verdict_label"] == "условие не найдено"
       and "в тексте договора не найдено условие: срок действия договора" in e["text"]
       and "если условия действительно нет — договор нужно дополнить" in e["text"]
       and any(c.startswith("В тексте договора не найдено условие: срок действия договора") for c in a["decision"]["checks"])
       and "не найдено существенное условие" in a["contract_check"]["summary"]["text"], (e, a["decision"]["checks"]))
    for lang, word in (("uz", "Shartnoma matnida shart topilmadi"), ("en", "The contract text does not contain the term")):
        st, al = call("GET", f"/act/{a['id']}", params={"lang": lang})
        ok(f"{lang}: формулировка без категоричности",
           any(c.startswith(word) for c in al["decision"]["checks"]), al["decision"]["checks"])
    for lg, v in (("ru", "нет в одном из документов"), ("uz", "hujjatlardan birida yoʻq"),
                  ("en", "missing in one of the documents")):
        ok(f"x_v_missing ({lg}): «{v}»", tx.t("x_v_missing", lg) == v)


def check_cross_same():
    print("39г. Сверка «запрос ↔ договор» в акте совпадает со сверкой при загрузке; объект — сопоставимое")
    fresh()
    model_on(True)
    REPLY["text"] = CRANE_REPLY
    st, b = upload([("sorov1.docx", DOCX_MIME, docx_table(BR_SAMPLE1)),
                    ("shartnoma.docx", DOCX_MIME, docx_bytes([_x(x) for x in CT_UZC]))], {"lang": "ru"})
    up = {i["code"]: (i["verdict"], i["text"]) for i in b["cross_check"]["items"]}
    st, a = call("POST", "/act/make", {"session": b["session"], "lang": "ru",
                                       "must": {"product_code": "0832", "sum_insured": BR_S1, "object_value": BR_S1,
                                                "region": "Ташкентская область"},
                                       "optional": {"request": br_request(b), "contract": ctb(b)["request"]}})
    got = {i["code"]: (i["verdict"], i["text"]) for i in a["cross_check"]["items"]}
    ok("все строки сверки акта = строки сверки загрузки (вывод и текст)", got == up,
       {k: (up.get(k), got.get(k)) for k in set(up) | set(got) if up.get(k) != got.get(k)})
    ok("стоимость сверяется (раньше в акте её не было в запросе), объект — по кадастру",
       got["object_value"][0] == "differs" and got["object"][0] == "differs"
       and a["cross_check"]["items"][-1]["compared"] == "cadastre", got)
    # при живой загрузке объект — сохранённый, присланный вид объекта не подменяет его
    st, a1 = call("POST", "/act/make", {"session": b["session"], "lang": "ru",
                                        "must": {"product_code": "0832", "sum_insured": BR_S1, "object_value": BR_S1,
                                                 "region": "Ташкентская область"},
                                        "optional": {"request": dict(br_request(b), cadastre_no=None, class_hint="cargo"),
                                                     "contract": ctb(b)["request"]}})
    xo = [i for i in a1["cross_check"]["items"] if i["code"] == "object"][0]
    ok("живая загрузка: объект из загрузки (кадастр сохранён), правка кадастра видна в правках",
       xo["compared"] == "cadastre" and xo["request"]["cadastre_no"] == "10:00:00:00:00:00001"
       and "cadastre_no" in [e["code"] for e in a1["request_check"]["edits"]["items"]], (xo, a1["request_check"]["edits"]))
    # сравнить нечем: кадастр только в договоре, вида объекта в запросе нет
    st, a2 = ct_make(None, {"premium": CT_PREMIUM, "sum_insured": CT_S, "cadastre_no": "10:00:00:00:00:00002",
                            "tariff_pct": 0.1, "term_days": 1096},
                     request={"tariff_pct": 0.1, "premium": CT_PREMIUM, "term_days": 1096})
    x2 = [i for i in a2["cross_check"]["items"] if i["code"] == "object"][0]
    lines = _s4_lines(a2, "Запрос филиала и договор: расхождения")
    ok("объект: сравнить нечем — вердикт missing, строка «сравнить нечем: в запросе …, в договоре …»",
       x2["verdict"] == "missing" and x2["text"] == "Объект — сравнить нечем: в запросе данные недоступны, "
                                                     "в договоре 10:00:00:00:00:00002"
       and x2["text"] in lines and not any("объект" in c.lower() for c in a2["decision"]["checks"]
                                           if c.startswith("Запрос филиала и договор")), (x2, lines))
    # вид с видом: кадастра нет в запросе, вид — в обоих
    st, a3 = ct_make(None, {"premium": CT_PREMIUM, "sum_insured": CT_S, "cadastre_no": "10:00:00:00:00:00002",
                            "object_description": "нежилое здание склада", "tariff_pct": 0.1, "term_days": 1096},
                     request={"tariff_pct": 0.1, "term_days": 1096, "object_description": "технологическое оборудование"})
    x3 = [i for i in a3["cross_check"]["items"] if i["code"] == "object"][0]
    ok("объект: кадастр только в одном — сравнивается вид объекта с видом (здание ≠ оборудование)",
       x3["compared"] == "kind" and x3["verdict"] == "differs"
       and x3["text"] == "Объект — расходится: в запросе оборудование, в договоре здание, помещение", x3)


def check_input_bounds():
    print("39д. Проверка ввода: даты 2000–2100, срок — только целое, разумный предел сумм")
    fresh()
    for bad, key in (({"term_from": "1999-12-31", "term_to": "2000-12-30"}, "срок с 1999 года"),
                     ({"term_from": "2100-06-01", "term_to": "2101-01-01"}, "срок по 2101 год"),
                     ({"term_days": 365.5}, "term_days 365.5"), ({"term_days": "365,5"}, "term_days «365,5»"),
                     ({"term_days": "365 дней"}, "term_days текстом"),
                     ({"premium": 2e14}, "премия больше 10^14"),
                     ({"premium": 5e9, "sum_insured": 1e9}, "премия больше страховой суммы")):
        st, e = br_make(None, 1, bad)
        ok(f"optional.request: {key} — 422", st == 422 and "request" in (e.get("errors") or {}), (st, e.get("errors")))
    for bad, key in (({"premium": 5, "contract_date": "1990-01-01"}, "дата договора 1990"),
                     ({"payments": [{"date": "2150-01-01", "amount": 5}]}, "платёж в 2150 году"),
                     ({"premium": 5, "term_days": 365.5}, "срок договора 365.5"),
                     ({"items": [{"name": "склад", "sum": 5e14}]}, "сумма части больше 10^14")):
        st, e = ct_make(None, badc := bad)
        ok(f"optional.contract: {key} — 422", st == 422 and "contract" in (e.get("errors") or {}), (st, badc, e.get("errors")))
    st, e = br_make(None, 1, None, {"term_days": 365.5})
    ok("optional.term_days 365.5 — 422", st == 422 and "term_days" in (e.get("errors") or {}), (st, e.get("errors")))
    st, a = br_make(None, 1, {"term_days": 365.0, "premium": "1 000 000"})
    ok("целое в записи 365.0 принимается", st == 200 and rq_items(a)["term"]["requested"] == 365, st)


def check_ct_ai_mask_labels():
    print("39е. Договор с р/с — счёт не уходит в модель; заметка «прочитано моделью» — подписями, без служебных полей")
    fresh()
    model_on(True)
    CALLS.clear()
    REPLY["text"] = ct_ai_reply
    lines = CT_AI_LINES + ["Реквизиты Страхователя: р/с 20208000900123456001, МФО 00873, х/р 2020 8000 9051 2345 6001, "
                           "карта 8600 1234 5678 9012."]
    st, b = upload([("dogovor_rs.docx", DOCX_MIME, docx_bytes(lines))], {"lang": "ru"})
    sent = " ".join(m["content"] for m in CALLS[0]["messages"]) if CALLS else ""
    ok("в модель ушёл текст без счёта, МФО и карты (метки [СЧЁТ], [МФО])",
       CALLS and not any(x in sent for x in ("20208000900123456001", "2020 8000 9051", "00873", "8600 1234"))
       and "[СЧЁТ]" in sent and "[МФО]" in sent and "1 000 000 000" in sent, sent[-300:])
    notes = [n for n in ctb(b)["notes"] if "прочитана моделью" in n]
    ok("заметка: поля подписями по-русски, служебных имён нет",
       notes and "страховая премия" in notes[0] and "срок страхования" in notes[0]
       and not re.search(r"[a-z]+_[a-z]+|class_hint|object_kind|term_text", notes[0]), notes)
    st, b2 = upload([("dogovor_rs.docx", DOCX_MIME, docx_bytes(lines))], {"lang": "uz"})
    n2 = [n for n in ctb(b2)["notes"] if "model" in n.lower()]
    ok("uz: подписи на узбекском", n2 and "sugʻurta mukofoti" in n2[0] and not re.search(r"[А-Яа-я]|_", n2[0]), n2)
    REPLY["text"] = CRANE_REPLY


def check_parties_amounts():
    print("39ж. Стороны-юрлица (банк слитно, маркеры, ЧП, «в лице …»), отрицательные суммы, множители, тийины")
    from app import branch_request as brm, contract_read as crm
    cases = (('"NAMUNA SAVDO" MCHJ', "legal"), ("Namunabank", "legal"), ("Намунабанк", "legal"),
             ("Sinov banki", "legal"), ('"OMAD" AJ', "legal"), ("АТБ «Намуна»", "legal"), ('"SINOV" XK', "legal"),
             ('"SINOV" QK', "legal"), ("DUK «Namuna»", "legal"), ('"Namuna" OK', "legal"),
             ('"Bahor" fermer xoʻjaligi', "legal"), ("фермер хўжалиги «Баҳор»", "legal"), ("ООО «Намуна»", "legal"),
             ("АО «Намуна»", "legal"), ("АКБ «Намуна»", "legal"), ("ЧП Каримов", "legal"),
             ("ИП Каримов", "individual"), ("ЯТТ Каримов", "individual"), ("YaTT Karimov", "individual"),
             ("Каримов Алишер Анварович", "individual"))
    bad = [(n, brm.party(n)) for n, kind in cases if brm.party(n)["kind"] != kind]
    ok("маркеры организаций и физлиц", not bad, bad)
    ok("«ЧП Каримов» — частное предприятие: юрлицо, название как есть",
       brm.party("ЧП Каримов") == {"kind": "legal", "name": "ЧП Каримов"})
    ok("«ООО «Ромашка», в лице директора Иванова И.И.» → «ООО «Ромашка»»",
       brm.party("ООО «Ромашка», в лице директора Иванова И.И.") == {"kind": "legal", "name": "ООО «Ромашка»"})
    ok("отрицательное число не превращается в положительное",
       brm.amount("-123 322 000,00 сўм") is None and brm.amount_ex("−5 000")[1] == "negative"
       and brm.amount("5 000") == 5000.0, brm.amount("-123 322 000,00 сўм"))
    ok("множители «млн/млрд/mln/mlrd» — как в договоре (общая функция)",
       brm.amount("1,5 млрд сум") == 1.5e9 and brm.amount("250 mln soʻm") == 2.5e8 and brm.amount("2 mlrd") == 2e9
       and crm.money("1,5 млрд сум")["value"] == 1.5e9 and brm.SCALE is crm._SCALE)
    rows = [(n, l, "-123 322 000,00 сўм" if n == "10." else v) for n, l, v in BR_SAMPLE1]
    st, b = upload([("neg.docx", DOCX_MIME, docx_table(rows))], {"lang": "ru"})
    f = b["branch_request"]["fields"]
    ok("бланк с отрицательной премией: премии нет, пометка для сотрудника",
       f["premium"] is None and f["amount_errors"] == ["premium"]
       and any("отрицательное число (страховая премия)" in n for n in b["branch_request"]["notes"]), b["branch_request"]["notes"])
    req2 = {"tariff_pct": 0.05, "premium": "122 589 000,00", "term_from": "07.09.2026", "term_to": "2031-11-07"}
    st, a = br_make(None, 2, req2)
    ok("«как считали»: сумма с тийинами (47 397 852 345,04)",
       any("47 397 852 345,04 сум" in nb(h) for h in a["request_check"]["how"]), a["request_check"]["how"])


def check_pdf_time_limit():
    print("39з. Запас времени для длинного договора: PDF с текстом — 8 с на файл, общий срок — 12 с")
    from app import act_extras as axm
    ok("настройки по умолчанию: doc_file_sec_pdf = 8, doc_parse_total_sec = 12, doc_parse_sec = 5",
       ae.DEFAULT_SETTINGS["limits"]["doc_file_sec_pdf"] == 8 and ae.DEFAULT_SETTINGS["limits"]["doc_parse_total_sec"] == 12
       and ae.DEFAULT_SETTINGS["limits"]["doc_parse_sec"] == 5 and axm.DOC_LIMITS["doc_file_sec_pdf"] == 8
       and "doc_file_sec_pdf" in ae.LIMIT_BOUNDS)
    seen = []
    orig = axm.parse_document_limited

    def spy(con, path, cls, dl, sec, inclusive=True):
        seen.append((Path(path).suffix, round(sec, 1)))
        return orig(con, path, cls, dl, sec, inclusive)
    axm.parse_document_limited = spy
    try:
        fresh()
        st, b = upload([("dog.pdf", "application/pdf", pdf_lines(CT_RU)),
                        ("dog.docx", DOCX_MIME, docx_bytes([_x(x) for x in CT_RU]))], {"lang": "ru"})
    finally:
        axm.parse_document_limited = orig
    ok("PDF — срок 8 с, DOCX — 5 с", st == 200 and seen and seen[0] == (".pdf", 8.0) and seen[1] == (".docx", 5.0), seen)


# ------------------------------------------------------------------ 40. аналитика раздела 4 (30.09.2026)

AN_TITLES_RU = ["Разбор по рискам: доля в нетто-ставке и уровень",
                "Учтённые факторы: значение, источник, вклад в техническую ставку",
                "Что изменит ставку (посчитано расчётным модулем)", "Состав тарифа", "Сценарии убытка подробно",
                "Сценарии «что если» (расчёт при других данных объекта)",
                "Лимит удержания и перестрахование", "Балл риска 0–100 (справочно)", "Рынок и статистика",
                "Франшиза: варианты (справочно)", "Мероприятия: эффект на ставку и премию"]
AN_SOURCES = {"input", "document", "photo", "plate", "marking", "text", "kind", "default", "not_set", "act_terms"}
WH8_MUST = {"product_code": "0807", "sum_insured": 4_200_000_000, "object_value": 4_200_000_000,
            "region": "Ташкентская область"}
WH8_OPT = {"object_kind": "warehouse", "protection": "alarm", "seismic_zone": 8, "construction": "reinforced",
           "losses_3y": {"count": 0, "small_count": 0}}
AN_REPORT = {}


def s4_titles(a):
    return [li["title"] for li in a["sections"][3]["lists"]]


def an_common(tag, a, S, term):
    """Проверки, общие для всех объектов: блоки на месте, факторы с источниками, вклады сходятся, балл сходится."""
    an = a.get("analytics") or {}
    ok(f"{tag}: analytics есть, calibrated = 0", an.get("available") and an.get("calibrated") == 0, an.get("reason"))
    titles = s4_titles(a)
    ok(f"{tag}: в разделе 4 все блоки аналитики", all(x in titles for x in AN_TITLES_RU),
       [x for x in AN_TITLES_RU if x not in titles])
    items = an["factors"]["items"]
    ok(f"{tag}: у каждого фактора источник и множитель", items and all(
        f["source"] in AN_SOURCES and f["source_label"] and f["multiplier"] > 0 for f in items), items[:2])
    total = an["factors"]["base_pct"] + sum(f["rate_pp"] or 0 for f in items)
    ok(f"{tag}: вклады факторов складываются в техническую ставку",
       abs(total - an["factors"]["technical_pct"]) < 2e-3, (total, an["factors"]["technical_pct"]))
    sc = an["score"]
    ok(f"{tag}: балл 0–100 = сумма вкладов составляющих", sc["available"] and 0 <= sc["score"] <= 100 and abs(
        sum(c["contribution"] or 0 for c in sc["components"] if c["applicable"]) - sc["score"]) < 0.3, sc.get("score"))
    sm = an["summary"]["sentences"]
    ok(f"{tag}: резюме 5–7 предложений и первым абзацем раздела 4", 5 <= len(sm) <= 7
       and a["sections"][3]["paragraphs"][0].startswith("Кратко: "), sm)
    ok(f"{tag}: у внешних данных — ссылка на источник", all(s.get("url", "").startswith("http")
                                                          for s in an["sources"]) and bool(an["sources"])
       or not an["market"]["available"], an.get("sources"))
    mk = an["market"]
    ok(f"{tag}: рынок — ставка, дата среза и источник НАПП (или честно «нет данных»)",
       (mk["available"] and mk["rate_pct"] and mk["rate_date"] and "napp.uz" in (mk["source"] or {}).get("url", ""))
       or (not mk["available"] and any("нет данных" in x for li in a["sections"][3]["lists"]
                                       if li["title"] == "Рынок и статистика" for x in li["items"])), mk)
    li = next(x for x in a["sections"][3]["lists"] if x["title"] == "Рынок и статистика")
    ok(f"{tag}: под рынком и статистикой — строки «Источник: …»",
       any(x.startswith("Источник: ") for x in li["items"]), li["items"][-3:])
    for it in an["stats"]["indicators"]:
        if it["status"] == "ok":
            ok(f"{tag}: показатель «{it['name']}» — с источником", it["sources"] and all(
                (s.get("url") or "").startswith("http") for s in it["sources"]), it["sources"])
            break
    return an


def check_analytics():
    print("40. Аналитика раздела 4: риски, факторы, чувствительность, состав тарифа, сценарии, балл, рынок, франшиза")
    from app import act_analytics as aa, act_extras as ax, market_picture as mp, risk_analytics as ra
    from app.engine import calculate, PURPOSE_ANALYSIS
    fresh()
    model_on(False)

    # --- 40а. автокран, класс 3 ---
    t0 = monotonic()
    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                       "optional": dict(CRANE_OPT, object_kind="truck_crane", year=2026)})
    sec = monotonic() - t0
    ok("40а автокран: акт сформирован быстрее 2 с", st == 200 and sec < 2, (st, sec))
    an = an_common("40а автокран", a, CRANE_MUST["sum_insured"], 365)
    rk = an["risks"]
    ok("40а: класс 3 не разбит на риски — одна строка 100 % и честная пометка",
       rk["whole_class"] and len(rk["items"]) == 1 and rk["items"][0]["share_of_net_pct"] == 100.0
       and any("не разбит на отдельные риски" in n for n in rk["notes"]), rk)
    fx = {f["code"]: f for f in an["factors"]["items"]}
    ok("40а: источники факторов — тип по виду объекта, убытки введены, год введён",
       fx["veh_type"]["source"] == "kind" and fx["loss_history"]["source"] == "input"
       and fx["veh_age"]["source"] == "input" and fx["antitheft"]["source"] == "not_set", fx)
    with db.tx() as con:
        ref = db.load_reference(con)
        inp = Input(product_code="0318", class_code="3", object_type=CRANE_TYPE,
                    value_amount=CRANE_MUST["object_value"], sum_insured=CRANE_MUST["sum_insured"], term_days=365,
                    factors={"veh_type": "special", "veh_age": "a3", "loss_history": "clean"})
        calc = calculate(ref, inp, purpose=PURPOSE_ANALYSIS)
    tb = an["tariff"]
    ok("40а: состав тарифа сходится с engine.calculate (нетто, техническая, минимум)",
       tb["available"] and abs(tb["technical_pct"] - calc["rates"]["technical_pct"]) < 1e-9
       and abs(tb["net_pct"] - calc["rates"]["net_pct"]) < 1e-9 and tb["min_pct"] == calc["rates"]["min_pct"],
       (tb.get("technical_pct"), calc["rates"]))
    ok("40а: тариф акта по-прежнему — ставка политики × поправка (техническая — справка)",
       tb["act_rate_pct"] == a["rate"]["applied_pct"] and tb["policy_rate_pct"] == a["rate"]["base_pct"]
       and a["premium"]["amount"] == round(a["rate"]["applied_pct"] / 100 * CRANE_MUST["sum_insured"])
       and "считается по тарифной политике" in tb["conclusion"], tb.get("conclusion"))
    ok("40а: вывод «Тариф акта … при технической ставке расчётного модуля … и рыночной …»",
       tb["conclusion"].startswith("Тариф акта " + act.pct(a["rate"]["applied_pct"], "ru") + " при технической "
                                   "ставке расчётного модуля " + act.pct(calc["rates"]["technical_pct"], "ru")),
       tb["conclusion"])
    sens = an["sensitivity"]["items"]
    good = bool(sens)
    for s in sens:
        alt = Input(**{**inp.__dict__})
        alt.factors = {**inp.factors, s["factor"]: next(o for (f, o), v in ref.coefficients.items()
                                                         if f == s["factor"] and s["to"] ==
                                                         tx.label(tx.OPTION_LABELS, f"{f}:{o}", "ru"))}
        g1 = rate_for(ref, alt)["gross_pct"]
        good = good and abs(round(g1, 4) - s["tech_after"]) < 1e-9
    ok("40а: чувствительность посчитана движком (сверка с прямым rate_for), 3–5 вариантов",
       good and 3 <= len(sens) <= 5, sens)
    ok("40а: мера страхователя — эффект и на премию акта (не ниже минимума)",
       any(s["kind"] == "measure" and s["act_premium_after"] is not None
           and s["act_premium_after"] >= round(0.35 / 100 * CRANE_MUST["sum_insured"]) for s in sens), sens)
    wi = {w["value"]: w for w in an["scenarios"]["whatif"]}
    with db.tx() as con:
        an2 = ra_same(con, {"class_code": "3", "product_code": "0318", "object_type": CRANE_TYPE,
                            "sum_insured": CRANE_MUST["sum_insured"], "object_value": CRANE_MUST["object_value"],
                            "region": CRANE_MUST["region"], "vehicle_type": "special", "year": 2026},
                      {"losses_3y": {"count": 0}, "protection": "immo"})
    ok("40а: «что если» с иммобилайзером — EML акта = PML модуля с тем же входом",
       "immo" in wi and wi["immo"]["eml"] == round(an2["scenarios"]["PML"]["amount"])
       and wi["immo"]["eml"] < an["scenarios"]["items"][1]["amount"], (wi.get("immo"), an2["scenarios"]["PML"]))
    fr = an["franchise"]
    ok("40а: таблица франшиз 0,5/1/2/5 % (потолок класса 3 — 5 %), вывод акта прежний",
       fr["available"] and [r["pct"] for r in fr["rows"]] == [0.5, 1.0, 2.0, 5.0]
       and a["franchise"]["text"] == "Франшиза не требуется" and fr["verdict"] == "Франшиза не требуется"
       and all(r["premium"] >= round(0.35 / 100 * CRANE_MUST["sum_insured"]) for r in fr["rows"]), fr)
    AN_REPORT["автокран"] = {"техническая": tb["technical_pct"], "тариф акта": tb["act_rate_pct"],
                             "рынок": tb["market_rate_pct"], "балл": an["score"]["score"],
                             "чувствительность": [(s["factor"], s["to"], s["delta_pct"]) for s in sens]}

    # --- 40б. склад, класс 8 (4,2 млрд, сейсмозона 8, сигнализация, железобетон) ---
    st, a = call("POST", "/act/make", {"lang": "ru", "must": WH8_MUST, "optional": WH8_OPT})
    an = an_common("40б склад кл. 8", a, WH8_MUST["sum_insured"], 365)
    items = an["risks"]["items"]
    with db.tx() as con:
        an_ra = ra_same(con, {"class_code": "8", "product_code": "0807", "object_type": "Склад",
                              "sum_insured": WH8_MUST["sum_insured"], "object_value": WH8_MUST["object_value"],
                              "region": WH8_MUST["region"], "construction": "reinforced", "activity": "warehouse"},
                        {"protection": "alarm", "seismic_zone": 8, "losses_3y": {"count": 0}})
    ok("40б: доли рисков — из risk_analytics, сумма 100 %",
       abs(sum(i["share_of_net_pct"] for i in items) - 100) <= 0.3
       and {i["code"]: i["share_of_net_pct"] for i in items} == {r["code"]: r["share_of_net_pct"] for r in an_ra["risks"]},
       [(i["code"], i["share_of_net_pct"]) for i in items])
    by = {i["code"]: i for i in items}
    ok("40б: уровни по рискам с причиной — землетрясение от сейсмозоны, пожар ниже среднего",
       by["earthquake"]["reason"]["code"] == "by_factor" and by["earthquake"]["reason"].get("factor") == "seismic"
       and "Сейсмическая зона" in by["earthquake"]["why"] and by["fire"]["level"] in ("low", "moderate")
       and all(i["level"] in ("low", "moderate", "high") and i["why"] for i in items), by["earthquake"])
    sc = an["scenarios"]
    ok("40б: сценарии — формула с числами, пожар по объекту и землетрясение по площадке 8 баллов",
       sc["available"] and any("землетрясение: 4 200 000 000 сум (вся площадка, 8 баллов)" in x["formula"].replace(" ", " ")
                               for x in sc["items"]), [x["formula"] for x in sc["items"]])
    ok("40б: «что если» — спринклеры снижают EML (посчитано модулем)",
       any(w["change"] == "protection" and w["value"] == "sprinkler" and w["eml"] < sc["items"][1]["amount"]
           for w in sc["whatif"]), sc["whatif"])
    ret = an["retention"]
    ok("40б: удержание — 20 % × (средства + резервы) по Положению 1806, EML в пределах, «временно», оценка",
       ret["known"] and ret["verdict"] == "within" and ret["status"] == "temporary"
       and "20 % × (собственные средства" in ret["text"] and "Положению № 1806, п. 15" in ret["text"]
       and ret["estimate"] is True and "оценка, не факт" in (ret["estimate_note"] or ""), ret)
    s1 = {r["label"]: r for r in a["sections"][0]["rows"]}
    ok("40б: раздел 1 здания — вид, адрес, кадастр, конструкция (введена), год постройки",
       s1.get("Вид объекта", {}).get("value") == "склад" and "Адрес / место нахождения" in s1
       and s1.get("Конструкция, материал стен", {}).get("value") == "железобетон, кирпич"
       and ("Кадастровый номер" in s1 or "кадастровый номер" in s1.get("Не указано", {}).get("value", "")), s1)
    AN_REPORT["склад кл. 8"] = {"PML/EML/MFL": [x["amount"] for x in sc["items"]], "балл": an["score"]["score"],
                                "уровни": {k: v["level"] for k, v in by.items()}}

    # --- 40в. склад, класс 9 ---
    st, a = call("POST", "/act/make", {"lang": "ru", "must": WH_MUST, "optional": WH_OPT})
    an = an_common("40в склад кл. 9", a, WH_MUST["sum_insured"], 365)
    items = an["risks"]["items"]
    ok("40в: риски класса 9 (кража со взломом, град…) — сумма 100 %",
       abs(sum(i["share_of_net_pct"] for i in items) - 100) <= 0.3 and items[0]["code"] == "burglary", items[:2])
    ok("40в: сейсмозона в балл класса 9 не входит", not next(c for c in an["score"]["components"]
                                                          if c["code"] == "seismic")["applicable"])
    ok("40в: сценарии класса 9 — кража, залив по помещению",
       an["scenarios"]["available"] and all(x["parts"][0]["peril"] == "damage9" for x in an["scenarios"]["items"]))

    # --- 40г. оборудование класса 8 по запросу филиала (продукт 0832, 47 397 852 345,04, 1 888 дн.) ---
    st, b = upload([("sorov2.docx", DOCX_MIME, docx_table(BR_SAMPLE2))], {"lang": "ru"})
    t0 = monotonic()
    st, a = br_make(b["session"], 2, br_request(b))
    sec = monotonic() - t0
    ok("40г оборудование: акт быстрее 2 с", st == 200 and sec < 2, sec)
    an = an_common("40г оборудование", a, BR_S2, 1888)
    fx = {f["code"]: f for f in an["factors"]["items"]}
    ok("40г: деятельность — по описанию документа («нон махсулотлари» → пищевое производство), не «склад»",
       fx["activity"]["option"] == "food" and fx["activity"]["source"] == "text"
       and "по описанию объекта" in fx["activity"]["source_label"], fx["activity"])
    lvl = a["risk"]["level"]
    applied = round(max(0.08 * (1 + ae.DEFAULT_SETTINGS["adj_pct"][lvl] / 100), 0.08), 4)
    ok("40г: правила тарифа акта не изменились (0,08 % × поправка, премия на 1 888 дн.)",
       a["rate"]["applied_pct"] == applied and a["premium"]["amount"] == round(BR_S2 * applied / 100 * 1888 / 365),
       (a["rate"]["applied_pct"], a["premium"]["amount"]))
    tb = an["tariff"]
    ok("40г: такафул — нагрузка 25 % без прибыли компании, техническая ставка справочно",
       tb["takaful"] and abs(tb["load_share"] - 0.25) < 1e-6 and tb["technical_pct"] > tb["act_rate_pct"], tb)
    s1rows = a["sections"][0]["rows"]
    s1 = {r["label"]: r for r in s1rows}
    ok("40г: раздел 1 оборудования — наименование на языке акта, текст документа в примечании",
       s1.get("Наименование", {}).get("value") == "машины и оборудование — пищевое производство"
       and "Технологик асбоб ускуна нон" in (s1["Наименование"].get("note") or "")
       and "по словарю" in s1["Наименование"]["note"], s1.get("Наименование"))
    ok("40г: строки оборудования (производитель, модель, заводской номер, год, место установки), "
       "«данные недоступны» ≤ 3, остальное — «Не указано»",
       sum(1 for r in s1rows if r["value"] == "данные недоступны") <= 3 and "Не указано" in s1
       and "Габариты" not in s1 and "Мощность двигателя" not in s1
       and all(x in " ".join(r["label"] + " " + str(r["value"]) for r in s1rows).lower()
               for x in ("модель", "заводской", "год выпуска", "место установки", "производитель")),
       [(r["label"], r["value"]) for r in s1rows])
    ok("40г: удержание — EML выше (защита не указана) → перестрахование или решение андеррайтера",
       an["retention"]["verdict"] in ("eml_excess", "mfl_excess", "within") and an["retention"]["known"]
       and "Вывод:" in an["retention"]["text"], an["retention"])
    fr = an["franchise"]
    with db.tx() as con:
        ctx = ax.ra_context(con, cls="8", product_code="0832", otype="Машины и оборудование", group="equipment",
                            kind="equipment", S=BR_S2, V=BR_S2, region="Ташкентская область", term_days=1888,
                            year=None, o={}, recognized=[], text="нон махсулотлари ишлаб чиқариш")
        em = ax._engine_multiplier(con, ctx, 1.0)
    rr = {"applied_pct": a["rate"]["applied_pct"], "min_pct": a["rate"]["min_pct"], "term_days": 1888}
    rate1, prem1, _fl = ax.apply_multiplier(rr, em["mult"], BR_S2)
    row1 = next(r for r in fr["rows"] if r["pct"] == 1.0)
    ok("40г: франшиза 1 % = ставка акта × множитель what_if (сверка с прямым вызовом), экономия = разница",
       row1["premium"] == prem1 and row1["rate_pct"] == rate1
       and row1["saving"] == a["premium"]["amount"] - prem1, (row1, prem1))
    ms = an["measures"]
    ok("40г: мероприятия — эффект на техническую ставку и премию акта", "items" in ms and all(
        "техническая ставка" in m["text"] or "не влияет" in m["text"] for m in ms["items"]), ms)
    ok("40г: вывод про франшизу прежний — «не требуется»", a["franchise"]["text"] == "Франшиза не требуется")
    aid_eq = a["id"]
    AN_REPORT["оборудование 0832"] = {"уровень акта": lvl, "тариф акта": a["rate"]["applied_pct"],
                                      "премия": a["premium"]["amount"], "техническая": tb["technical_pct"],
                                      "рынок": tb["market_rate_pct"], "балл": an["score"]["score"],
                                      "PML/EML/MFL": [x["amount"] for x in an["scenarios"]["items"]],
                                      "удержание": an["retention"].get("limit"),
                                      "франшизы": [(r["pct"], r["premium"], r["saving"]) for r in fr["rows"]]}

    # скан запроса: перевод описания — от модели (подменена), на языке акта
    model_on(True)
    REPLY["text"] = br_model_reply(2)
    st, b3 = upload([("scan2.png", "image/png", image((250, 250, 250)))], {"lang": "ru"})
    st, a3 = br_make(b3["session"], 2, br_request(b3))
    s13 = {r["label"]: r for r in a3["sections"][0]["rows"]}
    ok("40г: скан — наименование переводом модели, исходный текст в примечании",
       "хлебобулочных" in str(s13.get("Наименование", {}).get("value")) and "перевод модели" in (
           s13["Наименование"].get("note") or ""), s13.get("Наименование"))
    model_on(False)

    # --- 40д. три языка ---
    for lang in ("uz", "en"):
        st, x = call("GET", f"/act/{aid_eq}", params={"lang": lang})
        s4 = x["sections"][3]
        texts = [li["title"] for li in s4["lists"]] + s4["paragraphs"] + \
                [c for li in s4["lists"] if li.get("table") for c in li["table"]["columns"]] + \
                [str(c) for li in s4["lists"] if li.get("table") for r in li["table"]["rows"] for c in r] + \
                [n for li in s4["lists"] for n in li.get("notes") or []] + x["analytics"]["summary"]["sentences"]
        cyr = [t_ for t_ in texts if re.search(r"[А-Яа-яЁё]", t_ or "")]
        ok(f"40д {lang}: аналитика раздела 4 без кириллицы (заголовки, таблицы, резюме)", not cyr, cyr[:4])
        nm = x["sections"][0]["rows"][2]
        ok(f"40д {lang}: наименование объекта — на языке акта, текст документа — в примечании",
           not re.search(r"[А-Яа-яЁё]", str(nm["value"])) and "Технологик" in (nm.get("note") or ""), nm)

    # --- 40е. Word и PDF ---
    st, blob, h = call("GET", f"/act/{aid_eq}.docx", raw=True)
    plain = re.sub(r"<[^>]+>", "", zipfile.ZipFile(io.BytesIO(blob)).read("word/document.xml").decode("utf-8"))
    gone = ["Разбор по рискам", "Учтённые факторы", "Что изменит ставку", "Состав тарифа", "Сценарии убытка подробно",
            "Балл риска 0–100", "Рынок и статистика", "Франшиза: варианты", "Источник: НАПП", "Кратко:"]
    need = ["Оценка риска", "Решение", "Условия", "Цена", "Опасности", "Ожидаемая частота", "Ожидаемая тяжесть"]
    ok("40е: DOCX — раздел 4 четырьмя блоками; таблицы аналитики и источники — только в JSON и на экране",
       all(x in plain for x in need) and not any(x in plain for x in gone),
       ([x for x in need if x not in plain], [x for x in gone if x in plain]))
    st, blob, h = call("GET", f"/act/{aid_eq}.pdf", raw=True)
    text = pdf_text(pymupdf.open(stream=blob, filetype="pdf"))
    ok("40е: PDF — те же четыре блока, без таблиц аналитики", all(x in text for x in need)
       and not any(x in text for x in gone), [x for x in gone if x in text])

    # --- 40ж. старый акт без аналитики показывается ---
    with db.tx() as con:
        row = db.rows(con, "SELECT act_json FROM acts WHERE id=?", aid_eq)[0]
    stored = _json.loads(row["act_json"])
    D = stored["data"]
    D.pop("analytics", None)
    D.pop("object_doc", None)
    old = act.render(D, "ru", stored["meta"])
    ok("40ж: старый акт без блока analytics показывается (available = false)",
       old["analytics"]["available"] is False and old["analytics"]["reason"] == "old_act"
       and len(old["sections"]) == 5)

    # --- 40з. словарь деятельности и вида объекта (три языка) ---
    ok("40з: деятельность по описанию — ru, uz кириллица и латиница, en; «анонс» — не хлеб",
       ax.activity_from_text("Технологик асбоб ускуна нон махсулотлари ишлаб чиқариш учун") == "food"
       and ax.activity_from_text("хлебопекарное производство") == "food"
       and ax.activity_from_text("non mahsulotlari ishlab chiqarish uskunasi") == "food"
       and ax.activity_from_text("bakery equipment") == "food"
       and ax.activity_from_text("холодильник для хранения сельхозпродукции") == "warehouse"
       and ax.activity_from_text("АЗС и склад ГСМ") == "flammable"
       and ax.activity_from_text("анонс оборудования") is None and ax.activity_from_text("") is None)
    ok("40з: склад-холодильник узнаётся по «музлатгич» и «холодильник»",
       ax.cold_store("қишлоқ хўжалиги махсулотларини сақлаш учун музлатгич")
       and ax.cold_store("холодильник") and not ax.cold_store("склад"))
    ok("40з: пороги уровня риска помечены экспертными", aa.CALIBRATED == 0 and aa.PERIL_LEVEL["low_max"] < 1)
    del mp, ra


# ------------------------------------------------------------------ 41. замечания контролёра по аналитике (30.09.2026)

ROOT_DIR = Path(__file__).resolve().parent.parent
EQ_MUST = {"product_code": "0832", "sum_insured": BR_S2, "object_value": BR_S2, "region": "tashkent_region"}
EQ_OPT = {"term_days": 1888, "object_kind": "equipment", "activity": "food",
          "object_type": "Технологическое оборудование для производства хлебобулочных изделий"}
# «движок» как жаргон (двигатель техники — «Dvigatelni bloklash», «engine immobilisation» — не жаргон)
JARGON = re.compile(r"движ(?!ени)|dvigatel(?!ni)|\bengine\b(?! hours| lock| immobil)|Балл старого|what_if", re.I)


def s4_texts(a):
    """Все строки раздела 4: абзацы, строки, списки, таблицы, примечания."""
    s4 = a["sections"][3]
    out = list(s4["paragraphs"]) + [r["label"] + " " + str(r["value"]) + " " + str(r.get("note") or "") for r in s4["rows"]]
    for li in s4["lists"]:
        out += [li["title"]] + list(li["items"]) + list(li.get("notes") or [])
        if li.get("table"):
            out += list(li["table"]["columns"]) + [str(c) for r in li["table"]["rows"] for c in r]
    return out


def s4_list(a, title):
    return next((li for li in a["sections"][3]["lists"] if li["title"] == title), {"items": []})


def check_review_fixes():
    print("41. Замечания контролёра по аналитике: регион, удержание-оценка, тип по умолчанию, доли, рынок, балл, "
          "жаргон, уровни, склонения, вид документа, франшиза от неокруглённой ставки")
    from app import act_analytics as aa, act_extras as ax, market_picture as mp
    fresh()
    model_on(False)

    # --- 41.2. регион кодом экрана и названием — одна и та же статистика и один балл ---
    st1, a1 = call("POST", "/act/make", {"lang": "ru", "must": EQ_MUST, "optional": EQ_OPT})
    st2, a2 = call("POST", "/act/make", {"lang": "ru", "must": dict(EQ_MUST, region="Ташкентская область"),
                                         "optional": EQ_OPT})
    an1, an2 = a1["analytics"], a2["analytics"]
    strip = lambda st: [{k: v for k, v in i.items() if k != "text"} for i in st["indicators"]]   # noqa: E731
    ok("41.2: tashkent_region и «Ташкентская область» — одинаковые показатели региона и балл",
       st1 == st2 == 200 and strip(an1["stats"]) == strip(an2["stats"])
       and an1["score"]["score"] == an2["score"]["score"] == 50.9, (an1["score"]["score"], an2["score"]["score"]))
    vh = next(i for i in an1["stats"]["indicators"] if i["id"] == "vulnerable_housing")
    ok("41.2: показатель региона — Ташкентская область, 49,95 % (регион, а не республика)",
       vh["scope"] == "region" and abs(vh["value"] - 49.95) < 1e-9 and "49,95" in vh["value_text"], vh)
    with db.tx() as con:
        # 02.10.2026: плюс два особых — uz_all (вся республика → total) и other (вне Узбекистана → без региона)
        regs = [c for c in act._region_names() if c not in (act.REGION_ALL, act.REGION_OTHER)]
        ok("41.2: все 14 кодов регионов экрана узнаются модулями по названию; uz_all — республика, other — нет",
           all(mp.resolve_region(act.region_for_modules({"region": c, "region_code": c}))[0] for c in regs)
           and len(regs) == 14 and len(act._region_names()) == 16
           and mp.resolve_region(act.region_for_modules({"region": "uz_all", "region_code": "uz_all"}))[0] == "total"
           and act.region_for_stats({"region": "other", "region_code": "other"}) == "")
    del con

    # --- 41.3. удержание — оценка: норма на временных цифрах, таблица линий — экспертная ---
    ret = an1["retention"]
    lines = " ".join(ret["lines"])
    ok("41.3: verdict сохранён, добавлен estimate_note", ret["verdict"] == "eml_excess" and ret["estimate"] is True
       and ret["estimate_note"] and ret["estimate_note"].startswith("Это оценка, не факт"), ret)
    ok("41.3: лимит по Положению 1806 п. 15 — «цифры временные, до данных бухгалтерии» (company_financials)",
       "по Положению № 1806, п. 15 = 20 % × (собственные средства" in nb(lines)
       and "цифры временные, до данных бухгалтерии (источник: company_financials)" in lines, ret["lines"])
    # лимит и линия — из company_financials копии (03.10.2026: временные 180/240 млрд заменяет рэнкинг snsratings,
    # 95,2 + 150,1 млрд → лимит 49,06 млрд), а не числом в тесте
    from app import capacity as _cap
    with db.tx() as con:
        _lpr = _cap.capacity(con)["limit_per_risk"]
        _line8 = next(l_["retention"] for l_ in _cap.retention_table(con, _lpr) if l_["class_code"] == "8")
    _sp = lambda x: f"{x:,.0f}".replace(",", " ")  # noqa: E731
    ok(f"41.3: «страховая сумма 47,4 млрд в лимит {_sp(_lpr)} укладывается»",
       f"Страховая сумма 47 397 852 345 сум в лимит {_sp(_lpr)} сум укладывается" in nb(lines), ret["lines"])
    ok("41.3: таблица линий класса 8 — внутреннее экспертное правило, не норма, не калибровано",
       f"Лимит по таблице линий класса 8 — {_sp(_line8)} сум: внутреннее экспертное правило "
       "(capacity.retention_table), не норма, не калибровано" in nb(lines), ret["lines"])
    phrase = ("EML выше расчётного удержания по экспертной таблице — рекомендуем рассмотреть перестрахование или "
              "решение андеррайтера (оценочно, цифры временные)")
    ok("41.3: в «Кратко» — рекомендация, а не факт", any(phrase in s for s in an1["summary"]["sentences"]),
       an1["summary"]["sentences"])
    row = next(r for r in a1["sections"][3]["rows"] if r["label"] == "Лимит собственного удержания")
    ok("41.3: строка раздела 4 — «рекомендуем рассмотреть…», без «нужно перестрахование»",
       "рекомендуем рассмотреть перестрахование или решение андеррайтера (оценочно, цифры временные)" in row["note"]
       and "до данных бухгалтерии" in row["note"] and "нужно перестрахование (оценочно)" not in row["note"], row)
    how = s4_list(a1, "Как посчитаны сценарии убытка")["items"]
    ok("41.3: «как посчитаны сценарии» — таблица линий не выдана за норму",
       any("внутреннее экспертное правило, не норма" in h and "расчётное удержание" in h for h in how), how)
    for lang in ("uz", "en"):
        st, x = call("GET", f"/act/{a1['id']}", params={"lang": lang})
        r = x["analytics"]["retention"]
        ok(f"41.3 {lang}: удержание — оценка на языке акта (estimate_note, без кириллицы)",
           r["estimate_note"] and "company_financials" in r["estimate_note"]
           and not re.search(r"[А-Яа-яЁё]", " ".join(r["lines"])), r["lines"])

    # --- 41.4. пример оборудования для снимков: запрос филиала даёт вид, деятельность и описание ---
    br_file = ROOT_DIR / "sandbox" / "br30" / "sorov_equipment.docx"
    ok("41.4: образец запроса филиала для снимков оборудования лежит в sandbox/br30", br_file.exists())
    if br_file.exists():
        st, b = upload([("sorov_equipment.docx", DOCX_MIME, br_file.read_bytes())], {"lang": "ru"})
        st, ae_ = call("POST", "/act/make", {"session": b["session"], "lang": "ru", "recognized": b["recognized"],
                                             "must": EQ_MUST, "optional": {"term_days": 1888,
                                                                           "request": br_request(b)}})
        base = next(r for r in ae_["analytics"]["tariff"]["rows"] if r["code"] == "base_net")
        ok("41.4: по запросу филиала — оборудование 0,20 % и пищевое производство (не «производственное здание»)",
           st == 200 and "машины и оборудование" in base["label"] and base["value"] == act.pct(0.2, "ru")
           and next(f for f in ae_["analytics"]["factors"]["items"] if f["code"] == "activity")["option"] == "food",
           base)

    # --- 41.5. оборудование без документа: тип по умолчанию назван, деятельность согласована с типом ---
    st, a0 = call("POST", "/act/make", {"lang": "ru", "must": EQ_MUST, "optional": {"term_days": 1888}})
    s1 = {r["label"]: r for r in a0["sections"][0]["rows"]}
    ok("41.5: раздел 1 — «принят по умолчанию: производственное здание», а не «данные недоступны»",
       s1["Вид объекта"]["value"] == "принят по умолчанию: производственное здание"
       and "уточните" in s1["Вид объекта"]["note"], s1.get("Вид объекта"))
    base = next(r for r in a0["analytics"]["tariff"]["rows"] if r["code"] == "base_net")
    ok("41.5: состав тарифа — пометка «вид объекта принят по умолчанию»",
       "вид объекта принят по умолчанию: производственное здание" in base["note"], base)
    fx = {f["code"]: f for f in a0["analytics"]["factors"]["items"]}
    asm = s4_list(a0, "Принято по умолчанию (уточните)")["items"]
    ok("41.5: деятельность по умолчанию согласована с типом (производство → пищевое производство, не склад)",
       fx["activity"]["option"] == "food" and fx["activity"]["source"] == "default"
       and any("по типу объекта «производственное здание»" in x for x in asm)
       and not any("склад общего назначения" in x for x in asm), (fx["activity"], asm))
    with db.tx() as con:
        ok("41.5: правило согласования — таблица модуля (производство → food, склад → warehouse)",
           ax._default_activity(con, "8", "0832", None) == ("Производство", "food")
           and ax._default_activity(con, "8", "0807", "Склад") == ("Склад", "warehouse"))
    ok("41.5: фактор по умолчанию помечен в «Кратко» и в разборе рисков",
       any("(×1,2, принято по умолчанию)" in s for s in a0["analytics"]["summary"]["sentences"])
       and "принято по умолчанию" in a0["analytics"]["risks"]["items"][0]["why"])

    # --- 41.6. доли рисков: округление; автокран — без «Доли из справочника» ---
    notes8 = a0["analytics"]["risks"]["notes"]
    ok("41.6: класс 8 — «сумма 99,8 % — из-за округления долей»",
       any("сумма 99,8 % — из-за округления долей" in nb(n) for n in notes8), notes8)
    st, ac = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST, "optional": CRANE_OPT})
    nc = ac["analytics"]["risks"]["notes"]
    ok("41.6: автокран — «не разбит на отдельные риски», без «Доли — из справочника рисков»",
       any("не разбит на отдельные риски" in n for n in nc) and not any("Доли — из справочника" in n for n in nc), nc)

    # --- 41.7. строка рынка: полный год отдельно, какая строка отчёта взята — по market_stats ---
    mk_items = s4_list(a1, "Рынок и статистика")["items"]
    with db.tx() as con:
        last = db.rows(con, "SELECT MAX(report_date) d FROM market_stats WHERE row_key='cls8_9'")[0]["d"]
        pk = db.rows(con, "SELECT premiums_ytd p FROM market_stats WHERE row_key='cls8_9' AND report_date=?", last)[0]["p"]
        p8 = db.rows(con, "SELECT premiums_ytd p FROM market_stats WHERE row_key='cls8' AND report_date=?", last)[0]["p"]
    fy = [x for x in mk_items if x.startswith("За ") and "год: ставка" in x]
    ok("41.7: «За 2025 год: ставка …, убыточность …» — отдельной строкой, не хвостом убыточности среза",
       len(fy) == 1 and not any("за 20" in x for x in mk_items if x.startswith("Убыточность класса")), mk_items[:4])
    mli = s4_list(a1, "Рынок и статистика")
    trows = {r[0]: r for r in mli["table"]["rows"]}
    ok("41.7: в таблице документа полный год — две строки: ставка и убыточность",
       "Рыночная ставка за 2025 год" in trows and "Убыточность рынка за 2025 год" in trows
       and ";" not in trows["Рыночная ставка за 2025 год"][1], list(trows))
    ok("41.7: пояснение о строке отчёта — под таблицей документа",
       any(n.startswith("Взята строка классов 8 и 9 (cls8_9)") for n in mli["notes"]), mli.get("notes"))
    rown = [x for x in mk_items if x.startswith("Взята строка классов 8 и 9 (cls8_9)")]
    ok("41.7: «взята строка классов 8 и 9 (cls8_9); отдельная строка класса 8 мала» — числа из market_stats",
       len(rown) == 1 and act.tx._num(round(pk), "ru") + " млн сум" in rown[0]
       and act.tx._num(round(p8), "ru") + " млн сум" in rown[0] and "мала по объёму" in rown[0]
       and "одной строкой" not in " ".join(mk_items), rown)

    # --- 41.8. доля глинобитного жилья — только для зданий и складов ---
    comp1 = next(c for c in an1["score"]["components"] if c["code"] == "external_stats")
    vh1 = next(i for i in an1["stats"]["indicators"] if i["id"] == "vulnerable_housing")
    ok("41.8: оборудование — жилой фонд не в балле, честное «нет показателей региона для этого вида объекта»",
       not comp1["applicable"] and comp1["why"] == "нет показателей региона для этого вида объекта"
       and vh1["used_in_score"] is False and vh1["excluded_for_kind"] and "в балл не входит" in vh1["text"], comp1)
    comp0 = next(c for c in a0["analytics"]["score"]["components"] if c["code"] == "external_stats")
    ok("41.8: здание (тип по умолчанию «производство») — показатель жилого фонда в балле",
       comp0["applicable"] and comp0["points"] > 0, comp0)
    ok("41.8: балл без показателя — та же формула модуля (сумма вкладов = балл)",
       abs(sum(c["contribution"] or 0 for c in an1["score"]["components"] if c["applicable"])
           - an1["score"]["score"]) < 0.3)
    st, am_ = call("POST", "/act/make", {"lang": "ru", "must": dict(EQ_MUST, region="Марс"), "optional": EQ_OPT})
    cm = next(c for c in am_["analytics"]["score"]["components"] if c["code"] == "external_stats")
    ok("41.8: регион не распознан — так и написано, а не «нет показателей для класса»",
       not cm["applicable"] and cm["why"].startswith("регион не распознан"), cm)

    # --- 41.9. без жаргона на трёх языках ---
    for tag, aid in (("оборудование", a1["id"]), ("автокран", ac["id"])):
        for lang in ("ru", "uz", "en"):
            st, x = call("GET", f"/act/{aid}", params={"lang": lang})
            bad = [s for s in s4_texts(x) + x["analytics"]["summary"]["sentences"] + x["franchise"]["how"]
                   if JARGON.search(s or "")]
            ok(f"41.9 {tag} {lang}: в разделе 4 нет «движок», «Балл старого движка», what_if", not bad, bad[:3])
    st, x = call("GET", f"/act/{a1['id']}", params={"lang": "ru"})
    ok("41.9: «Балл риска (справочно)», «расчёт при других данных объекта», «посчитано расчётным модулем»",
       x["analytics"]["score"]["text"].startswith("Балл риска (справочно) — ")
       and "Сценарии «что если» (расчёт при других данных объекта)" in s4_titles(x)
       and "Что изменит ставку (посчитано расчётным модулем)" in s4_titles(x))

    # --- 41.10. одни и те же слова уровня у рисков и у акта ---
    ok("41.10: уровни рисков и акта — низкий / умеренный / высокий на трёх языках",
       all(tx.PERIL_LEVEL_LABELS[k] == tx.LEVEL_LABELS[k] for k in ("low", "moderate", "high")))
    lv = {i["level_label"] for i in an1["risks"]["items"]}
    ok("41.10: в разборе рисков нет «средний»", lv <= {"низкий", "умеренный", "высокий"}
       and not any("иначе средний" in n or "уровень средний" in n for n in an1["risks"]["notes"])
       and not any("уровень средний" in i["text"] for i in an1["risks"]["items"]), lv)

    # --- мелочи: склонения, вид документа, франшиза от неокруглённой ставки ---
    ok("склонение: 1 балл, 2 балла, 5 баллов, 11 баллов, 21 балл, 24 балла, 112 баллов, 2,5 балла",
       [tx.count_text(n, "points", "ru", 1 if n == 2.5 else 0) for n in (1, 2, 5, 11, 21, 24, 112, 2.5)]
       == ["1 балл", "2 балла", "5 баллов", "11 баллов", "21 балл", "24 балла", "112 баллов", "2,5 балла"]
       and tx.count_text(24, "points", "uz") == "24 ball" and tx.count_text(1, "points", "en") == "1 point")
    sc_items = s4_list(a1, "Балл риска 0–100 (справочно)")["items"]
    ok("склонение в акте: «24 балла», «32 случая»",
       any("MFL к лимиту удержания: 24 балла ×" in x for x in sc_items)
       and any("32 случая" in nb(x) for x in mk_items) and not any("24 баллов" in x for x in sc_items),
       [x for x in sc_items if "MFL" in x])
    if br_file.exists():
        for lang, want in (("ru", "запрос филиала"), ("uz", "filial soʻrovi"), ("en", "branch request")):
            st, x = call("GET", f"/act/{ae_['id']}", params={"lang": lang})
            r2 = [r for r in x["sections"][1]["rows"] if want in str(r["value"])]
            ok(f"вид документа в разделе 2 на языке акта ({lang}): «{want}»", bool(r2)
               and (lang == "ru" or "запрос филиала" not in " ".join(str(r["value"]) for r in x["sections"][1]["rows"])),
               x["sections"][1]["rows"])
    f05 = next(r for r in a0["analytics"]["franchise"]["rows"] if r["pct"] == 0.5)
    rr = a0["rate"]
    raw = rr["applied_pct"] * f05["multiplier"]
    ok("франшиза 0,5 %: премия от неокруглённой ставки (216 534 374), ставка показана округлённой",
       f05["premium"] == round(raw / 100 * BR_S2 * 1888 / 365) == 216_534_374
       and f05["rate_pct"] == round(raw, 4), (f05, raw))
    ok("франшиза: apply_multiplier — премия от неокруглённой ставки, ставка до 4 знаков",
       ax.apply_multiplier({"applied_pct": 0.096, "min_pct": 0.08, "term_days": 365}, 0.9197, 1e9)
       == (0.0883, round(0.096 * 0.9197 / 100 * 1e9), False))


# ================================================================================================
#  40. Бланк договора компании (шаблон с подчёркиваниями) и тот же шаблон заполненный — 30.09.2026
#  Копия структуры шаблона «Договор №____ / Страхования спортсменов от несчастных случаев» на выдуманных
#  данных: заголовок в две строки, подчёркивания, таблица приложения 1, пункты 2.6 и 5.4. Word-разметка —
#  как у настоящего файла: слова разрезаны на прогоны w:r с rsid, закладки, проверка правописания, табуляции.
# ================================================================================================

TPL_DIRECTOR = "Т.Т. Тестов"                    # выдуманный руководитель в шапке («УТВЕРЖДАЮ»)
TPL_SIGNER = "Сидоров Сидор Сидорович"          # выдуманный представитель страхователя (заполненный договор)
TPL_ATHLETE = "Спортов Тест Тестович"           # выдуманный спортсмен в списке приложения 1
TPL_LICENSE = "00099"
TPL_REQ_INSURER = ("СТРАХОВЩИК: АО СО «INSON» Адрес: ______________________________ тел: "
                   "_______________________________ факс: ______________________________ р/с: "
                   "_______________________________ в __________________________________ МФО: "
                   "_______________________________ ИНН: _______________________________ ОКОНХ: "
                   "_____________________________")
TPL_REQ_HOLDER = ("СТРАХОВАТЕЛЬ: _______________________ Адрес: ______________________________ тел: "
                  "_______________________________ факс: ______________________________ р/с: "
                  "_______________________________ в __________________________________ МФО: "
                  "_______________________________ ИНН: _______________________________ ОКОНХ: конец реквизитов")


def tpl_blocks(filled: bool = False) -> list:
    """Блоки шаблона: ("p", текст) | ("sdt", текст) | ("tbl", строки). filled — те же места заполнены."""
    u = lambda n: "_" * n                                                      # noqa: E731
    no = "Договор № 17-НС/2026" if filled else "Договор №" + u(12)
    place_date = ("г. Ташкент\t\t\t\t\t \t\t   «1» октября 2026 г." if filled
                  else "г. " + u(15) + "\t\t\t\t\t \t\t   «____» ________ 20___г.")
    holder = ("ООО «Спорт Клуб Тест», именуемое в дальнейшем «Страхователь», в лице директора " + TPL_SIGNER
              if filled else u(48) + ", именуемая в дальнейшем «Страхователь», в лице " + u(44))
    s_ins = "600 000 000 (шестьсот миллионов)" if filled else u(28) + " (" + u(60) + ")"
    prem = "9 000 000 (девять миллионов)" if filled else u(33) + " (" + u(60) + ")"
    term = ("с «1» октября 2026 года по «30» сентября 2027 года" if filled
            else "с «_____» ___________ 20___ года по «_____» _________ 20____ года")
    sched = [["Профессия (род занятия)", "Количество застрахованных \nлиц", "Персональная страховая \nсумма (сум)",
              "Процентная ставка (%)", "Страховой \nплатеж за одного застрахованного лица (сум)",
              "Страховая \nсумма ВСЕГО (гр2 х гр3)", "Страховая \nпремия ВСЕГО (гр2 х гр5)"],
             ["1", "2", "3", "4", "5", "6", "7"]]
    if filled:
        sched.append(["Футболист", "20", "30 000 000", "1,5", "450 000", "600 000 000", "9 000 000"])
    else:
        sched.append(["", "", "", "", "", "", ""])
    names = [["№ п/п", "Фамилия, Имя и Отчество", "Персональная страховая сумма, сум", "Выгодоприобретатель"],
             ["1", "2", "3", "4"]]
    for k in range(1, 16):
        who = TPL_ATHLETE if filled and k == 1 else ""
        names.append([f"{k}.", who, "30 000 000" if who else "", ""])
    names.append(["", "Итого:", "", ""])
    sign = [["ПОДПИСИ СТОРОН:", ""],
            ["От имени Страховщика: Генеральный директор/ Директор " + u(17) + " филиала/ Иное уполномоченное лицо "
             + u(18) + " (Ф.И.О.)", "От имени Страхователя: " + u(25) + " должность Ф.И.О."],
            [u(25) + " подпись\t\t м.п.", u(25) + " подпись\t\t м.п."]]
    filler = [("p", f"7.{k}. Стороны обязуются добросовестно исполнять условия настоящего Договора, своевременно "
                    f"информировать друг друга об изменении реквизитов, адресов и банковских счетов, соблюдать "
                    f"конфиденциальность сведений, полученных при исполнении настоящего Договора, и не передавать их "
                    f"третьим лицам без письменного согласия другой стороны, за исключением случаев, прямо "
                    f"предусмотренных законодательством Республики Узбекистан (пункт {k}).") for k in range(1, 61)]
    return [
        ("p", "«УТВЕРЖДАЮ»"), ("p", "Генеральный директор"), ("p", "АО СО «INSON»"), ("p", TPL_DIRECTOR),
        ("p", "Приложение №___"), ("p", "К приказу №___ от «___» _________ 20___г."),
        ("p", no), ("p", "Страхования спортсменов от несчастных случаев"), ("p", place_date),
        ("p", "Акционерное Общество Страховая организация «INSON», действующее на основании Лицензии на осуществление "
              "страховой деятельности серия ТС № " + TPL_LICENSE + " от «5» мая 2021 года, выданной уполномоченным "
              "государственным органом по регулированию страхового рынка Республики Узбекистан, именуемое в "
              "дальнейшем «Страховщик», в лице " + u(47) + ", действующего на основании " + u(21) + ", с одной "
              "стороны, и " + holder + ", действующего на основании " + u(24) + ", с другой стороны заключили "
              "настоящий Договор о нижеследующем:"),
        ("p", "1. ПРЕДМЕТ ДОГОВОРА"),
        ("p", "Страховщик обязуется в соответствии с условиями настоящего Договора выплатить при наступлении "
              "страхового случая Застрахованным лицам, указанным в Приложении 1 к настоящему Договору, обусловленную "
              "сумму, при условии, что Страхователь обязуется оплатить страховую премию в размере и сроки, указанные "
              "в настоящем Договоре."),
        ("p", "2. ОПРЕДЕЛЕНИЯ"),
        ("p", "2.1. Страховой Полис – документ, удостоверяющий факт заключения настоящего Договора."),
        ("p", "2.2. Страховая сумма – сумма денежных средств, представляющая собой предельный объем обязательств "
              "Страховщика."),
        ("p", "2.3. Страховая премия – плата за страхование, уплачиваемая Страхователем Страховщику."),
        ("p", "2.4.\tЗастрахованное лицо (спортсмен) – физическое лицо, профессионально занимающееся спортом, чьи "
              "имущественные интересы, связанные с жизнью и здоровьем, являются объектом страхования."),
        ("p", "2.5. Выгодоприобретатель – физическое лицо, названное в Приложении 1 к настоящему Договору, в качестве "
              "получателя страховой выплаты."),
        ("p", "2.6.\tСтраховой случай – получение травматического повреждения или смерть Застрахованного лица в "
              "результате несчастного случая, произошедшего во время спортивного соревнования в течение периода "
              "страхования, с наступлением которого возникает обязанность Страховщика произвести страховую выплату."),
        ("p", "2.7.\tНесчастный случай – внезапное, кратковременное событие, которое извне воздействует на организм "
              "человека."),
        ("p", "3. страховое ПОКРЫТИЕ"),
        ("p", "3.1. Страховая защита предоставляется Застрахованным лицам от несчастных случаев, произошедших во время "
              "участия в спортивных соревнованиях и приведших к:"),
        ("p", "3.1.1. травматическим повреждениям Застрахованного лица;"),
        ("p", "3.1.2. смерти Застрахованного лица."),
        ("p", "4. ОБЩИЕ ИСКЛЮЧЕНИЯ"),
        ("p", "4.1. По настоящему Договору не признаются страховым случаем события, произошедшие вследствие:"),
        ("p", "а) военных действий и их последствий, народных волнений и забастовок;"),
        ("p", "б) ядерного взрыва, радиации и радиоактивного заражения;"),
        ("p", "в) умышленных действий Страхователя или Застрахованного лица, направленных на наступление страхового "
              "случая;"),
        ("p", "г) доказанного факта применения допинга;"),
        ("p", "д) нахождения Застрахованного лица в состоянии алкогольного, наркотического или токсического "
              "опьянения;"),
        ("p", "е) самоубийства или покушения на самоубийство Застрахованного лица;"),
        ("p", "ж) совершения Застрахованным лицом умышленного преступления."),
        ("p", "5. CТРАХОВАЯ СУММА И СТРАХОВАЯ ПРЕМИЯ"),
        ("p", "5.1. Общая страховая сумма по настоящему Договору составляет " + s_ins + " сум."),
        ("p", "5.2. Персональная страховая сумма, установленная для каждого Застрахованного лица, указана в "
              "Приложении 1 к настоящему Договору."),
        ("p", "5.3. Страховая премия по настоящему Договору составляет " + prem + " сум."),
        ("tbl", sched),
        ("p", "5.4. Страховая премия оплачивается единовременно в течение 5 (пяти) банковских дней после подписания "
              "настоящего Договора сторонами."),
        ("sdt", "5.5. Все взаиморасчеты по настоящему Договору производятся в сумах Республики Узбекистан."),
        ("p", "6. ВСТУПЛЕНИЕ В СИЛУ И СРОК ДЕЙСТВИЯ ДОГОВОРА"),
        ("p", "6.1. Настоящий Договор вступает в силу с момента подписания сторонами. Обязательства Страховщика по "
              "страховой выплате вступают в силу " + term + "."),
        ("p", "7. ПРАВА И ОБЯЗАННОСТИ СТОРОН"),
    ] + filler + [
        ("p", "8. РАССМОТРЕНИЕ СТРАХОВОЙ ПРЕТЕНЗИИ"),
        ("p", "8.1. При наступлении события, которое могло бы обосновать требование к Страховщику, Застрахованное лицо "
              "обязано:"),
        ("p", "– в течение 30 (тридцати) календарных дней после наступления события, направить Страховщику "
              "письменное заявление с указанием причин и обстоятельств наступившего события."),
        ("p", "9. ПОРЯДОК ОСУЩЕСТВЛЕНИЯ СТРАХОВОЙ ВЫПЛАТЫ"),
        ("p", "9.1. При временной потере трудоспособности Застрахованным лицом страховая выплата производится по "
              "таблице выплат, но не более 50% от персональной страховой суммы.\nПереносы строки внутри пункта "
              "сохраняются."),
        ("p", "9.2. При установлении Застрахованному лицу группы инвалидности страховая выплата производится в "
              "размере от 60% до 100% персональной страховой суммы."),
        ("p", "14. ЮРИДИЧЕСКИЕ АДРЕСА И РЕКВИЗИТЫ СТОРОН:"),
        ("tbl", [[TPL_REQ_INSURER, TPL_REQ_HOLDER]] + sign),
        ("p", "Приложение 1"), ("p", "к Договору страхования спортсменов"),
        ("p", "от несчастных случаев №" + u(16)), ("p", "от «____» _________ 20___г."),
        ("p", "СПИСОК"), ("p", "ЗАСТРАХОВАННЫХ СПОРТСМЕНОВ"),
        ("tbl", names),
        ("tbl", sign),
    ]


def _w_runs(text: str, k: int) -> str:
    """Текст абзаца так, как его пишет Word: куски по нескольку знаков в разных w:r с rsid, между ними —
    закладка и отметка правописания; «\\t» — w:tab, «\\n» — w:br."""
    out = []
    for i, part in enumerate(re.split(r"(\t|\n)", text)):
        if part == "\t":
            out.append("<w:r><w:tab/></w:r>")
            continue
        if part == "\n":
            out.append("<w:r><w:br/></w:r>")
            continue
        for j in range(0, len(part), 7):
            piece = part[j:j + 7]
            if j and j % 21 == 0:
                out.append(f'<w:proofErr w:type="spellStart"/><w:bookmarkStart w:id="{k}{j}" w:name="_x{k}{j}"/>'
                           f'<w:bookmarkEnd w:id="{k}{j}"/>')
            out.append(f'<w:r w:rsidR="00A1{k:04d}" w:rsidRPr="00B2{j:04d}"><w:rPr><w:rFonts w:ascii="Times New Roman"/>'
                       f'<w:sz w:val="24"/></w:rPr><w:t xml:space="preserve">{_x(piece)}</w:t></w:r>')
    return "".join(out)


def _w_p(text: str, k: int) -> str:
    # позиции табуляции в свойствах абзаца — не знак табуляции в тексте
    return (f'<w:p w:rsidR="00C3{k:04d}"><w:pPr><w:tabs><w:tab w:val="left" w:pos="720"/></w:tabs>'
            f'<w:jc w:val="both"/></w:pPr>{_w_runs(text, k)}</w:p>')


def docx_word(blocks: list) -> bytes:
    """DOCX с разметкой, как у Word: прогоны, rsid, закладки, блок w:sdt, таблицы с w:tcPr."""
    ns = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
          'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"')
    body, k = [], 0
    for kind, val in blocks:
        k += 1
        if kind == "p":
            body.append(_w_p(val, k))
        elif kind == "sdt":
            body.append(f"<w:sdt><w:sdtPr><w:alias w:val=\"поле\"/></w:sdtPr><w:sdtContent>{_w_p(val, k)}"
                        f"</w:sdtContent></w:sdt>")
        else:
            rows = "".join("<w:tr>" + "".join(
                '<w:tc><w:tcPr><w:tcW w:w="1400" w:type="dxa"/></w:tcPr>'
                + "".join(_w_p(line, k) for line in (c.split("\n") if c else [""])) + "</w:tc>" for c in r) + "</w:tr>"
                for r in val)
            body.append(f"<w:tbl><w:tblPr><w:tblW w:w=\"0\" w:type=\"auto\"/></w:tblPr>{rows}</w:tbl>")
    xml = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {ns}><w:body>{"".join(body)}'
           f'<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr></w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def check_docx_reader():
    print("40а. Чтение DOCX: текст из дерева XML (прогоны, табуляции, переносы, w:sdt), пределы 200 000 знаков / "
          "3 000 абзацев / 10 000 ячеек")
    from app import act_extras as ax, ingest
    folder = Path(tempfile.mkdtemp(prefix="act-docx-"))
    try:
        p = folder / "tpl.docx"
        p.write_bytes(docx_word(tpl_blocks()))
        got = ax.read_limited(p, {})
        text = got["text"]
        ok(f"шаблон ≈{len(text) // 1000} тыс. знаков (больше 20 774) прочитан целиком, без пометки «часть»",
           len(text) > 21000 and got["truncated"] is False and got["status"] is None, (len(text), got["truncated"]))
        ok("в тексте нет разметки Word (ни «<w:», ни rsid, ни «</w:r>»)",
           "<w:" not in text and "rsid" not in text and "</w:" not in text and "w:r" not in text, text[:200])
        ok("слово, разрезанное на прогоны, склеено: «Страхования спортсменов от несчастных случаев»",
           "\nСтрахования спортсменов от несчастных случаев\n" in text)
        ok("w:tab — табуляция, w:br — перенос строки; позиции табуляции абзаца в текст не попали",
           "г. " + "_" * 15 + "\t\t\t\t\t \t\t   «____»" in text and "50% от персональной страховой суммы.\nПереносы" in text
           and "\n\t" not in text and not text.startswith("\t"))
        ok("абзац в блоке w:sdt прочитан", "5.5. Все взаиморасчеты по настоящему Договору производятся в сумах" in text)
        rq = next((t for t in got["tables"] if t["rows"] and t["rows"][0][0].startswith("СТРАХОВЩИК")), None)
        ok("строка реквизитов длиннее 500 знаков — целиком (правая ячейка не обрезана)",
           rq and rq["rows"][0][1].endswith("ОКОНХ: конец реквизитов") and len(" | ".join(rq["rows"][0])) > 500,
           rq and len(" | ".join(rq["rows"][0])))
        ok("таблица приложения 1 и список застрахованных прочитаны целиком (4 таблицы)",
           len(got["tables"]) == 4 and got["tables"][0]["rows"][0][0] == "Профессия (род занятия)",
           [t["rows"][0][:2] for t in got["tables"]])
        r_old = ingest.read_file(p)
        sdt = "5.5. Все взаиморасчеты по настоящему Договору производятся в сумах Республики Узбекистан."
        ok("потоковое чтение = прежнее чтение без пределов, плюс абзац w:sdt (прежнее его теряло)",
           r_old["text"].split() == text.replace(sdt + "\n", "").split() and sdt not in r_old["text"])
        # пределы: 2 000 абзацев и ≈190 000 знаков — целиком; 3 001 абзац — «часть»; строка таблицы > 4 000 знаков
        big = folder / "big.docx"
        big.write_bytes(docx_bytes([_x(f"{k}. " + "Страховщик обязан рассмотреть заявление в срок. " * 2)[:94]
                                    for k in range(2000)]))
        g = ax.read_limited(big, {})
        ok(f"2 000 абзацев, {len(g['text']) // 1000} тыс. знаков — прочитано целиком",
           g["truncated"] is False and len(g["text"].splitlines()) == 2000 and len(g["text"]) > 180000,
           (g["truncated"], len(g["text"])))
        many = folder / "many.docx"
        many.write_bytes(docx_bytes([f"пункт {k}" for k in range(3001)]))
        g = ax.read_limited(many, {})
        ok("3 001 абзац — предел абзацев (3 000): честная пометка «часть»",
           g["truncated"] is True and len(g["text"].splitlines()) == 3000, len(g["text"].splitlines()))
        wide = folder / "wide.docx"
        wide.write_bytes(docx_mixed(["Договор"], [[["а" * 2500, "б" * 2500]]]))
        g = ax.read_limited(wide, {})
        ok("строка таблицы длиннее 4 000 знаков — обрезана с пометкой",
           g["truncated"] is True and len(" | ".join(g["tables"][0]["rows"][0])) <= 4000)
        cells = folder / "cells.docx"
        cells.write_bytes(docx_mixed(["Договор"], [[[f"{r}-{c}" for c in range(10)] for r in range(150)]]))
        g = ax.read_limited(cells, {})
        ok("1 500 ячеек (меньше 10 000) — целиком", g["truncated"] is False
           and len(g["tables"][0]["rows"]) == 150, (g["truncated"], len(g["tables"][0]["rows"])))
        ok("предел ячеек из настроек по-прежнему работает (100)",
           ax.read_limited(cells, {"doc_max_cells": 100})["truncated"] is True)
        ok("пределы по умолчанию: 10 000 ячеек, 3 000 абзацев, 200 000 знаков, 4 000 знаков в строке DOCX, 5 с / 8 с",
           ax.DOC_LIMITS["doc_max_cells"] == 10000 and ax.DOC_LIMITS["doc_max_paras"] == 3000
           and ax.DOC_LIMITS["doc_max_text_chars"] == 200000 and ax.DOC_LIMITS["doc_max_row_chars"] == 4000
           and ax.DOC_LIMITS["doc_parse_sec"] == 5 and ax.DOC_LIMITS["doc_file_sec_pdf"] == 8
           and not ae.check_settings({"limits": dict(ae.DEFAULT_SETTINGS["limits"])})
           and ae.check_settings({"limits": dict(ae.DEFAULT_SETTINGS["limits"], doc_max_paras=1)}))
        # защита от «zip-бомбы» — прежняя: огромный распакованный объём отклоняется до разбора
        st, b = upload([("bomb.docx", DOCX_MIME, docx_bomb(60))], {"lang": "ru"})
        ok("zip-бомба в DOCX по-прежнему отклоняется до разбора", st == 422 and b["rejected"]
           and "слишком большой" in b["rejected"][0]["error"], (st, b.get("rejected")))
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def check_contract_template():
    print("40б. Бланк договора компании: заголовок в две строки, пустые поля, личное страхование (класс 1)")
    from app import contract_read as cr
    fresh()
    model_on(True)
    CALLS.clear()
    REPLY["text"] = ct_ai_reply
    blob = docx_word(tpl_blocks())
    st, b = upload([("0102_contract.docx", DOCX_MIME, blob)], {"lang": "ru"})
    c = ctb(b)
    f = c.get("fields") or {}
    dump = _json.dumps(b, ensure_ascii=False)
    ok("бланк узнан как договор, прочитан целиком (без «только часть»)",
       st == 200 and c.get("detected") and not c.get("truncated")
       and not any("только часть" in n for n in b["notes"] + c.get("notes", [])), (st, b.get("notes")))
    ok("заголовок в две строки: вид — договор, название продукта — «Страхование спортсменов от несчастных случаев»",
       f.get("product_name") == "Страхование спортсменов от несчастных случаев"
       and cr.title_kind("Договор №____\nСтрахования спортсменов от несчастных случаев\nг. ____") == "contract",
       f.get("product_name"))
    ok("код продукта из имени файла не взят (0102 нет ни в полях, ни в подсказке)",
       f.get("product_code") is None and "product_code" not in (b.get("prefill") or {}), f.get("product_code"))
    ok("«Приложение № к приказу» — пометка шаблона компании", f.get("template_hint") is True
       and c.get("template_hint") is True and any("приложения к приказу" in n for n in c["notes"]), c.get("notes"))
    ok("подсказка класса: несчастные случаи → класс 1, объект — люди",
       f["class_hint"] == "accident" and b["class_hint"] == "accident" and b["suggest_classes"] == ["1"]
       and f["object_kind"] == "people", (f["class_hint"], b.get("suggest_classes"), f["object_kind"]))
    ok("бланк: is_template и незаполненные поля (номер, дата, место, страхователь, сумма, премия, срок)",
       c.get("is_template") is True and f["is_template"] is True
       and {"contract_no", "contract_date", "place", "policyholder", "sum_insured", "premium", "term"}
       <= set(f["blank"]) and [x["code"] for x in c["blank"]][:3] == ["contract_no", "contract_date", "place"]
       and c["blank_label"] == "не заполнено", (f.get("blank"), c.get("is_template")))
    ok("пустые поля — не значения: номер не «00099» из лицензии, дата не дата лицензии, место не «г. ____»",
       f["contract_no"] is None and f["contract_date"] is None and f["place"] is None
       and TPL_LICENSE not in dump and "2021-05-05" not in dump, (f["contract_no"], f["contract_date"], f["place"]))
    ok("страхователь не заполнен — не «физическое лицо»; страховщик — юрлицо «INSON»",
       f["policyholder"] == {"kind": None, "name": None}
       and f["insurer"] == {"kind": "legal", "name": "Акционерное Общество Страховая организация «INSON»"}
       and f["beneficiary"]["kind"] is None and not any("физическое лицо" in n for n in c["notes"]),
       (f["policyholder"], f["insurer"], f["beneficiary"]))
    ok("честный текст: «Это бланк договора: поля … не заполнены. Существенные условия проверяются по заполненному»",
       c["notes"][0].startswith("Это бланк договора: поля номер договора, дата договора")
       and "не заполнены. Существенные условия проверяются по заполненному договору" in c["notes"][0], c["notes"])
    ok("ст. 929 и сверка не выполняются: essentials пуст, request = null, missing — незаполненные поля",
       c["essentials"] == [] and c["request"] is None and not any("ст. 929" in n for n in c["notes"])
       and [x["code"] for x in c["missing"]] == ["contract_no", "contract_date", "policyholder", "sum_insured",
                                                   "tariff_pct", "premium", "term"], (c["essentials"], c["missing"]))
    ok("объект: жизнь и здоровье застрахованных лиц — спортсменов",
       f["object_description"] == "жизнь и здоровье застрахованных лиц — спортсменов", f["object_description"])
    ok("страховой случай (п. 2.6): травма или смерть от несчастного случая во время соревнования",
       f["insured_event"].startswith("получение травматического повреждения или смерть Застрахованного лица")
       and "с наступлением которого" not in f["insured_event"]
       and f["cover_period"] == "во время спортивного соревнования", (f["insured_event"], f["cover_period"]))
    ok("порядок оплаты (п. 5.4): единовременно в течение 5 банковских дней после подписания",
       f["payment_mode"] == "single" and f["payment_text"] == "единовременно в течение 5 (пяти) банковских дней "
       "после подписания настоящего Договора сторонами" and c["payment_mode_label"] == "единовременно",
       f.get("payment_text"))
    ok("срок уведомления: 30 календарных дней (письменное заявление страховщику)",
       f["notice"] == "30 (тридцати) календарных дней", f["notice"])
    ok("риски личного страхования: травма и смерть — подписями",
       [x["code"] for x in f["covered_risks"]] == ["injury", "death"]
       and [x["label"] for x in c["covered_risks"]] == ["травма", "смерть"], f["covered_risks"])
    ok("исключения: военные действия, ядерные, умысел, допинг, опьянение, самоубийство, преступление",
       {"war", "riots", "nuclear", "intent", "doping", "intoxication", "suicide", "crime"}
       == {x["code"] for x in f["exclusions"]} and "применение допинга" in [x["label"] for x in c["exclusions"]],
       f["exclusions"])
    sch = f.get("schedule") or {}
    ok("приложение 1: структура таблицы (7 колонок) распознана, строк нет — пусто",
       sch.get("columns") == ["profession", "count", "personal_sum", "rate", "premium_one", "sum_total",
                              "premium_total"] and sch["items"] == [] and sch["blank"] is True
       and c["schedule"]["columns"][0]["label"] == "Профессия (род занятий)" and f["persons_listed"] == 0, sch)
    ok("франшизы в шаблоне нет — null (не выдумана)", f["franchise"] is None)
    ok("бланк: модель не вызывалась (дочитывать нечего)", not CALLS, len(CALLS))
    with db.tx() as con:
        saved = db.rows(con, "SELECT result_json FROM act_uploads WHERE id=?", b["session"])[0]["result_json"]
        journal = _json.dumps(db.rows(con, "SELECT detail FROM audit WHERE entity=?", "act_upload:" + b["session"]),
                              ensure_ascii=False)
    ok("ФИО руководителя из шапки нигде: ни в ответе, ни в базе, ни в журнале",
       "Тестов" not in dump + saved + journal, [x for x in ("Тестов",) if x in dump + saved + journal])
    # бланк + запрос филиала: сверки «запрос ↔ договор» нет
    st, b2 = upload([("tpl.docx", DOCX_MIME, blob), ("sorov1.docx", DOCX_MIME, docx_table(BR_SAMPLE1))],
                    {"lang": "ru"})
    ok("бланк и запрос филиала: сверки «запрос ↔ договор» нет", st == 200 and b2.get("branch_request")
       and ctb(b2).get("is_template") and b2.get("cross_check") is None, b2.get("cross_check"))
    # акт по бланку: contract_check не строится (как для заявления)
    st, a = ct_make(b["session"], None, must={"product_code": "0102", "sum_insured": 600_000_000,
                                              "object_value": 600_000_000, "region": "Ташкентская область"})
    ok("акт по бланку: сверки договора и ст. 929 нет", st == 200 and not (a.get("contract_check") or {}).get("available")
       and not ct_items(a), (st, (a.get("contract_check") or {}) if isinstance(a, dict) else a))
    st, a2 = ct_make(b["session"], {"premium": 9_000_000, "term_days": 365},
                     must={"product_code": "0102", "sum_insured": 600_000_000, "object_value": 600_000_000,
                           "region": "Ташкентская область"})
    ok("условия, присланные к бланку, в сверку договора не идут", st == 200
       and not (a2.get("contract_check") or {}).get("available"), (a2.get("contract_check") or {}).get("available"))
    # uz и en: тот же текст бланка на языке экрана
    for lang, head in (("uz", "Bu shartnoma blankasi"), ("en", "This is a blank contract form")):
        st, b3 = upload([("tpl.docx", DOCX_MIME, blob)], {"lang": lang})
        ok(f"{lang}: пометка бланка и «не заполнено» на языке экрана",
           ctb(b3)["notes"][0].startswith(head) and ctb(b3)["blank_label"] == ("toʻldirilmagan" if lang == "uz"
                                                                                else "not filled in")
           and [x["label"] for x in ctb(b3)["covered_risks"]] == (["jarohat", "vafot etish"] if lang == "uz"
                                                                   else ["injury", "death"]), ctb(b3)["notes"][:1])
    CT_REPORT["бланк 0102 (копия структуры)"] = {"blank": f["blank"], "product_name": f["product_name"],
                                                 "found": [x["code"] for x in c["found"]]}
    return b["session"]


def check_contract_template_filled():
    print("40в. Тот же шаблон заполнен: номер, дата, страхователь-юрлицо, сумма, премия, срок — полноценный договор")
    fresh()
    model_on(True)
    CALLS.clear()
    REPLY["text"] = ct_ai_reply
    st, b = upload([("dogovor_nс.docx", DOCX_MIME, docx_word(tpl_blocks(filled=True)))], {"lang": "ru"})
    c = ctb(b)
    f = c.get("fields") or {}
    dump = _json.dumps(b, ensure_ascii=False)
    ok("заполненный шаблон — не бланк: is_template = false, пустых ключевых полей нет",
       st == 200 and c.get("detected") and c["is_template"] is False and f["is_template"] is False
       and not (set(f["blank"]) & {"contract_no", "contract_date", "policyholder", "sum_insured", "premium", "term"}),
       f.get("blank"))
    ok("номер, дата, место — из шапки (не номер и дата лицензии)",
       f["contract_no"] == "17-НС/2026" and f["contract_date"] == "2026-10-01" and f["place"] == "г. Ташкент",
       (f["contract_no"], f["contract_date"], f["place"]))
    ok("страхователь — юрлицо без представителя; страховщик — «INSON»",
       f["policyholder"] == {"kind": "legal", "name": "ООО «Спорт Клуб Тест»"}
       and f["insurer"]["kind"] == "legal", f["policyholder"])
    ok("сумма 600 млн, премия 9 млн, срок 01.10.2026–30.09.2027 = 365 дн.",
       f["sum_insured"] == 6e8 and f["premium"] == 9e6 and f["term_from"] == "2026-10-01"
       and f["term_to"] == "2027-09-30" and f["term_days"] == 365, (f["sum_insured"], f["premium"], f["term_days"]))
    sch = f.get("schedule") or {}
    ok("приложение 1: строка «Футболист · 20 · 30 млн · 1,5 % · 450 000 · 600 млн · 9 млн»",
       sch.get("blank") is False and sch["items"] == [{"profession": "Футболист", "count": 20, "personal_sum": 3e7,
                                                        "rate": 1.5, "premium_one": 450000.0, "sum_total": 6e8,
                                                        "premium_total": 9e6}], sch)
    ok("список застрахованных: одна строка — только число, без фамилии",
       f["persons_listed"] == 1 and TPL_ATHLETE.split()[0] not in dump, f.get("persons_listed"))
    ess = {e["code"]: e["present"] for e in c["essentials"]}
    ok("существенные условия (ст. 929) проверяются — все есть; request готов",
       ess == {"object": True, "insured_event": True, "sum_insured": True, "premium": True, "term": True}
       and c["request"] and c["request"]["premium"] == 9e6 and c["request"]["term_days"] == 365, ess)
    ok("ФИО руководителя и представителя нигде нет; модель не вызывалась (правила нашли главное)",
       "Тестов" not in dump and "Сидоров" not in dump and not CALLS, len(CALLS))
    st, a = ct_make(b["session"], c["request"], must={"product_code": "0102", "sum_insured": 600_000_000,
                                                      "object_value": 600_000_000, "region": "Ташкентская область"})
    it = ct_items(a)
    ok("акт: сверка договора есть (contract_check), существенные условия — все есть",
       st == 200 and (a.get("contract_check") or {}).get("available") and it.get("essentials", {}).get("verdict") == "ok"
       and a["premium"]["term_days"] == 365, (st, list(it)))
    # договор, где правила нашли мало: текст уходит в модель только замаскированным (ФИО руководителя — метка)
    CALLS.clear()
    lines = ["«УТВЕРЖДАЮ»", "Генеральный директор", "АО СО «INSON»", TPL_DIRECTOR, "Договор № 18-НС/2026",
             "Страхования спортсменов от несчастных случаев", "г. Ташкент «2» октября 2026 г.",
             "Акционерное Общество Страховая организация «INSON», именуемое в дальнейшем «Страховщик», и ООО «Спорт "
             "Клуб Тест», именуемое в дальнейшем «Страхователь», заключили настоящий Договор.",
             "Страховая сумма и премия определяются по приложению к договору.",
             "2.6. Страховой случай – травма застрахованного лица во время соревнования."]
    st, b2 = upload([("d18.docx", DOCX_MIME, docx_word([("p", x) for x in lines]))], {"lang": "ru"})
    sent = " ".join(m["content"] for x in CALLS for m in x["messages"])
    ok("правила нашли мало — модель дочитывает; ФИО руководителя в модель не ушло (метка [ФИО])",
       len(CALLS) == 1 and "Тестов" not in sent and "[ФИО]" in sent and ctb(b2)["is_template"] is False,
       (len(CALLS), sent[:300]))


# ------------------------------------------------------------------ 41. шаблоны анализа по классам (30.09.2026)

TPL_REPORT = {}


def _admin_header(login: str) -> tuple:
    """Сессия администратора в копии базы: заголовок Authorization."""
    now = datetime.now().isoformat(timespec="seconds")
    token = secrets.token_urlsafe(32)
    with db.tx() as con:
        cur = con.execute("INSERT INTO users (login, full_name, role, branch, password_hash, salt, status, created_at,"
                          " approved_by, approved_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (login, "Test Admin", "админ", "тест", secrets.token_hex(32), secrets.token_hex(16),
                           "активен", now, "test", now))
        con.execute("INSERT INTO sessions (token, user_id, created_at, expires_at, ip, user_agent) VALUES (?,?,?,?,?,?)",
                    (token, cur.lastrowid, now, (datetime.now() + timedelta(hours=2)).isoformat(timespec="seconds"),
                     "127.0.0.1", "test_act"))
    return (b"authorization", f"Bearer {token}".encode())


def check_templates_ref():
    print("41а. Шаблоны всех классов: файл, таблица class_templates, структура, доли, оговорки, мероприятия, ракурсы")
    from app import class_templates as ctm
    data = ctm.load_file()
    ok("файл шаблонов: версия 1.4.2 от 06.10.2026, 20 шаблонов: 1–18, 13з, 16у (свои, без ссылок); классов жизни "
       "и блока classification.life_classes_uz нет",
       data["version"] == "1.4.2" and data["date"] == "2026-10-06" and list(data["classes"]) ==
       [str(i) for i in range(1, 19)] + ["13з", "16у"] and data["aliases"] == {}
       and "life_classes_uz" not in (data.get("classification") or {}),
       list(data["classes"]))
    with db.tx() as con:
        ctm.ensure(con)
        rows = ctm.all_current(con)
        n_db = con.execute("SELECT COUNT(DISTINCT class_code) FROM class_templates").fetchone()[0]
        errs = {r["class_code"]: ctm.validate(r["template"], r["class_code"], con) for r in rows}
        mcodes = ctm.measure_codes(con)
        perils = {r[0] for r in con.execute("SELECT DISTINCT class_code FROM perils")}
    ok("20 шаблонов загружены в таблицу class_templates (calibrated = 0)",
       len(rows) == 20 and n_db == 20 and all(r["calibrated"] == 0 for r in rows), (len(rows), n_db))
    ok("структура всех 20 шаблонов проходит проверку", not any(errs.values()), {k: v for k, v in errs.items() if v})
    clauses_all = ctm.clause_codes()
    for r in rows:
        c, tp = r["class_code"], r["template"]
        rk = tp["risks"]
        if c in ("8", "9"):
            ok(f"класс {c}: риски — ссылка на справочник perils (доли не дублируются)",
               rk["source"] == "perils" and rk["items"] == [] and c in perils, rk["source"])
        else:
            total = sum(x["share_pct"] for x in rk["items"])
            ok(f"класс {c}: экспертные доли рисков, сумма 100, calibrated = 0",
               rk["source"] == "template" and abs(total - 100) < 1e-9 and rk["calibrated"] == 0
               and all(set(x["label"]) == {"ru", "uz", "en"} for x in rk["items"]), total)
        cl = {x for codes in tp["clauses"].values() for x in codes}
        ms = {x for codes in tp["measures"].values() for x in codes}
        ok(f"класс {c}: оговорок ≥ 3 и мероприятий ≥ 3 в каждой группе, коды существуют",
           all(len(v) >= 3 for v in tp["clauses"].values()) and all(len(v) >= 3 for v in tp["measures"].values())
           and cl <= clauses_all and ms <= mcodes, (sorted(cl - clauses_all), sorted(ms - mcodes)))
        ok(f"класс {c}: ракурсы есть (каждая группа — непустой список из известных кодов), must ≤ 4",
           tp["required_views"] and all(v and set(v) <= set(ae.VIEWS) for v in tp["required_views"].values())
           and len(tp["must"]) <= 4 and tp["scenario_rule"]["code"] in ctm.SCENARIO_RULES, tp["required_views"])
    # классы 3, 8, 9: шаблон повторяет прежний выбор по группе объекта — акт не меняется
    cat = act.clause_catalog()
    mcat = __import__("app.act_extras", fromlist=["x"]).measures_catalog()
    same = True
    for c, groups in (("3", ("vehicle", "special")), ("8", ("property", "equipment")), ("9", ("property", "equipment"))):
        tp = next(r["template"] for r in rows if r["class_code"] == c)
        for g in groups:
            same = same and tp["clauses"][g] == [x["code"] for x in cat["groups"][g]]
            same = same and tp["required_views"][g] == ae.REQUIRED_VIEWS[g]
            by_group = [x["code"] for x in mcat["catalog"] if g in (x.get("groups") or [])]
            listed = [x for x in tp["measures"][g] if x in {m["code"] for m in mcat["catalog"]}]
            same = same and sorted(listed) == sorted(by_group)
    ok("классы 3, 8, 9: оговорки, ракурсы и мероприятия шаблона = прежний выбор по группе объекта", same)
    ok("класс 3: граница транспорта и спецтехники — по регистрационному документу (особое правило)",
       any(n["code"] == "vehicle_boundary" and "регистрационному документу" in n["text"]["ru"]
           for n in data["classes"]["3"]["notes"]))
    ok("класс 14: только необеспеченная часть и не более 50 %, страхователь — банк",
       any("не более 50 %" in n["text"]["ru"] and "банк" in n["text"]["ru"] for n in data["classes"]["14"]["notes"]))
    ok("перестрахование почти всегда — у 5, 6, 11, 12",
       [c for c, t_ in data["classes"].items() if t_["reinsurance_usually"]] == ["5", "6", "11", "12"])
    ci = ctm.credit_insurable(100_000_000, 60_000_000)
    ok("кредит 100 млн при залоге 60 млн: страхуется min(40; 50) = 40 млн (необеспеченная часть)",
       ci["insurable"] == 40_000_000 and ci["by"] == "unsecured", ci)
    ci2 = ctm.credit_insurable(100_000_000, 20_000_000)
    ok("кредит 100 млн при залоге 20 млн: min(80; 50) = 50 млн (предел 50 %)",
       ci2["insurable"] == 50_000_000 and ci2["by"] == "cap", ci2)
    TPL_REPORT["классы"] = {r["class_code"]: [(x["code"], x["share_pct"]) for x in r["template"]["risks"]["items"]]
                            or "perils" for r in rows}


def check_templates_api():
    print("41б. API шаблонов: список, класс на трёх языках, история; PUT — проверка структуры и новая версия")
    fresh()
    st, lst = call("GET", "/act/templates", params={"lang": "ru"})
    ok("GET /act/templates — 20 шаблонов кратко (18 классов + 13з, 16у), версия файла 1.4.2, признак variant",
       st == 200 and len(lst["templates"]) == 20 and lst["file_version"] == "1.4.2"
       and [x["class_code"] for x in lst["templates"]] == [str(i) for i in range(1, 19)] + ["13з", "16у"]
       and lst["counts"] == {"всего": 20, "классов": 18, "вариантов": 2}
       and [x["class_code"] for x in lst["templates"] if x["variant"]] == ["13з", "16у"]
       and "life_classes_uz" not in (lst.get("classification") or {}),
       (st, str(lst)[:300]))
    one = {x["class_code"]: x for x in lst["templates"]}
    ok("кратко: класс 3 — ДТП 45 %, угон 20 %; классы 8/9 — источник perils",
       one["3"]["risks"][:2] == [{"code": "mv_accident", "label": "ДТП", "share_pct": 45, "catastrophic": False},
                                 {"code": "mv_theft", "label": "Угон", "share_pct": 20, "catastrophic": False}]
       and one["8"]["risks_source"] == "perils", one["3"]["risks"][:2])
    names = {}
    for lg in ("ru", "uz", "en"):
        st, t1 = call("GET", "/act/templates/1", params={"lang": lg})
        names[lg] = (st, t1.get("template", {}).get("name"), t1["template"]["risks"]["items"][0]["label"]
                     if st == 200 else None, t1["template"]["must"][0]["label"] if st == 200 else None)
    ok("GET /act/templates/1 на трёх языках: название, риск, поле",
       names["ru"][1:] == ("Несчастные случаи", "Смерть", "Число застрахованных")
       and names["uz"][1:] == ("Baxtsiz hodisalar", "Oʻlim", "Sugʻurtalanganlar soni")
       and names["en"][1:] == ("Accident", "Death", "Number of insured persons"), names)
    st, t8 = call("GET", "/act/templates/8", params={"lang": "ru"})
    ok("класс 8: риски из perils базы (пожар 40 %, землетрясение 20 %) и документы из checklists",
       st == 200 and {p["code"]: p["share_pct"] for p in t8["perils"]}.get("fire") == 40.0
       and {p["code"]: p["share_pct"] for p in t8["perils"]}.get("earthquake") == 20.0
       and any(d["doc"] == "Сведения о пожарной сигнализации и охране" for d in t8["checklists"]), str(t8)[:300])
    st, t13z = call("GET", "/act/templates/13з", params={"lang": "ru"})
    ok("13з — свой шаблон (не ссылка на 14): вариант класса 13",
       st == 200 and t13z["class_code"] == "13з" and t13z["alias_of"] is None and t13z["variant"] is True
       and t13z["variant_of"] == "13", (st, t13z.get("class_code"), t13z.get("alias_of")))
    st, _x = call("GET", "/act/templates/99")
    ok("неизвестный класс — 404", st == 404, st)
    st, raw = call("GET", "/act/templates/1", params={"raw": 1})
    good = _json.loads(_json.dumps(raw["raw"]))
    # гость не правит
    st, _x = call("PUT", "/act/templates/1", {"template": good})
    ok("гость шаблон не меняет (401/403)", st in (401, 403), st)
    HEADERS.append(_admin_header("tpl_test_admin"))
    try:
        bad = _json.loads(_json.dumps(good))
        bad["risks"]["items"][0]["share_pct"] = 15            # сумма 90
        bad["clauses"]["default"].append("no_such_clause")
        bad["measures"]["default"].append("no_such_measure")
        bad["must"] = bad["must"] + [dict(bad["must"][0], code="x1"), dict(bad["must"][0], code="x2")]
        st, e = call("PUT", "/act/templates/1", {"template": bad})
        errs = " | ".join(e.get("errors") or [])
        ok("PUT с плохой структурой — 422: доли, оговорка, мероприятие, must > 4",
           st == 422 and "сумма долей 90" in errs and "no_such_clause" in errs and "no_such_measure" in errs
           and "не больше 4" in errs, (st, errs))
        st, e = call("PUT", "/act/templates/1", {"template": {"name": {"ru": "x"}}})
        ok("PUT без обязательных полей шаблона — 422", st == 422 and any("нет поля" in x for x in e["errors"]), e)
        new = _json.loads(_json.dumps(good))
        new["risks"]["items"][0]["share_pct"] = 20.2          # 20,2 + 30 + 30 + 15 = 95,2 → поправим травму
        new["risks"]["items"][3]["share_pct"] = 19.6          # сумма 99,8 — в пределах ± 0,5
        st, r = call("PUT", "/act/templates/1", {"template": new, "note": "тест: доли НС"})
        ok("PUT с хорошей структурой — новая версия 1.5 (правка администратора к файлу 1.4.2)",
           st == 200 and r["version"] == "1.5" and r["source"] == "admin"
           and r["template"]["risks"]["items"][0]["share_pct"] == 20.2, (st, str(r)[:300]))
        st, h = call("GET", "/act/templates/1/history")
        ok("история: версия файла 1.4.2 и правка 1.5 — обе сохранены",
           st == 200 and [(x["version"], x["source"]) for x in h["history"]] == [("1.4.2", "file"), ("1.5", "admin")],
           h)
        # файл той же версии правку не затирает
        from app import class_templates as ctm
        ctm.reset_cache()
        with db.tx() as con:
            ctm.ensure(con)
            cur = ctm.current(con, "1")
        ok("ensure с файлом 1.4.2 не затирает правку 1.5", cur["version"] == "1.5" and cur["source"] == "admin",
           cur["version"])
        st, a = call("POST", "/act/make", {"lang": "ru", "must": {"class_code": "1", "sum_insured": 1_000_000_000,
                                                                "object_value": 1_000_000_000, "region": "Ташкент"}})
        shares = {i["code"]: i["share_of_net_pct"] for i in a["analytics"]["risks"]["items"]}
        ok("акт класса 1 берёт действующую версию шаблона (смерть 20,2 %)",
           st == 200 and shares.get("pa_death") == 20.2 and a["template"]["version"] == "1.5", shares)
        # вернуть доли файла: ещё одна версия (история не удаляется)
        st, r = call("PUT", "/act/templates/1", {"template": good})
        ok("возврат долей — версия 1.6, история из трёх строк", st == 200 and r["version"] == "1.6", r.get("version"))
    finally:
        HEADERS.clear()


def _cls_act(cls, fields=None, S=1_000_000_000, V=None, lang="ru", optional=None):
    o = dict(optional or {})
    if fields is not None:
        o["class_fields"] = fields
    return call("POST", "/act/make", {"lang": lang, "must": {"class_code": cls, "sum_insured": S,
                                                             "object_value": V or S, "region": "Ташкент"},
                                      "optional": o})


def check_templates_act():
    print("41в. Акт по классу без perils (1, 7, 13, 14): риски и сценарии из шаблона; классы 3/8/9 — цифры прежние")
    fresh()
    model_on(False)
    # класс 1: 50 человек × 20 млн, в одном месте 10
    st, a = _cls_act("1", {"insured_count": 50, "occupation": "строители", "sum_per_person": 20_000_000,
                           "people_in_one_place": 10})
    rk = a["analytics"]["risks"]
    sc = a["scenarios"]
    ok("класс 1: риски из шаблона (смерть, инвалидность, ВУТ, травма), сумма 100, пометка «экспертные доли шаблона»",
       st == 200 and rk["source"] == "template" and [i["code"] for i in rk["items"]] ==
       ["pa_death", "pa_disability", "pa_temp_disability", "pa_injury"] and rk["total_pct"] == 100.0
       and rk["label"] == "экспертные доли шаблона" and any("шаблона класса" in n for n in rk["notes"])
       and not rk["whole_class"], rk.get("notes"))
    ok("класс 1: PML = EML = 20 млн (сумма на человека), MFL = 20 млн × 10 = 200 млн",
       sc["available"] and sc["source"] == "template" and
       [sc[k]["amount"] for k in ("pml", "eml", "mfl")] == [20_000_000, 20_000_000, 200_000_000]
       and "катастрофа" in sc["mfl"]["what"], [sc[k]["amount"] for k in ("pml", "eml", "mfl")])
    ok("класс 1: «как посчитано» — простое правило шаблона, экспертное; формула с числами",
       any("простым правилом шаблона класса 1" in h for h in sc["how"])
       and sc["mfl"]["formula"].replace(" ", " ") == "MFL = 20 000 000 сум × 10 чел. в одном месте = 200 000 000 сум",
       (sc["how"], sc["mfl"]["formula"]))
    ok("класс 1: оговорки и мероприятия — из шаблона",
       [c["code"] for c in a["clauses"]] == ["pa_list", "pa_hazard", "pa_cover_time", "ot_declared"]
       and [m["code"] for m in a["measures"]] == ["pa_briefing", "pa_ppe", "pa_medical"], [c["code"] for c in a["clauses"]])
    ok("класс 1: блок template — версия, поля класса, документы, статистика",
       a["template"]["class_code"] == "1" and a["template"]["class_fields"]["insured_count"] == 50
       and a["template"]["documents"]["items"] and "mortality_rate" in a["template"]["stats"]
       and a["template"]["required_views"] == ["document"], str(a.get("template"))[:200])
    st, u = call("GET", f"/act/{a['id']}", params={"lang": "uz"})
    ok("класс 1 по-узбекски: риск «Oʻlim», подпись сценария из шаблона",
       u["analytics"]["risks"]["items"][0]["name"] == "Oʻlim" and u["scenarios"]["mfl"]["what"].startswith("falokat"),
       (u["analytics"]["risks"]["items"][0]["name"], u["scenarios"]["mfl"]["what"]))
    st, e = call("GET", f"/act/{a['id']}", params={"lang": "en"})
    ok("класс 1 по-английски: риск «Death»", e["analytics"]["risks"]["items"][0]["name"] == "Death")
    # без полей класса — всё по страховой сумме, с пометками «по умолчанию»
    st, a0 = _cls_act("1")
    ok("класс 1 без полей: сценарии по страховой сумме, в «принято по умолчанию» — что не указано",
       a0["scenarios"]["available"] and a0["scenarios"]["pml"]["amount"] == 1_000_000_000
       and {x["code"] for x in a0["scenarios"]["assumptions"]} >= {"as_tpl_per_person_sum", "as_tpl_place"},
       a0["scenarios"]["assumptions"])
    # класс 7: отправка 200 млн, накопление 600 млн
    st, a = _cls_act("7", {"cargo_kind": "бытовая техника", "transport_mode": "auto", "route": "Ташкент — Самарканд",
                           "limit_per_shipment": 200_000_000, "accumulation_value": 600_000_000})
    sc = a["scenarios"]
    ok("класс 7: PML = EML = одна отправка 200 млн, MFL = накопление 600 млн",
       [sc[k]["amount"] for k in ("pml", "eml", "mfl")] == [200_000_000, 200_000_000, 600_000_000], sc)
    ok("класс 7: риски шаблона (повреждение 30 %, кража 20 % …)",
       {i["code"]: i["share_of_net_pct"] for i in a["analytics"]["risks"]["items"]}.get("cg_damage") == 30.0
       and a["analytics"]["risks"]["source"] == "template")
    ok("класс 7: неверный вид транспорта в полях класса — 422",
       _cls_act("7", {"transport_mode": "teleport"})[0] == 422)
    # класс 13: лимит на случай 300 млн, годовой 1 млрд
    st, a = _cls_act("13", {"activity_kind": "trade", "turnover_or_payroll": 5_000_000_000,
                            "limit_per_case": 300_000_000, "limit_aggregate": 1_000_000_000})
    sc = a["scenarios"]
    ok("класс 13: PML = EML = лимит на случай 300 млн, MFL = годовой лимит 1 млрд",
       [sc[k]["amount"] for k in ("pml", "eml", "mfl")] == [300_000_000, 300_000_000, 1_000_000_000], sc)
    ok("класс 13: удержание сравнивается с EML (Положение 1806, п. 15)",
       sc["retention"]["compared_with"] == "eml" and (not sc["retention"]["known"] or
                                                      sc["retention"]["eml_excess"] == max(300_000_000 - sc["retention"]["limit"], 0)),
       sc["retention"])
    ok("класс 13: раздел 4 — аналитика: сценарии шаблона формулой с суммой",
       a["analytics"]["scenarios"]["available"] and a["analytics"]["scenarios"]["items"][0]["formula"].startswith(
           "один случай: лимит на случай"), a["analytics"]["scenarios"])
    # класс 14: кредит 100 млн, залог 60 млн
    st, a = _cls_act("14", {"credit_amount": 100_000_000, "collateral_value": 60_000_000, "credit_term_months": 24},
                     S=40_000_000)
    ok("класс 14: кредит 100 млн, залог 60 млн, сумма 40 млн — в пределах (проверка в «как посчитано»)",
       any("допустимая страховая сумма 40" in h.replace(" ", " ") and "в пределах" in h for h in a["scenarios"]["how"])
       and not any("Кредит: страховая сумма" in c for c in a["decision"]["checks"]), a["scenarios"]["how"])
    st, a = _cls_act("14", {"credit_amount": 100_000_000, "collateral_value": 60_000_000}, S=50_000_000)
    ok("класс 14: сумма 50 млн выше допустимой 40 млн — проверка андеррайтеру, превышение 10 млн",
       any("Кредит: страховая сумма 50" in c.replace(" ", " ") and "10 000 000" in c.replace(" ", " ")
           for c in a["decision"]["checks"]) and a["decision"]["code"] != "accept", a["decision"])
    # классы 3, 8, 9: контрольные цифры прежние (как до шаблонов)
    st, c3 = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                        "optional": dict(CRANE_OPT, object_kind="truck_crane")})
    ok("класс 3 (автокран 2,945 млрд): ставка 0,42 %, премия 12 369 000, PML/EML/MFL 1 472 500 000 / 2 945 000 000 × 2",
       c3["rate"]["applied_pct"] == 0.42 and c3["premium"]["amount"] == 12_369_000
       and [c3["scenarios"][k]["amount"] for k in ("pml", "eml", "mfl")] == [1_472_500_000, 2_945_000_000, 2_945_000_000]
       and c3["analytics"]["risks"]["whole_class"] and c3["scenarios"].get("source") is None,
       (c3["rate"]["applied_pct"], c3["premium"]["amount"], [c3["scenarios"][k]["amount"] for k in ("pml", "eml", "mfl")]))
    ok("класс 3: оговорки спецтехники прежние (5 шт.), ракурсы — 7",
       [c["code"] for c in c3["clauses"]] == ["sp_attachments_storage", "sp_territory", "sp_reinspection", "sp_operator",
                                              "sp_rated_load"] and len(c3["template"]["required_views"]) == 7)
    st, c8 = call("POST", "/act/make", {"lang": "ru", "must": WH8_MUST, "optional": WH8_OPT})
    ok("класс 8 (склад 4,2 млрд): PML/EML/MFL 2 100 000 000 / 3 360 000 000 / 4 200 000 000, риски — perils",
       [c8["scenarios"][k]["amount"] for k in ("pml", "eml", "mfl")] == [2_100_000_000, 3_360_000_000, 4_200_000_000]
       and c8["analytics"]["risks"]["source"] == "perils"
       and c8["analytics"]["risks"]["items"][0]["code"] == "fire", [c8["scenarios"][k]["amount"] for k in ("pml", "eml", "mfl")])
    st, c9 = call("POST", "/act/make", {"lang": "ru", "must": WH_MUST, "optional": WH_OPT})
    ok("класс 9: риски из perils (кража со взломом первой), правило property9",
       c9["analytics"]["risks"]["items"][0]["code"] == "burglary" and c9["scenarios"]["rule"] == "property9")
    TPL_REPORT["класс 3"] = (c3["rate"]["applied_pct"], c3["premium"]["amount"],
                             [c3["scenarios"][k]["amount"] for k in ("pml", "eml", "mfl")])
    TPL_REPORT["класс 8"] = [c8["scenarios"][k]["amount"] for k in ("pml", "eml", "mfl")]
    TPL_REPORT["класс 9"] = [c9["scenarios"][k]["amount"] for k in ("pml", "eml", "mfl")]


CROP = {"crop": "wheat", "area_ha": 100, "avg_yield_5y": 30, "unit_price": 400_000}   # 100 га × 30 ц/га × 400 000


def check_templates_new_classes():
    print("41д. Шаблоны 13з и 16у — свои; строки L* (классы жизни) в базе игнорируются")
    from app import class_templates as ctm
    from app import act_extras as axm
    fresh()
    model_on(False)
    data = ctm.load_file()
    C = data["classes"]
    ok("все 20 шаблонов: в notes — «оценка разработчика, не утверждено страховщиком»; у 8 и 9 — в описании perils",
       len(C) == 20 and all(any(n["code"] == "expert_estimate" and "оценка разработчика, не утверждено страховщиком"
                                in n["text"]["ru"] for n in t_["notes"]) for t_ in C.values())
       and all("доли из таблицы perils — оценка разработчика, не утверждено страховщиком" in
               C[c]["risks"]["note"]["ru"].lower() for c in ("8", "9")))
    # ---------- 16у: чистые функции ----------
    cv = axm.crop_value(100, 30, 400_000)
    ok("стоимость урожая: 100 га × 30 ц/га × 400 000 сум/ц = 1 200 000 000 сум (3 000 ц)",
       cv["value"] == 1_200_000_000 and cv["harvest_c"] == 3000 and axm.crop_value(100, None, 1) is None, cv)
    r = axm.simple_scenarios("crop", 1_200_000_000, 1_200_000_000, CROP, C["16у"]["scenario_rule"]["params"])
    # руками: B = 1,2 млрд; PML = 1,2 млрд × 0,3 × 0,5 = 180 млн; EML = 1,2 млрд × 0,6 × 1,0 = 720 млн; MFL = 1,2 млрд
    ok("сценарии урожая руками: PML = 1,2 млрд × 0,3 × 0,5 = 180 млн; EML = × 0,6 × 1,0 = 720 млн; MFL = 1,2 млрд",
       [r[k]["amount"] for k in ("PML", "EML", "MFL")] == [180_000_000, 720_000_000, 1_200_000_000]
       and r["checks"][0]["code"] == "tpl_crop_ok", r)
    r2 = axm.simple_scenarios("crop", 900_000_000, 900_000_000, CROP, C["16у"]["scenario_rule"]["params"])
    ok("сумма 900 млн ниже стоимости 1,2 млрд (75 %): база 900 млн — PML 135 млн, EML 540 млн, MFL 900 млн, ст. 936",
       [r2[k]["amount"] for k in ("PML", "EML", "MFL")] == [135_000_000, 540_000_000, 900_000_000]
       and r2["checks"][0]["code"] == "tpl_crop_under" and r2["checks"][0]["params"]["pct"] == 75.0, r2)
    r3 = axm.simple_scenarios("crop", 500_000_000, 500_000_000, {}, {})
    ok("16у без площади и урожайности: стоимость урожая не считается — база = страховая сумма, пометка",
       r3["MFL"]["amount"] == 500_000_000 and r3["PML"]["amount"] == 75_000_000
       and [a_["code"] for a_ in r3["assumptions"]] == ["as_tpl_crop_value", "as_tpl_crop_shares"], r3)
    # ---------- 16у в акте ----------
    st, a = _cls_act("16у", CROP, S=1_200_000_000)
    sc = a["scenarios"]
    ok("акт 16у берёт шаблон 16у (не 16): риски засуха 30 % (катастрофа), град 15 %…; правило crop",
       st == 200 and a["template"]["class_code"] == "16у" and a["template"]["alias_of"] is None
       and a["template"]["variant"] is True and a["template"]["scenario_rule"]["code"] == "crop"
       and {i["code"]: i["share_of_net_pct"] for i in a["analytics"]["risks"]["items"]}.get("cp_drought") == 30.0,
       (st, a.get("template", {}).get("class_code")))
    ok("акт 16у: PML/EML/MFL = 180 млн / 720 млн / 1,2 млрд, в «как посчитано» — 100 га × 30 ц/га × 400 000 = 1,2 млрд",
       [sc[k]["amount"] for k in ("pml", "eml", "mfl")] == [180_000_000, 720_000_000, 1_200_000_000]
       and any("100 га × 30 ц/га × 400 000 сум за центнер = 1 200 000 000 сум" in flat(h) for h in sc["how"]),
       ([sc[k]["amount"] for k in ("pml", "eml", "mfl")], sc.get("how")))
    ok("акт 16у: у страховщика нет продуктов — ставка не определена, пометка в шаблоне и в «как посчитана ставка»",
       a["rate"]["applied_pct"] is None and a["premium"]["amount"] is None and a["template"]["products_count"] == 0
       and "нет продуктов этого класса" in a["template"]["no_products_note"]
       and any("нет продуктов класса 16у" in h for h in a["rate"]["how"]), a["rate"].get("how"))
    ok("акт 16у: оговорки и мероприятия — из шаблона (урожай), документы — сведения о посевах",
       [c["code"] for c in a["clauses"]] == ["cp_yield_basis", "cp_area_declared", "cp_agrotech", "cp_notice_expert"]
       and {m["code"] for m in a["measures"]} == {"cp_irrigation", "cp_plant_protection", "cp_frost_hail",
                                                 "cp_field_monitoring"}
       and "посевах" in a["template"]["documents"]["items"][0], [c["code"] for c in a["clauses"]])
    st, a = _cls_act("16у", CROP, S=1_500_000_000)
    chk = [flat(c) for c in a["decision"]["checks"]]
    ok("16у: сумма 1,5 млрд выше стоимости урожая 1,2 млрд — проверка ГК ст. 938 «уменьшить до 1 200 000 000 сум»",
       st == 200 and any(c.startswith("Урожай: страховая сумма 1 500 000 000 сум выше") and "уменьшить до "
                         "1 200 000 000 сум" in c and "ст. 938" in c for c in chk)
       and a["decision"]["code"] != "accept" and a["scenarios"]["mfl"]["amount"] == 1_200_000_000, chk)
    st, u = call("GET", f"/act/{a['id']}", params={"lang": "uz"})
    ok("16у по-узбекски: риск «Qurgʻoqchilik», проверка урожая на узбекском",
       u["analytics"]["risks"]["items"][0]["name"] == "Qurgʻoqchilik"
       and any("Hosil: sugʻurta summasi" in c for c in u["decision"]["checks"]), u["analytics"]["risks"]["items"][0])
    # ---------- 13з ----------
    st, a = _cls_act("13з", {"credit_amount": 100_000_000, "collateral_value": 60_000_000, "credit_term_months": 24,
                             "borrower_kind": "legal"}, S=40_000_000)
    ok("акт 13з берёт шаблон 13з (не 14): риски заёмщика, правило credit, документы с отчётом КАТМ",
       st == 200 and a["template"]["class_code"] == "13з" and a["template"]["alias_of"] is None
       and [i["code"] for i in a["analytics"]["risks"]["items"]] == ["bl_default", "bl_insolvency", "bl_closure"]
       and a["template"]["scenario_rule"]["code"] == "credit"
       and any("КАТМ" in d for d in a["template"]["documents"]["items"]), (st, a.get("template", {}).get("class_code")))
    ok("13з: кредит 100 млн, залог 60 млн, сумма 40 млн — в пределах min(40; 50); PML = EML = MFL = 40 млн",
       [a["scenarios"][k]["amount"] for k in ("pml", "eml", "mfl")] == [40_000_000] * 3
       and any("допустимая страховая сумма 40 000 000" in flat(h) and "в пределах" in h for h in a["scenarios"]["how"]),
       a["scenarios"]["how"])
    ok("13з: в notes — чем отличается от 14 (чья ответственность страхуется)",
       any(n["code"] == "diff_14" and "ответственность самого заёмщика" in n["text"] for n in a["template"]["notes"]))
    st, a = _cls_act("13з", {"credit_amount": 100_000_000, "collateral_value": 60_000_000}, S=50_000_000)
    ok("13з: сумма 50 млн выше допустимой 40 млн — проверка андеррайтеру (правило проекта № 6)",
       any("выше допустимой" in flat(c) and "уменьшить до 40 000 000 сум" in flat(c) for c in a["decision"]["checks"])
       and a["decision"]["code"] != "accept", a["decision"]["checks"])
    # ---------- строки L* в базе игнорируются ----------
    check_life_rows_ignored()
    TPL_REPORT["16у"] = [r[k]["amount"] for k in ("PML", "EML", "MFL")]


def check_life_rows_ignored():
    """Строки L* (классы жизни), если они почему-то есть в classes или class_templates, нигде не показываются:
    справочник /reference/classes, выпадающий список аналитики, /act/templates, акт. ensure их не трогает."""
    from app import class_templates as ctm
    from app import risk_api
    fresh()
    life_tpl = _json.dumps({"name": {"ru": "Страхование жизни", "uz": "Hayot", "en": "Life"}}, ensure_ascii=False)
    with db.tx() as con:
        ctm.ensure(con)
        con.execute("INSERT OR IGNORE INTO classes (code, name, group_code, branch, kind) "
                    "VALUES ('L4', 'Страхование жизни (тест)', NULL, 'жизнь', 'личное')")
        con.execute("INSERT INTO class_templates (class_code, version, json, source, file_version, updated_at, "
                    "updated_by, calibrated, note) VALUES ('L4', '9.0', ?, 'file', '9.0', '2026-10-01T00:00:00', "
                    "'тест', 0, 'тест: строка класса жизни')", (life_tpl,))
        n_before = con.execute("SELECT COUNT(*) FROM class_templates").fetchone()[0]
    db.invalidate_reference()
    try:
        ctm.reset_cache()
        with db.tx() as con:
            res = ctm.ensure(con)
            n_after = con.execute("SELECT COUNT(*) FROM class_templates").fetchone()[0]
            codes = [r["class_code"] for r in ctm.all_current(con)]
            cur = ctm.current(con, "L4")
            hist = ctm.history(con, "L4")
            cat = [c["code"] for c in risk_api._catalog(con, [])["classes"]]
        ok("ensure не трогает строки L*: ничего не добавлено, строк в class_templates столько же",
           not res.get("added") and not res.get("classes_added") and n_after == n_before, (res, n_before, n_after))
        ok("all_current — 20 шаблонов без L*; current('L4') и history('L4') пусты",
           codes == [str(i) for i in range(1, 19)] + ["13з", "16у"] and cur is None and hist == [], (codes, cur))
        ok("выпадающий список классов аналитики рисков — без L4", "L4" not in cat and "18" in cat, cat)
        st, ref = call("GET", "/reference/classes")
        ok("GET /reference/classes — без L4, класс 18 есть",
           st == 200 and "L4" not in [r["code"] for r in ref] and "18" in [r["code"] for r in ref],
           (st, [r.get("code") for r in ref] if isinstance(ref, list) else ref))
        st, lst = call("GET", "/act/templates", params={"lang": "ru"})
        ok("GET /act/templates — 20 шаблонов, L4 нет", st == 200 and len(lst["templates"]) == 20
           and not any(x["class_code"].startswith("L") for x in lst["templates"]), (st, lst.get("counts")))
        st, _x = call("GET", "/act/templates/L4")
        st2, _x = call("GET", "/act/templates/L4/history")
        ok("GET /act/templates/L4 и его история — 404", st == 404 and st2 == 404, (st, st2))
        st, a = _cls_act("L4", {"insured_count": 10}, S=500_000_000)
        ok("акт по классу L4 не формируется — 422 «класс не найден в справочнике»",
           st == 422 and "класс не найден" in str(a), (st, str(a)[:300]))
    finally:
        with db.tx() as con:
            con.execute("DELETE FROM class_templates WHERE class_code LIKE 'L%'")
            con.execute("DELETE FROM classes WHERE code LIKE 'L%'")
        ctm.reset_cache()
        db.invalidate_reference()


MED18 = {"insured_count": 50, "program": "амбулатория и стационар", "limit_per_person": 10_000_000,
         "territory": "uzbekistan", "avg_visits": 4, "avg_bill": 300_000, "copay_pct": 10, "waiting_days": 30}


def check_templates_class18():
    print("41е. Шаблоны 1.3.0: класс 18 «Tibbiy sugʻurta» (медицинское страхование) — шаблон, справочник, акт без ставки")
    from app import class_templates as ctm
    fresh()
    model_on(False)
    data = ctm.load_file()
    t = data["classes"]["18"]
    lib = (ROOT_DIR / "library" / "01_Законодательство" / "02_Акты_регуляторов" /
           "ПКМ № 80 от 21.02.2022 — единое положение о лицензировании (прил. 6 — классификатор страховой "
           "деятельности) (uz).txt")
    law = lib.read_text(encoding="utf-8") if lib.exists() else ""
    on = t["official_name"]
    ok("класс 18: узбекское название и содержание дословно из ПКМ № 80, прил. 6 (18-klass «Tibbiy sugʻurta»); "
       "текст класса 2 для сравнения — тоже дословно",
       t["name"]["uz"] == on["text_uz"] == "Tibbiy sugʻurta" and " 18-klass \n Tibbiy sugʻurta \n " + on["content_uz"] in law
       and on["content_uz"].startswith("Sugʻurtalangan shaxsning sugʻurta shartnomasida koʻrsatilgan shartlarga "
                                       "muvofiq tibbiy yordam olishini")
       and on["content_uz"].endswith("2-klass va hayotni sugʻurta qilish sohasining IV klassi boʻyicha "
                                     "shartnomalarni istisno qilgan holda")
       and " Kasallikdan ehtiyot shart sugʻurta qilish \n " + on["class_2_uz"]["content"] in law, on.get("text_uz"))
    ok("класс 18: не вариант; русское и английское названия — перевод (пометка в notes)",
       not t.get("variant") and t["name"]["ru"] == "Медицинское страхование"
       and t["name"]["en"] == "Medical insurance"
       and any(n["code"] == "official" and "перевод разработчика" in n["text"]["ru"] for n in t["notes"]))
    ok("класс 18: обязательные — число застрахованных, программа, лимит на человека, территория (4); метод оценки 6",
       [f["code"] for f in t["must"]] == ["insured_count", "program", "limit_per_person", "territory"]
       and [m["method"] for m in t["valuation_methods"]] == [6] and t["object"]["people"] is True)
    ok("класс 18: дополнительные — возраст агрегатами, сеть клиник, франшиза, сооплата, период ожидания, обращения, "
       "средний счёт, убытки за 3 года",
       [f["code"] for f in t["optional"] if not f.get("factor")] == ["age_structure", "clinics", "deductible",
                                                                     "copay_pct", "waiting_days", "avg_visits",
                                                                     "avg_bill", "losses_3y"]
       and [f["code"] for f in t["optional"] if f.get("factor")] == ["occupation_group", "program_level"]
       and "агрегаты" in t["optional"][0]["label"]["ru"])
    shares = [(r["code"], r["share_pct"]) for r in t["risks"]["items"]]
    ok("класс 18: риски — амбулатория 35, стационар 30, экстренная 12, лекарства 10, беременность и роды 7, "
       "стоматология 6 = 100 (оценка разработчика, не утверждено страховщиком)",
       shares == [("md_outpatient", 35), ("md_inpatient", 30), ("md_emergency", 12), ("md_drugs", 10),
                  ("md_maternity", 7), ("md_dental", 6)]
       and "не утверждено страховщиком" in t["risks"]["note"]["ru"] and t["risks"]["calibrated"] == 0, shares)
    ok("класс 18: правило сценария frequency, как у класса 2 (эпидемия — MFL, 30 % экспертно)",
       t["scenario_rule"]["code"] == data["classes"]["2"]["scenario_rule"]["code"] == "frequency"
       and t["scenario_rule"]["params"] == {"epidemic_share": 0.3} and "эпидемия" in t["scenario_rule"]["what"]["MFL"]["ru"])
    diff = next(n["text"]["ru"] for n in t["notes"] if n["code"] == "diff_2")
    ok("класс 18: в notes — отличие от класса 2 по акту (деньги — класс 2, медицинская помощь — класс 18) и учётная "
       "группа NULL (в Положении 1882, п. 10 класса 18 нет)",
       "Kasallikdan ehtiyot shart sugʻurta qilish" in diff and "медицинской помощи" in diff
       and t["accounting_group"] is None
       and any(n["code"] == "accounting_group" and "Положение 1882, п. 10" in n["text"]["ru"] for n in t["notes"]))
    ok("класс 18: персональные данные застрахованных в акт и модель не попадают; документы — программа, клиники, "
       "список без ПД",
       any(n["code"] == "personal" and "в акт и модель не попадают" in n["text"]["ru"] for n in t["notes"])
       and len(t["documents"]["items"]) == 3 and "без персональных данных" in t["documents"]["items"][2]["ru"])
    with db.tx() as con:
        ctm.ensure(con)
        row = con.execute("SELECT code, group_code, branch, kind FROM classes WHERE code='18'").fetchone()
        nprod = ctm.products_count(con, "18")
    ok("справочник classes: класс 18 — «общее», «личное», учётная группа NULL; продуктов класса 18 нет",
       row is not None and tuple(row) == ROW_18 and nprod == 0, (tuple(row) if row else None, nprod))
    st, a = _cls_act("18", MED18, S=500_000_000)
    sc = a["scenarios"]
    ok("акт по классу 18 формируется: ставка и премия не определены (продуктов нет), пометка в «как посчитана ставка»",
       st == 200 and a["rate"]["applied_pct"] is None and a["premium"]["amount"] is None
       and a["template"]["class_code"] == "18"
       and a["template"]["products_count"] == 0 and a["template"]["no_products_note"]
       and any("нет продуктов класса 18" in h for h in a["rate"]["how"]), (st, a.get("rate", {}).get("how")))
    # руками: PML = EML = лимит 10 млн; MFL = max(10 млн; 0,3 × 500 млн) = 150 млн; ожидаемый убыток 4 × 300 000 × 50
    ok("класс 18, 50 чел. × лимит 10 млн, сумма 500 млн: PML = EML = 10 млн, MFL (эпидемия) = 0,3 × 500 млн = 150 млн; "
       "ожидаемый годовой убыток 4 × 300 000 × 50 = 60 млн — справочно",
       [sc[k]["amount"] for k in ("pml", "eml", "mfl")] == [10_000_000, 10_000_000, 150_000_000]
       and sc["source"] == "template" and sc["rule"] == "frequency" and "эпидемия" in sc["mfl"]["what"]
       and any("60 000 000" in flat(h) and "справочно" in h for h in sc["how"]),
       ([sc[k]["amount"] for k in ("pml", "eml", "mfl")], sc.get("how")))
    ok("акт класса 18: риски шаблона, оговорки (5) и мероприятия (4) из шаблона",
       a["analytics"]["risks"]["source"] == "template"
       and [(i["code"], i["share_of_net_pct"]) for i in a["analytics"]["risks"]["items"]][:2]
       == [("md_outpatient", 35.0), ("md_inpatient", 30.0)]
       and [c["code"] for c in a["clauses"]] == ["hl_program", "hl_preexisting", "hl_waiting", "hl_copay", "ot_declared"]
       and [m["code"] for m in a["measures"]] == ["hl_checkup", "hl_network", "hl_control", "hl_preauth"],
       [c["code"] for c in a["clauses"]])
    st, a0 = _cls_act("18", None, S=500_000_000)
    ok("класс 18 без полей: сценарии по страховой сумме, в «принято по умолчанию» — лимит на человека",
       st == 200 and [a0["scenarios"][k]["amount"] for k in ("pml", "eml", "mfl")] == [500_000_000] * 3
       and {x["code"] for x in a0["scenarios"]["assumptions"]} >= {"as_tpl_limit_person", "as_tpl_epidemic"},
       a0["scenarios"].get("assumptions"))
    ok("класс 18: неверная территория — 422", _cls_act("18", {"territory": "moon"})[0] == 422)
    st, e = call("GET", "/act/templates/18", params={"lang": "en"})
    st2, z = call("GET", "/act/templates/18", params={"lang": "uz"})
    ok("GET /act/templates/18: en «Medical insurance», uz «Tibbiy sugʻurta», риск uz «Ambulator yordam», нет продуктов",
       st == 200 and st2 == 200 and e["template"]["name"] == "Medical insurance" and z["template"]["name"] == "Tibbiy sugʻurta"
       and z["template"]["risks"]["items"][0]["label"] == "Ambulator yordam" and e["products_count"] == 0,
       (st, e.get("template", {}).get("name")))
    TPL_REPORT["класс 18"] = [sc[k]["amount"] for k in ("pml", "eml", "mfl")]


PT_REPORT = {}
PT_CAR = {"class_code": "3", "sum_insured": 60_000_000, "object_value": 60_000_000,
          "object_description": "легковой автомобиль в залоге"}
PT_CREDIT = {"class_code": "14", "sum_insured": 40_000_000,
             "fields": {"class_fields": {"credit_amount": 100_000_000, "collateral_value": 60_000_000,
                                         "credit_term_months": 24}}}
PT_MUST = {"product_code": "0312", "sum_insured": 100_000_000, "object_value": 60_000_000, "region": "Ташкент"}
PT_OPT = {"losses_3y": {"count": 0, "small_count": 0}, "documents_provided": True}


def _pt_make(must, optional, lang="ru"):
    return call("POST", "/act/make", {"lang": lang, "must": must, "optional": optional})


def check_parts_engine():
    print("42а. Комплексный продукт: чистые функции разбора по частям (act_engine, раздел 9)")
    ok("ставки частей из текста тарифа: 0305 «ТС 1,1% · НС 0,5% · ОТВ 1%» → 3: 1,1; 1: 0,5; 13: 1",
       ae.part_rates_from_text("ТС 1,1% · НС 0,5% · ОТВ 1%", ["3", "1", "13"]) == {"3": 1.1, "1": 0.5, "13": 1.0})
    ok("0312 «фин. риск 0,5% · залог 0,5%» → 14: 0,5; 3: 0,5; 1415 «ГБО 0,3% · ГО 0,5% · кредит 1,7%» → 8/13/14",
       ae.part_rates_from_text("фин. риск 0,5% · залог 0,5%", ["3", "14"]) == {"14": 0.5, "3": 0.5}
       and ae.part_rates_from_text("ГБО 0,3% · ГО 0,5% · кредит 1,7%", ["8", "13", "14"]) ==
       {"8": 0.3, "13": 0.5, "14": 1.7})
    ok("одна ставка без подписи «0,25% фикс.» — общая для частей; «по согласованию» — ставок нет",
       ae.part_rates_from_text("0,25% фикс.", ["13", "1"]) == {"*": 0.25}
       and ae.part_rates_from_text("по согласованию с ЦО", ["8", "9"]) == {})
    sp = ae.split_sum(1_000_000_001, ["3", "14"])
    ok("поровну: 1 000 000 001 → 500 000 000 + 500 000 001 (остаток — последней части), сумма точно S",
       [x["sum_insured"] for x in sp] == [500_000_000, 500_000_001] and ae.check_parts_sum(sp, 1_000_000_001) is None)
    sp2 = ae.split_sum(1_000_000_000, ["8", "9"], {"8": 70, "9": 30})
    ok("по долям тарифной политики 70/30: 700 млн + 300 млн",
       [x["sum_insured"] for x in sp2] == [700_000_000, 300_000_000] and [x["share_pct"] for x in sp2] == [70, 30])
    bad = ae.check_parts_sum([{"sum_insured": 60e6}, {"sum_insured": 39_999_998}], 100e6, 1)
    ok("сумма частей на 2 сума меньше при допуске 1 сум — расхождение; на 1 сум — сходится",
       bad == {"total": 99_999_998, "sum_insured": 100e6, "diff": 2}
       and ae.check_parts_sum([{"sum_insured": 60e6}, {"sum_insured": 39_999_999}], 100e6, 1) is None, bad)
    rows = ae.parts_from_items([{"name": "Легковой автомобиль в залоге", "sum": 60e6},
                                {"name": "Финансовый риск непогашения кредита", "sum": 40e6}], ["3", "14"])
    ok("объекты договора → части: автомобиль → класс 3, непогашение кредита → класс 14",
       [(r["class_code"], r["sum_insured"], r["class_guess"]) for r in rows] == [("3", 60e6, False), ("14", 40e6, False)],
       rows)
    rows = ae.parts_from_items([{"name": "Склад", "sum": 1e9}, {"name": "Ущерб от залива и кражи", "sum": 2e8},
                                {"name": "Прочее", "sum": 1e8}], ["8", "9"], {"8": ["склад"], "9": ["склад"]})
    ok("8/9: «склад» → 8, «ущерб от залива и кражи» → 9, неузнанное «прочее» — к первому классу с пометкой",
       [(r["class_code"], r["sum_insured"], r["class_guess"]) for r in rows] == [("8", 1.1e9, True), ("9", 2e8, False)],
       rows)
    ok("один объект по умолчанию — только 8/9/16", ae.default_same_object(["8", "9", "16"])
       and not ae.default_same_object(["3", "14"]) and not ae.default_same_object(["8", "1"]))
    sc = lambda p, e, m: {"available": True, "items": {"PML": {"amount": p}, "EML": {"amount": e}, "MFL": {"amount": m}}}
    same = ae.aggregate_scenarios([{"index": 1, "class_code": "8", "main": True, "scenarios": sc(70, 100, 100)},
                                   {"index": 2, "class_code": "9", "main": True, "scenarios": sc(15, 37, 75)}])
    diff = ae.aggregate_scenarios([{"index": 1, "class_code": "3", "main": True, "scenarios": sc(30, 60, 60)},
                                   {"index": 2, "class_code": "14", "main": False, "scenarios": sc(40, 40, 40)},
                                   {"index": 3, "class_code": "13", "main": False, "scenarios": {"available": False,
                                                                                                 "reason": "sc_na_class"}}])
    ok("сценарии: один объект — большее (70/100/100), разные объекты — сумма (70/100/100), часть без правила — "
       "не входит", same["rule"] == "max" and [same["items"][s]["amount"] for s in ("PML", "EML", "MFL")] == [70, 100, 100]
       and diff["rule"] == "sum" and [diff["items"][s]["amount"] for s in ("PML", "EML", "MFL")] == [70, 100, 100]
       and diff["excluded"] == [{"index": 3, "class_code": "13", "reason": "sc_na_class"}], (same, diff))
    ret = ae.contract_retention([{"known": True, "limit": 11e9}, {"known": True, "limit": 7e9}], 8e9, 9e9)
    ok("удержание договора: наименьший лимит частей 7 млрд против EML договора 8 млрд — превышение 1 млрд",
       ret["limit"] == 7e9 and ret["eml_excess"] == 1_000_000_000 and ret["within"] is False
       and ret["mfl_excess"] == 2_000_000_000, ret)
    tot = ae.contract_totals([{"premium": 3_720_000, "level": "moderate", "sum_insured": 146.1e6, "term_days": 365},
                              {"premium": 4_480_000, "level": "high", "sum_insured": 226.1e6, "term_days": 365}], 372.2e6)
    ok("итоги: премия = сумма премий частей, уровень — самый высокий, ставка договора только справочно",
       tot["premium"] == 8_200_000 and tot["level"] == "high" and tot["reference_only"]
       and tot["reference_rate_pct"] == 2.2031, tot)
    with db.tx() as con:
        ref = db.load_reference(con)
        prod = db.rows(con, "SELECT code, name, pricing_mode, rate_text FROM products WHERE code='0312'")[0]
        r14 = ae.part_rate(ref, prod, "14", "moderate", 40e6, 365, None, None, act.load_settings(con))
        r3 = ae.part_rate(ref, prod, "3", "moderate", 60e6, 365, None, None, act.load_settings(con))
    ok("0312: минимум класса 3 — справочник min_rates 0,5 %; класса 14 — из текста тарифа 0,5 % (в min_rates его нет)",
       r3["class_min"] == {"pct": 0.5, "source": "min_rates"} and r14["class_min"] == {"pct": 0.5, "source": "rate_text"}
       and r14["min_pct"] == 0.5 and r14["applied_pct"] == 0.6 and r14["premium"] == 240_000,
       (r3["class_min"], r14["class_min"], r14["applied_pct"], r14["premium"]))


def check_parts_make():
    print("42б. Комплексный продукт в /act/make: части по умолчанию, подтверждённые, из договора, 8/9, обязательная часть")
    fresh()
    model_on(False)
    with db.tx() as con:
        ref = db.load_reference(con)
        st_ = act.load_settings(con)
        name = db.rows(con, "SELECT name, pricing_mode FROM products WHERE code='0312'")[0]
        cls0312 = [r["class_code"] for r in db.rows(con, "SELECT class_code FROM product_classes "
                                                         "WHERE product_code='0312' ORDER BY part_no")]
    ok("0312 — «Автокредит» (Хамкорбанк): классы 3 (залоговый автомобиль) и 14 (невозврат кредита) — разные объекты, "
       "делить осмысленно", cls0312 == ["3", "14"] and "Автокредит" in name["name"] and name["pricing_mode"] == "ставка",
       (cls0312, name))
    # 1. части по умолчанию: поровну, предложение с пометкой
    st, a = _pt_make(PT_MUST, PT_OPT)
    P = a.get("parts") or {}
    text = all_text(a) if st == 200 else str(a)
    ok("0312 без частей: mode multi, source default, не подтверждено, suggested_parts 50 + 50 млн",
       st == 200 and P["mode"] == "multi" and P["source"] == "default" and not P["confirmed"]
       and [(x["class_code"], x["sum_insured"]) for x in a["suggested_parts"]] == [("3", 50e6), ("14", 50e6)]
       and a["suggested_parts"] == P["suggested_parts"], (st, P.get("source"), a.get("suggested_parts")))
    ok("пометка «распределение по умолчанию — подтвердите» в акте и в решении, «разбор отложен» больше нет",
       "разделена поровну на 2 части по умолчанию" in text and "принято поровну по умолчанию — подтвердить" in text
       and any("Подтвердить распределение" in c for c in a["decision"]["checks"])
       and "разбор по частям отложен" not in text and a["rate"]["multi_class"], text[:300])
    ok("переключатель по умолчанию: автомобиль и кредит — разные объекты (object_mode different)",
       P["object_mode"] == "different" and [x["same_object"] for x in P["items"]] == [True, False])
    # 2. подтверждённые части: автомобиль 60 млн (залог), кредит 40 млн = min(100 − 60; 50)
    st, a = _pt_make(PT_MUST, dict(PT_OPT, parts=[PT_CAR, PT_CREDIT]))
    P = a["parts"]
    it = P["items"]
    exp = []
    for p in it:
        adj = st_["adj_pct"][p["level"]]
        applied = round(0.5 * (1 + adj / 100), 4)
        exp.append((applied, round(premium_of(applied, p["sum_insured"], 365))))
    ok("части сотрудника: source employee, подтверждено, suggested_parts на верхнем уровне нет",
       st == 200 and P["source"] == "employee" and P["confirmed"] and "suggested_parts" not in a, (st, P.get("source")))
    ok("ставка каждой части — по своему классу: 0,5 % × поправка уровня, не ниже 0,5 % класса",
       [(p["rate"]["applied_pct"], p["premium"]) for p in it] == exp
       and [p["rate"]["min_pct"] for p in it] == [0.5, 0.5]
       and [p["rate"]["min_source"] for p in it] == ["min_rates", "rate_text"], ([(p["rate"], p["premium"]) for p in it], exp))
    ok("премия договора = сумма премий частей; верхнее поле premium — итог договора",
       a["premium"]["amount"] == sum(p["premium"] for p in it) == P["totals"]["premium"],
       (a["premium"]["amount"], [p["premium"] for p in it]))
    lv = {"low": 0, "moderate": 1, "high": 2}
    ok("уровень договора — самый высокий среди частей (верхнее поле risk)",
       a["risk"]["level"] == max((p["level"] for p in it), key=lv.get) == P["totals"]["level"])
    ok("средняя ставка договора — только справочно: rate.applied_pct и min_pct пустые, reference_pct = премия / сумма",
       a["rate"]["mode"] == "multi" and a["rate"]["applied_pct"] is None and a["rate"]["min_pct"] is None
       and a["rate"]["reference_pct"] == round(a["premium"]["amount"] / 100e6 * 100, 4)
       and "средняя не используется для проверки минимума" in all_text(a), a["rate"])
    sc_parts = [[p["scenarios"][k]["amount"] for k in ("pml", "eml", "mfl")] for p in it]
    ok("разные объекты — сценарии складываются: EML договора = EML автомобиля + EML кредита",
       a["scenarios"]["available"] and [a["scenarios"][k]["amount"] for k in ("pml", "eml", "mfl")] ==
       [sc_parts[0][i] + sc_parts[1][i] for i in range(3)] and P["totals"]["scenarios"]["rule"] == "sum", sc_parts)
    ok("кредит 40 млн при залоге 60 млн из 100 млн — в пределах: проверки превышения нет",
       not any("выше допустимой" in c for c in a["decision"]["checks"])
       and sc_parts[1] == [40_000_000] * 3)
    ok("удержание договора сравнивается с EML договора",
       a["scenarios"]["retention"]["compared_with"] == "eml" and (not a["scenarios"]["retention"]["known"] or
       a["scenarios"]["retention"]["eml_excess"] == max(a["scenarios"]["eml"]["amount"] - a["scenarios"]["retention"]["limit"], 0)))
    ok("сумма к стоимости по частям: автомобиль 100 %, у кредита — «не применяется»; по договору 100 %",
       it[0]["value"]["ratio_pct"] == 100.0 and it[1]["value"]["verdict"] == "na" and not it[1]["value"]["applicable"]
       and a["value"]["ratio_pct"] == 100.0, [p["value"] for p in it])
    ok("аналитика — по каждой части (parts.items[].analytics), сводка договора — общая",
       all(p["analytics"].get("available") for p in it) and a["analytics"]["reason"] == "by_parts"
       and a["analytics"]["summary"]["text"].startswith("Договор из 2 частей"), a["analytics"].get("summary"))
    t4 = a["sections"][3]
    tbl = next((li for li in t4["lists"] if li["title"] == "Части договора"), None)
    ok("раздел 4: таблица частей (класс, сумма, уровень, ставка, премия, франшиза) с итогом договора",
       tbl and tbl["table"]["columns"] == ["№", "Класс", "Страховая сумма", "Уровень", "Ставка", "Премия", "Франшиза"]
       and len(tbl["table"]["rows"]) == 3 and tbl["table"]["rows"][-1][1] == "Итого по договору", tbl)
    ok("раздел 1 — перечень частей, раздел 3 — сумма к стоимости по частям, раздел 5 — подтверждено сотрудником",
       sum(1 for r in a["sections"][0]["rows"] if r["label"].startswith("Часть ")) == 2
       and sum(1 for r in a["sections"][2]["rows"] if r["label"].startswith("Часть ")) == 2
       and "Распределение страховой суммы по классам подтверждено сотрудником." in a["sections"][4]["paragraphs"])
    PT_REPORT["0312 подтверждено"] = {"части": [(p["class_code"], p["sum_insured"], p["level"], p["rate"]["applied_pct"],
                                                 p["premium"]) for p in it], "премия": a["premium"]["amount"],
                                       "PML/EML/MFL": [a["scenarios"][k]["amount"] for k in ("pml", "eml", "mfl")]}
    aid = a["id"]
    # кредит 60 млн — выше допустимых 40 млн: проверка по части
    st, a2 = _pt_make(dict(PT_MUST, sum_insured=120_000_000),
                      dict(PT_OPT, parts=[PT_CAR, dict(PT_CREDIT, sum_insured=60_000_000)]))
    ok("кредит 60 млн при допустимых 40 млн — проверка по части 2 с превышением 20 млн",
       any("Часть 2 (класс 14)" in c and "20 000 000" in flat(c) for c in a2["decision"]["checks"]),
       a2["decision"]["checks"])
    # франшиза по частям: своя франшиза только у автомобиля
    st, a3 = _pt_make(PT_MUST, dict(PT_OPT, parts=[dict(PT_CAR, deductible={"pct": 1}), PT_CREDIT]))
    f = [p["franchise"] for p in a3["parts"]["items"]]
    ok("франшиза по каждой части отдельно: у части 1 применена 1 %, у части 2 — нет; премия договора — сумма частей",
       f[0]["status"] == "applied" and f[0]["size_pct"] == 1.0 and f[1]["status"] == "none"
       and a3["premium"]["amount"] == sum(p["premium"] for p in a3["parts"]["items"])
       and "часть 1 — применена 1" in flat(all_text(a3)), f)
    ok("часть 1 на минимуме класса (0,5 %): со франшизой ставка не опускается ниже минимума — премия та же, пометка",
       f[0]["floor_applied"] and a3["parts"]["items"][0]["premium"] == a3["parts"]["items"][0]["premium_before_franchise"]
       and "упёрлась в минимальную ставку" in f[0]["text"], (f[0]["floor_applied"], f[0]["text"]))
    # 3. ошибки ввода
    st, e = _pt_make(PT_MUST, dict(PT_OPT, parts=[PT_CAR, dict(PT_CREDIT, sum_insured=39_999_998)]))
    ok("сумма частей ≠ страховой сумме (разница 2 сума) — 422 с объяснением",
       st == 422 and "parts" in e["errors"] and "не равна страховой сумме договора" in e["errors"]["parts"], (st, e))
    st, e1 = _pt_make(PT_MUST, dict(PT_OPT, parts=[PT_CAR, dict(PT_CREDIT, sum_insured=39_999_999)]))
    ok("разница 1 сум — в допуске, акт формируется", st == 200, (st, e1 if st != 200 else ""))
    st, e = _pt_make(PT_MUST, dict(PT_OPT, parts=[dict(PT_CAR, share_pct=150), PT_CREDIT]))
    ok("доля больше 100 — 422", st == 422 and "share_pct" in e["errors"]["parts"], e)
    st, e = _pt_make(PT_MUST, dict(PT_OPT, parts=[dict(PT_CAR, class_code="99"), PT_CREDIT]))
    ok("класс не из справочника — 422", st == 422 and "не найден" in e["errors"]["parts"], e)
    st, e = _pt_make({"product_code": "0807", "sum_insured": 1e9, "object_value": 1e9, "region": "Ташкент"},
                     {"parts": [{"class_code": "8", "sum_insured": 1e9}]})
    ok("у продукта с одним классом одна часть — 422 (это обычный акт)", st == 422, e)
    st, e = _pt_make(PT_MUST, dict(PT_OPT, parts=[PT_CAR, dict(PT_CREDIT, deductible={"pct": 80})]))
    ok("франшиза части больше 50 % — 422", st == 422 and "deductible" in e["errors"]["parts"], e)
    # 4. из договора: перечень объектов
    ct = {"sum_insured": 100_000_000, "premium": 500_000, "term_from": "2026-10-01", "term_to": "2027-09-30",
          "items": [{"name": "Легковой автомобиль в залоге", "sum": 60_000_000},
                    {"name": "Финансовый риск непогашения кредита", "sum": 40_000_000}]}
    st, a4 = _pt_make(PT_MUST, dict(PT_OPT, contract=ct))
    P4 = a4.get("parts") or {}
    ok("из договора: части по перечню объектов (автомобиль → 3, кредит → 14), source contract, на подтверждение",
       st == 200 and P4["source"] == "contract" and not P4["confirmed"]
       and [(x["class_code"], x["sum_insured"]) for x in a4["suggested_parts"]] == [("3", 60e6), ("14", 40e6)],
       (st, P4.get("source"), a4.get("suggested_parts")))
    ok("сверка с договором: премия договора против премии акта (суммы частей), суммы объектов = общей сумме",
       a4["contract_check"]["available"]
       and next(i for i in a4["contract_check"]["items"] if i["code"] == "premium_act")["calculated"] == a4["premium"]["amount"]
       and next(i for i in a4["contract_check"]["items"] if i["code"] == "items_sum")["verdict"] == "ok",
       a4["contract_check"]["items"])
    # 5. КАСКО-пример заметок: средняя ниже минимума первого класса, но каждая часть — не ниже своего минимума
    st, k = _pt_make({"product_code": "0305", "sum_insured": 400_000_000, "object_value": 100_000_000,
                      "region": "Ташкент"},
                     {"losses_3y": {"count": 0}, "documents_provided": True,
                      "parts": [{"class_code": "3", "sum_insured": 100_000_000},
                                {"class_code": "1", "sum_insured": 200_000_000},
                                {"class_code": "13", "sum_insured": 100_000_000}]})
    ki = k["parts"]["items"]
    ok("0305 (ТС + НС + ОТВ): минимумы по классам 1,1 / 0,5 / 1 %; каждая часть не ниже своего минимума",
       [p["rate"]["min_pct"] for p in ki] == [1.1, 0.5, 1.0]
       and all(p["rate"]["applied_pct"] >= p["rate"]["min_pct"] for p in ki), [p["rate"] for p in ki])
    ok("0305: средняя по договору ниже минимума класса 3 (1,1 %) — но это справочно, остановки нет (правило № 5)",
       k["rate"]["reference_pct"] < 1.1 and not any("ниже минимал" in c for c in k["decision"]["checks"])
       and k["decision"]["code"] != "decline", (k["rate"]["reference_pct"], k["decision"]))
    PT_REPORT["0305 по классам"] = {"ставки": [p["rate"]["applied_pct"] for p in ki],
                                    "премии": [p["premium"] for p in ki], "премия": k["premium"]["amount"],
                                    "справочная ставка": k["rate"]["reference_pct"]}
    # 6. 8/9 один объект: 0824 гостиница (8, 9, 16), по согласованию — ставка по каждой части не определена
    st, h = _pt_make({"product_code": "0824", "sum_insured": 3_000_000_000, "object_value": 3_000_000_000,
                      "region": "Ташкент"}, {"object_kind": "hotel", "losses_3y": {"count": 0}})
    hi = h["parts"]["items"]
    ok("0824 (8 + 9 + 16): по умолчанию один объект, сценарии договора — большее из частей",
       st == 200 and h["parts"]["object_mode"] == "one" and all(p["same_object"] for p in hi)
       and [h["scenarios"][k]["amount"] for k in ("pml", "eml", "mfl")] ==
       [max(p["scenarios"][k]["amount"] for p in hi if p["scenarios"]["available"]) for k in ("pml", "eml", "mfl")],
       [[p["scenarios"][k]["amount"] for k in ("pml", "eml", "mfl")] for p in hi])
    ok("0824 по согласованию: у частей ставки нет, премия договора не определена, проверка по каждой части",
       h["premium"]["amount"] is None and all(p["rate"]["mode"] == "undefined" for p in hi)
       and sum(1 for c in h["decision"]["checks"] if "определить ставку" in c) == 3, h["decision"]["checks"])
    # 8/9 с явными частями + обязательная часть (0820, ПКМ № 532) на другом объекте
    wh = {"product_code": "0807", "sum_insured": 5_000_000_000, "object_value": 5_000_000_000, "region": "Ташкент"}
    parts = [{"class_code": "8", "sum_insured": 3_000_000_000},
             {"class_code": "9", "sum_insured": 1_500_000_000, "same_object": True,
              "fields": {"class_fields": {"largest_room_value": 600_000_000}}},
             {"class_code": "8", "product_code": "0820", "sum_insured": 500_000_000, "same_object": False,
              "deductible": {"pct": 1}, "object_description": "строящийся склад (СМР)"}]
    st, w = _pt_make(wh, {"object_kind": "warehouse", "losses_3y": {"count": 0}, "parts": parts})
    wi = w["parts"]["items"]
    ok("склад: 8 и 9 — один объект (большее), СМР 0820 — другой объект (прибавляется): правило mixed",
       st == 200 and w["parts"]["totals"]["scenarios"]["rule"] == "mixed"
       and w["scenarios"]["eml"]["amount"] == max(wi[0]["scenarios"]["eml"]["amount"], wi[1]["scenarios"]["eml"]["amount"])
       + wi[2]["scenarios"]["eml"]["amount"], [p["scenarios"]["eml"]["amount"] for p in wi])
    ok("обязательная часть 0820: ставка 0,4 % по ПКМ № 532 без поправок, франшиза сотрудника не применена",
       wi[2]["rate"]["mode"] == "statutory" and wi[2]["rate"]["applied_pct"] == 0.4 and wi[2]["rate"]["adj_pct"] == 0
       and wi[2]["franchise"]["status"] == "statutory" and wi[2]["premium"] == 2_000_000
       and wi[2]["rate"]["min_source"] == "act", (wi[2]["rate"], wi[2]["franchise"]["status"]))
    ok("класс 9 не из состава продукта 0807 — принят с пометкой и проверкой, база — техническая ставка класса",
       wi[1]["class_outside"] and wi[1]["rate"]["base_source"] == "technical"
       and any("Часть 2 (класс 9): проверить класс" in c for c in w["decision"]["checks"]), wi[1]["rate"])
    ok("премия склада = сумма трёх частей", w["premium"]["amount"] == sum(p["premium"] for p in wi))
    PT_REPORT["склад 8/9 + СМР 0820"] = {"премии": [p["premium"] for p in wi], "премия": w["premium"]["amount"],
                                         "EML частей": [p["scenarios"]["eml"]["amount"] for p in wi],
                                         "EML договора": w["scenarios"]["eml"]["amount"]}
    # переключатель «разные объекты» для 0824
    st, h2 = _pt_make({"product_code": "0824", "sum_insured": 3_000_000_000, "object_value": 3_000_000_000,
                       "region": "Ташкент"}, {"object_kind": "hotel", "losses_3y": {"count": 0}, "same_object": False})
    ok("переключатель «разные объекты» (optional.same_object = false): сценарии складываются",
       h2["parts"]["object_mode"] == "different" and h2["scenarios"]["eml"]["amount"] ==
       sum(p["scenarios"]["eml"]["amount"] for p in h2["parts"]["items"] if p["scenarios"]["available"]))
    return aid


def check_parts_langs_files(aid):
    print("42в. Комплексный продукт: три языка, Word и PDF; однопродуктовые акты без изменений")
    fresh()
    st, u = call("GET", f"/act/{aid}", params={"lang": "uz"})
    st2, e = call("GET", f"/act/{aid}", params={"lang": "en"})
    tu, te = all_text(u), all_text(e)
    ok("по-узбекски: таблица «Shartnoma qismlari», «Jami», части «1-qism»",
       "Shartnoma qismlari" in tu and "Shartnoma boʻyicha jami" in tu and "1-qism" in tu
       and u["parts"]["totals"]["premium"] == e["parts"]["totals"]["premium"], tu[:200])
    ok("по-английски: «Contract parts», «Part 2: class 14», средняя «not used to check the minimum»",
       "Contract parts" in te and "Part 2: class 14" in te and "not used to check the minimum" in te)
    ok("ни одного незаполненного шаблона {…} ни на одном языке",
       not any(re.search(r"\{[a-z_]+\}", x) for x in (all_text(u), te, all_text(call("GET", f"/act/{aid}")[1]))))
    ok("русских слов в английском акте частей нет (кроме наименований объектов)",
       not re.search(r"[А-Яа-я]{4,}", "\n".join(li["title"] for s in e["sections"] for li in s.get("lists") or [])),
       [li["title"] for s in e["sections"] for li in s.get("lists") or [] if re.search(r"[А-Яа-я]{4,}", li["title"])][:5])
    for lang, word in (("ru", "Ставки по частям договора"), ("uz", "Shartnoma qismlari"),
                       ("en", "Rates by contract part")):
        dx, pd = docx_plain(aid, lang), pdf_plain(aid, lang)
        a = call("GET", f"/act/{aid}", params={"lang": lang})[1]
        prem = flat(act.money(a["premium"]["amount"], lang))
        ok(f"Word и PDF ({lang}): ставки по частям и премия договора {prem}",
           word in dx and prem in dx and word in pd and prem in pd, (word in dx, prem in dx, word in pd, prem in pd))
    # однопродуктовые акты: блок parts — single, остальное как прежде
    for code, must, opt in (("0318", CRANE_MUST, CRANE_OPT), ("0807", WH8_MUST, WH8_OPT),
                            ("0832", {"product_code": "0832", "sum_insured": 5e9, "object_value": 5e9,
                                      "region": "Ташкент"}, {"losses_3y": {"count": 0}})):
        st, a = call("POST", "/act/make", {"lang": "ru", "must": must, "optional": opt})
        r = a["rate"]
        ok(f"{code}: один класс — parts.mode single, ставка и премия по-прежнему, без частей в тексте",
           st == 200 and a["parts"]["mode"] == "single" and not a["parts"]["items"] and "suggested_parts" not in a
           and r["mode"] == "tariff" and not r["multi_class"] and "reference_pct" not in r
           and a["premium"]["amount"] == round(premium_of(r["applied_pct"], must["sum_insured"], 365))
           and a["analytics"]["available"] and "Части договора" not in all_text(a)
           and not any(x["label"].startswith("Часть ") for s in a["sections"] for x in s["rows"]),
           (st, a.get("parts"), r.get("mode")))


def check_review_0930():
    print("42г. Замечания контролёра 30.09.2026: кредит, лимиты сценариев, проверка шаблона, части, минимум класса")
    from app import class_templates as ctm
    from app import act_extras as axm
    fresh()
    model_on(False)
    data = ctm.load_file()
    good14 = _json.loads(_json.dumps(data["classes"]["14"]))
    good2 = _json.loads(_json.dumps(data["classes"]["2"]))
    # ---------- 1. кредит: параметры правила ----------
    bad = _json.loads(_json.dumps(good14))
    bad["scenario_rule"]["params"]["max_share_of_loan"] = 0.6
    e1 = ctm.validate(bad, "14")
    bad["scenario_rule"]["params"]["max_share_of_loan"] = "0.5"
    e2 = ctm.validate(bad, "14")
    ok("шаблон 14: доля кредита 0,6 — ошибка (правило № 6, не более 50 %); «0.5» строкой — «нужно число»",
       any("max_share_of_loan" in x and "не больше 0,5" in x for x in e1)
       and any("max_share_of_loan" in x and "нужно число" in x for x in e2), (e1, e2))
    b2 = _json.loads(_json.dumps(good2))
    b2["scenario_rule"]["params"] = {"epidemic_share": 1.5, "months": -3, "x": None}
    e3 = " | ".join(ctm.validate(b2, "2"))
    ok("шаблон 2: доля эпидемии 1,5 — вне (0; 1], отрицательный параметр и null — ошибки",
       "epidemic_share: доля больше 0 и не больше 1" in e3 and "months: число больше нуля" in e3
       and "x: нужно число" in e3, e3)
    ok("проверка параметров — чистая функция: 0,5 у кредита и 0,3 у эпидемии проходят",
       ctm.check_params("credit", {"max_share_of_loan": 0.5}) == [] and ctm.check_params("frequency", {"epidemic_share": 0.3}) == []
       and ctm.check_params("credit", {"max_share_of_loan": 0}) != [])
    # ---------- 1. кредит: чистая функция проверки ----------
    ok("банк по названию: «АКБ Хамкорбанк», «Xalq banki», «Kapitalbank ATB» — банк; «ООО Ромашка» — нет",
       ae.is_bank("АКБ «Хамкорбанк»") and ae.is_bank("Xalq banki") and ae.is_bank("Kapitalbank ATB")
       and ae.is_bank("ООО «Ромашка»") is False and ae.is_bank(None) is None)
    c0 = ae.credit_check(40e6, {}, 0.5, None)
    c1 = ae.credit_check(50e6, {"credit_amount": 100e6, "collateral_value": 60e6}, 0.5,
                         {"kind": "legal", "name": "АКБ «Хамкорбанк»", "source": "contract"})
    c2 = ae.credit_check(40e6, {"credit_amount": 100e6, "collateral_value": 60e6}, 0.5,
                         {"kind": "legal", "name": "ООО «Ромашка»", "source": "request"})
    ok("кредит без суммы и залога: «введите сумму кредита и обеспечение» + страхователь неизвестен",
       [c["code"] for c in c0] == ["credit_need_data", "credit_holder_unknown"], c0)
    ok("кредит 100 млн, залог 60 млн, сумма 50 млн, страхователь банк: превышение 10 млн, уменьшить до 40 млн",
       c1 == [{"code": "credit_over", "params": {"credit": 100_000_000, "collateral": 60_000_000,
                                                 "insurable": 40_000_000, "by": "unsecured", "sum": 50_000_000,
                                                 "excess": 10_000_000, "share_pct": 50}}], c1)
    ok("сумма 40 млн в пределах, страхователь ООО — только проверка страхователя",
       [c["code"] for c in c2] == ["credit_holder_not_bank"] and c2[0]["params"]["holder"] == "ООО «Ромашка»", c2)
    # ---------- 1. кредит в акте ----------
    st, a = _cls_act("14", None, S=40_000_000)
    chk = [flat(c) for c in a["decision"]["checks"]]
    ok("акт класса 14 без суммы кредита и залога: проверка андеррайтеру, рекомендация не выше «с оговорками»",
       st == 200 and any(c.startswith("Кредит: проверить, что страховая сумма не превышает необеспеченную часть и "
                                      "50 % суммы кредита — введите сумму кредита и обеспечение") for c in chk)
       and a["decision"]["code"] in ("accept_with_clauses", "decline"), (st, chk, a.get("decision", {}).get("code")))
    ok("страхователь неизвестен — пункт проверки «страхователь и плательщик премии — банк-кредитор»",
       any("страхователь и плательщик премии — банк-кредитор" in c and "не найден" in c for c in chk), chk)
    st, a = _cls_act("14", {"credit_amount": 100_000_000, "collateral_value": 60_000_000}, S=50_000_000)
    chk = [flat(c) for c in a["decision"]["checks"]]
    ok("кредит 100 млн, залог 60 млн, сумма 50 млн: «страховая сумма … выше допустимой … — уменьшить до 40 000 000 сум»",
       any("выше допустимой" in c and "уменьшить до 40 000 000 сум" in c and "превышение 10 000 000 сум" in c
           for c in chk) and a["decision"]["code"] != "accept", chk)
    ct_bad = {"sum_insured": 40_000_000, "premium": 240_000, "policyholder": "ООО «Ромашка»"}
    st, a = _cls_act("14", {"credit_amount": 100_000_000, "collateral_value": 60_000_000}, S=40_000_000,
                     optional={"contract": ct_bad})
    chk = [flat(c) for c in a["decision"]["checks"]]
    ok("страхователь в договоре — ООО, не банк: «в документе (договор страхования) страхователь — ООО «Ромашка»»",
       st == 200 and any("страхователь и плательщик премии — банк-кредитор" in c and "ООО «Ромашка»" in c
                         and "договор страхования" in c for c in chk) and a["decision"]["code"] != "accept", (st, chk))
    st, a = _cls_act("14", {"credit_amount": 100_000_000, "collateral_value": 60_000_000}, S=40_000_000,
                     optional={"contract": dict(ct_bad, policyholder="АКБ «Хамкорбанк»")})
    chk = [flat(c) for c in a["decision"]["checks"]]
    ok("страхователь — банк, сумма в пределах: кредитных проверок нет",
       st == 200 and not any("банк-кредитор" in c or "Кредит:" in c for c in chk), chk)
    st, a = _cls_act("14", {"credit_amount": 100_000_000, "collateral_value": 60_000_000}, S=50_000_000, lang="en",
                     optional={"contract": ct_bad})
    ok("кредитные проверки на английском: «reduce to 40,000,000 UZS», «the lending bank»",
       any("reduce to" in flat(c) for c in a["decision"]["checks"])
       and any("lending bank" in c and "ООО" in c for c in a["decision"]["checks"]), a["decision"]["checks"])
    # ---------- 2. лимиты ответственности не выше страховой суммы ----------
    r = axm.simple_scenarios("limit", 100, 100, {"limit_per_case": 300, "limit_aggregate": 500})
    r2 = axm.simple_scenarios("full_limit", 100, 100, {"limit_per_case": 300})
    ok("limit: лимит на случай 300 и годовой 500 при сумме 100 → PML = EML = MFL = 100, две пометки",
       [r[s]["amount"] for s in ("PML", "EML", "MFL")] == [100, 100, 100]
       and [x["code"] for x in r["assumptions"]] == ["as_tpl_limit_case_over", "as_tpl_limit_aggregate_over"], r)
    ok("full_limit: лимит 300 при сумме 100 → всё 100, пометка «лимит выше страховой суммы»",
       [r2[s]["amount"] for s in ("PML", "EML", "MFL")] == [100, 100, 100]
       and r2["assumptions"] == [{"code": "as_tpl_limit_case_over", "params": {"limit": 300, "sum": 100}}], r2)
    st, a = _cls_act("13", {"activity_kind": "trade", "limit_per_case": 300_000_000, "limit_aggregate": 1_000_000_000},
                     S=200_000_000)
    sc = a["scenarios"]
    asm = " ".join(flat(x["text"]) for x in sc["assumptions"])
    ok("класс 13, сумма 200 млн, лимиты 300 млн и 1 млрд: PML/EML/MFL = 200 млн, в допущениях — «взята страховая сумма»",
       [sc[k]["amount"] for k in ("pml", "eml", "mfl")] == [200_000_000] * 3
       and "лимит на один случай 300 000 000 сум выше страховой суммы 200 000 000 сум — взята страховая сумма" in asm
       and "годовой лимит 1 000 000 000 сум выше страховой суммы" in asm, (sc, asm))
    # ---------- 4. проверка шаблона: размер, подписи, переводы ----------
    errs_file = {c: ctm.validate(t_, c) for c, t_ in data["classes"].items()}
    ok("поставленный docs/act_class_templates.json проходит новую проверку (все 27 шаблонов, переводы uz/en)",
       not any(errs_file.values()), {k: v for k, v in errs_file.items() if v})
    b = _json.loads(_json.dumps(good14))
    b["must"][0]["label"].pop("uz")
    b["optional"][0]["label"]["en"] = ""
    b["risks"]["items"][0]["label"].pop("en")
    b["name"].pop("uz")
    b["notes"][0]["text"].pop("ru")
    b["documents"]["items"][0]["ru"] = "х" * 501
    e = " | ".join(ctm.validate(b, "14"))
    ok("шаблон без переводов: 422-перечень — name.uz, must.credit_amount.uz, optional.borrower_industry.en, "
       "risks.cr_insolvency.en; подпись без ru; подпись длиннее 500",
       "нет перевода uz/en" in e and "name.uz" in e and "must.credit_amount.uz" in e
       and "optional.borrower_industry.en" in e and "risks.cr_insolvency.en" in e
       and "подпись без ru: notes.credit_rule.text" in e and "длиннее 500 знаков" in e, e)
    big = _json.loads(_json.dumps(good14))
    big["notes"] = [{"code": f"n{i}", "text": {"ru": "я" * 400, "uz": "a" * 400, "en": "a" * 400}} for i in range(200)]
    e = ctm.validate(big, "14")
    ok("шаблон больше 200 КБ — ошибка размера", len(e) == 1 and "больше 200 КБ" in e[0], e)
    HEADERS.append(_admin_header("tpl_review_admin"))
    try:
        bad = _json.loads(_json.dumps(good14))
        bad["scenario_rule"]["params"]["max_share_of_loan"] = 0.7
        bad["must"][1]["label"].pop("en")
        st, e = call("PUT", "/act/templates/14", {"template": bad})
        errs = " | ".join(e.get("errors") or [])
        ok("PUT шаблона 14 с долей 0,7 и без en у поля — 422 с перечнем",
           st == 422 and "max_share_of_loan" in errs and "must.collateral_value.en" in errs, (st, errs))
        st, e = call("PUT", "/act/templates/14", {"template": _json.loads(_json.dumps(good14))})
        ok("PUT хорошего шаблона 14 проходит (новая версия)", st == 200, (st, e))
    finally:
        HEADERS.clear()
    # ---------- мелочи: пометки «сверх приложения А», убытки за 5 лет, единственный вид объекта ----------
    C = data["classes"]
    fld = lambda c, code: next(f for f in C[c]["must"] + C[c]["optional"] if f["code"] == code)
    exp_ok = lambda x: x.get("expert") is True and x["note"]["ru"] == "сверх приложения А, экспертно"
    ok("сверх приложения А — expert: true: факторы 13 liab_limit/liab_turnover, MFL класса 10, поля сценариев 1, 2, 7, 16, 17",
       all(exp_ok(f) for f in C["13"]["factors"] if f["code"] in ("liab_limit", "liab_turnover"))
       and exp_ok(C["10"]["scenario_rule"]["expert_scenarios"]["MFL"])
       and all(exp_ok(fld(c, k)) for c, k in (("1", "people_in_one_place"), ("2", "avg_visits"), ("2", "avg_bill"),
                                              ("7", "limit_per_shipment"), ("7", "accumulation_value"),
                                              ("16", "monthly_loss"), ("17", "limit_per_dispute")))
       and not fld("1", "insured_count").get("expert"))
    ok("классы 5, 6, 11, 12: убытки за 5 лет — поле losses_3y (так понимает акт), подпись «за 5 лет (… история убытков)»",
       all(fld(c, "losses_3y")["type"] == "losses" and "за 5 лет (в расчёте используется как история убытков)"
           in fld(c, "losses_3y")["label"]["ru"] and fld(c, "losses_3y")["period_years"] == 5
           and not any(f["code"] == "losses_5y" for f in C[c]["optional"]) for c in ("5", "6", "11", "12")))
    st, t14 = call("GET", "/act/templates/14", params={"lang": "ru"})
    st3, t3 = call("GET", "/act/templates/3", params={"lang": "ru"})
    st_, lst = call("GET", "/act/templates", params={"lang": "ru"})
    one = {x["class_code"]: x for x in lst["templates"]}
    ok("GET /act/templates/14: один вид объекта «кредит» — single_kind: true, default_kind: loan; у класса 3 — false",
       t14["template"]["object"]["single_kind"] is True and t14["template"]["object"]["default_kind"] == "loan"
       and t3["template"]["object"]["single_kind"] is False and one["14"]["single_kind"] and not one["3"]["single_kind"],
       (t14["template"]["object"], t3["template"]["object"].get("single_kind")))
    # ---------- 5, 6. части: недостающие поля частей 2+, минимум класса из текста тарифа ----------
    cr_no_term = dict(PT_CREDIT, fields={"class_fields": {"credit_amount": 100_000_000, "collateral_value": 60_000_000}})
    st, p = _pt_make(PT_MUST, dict(PT_OPT, parts=[PT_CAR, cr_no_term]))
    chk = [flat(c) for c in p["decision"]["checks"]]
    ok("часть 2 (класс 14) без срока кредита: «Часть 2 (класс 14): уточнить срок кредита, месяцев» в решении",
       st == 200 and "Часть 2 (класс 14): уточнить срок кредита, месяцев" in chk, chk)
    ok("у части 2 класса 14 — вид объекта по умолчанию «loan» (единственный), в расчёт не идёт",
       p["parts"]["items"][1]["object_kind_default"] == "loan" and p["parts"]["items"][1]["object_kind"] is None)
    ok("часть 2 кредита: страхователь не найден — проверка по части, сумма 40 млн в пределах — превышения нет",
       any(c.startswith("Часть 2 (класс 14): проверить, что страхователь и плательщик премии — банк-кредитор")
           for c in chk) and not any("выше допустимой" in c for c in chk), chk)
    st, p = _pt_make(PT_MUST, dict(PT_OPT, parts=[PT_CAR, {"class_code": "14", "sum_insured": 40_000_000}]))
    chk = [flat(c) for c in p["decision"]["checks"]]
    ok("часть 2 кредита без полей: «Часть 2 (класс 14). Кредит: … введите сумму кредита и обеспечение» и перечень полей",
       any(c.startswith("Часть 2 (класс 14). Кредит: проверить, что страховая сумма не превышает") for c in chk)
       and any(c.startswith("Часть 2 (класс 14): уточнить сумма кредита, стоимость обеспечения") for c in chk), chk)
    how = {}
    for lg in ("ru", "uz", "en"):
        st, x = _pt_make(PT_MUST, dict(PT_OPT, parts=[PT_CAR, PT_CREDIT]), lang=lg)
        how[lg] = (x["parts"]["items"][1]["rate"]["how"], x["parts"]["items"][0]["rate"]["how"], all_text(x))
    ok("часть 2 (класс 14): «минимум класса 14 по тарифной политике (из текста тарифа продукта 0312)», не «ставка продукта»",
       any("минимум класса 14 по тарифной политике (из текста тарифа продукта 0312)" in flat(h) for h in how["ru"][0])
       and not any("ставка продукта" in h or "минимальной ставки продукта" in h for h in how["ru"][0])
       and any("ставка продукта 0312" in h for h in how["ru"][1]), how["ru"][:2])
    ok("то же на узбекском и английском",
       any("14-klass minimumi (0312 mahsuloti tarif matnidan)" in flat(h) for h in how["uz"][0])
       and any("class 14 minimum under the tariff policy" in h and "product 0312" in h for h in how["en"][0]),
       (how["uz"][0], how["en"][0]))
    ok("аналитика части 2: строка «Минимум класса 14 по тарифной политике (из текста тарифа продукта 0312)»",
       "Минимум класса 14 по тарифной политике (из текста тарифа продукта 0312)" in how["ru"][2]
       and "Class 14 minimum under the tariff policy (from the tariff text of product 0312)" in how["en"][2])
    ok("ни одного незаполненного шаблона {…} в актах с кредитными проверками",
       not any(re.search(r"\{[a-z_]+\}", v[2]) for v in how.values()))


def _old_classes(path) -> None:
    """Справочник классов как до шаблонов 1.2.0: учётная группа NOT NULL, класса 18 нет."""
    import sqlite3
    con = sqlite3.connect(str(path), isolation_level=None)
    con.execute("PRAGMA foreign_keys = OFF")
    con.execute("BEGIN")
    con.execute("DELETE FROM classes WHERE group_code IS NULL")
    con.execute("CREATE TABLE classes_old (code TEXT PRIMARY KEY, name TEXT NOT NULL, "
                "group_code TEXT NOT NULL REFERENCES groups(code), branch TEXT NOT NULL, kind TEXT NOT NULL)")
    con.execute("INSERT INTO classes_old SELECT code, name, group_code, branch, kind FROM classes")
    con.execute("DROP TABLE classes")
    con.execute("ALTER TABLE classes_old RENAME TO classes")
    con.execute("COMMIT")
    con.close()


def _life_rows(path) -> tuple:
    """(строки L* [(code, group_code, branch, kind)] — должно быть пусто, group_code допускает NULL, нарушений ссылок)."""
    import sqlite3
    con = sqlite3.connect(str(path))
    try:
        rows = [tuple(r) for r in con.execute("SELECT code, group_code, branch, kind FROM classes "
                                              "WHERE code LIKE 'L%' ORDER BY code")]
        nullable = not [r for r in con.execute("PRAGMA table_info(classes)") if r[1] == "group_code"][0][3]
        fk = len(con.execute("PRAGMA foreign_key_check").fetchall())
    finally:
        con.close()
    return rows, nullable, fk


LIFE_ROWS = []                                   # классов жизни refsync и db_build в classes не заводят
ROW_18 = ("18", None, "общее", "личное")         # класс 18 — общее страхование, учётной группы в Положении 1882 нет


def _row_18(path):
    import sqlite3
    con = sqlite3.connect(str(path))
    try:
        r = con.execute("SELECT code, group_code, branch, kind FROM classes WHERE code='18'").fetchone()
    finally:
        con.close()
    return tuple(r) if r else None


def check_templates_sync():
    print("41г. Сервер: refsync доводит шаблоны 1.4.2 и класс 18 (старая база), классов жизни не заводит; новая "
          "версия файла; db_build на копии")
    import importlib
    from app import class_templates as ctm, refsync
    folder = Path(tempfile.mkdtemp(prefix="surveyor-tpl-"))
    try:
        disk = folder / "disk.db"
        db.snapshot(db.DB_PATH, disk)
        import sqlite3
        con = sqlite3.connect(str(disk))
        con.execute("DROP TABLE IF EXISTS class_templates")
        con.commit()
        con.close()
        _old_classes(disk)                     # база сервера до шаблонов 1.2.0
        ctm.reset_cache()
        res = refsync.sync_templates(disk)
        con = sqlite3.connect(str(disk))
        n = con.execute("SELECT COUNT(*), COUNT(DISTINCT class_code) FROM class_templates").fetchone()
        ok("база без таблицы: refsync.sync_templates создал таблицу и довёл 20 шаблонов версии 1.4.2 (с классом 18)",
           res["status"] == "обновлено" and n == (20, 20) and res["version"] == "1.4.2"
           and "18" in (res.get("added") or []), (res.get("status"), n))
        life, nullable, fk = _life_rows(disk)
        ok("старая база (учётная группа NOT NULL): refsync снял NOT NULL и добавил только класс 18 — branch «общее», "
           "kind «личное», учётная группа NULL; строк L* нет, ссылки целы",
           life == LIFE_ROWS and nullable and fk == 0 and _row_18(disk) == ROW_18
           and res["classes_added"] == ["18"],
           (life, nullable, fk, _row_18(disk), res.get("classes_added")))
        ok("классы общего страхования не тронуты: 19 строк с учётной группой (1–17, 13з, 16у)",
           con.execute("SELECT COUNT(*) FROM classes WHERE branch='общее' AND group_code IS NOT NULL").fetchone()[0] == 19)
        res2 = refsync.sync_templates(disk)
        ok("повторный запуск — «актуально», строк не прибавилось",
           res2["status"] == "актуально" and con.execute("SELECT COUNT(*) FROM class_templates").fetchone()[0] == 20
           and con.execute("SELECT COUNT(*) FROM classes WHERE code LIKE 'L%'").fetchone()[0] == 0
           and con.execute("SELECT COUNT(*) FROM classes WHERE code='18'").fetchone()[0] == 1)
        # правка администратора 1.1 на «сервере», потом образ приносит файл 2.0
        con.execute("INSERT INTO class_templates (class_code, version, json, source, file_version, updated_at, updated_by,"
                    " calibrated) SELECT class_code, '1.1', json, 'admin', '1.0', '2026-09-30T12:00:00', 'админ', 0 "
                    "FROM class_templates WHERE class_code='13'")
        con.commit()
        src = _json.loads(ctm.TEMPLATES_FILE.read_text(encoding="utf-8"))
        src["version"] = "2.0"
        newer = folder / "tpl.json"
        newer.write_text(_json.dumps(src, ensure_ascii=False), encoding="utf-8")
        orig = ctm.TEMPLATES_FILE
        ctm.TEMPLATES_FILE = newer
        try:
            res3 = refsync.sync_templates(disk)
        finally:
            ctm.TEMPLATES_FILE = orig
            ctm.reset_cache()
            ctm._file_cache.update(mtime=None, data=None)
        hist = [tuple(r) for r in con.execute("SELECT version, source FROM class_templates WHERE class_code='13' ORDER BY id")]
        ok("файл 2.0 новее — добавлен всем 20 шаблонам; история класса 13: 1.4.2 файл, 1.1 админ, 2.0 файл",
           len(res3["added"]) == 20 and hist == [("1.4.2", "file"), ("1.1", "admin"), ("2.0", "file")], (res3, hist))
        con.close()
        # образ собран до 1.2.0 (в classes нет класса 18): обновление справочников из образа его не теряет
        image = folder / "image.db"
        db.snapshot(db.DB_PATH, image)
        _old_classes(image)
        con = sqlite3.connect(str(disk))
        con.execute("DELETE FROM app_settings WHERE key=?", (refsync.HASH_KEY,))
        con.commit()
        con.close()
        saved_path = db.DB_PATH
        db.DB_PATH = disk
        try:
            res4 = refsync.sync_on_start(image)
        finally:
            db.DB_PATH = saved_path
            ctm.reset_cache()
        life, nullable, fk = _life_rows(disk)
        ok("образ без класса 18: справочники обновлены из образа, класс 18 на диске остался (доведён заново), "
           "строк L* нет",
           res4["status"] == "обновлено" and life == LIFE_ROWS and _row_18(disk) == ROW_18 and nullable and fk == 0,
           (res4.get("status"), life, _row_18(disk)))
        # tools/db_build.py — заполнение из JSON на копии (рабочая база не открывается), база тоже «старая»
        build_copy = folder / "build.db"
        db.snapshot(db.DB_PATH, build_copy)
        con = sqlite3.connect(str(build_copy))
        con.execute("DROP TABLE IF EXISTS class_templates")
        con.commit()
        con.close()
        _old_classes(build_copy)
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        dbb = importlib.import_module("db_build")
        saved = dbb.DB
        dbb.DB = build_copy
        buf = io.StringIO()
        try:
            import contextlib
            with contextlib.redirect_stdout(buf):
                dbb.main()
        finally:
            dbb.DB = saved
            ctm.reset_cache()
        con = sqlite3.connect(str(build_copy))
        nb = con.execute("SELECT COUNT(*) FROM class_templates").fetchone()[0]
        con.close()
        life, nullable, fk = _life_rows(build_copy)
        ok("tools/db_build.py на копии: class_templates — 20 строк из JSON, класс 18 (группа NULL), классов жизни нет",
           nb == 20 and "шаблонов классов добавлено: 20" in buf.getvalue()
           and "жизни" not in buf.getvalue()
           and "классов общего страхования без учётной группы добавлено: 18" in buf.getvalue()
           and life == LIFE_ROWS and _row_18(build_copy) == ROW_18 and nullable and fk == 0,
           (nb, life, _row_18(build_copy), buf.getvalue()[-400:]))
    finally:
        ctm.reset_cache()
        shutil.rmtree(folder, ignore_errors=True)


# ------------------------------------------------------------------ 43. страховой скоринг объекта (01.10.2026)

SC_REPORT = {}
CYR = re.compile(r"[А-Яа-яЁё]")


def _sc_an(score, comps):
    """Аналитика акта с баллом риска (форма act_analytics.score) — для чистой функции."""
    return {"available": True, "score": {"available": True, "score": score, "components": comps}}


def _sc_comp(code, points, weight, applicable=True):
    return {"code": code, "name_ru": code, "points": points, "weight": weight, "applicable": applicable,
            "contribution": round(points * weight, 1)}


def check_scoring_engine():
    print("43а. Страховой скоринг: чистая функция act_engine.insurance_score, границы секторов и подклассы")
    edges = {0: "E3", 33: "E3", 34: "E2", 66: "E2", 67: "E1", 99: "E1", 100: "D3", 133: "D3", 134: "D2",
             167: "D1", 199: "D1", 200: "C3", 299: "C1", 300: "B3", 333: "B3", 334: "B2", 366: "B2", 367: "B1",
             399: "B1", 400: "A3", 433: "A3", 434: "A2", 467: "A2", 468: "A1", 500: "A1", -5: "E3", 640: "A1"}
    got = {k: ae.score_band(k)["class_code"] for k in edges}
    ok("границы секторов: E 0–99, D 100–199, C 200–299, B 300–399, A 400–500; подкласс 1 — верхняя треть",
       got == edges, {k: (got[k], v) for k, v in edges.items() if got[k] != v})
    labels = {c: ae.score_band(v)["class_label"] for c, v in (("E", 10), ("D", 150), ("C", 250), ("B", 350), ("A", 450))}
    ok("подписи классов: плохой, слабый, средний, хороший, отличный",
       labels == {"E": "плохой", "D": "слабый", "C": "средний", "B": "хороший", "A": "отличный"}, labels)
    # автокран из проверки 40а: балл риска 32,7 = 20,5 + 0 + 0 + 1,1 + 11,1 → 500 − 5 × 32,7 = 336,5 → 337
    comps = [_sc_comp("rate", 73.7, 0.278), _sc_comp("mfl_retention", 0.0, 0.278), _sc_comp("losses", 0, 0.222),
             _sc_comp("insurance_to_value", 10.0, 0.111), _sc_comp("seismic", 0, 0.0, False),
             _sc_comp("external_stats", 100.0, 0.111)]
    S = ae.insurance_score({"analytics": _sc_an(32.7, comps), "risk": {"level": "moderate"}})
    ok("балл риска 32,7 → 500 − 5 × 32,7 = 336,5 → 337 (половина — вверх), класс B2 «хороший», версия 1.0",
       S["score"] == 337 and S["class"] == "B" and S["sub"] == 2 and S["class_label"] == "хороший"
       and S["version"] == "1.0" and S["basis"] == "risk_score" and S["risk_score_100"] == 32.7, S)
    on = [c for c in S["components"] if c["applicable"]]
    ok("составляющие в шкале 0–500: сумма баллов = балл, сумма «из» = 500, неучтённая — 0 из 0",
       sum(c["points"] for c in S["components"]) == 337 and sum(c["max"] for c in S["components"]) == 500
       and next(c for c in S["components"] if c["code"] == "seismic")["max"] == 0
       and all(0 <= c["points"] <= c["max"] for c in on), [(c["code"], c["points"], c["max"]) for c in S["components"]])
    rate_c = next(c for c in S["components"] if c["code"] == "rate")
    ok("составляющая «ставка»: вес 0,278 × (100 − 73,7) × 5 ≈ 37 из 0,278 × 500 ≈ 139",
       rate_c["points"] in (36, 37) and rate_c["max"] in (139, 140) and "73,7" in rate_c["why"], rate_c)
    ok("всё помечено экспертным: calibrated = 0, «не кредитный скоринг и не оценка КАТМ»",
       S["calibrated"] == 0 and all(c["calibrated"] == 0 for c in S["components"])
       and "не кредитный скоринг" in S["note"] and "КАТМ" in S["note"])
    ok("шкала: min 0, max 500, пять секторов E…A",
       S["scale"]["min"] == 0 and S["scale"]["max"] == 500
       and [b["code"] for b in S["scale"]["bands"]] == ["E", "D", "C", "B", "A"]
       and [(b["from"], b["to"]) for b in S["scale"]["bands"]] == [(0, 99), (100, 199), (200, 299), (300, 399),
                                                                   (400, 500)])
    # нет балла риска: по уровню риска акта
    lv = {lvl: ae.insurance_score({"risk": {"level": lvl}, "analytics": {"available": False, "reason": "no_engine"}})
          for lvl in ("low", "moderate", "high")}
    ok("класс без аналитики: низкий 430 (A3), умеренный 300 (B3), высокий 130 (D3), basis = level",
       [(lv[k]["score"], lv[k]["class_code"], lv[k]["basis"]) for k in ("low", "moderate", "high")]
       == [(430, "A3", "level"), (300, "B3", "level"), (130, "D3", "level")]
       and lv["high"]["components"][0]["code"] == "act_level" and lv["high"]["components"][0]["points"] == 130
       and "уровню риска" in lv["high"]["method_text"], [(v["score"], v["class_code"]) for v in lv.values()])
    old = ae.insurance_score({"risk": {"level": "low"}})
    ok("старый акт без блока analytics — тоже по уровню риска", old["score"] == 430 and old["basis"] == "level")
    # договор из частей: балл договора — по самой опасной части (наименьший балл)
    parts = {"mode": "multi", "items": [
        {"index": 1, "class_code": "3", "level": "low", "analytics": _sc_an(42.6, [_sc_comp("rate", 42.6, 1.0)])},
        {"index": 2, "class_code": "14", "level": "low", "analytics": _sc_an(35.8, [_sc_comp("rate", 35.8, 1.0)])},
        {"index": 3, "class_code": "13", "level": "high", "analytics": {"available": False}}]}
    M = ae.insurance_score({"parts": parts, "risk": {"level": "high"}})
    ok("договор из частей: части 287 (C1), 321 (B3), 130 (по уровню) — балл договора 130 по части 3",
       [p["score"] for p in M["parts"]] == [287, 321, 130] and M["score"] == 130 and M["worst_part"] == 3
       and M["contract"] and M["parts"][2]["basis"] == "level" and [p["score_class"] for p in M["parts"]]
       == ["C1", "B3", "D3"], M["parts"])
    # проверки по отчёту кредитного бюро — чистая функция
    today = date(2026, 10, 1)
    f_bad = {"score": 150, "score_class": "D2", "report_date": "2026-08-01", "active": {"overdue": 5_000_000}}
    ck = ae.borrower_checks(f_bad, True, None, today)
    ok("заёмщик: класс D2, просрочка 5 млн, отчёт 61 день → три проверки (класс, просрочка, устарел)",
       [c["code"] for c in ck] == ["borrower_low_class", "borrower_overdue", "borrower_stale"]
       and ck[2]["params"] == {"days": 61, "max": 30}, ck)
    f_ok = {"score": 420, "score_class": "A1", "report_date": "2026-09-25", "active": {"overdue": 0}}
    ok("заёмщик: класс A1, без просрочки, отчёт 6 дней — проверок нет",
       ae.borrower_checks(f_ok, True, None, today) == [])
    ok("класс C — проверка (порог «C и ниже»); класс B — нет; порог D — у C проверки нет",
       [c["code"] for c in ae.borrower_checks(dict(f_ok, score_class="C1"), True, None, today)] == ["borrower_low_class"]
       and ae.borrower_checks(dict(f_ok, score_class="B3"), True, None, today) == []
       and ae.borrower_checks(dict(f_ok, score_class="C1"), True, {"low_class": "D"}, today) == [])
    ok("класс кириллицей «С2» узнаётся; нет ни балла, ни класса — borrower_no_score; нет даты — устарел",
       ae.bureau_letter("С2") == "C" and [c["code"] for c in ae.borrower_checks(
           {"active": {}, "report_date": None}, True, None, today)] == ["borrower_no_score", "borrower_stale"])
    ok("не кредитный класс — проверок нет", ae.borrower_checks(f_bad, False, None, today) == [])
    errs = ae.check_settings({"credit_report": {"low_class": "X", "max_age_days": 0}, "scoring": {"brand_color": "blue"}})
    ok("настройки скоринга и отчёта бюро проверяются (цвет #RRGGBB, класс A–E, дни 1–365)",
       any("low_class" in e for e in errs) and any("max_age_days" in e for e in errs)
       and any("brand_color" in e for e in errs) and not ae.check_settings(
           {"credit_report": {"low_class": "D", "max_age_days": 45}, "scoring": {"brand_color": "#1D2C8F"}}), errs)


def _sc_expect(a):
    """Балл скоринга из балла риска аналитики акта: round(500 − 5 × балл), половина — вверх."""
    r = a["analytics"]["score"]["score"]
    return ae.round_half_up(round(500 - 5 * r, 6)), r


def _sc_common(tag, a):
    sc = a.get("scoring") or {}
    ok(f"{tag}: блок scoring есть, calibrated = 0, версия 1.0", sc.get("available") and sc["calibrated"] == 0
       and sc["version"] == "1.0", sc.get("reason"))
    bnd = ae.score_band(sc["score"], (a.get("analytics") or {}).get("score", {}).get("bounds"), sc["risk_score_100"])
    ok(f"{tag}: класс и подкласс — по сектору балла риска (порогам аналитики)", (sc["class"], sc["sub"]) ==
       (bnd["class"], bnd["sub"]) and sc["class_code"] == f"{sc['class']}{sc['sub']}", (sc["score"], sc["class_code"]))
    ok(f"{tag}: составляющие объясняют балл (сумма = балл, «из» — 500)",
       sum(c["points"] for c in sc["components"]) == sc["score"]
       and sum(c["max"] for c in sc["components"]) == 500 and all(c["why"] for c in sc["components"]),
       [(c["code"], c["points"], c["max"]) for c in sc["components"]])
    rq = sc["request"]
    ok(f"{tag}: шапка — тип отчёта, номер акта, дата и время, страховщик, продукт и класс",
       rq["type"] == "Страховой скоринг объекта" and rq["number"] == a["number"] and rq["date"] == a["date"]
       and re.fullmatch(r"\d{2}:\d{2}", rq["time"]) and [r["code"] for r in rq["rows"]] ==
       ["type", "number", "datetime"] + (["by"] if rq["insurer_known"] else []) + ["product", "class"], rq)
    codes = [o["code"] for o in sc["overview"]]
    ok(f"{tag}: общий обзор — 17 пар «значение — подпись»", codes == list(asc_codes()) and all(
        o["value"] not in (None, "") and o["label"] for o in sc["overview"]), sc["overview"])
    ov = {o["code"]: o for o in sc["overview"]}
    ok(f"{tag}: обзор совпадает с актом (сумма, премия, срок, проверок)",
       ov["sum"]["value"] == a["sections"][2]["rows"][0]["value"] and ov["premium"]["raw"] == a["premium"]["amount"]
       and ov["checks"]["raw"] == len(a["decision"]["checks"]) and ov["franchise"]["value"] == a["franchise"]["text"],
       ov)
    sub = {r["code"]: r for r in sc["subject"]["rows"]}
    same_kind = str(sc["subject"]["kind"] or "").lower() == str(sc["subject"]["name"] or "").lower()
    ok(f"{tag}: объект — наименование, вид (если не совпадает с наименованием), страхователь, регион, "
       f"идентификаторы, источник данных",
       [k for k in sub if k != "note"] == ["name"] + ([] if same_kind else ["kind"]) +
       ["policyholder", "region", "identifiers", "source"] and all(r["value"] for r in sub.values()), sub)
    ok(f"{tag}: первые 5 проверок андеррайтеру, тексты и пометки", sc["checks"] == a["decision"]["checks"][:5]
       and "не кредитный скоринг" in sc["note"] and "не является кредитным скорингом" in sc["footer_line"]
       and sc["downloads"]["png"].startswith(f"/act/{a['id']}/scoring.png"), sc["note"])
    return sc


def asc_codes():
    from app import act_scoring
    return act_scoring.OVERVIEW_CODES


def check_scoring_make():
    print("43б. Скоринг в /act/make: автокран, склад 4,2 млрд, оборудование, договор из частей, класс без аналитики")
    fresh()
    model_on(False)
    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                       "optional": dict(CRANE_OPT, object_kind="truck_crane")})
    sc = _sc_common("автокран", a)
    exp, r = _sc_expect(a)
    ok("автокран: балл = 500 − 5 × балл риска (сверка с analytics.score)", sc["score"] == exp
       and sc["risk_score_100"] == r and sc["basis"] == "risk_score", (sc["score"], exp, r))
    ok("автокран: правила акта не изменились — ставка 0,42 %, премия 12 369 000",
       a["rate"]["applied_pct"] == 0.42 and a["premium"]["amount"] == 12_369_000, (a["rate"]["applied_pct"],
                                                                                    a["premium"]["amount"]))
    ok("автокран: риски — одна строка класса 100 %, сценарии PML/EML/MFL",
       len(sc["risks"]) == 1 and sc["risks"][0]["share_pct"] == 100.0
       and [s_["name"] for s_ in sc["scenarios"]] == ["PML", "EML", "MFL"]
       and sc["scenarios"][0]["amount"] == 1_472_500_000, (sc["risks"], sc["scenarios"]))
    SC_REPORT["автокран"] = (r, sc["score"], sc["class_code"])
    crane_id = a["id"]

    st, a = call("POST", "/act/make", {"lang": "ru", "must": WH8_MUST, "optional": WH8_OPT})
    sc = _sc_common("склад 4,2 млрд", a)
    exp, r = _sc_expect(a)
    ok("склад: балл = 500 − 5 × балл риска; сейсмозона — составляющая с баллами",
       sc["score"] == exp and next(c for c in sc["components"] if c["code"] == "seismic")["max"] > 0,
       (sc["score"], exp))
    ov = {o["code"]: o for o in sc["overview"]}
    ok("склад: в обзоре PML/EML/MFL 2 100 000 000 / 3 360 000 000 / 4 200 000 000 и лимит удержания (оценка)",
       [ov[k]["raw"] for k in ("pml", "eml", "mfl")] == [2_100_000_000, 3_360_000_000, 4_200_000_000]
       and ov["retention"]["raw"] and "временно" in ov["retention"]["label"], [ov[k]["raw"] for k in ("pml", "eml", "mfl")])
    ok("склад: риски с долей и уровнем (пожар первым, землетрясение есть)",
       sc["risks"][0]["code"] == "fire" and any(x["code"] == "earthquake" for x in sc["risks"])
       and all(x["share_text"] and x["level_label"] for x in sc["risks"]), sc["risks"][:3])
    SC_REPORT["склад 4,2 млрд"] = (r, sc["score"], sc["class_code"])

    st, b = upload([("sorov2.docx", DOCX_MIME, docx_table(BR_SAMPLE2))], {"lang": "ru"})
    st, a = br_make(b["session"], 2, br_request(b))
    sc = _sc_common("оборудование", a)
    exp, r = _sc_expect(a)
    ok("оборудование: балл = 500 − 5 × балл риска; источник данных — запрос филиала",
       sc["score"] == exp and "запрос филиала" in sc["subject"]["source"], (sc["score"], exp, sc["subject"]["source"]))
    SC_REPORT["оборудование 0832"] = (r, sc["score"], sc["class_code"])
    eq_id = a["id"]

    # договор из частей (0312: автомобиль + кредит)
    st, a = _pt_make(PT_MUST, PT_OPT)
    sc = a["scoring"]
    parts = {p["index"]: p for p in a["parts"]["items"]}
    exp = {i: ae.round_half_up(round(500 - 5 * p["analytics"]["score"]["score"], 6)) for i, p in parts.items()}
    ok("договор из частей: баллы частей = 500 − 5 × балл риска части, балл договора — наименьший",
       {p["index"]: p["score"] for p in sc["parts"]} == exp and sc["score"] == min(exp.values())
       and sc["worst_part"] == min(exp, key=exp.get) and sc["parts_note"] and "по самой опасной части" in sc["text"],
       (sc["parts"], exp))
    SC_REPORT["договор 0312"] = {p["index"]: (p["class_code"], p["score"], p["score_class"]) for p in sc["parts"]}

    # класс без аналитики (старый акт или сбой модуля): по уровню риска
    with db.tx() as con:
        row = db.rows(con, "SELECT act_json FROM acts WHERE id=?", crane_id)[0]
    stored = _json.loads(row["act_json"])
    D = stored["data"]
    D.pop("analytics", None)
    old = act.render(D, "ru", stored["meta"])
    lvl = D["risk"]["level"]
    ok("акт без аналитики: балл по уровню риска (низкий 430 / умеренный 300 / высокий 130), пометка",
       old["scoring"]["basis"] == "level" and old["scoring"]["score"] == ae.SCORE_BY_LEVEL[lvl]
       and "Балла риска нет" in old["scoring"]["text"] and old["scoring"]["components"][0]["code"] == "act_level",
       (old["scoring"]["score"], lvl, old["scoring"]["text"]))
    return crane_id, eq_id


def check_scoring_files(aid):
    print("43в. Скоринг в PDF и Word, отдельные адреса scoring.pdf и scoring.png")
    st, a = call("GET", f"/act/{aid}")
    sc = a["scoring"]
    st, blob, h = call("GET", f"/act/{aid}.pdf", raw=True)
    doc = pymupdf.open(stream=blob, filetype="pdf")
    p1 = re.sub(r"\s+", " ", doc[0].get_text().replace("\u00a0", " ").replace("\u00ad", "-"))
    # акт на один лист (06.10.2026): страницы скоринга в акте нет — балл и класс одной фразой в заключении
    ok("PDF акта: один лист, страницы скоринга нет; балл и класс — в заключении",
       doc.page_count == 1 and "СТРАХОВОЙ СКОРИНГ ОБЪЕКТА" not in p1 and "СЮРВЕЙЕРСКИЙ АКТ ПРЕДСТРАХОВОГО ОСМОТРА" in p1
       and f"страховой скоринг {sc['score']} из 500 (класс {sc['class_code']})" in p1, p1[:300])
    plain = act.build_pdf(dict(a, scoring={"available": False}))
    ok("PDF без скоринга — тот же один лист, без фразы о скоринге",
       pymupdf.open(stream=plain, filetype="pdf").page_count == 1
       and "страховой скоринг" not in pdf_text(pymupdf.open(stream=plain, filetype="pdf")))
    st, sblob, h = call("GET", f"/act/{aid}/scoring.pdf", raw=True)
    sdoc0 = pymupdf.open(stream=sblob, filetype="pdf")
    s1 = re.sub(r"\s+", " ", sdoc0[0].get_text().replace("\u00a0", " ").replace("\u00ad", "-"))
    ok("scoring.pdf: «Страховой скоринг объекта», балл и класс",
       "Страховой скоринг объекта" in s1 and "СТРАХОВОЙ СКОРИНГ ОБЪЕКТА" in s1 and str(sc["score"]) in s1
       and sc["class_code"] in s1 and sc["class_label"].upper() in s1, s1[:300])
    ok("scoring.pdf: блоки 1–6 и строка о скоринге",
       all(x in s1 for x in ("1. ОБЪЕКТ", "2. СКОРИНГ", "3. ОБЩИЙ ОБЗОР", "4. РИСКИ", "5. СЦЕНАРИИ УБЫТКА",
                             "6. ЧТО ПРОВЕРИТЬ АНДЕРРАЙТЕРУ"))
       and "Скоринг сформирован ИИ-сюрвейером по данным акта" in s1, s1[-400:])
    draw = sdoc0[0].get_drawings()
    fills = {tuple(round(c, 2) for c in d["fill"]) for d in draw if d.get("fill")}
    from app import act_scoring
    ok("PDF: на странице скоринга нарисованы пять цветных секторов и полосы цвета бренда",
       all(tuple(round(c, 2) for c in rgbv) in fills for rgbv in act_scoring.BAND_RGB.values())
       and tuple(round(c, 2) for c in act_scoring.rgb("#0B4F8A")) in fills, sorted(fills)[:10])
    st, blob, h = call("GET", f"/act/{aid}.docx", raw=True)
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
        names = z.namelist()
        for n in names:
            if n.endswith(".xml") or n.endswith(".rels"):
                minidom.parseString(z.read(n))
        xml = z.read("word/document.xml").decode("utf-8")
        rels = z.read("word/_rels/document.xml.rels").decode("utf-8")
        types = z.read("[Content_Types].xml").decode("utf-8")
        media = [n for n in names if re.fullmatch(r"word/media/[^/]+\.png", n)]
        png = z.read(media[0]) if media else b""
        good = True
    except Exception as e:
        good, xml, rels, types, media, png = False, str(e), "", "", [], b""
    ok("DOCX: корректный zip и XML; картинки скоринга в акте нет (она — в scoring.png)", good and not media
       and "<wp:inline" not in xml, (good, media))
    plain = re.sub(r"<[^>]+>", "", xml)
    ok("DOCX: акт без секции скоринга и разрыва страницы; балл и класс — фразой в заключении, поля 15 мм",
       "СТРАХОВОЙ СКОРИНГ ОБЪЕКТА" not in plain and "СЮРВЕЙЕРСКИЙ АКТ ПРЕДСТРАХОВОГО ОСМОТРА" in plain
       and 'w:type="page"' not in xml and f"страховой скоринг {sc['score']} из 500" in plain
       and 'w:left="850"' in xml, plain[:200])
    # отдельные адреса: владелец получает, чужой — 404
    st, blob, h = call("GET", f"/act/{aid}/scoring.pdf", raw=True)
    sdoc = pymupdf.open(stream=blob, filetype="pdf") if st == 200 else None
    ok("scoring.pdf — одна страница скоринга владельцу", st == 200 and h.get("content-type") == "application/pdf"
       and sdoc.page_count == 1 and "СТРАХОВОЙ СКОРИНГ ОБЪЕКТА" in pdf_text(sdoc), (st, h))
    st, blob, h = call("GET", f"/act/{aid}/scoring.png", raw=True)
    w_, h_ = act._png_size(blob) if st == 200 else (0, 0)
    ok("scoring.png — картинка PNG владельцу (шкала с плашкой класса)", st == 200 and h.get("content-type") ==
       "image/png" and blob[:8] == b"\x89PNG\r\n\x1a\n" and w_ > 800 and h_ > 300, (st, w_, h_))
    saved = dict(COOKIES)
    COOKIES.clear()
    st1, _b, _h = call("GET", f"/act/{aid}/scoring.pdf", raw=True)
    st2, _b, _h = call("GET", f"/act/{aid}/scoring.png", raw=True)
    COOKIES.clear()
    COOKIES.update(saved)
    ok("scoring.pdf и scoring.png чужому — 404", st1 == 404 and st2 == 404, (st1, st2))
    st, _b, _h = call("GET", "/act/zzzz/scoring.png", raw=True)
    ok("кривой номер акта — 404", st == 404, st)
    # крайние значения шкалы рисуются (стрелка на 0 и на 500)
    from app import act_scoring as asc_
    regular, bold = act._fonts()
    fit = lambda s, font: str(s)                                   # noqa: E731
    for v in (0, 500):
        sc2 = dict(sc, score=v, **{k: ae.score_band(v)[k] for k in ("class", "sub", "class_code")})
        pngv = asc_.gauge_png(sc2, regular, bold, fit, zoom=1)
        ok(f"шкала рисуется и при балле {v}", pngv[:8] == b"\x89PNG\r\n\x1a\n")


KATM_LEGAL = [
    "Кредитное бюро «Кредитно-информационный аналитический центр»",
    "Тип кредитного отчёта: InfoScore",
    "Номер запроса: 1234567890   Время запроса: {date} 10:15:00",
    "1. СУБЪЕКТ КРЕДИТНОЙ ИНФОРМАЦИИ",
    'Наименование: ООО "SINOV SAVDO"',
    "Юридический статус: Юридическое лицо",
    "ИНН: 301234567",
    "ОКЭД: 47190",
    "Адрес регистрации: г. Тестовый, ул. Примерная, 1",
    "Номер телефона: 998901234567",
    "Электронная почта: test@example.uz",
    "2. SCORING",
    "СКОРИНГОВЫЙ БАЛЛ: {score}",
    "КЛАСС ОЦЕНКИ: {cls}, ХОРОШИЙ уровень",
    "ВЕРСИЯ СКОРИНГА: 3.0",
    "3. ОБЩИЙ ОБЗОР (ОТКРЫТЫЕ + ЗАКРЫТЫЕ)",
    "5 - заявки",
    "4 - договора",
    "1 - условные обязательства",
    "8 - запросы и подписки по субъекту КИ",
    "12 500 000 - среднемесячный платёж (сумма)",
    "2 - количество просрочек основного долга (ОД)",
    "15 - максимальная просрочка ОД (дни)",
    "3 000 000 - максимальная просрочка ОД (сумма)",
    "4 - максимальная непрерывная просрочка % (дни)",
    "250 000 - всего просроченных % (сумма)",
    "4. ДЕЙСТВУЮЩИЕ ДОГОВОРА",
    '1 АКБ "NAMUNA BANK" 100200300400 UZS 150 000 000.00 {od} 8 000 000.00',
    '2 "SINOV BANK" АТБ 100200300401 UZS 50 000 000.00 0 4 500 000.00',
    "Итого 200 000 000.00 {od} 12 500 000.00",
    "5. ЗАЯВКИ БЕЗ ДОГОВОРОВ",
]


def katm_pdf(date_iso, score=312, cls="B2", od="0", individual=False) -> bytes:
    """Отчёт бюро PDF с текстом на выдуманных данных (название, ИНН, банки — не из образца заказчика)."""
    lines = [x.format(date=date_iso, score=score, cls=cls, od=od) for x in KATM_LEGAL]
    if individual:
        lines = [("ФИО: ТЕСТОВ ТЕСТ ТЕСТОВИЧ" if x.startswith("Наименование") else
                  "Юридический статус: Физическое лицо" if x.startswith("Юридический статус") else
                  "ПИНФЛ: 12345678901234" if x.startswith("ИНН") else x) for x in lines]
    doc = pymupdf.open()
    page = doc.new_page()
    font = act._fonts()[0]
    tw = pymupdf.TextWriter(page.rect)
    for i, ln in enumerate(lines):
        tw.append((40, 50 + i * 15), ln, font=font, fontsize=9)
    tw.write_text(page)
    return doc.tobytes()


def katm_scan_reply(date_iso):
    return "```json\n" + _json.dumps({
        "files": [{"n": 1, "view": "document", "document_kind": "credit_report"}], "fields": [], "damages": [],
        "branch_request": None, "contract": None,
        "credit_report": {"file": 1, "report_date": date_iso, "subject_type": "individual",
                          "name": "Тестов Тест Тестович", "inn": "123456789", "oked": None, "score": 180,
                          "score_class": "D1", "score_version": "3.0",
                          "overview": {"applications": 3, "contracts": 2, "contingent": 0, "inquiries": 4,
                                       "avg_monthly_payment": 1_500_000, "overdue_principal_count": 6,
                                       "max_overdue_principal_days": 75, "max_overdue_principal_amount": 4_000_000,
                                       "max_overdue_interest_days": 30, "overdue_interest_total": 600_000},
                          "active": {"count": 1, "total_debt": 20_000_000, "overdue": 1_200_000,
                                     "monthly_payment": 1_500_000, "creditors": ['"NAMUNA BANK" АТБ',
                                                                                 "Тестов Тест"]}}},
        ensure_ascii=False) + "\n```"


CR14_MUST = {"class_code": "14", "sum_insured": 40_000_000, "object_value": 40_000_000, "region": "Ташкент"}
CR14_OPT = {"class_fields": {"credit_amount": 100_000_000, "collateral_value": 60_000_000}}


def check_credit_report():
    print("43г. Отчёт кредитного бюро (КАТМ): текстовый PDF, физлицо, скан, проверки заёмщика, правки, классы")
    fresh()
    model_on(False)
    d_fresh = (date.today() - timedelta(days=5)).isoformat()
    d_old = (date.today() - timedelta(days=45)).isoformat()
    CALLS.clear()
    st, b = upload([("katm.pdf", "application/pdf", katm_pdf(d_fresh))], {"lang": "ru", "class_code": "14"})
    cb = (b or {}).get("credit_report") or {}
    f = cb.get("fields") or {}
    ok("PDF с текстом: отчёт бюро узнан правилами (без модели), источник — файл с текстом",
       st == 200 and cb.get("detected") and cb["source"] == "document" and not CALLS
       and any(d["kind"] == "credit_report" for d in b["documents"]), (st, cb.get("source")))
    ok("поля: дата, юрлицо, наименование и ИНН юрлица, ОКЭД, балл 312, класс B2, версия 3.0",
       f.get("report_date") == d_fresh and f["subject_type"] == "legal" and f["name"] == 'ООО "SINOV SAVDO"'
       and f["inn"] == "301234567" and f["oked"] == "47190" and f["score"] == 312 and f["score_class"] == "B2"
       and f["score_version"] == "3.0", f)
    ok("общий обзор: заявки 5, договоры 4, условные 1, запросы 8, платёж 12,5 млн, просрочки 2 / 15 дн. / 3 млн / "
       "4 дн. / 250 тыс.", f["overview"] == {"applications": 5, "contracts": 4, "contingent": 1, "inquiries": 8,
                                            "avg_monthly_payment": 12_500_000, "overdue_principal_count": 2,
                                            "max_overdue_principal_days": 15, "max_overdue_principal_amount": 3_000_000,
                                            "max_overdue_interest_days": 4, "overdue_interest_total": 250_000},
       f.get("overview"))
    ok("действующие договоры: 2, остаток 200 млн, просрочка 0, платёж 12,5 млн, банки-кредиторы",
       f["active"] == {"count": 2, "total_debt": 200_000_000, "overdue": 0, "monthly_payment": 12_500_000,
                       "creditors": ['АКБ "NAMUNA BANK"', '"SINOV BANK" АТБ']}, f.get("active"))
    dump = _json.dumps(b, ensure_ascii=False)
    ok("телефон, e-mail и адрес не извлекаются", "998901234567" not in dump and "test@example.uz" not in dump
       and "Примерная" not in dump, [x for x in ("998901234567", "test@example.uz", "Примерная") if x in dump])
    ok("в ответе /act/photos — строки «подпись — значение» и пометки (нет прямого запроса в КАТМ)",
       any(r["code"] == "score" and r["value"] == "312" for r in cb["rows"])
       and any("Прямого запроса в КАТМ нет" in n for n in cb["notes"]), cb.get("notes"))
    sid_legal = b["session"]

    # физическое лицо: только признак и скоринг
    st, bi = upload([("katm_fiz.pdf", "application/pdf", katm_pdf(d_fresh, individual=True))], {"lang": "ru"})
    fi = bi["credit_report"]["fields"]
    dump = _json.dumps(bi, ensure_ascii=False)
    ok("физлицо: subject_type = individual, без ФИО, ПИНФЛ и ИНН; балл и класс есть",
       fi["subject_type"] == "individual" and fi["name"] is None and fi["inn"] is None and fi["score"] == 312
       and "ТЕСТОВ" not in dump and "12345678901234" not in dump
       and any("физическому лицу" in n for n in bi["credit_report"]["notes"]), fi)

    # скан: ответ модели подменён; ФИО и ИНН физлица модель «вернула» — сервер их отбрасывает.
    # Сканы отчёта бюро модель читает только с разрешения администратора (credit_report.allow_scan = true)
    set_act_settings({"credit_report": {"allow_scan": True}})
    model_on(True)
    REPLY["text"] = katm_scan_reply(d_old)
    CALLS.clear()
    st, bs = upload([("katm_scan.png", "image/png", image((250, 250, 250)))], {"lang": "ru"})
    set_act_settings(None)
    cs = bs["credit_report"]
    prompt = " ".join(m["content"] for m in CALLS[0]["messages"]) if CALLS else ""
    ok("скан: модель читает по схеме credit_report (в инструкции — без ФИО, ПИНФЛ, телефона), источник photo",
       cs["detected"] and cs["source"] == "photo" and '"credit_report": null или' in prompt
       and "ФИО, ПИНФЛ" in prompt, (cs.get("source"), prompt[-300:]))
    ok("скан: физлицо — имя и ИНН отброшены, кредитор-гражданин отброшен, банк остался",
       cs["fields"]["name"] is None and cs["fields"]["inn"] is None
       and cs["fields"]["active"]["creditors"] == ['"NAMUNA BANK" АТБ']
       and "Тестов" not in _json.dumps(bs, ensure_ascii=False), cs["fields"])
    model_on(False)

    # /act/make: кредит, класс 14 — проверки заёмщика; ставка и уровень не меняются
    st, base = call("POST", "/act/make", {"lang": "ru", "must": CR14_MUST, "optional": CR14_OPT})
    st, a = call("POST", "/act/make", {"session": bs["session"], "lang": "ru", "must": CR14_MUST,
                                       "optional": dict(CR14_OPT, credit_report=cs["fields"])})
    bw = a.get("borrower") or {}
    ok("класс 14 + отчёт со скана: блок borrower, источник «со скана», возраст 45 дней",
       st == 200 and bw.get("available") and bw["source"] == "photo" and bw["source_kind"] == "document"
       and bw["age_days"] == 45 and bw["credit_product"], (st, bw.get("source"), bw.get("age_days")))
    ok("проверки заёмщика: низкий класс D1, действующая просрочка, отчёт устарел (45 > 30 дней)",
       bw["check_codes"] == ["borrower_low_class", "borrower_overdue", "borrower_stale"]
       and any("класс оценки кредитного бюро D1" in c for c in a["decision"]["checks"])
       and any("действующая просрочка по кредитам 1 200 000" in c.replace("\u00a0", " ") for c in a["decision"]["checks"])
       and any("Отчёт кредитного бюро устарел: 45 дн." in c for c in a["decision"]["checks"]), bw.get("checks"))
    ok("в уровень риска и ставку не входит: уровень, ставка, премия — как без отчёта",
       a["risk"]["level"] == base["risk"]["level"] and a["rate"]["applied_pct"] == base["rate"]["applied_pct"]
       and a["premium"]["amount"] == base["premium"]["amount"] and bw["in_rate"] is False
       and bw["in_risk_level"] is False and a["scoring"]["score"] == base["scoring"]["score"],
       (a["rate"]["applied_pct"], base["rate"]["applied_pct"]))
    s4 = next((li for li in a["sections"][3]["lists"] if li["title"] == "Заёмщик: данные кредитного бюро"), None)
    ok("раздел 4 акта: «Заёмщик: данные кредитного бюро» — физлицо без ФИО, просрочки, нагрузка, пометки",
       s4 and any("физическое лицо" in x for x in s4["items"]) and any(x.startswith("Просрочки:") for x in s4["items"])
       and any("не входят" in x for x in s4["items"]) and any("Прямого запроса в КАТМ нет" in x for x in s4["items"])
       and "Тестов" not in _json.dumps(a, ensure_ascii=False), s4)
    sb = a["scoring"]["borrower"]
    ok("страница скоринга: блок «Заёмщик (кредитное бюро)» — балл, класс, просрочки",
       sb and sb["score"] == 180 and sb["score_class"] == "D1" and sb["overdue"] == 1_200_000
       and [r["code"] for r in sb["rows"]][:3] == ["score", "overdue", "max_overdue_days"], sb)
    st, blob, h = call("GET", f"/act/{a['id']}/scoring.pdf", raw=True)
    p1 = re.sub(r"\s+", " ", pymupdf.open(stream=blob, filetype="pdf")[0].get_text().replace("\u00a0", " "))
    ok("scoring.pdf: на странице скоринга блок «ЗАЁМЩИК (КРЕДИТНОЕ БЮРО)», без «КАТМ» в заголовке",
       "ЗАЁМЩИК (КРЕДИТНОЕ БЮРО)" in p1 and "180 / D1" in p1 and "ЗАЁМЩИК (КАТМ)" not in p1, p1[-600:])
    ok("п. 2: borrower.note — «7 дней» и согласие субъекта; п. 8: проверки заёмщика добавляют оговорки к "
       "рекомендации («принять» → «принять с оговорками»), в уровень риска и ставку не входят",
       "хранятся в акте 7 дней" in bw["note"] and "согласие субъекта" in bw["note"]
       and "добавляют оговорки к рекомендации" in bw["note"] and "в уровень риска и ставку не входят" in bw["note"]
       and any("добавляют оговорки к рекомендации" in x for x in s4["items"])
       and any("хранятся в акте 7 дней" in x for x in s4["items"]) and a["decision"]["code"] != "accept", bw["note"])

    # отчёт из файла с текстом, правка сотрудника → «было → стало»
    edited = _json.loads(_json.dumps(f))
    edited["score_class"] = "C1"
    st, a2 = call("POST", "/act/make", {"session": sid_legal, "lang": "ru", "must": CR14_MUST,
                                        "optional": dict(CR14_OPT, credit_report=edited)})
    bw2 = a2["borrower"]
    ok("правка класса сотрудником: источник поля — input, правка «B2 → C1», проверка «C и ниже»",
       bw2["source_kind"] == "document_edited" and bw2["edits"] == [{"code": "score_class", "was": "B2", "now": "C1"}]
       and bw2["field_sources"]["score_class"] == "input" and bw2["field_sources"]["score"] == "document"
       and bw2["check_codes"] == ["borrower_low_class"]
       and any("Класс оценки: B2 → C1" in x for x in a2["sections"][3]["lists"][-1]["items"]), bw2.get("edits"))
    st, a3 = call("POST", "/act/make", {"session": sid_legal, "lang": "ru", "must": CR14_MUST,
                                        "optional": CR14_OPT})
    ok("отчёт только в загрузке (без ввода): берётся из своей загрузки, юрлицо с названием, проверок нет",
       a3["borrower"]["source"] == "document" and a3["borrower"]["check_codes"] == []
       and any("«ООО \"SINOV SAVDO\"», ИНН 301234567" in x for x in a3["borrower"]["lines"]), a3["borrower"]["lines"][:3])
    # чужая/истёкшая сессия — значения введены сотрудником
    st, a4 = call("POST", "/act/make", {"session": "deadbeef" * 3, "lang": "ru", "must": CR14_MUST,
                                        "optional": dict(CR14_OPT, credit_report=dict(f, source="document"))})
    ok("загрузки нет — источник input и пометка «отчёт недоступен»",
       a4["borrower"]["source_kind"] == "input" and a4["borrower"]["doc_missing"], a4["borrower"].get("source_kind"))
    # не кредитный класс: блок есть, проверок нет
    st, a5 = call("POST", "/act/make", {"lang": "ru", "must": WH8_MUST,
                                        "optional": dict(WH8_OPT, credit_report=cs["fields"])})
    ok("класс 8 с отчётом бюро: блок есть, проверок заёмщика нет, на странице скоринга блока заёмщика нет",
       a5["borrower"]["available"] and a5["borrower"]["check_codes"] == [] and not a5["borrower"]["credit_product"]
       and a5["scoring"]["borrower"] is None and not any("Заёмщик" in c for c in a5["decision"]["checks"]),
       a5["borrower"].get("check_codes"))
    # класс 15 (поручительство) — тоже кредитный
    st, a6 = call("POST", "/act/make", {"lang": "ru", "must": dict(CR14_MUST, class_code="15"),
                                        "optional": {"credit_report": cs["fields"]}})
    ok("класс 15: проверки заёмщика применяются", st == 200 and a6["borrower"]["credit_product"]
       and "borrower_low_class" in a6["borrower"]["check_codes"], (st, a6.get("borrower", {}).get("check_codes")))
    # кривой ввод — 422
    st, e = call("POST", "/act/make", {"lang": "ru", "must": CR14_MUST,
                                       "optional": dict(CR14_OPT, credit_report={"score_class": "Z9"})})
    st2, e2 = call("POST", "/act/make", {"lang": "ru", "must": CR14_MUST,
                                         "optional": dict(CR14_OPT, credit_report={"score": -1})})
    ok("неверный класс или балл в отчёте — 422 с полем credit_report", st == 422 and st2 == 422
       and "credit_report" in e["errors"] and "credit_report" in e2["errors"], (st, e, st2))
    SC_REPORT["отчёт бюро, класс 14"] = {"проверки": bw["check_codes"], "балл бюро": 180, "класс": "D1"}
    return a["id"]


def check_scoring_langs(aid, cr_aid):
    print("43д. Скоринг и отчёт бюро на трёх языках")
    for lang, word in (("uz", "yaxshi"), ("en", "good")):
        st, a = call("GET", f"/act/{aid}", params={"lang": lang})
        sc = a["scoring"]
        texts = [sc["title"], sc["text"], sc["method_text"], sc["note"], sc["footer_line"], sc["class_label"],
                 sc["request"]["type"]] + list(sc["titles"].values()) + [o["label"] for o in sc["overview"]] + \
            [r["label"] for r in sc["request"]["rows"]] + [r["label"] for r in sc["subject"]["rows"]] + \
            [c["label"] + " " + c["why"] for c in sc["components"]] + [b["label"] for b in sc["scale"]["bands"]]
        cyr = [x for x in texts if CYR.search(x or "")]
        ok(f"{lang}: страница скоринга без кириллицы (заголовки, обзор, составляющие, шкала)", not cyr, cyr[:4])
        ok(f"{lang}: подпись класса на языке акта", sc["class_label"] == word or sc["scale"]["bands"][3]["label"] == word,
           (sc["class_label"], sc["scale"]["bands"][3]["label"]))
        st, blob, h = call("GET", f"/act/{aid}/scoring.pdf", params={"lang": lang}, raw=True)
        p1 = pymupdf.open(stream=blob, filetype="pdf")[0].get_text()
        ok(f"{lang}: scoring.pdf — страница скоринга на языке акта", sc["title"] in p1.replace("\u00a0", " "), p1[:120])
        st, c = call("GET", f"/act/{cr_aid}", params={"lang": lang})
        lines = [x for x in c["borrower"]["lines"] if "NAMUNA" not in x]     # названия банков — как в отчёте
        cyr = [x for x in lines if CYR.search(re.sub(r"«[^»]*»|\"[^\"]*\"", "", x))]
        ok(f"{lang}: строки «Заёмщик» и проверки заёмщика без кириллицы", not cyr and c["borrower"]["checks"]
           and not [x for x in c["borrower"]["checks"] if CYR.search(x)], cyr[:3])


# ------------------------------------------------------------------ 44. замечания контролёра по скорингу (01.10.2026)

def set_act_settings(custom):
    """Настройки акта для теста: custom поверх умолчаний (None — только умолчания)."""
    with db.tx() as con:
        act.ensure_tables(con)
        con.execute("DELETE FROM act_settings")
        if custom:
            con.execute("INSERT INTO act_settings (created_at, created_by, settings_json, calibrated, note) "
                        "VALUES (?,?,?,?,?)", (db.now(), "test", _json.dumps(custom), 0, "тест"))


def _norm(s):
    return re.sub(r"\s+", " ", str(s).replace(" ", " ").replace("­", "-"))


def _decline_act(aid, lang="ru"):
    """Тот же акт с рекомендацией «отказать» (решение подменено в сохранённом акте; правила акта не трогаются)."""
    with db.tx() as con:
        row = db.rows(con, "SELECT act_json FROM acts WHERE id=?", aid)[0]
        stored = _json.loads(row["act_json"])
        stored["data"]["decision"]["code"] = "d_decline"
    return act.render(stored["data"], lang, stored["meta"]), stored


def _page_of(a):
    """Страница скоринга (с 06.10.2026 — только отдельным файлом scoring.pdf, в акт не входит)."""
    return pymupdf.open(stream=act.build_pdf(a, scoring_only=True), filetype="pdf")


def check_scoring_bands_engine():
    print("44а. Сектора шкалы — по порогам аналитики; класс всегда равен уровню аналитики; «≈» при округлении")
    from app import act_analytics as aa
    from app import risk_analytics as ra
    custom = [20, 40, 50.9, 80]
    ok("сектора по умолчанию: E 0–99, D 100–199, C 200–299, B 300–399, A 400–500",
       [(c, lo, hi) for c, lo, hi, _l in ae.score_bands()] ==
       [("E", 0, 99), ("D", 100, 199), ("C", 200, 299), ("B", 300, 399), ("A", 400, 500)])
    ok("порог 50,9: граница = 500 − 5 × 50,9 = 245,5 → 246; сектора E 0–99, D 100–245, C 246–299, B, A",
       [(c, lo, hi) for c, lo, hi, _l in ae.score_bands(custom)] ==
       [("E", 0, 99), ("D", 100, 245), ("C", 246, 299), ("B", 300, 399), ("A", 400, 500)])
    bad = []
    for bounds in (None, custom, [15, 35, 55, 75], [10.3, 33.3, 66.6, 90.1]):
        b = ae.score_bounds(bounds)
        for i in range(0, 1001):
            r = i / 10
            s = ae.round_half_up(round(500 - 5 * r, 6))
            band = ae.score_band(s, bounds, r)
            if ae.BAND_LEVEL5[band["class"]] != aa.LEVEL5[ra._level_of(r, list(b))]:
                bad.append((bounds, r, s, band["class_code"]))
    ok("балл риска 0–100 с шагом 0,1 при четырёх наборах порогов: класс скоринга = уровень аналитики (A низкий … "
       "E критический), и на границах после округления тоже", not bad, bad[:5])
    S20 = ae.insurance_score({"analytics": _sc_an(20.0, []), "risk": {"level": "moderate"}})
    S1999 = ae.insurance_score({"analytics": _sc_an(19.99, []), "risk": {"level": "low"}})
    ok("балл риска ровно 20 — «умеренный» → B (балл 400), 19,99 — «низкий» → A (балл 400)",
       (S20["score"], S20["class"], S1999["score"], S1999["class"]) == (400, "B", 400, "A"), (S20, S1999))
    an = _sc_an(50.9, [])
    an["score"].update(bounds=custom, level="high")
    S = ae.insurance_score({"analytics": an, "risk": {"level": "high"}})
    ok("порог 50,9 и балл риска 50,9: 500 − 5 × 50,9 ≈ 246, класс D (высокий, как у аналитики), шкала с bounds",
       S["score"] == 246 and S["class"] == "D" and S["analytics_level"] == "high" and S["exact"] is False
       and S["components"][0]["why"] == "500 − 5 × 50,9 ≈ 246" and S["scale"]["bounds"] == custom
       and "D 100–245" in S["method_text"], (S["score"], S["class_code"], S["components"][0]["why"]))
    ok("текст на трёх языках с «≈»: «500 − 5 × 50,9 ≈ 246»",
       all("500 − 5 × 50,9 ≈ 246" in tx.t("sc_text_risk", lg, score=246, code="D1", label="x", risk="50,9", eq="≈")
           for lg in ("ru", "uz", "en")))
    ok("настройка credit_report.allow_scan: по умолчанию false, проверяется как true/false",
       ae.DEFAULT_SETTINGS["credit_report"]["allow_scan"] is False
       and any("allow_scan" in e for e in ae.check_settings({"credit_report": {"allow_scan": "да"}}))
       and not ae.check_settings({"credit_report": {"allow_scan": True}}))


def check_scoring_review(aid):
    print("44б. Картинка шкалы, рекомендация и уровень акта, термины, переносы, «и ещё N», пометки")
    from app import act_scoring as asc_
    regular, bold = act._fonts()
    fit = lambda s, font: str(s)                                   # noqa: E731
    st, a = call("GET", f"/act/{aid}")
    sc = a["scoring"]
    # п. 3 — рекомендация и уровень риска акта
    ok("п. 3: scoring.decision{code,text} и scoring.act_level{code,label} — как в акте",
       sc["decision"]["code"] == a["decision"]["code"] and sc["decision"]["text"] in (
           "принять", "принять с оговорками", "отказать")
       and sc["act_level"] == {**sc["act_level"], "code": a["risk"]["level"], "label": a["risk"]["level_label"]},
       (sc["decision"], sc["act_level"]))
    # п. 5 — термины и уровень по аналитике
    ok("п. 5: термины «Страховой балл», «Страховой класс», «Версия шкалы»",
       (sc["titles"]["sc_score"], sc["titles"]["sc_class"], sc["titles"]["sc_version"]) ==
       ("Страховой балл", "Страховой класс", "Версия шкалы"), sc["titles"])
    lvl = a["analytics"]["score"]["level_label"]
    ok("п. 5: рядом с классом — уровень по аналитике словом раздела 4 («B — умеренный риск по аналитике»)",
       sc["analytics_level"]["label"] == lvl and sc["analytics_level"]["text"] == f"{sc['class']} — {lvl} риск по аналитике"
       and sc["scale"]["bands"] == [dict(b, label=b["label"]) for b in sc["scale"]["bands"]], sc["analytics_level"])
    # п. 1 — картинка: заголовок, номер и дата, подпись под шкалой, рекомендация и уровень; три языка
    for lg in ("ru", "uz", "en"):
        st, al = call("GET", f"/act/{aid}", params={"lang": lg})
        s2 = al["scoring"]
        doc = asc_.gauge_doc(s2, regular, bold, fit)
        txt = _norm(doc[0].get_text())
        need = [s2["titles"]["sc_type"], s2["request"]["number"], s2["request"]["date"], s2["gauge_caption"],
                s2["decision"]["text"], s2["act_level"]["label"], s2["class_code"]]
        ok(f"п. 1 ({lg}): на картинке — заголовок, номер и дата акта, «экспертная шкала, не калибровано; не кредитный "
           f"скоринг», рекомендация и уровень риска акта", all(_norm(x) in txt for x in need),
           [x for x in need if _norm(x) not in txt])
    ok("п. 1: подпись под шкалой — «Экспертная шкала, не калибровано. Не является кредитным скорингом и оценкой "
       "кредитного бюро»", sc["gauge_caption"] == "Экспертная шкала, не калибровано. Не является кредитным скорингом "
                                              "и оценкой кредитного бюро")
    txt = _norm(asc_.gauge_doc(sc, regular, bold, fit)[0].get_text())
    ok("п. 1 / мелочи: страховщик не задан — на картинке нет «данные недоступны»", "данные недоступны" not in txt)
    named = dict(sc, request=dict(sc["request"], insurer="ООО «Тест Сугурта»", insurer_known=True))
    ok("п. 1: страховщик задан — его название на картинке",
       "ООО «Тест Сугурта»" in _norm(asc_.gauge_doc(named, regular, bold, fit)[0].get_text()))
    # мелочи: «Сформировал» без страховщика не выводится вовсе
    p1 = _norm(_page_of(a)[0].get_text())
    ok("мелочи: страховщик не задан — строки «Сформировал» нет ни в JSON, ни на странице",
       not any(r["code"] == "by" for r in sc["request"]["rows"]) and "Сформировал" not in p1
       and "данные недоступны" not in p1.split("1. ОБЪЕКТ")[0], p1[:300])
    with db.tx() as con:
        stored = _json.loads(db.rows(con, "SELECT act_json FROM acts WHERE id=?", aid)[0]["act_json"])
    D = stored["data"]
    D2 = dict(D, insurer="ООО «Тест Сугурта»")
    a_ins = act.render(D2, "ru", stored["meta"])
    ok("страховщик задан — строка «Сформировал» есть", any(r["code"] == "by" and r["value"] == "ООО «Тест Сугурта»"
                                                         for r in a_ins["scoring"]["request"]["rows"]))
    # п. 3 — страница: рядом с плашкой рекомендация и уровень; «отказать» — перечёркнуто красным
    ok("п. 3: на странице — «Рекомендация акта», решение и «Уровень риска акта»",
       "Рекомендация акта" in p1 and sc["decision"]["text"] in p1 and "Уровень риска акта" in p1
       and sc["act_level"]["label"] in p1, p1[:600])
    eq = "=" if float(500 - 5 * sc["risk_score_100"]) == sc["score"] else "≈"
    ok("п. 9: на странице и в JSON — как получен балл: «500 − 5 × балл риска = / ≈ балл»",
       sc["formula"] == f"500 − 5 × {asc_._n(sc['risk_score_100'], 'ru')} {eq} {sc['score']}" and sc["formula"] in p1,
       sc.get("formula"))
    dec, _st = _decline_act(aid)
    sd = dec["scoring"]
    pg = _page_of(dec)[0]
    pt = _norm(pg.get_text())
    reds = [d for d in pg.get_drawings() if d.get("color") and tuple(round(c, 2) for c in d["color"]) ==
            tuple(round(c, 2) for c in asc_.RED)]
    ok("п. 3: «отказать» — decision.code = decline, на странице «см. рекомендацию акта: отказать» и красная рамка "
       "с чертой поверх плашки", sd["decision"]["code"] == "decline" and sd["decision"]["text"] == "отказать"
       and "см. рекомендацию акта: отказать" in pt and len(reds) >= 2, (sd["decision"], len(reds)))
    gt = _norm(asc_.gauge_doc(sd, regular, bold, fit)[0].get_text())
    ok("п. 1 + 3: и на картинке «отказать» с пометкой", "см. рекомендацию акта: отказать" in gt and "отказать" in gt)
    # п. 4 — нижние строки переносом, «не является кредитным скорингом» целиком при широком шрифте (×1,15)
    orig = asc_._Ink.width
    asc_._Ink.width = lambda self, s, size, b=False: orig(self, s, size, b) * 1.15
    try:
        for lg, phrase in (("ru", "не является кредитным скорингом"), ("uz", "kredit skoringi hisoblanmaydi"),
                           ("en", "not a credit score")):
            st, al = call("GET", f"/act/{aid}", params={"lang": lg})
            pg = _page_of(al)[0]
            words = pg.get_text("words")
            low = [w for w in words if w[1] > pg.rect.height - 140]
            bottom = _norm(" ".join(w[4] for w in sorted(low, key=lambda w: (round(w[1]), w[0]))))
            ok(f"п. 4 ({lg}): шрифт шире на 15 % — строка о скоринге и метод переносятся, «{phrase}» целиком, "
               f"без многоточия", phrase in bottom and "…" not in bottom, bottom[-300:])
    finally:
        asc_._Ink.width = orig
    # п. 9 — «и ещё N — в акте» обязательно; отступ перед методом; подписи кольца не задевают буквы
    long_checks = [f"Проверка номер {i}: очень длинный текст проверки андеррайтеру, который занимает почти всю строку "
                   f"страницы и даже переносится на вторую строку, чтобы места точно не хватило" for i in range(1, 6)]
    many = dict(sc, checks=long_checks, checks_total=14, checks_more="и ещё 9 — в разделе 5 акта",
                parts_note="Договор из 9 частей: балл — по самой опасной части 1",
                parts=[{"index": i, "class": "C", "text": f"часть {i} (класс 8): 250 — C2"} for i in range(1, 10)],
                borrower={"rows": [{"code": "x", "label": "строка", "value": "1"}] * 6})
    doc = pymupdf.open()
    page = doc.new_page(width=595.28, height=841.89)
    asc_.draw_page(page, many, regular, bold, fit, 42, 841.89 - 46)
    pt = _norm(page.get_text())
    drawn = pt.count("•")
    m = re.search(r"и ещё (\d+) — в разделе 5 акта", pt)
    ok("п. 9: проверки не помещаются — «и ещё N — в разделе 5 акта», N = всего − показано",
       m and int(m.group(1)) == 14 - drawn, (drawn, m and m.group(0)))
    m2 = re.search(r"и ещё (\d+) — в акте", pt)
    ok("п. 9: части договора не помещаются — «и ещё N — в акте»", bool(m2), pt[:900])
    more_y = max(r.y1 for r in page.search_for("в разделе 5 акта"))
    meth_y = min(r.y0 for r in page.search_for("Балл = 500"))
    ok("п. 9: отступ между последней строкой проверок и строкой о методе — не меньше 6 pt", meth_y - more_y >= 6,
       (more_y, meth_y))
    # подписи уровней на кольце и буквы секторов не пересекаются (картинка шкалы)
    # повёрнутые подписи — по буквам (рамка всего повёрнутого слова шире самого слова)
    names = [b["label"] for b in sc["scale"]["bands"]]
    for lg in ("ru", "uz", "en"):
        st, al = call("GET", f"/act/{aid}", params={"lang": lg})
        s3 = al["scoring"]
        names = [b["label"] for b in s3["scale"]["bands"]]
        g = asc_.gauge_doc(s3, regular, bold, fit)[0]
        letters, labels = [], []
        for b in g.get_text("rawdict")["blocks"]:
            for ln in b.get("lines", []):
                for sp in ln["spans"]:
                    word = "".join(ch["c"] for ch in sp["chars"]).strip()
                    if word in ("A", "B", "C", "D", "E"):
                        letters.append(pymupdf.Rect(sp["bbox"]))
                    elif word in names:
                        labels += [pymupdf.Rect(ch["bbox"]) for ch in sp["chars"] if ch["c"].strip()]
        hit = [(a_, b_) for a_ in letters for b_ in labels if (a_ & b_).get_area() > 0.5]
        ok(f"п. 9 ({lg}): подписи уровней на кольце («хороший», «слабый» …) не задевают буквы секторов",
           len(letters) == 5 and len(labels) >= 20 and not hit, (len(letters), len(labels), hit[:2]))
    # мелочи: «принят по умолчанию: …» — не в наименовании, а в примечании; вид не дублирует наименование
    D3 = _json.loads(_json.dumps(D))
    D3.setdefault("analytics", {}).update(object_type_source="default", object_type="Тестовый тип")
    out = act.render(D3, "ru", stored["meta"])
    obj_label = act._s1_label("object_type", "ru", D3.get("group"))
    for r in out["sections"][0]["rows"]:
        if r and r.get("label") == obj_label:
            r["value"] = tx.t("s1_kind_default", "ru", v="Тестовый тип")
    subj = asc_._subject(D3, out, "ru")
    rows = {r["code"]: r for r in subj["rows"]}
    ok("мелочи: «принят по умолчанию: …» не в наименовании — наименование «Тестовый тип», пометка в примечании",
       subj["name"] == "Тестовый тип" and "по умолчанию" not in rows["name"]["value"]
       and rows.get("note", {}).get("value") == tx.t("s1_kind_default_note", "ru"), subj["rows"])
    ok("мелочи: вид объекта, равный наименованию, не выводится второй раз",
       not any(r["code"] == "kind" and str(r["value"]).lower() == str(subj["name"]).lower() for r in subj["rows"]),
       subj["rows"])


def check_credit_parse_review():
    print("44в. Отчёт бюро: «Итого» по строкам, название банка в две строки, дата только по подписи")
    from app import credit_report as crr_
    head = ["Кредитное бюро «Кредитно-информационный аналитический центр»", "Тип кредитного отчёта: InfoScore",
            "Время запроса: 2026-09-28 10:15:00", "1. СУБЪЕКТ КРЕДИТНОЙ ИНФОРМАЦИИ", 'Наименование: ООО "SINOV SAVDO"',
            "Юридический статус: Юридическое лицо", "ИНН: 301234567", "2. SCORING", "СКОРИНГОВЫЙ БАЛЛ: 312",
            "КЛАСС ОЦЕНКИ: B2", "3. ОБЩИЙ ОБЗОР", "5 - заявки", "4 - договора"]
    cells = ["4. ДЕЙСТВУЮЩИЕ ДОГОВОРА", "№", "Кредитор", "Номер договора", "Валюта", "Остаток", "Просрочка", "Платёж",
             "1", 'АКБ "NAMUNA', 'BANK"', "100200300400", "UZS", "788 963 272.68", "0", "8 000 000.00",
             "2", '"SINOV BANK"', "АТБ", "100200300401", "UZS", "50 000 000.00", "0", "4 500 000.00",
             "Итого", "838 963 272.68", "0", "12 500 000.00", "5. ЗАЯВКИ БЕЗ ДОГОВОРОВ"]
    got = crr_.parse_text("\n".join(head + cells))
    ac = (got or {}).get("fields", {}).get("active") or {}
    ok("п. 6: «каждое значение на своей строке» — остаток 838 963 272,68, просрочка 0, платёж 12 500 000; 2 договора",
       (ac.get("total_debt"), ac.get("overdue"), ac.get("monthly_payment"), ac.get("count")) ==
       (838963272.68, 0, 12500000, 2), ac)
    ok("п. 6: название банка, перенесённое на вторую строку, склеено: «АКБ \"NAMUNA BANK\"», «\"SINOV BANK\" АТБ»",
       ac.get("creditors") == ['АКБ "NAMUNA BANK"', '"SINOV BANK" АТБ'], ac.get("creditors"))
    ok("п. 6: «0» перед «788 963 272.68» — отдельное число (и в одной строке)",
       crr_._total_numbers(" 838 963 272.68 0 788 963 272.68") == ["838 963 272.68", "0", "788 963 272.68"]
       and crr_._total_numbers("\n838 963 272.68\n0\n788 963 272.68\n") == ["838 963 272.68", "0", "788 963 272.68"])
    # PDF с текстом, раскладка «каждое значение на своей строке» — через настоящий разбор документа
    doc = pymupdf.open()
    page = doc.new_page(height=1200)
    tw = pymupdf.TextWriter(page.rect)
    for i, ln in enumerate(head + cells):
        tw.append((40, 30 + i * 13), ln, font=act._fonts()[0], fontsize=9)
    tw.write_text(page)
    g2 = crr_.parse_text(page.get_text())
    ok("п. 6: то же из PDF с текстом (каждое значение на своей строке)",
       g2 and g2["fields"]["active"]["total_debt"] == 838963272.68 and g2["fields"]["active"]["overdue"] == 0
       and g2["fields"]["active"]["creditors"] == ['АКБ "NAMUNA BANK"', '"SINOV BANK" АТБ'], (g2 or {}).get("fields"))
    # п. 7 — дата отчёта только по подписи
    base = [x for x in head if not x.startswith("Время запроса")]
    t1 = "\n".join(base[:3] + ["Дата рождения: 01.01.1980", "Дата договора: 2026-09-01"] + base[3:] + cells)
    g = crr_.parse_text(t1)
    ok("п. 7: без подписи даты — report_date = null и пометка «дата отчёта не найдена» (первая дата текста не берётся)",
       g["fields"]["report_date"] is None and "cr_no_date" in g["notes"], g["fields"]["report_date"])
    t2 = "\n".join(base[:3] + ["Дата согласия: 15.09.2026", "Дата заявки: 2026-09-10"] + base[3:] + cells)
    t3 = "\n".join(base[:3] + ["Дата согласия: 15.09.2026", "Дата заявки: 2026-09-10", "Дата запроса: 20.09.2026",
                               "Время запроса: 2026-09-21 09:00"] + base[3:] + cells)
    ok("п. 7: порядок подписей — «Время запроса», «Дата запроса», «Дата заявки», «Дата согласия»",
       crr_.parse_text(t2)["fields"]["report_date"] == "2026-09-10"
       and crr_.parse_text(t3)["fields"]["report_date"] == "2026-09-21")
    ck = ae.borrower_checks({"score_class": "B1", "report_date": None, "active": {}}, True, None)
    ok("п. 7: проверка «дата отчёта не найдена»", [c["code"] for c in ck] == ["borrower_stale"]
       and act._check_text({"code": "c_borrower_stale", "params": ck[0]["params"]}, "ru").startswith(
           "Дата отчёта кредитного бюро не найдена"), ck)


def check_credit_scan_off():
    print("44г. Сканы отчёта бюро в модель не уходят по умолчанию (credit_report.allow_scan = false), поле kinds")
    fresh()
    set_act_settings(None)
    d_old = (date.today() - timedelta(days=45)).isoformat()
    model_on(True)
    REPLY["text"] = katm_scan_reply(d_old)
    CALLS.clear()
    st, b = upload([("katm_scan.png", "image/png", image((250, 250, 250)))], {"lang": "ru"})
    prompt = " ".join(m["content"] for m in CALLS[0]["messages"]) if CALLS else ""
    ok("п. 2б: по умолчанию в инструкции модели нет схемы credit_report — вид называется только для отбрасывания",
       st == 200 and '"credit_report": null или' not in prompt and "только document_kind = credit_report" in prompt,
       prompt[-400:])
    cb = b.get("credit_report") or {}
    ok("п. 2б: модель узнала отчёт бюро на снимке — значения отброшены, note «скан … не читается — загрузите PDF»",
       not cb.get("detected") and any("Скан отчёта кредитного бюро не читается — загрузите PDF с текстом" in n
                                      for n in b["notes"]) and "D1" not in _json.dumps(b, ensure_ascii=False),
       (cb.get("detected"), b["notes"]))
    ok("п. 2: в предупреждении warn_pd — про отчёты бюро", "Отчёт кредитного бюро — только PDF с текстом" in b["warning"])
    # пометка сотрудника: картинка «отчёт бюро» в модель не уходит
    REPLY["text"] = CRANE_REPLY
    CALLS.clear()
    st, b = upload([("katm.png", "image/png", image((250, 250, 250))), ("crane.png", "image/png", image())],
                   {"lang": "ru", "class_code": "14", "kinds": _json.dumps({"1": "credit_report", "2": "object"})})
    sent = len((CALLS[0].get("files") or [])) if CALLS else 0
    f1 = (b.get("files") or [{}])[0]
    ok("п. 2: класс 14, файл помечен «отчёт бюро» — в модель ушёл только второй файл, у первого пометка",
       st == 200 and len(CALLS) == 1 and sent == 1 and f1.get("credit_scan_withheld") is True
       and f1.get("kind_marked") == "credit_report" and f1.get("read_by_ai") is False
       and any("не читается" in n for n in b["notes"]), (st, len(CALLS), sent, f1))
    CALLS.clear()
    st, b = upload([("katm.png", "image/png", image((250, 250, 250)))],
                   {"lang": "ru", "class_code": "14", "kinds": _json.dumps({"1": "credit_report"})})
    ok("п. 2: единственный файл — помеченный скан отчёта: модель не вызывается вовсе", st == 200 and not CALLS
       and b["files"][0]["credit_scan_withheld"], (st, len(CALLS)))
    for bad in ('{"1": "bureau"}', "[1]", '{"9": "object"}', "не json"):
        st, e = upload([("x.png", "image/png", image())], {"lang": "ru", "kinds": bad})
        ok(f"п. 2: kinds {bad} — 422 с полем kinds", st == 422 and "kinds" in (e.get("errors") or {}), (st, e))
    # разрешено администратором — как раньше: помеченный файл уходит модели, значения отчёта принимаются
    set_act_settings({"credit_report": {"allow_scan": True}})
    REPLY["text"] = katm_scan_reply(d_old)
    CALLS.clear()
    st, b = upload([("katm.png", "image/png", image((250, 250, 250)))],
                   {"lang": "ru", "class_code": "14", "kinds": _json.dumps({"1": "credit_report"})})
    ok("п. 2: allow_scan = true — скан уходит модели, отчёт со скана принят (как прежде)",
       st == 200 and len(CALLS) == 1 and b["credit_report"]["detected"] and b["credit_report"]["source"] == "photo",
       (st, len(CALLS), b.get("credit_report", {}).get("detected")))
    set_act_settings(None)
    model_on(False)


# ------------------------------------------------------------------ 45. вилка ставки (01.10.2026)

RF_REPORT = {}


def _rf(a):
    return a.get("rate_fork") or {}


def _rf_marks(a):
    return {m["code"]: m for m in _rf(a).get("marks") or []}


def _rf_hand_region(rf, sens=0.5, lo=-10.0, hi=15.0):
    """Поправка региона руками по показателям ответа: среднее (отношение − 1) × чувствительность, в границах."""
    used = [i for i in ((rf.get("adjustments") or {}).get("region") or {}).get("indicators") or [] if i["used"]]
    if not used:
        return 0.0, used
    raw = round(sum(round((i["ratio"] - 1) * sens * 100, 4) for i in used) / len(used), 2)
    return round(min(hi, max(lo, raw)), 2), used


def _rf_hand_market(act_pct, mk, steps=((60, 10), (80, 20))):
    if mk.get("market_rate_pct") is None or mk.get("loss_ratio_pct") is None or act_pct >= mk["market_rate_pct"]:
        return 0.0
    hit = [s for s in steps if mk["loss_ratio_pct"] >= s[0]]
    return float(hit[-1][1]) if hit else 0.0


def _rf_common(tag, a, S, term=365):
    """Общие проверки вилки одного класса: поправки руками, ставка п. 3, премии отметок, источники со ссылками."""
    rf, mk = _rf(a), _rf_marks(a)
    adj = rf.get("adjustments") or {}
    reg, used = _rf_hand_region(rf)
    mkt = _rf_hand_market(mk["act"]["rate_pct"], adj.get("market") or {})
    want = round(mk["act"]["rate_pct"] * (1 + reg / 100) * (1 + mkt / 100), 4)
    want = max(want, mk["min"]["rate_pct"]) if "min" in mk else want
    ok(f"{tag}: поправка региона пересчитана руками из показателей ({reg} %)",
       abs(adj["region"]["pct"] - reg) < 1e-9 and -10 <= adj["region"]["pct"] <= 15, (adj["region"]["pct"], reg))
    ok(f"{tag}: поправка рынка — по порогам убыточности НАПП ({mkt} %)", adj["market"]["pct"] == mkt,
       (adj["market"], mkt))
    ok(f"{tag}: ставка с учётом региона и рынка = ставка акта × (1 + рег.) × (1 + рын.), не ниже минимума",
       abs(mk["adjusted"]["rate_pct"] - want) < 1e-9, (mk["adjusted"]["rate_pct"], want))
    ok(f"{tag}: премия каждой отметки = ставка × сумма × срок / 365",
       all(m["premium"] == round(m["rate_pct"] / 100 * S * term / 365) for c, m in mk.items() if c != "act"
           and not (c == "adjusted" and rf.get("mode") == "apply")), {c: (m["rate_pct"], m["premium"]) for c, m in mk.items()})
    ok(f"{tag}: у отметок подписи, объяснение и источник; калибровка 0",
       all(m["label"] and m["note"] and m["source"] and m["source"]["title"] for m in mk.values())
       and rf["calibrated"] == 0 and rf["unit"] == "% годовых", [(m["code"], m["source"]) for m in mk.values()])
    ok(f"{tag}: у показателей региона — значение региона, республики, период и ссылка на источник",
       all(i["region_value"] is not None and i["country_value"] is not None and i["period"]
           and (i["source"] or {}).get("url", "").startswith("http") for i in used), used)
    if "market" in mk:
        ok(f"{tag}: рыночная отметка — НАПП со ссылкой и датой среза",
           mk["market"]["source"]["url"].startswith("https://napp.uz") and mk["market"]["source"]["as_of"]
           and adj["market"]["as_of"] == mk["market"]["source"]["as_of"], mk["market"]["source"])
    s4 = a["sections"][3]
    li = next((x for x in s4["lists"] if x["title"] == "Вилка ставки"), None)
    ok(f"{tag}: в разделе 4 «Вилка ставки» — таблица отметок, объяснение поправок и источники",
       li and li.get("table") and len(li["table"]["rows"]) == len(mk) and li["notes"][0] == rf["summary"]
       and any(n.startswith("Поправка региона") for n in li["notes"])
       and any(n.startswith("Поправка рынка") for n in li["notes"])
       and any("napp.uz" in x for x in li["sources"]), li and li.get("notes"))
    titles = [x["title"] for x in s4["lists"]]
    ok(f"{tag}: «Вилка ставки» — сразу после «Как посчитан тариф»",
       titles.index("Вилка ставки") == titles.index("Как посчитан тариф") + 1, titles[:4])
    ov = {o["code"]: o for o in a["scoring"]["overview"]}
    want_ov = f"{act._fork_num(mk['min']['rate_pct'], 'ru')} – {act._fork_num(rf['recommended']['rate_pct'], 'ru')} – " \
              f"{act._fork_num((mk.get('market') or {}).get('rate_pct'), 'ru')} %"
    ok(f"{tag}: в «Общем обзоре» скоринга — строка «вилка ставки: минимум – акт – рынок»",
       flat(ov["fork"]["value"]) == want_ov and ov["fork"]["label"].startswith("вилка ставки"),
       (ov.get("fork"), want_ov))
    return rf, mk, reg, mkt


def check_rate_fork_engine():
    print("45а. Вилка ставки: чистые функции act_engine (поправки, границы, ступени рынка, настройки)")
    fs = ae.fork_settings({})
    r = ae.fork_region([{"id": "a", "ratio": 1.735}, {"id": "b", "ratio": 1.215}], fs, True, True)
    ok("регион: ДТП 1,735 и кражи 1,215 × 0,5 → 23,75 % → граница +15 %",
       r["raw_pct"] == 23.75 and r["pct"] == 15 and r["clamped"] == "max" and r["used"] == 2, r)
    r = ae.fork_region([{"id": "a", "ratio": 0.6}], fs, True, True)
    ok("регион: отношение 0,6 → −20 % → граница −10 %", r["raw_pct"] == -20 and r["pct"] == -10
       and r["clamped"] == "min", r)
    r = ae.fork_region([{"id": "a", "ratio": 0.936}], fs, True, True)
    ok("регион: внутри границ без обрезки (0,936 → −3,2 %)", r["pct"] == -3.2 and r["clamped"] is None, r)
    reasons = (ae.fork_region([], fs, True, False)["reason"], ae.fork_region([], fs, True, True)["reason"],
               ae.fork_region([{"id": "a", "ratio": 1.5}], fs, False, True)["reason"],
               ae.fork_region([{"id": "a", "ratio": None}], fs, True, True)["reason"])
    ok("регион: причины без поправки — нет правила, не для вида, регион не распознан, нет разреза по регионам",
       reasons == ("no_rules", "kind", "region_unknown", "no_regional"), reasons)
    m = [ae.fork_market(0.4, 0.7, lr, fs) for lr in (59.9, 60, 79.99, 80, 250)]
    ok("рынок: ставка ниже рыночной — убыточность 59,9 → 0; 60 → +10; 79,99 → +10; 80 → +20; 250 → +20",
       [x["pct"] for x in m] == [0, 10, 10, 20, 20] and m[0]["reason"] == "lr_below" and m[1]["threshold"] == 60, m)
    ok("рынок: ставка акта не ниже рыночной или нет данных НАПП — 0",
       ae.fork_market(0.7, 0.7, 90, fs)["reason"] == "act_not_below" and ae.fork_market(0.8, 0.7, 90, fs)["pct"] == 0
       and ae.fork_market(0.4, None, 90, fs)["reason"] == "no_data" and ae.fork_market(0.4, 0.7, None, fs)["pct"] == 0)
    ok("ставка п. 3: 0,42 × 1,15 × 1,2 = 0,5796; ниже минимума — минимум",
       ae.fork_rate(0.42, 15, 20, 0.35) == (0.5796, False) and ae.fork_rate(0.06, -10, 0, 0.06) == (0.06, True))
    ok("положение ставки документа: ниже минимума, внутри, выше рынка",
       [ae.fork_position(x, 0.08, 0.185) for x in (0.05, 0.1, 0.2, None)] == ["below_min", "inside", "above_market",
                                                                              "none"])
    ok("настройки по умолчанию: reference, 0,5, −10…+15 %, ступени 60/80, calibrated 0, check_settings без ошибок",
       ae.DEFAULT_SETTINGS["rate_fork"]["mode"] == "reference" and fs["region"]["sensitivity"] == 0.5
       and fs["region"]["min_pct"] == -10 and fs["region"]["max_pct"] == 15
       and fs["market"]["steps"] == [[60, 10], [80, 20]] and ae.DEFAULT_SETTINGS["rate_fork"]["calibrated"] == 0
       and ae.check_settings({}) == [] and ae.check_settings({"rate_fork": {"mode": "apply"}}) == [])
    # замечание контролёра 01.10.2026: частота претензий НАПП искажена (88,8 % претензий страны — город Ташкент),
    # поэтому по умолчанию вес 0, а у классов 4, 5, 6 показателя нет вовсе (не «единственный показатель»)
    ind = fs["region"]["indicators"]
    ok("claims_freq: вес по умолчанию 0; в списках классов 3, 7, 8, 9 остаётся, у 4, 5, 6 правила вилки нет",
       fs["region"]["weights"] == {"claims_freq": 0.0} and all(c not in ind for c in ("4", "5", "6"))
       and all("claims_freq" in ae.fork_indicator_ids(fs, c, None)[0] for c in ("3", "7", "8", "9")), ind)
    r = ae.fork_region([{"id": "claims_freq", "ratio": 0.652, "used": False, "why": "weight_zero"}], fs, True, True)
    ok("регион: сравнение есть только у показателя с весом 0 → поправка 0, причина zero_weight",
       r["pct"] == 0 and r["reason"] == "zero_weight", r)
    ok("рынок: база по умолчанию full_year_if_available; check_settings ловит неверную базу",
       fs["market"]["basis"] == "full_year_if_available"
       and any("rate_fork.market.basis" in e for e in ae.check_settings({"rate_fork": {"market": {"basis": "x"}}}))
       and ae.check_settings({"rate_fork": {"market": {"basis": "last"}}}) == [])
    pk = {"loss_ratio_pct": 81.9, "loss_ratio_full_year_pct": 2.5}
    ok("рынок: скачок за полугодие (81,9 %) при полном годе 2,5 % — ступень по полному году; basis last — по срезу",
       ae.fork_market_lr(pk, fs) == (2.5, "full_year", True)
       and ae.fork_market_lr(pk, ae.fork_settings({"rate_fork": {"market": {"basis": "last"}}})) == (81.9, "last", False)
       and ae.fork_market_lr({"loss_ratio_pct": 81.9, "loss_ratio_full_year_pct": None}, fs) == (81.9, "last", False)
       and ae.fork_market_lr({"loss_ratio_pct": 14.6, "loss_ratio_full_year_pct": 14.3}, fs) == (14.6, "last", False)
       and ae.fork_market_lr({"loss_ratio_pct": 14.6, "loss_ratio_full_year_pct": 90}, fs) == (14.6, "last", False))
    adj = ae.fork_adjust({"mode": "tariff", "applied_pct": 0.3, "min_pct": 0.1},
                         {"pct": 0.0}, {"rate_pct": 0.676, **pk}, fs)
    ok("fork_adjust: пакет 3,14 — убыточность 2,5 % за полный год, надбавки нет, пометка full_year_switch",
       adj["market"]["pct"] == 0 and adj["market"]["full_year_switch"] and adj["market"]["loss_ratio_pct"] == 2.5
       and adj["market"]["loss_ratio_last_pct"] == 81.9 and adj["market"]["basis"] == "full_year", adj["market"])
    bad = {"mode": "x", "region": {"sensitivity": 9, "max_pct": 500, "indicators": {"3": {"*": ["нет_такого"]},
                                                                                    "8": {"дом": []}}},
           "market": {"steps": [[80, 20], [60, 10]], "loss_ratio": "y"}, "calibrated": 1, "extra": 1}
    errs = " | ".join(ae.check_settings({"rate_fork": bad}))
    ok("check_settings ловит ошибки вилки: режим, чувствительность, граница, показатель, группа, ступени, база, "
       "calibrated, лишний ключ",
       all(x in errs for x in ("rate_fork.mode", "sensitivity", "max_pct", "нет_такого", "группа «дом»",
                               "по возрастанию", "loss_ratio", "calibrated", "неизвестные ключи extra")), errs)
    ok("частичная правка region не стирает остальные ключи (fork_settings)",
       ae.fork_settings({"rate_fork": {"region": {"sensitivity": 1}}})["region"]["indicators"]
       == ae.DEFAULT_SETTINGS["rate_fork"]["region"]["indicators"])


def _ensure_napp() -> bool:
    """Таблицы НАПП (napp_claims, napp_branches, пакеты классов) на копии базы теста: нет — собираем
    tools/market_stats.build() в эту же копию (db.DB_PATH — копия, рабочая база не трогается). Нет разобранных
    отчётов в data/parsed — False: проверки, которым нужны таблицы, честно пропускаются с пометкой."""
    with db.tx() as con:
        have = db.rows(con, "SELECT COUNT(*) n FROM sqlite_master WHERE type='table' AND name IN "
                            "('napp_claims', 'napp_branches')")[0]["n"] == 2
        filled = have and db.rows(con, "SELECT COUNT(*) n FROM napp_claims")[0]["n"] > 0
    if filled:
        return True
    parsed = ROOT_DIR / "data" / "parsed"
    if not any(parsed.glob("*/3.5.csv")):
        print("  ПРОПУСК: нет таблиц НАПП (napp_claims / napp_branches) и нет разобранных отчётов в data/parsed")
        return False
    sys.path.insert(0, str(ROOT_DIR / "tools"))
    import market_stats as ms
    assert ms.db_path() == db.DB_PATH and "surveyor-act-test" in str(db.DB_PATH), "только копия базы"
    n, dates = ms.build()
    print(f"  таблицы НАПП собраны на копии базы: market_stats {n} строк, срезы {dates[0]}…{dates[-1]}")
    return True


def check_rate_fork():
    print("45б. Вилка ставки в /act/make: автокран, склад кл. 8 и 9, оборудование 0832 с запросом, класс без "
          "статистики региона, обязательный вид, продукт без ставки, договор по частям, режим apply, три языка")
    fresh()
    model_on(False)
    set_act_settings(None)
    napp = _ensure_napp()
    cf_why = "weight_zero" if napp else "no_data"
    S = CRANE_MUST["sum_insured"]
    # --- автокран 0318: правила акта не меняются, вилка рядом
    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                       "optional": dict(CRANE_OPT, object_kind="truck_crane")})
    ok("автокран: режим reference — ставка 0,42 % и премия 12 369 000 прежние",
       a["rate"]["applied_pct"] == 0.42 and a["premium"]["amount"] == 12_369_000 and not a["rate"]["fork_applied"],
       (a["rate"]["applied_pct"], a["premium"]["amount"]))
    rf, mk, reg, mkt = _rf_common("автокран", a, S)
    ok("автокран: отметки min 0,35 / act 0,42 (рекомендуем) / adjusted / market / technical / factors (место хранения "
       "«открытая площадка» × 1,15 → 0,483 %, справочно)",
       list(mk) == ["min", "act", "adjusted", "market", "technical", "factors"] and mk["min"]["rate_pct"] == 0.35
       and mk["factors"]["rate_pct"] == 0.483 and not mk["factors"]["is_recommended"]
       and mk["act"]["is_recommended"] and rf["recommended"] == {"code": "act", "rate_pct": 0.42, "premium": 12_369_000}
       and rf["mode"] == "reference", list(mk))
    ids = {i["id"]: i for i in rf["adjustments"]["region"]["indicators"]}
    # 01.10.2026: частота претензий НАПП (claims_freq) в списке, но с весом 0 — показана справочно, в поправку не входит
    ok("автокран: регион — ДТП и кражи (угоны); претензии НАПП показаны, в поправку не входят (вес 0)",
       set(ids) == {"road_accidents", "thefts", "claims_freq"} and ids["road_accidents"]["used"]
       and ids["thefts"]["used"] and not ids["claims_freq"]["used"] and ids["claims_freq"]["why"] == cf_why
       and (not napp or ids["claims_freq"]["source"]["url"].startswith("https://napp.uz")), ids)
    R = rf["adjustments"]["region"]
    ok("автокран: поправка региона прежняя — ДТП 1,735 и кражи 1,215 × 0,5 → +23,75 % → граница +15 %",
       R["raw_pct"] == 23.75 and R["pct"] == 15 and R["clamped"] == "max", (R["raw_pct"], R["pct"]))
    crane_ratio = ids["claims_freq"].get("ratio")
    ok("автокран: техническая отметка = техническая ставка аналитики (справочно)",
       mk["technical"]["rate_pct"] == round(a["analytics"]["tariff"]["technical_pct"], 4), mk["technical"])
    ok("автокран: вывод одной фразой", flat(rf["summary"]).startswith("Допустимо от 0,35 % (минимум); рекомендуем 0,42 %; "
                                                               "рынок ") and "С учётом региона и рынка" in rf["summary"]
       and act.pct(mk["adjusted"]["rate_pct"], "ru") in rf["summary"], flat(rf["summary"]))
    rows = {r["label"]: r for r in a["sections"][3]["rows"]}
    ok("автокран: в разделе 4 рядом с премией — ставка с учётом региона и рынка и премия по ней (справочно)",
       rows["Ставка с учётом региона и рынка"]["value"] == act.pct(mk["adjusted"]["rate_pct"], "ru")
       and act.money(mk["adjusted"]["premium"], "ru") in rows["Ставка с учётом региона и рынка"]["note"],
       rows.get("Ставка с учётом региона и рынка"))
    RF_REPORT["автокран"] = (mk["min"]["rate_pct"], mk["act"]["rate_pct"], mk["adjusted"]["rate_pct"],
                             mk["market"]["rate_pct"], reg, mkt, sorted(ids))
    crane_aid = a["id"]
    plain_docx, plain_pdf = docx_plain(crane_aid), pdf_plain(crane_aid)
    ok("автокран: «Вилка ставки» с источниками — в JSON и на экране; в документе на один лист её нет (справочно)",
       "Вилка ставки" not in plain_docx and "Вилка ставки" not in plain_pdf and "napp.uz" not in plain_pdf
       and rf["summary"], plain_pdf[:200])
    for lg, title, start in (("uz", "Tarif oraligʻi", "ruxsat etiladi (minimum)"), ("en", "Rate range", "Acceptable from")):
        st, b = call("GET", f"/act/{crane_aid}", params={"lang": lg})
        txt = all_text(b) + _json.dumps(b["rate_fork"], ensure_ascii=False)
        ok(f"автокран {lg}: вилка на языке акта, без кодов текстов",
           b["rate_fork"]["title"] == title and (start in b["rate_fork"]["summary"] or b["rate_fork"]["summary"]
                                                  .startswith(start)) and "rf_" not in txt
           and not re.search(r"\bhow_fork", txt) and title in [x["title"] for x in b["sections"][3]["lists"]],
           b["rate_fork"]["summary"])
        li = next(x for x in b["sections"][3]["lists"] if x["title"] == title)
        cyr = [x for x in [str(c) for r in li["table"]["rows"] for c in r] + li["notes"] + li["sources"]
               + [b["rate_fork"]["summary"]] if re.search(r"[А-Яа-яЁё]", x)]
        ok(f"автокран {lg}: в таблице, пояснениях и источниках вилки нет кириллицы", not cyr, cyr[:3])
    # --- склад 4,2 млрд, класс 8 (0807): здание — доля глинобитного жилья; ЧС без регионов
    st, a = call("POST", "/act/make", {"lang": "ru", "must": WH8_MUST, "optional": WH8_OPT})
    ok("склад кл. 8: правила акта не меняются (0,06 %, 2 520 000)",
       a["rate"]["applied_pct"] == 0.06 and a["premium"]["amount"] == 2_520_000, (a["rate"]["applied_pct"],
                                                                                  a["premium"]["amount"]))
    rf, mk, reg, mkt = _rf_common("склад кл. 8", a, WH8_MUST["sum_insured"])
    ids = {i["id"]: i for i in rf["adjustments"]["region"]["indicators"]}
    ok("склад кл. 8: учтён жилой фонд по материалу стен (здание), ЧС — не учтены (нет разреза по регионам)",
       ids["vulnerable_housing"]["used"] and not ids["emergencies"]["used"]
       and ids["emergencies"]["why"] == "no_regional", ids)
    ok("склад кл. 8: претензии НАПП не в поправке; поправка прежняя −3,2 % (жилой фонд 0,936)",
       not ids["claims_freq"]["used"] and rf["adjustments"]["region"]["pct"] == -3.2
       and [k for k, i in ids.items() if i["used"]] == ["vulnerable_housing"], rf["adjustments"]["region"]["pct"])
    ok("склад кл. 8: рыночная ставка пакета «8, 9» — с пометкой", any("«8, 9»" in x for x in rf["how"]), rf["how"])
    RF_REPORT["склад кл. 8"] = (mk["min"]["rate_pct"], mk["act"]["rate_pct"], mk["adjusted"]["rate_pct"],
                                mk["market"]["rate_pct"], reg, mkt, [k for k, v in ids.items() if v["used"]])
    # --- склад класс 9 (0808), город Ташкент: преступления и кражи
    st, a = call("POST", "/act/make", {"lang": "ru", "must": WH_MUST, "optional": WH_OPT})
    rf, mk, reg, mkt = _rf_common("склад кл. 9", a, WH_MUST["sum_insured"])
    ids = {i["id"]: i for i in rf["adjustments"]["region"]["indicators"]}
    ok("склад кл. 9: регион — зарегистрированные преступления и кражи; претензии НАПП справочно; поправка +15 %",
       set(ids) == {"crimes_total", "thefts", "claims_freq"} and ids["crimes_total"]["used"] and ids["thefts"]["used"]
       and not ids["claims_freq"]["used"] and rf["adjustments"]["region"]["pct"] == 15,
       (rf["adjustments"]["region"]["pct"], ids))
    RF_REPORT["склад кл. 9"] = (mk["min"]["rate_pct"], mk["act"]["rate_pct"], mk["adjusted"]["rate_pct"],
                                mk["market"]["rate_pct"], reg, mkt, sorted(ids))
    # --- оборудование 0832 с запросом филиала 0,05 %
    st, b = upload([("sorov2.docx", DOCX_MIME, docx_table(BR_SAMPLE2))], {"lang": "ru"})
    st, a = br_make(b["session"], 2, br_request(b))
    rf, mk, reg, mkt = _rf_common("оборудование 0832", a, BR_S2, a["premium"]["term_days"])
    ok("0832: отметка запроса филиала 0,05 % — ниже минимума 0,08 %",
       mk["request"]["rate_pct"] == 0.05 and rf["position"]["request"] == "below_min"
       and "ниже минимума" in mk["request"]["note"], mk.get("request"))
    ok("0832: вывод «… Запрос филиала 0,05 % — ниже минимума»",
       flat(rf["summary"]).endswith("Запрос филиала 0,05 % — ниже минимума.") and "рекомендуем 0,096 %" in flat(rf["summary"]),
       rf["summary"])
    reg_b = rf["adjustments"]["region"]
    ids = {i["id"]: i for i in reg_b["indicators"]}
    # 01.10.2026: жилой фонд к оборудованию не относится, ЧС — без регионов, претензии НАПП — вес 0: поправки нет
    cf = ids["claims_freq"]
    ok("0832: оборудование — жилой фонд не подходит, ЧС без регионов, претензии НАПП справочно → поправка 0",
       ids["vulnerable_housing"]["why"] == "kind" and ids["emergencies"]["why"] == "no_regional"
       and not cf["used"] and cf["why"] == cf_why and not [k for k, i in ids.items() if i["used"]]
       and reg_b["pct"] == 0 and reg_b["reason"] == ("zero_weight" if napp else "no_regional"), reg_b.get("text"))
    ok("0832: премия акта прежняя (по ставке акта 0,096 %)", rf["recommended"]["premium"] == a["premium"]["amount"]
       and a["rate"]["applied_pct"] == 0.096, (rf["recommended"], a["premium"]["amount"]))
    RF_REPORT["оборудование 0832"] = (mk["min"]["rate_pct"], mk["act"]["rate_pct"], mk["adjusted"]["rate_pct"],
                                      mk["market"]["rate_pct"], reg, mkt, mk["request"]["rate_pct"],
                                      rf["position"]["request"])
    eq_aid = a["id"]
    # --- класс без статистики региона в правиле вилки (НС юрлиц, класс 1) и нераспознанный регион
    st, a = call("POST", "/act/make", {"lang": "ru", "must": {"product_code": "0101", "sum_insured": 100_000_000,
                                                              "object_value": 100_000_000, "region": "Ташкент"},
                                       "optional": {}})
    rf = _rf(a)
    ok("класс 1 без статистики региона: поправка 0, пометка «нет данных по региону для этого вида объекта»",
       st == 200 and rf["available"] and rf["adjustments"]["region"]["pct"] == 0
       and rf["adjustments"]["region"]["reason"] == "no_rules"
       and "нет данных по региону для этого вида объекта" in rf["adjustments"]["region"]["text"],
       (st, rf.get("adjustments")))
    st, a = call("POST", "/act/make", {"lang": "ru", "must": dict(CRANE_MUST, region="Атлантида"),
                                       "optional": dict(CRANE_OPT, object_kind="truck_crane")})
    reg_b = _rf(a)["adjustments"]["region"]
    ok("регион не распознан: поправка 0 с честной пометкой", reg_b["pct"] == 0 and reg_b["reason"] == "region_unknown"
       and "Атлантида" in reg_b["text"] and a["premium"]["amount"] == 12_369_000, reg_b["text"])
    # --- обязательный вид (0820): одна ставка по нормативному акту
    st, a = call("POST", "/act/make", {"lang": "ru", "must": {"product_code": "0820", "sum_insured": 1_000_000_000,
                                                              "object_value": 1_000_000_000, "region": "Ташкент"},
                                       "optional": {}})
    rf = _rf(a)
    ok("обязательный вид: вилки нет, одна ставка по акту, «тариф установлен нормативным актом»",
       not rf["available"] and rf["reason"] == "statutory" and [m["code"] for m in rf["marks"]] == ["act"]
       and rf["marks"][0]["rate_pct"] == a["rate"]["applied_pct"] and rf["adjustments"] is None
       and "установлен нормативным актом" in rf["summary"] and "ПКМ №532" in rf["summary"], rf.get("summary"))
    # --- продукт «по программе» (0321): только рыночный ориентир
    st, a = call("POST", "/act/make", {"lang": "ru", "must": dict(CRANE_MUST, product_code="0321"),
                                       "optional": CRANE_OPT})
    rf = _rf(a)
    ok("продукт без ставки: только рыночный ориентир со ссылкой и пометка",
       not rf["available"] and rf["reason"] == "undefined" and [m["code"] for m in rf["marks"]] == ["market"]
       and rf["marks"][0]["source"]["url"].startswith("https://napp.uz") and "не определена" in rf["summary"]
       and a["premium"]["amount"] is None, rf.get("summary"))
    # --- договор по частям (0312: 60 млн класс 3 + 40 млн класс 14)
    st, a = _pt_make(PT_MUST, dict(PT_OPT, parts=[PT_CAR, PT_CREDIT]))
    parts = a["parts"]["items"]
    pf = [p["rate_fork"] for p in parts]
    ok("части: вилка у каждой части; класс 3 — с поправкой региона, класс 14 — без правила (поправка 0)",
       all(f["available"] for f in pf) and pf[0]["adjustments"]["region"]["pct"] != 0
       and pf[1]["adjustments"]["region"]["reason"] == "no_rules", [f["summary"] for f in pf])
    rf = _rf(a)
    cm = _rf_marks(a)
    sums = {c: sum(next(m for m in f["marks"] if m["code"] == c)["premium"] for f in pf) for c in ("min", "act",
                                                                                                  "adjusted")}
    ok("части: вилка договора справочная — по каждой отметке сумма премий частей",
       rf["reason"] == "parts_reference" and rf["reference_only"] and all(cm[c]["premium"] == sums[c] for c in sums)
       and all(cm[c]["rate_pct"] == round(sums[c] / 100_000_000 * 100, 4) for c in sums), (cm, sums))
    ok("части: премия договора прежняя (сумма премий частей по ставкам акта)",
       a["premium"]["amount"] == sum(p["premium"] for p in parts) == sums["act"], (a["premium"]["amount"], sums))
    ok("части: в разделе 4 «Вилка ставки по частям» — строки частей и договора",
       any(x["title"] == "Вилка ставки по частям" and len(x["table"]["rows"]) == 3 for x in a["sections"][3]["lists"]))
    mk3 = pf[0]["adjustments"]["market"]
    ok("части 0312: рынок — пакет 3,14; при скачке убыточности за полугодие ступень по полному году с пометкой",
       mk3["row_key"] == "cls3_14" and (not mk3.get("full_year_switch") or (
           mk3["loss_ratio_pct"] == mk3["loss_ratio_full_year_pct"] and mk3["basis"] == "full_year"
           and any("взята оценка за полный год" in x for x in mk3["lines"]))), mk3.get("lines"))
    rows3 = {r["class_code"]: r for r in mk3.get("class_rows") or []} if "class_rows" in mk3 else None
    lines3 = " | ".join(mk3.get("lines") or [])
    ok("части 0312 (правило № 5): строка «по одиночным строкам классов: класс 3 — …, класс 14 — …»",
       "по одиночным строкам классов: класс 3 — " in lines3 and ", класс 14 — " in lines3, lines3)
    if mk3.get("full_year_switch"):
        RF_REPORT["0312: пакет 3,14 — убыточность срез / полный год"] = (mk3["loss_ratio_last_pct"],
                                                                           mk3["loss_ratio_full_year_pct"],
                                                                           mk3["pct"])
    RF_REPORT["0312 по частям"] = [(p["class_code"], f["recommended"]["rate_pct"],
                                    next(m for m in f["marks"] if m["code"] == "adjusted")["rate_pct"]) for p, f in
                                   zip(parts, pf)] + [("договор", cm["act"]["rate_pct"], cm["adjusted"]["rate_pct"])]
    # --- claims_freq: вес 0 (по умолчанию) ничего не меняет, вес 1 (администратор включил) — меняет
    if napp:
        set_act_settings({"rate_fork": {"region": {"weights": {"claims_freq": 0}}}})
        st, a0 = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                            "optional": dict(CRANE_OPT, object_kind="truck_crane")})
        set_act_settings({"rate_fork": {"region": {"weights": {"claims_freq": 1}}}})
        st, a1 = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                            "optional": dict(CRANE_OPT, object_kind="truck_crane")})
        R0, R1 = _rf(a0)["adjustments"]["region"], _rf(a1)["adjustments"]["region"]
        i1 = {i["id"]: i for i in R1["indicators"]}
        raw1 = round(sum(round((i["ratio"] - 1) * 0.5 * 100, 4) for i in i1.values() if i["used"]) / 3, 2)
        ok("claims_freq с весом 0 — поправка та же (+15 % от +23,75 %), ставка п. 3 та же",
           R0["raw_pct"] == 23.75 and R0["pct"] == 15
           and _rf_marks(a0)["adjusted"]["rate_pct"] == RF_REPORT["автокран"][2], (R0["raw_pct"], R0["pct"]))
        ok(f"claims_freq с весом 1 — входит в поправку: (36,75 + 10,75 + ({crane_ratio} − 1) × 50) / 3 = {raw1} %",
           i1["claims_freq"]["used"] and sum(1 for i in i1.values() if i["used"]) == 3 and R1["raw_pct"] == raw1 and R1["pct"] != R0["pct"]
           and _rf_marks(a1)["adjusted"]["rate_pct"] != _rf_marks(a0)["adjusted"]["rate_pct"], (R1["raw_pct"], raw1))
        it1 = next(x for x in a1["analytics"]["stats"]["indicators"] if x["id"] == "napp_region_claims")
        ok("вес 1: строка «Претензии в регионе» говорит, что входит в поправку (включено в настройках)",
           "входит в поправку ставки с весом 1" in it1["text"].lower() and not it1["reference_only"], it1["text"])
        RF_REPORT["автокран, claims_freq вес 1"] = (R1["raw_pct"], R1["pct"], _rf_marks(a1)["adjusted"]["rate_pct"])
        set_act_settings(None)
        # раздел 4: строка претензий справочная, с оговорками на трёх языках
        st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                           "optional": dict(CRANE_OPT, object_kind="truck_crane")})
        it = next(x for x in a["analytics"]["stats"]["indicators"] if x["id"] == "napp_region_claims")
        ok("раздел 4: «Претензии в регионе» — справочно: «в поправку ставки не входит: … 89 % — город Ташкент»",
           it["reference_only"] and "в поправку ставки не входит: претензии учитываются по месту головных офисов "
           "страховщиков, 89" in flat(it["text"]).lower() and "— город Ташкент" in it["text"], it["text"])
        ok("раздел 4: оговорки к претензиям — не страховые случаи; с начала года / на дату; прошлые периоды; "
           "Ташкент; срезы 3/6/9/12 мес. несопоставимы",
           all(w in it["text"] for w in ("не страховые случаи", "с начала года", "действующие на дату",
                                         "прошлых периодов", "сосредоточены в городе Ташкенте",
                                         "3, 6, 9 и 12 месяцев", "регион с республикой на одном срезе")), it["text"])
        cc = next(x for x in a["analytics"]["stats"]["indicators"] if x["id"] == "napp_company_claims")
        ok("раздел 4: «Претензии: рынок / INSON» — с теми же оговорками",
           "не страховые случаи" in cc["text"] and "несопоставимы" in cc["text"], cc["text"])
        br_it = next((x for x in a["analytics"]["stats"]["indicators"] if x["id"] == "napp_branches"), None)
        ok("раздел 4: подпись «Подразделения INSON в регионе (все вместе, по отчёту НАПП)»",
           br_it and br_it["name"] == "Подразделения INSON в регионе (все вместе, по отчёту НАПП)", br_it)
        for lg, words in (("uz", ("sugʻurta hodisalari emas", "Toshkent shahrida")),
                          ("en", ("not insured events", "Tashkent city", "not comparable"))):
            st, b = call("GET", f"/act/{a['id']}", params={"lang": lg})
            itl = next(x for x in b["analytics"]["stats"]["indicators"] if x["id"] == "napp_region_claims")
            ok(f"{lg}: оговорки к претензиям на языке акта",
               all(w in itl["text"] for w in words) and (lg != "en" or not re.search(r"[А-Яа-яЁё]", itl["text"])),
               itl["text"])
        set_act_settings({"napp": {"branch_min_contracts": 10 ** 6}})
        st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                           "optional": dict(CRANE_OPT, object_kind="truck_crane")})
        br_it = next((x for x in a["analytics"]["stats"]["indicators"] if x["id"] == "napp_branches"), None)
        ok("подразделения: договоров меньше порога настройки napp.branch_min_contracts — пометка «малая база»",
           br_it and br_it.get("small_base") and "малая база" in br_it["text"].lower(), br_it and br_it["text"])
        set_act_settings(None)
    else:
        print("  ПРОПУСК: claims_freq с весом 1 и строки претензий раздела 4 — нет таблиц НАПП")
    # --- поправка рынка через настройки ступеней (убыточность класса 3 по НАПП — около 15 %)
    set_act_settings({"rate_fork": {"market": {"steps": [[10, 10], [14, 20]]}}})
    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                       "optional": dict(CRANE_OPT, object_kind="truck_crane")})
    rf, mk = _rf(a), _rf_marks(a)
    lr = rf["adjustments"]["market"]["loss_ratio_pct"]
    want = 20.0 if lr >= 14 else (10.0 if lr >= 10 else 0.0)
    ok(f"рынок по ступеням настройки: убыточность {lr} % → +{want} %, ставка п. 3 пересчитана",
       rf["adjustments"]["market"]["pct"] == want and mk["adjusted"]["rate_pct"] == round(
           0.42 * (1 + rf["adjustments"]["region"]["pct"] / 100) * (1 + want / 100), 4)
       and a["premium"]["amount"] == 12_369_000, rf["adjustments"]["market"])
    # --- режим apply: ставка п. 3 — ставка акта, всё от неё
    set_act_settings({"rate_fork": {"mode": "apply"}})
    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                       "optional": dict(CRANE_OPT, object_kind="truck_crane")})
    rf, mk = _rf(a), _rf_marks(a)
    adj_rate = mk["adjusted"]["rate_pct"]
    prem = round(adj_rate / 100 * S)
    ok("apply: ставка акта = ставка с учётом региона и рынка, премия пересчитана",
       a["rate"]["applied_pct"] == adj_rate and a["premium"]["amount"] == prem and a["rate"]["fork_applied"]
       and a["rate"]["fork_act_pct"] == 0.42 and rf["recommended"] == {"code": "adjusted", "rate_pct": adj_rate,
                                                                       "premium": prem}
       and mk["adjusted"]["is_recommended"] and mk["act"]["premium"] == 12_369_000, (a["rate"]["applied_pct"], prem))
    _rf_common("apply автокран", a, S)
    ok("apply: в «как посчитан тариф» обе поправки и премия по новой ставке",
       any(h.startswith("Поправка региона") for h in a["rate"]["how"]) and any(h.startswith("Поправка рынка")
                                                                              for h in a["rate"]["how"])
       and any(act.money(prem, "ru") in h for h in a["rate"]["how"]), a["rate"]["how"])
    fr = a["franchise"]
    ms = a["measures_summary"]
    ov = {o["code"]: o for o in a["scoring"]["overview"]}
    ok("apply: франшиза, мероприятия, скоринг — от той же премии",
       fr["premium_before"] == prem and (ms.get("premium_before") in (None, prem)) and ov["premium"]["raw"] == prem
       and ov["tariff"]["raw"] == adj_rate, (fr.get("premium_before"), ms, ov["premium"]))
    plain_docx, plain_pdf = docx_plain(a["id"]), pdf_plain(a["id"])
    ok("apply: премия одна и та же в Word и PDF, в «Цене» — ставка с учётом региона и рынка",
       flat(act.money(prem, "ru")) in plain_docx and flat(act.money(prem, "ru")) in plain_pdf
       and "с учётом региона и рынка" in plain_pdf)
    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                       "optional": dict(CRANE_OPT, object_kind="truck_crane",
                                                        deductible={"pct": 1, "type": "unconditional"})})
    fr = a["franchise"]
    hand_rate = max(_rf_marks(a)["adjusted"]["rate_pct"] * fr["multiplier"], 0.35)   # премия — от неокруглённой ставки
    ok("apply + франшиза сотрудника: франшиза применяется к ставке с поправками",
       fr["applied"] and fr["premium_before"] == prem and a["premium"]["amount"] == round(hand_rate / 100 * S)
       and _rf(a)["premium_final"] == a["premium"]["amount"], (fr.get("premium_before"), a["premium"]["amount"]))
    st, b = upload([("sorov2.docx", DOCX_MIME, docx_table(BR_SAMPLE2))], {"lang": "ru"})
    st, a = br_make(b["session"], 2, br_request(b))
    it = rq_items(a)
    ok("apply 0832: сверка с запросом — со ставкой и премией акта",
       it["tariff_act"]["calculated"] == a["rate"]["applied_pct"] and it["premium_act"]["calculated"]
       == a["premium"]["amount"], (it["tariff_act"], it["premium_act"], a["premium"]["amount"]))
    st, a = _pt_make(PT_MUST, dict(PT_OPT, parts=[PT_CAR, PT_CREDIT]))
    parts = a["parts"]["items"]
    ok("apply части: премия части = ставка с поправками; премия договора = сумма; вилка договора рекомендует adjusted",
       all(p["rate"]["applied_pct"] == p["rate_fork"]["recommended"]["rate_pct"] for p in parts)
       and a["premium"]["amount"] == sum(p["premium"] for p in parts) == _rf_marks(a)["adjusted"]["premium"]
       and _rf(a)["recommended"]["code"] == "adjusted", [(p["rate"]["applied_pct"], p["premium"]) for p in parts])
    # --- reference снова: прежние цифры
    set_act_settings(None)
    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                       "optional": dict(CRANE_OPT, object_kind="truck_crane")})
    ok("reference снова: 0,42 % и 12 369 000", a["rate"]["applied_pct"] == 0.42
       and a["premium"]["amount"] == 12_369_000 and _rf(a)["mode"] == "reference")
    # --- старый акт без вилки открывается
    with db.tx() as con:
        row = db.rows(con, "SELECT act_json FROM acts WHERE id=?", eq_aid)[0]
        stored = _json.loads(row["act_json"])
    stored["data"].pop("rate_fork", None)
    old = act.render(stored["data"], "ru", stored["meta"])
    ok("акт до вилки (без rate_fork) открывается: блок available = false, reason old_act",
       old["rate_fork"]["available"] is False and old["rate_fork"]["reason"] == "old_act"
       and "Вилка ставки" not in [x["title"] for x in old["sections"][3]["lists"]])


# ------------------------------------------------------------------ 45. минимальная ставка страховщика и заниженная ставка
# (01.10.2026): «минимальную ставку пусть назначает сам страховщик; если ниже тарифа — можно ли застраховать: да/нет,
# почему». Версии минимальной ставки (app/min_rates.py), тип ставки annual | fixed, оценка below_min_assessment.

BM_REPORT = {}
BM_S = 1_000_000_000
BM_REQ = {"tariff_pct": 0.05, "term_from": "2026-10-01", "term_to": "2027-09-30"}      # 365 дн. включительно
BM_OPT = {"object_type": "Оборудование", "location": "open_area", "documents_provided": True,
          "losses_3y": {"count": 0, "small_count": 0, "amount": 0}}


def bm_make(product="0832", optional=None, request=None, S=BM_S, lang="ru"):
    body = {"lang": lang, "must": {"product_code": product, "sum_insured": S, "object_value": S,
                                   "region": "Ташкентская область"},
            "optional": dict(optional if optional is not None else BM_OPT)}
    if request is not None:
        body["optional"]["request"] = request
    return call("POST", "/act/make", body)


def _bm_codes(bm):
    return {r["code"]: r for r in bm.get("reasons") or []}


def _cyr(s) -> bool:
    return bool(re.search(r"[А-Яа-яЁё]", str(s)))


def check_rate_type_engine():
    print("45а. Тип ставки и оценка заниженной ставки — чистые функции (100 млн, 0,5 %, 3 года)")
    import dataclasses
    from app import min_rates as mrs
    ok("премия: годовая 100 млн × 0,5 % × 1 095 / 365 = 1 500 000; фиксированная 100 млн × 0,5 % = 500 000",
       round(ae.premium_by_type(0.5, 100e6, 1095)) == 1_500_000
       and round(ae.premium_by_type(0.5, 100e6, 1095, "fixed")) == 500_000
       and round(mrs.premium(0.5, 100e6, 1095, "fixed")) == 500_000)
    ok("годовой эквивалент фиксированной 0,5 % на 1 095 дн. = 0,1667 %; годовая — без пересчёта",
       round(ae.annual_pct(0.5, 1095, "fixed"), 4) == 0.1667 and ae.annual_pct(0.5, 1095) == 0.5)
    with db.tx() as con:
        ref = db.load_reference(con)
    ref2 = dataclasses.replace(ref, min_rates={**ref.min_rates, "0832": {"company": {None: 0.5}, "regulator": {}}})
    prod = {"code": "0832", "name": "т", "pricing_mode": "ставка", "rate_text": "0,5%"}
    ra = ae.rate(ref2, prod, "8", "low", 100e6, 1095, None, None, {})
    rf = ae.rate(ref2, prod, "8", "low", 100e6, 1095, None, None, {}, rate_type="fixed")
    ok("ae.rate: годовая — премия 1 500 000; фиксированная — 500 000, годовой эквивалент 0,1667 %",
       ra["premium"] == 1_500_000 and ra["rate_type"] == "annual" and rf["premium"] == 500_000
       and rf["rate_type"] == "fixed" and rf["annual_equiv_pct"] == 0.1667 and rf["applied_pct"] == 0.5,
       (ra["premium"], rf["premium"], rf.get("annual_equiv_pct")))
    ok("ae.rate fixed: «как посчитано» — премия без деления на срок и годовой эквивалент",
       [h["code"] for h in rf["how"]][-2:] == ["how_premium_fixed", "how_rate_annual_equiv"])
    rq = {"tariff_pct": 0.5, "premium": 500_000, "term_days": 1095, "source": "input"}
    val = {"verdict": "normal", "ratio_pct": 100}
    c_fix = ae.request_check(rq, rate_res=rf, rate_final=0.5, premium_final=500_000, sum_insured=100e6,
                             object_value=100e6, value=val, fr={}, rate_type="fixed")
    c_ann = ae.request_check(rq, rate_res=ra, rate_final=0.5, premium_final=1_500_000, sum_insured=100e6,
                             object_value=100e6, value=val, fr={})
    pf = {i["code"]: i for i in c_fix["items"]}["premium_request"]
    pa = {i["code"]: i for i in c_ann["items"]}["premium_request"]
    ok("сверка запроса по типу ставки: fixed — 500 000 сходится; annual — расчёт 1 500 000, премия 500 000 расходится",
       pf["verdict"] == "ok" and pf["calculated"] == 500_000 and pa["verdict"] == "differs"
       and pa["calculated"] == 1_500_000, (pf, pa))

    base = dict(requested_pct=0.05, requested_source="request", min_pct=0.08, term_days=365, sum_insured=BM_S,
                level="moderate", losses_count=0, documents=True, object_new=False, net_pct=0.04,
                net_calibrated=False, retention_within=True, eml=BM_S, retention_limit=43e9, market_rate_pct=0.185,
                loss_ratio_pct=24.78)
    r = ae.below_min_assess(**base, settings={"below_min": {"min_share_pct": 70}})
    ok("0,05 при минимуме 0,08: 62,5 % минимальной ниже порога 70 % (явная настройка) — «нет»",
       r["available"] and r["verdict"] == "not_allowed" and r["share_pct"] == 62.5 and r["shortfall"] == 300_000
       and _bm_codes(r)["bm_share_low"]["effect"] == "no_conditions" and r["rule"]["min_share_pct"] == 70,
       r.get("verdict"))
    r = ae.below_min_assess(**base)
    ok("порог по умолчанию 60 %: 62,5 % ≥ 60 %, умеренный уровень, убытков нет, нетто покрыта — «да при условиях», "
       "шесть условий",
       ae.DEFAULT_SETTINGS["below_min"]["min_share_pct"] == 60 and r["rule"]["min_share_pct"] == 60
       and r["verdict"] == "allowed_with_conditions" and r["conditions"] == list(ae.BM_CONDITIONS)
       and _bm_codes(r)["bm_share_ok"]["sign"] == "for", r.get("conditions"))
    r = ae.below_min_assess(**dict(base, losses_count=2), settings={"below_min": {"min_share_pct": 60}})
    ok("2 убытка за 3 года — «нет» без обсуждения", r["verdict"] == "not_allowed" and r["hard"]
       and _bm_codes(r)["bm_losses"]["effect"] == "blocks")
    r = ae.below_min_assess(**dict(base, statutory=True, requested_pct=0.07))
    ok("обязательный вид — «нет» без обсуждения", r["verdict"] == "not_allowed" and r["hard"]
       and "bm_statutory" in _bm_codes(r))
    r = ae.below_min_assess(**dict(base, level="high", requested_pct=0.07))
    ok("уровень высокий — «нет»", r["verdict"] == "not_allowed" and _bm_codes(r)["bm_level"]["effect"] == "blocks")
    r = ae.below_min_assess(**dict(base, retention_within=False, eml=50e9, requested_pct=0.07))
    ok("EML выше удержания — «нет»", r["verdict"] == "not_allowed" and "bm_retention_over" in _bm_codes(r))
    r = ae.below_min_assess(**dict(base, documents=False, requested_pct=0.07))
    ok("документов нет — «нет»", r["verdict"] == "not_allowed" and "bm_no_docs" in _bm_codes(r))
    yes = dict(base, level="low", object_new=True, requested_pct=0.07, net_pct=0.05, market_rate_pct=0.1)
    r = ae.below_min_assess(**yes)
    ok("низкий уровень, новый объект с документами, убытков нет, нетто покрыта, рынок ≤ 2× — «да» (условие — "
       "полномочия андеррайтера)", r["verdict"] == "allowed" and r["conditions"] == ["cond_uw_authority"]
       and all(x["sign"] == "for" for x in r["reasons"] if x["code"] != "bm_gap"), r.get("reasons"))
    r = ae.below_min_assess(**dict(yes, market_rate_pct=0.5, loss_ratio_pct=55))
    ok("рынок выше запрошенной в 7 раз и убыточность 55 % — уже не «да», а «при условиях»",
       r["verdict"] == "allowed_with_conditions" and "bm_market_no" in _bm_codes(r))
    r = ae.below_min_assess(**dict(yes, net_pct=0.09, net_calibrated=True))
    ok("запрошенная ниже калиброванной нетто-ставки — «нет» без обсуждения",
       r["verdict"] == "not_allowed" and _bm_codes(r)["bm_net_below"]["effect"] == "blocks")
    r = ae.below_min_assess(**dict(yes, net_pct=0.09, net_calibrated=False))
    ok("ниже экспертной нетто-ставки (net_check = calibrated) — «при условиях» с подтверждением андеррайтера",
       r["verdict"] == "allowed_with_conditions" and "cond_net_confirm" in r["conditions"])
    r = ae.below_min_assess(**dict(yes, net_pct=0.09), settings={"below_min": {"net_check": "always"}})
    ok("net_check = always: ниже экспертной нетто-ставки — «нет»", r["verdict"] == "not_allowed")
    r = ae.below_min_assess(**dict(base, requested_pct=0.08))
    ok("запрошенная не ниже минимальной — оценки нет (available = false)", not r["available"]
       and r["reason"] == "not_below")
    r = ae.below_min_assess(**dict(yes, rate_type="fixed", term_days=1095, requested_pct=0.15, min_pct=0.2,
                                   sum_insured=100e6))
    ok("фиксированная ставка: недобор = (0,2 − 0,15) % × 100 млн = 50 000 без деления на срок; годовой эквивалент "
       "запрошенной 0,05 %", r["shortfall"] == 50_000 and r["requested_annual_pct"] == 0.05, r.get("shortfall"))
    errs = ae.check_settings({"below_min": {"min_share_pct": 140, "net_check": "x"}})
    ok("настройки below_min проверяются", any("min_share_pct" in e for e in errs) and any("net_check" in e for e in errs),
       errs)


def check_below_min_act():
    print("45б. Акт: 0832, запрос филиала 0,05 % при минимуме 0,08 % — да / нет / при условиях, доводы, недобор премии")
    fresh()
    model_on(False)
    set_act_settings({"below_min": {"min_share_pct": 70}})
    st, a = bm_make(request=BM_REQ)
    bm = a["below_min_assessment"]
    ok("0832: уровень умеренный, документы есть, убытков нет; порог 70 % (явная настройка) — «нет»: 62,5 % минимальной",
       st == 200 and a["risk"]["level"] == "moderate" and bm["available"] and bm["verdict"] == "not_allowed"
       and bm["verdict_label"] == "нет" and "62,5" in bm["text"] and "70" in bm["text"], (a["risk"]["level"], bm.get("text")))
    BM_REPORT["0832 порог 70 %"] = (bm["verdict"], bm["share_pct"], bm["shortfall"])
    set_act_settings(None)                 # порог по умолчанию — 60 %
    st, a = bm_make(request=BM_REQ)
    bm = a["below_min_assessment"]
    rs = _bm_codes(bm)
    ok("порог по умолчанию 60 %: «да, при условиях»; requested 0,05, min 0,08, разрыв 0,03 п. п., источник — запрос "
       "филиала",
       bm["rule"]["min_share_pct"] == 60 and bm["verdict"] == "allowed_with_conditions" and bm["verdict_label"] == "да, при условиях"
       and bm["requested_pct"] == 0.05 and bm["min_pct"] == 0.08 and bm["gap_pct"] == 0.03
       and bm["requested_source"] == "request" and bm["calibrated"] == 0, bm.get("text"))
    ok("условия: франшиза, мероприятия, территория, повторный осмотр, документы, нетто-ставка, полномочия андеррайтера",
       [c["code"] for c in bm["conditions"]] == ["cond_franchise", "cond_measures", "cond_territory", "cond_reinspect",
                                                 "cond_docs_confirm", "cond_net_confirm", "cond_uw_authority"]
       and all(c["text"] for c in bm["conditions"]), [c["code"] for c in bm["conditions"]])
    ok("недобор премии за срок: (0,08 − 0,05) % × 1 млрд × 365 / 365 = 300 000 сум",
       bm["shortfall"] == 300_000 and "300 000 сум" in nb(bm["text"]) and "300 000" in nb(rs["bm_gap"]["text"]),
       bm.get("shortfall"))
    ok("доводы «за» и «против» с цифрами: уровень, убытки, нетто-ставка 0,18 % против 0,05 %, рынок 0,185 %, "
       "удержание, документы, разрыв",
       {"bm_level", "bm_losses_none", "bm_net_below_expert", "bm_retention_ok", "bm_docs", "bm_share_ok",
        "bm_not_new", "bm_gap"} <= set(rs) and {"for", "against"} == {r["sign"] for r in bm["reasons"]}
       and "0,18" in nb(rs["bm_net_below_expert"]["text"]) and "0,05" in nb(rs["bm_net_below_expert"]["text"])
       and any(k in rs for k in ("bm_market_high", "bm_market_ok")), [(r["code"], r["sign"]) for r in bm["reasons"]])
    ok("пометка: правило разработчика, не утверждено страховщиком; решение — андеррайтер",
       "правило разработчика" in bm["note"].lower() and "андеррайтер" in bm["note"])
    ok("decision.checks: «Отступление от минимальной ставки … да, при условиях — решение андеррайтера»",
       any(c.startswith("Отступление от минимальной ставки") and "да, при условиях" in c
           for c in a["decision"]["checks"]) and a["decision"]["code"] != "accept", a["decision"]["checks"])
    titles = [li["title"] for li in a["sections"][3]["lists"]]
    ok("раздел 4: подраздел «Ставка ниже минимальной: можно ли застраховать»; раздел 5 — ответ по запрошенной ставке",
       "Ставка ниже минимальной: можно ли застраховать" in titles
       and any("только при условиях" in p for p in a["sections"][4]["paragraphs"]), titles)
    ov = {o["code"]: o for o in a["scoring"]["overview"]}
    ok("скоринг: строка обзора «ставка ниже минимальной: можно ли принять» — да, при условиях",
       ov["below_min"]["value"] == "да, при условиях" and ov["below_min"]["raw"] == "allowed_with_conditions")
    BM_REPORT["0832 порог 60 %"] = (bm["verdict"], bm["shortfall"], [c["code"] for c in bm["conditions"]])
    BM_REPORT["0832 доводы"] = [(r["sign"], r["text"]) for r in bm["reasons"]]
    cond_aid = a["id"]

    st, a = bm_make(optional=dict(BM_OPT, losses_3y={"count": 2, "small_count": 0, "amount": 0}), request=BM_REQ)
    bm = a["below_min_assessment"]
    ok("тот же запрос с 2 убытками за 3 года — «нет» без обсуждения; рекомендация не «принять без оговорок»",
       bm["verdict"] == "not_allowed" and bm["hard"] and _bm_codes(bm)["bm_losses"]["effect"] == "blocks"
       and a["decision"]["code"] != "accept"
       and any("принять объект нельзя" in p for p in a["sections"][4]["paragraphs"]), bm.get("text"))
    BM_REPORT["0832 2 убытка"] = bm["verdict"]

    st, a = bm_make("0820", dict(BM_OPT, requested_rate_pct=0.3))
    bm = a["below_min_assessment"]
    ok("обязательный вид 0820 (0,4 % по ПКМ № 532), запрошено 0,3 % — «нет»: тариф нормативного акта",
       st == 200 and bm["available"] and bm["verdict"] == "not_allowed" and "bm_statutory" in _bm_codes(bm)
       and bm["min_pct"] == 0.4, (st, bm.get("text")))

    with db.tx() as con:
        mn = db.rows(con, "SELECT min_rate_pct FROM min_rates WHERE product_code='0901'")[0]["min_rate_pct"]
    rq = round(mn * 0.9, 4)
    st, a = bm_make("0901", {"location": "guarded", "documents_provided": True, "year": date.today().year,
                             "losses_3y": {"count": 0, "small_count": 0, "amount": 0}, "requested_rate_pct": rq},
                    S=100_000_000)
    bm = a["below_min_assessment"]
    ok("0901: уровень низкий, объект новый с документами, убытков нет, ставка покрывает нетто-ставку — «да»",
       a["risk"]["level"] == "low" and bm["verdict"] == "allowed" and bm["requested_source"] == "employee"
       and [c["code"] for c in bm["conditions"]] == ["cond_uw_authority"]
       and "bm_net_ok" in _bm_codes(bm) and "bm_new" in _bm_codes(bm), (a["risk"]["level"], bm.get("text")))
    BM_REPORT["0901 низкий, новый"] = (bm["verdict"], rq, mn)

    set_act_settings({"below_min": {"min_share_pct": 60, "net_check": "always"}})
    st, a = bm_make(optional=dict(BM_OPT, requested_rate_pct=0.07))
    bm = a["below_min_assessment"]
    ok("запрошенная 0,07 % ниже нетто-ставки расчётного модуля 0,18 % (net_check = always) — «нет»: ставка не "
       "покрывает ожидаемый убыток", bm["verdict"] == "not_allowed" and _bm_codes(bm)["bm_net_below"]["effect"] == "blocks"
       and "не покрывает ожидаемый убыток" in _bm_codes(bm)["bm_net_below"]["text"], bm.get("text"))
    set_act_settings(None)

    st, a = bm_make(optional=dict(BM_OPT, requested_rate_pct=0.1))
    ok("запрошенная 0,1 % не ниже минимальной 0,08 % — блока нет (available = false), строка обзора — «не ниже минимума»",
       a["below_min_assessment"]["available"] is False and a["below_min_assessment"]["reason"] == "not_below"
       and {o["code"]: o for o in a["scoring"]["overview"]}["below_min"]["value"] == "не ниже минимума")

    # три языка, Word и PDF
    for lg in ("uz", "en"):
        st, x = call("GET", f"/act/{cond_aid}", params={"lang": lg})
        b = x["below_min_assessment"]
        ok(f"{lg}: оценка на языке акта без кириллицы (ответ, доводы, условия, пометка)",
           b["verdict_label"] == {"uz": "ha, shartlar bilan", "en": "yes, subject to conditions"}[lg]
           and not _cyr(b["text"]) and not any(_cyr(r["text"]) for r in b["reasons"])
           and not any(_cyr(c["text"]) for c in b["conditions"]) and not _cyr(b["note"]), b["text"][:300])
    dx, pf = docx_plain(cond_aid), pdf_plain(cond_aid)
    ok("Word и PDF: одна строка «Цены» — запрошенная ставка ниже минимума, ответ и недобор премии; подробный "
       "подраздел — в JSON и на экране",
       all(s_ in dx and s_ in pf for s_ in ("Запрошенная ставка", "ниже минимума", "да, при условиях",
                                             "300 000 сум"))
       and "Ставка ниже минимальной: можно ли застраховать" not in dx, (len(dx), len(pf)))
    dx_en = docx_plain(cond_aid, "en")
    ok("Word en: «Requested rate … below the minimum» и «yes, subject to conditions»",
       "below the minimum" in dx_en and "yes, subject to conditions" in dx_en)
    return cond_aid


def check_min_rates_admin(old_aid):
    print("45в. Минимальная ставка страховщика: правка администратором, версии по дате, история, импорт, тип ставки")
    fresh()
    model_on(False)
    from app import min_rates as mrs
    today = date.today()
    st, h = call("GET", "/reference/min-rates/0832/history")
    ok("история 0832: одна версия «из тарифной политики, приказ 54-П», 0,08 %, годовая, действует",
       st == 200 and len(h["history"]) == 1 and h["history"][0]["source_label"] == "из тарифной политики, приказ 54-П"
       and h["history"][0]["min_rate_pct"] == 0.08 and h["history"][0]["rate_type"] == "annual"
       and h["history"][0]["active"], h)
    st, _ = call("PUT", "/reference/min-rates/0832", {"min_rate_pct": 0.06})
    ok("правка без входа — отказ (guard)", st in (401, 403), st)
    HEADERS.append(_admin_header("minrate_admin"))
    try:
        st, r = call("PUT", "/reference/min-rates/0832", {"min_rate_pct": 0.06,
                                                          "effective_from": (today - timedelta(days=1)).isoformat()})
        ok("дата начала в прошлом — 422 (старые расчёты воспроизводятся)", st == 422 and "effective_from" in r["errors"], r)
        st, r = call("PUT", "/reference/min-rates/0832", {"min_rate_pct": 0.06, "rate_type": "monthly"})
        ok("неизвестный тип ставки — 422", st == 422 and "rate_type" in r["errors"], r)
        st, r = call("PUT", "/reference/min-rates/0820", {"min_rate_pct": 0.3})
        ok("обязательный вид — 422: ставку устанавливает нормативный акт", st == 422, r)
        st, r = call("PUT", "/reference/min-rates/0832", {"min_rate_pct": 0.06, "effective_from": today.isoformat(),
                                                          "note": "решение правления № 7"})
        ok("PUT 0832: 0,06 % с сегодняшнего дня — новая версия", st == 200 and r["saved"]["min_rate_pct"] == 0.06
           and r["current"]["pct"] == 0.06 and r["current"]["source"] == "admin", r)
        nxt = (today + timedelta(days=1)).isoformat()
        st, r2 = call("PUT", "/reference/min-rates/0832", {"min_rate_pct": 0.07, "effective_from": nxt,
                                                           "note": "с завтрашнего дня"})
        ok("PUT 0832: 0,07 % с завтрашнего дня — версия сохранена, сегодня ещё действует 0,06 %",
           st == 200 and r2["current"]["pct"] == 0.06, r2.get("current"))
        st, h = call("GET", "/reference/min-rates/0832/history")
        hs = h["history"]
        ok("история: три версии — тарифная политика, правка (действует), правка с будущей даты; автор и примечание",
           len(hs) == 3 and [x["source"] for x in hs] == ["policy", "admin", "admin"] and hs[1]["active"]
           and hs[2]["future"] and hs[1]["created_by"] == "minrate_admin" and hs[1]["note"] == "решение правления № 7",
           [(x["source"], x["min_rate_pct"], x["active"]) for x in hs])
        with db.tx() as con:
            on_next = mrs.on_date(con, "0832", nxt)
            on_old = mrs.on_date(con, "0832", "2026-01-15")
        ok("on_date: на завтра — 0,07 %, на 15.01.2026 — 0,08 % из тарифной политики (пересчёт по дате)",
           on_next["pct"] == 0.07 and on_old["pct"] == 0.08 and on_old["source"] == "policy", (on_next, on_old))
        st, lst = call("GET", "/reference/min-rates")
        row = next(x for x in lst["items"] if x["code"] == "0832")
        ok("GET /reference/min-rates: 0832 — 0,06 %, годовая, с сегодняшнего дня, следующая версия 0,07 %",
           row["min_rate_pct"] == 0.06 and row["rate_type"] == "annual" and row["effective_from"] == today.isoformat()
           and row["next"]["min_rate_pct"] == 0.07, row)
    finally:
        HEADERS.clear()

    st, a = bm_make(optional=dict(BM_OPT), request=BM_REQ)
    src = a["min_rate"]["source_text"]
    want = f"минимальная ставка страховщика (установлена администратором, действует с {today.strftime('%d.%m.%Y')}, " \
           f"примечание: решение правления № 7)"
    ok("новый акт: минимум 0,06 % — правка администратора; источник в акте, в «как посчитано» и в вилке",
       a["rate"]["min_pct"] == 0.06 and src == want and any(want in h_ for h_ in a["rate"]["how"])
       and any(want in (m.get("source") or {}).get("title", "") for m in a["rate_fork"]["marks"] if m["code"] == "min"),
       (a["rate"]["min_pct"], src))
    ok("оценка заниженной ставки сравнивает с минимумом страховщика 0,06 %",
       a["below_min_assessment"]["min_pct"] == 0.06 and want in a["below_min_assessment"]["text"])
    st, old = call("GET", f"/act/{old_aid}")
    ok("старый акт — прежний минимум 0,08 % из тарифной политики (данные акта не пересчитываются задним числом)",
       old["rate"]["min_pct"] == 0.08 and old["min_rate"]["source_text"] == "из тарифной политики, приказ 54-П"
       and old["below_min_assessment"]["min_pct"] == 0.08, old["min_rate"])
    with db.tx() as con:
        ref = db.load_reference(con)
    ok("калькулятор видит ту же версию: engine.min_rate(0832) = 0,06 %", min_rate(ref, "0832")["company"] == 0.06)
    st, calc = call("POST", "/calculate", {"product_code": "0832", "class_code": "8", "object_type": "Машины и оборудование",
                                           "value_amount": 1e9, "sum_insured": 1e9, "term_days": 365, "factors": {}})
    ok("/calculate по 0832: минимум 0,06 %", st == 200 and calc["rates"]["min_pct"] == 0.06, (st, calc.get("rates")))

    # импорт из Excel
    import openpyxl
    st, blob, hd = call("GET", "/reference/min-rates/template.xlsx", raw=True)
    ok("шаблон импорта: xlsx с колонками код, ставка, тип, дата, примечание", st == 200 and blob[:2] == b"PK"
       and [c.value for c in openpyxl.load_workbook(io.BytesIO(blob)).active[1]][:4]
       == ["Код продукта", "Минимальная ставка, %", "Тип ставки (annual / fixed)", "Дата начала (ДД.ММ.ГГГГ)"])

    def xl(rows):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Код продукта", "Минимальная ставка, %", "Тип ставки", "Дата начала", "Примечание"])
        for r_ in rows:
            ws.append(r_)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def imp(blob_, apply):
        boundary = "----insonmr"
        crlf = "\r\n"
        payload = (f'--{boundary}{crlf}Content-Disposition: form-data; name="file"; filename="m.xlsx"{crlf}'
                   f'Content-Type: {XLSX_MIME}{crlf}{crlf}').encode() + blob_ + f"{crlf}--{boundary}--{crlf}".encode()
        hdrs = [(b"host", b"test"), (b"content-type", f"multipart/form-data; boundary={boundary}".encode()),
                (b"content-length", str(len(payload)).encode())] + _cookie_hdr()
        return _send("POST", "/reference/min-rates/import", {"apply": apply}, hdrs, payload)

    with db.tx() as con:
        cls8 = [r["code"] for r in db.rows(con, "SELECT p.code FROM products p JOIN product_classes pc ON "
                                                "pc.product_code = p.code WHERE pc.class_code='8' AND p.pricing_mode='ставка' "
                                                "AND p.code NOT IN ('0832') ORDER BY p.code")]
    pa, pb = cls8[0], cls8[1]
    bad = xl([[pa, 0.5, "fixed", today.strftime("%d.%m.%Y"), "импорт"], ["9999", 0.1, "annual", today.isoformat(), ""],
              [pb, "0,5", "годовая", (today - timedelta(days=3)).isoformat(), ""]])
    HEADERS.append(_admin_header("minrate_admin2"))
    try:
        st, r = imp(bad, 0)
        ok("импорт: предпросмотр — 3 строки, 2 с ошибками (нет продукта, дата в прошлом), записать нельзя",
           st == 200 and r["preview"]["rows"] == 3 and r["preview"]["errors"] == 2 and not r["preview"]["can_apply"]
           and r["applied"] is False, r)
        st, r = imp(bad, 1)
        with db.tx() as con:
            n_a = len(mrs.history(con, pa))
        ok("импорт с ошибками и apply=1 — 422, ничего не записано", st == 422 and n_a == 1, (st, n_a))
        good = xl([[pa, 0.5, "fixed", today.strftime("%d.%m.%Y"), "импорт: на весь срок"],
                   [pb, "0,5", "годовая", today.isoformat(), "импорт: годовая"]])
        st, r = imp(good, 0)
        ok("импорт без ошибок: предпросмотр «было → станет»", st == 200 and r["preview"]["can_apply"]
           and r["preview"]["items"][0]["new"]["rate_type"] == "fixed" and r["preview"]["items"][0]["now"]["min_rate_pct"]
           is not None, r)
        st, r = imp(good, 1)
        ok("импорт apply=1: две версии с источником «импорт из Excel»", st == 200 and r["applied"]
           and len(r["saved"]) == 2 and all(x_["source"] == "import" for x_ in r["saved"]), r)
    finally:
        HEADERS.clear()
    st, h = call("GET", f"/reference/min-rates/{pa}/history")
    ok("история после импорта: версия тарифной политики сохранена, новая — импорт, тип fixed; автор скрыт от гостя",
       [x["source"] for x in h["history"]] == ["policy", "import"] and h["history"][1]["rate_type"] == "fixed"
       and h["history"][1]["created_by"] == "администратор", h["history"])

    # тип ставки в акте: 100 млн, 0,5 %, 3 года — 500 000 против 1 500 000
    low = {"location": "guarded", "documents_provided": True, "losses_3y": {"count": 0, "small_count": 0, "amount": 0},
           "term_days": 1095}
    st, af = bm_make(pa, dict(low, request={"tariff_pct": 0.5, "premium": 500_000, "term_days": 1095}), S=100_000_000)
    st2, an = bm_make(pb, dict(low, request={"tariff_pct": 0.5, "premium": 1_500_000, "term_days": 1095}), S=100_000_000)
    itf, ita = rq_items(af), rq_items(an)
    ok("акт fixed: уровень низкий, ставка 0,5 % на весь срок, премия 500 000; годовой эквивалент 0,1667 %",
       af["risk"]["level"] == "low" and af["rate"]["rate_type"] == "fixed" and af["rate"]["applied_pct"] == 0.5
       and af["premium"]["amount"] == 500_000 and af["rate"]["annual_equiv_pct"] == 0.1667,
       (af["risk"]["level"], af["rate"].get("rate_type"), af["premium"]["amount"]))
    ok("акт annual: та же ставка 0,5 % на 1 095 дн. — премия 1 500 000",
       an["rate"]["rate_type"] == "annual" and an["premium"]["amount"] == 1_500_000, an["premium"]["amount"])
    ok("сверка запроса по типу ставки продукта: обе премии сходятся (500 000 и 1 500 000)",
       itf["premium_request"]["verdict"] == "ok" and itf["premium_request"]["calculated"] == 500_000
       and ita["premium_request"]["verdict"] == "ok" and ita["premium_request"]["calculated"] == 1_500_000,
       (itf["premium_request"], ita["premium_request"]))
    rows4 = {r_["label"]: r_ for r_ in af["sections"][3]["rows"]}
    ok("акт fixed: в разделе 4 обе ставки — фиксированная и годовой эквивалент; вилка — в годовом выражении",
       rows4.get("Тип ставки", {}).get("value") == "фиксированная (на весь срок)"
       and "0,1667" in nb(rows4.get("Годовой эквивалент ставки (для сравнения с рынком)", {}).get("value", ""))
       and af["rate_fork"]["rate_type"] == "fixed"
       and next(m for m in af["rate_fork"]["marks"] if m["code"] == "act")["rate_pct"] == 0.1667
       and next(m for m in af["rate_fork"]["marks"] if m["code"] == "act")["premium"] == 500_000,
       (list(rows4), af["rate_fork"].get("marks")))
    ok("акт fixed: в «как посчитано» — премия без деления на срок", any("без деления на срок" in h_
                                                                       for h_ in af["rate"]["how"]))
    BM_REPORT["тип ставки"] = {"fixed": af["premium"]["amount"], "annual": an["premium"]["amount"]}
    # контрольные цифры движка не изменились (склад 4,2 млрд, производство 8/9, спецтехника — tests/test_quick_mode.py)
    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                       "optional": dict(CRANE_OPT, object_kind="truck_crane")})
    ok("автокран: ставка 0,42 % и премия 12 369 000 — прежние", a["rate"]["applied_pct"] == 0.42
       and a["premium"]["amount"] == 12_369_000 and a["rate"]["rate_type"] == "annual")


FA_REPORT = {}


def check_factor_groups():
    print("46. Факторы объекта по подгруппам класса (шаблоны 1.4.1, factor_groups): множитель, справочно и в ставке, "
          "незаполненное — в «уточнить», проверка шаблона, части комплексного продукта, Word и PDF")
    from app import class_templates as ctm
    fresh()
    model_on(False)
    set_act_settings(None)
    data = ctm.load_file()
    t3 = data["classes"]["3"]
    groups = {g["code"]: g for g in t3["factor_groups"]}
    opt3 = {f["code"]: f for f in t3["optional"]}
    ok("шаблоны 1.4.2: у всех 20 шаблонов есть factor_groups, проверка проходит; у каждой группы нейтральный вариант 1,0 "
       "и пометка «экспертно, не калибровано»",
       data["version"] == "1.4.2" and all(t.get("factor_groups") for t in data["classes"].values())
       and all(not ctm.validate(t, c) for c, t in data["classes"].items())
       and all(any(o["coef"] == 1.0 for o in g["options"]) for t in data["classes"].values()
               for g in t["factor_groups"])
       and all("экспертно, не калибровано" in o["note"]["ru"] for t in data["classes"].values()
               for g in t["factor_groups"] for o in g["options"])
       and all(g["calibrated"] == 0 for t in data["classes"].values() for g in t["factor_groups"]))
    ok("класс 3: тип транспорта, топливо и привод, использование, хранение, противоугонная защита, водители; хранение и "
       "защита — ссылки на прежние поля (без повтора), новые — поля выбора optional.class_fields",
       list(groups) == ["veh_group", "fuel", "usage", "location", "protection", "drivers_profile"]
       and groups["location"]["input"] == "optional.location" and groups["protection"]["input"] == "optional.protection"
       and [o["code"] for o in groups["fuel"]["options"]] == ["petrol", "diesel", "lpg", "cng", "hybrid", "electric",
                                                              "other"]
       and opt3["fuel"]["type"] == "choice" and opt3["fuel"]["input"] == "optional.class_fields.fuel"
       and opt3["fuel"]["options"] == [o["code"] for o in groups["fuel"]["options"]]
       and opt3["fuel"]["option_labels"]["electric"]["uz"] == "elektromobil"
       and sum(1 for f in t3["optional"] if f["code"] == "location") == 1, list(groups))

    # --- чистые функции
    up = ae.factor_adjust({"fuel": "diesel", "usage": "taxi_rent"}, t3, None, {"location": "open_area"})
    down = ae.factor_adjust({"fuel": "electric", "usage": "personal"}, t3, None, {"location": "closed_storage"})
    ok("класс 3: дизель × такси × улица = 1,05 × 1,4 × 1,15 = 1,6905 > 1; электро × гараж × личное = 1,15 × 0,85 × "
       "0,9 = 0,8798 < 1",
       up["product"] == 1.6905 and up["product"] > 1 and down["product"] == 0.8798 and down["product"] < 1
       and up["mode"] == "reference" and up["calibrated"] == 0
       and [(a["group"], a["option"], a["coef"]) for a in up["applied"]] ==
       [("fuel", "diesel", 1.05), ("usage", "taxi_rent", 1.4), ("location", "open_area", 1.15)],
       (up["product"], down["product"]))
    ok("незаполненные группы — в unfilled (тип транспорта, защита, водители), ставку не меняют",
       [u["group"] for u in up["unfilled"]] == ["veh_group", "protection", "drivers_profile"]
       and up["unfilled"][0]["label"]["ru"].startswith("Тип транспорта"), up["unfilled"])
    nothing = ae.factor_adjust({}, t3)
    ok("ничего не заполнено — множитель 1, все 6 групп в unfilled",
       nothing["product"] == 1.0 and not nothing["applied"] and len(nothing["unfilled"]) == 6)
    t1 = data["classes"]["1"]
    pa = ae.factor_adjust({"cover_24h": True, "occupation_group": "office"}, t1)
    ok("поле «да/нет» (класс 1, круглосуточное покрытие): да → вариант yes × 1,15; офис × 0,85 → 0,9775",
       pa["product"] == 0.9775 and ("cover_24h", "yes") in [(a["group"], a["option"]) for a in pa["applied"]],
       pa["product"])
    big = ae.factor_adjust({"veh_group": "moto", "fuel": "other", "usage": "taxi_rent", "drivers_profile": "violations"},
                           t3, None, {"location": "construction", "protection": "none"})
    ok("границы множителя: мото 1,4 × прочее топливо 1,25 × такси 1,4 × нарушения 1,3 × стройплощадка 1,2 × без защиты 1,1 = 4,2042 → 2,5 (настройка max_product)",
       big["raw_product"] == 4.2042 and big["product"] == 2.5 and big["clamped"] == "max", big["raw_product"])
    # вклад каждого фактора в сумах: автокран 0318, ставка акта 0,42 %, 2 945 000 000 сум, 365 дней
    S = CRANE_MUST["sum_insured"]
    rr = {"mode": "tariff", "applied_pct": 0.42, "calc_pct": 0.42, "min_pct": 0.35, "premium": 12_369_000,
          "term_days": 365, "rate_type": "annual", "how": []}
    fa = ae.factor_effect(ae.factor_adjust({"fuel": "diesel", "usage": "taxi_rent"}, t3, None,
                                           {"location": "open_area"}), dict(rr), S)
    e = fa["effect"]
    ok("справочно: 0,42 % × 1,6905 = 0,71 %, премия 12 369 000 → 20 909 500; вклады 618 450 + 5 194 980 + 2 727 070 = "
       "8 540 500 сум",
       e["available"] and not e["applied_to_act"] and e["rate_pct"] == 0.71 and e["premium"] == 20_909_500
       and [x["premium_delta"] for x in e["steps"]] == [618_450, 5_194_980, 2_727_070]
       and sum(x["premium_delta"] for x in e["steps"]) == e["delta_premium"] == 8_540_500, e)
    low = ae.factor_adjust({"fuel": "electric", "usage": "personal"}, t3, {"factors": {"mode": "apply"}},
                           {"location": "closed_storage", "protection": "tracker"})
    r2 = dict(rr, how=[{"code": "how_premium", "params": {}}])
    ae.factor_effect(low, r2, S)
    ok("apply: 0,42 % × 0,7918 = 0,3326 % ниже минимума 0,35 % → ставка акта 0,35 %, премия 10 307 500; «как "
       "посчитано» — строки факторов и минимума",
       low["product"] == 0.7918 and r2["applied_pct"] == 0.35 and r2["premium"] == 10_307_500 and r2["min_applied"]
       and r2["factors_applied"] and r2["factor_act_pct"] == 0.42 and low["effect"]["floored"]
       and [h["code"] for h in r2["how"]] == ["how_factors", "how_factors_min", "how_premium"], r2)
    st_ = ae.factor_effect(ae.factor_adjust({"veh_group": "car"}, t3), {"mode": "statutory", "applied_pct": 0.4,
                                                                        "how": []}, S)
    ok("обязательный вид: факторы ставку не меняют (effect.reason = statutory)",
       not st_["effect"]["available"] and st_["effect"]["reason"] == "statutory")
    errs = ae.check_settings({"factors": {"mode": "x", "min_product": 2, "max_product": 0.5, "extra": 1}})
    ok("настройки factors проверяются: режим, границы множителя, лишние ключи",
       any("factors.mode" in x for x in errs) and any("min_product" in x for x in errs)
       and any("max_product" in x for x in errs) and any("неизвестные" in x for x in errs)
       and not ae.check_settings({"factors": {"mode": "apply"}}), errs)
    # --- проверка шаблона
    bad = _json.loads(_json.dumps(t3))
    bad["factor_groups"][1]["options"][1]["coef"] = 5
    bad["factor_groups"][2]["options"] = bad["factor_groups"][2]["options"][:1]
    bad["factor_groups"][3]["options"] = bad["factor_groups"][3]["options"][:-1]
    bad["factor_groups"].append(dict(bad["factor_groups"][0]))
    verrs = " | ".join(ctm.validate(bad, "3"))
    ok("проверка шаблона ловит плохой коэффициент (5 > 3), группу из одного варианта, несовпадение с полем, повтор группы",
       "fuel.diesel.coef" in verrs and "usage.options: не меньше 2" in verrs and "location.options" in verrs
       and "повтор группы veh_group" in verrs, verrs)
    bad2 = _json.loads(_json.dumps(t3))
    bad2["factor_groups"][0]["input"] = "optional.class_fields.nope"
    ok("группа без поля ввода в шаблоне — ошибка", any("nope" in x for x in ctm.validate(bad2, "3")))

    # --- акт: режим по умолчанию (reference) — премия автокрана прежняя
    cf = {"fuel": "diesel", "usage": "taxi_rent"}
    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                       "optional": dict(CRANE_OPT, object_kind="truck_crane", class_fields=cf)})
    fa = a.get("factor_adjustment") or {}
    mk = {m["code"]: m for m in a["rate_fork"]["marks"]}
    ok("автокран 0318, reference: ставка 0,42 % и премия 12 369 000 не меняются; факторы 1,6905 справочно",
       st == 200 and a["rate"]["applied_pct"] == 0.42 and a["premium"]["amount"] == 12_369_000
       and not a["rate"]["factors_applied"] and fa.get("mode") == "reference" and fa.get("product") == 1.6905
       and fa["effect"]["rate_pct"] == 0.71 and fa["effect"]["premium"] == 20_909_500 and fa["calibrated"] == 0,
       (st, a.get("rate", {}).get("applied_pct"), fa.get("product")))
    ok("вилка: отметка «С учётом факторов объекта (справочно)» 0,71 % / 20 909 500, не рекомендуемая",
       mk.get("factors", {}).get("rate_pct") == 0.71 and mk["factors"]["premium"] == 20_909_500
       and not mk["factors"]["is_recommended"] and mk["factors"]["label"] == "С учётом факторов объекта (справочно)"
       and "1,6905" in mk["factors"]["note"], mk.get("factors"))
    ok("factor_adjustment: применённые с подписями и вкладом в сумах, незаполненные — в «уточнить»",
       [(x["group"], x["option"], x["premium_delta"]) for x in fa["applied"]] ==
       [("fuel", "diesel", 618_450), ("usage", "taxi_rent", 5_194_980), ("location", "open_area", 2_727_070)]
       and fa["applied"][0]["label"] == "дизель" and fa["applied"][0]["group_label"] == "Топливо и привод"
       and [u["group"] for u in fa["unfilled"]] == ["veh_group", "protection", "drivers_profile"], fa.get("applied"))
    ex = nb(" ".join(fa.get("explain") or []))
    ok("пояснение построчно: база, каждый фактор (+618 450 сум …), итог 0,42 % → 0,71 %, режим справочно, уточнить",
       "База — ставка акта 0,42 %" in ex and "Топливо и привод: дизель — × 1,05" in ex and "+618 450 сум" in ex
       and "Итоговый множитель 1,6905: ставка 0,42 % → 0,71 %, премия 12 369 000 сум → 20 909 500 сум" in ex
       and "Режим «справочно»" in ex and "Уточнить (ставку не меняет): Тип транспорта" in ex, ex[:600])
    s4, s5 = a["sections"][3], a["sections"][4]
    row = next((r for r in s4["rows"] if r["label"] == "Факторы объекта"), None)
    ok("раздел 4: строка «Факторы объекта: … итоговый множитель 1,6905», режим справочно; перечень после вилки",
       row and "итоговый множитель 1,6905" in row["value"] and "режим справочно" in row["note"]
       and [x["title"] for x in s4["lists"]].index("Факторы объекта по подгруппам класса") ==
       [x["title"] for x in s4["lists"]].index("Вилка ставки") + 1, row)
    ok("раздел 5: фраза о факторах и перечень «Уточнить факторы объекта»",
       any(p.startswith("Факторы объекта (справочно, экспертно): ставка с их учётом 0,71") for p in s5["paragraphs"])
       and any(li["title"] == "Уточнить факторы объекта" and len(li["items"]) == 3 for li in s5["lists"]),
       s5["paragraphs"])
    aid = a["id"]
    st, g = call("GET", f"/act/{aid}", params={"lang": "uz"})
    ok("GET /act/{id}?lang=uz: тот же factor_adjustment на узбекском",
       st == 200 and g["factor_adjustment"]["product"] == 1.6905
       and g["factor_adjustment"]["applied"][0]["label"] == "dizel"
       and any("Yakuniy koeffitsiyent 1,6905" in x for x in g["factor_adjustment"]["explain"]),
       g.get("factor_adjustment", {}).get("explain"))
    dx, pf = docx_plain(aid), pdf_plain(aid)
    ok("Word и PDF собираются; режим справочно — перечень факторов в JSON и на экране, в документе на один лист нет",
       "Факторы объекта по подгруппам класса" not in dx and "Факторы объекта по подгруппам класса" not in pf
       and "СЮРВЕЙЕРСКИЙ АКТ" in dx and a["factor_adjustment"]["explain"],
       (dx.count("Факторы объекта"), pf.count("Факторы объекта")))
    FA_REPORT["автокран reference"] = (a["rate"]["applied_pct"], a["premium"]["amount"], fa["product"],
                                       fa["effect"]["rate_pct"], fa["effect"]["premium"])
    # неверное значение поля выбора — 422
    st, e = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                       "optional": dict(CRANE_OPT, class_fields={"fuel": "coal"})})
    ok("неизвестный вариант поля класса (топливо «coal») — 422", st == 422 and "fuel" in str(e), (st, str(e)[:200]))

    # --- режим apply: ставка акта = тариф × уровень × множитель, не ниже минимума
    set_act_settings({"factors": {"mode": "apply"}})
    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                       "optional": dict(CRANE_OPT, object_kind="truck_crane", class_fields=cf)})
    fa = a["factor_adjustment"]
    ok("apply: ставка акта 0,35 × 1,2 × 1,6905 = 0,71 %, премия 20 909 500; вилка рекомендует ставку акта, отметки "
       "factors нет",
       st == 200 and a["rate"]["applied_pct"] == 0.71 and a["premium"]["amount"] == 20_909_500
       and a["rate"]["factors_applied"] and a["rate"]["factor_act_pct"] == 0.42 and fa["effect"]["applied_to_act"]
       and "factors" not in {m["code"] for m in a["rate_fork"]["marks"]}
       and a["rate_fork"]["recommended"]["rate_pct"] == 0.71
       and any(h.startswith("Факторы объекта × 1,6905") for h in a["rate"]["how"]), (a["rate"]["applied_pct"],
                                                                                    a["premium"]["amount"]))
    row = next((r for r in a["sections"][3]["rows"] if r["label"] == "Факторы объекта"), {})
    ok("apply: строка раздела 4 — «режим применён», раздел 5 — «Ставка акта учитывает факторы объекта»",
       "режим применён" in (row.get("note") or "")
       and any(p.startswith("Ставка акта учитывает факторы объекта") for p in a["sections"][4]["paragraphs"]), row)
    FA_REPORT["автокран apply"] = (a["rate"]["applied_pct"], a["premium"]["amount"])
    st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST,
                                       "optional": dict(CRANE_OPT, location="closed_storage", protection="tracker",
                                                        object_kind="truck_crane",
                                                        class_fields={"fuel": "electric", "usage": "personal"})})
    ok("apply: множитель 0,7918 опускает ставку ниже минимума 0,35 % — применён минимум, премия 10 307 500",
       st == 200 and a["factor_adjustment"]["product"] == 0.7918 and a["rate"]["applied_pct"] == 0.35
       and a["rate"]["applied_pct"] >= a["rate"]["min_pct"] and a["premium"]["amount"] == 10_307_500
       and a["rate"]["min_applied"], (a["rate"]["applied_pct"], a["premium"]["amount"]))
    FA_REPORT["автокран apply, ниже минимума"] = (a["rate"]["applied_pct"], a["premium"]["amount"])

    # --- комплексный продукт 0305 (ТС + НС + ОТВ): факторы по шаблону класса каждой части
    must = {"product_code": "0305", "sum_insured": 400_000_000, "object_value": 100_000_000, "region": "Ташкент"}
    parts = [{"class_code": "3", "sum_insured": 100_000_000,
              "fields": {"class_fields": {"fuel": "diesel", "usage": "taxi_rent"}}},
             {"class_code": "1", "sum_insured": 200_000_000, "fields": {"class_fields": {"occupation_group": "office"}}},
             {"class_code": "13", "sum_insured": 100_000_000}]
    set_act_settings(None)
    st, k0 = _pt_make(must, {"losses_3y": {"count": 0}, "documents_provided": True,
                             "parts": [{"class_code": p["class_code"], "sum_insured": p["sum_insured"]} for p in parts]})
    st, k = _pt_make(must, {"losses_3y": {"count": 0}, "documents_provided": True, "parts": parts})
    fk = k.get("factor_adjustment") or {}
    pp = {p["class_code"]: p for p in fk.get("parts") or []}
    ok("0305 reference: факторы по частям (класс 3 — 1,47; класс 1 — 0,85; класс 13 — не заполнено); премии частей и "
       "договора прежние",
       st == 200 and fk.get("by_parts") and pp["3"]["product"] == 1.47 and pp["1"]["product"] == 0.85
       and pp["13"]["product"] == 1.0 and [u["group"] for u in pp["13"]["unfilled"]] == ["activity_kind"]
       and [p["premium"] for p in k["parts"]["items"]] == [p["premium"] for p in k0["parts"]["items"]]
       and k["premium"]["amount"] == k0["premium"]["amount"], (fk.get("parts"), k["premium"]["amount"]))
    set_act_settings({"factors": {"mode": "apply"}})
    st, ka = _pt_make(must, {"losses_3y": {"count": 0}, "documents_provided": True, "parts": parts})
    pa_ = ka["parts"]["items"]
    ok("0305 apply: меняется ставка частей 3 (× 1,47) и 1 (× 0,85, не ниже минимума 0,5 %), часть 13 — прежняя",
       st == 200 and pa_[0]["rate"]["applied_pct"] > k["parts"]["items"][0]["rate"]["applied_pct"]
       and pa_[1]["rate"]["applied_pct"] >= pa_[1]["rate"]["min_pct"]
       and pa_[2]["rate"]["applied_pct"] == k["parts"]["items"][2]["rate"]["applied_pct"]
       and all(p["rate"]["applied_pct"] >= p["rate"]["min_pct"] for p in pa_),
       [(p["rate"]["applied_pct"], p["rate"]["min_pct"]) for p in pa_])
    FA_REPORT["0305 по частям, reference"] = ([p["premium"] for p in k["parts"]["items"]], k["premium"]["amount"])
    FA_REPORT["0305 по частям, apply"] = ([p["rate"]["applied_pct"] for p in pa_], ka["premium"]["amount"])
    set_act_settings(None)


# фон региона к факторам (stat_ref, 02.10.2026): выдуманные ряды stat_series на копии базы
FS_WALLS = {"housing_fund_by_walls_brick": 50.0, "housing_walls_raw_brick": 10.0, "housing_walls_panel_rc": 25.0,
            "housing_walls_other": 5.0, "housing_walls_adobe": 10.0}
FS_IDS = list(FS_WALLS) + ["gas_supply_share"]


def _fs_put(con, region: str, vals: dict, period: str = "2025") -> None:
    """Выдуманные значения наборов фона по региону (только копия базы теста): прежние строки региона — удалить."""
    con.execute("DELETE FROM stat_series WHERE region=? AND dataset_id IN (%s)" % ",".join("?" * len(FS_IDS)),
                [region] + FS_IDS)
    for ds, v in vals.items():
        con.execute("INSERT INTO stat_series (source, dataset_id, key, region, period, value, unit, fetched_at, url) "
                    "VALUES ('stat.uz', ?, 'test', ?, ?, ?, 'тест', '2026-10-02T09:00:00+05:00', "
                    "'https://stat.uz/ru/ofitsialnaya-statistika/environment')", (ds, region, period, v))


def check_factor_stat():
    print("46а. Фон региона к факторам объекта (stat_ref класса 8, stat.uz): доля материала стен в жилищном фонде, "
          "газ, нет данных, проверка шаблона, акт")
    from app import act_analytics as aa
    from app import class_templates as ctm
    from app import stat_sources as ss
    fresh()
    model_on(False)
    set_act_settings(None)
    data = ctm.load_file()
    t8 = data["classes"]["8"]
    g8 = {g["code"]: g for g in t8["factor_groups"]}
    refs = {c: [(r["dataset_id"], r["option_codes"]) for r in g8[c].get("stat_ref") or []]
            for c in ("construction", "walls_special", "heating")}
    ok("шаблон 8: stat_ref у «конструкции» (кирпич и железобетон → 1256 + 1258, дерево → 1259), «особенностей» "
       "(саман → 1257 + 1260, лёгкие → 1259) и «отопления» (газ → 1243); наборы — в реестре stat_sources",
       refs["construction"] == [("housing_fund_by_walls_brick", ["reinforced"]),
                                ("housing_walls_panel_rc", ["reinforced"]), ("housing_walls_other", ["wood"])]
       and refs["walls_special"] == [("housing_walls_raw_brick", ["adobe"]), ("housing_walls_adobe", ["adobe"]),
                                     ("housing_walls_other", ["light"])]
       and refs["heating"] == [("gas_supply_share", ["auto", "stoves"])]
       and [ss.DATASETS[d]["src_id"] for d in ae.WALL_FUND] == ["1256", "1257", "1258", "1259", "1260"]
       and ss.DATASETS["gas_supply_share"]["src_id"] == "1243"
       and all(ss.DATASETS[d]["class_codes"] == ["8"] and ss.DATASETS[d]["regions"] for d in FS_IDS)
       and not ctm.check_factor_groups(t8), refs)

    # --- проверка stat_ref шаблона
    bad = _json.loads(_json.dumps(t8))
    gb = {g["code"]: g for g in bad["factor_groups"]}
    gb["construction"]["stat_ref"][0]["dataset_id"] = "no_such_dataset"
    gb["heating"]["stat_ref"][0]["option_codes"] = ["gas_boiler"]
    gb["walls_special"]["stat_ref"] = {"dataset_id": "housing_walls_adobe"}
    errs = ctm.check_factor_groups(bad)
    ok("check_factor_groups: неизвестный dataset_id, вариант не из группы и stat_ref не списком — ловятся",
       any("construction.stat_ref[0].dataset_id" in e and "no_such_dataset" in e for e in errs)
       and any("heating.stat_ref[0].option_codes" in e and "gas_boiler" in e for e in errs)
       and any("walls_special.stat_ref: список" in e for e in errs) and len(errs) == 3
       and bool(ctm.validate(bad, "8")), errs)

    # --- чистая функция: доля за последний общий год пяти наборов
    ser = {d: {"2024": {"value": v, "unit": "тыс. кв. м", "url": "u", "fetched_at": "f"}} for d, v in FS_WALLS.items()}
    for d in list(FS_WALLS)[:4]:
        ser[d]["2025"] = {"value": 999.0, "unit": "тыс. кв. м", "url": "u", "fetched_at": "f"}   # без глинобитных
    rr = ae.option_stat_refs(g8["construction"], "reinforced")
    s1 = ae.factor_stat(rr, ser, "region:TEST", "Тест", ss.DATASETS)
    ok("factor_stat: кирпич и железобетон (50 + 25) / 100 = 75 %; год — последний, где есть все пять наборов (2024)",
       s1["available"] and s1["share_pct"] == 75.0 and s1["period"] == "2024" and s1["value"] == 75.0
       and s1["total"] == 100.0 and s1["kind"] == "walls_share" and s1["calibrated"] == 0
       and s1["note"] == "фон региона, коэффициент не меняет" and s1["source_ids"] == ["1256", "1258"], s1)
    s0 = ae.factor_stat(rr, {d: v for d, v in ser.items() if d != "housing_walls_adobe"}, "region:TEST", "Тест",
                        ss.DATASETS)
    ok("factor_stat: нет одного из пяти наборов — available false (no_data), доля не выдумывается",
       s0["available"] is False and s0["reason"] == "no_data" and "housing_walls_adobe" in s0["reason_text"]
       and "share_pct" not in s0, s0)

    # --- через базу (stat_series копии): выдуманные ряды по регионам
    with db.tx() as con:
        _fs_put(con, "region:TOSHKENT", dict(FS_WALLS, gas_supply_share=88.8))
        _fs_put(con, "region:XORAZM", {"housing_fund_by_walls_brick": 12.0, "housing_walls_raw_brick": 30.0,
                                       "housing_walls_panel_rc": 3.0, "housing_walls_other": 15.0,
                                       "housing_walls_adobe": 40.0})
        _fs_put(con, "region:NAVOIY", {})
    with db.tx() as con:
        fa = aa.factor_stats(con, ae.factor_adjust({"walls_special": "adobe", "heating": "stoves"}, t8, None,
                                                   {"construction": "reinforced"}), "Хорезмская область")
        fa_t = aa.factor_stats(con, ae.factor_adjust({"heating": "auto"}, t8, None, {"construction": "wood"}),
                               "Ташкентская область")
        fa_n = aa.factor_stats(con, ae.factor_adjust({"heating": "central"}, t8, None, {"construction": "reinforced"}),
                               "Навоийская область")
        fa_u = aa.factor_stats(con, ae.factor_adjust({}, t8, None, {"construction": "reinforced"}), "Марс")
    sx = {a["group"]: a.get("stat") for a in fa["applied"]}
    st_t = {a["group"]: a.get("stat") for a in fa_t["applied"]}
    ok("Хорезм (выдумано): кирпич + железобетон (12 + 3) / 100 = 15 %, саман (30 + 40) = 70 %; газа в базе нет — "
       "available false; коэффициенты и множитель прежние (0,9 × 1,25 × 1,3)",
       sx["construction"]["share_pct"] == 15.0 and sx["walls_special"]["share_pct"] == 70.0
       and sx["construction"]["region"] == "region:XORAZM" and sx["construction"]["region_name"] == "Хорезмская область"
       and sx["construction"]["url"] == "https://stat.uz/ru/ofitsialnaya-statistika/environment"
       and sx["heating"]["available"] is False and sx["heating"]["reason"] == "no_data"
       and fa["product"] == round(0.9 * 1.25 * 1.3, 4), sx)
    ok("Ташкентская область (выдумано): дерево → «прочие» 5 / 100 = 5 %; газ — сам показатель 88,8 %",
       st_t["construction"]["share_pct"] == 5.0 and st_t["construction"]["dataset"] == ["housing_walls_other"]
       and st_t["heating"]["share_pct"] == 88.8 and st_t["heating"]["kind"] == "value"
       and st_t["heating"]["period"] == "2025" and st_t["heating"]["url"].startswith("https://stat.uz/"), st_t)
    ok("Навои без данных — available false с причиной; центральное отопление ссылки на газ не имеет (блока нет); "
       "регион не опознан — region_unknown",
       fa_n["applied"][0]["stat"]["available"] is False and fa_n["applied"][0]["stat"]["reason"] == "no_data"
       and "stat" not in fa_n["applied"][1] and "stat_ref" not in fa_n["applied"][1]
       and fa_u["applied"][0]["stat"]["reason"] == "region_unknown",
       (fa_n["applied"], fa_u["applied"][0].get("stat")))

    # --- акт: блок stat в JSON, строка explain и строка раздела 4
    st, a = call("POST", "/act/make", {"lang": "ru", "must": WH8_MUST,
                                       "optional": dict(WH8_OPT, class_fields={"heating": "stoves"})})
    fa = a.get("factor_adjustment") or {}
    ap = {x["group"]: x for x in fa.get("applied") or []}
    sb = (ap.get("construction") or {}).get("stat") or {}
    secs = a.get("sections") or []
    rows4 = [r_ for r_ in (secs[3].get("rows") or [] if len(secs) > 3 else [])
             if str(r_.get("label", "")).startswith("Фон региона (stat.uz)")]
    ok("акт 0807, Ташкентская область: у конструкции блок stat (75 %, 2025, ссылка stat.uz, «фон региона, "
       "коэффициент не меняет»), у отопления — газ 88,8 %; строки explain и раздела 4",
       st == 200 and sb.get("available") and sb.get("share_pct") == 75.0 and sb.get("period") == "2025"
       and sb.get("url") == "https://stat.uz/ru/ofitsialnaya-statistika/environment"
       and sb.get("note") == "фон региона, коэффициент не меняет"
       and ap["heating"]["stat"]["share_pct"] == 88.8 and ap["construction"]["coef"] == 0.9
       and any("По данным stat.uz: Ташкентская область, конец 2025 года" in x and "75,0" in x
               and "жилищного фонда" in x for x in fa.get("explain") or [])
       and len(rows4) == 2 and "88,8" in rows4[1]["value"],
       (st, sb, [r_["label"] for r_ in rows4]))
    FA_REPORT["фон stat.uz, акт 0807 (выдумано)"] = (sb.get("share_pct"), ap.get("heating", {}).get("stat", {})
                                                     .get("share_pct"))


def check_exchange():
    print("47. Справка биржи УзРТСБ (uzex.uz) в разделе 3: акт 0701, груз — дизельное топливо; выдуманные строки "
          "exchange_quotes в копии базы; без строк — available false")
    from datetime import timedelta
    from app import uzex_sources as us
    fresh()
    model_on(False)
    set_act_settings(None)
    body = {"lang": "ru", "must": {"product_code": "0701", "sum_insured": 800_000_000, "object_value": 800_000_000,
                                   "region": "Ташкент"},
            "optional": {"class_fields": {"cargo_kind": "дизельное топливо", "cargo_group": "bulk",
                                          "transport_mode": "auto", "route": "Навои — Ташкент"}}}
    with db.tx() as con:
        con.execute("DELETE FROM exchange_quotes")
    st, a0 = call("POST", "/act/make", body)
    ex0 = (a0.get("analytics") or {}).get("exchange") if isinstance(a0, dict) else None
    ok("без строк биржи: акт 200, analytics.exchange.available = false (no_data), в разделе 3 справки нет",
       st == 200 and ex0 and ex0["available"] is False and ex0.get("reason") == "no_data"
       and "Справка биржи" not in all_text(a0), (st, ex0))
    rows = []
    for i, (price, days_ago) in enumerate(((14_200_000, 1), (14_800_000, 2), (14_500_000, 3))):
        td = (date.today() - timedelta(days=days_ago)).isoformat()
        rows.append({"page": "List", "contract_no": "9%d" % i, "deal_key": "9%d|%s|60|x|1" % (i, td),
                     "name": "Дизельное топливо ЭКО (тест)", "grp": "diesel", "lot_qty": 60, "unit": "тонна",
                     "unit_norm": "т", "price_raw": price * 60, "price_lot": price * 60, "price_unit": price,
                     "price_unit_norm": price, "price_basis": "тест", "currency": "UZS", "warehouse": None,
                     "trade_date": td, "date_basis": us.DATE_DEAL, "contract_type": "внутренний",
                     "deal_status": None, "fetched_at": db.now(), "fetched_date": date.today().isoformat(),
                     "url": us.page_url("List")})
    with db.tx() as con:
        us.save_rows(con, rows)
    st, a = call("POST", "/act/make", body)
    ex = a["analytics"]["exchange"]
    it = (ex.get("items") or [{}])[0]
    s3 = a["sections"][2]
    row = next((r for r in s3["rows"] if r["label"] == "Справка биржи УзРТСБ"), None)
    ok("со строками: справка есть — дизельное топливо, медиана 14 500 000 сум/т по 3 сделкам, ссылка uzex.uz, "
       "пометка «стоимость объекта не меняет»",
       st == 200 and ex["available"] and it.get("group") == "diesel" and it.get("median_unit_price") == 14_500_000
       and it.get("deals") == 3 and it.get("url", "").startswith("https://uzex.uz/")
       and "стоимость объекта не меняет" in ex["note"], ex)
    ok("раздел 3: строка «Справка биржи УзРТСБ: дизельное топливо — медиана 14 500 000 сум/т по 3 сделкам, последняя "
       "дата …, источник uzex.uz» и строка источника со ссылкой",
       row is not None and "дизельное топливо — медиана 14 500 000 сум/т по 3 сделкам" in row["value"]
       and (date.today() - timedelta(days=1)).strftime("%d.%m.%Y") in row["value"] and "uzex.uz" in row["value"]
       and any("https://uzex.uz/Trade/List" in ln for ln in s3["source_lines"]), (row, s3["source_lines"]))
    ok("стоимость объекта и ставка от справки не зависят",
       a["value"]["ratio_pct"] == a0["value"]["ratio_pct"] and a["rate"]["applied_pct"] == a0["rate"]["applied_pct"])
    st, au = call("POST", "/act/make", dict(body, lang="uz"))
    ok("на узбекском — справка тоже есть, без кириллицы в подписи",
       st == 200 and au["analytics"]["exchange"]["available"]
       and not _cyr(next(r["label"] for r in au["sections"][2]["rows"] if "birja" in r["label"])))
    off = dict(body, optional={"class_fields": {"cargo_kind": "смартфоны", "cargo_group": "valuable",
                                                "transport_mode": "auto", "route": "Ташкент — Самарканд"}})
    st, a2 = call("POST", "/act/make", off)
    ok("груз «смартфоны» — справка не нужна (not_relevant), в разделе 3 её нет",
       st == 200 and a2["analytics"]["exchange"]["available"] is False
       and a2["analytics"]["exchange"]["reason"] == "not_relevant" and "Справка биржи" not in all_text(a2))
    with db.tx() as con:
        con.execute("DELETE FROM exchange_quotes")


# ------------------------------------------------------------------ 48–52. доработки 02.10.2026 (вечер)

FLEET = [
    {"label": "Truck A (test)", "sum_insured": 900_000_000, "object_value": 1_000_000_000, "year": 2019,
     "class_fields": {"veh_group": "truck", "fuel": "diesel"}, "mileage": 120_000, "plate_hint": "…123"},
    {"label": "Car B (test)", "sum_insured": 300_000_000, "object_value": 250_000_000, "year": 2024,
     "class_fields": {"veh_group": "car", "fuel": "petrol"}, "requested_rate_pct": 0.05},
    {"label": "Excavator C (test)", "sum_insured": 1_745_000_000, "object_value": 1_850_000_000, "year": 2010,
     "object_kind": "excavator", "condition": "worn"},
]
FLEET_OPT = {"location": "open_area", "losses_3y": {"count": 0, "small_count": 0, "amount": 0}}
OBJ_REPORT = {}


def fleet_make(objects=None, must=None, optional=None, lang="ru", session=None):
    body = {"lang": lang, "must": dict({"product_code": "0318", "region": "tashkent_region"}, **(must or {})),
            "optional": dict(FLEET_OPT, objects=objects if objects is not None else FLEET, **(optional or {}))}
    if session:
        body["session"] = session
    return call("POST", "/act/make", body)


def _obj_expected(con, ob: dict, level: str) -> tuple:
    """Ставка и премия объекта вручную: act_engine.rate по справочнику копии базы (тариф × поправка уровня, минимум)."""
    ref = db.load_reference(con)
    st = act.load_settings(con)
    prod = db.rows(con, "SELECT code, name, pricing_mode, rate_text FROM products WHERE code='0318'")[0]
    kind_type = tx.OBJECT_KINDS[ob["object_kind"]][0] if ob.get("object_kind") in tx.OBJECT_KINDS else None
    otype = ae.match_object_type(ref, "3", kind_type, None)
    r = ae.rate(ref, prod, "3", level, ob["sum_insured"], 365, otype, None, st)
    return r["applied_pct"], r["premium"]


def check_objects():
    print("48. Несколько объектов в одном акте (парк ТС): у каждого свой уровень, ставка, премия; итоги договора")
    fresh()
    model_on(False)
    set_act_settings(None)
    st, a = fleet_make()
    ok("акт на 3 объекта: 200, суммы договора посчитаны по объектам",
       st == 200 and a["objects_total"]["count"] == 3 and a["objects_total"]["sum_insured"] == 2_945_000_000
       and a["objects_total"]["object_value"] == 3_100_000_000 and a["objects_total"]["sums_from_objects"], (st, a))
    objs = a["objects"]
    with db.tx() as con:
        exp = [_obj_expected(con, ob, o["level"]) for ob, o in zip(FLEET, objs)]
    ok("ставка каждого объекта — тариф × поправка его уровня (не ниже минимума), премия — от его суммы",
       all(o["rate"]["applied_pct"] == e[0] and o["premium"] == e[1] for o, e in zip(objs, exp)),
       [(o["rate"]["applied_pct"], o["premium"], e) for o, e in zip(objs, exp)])
    ok("уровни разные: экскаватор 2010 г. с износом — высокий, грузовик и легковой — не высокий",
       objs[2]["level"] == "high" and objs[0]["level"] != "high" and objs[1]["level"] != "high",
       [o["level"] for o in objs])
    total = sum(o["premium"] for o in objs)
    ok("премия договора = сумма премий объектов (premium.amount, objects_total.premium)",
       a["premium"]["amount"] == total == a["objects_total"]["premium"], (a["premium"], total))
    avg = round(total / 2_945_000_000 * 100, 4)
    ok("средняя ставка — справочно: сумма премий / сумма × 365 / срок; уровень договора — самый высокий",
       a["objects_total"]["rate_avg_pct"] == avg and a["objects_total"]["reference_only"]
       and a["risk"]["level"] == "high" and a["objects_total"]["worst_index"] == 3, a["objects_total"])
    ok("сумма к стоимости по каждому объекту: легковой 120 % — выше стоимости, остальные в норме",
       [o["value"]["verdict"] for o in objs] == ["normal", "over", "normal"]
       and any("Car B (test)" in c and "938" in c for c in a["decision"]["checks"]), a["decision"]["checks"])
    ok("запрошенная ставка объекта ниже минимума — пометка у объекта и проверка андеррайтеру",
       objs[1]["below_min"] and any("Car B (test)" in c and "0,05" in c for c in a["decision"]["checks"]))
    sd = a["objects_total"]["scenarios"]
    sc = a["scenarios"]
    ok("сценарии: PML и EML — по самому крупному объекту (№ 3), MFL — сумма по объектам",
       sd["largest"]["index"] == 3 and sc["pml"]["amount"] == sd["largest"]["PML"]
       and sc["eml"]["amount"] == sd["largest"]["EML"] and sc["mfl"]["amount"] == sd["sum"]["MFL"]
       and sd["sum"]["MFL"] == sum(o["scenarios"]["mfl"] for o in objs)
       and sc["pml"]["amount"] <= sc["eml"]["amount"] <= sc["mfl"]["amount"], (sd, sc.get("pml")))
    s1, s3 = a["sections"][0], a["sections"][2]
    t1 = next((li["table"] for li in s1.get("lists") or [] if li.get("table")), None)
    t3 = next((li["table"] for li in s3.get("lists") or [] if li.get("table")
               and li["title"] == "Сумма к стоимости по объектам"), None)
    ok("раздел 1: таблица объектов (№, объект, год, сумма, стоимость, уровень, ставка, премия)",
       t1 and t1["columns"] == ["№", "Объект", "Год", "Страховая сумма", "Стоимость", "Уровень риска", "Ставка",
                                "Премия"] and len(t1["rows"]) == 3 and t1["rows"][2][1] == "Excavator C (test)", t1)
    ok("раздел 3: та же таблица плюс сумма к стоимости по объекту",
       t3 and len(t3["columns"]) == 9 and "выше стоимости" in t3["rows"][1][8], t3)
    ok("раздел 4: строка средней ставки и премии договора, перечень «Ставка и премия по объектам»",
       any(r["label"] == "Средняя ставка по объектам" for r in a["sections"][3]["rows"])
       and any(li["title"] == "Ставка и премия по объектам" and len(li["items"]) == 3
               for li in a["sections"][3]["lists"]))
    ok("вилка договора — сумма премий объектов по отметкам; рынок класса — общий",
       a["rate_fork"]["reason"] == "parts_reference"
       and next(m for m in a["rate_fork"]["marks"] if m["code"] == "act")["premium"] == total
       and any(m["code"] == "market" for m in a["rate_fork"]["marks"]), a["rate_fork"].get("marks"))
    ok("факторы — по каждому объекту (свои поля класса)",
       a["factor_adjustment"].get("by_objects") and len(a["factor_adjustment"]["objects"]) == 3
       and a["factor_adjustment"]["objects"][0]["product"] != a["factor_adjustment"]["objects"][1]["product"],
       a["factor_adjustment"].get("objects"))
    OBJ_REPORT["парк из 3 ТС"] = {"премии": [o["premium"] for o in objs], "ставки": [o["rate_pct"] for o in objs],
                                  "уровни": [o["level"] for o in objs], "премия договора": total,
                                  "средняя ставка": a["objects_total"]["rate_avg_pct"],
                                  "PML/EML/MFL": (sc["pml"]["amount"], sc["eml"]["amount"], sc["mfl"]["amount"])}
    # один объект в перечне = обычный акт: та же премия
    one = dict(FLEET[0])
    st, single = call("POST", "/act/make", {"lang": "ru", "must": {"product_code": "0318", "region": "tashkent_region",
                                                                 "sum_insured": one["sum_insured"],
                                                                 "object_value": one["object_value"]},
                                           "optional": dict(FLEET_OPT, year=one["year"],
                                                            class_fields=one["class_fields"])})
    st2, f1 = fleet_make([one])
    ok("перечень из одного объекта = обычный акт по тому же объекту (ставка и премия совпадают)",
       st == 200 and st2 == 200 and f1["premium"]["amount"] == single["premium"]["amount"]
       and f1["objects"][0]["rate"]["applied_pct"] == single["rate"]["applied_pct"],
       (single["premium"], f1["premium"]))
    # сумма договора введена и не сходится — 422; сходится в пределах допуска — 200
    st, r = fleet_make(must={"sum_insured": 3_000_000_000})
    ok("введённая сумма не равна сумме по объектам — 422 с разницей", st == 422 and "sum_insured" in r["errors"]
       and "55 000 000" in r["errors"]["sum_insured"], r)
    st, r = fleet_make(must={"sum_insured": 2_945_000_000, "object_value": 3_100_000_000})
    ok("введённые суммы совпали — 200", st == 200 and r["objects_total"]["sums_from_objects"] is False, st)
    st, r = fleet_make(optional={"parts": [{"class_code": "3", "sum_insured": 1}, {"class_code": "8",
                                                                                   "sum_insured": 1}]})
    ok("перечень объектов вместе с частями — 422 с понятной ошибкой",
       st == 422 and "части комплексного продукта" in r["errors"].get("objects", ""), r)
    st, r = fleet_make(must={"product_code": "0305", "region": "samarkand"})
    ok("комплексный продукт (несколько классов) — 422: перечень только у продукта одного класса",
       st == 422 and "несколько классов" in r["errors"].get("objects", ""), r)
    st, r = fleet_make([dict(FLEET[0])] * 51)
    ok("больше 50 объектов — 422", st == 422 and "до 50" in r["errors"].get("objects", ""), r)
    st, r = fleet_make([dict(FLEET[0], plate_hint="01 A 123 BC")])
    ok("полный госномер в plate_hint — 422 (только часть номера)", st == 422 and "госномер" in r["errors"]["objects"], r)
    bad_hints = [fleet_make([dict(FLEET[0], plate_hint=h)])[0] for h in ("A123BC", "…123AB", "12345", "AB")]
    ok("plate_hint с буквами или больше 4 цифр — 422", bad_hints == [422] * 4, bad_hints)
    good_hints = [fleet_make([dict(FLEET[0], plate_hint=h)])[0] for h in ("…123", "...0457", "12")]
    ok("plate_hint «…123», «...0457», «12» — принимаются", good_hints == [200] * 3, good_hints)
    st, r = fleet_make(optional={"deductible": {"amount": 10 ** 13, "type": "unconditional"}})
    ok("парк: франшиза суммой больше половины суммы парка — 422 (проверка после суммы по объектам)",
       st == 422 and "deductible" in r["errors"], r)
    st, r = fleet_make(optional={"deductible": {"amount": 1_000_000, "type": "unconditional"}})
    ok("парк: франшиза суммой в пределах половины суммы парка — 200", st == 200, (st, r.get("errors")))
    st, r = fleet_make([dict(FLEET[0], label="Тестов Тест Тестович")])
    ok("ФИО в подписи объекта — 422", st == 422 and "персональные" in r["errors"]["objects"], r)
    st, r = fleet_make([dict(FLEET[0], sum_insured=0)])
    ok("сумма объекта 0 — 422", st == 422 and "sum_insured" in r["errors"]["objects"], r)
    st, r = fleet_make([dict(FLEET[0], photo_ids=["x9"])])
    ok("неверный id файла — 422", st == 422 and "photo_ids" in r["errors"]["objects"], r)
    # франшиза договора — тем же множителем у каждого объекта
    st, fr = fleet_make(optional={"deductible": {"pct": 1, "type": "unconditional"}})
    fo = fr["objects"]
    ok("франшиза договора: у каждого объекта премия ниже, премия договора = сумма премий объектов с франшизой",
       st == 200 and all(o["franchise_applied"] and o["premium"] < o["premium_before_franchise"] for o in fo)
       and fr["premium"]["amount"] == sum(o["premium"] for o in fo)
       and fr["premium"]["before_franchise"] == sum(o["premium_before_franchise"] for o in fo),
       [(o["premium_before_franchise"], o["premium"]) for o in fo])
    # три языка, Word и PDF
    aid = a["id"]
    st, en = call("GET", f"/act/{aid}", params={"lang": "en"})
    ok("GET ?lang=en: те же объекты и цифры, подписи на английском",
       st == 200 and [o["premium"] for o in en["objects"]] == [o["premium"] for o in objs]
       and en["objects"][2]["level_label"] == "high"
       and any(li.get("title") == "Objects of the contract (3)" for li in en["sections"][0]["lists"]), en.get("objects"))
    st, body, _h = call("GET", f"/act/{aid}.docx", raw=True)
    xml = zipfile.ZipFile(io.BytesIO(body)).read("word/document.xml").decode("utf-8")
    ok("Word: таблица объектов в разделе 1 (раздел 3 — одной строкой, сумма к стоимости по объектам — в JSON)",
       st == 200 and xml.count("Excavator C (test)") >= 1 and "Объекты договора (3)" in xml
       and "Сумма к стоимости по объектам" not in xml)
    st, body, _h = call("GET", f"/act/{aid}.pdf", params={"lang": "uz"}, raw=True)
    with pymupdf.open(stream=body, filetype="pdf") as d:
        txt = pdf_text(d)
    ok("PDF на узбекском: таблица объектов", st == 200 and "Shartnoma obyektlari (3)" in _norm(txt), _norm(txt)[:300])
    single_js = single
    ok("обычный акт: objects = [], objects_total = null", single_js["objects"] == [] and single_js["objects_total"]
       is None)


def check_objects_photos():
    print("48а. Парк ТС: фото привязаны к объектам (photo_ids) — у каждого объекта свой осмотр")
    fresh()
    model_on(True)
    REPLY["text"] = "```json\n" + _json.dumps({
        "files": [{"n": 1, "view": "front"}, {"n": 2, "view": "left"}, {"n": 3, "view": "front"}],
        "object_kind": "truck", "class_hint": "vehicle", "condition": "good",
        "vehicle_category": {"code": "truck", "confidence": 0.8, "why": "кузов-фургон, сдвоенные задние колёса",
                             "fuel": None, "file": 1},
        "fields": [{"key": "year", "value": "2021", "source": "document", "file": 3}],
        "damages": [{"what": "вмятина на двери", "where": "слева", "file": 3}]}, ensure_ascii=False) + "\n```"
    files = [("a1.jpg", "image/jpeg", image(kind="jpg")), ("a2.png", "image/png", image((10, 200, 10))),
             ("b1.png", "image/png", image((10, 10, 200)))]
    st, up = upload(files, {"lang": "ru", "class_code": "3"})
    sid = up.get("session")
    objs = [dict(FLEET[0], photo_ids=["f1", "f2"]), dict(FLEET[1], photo_ids=[3], year=None),
            dict(FLEET[2])]
    st, a = fleet_make(objs, session=sid)
    o = a["objects"]
    ok("объект 1 — свои фото f1, f2: осмотрен, повреждений нет",
       st == 200 and o[0]["photo_ids"] == ["f1", "f2"] and o[0]["inspected"] and o[0]["damages"] == 0, o[0])
    ok("объект 2 — номер файла 3 = f3: вмятина с его снимка повышает риск; год — с его снимка (2021)",
       o[1]["photo_ids"] == ["f3"] and o[1]["damages"] == 1 and o[1]["year"] == 2021 and o[1]["year_source"] == "photo"
       and any(f["code"] == "f_cond_damage" for f in o[1]["risk_factors"]), o[1])
    ok("объект 3 без фото — не осмотрен, проверка «нет своих фото»",
       not o[2]["inspected"] and any("Excavator C (test)" in c and "фото" in c for c in a["decision"]["checks"]),
       a["decision"]["checks"])
    st, a2 = fleet_make([dict(FLEET[0]), dict(FLEET[1])], session=sid)
    ok("фото не привязаны — осмотр парка у всех объектов и пометка в разделе 5",
       st == 200 and all(x["inspected"] for x in a2["objects"])
       and any("не привязаны" in p for p in a2["sections"][4]["paragraphs"]), a2["sections"][4]["paragraphs"])
    model_on(False)


def _service_strings(a: dict, user: tuple = ()) -> list:
    """Служебные строки акта: разделы и блоки, которые сервер пишет сам (без значений, введённых сотрудником)."""
    out = []
    for s in a["sections"]:
        out += [s["title"]] + list(s["paragraphs"])
        out += [x for r in s["rows"] for x in (r["label"], str(r["value"]), r.get("note") or "")]
        for li in s.get("lists") or []:
            out += [li["title"]] + list(li["items"])
            if li.get("table"):
                out += list(li["table"]["columns"]) + [str(c) for row in li["table"]["rows"] for c in row]
    fa = a.get("factor_adjustment") or {}
    out += list(fa.get("explain") or [])
    for x in fa.get("applied") or []:
        out += [x.get("note") or "", (x.get("stat") or {}).get("reason_text") or "", (x.get("stat") or {}).get("note")
                or ""]
    bm = a.get("below_min_assessment") or {}
    out += [bm.get("text") or ""] + list(bm.get("lines") or [])
    ex = (a.get("analytics") or {}).get("exchange") or {}
    out += [ex.get("note") or "", ex.get("source_name") or "", ex.get("text") or ""]
    stt = (a.get("analytics") or {}).get("stats") or {}
    out += [stt.get("note") or ""] + [i.get("text") or "" for i in stt.get("indicators") or []]
    rf = a.get("rate_fork") or {}
    out += [rf.get("summary") or ""] + list(rf.get("how") or [])
    reg = ((rf.get("adjustments") or {}).get("region") or {})
    out += [i.get("unit") or "" for i in reg.get("indicators") or []]
    out += [str(((rf.get("adjustments") or {}).get("market") or {}).get("full_year_period") or "")]
    out += list((a.get("scenarios") or {}).get("how") or [])
    out += [o.get("text") or "" for o in a.get("objects") or []]
    for u in user:
        out = [s.replace(u, "") for s in out]
    return [s for s in out if _cyr(s)]


def check_three_langs():
    print("49. Акт на трёх языках из одного снимка: цифры одинаковые, в uz/en служебные строки без кириллицы")
    fresh()
    model_on(False)
    set_act_settings(None)
    body = {"lang": "ru", "must": {"product_code": "0318", "sum_insured": 2_945_000_000, "object_value": 3_100_000_000,
                                   "region": "tashkent_region"},
            "optional": dict(FLEET_OPT, class_fields={"veh_group": "truck", "fuel": "diesel"},
                             requested_rate_pct=0.1)}
    st, a = call("POST", "/act/make", body)
    ok("/act/make: langs_available = ru, uz, en", st == 200 and a["langs_available"] == ["ru", "uz", "en"],
       a.get("langs_available"))
    aid = a["id"]
    got = {}
    for lg in ("ru", "uz", "en"):
        st, got[lg] = call("GET", f"/act/{aid}", params={"lang": lg})
        ok(f"GET ?lang={lg}: 200, lang и langs_available", st == 200 and got[lg]["lang"] == lg
           and got[lg]["langs_available"] == ["ru", "uz", "en"])

    def nums(x):
        return (x["premium"]["amount"], x["rate"]["applied_pct"], x["risk"]["level"], x["value"]["ratio_pct"],
                (x["scenarios"].get("pml") or {}).get("amount"), x["factor_adjustment"].get("product"),
                x["below_min_assessment"].get("verdict"), [m["rate_pct"] for m in x["rate_fork"]["marks"]])
    ok("цифры на трёх языках одинаковые (премия, ставка, уровень, сценарии, факторы, вилка, оценка ставки)",
       nums(got["ru"]) == nums(got["uz"]) == nums(got["en"]), [nums(got[k]) for k in got])
    for lg in ("uz", "en"):
        bad = _service_strings(got[lg])
        ok(f"{lg}: служебные строки без кириллицы (факторы, оценка ставки, биржа, статистика, вилка)", not bad, bad[:5])
    ok("factor_adjustment.explain на узбекском — пометка факторов переведена",
       any("ekspert baho" in x for x in got["uz"]["factor_adjustment"]["explain"]),
       got["uz"]["factor_adjustment"]["explain"][:4])
    ok("below_min_assessment.text есть на всех языках", all(got[k]["below_min_assessment"].get("text") for k in got))
    # класс 7 — биржа и шаблонное правило сценария на трёх языках
    st, c = call("POST", "/act/make", {"lang": "ru", "must": {"class_code": "7", "sum_insured": 500_000_000,
                                                             "object_value": 500_000_000, "region": "tashkent_city"}})
    for lg in ("uz", "en"):
        st, cl = call("GET", f"/act/{c['id']}", params={"lang": lg})
        bad = _service_strings(cl)
        ok(f"класс 7, {lg}: без кириллицы (вид объекта по умолчанию, биржа, сценарии)", st == 200 and not bad, bad[:5])
        parts = [p for it in cl["analytics"].get("scenarios", {}).get("items") or [] for p in it.get("parts") or []]
        ok(f"класс 7, {lg}: формула простого правила шаблона (по-русски в снимке) не отдаётся",
           all(p.get("formula") is None for p in parts if p.get("peril") == "template"), parts[:2])
    st, body_, _h = call("GET", f"/act/{aid}.pdf", params={"lang": "en"}, raw=True)
    with pymupdf.open(stream=body_, filetype="pdf") as d:
        txt = _norm(pdf_text(d))
    ok("PDF ?lang=en из того же снимка", st == 200 and "INSPECTION REPORT" in txt.upper(), txt[:200])
    st, body_, _h = call("GET", f"/act/{aid}.docx", params={"lang": "uz"}, raw=True)
    xml = zipfile.ZipFile(io.BytesIO(body_)).read("word/document.xml").decode("utf-8")
    ok("Word ?lang=uz из того же снимка", st == 200 and "DALOLATNOMA" in xml.upper())


def check_regions():
    print("50. Регион: «Республика Узбекистан» (uz_all) и «Другое» (other, территория текстом)")
    fresh()
    model_on(False)
    ok("коды и названия: uz_all, other", act.region_code("uz_all") == "uz_all"
       and act.region_code("Республика Узбекистан") == "uz_all" and act.region_code("Oʻzbekiston Respublikasi")
       == "uz_all" and act.region_code("other") == "other")
    ok("модули: uz_all → «Республика Узбекистан»; other — республика для расчёта, фон stat.uz не берётся",
       act.region_for_modules({"region": "uz_all", "region_code": "uz_all"}) == "Республика Узбекистан"
       and act.region_for_modules({"region": "other", "region_code": "other"}) == "Республика Узбекистан"
       and act.region_for_stats({"region": "other", "region_code": "other"}) == "")
    must = {"product_code": "0318", "sum_insured": 2_945_000_000, "object_value": 3_100_000_000}
    st, a = call("POST", "/act/make", {"lang": "ru", "must": dict(must, region="uz_all"), "optional": CRANE_OPT})
    reg = (a.get("rate_fork") or {}).get("adjustments", {}).get("region") or {}
    ok("uz_all: поправка региона 0 с причиной «вся республика», статистика по республике с пометкой",
       st == 200 and a["region"]["scope"] == "republic" and reg.get("pct") == 0
       and "вся республика" in (reg.get("text") or "") and a["analytics"]["stats"].get("scope") == "republic"
       and "Республике Узбекистан" in (a["analytics"]["stats"].get("note") or ""), (reg.get("text"), a.get("region")))
    st, r = call("POST", "/act/make", {"lang": "ru", "must": dict(must, region="other")})
    ok("other без текста территории — 422 region_text", st == 422 and "region_text" in r["errors"], r)
    st, r = call("POST", "/act/make", {"lang": "ru", "must": dict(must, region="other", region_text="Х" * 121)})
    ok("текст территории длиннее 120 знаков — 422", st == 422 and "120" in r["errors"]["region_text"], r)
    st, r = call("POST", "/act/make", {"lang": "ru", "must": dict(must, region="other",
                                                                 region_text="Тестов Тест Тестович")})
    ok("ФИО вместо территории — 422", st == 422 and "персональных" in r["errors"]["region_text"], r)
    terr = "Республика Казахстан, маршрут Ташкент–Алматы"
    cargo = {"class_code": "7", "sum_insured": 500_000_000, "object_value": 500_000_000, "region": "other",
             "region_text": terr}
    st, c = call("POST", "/act/make", {"lang": "ru", "must": cargo})
    rows1 = {r["label"]: r for r in c["sections"][0]["rows"]}
    creg = (c.get("rate_fork") or {}).get("adjustments", {}).get("region") or {}
    ok("класс 7, other: в разделе 1 регион и «Территория страхования» — как введено, с пометкой",
       st == 200 and rows1["Регион"]["value"] == terr and rows1["Территория страхования"]["value"] == terr
       and "вне Узбекистана" in rows1["Территория страхования"]["note"] and c["territory"]["text"] == terr, rows1)
    ok("other: статистика недоступна с честной пометкой, поправка региона 0",
       c["analytics"]["stats"]["available"] is False and c["analytics"]["stats"]["reason"] == "outside"
       and c["analytics"]["stats"]["indicators"] == [] and "открытые данные" in c["analytics"]["stats"]["note"]
       and creg.get("pct") == 0 and "вне Узбекистана" in (creg.get("text") or ""), (c["analytics"]["stats"], creg))
    st, cu = call("GET", f"/act/{c['id']}", params={"lang": "uz"})
    ok("other на узбекском: территория как введена, пометка — на узбекском",
       st == 200 and cu["territory"]["text"] == terr and "Oʻzbekistondan tashqarida" in cu["territory"]["note"]
       and not _service_strings(cu, user=(terr,)), _service_strings(cu, user=(terr,))[:3])
    st, c8 = call("POST", "/act/make", {"lang": "ru", "must": dict(cargo, class_code="8", region="tashkent_city",
                                                                  region_text=None)})
    ok("класс 8 с обычным регионом: строки «Территория страхования» нет",
       st == 200 and c8["territory"] is None and all(r["label"] != "Территория страхования"
                                                     for r in c8["sections"][0]["rows"]), c8.get("territory"))
    st, c7 = call("POST", "/act/make", {"lang": "ru", "must": dict(cargo, region="tashkent_city", region_text=None)})
    ok("класс 7 с обычным регионом: территория = регион из списка",
       st == 200 and c7["territory"]["text"] == "город Ташкент", c7.get("territory"))


PASSPORT_LINES = ["СВИДЕТЕЛЬСТВО О РЕГИСТРАЦИИ АВТОМОТОТРАНСПОРТНОГО СРЕДСТВА",
                  "Тип транспортного средства: легковой", "Марка: Chevrolet", "Модель и модификация: Cobalt LTZ",
                  "Год выпуска: 2019", "Цвет окраски: белый", "Рабочий объём двигателя: 1485",
                  "Вид топлива: бензин/метан", "Мощность двигателя: 78 кВт", "Разрешённая максимальная масса: 1650 кг",
                  "Номер кузова: KA123456789", "Идентификационный номер (VIN): XWBJA69V0KA123456",
                  "Количество сидений: 5", "Владелец: Тестов Тест Тестович",
                  "Адрес владельца: г. Тестовый, ул. Примерная, 1", "ПИНФЛ: 30000000000000"]


def check_vehicle_autofill():
    print("51. Автозаполнение ТС: техпаспорт → подпись, год, подгруппа, топливо, характеристики; фото → категория")
    from app import vehicle_prefill as vp
    from app import class_templates as ctpl
    fresh()
    with db.tx() as con:
        tpl = ctpl.current(con, "3")["template"]
    groups = {g["code"]: [o["code"] for o in g["options"]] for g in tpl["factor_groups"]}
    ok("коды категорий и топлива — те же, что в шаблоне класса 3",
       list(vp.VEH_GROUPS) == groups["veh_group"] and list(vp.FUELS) == groups["fuel"], groups)
    prompt = act.model_prompt(2, "ru")
    ok("запрос к модели: vehicle_category с подгруппами, топливом, уверенностью и «почему»",
       "vehicle_category" in prompt and "special_tracked" in prompt and "газовые баллоны" in prompt
       and "fuel_why" in prompt and "уверенность" in prompt)
    model_on(False)
    pas = ("passport.docx", DOCX_MIME, docx_bytes(PASSPORT_LINES))
    st, r = upload([pas], {"lang": "ru", "class_code": "3"})
    pf = {x["field"]: x for x in r.get("prefill_fields") or []}
    ok("модели нет: техпаспорт всё равно разобран, prefill без ошибки, vehicle_category = null",
       st == 200 and r["vehicle_category"] is None and pf, (st, r.get("prefill_fields")))
    ok("техпаспорт → подпись «Chevrolet Cobalt LTZ», год 2019, легковой, газ-метан (бензин/метан), источник техпаспорт",
       pf.get("object_label", {}).get("value") == "Chevrolet Cobalt LTZ" and pf["year"]["value"] == 2019
       and pf["class_fields.veh_group"]["value"] == "car" and pf["class_fields.fuel"]["value"] == "cng"
       and all(x["source"] == "techpassport" and x["source_label"] == "техпаспорт" and 0 < x["confidence"] <= 1
               for x in pf.values()), pf)
    ch = (pf.get("characteristics") or {}).get("value") or {}
    ok("характеристики: мощность, масса, объём, места",
       ch.get("engine_power") == "78 кВт" and ch.get("max_mass") == "1650 кг" and ch.get("engine_cc") == "1485"
       and ch.get("seats") == "5", ch)
    ok("те же подсказки в prefill под своими ключами (с подписью и «проверьте»), пробега нет — его вводит сотрудник",
       r["prefill"] and r["prefill"]["class_fields.veh_group"]["value"] == "car"
       and r["prefill"]["class_fields.veh_group"]["value_label"] == "легковой автомобиль"
       and "техпаспорта" in r["prefill"]["year"]["check_label"] and "mileage" not in pf, r.get("prefill"))
    dump = _json.dumps(r, ensure_ascii=False)
    ok("ПД владельца из техпаспорта не возвращаются", "Тестов" not in dump and "30000000000000" not in dump
       and "Примерная" not in dump)
    # фото: категория и топливо по снимку
    model_on(True)
    REPLY["text"] = "```json\n" + _json.dumps({
        "files": [{"n": 1, "view": "back"}], "object_kind": "car", "class_hint": "vehicle", "condition": "good",
        "vehicle_category": {"code": "car", "confidence": 0.86, "why": "кузов седан, четыре двери",
                             "fuel": "electric", "fuel_confidence": 0.7, "fuel_why": "зарядный порт, нет выхлопной трубы",
                             "file": 1},
        "fields": [], "damages": []}, ensure_ascii=False) + "\n```"
    st, r = upload([("back.jpg", "image/jpeg", image(kind="jpg"))], {"lang": "ru", "class_code": "3"})
    vc = r.get("vehicle_category") or {}
    pf = {x["field"]: x for x in r.get("prefill_fields") or []}
    ok("фото: vehicle_category {code, label, confidence, why} и топливо, источник «фото»",
       st == 200 and vc.get("code") == "car" and vc.get("label") == "легковой автомобиль" and vc.get("confidence")
       == 0.86 and "седан" in vc.get("why") and vc.get("fuel") == "electric" and vc.get("fuel_label") == "электромобиль"
       and vc.get("source") == "photo" and vc.get("file") == "f1", vc)
    ok("фото: предзаполнение class_fields.veh_group и fuel с источником «фото»",
       pf["class_fields.veh_group"]["value"] == "car" and pf["class_fields.veh_group"]["source"] == "photo"
       and pf["class_fields.fuel"]["value"] == "electric" and pf["class_fields.fuel"]["confidence"] == 0.7
       and pf["class_fields.fuel"]["check_label"] == "с фото, проверьте", pf)
    # техпаспорт + фото: графа документа сильнее вида на снимке
    st, r = upload([pas, ("back.jpg", "image/jpeg", image(kind="jpg"))], {"lang": "ru", "class_code": "3"})
    pf = {x["field"]: x for x in r.get("prefill_fields") or []}
    ok("техпаспорт и фото вместе: подгруппа и топливо — из техпаспорта", pf["class_fields.fuel"]["value"] == "cng"
       and pf["class_fields.fuel"]["source"] == "techpassport" and r["vehicle_category"]["fuel"] == "electric", pf)
    REPLY["text"] = "```json\n" + _json.dumps({"files": [{"n": 1, "view": "back"}], "fields": [], "damages": [],
                                                "vehicle_category": {"code": "spaceship", "confidence": 2}},
                                               ensure_ascii=False) + "\n```"
    st, r = upload([("back.jpg", "image/jpeg", image(kind="jpg"))], {"lang": "ru", "class_code": "3"})
    ok("неизвестная категория от модели отбрасывается — без ошибки", st == 200 and r["vehicle_category"] is None
       and not r.get("prefill_fields"), r.get("vehicle_category"))
    ok("уверенность модели в процентах приводится к 0–1", act._conf(86) == 0.86 and act._conf("x") == 0.0
       and act._conf(3) == 0.03)
    model_on(False)


def check_send_fallback():
    print("52. Отправка ботом: initData устарел — запасной путь по привязке вошедшего пользователя")
    from app import telegram, tgbot
    import time as _t
    token = "123456:TEST-token-fallback"
    sent = []

    def fake_file(method, fields, field, filename, blob, mime):
        sent.append({"chat_id": fields.get("chat_id"), "filename": filename})
        return {"ok": True, "result": {"message_id": 9}}
    old = (tgbot.bot_token, telegram.bot_token, tgbot._deliver_file, tgbot._deliver)
    tgbot.bot_token = telegram.bot_token = lambda: token
    tgbot._deliver_file = fake_file
    tgbot._deliver = no_network
    saved_h, saved_c = list(HEADERS), dict(COOKIES)
    try:
        for login, tg in (("tg_fallback_on", "777000222"), ("tg_fallback_off", None)):
            now = datetime.now().isoformat(timespec="seconds")
            tok = secrets.token_urlsafe(32)
            with db.tx() as con:
                cur = con.execute("INSERT INTO users (login, full_name, role, branch, password_hash, salt, status, "
                                  "created_at, approved_by, approved_at, telegram_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                  (login, "Test User", "агент", "тест", secrets.token_hex(32), secrets.token_hex(16),
                                   "активен", now, "test", now, tg))
                con.execute("INSERT INTO sessions (token, user_id, created_at, expires_at, ip, user_agent) "
                            "VALUES (?,?,?,?,?,?)", (tok, cur.lastrowid, now,
                                                     (datetime.now() + timedelta(hours=2)).isoformat(timespec="seconds"),
                                                     "127.0.0.1", "test_act"))
            HEADERS[:] = [(b"authorization", f"Bearer {tok}".encode())]
            COOKIES.clear()
            act.reset_limits()
            st, a = call("POST", "/act/make", {"lang": "ru", "must": CRANE_MUST, "optional": CRANE_OPT})
            stale = init_data(int(tg or 777000333), token, auth_date=_t.time() - 3 * 24 * 3600)
            ok(f"{login}: устаревший initData не проходит проверку подписи по сроку",
               not telegram.check_init_data(stale, token).get("ok"))
            n0 = len(sent)
            st, r = call("POST", f"/act/{a['id']}/send", {"format": "pdf", "initData": stale})
            if tg:
                ok("initData устарел, у пользователя есть привязка — отправлено по привязке",
                   st == 200 and r.get("sent") and len(sent) == n0 + 1 and sent[-1]["chat_id"] == tg, (st, r))
                forged = init_data(777000999, "999999:чужой-токен")
                st, r = call("POST", f"/act/{a['id']}/send", {"format": "pdf", "initData": forged})
                ok("поддельный initData с чужим id — отправлено по привязке вошедшего, а не чужому id",
                   st == 200 and sent[-1]["chat_id"] == tg and all(x["chat_id"] != "777000999" for x in sent),
                   (st, r, sent[-1:]))
                own_aid = a["id"]
            else:
                ok("initData устарел, привязки нет — 403 bad_init_data, ничего не отправлено",
                   st == 403 and r.get("code") == "bad_init_data" and len(sent) == n0, (st, r))
                n1 = len(sent)
                st, r = call("POST", f"/act/{own_aid}/send", {"format": "pdf", "initData": stale})
                ok("чужой акт (другого пользователя) — 404, ничего не отправлено",
                   st == 404 and len(sent) == n1, (st, r))
    finally:
        tgbot.bot_token, telegram.bot_token, tgbot._deliver_file, tgbot._deliver = old
        HEADERS[:] = saved_h
        COOKIES.clear()
        COOKIES.update(saved_c)
        act.reset_limits()


# ------------------------------------------------------------------ 49. акт на один лист (06.10.2026)

CAR_MUST = {"product_code": "0301", "sum_insured": 300_000_000, "object_value": 320_000_000, "region": "Ташкент"}
CAR_OPT = {"protection": "immo", "losses_3y": {"count": 0, "small_count": 0, "amount": 0}}
CAR_FIELDS = [{"key": "object_type", "value": "легковой автомобиль", "source": "photo", "file": 1},
              {"key": "brand", "value": "Chevrolet", "source": "marking", "file": 1},
              {"key": "model", "value": "Cobalt", "source": "document", "file": 7},
              {"key": "year", "value": "2022", "source": "document", "file": 7}]
CAR_DAMAGES = [{"what": "царапины", "where": "левое крыло", "file": 3},
               {"what": "вмятина", "where": "задний бампер", "file": 2}]
CAR_FILES = [(f"{v}.jpg", "image/jpeg", image((40 + 20 * i, 90, 160), kind="jpg"))
             for i, v in enumerate(("front", "back", "left", "right", "plate", "odometer", "doc"))]


def car_reply(damages):
    return "```json\n" + _json.dumps({
        "files": [{"n": i + 1, "view": v} for i, v in enumerate(("front", "back", "left", "right", "plate",
                                                                   "odometer"))]
        + [{"n": 7, "view": "document", "document_kind": "техпаспорт"}],
        "object_kind": "car", "class_hint": "vehicle", "condition": "good",
        "vehicle_category": {"code": "car", "confidence": 0.9, "why": "седан", "fuel": "petrol",
                             "fuel_confidence": 0.8, "fuel_why": "бензин", "file": 1},
        "fields": CAR_FIELDS, "damages": damages}, ensure_ascii=False) + "\n```"


def car_act(damages, lang="ru", must=None, opt=None):
    """Легковой автомобиль 0301 с осмотром по семи фото (ответ модели подменён), повреждения — как задано."""
    REPLY["text"] = car_reply(damages)
    model_on(True)
    st, b = upload(CAR_FILES, {"lang": lang, "product_code": (must or CAR_MUST)["product_code"]})
    assert st == 200 and b.get("ok"), (st, b)
    st, a = call("POST", "/act/make", {"session": b["session"], "lang": lang, "must": must or CAR_MUST,
                                       "optional": opt or CAR_OPT, "recognized": b["recognized"]})
    return st, a


def act_pdf_pages(aid, lang="ru"):
    st, blob, h = call("GET", f"/act/{aid}.pdf", params={"lang": lang}, raw=True)
    d = pymupdf.open(stream=blob, filetype="pdf")
    return d.page_count, pdf_text(d)


def check_one_page_0610(crane_aid):
    print("49. Акт на один лист A4; советы по подгруппам транспорта; повреждения с фото в оценке; сценарии словами")
    fresh()
    # --- 49а. чистые функции: тяжесть повреждений, вес в уровне, штраф балла, подгруппа транспорта ---
    ok("49а: тяжесть по словам — «царапины левого крыла» косметические, «вмятина заднего бампера» существенные, "
       "«мелкая вмятина» косметическая, явная тяжесть модели важнее слов",
       ae.damage_severity({"what": "царапины", "where": "левое крыло"}) == "cosmetic"
       and ae.damage_severity({"what": "вмятина", "where": "задний бампер"}) == "major"
       and ae.damage_severity("мелкая вмятина двери") == "cosmetic"
       and ae.damage_severity({"what": "вмятина", "severity": "cosmetic"}) == "cosmetic"
       and ae.damage_severity({"what": "что-то непонятное"}) == "major")
    ds = ae.damages_summary(CAR_DAMAGES)
    ok("49а: пример — 2 повреждения, 1 косметическое и 1 существенное → тяжесть major, вес 1, штраф 30 (экспертно)",
       (ds["count"], ds["cosmetic"], ds["major"], ds["severity"], ds["weight"], ds["penalty"], ds["calibrated"])
       == (2, 1, 1, "major", 1.0, 30, 0), ds)
    base_in = {"inspected": True, "condition": "good", "year": None, "location": None, "losses_count": 0,
               "documents": True, "today": date(2026, 10, 6)}
    r0 = ae.risk_level(dict(base_in, damages=[]))
    r1 = ae.risk_level(dict(base_in, damages=["царапины на двери"]))
    r2 = ae.risk_level(dict(base_in, damages=CAR_DAMAGES))
    rw = ae.risk_level(dict(base_in, damages=[], condition="worn"))
    f1 = {f["code"]: f for f in r1["factors"]}
    ok("49а: уровень — косметические повреждения слабый повышающий признак (вес 0,5), существенные — как «изношено» "
       "(вес 1)", r0["up"] == 0 and r1["up"] == 0.5 and r1["net"] == r0["net"] + 0.5
       and f1["f_cond_damage_minor"]["weight"] == 0.5 and r2["up"] == 1 and r2["up"] == rw["up"]
       and r2["net"] == rw["net"], (r0["net"], r1["net"], r2["net"], rw["net"]))
    ok("49а: «вмятина» как раньше — признак f_cond_damage (уровень прежних актов не меняется)",
       any(f["code"] == "f_cond_damage" for f in r2["factors"]))
    row = {"score": 337, "basis": "risk_score", "risk_score_100": 32.7, "bounds": list(ae.SCORE_BOUNDS),
           "components": [{"code": "x", "points": 337, "max": 500}]}
    pen = ae.damage_penalty(row, 30, 2, "major")
    ok("49а: штраф балла — 337 (B2) → 307 (B3), составляющая «Повреждения с фото» −30, сумма составляющих = баллу",
       pen["score"] == 307 and pen["class_code"] == "B3" and pen["components"][-1]["points"] == -30
       and sum(c["points"] for c in pen["components"]) == 307, (pen["score"], pen["class_code"]))
    ok("49а: подгруппа транспорта — поле класса → вид объекта → категория с фото → по умолчанию легковой",
       ae.vehicle_group("3", {"veh_group": "truck"}, "car") == "truck"
       and ae.vehicle_group("3", {}, "truck_crane") == "special_wheeled"
       and ae.vehicle_group("3", {}, "excavator") == "special_tracked"
       and ae.vehicle_group("3", {}, None, {"code": "bus"}) == "bus"
       and ae.vehicle_group("3", {}, None, None, "special") == "special_wheeled"
       and ae.vehicle_group("3", {}, None, None, "vehicle", "КАСКО «Standart»") == "car"
       and ae.vehicle_group("8", {}, "car") is None)
    ok("49а: факторы подгрупп — «площадка» и моточасы только спецтехнике, «допущенные водители» — не крану",
       not ae.vehicle_factor_applies("spec_site", "car") and not ae.vehicle_factor_applies("engine_hours", "truck")
       and ae.vehicle_factor_applies("spec_site", "special_wheeled")
       and not ae.vehicle_factor_applies("drivers", "special_wheeled") and ae.vehicle_factor_applies("drivers", "car"))

    # --- 49б. легковой автомобиль 0301 с фото и повреждениями ---
    st, a = car_act(CAR_DAMAGES)
    ok("49б: акт по легковому автомобилю с осмотром сформирован", st == 200 and a.get("ok"), (st, str(a)[:300]))
    st0, a0 = car_act([])
    ok("49б: повреждения повышают уровень — без них низкий, с существенными умеренный; премия — по уровню",
       a0["risk"]["level"] == "low" and a["risk"]["level"] == "moderate" and a["risk"]["net"] == a0["risk"]["net"] + 1
       and a["rate"]["adj_pct"] == 20 and a0["rate"]["adj_pct"] == 0,
       (a["risk"]["level"], a0["risk"]["level"], a["risk"]["net"], a0["risk"]["net"]))
    fc = {f["code"] for f in a["risk"]["factors"]}
    ok("49б: повреждения с фото входят в уровень риска — признак «видимые повреждения (2)», повышающих на 1 больше",
       "f_cond_damage" in fc and a["risk"]["up"] == a0["risk"]["up"] + 1
       and any("видимые повреждения (2)" in f["text"] for f in a["risk"]["factors"]), a["risk"])
    sc, sc0 = a["scoring"], a0["scoring"]
    comp = [c for c in sc["components"] if c["code"] == "damages"]
    ok("49б: балл скоринга −30 за существенные повреждения, отдельная составляющая и фраза (экспертно); без "
       "повреждений штрафа нет", comp and comp[0]["points"] == -30 and f"− 30 = {sc['score']}" in sc["text"]
       and "С учётом повреждений с фото (2, существенные)" in sc["text"]
       and not any(c["code"] == "damages" for c in sc0["components"]), (sc0["score"], sc["score"], sc["text"]))
    dmg = a["inspection"]["damages"]
    ok("49б: JSON — тяжесть каждого повреждения и сводка (вес в уровне, штраф балла, исключаются как "
       "предсуществующие)", [d["severity"] for d in dmg] == ["cosmetic", "major"]
       and a["inspection"]["damages_summary"]["score_penalty"] == 30
       and a["inspection"]["damages_summary"]["excluded_as_preexisting"] is True, a["inspection"])
    n, txt = act_pdf_pages(a["id"])
    dx = docx_plain(a["id"])
    want2 = "Выявлены: царапины (левое крыло), вмятина (задний бампер) — исключаются из покрытия как предсуществующие"
    want4 = "Исключение: предсуществующие повреждения по списку раздела 2"
    ok("49б: раздел 2 — «выявлены … исключаются из покрытия как предсуществующие», раздел 4 «Условия» — "
       "исключение по списку раздела 2 (PDF и Word)", want2 in txt and want4 in txt and want2 in dx and want4 in dx,
       txt[:900])
    ok("49б: легковой автомобиль с осмотром, повреждениями и сценариями — PDF на один лист", n == 1, n)
    # советы по подгруппе: легковой
    codes = [m["code"] for m in a["measures"]]
    ok("49б: мероприятия легкового — стоянка/гараж, иммобилайзер и GPS-метка, круг водителей, маркировка, "
       "видеорегистратор", {"vh_car_parking", "vh_car_antitheft", "vh_car_drivers", "vh_car_marking",
                            "vh_car_dashcam"} <= set(codes) and not any(c.startswith("sp_") for c in codes), codes)
    allt = " ".join([all_text(a), _json.dumps(a["measures"], ensure_ascii=False),
                     _json.dumps(a["analytics"].get("summary"), ensure_ascii=False),
                     _json.dumps(a["analytics"].get("sensitivity"), ensure_ascii=False), txt, dx])
    bad = [w for w in ("закрытое помещение", "площадка предприятия", "моточасов") if w in allt]
    ok("49б: акт по легковому не советует и не уточняет спецтехнику — нет «закрытое помещение», «площадка "
       "предприятия», «моточасов» (JSON, экран, Word, PDF)", not bad, bad)
    fx = {f["code"] for f in a["analytics"]["factors"]["items"]}
    ok("49б: в аналитике легкового нет факторов спецтехники (площадка, охрана площадки, оператор, моточасы)",
       not fx & {"spec_site", "spec_guard", "spec_operator", "engine_hours"}, fx)
    # сценарии словами
    scn = a["scenarios"]
    ok("49б: сценарии словами — PML «ДТП с серьёзным повреждением кузова и агрегатов — ремонт до N % стоимости», "
       "EML «опрокидывание или пожар после ДТП», MFL «угон без последующего обнаружения или полная гибель в пожаре»",
       scn["pml"]["text"].startswith("PML (вероятный максимум): ДТП с серьёзным повреждением кузова и агрегатов — "
                                     f"ремонт до {scn['pml']['pct_text']} стоимости")
       and scn["eml"]["text"].startswith("EML (при отказе защиты): опрокидывание или пожар после ДТП")
       and scn["mfl"]["text"].startswith("MFL (полная потеря): угон без последующего обнаружения или полная гибель "
                                         "в пожаре — 100 %".replace(" %", " %"))
       and all(scn[k]["text_expert"] for k in ("pml", "eml", "mfl")), [scn[k]["text"] for k in ("pml", "eml", "mfl")])
    ok("49б: MFL — что снижает вероятность и что есть у объекта по данным сотрудника (иммобилайзер — есть, "
       "GPS-метка — нет, стоянка — не указано)",
       "Что снижает вероятность MFL: охраняемая стоянка, иммобилайзер, GPS-метка" in scn["mfl"]["text"]
       and "охраняемая стоянка — не указано, иммобилайзер — есть, GPS-метка — нет" in scn["mfl"]["text"],
       scn["mfl"]["text"])
    ok("49б: в документе — одна строка на сценарий (без повтора сумм), суммы — строкой «Ожидаемая тяжесть»",
       all(flat(scn[k]["text"].replace(f" ({act.money(scn[k]['amount'], 'ru')})", ""))[:60] in txt
           for k in ("pml", "eml", "mfl")) and "Ожидаемая тяжесть (экспертно): PML" in txt, txt[:600])
    ok("49б: решение по правилу — фото есть, уровень не высокий, ставка не ниже минимума → «Принять»",
       "Решение: Принять" in txt, txt[txt.find("Решение"):txt.find("Решение") + 120])
    for lg in ("uz", "en"):
        st, al = call("GET", f"/act/{a['id']}", params={"lang": lg})
        tt = " ".join(al["scenarios"][k]["text"] for k in ("pml", "eml", "mfl")) + al["inspection"][
            "damages_summary"]["severity_label"]
        ok(f"49б {lg}: сценарии словами и тяжесть повреждений — на языке акта, без кириллицы",
           not re.search(r"[А-Яа-яЁё]", tt) and act_pdf_pages(a["id"], lg)[0] == 1, tt[:200])

    # --- 49в. КАСКО без фото: легковой по умолчанию, решение «на рассмотрение» ---
    st, k = call("POST", "/act/make", {"lang": "ru", "must": {"product_code": "0309", "sum_insured": 300_000_000,
                                                              "object_value": 300_000_000, "region": "Ташкент"},
                                       "optional": {"losses_3y": {"count": 0}}})
    kc = [m["code"] for m in k["measures"]]
    n, txt = act_pdf_pages(k["id"])
    ok("49в: КАСКО 0309 без вида объекта — советы легкового (подгруппа по умолчанию car)",
       st == 200 and "vh_car_antitheft" in kc and not any(c.startswith("sp_") for c in kc), kc)
    ok("49в: КАСКО без фото — «На рассмотрение специалиста: нет фото объекта», PDF на один лист",
       n == 1 and "Решение: На рассмотрение специалиста: нет фото объекта" in txt, (n, txt[:300]))

    # --- 49г. грузовой автомобиль: свои советы ---
    st, tr = call("POST", "/act/make", {"lang": "ru", "must": CAR_MUST,
                                        "optional": dict(CAR_OPT, object_kind="truck", protection="none")})
    tc = [m["code"] for m in tr["measures"]]
    ok("49г: грузовой — охраняемая стоянка, тахограф и режим труда, GPS-мониторинг, допуск водителей",
       {"vh_truck_parking", "vh_truck_tacho", "vh_truck_gps", "vh_truck_drivers"} <= set(tc)
       and not any(c.startswith("vh_car_") for c in tc), tc)

    # --- 49д. автокран (пример заказчика с фото) и склад 0807 ---
    n, txt = act_pdf_pages(crane_aid)
    st, cr = call("GET", f"/act/{crane_aid}")
    cc = [m["code"] for m in cr["measures"]]
    crane_all = " ".join([_json.dumps(cr["measures"], ensure_ascii=False),
                          _json.dumps(cr["analytics"].get("summary"), ensure_ascii=False), txt])
    ok("49д: автокран — PDF на один лист", n == 1, n)
    ok("49д: автокран — прежние советы спецтехники (площадка, мониторинг, опоры крана, допуск оператора); "
       "«круг водителей» и «допущенные водители» не советуются (иммобилайзер для крана допустим)",
       {"sp_parking_guarded", "sp_gps", "sp_crane_setup", "sp_operator"} & set(cc)
       and not any(c.startswith("vh_") for c in cc) and "круг водителей" not in crane_all
       and "допущенные водители" not in crane_all
       and "drivers" not in {f["code"] for f in cr["analytics"]["factors"]["items"]}, cc)
    ok("49д: автокран — сценарии спецтехники словами (авария при работе, опрокидывание, хищение с площадки)",
       "авария при работе" in cr["scenarios"]["pml"]["text"] and "опрокидывание техники" in cr["scenarios"]["eml"][
           "text"] and "хищение с площадки" in cr["scenarios"]["mfl"]["text"], cr["scenarios"]["mfl"]["text"])
    st, wh = call("POST", "/act/make", {"lang": "ru", "must": WH8_MUST, "optional": WH8_OPT})
    n, txt = act_pdf_pages(wh["id"])
    ok("49д: склад 0807 — PDF на один лист, четыре блока раздела 4", n == 1 and all(
        x in txt for x in ("Оценка риска", "Решение:", "Условия", "Цена", "Опасности", "Ожидаемая частота",
                           "Ожидаемая тяжесть")), (n, txt[:300]))
    ws = wh["scenarios"]
    ok("49д: склад — сценарии словами: пожар в одном отсеке / распространение на здание или землетрясение / "
       "полная гибель", "пожар в одном" in ws["pml"]["text"]
       and ("распространяется на всё здание" in ws["eml"]["text"] or "землетрясение" in ws["eml"]["text"])
       and "полная гибель здания" in ws["mfl"]["text"] and "защита — пожарная сигнализация" in ws["mfl"]["text"],
       [ws[k]["text"] for k in ("pml", "eml", "mfl")])
    gone = ["Как сверен договор", "Франшиза: как посчитано", "Как посчитаны сценарии", "Рынок и статистика",
            "Разбор по рискам", "Положение № 1806", "Источник:"]
    ok("49д: в документе нет внутренних пояснений (сверки, «как посчитано», рынок, разбор рисков, лимит по "
       "Положению 1806, источники)", not [g for g in gone if g in txt], [g for g in gone if g in txt])
    ok("49д: подпись документа — «Акт сформирован ИИ-сюрвейером INSON, подлежит подтверждению андеррайтером»",
       "Акт сформирован ИИ-сюрвейером INSON, подлежит подтверждению андеррайтером" in txt)
    st, blob, h = call("GET", f"/act/{wh['id']}.docx", raw=True)
    xml = zipfile.ZipFile(io.BytesIO(blob)).read("word/document.xml").decode("utf-8")
    sty = zipfile.ZipFile(io.BytesIO(blob)).read("word/styles.xml").decode("utf-8")
    ok("49д: Word — поля 15 мм (850 twip), шрифт 10 pt, без картинки скоринга",
       'w:left="850"' in xml and 'w:top="850"' in xml and '<w:sz w:val="20"/>' in sty and "<wp:inline" not in xml)


def main():
    ORIG.update(chat_raw=llm.chat_raw, enabled=llm.enabled, supports_files=llm.supports_files, post=llm._post)
    llm.chat_raw = fake_chat_raw
    llm._post = no_network
    folder = Path(tempfile.mkdtemp(prefix="surveyor-act-"))
    try:
        with temp_db("surveyor-act-test.db"):
            db.ensure_schema()
            # собственные средства и резервы — фиксированные тестовые (как были временные 180 / 240 млрд): баллы,
            # лимиты и склонения в проверках не зависят от того, что сейчас в рабочей базе (с 03.10.2026 сервер
            # заменяет временные цифры рэнкингом snsratings — это проверяет tests/test_ranking.py)
            with db.tx() as con:
                con.execute("DELETE FROM company_financials")
                con.execute("INSERT INTO company_financials (report_date, own_funds, reserves, source) "
                            "VALUES ('2026-07-01', 180e9, 240e9, 'временно, до данных бухгалтерии (тест)')")
            act.DIR = folder
            guest.reset()
            check_engine()
            a, sid = check_crane()
            aid = a["id"]
            check_value()
            check_franchise()
            check_no_rate()
            check_no_model()
            check_pd()
            check_files(aid)
            check_langs(aid)
            check_polish()
            check_access(aid)
            check_bomb()
            check_threadpool()
            check_body_limit()
            check_limits()
            check_budget()
            check_sources()
            check_misc()
            check_scenarios()
            check_documents()
            fr_aid, fr_hand = check_franchise_apply()
            check_measures()
            check_new_langs(fr_aid)
            check_new_files(fr_aid, fr_hand)
            check_speed()
            check_doc_limits()
            check_dtd_prolog()
            check_fr_proposed()
            check_alt_base()
            check_protection_other()
            check_lang_fields()
            check_minor()
            check_screen()
            check_market_engine()
            check_market_links()
            shots_sid = check_market_shots()
            check_market_make(shots_sid)
            br_sid, br_b = check_branch_text()
            br_scan = check_branch_scan()
            br_aid, _ = check_branch_make(br_sid, br_b, br_scan)
            check_branch_langs_files(br_aid)
            check_contract_text()
            check_contract_long()
            check_contract_scan()
            check_contract_ai_assist()
            ct_aid = check_contract_make()
            x_aid = check_contract_cross()
            check_contract_langs_files(ct_aid, x_aid)
            check_trust_edits()
            check_doc_kind_title()
            check_essentials_wording()
            check_cross_same()
            check_input_bounds()
            check_ct_ai_mask_labels()
            check_parties_amounts()
            check_pdf_time_limit()
            check_analytics()
            check_review_fixes()
            check_docx_reader()
            check_contract_template()
            check_contract_template_filled()
            check_templates_ref()
            check_templates_api()
            check_templates_act()
            check_templates_new_classes()
            check_templates_class18()
            check_templates_sync()
            check_parts_engine()
            pt_aid = check_parts_make()
            check_parts_langs_files(pt_aid)
            check_review_0930()
            check_scoring_engine()
            sc_aid, _sc_eq = check_scoring_make()
            check_scoring_files(sc_aid)
            cr_aid = check_credit_report()
            check_scoring_langs(sc_aid, cr_aid)
            check_scoring_bands_engine()
            check_scoring_review(sc_aid)
            check_credit_parse_review()
            check_credit_scan_off()
            check_rate_fork_engine()
            check_rate_fork()
            check_rate_type_engine()
            bm_aid = check_below_min_act()
            check_min_rates_admin(bm_aid)
            check_factor_groups()
            check_factor_stat()
            check_exchange()
            check_objects()
            check_objects_photos()
            check_three_langs()
            check_regions()
            check_vehicle_autofill()
            check_one_page_0610(aid)
            check_send_fallback()
            check_send(aid)
            check_cleanup(sid, aid)
    finally:
        llm.chat_raw, llm.enabled, llm.supports_files, llm._post = (ORIG["chat_raw"], ORIG["enabled"],
                                                                    ORIG["supports_files"], ORIG["post"])
        if "cbu" in ORIG:
            from app import valuation_sources as vs
            vs.cbu_usd_rate = ORIG["cbu"]
        shutil.rmtree(folder, ignore_errors=True)
    if BR_REPORT:
        print("\nзапрос филиала, продукт 0832:")
        for k, v in BR_REPORT.items():
            print("  ", k, v)
    if CT_REPORT:
        print("\nдоговор страхования:")
        for k, v in CT_REPORT.items():
            print("  ", k, v)
    if AN_REPORT:
        print("\nаналитика раздела 4:")
        for k, v in AN_REPORT.items():
            print("  ", k, v)
    if TPL_REPORT:
        print("\nшаблоны классов:")
        for k, v in TPL_REPORT.items():
            print("  ", k, v)
    if PT_REPORT:
        print("\nкомплексные продукты по частям:")
        for k, v in PT_REPORT.items():
            print("  ", k, v)
    if SC_REPORT:
        print("\nстраховой скоринг (балл риска 0–100 → балл 0–500, класс):")
        for k, v in SC_REPORT.items():
            print("  ", k, v)
    if RF_REPORT:
        print("\nвилка ставки (минимум, акт, с учётом региона и рынка, рынок, поправка региона %, рынка %, …):")
        for k, v in RF_REPORT.items():
            print("  ", k, v)
    if BM_REPORT:
        print("\nминимальная ставка страховщика и заниженная ставка:")
        for k, v in BM_REPORT.items():
            print("  ", k, v)
    if FA_REPORT:
        print("\nфакторы объекта по подгруппам класса (ставка, премия, множитель …):")
        for k, v in FA_REPORT.items():
            print("  ", k, v)
    if OBJ_REPORT:
        print("\nпарк ТС (несколько объектов в одном акте):")
        for k, v in OBJ_REPORT.items():
            print("  ", k, v)
    if SCEN_REPORT:
        print("\nсценарии (сумма, % страховой суммы):")
        for k, v in SCEN_REPORT.items():
            print("  ", k, v)
    print(f"\nитог: ок {passed}, плохо {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
