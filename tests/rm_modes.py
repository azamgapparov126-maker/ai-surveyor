"""
Два прогона контрольных примеров (13.10.2026): модель ставки ТЗ 13.10.2026 (по умолчанию, rate_model.enabled = true)
и прежний путь (act_settings.rate_model.enabled = false). Старые контрольные числа (6 048 000 / 612 927 966 /
17 496 000 …) остаются в тестах как есть и проверяются прежним путём; новые — «эталон ТЗ 13.10.2026» — в словаре
NEW теста {(функция, подпись проверки): значение} и проверяются моделью ставки.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, rate_model as rm  # noqa: E402

MODE = ["model"]          # model | legacy
CUR = [None]              # текущая проверяемая функция


def set_mode(mode: str) -> None:
    """Переключатель модели ставки для прогона: значение по умолчанию act_settings.rate_model.enabled."""
    MODE[0] = mode
    rm.DEFAULTS["enabled"] = mode == "model"
    db.invalidate_reference()


def legacy() -> bool:
    return MODE[0] == "legacy"


def want(new: dict, what: str, old):
    """Ожидаемое значение: прежний путь — old (как было); модель ставки — new[(функция, what)], если есть."""
    if legacy():
        return old
    return new.get((CUR[0], what), old)


def run_both(funcs: list, title: str = "") -> None:
    """Каждая функция — в обоих режимах: сначала модель ставки (эталон ТЗ 13.10.2026), затем прежний путь."""
    for mode in ("model", "legacy"):
        set_mode(mode)
        print(f"\n===== {title} режим: {'модель ставки ТЗ 13.10.2026' if mode == 'model' else 'прежний путь (rate_model.enabled = false)'}")
        for f in funcs:
            CUR[0] = f.__name__
            f()
    set_mode("model")
