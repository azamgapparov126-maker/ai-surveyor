"""
Документ Word (.docx) средствами стандартной библиотеки: zipfile + WordprocessingML. Без пакетов.

    doc = Docx()
    doc.title("СЮРВЕЙЕРСКИЙ АКТ")         # крупный заголовок
    doc.heading("1. Объект", level=1)
    doc.para("Текст **с выделением**", bold=False, color="555555", size=20)
    doc.bullet("пункт списка")
    doc.table([["Поле", "Значение"], ["Марка", "XCMG"]], widths=[3000, 6600])
    doc.band("1. ОБЪЕКТ", fill="0B4F8A")   # полоса-заголовок блока: белый текст на цвете бренда
    doc.image(png_bytes, width_cm=15)      # картинка PNG: word/media + отношение + inline drawing
    doc.page_break()
    data = doc.to_bytes()                  # готовый .docx

Размеры шрифта — в полупунктах (как в WordprocessingML): 22 = 11 pt. Ширины — в twip (1/20 pt),
ширина текста на A4 с полями 2 см ≈ 9 600. Управляющие символы, которых не бывает в XML, вырезаются.
"""
import io
import re
import struct
import zipfile
from xml.sax.saxutils import escape

W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
# пространства имён картинки (inline drawing) — объявлены у корня документа
PIC_NS = ('xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
          'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
          'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
          'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"')
EMU_PER_CM = 360000
TEXT_WIDTH = 9638                  # A4 (11906) минус поля 1134 × 2
_BAD_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def clean(text) -> str:
    """Текст для XML: без запрещённых символов, спецсимволы экранированы."""
    return escape(_BAD_XML.sub("", str(text if text is not None else "")))


def _runs(text: str, bold=False, size=None, color=None, italic=False) -> str:
    out = []
    for i, part in enumerate(re.split(r"\*\*(.+?)\*\*", str(text or ""))):
        if not part:
            continue
        b = bold or i % 2 == 1
        pr = ("<w:b/>" if b else "") + ("<w:i/>" if italic else "") + \
             (f'<w:color w:val="{color}"/>' if color else "") + \
             (f'<w:sz w:val="{int(size)}"/><w:szCs w:val="{int(size)}"/>' if size else "")
        # перевод строки внутри абзаца — <w:br/>
        pieces = part.split("\n")
        for j, piece in enumerate(pieces):
            if j:
                out.append(f"<w:r><w:rPr>{pr}</w:rPr><w:br/></w:r>")
            if piece:
                out.append(f'<w:r><w:rPr>{pr}</w:rPr><w:t xml:space="preserve">{clean(piece)}</w:t></w:r>')
    return "".join(out)


def _para(text, *, bold=False, size=None, color=None, before=0, after=120, indent=0, keep=False,
          align=None, italic=False) -> str:
    ppr = f'<w:spacing w:before="{int(before)}" w:after="{int(after)}"/>'
    if indent:
        ppr += f'<w:ind w:left="{int(indent)}" w:hanging="280"/>'
    if keep:
        ppr += "<w:keepNext/>"
    if align:
        ppr += f'<w:jc w:val="{align}"/>'
    return f"<w:p><w:pPr>{ppr}</w:pPr>{_runs(text, bold, size, color, italic)}</w:p>"


class Docx:
    def __init__(self, lang: str = "ru-RU", font: str = "Arial", margin: int = 1134, base_size: int = 21,
                 line: int = 264):
        """margin — поля страницы в twip (1134 = 2 см, 850 = 15 мм); base_size — шрифт по умолчанию в полупунктах
        (21 = 10,5 pt); line — межстрочный интервал (240 = одинарный). Ширина текста — self.text_width."""
        self.parts = []
        self.lang = lang
        self.font = font
        self.margin = int(margin)
        self.base_size = int(base_size)
        self.line = int(line)
        self.text_width = 11906 - 2 * self.margin
        self.media = []                    # [(имя файла в word/media, байты PNG, rId)]

    # ---------- блоки ----------
    def title(self, text: str, color: str = "1D2C8F"):
        self.parts.append(_para(text, bold=True, size=30, color=color, after=160, align="center", keep=True))

    def heading(self, text: str, level: int = 1, color: str = "1D2C8F"):
        size = {1: 26, 2: 23}.get(level, 22)
        self.parts.append(_para(text, bold=True, size=size, color=color, before=240, after=100, keep=True))

    def para(self, text: str, bold=False, size=None, color=None, italic=False, align=None, after=120):
        self.parts.append(_para(text, bold=bold, size=size, color=color, italic=italic, align=align, after=after))

    def bullet(self, text: str, size=None, color=None):
        self.parts.append(_para("•\t" + str(text or ""), size=size, color=color, indent=420, after=60))

    def table(self, rows: list, widths: list = None, header: bool = True, size: int = 20, borders: bool = True,
              label_cols: tuple = (), bold_cols: tuple = ()):
        """rows — список строк (списков ячеек); первая строка — заголовок, если header=True.
        borders=False — без рамок (шапка и пары «число — подпись»); label_cols — серые подписи, bold_cols — жирные."""
        if not rows:
            return
        n = max(len(r) for r in rows)
        widths = list(widths or [TEXT_WIDTH // n] * n)[:n]
        sides = ("top", "left", "bottom", "right", "insideH", "insideV")
        if borders:
            border = "".join(f'<w:{s} w:val="single" w:sz="4" w:space="0" w:color="B9BFD6"/>' for s in sides)
        else:
            border = "".join(f'<w:{s} w:val="nil"/>' for s in sides)
        x = [f'<w:tbl><w:tblPr><w:tblW w:w="{sum(widths)}" w:type="dxa"/><w:tblBorders>{border}</w:tblBorders>'
             f'<w:tblLayout w:type="fixed"/><w:tblCellMar><w:left w:w="90" w:type="dxa"/>'
             f'<w:right w:w="90" w:type="dxa"/></w:tblCellMar></w:tblPr><w:tblGrid>'
             + "".join(f'<w:gridCol w:w="{w}"/>' for w in widths) + "</w:tblGrid>"]
        for ri, r in enumerate(rows):
            r = (list(r) + [""] * n)[:n]
            head = header and ri == 0
            x.append("<w:tr>" + ("<w:trPr><w:tblHeader/><w:cantSplit/></w:trPr>" if head
                                 else "<w:trPr><w:cantSplit/></w:trPr>"))
            for ci, (c, w) in enumerate(zip(r, widths)):
                shade = '<w:shd w:val="clear" w:color="auto" w:fill="EEF0F9"/>' if head else ""
                x.append(f'<w:tc><w:tcPr><w:tcW w:w="{w}" w:type="dxa"/>{shade}</w:tcPr>'
                         + _para(c, bold=head or ci in bold_cols, size=size, before=30, after=30,
                                 color="666666" if ci in label_cols and not head else None) + "</w:tc>")
            x.append("</w:tr>")
        x.append("</w:tbl>" + _para("", after=60))
        self.parts.append("".join(x))

    def band(self, text: str, fill: str = "0B4F8A", size: int = 18):
        """Полоса-заголовок блока: белый жирный текст на заливке цвета бренда во всю ширину."""
        fill = re.sub(r"[^0-9A-Fa-f]", "", str(fill or ""))[:6].upper() or "0B4F8A"
        self.parts.append(f'<w:p><w:pPr><w:keepNext/><w:shd w:val="clear" w:color="auto" w:fill="{fill}"/>'
                          f'<w:spacing w:before="160" w:after="80"/></w:pPr>'
                          + _runs(" " + str(text or ""), bold=True, size=size, color="FFFFFF") + "</w:p>")

    def image(self, png: bytes, width_cm: float = 15.0, descr: str = "", align: str = "center"):
        """Картинка PNG в строку: файл word/media/imageN.png, отношение rIdImgN и inline drawing (wp:inline).
        Высота — по пропорциям картинки (ширина и высота из заголовка IHDR)."""
        if not png or png[:8] != b"\x89PNG\r\n\x1a\n" or png[12:16] != b"IHDR":
            raise ValueError("нужна картинка PNG")
        pw, ph = struct.unpack(">II", png[16:24])
        k = len(self.media) + 1
        rid = f"rIdImg{k}"
        name = f"image{k}.png"
        self.media.append((name, png, rid))
        cx = int(float(width_cm) * EMU_PER_CM)
        cy = int(cx * ph / pw) if pw else cx
        d = clean(descr)
        self.parts.append(
            f'<w:p><w:pPr><w:spacing w:before="60" w:after="60"/><w:jc w:val="{align}"/></w:pPr><w:r><w:drawing>'
            f'<wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="{cx}" cy="{cy}"/>'
            f'<wp:effectExtent l="0" t="0" r="0" b="0"/><wp:docPr id="{k}" name="Picture {k}" descr="{d}"/>'
            '<wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>'
            '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            f'<pic:pic><pic:nvPicPr><pic:cNvPr id="{k}" name="{name}"/><pic:cNvPicPr/></pic:nvPicPr>'
            f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
            f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic>'
            '</wp:inline></w:drawing></w:r></w:p>')

    def page_break(self):
        self.parts.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')

    def rule(self):
        self.parts.append('<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" '
                          'w:color="B9BFD6"/></w:pBdr><w:spacing w:after="120"/></w:pPr></w:p>')

    # ---------- сборка ----------
    def document_xml(self) -> str:
        return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {W_NS} {PIC_NS}><w:body>'
                + "".join(self.parts) +
                '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
                f'<w:pgMar w:top="{self.margin}" w:right="{self.margin}" w:bottom="{self.margin}" '
                f'w:left="{self.margin}" w:header="{min(708, self.margin // 2)}" '
                f'w:footer="{min(708, self.margin // 2)}" w:gutter="0"/></w:sectPr></w:body></w:document>')

    def to_bytes(self) -> bytes:
        f = clean(self.font)
        styles = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles {W_NS}><w:docDefaults>'
                  f'<w:rPrDefault><w:rPr><w:rFonts w:ascii="{f}" w:hAnsi="{f}" w:cs="{f}" w:eastAsia="{f}"/>'
                  f'<w:sz w:val="{self.base_size}"/><w:szCs w:val="{self.base_size}"/>'
                  f'<w:lang w:val="{clean(self.lang)}"/></w:rPr></w:rPrDefault>'
                  f'<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="{self.line}" w:lineRule="auto"/></w:pPr>'
                  '</w:pPrDefault></w:docDefaults></w:styles>')
        types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                 '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                 '<Default Extension="xml" ContentType="application/xml"/>'
                 + ('<Default Extension="png" ContentType="image/png"/>' if self.media else "") +
                 '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
                 'officedocument.wordprocessingml.document.main+xml"/>'
                 '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-'
                 'officedocument.wordprocessingml.styles+xml"/>'
                 '</Types>')
        rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        doc_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                    'relationships/styles" Target="styles.xml"/>'
                    + "".join(f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/'
                              f'2006/relationships/image" Target="media/{name}"/>' for name, _png, rid in self.media)
                    + '</Relationships>')
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", types)
            z.writestr("_rels/.rels", rels)
            z.writestr("word/document.xml", self.document_xml())
            z.writestr("word/styles.xml", styles)
            z.writestr("word/_rels/document.xml.rels", doc_rels)
            for name, png, _rid in self.media:
                z.writestr("word/media/" + name, png)
        return buf.getvalue()
