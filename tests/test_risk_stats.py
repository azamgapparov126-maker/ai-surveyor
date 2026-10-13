"""
Показатели риска по классам из открытых данных (app/risk_stats.py) и составляющая уровня риска
«Внешняя статистика региона» (app/risk_analytics.py). Задача заказчика от 22.09.2026.

Запуск из корня:
    PYTHONIOENCODING=utf-8 sandbox/.venv/Scripts/python.exe tests/test_risk_stats.py

Всё идёт в копию базы (tests/tmpdb.temp_db). В копии ряды stat_series нужных наборов заменяются
зафиксированными числами — ожидания ниже посчитаны вручную и не зависят от суточного обновления.

Ряды (население — тыс. человек на начало года):
  население 2024 и 2025: республика 40 000; Навоийская обл. 1 000; Андижанская, Ташкентская,
                         Кашкадарьинская обл. и город Ташкент — по 3 000
  класс 3, Навоийская обл.:
    ДТП 2023: регион 250; ДТП 2024 (набор road_accidents_2024): республика 10 000, регион 300
        на 1 000 жителей: 300 / 1 000 = 0,30; республика 10 000 / 40 000 = 0,25; отношение 1,2 → 70 баллов
        изменение 2024 к 2023: 300 / 250 − 1 = +20 %
    пострадавшие 2024: республика 12 000 (0,30), регион 240 (0,24) → 0,8 → 30 баллов
    составляющая = (70 + 30) / 2 = 50
  класс 8, город Ташкент (2025): площадь фонда: республика 1 000 000 000 кв. м, город 100 000 000;
    глинобитные: 100 000 / 1 000 тыс. кв. м; сырцовый кирпич: 200 000 / 4 000
    доля: республика 300 000 × 1 000 / 1 000 000 000 × 100 = 30 %; город 5 000 × 1 000 / 100 000 000 × 100 = 5 %
    отношение 0,167 → 0 баллов
  класс 9, Ташкентская обл. (2025): преступления: республика 120 000 (3,0), регион 13 500 (4,5) → 1,5 → 100;
    кражи: республика 40 000 (1,0), регион 2 400 (0,8) → 0,8 → 30; составляющая 65
  класс 9 (для примера 2), город Ташкент (2025): преступления 9 000 (3,0 → 1,0 → 50), кражи 3 000 (1,0 → 50)
  класс 13: только республика (травматизм 2020 по отраслям) → в балл не входит, составляющая выпадает
  класс 16у, Кашкадарьинская обл.: сбор зерна 2014–2024; годовые изменения республики ±10 %,
    региона ±20 % → изменчивость региона вдвое выше → 100 баллов
  класс 14, Андижанская обл. (2024): ликвидации: республика 40 000 (1,0), регион 2 400 (0,8) → 30 баллов

Уровень риска (веса по умолчанию: ставка 0,25, MFL 0,25, убытки 0,20, сумма 0,10, сейсмика 0,20,
внешняя статистика 0,10; неприменимые исключаются, остальные нормируются):
  пример 1 (склад, класс 9): все прежние составляющие 0 баллов, сейсмика не применяется;
    внешняя статистика 65 × 0,10 / 0,90 = 7,2 → «Низкий» (было 0,0 → «Низкий»); премия 6 048 000 та же
  пример 2 (производство 8/9, г. Ташкент): внешняя статистика = среднее (0; 50; 50) = 33,3;
    балл = (57,8 × 1,0 + 33,3 × 0,10) / 1,10 ≈ 55,6 → «Повышенный» (было 57,8); премия — по модели ставки ТЗ 13.10.2026
"""
import statistics
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from tmpdb import temp_db  # noqa: E402
from app import db, risk_analytics as ra, risk_stats as rs, stat_sources as ss, statagency  # noqa: E402
import test_risk_analytics as T  # noqa: E402

AS_OF = date(2026, 9, 21)
NAV, AND, TSH, TSHC, QASH = ("region:NAVOIY", "region:ANDIJON", "region:TOSHKENT", "region:TOSHKENT SHAHRI",
                             "region:QASHQADARYO")
FAILED = []


def check(what, got, want, tol=None):
    ok = (abs(got - want) <= tol) if tol is not None and got is not None else got == want
    print(("  ok  " if ok else "  СБОЙ ") + "%s: %s" % (what, got) + ("" if ok else " (ожидалось %s)" % want))
    if not ok:
        FAILED.append(what)


def truth(what, cond, detail=""):
    print(("  ok  " if cond else "  СБОЙ ") + what + ("" if cond else " — %s" % detail))
    if not cond:
        FAILED.append(what)


def put(con, ds_id, region, period, value):
    d = ss.DATASETS[ds_id]
    reg = region if d["regions"] else ""
    key = region if d["regions"] else ("1700" if d["source"] == "stat.uz" else "Jami")
    con.execute("INSERT OR REPLACE INTO stat_series (source,dataset_id,key,region,period,value,unit,fetched_at,url) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (d["source"], ds_id, key, reg, period, value, d["unit"], "2026-09-22T12:00:00+05:00", d["page"]))


def seed(con):
    """Зафиксированные ряды вместо живых (см. шапку файла)."""
    ids = ["population", "road_accidents", "road_accidents_2024", "road_injured", "road_injured_2024",
           "housing_walls_adobe", "housing_walls_raw_brick", "housing_fund_area", "crimes_total", "thefts",
           "grain_production", "enterprises_liquidated", "work_injury_fatal_rate", "work_injury_nonfatal_rate",
           "work_accidents", "disability_work_injury_m", "disability_work_injury_f"]
    con.execute("DELETE FROM stat_series WHERE dataset_id IN (%s)" % ",".join("?" * len(ids)), ids)
    for y in ("2024", "2025"):
        for reg, v in (("total", 40000.0), (NAV, 1000.0), (AND, 3000.0), (TSH, 3000.0), (TSHC, 3000.0),
                       (QASH, 3000.0)):
            put(con, "population", reg, y, v)
    put(con, "road_accidents", NAV, "2023", 250)
    put(con, "road_accidents", "total", "2023", 9800)
    put(con, "road_accidents_2024", NAV, "2024", 300)
    put(con, "road_accidents_2024", "total", "2024", 10000)
    put(con, "road_injured_2024", NAV, "2024", 240)
    put(con, "road_injured_2024", "total", "2024", 12000)
    put(con, "housing_fund_area", "total", "2025", 1_000_000_000)
    put(con, "housing_fund_area", TSHC, "2025", 100_000_000)
    put(con, "housing_walls_adobe", "total", "2025", 100_000)
    put(con, "housing_walls_adobe", TSHC, "2025", 1_000)
    put(con, "housing_walls_raw_brick", "total", "2025", 200_000)
    put(con, "housing_walls_raw_brick", TSHC, "2025", 4_000)
    put(con, "crimes_total", "total", "2025", 120_000)
    put(con, "crimes_total", TSH, "2025", 13_500)
    put(con, "crimes_total", TSHC, "2025", 9_000)
    put(con, "thefts", "total", "2025", 40_000)
    put(con, "thefts", TSH, "2025", 2_400)
    put(con, "thefts", TSHC, "2025", 3_000)
    put(con, "enterprises_liquidated", "total", "2024", 40_000)
    put(con, "enterprises_liquidated", AND, "2024", 2_400)
    put(con, "work_injury_fatal_rate", "", "2020", 2.9)
    c, r = 1000.0, 1000.0
    for i, y in enumerate(range(2014, 2025)):
        put(con, "grain_production", "total", str(y), round(c, 6))
        put(con, "grain_production", QASH, str(y), round(r, 6))
        c *= 1.10 if i % 2 == 0 else 0.90
        r *= 1.20 if i % 2 == 0 else 0.80


def ind(items, id_):
    return next(i for i in items if i["id"] == id_)


def has_source(i):
    return bool(i["src"]) and all(s["url"].startswith("https://") and s["id"] for s in i["src"])


# --------------------------------------------------------------------------- #

def test_parsers():
    print("\nРазбор наборов data.egov.uz (регионы по-узбекски, годовые столбцы)")
    check("Andijon → Андижанская область", ss.egov_region("Andijon"), AND)
    check("Andijon viloyati → Андижанская область", ss.egov_region("Andijon viloyati"), AND)
    check("Andijon sh. — город, не регион", ss.egov_region("Andijon sh."), "")
    check("Toshkent sh. → город Ташкент", ss.egov_region("Toshkent sh. "), TSHC)
    check("Toshkent → Ташкентская область", ss.egov_region("Toshkent"), TSH)
    check("O’zbekiston Respublikasi * → республика", ss.egov_region("O’zbekiston Respublikasi *"), "total")
    check("Qoraqalpog'iston\\r\\nRespublikasi", ss.egov_region("Qoraqalpog'iston\r\nRespublikasi"),
          "region:QORAQALPOG'ISTON RESPUBLIKASI")
    check("Amudaryo tumani — район", ss.egov_region("Amudaryo tumani"), "")
    check("2014yil", ss.egov_period_ext("2014yil"), ("2014", "year"))
    check("2014Y", ss.egov_period_ext("2014Y"), ("2014", "year"))
    check("2024yanvardekabr = год", ss.egov_period_ext("2024yanvardekabr"), ("2024", "year"))
    check("2024yanvarsentabr = неполный год", ss.egov_period_ext("2024yanvarsentabr"), ("2024-M01-M09", "ytd"))
    check("2025yanvarMart", ss.egov_period_ext("2025yanvarMart"), ("2025-M01-M03", "ytd"))
    check("2024Dekabr = на конец месяца", ss.egov_period_ext("2024Dekabr"), ("2024-M12", "point"))
    check("Olchovbirligi — не период", ss.egov_period_ext("Olchovbirligi"), (None, None))
    sample = ('[{"Hududlar":"O‘zbekiston \\r\\nRespublikasi","Olchovbirligi":"birlik","2022Y":"9902","2023Y":"9839"},'
              '{"Hududlar":"Andijon viloyati","Olchovbirligi":"birlik","2022Y":"845","2023Y":"766"},'
              '{"Hududlar":"Andijon sh.","Olchovbirligi":"birlik","2022Y":"1","2023Y":"2"},'
              '{"Hududlar":"Toshkent shahri","Olchovbirligi":"birlik","2022Y":"1500","2023Y":"1400"}]')
    recs, _ = ss.parse_egov(sample, ss.DATASETS["road_accidents"])
    check("строк (3 региона × 2 года, город Андижан отброшен)", len(recs), 6)
    by = {(r["region"], r["period"]): r["value"] for r in recs}
    check("Андижанская обл. 2023", by.get((AND, "2023")), 766.0)
    check("город Ташкент 2022", by.get((TSHC, "2022")), 1500.0)
    truth("у каждой строки ссылка на страницу набора",
          all(r["url"] == ss.DATASETS["road_accidents"]["page"] for r in recs))
    ytd = ('[{"Hududlar":"Andijon","2024yanvarmart":"83","2024yanvardekabr":"699"}]')
    recs, _ = ss.parse_egov(ytd, ss.DATASETS["road_accidents_2024"])
    check("из нарастающих столбцов взят только январь–декабрь", [(r["period"], r["value"]) for r in recs],
          [("2024", 699.0)])
    fx = '[{"G1":"USD","G2":"840","G3":"1","G4":"11839.59","G5":"19.09.2026"}]'
    recs, _ = ss.parse_egov(fx, ss.DATASETS["fx_rates"])
    check("курс ЦБ: код, дата, курс", [(r["key"], r["period"], r["value"]) for r in recs],
          [("USD", "2026-09-19", 11839.59)])
    # прежний разбор (набор emergencies без настроек egov) не изменился — его проверяет test_stat_sources.py
    truth("у всех наборов есть class_codes",
          all(d.get("class_codes") for d in ss.DATASETS.values()),
          [d["id"] for d in ss.DATASETS.values() if not d.get("class_codes")])


def test_indicators_by_group():
    print("\nПоказатели по одному классу из каждой группы: 3, 8, 13, 16у, 14")
    with temp_db():
        with db.tx() as con:
            seed(con)
            th = ra.load_thresholds(con)

            items = rs.risk_indicators(con, "3", "Навоийская область", th)
            a = ind(items, "road_accidents")
            check("кл. 3: ДТП, значение", a["value"], 300.0)
            check("кл. 3: ДТП, период", a["period"], "2024")
            check("кл. 3: ДТП на 1 000 жителей", a["per_1000"], 0.3, 1e-9)
            check("кл. 3: изменение к 2023, %", a["trend_pct"], 20.0, 1e-9)
            check("кл. 3: регион / республика", a["level_vs_country"]["ratio"], 1.2, 1e-9)
            check("кл. 3: текст сравнения", a["level_vs_country"]["text"], "выше республики на 20,0%")
            check("кл. 3: балл ДТП", a["points"], 70.0, 1e-9)
            truth("кл. 3: источник — набор 2024 и население, со ссылками", has_source(a) and
                  {s["id"] for s in a["src"]} == {"road_accidents_2024", "population"}, a["src"])
            inj = ind(items, "road_injured")
            check("кл. 3: пострадавшие, балл", inj["points"], 30.0, 1e-9)
            b = rs.external_block(con, ["3"], "Навоийская область", th)
            check("кл. 3: составляющая применима", b["applicable"], True)
            check("кл. 3: балл составляющей", b["points"], 50.0, 1e-9)

            items = rs.risk_indicators(con, "8", "г. Ташкент", th)
            v = ind(items, "vulnerable_housing")
            check("кл. 8: доля уязвимого жилья города, %", v["value"], 5.0, 1e-9)
            check("кл. 8: республика, %", v["level_vs_country"]["country"], 30.0, 1e-9)
            check("кл. 8: балл", v["points"], 0.0, 1e-9)
            truth("кл. 8: три источника со ссылками", has_source(v) and len(v["src"]) == 3, v["src"])
            e = ind(items, "emergencies")
            truth("кл. 8: ЧС — только республика, в балл не входит",
                  e["scope"].startswith("республика") and not e["used_in_score"], e["scope"])

            items = rs.risk_indicators(con, "13", "Самаркандская область", th)
            w = ind(items, "work_injury_fatal_rate")
            check("кл. 13: смертельный травматизм, на 100 000 работников", w["value"], 2.9, 1e-9)
            check("кл. 13: период", w["period"], "2020")
            truth("кл. 13: пометка «разреза по регионам нет»", "нет разреза" in w["scope"], w["scope"])
            truth("кл. 13: источник со ссылкой", has_source(w), w["src"])
            b = rs.external_block(con, ["13"], "Самаркандская область", th)
            check("кл. 13: составляющая не учитывается (нет регионов)", b["applicable"], False)

            items = rs.risk_indicators(con, "16у", "Кашкадарьинская область", th)
            g = ind(items, "grain_volatility")
            rows = [r for r in con.execute(
                "SELECT period, value FROM stat_series WHERE dataset_id='grain_production' AND region=? "
                "ORDER BY period", (QASH,)).fetchall()]
            ch = [(b2 / a2 - 1) * 100 for (_, a2), (_, b2) in zip(rows, rows[1:])]
            check("кл. 16у: изменчивость = стандартное отклонение годовых изменений", g["value"],
                  round(statistics.stdev(ch), 2), 1e-9)
            check("кл. 16у: регион / республика", g["level_vs_country"]["ratio"], 2.0, 0.001)
            check("кл. 16у: балл", g["points"], 100.0, 1e-9)
            check("кл. 16у: период", g["period"], "2014–2024")
            truth("кл. 16у: источник со ссылкой", has_source(g), g["src"])

            items = rs.risk_indicators(con, "14", "Андижанская область", th)
            l = ind(items, "enterprises_liquidated")
            check("кл. 14: ликвидации на 1 000 жителей", l["per_1000"], 0.8, 1e-9)
            check("кл. 14: балл", l["points"], 30.0, 1e-9)
            truth("кл. 14: источник data.egov.uz со ссылкой",
                  has_source(l) and l["src"][0]["source"] == "data.egov.uz", l["src"])
            truth("кл. 14: в NOT_FOUND честно сказано про просрочку", "просроченной" in rs.NOT_FOUND["14"])

            items = rs.risk_indicators(con, "3", None, th)
            a = ind(items, "road_accidents")
            truth("регион не задан → республика с пометкой", a["scope"] == "республика — регион не задан"
                  and a["level_vs_country"] is None, a["scope"])
        # точка подключения открывает своё соединение — зовём её после фиксации рядов
        res = statagency.get_risk_indicators("3", "Навоийская область")
        truth("GET /stat/risk-indicators: показатели и составляющая",
              res["indicators"] and res["component"]["points"] == 50.0 and res["datasets"], res["component"])
        try:
            statagency.get_risk_indicators("99", None)
            truth("неизвестный класс → 404", False)
        except Exception as ex:
            truth("неизвестный класс → 404", getattr(ex, "status_code", None) == 404, ex)


def test_analysis_premium_unchanged():
    print("\nАнализ: премии и сценарии не меняются, уровень сдвигается только новой составляющей")
    with temp_db():
        with db.tx() as con:
            seed(con)
            T.fix_company(con)
            r = ra.analyze(con, *T.EX1, thresholds={}, market={"rate_pct": 0.18}, as_of=AS_OF)
            r0 = ra.analyze(con, *T.EX1, thresholds=T.NO_EXT, market={"rate_pct": 0.18}, as_of=AS_OF)
            # эталон ТЗ 13.10.2026 (модель ставки; было 6 048 000); сценарии названы как в акте (PML ≤ EML ≤ MFL)
            check("склад: премия", r["summary"]["premium"], 3_620_694)
            check("склад: PML / EML / MFL", [r["scenarios"][s]["amount"] for s in ("PML", "EML", "MFL")],
                  [336_000_000, 840_000_000, 1_680_000_000])
            ext = r["external_stats"]
            check("склад: внешняя статистика применима", ext["applicable"], True)
            check("склад: балл составляющей (100 + 30) / 2", ext["points"], 65.0, 1e-9)
            comp = next(c for c in r["level"]["components"] if c["code"] == "external_stats")
            check("склад: вес после нормировки 0,10 / 0,90", comp["weight_norm"], 0.111, 1e-9)
            check("склад: calibrated", comp["calibrated"], 0)
            check("склад: балл уровня риска 65 × 0,1 / 0,9", r["level"]["score"], 7.2, 0.05)
            check("склад: уровень", r["level"]["level"], "Низкий")
            check("склад: без составляющей балл прежний", r0["level"]["score"], 0.0, 0.05)
            truth("склад: в external_stats источники у показателей",
                  all(has_source(i) for i in ext["indicators"] if i["value"] is not None))

            r = ra.analyze(con, *T.EX2, thresholds={}, market={"rate_pct": 0.18}, as_of=AS_OF)
            r0 = ra.analyze(con, *T.EX2, thresholds=T.NO_EXT, market={"rate_pct": 0.18}, as_of=AS_OF)
            check("производство: премия", r["summary"]["premium"], 94_604_500)     # было 612 927 966
            check("производство: PML / EML / MFL",
                  [r["scenarios"][s]["amount"] for s in ("PML", "EML", "MFL")],
                  [11_600_000_000, 20_300_000_000, 34_000_000_000])
            check("производство: без составляющей балл 48 (модель ставки; было 57,8)", r0["level"]["score"], 48.0, 0.15)
            ext = r["external_stats"]
            check("производство: показатели обоих классов (жильё, преступления, кражи)",
                  sorted(i["id"] for i in ext["indicators"] if i["used_in_score"]),
                  ["crimes_total", "thefts", "vulnerable_housing"])
            check("производство: балл составляющей (0 + 50 + 50) / 3", ext["points"], 33.3, 0.05)
            want = (r0["level"]["score"] * 1.0 + ext["points"] * 0.10) / 1.10
            check("производство: балл уровня риска", r["level"]["score"], round(want, 1), 0.15)
            check("производство: уровень", r["level"]["level"], "Повышенный")


def test_component_drops_without_data():
    print("\nКласс без открытых данных: составляющая выпадает, в ответе пояснение")
    with temp_db():
        with db.tx() as con:
            seed(con)
            T.fix_company(con)
            must = {"class_code": "17", "object_type": "Правовая защита", "sum_insured": 100e6,
                    "object_value": 100e6, "region": "Андижанская область", "term_months": 12}
            r = ra.analyze(con, must, {}, thresholds={}, as_of=AS_OF)
            r0 = ra.analyze(con, must, {}, thresholds=T.NO_EXT, as_of=AS_OF)
            ext = r["external_stats"]
            check("кл. 17: показателей нет", ext["indicators"], [])
            check("кл. 17: составляющая не применима", ext["applicable"], False)
            truth("кл. 17: пояснение — чего нет в открытых данных", "судебных дел" in ext["why"], ext["why"])
            comp = next(c for c in r["level"]["components"] if c["code"] == "external_stats")
            check("кл. 17: вес составляющей после нормировки", comp["weight_norm"], 0.0)
            check("кл. 17: балл как без составляющей", r["level"]["score"], r0["level"]["score"])
            truth("кл. 17: в объяснении уровня строка «не учтено»",
                  any("Внешняя статистика региона: не учтено" in x for x in r["level"]["explanation"]))
            must3 = {**T.EX1[0], "region": "Республика Узбекистан"}
            r = ra.analyze(con, must3, T.EX1[1], thresholds={}, market={"rate_pct": 0.18}, as_of=AS_OF)
            truth("регион = вся республика → составляющая не учитывается",
                  r["external_stats"]["applicable"] is False and "республика" in r["external_stats"]["why"])


def test_thresholds_admin():
    print("\nПорог external_ratio и вес меняются админом, проверка ошибок")
    with temp_db():
        with db.tx() as con:
            seed(con)
            errs = ra.check_thresholds(ra._merge(ra.DEFAULT_THRESHOLDS, {"external_ratio": [2, 1]}))
            truth("external_ratio наоборот → ошибка", any("external_ratio" in e for e in errs), errs)
            t = ra.save_thresholds(con, {"external_ratio": [0.8, 1.2], "weights": {"external_stats": 0.2}}, "тест")
            check("сохранено: external_ratio", t["external_ratio"], [0.8, 1.2])
            check("сохранено: вес", t["weights"]["external_stats"], 0.2)
            b = rs.external_block(con, ["3"], "Навоийская область", t)
            # ДТП 1,2 → 100; пострадавшие 0,8 → 0 → среднее 50
            check("новая шкала: балл составляющей", b["points"], 50.0, 1e-9)
            check("новая шкала: балл ДТП", ind(b["indicators"], "road_accidents")["points"], 100.0, 1e-9)


def test_special_regions():
    print("\nОсобые регионы акта (02.10.2026): «Республика Узбекистан» (uz_all) и «Другое» (other)")
    from app import act
    with temp_db():
        with db.tx() as con:
            seed(con)
            th = ra.load_thresholds(con)
            m_all = {"region": "uz_all", "region_code": act.region_code("uz_all")}
            m_out = {"region": "other", "region_code": act.region_code("other"), "region_text": "Kazakhstan"}
            check("uz_all: код", m_all["region_code"], "uz_all")
            check("uz_all: регион для модулей", act.region_for_modules(m_all), "Республика Узбекистан")
            check("uz_all: ключ справочника регионов — вся республика",
                  rs._region_key(act.region_for_modules(m_all)), "total")
            items = rs.risk_indicators(con, "3", act.region_for_modules(m_all), th)
            a = ind(items, "road_accidents")
            truth("uz_all: ДТП — по республике (10 000), регион с республикой не сравнивается",
                  a["value"] == 10000.0 and a["scope"].startswith("республика") and not a.get("level_vs_country"), a)
            b = rs.external_block(con, ["3"], act.region_for_modules(m_all), th)
            check("uz_all: составляющая «внешняя статистика» не применяется", b["applicable"], False)
            check("other: код", m_out["region_code"], "other")
            check("other: фон открытых данных к факторам не берётся", act.region_for_stats(m_out), "")
            check("other: особый регион — вне Узбекистана", act.region_scope(m_out), "outside")
            check("uz_all: особый регион — вся республика", act.region_scope(m_all), "republic")
            an = {"available": True, "stats": {"available": True, "indicators": [{"id": "road_accidents"}]},
                  "napp": {"available": True}}
            act._region_scope_stats(an, "outside")
            truth("other: статистика и НАПП по регионам не применяются (reason = outside)",
                  an["stats"]["available"] is False and an["stats"]["reason"] == "outside"
                  and an["stats"]["indicators"] == [] and an["napp"]["available"] is False, an)


def main():
    test_parsers()
    test_indicators_by_group()
    test_special_regions()
    test_analysis_premium_unchanged()
    test_component_drops_without_data()
    test_thresholds_admin()
    if FAILED:
        print("\nСБОЕВ: %d — %s" % (len(FAILED), "; ".join(FAILED)))
        sys.exit(1)
    print("\nВсе проверки сошлись.")


if __name__ == "__main__":
    main()
