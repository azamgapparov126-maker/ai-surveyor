"""
Актуарный отчёт INSON за 2022 год → таблица actuarial_reports (ТЗ разработчику 13.10.2026, день 2.3).

Разбор текста library/02_Компания_INSON/«Актуарный отчёт INSON за 2022 год.txt»: стр. 8 — подписанная и
заработанная премия по 8 субпортфелям (ТС, Авиа, Имущество + СМР, Ответственность, Фин. риски, ОСГОВТС, ОСГОР,
Прочие); стр. 19–39 — выплаты, расходы на ведение дела, агентская комиссия, рисковая надбавка каждого субпортфеля.
Сверка: Σ заработанной премии = 73 566,68 (итог стр. 8), Σ выплат и Σ расходов — итоги стр. 15 (допуск 0,05 млн сум).
Не сошлось — ничего не записывается, причина в журнале audit. Повторный запуск обновляет те же строки.

Запуск из корня проекта:
    set PYTHONIOENCODING=utf-8
    sandbox\.venv\Scripts\python.exe tools\actuarial_load.py [--db путь] [--file путь к .txt] [--check]
--check — только разобрать и сверить, в базу не писать.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import actuarial, db  # noqa: E402


def _print(items: dict, totals: dict) -> None:
    print(f"  {'субпортфель':16} {'премия зараб.':>14} {'выплаты':>10} {'расходы':>10} {'L':>6} {'комис.%':>8} "
          f"{'надбавка':>9}")
    for k, r in items.items():
        rm = "—" if r.get("risk_margin") is None else f"{r['risk_margin']:.1f}"
        print(f"  {actuarial.SUBPORTFOLIOS[k][0]:16} {r['premium_earned']:14.2f} {r['payouts']:10.2f} "
              f"{r['expenses']:10.2f} {r['load_L']:6.3f} {r['commission_pct']:8.1f} {rm:>9}")
    s = sum(r["premium_earned"] for r in items.values())
    print(f"  итого заработанной премии: {s:.2f} (в отчёте {totals['premium_earned']:.2f}) млн сум")


def run(path=None, file=None, check=False) -> dict:
    if path:
        db.DB_PATH = Path(path)
    # текста отчёта нет (чистая машина) — разобранный снимок tests/fixtures/actuarial_2022.json, см. read_2022
    res = actuarial.read_2022(Path(file) if file else None)
    f = res["file"]
    if res["errors"]:
        print("Сверка не прошла:")
        for e in res["errors"]:
            print("  -", e)
        if not check:
            with db.tx() as con:
                db.audit(con, "tools/actuarial_load.py", "актуарный отчёт не загружен", "year:2022",
                         {"ошибки": res["errors"][:20]})
        raise SystemExit(1)
    _print(res["items"], res["totals"])
    if check:
        return res
    with db.tx() as con:
        out = actuarial.load_2022(con, "tools/actuarial_load.py", f)
    print(f"База: {db.DB_PATH} — 2022: добавлено {out['added']}, обновлено {out['updated']}")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--db")
    ap.add_argument("--file")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    run(a.db, a.file, a.check)
