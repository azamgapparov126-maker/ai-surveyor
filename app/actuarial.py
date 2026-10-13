"""
Актуарный отчёт по субпортфелям (ТЗ разработчику 13.10.2026, день 2.3): таблица actuarial_reports — заработанная
премия, выплаты, расходы на ведение дела, агентская комиссия, рисковая надбавка, нагрузка L по году и субпортфелю.

Загрузка 2022 года — tools/actuarial_load.py: разбор текста «Актуарный отчёт INSON за 2022 год.txt» (стр. 8 — премии
по субпортфелям, стр. 14–15 — итоги, стр. 19–39 — разделы субпортфелей) с проверкой сумм: Σ заработанной премии =
итог стр. 8 (73 566,68), Σ выплат и Σ расходов — итоги стр. 15. Не сошлось — ничего не пишется.

Маршруты (администратор):
  GET /reference/actuarial-reports?year=2022   — строки года (по умолчанию последний год), итоги, настройка load
  PUT /reference/actuarial-reports             — {year, source, items: [...]} — правка строк года
GET отвечает из app/main.py (путь /reference/{name} там общий для справочников) через list_reports.
"""
import re
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from . import db
from .auth import check_role, optional_user, require

router = APIRouter()

ADMIN = "админ"
UNIT = "млн сум"
TOL = 0.05                     # допуск сверки сумм, млн сум: в отчёте суммы округлены до сотых
REPORT_FILE = db.ROOT / "library" / "02_Компания_INSON" / "Актуарный отчёт INSON за 2022 год.txt"
SOURCE_2022 = "Актуарный отчёт INSON за 2022 год (library/02_Компания_INSON), стр. 8, 14–39"

# субпортфели отчёта: код → (название в таблице стр. 8, начало раздела в тексте)
SUBPORTFOLIOS = {
    "vehicle": ("Страхование ТС", r"Страхование ТС\|Данный субпортфель"),
    "aviation": ("Авиастрахование", r"Авиастрахование\|Андеррайтинговый результат"),
    "property": ("Имущество + СМР", r"Страхование имущества и СМР\|"),
    "liability": ("Ответственность", r"Страхование ответственности\|"),
    "financial": ("Фин. риски", r"Страхование финансовых/кредитных рисков\|"),
    "osgovts": ("ОСГОВТС", r"\(ОСГОВТС\)\|Данный портфель"),
    "osgor": ("ОСГОР", r"\(ОСГОР\)\|Данный портфель"),
    "other": ("Прочие", r"Портфель прочих рисков\|"),
}
NUM_KEYS = ("premium_written", "premium_earned", "payouts", "expenses", "expenses_pct", "commission_pct",
            "risk_margin", "load_L")


# --------------------------------------------------------------------------- #
#  Разбор текста отчёта
# --------------------------------------------------------------------------- #

_NUM_RX = re.compile(r"^-?\s?\d{1,3}(?:[\s ]\d{3})*(?:,\d+)?$|^-?\s?\d+(?:,\d+)?$")


def num(tok: str) -> Optional[float]:
    """«3 918,48» → 3918.48; «- 2 020,84» → −2020.84; «-» → 0.0 (прочерк отчёта = нет суммы); иначе None."""
    s = (tok or "").strip()
    if s in ("-", "–", "—"):
        return 0.0
    if not _NUM_RX.match(s):
        return None
    neg = s.startswith("-")
    v = float(re.sub(r"[\s -]", "", s).replace(",", "."))
    return -v if neg else v


def _pages(text: str) -> dict:
    out, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"=== стр (\d+) ===", line.strip())
        if m:
            cur = int(m.group(1))
            out[cur] = []
            continue
        if cur is not None:
            out[cur].append(line)
    return out


def _stream(lines: list) -> str:
    """Строки страниц без колонтитулов → «токен|токен|…» (одна ячейка таблицы — один токен)."""
    keep = []
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("АО СO") or s.startswith("Актуарный отчет за") or re.match(r"Страница \d+ из", s):
            continue
        keep.append(s)
    return "|" + "|".join(keep) + "|"


def _phrase(words: str) -> str:
    """Фраза с любыми пробелами и переносами ячеек между словами."""
    return r"[\s|]+".join(re.escape(w) for w in words.split())


def _after(sect: str, words: str, n: int = 1) -> Optional[list]:
    """n первых чисел (прочерк — 0) сразу после фразы в разделе; фразы нет — None."""
    m = re.search(_phrase(words), sect)
    if not m:
        return None
    toks = [t for t in sect[m.end():].split("|") if t.strip()]
    out = []
    for t in toks:
        v = num(t)
        if v is None:
            if out:
                break
            continue
        out.append(v)
        if len(out) == n:
            break
    return out


def parse_2022(text: str) -> dict:
    """
    Текст отчёта → {"items": {код: строка}, "totals": {...}, "errors": [...]}. Суммы — млн сум, брутто.
    expenses_pct — расходы к заработанной премии, %; commission_pct — агентская комиссия 2022 года из «структуры
    страховой премии» субпортфеля; risk_margin — рисковая надбавка 2022 года (прочерк — не оценивалась, None).
    """
    pages = _pages(text)
    errors = []
    # стр. 8: подписанная и заработанная премия по субпортфелям и «Итого»
    p8 = [s.strip() for s in pages.get(8, []) if s.strip()]
    table = {}
    names = {v[0]: k for k, v in SUBPORTFOLIOS.items()}
    names["Итого"] = "total"
    for i, s in enumerate(p8):
        if s in names:
            vals = [num(x) for x in p8[i + 1:i + 5]]
            if len(vals) == 4 and all(v is not None for v in vals[:2]):
                table[names[s]] = {"premium_written": vals[0], "premium_earned": vals[1]}
    for k in list(SUBPORTFOLIOS) + ["total"]:
        if k not in table:
            errors.append(f"стр. 8: нет строки «{SUBPORTFOLIOS[k][0] if k in SUBPORTFOLIOS else 'Итого'}»")
    # стр. 14–15: итоги портфеля
    s1415 = _stream(pages.get(14, []) + pages.get(15, []))
    totals = {"premium_written": (table.get("total") or {}).get("premium_written"),
              "premium_earned": (table.get("total") or {}).get("premium_earned"),
              "payouts": (_after(s1415, "Выплаченные страховые возмещения брутто") or [None])[0],
              "expenses": (_after(s1415, "материалы и пр.") or [None])[0]}
    # стр. 19–40: разделы субпортфелей
    body = _stream([ln for n in range(19, 41) for ln in pages.get(n, [])])
    starts = []
    for k, (_name, rx) in SUBPORTFOLIOS.items():
        m = re.search(rx, body)
        if not m:
            errors.append(f"раздел «{SUBPORTFOLIOS[k][0]}» не найден на стр. 19–39")
            continue
        starts.append((m.start(), k))
    starts.sort()
    items = {}
    for j, (pos, k) in enumerate(starts):
        sect = body[pos:starts[j + 1][0] if j + 1 < len(starts) else len(body)]
        earned_s = (_after(sect, "Заработанная премия брутто") or [None])[0]
        pay = (_after(sect, "Выплаченные страховые возмещения брутто") or [None])[0]
        exp = _after(sect, "материалы и пр.", 2) or []
        # «структура страховой премии»: столбцы 2021 и 2022 или только 2022 — по числу «100,0» у «Страховая премия»
        cols = _after(sect, "Страховая премия", 2) or []
        ncol = sum(1 for v in cols if v == 100.0) or 1
        com = _after(sect, "Агентская комиссия", ncol) or []
        rm = re.search(_phrase("Рисковая надбавка в составе нетто-премии (по отчетному году)"), sect)
        risk = None
        if rm:
            tok = next((t.strip() for t in sect[rm.end():].split("|") if t.strip()), "")
            risk = None if tok in ("-", "–") else num(tok)
        row = dict(table.get(k) or {})
        if earned_s is not None and row.get("premium_earned") is not None \
                and abs(earned_s - row["premium_earned"]) > TOL:
            errors.append(f"{SUBPORTFOLIOS[k][0]}: заработанная премия стр. 8 ({row['premium_earned']}) и раздела "
                          f"({earned_s}) расходятся")
        row.update(payouts=pay, expenses=exp[0] if exp else None, expenses_pct=exp[1] if len(exp) > 1 else None,
                   commission_pct=com[-1] if len(com) == ncol else None, risk_margin=risk)
        for key in ("premium_earned", "payouts", "expenses", "commission_pct"):
            if row.get(key) is None:
                errors.append(f"{SUBPORTFOLIOS[k][0]}: не найдено «{key}»")
        if row.get("premium_earned") and row.get("expenses") is not None:
            row["load_L"] = round(row["expenses"] / row["premium_earned"], 3)
            if row.get("expenses_pct") is not None and abs(row["expenses"] / row["premium_earned"] * 100
                                                           - row["expenses_pct"]) > 0.1:
                errors.append(f"{SUBPORTFOLIOS[k][0]}: расходы {row['expenses']} и их доля {row['expenses_pct']}% "
                              "не сходятся")
        items[k] = row
    errors += check_totals(items, totals)
    return {"items": items, "totals": totals, "errors": errors}


def check_totals(items: dict, totals: dict) -> list:
    """Σ по субпортфелям = итоги отчёта (допуск TOL млн сум на округление)."""
    errs = []
    for key, what in (("premium_earned", "заработанной премии"), ("premium_written", "подписанной премии"),
                      ("payouts", "выплат"), ("expenses", "расходов на ведение дела")):
        tot = totals.get(key)
        if tot is None:
            errs.append(f"нет итога {what} в отчёте")
            continue
        s = sum(float((r or {}).get(key) or 0) for r in items.values())
        if abs(s - tot) > TOL:
            errs.append(f"сумма {what} по субпортфелям {s:,.2f} ≠ итог отчёта {tot:,.2f}".replace(",", " "))
    return errs


# --------------------------------------------------------------------------- #
#  База
# --------------------------------------------------------------------------- #

def save_year(con, year: int, items: dict, source: str, who: str, pages: Optional[str] = None) -> dict:
    """Строки года (upsert по году и субпортфелю); прежние значения — в журнал audit."""
    now = db.now()
    before = {r["subportfolio"]: r for r in db.rows(con, "SELECT * FROM actuarial_reports WHERE year=?", year)}
    n_new = n_upd = 0
    for k, r in items.items():
        vals = (SUBPORTFOLIOS.get(k, (r.get("name") or k,))[0] if not r.get("name") else r["name"],
                r.get("premium_written"), r["premium_earned"], r.get("payouts") or 0, r.get("expenses"),
                r.get("expenses_pct"), r.get("commission_pct"), r.get("risk_margin"), r.get("load_L"), UNIT,
                pages, source, now, who)
        if k in before:
            con.execute("UPDATE actuarial_reports SET name=?, premium_written=?, premium_earned=?, payouts=?, "
                        "expenses=?, expenses_pct=?, commission_pct=?, risk_margin=?, load_L=?, unit=?, pages=?, "
                        "source=?, updated_at=?, updated_by=? WHERE year=? AND subportfolio=?", vals + (year, k))
            n_upd += 1
        else:
            con.execute("INSERT INTO actuarial_reports (name, premium_written, premium_earned, payouts, expenses, "
                        "expenses_pct, commission_pct, risk_margin, load_L, unit, pages, source, updated_at, "
                        "updated_by, year, subportfolio) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", vals + (year, k))
            n_new += 1
    db.audit(con, who, "актуарный отчёт", f"year:{year}",
             {"source": source, "added": n_new, "updated": n_upd,
              "before": {k: {x: v.get(x) for x in NUM_KEYS} for k, v in before.items() if k in items}})
    return {"year": year, "added": n_new, "updated": n_upd}


def load_2022(con, who: str = "система: актуарный отчёт", path: Optional[Path] = None) -> dict:
    """Разбор отчёта 2022 и запись; ошибка сверки — ValueError, ничего не записано."""
    p = Path(path or REPORT_FILE)
    if not p.exists():
        raise ValueError(f"нет файла отчёта: {p}")
    res = parse_2022(p.read_text(encoding="utf-8"))
    if res["errors"]:
        db.audit(con, who, "актуарный отчёт не загружен", "year:2022", {"ошибки": res["errors"][:20]})
        raise ValueError("сверка не прошла: " + "; ".join(res["errors"]))
    saved = save_year(con, 2022, res["items"], SOURCE_2022, who, pages="8, 14–39")
    return dict(saved, totals=res["totals"], items=res["items"])


def ensure_loaded() -> Optional[dict]:
    """Пустая таблица и файл отчёта на месте — загрузить 2022 (при старте сервера); иначе ничего."""
    with db.tx() as con:
        if con.execute("SELECT 1 FROM actuarial_reports LIMIT 1").fetchone() or not REPORT_FILE.exists():
            return None
        return load_2022(con)


def list_reports(con, year: Optional[int] = None) -> dict:
    from .act_pkg.common import load_settings
    years = [r["year"] for r in db.rows(con, "SELECT DISTINCT year FROM actuarial_reports ORDER BY year")]
    y = year if year is not None else (years[-1] if years else None)
    rows = db.rows(con, "SELECT * FROM actuarial_reports WHERE year=? ORDER BY premium_earned DESC", y) \
        if y is not None else []
    tot = {k: round(sum(float(r.get(k) or 0) for r in rows), 2) for k in ("premium_written", "premium_earned",
                                                                          "payouts", "expenses")}
    tot["loss_ratio_pct"] = round(tot["payouts"] / tot["premium_earned"] * 100, 1) if tot["premium_earned"] else None
    tot["expenses_pct"] = round(tot["expenses"] / tot["premium_earned"] * 100, 1) if tot["premium_earned"] else None
    st = load_settings(con)
    return {"ok": True, "years": years, "year": y, "unit": UNIT, "items": rows, "totals": tot,
            "subportfolios": {k: v[0] for k, v in SUBPORTFOLIOS.items()}, "load_settings": st.get("load")}


def respond_list(request: Request, year: Optional[str]):
    """GET /reference/actuarial-reports (вызывается из app/main.py): только администратор."""
    user = request.scope.get("surveyor_user") or optional_user(request)
    if not user:
        raise HTTPException(401, "нужен вход")
    if user.get("role") != ADMIN:
        raise HTTPException(403, "нужны права администратора")
    check_role(user, (ADMIN,))
    y = None
    if year not in (None, ""):
        try:
            y = int(year)
        except ValueError:
            return JSONResponse({"ok": False, "detail": "year: год числом"}, status_code=422)
    with db.tx() as con:
        return list_reports(con, y)


def check_items(year, items) -> tuple:
    """Правка строк года (недоверенный ввод) → (чистые строки {код: строка}, ошибки)."""
    errs = []
    try:
        y = int(year)
    except (TypeError, ValueError):
        return None, {}, ["year: год числом"]
    if not 2000 <= y <= date.today().year:
        errs.append(f"year: от 2000 до {date.today().year}")
    if not isinstance(items, list) or not items:
        return y, {}, errs + ["items: список строк {subportfolio, premium_earned, payouts, …}"]
    out = {}
    for i, it in enumerate(items, start=1):
        if not isinstance(it, dict):
            errs.append(f"строка {i}: объект")
            continue
        k = str(it.get("subportfolio") or "").strip()
        if k not in SUBPORTFOLIOS:
            errs.append(f"строка {i}: subportfolio — одно из {', '.join(SUBPORTFOLIOS)}")
            continue
        if k in out:
            errs.append(f"строка {i}: субпортфель {k} повторяется")
            continue
        row = {"name": str(it.get("name") or SUBPORTFOLIOS[k][0]).strip()[:80]}
        for key in NUM_KEYS:
            v = it.get(key)
            if v in (None, ""):
                row[key] = None
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                errs.append(f"строка {i}: {key} — число")
                continue
            row[key] = float(v)
        if row.get("premium_earned") is None or row["premium_earned"] <= 0:
            errs.append(f"строка {i}: premium_earned — число больше 0")
        for key in ("premium_written", "payouts", "expenses", "risk_margin"):
            if row.get(key) is not None and row[key] < 0:
                errs.append(f"строка {i}: {key} не меньше 0")
        for key in ("expenses_pct", "commission_pct"):
            if row.get(key) is not None and not 0 <= row[key] <= 100:
                errs.append(f"строка {i}: {key} — от 0 до 100 (%)")
        if row.get("load_L") is not None and not 0 <= row["load_L"] < 1:
            errs.append(f"строка {i}: load_L — доля от 0 до 1")
        if row.get("load_L") is None and row.get("expenses") is not None and row.get("premium_earned"):
            row["load_L"] = round(row["expenses"] / row["premium_earned"], 3)
        if row.get("expenses_pct") is None and row.get("expenses") is not None and row.get("premium_earned"):
            row["expenses_pct"] = round(row["expenses"] / row["premium_earned"] * 100, 1)
        out[k] = row
    return y, out, errs


@router.put("/reference/actuarial-reports")
def actuarial_put(body: dict = Body(...), user: dict = Depends(require(ADMIN))):
    """Правка строк года (администратор): {year, source, items: [{subportfolio, premium_earned, payouts, expenses,
    commission_pct, risk_margin, load_L?, premium_written?, expenses_pct?, name?}], total_premium_earned?}.
    total_premium_earned — итог отчёта для сверки суммы строк (допуск 0,05 млн сум)."""
    body = body if isinstance(body, dict) else {}
    y, items, errs = check_items(body.get("year"), body.get("items"))
    source = str(body.get("source") or "").strip()[:300]
    if not source:
        errs.append("source: откуда цифры (документ, страница)")
    tot = body.get("total_premium_earned")
    if tot not in (None, "") and not errs:
        if isinstance(tot, bool) or not isinstance(tot, (int, float)):
            errs.append("total_premium_earned: число")
        else:
            s = sum(r["premium_earned"] for r in items.values())
            if abs(s - float(tot)) > TOL:
                errs.append(f"сумма заработанной премии строк {s:.2f} ≠ итог {float(tot):.2f}")
    if errs:
        return JSONResponse({"ok": False, "errors": errs, "detail": "; ".join(errs)}, status_code=422)
    with db.tx() as con:
        saved = save_year(con, y, items, source, user.get("login") or ADMIN)
        return dict(list_reports(con, y), saved=saved)
