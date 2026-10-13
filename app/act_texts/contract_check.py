"""Сверка договора с актом — 12 проверок (ТЗ 13.10.2026, день 7.2; app/contract_check.py): подписи проверок,
вердикты и строки. Часть словаря текстов акта (app/act_texts): склеивается в TX в __init__.py."""

CC_LABELS = {
    "essentials": {"ru": "Существенные условия", "uz": "Muhim shartlar", "en": "Essential terms"},
    "template": {"ru": "Бланк договора", "uz": "Shartnoma blanki", "en": "Contract form"},
    "product": {"ru": "Продукт", "uz": "Mahsulot", "en": "Product"},
    "sum_insured": {"ru": "Страховая сумма", "uz": "Sugʻurta summasi", "en": "Sum insured"},
    "sum_value": {"ru": "Сумма к стоимости (S/V)", "uz": "Summaning qiymatga nisbati (S/V)",
                  "en": "Sum to value (S/V)"},
    "min_rate": {"ru": "Минимальная ставка", "uz": "Eng kam tarif", "en": "Minimum rate"},
    "act_rate": {"ru": "Ставка акта", "uz": "Dalolatnoma tarifi", "en": "Report rate"},
    "premium": {"ru": "Премия и платежи", "uz": "Mukofot va toʻlovlar", "en": "Premium and payments"},
    "term": {"ru": "Срок и дата осмотра", "uz": "Muddat va koʻrik sanasi", "en": "Term and inspection date"},
    "risks": {"ru": "Риски продукта", "uz": "Mahsulot xavflari", "en": "Product perils"},
    "franchise": {"ru": "Франшиза (справочно)", "uz": "Franshiza (maʼlumot uchun)", "en": "Deductible (for reference)"},
    "beneficiary": {"ru": "Банк-выгодоприобретатель (справочно)", "uz": "Bank — naf oluvchi (maʼlumot uchun)",
                    "en": "Bank as beneficiary (for reference)"},
}

CC_VERDICT_LABELS = {
    "ok": {"ru": "соответствует", "uz": "mos keladi", "en": "complies"},
    "violated": {"ru": "расхождение", "uz": "farq bor", "en": "discrepancy"},
    "review": {"ru": "На рассмотрение", "uz": "Koʻrib chiqishga", "en": "Refer"},
    "decline_or_agree": {"ru": "Отказать / согласовать с ЦО", "uz": "Rad etish / Bosh ofis bilan kelishish",
                         "en": "Decline / agree with head office"},
    "no_check": {"ru": "сверки нет", "uz": "solishtirish yoʻq", "en": "not checked"},
    "skipped": {"ru": "не проверяется", "uz": "tekshirilmaydi", "en": "not checked"},
    "na": {"ru": "нет данных", "uz": "maʼlumot yoʻq", "en": "no data"},
}

TX_CONTRACT_CHECK = {
    "cc_title": {"ru": "Сверка с договором: 12 проверок", "uz": "Shartnoma bilan solishtirish: 12 ta tekshiruv",
                 "en": "Contract check: 12 tests"},
    "cc_skipped": {"ru": "Загружен бланк договора — проверка не выполняется",
                   "uz": "Shartnoma blanki yuklangan — tekshiruv bajarilmaydi",
                   "en": "A blank contract form was uploaded — the test is not run"},
    # 1. существенные условия (ГК РУз, ст. 929)
    "cc_essentials_ok": {"ru": "Существенные условия договора (ГК РУз, ст. 929) есть: {present}",
                         "uz": "Shartnomaning muhim shartlari (OʻzR FK, 929-modda) bor: {present}",
                         "en": "The essential terms of the contract (Civil Code, Art. 929) are present: {present}"},
    "cc_essentials_review": {"ru": "В договоре нет существенных условий (ГК РУз, ст. 929): {missing} — договор "
                                   "на рассмотрение",
                             "uz": "Shartnomada muhim shartlar yoʻq (OʻzR FK, 929-modda): {missing} — shartnoma "
                                   "koʻrib chiqishga",
                             "en": "The contract lacks essential terms (Civil Code, Art. 929): {missing} — refer the "
                                   "contract"},
    # 2. бланк
    "cc_template_ok": {"ru": "Договор заполнен — сверка выполняется", "uz": "Shartnoma toʻldirilgan — solishtiriladi",
                       "en": "The contract is filled in — the check is run"},
    "cc_template_no_check": {"ru": "Загружен бланк договора (поля не заполнены) — сверки с договором нет",
                             "uz": "Shartnoma blanki yuklangan (maydonlar toʻldirilmagan) — shartnoma bilan "
                                   "solishtirish yoʻq",
                             "en": "A blank contract form was uploaded (fields are empty) — no contract check"},
    # 3. продукт
    "cc_product_ok": {"ru": "Продукт договора {contract} совпадает с продуктом акта {act}",
                      "uz": "Shartnoma mahsuloti {contract} dalolatnoma mahsuloti {act} bilan mos",
                      "en": "The contract product {contract} matches the report product {act}"},
    "cc_product_violated": {"ru": "Продукт договора {contract} не совпадает с продуктом акта {act}",
                            "uz": "Shartnoma mahsuloti {contract} dalolatnoma mahsuloti {act} bilan mos emas",
                            "en": "The contract product {contract} does not match the report product {act}"},
    "cc_product_na": {"ru": "Код продукта в договоре не найден — сверить с продуктом акта {act} вручную",
                      "uz": "Shartnomada mahsulot kodi topilmadi — dalolatnoma mahsuloti {act} bilan qoʻlda "
                            "solishtiring",
                      "en": "No product code found in the contract — compare with the report product {act} manually"},
    # 4. страховая сумма
    "cc_sum_insured_ok": {"ru": "Страховая сумма договора {contract} равна сумме акта {act} (допуск {tol})",
                          "uz": "Shartnoma sugʻurta summasi {contract} dalolatnoma summasi {act} ga teng "
                                "(ruxsat {tol})",
                          "en": "The contract sum insured {contract} equals the report sum {act} (tolerance {tol})"},
    "cc_sum_insured_violated": {"ru": "Страховая сумма договора {contract} не равна сумме акта {act}: разница {diff}",
                                "uz": "Shartnoma sugʻurta summasi {contract} dalolatnoma summasi {act} ga teng emas: "
                                      "farq {diff}",
                                "en": "The contract sum insured {contract} differs from the report sum {act}: "
                                      "difference {diff}"},
    "cc_sum_insured_na": {"ru": "Страховая сумма в договоре не найдена", "uz": "Shartnomada sugʻurta summasi topilmadi",
                          "en": "No sum insured found in the contract"},
    # 5. S / V
    "cc_sum_value_ok": {"ru": "S/V = {ratio} — в пределах {lo}–{hi}", "uz": "S/V = {ratio} — {lo}–{hi} oraligʻida",
                        "en": "S/V = {ratio} — within {lo}–{hi}"},
    "cc_sum_value_under": {"ru": "S/V = {ratio} ниже {lo}: недострахование, выплата пропорционально (ГК РУз, ст. 936)",
                           "uz": "S/V = {ratio} {lo} dan past: toʻliq sugʻurtalanmagan, toʻlov mutanosib "
                                 "(OʻzR FK, 936-modda)",
                           "en": "S/V = {ratio} is below {lo}: underinsurance, claims paid pro rata "
                                 "(Civil Code, Art. 936)"},
    "cc_sum_value_over": {"ru": "S/V = {ratio} выше {hi}: страховая сумма больше стоимости, превышение "
                                "недействительно (ГК РУз, ст. 938)",
                          "uz": "S/V = {ratio} {hi} dan yuqori: sugʻurta summasi qiymatdan katta, ortiqcha qismi "
                                "haqiqiy emas (OʻzR FK, 938-modda)",
                          "en": "S/V = {ratio} is above {hi}: the sum insured exceeds the value, the excess is void "
                                "(Civil Code, Art. 938)"},
    "cc_sum_value_na": {"ru": "Нет страховой суммы или стоимости — S/V не посчитано",
                        "uz": "Sugʻurta summasi yoki qiymat yoʻq — S/V hisoblanmadi",
                        "en": "No sum insured or value — S/V not calculated"},
    # 6. минимальная ставка
    "cc_min_rate_ok": {"ru": "Ставка договора {rate} не ниже минимальной {min} (класс {cls})",
                       "uz": "Shartnoma tarifi {rate} eng kam tarif {min} dan past emas ({cls}-klass)",
                       "en": "The contract rate {rate} is not below the minimum {min} (class {cls})"},
    "cc_min_rate_decline_or_agree": {"ru": "Ставка договора {rate} ниже минимальной {min} (класс {cls}) — отказать "
                                           "или согласовать с ЦО",
                                     "uz": "Shartnoma tarifi {rate} eng kam tarif {min} dan past ({cls}-klass) — rad "
                                           "etish yoki Bosh ofis bilan kelishish",
                                     "en": "The contract rate {rate} is below the minimum {min} (class {cls}) — "
                                           "decline or agree with head office"},
    "cc_min_rate_parts": {"ru": "Ставка договора {rate} не ниже минимальной {min} (класс {cls}), но в договоре одна "
                                "ставка на весь продукт: ставки частей классов {unchecked} проверить по каждой части",
                          "uz": "Shartnoma tarifi {rate} eng kam tarif {min} dan past emas ({cls}-klass), lekin "
                                "shartnomada butun mahsulotga bitta tarif: {unchecked} klasslari qismlari tariflarini "
                                "har biri boʻyicha tekshiring",
                          "en": "The contract rate {rate} is not below the minimum {min} (class {cls}), but the "
                                "contract has one rate for the whole product: check the rates of the parts in classes "
                                "{unchecked} one by one"},
    "cc_min_rate_na": {"ru": "Нет ставки договора или минимальной ставки продукта — сравнить нечем",
                       "uz": "Shartnoma tarifi yoki mahsulotning eng kam tarifi yoʻq — solishtirib boʻlmaydi",
                       "en": "No contract rate or product minimum rate — nothing to compare"},
    # 7. ставка акта
    "cc_act_rate_ok": {"ru": "Ставка договора {rate} не ниже ставки акта {r} − {tol} % ({floor})",
                       "uz": "Shartnoma tarifi {rate} dalolatnoma tarifi {r} − {tol} % ({floor}) dan past emas",
                       "en": "The contract rate {rate} is not below the report rate {r} − {tol}% ({floor})"},
    "cc_act_rate_review": {"ru": "Ставка договора {rate} ниже ставки акта {r} более чем на {tol} % (порог {floor}) — "
                                 "на рассмотрение",
                           "uz": "Shartnoma tarifi {rate} dalolatnoma tarifi {r} dan {tol} % dan koʻproq past "
                                 "(chegara {floor}) — koʻrib chiqishga",
                           "en": "The contract rate {rate} is more than {tol}% below the report rate {r} "
                                 "(threshold {floor}) — refer"},
    "cc_act_rate_na": {"ru": "Нет ставки договора или ставки акта — сравнить нечем",
                       "uz": "Shartnoma yoki dalolatnoma tarifi yoʻq — solishtirib boʻlmaydi",
                       "en": "No contract rate or report rate — nothing to compare"},
    # 8. премия и платежи
    "cc_premium_ok": {"ru": "Премия договора {premium} = S × ставка × срок = {calc} (допуск {tol_pct} %)",
                      "uz": "Shartnoma mukofoti {premium} = S × tarif × muddat = {calc} (ruxsat {tol_pct} %)",
                      "en": "The contract premium {premium} = S × rate × term = {calc} (tolerance {tol_pct}%)"},
    "cc_premium_premium": {"ru": "Премия договора {premium} не равна S × ставка × срок = {calc} (допуск {tol_pct} %)",
                           "uz": "Shartnoma mukofoti {premium} S × tarif × muddat = {calc} ga teng emas "
                                 "(ruxsat {tol_pct} %)",
                           "en": "The contract premium {premium} is not equal to S × rate × term = {calc} "
                                 "(tolerance {tol_pct}%)"},
    "cc_premium_payments": {"ru": "Сумма платежей по графику {payments_total} ({payments_n}) не равна премии {premium}",
                            "uz": "Jadval boʻyicha toʻlovlar summasi {payments_total} ({payments_n}) mukofot {premium} "
                                  "ga teng emas",
                            "en": "The scheduled payments {payments_total} ({payments_n}) do not add up to the premium "
                                  "{premium}"},
    "cc_premium_na": {"ru": "Нет премии, ставки, суммы или срока договора — премию не проверить",
                      "uz": "Shartnoma mukofoti, tarifi, summasi yoki muddati yoʻq — mukofotni tekshirib boʻlmaydi",
                      "en": "No contract premium, rate, sum or term — the premium cannot be checked"},
    # 9. срок и осмотр
    "cc_term_ok": {"ru": "Срок с {from} по {to}; осмотр {inspection} — за {before_days} дн. до начала",
                   "uz": "Muddat {from} dan {to} gacha; koʻrik {inspection} — boshlanishidan {before_days} kun oldin",
                   "en": "Term {from} to {to}; inspection {inspection} — {before_days} days before the start"},
    "cc_term_no_dates": {"ru": "В договоре нет дат начала и окончания срока",
                         "uz": "Shartnomada muddatning boshlanish va tugash sanalari yoʻq",
                         "en": "The contract has no start and end dates"},
    "cc_term_order": {"ru": "Дата окончания {to} раньше даты начала {from}",
                      "uz": "Tugash sanasi {to} boshlanish sanasi {from} dan oldin",
                      "en": "The end date {to} is before the start date {from}"},
    "cc_term_after_start": {"ru": "Осмотр {inspection} позже начала срока {from}: имущество застраховано до осмотра",
                            "uz": "Koʻrik {inspection} muddat boshlanishi {from} dan keyin: mol-mulk koʻrikdan oldin "
                                  "sugʻurtalangan",
                            "en": "The inspection {inspection} is after the start {from}: the property was insured "
                                  "before the inspection"},
    "cc_term_too_early": {"ru": "Осмотр {inspection} за {before_days} дн. до начала срока {from} — больше {max_days} "
                                "дн.: нужен повторный осмотр",
                          "uz": "Koʻrik {inspection} muddat boshlanishi {from} dan {before_days} kun oldin — "
                                "{max_days} kundan koʻp: qayta koʻrik kerak",
                          "en": "The inspection {inspection} is {before_days} days before the start {from} — more "
                                "than {max_days} days: re-inspection needed"},
    "cc_term_na": {"ru": "Срок страхования в договоре не найден", "uz": "Shartnomada sugʻurta muddati topilmadi",
                   "en": "No insurance term found in the contract"},
    # 10. риски
    "cc_risks_ok": {"ru": "Риски договора входят в покрытие продукта: {risks}",
                    "uz": "Shartnoma xavflari mahsulot qoplamiga kiradi: {risks}",
                    "en": "The contract perils are within the product cover: {risks}"},
    "cc_risks_violated": {"ru": "Риски договора вне покрытия продукта: {outside}",
                          "uz": "Shartnoma xavflari mahsulot qoplamidan tashqarida: {outside}",
                          "en": "Contract perils outside the product cover: {outside}"},
    "cc_risks_unknown": {"ru": "Риски договора не сверены со списком продукта: {unknown} — проверить андеррайтеру",
                         "uz": "Shartnoma xavflari mahsulot roʻyxati bilan solishtirilmadi: {unknown} — anderrayter "
                               "tekshirsin",
                         "en": "Contract perils not matched with the product list: {unknown} — for the underwriter"},
    "cc_risks_na": {"ru": "Нет рисков договора или списка рисков продукта — сравнить нечем",
                    "uz": "Shartnoma xavflari yoki mahsulot xavflari roʻyxati yoʻq — solishtirib boʻlmaydi",
                    "en": "No contract perils or product peril list — nothing to compare"},
    # 11. франшиза (справочно)
    "cc_franchise_ok": {"ru": "Франшиза договора {contract_pct} не ниже рекомендованной {rec_pct} (справочно)",
                        "uz": "Shartnoma franshizasi {contract_pct} tavsiya etilgan {rec_pct} dan past emas "
                              "(maʼlumot uchun)",
                        "en": "The contract deductible {contract_pct} is not below the recommended {rec_pct} "
                              "(for reference)"},
    "cc_franchise_violated": {"ru": "Франшиза договора {contract_pct} ниже рекомендованной {rec_pct} (справочно)",
                              "uz": "Shartnoma franshizasi {contract_pct} tavsiya etilgan {rec_pct} dan past "
                                    "(maʼlumot uchun)",
                              "en": "The contract deductible {contract_pct} is below the recommended {rec_pct} "
                                    "(for reference)"},
    "cc_franchise_na": {"ru": "Акт франшизу не рекомендует — сравнивать не с чем (справочно)",
                        "uz": "Dalolatnoma franshiza tavsiya qilmaydi — solishtirib boʻlmaydi (maʼlumot uchun)",
                        "en": "The report recommends no deductible — nothing to compare (for reference)"},
    "cc_franchise_na_contract": {"ru": "Франшиза в договоре не найдена; рекомендованная — {rec_pct} (справочно)",
                                 "uz": "Shartnomada franshiza topilmadi; tavsiya etilgani — {rec_pct} "
                                       "(maʼlumot uchun)",
                                 "en": "No deductible found in the contract; recommended — {rec_pct} (for reference)"},
    # 12. банк-выгодоприобретатель (справочно)
    "cc_beneficiary_ok": {"ru": "Выгодоприобретатель — банк, продукт «в залоге»: {product}",
                          "uz": "Naf oluvchi — bank, mahsulot «garovda»: {product}",
                          "en": "The beneficiary is a bank and the product is a pledge product: {product}"},
    "cc_beneficiary_none": {"ru": "Выгодоприобретатель — не банк: проверка не нужна (справочно)",
                            "uz": "Naf oluvchi — bank emas: tekshiruv kerak emas (maʼlumot uchun)",
                            "en": "The beneficiary is not a bank: no check needed (for reference)"},
    "cc_beneficiary_violated": {"ru": "Выгодоприобретатель — банк, а продукт не «в залоге»: {product} — проверить "
                                      "продукт (справочно)",
                                "uz": "Naf oluvchi — bank, lekin mahsulot «garovda» emas: {product} — mahsulotni "
                                      "tekshiring (maʼlumot uchun)",
                                "en": "The beneficiary is a bank but the product is not a pledge product: {product} — "
                                      "check the product (for reference)"},
    # итог и пункт проверок андеррайтера
    "cc_summary_ok": {"ru": "Сверка с договором: нарушений нет", "uz": "Shartnoma bilan solishtirish: buzilish yoʻq",
                      "en": "Contract check: no breaches"},
    "cc_summary_violated": {"ru": "Сверка с договором: есть расхождения — проверить",
                            "uz": "Shartnoma bilan solishtirish: farqlar bor — tekshiring",
                            "en": "Contract check: discrepancies — check them"},
    "cc_summary_review": {"ru": "Сверка с договором: на рассмотрение андеррайтера",
                          "uz": "Shartnoma bilan solishtirish: anderrayter koʻrib chiqishiga",
                          "en": "Contract check: refer to the underwriter"},
    "cc_summary_decline_or_agree": {"ru": "Сверка с договором: отказать или согласовать с ЦО",
                                    "uz": "Shartnoma bilan solishtirish: rad etish yoki Bosh ofis bilan kelishish",
                                    "en": "Contract check: decline or agree with head office"},
    "cc_summary_no_check": {"ru": "Сверка с договором: загружен бланк — сверки нет",
                            "uz": "Shartnoma bilan solishtirish: blank yuklangan — solishtirish yoʻq",
                            "en": "Contract check: a blank form was uploaded — no check"},
    "cc_summary_na": {"ru": "Сверка с договором: данных для сверки нет",
                      "uz": "Shartnoma bilan solishtirish: solishtirish uchun maʼlumot yoʻq",
                      "en": "Contract check: no data to compare"},
    "c_cc": {"ru": "Сверка с договором — {verdict}: {what}", "uz": "Shartnoma bilan solishtirish — {verdict}: {what}",
             "en": "Contract check — {verdict}: {what}"},
    # 7.1: условия договора в полях осмотра
    "from_contract": {"ru": "из договора", "uz": "shartnomadan", "en": "from the contract"},
}
