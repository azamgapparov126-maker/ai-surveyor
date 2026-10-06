"""
Проверка ввода /act/make (недоверенные данные): продукт и класс, суммы, регион, поля объекта и класса,
части комплексного продукта, парк объектов.
"""
import re
from datetime import date
from typing import Any, Optional

from .. import act_engine as ae
from .. import act_extras as ax
from .. import class_templates as ctpl
from .. import act_market as am
from .. import act_texts as tx
from .. import db, llm

from .common import CONDITIONS, load_settings, MAX_FILES, MAX_RECOGNIZED, MAX_REGION_TEXT, MAX_SUM, REGION_OTHER
from .recognize import pd_like, _s, value_limit
from .regions import region_code
from .documents import validate_credit_report
from .terms import _bool_in, _int_in, _money_in, validate_contract, validate_request


# --------------------------------------------------------------------------- #
#  Проверка ввода (недоверенные данные)
# --------------------------------------------------------------------------- #

def validate(con, body: dict) -> tuple:
    """(чистый ввод, ошибки {поле: текст}). Ошибки не глотаются: вызывающий отвечает 422 и пишет в журнал."""
    errs = {}
    must = body.get("must") if isinstance(body.get("must"), dict) else {}
    opt = body.get("optional") if isinstance(body.get("optional"), dict) else {}
    clean = {"must": {}, "optional": {}}
    m = clean["must"]
    st = load_settings(con)               # настройки читаются один раз на проверку

    ccode = _product_in(con, errs, m, must)
    _sums_region_in(errs, m, must)
    o = _object_opts_in(clean, errs, opt)
    tpl = _class_opts_in(con, ccode, errs, m, o, opt)
    _docs_opts_in(clean, errs, opt, st)
    _recognized_opts_in(body, clean, errs)
    # комплексный продукт по частям (30.09.2026): части сотрудника и переключатель «один объект / разные объекты»
    try:
        clean["same_object"] = _bool_in(opt.get("same_object"))
    except ValueError:
        errs["same_object"] = "да или нет"
    try:
        clean["parts_confirmed"] = _bool_in(opt.get("parts_confirmed"))
    except ValueError:
        errs["parts_confirmed"] = "да или нет"
    clean["parts"] = None
    if opt.get("parts") not in (None, "", []) and not any(k in errs for k in ("product_code", "class_code",
                                                                             "sum_insured")):
        tol = float(st["parts"]["sum_tolerance"])
        plan, p_err = validate_parts(con, opt.get("parts"), m, tol)
        if p_err:
            errs["parts"] = p_err
        clean["parts"] = plan
    # несколько объектов в одном акте (02.10.2026, парк ТС): у каждого свой уровень, ставка и премия
    _objects_in(clean, errs, must, opt, tpl, st)
    if m.get("sum_insured_from_objects"):
        # сумма договора посчитана по объектам — франшиза суммой проверяется против неё, как у обычного акта
        o["deductible"], ded_err = deductible_in(opt.get("deductible"), m["sum_insured"])
        errs.pop("deductible", None)
        if ded_err:
            errs["deductible"] = ded_err
    return clean, errs


def _product_in(con, errs: dict, m: dict, must: dict) -> str:
    """Продукт и класс по справочнику; результат — в clean.must."""
    pcode = str(must.get("product_code") or "").strip()[:10]
    ccode = str(must.get("class_code") or "").strip()[:10]
    product = None
    classes = []
    if pcode:
        rows = db.rows(con, "SELECT code, name, pricing_mode, rate_text FROM products WHERE code=?", pcode)
        if not rows:
            errs["product_code"] = "продукт не найден в справочнике"
        else:
            product = rows[0]
            classes = [r["class_code"] for r in db.rows(
                con, "SELECT class_code FROM product_classes WHERE product_code=? ORDER BY part_no", pcode)]
            classes = list(dict.fromkeys(classes))
            if ccode and ccode not in classes:
                errs["class_code"] = "класс не относится к продукту: " + ", ".join(classes)
            ccode = ccode or (classes[0] if classes else "")
            if not ccode:
                errs["product_code"] = "у продукта нет класса страхования"
    elif ccode:
        try:
            ctpl.ensure(con)              # класс 18 в справочнике classes (шаблоны 1.2.0)
        except Exception as e:
            print("акт: шаблоны классов не доведены:", type(e).__name__)
        # классов страхования жизни в шаблонах нет: строка L*, если она вдруг есть в classes, не принимается
        if ctpl.is_life_code(ccode) or not db.rows(con, "SELECT code FROM classes WHERE code=?", ccode):
            errs["class_code"] = "класс не найден в справочнике"
    else:
        errs["product_code"] = "нужен продукт или класс"
    m.update(product_code=pcode or None, class_code=ccode or None, product=product, product_classes=classes)
    return ccode


def _sums_region_in(errs: dict, m: dict, must: dict) -> None:
    """Страховая сумма, стоимость, регион и территория текстом (без ПД)."""
    for key in ("sum_insured", "object_value"):
        x = _money_in(must.get(key))
        if x is None:
            errs[key] = "нужно число больше нуля"
        elif x <= 0 or x > MAX_SUM:
            errs[key] = "сумма должна быть больше нуля и меньше 10^15"
        else:
            m[key] = x
    region = str(must.get("region") or "").strip()
    if not region:
        errs["region"] = "укажите регион"
    elif len(region) > 80:
        errs["region"] = "не длиннее 80 знаков"
    else:
        m["region"] = region
        m["region_code"] = region_code(region)
    # территория текстом (02.10.2026): обязательна для «Другое» (вне Узбекистана, маршрут), у остальных — по желанию
    rt_raw = must.get("region_text")
    rt = re.sub(r"\s+", " ", str(rt_raw)).strip() if rt_raw not in (None, "") and not isinstance(
        rt_raw, (dict, list, bool)) else ""
    if rt_raw not in (None, "") and not rt:
        errs["region_text"] = "текст территории страхования"
    elif len(rt) > MAX_REGION_TEXT:
        errs["region_text"] = f"не длиннее {MAX_REGION_TEXT} знаков"
    elif rt and llm.has_pd(rt):
        errs["region_text"] = "без персональных данных: только территория (страна, область, маршрут)"
    elif rt:
        m["region_text"] = rt
    if m.get("region_code") == REGION_OTHER and not m.get("region_text") and "region_text" not in errs:
        errs["region_text"] = "для региона «Другое» укажите территорию страхования текстом (до 120 знаков)"


def _object_opts_in(clean: dict, errs: dict, opt: dict) -> dict:
    """Необязательные поля объекта: годы, срок, место, признаки, убытки, цены, ставка."""
    o = clean["optional"]
    this_year = date.today().year
    for key, lo, hi in (("year", 1950, this_year + 1), ("purchase_year", 1950, this_year),
                        ("term_days", 1, 3660)):
        try:
            o[key] = _int_in(opt.get(key), lo, hi)
        except ValueError:
            errs[key] = f"целое число от {lo} до {hi}"
    loc = opt.get("location")
    if loc not in (None, ""):
        if str(loc) not in ae.LOCATIONS:
            errs["location"] = "одно из: " + ", ".join(ae.LOCATIONS)
        else:
            o["location"] = str(loc)
    for key in ("guard", "want_lower_premium", "documents_provided"):
        try:
            o[key] = _bool_in(opt.get(key))
        except ValueError:
            errs[key] = "да или нет"
    losses = opt.get("losses_3y")
    o["losses_count"] = o["small_count"] = o["losses_amount"] = None
    if losses not in (None, ""):
        if not isinstance(losses, dict):
            errs["losses_3y"] = "объект {count, small_count, amount}"
        else:
            try:
                o["losses_count"] = _int_in(losses.get("count"), 0, 1000)
                o["small_count"] = _int_in(losses.get("small_count"), 0, 1000)
            except ValueError:
                errs["losses_3y"] = "количество — целое число от 0 до 1000"
            amt = losses.get("amount")
            if amt not in (None, ""):
                a = _money_in(amt)
                if a is None or a > MAX_SUM:
                    errs["losses_3y"] = "сумма убытков — число не меньше нуля"
                else:
                    o["losses_amount"] = a
            if o["small_count"] is not None and o["losses_count"] is not None \
                    and o["small_count"] > o["losses_count"]:
                errs["losses_3y"] = "мелких убытков не может быть больше, чем всех"
    price = opt.get("price_new")
    if price not in (None, ""):
        p = _money_in(price)
        if p is None or p <= 0 or p > MAX_SUM:
            errs["price_new"] = "нужно число больше нуля"
        else:
            o["price_new"] = p
    # запрошенная ставка, введённая сотрудником (01.10.2026): если ниже минимальной ставки страховщика — акт отвечает,
    # можно ли застраховать по ней (оценка below_min_assessment); в типе ставки продукта (годовая или на весь срок)
    rr = opt.get("requested_rate_pct")
    if rr not in (None, ""):
        x = _money_in(rr)
        if x is None or not 0 < x <= 100:
            errs["requested_rate_pct"] = "процент больше 0 и не больше 100"
        else:
            o["requested_rate_pct"] = x
    # стоимость, которую клиент заявил до того, как сотрудник заменил её медианой объявлений
    dvo = opt.get("declared_value_original")
    if dvo not in (None, ""):
        v = _money_in(dvo)
        if v is None or v <= 0 or v > MAX_SUM:
            errs["declared_value_original"] = "нужно число больше нуля"
        else:
            o["declared_value_original"] = v
    return o


def _class_opts_in(con, ccode: str, errs: dict, m: dict, o: dict, opt: dict) -> Optional[dict]:
    """Поля по шаблону класса: вид объекта, поля класса, защита, сейсмика, франшиза."""
    # шаблон класса (app/class_templates.py): свои виды объекта и поля класса (optional.class_fields)
    tpl_row = ctpl.current(con, ccode) if ccode and "class_code" not in errs else None
    tpl = (tpl_row or {}).get("template")
    kind = opt.get("object_kind")
    if kind not in (None, ""):
        if kind not in tx.OBJECT_KINDS and kind not in ctpl.kind_labels(tpl):
            errs["object_kind"] = "неизвестный вид объекта"
        else:
            o["object_kind"] = kind
    cf, cf_err = validate_class_fields(opt.get("class_fields"), tpl)
    if cf_err:
        errs["class_fields"] = cf_err
    if cf:
        o["class_fields"] = cf
    otype = opt.get("object_type")
    if otype not in (None, ""):
        o["object_type"] = str(otype).strip()[:120]
    cond = opt.get("condition")
    if cond not in (None, ""):
        if cond not in CONDITIONS:
            errs["condition"] = "одно из: " + ", ".join(CONDITIONS)
        else:
            o["condition"] = cond
    dom = _s(opt.get("dominant_risk"), 80)
    if dom:
        o["dominant_risk"] = None if llm.has_pd(dom) else dom
    payer = opt.get("payer_type")
    if payer not in (None, ""):
        if payer not in ("юр", "физ"):
            errs["payer_type"] = "юр или физ"
        else:
            o["payer_type"] = payer

    # уточнения для сценариев убытка (risk_analytics); чего нет — берётся по умолчанию с пометкой
    prot = opt.get("protection")
    if prot not in (None, ""):
        codes = ax.PROT_CODES.get(ccode)
        if not codes:
            # у класса нет списка защиты (он есть только у 3, 8, 9) — значение не учитывается,
            # в акте об этом пометка в «принято по умолчанию» (as_protection_ignored)
            o["protection_ignored"] = True
        elif prot not in codes:
            errs["protection"] = "одно из: " + ", ".join(codes)
        else:
            o["protection"] = prot
    try:
        o["seismic_zone"] = _int_in(opt.get("seismic_zone"), 5, 10)
    except ValueError:
        errs["seismic_zone"] = "целое число баллов от 5 до 10"
    for key, allowed in (("construction", ax.CONSTRUCTIONS), ("activity", ax.ACTIVITIES)):
        v = opt.get(key)
        if v not in (None, ""):
            if v not in allowed:
                errs[key] = "одно из: " + ", ".join(allowed)
            else:
                o[key] = v
    # франшиза сотрудника: {pct | amount, type}; по обязательным видам не применяется (решает build_data)
    o["deductible"], ded_err = deductible_in(opt.get("deductible"), m.get("sum_insured"))
    if ded_err:
        errs["deductible"] = ded_err
    return tpl


def _docs_opts_in(clean: dict, errs: dict, opt: dict, st: dict) -> None:
    """Запрос филиала, договор, отчёт бюро и объявления — для сверок акта."""
    # запрос филиала (30.09.2026): тариф, премия, франшиза, срок — для сверки с расчётом акта
    inclusive = bool(st["request_check"]["term_inclusive"])
    rq, rq_err = validate_request(opt.get("request"), inclusive)
    if rq_err:
        errs["request"] = rq_err
    clean["request"] = rq
    # договор страхования (30.09.2026): те же поля и условия договора — для сверки с расчётом акта
    ct, ct_err = validate_contract(opt.get("contract"), inclusive)
    if ct_err:
        errs["contract"] = ct_err
    clean["contract"] = ct
    # отчёт кредитного бюро (01.10.2026): поля отчёта для проверок андеррайтеру по кредитным классам
    cbr, cbr_err = validate_credit_report(opt.get("credit_report"))
    if cbr_err:
        errs["credit_report"] = cbr_err
    clean["credit_report"] = cbr

    # объявления со снимков экрана с правками сотрудника (30.09.2026); в расчёт — act_engine.market_estimate
    mk, mk_err = am.validate_market(opt.get("market"))
    if mk_err:
        errs["market"] = mk_err
    clean["market"] = mk


def _recognized_opts_in(body: dict, clean: dict, errs: dict) -> None:
    """Распознанное с правками сотрудника и повреждения: известные поля без ПД."""
    # распознанное с правками сотрудника: только известные поля, значения без ПД
    rec = body.get("recognized")
    items, dropped = [], 0
    if rec is not None:
        if not isinstance(rec, list):
            errs["recognized"] = "список {key, value, source}"
        else:
            for r in rec[:MAX_RECOGNIZED]:
                if not isinstance(r, dict):
                    continue
                key = str(r.get("key") or "")
                val = _s(r.get("value"), value_limit(key))
                if key not in ae.FIELD_KEYS or not val:
                    continue
                if pd_like(key, val):
                    dropped += 1
                    continue
                src = str(r.get("source") or "input")
                src = src if src in ae.SOURCES else "input"
                note = _s(r.get("note"), 200)
                items.append({"key": key, "value": val, "source": src,
                              "note": None if (note and llm.has_pd(note)) else note,
                              "file_id": _s(r.get("file"), 10)})
    clean["recognized"] = items if rec is not None else None
    clean["dropped_pd"] = dropped
    dmg = body.get("damages")
    if dmg is not None:
        if not isinstance(dmg, list):
            errs["damages"] = "список {what, where}"
        else:
            clean["damages"] = []
            for d in dmg[:20]:
                d = {"what": d} if isinstance(d, str) else d
                if not isinstance(d, dict):
                    continue
                what, where = _s(d.get("what"), 160), _s(d.get("where"), 80)
                if what and not llm.has_pd(what) and not (where and llm.has_pd(where)):
                    item = {"what": what, "where": where, "file": _s(d.get("file"), 10)}
                    sev = str(d.get("severity") or "").strip().lower()
                    if sev in ("cosmetic", "major"):
                        item["severity"] = sev      # тяжесть повреждения (06.10.2026): косметические / существенные
                    clean["damages"].append(item)
    clean["session"] = _s(body.get("session"), 40)


def deductible_in(ded: Any, S: Optional[float]) -> tuple:
    """Франшиза сотрудника {pct | amount, type} → (франшиза | None, ошибка | None). amount — не больше половины S."""
    if ded in (None, "", {}):
        return None, None
    if not isinstance(ded, dict):
        return None, "объект {pct | amount, type}"
    p, a = ded.get("pct"), ded.get("amount")
    ftype = str(ded.get("type") or "unconditional").strip()
    pv = _money_in(p) if p not in (None, "") else None
    av = _money_in(a) if a not in (None, "") else None
    if ftype not in ax.FR_TYPES:
        return None, "type: " + ", ".join(ax.FR_TYPES)
    if (p not in (None, "") and pv is None) or (a not in (None, "") and av is None):
        return None, "pct и amount — числа"
    if pv is None and av is None:
        return None, "нужен pct (% страховой суммы) или amount (сумы)"
    if pv is not None and av is not None:
        return None, "укажите что-то одно: pct или amount"
    if pv is not None and not 0 < pv <= 50:
        return None, "pct больше 0 и не больше 50 % страховой суммы"
    if av is not None and not (0 < av <= (S or MAX_SUM) * 0.5):
        return None, "amount больше 0 и не больше половины страховой суммы"
    return {"pct": pv, "amount": av, "type": ftype}, None


def validate_part_fields(raw: Any, cls: str, tpl: Optional[dict]) -> tuple:
    """
    Признаки части (optional.parts[].fields): те же поля, что у договора, но свои у части — год, состояние, место,
    охрана, убытки за 3 года, документы, тип объекта, конструкция, деятельность, защита, сейсмозона, поля класса
    (по шаблону класса части), просьба снизить премию, преобладающий риск. (поля, ошибка).
    """
    if raw in (None, "", {}):
        return {}, None
    if not isinstance(raw, dict):
        return {}, "fields — объект {признак: значение}"
    out = {}
    this_year = date.today().year
    try:
        for key, lo, hi in (("year", 1950, this_year + 1), ("purchase_year", 1950, this_year)):
            v = _int_in(raw.get(key), lo, hi)
            if v is not None:
                out[key] = v
        z = _int_in(raw.get("seismic_zone"), 5, 10)
        if z is not None:
            out["seismic_zone"] = z
        for key in ("guard", "documents_provided", "want_lower_premium"):
            v = _bool_in(raw.get(key))
            if v is not None:
                out[key] = v
    except ValueError:
        return {}, "fields: год — целое 1950–следующий год, сейсмозона 5–10, да/нет — true/false"
    for key, allowed in (("condition", CONDITIONS), ("location", ae.LOCATIONS), ("construction", ax.CONSTRUCTIONS),
                         ("activity", ax.ACTIVITIES)):
        v = raw.get(key)
        if v not in (None, ""):
            if v not in allowed:
                return {}, f"fields.{key}: одно из " + ", ".join(allowed)
            out[key] = v
    prot = raw.get("protection")
    if prot not in (None, ""):
        codes = ax.PROT_CODES.get(cls)
        if codes and prot not in codes:
            return {}, "fields.protection: одно из " + ", ".join(codes)
        if codes:
            out["protection"] = prot
    losses = raw.get("losses_3y")
    if losses not in (None, ""):
        if not isinstance(losses, dict):
            return {}, "fields.losses_3y — объект {count, small_count, amount}"
        try:
            n, small = _int_in(losses.get("count"), 0, 1000), _int_in(losses.get("small_count"), 0, 1000)
        except ValueError:
            return {}, "fields.losses_3y: количество — целое число от 0 до 1000"
        if n is not None and small is not None and small > n:
            return {}, "fields.losses_3y: мелких убытков не может быть больше, чем всех"
        out["losses_count"], out["small_count"] = n, small
        amt = losses.get("amount")
        if amt not in (None, ""):
            a = _money_in(amt)
            if a is None or a > MAX_SUM:
                return {}, "fields.losses_3y: сумма убытков — число не меньше нуля"
            out["losses_amount"] = a
    price = raw.get("price_new")
    if price not in (None, ""):
        p = _money_in(price)
        if p is None or p <= 0 or p > MAX_SUM:
            return {}, "fields.price_new: число больше нуля"
        out["price_new"] = p
    ot = _s(raw.get("object_type"), 120)
    if ot and not llm.has_pd(ot):
        out["object_type"] = ot
    dom = _s(raw.get("dominant_risk"), 80)
    if dom and not llm.has_pd(dom):
        out["dominant_risk"] = dom
    cf, cf_err = validate_class_fields(raw.get("class_fields"), tpl)
    if cf_err:
        return {}, "fields.class_fields: " + cf_err
    if cf:
        out["class_fields"] = cf
    return out, None


def validate_parts(con, raw: Any, must: dict, tol: float) -> tuple:
    """
    optional.parts (недоверенный ввод) → (части | None, ошибка | None). Часть: {class_code, product_code?,
    sum_insured | share_pct, object_value?, object_kind?, object_description?, same_object?, fields{…}, deductible?}.
    Класс — из справочника; не из состава продукта — можно, с пометкой class_outside. Продукт части (например,
    обязательный вид) — из справочника, класс части должен к нему относиться. Сумма частей = страховой сумме договора
    (допуск tol сумов), доли 0–100. У продукта с одним классом частей не меньше двух.
    """
    if not isinstance(raw, list) or not raw or len(raw) > ae.MAX_PARTS:
        return None, f"parts — список от 1 до {ae.MAX_PARTS} частей {{class_code, sum_insured, …}}"
    classes = list(must.get("product_classes") or [])
    if len(classes) <= 1 and len(raw) < 2:
        return None, "у продукта один класс: одна часть — это обычный акт, уберите parts или добавьте часть"
    S = must.get("sum_insured")
    out = []
    for i, p in enumerate(raw, 1):
        if not isinstance(p, dict):
            return None, f"часть {i}: объект {{class_code, sum_insured, …}}"
        cls = str(p.get("class_code") or "").strip()[:10]
        if not cls:
            return None, f"часть {i}: нужен class_code"
        crow = db.rows(con, "SELECT code, name FROM classes WHERE code=?", cls)
        if not crow:
            return None, f"часть {i}: класс {cls} не найден в справочнике"
        pcode = str(p.get("product_code") or "").strip()[:10] or None
        prod = None
        if pcode and pcode != must.get("product_code"):
            rows = db.rows(con, "SELECT code, name, pricing_mode, rate_text FROM products WHERE code=?", pcode)
            if not rows:
                return None, f"часть {i}: продукт {pcode} не найден в справочнике"
            pcls = [r["class_code"] for r in db.rows(con, "SELECT class_code FROM product_classes "
                                                          "WHERE product_code=? ORDER BY part_no", pcode)]
            if cls not in pcls:
                return None, f"часть {i}: класс {cls} не относится к продукту {pcode} ({', '.join(pcls)})"
            prod = rows[0]
        share = None
        if p.get("share_pct") not in (None, ""):
            share = _money_in(p.get("share_pct"))
            if share is None or not 0 <= share <= 100:
                return None, f"часть {i}: share_pct — доля от 0 до 100"
        s = _money_in(p.get("sum_insured")) if p.get("sum_insured") not in (None, "") else None
        if s is None and share is not None and S:
            s = round(float(S) * share / 100, 2)
        if s is None or s <= 0 or s > MAX_SUM:
            return None, f"часть {i}: sum_insured — число больше нуля (или share_pct)"
        v = None
        if p.get("object_value") not in (None, ""):
            v = _money_in(p.get("object_value"))
            if v is None or v <= 0 or v > MAX_SUM:
                return None, f"часть {i}: object_value — число больше нуля"
        tpl = (ctpl.current(con, cls) or {}).get("template")
        kind = p.get("object_kind")
        if kind not in (None, ""):
            if kind not in tx.OBJECT_KINDS and kind not in ctpl.kind_labels(tpl):
                return None, f"часть {i}: неизвестный вид объекта"
        else:
            kind = None
        desc = _s(p.get("object_description"), 200)
        try:
            same = _bool_in(p.get("same_object"))
        except ValueError:
            return None, f"часть {i}: same_object — да или нет"
        fields, f_err = validate_part_fields(p.get("fields"), cls, tpl)
        if f_err:
            return None, f"часть {i}: {f_err}"
        ded, d_err = deductible_in(p.get("deductible"), s)
        if d_err:
            return None, f"часть {i}: deductible — {d_err}"
        out.append({"class_code": cls, "class_name": crow[0]["name"], "product_code": pcode if prod else None,
                    "product": prod, "class_outside": bool(classes) and cls not in classes and not prod,
                    "sum_insured": s, "share_pct": share, "object_value": v, "object_kind": kind,
                    "object_description": desc if desc and not pd_like("object_type", desc) else None,
                    "same_object": same, "fields": fields, "deductible": ded})
    bad = ae.check_parts_sum(out, S, tol) if S else None
    if bad:
        return None, (f"сумма частей {ae._plain_number(bad['total'])} не равна страховой сумме договора "
                      f"{ae._plain_number(bad['sum_insured'])} (разница {ae._plain_number(abs(bad['diff']))}, "
                      f"допуск {ae._plain_number(tol)} сум)")
    return out, None


def validate_class_fields(raw: Any, tpl: Optional[dict]) -> tuple:
    """
    Поля класса из шаблона (optional.class_fields): {код: значение}. Берутся только поля шаблона с вводом
    optional.class_fields.*; числа — в пределах, текст — до 120 знаков и без ПД (отбрасывается). (поля, ошибка).
    """
    if raw in (None, "", {}):
        return {}, None
    if not isinstance(raw, dict):
        return {}, "объект {код поля: значение}"
    known = ctpl.class_fields(tpl)
    out, bad = {}, []
    for code, v in list(raw.items())[:40]:
        f = known.get(str(code))
        if f is None or v in (None, ""):
            continue
        typ = f.get("type")
        try:
            if typ in ("int", "year"):
                x = _int_in(v, 0, 10_000_000)
                if x is not None:
                    out[code] = x
            elif typ in ("number", "money"):
                x = _money_in(v)
                if x is None or x < 0 or x > MAX_SUM:
                    raise ValueError
                out[code] = x
            elif typ == "bool":
                x = _bool_in(v)
                if x is not None:
                    out[code] = x
            elif typ == "choice":
                if str(v) not in (f.get("options") or []):
                    raise ValueError
                out[code] = str(v)
            else:
                s = _s(v, 120)
                if s and not llm.has_pd(s):
                    out[code] = s
        except ValueError:
            bad.append(str(code))
    return out, ("неверные значения: " + ", ".join(bad)) if bad else None


MAX_OBJECTS = 50
MAX_OBJECT_PHOTOS = MAX_FILES
# госномер косвенно указывает на владельца: в акт — только подсказка до 4 цифр, можно с многоточием впереди
# («…123», «...0457», «123»); буквы и полный номер не принимаются
_PLATE_HINT = re.compile(r"(?:…|\.{1,3})?\s*\d{1,4}")


def _photo_ref(v) -> Optional[str]:
    """Ссылка на файл загрузки: «f3» (id из /act/photos) или номер файла в запросе (3) → «f3» / «#3»."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return f"#{v}" if 1 <= v <= MAX_OBJECT_PHOTOS else None
    s = str(v).strip().lower()
    if re.fullmatch(r"f\d{1,2}", s):
        return s
    if re.fullmatch(r"\d{1,2}", s) and 1 <= int(s) <= MAX_OBJECT_PHOTOS:
        return "#" + s
    return None


def validate_objects(raw: Any, tpl: Optional[dict]) -> tuple:
    """
    optional.objects (недоверенный ввод) → (объекты | None, ошибка | None). Объект: {label, sum_insured,
    object_value, object_kind?, year?, mileage?, plate_hint?, photo_ids?, class_fields?, requested_rate_pct?,
    condition?}. До MAX_OBJECTS объектов; подпись — без ПД; номер — только подсказка (часть номера), полный
    госномер не принимается; поля класса — по шаблону класса договора.
    """
    if not isinstance(raw, list) or not raw or len(raw) > MAX_OBJECTS:
        return None, f"objects — список от 1 до {MAX_OBJECTS} объектов {{label, sum_insured, object_value, …}}"
    this_year = date.today().year
    out = []
    for i, ob in enumerate(raw, 1):
        if not isinstance(ob, dict):
            return None, f"объект {i}: объект {{label, sum_insured, object_value, …}}"
        label = _s(ob.get("label"), 120)
        if not label:
            return None, f"объект {i}: нужна подпись label (например, марка и модель)"
        if llm.has_pd(label):
            return None, f"объект {i}: в подписи похоже на персональные данные — только марка, модель, вид"
        vals = {}
        for key in ("sum_insured", "object_value"):
            x = _money_in(ob.get(key))
            if x is None or x <= 0 or x > MAX_SUM:
                return None, f"объект {i}: {key} — число больше нуля"
            vals[key] = x
        kind = ob.get("object_kind")
        if kind not in (None, ""):
            if kind not in tx.OBJECT_KINDS and kind not in ctpl.kind_labels(tpl):
                return None, f"объект {i}: неизвестный вид объекта"
        else:
            kind = None
        try:
            year = _int_in(ob.get("year"), 1950, this_year + 1)
            mileage = _int_in(ob.get("mileage"), 0, 9_999_999)
        except ValueError:
            return None, f"объект {i}: year — целое 1950–{this_year + 1}, mileage — целое от 0"
        cond = ob.get("condition")
        if cond not in (None, "") and cond not in CONDITIONS:
            return None, f"объект {i}: condition — одно из " + ", ".join(CONDITIONS)
        plate = _s(ob.get("plate_hint"), 40)
        if plate and not _PLATE_HINT.fullmatch(plate):
            return None, (f"объект {i}: plate_hint — только до 4 цифр номера, например «…123» или «123»; "
                          "буквы и полный госномер в акт не берутся")
        rr = ob.get("requested_rate_pct")
        req = None
        if rr not in (None, ""):
            req = _money_in(rr)
            if req is None or not 0 < req <= 100:
                return None, f"объект {i}: requested_rate_pct — процент больше 0 и не больше 100"
        pids = ob.get("photo_ids")
        refs = []
        if pids not in (None, "", []):
            if not isinstance(pids, list) or len(pids) > MAX_OBJECT_PHOTOS:
                return None, f"объект {i}: photo_ids — список до {MAX_OBJECT_PHOTOS} id файлов из /act/photos"
            for v in pids:
                r = _photo_ref(v)
                if r is None:
                    return None, f"объект {i}: photo_ids — id файла («f1») или его номер в загрузке (1–{MAX_FILES})"
                if r not in refs:
                    refs.append(r)
        cf, cf_err = validate_class_fields(ob.get("class_fields"), tpl)
        if cf_err:
            return None, f"объект {i}: class_fields — {cf_err}"
        out.append({"index": i, "label": label, "sum_insured": vals["sum_insured"],
                    "object_value": vals["object_value"], "object_kind": kind, "year": year, "mileage": mileage,
                    "condition": cond or None, "plate_hint": plate or None, "photo_ids": refs,
                    "class_fields": cf, "requested_rate_pct": req})
    return out, None


def _objects_in(clean: dict, errs: dict, must: dict, opt: dict, tpl: Optional[dict], st: dict) -> None:
    """Перечень объектов (парк ТС): проверка и суммы договора. Сумма и стоимость договора — сумма по объектам:
    не введены — считаются; введены — должны совпасть с допуском parts.sum_tolerance (иначе 422). Вместе с
    частями комплексного продукта и у продукта из нескольких классов — нельзя (понятная ошибка)."""
    m = clean["must"]
    clean["objects"] = None
    raw = opt.get("objects")
    if raw in (None, "", []):
        return
    if opt.get("parts") not in (None, "", []):
        errs["objects"] = ("перечень объектов и части комплексного продукта вместе не принимаются: оформите парк "
                           "отдельным актом по продукту одного класса")
        return
    if len(m.get("product_classes") or []) > 1:
        errs["objects"] = ("у продукта несколько классов (" + ", ".join(m["product_classes"]) + "): перечень "
                           "объектов принимается только у продукта одного класса")
        return
    if "product_code" in errs or "class_code" in errs:
        return
    objs, o_err = validate_objects(raw, tpl)
    if o_err:
        errs["objects"] = o_err
        return
    clean["objects"] = objs
    tol = float(st["parts"]["sum_tolerance"])
    for key in ("sum_insured", "object_value"):
        total = round(sum(float(x[key]) for x in objs), 2)
        if must.get(key) in (None, ""):
            m[key] = total
            m[key + "_from_objects"] = True
            errs.pop(key, None)
        elif key in m and abs(float(m[key]) - total) > tol:
            errs[key] = (f"сумма по объектам {ae._plain_number(total)} не равна введённой "
                         f"{ae._plain_number(m[key])} (разница {ae._plain_number(abs(float(m[key]) - total))}, "
                         f"допуск {ae._plain_number(tol)} сум); можно не вводить — посчитается по объектам")
