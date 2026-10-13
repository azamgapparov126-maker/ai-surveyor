"""
База знаний о рынке (tools/market_knowledge.py): заметки docs/Знания/Рынок/*.md и факты docs/market_facts.json.

Запуск из корня:
    set PYTHONIOENCODING=utf-8
    sandbox\\.venv\\Scripts\\python.exe tests\\test_market_knowledge.py

Всё собирается на копии базы во временной папке: заметки и факты пишутся туда же, рабочая база только читается
(контрольная сумма до и после). Ожидаемые числа берутся из копии базы после пересборки, а не из головы.

Что проверяется:
  1. пять контрольных фактов: цифра в заметке и в JSON равна цифре в базе (премии рынка на последний срез, выплаты
     ОСАГО за полный год, премии INSON на последний срез, премии класса 14, премии города Ташкента);
  2. рост год к году — только к той же дате год назад (I полугодие 2026 к I полугодию 2025, а не к 2025 году);
     пересчёт руками для всего рынка по всем срезам;
  3. в заметках нет пустых разделов без пометки, у каждой заметки в конце — «Источник: отчёт НАПП …»;
  4. чего нет в данных — сказано словами «нет в открытых данных» (разрез «страховщик × класс»).
"""
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db as appdb  # noqa: E402
import tools.market_knowledge as mk  # noqa: E402

FAILED = []


def check(cond, what):
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        FAILED.append(what)


def md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def one(con, sql, *args):
    r = con.execute(sql, args).fetchone()
    return r[0] if r else None


def fact(facts, **kw):
    return [f for f in facts if all(f.get(k) == v for k, v in kw.items())]


def sections(text):
    """[(заголовок, содержимое)] по строкам «## »."""
    out, cur, buf = [], None, []
    for line in text.splitlines():
        if line.startswith("## "):
            if cur is not None:
                out.append((cur, "\n".join(buf)))
            cur, buf = line[3:].strip(), []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        out.append((cur, "\n".join(buf)))
    return out


def main():
    tmp = Path(tempfile.mkdtemp(prefix="market-knowledge-test-"))
    src = appdb.DB_PATH
    before = md5(src)
    try:
        out, js, work = tmp / "md", tmp / "facts.json", tmp / "work.db"
        docs, facts, _ = mk.generate(src, out, js, rebuild=True, work_db=work)
        con = sqlite3.connect(str(work))
        L = one(con, "SELECT MAX(report_date) FROM market_stats")
        FY = one(con, "SELECT MAX(report_date) FROM market_stats WHERE substr(report_date,6)='01-01'")
        md = {n: (out / n).read_text(encoding="utf-8") for n in docs}
        payload = json.loads(js.read_text(encoding="utf-8"))

        print("1. Пять контрольных фактов: заметка = JSON = база")
        v = one(con, "SELECT premiums_ytd FROM market_stats WHERE report_date=? AND row_key='total'", L)
        check(f"**{mk.fmt_mln(v)} млн сум**" in md["01 Рынок в целом.md"],
              f"премии рынка {L} в заметке: {mk.fmt_mln(v)}")
        f = fact(facts, topic="рынок", metric="premiums", entity="весь рынок", source_date=L)
        check(len(f) == 1 and f[0]["value"] == round(v, 1), "премии рынка в JSON = базе")

        v = one(con, "SELECT payouts_ytd FROM market_stats WHERE report_date=? AND row_key='osago'", FY)
        fy_part = md["05 Обязательные виды.md"].split(f"## {mk.period_label(FY)}")[1].split("## ")[0]
        row = next((ln for ln in fy_part.splitlines() if ln.startswith("| ОСАГО")), "")
        check(f"| {mk.fmt_mln(v)} |" in row, f"выплаты ОСАГО за {mk.period_label(FY)} в заметке: {mk.fmt_mln(v)}")
        f = fact(facts, topic="обязательные", metric="premiums", source_date=FY)
        f = [x for x in f if x["entity"].startswith("ОСАГО")]
        prem = one(con, "SELECT premiums_ytd FROM market_stats WHERE report_date=? AND row_key='osago'", FY)
        check(len(f) == 1 and f[0]["value"] == round(prem, 1)
              and f[0]["loss_ratio_pct"] == round(v / prem * 100, 1), "ОСАГО в JSON: премии и убыточность = базе")

        v = one(con, "SELECT premiums_ytd FROM market_stats WHERE report_date=? AND row_key='company:INSON AJ'", L)
        check(f"| {mk.date_ru(L)} | {mk.period_label(L)} | {mk.fmt_mln(v)} |" in md["03 Страховые компании.md"],
              f"премии INSON {L} в карточке: {mk.fmt_mln(v)}")
        tot = one(con, "SELECT SUM(premiums_ytd) FROM market_stats WHERE report_date=? AND row_key LIKE 'company:%' "
                       "AND premiums_ytd>0", L)
        f = fact(facts, topic="INSON", metric="premiums", source_date=L)
        check(len(f) == 1 and f[0]["value"] == round(v, 1) and f[0]["share_pct"] == round(v / tot * 100, 1),
              "INSON в JSON: премии и доля = базе")

        v = one(con, "SELECT premiums_ytd FROM market_stats WHERE report_date=? AND row_key='cls14'", L)
        check(re.search(r"^\| 14 \| [^|]+ \| [^|]+ \| " + re.escape(mk.fmt_mln(v)) + r" \|",
                        md["02 Классы страхования.md"], re.M) is not None, f"премии класса 14 в заметке: {mk.fmt_mln(v)}")
        f = [x for x in fact(facts, topic="классы", metric="premiums", source_date=L) if x["entity"].startswith("класс 14 ")]
        check(len(f) == 1 and f[0]["value"] == round(v, 1) and f[0]["rank"] == 1, "класс 14 в JSON: премии и место 1")

        v = one(con, "SELECT premiums_ytd FROM market_stats WHERE report_date=? AND row_key='region:TOSHKENT SHAHRI'", L)
        check(f"| город Ташкент | {mk.fmt_mln(v)} |" in md["04 Регионы.md"], f"премии г. Ташкента в заметке: {mk.fmt_mln(v)}")
        f = fact(facts, topic="регионы", metric="premiums", entity="город Ташкент", source_date=L)
        check(len(f) == 1 and f[0]["value"] == round(v, 1), "г. Ташкент в JSON = базе")

        print("2. Рост год к году — только сопоставимые периоды")
        with_yoy = [x for x in facts if x["yoy_pct"] is not None]
        check(len(with_yoy) > 50, f"фактов с ростом г/г: {len(with_yoy)}")
        bad = [x for x in with_yoy if not x["yoy_base_date"] or x["yoy_base_date"][5:] != x["source_date"][5:]
               or int(x["source_date"][:4]) - int(x["yoy_base_date"][:4]) != 1]
        check(not bad, "у всех фактов база роста — та же дата год назад" + (f" (нарушений {len(bad)})" if bad else ""))
        dates = [r[0] for r in con.execute("SELECT DISTINCT report_date FROM market_stats ORDER BY 1")]
        ok = True
        for d in dates:
            b = f"{int(d[:4]) - 1}{d[4:]}"
            f = fact(facts, topic="рынок", metric="premiums", entity="весь рынок", source_date=d)[0]
            cur = one(con, "SELECT premiums_ytd FROM market_stats WHERE report_date=? AND row_key='total'", d)
            base = one(con, "SELECT premiums_ytd FROM market_stats WHERE report_date=? AND row_key='total'", b)
            want = None if base is None else round((cur / base - 1) * 100, 1)
            ok &= f["yoy_pct"] == want
        check(ok, "рост премий рынка пересчитан руками по всем срезам")
        f = fact(facts, topic="рынок", metric="premiums", entity="весь рынок", source_date=L)[0]
        check(f["yoy_base_date"] == f"{int(L[:4]) - 1}{L[4:]}" and f["yoy_base_date"] != FY,
              f"{mk.period_label(L)} сравнивается с {mk.period_label(f['yoy_base_date'])}, а не с {mk.period_label(FY)}")
        check(f"{mk.period_label(L)} к {mk.period_label(FY)}" not in md["01 Рынок в целом.md"]
              and f"{mk.period_label(L)} {mk.period_to(FY)}" not in md["01 Рынок в целом.md"],
              "в таблице роста нет пары «полугодие к полному году»")
        if "2024-03-31" in dates:
            f = fact(facts, topic="рынок", metric="premiums", entity="весь рынок", source_date="2024-03-31")[0]
            check(f["yoy_pct"] is None, "у самого раннего года роста нет (нет среза годом раньше)")

        print("3. Разделы не пустые, в конце — источник")
        for name, text in md.items():
            empty = [h for h, body in sections(text) if not re.search(r"\d|" + mk.NO_DATA, body)]
            check(not empty, f"{name}: пустых разделов нет" + (f" ({', '.join(empty)})" if empty else ""))
            last = [ln for ln in text.strip().splitlines() if ln.strip()][-1]
            check(last.startswith("Источник: отчёт НАПП «") and re.search(r"срез \d\d\.\d\d\.\d{4}", last) is not None,
                  f"{name}: последняя строка — источник")
        check(len(md) == 7, "заметок семь")
        check(payload.get("facts") and payload.get("slices") == dates, "в JSON — факты и список срезов")
        keys = {"topic", "period", "metric", "value", "unit", "entity", "rank", "share_pct", "loss_ratio_pct", "yoy_pct",
                "source_file", "source_date", "note"}
        check(all(keys <= set(x) for x in facts), "у каждого факта все поля схемы")
        napp = [x for x in facts if x["source_file"] and x["topic"] != "company_finance"]
        # отчёты НАПП (xlsx) в library/ есть только на рабочей машине; в репозитории те же файлы — в data/inbox
        lib = {p.name for p in mk.LIBRARY_NAPP.glob("*.xlsx")} | {p.name for p in (ROOT / "data" / "inbox").glob("*.xlsx")}
        check(all(x["source_file"] in lib for x in napp),
              "source_file каждого факта НАПП — файл отчёта (library/03_Рынок_НАПП или data/inbox)")

        print("5. Финансы страховщиков (рэнкинг snsratings)")
        rk = [x for x in facts if x["topic"] == "company_finance"]
        txt = {p.name for p in mk.LIBRARY_NAPP.rglob("*.txt")}
        check(len(rk) > 300 and all(x["source_file"] in txt for x in rk),
              f"факты рэнкинга: {len(rk)}, source_file — текст рэнкинга из library/03_Рынок_НАПП")
        v = one(con, "SELECT value_cur FROM company_rankings WHERE company='INSON AJ' AND indicator_code='total_capital'")
        f = fact(facts, topic="company_finance", metric="total_capital", entity="INSON AJ")
        check(len(f) == 1 and f[0]["value"] == round(v, 2) and "snsratings" in (f[0].get("source") or ""),
              f"капитал INSON в JSON = базе ({v}) и с источником")
        sec = dict(sections(md["03 Страховые компании.md"])).get("Финансы страховщиков (рэнкинг snsratings)", "")
        check("snsratings.uz" in sec and mk._grp(f"{v:,.1f}") in sec and "INSON в рэнкинге" in sec,
              "раздел «Финансы страховщиков (рэнкинг snsratings)» в заметке: источник и капитал INSON")

        print("4. Чего нет в данных — сказано прямо")
        spec = dict(sections(md["03 Страховые компании.md"])).get("Специализация компаний", "")
        check(mk.NO_DATA in spec and "страховщик × класс" in spec, "разреза «страховщик × класс» нет — сказано")
        check("вывод разработчика" in md["02 Классы страхования.md"], "выводы для тарифа помечены «вывод разработчика»")
        con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    check(md5(src) == before, "рабочая база не изменилась")
    print()
    if FAILED:
        print("Не пройдено: %d — %s" % (len(FAILED), "; ".join(FAILED)))
        sys.exit(1)
    print("Все проверки пройдены.")


if __name__ == "__main__":
    main()
