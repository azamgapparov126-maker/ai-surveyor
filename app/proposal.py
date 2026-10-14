"""
Предложение клиенту: PDF по сохранённому запросу, который агент отправляет страхователю.
  GET /requests/{rid}/proposal.pdf

Данные — из базы (запрос, объект, расчёт, проверки, рекомендации); предупредительные мероприятия
пересчитываются движком на актуальных справочниках. Вёрстка — pymupdf, шрифт из app/fonts.py
(Arial на Windows, DejaVu Sans на сервере; нет ни одного — 503).
"""
import json
from datetime import datetime

import pymupdf
from fastapi import APIRouter, HTTPException, Response

from . import db, fonts
from .engine import Input, calculate

router = APIRouter()


A4_W, A4_H = 595.28, 841.89
MARGIN = 20 / 25.4 * 72            # 20 мм в пунктах
CONTENT_W = A4_W - 2 * MARGIN

BLACK = (0.10, 0.11, 0.13)
GRAY = (0.45, 0.47, 0.50)
ACCENT = (0.05, 0.42, 0.36)
LINE = (0.80, 0.82, 0.84)
FILL = (0.95, 0.96, 0.97)

NBSP = "\u00a0"


# ---------- форматирование ----------

def money(x) -> str:
    if x is None:
        return "—"
    return f"{x:,.0f}".replace(",", NBSP) + NBSP + "сум"


def pct(x, digits=3) -> str:
    if x is None:
        return "—"
    return f"{x:.{digits}f}".replace(".", ",") + NBSP + "%"


def ru_date(s: str) -> str:
    """ГГГГ-ММ-ДД (или ISO с временем) → ДД.ММ.ГГГГ."""
    if not s:
        return "—"
    try:
        return datetime.fromisoformat(s[:19]).strftime("%d.%m.%Y")
    except ValueError:
        return s


def days_word(n: int) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "дня"
    return "дней"


# ---------- вёрстка ----------

class _Pdf:
    """Поток текста сверху вниз с переносом слов и страниц. Один шрифт — один TextWriter на цвет."""

    def __init__(self, where: str = "предложение клиенту"):
        self.doc = pymupdf.open()
        reg, bold = fonts.require_pair(where)     # нет шрифта — 503 и проверенные пути в журнал
        self.regular = pymupdf.Font(fontfile=reg)
        self.bold = pymupdf.Font(fontfile=bold)
        self.page = None
        self.writers = {}
        self.y = 0.0
        # подпись внизу страницы: у предложения клиенту и у анализа запроса она разная
        self.footer = "СО АО «INSON» — Предложение по страхованию"
        self.new_page()

    # страницы
    def new_page(self):
        self._flush()
        self.page = self.doc.new_page(width=A4_W, height=A4_H)
        self.writers = {}
        self.y = MARGIN

    def _flush(self):
        if self.page is None:
            return
        for color, tw in self.writers.items():
            tw.write_text(self.page, color=color)
        self.writers = {}

    def _writer(self, color):
        if color not in self.writers:
            self.writers[color] = pymupdf.TextWriter(self.page.rect)
        return self.writers[color]

    def ensure(self, h: float):
        if self.y + h > A4_H - MARGIN - 18:     # запас под номер страницы
            self.new_page()

    # примитивы
    def _put(self, x, baseline, s, font, size, color):
        self._writer(color).append((x, baseline), s, font=font, fontsize=size)

    def wrap(self, s: str, font, size: float, width: float) -> list:
        lines = []
        for raw in str(s).split("\n"):
            words, cur = raw.split(" "), ""
            for w in words:
                cand = (cur + " " + w).strip()
                if font.text_length(cand, fontsize=size) <= width or not cur:
                    cur = cand
                else:
                    lines.append(cur)
                    cur = w
            lines.append(cur)
        return lines

    def para(self, s: str, size=10, bold=False, color=BLACK, gap=4, x=None, width=None, leading=1.35):
        font = self.bold if bold else self.regular
        x = MARGIN if x is None else x
        width = CONTENT_W - (x - MARGIN) if width is None else width
        lines = self.wrap(s, font, size, width)
        lh = size * leading
        self.ensure(lh * min(len(lines), 2))
        for ln in lines:
            self.ensure(lh)
            self._put(x, self.y + size, ln, font, size, color)
            self.y += lh
        self.y += gap

    def space(self, h: float):
        self.y += h

    def rule(self, color=LINE, width=0.6):
        self.ensure(4)
        self.page.draw_line((MARGIN, self.y), (A4_W - MARGIN, self.y), color=color, width=width)
        self.y += 4

    def heading(self, s: str, size=12.5):
        self.ensure(size * 3)
        self.space(6)
        self._put(MARGIN, self.y + size, s, self.bold, size, ACCENT)
        self.y += size * 1.35 + 2
        self.rule(ACCENT, 0.8)
        self.space(3)

    def bullet(self, s: str, size=10, color=BLACK):
        self._put(MARGIN + 4, self.y + size, "•", self.regular, size, color)
        self.para(s, size=size, color=color, x=MARGIN + 14, gap=3)

    def kv(self, pairs: list, label_w=170, size=10):
        """Пары «подпись — значение» в две колонки."""
        for label, value in pairs:
            lines = self.wrap(value, self.regular, size, CONTENT_W - label_w)
            lh = size * 1.35
            self.ensure(lh * len(lines))
            self._put(MARGIN, self.y + size, label, self.regular, size, GRAY)
            for ln in lines:
                self._put(MARGIN + label_w, self.y + size, ln, self.regular, size, BLACK)
                self.y += lh
            self.y += 2

    def table(self, cols: list, rows: list, size=9.5, header=True):
        """cols: [(заголовок, ширина, выравнивание 'l'|'r')]; rows: списки строк по колонкам."""
        lh = size * 1.35
        pad = 4

        def draw_row(cells, font, color, fill=None):
            wrapped = [self.wrap(c, font, size, w - 2 * pad) for c, (_, w, _) in zip(cells, cols)]
            n = max(len(w) for w in wrapped)
            h = n * lh + 2 * pad
            self.ensure(h)
            if fill:
                self.page.draw_rect(pymupdf.Rect(MARGIN, self.y, A4_W - MARGIN, self.y + h), color=None, fill=fill)
            x = MARGIN
            for lines, (_, w, align) in zip(wrapped, cols):
                for i, ln in enumerate(lines):
                    tx = x + pad
                    if align == "r":
                        tx = x + w - pad - font.text_length(ln, fontsize=size)
                    self._put(tx, self.y + pad + size + i * lh, ln, font, size, color)
                x += w
            self.y += h
            self.page.draw_line((MARGIN, self.y), (A4_W - MARGIN, self.y), color=LINE, width=0.5)

        if header:
            draw_row([c[0] for c in cols], self.bold, GRAY, fill=FILL)
        for r in rows:
            draw_row(r, self.regular, BLACK)
        self.space(6)

    def big_number(self, label: str, value: str, sub: str = ""):
        """Цена крупно, с подписью и пояснением."""
        self.ensure(60)
        self._put(MARGIN, self.y + 9, label, self.regular, 9, GRAY)
        self.y += 14
        self._put(MARGIN, self.y + 22, value, self.bold, 22, BLACK)
        self.y += 30
        if sub:
            self._put(MARGIN, self.y + 9.5, sub, self.regular, 9.5, GRAY)
            self.y += 16

    def finish(self) -> bytes:
        self._flush()
        n = self.doc.page_count
        for i, page in enumerate(self.doc, start=1):
            tw = pymupdf.TextWriter(page.rect)
            s = f"Страница {i} из {n}"
            w = self.regular.text_length(s, fontsize=8)
            tw.append((A4_W - MARGIN - w, A4_H - MARGIN + 6), s, font=self.regular, fontsize=8)
            tw.append((MARGIN, A4_H - MARGIN + 6), self.footer, font=self.regular, fontsize=8)
            tw.write_text(page, color=GRAY)
        self.doc.subset_fonts()                 # встраиваем только использованные глифы (~1 МБ → ~100 КБ)
        return self.doc.tobytes(garbage=3, deflate=True)


# ---------- данные ----------

def _card(con, rid: int) -> dict:
    """Карточка запроса: как в main._card, плюс риски объекта, продукт и агент."""
    req = db.rows(con, "SELECT * FROM requests WHERE id=?", rid)
    if not req:
        raise HTTPException(404, "Запрос не найден")
    obj = db.rows(con, "SELECT * FROM objects WHERE request_id=?", rid)
    calc = db.rows(con, "SELECT * FROM calculations WHERE request_id=? ORDER BY id DESC LIMIT 1", rid)
    if not obj or not calc:
        raise HTTPException(409, "По запросу нет объекта или расчёта — предложение сформировать нельзя")
    cid = calc[0]["id"]
    checks = db.rows(con, "SELECT rule_code, status, detail FROM check_results WHERE calculation_id=?", cid)
    recs = db.rows(con, "SELECT kind, text, premium_delta FROM recommendations WHERE calculation_id=?", cid)
    docs = db.rows(con, "SELECT doc_name, received FROM documents WHERE request_id=?", rid)
    perils = db.rows(con, "SELECT peril_code FROM object_perils WHERE object_id=? AND included=1", obj[0]["id"])
    product = db.rows(con, "SELECT code, name FROM products WHERE code=?", req[0]["product_code"])
    agent = db.rows(con, "SELECT name, kind FROM agents WHERE id=?", req[0]["agent_id"]) if req[0]["agent_id"] else []
    return {"request": req[0], "object": obj[0], "calculation": calc[0], "checks": checks,
            "recommendations": recs, "documents": docs, "perils": [p["peril_code"] for p in perils],
            "product": product[0] if product else {"code": req[0]["product_code"], "name": "—"},
            "agent": agent[0] if agent else None}


def _class_of(con, card: dict) -> str:
    """Класс объекта: по включённым рискам, иначе первый класс продукта."""
    if card["perils"]:
        r = db.rows(con, "SELECT class_code FROM perils WHERE code=?", card["perils"][0])
        if r:
            return r[0]["class_code"]
    pcs = db.rows(con, "SELECT class_code FROM product_classes WHERE product_code=? ORDER BY part_no",
                  card["request"]["product_code"])
    if not pcs:
        raise HTTPException(409, "У продукта нет классов страхования")
    return pcs[0]["class_code"]


def _term_days(card: dict, attrs: dict) -> int:
    """Срок не хранится отдельно: восстанавливаем из премии и ставки, иначе из дат, иначе год."""
    c = card["calculation"]
    o = card["object"]
    if c["premium"] and c["applied_rate_pct"] and o["sum_insured"]:
        d = round(c["premium"] / (c["applied_rate_pct"] / 100 * o["sum_insured"]) * 365)
        if 1 <= d <= 3660:
            return int(d)
    if attrs.get("term_from") and attrs.get("term_to"):
        try:
            return max(1, (datetime.fromisoformat(attrs["term_to"]) - datetime.fromisoformat(attrs["term_from"])).days)
        except ValueError:
            pass
    return 365


def _to_input(con, card: dict) -> Input:
    o, c = card["object"], card["calculation"]
    attrs = json.loads(o["attributes"] or "{}")
    expl = json.loads(c["explanation"] or "{}")
    return Input(product_code=card["request"]["product_code"], class_code=_class_of(con, card),
                 object_type=o["object_type"], value_amount=o["value_amount"], sum_insured=o["sum_insured"],
                 term_days=_term_days(card, attrs), factors=attrs.get("factors") or {},
                 perils_included=card["perils"] or None,
                 docs_received=[d["doc_name"] for d in card["documents"] if d["received"]],
                 applied_rate_pct=c["applied_rate_pct"] if expl.get("manual") else None,
                 manual_reason=expl.get("manual_reason") or "", premium_paid=bool(attrs.get("premium_paid")),
                 credit=attrs.get("credit"))


def _chain_rows(chain: list, calc: dict, expl: dict) -> list:
    """Строки таблицы «Из чего сложилась ставка» с нарастающим значением ставки."""
    run, out = 0.0, []
    for step in chain:
        name = step["name"]
        if "value_pct" in step:
            run = step["value_pct"]
            action = pct(step["value_pct"], 4)
        elif "mult" in step:
            if step.get("applies") is False:
                action = "× 1,000 (не применяется)"
            else:
                run *= step["mult"]
                action = "× " + f"{step['mult']:.3f}".replace(".", ",")
            if step.get("excluded"):
                name += " (исключено: " + ", ".join(step["excluded"]).lower() + ")"
        elif "add_pct" in step:
            run += step["add_pct"]
            action = "+ " + pct(step["add_pct"], 4)
        elif "divide_by" in step:
            run /= step["divide_by"]
            action = "÷ " + f"{step['divide_by']:.2f}".replace(".", ",")
        else:
            action = ""
        out.append([name, action, pct(run, 4)])
    applied = calc["applied_rate_pct"]
    if expl.get("manual"):
        out.append(["Ставка установлена андеррайтером" + (f" ({expl.get('manual_reason')})" if expl.get("manual_reason") else ""),
                    "", pct(applied, 4)])
    elif calc["min_rate_pct"] is not None and applied > (calc["gross_rate_pct"] or 0) + 1e-9:
        out.append(["Минимальная ставка по продукту (не ниже неё)", "", pct(applied, 4)])
    return out


def _missing_docs(checks: list) -> list:
    for ch in checks:
        if ch["rule_code"] == "docs_missing" and ch["detail"]:
            # detail = «Не хватает документов: N: имя; имя» — берём часть после второго двоеточия
            tail = ch["detail"].split(": ", 2)[-1]
            return [d.strip() for d in tail.split(";") if d.strip()]
    return []


# ---------- документ ----------

def build_proposal(rid: int) -> bytes:
    with db.tx() as con:
        card = _card(con, rid)
        ref = db.load_reference(con)
        inp = _to_input(con, card)
        fresh = calculate(ref, inp)                           # актуальные предупредительные мероприятия
        class_name = db.rows(con, "SELECT name FROM classes WHERE code=?", inp.class_code)
        fr = db.rows(con, "SELECT option_name FROM coefficients WHERE factor_code='franchise' AND option_code=?",
                     inp.factors.get("franchise") or "")
        uncalibrated = con.execute("SELECT COUNT(*) FROM coefficients WHERE calibrated=0").fetchone()[0] > 0 \
            or con.execute("SELECT COUNT(*) FROM base_rates WHERE calibrated=0").fetchone()[0] > 0

    req, obj, calc = card["request"], card["object"], card["calculation"]
    expl = json.loads(calc["explanation"] or "{}")
    attrs = json.loads(obj["attributes"] or "{}")
    class_perils = {c: p for c, p in ref.perils.items() if p["class_code"] == inp.class_code}
    included = [class_perils[c]["name"] for c in card["perils"] if c in class_perils] or \
               [class_perils[c]["name"] for c in fresh["perils_included"] if c in class_perils]
    excluded = [p["name"] for c, p in class_perils.items()
                if c not in set(card["perils"] or fresh["perils_included"])]
    under = obj["sum_insured"] < obj["value_amount"] and not inp.credit
    over = obj["sum_insured"] > obj["value_amount"] and not inp.credit

    if attrs.get("term_from") and attrs.get("term_to"):
        term = f"с {ru_date(attrs['term_from'])} по {ru_date(attrs['term_to'])} ({inp.term_days} {days_word(inp.term_days)})"
    else:
        term = f"{inp.term_days} {days_word(inp.term_days)}"
    franchise = fr[0]["option_name"] if fr else ("не установлена" if not inp.factors.get("franchise") else str(inp.factors["franchise"]))
    where = ", ".join(x for x in (obj["region"], obj["address"]) if x) or "не указан"

    pdf = _Pdf()

    # 1. Шапка
    pdf.para("СО АО «INSON»", size=10, bold=True, color=GRAY, gap=2)
    pdf.para("Предложение по страхованию", size=20, bold=True, gap=2)
    number = f"Запрос № {rid}" + (f", договор {req['external_no']}" if req["external_no"] else "")
    pdf.para(f"{number} — {datetime.now().strftime('%d.%m.%Y')}", size=10, color=GRAY, gap=2)
    if req["policyholder"]:
        pdf.para(f"Страхователь: {req['policyholder']}", size=10, gap=2)
    pdf.space(4)
    pdf.rule()

    # 2. Объект
    pdf.heading("Что страхуем")
    pdf.kv([
        ("Продукт", f"{card['product']['code']} — {card['product']['name']}"),
        ("Класс страхования", f"{inp.class_code} — {class_name[0]['name']}" if class_name else inp.class_code),
        ("Тип объекта", obj["object_type"]),
        ("Адрес / регион", where),
        ("Страховая стоимость", money(obj["value_amount"])),
        ("Страховая сумма", money(obj["sum_insured"])),
        ("Франшиза", franchise),
        ("Срок страхования", term),
    ])

    # 3. Цена
    pdf.heading("Стоимость")
    pdf.big_number("Страховая премия за весь срок", money(calc["premium"]),
                   f"Ставка {pct(calc['applied_rate_pct'])} годовых от страховой суммы"
                   + ("" if inp.term_days == 365 else f", пересчитана на {inp.term_days} {days_word(inp.term_days)}"))
    pdf.space(4)
    pdf.para("Из чего сложилась ставка", size=10.5, bold=True, gap=3)
    pdf.table([("Составляющая", CONTENT_W - 190, "l"), ("Действие", 95, "r"), ("Ставка", 95, "r")],
              _chain_rows(expl.get("chain") or [], calc, expl))

    # 4. Покрытие
    pdf.heading("Что покрывается и что исключено")
    if included:
        pdf.para("Включённые риски", size=10, bold=True, gap=2)
        pdf.para(", ".join(included) + ".", gap=6)
    else:
        pdf.para("Покрытие по классу целиком, без выбора отдельных рисков.", gap=6)
    if excluded:
        pdf.para("Исключённые риски", size=10, bold=True, gap=2)
        pdf.para(", ".join(excluded) + ". Ущерб от этих событий не возмещается.", gap=6)
    pdf.para("Полный перечень исключений — в правилах страхования по продукту.", size=9, color=GRAY)

    # 5. Как снизить стоимость
    pdf.heading("Как снизить стоимость")
    if card["recommendations"]:
        rows = [[t["text"], t["kind"], "экономия " + money(-t["premium_delta"]) if t["premium_delta"] else "—"]
                for t in card["recommendations"]]
        pdf.table([("Что изменить", CONTENT_W - 250, "l"), ("Направление", 100, "l"), ("Эффект на премию", 150, "r")], rows)
    else:
        pdf.para("Дополнительных способов снизить премию по этому объекту не найдено.")

    # 6. Предупредительные мероприятия
    pdf.heading("Предупредительные мероприятия")
    measures = fresh.get("preventive_measures") or []
    if measures:
        pdf.para("Меры, снижающие вероятность убытка. Отмеченные как условие договора должны быть выполнены в указанный срок.",
                 size=9.5, color=GRAY, gap=5)
        for m in measures:
            status = f"условие договора, срок {m['deadline_days']} {days_word(m['deadline_days'])}" if m["mandatory"] else "рекомендация"
            effect = ""
            if m["premium_delta"] is not None and m["premium_delta"] != 0:
                effect = f" Эффект на премию: {'минус ' if m['premium_delta'] < 0 else 'плюс '}{money(abs(m['premium_delta']))}."
            pdf.para(m["measure"], size=10, bold=True, gap=1)
            pdf.para(f"{m['why']} Статус: {status}.{effect}", size=9.5, color=GRAY, gap=6)
    else:
        pdf.para("По этому объекту дополнительных мер не требуется.")

    # 7. Обязательная информация
    pdf.heading("Обязательная информация для страхователя")
    pdf.para("Раскрывается до заключения договора в соответствии со статьёй 63 Закона «О страховой деятельности».",
             size=9.5, color=GRAY, gap=6)
    agent = card["agent"]["name"] if card["agent"] else (req["branch"] or "уполномоченный сотрудник компании")
    pdf.bullet(f"Страховщик — СО АО «INSON». Предложение подготовлено: {agent}.")
    pdf.bullet(f"Цена договора — страховая премия {money(calc['premium'])} при ставке {pct(calc['applied_rate_pct'])} годовых "
               f"от страховой суммы {money(obj['sum_insured'])}.")
    pdf.bullet("Возврат премии при досрочном расторжении: если страхователь отказывается от договора, страховщик возвращает "
               "часть премии пропорционально неистёкшему сроку страхования, за вычетом понесённых расходов на ведение дела, "
               "если договором не предусмотрено иное.")
    if under:
        share = obj["sum_insured"] / obj["value_amount"] * 100
        pdf.bullet(f"Неполное страхование: страховая сумма составляет {share:.0f}% страховой стоимости. По статье 936 "
                   f"Гражданского кодекса при наступлении страхового случая возмещение выплачивается в той же доле — "
                   f"пропорционально отношению страховой суммы к страховой стоимости.")
    if over:
        pdf.bullet(f"Страховая сумма выше страховой стоимости на {money(obj['sum_insured'] - obj['value_amount'])}. "
                   "По статье 938 Гражданского кодекса договор в части превышения недействителен; премия за эту часть не подлежит возврату. "
                   "Рекомендуем привести сумму к стоимости.")
    else:
        pdf.bullet("Страховая сумма не может превышать страховую стоимость имущества: по статье 938 Гражданского кодекса "
                   "договор в части превышения недействителен.")
    pdf.bullet("Порядок урегулирования претензий: о событии, имеющем признаки страхового случая, страхователь уведомляет "
               "страховщика в срок и способом, установленными договором, и представляет документы по перечню. Решение о выплате "
               "или об отказе страховщик принимает не позднее 15 дней с момента получения всех документов (статья 955 Гражданского кодекса); "
               "отказ направляется письменно с указанием причин.")

    # 8. Подвал — не разрывать между страницами
    pdf.space(6)
    pdf.ensure(80)
    pdf.rule()
    missing = _missing_docs(card["checks"])
    if missing:
        pdf.para("Расчёт предварительный до предоставления документов: " + "; ".join(missing) + ".", size=9, color=GRAY, gap=3)
    else:
        pdf.para("Расчёт выполнен по представленным документам; окончательные условия — в договоре страхования.", size=9, color=GRAY, gap=3)
    if uncalibrated:
        pdf.para("Базовые ставки и коэффициенты — экспертные, до калибровки по статистике компании.", size=9, color=GRAY, gap=3)
    pdf.para("Предложение не является публичной офертой и действует до изменения тарифной политики компании.", size=9, color=GRAY)

    return pdf.finish()


@router.get("/requests/{rid}/proposal.pdf")
def proposal_pdf(rid: int):
    pdf_bytes = build_proposal(rid)
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="proposal_{rid}.pdf"'})
