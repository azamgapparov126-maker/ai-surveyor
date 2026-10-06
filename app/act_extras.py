"""
Дополнения сюрвейерского акта (решение заказчика 29.09.2026): сценарии убытка PML/EML/MFL, разбор
документов с текстовым слоем, применение франшизы и рекомендации страхователю.

Второго калькулятора здесь нет — всё считают существующие модули, их вызывают отсюда:
  risk_analytics.apply_defaults / analyze — сценарии, лимит удержания, reducers (мероприятия таблицы
      preventive_measures с эффектом движка);
  franchise._losses / _pick / _cap / what_if / _alternatives / _measures_delta — размер франшизы,
      её эффект на премию, альтернативы и сложение скидок мероприятий (перемножением, не ниже минимума);
  ingest.read_file / detect_kind / extract_fields / extract_contract_terms, analysis_docs.contract_term /
      build_prefill, docparse (через ingest) — разбор договора, заявления, выгрузки, техпаспорта, кадастра;
  engine.rate_for — эффект экспертных мероприятий из docs/act_measures.json (коэффициент справочника).

Базовая ставка акта — из тарифной политики (act_engine.rate, base_source=product_rate). Старый движок
считает свою техническую премию, поэтому его цифры в акт НЕ переносятся: из него берётся только
отношение «премия с изменением / премия без изменения» (множитель), и этот множитель применяется
к ставке акта; ниже минимальной ставки продукта ставка не опускается.

Модуль без HTTP и без сети. Каждая функция возвращает коды и числа; слова — app/act_texts.py.
Всё экспертное — calibrated = 0.
"""
import io
import json
import posixpath
import re
import threading
import time
import zipfile
from datetime import date
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

from . import branch_request
from . import contract_read
from . import credit_report
from . import db
from . import engine
from . import franchise as frm
from . import risk_analytics as ra

CALIBRATED = 0
RULE_CLASSES = ("3", "8", "9")            # классы, для которых в risk_analytics есть своё правило сценариев;
                                          # остальным — простое правило шаблона класса (simple_scenarios)
MEASURES_FILE = db.ROOT / "docs" / "act_measures.json"
MAX_MEASURES = 8
FR_TYPES = ("unconditional", "conditional", "peril")
PROT_CODES = {"3": [o["code"] for o in ra.PROT_3], "8": [o["code"] for o in ra.PROT_8],
              "9": [o["code"] for o in ra.PROT_8]}
CONSTRUCTIONS = ("reinforced", "mixed", "wood")
ACTIVITIES = ("office", "warehouse", "food", "flammable")
# вид объекта акта → деятельность для сценариев класса 8/9 (только однозначные)
KIND_ACTIVITY = {"warehouse": "warehouse", "shop": "office", "office": "office", "dwelling": "office",
                 "hotel": "office"}
KIND_VEHICLE = {"truck": "truck", "electric_car": "ev", "car": "car", "trailer": "truck"}
# Деятельность на объекте по словам описания в документе (ru, uz кириллица и латиница, en) — 30.09.2026.
# Порядок — по пожарной опасности: горючие материалы сильнее пищевого производства, оно сильнее хранения.
# Совпадение — по началу слова (граница слова слева), чтобы «нон» (хлеб) не находилось в «анонс».
ACTIVITY_TEXT_WORDS = (
    ("flammable", ("горюч", "легковоспламен", "лакокрас", "нефт", "нефтепродукт", "азс", "заправочн",
                   "деревообработ", "химическ", "ёқилғи", "ёнилғи", "yoqilg", "yonilg", "neft", "kimyo",
                   "fuel", "petrol", "chemical", "paint", "woodwork", "flammable")),
    ("food", ("пищев", "хлеб", "хлебопек", "нон", "мясо", "мясн", "молоч", "кондитер", "мукомол", "макарон",
              "озиқ-овқат", "озиқ овқат", "гўшт", "сут маҳсулот", "сут махсулот", "қандолат", "кандолат",
              "тегирмон", "новвой", "нон-булка", "oziq-ovqat", "oziq ovqat", "non mahsulot", "non-bulka", "novvoy",
              "go'sht", "goʻsht", "sut mahsulot", "qandolat", "tegirmon", "food", "bakery", "bread", "meat", "dairy", "confection", "flour mill")),
    ("warehouse", ("склад", "хранени", "холодильн", "омбор", "сақлаш", "сақланадиган", "музлатгич", "совутгич",
                   "ombor", "saqlash", "muzlatgich", "sovutgich", "warehouse", "storage", "cold store",
                   "cold storage", "refrigerat")),
    ("office", ("офис", "административ", "торгов", "магазин", "савдо", "дўкон", "ofis", "savdo", "do'kon",
                "doʻkon", "office", "retail", "shop")),
)
# склад-холодильник: уточнение вида «склад» для подписи в акте (вид и деятельность — те же, warehouse)
COLD_STORE_WORDS = ("холодильн", "музлатгич", "совутгич", "muzlatgich", "sovutgich", "cold store", "cold storage",
                    "refrigerat")


def _fold_text(text: str) -> str:
    return " " + re.sub(r"\s+", " ", str(text or "").lower().replace("ё", "е").replace("ʻ", "'")
                        .replace("‘", "'").replace("’", "'").replace("`", "'")) + " "


def _has_word(folded: str, stem: str) -> bool:
    """Слово, начинающееся с stem (граница слова слева): «нон» есть в «нон махсулотлари», нет в «анонс»."""
    stem = stem.replace("ё", "е").replace("ʻ", "'")
    return re.search(r"(?<![\w'])" + re.escape(stem), folded) is not None


def activity_from_text(text: str) -> Optional[str]:
    """Деятельность на объекте по описанию в документе (словарь ACTIVITY_TEXT_WORDS) или None — не угадываем."""
    folded = _fold_text(text)
    if not folded.strip():
        return None
    for code, words in ACTIVITY_TEXT_WORDS:
        if any(_has_word(folded, w) for w in words):
            return code
    return None


def cold_store(text: str) -> bool:
    """Холодильник, склад-холодильник, музлатгич — по описанию объекта."""
    folded = _fold_text(text)
    return any(_has_word(folded, w) for w in COLD_STORE_WORDS)


# поля техпаспорта и кадастра, которые берутся и из договора или заявления (данные объекта, не людей)
EXTRA_DOC_KEYS = ("brand", "model", "year", "vin", "body_no", "chassis_no", "engine_no", "reg_no",
                  "cadastre_no", "build_year", "walls")
# пределы разбора документа для акта (значения по умолчанию; действующие — act_settings.limits)
# (30.09.2026: договор 0102 резался на строке реквизитов сторон — строка таблицы DOCX мерилась пределом ячейки
#  в 500 знаков; у DOCX теперь свой предел строки таблицы и отдельный счёт абзацев)
DOC_LIMITS = {"doc_max_cells": 10000, "doc_max_paras": 3000, "doc_max_rows": 200, "doc_max_cols": 30,
              "doc_max_sheets": 3, "doc_max_line_chars": 500, "doc_max_row_chars": 4000, "doc_max_para_chars": 4000,
              "doc_max_text_chars": 200_000, "doc_parse_sec": 5, "doc_file_sec_pdf": 8, "doc_parse_total_sec": 12}
APPLICATION = "application"        # вид документа «заявление на страхование» (подпись — act_texts.DOC_KIND_LABELS)
# документов, которые разбираются одновременно на весь сервер
PARSE_SLOTS = 2
_PARSE_SEM = threading.BoundedSemaphore(PARSE_SLOTS)


# ================================================================================================
#  Документы DOCX / XLSX / PDF с текстом
# ================================================================================================

def zip_format(blob: bytes) -> Optional[str]:
    """docx | xlsx по содержимому архива; другой zip или битый — None. Имена частей читаются из
    центрального каталога, сами части не распаковываются."""
    if blob[:4] != b"PK\x03\x04":
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            names = set(z.namelist())
    except Exception:
        return None
    if "word/document.xml" in names:
        return "docx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    return None


def zip_check(blob: bytes, limits: dict) -> tuple:
    """
    Проверка архива до разбора: (код ошибки | None, {"macros": bool}).
    Отказ: больше doc_max_parts частей, распакованный объём больше doc_max_unzip_mb, одна часть
    сжата сильнее 1:200 при размере от 10 МБ (признак zip-бомбы), шифрование, DTD/ENTITY в XML
    (раскрытие сущностей). Макросы (vbaProject.bin) не исполняются никогда — только отмечаются.
    """
    max_parts = int(limits.get("doc_max_parts", 2000))
    max_bytes = float(limits.get("doc_max_unzip_mb", 50)) * 1024 * 1024
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            infos = z.infolist()
            if len(infos) > max_parts:
                return "doc_parts", {}
            total = 0
            for i in infos:
                if i.flag_bits & 0x1:
                    return "doc_encrypted", {}
                total += i.file_size
                if total > max_bytes:
                    return "doc_bomb", {}
                if i.file_size > 10 * 1024 * 1024 and i.file_size > 200 * max(i.compress_size, 1):
                    return "doc_bomb", {}
            macros = any(i.filename.lower().endswith("vbaproject.bin") for i in infos)
            for i in infos:
                if i.filename.lower().endswith((".xml", ".rels")):
                    with z.open(i) as f:
                        if xml_prolog_has_dtd(f, int(max_bytes)):
                            return "doc_dtd", {}
    except zipfile.BadZipFile:
        return "doc_bad", {}
    except Exception:
        return "doc_bad", {}
    return None, {"macros": macros}


_CHUNK = 64 * 1024


def xml_prolog_has_dtd(stream, limit: int) -> bool:
    """
    Есть ли в XML объявление DTD или сущности. Искать надо во всём прологе — от начала части до первого
    элемента: перед <!DOCTYPE может стоять комментарий любой длины (раньше смотрели только первые
    4096 байт, и длинный комментарий прятал DTD). Часть читается потоком кусками по 64 КБ, всего не
    больше limit байт (предел распаковки); после первого элемента DTD в XML быть не может — дальше не читаем.
    UTF-16 (редко, но допустимо) перекодируется. Непонятное объявление «<!…» в прологе — тоже отказ.
    """
    state = {"buf": b"", "eof": False, "total": 0}

    def more() -> bool:
        if state["eof"]:
            return False
        chunk = stream.read(_CHUNK)
        state["total"] += len(chunk)
        if not chunk or state["total"] > limit:
            state["eof"] = True
            return bool(chunk)
        state["buf"] += chunk
        return True

    def skip_until(term: bytes) -> bool:
        while True:
            k = state["buf"].find(term)
            if k >= 0:
                state["buf"] = state["buf"][k + len(term):]
                return True
            state["buf"] = state["buf"][-(len(term) - 1):]      # хвост на случай разрыва терминатора
            if not more():
                return False

    more()
    head = state["buf"][:2]
    if head in (b"\xff\xfe", b"\xfe\xff"):                     # UTF-16: читаем часть целиком (в пределе)
        while more():
            pass
        try:
            text = state["buf"].decode("utf-16")
        except UnicodeDecodeError:
            return True
        state.update(buf=text.encode("utf-8"), eof=True)
    if state["buf"][:3] == b"\xef\xbb\xbf":
        state["buf"] = state["buf"][3:]
    while True:
        while len(state["buf"]) < 16 and more():
            pass
        state["buf"] = state["buf"].lstrip(b" \t\r\n")
        buf = state["buf"]
        if not buf:
            if not more():
                return False
            continue
        if not buf.startswith(b"<"):
            return False                   # не XML — разбор сам откажет
        if buf.startswith(b"<!--"):
            state["buf"] = buf[4:]
            if not skip_until(b"-->"):
                return False
            continue
        if buf.startswith(b"<?"):
            state["buf"] = buf[2:]
            if not skip_until(b"?>"):
                return False
            continue
        if buf.startswith(b"<!"):
            return True                    # <!DOCTYPE, <!ENTITY и любое другое объявление до корня
        return False                       # начался первый элемент


# ================================================================================================
#  Чтение документа для акта: пределы ДО разбора и срок внутри циклов
# ================================================================================================

def doc_limits(limits: Optional[dict]) -> dict:
    lim = dict(DOC_LIMITS)
    for k in DOC_LIMITS:
        v = (limits or {}).get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            lim[k] = v
    return lim


class _Budget:
    """Сколько ещё можно прочитать: ячеек таблиц, абзацев текста (DOCX) и знаков. Лишнее — отбрасывается."""

    def __init__(self, lim: dict):
        self.cells = int(lim["doc_max_cells"])
        self.paras = int(lim.get("doc_max_paras") or self.cells)
        self.chars = int(lim["doc_max_text_chars"])
        self.line = int(lim["doc_max_line_chars"])
        self.para = max(self.line, int(lim.get("doc_max_para_chars") or self.line))
        self.row = max(self.line, int(lim.get("doc_max_row_chars") or self.line))
        self.rows = int(lim["doc_max_rows"])
        self.cols = int(lim["doc_max_cols"])
        self.sheets = int(lim["doc_max_sheets"])
        self.truncated = False

    def cut(self, s: str) -> str:
        if len(s) > self.line:
            self.truncated = True
            return s[:self.line]
        return s

    def cut_para(self, s: str) -> str:
        """Абзац текста (не ячейка таблицы): предел больше — пункт договора бывает на полстраницы."""
        if len(s) > self.para:
            self.truncated = True
            return s[:self.para]
        return s

    def take(self, n_cells: int, n_chars: int) -> bool:
        """Засчитать прочитанное; False — предел исчерпан, читать дальше нельзя."""
        if n_cells > self.cells or n_chars > self.chars:
            self.truncated = True
            return False
        self.cells -= n_cells
        self.chars -= n_chars
        return True

    def take_para(self, n_chars: int) -> bool:
        """Абзац текста DOCX: свой счёт абзацев (doc_max_paras) и общий счёт знаков."""
        if self.paras < 1 or n_chars > self.chars:
            self.truncated = True
            return False
        self.paras -= 1
        self.chars -= n_chars
        return True

    def fit_row(self, cells: list, limit: Optional[int] = None) -> list:
        """Строка таблицы «ячейка | ячейка» не длиннее предела строки: правые ячейки отбрасываются.
        limit — свой предел (строка таблицы DOCX: реквизиты двух сторон в одной строке длиннее 500 знаков)."""
        width = int(limit or self.line)
        out, used = [], 0
        for k, c in enumerate(cells):
            add = len(c) + (3 if out else 0)
            if used + add > width:
                if not any(cells[k:]):
                    break                  # дальше только пустые ячейки выравнивания — ничего не потеряно
                room = width - used - (3 if out else 0)
                if room > 0:
                    out.append(c[:room])
                self.truncated = True
                break
            out.append(c)
            used += add
        return out

    def fit_rows(self, rows: list, limit: Optional[int] = None) -> list:
        """После выравнивания таблицы (ingest._clean_rows) строка снова приводится к пределу."""
        return [self.fit_row(r, limit) for r in rows]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# что в абзаце DOCX не текст: запасная копия надписи (mc:Fallback повторяет mc:Choice), удалённые правкой
# куски, коды полей, свойства абзаца и прогона (w:pPr/w:tabs/w:tab — позиции табуляции, а не знак табуляции)
_PARA_SKIP = frozenset(("Fallback", "del", "delText", "instrText", "pPr", "rPr", "moveFrom"))


def para_text(p) -> str:
    """
    Текст абзаца DOCX из дерева ElementTree (не регулярными выражениями по XML): все w:t абзаца подряд —
    Word режет слово на несколько прогонов w:r (правописание, rsid), они склеиваются без пробела; w:tab —
    табуляция, w:br и w:cr — перенос строки, w:noBreakHyphen — дефис. Абзацы надписи внутри абзаца — с новой строки.
    """
    out = []

    def walk(node):
        for ch in node:
            n = _local(ch.tag)
            if n in _PARA_SKIP:
                continue
            if n == "t":
                out.append(ch.text or "")
            elif n == "tab":
                out.append("\t")
            elif n in ("br", "cr"):
                out.append("\n")
            elif n == "noBreakHyphen":
                out.append("-")
            elif n == "p":
                walk(ch)
                out.append("\n")
            else:
                walk(ch)
    walk(p)
    return "".join(out).strip()


def _read_docx_limited(path: Path, bud: _Budget) -> tuple:
    """
    DOCX потоком (iterparse) с тем же смыслом, что ingest.read_docx: абзацы документа и таблицы верхнего
    уровня (строка таблицы — «ячейка | ячейка»). Абзацы в блоках w:sdt и w:customXml — тоже текст документа;
    вложенная таблица читается внутри своей ячейки. Прочитанные элементы сразу очищаются; срок проверяется
    на каждом элементе, пределы — на каждом абзаце (doc_max_paras), ячейке (doc_max_cells), строке таблицы
    (doc_max_row_chars) и по знакам (doc_max_text_chars).
    """
    from . import docparse as D
    from . import ingest
    lines, tables = [], []
    stack, parts, cells, rows = [], [], [], []
    body = None
    tbl_depth = p_depth = 0

    def row_line(cells_row: list) -> str:
        return " | ".join(cells_row)

    def finish_table():
        got = bud.fit_rows(ingest._clean_rows(rows), bud.row)
        if got:
            tables.append({"name": "таблица %d" % (len(tables) + 1), "rows": got})
            lines.extend(row_line(r) for r in got)

    with zipfile.ZipFile(path) as z:
        try:
            src = z.open("word/document.xml")
        except KeyError:
            raise ValueError("в файле нет word/document.xml — это не документ Word")
        with src:
            for event, el in ET.iterparse(src, events=("start", "end")):
                D.tick()
                name = _local(el.tag)
                if event == "start":
                    stack.append(name)
                    if name == "body" and len(stack) == 2:
                        body = el
                    elif name == "tbl" and body is not None:
                        tbl_depth += 1
                    elif name == "p" and body is not None:
                        p_depth += 1
                    continue
                stack.pop()
                top_child = body is not None and len(stack) == 2
                if body is None:
                    continue
                if name == "p":
                    p_depth -= 1
                    if p_depth:
                        continue           # абзац надписи внутри абзаца — уже в тексте внешнего абзаца
                    txt = bud.cut_para(para_text(el))
                    el.clear()
                    if txt:
                        if tbl_depth == 0:
                            if not bud.take_para(len(txt) + 1):
                                break
                            lines.append(txt)
                        else:
                            parts.append(txt)
                elif name == "tc" and tbl_depth == 1:
                    cell = bud.cut_para(" ".join(x for x in parts if x).strip())
                    parts = []
                    el.clear()
                    if cell and not bud.take(1, 0):
                        break
                    cells.append(cell)
                elif name == "tr" and tbl_depth == 1:
                    if any(cells):
                        if len(rows) >= bud.rows:
                            bud.truncated = True
                        else:
                            if len(cells) > bud.cols:
                                bud.truncated = True
                            row = bud.fit_row(cells[:bud.cols], bud.row)
                            if not bud.take(0, len(row_line(row)) + 1):
                                cells = []
                                break
                            rows.append(row)
                    cells = []
                    el.clear()
                elif name == "tbl":
                    tbl_depth -= 1
                    if tbl_depth == 0:
                        finish_table()
                        rows = []
                        el.clear()
                if top_child:
                    # разобранный элемент тела убираем из дерева: пустые узлы не копятся в памяти
                    _body_done(body, el)
                    el.clear()
    if rows or any(cells):
        if any(cells) and len(rows) < bud.rows:
            rows.append(bud.fit_row(cells[:bud.cols], bud.row))
        finish_table()                     # чтение оборвал предел посреди таблицы — прочитанное не теряем
    return "\n".join(lines), tables


def _body_done(body, el) -> None:
    """Отцепить закрытый элемент от тела документа (сам элемент ещё разбирается вызывающим)."""
    try:
        body.remove(el)
    except ValueError:
        pass


_CELL_REF = re.compile(r"([A-Z]+)(\d+)")


def _col_index(ref: str) -> Optional[int]:
    m = _CELL_REF.match(ref or "")
    if not m:
        return None
    n = 0
    for ch in m.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n


def _xlsx_number(v: str) -> str:
    """Число ячейки — так же, как его отдаёт openpyxl (целое без «.0»)."""
    s = (v or "").strip()
    if re.fullmatch(r"-?\d+", s):
        return str(int(s))
    try:
        return str(float(s))
    except ValueError:
        return s


def _read_xlsx_limited(path: Path, bud: _Budget) -> tuple:
    """
    XLSX своим потоковым чтением (openpyxl в режиме read_only целиком читает общую таблицу строк ещё до
    первого листа — на большой книге это десятки секунд без возможности остановиться). Берём не больше
    doc_max_sheets листов, doc_max_rows строк и doc_max_cols колонок с листа; из общей таблицы строк —
    только нужные номера. Даты приходят числом (формат ячейки не читается) — для акта это не важно.
    """
    from . import docparse as D
    from . import ingest
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = {}
        if "xl/_rels/workbook.xml.rels" in names:
            for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")):
                if str(r.get("Type") or "").endswith("/worksheet"):
                    tgt = str(r.get("Target") or "")
                    tgt = tgt.lstrip("/") if tgt.startswith("/") else posixpath.normpath("xl/" + tgt)
                    rels[r.get("Id")] = tgt
        # r:id — в обычном и в строгом (purl.oclc.org) пространстве имён
        sheets = [(str(s.get("name") or ""), rels.get(next((v for k, v in s.attrib.items() if k.endswith("}id")),
                                                            None)))
                  for s in wb.iter() if _local(s.tag) == "sheet"]
        sheets = [(n, p) for n, p in sheets if p and p in names]
        if len(sheets) > bud.sheets:
            bud.truncated = True
        raw_tables, need = [], set()
        stop = False
        for name, part in sheets[:bud.sheets]:
            rows, cut = {}, False
            row_no, col_no, cur, cell_t, cell_v, cell_is = 0, 0, None, None, None, []
            with z.open(part) as src:
                for event, el in ET.iterparse(src, events=("start", "end")):
                    D.tick()
                    tag = _local(el.tag)
                    if event == "start":
                        if tag == "row":
                            try:
                                row_no = int(el.get("r") or row_no + 1)
                            except ValueError:
                                row_no += 1
                            col_no = 0
                            if row_no > bud.rows:
                                cut = True
                                break
                            cur = rows.setdefault(row_no, {})
                        continue
                    if tag == "c":
                        col = _col_index(el.get("r") or "") or col_no + 1
                        col_no = col
                        t = el.get("t") or "n"
                        v = next((x.text for x in el if _local(x.tag) == "v"), None)
                        if col > bud.cols:
                            if v is not None or t == "inlineStr":
                                bud.truncated = True
                        elif cur is not None:
                            if t == "s" and v is not None and v.strip().isdigit():
                                cur[col] = ("s", int(v))
                                need.add(int(v))
                            elif t == "inlineStr":
                                cur[col] = "".join(_si_text(x) for x in el if _local(x.tag) == "is")
                            elif t == "b" and v is not None:
                                cur[col] = "True" if v.strip() == "1" else "False"
                            elif t in ("str", "e") and v is not None:
                                cur[col] = v
                            elif v is not None:
                                cur[col] = _xlsx_number(v)
                            if col in cur and not bud.take(1, 0):
                                cur.pop(col, None)
                                stop = True
                                break
                        el.clear()
                    elif tag == "row":
                        el.clear()
            raw_tables.append((name, rows, cut))
            if cut:
                bud.truncated = True
            if stop:
                break
        shared = {}
        if need and "xl/sharedStrings.xml" in names:
            top = max(need)
            k = -1
            with z.open("xl/sharedStrings.xml") as src:
                for _event, el in ET.iterparse(src, events=("end",)):
                    D.tick()
                    if _local(el.tag) != "si":
                        continue
                    k += 1
                    if k in need:
                        shared[k] = _si_text(el)
                    el.clear()
                    if k >= top:
                        break              # дальше нужных номеров общую таблицу строк не читаем
    lines, tables = [], []
    full = True
    for name, rows, cut in raw_tables:
        grid = []
        for r in sorted(rows):
            row = rows[r]
            width = max(row) if row else 0
            vals = []
            for c in range(1, width + 1):
                v = row.get(c, "")
                if isinstance(v, tuple):
                    v = shared.get(v[1], "")
                vals.append(bud.cut(str(v).strip()))
            vals = bud.fit_row(vals)
            if any(vals) and not bud.take(0, len(" | ".join(vals)) + 1):
                full = False
                break
            grid.append(vals)
        grid = bud.fit_rows(ingest._clean_rows(grid))
        if grid:
            if cut:
                name += " (показаны первые %d строк)" % bud.rows
            tables.append({"name": name, "rows": grid, "обрезан": cut or not full})
            lines.append(name)
            lines.extend(" | ".join(r) for r in grid)
        if not full:
            break
    return "\n".join(lines), tables


def _si_text(si) -> str:
    """Текст строки общей таблицы: все w:t, кроме фонетической подсказки rPh (как в openpyxl)."""
    out = []

    def walk(node):
        for ch in node:
            n = _local(ch.tag)
            if n == "rPh":
                continue
            if n == "t":
                out.append(ch.text or "")
            else:
                walk(ch)
    walk(si)
    return "".join(out)


def _read_pdf_limited(path: Path, bud: _Budget) -> tuple:
    """PDF: текст и таблицы постранично (как ingest.read_pdf), срок проверяется перед каждой страницей."""
    import pymupdf
    from . import docparse as D
    from . import ingest
    lines, tables = [], []
    with pymupdf.open(path) as doc:
        for n, page in enumerate(doc, 1):
            D.tick()
            full = True
            for ln in page.get_text("text").splitlines():
                ln = bud.cut(ln)
                if ln.strip() and not bud.take(1, len(ln) + 1):
                    full = False
                    break
                lines.append(ln)
            if not full:
                break
            D.tick()
            try:
                for tb in (page.find_tables() or []):
                    rows = ingest._clean_rows(tb.extract())[:bud.rows]
                    rows = [bud.fit_row([bud.cut(c) for c in r[:bud.cols]]) for r in rows]
                    if rows and bud.take(sum(len(r) for r in rows), sum(len(" | ".join(r)) + 1 for r in rows)):
                        tables.append({"name": "стр. %d" % n, "rows": rows})
            except D.ParseTimeout:
                raise
            except Exception as e:     # таблицы — дополнение, из-за них разбор не роняем
                print("акт: таблицы PDF не прочитаны:", type(e).__name__)
    return "\n".join(lines), tables


def read_limited(path: Path, limits: Optional[dict] = None) -> dict:
    """
    Чтение документа для акта в пределах limits (doc_max_*): лишнее отбрасывается, truncated=True.
    Ответ — как у ingest.read_file: {"format", "text", "tables", "status", "note", "truncated"}.
    Срок (docparse.deadline_scope) проверяется внутри циклов чтения — ParseTimeout уходит вызывающему.
    """
    from . import docparse as D
    from . import ingest
    fmt = ingest.sniff_format(path)
    if fmt not in ("pdf", "docx", "xlsx"):
        return {**ingest.read_file(path), "truncated": False}
    bud = _Budget(doc_limits(limits))
    try:
        if fmt == "pdf":
            text, tables = _read_pdf_limited(path, bud)
        elif fmt == "docx":
            text, tables = _read_docx_limited(path, bud)
        else:
            text, tables = _read_xlsx_limited(path, bud)
    except D.ParseTimeout:
        raise
    except Exception as e:
        return {"format": fmt, "text": "", "tables": [], "status": ingest.ST_ERROR, "truncated": False,
                "note": "Файл прочитать не удалось: %s" % type(e).__name__}
    base = {"format": fmt, "text": text, "tables": tables, "truncated": bud.truncated}
    if len(D.norm(text)) < D.MIN_TEXT_CHARS:
        return {**base, "status": ingest.ST_OCR if fmt == "pdf" else ingest.ST_PARTIAL, "note": None}
    return {**base, "status": None, "note": None}


def parse_document_limited(con, path: Path, class_code: str = "", limits: Optional[dict] = None,
                           seconds: Optional[float] = None, term_inclusive: bool = True) -> dict:
    """
    parse_document в пределах limits и со сроком seconds (проверка внутри циклов чтения и поиска подписей,
    поток освобождается сам). Не уложились — {"status": "timeout", notes: ["doc_timeout"]}.
    """
    from . import docparse as D
    lim = doc_limits(limits)
    sec = float(lim["doc_parse_sec"]) if seconds is None else seconds
    try:
        with D.deadline_scope(sec):
            return parse_document(con, path, class_code, limits=lim, term_inclusive=term_inclusive)
    except D.ParseTimeout:
        return {"text_layer": False, "status": "timeout", "format": None, "kind": None, "items": [],
                "prefill": {}, "notes": ["doc_timeout"]}


def parse_slot(timeout: float) -> bool:
    """Место в очереди разбора (не больше PARSE_SLOTS документов одновременно на сервер)."""
    return _PARSE_SEM.acquire(timeout=max(0.0, timeout))


def parse_slot_release() -> None:
    _PARSE_SEM.release()


def _num_text(x: float) -> str:
    v = float(x)
    return f"{v:,.0f}".replace(",", " ") if v == int(v) else f"{v:,.2f}".replace(",", " ")


def parse_document(con, path: Path, class_code: str = "", limits: Optional[dict] = None,
                   term_inclusive: bool = True) -> dict:
    """
    Разбор файла с текстовым слоем существующими парсерами, без языковой модели.
    Возвращает {"text_layer": bool, "status", "kind", "items": [{key, value}], "prefill": {...},
    "notes": [коды]}. Данные людей не извлекаются: строки с подписями ФИО, паспорта, ПИНФЛ, адреса
    проживания пропускает docparse.is_personal_label, значения маскирует ingest._mask_fields;
    вызывающий дополнительно отбрасывает всё, похожее на ПД (act.pd_like).
    limits — пределы акта (doc_max_*): документ читается read_limited, лишнее отбрасывается с пометкой
    doc_partial; без limits — прежнее ingest.read_file.
    Запрос филиала (таблица из 16 строк, app/branch_request.py) узнаётся раньше общего разбора: kind =
    branch_request, в ответе ещё блок branch_request; term_inclusive — дни срока с обоими крайними днями.
    Договор страхования (app/contract_read.py) узнаётся после общего разбора: kind = contract, в ответе блок
    contract (все условия договора); его сумма, стоимость и срок по датам сильнее общего разбора.
    """
    from . import analysis_docs as ad
    from . import docparse as D
    from . import ingest
    read = read_limited(path, limits) if limits is not None else ingest.read_file(path)
    out = {"text_layer": False, "status": read.get("status"), "format": read.get("format"), "kind": None,
           "items": [], "prefill": {}, "notes": []}
    if read["status"] in (ingest.ST_UNSUPPORTED, ingest.ST_ERROR):
        out["notes"].append("doc_unreadable")
        return out
    if read["status"] == ingest.ST_OCR:
        return out                        # скан без текста: его читает модель (PDF) или никто
    if read.get("truncated"):
        out["notes"].append("doc_partial")
    text, tables = read["text"][:ingest.MAX_TEXT_CHARS], read["tables"]
    out["text_layer"] = True
    D.tick()
    # отчёт кредитного бюро (КАТМ, 01.10.2026): узнаётся первым — у него нет полей объекта, только заёмщик;
    # у физлица ФИО, ПИНФЛ, адрес и телефон не извлекаются (app/credit_report.py)
    crp = credit_report.parse_text(text)
    if crp:
        out.update(kind=credit_report.KIND, credit_report=crp)
        out["notes"] += [c for c in crp["notes"] if c not in out["notes"]]
        return out
    # заголовок сильнее строк: «ДОГОВОР СТРАХОВАНИЯ», «ПОЛИС» — договор, даже если строки как в бланке запроса;
    # «ЗАЯВЛЕНИЕ НА СТРАХОВАНИЕ», «АРИЗА» — заявление (данные подставляются, сверки договора нет)
    title = contract_read.title_kind(text, tables)
    brq = None if title else branch_request.parse_text(text, tables, term_inclusive)
    if brq:
        f = brq["fields"]
        out.update(kind=branch_request.KIND, items=branch_request.items(f),
                   prefill=branch_request.prefill(f, branch_request.region_in(f)),
                   branch_request={"rows": brq["rows"], "rows_found": brq["rows_found"], "fields": f})
        if f.get("term_error"):
            out["notes"].append("br_term_unread")
        if not out["items"]:
            out["notes"].append("doc_no_values")
        return out
    lang = ingest.detect_language(text)
    kind = ingest.detect_kind(text, tables, None)["kind"]
    out["kind"] = kind
    D.tick()
    ctr = contract_read.parse_text(text, tables, term_inclusive, pdf=read.get("format") == "pdf",
                                   force=title == "application")
    D.tick()
    fields = ingest._mask_fields(ingest.extract_fields(text, tables, kind, lang.get("language"),
                                                       use_llm=False, with_reg_no=True))
    # договор, заявление или выгрузка по технике/зданию: подписи техпаспорта или кадастра тоже ищем,
    # но берём только поля объекта (номера, марку, год), без «типа» и регионов учёта
    extra_kind = D.KIND_PASSPORT if class_code in ra.VEHICLE_CLASSES else \
        D.KIND_CADASTRE if class_code in ra.PROPERTY_CLASSES else None
    if kind not in D.FIELDS and extra_kind:
        have = {f["ключ"] for f in fields}
        got = D.parse_text("\n".join(ingest._lines(text, tables)), extra_kind, with_reg_no=True, use_llm=False)
        for it in got.get("поля") or []:
            if it.get("значение") is not None and it["поле"] in EXTRA_DOC_KEYS and it["поле"] not in have:
                fields += ingest._mask_fields([{"ключ": it["поле"], "значение": it["значение"]}])
    D.tick()
    terms = ingest.extract_contract_terms(text, tables)
    fd = ad._fields_dict(fields, terms)
    D.tick()
    if ad._is_insurance_contract(text):
        months, _label, term_note = ad.contract_term(text, tables)
        if months:
            fd["term_months"] = {"value": months, "found_by": _label}
        if term_note:
            out["notes"].append("doc_term_conflict")
    pre, _notes = ad.build_prefill(con, fd, class_code if class_code in ra.PROPERTY_CLASSES | ra.VEHICLE_CLASSES
                                   else "")
    must = pre.get("must") or {}
    for key in ("sum_insured", "object_value"):
        src = fd.get(key) or {}
        if src.get("value_num") is not None and src.get("currency") not in (None, "UZS"):
            out["notes"].append("doc_currency")
    items = []

    def add(key, value):
        v = str(value if value is not None else "").strip()
        if v and not any(i["key"] == key for i in items):
            items.append({"key": key, "value": v[:120]})

    prefill = {}
    for key in ("sum_insured", "object_value"):
        if must.get(key):
            val = float(must[key]["value"])
            add(key, _num_text(val))
            prefill[key] = val
    tm = (fd.get("term_months") or {}).get("value")
    if isinstance(tm, int) and 1 <= tm <= 60:
        days = contract_read.months_days(tm)
        add("term_days", str(days))
        prefill["term_days"] = days
    if must.get("region"):
        name = must["region"].get("name") or must["region"].get("value")
        add("region", name)
        prefill["region"] = name
    obj = (fd.get("object_kind") or {}).get("value")
    if obj:
        add("object_type", obj)
    walls = (fd.get("walls") or {}).get("value")
    if walls:
        add("construction", walls)
    by = {f["ключ"]: f.get("значение") for f in fields if f.get("значение")}
    for key in ("brand", "model", "engine_no", "color", "reg_no", "cadastre_no"):
        if by.get(key):
            add(key, by[key])
    for key in ("vin", "body_no", "chassis_no"):
        if by.get(key):
            add("serial_no", by[key])
            break
    y = by.get("year") or by.get("build_year") or (fd.get("year") or {}).get("value")
    if y:
        add("year", y)
    if kind == D.KIND_PASSPORT:
        # техпаспорт (02.10.2026): подпись объекта, год, подгруппа ТС, топливо, характеристики — автозаполнение акта
        # (app/vehicle_prefill.py); поля владельца сюда не попадают — docparse их не извлекает
        from . import vehicle_prefill as vp
        veh = vp.from_passport(by)
        if veh:
            out["vehicle"] = veh
            if by.get("engine_power"):
                add("engine_power", by["engine_power"])
    if ctr:
        # договор: его значения (сумма «общая», срок по датам) точнее общего разбора — заменяют их
        f = ctr["fields"]
        out["kind"] = APPLICATION if title == "application" else contract_read.KIND
        own = {it["key"]: it["value"] for it in contract_read.items(f)}
        for key in ("sum_insured", "object_value", "term_days"):
            if key in own:
                items = [it for it in items if it["key"] != key]
        for key, value in own.items():
            add(key, value)
        for key, value in contract_read.prefill(f).items():
            if key in ("sum_insured", "object_value", "term_days") or key not in prefill:
                prefill[key] = value
        if title == "application":
            # заявление: те же поля в распознанное и подсказку, но это не договор — ни сверки, ни ст. 929
            out["notes"].append("doc_application")
        else:
            out["contract"] = dict(ctr, truncated="doc_partial" in out["notes"])
            if contract_read.need_assist(f):
                out["_text"] = text       # только в памяти: для дочитывания моделью (app/act.py), в базу не идёт
        if f.get("currency") not in (None, "UZS") and "doc_currency" not in out["notes"]:
            out["notes"].append("doc_currency")
    out["items"] = items
    out["prefill"] = prefill
    if not items:
        out["notes"].append("doc_no_values")
    return out


# ================================================================================================
#  Вход старого движка: поля акта → поля risk_analytics
# ================================================================================================

def _default_activity(con, cls: str, product_code: Optional[str], otype: Optional[str]) -> tuple:
    """
    (тип объекта, деятельность по умолчанию для него). Тип — как у ra.apply_defaults: заданный, иначе по
    продукту, иначе типовой для класса; деятельность — по таблице модуля ra.ACTIVITY_BY_OBJECT_TYPE.
    Нет строки для типа — (тип, None): тогда деятельность подставит ra.apply_defaults, как раньше.
    """
    t = otype
    if not t:
        try:
            ref = db.load_reference(con)
            pd = ra.product_defaults(ref, product_code) if product_code else None
        except Exception:                 # справочник не прочитан — решает ra.apply_defaults
            pd = None
        t = (pd or {}).get("object_type") or ra._defaults_for_classes([cls]).get("object_type")
    return t, ra.ACTIVITY_BY_OBJECT_TYPE.get(t or "")


def ra_context(con, *, cls: str, product_code: Optional[str], otype: Optional[str], group: str,
               kind: Optional[str], S: float, V: float, region: str, term_days: Optional[int],
               year: Optional[int], o: dict, recognized: list, text: str = "") -> dict:
    """
    Те же входные данные, что у акта, в форме risk_analytics. Чего нет — подставляет
    ra.apply_defaults (быстрый режим), каждая подстановка — в assumptions с пометкой «по умолчанию».
    text — описание объекта из документа: по нему определяется деятельность на объекте, если её не ввели
    и вид объекта её не задаёт (хлебопекарное производство → пищевое производство).
    Возвращает {"ok", "must", "optional", "assumptions": [{"code", "params"}], "analysis", "error",
    "sources": {поле: input | document | photo | text | kind | product | default}}.
    """
    must = {"class_code": cls, "product_code": product_code or "", "sum_insured": S, "object_value": V,
            "region": region}
    assumptions = []
    sources = {}
    if otype:
        must["object_type"] = otype
    if term_days:
        months = term_days * 12 / 365
        if abs(term_days - 365) <= 1:
            months = 12
        if months > 60:
            months = 12
            assumptions.append({"code": "as_term_long", "params": {"days": term_days}})
        must["term_months"] = round(months, 4)
    if cls in ra.VEHICLE_CLASSES:
        vt = "special" if group == "special" else KIND_VEHICLE.get(kind or "")
        if vt:
            must["vehicle_type"] = vt
            sources["vehicle_type"] = "kind"
        if year:
            must["year"] = int(year)
            rec_y = next((r for r in recognized if r.get("key") in ("year", "manufacture_date") and r.get("value")),
                         None)
            sources["year"] = "input" if o.get("year") or not rec_y else (rec_y.get("source") or "document")
    if cls in ra.PROPERTY_CLASSES:
        cons = o.get("construction")
        if cons:
            sources["construction"] = "input"
        else:
            walls = next((r for r in recognized if r["key"] == "construction" and r.get("value")), None)
            from .analysis_docs import CONSTRUCTION_WORDS, _one
            cons = _one(walls["value"], CONSTRUCTION_WORDS) if walls else None
            if cons:
                sources["construction"] = walls.get("source") or "document"
        if cons:
            must["construction"] = cons
        # деятельность: ввод сотрудника → вид объекта (однозначный) → описание объекта в документе → умолчание
        act = o.get("activity")
        if act:
            sources["activity"] = "input"
        elif KIND_ACTIVITY.get(kind or ""):
            act = KIND_ACTIVITY[kind]
            sources["activity"] = "kind"
        else:
            act = activity_from_text(text)
            if act:
                sources["activity"] = "text"
                assumptions.append({"code": "as_activity_text", "params": {"value": act}})
        if not act:
            # деятельность по умолчанию — согласованная с типом объекта (тот же тип, что подставит
            # ra.apply_defaults): «производство» не получает «склад» из названия продукта (30.09.2026)
            otype_eff, act = _default_activity(con, cls, product_code, otype)
            if act:
                sources["activity"] = "default"
                assumptions.append({"code": "as_activity_by_type", "params": {"value": act, "type": otype_eff}})
        if act:
            must["activity"] = act
    optional = {}
    if o.get("protection"):
        optional["protection"] = o["protection"]
        sources["protection"] = "input"
    if o.get("seismic_zone") is not None:
        optional["seismic_zone"] = o["seismic_zone"]
        sources["seismic_zone"] = "input"
    if o.get("losses_count") is not None:
        sources["losses_3y"] = "input"
        optional["losses_3y"] = {"count": o["losses_count"]}
        if o.get("losses_amount") is not None:
            optional["losses_3y"]["amount"] = o["losses_amount"]
        if o.get("small_count") is not None:
            optional["losses_3y"]["small_count"] = o["small_count"]
    try:
        m, op, a = ra.apply_defaults(con, must, optional)
        an = ra.analyze(con, m, op, assumptions=a)
    except Exception as e:                # сбой старого движка не роняет акт: блоки честно помечены
        return {"ok": False, "error": type(e).__name__, "must": must, "optional": optional,
                "assumptions": assumptions, "analysis": None, "sources": sources}
    if not an.get("ok"):
        v = an.get("validation") or {}
        return {"ok": False, "error": "validation:" + ",".join(sorted(list(v.get("missing") or [])
                                                                      + list((v.get("errors") or {})))),
                "must": m, "optional": op, "assumptions": assumptions, "analysis": None, "sources": sources}
    for x in a:
        if x["key"] == "year":
            assumptions.append({"code": "as_year", "params": {}})
            sources["year"] = "default"
        elif x["key"] in ("object_type", "construction", "activity", "vehicle_type", "term_months"):
            assumptions.append({"code": "as_" + x["key"], "params": {"value": x["value"]}})
            sources[x["key"]] = "default"
    if otype and "object_type" not in sources:
        sources["object_type"] = "kind"
    return {"ok": True, "error": None, "must": m, "optional": op, "assumptions": assumptions, "analysis": an,
            "sources": sources}


# ================================================================================================
#  1. Сценарии PML / EML / MFL
# ================================================================================================

# Порядок и формулировки заказчика (21.09.2026, акт 29.09.2026): PML ≤ EML ≤ MFL.
#   PML — вероятный максимальный убыток, защита сработала штатно (наименьший);
#   EML — оценочный максимальный убыток, защита сработала частично; с ним сравнивается удержание;
#   MFL — максимально возможный убыток при отказе защиты или катастрофе.
# В risk_analytics названия переставлены (там EML — «защита сработала», PML — «сработала частично»),
# сам модуль не меняется — сопоставление только здесь: сценарий акта ← сценарий модуля.
ACT_FROM_RA = {"PML": "EML", "EML": "PML", "MFL": "MFL"}
SCENARIO_ORDER = "classic"               # метка сопоставления в сохранённом акте (старые акты — без неё)


def _what(rule: str, s: str, sc: dict, op: dict) -> tuple:
    """(код причины, параметры, код состояния защиты) — подпись того сценария, который посчитан."""
    state = {"PML": "sc_state_pml", "EML": "sc_state_eml", "MFL": "sc_state_mfl"}[s]
    if rule == "vehicle":
        if s == "PML":
            return "sc_w_veh_pml", {}, None
        if s == "EML":
            prot = op.get("protection") in ("immo", "tracker")
            return ("sc_w_veh_eml_prot" if prot else "sc_w_veh_eml_noprot"), {}, None
        return "sc_w_veh_mfl", {}, None
    whole = not op.get("compartments")
    if rule == "property9":
        return ("sc_w_c9_whole" if whole else "sc_w_c9"), {}, state
    zone = op.get("seismic_zone")
    eq, fire = sc.get("earthquake_loss"), sc.get("fire_loss")
    if eq is not None and zone is None and s == "MFL":
        # сейсмозона не указана: катастрофа принята как полное уничтожение (risk_analytics, eq = V × 1,00)
        return "sc_w_total", {}, state
    if eq is not None and (fire is None or eq > fire):
        return "sc_w_eq", {"zone": zone}, state
    return ("sc_w_fire_whole" if whole else "sc_w_fire"), {}, state


def _retention_block(an: Optional[dict], eml: float, mfl: Optional[float] = None) -> Optional[dict]:
    """Лимит удержания из risk_analytics (Положение 1806, п. 15) против EML акта; MFL — справкой."""
    if not an:
        return None
    ret = an.get("retention") or {}
    known = ret.get("retention_limit") is not None
    cat = (an["scenarios"]["MFL"].get("catastrophe") or {})
    status = {"по отчётности": "reported", "временно": "temporary"}.get(ret.get("status"), "unknown")
    limit = ret.get("retention_limit")
    mfl_excess = cat.get("reinsurance_need") if known else None
    if known and mfl is not None:
        mfl_excess = round(max(float(mfl) - limit, 0)) or None
    return {"known": known, "limit": limit, "limit_per_risk": ret.get("limit_per_risk"),
            "line_retention": ret.get("line_retention"), "line_class": ret.get("line_class"),
            "own_funds": ret.get("own_funds"), "reserves": ret.get("reserves"), "status": status,
            # удержание сравнивается с EML (решение заказчика 21.09.2026); MFL — отдельной справкой
            "compared_with": "eml",
            "eml_excess": round(max(eml - limit, 0)) if known else None,
            "within": (eml <= limit) if known else None,
            "mfl_excess": mfl_excess,
            "legal_ref": ret.get("legal_ref") or "Положение № 1806, п. 15"}


def _num(v) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x > 0 else None


def simple_scenarios(rule: str, S: float, V: float, fields: Optional[dict] = None,
                     params: Optional[dict] = None) -> dict:
    """
    Простые экспертные правила сценариев для классов без правила в risk_analytics (шаблон класса, приложение А).
    Чистая функция: S — страховая сумма (лимит), V — стоимость (лимит), fields — поля класса (optional.class_fields),
    params — параметры правила из шаблона. Возвращает {"PML", "EML", "MFL": {"amount", "formula"},
    "assumptions": [{"code", "params"}], "checks": [...]}; PML ≤ EML ≤ MFL ≤ страховой суммы (кроме накопления груза).
    Лимиты ответственности (limit, full_limit) выше страховой суммы не берутся: min(лимит, S), в допущениях —
    «лимит … выше страховой суммы — взята страховая сумма» (as_tpl_limit_case_over, as_tpl_limit_aggregate_over).
    Все правила экспертные (calibrated = 0), это не статистика убытков.
      people     — НС: PML = EML = сумма на человека; MFL = сумма на человека × люди в одном месте (нет — все);
      frequency  — болезни: PML = EML = лимит на человека; MFL = max(лимит, epidemic_share × S);
      unit       — ж/д: PML = EML = S / число единиц; MFL = S (весь состав);
      full_loss  — авиа, суда: всё = min(S, V) (полная гибель);
      shipment   — грузы: PML = EML = сумма отправки (нет — S); MFL = накопление (нет — S);
      limit      — ответственность: PML = EML = min(лимит на случай, S) (нет — S); MFL = min(годовой лимит, S)
                   (нет — S);
      full_limit — ответственность в авиации и на море: всё = min(лимит, S) (нет — S);
      full_sum   — гарантии: всё = S;
      credit     — кредиты: всё = S; проверка S ≤ min(кредит − обеспечение; 50 % кредита);
      bi         — финансовые риски: PML = EML = потери в месяц × срок восстановления (≤ S; нет — S); MFL = S;
      dispute    — правовая защита: PML = EML = лимит на спор (нет — S); MFL = S;
      crop       — урожай (16у): стоимость урожая W = площадь, га × средняя урожайность за 5 лет, ц/га × цена, сум/ц
                   (нет полей — W = стоимость V); база B = min(S, W) (ниже стоимости — пропорциональная выплата,
                   ГК ст. 936); PML = B × pml_area_share × pml_loss_ratio (частичная потеря урожая на части площади),
                   EML = B × eml_area_share × eml_loss_ratio (гибель на значительной площади), MFL = B (полная
                   гибель на всей площади). Проверка: S ≤ W (ГК ст. 938). Доли — экспертные параметры шаблона.
    """
    from .class_templates import credit_insurable
    f = fields or {}
    p = params or {}
    S = float(S)
    V = float(V) if V else S
    asm, checks = [], []

    def money(x):
        return f"{round(x):,}".replace(",", " ") + " сум"

    def out(pml, eml, mfl, fp, fe, fm):
        eml = max(eml, pml)
        mfl = max(mfl, eml)
        return {"PML": {"amount": round(pml), "formula": "PML = " + fp},
                "EML": {"amount": round(eml), "formula": "EML = " + fe},
                "MFL": {"amount": round(mfl), "formula": "MFL = " + fm},
                "assumptions": asm, "checks": checks}

    if rule == "people":
        n = _num(f.get("insured_count"))
        per = _num(f.get("sum_per_person"))
        if per is None:
            per = S / n if n else S
            asm.append({"code": "as_tpl_per_person" if n else "as_tpl_per_person_sum", "params": {}})
        per = min(per, S)
        place = _num(f.get("people_in_one_place"))
        if place is None:
            place = n
            asm.append({"code": "as_tpl_place", "params": {}})
        mfl = min(S, per * place) if place else S
        fm = f"{money(per)} × {int(place)} чел. в одном месте = {money(mfl)}" if place else f"страховая сумма {money(S)}"
        return out(per, per, mfl, f"сумма на человека {money(per)}", f"сумма на человека {money(per)}", fm)
    if rule == "frequency":
        n = _num(f.get("insured_count"))
        lim = _num(f.get("limit_per_person"))
        if lim is None:
            lim = S / n if n else S
            asm.append({"code": "as_tpl_limit_person", "params": {}})
        lim = min(lim, S)
        share = float(p.get("epidemic_share", 0.3))
        mfl = max(lim, min(S, S * share))
        asm.append({"code": "as_tpl_epidemic", "params": {"pct": round(share * 100, 1)}})
        visits, bill = _num(f.get("avg_visits")), _num(f.get("avg_bill"))
        if visits and bill and n:
            checks.append({"code": "tpl_expected", "params": {"amount": round(visits * bill * n)}})
        return out(lim, lim, mfl, f"лимит на человека {money(lim)}", f"лимит на человека {money(lim)}",
                   f"эпидемия: {share:g} × {money(S)} = {money(mfl)}")
    if rule == "unit":
        u = _num(f.get("units_count"))
        one = S / u if u else S
        if not u:
            asm.append({"code": "as_tpl_units", "params": {}})
        return out(one, one, S, f"одна единица {money(one)}" + (f" ({money(S)} / {int(u)})" if u else ""),
                   f"одна единица {money(one)}", f"весь состав {money(S)}")
    if rule == "full_loss":
        b = min(S, V)
        txt = f"полная гибель: меньшее из суммы и стоимости {money(b)}"
        return out(b, b, b, txt, txt, txt)
    if rule == "shipment":
        per = _num(f.get("limit_per_shipment"))
        if per is None:
            per = S
            asm.append({"code": "as_tpl_shipment", "params": {}})
        per = min(per, S)
        acc = _num(f.get("accumulation_value"))
        if acc is None:
            asm.append({"code": "as_tpl_accumulation", "params": {}})
        mfl = max(per, acc) if acc is not None else S
        return out(per, per, mfl, f"одна отправка {money(per)}", f"одна отправка {money(per)}",
                   f"накопление {money(mfl)}" if acc is not None else f"страховая сумма {money(S)}")
    if rule in ("limit", "full_limit"):
        # убыток по договору не больше страховой суммы: лимит выше неё — берётся страховая сумма (с пометкой)
        per = _num(f.get("limit_per_case"))
        if per is None:
            per = S
            asm.append({"code": "as_tpl_limit_case", "params": {}})
        elif per > S:
            asm.append({"code": "as_tpl_limit_case_over", "params": {"limit": round(per), "sum": round(S)}})
            per = S
        if rule == "full_limit":
            txt = f"полный лимит {money(per)}"
            return out(per, per, per, txt, txt, txt)
        agg = _num(f.get("limit_aggregate"))
        if agg is None:
            agg = S
            asm.append({"code": "as_tpl_limit_aggregate", "params": {}})
        elif agg > S:
            asm.append({"code": "as_tpl_limit_aggregate_over", "params": {"limit": round(agg), "sum": round(S)}})
            agg = S
        pml = min(per, agg)
        return out(pml, pml, max(per, agg), f"лимит на случай {money(pml)}", f"лимит на случай {money(pml)}",
                   f"годовой лимит {money(max(per, agg))}")
    if rule in ("full_sum", "credit"):
        txt = f"полная страховая сумма {money(S)}"
        if rule == "credit":
            c, k = _num(f.get("credit_amount")), f.get("collateral_value")
            k = float(k) if isinstance(k, (int, float)) and not isinstance(k, bool) and k >= 0 else None
            if c and k is not None:
                ci = credit_insurable(c, k, float(p.get("max_share_of_loan", 0.5)))
                over = S > ci["insurable"] + 0.5
                checks.append({"code": "tpl_credit_over" if over else "tpl_credit_ok",
                               "params": {"credit": round(c), "collateral": round(k),
                                          "insurable": round(ci["insurable"]), "by": ci["by"], "sum": round(S),
                                          "excess": round(S - ci["insurable"]) if over else 0}})
            else:
                asm.append({"code": "as_tpl_credit_unknown", "params": {}})
        return out(S, S, S, txt, txt, txt)
    if rule == "bi":
        m, r = _num(f.get("monthly_loss")), _num(f.get("recovery_months"))
        if m and r:
            loss = min(S, m * r)
            fp = f"{money(m)} в месяц × {r:g} мес. = {money(loss)}" + (" (не больше страховой суммы)"
                                                                        if m * r > S else "")
        else:
            loss = S
            fp = f"страховая сумма {money(S)}"
            asm.append({"code": "as_tpl_bi", "params": {}})
        return out(loss, loss, S, fp, fp, f"весь период ответственности {money(S)}")
    if rule == "dispute":
        per = _num(f.get("limit_per_dispute"))
        if per is None:
            per = S
            asm.append({"code": "as_tpl_dispute", "params": {}})
        per = min(per, S)
        return out(per, per, S, f"лимит на спор {money(per)}", f"лимит на спор {money(per)}",
                   f"лимит расходов {money(S)}")
    if rule == "crop":
        cv = crop_value(f.get("area_ha"), f.get("avg_yield_5y"), f.get("unit_price"))
        if cv is not None:
            W = cv["value"]
            over = S > W + 0.5
            checks.append({"code": "tpl_crop_over" if over else ("tpl_crop_under" if S < W - 0.5 else "tpl_crop_ok"),
                           "params": {"area": cv["area_ha"], "yield": cv["avg_yield_5y"], "price": round(cv["unit_price"]),
                                      "value": round(W), "sum": round(S), "excess": round(S - W) if over else 0,
                                      "pct": round(S / W * 100, 1) if W else None}})
            wtxt = (f"стоимость урожая {cv['area_ha']:g} га × {cv['avg_yield_5y']:g} ц/га × {money(cv['unit_price'])}"
                    f" = {money(W)}")
        else:
            W = V
            asm.append({"code": "as_tpl_crop_value", "params": {}})
            wtxt = f"стоимость объекта {money(W)}"
        base = min(S, W)
        btxt = f"min(страховая сумма {money(S)}; {wtxt})" if abs(S - W) > 0.5 else f"страховая сумма {money(S)}"
        pa, pl = float(p.get("pml_area_share", 0.3)), float(p.get("pml_loss_ratio", 0.5))
        ea, el = float(p.get("eml_area_share", 0.6)), float(p.get("eml_loss_ratio", 1.0))
        whole = lambda x: int(round(x * 100)) if abs(x * 100 - round(x * 100)) < 1e-9 else round(x * 100, 1)
        asm.append({"code": "as_tpl_crop_shares", "params": {"pa": whole(pa), "pl": whole(pl),
                                                             "ea": whole(ea), "el": whole(el)}})
        pml, eml = base * pa * pl, base * ea * el
        return out(pml, eml, base,
                   f"{btxt} × {pa:g} площади × {pl:g} потери = {money(pml)}",
                   f"{btxt} × {ea:g} площади × {el:g} гибели = {money(eml)}",
                   f"полная гибель на всей площади: {btxt} = {money(base)}")
    raise ValueError("неизвестное правило сценария: " + str(rule))


def crop_value(area_ha, avg_yield_5y, unit_price) -> Optional[dict]:
    """
    Страховая стоимость урожая (вариант 16у, шаблон класса): площадь, га × средняя урожайность за 5 лет, ц/га ×
    цена единицы продукции, сум за центнер. Чистая функция; не хватает числа (или оно не больше нуля) — None.
    Пример: 100 га × 30 ц/га × 400 000 сум/ц = 1 200 000 000 сум.
    """
    a, y, c = _num(area_ha), _num(avg_yield_5y), _num(unit_price)
    if a is None or y is None or c is None:
        return None
    return {"area_ha": a, "avg_yield_5y": y, "unit_price": c, "harvest_c": a * y, "value": a * y * c,
            "formula": "площадь × средняя урожайность за 5 лет × цена", "calibrated": CALIBRATED}


def template_scenarios(ctx: dict, cls: str, S: float, V: float, template: dict, fields: Optional[dict]) -> dict:
    """Сценарии по простому правилу шаблона класса (класс без правила в risk_analytics)."""
    rule = (template.get("scenario_rule") or {})
    code = rule.get("code")
    base = {"available": False, "reason": None, "class_code": cls, "rule": code, "items": {},
            "retention": None, "assumptions": [], "calibrated": CALIBRATED, "order": SCENARIO_ORDER,
            "source": "template", "rule_text": rule.get("text"), "rule_simple": rule.get("simple_rule")}
    try:
        res = simple_scenarios(code, S, V, fields, rule.get("params"))
    except (ValueError, TypeError, ZeroDivisionError):
        base["reason"] = "sc_na_class"
        return base
    what = rule.get("what") or {}
    items = {}
    for s in ("PML", "EML", "MFL"):
        amt = res[s]["amount"]
        items[s] = {"amount": amt, "pct": round(amt / S * 100, 1) if S else None, "what": "sc_w_tpl",
                    "what_params": {}, "what_text": dict(what.get(s) or {}), "state": None,
                    "formula": res[s]["formula"], "level": None, "source_scenario": s}
    an = ctx.get("analysis") if ctx.get("ok") else None
    base.update(available=True, items=items,
                order_ok=items["PML"]["amount"] <= items["EML"]["amount"] <= items["MFL"]["amount"],
                retention=_retention_block(an, items["EML"]["amount"], items["MFL"]["amount"]),
                assumptions=list(res["assumptions"]), checks=list(res["checks"]), k=1.0,
                params=dict(rule.get("params") or {}))
    return base


def scenarios(ctx: dict, cls: str, S: float, template: Optional[dict] = None, V: Optional[float] = None,
              fields: Optional[dict] = None) -> dict:
    """Сценарии из risk_analytics.analyze (классы 3, 8, 9). Класс без правила в модуле — простое правило шаблона
    класса (template_scenarios); шаблона нет — «сценарий не считается»."""
    base = {"available": False, "reason": None, "class_code": cls, "rule": None, "items": {},
            "retention": None, "assumptions": [], "calibrated": CALIBRATED, "order": SCENARIO_ORDER}
    if cls not in RULE_CLASSES:
        code = ((template or {}).get("scenario_rule") or {}).get("code")
        from .class_templates import TEMPLATE_RULES
        if template and code in TEMPLATE_RULES:
            return template_scenarios(ctx, cls, S, V or S, template, fields)
        base["reason"] = "sc_na_class"
        return base
    if not ctx.get("ok"):
        base["reason"] = "sc_na_error"
        base["error"] = ctx.get("error")
        return base
    an = ctx["analysis"]
    op = ctx["optional"]
    rule = "vehicle" if cls in ra.VEHICLE_CLASSES else ("property8" if cls == "8" else "property9")
    items = {}
    for s in ("PML", "EML", "MFL"):
        src = ACT_FROM_RA[s]
        sc = an["scenarios"][src]
        what, wparams, state = _what(rule, s, sc, op)
        formula = sc.get("formula")
        if formula:                       # формула модуля начинается с его названия — пишем название акта
            formula = re.sub(r"^(EML|PML|MFL) =", s + " =", formula)
        items[s] = {"amount": sc["amount"], "pct": round(sc["amount"] / S * 100, 1) if S else None,
                    "what": what, "what_params": wparams, "state": state, "formula": formula,
                    "level": sc.get("level"), "source_scenario": src}
    order_ok = items["PML"]["amount"] <= items["EML"]["amount"] <= items["MFL"]["amount"]
    ret = an.get("retention") or {}
    known = ret.get("retention_limit") is not None
    cat = (an["scenarios"]["MFL"].get("catastrophe") or {})
    status = {"по отчётности": "reported", "временно": "temporary"}.get(ret.get("status"), "unknown")
    limit = ret.get("retention_limit")
    eml = items["EML"]["amount"]
    retention = {"known": known, "limit": limit, "limit_per_risk": ret.get("limit_per_risk"),
                 "line_retention": ret.get("line_retention"), "line_class": ret.get("line_class"),
                 "own_funds": ret.get("own_funds"), "reserves": ret.get("reserves"), "status": status,
                 # удержание сравнивается с EML (решение заказчика 21.09.2026); MFL — отдельной справкой
                 "compared_with": "eml",
                 "eml_excess": round(max(eml - limit, 0)) if known else None,
                 "within": (eml <= limit) if known else None,
                 "mfl_excess": cat.get("reinsurance_need") if known else None,
                 "legal_ref": ret.get("legal_ref") or "Положение № 1806, п. 15"}
    extra = list(ctx.get("assumptions") or [])
    if cls in RULE_CLASSES and not op.get("protection"):
        # у техники от защиты зависит EML акта (PML модуля) — отсюда отдельная формулировка
        extra.append({"code": "as_protection_veh_eml" if rule == "vehicle" else "as_protection_prop", "params": {}})
    if rule != "vehicle" and not op.get("compartments"):
        extra.append({"code": "as_compartments", "params": {}})
    if rule == "property8" and op.get("seismic_zone") is None:
        extra.append({"code": "as_seismic", "params": {}})
    base.update(available=True, rule=rule, items=items, retention=retention, assumptions=extra, order_ok=order_ok,
                k=round(min(S, float(an["summary"]["object_value"])) / float(an["summary"]["object_value"]), 4))
    return base


# ================================================================================================
#  2. Франшиза: размер, эффект, применение
# ================================================================================================

def _floor_rate(rate_res: dict) -> float:
    return float(rate_res.get("min_pct") or 0.0)


def apply_multiplier(rate_res: dict, mult: float, S: float) -> tuple:
    """
    Ставка и премия акта с множителем, не ниже минимума. Премия считается от неокруглённой ставки
    (ставка акта × множитель), показываемая ставка округляется до 4 знаков — иначе премия расходится с
    множителем на округление ставки (0,5 % франшизы: 216 534 374, а не 216 485 340 сум).
    """
    applied = rate_res.get("applied_pct")
    if applied is None:
        return None, None, False
    raw = float(applied) * mult
    floor = _floor_rate(rate_res)
    floored = raw + 1e-12 < floor
    rate = floor if floored else raw
    prem = round(engine.premium_of(rate, S, rate_res["term_days"]))
    return round(rate, 4), prem, floored


def _engine_multiplier(con, ctx: dict, pct: float) -> dict:
    """Множитель франшизы из franchise.what_if: премия старого движка с франшизой / без неё."""
    if not ctx.get("ok"):
        return {"ok": False, "error": ctx.get("error")}
    try:
        w = frm.what_if(con, ctx["must"], ctx["optional"], pct, analysis=ctx["analysis"])
    except Exception as e:
        return {"ok": False, "error": type(e).__name__}
    if not w.get("ok"):
        return {"ok": False, "error": "what_if"}
    base = float(w.get("base_premium") or 0)
    prem = float(w.get("premium") or 0)
    mult = round(prem / base, 6) if base > 0 else 1.0
    return {"ok": True, "mult": min(mult, 1.0), "base_premium": w.get("base_premium"), "premium": w.get("premium"),
            "cap_pct": w.get("cap_pct"), "extrapolated": pct > 2}


def franchise(con, ctx: dict, fr: dict, rate_res: dict, *, cls: str, S: float, level: str, statutory: bool,
              requested: Optional[dict], th: dict) -> dict:
    """
    Расширяет результат act_engine.franchise (основания акта). Статусы:
      none — оснований нет, франшиза не требуется (по умолчанию);
      proposed — основание есть: размер из вилки уровня (franchise._pick), эффект посчитан, в премию
                 акта не включён — решение андеррайтера;
      applied — сотрудник применил свою франшизу (optional.deductible): премия акта пересчитана;
      statutory — обязательный вид: франшиза не применяется, запрос сотрудника (если был) не исполнен.
    """
    out = dict(fr)
    P = rate_res.get("premium")
    out.update(status="none", applied=False, applied_by=None, type=None, size_pct=0.0, size_amount=0,
               cap_pct=None, premium_before=P, premium_after=P, delta=0 if P is not None else None,
               delta_pct=0.0 if P is not None else None, rate_before=rate_res.get("applied_pct"),
               rate_after=rate_res.get("applied_pct"), multiplier=1.0, floor_applied=False, engine=None,
               how=[], alternatives=[], warning=None, requested=requested, calibrated=CALIBRATED, error=None)
    cap, _why = frm._cap([cls], th)
    out["cap_pct"] = cap
    if statutory:
        out["status"] = "statutory"
        out["how"].append({"code": "frh_statutory", "params": {}})
        if requested:
            out["warning"] = {"code": "fr_w_statutory", "params": {}}
        return out

    pct, ftype = None, None
    if requested:
        pct = float(requested["pct"]) if requested.get("pct") is not None else \
            float(requested["amount"]) / S * 100 if S else None
        ftype = requested.get("type") or "unconditional"
        out.update(status="applied", applied=True, applied_by="employee")
        out["how"].append({"code": "frh_employee", "params": {"pct": round(pct, 4), "type": ftype,
                                                              "amount": round(S * pct / 100)}})
        if cap is not None and pct > cap + 1e-9:
            out["warning"] = {"code": "fr_w_cap", "params": {"pct": round(pct, 4), "cap": cap}}
    elif fr.get("needed"):
        codes = [g["code"] for g in fr.get("grounds") or []]
        size = fr.get("size")
        ftype = "peril" if codes == ["fr_g_dominant"] else "unconditional"
        out["status"] = "proposed"
        if size:
            lo, hi = float(size["from_pct"]), float(size["to_pct"])
            losses = frm._losses(ctx.get("optional") or {}, S, th) if ctx.get("optional") is not None \
                else frm._losses({}, S, th)
            ra_level = {"low": "Низкий", "moderate": "Умеренный", "high": "Высокий"}[level]
            if ftype == "peril":
                pct = frm._peril_pct([lo, hi], cap)
                out["how"].append({"code": "frh_peril_size", "params": {"pct": pct}})
            else:
                pct, _w = frm._pick([lo, hi], ra_level, losses, th)
                if pct <= 0 and hi > 0:
                    pct = hi               # франшиза 0 % основание не исполняет — верх вилки (как в franchise.py)
                pct = min(max(pct, lo), hi)
                out["how"].append({"code": "frh_size", "params": {"from": lo, "to": hi, "pct": pct,
                                                                  "losses": losses.get("count")}})
        else:
            out["how"].append({"code": "frh_nosize", "params": {}})
    else:
        out["how"].append({"code": "frh_none", "params": {"premium": P}})
        return out

    out["type"] = ftype
    if pct is None:
        return out
    out["size_pct"] = round(pct, 4)
    out["size_amount"] = round(S * pct / 100)
    if ftype == "peril":
        # доля отдельного риска в ставке лёгкой версии неизвестна — эффект не выдумываем
        out["how"].append({"code": "frh_peril_na", "params": {}})
        return out
    em = _engine_multiplier(con, ctx, pct)
    if not em.get("ok"):
        out["error"] = em.get("error")
        out["how"].append({"code": "frh_effect_na", "params": {}})
        return out
    mult = em["mult"]
    out["multiplier"] = mult
    out["engine"] = {k: em[k] for k in ("base_premium", "premium", "extrapolated")}
    out["how"].append({"code": "frh_mult", "params": {"pct": round(pct, 4), "base": em["base_premium"],
                                                      "with": em["premium"], "mult": mult}})
    if em.get("extrapolated"):
        out["how"].append({"code": "frh_extrapolated", "params": {}})
    if ftype == "conditional":
        out["how"].append({"code": "frh_conditional", "params": {}})
    rate, prem, floored = apply_multiplier(rate_res, mult, S)
    out["floor_applied"] = floored
    if rate is None:
        out["how"].append({"code": "frh_no_premium", "params": {}})
        return out
    out.update(rate_after=rate, premium_after=prem, delta=prem - P,
               delta_pct=round((prem - P) / P * 100, 1) if P else 0.0)
    out["how"].append({"code": "frh_apply", "params": {"rate": rate_res["applied_pct"], "mult": mult,
                                                       "raw": round(float(rate_res["applied_pct"]) * mult, 4),
                                                       "rate_after": rate, "premium": prem}})
    if floored:
        out["how"].append({"code": "frh_floor", "params": {"min": rate_res.get("min_pct")}})
    if out["status"] == "proposed":
        out["how"].append({"code": "frh_not_applied", "params": {}})
    return out


def alternatives(ctx: dict, rate_res: dict, S: float, meas: dict, th: dict) -> list:
    """
    Вместо франшизы: мероприятия, исключение риска и сумма к стоимости (franchise._alternatives).
    Все варианты считаются от одной базы — премии акта БЕЗ франшизы (rate_res.premium), даже если
    франшиза применена: premium — премия с этим вариантом вместо франшизы, premium_delta — к этой базе.
    meas — блок measures (items с ratio и total), его итог пересчитывается от базы заново.
    """
    P = rate_res.get("premium")
    out = []
    items = (meas or {}).get("items") or []
    total = (meas or {}).get("total") or {}
    if items:
        d = 0
        effective = [it for it in items if it.get("ratio")]
        if effective and P and not total.get("statutory"):
            fake = {"summary": {"premium": P, "rate_min_pct": rate_res.get("min_pct"), "sum_insured": S,
                                "term_days": rate_res.get("term_days") or 365}}
            d = round(frm._measures_delta(fake, [{"premium_delta": P * (it["ratio"] - 1)} for it in effective]))
        if d:
            out.append({"code": "alt_measures", "params": {"premium": P + d, "delta": d, "base": P},
                        "premium": P + d, "premium_delta": d, "base_premium": P})
        else:
            out.append({"code": "alt_measures_free", "params": {"base": P}, "premium": P,
                        "premium_delta": 0 if P is not None else None, "base_premium": P})
    if not ctx.get("ok"):
        return out
    an = ctx["analysis"]
    base = float(an["summary"]["premium"] or 0)
    try:
        alts = frm._alternatives(an, None, th)
    except Exception:
        return out
    for a in alts:
        if a["code"] == "measures" or not base:
            continue
        mult = 1 + float(a["premium_delta"]) / base
        rate, prem, _f = apply_multiplier(rate_res, mult, S) if P is not None else (None, None, False)
        if a["code"] == "exclude_peril":
            code = "alt_exclude_eq" if "землетряс" in str(a.get("what") or "").lower() else "alt_exclude"
            delta = (prem - P) if prem is not None and P is not None else None
            out.append({"code": code, "params": {"premium": prem, "delta": delta, "base": P},
                        "premium": prem, "premium_delta": delta, "base_premium": P})
        elif a["code"] == "sum_to_value":
            V = float(an["summary"]["object_value"])
            # сумма меняется — премия акта пересчитывается от новой суммы по той же ставке
            prem2 = round(engine.premium_of(rate_res["applied_pct"], V, rate_res["term_days"])) \
                if rate_res.get("applied_pct") is not None else None
            delta = (prem2 - P) if prem2 is not None and P is not None else None
            out.append({"code": "alt_sum_up" if S < V else "alt_sum_down",
                        "params": {"sum": S, "value": V, "premium": prem2, "delta": delta, "base": P},
                        "premium": prem2, "premium_delta": delta, "base_premium": P,
                        "legal_ref": a.get("legal_ref")})
    return out


# ================================================================================================
#  3. Рекомендации страхователю (предупредительные мероприятия)
# ================================================================================================

_catalog_cache = {"mtime": None, "data": {}}


def measures_catalog() -> dict:
    try:
        m = MEASURES_FILE.stat().st_mtime
        if _catalog_cache["mtime"] != m:
            _catalog_cache["data"] = json.loads(MEASURES_FILE.read_text(encoding="utf-8"))
            _catalog_cache["mtime"] = m
    except (OSError, ValueError) as e:
        print("акт: список мероприятий не прочитан:", type(e).__name__)
        return {}
    return _catalog_cache["data"]


def _base_input(ref, ctx: dict, cls: str, S: float, V: float, th: dict):
    """Input движка на тех же данных, что analyze (для эффекта экспертных мероприятий)."""
    if not ctx.get("ok"):
        return None
    m, op = ctx["must"], ctx["optional"]
    factors, _n = ra.engine_factors([cls], m, op, th, date.today())
    prod, _note = ra._product_for(cls, m, ref)
    return engine.Input(product_code=prod or "", class_code=cls, object_type=m.get("object_type") or "",
                        value_amount=V, sum_insured=S, term_days=365, factors=factors)


def _factor_ratio(ref, inp, effect: dict) -> Optional[float]:
    if inp is None or not effect or str(effect.get("class")) != inp.class_code:
        return None
    f, opt = effect.get("factor"), effect.get("option")
    if f not in engine.factors_for(ref, inp) or (f, opt) not in ref.coefficients or inp.factors.get(f) == opt:
        return None
    alt = engine.Input(**{**inp.__dict__})
    alt.factors = {**inp.factors, f: opt}
    alt.applied_rate_pct = None
    g0 = engine.rate_for(ref, inp)["gross_pct"]
    g1 = engine.rate_for(ref, alt)["gross_pct"]
    if not g0:
        return None
    r = g1 / g0
    return round(r, 6) if r < 0.9999 else None


def _when_ok(when: dict, facts: dict) -> bool:
    if not when:
        return True
    if when.get("any_of"):
        return any(_when_ok(w, facts) for w in when["any_of"])
    if "location" in when and facts.get("location") not in when["location"]:
        return False
    if "location_not" in when and facts.get("location") in when["location_not"]:
        return False                       # гараж, охраняемая стоянка — совет про стоянку не нужен
    if when.get("not_guarded") and facts.get("guard"):
        return False
    if "kinds" in when and facts.get("kind") not in when["kinds"]:
        return False
    if "classes" in when and facts.get("cls") not in when["classes"]:
        return False
    if "protection_not" in when and facts.get("protection") in when["protection_not"]:
        return False
    if when.get("protection_unknown") and facts.get("protection"):
        return False
    if "losses_min" in when and (facts.get("losses") or 0) < int(when["losses_min"]):
        return False
    return True


def _tr(tr: dict, code: str, lang: str, what: str) -> Optional[str]:
    """Перевод мероприятия таблицы preventive_measures из docs/act_measures.json (русский — в базе)."""
    return ((tr.get(code) or {}).get(lang) or {}).get(what)


def measures(con, ctx: dict, rate_res: dict, *, cls: str, group: str, kind: Optional[str], S: float, V: float,
             o: dict, location: Optional[str], statutory: bool, th: dict, premium: Optional[float],
             codes: Optional[list] = None) -> dict:
    """
    Мероприятия: таблица preventive_measures (через reducers risk_analytics — те же, что в старом
    движке) плюс экспертный список docs/act_measures.json. codes — мероприятия шаблона класса для группы
    объекта (app/class_templates.py): из списка берутся ровно они (условия when по-прежнему действуют);
    codes = None — прежний выбор по группе объекта. Эффект — множитель движка к ставке акта; несколько скидок
    перемножаются и премия не ниже минимальной ставки продукта (franchise._measures_delta). premium — премия
    акта, к которой относится эффект.
    """
    ref = db.load_reference(con)
    cat = measures_catalog()
    tr = cat.get("translations") or {}
    items, seen = [], set()
    note_days = int(th.get("franchise_measures_note_days", 90))
    an = ctx.get("analysis") if ctx.get("ok") else None
    an_base = float(an["summary"]["premium"] or 0) if an else 0.0
    for r in (an or {}).get("reducers") or []:
        if r.get("kind") != "мероприятие":
            continue
        row = next((m for m in ref.measures if m["measure"] == r["text"] and m["class_code"] == cls), None)
        code = row["code"] if row else "pm_%d" % len(items)
        if code in seen:
            continue
        seen.add(code)
        ratio = round(1 + float(r["premium_delta"]) / an_base, 6) \
            if r.get("premium_delta") and an_base and r["premium_delta"] < 0 else None
        items.append({"code": code, "source": "preventive_measures",
                      "text": {"ru": r["text"], **{lg: _tr(tr, code, lg, "text") for lg in ("uz", "en")}},
                      "why": {"ru": r.get("why"), **{lg: _tr(tr, code, lg, "why") for lg in ("uz", "en")}},
                      "ratio": ratio, "mandatory": bool(r.get("mandatory")),
                      "deadline_days": r.get("deadline_days") or (row or {}).get("deadline_days") or note_days,
                      "deadline_default": not (r.get("deadline_days") or (row or {}).get("deadline_days")),
                      "legal_ref": None})
    inp = _base_input(ref, ctx, cls, S, V, th)
    facts = {"location": location, "guard": o.get("guard"), "kind": kind, "cls": cls,
             "protection": o.get("protection"), "losses": o.get("losses_count")}
    wanted = set(codes) if codes is not None else None
    for c in cat.get("catalog") or []:
        chosen = (c["code"] in wanted) if wanted is not None else (group in (c.get("groups") or []))
        if not chosen or c["code"] in seen or not _when_ok(c.get("when") or {}, facts):
            continue
        seen.add(c["code"])
        mandatory = c.get("mandatory_over") is not None and S >= float(c["mandatory_over"])
        items.append({"code": c["code"], "source": "catalog",
                      "text": {lg: (c.get(lg) or {}).get("text") for lg in ("ru", "uz", "en")},
                      "why": {lg: (c.get(lg) or {}).get("why") for lg in ("ru", "uz", "en")},
                      "ratio": _factor_ratio(ref, inp, c.get("effect")), "mandatory": mandatory,
                      "deadline_days": c.get("deadline_days") or note_days,
                      "deadline_default": not c.get("deadline_days"), "legal_ref": c.get("legal_ref")})
    items.sort(key=lambda x: (not x["mandatory"], x["ratio"] if x["ratio"] is not None else 1.0))
    items = items[:MAX_MEASURES]

    # эффект на премию акта: каждая скидка — отдельно, итог — перемножением и не ниже минимума
    floor = rate_res.get("min_pct")
    fake = {"summary": {"premium": premium or 0, "rate_min_pct": floor, "sum_insured": S,
                        "term_days": rate_res.get("term_days") or 365}}
    for it in items:
        it["effect_pct"] = round((it["ratio"] - 1) * 100, 1) if it["ratio"] else None
        if it["ratio"] and premium and not statutory:
            it["premium_delta"] = round(frm._measures_delta(fake, [{"premium_delta": premium * (it["ratio"] - 1)}]))
        else:
            it["premium_delta"] = None
        it["calibrated"] = CALIBRATED
    with_effect = [it for it in items if it["premium_delta"]]
    total = {"count": len(items), "with_effect": len(with_effect), "premium_before": premium,
             "premium_after": premium, "delta": 0 if premium else None, "floor_applied": False,
             "statutory": statutory, "calibrated": CALIBRATED}
    if with_effect and premium:
        d = frm._measures_delta(fake, [{"premium_delta": premium * (it["ratio"] - 1)} for it in with_effect])
        total["delta"] = round(d)
        total["premium_after"] = round(premium + d)
        naive = premium
        for it in with_effect:
            naive *= it["ratio"]
        total["floor_applied"] = floor is not None and round(naive) < total["premium_after"]
    return {"items": items, "total": total}
