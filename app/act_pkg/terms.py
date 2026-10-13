"""
Условия запроса филиала и договора: простые значения ввода, проверка условий, страхователь, правило
кредита, источник условий (решает сервер по своей загрузке).
"""
import re
from datetime import date
from typing import Any, Optional

from .. import branch_request as br
from .. import class_templates as ctpl
from .. import contract_read as cr
from .. import act_texts as tx
from .. import llm

from .common import _empty_v
from .recognize import pd_like, _s


# --------------------------------------------------------------------------- #
#  Простые значения ввода: сумма, целое, да/нет
# --------------------------------------------------------------------------- #

def _money_in(v) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        x = float(v)
    else:
        s = re.sub(r"[\s\u00a0\u202f_]", "", str(v)).replace(",", ".")
        if not re.fullmatch(r"\d+(\.\d+)?", s):
            return None
        x = float(s)
    if x != x or x in (float("inf"), float("-inf")):
        return None
    return x


def _int_in(v, lo: int, hi: int) -> Optional[int]:
    if v is None or v == "" or isinstance(v, bool):
        return None
    # только целое: 365.5 — ошибка ввода, а не 365 (дробная часть не отбрасывается молча)
    if isinstance(v, float):
        if v != v or not v.is_integer():
            raise ValueError
        x = int(v)
    elif isinstance(v, int):
        x = v
    else:
        s = str(v).strip()
        if not re.fullmatch(r"[+-]?\d{1,9}(?:[.,]0+)?", s):
            raise ValueError
        x = int(re.split(r"[.,]", s)[0])
    if not lo <= x <= hi:
        raise ValueError
    return x


def _bool_in(v) -> Optional[bool]:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "да", "ha"):
        return True
    if s in ("0", "false", "no", "нет", "yoʻq", "yoq"):
        return False
    raise ValueError


# --------------------------------------------------------------------------- #
#  Условия запроса филиала и договора: проверка ввода, страхователь, правило кредита
# --------------------------------------------------------------------------- #

REQUEST_SOURCES = ("document", "photo", "input", "session")
CONTRACT_SOURCES = ("document", "document_ai", "photo", "input", "session")


DATE_MIN, DATE_MAX = date(2000, 1, 1), date(2100, 12, 31)     # даты срока, договора, платежей
DATE_RANGE = "с 01.01.2000 по 31.12.2100"
MAX_TERMS_SUM = 1e14          # суммы запроса и договора: сто триллионов сумов — опечатка в разрядах, а не объект


def _date_in(v) -> Optional[date]:
    """Дата из «ГГГГ-ММ-ДД» или «ДД.ММ.ГГГГ» в пределах 2000–2100; иначе ValueError."""
    s = str(v or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s) or None
    m2 = re.fullmatch(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", s)
    d = None
    try:
        if m:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        elif m2:
            d = date(int(m2.group(3)), int(m2.group(2)), int(m2.group(1)))
    except ValueError:
        pass
    if d is None or not DATE_MIN <= d <= DATE_MAX:
        raise ValueError
    return d


def validate_request(rq: Any, inclusive: bool = True) -> tuple:
    """
    optional.request (запрос филиала, недоверенный ввод) → (чистый запрос | None, текст ошибки | None).
    {tariff_pct: % годовых (0; 100], premium: сумы > 0, franchise: {applied, text?, pct?, amount?} или текст
    бланка, term_from/term_to: ГГГГ-ММ-ДД или ДД.ММ.ГГГГ (2000–2100), term_days: целое 1–3660, sum_insured?,
    object_value?, cadastre_no?, object_kind?, class_hint?, object_description?}. Поле source не доверяется:
    источник решает сервер по своей загрузке (build_data → _trust_doc).
    Даты сильнее term_days: дни считаются по датам (оба крайних дня — настройка request_check.term_inclusive);
    term_days, не совпавший с датами, — ошибка, а не молчаливая замена.
    """
    if rq in (None, "", {}):
        return None, None
    if not isinstance(rq, dict):
        return None, "объект {tariff_pct, premium, franchise, term_from, term_to, term_days}"
    out, err = _validate_terms(rq, inclusive, REQUEST_SOURCES)
    if err:
        return None, err
    err = _object_terms(rq, out)
    if err:
        return None, err
    if all(out[k] is None for k in ("tariff_pct", "premium", "franchise", "term_days")):
        return None, "в запросе нет ни тарифа, ни премии, ни франшизы, ни срока"
    return out, None


def _money_term(v, key: str) -> tuple:
    """Сумма условия документа: (число | None, ошибка | None). Больше MAX_TERMS_SUM — опечатка в разрядах."""
    if v in (None, ""):
        return None, None
    x = _money_in(v)
    if x is None or not 0 < x <= MAX_TERMS_SUM:
        return None, f"{key} — число больше нуля и не больше 10^14 сумов"
    return x, None


def _object_terms(rq: dict, out: dict) -> Optional[str]:
    """Стоимость и объект документа (для сверки запроса с договором): стоимость, кадастр, вид, описание."""
    out["object_value"], err = _money_term(rq.get("object_value"), "object_value")
    if err:
        return err
    desc = _s(rq.get("object_description"), br.MAX_TEXT)
    out["object_description"] = desc if desc and not pd_like("object_type", desc) else None
    cad = _s(rq.get("cadastre_no"), 40)
    out["cadastre_no"] = cad if cad and br._KADASTR.fullmatch(cad) else None
    kind = str(rq.get("object_kind") or "").strip()
    out["object_kind"] = kind if kind in tx.OBJECT_KINDS else None
    hint = str(rq.get("class_hint") or "").strip()
    out["class_hint"] = hint if hint in HINT_CODES else None
    out["policyholder"] = policyholder_in(rq.get("policyholder"))
    return None


def policyholder_in(v: Any) -> Optional[dict]:
    """
    Страхователь из запроса или договора (для проверки «по кредиту страхователь — банк», правило проекта № 6):
    строка названия или {kind: legal | individual, name} / {is_legal, name}. Название хранится только у юрлица
    (гражданин — kind = individual без имени, как в branch_request.party). Пусто — None.
    """
    if v in (None, "", {}):
        return None
    if isinstance(v, dict):
        kind = v.get("kind")
        if kind == "individual" or v.get("is_legal") is False:
            return {"kind": "individual", "name": None}
        name = _s(v.get("name"), 200)
        if not name:
            return {"kind": "legal", "name": None} if kind == "legal" or v.get("is_legal") is True else None
        v = name
    got = br.party(_s(v, 200))
    if got.get("kind") == "legal" and got.get("name") and pd_like("policyholder", got["name"]):
        got = {"kind": "legal", "name": None}
    return got if got.get("kind") else None


def _policyholder(req: Optional[dict], ct: Optional[dict], upload: dict) -> Optional[dict]:
    """Страхователь договора: договор, потом запрос филиала (введённое или своя загрузка документа)."""
    cands = [("contract", (ct or {}).get("policyholder")), ("request", (req or {}).get("policyholder")),
             ("contract", ((upload.get("contract") or {}).get("fields") or {}).get("policyholder")),
             ("request", ((upload.get("branch_request") or {}).get("fields") or {}).get("policyholder"))]
    for src, p in cands:
        if isinstance(p, dict) and p.get("kind") in ("legal", "individual"):
            return {"kind": p["kind"], "name": p.get("name") if p["kind"] == "legal" else None, "source": src}
    return None


def credit_rule(tpl: Optional[dict], cls: str) -> Optional[float]:
    """Доля кредита по правилу credit шаблона (0,5), если класс кредитный (14, 13з); иначе None."""
    sr = (tpl or {}).get("scenario_rule") or {}
    if sr.get("code") != "credit" and ctpl.base_class(cls) != "14":
        return None
    try:
        share = float((sr.get("params") or {}).get("max_share_of_loan", ctpl.CREDIT_MAX_SHARE))
    except (TypeError, ValueError):
        share = ctpl.CREDIT_MAX_SHARE
    return min(share, ctpl.CREDIT_MAX_SHARE)


HINT_CODES = ("building", "equipment", "vehicle", "special_machinery", "cargo", "other")


def _validate_terms(rq: dict, inclusive: bool, sources: tuple) -> tuple:
    """Общие условия запроса филиала и договора: тариф, премия, сумма, франшиза, срок."""
    out = {"tariff_pct": None, "premium": None, "franchise": None, "term_from": None, "term_to": None,
           "term_days": None, "sum_insured": None, "source": "input"}
    tr = rq.get("tariff_pct")
    if tr not in (None, ""):
        x = _money_in(tr)
        if x is None or not 0 < x <= 100:
            return None, "tariff_pct — процент годовых больше 0 и не больше 100"
        out["tariff_pct"] = x
    for key in ("premium", "sum_insured"):
        out[key], err = _money_term(rq.get(key), key)
        if err:
            return None, err
    if out["premium"] is not None and out["sum_insured"] is not None and out["premium"] > out["sum_insured"]:
        return None, "premium больше страховой суммы — проверьте разряды"
    fr = rq.get("franchise")
    if fr not in (None, "", {}):
        if isinstance(fr, str):
            fr = br.franchise(fr)
        if not isinstance(fr, dict) or not isinstance(fr.get("applied"), bool):
            return None, "franchise — {applied: true|false, text, pct, amount}"
        pv = _money_in(fr.get("pct")) if fr.get("pct") not in (None, "") else None
        av = _money_in(fr.get("amount")) if fr.get("amount") not in (None, "") else None
        if (fr.get("pct") not in (None, "") and (pv is None or not 0 < pv <= 100)) or \
                (fr.get("amount") not in (None, "") and (av is None or not 0 < av <= MAX_TERMS_SUM)):
            return None, "franchise.pct — от 0 до 100 %, franchise.amount — сумы больше нуля"
        text = _s(fr.get("text"), 120)
        out["franchise"] = {"applied": fr["applied"], "text": None if (text and llm.has_pd(text)) else text,
                            "pct": pv if fr["applied"] else None, "amount": av if fr["applied"] else None}
    d1, d2 = rq.get("term_from"), rq.get("term_to")
    if d1 not in (None, "") or d2 not in (None, ""):
        try:
            a, b = _date_in(d1), _date_in(d2)
        except ValueError:
            return None, f"term_from и term_to — даты ГГГГ-ММ-ДД или ДД.ММ.ГГГГ {DATE_RANGE}, обе"
        if b < a:
            return None, "term_to раньше term_from"
        days = br.term_days(a, b, inclusive)
        if not 1 <= days <= br.MAX_TERM_DAYS:
            return None, f"срок от 1 до {br.MAX_TERM_DAYS} дней"
        out.update(term_from=a.isoformat(), term_to=b.isoformat(), term_days=days)
    td = rq.get("term_days")
    if td not in (None, ""):
        try:
            x = _int_in(td, 1, br.MAX_TERM_DAYS)
        except ValueError:
            return None, f"term_days — целое число от 1 до {br.MAX_TERM_DAYS}"
        if out["term_days"] is not None and x != out["term_days"]:
            return None, (f"term_days {x} не совпадает со сроком по датам ({out['term_days']} дн., "
                          f"{'оба крайних дня включены' if inclusive else 'без последнего дня'})")
        out["term_days"] = x
    # заявленный экраном источник — только подсказка «был ли документ» (пометка «документ недоступен»)
    src = str(rq.get("source") or "input").strip()
    out["claimed_source"] = src if src in sources else "input"
    pc = re.search(r"(?<!\d)(\d{3,4})(?!\d)", str(rq.get("product_code") or ""))
    out["product_code"] = pc.group(1).zfill(4) if pc else None
    return out, None


def validate_contract(ct: Any, inclusive: bool = True) -> tuple:
    """
    optional.contract (договор страхования, недоверенный ввод) → (чистый договор | None, текст ошибки | None).
    Поля optional.request (тариф, премия, франшиза {applied, text, pct, amount, type}, срок, sum_insured,
    объект) и contract_no, contract_date, currency, covered_risks[], exclusions[] (коды словаря или короткий
    текст), payment_mode, payments[{date, amount}], items[{name, sum}]. Даты — 2000–2100. Поле source не
    доверяется (build_data → _trust_doc). Значения, похожие на ПД, отбрасываются.
    """
    if ct in (None, "", {}):
        return None, None
    if not isinstance(ct, dict):
        return None, "объект {tariff_pct, premium, franchise, term_from, term_to, sum_insured, contract_no, …}"
    out, err = _validate_terms(ct, inclusive, CONTRACT_SOURCES)
    if err:
        return None, err
    err = _object_terms(ct, out)
    if err:
        return None, err
    fr = ct.get("franchise")
    if out["franchise"] is not None and isinstance(fr, dict) and fr.get("type") not in (None, ""):
        if fr["type"] not in ("unconditional", "conditional"):
            return None, "franchise.type — unconditional или conditional"
        out["franchise"]["type"] = fr["type"] if out["franchise"]["applied"] else None
    no = _s(ct.get("contract_no"), 40)
    out["contract_no"] = no if no and not llm.has_pd(no) else None
    out["contract_date"] = None
    if ct.get("contract_date") not in (None, ""):
        try:
            out["contract_date"] = _date_in(ct["contract_date"]).isoformat()
        except ValueError:
            return None, f"contract_date — дата ГГГГ-ММ-ДД или ДД.ММ.ГГГГ {DATE_RANGE}"
    cur = str(ct.get("currency") or "").strip().upper()
    if cur and cur not in ("UZS", "USD", "EUR", "RUB"):
        return None, "currency — UZS, USD, EUR или RUB"
    out["currency"] = cur or None
    for key, table, codes in (("covered_risks", cr._RISK_F, cr.RISK_CODES),
                              ("exclusions", cr._EXCL_F, cr.EXCLUSION_CODES)):
        v = ct.get(key)
        if v in (None, "", []):
            out[key] = []
            continue
        if not isinstance(v, list) or len(v) > 40:
            return None, f"{key} — список (не больше 40) кодов или коротких названий"
        vals = [x for x in (_s(x.get("text") or x.get("code") if isinstance(x, dict) else x, 80) for x in v) if x]
        out[key] = cr._codes([x for x in vals if not llm.has_pd(x)], table, codes)
    mode = ct.get("payment_mode")
    if mode not in (None, "", "single", "installments"):
        return None, "payment_mode — single или installments"
    out["payment_mode"] = mode or None
    pays = ct.get("payments")
    out["payments"] = []
    if pays not in (None, "", []):
        if not isinstance(pays, list) or len(pays) > cr.MAX_PAYMENTS:
            return None, f"payments — список до {cr.MAX_PAYMENTS} платежей {{date, amount}}"
        for x in pays:
            if not isinstance(x, dict):
                return None, "payments — список {date, amount}"
            try:
                d = _date_in(x.get("date"))
            except ValueError:
                return None, f"payments.date — дата ГГГГ-ММ-ДД или ДД.ММ.ГГГГ {DATE_RANGE}"
            a, err = _money_term(x.get("amount"), "payments.amount")
            if err or a is None:
                return None, "payments.amount — число больше нуля и не больше 10^14 сумов"
            out["payments"].append({"date": d.isoformat(), "amount": a})
    items = ct.get("items")
    out["items"] = []
    if items not in (None, "", []):
        if not isinstance(items, list) or len(items) > cr.MAX_ITEMS:
            return None, f"items — список до {cr.MAX_ITEMS} частей {{name, sum}}"
        for x in items:
            if not isinstance(x, dict):
                return None, "items — список {name, sum}"
            a, err = _money_term(x.get("sum"), "items.sum")
            if err or a is None:
                return None, "items.sum — число больше нуля и не больше 10^14 сумов"
            name = _s(x.get("name"), 200)
            out["items"].append({"name": name if name and not pd_like("object_type", name) else None, "sum": a})
    # выгодоприобретатель (день 7.2, проверка 12): только название юрлица; гражданин — без имени (ПД не берём)
    bf = ct.get("beneficiary")
    bname = _s(bf.get("name") if isinstance(bf, dict) else bf, 120)
    if bname and br.is_legal(bname) and not pd_like("beneficiary", bname):
        out["beneficiary"] = {"kind": "legal", "name": bname}
    elif bname or (isinstance(bf, dict) and bf.get("kind") == "individual"):
        out["beneficiary"] = {"kind": "individual", "name": None}
    else:
        out["beneficiary"] = None
    if all(out[k] in (None, []) for k in ("tariff_pct", "premium", "franchise", "term_days", "sum_insured",
                                          "contract_no", "object_description", "covered_risks", "payments")):
        return None, "в договоре нет ни одного условия"
    return out, None


# --------------------------------------------------------------------------- #
#  Источник условий запроса и договора: решает сервер по своей загрузке (30.09.2026)
# --------------------------------------------------------------------------- #

# поля, правка которых сотрудником видна в акте (было → стало)
RQ_COMPARE = ("product_code", "sum_insured", "object_value", "tariff_pct", "premium", "franchise",
              "term_from", "term_to", "term_days", "cadastre_no", "object_description")
CT_COMPARE = RQ_COMPARE + ("contract_no", "contract_date", "currency", "covered_risks", "exclusions",
                           "payment_mode", "payments", "items")
_MONEY_CMP = ("sum_insured", "object_value", "premium")
DOC_SOURCES = ("document", "document_ai", "photo", "session")


def _same_term(key: str, a, b) -> bool:
    if _empty_v(a) and _empty_v(b):
        return True
    if key in _MONEY_CMP or key == "tariff_pct":
        try:
            return a is not None and b is not None and abs(float(a) - float(b)) <= 0.005
        except (TypeError, ValueError):
            return False
    return a == b


def _saved_terms(prefix: str, block: dict, inclusive: bool) -> dict:
    """Условия своей загрузки в той же форме, что проверенный ввод экрана (validate_request / validate_contract):
    сравниваются одинаково нормализованные значения."""
    f = block.get("fields") or {}
    if prefix == "rq":
        raw = br.request_of(f, "document")
        clean, err = validate_request(raw, inclusive)
    else:
        raw = cr.request_of(f, "document")
        raw["cadastre_no"] = f.get("cadastre_no")
        clean, err = validate_contract(raw, inclusive)
    return clean if clean and not err else dict(raw)


def _field_source(prefix: str, block: dict, key: str) -> str:
    """Источник поля своей загрузки: photo — скан (модель), document_ai — дочитано моделью, иначе document."""
    base = "photo" if block.get("source") == "photo" else "document"
    if prefix == "ct":
        return (block.get("field_sources") or {}).get(key) or base
    return base


def _trust_doc(prefix: str, sent: Optional[dict], block: Optional[dict], upload_missing: bool,
               inclusive: bool) -> tuple:
    """
    Условия запроса (rq) или договора (ct) для акта и источник каждого поля (образец — act_market.trust_listings).
    Своя живая загрузка с этим документом есть: присланное сравнивается с сохранённым по полям — совпало —
    источник документа (document / photo / document_ai), отличается — input и правка «было → стало».
    Загрузки нет (истекла, чужая, сменилась сессия): все поля — input, пометка «документ недоступен».
    Возвращает (условия | None, {source_kind, origin, edits, field_sources, doc_missing}).
    """
    keys = RQ_COMPARE if prefix == "rq" else CT_COMPARE
    if prefix == "ct" and ((block or {}).get("fields") or {}).get("is_template"):
        # загружен бланк договора (поля не заполнены): сверки договора с расчётом нет — как у заявления
        return None, None
    if sent is None:
        if not block:
            return None, None
        doc = _request_from_upload(block) if prefix == "rq" else _contract_from_upload(block)
        if not doc:
            return None, None
        fs = {k: _field_source(prefix, block, k) for k in keys if not _empty_v(doc.get(k))}
        return doc, {"source_kind": "document", "origin": block.get("source"), "edits": [], "field_sources": fs,
                     "doc_missing": False}
    doc = dict(sent)
    claimed = doc.pop("claimed_source", "input")
    if not block:
        fs = {k: "input" for k in keys if not _empty_v(doc.get(k))}
        doc["source"] = "input"
        return doc, {"source_kind": "input", "origin": None, "edits": [], "field_sources": fs,
                     "doc_missing": bool(upload_missing or claimed in DOC_SOURCES)}
    saved = _saved_terms(prefix, block, inclusive)
    edits, fs = [], {}
    dates_edited = any(not _same_term(k, saved.get(k), doc.get(k)) for k in ("term_from", "term_to"))
    for k in keys:
        a, b = saved.get(k), doc.get(k)
        if _empty_v(a) and _empty_v(b):
            continue
        if _same_term(k, a, b):
            fs[k] = _field_source(prefix, block, k)
            continue
        fs[k] = "input"
        if k == "term_days" and dates_edited:
            continue                      # дни — следствие правки дат, отдельной правкой не считаются
        edits.append({"code": k, "was": a, "now": b})
    doc["source"] = block.get("source") or "document"
    return doc, {"source_kind": "document_edited" if edits else "document", "origin": doc["source"],
                 "edits": edits, "field_sources": fs, "doc_missing": False}


def trust_sources(items: list, upload: Optional[dict]) -> int:
    """
    Источник photo/plate/document/marking остаётся только у значения, которое сервер сам вернул в этой
    загрузке (совпали key, value и source). Загрузки нет, она чужая или истекла, значение изменено —
    источник input («введено сотрудником»). Возвращает, сколько источников понижено.
    """
    known = {(f.get("key"), f.get("value"), f.get("source")): f.get("file_id")
             for f in (upload or {}).get("fields") or []}
    n = 0
    for r in items:
        if r["source"] == "input":
            continue
        sig = (r["key"], r["value"], r["source"])
        if sig in known:
            r["file_id"] = known[sig]
        else:
            r["source"], r["file_id"] = "input", None
            n += 1
    return n


def _request_from_upload(b: dict) -> Optional[dict]:
    """Запрос филиала из своей живой загрузки: те же числа, что сервер разобрал сам (источник session)."""
    f = (b or {}).get("fields") or {}
    # те же поля, что optional.request экрана (со стоимостью и объектом — для сверки с договором)
    req = br.request_of(f, "session")
    if all(req[k] is None for k in ("tariff_pct", "premium", "franchise", "term_days")):
        return None
    return req


def _contract_from_upload(b: dict) -> Optional[dict]:
    """Договор из своей живой загрузки: те же поля, что сервер разобрал сам (источник session)."""
    f = (b or {}).get("fields") or {}
    ct = cr.request_of(f, "session")
    ct["covered_risks"] = list(f.get("covered_risks") or [])
    ct["exclusions"] = list(f.get("exclusions") or [])
    ct["cadastre_no"] = f.get("cadastre_no")
    if ct.get("currency") not in (None, "UZS"):
        # суммы не в сумах сверять с расчётом акта нельзя — только условия без денег
        ct.update(sum_insured=None, premium=None, object_value=None, payments=[], items=[])
    if all(ct.get(k) in (None, []) for k in ("tariff_pct", "premium", "franchise", "term_days", "sum_insured",
                                             "contract_no", "object_description", "covered_risks")):
        return None
    return ct


def _with_object(doc: Optional[dict], fields: Optional[dict]) -> Optional[dict]:
    """Условия документа + кадастр, вид и описание объекта (для сверки запроса с договором): при своей живой
    загрузке этого документа — сохранённые при загрузке (как в сверке экрана), иначе — присланные."""
    if not doc:
        return None
    keys = ("cadastre_no", "class_hint", "object_kind", "object_description")
    if fields:
        return {**doc, **{k: fields.get(k) for k in keys}}
    return dict(doc)


def _attach_trust(rc: dict, trust: Optional[dict]) -> None:
    """Источник условий и правки сотрудника — в сверку (request_check / contract_check), для акта и экрана."""
    if rc.get("available") and trust:
        rc.update(source_kind=trust["source_kind"], origin=trust["origin"], edits=trust["edits"],
                  field_sources=trust["field_sources"], doc_missing=trust["doc_missing"])
