"""
Части продуктов (ТЗ разработчику 13.10.2026, день 2.1–2.2): у продукта — части в своих классах, у части — тип
клиента (fiz | yur | both), базовая и минимальная ставка, тип ставки, база суммы, доля, лимит, франшиза.

Версии. Как у минимальных ставок (app/min_rates.py): каждая правка (API, Excel) — новая версия тарифа
(tariff_versions, уровень «компания», effective_from = valid_from) и строка product_part_versions; строки
тарифной политики (source = policy, tools/product_parts_migrate.py) ссылаются на версию политики. Старые версии не
меняются (правило проекта № 9). На дату D у продукта действует версия с наибольшей valid_from ≤ D (при равных —
больший id версии); её строки — весь состав частей продукта.

Расчёт (rate_part). Акт берёт ставку части, если она есть в действующей версии и эта версия не старше
действующей минимальной ставки продукта (правка администратора в min_rates новее частей — прежний путь). Нет части
или у неё нет ставки — прежний путь (rate_text и min_rates), контрольные премии не меняются.

Маршруты:
  GET  /reference/product-parts/template.xlsx   — шаблон Excel
  POST /reference/product-parts/import          — файл → предпросмотр и token (администратор)
  POST /reference/product-parts/import/apply    — {token} → новые версии (администратор)
  GET  /reference/product-parts/{code}          — части продукта на дату (?date=ГГГГ-ММ-ДД&client_type=fiz|yur)
  PUT  /reference/product-parts/{code}          — новая версия частей одного продукта JSON-ом (администратор)
"""
import json
import re
from datetime import date
from typing import Optional

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from . import db, min_rates, xlsx_import as xi
from .auth import require

router = APIRouter()

ADMIN = "админ"
LEVEL = "компания"
CLIENT_TYPES = ("fiz", "yur", "both")
CLIENT_WORDS = {"fiz": "fiz", "физ": "fiz", "физлицо": "fiz", "физическое лицо": "fiz", "jismoniy": "fiz",
                "yur": "yur", "юр": "yur", "юрлицо": "yur", "юридическое лицо": "yur", "yuridik": "yur",
                "both": "both", "все": "both", "оба": "both", "любой": "both", "": "both"}
PAYER_OF = {"fiz": "физ", "yur": "юр"}              # тип клиента → payer_type справочника min_rates
CLIENT_OF = {"физ": "fiz", "юр": "yur", None: "both"}
CLIENT_LABEL = {"fiz": "физлицо", "yur": "юрлицо", "both": "любой клиент"}
CLIENT_GEN = {"fiz": "физлица", "yur": "юрлица"}
SUM_BASES = ("value", "limit", "share")
SRC_POLICY, SRC_EXAMPLE, SRC_ADMIN, SRC_IMPORT = "policy", "tz_example", "admin", "import"
POLICY_SOURCES = (SRC_POLICY,)                      # строки тарифной политики: значения те же, что прежний путь
POLICY_NAME = "Тарифная политика INSON"
FILE_MAX = 2 * 1024 * 1024
MAX_ROWS = 2000
MAX_PARTS = 20
MAX_TEXT = 300
SHARE_EPS = 1e-9

SCHEMA_SQL = [
    """CREATE TABLE IF NOT EXISTS product_part_versions (
        tariff_version_id INTEGER PRIMARY KEY REFERENCES tariff_versions(id),
        product_code      TEXT NOT NULL,
        valid_from        TEXT NOT NULL,
        source            TEXT NOT NULL,
        created_at        TEXT NOT NULL,
        created_by        TEXT,
        note              TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS product_parts (
        id                INTEGER PRIMARY KEY,
        tariff_version_id INTEGER NOT NULL REFERENCES tariff_versions(id),
        product_code      TEXT NOT NULL REFERENCES products(code),
        part_no           INTEGER NOT NULL,
        class_code        TEXT NOT NULL REFERENCES classes(code),
        perils            TEXT NOT NULL DEFAULT '[]',
        client_type       TEXT NOT NULL DEFAULT 'both',
        base_rate_pct     REAL,
        min_rate_pct      REAL,
        rate_type         TEXT NOT NULL DEFAULT 'annual',
        sum_basis         TEXT NOT NULL DEFAULT 'value',
        default_share     REAL,
        limit_amount      REAL,
        deductible        REAL,
        mandatory         INTEGER NOT NULL DEFAULT 1,
        sort_order        INTEGER NOT NULL DEFAULT 1,
        valid_from        TEXT NOT NULL,
        rules_ref         TEXT,
        source            TEXT NOT NULL,
        created_at        TEXT NOT NULL,
        created_by        TEXT,
        note              TEXT,
        UNIQUE (tariff_version_id, product_code, part_no, client_type)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_product_parts_product ON product_parts(product_code, valid_from)",
]

PUBLIC_KEYS = ("product_code", "part_no", "class_code", "perils", "client_type", "base_rate_pct", "min_rate_pct",
               "rate_type", "sum_basis", "default_share", "limit", "deductible", "mandatory", "order", "valid_from",
               "rules_ref")


def ensure(con) -> None:
    for sql in SCHEMA_SQL:
        con.execute(sql)


# --------------------------------------------------------------------------- #
#  Чтение и выбор версии
# --------------------------------------------------------------------------- #

def _public(r: dict) -> dict:
    """Строка таблицы → вид API (limit, order, perils списком, mandatory да/нет)."""
    try:
        perils = json.loads(r.get("perils") or "[]")
    except ValueError:
        perils = []
    return {"product_code": r["product_code"], "part_no": int(r["part_no"]), "class_code": r["class_code"],
            "perils": perils if isinstance(perils, list) else [], "client_type": r["client_type"],
            "base_rate_pct": r["base_rate_pct"], "min_rate_pct": r["min_rate_pct"], "rate_type": r["rate_type"],
            "sum_basis": r["sum_basis"], "default_share": r["default_share"], "limit": r["limit_amount"],
            "deductible": r["deductible"], "mandatory": bool(r["mandatory"]), "order": int(r["sort_order"]),
            "valid_from": r["valid_from"], "rules_ref": r["rules_ref"], "source": r["source"],
            "created_at": r["created_at"], "tariff_version_id": int(r["tariff_version_id"]), "note": r.get("note")}


def _rows(con, code: str) -> list:
    ensure(con)
    return db.rows(con, "SELECT * FROM product_parts WHERE product_code=? ORDER BY valid_from, tariff_version_id, "
                        "sort_order, part_no, client_type", code)


def _iso(as_of) -> str:
    if as_of is None:
        return date.today().isoformat()
    d = min_rates.parse_date(as_of)
    return (d or date.today()).isoformat()


def versions(con, code: str) -> list:
    """Версии частей продукта от ранней к поздней: {tariff_version_id, valid_from, source, created_at, parts}."""
    out = {}
    for r in _rows(con, code):
        k = (str(r["valid_from"]), int(r["tariff_version_id"]))
        v = out.setdefault(k, {"tariff_version_id": k[1], "valid_from": k[0], "source": r["source"],
                               "created_at": r["created_at"], "created_by": r.get("created_by"), "parts": 0})
        v["parts"] += 1
    return [out[k] for k in sorted(out)]


def version_on(con, code: str, as_of=None) -> Optional[dict]:
    """Версия частей продукта, действующая на дату (ISO или date)."""
    d = _iso(as_of)
    act = [v for v in versions(con, code) if v["valid_from"] <= d]
    return act[-1] if act else None


def pick_client(rows: list, client_type: Optional[str]) -> list:
    """Строки на тип клиента: по каждому номеру части — строка своего типа, иначе both. None — все строки."""
    if client_type not in ("fiz", "yur"):
        return list(rows)
    by_no = {}
    for r in rows:
        if r["client_type"] not in (client_type, "both"):
            continue
        cur = by_no.get(r["part_no"])
        if cur is None or (cur["client_type"] == "both" and r["client_type"] == client_type):
            by_no[r["part_no"]] = r
    return sorted(by_no.values(), key=lambda r: (r["order"], r["part_no"]))


def parts_on(con, code: str, as_of=None, client_type: Optional[str] = None) -> dict:
    """Части продукта на дату: {"version": …|None, "parts": [вид API]}."""
    v = version_on(con, code, as_of)
    if not v:
        return {"version": None, "parts": []}
    rows = [_public(r) for r in _rows(con, code) if int(r["tariff_version_id"]) == v["tariff_version_id"]
            and str(r["valid_from"]) == v["valid_from"]]
    return {"version": v, "parts": pick_client(rows, client_type)}


def rate_part(con, code: Optional[str], cls: str, client_type: Optional[str], as_of=None) -> Optional[dict]:
    """
    Часть продукта для ставки акта: класс cls, тип клиента; None — прежний путь (rate_text и min_rates).
    Часть не берётся, если у неё нет ни базовой, ни минимальной ставки или если минимальная ставка продукта
    (min_rates, уровень «компания») на эту дату — версия новее частей: правка администратора не теряется.
    """
    if not code or not cls:
        return None
    try:
        got = parts_on(con, code, as_of, client_type if client_type in ("fiz", "yur") else None)
    except Exception as e:            # таблицы ещё нет или она повреждена — прежний путь, причина в журнал сервера
        print("части продукта не прочитаны:", type(e).__name__)
        return None
    v = got["version"]
    rows = [p for p in got["parts"] if p["class_code"] == cls]
    if client_type not in ("fiz", "yur"):
        # тип клиента не известен: строки «физ / юр» не выбираются — прежний путь (наибольшая из ставок)
        rows = [p for p in rows if p["client_type"] == "both"]
    if not v or not rows:
        return None
    part = min(rows, key=lambda p: (p["part_no"], p["order"]))
    if part["base_rate_pct"] is None and part["min_rate_pct"] is None:
        return None
    mi = min_rates.on_date(con, code, _iso(as_of), PAYER_OF.get(client_type))
    if mi and (str(mi["effective_from"]), int(mi["version_id"])) > (v["valid_from"], v["tariff_version_id"]):
        return None
    return part


def default_client_type(product: Optional[dict], settings: dict) -> tuple:
    """(fiz | yur, откуда): по словам в названии продукта (настройка client_type.words), иначе по умолчанию."""
    cfg = (settings or {}).get("client_type") or {}
    text = " ".join(str((product or {}).get(k) or "") for k in ("code", "name")).lower()
    for ct in ("fiz", "yur"):
        if any(str(w).lower() in text for w in (cfg.get("words") or {}).get(ct) or []):
            return ct, "product"
    return str(cfg.get("default") or "yur"), "default"


# --------------------------------------------------------------------------- #
#  Проверка строк (Excel и JSON — одни правила)
# --------------------------------------------------------------------------- #

def _num(v, what: str, errors: list, lo=None, hi=None, lo_open=False) -> Optional[float]:
    n, err = xi.as_number(v)
    if err:
        errors.append(f"{what}: {err}")
        return None
    if n is None:
        return None
    if (lo is not None and (n < lo or (lo_open and n <= lo))) or (hi is not None and n > hi):
        rng = ("больше " if lo_open else "от ") + f"{lo:g}" + (f" до {hi:g}" if hi is not None else "")
        errors.append(f"{what}: число {rng}")
        return None
    return round(n, 6)


def _bool(v) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    s = str(v if v is not None else "").strip().lower()
    if s in ("", "1", "да", "ha", "yes", "true", "обязательная", "1.0"):
        return True
    if s in ("0", "нет", "yoʻq", "yo'q", "no", "false", "0.0", "необязательная"):
        return False
    return None


def _perils(v) -> list:
    if isinstance(v, list):
        items = v
    else:
        items = re.split(r"[,;\s]+", str(v or "").strip())
    out = []
    for x in items:
        s = str(x).strip()[:40]
        if s and s not in out:
            out.append(s)
    return out


def _ddmmyyyy(iso: str) -> str:
    d = min_rates.parse_date(iso)
    return d.strftime("%d.%m.%Y") if d else str(iso)


def _pct_ru(x) -> str:
    return f"{x:g}".replace(".", ",") + "%"


def check_row(con, v: dict, known_classes: set, today: date) -> tuple:
    """Одна строка (сырые значения) → (чистая строка, ошибки, предупреждения)."""
    errors, warnings = [], []
    code = xi.product_code(v.get("product_code"))
    if not code:
        errors.append("Код продукта: пустой")
    pno = _num(v.get("part_no"), "№ части", errors, 1, MAX_PARTS)
    if pno is None and not any(e.startswith("№ части") for e in errors):
        errors.append("№ части: целое число от 1")
    elif pno is not None and pno != int(pno):
        errors.append("№ части: целое число")
    cls = xi.as_text(v.get("class_code"), 10)
    if not cls:
        errors.append("Класс: пустой")
    elif cls not in known_classes:
        errors.append(f"Класс: «{cls}» нет в справочнике классов")
    ct_raw = str(v.get("client_type") if v.get("client_type") is not None else "").strip().lower()
    ct = CLIENT_WORDS.get(ct_raw)
    if ct is None:
        errors.append("Тип клиента: fiz (физлицо), yur (юрлицо) или both (любой)")
    base = _num(v.get("base_rate_pct"), "Базовая ставка", errors, 0, min_rates.MAX_PCT, lo_open=True)
    mn = _num(v.get("min_rate_pct"), "Минимальная ставка", errors, 0, min_rates.MAX_PCT, lo_open=True)
    if base is None and not any(e.startswith("Базовая") for e in errors):
        errors.append("Базовая ставка: нужна (в процентах, 0,5 — это 0,5%)")
    if mn is None and not any(e.startswith("Минимальная") for e in errors):
        errors.append("Минимальная ставка: нужна (в процентах)")
    if base is not None and mn is not None and base + 1e-12 < mn:
        errors.append(f"Ставка: базовая {_pct_ru(base)} ниже минимальной {_pct_ru(mn)} — базовая ставка части "
                      "должна быть не ниже минимума")
    rt = min_rates.parse_rate_type(v.get("rate_type"))
    if rt is None:
        errors.append("Тип ставки: annual (годовая) или fixed (на весь срок)")
    sb = str(v.get("sum_basis") or "value").strip().lower()
    if sb not in SUM_BASES:
        errors.append("База суммы: value (стоимость), limit (лимит) или share (доля)")
    share = _num(v.get("default_share"), "Доля по умолчанию", errors, 0, 1)
    limit = _num(v.get("limit"), "Лимит", errors, 0)
    ded = _num(v.get("deductible"), "Франшиза", errors, 0, 100)
    mand = _bool(v.get("mandatory"))
    if mand is None:
        errors.append("Обязательная: да или нет")
    order = _num(v.get("order"), "Порядок", errors, 1, 1000)
    d, derr = xi.as_date(v.get("valid_from"))
    if derr:
        errors.append("Действует с: " + derr)
    elif d is None:
        errors.append("Действует с: нужна дата (ДД.ММ.ГГГГ)")
    elif d < today:
        errors.append(f"Действует с: {d.strftime('%d.%m.%Y')} — дата в прошлом; новая версия действует с сегодняшней "
                      "или будущей даты (прошлые расчёты должны воспроизводиться, правило проекта № 9)")
    elif d.year > today.year + 10:
        errors.append("Действует с: не дальше чем на 10 лет вперёд")
    rules = xi.as_text(v.get("rules_ref"), MAX_TEXT)
    perils = _perils(v.get("perils"))
    clean = {"product_code": code, "part_no": int(pno) if pno is not None else None, "class_code": cls,
             "perils": perils, "client_type": ct, "base_rate_pct": base, "min_rate_pct": mn, "rate_type": rt,
             "sum_basis": sb, "default_share": share, "limit": limit, "deductible": ded,
             "mandatory": True if mand is None else mand, "order": int(order) if order is not None else None,
             "valid_from": d.isoformat() if d else None, "rules_ref": rules or None}
    return clean, errors, warnings


def check_rows(con, raw_rows: list, today: Optional[date] = None) -> dict:
    """
    Строки (Excel или JSON) → предпросмотр. Проверки: ставка ≥ минимума, сумма долей обязательных частей ≤ 1
    (для физлица и для юрлица отдельно), класс есть в справочнике, дата не в прошлом; продукт есть и не обязательный
    вид; у продукта одна дата начала; номер части и тип клиента не повторяются. Ничего не пишет.
    """
    today = today or date.today()
    ensure(con)
    known = {r["code"] for r in db.rows(con, f"SELECT code FROM classes WHERE {db.CLASSES_SHOWN}")}
    items, by_prod = [], {}
    for raw in raw_rows:
        v = raw.get("values") if isinstance(raw.get("values"), dict) else raw
        clean, errors, warnings = check_row(con, v, known, today)
        items.append({"row": raw.get("row"), "part": clean, "errors": errors, "warnings": warnings})
        if clean["product_code"]:
            by_prod.setdefault(clean["product_code"], []).append(items[-1])
    products = []
    for code, its in by_prod.items():
        perrs, pwarn = [], []
        pr = db.rows(con, "SELECT code, name, pricing_mode FROM products WHERE code=?", code)
        if not pr:
            perrs.append(f"Продукт {code}: нет в справочнике продуктов")
        elif pr[0]["pricing_mode"] == "нормативный акт":
            perrs.append(f"Продукт {code}: обязательный вид — ставку устанавливает нормативный акт")
        dates = sorted({it["part"]["valid_from"] for it in its if it["part"]["valid_from"]})
        if len(dates) > 1:
            perrs.append(f"Продукт {code}: у частей разные даты начала ({', '.join(_ddmmyyyy(x) for x in dates)}) — "
                         "одна версия частей продукта действует с одной даты")
        seen = set()
        for it in its:
            k = (it["part"]["part_no"], it["part"]["client_type"])
            if None not in k and k in seen:
                it["errors"].append(f"№ части {k[0]} ({CLIENT_LABEL.get(k[1], k[1])}): повтор в файле")
            seen.add(k)
        # доли обязательных частей — отдельно для физлица и юрлица (часть «both» входит в обе)
        for ct in ("fiz", "yur"):
            rows = pick_client([dict(it["part"], order=it["part"]["order"] or it["part"]["part_no"] or 0)
                                for it in its if it["part"]["part_no"] is not None and it["part"]["client_type"]],
                               ct)
            total = sum(float(p["default_share"] or 0) for p in rows if p["mandatory"])
            if total > 1 + SHARE_EPS:
                perrs.append(f"Продукт {code}: сумма долей обязательных частей для {CLIENT_GEN[ct]} — "
                             + f"{total:g}".replace(".", ",") + " (больше 1); доля — часть страховой суммы договора, "
                             "0,6 — это 60%")
        if pr:
            have = [r["class_code"] for r in db.rows(
                con, "SELECT class_code FROM product_classes WHERE product_code=? ORDER BY part_no", code)]
            outside = sorted({it["part"]["class_code"] for it in its} - set(have) - {""})
            if outside:
                pwarn.append(f"класс {', '.join(outside)} не входит в состав продукта ({', '.join(have)}) — часть "
                             "сохранится, состав продукта не меняется")
            missing = [c for c in have if c not in {it["part"]["class_code"] for it in its}]
            if missing:
                pwarn.append(f"у класса {', '.join(missing)} продукта нет части — по нему расчёт прежним путём "
                             "(текст тарифа и минимальная ставка)")
        cur = parts_on(con, code, today) if pr else {"version": None, "parts": []}
        products.append({"product_code": code, "product_name": pr[0]["name"] if pr else None,
                         "valid_from": dates[0] if len(dates) == 1 else None, "parts": len(its),
                         "errors": perrs, "warnings": pwarn,
                         "now": {"version": cur["version"], "parts": cur["parts"]}})
    n_err = sum(1 for it in items if it["errors"]) + sum(1 for p in products if p["errors"])
    messages = [f"строка {it['row']}: {e}" if it.get("row") else e for it in items for e in it["errors"]] + \
        [e for p in products for e in p["errors"]]
    out = {"rows": len(items), "error_count": n_err, "items": items, "products": products,
           "can_apply": bool(items) and n_err == 0, "messages": messages}
    if n_err:
        out["detail"] = "Файл не будет сохранён — исправьте ошибки: " + "; ".join(messages[:10]) + \
            (f" … и ещё {len(messages) - 10}" if len(messages) > 10 else "")
    elif not items:
        out["detail"] = "В файле нет строк с частями"
    return out


# --------------------------------------------------------------------------- #
#  Запись
# --------------------------------------------------------------------------- #

def save_version(con, code: str, parts: list, who: str, source: str, note: str = "",
                 document_ref: Optional[str] = None) -> dict:
    """Новая версия частей продукта: версия тарифа + product_part_versions + строки частей. Ничего не удаляет."""
    ensure(con)
    min_rates.ensure(con)
    vf = parts[0]["valid_from"]
    what = {SRC_ADMIN: "правка администратора", SRC_IMPORT: "импорт из Excel",
            SRC_EXAMPLE: "учебный пример ТЗ 13.10.2026"}.get(source, source)
    name = f"Части продукта {code}"
    cur = con.execute("INSERT INTO tariff_versions (level, name, document_ref, effective_from) VALUES (?,?,?,?)",
                      (LEVEL, name, document_ref or what, vf))
    vid = cur.lastrowid
    now = db.now()
    con.execute("INSERT INTO product_part_versions (tariff_version_id, product_code, valid_from, source, created_at,"
                " created_by, note) VALUES (?,?,?,?,?,?,?)", (vid, code, vf, source, now, who, note or ""))
    for p in parts:
        _insert(con, vid, p, source, now, who)
    # журнал: «новая версия тарифов» version:<id> — признак ручной правки для app/refsync.py
    db.audit(con, who, "новая версия тарифов", f"version:{vid}",
             {"level": LEVEL, "name": name, "effective_from": vf, "source": source})
    db.audit(con, who, "части продукта", code, {"tariff_version_id": vid, "valid_from": vf, "source": source,
                                                "parts": [{k: p.get(k) for k in PUBLIC_KEYS} for p in parts]})
    db.reference_changed(con)
    return {"tariff_version_id": vid, "product_code": code, "valid_from": vf, "source": source, "parts": len(parts)}


def _insert(con, vid: int, p: dict, source: str, now: str, who: str) -> None:
    con.execute(
        "INSERT INTO product_parts (tariff_version_id, product_code, part_no, class_code, perils, client_type,"
        " base_rate_pct, min_rate_pct, rate_type, sum_basis, default_share, limit_amount, deductible, mandatory,"
        " sort_order, valid_from, rules_ref, source, created_at, created_by, note)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (vid, p["product_code"], int(p["part_no"]), p["class_code"], json.dumps(p.get("perils") or [],
                                                                              ensure_ascii=False),
         p.get("client_type") or "both", p.get("base_rate_pct"), p.get("min_rate_pct"), p.get("rate_type") or "annual",
         p.get("sum_basis") or "value", p.get("default_share"), p.get("limit"), p.get("deductible"),
         1 if p.get("mandatory", True) else 0, int(p.get("order") or p["part_no"]), p["valid_from"],
         p.get("rules_ref"), source, now, who, p.get("note")))


def apply_rows(con, raw_rows: list, who: str, source: str, file_name: str = "") -> dict:
    """Проверяет строки заново по текущей базе; есть ошибка — ничего не пишет (ValueError с текстом)."""
    pv = check_rows(con, raw_rows)
    if not pv["can_apply"]:
        raise ValueError(pv.get("detail") or "в файле есть ошибки — ничего не записано")
    saved = []
    for p in pv["products"]:
        parts = [it["part"] for it in pv["items"] if it["part"]["product_code"] == p["product_code"]]
        saved.append(save_version(con, p["product_code"], parts, who, source,
                                  note=(f"файл {file_name[:120]}" if file_name else "")))
    db.audit(con, who, "импорт частей продуктов", "product_parts",
             {"файл": file_name[:120], "строк": pv["rows"], "продуктов": len(saved), "источник": source})
    return {"ok": True, "saved": saved, "preview": pv}


# --------------------------------------------------------------------------- #
#  Миграция из тарифной политики (tools/product_parts_migrate.py)
# --------------------------------------------------------------------------- #

_CLIENT_RX = re.compile(r"(физ|юр)[^\d·;]*?(\d+(?:[.,]\d+)?)\s*%", re.I)


def client_rates_from_text(rate_text) -> dict:
    """«физ 1,2% · юр 1,5%» → {"fiz": 1.2, "yur": 1.5}; нет таких подписей — {}."""
    out = {}
    for m in _CLIENT_RX.finditer(str(rate_text or "")):
        out[CLIENT_OF["физ" if m.group(1).lower() == "физ" else "юр"]] = float(m.group(2).replace(",", "."))
    return out


def _sum_basis(con, cls: str) -> str:
    """База суммы части по справочнику: имущество — стоимость; кредит (правило проекта № 6) — доля; иначе лимит."""
    from . import class_templates as ctpl
    from .act_pkg.terms import credit_rule
    row = db.rows(con, "SELECT kind FROM classes WHERE code=?", cls)
    try:
        tpl = (ctpl.current(con, cls) or {}).get("template") or {}
        if credit_rule(tpl, cls) is not None:
            return "share"
    except Exception as e:
        print("части продукта: шаблон класса не прочитан:", cls, type(e).__name__)
    return "value" if row and row[0]["kind"] == "имущество" else "limit"


def _class_perils(con, cls: str) -> list:
    """Риски класса: справочник perils (классы 8, 9), иначе риски шаблона класса."""
    codes = [r["code"] for r in db.rows(con, "SELECT code FROM perils WHERE class_code=? ORDER BY base_share DESC, "
                                             "code", cls)]
    if codes:
        return codes
    try:
        from . import class_templates as ctpl
        tpl = (ctpl.current(con, cls) or {}).get("template") or {}
        return [str(x.get("code")) for x in ((tpl.get("risks") or {}).get("items") or []) if x.get("code")]
    except Exception as e:
        print("части продукта: риски шаблона не прочитаны:", cls, type(e).__name__)
        return []


def policy_version(con) -> Optional[dict]:
    rows = db.rows(con, "SELECT id, effective_from, document_ref FROM tariff_versions WHERE level=? AND name=? "
                        "ORDER BY effective_from, id LIMIT 1", LEVEL, POLICY_NAME)
    return rows[0] if rows else None


def migrate_from_policy(con, who: str = "миграция частей") -> dict:
    """
    Части из тарифной политики: по строке на класс продукта (product_classes), у первого класса — минимальные
    ставки версии политики (min_rates; «физ / юр» — две строки с client_type), у остальных — ставка части из текста
    тарифа (act_engine.part_rates_from_text, общая ставка «0,1%» — всем частям). Базовая = минимальная (так считает
    акт: base_source = product_rate). Идемпотентно: продукт, у которого строки политики уже есть, пропускается.
    Возвращает счётчики и список спорных разборов текста тарифа.
    """
    from . import act_engine as ae
    from .engine import NEGOTIATED_MODES, STATUTORY_MODE
    ensure(con)
    pv = policy_version(con)
    if not pv:
        raise ValueError(f"нет версии «{POLICY_NAME}» в tariff_versions — сначала tools/db_build.py")
    vid, vf, ref_doc = int(pv["id"]), str(pv["effective_from"]), pv["document_ref"]
    now = db.now()
    done = {"products": 0, "parts": 0, "rows": 0, "skipped": 0, "disputed": [], "policy_version": vid,
            "valid_from": vf}
    cls_cache = {}
    for p in db.rows(con, "SELECT code, name, rate_text, pricing_mode FROM products ORDER BY code"):
        code, text, mode = p["code"], p["rate_text"] or "", p["pricing_mode"]
        if db.rows(con, "SELECT 1 FROM product_parts WHERE product_code=? AND tariff_version_id=? LIMIT 1", code, vid):
            done["skipped"] += 1
            continue
        classes = [r["class_code"] for r in db.rows(
            con, "SELECT class_code FROM product_classes WHERE product_code=? ORDER BY part_no", code)]
        if not classes:
            done["disputed"].append({"product_code": code, "rate_text": text, "why": "у продукта нет классов"})
            continue
        mr = {r["payer_type"]: float(r["min_rate_pct"]) for r in db.rows(
            con, "SELECT payer_type, min_rate_pct FROM min_rates WHERE product_code=? AND tariff_version_id=?",
            code, vid)}
        texts = ae.part_rates_from_text(text, classes)
        by_client = client_rates_from_text(text)
        has_pct = bool(re.search(r"\d\s*%", text))
        tariff = mode not in NEGOTIATED_MODES and mode != STATUTORY_MODE
        why = []
        if by_client and {CLIENT_OF.get(k): v for k, v in mr.items()} != by_client:
            why.append(f"ставки по типу клиента в тексте {by_client} и в min_rates {mr} расходятся")
        for i, cls in enumerate(classes, start=1):
            if cls not in cls_cache:
                cls_cache[cls] = (_sum_basis(con, cls), _class_perils(con, cls))
            sb, perils = cls_cache[cls]
            if not tariff:
                variants = {None: None}
            elif i == 1:
                variants = dict(mr) or {None: None}
                if cls in texts and mr and any(abs(v - texts[cls]) > 1e-9 for v in mr.values()):
                    why.append(f"кл. {cls}: ставка части в тексте {texts[cls]:g}% и минимальная ставка продукта "
                               f"{', '.join(f'{v:g}%' for v in mr.values())} расходятся — взята минимальная")
                if not mr and has_pct:
                    why.append(f"кл. {cls}: в тексте тарифа есть ставка, но минимальной ставки продукта нет — "
                               "часть без ставки (расчёт прежним путём)")
            else:
                v = texts.get(cls, texts.get("*"))
                variants = {None: v}
                if v is None and has_pct:
                    why.append(f"кл. {cls}: подпись части в тексте тарифа не найдена — часть без ставки")
                elif cls not in texts and "*" in texts:
                    why.append(f"кл. {cls}: общая ставка продукта {texts['*']:g}% применена к части "
                               "(в тексте одна ставка на весь продукт)")
            for payer, pct in variants.items():
                row = {"product_code": code, "part_no": i, "class_code": cls, "perils": perils,
                       "client_type": CLIENT_OF.get(payer, "both"), "base_rate_pct": pct, "min_rate_pct": pct,
                       "rate_type": "annual", "sum_basis": sb,
                       "default_share": 1.0 if len(classes) == 1 else None, "limit": None, "deductible": None,
                       "mandatory": True, "order": i, "valid_from": vf, "rules_ref": ref_doc,
                       "note": None if tariff else f"ставка: {mode}"}
                _insert(con, vid, row, SRC_POLICY, now, who)
                done["rows"] += 1
            done["parts"] += 1
        if tariff and has_pct:
            segs = [s.strip() for s in re.split(r"[·;]", text) if s.strip()]
            known = len(texts) + (1 if by_client else 0)
            if len(segs) > 1 and known < len(segs) and not by_client:
                why.append(f"в тексте {len(segs)} ставок, узнано {len(texts)}")
        if why:
            done["disputed"].append({"product_code": code, "name": p["name"], "rate_text": text, "classes": classes,
                                     "why": "; ".join(why)})
        done["products"] += 1
    if done["rows"]:
        db.audit(con, who, "части продуктов из тарифной политики", "product_parts",
                 {"продуктов": done["products"], "частей": done["parts"], "строк": done["rows"],
                  "спорных": len(done["disputed"]), "версия": vid})
        db.reference_changed(con)
    return done


def seed_if_empty(con) -> Optional[dict]:
    """Пустая таблица частей (первый запуск после обновления) — части из тарифной политики; иначе ничего."""
    ensure(con)
    if con.execute("SELECT 1 FROM product_parts LIMIT 1").fetchone():
        return None
    if not policy_version(con):
        return None
    return migrate_from_policy(con, "система: части продуктов")


# --------------------------------------------------------------------------- #
#  Excel
# --------------------------------------------------------------------------- #

HEAD = ["Код продукта", "№ части", "Класс", "Риски (коды через запятую)", "Тип клиента (fiz|yur|both)",
        "Базовая ставка %", "Минимальная ставка %", "Тип ставки (annual|fixed)", "База суммы (value|limit|share)",
        "Доля по умолчанию (0–1)", "Лимит, сум", "Франшиза, %", "Обязательная (да|нет)", "Порядок",
        "Действует с (ДД.ММ.ГГГГ)", "Правила (ссылка)"]
HEAD_HINT = "«Код продукта», «№ части», «Класс», «Базовая ставка %», «Минимальная ставка %», «Действует с»"
PREVIEWS = xi.Previews("product_parts")


def _match(h: str) -> Optional[str]:
    pairs = (("код", "product_code"), ("№", "part_no"), ("номер", "part_no"), ("part_no", "part_no"),
             ("класс", "class_code"), ("риск", "perils"), ("тип клиент", "client_type"), ("клиент", "client_type"),
             ("базов", "base_rate_pct"), ("миним", "min_rate_pct"), ("тип ставк", "rate_type"),
             ("база сумм", "sum_basis"), ("дол", "default_share"), ("лимит", "limit"), ("франш", "deductible"),
             ("обязат", "mandatory"), ("порядок", "order"), ("действует", "valid_from"), ("дата", "valid_from"),
             ("правил", "rules_ref"))
    for start, key in pairs:
        if h.startswith(start):
            return key
    return None


def template_bytes() -> bytes:
    d = date.today().strftime("%d.%m.%Y")
    return xi.template(
        "Части продуктов", HEAD,
        ["0833", 1, "8", "fire, nature", "both", 0.1, 0.1, "annual", "value", 0.8, "", "", "да", 1, d,
         "Тарифная политика, приказ 54-П — пример, удалите строку"],
        ["Импорт частей продуктов",
         "",
         "1. Одна строка — одна часть продукта. Все части одного продукта в файле — новая версия частей этого",
         "   продукта: она заменяет прежний состав с даты «Действует с». Прежние версии сохраняются.",
         "2. Класс — код из справочника классов (8, 13, 13з …). Риски — коды рисков класса через запятую (можно пусто).",
         "3. Тип клиента: fiz — физлицо, yur — юрлицо, both — любой. Для одной части можно дать две строки (fiz и",
         "   yur) с разными ставками — так у КАСКО 0309: физ 1,2%, юр 1,5%.",
         "4. Ставки — в процентах: 0,5 значит 0,5%. Базовая ставка не ниже минимальной.",
         "5. Тип ставки: annual — годовая (премия = сумма × ставка × дни / 365), fixed — на весь срок.",
         "6. База суммы: value — стоимость объекта, limit — лимит ответственности, share — доля (кредит).",
         "7. Доля по умолчанию — часть страховой суммы договора от 0 до 1 (0,6 — это 60%). Сумма долей",
         "   обязательных частей продукта — не больше 1 (отдельно для физлица и юрлица).",
         "8. Лимит — в сумах, франшиза — в % страховой суммы; можно пусто. Обязательная: да или нет.",
         "9. Действует с — не раньше сегодняшней даты: прошлые расчёты должны воспроизводиться.",
         "10. Сначала — предпросмотр с ошибками по строкам. Файл с любой ошибкой не сохраняется целиком."],
        [12, 8, 8, 24, 22, 16, 18, 22, 26, 20, 14, 12, 18, 10, 22, 40])


@router.get("/reference/product-parts/template.xlsx")
def parts_template():
    return Response(template_bytes(), media_type=xi.XLSX_MIME,
                    headers={"Content-Disposition": "attachment; filename*=UTF-8''product_parts_template.xlsx"})


def _parse_and_check(data: bytes) -> tuple:
    sheet = xi.read_sheet(data, _match, {"product_code", "part_no", "class_code"}, MAX_ROWS, HEAD_HINT)
    with db.tx() as con:
        ensure(con)
        return sheet, check_rows(con, sheet["rows"])


@router.post("/reference/product-parts/import")
async def parts_import(file: UploadFile = File(...), user: dict = Depends(require(ADMIN))):
    """Предпросмотр: ошибки по строкам и по продуктам; token — только если файл можно сохранить."""
    data = await xi.check_upload(file, FILE_MAX)
    sheet, pv = await run_in_threadpool(_parse_and_check, data)
    out = pv | {"file_name": file.filename or "", "ignored_columns": sheet["unknown"],
                "expires_min": xi.TOKEN_TTL_SEC // 60, "token": None}
    if pv["can_apply"]:
        out["token"] = PREVIEWS.put(user["login"], {"rows": sheet["rows"], "file_name": file.filename or ""})
    return out


@router.post("/reference/product-parts/import/apply")
def parts_import_apply(body: dict = Body(...), user: dict = Depends(require(ADMIN))):
    token = str((body or {}).get("token") or "")
    if not token:
        raise HTTPException(422, "Нужен token из предпросмотра (файл с ошибками token не получает)")
    payload = PREVIEWS.take(token, user["login"])
    with db.tx() as con:
        try:
            return apply_rows(con, payload["rows"], user["login"], SRC_IMPORT, payload["file_name"])
        except ValueError as e:
            return JSONResponse({"ok": False, "detail": str(e)}, status_code=422)


@router.get("/reference/product-parts/{code}")
def parts_get(code: str, date_: Optional[str] = Query(None, alias="date"), client_type: Optional[str] = None):
    """Части продукта на дату (по умолчанию сегодня): действующая версия, части (для client_type — строки своего
    типа клиента), используется ли версия для ставки акта, история версий."""
    if client_type not in (None, "", "fiz", "yur", "both"):
        return JSONResponse({"ok": False, "detail": "client_type: fiz, yur или both"}, status_code=422)
    if date_ and not min_rates.parse_date(date_):
        return JSONResponse({"ok": False, "detail": "date: ГГГГ-ММ-ДД или ДД.ММ.ГГГГ"}, status_code=422)
    as_of = _iso(date_)
    with db.tx() as con:
        pr = db.rows(con, "SELECT code, name, pricing_mode, rate_text FROM products WHERE code=?", code)
        if not pr:
            return JSONResponse({"ok": False, "detail": "продукт не найден"}, status_code=404)
        ct = client_type if client_type in ("fiz", "yur") else None
        got = parts_on(con, code, as_of, ct)
        used = {}
        for p in got["parts"]:
            used[p["class_code"]] = rate_part(con, code, p["class_code"], ct, as_of) is not None
        return {"ok": True, "product": pr[0], "as_of": as_of, "client_type": client_type or None,
                "version": got["version"], "parts": got["parts"], "used_for_rate": used,
                "versions": versions(con, code)}


@router.put("/reference/product-parts/{code}")
def parts_put(code: str, body: dict = Body(...), user: dict = Depends(require(ADMIN))):
    """Новая версия частей одного продукта (форма админки): {"valid_from", "parts": [часть, …], "note"}; поля части —
    как колонки Excel (PUBLIC_KEYS). Те же проверки, что у импорта; ошибка — 422 и ничего не записано."""
    body = body if isinstance(body, dict) else {}
    parts = body.get("parts")
    if not isinstance(parts, list) or not parts:
        return JSONResponse({"ok": False, "detail": "parts: список частей"}, status_code=422)
    if len(parts) > MAX_PARTS * 3:
        return JSONResponse({"ok": False, "detail": f"частей не больше {MAX_PARTS * 3}"}, status_code=422)
    rows = []
    for i, p in enumerate(parts, start=1):
        if not isinstance(p, dict):
            return JSONResponse({"ok": False, "detail": f"часть {i}: объект с полями части"}, status_code=422)
        v = {k: p.get(k) for k in PUBLIC_KEYS}
        v["product_code"] = code
        if v.get("valid_from") in (None, ""):
            v["valid_from"] = body.get("valid_from")
        rows.append({"row": i, "values": v})
    with db.tx() as con:
        if not db.rows(con, "SELECT code FROM products WHERE code=?", code):
            return JSONResponse({"ok": False, "detail": "продукт не найден"}, status_code=404)
        try:
            out = apply_rows(con, rows, user["login"], SRC_ADMIN)
        except ValueError as e:
            return JSONResponse({"ok": False, "detail": str(e), "preview": check_rows(con, rows)}, status_code=422)
        return out | {"current": parts_on(con, code)}
