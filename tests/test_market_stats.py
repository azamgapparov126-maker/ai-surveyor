"""
Новые таблицы квартального отчёта НАПП (01.10.2026): комплексное страхование (пакеты классов листа 1.4), претензии по
регионам (3.5 + 3.4 + 3.2) и страховщикам (2.10 + 2.7 + 2.5), обособленные подразделения (2.12–2.14).

Запуск из корня:
    set PYTHONIOENCODING=utf-8
    sandbox\\.venv\\Scripts\\python.exe tests\\test_market_stats.py

Всё — во временной копии базы (tests/tmpdb.py): tools/market_stats.build() пишет в копию, рабочая база не меняется.
Ожидаемые числа берутся из разобранных CSV (data/parsed), а не из головы.

Что проверяется:
  1. пакеты классов («3,8,9 klasslar», «8,9 klasslar», «3,14 klasslar» …) загружены ключами cls3_8_9 … со всеми
     тремя показателями по обоим срезам каждого отчёта; итог «Ikki va undan ortiq …» — multi_general (общее) и
     multi_life (жизнь), «boshqalar» блока — multi_other; сумма пакетов = итогу (кроме отчёта IV кв. 2025 — сверка
     ловит, что строка итога там не обновлена: в базе — сумма пакетов с пометкой market_stats_notes, она же в
     /market/series и в CSV); отчёты загружаются по дате среза, а не по имени папки;
  2. выбор строки рынка для продукта: 0312 → пакет «3,14» (точный); 0807 (один класс 8) → прежняя строка «8, 9»,
     контрольная ставка 0,185 % не меняется, одиночная строка класса 8 — в alternatives; 0806 (8 + 13) → ближайший
     пакет «8,9,13»; 0305 (1, 3, 13) → пакета нет, строка класса; 1421 (13з + 8) — пометка о подклассе;
  3. претензии региона руками из CSV: частота на 1 000 договоров, доля отказов, средняя выплата; республика;
  4. поправка региона вилки с показателем claims_freq: по умолчанию вес 0 (88,8 % претензий страны — город Ташкент,
     замечание контролёра 01.10.2026) — показан, в поправку не входит; с весом 1 и 3 — пересчёт руками, в границах;
     нет листа за срез → показатель пропущен с пометкой; лист отсутствует в отчёте → строки не пишутся;
     пакет продукта: одиночные строки классов и полный год; оговорки к претензиям на трёх языках;
  5. претензии рынка и INSON (2.10 + 2.7 + 2.5);
  6. подразделения загружены (2.12–2.14), INSON в регионе против компании; /market/branches и страница /stats.
"""
import csv
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
os.environ["SURVEYOR_NO_BACKGROUND"] = "1"

from tmpdb import ensure_stat_series, temp_db   # noqa: E402
from app import db, market_picture as mp        # noqa: E402
from app import act_engine as ae, act_analytics as aa, risk_stats as rs   # noqa: E402
import market_stats as ms                       # noqa: E402

PARSED = ROOT / "data" / "parsed"
Q2_2026 = PARSED / "Страх_отчет(II_чорак_2026_йил_)_uz"
FAILED = []


def ok(name, cond, extra=""):
    if cond:
        print("  ok  ", name)
    else:
        FAILED.append(name)
        print("  FAIL", name, extra)


def rows_of(path):
    return list(csv.reader(open(path, encoding="utf-8-sig")))


def num(x):
    return ms.num(x)


def q(con, sql, *args):
    cur = con.execute(sql, args)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


# ------------------------------------------------------------------ 1. пакеты классов
def check_packs(con):
    print("1. Комплексное страхование: пакеты классов листа 1.4")
    n_rows = 0
    for sheet in sorted(PARSED.glob("*/1.4.csv")):
        rows = rows_of(sheet)
        dates = [rows[3][1][:10], rows[3][2][:10]]
        block, seen_multi = None, 0
        for r in rows[5:]:
            name = (r[0] or "").strip().strip('"')
            if name.startswith("Klasslar bo") and "umumiy" in name:
                block = "general"
            elif name.startswith("Klasslar bo") and "hayot" in name:
                block = "life"
            if name.endswith("klasslar") and "," in name:
                key = "cls" + "_".join(x.strip() for x in name.split(" ")[0].split(","))
            elif name.startswith("Ikki va undan ortiq"):
                key = "multi_general" if block == "general" else "multi_life"
                seen_multi += 1
            else:
                continue
            for i, d in enumerate(dates):
                got = q(con, "SELECT * FROM market_stats WHERE report_date=? AND row_key=?", d, key)
                # срез встречается в двух отчётах: строка берётся из более позднего по дате среза — сравниваем
                # только там, где источник — этот отчёт
                if not got or got[0]["source_file"] != sheet.parent.name:
                    continue
                if q(con, "SELECT 1 FROM market_stats_notes WHERE report_date=? AND row_key=?", d, key):
                    continue                           # итог заменён суммой пакетов — проверяется ниже
                g = got[0]
                want = (num(r[1 + i]), num(r[4 + i]), num(r[7 + i]))
                n_rows += 1
                if (g["premiums_ytd"], g["payouts_ytd"], g["liabilities"]) != want:
                    ok(f"{sheet.parent.name} {d} {key}: премии, выплаты, обязательства как в CSV", False,
                       ((g["premiums_ytd"], g["payouts_ytd"], g["liabilities"]), want))
        ok(f"{sheet.parent.name}: итог «Ikki va undan ortiq» найден в общем и в жизни", seen_multi == 2, seen_multi)
    ok(f"все строки пакетов и итогов совпали с CSV ({n_rows} точек)", n_rows > 100 and not any(
        "как в CSV" in f for f in FAILED), n_rows)
    keys = {r["row_key"] for r in q(con, "SELECT DISTINCT row_key FROM market_stats")}
    for k in ("cls3_8_9", "cls8_9", "cls8_9_13", "cls3_14", "cls7_8_13_16", "cls8_9_13_16", "cls5_11", "cls8_9_16",
              "cls1_2", "cls1_14", "cls1_3_10", "cls1_8_9_13", "cls7_8_9_13", "cls1_2_8_9", "multi_general",
              "multi_life", "multi_other"):
        if k not in keys:
            ok(f"ключ {k} в market_stats", False)
    ok("все 14 пакетов листа II кв. 2026 + итоги загружены (cls3_8_9 … multi_other)",
       {"cls3_8_9", "cls3_14", "multi_general", "multi_life", "multi_other"} <= keys)
    g = q(con, "SELECT * FROM market_stats WHERE report_date='2026-07-01' AND row_key='cls3_14'")[0]
    ok("«3,14 klasslar» 01.07.2026: премии 322 674,207, выплаты 264 430,620, обязательства 95 496 722,865 млн сум",
       round(g["premiums_ytd"], 3) == 322674.207 and round(g["payouts_ytd"], 3) == 264430.620
       and round(g["liabilities"], 3) == 95496722.865, g)
    g = q(con, "SELECT row_name FROM market_stats WHERE row_key='multi_general' LIMIT 1")[0]
    ok("итог комплексного — понятное название", g["row_name"].startswith("Комплексное страхование"), g)
    # сверка: сумма пакетов + «boshqalar» = итог (кроме отчёта IV кв. 2025)
    for sheet in sorted(PARSED.glob("*/1.4.csv")):
        mm = ms.multi_mismatch(ms.load_sheet(sheet))
        if "IV_чорак_2025" in sheet.parent.name:
            ok("IV кв. 2025: сверка ловит несовпадение итога комплексного с суммой пакетов (строка не обновлена)",
               len(mm) == 2, mm)
            for d, total, parts in mm:
                g = q(con, "SELECT * FROM market_stats WHERE report_date=? AND row_key='multi_general'", d)[0]
                n = q(con, "SELECT * FROM market_stats_notes WHERE report_date=? AND row_key='multi_general'", d)
                ok(f"IV кв. 2025, {d}: итог комплексного = сумма пакетов {parts:,.1f} (а не {total:,.1f}) с пометкой",
                   abs(g["premiums_ytd"] - parts) < 1e-6 and n and n[0]["note"].startswith(ms.MULTI_NOTE)
                   and n[0]["source_file"] == sheet.parent.name, (g["premiums_ytd"], n))
        else:
            ok(f"{sheet.parent.name}: сумма пакетов = итогу комплексного", mm == [], mm)
    ok("пометки market_stats_notes — только у двух срезов IV кв. 2025 (01.01.2025, 01.01.2026)",
       [r["report_date"] for r in q(con, "SELECT report_date FROM market_stats_notes ORDER BY 1")]
       == ["2025-01-01", "2026-01-01"])
    fd = [ms.folder_date(f) for f in ms.report_folders()]
    ok("отчёты загружаются по дате среза, а не по имени папки", fd == sorted(fd) and len(fd) == 5, fd)
    ok("общий срез 01.07.2025 (II кв. 2025 и II кв. 2026) — из более позднего отчёта",
       q(con, "SELECT source_file FROM market_stats WHERE report_date='2025-07-01' AND row_key='cls3_14'")[0][
           "source_file"].startswith("Страх_отчет(II_чорак_2026"))


# ------------------------------------------------------------------ 2. выбор строки для продукта
def check_product_rows(con):
    print("2. Рыночная строка продукта: пакет НАПП с тем же набором классов")
    d = "2026-07-01"
    r = rows_of(Q2_2026 / "1.4.csv")
    row = next(x for x in r if x[0].strip().strip('"') == "3,14 klasslar")
    rate = num(row[2]) * 12 / 6 / num(row[8]) * 100
    lr = num(row[5]) / num(row[2]) * 100
    p = mp.picture(con, "3", "0312")["market"]
    ok(f"0312 (классы 3 и 14) → пакет «3,14» (cls3_14), ставка {rate:.3f} %, убыточность {lr:.3f} %",
       p["row_key"] == "cls3_14" and p["pack_choice"]["how"] == "exact" and p["rate_pct"] == round(rate, 3)
       and p["loss_ratio_pct"] == round(lr, 3) and p["rate_date"] == d, (p["row_key"], p["rate_pct"]))
    ok("0312: пометка «пакет НАПП 3,14 — тот же набор классов, что у продукта 0312»",
       p["pack_choice"]["note"].startswith("пакет НАПП 3,14 — тот же набор классов, что у продукта 0312"),
       p["pack_choice"])
    p14 = mp.picture(con, "14", "0312")["market"]
    ok("0312, часть класса 14 → тот же пакет «3,14» (рынок по продукту, а не по классу части)",
       p14["row_key"] == "cls3_14", p14["row_key"])
    p8 = mp.picture(con, "8", "0807")["market"]
    one = mp.picture(con, "8")["market"]
    ok("0807 (один класс 8) → прежняя строка «8, 9» (cls8_9), контрольная ставка 0,185 % не изменилась",
       p8["row_key"] == "cls8_9" and p8["rate_pct"] == 0.185 and p8["pack_choice"] is None
       and one["rate_pct"] == 0.185, (p8["row_key"], p8["rate_pct"]))
    ok("0807: одиночная строка класса 8 (cls8) — в alternatives", any(
        a["row_key"] == "cls8" for a in p8["alternatives"]), p8["alternatives"])
    p = mp.picture(con, "8", "0806")["market"]
    ok("0806 (классы 8 и 13) → ближайший пакет «8,9,13» с пометкой «ближайший к составу продукта»",
       p["row_key"] == "cls8_9_13" and p["pack_choice"]["how"] == "nearest"
       and "ближайший к составу продукта 0806 (8,13)" in p["pack_choice"]["note"], p.get("pack_choice"))
    p = mp.picture(con, "1", "0305")["market"]
    ok("0305 (1, 3, 13) → пакета нет, строка класса 1 с пометкой", p["row_key"] == "cls1"
       and p["pack_choice"]["how"] == "class" and "в отчёте нет — взята строка класса 1" in p["pack_choice"]["note"],
       p.get("pack_choice"))
    p = mp.picture(con, "8", "1421")["market"]
    ok("1421 (13з + 8): подкласс 13з сравнивается как класс 13, пометка", p["row_key"] == "cls8_9_13"
       and "13з" in (p["pack_choice"]["subclass_note"] or ""), p.get("pack_choice"))
    m = aa.market(con, "3", "0312", "Ташкентская область", 0.5, None)
    ok("блок market акта: pack_choice и классы пакета 3, 14", m["pack_choice"]["how"] == "exact"
       and m["pack_classes"] == ["3", "14"] and m["row_key"] == "cls3_14", m)
    hand = {}
    for n in ("3", "14"):
        x = next(x for x in r if x[0].strip().strip('"').startswith(n + "-klass"))
        hand[n] = (round(num(x[2]) * 12 / 6 / num(x[8]) * 100, 3), round(num(x[5]) / num(x[2]) * 100, 3))
    cr = {c["class_code"]: c for c in mp.picture(con, "3", "0312")["market"]["class_rows"]}
    ok(f"0312: рядом с пакетом — одиночные строки классов: класс 3 {hand['3']}, класс 14 {hand['14']} (ставка, "
       "убыточность — руками из CSV)",
       set(cr) == {"3", "14"} and all((cr[k]["rate_pct"], cr[k]["loss_ratio_pct"]) == hand[k] for k in hand)
       and all(cr[k]["rate_full_year_pct"] is not None for k in hand), cr)
    ok("0312: в блоке market акта — одиночные строки классов", [c["class_code"] for c in m["class_rows"]] == ["3", "14"])
    pk = mp.picture(con, "3", "0312")["market"]
    ok("пакет 3,14: полный 2025 год — убыточность 2,52 %, срез 01.07.2026 — 81,95 % (скачок за полугодие)",
       abs(pk["loss_ratio_full_year_pct"] - 2.52) < 0.01 and abs(pk["loss_ratio_pct"] - 81.95) < 0.01
       and pk["rate_full_year_period"] == "2025 год", (pk["loss_ratio_full_year_pct"], pk["loss_ratio_pct"]))
    ok("одиночный продукт (0807) — без одиночных строк классов пакета", mp.picture(con, "8", "0807")["market"][
        "class_rows"] == [])


# ------------------------------------------------------------------ 3. претензии региона руками
def _hand_claims(region_title):
    r35 = rows_of(Q2_2026 / "3.5.csv")
    r34 = rows_of(Q2_2026 / "3.4.csv")
    r32 = rows_of(Q2_2026 / "3.2.csv")
    # 3.5: срез 01.07.2026 — колонки 9..15: поступило, оплачено, %, отказано, %, не урегулировано, %
    c = next(x for x in r35 if x[1].strip() == region_title)
    # 3.4: общее страхование, действующие на 01.07.2026 — колонка 10 (8, 9 — 2025; 10, 11 — 2026)
    k = next(x for x in r34 if x[1].strip() == region_title)
    # 3.2: общее страхование, 01.07.2026 — колонка 6
    y = next(x for x in r32 if x[1].strip() == region_title)
    assert r35[2][9].startswith("2026-07-01") and r34[5][10].startswith("2026-07-01") and r32[4][6].startswith("2026-07-01")
    rcv, paid, ref = num(c[9]), num(c[10]), num(c[12])
    return {"per_1000": rcv / num(k[10]) * 1000, "refused_pct": ref / rcv * 100,
            "avg_payout": num(y[6]) * 1e6 / paid, "received": rcv}


def check_region_claims(con):
    print("3. Претензии по регионам (листы 3.5, 3.4, 3.2): частота, отказы, средняя выплата — руками из CSV")
    reg, rep = _hand_claims("Toshkent"), _hand_claims("Umumiy")
    rc = mp.region_claims(con, "Ташкентская область")
    ok(f"Ташкентская область: {reg['per_1000']:.4f} претензии на 1 000 договоров (2 420 / 339 572)",
       rc["available"] and abs(rc["region"]["per_1000"] - reg["per_1000"]) < 1e-4 and rc["region"]["received"] == 2420,
       rc.get("region"))
    ok(f"республика: {rep['per_1000']:.4f} на 1 000 договоров (137 727 / 12 599 883)",
       abs(rc["republic"]["per_1000"] - rep["per_1000"]) < 1e-4, rc.get("republic"))
    ok("отношение регион / республика и разница в %", abs(rc["ratio"] - reg["per_1000"] / rep["per_1000"]) < 1e-4
       and rc["diff_pct"] == round((rc["ratio"] - 1) * 100, 1), (rc["ratio"], rc["diff_pct"]))
    ok(f"доля отказов {reg['refused_pct']:.3f} % и средняя выплата {reg['avg_payout']:,.0f} сум — как руками",
       abs(rc["region"]["refused_pct"] - reg["refused_pct"]) < 1e-3
       and abs(rc["region"]["avg_payout"] - reg["avg_payout"]) <= 1, rc["region"])
    ok("источник — страница НАПП, листы 3.5, 3.4, 3.2 и срез", rc["source"]["url"].startswith("https://napp.uz")
       and rc["source"]["sheets"] == "3.5, 3.4, 3.2" and rc["source"]["as_of"] == "2026-07-01", rc["source"])
    rc0 = mp.region_claims(con, None)
    ok("регион не задан — только республика, сравнения нет", not rc0["available"] and rc0["republic"]
       and rc0["reason"] == "no_region", rc0["reason"])
    n = q(con, "SELECT COUNT(*) n FROM napp_claims WHERE scope='region' AND report_date='2026-07-01'")[0]["n"]
    ok("на срез 01.07.2026 — 14 регионов + республика", n == 15, n)
    return reg, rep


# ------------------------------------------------------------------ 4. вилка: поправка региона
def check_fork(con, reg, rep):
    print("4. Вилка ставки: показатель «частота претензий региона к республике»")
    fs = ae.fork_settings({})
    ok("claims_freq — в правилах классов 3, 7, 8, 9 с весом 0 по умолчанию; у классов 4, 5, 6 правила нет",
       all("claims_freq" in ae.fork_indicator_ids(fs, c, None)[0] for c in "3789")
       and all(ae.fork_indicator_ids(fs, c, None) == ([], []) for c in "456")
       and fs["region"]["weights"] == {"claims_freq": 0.0} and ae.check_settings({}) == [])
    ok("check_settings: вес вне 0–5 и неизвестный показатель — ошибки",
       len(ae.check_settings({"rate_fork": {"region": {"weights": {"claims_freq": 9, "нет": 1}}}})) == 2)
    fd0 = aa.fork_data(con, cls="8", region="Ташкентская область", group="property", fs=fs, product_code="0807")
    R0 = fd0["region"]
    c0 = next(i for i in R0["indicators"] if i["id"] == "claims_freq")
    ok("по умолчанию (вес 0): claims_freq показан со значением и отношением, но не учтён (why = weight_zero); "
       "поправка — по жилому фонду, −3,2 %",
       not c0["used"] and c0["why"] == "weight_zero" and c0["ratio"] is not None and R0["pct"] == -3.2
       and R0["used"] == 1, (c0, R0["pct"]))
    fs1 = ae.fork_settings({"rate_fork": {"region": {"weights": {"claims_freq": 1}}}})
    fd = aa.fork_data(con, cls="8", region="Ташкентская область", group="property", fs=fs1, product_code="0807")
    R = fd["region"]
    ci = next(i for i in R["indicators"] if i["id"] == "claims_freq")
    ratio = round(reg["per_1000"] / rep["per_1000"], 3)
    ok(f"вес 1: класс 8, Ташкентская область — claims_freq учтён, отношение {ratio} (регион / республика)",
       ci["used"] and ci["ratio"] == ratio and abs(ci["region_value"] - reg["per_1000"]) < 1e-3
       and ci["source"]["kind"] == "napp_claims" and ci["source"]["url"].startswith("https://napp.uz"), ci)
    ok("вес 1 меняет поправку, вес 0 — нет", R["pct"] != R0["pct"], (R["pct"], R0["pct"]))
    used = [i for i in R["indicators"] if i["used"]]
    raw = round(sum(round((i["ratio"] - 1) * 0.5 * 100, 4) for i in used) / len(used), 2)
    ok(f"вес 1: поправка региона руками — среднее вкладов {raw} % → в границах [−10; +15] = {R['pct']} %",
       R["raw_pct"] == raw and R["pct"] == round(min(15, max(-10, raw)), 2) and -10 <= R["pct"] <= 15, R)
    fs2 = ae.fork_settings({"rate_fork": {"region": {"weights": {"claims_freq": 3}}}})
    R2 = aa.fork_data(con, cls="8", region="Ташкентская область", group="property", fs=fs2)["region"]
    eff = [(round((i["ratio"] - 1) * 0.5 * 100, 4), 3 if i["id"] == "claims_freq" else 1) for i in R2["indicators"]
           if i["used"]]
    raw2 = round(sum(e * w for e, w in eff) / sum(w for _, w in eff), 2)
    ok(f"вес claims_freq = 3 → взвешенное среднее {raw2} %", R2["raw_pct"] == raw2 and R2["weights"] == {"claims_freq": 3.0},
       (R2["raw_pct"], raw2))
    r = ae.fork_region([{"id": "a", "ratio": 1.735}, {"id": "claims_freq", "ratio": 0.652}],
                       ae.fork_settings({"rate_fork": {"region": {"weights": {"claims_freq": 0}}}}), True, True)
    ok("вес 0 — показатель не влияет (только ДТП: 36,75 % → граница 15 %)", r["raw_pct"] == 36.75 and r["pct"] == 15, r)
    rc = mp.region_claims(con, "Ташкентская область")
    tot = q(con, "SELECT claims_received FROM napp_claims WHERE report_date='2026-07-01' AND scope='region' "
                 "AND key='total'")[0]["claims_received"]
    cap = q(con, "SELECT claims_received FROM napp_claims WHERE report_date='2026-07-01' AND scope='region' "
                 "AND key='region:TOSHKENT SHAHRI'")[0]["claims_received"]
    ok(f"доля города Ташкента в претензиях страны: {cap:,.0f} / {tot:,.0f} = {cap / tot * 100:.1f} %",
       rc["capital_share_pct"] == round(cap / tot * 100, 1) == 88.8, rc["capital_share_pct"])
    ok("оговорки к претензиям (API): не страховые случаи, с начала года / на дату, прошлые периоды, Ташкент 89 %, "
       "срезы несопоставимы", len(rc["caveats"]) == 6 and "89" in rc["caveats"][3]
       and "несопоставимы" in rc["caveats"][4], rc["caveats"])
    from app import act_texts as tx
    ok("оговорки на трёх языках (act_texts.CLAIMS_CAVEATS)", all(set(v) == {"ru", "uz", "en"}
                                                                 for v in tx.CLAIMS_CAVEATS.values())
       and len(mp.claims_caveats("en", 88.8)) == 6 and "88" not in " ".join(mp.claims_caveats("en", None)))
    mk = fd["market"]
    ok("рынок вилки 0807 — строка «8, 9», 0,185 %", mk["row_key"] == "cls8_9" and mk["rate_pct"] == 0.185, mk)
    mk = aa.fork_data(con, cls="14", region="Ташкентская область", group=None, fs=fs, product_code="0312")["market"]
    ok("рынок вилки части класса 14 продукта 0312 — пакет «3,14» с пометкой (pack_choice)",
       mk["row_key"] == "cls3_14" and (mk.get("pack_choice") or {}).get("how") == "exact", mk)


def check_missing(con):
    print("4б. Нет листа за срез — показатель пропускается с пометкой")
    con.execute("DELETE FROM napp_claims WHERE report_date='2026-07-01' AND scope='region'")
    con.commit()
    rc = mp.region_claims(con, "Ташкентская область")
    ok("region_claims: reason no_sheet, пометка «листа 3.5 нет — показатель пропущен»",
       not rc["available"] and rc["reason"] == "no_sheet" and "листа 3.5" in rc["note"], rc)
    fs = ae.fork_settings({})
    R = aa.fork_data(con, cls="8", region="Ташкентская область", group="property", fs=fs)["region"]
    ci = next(i for i in R["indicators"] if i["id"] == "claims_freq")
    ok("вилка: claims_freq не учтён (why = no_data), остальные показатели считаются",
       not ci["used"] and ci["why"] == "no_data" and any(i["used"] for i in R["indicators"]), ci)
    NP = aa.napp_extra(con, "Ташкентская область")
    from app import act
    items = act._napp_items(NP, "ru", {"region": "Ташкентская область"})
    it = next(x for x in items if x["id"] == "napp_region_claims")
    ok("раздел 4: «Претензии в регионе (НАПП): в отчёте НАПП за срез 01.07.2026 листа 3.5 нет — показатель пропущен»",
       it["status"] == "no_data" and "листа 3.5 нет — показатель пропущен" in it["text"], it["text"])
    # лист отсутствует в разобранном отчёте: строки разреза не пишутся, причина — в журнал
    tmp = Path(tempfile.mkdtemp(prefix="napp-"))
    try:
        f = tmp / Q2_2026.name
        shutil.copytree(Q2_2026, f)
        (f / "3.5.csv").unlink()
        rows, missing = ms.load_claims(f)
        ok("load_claims без листа 3.5: регионов нет, страховщики есть, в missing — 3.5.csv",
           "3.5.csv" in missing and not any(r[1] == "region" for r in rows) and any(r[1] == "company" for r in rows),
           missing)
        (f / "2.13.csv").unlink()
        rows, missing = ms.load_branches(f)
        ok("load_branches без листа 2.13: выплат нет, премии и договоры есть, в missing — 2.13.csv",
           missing == ["2.13.csv"] and all(r[7] is None for r in rows) and any(r[6] for r in rows), missing)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ 5. претензии INSON
def check_company(con):
    print("5. Претензии по страховщикам (2.10 + 2.7 + 2.5): рынок и INSON")
    r10 = rows_of(Q2_2026 / "2.10.csv")
    r7 = rows_of(Q2_2026 / "2.7.csv")
    i10 = next(x for x in r10 if '"INSON"' in x[1])
    i7 = next(x for x in r7 if '"INSON"' in x[1])
    m10 = next(x for x in r10 if x[1].strip() == "Umumiy")
    m7 = next(x for x in r7 if x[1].strip() == "Umumiy")
    cc = mp.company_claims(con)
    co, mk = cc["company"], cc["market"]
    ok(f"INSON: {num(i10[9]):,.0f} претензий, отказано {num(i10[12]):,.0f}, договоров {num(i7[4]):,.0f} (лист 2.10 / 2.7)",
       cc["available"] and co["received"] == num(i10[9]) and co["refused"] == num(i10[12])
       and co["contracts_active"] == num(i7[4]), co)
    ok(f"INSON: {num(i10[9]) / num(i7[4]) * 1000:.4f} на 1 000 договоров; рынок {num(m10[9]) / num(m7[4]) * 1000:.4f}",
       abs(co["per_1000"] - num(i10[9]) / num(i7[4]) * 1000) < 1e-4
       and abs(mk["per_1000"] - num(m10[9]) / num(m7[4]) * 1000) < 1e-4, (co["per_1000"], mk["per_1000"]))
    ok("средняя выплата INSON = выплаты 2.5 × 1 000 000 / оплачено", co["avg_payout"] == round(
        co["payouts_mln"] * 1e6 / co["paid"]), co)
    ok("ключ INSON — как в листе 2.1 (company:INSON AJ), источник — листы 2.10, 2.7, 2.5",
       cc["company_key"] == mp.INSON_ROW == "company:INSON AJ" and cc["source"]["sheets"] == "2.10, 2.7, 2.5")
    renamed = q(con, "SELECT COUNT(*) n FROM napp_claims WHERE key IN ('company:INGO-UZBEKISTON AJ', "
                     "'company:MOSAIC INSURANCE COMPANY AJ QK')")[0]["n"]
    ok("переименованные страховщики склеены под новым ключом (как в market_stats)", renamed == 0, renamed)


# ------------------------------------------------------------------ 6. подразделения и /stats
def check_branches(con):
    print("6. Обособленные подразделения (листы 2.12–2.14)")
    n = q(con, "SELECT report_date, COUNT(*) n FROM napp_branches GROUP BY 1")
    ok(f"подразделения загружены по 5 отчётам: {n}", len(n) == 5 and all(x["n"] > 400 for x in n), n)
    r12 = rows_of(Q2_2026 / "2.12.csv")
    ins = next(x for x in r12 if '"INSON"' in x[1])
    col = next(j for j, c in enumerate(r12[3]) if c.strip().startswith("Toshkent") and "viloyati" in c)
    g = q(con, "SELECT * FROM napp_branches WHERE report_date='2026-07-01' AND company_key='company:INSON AJ' "
               "AND region_key='region:TOSHKENT'")[0]
    ok(f"INSON, Ташкентская область: премии {num(ins[col]):.3f} млн сум — как в CSV", g["premiums_mln"] == num(ins[col]), g)
    tot = q(con, "SELECT * FROM napp_branches WHERE report_date='2026-07-01' AND company_key='company:INSON AJ' "
                 "AND region_key='total'")[0]
    s = q(con, "SELECT SUM(premiums_mln) p, SUM(contracts) c FROM napp_branches WHERE report_date='2026-07-01' "
               "AND company_key='company:INSON AJ' AND region_key<>'total'")[0]
    ok("сумма по регионам = итог компании (премии и договоры)", abs(s["p"] - tot["premiums_mln"]) < 0.01
       and s["c"] == tot["contracts"], (s, tot))
    mk = q(con, "SELECT contracts FROM napp_branches WHERE report_date='2026-07-01' AND company_key='total' "
                "AND region_key='total'")[0]["contracts"]
    ok("договоры рынка в 2.14 = действующие договоры всего по листу 3.4 (12 955 594)", mk == 12955594, mk)
    b = mp.branches(con, "Ташкентская область")
    ok("INSON в Ташкентской области: убыточность и средняя премия против компании",
       b["available"] and b["region"]["loss_ratio_pct"] == round(g["payouts_mln"] / g["premiums_mln"] * 100, 3)
       and b["company"]["loss_ratio_pct"] == round(tot["payouts_mln"] / tot["premiums_mln"] * 100, 3)
       and b["region"]["avg_premium"] == round(g["premiums_mln"] * 1e6 / g["contracts"]), b)
    b = mp.branches(con, "Джизакская область")
    ok("регион без премий INSON → «подразделения INSON в регионе не выделены»", not b["available"]
       and b["reason"] == "not_listed" and "не выделены" in b["note"], b.get("note"))
    b = mp.branches(con, "Ташкент", company_key="company:НЕТ ТАКОЙ")
    ok("страховщика нет в листе → «в отчёте НАПП подразделения НЕТ ТАКОЙ не выделены» (название из ключа)",
       b["reason"] == "not_listed" and b["note"] == "в отчёте НАПП подразделения НЕТ ТАКОЙ не выделены", b.get("note"))
    b = mp.branches(con, "Джизакская область", company_key="company:QUANTUM INSURANCE AJ")
    ok("название страховщика в пометках — из отчёта, а не «INSON»", "INSON" not in (b.get("note") or "")
       and b.get("company_label") and "QUANTUM" in b["company_label"], (b.get("company_label"), b.get("note")))


def check_http(path):
    print("6б. /market/branches, /market/claims и страница /stats")
    db.DB_PATH = path
    from app import main
    t = main.market_branches(date="2026-07-01", company="company:INSON AJ")
    ok("/market/branches: 14 регионов INSON, итог, рынок, источник", len(t["rows"]) == 14 and t["total"]
       and t["market"] and t["source"]["url"].startswith("https://napp.uz") and t["date"] == "2026-07-01", t.get("date"))
    t2 = main.market_branches(date="2026-03-31", company="company:INSON AJ")
    ok("срез без листов 2.12–2.14 → ближайший более ранний (01.01.2026)", t2["date"] == "2026-01-01", t2["date"])
    ser = main.market_series(row="multi_general")["points"]
    nt = {p["date"]: p["note"] for p in ser if p.get("note")}
    ok("/market/series (страница /stats): у итога комплексного IV кв. 2025 — пометка «взята сумма пакетов»",
       sorted(nt) == ["2025-01-01", "2026-01-01"] and all(v.startswith(ms.MULTI_NOTE) for v in nt.values()), nt)
    page0 = main.stats_page(embed=0)
    body0 = page0 if isinstance(page0, str) else page0.body.decode("utf-8")
    ok("/stats: в таблице и CSV-выгрузке страницы — колонка «Примечание»",
       body0.count("Примечание") >= 2 and "p.note" in body0)
    c = main.market_claims(region="Ташкентская область")
    ok("/market/claims: регион и INSON", c["region"]["available"] and c["company"]["available"])
    from app import guard
    ok("чтение подразделений и претензий открыто как /market/series (guard)",
       {"/market/branches", "/market/claims"} <= guard.GUEST_GET_EXACT)
    page = main.stats_page(embed=0)
    body = page if isinstance(page, str) else page.body.decode("utf-8")      # page() отдаёт готовый HTML
    ok("/stats: блок «Подразделения» (chBr) и запрос /market/branches", 'id="chBr"' in body
       and "/market/branches" in body and "Подразделения" in body)
    if shutil.which("node"):
        js = "\n".join(x.split("</script>")[0] for x in body.split("<script>")[1:])
        r = subprocess.run(["node", "-e", "new Function(require('fs').readFileSync(0,'utf8'))"], input=js,
                           capture_output=True, text=True, encoding="utf-8")
        ok("/stats: скрипт страницы без синтаксических ошибок (node)", r.returncode == 0, r.stderr[-300:])
    from app import refsync
    ok("refsync: napp_claims и napp_branches доводятся при старте (режим add — данные диска не теряются)",
       ("napp_claims", ("report_date", "scope", "key"), "add", ()) in refsync.TABLES
       and ("napp_branches", ("report_date", "company_key", "region_key"), "add", ()) in refsync.TABLES)


def check_act_lines(con):
    print("7. Раздел 4 акта: строки претензий и подразделений на трёх языках")
    from app import act
    NP = aa.napp_extra(con, "Ташкентская область")
    for lang, want in (("ru", ("Претензии в регионе:", "на 1 000 договоров, по республике", "доля отказов",
                               "Претензии: рынок", "/ INSON",
                               "Подразделения INSON в регионе (все вместе, по отчёту НАПП): убыточность",
                               "против среднего по компании", "В поправку ставки не входит",
                               "89\u00a0% — город Ташкент", "Оговорки:", "не страховые случаи")),
                       ("uz", ("Hududdagi daʼvolar:", "Daʼvolar: bozor", "INSONning hududdagi boʻlinmalari",
                               "Izohlar:")),
                       ("en", ("Claims in the region:", "Claims: market", "INSON subdivisions in the region",
                               "Caveats:", "Not part of the rate adjustment"))):
        items = act._napp_items(NP, lang, {"region": "Ташкентская область"})
        text = " | ".join(x["text"] for x in items)
        ok(f"{lang}: три строки с источником НАПП и срезом", len(items) == 3 and all(w in text for w in want)
           and all(x["sources"] and x["sources"][0]["url"].startswith("https://napp.uz") for x in items)
           and all(x["source_lines"] for x in items), text[:400])
        if lang == "en":
            import re
            ok("en: без кириллицы", not re.search(r"[А-Яа-яЁё]", text + json.dumps(
                [x["sources"] for x in items], ensure_ascii=False)), text[:300])
    rc = next(x for x in act._napp_items(NP, "ru", {"region": "Ташкентская область"}) if x["id"] == "napp_region_claims")
    ok("строка претензий региона — в форме показателя экрана (value_text, vs_country, sources)",
       rc["value_text"].endswith("на 1 000 договоров") and rc["vs_country"]["diff_pct"] < 0 and rc["scope"] == "region")


def main():
    with temp_db() as path:
        ms.DB = path
        n, dates = ms.build()
        # поправка региона по жилому фонду (−3,2 %) и прочие показатели вилки — из stat_series (stat.uz);
        # на чистой машине они берутся из снимка tests/fixtures/stat_series.json.gz
        ensure_stat_series()
        print(f"market_stats на копии: {n} строк, срезы {dates[0]}…{dates[-1]}")
        con = db.connect_path(path)              # как у сервера: строки sqlite3.Row
        try:
            check_packs(con)
            check_product_rows(con)
            reg, rep = check_region_claims(con)
            check_fork(con, reg, rep)
            check_company(con)
            check_branches(con)
            check_act_lines(con)
            check_http(path)
            check_missing(con)
        finally:
            con.close()
            ms.DB = None
    print()
    if FAILED:
        print("Не пройдено: %d — %s" % (len(FAILED), "; ".join(FAILED)))
        sys.exit(1)
    print("Все проверки пройдены.")


if __name__ == "__main__":
    main()
