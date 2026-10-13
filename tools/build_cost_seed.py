"""
Учебная строка справочника build_cost_m2 (ТЗ 13.10.2026, день 6.2): город Ташкент / магазин, вымышленная цена 1 м²
для проверки затратного метода. Правку администратора не перетирает. Запуск из корня:
    set PYTHONIOENCODING=utf-8
    sandbox\.venv\Scripts\python.exe tools\build_cost_seed.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, valuation  # noqa: E402

if __name__ == "__main__":
    with db.tx() as con:
        print(valuation.build_cost_seed(con))
