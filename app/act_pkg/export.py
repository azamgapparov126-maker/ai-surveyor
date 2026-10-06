"""Выгрузка акта: Word (app/docx_lite.py) и PDF (pymupdf) на один лист A4, страница и картинка страхового скоринга.

С 06.10.2026 документ — краткий акт заказчика (app/act_pkg/compact.py): шапка и пять разделов, шрифт 10 pt, поля
15 мм, компактные таблицы. Внутренние пояснения, источники, формулы, статистика и скоринг-картинка в документ не
идут — они остаются в JSON и на экране; страница скоринга отдельно — GET /act/{id}/scoring.pdf и scoring.png.
Одиночный объект помещается на один лист; парк ТС и комплексный продукт с таблицей объектов — допускается второй.
"""
from pathlib import Path

import pymupdf

from .. import act_scoring as asc
from ..act_texts import t
from ..docx_lite import Docx
from ..proposal import A4_H, A4_W, BLACK, GRAY, LINE, FILL

from .compact import compact_view

BRAND = (0.11, 0.17, 0.56)                 # синий INSON #1D2C8F
MARGIN_C = 15 / 25.4 * 72                  # 15 мм в пунктах
CONTENT_C = A4_W - 2 * MARGIN_C
BODY = 10                                  # основной шрифт, pt
LEAD = 1.18                                # межстрочный интервал
DOCX_MARGIN = 850                          # 15 мм в twip
DOCX_WIDTH = 11906 - 2 * DOCX_MARGIN


# --------------------------------------------------------------------------- #
#  Картинка скоринга (для scoring.png; в акт больше не входит)
# --------------------------------------------------------------------------- #

def scoring_png(act: dict) -> bytes:
    """Картинка шкалы скоринга с плашкой класса (тот же код рисования, что у страницы PDF)."""
    regular, bold = _fonts()
    fit = lambda s, font: "".join(ch if ord(ch) < 128 or font.has_glyph(ord(ch)) else GLYPH_FALLBACK.get(ch, "?")  # noqa
                                  for ch in str(s))
    return asc.gauge_png(act["scoring"], regular, bold, fit)


# --------------------------------------------------------------------------- #
#  Word
# --------------------------------------------------------------------------- #

def build_docx(act: dict) -> bytes:
    """Акт (ответ render) → DOCX на один лист: шапка, пять разделов, четыре блока раздела 4, подпись."""
    lang = act["lang"]
    V = compact_view(act)
    doc = Docx(lang={"ru": "ru-RU", "uz": "uz-Latn-UZ", "en": "en-GB"}[lang], margin=DOCX_MARGIN, base_size=20,
               line=240)
    doc.parts.append(_dpara(V["title"].upper(), bold=True, size=26, color="1D2C8F", after=20, align="center"))
    doc.parts.append(_dpara(V["head"], size=19, color="555555", after=60, align="center"))
    for s in V["sections"]:
        doc.parts.append(_dpara(f"{s['n']}. {s['title']}", bold=True, size=21, color="1D2C8F", before=80, after=30,
                                keep=True, border=True))
        if s["pairs"]:
            rows = _pairs4(s["pairs"])
            w = DOCX_WIDTH
            doc.table(rows, widths=[int(w * .2), int(w * .3), int(w * .2), w - int(w * .2) * 2 - int(w * .3)],
                      header=False, size=17, label_cols=(0, 2))
        for p in s["lines"]:
            doc.parts.append(_dpara(p, size=20, after=20))
        tb = s.get("table")
        if tb:
            doc.parts.append(_dpara(tb.get("title") or "", bold=True, size=19, after=20))
            wts = tb.get("widths") or [1] * len(tb["columns"])
            doc.table([list(tb["columns"])] + [[str(c) for c in r] for r in tb["rows"]],
                      widths=[int(DOCX_WIDTH * x / sum(wts)) for x in wts], header=True, size=16)
        for b in s["blocks"]:
            if b.get("inline"):
                for p in b.get("lines") or []:
                    doc.parts.append(_dpara(f"**{b['title']}:** {p}", size=20, after=10))
                continue
            doc.parts.append(_dpara(b["title"], bold=True, size=20, after=10, keep=True))
            for p in b.get("lines") or []:
                doc.parts.append(_dpara(p, size=19, after=10, indent=160))
            if b.get("bullets"):
                if b.get("bullet_cols"):
                    k = int(b["bullet_cols"])
                    if b.get("bullets_title"):
                        doc.parts.append(_dpara(b["bullets_title"] + ":", size=19, after=0, indent=160, keep=True))
                    cells = ["• " + x for x in b["bullets"]]
                    rows = [(cells[i:i + k] + [""] * k)[:k] for i in range(0, len(cells), k)]
                    doc.table(rows, widths=[DOCX_WIDTH // k] * k, header=False, size=19, borders=False)
                elif b.get("bullets_title"):
                    doc.parts.append(_dpara(b["bullets_title"] + ": " + "; ".join(b["bullets"]), size=19, after=10,
                                            indent=160))
                else:
                    for it in b["bullets"]:
                        doc.parts.append(_dpara("•\u00a0" + it, size=19, after=10, indent=160))
            for p in b.get("after") or []:
                doc.parts.append(_dpara(p, size=19, after=10, indent=160))
    doc.parts.append(_dpara(V["sign"], bold=True, italic=True, size=19, before=80, after=0, border_top=True))
    return doc.to_bytes()


def _pairs4(pairs: list) -> list:
    """Строки «подпись — значение» по две пары в строке таблицы (компактно)."""
    rows = []
    for i in range(0, len(pairs), 2):
        a = pairs[i]
        b = pairs[i + 1] if i + 1 < len(pairs) else ("", "")
        rows.append([a[0], a[1], b[0], b[1]])
    return rows


def _dpara(text, *, bold=False, italic=False, size=20, color=None, before=0, after=40, align=None, keep=False,
           indent=0, border=False, border_top=False) -> str:
    """Абзац WordprocessingML с нужными отступами (docx_lite._para без висячего отступа и с линией под заголовком)."""
    from ..docx_lite import _runs
    ppr = f'<w:spacing w:before="{int(before)}" w:after="{int(after)}" w:line="240" w:lineRule="auto"/>'
    if indent:
        ppr += f'<w:ind w:left="{int(indent)}"/>'
    if keep:
        ppr += "<w:keepNext/>"
    if border:
        ppr += '<w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" w:color="1D2C8F"/></w:pBdr>'
    if border_top:
        ppr += '<w:pBdr><w:top w:val="single" w:sz="6" w:space="2" w:color="B9BFD6"/></w:pBdr>'
    if align:
        ppr += f'<w:jc w:val="{align}"/>'
    return f"<w:p><w:pPr>{ppr}</w:pPr>{_runs(text, bold, size, color, italic)}</w:p>"


# --------------------------------------------------------------------------- #
#  PDF
# --------------------------------------------------------------------------- #

# Windows — Arial; сервер (Dockerfile ставит fonts-dejavu-core) — DejaVu Sans, в нём есть узбекская ʻ
FONT_CANDIDATES = (
    (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
)
GLYPH_FALLBACK = {"ʻ": "'", "ʼ": "'", "−": "-", "—": "-", "×": "x", "≤": "<=", "≥": ">=", "→": "->",
                  "«": '"', "»": '"', "…": "...", "\u00a0": " "}


def _fonts():
    for reg, bold in FONT_CANDIDATES:
        if Path(reg).exists() and Path(bold).exists():
            return pymupdf.Font(fontfile=reg), pymupdf.Font(fontfile=bold)
    # на сервере без шрифтов — встроенные Helvetica (кириллица в них есть, узбекской ʻ нет)
    return pymupdf.Font("helv"), pymupdf.Font("hebo")


class _Sheet:
    """Поток текста сверху вниз на A4 с полями 15 мм: абзацы, заголовки разделов, маркированные строки, таблицы."""

    def __init__(self, footer: str, lang: str):
        self.doc = pymupdf.open()
        self.regular, self.bold = _fonts()
        self.footer, self.lang = footer, lang
        self.page = None
        self.writers = {}
        self.y = 0.0
        self.new_page()

    def fit(self, s, font):
        return "".join(ch if ord(ch) < 128 or font.has_glyph(ord(ch)) else GLYPH_FALLBACK.get(ch, "?") for ch in str(s))

    def new_page(self):
        self._flush()
        self.page = self.doc.new_page(width=A4_W, height=A4_H)
        self.writers = {}
        self.y = MARGIN_C

    def _flush(self):
        if self.page is None:
            return
        for color, tw in self.writers.items():
            tw.write_text(self.page, color=color)
        self.writers = {}

    def _put(self, x, baseline, s, font, size, color):
        if color not in self.writers:
            self.writers[color] = pymupdf.TextWriter(self.page.rect)
        self.writers[color].append((x, baseline), self.fit(s, font), font=font, fontsize=size)

    def ensure(self, h: float):
        if self.y + h > A4_H - MARGIN_C - 10:      # запас под номер страницы
            self.new_page()

    def wrap(self, s: str, font, size: float, width: float) -> list:
        lines = []
        for raw in str(s).split("\n"):
            cur = ""
            for w in raw.split(" "):
                cand = (cur + " " + w).strip()
                if font.text_length(self.fit(cand, font), fontsize=size) <= width or not cur:
                    cur = cand
                else:
                    lines.append(cur)
                    cur = w
            lines.append(cur)
        return lines

    def para(self, s: str, size=BODY, bold=False, color=BLACK, gap=1.5, x=None, width=None, align="l"):
        font = self.bold if bold else self.regular
        x = MARGIN_C if x is None else x
        width = CONTENT_C - (x - MARGIN_C) if width is None else width
        lh = size * LEAD
        for ln in self.wrap(s, font, size, width):
            self.ensure(lh)
            tx = x
            if align == "c":
                tx = x + (width - font.text_length(self.fit(ln, font), fontsize=size)) / 2
            self._put(tx, self.y + size, ln, font, size, color)
            self.y += lh
        self.y += gap

    def heading(self, s: str):
        self.ensure(BODY * 4)
        self.y += 2
        self._put(MARGIN_C, self.y + 10.5, s, self.bold, 10.5, BRAND)
        self.y += 10.5 * LEAD + 1
        self.page.draw_line((MARGIN_C, self.y), (A4_W - MARGIN_C, self.y), color=BRAND, width=0.6)
        self.y += 2.5

    def bullet(self, s: str, size=BODY, indent=8):
        self.ensure(size * LEAD)
        self._put(MARGIN_C + indent, self.y + size, "•", self.regular, size, BLACK)
        self.para(s, size=size, x=MARGIN_C + indent + 8, gap=0.5)

    def columns(self, title, items: list, k: int, size=BODY):
        """Маркированный список в k колонок: «Опасности:» и пункты по строкам (компактно, без рамок)."""
        x0 = MARGIN_C + 8
        tw = 0.0
        if title:
            s = title + ":"
            self.ensure(size * LEAD)
            self._put(x0, self.y + size, s, self.regular, size, BLACK)
            tw = self.regular.text_length(self.fit(s, self.regular), fontsize=size) + 6
        colw = (CONTENT_C - 8 - tw) / k
        for i in range(0, len(items), k):
            wrapped = [self.wrap(it, self.regular, size, colw - 10) for it in items[i:i + k]]
            h = max(len(w) for w in wrapped) * size * LEAD
            self.ensure(h)
            for j, lines in enumerate(wrapped):
                x = x0 + tw + j * colw
                self._put(x, self.y + size, "•", self.regular, size, BLACK)
                for n, ln in enumerate(lines):
                    self._put(x + 7, self.y + size + n * size * LEAD, ln, self.regular, size, BLACK)
            self.y += h
        self.y += 0.5

    def table(self, cols: list, rows: list, size=9, header=False, label_cols=()):
        """cols: [ширина]; rows: списки строк. Компактно: отступ 2,5 pt, тонкие линии."""
        lh = size * 1.18
        pad = 2.5

        def draw(cells, font, color, fill=None):
            wrapped = [self.wrap(c, font, size, w - 2 * pad) for c, w in zip(cells, cols)]
            h = max(len(w) for w in wrapped) * lh + 2 * pad
            self.ensure(h)
            if fill:
                self.page.draw_rect(pymupdf.Rect(MARGIN_C, self.y, A4_W - MARGIN_C, self.y + h), color=None, fill=fill)
            x = MARGIN_C
            for ci, (lines, w) in enumerate(zip(wrapped, cols)):
                for i, ln in enumerate(lines):
                    self._put(x + pad, self.y + pad + size + i * lh - 1, ln, font,
                              size, GRAY if ci in label_cols and fill is None else color)
                x += w
            self.y += h
            self.page.draw_line((MARGIN_C, self.y), (A4_W - MARGIN_C, self.y), color=LINE, width=0.4)

        if header:
            draw(rows[0], self.bold, GRAY, fill=FILL)
            rows = rows[1:]
        for r in rows:
            draw([str(c) for c in r], self.regular, BLACK)
        self.y += 3

    def finish(self) -> bytes:
        self._flush()
        n = self.doc.page_count
        for i, page in enumerate(self.doc, start=1):
            tw = pymupdf.TextWriter(page.rect)
            s = self.fit(t("page", self.lang, i=i, n=n), self.regular)
            w = self.regular.text_length(s, fontsize=7.5)
            tw.append((A4_W - MARGIN_C - w, A4_H - MARGIN_C + 12), s, font=self.regular, fontsize=7.5)
            tw.append((MARGIN_C, A4_H - MARGIN_C + 12), self.fit(self.footer, self.regular)[:110], font=self.regular,
                      fontsize=7.5)
            tw.write_text(page, color=GRAY)
        self.doc.subset_fonts()
        return self.doc.tobytes(garbage=3, deflate=True)


def build_pdf(act: dict, scoring_only: bool = False) -> bytes:
    """Акт в PDF на один лист A4 (пять разделов заказчика). scoring_only — только страница скоринга
    (GET /act/{id}/scoring.pdf): в сам акт скоринг-картинка больше не входит."""
    lang = act["lang"]
    if scoring_only:
        pdf = _Sheet(act["footer"], lang)
        if (act.get("scoring") or {}).get("available"):
            asc.draw_page(pdf.page, act["scoring"], pdf.regular, pdf.bold, pdf.fit, 20 / 25.4 * 72,
                          A4_H - 20 / 25.4 * 72 - 4)
        return pdf.finish()
    V = compact_view(act)
    pdf = _Sheet(V["sign"], lang)
    pdf.para(V["title"].upper(), size=13, bold=True, color=BRAND, gap=1, align="c")
    pdf.para(V["head"], size=9.5, color=GRAY, gap=2, align="c")
    for s in V["sections"]:
        pdf.heading(f"{s['n']}. {s['title']}")
        if s["pairs"]:
            w = CONTENT_C
            pdf.table([w * .2, w * .3, w * .2, w * .3], _pairs4(s["pairs"]), size=8.5, label_cols=(0, 2))
        for p in s["lines"]:
            pdf.para(p, size=BODY)
        tb = s.get("table")
        if tb:
            pdf.para(tb.get("title") or "", size=9.5, bold=True, gap=1)
            wts = tb.get("widths") or [1] * len(tb["columns"])
            pdf.table([CONTENT_C * x / sum(wts) for x in wts], [list(tb["columns"])] + [[str(c) for c in r]
                                                                                       for r in tb["rows"]],
                      size=8, header=True)
        for b in s["blocks"]:
            pdf.ensure(BODY * LEAD * 2)
            if b.get("inline"):
                # «Решение: Принять: …» — заголовок блока и текст в одной строке
                head = b["title"] + ": "
                pdf._put(MARGIN_C, pdf.y + BODY, head, pdf.bold, BODY, BLACK)
                hw = pdf.bold.text_length(pdf.fit(head, pdf.bold), fontsize=BODY)
                for p in b.get("lines") or []:
                    pdf.para(p, size=BODY, x=MARGIN_C + hw, gap=0.5)
                pdf.y += 1.5
                continue
            pdf.para(b["title"], size=BODY, bold=True, gap=0.5)
            for p in b.get("lines") or []:
                pdf.para(p, size=BODY, x=MARGIN_C + 8, gap=0.5)
            if b.get("bullets"):
                if b.get("bullet_cols"):
                    pdf.columns(b.get("bullets_title"), b["bullets"], int(b["bullet_cols"]))
                elif b.get("bullets_title"):
                    pdf.para(b["bullets_title"] + ": " + "; ".join(b["bullets"]), size=BODY, x=MARGIN_C + 8, gap=0.5)
                else:
                    for it in b["bullets"]:
                        pdf.bullet(it)
            for p in b.get("after") or []:
                pdf.para(p, size=BODY, x=MARGIN_C + 8, gap=0.5)
            pdf.y += 1.5
    pdf.y += 3
    pdf.ensure(BODY * 2)
    pdf.page.draw_line((MARGIN_C, pdf.y), (A4_W - MARGIN_C, pdf.y), color=LINE, width=0.5)
    pdf.y += 3
    pdf.para(V["sign"], size=BODY, bold=True)
    return pdf.finish()
