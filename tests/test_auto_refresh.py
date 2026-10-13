"""
Автообновление данных: знания о рынке после нового среза НАПП и документы конкурентов.

Запуск из корня (сеть не нужна, живой сервер не трогаем):
    set PYTHONIOENCODING=utf-8
    sandbox\\.venv\\Scripts\\python.exe tests\\test_auto_refresh.py

Всё — на копии базы (tests/tmpdb.py) и во временной папке: STORAGE_DIR/knowledge и library_live подменяются
временными, docs/ и library/ проекта не меняются (контрольные суммы до и после).

Что проверяем:
  1. новый срез в market_stats → хук после обновления НАПП (_refresh_job) пересобирает заметки и факты
     в «STORAGE_DIR/knowledge» без data/parsed; повторный запуск ничего не пересобирает;
  2. специалист отвечает по новому срезу: дата ответа, факты из нового market_facts.json, заметки из хранилища
     в индексе (тип market); журнал audit; /health — блок «Источники данных»;
  3. реестр конкурентов из шапок (реальный docs/competitors_sources.json и синтетические шапки);
  4. «скачанный» документ из локального файла: PDF без изменений (sha256 совпал) — без новой версии;
     DOCX изменился — новая версия с шапкой, ПД вырезаны только по явным шаблонам, прежняя — в _history;
  5. страница: первый проход — baseline, второй — новая версия; новая ссылка на правила — источник new;
  6. robots.txt: запрет, недоступен, ошибка сети — пропуск с записью; пауза ≥ 2,5 с на домен;
  7. индекс специалиста видит library_live/Конкуренты (свежая версия вместо образа), _history не индексируется;
  8. сервер: competitors.refresh → state, статус, журнал; API статуса и закрытый для не-админа refresh.
"""
import asyncio
import hashlib
import io
import json as _json
import os
import shutil
import sys
import tempfile
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.pop("SURVEYOR_DEV", None)
os.environ["SURVEYOR_NO_BACKGROUND"] = "1"
os.environ["LEX_LIVE"] = "0"

from tmpdb import ensure_market_stats, temp_db   # noqa: E402
from app import auth, background, db, legal     # noqa: E402
from app import market_expert as mx             # noqa: E402
from app import market_knowledge as mkn         # noqa: E402
from app import competitors as comp             # noqa: E402
from app import main as appmain                 # noqa: E402
from app.main import app                        # noqa: E402
import tools.market_knowledge as mk_tool        # noqa: E402
import tools.competitors_fetch as cf            # noqa: E402

passed, failed = 0, 0
TOKENS = {}


def ok(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ок  ", name)
    else:
        failed += 1
        print("  ПЛОХО", name, str(extra)[:600])


def md5_tree(*paths) -> str:
    h = hashlib.md5()
    for base in paths:
        files = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file())
        for p in files:
            h.update(str(p).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def call(method, path, who=None):
    hdrs = [(b"host", b"test"), (b"content-type", b"application/json"), (b"content-length", b"0")]
    if who:
        hdrs.append((b"cookie", f"sid={TOKENS[who]}".encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "",
             "query_string": b"", "headers": hdrs, "client": ("203.0.113.29", 0), "server": ("test", 80)}
    out = {"status": None, "chunks": []}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
        elif msg["type"] == "http.response.body":
            out["chunks"].append(msg.get("body") or b"")

    asyncio.run(app(scope, receive, send))
    raw = b"".join(out["chunks"]).decode("utf-8", "replace")
    try:
        return out["status"], _json.loads(raw)
    except ValueError:
        return out["status"], raw


def user(login, role):
    with db.tx() as con:
        cur = con.execute("INSERT INTO users (login, full_name, role, branch, password_hash, salt, status, created_at)"
                          " VALUES (?,?,?,?,?,?,?,?)", (login, "Тест", role, "тест-авто", "x", "y", "активен", db.now()))
        u = db.rows(con, "SELECT * FROM users WHERE id=?", cur.lastrowid)[0]
        TOKENS[login], _ = auth.create_session(con, u, "203.0.113.29", "test")


# --------------------------------------------------------------------------- #
#  1–2. Знания о рынке
# --------------------------------------------------------------------------- #

NEW_DATE = "2026-10-01"


def add_slice():
    """Новый срез: строки последнего среза ×1,4 (9 месяцев против полугодия) под датой NEW_DATE."""
    with db.tx() as con:
        last = con.execute("SELECT MAX(report_date) FROM market_stats").fetchone()[0]
        if last >= NEW_DATE:
            con.execute("DELETE FROM market_stats WHERE report_date>=?", (NEW_DATE,))
            last = con.execute("SELECT MAX(report_date) FROM market_stats").fetchone()[0]
        con.execute("INSERT INTO market_stats (report_date, row_key, row_name, premiums_ytd, payouts_ytd,"
                    " liabilities, source_file, loaded_at) SELECT ?, row_key, row_name, premiums_ytd*1.4,"
                    " payouts_ytd*1.4, liabilities, 'тест_9_месяцев_2026.xlsx', ? FROM market_stats"
                    " WHERE report_date=?", (NEW_DATE, db.now(), last))
        return last


def check_knowledge(tmp: Path):
    print("1. новый срез НАПП → пересборка знаний о рынке в хранилище")
    knowledge = tmp / "knowledge"
    mkn.KNOWLEDGE = knowledge
    mk_tool.ms.PARSED = tmp / "нет_разборов"        # на сервере data/parsed может не быть
    ok("до среза отпечаток сохраняется; файлов в хранилище нет", not knowledge.exists())
    prev = add_slice()
    need, why = mkn.needs_rebuild()
    ok("после вставки среза нужна пересборка (%s)" % why, need)

    # хук после обновления НАПП: подменяем сетевую часть, фоновые потоки включаем на время проверки
    orig_refresh = appmain.market_stats.refresh
    appmain.market_stats.refresh = lambda: ["market_stats: тест"]
    os.environ.pop("SURVEYOR_NO_BACKGROUND", None)
    try:
        appmain._refresh_job()
        th = background._threads.get(mkn.THREAD)
        ok("_refresh_job запустил поток «market-knowledge»", th is not None)
        if th:
            th.join(300)
    finally:
        os.environ["SURVEYOR_NO_BACKGROUND"] = "1"
        appmain.market_stats.refresh = orig_refresh
    notes = sorted(p.name for p in (knowledge / "Рынок").glob("*.md")) if (knowledge / "Рынок").exists() else []
    ok("заметки записаны в STORAGE_DIR/knowledge/Рынок (%d)" % len(notes), len(notes) >= 7, notes)
    js = knowledge / "market_facts.json"
    data = _json.loads(js.read_text(encoding="utf-8")) if js.exists() else {}
    ok("market_facts.json в хранилище, срез %s в slices" % NEW_DATE, NEW_DATE in (data.get("slices") or []))
    ok("факты по новому срезу есть", any(f.get("source_date") == NEW_DATE for f in data.get("facts") or []))
    txt = (knowledge / "Рынок" / "01 Рынок в целом.md").read_text(encoding="utf-8") if notes else ""
    ok("в заметке «Рынок в целом» — 9 месяцев 2026", "9 месяцев 2026" in txt)
    ok("состояние сборки: отпечаток и время", bool(mkn.load_state().get("built_at")))
    ok("поток отметил успешную сборку", bool(background.state(mkn.THREAD).get("last_ok")),
       background.state(mkn.THREAD))
    st = mkn.status()
    ok("статус: последний срез %s" % st["slice"], st["slice"] == NEW_DATE and st["where"] == "STORAGE_DIR/knowledge")
    need2, why2 = mkn.needs_rebuild()
    ok("повторно — не нужно (%s)" % why2, not need2)
    ok("maybe_rebuild без изменений ничего не делает", mkn.maybe_rebuild()["rebuilt"] is False)
    with db.tx() as con:
        a = con.execute("SELECT COUNT(*) FROM audit WHERE action='пересобраны знания о рынке'").fetchone()[0]
    ok("журнал: запись о пересборке (одна)", a == 1, a)

    print("2. специалист отвечает по новым данным")
    ok("факты читаются из хранилища", mx.facts_file() == js, mx.facts_file())
    ok("в фактах специалиста есть срез %s" % NEW_DATE, any(f.get("source_date") == NEW_DATE for f in mx.facts()))
    files = legal.market_note_files()
    ok("обзоры рынка: из хранилища, «Конкуренты…» — из docs",
       any(knowledge in p.parents for p in files) and any(legal.is_competitor_note(p) for p in files), files)
    ok("одно имя — один файл", len({p.name for p in files}) == len(files))
    kinds = {legal.source_kind(legal.rel_path(p)) for p in files if knowledge in p.parents}
    ok("тип заметок хранилища — market", kinds == {"market"}, kinds)
    legal.reindex(force=True)
    r = legal.ask("кто лидер рынка по премиям", "ru", who="u:auto-test")
    m = r.get("market") or {}
    ok("ответ о лидере — по срезу %s (было %s)" % (NEW_DATE, prev), m.get("date") == NEW_DATE, m.get("date"))
    r2 = legal.ask("какие премии рынка", "ru", who="u:auto-test")
    m2 = r2.get("market") or {}
    ok("премии рынка — срез %s и факты сводки" % NEW_DATE, m2.get("date") == NEW_DATE, m2)
    with db.tx() as con:
        hit = con.execute("SELECT COUNT(*) FROM legal_files WHERE path LIKE ?", ("%/knowledge/Рынок/%",)).fetchone()[0]
    ok("заметки хранилища в индексе специалиста (%d)" % hit, hit >= 7)
    st, h = call("GET", "/health")
    ds = h.get("data_sources") or {}
    ok("/health: блок «Источники данных» — 6 источников",
       st == 200 and set(ds) == {"napp", "stat_uz", "exchange", "laws", "market_knowledge", "competitors"}, ds)
    ok("/health: знания о рынке — дата сборки", bool(ds.get("market_knowledge", {}).get("last"))
       and h["updated"].get("market_knowledge") == ds["market_knowledge"]["last"])


# --------------------------------------------------------------------------- #
#  3–7. Документы конкурентов
# --------------------------------------------------------------------------- #

def make_docx(paragraphs) -> bytes:
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t xml:space=\"preserve\">{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{w}"><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def head(company, doc, url, fmt, lang="русский", extra=""):
    return (f"Компания: {company}\n{extra}Документ: {doc}\nИсточник: {url}\nЗагружено: 02.10.2026\nЯзык: {lang}\n"
            f"Формат оригинала: {fmt}\nПометка: публичный документ страховщика с его официального сайта; не норма права."
            f" Персональные данные (телефоны, e-mail, ФИО) вырезаны: замен 0.\n-----\n")


class FakeWeb:
    """Сайт без сети: адрес → (код, тип, байты); robots — отдельно по домену."""

    def __init__(self):
        self.pages, self.robots, self.calls = {}, {}, []

    def get(self, url):
        self.calls.append(url)
        if url.endswith("/robots.txt"):
            host = url.split("/")[2]
            r = self.robots.get(host, (404, b""))
            if isinstance(r, Exception):
                raise r
            return r[0], "text/plain", r[1]
        if url not in self.pages:
            return 404, "", b""
        code, ctype, data = self.pages[url]
        return code, ctype, data


def check_registry(tmp: Path):
    print("3. реестр источников из шапок")
    reg = _json.loads(cf.REGISTRY.read_text(encoding="utf-8"))
    n_txt = 0
    for p in cf.LOCAL_DIR.rglob("*.txt"):
        if "_history" not in p.parts and "Источник: http" in p.read_text(encoding="utf-8")[:1500]:
            n_txt += 1
    ok("docs/competitors_sources.json: %d источников = txt с адресом (%d)" % (reg["count"], n_txt),
       reg["count"] == n_txt == len(reg["sources"]))
    ok("у каждого — компания, документ, URL, язык, тип pdf|docx|html, путь txt",
       all(s["company"] and s["document"] and s["url"].startswith("http") and s["type"] in ("pdf", "docx", "html")
           and s["txt"].endswith(".txt") and "lang" in s for s in reg["sources"]))
    ok("у pdf/docx с оригиналом — sha256", all(len(s.get("orig_sha256", "")) == 64
                                                for s in reg["sources"] if s["type"] != "html"))
    base = tmp / "img"
    (base / "Тест Сугурта").mkdir(parents=True)
    (base / "Тест Сугурта" / "Правила.txt").write_text(
        head("Тест Сугурта", "Правила", "https://test-sug.uz/files/rules.pdf", "PDF, страниц 2, без текстового слоя 0") + "Текст",
        encoding="utf-8")
    (base / "Тест Сугурта" / "Правила.pdf").write_bytes(b"%PDF-1.4 test")
    (base / "Тест Сугурта" / "Оферта.txt").write_text(
        head("Тест Сугурта", "Оферта", "https://test-sug.uz/files/offer.docx", "DOCX", extra="Индексировать: нет\n")
        + "Текст", encoding="utf-8")
    (base / "Тест Сугурта" / "Оферта.docx").write_bytes(make_docx(["Старая редакция правил"]))
    (base / "Тест Сугурта" / "Имущество — страница продукта.txt").write_text(
        head("Тест Сугурта", "Имущество — страница продукта", "https://test-sug.uz/ru/property", "HTML-страница")
        + "Старый текст", encoding="utf-8")
    (base / "Тест Сугурта" / "_history").mkdir()
    (base / "Тест Сугурта" / "_history" / "Правила (2026-01-01).txt").write_text(
        head("Тест Сугурта", "Правила", "https://test-sug.uz/old.pdf", "PDF"), encoding="utf-8")
    r = cf.build_registry(base)
    by = {s["document"]: s for s in r["sources"]}
    ok("синтетический реестр: 3 источника, _history не входит", r["count"] == 3, list(by))
    ok("типы из «Формат оригинала»", (by["Правила"]["type"], by["Оферта"]["type"],
                                      by["Имущество — страница продукта"]["type"]) == ("pdf", "docx", "html"))
    ok("sha256 оригинала рядом", by["Правила"].get("orig_sha256") == hashlib.sha256(b"%PDF-1.4 test").hexdigest())
    ok("«Индексировать: нет» → indexed false", by["Оферта"]["indexed"] is False and by["Правила"]["indexed"] is True)
    return base, r


def sample_pdf() -> bytes:
    """
    PDF конкурента для «скачивания». Оригиналы PDF в репозиторий не входят (.gitignore: library/**/*.pdf) — на чистой
    машине PDF собирается из текста конкурента (.txt в репозитории) через PyMuPDF (requirements.txt): тот же путь
    «PDF → текст», только без оригинала.
    """
    pdfs = sorted(cf.LOCAL_DIR.rglob("*.pdf"))
    if pdfs:
        return pdfs[0].read_bytes()
    import pymupdf as fitz
    txts = sorted(p for p in cf.LOCAL_DIR.rglob("*.txt") if "_history" not in p.parts)
    _, _, body = cf.split_txt(txts[0].read_text(encoding="utf-8")) if txts else ({}, "", "Правила страхования")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(36, 36, 559, 806), (body or "Правила страхования")[:1500], fontsize=9,
                        fontname="helv")
    out = doc.tobytes()
    doc.close()
    return out


def check_versions(tmp: Path, img: Path, reg: dict):
    print("4. «скачанный» документ из локального файла, история версий")
    live = tmp / "library_live" / "Конкуренты"      # как на сервере: STORAGE_DIR/library_live/Конкуренты
    web = FakeWeb()
    sleeps = []
    client = cf.Client(get=web.get, sleep=lambda s: sleeps.append(s))
    pdf_bytes = sample_pdf()
    by = {s["document"]: s for s in reg["sources"]}
    by["Правила"]["orig_sha256"] = hashlib.sha256(pdf_bytes).hexdigest()
    web.pages["https://test-sug.uz/files/rules.pdf"] = (200, "application/pdf", pdf_bytes)
    pd_line = "Директор Иванов И. И., тел. +998 90 123-45-67, почта ivanov.ii@gmail.com"
    keep_line = "Адрес: г. Ташкент, ул. Амира Темура, 10; колл-центр +998 71 200-00-00; info@test-sug.uz; И. О. руководителя"
    web.pages["https://test-sug.uz/files/offer.docx"] = (200, "application/vnd.openxmlformats", make_docx(
        ["Правила страхования имущества — новая редакция 2026", pd_line, keep_line, "Франшиза 1 % от суммы"]))
    web.pages["https://test-sug.uz/ru/property"] = (200, "text/html; charset=utf-8", (
        "<html><head><script>var x=1</script><title>t</title></head><body><h1>Страхование имущества</h1>"
        "<p>Тариф от 0,1 %</p></body></html>").encode())
    state = {}
    today = date(2026, 10, 3)
    img_before = md5_tree(img)
    res = cf.run(reg, state, live, img, client=client, today=today, log=lambda s: None)
    ok("проверено 3, robots нет (404 — ограничений нет)", res["checked"] == 3 and res["robots_closed"] == 0, res)
    ok("PDF не изменился (sha256 = как в реестре) — новой версии нет",
       state["sources"]["Тест Сугурта/Правила.txt"]["status"] == "unchanged"
       and not (live / "Тест Сугурта" / "Правила.txt").exists())
    new_txt = live / "Тест Сугурта" / "Оферта.txt"
    ok("DOCX изменился — новая версия в выходной папке", new_txt.exists() and res["changed"] == 1, res)
    t = new_txt.read_text(encoding="utf-8") if new_txt.exists() else ""
    f, _, body = cf.split_txt(t)
    ok("шапка как у существующих: Компания, Документ, Источник, Загружено, Язык, Формат, Пометка",
       f.get("Компания") == "Тест Сугурта" and f.get("Документ") == "Оферта"
       and f.get("Источник") == "https://test-sug.uz/files/offer.docx" and f.get("Загружено") == "03.10.2026"
       and f.get("Язык") == "русский" and f.get("Формат оригинала") == "DOCX" and "замен 3" in f.get("Пометка", ""), f)
    ok("ПД вырезаны: ФИО с инициалами, мобильный, личная почта",
       "Иванов" not in body and "123-45-67" not in body and "gmail" not in body and body.count("[") == 3, body)
    ok("адрес, городской телефон и почта компании не тронуты", keep_line in body, body)
    hist = live / "Тест Сугурта" / "_history" / "Оферта (2026-10-03).txt"
    ok("прежняя версия — в _history с датой", hist.exists() and "Старый текст" not in hist.read_text(encoding="utf-8")
       and hist.read_text(encoding="utf-8").startswith("Компания: Тест Сугурта"))
    ok("в шапке новой версии — ссылка на прежнюю", "_history/Оферта (2026-10-03).txt" in f.get("Версия", ""), f)
    ok("оригинал DOCX сохранён рядом", (live / "Тест Сугурта" / "Оферта.docx").exists())
    ok("файлы образа (library) не тронуты", md5_tree(img) == img_before)

    print("5. страница: baseline, затем изменение; новая ссылка на правила")
    page = state["sources"]["Тест Сугурта/Имущество — страница продукта.txt"]
    ok("страница без прежнего отпечатка — только baseline", page["status"] == "baseline" and res["baseline"] == 1
       and not (live / "Тест Сугурта" / "Имущество — страница продукта.txt").exists())
    res2 = cf.run(reg, state, live, img, client=client, today=today, log=lambda s: None)
    ok("повторный проход без изменений: 3 без изменений, новых версий нет",
       res2["unchanged"] == 3 and res2["changed"] == 0, res2)
    web.pages["https://test-sug.uz/ru/property"] = (200, "text/html; charset=utf-8", (
        "<html><body><h1>Страхование имущества</h1><p>Тариф от 0,15 %</p>"
        "<a href='/files/pravila-2026.pdf'>Правила страхования имущества (2026)</a>"
        "<a href='/files/report.pdf'>Годовой отчёт</a><a href='https://other.uz/rules.pdf'>Правила</a>"
        "</body></html>").encode())
    web.pages["https://test-sug.uz/files/pravila-2026.pdf"] = (200, "application/pdf", pdf_bytes)
    res3 = cf.run(reg, state, live, img, client=client, today=date(2026, 10, 10), log=lambda s: None)
    pg = live / "Тест Сугурта" / "Имущество — страница продукта.txt"
    ok("страница изменилась — новая версия, прежняя в _history",
       pg.exists() and "0,15" in pg.read_text(encoding="utf-8")
       and (live / "Тест Сугурта" / "_history" / "Имущество — страница продукта (2026-10-10).txt").exists(), res3)
    ok("найдена одна новая ссылка (правила того же сайта; отчёт и чужой сайт — нет)",
       res3["new_found"] == 1 and res3["new_list"][0]["url"] == "https://test-sug.uz/files/pravila-2026.pdf", res3)
    nd = live / "Тест Сугурта" / "Правила страхования имущества (2026).txt"
    ok("новый документ скачан и записан с пометкой «новый документ»", nd.exists() and res3["new_saved"] == 1
       and "новый документ" in nd.read_text(encoding="utf-8")[:1500])
    ok("найденный источник — в state с пометкой new", any(d["status"] == "new" for d in state["discovered"]))
    ok("пауза между обращениями к домену ≥ 2,5 с", sleeps and min(sleeps) > 2.0 and max(sleeps) <= 2.5, sleeps[:5])
    return live, state


def check_robots(tmp: Path):
    print("6. robots.txt")
    web = FakeWeb()
    web.robots["closed.uz"] = (200, b"User-agent: *\nDisallow: /\n")
    web.robots["down.uz"] = (503, b"")
    web.robots["neterr.uz"] = OSError("нет сети")
    web.robots["part.uz"] = (200, b"User-agent: *\nDisallow: /private/\n")
    web.pages["https://part.uz/public/a"] = (200, "text/html", b"<p>A</p>")
    reg = {"sources": [
        {"company": "C", "document": "d1", "url": "https://closed.uz/rules.pdf", "type": "pdf", "txt": "C/d1.txt"},
        {"company": "C", "document": "d2", "url": "https://down.uz/rules.pdf", "type": "pdf", "txt": "C/d2.txt"},
        {"company": "C", "document": "d3", "url": "https://neterr.uz/x", "type": "html", "txt": "C/d3.txt"},
        {"company": "C", "document": "d4", "url": "https://part.uz/private/x", "type": "html", "txt": "C/d4.txt"},
        {"company": "C", "document": "d5", "url": "https://part.uz/public/a", "type": "html", "txt": "C/d5.txt"}]}
    state = {}
    out = tmp / "robots_out"
    res = cf.run(reg, state, out, None, client=cf.Client(get=web.get, sleep=lambda s: None), log=lambda s: None)
    ok("закрыто robots: 4 из 5 (запрет, 503, ошибка сети, закрытый раздел)", res["robots_closed"] == 4, res)
    reasons = {r["txt"]: r["reason"] for r in res["robots"]}
    ok("причины записаны", "закрыт" in reasons.get("C/d1.txt", "") and "недоступен" in reasons.get("C/d2.txt", "")
       and "недоступен" in reasons.get("C/d3.txt", "") and "закрыт" in reasons.get("C/d4.txt", ""), reasons)
    ok("к закрытым адресам не обращались", not any(u in web.calls for u in (
        "https://closed.uz/rules.pdf", "https://down.uz/rules.pdf", "https://part.uz/private/x")), web.calls)
    ok("открытый адрес проверен", "https://part.uz/public/a" in web.calls and res["checked"] == 1)
    ok("ничего не записано", not out.exists())
    ok("в state — статус robots", state["sources"]["C/d1.txt"]["status"] == "robots")


def check_index(tmp: Path, img: Path, live: Path):
    print("7. индекс специалиста видит library_live/Конкуренты")
    orig = (legal.LIVE_LIB, legal.competitors_dir)
    try:
        legal.LIVE_LIB = live.parent
        # образ: подменяем папку конкурентов временной (через ROOT нельзя — там и законы), только функцию
        legal.competitors_dir = lambda: img
        files = legal.competitor_files()
        rels = {p.relative_to(live if live in p.parents else img).as_posix(): p for p in files}
        ok("«Оферта» берётся из library_live (свежая версия)", live in rels["Тест Сугурта/Оферта.txt"].parents, rels)
        ok("«Правила» — из образа (новой версии нет)", img in rels["Тест Сугурта/Правила.txt"].parents)
        ok("новый документ — в списке", "Тест Сугурта/Правила страхования имущества (2026).txt" in rels)
        ok("_history не индексируется", not any("_history" in p.parts for p in files))
        acts = [s for s in legal.source_files() if s["kind"] == "act" and live in s["path"].parents]
        ok("документы конкурентов на диске не попадают в акты", not acts, acts)
        kinds = {legal.source_kind(legal.rel_path(p)) for p in files if live in p.parents}
        ok("тип источника — competitor", kinds == {"competitor"}, kinds)
        ok("компания документа — по папке", legal._competitor_of({"path": legal.rel_path(rels["Тест Сугурта/Оферта.txt"])})
           == "Тест Сугурта")
    finally:
        legal.LIVE_LIB, legal.competitors_dir = orig


def check_server(tmp: Path):
    print("8. сервер: проход, статус, журнал, API")
    web = FakeWeb()
    web.pages["https://srv-test.uz/a.docx"] = (200, "application/octet-stream", make_docx(["Правила, редакция 2"]))
    reg_file = tmp / "srv_registry.json"
    cf.save_json(reg_file, {"sources": [
        {"company": "Срв", "document": "a", "url": "https://srv-test.uz/a.docx", "type": "docx", "txt": "Срв/a.txt",
         "orig_sha256": "0" * 64},
        {"company": "Срв", "document": "b", "url": "https://srv-test.uz/b.pdf", "type": "pdf", "txt": "Срв/b.txt"}]})
    orig = (cf.REGISTRY, comp.CLIENT, legal.LIVE_LIB)
    cf.REGISTRY = reg_file
    comp.CLIENT = cf.Client(get=web.get, sleep=lambda s: None)
    legal.LIVE_LIB = tmp / "srv_live"
    try:
        ok("проход нужен (раньше не было)", comp.due())
        res = comp.refresh(who="тест")
        ok("итог: проверено 2, изменилось 1, ошибок 1 (404)", res["checked"] == 2 and res["changed"] == 1
           and res["errors"] == 1, {k: v for k, v in res.items() if not isinstance(v, list)})
        ok("новая версия — в STORAGE_DIR/library_live/Конкуренты", (tmp / "srv_live" / "Конкуренты" / "Срв" / "a.txt").exists())
        ok("state рядом с базой", comp.state_file().exists() and comp.state_file().parent == Path(db.DB_PATH).parent)
        ok("следующий проход не раньше чем через 7 дней", not comp.due())
        st = comp.status()
        ok("статус: дата, проверено, изменилось, robots, ошибки, следующий запуск",
           st["last_run"] and st["checked"] == 2 and st["changed"] == 1 and st["robots_closed"] == 0
           and st["errors"] == 1 and st["next_run"] and st["sources"] == 2, st)
        with db.tx() as con:
            acts = [r[0] for r in con.execute("SELECT action FROM audit WHERE who='тест'")]
        ok("журнал: изменение и сводка", "документ конкурента изменился" in acts
           and "проверка документов конкурентов" in acts, acts)
        user("тест-авто-админ", "админ")
        user("тест-авто-сотр", "сотрудник")
        s1, j1 = call("GET", "/market/competitors/status", who="тест-авто-сотр")
        ok("GET /market/competitors/status — 200, итог прохода", s1 == 200 and j1.get("changed") == 1, (s1, j1))
        s2, _ = call("POST", "/market/competitors/refresh", who="тест-авто-сотр")
        s3, _ = call("POST", "/market/knowledge/rebuild", who="тест-авто-сотр")
        ok("refresh и rebuild не-админу закрыты (403)", s2 == 403 and s3 == 403, (s2, s3))
        s4, _ = call("POST", "/market/competitors/refresh")
        ok("без входа — 401", s4 == 401, s4)
        s5, j5 = call("GET", "/market/knowledge/status", who="тест-авто-сотр")
        ok("GET /market/knowledge/status — срез %s" % NEW_DATE, s5 == 200 and j5.get("slice") == NEW_DATE, j5)
        s6, j6 = call("POST", "/market/knowledge/rebuild", who="тест-авто-админ")
        ok("админ: ручная пересборка (без фоновых потоков — сразу)", s6 == 200 and j6.get("facts"), (s6, str(j6)[:200]))
        s7, h = call("GET", "/health")
        c = (h.get("data_sources") or {}).get("competitors") or {}
        ok("/health: конкуренты — дата, проверено, следующий запуск", c.get("last") and c.get("checked") == 2
           and c.get("next"), c)
    finally:
        cf.REGISTRY, comp.CLIENT, legal.LIVE_LIB = orig


def main():
    tmp = Path(tempfile.mkdtemp(prefix="auto-refresh-test-"))
    guard_paths = [ROOT / "docs" / "market_facts.json", ROOT / "docs" / "Знания" / "Рынок", cf.REGISTRY, cf.LOCAL_DIR]
    before = md5_tree(*guard_paths)
    saved = (mkn.KNOWLEDGE, mk_tool.ms.PARSED, legal.LIVE_LIB)
    try:
        with temp_db():
            db.ensure_schema()
            ensure_market_stats()         # база старше db_build 13.10.2026 — рынок НАПП из data/parsed
            check_knowledge(tmp)
            img, reg = check_registry(tmp)
            live, _ = check_versions(tmp, img, reg)
            check_robots(tmp)
            check_index(tmp, img, live)
            check_server(tmp)
    finally:
        mkn.KNOWLEDGE, mk_tool.ms.PARSED, legal.LIVE_LIB = saved
        mx._facts_cache["mtime"] = None
        legal._index_ready["stamp"] = None
        shutil.rmtree(tmp, ignore_errors=True)
    ok("docs/ и library/ проекта не изменились", md5_tree(*guard_paths) == before)
    print(f"\nИтого: {passed} ок, {failed} плохо")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
