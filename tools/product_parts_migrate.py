"""
Части продуктов из тарифной политики (ТЗ разработчику 13.10.2026, день 2.1): 176 продуктов → product_parts.

По строке на класс продукта (product_classes): у первого класса — минимальные ставки версии политики (min_rates;
«физ 1,2% · юр 1,5%» — две строки с client_type fiz и yur), у остальных — ставка части из текста тарифа
(act_engine.part_rates_from_text; одна общая ставка «0,1%» — всем частям). Базовая ставка = минимальной (так акт
считал и раньше: base_source = product_rate). Строки ссылаются на версию «Тарифная политика INSON» (source = policy).
Идемпотентно: продукт, у которого строки политики уже есть, пропускается. Ничего не удаляет.

Запуск из корня проекта (база — та же, что у сервера: STORAGE_DIR или data/):
    set PYTHONIOENCODING=utf-8
    sandbox\.venv\Scripts\python.exe tools\product_parts_migrate.py [--db путь] [--quiet]
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import db, product_parts  # noqa: E402


def run(path=None, quiet=False) -> dict:
    if path:
        db.DB_PATH = Path(path)
    with db.tx() as con:
        product_parts.ensure(con)
        res = product_parts.migrate_from_policy(con)
        n_prod = con.execute("SELECT COUNT(DISTINCT product_code) FROM product_parts").fetchone()[0]
        n_rows = con.execute("SELECT COUNT(*) FROM product_parts").fetchone()[0]
        n_all = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    if not quiet:
        print(f"База: {db.DB_PATH}")
        print(f"  версия политики: {res['policy_version']} (действует с {res['valid_from']})")
        print(f"  продуктов разобрано сейчас: {res['products']}, пропущено (уже есть): {res['skipped']}")
        print(f"  частей добавлено: {res['parts']}, строк (с типом клиента): {res['rows']}")
        print(f"  всего в product_parts: {n_rows} строк по {n_prod} продуктам из {n_all}")
        if res["disputed"]:
            print(f"  спорные разборы текста тарифа: {len(res['disputed'])}")
            for d in res["disputed"]:
                print(f"    {d['product_code']} «{d.get('rate_text')}» кл. {','.join(d.get('classes') or [])}: "
                      f"{d['why']}")
    return dict(res, total_rows=n_rows, total_products=n_prod, products_all=n_all)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--db", help="путь к базе (по умолчанию — база сервера)")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    run(a.db, a.quiet)
