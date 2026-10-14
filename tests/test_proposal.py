"""
Проверка модуля «Предложение клиенту». Запуск из корня проекта:
  set PYTHONIOENCODING=utf-8 && sandbox\.venv\Scripts\python.exe tests\test_proposal.py
Создаёт запрос, строит PDF, сохраняет в sandbox\proposal_test.pdf и читает текст обратно.
Запрос создаётся во ВРЕМЕННОЙ копии базы (tests/tmpdb.py) — рабочая data/surveyor.db не меняется.
"""
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tmpdb import temp_db                          # noqa: E402  (tests/tmpdb.py)
from app.main import RequestIn, create_request     # noqa: E402
from app import fonts                              # noqa: E402
from app.proposal import build_proposal            # noqa: E402

OUT = ROOT / "sandbox" / "proposal_test.pdf"


def test_proposal_pdf():
    """Склад 4,2 млрд, сумма 4 млрд (неполное страхование), часть документов не собрана."""
    with temp_db():
        _proposal_checks()


def _proposal_checks():
    body = RequestIn(product_code="0807", class_code="8", object_type="Склад",
                     value_amount=4.2e9, sum_insured=4.0e9, term_days=365,
                     factors={"construction": "wood", "activity": "warehouse", "protection": "none",
                              "seismic": "z9", "wear": "old", "loss_history": "clean", "franchise": "f0"},
                     docs_received=["Заявление-анкета на страхование", "Документ о праве на объект"],
                     branch="тест", policyholder="ООО «Пример»", region="Ташкент",
                     address="ул. Складская, 1", external_no="ТЕСТ-001")
    saved = create_request(body)
    rid = saved["request_id"]

    if fonts.find_pair() is None:
        # нет шрифта из app/fonts.py (ни Arial, ни DejaVu Sans): PDF не проверить, ответ — понятная 503
        from fastapi import HTTPException
        try:
            build_proposal(rid)
        except HTTPException as e:
            assert e.status_code == 503 and fonts.NO_FONT in e.detail, (e.status_code, e.detail)
            print("ПРОПУСК: нет шрифта для PDF — предложение отвечает 503 «На сервере нет шрифта»")
            return
        raise AssertionError("без шрифта предложение должно отвечать 503")

    pdf = build_proposal(rid)
    assert pdf[:5] == b"%PDF-", "не PDF"
    OUT.write_bytes(pdf)

    doc = pymupdf.open(stream=pdf, filetype="pdf")
    # шрифт (Arial) может отдать пробел как неразрывный (общий глиф) — приводим к обычному
    text = "\n".join(p.get_text() for p in doc).replace(" ", " ")
    pages = doc.page_count
    doc.close()

    assert "Предложение по страхованию" in text, "нет заголовка"
    assert "Премия" in text or "премия" in text, "нет слова «премия»"
    assert any("Ѐ" <= ch <= "ӿ" for ch in text), "кириллица не извлеклась"
    assert "0807" in text, "нет кода продукта"
    assert "Из чего сложилась ставка" in text
    assert "Землетрясение" in text, "включённые риски не перечислены"
    assert "Ядерная энергия" in text, "исключённые риски не перечислены"
    assert "Предупредительные мероприятия" in text
    assert "условие договора" in text, "мероприятия не пересчитаны движком"
    assert "936" in text, "нет предупреждения о неполном страховании при сумме ниже стоимости"
    assert "955" in text and "15 дней" in text, "нет порядка урегулирования претензий"
    assert "Расчёт предварительный до предоставления документов" in text, "нет перечня недостающих документов"
    assert "ЕАИС" not in text
    print(f"запрос № {rid}: PDF {len(pdf)} байт, страниц {pages}, сохранён в {OUT}")


if __name__ == "__main__":
    test_proposal_pdf()
    print("Все проверки пройдены.")
