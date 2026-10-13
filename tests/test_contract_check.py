"""
Договор в осмотре и сверка договора с актом — 12 проверок (ТЗ разработчику 13.10.2026, день 7.1 и 7.2).

Запуск из корня:
    set PYTHONIOENCODING=utf-8
    sandbox\\.venv\\Scripts\\python.exe tests\\test_contract_check.py

7.1 — три учебных договора на бланке INSON (tests/fixtures/contracts, делает tools/make_contract_fixtures.py):
DOCX, PDF с текстом и скан (PDF из картинки; ответ модели подменён — contract_scan.model.json идёт через
recognize.parse_model, как настоящий ответ). На каждом находится ≥ 8 из 10 ключевых полей (contract_read.KEY_FIELDS),
пустой бланк — is_template, персональные данные (представители, залогодатель-гражданин, паспорт, адрес) не
извлекаются, условия договора отдаются полям осмотра с пометкой «из договора».
7.2 — app/contract_check.py: на каждую из 12 проверок — нарушена / не нарушена; договор со ставкой ниже минимума —
«Отказать / согласовать с ЦО»; сборка акта во временной копии базы (tests/tmpdb.py): act_json.contract_check.checks,
прежние ключи сверки на месте, текст на трёх языках, пункт c_cc и решение «На рассмотрение». Сети нет.
"""
import copy
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["SURVEYOR_NO_BACKGROUND"] = "1"

from tmpdb import temp_db  # noqa: E402
from app import act_engine as ae, act_extras as ax, contract_check as cck, contract_read as cr, db, llm  # noqa: E402
from app import act_texts as tx  # noqa: E402
from app.act_pkg.documents import contract_view, ct_clean  # noqa: E402
from app.act_pkg.recognize import parse_model  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "contracts"
passed, failed = 0, 0


def ok(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ок  ", name)
    else:
        failed += 1
        print("  ПЛОХО", name, str(extra)[:400])


# поля, которые просил заказчик (ТЗ 7.1 «10 ключевых полей»), — для отчёта рядом с contract_read.KEY_FIELDS
ASKED = {
    "продукт": lambda f: bool(f.get("product_code") or f.get("product_name")),
    "страховая сумма": lambda f: f.get("sum_insured") is not None,
    "стоимость": lambda f: f.get("object_value") is not None,
    "ставка": lambda f: f.get("tariff_pct") is not None,
    "премия": lambda f: f.get("premium") is not None,
    "срок / даты": lambda f: bool(f.get("term_from") and f.get("term_to")),
    "стороны-юрлица": lambda f: (f.get("policyholder") or {}).get("kind") == "legal",
    "риски": lambda f: bool(f.get("covered_risks")),
    "франшиза": lambda f: bool(f.get("franchise")),
    "выгодоприобретатель": lambda f: (f.get("beneficiary") or {}).get("kind") is not None,
}
# ПД учебных договоров (выдуманные): ни одно не должно попасть в разбор
PD_STRINGS = ("Тестов", "1234567", "Учебная, дом", "кв. 7", "Примеров", "Образцов", "Учебного У")
REPORT = {}


def parse_file(con, name: str) -> dict:
    res = ax.parse_document(con, FIX / name, "8", ae.DEFAULT_SETTINGS["limits"])
    return res


def scan_contract() -> dict:
    raw = (FIX / "contract_scan.model.json").read_text(encoding="utf-8")
    m = parse_model(raw, 1)
    return m["contract"] if m else None


def check_fixtures():
    print("1. Учебные договоры INSON: ключевые поля, бланк, ПД (день 7.1)")
    for n in ("contract_fire.docx", "contract_pledge.pdf", "contract_scan.pdf", "contract_scan.model.json",
              "contract_blank.docx"):
        ok(f"файл {n} есть", (FIX / n).exists())
    with db.tx() as con:
        got = {}
        for name in ("contract_fire.docx", "contract_pledge.pdf"):
            res = parse_file(con, name)
            ok(f"{name}: текстовый слой, вид — договор", res["text_layer"] and res["kind"] == cr.KIND, res["kind"])
            got[name] = res.get("contract")
        scan = parse_file(con, "contract_scan.pdf")
        ok("скан: текстового слоя нет — читает модель", not scan["text_layer"] and not scan.get("contract"), scan)
        got["contract_scan.pdf"] = scan_contract()
        ok("скан: ответ модели (заглушка) разобран recognize.parse_model → contract_read.from_model",
           got["contract_scan.pdf"] is not None)
        blank = parse_file(con, "contract_blank.docx").get("contract") or {}
    for name, c in got.items():
        f, _n = ct_clean(c["fields"])
        found = [k for k in cr.KEY_FIELDS if cr.present(f, k)]
        asked = [k for k, fn in ASKED.items() if fn(f)]
        REPORT[name] = (len(found), found, len(asked), asked)
        ok(f"{name}: найдено ≥ 8 из 10 ключевых полей ({len(found)})", len(found) >= 8,
           [k for k in cr.KEY_FIELDS if k not in found])
        ok(f"{name}: из списка ТЗ ≥ 8 из 10 ({len(asked)})", len(asked) >= 8)
        ok(f"{name}: не бланк", not f.get("is_template"))
        dump = json.dumps(f, ensure_ascii=False)
        ok(f"{name}: ПД людей в разборе нет", not any(s in dump for s in PD_STRINGS),
           [s for s in PD_STRINGS if s in dump])
        v = contract_view({"fields": f, "found": found, "missing": []}, "ru")
        labels = {x["source_label"] for x in v["conditions"]}
        ok(f"{name}: условия в поля осмотра с пометкой «из договора»",
           v["conditions"] and labels == {"из договора"} and v["sources"].get("sum_insured") == "contract", labels)
    fp = ct_clean(got["contract_pledge.pdf"]["fields"])[0]
    ok("залогодатель-гражданин: признак individual, имени нет",
       fp["pledger"] == {"kind": "individual", "name": None}, fp["pledger"])
    ok("договор в PDF: номер перенесён на вторую строку — найден", fp["contract_no"] == "INS-0801/045",
       fp["contract_no"])
    ok("код продукта из названия вида страхования («код 0801»)", fp["product_code"] == "0801")
    from app.act_pkg.photos import prefill_view
    pv = prefill_view({"sum_insured": {"value": 1.8e9, "source": "document", "file": "f1", "doc": cr.KIND},
                       "region": {"value": "Ташкент", "source": "document", "file": "f2"}}, "uz")
    ok("подсказка шага 2 из файла договора — пометка «shartnomadan», из другого документа — без неё",
       pv["sum_insured"].get("source_label") == "shartnomadan" and "source_label" not in pv["region"], pv)
    ok("пустой бланк INSON: is_template", blank.get("is_template") is True, blank.get("missing"))
    v = contract_view(blank, "ru")
    ok("бланк: готового optional.contract нет, существенные условия не проверяются",
       v["request"] is None and v["essentials"] == [])
    # модель прислала гражданина-выгодоприобретателя с именем — имя не берётся
    raw = json.loads((FIX / "contract_scan.model.json").read_text(encoding="utf-8"))
    raw["contract"]["beneficiary"] = {"is_legal": False, "name": "Тестов Тест Тестович"}
    raw["contract"]["pledger"] = "Тестов Тест Тестович, паспорт AA 1234567"
    m = parse_model(json.dumps(raw, ensure_ascii=False), 1)
    f, _n = ct_clean(m["contract"]["fields"])
    dump = json.dumps(f, ensure_ascii=False)
    ok("скан: гражданин от модели — без имени и паспорта", "Тестов" not in dump and "1234567" not in dump
       and f["beneficiary"]["kind"] == "individual", dump[:300])
    req = cr.request_of(f, "photo")
    ok("optional.contract: выгодоприобретатель-гражданин без имени", req["beneficiary"] == {"kind": "individual",
                                                                                          "name": None})


# --------------------------------------------------------------------------- #
#  12 проверок: базовый договор без нарушений и нарушение каждой проверки
# --------------------------------------------------------------------------- #

PARTS = [{"part_no": 1, "class_code": "8", "min_rate_pct": 0.05,
          "perils": ["fire", "earthquake", "explosion", "storm", "lightning"]}]
CT = {"product_code": "0807", "sum_insured": 2_400_000_000, "object_value": 2_500_000_000, "tariff_pct": 0.06,
      "premium": 1_440_000, "term_from": "2026-11-01", "term_to": "2027-10-31", "term_days": 365,
      "franchise": {"applied": True, "pct": 0.5, "amount": None, "text": "0,5 %"},
      "covered_risks": ["fire", "lightning", "explosion"], "object_description": "здание цеха",
      "payments": [{"date": "2026-11-10", "amount": 1_440_000}], "beneficiary": None}
ACT = {"product_code": "0807", "product_name": "Имущество от огня и стихийных бедствий", "class_code": "8",
       "client_type": "yur", "sum_insured": 2_400_000_000, "object_value": 2_500_000_000, "rate_pct": 0.06,
       "min_pct": 0.05, "rate_type": "annual", "term_days": 365, "inspection_date": "2026-10-20",
       "franchise_pct": 0.5}


def by(res, code):
    return next(c for c in res["checks"] if c["code"] == code)


def run(ct=None, act=None, parts=None, template=False):
    return cck.run(CT if ct is None else ct, ACT if act is None else act, PARTS if parts is None else parts,
                   ae.DEFAULT_SETTINGS, template)


def mod(base, **kw):
    out = copy.deepcopy(base)
    out.update(kw)
    return out


def check_rules():
    print("2. Сверка договора: 12 проверок — не нарушена / нарушена (день 7.2)")
    base = run()
    ok("12 проверок, номера 1–12, коды по порядку", [c["no"] for c in base["checks"]] == list(range(1, 13))
       and tuple(c["code"] for c in base["checks"]) == cck.CODES)
    ok("договор без нарушений: все проверки ok, итог ok",
       all(c["verdict"] == "ok" for c in base["checks"]) and base["summary"]["verdict"] == "ok",
       [(c["code"], c["verdict"]) for c in base["checks"] if c["verdict"] != "ok"])
    ok("у результата версия и дата порогов", base["version"] == "1.0" and base["date"] == "2026-10-13")
    ok("у каждой проверки — code, verdict, values, text_code",
       all({"code", "verdict", "values", "text_code"} <= set(c) for c in base["checks"]))

    # 1. существенные условия (ГК ст. 929) → «На рассмотрение»
    r = run(mod(CT, premium=None, payments=[]))
    ok("1 нарушена: нет премии → «На рассмотрение»", by(r, "essentials")["verdict"] == "review"
       and by(r, "essentials")["values"]["missing"] == ["premium"])
    ok("1: итог «На рассмотрение»", r["summary"]["verdict"] == "review")
    # 2. бланк → сверки нет
    r = run(template=True)
    ok("2 нарушена: бланк → «сверки нет», остальные не проверяются",
       by(r, "template")["verdict"] == "no_check" and all(c["verdict"] == "skipped" for c in r["checks"]
                                                           if c["code"] != "template")
       and r["summary"]["verdict"] == "no_check")
    ok("2 не нарушена: заполненный договор", by(base, "template")["verdict"] == "ok")
    r = run(mod(CT, is_template=True))
    ok("2: признак is_template в самом договоре — тоже бланк", by(r, "template")["verdict"] == "no_check")
    # 3. продукт
    r = run(mod(CT, product_code="0808"))
    ok("3 нарушена: продукт 0808 ≠ 0807", by(r, "product")["verdict"] == "violated")
    ok("3: кода нет — нет данных", by(run(mod(CT, product_code=None)), "product")["verdict"] == "na")
    # 4. страховая сумма ±1 000
    ok("4 не нарушена: разница 999 сум", by(run(mod(CT, sum_insured=2_400_000_999)), "sum_insured")["verdict"] == "ok")
    r = run(mod(CT, sum_insured=2_400_001_001))
    ok("4 нарушена: разница 1 001 сум", by(r, "sum_insured")["verdict"] == "violated"
       and by(r, "sum_insured")["values"]["diff"] == 1001)
    # 5. S/V 0,9–1,1
    r = run(mod(CT, sum_insured=2_000_000_000), mod(ACT, sum_insured=2_000_000_000))
    ok("5 нарушена: S/V = 0,8 — недострахование (ГК ст. 936)", by(r, "sum_value")["verdict"] == "violated"
       and by(r, "sum_value")["values"]["side"] == "under" and by(r, "sum_value")["text_code"] == "cc_sum_value_under")
    r = run(mod(CT, sum_insured=2_800_000_000), mod(ACT, sum_insured=2_800_000_000))
    ok("5 нарушена: S/V = 1,12 — выше стоимости (ГК ст. 938)", by(r, "sum_value")["values"]["side"] == "over")
    ok("5 не нарушена: S/V = 0,96", by(base, "sum_value")["verdict"] == "ok"
       and by(base, "sum_value")["values"]["ratio"] == 0.96)
    # 6. минимальная ставка по каждой части → «Отказать / согласовать с ЦО»
    r = run(mod(CT, tariff_pct=0.04, premium=960_000, payments=[]))
    ok("6 нарушена: 0,04 % < 0,05 % → «Отказать / согласовать с ЦО»",
       by(r, "min_rate")["verdict"] == "decline_or_agree" and r["summary"]["verdict"] == "decline_or_agree")
    ok("6 не нарушена: ставка равна минимуму", by(run(mod(CT, tariff_pct=0.05, premium=1_200_000, payments=[])),
                                                  "min_rate")["verdict"] == "ok")
    two = PARTS + [{"part_no": 2, "class_code": "13", "min_rate_pct": 0.5, "perils": ["gl_bodily"]}]
    r = run(parts=two)
    ok("6: продукт из двух частей, в договоре одна ставка — «На рассмотрение» (ставка по каждому классу)",
       by(r, "min_rate")["verdict"] == "review" and by(r, "min_rate")["values"]["unchecked"] == ["13"])
    r = run(mod(CT, tariff_pct=0.04, premium=960_000, payments=[]), parts=[])
    ok("6: частей нет — минимум акта (min_rates)", by(r, "min_rate")["verdict"] == "decline_or_agree"
       and by(r, "min_rate")["values"]["min_source"] == "min_rates")
    # 7. ставка ≥ r − 10 %
    ok("7 не нарушена: 0,06 % при r = 0,0666 % (порог 0,05994 %)",
       by(run(act=mod(ACT, rate_pct=0.0666)), "act_rate")["verdict"] == "ok")
    r = run(act=mod(ACT, rate_pct=0.07))
    ok("7 нарушена: 0,06 % при r = 0,07 % (порог 0,063 %) → «На рассмотрение»",
       by(r, "act_rate")["verdict"] == "review" and by(r, "act_rate")["values"]["floor"] == 0.063)
    # 8. премия = S × ставка × срок (±1 %), платежи = премия
    ok("8 не нарушена: премия +0,9 %", by(run(mod(CT, premium=1_452_960, payments=[])), "premium")["verdict"] == "ok")
    r = run(mod(CT, premium=1_500_000, payments=[]))
    ok("8 нарушена: премия 1 500 000 при расчёте 1 440 000", by(r, "premium")["verdict"] == "violated"
       and by(r, "premium")["values"]["issues"] == ["premium"])
    r = run(mod(CT, payments=[{"date": "2026-11-10", "amount": 700_000}]))
    ok("8 нарушена: платежи 700 000 ≠ премии", by(r, "premium")["values"]["issues"] == ["payments"])
    r = run(mod(CT, premium=4_320_000, payments=[]), mod(ACT, rate_type="fixed"))
    ok("8: ставка на весь срок (fixed) — премия = S × ставка", by(r, "premium")["values"]["calc"] == 1_440_000)
    # 9. даты срока и осмотр ≤ 30 дней до начала
    ok("9 не нарушена: осмотр за 12 дней", by(base, "term")["verdict"] == "ok"
       and by(base, "term")["values"]["before_days"] == 12)
    r = run(act=mod(ACT, inspection_date="2026-09-20"))
    ok("9 нарушена: осмотр за 42 дня", by(r, "term")["values"]["issues"] == ["too_early"])
    r = run(act=mod(ACT, inspection_date="2026-11-05"))
    ok("9 нарушена: осмотр после начала срока", by(r, "term")["values"]["issues"] == ["after_start"])
    r = run(mod(CT, term_from=None, term_to=None))
    ok("9 нарушена: нет дат срока", by(r, "term")["values"]["issues"] == ["no_dates"])
    r = run(mod(CT, term_to="2026-10-01"))
    ok("9 нарушена: конец раньше начала", "order" in by(r, "term")["values"]["issues"])
    # 10. риски ⊆ perils продукта
    r = run(mod(CT, covered_risks=["fire", "theft"]))
    ok("10 нарушена: кража вне рисков продукта класса 8", by(r, "risks")["verdict"] == "violated"
       and by(r, "risks")["values"]["outside"] == ["theft"])
    ok("10 не нарушена: пожар, молния, взрыв", by(base, "risks")["verdict"] == "ok")
    r = run(mod(CT, covered_risks=[{"code": "other", "text": "поломка холодильника"}]))
    ok("10: незнакомый риск — андеррайтеру", by(r, "risks")["verdict"] == "review")
    # 11. франшиза ≥ рекомендованной (справочно)
    r = run(mod(CT, franchise={"applied": True, "pct": 0.2, "amount": None}))
    ok("11 нарушена: 0,2 % < 0,5 % — справочно, в итог не идёт",
       by(r, "franchise")["verdict"] == "violated" and by(r, "franchise")["reference"]
       and r["summary"]["verdict"] == "ok" and r["summary"]["reference"] == ["franchise"])
    r = run(mod(CT, franchise={"applied": True, "pct": None, "amount": 12_000_000}))
    ok("11 не нарушена: 12 млн от 2,4 млрд = 0,5 %", by(r, "franchise")["verdict"] == "ok")
    # 12. банк-выгодоприобретатель → продукт «в залоге» (справочно)
    bank = {"kind": "legal", "name": "АКБ «Учебный банк»"}
    r = run(mod(CT, beneficiary=bank))
    ok("12 нарушена: банк-выгодоприобретатель, продукт не «в залоге» — справочно",
       by(r, "beneficiary")["verdict"] == "violated" and by(r, "beneficiary")["reference"]
       and r["summary"]["verdict"] == "ok")
    r = run(mod(CT, beneficiary=bank), mod(ACT, product_name="Имущество в залоге"))
    ok("12 не нарушена: продукт «Имущество в залоге»", by(r, "beneficiary")["verdict"] == "ok")
    ok("12: выгодоприобретателя нет — проверка не нужна", by(base, "beneficiary")["text_code"] == "cc_beneficiary_none")
    # нет договора
    ok("нет договора и не бланк — available = false", cck.run(None, ACT, PARTS, ae.DEFAULT_SETTINGS)["available"]
       is False)


def check_texts_settings():
    print("3. Тексты RU/UZ/EN и настройки act_settings.contract_check")
    keys = [k for k in tx.TX if k.startswith("cc_")] + ["c_cc", "from_contract"]
    ok("тексты cc_* на трёх языках", all(all(tx.TX[k].get(l) for l in ("ru", "uz", "en")) for k in keys))
    samples = [run(), run(mod(CT, tariff_pct=0.04, premium=1, payments=[{"date": "2026-11-10", "amount": 2}],
                               covered_risks=["theft"], term_from=None, product_code="0808",
                               franchise={"applied": True, "pct": 0.1}, beneficiary={"kind": "legal",
                                                                                      "name": "ATB Bank"})),
               run(template=True), run(parts=PARTS + [{"part_no": 2, "class_code": "13", "min_rate_pct": 0.5}])]
    bad = []
    for res in samples:
        for c in res["checks"]:
            for lang in ("ru", "uz", "en"):
                s = cck.text(c, lang)
                if not s or "{" in s or s.startswith("cc_"):
                    bad.append((c["code"], c["verdict"], lang, s))
    ok("каждая проверка даёт текст на ru/uz/en без пропусков", not bad, bad[:3])
    v = cck.view({"checks": run(mod(CT, tariff_pct=0.04, premium=960_000, payments=[]))["checks"],
                  "checks_summary": {"verdict": "decline_or_agree", "codes": ["min_rate"]}}, "uz")
    ok("вид для экрана (uz): подпись вердикта «Rad etish / Bosh ofis bilan kelishish»",
       v["summary"]["verdict_label"].startswith("Rad etish") and len(v["items"]) == 12)
    ok("пункт проверок андеррайтера c_cc (ru)", "Отказать / согласовать с ЦО" in cck.check_text(
        {"code": "c_cc", "params": {"verdict": "decline_or_agree", "codes": ["min_rate"]}}, "ru"))
    st = ae.DEFAULT_SETTINGS["contract_check"]
    ok("пороги по умолчанию — в act_settings с версией и датой", st["version"] and st["date"] == "2026-10-13"
       and st["sum_tolerance"] == 1000 and st["inspection_max_days"] == 30 and st["calibrated"] == 0)
    ok("настройки по умолчанию проверку проходят", not cck.check_settings(ae.merge_settings({})))
    errs = ae.check_settings({"contract_check": {"sv_min": 1.5, "risk_map": [], "lucky": 1}})
    ok("ошибочные пороги ловятся", any("sv_min" in e for e in errs) and any("risk_map" in e for e in errs)
       and any("lucky" in e for e in errs), errs)
    r = cck.run(CT, ACT, PARTS, {"contract_check": {"sum_tolerance": 5000}})
    ok("порог из настроек администратора применяется",
       by(cck.run(mod(CT, sum_insured=2_400_004_000), ACT, PARTS, {"contract_check": {"sum_tolerance": 5000}}),
          "sum_insured")["verdict"] == "ok" and r["summary"]["verdict"] == "ok")


def check_act():
    print("4. Сборка акта: act_json.contract_check.checks, прежние ключи, решение")
    from app.act_pkg.validate import validate
    from app.act_pkg.build import build_data
    from app.act_pkg.view import render
    must = {"product_code": "0801", "sum_insured": 1_800_000_000, "object_value": 2_000_000_000,
            "region": "Ташкент"}
    contract = {"product_code": "0801", "sum_insured": 1_800_000_000, "object_value": 2_000_000_000,
                "tariff_pct": 0.025, "premium": 450_000, "term_from": "2026-11-15", "term_to": "2027-11-14",
                "covered_risks": ["fire", "explosion"], "object_description": "складское здание",
                "beneficiary": {"kind": "legal", "name": "АКБ «Учебный банк»"}}
    with temp_db("surveyor-cc-test.db"):
        db.ensure_schema()
        with db.tx() as con:
            clean, errs = validate(con, {"lang": "ru", "must": must, "optional": {"contract": contract}})
            ok("ввод с договором проходит проверку", not errs, errs)
            D = build_data(con, clean, "test-owner", "ru")
            out = render(D, "ru", {"id": "x", "created_at": "2026-10-13T10:00:00+05:00", "expires_at": "2026-10-20T10:00:00+05:00", "number": "T-1"})
            clean2, _ = validate(con, {"lang": "en", "must": must,
                                       "optional": {"contract": dict(contract, tariff_pct=0.05, premium=900_000)}})
            D2 = build_data(con, clean2, "test-owner", "en")
            out2 = render(D2, "en", {"id": "y", "created_at": "2026-10-13T10:00:00+05:00", "expires_at": "2026-10-20T10:00:00+05:00", "number": "T-2"})
            D3 = build_data(con, validate(con, {"lang": "ru", "must": must, "optional": {}})[0], "test-owner", "ru")
    cc = D["contract_check"]
    ok("прежние ключи сверки на месте (available, items, summary, how)",
       cc.get("available") and {"items", "summary", "how"} <= set(cc))
    ok("act_json.data.contract_check.checks — 12 проверок {code, verdict, values, text}",
       len(cc["checks"]) == 12 and all({"code", "verdict", "values", "text"} <= set(c) for c in cc["checks"]))
    mr = next(c for c in cc["checks"] if c["code"] == "min_rate")
    ok("ставка 0,025 % ниже минимума 0,03 % (продукт 0801) → «Отказать / согласовать с ЦО»",
       mr["verdict"] == "decline_or_agree" and mr["values"]["min"] == 0.03, mr)
    ok("итог сверки — decline_or_agree; версия порогов в акте",
       cc["checks_summary"]["verdict"] == "decline_or_agree" and cc["checks_settings"]["version"] == "1.0")
    ok("проверки андеррайтера: пункт c_cc; решение «На рассмотрение»",
       any(c["code"] == "c_cc" for c in D["decision"]["checks"]) and D["decision"]["code"] == "d_review",
       D["decision"]["code"])
    bn = next(c for c in cc["checks"] if c["code"] == "beneficiary")
    ok("банк-выгодоприобретатель и продукт «Имущество в залоге» — 12 не нарушена", bn["verdict"] == "ok", bn)
    js = out["contract_check"]
    ok("ответ /act/make: contract_check.checks с текстом на языке акта",
       len(js["checks"]) == 12 and js["checks_summary"]["verdict_label"] == "Отказать / согласовать с ЦО"
       and all(c["text"] for c in js["checks"]))
    ok("ответ /act/make: пункт c_cc в проверках андеррайтера текстом",
       any("Сверка с договором — Отказать / согласовать с ЦО" in str(x) for x in out["decision"]["checks"])
       and out["decision"]["code"] == "review", out["decision"])
    mr2 = next(c for c in D2["contract_check"]["checks"] if c["code"] == "min_rate")
    ok("та же сверка, ставка 0,05 % ≥ минимума — 6 не нарушена", mr2["verdict"] == "ok", mr2)
    ok("английский текст проверок", any("rate" in c["text"] for c in out2["contract_check"]["checks"]))
    ok("акт без договора: проверок нет, прежний ключ сверки на месте",
       D3["contract_check"].get("available") is False and D3["contract_check"]["checks"] == [])


def main():
    orig = (llm.chat_raw, llm._post)

    def no_network(*a, **k):
        raise RuntimeError("сеть в тесте запрещена")
    llm.chat_raw = no_network
    llm._post = no_network
    try:
        check_fixtures()
        check_rules()
        check_texts_settings()
        check_act()
    finally:
        llm.chat_raw, llm._post = orig
    print("\nКлючевые поля на учебных договорах (KEY_FIELDS contract_read / список ТЗ):")
    for name, (n, found, k, asked) in REPORT.items():
        print(f"   {name}: {n}/10 {found}; по списку ТЗ {k}/10")
    print(f"\nитог: ок {passed}, плохо {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
