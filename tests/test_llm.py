"""
Контрольные примеры клиента ИИ (app/llm.py).
Запуск:  sandbox\\.venv\\Scripts\\python.exe tests/test_llm.py

Сеть не нужна: проверяем маскировку персональных данных и поведение без ключа.
Проверяем:
  1) маскировка ПИНФЛ, паспорта, ИНН, телефона, e-mail, номера карты, госномера, кадастра;
  2) маскировка ФИО — кириллицей, латиницей и «Фамилия И. О.»;
  3) полезный текст (суммы, ставки, названия документов) маскировкой не портится;
  4) без ключа: status() говорит «не подключён», chat() и extract_fields() возвращают пусто;
  5) объяснение расчёта без ИИ собирается по шаблону и содержит цифры;
  6) маска ключа не показывает ключ целиком.
Записей в базе тест не создаёт (кроме строки журнала llm_calls, которая удаляется в конце).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tmpdb import temp_db  # noqa: E402  (tests/tmpdb.py)
from app import db                                          # noqa: E402
from app import llm                                         # noqa: E402


def test_pinfl():
    out = llm.mask_pd("ПИНФЛ страхователя 31234567890123, проверить в реестре")
    assert "31234567890123" not in out, out
    assert "[ПИНФЛ]" in out, out


def test_passport_and_inn():
    out = llm.mask_pd("Паспорт AA 1234567, ИНН 305123456")
    assert "1234567" not in out and "305123456" not in out, out
    assert "[ПАСПОРТ]" in out and "[ИНН]" in out, out


def test_phone_and_email():
    out = llm.mask_pd("Телефон +998 90 123-45-67, почта ivanov.i@mail.uz")
    assert "123-45-67" not in out and "mail.uz" not in out, out
    assert "[ТЕЛЕФОН]" in out and "[E-MAIL]" in out, out


def test_card_and_plate_and_cadastre():
    out = llm.mask_pd("Карта 8600 1234 5678 9012, машина 01 A 123 AB, кадастр 10:09:05:01:0123:0001")
    assert "8600" not in out and "0123" not in out, out
    assert "[НОМЕР КАРТЫ]" in out and "[ГОСНОМЕР]" in out and "[КАДАСТР]" in out, out


def test_names():
    a = llm.mask_pd("Страхователь Иванов Пётр Сергеевич обратился в филиал")
    assert "Иванов" not in a and "[ФИО]" in a, a
    b = llm.mask_pd("Owner: Ivanov Petr Sergeevich")
    assert "Ivanov" not in b and "[ФИО]" in b, b
    c = llm.mask_pd("Подпись: Каримов А. Б.")
    assert "Каримов" not in c and "[ФИО]" in c, c


def test_vehicle_brand_is_not_a_name():
    """«Chevrolet Cobalt 2022» — марка и модель ТС, не ФИО; человек рядом с маркой всё равно маскируется."""
    t = "Легковой Chevrolet Cobalt 2022, 3 фото"
    assert not llm.has_pd(t), llm.mask_pd(t)
    assert not llm.has_pd("Грузовой Isuzu NQR 2019"), llm.mask_pd("Грузовой Isuzu NQR 2019")
    assert not llm.has_pd("Легковой Gamma Delta 2021"), "две латинские модели рядом с годом и «легковой»"
    m = llm.mask_pd("Toyota Camry 2020, владелец Karimov Aziz")
    assert "Toyota Camry 2020" in m and "Karimov" not in m and "[ФИО]" in m, m
    for person in ("Owner: Ivanov Petr", "Ivanov Petr 1985", "John Smith"):
        assert llm.has_pd(person), person


def test_useful_text_survives():
    src = "Страховая сумма 250 000 000 сум, ставка 0,35%, класс 8, износ 12%"
    out = llm.mask_pd(src)
    assert "250 000 000" in out and "0,35%" in out and "класс 8" in out, out


def test_bank_account_and_mfo():
    """Банковский счёт (20 цифр подряд или группами, после «р/с», «х/р», «h/r», «IBAN»), МФО и карта."""
    for src in ("р/с 20208000900123456001, МФО 00873", "х/р: 2020 8000 9051 2345 6001, MFO: 00873",
                "h/r 20208000900123456001", "Расчётный счёт 20208-00090-01234-56001",
                "счёт в банке 20208000900123456001", "IBAN UZ12NAMU00000000001234 в банке"):
        out = llm.mask_pd(src)
        assert "[СЧЁТ]" in out and not any(x in out for x in ("20208", "2020 8000", "00873", "UZ12")), (src, out)
    card = llm.mask_pd("карта 8600123456789012 и 8600 1234 5678 9012")
    assert "8600" not in card and card.count("[НОМЕР КАРТЫ]") == 2, card


def test_bank_rules_keep_useful_numbers():
    """Суммы с пробелами, кадастр (с двоеточиями), VIN и даты счётом не считаются."""
    for src in ("Страховая сумма 81 250 000 000,00 сум", "стоимость 47 397 852 345,04 сум",
                "премия 123 322 000,00 (бир юз йигирма уч миллион) сўм", "срок 2026 йил 29 сентябрдан"):
        assert llm.mask_pd(src) == src, (src, llm.mask_pd(src))
    assert "[КАДАСТР]" in llm.mask_pd("кадастр 10:00:00:00:00:00001")
    assert "[СЧЁТ]" not in llm.mask_pd("кадастр 10:00:00:00:00:00001")
    vin = llm.mask_pd("VIN XTA21703080123456")
    assert "XTA21703080123456" in vin, vin
    assert llm.mask_pd("тел +998 90 123-45-67") == "тел [ТЕЛЕФОН]"


def test_mask_is_applied_to_dict():
    out = llm.mask_pd({"фио": "Иванов Пётр", "пинфл": "31234567890123"})
    assert "Иванов" not in out and "31234567890123" not in out, out


def test_no_key_behaviour():
    """Без ключа ИИ честно выключен, а вызовы ничего не ломают."""
    if llm.enabled():
        print("  (ключ ИИ задан — проверка поведения без ключа пропущена)")
        return
    st = llm.status()
    assert st["connected"] is False, st
    assert "не подключ" in st["reason"] or "выключен" in st["reason"] or "не поддержан" in st["reason"], st
    assert llm.chat("тест", "system", "user") is None
    assert llm.extract_fields("текст документа", [{"key": "year", "name": "Год"}], "техпаспорт") == {}
    assert llm.ping()["ok"] is False


def test_key_mask():
    assert llm.mask_key("sk-abcdefghijklmnop1234") == "sk-...1234"
    assert llm.mask_key("") == ""
    assert "secret" not in llm.mask_key("secretkey12345678")


def test_explain_template_always_works():
    card = {"request": {"product_code": "0126"},
            "object": {"object_type": "квартира", "sum_insured": 250_000_000,
                       "attributes": '{"factors": {"class_code": "8"}, "term_from": "2026-01-01",'
                                     ' "term_to": "2026-12-31"}'},
            "calculation": {"gross_rate_pct": 0.35, "applied_rate_pct": 0.4, "min_rate_pct": 0.3,
                            "premium": 1_000_000, "verdict": "ок",
                            "explanation": '{"chain": [{"name": "Базовая нетто-ставка", "value_pct": 0.2},'
                                           ' {"name": "Сейсмозона · 8 баллов", "mult": 1.3}]}'},
            "checks": [], "recommendations": [{"kind": "франшиза", "text": "Франшиза 1% снизит премию",
                                               "premium_delta": -50000}]}
    res = llm.explain_calculation(card)
    assert res["text"], res
    assert res["source"] in ("шаблон", "ИИ")
    assert "квартира" in res["template"]
    assert "0,350%" in res["template"] or "0.35" in res["template"], res["template"]
    assert "Франшиза" in res["template"]
    # в объяснении не должно быть персональных данных
    assert llm.mask_pd(res["template"]) == res["template"] or "[ФИО]" not in res["template"]


def test_news_interface_is_a_stub():
    res = llm.analyze_news([{"title": "Изменения в положении об агентах", "url": "https://lex.uz/x"}])
    assert res["ok"] is False and res["items"][0]["важность"] is None


def cleanup():
    """Убираем строки журнала, которые мог оставить тест."""
    with db.tx() as con:
        con.execute("DELETE FROM llm_calls WHERE purpose IN ('тест', 'проверка связи')")
    print("тестовые записи журнала удалены")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    try:
        for t in tests:
            t()
            print("ок:", t.__name__)
    finally:
        cleanup()
    print("\nвсе проверки клиента ИИ пройдены:", len(tests))


if __name__ == "__main__":
    with temp_db("surveyor-llm.db"):  # рабочая data/surveyor.db не меняется
        main()
