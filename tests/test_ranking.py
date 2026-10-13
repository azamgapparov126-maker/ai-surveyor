"""
Рэнкинг страховщиков snsratings.uz (II кв. 2026): tools/ranking_parse.py → company_rankings, app/rankings.py,
INSON в company_financials, лимит удержания в акте, ответы ИИ специалиста (app/market_expert.py).

Запуск из корня (сеть и модель не нужны, живой сервер не трогаем):
    set PYTHONIOENCODING=utf-8
    sandbox\\.venv\\Scripts\\python.exe tests\\test_ranking.py

Всё, что пишет в базу, — во временной копии (tests/tmpdb.py); рабочая база не открывается на запись.
Ожидаемые числа — из текста рэнкинга (страницы документа), а не из кода разбора.

Что проверяем:
  1. разбор образца страниц: обычная строка, «-» на месте значения (данных нет), отдельный минус после суммы,
     относительная страница (4 числа), строка ВСЕГО;
  2. весь документ: 79 показателей, 36 компаний (7 — жизнь), сумма компаний = ВСЕГО (±1 %) по всем суммам и
     количествам, одинаковый ранг — только у равных значений, пропуски записаны;
  3. загрузка в копию базы: число строк, повторная загрузка ничего не дублирует (замена по ключу),
     изменённый файл — needs_load;
  4. строки INSON: активы, капитал, резервы, отказы, рентабельность — как на страницах документа;
  5. company_financials: собственные средства и резервы из рэнкинга с пометкой «до подтверждения бухгалтерией»;
     ручной ввод не перезаписывается; лимит 20 % (Положение 1806 п. 15) в capacity — от новых цифр;
  6. акт: удержание считается от новых собственных средств;
  7. ИИ специалист: «активы APEX», «капитал INSON», «претензии и отказы у INSON», «рентабельность INSON»,
     «сколько договоров у INSON», «расскажи про компанию INSON», «у кого больше всего капитала»,
     правовой вопрос про отказ не уходит в рэнкинг; источник «Рэнкинг snsratings.uz».
"""
import asyncio
import json as _json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.pop("SURVEYOR_DEV", None)
os.environ["SURVEYOR_NO_BACKGROUND"] = "1"
os.environ["LEX_LIVE"] = "0"

from tmpdb import ensure_market_stats, temp_db  # noqa: E402
from app import capacity, db, llm, rankings   # noqa: E402
from app import market_expert as mx           # noqa: E402
import tools.ranking_parse as rp              # noqa: E402

passed, failed = 0, 0
INSON = "INSON AJ"


def ok(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ок  ", name)
    else:
        failed += 1
        print("  ПЛОХО", name, str(extra)[:600])


# --------------------------------------------------------------------------- образец страниц
def sample_text() -> str:
    """Обложка и три страницы из настоящего текста: 9 (активы), 20 (отрицательные суммы), 81 (относительная)."""
    full = rp.source_file().read_text(encoding="utf-8")
    pg = rp.PAGE_RE.split(full)
    keep = {1, 5, 6, 9, 20, 81}
    out = []
    for i in range(1, len(pg), 2):
        if int(pg[i]) in keep:
            out.append(f"=== стр {pg[i]} ===\n" + pg[i + 1])
    return "".join(out)


def check_sample():
    print("1. разбор образца страниц")
    tmp = Path(tempfile.mkdtemp(prefix="ranking-sample-")) / "sample.txt"
    tmp.write_text(sample_text(), encoding="utf-8")
    rows, rep = rp.parse(tmp)
    by = {(r["indicator_code"], r["company"]): r for r in rows}
    ok("период с обложки — 2026-Q2", rep["period"] == "2026-Q2", rep["period"])
    ok("три показателя образца", sorted({r["indicator_code"] for r in rows}) == ["retained_earnings", "roa",
                                                                                 "total_assets"], rep["indicators"])
    a = by.get(("total_assets", INSON))
    ok("стр. 9, INSON: 207 277,6 / 1,61 % / 18 → 272 856,7 / 1,61 % / 18, +31,64 %, влияние 0,51 п.п.",
       a and (a["value_prev"], a["share_prev"], a["rank_prev"], a["value_cur"], a["share_cur"], a["rank_cur"],
              a["change_pct"], a["growth_impact_pp"]) == (207277.6, 1.61, 18, 272856.7, 1.61, 18, 31.64, 0.51), a)
    ok("изменение доли п.п. = доля 2026 − доля 2025 (не «влияние на рост рынка»)",
       a and a["change_pp"] == 0.0 and a["growth_impact_pp"] == 0.51, a)
    t = by.get(("total_assets", "ВСЕГО"))
    ok("ВСЕГО: 12 847 076,1 → 16 983 880,3, +32,20 %",
       t and (t["value_prev"], t["value_cur"], t["change_pct"]) == (12847076.1, 16983880.3, 32.2), t)
    q = by.get(("total_assets", "O'ZBEKISTON QAYTA SUG'URTA KOMPANIYASI MQST AJ"))
    ok("«-» на месте значения — данных нет (None), а не ноль", q and q["value_prev"] is None
       and q["value_cur"] == 131626.3 and q["rank_prev"] == 35, q)
    pr = by.get(("retained_earnings", "PRESTIGE INSURANCE AJ"))
    ok("стр. 20: «35 634,9», «-», «-6,16%» — отрицательная сумма −35 634,9 и доля −6,16 %",
       pr and pr["value_cur"] == -35634.9 and pr["share_cur"] == -6.16 and pr["rank_cur"] == 36, pr)
    roa = by.get(("roa", INSON))
    ok("стр. 81 (относительная): INSON 0,29 % (29) → 0,54 % (26), изменение +0,25 п.п.",
       roa and (roa["value_prev"], roa["rank_prev"], roa["value_cur"], roa["rank_cur"], roa["share_cur"])
       == (0.29, 29, 0.54, 26, None) and roa["change_pp"] == 0.25 and roa["unit"] == "%", roa)
    ok("в образце сумма компаний = ВСЕГО", not rep["sum"] and rep["sum_checked"] == 4, rep["sum"])
    ok("страница и файл записаны в строке", a and a["page"] == 9 and a["source_file"] == "sample.txt", a)


# --------------------------------------------------------------------------- весь документ
def check_full():
    print("2. весь документ: показатели, компании, сходимость")
    rows, rep = rp.parse()
    ok("79 показателей из 79, ничего не пропущено", rep["indicators"] == 79 and not rep["not_parsed"]
       and not rep["issues"], (rep["indicators"], rep["not_parsed"], rep["issues"][:3]))
    ok("36 компаний, из них жизнь — 7", rep["companies"] == 36 and len(rep["life"]) == 7, rep["life"])
    ok("сумма компаний = ВСЕГО (±1 %) — 92 сверки (46 показателей × 2 периода), расхождений нет",
       rep["sum_checked"] == 92 and not rep["sum"], rep["sum"][:3])
    ok("одинаковый ранг — только у равных значений", not rep["rank"], rep["rank"][:3])
    ok("компаний на странице обычно 36; две страницы без GLOBAL INSURANCE GROUP — записано",
       rep["companies_usual"] == 36 and set(rep["companies_odd"]) == {"cost_to_premiums", "refused_to_claims"}
       and all(v == ["GLOBAL INSURANCE GROUP AJ"] for v in rep["companies_missing"].values()), rep["companies_odd"])
    names = {r["company"] for r in rows}
    with db.tx() as con:
        ms = {r[0][len("company:"):] for r in con.execute(
            "SELECT DISTINCT row_key FROM market_stats WHERE row_key LIKE 'company:%' AND report_date='2026-07-01'")}
    general = {r["company"] for r in rows if not r["is_life"] and r["company"] != "ВСЕГО"}
    ok("имена страховщиков общего страхования — как ключи market_stats (company:<NAME>)",
       general <= ms, sorted(general - ms))
    ok("INSON AJ и ВСЕГО есть", INSON in names and "ВСЕГО" in names)
    rel = {r["code"]: r for r in rep["relative"]}
    ok("рентабельность активов рынка = чистая прибыль / активы (2,98 %)",
       rel["roa"]["doc"] == 2.98 and rel["roa"]["diff"] <= 0.01, rel["roa"])
    ok("стр. 73: подпись по пересчёту — резервы чистые / совокупный капитал",
       next(r for r in rows if r["indicator_code"] == "reserves_to_capital")["indicator_name"]
       == "Страховые резервы, чистые / Совокупный капитал")
    return rows, rep


# --------------------------------------------------------------------------- база
def check_load(rows):
    print("3. загрузка в копию базы, идемпотентность")
    db.ensure_schema()
    with db.tx() as con:
        need0 = rp.needs_load(con)
    r1 = rankings.ensure_loaded()
    with db.tx() as con:
        n1 = con.execute("SELECT COUNT(*) FROM company_rankings").fetchone()[0]
        keys1 = con.execute("SELECT COUNT(DISTINCT report_period||indicator_code||company) FROM company_rankings"
                            ).fetchone()[0]
    ok("пустая таблица — загрузка при старте", need0 and r1["loaded"], r1)
    ok(f"строк в базе = строк разбора ({len(rows)})", n1 == len(rows) == keys1, (n1, len(rows), keys1))
    r2 = rankings.ensure_loaded()
    ok("второй старт — «уже загружен»", not r2["loaded"], r2)
    rep = rp.load()                       # явная повторная загрузка (python tools/ranking_parse.py --load)
    with db.tx() as con:
        n2 = con.execute("SELECT COUNT(*) FROM company_rankings").fetchone()[0]
    ok("повторная загрузка: строк столько же, дублей нет", n2 == n1 and rep["written"] == n1 and rep["removed"] == 0,
       (n2, rep["written"], rep["removed"]))
    changed = Path(tempfile.mkdtemp(prefix="ranking-changed-")) / "x.txt"
    changed.write_text(rp.source_file().read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with db.tx() as con:
        ok("файл изменился (другой sha256) — needs_load", rp.needs_load(con, changed) and not rp.needs_load(con))


def check_inson_rows():
    print("4. строки INSON")
    with db.tx() as con:
        d = rankings.company_rows(con, INSON)
    want = {"total_assets": (272856.7, 18), "total_capital": (95241.7, 20), "share_capital": (85751.5, 11),
            "reserves_gross": (152322.6, 19), "reserves_net": (150058.6, 13), "premiums_total": (97801.3, 17),
            "claims_paid": (28529.0, 15), "claims_received": (3516, 13), "claims_refused": (54, 14),
            "refused_to_claims": (1.54, 16), "roe": (1.56, 25), "payouts_to_premiums": (28.52, 16),
            "contracts_active": (451041, 9)}
    bad = {k: (d.get(k) or {}).get("value_cur") for k, (v, rk_) in want.items()
           if not d.get(k) or d[k]["value_cur"] != v or d[k]["rank_cur"] != rk_}
    ok("INSON: активы, капитал, резервы, премии, выплаты, претензии, отказы, ROE — как в документе", not bad, bad)
    ok("INSON — не страховщик жизни, страховые обязательства в млрд сум",
       all(r["is_life"] == 0 for r in d.values()) and d["insurance_liabilities"]["unit"] == "млрд сум")


def check_financials():
    print("5. company_financials и лимит 20 %")
    with db.tx() as con:
        fin = db.rows(con, "SELECT * FROM company_financials WHERE report_date='2026-07-01'")[0]
        cap = capacity.capacity(con)
    ok("собственные средства = совокупный капитал 95 241,7 млн сум; резервы = чистые 150 058,6 млн сум",
       fin["own_funds"] == 95_241_700_000 and fin["reserves"] == 150_058_600_000, fin)
    ok("разбивка: акционерный капитал, резервы брутто, активы",
       fin["share_capital"] == 85_751_500_000 and fin["reserves_gross"] == 152_322_600_000
       and fin["total_assets"] == 272_856_700_000 and fin["confirmed"] == 0, fin)
    ok("пометка «из публичного рэнкинга, до подтверждения бухгалтерией» и источник snsratings",
       "до подтверждения бухгалтерией" in fin["source"] and "snsratings.uz" in fin["source"], fin["source"])
    lim = 0.2 * (95_241_700_000 + 150_058_600_000)
    ok("capacity: лимит на один риск = 20 % × (собственные средства + резервы) — от новых цифр (1806 п. 15)",
       cap["own_funds"] == 95_241_700_000 and abs(cap["limit_per_risk"] - lim) < 1, cap["limit_per_risk"])
    ok("цифры по-прежнему помечены как временные (нет отчёта о платёжеспособности)",
       cap["temporary"] and "до подтверждения бухгалтерией" in cap["temporary"][0], cap["temporary"])
    # ручной ввод бухгалтерии рэнкинг не перезаписывает
    with db.tx() as con:
        con.execute("UPDATE company_financials SET own_funds=1e11, source='бухгалтерия, баланс 30.06' "
                    "WHERE report_date='2026-07-01'")
    rep = rp.load()
    with db.tx() as con:
        f2 = db.rows(con, "SELECT * FROM company_financials WHERE report_date='2026-07-01'")[0]
        con.execute("UPDATE company_financials SET own_funds=95241700000, source=? WHERE report_date='2026-07-01'",
                    (fin["source"],))
    ok("ручные цифры не перезаписаны, причина — в итоге загрузки",
       f2["own_funds"] == 1e11 and "не перезаписаны" in (rep.get("financials") or ""), rep.get("financials"))


# --------------------------------------------------------------------------- акт
COOKIES = {}


def call(method, path, body=None):
    payload = _json.dumps(body, ensure_ascii=False).encode() if body is not None else b""
    hdrs = [(b"host", b"test"), (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode())]
    if COOKIES:
        hdrs.append((b"cookie", "; ".join(f"{k}={v}" for k, v in COOKIES.items()).encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": path.encode(), "root_path": "", "query_string": b"", "headers": hdrs,
             "client": ("203.0.113.29", 0), "server": ("test", 80)}
    out = {"status": None, "chunks": [], "headers": []}

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"], out["headers"] = msg["status"], msg.get("headers") or []
        elif msg["type"] == "http.response.body":
            out["chunks"].append(msg.get("body") or b"")

    from app.main import app
    asyncio.run(app(scope, receive, send))
    for k, v in out["headers"]:
        if k.lower() == b"set-cookie":
            name, _, value = v.decode("latin-1").split(";")[0].partition("=")
            COOKIES[name.strip()] = value.strip()
    body = b"".join(out["chunks"])
    try:
        return out["status"], _json.loads(body)
    except ValueError:
        return out["status"], body


def check_act():
    print("6. акт: удержание от новых собственных средств")
    from app import act, guest
    act.DIR = Path(tempfile.mkdtemp(prefix="ranking-act-"))
    guest.reset()
    llm.enabled = lambda: False                  # модель не нужна
    must = {"product_code": "0808", "sum_insured": 1_000_000_000, "object_value": 1_200_000_000, "region": "Ташкент"}
    opt = {"object_kind": "warehouse", "protection": "alarm", "losses_3y": {"count": 0, "small_count": 0}}
    st, a = call("POST", "/act/make", {"lang": "ru", "must": must, "optional": opt})
    if st != 200:
        ok("акт собран", False, (st, str(a)[:300]))
        return
    sc = (a.get("scenarios") or {}).get("retention") or {}
    an = (a.get("analytics") or {}).get("retention") or {}
    lim = round(0.2 * (95_241_700_000 + 150_058_600_000))
    basis = (sc.get("basis") or "").replace(" ", " ").replace(" ", " ")
    ok("сценарии акта: основание лимита — собственные средства 95 241 700 000 и резервы 150 058 600 000 сум",
       "собственные средства 95 241 700 000 сум + резервы 150 058 600 000 сум" in basis, basis[:300])
    ok("сценарии акта: лимит на один риск 20 % — 49 060 060 000 сум", sc.get("limit_per_risk") == lim,
       sc.get("limit_per_risk"))
    ok("аналитика акта (удержание): те же собственные средства и лимит; статус — временно",
       an.get("own_funds") == 95_241_700_000 and an.get("limit_per_risk") == lim and an.get("status") == "temporary",
       {k: an.get(k) for k in ("own_funds", "limit_per_risk", "status")})
    lines = " ".join(an.get("lines") or []).replace(" ", " ").replace(" ", " ")
    ok("строка акта: «20 % × (собственные средства 95 241 700 000 …» и лимит 49 060 060 000",
       "95 241 700 000" in lines and "49 060 060 000" in lines, lines[:400])


# --------------------------------------------------------------------------- специалист
def ask(q, lang="ru", last=None):
    it = mx.detect(q, lang, last)
    if not it["is_market"]:
        return it, None
    return it, mx.answer(q, lang, mx.resolve(it, last))


def ind(r, code):
    return ((r or {}).get("numbers") or {}).get("indicators", {}).get(code) or {}


def snsrc(r):
    return any(s.get("domain") == "snsratings.uz" and s.get("url") and "snsratings" in (s.get("title") or "")
               for s in (r or {}).get("sources") or [])


def check_expert():
    print("7. ИИ специалист: вопросы о финансах компаний")
    mx._facts_cache["mtime"] = None
    it, r = ask("активы APEX")
    ok("«активы APEX» → активы APEX INSURANCE из рэнкинга, 1-е место, источник snsratings",
       r and ind(r, "total_assets").get("value") == 4239888.0 and ind(r, "total_assets").get("rank") == 1
       and snsrc(r) and "1-е место из 36" in r["text"], r and r["text"][:300])
    it, r = ask("капитал INSON")
    ok("«капитал INSON» → совокупный 95 241,7 (20-е) и акционерный 85 751,5 (11-е), изменение к году назад",
       r and ind(r, "total_capital").get("value") == 95241.7 and ind(r, "share_capital").get("rank") == 11
       and "95 241,7" in r["text"] and "+37,5 %" in r["text"] and "год назад" in r["text"], r and r["text"][:300])
    it, r = ask("претензии и отказы у INSON")
    ok("«претензии и отказы у INSON» → 3 516 поступило, 54 отказа, доля отказов 1,54 %",
       r and ind(r, "claims_received").get("value") == 3516 and ind(r, "claims_refused").get("value") == 54
       and ind(r, "refused_to_claims").get("value") == 1.54 and snsrc(r), r and r["text"][:300])
    ok("вывод специалиста сравнивает долю отказов с рынком и помечен как вывод",
       r and r.get("opinion") and "2,08 %" in r["opinion"], r and r.get("opinion"))
    it, r = ask("рентабельность INSON")
    ok("«рентабельность INSON» → ROE 1,56 %, ROA 0,54 %", r and ind(r, "roe").get("value") == 1.56
       and ind(r, "roa").get("value") == 0.54, r and r["text"][:300])
    it, r = ask("сколько договоров у INSON")
    ok("«сколько договоров у INSON» → действующих 451 041 (9-е место)",
       r and ind(r, "contracts_active").get("value") == 451041 and ind(r, "contracts_active").get("rank") == 9,
       r and r["text"][:300])
    it, r = ask("прибыль Gross")
    ok("«прибыль Gross» → чистая прибыль GROSS из рэнкинга", r and ind(r, "net_profit").get("value") == 14715.9,
       r and r["text"][:200])
    it, r = ask("расскажи про компанию INSON")
    need = ("premiums_total", "total_assets", "total_capital", "reserves_net", "claims_paid", "claims_received",
            "claims_refused", "roe")
    ok("карточка INSON: премии, активы, капитал, резервы, выплаты, претензии, отказы, рентабельность, место",
       r and it["card"] and all(ind(r, c) for c in need) and "18-е место из 36" in r["text"] and snsrc(r),
       r and list(((r.get("numbers") or {}).get("indicators") or {}).keys()))
    ok("карточка: таблица и пометка источника вместо «ytd НАПП»",
       r and r["table"] and len(r["table"]["rows"]) >= 10 and "snsratings" in r["ytd_note"], r and r.get("ytd_note"))
    it, r = ask("у кого больше всего капитала")
    ok("«у кого больше всего капитала» → APEX INSURANCE первым, INSON — 20-е место",
       r and r["numbers"].get("leader") == "APEX INSURANCE AJ" and r["numbers"].get("inson_rank") == 20,
       r and r.get("numbers"))
    it, r = ask("расскажи про APEX LIFE")
    ok("страховщик жизни (APEX LIFE) — карточка из рэнкинга с пометкой «жизнь»",
       r and (r["numbers"] or {}).get("is_life") and "страховщик жизни" in r["text"], r and r["text"][-200:])
    it, r = ask("INSON aktivlari qancha", "uz")
    ok("uz: «INSON aktivlari qancha» — ответ на узбекском, те же цифры",
       r and "272 856,7" in r["text"] and "oʻrin" in r["text"], r and r["text"][:200])
    it, r = ask("INSON total assets", "en")
    ok("en: «INSON total assets» — rank 18 of 36", r and "rank 18 of 36" in r["text"], r and r["text"][:200])
    it, r = ask("собственные средства INSON")
    ok("«собственные средства INSON» — company_financials (из рэнкинга) + лимит 20 % + разбивка",
       r and r["numbers"]["own_funds"] == 95_241_700_000 and "49 060 060 000" in r["text"]
       and "акционерный капитал" in r["text"], r and r["text"][:400])
    # правовые вопросы — не статистика рэнкинга
    for q in ("может ли страховщик отказать в выплате?", "INSON отказал в выплате, что делать?",
              "договор страхования INSON можно расторгнуть?"):
        it, r = ask(q)
        ok(f"«{q}» — не вопрос о финансах компании", not it["fin"] and not it["card"], it["fin"])
    # уточнение по контексту: тот же показатель у другой компании
    it, r = ask("активы INSON")
    it2, r2 = ask("а у APEX?", last=r["context"])
    ok("«а у APEX?» после «активы INSON» → активы APEX", r2 and ind(r2, "total_assets").get("value") == 4239888.0,
       r2 and r2["text"][:200])
    # premium-вопросы по-прежнему из НАПП
    it, r = ask("доля INSON")
    ok("«доля INSON» — по-прежнему из отчёта НАПП (market_stats), а не из рэнкинга",
       r and not r.get("numbers", {}).get("indicators") and r["numbers"].get("share_pct") is not None)


def main():
    check_sample()
    with temp_db("surveyor-ranking-test.db"):
        # чистая машина (git clone → tools/db_build.py): статистики НАПП в базе ещё нет — грузим её в копию из
        # data/parsed (как tools/market_stats.py в образе); имена страховщиков сверяются с ней
        ensure_market_stats()
        rows, _rep = check_full()
        check_load(rows)
        check_inson_rows()
        check_financials()
        check_act()
        check_expert()
    print(f"\nИтого: {passed} ок, {failed} плохо")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
