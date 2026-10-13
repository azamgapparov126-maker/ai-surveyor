"""
Слежение за законодательством: робот-юрист раз в сутки проверяет, не изменились ли нормы.

Зачем. Требование заказчика: система не должна держаться на старых данных. Статистика рынка
(НАПП) и открытые данные агентства статистики уже обновляются сами; теперь так же обновляются
нормы. Робот только ПРИНОСИТ событие — решение, что менять в движке, принимает человек.

Что делает один проход (см. docs/Слежение за законодательством.md):
  1. Для каждого акта из реестра watched_acts скачивает страницу lex.uz, снимает вёрстку,
     нормализует текст, считает sha256 и сравнивает с сохранённым. Расхождение → событие
     «новая редакция». Дополнительно вытаскивается первая дата из блока «Источники изменений»
     — если она изменилась, это уже не «возможное», а подтверждённое изменение редакции.
  2. Лента новых документов lex.uz (главная страница) → по ключевым словам «страхован»/«sug'urta»
     событие «новый документ».
  3. Новости НАПП → событие «новость».

Правила работы с сайтами (жёстко):
  * только urllib из стандартной библиотеки (через app/valuation_sources.py: там честный
    User-Agent, соблюдение robots.txt и пауза между запросами);
  * пауза не меньше PAUSE_SEC между обращениями, таймаут на каждый запрос;
  * никаких обходов защиты: 403, капча, robots.txt запрещает — пишем «источник недоступен»
    и идём дальше;
  * napp.uz отвечает 200 на ЛЮБОЙ адрес, поэтому проверяется содержимое, а не код ответа.

Источник нормы — всегда оригинал на lex.uz. Поле summary в событии — ИЗЛОЖЕНИЕ СИСТЕМЫ,
оно помечено плашкой и юридической силы не имеет.

Связка с движком — правило LAWWATCH-01: изменился акт → все правила из его rules_refs
получают review_status='требует пересмотра'. Расчёты не блокируются (иначе встанет продажа),
пометку снимает юрист: POST /law-events/{id}/seen или POST /rules/{code}/review-ok.
"""
import hashlib
import html as htmllib
import os
import json
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import background, db, web

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "docs" / "Отслеживаемые акты.json"
FEED_HTML = ROOT / "app" / "lawfeed.html"
router = APIRouter()

# --- ИИ: только через точку входа команды ИИ. Нет её — работаем без ИИ. -------
try:                                   # app/llm.py делает другая команда, мы его не трогаем
    from .llm import ask               # noqa: F401
except Exception:
    ask = None
try:
    from .llm import chat as _chat     # действующая точка входа в app/llm.py (возвращает None без ключа)
except Exception:
    _chat = None

# --- сеть: urllib с честным User-Agent, robots.txt и паузой -------------------
try:
    from .valuation_sources import _http_get as _get, robots_check
except Exception:                      # модуль недоступен — весь обход просто не пойдёт
    _get, robots_check = None, None

PAUSE_SEC = 3.0                        # пауза между обращениями (сверх паузы внутри _http_get)
TIMEOUT_ACT = 120                      # страница кодекса весит больше 2,5 МБ
TIMEOUT_FEED = 60

KIND_REDACTION = "новая редакция"
KIND_NEWDOC = "новый документ"
KIND_NEWS = "новость"
KINDS = (KIND_REDACTION, KIND_NEWDOC, KIND_NEWS)

ST_WATCH = "следим"
ST_CHANGED = "изменился"
ST_DOWN = "источник недоступен"
ST_NOURL = "адрес не найден"
ST_MANUAL = "следит юрист"

DISCLAIMER = "Это изложение системы, оригинал по ссылке."
SHELL_TEXT_LEN = 4000                  # ниже этого страница lex.uz — «оболочка» (одни реквизиты)

LEX_FEED = "https://lex.uz/ru/"
NAPP_NEWS = "https://napp.uz/ru/category/yangiliklar"

_state = {"last": None, "running": False, "log": [], "error": None}


# --------------------------------------------------------------------------- #
#  Реестр отслеживаемых актов
# --------------------------------------------------------------------------- #

def registry() -> dict:
    """Читает docs/Отслеживаемые акты.json. Нет файла — пустой реестр, ничего не выдумываем."""
    try:
        return json.loads(REGISTRY.read_text(encoding="utf-8"))
    except Exception as e:
        print("lawwatch: реестр актов не прочитан:", e)
        return {"acts": [], "sources": [], "keywords": {}}


# Основы слов: в реестре слова записаны в начальной форме («страхование», «страховой агент»),
# а в заголовках они стоят в падежах («страховых агентах»). Поиск по основе, как советует
# сам реестр (примечание к блоку keywords: «искать по основе sug»).
STEMS = ["страхов", "суғурт", "sug'urt", "sugurt", "перестрахов", "қайта суғурт"]


def keywords() -> list:
    """Ключевые слова из реестра плюс основы слов; нижний регистр, один вид апострофа."""
    kw = registry().get("keywords") or {}
    out = list(STEMS)
    for key in ("ru", "uz_cyr", "uz_lat", "doc_numbers"):
        out += [w for w in (kw.get(key) or []) if w]
    return sorted({_fold(w) for w in out})


def _fold(s: str) -> str:
    """Апостроф в латинице пишут тремя знаками — приводим к одному; регистр вниз."""
    return re.sub(r"[’‘`´]", "'", (s or "")).lower()


def seed_acts(con) -> int:
    """
    Идемпотентно заливает реестр в watched_acts: новые акты добавляет, у существующих
    обновляет справочную часть (название, адрес, редакцию, зависимые правила).
    Рабочие поля (text_hash, last_checked_at, last_changed_at) не трогает.
    """
    n = 0
    for a in registry().get("acts") or []:
        code = a.get("code")
        if not code:
            continue
        url = a.get("lex_url") or None
        refs = json.dumps(a.get("rules_refs") or [], ensure_ascii=False)
        row = con.execute("SELECT code, status, title, kind, lex_url, redaction, last_changed_at, priority, why,"
                          " rules_refs FROM watched_acts WHERE code=?", (code,)).fetchone()
        # у внутренних актов и у актов без установленного адреса робота нет — следит юрист
        status = ST_NOURL if not url else ST_WATCH
        if not url and (a.get("kind") == "внутренний акт"):
            status = ST_MANUAL
        if row:
            keep = row["status"] if row["status"] in (ST_CHANGED, ST_DOWN) and url else status
            red = a.get("redaction") if row["last_changed_at"] is None else row["redaction"]
            if (row["title"], row["kind"], row["lex_url"], row["redaction"], row["priority"], row["why"],
                    row["rules_refs"], row["status"]) == (a.get("title") or code, a.get("kind"), url, red,
                                                          a.get("priority"), a.get("why"), refs, keep):
                continue            # без изменений — не пишем: импорт модуля не должен трогать файл базы
            # дату редакции из реестра ставим, пока робот сам не увидел более свежую:
            # иначе после каждого прохода реестр «откатывал» бы находку и событие приходило заново
            con.execute("""UPDATE watched_acts SET title=?, kind=?, lex_url=?,
                           redaction=CASE WHEN last_changed_at IS NULL THEN ? ELSE redaction END,
                           priority=?, why=?, rules_refs=?, status=? WHERE code=?""",
                        (a.get("title") or code, a.get("kind"), url, a.get("redaction"),
                         a.get("priority"), a.get("why"), refs, keep, code))
        else:
            con.execute("""INSERT INTO watched_acts (code, title, kind, lex_url, redaction, priority,
                           why, rules_refs, status) VALUES (?,?,?,?,?,?,?,?,?)""",
                        (code, a.get("title") or code, a.get("kind"), url, a.get("redaction"),
                         a.get("priority"), a.get("why"), refs, status))
            n += 1
    return n


def _ensure_seed():
    """Вызывается при импорте модуля: таблицы уже созданы db_build/ensure_schema, акты доливаем."""
    try:
        with db.tx() as con:
            n = seed_acts(con)
            if n:
                db.audit(con, "юрист", "реестр отслеживаемых актов", "watched_acts", {"добавлено": n})
    except Exception as e:      # база может быть ещё не собрана — сервер всё равно должен подняться
        print(f"lawwatch: реестр актов не залит: {e}")


# --------------------------------------------------------------------------- #
#  Текст страницы: снятие вёрстки и нормализация
# --------------------------------------------------------------------------- #

# Куски, которые меняются сами по себе и к содержанию акта отношения не имеют.
# Без их вычистки хэш «менялся» бы каждый день — и юрист перестал бы верить роботу.
NOISE = [
    (re.compile(r"<!--.*?-->", re.S), " "),                       # комментарии вёрстки
    (re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I), " "),
    (re.compile(r"(просмотр\w*|ko'rishlar|ko‘rishlar|марта прочитано)\s*[:\-]?\s*\d[\d  ]*", re.I), " "),
    (re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?\b"), " "),             # часы генерации страницы
    (re.compile(r"(сформирован|сгенерирован|yaratilgan)\w*\s+\d{2}\.\d{2}\.\d{4}", re.I), " "),
    (re.compile(r"csrf[-_]?token[^\s\"']*", re.I), " "),
    (re.compile(r"\b[0-9a-f]{32,}\b", re.I), " "),                # хэши сессий и версии статики
]


def page_text(page: str) -> str:
    """HTML → чистый текст без меняющихся кусков. Один и тот же акт даёт один и тот же текст."""
    for rx, rep in NOISE[:2]:
        page = rx.sub(rep, page)
    page = re.sub(r"<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", page, flags=re.I)
    text = htmllib.unescape(re.sub(r"<[^>]+>", " ", page))
    for rx, rep in NOISE[2:]:
        text = rx.sub(rep, text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text.strip()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def redaction_of(text: str) -> Optional[str]:
    """
    Первая дата из блока «Источники изменений / Источники опубликования» — дата свежей редакции.
    У страниц-«оболочек» блока нет: возвращаем None, тогда работает только сравнение хэша.
    """
    i = text.find("Источники изменений")
    if i < 0:
        i = text.find("O'zgartirishlar manbai")
    if i < 0:
        return None
    m = re.search(r"\b(\d{2}\.\d{2}\.\d{4})\b", text[i:i + 600])
    return m.group(1) if m else None


# --------------------------------------------------------------------------- #
#  Загрузка страницы: robots.txt, пауза, честный отказ при 403 и капче
# --------------------------------------------------------------------------- #

def fetch(url: str, timeout: int = TIMEOUT_FEED) -> tuple:
    """
    (текст_страницы | None, причина). Никаких обходов защиты: robots.txt запрещает,
    403 или капча — возвращаем None и причину, вызывающий код пишет «источник недоступен».
    """
    if _get is None or robots_check is None:
        return None, "сетевой слой недоступен (app/valuation_sources не импортировался)"
    allowed, why = robots_check(url)
    if not allowed:
        return None, why or "robots.txt запрещает загрузку"
    time.sleep(PAUSE_SEC)
    try:
        page = _get(url, timeout=timeout)
    except Exception as e:
        code = getattr(e, "code", None)
        if code in (401, 403, 429):
            return None, f"сайт отказал в доступе (HTTP {code}) — обходить защиту нельзя"
        return None, f"не открылось ({type(e).__name__}{': ' + str(code) if code else ''})"
    low = page.lower()
    if len(page) < 1500 and ("captcha" in low or "проверка браузера" in low or "cf-challenge" in low):
        return None, "вместо страницы показана проверка (капча) — обходить её нельзя"
    return page, ""


# --------------------------------------------------------------------------- #
#  Краткое изложение (summary)
# --------------------------------------------------------------------------- #

SYSTEM = ("Ты помощник юриста страховой компании в Узбекистане. Пиши по-русски, просто, "
          "2–4 предложения, без юридического жаргона. Ничего не придумывай: если в тексте "
          "нет сути изменения, так и скажи. Оценок и советов не давай.")


# Служебные надписи страницы lex.uz — в изложение их тащить нельзя
FURNITURE = re.compile(r"^(Все\b|Ссылка на|Индексация|Источники|Вид\b|Дополни|Основные реквизиты|"
                       r"Кодификация|Пересмотренные|Акты основания|Корреспондент|ONLINE|Рус|Ўзб|O'|"
                       r"\[|ОКОЗ|ТСЗ|Классификатор)")


def _llm_on() -> bool:
    """ИИ настроен? Если нет — не дёргаем его вовсе, чтобы не засорять журнал обращений."""
    try:
        from .llm import enabled
        return bool(enabled())
    except Exception:
        return False


def _plain_summary(title: str, text: str) -> str:
    """
    Выжимка без ИИ: первый кусок, похожий на связный текст (длинная строка с точками),
    а не оглавление и не служебные надписи страницы. Не нашли — возвращаем пусто,
    ничего не сочиняем.
    """
    for line in (text or "").split("\n"):
        s = line.strip()
        if len(s) < 150 or s.count(".") < 2 or FURNITURE.match(s):
            continue
        if sum(ch.isupper() for ch in s) > len(s) * 0.4:      # заголовок капсом — не проза
            continue
        return s[:600]
    return ""


def summarize(title: str, text: str = "", url: str = "", what: str = "") -> str:
    """
    2–4 предложения своими словами с обязательной плашкой. ИИ — только через app/llm.py;
    его нет или он не настроен — простая выжимка из первых значимых абзацев.
    """
    body = None
    prompt = (f"{what}\n\nНазвание: {title}\n\nТекст (фрагмент):\n{(text or '')[:6000]}\n\n"
              "Изложи своими словами в 2–4 предложениях, что это за документ и что в нём "
              "может касаться страховой компании.")
    try:
        if ask is not None:
            body = ask(prompt)
        elif _chat is not None and _llm_on():
            body = _chat("изложение изменения законодательства", SYSTEM, prompt, max_tokens=300)
    except Exception as e:
        print("lawwatch: ИИ недоступен:", e)
        body = None
    if not body:
        # Без ИИ: то, что система знает точно (what), плюс первый связный абзац, если он нашёлся.
        body = " ".join(x for x in (what, f"Акт: {title}.", _plain_summary(title, text),
                                    "Что именно изменилось, видно только в оригинале по ссылке: "
                                    "смотреть статьи и пункты, на которых стоят наши проверки.") if x)
    body = re.sub(r"\s+", " ", str(body)).strip()
    return f"{DISCLAIMER}\n{body}" if body else DISCLAIMER


# --------------------------------------------------------------------------- #
#  События и пометка правил (LAWWATCH-01)
# --------------------------------------------------------------------------- #

def add_event(con, kind: str, title: str, source: str, url: str = None, act_code: str = None,
              published_at: str = None, summary: str = None, note: str = None) -> Optional[int]:
    """Пишет событие. Новые документы и новости не дублируются по адресу."""
    if kind not in KINDS:
        raise ValueError(f"вид события должен быть одним из: {', '.join(KINDS)}")
    if kind in (KIND_NEWDOC, KIND_NEWS) and url:
        if con.execute("SELECT 1 FROM law_events WHERE kind=? AND url=?", (kind, url)).fetchone():
            return None
    if kind == KIND_REDACTION and act_code and published_at:
        # одну и ту же редакцию второй раз не приносим, даже если реестр в docs ещё не обновлён
        if con.execute("SELECT 1 FROM law_events WHERE kind=? AND act_code=? AND published_at=?",
                       (kind, act_code, published_at)).fetchone():
            return None
    cur = con.execute("""INSERT INTO law_events (created_at, kind, act_code, title, source, url,
                         published_at, summary, seen, note) VALUES (?,?,?,?,?,?,?,?,0,?)""",
                      (db.now(), kind, act_code, (title or "без названия")[:500], source, url,
                       published_at, summary, note))
    return cur.lastrowid


def mark_rules(con, act_code: str, reason: str) -> list:
    """LAWWATCH-01: правила, ссылающиеся на изменившийся акт, получают «требует пересмотра»."""
    row = con.execute("SELECT rules_refs FROM watched_acts WHERE code=?", (act_code,)).fetchone()
    try:
        refs = json.loads(row["rules_refs"]) if row and row["rules_refs"] else []
    except Exception:
        refs = []
    marked = []
    for code in refs:
        if not con.execute("SELECT 1 FROM rules WHERE code=?", (code,)).fetchone():
            continue                      # правила с таким кодом в движке нет — ничего не выдумываем
        con.execute("UPDATE rules SET review_status=?, review_reason=?, review_since=? WHERE code=?",
                    ("требует пересмотра", reason[:500], db.now(), code))
        marked.append(code)
    return marked


def rules_to_review(con) -> list:
    return db.rows(con, "SELECT code, name, severity, legal_ref, review_reason, review_since "
                        "FROM rules WHERE review_status='требует пересмотра' ORDER BY review_since DESC, code")


def add_topic(event_title: str, hint: str) -> None:
    """Новое событие → тема на разбор в реестре знаний (логика — в app/knowledge.py)."""
    try:
        from . import knowledge
        knowledge.add_topic(knowledge.TopicIn(
            topic=f"Разобрать изменение законодательства: {event_title}"[:300],
            area="закон", priority=2, source_hint=hint))
    except Exception as e:                # реестр знаний недоступен — проход не срываем
        print("lawwatch: тема знаний не заведена:", e)


# --------------------------------------------------------------------------- #
#  Проход: акты
# --------------------------------------------------------------------------- #

def check_act(con, act: dict) -> dict:
    """Одна страница lex.uz: хэш и дата редакции. Возвращает строку журнала прохода."""
    code, url = act["code"], act["lex_url"]
    out = {"code": code, "title": act["title"], "url": url}
    if not url:
        out["result"] = ST_NOURL if act["status"] != ST_MANUAL else ST_MANUAL
        con.execute("UPDATE watched_acts SET last_checked_at=? WHERE code=?", (db.now(), code))
        return out
    page, why = fetch(url, timeout=TIMEOUT_ACT)
    if page is None:
        con.execute("UPDATE watched_acts SET status=?, last_checked_at=? WHERE code=?",
                    (ST_DOWN, db.now(), code))
        out["result"] = ST_DOWN
        out["note"] = why
        return out
    text = page_text(page)
    h, red = text_hash(text), redaction_of(text)
    shell = len(text) < SHELL_TEXT_LEN
    changed = bool(act["text_hash"]) and act["text_hash"] != h
    red_changed = bool(red) and bool(act["redaction"]) and red != act["redaction"]
    out.update({"result": "без изменений", "знаков": len(text), "редакция": red,
                "оболочка": shell})
    if act["text_hash"] is None:
        out["result"] = "первая проверка — хэш запомнен"
    if changed or red_changed:
        if red_changed:
            what = (f"Редакция акта на lex.uz изменилась: было {act['redaction']}, стало {red}.")
            note = f"дата редакции: {act['redaction']} → {red}"
        else:
            what = ("Текст страницы акта на lex.uz изменился, но блока дат редакций на ней нет "
                    "(страница отдаёт только реквизиты). Это возможное изменение, а не факт.")
            note = ("возможное изменение: изменился хэш текста, дата редакции на странице не показана"
                    if shell else "изменился хэш текста страницы")
        summary = summarize(act["title"], text, url, what)
        eid = add_event(con, KIND_REDACTION, act["title"], "lex.uz", url, code,
                        published_at=red, summary=summary, note=note)
        reason = f"изменён акт {code}" + (f", редакция {act['redaction']} → {red}" if red_changed else
                                          ", изменился текст страницы")
        marked = mark_rules(con, code, reason)
        con.execute("""UPDATE watched_acts SET text_hash=?, text_len=?, status=?, last_checked_at=?,
                       last_changed_at=?, redaction=COALESCE(?, redaction) WHERE code=?""",
                    (h, len(text), ST_CHANGED, db.now(), db.now(), red, code))
        db.audit(con, "юрист", "изменение акта", f"lex.uz {code}",
                 {"акт": act["title"][:120], "редакция": red, "правила": marked, "событие": eid})
        out.update({"result": ST_CHANGED, "правила": marked, "событие": eid,
                    "темы": [(act["title"], f"{url} (событие {eid})")]})
        return out
    con.execute("""UPDATE watched_acts SET text_hash=?, text_len=?, status=?, last_checked_at=?
                   WHERE code=?""",
                (h, len(text), ST_WATCH if act["status"] != ST_CHANGED else ST_CHANGED, db.now(), code))
    return out


# --------------------------------------------------------------------------- #
#  Проход: ленты
# --------------------------------------------------------------------------- #

def _seen_url(con, kind: str, url: str) -> bool:
    """Это событие уже было? Проверка до записи, чтобы зря не звать ИИ."""
    return bool(con.execute("SELECT 1 FROM law_events WHERE kind=? AND url=?", (kind, url)).fetchone())


def _clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()


def _matches(title: str) -> Optional[str]:
    """Первое совпавшее ключевое слово реестра или None."""
    t = _fold(title)
    for w in keywords():
        if w and w in t:
            return w
    return None


def _near_date(page: str, pos: int) -> Optional[str]:
    """Дата рядом с карточкой в ленте: ищем ДД.ММ.ГГГГ в окне после ссылки."""
    m = re.search(r"\b(\d{2}\.\d{2}\.\d{4})\b", page[pos:pos + 2500])
    return m.group(1) if m else None


def _links(page: str, pattern: str) -> list:
    """[(url, заголовок, позиция)] без повторов; из двух ссылок на один адрес берём ту, где есть текст."""
    found = {}
    for m in re.finditer(r"<a[^>]+href=\"([^\"]*" + pattern + r"[^\"]*)\"[^>]*>(.*?)</a>",
                         page, flags=re.S | re.I):
        url, title = m.group(1), _clean(m.group(2))
        if not title:
            continue
        found.setdefault(url, (title, m.start()))
    return [(u, t, p) for u, (t, p) in found.items()]


def check_lex_feed(con) -> dict:
    """Лента новых документов на главной lex.uz: по ключевым словам → «новый документ»."""
    page, why = fetch(LEX_FEED, timeout=TIMEOUT_FEED)
    if page is None:
        return {"источник": "lex.uz — лента новых документов", "результат": ST_DOWN, "причина": why}
    items = _links(page, r"/docs/\d+")
    added, topics = 0, []
    # сначала изложения (они могут идти в ИИ и писать свой журнал), потом запись событий:
    # вложенная запись в SQLite во время открытой транзакции упирается в блокировку
    new = [(url if url.startswith("http") else "https://lex.uz" + url, title, _near_date(page, pos))
           for url, title, pos in items if _matches(title)]
    new = [(u, t, d, summarize(t, t, u, "Это новый документ в ленте новых актов lex.uz."))
           for u, t, d in new if not _seen_url(con, KIND_NEWDOC, u)]
    for full, title, when, summary in new:
        eid = add_event(con, KIND_NEWDOC, title, "lex.uz", full, published_at=when,
                        summary=summary, note="сигнал из ленты новых актов, решает юрист")
        if eid:
            added += 1
            topics.append((title, full))
    if items:
        db.audit(con, "юрист", "проверена лента новых актов", "lex.uz",
                 {"в ленте": len(items), "по страхованию": added})
    return {"источник": "lex.uz — лента новых документов", "результат": "ok" if items else "лента пуста",
            "в ленте": len(items), "новых по страхованию": added, "темы": topics}


def check_napp_news(con) -> dict:
    """
    Новости НАПП. Сайт отвечает 200 на любой адрес, поэтому смотрим содержимое:
    нет ни одной ссылки вида /news/... — раздел не найден, тревогу не поднимаем.
    """
    page, why = fetch(NAPP_NEWS, timeout=TIMEOUT_FEED)
    if page is None:
        return {"источник": "napp.uz — новости", "результат": ST_DOWN, "причина": why}
    items = _links(page, r"/news/")
    if not items:
        return {"источник": "napp.uz — новости", "результат": "раздел не найден (ссылок на новости нет)"}
    added = 0
    new = [(url if url.startswith("http") else "https://napp.uz" + url, title, _near_date(page, pos))
           for url, title, pos in items if _matches(title)]
    new = [(u, t, d, summarize(t, t, u, "Это новость на сайте НАПП."))
           for u, t, d in new if not _seen_url(con, KIND_NEWS, u)]
    for full, title, when, summary in new:
        eid = add_event(con, KIND_NEWS, title, "napp.uz", full, published_at=when, summary=summary,
                        note="новость, не норма: источник нормы — только lex.uz")
        if eid:
            added += 1
    db.audit(con, "юрист", "проверены новости НАПП", "napp.uz",
             {"в ленте": len(items), "по страхованию": added})
    return {"источник": "napp.uz — новости", "результат": "ok", "в ленте": len(items),
            "новых по страхованию": added}


# --------------------------------------------------------------------------- #
#  Полный проход и расписание
# --------------------------------------------------------------------------- #

def _flush_topics(entry: dict) -> None:
    """Темы на разбор заводим вне транзакции прохода: knowledge.add_topic открывает своё соединение."""
    for title, hint in (entry or {}).pop("темы", []) or []:
        add_topic(title, hint)


def check_all(only: str = None) -> dict:
    """Один проход: акты, лента новых документов, новости. only — код акта для точечной проверки."""
    if _state["running"]:
        return {"пропущено": "проверка уже идёт", "last": _state["last"]}
    _state["running"] = True
    log = []
    try:
        with db.tx() as con:
            seed_acts(con)
            sql = "SELECT * FROM watched_acts WHERE 1=1" + (" AND code=?" if only else "")
            acts = db.rows(con, sql + " ORDER BY CASE priority WHEN 'высокий' THEN 1 "
                                 "WHEN 'средний' THEN 2 ELSE 3 END, code", *( [only] if only else [] ))
        for a in acts:
            with db.tx() as con:               # каждый акт своей транзакцией: сеть долгая
                try:
                    log.append(check_act(con, a))
                except Exception as e:
                    log.append({"code": a["code"], "result": "ошибка", "note": f"{type(e).__name__}: {e}"})
            _flush_topics(log[-1])             # темы знаний — уже после коммита
        if not only:
            with db.tx() as con:
                log.append(check_lex_feed(con))
            _flush_topics(log[-1])
            with db.tx() as con:
                log.append(check_napp_news(con))
        _state["last"] = db.now()
        _state["error"] = None
    except Exception as e:                     # сеть или база не должны ронять сервер
        _state["error"] = f"{type(e).__name__}: {e}"
        log.append({"result": "ошибка прохода", "note": _state["error"]})
    finally:
        _state["running"] = False
        _state["log"] = log
    changed = [x for x in log if x.get("result") == ST_CHANGED]
    down = [x for x in log if x.get("result") == ST_DOWN]
    return {"когда": _state["last"], "проверено актов": len([x for x in log if x.get("code")]),
            "изменилось": len(changed), "недоступно источников": len(down), "журнал": log}


CHECK_HOUR = 6           # 06:30 по Ташкенту: результат успевает в доклад 08:00
CHECK_MINUTE = 30


def _scheduler():
    """Раз в сутки, тем же приёмом, что автообновление НАПП: поток внутри сервера."""
    time.sleep(120)                    # даём серверу подняться
    done_for = None
    while True:
        try:
            now = datetime.now()
            today = now.date().isoformat()
            if done_for != today and (now.hour, now.minute) >= (CHECK_HOUR, CHECK_MINUTE):
                check_all()
                done_for = today
                if _state.get("error"):
                    background.failed("lawwatch", kind="ошибка прохода")
                else:
                    background.ok("lawwatch")
        except Exception as e:
            background.failed("lawwatch", e)
        time.sleep(600)


def start_scheduler():
    background.start("lawwatch", _scheduler)


def last_status() -> dict:
    """Статус агента-юриста — для офиса и ежедневного доклада."""
    unseen, changed, last = 0, 0, _state["last"]
    try:
        with db.tx() as con:
            unseen = con.execute("SELECT COUNT(*) FROM law_events WHERE seen=0").fetchone()[0]
            changed = con.execute("SELECT COUNT(*) FROM watched_acts WHERE status=?",
                                  (ST_CHANGED,)).fetchone()[0]
            # сервер могли перезапустить — время последней проверки берём из базы
            last = last or con.execute("SELECT MAX(last_checked_at) FROM watched_acts").fetchone()[0]
    except Exception:
        pass
    return {"last": last, "running": _state["running"], "error": _state["error"],
            "unseen_events": unseen, "changed_acts": changed,
            "schedule": f"каждые сутки в {CHECK_HOUR:02d}:{CHECK_MINUTE:02d}"}


def events_for_report(con, d0: str, d1: str) -> list:
    """События за сутки — для раздела ежедневного доклада."""
    return db.rows(con, "SELECT * FROM law_events WHERE created_at >= ? AND created_at < ? "
                        "ORDER BY kind, id", d0, d1)


# --------------------------------------------------------------------------- #
#  Точки подключения
# --------------------------------------------------------------------------- #

class SeenIn(BaseModel):
    who: str = "law"
    note: str = ""


@router.get("/law-events")
def get_events(limit: int = 50, kind: Optional[str] = None, unseen: bool = False):
    """Журнал событий: что робот принёс. kind — 'новая редакция' | 'новый документ' | 'новость'."""
    if kind and kind not in KINDS:
        raise HTTPException(400, f"вид события должен быть одним из: {', '.join(KINDS)}")
    sql, args = "SELECT * FROM law_events WHERE 1=1", []
    if kind:
        sql, args = sql + " AND kind=?", args + [kind]
    if unseen:
        sql += " AND seen=0"
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(int(limit), 1))
    with db.tx() as con:
        items = db.rows(con, sql, *args)
        unseen_n = con.execute("SELECT COUNT(*) FROM law_events WHERE seen=0").fetchone()[0]
        review = rules_to_review(con)
    return {"count": len(items), "unseen": unseen_n, "kinds": list(KINDS),
            "disclaimer": DISCLAIMER, "rules_to_review": review, "items": items}


@router.post("/law-events/{event_id}/seen")
def mark_seen(event_id: int, x: SeenIn):
    """Юрист разобрал событие. Пометку с правил снимает он же — POST /rules/{code}/review-ok."""
    with db.tx() as con:
        row = con.execute("SELECT * FROM law_events WHERE id=?", (event_id,)).fetchone()
        if not row:
            raise HTTPException(404, "событие не найдено")
        con.execute("UPDATE law_events SET seen=1, note=COALESCE(NULLIF(?,''), note) WHERE id=?",
                    (x.note, event_id))
        if row["act_code"]:              # акт разобран — возвращаем его в обычное наблюдение
            con.execute("UPDATE watched_acts SET status=? WHERE code=? AND status=?",
                        (ST_WATCH, row["act_code"], ST_CHANGED))
        db.audit(con, "юрист", "событие законодательства разобрано", f"law_event:{event_id}",
                 {"кто": x.who, "вывод": x.note[:300]})
    return {"ok": True, "event_id": event_id}


@router.post("/rules/{code}/review-ok")
def review_ok(code: str, x: SeenIn):
    """Снять пометку «требует пересмотра» с правила. Ставит робот, снимает только человек."""
    if not x.note.strip():
        raise HTTPException(400, "напишите одной строкой, почему изменение правила не касается "
                                 "или что уже поправлено")
    with db.tx() as con:
        if not con.execute("SELECT 1 FROM rules WHERE code=?", (code,)).fetchone():
            raise HTTPException(404, "правила с таким кодом нет")
        con.execute("UPDATE rules SET review_status='ok', review_reason=?, review_since=? WHERE code=?",
                    (f"снято {db.now()[:10]}: {x.note.strip()[:400]}", db.now(), code))
        db.audit(con, "юрист", "снята пометка пересмотра", f"rule:{code}",
                 {"кто": x.who, "почему": x.note.strip()[:300]})
    return {"ok": True, "rule": code}


@router.get("/watched-acts")
def get_acts():
    """Реестр отслеживаемых актов и состояние последней проверки."""
    with db.tx() as con:
        seed_acts(con)
        items = db.rows(con, "SELECT * FROM watched_acts ORDER BY CASE priority WHEN 'высокий' THEN 1 "
                             "WHEN 'средний' THEN 2 ELSE 3 END, code")
    for it in items:
        try:
            it["rules_refs"] = json.loads(it["rules_refs"] or "[]")
        except Exception:
            it["rules_refs"] = []
    return {"count": len(items), "status": last_status(), "items": items}


@router.post("/lawwatch/check")
def post_check(act: Optional[str] = None):
    """Ручная проверка сейчас. Полный обход занимает несколько минут (паузы между запросами)."""
    return check_all(only=act)


@router.get("/lawwatch/status")
def get_status():
    return {**last_status(), "журнал последнего прохода": _state["log"]}


@router.get("/law-feed", response_class=HTMLResponse)
def law_feed_page(embed: int = 0):
    """Страница «Законодательство». Оформление делает дизайнер — файл app/lawfeed.html."""
    if not FEED_HTML.exists():
        raise HTTPException(404, "страница app/lawfeed.html ещё не сделана")
    return _page(web.read_text(FEED_HTML), "/law-feed", embed)


# tools/db_build.py держит открытую запись в той же базе и заливает реестр сам (seed_acts на своём соединении):
# второе соединение при импорте упёрлось бы в блокировку («database is locked»)
if not os.environ.get("SURVEYOR_DB_BUILD"):
    _ensure_seed()


def _page(html: str, active: str, embed: int) -> str:
    """Общая раскладка из app/main.py; импорт отложенный — main.py сам подключает этот модуль."""
    from .main import page
    return page(html, active, bool(embed))
