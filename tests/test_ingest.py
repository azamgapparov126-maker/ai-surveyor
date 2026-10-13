"""
Контрольные примеры приёма документов (app/ingest.py).
Запуск:  sandbox\\.venv\\Scripts\\python.exe tests/test_ingest.py

Офлайн: все документы для проверки создаём сами — DOCX через zipfile, XLSX через openpyxl,
PDF через pymupdf. Сеть не нужна, ИИ не нужен (без ключа работают только регулярки).

Проверяем:
  1) определение языка: uz-latn, ru, en;
  2) определение вида документа: техпаспорт, кадастр, штатное расписание;
  3) извлечение полей и чтение таблицы из DOCX;
  4) отказ по .doc и .xls с понятным сообщением;
  5) скан без текстового слоя → «нужно распознавание», поля пустые;
  6) ЗРУ-547 (PD-01): ФИО, адрес физлица, паспорт, ПИНФЛ и телефон не попадают ни в поля
     и факты, ни в текст и таблицы, сохранённые в document_extracts;
  7) весь конвейер ingest_file и сводки fields_for_request / summary_for_request.

Рабочая база НЕ трогается: тест делает временную копию data/surveyor.db во временной папке
и работает только в ней (см. _sandbox()).
"""
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db                                        # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="ingest_"))


def _sandbox():
    """Временная копия базы и отдельная папка файлов: рабочая база остаётся нетронутой."""
    src = db.DB_PATH
    dst = TMP / "surveyor.db"
    db.snapshot(src, dst)               # штатная копия SQLite: учитывает журнал WAL рабочей базы
    db.DB_PATH = dst
    db.DATA_DIR = TMP
    from app import ingest, photos
    photos.ROOT = TMP
    photos.PHOTOS_DIR = TMP / "photos"
    ingest.ROOT = TMP
    return ingest, photos


ingest, photos = _sandbox()
from app import docparse as D                             # noqa: E402

MADE_REQUESTS = []

# --------------------------------------------------------------------------- #
# Тексты документов. Персональные данные в них есть намеренно: проверяем, что их не берут.
# --------------------------------------------------------------------------- #

PASSPORT_UZ_ROWS = [
    ["Markasi va modeli", "Chevrolet Cobalt"],
    ["Ishlab chiqarilgan yili", "2019"],
    ["Kuzov (shassi) raqami", "KA123456789"],
    ["Dvigatel raqami", "B15D2123456"],
    ["Rangi", "oq"],
    ["O‘rindiqlar soni", "5"],
    ["Egasi", "Karimov Aziz Akmalovich"],
    ["Yashash manzili", "Toshkent shahri, Amir Temur ko‘chasi, 15-uy"],
    ["Pasport", "AA 1234567"],
]
PASSPORT_UZ_PARAS = [
    "AVTOMOTOTRANSPORT VOSITASI TEXNIK PASPORTI",
    "Ro‘yxatdan o‘tganlik guvohnomasi",
    "Ushbu guvohnoma transport vositasi davlat ro‘yxatidan o‘tganligini tasdiqlaydi va "
    "uning egasiga beriladi. Hujjat bilan birga sug‘urta polisi ham bo‘lishi kerak, "
    "chunki bu qonun talabi hisoblanadi.",
]

CADASTRE_RU_TEXT = """КАДАСТРОВЫЙ ПАСПОРТ ОБЪЕКТА НЕДВИЖИМОСТИ
Выписка из государственного реестра прав на недвижимое имущество
Кадастровый номер: 10:09:05:01:02:1234
Местонахождение: город Ташкент, Мирзо-Улугбекский район, дом 28, кв. 41
Вид объекта недвижимости: квартира
Площадь зданий и сооружений: 74,5 кв. м
Число комнат: 3
Этажность: 9
Год постройки: 2007
Вид права: частная собственность
Кадастровая стоимость: 185000
Наличие запрета, ареста или ограничений: ипотека в пользу банка
Правообладатель: Каримов Азиз Акмалович
Место жительства: город Ташкент, дом 28, кв. 41
Настоящая выписка выдана для представления по месту требования и действительна
в течение срока, который установлен законом, а также может быть проверена в реестре.
"""

STAFF_EN_ROWS = [
    ["STAFFING TABLE FOR THE YEAR 2026", "", "", ""],
    ["Structural unit", "Position", "Headcount", "Salary"],
    ["Administration", "Director", "1", "12000000"],
    ["Administration", "Accountant", "2", "8000000"],
    ["Sales", "Agent", "12", "6000000"],
    ["Total number of employees", "", "15", ""],
    ["Payroll fund", "", "", "118000000"],
    ["Full name", "Karimov Aziz Akmalovich", "", ""],
]
# Техпаспорт с персональными данными в таблице: проверяем, что в базу они не попадают
PASSPORT_RU_PD_ROWS = [
    ["Марка и модель", "Chevrolet Malibu"],
    ["Год выпуска", "2021"],
    ["Номер кузова", "KL1234567890ABCDE"],
    ["Владелец", "Мирзаумаров Жасур Алишерович"],
    ["Адрес проживания", "г. Ташкент, ул. Шота Руставели, дом 12, кв. 5"],
    ["Паспорт", "AA 1234567"],
    ["ПИНФЛ", "31234567890123"],
    ["Телефон", "+998 90 123-45-67"],
]
PASSPORT_RU_PD_PARAS = [
    "ТЕХНИЧЕСКИЙ ПАСПОРТ ТРАНСПОРТНОГО СРЕДСТВА",
    "Свидетельство о регистрации транспортного средства выдано владельцу.",
]
PD_SECRETS = ("Мирзаумаров", "Жасур", "Алишерович", "Шота Руставели", "AA 1234567",
              "31234567890123", "+998 90 123-45-67")

# Кадастр в XLSX: рядом стоят адрес ОБЪЕКТА (он нужен для оценки) и персональные данные
# владельца (их сохранять нельзя). Подписи — распространённые формулировки бланков.
CADASTRE_PD_ROWS = [
    ["ВЫПИСКА ИЗ ГОСУДАРСТВЕННОГО РЕЕСТРА ПРАВ НА НЕДВИЖИМОЕ ИМУЩЕСТВО", ""],
    ["Кадастровый номер", "10:09:05:01:0123:0001"],
    ["Местонахождение объекта", "город Ташкент, Мирабадский район, улица Нукус, дом 7"],
    ["Вид объекта недвижимости", "квартира"],
    ["Общая площадь", "74,5"],
    ["Год постройки", "2007"],
    ["Ф.И.О. собственника", "Мирзаумаров Жасур Алишерович"],
    ["Страхователь", "Мирзаумаров Жасур Алишерович"],
    ["Адрес проживания", "г. Ташкент, ул. Шота Руставели, дом 7, кв. 3"],
    ["Адрес регистрации", "г. Ташкент, ул. Шота Руставели, дом 7, кв. 3"],
    ["Домашний адрес", "г. Ташкент, ул. Шота Руставели, дом 7, кв. 3"],
    ["Паспорт", "AA 1234567"],
    ["ПИНФЛ", "31234567890123"],
]
OBJECT_ADDRESS = "город Ташкент, Мирабадский район, улица Нукус, дом 7"

# Договор на английском: серия и номер полиса — реквизит договора, а не паспорт человека
CONTRACT_EN_TEXT = """INSURANCE CONTRACT
Policy series and number: AB 0001234
Policyholder: Zhasur Mirzaumarov
Insured sum: 500 000 000 UZS
"""

# Тот же отчёт оценщика по-английски: подпись «market value» попадает в заголовок
VALUATION_EN_TEXT = """VALUATION REPORT ON THE MARKET VALUE OF THE PROPERTY
Valuation date: 01.06.2026
The valuation report is prepared by the appraisal company under a contract.
Market value: 850 000 000 UZS
"""

VALUATION_RU_TEXT = """ОТЧЁТ ОБ ОЦЕНКЕ РЫНОЧНОЙ СТОИМОСТИ ОБЪЕКТА НЕДВИЖИМОСТИ
Отчёт об оценке составлен оценочной организацией по договору.
Дата оценки: 01.06.2026
Рыночная стоимость: 850 000 000 сум
"""


STAFF_EN_NOTE = ("This staffing table is approved by the director of the company and is valid "
                 "from the first day of the year until it is replaced by a new one.")


# --------------------------------------------------------------------------- #
# Сборка тестовых файлов стандартной библиотекой
# --------------------------------------------------------------------------- #

def make_docx(path: Path, paragraphs: list, rows: list) -> Path:
    """Минимальный DOCX: абзацы w:p и одна таблица w:tbl. Никаких пакетов, только zipfile."""
    def esc(s):
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    body = "".join('<w:p><w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>' % esc(p)
                   for p in paragraphs)
    trs = []
    for r in rows:
        tcs = "".join('<w:tc><w:p><w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p></w:tc>'
                      % esc(c) for c in r)
        trs.append("<w:tr>%s</w:tr>" % tcs)
    body += "<w:tbl>%s</w:tbl>" % "".join(trs)
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/'
                '2006/main"><w:body>%s</w:body></w:document>' % body)
    ctypes = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
              '<Default Extension="xml" ContentType="application/xml"/>'
              '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
              'relationships+xml"/><Override PartName="/word/document.xml" ContentType='
              '"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main'
              '+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ctypes)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return path


def make_xlsx(path: Path, rows: list, note: str = None) -> Path:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Staffing table"
    for r in rows:
        ws.append(r)
    if note:
        ws.append([note])
    wb.save(path)
    wb.close()
    return path


def make_pdf(path: Path, text: str) -> Path:
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((40, 60), text, fontname="china-s", fontsize=8)
    doc.save(path)
    doc.close()
    return path


def make_scan_pdf(path: Path) -> Path:
    """PDF-«скан»: картинка без единого символа текста."""
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 120))
    pix.clear_with(200)
    page.insert_image(pymupdf.Rect(50, 50, 250, 170), pixmap=pix)
    doc.save(path)
    doc.close()
    return path


def make_old_doc(path: Path) -> Path:
    """Файл старого формата Word: сигнатура OLE2 и немного мусора."""
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 500)
    return path


FILES = {}


def files() -> dict:
    if not FILES:
        FILES["passport_docx"] = make_docx(TMP / "texnik_pasport.docx",
                                           PASSPORT_UZ_PARAS, PASSPORT_UZ_ROWS)
        FILES["cadastre_pdf"] = make_pdf(TMP / "kadastr.pdf", CADASTRE_RU_TEXT)
        FILES["staff_xlsx"] = make_xlsx(TMP / "staffing.xlsx", STAFF_EN_ROWS, STAFF_EN_NOTE)
        FILES["scan_pdf"] = make_scan_pdf(TMP / "scan.pdf")
        FILES["old_doc"] = make_old_doc(TMP / "old.doc")
        FILES["passport_pd_docx"] = make_docx(TMP / "tehpasport_pd.docx",
                                              PASSPORT_RU_PD_PARAS, PASSPORT_RU_PD_ROWS)
        FILES["cadastre_pd_xlsx"] = make_xlsx(TMP / "kadastr_pd.xlsx", CADASTRE_PD_ROWS)
    return FILES


# --------------------------------------------------------------------------- #
# 1. Языки
# --------------------------------------------------------------------------- #

def test_language_uz_latn():
    text, _ = ingest.read_docx(files()["passport_docx"])
    out = ingest.detect_language(text)
    assert out["language"] == "uz-latn", out
    print("язык техпаспорта:", out["language"], out["confidence"])


def test_language_ru():
    out = ingest.detect_language(CADASTRE_RU_TEXT)
    assert out["language"] == "ru", out
    print("язык кадастра:", out["language"], out["confidence"])


def test_language_en():
    text, _ = ingest.read_xlsx(files()["staff_xlsx"])
    out = ingest.detect_language(text)
    assert out["language"] == "en", out
    print("язык штатного расписания:", out["language"], out["confidence"])


def test_language_uz_cyrl():
    text = ("КЎЧМАС МУЛК ОБЪЕКТИНИНГ КАДАСТР ПАСПОРТИ. Жойлашган жойи: Тошкент шаҳри. "
            "Кадастр рақами берилган ва ҳужжат давлат реестридан кўчирма сифатида расмийлаштирилган. "
            "Ушбу ҳужжат бўйича ҳуқуқ тури ва бино ва иншоотлар майдони кўрсатилган.")
    out = ingest.detect_language(text)
    assert out["language"] == "uz-cyrl", out
    print("язык узбекской кириллицы:", out["language"], out["confidence"])


def test_language_short_text_is_none():
    """Коротким текстам язык не приписываем — это было бы гаданием."""
    out = ingest.detect_language("Акт № 5")
    assert out["language"] is None and out["reason"]
    print("короткий текст:", out["reason"])


# --------------------------------------------------------------------------- #
# 2. Виды документов
# --------------------------------------------------------------------------- #

def test_kind_passport_uz():
    text, tables = ingest.read_docx(files()["passport_docx"])
    out = ingest.detect_kind(text, tables, "texnik_pasport.docx")
    assert out["kind"] == D.KIND_PASSPORT, out
    print("вид (uz-latn):", out["kind"], out["confidence"])


def test_kind_cadastre_ru():
    out = ingest.detect_kind(CADASTRE_RU_TEXT, [], "kadastr.pdf")
    assert out["kind"] == D.KIND_CADASTRE, out
    print("вид (ru):", out["kind"], out["confidence"], out["candidates"][:2])


def test_kind_staff_en():
    text, tables = ingest.read_xlsx(files()["staff_xlsx"])
    out = ingest.detect_kind(text, tables, "staffing.xlsx")
    assert out["kind"] == D.KIND_STAFF, out
    print("вид (en):", out["kind"], out["confidence"])


def test_kind_unknown_is_other():
    """Ничего не набрало порога — вид «прочее» и уверенность 0, а не выдуманный ответ."""
    out = ingest.detect_kind("Просто текст о погоде и о том, что сегодня тепло. " * 5, [], "a.pdf")
    assert out["kind"] == ingest.KIND_OTHER and out["confidence"] == 0.0
    print("неизвестный документ:", out["kind"])


# Голые бланки: заголовок и номер, больше ничего. Полис — обязательный документ
# (Закон о страховой деятельности ст. 33-1, Положение 3571), поэтому опознавать обязаны.
# Пара (текст, ожидаемая уверенность): числа сторожат не только вид, но и вес заголовка.
BLANK_TITLES = [("СТРАХОВОЙ ПОЛИС № AB 0001234", 0.750),
                ("ДОГОВОР СТРАХОВАНИЯ № 12", 1.000),
                ("SUG'URTA POLISI № AB 0001234", 1.000),
                ("СУҒУРТА ПОЛИСИ № AB 0001234", 1.000),
                ("INSURANCE POLICY No. AB 0001234", 1.000),
                ("INSURANCE CONTRACT No. 12", 1.000),
                ("Policy no: AB 0001234", 1.000)]

# Учебник: те же слова, но в глубине текста, а не в шапке. Вид ставить нельзя.
BOOK_FILLER = "Страхование как институт известно давно, и его развитие продолжается.\n" * 60
BOOK_DEEP = (BOOK_FILLER + "Страхователь подписывает страховой полис и обязуется "
             "уплатить премию, а договор страхования вступает в силу.\n" + BOOK_FILLER)


def test_kind_blank_titles_are_recognised():
    """Голый заголовок бланка в шапке — это договор (полис), а не «прочее»."""
    for title, want in BLANK_TITLES:
        out = ingest.detect_kind(title, [], None)
        assert out["kind"] == D.KIND_CONTRACT and out["confidence"] >= 0.55, (title, out)
        assert abs(out["confidence"] - want) < 0.001, (title, out["confidence"], want)
        print("бланк:", title, "→", out["kind"], out["confidence"])


def test_kind_head_marker_only_counts_in_head():
    """
    Правило шапки: маркер со словарным флагом head сильный только в шапке документа.
    Тот же маркер в глубине длинного текста вида не даёт — иначе учебник о страховании
    и нормативный акт становятся «договором».
    """
    deep = ingest.detect_kind(BOOK_DEEP, [], "book.txt")
    assert deep["kind"] == ingest.KIND_OTHER, deep
    head = ingest.detect_kind("СТРАХОВОЙ ПОЛИС № AB 0001234\n" + BOOK_DEEP, [], "polis.pdf")
    assert head["kind"] == D.KIND_CONTRACT, head
    print("маркер в тексте:", deep["kind"], "| он же в шапке:", head["kind"], head["confidence"])


# Известные исключения по docs/source: файлов, для которых «прочее» не ожидается, нет.
# Список оставлен пустым намеренно — если сюда что-то попадёт, в отчёте теста это будет видно
# вместе с причиной, а не тихо пропущено (как было с «Тарифной политикой»).
SOURCE_EXCEPTIONS = {}


# Чужие документы, в которых слова «страховой полис» и «договор страхования» стоят
# не в заголовке бланка: оглавление учебника, служебная записка, акт сверки.
BOOK_WITH_CONTENTS = (
    "СТРАХОВАНИЕ. Учебник для высших учебных заведений\n"
    "Издательство, 2024 год\n"
    "Содержание\n"
    "Глава 1. Договор страхования ................ 12\n"
    "Глава 2. Страховой полис .................... 45\n" + BOOK_FILLER)
MEMO_TEXT = (
    "Служебная записка\n"
    "Прошу выдать страховой полис серии AB на автомобиль компании и оформить "
    "договор страхования имущества филиала на 2026 год.\n"
    "Начальник отдела\n"
    + "Обоснование: имущество филиала на балансе не застраховано.\n" * 10)
ACT_TEXT = (
    "АКТ СВЕРКИ ВЗАИМНЫХ РАСЧЁТОВ\n"
    "между организацией и филиалом за 2026 год\n"
    "Основание: страховой полис № AB 0001234 и договор страхования от 01.06.2026\n"
    + "Сальдо на начало периода составляет ноль сумов.\n" * 20)

# Двуязычный бланк: заголовок по-русски, остальное на узбекской латинице.
# Для РУз это норма — бланк ОСГО по ПКМ № 141 печатают сразу на двух языках.
BILINGUAL_POLICY = (
    "СТРАХОВОЙ ПОЛИС № AB 0001234\n"
    "Ushbu hujjat 2026 yil 1 iyun kuni berilgan va bir yil davomida amal qiladi\n"
    "Toshkent shahri, Amir Temur ko'chasi, 15-uy manzilidagi bino uchun\n"
    "Yillik to'lov miqdori 1 200 000 so'm qilib belgilandi\n"
    "Hujjat qonun talablariga muvofiq rasmiylashtirilgan hisoblanadi\n")


def test_kind_foreign_document_is_not_contract():
    """
    Шапка — это строка-заголовок бланка, а не «первые 40 строк». Оглавление учебника,
    служебная записка и акт сверки упоминают полис и договор страхования в первых строках,
    но бланком от этого не становятся.
    """
    for name, text in (("учебник с оглавлением", BOOK_WITH_CONTENTS),
                       ("служебная записка", MEMO_TEXT),
                       ("акт сверки", ACT_TEXT)):
        out = ingest.detect_kind(text, [], name + ".pdf")
        assert out["kind"] == ingest.KIND_OTHER, (name, out)
        print("чужой документ:", name, "→", out["kind"], out["confidence"],
              "| кандидат:", out["candidates"][:1])


def test_kind_bilingual_blank():
    """
    Двуязычный бланк: русский заголовок, узбекский текст. Штраф ×0.7 за «чужой» язык
    к заголовку бланка не применяется, иначе полис уходил в «прочее» (0.394).
    """
    lang = ingest.detect_language(BILINGUAL_POLICY)["language"]
    out = ingest.detect_kind(BILINGUAL_POLICY, [], None)
    assert out["kind"] == D.KIND_CONTRACT and out["confidence"] >= 0.55, (lang, out)
    print("двуязычный бланк: язык", lang, "→", out["kind"], out["confidence"])


# Класс ошибок, а не три примера: любой КОРОТКИЙ чужой документ, где слова «страховой полис»
# или «договор страхования» стоят в первых строках. Для вида «договор» и «выписка»
# docparse.mark_received закрывает пункт чек-листа «Документ о праве на объект», поэтому
# заявление, прайс-лист или счёт-фактура не должны получать вид ни при каком раскладе.
UZ_FILLER = "Sug'urta ishi uzoq tarixga ega va uning rivoji davom etmoqda.\n" * 60

SHORT_FOREIGN = [
    ("заявление", "ЗАЯВЛЕНИЕ\nПрошу заключить договор страхования склада.\nПодпись\n"),
    ("прайс-лист", "ПРАЙС-ЛИСТ\nУслуги компании на 2026 год\n"
                   "Страховой полис (бланк) — 5 000 сум\n"),
    ("оглавление uz", "MUNDARIJA\n1. Sug'urta polisi ... 5\n"
                      "2. Sug'urta shartnomasi ... 20\n" + UZ_FILLER),
    ("счёт-фактура", "СЧЁТ-ФАКТУРА № 12\nСтраховой полис (услуга) — 5 000 сум\n"),
    ("акт об услугах", "АКТ\nОказаны услуги: страховой полис, 1 шт.\n"),
    ("записка короткая", "СЛУЖЕБНАЯ ЗАПИСКА\nПрошу выдать страховой полис № 5.\nНачальник\n"),
    ("содержание короткое", "СОДЕРЖАНИЕ\n1. Страховой полис ...... 5\n"),
    # прозаическое упоминание сумм не делает документ бланком: реквизит — это метка со значением
    ("оглавление с суммами в тексте",
     "СОДЕРЖАНИЕ\n1. Страховой полис ...... 5\n2. Договор страхования ...... 20\n"
     "Страховая сумма в примере 100 000 000 сум рассчитана условно.\n"
     "Страховая премия в примере 1 200 000 сум рассчитана условно.\n"
     + "Текст учебника о страховании имущества продолжается далее.\n" * 60),
]

# Те же восемь чужих документов по-английски. В английском словаре есть маркеры с номером
# бланка (insurance policy no, policy no, insurance contract number), поэтому фраза
# «please issue an insurance policy No. 5» в шапке давала «договор» с уверенностью 1.000,
# хотя русский аналог «прошу выдать страховой полис № 5» правильно давал «прочее».
EN_FILLER = "Insurance as an institution has a long history and its development continues.\n@" * 60

SHORT_FOREIGN += [
    ("заявление en", "APPLICATION\nPlease conclude an insurance contract for the warehouse.\n"
                     "Signature\n"),
    ("прайс-лист en", "PRICE LIST\nCompany services for 2026\n"
                      "Insurance policy (blank form) - 5 000 UZS\n"),
    ("счёт-фактура en", "INVOICE No. 12\nInsurance policy (service) - 5 000 UZS\n"),
    ("акт об услугах en", "ACT\nServices rendered: insurance policy, 1 pc.\n"),
    ("содержание en", "CONTENTS\n1. Insurance policy ...... 5\n"),
    ("оглавление с суммами в тексте en",
     "CONTENTS\n1. Insurance policy ...... 5\n2. Insurance contract ...... 20\n"
     "Sum insured of 100 000 000 UZS in the example is calculated conditionally.\n"
     "Insurance premium of 1 200 000 UZS in the example is calculated conditionally.\n"
     + EN_FILLER),
    ("акт сверки en", "RECONCILIATION ACT OF MUTUAL SETTLEMENTS\n"
                      "between the company and the branch for 2026\n"
                      "Basis: insurance policy No. AB 0001234 and insurance contract "
                      "dated 01.06.2026\n"
                      + "The opening balance for the period is zero UZS.\n" * 20),
    ("учебник с оглавлением en",
     "INSURANCE. A textbook for higher education institutions\n"
     "Publishing house, 2024\nContents\n"
     "Chapter 1. Insurance contract ................ 12\n"
     "Chapter 2. Insurance policy No. 5 ............ 45\n" + EN_FILLER),
]



def test_kind_short_foreign_documents_are_not_contract():
    """
    Заголовка мало: маркер с флагом head становится сильным, только если строка —
    действительно заголовок бланка (кроме маркера значащих слов нет) И есть второй признак
    бланка: номер в той же строке или заполненные реквизиты. Иначе заявление, прайс-лист,
    счёт-фактура, акт, служебная записка и оглавление закрывали бы чужим файлом
    обязательный пункт чек-листа «Документ о праве на объект».
    """
    for name, text in SHORT_FOREIGN:
        out = ingest.detect_kind(text, [], name + ".pdf")
        assert out["kind"] == ingest.KIND_OTHER, (name, out)
        print("короткий чужой документ:", name, "→", out["kind"], out["confidence"],
              "| кандидат:", out["candidates"][:1])


# Класс, а не один пример: ФРАЗА с номером бланка в шапке чужого документа. Маркеры вида
# «номер бланка» (название бланка + слово «номер/серия»: «номер полиса», insurance policy no,
# polis raqami) весят 4-5 и в шапке делали сильным любой документ, где бланк лишь упомянут.
# Проверяем на всех четырёх написаниях — ошибка была только в английском, но правило общее.
PHRASE_WITH_BLANK_NUMBER = [
    ("записка ru", "СЛУЖЕБНАЯ ЗАПИСКА\nПрошу сообщить номер полиса № 5 по филиалу.\n"
                   "Начальник отдела\n"),
    ("записка ru серия", "СЛУЖЕБНАЯ ЗАПИСКА\nПрошу выдать страховой полис серия AB № 5.\n"),
    ("записка en", "MEMORANDUM\nPlease issue an insurance policy No. 5 for the branch\n"),
    ("записка en длинная", "MEMORANDUM\nPlease issue an insurance policy No. 5 for the branch "
                           "and conclude an insurance contract for 2026.\n"
                           "Head of department\n"
                           + "Justification: the branch property is not insured.\n" * 10),
    ("записка uz-latn", "XIZMAT XATI\nIltimos, filial uchun sug'urta polisi seriya AB ni "
                        "bering.\nBo'lim boshlig'i\n"),
    ("записка uz-cyrl", "ХИЗМАТ ХАТИ\nИлтимос, филиал учун суғурта полиси рақами 5 ни "
                        "беринг.\nБўлим бошлиғи\n"),
]

# Тот же маркер номера бланка, но в строке-заголовке: это настоящий бланк, вид обязан остаться
BLANK_NUMBER_TITLES = [
    ("полис ru", "СТРАХОВОЙ ПОЛИС серия AB № 0001234\n"),
    ("полис en", "INSURANCE POLICY No. AB 0001234\n"),
    ("polis uz-latn", "SUG'URTA POLISI seriya AB № 0001234\n"),
    ("полис uz-cyrl", "СУҒУРТА ПОЛИСИ РАҚАМИ AB 0001234\n"),
]


def test_kind_blank_number_marker_needs_title_line():
    """
    Маркер «номер бланка» силён только в строке-заголовке — как и маркер с флагом head.
    Иначе служебная записка «прошу выдать полис № 5» и учебник с главой «Insurance policy
    No. 5» закрывали бы чужим файлом пункт чек-листа «Документ о праве на объект».
    """
    for name, text in PHRASE_WITH_BLANK_NUMBER:
        out = ingest.detect_kind(text, [], name + ".pdf")
        assert out["kind"] == ingest.KIND_OTHER, (name, out)
        print("фраза с номером бланка:", name, "→", out["kind"], out["confidence"])
    for name, text in BLANK_NUMBER_TITLES:
        out = ingest.detect_kind(text, [], None)
        assert out["kind"] == D.KIND_CONTRACT and out["confidence"] >= 0.55, (name, out)
        print("номер бланка в заголовке:", name, "→", out["kind"], out["confidence"])
    # маркеры-реквизиты («номер кузова», «кадастровый номер») под правило не попадают
    assert ingest._is_number_marker("insurance policy no")
    assert ingest._is_number_marker("polis raqami")
    assert not ingest._is_number_marker("kadastr raqami")
    assert not ingest._is_number_marker("body number")



# Бланк с заполненными реквизитами (метка со значением) — второй признак бланка
BLANK_REQUISITES_RU = ("Объект страхования: склад по адресу г. Ташкент, ул. Навои, 1\n"
                       "Страховая сумма: 100 000 000 сум\n"
                       "Страховая премия: 1 200 000 сум\n"
                       "Срок страхования: с 01.06.2026 по 31.05.2027\n")


def test_kind_blank_needs_second_sign():
    """Второй признак бланка: номер в строке заголовка ИЛИ заполненные реквизиты."""
    cases = [("заголовок с номером, реквизитов нет",
              "СТРАХОВОЙ ПОЛИС № AB 0001234\nТашкент\n", D.KIND_CONTRACT),
             ("заголовок без номера, но реквизиты заполнены",
              "СТРАХОВОЙ ПОЛИС\n" + BLANK_REQUISITES_RU, D.KIND_CONTRACT),
             ("заголовок uz без номера, реквизиты заполнены",
              "SUG'URTA POLISI\nSug'urta summasi: 100 000 000 so'm\n"
              "Sug'urta mukofoti: 1 200 000 so'm\n", D.KIND_CONTRACT),
             ("ни номера, ни реквизитов",
              "СТРАХОВОЙ ПОЛИС\nТашкент\n", ingest.KIND_OTHER)]
    for name, text, want in cases:
        out = ingest.detect_kind(text, [], None)
        assert out["kind"] == want, (name, out)
        print("второй признак бланка:", name, "→", out["kind"], out["confidence"])


def test_kind_blank_title_line_boundary():
    """
    Реальная граница для заголовка бланка: он должен стоять в первых 3 НЕПУСТЫХ строках
    (ingest.BLANK_HEAD_LINES), а не в первых 40. С 4-й непустой строки вид теряется,
    если в шапке нет сильного маркера договора («настоящий договор», «именуемый
    в дальнейшем»): с ним вид держится до 40-й строки (ingest.HEAD_LINES).
    """
    pad = "Организация ООО «Пример», расчётный счёт в банке, город Ташкент.\n"
    title = "СТРАХОВОЙ ПОЛИС № AB 0001234\n"
    strong = "Настоящий договор заключили стороны, именуемый в дальнейшем Страховщик\n"
    for n, want in ((0, D.KIND_CONTRACT), (1, D.KIND_CONTRACT), (2, D.KIND_CONTRACT),
                    (3, ingest.KIND_OTHER), (20, ingest.KIND_OTHER)):
        out = ingest.detect_kind(pad * n + title + BLANK_REQUISITES_RU, [], None)
        assert out["kind"] == want, ("слабые реквизиты, отступ %d" % n, out)
    for n, want in ((3, D.KIND_CONTRACT), (20, D.KIND_CONTRACT), (40, ingest.KIND_OTHER)):
        out = ingest.detect_kind(pad * n + title + strong + BLANK_REQUISITES_RU, [], None)
        assert out["kind"] == want, ("сильный маркер, отступ %d" % n, out)
    print("граница заголовка: 3 непустые строки; с сильным маркером — 40 строк шапки")


# Класс, а не один пример: СТРОКА ПРАЙСА, СМЕТЫ ИЛИ РЕЕСТРА. Раньше служебным считалось
# любое слово из трёх букв, а свёртка делает из кириллицы латиницу: «сум» → sum, «шт» → sht.
# Поэтому строка сметы «Страховой полис № 5 | 5 000 сум» проходила как заголовок бланка
# и закрывала пункт чек-листа «Документ о праве на объект» (docparse.CHECKLIST_MATCH).
# Единица измерения и валюта рядом с номером — признак сметы, а не бланка.
PRICE_LINES_TEXT = [
    ("смета ru", "Страховой полис № 5  5 000 сум"),
    ("реестр ru", "Договор страхования № 15  2 шт"),
    ("прайс en", "Insurance policy No. 5 pcs"),
    ("прайс en валюта", "Insurance policy No. 5 - 10 USD"),
    ("смета uz-latn", "Sug'urta polisi № 5 - 5 000 so'm, 2 dona"),
    ("смета uz-cyrl", "Суғурта полиси № 5 - 5 000 сўм, 2 дона"),
    # контроль: длинное слово правило ловило и раньше — эти примеры не должны измениться
    ("контроль штука", "Страховой полис № 5 штука"),
    ("контроль items", "Insurance policy No. 5 items"),
    ("контроль per unit", "Insurance policy No. 5 per unit"),
]
PRICE_LINES_XLSX = [
    ("лист Смета", "Смета", ["Страховой полис № 5", "5 000 сум"]),
    ("лист Реестр", "Реестр", ["Договор страхования № 15", "2 шт"]),
    ("лист Смета серия", "Смета", ["Страховой полис серия AB", "10 usd"]),
]


def test_kind_price_line_is_not_blank_title():
    """Единица измерения и валюта в строке — это смета или прайс, а не бланк."""
    for name, text in PRICE_LINES_TEXT:
        out = ingest.detect_kind(text, [], None)
        assert out["kind"] == ingest.KIND_OTHER, (name, out)
        print("строка прайса:", name, "→", out["kind"], out["confidence"])
    for name, sheet, cells in PRICE_LINES_XLSX:
        tables = [{"name": sheet, "rows": [cells]}]
        out = ingest.detect_kind("", tables, "smeta.xlsx")
        assert out["kind"] == ingest.KIND_OTHER, (name, out)
        print("строка XLSX:", name, "→", out["kind"], out["confidence"])
    # сами слова: «AB» — серия бланка, «сум» и «шт» — единицы, хотя после свёртки
    # все три выглядят коротким латинским словом
    assert ingest._blank_title_word("ab", "СТРАХОВОЙ ПОЛИС № AB 1")
    assert not ingest._blank_title_word("sum", "Страховой полис № 5 5 000 сум")
    assert not ingest._blank_title_word("sht", "Договор страхования № 15 2 шт")
    assert not ingest._blank_title_word("akt", "АКТ страховой полис")


# Класс, а не один пример: УТОЧНЕНИЕ ВИДА СТРАХОВАНИЯ в строке-заголовке. Реальный бланк
# печатают не двумя словами: «СТРАХОВОЙ ПОЛИС (КАСКО)», «ПОЛИС ДОБРОВОЛЬНОГО СТРАХОВАНИЯ
# ИМУЩЕСТВА», «INSURANCE POLICY (PROPERTY)». Полис — обязательный документ (Закон
# о страховой деятельности ст. 33-1, Положение 3571), и такой бланк уходил в «прочее».
BLANK_REQ_EN = ("Policyholder: Example LLC\nSum insured: 146 100 000 UZS\n"
                "Insurance premium: 3 900 000 UZS\n")
BLANK_REQ_UZ = "Sug'urta summasi: 146 100 000 so'm\nSug'urta mukofoti: 3 900 000 so'm\n"
BLANK_REQ_UZC = "Суғурта суммаси: 146 100 000 сўм\nСуғурта мукофоти: 3 900 000 сўм\n"

BLANK_TITLES_WITH_TYPE = [
    ("каско ru", "СТРАХОВОЙ ПОЛИС (КАСКО) № AB 0001234\n" + BLANK_REQUISITES_RU),
    ("полис имущества ru", "ПОЛИС ДОБРОВОЛЬНОГО СТРАХОВАНИЯ ИМУЩЕСТВА № AB 0001234\n"
                           + BLANK_REQUISITES_RU),
    ("полис с уточнением в строке", "СТРАХОВОЙ ПОЛИС добровольного страхования "
                                    "имущества № AB 1\n"),
    ("договор имущества ru", "ДОГОВОР СТРАХОВАНИЯ ИМУЩЕСТВА № 15\n" + BLANK_REQUISITES_RU),
    ("property en", "INSURANCE POLICY (PROPERTY) No. AB 1\n" + BLANK_REQ_EN),
    ("contract property en", "INSURANCE CONTRACT (PROPERTY) No. 12\n" + BLANK_REQ_EN),
    ("mulk uz-latn", "SUG'URTA POLISI (MULK) № AB 1\n" + BLANK_REQ_UZ),
    ("ixtiyoriy uz-latn", "MULKNI IXTIYORIY SUG'URTA QILISH POLISI № AB 1\n" + BLANK_REQ_UZ),
    ("мулк uz-cyrl", "СУҒУРТА ПОЛИСИ (МУЛК) № AB 1\n" + BLANK_REQ_UZC),
    ("образец ru", "ОБРАЗЕЦ СТРАХОВОГО ПОЛИСА № AB 1\n" + BLANK_REQUISITES_RU),
    ("копия ru", "КОПИЯ: СТРАХОВОЙ ПОЛИС № AB 1\n" + BLANK_REQUISITES_RU),
    ("образец договора ru", "ОБРАЗЕЦ ДОГОВОРА СТРАХОВАНИЯ № 15\n" + BLANK_REQUISITES_RU),
]


def test_kind_blank_title_with_insurance_type():
    """
    В строке-заголовке допустимо уточнение вида страхования и пометка копии: название
    бланка, вид, серия и номер. Фраза связной речи так не выглядит — её проверяем рядом.
    """
    for name, text in BLANK_TITLES_WITH_TYPE:
        out = ingest.detect_kind(text, [], None)
        assert out["kind"] == D.KIND_CONTRACT and out["confidence"] >= 0.55, (name, out)
        print("бланк с уточнением вида:", name, "→", out["kind"], out["confidence"])
    # уточнение вида не спасает связную речь и строку прайса
    for name, text in (("просьба с видом",
                        "Прошу выдать страховой полис добровольного страхования "
                        "имущества № 5.\n" + BLANK_REQUISITES_RU),
                       ("прайс с видом",
                        "ПРАЙС-ЛИСТ\nСтраховой полис (имущество) — 5 000 сум\n")):
        out = ingest.detect_kind(text, [], None)
        assert out["kind"] == ingest.KIND_OTHER, (name, out)
        print("уточнение вида не спасает:", name, "→", out["kind"], out["confidence"])


# Связные фразы чужих документов на четырёх языках: полис или договор в них упомянут,
# но документ не бланк. Класс проверяем целиком, а не тремя примерами.
FOREIGN_PHRASES = [
    ("ru служебка", "СЛУЖЕБНАЯ ЗАПИСКА\nПрошу выдать страховой полис № 5 на склад филиала.\n"
                    "Начальник отдела\n"),
    ("ru претензия", "ПРЕТЕНЗИЯ\nВы не исполнили договор страхования № 15 и не выплатили "
                     "возмещение.\nТребуем ответа в срок.\n"),
    ("ru смета", "СМЕТА РАСХОДОВ\nСтраховой полис № 5 — 5 000 сум за бланк\n"
                 "Итого: 5 000 сум\n"),
    ("ru реестр", "РЕЕСТР ДОКУМЕНТОВ\nДоговор страхования № 15 — 2 шт\nПолис № 7 — 1 шт\n"),
    ("ru оглавление", "СОДЕРЖАНИЕ\n1. Страховой полис ...... 5\n"
                      "2. Договор страхования ...... 20\n"),
    ("ru акт сверки", "АКТ СВЕРКИ\nОснование: страховой полис № AB 0001234 от 01.06.2026\n"
                      "Сальдо ноль.\n"),
    ("ru прайс", "ПРАЙС-ЛИСТ\nСтраховой полис (бланк) — 5 000 сум за штуку\n"),
    ("ru заявление", "ЗАЯВЛЕНИЕ\nПрошу заключить договор страхования имущества склада.\n"
                     "Подпись\n"),
    ("en memo", "MEMORANDUM\nPlease issue an insurance policy No. 5 for the branch.\n"),
    ("en cover letter", "COVER LETTER\nWe are sending you the insurance contract No. 12 "
                        "for signing.\n"),
    ("en invoice", "INVOICE No. 12\nInsurance policy (property) - 5 000 UZS per unit\n"),
    ("uz-latn ariza", "ARIZA\nIltimos, ombor uchun sug'urta polisi № 5 ni bering.\nImzo\n"),
    ("uz-latn xat", "XIZMAT XATI\nFilial uchun sug'urta shartnomasi № 15 tuzishni "
                    "so'raymiz.\n"),
    ("uz-cyrl ариза", "АРИЗА\nИлтимос, омбор учун суғурта полиси № 5 ни беринг.\nИмзо\n"),
    ("uz-cyrl хат", "ХИЗМАТ ХАТИ\nФилиал учун суғурта полиси рақами 5 ни юборинг.\n"),
]


def test_kind_foreign_phrases_stay_other():
    """Связная фраза с упоминанием полиса — чужой документ на любом из четырёх языков."""
    for name, text in FOREIGN_PHRASES:
        out = ingest.detect_kind(text, [], name + ".pdf")
        assert out["kind"] == ingest.KIND_OTHER, (name, out)
        print("чужая фраза:", name, "→", out["kind"], out["confidence"])


def test_kind_source_texts_stay_other():
    """
    Учебники, нормативные акты и рабочие тексты из docs/source остаются «прочим».
    Перебираем ВСЕ файлы папки (glob), а не белый список имён: раньше «Тарифная политика.txt»
    в список не входила и незаметно опознавалась техпаспортом с уверенностью 1.000.
    """
    root = Path(__file__).resolve().parent.parent
    src = root / "docs" / "source"
    files_ = sorted(src.glob("*.txt"))
    if len(files_) < 10:
        # чистая машина: docs/source в .gitignore (тексты внутренних документов); в репозитории есть открытые тексты
        # законов library/01_Законодательство/**/*.txt — они тоже «прочее» и проверяются вместо docs/source
        files_ = sorted((root / "library" / "01_Законодательство").rglob("*.txt"))
        print("docs/source пуста (чистая машина) — проверяются тексты законов library/01_Законодательство:",
              len(files_))
    assert len(files_) >= 10, "текстов в docs/source и library/01_Законодательство слишком мало: %d" % len(files_)
    checked, skipped = 0, []
    for f in files_:
        out = ingest.detect_kind(f.read_text(encoding="utf-8", errors="ignore"), [], f.name)
        if f.name in SOURCE_EXCEPTIONS:
            skipped.append("%s (%s): %s" % (f.name, SOURCE_EXCEPTIONS[f.name], out["kind"]))
            continue
        assert out["kind"] == ingest.KIND_OTHER, (f.name, out)
        checked += 1
    print("файлов в docs/source:", len(files_), "| остались «прочим»:", checked,
          "| известные исключения:", skipped or "нет")


# --------------------------------------------------------------------------- #
# 3. Чтение файлов
# --------------------------------------------------------------------------- #

def test_read_docx_table():
    text, tables = ingest.read_docx(files()["passport_docx"])
    assert tables and len(tables[0]["rows"]) == len(PASSPORT_UZ_ROWS), tables
    assert tables[0]["rows"][0][0].startswith("Markasi")
    assert "TEXNIK PASPORT" in text
    print("DOCX: строк в таблице", len(tables[0]["rows"]))


def test_read_xlsx_sheet():
    text, tables = ingest.read_xlsx(files()["staff_xlsx"])
    assert tables and tables[0]["name"].startswith("Staffing")
    assert any("Headcount" in " ".join(r) for r in tables[0]["rows"])
    assert "Payroll fund" in text
    print("XLSX: листов", len(tables), "строк", len(tables[0]["rows"]))


def test_read_pdf_text():
    text, _ = ingest.read_pdf(files()["cadastre_pdf"])
    assert "Кадастровый номер" in text
    print("PDF: символов", len(text))


def test_old_doc_not_supported():
    out = ingest.read_file(files()["old_doc"])
    assert out["status"] == ingest.ST_UNSUPPORTED
    assert ".doc" in out["note"] and "docx" in out["note"].lower()
    print("старый формат:", out["note"])


def test_scan_needs_ocr():
    out = ingest.read_file(files()["scan_pdf"], "application/pdf")
    assert out["status"] == ingest.ST_OCR and not out["text"].strip()
    print("скан:", out["note"][:60], "…")


def test_sniff_formats():
    assert ingest.sniff_format(files()["passport_docx"]) == "docx"
    assert ingest.sniff_format(files()["staff_xlsx"]) == "xlsx"
    assert ingest.sniff_format(files()["cadastre_pdf"]) == "pdf"
    assert ingest.sniff_format(files()["old_doc"]) == "doc"
    print("форматы определяются по содержимому файла")


def test_photos_sniff_mime():
    """Приём файлов: DOCX и XLSX различаются по содержимому архива, .doc отклоняется."""
    assert photos.sniff_mime(files()["passport_docx"].read_bytes()) == photos.DOCX_MIME
    assert photos.sniff_mime(files()["staff_xlsx"].read_bytes()) == photos.XLSX_MIME
    assert photos.sniff_mime(files()["old_doc"].read_bytes()) == photos.OLD_OFFICE_MIME
    print("сигнатуры DOCX/XLSX/.doc распознаются")


# --------------------------------------------------------------------------- #
# 4. Поля и персональные данные
# --------------------------------------------------------------------------- #

def test_fields_passport_uz():
    text, tables = ingest.read_docx(files()["passport_docx"])
    fields = ingest.extract_fields(text, tables, D.KIND_PASSPORT, "uz-latn")
    got = {f["ключ"]: f["значение"] for f in fields}
    assert got.get("year") == "2019", got
    assert got.get("brand", "").startswith("Chevrolet"), got
    assert got.get("seats") == "5", got
    assert all(f["метод"] == ingest.METHOD_REGEX for f in fields)
    print("поля техпаспорта (uz-latn):", got)


def test_fields_cadastre_ru():
    fields = ingest.extract_fields(CADASTRE_RU_TEXT, [], D.KIND_CADASTRE, "ru")
    got = {f["ключ"]: f["значение"] for f in fields}
    assert got.get("area_m2") == "74.5" and got.get("rooms") == "3"
    assert got.get("cadastre_no") == "10:09:05:01:02:1234"
    assert got.get("region") == "город Ташкент"
    print("поля кадастра (ru):", got)


def test_fields_staff_en():
    """Штатное расписание на английском: численность и фонд оплаты труда."""
    text, tables = ingest.read_xlsx(files()["staff_xlsx"])
    fields = ingest.extract_fields(text, tables, D.KIND_STAFF, "en")
    got = {f["ключ"]: f["значение"] for f in fields}
    assert got.get("headcount") == "15", got
    assert got.get("payroll_fund") == "118000000", got
    print("поля штатного расписания (en):", got)


def test_llm_prompts_by_language():
    """Промпт выбирается по языку документа: uz-latn и uz-cyrl → uz.txt, mixed → ru.txt."""
    from app import llm
    assert llm.prompt_file("uz-latn").name == "uz.txt"
    assert llm.prompt_file("uz-cyrl").name == "uz.txt"
    assert llm.prompt_file("en").name == "en.txt"
    assert llm.prompt_file("mixed").name == "ru.txt" and llm.prompt_file(None).name == "ru.txt"
    for lang in ("ru", "uz-latn", "en"):
        body = llm.load_prompt(lang)
        assert body and "{text}" in body and "{keys}" in body
    if not llm.enabled():
        out = llm.extract_document("любой текст", D.KIND_CONTRACT, "ru")
        assert out["ok"] is False and out["fields"] == {} and out["reason"]
        print("без ключа ИИ:", out["reason"])


def test_no_personal_data_in_fields():
    """ЗРУ-547 (PD-01): ФИО, адрес физлица, паспорт и ПИНФЛ не извлекаются ни в поля, ни в факты."""
    text, tables = ingest.read_docx(files()["passport_docx"])
    fields = ingest.extract_fields(text, tables, D.KIND_PASSPORT, "uz-latn")
    facts = ingest.extract_facts(text, D.KIND_PASSPORT)
    dump = json.dumps([fields, facts], ensure_ascii=False)
    for secret in ("Karimov", "Akmalovich", "Amir Temur", "AA 1234567"):
        assert secret not in dump, "в поля попали персональные данные: " + secret
    # сам факт наличия ПД в документе отмечаем — но без значений
    assert any(f["ключ"] == "has_personal_data" for f in facts)
    print("персональные данные не извлечены, отметка has_personal_data поставлена")


def test_facts_cadastre():
    facts = {f["ключ"]: f["значение"] for f in ingest.extract_facts(CADASTRE_RU_TEXT,
                                                                   D.KIND_CADASTRE)}
    assert facts.get("has_mortgage") is True and facts.get("is_register_extract") is True
    print("факты кадастра:", sorted(facts))


# --------------------------------------------------------------------------- #
# 5. Весь конвейер и сводки
# --------------------------------------------------------------------------- #

def _request(con) -> int:
    cur = con.execute("INSERT INTO requests (external_no, branch, product_code, policyholder,"
                      " created_at, status) VALUES (?,?,?,?,?,?)",
                      ("тест-ingest", "тест", None, "тест", db.now(), "новый"))
    MADE_REQUESTS.append(cur.lastrowid)
    return cur.lastrowid


def test_pipeline():
    with db.tx() as con:
        rid = _request(con)
        saved = []
        for key, name in (("passport_docx", "texnik_pasport.docx"),
                          ("cadastre_pdf", "kadastr.pdf"),
                          ("staff_xlsx", "staffing.xlsx"),
                          ("scan_pdf", "scan.pdf")):
            blob = files()[key].read_bytes()
            rec = photos._store(con, rid, blob, name, "тест", None, D.KIND_OTHER)
            saved.append(rec["id"])

        out = ingest.ingest_request(con, rid, who="тест", use_llm=False)
        assert out["разобрано"] == 4, out
        kinds = {d["вид_документа"] for d in out["документы"]}
        assert D.KIND_PASSPORT in kinds and D.KIND_CADASTRE in kinds and D.KIND_STAFF in kinds
        scan = [d for d in out["документы"] if d["формат"] == "pdf"
                and d["статус"] == ingest.ST_OCR]
        assert scan and scan[0]["поля"] == [], "скан должен уйти в «нужно распознавание»"
        # без ключа ИИ работает только regex — и это видно в ответе
        assert all(d["метод"] == ingest.METHOD_REGEX for d in out["документы"])
        print("конвейер:", {d["вид_документа"]: d["статус"] for d in out["документы"]})

        # повторный вызов ничего не разбирает заново (идемпотентность)
        again = ingest.ingest_request(con, rid, who="тест", use_llm=False)
        assert again["разобрано"] == 0 and again["пропущено"] == 4
        # в базе по одной строке на файл
        rows = db.rows(con, "SELECT photo_id FROM document_extracts WHERE request_id=?", rid)
        assert len(rows) == 4 and len({r["photo_id"] for r in rows}) == 4

        # в журнал не попали ни текст документа, ни имя файла клиента
        audit = db.rows(con, "SELECT detail FROM audit WHERE entity=? AND action=?",
                        "request:%d" % rid, "документ разобран")
        assert audit and all("Karimov" not in (a["detail"] or "") for a in audit)
        assert all("texnik_pasport.docx" not in (a["detail"] or "") for a in audit)

    # 6. Сводки для смежных команд
    merged = ingest.fields_for_request(rid)
    assert merged["всего_полей"] > 5, merged
    assert all(v["документ"]["id"] for v in merged["поля"].values())
    print("сведённые поля:", sorted(merged["поля"])[:8], "…")

    summary = ingest.summary_for_request(rid)
    assert len(summary["документы"]) == 4
    assert any(w["вид"] == "нужно распознавание" for w in summary["пометки"])
    assert isinstance(summary["поля"], list) and isinstance(summary["факты"], list)
    dump = json.dumps(summary, ensure_ascii=False)
    assert "Karimov" not in dump and "AA 1234567" not in dump
    print("сводка: документов", len(summary["документы"]), "полей", len(summary["поля"]),
          "пометок", len(summary["пометки"]))


def test_text_in_db_is_masked():
    """Текст документа в базе хранится с замаскированными персональными данными."""
    with db.tx() as con:
        rows = db.rows(con, "SELECT text FROM document_extracts WHERE request_id=?",
                       MADE_REQUESTS[0])
    joined = " ".join(r["text"] or "" for r in rows)
    assert "Karimov Aziz" not in joined and "AA 1234567" not in joined
    assert "[ПАСПОРТ]" in joined or "[ФИО]" in joined
    print("текст в базе замаскирован")


def test_tables_in_db_are_masked():
    """
    ЗРУ-547: в document_extracts.tables не должно быть ни ФИО, ни адреса, ни паспорта,
    ни ПИНФЛ, ни телефона — таблица такой же текст документа, как и всё остальное.
    """
    with db.tx() as con:
        rid = _request(con)
        blob = files()["passport_pd_docx"].read_bytes()
        rec = photos._store(con, rid, blob, "tehpasport_pd.docx", "тест", None, D.KIND_OTHER)
        ingest.ingest_file(con, rec["id"], who="тест", use_llm=False)
        row = db.rows(con, "SELECT text, tables, fields, facts FROM document_extracts"
                           " WHERE photo_id=?", rec["id"])[0]

    assert row["tables"] and row["tables"] != "[]", "таблица документа не сохранилась"
    for column in ("tables", "text", "fields", "facts"):
        got = row[column] or ""
        for secret in PD_SECRETS:
            assert secret not in got, "персональные данные в document_extracts.%s: %s" % (
                column, secret)
    assert "[ФИО]" in row["tables"] and "[ПАСПОРТ]" in row["tables"],         "в таблице должны стоять метки вместо персональных данных"
    assert "[ПИНФЛ]" in row["tables"] and "[ТЕЛЕФОН]" in row["tables"]
    # маскировка намеренно избыточна (два слова с заглавной = возможное ФИО), поэтому
    # марка и модель берутся не из текста таблицы, а из разобранных полей
    assert "2021" in row["tables"], "нетекстовые данные объекта должны остаться"
    fields = {f["ключ"]: f["значение"] for f in json.loads(row["fields"])}
    assert fields.get("brand") and fields.get("year") == "2021", fields
    print("таблицы в базе замаскированы, поля объекта разобраны:", sorted(fields))


def test_fold_keeps_english():
    """Свёртка языкозависима: узбекские правила не калечат английские слова."""
    assert D.fold("year of manufacture") == "year of manufacture"
    assert D.fold("Yearly Yield") == "yearly yield"
    assert D.fold("Owner") == "owner"
    assert D.fold("Total costs") == "total costs"
    # узбекские и русские написания по-прежнему сводятся к одному
    assert D.fold("Sug‘urta polisi") == D.fold("Суғурта полиси") == "sugurta polisi"
    assert D.fold("Texnik pasport") == D.fold("Техник паспорт") == "tehnik pasport"
    print("свёртка: английский не портится, узбекские написания сходятся")


def test_date_is_full_not_year():
    """Полная дата важнее одиночного года: «01.06.2026» — это дата оценки, а не 2026-й."""
    fields = {f["ключ"]: f["значение"] for f in
              ingest.extract_fields(VALUATION_RU_TEXT, [], D.KIND_VALUATION, "ru")}
    assert fields.get("valuation_date") == "01.06.2026", fields
    print("дата оценки:", fields.get("valuation_date"))


def test_person_address_and_name_not_in_fields():
    """
    ЗРУ-547 (PD-01): ни ФИО, ни адрес физлица не должны попасть в document_extracts.fields.
    Адрес ОБЪЕКТА при этом обязан сохраниться — это характеристика имущества (ЗРУ-803, ст. 23).
    """
    with db.tx() as con:
        rid = _request(con)
        blob = files()["cadastre_pd_xlsx"].read_bytes()
        rec = photos._store(con, rid, blob, "kadastr_pd.xlsx", "тест", None, D.KIND_CADASTRE)
        ingest.ingest_file(con, rec["id"], who="тест", use_llm=False)
        row = db.rows(con, "SELECT fields, facts, text, tables FROM document_extracts"
                           " WHERE photo_id=?", rec["id"])[0]

    fields = {f["ключ"]: f["значение"] for f in json.loads(row["fields"])}
    leaked = ("Мирзаумаров", "Жасур", "Алишерович", "Шота Руставели", "AA 1234567",
              "31234567890123")
    for column in ("fields", "facts"):
        for secret in leaked:
            assert secret not in (row[column] or ""), (
                "персональные данные в document_extracts.%s: %s (поля: %s)" % (
                    column, secret, fields))
    # адрес объекта не пострадал
    assert fields.get("address") == OBJECT_ADDRESS, fields
    assert fields.get("region") == "город Ташкент" and fields.get("area_m2") == "74.5", fields
    assert fields.get("cadastre_no") == "10:09:05:01:0123:0001", fields
    print("адрес объекта сохранён, персональные данные в поля не попали:", fields.get("address"))


def test_object_word_inside_person_address():
    """
    Признак объекта ищется только в ПОДПИСИ. Слова «здание», «квартира», «помещения», bino
    в самом адресе проживания не должны отменять персональную проверку (ЗРУ-547, PD-01).
    """
    person = [
        "Адрес проживания: город Самарканд, улица Регистан, здание 3, квартира 12",
        "Домашний адрес: Ташкент, Мирабадский район, помещения 4",
        "Yashash joyi: Samarqand, bino 3",
        "Адрес проживания: город Самарканд, улица Беруни, дом 44",
        "Домашний адрес | Самарканд, бино 3",
    ]
    obj = [
        "Местонахождение объекта: г. Ташкент, улица Нукус, дом 7",
        "Адрес объекта: Самарканд, здание 3",
        "Obyekt manzili: Samarqand, bino 3",
        "Joylashgan joyi: Toshkent, kvartira 12",
        "Object address: Tashkent, building 3",
        "Местонахождение объекта | г. Ташкент, улица Нукус, дом 7",
    ]
    for line in person:
        assert D.is_personal_label(line), "адрес человека принят за адрес объекта: " + line
    for line in obj:
        assert not D.is_personal_label(line), "адрес объекта принят за адрес человека: " + line

    # разбор кадастра: домашний адрес со словом «квартира» стоит первым и не должен
    # попасть в fields.address вместо адреса объекта
    rows = ([["Адрес проживания", "г. Самарканд, улица Регистан, здание 3, квартира 12"]]
            + CADASTRE_PD_ROWS)
    text = "\n".join(" | ".join(c for c in r if c) for r in rows)
    fields = {f["ключ"]: f["значение"] for f in
              ingest.extract_fields(text, [{"rows": rows}], D.KIND_CADASTRE, "ru")}
    assert fields.get("address") == OBJECT_ADDRESS, fields
    assert "Регистан" not in (fields.get("address") or ""), fields
    print("подпись решает: адрес объекта сохранён, домашний — нет:", fields.get("address"))


def test_person_label_without_separator():
    """
    Извлечение текста из PDF часто отдаёт подпись и значение через пробелы, без «:» и « | ».
    В такой строке признак объекта ищем только в первых словах — иначе «здание» и «помещения»
    из самого адреса проживания отменяли персональную проверку (ЗРУ-547, PD-01).
    """
    person = ["Адрес проживания Самарканд улица Беруни здание 44",
              "Домашний адрес Ташкент помещения 4",
              "Yashash joyi Samarqand bino 3",
              "Адрес регистрации Ташкент квартира 12",
              "Residential address Tashkent building 3"]
    obj = ["Местонахождение объекта Самарканд здание 3",
           "Адрес объекта Ташкент улица Нукус дом 7",
           "Obyekt manzili Samarqand bino 3",
           "Object address Tashkent building 3",
           "Location of the property"]
    for line in person:
        assert D.is_personal_label(line), "адрес человека принят за адрес объекта: " + line
    for line in obj:
        assert not D.is_personal_label(line), "адрес объекта принят за адрес человека: " + line
    # через полный разбор: адрес проживания в поля не попадает, адрес объекта остаётся
    text = ("КАДАСТРОВЫЙ ПАСПОРТ ОБЪЕКТА НЕДВИЖИМОСТИ\n"
            "Адрес проживания Самарканд улица Беруни здание 44\n"
            "Местонахождение объекта Ташкент улица Нукус дом 7\n")
    fields = {f["ключ"]: f["значение"] for f in
              ingest.extract_fields(text, [], D.KIND_CADASTRE, "ru")}
    assert "Беруни" not in (fields.get("address") or ""), fields
    assert "Нукус" in (fields.get("address") or ""), fields
    # остаток подписи («объекта») в значение не переходит
    assert (fields.get("address") or "").startswith("Ташкент"), fields
    print("подпись без разделителя: адрес человека отброшен, адрес объекта взят:",
          fields.get("address"))


def test_region_takes_nearest_and_longest():
    """Регион — по первому и самому длинному совпадению, а не по порядку словаря."""
    cases = [("город Самарканд, улица Навои, дом 12", "Самаркандская область"),
             ("Самаркандская область, Ургутский район", "Самаркандская область"),
             ("город Ташкент, Мирзо-Улугбекский район", "город Ташкент"),
             ("Andijon viloyati, Navoiy ko'chasi, 5-uy", "Андижанская область"),
             ("Навоийская область, город Зарафшан", "Навоийская область"),
             ("Toshkent shahri, Amir Temur ko'chasi", "город Ташкент")]
    for raw, want in cases:
        got = D.cast("region", raw)
        assert got == want, (raw, got, want)
    print("регион берётся по ближайшему совпадению:", len(cases), "проверки")


def test_label_line_without_value_takes_next_line():
    """
    Вся строка — подпись, значение на следующей строке: «Location of the property /
    Tashkent…». Раньше из хвоста подписи резалось не больше двух слов, и в адрес попадало
    бессмысленное «property» с пометкой «уверенность высокая».
    Класс ошибок — подпись из нескольких служебных слов на четырёх написаниях.
    """
    cases = [("Location of the property", "Tashkent, Chilonzor district, building 5"),
             ("Address of the insured property", "Tashkent, Chilonzor district, building 5"),
             ("Местонахождение объекта страхования", "город Ташкент, улица Нукус, дом 7"),
             ("Sug'urta obyektining manzili", "Toshkent shahri, Nukus ko'chasi, 7-uy")]
    for label, value in cases:
        out = D.parse_text("%s\n%s\nTotal area: 120 sq.m\n" % (label, value), D.KIND_CADASTRE)
        got = out["для_оценки"].get("адрес")
        assert got == value, (label, got)
        addr = [f for f in out["поля"] if f["поле"] == "address"][0]
        assert addr["уверенность"] == "средняя", (label, addr)
    assert D.cut_tail(" of the property") == "", D.cut_tail(" of the property")
    print("подпись без значения: адрес берётся со следующей строки,", len(cases), "проверки")


def test_mask_names_does_not_cross_lines():
    """Маска ФИО не перескакивает на следующую строку и не съедает её подпись."""
    from app import llm
    text = ("Страхователь: Мирзаумаров Жасур\n"
            "Домашний адрес: город Самарканд\n"
            "Водитель: Петров Пётр\n"
            "Адрес объекта страхования: Самаркандская область\n"
            "Director: John Smith\n"
            "Object address: Tashkent")
    out = llm.mask_pd(text)
    for label in ("Домашний адрес", "Адрес объекта страхования", "Object address"):
        assert label in out, "подпись уничтожена маской ФИО: %s\n%s" % (label, out)
    for name in ("Мирзаумаров", "Жасур", "Петров", "Пётр", "John", "Smith"):
        assert name not in out, "ФИО не замаскировано: %s\n%s" % (name, out)
    assert len(out.splitlines()) == len(text.splitlines()), out
    # написание «Фамилия И. О.» по-прежнему закрывается
    assert llm.mask_pd("Страхователь: Мирзаумаров Ж.А.") .endswith("[ФИО]")
    print("маска ФИО не переходит через перевод строки")


def test_brand_and_model_not_duplicated():
    """Подпись «Марка и модель» — одна на два поля: модель не должна повторять марку."""
    rows = [["Markasi va modeli", "Chevrolet Malibu"], ["Ishlab chiqarilgan yili", "2019"]]
    text = "Markasi va modeli | Chevrolet Malibu" + chr(10) + "Ishlab chiqarilgan yili | 2019"
    f = {x["ключ"]: x["значение"] for x in
         ingest.extract_fields(text, [{"rows": rows}], D.KIND_PASSPORT, "uz-latn")}
    assert f.get("brand") == "Chevrolet" and f.get("model") == "Malibu", f
    # раздельные подписи не трогаем
    rows2 = [["Марка", "Chevrolet"], ["Модель", "Malibu"]]
    text2 = "Марка | Chevrolet" + chr(10) + "Модель | Malibu"
    f2 = {x["ключ"]: x["значение"] for x in
          ingest.extract_fields(text2, [{"rows": rows2}], D.KIND_PASSPORT, "ru")}
    assert f2.get("brand") == "Chevrolet" and f2.get("model") == "Malibu", f2
    print("марка и модель разделены:", f.get("brand"), "/", f.get("model"))


def test_compound_brand_not_broken():
    """Марка из двух слов («Land Rover») по первому слову не делится: марка была бы «Land»."""
    cases = [("Land Rover Discovery", ("Land Rover", "Discovery")),
             ("Land Rover Range Rover", ("Land Rover", "Range Rover")),
             ("Alfa Romeo Giulia", ("Alfa Romeo", "Giulia")),
             ("Great Wall Hover", ("Great Wall", "Hover")),
             ("Mercedes-Benz E200", ("Mercedes-Benz", "E200")),
             ("Chevrolet Damas", ("Chevrolet", "Damas")),
             ("Land Rover", ("Land Rover", ""))]
    for raw, want in cases:
        assert ingest._brand_split(raw) == want, (raw, ingest._brand_split(raw))
    # через полный разбор: одна подпись «Марка и модель» на два поля
    rows = [["Make and model", "Land Rover Discovery"]]
    f = {x["ключ"]: x["значение"] for x in ingest.extract_fields(
        "Make and model | Land Rover Discovery", [{"rows": rows}], D.KIND_PASSPORT, "en")}
    assert f.get("brand") == "Land Rover" and f.get("model") == "Discovery", f
    # модели в значении нет — пустое поле не выдумываем
    rows2 = [["Марка и модель", "Land Rover"]]
    f2 = {x["ключ"]: x["значение"] for x in ingest.extract_fields(
        "Марка и модель | Land Rover", [{"rows": rows2}], D.KIND_PASSPORT, "ru")}
    assert f2.get("brand") == "Land Rover" and "model" not in f2, f2
    print("составные марки не ломаются:", len(cases), "проверки")


def test_english_valuation_market_value():
    """
    Английский отчёт оценщика: подпись «market value» стоит в заголовке, значения там нет.
    Брать следующую строку нельзя — иначе страховой суммой станет год из даты (ГК ст. 936/938).
    """
    fields = {f["ключ"]: f["значение"] for f in
              ingest.extract_fields(VALUATION_EN_TEXT, [], D.KIND_VALUATION, "en")}
    assert fields.get("market_value") == "850 000 000", fields
    assert fields.get("valuation_date") == "01.06.2026", fields
    # русский вариант того же отчёта не изменился
    ru = {f["ключ"]: f["значение"] for f in
          ingest.extract_fields(VALUATION_RU_TEXT, [], D.KIND_VALUATION, "ru")}
    assert ru.get("market_value") == "850 000 000", ru
    # год из даты деньгами не считается даже напрямую
    assert ingest._apply_pattern("money", "01.06.2026") is None
    print("рыночная стоимость на английском:", fields.get("market_value"))


def test_calendar_dates_only():
    """Несуществующая дата — не дата: проверяем календарь, а не диапазон 1–31."""
    bad = [{"d": "31", "m": "02", "yyyy": "2026"}, {"d": "30", "m": "февраля", "yyyy": "2026"},
           {"d": "29", "m": "02", "yyyy": "2025"}, {"d": "31", "m": "04", "yyyy": "2026"},
           {"d": "45", "m": "13", "yyyy": "2026"}, {"d": "00", "m": "01", "yyyy": "2026"}]
    for g in bad:
        assert ingest._as_date(g) is None, g
    good = [({"d": "29", "m": "02", "yyyy": "2024"}, "29.02.2024"),
            ({"d": "01", "m": "06", "yyyy": "2026"}, "01.06.2026"),
            ({"d": "12", "m": "мая", "yyyy": "2025"}, "12.05.2025"),
            ({"d": "31", "m": "12", "yyyy": "2026"}, "31.12.2026")]
    for g, want in good:
        assert ingest._as_date(g) == want, (g, ingest._as_date(g))
    # через полный разбор: «31.02.2026» в тексте датой оценки не становится
    fields = {f["ключ"]: f["значение"] for f in ingest.extract_fields(
        "ОТЧЁТ ОБ ОЦЕНКЕ\nДата оценки: 31.02.2026\n", [], D.KIND_VALUATION, "ru")}
    assert fields.get("valuation_date") is None, fields
    print("несуществующие даты отброшены:", len(bad))


def test_personal_labels_cover_common_wordings():
    """Подписи человека узнаются в четырёх написаниях, подпись объекта — нет (иначе потеряем адрес)."""
    person = ["Адрес проживания", "Адрес регистрации", "Домашний адрес",
              "Проживает по адресу", "Страхователь", "Место жительства",
              "Ro‘yxatdan o‘tgan manzil", "Yashash manzili", "Doimiy yashash manzili",
              "Рўйхатдан ўтган манзил", "Яшаш манзили", "Суғурта қилдирувчи",
              "Residential address", "Registered address", "Home address", "Policyholder"]
    obj = ["Местонахождение объекта", "Адрес объекта", "Местонахождение", "Почтовый адрес",
           "Obyekt manzili", "Joylashgan joyi", "Объект манзили", "Object address",
           "Location of the property"]
    for line in person:
        assert D.is_personal_label(line), "не опознана подпись человека: " + line
    for line in obj:
        assert not D.is_personal_label(line), "подпись объекта принята за человека: " + line
    print("подписи человека и объекта различаются:", len(person), "и", len(obj))


def test_policy_series_survives_masking():
    """Серия и номер полиса — реквизит договора: маскировка их не съедает, хвост подписи отрезан."""
    fields = {f["ключ"]: f["значение"] for f in
              ingest.extract_fields(CONTRACT_EN_TEXT, [], D.KIND_CONTRACT, "en")}
    assert fields.get("policy_series_no") == "AB 0001234", fields
    masked = ingest._mask_field_value(fields["policy_series_no"], "policy_series_no")
    assert masked == "AB 0001234", masked
    # в любом другом поле такое написание по-прежнему считается паспортом
    assert ingest._mask_field_value("AA 1234567", "org_name") == "[ПАСПОРТ]"
    print("серия и номер полиса сохранены:", masked)


def test_date_all_six_writings():
    """Шесть написаний даты дают один формат ДД.ММ.ГГГГ — иначе срок договора не посчитать."""
    same = {"01.06.2026", "2026-06-01", "01/06/2026", "1 июня 2026 г.",
            "2026 yil 1 iyun", "2026 йил 1 июн"}
    for src in same:
        assert ingest._apply_pattern("date_dmy", src) == "01.06.2026", (src,
            ingest._apply_pattern("date_dmy", src))
    # одиночный год датой не считаем
    assert ingest._apply_pattern("date_dmy", "2019 год") is None
    assert ingest._as_date({"d": "01", "m": "06", "yyyy": "2026"}) == "01.06.2026"
    assert ingest._as_date({"d": "01", "m": "06", "y": "2026"}) == "01.06.2026"
    assert ingest._as_date({"y": "2019"}) is None
    print("все шесть написаний даты сведены к 01.06.2026")


def cleanup():
    db.close_pool()                     # соединения пула держат файл копии открытым
    shutil.rmtree(TMP, ignore_errors=True)
    print("временная база и файлы удалены (рабочая база не менялась)")


def main():
    order = ["test_language_uz_latn", "test_language_ru", "test_language_en",
             "test_language_uz_cyrl", "test_language_short_text_is_none",
             "test_kind_passport_uz", "test_kind_cadastre_ru", "test_kind_staff_en",
             "test_kind_unknown_is_other", "test_kind_blank_titles_are_recognised",
             "test_kind_head_marker_only_counts_in_head",
             "test_kind_foreign_document_is_not_contract", "test_kind_bilingual_blank",
             "test_kind_short_foreign_documents_are_not_contract",
             "test_kind_blank_number_marker_needs_title_line",
             "test_kind_blank_needs_second_sign", "test_kind_blank_title_line_boundary",
             "test_kind_price_line_is_not_blank_title",
             "test_kind_blank_title_with_insurance_type",
             "test_kind_foreign_phrases_stay_other",
             "test_kind_source_texts_stay_other",
             "test_read_docx_table", "test_read_xlsx_sheet", "test_read_pdf_text",
             "test_old_doc_not_supported", "test_scan_needs_ocr", "test_sniff_formats",
             "test_photos_sniff_mime",
             "test_fields_passport_uz", "test_fields_cadastre_ru", "test_fields_staff_en",
             "test_llm_prompts_by_language",
             "test_no_personal_data_in_fields", "test_facts_cadastre",
             "test_fold_keeps_english", "test_date_is_full_not_year",
             "test_date_all_six_writings", "test_personal_labels_cover_common_wordings",
             "test_policy_series_survives_masking",
             "test_person_address_and_name_not_in_fields",
             "test_object_word_inside_person_address",
             "test_person_label_without_separator", "test_region_takes_nearest_and_longest",
             "test_label_line_without_value_takes_next_line",
             "test_mask_names_does_not_cross_lines",
             "test_brand_and_model_not_duplicated", "test_compound_brand_not_broken",
             "test_english_valuation_market_value", "test_calendar_dates_only",
             "test_pipeline", "test_text_in_db_is_masked", "test_tables_in_db_are_masked"]
    try:
        for name in order:
            globals()[name]()
    finally:
        cleanup()
    print("\nвсе проверки приёма документов пройдены:", len(order))


if __name__ == "__main__":
    main()
