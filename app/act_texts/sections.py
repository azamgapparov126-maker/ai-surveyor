"""Разделы акта и первая страница: подписи полей, источников, видов объекта, уровни, скоринг.
Часть словаря текстов акта (app/act_texts): склеивается в TX в __init__.py в прежнем порядке."""

FIELD_LABELS = {
    "object_type": {"ru": "Тип объекта", "uz": "Obyekt turi", "en": "Object type"},
    "class_hint": {"ru": "Подсказка класса", "uz": "Klass boʻyicha taxmin", "en": "Class hint"},
    "brand": {"ru": "Марка", "uz": "Marka", "en": "Make"},
    "model": {"ru": "Модель", "uz": "Model", "en": "Model"},
    "manufacture_date": {"ru": "Дата изготовления", "uz": "Ishlab chiqarilgan sana", "en": "Date of manufacture"},
    "year": {"ru": "Год выпуска", "uz": "Ishlab chiqarilgan yil", "en": "Year of manufacture"},
    "serial_no": {"ru": "Заводской (серийный) номер", "uz": "Zavod (seriya) raqami", "en": "Serial number"},
    "manufacturer": {"ru": "Производитель", "uz": "Ishlab chiqaruvchi", "en": "Manufacturer"},
    "engine_no": {"ru": "Номер двигателя", "uz": "Dvigatel raqami", "en": "Engine number"},
    "engine_model": {"ru": "Модель двигателя", "uz": "Dvigatel modeli", "en": "Engine model"},
    "engine_power": {"ru": "Мощность двигателя", "uz": "Dvigatel quvvati", "en": "Engine power"},
    "curb_mass": {"ru": "Снаряжённая масса", "uz": "Jihozlangan massa", "en": "Curb weight"},
    "payload": {"ru": "Грузоподъёмность", "uz": "Yuk koʻtarish quvvati", "en": "Lifting capacity"},
    "dimensions": {"ru": "Габариты", "uz": "Gabarit oʻlchamlari", "en": "Dimensions"},
    "color": {"ru": "Цвет", "uz": "Rangi", "en": "Colour"},
    "mileage": {"ru": "Пробег / моточасы", "uz": "Yurgan masofa / motosoat", "en": "Mileage / engine hours"},
    "location": {"ru": "Место эксплуатации", "uz": "Foydalanish joyi", "en": "Place of operation"},
}

SOURCE_LABELS = {
    "photo": {"ru": "с фото объекта", "uz": "obyekt suratidan", "en": "from the object photo"},
    "plate": {"ru": "с заводской таблички", "uz": "zavod lavhachasidan", "en": "from the nameplate"},
    "document": {"ru": "из документа", "uz": "hujjatdan", "en": "from the document"},
    "marking": {"ru": "с маркировки на кузове", "uz": "korpusdagi yozuvdan", "en": "from the body marking"},
    "input": {"ru": "введено сотрудником", "uz": "xodim kiritgan", "en": "entered by staff"},
}

# в строке расхождения: «на табличке 36 170 кг, в документе 38 600 кг»
SOURCE_IN = {
    "photo": {"ru": "на фото", "uz": "suratda", "en": "on the photo"},
    "plate": {"ru": "на табличке", "uz": "lavhachada", "en": "on the nameplate"},
    "document": {"ru": "в документе", "uz": "hujjatda", "en": "in the document"},
    "marking": {"ru": "на маркировке", "uz": "yozuvda", "en": "on the marking"},
    "input": {"ru": "во вводе сотрудника", "uz": "xodim kiritganida", "en": "as entered by staff"},
}

VIEW_LABELS = {
    "front": {"ru": "спереди", "uz": "old tomondan", "en": "front"},
    "back": {"ru": "сзади", "uz": "orqa tomondan", "en": "rear"},
    "left": {"ru": "левый борт", "uz": "chap tomoni", "en": "left side"},
    "right": {"ru": "правый борт", "uz": "oʻng tomoni", "en": "right side"},
    "plate": {"ru": "заводская табличка", "uz": "zavod lavhachasi", "en": "nameplate"},
    "odometer": {"ru": "счётчик пробега или моточасов", "uz": "yurgan masofa yoki motosoat hisoblagichi",
                 "en": "odometer or hour meter"},
    "document": {"ru": "снимок документа", "uz": "hujjat surati", "en": "document image"},
    "interior": {"ru": "внутри", "uz": "ichki koʻrinish", "en": "interior"},
    "facade": {"ru": "фасад", "uz": "fasad", "en": "facade"},
    "roof": {"ru": "кровля", "uz": "tom", "en": "roof"},
    "electrical": {"ru": "электрощит", "uz": "elektr shiti", "en": "electrical panel"},
    "fire_safety": {"ru": "средства пожаротушения", "uz": "yongʻin oʻchirish vositalari", "en": "fire-fighting equipment"},
    "general": {"ru": "общий вид", "uz": "umumiy koʻrinish", "en": "general view"},
    "installation": {"ru": "место установки", "uz": "oʻrnatilgan joy", "en": "installation site"},
    "packaging": {"ru": "упаковка", "uz": "qadoq", "en": "packaging"},
    "marking": {"ru": "маркировка", "uz": "markirovka", "en": "marking"},
    "transport": {"ru": "транспорт", "uz": "transport vositasi", "en": "transport"},
    "other": {"ru": "другое", "uz": "boshqa", "en": "other"},
}

# ссылки на нормы: в данных хранится русское написание, в тексте — на языке акта
LEGAL_REFS = {
    "ГК РУз, ст. 936": {"ru": "ГК РУз, ст. 936", "uz": "OʻzR Fuqarolik kodeksi, 936-modda",
                        "en": "Civil Code of Uzbekistan, Art. 936"},
    "ГК РУз, ст. 938": {"ru": "ГК РУз, ст. 938", "uz": "OʻzR Fuqarolik kodeksi, 938-modda",
                        "en": "Civil Code of Uzbekistan, Art. 938"},
    "Положение № 1806, п. 15": {"ru": "Положение № 1806, п. 15", "uz": "1806-son Nizom, 15-band",
                                "en": "Regulation No. 1806, para. 15"},
    "Положение № 1806, п. 15, 16": {"ru": "Положение № 1806, п. 15, 16", "uz": "1806-son Nizom, 15, 16-bandlar",
                                    "en": "Regulation No. 1806, paras. 15, 16"},
    "ГК РУз, ст. 929": {"ru": "ГК РУз, ст. 929", "uz": "OʻzR Fuqarolik kodeksi, 929-modda",
                        "en": "Civil Code of Uzbekistan, Art. 929"},
}

# год объекта: у зданий и помещений — год постройки, у транспорта, техники и оборудования — год выпуска
YEAR_BUILT = {"ru": "Год постройки", "uz": "Qurilgan yili", "en": "Year built"}

YEAR_BUILT_GROUPS = ("property",)

LOCATION_LABELS = {
    "open_area": {"ru": "открытая площадка", "uz": "ochiq maydon", "en": "open yard"},
    "construction": {"ru": "строительная площадка", "uz": "qurilish maydoni", "en": "construction site"},
    "port": {"ru": "порт", "uz": "port", "en": "port"},
    "guarded": {"ru": "охраняемая территория", "uz": "qoʻriqlanadigan hudud", "en": "guarded area"},
    "closed_storage": {"ru": "закрытое хранение", "uz": "yopiq saqlash", "en": "indoor storage"},
    "other": {"ru": "другое место", "uz": "boshqa joy", "en": "other place"},
}

LEVEL_LABELS = {
    "low": {"ru": "низкий", "uz": "past", "en": "low"},
    "moderate": {"ru": "умеренный", "uz": "oʻrtacha", "en": "moderate"},
    "high": {"ru": "высокий", "uz": "yuqori", "en": "high"},
}

GROUP_LABELS = {
    "special": {"ru": "спецтехника", "uz": "maxsus texnika", "en": "special machinery"},
    "vehicle": {"ru": "транспорт", "uz": "transport", "en": "vehicle"},
    "property": {"ru": "здание, помещение", "uz": "bino, xona", "en": "building, premises"},
    "equipment": {"ru": "оборудование", "uz": "uskuna", "en": "equipment"},
    "cargo": {"ru": "груз", "uz": "yuk", "en": "cargo"},
    "liability": {"ru": "ответственность", "uz": "javobgarlik", "en": "liability"},
    "other": {"ru": "прочее", "uz": "boshqa", "en": "other"},
}

# вид объекта (подсказка модели) → тип объекта справочника базовых ставок; подписи — на трёх языках
OBJECT_KINDS = {
    "truck_crane": ("Спецтехника — автокран", {"ru": "автокран", "uz": "avtokran", "en": "truck crane"}),
    "crawler_crane": ("Спецтехника — кран гусеничный", {"ru": "гусеничный кран", "uz": "gusenitsali kran",
                                                        "en": "crawler crane"}),
    "excavator": ("Спецтехника — экскаватор", {"ru": "экскаватор", "uz": "ekskavator", "en": "excavator"}),
    "backhoe_loader": ("Спецтехника — экскаватор-погрузчик", {"ru": "экскаватор-погрузчик",
                                                              "uz": "ekskavator-yuklagich", "en": "backhoe loader"}),
    "bulldozer": ("Спецтехника — бульдозер", {"ru": "бульдозер", "uz": "buldozer", "en": "bulldozer"}),
    "wheel_loader": ("Спецтехника — погрузчик фронтальный", {"ru": "фронтальный погрузчик",
                                                             "uz": "frontal yuklagich", "en": "wheel loader"}),
    "forklift": ("Спецтехника — погрузчик вилочный", {"ru": "вилочный погрузчик", "uz": "vilkali yuklagich",
                                                      "en": "forklift"}),
    "telehandler": ("Спецтехника — телескопический погрузчик", {"ru": "телескопический погрузчик",
                                                                "uz": "teleskopik yuklagich", "en": "telehandler"}),
    "aerial_platform": ("Спецтехника — автовышка", {"ru": "автовышка", "uz": "avtominora",
                                                    "en": "aerial work platform"}),
    "concrete_pump": ("Спецтехника — бетононасос", {"ru": "бетононасос", "uz": "beton nasos",
                                                    "en": "concrete pump"}),
    "concrete_mixer": ("Спецтехника — автобетоносмеситель", {"ru": "автобетоносмеситель",
                                                             "uz": "avtobetonaralashtirgich",
                                                             "en": "truck mixer"}),
    "drilling_rig": ("Спецтехника — буровая установка", {"ru": "буровая установка", "uz": "burgʻulash qurilmasi",
                                                         "en": "drilling rig"}),
    "road_machinery": ("Спецтехника — дорожная (каток, грейдер, асфальтоукладчик)",
                       {"ru": "дорожная техника", "uz": "yoʻl texnikasi", "en": "road machinery"}),
    "tractor": ("Спецтехника — трактор", {"ru": "трактор", "uz": "traktor", "en": "tractor"}),
    "combine": ("Спецтехника — комбайн", {"ru": "комбайн", "uz": "kombayn", "en": "combine harvester"}),
    "special_other": ("Спецтехника", {"ru": "спецтехника", "uz": "maxsus texnika", "en": "special machinery"}),
    "trailer": ("Прицеп и полуприцеп", {"ru": "прицеп", "uz": "tirkama", "en": "trailer"}),
    "car": ("Легковой", {"ru": "легковой автомобиль", "uz": "yengil avtomobil", "en": "passenger car"}),
    "truck": ("Грузовой", {"ru": "грузовой автомобиль", "uz": "yuk avtomobili", "en": "truck"}),
    "electric_car": ("Электромобиль", {"ru": "электромобиль", "uz": "elektromobil", "en": "electric car"}),
    "warehouse": ("Склад", {"ru": "склад", "uz": "ombor", "en": "warehouse"}),
    "shop": ("Магазин", {"ru": "магазин", "uz": "doʻkon", "en": "shop"}),
    "production": ("Производство", {"ru": "производственное здание", "uz": "ishlab chiqarish binosi",
                                    "en": "production building"}),
    "office": ("Офис", {"ru": "офис", "uz": "ofis", "en": "office"}),
    "dwelling": ("Жильё", {"ru": "жильё", "uz": "turar joy", "en": "dwelling"}),
    "hotel": ("Гостиница", {"ru": "гостиница", "uz": "mehmonxona", "en": "hotel"}),
    "equipment": ("Машины и оборудование", {"ru": "машины и оборудование", "uz": "mashina va uskunalar",
                                            "en": "machinery and equipment"}),
    "cargo": (None, {"ru": "груз", "uz": "yuk", "en": "cargo"}),
    "other": (None, {"ru": "прочее", "uz": "boshqa", "en": "other"}),
}

FIELD_LABELS.update({
    "sum_insured": {"ru": "Страховая сумма", "uz": "Sugʻurta summasi", "en": "Sum insured"},
    "object_value": {"ru": "Стоимость объекта", "uz": "Obyekt qiymati", "en": "Object value"},
    "term_days": {"ru": "Срок страхования, дней", "uz": "Sugʻurta muddati, kun", "en": "Term, days"},
    "region": {"ru": "Регион", "uz": "Hudud", "en": "Region"},
    "construction": {"ru": "Конструкция, материал стен", "uz": "Konstruksiya, devor materiali",
                     "en": "Construction, wall material"},
    "reg_no": {"ru": "Государственный номер", "uz": "Davlat raqami", "en": "Registration plate"},
    "cadastre_no": {"ru": "Кадастровый номер", "uz": "Kadastr raqami", "en": "Cadastral number"},
})

# ================================================================================================
#  Запрос филиала и сверка с ним (30.09.2026)
# ================================================================================================
FIELD_LABELS.update({
    "product_code": {"ru": "Код вида страхования", "uz": "Sugʻurta turi kodi", "en": "Insurance product code"},
    "policyholder": {"ru": "Страхователь", "uz": "Sugʻurta qildiruvchi", "en": "Policyholder"},
    "beneficiary": {"ru": "Выгодоприобретатель", "uz": "Naf oluvchi", "en": "Beneficiary"},
    "pledger": {"ru": "Залогодатель", "uz": "Garovga qoʻyuvchi", "en": "Pledgor"},
    "land_area": {"ru": "Площадь земельного участка", "uz": "Yer uchastkasi maydoni", "en": "Land plot area"},
    "useful_area": {"ru": "Полезная площадь", "uz": "Foydali maydon", "en": "Usable area"},
    "total_area": {"ru": "Общая площадь", "uz": "Umumiy maydon", "en": "Total area"},
    "franchise": {"ru": "Франшиза (в запросе)", "uz": "Franshiza (soʻrovda)", "en": "Deductible (in the request)"},
    "tariff_pct": {"ru": "Тариф в запросе, % в год", "uz": "Soʻrovdagi tarif, yillik %",
                   "en": "Rate in the request, % per year"},
    "premium": {"ru": "Страховая премия (в запросе)", "uz": "Sugʻurta mukofoti (soʻrovda)",
                "en": "Premium (in the request)"},
    "term_from": {"ru": "Срок: с", "uz": "Muddat: dan", "en": "Term: from"},
    "term_to": {"ru": "Срок: по", "uz": "Muddat: gacha", "en": "Term: to"},
    "contract_terms": {"ru": "Изменения стандартных условий", "uz": "Standart shartlarga oʻzgartirishlar",
                       "en": "Changes to standard terms"},
    "contracts_count": {"ru": "Число договоров", "uz": "Shartnomalar soni", "en": "Number of contracts"},
    "additional_info": {"ru": "Дополнительные сведения", "uz": "Qoʻshimcha maʼlumot", "en": "Additional information"},
})

FIELD_LABELS["insurer"] = {"ru": "Страховщик", "uz": "Sugʻurtalovchi", "en": "Insurer"}

SOURCE_LABELS["document_ai"] = {"ru": "из текста договора, прочитано моделью", "uz": "shartnoma matnidan, model oʻqigan",
                                "en": "from the contract text, read by the model"}

SOURCE_IN["document_ai"] = {"ru": "в тексте договора (по модели)", "uz": "shartnoma matnida (model boʻyicha)",
                            "en": "in the contract text (per the model)"}

# ================================================================================================
#  Страховой скоринг объекта (01.10.2026): первая страница акта, шкала 0–500
# ================================================================================================
SCORE_BAND_LABELS = {
    "poor": {"ru": "плохой", "uz": "yomon", "en": "poor"},
    "weak": {"ru": "слабый", "uz": "zaif", "en": "weak"},
    "fair": {"ru": "средний", "uz": "oʻrtacha", "en": "fair"},
    "good": {"ru": "хороший", "uz": "yaxshi", "en": "good"},
    "excellent": {"ru": "отличный", "uz": "aʼlo", "en": "excellent"},
}

# типы объекта справочника базовых ставок и умолчания risk_analytics без вида объекта на экране (02.10.2026):
# в снимке акта — по-русски, в акте на узбекском и английском — по этой таблице
OTYPE_LABELS = {
    "Иное имущество": {"ru": "Иное имущество", "uz": "Boshqa mol-mulk", "en": "Other property"},
    "Воздушное судно": {"ru": "Воздушное судно", "uz": "Havo kemasi", "en": "Aircraft"},
    "Гарантия": {"ru": "Гарантия", "uz": "Kafolat", "en": "Guarantee"},
    "Груз": {"ru": "Груз", "uz": "Yuk", "en": "Cargo"},
    "Кредит": {"ru": "Кредит", "uz": "Kredit", "en": "Loan"},
    "Микрозайм": {"ru": "Микрозайм", "uz": "Mikroqarz", "en": "Microloan"},
    "Морское судно": {"ru": "Морское судно", "uz": "Dengiz kemasi", "en": "Sea vessel"},
    "Общегражданская ответственность": {"ru": "Общегражданская ответственность", "uz": "Umumfuqarolik javobgarligi",
                                        "en": "General civil liability"},
    "Опасный производственный объект": {"ru": "Опасный производственный объект", "uz": "Xavfli ishlab chiqarish obyekti",
                                        "en": "Hazardous production facility"},
    "Ответственность": {"ru": "Ответственность", "uz": "Javobgarlik", "en": "Liability"},
    "Ответственность авиаперевозчика": {"ru": "Ответственность авиаперевозчика",
                                        "uz": "Havo tashuvchisining javobgarligi", "en": "Air carrier liability"},
    "Ответственность владельца ТС": {"ru": "Ответственность владельца ТС", "uz": "TV egasining javobgarligi",
                                     "en": "Motor vehicle owner liability"},
    "Ответственность заёмщика": {"ru": "Ответственность заёмщика", "uz": "Qarz oluvchining javobgarligi",
                                 "en": "Borrower liability"},
    "Ответственность морского перевозчика": {"ru": "Ответственность морского перевозчика",
                                             "uz": "Dengiz tashuvchisining javobgarligi", "en": "Sea carrier liability"},
    "Подвижной состав": {"ru": "Подвижной состав", "uz": "Harakatlanuvchi tarkib", "en": "Rolling stock"},
    "Правовая защита": {"ru": "Правовая защита", "uz": "Huquqiy himoya", "en": "Legal protection"},
    "Профессиональная ответственность": {"ru": "Профессиональная ответственность", "uz": "Kasbiy javobgarlik",
                                         "en": "Professional liability"},
    "Финансовый риск": {"ru": "Финансовый риск", "uz": "Moliyaviy xavf", "en": "Financial risk"},
    "Человек": {"ru": "Человек", "uz": "Inson", "en": "Person"},
}

TX_SECTIONS = {
    # ---------- шапка и подвал ----------
    "title": {"ru": "СЮРВЕЙЕРСКИЙ АКТ ПРЕДСТРАХОВОГО ОСМОТРА",
              "uz": "SUGʻURTADAN OLDINGI KOʻZDAN KECHIRISH BOʻYICHA SYURVEYER DALOLATNOMASI",
              "en": "SURVEYOR'S PRE-INSURANCE INSPECTION REPORT"},
    "insurer": {"ru": "Страховщик", "uz": "Sugʻurtalovchi", "en": "Insurer"},
    "date": {"ru": "Дата", "uz": "Sana", "en": "Date"},
    "number": {"ru": "Номер акта", "uz": "Dalolatnoma raqami", "en": "Report No."},
    "footer": {"ru": "Акт сформирован ИИ-сюрвейером, подлежит подтверждению андеррайтером",
               "uz": "Dalolatnoma sunʼiy intellekt syurveyeri tomonidan tuzilgan, anderrayter tasdigʻini talab qiladi",
               "en": "The report was generated by the AI surveyor and is subject to underwriter confirmation"},
    "page": {"ru": "Страница {i} из {n}", "uz": "{i}-sahifa, jami {n}", "en": "Page {i} of {n}"},
    "na": {"ru": "данные недоступны", "uz": "maʼlumot mavjud emas", "en": "data not available"},
    "no_inspection": {"ru": "осмотр не проводился", "uz": "koʻzdan kechirish oʻtkazilmagan",
                      "en": "inspection not carried out"},
    "check_mark": {"ru": "проверьте", "uz": "tekshiring", "en": "please check"},
    "uncalibrated": {"ru": "не калибровано", "uz": "kalibrlanmagan", "en": "not calibrated"},
    "expert": {"ru": "экспертная оговорка", "uz": "ekspert izohi", "en": "expert clause"},

    # ---------- разделы ----------
    "s1": {"ru": "Объект и идентификация", "uz": "Obyekt va uni identifikatsiya qilish",
           "en": "Object and identification"},
    "s2": {"ru": "Результаты осмотра", "uz": "Koʻzdan kechirish natijalari", "en": "Inspection results"},
    "s3": {"ru": "Стоимость и страховая сумма", "uz": "Qiymat va sugʻurta summasi", "en": "Value and sum insured"},
    "s4": {"ru": "Риск-факторы и франшиза", "uz": "Xavf omillari va franshiza", "en": "Risk factors and deductible"},
    "s5": {"ru": "Заключение и рекомендация", "uz": "Xulosa va tavsiya", "en": "Conclusion and recommendation"},

    # ---------- раздел 1 ----------
    "class": {"ru": "Класс страхования", "uz": "Sugʻurta klassi", "en": "Insurance class"},
    "product": {"ru": "Продукт", "uz": "Mahsulot", "en": "Product"},
    "region": {"ru": "Регион", "uz": "Hudud", "en": "Region"},
    "guard": {"ru": "Охрана", "uz": "Qoʻriqlash", "en": "Security"},
    "yes": {"ru": "есть", "uz": "bor", "en": "yes"},
    "no": {"ru": "нет", "uz": "yoʻq", "en": "no"},
    "also_in": {"ru": "также: {what}", "uz": "shuningdek: {what}", "en": "also: {what}"},

    # ---------- раздел 2 ----------
    "views_seen": {"ru": "Осмотрено по фото", "uz": "Suratlar boʻyicha koʻrilgan",
                   "en": "Inspected on photos"},
    "views_missing": {"ru": "Не хватает ракурсов", "uz": "Yetishmayotgan rakurslar", "en": "Missing views"},
    "views_all": {"ru": "все нужные ракурсы представлены", "uz": "barcha kerakli rakurslar taqdim etilgan",
                  "en": "all required views provided"},
    "views_none_needed": {"ru": "для этого класса ракурсы не требуются",
                          "uz": "bu klass uchun rakurslar talab qilinmaydi",
                          "en": "no views are required for this class"},
    "damages": {"ru": "Видимые повреждения", "uz": "Koʻrinadigan shikastlar", "en": "Visible damage"},
    "damages_none": {"ru": "на представленных фото не обнаружены",
                     "uz": "taqdim etilgan suratlarda aniqlanmadi",
                     "en": "none found on the photos provided"},
    "documents": {"ru": "Документы на объект", "uz": "Obyekt hujjatlari", "en": "Object documents"},
    "docs_given": {"ru": "представлены", "uz": "taqdim etilgan", "en": "provided"},
    "docs_not_given": {"ru": "не представлены", "uz": "taqdim etilmagan", "en": "not provided"},
    "files_count": {"ru": "Загружено файлов", "uz": "Yuklangan fayllar", "en": "Files uploaded"},
    "p_no_photos": {"ru": "Осмотр не проводился: фотографии объекта не загружены. Разделы об осмотре заполнены "
                          "только по вводу сотрудника.",
                    "uz": "Koʻzdan kechirish oʻtkazilmagan: obyekt suratlari yuklanmagan. Koʻzdan kechirish "
                          "boʻlimlari faqat xodim kiritgan maʼlumotlar asosida toʻldirilgan.",
                    "en": "Inspection not carried out: no photos of the object were uploaded. The inspection "
                          "sections are based only on the data entered by staff."},
    "p_ai_failed": {"ru": "Фотографии загружены (файлов: {n}), но распознать их не удалось: {reason}. Осмотр по фото "
                          "не проводился — объект нужно проверить вручную.",
                    "uz": "Suratlar yuklandi (fayllar: {n}), lekin ularni aniqlab boʻlmadi: {reason}. Suratlar boʻyicha "
                          "koʻzdan kechirish oʻtkazilmagan — obyektni qoʻlda tekshirish kerak.",
                    "en": "Photos were uploaded (files: {n}) but could not be recognised: {reason}. No photo inspection "
                          "was carried out — the object must be checked manually."},
    "p_inspected": {"ru": "Осмотр проведён по фотографиям и снимкам документов (файлов: {n}). Распознанные "
                          "значения требуют проверки по оригиналам.",
                    "uz": "Koʻzdan kechirish suratlar va hujjat suratlari boʻyicha oʻtkazildi (fayllar: {n}). "
                          "Aniqlangan qiymatlar asl nusxalar bilan tekshirilishi kerak.",
                    "en": "The inspection was carried out on photos and document images (files: {n}). Recognised "
                          "values must be checked against the originals."},

    # ---------- раздел 3 ----------
    "sum_insured": {"ru": "Страховая сумма", "uz": "Sugʻurta summasi", "en": "Sum insured"},
    "object_value": {"ru": "Стоимость объекта", "uz": "Obyekt qiymati", "en": "Object value"},
    "ratio": {"ru": "Отношение суммы к стоимости", "uz": "Summaning qiymatga nisbati",
              "en": "Sum insured to value"},
    "verdict": {"ru": "Вывод", "uz": "Xulosa", "en": "Conclusion"},
    "v_normal": {"ru": "В норме: страховая сумма составляет {ratio} стоимости объекта.",
                 "uz": "Meʼyorda: sugʻurta summasi obyekt qiymatining {ratio} qismini tashkil etadi.",
                 "en": "Within norm: the sum insured is {ratio} of the object value."},
    "v_under": {"ru": "Недострахование: страховая сумма составляет {ratio} стоимости, выплата будет "
                      "пропорциональной — в той же доле от ущерба ({ref}).",
                "uz": "Toʻliq boʻlmagan mulkiy sugʻurta: sugʻurta summasi qiymatning {ratio} qismini tashkil "
                      "etadi, toʻlov zararning xuddi shu ulushida mutanosib boʻladi ({ref}).",
                "en": "Underinsurance: the sum insured is {ratio} of the value; claims will be paid "
                      "proportionally, in the same share of the loss ({ref})."},
    "v_under_small": {"ru": "Страховая сумма ниже стоимости ({ratio}) в пределах допустимого; при убытке выплата "
                            "будет в той же доле ({ref}).",
                      "uz": "Sugʻurta summasi qiymatdan past ({ratio}), lekin ruxsat etilgan chegarada; zarar "
                            "yuz berganda toʻlov xuddi shu ulushda boʻladi ({ref}).",
                      "en": "The sum insured is below the value ({ratio}) but within the acceptable range; "
                            "a claim will be paid in the same share ({ref})."},
    "v_over": {"ru": "Превышение: страховая сумма выше стоимости на {diff}. Сумму нужно снизить до стоимости — "
                     "в части превышения договор недействителен ({ref}).",
               "uz": "Oshib ketish: sugʻurta summasi qiymatdan {diff} ga yuqori. Summani qiymatgacha kamaytirish "
                     "kerak — oshgan qismda shartnoma haqiqiy emas ({ref}).",
               "en": "Over-insurance: the sum insured exceeds the value by {diff}. The sum must be reduced to "
                     "the value — the contract is void in the excess part ({ref})."},
    "depreciated": {"ru": "Стоимость с учётом износа (ориентир)", "uz": "Eskirishni hisobga olgan qiymat (moʻljal)",
                    "en": "Depreciated value (guide)"},
    "depr_how": {"ru": "цена {price} минус износ {rate} в год × {years} лет",
                 "uz": "narx {price}, yiliga {rate} eskirish × {years} yil",
                 "en": "price {price} less wear {rate} per year × {years} years"},
    "depr_note": {"ru": "Ориентир, а не оценка: стоимость объекта вводит сотрудник.",
                  "uz": "Bu baholash emas, faqat moʻljal: obyekt qiymatini xodim kiritadi.",
                  "en": "A guide, not a valuation: the object value is entered by staff."},

    # ---------- раздел 4 ----------
    "level": {"ru": "Уровень риска", "uz": "Xavf darajasi", "en": "Risk level"},
    "factors": {"ru": "Признаки", "uz": "Belgilar", "en": "Indicators"},
    "base_rate": {"ru": "Базовая ставка", "uz": "Bazaviy tarif", "en": "Base rate"},
    "adj": {"ru": "Поправка по уровню риска", "uz": "Xavf darajasi boʻyicha tuzatish", "en": "Risk level loading"},
    "applied_rate": {"ru": "Рекомендуемый тариф", "uz": "Tavsiya etilgan tarif", "en": "Recommended rate"},
    "min_rate": {"ru": "Минимальная ставка продукта", "uz": "Mahsulotning eng kam tarifi",
                 "en": "Product minimum rate"},
    "premium": {"ru": "Страховая премия", "uz": "Sugʻurta mukofoti", "en": "Premium"},
    "premium_term": {"ru": "за {days} дн.", "uz": "{days} kun uchun", "en": "for {days} days"},
    "franchise": {"ru": "Франшиза", "uz": "Franshiza", "en": "Deductible"},
    "clauses": {"ru": "Оговорки", "uz": "Izohlar (shartlar)", "en": "Clauses"},
    "rate_undefined": {"ru": "ставка не определена — нужен расчёт андеррайтера",
                       "uz": "tarif aniqlanmagan — anderrayter hisobi kerak",
                       "en": "rate not determined — underwriter calculation required"},
    "rate_by_act": {"ru": "по нормативному акту, без поправок", "uz": "meʼyoriy hujjat boʻyicha, tuzatishsiz",
                    "en": "per the statutory act, no loadings"},
    "adj_none_statutory": {"ru": "не применяется (обязательный вид)", "uz": "qoʻllanilmaydi (majburiy tur)",
                           "en": "not applied (compulsory class)"},
    "min_applied": {"ru": "применена минимальная ставка продукта", "uz": "mahsulotning eng kam tarifi qoʻllanildi",
                    "en": "product minimum rate applied"},

    # ---------- уровень риска: признаки ----------
    "f_cond_damage": {"ru": "Состояние объекта: видимые повреждения ({n}) — повышает риск",
                      "uz": "Obyekt holati: koʻrinadigan shikastlar ({n}) — xavfni oshiradi",
                      "en": "Condition: visible damage ({n}) — increases risk"},
    "f_cond_damage_minor": {"ru": "Состояние объекта: косметические повреждения ({n}) — слабо повышает риск "
                                  "(вес 0,5, экспертно)",
                            "uz": "Obyekt holati: kosmetik shikastlar ({n}) — xavfni biroz oshiradi "
                                  "(vazni 0,5, ekspert baho)",
                            "en": "Condition: cosmetic damage ({n}) — slightly increases risk (weight 0.5, expert)"},
    "f_cond_worn": {"ru": "Состояние объекта: признаки износа — повышает риск",
                    "uz": "Obyekt holati: eskirish belgilari — xavfni oshiradi",
                    "en": "Condition: signs of wear — increases risk"},
    "f_cond_new": {"ru": "Состояние объекта: объект новый ({year}), повреждений на фото нет — снижает риск",
                   "uz": "Obyekt holati: obyekt yangi ({year}), suratlarda shikast yoʻq — xavfni kamaytiradi",
                   "en": "Condition: new object ({year}), no damage on photos — reduces risk"},
    "f_cond_neutral": {"ru": "Состояние объекта: повреждений на фото нет, объект не новый — не влияет",
                       "uz": "Obyekt holati: suratlarda shikast yoʻq, obyekt yangi emas — taʼsir qilmaydi",
                       "en": "Condition: no damage on photos, object not new — no effect"},
    "f_cond_no_year": {"ru": "Состояние объекта: повреждений на фото нет, год выпуска неизвестен — не влияет",
                       "uz": "Obyekt holati: suratlarda shikast yoʻq, ishlab chiqarilgan yili nomaʼlum — taʼsir qilmaydi",
                       "en": "Condition: no damage on photos, year of manufacture unknown — no effect"},
    "f_cond_unknown": {"ru": "Состояние объекта: не оценено — нет фото и сведений",
                       "uz": "Obyekt holati: baholanmagan — surat va maʼlumot yoʻq",
                       "en": "Condition: not assessed — no photos or data"},
    "f_loc_up": {"ru": "Место эксплуатации: {place} — повышает риск",
                 "uz": "Foydalanish joyi: {place} — xavfni oshiradi",
                 "en": "Place of operation: {place} — increases risk"},
    "f_loc_down": {"ru": "Место эксплуатации: {place} — снижает риск",
                   "uz": "Foydalanish joyi: {place} — xavfni kamaytiradi",
                   "en": "Place of operation: {place} — reduces risk"},
    "f_loc_guarded_open": {"ru": "Место эксплуатации: {place}, но под охраной — не влияет",
                           "uz": "Foydalanish joyi: {place}, lekin qoʻriqlanadi — taʼsir qilmaydi",
                           "en": "Place of operation: {place}, but guarded — no effect"},
    "f_loc_neutral": {"ru": "Место эксплуатации: {place} — не влияет",
                      "uz": "Foydalanish joyi: {place} — taʼsir qilmaydi",
                      "en": "Place of operation: {place} — no effect"},
    "f_loc_unknown": {"ru": "Место эксплуатации: не указано", "uz": "Foydalanish joyi: koʻrsatilmagan",
                      "en": "Place of operation: not stated"},
    "f_loss_up": {"ru": "История убытков: {n} за три года — повышает риск",
                  "uz": "Zararlar tarixi: uch yilda {n} ta — xavfni oshiradi",
                  "en": "Loss history: {n} in three years — increases risk"},
    "f_loss_down": {"ru": "История убытков: убытков за три года нет — снижает риск",
                    "uz": "Zararlar tarixi: uch yilda zarar boʻlmagan — xavfni kamaytiradi",
                    "en": "Loss history: no losses in three years — reduces risk"},
    "f_loss_neutral": {"ru": "История убытков: {n} за три года — не влияет",
                       "uz": "Zararlar tarixi: uch yilda {n} ta — taʼsir qilmaydi",
                       "en": "Loss history: {n} in three years — no effect"},
    "f_loss_unknown": {"ru": "История убытков: сведений нет", "uz": "Zararlar tarixi: maʼlumot yoʻq",
                       "en": "Loss history: no data"},
    "f_docs_down": {"ru": "Документы на объект представлены — снижает риск",
                    "uz": "Obyekt hujjatlari taqdim etilgan — xavfni kamaytiradi",
                    "en": "Object documents provided — reduces risk"},
    "f_docs_up": {"ru": "Документы на объект не представлены — повышает риск",
                  "uz": "Obyekt hujjatlari taqdim etilmagan — xavfni oshiradi",
                  "en": "Object documents not provided — increases risk"},
    "level_rule": {"ru": "Правило: признак, повышающий риск, даёт +1, снижающий — −1. Сумма {net}: не больше "
                         "{low} — низкий уровень, не меньше {high} — высокий, иначе умеренный. Если известно "
                         "меньше {k} признаков — умеренный. Пороги экспертные, не калибровано.",
                   "uz": "Qoida: xavfni oshiruvchi belgi +1, kamaytiruvchi −1 beradi. Yigʻindi {net}: {low} dan "
                         "oshmasa — past daraja, {high} dan kam boʻlmasa — yuqori, aks holda oʻrtacha. Agar "
                         "{k} tadan kam belgi maʼlum boʻlsa — oʻrtacha. Chegaralar ekspert tomonidan belgilangan, "
                         "kalibrlanmagan.",
                   "en": "Rule: an indicator that increases risk counts +1, one that reduces it −1. Total {net}: "
                         "{low} or less — low, {high} or more — high, otherwise moderate. If fewer than {k} "
                         "indicators are known — moderate. Expert thresholds, not calibrated."},
    "level_few": {"ru": "Известно мало признаков — принят умеренный уровень",
                  "uz": "Belgilar kam maʼlum — oʻrtacha daraja qabul qilindi",
                  "en": "Few indicators known — moderate level assumed"},

    # ---------- ставка: как посчитана ----------
    "how_title": {"ru": "Как посчитан тариф", "uz": "Tarif qanday hisoblangan", "en": "How the rate was calculated"},
    "how_base_tech": {"ru": "Базовая ставка {base} — техническая ставка калькулятора по классу {cls}, тип «{otype}» "
                            "(базовые ставки и нагрузка из справочника, экспертные значения)",
                      "uz": "Bazaviy tarif {base} — kalkulyatorning {cls}-klass, «{otype}» turi boʻyicha texnik "
                            "tarifi (maʼlumotnomadagi bazaviy tariflar va yuklama, ekspert qiymatlari)",
                      "en": "Base rate {base} — the calculator's technical rate for class {cls}, type «{otype}» "
                            "(base rates and loading from the reference, expert values)"},
    "how_base_tech_avg": {"ru": "Базовая ставка {base} — техническая ставка калькулятора по классу {cls}; тип объекта "
                                "не определён, взята средняя базовая ставка класса",
                          "uz": "Bazaviy tarif {base} — kalkulyatorning {cls}-klass boʻyicha texnik tarifi; obyekt turi "
                                "aniqlanmagan, klassning oʻrtacha bazaviy tarifi olindi",
                          "en": "Base rate {base} — the calculator's technical rate for class {cls}; the object type "
                                "was not determined, the class average base rate was used"},
    "how_base_product": {"ru": "Базовая ставка {base} — ставка продукта {code} по тарифной политике",
                         "uz": "Bazaviy tarif {base} — tarif siyosati boʻyicha {code} mahsuloti tarifi",
                         "en": "Base rate {base} — product {code} rate under the tariff policy"},
    "how_adj": {"ru": "Поправка по уровню риска «{level}»: +{adj} (не калибровано)",
                "uz": "«{level}» xavf darajasi boʻyicha tuzatish: +{adj} (kalibrlanmagan)",
                "en": "Risk level «{level}» loading: +{adj} (not calibrated)"},
    "how_min_ok": {"ru": "Ставка не ниже минимальной ставки продукта {min}",
                   "uz": "Tarif mahsulotning eng kam tarifi {min} dan past emas",
                   "en": "The rate is not below the product minimum {min}"},
    "how_min_applied": {"ru": "Расчётная ставка {calc} ниже минимальной — применена минимальная ставка продукта {min}",
                        "uz": "Hisoblangan tarif {calc} eng kamidan past — mahsulotning eng kam tarifi {min} qoʻllanildi",
                        "en": "The calculated rate {calc} is below the minimum — the product minimum {min} applied"},
    "how_min_none": {"ru": "Минимальная ставка продукта в справочнике не задана",
                     "uz": "Mahsulotning eng kam tarifi maʼlumotnomada belgilanmagan",
                     "en": "No product minimum rate in the reference"},
    "how_premium": {"ru": "Премия = {sum} × {rate} × {days} / 365 = {premium}",
                    "uz": "Mukofot = {sum} × {rate} × {days} / 365 = {premium}",
                    "en": "Premium = {sum} × {rate} × {days} / 365 = {premium}"},
    "how_undefined": {"ru": "У продукта {code} ставка «{rate_text}» — числа в справочнике нет, ставку определяет "
                            "андеррайтер. Цифры не подставлялись.",
                      "uz": "{code} mahsulotining tarifi «{rate_text}» — maʼlumotnomada son yoʻq, tarifni "
                            "anderrayter belgilaydi. Raqamlar qoʻyilmadi.",
                      "en": "Product {code} rate is «{rate_text}» — there is no number in the reference, the rate "
                            "is set by the underwriter. No figures were substituted."},
    "how_no_base": {"ru": "В справочнике нет базовой ставки для класса {cls} — ставку определяет андеррайтер",
                    "uz": "Maʼlumotnomada {cls}-klass uchun bazaviy tarif yoʻq — tarifni anderrayter belgilaydi",
                    "en": "No base rate for class {cls} in the reference — the rate is set by the underwriter"},
    "how_statutory": {"ru": "Обязательный вид: ставка {rate} по нормативному акту ({ref}), без поправок",
                      "uz": "Majburiy tur: tarif {rate} meʼyoriy hujjat boʻyicha ({ref}), tuzatishsiz",
                      "en": "Compulsory class: rate {rate} per the statutory act ({ref}), no loadings"},
    "how_statutory_na": {"ru": "Обязательный вид: ставка устанавливается нормативным актом ({ref}) и зависит от "
                               "условий акта — рассчитайте её в разделе «Обязательные виды»",
                         "uz": "Majburiy tur: tarif meʼyoriy hujjat ({ref}) bilan belgilanadi va uning shartlariga "
                               "bogʻliq — uni «Majburiy turlar» boʻlimida hisoblang",
                         "en": "Compulsory class: the rate is set by the statutory act ({ref}) and depends on its "
                               "terms — calculate it in the «Compulsory classes» section"},

    # ---------- франшиза ----------
    "fr_not_needed": {"ru": "Франшиза не требуется", "uz": "Franshiza talab qilinmaydi",
                      "en": "No deductible required"},
    "fr_statutory": {"ru": "Франшиза не применяется: обязательный вид страхования, условия по нормативному акту",
                     "uz": "Franshiza qoʻllanilmaydi: majburiy sugʻurta turi, shartlar meʼyoriy hujjat boʻyicha",
                     "en": "No deductible: compulsory class, terms per the statutory act"},
    "fr_advise_range": {"ru": "Рекомендуется рассмотреть безусловную франшизу {range} страховой суммы ({amount}). "
                              "Точный размер определяет андеррайтер.",
                        "uz": "Sugʻurta summasining {range} miqdorida shartsiz franshizani koʻrib chiqish tavsiya "
                              "etiladi ({amount}). Aniq miqdorni anderrayter belgilaydi.",
                        "en": "An unconditional deductible of {range} of the sum insured ({amount}) is recommended "
                              "for consideration. The exact amount is set by the underwriter."},
    "fr_advise_nosize": {"ru": "Рекомендуется рассмотреть франшизу, размер определяет андеррайтер",
                         "uz": "Franshizani koʻrib chiqish tavsiya etiladi, miqdorini anderrayter belgilaydi",
                         "en": "A deductible is recommended for consideration; the amount is set by the underwriter"},
    "fr_range_upto": {"ru": "до {to}", "uz": "{to} gacha", "en": "up to {to}"},
    "fr_range": {"ru": "от {from_} до {to}", "uz": "{from_} dan {to} gacha", "en": "from {from_} to {to}"},
    "fr_grounds": {"ru": "Основания", "uz": "Asoslar", "en": "Grounds"},
    "fr_g_small_losses": {"ru": "{n} мелких убытков за три года", "uz": "uch yilda {n} ta kichik zarar",
                          "en": "{n} small losses in three years"},
    "fr_g_level_high": {"ru": "высокий уровень риска", "uz": "yuqori xavf darajasi", "en": "high risk level"},
    "fr_g_dominant": {"ru": "явно преобладает один риск: {what}", "uz": "bitta xavf aniq ustun: {what}",
                      "en": "one risk clearly dominates: {what}"},
    "fr_g_client": {"ru": "клиент просит снизить премию", "uz": "mijoz mukofotni kamaytirishni soʻraydi",
                    "en": "the client asks to lower the premium"},
    "fr_none_grounds": {"ru": "оснований нет: мелких убытков меньше двух, уровень не высокий, преобладающего риска и "
                              "просьбы клиента нет",
                        "uz": "asoslar yoʻq: kichik zararlar ikkitadan kam, daraja yuqori emas, ustun xavf va mijoz "
                              "iltimosi yoʻq",
                        "en": "no grounds: fewer than two small losses, level not high, no dominant risk and no "
                              "client request"},
    "fr_alt": {"ru": "Вместо франшизы можно ограничиться оговорками ниже.",
               "uz": "Franshiza oʻrniga quyidagi izohlar bilan cheklanish mumkin.",
               "en": "Instead of a deductible, the clauses below may be used."},

    # ---------- раздел 5 ----------
    "d_accept": {"ru": "Рекомендация: принять объект на страхование.",
                 "uz": "Tavsiya: obyektni sugʻurtaga qabul qilish.",
                 "en": "Recommendation: accept the object for insurance."},
    "d_accept_with_clauses": {"ru": "Рекомендация: принять объект на страхование с оговорками.",
                              "uz": "Tavsiya: obyektni izohlar (shartlar) bilan sugʻurtaga qabul qilish.",
                              "en": "Recommendation: accept the object for insurance subject to clauses."},
    "d_decline": {"ru": "Рекомендация: отказать в страховании — сработали все признаки повышенного риска.",
                  "uz": "Tavsiya: sugʻurtadan rad etish — xavfni oshiruvchi barcha belgilar mavjud.",
                  "en": "Recommendation: decline — all risk-increasing indicators are present."},
    "disc_title": {"ru": "Расхождения в данных", "uz": "Maʼlumotlardagi tafovutlar", "en": "Data discrepancies"},
    "disc_none": {"ru": "Расхождений между источниками не выявлено.",
                  "uz": "Manbalar oʻrtasida tafovut aniqlanmadi.",
                  "en": "No discrepancies between sources were found."},
    "disc_line": {"ru": "{label}: {values}.", "uz": "{label}: {values}.", "en": "{label}: {values}."},
    "disc_priority": {"ru": "При расхождении приоритет у документа с печатью производителя; решение принимает "
                            "андеррайтер.",
                      "uz": "Tafovut boʻlsa, ishlab chiqaruvchi muhri qoʻyilgan hujjat ustuvor; qarorni "
                            "anderrayter qabul qiladi.",
                      "en": "In case of discrepancy the document bearing the manufacturer's seal prevails; the "
                            "underwriter decides."},
    "checks_title": {"ru": "Что проверить андеррайтеру до выдачи полиса",
                     "uz": "Polis berilishidan oldin anderrayter nimani tekshirishi kerak",
                     "en": "What the underwriter should check before issuing the policy"},
    "missing_title": {"ru": "Данные недоступны", "uz": "Maʼlumot mavjud emas", "en": "Data not available"},

    # ---------- что проверить ----------
    "c_disc": {"ru": "Снять расхождение «{label}» по оригиналу документа",
               "uz": "«{label}» boʻyicha tafovutni hujjat asl nusxasi orqali bartaraf etish",
               "en": "Resolve the «{label}» discrepancy against the original document"},
    "c_views": {"ru": "Запросить недостающие фото: {views}", "uz": "Yetishmayotgan suratlarni soʻrash: {views}",
                "en": "Request the missing photos: {views}"},
    "c_no_inspection": {"ru": "Провести осмотр: фотографии объекта не представлены",
                        "uz": "Koʻzdan kechirish oʻtkazish: obyekt suratlari taqdim etilmagan",
                        "en": "Carry out an inspection: no photos of the object were provided"},
    "c_ai_failed": {"ru": "Проверить объект по фото вручную: автоматическое распознавание не выполнено",
                    "uz": "Obyektni suratlar boʻyicha qoʻlda tekshirish: avtomatik aniqlash bajarilmadi",
                    "en": "Check the object on the photos manually: automatic recognition failed"},
    "c_under": {"ru": "Предупредить клиента о пропорциональной выплате или довести сумму до стоимости",
                "uz": "Mijozni mutanosib toʻlov haqida ogohlantirish yoki summani qiymatgacha yetkazish",
                "en": "Warn the client about proportional settlement or raise the sum to the value"},
    "c_over": {"ru": "Снизить страховую сумму до стоимости объекта",
               "uz": "Sugʻurta summasini obyekt qiymatigacha kamaytirish",
               "en": "Reduce the sum insured to the object value"},
    "c_rate_undefined": {"ru": "Определить ставку: в справочнике её нет",
                         "uz": "Tarifni belgilash: u maʼlumotnomada yoʻq",
                         "en": "Set the rate: it is not in the reference"},
    "c_statutory": {"ru": "Применить тариф нормативного акта без поправок",
                    "uz": "Meʼyoriy hujjat tarifini tuzatishsiz qoʻllash",
                    "en": "Apply the statutory tariff without loadings"},
    "c_franchise": {"ru": "Решить вопрос о франшизе и её размере", "uz": "Franshiza va uning miqdori masalasini hal qilish",
                    "en": "Decide on the deductible and its amount"},
    "c_damages": {"ru": "Оценить видимые повреждения и исключить их из покрытия",
                  "uz": "Koʻrinadigan shikastlarni baholash va ularni qoplamadan chiqarish",
                  "en": "Assess the visible damage and exclude it from cover"},
    "c_docs": {"ru": "Запросить документы на объект", "uz": "Obyekt hujjatlarini soʻrash",
               "en": "Request the object documents"},
    "c_missing": {"ru": "Уточнить: {what}", "uz": "Aniqlashtirish: {what}", "en": "Clarify: {what}"},
    "c_confirm": {"ru": "Сверить распознанные данные с оригиналами документов",
                  "uz": "Aniqlangan maʼlumotlarni hujjatlarning asl nusxalari bilan solishtirish",
                  "en": "Verify the recognised data against the original documents"},
    "c_decline": {"ru": "Подтвердить или пересмотреть отказ после проверки данных",
                  "uz": "Maʼlumotlar tekshirilgandan soʻng rad etishni tasdiqlash yoki qayta koʻrib chiqish",
                  "en": "Confirm or reconsider the decline after checking the data"},

    # ---------- /act/photos ----------
    "warn_pd": {"ru": "Не загружайте документы с данными людей (паспорт, ФИО, адрес): снимки уходят в языковую "
                      "модель как картинки. Отчёт кредитного бюро — только PDF с текстом: скан или фото отчёта не "
                      "читается и в модель не отправляется (отчёт содержит кредитную историю). Сервер тестовый.",
                "uz": "Odamlar maʼlumotlari bor hujjatlarni (pasport, F.I.Sh., manzil) yuklamang: suratlar til "
                      "modeliga rasm sifatida yuboriladi. Kredit byurosi hisoboti — faqat matnli PDF: hisobot skani "
                      "yoki surati oʻqilmaydi va modelga yuborilmaydi (hisobotda kredit tarixi bor). Server test "
                      "rejimida.",
                "en": "Do not upload documents containing personal data (passport, names, addresses): images are "
                      "sent to the language model as pictures. A credit bureau report — only as a PDF with text: a "
                      "scan or photo of the report is not read and not sent to the model (the report contains a credit "
                      "history). This is a test server."},
    "ph_ok": {"ru": "Распознано по фото: {n} значений. Проверьте каждое — модель может ошибаться.",
              "uz": "Suratlardan aniqlandi: {n} ta qiymat. Har birini tekshiring — model xato qilishi mumkin.",
              "en": "Recognised from photos: {n} values. Check each one — the model can make mistakes."},
    "ph_empty": {"ru": "Модель не нашла на фото значений для акта. Заполните данные вручную.",
                 "uz": "Model suratlarda dalolatnoma uchun qiymat topmadi. Maʼlumotlarni qoʻlda kiriting.",
                 "en": "The model found no values for the report on the photos. Enter the data manually."},
    "ph_ai_off": {"ru": "Распознавание фото недоступно: {reason}. Фото сохранены; данные можно ввести вручную — акт "
                        "сформируется.",
                  "uz": "Suratlarni aniqlash imkonsiz: {reason}. Suratlar saqlandi; maʼlumotlarni qoʻlda kiritish "
                        "mumkin — dalolatnoma tuziladi.",
                  "en": "Photo recognition is unavailable: {reason}. Photos are saved; enter the data manually — "
                        "the report will still be generated."},
    "ph_bad_json": {"ru": "языковая модель вернула ответ не по схеме", "uz": "til modeli javobi sxemaga mos emas",
                    "en": "the language model returned an answer that does not match the schema"},
    "ph_format": {"ru": "формат не принимается — пришлите JPG, PNG или PDF, документы — DOCX или XLSX (WEBP и HEIC "
                        "приложение переводит в JPG само)",
                  "uz": "format qabul qilinmaydi — JPG, PNG yoki PDF, hujjatlarni esa DOCX yoki XLSX yuboring (WEBP "
                        "va HEIC ilova oʻzi JPG ga oʻtkazadi)",
                  "en": "format not accepted — send JPG, PNG or PDF, documents as DOCX or XLSX (the app converts "
                        "WEBP and HEIC to JPG itself)"},
    "ph_too_big": {"ru": "файл больше {mb} МБ", "uz": "fayl {mb} MB dan katta", "en": "file larger than {mb} MB"},    "ph_empty_file": {"ru": "пустой файл", "uz": "boʻsh fayl", "en": "empty file"},
    "ph_too_many": {"ru": "За один раз — не больше {n} файлов", "uz": "Bir martada {n} tadan koʻp fayl emas",
                    "en": "No more than {n} files at a time"},
    "ph_none": {"ru": "Файлы не приняты", "uz": "Fayllar qabul qilinmadi", "en": "No files accepted"},
    "ai_not_connected": {"ru": "языковая модель не подключена", "uz": "til modeli ulanmagan",
                         "en": "the language model is not connected"},
    "ai_no_files": {"ru": "текущая языковая модель не принимает изображения",
                    "uz": "joriy til modeli rasmlarni qabul qilmaydi",
                    "en": "the current language model does not accept images"},
    "not_found": {"ru": "Акт не найден или его срок (7 дней) истёк", "uz": "Dalolatnoma topilmadi yoki muddati (7 kun) tugagan",
                  "en": "Report not found or its 7-day retention period has expired"},
    "ph_body_too_big": {"ru": "Запрос больше {mb} МБ — отправьте меньше файлов или уменьшите снимки",
                        "uz": "Soʻrov {mb} MB dan katta — kamroq fayl yuboring yoki suratlarni kichraytiring",
                        "en": "The request is larger than {mb} MB — send fewer files or smaller images"},
    "ph_length_required": {"ru": "Не указан размер запроса — обновите приложение и повторите",
                           "uz": "Soʻrov hajmi koʻrsatilmagan — ilovani yangilang va qaytadan urinib koʻring",
                           "en": "Request size is missing — update the app and try again"},
    "ph_guest_limit": {"ru": "Слишком много фото подряд: без входа не больше {n} фото в час. Подождите немного и "
                             "повторите.",
                       "uz": "Juda koʻp surat: kirishsiz soatiga {n} tadan koʻp emas. Biroz kutib, qaytadan urinib "
                             "koʻring.",
                       "en": "Too many photos: without signing in, no more than {n} photos per hour. Please wait "
                             "and try again."},
    "ph_dims_unknown": {"ru": "не удалось прочитать размеры картинки — пришлите другой снимок",
                        "uz": "rasm oʻlchamlarini oʻqib boʻlmadi — boshqa surat yuboring",
                        "en": "could not read the image size — send another picture"},
    "ph_too_many_px": {"ru": "картинка слишком большая: {w}×{h} точек, больше {mp} Мп — уменьшите снимок",
                       "uz": "rasm juda katta: {w}×{h} nuqta, {mp} Mp dan koʻp — suratni kichraytiring",
                       "en": "image too large: {w}×{h} pixels, over {mp} MP — reduce the picture"},
    "ph_pdf_pages": {"ru": "в PDF больше {n} страниц — пришлите только нужные страницы",
                     "uz": "PDF da {n} sahifadan koʻp — faqat kerakli sahifalarni yuboring",
                     "en": "the PDF has more than {n} pages — send only the pages you need"},
    "ph_pdf_bad": {"ru": "PDF не открывается или защищён паролем", "uz": "PDF ochilmaydi yoki parol bilan himoyalangan",
                   "en": "the PDF cannot be opened or is password-protected"},
    "ph_not_sent": {"ru": "Модели не показаны файлы № {files}: вместе с остальными они больше {mb} МБ даже после "
                          "сжатия. Их содержимое не распознано — проверьте вручную или загрузите отдельно.",
                    "uz": "Modelga {files}-fayllar koʻrsatilmadi: siqilgandan keyin ham boshqalar bilan birga {mb} MB "
                          "dan katta. Ularning mazmuni aniqlanmadi — qoʻlda tekshiring yoki alohida yuklang.",
                    "en": "Files No. {files} were not shown to the model: together with the others they exceed "
                          "{mb} MB even after compression. They were not recognised — check them manually or "
                          "upload them separately."},
    "ai_timeout": {"ru": "языковая модель не ответила за {sec} с", "uz": "til modeli {sec} soniyada javob bermadi",
                   "en": "the language model did not answer within {sec} s"},
    "ai_busy": {"ru": "исчерпан общий лимит распознаваний на сервере ({n} в час) — попробуйте позже",
                "uz": "serverdagi umumiy aniqlash limiti tugadi (soatiga {n} ta) — keyinroq urinib koʻring",
                "en": "the server-wide recognition limit ({n} per hour) is used up — try again later"},
    "ai_error": {"ru": "ошибка при обращении к языковой модели", "uz": "til modeliga murojaatda xato",
                 "en": "error when calling the language model"},
    "multi_class_note": {"ru": "Продукт относится к нескольким классам ({classes}): договор разобран по частям, каждая "
                               "часть посчитана по шаблону и тарифу своего класса (Положение 1882, п. 11).",
                         "uz": "Mahsulot bir nechta klassga tegishli ({classes}): shartnoma qismlarga ajratildi, har "
                               "bir qism oʻz klassi shabloni va tarifi boʻyicha hisoblandi (1882-son Nizom, 11-band).",
                         "en": "The product belongs to several classes ({classes}): the contract is split into parts, "
                               "each part is priced by its own class template and tariff (Regulation 1882, cl. 11)."},

    # ---------- /act/{id}/send ----------
    "send_ok": {"ru": "Акт отправлен в чат с ботом", "uz": "Dalolatnoma bot bilan chatga yuborildi",
                "en": "The report has been sent to your chat with the bot"},
    "send_no_tg": {"ru": "Отправка ботом работает только в Telegram: откройте приложение в Telegram или скачайте "
                         "акт на компьютере",
                   "uz": "Bot orqali yuborish faqat Telegramda ishlaydi: ilovani Telegramda oching yoki "
                         "dalolatnomani kompyuterda yuklab oling",
                   "en": "Sending via the bot works only in Telegram: open the app in Telegram or download the "
                         "report on a computer"},
    "send_bad_init": {"ru": "Не удалось подтвердить вход через Telegram — откройте приложение заново",
                      "uz": "Telegram orqali kirishni tasdiqlab boʻlmadi — ilovani qaytadan oching",
                      "en": "Could not confirm the Telegram sign-in — reopen the app"},
    "send_bot_off": {"ru": "Бот не подключён — скачайте акт на компьютере",
                     "uz": "Bot ulanmagan — dalolatnomani kompyuterda yuklab oling",
                     "en": "The bot is not connected — download the report on a computer"},
    "send_limit": {"ru": "Не больше {n} отправок в час — подождите немного и повторите",
                   "uz": "Soatiga {n} tadan koʻp yuborib boʻlmaydi — biroz kutib, qaytadan urinib koʻring",
                   "en": "No more than {n} sends per hour — please wait and try again"},
    "send_failed": {"ru": "Telegram не принял файл — попробуйте позже или скачайте акт на компьютере",
                    "uz": "Telegram faylni qabul qilmadi — keyinroq urinib koʻring yoki kompyuterda yuklab oling",
                    "en": "Telegram did not accept the file — try later or download the report on a computer"},
    "send_start_bot": {"ru": "Бот не может написать вам первым: откройте чат с ботом, нажмите «Start» и повторите",
                       "uz": "Bot sizga birinchi boʻlib yoza olmaydi: bot bilan chatni oching, «Start» ni bosing va "
                             "qaytadan urinib koʻring",
                       "en": "The bot cannot message you first: open the chat with the bot, press Start and try "
                             "again"},
    "send_format": {"ru": "Формат — docx или pdf", "uz": "Format — docx yoki pdf", "en": "Format must be docx or pdf"},
    "send_caption": {"ru": "Сюрвейерский акт № {number}", "uz": "Syurveyer dalolatnomasi № {number}",
                     "en": "Surveyor's report No. {number}"},
    "session_not_found": {"ru": "Загрузка фото не найдена или её срок (24 часа) истёк — акт собран без неё",
                          "uz": "Suratlar yuklamasi topilmadi yoki muddati (24 soat) tugagan — dalolatnoma usiz tuzildi",
                          "en": "Photo upload not found or expired (24 hours) — the report was built without it"},
}

TX_SCORING = {
    # ---------- страница скоринга ----------
    "sc_title": {"ru": "СТРАХОВОЙ СКОРИНГ ОБЪЕКТА", "uz": "OBYEKTNING SUGʻURTA SKORINGI",
                 "en": "INSURANCE SCORING OF THE OBJECT"},
    "sc_type": {"ru": "Страховой скоринг объекта", "uz": "Obyektning sugʻurta skoringi",
                "en": "Insurance scoring of the object"},
    "sc_rq_type": {"ru": "Тип отчёта", "uz": "Hisobot turi", "en": "Report type"},
    "sc_rq_number": {"ru": "Номер акта", "uz": "Dalolatnoma raqami", "en": "Report number"},
    "sc_rq_datetime": {"ru": "Дата и время", "uz": "Sana va vaqt", "en": "Date and time"},
    "sc_rq_by": {"ru": "Сформировал", "uz": "Tuzgan", "en": "Prepared by"},
    "sc_rq_product": {"ru": "Продукт", "uz": "Mahsulot", "en": "Product"},
    "sc_rq_class": {"ru": "Класс страхования", "uz": "Sugʻurta klassi", "en": "Insurance class"},
    "sc_b1": {"ru": "1. ОБЪЕКТ", "uz": "1. OBYEKT", "en": "1. OBJECT"},
    "sc_b2": {"ru": "2. СКОРИНГ", "uz": "2. SKORING", "en": "2. SCORING"},
    "sc_b3": {"ru": "3. ОБЩИЙ ОБЗОР", "uz": "3. UMUMIY KOʻRINISH", "en": "3. OVERVIEW"},
    "sc_b4": {"ru": "4. РИСКИ", "uz": "4. XAVFLAR", "en": "4. RISKS"},
    "sc_b5": {"ru": "5. СЦЕНАРИИ УБЫТКА", "uz": "5. ZARAR STSENARIYLARI", "en": "5. LOSS SCENARIOS"},
    "sc_b6": {"ru": "6. ЧТО ПРОВЕРИТЬ АНДЕРРАЙТЕРУ", "uz": "6. ANDERRAYTER NIMANI TEKSHIRISHI KERAK",
              "en": "6. WHAT THE UNDERWRITER SHOULD CHECK"},
    "sc_b_borrower": {"ru": "ЗАЁМЩИК (КРЕДИТНОЕ БЮРО)", "uz": "QARZ OLUVCHI (KREDIT BYUROSI)",
                      "en": "BORROWER (CREDIT BUREAU)"},
    "sc_s_name": {"ru": "Наименование", "uz": "Nomi", "en": "Name"},
    "sc_s_kind": {"ru": "Вид объекта", "uz": "Obyekt turi", "en": "Object type"},
    "sc_s_holder": {"ru": "Страхователь", "uz": "Sugʻurtalanuvchi", "en": "Policyholder"},
    "sc_s_region": {"ru": "Регион / адрес", "uz": "Hudud / manzil", "en": "Region / address"},
    "sc_s_ids": {"ru": "Идентификаторы", "uz": "Identifikatorlar", "en": "Identifiers"},
    "sc_s_source": {"ru": "Источник данных", "uz": "Maʼlumot manbai", "en": "Data source"},
    "sc_holder_unknown": {"ru": "не указан", "uz": "koʻrsatilmagan", "en": "not stated"},
    "sc_holder_individual": {"ru": "физическое лицо (данные не показываются)",
                             "uz": "jismoniy shaxs (maʼlumotlar koʻrsatilmaydi)",
                             "en": "individual (details not shown)"},
    "sc_id_cadastre": {"ru": "кадастровый номер {v}", "uz": "kadastr raqami {v}", "en": "cadastral number {v}"},
    "sc_id_serial": {"ru": "заводской номер {v}", "uz": "zavod raqami {v}", "en": "serial number {v}"},
    "sc_ids_none": {"ru": "нет", "uz": "yoʻq", "en": "none"},
    "sc_src_photos": {"ru": "фото: {n} (распознаны ИИ)", "uz": "suratlar: {n} (SI aniqlagan)",
                      "en": "photos: {n} (recognised by AI)"},
    "sc_src_photos_noai": {"ru": "фото: {n} (без распознавания)", "uz": "suratlar: {n} (aniqlanmagan)",
                           "en": "photos: {n} (not recognised)"},
    "sc_src_docs": {"ru": "документы: {what}", "uz": "hujjatlar: {what}", "en": "documents: {what}"},
    "sc_src_input": {"ru": "ввод сотрудника", "uz": "xodim kiritgan", "en": "staff input"},
    "sc_score": {"ru": "Страховой балл", "uz": "Sugʻurta bali", "en": "Insurance score"},
    "sc_class": {"ru": "Страховой класс", "uz": "Sugʻurta sinfi", "en": "Insurance grade"},
    "sc_class_value": {"ru": "{code}, {label} уровень", "uz": "{code}, {label} daraja", "en": "{code}, {label}"},
    "sc_version": {"ru": "Версия шкалы", "uz": "Shkala versiyasi", "en": "Scale version"},
    "sc_risk100": {"ru": "Балл риска 0–100", "uz": "Xavf bali 0–100", "en": "Risk score 0–100"},
    "sc_by_level": {"ru": "по уровню риска", "uz": "xavf darajasi boʻyicha", "en": "by risk level"},
    "sc_comp_title": {"ru": "Из чего сложился балл", "uz": "Ball nimalardan tashkil topdi",
                      "en": "What the score is made of"},
    "sc_comp_why": {"ru": "балл риска {p} из 100, вес {w}: {points} из {max}",
                    "uz": "xavf bali 100 dan {p}, vazn {w}: {max} dan {points}",
                    "en": "risk score {p} of 100, weight {w}: {points} of {max}"},
    "sc_comp_off": {"ru": "не учтено: нет данных или к объекту не относится",
                    "uz": "hisobga olinmadi: maʼlumot yoʻq yoki obyektga taalluqli emas",
                    "en": "not counted: no data or not relevant to the object"},
    "sc_comp_level": {"ru": "Уровень риска акта", "uz": "Dalolatnomadagi xavf darajasi", "en": "Report risk level"},
    "sc_comp_level_why": {"ru": "оценка по уровню риска: {level} — {score}",
                          "uz": "xavf darajasi boʻyicha baho: {level} — {score}",
                          "en": "estimate by risk level: {level} — {score}"},
    "sc_comp_risk": {"ru": "Балл риска 0–100", "uz": "Xavf bali 0–100", "en": "Risk score 0–100"},
    "sc_of": {"ru": "{points} из {max}", "uz": "{max} dan {points}", "en": "{points} of {max}"},
    # общий обзор
    "sc_o_sum": {"ru": "страховая сумма", "uz": "sugʻurta summasi", "en": "sum insured"},
    "sc_o_value": {"ru": "стоимость объекта", "uz": "obyekt qiymati", "en": "object value"},
    "sc_o_ratio": {"ru": "сумма к стоимости", "uz": "summaning qiymatga nisbati", "en": "sum to value"},
    "sc_o_tariff": {"ru": "тариф", "uz": "tarif", "en": "rate"},
    "sc_o_premium": {"ru": "премия", "uz": "sugʻurta mukofoti", "en": "premium"},
    "sc_o_term": {"ru": "срок, дней", "uz": "muddat, kun", "en": "term, days"},
    "sc_o_losses": {"ru": "убытков за 3 года", "uz": "3 yillik zararlar", "en": "losses over 3 years"},
    "sc_o_docs": {"ru": "документов представлено / не хватает", "uz": "hujjatlar taqdim etilgan / yetishmaydi",
                  "en": "documents provided / missing"},
    "sc_o_disc": {"ru": "расхождений в данных", "uz": "maʼlumotlardagi tafovutlar", "en": "data discrepancies"},
    "sc_o_views": {"ru": "ракурсов осмотра", "uz": "koʻrik rakurslari", "en": "inspection views"},
    "sc_o_pml": {"ru": "PML — вероятный максимальный убыток", "uz": "PML — ehtimoliy maksimal zarar",
                 "en": "PML — probable maximum loss"},
    "sc_o_eml": {"ru": "EML — оценочный максимальный убыток", "uz": "EML — taxminiy maksimal zarar",
                 "en": "EML — estimated maximum loss"},
    "sc_o_mfl": {"ru": "MFL — максимально возможный убыток", "uz": "MFL — mumkin boʻlgan maksimal zarar",
                 "en": "MFL — maximum foreseeable loss"},
    "sc_o_retention": {"ru": "лимит удержания", "uz": "ushlab qolish limiti", "en": "retention limit"},
    "sc_o_retention_est": {"ru": "лимит удержания (оценка, временно)", "uz": "ushlab qolish limiti (taxminiy, vaqtincha)",
                           "en": "retention limit (estimate, temporary)"},
    "sc_o_market": {"ru": "рыночная ставка", "uz": "bozor tarifi", "en": "market rate"},
    "sc_o_franchise": {"ru": "франшиза", "uz": "franshiza", "en": "deductible"},
    "sc_o_checks": {"ru": "проверок андеррайтеру", "uz": "anderrayter tekshiruvlari", "en": "underwriter checks"},
    "sc_v_no_data": {"ru": "нет данных", "uz": "maʼlumot yoʻq", "en": "no data"},
    "sc_v_not_set": {"ru": "не задан", "uz": "belgilanmagan", "en": "not set"},
    "sc_v_of": {"ru": "{n} из {m}", "uz": "{m} dan {n}", "en": "{n} of {m}"},
    "sc_v_ref": {"ru": "{v} справочно", "uz": "{v} maʼlumot uchun", "en": "{v} for reference"},
    "sc_v_temp": {"ru": "{v} (оценка, временно)", "uz": "{v} (taxminiy, vaqtincha)", "en": "{v} (estimate, temporary)"},
    "sc_r_risk": {"ru": "Риск", "uz": "Xavf", "en": "Risk"},
    "sc_r_share": {"ru": "Доля", "uz": "Ulush", "en": "Share"},
    "sc_r_level": {"ru": "Уровень", "uz": "Daraja", "en": "Level"},
    "sc_r_none": {"ru": "Риски не разбиты — см. раздел 4 акта", "uz": "Xavflar ajratilmagan — dalolatnomaning 4-boʻlimiga qarang",
                  "en": "Risks are not broken down — see section 4 of the report"},
    "sc_sc_name": {"ru": "Сценарий", "uz": "Stsenariy", "en": "Scenario"},
    "sc_sc_amount": {"ru": "Сумма", "uz": "Summa", "en": "Amount"},
    "sc_sc_pct": {"ru": "% суммы", "uz": "summadan %", "en": "% of sum"},
    "sc_sc_none": {"ru": "Сценарии не посчитаны — см. раздел 4 акта", "uz": "Stsenariylar hisoblanmagan — 4-boʻlimga qarang",
                   "en": "Scenarios not calculated — see section 4"},
    "sc_checks_none": {"ru": "Проверок нет", "uz": "Tekshiruvlar yoʻq", "en": "No checks"},
    "sc_checks_more": {"ru": "и ещё {n} — в разделе 5 акта", "uz": "yana {n} ta — dalolatnomaning 5-boʻlimida",
                       "en": "and {n} more — in section 5 of the report"},
    "sc_parts_note": {"ru": "Договор из {n} частей: балл — по самой опасной части {k}",
                      "uz": "{n} qismdan iborat shartnoma: ball eng xavfli {k}-qism boʻyicha",
                      "en": "Contract of {n} parts: the score is that of the most hazardous part {k}"},
    "sc_parts_line": {"ru": "часть {n} (класс {cls}): {score} — {code}", "uz": "{n}-qism ({cls}-klass): {score} — {code}",
                      "en": "part {n} (class {cls}): {score} — {code}"},
    "sc_text_risk": {"ru": "Балл {score} из 500 — класс {code} ({label}). Основа — балл риска {risk} из 100 аналитики "
                           "акта: 500 − 5 × {risk} {eq} {score}.",
                     "uz": "500 dan {score} ball — {code} sinf ({label}). Asos — dalolatnoma tahlilidagi 100 dan {risk} "
                           "xavf bali: 500 − 5 × {risk} {eq} {score}.",
                     "en": "Score {score} of 500 — class {code} ({label}). Based on the report's risk score {risk} of "
                           "100: 500 − 5 × {risk} {eq} {score}."},
    "sc_text_dmg": {"ru": "С учётом повреждений с фото ({n}, {sev}): {before} − {pen} = {score} (экспертно, не калибровано).",
                    "uz": "Suratdagi shikastlar hisobga olinganda ({n}, {sev}): {before} − {pen} = {score} (ekspert baho, "
                          "kalibrlanmagan).",
                    "en": "Including damage seen on photos ({n}, {sev}): {before} − {pen} = {score} (expert, not "
                          "calibrated)."},
    "sc_comp_dmg": {"ru": "Повреждения с фото", "uz": "Suratdagi shikastlar", "en": "Damage on photos"},
    "sc_comp_dmg_why": {"ru": "{n} шт., {sev}: −{pen} (экспертно, не калибровано)",
                        "uz": "{n} ta, {sev}: −{pen} (ekspert baho, kalibrlanmagan)",
                        "en": "{n}, {sev}: −{pen} (expert, not calibrated)"},
    "dmg_preexisting": {"ru": "исключаются из покрытия как предсуществующие",
                        "uz": "avvaldan mavjud sifatida qoplamadan chiqariladi",
                        "en": "excluded from cover as pre-existing"},
    "dmg_sev_cosmetic": {"ru": "косметические", "uz": "kosmetik", "en": "cosmetic"},
    "dmg_sev_major": {"ru": "существенные", "uz": "jiddiy", "en": "substantial"},
    "sc_text_level": {"ru": "Балл {score} из 500 — класс {code} ({label}). Балла риска нет — оценка по уровню риска "
                            "акта ({level}): низкий 430, умеренный 300, высокий 130.",
                      "uz": "500 dan {score} ball — {code} sinf ({label}). Xavf bali yoʻq — dalolatnomadagi xavf "
                            "darajasi boʻyicha baho ({level}): past 430, oʻrtacha 300, yuqori 130.",
                      "en": "Score {score} of 500 — class {code} ({label}). No risk score — estimate by the report's "
                            "risk level ({level}): low 430, moderate 300, high 130."},
    "sc_method_risk": {"ru": "Балл = 500 − 5 × балл риска 0–100 аналитики акта (чем выше балл риска, тем хуже). Сектора "
                             "по порогам уровня аналитики: {bands}; подкласс 1 — верхняя треть сектора, 3 — нижняя.",
                       "uz": "Ball = 500 − 5 × dalolatnoma tahlilidagi 0–100 xavf bali (xavf bali qancha yuqori boʻlsa, "
                             "shuncha yomon). Sektorlar tahlil darajasi chegaralari boʻyicha: {bands}; 1-kichik "
                             "sinf — sektorning yuqori uchdan biri, 3 — pastki.",
                       "en": "Score = 500 − 5 × the report's risk score 0–100 (the higher the risk score, the worse). "
                             "Bands follow the analytics level thresholds: {bands}; sub-class 1 is the top third "
                             "of the band, 3 the bottom."},
    "sc_method_level": {"ru": "Балла риска нет (класс без аналитики) — оценка по уровню риска акта: низкий 430, "
                              "умеренный 300, высокий 130.",
                        "uz": "Xavf bali yoʻq (tahlilsiz klass) — dalolatnomadagi xavf darajasi boʻyicha baho: past 430, "
                              "oʻrtacha 300, yuqori 130.",
                        "en": "No risk score (class without analytics) — estimate by the report's risk level: low 430, "
                              "moderate 300, high 130."},
    "sc_note": {"ru": "Экспертно, не калибровано; это не кредитный скоринг и не оценка КАТМ.",
                "uz": "Ekspert baho, kalibrlanmagan; bu kredit skoringi emas va KATM bahosi emas.",
                "en": "Expert estimate, not calibrated; this is not a credit score and not a KATM assessment."},
    "sc_caption": {"ru": "Экспертная шкала, не калибровано. Не является кредитным скорингом и оценкой кредитного бюро",
                   "uz": "Ekspert shkala, kalibrlanmagan. Kredit skoringi va kredit byurosi bahosi hisoblanmaydi",
                   "en": "Expert scale, not calibrated. Not a credit score and not a credit bureau assessment"},
    "sc_png_act": {"ru": "Акт № {n} от {d}", "uz": "Dalolatnoma № {n}, {d}", "en": "Report No. {n} of {d}"},
    "sc_dec_title": {"ru": "Рекомендация акта", "uz": "Dalolatnoma tavsiyasi", "en": "Report recommendation"},
    "sc_dec_accept": {"ru": "принять", "uz": "qabul qilish", "en": "accept"},
    "sc_dec_accept_with_clauses": {"ru": "принять с оговорками", "uz": "izohlar bilan qabul qilish",
                                   "en": "accept with clauses"},
    "sc_dec_decline": {"ru": "отказать", "uz": "rad etish", "en": "decline"},
    "sc_act_level_title": {"ru": "Уровень риска акта", "uz": "Dalolatnomadagi xavf darajasi", "en": "Report risk level"},
    "sc_line": {"ru": "{title}: {v}", "uz": "{title}: {v}", "en": "{title}: {v}"},
    "sc_dec_see": {"ru": "см. рекомендацию акта: отказать", "uz": "dalolatnoma tavsiyasiga qarang: rad etish",
                   "en": "see the report recommendation: decline"},
    "sc_an_level": {"ru": "{cls} — {level} риск по аналитике", "uz": "{cls} — tahlil boʻyicha {level} xavf",
                    "en": "{cls} — {level} risk per analytics"},
    "sc_parts_more": {"ru": "и ещё {n} — в акте", "uz": "yana {n} ta — dalolatnomada", "en": "and {n} more — in the report"},
    "sc_s_note": {"ru": "Примечание", "uz": "Izoh", "en": "Note"},
    "sc_footer_line": {"ru": "Скоринг сформирован ИИ-сюрвейером по данным акта; экспертная шкала, не калибровано; "
                             "не является кредитным скорингом",
                       "uz": "Skoring SI-syurveyer tomonidan dalolatnoma maʼlumotlari asosida tuzilgan; ekspert shkala, "
                             "kalibrlanmagan; kredit skoringi hisoblanmaydi",
                       "en": "Scoring prepared by the AI surveyor from the report data; expert scale, not calibrated; "
                             "not a credit score"},
    # ---------- заёмщик: отчёт кредитного бюро ----------
    "cb_title": {"ru": "Заёмщик: данные кредитного бюро", "uz": "Qarz oluvchi: kredit byurosi maʼlumotlari",
                 "en": "Borrower: credit bureau data"},
    "cb_date": {"ru": "Отчёт кредитного бюро от {date} ({days} дн. назад); источник: {src}",
                "uz": "Kredit byurosi hisoboti {date} ({days} kun oldin); manba: {src}",
                "en": "Credit bureau report of {date} ({days} days ago); source: {src}"},
    "cb_date_unknown": {"ru": "Отчёт кредитного бюро без даты; источник: {src}",
                        "uz": "Sanasiz kredit byurosi hisoboti; manba: {src}",
                        "en": "Credit bureau report without a date; source: {src}"},
    "cb_subject_legal": {"ru": "Субъект: юридическое лицо{rest}", "uz": "Subyekt: yuridik shaxs{rest}",
                         "en": "Subject: legal entity{rest}"},
    "cb_subject_individual": {"ru": "Субъект: физическое лицо — ФИО, ПИНФЛ, адрес и телефон не извлекаются",
                              "uz": "Subyekt: jismoniy shaxs — F.I.Sh., JShShIR, manzil va telefon olinmaydi",
                              "en": "Subject: individual — name, personal ID, address and phone are not extracted"},
    "cb_subject_unknown": {"ru": "Субъект: тип не указан в отчёте", "uz": "Subyekt: turi hisobotda koʻrsatilmagan",
                           "en": "Subject: type not stated in the report"},
    "cb_inn": {"ru": "ИНН {v}", "uz": "STIR {v}", "en": "TIN {v}"},
    "cb_oked": {"ru": "ОКЭД {v}", "uz": "IFUT {v}", "en": "OKED {v}"},
    "cb_score": {"ru": "Скоринговый балл бюро: {score}, класс оценки {cls}, версия {ver}",
                 "uz": "Byuro skoring bali: {score}, baholash sinfi {cls}, versiya {ver}",
                 "en": "Bureau credit score: {score}, score class {cls}, version {ver}"},
    "cb_overview": {"ru": "Общий обзор: заявок {a}, договоров {c}, условных обязательств {u}, запросов {q}, "
                          "среднемесячный платёж {p}",
                    "uz": "Umumiy koʻrinish: arizalar {a}, shartnomalar {c}, shartli majburiyatlar {u}, soʻrovlar {q}, "
                          "oʻrtacha oylik toʻlov {p}",
                    "en": "Overview: applications {a}, contracts {c}, contingent liabilities {u}, inquiries {q}, "
                          "average monthly payment {p}"},
    "cb_overdue": {"ru": "Просрочки: основного долга — {n} раз, максимальная {days} дн. на {amount}; процентов — "
                         "максимальная непрерывная {idays} дн., всего {itotal}",
                   "uz": "Kechikishlar: asosiy qarz — {n} marta, eng uzoq {days} kun, {amount}; foizlar — eng uzoq "
                         "uzluksiz {idays} kun, jami {itotal}",
                   "en": "Overdues: principal — {n} times, longest {days} days for {amount}; interest — longest "
                         "continuous {idays} days, total {itotal}"},
    "cb_active": {"ru": "Действующие договоры: {n}, остаток задолженности {debt}, просроченная часть {od}, "
                        "среднемесячный платёж {pay}",
                  "uz": "Amaldagi shartnomalar: {n}, qarz qoldigʻi {debt}, kechiktirilgan qism {od}, oylik toʻlov {pay}",
                  "en": "Active contracts: {n}, outstanding debt {debt}, overdue part {od}, monthly payment {pay}"},
    "cb_creditors": {"ru": "Кредиторы: {v}", "uz": "Kreditorlar: {v}", "en": "Creditors: {v}"},
    "cb_not_in_rate": {"ru": "Проверки заёмщика добавляют оговорки к рекомендации («принять» становится «принять с "
                             "оговорками»); в уровень риска и ставку не входят — пока заказчик не утвердит правило.",
                       "uz": "Qarz oluvchi tekshiruvlari tavsiyaga izohlar qoʻshadi («qabul qilish» «izohlar bilan "
                             "qabul qilish»ga aylanadi); xavf darajasi va tarifga kirmaydi — buyurtmachi qoidani "
                             "tasdiqlamaguncha.",
                       "en": "Borrower checks add clauses to the recommendation (“accept” becomes “accept with "
                             "clauses”); they do not enter the risk level or the rate — until the client approves a rule."},
    "cb_keep": {"ru": "Данные кредитного отчёта хранятся в акте 7 дней; требуется согласие субъекта на получение "
                      "кредитного отчёта (обязанность страховщика).",
                "uz": "Kredit hisoboti maʼlumotlari dalolatnomada 7 kun saqlanadi; kredit hisobotini olish uchun "
                      "subyektning roziligi kerak (sugʻurtalovchining majburiyati).",
                "en": "Credit report data are kept in the report for 7 days; the subject's consent to obtain the credit "
                      "report is required (the insurer's obligation)."},
    "ph_kinds_bad": {"ru": "Вид файла передан неверно: нужен JSON {номер файла: credit_report, object или document}",
                     "uz": "Fayl turi notoʻgʻri berilgan: JSON {fayl raqami: credit_report, object yoki document} kerak",
                     "en": "File kinds are malformed: expected JSON {file number: credit_report, object or document}"},
    "cr_scan_off": {"ru": "Скан отчёта кредитного бюро не читается — загрузите PDF с текстом",
                    "uz": "Kredit byurosi hisobotining skani oʻqilmaydi — matnli PDF yuklang",
                    "en": "A scan of a credit bureau report is not read — upload a PDF with text"},
    "cb_no_direct": {"ru": "Прямого запроса в КАТМ нет — отчёт загружает сотрудник; для прямого подключения нужен "
                           "договор страховщика с бюро и согласие субъекта.",
                     "uz": "KATMga toʻgʻridan-toʻgʻri soʻrov yoʻq — hisobotni xodim yuklaydi; toʻgʻridan-toʻgʻri ulanish "
                           "uchun sugʻurtalovchining byuro bilan shartnomasi va subyekt roziligi kerak.",
                     "en": "There is no direct KATM query — staff upload the report; a direct connection requires the "
                           "insurer's contract with the bureau and the subject's consent."},
    "cb_not_credit": {"ru": "Класс договора не кредитный — проверки по отчёту бюро не применяются.",
                      "uz": "Shartnoma klassi kredit emas — byuro hisoboti boʻyicha tekshiruvlar qoʻllanilmaydi.",
                      "en": "The contract class is not a credit class — bureau report checks do not apply."},
    "cb_edits": {"ru": "Сотрудник исправил: {what}", "uz": "Xodim tuzatdi: {what}", "en": "Edited by staff: {what}"},
    "cb_doc_missing": {"ru": "Загрузка отчёта недоступна — значения введены сотрудником",
                       "uz": "Hisobot yuklamasi mavjud emas — qiymatlarni xodim kiritgan",
                       "en": "The report upload is unavailable — values entered by staff"},
    "cb_s_score": {"ru": "балл / класс бюро", "uz": "byuro bali / sinfi", "en": "bureau score / class"},
    "cb_s_overdue": {"ru": "действующая просрочка", "uz": "amaldagi kechikish", "en": "current overdue"},
    "cb_s_max_overdue": {"ru": "макс. просрочка ОД, дней", "uz": "asosiy qarz eng uzoq kechikish, kun",
                         "en": "longest principal overdue, days"},
    "cb_s_debt": {"ru": "остаток задолженности", "uz": "qarz qoldigʻi", "en": "outstanding debt"},
    "cb_s_payment": {"ru": "среднемесячный платёж", "uz": "oʻrtacha oylik toʻlov", "en": "average monthly payment"},
    "cb_s_date": {"ru": "дата отчёта", "uz": "hisobot sanasi", "en": "report date"},
    "cr_individual": {"ru": "Отчёт бюро по физическому лицу: ФИО, ПИНФЛ, адрес и телефон не извлекаются",
                      "uz": "Jismoniy shaxs boʻyicha byuro hisoboti: F.I.Sh., JShShIR, manzil va telefon olinmaydi",
                      "en": "Bureau report on an individual: name, personal ID, address and phone are not extracted"},
    "cr_found": {"ru": "Распознан отчёт кредитного бюро — проверьте значения",
                 "uz": "Kredit byurosi hisoboti aniqlandi — qiymatlarni tekshiring",
                 "en": "Credit bureau report recognised — please check the values"},
    "c_borrower_low_class": {"ru": "Заёмщик: класс оценки кредитного бюро {cls} — порог «{low} и ниже»: оценить "
                                   "кредитоспособность (экспертно, в ставку не входит)",
                             "uz": "Qarz oluvchi: kredit byurosi baholash sinfi {cls} — chegara «{low} va past»: "
                                   "kreditga layoqatni baholang (ekspert baho, tarifga kirmaydi)",
                             "en": "Borrower: credit bureau score class {cls} — threshold “{low} and below”: assess "
                                   "creditworthiness (expert rule, not in the rate)"},
    "c_borrower_no_score": {"ru": "Заёмщик: в отчёте кредитного бюро нет балла и класса — проверить вручную",
                            "uz": "Qarz oluvchi: kredit byurosi hisobotida ball va sinf yoʻq — qoʻlda tekshiring",
                            "en": "Borrower: the credit bureau report has no score or class — check manually"},
    "c_borrower_overdue": {"ru": "Заёмщик: действующая просрочка по кредитам {amount} — выяснить причину",
                           "uz": "Qarz oluvchi: kreditlar boʻyicha amaldagi kechikish {amount} — sababini aniqlang",
                           "en": "Borrower: current overdue on loans {amount} — find out why"},
    "c_borrower_stale": {"ru": "Отчёт кредитного бюро устарел: {days} дн. (допустимо {max}) — запросить свежий отчёт",
                         "uz": "Kredit byurosi hisoboti eskirgan: {days} kun (ruxsat {max}) — yangi hisobot soʻrang",
                         "en": "The credit bureau report is outdated: {days} days (allowed {max}) — request a fresh one"},
    "c_borrower_stale_nodate": {"ru": "Дата отчёта кредитного бюро не найдена — считать отчёт устаревшим, запросить "
                                      "свежий отчёт",
                                "uz": "Kredit byurosi hisobotining sanasi topilmadi — eskirgan deb hisoblang, yangisini "
                                      "soʻrang",
                                "en": "The credit bureau report date was not found — treat the report as outdated, "
                                      "request a fresh one"},
    "cr_no_date": {"ru": "Дата отчёта не найдена (ищется по подписям «Время запроса», «Дата запроса», «Дата заявки», "
                         "«Дата согласия») — введите её вручную",
                   "uz": "Hisobot sanasi topilmadi (soʻrov vaqti, soʻrov sanasi, ariza sanasi yoki rozilik sanasi "
                         "yozuvlari boʻyicha qidiriladi) — qoʻlda kiriting",
                   "en": "Report date not found (looked up by the request time, request date, application date or "
                         "consent date labels) — enter it manually"},
}
