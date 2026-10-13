"""
Акт на две страницы A4 (ТЗ 13.10.2026, день 9.1; макет — приложение A, «Акт, стр. 1» и «Акт, стр. 2»).

Собирается из готового ответа render (тот же JSON, что у экрана) — цифры в документе совпадают с экраном, сам JSON
не меняется. Источники, формулы оценки, вилка и прочие ставки в документ не идут: они в «Как посчитано»
(GET /act/{id}?how=1, ссылка QR-кодом на стр. 2). Исключение — цепочка множителей ставки (раздел 4).

Стр. 1: шапка (номер, дата по Ташкенту, филиал / сотрудник без ПД, тариф);
  1 Объект (до 6 строк по две пары): продукт, объект, госномер / VIN маскированные, год, пробег, место, состояние;
  2 Стоимость и страховая сумма (3 строки): оценка с методом, n и надёжностью; S и S/V с выводом по ГК ст. 936/938;
    решение «принята рыночная / оставлена с пояснением»;
  3 Риск (до 6 строк): уровень из K_об с баллом, что повышает и снижает, PML / EML / MFL одной строкой;
  4 Решение и цена: вердикт, ставка одной строкой, пояснение до 3 строк (rate.explain), цепочка множителей до 8 строк,
    части договора до 5 строк, сверка с договором (итог и до 3 нарушений);
  5 Условия (до 5): исключения, франшиза, оговорки, предупредительные мероприятия с эффектом.
Стр. 2: фото с датой и координатами (заполняет export.py), подписи, QR на «Как посчитано».

detail: 0 — полный вид; 1 — короче факторы (меньше пунктов в разделах 3–5, цепочка без расшифровки факторов);
export.py переходит на detail = 1, если первая страница не помещается (сначала он убирает фото).
Решение документа — правило заказчика (экспертно, calibrated = 0): см. decision().
"""
import re
from typing import Optional

from .. import act_texts as tx
from ..act_texts import money, pct, t

MAX_PAIRS = 12                      # раздел 1: до 6 строк по две пары «подпись — значение»
MAX_VALUE = 70                      # длинное значение пары обрезается по слову
MAX_PLACE = 110
MAX_RISK_ITEMS = (4, 2)             # признаков в строках «Повышает» / «Снижает» (полный вид, короткий)
MAX_TERMS = (5, 4)
MAX_TERM_LEN = (110, 85)
MAX_PARTS = 5
MAX_CC = 3
# номера объекта в документе — маскированно (ТЗ 9.1): госномер, заводской номер / VIN, номер двигателя, кадастр
ID_KEYS = ("reg_no", "serial_no", "engine_no", "cadastre_no")
DAMAGE_VIEWS_ORDER = ("front", "back", "left", "right", "plate", "odometer", "interior", "facade", "roof", "electrical",
                      "fire_safety", "general", "installation", "packaging", "marking", "transport")
# устаревшее имя (06.10.2026) — порядок ракурсов используют фото второй страницы
VIEW_ORDER = DAMAGE_VIEWS_ORDER + ("document",)


def _short(s, n: int) -> str:
    s = " ".join(str(s or "").split())
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0].rstrip(",;:—- ")
    return cut + "…"


def _row_map(rows: list) -> dict:
    return {r.get("label"): r for r in rows or []}


def _lower(s: str, lang: str) -> str:
    s = str(s or "")
    return s[:1].lower() + s[1:] if s and lang != "en" else s


def group_vehicle(act: dict) -> bool:
    """Дорожный транспорт или спецтехника (класс 3): подпись номеров — «Госномер / VIN»."""
    return bool(act.get("veh_group"))


def mask_id(v) -> str:
    """Госномер, VIN, заводской номер — маскированно: первые 3 и последние 4 знака (короткие — 2 и 1)."""
    s = re.sub(r"\s+", "", str(v or ""))
    if len(s) >= 9:
        return s[:3] + "•" * (len(s) - 7) + s[-4:]
    if len(s) >= 4:
        return s[:2] + "•" * (len(s) - 3) + s[-1:]
    return "•" * len(s)


# --------------------------------------------------------------------------- #
#  Решение документа
# --------------------------------------------------------------------------- #

def decision(act: dict) -> dict:
    """Решение документа по правилу заказчика (экспертно): {code: accept | review | decline, label, why}.
    Отказать / согласовать с ЦО — отказ движка акта или сверка договора «decline_or_agree»; на рассмотрение —
    решение акта «на рассмотрение», высокий уровень, запрошенная ставка ниже минимума, нет фото или сверка «review»;
    иначе — принять."""
    lang = act["lang"]
    risk = act.get("risk") or {}
    ins = act.get("inspection") or {}
    bm = act.get("below_min_assessment") or {}
    cs = (act.get("contract_check") or {}).get("checks_summary") or {}
    level = risk.get("level")
    level_label = risk.get("level_label") or tx.label(tx.LEVEL_LABELS, level or "moderate", lang)
    if (act.get("decision") or {}).get("code") == "decline":
        return {"code": "decline", "label": t("a2_dec_decline", lang), "why": t("cp_why_decline", lang)}
    if cs.get("verdict") == "decline_or_agree":
        return {"code": "decline", "label": t("a2_dec_decline", lang),
                "why": _lower(cs.get("text") or cs.get("verdict_label") or "", lang)}
    why = []
    if level == "high":
        why.append(t("cp_why_high", lang))
    if bm.get("available"):
        why.append(t("cp_why_below", lang))
    if not ins.get("photos"):
        why.append(t("cp_why_no_photo", lang))
    if cs.get("verdict") == "review":
        why.append(_lower(cs.get("text") or cs.get("verdict_label") or "", lang))
    if (act.get("decision") or {}).get("code") == "review" and not why:
        why.append(_lower(str((act.get("decision") or {}).get("text") or "").rstrip("."), lang))
    if why:
        return {"code": "review", "label": t("a2_dec_review", lang), "why": "; ".join(w for w in why if w)}
    return {"code": "accept", "label": t("a2_dec_accept", lang),
            "why": t("cp_why_ok", lang, level=_lower(level_label, lang))}


# --------------------------------------------------------------------------- #
#  Шапка
# --------------------------------------------------------------------------- #

def _tariff_text(act: dict, extras: dict) -> str:
    lang = act["lang"]
    if extras.get("tariff"):
        return t("a2_tariff", lang, v=extras["tariff"])
    from .view_fork import _order_ref_text
    marks = (((act.get("how_computed") or {}).get("rate_fork") or {}).get("marks")
             or (act.get("rate_fork") or {}).get("marks") or [])
    for m in marks:
        ref = _order_ref_text(((m.get("source") or {}).get("title")), lang)
        if ref:
            return t("a2_tariff", lang, v=ref)
    return t("a2_tariff", lang, v=t("a2_tariff_policy", lang))


def _issuer_text(act: dict, extras: dict) -> str:
    lang = act["lang"]
    iss = extras.get("issuer") or {}
    parts = []
    if iss.get("branch"):
        parts.append(t("a2_branch", lang, v=_short(iss["branch"], 40)))
    if iss.get("employee"):
        parts.append(t("a2_employee", lang, v=iss["employee"]))
    elif iss.get("guest"):
        parts.append(t("a2_guest", lang))
    return " · ".join(parts)


# --------------------------------------------------------------------------- #
#  1. Объект
# --------------------------------------------------------------------------- #

def _s1(act: dict, detail: int) -> dict:
    lang = act["lang"]
    NA = t("na", lang)
    rows = [r for r in act["sections"][0].get("rows") or []
            if r.get("value") not in (None, "", NA) and r.get("label") != t("s1_not_specified", lang)]
    by = _row_map(rows)
    used = set()

    def take(label):
        r = by.get(label)
        if r is None:
            return None
        used.add(label)
        return str(r["value"])

    def fl(key):
        return tx.field_label(key, lang)

    take(t("class", lang))
    pairs = []
    product = take(t("product", lang))
    if product:
        pairs.append((t("a2_l_product", lang), product))
    otype, brand, model = take(fl("object_type")), take(fl("brand")), take(fl("model"))
    if brand and model and model.lower().startswith(brand.lower()):
        brand = None                                    # «XCMG» + «XCMG QY50K5D» — марка уже в модели
    obj = [x for x in (otype, brand, model) if x]
    if obj:
        pairs.append((t("a2_l_object", lang), " ".join(obj)))
    ids, id_keys = [], []
    for key in ID_KEYS:
        v = take(fl(key)) if fl(key) != key else None
        if v:
            ids.append(mask_id(v))
            id_keys.append(key)
    if ids:
        lbl = t("a2_l_id", lang) if "reg_no" in id_keys or group_vehicle(act) else fl(id_keys[0])
        pairs.append((lbl, " / ".join(dict.fromkeys(ids))))
    year = take(fl("year")) or take(tx.YEAR_BUILT.get(tx.lang_of(lang)) or "")
    if year:
        pairs.append((t("a2_l_year", lang), year))
    mileage = take(fl("mileage"))
    if mileage:
        pairs.append((t("a2_l_mileage", lang), mileage))
    address = take(t("lbl_object_address", lang))
    region = take(t("region", lang))
    place = address or region
    if place:
        pairs.append((t("a2_l_place", lang), _short(place, MAX_PLACE)))
    pairs.append((t("a2_l_state", lang), _short(_state(act), MAX_PLACE)))
    for r in rows:
        if r["label"] in used or len(pairs) >= MAX_PAIRS:
            continue
        pairs.append((r["label"], _short(r["value"], MAX_VALUE)))
    long_ = (t("a2_l_place", lang), t("a2_l_state", lang))
    pairs = [(a, b if a in long_ else _short(b, MAX_VALUE)) for a, b in pairs[:MAX_PAIRS]]
    return {"n": 1, "title": t("a2_s1", lang), "pairs": pairs}


def _state(act: dict) -> str:
    lang = act["lang"]
    ins = act.get("inspection") or {}
    photos = ins.get("photos") or 0
    if not photos:
        return t("a2_state_no_photo", lang)
    dmg = ins.get("damages") or []
    if dmg:
        items = [str(d.get("what") or "") + (f" ({d['where']})" if d.get("where") else "") for d in dmg]
        s = t("a2_state_damage", lang, list=", ".join(items))
    elif ins.get("done"):
        s = t("a2_state_clean", lang)
    else:
        s = t("cp_s2_damages_na", lang)
    return s + "; " + t("a2_photos_n", lang, n=photos)


# --------------------------------------------------------------------------- #
#  2. Стоимость и страховая сумма
# --------------------------------------------------------------------------- #

def _verdict_text(code: Optional[str], ratio, lang: str) -> str:
    if code == "over":
        return t("cp_v_over", lang)
    if code == "under":
        return t("cp_v_under", lang)
    if code == "refine":
        return t("cp_v_refine", lang)
    if ratio is not None and float(ratio) < 100 - 1e-9:
        return t("cp_v_prop", lang)
    return t("cp_v_normal", lang)


def _valuation_row(v: dict, lang: str) -> tuple:
    method = v.get("method") or "analogs"
    label = t("a2_v_" + method, lang) if method in ("analogs", "cost", "exchange", "depreciation") \
        else t("a2_v_analogs", lang)
    if not v.get("available") or not v.get("V"):
        why = str(v.get("reason") or "")
        return label, (t("a2_v_failed_why", lang, why=why) if why and lang == "ru" else t("a2_v_failed", lang))
    parts = [money(v["V"], lang)]
    if method == "analogs":
        parts.append(t("a2_v_m_analogs", lang, n=v.get("n") or 0))
    elif method == "exchange":
        parts.append(t("a2_v_m_exchange", lang, n=v.get("n") or 0, days=(v.get("period") or {}).get("days") or 30))
    elif method == "cost":
        inp = v.get("inputs") or {}
        area = inp.get("area_m2")
        parts.append(t("a2_v_m_cost", lang, area=tx._num(float(area), tx.lang_of(lang), 0 if float(area).is_integer()
                                                          else 1) if area is not None else t("na", lang),
                       wear=tx.pct_fixed(float(inp.get("wear") or 0) * 100, lang, 1)))
    elif method == "depreciation":
        parts.append(t("a2_v_m_depreciation", lang))
    rng = v.get("range") or {}
    if rng.get("low") and rng.get("high"):
        parts.append(t("a2_v_range", lang, lo=_mln(rng["low"], lang, bare=True), hi=_mln(rng["high"], lang)))
    if v.get("reliability"):
        parts.append(t("a2_v_rel", lang, v=t("a2_rel_" + v["reliability"], lang)
                       if v["reliability"] in ("high", "medium", "low") else v["reliability"]))
    return label, parts[0] + " — " + ", ".join(parts[1:]) if len(parts) > 1 else parts[0]


def _s2(act: dict) -> dict:
    lang = act["lang"]
    rows3 = _row_map(act["sections"][2].get("rows") or [])
    S_txt = (rows3.get(t("sum_insured", lang)) or {}).get("value") or t("na", lang)
    V_txt = (rows3.get(t("object_value", lang)) or {}).get("value")
    val, sv, vd = act.get("valuation") or None, act.get("value_sv") or None, act.get("value_decision") or None
    value = act.get("value") or {}
    rows = []
    if val:
        rows.append(_valuation_row(val, lang))
    if not val or not val.get("available"):
        if V_txt and V_txt != t("na", lang):
            rows.append((t("a2_v_declared", lang), V_txt))
    if sv:
        ratio, code = sv.get("ratio_pct"), sv.get("verdict")
    else:
        ratio, code = value.get("ratio_pct"), value.get("final_verdict") or value.get("verdict")
    if ratio is not None:
        line = t("a2_sum_line", lang, sum=S_txt, ratio=tx.pct_fixed(ratio, lang, 1),
                 verdict=_verdict_text(code, ratio, lang))
    else:
        line = t("a2_sum_line_nov", lang, sum=S_txt)
    rows.append((t("a2_sum", lang), line))
    if vd:
        if vd.get("accept_market"):
            rows.append((t("a2_vdec", lang), t("a2_vdec_accept", lang, v=money(vd.get("V"), lang))))
        else:
            rows.append((t("a2_vdec", lang), t("a2_vdec_keep", lang, note=_short(vd.get("note"), 140))))
    elif sv and sv.get("verdict") not in (None, "normal"):
        rows.append((t("a2_vdec", lang), t("a2_vdec_pending", lang)))
    return {"n": 2, "title": t("a2_s2", lang), "rows": rows[:3]}


# --------------------------------------------------------------------------- #
#  3. Риск
# --------------------------------------------------------------------------- #

def _mln(x, lang: str, bare: bool = False) -> str:
    """1 472 500 000 → «1 472,5 млн сум» (bare — без «сум»: для левой границы диапазона)."""
    if x is None:
        return t("na", lang)
    if abs(float(x)) < 1e6:
        return money(x, lang)
    v = round(float(x) / 1e6, 1)
    num = tx._num(v, tx.lang_of(lang), 0 if v == int(v) else 1)
    return num if bare else t("cp_mln", lang, v=num)


def _factor_head(text: str) -> str:
    """«Состояние объекта: видимые повреждения (2) — повышает риск» → «состояние объекта: видимые повреждения (2)»."""
    return str(text or "").split(" — ")[0].strip()


def _s3(act: dict, detail: int) -> dict:
    lang = act["lang"]
    risk = act.get("risk") or {}
    hc = act.get("how_computed") or {}
    sc = act.get("scoring") or {}
    head = [t("a2_level", lang, v=_lower(risk.get("level_label") or "", lang))]
    if hc.get("k_object") is not None:
        head[0] += f" ({t('a2_level_k', lang, k=tx._num(float(hc['k_object']), tx.lang_of(lang), 2))})"
    if sc.get("available") and sc.get("score") is not None:
        head.append(t("a2_score", lang, cls=sc.get("class_code") or "", score=sc["score"]))
    lines = [" · ".join(head)]
    n = MAX_RISK_ITEMS[1 if detail else 0]
    fs = risk.get("factors") or []
    ups = [_factor_head(f["text"]) for f in fs if f.get("sign") == "up"]
    downs = [_factor_head(f["text"]) for f in fs if f.get("sign") == "down"]
    unknown = [str(f.get("text") or "").split(":")[0].strip() for f in fs if f.get("sign") == "unknown"]
    if ups:
        lines.append(t("a2_up", lang, v="; ".join(_lower(x, lang) for x in ups[:n])))
    if downs:
        lines.append(t("a2_down", lang, v="; ".join(_lower(x, lang) for x in downs[:n])))
    if unknown and not detail:
        lines.append(t("a2_unknown", lang, v=", ".join(_lower(x, lang) for x in unknown[:n])))
    lines.append(_loss_line(act))
    return {"n": 3, "title": t("a2_s3", lang), "lines": lines[:6]}


def _loss_line(act: dict) -> str:
    lang = act["lang"]
    sc = act.get("scenarios") or {}
    tot = (act.get("parts") or {}).get("totals") or {}
    src = sc if sc.get("available") and sc.get("pml") else None
    if src is None and (tot.get("scenarios") or {}).get("available"):
        s2 = tot["scenarios"]
        src = {"pml": {"amount": s2.get("pml")}, "eml": {"amount": s2.get("eml")}, "mfl": {"amount": s2.get("mfl")},
               "retention": tot.get("retention") or {}}
    if src is None:
        return t("a2_loss_na", lang)

    def amt(k):
        it = src.get(k) or {}
        a = _mln(it.get("amount"), lang)
        return a + (f" ({it['pct_text']})" if it.get("pct_text") else "")
    ret = src.get("retention") or {}
    if not ret.get("known"):
        rt = t("a2_ret_na", lang)
    elif ret.get("within") is False and ret.get("eml_excess"):
        rt = t("a2_ret_out", lang, v=_mln(ret["eml_excess"], lang))
    else:
        rt = t("a2_ret_in", lang)
    return t("a2_loss", lang, pml=amt("pml"), eml=amt("eml"), mfl=amt("mfl"), ret=rt)


# --------------------------------------------------------------------------- #
#  4. Решение и цена
# --------------------------------------------------------------------------- #

def _fr_short(act: dict) -> str:
    lang = act["lang"]
    fr = act.get("franchise") or {}
    st = fr.get("status")
    if st == "applied" and (fr.get("size_pct") or fr.get("size_amount")):
        return t("a2_fr_on", lang, v=f"{pct(fr.get('size_pct'), lang)} ({money(fr.get('size_amount'), lang)})")
    if st == "statutory":
        return _lower(t("cp_fr_statutory", lang), lang)
    if fr.get("needed") or st == "proposed":
        return _lower(_franchise_value(act), lang)
    return t("a2_fr_none", lang)


def _franchise_value(act: dict) -> str:
    lang = act["lang"]
    fr = act.get("franchise") or {}
    st = fr.get("status")
    if st == "applied":
        return t("cp_fr_applied", lang, type=fr.get("type_label") or "", pct=pct(fr.get("size_pct"), lang),
                 amount=money(fr.get("size_amount"), lang))
    if st == "statutory":
        return t("cp_fr_statutory", lang)
    if fr.get("size_pct"):
        v = f"{fr.get('type_label') or ''} {pct(fr['size_pct'], lang)} ({money(fr.get('size_amount'), lang)})".strip()
    else:
        v = _short(fr.get("text"), 90)
    return t("cp_fr_proposed", lang, v=v)


def _rate_line(act: dict) -> str:
    lang = act["lang"]
    r = act.get("rate") or {}
    pr = act.get("premium") or {}
    prem = money(pr.get("amount"), lang) if pr.get("amount") is not None else t("na", lang)
    if act.get("objects"):
        return t("a2_rate_objects", lang, premium=prem) + " · " + _fr_short(act)
    if r.get("mode") == "multi" or (act.get("parts") or {}).get("mode") == "multi":
        return t("a2_rate_parts", lang, premium=prem) + " · " + _fr_short(act)
    rate = r.get("final_pct") if r.get("final_pct") is not None else r.get("applied_pct")
    if rate is None or r.get("mode") not in ("tariff", "statutory"):
        return t("a2_rate_none", lang)
    if r.get("rate_type") == "fixed":
        s = t("a2_rate_fixed", lang, rate=pct(rate, lang), premium=prem)
    else:
        s = t("a2_rate_annual", lang, rate=pct(rate, lang), premium=prem, days=pr.get("term_days") or 365)
    return s + " · " + _fr_short(act)


def _explain(act: dict) -> dict:
    """Пояснение ставки до 3 строк: {main, factors: [подписи], tail} — export.py подгоняет под ширину."""
    lang = act["lang"]
    r = act.get("rate") or {}
    ex = r.get("explain") or {}
    if ex.get("available"):
        lines = list(ex.get("lines") or [])
        fl = [f.get("label") for f in ex.get("factors") or [] if f.get("label")]
        main = lines[0] if lines else ex.get("text") or ""
        rest = lines[1:]
        tail = None
        if fl and rest:
            rest = rest[1:]
        if rest:
            tail = " ".join(rest)
        return {"main": main, "factors": fl, "tail": tail}
    if r.get("mode") == "multi" or (act.get("parts") or {}).get("mode") == "multi":
        return {"main": t("a2_parts_note", lang), "factors": [], "tail": None}
    if r.get("mode") == "tariff" and r.get("base_pct") is not None:
        return {"main": t("a2_legacy_explain", lang, rate=pct(r.get("final_pct") or r.get("applied_pct"), lang),
                          base=pct(r.get("base_pct"), lang), adj=tx.pct_fixed(r.get("adj_pct") or 0, lang, 0),
                          min=pct(r.get("min_pct"), lang) if r.get("min_pct") is not None else t("na", lang)),
                "factors": [], "tail": None}
    return {"main": "", "factors": [], "tail": None}


def explain_text(E: dict, lang: str, n_factors: Optional[int] = None) -> str:
    """Текст пояснения с n_factors факторами (None — все); фраза факторов — из rm_explain_factors."""
    out = [E.get("main") or ""]
    fl = E.get("factors") or []
    if n_factors is not None:
        fl = fl[:n_factors]
    if fl:
        out.append(t("rm_explain_factors", lang, list=", ".join(fl)))
    if E.get("tail"):
        out.append(E["tail"])
    return " ".join(x for x in out if x)


def _signed_money(x, lang: str) -> str:
    if x is None:
        return ""
    x = round(float(x))
    if x == 0:
        return "0"
    return ("+" if x > 0 else "−") + money(abs(x), lang)


def _chain(act: dict, detail: int) -> list:
    """Цепочка множителей: [(шаг, ставка, вклад в премию)] до 8 строк (экспертные множители — calibrated = 0)."""
    lang = act["lang"]
    r = act.get("rate") or {}
    hc = act.get("how_computed") or {}
    L = tx.lang_of(lang)
    if r.get("mode") == "multi" or act.get("objects") or (act.get("parts") or {}).get("mode") == "multi":
        return []
    rate = r.get("final_pct") if r.get("final_pct") is not None else r.get("applied_pct")
    if rate is None or r.get("mode") not in ("tariff",):
        return []
    prem_rate = r.get("applied_pct")
    scale = (float(r["premium"]) / float(prem_rate)) if r.get("premium") and prem_rate else None

    def pp(x):                         # премия по ставке x, сум
        return None if scale is None or x is None else float(x) * scale

    def num(x, d=2):
        return tx._num(float(x), L, d)
    rows = []
    if hc.get("r_net") is not None and hc.get("r_tech") is not None:
        rows.append((t("a2_ch_net", lang), pct(hc["r_net"], lang), money(pp(hc["r_net"]), lang) if scale else ""))
        rows.append((t("a2_ch_load", lang, theta=num(hc.get("theta") or 0), c=num(hc.get("c") or 0),
                       L=num(hc.get("L") or 0, 3)), pct(hc["r_tech"], lang),
                     _signed_money(pp(hc["r_tech"]) - pp(hc["r_net"]), lang) if scale else ""))
        cur = float(hc["r_tech"])
        facs = hc.get("factors") or []
        for kind, key, k in (("object", "a2_ch_k_object", hc.get("k_object")),
                             ("region", "a2_ch_k_region", hc.get("k_region")),
                             ("client", "a2_ch_k_client", hc.get("k_client"))):
            if k is None:
                continue
            nxt = cur * float(k)
            step = t(key, lang, k=num(k))
            if not detail:
                why = []
                if kind == "object":
                    obj = sorted([f for f in facs if f.get("kind") == "object" and abs(f.get("effect_pct") or 0) > 0],
                                 key=lambda f: -abs(f.get("effect_pct") or 0))
                    why = [f"{(f.get('label') or {}).get(L) or (f.get('label') or {}).get('ru') or f.get('code')} "
                           f"{_signed_pct(f.get('effect_pct'), lang)}" for f in obj[:2]]
                elif kind == "client":
                    why = [t("a2_ch_no_loss" if float(k) < 1 else "a2_ch_losses", lang)] if float(k) != 1 else []
                elif kind == "region" and float(k) == 1:
                    why = [t("a2_ch_region_flat", lang)]
                if why:
                    step += " — " + ", ".join(why)
            if detail and kind == "region" and float(k) == 1:
                cur = nxt
                continue
            rows.append((step, pct(round(nxt, 6), lang), _signed_money(pp(nxt) - pp(cur), lang) if scale else ""))
            cur = nxt
        calc = cur
    else:
        base = r.get("base_pct") if r.get("base_pct") is not None else hc.get("base_pct")
        if base is None:
            return []
        rows.append((t("a2_ch_tariff", lang), pct(base, lang), money(pp(base), lang) if scale else ""))
        k = 1 + float(r.get("adj_pct") or 0) / 100
        calc = float(r.get("calc_pct") if r.get("calc_pct") is not None else base * k)
        rows.append((t("a2_ch_level", lang, k=num(k)), pct(calc, lang),
                     _signed_money(pp(calc) - pp(base), lang) if scale else ""))
        # прежний путь, режим apply: факторы объекта и поправки региона и рынка уже в ставке акта
        if r.get("factors_applied") and r.get("factor_act_pct") is not None:
            nxt = float(r["factor_act_pct"])
            rows.append((t("a2_ch_factors", lang), pct(nxt, lang), _signed_money(pp(nxt) - pp(calc), lang)
                         if scale else ""))
            calc = nxt
        if r.get("fork_applied") and r.get("applied_pct") is not None:
            nxt = float(r["applied_pct"])
            rows.append((t("a2_ch_fork", lang), pct(nxt, lang), _signed_money(pp(nxt) - pp(calc), lang)
                         if scale else ""))
            calc = nxt
    mn, cap = r.get("min_pct"), hc.get("cap_pct")
    applied = r.get("applied_pct")
    if r.get("min_applied") and mn is not None:
        rows.append((t("a2_ch_min", lang, m=pct(mn, lang)), pct(mn, lang),
                     _signed_money(pp(mn) - pp(calc), lang) if scale else ""))
    elif cap is not None and applied is not None and abs(float(applied) - float(cap)) < 1e-9 and calc > float(cap):
        rows.append((t("a2_ch_cap", lang, m=pct(cap, lang)), pct(cap, lang),
                     _signed_money(pp(cap) - pp(calc), lang) if scale else ""))
    elif mn is not None:
        step = t("a2_ch_min_ok", lang, m=pct(mn, lang))
        if cap is not None:
            step += "; " + t("a2_ch_cap_ok", lang, m=pct(cap, lang))
        rows.append((step, "", ""))
    pr = act.get("premium") or {}
    rows.append((t("a2_ch_result", lang), pct(rate, lang), money(pr.get("amount"), lang)))
    return rows[:8]


def _signed_pct(x, lang: str) -> str:
    if x is None:
        return ""
    return ("+" if float(x) > 0 else "−") + tx.pct_fixed(abs(float(x)), lang, 0)


def _parts_table(act: dict) -> Optional[dict]:
    lang = act["lang"]
    p = act.get("parts") or {}
    if p.get("mode") == "multi" and p.get("items"):
        cols = [t("a2_p_part", lang), t("a2_p_class", lang), t("a2_p_sum", lang), t("a2_p_rate", lang),
                t("a2_p_min", lang), t("a2_p_premium", lang)]
        rows = []
        for it in p["items"][:MAX_PARTS]:
            rr = it.get("rate") or {}
            rate = rr.get("final_pct") if rr.get("final_pct") is not None else rr.get("applied_pct")
            rows.append([str(it.get("index")), str(it.get("class_code") or ""), money(it.get("sum_insured"), lang),
                         pct(rate, lang), pct(rr.get("min_pct"), lang) if rr.get("min_pct") is not None else "—",
                         money(it.get("premium"), lang) if it.get("premium") is not None else t("na", lang)])
        tot = p.get("totals") or {}
        more = len(p["items"]) - MAX_PARTS
        rows.append([t("a2_p_total", lang), t("a2_p_more", lang, n=more) if more > 0 else "",
                     money(tot.get("sum_insured"), lang) if tot.get("sum_insured") is not None else "", "", "",
                     money((act.get("premium") or {}).get("amount"), lang)])
        return {"title": t("a2_parts", lang), "columns": cols, "rows": rows, "widths": [9, 9, 22, 14, 14, 22]}
    objs = act.get("objects") or []
    if objs:
        cols = ["№", t("a2_o_object", lang), t("a2_p_sum", lang), t("a2_p_rate", lang), t("a2_p_premium", lang)]
        rows = []
        for o in objs[:MAX_PARTS]:
            rows.append([str(o.get("index")), _short(o.get("label"), 40), money(o.get("sum_insured"), lang),
                         pct((o.get("rate") or {}).get("applied_pct"), lang),
                         money(o.get("premium"), lang) if o.get("premium") is not None else t("na", lang)])
        more = len(objs) - MAX_PARTS
        tot = act.get("objects_total") or {}
        rows.append([t("a2_p_total", lang), t("a2_p_more", lang, n=more) if more > 0 else "",
                     money(tot.get("sum_insured"), lang) if tot.get("sum_insured") is not None else "", "",
                     money((act.get("premium") or {}).get("amount"), lang)])
        return {"title": t("a2_objects", lang, n=len(objs)), "columns": cols, "rows": rows, "widths": [7, 41, 20, 12, 20]}
    return None


_FORMULA = re.compile(r"\s*=\s*S\s*×[^=]*=\s*")


def _cc_lines(act: dict) -> list:
    """Сверка с договором: итог и до 3 нарушений (справочные проверки — после основных)."""
    lang = act["lang"]
    cc = act.get("contract_check") or {}
    checks = cc.get("checks") or []
    sm = cc.get("checks_summary") or {}
    if not checks:
        if cc.get("available") and (cc.get("summary") or {}).get("text"):
            return [t("a2_cc", lang, v=_lower(cc["summary"]["text"], lang))]   # акт до 12 проверок
        return [t("a2_cc_none", lang)]
    v = sm.get("verdict")
    head = t("a2_cc", lang, v=t("a2_cc_ok", lang) if v in ("ok", None) else (sm.get("verdict_label") or v))
    bad = [c for c in checks if c.get("verdict") in ("violated", "review", "decline_or_agree")]
    bad.sort(key=lambda c: (bool(c.get("reference")), c.get("no") or 0))
    out = [head]
    for c in bad[:MAX_CC]:
        txt = _FORMULA.sub(" — ", str(c.get("text") or "")).replace("S/V = ", "S/V ")
        out.append(f"{c.get('label') or c.get('code')}: {_short(txt, 150)}")
    return out


def _below_line(act: dict) -> Optional[str]:
    """Запрошенная ставка ниже минимума (01.10.2026): ответ, причина и недобор премии одной строкой."""
    lang = act["lang"]
    bm = act.get("below_min_assessment") or {}
    if not bm.get("available"):
        return None
    v = bm.get("verdict")
    ans = bm.get("verdict_label") or t({"not_allowed": "cp_below_no",
                                       "allowed_with_conditions": "cp_below_cond"}.get(v, "cp_below_yes"), lang)
    reasons = bm.get("reasons") or []
    if v == "not_allowed":
        why = [x["text"] for x in reasons if x.get("effect") in ("blocks", "no_conditions")]
    elif v == "allowed_with_conditions":
        why = [c["text"] for c in bm.get("conditions") or []] or [x["text"] for x in reasons]
    else:
        why = [x["text"] for x in reasons if x.get("sign") == "plus"] or [x["text"] for x in reasons]
    return t("cp_below", lang, req=pct(bm.get("requested_pct"), lang), min=pct(bm.get("min_pct"), lang),
             answer=ans, why=_short(why[0] if why else "", 120).rstrip("."),
             short=bm.get("shortfall_text") or money(bm.get("shortfall"), lang))


def _s4(act: dict, dec: dict, detail: int) -> dict:
    lang = act["lang"]
    return {"n": 4, "title": t("a2_s4", lang), "verdict": dec, "rate_line": _rate_line(act),
            "below": _below_line(act),
            "explain": _explain(act), "chain": _chain(act, detail),
            "chain_head": [t("a2_ch_step", lang), t("a2_ch_rate", lang), t("a2_ch_premium", lang)],
            "chain_title": t("a2_chain", lang), "table": _parts_table(act), "contract": _cc_lines(act)}


# --------------------------------------------------------------------------- #
#  5. Условия
# --------------------------------------------------------------------------- #

def _measure_effect(m: dict, lang: str) -> str:
    if m.get("premium_delta"):
        return t("cp_ms_eff", lang, pct=tx.pct_fixed(abs(m.get("effect_pct") or 0), lang, 1),
                 sign="−" if (m.get("effect_pct") or 0) < 0 else "+", delta=money(abs(m["premium_delta"]), lang))
    if m.get("effect_pct"):
        return t("cp_ms_eff_pct", lang, pct=tx.pct_fixed(abs(m["effect_pct"]), lang, 1),
                 sign="−" if m["effect_pct"] < 0 else "+")
    return t("cp_ms_eff_none", lang)


def _s5(act: dict, detail: int) -> dict:
    lang = act["lang"]
    n, ln = MAX_TERMS[1 if detail else 0], MAX_TERM_LEN[1 if detail else 0]
    items = []
    dmg = (act.get("inspection") or {}).get("damages") or []
    if dmg:
        lst = ", ".join(str(d.get("what") or "") + (f" ({d['where']})" if d.get("where") else "") for d in dmg)
        items.append(t("a2_c_excl_damage", lang, list=_short(lst, ln - 30)))
    fr = act.get("franchise") or {}
    if fr.get("needed") or fr.get("status") in ("applied", "proposed", "statutory"):
        items.append(_franchise_value(act))
    clauses = [_short(c.get("text"), ln).rstrip(".") for c in act.get("clauses") or [] if c.get("text")]
    measures = []
    for m in act.get("measures") or []:
        txt = str(m.get("text") or "").rstrip(".")
        if m.get("deadline_days") and not m.get("deadline_default"):
            txt += t("a2_m_deadline", lang, d=m["deadline_days"])
        measures.append(t("a2_c_measure", lang, text=_short(txt, ln - 35), effect=_measure_effect(m, lang)))
    # оговорки и мероприятия поровну: сначала по одной, затем остальное
    room = n - len(items)
    take_c = min(len(clauses), max(1, (room + 1) // 2)) if clauses else 0
    take_m = min(len(measures), room - take_c)
    take_c = min(len(clauses), room - take_m)
    items += clauses[:take_c] + measures[:take_m]
    if not items:
        items.append(t("a2_c_none", lang))
    return {"n": 5, "title": t("a2_s5", lang), "items": items[:n]}


# --------------------------------------------------------------------------- #
#  Документ
# --------------------------------------------------------------------------- #

def how_url(act: dict, extras: Optional[dict] = None) -> str:
    """Ссылка «Как посчитано» (GET /act/{id}?how=1): с адресом сервера, если он задан (SERVER_URL)."""
    base = str((extras or {}).get("server_url") or "").rstrip("/")
    return f"{base}/act/{act.get('id')}?how=1&lang={act['lang']}"


def compact_view(act: dict, extras: Optional[dict] = None, detail: int = 0) -> dict:
    """Документ на две страницы из ответа render. extras — данные вне акта (export.pdf_extras): филиал и номер
    сотрудника, тариф, адрес сервера. detail — 0 полный, 1 короче факторы."""
    lang = act["lang"]
    extras = extras or {}
    dec = decision(act)
    head = t("a2_head", lang, number=act.get("number"), date=act.get("date"))
    iss = _issuer_text(act, extras)
    return {"lang": lang, "title": act["title"], "head": head + (" · " + iss if iss else ""),
            "brand": "INSON", "tariff": _tariff_text(act, extras), "decision": dec, "detail": detail,
            "s1": _s1(act, detail), "s2": _s2(act), "s3": _s3(act, detail), "s4": _s4(act, dec, detail),
            "s5": _s5(act, detail),
            "page2": {"title": t("a2_photos", lang), "head": head,
                      "sign": [t("a2_sign_surveyor", lang), t("a2_sign_holder", lang)],
                      "sign_name": t("a2_sign_name", lang), "qr_text": t("a2_qr_text", lang),
                      "how_url": how_url(act, extras), "no_photos": t("a2_ph_none", lang)},
            "sign": t("cp_sign", lang), "calibrated": 0}
