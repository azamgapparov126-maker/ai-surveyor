"""
Временная копия базы для тестов, которые пишут данные.

Зачем: тест не должен оставлять строки в рабочей data/surveyor.db — иначе тестовые договоры
попадают в аналитику, портфель и доклад заказчику.

Как пользоваться:

    from tmpdb import temp_db
    with temp_db():
        ...                 # всё, что делает модуль, идёт в копию

Копия удаляется и при падении теста (finally), рабочая база не открывается вообще.
"""
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402


@contextmanager
def temp_db(name: str = "surveyor-test.db"):
    """Подменяет db.DB_PATH копией рабочей базы. Возвращает путь к копии."""
    folder = Path(tempfile.mkdtemp(prefix="surveyor-test-"))
    copy = folder / name
    db.snapshot(db.DB_PATH, copy)       # штатная копия SQLite: учитывает журнал WAL рабочей базы
    original = db.DB_PATH
    db.DB_PATH = copy
    try:
        yield copy
    finally:
        db.DB_PATH = original
        db.close_pool()                 # соединения пула держат файл копии открытым
        shutil.rmtree(folder, ignore_errors=True)


def ensure_market_stats() -> int:
    """
    Рыночная статистика НАПП (market_stats) в текущей базе — для копии на чистой машине: tools/db_build.py её не
    заполняет (в образе это делает tools/market_stats.py, данные — data/parsed в репозитории). Таблица уже
    заполнена — ничего не делает. Возвращает число строк. Вызывать внутри temp_db(): пишет в db.DB_PATH.
    """
    with db.tx() as con:
        try:
            n = con.execute("SELECT COUNT(*) FROM market_stats").fetchone()[0]
        except Exception:
            n = 0
    if n:
        return n
    import tools.market_stats as ms
    ms.DB = None                        # та же база, что db.DB_PATH (копия)
    ms.build()
    with db.tx() as con:
        return con.execute("SELECT COUNT(*) FROM market_stats").fetchone()[0]


STAT_FIXTURE = ROOT / "tests" / "fixtures" / "stat_series.json.gz"


def ensure_stat_series() -> int:
    """
    Открытые данные stat.uz (stat_series) в текущей базе — для копии на чистой машине: сервер скачивает их сам, а
    tools/db_build.py — нет. Берутся из tests/fixtures/stat_series.json.gz (снимок рабочей базы, без ПД). Таблица
    уже заполнена — ничего не делает. Возвращает число строк. Вызывать внутри temp_db().
    """
    import gzip
    import json
    with db.tx() as con:
        n = con.execute("SELECT COUNT(*) FROM stat_series").fetchone()[0]
        if n:
            return n
        data = json.loads(gzip.decompress(STAT_FIXTURE.read_bytes()).decode("utf-8"))
        for g in data["groups"]:
            con.executemany("INSERT OR IGNORE INTO stat_series (source, dataset_id, key, region, period, value, unit, "
                            "fetched_at, url) VALUES (?,?,?,?,?,?,?,?,?)",
                            [(g["source"], g["dataset_id"], g["key"], r[0], r[1], r[2], g["unit"], g["fetched_at"],
                              g["url"]) for r in g["rows"]])
        return con.execute("SELECT COUNT(*) FROM stat_series").fetchone()[0]
