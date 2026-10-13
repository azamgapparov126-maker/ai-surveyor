"""
ТЗ разработчику от 13.10.2026, день 9.1: акт на две страницы A4 (макет — приложение A).

Запуск из корня:
    set PYTHONIOENCODING=utf-8
    sandbox\\.venv\\Scripts\\python.exe tests\\test_act_2pages.py            # проверки
    sandbox\\.venv\\Scripts\\python.exe tests\\test_act_2pages.py --samples  # + образцы PDF на Рабочий стол

Работает на копии базы (tests/tmpdb.py), в сеть не ходит; ответ модели по фото подменён (как в tests/test_act.py).
  1. Чистые функции: QR (эталонные векторы Рида — Соломона, строки формата и версии; обратное чтение матрицы),
     EXIF (дата, координаты, поворот), маскировка госномера / VIN.
  2. Семь документов: 4 объекта теста — легковой 0301 с фото (EXIF) и повреждениями, автокран 0318 с фото,
     магазин 0821 с затратной оценкой, груз 0701 с биржевой оценкой — и 3 учебных договора (tests/fixtures/contracts:
     DOCX, PDF с текстом, скан с ответом модели) на RU, UZ, EN:
       ≤ 2 страниц (pymupdf page_count), с фото — ровно 2, без фото — 1 допустима;
       пояснение ставки ≤ 3 строк; шрифт ≥ 9 pt; пять разделов по порядку;
       нет «не указано» / «данные недоступны» по заданным полям объекта, сами значения на месте;
       нет ссылок на источники (кроме ссылки «Как посчитано») и формул (кроме цепочки множителей ставки).
  3. Стр. 2: фото без документов (техпаспорт не выводится), подпись снимка с датой и координатами из EXIF,
     подписи «Сюрвейер / Страхователь», QR-код и ссылка GET /act/{id}?how=1; эта ссылка отдаёт «Как посчитано».
"""
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["SURVEYOR_NO_BACKGROUND"] = "1"

import pymupdf                                            # noqa: E402

import test_act as T                                      # noqa: E402  ASGI-клиент и заглушки сети
import test_valuation_tz as VT                            # noqa: E402  учебные биржевые сделки
from tmpdb import ensure_market_stats, ensure_stat_series, temp_db   # noqa: E402
from app import act, act_engine as ae, act_extras as ax, contract_read as cr, db, guest, llm   # noqa: E402
from app import act_texts as tx, valuation as V             # noqa: E402
from app.act_pkg import compact, exif, export, qr           # noqa: E402
from app.act_pkg.documents import ct_clean                  # noqa: E402
from app.act_pkg.recognize import parse_model               # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "contracts"
DESKTOP = Path(os.path.expanduser("~")) / "Desktop"
LANGS = ("ru", "uz", "en")
passed, failed = 0, 0
PAGES = {}                                                # (документ, язык) → страниц, для отчёта


def ok(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ок  ", name)
    else:
        failed += 1
        print("  ПЛОХО", name, str(extra)[:500])


# ------------------------------------------------------------------ 1. чистые функции

def check_units():
    print("1. QR, EXIF, маскировка")
    data = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236]          # «HELLO WORLD», 1-Q (ISO 18004)
    ok("Рид — Соломон: эталон «HELLO WORLD» 1-Q", qr.rs_ecc(data, 13) ==
       [168, 72, 22, 82, 217, 54, 156, 0, 46, 15, 180, 122, 16])
    ok("строка формата M, маски 0 и 4; строка версии 7 — по таблицам ISO 18004",
       format(qr._bch_format(0), "015b") == "101010000010010" and format(qr._bch_format(4), "015b") ==
       "100010111111001" and format(qr._bch_version(7), "018b") == "000111110010010100")
    for s in ("/act/0123456789abcdef?how=1&lang=ru", "https://inson-surveyor.up.railway.app/act/0123456789abcdef"
              "?how=1&lang=uz", "x" * 200):
        m = qr.matrix(s)
        d = qr.decode_self(m)
        ok(f"QR {len(s)} байт: версия {d['version']}, обе копии формата, остатки RS = 0, текст читается обратно",
           d["text"] == s and d["rs_ok"] and d["format_copies_equal"] and d["mode"] == 4 and len(m) == 17 + 4 *
           d["version"], d)
    try:
        qr.matrix("x" * 300)
        ok("QR длиннее 213 байт — ValueError", False)
    except ValueError:
        ok("QR длиннее 213 байт — ValueError", True)
    j = T.image(kind="jpg")
    b = exif.with_exif(j, taken="2026-10-13 10:12", lat=41.31123, lon=69.27961, orientation=6)
    ok("EXIF: дата, координаты, поворот читаются; без EXIF и мусор — пусто",
       exif.read(b) == {"taken": "2026-10-13 10:12", "lat": 41.31123, "lon": 69.27961, "orientation": 6}
       and exif.read(j)["taken"] is None and exif.read(b"\xff\xd8\xff\xe1\x00\x02") ["lat"] is None)
    ok("JPEG с EXIF открывается", pymupdf.Pixmap(b).width == 320)
    ok("маскировка: VIN LXGCPA393TA006921 → LXG••••••••••6921, госномер 01A123BC → 01•••••C",
       compact.mask_id("LXGCPA393TA006921") == "LXG••••••••••6921" and compact.mask_id("01 A 123 BC") ==
       "01•••••C", (compact.mask_id("LXGCPA393TA006921"), compact.mask_id("01 A 123 BC")))


# ------------------------------------------------------------------ 2. семь документов

CAR_VIEWS = ("front", "back", "left", "right", "plate", "odometer", "doc")


def car_files() -> list:
    out = []
    for i, v in enumerate(CAR_VIEWS):
        j = T.image((40 + 20 * i, 90, 160), kind="jpg", w=640, h=480)
        if v != "doc":
            j = exif.with_exif(j, taken=f"2026-10-13 10:{12 + i:02d}", lat=41.31123, lon=69.27961)
        out.append((f"{v}.jpg", "image/jpeg", j))
    return out


def make_car() -> dict:
    T.REPLY["text"] = T.car_reply(T.CAR_DAMAGES)
    T.model_on(True)
    st, b = T.upload(car_files(), {"lang": "ru", "product_code": "0301"})
    assert st == 200 and b.get("ok"), (st, b)
    st, a = T.call("POST", "/act/make", {"session": b["session"], "lang": "ru", "must": T.CAR_MUST,
                                         "optional": dict(T.CAR_OPT, object_kind="car", year=2022),
                                         "recognized": b["recognized"]})
    assert st == 200, (st, a)
    return a


def make_crane() -> dict:
    T.REPLY["text"] = T.CRANE_REPLY
    T.model_on(True)
    st, b = T.upload(T.CRANE_FILES, {"lang": "ru", "product_code": "0318"})
    assert st == 200 and b.get("ok"), (st, b)
    st, a = T.call("POST", "/act/make", {"session": b["session"], "lang": "ru", "must": T.CRANE_MUST,
                                         "optional": dict(T.CRANE_OPT, object_kind="truck_crane"),
                                         "recognized": b["recognized"]})
    assert st == 200, (st, a)
    return a


SHOP = {"lang": "ru", "must": {"product_code": "0821", "sum_insured": 3_000_000_000, "object_value": 3_000_000_000,
                               "region_code": "tashkent_city", "region": "город Ташкент"},
        "optional": {"object_kind": "shop", "material": "brick", "year": 2016,
                     "address": "г. Ташкент, Юнусабадский район, ул. Амира Темура, 1",
                     "class_fields": {"area_m2": 450, "floors": 2, "wear": "15 %"}}}
CARGO = {"lang": "ru", "must": {"product_code": "0701", "sum_insured": 1_500_000_000, "object_value": 1_500_000_000,
                                "region": "Ташкент"},
         "optional": {"object_kind": "cargo", "class_fields": {"cargo_kind": "дизельное топливо"}, "quantity": 120,
                      "unit": "т"}}


def make_plain(body: dict) -> dict:
    st, a = T.call("POST", "/act/make", body)
    assert st == 200, (st, a)
    return a


def contract_fields(name: str) -> dict:
    if name == "contract_scan.pdf":
        c = parse_model((FIX / "contract_scan.model.json").read_text(encoding="utf-8"), 1)["contract"]
    else:
        with db.tx() as con:
            c = ax.parse_document(con, FIX / name, "8", ae.DEFAULT_SETTINGS["limits"])["contract"]
    return ct_clean(c["fields"])[0]


def make_contract(name: str) -> dict:
    f = contract_fields(name)
    must = {"product_code": f.get("product_code"), "sum_insured": f.get("sum_insured"),
            "object_value": f.get("object_value") or f.get("sum_insured"), "region": f.get("region") or "Ташкент"}
    return make_plain({"lang": "ru", "must": must, "optional": {"contract": cr.request_of(f, "document")}})


def pdf_of(aid: str, lang: str):
    st, blob, h = T.call("GET", f"/act/{aid}.pdf", params={"lang": lang}, raw=True)
    assert st == 200, (st, blob[:200])
    return pymupdf.open(stream=blob, filetype="pdf"), blob


def norm(s: str) -> str:
    """Текст PDF как написан: дефис шрифт отдаёт мягким переносом, точку-разделитель — «ꞏ»."""
    return re.sub(r"\s+", " ", str(s).replace("­", "-").replace("ꞏ", "·")).replace(" ", " ").strip()


def text_of(doc) -> str:
    return norm(" ".join(p.get_text() for p in doc))


FORMULA_MARKS = ("Σ", "median", "clip(", "r_нетто", "r_net", "r_брутто", "V = ", "PML =", "EML =", "MFL =",
                 "K_констр", "K_constr", "= S", "× срок", "VWAP", "T/T_", "(1 − Z)", "Z ×")
SOURCE_MARKS = ("http://", "https://", "www.", "Источник", "источник:", "Manba", "Source:", "avtoelon", "uzex",
                "build_cost_m2", "lex.uz", "stat.uz", "data.egov")


def check_doc(name: str, aid: str, lang: str, act_json: dict, has_photos: bool, must_have: tuple = ()):
    doc, blob = pdf_of(aid, lang)
    n = doc.page_count
    PAGES[(name, lang)] = n
    ok(f"{name} [{lang}]: страниц {n} ≤ 2" + (" (с фото — ровно 2)" if has_photos else ""),
       n <= 2 and (n == 2 if has_photos else True), n)
    txt = text_of(doc)
    titles = [t for t in (tx.t(k, lang) for k in ("a2_s1", "a2_s2", "a2_s3", "a2_s4", "a2_s5"))]
    pos = [txt.find(f"{i}. {tt}") for i, tt in enumerate(titles, 1)]
    ok(f"{name} [{lang}]: пять разделов по порядку на стр. 1",
       all(p >= 0 for p in pos) and pos == sorted(pos) and all(f"{i}. {tt}" in norm(doc[0].get_text())
                                                              for i, tt in enumerate(titles, 1)), (pos, txt[:300]))
    sizes = [s["size"] for p in doc for b in p.get_text("dict")["blocks"] for ln in b.get("lines", [])
             for s in ln["spans"] if s["text"].strip()]
    ok(f"{name} [{lang}]: шрифт не меньше 9 pt (минимум {min(sizes):.1f})", min(sizes) >= 9 - 1e-6, min(sizes))
    # пояснение ставки ≤ 3 строк и оно в документе
    a = act_json[lang]
    V0 = compact.compact_view(a, {})
    E = V0["s4"]["explain"]
    if E.get("main"):
        regular, _b = export._fonts()
        lines = export.explain_fit(E, lang, regular)
        flat = norm(txt)
        ok(f"{name} [{lang}]: пояснение ставки ≤ 3 строк ({len(lines)}) и стоит в документе",
           len(lines) <= 3 and all(norm(ln).replace("…", "")[:40]
                                   in flat for ln in lines), lines)
    na_words = {tx.t("na", lang), tx.t("s1_not_specified", lang)}
    if lang == "ru":
        na_words.add("не указано")
    bad = [w for w in na_words if w.lower() in txt.lower()]
    ok(f"{name} [{lang}]: нет «{tx.t('s1_not_specified', lang)}» / «{tx.t('na', lang)}» в документе", not bad, bad)
    miss = [w for w in must_have if norm(w) not in txt]
    ok(f"{name} [{lang}]: заданные поля объекта на месте {list(must_have)[:4]}", not miss, miss)
    how = f"/act/{aid}?how=1&lang={lang}"
    rest = re.sub(r"\S*" + re.escape(how), "", txt)
    src = [m for m in SOURCE_MARKS if m in rest]
    ok(f"{name} [{lang}]: нет ссылок на источники (кроме «Как посчитано»)", not src, src)
    frm = [m for m in FORMULA_MARKS if m in rest]
    if re.search(r"(?<![A-Za-z])S × ", rest):
        frm.append("S ×")
    ok(f"{name} [{lang}]: формул нет, кроме цепочки множителей", not frm, frm)
    ok(f"{name} [{lang}]: ссылка «Как посчитано» и подписи на последней странице",
       how in norm(doc[-1].get_text()) and tx.t("a2_sign_surveyor", lang) in doc[-1].get_text()
       and tx.t("a2_sign_holder", lang) in doc[-1].get_text(), doc[-1].get_text()[-300:])
    return doc, blob


def act_langs(aid: str) -> dict:
    out = {}
    for lang in LANGS:
        st, a = T.call("GET", f"/act/{aid}", params={"lang": lang})
        out[lang] = a
    return out


def check_documents(samples: bool):
    print("2. Семь документов × 3 языка: ≤ 2 страниц, пояснение ≤ 3 строк, без «не указано», источников и формул")
    with db.tx() as con:
        V.ensure_build_cost(con)
        con.execute("DELETE FROM build_cost_m2")
        V.build_cost_seed(con)
        con.execute("DELETE FROM exchange_quotes")
        VT.seed_quotes(con, [(2, 60, "тонна", 12_000_000), (5, 20, "тонна", 12_600_000),
                             (10, 20_000, "килограмм", 11_800_000)])
    T.fresh()
    docs = {}
    car = make_car()
    docs["легковой 0301"] = (car, True, ("Chevrolet Cobalt", "2022", "царапины (левое крыло)"))
    crane = make_crane()
    docs["автокран 0318"] = (crane, True, ("XCMG QY50K5D", "2026"))
    shop = make_plain(SHOP)
    docs["магазин 0821"] = (shop, False, ("Амира Темура", "450 м²", "15 % (до 50 %)"))
    cargo = make_plain(CARGO)
    docs["груз 0701"] = (cargo, False, ())
    for nm in ("contract_fire.docx", "contract_pledge.pdf", "contract_scan.pdf"):
        docs["договор " + nm] = (make_contract(nm), False, ())
    ok("магазин: затратная оценка, груз: биржевая", shop["valuation"]["method"] == "cost"
       and cargo["valuation"]["method"] == "exchange", (shop["valuation"].get("method"), cargo["valuation"].get("method")))
    for name, (a, photos, must) in docs.items():
        js = act_langs(a["id"])
        for lang in LANGS:
            want = must if lang == "ru" else tuple(m for m in must if not re.search("[А-Яа-яЁё]", m))
            if name.startswith("магазин") and lang != "ru":
                want = ("450 m²", "15%" if lang == "en" else "15 %")
            check_doc(name, a["id"], lang, js, photos, want)
    # сверка с договором в документе: итог и нарушения
    d, _b = pdf_of(docs["договор contract_pledge.pdf"][0]["id"], "ru")
    tt = text_of(d)
    ok("договор в залоге: «Сверка с договором: Отказать / согласовать с ЦО» и вердикт «Отказать / согласовать с ЦО»",
       "Сверка с договором: Отказать / согласовать с ЦО" in tt and tt.count("Отказать / согласовать с ЦО") >= 2, tt[:900])
    ok("договор в залоге: нарушение «Минимальная ставка» в списке", "Минимальная ставка:" in tt)
    # стоимость: метод, n, надёжность — словами, без формулы
    d, _b = pdf_of(cargo["id"], "ru")
    tt = text_of(d)
    ok("груз: «Биржевая оценка 1 449 600 000 сум — по 3 биржевым сделкам за 30 дн., …, надёжность высокая»",
       "Биржевая оценка" in tt and "1 449 600 000 сум" in tt and "по 3 биржевым сделкам за 30 дн." in tt
       and "надёжность высокая" in tt, tt[:1200])
    d, _b = pdf_of(shop["id"], "ru")
    tt = text_of(d)
    ok("магазин: «Затратная оценка … затратный метод: 450 м², износ 12,5 %», S/V 117,2 % — превышение (ГК ст. 938) и "
       "нужно решение по сумме", "Затратная оценка" in tt and "затратный метод: 450 м², износ 12,5 %" in tt
       and "S/V 117,2 %" in tt and "ст. 938" in tt and "Принять рыночную" in tt, tt[:1500])
    # легковой: фото, EXIF, повреждения, цепочка
    d, blob = pdf_of(car["id"], "ru")
    p2 = norm(d[1].get_text())
    ok("стр. 2 легкового: шесть фото (техпаспорт не выводится)", len(d[1].get_images()) == 6, len(d[1].get_images()))
    ok("подпись снимка: ракурс, дата и координаты из EXIF", "13.10.2026 10:12 · 41.3112, 69.2796" in p2
       and "Спереди" in p2, p2[:400])
    ok("стр. 1 легкового: картинок нет (фото — только на стр. 2)", not d[0].get_images())
    t1 = norm(d[0].get_text())
    ok("легковой: исключение предсуществующих повреждений в условиях",
       "Исключение: предсуществующие повреждения — царапины (левое крыло), вмятина (задний бампер)" in t1, t1[-900:])
    ok("легковой: цепочка множителей — нетто, θ / c / L, K_об, K_рег, K_кл, минимум, ставка акта",
       all(w in t1 for w in ("Нетто-ставка", "× (1 + θ", "× K_об", "× K_рег", "× K_кл", "Минимум INSON",
                             "Ставка акта")), t1[:2000])
    ok("легковой: госномер / VIN маскированы, полного номера нет", "LXGCPA393TA006921" not in t1)
    # «Как посчитано»
    st, h = T.call("GET", f"/act/{car['id']}", params={"how": 1, "lang": "ru"})
    ok("GET /act/{id}?how=1 — «Как посчитано»: rate_trace, how_computed, вилка, сценарии",
       st == 200 and h.get("how") and h["rate"].get("rate_trace") and h.get("how_computed", {}).get("rate_fork")
       and "sections" not in h and h["calibrated"] == 0, (st, sorted(h)[:20] if isinstance(h, dict) else h))
    url = compact.how_url(car, {})
    ok("QR стр. 2 кодирует ту же ссылку (обратное чтение матрицы)",
       qr.decode_self(qr.matrix(f"/act/{car['id']}?how=1&lang=ru"))["text"] == f"/act/{car['id']}?how=1&lang=ru"
       and url.startswith("/act/"))
    # Word: те же разделы, без ошибок
    st, wb, _h = T.call("GET", f"/act/{car['id']}.docx", params={"lang": "ru"}, raw=True)
    ok("Word отдаётся и содержит пять разделов и подписи", st == 200 and all(
        x in re.sub(r"<[^>]+>", "", __import__("zipfile").ZipFile(__import__("io").BytesIO(wb)).read(
            "word/document.xml").decode("utf-8")) for x in ("1. Объект", "5. Условия", "Сюрвейер")))
    if samples:
        for aid, fname in ((car["id"], "Акт 2 стр — легковой (образец).pdf"),
                           (crane["id"], "Акт 2 стр — автокран (образец).pdf")):
            _d, blob = pdf_of(aid, "ru")
            (DESKTOP / fname).write_bytes(blob)
            print("   образец:", DESKTOP / fname)


def main():
    samples = "--samples" in sys.argv
    T.ORIG.update(chat_raw=llm.chat_raw, enabled=llm.enabled, supports_files=llm.supports_files, post=llm._post)
    llm.chat_raw = T.fake_chat_raw
    llm._post = T.no_network
    folder = Path(tempfile.mkdtemp(prefix="surveyor-2pages-"))
    try:
        with temp_db("surveyor-2pages-test.db"):
            db.ensure_schema()
            ensure_market_stats()
            ensure_stat_series()
            with db.tx() as con:
                con.execute("DELETE FROM company_financials")
                con.execute("INSERT INTO company_financials (report_date, own_funds, reserves, source) "
                            "VALUES ('2026-07-01', 180e9, 240e9, 'временно, до данных бухгалтерии (тест)')")
            act.DIR = folder
            guest.reset()
            check_units()
            check_documents(samples)
    finally:
        llm.chat_raw = T.ORIG["chat_raw"]
        llm._post = T.ORIG["post"]
        llm.enabled = T.ORIG["enabled"]
        llm.supports_files = T.ORIG["supports_files"]
        shutil.rmtree(folder, ignore_errors=True)
    print("\nСтраниц по документам (RU / UZ / EN):")
    for name in dict.fromkeys(k[0] for k in PAGES):
        print(f"   {name}: " + " / ".join(str(PAGES.get((name, lg), "—")) for lg in LANGS))
    print(f"\nитог: ок {passed}, плохо {failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
