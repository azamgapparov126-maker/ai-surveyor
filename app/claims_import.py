"""
Загрузка страховых случаев из Excel (записка заказчика, 02.10.2026) и сводка для экрана и калибровки.

  GET  /claims/template.xlsx      — шаблон
  POST /claims/import             — файл .xlsx ≤ 10 МБ → предпросмотр с проверками и token (администратор)
  POST /claims/import/apply       — {token} → запись в claims + строка claim_batches (администратор)
  GET  /claims/yearly?years=2023,2024,2025 — итоги по году и продукту: число случаев и сумма (ручной ввод и файлы)
  PUT  /claims/yearly             — {year, rows:[{product_code, cases, amount}]} → итоги года вручную (администратор)
  GET  /claims/summary?years=3    — по продуктам и классам: случаи, заявлено, выплачено, средняя выплата,
                                    убыточность (выплачено / премия) — актуарий и администратор (app/guard.py)

Строки пишутся в ту же таблицу claims, из которой читает калибровка (app/calibration.py): номер дела —
external_no, повторная загрузка того же номера — обновление строки, а не дубль. Статусы файла:
заявлен | урегулирован | отказ | в работе (калибровка считает «урегулирован» по выплате, остальные кроме
отказа — по заявленной сумме).

Персональные данные (правило проекта № 8): ФИО страхователя в шаблоне нет. Если в файле есть колонка,
похожая на ФИО, паспорт, ПИНФЛ, ИНН, телефон или адрес, — она не читается и не сохраняется, в предпросмотре
предупреждение. В журнал — только счётчики.
"""
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from . import db, xlsx_import as xi
from .auth import require

router = APIRouter()

ADMIN = "админ"
FILE_MAX = 10 * 1024 * 1024
MAX_ROWS = 20000
STATUSES = ("заявлен", "урегулирован", "отказ", "в работе")
SOURCE = "Excel"
PREVIEWS = xi.Previews("claims")

HEAD = ["Номер дела", "Код продукта", "Класс", "Номер договора", "Филиал", "Регион", "Вид объекта",
        "Дата события", "Дата заявления", "Дата выплаты", "Причина", "Заявлено (сум)", "Выплачено (сум)",
        "Статус (заявлен|урегулирован|отказ|в работе)", "Страховая сумма", "Премия"]
HEAD_HINT = "«Номер дела», «Код продукта», «Дата события», «Статус»"

# признаки колонок с персональными данными: такие колонки не читаем
PD_HEADS = ("фио", "ф.и.о", "ф. и. о", "страхователь", "застрахован", "выгодоприобретател", "фамилия", "имя",
            "отчество", "паспорт", "пинфл", "инн", "телефон", "адрес", "владелец", "full name", "full_name",
            "insured name", "phone")


def _match(h: str) -> Optional[str]:
    if any(p in h for p in PD_HEADS):
        return None
    pairs = (("номер дела", "claim_no"), ("№ дела", "claim_no"), ("код продукта", "product_code"),
             ("продукт", "product_code"), ("класс", "class_code"), ("номер договора", "contract_no"),
             ("№ договора", "contract_no"), ("договор", "contract_no"), ("филиал", "branch"),
             ("регион", "region"), ("вид объекта", "object_type"), ("тип объекта", "object_type"),
             ("дата события", "event_date"), ("дата заявления", "reported_date"), ("дата выплаты", "paid_date"),
             ("причина", "cause"), ("заявлено", "claimed"), ("выплачено", "paid"), ("статус", "status"),
             ("страховая сумма", "sum_insured"), ("премия", "premium"))
    for start, key in pairs:
        if h.startswith(start):
            return key
    return None


def _pd_columns(unknown: list) -> list:
    return [c for c in unknown if any(p in xi.norm_head(c) for p in PD_HEADS)]


# --------------------------------------------------------------------------- #
#  Проверка строк
# --------------------------------------------------------------------------- #

def check_rows(con, raw_rows: list, today: Optional[date] = None) -> dict:
    today = today or date.today()
    products = {r["code"] for r in db.rows(con, "SELECT code FROM products")}
    classes = {r["code"] for r in db.rows(con, "SELECT code FROM classes")}
    pclasses = {}
    for r in db.rows(con, "SELECT product_code, class_code FROM product_classes ORDER BY part_no"):
        pclasses.setdefault(r["product_code"], []).append(r["class_code"])
    obj_types = {}
    for r in db.rows(con, "SELECT DISTINCT class_code, object_type FROM base_rates"):
        obj_types.setdefault(r["class_code"], set()).add(r["object_type"])
    items, seen = [], {}
    for raw in raw_rows:
        v = raw["values"]
        errors, warnings = [], []
        no = xi.as_text(v.get("claim_no"), 60)
        code = xi.product_code(v.get("product_code"))
        cls = xi.as_text(v.get("class_code"), 10)
        status = xi.as_text(v.get("status"), 20).lower()

        if not no:
            errors.append("Номер дела: пустой")
        elif no in seen:
            errors.append(f"Номер дела: повтор — уже в строке {seen[no]}")
        else:
            seen[no] = raw["row"]
        if not code:
            errors.append("Код продукта: пустой")
        elif code not in products:
            errors.append(f"Код продукта: {code} нет в справочнике")
        if not cls and code in pclasses:
            cls = pclasses[code][0]
            warnings.append(f"класс не указан — взят первый класс продукта ({cls})")
        if cls and cls not in classes:
            errors.append(f"Класс: {cls} нет в справочнике")
        elif cls and code in pclasses and cls not in pclasses[code]:
            errors.append(f"Класс: {cls} не входит в продукт {code} ({', '.join(pclasses[code])})")
        if status not in STATUSES:
            errors.append("Статус: " + " | ".join(STATUSES))

        dates = {}
        for key, what in (("event_date", "Дата события"), ("reported_date", "Дата заявления"),
                          ("paid_date", "Дата выплаты")):
            d, err = xi.as_date(v.get(key))
            if err:
                errors.append(f"{what}: {err}")
            elif d and d > today:
                errors.append(f"{what}: в будущем")
            elif d and d.year < 1991:
                errors.append(f"{what}: слишком ранняя")
            dates[key] = d
        if not dates["event_date"] and not any(e.startswith("Дата события") for e in errors):
            errors.append("Дата события: обязательна")
        ev, rep, pd_ = dates["event_date"], dates["reported_date"], dates["paid_date"]
        if ev and rep and rep < ev:
            errors.append("Дата заявления раньше даты события")
        if ev and pd_ and pd_ < ev:
            errors.append("Дата выплаты раньше даты события")

        nums = {}
        for key, what in (("claimed", "Заявлено"), ("paid", "Выплачено"), ("sum_insured", "Страховая сумма"),
                          ("premium", "Премия")):
            n, err = xi.as_number(v.get(key))
            if err:
                errors.append(f"{what}: {err}")
            elif n is not None and n < 0:
                errors.append(f"{what}: не может быть меньше нуля")
                n = None
            nums[key] = n
        if status == "отказ" and (nums["paid"] or 0) > 0:
            warnings.append("статус «отказ», но есть выплата")
        if status == "урегулирован" and nums["paid"] is None:
            warnings.append("статус «урегулирован», но выплата не указана")
        if nums["paid"] and nums["sum_insured"] and nums["paid"] > nums["sum_insured"]:
            warnings.append("выплата больше страховой суммы")
        if nums["paid"] and not pd_:
            warnings.append("есть выплата, но нет даты выплаты")
        otype = xi.as_text(v.get("object_type"), 120)
        if not otype:
            warnings.append("вид объекта не указан — в калибровке пойдёт в группу «не указан»")
        elif cls in obj_types and otype not in obj_types[cls]:
            warnings.append(f"вид объекта «{otype}» не из справочника базовых ставок класса {cls} — "
                            "калибровка покажет его отдельной группой без предложения ставки")

        action = "skip"
        if not errors:
            action = "update" if db.rows(con, "SELECT 1 FROM claims WHERE external_no=? LIMIT 1", no) else "create"
        items.append({"row": raw["row"], "claim_no": no, "product_code": code, "class_code": cls,
                      "contract_no": xi.as_text(v.get("contract_no"), 60),
                      "branch": xi.as_text(v.get("branch"), 120), "region": xi.as_text(v.get("region"), 120),
                      "object_type": otype,
                      "event_date": ev.isoformat() if ev else None,
                      "reported_date": rep.isoformat() if rep else None,
                      "paid_date": pd_.isoformat() if pd_ else None,
                      "cause": xi.as_text(v.get("cause"), 200), "status": status,
                      **nums, "action": action, "errors": errors, "warnings": warnings})
    ok = sum(1 for it in items if not it["errors"])
    return {"items": items, "ok_count": ok, "error_count": len(items) - ok,
            "create_count": sum(1 for it in items if it["action"] == "create"),
            "update_count": sum(1 for it in items if it["action"] == "update")}


# --------------------------------------------------------------------------- #
#  Запись
# --------------------------------------------------------------------------- #

COLS = ("class_code", "object_type", "event_date", "reported_date", "paid_date", "cause", "claimed", "paid",
        "status", "source", "product_code", "contract_no", "branch", "region", "sum_insured", "premium",
        "batch_id", "updated_at")


def apply_rows(con, raw_rows: list, who: str, file_name: str = "") -> dict:
    pv = check_rows(con, raw_rows)
    good = [it for it in pv["items"] if not it["errors"]]
    if not good:
        raise HTTPException(422, "Нет строк без ошибок — ничего не записано")
    ts = db.now()
    cur = con.execute("INSERT INTO claim_batches (file_name, rows, created, updated, skipped, loaded_by, loaded_at)"
                      " VALUES (?,?,?,?,?,?,?)", ((file_name or "")[:200], len(pv["items"]), 0, 0,
                                                  pv["error_count"], who, ts))
    batch = cur.lastrowid
    created = updated = 0
    src = f"{SOURCE}: {(file_name or 'без имени')[:120]}"
    for it in good:
        vals = {**{k: it.get(k) for k in COLS}, "source": src, "batch_id": batch, "updated_at": ts}
        for k in ("contract_no", "branch", "region", "object_type", "cause"):
            vals[k] = vals[k] or None
        old = db.rows(con, "SELECT id FROM claims WHERE external_no=? ORDER BY id LIMIT 1", it["claim_no"])
        if old:
            con.execute("UPDATE claims SET " + ", ".join(f"{k}=?" for k in COLS) + " WHERE id=?",
                        tuple(vals[k] for k in COLS) + (old[0]["id"],))
            updated += 1
        else:
            con.execute("INSERT INTO claims (external_no, " + ", ".join(COLS) + ") VALUES (?"
                        + ",?" * len(COLS) + ")", (it["claim_no"],) + tuple(vals[k] for k in COLS))
            created += 1
    con.execute("UPDATE claim_batches SET created=?, updated=? WHERE id=?", (created, updated, batch))
    db.audit(con, who, "загружены страховые случаи", f"claim_batch:{batch}",
             {"файл": (file_name or "")[:120], "строк": len(pv["items"]), "создано": created,
              "обновлено": updated, "с ошибками": pv["error_count"]})
    return {"ok": True, "batch_id": batch, "rows": len(pv["items"]), "created": created, "updated": updated,
            "skipped": pv["error_count"], "errors": [{"row": it["row"], "claim_no": it["claim_no"],
                                                      "errors": it["errors"]}
                                                     for it in pv["items"] if it["errors"]]}


# --------------------------------------------------------------------------- #
#  Сводка
# --------------------------------------------------------------------------- #

def summary(con, years: int = 3, today: Optional[date] = None) -> dict:
    """
    По продуктам и классам за последние years лет (по дате события, если её нет — по дате заявления).
    Заявлено и выплачено — по всем случаям периода, включая отказы (у отказа выплата 0).
    Средняя выплата — выплачено / число случаев с выплатой > 0.
    Убыточность — выплачено / премия; премия берётся один раз на договор (у одного договора может быть
    несколько случаев), и только по строкам, где она указана. Это убыточность договоров, по которым был
    случай, а не всего портфеля: премия договоров без случаев сюда не попадает.
    """
    today = today or date.today()
    start = (today - timedelta(days=365 * years + years // 4)).isoformat()
    rows = db.rows(con, """SELECT c.id, c.external_no, c.status, c.claimed, c.paid, c.premium, c.contract_no,
                                  c.event_date, c.reported_date,
                                  COALESCE(c.product_code, r.product_code) AS product_code,
                                  COALESCE(c.class_code, (SELECT pc.class_code FROM product_classes pc
                                       WHERE pc.product_code = COALESCE(c.product_code, r.product_code)
                                       ORDER BY pc.part_no LIMIT 1)) AS class_code
                           FROM claims c LEFT JOIN requests r ON r.id = c.request_id""")
    names = {r["code"]: r["name"] for r in db.rows(con, "SELECT code, name FROM products")}
    cnames = {r["code"]: r["name"] for r in db.rows(con, "SELECT code, name FROM classes")}
    groups, totals = {}, {"cases": 0, "claimed": 0.0, "paid": 0.0, "premium": 0.0, "paid_with_premium": 0.0}
    for r in rows:
        d = (r["event_date"] or r["reported_date"] or "")[:10]
        if not d or d < start or d > today.isoformat():
            continue
        key = (r["product_code"] or "—", r["class_code"] or "—")
        g = groups.setdefault(key, {"cases": 0, "refused": 0, "paid_cases": 0, "claimed": 0.0, "paid": 0.0,
                                    "_prem": {}, "_paid_prem": 0.0})
        paid = float(r["paid"] or 0)
        g["cases"] += 1
        g["refused"] += 1 if r["status"] == "отказ" else 0
        g["paid_cases"] += 1 if paid > 0 else 0
        g["claimed"] += float(r["claimed"] or 0)
        g["paid"] += paid
        if r["premium"]:
            contract = r["contract_no"] or f"дело:{r['external_no'] or r['id']}"
            g["_prem"][contract] = max(g["_prem"].get(contract, 0.0), float(r["premium"]))
            g["_paid_prem"] += paid
    items = []
    for (pc, cc), g in sorted(groups.items()):
        prem = sum(g.pop("_prem").values())
        paid_prem = g.pop("_paid_prem")
        items.append({"product_code": pc, "product_name": names.get(pc), "class_code": cc,
                      "class_name": cnames.get(cc), **{k: round(v, 2) if isinstance(v, float) else v
                                                        for k, v in g.items()},
                      "avg_paid": round(g["paid"] / g["paid_cases"], 2) if g["paid_cases"] else None,
                      "premium": round(prem, 2) if prem else None,
                      "loss_ratio": round(paid_prem / prem, 4) if prem else None})
        totals["cases"] += g["cases"]
        totals["claimed"] += g["claimed"]
        totals["paid"] += g["paid"]
        totals["premium"] += prem
        totals["paid_with_premium"] += paid_prem
    totals["loss_ratio"] = round(totals["paid_with_premium"] / totals["premium"], 4) if totals["premium"] else None
    for k in ("claimed", "paid", "premium", "paid_with_premium"):
        totals[k] = round(totals[k], 2)
    batches = db.rows(con, "SELECT id, file_name, rows, created, updated, skipped, loaded_by, loaded_at"
                           " FROM claim_batches ORDER BY id DESC LIMIT 10")
    return {"period_from": start, "period_to": today.isoformat(), "years": years, "items": items,
            "totals": totals, "batches": batches,
            "note": "Убыточность — выплачено / премия по договорам, где был случай и указана премия "
                    "(премия договора учитывается один раз)."}


# --------------------------------------------------------------------------- #
#  Маршруты
# --------------------------------------------------------------------------- #

def template_bytes() -> bytes:
    d = date.today()
    return xi.template(
        "Страховые случаи", HEAD,
        ["У-2026-0001", "0999", "8", "Д-0001", "Головной офис", "г. Ташкент", "склад",
         (d - timedelta(days=40)).strftime("%d.%m.%Y"), (d - timedelta(days=38)).strftime("%d.%m.%Y"),
         (d - timedelta(days=10)).strftime("%d.%m.%Y"), "пожар", 15000000, 12000000, "урегулирован",
         500000000, 2500000],
        ["Загрузка страховых случаев",
         "",
         "1. Одна строка — одно дело. Номер дела уникален: повторная загрузка того же номера обновляет дело.",
         "2. Код продукта — как в справочнике (например, 0832). Класс — один из классов продукта;",
         "   пусто — берётся первый класс продукта.",
         "3. Даты — ДД.ММ.ГГГГ. Дата события обязательна; даты заявления и выплаты не раньше даты события.",
         "4. Суммы — в сумах, не меньше нуля.",
         "5. Статус: заявлен, урегулирован, отказ или в работе.",
         "6. ФИО, паспорт, телефон и адрес страхователя НЕ указывайте: такие колонки не загружаются.",
         "7. Сначала — предпросмотр с ошибками по строкам; записываются только строки без ошибок.",
         "8. Пример в первой строке — удалите его перед загрузкой."],
        [14, 14, 8, 16, 18, 16, 18, 14, 14, 14, 20, 16, 16, 30, 18, 14])


@router.get("/claims/template.xlsx")
def claims_template():
    return Response(template_bytes(), media_type=xi.XLSX_MIME,
                    headers={"Content-Disposition": "attachment; filename*=UTF-8''claims_template.xlsx"})


def _parse_and_check(data: bytes) -> tuple:
    """Разбор файла и проверка строк — в пуле потоков, не в цикле событий."""
    sheet = xi.read_sheet(data, _match, {"claim_no", "product_code", "event_date", "status"}, MAX_ROWS, HEAD_HINT)
    with db.tx() as con:
        return sheet, check_rows(con, sheet["rows"])


@router.post("/claims/import")
async def claims_import(file: UploadFile = File(...), user: dict = Depends(require(ADMIN))):
    data = await xi.check_upload(file, FILE_MAX)
    sheet, pv = await run_in_threadpool(_parse_and_check, data)
    pd_cols = _pd_columns(sheet["unknown"])
    warnings = []
    if pd_cols:
        warnings.append("Колонки с персональными данными пропущены и не сохраняются: " + ", ".join(pd_cols))
    other = [c for c in sheet["unknown"] if c not in pd_cols]
    if other:
        warnings.append("Колонки не из шаблона пропущены: " + ", ".join(other))
    token = PREVIEWS.put(user["login"], {"rows": sheet["rows"], "file_name": file.filename or ""})
    return pv | {"token": token, "file_name": file.filename or "", "warnings": warnings,
                 "ignored_columns": sheet["unknown"], "pd_columns_ignored": pd_cols,
                 "expires_min": xi.TOKEN_TTL_SEC // 60}


@router.post("/claims/import/apply")
def claims_import_apply(body: dict = Body(...), user: dict = Depends(require(ADMIN))):
    token = str((body or {}).get("token") or "")
    if not token:
        raise HTTPException(422, "Нужен token из предпросмотра")
    payload = PREVIEWS.take(token, user["login"])
    with db.tx() as con:
        return apply_rows(con, payload["rows"], user["login"], payload["file_name"])


@router.get("/claims/summary")
def claims_summary(years: int = 3):
    if not 1 <= years <= 10:
        raise HTTPException(422, "years — от 1 до 10")
    with db.tx() as con:
        return summary(con, years)


# --------------------------------------------------------------------------- #
#  Итоги по году и продукту — ручной ввод (записка заказчика, 06.10.2026)
# --------------------------------------------------------------------------- #
# Заказчик вводит «за год по продукту: число случаев и сумма». Отдельной таблицы итогов нет, а сводка
# (summary) и калибровка (app/calibration.py) считают случаи строками claims. Поэтому итог года пишется
# N строками claims — по одной на случай, сумма делится поровну: число случаев, сумма выплат и средняя
# выплата в сводке и калибровке получаются ровно такими, как ввёл человек. Метки строк:
# source = «итог за год», object_type = «итог за год», external_no = agg-<год>-<продукт>-<№>,
# статус «урегулирован», дата события — 31.12 года (для текущего года — сегодня: будущую дату сводка не берёт).
# Повторное сохранение года заменяет его ручные итоги целиком; строки из Excel не трогаются.

AGG_SOURCE = "итог за год"
AGG_MAX_CASES = 100000


def _agg_date(year: int, today: date) -> str:
    end = date(year, 12, 31)
    return (end if end <= today else today).isoformat()


def yearly(con, years: list, today: Optional[date] = None) -> dict:
    today = today or date.today()
    names = {r["code"]: r["name"] for r in db.rows(con, "SELECT code, name FROM products")}
    rows = db.rows(con, """SELECT COALESCE(c.product_code, r.product_code) AS product_code, c.source,
                                  c.paid, c.claimed, c.status, COALESCE(c.event_date, c.reported_date) AS d
                           FROM claims c LEFT JOIN requests r ON r.id = c.request_id""")
    out = []
    for y in years:
        lo, hi = f"{y}-01-01", f"{y}-12-31"
        manual, files = {}, {}
        for r in rows:
            d = str(r["d"] or "")[:10]
            if not (lo <= d <= hi):
                continue
            pc = r["product_code"] or "—"
            bucket = manual if r["source"] == AGG_SOURCE else files
            g = bucket.setdefault(pc, {"cases": 0, "amount": 0.0})
            g["cases"] += 1
            g["amount"] += float(r["paid"] if r["paid"] is not None else (r["claimed"] or 0))
        pack = lambda b: [{"product_code": k, "product_name": names.get(k), "cases": v["cases"],
                           "amount": round(v["amount"], 2)} for k, v in sorted(b.items())]
        out.append({"year": y, "manual": pack(manual), "files": pack(files),
                    "event_date": _agg_date(y, today) if y <= today.year else None})
    return {"years": out, "source": AGG_SOURCE}


def _years_arg(raw: str, today: date) -> list:
    try:
        ys = [int(x) for x in str(raw or "").replace(" ", "").split(",") if x]
    except ValueError:
        raise HTTPException(422, "years — годы через запятую, например 2023,2024,2025")
    if not ys:
        ys = [today.year - 3, today.year - 2, today.year - 1]
    if len(ys) > 10 or any(y < 1991 or y > today.year for y in ys):
        raise HTTPException(422, f"Годы — от 1991 до {today.year}, не больше десяти")
    return ys


@router.get("/claims/yearly")
def claims_yearly(years: str = ""):
    today = date.today()
    ys = _years_arg(years, today)
    with db.tx() as con:
        return yearly(con, ys, today)


def save_yearly(con, year, rows, who: str, today: Optional[date] = None) -> dict:
    today = today or date.today()
    try:
        year = int(year)
    except (TypeError, ValueError):
        raise HTTPException(422, "Год — целое число, например 2024")
    if year < 1991 or year > today.year:
        raise HTTPException(422, f"Год — от 1991 до {today.year}")
    if not isinstance(rows, list):
        raise HTTPException(422, "rows — список строк {product_code, cases, amount}")
    products = {r["code"] for r in db.rows(con, "SELECT code FROM products")}
    first_cls = {}
    for r in db.rows(con, "SELECT product_code, class_code FROM product_classes ORDER BY part_no"):
        first_cls.setdefault(r["product_code"], r["class_code"])
    errors, clean, seen = [], [], set()
    for i, r in enumerate(rows, start=1):
        r = r if isinstance(r, dict) else {}
        code = xi.product_code(r.get("product_code"))
        try:
            n = int(float(r.get("cases")))
        except (TypeError, ValueError):
            n = None
        try:
            amt = float(str(r.get("amount")).replace(" ", "").replace(",", "."))
        except (TypeError, ValueError):
            amt = None
        bad = []
        if not code:
            bad.append("код продукта пустой")
        elif code not in products:
            bad.append(f"продукта {code} нет в справочнике")
        elif code in seen:
            bad.append(f"продукт {code} в этом году уже есть строкой выше")
        if n is None or n < 0 or n > AGG_MAX_CASES:
            bad.append(f"число случаев — целое от 0 до {AGG_MAX_CASES}")
        if amt is None or amt < 0:
            bad.append("сумма — число не меньше нуля")
        if n == 0 and amt:
            bad.append("сумма есть, а случаев 0")
        if bad:
            errors.append(f"строка {i}: " + "; ".join(bad))
        else:
            seen.add(code)
            clean.append((code, n, amt))
    if errors:
        raise HTTPException(422, {"message": "Год не сохранён — исправьте строки", "errors": errors})
    d, now = _agg_date(year, today), db.now()
    lo, hi = f"{year}-01-01", f"{year}-12-31"
    gone = con.execute("DELETE FROM claims WHERE source=? AND event_date BETWEEN ? AND ?", (AGG_SOURCE, lo, hi)).rowcount
    written = 0
    for code, n, amt in clean:
        each = round(amt / n, 2) if n else 0.0
        for k in range(1, n + 1):
            # последняя строка забирает остаток округления: сумма за год сходится до тийина
            pay = round(amt - each * (n - 1), 2) if k == n else each
            con.execute("INSERT INTO claims (external_no, product_code, class_code, object_type, event_date, claimed, paid,"
                        " status, source, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (f"agg-{year}-{code}-{k}", code, first_cls.get(code), AGG_SOURCE, d, pay, pay,
                         "урегулирован", AGG_SOURCE, now))
            written += 1
    db.audit(con, who, "страховые случаи: итоги года", f"year:{year}",
             {"products": len(clean), "cases": written, "replaced": gone})
    return {"ok": True, "year": year, "products": len(clean), "cases": written, "replaced": gone, "event_date": d}


@router.put("/claims/yearly")
def claims_yearly_put(body: dict = Body(...), user: dict = Depends(require(ADMIN))):
    body = body if isinstance(body, dict) else {}
    with db.tx() as con:
        return save_yearly(con, body.get("year"), body.get("rows"), user["login"])
