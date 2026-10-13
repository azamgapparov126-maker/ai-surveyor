"""
Модель ставки по разделу 3 ТЗ разработчику от 13.10.2026 (app/rate_model.py, задачи 3.1, 4.1, 4.2).

Запуск из корня:
    PYTHONIOENCODING=utf-8 sandbox/.venv/Scripts/python.exe tests/test_rate_model.py

Эталон ТЗ 13.10.2026 (обязательный): выплаты 180 млн, экспозиция 120 млрд сум·лет, 6 убытков, r_рын 0,120 %,
θ 0,10, c 0,05, L 0,30, K_об 0,9 × 0,85 × 1,10, K_рег 1,05, K_кл 0,95, S 5 млрд, 365 дней:
    r_опыт = 180 млн / 120 млрд × 100                       = 0,150 %
    Z      = √(6 / 1082)                                    = 0,0745 (≈ 0,074; договоры не заданы — трактовка 1)
    нетто  = 0,0745 × 0,150 + 0,9255 × 0,120                = 0,1222 %
    брутто = 0,1222 × (1 + 0,10 + 0,05) / (1 − 0,30)        = 0,2008 %
    r      = 0,2008 × 0,9 × 0,85 × 1,10 × 1,05 × 0,95       = 0,1686 % (≈ 0,169 %)
    P      = 5 000 000 000 × 0,168562 / 100 × 365 / 365     = 8 428 084 сум (±1 сум)
Части с базой — на копии базы (tests/tmpdb.temp_db): рынок НАПП, единый справочник факторов, части продукта.
"""
import random
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from tmpdb import temp_db  # noqa: E402
from app import act_texts as tx, db, engine, rate_model as rm  # noqa: E402

passed, failed = 0, 0
CYR = re.compile(r"[А-Яа-яЁё]")


def ok(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ок  ", name)
    else:
        failed += 1
        print("  ПЛОХО", name, str(extra)[:400])


REF_FACTORS = [
    {"code": "alarm_guard", "group": "protection", "coef": 0.9,
     "label": {"ru": "сигнализация и охрана", "uz": "signalizatsiya va qoʻriqlash", "en": "alarm and guard"}},
    {"code": "sprinkler", "group": "fire", "coef": 0.85,
     "label": {"ru": "спринклеры", "uz": "sprinklerlar", "en": "sprinklers"}},
    {"code": "old", "group": "electrics", "coef": 1.10,
     "label": {"ru": "старая проводка", "uz": "eski elektr simlari", "en": "old wiring"}},
]
REF_IN = {"class_code": "8", "sum_insured": 5e9, "term_days": 365,
          "experience": {"losses": 180e6, "exposure": 120e9, "n_claims": 6},
          "market": {"r_pct": 0.12, "source": "НАПП (эталон ТЗ)", "date": "2026-01-01", "market_rate_pct": 0.32},
          "theta": 0.10, "c": 0.05, "L": 0.30, "k_object_factors": REF_FACTORS, "k_region": 1.05, "k_client": 0.95}


def check_reference():
    print("\nЭталон ТЗ 13.10.2026 (раздел 3)")
    r = rm.compute(REF_IN)
    ok("r_опыт 0,150 %", abs(r["r_exp"] - 0.150) < 1e-9, r["r_exp"])
    ok("Z 0,074 (√(6/1082) = 0,0745)", round(r["Z"], 3) == 0.074 and abs(r["Z"] - 0.0744667) < 1e-6, r["Z"])
    ok("нетто 0,122 %", round(r["r_net"], 3) == 0.122, r["r_net"])
    ok("брутто 0,201 %", round(r["r_gross"], 3) == 0.201, r["r_gross"])
    ok("K_об = 0,9 × 0,85 × 1,10 = 0,8415", abs(r["k_object"] - 0.8415) < 1e-9, r["k_object"])
    ok("r = 0,169 %", round(r["r"], 3) == 0.169, r["r"])
    ok("P = 8 428 084 сум (±1)", abs(r["premium"] - 8_428_084) <= 1, r["premium"])
    steps = [s["step"] for s in r["trace"]]
    need = ["r_exp", "Z", "L", "r_market", "r_net", "theta", "c", "r_gross", "K_object", "K_region", "K_client",
            "r_calc", "r", "premium"]
    ok("rate_trace: все шаги по порядку", all(x in steps for x in need), steps)
    ok("каждый шаг — {step, value, source, date, note}",
       all({"step", "value", "source", "date", "note"} <= set(s) for s in r["trace"]), r["trace"][0])
    ok("уровень риска из K_об 0,8415 — низкий", r["risk_level"] == "low", r["risk_level"])
    deltas = sum(f["premium_delta"] for f in r["factors"])
    ok("вклад множителей в сумах складывается в разницу премий (±5 сум)",
       abs(r["premium_gross"] + deltas - r["premium"]) <= 5, (r["premium_gross"], deltas, r["premium"]))


def check_explain():
    print("\nПояснение ставки (задача 4.2) на эталоне")
    r = rm.compute(REF_IN)
    ex = {lg: tx.rate_explain(r, lg) for lg in ("ru", "uz", "en")}
    ru = ex["ru"]["text"].replace(" ", " ")
    ok("RU: 0,17 %, 0,20 %, 0,12 %", all(x in ru for x in ("0,17 %", "0,20 %", "0,12 %")), ru)
    ok("RU: 4 фактора (из 5 с влиянием ≥ 3 %)", len(ex["ru"]["factors"]) == 4, ex["ru"]["factors"])
    ok("RU: до 3 строк", len(ex["ru"]["lines"]) <= 3, ex["ru"]["lines"])
    en = ex["en"]["text"]
    ok("EN: 0.17%, 0.20%, 0.12%", all(x in en for x in ("0.17%", "0.20%", "0.12%")), en)
    uz = ex["uz"]["text"].replace(" ", " ")
    ok("UZ: 0,17 %, 0,20 %, 0,12 %", all(x in uz for x in ("0,17 %", "0,20 %", "0,12 %")), uz)
    ok("UZ и EN без кириллицы", not CYR.search(uz) and not CYR.search(en), (uz, en))
    r2 = rm.compute(dict(REF_IN, min_pct=0.25))
    ex2 = tx.rate_explain(r2, "ru")["text"].replace(" ", " ")
    ok("при минимуме — «применён минимум INSON 0,25 %»", "минимум INSON 0,25 %" in ex2 and r2["r"] == 0.25, ex2)
    print("   ", ru)


def check_z_and_theta():
    print("\nZ, θ, c, K_кл, уровень")
    cfg = rm.settings({})
    ok("Z = 0 при N < 5", rm.credibility(4, None, cfg)[0] == 0)
    ok("Z = 0 при < 30 договорах", rm.credibility(50, 20, cfg)[0] == 0)
    ok("Z = 1 при N ≥ 1082", rm.credibility(2000, 5000, cfg)[0] == 1.0)
    ok("Z без договоров считается (трактовка 1)", rm.credibility(6, None, cfg)[1] is None)
    th, src, _ = rm.theta_value("8", cfg, 1000, 0.05)
    want = max(0.05, 1.2 * 1.3 * ((1 - 0.05) / (1000 * 0.05)) ** 0.5)
    ok("θ по формуле calibration.risk_load: 1,2 × α(0,9) × √((1−q)/(nq)) = 0,215", src == "formula"
       and abs(th - want) < 1e-9 and round(th, 3) == 0.215, th)
    th2, _s, _n = rm.theta_value("8", cfg, 10_000_000, 0.5)
    ok("θ не ниже 0,05", th2 == 0.05, th2)
    ok("θ по умолчанию: класс 8 — 0,10; класс 5 (меньше 1 000 договоров) — 0,20",
       rm.theta_value("8", cfg)[0] == 0.10 and rm.theta_value("5", cfg)[0] == 0.20)
    cz = [rm.cat_load("8", z, cfg)[0] for z in (5, 6, 7, 8, 9, 10)]
    ok("c: ≤6 — 0; 7 — 0,03; 8 — 0,05; ≥9 — 0,10", cz == [0.0, 0.0, 0.03, 0.05, 0.10, 0.10], cz)
    ok("c: зона не указана — 0,05 (временно); класс 3 — 0", rm.cat_load("9", None, cfg)[0] == 0.05
       and rm.cat_load("3", 9, cfg)[0] == 0.0)
    kc = [rm.k_client(n, cfg) for n in (None, 0, 1, 2, 3)]
    ok("K_кл: 1,00 / 0,95 / 1,10 / 1,25 / ≥3 на рассмотрение",
       [k for k, _ in kc] == [1.0, 0.95, 1.10, 1.25, 1.25] and [x for _, x in kc] == [False] * 4 + [True], kc)
    ok("уровень: 0,95 — низкий; 1,0 — умеренный; 1,2 — высокий",
       [rm.risk_level(k, cfg) for k in (0.95, 1.0, 1.2)] == ["low", "moderate", "high"])
    ok("L по субпортфелям: 8 — 0,729; 3 — 0,591; 14 — 0,625; 13 — 0,703",
       [rm.load_share(c, rm.load_settings({}))[0] for c in ("8", "3", "14", "13")] == [0.729, 0.591, 0.625, 0.703])


def check_bounds():
    print("\nГраницы: нетто, 3 × тарифа, минимум, запасной r_рын, срок")
    base = {"class_code": "13", "sum_insured": 1e9, "term_days": 365, "market": {"r_pct": 0.05}}
    r = rm.compute(dict(base, tariff_pct=0.05, k_object=2.5, losses_count=2))
    ok("сверх 3 × тарифа — ставка = 0,15 % и «на рассмотрение»", r["r"] == 0.15 and r["bound"] == "cap"
       and any(x["code"] == "cap" for x in r["refer"]), (r["r"], r["refer"]))
    r = rm.compute(dict(base, k_object=0.5, k_region=0.5, losses_count=0))
    ok("ниже нетто не опускается", r["r"] == r["r_net"] and r["bound"] == "net", (r["r"], r["r_net"]))
    r = rm.compute(dict(base, min_pct=0.3))
    ok("минимум INSON сверху", r["r"] == 0.3 and r["min_applied"], r["r"])
    r = rm.compute({"class_code": "8", "sum_insured": 1e9, "tariff_pct": 0.05, "seismic_zone": 7})
    ok("нет r_рын → тариф × (1 − L) = 0,05 × 0,271", abs(r["r_market"] - 0.01355) < 1e-9
       and r["r_market_kind"] == "tariff", r["r_market"])
    ok("…и брутто = тариф × (1 + θ + c) = 0,05 × 1,13", abs(r["r_gross"] - 0.0565) < 1e-6, r["r_gross"])
    r = rm.compute(dict(REF_IN, term_days=180))
    ok("P = S × r / 100 × t / 365 (180 дней)", abs(r["premium"] - 5e9 * r["r_exact"] / 100 * 180 / 365) <= 0.5,
       r["premium"])
    r = rm.compute(dict(REF_IN, losses_count=3, k_client=None))
    ok("≥ 3 убытков — на рассмотрение", any(x["code"] == "losses" for x in r["refer"]), r["refer"])
    st = {"rate_model": {"k_region": 1.05, "version": "1.1"}, "load": {"by_group": {"property": 0.7}}}
    r = rm.compute({"class_code": "8", "sum_insured": 1e9, "market": {"r_pct": 0.02}}, st)
    ok("константы — из act_settings: K_рег 1,05 и L 0,70 из настроек, версия 1.1",
       r["k_region"] == 1.05 and r["L"] == 0.7 and r["version"] == "1.1", (r["k_region"], r["L"], r["version"]))
    ok("у настроек модели есть версия и дата", rm.DEFAULTS["version"] and rm.DEFAULTS["date"] == "2026-10-13"
       and rm.LOAD_DEFAULTS["status"] == "временно")


def check_random_1000():
    print("\nАвтотест: 1 000 случайных объектов, r ≥ max(минимум, нетто)")
    rnd = random.Random(20261013)
    bad = []
    for i in range(1000):
        tariff = rnd.choice([None, rnd.uniform(0.01, 3.0)])
        inp = {"class_code": rnd.choice(["1", "3", "7", "8", "9", "13", "14", "16"]),
               "sum_insured": rnd.uniform(1e6, 1e11), "term_days": rnd.randint(1, 1095),
               "market": rnd.choice([None, {"r_pct": rnd.uniform(0.001, 2.0)}]),
               "experience": rnd.choice([None, {"losses": rnd.uniform(0, 5e9), "exposure": rnd.uniform(1e9, 5e11),
                                                "n_claims": rnd.randint(0, 3000),
                                                "n_contracts": rnd.choice([None, rnd.randint(1, 50000)])}]),
               "tariff_pct": tariff, "min_pct": rnd.choice([None, tariff, rnd.uniform(0.01, 1.0)]),
               "base_net_pct": rnd.uniform(0.01, 1.0), "seismic_zone": rnd.choice([None, 6, 7, 8, 9]),
               "k_object_factors": [{"code": str(j), "coef": rnd.uniform(0.6, 1.8)} for j in range(rnd.randint(0, 5))],
               "losses_count": rnd.choice([None, 0, 1, 2, 3, 5])}
        r = rm.compute(inp)
        if not r.get("available"):
            bad.append((i, "нет ставки"))
            continue
        floor = max(r["r_net"], inp["min_pct"] or 0)
        if r["r"] + 1e-6 < floor:
            bad.append((i, r["r"], r["r_net"], inp["min_pct"]))
    ok("1 000 из 1 000: r ≥ max(минимум, нетто)", not bad, bad[:5])
    # тот же автотест на движке калькулятора (engine.calculate) — справочник копии базы, случайные продукты
    with temp_db():
        with db.tx() as con:
            ref = db.load_reference(con)
    prods = [(p, c) for p, cs in ref.product_classes.items() for c in cs[:1]]
    rnd = random.Random(7)
    bad = []
    for i in range(1000):
        p, c = rnd.choice(prods)
        factors = {}
        for f in engine.factors_for(ref, engine.Input(product_code=p, class_code=c, object_type="", value_amount=1,
                                                      sum_insured=1)):
            opts = [o for (ff, o) in ref.coefficients if ff == f]
            if opts and rnd.random() < 0.7:
                factors[f] = rnd.choice(opts)
        S = rnd.uniform(1e6, 5e10)
        inp = engine.Input(product_code=p, class_code=c, object_type="", value_amount=S, sum_insured=S,
                           term_days=rnd.randint(30, 730), factors=factors,
                           losses_count=rnd.choice([None, 0, 1, 2]), seismic_zone=rnd.choice([None, 7, 8, 9]))
        out = engine.calculate(ref, inp)
        rmr = engine.rate_for(ref, inp).get("rate_model") or {}
        floor = max(out["rates"]["min_pct"] or 0, rmr.get("r_net") or 0)
        if out["rates"]["applied_pct"] + 1e-4 < floor:
            bad.append((p, c, out["rates"]["applied_pct"], floor))
    ok("движок: 1 000 случайных объектов, applied ≥ max(минимум, нетто)", not bad, bad[:5])


def check_db():
    print("\nДанные базы: рынок НАПП, единый справочник факторов, части продукта")
    with temp_db():
        with db.tx() as con:
            m3 = rm.market_net(con, "3")
            row = con.execute("SELECT payouts_ytd, liabilities, report_date FROM market_stats WHERE row_key='cls3' "
                              "AND report_date LIKE '%-01-01' ORDER BY report_date DESC LIMIT 1").fetchone()
            want = round(row[0] / row[1] * 100, 6)
            ok("r_рын кл. 3 = выплаты / обязательства за год (НАПП, итог года)", m3 and m3["r_pct"] == want
               and m3["basis"] == "full_year" and m3["date"] == row[2], (m3, want))
            m10 = rm.market_net(con, "10")
            ok("кл. 10: убыточность > 200 % — r_рын не применяется", m10 is not None and m10.get("rejected")
               == "loss_ratio_over_max" and m10["r_pct"] is None, m10)
            ref = db.load_reference(con)
            ok("справочник движка несёт r_рын и настройки модели", (ref.rate_inputs.get("3") or {}).get("market")
               and ref.rate_settings.get("rate_model", {}).get("enabled") is True)
            from app import class_templates as ctpl
            tpl3 = ctpl.current(con, "3")["template"]
            cat = tpl3.get(ctpl.CATALOG_KEY) or []
            codes = [g["code"] for g in cat]
            ok("единый справочник кл. 3: без дублей", len(codes) == len(set(codes)), codes)
            ok("…группы шаблона и coefficients (оператор, моточасы, возраст)",
               {"veh_group", "location", "protection"} <= set(codes)
               and {"spec_operator", "engine_hours", "veh_age"} <= set(codes), codes)
            ok("…без групп, учтённых отдельно (убытки → K_кл, сейсмозона → c, франшиза)",
               not {"loss_history", "seismic", "franchise", "antitheft", "spec_site", "spec_guard"} & set(codes), codes)
            eh = next(g for g in cat if g["code"] == "engine_hours")
            ok("моточасы 6 500 → вариант eh_high", rm.derive_option(eh["derive"], {"engine_hours": 6500}, {})
               == "eh_high")
            ok("год 2021 на 2026 → возраст 5 лет → a7",
               rm.derive_option(rm.DEFAULTS["derive"]["veh_age"], {}, {"year": 2021}, date(2026, 10, 13)) == "a7")
            pp = rm.product_parts(con, "0833")
            ok("части продукта 0833: классы 8 и 13", sorted(p["class_code"] for p in pp) == ["13", "8"], pp)
            ctpl.save(con, "3", dict(tpl3), "тест")
            raw = con.execute("SELECT json FROM class_templates WHERE class_code='3' ORDER BY id DESC LIMIT 1").fetchone()[0]
            ok("сохранение шаблона не пишет справочник факторов в базу", ctpl.CATALOG_KEY not in raw)


def check_settings_and_switch():
    print("\nНастройки модели и переключатель rate_model.enabled")
    ok("правка настроек проверяется", rm.check_settings({}) == [] and
       len(rm.check_settings({"rate_model": {"enabled": "да", "k_region": 0}, "load": {"by_group": {"property": 2}}}))
       == 3)
    import test_risk_analytics as T
    import rm_modes
    from app import risk_analytics as ra
    want = {"model": (3_620_694, 94_604_500, 4_200_000), "legacy": (6_048_000, 612_927_966, 17_496_000)}
    with temp_db():
        with db.tx() as con:
            T.fix_company(con)
        for mode in ("model", "legacy"):
            rm_modes.set_mode(mode)
            with db.tx() as con:
                got = tuple(ra.analyze(con, *ex, thresholds=T.NO_EXT, market={"rate_pct": mk}, as_of=T.AS_OF)
                            ["summary"]["premium"] for ex, mk in ((T.EX1, 0.18), (T.EX2, 0.18), (T.EX3, 0.70)))
            ok(f"контрольные премии ({mode}): " + " / ".join(f"{x:,}".replace(",", " ") for x in want[mode]),
               all(abs(g - w) <= 1 for g, w in zip(got, want[mode])), got)
        rm_modes.set_mode("model")


def main():
    check_reference()
    check_explain()
    check_z_and_theta()
    check_bounds()
    check_random_1000()
    check_db()
    check_settings_and_switch()
    print(f"\nИтого: {passed} ок, {failed} плохо")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
