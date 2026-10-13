"""Доступ к базе. SQLite сейчас, PostgreSQL потом: SQL здесь без диалектных особенностей."""
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .engine import Reference

log = logging.getLogger("surveyor.db")

ROOT = Path(__file__).resolve().parent.parent
# STORAGE_DIR — постоянный диск на сервере (на Railway смонтирован в /srv/storage): база, фото, загрузки, копии
# живут там и переживают обновления кода. Без переменной — обычная папка data/ проекта.
DATA_DIR = Path(os.environ["STORAGE_DIR"]) if os.environ.get("STORAGE_DIR") else ROOT / "data"
DB_PATH = DATA_DIR / "surveyor.db"


def stored_path(p) -> str:
    """Путь файла для записи в базу. Внутри проекта — относительный (как раньше); на постоянном диске
    сервера (STORAGE_DIR вне проекта, например /srv/storage) — абсолютный. Читатели делают ROOT / путь:
    для абсолютного пути это даёт сам абсолютный путь, поэтому старые записи и новые читаются одинаково."""
    p = Path(p)
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        return p.resolve().as_posix()


def init_storage():
    """На постоянном диске ещё нет базы — переносим собранную при сборке образа (справочники, статистика).
    Если база уже есть, новые справочники образа доводит app/refsync.py после ensure_schema()."""
    if DATA_DIR == ROOT / "data":
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    built = ROOT / "data" / "surveyor.db"
    if not built.exists() or built.resolve() == DB_PATH.resolve():
        return
    empty = True
    if DB_PATH.exists():
        # файл мог появиться раньше переноса (модуль подключился к базе при импорте) — тогда он пустой
        try:
            con = sqlite3.connect(DB_PATH)
            empty = con.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0
            con.close()
        except sqlite3.Error:
            empty = True
    if empty:
        # штатной копией SQLite, а не копированием файла: в режиме WAL часть данных может лежать в -wal
        snapshot(built, DB_PATH)
        print(f"хранилище: собранная база перенесена на постоянный диск {DB_PATH}")
    else:
        print(f"хранилище: база на постоянном диске {DB_PATH}")
SCHEMA = ROOT / "db" / "schema.sql"

BUSY_TIMEOUT_MS = 5000
_wal_done = set()                 # файлы, для которых WAL уже включён в этом процессе
_wal_lock = threading.Lock()


class Connection(sqlite3.Connection):
    """Обычное соединение плюс список действий «после фиксации» (см. after_commit)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.after_commit_jobs = []


def _pragmas(con: sqlite3.Connection, path) -> None:
    """
    Настройки соединения. WAL: читатели не ждут писателя, писатель не ждёт читателей — при 20
    одновременных расчётах это убирает очередь. synchronous=NORMAL в режиме WAL не теряет
    целостность базы при сбое питания (теряется максимум последняя фиксация), а пишет в разы быстрее.
    Режим WAL хранится в самом файле, поэтому включаем его один раз на процесс.
    """
    key = str(path)
    if key not in _wal_done:
        with _wal_lock:
            if key not in _wal_done:
                # переключение режима требует монопольного доступа: не ждём, база занята —
                # попробует следующее соединение
                con.execute("PRAGMA busy_timeout = 200")
                try:
                    con.execute("PRAGMA journal_mode = WAL")
                except sqlite3.OperationalError as e:
                    log.info("WAL пока не включён (база занята): %s", e)
                else:
                    _wal_done.add(key)
    con.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    con.execute("PRAGMA synchronous = NORMAL")
    con.execute("PRAGMA foreign_keys = ON")


def connect_path(path, row_factory=True) -> sqlite3.Connection:
    """Соединение к произвольному файлу базы с теми же настройками (для tools/*)."""
    con = sqlite3.connect(str(path), check_same_thread=False, factory=Connection)
    if row_factory:
        con.row_factory = sqlite3.Row
    _pragmas(con, path)
    return con


def connect() -> sqlite3.Connection:
    return connect_path(DB_PATH)


def after_commit(con, job) -> bool:
    """
    Выполнить job() после успешной фиксации транзакции db.tx() — для долгих внешних действий
    (отправка в Telegram): блокировка записи к этому моменту уже снята. Если соединение не наше
    (без списка действий) — возвращает False, вызывающий делает действие сам.
    """
    jobs = getattr(con, "after_commit_jobs", None)
    if jobs is None:
        return False
    jobs.append(job)
    return True


def holds_write_lock(con) -> bool:
    """Открыта ли у соединения пишущая транзакция (после INSERT/UPDATE/DELETE до commit)."""
    return bool(getattr(con, "in_transaction", False))


_local = threading.local()

# Соединения db.tx() переиспользуются внутри потока: открытие соединения в режиме WAL стоит около
# 1 мс (чтение индекса журнала), а запрос с входом открывал два-три соединения. У каждого потока —
# одно своё соединение на файл базы; вложенная db.tx() в том же потоке получает отдельное, как раньше.
# Соединение возвращается в пул только без открытой транзакции; файл базы заменили (тесты, перенос) —
# соединение закрывается и открывается заново.
_pool_lock = threading.Lock()
_pool_all = {}                    # id(соединения) -> соединение: закрыть всё разом (close_pool)


def _file_sig(path) -> tuple:
    try:
        st = os.stat(path)
        return (st.st_dev, st.st_ino)
    except OSError:
        return None


def _usable(con) -> bool:
    try:
        return not con.in_transaction
    except sqlite3.ProgrammingError:          # соединение уже закрыто
        return False


def _drop(con):
    with _pool_lock:
        _pool_all.pop(id(con), None)
    try:
        con.close()
    except Exception:
        pass


def close_pool(path=None) -> None:
    """Закрыть свободные соединения пула (все или к одному файлу) — перед удалением файла базы."""
    with _pool_lock:
        items = list(_pool_all.values())
    for con in items:
        if getattr(con, "pool_busy", False):
            continue
        if path is not None and getattr(con, "pool_path", None) != str(path):
            continue
        _drop(con)


def _checkout():
    """(соединение, из пула ли оно)."""
    path = str(DB_PATH)
    cons = getattr(_local, "cons", None)
    if cons is None:
        cons = _local.cons = {}
    con = cons.get(path)
    if con is not None and not getattr(con, "pool_busy", False):
        if _usable(con) and getattr(con, "pool_sig", None) == _file_sig(path):
            con.pool_busy = True
            return con, True
        _drop(con)
        cons.pop(path, None)
        con = None
    fresh = connect()
    if con is None:                          # места в пуле потока нет только при вложенной db.tx()
        fresh.pool_path, fresh.pool_sig, fresh.pool_busy = path, _file_sig(path), True
        cons[path] = fresh
        with _pool_lock:
            _pool_all[id(fresh)] = fresh
        return fresh, True
    return fresh, False


def current_tx():
    """Соединение самой внутренней db.tx() в этом потоке (или None)."""
    stack = getattr(_local, "stack", None)
    return stack[-1] if stack else None


@contextmanager
def tx():
    con, pooled = _checkout()
    stack = getattr(_local, "stack", None)
    if stack is None:
        stack = _local.stack = []
    stack.append(con)
    committed = False
    try:
        yield con
        con.commit()
        committed = True
    finally:
        stack.remove(con)
        jobs, con.after_commit_jobs = con.after_commit_jobs, []
        if not committed:
            try:
                con.rollback()               # как раньше: без commit изменения не сохраняются
            except Exception:
                pass
        if pooled and _usable(con):
            con.pool_busy = False
        elif pooled:
            _drop(con)
            getattr(_local, "cons", {}).pop(str(getattr(con, "pool_path", "")), None)
        else:
            con.close()
    # сюда доходим только без исключения: действия «после фиксации» при откате не выполняются
    for job in jobs:
        try:
            job()
        except Exception:                  # внешнее действие не должно ломать уже сохранённое
            log.exception("действие после фиксации не выполнено")


def snapshot(src, dest) -> None:
    """Целостная копия базы штатным механизмом SQLite (учитывает журнал WAL)."""
    s = sqlite3.connect(str(src), timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        d = sqlite3.connect(str(dest))
        try:
            s.backup(d)
        finally:
            d.close()
    finally:
        s.close()


# Колонки, добавленные в уже существующие таблицы. SQLite не умеет ADD COLUMN IF NOT EXISTS,
# а CREATE TABLE IF NOT EXISTS старую таблицу не меняет — поэтому доводим руками.
# Типы без диалектных особенностей: на PostgreSQL те же ALTER TABLE ... ADD COLUMN пройдут.
ADDED_COLUMNS = {
    "company_financials": [
        # 03.10.2026: разбивка собственных средств и резервов и происхождение цифр (tools/ranking_parse.py)
        ("total_capital", "REAL"),
        ("share_capital", "REAL"),
        ("reserves_gross", "REAL"),
        ("reserves_net", "REAL"),
        ("total_assets", "REAL"),
        ("basis", "TEXT"),
        ("source_url", "TEXT"),
        ("confirmed", "INTEGER"),
    ],
    "photos": [
        ("doc_kind", "TEXT NOT NULL DEFAULT 'фото объекта'"),
        ("parse_status", "TEXT"),
        ("parsed_at", "TEXT"),
        ("parsed_json", "TEXT"),
    ],
    "requests": [
        # итог коллективного согласования: 'не требуется'|'на согласовании'|'согласован'|'отклонён'
        ("approval_status", "TEXT NOT NULL DEFAULT 'не требуется'"),
        ("general_agreement_id", "INTEGER"),   # по какому генеральному соглашению идёт запрос
        # кто подал запрос из приложения: у роли «сотрудник» нет ID агента в ЕАИС,
        # поэтому связь «человек — свой запрос» держим напрямую (app/registration.py)
        ("created_by_user_id", "INTEGER"),
    ],
    "users": [
        ("telegram_id", "TEXT"),               # вход из мини-приложения Telegram (app/telegram.py)
        ("position", "TEXT"),                  # должность со слов человека (регистрация, app/registration.py)
        ("department", "TEXT"),                # департамент со слов человека (свободный текст с подсказками)
        ("google_sub", "TEXT"),                # вход через Google: вечный идентификатор аккаунта (app/google_auth.py)
        ("email", "TEXT"),                     # рабочая почта из аккаунта Google (проверена самим Google)
        ("unit", "TEXT"),                      # отдел: сотрудника заводит администратор вручную (app/staff.py)
        ("must_change_password", "INTEGER NOT NULL DEFAULT 0"),   # временный пароль — сменить при входе
    ],
    "claims": [
        # загрузка страховых случаев из Excel (app/claims_import.py)
        ("product_code", "TEXT"),
        ("contract_no", "TEXT"),
        ("branch", "TEXT"),
        ("region", "TEXT"),
        ("sum_insured", "REAL"),
        ("premium", "REAL"),
        ("batch_id", "INTEGER"),
        ("updated_at", "TEXT"),
    ],
    "pd_consents": [
        # обязательные поля из раздела 7 docs/Регистрация и роли.md
        ("consent_text_hash", "TEXT"),                       # хэш показанного текста
        ("scope", "TEXT NOT NULL DEFAULT 'основное'"),       # 'основное' | 'телефон'
        ("revoked_at", "TEXT"),                              # отзыв — заполнением даты, строку не удаляем
    ],
    "reg_codes": [
        # защита кода регистрации от перебора (раздел 7 docs/Регистрация и роли.md)
        ("salt", "TEXT"),
        ("sent_total", "INTEGER NOT NULL DEFAULT 0"),
        ("first_sent_at", "TEXT"),
        ("exhausted", "INTEGER NOT NULL DEFAULT 0"),
        ("blocked_until", "TEXT"),
    ],
    "checklists": [
        # 22.09.2026: зачем документ нужен. 'анализ' — для оценки риска, 'оформление' — только
        # для заключения договора (заявление-анкета). Анализ риска строки 'оформление' не спрашивает.
        ("scope", "TEXT NOT NULL DEFAULT 'анализ'"),
    ],
    "google_oauth_states": [
        # зачем начат вход (23.09.2026): 'вход' | 'админ' (один клик для админки) |
        # 'привязка' (добавить Google к уже открытому профилю, app/login_links.py)
        ("purpose", "TEXT"),
        ("link_user_id", "INTEGER"),           # чей профиль дополняем при purpose='привязка'
    ],
    "tg_link_codes": [
        # то же самое для входа по коду боту (app/tg_link.py)
        ("purpose", "TEXT"),
        ("link_user_id", "INTEGER"),
    ],
    "rules": [
        # LAWWATCH-01 (app/lawwatch.py): изменился акт — правила, которые на него ссылаются,
        # помечаются «требует пересмотра». Расчёты не блокируются, пометку снимает юрист.
        ("review_status", "TEXT NOT NULL DEFAULT 'ok'"),   # 'ok' | 'требует пересмотра'
        ("review_reason", "TEXT"),
        ("review_since", "TEXT"),
    ],
    "decision_outcomes": [
        # состав полей модуля вероятности (app/analysis.py, SCHEMA_SQL) плюс result_json:
        # база, заведённая до подключения модуля, доводится без пересоздания
        ("product_code", "TEXT"),
        ("branch", "TEXT"),
        ("class_code", "TEXT"),
        ("verdict", "TEXT"),
        ("factors_json", "TEXT"),
        ("decision", "TEXT"),
        ("decided_at", "TEXT"),
        ("decided_by", "TEXT"),
        ("comment", "TEXT"),
        ("result_json", "TEXT"),
    ],
}


def _old_decision_outcomes(con):
    """
    Первая версия decision_outcomes (created_at NOT NULL, probability 0..1, explanation,
    fact_decision) несовместима с модулем вероятности: ALTER TABLE в SQLite не снимает
    NOT NULL с created_at, поэтому таблицу переименовываем, а данные переносит
    _carry_decision_outcomes() уже после schema.sql.
    """
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(decision_outcomes)")}
    except Exception:
        return False
    if not cols or "sent_at" in cols:          # таблицы нет или она уже новая
        return False
    con.execute("DROP TABLE IF EXISTS decision_outcomes_old")
    con.execute("ALTER TABLE decision_outcomes RENAME TO decision_outcomes_old")
    # имя индекса занято старой таблицей — иначе CREATE INDEX IF NOT EXISTS из schema.sql промолчит
    con.execute("DROP INDEX IF EXISTS ix_decision_outcomes_request")
    con.execute("DROP INDEX IF EXISTS ix_decision_outcomes_scope")
    return True


def _carry_decision_outcomes(con):
    """Переносит прогнозы из старой таблицы в новую: вероятность 0..1 → проценты, факт → decision."""
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(decision_outcomes_old)")}
    except Exception:
        return 0
    if not cols:
        return 0
    moved = 0
    names = [r[1] for r in con.execute("PRAGMA table_info(decision_outcomes_old)")]
    for row in con.execute("SELECT * FROM decision_outcomes_old ORDER BY id").fetchall():
        r = dict(zip(names, tuple(row)))     # соединение может быть без row_factory (tools/db_build.py)
        p = r.get("probability")
        con.execute("INSERT INTO decision_outcomes (request_id, calculation_id, probability, verdict,"
                    " factors_json, model_version, sent_at, decision, decided_at, result_json)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (r.get("request_id"), r.get("calculation_id"),
                     float(p) * 100 if p is not None else 0.0, None, r.get("explanation"),
                     r.get("model_version"), r.get("created_at") or now(),
                     r.get("fact_decision"), r.get("fact_at"), None))
        moved += 1
    con.execute("DROP TABLE decision_outcomes_old")
    return moved


# справочник классов на экранах и в /reference/classes: классов страхования жизни в справочнике нет — строки L*,
# если они вдруг завелись, не показываются; порядок 1…13, 13з, 14…16, 16у, 17, 18 (CAST берёт число в начале кода)
CLASSES_SHOWN = "code NOT LIKE 'L%'"
CLASSES_ORDER = "CAST(code AS INTEGER), code"


CLASSES_SQL = """CREATE TABLE {name} (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    group_code  TEXT REFERENCES groups(code),
    branch      TEXT NOT NULL,
    kind        TEXT NOT NULL
)"""


def _classes_group_nullable(con) -> bool:
    """
    Учётная группа класса (classes.group_code) была NOT NULL. У класса 18 общего страхования («Tibbiy sugʻurta»,
    ПКМ № 80, прил. 6) учётной группы РНП нет: Положение 1882, п. 10 перечисляет классы 1–17 — поэтому колонка
    допускает NULL (отнести класс 18 к группе — решение страховщика). ALTER TABLE в SQLite
    NOT NULL не снимает — таблица пересоздаётся штатным порядком SQLite (проверка ссылок выключена на время
    пересоздания, всё в одной транзакции; строки и ссылки других таблиц на classes сохраняются).
    Идемпотентно: колонка уже допускает NULL или таблицы нет — ничего не делает. Открыта транзакция — пропуск
    (проверку ссылок внутри транзакции не выключить), доведёт следующий запуск.
    """
    try:
        info = con.execute("PRAGMA table_info(classes)").fetchall()
    except Exception:
        return False
    col = [r for r in info if r[1] == "group_code"]
    if not col or not col[0][3]:
        return False
    if con.in_transaction:
        log.warning("classes.group_code: открыта транзакция — пересоздание отложено")
        return False
    fk = con.execute("PRAGMA foreign_keys").fetchone()[0]
    con.execute("PRAGMA foreign_keys = OFF")
    try:
        con.execute("BEGIN")
        try:
            con.execute("DROP TABLE IF EXISTS classes_new")
            con.execute(CLASSES_SQL.format(name="classes_new"))
            con.execute("INSERT INTO classes_new (code, name, group_code, branch, kind) "
                        "SELECT code, name, group_code, branch, kind FROM classes")
            con.execute("DROP TABLE classes")
            con.execute("ALTER TABLE classes_new RENAME TO classes")
            bad = con.execute("PRAGMA foreign_key_check(classes)").fetchall()
            if bad:
                raise sqlite3.IntegrityError(f"classes: нарушены ссылки на учётные группы ({len(bad)})")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.execute(f"PRAGMA foreign_keys = {'ON' if fk else 'OFF'}")
    return True


def migrate(con):
    """Добавляет недостающие колонки. Идемпотентно: повторный запуск ничего не делает."""
    added = []
    if _old_decision_outcomes(con):
        added.append("decision_outcomes: старая таблица переименована, строки переносятся")
    if _classes_group_nullable(con):
        added.append("classes.group_code: допускает NULL (класс 18 без учётной группы)")
    for table, cols in ADDED_COLUMNS.items():
        try:
            have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        except Exception:
            continue
        if not have:                       # таблицы ещё нет — её создаст schema.sql
            continue
        for name, decl in cols:
            if name not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                added.append(f"{table}.{name}")
                if (table, name) == ("checklists", "scope"):
                    # LIKE без lower(): в SQLite lower() кириллицу не приводит
                    con.execute("UPDATE checklists SET scope='оформление' WHERE doc_name LIKE '%аявлен%'")
    return added


def backfill_login_links(con) -> int:
    """
    Переносит уже существующие привязки из users (telegram_id, google_sub) в login_links.
    Идемпотентно: строка создаётся, только если такой пары «провайдер + аккаунт» ещё нет.

    Если один и тот же telegram_id по недосмотру оказался у двух профилей, берём самый ранний:
    уникальный индекс не даст создать два способа входа, ведущих в разные профили.
    """
    made = 0
    for provider, column, display in (("telegram", "telegram_id", "NULL"),
                                      ("google", "google_sub", "email")):
        sql = (f"INSERT INTO login_links (user_id, provider, external_id, display, linked_at)"
               f" SELECT u.id, ?, u.{column}, {display}, COALESCE(u.created_at, ?)"
               f" FROM users u WHERE u.{column} IS NOT NULL AND u.{column} <> ''"
               f"   AND u.id = (SELECT MIN(u2.id) FROM users u2 WHERE u2.{column} = u.{column})"
               f"   AND NOT EXISTS (SELECT 1 FROM login_links l"
               f"                   WHERE l.provider = ? AND l.external_id = u.{column})"
               f"   AND NOT EXISTS (SELECT 1 FROM login_links l2"
               f"                   WHERE l2.provider = ? AND l2.user_id = u.id)")
        made += con.execute(sql, (provider, now(), provider, provider)).rowcount or 0
    return made


def ensure_schema():
    with tx() as con:
        # сначала доводим старые таблицы, потом schema.sql: индексы по новым колонкам
        # иначе не создаются
        migrate(con)
        con.executescript(SCHEMA.read_text(encoding="utf-8"))
        _carry_decision_outcomes(con)       # строки старой decision_outcomes — в новую
        backfill_login_links(con)           # способы входа из users → login_links (23.09.2026)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def audit(con, who: str, action: str, entity: str = None, detail=None):
    con.execute("INSERT INTO audit (ts, who, action, entity, detail) VALUES (?,?,?,?,?)",
                (now(), who, action, entity, json.dumps(detail, ensure_ascii=False) if detail is not None else None))


def rows(con, sql, *args) -> list:
    return [dict(r) for r in con.execute(sql, args)]


# Кэш справочников движка. Сборка из 12 таблиц занимала около половины времени расчёта.
# Ключ — файл базы и сегодняшняя дата (действующие версии тарифов зависят от даты).
# Срок жизни 60 с — на случай правок из другого процесса (tools/db_build.py, demo_seed);
# правки через сервер сбрасывают кэш сразу (reference_changed). Движок справочник не меняет
# (проверено тестом tests/test_optimization.py), поэтому отдаём один общий объект без копирования.
REFERENCE_TTL_SEC = 60
_ref_cache = {}                   # (файл, дата) -> (время загрузки, поколение, Reference)
_ref_lock = threading.Lock()
_ref_generation = [0]


def invalidate_reference() -> None:
    with _ref_lock:
        _ref_generation[0] += 1
        _ref_cache.clear()


def reference_generation() -> int:
    """Номер поколения справочников: растёт при каждом сбросе кэша (правка админа, refsync).
    Модули со своим кэшем в памяти (app/osgor.py — поиск видов деятельности) сверяются с ним."""
    with _ref_lock:
        return _ref_generation[0]


def reference_changed(con=None) -> None:
    """Справочники изменились: сбросить кэш сейчас и ещё раз после фиксации транзакции
    (иначе параллельный расчёт успеет закэшировать старые данные до commit)."""
    invalidate_reference()
    if con is not None:
        after_commit(con, invalidate_reference)


def _db_file(con) -> str:
    try:
        return con.execute("PRAGMA database_list").fetchone()[2] or ""
    except Exception:
        return ""


def load_reference(con) -> Reference:
    """Справочники движка из кэша (см. REFERENCE_TTL_SEC) или из базы."""
    today = datetime.now().date().isoformat()
    path = _db_file(con)
    # незафиксированные правки в этом же соединении кэш не видит — читаем напрямую
    if not path or holds_write_lock(con):
        return _load_reference(con, today)
    key = (path, today)
    now_t = time.monotonic()
    with _ref_lock:
        hit = _ref_cache.get(key)
        gen = _ref_generation[0]
    if hit and now_t - hit[0] < REFERENCE_TTL_SEC and hit[1] == gen:
        return hit[2]
    ref = _load_reference(con, today)
    with _ref_lock:
        if _ref_generation[0] == gen:      # пока читали, справочники не менялись
            _ref_cache[key] = (now_t, gen, ref)
    return ref


def _load_reference(con, today: str) -> Reference:
    """Собирает справочники в структуру движка. Действующие версии тарифов — по дате."""
    base = {(r["class_code"], r["object_type"]): r["net_rate_pct"] for r in rows(con, "SELECT * FROM base_rates")}
    coef = {(r["factor_code"], r["option_code"]): {"name": r["option_name"], "multiplier": r["multiplier"],
                                                   "factor_name": r["factor_name"]}
            for r in rows(con, "SELECT * FROM coefficients")}
    perils = {r["code"]: {"class_code": r["class_code"], "share": r["base_share"] or 0,
                          "name": r["name"], "cat": bool(r["is_catastrophic"])}
              for r in rows(con, "SELECT * FROM perils")}
    load = sum(r["share"] for r in rows(con, "SELECT share FROM load_components"))
    min_rates = {}
    mr_rows = rows(con, """SELECT m.product_code, m.payer_type, m.min_rate_pct, v.level, v.id AS vid,
                                  v.effective_from FROM min_rates m
                           JOIN tariff_versions v ON v.id = m.tariff_version_id
                           WHERE v.effective_from <= ? AND (v.effective_to IS NULL OR v.effective_to >= ?)""",
                   today, today)
    # по продукту и уровню действует одна версия — с наибольшей датой начала, при равных датах — более поздняя
    # (больший id): правка минимальной ставки администратором (app/min_rates.py) заменяет ставку тарифной политики
    # с даты начала, а не складывается с ней по максимуму. Внутри версии — по типу клиента.
    latest = {}
    for r in mr_rows:
        k = (r["product_code"], r["level"])
        latest[k] = max(latest.get(k, ("", 0)), (str(r["effective_from"]), int(r["vid"])))
    for r in mr_rows:
        if (str(r["effective_from"]), int(r["vid"])) != latest[(r["product_code"], r["level"])]:
            continue
        slot = "regulator" if r["level"] == "регулятор" else "company"
        d = min_rates.setdefault(r["product_code"], {"company": {}, "regulator": {}})
        d[slot][r["payer_type"]] = max(d[slot].get(r["payer_type"], 0), r["min_rate_pct"])
    pcs = {}
    for r in rows(con, "SELECT product_code, class_code FROM product_classes ORDER BY part_no"):
        pcs.setdefault(r["product_code"], []).append(r["class_code"])
    # scope появился 22.09.2026 ('анализ' | 'оформление'); на старых базах колонки ещё нет
    has_scope = any(r[1] == "scope" for r in con.execute("PRAGMA table_info(checklists)").fetchall())
    checklists = rows(con, "SELECT scope_type, scope_code, doc_name, required, condition"
                           + (", scope" if has_scope else ", 'анализ' AS scope")
                           + " FROM checklists ORDER BY id")
    fin = rows(con, "SELECT * FROM company_financials ORDER BY report_date DESC LIMIT 1")
    measures = rows(con, "SELECT * FROM preventive_measures")
    # факторы каждого класса: движок строит форму и расчёт по набору своего класса, а не по фиксированному списку
    cls_factors = {}
    for r in rows(con, "SELECT DISTINCT class_code, factor_code FROM coefficients WHERE class_code IS NOT NULL ORDER BY id"):
        lst = cls_factors.setdefault(r["class_code"], [])
        if r["factor_code"] not in lst:
            lst.append(r["factor_code"])
    prods = {r["code"]: {"name": r["name"], "pricing_mode": r["pricing_mode"]}
             for r in rows(con, "SELECT code, name, pricing_mode FROM products")}
    # модель ставки ТЗ 13.10.2026: r_рын НАПП и опыт INSON по классам, настройки rate_model и load (act_settings)
    from . import rate_model
    try:
        rate_inputs = rate_model.reference_inputs(con)
    except Exception as e:                  # рынок и опыт не прочитаны — модель берёт запасной вариант (тариф)
        print("модель ставки: данные рынка не прочитаны:", type(e).__name__)
        rate_inputs = {}
    return Reference(base_rates=base, coefficients=coef, perils=perils, load_share=load,
                     min_rates=min_rates, product_classes=pcs, checklists=checklists,
                     financials=fin[0] if fin else None, measures=measures, market=market_rates(con),
                     class_factors=cls_factors, products=prods, rate_inputs=rate_inputs,
                     rate_settings=rate_model.db_settings(con))


# какая строка отчёта НАПП представляет класс: для 8 и 9 пакет «8,9» показательнее одиночных строк
MARKET_ROW = {"8": "cls8_9", "9": "cls8_9", "7": "cls7", "3": "cls3", "13": "cls13", "14": "cls14",
              "16": "cls16", "1": "cls1", "15": "cls15"}


def market_rates(con) -> dict:
    """Средняя годовая ставка и убыточность по классу на последний срез рыночной статистики."""
    out = {}
    try:
        latest = rows(con, """SELECT m.* FROM market_stats m
                              JOIN (SELECT row_key, MAX(report_date) d FROM market_stats GROUP BY row_key) t
                              ON t.row_key = m.row_key AND t.d = m.report_date""")
    except Exception:
        return out
    by_key = {r["row_key"]: r for r in latest}
    months = {"03-31": 3, "04-01": 3, "07-01": 6, "10-01": 9, "01-01": 12}
    for cls, key in MARKET_ROW.items():
        r = by_key.get(key)
        if not r or not r["premiums_ytd"] or not r["liabilities"]:
            continue
        m = months.get(r["report_date"][5:])
        if not m:
            continue
        year = int(r["report_date"][:4]) - (1 if m == 12 else 0)
        out[cls] = {"row": key, "rate_pct": r["premiums_ytd"] * 12 / m / r["liabilities"] * 100,
                    "loss_ratio": (r["payouts_ytd"] or 0) / r["premiums_ytd"] * 100,
                    "label": f"НАПП, {year} · {m} мес."}
    return out
