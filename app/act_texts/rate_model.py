"""Модель ставки ТЗ 13.10.2026 (app/rate_model.py): строки «как посчитано», решение «На рассмотрение» и пояснение
ставки (задача 4.2) — до 3 строк из rate_trace. Часть словаря текстов акта (app/act_texts): склеивается в TX в
__init__.py последним блоком."""

TX_RATE_MODEL = {
    # ---------- как посчитано ----------
    "how_rm_net_market": {"ru": "Нетто-ставка {net} = Z × опыт INSON + (1 − Z) × рынок НАПП (выплаты / страховые "
                                "обязательства класса за год), Z = {z}",
                          "uz": "Netto tarif {net} = Z × INSON tajribasi + (1 − Z) × NAPP bozori (klassning yillik "
                                "toʻlovlari / sugʻurta majburiyatlari), Z = {z}",
                          "en": "Net rate {net} = Z × INSON experience + (1 − Z) × NAPP market (class payouts / "
                                "insurance liabilities for the year), Z = {z}"},
    "how_rm_net_tariff": {"ru": "Нетто-ставка {net} = тариф INSON × (1 − L): рыночных данных по классу нет (временно)",
                          "uz": "Netto tarif {net} = INSON tarifi × (1 − L): klass boʻyicha bozor maʼlumoti yoʻq "
                                "(vaqtincha)",
                          "en": "Net rate {net} = INSON tariff × (1 − L): no market data for the class (temporary)"},
    "how_rm_net_base": {"ru": "Нетто-ставка {net} — базовая ставка справочника (экспертно, не калибровано): нет ни "
                              "рынка, ни тарифа",
                        "uz": "Netto tarif {net} — maʼlumotnomaning bazaviy tarifi (ekspert, kalibrlanmagan): bozor ham, "
                              "tarif ham yoʻq",
                        "en": "Net rate {net} — the reference base rate (expert, not calibrated): neither market nor "
                              "tariff data"},
    "how_rm_net_experience": {"ru": "Нетто-ставка {net} — опыт INSON (Z = {z})",
                              "uz": "Netto tarif {net} — INSON tajribasi (Z = {z})",
                              "en": "Net rate {net} — INSON experience (Z = {z})"},
    "how_rm_gross": {"ru": "Техническая ставка {gross} = нетто × (1 + θ {theta} + c {c}) / (1 − L {L})",
                     "uz": "Texnik tarif {gross} = netto × (1 + θ {theta} + c {c}) / (1 − L {L})",
                     "en": "Technical rate {gross} = net × (1 + θ {theta} + c {c}) / (1 − L {L})"},
    "how_rm_k": {"ru": "Поправки: факторы объекта × {k_obj}, регион × {k_reg}, убытки клиента × {k_cl} → {calc}",
                 "uz": "Tuzatishlar: obyekt omillari × {k_obj}, hudud × {k_reg}, mijoz zararlari × {k_cl} → {calc}",
                 "en": "Adjustments: object factors × {k_obj}, region × {k_reg}, client losses × {k_cl} → {calc}"},
    "how_rm_cap": {"ru": "Ставка ограничена 3 × тарифа = {cap} — решение «На рассмотрение»",
                   "uz": "Tarif 3 × tarif = {cap} bilan cheklandi — qaror «Koʻrib chiqishga»",
                   "en": "The rate is capped at 3 × tariff = {cap} — decision «Refer»"},
    "how_rm_net_floor": {"ru": "Ставка поднята до нетто-ставки {net}: ниже ожидаемого убытка она не опускается",
                         "uz": "Tarif netto tarif {net} gacha koʻtarildi: kutilgan zarardan past boʻlmaydi",
                         "en": "The rate is raised to the net rate {net}: it never goes below the expected loss"},
    "how_rm_premium": {"ru": "Премия = {sum} × {rate6} × {days} / 365 = {premium}",
                       "uz": "Mukofot = {sum} × {rate6} × {days} / 365 = {premium}",
                       "en": "Premium = {sum} × {rate6} × {days} / 365 = {premium}"},
    "how_rm_na": {"ru": "Для класса {cls} нет ни рыночных данных, ни тарифа, ни базовой ставки — ставку определяет "
                        "андеррайтер",
                  "uz": "{cls}-klass uchun bozor maʼlumoti, tarif va bazaviy tarif yoʻq — tarifni anderrayter belgilaydi",
                  "en": "Class {cls} has no market data, tariff or base rate — the underwriter sets the rate"},
    # ---------- решение и проверки ----------
    "d_review": {"ru": "Рекомендация: направить на рассмотрение андеррайтера.",
                 "uz": "Tavsiya: anderrayter koʻrib chiqishiga yuborish.",
                 "en": "Recommendation: refer to the underwriter."},
    "c_rm_refer_losses": {"ru": "Убытков за 3 года: {n} — по правилу ставки договор идёт на рассмотрение",
                          "uz": "3 yillik zararlar: {n} — tarif qoidasiga koʻra shartnoma koʻrib chiqishga yuboriladi",
                          "en": "Losses over 3 years: {n} — under the rate rule the contract is referred"},
    "c_rm_refer_cap": {"ru": "Расчётная ставка {rate} выше 3 × тарифа ({cap}) — согласовать с андеррайтером",
                       "uz": "Hisoblangan tarif {rate} 3 × tarifdan ({cap}) yuqori — anderrayter bilan kelishish",
                       "en": "The calculated rate {rate} exceeds 3 × tariff ({cap}) — agree with the underwriter"},
    "level_rule_k": {"ru": "Уровень риска — по произведению факторов объекта K_об = {k}: до {low} — низкий, от {high} — "
                           "высокий, между — умеренный (экспертно)",
                     "uz": "Xavf darajasi — obyekt omillari koʻpaytmasi K_ob = {k} boʻyicha: {low} gacha — past, "
                           "{high} dan — yuqori, oraligʻida — oʻrtacha (ekspert)",
                     "en": "Risk level — by the product of object factors K_obj = {k}: up to {low} — low, from {high} — "
                           "high, in between — moderate (expert)"},
    "fa_base_gross": {"ru": "База — техническая ставка {rate} (нетто с надбавками и расходами компании)",
                      "uz": "Asos — texnik tarif {rate} (ustamalar va kompaniya xarajatlari bilan netto)",
                      "en": "Base — technical rate {rate} (net with loadings and company expenses)"},
    # ---------- пояснение ставки (задача 4.2) ----------
    "rm_explain_title": {"ru": "Пояснение ставки", "uz": "Tarif izohi", "en": "Rate explanation"},
    "rm_explain_main": {"ru": "Ставка {r} годовых. Основа — {tech}: по данным {src} такие объекты теряют в среднем "
                              "{net} стоимости в год, плюс расходы компании и запас на крупные убытки.",
                        "uz": "Tarif yillik {r}. Asos — {tech}: {src} maʼlumotlariga koʻra bunday obyektlar yiliga "
                              "oʻrtacha qiymatining {net} qismini yoʻqotadi, bunga kompaniya xarajatlari va yirik "
                              "zararlar uchun zaxira qoʻshiladi.",
                        "en": "Rate {r} per year. Basis — {tech}: according to {src}, such objects lose on average "
                              "{net} of their value per year, plus company expenses and a reserve for large losses."},
    "rm_explain_factors": {"ru": "Учтено: {list}.", "uz": "Hisobga olindi: {list}.", "en": "Taken into account: {list}."},
    "rm_explain_min": {"ru": "Применён минимум INSON {m}.", "uz": "INSON minimumi {m} qoʻllanildi.",
                       "en": "The INSON minimum {m} applied."},
    "rm_explain_cap": {"ru": "Ставка ограничена 3 × тарифа — на рассмотрение.",
                       "uz": "Tarif 3 × tarif bilan cheklandi — koʻrib chiqishga.",
                       "en": "The rate is capped at 3 × tariff — referred."},
    "rm_src_market": {"ru": "НАПП за {year} год", "uz": "NAPP {year}-yil", "en": "NAPP for {year}"},
    "rm_src_market_nodate": {"ru": "НАПП", "uz": "NAPP", "en": "NAPP"},
    "rm_src_mix": {"ru": "опыта INSON (вес {z}) и НАПП за {year} год", "uz": "INSON tajribasi (ulushi {z}) va NAPP "
                   "{year}-yil", "en": "INSON experience (weight {z}) and NAPP for {year}"},
    "rm_src_mix_nodate": {"ru": "опыта INSON (вес {z}) и НАПП", "uz": "INSON tajribasi (ulushi {z}) va NAPP",
                          "en": "INSON experience (weight {z}) and NAPP"},
    "rm_src_experience": {"ru": "опыта INSON", "uz": "INSON tajribasi", "en": "INSON experience"},
    "rm_src_tariff": {"ru": "тарифа INSON (рыночных данных нет)", "uz": "INSON tarifi (bozor maʼlumoti yoʻq)",
                      "en": "the INSON tariff (no market data)"},
    "rm_src_base": {"ru": "справочника базовых ставок", "uz": "bazaviy tariflar maʼlumotnomasi",
                    "en": "the base rate reference"},
    "rm_f_region": {"ru": "регион", "uz": "hudud", "en": "region"},
    "rm_f_client": {"ru": "убытки клиента за 3 года", "uz": "mijozning 3 yillik zararlari",
                    "en": "client losses over 3 years"},
    "rm_f_bound": {"ru": "граница факторов объекта", "uz": "obyekt omillari chegarasi", "en": "object factor bound"},
    "rm_market_title": {"ru": "Рынок (для сравнения)", "uz": "Bozor (taqqoslash uchun)", "en": "Market (for comparison)"},
    "rm_market_line": {"ru": "Рыночная ставка НАПП {m} — для сравнения (премии / страховые обязательства класса)",
                       "uz": "NAPP bozor tarifi {m} — taqqoslash uchun (klass mukofotlari / sugʻurta majburiyatlari)",
                       "en": "NAPP market rate {m} — for comparison (class premiums / insurance liabilities)"},
    "rm_market_none": {"ru": "Рыночной ставки НАПП по классу нет", "uz": "Klass boʻyicha NAPP bozor tarifi yoʻq",
                       "en": "No NAPP market rate for the class"},
}


def _fmt(x, lang: str) -> str:
    """Ставка для пояснения: от 0,1 % — 2 знака (0,17 %), от 0,01 % — 3 знака, меньше — 4 знака."""
    from . import NBSP, _num, lang_of
    lang = lang_of(lang)
    v = float(x)
    d = 2 if abs(v) >= 0.1 else (3 if abs(v) >= 0.01 else 4)
    return _num(v, lang, d) + ("%" if lang == "en" else NBSP + "%")


def _signed(v: float, lang: str) -> str:
    from . import NBSP, _num, lang_of
    lang = lang_of(lang)
    d = 0 if abs(v - round(v)) < 1e-9 else 1
    s = _num(abs(v), lang, d) + ("%" if lang == "en" else NBSP + "%")
    return ("+" if v > 0 else "−") + s


def factor_label(f: dict, lang: str) -> str:
    """Подпись множителя модели на языке акта: вариант из шаблона ({ru, uz, en}) → подписи коэффициентов
    (OPTION_LABELS, FACTOR_LABELS) → по-русски как есть (название)."""
    from . import FACTOR_LABELS, OPTION_LABELS, lang_of, t
    lang = lang_of(lang)
    kind = f.get("kind")
    if kind == "region":
        return t("rm_f_region", lang)
    if kind == "client":
        return t("rm_f_client", lang)
    if kind == "bound":
        return t("rm_f_bound", lang)
    lab = f.get("label")
    if isinstance(lab, dict) and (lab.get(lang) or lab.get("ru")):
        if lab.get(lang):
            return lab[lang]
    grp, code = f.get("group"), f.get("code")
    opt = OPTION_LABELS.get(f"{grp}:{code}")
    if opt and opt.get(lang):
        return opt[lang]
    gl = f.get("group_label")
    if isinstance(gl, dict) and gl.get(lang):
        return gl[lang]
    if grp in FACTOR_LABELS and FACTOR_LABELS[grp].get(lang):
        return FACTOR_LABELS[grp][lang]
    if isinstance(lab, dict):
        return lab.get("ru") or str(code)
    return str(lab or code)


def rate_explain(res: dict, lang: str, explain: dict = None) -> dict:
    """
    Пояснение ставки (задача 4.2, до 3 строк) из результата rate_model.compute: «Ставка {r} годовых. Основа —
    {r_тех}: по данным {источник} такие объекты теряют в среднем {r_нетто} стоимости в год, плюс расходы компании
    и запас на крупные убытки. Учтено: {до 4 факторов с влиянием ≥ 3 %}.» При минимуме — «Применён минимум INSON».
    explain — act_settings.rate_model.explain {min_effect_pct, max_factors}. Возвращает {text, lines, factors}.
    """
    from . import lang_of, t
    from .. import rate_model as rm
    lang = lang_of(lang)
    if not res or not res.get("available"):
        return {"available": False, "text": None, "lines": [], "factors": []}
    ex = dict(rm.DEFAULTS["explain"], **(explain or {}))
    mk = res.get("market") or {}
    d = str(mk.get("date") or "")
    year = (int(d[:4]) - 1 if d[5:10] == "01-01" else int(d[:4])) if d[:4].isdigit() else None
    kind, z = res.get("r_market_kind"), float(res.get("Z") or 0)
    zt = _num_z(z, lang)
    if z >= 1 or kind is None:
        src = t("rm_src_experience", lang)
    elif kind == "market":
        src = t("rm_src_mix" if z > 0 else "rm_src_market", lang, year=year, z=zt) if year \
            else t("rm_src_mix_nodate" if z > 0 else "rm_src_market_nodate", lang, z=zt)
    elif kind == "tariff":
        src = t("rm_src_tariff", lang)
    else:
        src = t("rm_src_base", lang)
    lines = [t("rm_explain_main", lang, r=_fmt(res["r"], lang), tech=_fmt(res["r_gross"], lang), src=src,
               net=_fmt(res["r_net"], lang))]
    picked = [f for f in res.get("factors") or [] if abs(float(f["effect_pct"])) + 1e-9 >= float(ex["min_effect_pct"])]
    picked.sort(key=lambda f: -abs(float(f["effect_pct"])))
    picked = picked[:int(ex["max_factors"])]
    fl = [f"{factor_label(f, lang)} {_signed(float(f['effect_pct']), lang)}" for f in picked]
    if fl:
        lines.append(t("rm_explain_factors", lang, list=", ".join(fl)))
    if res.get("min_applied") and res.get("min_pct") is not None:
        lines.append(t("rm_explain_min", lang, m=_fmt(res["min_pct"], lang)))
    elif res.get("bound") in ("cap", "cap_net"):
        lines.append(t("rm_explain_cap", lang))
    return {"available": True, "text": " ".join(lines), "lines": lines,
            "factors": [{"code": f.get("code"), "kind": f.get("kind"), "label": x, "effect_pct": f["effect_pct"],
                         "premium_delta": f.get("premium_delta")} for f, x in zip(picked, fl)]}


def _num_z(z: float, lang: str) -> str:
    from . import _num
    return _num(z, lang, 3)
