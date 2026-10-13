"""
Контрольные примеры движка. Запуск:  python tests/test_engine.py
Без pytest: обычные assert, чтобы не ставить лишнего.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["SURVEYOR_NO_BACKGROUND"] = "1"     # пересборка знаний о рынке после рэнкинга в тесте не нужна

from tmpdb import temp_db               # noqa: E402
import rm_modes                         # noqa: E402
from app import db, rankings            # noqa: E402
from app.engine import Input, calculate, min_rate  # noqa: E402


def ref():
    with db.tx() as con:
        return db.load_reference(con)


def test_warehouse_property():
    """Склад 4,2 млрд, продукт 0807: ставка не ниже минимума 0,05%, сумма равна стоимости."""
    R = ref()
    inp = Input(product_code="0807", class_code="8", object_type="Склад",
                value_amount=4.2e9, sum_insured=4.2e9,
                factors={"construction": "mixed", "activity": "warehouse", "protection": "alarm",
                         "seismic": "z8", "wear": "mid", "loss_history": "clean", "franchise": "f0"})
    out = calculate(R, inp)
    assert out["rates"]["applied_pct"] >= 0.05
    assert out["premium"] > 0
    assert any(c["rule"] == "sum_ok" for c in out["checks"])
    assert any(t["kind"] == "франшиза" for t in out["recommendations"])
    print("склад: ставка", out["rates"]["applied_pct"], "% премия", out["premium"], "вердикт", out["verdict"])


def test_over_and_under_insurance():
    R = ref()
    base = dict(product_code="0807", class_code="8", object_type="Склад", factors={"franchise": "f1"})
    over = calculate(R, Input(value_amount=100, sum_insured=120, **base))
    under = calculate(R, Input(value_amount=100, sum_insured=60, **base))
    assert any(c["rule"] == "sum_over_value" and c["status"] == "stop" for c in over["checks"])
    assert over["verdict"] == "отклонено"
    assert any(c["rule"] == "underinsurance" and c["status"] == "warn" for c in under["checks"])
    print("превышение → стоп; недострахование → предупреждение")


def test_credit_rules():
    """Кредит 100 млн, залог 60 млн → допустимо 40 млн; залог 30 млн → 50 млн."""
    R = ref()
    mk = lambda coll, s: Input(product_code="1404", class_code="14", object_type="Кредит",
                               value_amount=100e6, sum_insured=s,
                               credit={"loan_amount": 100e6, "collateral_value": coll,
                                       "policyholder_is_bank": True, "payer_is_bank": True})
    ok = calculate(R, mk(60e6, 40e6))
    assert any(c["rule"] == "credit_uncovered" and c["status"] == "ok" for c in ok["checks"])
    assert not any(c["rule"] == "underinsurance" for c in ok["checks"]), "для кредита недострахование не проверяется"
    assert ok["rates"]["applied_pct"] > 0.5, ok["rates"]   # кредит не может стоить как воздух
    bad = calculate(R, mk(60e6, 50e6))
    assert any(c["rule"] == "credit_uncovered" and c["status"] == "stop" for c in bad["checks"])
    cap = calculate(R, mk(30e6, 50e6))
    assert any(c["rule"] == "credit_uncovered" and c["status"] == "ok" for c in cap["checks"])
    cap2 = calculate(R, mk(30e6, 70e6))
    assert any(c["rule"] == "credit_uncovered" and c["status"] == "stop" for c in cap2["checks"])
    notbank = calculate(R, Input(product_code="1404", class_code="14", object_type="Кредит",
                                 value_amount=100e6, sum_insured=40e6,
                                 credit={"loan_amount": 100e6, "collateral_value": 60e6,
                                         "policyholder_is_bank": False, "payer_is_bank": True}))
    assert any(c["rule"] == "credit_payer" and c["status"] == "stop" for c in notbank["checks"])
    print("кредит: 40 из 100 при залоге 60 — ок; 50 — стоп; при залоге 30 потолок 50 — ок, 70 — стоп")


def test_kasko_per_class():
    """Договор КАСКО: по классам ставка проверяется отдельно, средняя по договору не используется."""
    R = ref()
    mr = min_rate(R, "0311", "юр")
    assert mr["floor"] == 2.5, mr
    assert min_rate(R, "0311", "физ")["floor"] == 2.0
    # КАСКО-часть 2,6694% выше минимума — по этой части нарушения нет
    kasko = calculate(R, Input(product_code="0311", class_code="3", object_type="Легковой",
                               value_amount=146.1e6, sum_insured=146.1e6, applied_rate_pct=2.6694,
                               manual_reason="ставка филиала", payer_type="юр"))
    assert not any(c["rule"] == "min_rate" and c["status"] == "stop" for c in kasko["checks"])
    # средняя по договору 2,2031% — ниже минимума; если бы сравнивали её, был бы ложный стоп
    avg = 8.2e6 / 372.2e6 * 100
    assert round(avg, 4) == 2.2031 and avg < mr["floor"]
    print("КАСКО: минимум 2,5%; часть 2,6694% проходит; средняя 2,2031% не используется")


def test_preventive_measures():
    """Склад без защиты на 4,2 млрд: сигнализация обязательна как условие договора, с экономией."""
    R = ref()
    out = calculate(R, Input(product_code="0807", class_code="8", object_type="Склад",
                             value_amount=4.2e9, sum_insured=4.2e9,
                             factors={"construction": "wood", "activity": "warehouse", "protection": "none",
                                      "seismic": "z9", "wear": "old", "loss_history": "clean", "franchise": "f1"}))
    codes = {m["code"]: m for m in out["preventive_measures"]}
    assert "alarm_install" in codes and codes["alarm_install"]["mandatory"] and codes["alarm_install"]["deadline_days"] == 60
    if rm_modes.legacy():
        assert codes["alarm_install"]["premium_delta"] < 0
    else:
        # модель ставки (ТЗ 13.10.2026): дерево × без защиты × старое здание × 9 баллов — ставка 0,2078 % упирается
        # в 3 × тарифа 0807 (0,15 %, «на рассмотрение»); сигнализация её не опускает ниже границы — экономии нет
        assert codes["alarm_install"]["premium_delta"] in (0, None) or codes["alarm_install"]["premium_delta"] < 0
    assert "fire_treatment" in codes and "wiring_check" in codes and "seismic_survey" in codes
    print("мероприятия:", ", ".join(f"{m['measure'][:30]}… ({'условие' if m['mandatory'] else 'совет'})" for m in out["preventive_measures"]))


def test_capacity_and_retention():
    """Ёмкость: лимит 20% и таблица линий; без отчётности — список запроса данных."""
    from app import capacity as cap
    with db.tx() as con:
        c = cap.capacity(con)
        assert c["limit_per_risk"] is not None and abs(c["limit_per_risk"] - 0.2 * (c["own_funds"] + c["reserves"])) < 1
        assert {r["code"] for r in c["data_request"]} >= {"reserves", "assets"}
        t = cap.retention_table(con, c["limit_per_risk"])
    assert t and t[0]["retention"] == round(c["limit_per_risk"]), "самый безопасный класс удерживает весь лимит"
    prem = {r["net_premium_on_retention"] for r in t}
    assert max(prem) - min(prem) <= 1, "чистая премия с удержания одинакова по классам"
    assert all(a["retention"] >= b["retention"] for a, b in zip(t, t[1:])), "чем выше ставка, тем меньше удержание"
    print("ёмкость: лимит на риск", f"{c['limit_per_risk']:,.0f}", "; удержание от", f"{t[0]['retention']:,.0f}", "до", f"{t[-1]['retention']:,.0f}")


def test_manual_without_reason_blocks():
    R = ref()
    out = calculate(R, Input(product_code="0807", class_code="8", object_type="Склад",
                             value_amount=1e9, sum_insured=1e9, applied_rate_pct=0.1))
    assert any(c["rule"] == "manual_rate" and c["status"] == "stop" for c in out["checks"])
    print("ручная ставка без причины → стоп")


def test_kasko_0311_full_calc():
    """КАСКО «Premium» 0311, юрлицо, ТС 146,1 млн: считается по факторам класса 3, минимум 2,5% применяется."""
    R = ref()
    out = calculate(R, Input(product_code="0311", class_code="3", object_type="Легковой",
                             value_amount=146.1e6, sum_insured=146.1e6, payer_type="юр",
                             factors={"veh_age": "a3", "veh_type": "car", "antitheft": "alarm",
                                      "drivers": "limited", "loss_history": "clean", "franchise": "f1"}))
    assert out["rates"]["technical_pct"] > 0, out["rates"]
    assert out["rates"]["min_pct"] == 2.5 and out["rates"]["applied_pct"] >= 2.5, out["rates"]
    assert out["premium"] > 0
    # применились именно факторы класса 3, а не класса 8
    names = " ".join(str(c.get("name", "")) for c in out["explanation"])
    assert "Возраст ТС" in names and "Противоугонная" in names and "Конструкция" not in names, names
    print("КАСКО 0311:", out["rates"]["technical_pct"], "% техническая →", out["rates"]["applied_pct"],
          "% применённая, премия", out["premium"])


def test_cargo_0701():
    """Груз 0701: факторы класса 7, минимум 0,2% из тарифной политики."""
    R = ref()
    out = calculate(R, Input(product_code="0701", class_code="7", object_type="Груз",
                             value_amount=800e6, sum_insured=800e6,
                             factors={"cargo_mode": "auto", "cargo_pack": "container", "cargo_route": "cis",
                                      "cargo_transship": "t1", "cargo_nature": "fragile",
                                      "loss_history": "clean", "franchise": "f1"}))
    assert out["rates"]["technical_pct"] > 0 and out["premium"] > 0, out["rates"]
    assert out["rates"]["min_pct"] == 0.2, out["rates"]
    assert out["rates"]["applied_pct"] >= 0.2
    names = " ".join(str(c.get("name", "")) for c in out["explanation"])
    assert "Характер груза" in names and "Маршрут" in names, names
    print("груз 0701:", out["rates"]["applied_pct"], "% премия", out["premium"])


def test_liability_1329():
    """Общая гражданская ответственность 1329: свои факторы, минимум 0,5%."""
    R = ref()
    out = calculate(R, Input(product_code="1329", class_code="13", object_type="Общегражданская ответственность",
                             value_amount=500e6, sum_insured=500e6,
                             factors={"liab_activity": "trade", "liab_turnover": "t10",
                                      "liab_claims": "clean", "liab_limit": "l1000", "franchise": "f1"}))
    assert out["rates"]["technical_pct"] > 0 and out["premium"] > 0, out["rates"]
    assert out["rates"]["min_pct"] == 0.5 and out["rates"]["applied_pct"] >= 0.5, out["rates"]
    names = " ".join(str(c.get("name", "")) for c in out["explanation"])
    assert "Вид деятельности" in names and "Лимит ответственности" in names, names
    print("ответственность 1329:", out["rates"]["applied_pct"], "% премия", out["premium"])


def test_credit_1404_factors_and_mode():
    """Кредит 1404 — режим «по согласованию с ЦО»: минимум не применяется, в проверках подсказка."""
    R = ref()
    with db.tx() as con:
        mode = db.rows(con, "SELECT pricing_mode FROM products WHERE code='1404'")[0]["pricing_mode"]
    assert mode == "по согласованию", mode
    inp = Input(product_code="1404", class_code="14", object_type="Кредит", pricing_mode=mode,
                value_amount=100e6, sum_insured=40e6,
                factors={"credit_collateral": "partial", "credit_term": "t36",
                         "credit_industry": "trade", "credit_history": "clean"},
                credit={"loan_amount": 100e6, "collateral_value": 60e6,
                        "policyholder_is_bank": True, "payer_is_bank": True})
    out = calculate(R, inp)
    assert out["rates"]["technical_pct"] > 0 and out["premium"] > 0, out["rates"]
    assert out["rates"]["min_pct"] is None, "у продукта по согласованию минимального тарифа нет"
    assert any(c["rule"] == "pricing_mode" and "ЦО" in c["title"] for c in out["checks"]), out["checks"]
    names = " ".join(str(c.get("name", "")) for c in out["explanation"])
    assert "Обеспечение" in names and "Кредитная история" in names, names
    print("кредит 1404:", out["rates"]["applied_pct"], "% (по согласованию с ЦО), премия", out["premium"])


def test_statutory_rate_products():
    """1002 ОСГО владельцев ТС — тариф установлен нормативным актом: ручные 0,01% не проходят молча."""
    R = ref()
    with db.tx() as con:
        mode = db.rows(con, "SELECT pricing_mode FROM products WHERE code='1002'")[0]["pricing_mode"]
    assert mode == "нормативный акт", mode
    inp = Input(product_code="1002", class_code="10", object_type="Легковой", pricing_mode=mode,
                value_amount=100e6, sum_insured=100e6,
                applied_rate_pct=0.01, manual_reason="ставка филиала")
    out = calculate(R, inp)
    assert out["rates"]["min_pct"] is None, "ставки из акта в справочнике пока нет"
    hit = [c for c in out["checks"] if c["rule"] == "statutory_rate"]
    assert hit and hit[0]["status"] != "ok", out["checks"]
    assert "нормативн" in hit[0]["title"].lower() or "нормативн" in hit[0]["detail"].lower()
    assert out["verdict"] != "ок", out["verdict"]
    print("1002: ручные 0,01% →", out["verdict"], "+ проверка «" + hit[0]["title"] + "»")


def test_multi_class_product_does_not_break():
    """Продукт из нескольких классов (0305: ТС + НС + ОТВ) считается по первому классу, остальные — отдельно."""
    R = ref()
    out = calculate(R, Input(product_code="0305", class_code="3", object_type="Легковой",
                             value_amount=200e6, sum_insured=200e6, payer_type="юр",
                             factors={"veh_age": "a7", "franchise": "f1"}))
    hint = [c for c in out["checks"] if c["rule"] == "multi_class"]
    assert hint and "отдельный расчёт" in hint[0]["detail"], out["checks"]
    assert out["rates"]["applied_pct"] > 0
    # вторая часть того же договора считается как отдельный условный договор и не падает
    part2 = calculate(R, Input(product_code="0305", class_code="1", object_type="Человек",
                               value_amount=50e6, sum_insured=50e6))
    assert part2["rates"]["technical_pct"] > 0 and part2["premium"] > 0
    print("0305: часть класса 3 —", out["rates"]["applied_pct"], "%, часть класса 1 —",
          part2["rates"]["applied_pct"], "%")


def test_every_class_with_products_is_calculable():
    """У каждого класса, где есть продукты, есть базовая ставка и расчёт даёт ставку больше нуля."""
    R = ref()
    with db.tx() as con:
        rows = db.rows(con, """SELECT class_code, COUNT(*) n FROM product_classes
                               WHERE part_no = 1 GROUP BY class_code""")
        objs = db.rows(con, "SELECT class_code, object_type FROM base_rates")
    by_class = {}
    for o in objs:
        by_class.setdefault(o["class_code"], []).append(o["object_type"])
    missing = [r["class_code"] for r in rows if r["class_code"] not in by_class]
    assert not missing, f"классы без базовой ставки: {missing}"
    for r in rows:
        cl = r["class_code"]
        out = calculate(R, Input(product_code="—", class_code=cl, object_type=by_class[cl][0],
                                 value_amount=1e9, sum_insured=1e9))
        assert out["rates"]["technical_pct"] > 0, (cl, out["rates"])
    print("классов с продуктами:", len(rows), "— по всем считается ставка")


def test_purpose_analysis():
    """Правка заказчика 22.09.2026: в анализе риска нет проверок оформления и заявления-анкеты."""
    R = ref()
    inp = Input(product_code="0807", class_code="8", object_type="здание",
                value_amount=1e9, sum_insured=1e9)
    contract = calculate(R, inp)
    analysis = calculate(R, inp, purpose="analysis")
    rules_c = {c["rule"] for c in contract["checks"]}
    rules_a = {c["rule"] for c in analysis["checks"]}
    assert {"premium_unpaid", "disclosure"} <= rules_c, rules_c
    assert not ({"premium_unpaid", "disclosure"} & rules_a), rules_a
    miss_c = next(c["detail"] for c in contract["checks"] if c["rule"] == "docs_missing")
    miss_a = next(c["detail"] for c in analysis["checks"] if c["rule"] == "docs_missing")
    assert "Заявление-анкета на страхование" in miss_c, miss_c
    assert "аявлен" not in miss_a, miss_a
    assert contract["premium"] == analysis["premium"]
    print("purpose=analysis: без оплаты, раскрытия и заявления; премия та же", analysis["premium"])


if __name__ == "__main__":
    # копия базы: на чистой машине (git clone → tools/db_build.py) рэнкинга ещё нет, а ёмкость считается от
    # собственных средств и резервов из него — загружаем его в копию тем же путём, что сервер при старте
    with temp_db("surveyor-engine-test.db"):
        rankings.ensure_loaded()
        # оба режима: модель ставки ТЗ 13.10.2026 (по умолчанию) и прежний путь (rate_model.enabled = false)
        rm_modes.run_both([fn for name, fn in list(globals().items()) if name.startswith("test_")], "движок:")
    print("Все проверки пройдены.")
