"""
ТЗ разработчику от 13.10.2026, день 1: факты об объекте и часы акта.

Запуск из корня:
    set PYTHONIOENCODING=utf-8
    sandbox\\.venv\\Scripts\\python.exe tests\\test_act_facts.py

Работает на копии базы (tests/tmpdb.py), в сеть не ходит. Данные — учебные.
  1. Факты по частям: числа из текста, моточасы, вариант износа по проценту, наработка → вариант справочника,
     проверка адреса (улица с именем — можно, имя и телефон — нельзя), проверка настроек clock и object_facts.
  2. Магазин 450 м², 2 этажа, износ 15 %, адрес — в act_json.object, в разделе 1 (экран, Word, PDF), в факторах
     акта (вариант износа) и в risk_analytics (износ, этажность, площадь); нигде нет «не указано» по этим полям.
  3. Экскаватор 6 500 моточасов на стройплощадке под охраной, водители — то же: в акте, в факторах (место),
     в аналитике (наработка, площадка, охрана, оператор); тип места — только в разделе риска.
  4. Адрес с ФИО или телефоном — 422; слишком длинный — 422.
  5. Часы: акт, созданный в 23:30 UTC, — дата следующего дня по Ташкенту (дата, время, номер); срок хранения
     и очистка считаются от того же времени.
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["SURVEYOR_NO_BACKGROUND"] = "1"

import pymupdf                            # noqa: E402

import test_act as T                      # noqa: E402  ASGI-клиент и заглушки сети из тестов акта
from tmpdb import temp_db                 # noqa: E402
from app import act, act_engine as ae, db, guest, llm   # noqa: E402
from app.act_pkg import common, facts as of             # noqa: E402

passed, failed = 0, 0

SHOP = {"lang": "ru",
        "must": {"product_code": "0821", "sum_insured": 2_000_000_000, "object_value": 2_100_000_000, "region": "Ташкент"},
        "optional": {"object_kind": "shop", "construction": "reinforced", "activity": "office",
                     "address": "г. Ташкент, Юнусабадский район, ул. Амира Темура, 1",
                     "class_fields": {"area_m2": 450, "floors": 2, "wear": "15 %"}}}
EXCAVATOR = {"lang": "ru",
             "must": {"product_code": "0318", "sum_insured": 1_500_000_000, "object_value": 1_600_000_000,
                      "region": "Ташкентская область"},
             "optional": {"object_kind": "excavator", "location": "construction", "guard": True, "year": 2019,
                          "class_fields": {"veh_group": "special_tracked", "drivers": "2 машиниста, стаж от 10 лет"}},
             "recognized": [{"key": "mileage", "value": "6 500 моточасов", "source": "input"}]}


def ok(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ок  ", name)
    else:
        failed += 1
        print("  ПЛОХО", name, str(extra)[:400])


def rows1(a: dict) -> dict:
    return {r["label"]: r for r in a["sections"][0]["rows"]}


def not_stated(a: dict) -> str:
    """Строка «Не указано: …» раздела 1 (пусто, если её нет)."""
    r = rows1(a).get(T.tx.t("s1_not_specified", a["lang"]))
    return (r or {}).get("value") or ""


def stored(aid: str) -> dict:
    with db.tx() as con:
        return json.loads(db.rows(con, "SELECT act_json FROM acts WHERE id=?", aid)[0]["act_json"])


def docx_text(aid: str) -> str:
    st, blob, _h = T.call("GET", f"/act/{aid}.docx", raw=True)
    xml = zipfile.ZipFile(io.BytesIO(blob)).read("word/document.xml").decode("utf-8")
    # неразрывные пробелы и разметка — обычными пробелами
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", xml)) if st == 200 else ""


def pdf_text(aid: str) -> str:
    st, blob, _h = T.call("GET", f"/act/{aid}.pdf", raw=True)
    if st != 200:
        return ""
    return re.sub(r"\s+", " ", " ".join(p.get_text() for p in pymupdf.open(stream=blob, filetype="pdf")))


def factor_items(a: dict) -> dict:
    return {f["code"]: f for f in a["analytics"]["factors"]["items"]}


# ------------------------------------------------------------------ 1. по частям

def check_units():
    print("1. Факты по частям, проверка адреса и настроек")
    ok("числа из текста: 450, «6 500 моточасов», «15 %», «12,5»",
       of.number_of(450) == 450 and of.number_of("6 500 моточасов") == 6500 and of.number_of("15 %") == 15
       and of.number_of("12,5") == 12.5 and of.number_of("нет") is None)
    ok("единица пробега: моточасы, километры, не сказано",
       of.mileage_unit("6 500 моточасов") == "hours" and of.mileage_unit("6500 motosoat") == "hours"
       and of.mileage_unit("120 000 км") == "km" and of.mileage_unit("6500") is None)
    b = ae.DEFAULT_SETTINGS["object_facts"]["engine_hours_bands"]
    ok("наработка → вариант справочника: 1 500 → eh_low, 6 000 → eh_mid, 6 500 → eh_high, 13 000 → eh_max",
       [of.engine_hours_option(x, b) for x in (1500, 6000, 6500, 13000)] == ["eh_low", "eh_mid", "eh_high", "eh_max"])
    st = {"object_facts": ae.DEFAULT_SETTINGS["object_facts"]}
    f15 = of.build_facts({"class_fields": {"wear": "15 %"}}, [], {}, st)
    f60 = of.build_facts({"class_fields": {"wear": "60"}}, [], {}, st)
    fsel = of.build_facts({"class_fields": {"wear": "60", "wear_level": "new"}}, [], {}, st)
    ok("износ 15 % → вариант «до 50 %» (normal), 60 % → «более 50 %» (high), выбор сотрудника сильнее процента",
       f15["wear_level"] == "normal" and f15["sources"]["wear_level"] == "wear_pct" and f60["wear_level"] == "high"
       and fsel["wear_level"] == "new" and fsel["sources"]["wear_level"] == "input")
    fl = of.build_facts({}, [{"key": "location", "value": "стройка, открытая площадка", "source": "photo"}], {}, st)
    ok("место с фото словами → код в фактах (источник — фото)", fl["location"] == "construction"
       and fl["sources"]["location"] == "photo", fl)
    ok("оператор по словам: машинисты → op_staff, аренда с экипажем → op_rent_crew, аренда → op_rent",
       of.operator_of("2 машиниста, стаж от 10 лет") == "op_staff"
       and of.operator_of("сдаётся в аренду с экипажем") == "op_rent_crew"
       and of.operator_of("сдаётся в аренду") == "op_rent" and of.operator_of("—") is None)
    ok("адрес: улица с именем человека — можно", of.address_in("г. Ташкент, ул. Амира Темура, 1", 200) ==
       ("г. Ташкент, ул. Амира Темура, 1", None))
    ok("адрес: «Amir Temur ko‘chasi» — можно", of.address_in("Toshkent, Amir Temur koʻchasi, 7", 200)[1] is None)
    ok("адрес: ФИО — нельзя", of.address_in("Ташкент, Иванов Иван Иванович", 200)[1] is not None)
    ok("адрес: телефон — нельзя", of.address_in("Ташкент, ул. Навои 5, +998 90 123 45 67", 200)[1] is not None)
    ok("адрес: длиннее предела — нельзя", of.address_in("Ташкент " * 40, 200)[1] is not None)
    ok("настройки clock и object_facts по умолчанию проходят проверку", not ae.check_settings({}), ae.check_settings({}))
    errs = ae.check_settings({"clock": {"tz": "Ташкент", "utc_offset_hours": 50},
                              "object_facts": {"wear_high_from_pct": 0, "engine_hours_bands": [6000, 2000, 12000]}})
    ok("неверные clock и object_facts — понятные ошибки",
       any(e.startswith("clock.tz") for e in errs) and any(e.startswith("clock.utc_offset_hours") for e in errs)
       and any(e.startswith("object_facts.wear_high_from_pct") for e in errs)
       and any(e.startswith("object_facts.engine_hours_bands") for e in errs), errs)
    ok("у констант есть версия и дата", all(ae.DEFAULT_SETTINGS[k].get("version") and ae.DEFAULT_SETTINGS[k].get("date")
                                            for k in ("clock", "object_facts")))


# ------------------------------------------------------------------ 2. магазин

def check_shop() -> str:
    print("2. Магазин 450 м², 2 этажа, износ 15 %, адрес")
    T.fresh()
    st, a = T.call("POST", "/act/make", SHOP)
    ok("акт сформирован", st == 200, a if st != 200 else "")
    if st != 200:
        return ""
    obj = stored(a["id"])["data"]["object"]
    ok("act_json.object: адрес, 450 м², 2 этажа, износ 15 %",
       obj["address"] == SHOP["optional"]["address"] and obj["area_m2"] == 450 and obj["floors"] == 2
       and obj["wear_pct"] == 15 and obj["wear_level"] == "normal", obj)
    ok("act_json.object: источник каждого значения и версия настройки",
       obj["sources"]["area_m2"] == "input" and obj["sources"]["address"] == "input"
       and obj["settings"]["version"] == ae.DEFAULT_SETTINGS["object_facts"]["version"], obj)
    ok("ответ /act/make отдаёт тот же словарь object", a["object"] == obj)
    r = rows1(a)
    ok("раздел 1: «Адрес объекта» — введённый адрес", r.get("Адрес объекта", {}).get("value") == SHOP["optional"]["address"],
       list(r))
    ok("раздел 1: «Общая площадь — 450 м²»", r.get("Общая площадь", {}).get("value") == "450 м²", r.get("Общая площадь"))
    ok("раздел 1: «Этажность — 2»", r.get("Этажность", {}).get("value") == "2", r.get("Этажность"))
    ok("раздел 1: «Износ — 15 % (до 50 %)»", r.get("Износ", {}).get("value") == "15 % (до 50 %)", r.get("Износ"))
    ns = not_stated(a).lower()
    ok("раздел 1: в «Не указано» нет площади, этажности, износа и адреса",
       not any(w in ns for w in ("общая площадь", "этажность", "износ", "адрес")), ns)
    na = T.tx.t("na", "ru")
    ok("раздел 1: у этих строк нет «данные недоступны»",
       all(r[k]["value"] != na for k in ("Адрес объекта", "Общая площадь", "Этажность", "Износ") if k in r))
    fa = {x["group"]: x for x in a["factor_adjustment"]["applied"]}
    ok("факторы акта: «Износ — до 50 %» выведен из 15 % (тот же словарь)", fa.get("wear_level", {}).get("option") == "normal",
       list(fa))
    fi = factor_items(a)
    ok("risk_analytics: износ здания задан из 15 % (введено сотрудником), не «не указано»",
       fi["wear"]["status"] == "set" and fi["wear"]["source"] == "input", fi.get("wear"))
    ok("risk_analytics: 15 % → «до 10 лет» по порогам модуля (wear_pct_bands)", fi["wear"]["option"] == "new",
       fi["wear"])
    txt = docx_text(a["id"])
    ok("Word: адрес, 450 м², этажность 2, износ 15 %",
       SHOP["optional"]["address"] in txt and "450 м²" in txt and "Этажность 2" in txt and "Износ 15 % (до 50 %)" in txt,
       txt[:600])
    ptxt = pdf_text(a["id"])
    ok("PDF: адрес, 450 м², износ 15 %", "Амира Темура" in ptxt and "450" in ptxt and "15" in ptxt and "Износ" in ptxt,
       ptxt[:600])
    for lang in ("uz", "en"):
        st2, x = T.call("GET", f"/act/{a['id']}", params={"lang": lang})
        labels = " ".join(rw["label"] for rw in x["sections"][0]["rows"])
        ok(f"{lang}: подписи фактов на языке акта, без кириллицы",
           T.tx.t("lbl_object_address", lang) in labels and T.tx.t("lbl_wear", lang) in labels
           and not re.search(r"[А-Яа-яЁё]", labels), labels)
    return a["id"]


# ------------------------------------------------------------------ 3. экскаватор

def check_excavator() -> str:
    print("3. Экскаватор 6 500 моточасов, стройплощадка, охрана, водители")
    T.fresh()
    st, a = T.call("POST", "/act/make", EXCAVATOR)
    ok("акт сформирован", st == 200, a if st != 200 else "")
    if st != 200:
        return ""
    obj = stored(a["id"])["data"]["object"]
    ok("act_json.object: 6 500 моточасов, стройплощадка, охрана, водители",
       obj["mileage"] == 6500 and obj["mileage_unit"] == "hours" and obj["location"] == "construction"
       and obj["guard"] is True and obj["drivers"] == "2 машиниста, стаж от 10 лет", obj)
    r = rows1(a)
    ok("раздел 1: «Пробег / моточасы — 6 500 моточасов»",
       r.get("Пробег / моточасы", {}).get("value") == "6 500 моточасов", r.get("Пробег / моточасы"))
    ok("раздел 1: «Водители и стаж»", r.get("Водители и стаж", {}).get("value") == "2 машиниста, стаж от 10 лет")
    ok("раздел 1: охрана — есть", r.get("Охрана", {}).get("value") == "есть", r.get("Охрана"))
    ok("тип места — только в разделе риска: в разделе 1 строки места нет",
       "Место эксплуатации" not in r and "строительная площадка" not in json.dumps(a["sections"][0], ensure_ascii=False))
    ok("раздел риска: «строительная площадка, но под охраной»",
       any("строительная площадка" in f["text"] for f in a["risk"]["factors"]), [f["text"] for f in a["risk"]["factors"]])
    ns = not_stated(a).lower()
    ok("раздел 1: в «Не указано» нет пробега, места, водителей",
       not any(w in ns for w in ("пробег / моточасы", "место эксплуатации", "водители и стаж")), ns)
    fa = {x["group"]: x for x in a["factor_adjustment"]["applied"]}
    ok("факторы акта: место «строительная площадка»", fa.get("location", {}).get("option") == "construction", list(fa))
    fi = factor_items(a)
    want = {"engine_hours": "eh_high", "spec_site": "site_build", "spec_guard": "guard_watch", "spec_operator": "op_staff"}
    ok("risk_analytics: наработка 6 000–12 000, стройплощадка, сторож, штатный оператор — заданы, не «не указано»",
       all(fi.get(k, {}).get("option") == v and fi[k]["status"] == "set" for k, v in want.items()),
       {k: (fi.get(k) or {}).get("option") for k in want})
    blob = json.dumps(a, ensure_ascii=False)
    for name in ("Наработка, моточасов", "Условия эксплуатации", "Охрана и контроль на площадке", "Кто управляет машиной"):
        ok(f"нигде нет «{name}: не указано»", f"{name}: не указано" not in blob)
    txt = docx_text(a["id"])
    ok("Word: 6 500 моточасов и водители", "6 500 моточасов" in txt and "2 машиниста" in txt, txt[:600])
    ok("PDF: 6 500 моточасов", "6 500 моточасов" in pdf_text(a["id"]))
    return a["id"]


# ------------------------------------------------------------------ 4. адрес с ПД

def check_address_pd():
    print("4. Адрес объекта без персональных данных")
    T.fresh()
    for addr, what in (("Ташкент, Иванов Иван Иванович", "ФИО"), ("Ташкент, тел. +998 90 123 45 67", "телефон"),
                       ("Ташкент " * 40, "длина")):
        body = json.loads(json.dumps(SHOP))
        body["optional"]["address"] = addr
        st, a = T.call("POST", "/act/make", body)
        ok(f"адрес с ПД ({what}) — 422, ошибка в поле address", st == 422 and "address" in (a.get("errors") or {}), a)


# ------------------------------------------------------------------ 5. часы

def check_clock():
    print("5. Дата и номер акта по Ташкенту")
    orig = common._utcnow
    try:
        common._utcnow = lambda: datetime(2026, 10, 13, 23, 30, tzinfo=timezone.utc)
        ok("23:30 UTC 13.10.2026 → 04:30 14.10.2026 по Ташкенту", common._now() == datetime(2026, 10, 14, 4, 30)
           and str(common._today()) == "2026-10-14", common._now())
        ok("без базы поясов — постоянный сдвиг +5 из настроек (Windows без tzdata)",
           common._zone("Asia/NoSuchZone", 5).utcoffset(None) == timedelta(hours=5))
        T.fresh()
        st, a = T.call("POST", "/act/make", SHOP)
        ok("акт создан в 23:30 UTC", st == 200, a if st != 200 else "")
        if st != 200:
            return
        ok("дата акта — 14.10.2026 (следующий день)", a["date"] == "14.10.2026", a["date"])
        ok("время акта — 04:30 по Ташкенту", a["created_at"] == "2026-10-14T04:30:00", a["created_at"])
        ok("номер акта начинается с 20261014", a["number"].startswith("20261014-"), a["number"])
        ok("шапка: та же дата", any(r["value"] == "14.10.2026" for r in a["header"]), a["header"])
        ok("хранение — 7 дней от времени акта", a["expires_at"] == "2026-10-21T04:30:00", a["expires_at"])
        st, x = T.call("GET", f"/act/{a['id']}")
        ok("акт открывается в те же часы", st == 200 and x.get("number") == a["number"])
        with db.tx() as con:
            act.cleanup(con)
        ok("очистка в те же часы акт не трогает", T.call("GET", f"/act/{a['id']}")[0] == 200)
        common._utcnow = lambda: datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc)     # 05:00 21.10 по Ташкенту
        with db.tx() as con:
            act.cleanup(con)
        ok("через 7 дней по Ташкенту очистка удаляет акт", T.call("GET", f"/act/{a['id']}")[0] == 404)
    finally:
        common._utcnow = orig


def main():
    T.ORIG.update(post=llm._post)
    llm._post = T.no_network
    folder = Path(tempfile.mkdtemp(prefix="surveyor-facts-"))
    try:
        with temp_db("surveyor-facts-test.db"):
            db.ensure_schema()
            act.DIR = folder
            guest.reset()
            check_units()
            check_shop()
            check_excavator()
            check_address_pd()
            check_clock()
    finally:
        llm._post = T.ORIG["post"]
        shutil.rmtree(folder, ignore_errors=True)
    print(f"\nитог: ок {passed}, плохо {failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
