"""
Шрифт для PDF (акт, предложение клиенту, анализ запроса) — единый список кандидатов.

Windows — Arial; сервер (Dockerfile ставит fonts-dejavu-core) — DejaVu Sans, в нём есть кириллица и узбекская ʻ;
Liberation Sans — запасной вариант для Linux без DejaVu. Шрифт годен, если есть оба файла: обычный и жирный.
Нет ни одного — в журнал (логгер surveyor.fonts) пишутся все проверенные пути, адрес PDF отвечает 503.
"""
import logging
from pathlib import Path

log = logging.getLogger("surveyor.fonts")

# (обычный, жирный); порядок = приоритет
FONT_CANDIDATES = (
    (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
)

NO_FONT = "На сервере нет шрифта для PDF"


def find_pair():
    """(обычный, жирный) — пути первого кандидата, у которого есть оба файла; иначе None."""
    for reg, bold in FONT_CANDIDATES:
        if Path(reg).is_file() and Path(bold).is_file():
            return reg, bold
    return None


def find_font(bold: bool = False):
    """Путь к файлу шрифта (обычного или жирного) или None, если на машине нет ни одного кандидата."""
    pair = find_pair()
    return None if pair is None else pair[1 if bold else 0]


def checked_paths() -> list:
    return [p for pair in FONT_CANDIDATES for p in pair]


_logged = set()


def log_missing(where: str, once: bool = False) -> list:
    """Запись в журнал: шрифта нет, какие пути проверены. Возвращает список путей.
    once — писать один раз за процесс (акт зовёт шрифт по нескольку раз на документ)."""
    paths = checked_paths()
    if once and where in _logged:
        return paths
    _logged.add(where)
    log.error("%s (%s). Проверены пути: %s. На Linux: apt-get install fonts-dejavu-core",
              NO_FONT, where, "; ".join(paths))
    return paths


def require_pair(where: str, hint: str = ""):
    """Пути (обычный, жирный) или HTTP 503 «На сервере нет шрифта» с записью проверенных путей в журнал."""
    pair = find_pair()
    if pair is None:
        log_missing(where)
        from fastapi import HTTPException
        raise HTTPException(503, NO_FONT + ". " + (hint + ". " if hint else "")
                            + "Какие пути проверены — в журнале сервера")
    return pair
