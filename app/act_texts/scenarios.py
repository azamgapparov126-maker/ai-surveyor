"""Сценарии убытка, разбор документов, применение франшизы, рекомендации (29.09.2026).
Часть словаря текстов акта (app/act_texts): склеивается в TX в __init__.py в прежнем порядке."""

FR_TYPE_LABELS = {
    "unconditional": {"ru": "безусловная", "uz": "shartsiz", "en": "unconditional"},
    "conditional": {"ru": "условная", "uz": "shartli", "en": "conditional"},
    "peril": {"ru": "по отдельному риску", "uz": "alohida xavf boʻyicha", "en": "per peril"},
}

# значения, подставленные по умолчанию для старого движка (коды risk_analytics)
RA_VALUE_LABELS = {
    "reinforced": {"ru": "железобетон, кирпич", "uz": "temir-beton, gʻisht", "en": "reinforced concrete, brick"},
    "mixed": {"ru": "смешанные конструкции", "uz": "aralash konstruksiyalar", "en": "mixed construction"},
    "wood": {"ru": "дерево, сэндвич-панели", "uz": "yogʻoch, sendvich-panellar", "en": "timber, sandwich panels"},
    "office": {"ru": "офис, торговля", "uz": "ofis, savdo", "en": "office, retail"},
    "warehouse": {"ru": "склад общего назначения", "uz": "umumiy ombor", "en": "general warehouse"},
    "food": {"ru": "пищевое производство", "uz": "oziq-ovqat ishlab chiqarish", "en": "food production"},
    "flammable": {"ru": "работа с горючими материалами", "uz": "yonuvchan materiallar bilan ishlash",
                  "en": "work with flammable materials"},
    "car": {"ru": "легковой", "uz": "yengil", "en": "passenger car"},
    "truck": {"ru": "грузовой", "uz": "yuk", "en": "truck"},
    "special": {"ru": "спецтехника", "uz": "maxsus texnika", "en": "special machinery"},
    "ev": {"ru": "электромобиль", "uz": "elektromobil", "en": "electric car"},
}

# ================================================================================================
#  Дополнения 29.09.2026: сценарии убытка, разбор документов, применение франшизы, рекомендации
# ================================================================================================
TX_SCENARIOS = {
    # ---------- сценарии ----------
    "sc_pml": {"ru": "PML — вероятный максимальный убыток", "uz": "PML — ehtimoliy maksimal zarar",
               "en": "PML — probable maximum loss"},
    "sc_eml": {"ru": "EML — оценочный максимальный убыток", "uz": "EML — baholangan maksimal zarar",
               "en": "EML — estimated maximum loss"},
    "sc_mfl": {"ru": "MFL — максимально возможный убыток", "uz": "MFL — maksimal mumkin boʻlgan zarar",
               "en": "MFL — maximum foreseeable loss"},
    "sc_value": {"ru": "{amount} ({pct} страховой суммы)", "uz": "{amount} (sugʻurta summasining {pct})",
                 "en": "{amount} ({pct} of the sum insured)"},
    "sc_defs": {"ru": "Сценарии убытка простыми словами: PML — вероятный максимальный убыток при работающей "
                      "защите; EML — оценочный максимальный убыток; MFL — максимально возможный убыток, если защита "
                      "откажет или случится катастрофа. Суммы — экспертная оценка, не калибровано.",
                "uz": "Zarar ssenariylari oddiy soʻzlar bilan: PML — himoya ishlaganda ehtimoliy maksimal zarar; "
                      "EML — baholangan maksimal zarar; MFL — himoya ishlamay qolsa yoki falokat yuz bersa, maksimal "
                      "mumkin boʻlgan zarar. Summalar — ekspert bahosi, kalibrlanmagan.",
                "en": "Loss scenarios in plain words: PML — the probable maximum loss with protection working; "
                      "EML — the estimated maximum loss; MFL — the maximum possible loss if protection fails or a "
                      "catastrophe occurs. Amounts are expert estimates, not calibrated."},
    "sc_what_veh_eml": {"ru": "крупная авария с ремонтом", "uz": "taʼmirlanadigan yirik avariya",
                        "en": "a major accident with repair"},
    "sc_what_veh_pml": {"ru": "угон или полная гибель: иммобилайзера или спутникового поиска нет",
                        "uz": "oʻgʻirlash yoki toʻliq nobud boʻlish: immobilayzer yoki sunʼiy yoʻldosh qidiruvi yoʻq",
                        "en": "theft or total loss: no immobiliser or satellite tracking"},
    "sc_what_veh_pml_prot": {"ru": "конструктивная гибель (ремонт дороже 75 % стоимости)",
                             "uz": "konstruktiv nobud boʻlish (taʼmir qiymatning 75 % idan qimmat)",
                             "en": "constructive total loss (repair above 75% of value)"},
    "sc_what_veh_mfl": {"ru": "полная гибель или угон", "uz": "toʻliq nobud boʻlish yoki oʻgʻirlash",
                        "en": "total loss or theft"},
    "sc_what_fire": {"ru": "пожар в наибольшем противопожарном отсеке",
                     "uz": "eng katta yongʻinga qarshi boʻlimdagi yongʻin",
                     "en": "fire in the largest fire compartment"},
    "sc_what_eq": {"ru": "землетрясение по всей площадке", "uz": "butun maydon boʻyicha zilzila",
                   "en": "earthquake across the whole site"},
    "sc_what_c9": {"ru": "кража, залив и прочий ущерб в наибольшем помещении",
                   "uz": "eng katta xonada oʻgʻirlik, suv bosishi va boshqa zarar",
                   "en": "theft, water and other damage in the largest premises"},
    # ---------- сценарии в порядке заказчика PML ≤ EML ≤ MFL (акты с order = classic) ----------
    "sc_defs_classic": {
        "ru": "Сценарии убытка простыми словами (PML ≤ EML ≤ MFL): PML — вероятный максимальный убыток, когда защита "
              "сработала штатно (наименьший); EML — оценочный максимальный убыток, когда защита сработала частично — "
              "именно с ним сравнивается лимит собственного удержания; MFL — максимально возможный убыток при отказе "
              "защиты или катастрофе; если MFL выше удержания — нужно перестрахование или решение андеррайтера. "
              "Суммы — экспертная оценка, не калибровано.",
        "uz": "Zarar ssenariylari oddiy soʻzlar bilan (PML ≤ EML ≤ MFL): PML — himoya meʼyorida ishlaganda ehtimoliy "
              "maksimal zarar (eng kichigi); EML — himoya qisman ishlaganda baholangan maksimal zarar — oʻz ushlab "
              "qolish limiti aynan u bilan solishtiriladi; MFL — himoya ishlamay qolganda yoki falokatda maksimal "
              "mumkin boʻlgan zarar; MFL ushlab qolishdan yuqori boʻlsa — qayta sugʻurtalash yoki anderrayter qarori "
              "kerak. Summalar — ekspert bahosi, kalibrlanmagan.",
        "en": "Loss scenarios in plain words (PML ≤ EML ≤ MFL): PML — the probable maximum loss when protection works "
              "as intended (the smallest); EML — the estimated maximum loss when protection works only partly — the "
              "own retention limit is compared with it; MFL — the maximum possible loss if protection fails or a "
              "catastrophe occurs; if MFL exceeds the retention, reinsurance or an underwriter decision is needed. "
              "Amounts are expert estimates, not calibrated."},
    "sc_state_pml": {"ru": "защита сработала штатно", "uz": "himoya meʼyorida ishladi", "en": "protection worked"},
    "sc_state_eml": {"ru": "защита сработала частично", "uz": "himoya qisman ishladi",
                     "en": "protection worked only partly"},
    "sc_state_mfl": {"ru": "защита не сработала или катастрофа", "uz": "himoya ishlamadi yoki falokat",
                     "en": "protection failed or a catastrophe"},
    "sc_w_veh_pml": {"ru": "крупная авария с ремонтом — угона и полной гибели нет",
                     "uz": "taʼmirlanadigan yirik avariya — oʻgʻirlash va toʻliq nobud boʻlish yoʻq",
                     "en": "a major accident with repair — no theft or total loss"},
    "sc_w_veh_eml_prot": {"ru": "конструктивная гибель (ремонт дороже 75 % стоимости): противоугонная система есть, "
                                "угон ею сдержан",
                          "uz": "konstruktiv nobud boʻlish (taʼmir qiymatning 75 % idan qimmat): oʻgʻirlikka qarshi "
                                "tizim bor, oʻgʻirlash toʻxtatilgan",
                          "en": "constructive total loss (repair above 75% of value): an anti-theft system is fitted "
                                "and holds back theft"},
    "sc_w_veh_eml_noprot": {"ru": "угон или полная гибель: противоугонной системы с иммобилайзером или поиском нет, "
                                  "сдерживать угон нечему",
                            "uz": "oʻgʻirlash yoki toʻliq nobud boʻlish: immobilayzer yoki qidiruvli oʻgʻirlikka qarshi "
                                  "tizim yoʻq",
                            "en": "theft or total loss: there is no anti-theft system with an immobiliser or tracking"},
    "sc_w_veh_mfl": {"ru": "полная гибель или угон — защита не сработала",
                     "uz": "toʻliq nobud boʻlish yoki oʻgʻirlash — himoya ishlamadi",
                     "en": "total loss or theft — protection failed"},
    "sc_w_fire": {"ru": "пожар в наибольшем противопожарном отсеке", "uz": "eng katta yongʻinga qarshi boʻlimdagi yongʻin",
                  "en": "fire in the largest fire compartment"},
    "sc_w_fire_whole": {"ru": "пожар: отсеки не указаны — весь объект считается одним отсеком",
                        "uz": "yongʻin: boʻlimlar koʻrsatilmagan — butun obyekt bitta boʻlim deb hisoblanadi",
                        "en": "fire: compartments not stated — the whole object is one compartment"},
    "sc_w_eq": {"ru": "землетрясение по всей площадке ({zone} баллов)", "uz": "butun maydon boʻyicha zilzila ({zone} ball)",
                "en": "earthquake across the whole site ({zone} points)"},
    "sc_w_total": {"ru": "полное уничтожение всего объекта: сейсмозона не указана — катастрофа принята как полная гибель",
                   "uz": "butun obyektning toʻliq vayron boʻlishi: seysmik zona koʻrsatilmagan — falokat toʻliq nobud "
                         "boʻlish deb qabul qilindi",
                   "en": "total destruction of the whole object: seismic zone not stated — the catastrophe is taken as "
                         "a total loss"},
    "sc_w_c9": {"ru": "кража, залив и прочий ущерб в наибольшем помещении",
                "uz": "eng katta xonada oʻgʻirlik, suv bosishi va boshqa zarar",
                "en": "theft, water and other damage in the largest premises"},
    "sc_w_c9_whole": {"ru": "кража, залив и прочий ущерб: помещения не указаны — весь объект считается одним помещением",
                      "uz": "oʻgʻirlik, suv bosishi va boshqa zarar: xonalar koʻrsatilmagan — butun obyekt bitta xona "
                            "deb hisoblanadi",
                      "en": "theft, water and other damage: premises not stated — the whole object is one premises"},
    "sc_how_names": {"ru": "Названия сценариев в акте и в модуле аналитики рисков совпадают (с 13.10.2026): PML — "
                           "защита сработала штатно, EML — частично, MFL — не сработала; PML ≤ EML ≤ MFL",
                     "uz": "Dalolatnomadagi va xavf tahlili modulidagi ssenariy nomlari bir xil (13.10.2026 dan): PML — "
                           "himoya odatdagidek ishladi, EML — qisman, MFL — ishlamadi; PML ≤ EML ≤ MFL",
                     "en": "Scenario names in the report and in the risk analytics module are the same (since "
                           "13.10.2026): PML — protection worked as intended, EML — partly, MFL — failed; "
                           "PML ≤ EML ≤ MFL"},
    "sc_ret_eml_within": {"ru": "EML укладывается в удержание", "uz": "EML ushlab qolish doirasida",
                          "en": "EML is within the retention"},
    "sc_ret_eml_excess": {"ru": "EML выше расчётного удержания по экспертной таблице на {x} — рекомендуем рассмотреть "
                                "перестрахование или решение андеррайтера (оценочно, цифры временные)",
                          "uz": "EML ekspert jadvali boʻyicha hisoblangan ushlab qolishdan {x} ga yuqori — qayta "
                                "sugʻurtalash yoki anderrayter qarorini koʻrib chiqishni tavsiya qilamiz (taxminiy, "
                                "raqamlar vaqtinchalik)",
                          "en": "EML exceeds the estimated retention from the expert table by {x} — we recommend "
                                "considering reinsurance or an underwriter decision (estimate, provisional figures)"},
    "sc_ret_mfl_excess": {"ru": "справочно: MFL выше удержания на {x} — нужно перестрахование или решение андеррайтера",
                          "uz": "maʼlumot uchun: MFL ushlab qolishdan {x} ga yuqori — qayta sugʻurtalash yoki "
                                "anderrayter qarori kerak",
                          "en": "for reference: MFL exceeds the retention by {x} — reinsurance or an underwriter "
                                "decision is needed"},
    "sc_ret_mfl_within": {"ru": "справочно: MFL тоже укладывается в удержание",
                          "uz": "maʼlumot uchun: MFL ham ushlab qolish doirasida",
                          "en": "for reference: MFL is also within the retention"},
    "as_protection_veh_eml": {"ru": "противоугонная система не указана — EML взят как без иммобилайзера и поиска — "
                                    "принято по умолчанию",
                              "uz": "oʻgʻirlikka qarshi tizim koʻrsatilmagan — EML immobilayzer va qidiruvsiz deb "
                                    "olindi — standart boʻyicha qabul qilindi",
                              "en": "anti-theft system not stated — EML taken as without immobiliser or tracking — "
                                    "assumed by default"},
    "as_protection_ignored": {"ru": "защита объекта указана, но для этого класса сценарии по защите не считаются — "
                                    "значение не учтено",
                              "uz": "obyekt himoyasi koʻrsatilgan, lekin bu klass uchun himoya boʻyicha ssenariylar "
                                    "hisoblanmaydi — qiymat hisobga olinmadi",
                              "en": "object protection was given, but this class has no protection-based scenarios — "
                                    "the value was not used"},
    "sc_retention": {"ru": "Лимит собственного удержания", "uz": "Oʻz ushlab qolish limiti",
                     "en": "Own retention limit"},
    "sc_ret_unknown": {"ru": "не задан — в системе нет собственных средств и резервов компании",
                       "uz": "belgilanmagan — tizimda kompaniyaning oʻz mablagʻlari va zaxiralari yoʻq",
                       "en": "not set — the company's own funds and reserves are not in the system"},
    "sc_ret_reported": {"ru": "по отчётности компании", "uz": "kompaniya hisoboti boʻyicha",
                        "en": "per company reporting"},
    "sc_ret_temporary": {"ru": "по временным цифрам собственных средств и резервов (до данных бухгалтерии) — оценочно",
                         "uz": "oʻz mablagʻlari va zaxiralarning vaqtinchalik raqamlari boʻyicha (buxgalteriya "
                               "maʼlumotlarigacha) — taxminiy",
                         "en": "based on provisional own funds and reserves (until accounting data arrive) — an "
                               "estimate"},
    "sc_ret_basis": {"ru": "Лимит на один риск по Положению № 1806, п. 15 = 20 % × (собственные средства {own} + "
                           "резервы {res}) = {limit}; по таблице линий класса {cls} — {line} (внутреннее экспертное "
                           "правило, не норма, не калибровано); расчётное удержание — меньшее: {ret} (оценочно)",
                     "uz": "1806-son Nizom, 15-band boʻyicha bitta xavf limiti = 20 % × (oʻz mablagʻlari {own} + "
                           "zaxiralar {res}) = {limit}; {cls}-klass liniyalar jadvali boʻyicha — {line} (ichki ekspert "
                           "qoidasi, meʼyor emas, kalibrlanmagan); hisoblangan ushlab qolish — kichigi: {ret} (taxminiy)",
                     "en": "Limit per risk under Regulation No. 1806, para. 15 = 20% × (own funds {own} + reserves "
                           "{res}) = {limit}; per the line table for class {cls} — {line} (internal expert rule, not a "
                           "regulation, not calibrated); estimated retention — the lower: {ret} (estimate)"},
    "sc_ret_excess": {"ru": "MFL выше удержания на {x} — эту часть нужно перестраховать (оценочно)",
                      "uz": "MFL ushlab qolishdan {x} ga yuqori — bu qismni qayta sugʻurtalash kerak (taxminiy)",
                      "en": "MFL exceeds the retention by {x} — this part needs reinsurance (estimate)"},
    "sc_ret_within": {"ru": "MFL укладывается в удержание", "uz": "MFL ushlab qolish doirasida",
                      "en": "MFL is within the retention"},
    "sc_na_class": {"ru": "Для этого класса сценарий убытка не считается: правила класса в модуле аналитики рисков нет",
                    "uz": "Bu klass uchun zarar ssenariysi hisoblanmaydi: xavf tahlili modulida klass qoidasi yoʻq",
                    "en": "No loss scenario is calculated for this class: the risk analytics module has no rule "
                          "for it"},
    "sc_na_error": {"ru": "Сценарии убытка не посчитаны: не хватило данных для модуля аналитики рисков",
                    "uz": "Zarar ssenariylari hisoblanmadi: xavf tahlili moduli uchun maʼlumot yetmadi",
                    "en": "Loss scenarios were not calculated: not enough data for the risk analytics module"},
    "sc_na": {"ru": "не считается", "uz": "hisoblanmaydi", "en": "not calculated"},
    # простые правила сценариев шаблона класса (app/class_templates.py, act_extras.simple_scenarios), 30.09.2026
    "sc_how_source_tpl": {"ru": "Сценарии посчитаны простым правилом шаблона класса {cls} (приложение А): {rule}. "
                                "Правило экспертное, не калибровано",
                          "uz": "Ssenariylar {cls}-klass shablonining oddiy qoidasi boʻyicha hisoblangan (A ilova): "
                                "{rule}. Qoida ekspert tomonidan belgilangan, kalibrlanmagan",
                          "en": "Scenarios were calculated by the simple rule of the class {cls} template (Annex A): "
                                "{rule}. The rule is an expert one, not calibrated"},
    "sc_tpl_rule_text": {"ru": "Правило приложения А: {text}", "uz": "A ilova qoidasi: {text}",
                         "en": "Annex A rule: {text}"},
    "tpl_expected": {"ru": "Ожидаемый годовой убыток (обращения × средний счёт × люди) — {amount}, справочно",
                     "uz": "Kutilayotgan yillik zarar (murojaatlar × oʻrtacha hisob × odamlar) — {amount}, maʼlumot uchun",
                     "en": "Expected annual loss (visits × average bill × people) — {amount}, for reference"},
    "tpl_credit_ok": {"ru": "Кредит {credit}, обеспечение {collateral}: допустимая страховая сумма {insurable} "
                            "(необеспеченная часть, не более 50 % кредита) — страховая сумма в пределах",
                      "uz": "Kredit {credit}, taʼminot {collateral}: ruxsat etilgan sugʻurta summasi {insurable} "
                            "(taʼminlanmagan qism, kreditning 50 % idan koʻp emas) — sugʻurta summasi doirasida",
                      "en": "Loan {credit}, collateral {collateral}: permitted sum insured {insurable} (the unsecured "
                            "part, not more than 50 % of the loan) — the sum insured is within it"},
    "tpl_credit_over": {"ru": "Кредит {credit}, обеспечение {collateral}: допустимая страховая сумма {insurable} "
                              "(необеспеченная часть, не более 50 % кредита), заявлено {sum} — превышение {excess}",
                        "uz": "Kredit {credit}, taʼminot {collateral}: ruxsat etilgan sugʻurta summasi {insurable} "
                              "(taʼminlanmagan qism, kreditning 50 % idan koʻp emas), eʼlon qilingan {sum} — oshiqcha {excess}",
                        "en": "Loan {credit}, collateral {collateral}: permitted sum insured {insurable} (the unsecured "
                              "part, not more than 50 % of the loan), declared {sum} — excess {excess}"},
    "c_tpl_credit_over": {"ru": "Кредит: страховая сумма {sum} выше допустимой: при кредите {credit} и обеспечении "
                                "{collateral} страхуется только необеспеченная часть и не более {share} % кредита "
                                "(правило компании, требования НАПП) — уменьшить до {insurable} (превышение {excess})",
                          "uz": "Kredit: sugʻurta summasi {sum} ruxsat etilganidan yuqori: kredit {credit} va taʼminot "
                                "{collateral} boʻlganda faqat taʼminlanmagan qism va kreditning {share} % idan koʻp "
                                "boʻlmagan qismi sugʻurtalanadi (kompaniya qoidasi, SNAP talablari) — {insurable} gacha "
                                "kamaytirish (oshiqcha {excess})",
                          "en": "Credit: the sum insured {sum} is above the permitted amount: with a loan of {credit} "
                                "and collateral of {collateral} only the unsecured part and not more than {share} % of "
                                "the loan may be insured (company rule, regulator's requirements) — reduce to "
                                "{insurable} (excess {excess})"},
    "as_tpl_per_person": {"ru": "сумма на человека не указана — взята страховая сумма, делённая на число застрахованных",
                          "uz": "bir kishiga summa koʻrsatilmagan — sugʻurta summasi sugʻurtalanganlar soniga boʻlingan",
                          "en": "the sum per person is not stated — the sum insured divided by the number of insured is used"},
    "as_tpl_per_person_sum": {"ru": "ни сумма на человека, ни число застрахованных не указаны — взята вся страховая сумма",
                              "uz": "bir kishiga summa ham, sugʻurtalanganlar soni ham koʻrsatilmagan — butun sugʻurta "
                                    "summasi olindi",
                              "en": "neither the sum per person nor the number of insured is stated — the whole sum "
                                    "insured is used"},
    "as_tpl_place": {"ru": "число застрахованных в одном месте не указано — для катастрофы взяты все застрахованные",
                     "uz": "bir joydagi sugʻurtalanganlar soni koʻrsatilmagan — falokat uchun barcha sugʻurtalanganlar olindi",
                     "en": "the number of insured in one place is not stated — all insured are taken for the catastrophe"},
    "as_tpl_limit_person": {"ru": "лимит на человека не указан — взята страховая сумма, делённая на число застрахованных "
                                  "(или вся сумма)",
                            "uz": "bir kishiga limit koʻrsatilmagan — sugʻurta summasi sugʻurtalanganlar soniga boʻlingan "
                                  "(yoki butun summa)",
                            "en": "the limit per person is not stated — the sum insured divided by the number of insured "
                                  "(or the whole sum) is used"},
    "as_tpl_epidemic": {"ru": "эпидемия — {pct} % страховой суммы: экспертная доля шаблона, не калибровано",
                        "uz": "epidemiya — sugʻurta summasining {pct} %: shablonning ekspert ulushi, kalibrlanmagan",
                        "en": "epidemic — {pct} % of the sum insured: an expert template share, not calibrated"},
    "as_tpl_units": {"ru": "число единиц не указано — одна единица принята равной всей страховой сумме",
                     "uz": "birliklar soni koʻrsatilmagan — bitta birlik butun sugʻurta summasiga teng deb olindi",
                     "en": "the number of units is not stated — one unit is taken as the whole sum insured"},
    "as_tpl_shipment": {"ru": "сумма одной отправки не указана — взята страховая сумма",
                        "uz": "bitta joʻnatma summasi koʻrsatilmagan — sugʻurta summasi olindi",
                        "en": "the value of one shipment is not stated — the sum insured is used"},
    "as_tpl_accumulation": {"ru": "накопление на одном складе или транспорте не указано — взята страховая сумма",
                            "uz": "bitta ombor yoki transportdagi jamlanma koʻrsatilmagan — sugʻurta summasi olindi",
                            "en": "the accumulation in one store or vehicle is not stated — the sum insured is used"},
    "as_tpl_limit_case": {"ru": "лимит на один случай не указан — взята страховая сумма",
                          "uz": "bitta hodisaga limit koʻrsatilmagan — sugʻurta summasi olindi",
                          "en": "the per-occurrence limit is not stated — the sum insured is used"},
    "as_tpl_limit_aggregate": {"ru": "годовой лимит не указан — взята страховая сумма",
                               "uz": "yillik limit koʻrsatilmagan — sugʻurta summasi olindi",
                               "en": "the aggregate limit is not stated — the sum insured is used"},
    "as_tpl_credit_unknown": {"ru": "сумма кредита и обеспечение не указаны — проверка «не более необеспеченной части и "
                                    "50 % кредита» не выполнена",
                              "uz": "kredit summasi va taʼminot koʻrsatilmagan — «taʼminlanmagan qism va kreditning 50 % "
                                    "idan koʻp emas» tekshiruvi bajarilmadi",
                              "en": "the loan amount and collateral are not stated — the check «not above the unsecured "
                                    "part and 50 % of the loan» was not done"},
    "as_tpl_bi": {"ru": "ежемесячные потери или срок восстановления не указаны — взята страховая сумма",
                  "uz": "oylik yoʻqotishlar yoki tiklanish muddati koʻrsatilmagan — sugʻurta summasi olindi",
                  "en": "the monthly loss or recovery period is not stated — the sum insured is used"},
    "as_tpl_dispute": {"ru": "лимит на один спор не указан — взята страховая сумма",
                       "uz": "bitta nizoga limit koʻrsatilmagan — sugʻurta summasi olindi",
                       "en": "the limit per dispute is not stated — the sum insured is used"},
    # урожай сельхозкультур (вариант 16у, правило crop), 01.10.2026
    "tpl_crop_ok": {"ru": "Стоимость урожая: {area} га × {yield} ц/га × {price} за центнер = {value}; страховая сумма "
                          "{sum} равна стоимости",
                    "uz": "Hosil qiymati: {area} ga × {yield} s/ga × sentner uchun {price} = {value}; sugʻurta summasi "
                          "{sum} qiymatga teng",
                    "en": "Crop value: {area} ha × {yield} c/ha × {price} per centner = {value}; the sum insured {sum} "
                          "equals the value"},
    "tpl_crop_under": {"ru": "Стоимость урожая: {area} га × {yield} ц/га × {price} за центнер = {value}; страховая "
                             "сумма {sum} ниже стоимости ({pct} %) — выплата пропорционально (ГК РУз, ст. 936)",
                       "uz": "Hosil qiymati: {area} ga × {yield} s/ga × sentner uchun {price} = {value}; sugʻurta "
                             "summasi {sum} qiymatdan past ({pct} %) — toʻlov mutanosib (FK, 936-modda)",
                       "en": "Crop value: {area} ha × {yield} c/ha × {price} per centner = {value}; the sum insured "
                             "{sum} is below the value ({pct} %) — proportional payment (Civil Code, art. 936)"},
    "tpl_crop_over": {"ru": "Стоимость урожая: {area} га × {yield} ц/га × {price} за центнер = {value}; страховая "
                            "сумма {sum} выше стоимости на {excess} — недопустимо (ГК РУз, ст. 938)",
                      "uz": "Hosil qiymati: {area} ga × {yield} s/ga × sentner uchun {price} = {value}; sugʻurta "
                            "summasi {sum} qiymatdan {excess} ga yuqori — yoʻl qoʻyilmaydi (FK, 938-modda)",
                      "en": "Crop value: {area} ha × {yield} c/ha × {price} per centner = {value}; the sum insured "
                            "{sum} exceeds the value by {excess} — not allowed (Civil Code, art. 938)"},
    "c_tpl_crop_over": {"ru": "Урожай: страховая сумма {sum} выше страховой стоимости урожая {value} (площадь × "
                              "средняя урожайность за 5 лет × цена) — уменьшить до {value} (ГК РУз, ст. 938)",
                        "uz": "Hosil: sugʻurta summasi {sum} hosilning sugʻurta qiymati {value} dan yuqori (maydon × "
                              "5 yillik oʻrtacha hosildorlik × narx) — {value} gacha kamaytirish (FK, 938-modda)",
                        "en": "Crop: the sum insured {sum} exceeds the insured value of the crop {value} (area × "
                              "5-year average yield × price) — reduce to {value} (Civil Code, art. 938)"},
    "as_tpl_crop_value": {"ru": "площадь, средняя урожайность за 5 лет или цена не указаны — стоимость урожая не "
                                "рассчитана, взята стоимость объекта",
                          "uz": "maydon, 5 yillik oʻrtacha hosildorlik yoki narx koʻrsatilmagan — hosil qiymati "
                                "hisoblanmadi, obyekt qiymati olindi",
                          "en": "the area, 5-year average yield or price is not stated — the crop value was not "
                                "calculated, the object value is used"},
    "as_tpl_crop_shares": {"ru": "PML — потеря {pl} % урожая на {pa} % площади, EML — гибель {el} % урожая на {ea} % "
                                 "площади, MFL — полная гибель на всей площади: доли экспертные (оценка разработчика, "
                                 "не утверждено страховщиком)",
                           "uz": "PML — maydonning {pa} % ida hosilning {pl} % i yoʻqotilishi, EML — maydonning {ea} % "
                                 "ida hosilning {el} % i nobud boʻlishi, MFL — butun maydonda toʻliq nobud boʻlish: "
                                 "ulushlar ekspert bahosi (ishlab chiquvchi bahosi, sugʻurtalovchi tasdiqlamagan)",
                           "en": "PML — loss of {pl} % of the crop on {pa} % of the area, EML — loss of {el} % of the "
                                 "crop on {ea} % of the area, MFL — total loss on the whole area: expert shares "
                                 "(developer's estimate, not approved by the insurer)"},
    # класс без продуктов страховщика (16у, 18), 01.10.2026
    "how_no_products": {"ru": "У страховщика нет продуктов класса {cls} — тарифной политики по классу нет, ставка не "
                              "определена, акт сформирован по шаблону класса",
                        "uz": "Sugʻurtalovchida {cls}-klass mahsulotlari yoʻq — klass boʻyicha tarif siyosati yoʻq, "
                              "tarif aniqlanmagan, dalolatnoma klass shabloni boʻyicha tuzildi",
                        "en": "The insurer has no products in class {cls} — there is no tariff policy for the class, "
                              "the rate is not determined, the report follows the class template"},
    "an_r_template": {"ru": "В справочнике рисков класс {cls} не разбит на риски: доли — экспертные доли шаблона класса "
                            "(приложение А), сумма {total}, не калибровано. {note}",
                      "uz": "Xavflar maʼlumotnomasida {cls}-klass xavflarga boʻlinmagan: ulushlar — klass shablonining "
                            "ekspert ulushlari (A ilova), jami {total}, kalibrlanmagan. {note}",
                      "en": "The risk reference does not split class {cls} into risks: the shares are the class "
                            "template's expert shares (Annex A), total {total}, not calibrated. {note}"},
    "an_sc_tpl": {"ru": "{what}: {amount}", "uz": "{what}: {amount}", "en": "{what}: {amount}"},
    "an_r_tpl_label": {"ru": "экспертные доли шаблона", "uz": "shablonning ekspert ulushlari",
                       "en": "expert template shares"},
    "sc_how_title": {"ru": "Как посчитаны сценарии убытка", "uz": "Zarar ssenariylari qanday hisoblangan",
                     "en": "How the loss scenarios were calculated"},
    "sc_how_source": {"ru": "Сценарии посчитаны модулем аналитики рисков по правилу класса {cls}: {rule}. Доли "
                            "экспертные, не калибровано",
                      "uz": "Ssenariylar xavf tahlili moduli tomonidan {cls}-klass qoidasi boʻyicha hisoblangan: "
                            "{rule}. Ulushlar ekspert tomonidan belgilangan, kalibrlanmagan",
                      "en": "Scenarios were calculated by the risk analytics module under the class {cls} rule: "
                            "{rule}. Shares are expert values, not calibrated"},
    "sc_rule_vehicle": {"ru": "одна единица техники — доля от меньшей из величин «страховая сумма» и «стоимость»",
                        "uz": "bitta texnika birligi — «sugʻurta summasi» va «qiymat»ning kichigidan ulush",
                        "en": "a single unit — a share of the lower of the sum insured and the value"},
    "sc_rule_property8": {"ru": "пожар по наибольшему отсеку или землетрясение по всей площадке — берётся большее",
                          "uz": "eng katta boʻlimdagi yongʻin yoki butun maydondagi zilzila — kattasi olinadi",
                          "en": "fire in the largest compartment or earthquake across the site — the larger is taken"},
    "sc_rule_property9": {"ru": "кража, залив и прочее в наибольшем помещении с поправкой на защиту",
                          "uz": "eng katta xonada oʻgʻirlik, suv bosishi va boshqalar, himoya tuzatmasi bilan",
                          "en": "theft, water and other damage in the largest premises, adjusted for protection"},
    "sc_how_k": {"ru": "Страховая сумма ниже стоимости — убыток уменьшен в доле {k}",
                 "uz": "Sugʻurta summasi qiymatdan past — zarar {k} ulushida kamaytirilgan",
                 "en": "The sum insured is below the value — the loss is reduced in the share {k}"},
    "sc_assumptions_title": {"ru": "Принято по умолчанию (уточните)", "uz": "Standart boʻyicha qabul qilindi (aniqlang)",
                             "en": "Assumed by default (please clarify)"},
    "as_object_type": {"ru": "тип объекта «{value}» — принято по умолчанию",
                       "uz": "obyekt turi «{value}» — standart boʻyicha qabul qilindi",
                       "en": "object type «{value}» — assumed by default"},
    "as_construction": {"ru": "конструкция: {value} — принято по умолчанию",
                        "uz": "konstruksiya: {value} — standart boʻyicha qabul qilindi",
                        "en": "construction: {value} — assumed by default"},
    "as_activity": {"ru": "деятельность на объекте: {value} — принято по умолчанию",
                    "uz": "obyektdagi faoliyat: {value} — standart boʻyicha qabul qilindi",
                    "en": "activity on site: {value} — assumed by default"},
    "as_vehicle_type": {"ru": "тип транспорта: {value} — принято по умолчанию",
                        "uz": "transport turi: {value} — standart boʻyicha qabul qilindi",
                        "en": "vehicle type: {value} — assumed by default"},
    "as_year": {"ru": "год выпуска неизвестен — возраст взят по худшему варианту «старше 15 лет» — принято по "
                      "умолчанию",
                "uz": "ishlab chiqarilgan yili nomaʼlum — yosh eng yomon variant «15 yildan katta» boʻyicha olindi — "
                      "standart boʻyicha qabul qilindi",
                "en": "year unknown — age taken as the worst option «over 15 years» — assumed by default"},
    "as_term_months": {"ru": "срок не указан — годовой договор — принято по умолчанию",
                       "uz": "muddat koʻrsatilmagan — bir yillik shartnoma — standart boʻyicha qabul qilindi",
                       "en": "term not stated — a one-year contract — assumed by default"},
    "as_term_long": {"ru": "срок {days} дн. длиннее 60 месяцев — для сценариев и франшизы взят годовой — принято по "
                           "умолчанию",
                     "uz": "{days} kunlik muddat 60 oydan uzun — ssenariy va franshiza uchun bir yillik olindi — "
                           "standart boʻyicha qabul qilindi",
                     "en": "the {days}-day term is longer than 60 months — one year used for scenarios and the "
                           "deductible — assumed by default"},
    "as_protection_veh": {"ru": "противоугонная система не указана — PML взят как без иммобилайзера и поиска — "
                                "принято по умолчанию",
                          "uz": "oʻgʻirlikka qarshi tizim koʻrsatilmagan — PML immobilayzer va qidiruvsiz deb olindi — "
                                "standart boʻyicha qabul qilindi",
                          "en": "anti-theft system not stated — PML taken as without immobiliser or tracking — "
                                "assumed by default"},
    "as_protection_prop": {"ru": "защита не указана — взят худший вариант «нет сигнализации и охраны» — принято по "
                                 "умолчанию",
                           "uz": "himoya koʻrsatilmagan — eng yomon variant «signalizatsiya va qoʻriqlash yoʻq» olindi "
                                 "— standart boʻyicha qabul qilindi",
                           "en": "protection not stated — the worst option «no alarm or security» taken — assumed by "
                                 "default"},
    "as_compartments": {"ru": "противопожарные отсеки не указаны — весь объект считается одним отсеком — принято по "
                              "умолчанию",
                        "uz": "yongʻinga qarshi boʻlimlar koʻrsatilmagan — butun obyekt bitta boʻlim deb hisoblandi — "
                              "standart boʻyicha qabul qilindi",
                        "en": "fire compartments not stated — the whole object is one compartment — assumed by "
                              "default"},
    "as_seismic": {"ru": "сейсмозона не указана — MFL принят как полное уничтожение — принято по умолчанию",
                   "uz": "seysmik zona koʻrsatilmagan — MFL toʻliq vayron boʻlish deb qabul qilindi — standart "
                         "boʻyicha qabul qilindi",
                   "en": "seismic zone not stated — MFL taken as total destruction — assumed by default"},

    # ---------- франшиза: применение ----------
    "fr_proposed_tail": {"ru": " Предлагается {type} франшиза {pct} ({amount}); премия с ней — {after} вместо {before}.",
                         "uz": " {type} franshiza {pct} ({amount}) taklif etiladi; u bilan mukofot — {before} oʻrniga "
                               "{after}.",
                         "en": " Proposed: {type} deductible of {pct} ({amount}); the premium with it is {after} "
                               "instead of {before}."},
    "fr_proposed_tail_na": {"ru": " Предлагается {type} франшиза {pct} ({amount}); эффект на премию не посчитан.",
                            "uz": " {type} franshiza {pct} ({amount}) taklif etiladi; mukofotga taʼsiri hisoblanmadi.",
                            "en": " Proposed: {type} deductible of {pct} ({amount}); the premium effect was not "
                                  "calculated."},
    "fr_applied": {"ru": "Франшиза применена по решению сотрудника: {type}, {pct} страховой суммы ({amount}) с каждого "
                         "убытка; премия пересчитана: {before} → {after}.",
                   "uz": "Franshiza xodim qarori bilan qoʻllanildi: {type}, har bir zarardan sugʻurta summasining {pct} "
                         "({amount}); mukofot qayta hisoblandi: {before} → {after}.",
                   "en": "Deductible applied by staff decision: {type}, {pct} of the sum insured ({amount}) per loss; "
                         "the premium was recalculated: {before} → {after}."},
    "fr_applied_na": {"ru": "Франшиза применена по решению сотрудника: {type}, {pct} страховой суммы ({amount}) с "
                            "каждого убытка; премия не пересчитана — эффект посчитать нельзя.",
                      "uz": "Franshiza xodim qarori bilan qoʻllanildi: {type}, sugʻurta summasining {pct} ({amount}); "
                            "mukofot qayta hisoblanmadi — taʼsirni hisoblab boʻlmaydi.",
                      "en": "Deductible applied by staff decision: {type}, {pct} of the sum insured ({amount}) per "
                            "loss; the premium was not recalculated — the effect cannot be calculated."},
    "fr_floor_note": {"ru": "Ставка упёрлась в минимальную ставку продукта — премия ниже не опускается.",
                      "uz": "Tarif mahsulotning eng kam tarifiga yetdi — mukofot bundan pastga tushmaydi.",
                      "en": "The rate has hit the product minimum — the premium does not go lower."},
    "fr_extrapolated_note": {"ru": "Франшиза выше 2 %: множитель — экспертное продолжение кривой коэффициентов, не "
                                   "калибровано.",
                             "uz": "Franshiza 2 % dan yuqori: koeffitsiyent — koeffitsiyentlar egri chizigʻining ekspert "
                                   "davomi, kalibrlanmagan.",
                             "en": "The deductible is above 2%: the multiplier is an expert extension of the coefficient "
                                   "curve, not calibrated."},
    "frh_size_one": {"ru": "Основание есть: вилка уровня риска — ровно {to} (пороги franchise_by_level); размер {pct} "
                           "(экспертно)",
                     "uz": "Asos bor: xavf darajasi oraligʻi — aynan {to} (franchise_by_level chegaralari); {pct} "
                           "miqdor (ekspert)",
                     "en": "Grounds exist: the risk-level band is exactly {to} (franchise_by_level thresholds); size "
                           "{pct} (expert)"},
    "fr_w_statutory": {"ru": "Франшиза сотрудника не применена: по обязательным видам франшиза не применяется.",
                       "uz": "Xodim franshizasi qoʻllanilmadi: majburiy turlarda franshiza qoʻllanilmaydi.",
                       "en": "The staff deductible was not applied: deductibles do not apply to compulsory classes."},
    "fr_w_cap": {"ru": "Франшиза {pct} выше потолка класса {cap} — нужно решение андеррайтера.",
                 "uz": "{pct} franshiza klass chegarasi {cap} dan yuqori — anderrayter qarori kerak.",
                 "en": "The {pct} deductible is above the class cap of {cap} — an underwriter decision is required."},
    "fr_how_title": {"ru": "Франшиза: как посчитано", "uz": "Franshiza: qanday hisoblangan",
                     "en": "Deductible: how it was calculated"},
    "fr_alt_title": {"ru": "Вместо франшизы можно", "uz": "Franshiza oʻrniga mumkin", "en": "Instead of a deductible"},
    "rate_with_fr": {"ru": "Ставка с учётом франшизы", "uz": "Franshiza hisobga olingan tarif",
                     "en": "Rate with the deductible"},
    "premium_no_fr": {"ru": "без франшизы: {before}", "uz": "franshizasiz: {before}", "en": "without deductible: {before}"},
    "frh_none": {"ru": "Оснований нет — франшиза не требуется, премия без изменений: {premium}",
                 "uz": "Asoslar yoʻq — franshiza talab qilinmaydi, mukofot oʻzgarishsiz: {premium}",
                 "en": "No grounds — no deductible required, the premium is unchanged: {premium}"},
    "frh_statutory": {"ru": "Обязательный вид: франшиза не применяется, премия — по нормативному акту",
                      "uz": "Majburiy tur: franshiza qoʻllanilmaydi, mukofot — meʼyoriy hujjat boʻyicha",
                      "en": "Compulsory class: no deductible, the premium follows the statutory act"},
    "frh_employee": {"ru": "Размер задан сотрудником: {pct} страховой суммы = {amount}, тип — {type}",
                     "uz": "Miqdorni xodim belgilagan: sugʻurta summasining {pct} = {amount}, turi — {type}",
                     "en": "Size set by staff: {pct} of the sum insured = {amount}, type — {type}"},
    "frh_size": {"ru": "Основание есть: вилка уровня риска {from_}–{to} (пороги franchise_by_level); размер {pct} "
                       "выбран модулем франшизы по истории убытков (экспертно)",
                 "uz": "Asos bor: xavf darajasi oraligʻi {from_}–{to} (franchise_by_level chegaralari); {pct} miqdor "
                       "franshiza moduli tomonidan zararlar tarixi boʻyicha tanlangan (ekspert)",
                 "en": "Grounds exist: the risk-level band is {from_}–{to} (franchise_by_level thresholds); the size "
                       "{pct} was chosen by the deductible module from the loss history (expert)"},
    "frh_peril_size": {"ru": "Франшиза по отдельному риску — верх вилки уровня: {pct}",
                       "uz": "Alohida xavf boʻyicha franshiza — daraja oraligʻining yuqori chegarasi: {pct}",
                       "en": "Per-peril deductible — the top of the level band: {pct}"},
    "frh_nosize": {"ru": "Вилка для уровня пустая — размер определяет андеррайтер",
                   "uz": "Daraja oraligʻi boʻsh — miqdorni anderrayter belgilaydi",
                   "en": "The level band is empty — the underwriter sets the size"},
    "frh_peril_na": {"ru": "Эффект франшизы по отдельному риску на премию не считается: доля риска в ставке акта "
                           "неизвестна",
                     "uz": "Alohida xavf franshizasining mukofotga taʼsiri hisoblanmaydi: xavfning dalolatnoma "
                           "tarifidagi ulushi nomaʼlum",
                     "en": "The premium effect of a per-peril deductible is not calculated: the peril's share of the "
                           "report rate is unknown"},
    "frh_effect_na": {"ru": "Эффект на премию не посчитан: модуль франшизы не дал расчёта",
                      "uz": "Mukofotga taʼsir hisoblanmadi: franshiza moduli hisob bermadi",
                      "en": "The premium effect was not calculated: the deductible module returned no result"},
    "frh_mult": {"ru": "Эффект — из модуля франшизы: премия модуля расчёта ставок без франшизы {base}, с франшизой {pct} — "
                       "{with_}; множитель {mult}",
                 "uz": "Taʼsir — franshiza modulidan: tarif hisoblash moduli mukofoti franshizasiz {base}, {pct} franshiza "
                       "bilan — {with_}; koeffitsiyent {mult}",
                 "en": "The effect comes from the deductible module: rating module premium without deductible "
                       "{base}, with a {pct} deductible — {with_}; multiplier {mult}"},
    "frh_extrapolated": {"ru": "Выше 2 % множитель — экспертное продолжение кривой коэффициентов (не калибровано)",
                         "uz": "2 % dan yuqorida koeffitsiyent — koeffitsiyentlar egri chizigʻining ekspert davomi "
                               "(kalibrlanmagan)",
                         "en": "Above 2% the multiplier is an expert extension of the coefficient curve (not "
                               "calibrated)"},
    "frh_conditional": {"ru": "Для условной франшизы отдельного коэффициента нет — множитель взят как для безусловной "
                              "того же размера (оценка сверху, экспертно)",
                        "uz": "Shartli franshiza uchun alohida koeffitsiyent yoʻq — koeffitsiyent xuddi shu miqdordagi "
                              "shartsiz franshiza kabi olindi (yuqori baho, ekspert)",
                        "en": "There is no separate coefficient for a conditional deductible — the multiplier is taken "
                              "as for an unconditional one of the same size (upper estimate, expert)"},
    "frh_apply": {"ru": "Ставка акта (из тарифной политики) {rate} × {mult} = {raw}, применяется {rate_after}; премия {premium} (от неокруглённой ставки). "
                        "Премия модуля расчёта ставок в акт не переносится — берётся только множитель",
                  "uz": "Dalolatnoma tarifi (tarif siyosatidan) {rate} × {mult} = {raw}, qoʻllaniladi {rate_after}; mukofot {premium} (yaxlitlanmagan "
                        "tarifdan). Tarif hisoblash moduli mukofoti dalolatnomaga koʻchirilmaydi — faqat koeffitsiyent olinadi",
                  "en": "Report rate (from the tariff policy) {rate} × {mult} = {raw}, applied {rate_after}; premium {premium} (from the unrounded "
                        "rate). The rating module's premium is not carried over — only the multiplier is used"},
    "frh_floor": {"ru": "Ставка не опускается ниже минимальной ставки продукта {min}",
                  "uz": "Tarif mahsulotning eng kam tarifi {min} dan pastga tushmaydi",
                  "en": "The rate does not go below the product minimum {min}"},
    "frh_not_applied": {"ru": "Франшиза предложена, но в премию акта не включена: решение за андеррайтером",
                        "uz": "Franshiza taklif etildi, lekin dalolatnoma mukofotiga kiritilmadi: qaror anderrayterda",
                        "en": "The deductible is proposed but not included in the report premium: the underwriter "
                              "decides"},
    "frh_no_premium": {"ru": "Ставка продукта не определена — премию с франшизой посчитать нельзя",
                       "uz": "Mahsulot tarifi aniqlanmagan — franshizali mukofotni hisoblab boʻlmaydi",
                       "en": "The product rate is not determined — the premium with the deductible cannot be "
                             "calculated"},
    # варианты «вместо франшизы»: премия и разница — к премии акта без франшизы (одна база для всех)
    "alt_measures": {"ru": "Вместо франшизы — выполнить рекомендации страхователю: премия {premium} ({delta} к премии "
                           "без франшизы)",
                     "uz": "Franshiza oʻrniga — sugʻurtalanuvchiga tavsiyalarni bajarish: mukofot {premium} "
                           "(franshizasiz mukofotga nisbatan {delta})",
                     "en": "Instead of a deductible — carry out the recommendations to the policyholder: premium "
                           "{premium} ({delta} against the premium without a deductible)"},
    "alt_measures_free": {"ru": "Вместо франшизы — выполнить рекомендации страхователю: на ставку не влияет, снижает "
                                "вероятность убытка",
                          "uz": "Franshiza oʻrniga — sugʻurtalanuvchiga tavsiyalarni bajarish: tarifga taʼsir "
                                "qilmaydi, zarar ehtimolini kamaytiradi",
                          "en": "Instead of a deductible — carry out the recommendations to the policyholder: no "
                                "effect on the rate, reduces the likelihood of a loss"},
    "alt_exclude_eq": {"ru": "Вместо франшизы — исключить землетрясение из покрытия: премия {premium} ({delta} к премии "
                             "без франшизы); убыток от него клиент несёт полностью",
                       "uz": "Franshiza oʻrniga — zilzilani qoplamadan chiqarish: mukofot {premium} (franshizasiz "
                             "mukofotga nisbatan {delta}); undan zararni mijoz toʻliq oʻzi koʻtaradi",
                       "en": "Instead of a deductible — exclude earthquake from cover: premium {premium} ({delta} "
                             "against the premium without a deductible); the client bears that loss in full"},
    "alt_exclude": {"ru": "Вместо франшизы — исключить отдельный риск из покрытия: премия {premium} ({delta} к премии "
                          "без франшизы)",
                    "uz": "Franshiza oʻrniga — alohida xavfni qoplamadan chiqarish: mukofot {premium} (franshizasiz "
                          "mukofotga nisbatan {delta})",
                    "en": "Instead of a deductible — exclude a single peril from cover: premium {premium} ({delta} "
                          "against the premium without a deductible)"},
    "alt_sum_up": {"ru": "Вместо франшизы — поднять страховую сумму до стоимости {value}: выплата станет полной (ГК РУз, "
                         "ст. 936); премия {premium} ({delta} к премии без франшизы)",
                   "uz": "Franshiza oʻrniga — sugʻurta summasini qiymatgacha {value} oshirish: toʻlov toʻliq boʻladi "
                         "(OʻzR FK, 936-modda); mukofot {premium} (franshizasiz mukofotga nisbatan {delta})",
                   "en": "Instead of a deductible — raise the sum insured to the value {value}: claims will be paid in "
                         "full (Civil Code, Art. 936); premium {premium} ({delta} against the premium without a "
                         "deductible)"},
    "alt_sum_down": {"ru": "Вместо франшизы — снизить страховую сумму до стоимости {value} (ГК РУз, ст. 938): премия "
                           "{premium} ({delta} к премии без франшизы)",
                     "uz": "Franshiza oʻrniga — sugʻurta summasini qiymatgacha {value} kamaytirish (OʻzR FK, "
                           "938-modda): mukofot {premium} (franshizasiz mukofotga nisbatan {delta})",
                     "en": "Instead of a deductible — reduce the sum insured to the value {value} (Civil Code, "
                           "Art. 938): premium {premium} ({delta} against the premium without a deductible)"},
    "c_fr_applied": {"ru": "Подтвердить франшизу, применённую сотрудником",
                     "uz": "Xodim qoʻllagan franshizani tasdiqlash",
                     "en": "Confirm the deductible applied by staff"},

    # ---------- рекомендации страхователю ----------
    "ms_title": {"ru": "Рекомендации страхователю", "uz": "Sugʻurtalanuvchiga tavsiyalar",
                 "en": "Recommendations to the policyholder"},
    "ms_item": {"ru": "{text}. Зачем: {why} Срок: {deadline}.{mandatory} {effect}",
                "uz": "{text}. Nima uchun: {why} Muddat: {deadline}.{mandatory} {effect}",
                "en": "{text}. Why: {why} Deadline: {deadline}.{mandatory} {effect}"},
    "ms_deadline": {"ru": "{n} дн.", "uz": "{n} kun", "en": "{n} days"},
    "ms_deadline_default": {"ru": "{n} дн. (рекомендуемый)", "uz": "{n} kun (tavsiya etilgan)",
                            "en": "{n} days (recommended)"},
    "ms_mandatory": {"ru": " Обязательно — условие договора.", "uz": " Majburiy — shartnoma sharti.",
                     "en": " Mandatory — a contract condition."},
    "ms_effect": {"ru": "Влияние на премию: {effect} ({delta}).", "uz": "Mukofotga taʼsiri: {effect} ({delta}).",
                  "en": "Effect on the premium: {effect} ({delta})."},
    "ms_effect_pct": {"ru": "Влияние на ставку: {effect}.", "uz": "Tarifga taʼsiri: {effect}.",
                      "en": "Effect on the rate: {effect}."},
    "ms_effect_zero": {"ru": "Ставка уже на минимуме продукта — премия не снижается, но снижается вероятность убытка.",
                       "uz": "Tarif allaqachon mahsulot minimumida — mukofot kamaymaydi, lekin zarar ehtimoli kamayadi.",
                       "en": "The rate is already at the product minimum — the premium does not fall, but the "
                             "likelihood of a loss does."},
    "ms_effect_na": {"ru": "На ставку не влияет, снижает вероятность убытка.",
                     "uz": "Tarifga taʼsir qilmaydi, zarar ehtimolini kamaytiradi.",
                     "en": "No effect on the rate; reduces the likelihood of a loss."},
    "ms_effect_statutory": {"ru": "На ставку не влияет: обязательный вид, тариф по нормативному акту.",
                            "uz": "Tarifga taʼsir qilmaydi: majburiy tur, tarif meʼyoriy hujjat boʻyicha.",
                            "en": "No effect on the rate: compulsory class, the tariff follows the statutory act."},
    "ms_total": {"ru": "Если выполнить все мероприятия со скидкой, премия составит {after} вместо {before}: скидки "
                       "перемножаются, ставка не ниже минимальной ставки продукта. Эффекты экспертные, не калибровано.",
                 "uz": "Chegirmali barcha tadbirlar bajarilsa, mukofot {before} oʻrniga {after} boʻladi: chegirmalar "
                       "koʻpaytiriladi, tarif mahsulotning eng kam tarifidan past emas. Taʼsirlar ekspert, "
                       "kalibrlanmagan.",
                 "en": "If all measures with a discount are carried out, the premium will be {after} instead of "
                       "{before}: discounts are multiplied, the rate is not below the product minimum. Effects are "
                       "expert values, not calibrated."},
    "ms_total_floor": {"ru": "Скидки упираются в минимальную ставку продукта.",
                       "uz": "Chegirmalar mahsulotning eng kam tarifiga yetib qoladi.",
                       "en": "The discounts hit the product minimum rate."},
    "ms_none": {"ru": "Для этого объекта готовых рекомендаций нет.", "uz": "Bu obyekt uchun tayyor tavsiyalar yoʻq.",
                "en": "There are no ready-made recommendations for this object."},

    # ---------- документы ----------
    "doc_parsed_note": {"ru": "из документа (разбор текста), проверьте", "uz": "hujjatdan (matn tahlili), tekshiring",
                        "en": "from the document (text parsing), please check"},
    "ph_docs_ok": {"ru": "Разобрано документов с текстом: {n}, найдено значений: {k}. Проверьте каждое.",
                   "uz": "Matnli hujjatlar tahlil qilindi: {n}, topilgan qiymatlar: {k}. Har birini tekshiring.",
                   "en": "Text documents parsed: {n}, values found: {k}. Check each one."},
    "docs_parsed": {"ru": "Разобрано документов (по тексту)", "uz": "Tahlil qilingan hujjatlar (matn boʻyicha)",
                    "en": "Documents parsed (text)"},
    "doc_term_conflict": {"ru": "В документе указаны разные сроки — срок не подставлен, введите вручную",
                          "uz": "Hujjatda turli muddatlar koʻrsatilgan — muddat qoʻyilmadi, qoʻlda kiriting",
                          "en": "The document states different terms — the term was not filled in, enter it manually"},
    "doc_currency": {"ru": "Сумма в документе не в сумах — пересчитайте и введите вручную",
                     "uz": "Hujjatdagi summa soʻmda emas — qayta hisoblang va qoʻlda kiriting",
                     "en": "The amount in the document is not in UZS — convert it and enter manually"},
    "doc_no_values": {"ru": "В документе не найдено значений для акта — введите данные вручную",
                      "uz": "Hujjatda dalolatnoma uchun qiymat topilmadi — maʼlumotlarni qoʻlda kiriting",
                      "en": "No values for the report were found in the document — enter the data manually"},
    "doc_unreadable": {"ru": "Документ прочитать не удалось", "uz": "Hujjatni oʻqib boʻlmadi",
                       "en": "The document could not be read"},
    "doc_partial": {"ru": "Прочитана только часть документа: лишние строки, колонки, листы и слишком длинный текст "
                          "не разбирались — остальное проверьте вручную",
                    "uz": "Hujjatning faqat bir qismi oʻqildi: ortiqcha qatorlar, ustunlar, varaqlar va juda uzun "
                          "matn tahlil qilinmadi — qolganini qoʻlda tekshiring",
                    "en": "Only part of the document was read: extra rows, columns, sheets and overly long text were "
                          "not parsed — check the rest manually"},
    "doc_timeout": {"ru": "Документ не разобран: слишком большой — разбор не уложился в отведённое время; введите "
                          "данные вручную",
                    "uz": "Hujjat tahlil qilinmadi: juda katta — tahlil ajratilgan vaqtga sigʻmadi; maʼlumotlarni "
                          "qoʻlda kiriting",
                    "en": "The document was not parsed: too large — parsing did not finish in the allotted time; "
                          "enter the data manually"},
    "doc_busy": {"ru": "Документ не разобран: сервер занят разбором других документов — загрузите его ещё раз "
                       "через минуту",
                 "uz": "Hujjat tahlil qilinmadi: server boshqa hujjatlarni tahlil qilmoqda — bir daqiqadan soʻng "
                       "qayta yuklang",
                 "en": "The document was not parsed: the server is busy parsing other documents — upload it again "
                       "in a minute"},
    "doc_macros": {"ru": "В файле есть макросы — они не исполняются, прочитан только текст",
                   "uz": "Faylda makroslar bor — ular bajarilmaydi, faqat matn oʻqildi",
                   "en": "The file contains macros — they are not run, only the text was read"},
    "doc_bomb": {"ru": "архив документа после распаковки слишком большой — файл отклонён",
                 "uz": "hujjat arxivi ochilgandan keyin juda katta — fayl rad etildi",
                 "en": "the document archive is too large when unpacked — file rejected"},
    "doc_parts": {"ru": "в документе слишком много частей — файл отклонён",
                  "uz": "hujjatda juda koʻp qismlar bor — fayl rad etildi",
                  "en": "the document has too many parts — file rejected"},
    "doc_encrypted": {"ru": "документ зашифрован или защищён паролем",
                      "uz": "hujjat shifrlangan yoki parol bilan himoyalangan",
                      "en": "the document is encrypted or password-protected"},
    "doc_bad": {"ru": "документ повреждён и не открывается", "uz": "hujjat buzilgan va ochilmaydi",
                "en": "the document is damaged and cannot be opened"},
    "doc_dtd": {"ru": "в документе недопустимая разметка (DTD) — файл отклонён",
                "uz": "hujjatda ruxsat etilmagan belgilash (DTD) bor — fayl rad etildi",
                "en": "the document contains forbidden markup (DTD) — file rejected"},
    "prefill_check": {"ru": "из документа, проверьте", "uz": "hujjatdan, tekshiring",
                      "en": "from the document, please check"},
}
