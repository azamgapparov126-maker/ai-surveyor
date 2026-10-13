"""
Проверка блока «Техническая картина рынка» (app/market_picture.py).

Запуск из корня:  set PYTHONIOENCODING=utf-8 && sandbox\\.venv\\Scripts\\python.exe tests\\test_market_picture.py

Работает на КОПИИ рабочей базы (tests/tmpdb.temp_db): модуль только читает, но и копия
гарантирует, что data/surveyor.db не открывается вовсе.

Что проверяется для классов 8, 9, 3 и классов без данных (16у — строки в отчёте нет,
17 — строка есть, но премии и обязательства нулевые):
  1. структура ответа (market / region / sources / notes);
  2. ставка, убыточность, число срезов, дата, доля INSON, отклонение нашей ставки —
     совпадают с прямым SQL-подсчётом по market_stats;
  3. регион «Ташкент» → город Ташкент: население, ВРП, индекс цен на жильё и проникновение
     совпадают с прямым SQL по stat_series; индекс цен на строительство — level='республика';
  4. у каждого числа есть источник (поле src → id из списка sources с url и доменом);
  5. нет данных → None и пояснение в notes, а не ноль;
  6. regions() отдаёт республику и 14 регионов.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from tmpdb import ensure_market_stats, ensure_stat_series, temp_db   # noqa: E402
from app import db                             # noqa: E402
from app import market_picture as mp           # noqa: E402

FAILED = []
MONTHS = {"03-31": 3, "04-01": 3, "07-01": 6, "10-01": 9, "01-01": 12}


def check(name, got, want, eps=0.0006):
    if isinstance(want, float) and got is not None:
        ok = abs(got - want) < eps
    else:
        ok = got == want
    print(("  ОК   " if ok else "  СБОЙ ") + "%s: получено %r, ожидалось %r" % (name, got, want))
    if not ok:
        FAILED.append(name)


def check_true(name, cond, hint=""):
    print(("  ОК   " if cond else "  СБОЙ ") + name + ("" if cond else " — " + str(hint)))
    if not cond:
        FAILED.append(name)


def one(con, sql, *args):
    r = con.execute(sql, args).fetchone()
    return None if r is None else tuple(r)


# --------------------------------------------------------------------------- #
# «У каждого числа есть источник»
# --------------------------------------------------------------------------- #
# Числовые поля верхнего уровня market и поле, где лежит их источник.
MARKET_SRC = {"rate_pct": "rate_src", "loss_ratio_pct": "loss_ratio_src",
              "rate_full_year_pct": "full_year_src", "loss_ratio_full_year_pct": "full_year_src"}
SKIP = {"our_rate_pct", "periods"}          # ввод пользователя и счётчик — не данные источника


def orphans(obj, ids, path="", parent=None):
    """Числа без источника: число в словаре без src (и без отдельного поля-источника)."""
    bad = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = path + "." + k
            if isinstance(v, (int, float)) and not isinstance(v, bool) and k not in SKIP:
                # отдельные поля-источники — только у верхнего уровня market, у остальных — src
                field = MARKET_SRC.get(k, "src") if path == ".market" else "src"
                sref = obj.get(field)
                refs = sref if isinstance(sref, list) else [sref]
                if not refs or not all(r in ids for r in refs):
                    bad.append(p)
            elif isinstance(v, (dict, list)):
                bad += orphans(v, ids, p, obj)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            bad += orphans(v, ids, "%s[%d]" % (path, i), parent)
    return bad


def check_sources(tag, res):
    ids = {s["id"] for s in res["sources"]}
    for s in res["sources"]:
        if not (s.get("title") and s.get("url", "").startswith("https://") and s.get("domain")
                and "as_of" in s):
            check_true("%s: источник %s оформлен полностью" % (tag, s.get("id")), False, s)
    bad = orphans({"market": res["market"], "region": res["region"]}, ids)
    check_true("%s: у каждого числа есть источник" % tag, not bad, bad[:10])
    domains = {s["domain"] for s in res["sources"]}
    check_true("%s: источники — napp.uz и stat.uz" % tag, {"napp.uz", "stat.uz"} <= domains, domains)


# --------------------------------------------------------------------------- #
# Рынок: сверка с прямым SQL
# --------------------------------------------------------------------------- #

def sql_row(con, row_key):
    last = one(con, "SELECT report_date, premiums_ytd, payouts_ytd, liabilities FROM market_stats "
                    "WHERE row_key=? ORDER BY report_date DESC LIMIT 1", row_key)
    full = one(con, "SELECT report_date, premiums_ytd, payouts_ytd, liabilities FROM market_stats "
                    "WHERE row_key=? AND report_date LIKE '%-01-01' ORDER BY report_date DESC LIMIT 1", row_key)
    n = one(con, "SELECT COUNT(*) FROM market_stats WHERE row_key=?", row_key)[0]
    return last, full, n


def rate(r):
    d, p, _pay, liab = r
    return p * 12 / MONTHS[d[5:]] / liab * 100 if p and liab else None


def lr(r):
    return r[2] / r[1] * 100 if r[1] else None


def check_market(con, cls, want_row, our):
    print("\nКласс %s (строка %s), наша ставка %s %%" % (cls, want_row, our))
    res = mp.picture(con, cls, region="Ташкент", our_rate_pct=our)
    check_true("структура ответа", set(res) >= {"market", "region", "sources", "notes"}, list(res))
    m = res["market"]
    check("строка НАПП", m["row_key"], want_row)
    last, full, n = sql_row(con, want_row)
    check("ставка последнего среза, %", m["rate_pct"], round(rate(last), 3))
    check("дата последнего среза", m["last_date"], last[0])
    check("число срезов", m["periods"], n)
    check("убыточность последнего среза, %", m["loss_ratio_pct"], round(lr(last), 3))
    check("ставка полного года, %", m["rate_full_year_pct"], round(rate(full), 3))
    check("убыточность полного года, %", m["loss_ratio_full_year_pct"], round(lr(full), 3))
    check("премии последнего среза, млн сум", m["premiums"]["value"], round(last[1], 3))
    check("точек в ряду ставки", len(m["rate_series"]), n)
    check("точек в ряду убыточности", len(m["loss_ratio_series"]), n)
    check("последняя точка ряда = ставка", m["rate_series"][-1]["value"], m["rate_pct"])
    # отклонение
    check("отклонение, п.п.", m["deviation"]["pp"], round(our - rate(last), 3))
    check("отклонение, %", m["deviation"]["pct"], round((our / rate(last) - 1) * 100, 1), eps=0.06)
    # доля INSON по рынку
    ins = one(con, "SELECT premiums_ytd FROM market_stats WHERE row_key='company:INSON AJ' AND report_date=?",
              m["inson_share"]["date"])[0]
    tot = one(con, "SELECT SUM(premiums_ytd) FROM market_stats WHERE row_key LIKE 'company:%' "
                   "AND report_date=?", m["inson_share"]["date"])[0]
    check("доля INSON в премиях рынка, %", m["inson_share"]["market_pct"], round(ins / tot * 100, 3))
    check("доля INSON по классу — нет данных", m["inson_share"]["by_class_pct"], None)
    check_true("пояснение про долю по классу", "нет выгрузки компании" in m["inson_share"]["by_class_note"])
    check_sources("класс %s" % cls, res)
    return res


def check_region(con, res):
    print("\nРегион «Ташкент» → город Ташкент")
    r = res["region"]
    check("ключ региона", r["key"], "region:TOSHKENT SHAHRI")
    check("уровень", r["level"], "регион")
    ind = r["indicators"]
    for ind_id, ds in (("population", "population"), ("grp", "grp"), ("grp_per_capita", "grp_per_capita"),
                       ("house_price_index", "house_price_index")):
        want = one(con, "SELECT period, value FROM stat_series WHERE dataset_id=? AND region=? "
                        "AND value IS NOT NULL ORDER BY period DESC LIMIT 1", ds, "region:TOSHKENT SHAHRI")
        check("%s: период" % ind_id, ind[ind_id]["period"], want[0])
        check("%s: значение" % ind_id, ind[ind_id]["value"], want[1])
        check("%s: уровень" % ind_id, ind[ind_id]["level"], "регион")
    cpi = ind["construction_price_index"]
    want = one(con, "SELECT period, value FROM stat_series WHERE dataset_id='construction_price_index_y' "
                    "AND value IS NOT NULL ORDER BY period DESC LIMIT 1")
    check("индекс цен на строительство: значение", cpi["value"], want[1])
    check("индекс цен на строительство: уровень", cpi["level"], "республика")
    pen = ind["penetration"]
    year = pen["period"]
    prem = one(con, "SELECT premiums_ytd FROM market_stats WHERE row_key='region:TOSHKENT SHAHRI' "
                    "AND report_date=?", "%d-01-01" % (int(year) + 1))[0]
    grp = one(con, "SELECT value FROM stat_series WHERE dataset_id='grp' AND region='region:TOSHKENT SHAHRI' "
                   "AND period=?", year)[0]
    check("проникновение %s, %% ВРП" % year, pen["value"], round(prem / 1000 / grp * 100, 4), eps=0.00006)
    check_true("проникновение ссылается на НАПП и stat.uz", isinstance(pen["src"], list) and len(pen["src"]) == 2)


def check_nodata(con):
    print("\nКласс 16у — строки в отчёте НАПП нет")
    res = mp.picture(con, "16у", our_rate_pct=1.0)
    m = res["market"]
    for k in ("row_key", "rate_pct", "loss_ratio_pct", "rate_full_year_pct", "deviation"):
        check("%s = None" % k, m[k], None)
    check("рядов нет", m["rate_series"], [])
    check_true("пояснение в notes", any("16у" in n for n in res["notes"]), res["notes"])
    check("регион не задан → республика", res["region"]["level"], "республика")
    check_sources("класс 16у", res)

    print("\nКласс 17 — строка есть, но нули")
    res = mp.picture(con, "17", our_rate_pct=1.0)
    m = res["market"]
    check("строка", m["row_key"], "cls17")
    check("ставка = None (не ноль)", m["rate_pct"], None)
    check("убыточность = None", m["loss_ratio_pct"], None)
    check("отклонение не считается", m["deviation"]["pp"], None)
    check_true("пояснение про нулевые обязательства",
               any("обязательства равны нулю" in n for n in res["notes"]), res["notes"])
    check_sources("класс 17", res)

    print("\nНеизвестный регион и неизвестный класс")
    res = mp.picture(con, "99", region="Марс")
    check("класс не найден → ставка None", res["market"]["rate_pct"], None)
    check("регион не опознан → республика", res["region"]["key"], "total")
    check_true("пояснения", any("Марс" in n for n in res["notes"]) and any("99" in n for n in res["notes"]))


def check_product(con):
    print("\nПродукт: проверка принадлежности класса")
    pc = one(con, "SELECT product_code FROM product_classes WHERE class_code='9' LIMIT 1")
    res = mp.picture(con, "9", product_code=pc[0])
    check("класс входит в продукт", res["market"]["product"]["class_in_product"], True)
    check_true("пояснение: разреза по продуктам нет", any("по продуктам" in n for n in res["notes"]))


def check_regions(con):
    print("\nregions()")
    rs = mp.regions(con)
    check("число записей (республика + 14)", len(rs), 15)
    check("первая — республика", rs[0]["key"], "total")
    check_true("есть город Ташкент", any(r["name"] == "город Ташкент" for r in rs))
    for alias, key in (("Ташкент", "region:TOSHKENT SHAHRI"), ("Ташкентская область", "region:TOSHKENT"),
                       ("1718", "region:SAMARQAND"), ("FARG‘ONA", "region:FARG'ONA")):
        check("сопоставление «%s»" % alias, mp.resolve_region(alias)[0], key)


def main():
    with temp_db() as path:
        # чистая машина: рынок НАПП (market_stats) и ряды stat.uz (stat_series) — из data/parsed и снимка fixtures
        ensure_market_stats()
        ensure_stat_series()
        con = db.connect_path(path)
        try:
            res9 = check_market(con, "9", "cls8_9", 0.25)
            check("класс 9: одиночная строка в alternatives", res9["market"]["alternatives"][0]["row_key"], "cls9")
            check_region(con, res9)
            check_market(con, "8", "cls8_9", 0.15)
            check_market(con, "3", "cls3", 1.2)
            check_nodata(con)
            check_product(con)
            check_regions(con)
        finally:
            con.close()
    print()
    if FAILED:
        print("Не пройдено: %d — %s" % (len(FAILED), ", ".join(FAILED)))
        sys.exit(1)
    print("Все проверки пройдены.")


if __name__ == "__main__":
    main()
