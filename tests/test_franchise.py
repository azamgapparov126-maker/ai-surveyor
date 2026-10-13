"""
Контрольные примеры подбора франшизы (app/franchise.recommend_franchise, what_if).

Запуск из корня:
    PYTHONIOENCODING=utf-8 sandbox/.venv/Scripts/python.exe tests/test_franchise.py

Всё идёт в копию базы (tests/tmpdb.temp_db); рабочая база не открывается.
Объекты те же, что в tests/test_risk_analytics.py (примеры 1–3), плюс примеры на основания.

Решение заказчика 27.09.2026: франшиза необязательна — она предлагается ТОЛЬКО при основании
(поток мелких убытков, высокий/критический уровень, доминирующий риск при неблагоприятном факторе,
запрос клиента). По умолчанию — «Франшиза не требуется» и альтернативы вместо неё.

Множители франшизы (справочник коэффициентов, calibrated = 0): 0% → 1,00; 0,5% → 0,92;
1% → 0,85; 2% → 0,75. Экспертное продолжение (пороги franchise_multipliers): 5% → 0,60;
10% → 0,45; 90% → 0,15. Между узлами — прямая.

Пример A. Низкий риск (склад, класс 9, 4,2 млрд, убытков нет, франшиза в заявке не указана).
    премия = 0,144% × 4,2 млрд = 6 048 000 сум
    основания: мелких убытков нет (0 убытков < порога 2), уровень «Низкий», доминирующего риска нет
    (наибольший риск «Кража со взломом» 45% нетто-ставки, но неблагоприятного фактора по нему нет),
    клиент о снижении премии не просил → решение no_franchise, франшиза «нет», 0%.
    A+. Тот же склад, но клиент просит снизить премию (want_lower_premium):
    основание client_request, вилка «Низкого» 0–1%, низ вилки премию не снижает → 1%
    премия 6 048 000 × 0,85 = 5 140 800 сум (минус 907 200).

Пример B. Повышенный риск (производство 8/9, 50 млрд, сейсмозона 9, 1 убыток 300 млн).
    B1 — франшиза в заявке НЕ указана: премия 666 226 050 сум (= 612 927 966 / 0,92).
    основания: убыток один (порог 2), доля мелких 1,00 — но число убытков ниже порога → нет;
    уровень «Повышенный» сам по себе основанием не является; землетрясение в 9-балльной зоне даёт
    16,7% нетто-ставки, наибольший риск «Пожар» 33,5% — оба ниже порога доминирования 35%;
    запроса клиента нет → франшиза НЕ требуется, премия остаётся 666 226 050 сум.
    B2 — в заявке указана франшиза 0,5% (запрос клиента): базовая премия 612 927 966 сум,
    вилка 2–5%, доля мелких 1,00 > 0,5 → верх вилки 5%;
    премия 612 927 966 × 0,60 / 0,92 = 399 735 630 сум (минус 213 192 336).
    options_custom — премия при запрошенных 0,5%: 612 927 966 сум (это и есть базовый вариант).

Пример C. Критический риск (деревянный склад, горючие материалы, 9-балльная зона, износ 60%,
    3 убытка на 9 млрд, сумма 100 млрд = 50% стоимости 200 млрд).
    базовая премия 4 875 891 429 сум (ставка 4,8759% × 100 млрд)
    основания: уровень «Критический» + доминирующий/неприемлемый риск землетрясения
    вилка 10–90%; 3 убытка ≥ 2 → верх вилки 90%
    премия при 90%: узел движка 2% (×0,75) × (0,15 / 0,75) = базовая × 0,15 = 731 383 714 сум
    плюс франшиза 90% по риску «землетрясение» (доля риска 20,2% нетто):
    минус 731 383 714 × 0,202 × 0,9 = 132 965 559 → итого 598 418 155 сум,
    альтернатива — исключить землетрясение либо отказать.

Пример D. Франшиза только по одному риску (склад 10 млрд, дерево, горючие материалы, без защиты,
    сейсмозона 7, износ 10%, убытков нет). Уровень «Умеренный» → основание только dominant_peril:
    «Пожар» 40,4% нетто-ставки ≥ 35% при горючих конструкциях без защиты.
    По договору франшизы нет (0%), по риску «Пожар» — 2% (верх вилки «Умеренного»):
    премия 119 139 429 → 119 139 429 − 119 139 429 × 0,404 × 0,02 = 118 176 782 сум.

Пример E. Единственное основание — поток мелких убытков (замечание контролёра 27.09.2026).
    Тот же склад класса 9 на 4,2 млрд, но 4 убытка за 3 года на 60 млн, все мелкие
    (порог мелкого убытка 1% суммы = 42 000 000 сум, доля мелких 1,00 > 0,5).
    Уровень «Умеренный» (убытки поднимают балл) — основанием сам по себе не является,
    доминирующего риска нет, клиент ничего не просил → grounds = ['small_losses'].
    Вилка «Умеренного» 1–2%, 4 убытка ≥ порога 2 → верх вилки 2%:
    базовая премия 0,24% × 4,2 млрд = 10 080 000 сум, с франшизой 2% — ×0,75 = 7 560 000 сум.

Пример F. Обязательный вид (продукт 0820 «Строительно-монтажные риски, обязательное», класс 8,
    10 млрд, тариф по ПКМ № 532). Франшиза не применяется:
    decision_code = statutory, тип «нет», 0%, текст «Франшиза не применяется: обязательный вид,
    тариф установлен нормативным актом»; основания по одному не разбираются — одно общее
    объяснение в basis; в options только base и measures, премия 25 936 035 сум.
    what_if(3%) по этому продукту тоже не считает: applied = False, decision_code = statutory,
    премия остаётся 25 936 035 сум.

Пример G. Потолок класса в what_if. Спецтехника, класс 3 (потолок franchise_class_caps 5%),
    сумма 1,2 млрд, базовая премия 17 496 000 сум (тот же объект, что пример 3 анализа рисков).
    Запрос 5% укладывается в потолок — предупреждения нет; запрос 10% считается
    (17 496 000 × 0,75 × 0,45 / 0,75 = 7 873 200 сум), но в ответе есть warning о превышении
    потолка 5% — решение за андеррайтером.
"""
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import rm_modes as M  # noqa: E402
from tmpdb import temp_db  # noqa: E402
from app import db, franchise as fr, risk_analytics as ra  # noqa: E402

AS_OF = date(2026, 9, 21)
NO_EXT = {"weights": {"external_stats": 0}}
MARKET = {"rate_pct": 0.18, "label": "тест"}

EX_A = ({"class_code": "9", "object_type": "Склад", "sum_insured": 4.2e9, "object_value": 4.2e9,
         "region": "Ташкентская область", "term_months": 12, "construction": "reinforced",
         "activity": "warehouse"},
        {"protection": "alarm_guard", "losses_3y": {"count": 0, "amount": 0}})
B_MUST = {"class_code": "8/9", "object_type": "Производство", "sum_insured": 50e9, "object_value": 50e9,
          "region": "г. Ташкент", "term_months": 12, "construction": "reinforced", "activity": "food"}
B_OPT = {"protection": "alarm_guard", "seismic_zone": "8–9", "wear_pct": 30,
         "losses_3y": {"count": 1, "amount": 300e6},
         "compartments": {"count": 4, "largest_value": 18e9},
         "bi": {"monthly_amount": 1e9, "recovery_months": 4}, "fire_station_km": 5, "floors": 2}
EX_B1 = (B_MUST, B_OPT)                                   # франшиза в заявке не указана
EX_B2 = (B_MUST, dict(B_OPT, deductible={"pct": 0.5}))    # клиент сам указал франшизу 0,5%
EX_C = ({"class_code": "8", "object_type": "Склад", "sum_insured": 100e9, "object_value": 200e9,
         "region": "Ферганская область", "term_months": 12, "construction": "wood",
         "activity": "flammable"},
        {"protection": "none", "seismic_zone": 9, "wear_pct": 60,
         "losses_3y": {"count": 3, "amount": 9e9}, "fire_station_km": 25, "floors": 1})
EX_E = ({"class_code": "9", "object_type": "Склад", "sum_insured": 4.2e9, "object_value": 4.2e9,
         "region": "Ташкентская область", "term_months": 12, "construction": "reinforced",
         "activity": "warehouse"},
        {"protection": "alarm_guard", "losses_3y": {"count": 4, "amount": 60e6, "small_count": 4}})
EX_F = ({"class_code": "8", "product_code": "0820", "object_type": "Стройка", "sum_insured": 10e9,
         "object_value": 10e9, "region": "г. Ташкент", "term_months": 12,
         "construction": "reinforced", "activity": "warehouse"},
        {"protection": "alarm_guard", "losses_3y": {"count": 0, "amount": 0}})
EX_G = ({"class_code": "3", "object_type": "Спецтехника — экскаватор", "sum_insured": 1.2e9,
         "object_value": 1.5e9, "region": "Навоийская область", "term_months": 12,
         "vehicle_type": "special", "year": 2021},
        {"protection": "tracker", "losses_3y": {"count": 2, "amount": 150e6}})
EX_D = ({"class_code": "8", "object_type": "Склад", "sum_insured": 10e9, "object_value": 10e9,
         "region": "Ташкентская область", "term_months": 12, "construction": "wood",
         "activity": "flammable"},
        {"protection": "none", "seismic_zone": 7, "wear_pct": 10,
         "losses_3y": {"count": 0, "amount": 0}, "fire_station_km": 3, "floors": 1,
         "compartments": {"count": 3, "largest_value": 3e9}})


# эталон ТЗ 13.10.2026 (модель ставки): {(функция, подпись): значение}; прежние числа — в проверках ниже
NEW = {
    # склад кл. 9, 4,2 млрд: 0,086207 % (r_рын НАПП 0,022356 × 1,10 / 0,271 × K_кл 0,95) — было 6 048 000
    ('test_a_low_no_franchise', 'базовая премия, сум'): 3_620_694,
    ('test_a_low_no_franchise', 'what_if(3%): премия, сум'): 2_534_476,
    ('test_a_low_no_franchise', 'what_if(3%): изменение премии, сум'): -1_086_218,
    ('test_a_client_request', 'премия при франшизе 1%, сум'): 3_077_592,
    # производство 8/9, 50 млрд: 94 604 500 (было 612 927 966; без франшизы 0,5 % — 102 831 000, было 666 226 050)
    ('test_b1_no_grounds', 'премия без франшизы, сум'): 102_831_000,
    ('test_b2_client_request', 'базовая премия (с франшизой 0,5% из заявки), сум'): 94_604_500,
    ('test_b2_client_request', 'премия при франшизе 5%, сум'): 61_698_800,
    ('test_b2_client_request', 'эффект на премию, сум'): -32_905_700,
    ('test_b2_client_request', 'премия при франшизе пользователя, сум'): 94_604_500,
    # деревянный склад: ставка к рынку ниже — уровень «Высокий» вместо «Критического», франшиза 10 % по пожару
    ('test_c_critical', 'уровень риска'): 'Высокий',
    ('test_c_critical', 'франшиза, %'): 10.0,
    ('test_c_critical', 'код решения'): 'accept_conditions',
    ('test_c_critical', 'решение'): 'принять с франшизой и условиями (мероприятия по снижению риска)',
    ('test_c_critical', 'peril code'): 'fire',
    ('test_c_critical', 'peril pct'): 10.0,
    # ставка упирается в 3 × тарифа 0807 (0,15 %) — «на рассмотрение»: 100 млрд × 0,15 % = 150 000 000
    ('test_c_critical', 'базовая премия, сум'): 150_000_000,
    ('test_c_critical', 'премия с франшизой 90% и франшизой по риску, сум'): 86_364_000,
    ('test_d_dominant_peril_only', 'уровень риска'): 'Низкий',
    ('test_d_dominant_peril_only', 'франшиза по риску, %'): 1.0,
    ('test_d_dominant_peril_only', 'базовая премия, сум'): 15_000_000,
    ('test_d_dominant_peril_only', 'премия с франшизой по риску, сум'): 14_939_400,
    ('test_e_small_losses_only', 'базовая премия, сум'): 4_764_060,
    ('test_e_small_losses_only', 'премия с франшизой 2%, сум'): 3_573_024,
    ('test_f_statutory', 'базовая премия, сум'): 7_300_100,
    ('test_f_statutory', 'what_if: премия без изменений, сум'): 7_300_100,
    # экскаватор: минимум 0318 0,35 % × 1,2 млрд = 4 200 000 (было 17 496 000); франшиза ниже минимума не опускает
    ('test_g_cap_warning', 'базовая премия, сум'): 4_200_000,
    ('test_g_cap_warning', 'премия при франшизе 10%, сум'): 4_200_000,
    ('test_thresholds_in_db', 'dominant after 0,30'): False,
}


def eq(got, want, what, tol=1.0):
    want = M.want(NEW, what, want)
    ok = abs(got - want) <= tol if isinstance(want, (int, float)) and not isinstance(want, bool) else got == want
    assert ok, f"{what}: получено {got}, ожидалось {want}"
    print(f"  ok  {what}: {got}")


def fix_company(con):
    for t in ("solvency_reports", "reserve_reports", "company_financials"):
        con.execute(f"DELETE FROM {t}")
    con.execute("INSERT INTO company_financials (report_date, own_funds, reserves, source) VALUES (?,?,?,?)",
                ("2026-07-01", 180e9, 240e9, "временно, тест"))


def run(must, optional, what_if_pct=None):
    with temp_db():
        with db.tx() as con:
            fix_company(con)
            out = fr.recommend_franchise(con, must, optional, thresholds=NO_EXT,
                                         market=MARKET, as_of=AS_OF)
            if what_if_pct is None:
                return out
            wi = fr.what_if(con, must, optional, what_if_pct, thresholds=NO_EXT,
                            market=MARKET, as_of=AS_OF)
            return out, wi


def check_shape(r):
    for k in ("ok", "level", "decision", "decision_code", "franchise", "needed", "grounds",
              "grounds_checked", "basis", "premium_effect", "options", "alternatives",
              "tariff_advice", "explanation", "llm_text", "thresholds_used"):
        assert k in r, f"нет поля {k}"
    keys = [o["key"] for o in r["options"]]
    assert keys[0] == "base" and keys[-1] == "measures", keys      # base и measures — всегда
    assert ("franchise" in keys) == bool(r["needed"]), (keys, r["needed"])
    assert [c["code"] for c in r["grounds_checked"]] == \
           ["small_losses", "level_high", "dominant_peril", "client_request"], r["grounds_checked"]
    assert r["calibrated"] == 0 and r["franchise"]["calibrated"] == 0
    assert r["llm_text"].strip()
    assert "franchise_dominant_share" in r["thresholds_used"], r["thresholds_used"]


def test_a_low_no_franchise():
    print("\nПример A — низкий риск, склад класс 9: франшиза не требуется")
    r, wi = run(*EX_A, what_if_pct=3.0)
    check_shape(r)
    eq(r["level"], "Низкий", "уровень риска")
    eq(r["decision_code"], "no_franchise", "код решения")
    eq(r["needed"], False, "франшиза нужна?")
    eq(r["grounds"], [], "основания")
    eq(r["franchise"]["type"], "нет", "тип франшизы")
    eq(r["franchise"]["pct"], 0.0, "франшиза, %", 1e-9)
    eq(r["franchise"]["text"], "Франшиза не требуется", "текст")
    eq([o["key"] for o in r["options"]], ["base", "measures"], "варианты без франшизы")
    eq(r["options"][0]["premium"], 6_048_000, "базовая премия, сум")
    eq(r["premium_effect"]["delta"], 0, "эффект на премию, сум")
    assert any("уровень риска «Низкий» — сам по себе основанием не является" in c["why"]
               for c in r["grounds_checked"]), r["grounds_checked"]
    assert any("доля мелких выше" in c["why"] for c in r["grounds_checked"])
    assert any("неблагоприятного фактора по нему нет" in c["why"] for c in r["grounds_checked"])
    assert any(a["code"] == "measures" for a in r["alternatives"]), r["alternatives"]
    # пользователь сам считает любой размер
    eq(wi["premium"], 4_233_600, "what_if(3%): премия, сум", 2)
    eq(wi["delta"], -1_814_400, "what_if(3%): изменение премии, сум", 2)


def test_a_client_request():
    print("\nПример A+ — тот же склад, но клиент просит снизить премию")
    r = run(EX_A[0], dict(EX_A[1], want_lower_premium=True))
    check_shape(r)
    eq(r["grounds"], ["client_request"], "основания")
    eq(r["decision_code"], "accept_franchise", "код решения")
    eq(r["franchise"]["pct"], 1.0, "франшиза, %", 1e-9)
    eq(r["franchise"]["amount"], 42_000_000, "франшиза в сумах")
    opt = next(o for o in r["options"] if o["key"] == "franchise")
    eq(opt["premium"], 5_140_800, "премия при франшизе 1%, сум", 2)


def test_b1_no_grounds():
    print("\nПример B1 — производство 8/9, «Повышенный», один убыток: основания нет")
    r = run(*EX_B1)
    check_shape(r)
    eq(r["level"], "Повышенный", "уровень риска")
    eq(r["franchise"]["band_pct"], [2, 5], "вилка по уровню (не применяется)")
    eq(r["grounds"], [], "основания")
    eq(r["decision_code"], "no_franchise", "код решения")
    eq(r["franchise"]["pct"], 0.0, "франшиза, %", 1e-9)
    eq(r["options"][0]["premium"], 666_226_050, "премия без франшизы, сум", 2)
    eq(r["premium_effect"]["delta"], 0, "эффект на премию, сум")
    assert any("«Повышенный» даёт основание только вместе" in c["why"] for c in r["grounds_checked"])
    assert any("порог 35%" in c["why"] for c in r["grounds_checked"]), r["grounds_checked"]
    assert any(a["code"] == "exclude_peril" for a in r["alternatives"]), r["alternatives"]
    assert r["tariff_advice"]["rate_market_pct"] == 0.18
    assert r["tariff_advice"]["raises"], "должны быть факторы, поднимающие ставку"


def test_b2_client_request():
    print("\nПример B2 — то же производство, франшиза 0,5% указана в заявке")
    r = run(*EX_B2)
    check_shape(r)
    eq(r["grounds"], ["client_request"], "основания")
    eq(r["decision_code"], "accept_franchise", "код решения")
    eq(r["franchise"]["pct"], 5.0, "франшиза, %", 1e-9)
    eq(r["franchise"]["type"], "безусловная", "тип франшизы")
    eq(r["franchise"]["amount"], 2_500_000_000, "франшиза в сумах")
    base = r["options"][0]["premium"]
    eq(base, 612_927_966, "базовая премия (с франшизой 0,5% из заявки), сум", 2)
    want = base * 0.60 / 0.92
    opt = next(o for o in r["options"] if o["key"] == "franchise")
    eq(opt["premium"], round(want), "премия при франшизе 5%, сум", 2)
    eq(r["premium_effect"]["delta"], round(want - base), "эффект на премию, сум", 2)
    assert r.get("options_custom"), "нет расчёта по размеру, который задал пользователь"
    eq(r["options_custom"]["franchise_pct"], 0.5, "франшиза пользователя, %", 1e-9)
    eq(r["options_custom"]["premium"], 612_927_966, "премия при франшизе пользователя, сум", 2)


def test_c_critical():
    print("\nПример C — критический риск, дерево, 9 баллов, износ 60%")
    r = run(*EX_C)
    check_shape(r)
    eq(r["level"], "Критический", "уровень риска")
    eq(sorted(r["grounds"]), ["dominant_peril", "level_high"], "основания")
    eq(r["franchise"]["pct"], 90.0, "франшиза, %", 1e-9)
    eq(r["decision_code"], "accept_or_decline", "код решения")
    # скидки нескольких мероприятий не складываются выше самой премии
    for o in r["options"]:
        assert o["premium"] > 0, f"вариант {o['key']}: премия {o['premium']} должна быть больше нуля"
    for a in r["alternatives"]:
        assert a["premium"] > 0, f"альтернатива {a['code']}: премия {a['premium']} должна быть больше нуля"
    eq(r["decision"], "принять только с франшизой либо отказать", "решение")
    p = r["franchise"]["peril"]
    assert p and p["code"] == M.want(NEW, "peril code", "earthquake") and p["pct"] == M.want(NEW, "peril pct", 90), p
    print(f"  ok  франшиза по риску: {p['name']} {p['pct']}% (доля риска {p['share_of_net_pct']}% нетто)")
    base = r["options"][0]["premium"]
    with_fr = next(o for o in r["options"] if o["key"] == "franchise")["premium"]
    eq(base, 4_875_891_429, "базовая премия, сум", 2)
    eq(with_fr, 598_418_155, "премия с франшизой 90% и франшизой по риску, сум", 2)
    if M.legacy():
        assert "исключить риск землетрясения" in " ".join(r["basis"])
    assert any(a["code"] == "sum_to_value" and "ст. 936" in (a.get("legal_ref") or "")
               for a in r["alternatives"]), r["alternatives"]


def test_d_dominant_peril_only():
    print("\nПример D — франшиза только по доминирующему риску «Пожар»")
    r = run(*EX_D)
    check_shape(r)
    eq(r["level"], "Умеренный", "уровень риска")
    eq(r["grounds"], ["dominant_peril"], "основания")
    eq(r["decision_code"], "accept_peril_franchise", "код решения")
    eq(r["franchise"]["type"], "безусловная по риску", "тип франшизы")
    eq(r["franchise"]["pct"], 0.0, "франшиза по договору, %", 1e-9)
    eq(r["franchise"]["peril_pct"], 2.0, "франшиза по риску, %", 1e-9)
    eq(r["franchise"]["peril"]["code"], "fire", "риск")
    base = r["options"][0]["premium"]
    with_fr = next(o for o in r["options"] if o["key"] == "franchise")["premium"]
    eq(base, 119_139_429, "базовая премия, сум", 2)
    eq(with_fr, round(base - base * 0.404 * 0.02), "премия с франшизой по риску, сум", 2)


def test_e_small_losses_only():
    print("\nПример E — единственное основание: поток мелких убытков")
    r = run(*EX_E)
    check_shape(r)
    eq(r["level"], "Умеренный", "уровень риска")
    eq(r["grounds"], ["small_losses"], "основания")
    eq(r["decision_code"], "accept_franchise", "код решения")
    eq(r["franchise"]["type"], "безусловная", "тип франшизы")
    eq(r["franchise"]["band_pct"], [1, 2], "вилка «Умеренного»")
    eq(r["franchise"]["pct"], 2.0, "франшиза, %", 1e-9)
    eq(r["franchise"]["amount"], 84_000_000, "франшиза в сумах")
    eq(r["losses"]["small_loss_threshold"], 42_000_000, "порог мелкого убытка, сум")
    eq(r["losses"]["small_share"], 1.0, "доля мелких убытков", 1e-9)
    base = r["options"][0]["premium"]
    with_fr = next(o for o in r["options"] if o["key"] == "franchise")["premium"]
    eq(base, 10_080_000, "базовая премия, сум", 2)
    eq(with_fr, 7_560_000, "премия с франшизой 2%, сум", 2)
    # остальные три основания — «нет», и каждое объяснено
    nos = [c for c in r["grounds_checked"] if not c["ok"]]
    eq(len(nos), 3, "оснований «нет»")
    assert all(c["why"] for c in nos), nos
    ground = next(c for c in r["grounds_checked"] if c["code"] == "small_losses")
    assert "франшиза отсекает именно такой поток" in ground["why"], ground


def test_f_statutory():
    print("\nПример F — обязательный вид (продукт 0820): франшиза не применяется")
    r, wi = run(*EX_F, what_if_pct=3.0)
    check_shape(r)
    eq(r["decision_code"], "statutory", "код решения")
    eq(r["needed"], False, "франшиза нужна?")
    eq(r["grounds"], [], "основания")
    eq(r["franchise"]["type"], "нет", "тип франшизы")
    eq(r["franchise"]["pct"], 0.0, "франшиза, %", 1e-9)
    eq(r["franchise"]["text"],
       "Франшиза не применяется: обязательный вид, тариф установлен нормативным актом", "текст")
    eq([o["key"] for o in r["options"]], ["base", "measures"], "варианты без франшизы")
    eq(r["options"][0]["premium"], 25_936_035, "базовая премия, сум", 2)
    eq(r["premium_effect"]["delta"], 0, "эффект на премию, сум")
    # одно общее объяснение, а не приписка к каждому из четырёх оснований
    basis = " ".join(r["basis"])
    assert "Основания для франшизы не разбираются" in basis, r["basis"]
    assert "продукт 0820" in basis, r["basis"]
    assert all("франшиза договором не вводится" not in c["why"] for c in r["grounds_checked"]), \
        r["grounds_checked"]
    assert all(c["ok"] is False and c.get("statutory") for c in r["grounds_checked"]), r["grounds_checked"]
    # what_if по обязательному виду не считает
    eq(wi["ok"], True, "what_if выполнен")
    eq(wi["applied"], False, "what_if: франшиза применена?")
    eq(wi["decision_code"], "statutory", "what_if: код решения")
    eq(wi["pct"], 0.0, "what_if: франшиза, %", 1e-9)
    eq(wi["requested_pct"], 3.0, "what_if: запрошено, %", 1e-9)
    eq(wi["premium"], 25_936_035, "what_if: премия без изменений, сум", 2)
    eq(wi["delta"], 0, "what_if: изменение премии, сум")
    assert "по обязательным видам франшиза не применяется" in wi["message"], wi
    assert "нормативным актом" in wi["text"], wi["text"]


def test_g_cap_warning():
    print("\nПример G — what_if выше потолка класса 3 (5%): считаем, но предупреждаем")
    with temp_db():
        with db.tx() as con:
            fix_company(con)
            ok5 = fr.what_if(con, *EX_G, 5.0, thresholds=NO_EXT, market=MARKET, as_of=AS_OF)
            eq(ok5["warning"], None, "в потолок укладывается — предупреждения нет")
            eq(ok5["cap_pct"], 5.0, "потолок класса 3, %", 1e-9)
            wi = fr.what_if(con, *EX_G, 10.0, thresholds=NO_EXT, market=MARKET, as_of=AS_OF)
            eq(wi["ok"], True, "расчёт выполнен")
            eq(wi["applied"], True, "франшиза применена")
            eq(wi["pct"], 10.0, "франшиза, %", 1e-9)
            eq(wi["base_premium"], 17_496_000, "базовая премия, сум", 2)
            eq(wi["premium"], 7_873_200, "премия при франшизе 10%, сум", 2)
            assert wi["warning"] and "выше потолка класса" in wi["warning"], wi["warning"]
            assert "Внимание" in wi["text"], wi["text"]
            print(f"  ok  предупреждение: {wi['warning']}")


def test_thresholds_in_db():
    print("\nПороги франшизы правятся админом (risk_thresholds)")
    with temp_db():
        with db.tx() as con:
            fix_company(con)
            ra.save_thresholds(con, {"franchise_by_level": {"Низкий": [0, 1], "Умеренный": [1, 2],
                                                            "Повышенный": [3, 3], "Высокий": [5, 10],
                                                            "Критический": [10, 90]},
                                     "weights": {"external_stats": 0}}, "тест")
            r = fr.recommend_franchise(con, *EX_B2, market=MARKET, as_of=AS_OF)
            eq(r["franchise"]["pct"], 3.0, "франшиза после правки порогов, %", 1e-9)
            eq(r["thresholds_source"]["what"], "правка администратора", "источник порогов")
            bad = ra.check_thresholds(ra._merge(ra.DEFAULT_THRESHOLDS,
                                                {"franchise_multipliers": {"0": 0.5, "1": 0.9},
                                                 "franchise_dominant_share": 5}))
            assert any("franchise_multipliers" in e for e in bad), bad
            assert any("franchise_dominant_share" in e for e in bad), bad
            print(f"  ok  неверные пороги отклоняются: {[e for e in bad if 'franchise' in e]}")
            # порог доминирования опускаем до 0,3 — «Пожар» 33,5% становится доминирующим
            ra.save_thresholds(con, {"franchise_dominant_share": 0.30,
                                     "weights": {"external_stats": 0}}, "тест")
            r2 = fr.recommend_franchise(con, dict(EX_B1[0], construction="wood"),
                                        dict(EX_B1[1], protection="none"),
                                        market=MARKET, as_of=AS_OF)
            assert ("dominant_peril" in r2["grounds"]) == M.want(NEW, "dominant after 0,30", True), r2["grounds_checked"]
            print(f"  ok  порог доминирования 0,30 → основания {r2['grounds']}")


def main():
    M.run_both([test_a_low_no_franchise, test_a_client_request, test_b1_no_grounds, test_b2_client_request,
                test_c_critical, test_d_dominant_peril_only, test_e_small_losses_only, test_f_statutory,
                test_g_cap_warning, test_thresholds_in_db], "франшиза:")
    print("\nВсе примеры сошлись.")


if __name__ == "__main__":
    main()
