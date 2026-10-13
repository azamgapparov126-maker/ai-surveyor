"""Раздел 4 — аналитика риска: риски, чувствительность, показатели stat.uz / data.egov.uz.
Часть словаря текстов акта (app/act_texts): склеивается в TX в __init__.py в прежнем порядке."""

# ================================================================================================
#  Раздел 4 — аналитика риска (30.09.2026): риски, факторы, чувствительность, состав тарифа, сценарии
#  подробно, балл, рынок и статистика, франшиза и мероприятия; раздел 1 по виду объекта
# ================================================================================================
# риски классов 8 и 9 (коды таблицы perils): имена в базе — по-русски, в акте — на языке акта
PERIL_LABELS = {
    "fire": {"ru": "Пожар", "uz": "Yongʻin", "en": "Fire"},
    "explosion": {"ru": "Взрыв", "uz": "Portlash", "en": "Explosion"},
    "storm": {"ru": "Буря", "uz": "Boʻron", "en": "Storm"},
    "hurricane": {"ru": "Ураган", "uz": "Dovul", "en": "Hurricane"},
    "downpour": {"ru": "Ливень", "uz": "Jala", "en": "Downpour"},
    "collapse": {"ru": "Обвал", "uz": "Qulash", "en": "Collapse"},
    "subsidence": {"ru": "Просадка грунта", "uz": "Grunt choʻkishi", "en": "Subsidence"},
    "landslide": {"ru": "Оползень", "uz": "Surilma", "en": "Landslide"},
    "groundwater": {"ru": "Действие подпочвенных вод", "uz": "Grunt suvlari taʼsiri", "en": "Groundwater"},
    "mudflow": {"ru": "Сель", "uz": "Sel", "en": "Mudflow"},
    "lightning": {"ru": "Удар молнии", "uz": "Chaqmoq urishi", "en": "Lightning"},
    "earthquake": {"ru": "Землетрясение", "uz": "Zilzila", "en": "Earthquake"},
    "nuclear": {"ru": "Ядерная энергия", "uz": "Yadro energiyasi", "en": "Nuclear energy"},
    "hail": {"ru": "Град", "uz": "Doʻl", "en": "Hail"},
    "snowfall": {"ru": "Обильный снегопад", "uz": "Kuchli qor yogʻishi", "en": "Heavy snowfall"},
    "frost": {"ru": "Заморозки", "uz": "Sovuq urishi", "en": "Frost"},
    "burglary": {"ru": "Кража со взломом", "uz": "Buzib kirib oʻgʻirlash", "en": "Burglary"},
    "other9": {"ru": "Иные события, не указанные в классе 8", "uz": "8-klassda koʻrsatilmagan boshqa hodisalar",
               "en": "Other events not listed in class 8"},
}

PERIL_LEVEL_LABELS = {
    "low": {"ru": "низкий", "uz": "past", "en": "low"},
    "moderate": {"ru": "умеренный", "uz": "oʻrtacha", "en": "moderate"},
    "high": {"ru": "высокий", "uz": "yuqori", "en": "high"},
}

LEVEL5_LABELS = {
    "low": {"ru": "низкий", "uz": "past", "en": "low"},
    "moderate": {"ru": "умеренный", "uz": "oʻrtacha", "en": "moderate"},
    "elevated": {"ru": "повышенный", "uz": "oshgan", "en": "elevated"},
    "high": {"ru": "высокий", "uz": "yuqori", "en": "high"},
    "critical": {"ru": "критический", "uz": "juda yuqori", "en": "critical"},
}

SCORE_COMP_LABELS = {
    "rate": {"ru": "Техническая ставка к минимуму или рынку", "uz": "Texnik tarifning minimum yoki bozorga nisbati",
             "en": "Technical rate to the minimum or market"},
    "mfl_retention": {"ru": "MFL к лимиту удержания", "uz": "MFL ning ushlab qolish limitiga nisbati",
                      "en": "MFL to the retention limit"},
    "losses": {"ru": "Убытки за 3 года", "uz": "3 yillik zararlar", "en": "Losses over 3 years"},
    "insurance_to_value": {"ru": "Страховая сумма к стоимости", "uz": "Sugʻurta summasining qiymatga nisbati",
                           "en": "Sum insured to value"},
    "seismic": {"ru": "Сейсмозона", "uz": "Seysmik zona", "en": "Seismic zone"},
    "external_stats": {"ru": "Внешняя статистика региона", "uz": "Hududning tashqi statistikasi",
                       "en": "External regional statistics"},
}

# показатели stat.uz / data.egov.uz (коды risk_stats.INDICATORS) для классов 3, 8, 9 и единицы
STAT_LABELS = {
    "road_accidents": {"ru": "Дорожно-транспортные происшествия", "uz": "Yoʻl-transport hodisalari",
                       "en": "Road traffic accidents"},
    "road_injured": {"ru": "Пострадавшие в ДТП", "uz": "YTH jabrlanuvchilari", "en": "Road accident casualties"},
    "road_death_rate": {"ru": "Смертность в ДТП на 100 000 жителей", "uz": "100 000 aholiga YTHda oʻlim",
                        "en": "Road deaths per 100,000 people"},
    "cars_per_100_households": {"ru": "Автомобилей на 100 домохозяйств", "uz": "100 xonadonga avtomobillar",
                                "en": "Cars per 100 households"},
    "passengers_road": {"ru": "Перевезено пассажиров автотранспортом", "uz": "Avtotransportda tashilgan yoʻlovchilar",
                        "en": "Road passengers carried"},
    "freight_road": {"ru": "Перевезено грузов автотранспортом", "uz": "Avtotransportda tashilgan yuklar",
                     "en": "Road freight carried"},
    "thefts": {"ru": "Зарегистрированные кражи", "uz": "Roʻyxatga olingan oʻgʻirliklar", "en": "Registered thefts"},
    "crimes_total": {"ru": "Зарегистрированные преступления", "uz": "Roʻyxatga olingan jinoyatlar",
                     "en": "Registered crimes"},
    "robberies": {"ru": "Грабежи и разбои", "uz": "Talonchilik va bosqinchilik", "en": "Robberies"},
    "vulnerable_housing": {"ru": "Доля глинобитного жилья и жилья из сырцового кирпича",
                           "uz": "Paxsa va xom gʻishtdan qurilgan uy-joylar ulushi",
                           "en": "Share of adobe and raw-brick housing"},
    "emergencies": {"ru": "Чрезвычайные ситуации (всего)", "uz": "Favqulodda vaziyatlar (jami)",
                    "en": "Emergencies (total)"},
    "construction_price_index_y": {"ru": "Индекс цен на строительство (к декабрю прошлого года)",
                                   "uz": "Qurilish narxlari indeksi (oʻtgan yil dekabriga)",
                                   "en": "Construction price index (to December of the previous year)"},
}

# оговорки показателей вилки ставки (01.10.2026): что показатель измеряет на самом деле — в карточке и в документе
STAT_CAVEATS = {
    "road_accidents": {"ru": "все ДТП региона, а не страховые случаи",
                       "uz": "hududdagi barcha YTH, sugʻurta hodisalari emas",
                       "en": "all road accidents in the region, not insured events"},
    "thefts": {"ru": "все кражи, угоны отдельно не публикуются",
               "uz": "barcha oʻgʻirliklar, avtomobil oʻgʻirlash alohida eʼlon qilinmaydi",
               "en": "all thefts; vehicle thefts are not published separately"},
    "vulnerable_housing": {"ru": "доля глинобитного жилья — свойство жилого фонда региона, не конструкции объекта",
                           "uz": "paxsa uylar ulushi — hudud uy-joy fondining xususiyati, obyekt konstruksiyasiniki emas",
                           "en": "the share of adobe housing describes the region's housing stock, not the object's "
                                 "construction"},
}

STAT_UNITS = {
    "ДТП": {"ru": "ДТП", "uz": "YTH", "en": "accidents"},
    "человек": {"ru": "человек", "uz": "kishi", "en": "people"},
    "на 100 000 человек": {"ru": "на 100 000 человек", "uz": "100 000 kishiga", "en": "per 100,000 people"},
    "штук": {"ru": "штук", "uz": "dona", "en": "units"},
    "млн человек": {"ru": "млн человек", "uz": "mln kishi", "en": "million people"},
    "млн т": {"ru": "млн т", "uz": "mln t", "en": "million t"},
    "краж": {"ru": "краж", "uz": "oʻgʻirlik", "en": "thefts"},
    "преступлений": {"ru": "преступлений", "uz": "jinoyat", "en": "crimes"},
    "случаев": {"ru": "случаев", "uz": "holat", "en": "cases"},
    "% жилищного фонда": {"ru": "% жилищного фонда", "uz": "uy-joy fondining %", "en": "% of housing stock"},
    "%": {"ru": "%", "uz": "%", "en": "%"},
}

# чего нет в открытых данных по классу (risk_stats.NOT_FOUND — по-русски; здесь — на трёх языках)
STAT_NOT_FOUND = {
    "3": {"ru": "парка транспортных средств по регионам и угонов отдельно от прочих краж нет",
          "uz": "hududlar boʻyicha transport vositalari parki va boshqa oʻgʻirliklardan alohida oʻgʻirlab ketishlar yoʻq",
          "en": "no regional vehicle fleet data and no vehicle thefts separate from other thefts"},
    "8": {"ru": "пожаров и ущерба от пожаров по регионам нет; ЧС МЧС — два квартала без регионов и без сумм ущерба; "
                "перечня землетрясений нет",
          "uz": "hududlar boʻyicha yongʻinlar va yongʻin zarari yoʻq; FVV favqulodda vaziyatlari — hududlar va zarar "
                "summalarisiz ikki chorak; zilzilalar roʻyxati yoʻq",
          "en": "no regional fire or fire-damage data; emergencies — two quarters without regions or loss amounts; "
                "no earthquake list"},
    "9": {"ru": "краж из жилищ и предприятий отдельно нет — только все кражи вместе",
          "uz": "uy-joy va korxonalardan oʻgʻirliklar alohida yoʻq — faqat barcha oʻgʻirliklar birga",
          "en": "no thefts from homes and businesses separately — only all thefts together"},
}

AN_WHATIF_LABELS = {
    "protection": {"ru": "если защита — {v}", "uz": "agar himoya — {v}", "en": "if protection is {v}"},
    "seismic_zone": {"ru": "если сейсмозона — {v}", "uz": "agar seysmik zona — {v}",
                     "en": "if the seismic zone is {v}"},
}

# вид объекта в акте: уточнение по описанию (склад-холодильник) — подпись вида, а не перевод текста
OBJECT_SUBKINDS = {
    "cold_store": {"ru": "склад-холодильник", "uz": "sovutgichli ombor", "en": "cold store"},
}

# ---------- НАПП (01.10.2026): комплексное страхование (пакеты классов), претензии, подразделения ----------
STAT_LABELS["claims_freq"] = {"ru": "Частота страховых претензий (НАПП)", "uz": "Sugʻurta daʼvolari chastotasi (SHNMA)",
                              "en": "Insurance claims frequency (NAPP)"}

STAT_UNITS["претензий на 1 000 договоров"] = {"ru": "претензий на 1 000 договоров", "uz": "1 000 shartnomaga daʼvo",
                                             "en": "claims per 1,000 contracts"}

STAT_CAVEATS["claims_freq"] = {"ru": "все претензии общего страхования региона, без разреза по классам; претензии "
                                     "учитываются по месту головных офисов страховщиков — почти все приходятся на "
                                     "город Ташкент",
                               "uz": "hududdagi umumiy sugʻurta boʻyicha barcha daʼvolar, klasslar kesimisiz; daʼvolar "
                                     "sugʻurtachilarning bosh ofislari joylashgan joy boʻyicha hisobga olinadi — deyarli "
                                     "barchasi Toshkent shahriga toʻgʻri keladi",
                               "en": "all general-insurance claims in the region, not broken down by class; claims "
                                     "are recorded where insurers' head offices are — almost all fall on Tashkent "
                                     "city"}

TX_ANALYTICS = {
    # ---------- раздел 1: строки по виду объекта ----------
    "s1_not_specified": {"ru": "Не указано", "uz": "Koʻrsatilmagan", "en": "Not stated"},
    "s1_doc_text": {"ru": "текст документа: «{text}»", "uz": "hujjat matni: «{text}»", "en": "document text: «{text}»"},
    "s1_doc_text_mark": {"ru": "текст документа", "uz": "hujjat matni", "en": "document text"},
    "s1_by_model": {"ru": "перевод модели, проверьте", "uz": "model tarjimasi, tekshiring",
                    "en": "model translation, please check"},
    "s1_by_dict": {"ru": "вид объекта определён по словарю, проверьте",
                   "uz": "obyekt turi lugʻat boʻyicha aniqlandi, tekshiring",
                   "en": "object type identified by dictionary, please check"},
    "lbl_equipment_name": {"ru": "Наименование", "uz": "Nomi", "en": "Name"},
    "lbl_install_place": {"ru": "Место установки", "uz": "Oʻrnatilgan joy", "en": "Installation site"},
    "lbl_building_kind": {"ru": "Вид объекта", "uz": "Obyekt turi", "en": "Type of property"},
    "lbl_address": {"ru": "Адрес / место нахождения", "uz": "Manzil / joylashgan joyi", "en": "Address / location"},
    "lbl_floors": {"ru": "Этажность", "uz": "Qavatlar soni", "en": "Number of floors"},
    # факты об объекте в разделе 1 (13.10.2026, ТЗ день 1): адрес, износ, водители, площадь в м²
    "lbl_object_address": {"ru": "Адрес объекта", "uz": "Obyekt manzili", "en": "Object address"},
    "lbl_wear": {"ru": "Износ", "uz": "Eskirish", "en": "Wear"},
    "lbl_drivers": {"ru": "Водители и стаж", "uz": "Haydovchilar va staji", "en": "Drivers and experience"},
    "unit_m2": {"ru": "м²", "uz": "m²", "en": "m²"},
    "s1_wear_new": {"ru": "новое или после капремонта", "uz": "yangi yoki kapital taʼmirdan keyin",
                    "en": "new or after overhaul"},
    "s1_wear_normal": {"ru": "до 50 %", "uz": "50 % gacha", "en": "up to 50 %"},
    "s1_wear_high": {"ru": "более 50 %", "uz": "50 % dan ortiq", "en": "over 50 %"},
    "s1_wear_emergency": {"ru": "аварийное состояние", "uz": "avariya holati", "en": "dilapidated"},
    "s1_wear_by_pct": {"ru": "вариант фактора «Износ» — по проценту", "uz": "«Eskirish» omili varianti — foiz boʻyicha",
                       "en": "«Wear» factor option — from the percentage"},
    "as_activity_by_type": {"ru": "деятельность на объекте: {value} — по типу объекта «{type}», принято по умолчанию",
                            "uz": "obyektdagi faoliyat: {value} — «{type}» obyekt turi boʻyicha, standart boʻyicha "
                                  "qabul qilindi",
                            "en": "activity on site: {value} — from the object type «{type}», assumed by default"},
    "an_default_mark": {"ru": "принято по умолчанию", "uz": "standart boʻyicha qabul qilindi", "en": "assumed by default"},
    "s1_kind_default": {"ru": "принят по умолчанию: {v}", "uz": "standart boʻyicha qabul qilindi: {v}",
                        "en": "assumed by default: {v}"},
    "s1_kind_default_note": {"ru": "вид объекта принят по умолчанию — уточните",
                             "uz": "obyekt turi standart boʻyicha qabul qilindi — aniqlang",
                             "en": "object type assumed by default — please clarify"},
    "an_t_base_default": {"ru": "вид объекта принят по умолчанию: {v} — уточните",
                          "uz": "obyekt turi standart boʻyicha qabul qilindi: {v} — aniqlang",
                          "en": "object type assumed by default: {v} — please clarify"},
    "as_activity_text": {"ru": "деятельность на объекте определена по описанию в документе: {value} — проверьте",
                         "uz": "obyektdagi faoliyat hujjatdagi tavsif boʻyicha aniqlandi: {value} — tekshiring",
                         "en": "activity on site identified from the document description: {value} — please check"},

    # ---------- раздел 4: заголовки блоков аналитики ----------
    "an_summary": {"ru": "Кратко: {text}", "uz": "Qisqacha: {text}", "en": "In brief: {text}"},
    "an_risks_title": {"ru": "Разбор по рискам: доля в нетто-ставке и уровень",
                       "uz": "Xavflar tahlili: netto-tarifdagi ulush va daraja",
                       "en": "Risks: share of the net rate and level"},
    "an_factors_title": {"ru": "Учтённые факторы: значение, источник, вклад в техническую ставку",
                         "uz": "Hisobga olingan omillar: qiymat, manba, texnik tarifga hissa",
                         "en": "Factors used: value, source, contribution to the technical rate"},
    "an_sens_title": {"ru": "Что изменит ставку (посчитано расчётным модулем)",
                      "uz": "Tarifni nima oʻzgartiradi (hisob-kitob moduli hisobladi)",
                      "en": "What would change the rate (calculated by the rating module)"},
    "an_tariff_title": {"ru": "Состав тарифа", "uz": "Tarif tarkibi", "en": "Rate build-up"},
    "an_sc_title": {"ru": "Сценарии убытка подробно", "uz": "Zarar ssenariylari batafsil",
                    "en": "Loss scenarios in detail"},
    "an_whatif_title": {"ru": "Сценарии «что если» (расчёт при других данных объекта)",
                        "uz": "«Agar…» ssenariylari (obyektning boshqa maʼlumotlari bilan hisob)",
                        "en": "What-if scenarios (calculation with other object data)"},
    "an_ret_title": {"ru": "Лимит удержания и перестрахование", "uz": "Ushlab qolish limiti va qayta sugʻurtalash",
                     "en": "Retention limit and reinsurance"},
    "an_score_title": {"ru": "Балл риска 0–100 (справочно)", "uz": "Xavf bali 0–100 (maʼlumot uchun)",
                       "en": "Risk score 0–100 (for reference)"},
    "an_market_title": {"ru": "Рынок и статистика", "uz": "Bozor va statistika", "en": "Market and statistics"},
    "an_fr_title": {"ru": "Франшиза: варианты (справочно)", "uz": "Franshiza: variantlar (maʼlumot uchun)",
                    "en": "Deductible: options (for reference)"},
    "an_ms_title": {"ru": "Мероприятия: эффект на ставку и премию", "uz": "Tadbirlar: tarif va mukofotga taʼsiri",
                    "en": "Measures: effect on the rate and premium"},
    "an_na": {"ru": "Аналитика риска не посчитана: не хватило данных для модуля аналитики рисков",
              "uz": "Xavf tahlili hisoblanmadi: xavf tahlili moduli uchun maʼlumot yetmadi",
              "en": "Risk analytics was not calculated: not enough data for the risk analytics module"},

    # ---------- колонки таблиц ----------
    "col_risk": {"ru": "Риск", "uz": "Xavf", "en": "Risk"},
    "col_share": {"ru": "Доля в нетто-ставке", "uz": "Netto-tarifdagi ulush", "en": "Share of net rate"},
    "col_level": {"ru": "Уровень", "uz": "Daraja", "en": "Level"},
    "col_why": {"ru": "Почему", "uz": "Nima uchun", "en": "Why"},
    "col_factor": {"ru": "Фактор", "uz": "Omil", "en": "Factor"},
    "col_value": {"ru": "Значение", "uz": "Qiymat", "en": "Value"},
    "col_source": {"ru": "Источник", "uz": "Manba", "en": "Source"},
    "col_mult": {"ru": "Множитель", "uz": "Koeffitsiyent", "en": "Multiplier"},
    "col_contrib": {"ru": "Вклад в техническую ставку", "uz": "Texnik tarifga hissa", "en": "Contribution"},
    "col_change": {"ru": "Изменение", "uz": "Oʻzgarish", "en": "Change"},
    "col_tech": {"ru": "Техническая ставка", "uz": "Texnik tarif", "en": "Technical rate"},
    "col_act_premium": {"ru": "Премия акта", "uz": "Dalolatnoma mukofoti", "en": "Report premium"},
    "col_indicator": {"ru": "Показатель", "uz": "Koʻrsatkich", "en": "Item"},
    "col_note": {"ru": "Пояснение", "uz": "Izoh", "en": "Note"},
    "col_scenario": {"ru": "Сценарий", "uz": "Ssenariy", "en": "Scenario"},
    "col_amount": {"ru": "Сумма", "uz": "Summa", "en": "Amount"},
    "col_how": {"ru": "Как посчитано", "uz": "Qanday hisoblangan", "en": "How calculated"},
    "col_variant": {"ru": "Вариант", "uz": "Variant", "en": "Option"},
    "col_points": {"ru": "Баллы", "uz": "Ball", "en": "Points"},
    "col_weight": {"ru": "Вес", "uz": "Vazn", "en": "Weight"},
    "col_contribution": {"ru": "Вклад", "uz": "Hissa", "en": "Contribution"},
    "col_period": {"ru": "Период и источник", "uz": "Davr va manba", "en": "Period and source"},
    "col_franchise": {"ru": "Франшиза", "uz": "Franshiza", "en": "Deductible"},
    "col_per_loss": {"ru": "С каждого убытка", "uz": "Har bir zarardan", "en": "Per loss"},
    "col_rate": {"ru": "Ставка", "uz": "Tarif", "en": "Rate"},
    "col_premium": {"ru": "Премия", "uz": "Mukofot", "en": "Premium"},
    "col_saving": {"ru": "Экономия", "uz": "Tejam", "en": "Saving"},
    "col_measure": {"ru": "Мероприятие", "uz": "Tadbir", "en": "Measure"},

    # ---------- 1. риски ----------
    "an_r_line": {"ru": "{name} — {share} нетто-ставки — уровень {level}: {why}",
                  "uz": "{name} — netto-tarifning {share} — daraja {level}: {why}",
                  "en": "{name} — {share} of the net rate — level {level}: {why}"},
    "an_r_by_factor": {"ru": "определил фактор «{factor}: {option}» (×{mult})",
                       "uz": "«{factor}: {option}» omili belgiladi (×{mult})",
                       "en": "set by the factor «{factor}: {option}» (×{mult})"},
    "an_r_all_avg": {"ru": "факторы этого риска на среднем уровне или не указаны",
                     "uz": "bu xavf omillari oʻrtacha darajada yoki koʻrsatilmagan",
                     "en": "the factors of this risk are average or not stated"},
    "an_r_zone_unknown": {"ru": "сейсмозона не указана — уровень умеренный до уточнения",
                          "uz": "seysmik zona koʻrsatilmagan — aniqlashtirilgunga qadar daraja oʻrtacha",
                          "en": "seismic zone not stated — moderate until clarified"},
    "an_r_raises": {"ru": "повышает: {what}", "uz": "oshiradi: {what}", "en": "increases: {what}"},
    "an_r_lowers": {"ru": "снижает: {what}", "uz": "kamaytiradi: {what}", "en": "reduces: {what}"},
    "an_r_unknown": {"ru": "не указано: {what}", "uz": "koʻrsatilmagan: {what}", "en": "not stated: {what}"},
    "an_r_helps": {"ru": "снизит: {what}", "uz": "kamaytiradi: {what}", "en": "would reduce: {what}"},
    "an_r_cat": {"ru": "катастрофический риск", "uz": "falokat xavfi", "en": "catastrophe risk"},
    "an_r_whole": {"ru": "В справочнике класс {cls} не разбит на отдельные риски (ДТП, угон, пожар техники): вся "
                         "нетто-ставка — один риск класса, долю каждого события выделить нельзя.",
                   "uz": "Maʼlumotnomada {cls}-klass alohida xavflarga (YTH, oʻgʻirlash, texnika yongʻini) "
                         "boʻlinmagan: butun netto-tarif — klassning bitta xavfi, har bir hodisa ulushini ajratib "
                         "boʻlmaydi.",
                   "en": "In the reference, class {cls} is not split into separate risks (accident, theft, fire): "
                         "the whole net rate is one class risk; the share of each event cannot be separated."},
    "an_r_covered": {"ru": "Риски, перечисленные в договоре: {what}", "uz": "Shartnomada sanab oʻtilgan xavflar: {what}",
                     "en": "Risks listed in the contract: {what}"},
    "an_r_rule": {"ru": "Уровень риска — по произведению множителей его факторов: не больше {low} — низкий, не меньше "
                        "{high} — высокий, иначе умеренный. Экспертно, не калибровано.",
                  "uz": "Xavf darajasi — uning omillari koeffitsiyentlari koʻpaytmasi boʻyicha: {low} dan oshmasa — "
                        "past, {high} dan kam boʻlmasa — yuqori, aks holda oʻrtacha. Ekspert, kalibrlanmagan.",
                  "en": "A risk's level is the product of its factor multipliers: {low} or less — low, {high} or more "
                        "— high, otherwise moderate. Expert values, not calibrated."},
    "an_r_shares": {"ru": "Доли — из справочника рисков (нетто-ставка класса), сумма {total}.",
                    "uz": "Ulushlar — xavflar maʼlumotnomasidan (klass netto-tarifi), jami {total}.",
                    "en": "Shares come from the risk reference (class net rate), total {total}."},
    "an_r_shares_round": {"ru": "Доли — из справочника рисков (нетто-ставка класса); сумма {total} — из-за округления долей.",
                          "uz": "Ulushlar — xavflar maʼlumotnomasidan (klass netto-tarifi); jami {total} — ulushlar "
                                "yaxlitlangani sababli.",
                          "en": "Shares come from the risk reference (class net rate); the total of {total} is due to "
                                "rounding of the shares."},

    # ---------- 2. факторы ----------
    "an_f_line": {"ru": "{factor}: {value} ({source}) — ×{mult}, {contrib}",
                  "uz": "{factor}: {value} ({source}) — ×{mult}, {contrib}",
                  "en": "{factor}: {value} ({source}) — ×{mult}, {contrib}"},
    "an_f_contrib": {"ru": "{pp} п. п. к технической ставке ({premium} за срок)",
                     "uz": "texnik tarifga {pp} f. p. (muddat uchun {premium})",
                     "en": "{pp} pp to the technical rate ({premium} for the term)"},
    "an_f_zero": {"ru": "не влияет", "uz": "taʼsir qilmaydi", "en": "no effect"},
    "an_f_not_set": {"ru": "не указано", "uz": "koʻrsatilmagan", "en": "not stated"},
    "an_f_base": {"ru": "База без факторов: базовая нетто-ставка с надбавками и нагрузкой — {base}; с факторами — "
                        "техническая ставка {tech}. Вклады идут по цепочке расчётного модуля и складываются в разницу. "
                        "Коэффициенты экспертные, не калибровано.",
                  "uz": "Omillarsiz baza: ustama va yuklama bilan bazaviy netto-tarif — {base}; omillar bilan — texnik "
                        "tarif {tech}. Hissalar hisob-kitob moduli zanjiri boʻyicha hisoblanadi va farqqa qoʻshiladi. "
                        "Koeffitsiyentlar ekspert, kalibrlanmagan.",
                  "en": "Base without factors: base net rate with loadings — {base}; with factors — technical rate "
                        "{tech}. Contributions follow the rating module chain and add up to the difference. Expert "
                        "coefficients, not calibrated."},
    "an_f_seismic_na": {"ru": "землетрясение не в покрытии — не применяется",
                        "uz": "zilzila qoplamada emas — qoʻllanilmaydi", "en": "earthquake not covered — not applied"},

    # ---------- чувствительность ----------
    "an_s_measure": {"ru": "{factor}: {to} — техническая ставка {before} → {after} ({delta})",
                     "uz": "{factor}: {to} — texnik tarif {before} → {after} ({delta})",
                     "en": "{factor}: {to} — technical rate {before} → {after} ({delta})"},
    "an_s_clarify": {"ru": "если {factor} окажется «{to}» — техническая ставка {before} → {after} ({delta})",
                     "uz": "agar {factor} «{to}» boʻlsa — texnik tarif {before} → {after} ({delta})",
                     "en": "if {factor} turns out to be «{to}» — technical rate {before} → {after} ({delta})"},
    "an_s_if": {"ru": "если {factor} окажется «{to}»", "uz": "agar {factor} «{to}» boʻlsa", "en": "if {factor} is «{to}»"},
    "an_s_act": {"ru": "; премия акта {after} ({delta})", "uz": "; dalolatnoma mukofoti {after} ({delta})",
                 "en": "; report premium {after} ({delta})"},
    "an_s_act_floor": {"ru": ", ставка акта упирается в минимум продукта",
                       "uz": ", dalolatnoma tarifi mahsulot minimumiga yetadi",
                       "en": ", the report rate hits the product minimum"},
    "an_s_none": {"ru": "Расчётный модуль не нашёл значений факторов, которые заметно меняют ставку.",
                  "uz": "Hisob-kitob moduli tarifni sezilarli oʻzgartiradigan omil qiymatlarini topmadi.",
                  "en": "The rating module found no factor values that noticeably change the rate."},
    "an_s_note": {"ru": "Меры страхователя — лучшее значение фактора; неизвестные данные — лучший и худший варианты. "
                        "Тариф акта считается по тарифной политике; эффект мер на премию акта — как у мероприятий "
                        "(отношение ставок расчётного модуля), не ниже минимума продукта.",
                  "uz": "Sugʻurtalanuvchi choralari — omilning eng yaxshi qiymati; nomaʼlum maʼlumotlar — eng yaxshi "
                        "va eng yomon variantlar. Dalolatnoma tarifi tarif siyosati boʻyicha hisoblanadi; choralarning "
                        "dalolatnoma mukofotiga taʼsiri — tadbirlardagi kabi (hisob-kitob moduli tariflari nisbati), mahsulot "
                        "minimumidan past emas.",
                  "en": "Policyholder measures show the best factor value; unknown data shows the best and worst "
                        "options. The report rate follows the tariff policy; the effect of measures on the report "
                        "premium is taken as for measures (ratio of rating module rates), not below the product minimum."},

    # ---------- 3. состав тарифа ----------
    "an_t_base": {"ru": "Базовая нетто-ставка класса {cls}, тип «{otype}»",
                  "uz": "{cls}-klass bazaviy netto-tarifi, «{otype}» turi",
                  "en": "Base net rate, class {cls}, type «{otype}»"},
    "an_t_base_avg": {"ru": "тип объекта не найден — средняя по классу",
                      "uz": "obyekt turi topilmadi — klass boʻyicha oʻrtacha",
                      "en": "object type not found — class average"},
    "an_t_perils": {"ru": "Набор рисков (доля включённых)", "uz": "Xavflar toʻplami (kiritilganlar ulushi)",
                    "en": "Risk set (share included)"},
    "an_t_excluded": {"ru": "не включено: {what}", "uz": "kiritilmagan: {what}", "en": "excluded: {what}"},
    "an_t_factors": {"ru": "Произведение коэффициентов", "uz": "Koeffitsiyentlar koʻpaytmasi",
                     "en": "Product of multipliers"},
    "an_t_net": {"ru": "Нетто-ставка", "uz": "Netto-tarif", "en": "Net rate"},
    "an_t_risk": {"ru": "Рисковая надбавка ({r} нетто)", "uz": "Xavf ustamasi (nettoning {r})",
                  "en": "Risk loading ({r} of net)"},
    "an_t_cat": {"ru": "Катастрофическая надбавка (землетрясение, сель, оползень)",
                 "uz": "Falokat ustamasi (zilzila, sel, surilma)",
                 "en": "Catastrophe loading (earthquake, mudflow, landslide)"},
    "an_t_load": {"ru": "Нагрузка", "uz": "Yuklama", "en": "Expense loading"},
    "an_t_load_takaful": {"ru": "профиль такафул: без прибыли компании (вознаграждение вакаля)",
                          "uz": "takaful profili: kompaniya foydasisiz (vakola haqi)",
                          "en": "takaful profile: no company profit (wakala fee)"},
    "an_t_tech": {"ru": "Техническая (брутто) ставка расчётного модуля",
                  "uz": "Hisob-kitob modulining texnik (brutto) tarifi",
                  "en": "Rating module technical (gross) rate"},
    "an_t_tech_note": {"ru": "справочно; премия по ней за срок — {premium}",
                       "uz": "maʼlumot uchun; u boʻyicha muddat mukofoti — {premium}",
                       "en": "for reference; premium at it for the term — {premium}"},
    "an_t_min": {"ru": "Минимальная ставка продукта", "uz": "Mahsulotning eng kam tarifi", "en": "Product minimum rate"},
    "an_t_policy": {"ru": "Ставка тарифной политики (продукт {code})", "uz": "Tarif siyosati tarifi ({code} mahsulot)",
                    "en": "Tariff policy rate (product {code})"},
    "an_t_base_act": {"ru": "Базовая ставка акта (техническая без коэффициентов)",
                      "uz": "Dalolatnoma bazaviy tarifi (koeffitsiyentsiz texnik)",
                      "en": "Report base rate (technical without multipliers)"},
    "an_t_act": {"ru": "Ставка акта (поправка по уровню {adj})", "uz": "Dalolatnoma tarifi (daraja tuzatishi {adj})",
                 "en": "Report rate (level loading {adj})"},
    "an_t_act_min": {"ru": "применён минимум продукта", "uz": "mahsulot minimumi qoʻllanildi",
                     "en": "product minimum applied"},
    "an_t_act_other": {"ru": "Ставка акта", "uz": "Dalolatnoma tarifi", "en": "Report rate"},
    "an_t_market": {"ru": "Рыночная ставка класса (НАПП)", "uz": "Klassning bozor tarifi (SHNMA)",
                    "en": "Market rate for the class (NAPP)"},
    "an_t_conclusion": {"ru": "Тариф акта {act} при технической ставке расчётного модуля {tech} и рыночной {market}. Тариф акта "
                              "считается по тарифной политике; техническая ставка — справка (экспертные базовые "
                              "ставки и коэффициенты, не калибровано).",
                        "uz": "Dalolatnoma tarifi {act}, hisob-kitob modulining texnik tarifi {tech} va bozor tarifi {market}. "
                              "Dalolatnoma tarifi tarif siyosati boʻyicha hisoblanadi; texnik tarif — maʼlumot uchun "
                              "(ekspert bazaviy tariflar va koeffitsiyentlar, kalibrlanmagan).",
                        "en": "Report rate {act} against the rating module technical rate {tech} and the market rate {market}. "
                              "The report rate follows the tariff policy; the technical rate is for reference (expert "
                              "base rates and multipliers, not calibrated)."},
    "an_t_na": {"ru": "Состав тарифа недоступен: в справочнике нет базовой нетто-ставки класса {cls}",
                "uz": "Tarif tarkibi mavjud emas: maʼlumotnomada {cls}-klass bazaviy netto-tarifi yoʻq",
                "en": "Rate build-up unavailable: no base net rate for class {cls} in the reference"},
    "an_t_na_short": {"ru": "нет данных", "uz": "maʼlumot yoʻq", "en": "no data"},

    # ---------- 4. сценарии ----------
    "an_sc_fire": {"ru": "пожар: {base} ({where}) × {share} = {amount}",
                   "uz": "yongʻin: {base} ({where}) × {share} = {amount}",
                   "en": "fire: {base} ({where}) × {share} = {amount}"},
    "an_sc_eq": {"ru": "землетрясение: {base} (вся площадка, {zone}) × {share} = {amount}",
                 "uz": "zilzila: {base} (butun maydon, {zone}) × {share} = {amount}",
                 "en": "earthquake: {base} (whole site, {zone}) × {share} = {amount}"},
    "an_sc_eq_na": {"ru": "землетрясение не считалось: сейсмозона не указана",
                    "uz": "zilzila hisoblanmadi: seysmik zona koʻrsatilmagan",
                    "en": "earthquake not calculated: seismic zone not stated"},
    "an_sc_zone": {"ru": "{z}", "uz": "{z}", "en": "{z}"},
    "an_sc_zone_na": {"ru": "зона не указана — полное уничтожение", "uz": "zona koʻrsatilmagan — toʻliq vayron boʻlish",
                      "en": "zone not stated — total destruction"},
    "an_sc_c9": {"ru": "кража, залив, прочее: {base} ({where}) × {share} = {amount}",
                 "uz": "oʻgʻirlik, suv bosishi, boshqalar: {base} ({where}) × {share} = {amount}",
                 "en": "theft, water, other: {base} ({where}) × {share} = {amount}"},
    "an_sc_veh": {"ru": "единица техники: {base} (меньшее из суммы и стоимости) × {share} = {amount}",
                  "uz": "texnika birligi: {base} (summa va qiymatning kichigi) × {share} = {amount}",
                  "en": "one unit: {base} (lower of sum and value) × {share} = {amount}"},
    "an_sc_where_compartment": {"ru": "наибольший отсек", "uz": "eng katta boʻlim", "en": "largest compartment"},
    "an_sc_where_whole": {"ru": "весь объект: отсеки не указаны", "uz": "butun obyekt: boʻlimlar koʻrsatilmagan",
                          "en": "whole object: compartments not stated"},
    "an_sc_where_whole_spread": {"ru": "весь объект: огонь переходит на соседние здания",
                                 "uz": "butun obyekt: olov qoʻshni binolarga oʻtadi",
                                 "en": "whole object: fire spreads to neighbouring buildings"},
    "an_sc_taken": {"ru": "взят больший — {what}", "uz": "kattasi olindi — {what}", "en": "the larger is taken — {what}"},
    "an_sc_k": {"ru": "× доля страхования {k}", "uz": "× sugʻurta ulushi {k}", "en": "× insured share {k}"},
    "an_sc_line": {"ru": "{name} = {amount} ({pct}): {how}", "uz": "{name} = {amount} ({pct}): {how}",
                   "en": "{name} = {amount} ({pct}): {how}"},
    "an_sc_prot": {"ru": "защита: {v}", "uz": "himoya: {v}", "en": "protection: {v}"},
    "an_sc_prot_assumed": {"ru": "защита не указана — взят худший вариант «без сигнализации и охраны»",
                           "uz": "himoya koʻrsatilmagan — eng yomon variant «signalizatsiya va qoʻriqlashsiz» olindi",
                           "en": "protection not stated — worst option «no alarm or security» taken"},
    "an_sc_bi": {"ru": "простой {x}", "uz": "toʻxtab qolish {x}", "en": "business interruption {x}"},
    "an_wi_line": {"ru": "{what}: PML {pml} ({dp}), EML {eml} ({de}), MFL {mfl}{tail}",
                   "uz": "{what}: PML {pml} ({dp}), EML {eml} ({de}), MFL {mfl}{tail}",
                   "en": "{what}: PML {pml} ({dp}), EML {eml} ({de}), MFL {mfl}{tail}"},
    "an_wi_within": {"ru": "; EML в пределах удержания", "uz": "; EML ushlab qolish doirasida",
                     "en": "; EML within the retention"},
    "an_wi_excess": {"ru": "; EML выше удержания на {x}", "uz": "; EML ushlab qolishdan {x} ga yuqori",
                     "en": "; EML exceeds the retention by {x}"},
    "an_wi_same": {"ru": "без изменений", "uz": "oʻzgarishsiz", "en": "no change"},
    "an_wi_none": {"ru": "Вариантов «что если» для этого класса нет.", "uz": "Bu klass uchun «agar…» variantlari yoʻq.",
                   "en": "There are no what-if options for this class."},

    # ---------- удержание ----------
    "an_ret_law": {"ru": "Лимит на один риск по Положению № 1806, п. 15 = 20 % × (собственные средства {own} + резервы "
                         "{res}) = {lpr}{temp}.",
                   "uz": "1806-son Nizom, 15-band boʻyicha bitta xavf limiti = 20 % × (oʻz mablagʻlari {own} + "
                         "zaxiralar {res}) = {lpr}{temp}.",
                   "en": "Limit per risk under Regulation No. 1806, para. 15 = 20% × (own funds {own} + reserves "
                         "{res}) = {lpr}{temp}."},
    "an_ret_temp": {"ru": " — цифры временные, до данных бухгалтерии (источник: company_financials)",
                    "uz": " — raqamlar vaqtinchalik, buxgalteriya maʼlumotlarigacha (manba: company_financials)",
                    "en": " — provisional figures until accounting data arrive (source: company_financials)"},
    "an_ret_reported": {"ru": " — по отчётности компании", "uz": " — kompaniya hisoboti boʻyicha",
                        "en": " — per company reporting"},
    "an_ret_sum_in": {"ru": "Страховая сумма {sum} в лимит {lpr} укладывается.",
                      "uz": "Sugʻurta summasi {sum} {lpr} limitiga sigʻadi.",
                      "en": "The sum insured {sum} fits within the {lpr} limit."},
    "an_ret_line": {"ru": "Лимит по таблице линий класса {cls} — {line}: внутреннее экспертное правило "
                          "(capacity.retention_table), не норма, не калибровано.",
                    "uz": "{cls}-klass liniyalar jadvali boʻyicha limit — {line}: ichki ekspert qoidasi "
                          "(capacity.retention_table), meʼyor emas, kalibrlanmagan.",
                    "en": "Limit per the line table for class {cls} — {line}: internal expert rule "
                          "(capacity.retention_table), not a regulation, not calibrated."},
    "an_ret_calc": {"ru": "Расчётное удержание — меньшее из двух лимитов: {limit} (оценочно).",
                    "uz": "Hisoblangan ushlab qolish — ikki limitning kichigi: {limit} (taxminiy).",
                    "en": "Estimated retention — the lower of the two limits: {limit} (estimate)."},
    "an_ret_calc_one": {"ru": "Расчётное удержание: {limit} (оценочно).", "uz": "Hisoblangan ushlab qolish: {limit} "
                        "(taxminiy).", "en": "Estimated retention: {limit} (estimate)."},
    "an_ret_cmp": {"ru": "EML {eml} против расчётного удержания {limit}; MFL {mfl}.",
                   "uz": "EML {eml} va hisoblangan ushlab qolish {limit}; MFL {mfl}.",
                   "en": "EML {eml} against the estimated retention {limit}; MFL {mfl}."},
    "an_ret_est": {"ru": "оценочно, цифры временные", "uz": "taxminiy, raqamlar vaqtinchalik",
                   "en": "estimate, provisional figures"},
    "an_ret_est_rep": {"ru": "оценочно", "uz": "taxminiy", "en": "estimate"},
    "an_ret_within": {"ru": "Вывод: EML и MFL укладываются в расчётное удержание — перестрахование не требуется ({est}).",
                      "uz": "Xulosa: EML va MFL hisoblangan ushlab qolish doirasida — qayta sugʻurtalash talab "
                            "qilinmaydi ({est}).",
                      "en": "Conclusion: EML and MFL are within the estimated retention — no reinsurance needed ({est})."},
    "an_ret_mfl": {"ru": "Вывод: EML в пределах расчётного удержания; MFL выше на {x} — рекомендуем рассмотреть "
                         "факультативное перестрахование этой части или решение андеррайтера ({est}).",
                   "uz": "Xulosa: EML hisoblangan ushlab qolish doirasida; MFL {x} ga yuqori — bu qismni fakultativ "
                         "qayta sugʻurtalash yoki anderrayter qarorini koʻrib chiqishni tavsiya qilamiz ({est}).",
                   "en": "Conclusion: EML is within the estimated retention; MFL exceeds it by {x} — we recommend "
                         "considering facultative reinsurance of that part or an underwriter decision ({est})."},
    "an_ret_eml": {"ru": "Вывод: EML выше расчётного удержания по экспертной таблице на {x} — рекомендуем рассмотреть "
                         "перестрахование или решение андеррайтера ({est}); MFL выше на {y}.",
                   "uz": "Xulosa: EML ekspert jadvali boʻyicha hisoblangan ushlab qolishdan {x} ga yuqori — qayta "
                         "sugʻurtalash yoki anderrayter qarorini koʻrib chiqishni tavsiya qilamiz ({est}); MFL {y} ga "
                         "yuqori.",
                   "en": "Conclusion: EML exceeds the estimated retention from the expert table by {x} — we recommend "
                         "considering reinsurance or an underwriter decision ({est}); MFL exceeds it by {y}."},
    "an_ret_note": {"ru": "Это оценка, не факт: {what}.", "uz": "Bu taxmin, fakt emas: {what}.",
                    "en": "This is an estimate, not a fact: {what}."},
    "an_ret_note_temp": {"ru": "собственные средства и резервы — временные цифры из company_financials до данных "
                               "бухгалтерии",
                         "uz": "oʻz mablagʻlari va zaxiralar — buxgalteriya maʼlumotlarigacha company_financials dagi "
                               "vaqtinchalik raqamlar",
                         "en": "own funds and reserves are provisional figures from company_financials until "
                               "accounting data arrive"},
    "an_ret_note_line": {"ru": "таблица линий — внутреннее экспертное правило (capacity.retention_table), не норма, "
                               "не калибровано",
                         "uz": "liniyalar jadvali — ichki ekspert qoidasi (capacity.retention_table), meʼyor emas, "
                               "kalibrlanmagan",
                         "en": "the line table is an internal expert rule (capacity.retention_table), not a "
                               "regulation, not calibrated"},
    "an_ret_over20": {"ru": "Страховая сумма выше лимита 20 % на один риск — без перестрахования нарушается Положение "
                            "№ 1806, п. 15.",
                      "uz": "Sugʻurta summasi bitta xavf uchun 20 % limitdan yuqori — qayta sugʻurtalashsiz 1806-son "
                            "Nizomning 15-bandi buziladi.",
                      "en": "The sum insured exceeds the 20% per-risk limit — without reinsurance Regulation No. 1806, "
                            "para. 15 is breached."},
    "an_ret_unknown": {"ru": "Лимит удержания не посчитан: в системе нет собственных средств и резервов компании. "
                             "Введите их в админке (раздел «Финансы»: собственные средства, страховые резервы по "
                             "отчётности на последнюю дату) — лимит = 20 % × (средства + резервы), Положение № 1806, "
                             "п. 15.",
                       "uz": "Ushlab qolish limiti hisoblanmadi: tizimda kompaniyaning oʻz mablagʻlari va zaxiralari "
                             "yoʻq. Ularni admin panelga kiriting («Moliya» boʻlimi: oʻz mablagʻlari, soʻnggi sanadagi "
                             "hisobot boʻyicha sugʻurta zaxiralari) — limit = 20 % × (mablagʻlar + zaxiralar), 1806-son "
                             "Nizom, 15-band.",
                       "en": "The retention limit was not calculated: the company's own funds and reserves are not in "
                             "the system. Enter them in the admin panel (Finance: own funds, insurance reserves as of "
                             "the latest reporting date) — limit = 20% × (funds + reserves), Regulation No. 1806, "
                             "para. 15."},

    # ---------- 5. балл ----------
    "an_sc_score": {"ru": "Балл риска (справочно) — {score} из 100 ({level}); уровень акта — {act} (по четырём признакам). "
                          "Балл — справочно, на тариф акта не влияет. Шкала экспертная: до {b1} — низкий, до {b2} — "
                          "умеренный, до {b3} — повышенный, до {b4} — высокий, выше — критический.",
                    "uz": "Xavf bali (maʼlumot uchun) — 100 dan {score} ({level}); dalolatnoma darajasi — {act} (toʻrt belgi "
                          "boʻyicha). Ball — maʼlumot uchun, dalolatnoma tarifiga taʼsir qilmaydi. Shkala ekspert: "
                          "{b1} gacha — past, {b2} gacha — oʻrtacha, {b3} gacha — oshgan, {b4} gacha — yuqori, "
                          "undan yuqori — juda yuqori.",
                    "en": "Risk score (for reference) — {score} out of 100 ({level}); report level — {act} (by four indicators). "
                          "The score is for reference and does not affect the report rate. Expert scale: up to {b1} — "
                          "low, up to {b2} — moderate, up to {b3} — elevated, up to {b4} — high, above — critical."},
    "an_sc_comp": {"ru": "{name}: {points} × вес {weight} = {contrib} ({why})",
                   "uz": "{name}: {points} × vazn {weight} = {contrib} ({why})",
                   "en": "{name}: {points} × weight {weight} = {contrib} ({why})"},
    "an_sc_comp_na": {"ru": "{name}: не учтено ({why})", "uz": "{name}: hisobga olinmadi ({why})",
                      "en": "{name}: not counted ({why})"},
    "an_why_rate": {"ru": "техническая ставка к ориентиру = {v}; {lo} и ниже → 0, {hi} и выше → 100",
                    "uz": "texnik tarifning moʻljalga nisbati = {v}; {lo} va past → 0, {hi} va yuqori → 100",
                    "en": "technical rate to benchmark = {v}; {lo} or less → 0, {hi} or more → 100"},
    "an_why_rate_na": {"ru": "нет минимума и рынка", "uz": "minimum va bozor yoʻq", "en": "no minimum or market"},
    "an_why_mfl_ret": {"ru": "MFL к удержанию = {v}; {lo} и ниже → 0, {hi} и выше → 100",
                       "uz": "MFL ning ushlab qolishga nisbati = {v}; {lo} va past → 0, {hi} va yuqori → 100",
                       "en": "MFL to retention = {v}; {lo} or less → 0, {hi} or more → 100"},
    "an_why_mfl_sum": {"ru": "данных компании нет — MFL {v} страховой суммы",
                       "uz": "kompaniya maʼlumoti yoʻq — MFL sugʻurta summasining {v}",
                       "en": "no company data — MFL is {v} of the sum insured"},
    "an_why_losses": {"ru": "убытков за 3 года: {v}", "uz": "3 yilda zararlar: {v}", "en": "losses in 3 years: {v}"},
    "an_why_unknown": {"ru": "нет данных — средний балл {v}", "uz": "maʼlumot yoʻq — oʻrtacha ball {v}",
                       "en": "no data — average score {v}"},
    "an_why_itv": {"ru": "сумма — {v} стоимости", "uz": "summa — qiymatning {v}", "en": "sum is {v} of value"},
    "an_why_seismic": {"ru": "{v}", "uz": "{v}", "en": "{v}"},
    "an_why_seismic_na": {"ru": "землетрясение не в покрытии класса", "uz": "zilzila klass qoplamasida emas",
                          "en": "earthquake is not covered in this class"},
    "an_why_ext": {"ru": "регион к республике = {v} (stat.uz, data.egov.uz)",
                   "uz": "hudud respublikaga nisbatan = {v} (stat.uz, data.egov.uz)",
                   "en": "region to country = {v} (stat.uz, data.egov.uz)"},
    "an_why_ext_na": {"ru": "нет показателей региона для класса", "uz": "klass uchun hudud koʻrsatkichlari yoʻq",
                      "en": "no regional indicators for the class"},
    "an_why_ext_kind": {"ru": "нет показателей региона для этого вида объекта",
                        "uz": "bu obyekt turi uchun hudud koʻrsatkichlari yoʻq",
                        "en": "no regional indicators for this type of object"},
    "an_why_ext_region": {"ru": "регион не распознан — показатели даны по республике, сравнивать не с чем",
                          "uz": "hudud aniqlanmadi — koʻrsatkichlar respublika boʻyicha, taqqoslash uchun asos yoʻq",
                          "en": "region not recognised — indicators are for the whole country, nothing to compare"},
    "an_why_ext_noreg": {"ru": "по классу нет показателей с разрезом по регионам — даны по республике для справки",
                         "uz": "klass boʻyicha hududlar kesimidagi koʻrsatkichlar yoʻq — maʼlumot uchun respublika "
                               "boʻyicha berilgan",
                         "en": "no indicators broken down by region for the class — country figures for reference"},

    # ---------- 6. рынок и статистика ----------
    "an_m_rate": {"ru": "Рыночная ставка класса {cls}{pack}: {rate} годовых на {date} ({months} мес.)",
                  "uz": "{cls}-klass bozor tarifi{pack}: {date} holatiga yillik {rate} ({months} oy)",
                  "en": "Market rate, class {cls}{pack}: {rate} per year as of {date} ({months} months)"},
    "an_m_pack": {"ru": " (строка классов {pack})", "uz": " ({pack} klasslar qatori)",
                  "en": " (row for classes {pack})"},
    "an_m_fy": {"ru": "За {year} год: ставка {rate}, убыточность {lr}", "uz": "{year} yil uchun: tarif {rate}, "
                "zararlilik {lr}", "en": "For {year}: rate {rate}, loss ratio {lr}"},
    "an_m_row": {"ru": "Взята строка классов {pack} ({rk}) — премии {pp} млн сум на {date}; отдельная строка класса "
                       "{cls} ({alt}) — {ap} млн сум ({share} от строки {pack}), мала по объёму.",
                 "uz": "{pack} klasslar qatori ({rk}) olindi — {date} holatiga mukofotlar {pp} mln soʻm; {cls}-klass "
                       "alohida qatori ({alt}) — {ap} mln soʻm ({pack} qatorining {share}), hajmi kichik.",
                 "en": "The row for classes {pack} ({rk}) is used — premiums {pp} million UZS as of {date}; the "
                       "separate class {cls} row ({alt}) — {ap} million UZS ({share} of the {pack} row), too small."},
    "an_m_row_only": {"ru": "Взята строка классов {pack} ({rk}): отдельной строки класса {cls} в отчёте нет.",
                      "uz": "{pack} klasslar qatori ({rk}) olindi: hisobotda {cls}-klassning alohida qatori yoʻq.",
                      "en": "The row for classes {pack} ({rk}) is used: the report has no separate class {cls} row."},
    "an_m_lr": {"ru": "Убыточность класса (выплаты / премии): {lr} на {date}",
                "uz": "Klass zararliligi (toʻlovlar / mukofotlar): {date} holatiga {lr}",
                "en": "Class loss ratio (claims paid / premiums): {lr} as of {date}"},
    "an_m_lr_label": {"ru": "Убыточность класса (выплаты / премии)", "uz": "Klass zararliligi (toʻlovlar / mukofotlar)",
                      "en": "Class loss ratio (claims paid / premiums)"},
    "an_m_fy_label": {"ru": "Ставка и убыточность рынка за {year} год", "uz": "{year} yil bozor tarifi va zararliligi",
                      "en": "Market rate and loss ratio for {year}"},
    "an_m_fy_rate_label": {"ru": "Рыночная ставка за {year} год", "uz": "{year} yil bozor tarifi",
                           "en": "Market rate for {year}"},
    "an_m_fy_lr_label": {"ru": "Убыточность рынка за {year} год", "uz": "{year} yil bozor zararliligi",
                         "en": "Market loss ratio for {year}"},
    "an_m_cmp_label": {"ru": "Ставка акта к рыночной", "uz": "Dalolatnoma tarifining bozorga nisbati",
                       "en": "Report rate against the market"},
    "an_m_cmp_above": {"ru": "Ставка акта {act} выше рынка на {pp} п. п. ({pct})",
                       "uz": "Dalolatnoma tarifi {act} bozordan {pp} f. p. ga yuqori ({pct})",
                       "en": "The report rate {act} is above the market by {pp} pp ({pct})"},
    "an_m_cmp_below": {"ru": "Ставка акта {act} ниже рынка на {pp} п. п. ({pct})",
                       "uz": "Dalolatnoma tarifi {act} bozordan {pp} f. p. ga past ({pct})",
                       "en": "The report rate {act} is below the market by {pp} pp ({pct})"},
    "an_m_cmp_tech": {"ru": "техническая ставка расчётного модуля {tech} — {pct} к рынку",
                      "uz": "hisob-kitob modulining texnik tarifi {tech} — bozorga nisbatan {pct}",
                      "en": "rating module technical rate {tech} — {pct} against the market"},
    "an_m_na": {"ru": "Рыночной ставки по классу {cls} нет в данных НАПП — нет данных",
                "uz": "SHNMA maʼlumotlarida {cls}-klass boʻyicha bozor tarifi yoʻq — maʼlumot yoʻq",
                "en": "There is no market rate for class {cls} in the NAPP data — no data"},
    "an_m_src": {"ru": "Источник: НАПП — отчёт о страховом рынке{file}, срез на {date} — {url}",
                 "uz": "Manba: SHNMA — sugʻurta bozori hisoboti, {date} holatiga kesim — {url}",
                 "en": "Source: NAPP — insurance market report, snapshot as of {date} — {url}"},
    "an_m_gap": {"ru": "в ряду НАПП нет срезов: {what}", "uz": "SHNMA qatorida kesimlar yoʻq: {what}",
                 "en": "the NAPP series is missing snapshots: {what}"},
    "an_st_line": {"ru": "{name} ({where}, {period}): {value}{cmp}", "uz": "{name} ({where}, {period}): {value}{cmp}",
                   "en": "{name} ({where}, {period}): {value}{cmp}"},
    "an_st_region": {"ru": "регион", "uz": "hudud", "en": "region"},
    "an_st_republic": {"ru": "по республике", "uz": "respublika boʻyicha", "en": "whole country"},
    "an_st_cmp": {"ru": "; к республике {pct}{pts}", "uz": "; respublikaga nisbatan {pct}{pts}",
                  "en": "; against the country {pct}{pts}"},
    "an_st_pts": {"ru": ", в балле {p}", "uz": ", ballda {p}", "en": ", {p} in the score"},
    "an_st_kind": {"ru": "; в балл не входит: показатель жилого фонда, а объект — {kind}",
                   "uz": "; ballga kirmaydi: uy-joy fondi koʻrsatkichi, obyekt esa — {kind}",
                   "en": "; not in the score: a housing-stock indicator, while the object is {kind}"},
    "an_st_per1000": {"ru": " ({v} на 1 000 жителей)", "uz": " (1 000 aholiga {v})", "en": " ({v} per 1,000 people)"},
    "an_st_nodata": {"ru": "нет данных", "uz": "maʼlumot yoʻq", "en": "no data"},
    "an_st_src": {"ru": "Источник: {src} — {name}, период {period}, загружено {fetched} — {url}",
                  "uz": "Manba: {src} — {name}, davr {period}, yuklangan {fetched} — {url}",
                  "en": "Source: {src} — {name}, period {period}, loaded {fetched} — {url}"},
    "an_st_src_nf": {"ru": "Источник: {src} — {name}, период {period} — {url}",
                     "uz": "Manba: {src} — {name}, davr {period} — {url}",
                     "en": "Source: {src} — {name}, period {period} — {url}"},
    "an_st_none": {"ru": "Статистики региона по классу {cls} в загруженных наборах нет — нет данных",
                   "uz": "Yuklangan toʻplamlarda {cls}-klass boʻyicha hudud statistikasi yoʻq — maʼlumot yoʻq",
                   "en": "There are no regional statistics for class {cls} in the loaded datasets — no data"},
    "an_st_nf": {"ru": "Нет в открытых данных: {what}", "uz": "Ochiq maʼlumotlarda yoʻq: {what}",
                 "en": "Not in open data: {what}"},

    # ---------- 7. франшиза ----------
    "an_fr_line": {"ru": "Франшиза {pct} ({amount} с каждого убытка): ставка {rate}, премия {premium}, экономия "
                         "{saving}{note}",
                   "uz": "Franshiza {pct} (har bir zarardan {amount}): tarif {rate}, mukofot {premium}, tejam "
                         "{saving}{note}",
                   "en": "Deductible {pct} ({amount} per loss): rate {rate}, premium {premium}, saving {saving}{note}"},
    "an_fr_floor": {"ru": " — упирается в минимум продукта", "uz": " — mahsulot minimumiga yetadi",
                    "en": " — hits the product minimum"},
    "an_fr_extra": {"ru": " — выше 2 % множитель экспертный", "uz": " — 2 % dan yuqorida koeffitsiyent ekspert",
                    "en": " — above 2% the multiplier is expert"},
    "an_fr_na_row": {"ru": "Франшиза {pct}: эффект не посчитан", "uz": "Franshiza {pct}: taʼsir hisoblanmadi",
                     "en": "Deductible {pct}: effect not calculated"},
    "an_fr_note": {"ru": "Таблица справочная: вывод акта о франшизе — «{verdict}». Премия с франшизой = ставка акта × "
                         "множитель модуля франшизы, не ниже минимума продукта. Экспертно, не калибровано.",
                   "uz": "Jadval maʼlumot uchun: dalolatnomaning franshiza boʻyicha xulosasi — «{verdict}». Franshizali "
                         "mukofot = dalolatnoma tarifi × franshiza moduli koeffitsiyenti, mahsulot "
                         "minimumidan past emas. Ekspert, kalibrlanmagan.",
                   "en": "The table is for reference: the report's deductible conclusion is «{verdict}». Premium with "
                         "a deductible = report rate × deductible module multiplier, not below the product "
                         "minimum. Expert values, not calibrated."},
    "an_fr_na_statutory": {"ru": "Обязательный вид: франшиза не применяется — таблица вариантов не строится",
                           "uz": "Majburiy tur: franshiza qoʻllanilmaydi — variantlar jadvali tuzilmaydi",
                           "en": "Compulsory class: no deductible — no options table"},
    "an_fr_na_no_rate": {"ru": "Ставка акта не определена — премию с франшизой посчитать нельзя",
                         "uz": "Dalolatnoma tarifi aniqlanmagan — franshizali mukofotni hisoblab boʻlmaydi",
                         "en": "The report rate is not determined — the premium with a deductible cannot be calculated"},
    "an_fr_na_no_engine": {"ru": "Модуль франшизы не дал расчёта", "uz": "Franshiza moduli hisob bermadi",
                           "en": "The deductible module returned no result"},
    "an_fr_na_error": {"ru": "Таблица франшиз не посчитана", "uz": "Franshizalar jadvali hisoblanmadi",
                       "en": "The deductible table was not calculated"},

    # ---------- 8. мероприятия ----------
    "an_ms_line": {"ru": "{text}: техническая ставка {before} → {after} ({eff}); премия акта {delta}",
                   "uz": "{text}: texnik tarif {before} → {after} ({eff}); dalolatnoma mukofoti {delta}",
                   "en": "{text}: technical rate {before} → {after} ({eff}); report premium {delta}"},
    "an_ms_line_na": {"ru": "{text}: на ставку не влияет, снижает вероятность убытка",
                      "uz": "{text}: tarifga taʼsir qilmaydi, zarar ehtimolini kamaytiradi",
                      "en": "{text}: no effect on the rate, reduces the likelihood of a loss"},
    "an_ms_zero": {"ru": "без изменения (минимум продукта)", "uz": "oʻzgarishsiz (mahsulot minimumi)",
                   "en": "no change (product minimum)"},
    "an_ms_total": {"ru": "Если выполнить все: техническая ставка {before} → {after}; премия акта {pb} → {pa}.",
                    "uz": "Hammasi bajarilsa: texnik tarif {before} → {after}; dalolatnoma mukofoti {pb} → {pa}.",
                    "en": "If all are carried out: technical rate {before} → {after}; report premium {pb} → {pa}."},
    "an_ms_none": {"ru": "Мероприятий с эффектом на ставку нет.", "uz": "Tarifga taʼsir qiluvchi tadbirlar yoʻq.",
                   "en": "There are no measures with an effect on the rate."},

    # ---------- 9. резюме ----------
    "an_sum_risks": {"ru": "Главные риски объекта — {what}.", "uz": "Obyektning asosiy xavflari — {what}.",
                     "en": "The main risks of the object are {what}."},
    "an_sum_risk_item": {"ru": "{name} ({share} нетто-ставки)", "uz": "{name} (netto-tarifning {share})",
                         "en": "{name} ({share} of the net rate)"},
    "an_sum_whole": {"ru": "Класс {cls} в справочнике не разбит на отдельные риски: ставка покрывает ущерб, угон и "
                           "гибель техники вместе.",
                     "uz": "{cls}-klass maʼlumotnomada alohida xavflarga boʻlinmagan: tarif texnikaning zarari, "
                           "oʻgʻirlanishi va nobud boʻlishini birga qoplaydi.",
                     "en": "Class {cls} is not split into separate risks in the reference: the rate covers damage, "
                           "theft and total loss together."},
    "an_sum_up": {"ru": "Повышает риск: {what}.", "uz": "Xavfni oshiradi: {what}.", "en": "Increases the risk: {what}."},
    "an_sum_down": {"ru": "Снижает риск: {what}.", "uz": "Xavfni kamaytiradi: {what}.", "en": "Reduces the risk: {what}."},
    "an_sum_nodown": {"ru": "Снижающих риск факторов в данных нет.",
                      "uz": "Maʼlumotlarda xavfni kamaytiruvchi omillar yoʻq.",
                      "en": "The data contains no risk-reducing factors."},
    "an_sum_tariff": {"ru": "Тариф акта {act} — ставка тарифной политики {base} с поправкой +{adj} за {level} уровень "
                            "риска{min}; техническая ставка расчётного модуля {tech}{market}.",
                      "uz": "Dalolatnoma tarifi {act} — tarif siyosati tarifi {base}, «{level}» xavf darajasi uchun "
                            "+{adj} tuzatish bilan{min}; hisob-kitob modulining texnik tarifi {tech}{market}.",
                      "en": "The report rate {act} is the tariff policy rate {base} with a +{adj} loading for the "
                            "{level} risk level{min}; the rating module technical rate is {tech}{market}."},
    "an_sum_tariff_tech": {"ru": "Тариф акта {act} — техническая ставка без коэффициентов {base} с поправкой +{adj} за "
                                 "{level} уровень риска{min}; техническая ставка расчётного модуля с факторами {tech}{market}.",
                           "uz": "Dalolatnoma tarifi {act} — koeffitsiyentsiz texnik tarif {base}, «{level}» xavf "
                                 "darajasi uchun +{adj} tuzatish bilan{min}; omillar bilan hisob-kitob modulining texnik tarifi "
                                 "{tech}{market}.",
                           "en": "The report rate {act} is the technical rate without multipliers {base} with a +{adj} "
                                 "loading for the {level} risk level{min}; the rating module technical rate with factors is "
                                 "{tech}{market}."},
    "an_sum_min": {"ru": ", не ниже минимума {m}", "uz": ", {m} minimumidan past emas", "en": ", not below the minimum {m}"},
    "an_sum_market": {"ru": ", рынок {m}", "uz": ", bozor {m}", "en": ", market {m}"},
    "an_sum_tariff_other": {"ru": "Тариф акта: {what}.", "uz": "Dalolatnoma tarifi: {what}.",
                            "en": "Report rate: {what}."},
    "an_sum_loss": {"ru": "Крупный убыток: EML {eml}, MFL {mfl}; {ret}.", "uz": "Yirik zarar: EML {eml}, MFL {mfl}; "
                    "{ret}.", "en": "Large loss: EML {eml}, MFL {mfl}; {ret}."},
    "an_sum_ret_within": {"ru": "это в пределах расчётного удержания ({est})",
                          "uz": "bu hisoblangan ushlab qolish doirasida ({est})",
                          "en": "this is within the estimated retention ({est})"},
    "an_sum_ret_eml": {"ru": "EML выше расчётного удержания по экспертной таблице — рекомендуем рассмотреть "
                             "перестрахование или решение андеррайтера ({est})",
                       "uz": "EML ekspert jadvali boʻyicha hisoblangan ushlab qolishdan yuqori — qayta sugʻurtalash "
                             "yoki anderrayter qarorini koʻrib chiqishni tavsiya qilamiz ({est})",
                       "en": "EML exceeds the estimated retention from the expert table — we recommend considering "
                             "reinsurance or an underwriter decision ({est})"},
    "an_sum_ret_mfl": {"ru": "EML в пределах расчётного удержания {limit}, MFL выше — рекомендуем рассмотреть "
                             "перестрахование ({est})",
                       "uz": "EML hisoblangan ushlab qolish {limit} doirasida, MFL yuqori — qayta sugʻurtalashni "
                             "koʻrib chiqishni tavsiya qilamiz ({est})",
                       "en": "EML is within the estimated retention {limit}, MFL exceeds it — we recommend considering "
                             "reinsurance ({est})"},
    "an_sum_ret_na": {"ru": "удержание не посчитано — нет собственных средств и резервов",
                      "uz": "ushlab qolish hisoblanmadi — oʻz mablagʻlari va zaxiralar yoʻq",
                      "en": "retention not calculated — no own funds and reserves"},
    "an_sum_advice": {"ru": "Советуем: {what}.", "uz": "Tavsiya: {what}.", "en": "We advise: {what}."},
    "an_sum_advice_measure": {"ru": "{text} (техническая ставка {eff})", "uz": "{text} (texnik tarif {eff})",
                              "en": "{text} (technical rate {eff})"},
    "an_sum_advice_fr_no": {"ru": "франшиза не требуется — оснований нет",
                            "uz": "franshiza talab qilinmaydi — asos yoʻq",
                            "en": "no deductible is required — there are no grounds"},
    "an_sum_advice_fr": {"ru": "решить вопрос о франшизе", "uz": "franshiza masalasini hal qilish",
                         "en": "decide on the deductible"},
    "an_sum_clarify": {"ru": "Уточните: {what} — это может изменить оценку.",
                       "uz": "Aniqlashtiring: {what} — bu baholashni oʻzgartirishi mumkin.",
                       "en": "Please clarify: {what} — this may change the assessment."},
    "an_sum_score": {"ru": "Балл риска (справочно) — {score} из 100 ({level}).",
                     "uz": "Xavf bali (maʼlumot uchun) — 100 dan {score} ({level}).",
                     "en": "Risk score (for reference) — {score} out of 100 ({level})."},
}
