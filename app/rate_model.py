"""
Модель ставки по разделу 3 ТЗ разработчику от 13.10.2026 (дни 3–4, задача 3.1): одна формула для акта,
калькулятора и аналитики риска.

Формулы (все проценты — годовые, % страховой суммы):
  r_опыт  = Σ выплат × индекс цен / Σ (страховая сумма × доля года) × 100           — опыт INSON по классу;
  Z       = min(1; √(N / n_full)), N — число убытков; Z = 0 при N < min_claims или договорах < min_contracts;
  r_рын   = выплаты рынка за год / страховые обязательства класса × 100 (НАПП, app/market_picture.py;
            срез «01.01» — итог прошлого года); нет r_рын → тариф INSON × (1 − L) с пометкой «временно»;
  r_нетто = (Z · r_опыт + (1 − Z) · r_рын) × доля включённых рисков (у классов 8, 9 — справочник perils);
  θ       = max(θ_min; 1,2 · α(γ) · √((1 − q) / (n · q))) — app/calibration.risk_load (методика
            Росстрахнадзора, γ = 0,9 → α = 1,3); нет своих n, q → θ по умолчанию: 0,10 для классов с ≥ 1 000
            договоров на рынке, 0,20 — для классов из списка small_classes (Аналитика 13.10.2026, табл. 2, «*»);
  c       = катастрофическая надбавка классов 8, 9 по сейсмозоне: ≤ 6 — 0; 7 — 0,03; 8 — 0,05; ≥ 9 — 0,10;
            зона не указана — unknown_zone (временно); землетрясение исключено из покрытия — 0;
  L       = доля расходов на ведение дела (act_settings.load, актуарный отчёт 2022, временно);
  r_тех   = r_брутто = r_нетто · (1 + θ + c) / (1 − L);
  K_об    = Π коэффициентов групп факторов объекта, в границах [0,5; 2,5], неизвестное = 1;
  K_рег   = 1,00 (до данных об убытках по регионам);
  K_кл    = по убыткам клиента за 3 года: неизвестно 1,00; 0 — 0,95; 1 — 1,10; 2 — 1,25; ≥ 3 — «На рассмотрение»;
  r       = clip(r_брутто · K_об · K_рег · K_кл; r_нетто; 3 × тариф), затем r = max(минимум INSON; r);
            сверх 3 × тарифа — ставка = 3 × тариф и решение «На рассмотрение»;
  P       = S · r / 100 · t / 365 (премия — от неокруглённой ставки; ставка в акте показывается округлённой).

Трактовки (зафиксированы 13.10.2026, задача 3.1):
  1. «Z = 0 при N < 5 или < 30 договорах»: условие по договорам проверяется, только если число договоров
     известно. Эталон ТЗ (6 убытков, договоры не заданы) даёт Z = √(6/1082) = 0,0745 ≈ 0,074, значит
     неизвестное число договоров считается достаточным. Число убытков (N) обязательно: нет — Z = 0.
  2. Нижняя граница clip — r_нетто (строка формулы ТЗ «clip(r·K·K·K; r; 3×тариф)» и приёмка «r ≥ max(минимум,
     нетто)»): ставка не опускается ниже ожидаемого убытка даже при K_об = 0,5; минимум INSON — отдельно сверху.
  3. «Тариф» для 3 × тарифа и запасного r_рын — ставка продукта по тарифной политике INSON (минимум компании
     из min_rates / product_parts на дату); тарифа нет (по согласованию, по программе) — верхней границы нет,
     а запасной r_нетто — базовая нетто-ставка справочника base_rates (экспертная, calibrated = 0).
  4. Убыточность рынка выше max_loss_ratio_pct (кл. 10: выплаты в 14 раз больше премий — хвост прошлых лет)
     — r_рын не применяется (Аналитика 13.10.2026: «расчёт не применять»), берётся запасной вариант.
  5. Группы факторов, которые уже есть в формуле отдельным множителем, в K_об не входят: убытки клиента
     (loss_history, liab_claims) — это K_кл; сейсмозона (seismic) — это c. Франшиза договора — условие
     договора: в акте её считает блок франшизы, в калькуляторе — множитель coefficients.franchise.
  6. Индекс цен в r_опыт пока 1,00 (временно): индексов по классам у INSON нет.

Каждое число результата лежит в trace: {step, value, source, date, note}. Все константы — в
act_settings.rate_model и act_settings.load (с версией и датой), здесь — только их значения по умолчанию.
Экспертные значения помечены calibrated = 0 (правило проекта № 7).
"""
import json
import math
from datetime import date, timedelta
from typing import Optional

CALIBRATED = 0

# --------------------------------------------------------------------------------------------------------------
# Значения по умолчанию (поверх них — правки администратора в act_settings, ключи rate_model и load)
# --------------------------------------------------------------------------------------------------------------
DEFAULTS = {
    "enabled": True,
    "version": "1.0",
    "date": "2026-10-13",
    "source": "ТЗ разработчику 13.10.2026, раздел 3; Аналитика по 18 классам 13.10.2026",
    "calibrated": 0,
    # теория доверия: Z = min(1; √(N / n_full)); меньше min_claims убытков или min_contracts договоров — Z = 0
    "credibility": {"n_full": 1082, "min_claims": 5, "min_contracts": 30},
    # опыт INSON: убытки за years лет до даты расчёта (claims + договоры calibration.exposure_and_losses)
    "experience": {"years": 3, "price_index": 1.0,
                   "price_index_note": "временно: индекс цен по классам не задан, принят 1,00"},
    # рынок НАПП: full_year — итог года (срез 01.01), иначе последний срез, приведённый к году
    "market": {"basis": "full_year", "max_loss_ratio_pct": 200},
    # рисковая надбавка θ: формула calibration.risk_load при своих n, q; иначе — по умолчанию
    "theta": {"gamma": 0.9, "floor": 0.05, "default_large": 0.10, "default_small": 0.20,
              "large_from_contracts": 1000, "small_classes": ["4", "5", "6", "11", "12", "15", "17"],
              "small_source": "Аналитика по 18 классам 13.10.2026, табл. 2 (пометка «*»: меньше 1 000 договоров)"},
    # катастрофическая надбавка c (классы 8, 9): [[зона от, c], ...] — берётся строка с наибольшей «зоной от» ≤ зоны
    "cat": {"classes": ["8", "9"], "zones": [[6, 0.0], [7, 0.03], [8, 0.05], [9, 0.10]], "unknown_zone": 0.05,
            "unknown_note": "временно: сейсмозона не указана — принята надбавка зоны 8 баллов"},
    "k_region": 1.0,
    # K_кл по числу убытков за 3 года: by_count[0] — 0 убытков, [1] — 1, [2] — 2; refer_from и больше — на рассмотрение
    "k_client": {"unknown": 1.0, "by_count": [0.95, 1.10, 1.25], "refer_from": 3},
    # K_об: границы произведения; exclude — группы, которые уже есть в формуле (K_кл, c, франшиза)
    "k_object": {"min": 0.5, "max": 2.5, "exclude": ["loss_history", "liab_claims", "seismic", "franchise"],
                 "engine_exclude": ["loss_history", "liab_claims", "seismic"]},
    "cap_tariff_mult": 3.0,
    # уровень риска акта — из K_об
    "risk_level": {"low_max": 0.95, "high_min": 1.2},
    # пояснение ставки: факторы с влиянием не меньше min_effect_pct, не больше max_factors
    "explain": {"min_effect_pct": 3, "max_factors": 4},
    # единый справочник факторов (шаблон класса + coefficients): одинаковые по смыслу группы coefficients,
    # которые уже есть в шаблоне, не добавляются {класс: {код coefficients: код группы шаблона}}
    "catalog_same": {"3": {"antitheft": "protection", "drivers": "drivers_profile", "veh_type": "veh_group",
                           "spec_site": "location", "spec_guard": "location"},
                     "7": {"cargo_nature": "cargo_group", "cargo_mode": "transport_mode", "cargo_pack": "packaging"},
                     "8": {"activity": "object_group", "wear": "wear_level"},
                     "13": {"liab_activity": "activity_kind"},
                     "14": {"credit_industry": "borrower_industry"}},
    # значения групп coefficients из чисел акта: [[верхняя граница включительно, вариант], ...], выше — above
    "derive": {"engine_hours": {"fields": ["engine_hours", "moto_hours"], "kind": "value",
                                "bounds": [[2000, "eh_low"], [6000, "eh_mid"], [12000, "eh_high"]], "above": "eh_max"},
               "veh_age": {"fields": ["year"], "kind": "age",
                           "bounds": [[3, "a3"], [7, "a7"], [15, "a15"]], "above": "a15p"}},
}

LOAD_DEFAULTS = {
    "version": "2022",
    "date": "2026-10-13",
    "source": "Актуарный отчёт INSON за 2022 год (запасные значения раздела 2 ТЗ 13.10.2026)",
    "status": "временно",
    "calibrated": 0,
    # доля расходов на ведение дела по субпортфелям (комиссия входит в долю; отдельно — справочно)
    "by_group": {"property": 0.729, "vehicle": 0.591, "financial": 0.625, "other": 0.703},
    "commission": 0.10,
    # класс → субпортфель; классы вне списка — default_group (по Аналитике 13.10.2026, табл. 2)
    "class_group": {"3": "vehicle", "4": "vehicle", "5": "property", "6": "property", "7": "property",
                    "8": "property", "9": "property", "14": "financial", "15": "financial", "16": "financial",
                    "13з": "financial", "16у": "property"},
    "default_group": "other",
}


def _merge(base: dict, patch: Optional[dict]) -> dict:
    out = {k: (_merge(v, {}) if isinstance(v, dict) else (list(v) if isinstance(v, list) else v))
           for k, v in (base or {}).items()}
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = _merge(v, {}) if isinstance(v, dict) else (list(v) if isinstance(v, list) else v)
    return out


def settings(st: Optional[dict]) -> dict:
    """Настройки модели: act_settings.rate_model поверх DEFAULTS."""
    raw = (st or {}).get("rate_model") if isinstance((st or {}).get("rate_model"), dict) else {}
    return _merge(DEFAULTS, raw)


def load_settings(st: Optional[dict]) -> dict:
    """Нагрузка: act_settings.load поверх LOAD_DEFAULTS."""
    raw = (st or {}).get("load") if isinstance((st or {}).get("load"), dict) else {}
    return _merge(LOAD_DEFAULTS, raw)


def check_settings(st: Optional[dict]) -> list:
    """Ошибки правки администратора (act_settings.rate_model и load): пустой список — всё верно."""
    errs = []
    cfg, ld = settings(st), load_settings(st)
    if not isinstance(cfg.get("enabled"), bool):
        errs.append("rate_model.enabled: true или false")
    if cfg.get("calibrated") != 0:
        errs.append("rate_model.calibrated: 0 — значения экспертные, не калиброваны")

    def num(path, v, lo, hi):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= float(v) <= hi:
            errs.append(f"{path}: число от " + f"{lo:g} до {hi:g}".replace(".", ","))
    try:
        num("rate_model.credibility.n_full", cfg["credibility"]["n_full"], 1, 1e7)
        num("rate_model.theta.floor", cfg["theta"]["floor"], 0, 1)
        num("rate_model.theta.default_large", cfg["theta"]["default_large"], 0, 1)
        num("rate_model.theta.default_small", cfg["theta"]["default_small"], 0, 1)
        num("rate_model.cat.unknown_zone", cfg["cat"]["unknown_zone"], 0, 1)
        for z, c in cfg["cat"]["zones"]:
            num("rate_model.cat.zones", c, 0, 1)
        num("rate_model.k_region", cfg["k_region"], 0.1, 10)
        for k in cfg["k_client"]["by_count"]:
            num("rate_model.k_client.by_count", k, 0.1, 10)
        num("rate_model.k_object.min", cfg["k_object"]["min"], 0.1, 1)
        num("rate_model.k_object.max", cfg["k_object"]["max"], 1, 10)
        num("rate_model.cap_tariff_mult", cfg["cap_tariff_mult"], 1, 100)
        for g, v in ld["by_group"].items():
            num(f"load.by_group.{g}", v, 0, 0.95)
        num("load.commission", ld["commission"], 0, 0.95)
    except (KeyError, TypeError, ValueError):
        errs.append("rate_model / load: нарушена структура настроек")
    return errs


def enabled(st: Optional[dict]) -> bool:
    return bool(settings(st).get("enabled", True))


def db_settings(con) -> dict:
    """{"rate_model", "load"} из последней версии act_settings (таблицы нет — значения по умолчанию)."""
    custom = {}
    try:
        r = con.execute("SELECT settings_json FROM act_settings ORDER BY id DESC LIMIT 1").fetchone()
        if r:
            custom = json.loads(r[0] or "{}")
    except Exception:
        custom = {}
    return {"rate_model": settings(custom), "load": load_settings(custom)}


# --------------------------------------------------------------------------------------------------------------
# Чистые формулы
# --------------------------------------------------------------------------------------------------------------

def experience_rate(losses: Optional[float], exposure: Optional[float], price_index: float = 1.0) -> Optional[float]:
    """r_опыт, %: Σ выплат × индекс цен / Σ (сумма × доля года) × 100. 180 млн / 120 млрд → 0,15 %."""
    if losses is None or not exposure or exposure <= 0:
        return None
    return float(losses) * float(price_index) / float(exposure) * 100


def credibility(n_claims: Optional[int], n_contracts: Optional[int], cfg: dict) -> tuple:
    """(Z, причина). Z = min(1; √(N/n_full)); Z = 0 при N < min_claims или известном числе договоров
    < min_contracts (трактовка 1: неизвестное число договоров не обнуляет Z). 6 убытков → 0,0745."""
    c = cfg["credibility"]
    if n_claims is None:
        return 0.0, "no_data"
    if n_claims < int(c["min_claims"]):
        return 0.0, "few_claims"
    if n_contracts is not None and n_contracts < int(c["min_contracts"]):
        return 0.0, "few_contracts"
    return min(1.0, math.sqrt(float(n_claims) / float(c["n_full"]))), None


def theta_value(cls: str, cfg: dict, n: Optional[int] = None, q: Optional[float] = None) -> tuple:
    """(θ, источник, примечание). Свои n, q (не меньше min_contracts договоров и хотя бы один убыток) —
    max(θ_min; 1,2·α(γ)·√((1−q)/(n·q))) по calibration.risk_load; иначе θ по умолчанию для класса."""
    from . import calibration as cal
    t = cfg["theta"]
    if n is not None and q is not None and n >= int(cfg["credibility"]["min_contracts"]) and 0 < q < 1:
        tr = cal.risk_load(1.0, q, int(n), float(t["gamma"]))
        if tr is not None:
            return max(float(t["floor"]), tr), "formula", (
                f"1,2 × α({t['gamma']}) × √((1 − {q:.4f}) / ({n} × {q:.4f})), не ниже {t['floor']}")
    small = str(cls) in [str(x) for x in t.get("small_classes") or []]
    if small:
        return float(t["default_small"]), "default_small", t.get("small_source")
    return float(t["default_large"]), "default_large", (
        f"класс с ≥ {t['large_from_contracts']} договоров на рынке: θ по умолчанию")


def cat_load(cls: str, zone: Optional[int], cfg: dict, quake_covered: bool = True) -> tuple:
    """(c, примечание). Только классы cat.classes; ≤ 6 — 0; 7 — 0,03; 8 — 0,05; ≥ 9 — 0,10."""
    c = cfg["cat"]
    if str(cls) not in [str(x) for x in c["classes"]]:
        return 0.0, "class_not_cat"
    if not quake_covered:
        return 0.0, "quake_excluded"
    if zone is None:
        return float(c["unknown_zone"]), "zone_unknown"
    rows = sorted(([float(z), float(v)] for z, v in c["zones"]), key=lambda x: x[0])
    val = rows[0][1]
    for z, v in rows:
        if float(zone) >= z:
            val = v
    return val, None


def load_share(cls: str, load: dict) -> tuple:
    """(L, субпортфель) по классу."""
    grp = (load.get("class_group") or {}).get(str(cls))
    if grp is None:
        base = str(cls).rstrip("зу")
        grp = (load.get("class_group") or {}).get(base) or load.get("default_group") or "other"
    return float(load["by_group"][grp]), grp


def k_client(losses_count: Optional[int], cfg: dict) -> tuple:
    """(K_кл, на рассмотрение?). Неизвестно — 1,00; 0 — 0,95; 1 — 1,10; 2 — 1,25; ≥ 3 — на рассмотрение."""
    k = cfg["k_client"]
    if losses_count is None:
        return float(k["unknown"]), False
    n = int(losses_count)
    by = [float(x) for x in k["by_count"]]
    refer = n >= int(k["refer_from"])
    return by[min(n, len(by) - 1)], refer


def risk_level(k_object: Optional[float], cfg: dict) -> str:
    """Уровень риска из K_об: ≤ 0,95 — low; ≥ 1,2 — high; иначе moderate."""
    rl = cfg["risk_level"]
    k = 1.0 if k_object is None else float(k_object)
    if k <= float(rl["low_max"]) + 1e-12:
        return "low"
    if k >= float(rl["high_min"]) - 1e-12:
        return "high"
    return "moderate"


def premium(rate_pct: float, sum_insured: float, term_days: int) -> float:
    """P = S · r / 100 · t / 365."""
    return float(sum_insured) * float(rate_pct) / 100 * int(term_days) / 365


def _r6(x):
    return None if x is None else round(float(x), 6)


def compute(inp: dict, st: Optional[dict] = None) -> dict:
    """
    Ставка по разделу 3 ТЗ. inp (все ключи, кроме class_code, необязательны):
      class_code, sum_insured, term_days (365), as_of (дата строкой),
      experience {losses, exposure, n_claims, n_contracts, period, source, date} — опыт INSON;
      market {r_pct, period, date, source, row_key} — r_рын НАПП;
      tariff_pct — тариф INSON (ставка продукта), min_pct — минимум INSON (и регулятора), base_net_pct — запасная
      экспертная нетто-ставка (base_rates), peril_share — доля включённых рисков (1 = все), quake_covered;
      theta, c, L — явные значения (эталон, ручная правка); seismic_zone;
      k_object_factors [{code, label, coef, source}] или k_object (число); k_region; losses_count.
    Возвращает словарь с r (применённая ставка), premium, всеми промежуточными величинами, factors (вклад
    каждого множителя в сумах), refer (почему «На рассмотрение»), risk_level и trace.
    """
    cfg = settings(st)
    ld = load_settings(st)
    cls = str(inp.get("class_code") or "")
    S = float(inp.get("sum_insured") or 0)
    term = int(inp.get("term_days") or 365)
    as_of = str(inp.get("as_of") or date.today().isoformat())
    cfg_date = cfg.get("date")
    trace, refer, notes = [], [], []

    def step(name, value, source, d=None, note=None, **extra):
        row = {"step": name, "value": value, "source": source, "date": d, "note": note}
        row.update(extra)
        trace.append(row)

    # ---- опыт INSON и вес Z
    exp = inp.get("experience") or None
    r_exp = None
    if exp:
        idx = float(exp.get("price_index", cfg["experience"]["price_index"]))
        r_exp = experience_rate(exp.get("losses"), exp.get("exposure"), idx)
        if r_exp is not None:
            step("r_exp", _r6(r_exp), exp.get("source") or "убытки INSON (claims)", exp.get("date"),
                 f"Σ выплат {round(float(exp['losses'])):,} × индекс {idx:g} / экспозиция "
                 f"{round(float(exp['exposure'])):,} сум·лет".replace(",", " "),
                 period=exp.get("period"))
    z, z_why = credibility(exp.get("n_claims") if exp and r_exp is not None else None,
                           exp.get("n_contracts") if exp else None, cfg)
    step("Z", round(z, 6), "теория доверия, act_settings.rate_model.credibility", cfg_date,
         {"few_claims": f"убытков меньше {cfg['credibility']['min_claims']} — Z = 0",
          "few_contracts": f"договоров меньше {cfg['credibility']['min_contracts']} — Z = 0",
          "no_data": "выгрузки убытков INSON нет — Z = 0 (раздел 2 ТЗ)"}.get(z_why)
         or f"√({exp.get('n_claims') if exp else 0} / {cfg['credibility']['n_full']})")

    # ---- нагрузка (нужна и запасному r_рын)
    if inp.get("L") is not None:
        L, lgrp = float(inp["L"]), None
        step("L", L, "задано явно", as_of)
    else:
        L, lgrp = load_share(cls, ld)
        step("L", L, ld.get("source"), ld.get("date"), f"субпортфель {lgrp}; {ld.get('status') or ''}".strip("; "),
             calibrated=CALIBRATED)

    # ---- рынок или запасной вариант
    tariff = inp.get("tariff_pct")
    mk = inp.get("market") or None
    r_mkt, mkt_kind = None, None
    if mk and mk.get("r_pct") is not None and float(mk["r_pct"]) > 0:
        # выплат рынка нет (r_рын = 0, например кл. 12: три договора на рынке) — данных нет, запасной вариант
        r_mkt, mkt_kind = float(mk["r_pct"]), "market"
        step("r_market", _r6(r_mkt), mk.get("source") or "НАПП", mk.get("date"),
             mk.get("note") or "выплаты / страховые обязательства класса за год", period=mk.get("period"),
             row_key=mk.get("row_key"))
    elif z < 1:
        if tariff is not None:
            r_mkt, mkt_kind = float(tariff) * (1 - L), "tariff"
            step("r_market", _r6(r_mkt), "тариф INSON × (1 − L)", ld.get("date"),
                 f"временно: рыночной частоты нет; {float(tariff):g} × (1 − {L:g})", calibrated=CALIBRATED)
        elif inp.get("base_net_pct") is not None:
            r_mkt, mkt_kind = float(inp["base_net_pct"]), "base"
            step("r_market", _r6(r_mkt), "базовая нетто-ставка справочника base_rates", None,
                 "экспертно, calibrated = 0: нет ни рынка, ни тарифа", calibrated=CALIBRATED)
    if r_exp is None and r_mkt is None:
        return {"available": False, "reason": "no_basis", "trace": trace, "calibrated": CALIBRATED,
                "version": cfg.get("version"), "date": cfg_date}
    blend = (z * (r_exp or 0.0)) + (1 - z) * (r_mkt if r_mkt is not None else (r_exp or 0.0))
    share = float(inp.get("peril_share") if inp.get("peril_share") is not None else 1.0)
    r_net = blend * share
    step("r_net", _r6(r_net), "Z · r_опыт + (1 − Z) · r_рын", as_of,
         f"{z:.4f} × {_r6(r_exp) if r_exp is not None else 0} + {1 - z:.4f} × {_r6(r_mkt)}"
         + (f"; доля включённых рисков {share:.3f}" if abs(share - 1) > 1e-9 else ""))

    # ---- надбавки и брутто
    if inp.get("theta") is not None:
        theta, th_src, th_note = float(inp["theta"]), "given", "задано явно"
    else:
        n = (exp or {}).get("n_contracts")
        q = ((exp or {}).get("n_claims") / n) if exp and n and exp.get("n_claims") is not None else None
        theta, th_src, th_note = theta_value(cls, cfg, n, q)
    step("theta", round(theta, 6), "рисковая надбавка (calibration.risk_load)" if th_src == "formula"
         else "act_settings.rate_model.theta", cfg_date, th_note, calibrated=CALIBRATED)
    if inp.get("c") is not None:
        c, c_why = float(inp["c"]), "given"
    else:
        c, c_why = cat_load(cls, inp.get("seismic_zone"), cfg, inp.get("quake_covered", True))
    step("c", round(c, 6), "act_settings.rate_model.cat", cfg_date,
         {"zone_unknown": cfg["cat"].get("unknown_note"), "class_not_cat": "катастрофическая надбавка — только классы "
          + ", ".join(map(str, cfg["cat"]["classes"])), "quake_excluded": "землетрясение исключено из покрытия",
          "given": "задано явно"}.get(c_why) or f"сейсмозона {inp.get('seismic_zone')} баллов",
         calibrated=CALIBRATED)
    r_gross = r_net * (1 + theta + c) / (1 - L)
    step("r_gross", _r6(r_gross), "r_нетто · (1 + θ + c) / (1 − L)", as_of,
         f"{_r6(r_net)} × (1 + {theta:g} + {c:g}) / (1 − {L:g})")

    # ---- множители
    kc = cfg["k_object"]
    facs = [f for f in (inp.get("k_object_factors") or []) if f and f.get("coef") is not None]
    if facs:
        k_raw = math.prod(float(f["coef"]) for f in facs)
    else:
        k_raw = float(inp["k_object"]) if inp.get("k_object") is not None else 1.0
    k_obj = min(float(kc["max"]), max(float(kc["min"]), k_raw))
    step("K_object", round(k_obj, 6), "факторы объекта (шаблон класса + coefficients)", cfg_date,
         " × ".join(f"{float(f['coef']):g}" for f in facs) + (f"; в границах {kc['min']}–{kc['max']}"
                                                                if abs(k_obj - k_raw) > 1e-12 else "")
         if facs else "факторы не заполнены — 1,00", calibrated=CALIBRATED)
    k_reg = float(inp["k_region"]) if inp.get("k_region") is not None else float(cfg["k_region"])
    step("K_region", k_reg, "act_settings.rate_model.k_region", cfg_date,
         "до данных об убытках по регионам — 1,00" if inp.get("k_region") is None else None, calibrated=CALIBRATED)
    if inp.get("k_client") is not None:
        k_cl, cl_refer = float(inp["k_client"]), False
        step("K_client", k_cl, "задано явно", as_of)
    else:
        k_cl, cl_refer = k_client(inp.get("losses_count"), cfg)
        step("K_client", k_cl, "act_settings.rate_model.k_client", cfg_date,
             "убытки за 3 года неизвестны" if inp.get("losses_count") is None
             else f"убытков за 3 года: {inp.get('losses_count')}", calibrated=CALIBRATED)
    if cl_refer:
        refer.append({"code": "losses", "params": {"n": inp.get("losses_count")}})
    k_total = k_obj * k_reg * k_cl
    r_raw = r_gross * k_total

    # ---- границы и минимум
    cap = float(tariff) * float(cfg["cap_tariff_mult"]) if tariff is not None else None
    # сначала верхняя граница (сверх 3 × тарифа — «на рассмотрение»), затем нижняя: ставка не ниже r_нетто, даже
    # если 3 × тарифа меньше ожидаемого убытка (приёмка ТЗ: r ≥ max(минимум, нетто))
    r_calc, bound = r_raw, None
    if cap is not None and r_calc > cap + 1e-12:
        r_calc, bound = cap, "cap"
        refer.append({"code": "cap", "params": {"rate": _r6(r_raw), "cap": _r6(cap)}})
    if r_calc < r_net:
        r_calc, bound = r_net, ("cap_net" if bound == "cap" else "net")
    min_pct = inp.get("min_pct")
    r = r_calc
    min_applied = False
    if min_pct is not None and r + 1e-12 < float(min_pct):
        r, min_applied = float(min_pct), True
    step("r_calc", _r6(r_calc), "clip(r_брутто · K_об · K_рег · K_кл; r_нетто; 3 × тариф)", as_of,
         f"{_r6(r_gross)} × {k_obj:g} × {k_reg:g} × {k_cl:g} = {_r6(r_raw)}"
         + {"net": "; поднята до r_нетто", "cap": f"; ограничена 3 × тарифа = {_r6(cap)}",
            "cap_net": f"; 3 × тарифа = {_r6(cap)} ниже r_нетто — взята r_нетто"}.get(bound, ""))
    step("r", _r6(r), "max(минимум INSON; r)", as_of,
         (f"применён минимум INSON {float(min_pct):g}%" if min_applied else
          (f"минимум INSON {float(min_pct):g}% соблюдён" if min_pct is not None else "минимума нет")))
    P = premium(r, S, term)
    step("premium", round(P), "S · r / 100 · t / 365", as_of, f"{S:,.0f} × {_r6(r)}% × {term}/365".replace(",", " "))

    # ---- вклад множителей в сумах (по цепочке от r_брутто)
    factors, prev_r = [], r_gross
    chain = [dict(f, kind="object") for f in facs]
    if abs(k_obj - k_raw) > 1e-12:
        chain.append({"code": "k_object_bound", "label": None, "coef": k_obj / k_raw, "kind": "bound"})
    chain.append({"code": "k_region", "label": None, "coef": k_reg, "kind": "region"})
    chain.append({"code": "k_client", "label": None, "coef": k_cl, "kind": "client",
                  "losses_count": inp.get("losses_count")})
    for f in chain:
        nr = prev_r * float(f["coef"])
        factors.append({"code": f.get("code"), "group": f.get("group"), "label": f.get("label"),
                        "group_label": f.get("group_label"), "kind": f["kind"],
                        "coef": round(float(f["coef"]), 6), "effect_pct": round((float(f["coef"]) - 1) * 100, 2),
                        "rate_after_pct": _r6(nr), "premium": round(premium(nr, S, term)),
                        "premium_delta": round(premium(nr, S, term) - premium(prev_r, S, term)),
                        "source": f.get("source"), "calibrated": CALIBRATED})
        prev_r = nr

    level = risk_level(k_obj, cfg)
    return {"available": True, "class_code": cls, "version": cfg.get("version"), "date": cfg_date,
            "r_exp": _r6(r_exp), "Z": round(z, 6), "z_reason": z_why, "r_market": _r6(r_mkt),
            "r_market_kind": mkt_kind, "r_net": _r6(r_net), "peril_share": share,
            "theta": theta, "theta_source": th_src, "c": c, "c_reason": c_why, "L": L, "load_group": lgrp,
            "r_gross": _r6(r_gross), "r_tech": _r6(r_gross), "premium_gross": round(premium(r_gross, S, term)),
            "k_object": round(k_obj, 6), "k_object_raw": round(k_raw, 6), "k_region": k_reg, "k_client": k_cl,
            "k_total": round(k_total, 6), "r_raw": _r6(r_raw), "bound": bound, "cap_pct": _r6(cap),
            "tariff_pct": tariff, "min_pct": min_pct, "min_applied": min_applied,
            "r_calc": _r6(r_calc), "r_exact": r, "r": _r6(r), "premium": round(P), "premium_exact": P,
            "sum_insured": S, "term_days": term, "losses_count": inp.get("losses_count"),
            "seismic_zone": inp.get("seismic_zone"), "risk_level": level, "refer": refer,
            "factors": factors, "trace": trace, "market": mk, "experience": exp, "notes": notes,
            "calibrated": CALIBRATED}


# --------------------------------------------------------------------------------------------------------------
# Единый справочник факторов: группы шаблона класса + коэффициенты coefficients (одна функция чтения)
# --------------------------------------------------------------------------------------------------------------

def factor_catalog(template_groups: Optional[list], coef_rows: list, cls: str, st: Optional[dict] = None,
                   engine: bool = False) -> list:
    """
    Группы факторов класса без дублей: сначала группы шаблона (factor_groups), затем группы coefficients того же
    класса, которых в шаблоне нет ни по коду, ни по смыслу (catalog_same), и которые не учтены в формуле отдельно
    (k_object.exclude: убытки → K_кл, сейсмозона → c, франшиза → блок франшизы). coef_rows — строки таблицы
    coefficients {factor_code, factor_name, class_code, option_code, option_name, multiplier, calibrated}.
    Группа: {code, label, input, options: [{code, label, coef}], source: template | coefficients, derive?}.
    """
    cfg = settings(st)
    excl = set(cfg["k_object"]["engine_exclude" if engine else "exclude"])
    same = (cfg.get("catalog_same") or {}).get(str(cls)) or {}
    out, seen = [], set()
    for g in template_groups or []:
        if isinstance(g, dict) and g.get("code") and g["code"] not in seen and g["code"] not in excl:
            out.append(dict(g, source="template"))
            seen.add(g["code"])
    by_code = {}
    for r in coef_rows or []:
        if str(r.get("class_code")) != str(cls):
            continue
        by_code.setdefault(r["factor_code"], []).append(r)
    derive = cfg.get("derive") or {}
    for code, rows in by_code.items():
        if code in excl or code in seen or same.get(code) in seen:
            continue
        g = {"code": code, "label": {"ru": rows[0].get("factor_name") or code}, "source": "coefficients",
             "input": "optional.class_fields." + code,
             "options": [{"code": r["option_code"], "label": {"ru": r.get("option_name") or r["option_code"]},
                          "coef": float(r["multiplier"]), "calibrated": int(r.get("calibrated") or 0)}
                         for r in rows]}
        if code in derive:
            g["derive"] = derive[code]
        out.append(g)
        seen.add(code)
    return out


def derive_option(rule: dict, class_fields: dict, refinements: dict, today: Optional[date] = None) -> Optional[str]:
    """Вариант группы из числа акта: моточасы 6 500 → eh_high; год 2021 → возраст 5 лет → a7."""
    v = None
    for f in rule.get("fields") or []:
        v = class_fields.get(f) if class_fields.get(f) not in (None, "") else refinements.get(f)
        if v not in (None, ""):
            break
    try:
        x = float(str(v).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None
    if rule.get("kind") == "age":
        x = (today or date.today()).year - int(x)
        if x < 0:
            return None
    for bound, opt in rule.get("bounds") or []:
        if x <= float(bound):
            return opt
    return rule.get("above")


def engine_k_object(coefficients: dict, factors: dict, class_factor_codes: list, st: Optional[dict] = None) -> list:
    """Факторы калькулятора (engine.Input.factors) для K_об: коэффициенты класса без групп, учтённых в формуле
    отдельно (engine_exclude). Возвращает [{code, label, coef, source}]."""
    cfg = settings(st)
    excl = set(cfg["k_object"]["engine_exclude"])
    out = []
    for f in class_factor_codes:
        if f in excl:
            continue
        opt = coefficients.get((f, factors.get(f, "")))
        if not opt:
            continue
        out.append({"code": f, "group": f, "label": f"{opt['factor_name']} · {opt['name']}",
                    "coef": float(opt["multiplier"]), "source": "coefficients"})
    return out


# --------------------------------------------------------------------------------------------------------------
# Данные из базы (только чтение): рынок НАПП, опыт INSON, части продукта
# --------------------------------------------------------------------------------------------------------------

def market_net(con, cls: str, product_code: Optional[str] = None, st: Optional[dict] = None) -> Optional[dict]:
    """
    r_рын класса по НАПП через app/market_picture (тот же ряд, что в аналитике): выплаты / страховые обязательства.
    basis full_year — итог года (срез 01.01, премии и выплаты за 12 месяцев); нет — последний срез, приведённый к
    году. Убыточность выше max_loss_ratio_pct — None (reason в notes). Возвращает {r_pct, period, date, source,
    row_key, loss_ratio_pct, market_rate_pct, basis, note} или None.
    """
    from . import market_picture as mp
    cfg = settings(st)
    src, notes = mp._Sources(), []
    m = mp._market(con, str(cls), product_code, None, src, notes)
    if not m.get("row_key"):
        return None
    sources = {s["id"]: s for s in src.items}
    fy = cfg["market"]["basis"] == "full_year" and m.get("rate_full_year_pct") is not None \
        and m.get("loss_ratio_full_year_pct") is not None
    if fy:
        rate, lr, sid, basis = m["rate_full_year_pct"], m["loss_ratio_full_year_pct"], m.get("full_year_src"), "full_year"
        period = m.get("rate_full_year_period")
    else:
        rate, lr, sid, basis = m.get("rate_pct"), m.get("loss_ratio_pct"), m.get("rate_src"), "last"
        period = m.get("rate_period")
    if rate is None or lr is None:
        return None
    s = sources.get(sid) or {}
    out = {"r_pct": None, "period": period, "date": s.get("as_of"), "row_key": m["row_key"],
           "row_name": m.get("row_name"), "loss_ratio_pct": lr, "market_rate_pct": rate, "basis": basis,
           "source": "НАПП, " + (period or "") + (" (" + m["row_key"] + ")" if m.get("row_key") else ""),
           "url": s.get("url"), "note": "выплаты / страховые обязательства класса за год"
           + ("" if basis == "full_year" else " (неполный год, приведено к году)")}
    if float(lr) > float(cfg["market"]["max_loss_ratio_pct"]):
        out["rejected"] = "loss_ratio_over_max"
        return out
    # r_рын — из сырых чисел среза (ставка и убыточность market_picture округлены до 3 знаков)
    blk = mp._row_block(con, m["row_key"], mp._Sources())
    row = (blk or {}).get("full_year" if basis == "full_year" else "last")
    months = mp.MONTHS.get(str((row or {}).get("report_date") or "")[5:]) if row else None
    if row and row.get("liabilities") and row.get("payouts_ytd") is not None and months:
        out["r_pct"] = round(float(row["payouts_ytd"]) * 12 / months / float(row["liabilities"]) * 100, 6)
        out["payouts"], out["liabilities"], out["date"] = row["payouts_ytd"], row["liabilities"], row["report_date"]
    else:
        out["r_pct"] = round(float(rate) * float(lr) / 100, 6)
    if not out["r_pct"] or out["r_pct"] <= 0:
        out["r_pct"], out["rejected"] = None, "no_payouts"
    return out


def experience(con, cls: str, as_of: Optional[date] = None, st: Optional[dict] = None) -> Optional[dict]:
    """Опыт INSON по классу за experience.years лет (calibration.exposure_and_losses): договоры, экспозиция,
    убытки. Договоров нет — None."""
    from . import calibration as cal
    cfg = settings(st)
    as_of = as_of or date.today()
    frm = (as_of - timedelta(days=round(365.25 * int(cfg["experience"]["years"])))).isoformat()
    try:
        data = cal.exposure_and_losses(con, frm, as_of.isoformat())
    except Exception:
        return None
    n = m = 0
    exposure = losses = 0.0
    for (c, _ot), g in (data.get("groups") or {}).items():
        if str(c) != str(cls):
            continue
        n += g["n"]
        m += g["m"]
        exposure += g["exposure"]
        losses += g["losses"]
    if n == 0 and m == 0:
        return None
    return {"losses": losses, "exposure": exposure, "n_claims": m, "n_contracts": n,
            "price_index": float(cfg["experience"]["price_index"]), "period": [frm, as_of.isoformat()],
            "source": "убытки и договоры INSON (claims, calibration.exposure_and_losses)", "date": as_of.isoformat()}


def reference_inputs(con, classes: Optional[list] = None, as_of: Optional[date] = None) -> dict:
    """{класс: {"market", "experience"}} для справочника движка (db.load_reference). Сбой одного класса — пусто."""
    st = db_settings(con)
    if classes is None:
        try:
            classes = [r[0] for r in con.execute("SELECT code FROM classes").fetchall()]
        except Exception:
            classes = []
    out = {}
    for c in classes:
        item = {"market": None, "experience": None}
        try:
            mk = market_net(con, c, None, st)
            item["market"] = mk if mk and mk.get("r_pct") is not None else None
            item["market_rejected"] = (mk or {}).get("rejected")
        except Exception as e:
            item["error"] = type(e).__name__
        try:
            item["experience"] = experience(con, c, as_of, st)
        except Exception as e:
            item["error"] = type(e).__name__
        out[str(c)] = item
    return out


def product_parts(con, product_code: str, as_of=None, client_type: Optional[str] = None) -> list:
    """
    Части продукта на дату (день 2 ТЗ): через app/product_parts.parts_on — версия на дату и тип клиента (fiz | yur),
    без смешения версий. Частей нет (таблица пуста или модуля нет) — запасной путь: классы продукта (product_classes)
    и ставки частей из текста тарифа (act_engine.part_rates_from_text), source = rate_text.
    """
    try:
        from . import product_parts as pp
        got = pp.parts_on(con, product_code, as_of, client_type)
        if got.get("parts"):
            v = got.get("version") or {}
            return [dict(p, source=p.get("source") or "product_parts", version=v) for p in got["parts"]]
    except Exception:
        pass
    from . import act_engine as ae
    classes = [r[0] for r in con.execute("SELECT class_code FROM product_classes WHERE product_code=? "
                                         "ORDER BY part_no", (product_code,)).fetchall()]
    pr = con.execute("SELECT rate_text FROM products WHERE code=?", (product_code,)).fetchone()
    rates = ae.part_rates_from_text(pr[0] if pr else None, classes) if classes else {}
    return [{"product_code": product_code, "part_no": i + 1, "class_code": c,
             "base_rate_pct": rates.get(c), "min_rate_pct": rates.get(c), "source": "rate_text"}
            for i, c in enumerate(classes)]
