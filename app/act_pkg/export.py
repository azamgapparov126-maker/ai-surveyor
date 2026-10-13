"""Выгрузка акта: Word (app/docx_lite.py) и PDF (pymupdf) на две страницы A4, страница и картинка страхового скоринга.

С 13.10.2026 (ТЗ, день 9.1; макет — приложение A) документ — акт на две страницы (app/act_pkg/compact.py):
  стр. 1 — шапка и пять разделов (объект; стоимость и сумма; риск; решение и цена; условия);
  стр. 2 — фото осмотра с датой и координатами из EXIF (без документов: техпаспорт и прочие бумаги могут нести ПД),
           подписи «Сюрвейер / Страхователь», QR-код и ссылка на «Как посчитано» (GET /act/{id}?how=1).
Не помещается — сначала меньше фото, затем короче факторы (compact detail = 1); шрифт не меньше 9 pt.
Фото живут 24 часа (act_uploads): позже акт выходит без фото — одна страница, подписи и QR на ней же, если влезают.
Внутренние пояснения, источники и формулы (кроме цепочки множителей ставки) в документ не идут — они в JSON,
на экране и по ссылке «Как посчитано». Страница скоринга — отдельно: GET /act/{id}/scoring.pdf и scoring.png.
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import pymupdf

from .. import act_scoring as asc
from .. import act_texts as tx
from .. import db
from ..act_texts import t
from ..docx_lite import Docx
from ..proposal import A4_H, A4_W, BLACK, GRAY, LINE, FILL

from . import exif as exif_mod
from . import qr as qr_mod
from .compact import VIEW_ORDER, compact_view, explain_text

BRAND = (0.11, 0.17, 0.56)                 # синий INSON #1D2C8F
GREEN = (0.10, 0.47, 0.29)                 # заголовки разделов и линия шапки (макет, приложение A)
GREEN_FILL = (0.92, 0.96, 0.93)
VERDICT_COLORS = {"accept": ((0.10, 0.47, 0.29), (0.90, 0.96, 0.91)),
                  "review": ((0.62, 0.40, 0.02), (0.99, 0.95, 0.86)),
                  "decline": ((0.70, 0.13, 0.13), (0.99, 0.91, 0.91))}
MARGIN_C = 15 / 25.4 * 72                  # 15 мм в пунктах
CONTENT_C = A4_W - 2 * MARGIN_C
BODY = 9.5                                 # основной шрифт, pt
SMALL = 9                                  # таблицы, подписи фото, низ страницы — не меньше 9 pt (ТЗ 9.1)
LEAD = 1.2                                 # межстрочный интервал
EXPLAIN_MAX_LINES = 3
MAX_PHOTOS = 8
PHOTO_ROW_H = {1: 176, 2: 176, 3: 176, 4: 132}   # высота строки фото (с подписью) по числу строк
SIGN_BLOCK_H = 112                         # подписи, текст QR и сам QR
QR_SIZE = 74
DOCX_MARGIN = 850                          # 15 мм в twip
DOCX_WIDTH = 11906 - 2 * DOCX_MARGIN
PHOTO_MAX_SIDE = 1400                      # фото пережимается для PDF (оригинал не меняется)
_ROTATE = {3: 180, 6: -90, 8: 90}          # EXIF Orientation → поворот insert_image (против часовой)


# --------------------------------------------------------------------------- #
#  Картинка скоринга (для scoring.png; в акт не входит)
# --------------------------------------------------------------------------- #

def scoring_png(act: dict) -> bytes:
    """Картинка шкалы скоринга с плашкой класса (тот же код рисования, что у страницы PDF)."""
    regular, bold = _fonts()
    fit = lambda s, font: "".join(ch if ord(ch) < 128 or font.has_glyph(ord(ch)) else GLYPH_FALLBACK.get(ch, "?")  # noqa
                                  for ch in str(s))
    return asc.gauge_png(act["scoring"], regular, bold, fit)


# --------------------------------------------------------------------------- #
#  Данные вне акта: филиал, тариф, адрес сервера, фото (чтение базы и файлов)
# --------------------------------------------------------------------------- #

def pdf_extras(D: dict, row: dict, photos: bool = True) -> dict:
    """Для документа: филиал и номер сотрудника (без ФИО и телефона), приказ тарифа, адрес сервера для QR и фото
    осмотра (если загрузка ещё жива). row — строка таблицы acts. Ошибка чтения не роняет документ."""
    out = {"issuer": {}, "tariff": None, "server_url": "", "photos": []}
    try:
        from .. import llm
        out["server_url"] = str(llm.get("SERVER_URL") or "").strip().rstrip("/")
    except Exception:
        pass
    try:
        with db.tx() as con:
            uid = int((row or {}).get("user_id") or 0)
            if uid:
                u = db.rows(con, "SELECT branch FROM users WHERE id=?", uid)
                out["issuer"] = {"employee": uid, "branch": (u[0].get("branch") or None) if u else None}
            else:
                out["issuer"] = {"guest": True}
            tv = D.get("tariff_version_id")
            if tv:
                r = db.rows(con, "SELECT name, document_ref FROM tariff_versions WHERE id=?", tv)
                if r:
                    out["tariff_ref"] = r[0].get("document_ref") or r[0].get("name")
            if photos:
                out["photos"] = act_photos(con, D, row)
    except Exception as e:                                   # документ важнее подписи и фото
        print("акт: данные для PDF не прочитаны:", type(e).__name__, e)
    return out


def _tariff_for(extras: dict, lang: str) -> Optional[str]:
    from .view_fork import _order_ref_text
    ref = extras.get("tariff_ref")
    return _order_ref_text(ref, lang) if ref else extras.get("tariff")


def act_photos(con, D: dict, row: dict) -> list:
    """Фото осмотра для стр. 2: снимки загрузки акта (не документы), по порядку ракурсов, с EXIF (дата, координаты,
    поворот) и временем загрузки. Только своя загрузка (тот же владелец, что у акта)."""
    sid = (D.get("inspection") or {}).get("session")
    if not sid:
        return []
    r = db.rows(con, "SELECT files_json, result_json, created_at FROM act_uploads WHERE id=? AND owner_key=?",
                sid, (row or {}).get("owner_key") or "")
    if not r:
        return []
    files = json.loads(r[0]["files_json"] or "[]")
    res = json.loads(r[0]["result_json"] or "{}")
    views = res.get("views") or {}
    up = str(r[0].get("created_at") or "")[:16].replace("T", " ")
    order = {v: i for i, v in enumerate(VIEW_ORDER)}
    out = []
    for f in files:
        view = views.get(f.get("id"))
        if f.get("fmt") not in ("jpg", "png") or view in ("document", None):
            continue
        p = Path(f.get("path") or "")
        p = p if p.is_absolute() else db.ROOT / p
        try:
            blob = p.read_bytes()
        except OSError:
            continue
        ex = exif_mod.read(blob) if f.get("fmt") == "jpg" else {}
        out.append({"view": view, "blob": blob, "fmt": f["fmt"], "taken": ex.get("taken"), "lat": ex.get("lat"),
                    "lon": ex.get("lon"), "orientation": ex.get("orientation"), "uploaded": up,
                    "index": f.get("index") or 0})
    out.sort(key=lambda x: (order.get(x["view"], len(order)), x["index"]))
    return out


def _photo_blob(ph: dict) -> Optional[bytes]:
    """Фото для PDF: не больше PHOTO_MAX_SIDE по длинной стороне, JPEG; не открылось — None."""
    try:
        pix = pymupdf.Pixmap(ph["blob"])
        while max(pix.width, pix.height) > PHOTO_MAX_SIDE:
            pix.shrink(1)
        if pix.alpha:
            pix = pymupdf.Pixmap(pix, 0)
        if pix.n > 3:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        return pix.tobytes("jpg", jpg_quality=80)
    except Exception:
        return None


def _ddmm(s: str) -> str:
    try:
        return datetime.strptime(s[:16], "%Y-%m-%d %H:%M").strftime("%d.%m.%Y %H:%M")
    except (ValueError, TypeError):
        return str(s or "")


def photo_caption(ph: dict, lang: str, veh_group: Optional[str] = None, digits: int = 4) -> str:
    """«Спереди · 13.10.2026 10:12 · 41.3112, 69.2796»; без EXIF — «… · загружено 13.10.2026 10:12»."""
    name = tx.view_label(ph.get("view") or "other", lang, veh_group)
    parts = [name[:1].upper() + name[1:]]
    if ph.get("taken"):
        parts.append(_ddmm(ph["taken"]))
    elif ph.get("uploaded"):
        parts.append(t("a2_ph_uploaded", lang, v=_ddmm(ph["uploaded"])))
    if ph.get("lat") is not None and ph.get("lon") is not None:
        parts.append(f"{ph['lat']:.{digits}f}, {ph['lon']:.{digits}f}")
    return " · ".join(parts)


# --------------------------------------------------------------------------- #
#  Word
# --------------------------------------------------------------------------- #

def build_docx(act: dict, extras: Optional[dict] = None) -> bytes:
    """Акт (ответ render) → DOCX: те же две страницы, что у PDF; вместо фото — список подписей снимков."""
    lang = act["lang"]
    extras = dict(extras or {})
    extras["tariff"] = _tariff_for(extras, lang)
    V = compact_view(act, extras)
    doc = Docx(lang={"ru": "ru-RU", "uz": "uz-Latn-UZ", "en": "en-GB"}[lang], margin=DOCX_MARGIN, base_size=20,
               line=240)
    doc.table([[V["title"].upper(), V["brand"]], [V["head"], V["tariff"]]],
              widths=[int(DOCX_WIDTH * .7), DOCX_WIDTH - int(DOCX_WIDTH * .7)], header=False, size=19,
              borders=False, bold_cols=(1,))
    w = DOCX_WIDTH

    def head(s):
        doc.parts.append(_dpara(f"{s['n']}. {s['title']}", bold=True, size=21, color="1A784A", before=80, after=30,
                                keep=True))
    s1 = V["s1"]
    head(s1)
    doc.table(_pairs4(s1["pairs"]), widths=[int(w * .17), int(w * .33), int(w * .17), w - int(w * .17) * 2 - int(w * .33)],
              header=False, size=18, label_cols=(0, 2))
    s2 = V["s2"]
    head(s2)
    doc.table([list(r) for r in s2["rows"]], widths=[int(w * .22), w - int(w * .22)], header=False, size=18,
              label_cols=(0,))
    s3 = V["s3"]
    head(s3)
    for ln in s3["lines"]:
        doc.parts.append(_dpara(ln, size=19, after=10))
    s4 = V["s4"]
    head(s4)
    doc.parts.append(_dpara(f"**{s4['verdict']['label']}** · {s4['rate_line']}", size=19, after=20))
    if s4["verdict"].get("why") and s4["verdict"]["code"] != "accept":
        w_ = s4["verdict"]["why"]
        doc.parts.append(_dpara(w_[:1].upper() + w_[1:], size=18, color="6B7280", after=20))
    if s4.get("below"):
        doc.parts.append(_dpara(s4["below"], size=18, after=20))
    ex = explain_text(s4["explain"], lang)
    if ex:
        doc.parts.append(_dpara(ex, size=19, after=30, indent=160, border_left=True))
    if s4["chain"]:
        doc.parts.append(_dpara(s4["chain_title"], bold=True, size=19, after=10, keep=True))
        doc.table([s4["chain_head"]] + [list(r) for r in s4["chain"]],
                  widths=[int(w * .6), int(w * .15), w - int(w * .6) - int(w * .15)], header=True, size=18)
    tb = s4.get("table")
    if tb:
        doc.parts.append(_dpara(tb["title"], bold=True, size=19, after=10, keep=True))
        wts = tb["widths"]
        doc.table([tb["columns"]] + tb["rows"], widths=[int(w * x / sum(wts)) for x in wts], header=True, size=18)
    for i, ln in enumerate(s4["contract"]):
        doc.parts.append(_dpara(ln if i == 0 else "•\u00a0" + ln, bold=(i == 0), size=19, after=10,
                                indent=0 if i == 0 else 160))
    s5 = V["s5"]
    head(s5)
    for i, it in enumerate(s5["items"], 1):
        doc.parts.append(_dpara(f"{i}. {it}", size=19, after=10, indent=160))
    # стр. 2
    p2 = V["page2"]
    doc.parts.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
    doc.parts.append(_dpara(p2["title"].upper(), bold=True, size=24, after=10))
    doc.parts.append(_dpara(p2["head"], size=19, after=60))
    photos = (extras.get("photos") or [])[:MAX_PHOTOS]
    if photos:
        for ph in photos:
            doc.parts.append(_dpara("•\u00a0" + photo_caption(ph, lang, act.get("veh_group")), size=19, after=10))
    else:
        doc.parts.append(_dpara(p2["no_photos"], size=19, after=10))
    doc.parts.append(_dpara(f"{p2['sign'][0]}: ____________ / {p2['sign_name']}        "
                            f"{p2['sign'][1]}: ____________ / {p2['sign_name']}", size=19, before=200, after=80))
    doc.parts.append(_dpara(p2["qr_text"] + " " + p2["how_url"], size=19, after=40))
    doc.parts.append(_dpara(V["sign"], bold=True, italic=True, size=19, before=40, after=0, border_top=True))
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
           indent=0, border=False, border_top=False, border_left=False) -> str:
    """Абзац WordprocessingML с нужными отступами (docx_lite._para без висячего отступа и с линией под заголовком)."""
    from ..docx_lite import _runs
    ppr = f'<w:spacing w:before="{int(before)}" w:after="{int(after)}" w:line="240" w:lineRule="auto"/>'
    if indent:
        ppr += f'<w:ind w:left="{int(indent)}"/>'
    if keep:
        ppr += "<w:keepNext/>"
    if border:
        ppr += '<w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" w:color="1A784A"/></w:pBdr>'
    if border_top:
        ppr += '<w:pBdr><w:top w:val="single" w:sz="6" w:space="2" w:color="B9BFD6"/></w:pBdr>'
    if border_left:
        ppr += '<w:pBdr><w:left w:val="single" w:sz="18" w:space="6" w:color="1A784A"/></w:pBdr>'
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
                  "«": '"', "»": '"', "…": "...", "\u00a0": " ", "θ": "theta", "•": "*"}


def _fonts():
    for reg, bold in FONT_CANDIDATES:
        if Path(reg).exists() and Path(bold).exists():
            return pymupdf.Font(fontfile=reg), pymupdf.Font(fontfile=bold)
    # на сервере без шрифтов — встроенные Helvetica (кириллица в них есть, узбекской ʻ нет)
    return pymupdf.Font("helv"), pymupdf.Font("hebo")


class _Sheet:
    """Поток текста сверху вниз на A4 с полями 15 мм: абзацы, заголовки разделов, таблицы, фото, QR."""

    def __init__(self, footer: str, lang: str):
        self.doc = pymupdf.open()
        self.regular, self.bold = _fonts()
        self.footer, self.lang = footer, lang
        self.page = None
        self.writers = {}
        self.y = 0.0
        self.new_page()

    @property
    def bottom(self) -> float:
        return A4_H - MARGIN_C - 12                # запас под строку низа страницы

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

    def width(self, s, font, size) -> float:
        return font.text_length(self.fit(s, font), fontsize=size)

    def ensure(self, h: float):
        if self.y + h > self.bottom:
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
            tx_ = x
            if align == "c":
                tx_ = x + (width - self.width(ln, font, size)) / 2
            elif align == "r":
                tx_ = x + width - self.width(ln, font, size)
            self._put(tx_, self.y + size, ln, font, size, color)
            self.y += lh
        self.y += gap

    def heading(self, s: str):
        self.ensure(BODY * 4.5)
        self.y += 4
        self._put(MARGIN_C, self.y + 11, s, self.bold, 11, GREEN)
        self.y += 11 * LEAD + 2.5

    def rule(self, color=GREEN, width=1.2, gap=4):
        self.page.draw_line((MARGIN_C, self.y), (A4_W - MARGIN_C, self.y), color=color, width=width)
        self.y += gap

    def table(self, cols: list, rows: list, size=SMALL, header=False, label_cols=(), bold_rows=()):
        """cols: [ширина]; rows: списки строк. Компактно: отступ 2,5 pt, тонкие линии."""
        lh = size * 1.18
        pad = 2.5

        def draw(cells, font, color, fill=None, bold_row=False):
            wrapped = [self.wrap(c, self.bold if bold_row else font, size, w - 2 * pad) for c, w in zip(cells, cols)]
            h = max(len(w) for w in wrapped) * lh + 2 * pad
            self.ensure(h)
            if fill:
                self.page.draw_rect(pymupdf.Rect(MARGIN_C, self.y, A4_W - MARGIN_C, self.y + h), color=None, fill=fill)
            x = MARGIN_C
            for ci, (lines, w) in enumerate(zip(wrapped, cols)):
                for i, ln in enumerate(lines):
                    self._put(x + pad, self.y + pad + size + i * lh - 1, ln, self.bold if bold_row else font,
                              size, GRAY if ci in label_cols and fill is None else color)
                x += w
            self.y += h
            self.page.draw_line((MARGIN_C, self.y), (A4_W - MARGIN_C, self.y), color=LINE, width=0.4)

        if header:
            draw(rows[0], self.bold, GRAY, fill=FILL)
            rows = rows[1:]
        for i, r in enumerate(rows):
            draw([str(c) for c in r], self.regular, BLACK, bold_row=i in bold_rows)
        self.y += 3

    def finish(self) -> bytes:
        self._flush()
        n = self.doc.page_count
        for i, page in enumerate(self.doc, start=1):
            tw = pymupdf.TextWriter(page.rect)
            s = self.fit(t("page", self.lang, i=i, n=n), self.regular)
            w = self.regular.text_length(s, fontsize=SMALL)
            tw.append((A4_W - MARGIN_C - w, A4_H - MARGIN_C + 10), s, font=self.regular, fontsize=SMALL)
            foot = self.fit(self.footer, self.regular)
            while foot and self.regular.text_length(foot, fontsize=SMALL) > CONTENT_C - w - 12:
                foot = foot[:-1]
            tw.append((MARGIN_C, A4_H - MARGIN_C + 10), foot, font=self.regular, fontsize=SMALL)
            tw.write_text(page, color=GRAY)
        self.doc.subset_fonts()
        return self.doc.tobytes(garbage=3, deflate=True)


def explain_fit(E: dict, lang: str, font, size: float = BODY, width: float = CONTENT_C - 14) -> list:
    """Пояснение ставки не длиннее 3 строк на ширине блока: убираем факторы по одному с конца списка; если и без
    факторов длиннее — последняя строка обрезается многоточием. Возвращает строки."""
    sheet_fit = lambda s: "".join(ch if ord(ch) < 128 or font.has_glyph(ord(ch)) else GLYPH_FALLBACK.get(ch, "?")  # noqa
                                  for ch in str(s))

    def wrap(s):
        lines, cur = [], ""
        for w in str(s).split(" "):
            cand = (cur + " " + w).strip()
            if font.text_length(sheet_fit(cand), fontsize=size) <= width or not cur:
                cur = cand
            else:
                lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return lines
    n = len(E.get("factors") or [])
    for k in range(n, -1, -1):
        lines = wrap(explain_text(E, lang, k))
        if len(lines) <= EXPLAIN_MAX_LINES:
            return lines
    lines = wrap(explain_text(E, lang, 0))[:EXPLAIN_MAX_LINES]
    last = lines[-1]
    while last and font.text_length(sheet_fit(last + "…"), fontsize=size) > width:
        last = last.rsplit(" ", 1)[0] if " " in last else last[:-1]
    lines[-1] = last + "…"
    return lines


def _page1(pdf: _Sheet, V: dict) -> None:
    lang = V["lang"]
    # шапка: слева заголовок и номер, справа INSON и тариф
    right_w = max(pdf.width(V["brand"], pdf.bold, 11), pdf.width(V["tariff"], pdf.regular, SMALL)) + 4
    y0 = pdf.y
    pdf._put(A4_W - MARGIN_C - pdf.width(V["brand"], pdf.bold, 11), y0 + 11, V["brand"], pdf.bold, 11, GREEN)
    pdf._put(A4_W - MARGIN_C - pdf.width(V["tariff"], pdf.regular, SMALL), y0 + 11 + 13, V["tariff"], pdf.regular,
             SMALL, BLACK)
    pdf.para(V["title"].upper(), size=13, bold=True, gap=1, width=CONTENT_C - right_w - 8)
    pdf.para(V["head"], size=SMALL, color=BLACK, gap=2, width=CONTENT_C - right_w - 8)
    pdf.y = max(pdf.y, y0 + 30)
    pdf.rule()
    w = CONTENT_C
    # 1. объект
    s1 = V["s1"]
    pdf.heading(f"1. {s1['title']}")
    pdf.table([w * .15, w * .35, w * .15, w * .35], _pairs4(s1["pairs"]), label_cols=(0, 2))
    # 2. стоимость
    s2 = V["s2"]
    pdf.heading(f"2. {s2['title']}")
    pdf.table([w * .2, w * .8], [list(r) for r in s2["rows"]], label_cols=(0,))
    # 3. риск
    s3 = V["s3"]
    pdf.heading(f"3. {s3['title']}")
    for ln in s3["lines"]:
        pdf.para(ln, size=BODY, gap=1)
    # 4. решение и цена
    s4 = V["s4"]
    pdf.heading(f"4. {s4['title']}")
    dec = s4["verdict"]
    fg, bg = VERDICT_COLORS.get(dec["code"], VERDICT_COLORS["review"])
    lw = pdf.width(dec["label"], pdf.bold, BODY) + 14
    pdf.ensure(BODY * 2.4)
    rect = pymupdf.Rect(MARGIN_C, pdf.y, MARGIN_C + lw, pdf.y + BODY * 1.9)
    pdf.page.draw_rect(rect, color=fg, fill=bg, width=0.6, radius=0.45)
    pdf._put(MARGIN_C + 7, pdf.y + BODY * 1.35, dec["label"], pdf.bold, BODY, fg)
    ytop = pdf.y
    pdf.y += (BODY * 1.9 - BODY * LEAD) / 2
    pdf.para(s4["rate_line"], size=BODY, x=MARGIN_C + lw + 8, gap=0)
    pdf.y = max(pdf.y, ytop + BODY * 1.9) + 3
    if dec.get("why") and dec["code"] != "accept":
        pdf.para(dec["why"][:1].upper() + dec["why"][1:], size=SMALL, color=GRAY, gap=2)
    if s4.get("below"):
        pdf.para(s4["below"], size=SMALL, gap=2)
    lines = explain_fit(s4["explain"], lang, pdf.regular) if s4["explain"].get("main") else []
    if lines:
        h = len(lines) * BODY * LEAD + 6
        pdf.ensure(h)
        pdf.page.draw_rect(pymupdf.Rect(MARGIN_C, pdf.y, A4_W - MARGIN_C, pdf.y + h), color=None, fill=GREEN_FILL)
        pdf.page.draw_rect(pymupdf.Rect(MARGIN_C, pdf.y, MARGIN_C + 2.5, pdf.y + h), color=None, fill=GREEN)
        pdf.y += 3
        for ln in lines:
            pdf._put(MARGIN_C + 9, pdf.y + BODY, ln, pdf.regular, BODY, BLACK)
            pdf.y += BODY * LEAD
        pdf.y += 6
    if s4["chain"]:
        pdf.para(s4["chain_title"], size=SMALL, bold=True, gap=1)
        pdf.table([w * .6, w * .15, w * .25], [s4["chain_head"]] + [list(r) for r in s4["chain"]], header=True,
                  bold_rows=(len(s4["chain"]) - 1,))
    tb = s4.get("table")
    if tb:
        pdf.para(tb["title"], size=SMALL, bold=True, gap=1)
        wts = tb["widths"]
        pdf.table([w * x / sum(wts) for x in wts], [tb["columns"]] + tb["rows"], header=True,
                  bold_rows=(len(tb["rows"]) - 1,))
    for i, ln in enumerate(s4["contract"]):
        if i == 0:
            pdf.para(ln, size=BODY, bold=True, gap=1)
        else:
            pdf.ensure(BODY * LEAD)
            pdf._put(MARGIN_C + 6, pdf.y + BODY, "•", pdf.regular, BODY, BLACK)
            pdf.para(ln, size=BODY, x=MARGIN_C + 14, gap=0.5)
    # 5. условия
    s5 = V["s5"]
    pdf.heading(f"5. {s5['title']}")
    for i, it in enumerate(s5["items"], 1):
        pdf.ensure(BODY * LEAD)
        pdf._put(MARGIN_C + 2, pdf.y + BODY, f"{i}.", pdf.regular, BODY, BLACK)
        pdf.para(it, size=BODY, x=MARGIN_C + 14, gap=1)


def _draw_qr(pdf: _Sheet, text: str, x: float, y: float, size: float) -> bool:
    try:
        m = qr_mod.matrix(text)
    except ValueError:
        return False
    n = len(m) + 8                                       # тихая зона по 4 модуля
    cell = size / n
    shape = pdf.page.new_shape()
    for r, line in enumerate(m):
        c = 0
        while c < len(line):
            if line[c]:
                c0 = c
                while c < len(line) and line[c]:
                    c += 1
                shape.draw_rect(pymupdf.Rect(x + (c0 + 4) * cell, y + (r + 4) * cell, x + (c + 4) * cell,
                                             y + (r + 5) * cell))
            else:
                c += 1
    shape.finish(color=None, fill=(0, 0, 0))
    shape.commit()
    return True


def _sign_block(pdf: _Sheet, V: dict) -> None:
    p2 = V["page2"]
    pdf.ensure(SIGN_BLOCK_H)
    pdf.y += 8
    half = CONTENT_C / 2
    for i, who in enumerate(p2["sign"]):
        x = MARGIN_C + i * (half + 6)
        pdf.page.draw_line((x, pdf.y), (x + half - 12, pdf.y), color=GRAY, width=0.5)
        pdf._put(x, pdf.y + 12, f"{who}: ______________ / {p2['sign_name']}", pdf.regular, SMALL, BLACK)
    pdf.y += 26
    y0 = pdf.y
    ok = _draw_qr(pdf, p2["how_url"], A4_W - MARGIN_C - QR_SIZE, y0, QR_SIZE)
    tw = CONTENT_C - (QR_SIZE + 10 if ok else 0)
    pdf.para(p2["qr_text"], size=SMALL, width=tw, gap=1)
    pdf.para(p2["how_url"], size=SMALL, color=BRAND, width=tw, gap=1)
    pdf.y = max(pdf.y, y0 + (QR_SIZE if ok else 0)) + 2


def _photos_grid(pdf: _Sheet, photos: list, lang: str, veh_group: Optional[str], n: int) -> None:
    rows = (n + 1) // 2
    row_h = PHOTO_ROW_H[min(4, max(1, rows))]
    cw = (CONTENT_C - 10) / 2
    for i, ph in enumerate(photos[:n]):
        r, c = divmod(i, 2)
        x = MARGIN_C + c * (cw + 10)
        y = pdf.y + r * row_h
        box = pymupdf.Rect(x, y, x + cw, y + row_h - 16)
        pdf.page.draw_rect(box, color=None, fill=(0.91, 0.92, 0.94))
        blob = _photo_blob(ph)
        if blob:
            try:
                pdf.page.insert_image(box, stream=blob, keep_proportion=True,
                                      rotate=_ROTATE.get(ph.get("orientation") or 1, 0))
            except Exception:
                pass
        cap = photo_caption(ph, lang, veh_group)
        if pdf.width(cap, pdf.regular, SMALL) > cw:
            cap = photo_caption(ph, lang, veh_group, digits=2)      # длинный ракурс — координаты короче
        if pdf.width(cap, pdf.regular, SMALL) > cw:
            while cap and pdf.width(cap + "…", pdf.regular, SMALL) > cw:
                cap = cap[:-1]
            cap += "…"
        pdf._put(x, y + row_h - 5, cap, pdf.regular, SMALL, BLACK)
    pdf.y += rows * row_h + 2


def _photos_fit(pdf: _Sheet, n_total: int) -> int:
    """Сколько фото помещается на текущей странице над блоком подписей (строки по две; не больше MAX_PHOTOS)."""
    n = min(n_total, MAX_PHOTOS)
    while n > 0:
        rows = (n + 1) // 2
        if pdf.y + 40 + rows * PHOTO_ROW_H[min(4, rows)] + SIGN_BLOCK_H <= pdf.bottom:
            return n
        n -= 2 if n % 2 == 0 else 1
    return 0


def _layout(act: dict, V: dict, photos: list, n_photos: Optional[int]) -> tuple:
    """Одна попытка вёрстки: (pdf, страниц, фото показано). n_photos None — сколько влезет."""
    lang = act["lang"]
    pdf = _Sheet(V["sign"], lang)
    _page1(pdf, V)
    shown = 0
    if photos:
        if pdf.doc.page_count == 1:
            pdf.new_page()
        else:
            pdf.y += 10
        p2 = V["page2"]
        pdf.ensure(80)
        y0 = pdf.y
        pdf._put(A4_W - MARGIN_C - pdf.width(V["brand"], pdf.bold, 11), y0 + 11, V["brand"], pdf.bold, 11, GREEN)
        pdf.para(p2["title"].upper(), size=13, bold=True, gap=1)
        pdf.para(p2["head"], size=SMALL, gap=2)
        pdf.rule()
        can = _photos_fit(pdf, len(photos))
        shown = can if n_photos is None else min(n_photos, can)
        if shown:
            _photos_grid(pdf, photos, lang, act.get("veh_group"), shown)
        more = len(photos) - shown
        if more > 0:
            pdf.para(t("a2_ph_more", lang, n=more), size=SMALL, color=GRAY, gap=2)
    elif pdf.y + SIGN_BLOCK_H > pdf.bottom:
        pdf.new_page()
    _sign_block(pdf, V)
    return pdf, pdf.doc.page_count, shown


def build_pdf(act: dict, scoring_only: bool = False, extras: Optional[dict] = None) -> bytes:
    """Акт в PDF на две страницы A4 (стр. 1 — пять разделов, стр. 2 — фото, подписи, QR). extras —
    pdf_extras(): филиал, тариф, адрес сервера, фото. scoring_only — только страница скоринга
    (GET /act/{id}/scoring.pdf)."""
    lang = act["lang"]
    if scoring_only:
        pdf = _Sheet(act["footer"], lang)
        if (act.get("scoring") or {}).get("available"):
            asc.draw_page(pdf.page, act["scoring"], pdf.regular, pdf.bold, pdf.fit, 20 / 25.4 * 72,
                          A4_H - 20 / 25.4 * 72 - 4)
        return pdf.finish()
    extras = dict(extras or {})
    extras["tariff"] = _tariff_for(extras, lang)
    photos = [p for p in extras.get("photos") or []]
    want = min(len(photos), MAX_PHOTOS)
    # порядок упрощения (ТЗ 9.1): сначала меньше фото (до одной строки), затем короче факторы; акт без фото, у
    # которого на вторую страницу ушли одни подписи, тоже пробует короткий вид — чтобы уместиться на одной
    tries = []
    for detail in (0, 1):
        V = compact_view(act, extras, detail)
        res = _layout(act, V, photos, None)
        tries.append(res)
        _pdf, pages, shown = res
        if pages <= 2 and shown >= min(want, 2) and (photos or pages == 1):
            return _pdf.finish()
    good = [r for r in tries if r[1] <= 2 and r[2] >= min(want, 2)]
    return (good[0] if good else tries[-1])[0].finish()
