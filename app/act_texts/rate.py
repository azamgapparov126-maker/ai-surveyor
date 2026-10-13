"""Ставка: комплексный продукт по частям, замечания контролёра (кредит, части, минимум класса).
Часть словаря текстов акта (app/act_texts): склеивается в TX в __init__.py в прежнем порядке."""

# ---------- комплексный продукт по частям (30.09.2026, ТЗ универсального шаблона 4.1в) ----------
TX_PARTS = {
    # часть продукта из справочника частей (ТЗ 13.10.2026, день 2.1): правка администратора или импорт из Excel
    "how_part_src": {"ru": "Ставка части {n} продукта {code} (класс {cls}) — из справочника частей, версия с {date}: "
                           "базовая {base}, минимальная {min}",
                     "uz": "{code} mahsulotining {n}-qismi ({cls}-klass) tarifi — qismlar maʼlumotnomasidan, {date} "
                           "dagi versiya: asosiy {base}, eng kam {min}",
                     "en": "Rate of part {n} of product {code} (class {cls}) — from the product parts reference, "
                           "version from {date}: base {base}, minimum {min}"},
    "how_part_min_text": {"ru": "Ставка класса {cls} по тарифной политике продукта {code} — {rate} (из текста тарифа: "
                                "часть договора)",
                          "uz": "{code} mahsuloti tarif siyosati boʻyicha {cls}-klass tarifi — {rate} (tarif matnidan: "
                                "shartnoma qismi)",
                          "en": "Class {cls} rate under the tariff policy of product {code} — {rate} (from the tariff "
                                "text: a part of the contract)"},
    "how_part_outside": {"ru": "Класс {cls} не входит в состав продукта {code} — ставки тарифной политики для него нет, "
                               "база — техническая ставка класса",
                         "uz": "{cls}-klass {code} mahsuloti tarkibiga kirmaydi — tarif siyosatida uning tarifi yoʻq, "
                               "asos — klassning texnik tarifi",
                         "en": "Class {cls} is not part of product {code} — the tariff policy has no rate for it, the "
                               "base is the class technical rate"},
    "how_part_min_none": {"ru": "Для класса {cls} ставки в тарифной политике продукта {code} не найдено — база "
                                "техническая ставка класса, минимум не задан",
                          "uz": "{code} mahsuloti tarif siyosatida {cls}-klass uchun tarif topilmadi — asos klassning "
                                "texnik tarifi, eng kam tarif belgilanmagan",
                          "en": "No rate for class {cls} in the tariff policy of product {code} — the base is the "
                                "class technical rate, no minimum set"},
    "how_multi": {"ru": "Договор из {n} частей: ставка и минимум проверены по каждому классу отдельно (правило проекта "
                        "№ 5), премия договора = сумма премий частей; средняя ставка договора — только справочно",
                  "uz": "{n} qismdan iborat shartnoma: tarif va eng kam tarif har bir klass boʻyicha alohida "
                        "tekshirildi, shartnoma mukofoti = qismlar mukofotlari yigʻindisi; shartnomaning oʻrtacha "
                        "tarifi — faqat maʼlumot uchun",
                  "en": "A contract of {n} parts: the rate and the minimum are checked for each class separately, the "
                        "contract premium = the sum of the parts' premiums; the contract average rate is for "
                        "reference only"},
    "fr_parts": {"ru": "Франшиза — по каждой части отдельно", "uz": "Franshiza — har bir qism boʻyicha alohida",
                 "en": "Deductible — for each part separately"},
    "sc_w_parts_max": {"ru": "один объект: большее из частей", "uz": "bitta obyekt: qismlarning kattasi",
                       "en": "one object: the larger of the parts"},
    "sc_w_parts_sum": {"ru": "разные объекты: сумма частей", "uz": "turli obyektlar: qismlar yigʻindisi",
                       "en": "different objects: the sum of the parts"},
    "sc_w_parts_mixed": {"ru": "большее по одному объекту плюс другие объекты",
                         "uz": "bitta obyekt boʻyicha kattasi va boshqa obyektlar",
                         "en": "the larger for one object plus the other objects"},
    "pt_sc_na": {"ru": "Сценарий убытка не считается ни по одной части договора",
                 "uz": "Zarar ssenariysi shartnomaning birorta qismi boʻyicha hisoblanmaydi",
                 "en": "The loss scenario is not calculated for any part of the contract"},
    "pt_sc_rule_max": {"ru": "части относятся к одному объекту — по каждому сценарию берётся больший из частей",
                       "uz": "qismlar bitta obyektga tegishli — har bir ssenariy boʻyicha qismlarning kattasi olinadi",
                       "en": "the parts relate to one object — the larger of the parts is taken for each scenario"},
    "pt_sc_rule_sum": {"ru": "части относятся к разным объектам — сценарии частей складываются",
                       "uz": "qismlar turli obyektlarga tegishli — qismlar ssenariylari qoʻshiladi",
                       "en": "the parts relate to different objects — the parts' scenarios are added up"},
    "pt_sc_rule_mixed": {"ru": "по частям одного объекта берётся больший сценарий, сценарии частей других объектов "
                               "прибавляются",
                         "uz": "bitta obyekt qismlari boʻyicha katta ssenariy olinadi, boshqa obyektlar qismlari "
                               "ssenariylari qoʻshiladi",
                         "en": "the larger scenario is taken for the parts of one object, the scenarios of the parts "
                               "of other objects are added"},
    "pt_sc_max": {"ru": "большее из ({vals})", "uz": "kattasi ({vals})", "en": "the larger of ({vals})"},
    "pt_part_short": {"ru": "часть {n}", "uz": "{n}-qism", "en": "part {n}"},
    "pt_sc_line": {"ru": "{s} договора = {expr} = {total}", "uz": "Shartnoma {s} = {expr} = {total}",
                   "en": "Contract {s} = {expr} = {total}"},
    "pt_sc_excluded": {"ru": "Часть {n} (класс {cls}): сценарий не считается — в сумму договора не входит",
                       "uz": "{n}-qism ({cls}-klass): ssenariy hisoblanmaydi — shartnoma yigʻindisiga kirmaydi",
                       "en": "Part {n} (class {cls}): the scenario is not calculated — not included in the contract "
                             "total"},
    "pt_sc_ret": {"ru": "Удержание сравнивается с EML договора; лимит — наименьший из лимитов частей (Положение "
                        "№ 1806, п. 15, таблица линий по классу)",
                  "uz": "Ushlab qolish shartnoma EML bilan solishtiriladi; limit — qismlar limitlarining eng kichigi "
                        "(1806-son Nizom, 15-band, klass boʻyicha liniyalar jadvali)",
                  "en": "Retention is compared with the contract EML; the limit is the lowest of the parts' limits "
                        "(Regulation No. 1806, cl. 15, lines table by class)"},
    "pt_part": {"ru": "Часть {n}: класс {cls}", "uz": "{n}-qism: klass {cls}", "en": "Part {n}: class {cls}"},
    "pt_ratio_note": {"ru": "по частям со страховой стоимостью: сумма {sum}, стоимость {value}",
                      "uz": "sugʻurta qiymati bor qismlar boʻyicha: summa {sum}, qiymat {value}",
                      "en": "for the parts with an insurable value: sum {sum}, value {value}"},
    "pt_same_object": {"ru": "тот же объект", "uz": "oʻsha obyekt", "en": "same object"},
    "pt_other_object": {"ru": "другой объект", "uz": "boshqa obyekt", "en": "another object"},
    "pt_outside": {"ru": "класс не из состава продукта — проверьте", "uz": "klass mahsulot tarkibida emas — tekshiring",
                   "en": "the class is not part of the product — please check"},
    "pt_guess": {"ru": "класс по наименованию не распознан — проверьте",
                 "uz": "klass nomi boʻyicha aniqlanmadi — tekshiring",
                 "en": "the class was not recognised from the name — please check"},
    "pt_s1_value": {"ru": "{sum} ({share} страховой суммы)", "uz": "{sum} (sugʻurta summasining {share})",
                    "en": "{sum} ({share} of the sum insured)"},
    "pt_s1_par": {"ru": "Договор состоит из {n} частей ({mode}); каждая часть — отдельный условный договор своего "
                        "класса (Положение 1882, п. 11).",
                  "uz": "Shartnoma {n} qismdan iborat ({mode}); har bir qism — oʻz klassining alohida shartli "
                        "shartnomasi (1882-son Nizom, 11-band).",
                  "en": "The contract consists of {n} parts ({mode}); each part is a separate notional contract of "
                        "its class (Regulation 1882, cl. 11)."},
    "pt_mode_one": {"ru": "один объект", "uz": "bitta obyekt", "en": "one object"},
    "pt_mode_different": {"ru": "разные объекты", "uz": "turli obyektlar", "en": "different objects"},
    "pt_mode_mixed": {"ru": "часть частей — один объект, остальные — другие объекты",
                      "uz": "baʼzi qismlar — bitta obyekt, qolganlari — boshqa obyektlar",
                      "en": "some parts relate to one object, the rest to other objects"},
    "pt_value_default": {"ru": "Стоимость части не введена — принята долей стоимости объекта договора; подтвердите.",
                         "uz": "Qism qiymati kiritilmagan — shartnoma obyekti qiymatining ulushi olindi; tasdiqlang.",
                         "en": "The part's value was not entered — a share of the contract object value is used; "
                               "please confirm."},
    "pt_s3_note": {"ru": "сумма {sum}, стоимость {value}", "uz": "summa {sum}, qiymat {value}",
                   "en": "sum {sum}, value {value}"},
    "pt_value_na": {"ru": "Не применяется: у ответственности, несчастного случая и финансовых рисков нет страховой "
                          "стоимости (ГК РУз, ст. 936, 938 — об имущественном страховании)",
                    "uz": "Qoʻllanilmaydi: javobgarlik, baxtsiz hodisa va moliyaviy xavflarda sugʻurta qiymati yoʻq "
                          "(OʻzR FK, 936, 938-moddalar — mol-mulk sugʻurtasi haqida)",
                    "en": "Not applicable: liability, accident and financial risks have no insurable value (Civil Code, "
                          "Art. 936, 938 — on property insurance)"},
    "pt_value_na_short": {"ru": "не применяется", "uz": "qoʻllanilmaydi", "en": "not applicable"},
    "pt_s3_par": {"ru": "Сумма к стоимости проверена по каждой части и по договору (части со страховой стоимостью).",
                  "uz": "Summaning qiymatga nisbati har bir qism va shartnoma boʻyicha tekshirildi (sugʻurta qiymati "
                        "bor qismlar).",
                  "en": "The sum-to-value ratio is checked for each part and for the contract (parts with an "
                        "insurable value)."},
    "pt_fr_statutory": {"ru": "не применяется (обязательный вид)", "uz": "qoʻllanilmaydi (majburiy tur)",
                        "en": "not applied (compulsory class)"},
    "pt_fr_applied": {"ru": "применена {pct}", "uz": "qoʻllanildi {pct}", "en": "applied {pct}"},
    "pt_fr_proposed": {"ru": "рекомендована {pct}", "uz": "tavsiya etilgan {pct}", "en": "recommended {pct}"},
    "pt_fr_proposed_nosize": {"ru": "рассмотреть", "uz": "koʻrib chiqish", "en": "to consider"},
    "pt_fr_none": {"ru": "не требуется", "uz": "talab qilinmaydi", "en": "not required"},
    "pt_fr_by_parts": {"ru": "по каждой части отдельно: {list}", "uz": "har bir qism boʻyicha alohida: {list}",
                       "en": "for each part separately: {list}"},
    "pt_sub_level": {"ru": "{part}: уровень риска «{level}»", "uz": "{part}: xavf darajasi «{level}»",
                     "en": "{part}: risk level «{level}»"},
    "pt_sub_rate": {"ru": "{part}: как посчитан тариф", "uz": "{part}: tarif qanday hisoblangan",
                    "en": "{part}: how the rate was calculated"},
    "pt_statutory_note": {"ru": "Обязательная часть: ставка строго по нормативному акту, без поправок и без франшизы",
                          "uz": "Majburiy qism: tarif qatʼiy meʼyoriy hujjat boʻyicha, tuzatishlarsiz va franshizasiz",
                          "en": "Compulsory part: the rate strictly per the statutory act, without loadings or "
                                "deductible"},
    "pt_sub_fr": {"ru": "{part}: франшиза", "uz": "{part}: franshiza", "en": "{part}: deductible"},
    "pt_sub_sc": {"ru": "{part}: сценарии убытка", "uz": "{part}: zarar ssenariylari", "en": "{part}: loss scenarios"},
    "pt_sub_summary": {"ru": "{part}: резюме аналитики", "uz": "{part}: tahlil xulosasi",
                       "en": "{part}: analytics summary"},
    "pt_col_n": {"ru": "№", "uz": "№", "en": "No."},
    "pt_col_class": {"ru": "Класс", "uz": "Klass", "en": "Class"},
    "pt_col_sum": {"ru": "Страховая сумма", "uz": "Sugʻurta summasi", "en": "Sum insured"},
    "pt_col_level": {"ru": "Уровень", "uz": "Daraja", "en": "Level"},
    "pt_col_rate": {"ru": "Ставка", "uz": "Tarif", "en": "Rate"},
    "pt_col_premium": {"ru": "Премия", "uz": "Mukofot", "en": "Premium"},
    "pt_col_fr": {"ru": "Франшиза", "uz": "Franshiza", "en": "Deductible"},
    "pt_total": {"ru": "Итого по договору", "uz": "Shartnoma boʻyicha jami", "en": "Contract total"},
    "pt_table_title": {"ru": "Части договора", "uz": "Shartnoma qismlari", "en": "Contract parts"},
    "pt_table_note": {"ru": "Ставка каждой части не ниже минимума своего класса; средняя по договору для проверки "
                            "минимума не используется. Уровень договора — самый высокий среди частей.",
                      "uz": "Har bir qism tarifi oʻz klassi minimumidan past emas; shartnoma boʻyicha oʻrtacha tarif "
                            "minimumni tekshirishda ishlatilmaydi. Shartnoma darajasi — qismlar ichida eng yuqorisi.",
                      "en": "Each part's rate is not below its class minimum; the contract average is not used to "
                            "check the minimum. The contract level is the highest among the parts."},
    "pt_level_note": {"ru": "по самой опасной части: {part}", "uz": "eng xavfli qism boʻyicha: {part}",
                      "en": "by the riskiest part: {part}"},
    "pt_premium_note": {"ru": "сумма премий {n} частей за весь срок ({days} дн.)",
                        "uz": "{n} qism mukofotlari yigʻindisi, butun muddat uchun ({days} kun)",
                        "en": "the sum of {n} parts' premiums for the whole term ({days} days)"},
    "pt_premium_incomplete": {"ru": "не у всех частей определена ставка; известная часть премии — {known}",
                              "uz": "hamma qismlarda tarif aniqlanmagan; maʼlum mukofot qismi — {known}",
                              "en": "not all parts have a rate; the known part of the premium is {known}"},
    "pt_ref_rate": {"ru": "Ставка договора (справочно)", "uz": "Shartnoma tarifi (maʼlumot uchun)",
                    "en": "Contract rate (for reference)"},
    "pt_ref_note": {"ru": "премия / страховая сумма; средняя не используется для проверки минимума",
                    "uz": "mukofot / sugʻurta summasi; oʻrtacha tarif minimumni tekshirishda ishlatilmaydi",
                    "en": "premium / sum insured; the average is not used to check the minimum"},
    "pt_src_employee": {"ru": "распределение сотрудника", "uz": "xodim taqsimoti", "en": "the employee's split"},
    "pt_src_contract": {"ru": "по перечню объектов договора", "uz": "shartnomadagi obyektlar roʻyxati boʻyicha",
                        "en": "by the list of objects in the contract"},
    "pt_src_policy_shares": {"ru": "по долям тарифной политики", "uz": "tarif siyosati ulushlari boʻyicha",
                             "en": "by the tariff policy shares"},
    "pt_src_default": {"ru": "поровну по умолчанию", "uz": "sukut boʻyicha teng", "en": "equally by default"},
    "pt_s5_confirmed": {"ru": "Распределение страховой суммы по классам подтверждено сотрудником.",
                        "uz": "Sugʻurta summasining klasslar boʻyicha taqsimoti xodim tomonidan tasdiqlangan.",
                        "en": "The split of the sum insured by class is confirmed by the employee."},
    "pt_s5_default": {"ru": "Распределение страховой суммы по классам принято {source} — подтвердить до оформления "
                            "договора.",
                      "uz": "Sugʻurta summasining klasslar boʻyicha taqsimoti {source} olingan — shartnoma "
                            "rasmiylashtirilgunga qadar tasdiqlash kerak.",
                      "en": "The split of the sum insured by class is taken {source} — confirm it before the contract "
                            "is issued."},
    "pt_sum_intro": {"ru": "Договор из {n} частей: премия {premium}, уровень риска договора «{level}» (по самой "
                           "опасной части: {part}).",
                     "uz": "{n} qismli shartnoma: mukofot {premium}, shartnoma xavf darajasi «{level}» (eng xavfli "
                           "qism boʻyicha: {part}).",
                     "en": "A contract of {n} parts: premium {premium}, contract risk level «{level}» (by the "
                           "riskiest part: {part})."},
    "pt_sum_sc": {"ru": "Крупный убыток по договору: EML {eml}, MFL {mfl} ({rule}).",
                  "uz": "Shartnoma boʻyicha yirik zarar: EML {eml}, MFL {mfl} ({rule}).",
                  "en": "Large loss for the contract: EML {eml}, MFL {mfl} ({rule})."},
    "pt_sum_ret_ok": {"ru": "EML договора в пределах удержания {limit}.",
                      "uz": "Shartnoma EML ushlab qolish {limit} doirasida.",
                      "en": "The contract EML is within the retention {limit}."},
    "pt_sum_ret_over": {"ru": "EML договора выше удержания {limit} на {x} — рассмотреть перестрахование.",
                        "uz": "Shartnoma EML ushlab qolish {limit} dan {x} ga yuqori — qayta sugʻurtalashni koʻrib "
                              "chiqish kerak.",
                        "en": "The contract EML exceeds the retention {limit} by {x} — consider reinsurance."},
    "pt_an_by_parts": {"ru": "Аналитика риска — по каждой части договора (parts.items[].analytics)",
                       "uz": "Xavf tahlili — shartnomaning har bir qismi boʻyicha (parts.items[].analytics)",
                       "en": "Risk analytics — for each part of the contract (parts.items[].analytics)"},
    "pt_factors_title": {"ru": "Что влияет на уровень договора (самая опасная часть {n})",
                         "uz": "Shartnoma darajasiga nima taʼsir qiladi (eng xavfli qism — {n})",
                         "en": "What drives the contract level (riskiest part {n})"},
    "pt_missing_title": {"ru": "Не хватает данных по частям", "uz": "Qismlar boʻyicha maʼlumot yetishmaydi",
                         "en": "Missing data for the parts"},
    "pt_n_class_guess": {"ru": "Объекты договора «{names}» не распознаны по наименованию — отнесены к классу {cls}, "
                               "проверьте.",
                         "uz": "Shartnomadagi «{names}» obyektlari nomi boʻyicha aniqlanmadi — {cls}-klassga "
                               "kiritildi, tekshiring.",
                         "en": "Contract objects «{names}» were not recognised by name — assigned to class {cls}, "
                               "please check."},
    "pt_n_items_sum": {"ru": "Суммы объектов договора ({sum}) не сходятся со страховой суммой ({total}) — перечень "
                             "объектов для разбора не взят.",
                       "uz": "Shartnoma obyektlari summalari ({sum}) sugʻurta summasi ({total}) bilan mos emas — "
                             "obyektlar roʻyxati olinmadi.",
                       "en": "The contract objects' sums ({sum}) do not match the sum insured ({total}) — the object "
                             "list was not used."},
    "pt_n_default": {"ru": "Долей страховой суммы по классам в тарифной политике нет — сумма разделена поровну на {n} "
                           "части по умолчанию; подтвердите или исправьте.",
                     "uz": "Tarif siyosatida sugʻurta summasining klasslar boʻyicha ulushlari yoʻq — summa sukut "
                           "boʻyicha {n} qismga teng boʻlindi; tasdiqlang yoki tuzating.",
                     "en": "The tariff policy has no shares of the sum insured by class — the sum is split equally "
                           "into {n} parts by default; confirm or correct it."},
    "pt_n_outside": {"ru": "Часть {n}: класс {cls} не входит в состав продукта ({classes}) — принят по вводу "
                           "сотрудника, проверьте.",
                     "uz": "{n}-qism: {cls}-klass mahsulot tarkibiga kirmaydi ({classes}) — xodim kiritgani boʻyicha "
                           "olindi, tekshiring.",
                     "en": "Part {n}: class {cls} is not part of the product ({classes}) — taken as entered by the "
                           "employee, please check."},
    "pt_n_value_default": {"ru": "Стоимость объекта договора разделена между частями со страховой стоимостью "
                                 "пропорционально их суммам (где стоимость части не ввели).",
                           "uz": "Shartnoma obyekti qiymati sugʻurta qiymati bor qismlar oʻrtasida ularning summalariga "
                                 "mutanosib taqsimlandi (qism qiymati kiritilmagan joyda).",
                           "en": "The contract object value is split among the parts with an insurable value in "
                                 "proportion to their sums (where the part's value was not entered)."},
    "pt_n_confirm": {"ru": "Распределение — предложение системы: подтвердите на экране.",
                     "uz": "Taqsimot — tizim taklifi: ekranda tasdiqlang.",
                     "en": "The split is the system's proposal: confirm it on the screen."},
    "c_parts_confirm": {"ru": "Подтвердить распределение страховой суммы по классам ({source})",
                        "uz": "Sugʻurta summasining klasslar boʻyicha taqsimotini tasdiqlash ({source})",
                        "en": "Confirm the split of the sum insured by class ({source})"},
    "c_part_rate_undefined": {"ru": "Часть {n} (класс {cls}): определить ставку — в справочнике её нет",
                              "uz": "{n}-qism ({cls}-klass): tarifni belgilash — maʼlumotnomada yoʻq",
                              "en": "Part {n} (class {cls}): set the rate — it is not in the reference"},
    "c_part_statutory": {"ru": "Часть {n} (класс {cls}): тариф нормативного акта без поправок и франшизы",
                         "uz": "{n}-qism ({cls}-klass): meʼyoriy hujjat tarifi tuzatish va franshizasiz",
                         "en": "Part {n} (class {cls}): the statutory tariff without loadings or deductible"},
    "c_part_under": {"ru": "Часть {n} (класс {cls}): недострахование — предупредить о пропорциональной выплате "
                           "(ГК РУз, ст. 936)",
                     "uz": "{n}-qism ({cls}-klass): toʻliq sugʻurtalanmagan — mutanosib toʻlov haqida ogohlantirish "
                           "(OʻzR FK, 936-modda)",
                     "en": "Part {n} (class {cls}): underinsurance — warn about proportional settlement (Civil Code, "
                           "Art. 936)"},
    "c_part_over": {"ru": "Часть {n} (класс {cls}): сумма выше стоимости — снизить до стоимости (ГК РУз, ст. 938)",
                    "uz": "{n}-qism ({cls}-klass): summa qiymatdan yuqori — qiymatgacha kamaytirish (OʻzR FK, "
                          "938-modda)",
                    "en": "Part {n} (class {cls}): the sum exceeds the value — reduce it to the value (Civil Code, "
                          "Art. 938)"},
    "c_part_franchise": {"ru": "Часть {n} (класс {cls}): решить вопрос о франшизе и её размере",
                         "uz": "{n}-qism ({cls}-klass): franshiza va uning miqdori masalasini hal qilish",
                         "en": "Part {n} (class {cls}): decide on the deductible and its amount"},
    "c_part_fr_applied": {"ru": "Часть {n} (класс {cls}): франшиза сотрудника — условие договора, подтвердить",
                          "uz": "{n}-qism ({cls}-klass): xodim franshizasi — shartnoma sharti, tasdiqlash",
                          "en": "Part {n} (class {cls}): the employee's deductible is a contract term, confirm it"},
    "c_part_class_check": {"ru": "Часть {n} (класс {cls}): проверить класс части",
                           "uz": "{n}-qism ({cls}-klass): qism klassini tekshirish",
                           "en": "Part {n} (class {cls}): check the part's class"},
    "c_part_credit_over": {"ru": "Часть {n} (класс {cls}): страховая сумма выше допустимой: {sum} при кредите {credit} "
                                 "и обеспечении {collateral} — страхуется только необеспеченная часть и не более "
                                 "{share} % кредита (правило компании, требования НАПП) — уменьшить до {insurable} "
                                 "(превышение {excess})",
                           "uz": "{n}-qism ({cls}-klass): sugʻurta summasi ruxsat etilganidan yuqori: kredit {credit} "
                                 "va taʼminot {collateral} boʻlganda {sum} — faqat taʼminlanmagan qism va kreditning "
                                 "{share} % idan koʻp boʻlmagan qismi sugʻurtalanadi (kompaniya qoidasi, SNAP talablari)"
                                 " — {insurable} gacha kamaytirish (oshiqcha {excess})",
                           "en": "Part {n} (class {cls}): the sum insured is above the permitted amount: {sum} with a "
                                 "loan of {credit} and collateral of {collateral} — only the unsecured part and not "
                                 "more than {share} % of the loan may be insured (company rule, regulator's "
                                 "requirements) — reduce to {insurable} (excess {excess})"},
}

# ---------- замечания контролёра 30.09.2026: кредит, части, минимум класса, лимиты сценариев ----------
TX_CONTROLLER = {
    "c_credit_need_data": {"ru": "Кредит: проверить, что страховая сумма не превышает необеспеченную часть и {share} % "
                                 "суммы кредита — введите сумму кредита и обеспечение (правило компании, требования "
                                 "НАПП)",
                           "uz": "Kredit: sugʻurta summasi taʼminlanmagan qismdan va kredit summasining {share} % idan "
                                 "oshmasligini tekshirish — kredit summasi va taʼminotni kiriting (kompaniya qoidasi, "
                                 "SNAP talablari)",
                           "en": "Credit: check that the sum insured does not exceed the unsecured part and {share} % "
                                 "of the loan — enter the loan amount and the collateral (company rule, regulator's "
                                 "requirements)"},
    "c_part_credit_need_data": {"ru": "Часть {n} (класс {cls}). Кредит: проверить, что страховая сумма не превышает "
                                      "необеспеченную часть и {share} % суммы кредита — введите сумму кредита и "
                                      "обеспечение",
                                "uz": "{n}-qism ({cls}-klass). Kredit: sugʻurta summasi taʼminlanmagan qismdan va kredit "
                                      "summasining {share} % idan oshmasligini tekshirish — kredit summasi va "
                                      "taʼminotni kiriting",
                                "en": "Part {n} (class {cls}). Credit: check that the sum insured does not exceed the "
                                      "unsecured part and {share} % of the loan — enter the loan amount and the "
                                      "collateral"},
    "c_credit_holder_not_bank": {"ru": "По кредитному страхованию страхователь и плательщик премии — банк-кредитор "
                                       "(правило компании); в документе ({where}) страхователь — {holder}",
                                 "uz": "Kredit sugʻurtasida sugʻurta qildiruvchi va mukofot toʻlovchi — kreditor bank "
                                       "(kompaniya qoidasi); hujjatda ({where}) sugʻurta qildiruvchi — {holder}",
                                 "en": "In credit insurance the policyholder and premium payer is the lending bank "
                                       "(company rule); in the document ({where}) the policyholder is {holder}"},
    "c_part_credit_holder_not_bank": {"ru": "Часть {n} (класс {cls}): по кредитному страхованию страхователь и "
                                            "плательщик премии — банк-кредитор (правило компании); в документе "
                                            "({where}) страхователь — {holder}",
                                      "uz": "{n}-qism ({cls}-klass): kredit sugʻurtasida sugʻurta qildiruvchi va mukofot "
                                            "toʻlovchi — kreditor bank (kompaniya qoidasi); hujjatda ({where}) sugʻurta "
                                            "qildiruvchi — {holder}",
                                      "en": "Part {n} (class {cls}): in credit insurance the policyholder and premium "
                                            "payer is the lending bank (company rule); in the document ({where}) the "
                                            "policyholder is {holder}"},
    "c_credit_holder_unknown": {"ru": "Кредит: проверить, что страхователь и плательщик премии — банк-кредитор "
                                      "(правило компании); в запросе и договоре страхователь не найден",
                                "uz": "Kredit: sugʻurta qildiruvchi va mukofot toʻlovchi kreditor bank ekanini tekshirish "
                                      "(kompaniya qoidasi); soʻrov va shartnomada sugʻurta qildiruvchi topilmadi",
                                "en": "Credit: check that the policyholder and premium payer is the lending bank "
                                      "(company rule); no policyholder was found in the request or contract"},
    "c_part_credit_holder_unknown": {"ru": "Часть {n} (класс {cls}): проверить, что страхователь и плательщик премии — "
                                           "банк-кредитор (правило компании); в запросе и договоре страхователь не "
                                           "найден",
                                     "uz": "{n}-qism ({cls}-klass): sugʻurta qildiruvchi va mukofot toʻlovchi kreditor "
                                           "bank ekanini tekshirish (kompaniya qoidasi); soʻrov va shartnomada sugʻurta "
                                           "qildiruvchi topilmadi",
                                     "en": "Part {n} (class {cls}): check that the policyholder and premium payer is the "
                                           "lending bank (company rule); no policyholder was found in the request or "
                                           "contract"},
    "credit_holder_individual": {"ru": "физическое лицо", "uz": "jismoniy shaxs", "en": "an individual"},
    "credit_holder_noname": {"ru": "организация не банк", "uz": "bank boʻlmagan tashkilot",
                             "en": "an organisation that is not a bank"},
    "credit_src_contract": {"ru": "договор страхования", "uz": "sugʻurta shartnomasi", "en": "insurance contract"},
    "credit_src_request": {"ru": "запрос филиала", "uz": "filial soʻrovi", "en": "branch request"},
    "c_part_missing": {"ru": "Часть {n} (класс {cls}): уточнить {what}",
                       "uz": "{n}-qism ({cls}-klass): aniqlashtirish — {what}",
                       "en": "Part {n} (class {cls}): clarify {what}"},
    # минимум класса части из текста тарифа продукта (act_engine.part_rate): не «ставка продукта»
    "how_part_base_text": {"ru": "Базовая ставка {base} — минимум класса {cls} по тарифной политике (из текста тарифа "
                                 "продукта {code})",
                           "uz": "Bazaviy tarif {base} — tarif siyosati boʻyicha {cls}-klass minimumi ({code} mahsuloti "
                                 "tarif matnidan)",
                           "en": "Base rate {base} — the class {cls} minimum under the tariff policy (from the tariff "
                                 "text of product {code})"},
    "how_part_min_ok_text": {"ru": "Ставка не ниже минимума класса {cls} по тарифной политике {min} (из текста тарифа "
                                   "продукта {code})",
                             "uz": "Tarif tarif siyosati boʻyicha {cls}-klass minimumi {min} dan past emas ({code} "
                                   "mahsuloti tarif matnidan)",
                             "en": "The rate is not below the class {cls} minimum under the tariff policy {min} (from "
                                   "the tariff text of product {code})"},
    "how_part_min_applied_text": {"ru": "Расчётная ставка {calc} ниже минимума — применён минимум класса {cls} по "
                                        "тарифной политике {min} (из текста тарифа продукта {code})",
                                  "uz": "Hisoblangan tarif {calc} minimumdan past — tarif siyosati boʻyicha {cls}-klass "
                                        "minimumi {min} qoʻllanildi ({code} mahsuloti tarif matnidan)",
                                  "en": "The calculated rate {calc} is below the minimum — the class {cls} minimum "
                                        "under the tariff policy {min} applied (from the tariff text of product "
                                        "{code})"},
    "an_t_min_class": {"ru": "Минимум класса {cls} по тарифной политике (из текста тарифа продукта {code})",
                       "uz": "Tarif siyosati boʻyicha {cls}-klass minimumi ({code} mahsuloti tarif matnidan)",
                       "en": "Class {cls} minimum under the tariff policy (from the tariff text of product {code})"},
    "an_t_policy_class": {"ru": "Ставка класса {cls} по тарифной политике (из текста тарифа продукта {code})",
                          "uz": "Tarif siyosati boʻyicha {cls}-klass tarifi ({code} mahsuloti tarif matnidan)",
                          "en": "Class {cls} rate under the tariff policy (from the tariff text of product {code})"},
    "an_t_act_min_class": {"ru": "применён минимум класса {cls}", "uz": "{cls}-klass minimumi qoʻllanildi",
                           "en": "class {cls} minimum applied"},
    "as_tpl_limit_case_over": {"ru": "лимит на один случай {limit} выше страховой суммы {sum} — взята страховая сумма",
                               "uz": "bitta hodisaga limit {limit} sugʻurta summasi {sum} dan yuqori — sugʻurta summasi "
                                     "olindi",
                               "en": "the per-occurrence limit {limit} is above the sum insured {sum} — the sum insured "
                                     "is used"},
    "as_tpl_limit_aggregate_over": {"ru": "годовой лимит {limit} выше страховой суммы {sum} — взята страховая сумма",
                                    "uz": "yillik limit {limit} sugʻurta summasi {sum} dan yuqori — sugʻurta summasi "
                                          "olindi",
                                    "en": "the aggregate limit {limit} is above the sum insured {sum} — the sum insured "
                                          "is used"},
}
