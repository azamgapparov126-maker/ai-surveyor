"""
Шаблоны анализа по классам (ТЗ — Приложение А, версия 1.3 от 01.10.2026) — справочник, а не код.

Состав (файл 1.3.0): все классы общего страхования — 18 классов (1–18) и варианты 13з и 16у (свои полноценные
шаблоны, раньше — ссылки на 14 и 16), всего 20. Классов страхования жизни в шаблонах нет (указание заказчика
01.10.2026: нужны все классы общего страхования); строки L*, если они вдруг есть в таблицах classes или
class_templates, игнорируются и не показываются. Класс 18 «Tibbiy sugʻurta» (медицинское страхование, классификатор
страховой деятельности: ПКМ № 80 от 21.02.2022, прил. 6, п. 12, ред. ПКМ № 458 от 23.07.2025) заводится и в таблицу
classes (branch = 'общее', kind = 'личное', учётная группа NULL: в Положении 1882, п. 10 класса 18 нет):
ensure_classes, вызывается из ensure (старт сервера через refsync, tools/db_build.py).

Источник истины в репозитории — docs/act_class_templates.json (версия и дата файла). В базе — таблица
class_templates: одна строка на версию шаблона класса (class_code, version, json, updated_at, updated_by,
calibrated = 0). Действует строка с наибольшей версией класса.

  * ensure(con) — при старте сервера (app/refsync.sync_on_start) и при первом обращении: создаёт таблицу, если
    её нет, и добавляет шаблон класса из файла, если в базе его нет или версия файла новее последней версии в базе.
    Старые версии не удаляются (история).
  * правка администратора (PUT /act/templates/{class}, app/act.py) — новая строка с версией «+0,1» к последней;
    файл её не затирает: пока действует правка администратора, новая версия файла добавляется в историю строкой
    source = 'file_pending' («доступна новая версия шаблона из поставки», pending_file_version в
    GET /act/templates/{class}), действующей остаётся правка. Версии «1.2» и «1.2.0» равны (ver()).
  * 13з и 16у — свои шаблоны; LEGACY_ALIASES (13з → 14, 16у → 16) — только запасной путь, если шаблона варианта
    нет ни в базе, ни в файле (старый файл шаблонов).

Шаблон класса — восемь полей приложения А плюс то, что нужно акту: object (описание, kinds, people), must (не
больше четырёх полей сверх общих), optional, valuation_methods, risks (source = perils у классов 8 и 9 — доли из
таблицы perils базы; source = template — экспертные доли, сумма 100), factors, scenario_rule, documents, stats,
required_views / clauses / measures (по группе объекта акта или default), notes. Подписи — ru/uz/en в самом JSON.
Все доли, веса и параметры — экспертные (calibrated = 0), пока нет статистики убытков компании.

Файл 1.4.0 (02.10.2026): у каждого шаблона — factor_groups, подгруппы и факторы повышения и понижения тарифа по
документу «Факторы тарифа по классам и подгруппам»: группа {code, label, input, options: [{code, label, coef, note}]}.
Значение группы вводится полем шаблона (input: optional.class_fields.<код> или прежнее уточнение акта —
optional.location, optional.protection, optional.construction): новые поля выбора — в optional, уже существующие
(место хранения, защита, конструкция, вид транспорта груза, вид деятельности, территория 18…) — не дублируются, группа
ссылается на них; варианты группы = options поля (check_factor_groups).
Файл 1.4.1 (02.10.2026): у групп класса 8 «конструкция», «особенности конструкции» и «отопление» — stat_ref, ссылки на
открытые наборы stat.uz (материал стен жилищного фонда, обеспеченность газом); проверка — check_stat_ref. Это фон
региона в акте, коэффициент варианта не меняется (act_analytics.factor_stats).
Коэффициенты экспертные (calibrated = 0); как они влияют на ставку — act_engine.factor_adjust.

Модуль без HTTP и без сети: чтение файла, таблица, проверка структуры, выдача на языке.
"""
import json
import threading
from pathlib import Path
from typing import Optional

from . import db

CALIBRATED = 0
TEMPLATES_FILE = db.ROOT / "docs" / "act_class_templates.json"
CLAUSES_FILE = db.ROOT / "docs" / "act_clauses.json"
MEASURES_FILE = db.ROOT / "docs" / "act_measures.json"
LANGS = ("ru", "uz", "en")
GENERAL_CODES = tuple(str(i) for i in range(1, 19))          # общее страхование, классы 1–18
# классы общего страхования, которых нет в Положении 1882, п. 10 (учётная группа РНП не задана — NULL); их строки
# в справочнике classes заводит ensure_classes из файла шаблонов: 18 — «Tibbiy sugʻurta» (ПКМ № 80, прил. 6)
GENERAL_NO_GROUP_CODES = ("18",)
VARIANT_CODES = ("13з", "16у")                                 # варианты классов 13 и 16 (Положение 1882, п. 10)
CLASS_CODES = GENERAL_CODES + VARIANT_CODES
# строка file_pending: версия файла, пришедшая при действующей правке администратора (в истории, не действует)
PENDING = "file_pending"
# запасной путь для старого файла шаблонов (до 1.1.0): шаблона варианта нет — берётся шаблон основного класса
LEGACY_ALIASES = {"13з": "14", "16у": "16"}
MAX_MUST = 4
SHARE_TOLERANCE = 0.5
FIELD_TYPES = ("int", "number", "money", "text", "bool", "choice", "year", "kind", "losses", "deductible")
# crop — стоимость урожая: площадь × средняя урожайность за 5 лет × цена единицы продукции (вариант 16у)
VALUATION_METHODS = (1, 2, 3, 4, 5, 6, "agreed", "crop")
# правило сценария: engine = risk_analytics — считает модуль аналитики рисков (классы 3, 8, 9); template — простое
# экспертное правило act_extras.simple_scenarios
ENGINE_RULES = ("vehicle", "property8", "property9")
TEMPLATE_RULES = ("people", "frequency", "unit", "full_loss", "shipment", "limit", "full_limit", "full_sum",
                  "credit", "bi", "dispute", "crop")
SCENARIO_RULES = ENGINE_RULES + TEMPLATE_RULES
SCENARIOS = ("PML", "EML", "MFL")
DIRECTIONS = ("up", "down", "both")
# группы факторов тарифа (factor_groups, файл 1.4.0 от 02.10.2026, документ «Факторы тарифа по классам и
# подгруппам»): коэффициент варианта — в границах FACTOR_COEF_BOUNDS, у группы не меньше FACTOR_MIN_OPTIONS вариантов.
# Коэффициенты экспертные (calibrated = 0); границы — защита от опечатки администратора, а не тариф
FACTOR_COEF_BOUNDS = (0.5, 3.0)
FACTOR_MIN_OPTIONS = 2
# поле «да/нет» в группе факторов: варианты группы — yes и no
FACTOR_BOOL_OPTIONS = ("yes", "no")
# пределы правки администратора (PUT /act/templates/{class}): размер JSON шаблона и длина одной подписи
MAX_TEMPLATE_BYTES = 200 * 1024
MAX_LABEL_CHARS = 500
# правило проекта № 6 (CLAUDE.md): кредит страхуется не более чем на 50 % суммы кредита
CREDIT_MAX_SHARE = 0.5
WHO_FILE = "система: шаблоны классов"

SCHEMA_SQL = [
    """CREATE TABLE IF NOT EXISTS class_templates (
        id           INTEGER PRIMARY KEY,
        class_code   TEXT NOT NULL,
        version      TEXT NOT NULL,
        json         TEXT NOT NULL,
        source       TEXT NOT NULL DEFAULT 'file',
        file_version TEXT,
        updated_at   TEXT NOT NULL,
        updated_by   TEXT,
        calibrated   INTEGER NOT NULL DEFAULT 0,
        note         TEXT,
        UNIQUE (class_code, version)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_class_templates_class ON class_templates (class_code, id)",
]

_file_cache = {"mtime": None, "data": None}
_cat_cache = {}
_ensured = set()                          # (файл базы, mtime файла шаблонов): ensure уже прошёл
_lock = threading.Lock()


# ================================================================================================
#  Файлы
# ================================================================================================

def _read_json(path: Path, cache: dict) -> dict:
    m = path.stat().st_mtime
    if cache.get("mtime") != m:
        cache["data"] = json.loads(path.read_text(encoding="utf-8"))
        cache["mtime"] = m
    return cache["data"]


def load_file() -> dict:
    """docs/act_class_templates.json; перечитывается, только если файл изменился."""
    return _read_json(TEMPLATES_FILE, _file_cache)


def _catalog(path: Path) -> dict:
    try:
        return _read_json(path, _cat_cache.setdefault(str(path), {}))
    except (OSError, ValueError):
        return {}


def clause_codes() -> set:
    cat = _catalog(CLAUSES_FILE)
    return {c.get("code") for g in (cat.get("groups") or {}).values() for c in g if isinstance(c, dict)}


def measure_codes(con=None) -> set:
    """Коды мероприятий: экспертный список, переводы таблицы preventive_measures и сама таблица (если есть база)."""
    cat = _catalog(MEASURES_FILE)
    out = {c.get("code") for c in cat.get("catalog") or []} | set(cat.get("translations") or {})
    if con is not None:
        try:
            out |= {r[0] for r in con.execute("SELECT code FROM preventive_measures")}
        except Exception:
            pass
    return out


def aliases() -> dict:
    try:
        return dict(load_file().get("aliases") or {})
    except (OSError, ValueError):
        return {}


def base_class(cls: Optional[str]) -> Optional[str]:
    """Класс, чей шаблон берётся: свой (13з и 16у — тоже свои с файла 1.1.0); ссылка aliases файла — только если
    она там задана. Запасной путь старого файла (13з → 14) — в current()."""
    c = str(cls or "").strip()
    return aliases().get(c, c) or None


def is_life_code(code) -> bool:
    """Код класса страхования жизни (L1…): таких шаблонов нет — строки игнорируются и не показываются."""
    return str(code or "").strip().upper().startswith("L")


def general_extra_class_rows(data: Optional[dict] = None) -> list:
    """Строки classes для классов общего страхования без учётной группы в Положении 1882 (класс 18):
    (code, name, NULL, 'общее', 'личное'). Учётная группа — решение страховщика, до него NULL."""
    try:
        data = data or load_file()
    except (OSError, ValueError):
        return []
    out = []
    for code in GENERAL_NO_GROUP_CODES:
        tpl = (data.get("classes") or {}).get(code)
        if tpl:
            out.append((code, tpl["name"]["ru"], tpl.get("accounting_group"), "общее", "личное"))
    return out


def ensure_classes(con, data: Optional[dict] = None) -> list:
    """
    Класс 18 общего страхования в таблице classes (правило проекта № 3: справочник полный, ничего не исключаем):
    недостающий добавляется, имеющийся не меняется. Колонка group_code ещё NOT NULL (база до миграции
    app/db.py._classes_group_nullable) — ничего не делает. Возвращает коды добавленных классов.
    """
    try:
        info = con.execute("PRAGMA table_info(classes)").fetchall()
    except Exception:
        return []
    cols = {r[1]: r[3] for r in info}
    if not cols or cols.get("group_code"):
        return []
    have = {r[0] for r in con.execute("SELECT code FROM classes")}
    added = []
    for row in general_extra_class_rows(data):
        if row[0] in have:
            continue
        con.execute("INSERT INTO classes (code, name, group_code, branch, kind) VALUES (?,?,?,?,?)", row)
        added.append(row[0])
    if added:
        db.audit(con, WHO_FILE, "классы из файла шаблонов добавлены в справочник", "classes",
                 {"classes": added, "источник": "ПКМ № 80 от 21.02.2022, прил. 6, п. 12 (ред. ПКМ № 458 от 23.07.2025)",
                  "учётная группа": "NULL — класса 18 нет в Положении 1882, п. 10"})
        db.invalidate_reference()
    return added


# ================================================================================================
#  Версии
# ================================================================================================

def ver(v) -> tuple:
    """«1.0» → (1, 0); «1.2» и «1.2.0» — одна версия (нули в конце отбрасываются); неразборчивое — (0,)."""
    try:
        t = [int(x) for x in str(v).strip().split(".")]
    except (TypeError, ValueError):
        return (0,)
    while len(t) > 1 and t[-1] == 0:
        t.pop()
    return tuple(t)


def bump(v) -> str:
    """Следующая версия правки администратора: 1.0 → 1.1, 1.9 → 1.10."""
    t = list(ver(v)) + [0]
    major, minor = t[0], t[1]
    return f"{major}.{minor + 1}"


# ================================================================================================
#  Таблица
# ================================================================================================

def ensure_table(con) -> None:
    for sql in SCHEMA_SQL:
        con.execute(sql)


def _db_key(con) -> str:
    try:
        return con.execute("PRAGMA database_list").fetchone()[2] or ""
    except Exception:
        return ""


def ensure(con, force: bool = False) -> dict:
    """
    Таблица есть и шаблоны файла доведены. Версия файла для класса ещё не загружалась:
      * строк класса нет или действует строка файла с меньшей версией — новая строка source = file (действует);
      * действует правка администратора (source = admin) — файл её молча не заменяет: строка source = file_pending
        уходит в историю с пометкой «доступна новая версия шаблона из поставки», действующей остаётся правка;
      * действует строка файла с той же или большей версией — ничего.
    Версии сравниваются ver(): «1.2» = «1.2.0». Строки классов жизни (L*) не трогаются и не показываются.
    Возвращает {"added": [...], "pending": [...]}.
    """
    try:
        data = load_file()
        mtime = TEMPLATES_FILE.stat().st_mtime
    except (OSError, ValueError) as e:
        print("шаблоны классов: файл не прочитан:", type(e).__name__)
        return {"added": [], "error": type(e).__name__}
    key = (_db_key(con), mtime)
    if not force and key in _ensured:
        return {"added": [], "cached": True}
    with _lock:
        ensure_table(con)
        fv = str(data.get("version") or "1.0")
        added, pending = [], []
        rows = {}
        for r in con.execute("SELECT id, class_code, version, source FROM class_templates"):
            rows.setdefault(r[1], []).append({"id": r[0], "version": r[2], "source": r[3]})
        last = {}
        for cls, tpl in (data.get("classes") or {}).items():
            if is_life_code(cls):
                continue
            have = rows.get(cls) or []
            if any(x["source"] in ("file", PENDING) and ver(x["version"]) == ver(fv) for x in have):
                continue                       # эта версия файла уже загружена (действует или ждёт в истории)
            act = _active(have)
            last[cls] = act["version"] if act else None
            if act and act["source"] == "admin":
                src, note = PENDING, (f"доступна новая версия шаблона из поставки: docs/act_class_templates.json {fv} "
                                      f"от {data.get('date')}; действует правка администратора {act['version']}")
            elif act and ver(fv) <= ver(act["version"]):
                continue
            else:
                src, note = "file", f"docs/act_class_templates.json {fv} от {data.get('date')}"
            con.execute("INSERT INTO class_templates (class_code, version, json, source, file_version, updated_at, "
                        "updated_by, calibrated, note) VALUES (?,?,?,?,?,?,?,?,?)",
                        (cls, fv, json.dumps(tpl, ensure_ascii=False), src, fv, db.now(), WHO_FILE, CALIBRATED, note))
            (pending if src == PENDING else added).append(cls)
        if added:
            db.audit(con, WHO_FILE, "шаблоны классов доведены из файла", "class_templates",
                     {"version": fv, "date": data.get("date"), "classes": added,
                      "было": {c: last.get(c) for c in added}})
        if pending:
            db.audit(con, WHO_FILE, "доступна новая версия шаблона из поставки", "class_templates",
                     {"version": fv, "date": data.get("date"), "classes": pending,
                      "действует правка администратора": {c: last.get(c) for c in pending}})
        try:
            cls_added = ensure_classes(con, data)
        except Exception as e:             # справочник классов не довели — шаблоны всё равно работают
            print("шаблоны классов: класс 18 не добавлен в classes:", type(e).__name__)
            cls_added = []
        _ensured.add(key)
    return {"added": added, "pending": pending, "version": fv, "classes_added": cls_added}


def _active(rows: list) -> Optional[dict]:
    """Действующая строка класса: наибольшая версия среди строк файла и правок администратора (строки file_pending
    — только история); при равных версиях — более поздняя строка."""
    live = [r for r in rows if r.get("source") != PENDING]
    return max(live, key=lambda x: (ver(x["version"]), x["id"])) if live else None


def reset_cache() -> None:
    """Для тестов: следующий вызов ensure снова сверит базу с файлом."""
    _ensured.clear()


def _row(r) -> dict:
    return {"id": r[0], "class_code": r[1], "version": r[2], "template": json.loads(r[3]), "source": r[4],
            "file_version": r[5], "updated_at": r[6], "updated_by": r[7], "calibrated": r[8], "note": r[9]}


COLS = "id, class_code, version, json, source, file_version, updated_at, updated_by, calibrated, note"


def current(con, cls: Optional[str]) -> Optional[dict]:
    """
    Действующий шаблон класса (с учётом 13з → 14, 16у → 16): строка базы с наибольшей версией. Таблицы или строки
    нет и довести не удалось (база только для чтения) — шаблон из файла с пометкой source = file_only.
    """
    b = base_class(cls)
    if not b or is_life_code(b):           # классов жизни в шаблонах нет; строки L* в таблице не показываются
        return None
    try:
        ensure(con)
        rows = [_row(r) for r in con.execute(f"SELECT {COLS} FROM class_templates WHERE class_code=?", (b,))]
    except Exception as e:                 # только чтение или сбой таблицы — берём файл
        print("шаблоны классов: таблица не прочитана:", type(e).__name__)
        rows = []
    out = _active(rows)
    if out is not None:
        # новая версия файла ждёт в истории (действует правка администратора) — pending_file_version
        newer = [r for r in rows if r["source"] == PENDING and r["id"] > out["id"]]
        out = dict(out, pending_file_version=max(newer, key=lambda x: x["id"])["version"] if newer else None)
    else:
        try:
            tpl = (load_file().get("classes") or {}).get(b)
        except (OSError, ValueError):
            tpl = None
        if not tpl:
            alt = LEGACY_ALIASES.get(b)
            if alt and alt != b:          # старый файл без шаблона варианта: 13з → 14, 16у → 16
                out = current(con, alt)
                if out:
                    out["requested_class"] = str(cls)
                    out["alias_of"] = alt
                return out
            return None
        out = {"id": None, "class_code": b, "version": str(load_file().get("version")), "template": tpl,
               "source": "file_only", "file_version": str(load_file().get("version")), "updated_at": None,
               "updated_by": None, "calibrated": CALIBRATED, "note": None, "pending_file_version": None}
    out["requested_class"] = str(cls)
    out["alias_of"] = b if b != str(cls) else None
    out["template"] = with_catalog(con, out.get("class_code") or b, out.get("template"))
    return out


# Единый справочник факторов (задача 4.1 ТЗ 13.10.2026): группы шаблона (factor_groups) + коэффициенты класса из
# таблицы coefficients без дублей — rate_model.factor_catalog. Собирается при каждом чтении шаблона и лежит в
# копии шаблона под этим ключом; в базу шаблонов не пишется (save и правка администратора его отбрасывают).
CATALOG_KEY = "factor_catalog"


def with_catalog(con, cls: str, tpl: Optional[dict]) -> Optional[dict]:
    """Копия шаблона с единым справочником факторов класса (factor_catalog). Сбой чтения coefficients — без него."""
    if not isinstance(tpl, dict):
        return tpl
    from . import rate_model
    try:
        cur = con.execute("SELECT factor_code, factor_name, class_code, option_code, option_name, multiplier, "
                          "calibrated FROM coefficients WHERE class_code=? ORDER BY id", (str(cls),))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        st = rate_model.db_settings(con)
    except Exception:
        return {k: v for k, v in tpl.items() if k != CATALOG_KEY}
    groups = factor_groups(tpl)
    return dict(tpl, **{CATALOG_KEY: rate_model.factor_catalog(groups, rows, str(cls), st)})


def all_current(con) -> list:
    """Все действующие шаблоны: классы общего страхования 1–18 и варианты 13з и 16у (каждый — свой, без ссылок)."""
    return [x for x in (current(con, c) for c in CLASS_CODES) if x and x.get("alias_of") is None]


def products_count(con, cls: str) -> Optional[int]:
    """Сколько продуктов страховщика относится к классу (product_classes); база не прочитана — None."""
    try:
        return con.execute("SELECT COUNT(DISTINCT product_code) FROM product_classes WHERE class_code=?",
                           (str(cls),)).fetchone()[0]
    except Exception:
        return None


NO_PRODUCTS_NOTE = {"ru": "У страховщика нет продуктов этого класса: акт формируется по шаблону, ставка не определена",
                    "uz": "Sugʻurtalovchida bu klass mahsulotlari yoʻq: dalolatnoma shablon boʻyicha tuziladi, tarif "
                          "aniqlanmagan",
                    "en": "The insurer has no products in this class: the report follows the template, the rate is "
                          "not determined"}


def history(con, cls: str) -> list:
    b = base_class(cls)
    if not b or is_life_code(b):
        return []
    ensure(con)
    rows = [_row(r) for r in con.execute(f"SELECT {COLS} FROM class_templates WHERE class_code=? ORDER BY id", (b,))]
    for r in rows:
        r.pop("template", None)
        if r["source"] == PENDING:
            r["pending"] = True
            r["pending_note"] = "доступна новая версия шаблона из поставки"
    return rows


def save(con, cls: str, tpl: dict, who: str, note: str = "") -> dict:
    """Новая версия шаблона класса (правка администратора). Структура уже проверена validate()."""
    b = base_class(cls)
    ensure(con)
    cur = current(con, b)
    v = bump(cur["version"] if cur else "1.0")
    while con.execute("SELECT 1 FROM class_templates WHERE class_code=? AND version=?", (b, v)).fetchone():
        v = bump(v)
    tpl = {k: v for k, v in dict(tpl, class_code=b, calibrated=CALIBRATED).items() if k != CATALOG_KEY}
    rid = con.execute("INSERT INTO class_templates (class_code, version, json, source, file_version, updated_at, "
                      "updated_by, calibrated, note) VALUES (?,?,?,?,?,?,?,?,?)",
                      (b, v, json.dumps(tpl, ensure_ascii=False), "admin", (cur or {}).get("file_version"),
                       db.now(), who, CALIBRATED, str(note or "")[:300])).lastrowid
    db.audit(con, who, "шаблон класса изменён", f"class_templates:{b}",
             {"version": v, "было": (cur or {}).get("version"), "id": rid})
    return current(con, b)


# ================================================================================================
#  Проверка структуры
# ================================================================================================

def _has_ru(x) -> bool:
    return isinstance(x, dict) and isinstance(x.get("ru"), str) and bool(x["ru"].strip())


def _codes_by_group(d, name: str, known: Optional[set], errs: list) -> None:
    if not isinstance(d, dict) or not d:
        errs.append(f"{name}: словарь {{группа объекта | default: [коды]}}")
        return
    for g, codes in d.items():
        if not isinstance(codes, list) or not all(isinstance(c, str) for c in codes):
            errs.append(f"{name}.{g}: список кодов")
            continue
        if known is not None:
            bad = [c for c in codes if c not in known]
            if bad:
                errs.append(f"{name}.{g}: нет таких кодов — " + ", ".join(bad))


def _walk_labels(obj, path: str, out: list) -> None:
    """Все подписи шаблона (словари, ключи которых — только ru/uz/en) с путём до них."""
    if isinstance(obj, dict):
        if obj and set(obj) <= set(LANGS):
            out.append((path or "шаблон", obj))
            return
        for k, v in obj.items():
            _walk_labels(v, f"{path}.{k}" if path else str(k), out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            key = v.get("code") if isinstance(v, dict) and isinstance(v.get("code"), str) else str(i)
            _walk_labels(v, f"{path}.{key}", out)


def _num_ok(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v \
        and v not in (float("inf"), float("-inf"))


def check_params(code: str, params) -> list:
    """
    Параметры правила сценария: только числа в разумных границах. Доли (в имени share или ratio) — (0; 1],
    проценты (_pct) — [0; 100], прочие (суммы, месяцы) — больше нуля. У правила credit доля кредита
    max_share_of_loan — больше 0 и не больше 0,5 (правило проекта № 6: не более 50 % суммы кредита).
    """
    if params is None:
        return []
    if not isinstance(params, dict):
        return ["scenario_rule.params: словарь {параметр: число}"]
    errs = []
    for k, v in params.items():
        name = f"scenario_rule.params.{k}"
        if not _num_ok(v):
            errs.append(f"{name}: нужно число, передано {json.dumps(v, ensure_ascii=False)[:40]}")
            continue
        v = float(v)
        if code == "credit" and k == "max_share_of_loan":
            if not 0 < v <= CREDIT_MAX_SHARE:
                errs.append(f"{name}: доля кредита больше 0 и не больше 0,5 — страхуется только необеспеченная "
                            f"часть и не более 50 % суммы кредита (правило проекта № 6), передано {v:g}")
        elif "share" in k or "ratio" in k:
            if not 0 < v <= 1:
                errs.append(f"{name}: доля больше 0 и не больше 1, передано {v:g}")
        elif k.endswith("_pct"):
            if not 0 <= v <= 100:
                errs.append(f"{name}: процент от 0 до 100, передано {v:g}")
        elif v <= 0:
            errs.append(f"{name}: число больше нуля, передано {v:g}")
    return errs


def _catalog_labels() -> dict:
    """Подписи кодов, на которые ссылается шаблон: {"views" | "clauses" | "measures": {код: {язык: текст}}}."""
    from . import act_texts as tx
    views = {k: dict(v) for k, v in tx.VIEW_LABELS.items()}
    cl = {}
    for g in (_catalog(CLAUSES_FILE).get("groups") or {}).values():
        for c in g:
            if isinstance(c, dict) and c.get("code"):
                cl[c["code"]] = {lg: c.get(lg) for lg in LANGS if isinstance(c.get(lg), str)}
    ms = {}
    cat = _catalog(MEASURES_FILE)
    for c in cat.get("catalog") or []:
        if isinstance(c, dict) and c.get("code"):
            ms[c["code"]] = {lg: (c.get(lg) or {}).get("text") for lg in LANGS if isinstance(c.get(lg), dict)}
    for code, tr in (cat.get("translations") or {}).items():
        d = ms.setdefault(code, {})
        for lg in LANGS:
            if isinstance((tr or {}).get(lg), dict) and (tr[lg].get("text") or "").strip():
                d.setdefault(lg, tr[lg]["text"])
    return {"views": views, "clauses": cl, "measures": ms}


def check_labels(tpl: dict) -> list:
    """
    Подписи шаблона: у каждой есть ru (непустая строка) и ни одна не длиннее 500 знаков; у названия класса, полей
    must/optional и рисков — ещё uz и en; у ракурсов, оговорок и мероприятий, на которые ссылается шаблон, —
    перевод uz и en в своём справочнике. Возвращает ошибки с перечнем того, чего не хватает.
    """
    errs, found = [], []
    _walk_labels(tpl, "", found)
    no_ru, too_long, bad_type = [], [], []
    for path, lab in found:
        if not isinstance(lab.get("ru"), str) or not lab["ru"].strip():
            no_ru.append(path)
        for lg, txt in lab.items():
            if not isinstance(txt, str):
                bad_type.append(f"{path}.{lg}")
            elif len(txt) > MAX_LABEL_CHARS:
                too_long.append(f"{path}.{lg} ({len(txt)})")
    if no_ru:
        errs.append("подпись без ru: " + ", ".join(no_ru[:30]) + (" …" if len(no_ru) > 30 else ""))
    if bad_type:
        errs.append("подпись — строка: " + ", ".join(bad_type[:30]))
    if too_long:
        errs.append(f"подпись длиннее {MAX_LABEL_CHARS} знаков: " + ", ".join(too_long[:30]))
    need = [("name", tpl.get("name"))]
    for key in ("must", "optional"):
        for f in tpl.get(key) or []:
            if isinstance(f, dict):
                need.append((f"{key}.{f.get('code')}", f.get("label")))
    for r in ((tpl.get("risks") or {}).get("items") or []) if isinstance(tpl.get("risks"), dict) else []:
        if isinstance(r, dict):
            need.append((f"risks.{r.get('code')}", r.get("label")))
    # группы факторов тарифа и их варианты — на трёх языках (пояснение note — только ru)
    for g in tpl.get("factor_groups") or [] if isinstance(tpl.get("factor_groups"), list) else []:
        if isinstance(g, dict):
            need.append((f"factor_groups.{g.get('code')}", g.get("label")))
            for o in g.get("options") or [] if isinstance(g.get("options"), list) else []:
                if isinstance(o, dict):
                    need.append((f"factor_groups.{g.get('code')}.{o.get('code')}", o.get("label")))
    missing = []
    for path, lab in need:
        if not isinstance(lab, dict):
            continue                      # нет самой подписи — это ошибка структуры, её пишет validate
        for lg in ("uz", "en"):
            if not isinstance(lab.get(lg), str) or not lab[lg].strip():
                missing.append(f"{path}.{lg}")
    cats = _catalog_labels()
    for key, cat_key, what in (("required_views", "views", "ракурс"), ("clauses", "clauses", "оговорка"),
                               ("measures", "measures", "мероприятие")):
        d = tpl.get(key)
        if not isinstance(d, dict):
            continue
        codes = []
        for lst in d.values():
            if isinstance(lst, list):
                codes += [c for c in lst if isinstance(c, str) and c not in codes]
        for c in codes:
            lab = cats[cat_key].get(c)
            if lab is None:
                if cat_key == "measures":
                    missing.append(f"{what} {c}.uz/en")   # мероприятие только из таблицы базы — перевода нет
                continue                  # неизвестный ракурс или оговорка — ошибка структуры (_codes_by_group)
            for lg in ("uz", "en"):
                if not isinstance(lab.get(lg), str) or not lab[lg].strip():
                    missing.append(f"{what} {c}.{lg}")
    if missing:
        errs.append("нет перевода uz/en: " + ", ".join(missing[:40]) + (" …" if len(missing) > 40 else ""))
    return errs


def check_factor_groups(tpl: dict) -> list:
    """
    Группы факторов тарифа (factor_groups): [{code, label {ru, uz, en}, input, options: [{code, label, coef, note}]}].
    Коды групп уникальны, коды вариантов уникальны в группе; у группы не меньше двух вариантов; коэффициент — число
    от 0,5 до 3; поле ввода группы (input) есть в шаблоне (optional, а если поле уже обязательное — must, без
    повтора): поле выбора — варианты группы совпадают с его options, поле «да/нет» — варианты yes и no. Нет блока —
    ошибок нет (шаблон старой версии, правка администратора).
    """
    fg = tpl.get("factor_groups")
    if fg is None:
        return []
    if not isinstance(fg, list):
        return ["factor_groups: список групп {code, label, input, options}"]
    errs, seen = [], set()
    lo, hi = FACTOR_COEF_BOUNDS
    fields = {f.get("input"): f for f in list(tpl.get("must") or []) + list(tpl.get("optional") or [])
              if isinstance(f, dict) and f.get("input")}
    for g in fg:
        if not isinstance(g, dict) or not isinstance(g.get("code"), str) or not g["code"] or not _has_ru(g.get("label")):
            errs.append("factor_groups: группа {code, label: {ru, uz, en}, input, options}")
            continue
        name = f"factor_groups.{g['code']}"
        if g["code"] in seen:
            errs.append(f"factor_groups: повтор группы {g['code']}")
        seen.add(g["code"])
        opts = g.get("options")
        if not isinstance(opts, list) or len(opts) < FACTOR_MIN_OPTIONS:
            errs.append(f"{name}.options: не меньше {FACTOR_MIN_OPTIONS} вариантов")
            continue
        codes = []
        for o in opts:
            if not isinstance(o, dict) or not isinstance(o.get("code"), str) or not o["code"]                     or not _has_ru(o.get("label")):
                errs.append(f"{name}.options: вариант {{code, label: {{ru, uz, en}}, coef, note}}")
                break
            oc = o["code"]
            if oc in codes:
                errs.append(f"{name}.options: повтор варианта {oc}")
            codes.append(oc)
            c = o.get("coef")
            if not _num_ok(c) or not lo <= float(c) <= hi:
                errs.append(f"{name}.{oc}.coef: число от {lo:g} до {hi:g}, передано "
                            f"{json.dumps(c, ensure_ascii=False)[:40]}")
            if o.get("note") is not None and not _has_ru(o.get("note")):
                errs.append(f"{name}.{oc}.note: подпись ru")
        inp = g.get("input")
        f = fields.get(inp) if isinstance(inp, str) else None
        if f is None:
            errs.append(f"{name}.input: в шаблоне (must, optional) нет поля с вводом {inp}")
            continue
        if f.get("type") == "bool":
            want = list(FACTOR_BOOL_OPTIONS)
        elif f.get("type") == "choice":
            want = [str(x) for x in f.get("options") or []]
        else:
            errs.append(f"{name}.input: поле {f.get('code')} — выбор (choice) или да/нет (bool), а не {f.get('type')}")
            continue
        if sorted(codes) != sorted(want):
            errs.append(f"{name}.options: варианты группы не совпадают с вариантами поля {f.get('code')} — "
                        f"в группе {', '.join(codes)}; в поле {', '.join(want)}")
        errs += check_stat_ref(g, codes)
    return errs


def check_stat_ref(g: dict, codes: list) -> list:
    """
    Ссылки группы факторов на открытые наборы stat.uz (stat_ref, файл 1.4.1): [{dataset_id, option_codes, label
    {ru, uz, en}}]. dataset_id — ключ реестра app/stat_sources.DATASETS, option_codes — непустой список вариантов
    этой группы. Набор — фон региона в акте, коэффициент варианта не меняет. Нет поля — ошибок нет.
    """
    refs = g.get("stat_ref")
    if refs is None:
        return []
    name = f"factor_groups.{g.get('code')}.stat_ref"
    if not isinstance(refs, list) or not refs:
        return [f"{name}: список {{dataset_id, option_codes, label}}"]
    from . import stat_sources as ss        # реестр наборов — только словарь, без обращения к сети
    errs = []
    for i, r in enumerate(refs):
        if not isinstance(r, dict):
            errs.append(f"{name}[{i}]: {{dataset_id, option_codes, label}}")
            continue
        ds = r.get("dataset_id")
        if not isinstance(ds, str) or ds not in ss.DATASETS:
            errs.append(f"{name}[{i}].dataset_id: набора {json.dumps(ds, ensure_ascii=False)[:60]} нет в реестре "
                        f"stat_sources.DATASETS")
        oc = r.get("option_codes")
        if not isinstance(oc, list) or not oc:
            errs.append(f"{name}[{i}].option_codes: непустой список вариантов группы")
        else:
            bad = [str(x) for x in oc if x not in codes]
            if bad:
                errs.append(f"{name}[{i}].option_codes: в группе нет вариантов {', '.join(bad)}")
        if not _has_ru(r.get("label")):
            errs.append(f"{name}[{i}].label: подпись ru")
    return errs


def validate(tpl, cls: str, con=None) -> list:
    """
    Ошибки структуры шаблона (пустой список — всё верно). Проверяется то, на что опирается акт, плюс пределы
    правки администратора: JSON не больше 200 КБ, подписи до 500 знаков, ru везде, uz и en — у названия, полей,
    рисков, ракурсов, оговорок и мероприятий; параметры правила сценария — числа в границах (check_params).
    """
    from . import act_engine as ae
    from . import risk_stats as rs
    errs = []
    if not isinstance(tpl, dict):
        return ["шаблон — объект JSON"]
    b = base_class(cls)
    if b not in CLASS_CODES:
        return [f"нет шаблона для класса {cls}"]
    try:
        size = len(json.dumps(tpl, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return ["шаблон — JSON из строк, чисел, списков и словарей"]
    if size > MAX_TEMPLATE_BYTES:
        return [f"шаблон больше {MAX_TEMPLATE_BYTES // 1024} КБ ({round(size / 1024)} КБ)"]
    if tpl.get("class_code") not in (None, b):
        errs.append(f"class_code: {tpl.get('class_code')} не совпадает с классом {b}")
    # варианты 13з и 16у — variant: true и variant_of 13 / 16
    if b in VARIANT_CODES and (tpl.get("variant") is not True or tpl.get("variant_of") != b[:-1]):
        errs.append(f"variant: вариант {b} — variant: true, variant_of: {b[:-1]}")
    for key in ("name", "object", "must", "optional", "valuation_methods", "risks", "factors", "scenario_rule",
                "documents", "stats", "required_views", "clauses", "measures", "notes"):
        if key not in tpl:
            errs.append(f"нет поля {key}")
    if errs:
        return errs
    if not _has_ru(tpl["name"]):
        errs.append("name: подпись ru/uz/en")
    obj = tpl["object"]
    if not isinstance(obj, dict) or not _has_ru(obj.get("description")) or not isinstance(obj.get("kinds"), list):
        errs.append("object: description (ru/uz/en) и kinds[]")
    else:
        for k in obj["kinds"]:
            if not isinstance(k, dict) or not isinstance(k.get("code"), str) or not _has_ru(k.get("label")):
                errs.append("object.kinds: {code, label: {ru, uz, en}}")
                break
    for key in ("must", "optional"):
        fs = tpl[key]
        if not isinstance(fs, list):
            errs.append(f"{key}: список полей")
            continue
        seen = set()
        for f in fs:
            if not isinstance(f, dict) or not isinstance(f.get("code"), str) or not _has_ru(f.get("label")):
                errs.append(f"{key}: поле {{code, type, label: {{ru, uz, en}}}}")
                break
            if f.get("type") not in FIELD_TYPES:
                errs.append(f"{key}.{f['code']}: тип одно из " + ", ".join(FIELD_TYPES))
            if f["code"] in seen:
                errs.append(f"{key}: повтор поля {f['code']}")
            seen.add(f["code"])
    if isinstance(tpl["must"], list) and len(tpl["must"]) > MAX_MUST:
        errs.append(f"must: не больше {MAX_MUST} обязательных полей сверх общих (приложение А.1)")
    vm = tpl["valuation_methods"]
    if not isinstance(vm, list) or not vm or any(not isinstance(m, dict) or m.get("method") not in VALUATION_METHODS
                                                 for m in vm):
        errs.append("valuation_methods: список {method: 1–6 | agreed | crop, when}")
    rk = tpl["risks"]
    if not isinstance(rk, dict) or rk.get("source") not in ("perils", "template"):
        errs.append("risks: {source: perils | template, items: [...]}")
    elif rk["source"] == "template":
        items = rk.get("items")
        if not isinstance(items, list) or not items:
            errs.append("risks.items: список рисков с долями")
        else:
            codes, total, ok_items = set(), 0.0, True
            for r in items:
                share = r.get("share_pct") if isinstance(r, dict) else None
                if not isinstance(r, dict) or not isinstance(r.get("code"), str) or not _has_ru(r.get("label")) \
                        or isinstance(share, bool) or not isinstance(share, (int, float)) or share < 0:
                    ok_items = False
                    break
                if r["code"] in codes:
                    errs.append(f"risks.items: повтор кода {r['code']}")
                codes.add(r["code"])
                total += float(share)
                if not isinstance(r.get("catastrophic", False), bool):
                    errs.append(f"risks.items.{r['code']}.catastrophic: true или false")
            if not ok_items:
                errs.append("risks.items: {code, label: {ru, uz, en}, share_pct ≥ 0, catastrophic}")
            elif abs(total - 100) > SHARE_TOLERANCE + 1e-9:
                errs.append(f"risks.items: сумма долей {round(total, 2)} — нужно 100 ± {SHARE_TOLERANCE}")
    elif con is not None:
        try:
            n = con.execute("SELECT COUNT(*) FROM perils WHERE class_code=?", (b,)).fetchone()[0]
        except Exception:
            n = None
        if n == 0:
            errs.append(f"risks.source = perils, но в таблице perils нет рисков класса {b}")
    fa = tpl["factors"]
    if not isinstance(fa, list) or any(not isinstance(f, dict) or not isinstance(f.get("code"), str)
                                       or f.get("direction") not in DIRECTIONS for f in fa):
        errs.append("factors: список {code, label, direction: up | down | both, ref}")
    sr = tpl["scenario_rule"]
    if not isinstance(sr, dict) or sr.get("code") not in SCENARIO_RULES:
        errs.append("scenario_rule.code: одно из " + ", ".join(SCENARIO_RULES))
    else:
        want = "risk_analytics" if sr["code"] in ENGINE_RULES else "template"
        if sr.get("engine") not in (None, want):
            errs.append(f"scenario_rule.engine: для правила {sr['code']} — {want}")
        if sr["code"] in ENGINE_RULES and b not in ("3", "8", "9"):
            errs.append(f"scenario_rule: правило {sr['code']} есть только у классов 3, 8, 9")
        if sr["code"] in TEMPLATE_RULES:
            what = sr.get("what") or {}
            if not all(_has_ru(what.get(s)) for s in SCENARIOS):
                errs.append("scenario_rule.what: подписи PML, EML, MFL (ru/uz/en)")
        errs += check_params(sr["code"], sr.get("params", {}))
    if not isinstance(tpl["documents"], dict) or not isinstance(tpl["documents"].get("items"), list):
        errs.append("documents: {checklists, items: [...]}")
    st = tpl["stats"]
    if not isinstance(st, list):
        errs.append("stats: список кодов наборов risk_stats")
    else:
        bad = [s for s in st if s not in rs.BY_ID]
        if bad:
            errs.append("stats: нет таких наборов — " + ", ".join(map(str, bad)))
    _codes_by_group(tpl["required_views"], "required_views", set(ae.VIEWS), errs)
    _codes_by_group(tpl["clauses"], "clauses", clause_codes(), errs)
    _codes_by_group(tpl["measures"], "measures", measure_codes(con), errs)
    if not isinstance(tpl["notes"], list):
        errs.append("notes: список")
    errs += check_factor_groups(tpl)
    errs += check_labels(tpl)
    return errs


# ================================================================================================
#  Для акта
# ================================================================================================

def for_group(d, group: Optional[str]) -> Optional[list]:
    """Список шаблона для группы объекта акта: своя группа → default → None (правило кода по группе)."""
    if not isinstance(d, dict):
        return None
    got = d.get(group or "")
    if got is None:
        got = d.get("default")
    return list(got) if got is not None else None


def single_kind(tpl: Optional[dict]) -> Optional[str]:
    """Код вида объекта, если у класса он один (кредит, груз, перерыв, правовая защита) — выбирать нечего."""
    kinds = [k for k in (((tpl or {}).get("object") or {}).get("kinds") or [])
             if isinstance(k, dict) and k.get("code")]
    return kinds[0]["code"] if len(kinds) == 1 else None


def with_single_kind(tpl: dict) -> dict:
    """Шаблон для экрана: object.single_kind = true и object.default_kind — единственный вид объекта берётся по
    умолчанию, список выбора из одного варианта экран не показывает."""
    obj = tpl.get("object") if isinstance(tpl, dict) else None
    if isinstance(obj, dict):
        one = single_kind(tpl)
        obj["single_kind"] = one is not None
        obj["default_kind"] = one
    return tpl


def kind_labels(tpl: Optional[dict]) -> dict:
    return {k["code"]: dict(k["label"]) for k in ((tpl or {}).get("object") or {}).get("kinds") or []
            if isinstance(k, dict) and k.get("code")}


def class_fields(tpl: Optional[dict]) -> dict:
    """Поля класса, которые вводятся как optional.class_fields: {код: описание поля}."""
    out = {}
    for f in list((tpl or {}).get("must") or []) + list((tpl or {}).get("optional") or []):
        if str(f.get("input") or "").startswith("optional.class_fields."):
            out[f["code"]] = f
    return out


def factor_groups(tpl: Optional[dict]) -> list:
    """Группы факторов тарифа шаблона (factor_groups) — список; нет блока — пусто."""
    fg = (tpl or {}).get("factor_groups")
    return [g for g in fg if isinstance(g, dict) and g.get("code")] if isinstance(fg, list) else []


def template_risks(tpl: Optional[dict], cls: str) -> list:
    """Риски шаблона в форме risk_analytics.analyze()["risks"] (доли в % нетто-ставки) — для классов без perils."""
    rk = (tpl or {}).get("risks") or {}
    if rk.get("source") != "template":
        return []
    return [{"code": r["code"], "name": r["label"]["ru"], "labels": dict(r["label"]), "class_code": cls,
             "catastrophic": bool(r.get("catastrophic")), "share_of_net_pct": round(float(r["share_pct"]), 1),
             "source": "template", "calibrated": CALIBRATED} for r in rk.get("items") or []]


def credit_insurable(credit_amount: float, collateral_value: float, max_share: float = 0.5) -> dict:
    """
    Кредиты (требования НАПП, CLAUDE.md п. 6): страхуется только необеспеченная часть и не более max_share суммы
    кредита. Сумма = min(кредит − обеспечение; max_share × кредит), не меньше нуля.
    Пример: кредит 100 млн, залог 60 млн → min(40; 50) = 40 млн.
    """
    c, k = float(credit_amount), float(collateral_value)
    unsecured = max(c - k, 0.0)
    cap = c * float(max_share)
    return {"credit": c, "collateral": k, "unsecured": unsecured, "cap": cap, "insurable": min(unsecured, cap),
            "by": "unsecured" if unsecured <= cap else "cap", "max_share": float(max_share),
            "legal_ref": "требования НАПП к кредитному страхованию", "calibrated": CALIBRATED}


# ================================================================================================
#  На языке
# ================================================================================================

def _is_label(x) -> bool:
    return isinstance(x, dict) and x and set(x) <= set(LANGS) and "ru" in x


def localize(obj, lang: str):
    """Все подписи {ru, uz, en} → строка на языке lang (нет перевода — русская)."""
    if _is_label(obj):
        return obj.get(lang) or obj["ru"]
    if isinstance(obj, dict):
        return {k: localize(v, lang) for k, v in obj.items()}
    if isinstance(obj, list):
        return [localize(v, lang) for v in obj]
    return obj


def checklist_docs(con, cls: str) -> list:
    """Документы для анализа из таблицы checklists: всегда + класс + продукты класса (scope = анализ)."""
    b = base_class(cls)
    codes = [str(cls)] + ([b] if b != str(cls) else [])
    try:
        prods = [r[0] for r in con.execute(
            f"SELECT DISTINCT product_code FROM product_classes WHERE class_code IN ({','.join('?' * len(codes))})",
            codes)]
        has_scope = any(r[1] == "scope" for r in con.execute("PRAGMA table_info(checklists)"))
        rows = db.rows(con, "SELECT scope_type, scope_code, doc_name, required"
                       + (", scope" if has_scope else ", 'анализ' AS scope") + " FROM checklists ORDER BY id")
    except Exception:
        return []
    out = []
    for r in rows:
        if r["scope"] != "анализ":
            continue
        if r["scope_type"] == "всегда" or (r["scope_type"] == "класс" and r["scope_code"] in codes) \
                or (r["scope_type"] == "продукт" and r["scope_code"] in prods):
            out.append({"doc": r["doc_name"], "required": bool(r["required"]), "scope_type": r["scope_type"],
                        "scope_code": r["scope_code"]})
    return out


def view(row: dict, lang: str, con=None, full: bool = True) -> dict:
    """Шаблон для ответа API: подписи на языке lang; полный — с документами из checklists базы."""
    tpl = row["template"]
    meta = {k: row.get(k) for k in ("class_code", "version", "source", "file_version", "updated_at", "updated_by",
                                    "requested_class", "alias_of", "pending_file_version")}
    # вариант класса (13з, 16у)
    meta.update(variant=bool(tpl.get("variant")), variant_of=tpl.get("variant_of"))
    if con is not None:
        n = products_count(con, row["class_code"])
        meta["products_count"] = n
        meta["no_products_note"] = localize(NO_PRODUCTS_NOTE, lang) if n == 0 else None
    if not full:
        rk = tpl.get("risks") or {}
        return {**meta, "name": localize(tpl.get("name"), lang), "lang": lang,
                "risks_source": rk.get("source"),
                "risks": [{"code": r["code"], "label": localize(r["label"], lang), "share_pct": r["share_pct"],
                           "catastrophic": bool(r.get("catastrophic"))} for r in rk.get("items") or []],
                "scenario_rule": (tpl.get("scenario_rule") or {}).get("code"),
                "must": [f["code"] for f in tpl.get("must") or []],
                "single_kind": single_kind(tpl) is not None, "default_kind": single_kind(tpl),
                "calibrated": CALIBRATED}
    out = {**meta, "lang": lang, "template": with_single_kind(localize(tpl, lang)), "calibrated": CALIBRATED}
    if con is not None:
        out["checklists"] = checklist_docs(con, row.get("requested_class") or row["class_code"])
        if (tpl.get("risks") or {}).get("source") == "perils":
            try:
                out["perils"] = [{"code": r[0], "name": r[1], "share_pct": round(float(r[2] or 0) * 100, 1),
                                  "catastrophic": bool(r[3])}
                                 for r in con.execute("SELECT code, name, base_share, is_catastrophic FROM perils "
                                                      "WHERE class_code=? ORDER BY base_share DESC",
                                                      (row["class_code"],))]
            except Exception:
                out["perils"] = []
    return out
