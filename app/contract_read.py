"""
Договор страхования (решение заказчика 30.09.2026): распознавание вида документа и извлечение условий.

Тот же образец, что app/branch_request.py — два пути, один результат:
  parse_text(text, tables) — файл с текстом (DOCX, XLSX, PDF с текстом), без модели: подписи и словари
                             на русском, узбекском (кириллица и латиница) и английском;
  from_model(raw)          — ответ языковой модели по строгой схеме (скан или текст договора при ai_assist).
Оба пути собирают «сырые» значения (строки как в документе) и отдают их fields(): числа, даты, срок,
франшизу и стороны разбирают общие функции branch_request (amount, tariff, franchise, dates_in, term, party,
object_facts) — скан и файл дают одинаковые цифры. Чего в документе нет — None, ничего не выдумывается.

Стороны: название возвращается только у юрлица (branch_request.party); физическое лицо — признак
kind = individual. Персональные данные не извлекаются; в журналы — только счётчики.

Бланк договора (30.09.2026): поля-подчёркивания («Договор №____», «____, именуемая … «Страхователь»», «составляет
____ (____) сум») — не значения, а blank; много пустых ключевых полей — is_template: существенные условия и сверка
не проверяются, модель не вызывается. Личное страхование (несчастные случаи, болезни) — class_hint accident/health,
объект — жизнь и здоровье застрахованных лиц, таблица застрахованных по профессиям — schedule (список с фамилиями
не читается, только число заполненных строк).

Модуль без сети, без базы и без HTTP. Срок разбора — docparse.tick() внутри циклов.
"""
import re
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Optional

from . import branch_request as br
from . import docparse as D

KIND = "contract"                 # вид документа в акте (подпись — act_texts.DOC_KIND_LABELS)
PARTY_CODES = ("insurer", "policyholder", "beneficiary", "pledger")
CORE = ("sum_insured", "premium", "term", "object")          # ключевые поля (порог ai_assist — половина)
KEY_FIELDS = ("contract_no", "contract_date", "policyholder", "object", "sum_insured", "tariff_pct", "premium",
              "term", "franchise", "covered_risks")
# существенные условия договора имущественного страхования (ГК РУз, ст. 929 — формулировки в act_texts)
ESSENTIALS = ("object", "insured_event", "sum_insured", "premium", "term")
MAX_ITEMS = 50
MAX_LIST = 20
MAX_PAYMENTS = 24
MAX_TEXT = br.MAX_TEXT
HEAD_LINES = 25
MIN_MONEY = 1000                  # премия или сумма меньше тысячи сумов — это номер пункта или год, а не деньги

_F = br._F


def _clean(v) -> Optional[str]:
    return br._clean(v)


# ------------------------------------------------------------------ подписи (естественное написание)

TITLE_RX = [re.compile(x) for x in (
    r"\bdogovor\b(?:\s+\S+){0,3}\s+strahovaniya\b",
    r"\bstrahov\w*\s+polis\w*\b",
    r"^polis(?:i)?(?:\s+\S+){0,3}$",                     # короткая строка «ПОЛИС № 12/2026», «POLISI»
    r"\bpolis\b(?:\s+\S+){0,3}\s+strahovaniya\b",
    r"\bsugurta\w*\b(?:\s+\S+){0,2}\s+(?:shartnomasi|shartnoma|polisi|polis)\b",
    r"\binsurance\s+(?:contract|policy|agreement)\b",
    r"\b(?:contract|policy|agreement)\s+of\s+(?:\S+\s+)?insurance\b",
)]
# в строке-заголовке таких слов нет: счёт, письмо, заявление, приложение, акт
TITLE_STOP = _F(("счет", "счёт", "hisob", "invoice", "письмо", "заявлен", "ariza", "ариза", "application",
                 "приложен", "ilova", "илова", "annex", "appendix", "акт", "dalolatnoma", "далолатнома",
                 "дополнительное соглашение", "qo'shimcha kelishuv", "справка", "уведомлен"))
_TITLE_STOP_RX = re.compile(r"(?<![a-z])(?:%s)" % "|".join(re.escape(w) for w in TITLE_STOP))

L = {
    "sum_insured_total": ("общая страховая сумма", "совокупная страховая сумма", "итоговая страховая сумма",
                          "умумий суғурта суммаси", "umumiy sug'urta summasi", "jami sug'urta summasi",
                          "total sum insured", "aggregate sum insured"),
    "sum_insured": ("страховая сумма", "размер страховой суммы", "лимит ответственности", "лимит возмещения",
                    "суғурта суммаси", "sug'urta summasi", "жавобгарлик лимити", "javobgarlik limiti",
                    "sum insured", "insured amount", "amount insured", "limit of liability", "limit of indemnity"),
    "object_value": ("действительная стоимость имущества", "действительная стоимость", "страховая стоимость имущества",
                     "страховая стоимость", "стоимость застрахованного имущества", "стоимость имущества",
                     "стоимость объекта", "рыночная стоимость", "балансовая стоимость", "суғурта қиймати",
                     "sug'urta qiymati", "ҳақиқий қиймати", "haqiqiy qiymati", "мулк қиймати", "mulk qiymati",
                     "insured value", "actual value", "property value", "replacement value", "market value"),
    "premium": ("общая страховая премия", "страховая премия", "размер страховой премии", "сумма страховой премии",
                "суғурта мукофоти", "sug'urta mukofoti", "суғурта мукофоти миқдори", "sug'urta mukofoti miqdori",
                "insurance premium", "total premium", "premium"),
    "tariff": ("ставка страховой премии", "страховой тариф", "тариф страхования", "тарифная ставка",
               "страховая ставка", "тариф", "суғурта тарифи", "sug'urta tarifi", "тариф ставкаси", "tarif stavkasi",
               "tarif", "premium rate", "insurance rate", "rate"),
    "term": ("срок страхования", "срок действия настоящего договора", "срок действия договора", "период страхования",
             "срок договора", "договор действует", "договор вступает в силу", "настоящий договор действует",
             "договор заключен на срок", "суғурта муддати", "sug'urta muddati", "шартноманинг амал қилиш муддати",
             "shartnomaning amal qilish muddati", "амал қилиш муддати", "amal qilish muddati", "шартнома амал қилади",
             "shartnoma amal qiladi", "period of insurance", "insurance period", "policy period", "term of insurance",
             "term of the contract", "this contract is valid", "this policy is valid"),
    "liability": ("ответственность страховщика начинается", "ответственность страховщика по настоящему договору",
                  "ответственность страховщика возникает", "страховая защита начинается",
                  "суғурталовчининг жавобгарлиги", "sug'urtalovchining javobgarligi", "liability of the insurer",
                  "insurer's liability", "cover commences", "cover starts"),
    "franchise": ("безусловная франшиза", "условная франшиза", "франшиза", "franshiza", "франшиза (собственное удержание)",
                  "deductible", "excess"),
    "object": ("объектом страхования являются", "объектом страхования является", "объект страхования",
               "застрахованное имущество", "страхуемое имущество", "предмет страхования", "суғурта объекти",
               "sug'urta obyekti", "sug'urta ob'ekti", "суғурта предмети", "sug'urta predmeti", "insured property",
               "subject matter insured", "subject-matter insured", "insured object", "object of insurance",
               "property insured"),
    "object_fallback": ("предметом настоящего договора является", "предметом договора является",
                        "shartnomaning predmeti", "шартноманинг предмети", "subject of this contract"),
    "address": ("адрес места страхования", "место страхования", "место нахождения имущества",
                "местонахождение имущества", "местонахождение объекта", "адрес объекта", "адрес расположения",
                "объект расположен по адресу", "имущество находится по адресу", "объект манзили", "obyekt manzili",
                "жойлашган манзили", "joylashgan manzili", "суғурта жойи", "sug'urta joyi", "risk address",
                "location of insured property", "address of the insured property", "premises address", "location"),
    "address_bare": ("адрес", "манзил", "manzil", "manzili", "address"),
    "cadastre": ("кадастровый номер", "кадастр рақами", "kadastr raqami", "cadastral number", "cadastre number"),
    "construction": ("материал стен", "конструкция здания", "конструктивные элементы", "конструкция",
                     "девор материали", "devor materiali", "wall material", "construction"),
    "purpose": ("назначение объекта", "назначение здания", "назначение имущества", "назначение", "вид деятельности",
                "фаолият тури", "faoliyat turi", "фойдаланиш мақсади", "foydalanish maqsadi", "occupancy",
                "use of premises", "purpose"),
    "year_built": ("год постройки", "год ввода в эксплуатацию", "қурилган йили", "qurilgan yili",
                   "year of construction", "year built"),
    "product": ("вид страхования", "страховой продукт", "наименование продукта", "суғурта тури", "sug'urta turi",
                "суғурта маҳсулоти", "sug'urta mahsuloti", "type of insurance", "insurance product", "class of insurance"),
    "product_code": ("код вида страхования", "код продукта", "вид страхования (код", "суғурта тури коди",
                     "sug'urta turi kodi", "маҳсулот коди", "mahsulot kodi", "product code"),
    "risks": ("страховые риски", "застрахованные риски", "страховым случаем является", "страховыми случаями являются",
              "страховым случаем признается", "страховыми случаями признаются", "страховые случаи",
              "перечень рисков", "страхование производится на случай", "суғурта хавфлари", "sug'urta xavflari",
              "суғурта ҳодисаси", "sug'urta hodisasi", "суғурта ҳодисалари", "sug'urta hodisalari",
              "суғурта ҳодисаси деб", "sug'urta hodisasi deb", "insured risks", "insured perils", "perils insured",
              "risks covered", "covered risks", "insured events", "insured event",
              # личное страхование: «Страховой случай – травма или смерть…», раздел «Страховое покрытие»
              "страховой случай", "страховое покрытие", "суғурта қоплами", "sug'urta qoplami", "insurance cover"),
    "exclusions": ("исключения из страхового покрытия", "исключения из страхования", "исключения",
                   "не являются страховыми случаями", "не является страховым случаем",
                   "не признаются страховыми случаями", "страховщик не несет ответственности",
                   "страховщик освобождается от", "не подлежит возмещению", "не возмещается",
                   "истиснолар", "istisnolar", "суғурта ҳодисаси ҳисобланмайди", "sug'urta hodisasi hisoblanmaydi",
                   "суғурта ҳодисалари ҳисобланмайди", "sug'urta hodisalari hisoblanmaydi", "exclusions",
                   "not covered", "shall not be liable"),
    "territory": ("территория страхования", "территория действия договора", "территория покрытия",
                  "суғурта ҳудуди", "sug'urta hududi", "territory", "territorial limits", "geographical limits"),
    "special": ("особые условия", "специальные условия", "дополнительные условия", "оговорки",
                "махсус шартлар", "maxsus shartlar", "алоҳида шартлар", "alohida shartlar", "қўшимча шартлар",
                "qo'shimcha shartlar", "special conditions", "special terms", "additional conditions", "endorsements"),
    "items": ("перечень застрахованного имущества", "перечень имущества", "список имущества",
              "суғурталанган мол-мулк рўйхати", "sug'urtalangan mol-mulk ro'yxati", "мол-мулк рўйхати",
              "mol-mulk ro'yxati", "schedule of insured property", "schedule of property", "list of insured property"),
    "insured_event": ("страховым случаем является", "страховым случаем признается", "страховой случай",
                      "суғурта ҳодисаси деб", "sug'urta hodisasi deb", "суғурта ҳодисаси", "sug'urta hodisasi",
                      "insured event"),
    "payment": ("порядок уплаты страховой премии", "порядок оплаты", "порядок уплаты", "график платежей",
                "график уплаты", "график оплаты", "сроки уплаты", "уплачивается", "оплачивается",
                "тўлов жадвали", "to'lov jadvali", "тўлаш тартиби", "to'lash tartibi", "тўланади", "to'lanadi",
                "payment schedule", "schedule of payments", "payment terms", "payable"),
}
L["parties"] = {
    "insurer": ("страховщик", "суғурталовчи", "sug'urtalovchi", "insurer"),
    "policyholder": ("страхователь", "суғурта қилдирувчи", "sug'urta qildiruvchi", "policyholder", "the insured"),
    "beneficiary": ("выгодоприобретателем является", "выгодоприобретатель", "наф олувчи", "naf oluvchi", "beneficiary"),
    "pledger": ("залогодатель", "гаровга қўювчи", "garovga qo'yuvchi", "pledgor", "pledger"),
}
_LAB = {k: sorted(_F(v), key=len, reverse=True) for k, v in L.items() if k != "parties"}
_PARTY_LAB = {k: sorted(_F(v), key=len, reverse=True) for k, v in L["parties"].items()}
_ALL_LAB = sorted({x for v in _LAB.values() for x in v} | {x for v in _PARTY_LAB.values() for x in v},
                  key=len, reverse=True)

# «именуемое в дальнейшем «Страхователь»», «бундан буён «Суғурталовчи» деб юритилади», «hereinafter the Insurer»
_HEREAFTER = re.compile(r"(?:именуем\w*(?:\s+в\s+дальнейшем)?|далее(?:\s+по\s+тексту)?|бундан\s+буён|бундан\s+буен|"
                        r"bundan\s+buyon|кейинги\s+ўринларда|keyingi\s+o'?rinlarda|hereinafter(?:\s+referred\s+to)?"
                        r"(?:\s+as)?|hereinafter\s+called)\s*(?:[-–—:]\s*)?(?:matnda\s+|матнда\s+)?[«\"'“„]?\s*"
                        r"(страховщик\w*|страховател\w*|выгодоприобретател\w*|залогодател\w*|суғурталовчи\w*|"
                        r"sug'urtalovchi\w*|суғурта\s+қилдирувчи\w*|sug'urta\s+qildiruvchi\w*|наф\s+олувчи\w*|"
                        r"naf\s+oluvchi\w*|гаровга\s+қўювчи\w*|garovga\s+qo'yuvchi\w*|(?:the\s+)?insurer|"
                        r"(?:the\s+)?policyholder|(?:the\s+)?insured|(?:the\s+)?beneficiary|(?:the\s+)?pledgor)",
                        re.I)
_ROLE_OF = (("insurer", ("страховщик", "суғурталовчи", "sug'urtalovchi", "insurer")),
            ("policyholder", ("страховател", "суғурта қилдирувчи", "sug'urta qildiruvchi", "policyholder", "insured")),
            ("beneficiary", ("выгодоприобретател", "наф олувчи", "naf oluvchi", "beneficiary")),
            ("pledger", ("залогодател", "гаровга", "garovga", "pledgor")))
# где кончается название стороны: реквизиты, представитель, основание
_PARTY_CUT = re.compile(r"\s*(?:,|\(|;)?\s*(?:в\s+лице|в\s+лице\s+|действующ\w*|на\s+основании|инн\b|стир\b|stir\b|"
                        r"inn\b|tin\b|адрес|манзил|manzil|р/с|х/р|x/r|тел\.?|телефон|represented|acting|директор\w*|"
                        r"direktor\w*|раҳбари|rahbari|номидан|nomidan|паспорт|pasport|passport|пинфл|jshshir)",
                        re.I)
_CLAUSE_START = re.compile(r"(?:\n|с\s+одной\s+стороны\s*,?|с\s+другой\s+стороны\s*,?|бир\s+томондан\s*,?|"
                           r"bir\s+tomondan\s*,?|иккинчи\s+томондан\s*,?|ikkinchi\s+tomondan\s*,?|"
                           r"(?<=[\s,])и\s|(?<=[\s,])ва\s|(?<=[\s,])va\s|(?<=[\s,])and\s|;|\bмежду\b|\borasida\b|"
                           r"\bbetween\b|\bбилан\b|\bbilan\b)", re.I)

# застрахованные риски и исключения: код → стебли слов (все написания); короткие — только целым словом
RISK_WORDS = (
    ("all_risks", ("все риски", "от всех рисков", "барча хавф", "barcha xavf", "all risks")),
    ("fire", ("пожар", "огонь", "возгоран", "ёнғин", "yong'in", "fire")),
    ("lightning", ("молни", "чақмоқ", "chaqmoq", "lightning")),
    ("explosion", ("взрыв", "портлаш", "portlash", "explosion")),
    ("water", ("залив", "повреждение водой", "воздействие воды", "протечк", "сув босиш", "suv bosish",
               "сув таъсир", "suv ta'sir", "water damage", "escape of water")),
    ("natural", ("стихийн", "табиий офат", "tabiiy ofat", "natural disaster", "natural perils", "act of god")),
    ("earthquake", ("землетрясен", "зилзила", "zilzila", "earthquake")),
    ("flood", ("наводнен", "паводок", "паводк", "тошқин", "toshqin", "flood")),
    ("storm", ("буря", "бури", "ураган", "смерч", "шторм", "бўрон", "bo'ron", "довул", "dovul", "storm",
               "hurricane", "windstorm")),
    ("hail", ("град", "дўл", "do'l", "hail")),
    ("landslide", ("оползен", "обвал", "сел", "кўчки", "ko'chki", "landslide", "mudflow")),
    ("theft", ("кража", "краж", "хищен", "грабеж", "разбо", "ўғирлик", "o'g'irlik", "талон", "talon", "theft",
               "burglary", "robbery")),
    ("third_party", ("противоправн", "третьих лиц", "учинчи шахс", "uchinchi shaxs", "malicious", "вандал",
                     "vandal")),
    ("vehicle_impact", ("наезд", "impact by vehicle", "vehicle impact")),
    ("aircraft", ("летательн", "учиш аппарат", "uchish apparat", "aircraft")),
    ("glass", ("бой стекол", "стекол", "стёкол", "ойна", "oyna", "glass")),
    ("breakdown", ("поломк", "механическ", "machinery breakdown", "бузилиш", "buzilish")),
    ("electrical", ("короткое замыкание", "короткого замыкания", "электрическ", "қисқа туташув", "qisqa tutashuv",
                    "electrical")),
    ("collision", ("дтп", "дорожно-транспорт", "столкновен", "йўл-транспорт", "yo'l-transport", "collision")),
    # личное страхование (классы 1 и 2): временная утрата трудоспособности — раньше инвалидности (общее слово)
    ("death", ("смерт", "ўлим", "o'lim", "вафот", "vafot", "death")),
    ("temp_disability", ("временной утрат", "временная утрат", "временную утрат", "временной потер",
                         "временная потер", "временной нетрудоспособ", "временная нетрудоспособ",
                         "вақтинча меҳнат қобилият", "vaqtincha mehnat qobiliyat", "вақтинча ногирон",
                         "temporary disability", "temporary incapacity", "temporary loss of working")),
    ("disability", ("инвалидност", "стойкой утрат", "стойкая утрат", "стойком повреждени", "стойкое повреждени",
                    "ногиронлик", "nogironlik", "permanent disability", "disablement", "disability")),
    # «шикаст» не берём: в узбекском договоре имущества «шикастланиш» — повреждение имущества
    ("injury", ("травм", "телесн", "жароҳат", "jarohat", "injur", "bodily harm")),
    ("illness", ("заболеван", "болезн", "касаллик", "kasallik", "illness", "sickness", "disease")),
)
EXCLUSION_WORDS = (
    ("war", ("военн", "войн", "уруш", "urush", "war", "hostilit")),
    ("terrorism", ("террор", "terror")),
    ("nuclear", ("ядерн", "радиац", "радиоактив", "ядро", "yadro", "nuclear", "radioactiv", "radiation")),
    ("riots", ("забастов", "беспоряд", "бунт", "мятеж", "ғалаён", "g'alayon", "strike", "riot", "civil commotion")),
    ("intent", ("умысл", "умышлен", "қасддан", "qasddan", "intentional", "wilful", "willful", "deliberate")),
    ("gross_negligence", ("грубой неосторожност", "грубая неосторожност", "gross negligence")),
    ("wear", ("износ", "коррози", "ветхост", "ржав", "эскир", "eskir", "занг", "zang", "wear and tear", "corrosion",
              "gradual deterioration")),
    ("confiscation", ("конфиска", "арест", "реквизиц", "национализ", "мусодара", "musodara", "confiscation",
                      "requisition", "nationalis", "nationaliz")),
    ("defects", ("дефект", "брак", "нуқсон", "nuqson", "latent defect", "inherent vice")),
    ("consequential", ("косвенн", "упущенн", "неустойк", "билвосита", "bilvosita", "consequential loss",
                       "loss of profit")),
    ("cyber", ("кибер", "cyber")),
    ("mould", ("плесен", "грибок", "mould", "mold")),
    ("pollution", ("загрязнен", "ифлослан", "ifloslan", "pollution", "contamination")),
    ("intoxication", ("алкогол", "опьянен", "наркот", "маст ҳол", "mast hol", "intoxicat")),
    # личное страхование
    ("doping", ("допинг", "doping")),
    ("suicide", ("самоубийств", "суицид", "ўз жонига қасд", "o'z joniga qasd", "suicide")),
    ("self_harm", ("членовредительств", "ўзига шикаст", "o'ziga shikast", "self-inflicted", "self-harm",
                   "self inflicted")),
    ("crime", ("преступлени", "уголовно наказуем", "жиноят", "jinoyat", "criminal act", "crime")),
)


def _F2(xs) -> tuple:
    """Свёртка слов словаря двумя способами: узбекские правила по слову решает fold() и может принять
    узбекское слово за английское («shaxslarning» оканчивается на -ing) — второй вариант без них."""
    return tuple(sorted(set(_F(xs)) | {D.fold(x, "en") for x in xs if D.fold(x, "en")}))


_RISK_F = [(c, _F2(ws)) for c, ws in RISK_WORDS]
_EXCL_F = [(c, _F2(ws)) for c, ws in EXCLUSION_WORDS]
RISK_CODES = tuple(c for c, _ in RISK_WORDS) + ("other",)
EXCLUSION_CODES = tuple(c for c, _ in EXCLUSION_WORDS) + ("other",)

_SINGLE = _F(("единовременно", "единовременным платежом", "единовременной оплатой", "одним платежом",
              "бир марталик", "bir martalik", "бир йўла", "bir yo'la", "lump sum", "single payment",
              "in one payment", "one instalment", "one installment"))
_INSTAL = _F(("в рассрочку", "рассрочк", "частями", "по графику", "график платежей", "график уплаты",
              "бўлиб-бўлиб", "bo'lib-bo'lib", "бўлиб тўлаш", "bo'lib to'lash", "тўлов жадвали", "to'lov jadvali",
              "installment", "instalment", "payment schedule", "schedule of payments", "in instalments"))
_PAY_WORDS = _F(("взнос", "платеж", "платёж", "оплат", "уплат", "тўлов", "to'lov", "тўлан", "to'lan", "payment",
                 "instal", "install", "payable"))
_TOTAL_WORDS = _F(("итого", "всего", "жами", "jami", "total", "барчаси", "barchasi"))
_NAME_HEAD = _F(("наименование", "объект", "имущество", "номи", "nomi", "объекти", "obyekti", "name",
                 "description", "item", "property"))
_SUM_HEAD = _F(("страховая сумма", "суғурта суммаси", "sug'urta summasi", "sum insured", "сумма", "summa", "sum"))
_NOTICE_CTX = _F(("уведом", "сообщ", "извест", "хабар", "xabar", "notify", "inform", "report"))
# «направить Страховщику письменное заявление» — тоже уведомление (оба слова в строке)
_NOTICE_PAIR = (_F(("направ", "юбор", "yubor", "submit", "send")), _F(("заявлен", "ариза", "ariza", "claim")))
_EVENT_CTX = _F(("случа", "ущерб", "событ", "ҳодиса", "hodisa", "loss", "claim", "event", "occurrence"))
_TERM_EXCLUDE = re.compile(r"(уплат|оплат|рассроч|платеж|платёж|выплат|уведом|рассмотр|претензи|эксплуатац|"
                           r"стаж|гарант|давност|to'lov|тўлов|payment|notify)", re.I)
_TYPE_UNCOND = _F(("безусловн", "shartsiz", "шартсиз", "unconditional"))
_TYPE_COND = _F(("условн", "shartli", "шартли", "conditional"))
_FR_RISK = re.compile(r"(?:по\s+риск\w*|по\s+страховому\s+риску|по\s+каждому\s+случаю\s+по\s+риску|"
                      r"хавфи\s+бўйича|xavfi\s+bo'yicha|for\s+(?:the\s+)?(?:risk|peril)\s+(?:of\s+)?)\s*[«\"]?"
                      r"([^;,.»\"]{3,60})", re.I)
_CODE = re.compile(r"(?<!\d)(\d{3,4})(?!\d)")
# код продукта в названии вида страхования: «Имущество в залоге (код 0801)», «… kodi 0801», «product code 0801»
_CODE_IN_NAME = re.compile(r"(?i)(?:код|kod|kodi|коди|code)\s*[:№]?\s*(\d{4})(?!\d)")
_CONTRACT_NO = re.compile(r"(?:№|(?<!\w)N[o°º]\.?|(?<!\w)No\.?|(?<!\w)raqami|(?<!\w)рақами|(?<!\w)number)\s*:?\s*"
                          r"([A-Za-zА-Яа-яЁёЎўҚқҒғҲҳ0-9][\w\-/.]{0,38}[\w])|(?:№|(?<!\w)N[o°º]\.?)\s*:?\s*(\d)", re.I)


# ------------------------------------------------------------------ строки документа

_SPLIT_CLAUSE = re.compile(r"(?<=[.;:])\s+(?=\d{1,2}(?:\.\d{1,2}){1,3}\.?\s)")


_CLAUSE_NO = re.compile(r"^\s*(?:\d{1,2}(?:\.\d{1,2})+\.?|\d{1,2}[.)]|[-•–—]|[a-zа-я]\))\s")
JOIN_MAX = 400


def _unwrap(lines: list) -> list:
    """PDF: строка, перенесённая по ширине страницы, склеивается с продолжением — если предыдущая не кончается
    точкой, двоеточием или «;», а следующая не начинается с номера пункта, пункта списка, подписи или заголовка."""
    out = []
    for ln in lines:
        prev = out[-1] if out else None
        if prev is not None and not re.search(r"[.:;!?]\s*$", prev) and not _CLAUSE_NO.match(ln)                 and not _is_heading(prev) and not _is_heading(ln) and len(prev) + len(ln) < JOIN_MAX:
            f = re.sub(r"^(?:\d+\s+|[a-zа-я]\s+)+", "", D.fold(ln))
            if not any(f.startswith(lab + " ") or f == lab for lab in _ALL_LAB):
                out[-1] = prev + " " + ln
                continue
        out.append(ln)
    return out


def lines_of(text: str, tables: Optional[list] = None, pdf: bool = False) -> list:
    """Строки для поиска подписей: абзацы (длинный абзац режется по номерам пунктов «3.1.») и строки таблиц.
    pdf — текст PDF: перенесённые по ширине строки склеиваются (_unwrap)."""
    out = []
    raw = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    for ln in (_unwrap(raw) if pdf else raw):
        out.extend(p.strip() for p in _SPLIT_CLAUSE.split(ln) if p.strip())
    for t in tables or []:
        for row in t.get("rows") or []:
            s = " | ".join(str(c or "").strip() for c in row)
            if s.strip(" |"):
                out.append(s)
    return out


def _plain(s: str) -> str:
    """Текст PDF: неразрывные пробелы — пробелы, мягкий перенос в конце строки — склейка, внутри слова — дефис
    (pymupdf отдаёт дефис «45-ИМ», «2026-yil» мягким переносом U+00AD)."""
    s = str(s or "").replace(" ", " ").replace(" ", " ").replace(" ", " ")
    # «;» в PDF иногда приходит греческим знаком вопроса U+037E, дефис — U+2010/U+2011
    s = s.replace(";", ";").replace("‐", "-").replace("‑", "-")
    return re.sub(r"­\s*\n\s*", "", s).replace("­", "-")


class _Doc:
    """Строки документа и их свёрнутое написание (считается один раз)."""

    def __init__(self, text: str, tables: Optional[list], pdf: bool = False):
        text = _plain(text)
        tables = [dict(t, rows=[[_plain(c) for c in r] for r in t.get("rows") or []]) for t in tables or []]
        self.text = text or ""
        self.tables = tables or []
        self.lines = lines_of(text, tables, pdf)
        self.F = []
        for i, ln in enumerate(self.lines):
            if i % 200 == 0:
                D.tick()
            self.F.append(D.fold(ln))
        head = [ln.strip() for ln in self.text.splitlines() if ln.strip()][:HEAD_LINES]
        self.head = head
        self.head_F = [D.fold(x) for x in head]


def _prefix_words(prefix: str) -> int:
    """Сколько слов стоит перед подписью, не считая номера пункта («3 1», «a», «б»)."""
    return len([w for w in prefix.split() if not re.fullmatch(r"\d+|[a-zа-я]", w)])


def _tail(line: str, pos: int, lab: str) -> str:
    """Исходный текст строки после подписи (свёртка меняет длину — берём позицию по карте fold_map)."""
    f, idx = D.fold_map(line)
    end = pos + len(lab) - 1
    start = (idx[end] + 1) if 0 <= end < len(idx) else len(line)
    # буквы, которые свёртка убирает (ь, ъ, апостроф), остаются хвостом подписи: «страхователь» → «…ль»
    while start < len(line) and line[start] in "ьЬъЪ'ʻʼ’‘`":
        start += 1
    return line[start:]


def _has_sep(tail: str) -> bool:
    return bool(re.match(r"^\s*(?:\([^)]{0,60}\))?\s*[:|—–\-=]", tail or ""))


def _strip_sep(tail: str) -> str:
    return re.sub(r"^[\s:|—–\-=*»\"')]+", "", tail or "").strip()


def find(doc: _Doc, labels: list, max_prefix: int = 0, sep: bool = False, start: int = 0):
    """Строки с подписью: (номер строки, хвост после подписи). Подпись — целыми словами, перед ней
    не больше max_prefix слов (номер пункта не считается); sep — после подписи обязателен «:», «|» или тире."""
    for i in range(start, len(doc.lines)):
        if i % 200 == 0:
            D.tick()
        f = doc.F[i]
        for lab in labels:
            p = f.find(lab)
            hit = None
            while p >= 0:
                if (p == 0 or f[p - 1] == " ") and (p + len(lab) == len(f) or f[p + len(lab)] == " ") \
                        and _prefix_words(f[:p]) <= max_prefix:
                    hit = p
                    break
                p = f.find(lab, p + 1)
            if hit is None:
                continue
            tail = _tail(doc.lines[i], hit, lab)
            if sep and not _has_sep(tail):
                continue
            yield i, tail
            break


def _next_line(doc: _Doc, i: int) -> str:
    return doc.lines[i + 1] if i + 1 < len(doc.lines) else ""


def _starts_label(doc: _Doc, i: int) -> bool:
    """Строка начинается с какой-либо известной подписи (значит, это уже другое поле)."""
    f = re.sub(r"^(?:\d+\s+|[a-zа-я]\s+)+", "", doc.F[i])
    return any(f.startswith(lab + " ") or f == lab for lab in _ALL_LAB)


# ------------------------------------------------------------------ деньги и числа

_DATE_BLANK = (re.compile(r"\d{1,2}[./]\d{1,2}[./]\d{2,4}"), re.compile(r"\d{4}-\d{1,2}-\d{1,2}"),
               re.compile(r"\d{4}\s*[-–]?\s*(?:йил|yil|г\.|год|года|году|year)\w*", re.I),
               re.compile(r"[«\"]?\d{1,2}[»\"]?\s*[-–]?\s*[a-zа-яёўқғҳ']{3,}\s+\d{4}", re.I))
_REF_BLANK = (re.compile(r"(?:№|No\.?|N)\s*[\w/\-.]+"),
              re.compile(r"\b(?:п\.|пункт\w*|статья|ст\.|band\w*|modda\w*|банд\w*|модда\w*|section|clause)\s*[\d.]+",
                         re.I),
              re.compile(r"(?<![\d.,])\d{1,2}(?:\.\d{1,2})+\.(?=\s|$)"),
              re.compile(r"\d+(?:[.,]\d+)?\s*(?:%|процент\w*|foiz\w*|фоиз\w*|percent)", re.I),
              re.compile(r"(?:инн|стир|stir|inn|tin)\s*[:№]?\s*\d+", re.I))
_SCALE = br.SCALE                    # множители «млн/млрд» — общие с branch_request.amount
_CURRENCY = (("USD", re.compile(r"usd|\$|долл|dollar|доллар", re.I)), ("EUR", re.compile(r"eur|€|евро|yevro", re.I)),
             ("RUB", re.compile(r"\brub|руб", re.I)),
             ("UZS", re.compile(r"сум\b|сўм|so['ʻ‘’`]?m\b|uzs|sum\b", re.I)))


def _blank(s: str, rxs) -> str:
    for rx in rxs:
        s = rx.sub(lambda m: " " * len(m.group(0)), s)
    return s


def money(text, min_value: float = MIN_MONEY) -> Optional[dict]:
    """Сумма из текста после подписи: {"value", "currency"}. Даты, номера пунктов, проценты и ИНН затираются;
    сумма прописью в скобках не читается (если цифр вне скобок нет — берутся цифры в скобках)."""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return {"value": float(text), "currency": None} if text >= min_value else None
    raw = _blank(_blank(str(text or ""), _DATE_BLANK), _REF_BLANK)
    outside = re.sub(r"\([^)]{0,400}\)", lambda m: " " * len(m.group(0)), raw)
    for s in (outside, raw):
        for m in br._AMOUNT.finditer(s):
            v = br.amount(m.group(0))
            if v is None:
                continue
            v *= br.scale_of(s[m.end():m.end() + 30])
            if v < min_value:
                continue
            around = raw[max(0, m.start() - 8):m.start()] + " " + raw[m.end():m.end() + 80]
            cur = next((c for c, rx in _CURRENCY if rx.search(around)), None)
            return {"value": round(v, 2), "currency": cur}
    return None


def tariff(tail: str, sep: bool) -> Optional[float]:
    """Тариф в % годовых: число со знаком % (или «процент», «foiz», «percent»); без знака — только в строке
    «Тариф: 0,3» (сразу после подписи)."""
    s = str(tail or "")
    m = re.search(r"(?<![\d.,])(\d{1,3}(?:[.,]\d+)?)\s*(?:%|процент\w*|foiz\w*|фоиз\w*|percent)", s, re.I)
    if m:
        return br.tariff(m.group(1))
    if sep:
        m = re.match(r"^\s*(?:\([^)]{0,60}\))?\s*[:|—–\-=]\s*(\d{1,3}(?:[.,]\d+)?)(?![\d.,])(?!\s*\d)"
                     r"(?!\s*(?:сум|сўм|so'm|uzs))", s, re.I)
        if m:
            return br.tariff(m.group(1))
    return None


def months_days(months: int) -> int:
    """Срок в месяцах → дни (как в разборе договора до 30.09.2026: 12 месяцев — 365 дней)."""
    return 365 if months == 12 else int(round(months * 365 / 12))


# ------------------------------------------------------------------ распознавание вида

def _no_akt(f: str) -> str:
    """«акт» — стоп-слово только целым словом («контракт», «фактический» — не акт)."""
    return re.sub(r"(?<![a-z])akt(?=[a-z])", "_", f)


# заголовок в две строки: «Договор №____» и на следующей строке «Страхования спортсменов от несчастных случаев»
_TITLE_HEAD = re.compile(r"(?i)^\s*(?:договор|полис|contract|policy|agreement)\s*(?:(?:№|no\.?|n)\s*[\w/\-.]*)?\s*$")
_TITLE_TAIL = re.compile(r"^(?:strahovaniya|strahovanie|strahovaniyu|of\s+insurance|insurance)\b")


def _title_lines(head: list, head_F: list, n: int = 15):
    """Строки-кандидаты в заголовок: каждая строка начала, а короткое «Договор №…» — ещё и вместе со следующей
    строкой, если та продолжает заголовок («Страхования …»)."""
    for k in range(min(n, len(head))):
        yield head[k], head_F[k]
        if k + 1 < len(head) and _TITLE_HEAD.match(head[k]) and _TITLE_TAIL.match(head_F[k + 1]):
            yield head[k] + " " + head[k + 1], head_F[k] + " " + head_F[k + 1]


def title_of(doc: _Doc) -> Optional[str]:
    """Строка-заголовок договора в начале документа: «ДОГОВОР СТРАХОВАНИЯ ИМУЩЕСТВА № 15/2026»,
    «МОЛ-МУЛК СУҒУРТАСИ ШАРТНОМАСИ», «INSURANCE POLICY», «Договор №____ / Страхования спортсменов …» (две строки).
    Не больше 14 слов, не предложение письма."""
    for raw, f in _title_lines(doc.head, doc.head_F):
        if len(raw) > 200 or len(f.split()) > 14:
            continue
        if _TITLE_STOP_RX.search(_no_akt(f)):
            continue
        for rx in TITLE_RX:
            m = rx.search(f)
            if not m or _prefix_words(f[:m.start()]) > 3:
                continue
            # строка с точкой в конце и строчными буквами — предложение («Просим заключить договор …»)
            if raw.rstrip().endswith(".") and raw != raw.upper():
                continue
            return raw
    return None


# заголовок заявления: «ЗАЯВЛЕНИЕ НА СТРАХОВАНИЕ …», «АРИЗА», «Sugʻurta qilish uchun ARIZA»
_APP_RX = re.compile(r"(?:^|\s)(?:zayavlenie|ariza|arizasi|application\s+for\s+insurance|insurance\s+application)"
                     r"(?:\s|$)")


def _app_title(raw: str, f: str, pos: int) -> bool:
    if len(raw) > 160 or len(f.split()) > 10 or ":" in raw or not _APP_RX.search(f):
        return False
    return raw == raw.upper() or pos < 3


def title_kind(text: str, tables: Optional[list] = None) -> Optional[str]:
    """Вид документа по заголовку в начале: contract (договор, полис) | application (заявление) | None.
    Заголовок сильнее строк «подпись: значение» — полис со строками как в бланке остаётся полисом."""
    # только начало документа: весь текст здесь сворачивать незачем
    # строки бланка запроса («Стандарт суғурта шартномаси шартларини …:») и подписи с двоеточием — не заголовок
    head = [ln.strip() for ln in _plain(text).splitlines() if ln.strip()][:HEAD_LINES * 2]
    head = [ln for ln in head if not ln.rstrip(" *").endswith(":") and not br._row_label(ln)][:HEAD_LINES]
    doc = SimpleNamespace(head=head, head_F=[D.fold(x) for x in head])
    title = title_of(doc)
    for pos, (raw, f) in enumerate(zip(doc.head[:15], doc.head_F[:15])):
        if title is not None and (raw == title or title.startswith(raw + " ")):
            return "contract"
        if _app_title(raw, f, pos):
            return "application"
    return "contract" if title else None


def detect(doc: _Doc) -> bool:
    """Договор страхования: заголовок в начале и хотя бы две подписи условий, или без заголовка —
    не меньше четырёх разных подписей, среди которых сумма или премия и сторона договора."""
    found = set()
    for key in ("sum_insured", "sum_insured_total", "premium", "term", "object", "franchise", "tariff", "risks"):
        for _i, _t in find(doc, _LAB[key], max_prefix=4):
            found.add("sum_insured" if key == "sum_insured_total" else key)
            break
    party = any(True for _ in find(doc, _PARTY_LAB["policyholder"] + _PARTY_LAB["insurer"], max_prefix=6))
    if title_of(doc):
        return len(found) >= 2
    return len(found) >= 4 and bool(found & {"sum_insured", "premium"}) and party


# ------------------------------------------------------------------ извлечение (сырые значения)

def _first_money(doc: _Doc, keys: tuple) -> Optional[str]:
    for key in keys:
        for i, tail in find(doc, _LAB[key], max_prefix=4):
            if _is_heading(doc.lines[i]):
                continue
            got = money(tail)
            if got is None and len(re.sub(r"[\W\d_]", "", tail)) < 30 and i + 1 < len(doc.lines)                     and not _starts_label(doc, i + 1) and not _is_heading(doc.lines[i + 1]):
                tail = tail + " " + _next_line(doc, i)
                got = money(tail)
            if got:
                return tail
    return None


def _block(doc: _Doc, i: int, tail: str, max_lines: int = 3) -> str:
    """Значение поля: хвост строки и продолжение на следующих строках (пока не началась другая подпись)."""
    parts = [_strip_sep(tail)] if _strip_sep(tail) else []
    k = i + 1
    while k < len(doc.lines) and len(parts) < max_lines and sum(len(p) for p in parts) < MAX_TEXT:
        if _starts_label(doc, k) or _is_heading(doc.lines[k]):
            break
        nxt = doc.lines[k]
        # продолжение — только если значения ещё нет, фраза оборвана на «:» или «,», или это пункт списка
        if parts and not (parts[-1].endswith((":", ",")) or re.match(r"^\s*(?:[-•–—]|[a-zа-я0-9]{1,2}\))", nxt)):
            break
        parts.append(nxt.strip())
        k += 1
    return "\n".join(parts)[:MAX_TEXT]


_HEADING = re.compile(r"^\s*\d{1,2}\.?\s+[A-ZА-ЯЁЎҚҒҲ][^a-zа-яё]{2,}$")


def _is_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 120:
        return False
    if _HEADING.match(s):
        return True
    letters = re.sub(r"[^A-Za-zА-Яа-яЁёЎўҚқҒғҲҳ]", "", s)
    return len(letters) >= 6 and letters == letters.upper() and len(s.split()) <= 10


def _top_no(line: str) -> Optional[str]:
    m = re.match(r"^\s*(\d{1,2})[.)]", line)
    return m.group(1) if m else None


def _section(doc: _Doc, i: int, tail: str, stop_keys=(), max_lines: int = 40) -> list:
    """Раздел договора со строки i: хвост строки и следующие строки — до заголовка другого раздела,
    пункта с другим верхним номером или подписи из stop_keys."""
    out = [_strip_sep(tail)] if _strip_sep(tail) else []
    top = _top_no(doc.lines[i])
    stops = [lab for k in stop_keys for lab in _LAB[k]]
    list_only = top is None and bool(out) and not out[-1].endswith(":")
    for k in range(i + 1, min(len(doc.lines), i + 1 + max_lines)):
        ln = doc.lines[k]
        f = doc.F[k]
        if list_only and not re.match(r"^\s*(?:[-•–—]|[a-zа-я]\)|\d{1,2}\))", ln):
            break
        if any(re.search(r"(?:^|\s)" + re.escape(lab) + r"(?:\s|$)", f) for lab in stops):
            break
        t = _top_no(ln)
        if (t and top and t != top) or (_is_heading(ln) and k > i + 1):
            break
        if t and not top and _is_heading(ln):
            break
        out.append(ln.strip())
    return out


def _words_in(text: str, table) -> list:
    """Коды словаря (риски или исключения) в порядке появления в тексте."""
    texts = (" " + D.fold(text) + " ", " " + D.fold(text, "en") + " ")
    hits = []
    for code, words in table:
        best = None
        for w in words:
            for f in texts:
                if len(w) < 4:
                    m = re.search(r"(?<![a-z])%s(?![a-z])" % re.escape(w), f)
                    p = m.start() if m else -1
                else:
                    p = f.find(w)
                if p >= 0 and (best is None or p < best):
                    best = p
        if best is not None:
            hits.append((best, code))
    return [c for _p, c in sorted(hits)]


def _party_before(text: str, pos: int) -> Optional[str]:
    """Название стороны перед «именуемое в дальнейшем …»: от начала предложения или союза до этих слов."""
    # 600 знаков: перед «именуемое в дальнейшем» бывает длинная ссылка на лицензию («…серия СФ № …, выданной …»)
    seg = text[max(0, pos - 600):pos]
    cut = 0
    for m in _CLAUSE_START.finditer(seg):
        cut = m.end()
    seg = seg[cut:]
    m = _PARTY_CUT.search(seg)
    if m:
        seg = seg[:m.start()]
    seg = seg.strip(" ,;:(\t\n-–—")
    return seg[-200:] if seg else None


def _role(word: str) -> Optional[str]:
    w = D.norm(word)
    for code, stems in _ROLE_OF:
        if any(D.norm(s) in w for s in stems):
            return code
    return None


# определение термина в разделе «Определения»: «– физическое лицо, названное …», «– юридическое или физическое лицо, …»
_DEFINITION = re.compile(r"(?:физическ|юридическ|дееспособн|лицо\b|лица\b|сторона\b|любое\s|организаци\w*,|"
                         r"jismoniy\s+shaxs|yuridik\s+shaxs|жисмоний\s+шахс|юридик\s+шахс|"
                         r"a\s+(?:natural|legal)\s+person|any\s+person|the\s+person)", re.I)


def _parties(doc: _Doc, blank: Optional[set] = None) -> dict:
    """Стороны: сначала преамбула («ООО «X», именуемое в дальнейшем «Страхователь»»), затем строки
    «Страхователь: ООО «X»». Возвращаются строки как в документе — решает party() (юрлицо или гражданин).
    blank — сюда пишутся роли, у которых в бланке вместо названия подчёркивания («______, именуемая …»).
    Определения («Выгодоприобретатель – физическое лицо, названное …») стороной не считаются."""
    got = {}
    blank = set() if blank is None else blank
    pre = "\n".join(doc.head[:40])
    low = pre.replace("ʻ", "'").replace("’", "'").replace("‘", "'").replace("`", "'")
    for m in _HEREAFTER.finditer(low):
        D.tick()
        code = _role(m.group(1))
        if not code or code in got:
            continue
        name = _party_before(pre, m.start())
        if name and re.search(r"[A-Za-zА-Яа-яЁё]", name):
            got[code] = name
        elif name and blank_value(name):
            blank.add(code)
    for code, labs in _PARTY_LAB.items():
        if code in got:
            continue
        for i, tail in find(doc, labs, max_prefix=0, sep=True):
            sep = re.match(r"^\s*(?:\([^)]{0,60}\))?\s*([:|—–\-=])", tail)
            val = _strip_sep(tail)
            if sep and sep.group(1) in "—–-" and _DEFINITION.match(val):
                continue                   # «Выгодоприобретатель – физическое лицо, …» — определение термина
            if not re.search(r"[A-Za-zА-Яа-яЁёЎўҚқҒғҲҳ]{2}", val):
                val = _strip_sep(_next_line(doc, i)) if not val else val
            m = _PARTY_CUT.search(val)
            if m:
                val = val[:m.start()]
            val = val.strip(" ,;:")
            if val and blank_value(val):
                blank.add(code)
                continue
            if re.search(r"[A-Za-zА-Яа-яЁёЎўҚқҒғҲҳ]{2}", val) and not re.fullmatch(r"[_\s./]+", val):
                got[code] = val
                blank.discard(code)
                break
    return got


# ------------------------------------------------------------------ бланк договора (незаполненные поля)

_UNDER = re.compile(r"_{3,}|…{2,}|\.{6,}")
# пустая дата бланка: «____» ________ 20___г., «_____» ___________ 20___ года
_BLANK_DATE = re.compile(r"[«\"“]\s*_{2,}\s*[»\"”]\s*_{2,}\s*(?:(?:19|20)?_*\s*(?:г\.?|года?|йил\w*|yil\w*)?)")
# строки приказа, лицензии и приложения — не шапка договора: номер и дата там свои
_NOT_HEAD = re.compile(r"(?:prikaz|prilojen|licenz|litsenz|license|licence|buyruq|ilova|utverjdayu|tasdiqlayman)")
# поля бланка, по которым решается «это бланк»
TEMPLATE_KEYS = ("contract_no", "contract_date", "policyholder", "sum_insured", "premium", "term")
TEMPLATE_MIN = 3
BLANK_CODES = KEY_FIELDS + ("place",)


def blank_value(s) -> bool:
    """Пустое поле бланка: только подчёркивания и служебные слова («г. ____», «___» _____ 20___г., «(______)»)."""
    s = str(s or "")
    if not _UNDER.search(s):
        return False
    rest = _UNDER.sub(" ", s)
    rest = re.sub(r"(?i)(?<![A-Za-zА-Яа-яЁё])(?:г|гг|год|года|году|йил|yil|от|№|no|сум|сўм|so'm|с|по|до|dan|gacha)"
                  r"(?![A-Za-zА-Яа-яЁё])\.?", " ", rest)
    rest = re.sub(r"(?<!\d)(?:19|20)(?!\d)", " ", rest)
    return not re.search(r"[A-Za-zА-Яа-яЁёЎўҚқҒғҲҳ]{2,}|\d", rest)


def _blank_money(doc: _Doc, keys: tuple) -> bool:
    """Сумма бланка: у подписи вместо числа — подчёркивания («составляет ______ (______) сум»)."""
    for key in keys:
        for i, tail in find(doc, _LAB[key], max_prefix=4):
            head = tail[:160]
            u = _UNDER.search(head)
            if u and money(head) is None and not re.search(r"\d", head[:u.start()]):
                return True
    return False


def _blanks(doc: _Doc, raw: dict, title: Optional[str], party_blank: set) -> list:
    """Какие ключевые поля в документе стоят пустыми (подчёркивания вместо значения)."""
    out = []
    if not raw.get("contract_no") and title and re.search(r"(?:№|No\.?|N)\s*_{2,}", title):
        out.append("contract_no")
    head_line = None
    for raw_l, f in zip(doc.head[:12], doc.head_F[:12]):
        if len(raw_l) <= 200 and not _NOT_HEAD.search(f) and _BLANK_DATE.search(raw_l):
            head_line = raw_l
            break
    if not raw.get("contract_date") and head_line:
        out.append("contract_date")
    if not raw.get("place") and head_line and blank_value(head_line[:_BLANK_DATE.search(head_line).start()]):
        out.append("place")
    if "policyholder" in party_blank and not raw.get("policyholder"):
        out.append("policyholder")
    if not raw.get("sum_insured") and _blank_money(doc, ("sum_insured_total", "sum_insured")):
        out.append("sum_insured")
    if not raw.get("premium") and _blank_money(doc, ("premium",)):
        out.append("premium")
    if not raw.get("term") and not raw.get("term_months"):
        for ln, f in zip(doc.lines, doc.F):
            if len(_BLANK_DATE.findall(ln)) >= 2 and any(w in f for w in ("srok", "v silu", "muddat", "period",
                                                                           "deystv", "amal qil")):
                out.append("term")
                break
    return out


# ------------------------------------------------------------------ личное страхование (классы 1 и 2)

_PERSONAL = (("accident", _F2(("несчастн", "бахтсиз ҳодиса", "baxtsiz hodisa", "accident"))),
             ("health", _F2(("медицинск", "тиббий суғурта", "tibbiy sug'urta", "болезн", "заболеван", "касаллик",
                             "kasallik", "health insurance", "medical insurance"))))
_MANY_PERSONS = _F2(("застрахованным лицам", "застрахованных лиц", "список застрахованных", "застрахованные лица",
                     "sug'urtalangan shaxslar", "суғурталанган шахслар", "insured persons"))
PERSON_HINTS = ("accident", "health")


def personal_hint(text) -> Optional[str]:
    """Личное страхование по названию договора: несчастные случаи — accident (класс 1), болезни — health (класс 2)."""
    f = " " + D.fold(str(text or "")) + " "
    for code, words in _PERSONAL:
        if any(w in f for w in words):
            return code
    return None


def _person_object(product: Optional[str], many: bool, doc: _Doc) -> str:
    """Объект личного страхования: жизнь и здоровье застрахованных лиц (и кого — из названия: «спортсменов»)."""
    who = None
    m = re.match(r"(?i)^\s*страховани\w*\s+(.+?)\s+(?:от|на\s+случай)\s", str(product or ""))
    if m and re.fullmatch(r"[а-яё\- ]{3,60}", m.group(1).lower()) and not re.search(r"(?i)имуществ|ответствен",
                                                                                     m.group(1)):
        who = m.group(1).lower()
    sample = " ".join(doc.head_F[:15])
    if re.search(r"sug'urta|sugurta", sample):
        base = "sug'urtalangan shaxslarning hayoti va sog'lig'i" if many else "sug'urtalangan shaxsning hayoti va sog'lig'i"
    elif re.search(r"[ўқғҳ]", " ".join(doc.head[:15]).lower()):
        base = "суғурталанган шахсларнинг ҳаёти ва соғлиғи" if many else "суғурталанган шахснинг ҳаёти ва соғлиғи"
    elif not re.search(r"[а-яё]", " ".join(doc.head[:15]).lower()):
        base = "life and health of the insured persons" if many else "life and health of the insured person"
    else:
        base = "жизнь и здоровье застрахованных лиц" if many else "жизнь и здоровье застрахованного лица"
    return base + (" — " + who if who and many else "")


def product_from_title(title: Optional[str]) -> Optional[str]:
    """Название продукта из заголовка: «Договор №___ Страхования спортсменов от несчастных случаев» →
    «Страхование спортсменов от несчастных случаев»; номер договора и пустое поле номера отбрасываются."""
    if not title:
        return None
    s = re.sub(r"(?:№|(?<!\w)No\.?|(?<!\w)N(?=\s*[\d_]))\s*[\w/\-.]*", " ", title)
    s = re.sub(r"\s+", " ", _UNDER.sub(" ", s.replace("_", " "))).strip(" ,.-–—")
    m = re.match(r"(?i)^(?:договор\w*|полис\w*)\s+страховани\w*\s+(.+)$", s)
    if m:
        rest = m.group(1).strip()
        if rest.isupper():
            # «ИМУЩЕСТВА ЮРИДИЧЕСКИХ ЛИЦ (КАСКО)» — строчными, короткие сокращения остаются
            rest = " ".join(w if len(re.sub(r"\W", "", w)) <= 5 else w.lower() for w in rest.split())
        return ("Страхование " + rest)[:200]
    return s[:200] or None


# колонки таблицы застрахованных по профессиям (приложение к договору личного страхования)
_SCHED_COLS = (
    ("sum_total", _F(("страховая сумма всего", "общая страховая сумма", "жами суғурта суммаси", "jami sug'urta summasi",
                      "total sum insured"))),
    ("premium_total", _F(("страховая премия всего", "общая страховая премия", "жами суғурта мукофоти",
                          "jami sug'urta mukofoti", "total premium"))),
    ("premium_one", _F(("страховой платеж за одного", "страховой платёж за одного", "страховая премия за одного",
                        "премия на одного", "бир кишига", "bir kishiga", "premium per person"))),
    ("profession", _F(("профессия", "род занятий", "касби", "kasbi", "касб", "kasb", "occupation", "profession"))),
    ("count", _F(("количество", "число застрахованных", "сони", "soni", "number of insured", "headcount"))),
    ("personal_sum", _F(("персональная страховая сумма", "страховая сумма на одного", "индивидуальная страховая сумма",
                         "шахсий суғурта суммаси", "shaxsiy sug'urta summasi", "sum insured per person"))),
    ("rate", _F(("процентная ставка", "ставка", "тариф", "stavka", "tarif", "rate"))),
)
_NAME_COLS = _F(("фамилия", "ф и о", "фио", "имя", "familiya", "ism sharif", "исм шариф", "full name",
                 "name of the insured"))
SCHED_CODES = tuple(c for c, _ in _SCHED_COLS)
MAX_SCHED = 30


def _sched_num(s: str) -> Optional[float]:
    v = br.amount(str(s or "")) if re.search(r"\d", str(s or "")) else None
    return v


def _schedule(doc: _Doc) -> tuple:
    """Таблица застрахованных по профессиям: ({"columns", "items", "blank"} | None, число строк списка лиц).
    Список с фамилиями не читается: считаются только заполненные строки (без имён)."""
    sched, persons = None, None
    for t in doc.tables:
        rows = t.get("rows") or []
        for r, row in enumerate(rows[:3]):
            fs = [D.fold(str(c or "")) for c in row]
            # список лиц: колонка «Фамилия, имя…» и колонка номера по порядку (подписи сторон «Ф.И.О.» — не список)
            if any(any(w in f for w in _NAME_COLS) for f in fs) and \
                    any(re.match(r"^\s*(?:№|n\b|no\b|п/п|t/r|т/р)", str(c or "").lower()) for c in row):
                name_col = next(k for k, f in enumerate(fs) if any(w in f for w in _NAME_COLS))
                n = 0
                for row2 in rows[r + 1:]:
                    if name_col < len(row2) and re.search(r"[A-Za-zА-Яа-яЁё]{2}", str(row2[name_col] or "")) \
                            and not any(w in D.fold(str(row2[name_col])) for w in _TOTAL_WORDS):
                        n += 1
                persons = (persons or 0) + n
                break
            cols, used = {}, set()
            for k, f in enumerate(fs):
                for code, words in _SCHED_COLS:
                    if code not in used and any(w in f for w in words):
                        cols[k] = code
                        used.add(code)
                        break
            if sched is None and len(cols) >= 3 and used & {"profession", "count"}:
                items = []
                for row2 in rows[r + 1:]:
                    D.tick()
                    cells = [str(c or "").strip() for c in row2]
                    # строка нумерации колонок «1 | 2 | 3 …» — не данные
                    if all(re.fullmatch(r"\d{1,2}", c) for c in cells if c) and \
                            [int(c) for c in cells if c] == list(range(1, len([c for c in cells if c]) + 1)):
                        continue
                    it = {}
                    for k, code in cols.items():
                        if k >= len(cells) or not cells[k]:
                            continue
                        if code == "profession":
                            it[code] = cells[k][:120]
                        elif code == "rate":
                            it[code] = br.tariff(cells[k])
                        elif code == "count":
                            v = _sched_num(cells[k])
                            it[code] = int(v) if v and v == int(v) else None
                        else:
                            it[code] = _sched_num(cells[k])
                    it = {k: v for k, v in it.items() if v not in (None, "")}
                    if it and len(items) < MAX_SCHED:
                        items.append(it)
                sched = {"columns": [cols[k] for k in sorted(cols)], "items": items, "blank": not items}
                break
    return sched, persons


def _schedule_of(v) -> Optional[dict]:
    """Таблица застрахованных по профессиям — только известные колонки и числа (вход недоверенный)."""
    if not isinstance(v, dict):
        return None
    cols = [c for c in (v.get("columns") or []) if c in SCHED_CODES][:len(SCHED_CODES)]
    items = []
    for it in (v.get("items") or [])[:MAX_SCHED]:
        if not isinstance(it, dict):
            continue
        row = {}
        for c in SCHED_CODES:
            x = it.get(c)
            if c == "profession":
                x = _clean(x)
                if x:
                    row[c] = x[:120]
            elif isinstance(x, (int, float)) and not isinstance(x, bool) and x >= 0:
                row[c] = int(x) if c == "count" else float(x)
        if row:
            items.append(row)
    if not cols and not items:
        return None
    return {"columns": cols, "items": items, "blank": not items}


def _date_pos(raw: str) -> int:
    """Где в строке начинается первая дата (те же шаблоны, что branch_request.dates_in)."""
    s = re.sub(r"[«»\"“”„'‘’ʻʼ`]", " ", str(raw or "").lower().replace("ё", "е"))
    best = len(raw)
    for rx in (br._RX_UZ, br._RX_NUM, br._RX_ISO, br._RX_WORD, br._RX_EN):
        for m in rx.finditer(s):
            if br.dates_in(m.group(0)):
                best = min(best, m.start())
                break
    return best


def _head_date(doc: _Doc) -> tuple:
    """Дата договора и место заключения — из первых строк (строка с датой до условий договора)."""
    for raw, f in zip(doc.head[:10], doc.head_F[:10]):
        # преамбула (дата лицензии страховщика) и строки приказа или приложения — не дата договора
        if len(raw) > 200 or _NOT_HEAD.search(f):
            continue
        ds = br.dates_in(raw)
        if not ds:
            continue
        before = raw[:_date_pos(raw)]
        before = re.sub(r"(?i)\b(?:от|dated|сана|sana)\b\s*$", "", before).strip(" ,.;:-–—«\"")
        f = D.fold(before)
        place = before if (2 <= len(before) <= 60 and re.search(r"[A-Za-zА-Яа-я]", before)
                           and not any(rx.search(f) for rx in TITLE_RX) and "№" not in before) else None
        return ds[0], place
    return None, None


def _contract_no(doc: _Doc, title: Optional[str]) -> Optional[str]:
    # длинная строка — преамбула («… Лицензии … № 00058 …, заключили настоящий Договор»), номер там не договора
    cands = ([title] if title else []) + [raw for raw, f in zip(doc.head[:10], doc.head_F[:10])
                                         if len(raw) <= 200 and not _NOT_HEAD.search(f)
                                         and any(w in f for w in ("dogovor", "polis", "shartnoma", "contract",
                                                                  "policy"))]
    # PDF: номер перенесён на следующую строку («… В ЗАЛОГЕ №» / «INS-0801/045»); мягкий перенос — дефис
    for i, raw in enumerate(doc.head[:10]):
        if re.search(r"(?:№|(?<!\w)No\.?)\s*$", raw) and i + 1 < len(doc.head) and not _NOT_HEAD.search(doc.head_F[i]):
            cands.append(raw.rstrip() + " " + doc.head[i + 1].strip().split(" ")[0])
    for raw in cands:
        if not raw:
            continue
        raw = raw.replace("­", "-")
        m = _CONTRACT_NO.search(raw)
        if m:
            no = (m.group(1) or m.group(2) or "").strip(" .,")
            if no and not re.fullmatch(r"\d{9}|\d{14}", no):
                return no[:40]
    return None


def _items_from_tables(doc: _Doc) -> tuple:
    """Страховая сумма по частям из таблицы: колонка с «наименованием» и колонка со «страховой суммой»."""
    items, total = [], None
    for t in doc.tables:
        rows = t.get("rows") or []
        name_col = sum_col = None
        start = None
        for r, row in enumerate(rows[:5]):
            fs = [D.fold(str(c or "")) for c in row]
            sc = next((k for k, f in enumerate(fs) if any(lab in f for lab in _SUM_HEAD)), None)
            nc = next((k for k, f in enumerate(fs) if k != sc and any(w in f for w in _NAME_HEAD)), None)
            if sc is not None and nc is not None:
                name_col, sum_col, start = nc, sc, r + 1
                break
        if start is None:
            continue
        for row in rows[start:]:
            D.tick()
            if max(name_col, sum_col) >= len(row):
                continue
            name = _clean(row[name_col])
            got = money(str(row[sum_col] or ""))
            if not got:
                continue
            if name and any(w in D.fold(name) for w in _TOTAL_WORDS):
                total = got["value"]
                continue
            if name and len(items) < MAX_ITEMS:
                items.append({"name": name[:200], "sum": got["value"]})
        if items:
            break
    return items, total


def _items_from_lines(doc: _Doc) -> tuple:
    """Перечень имущества строками «1. Здание склада — 3 000 000 000 сум» после подписи перечня."""
    for i, tail in find(doc, _LAB["items"], max_prefix=4):
        items, total, gap = [], None, 0
        for k in range(i + 1, min(len(doc.lines), i + 1 + MAX_ITEMS + 5)):
            ln = doc.lines[k]
            got = money(ln)
            if not got:
                gap += 1
                if items and gap > 1:
                    break
                continue
            name = re.split(r"\s[-–—:|]\s|\s\d", re.sub(r"^\s*\d{1,3}[.)]\s*", "", ln), maxsplit=1)[0].strip(" -–—:|")
            if not re.search(r"[A-Za-zА-Яа-яЁё]", name):
                continue
            if any(w in D.fold(name) for w in _TOTAL_WORDS):
                total = got["value"]
                break
            items.append({"name": name[:200], "sum": got["value"]})
        if items:
            return items[:MAX_ITEMS], total
    return [], None


_SEG_SPLIT = re.compile(r";|,\s*(?=(?:втор|трет|четв|пят|шест|ikkinchi|uchinchi|to'rtinchi|иккинчи|учинчи|"
                        r"second|third|fourth|fifth)\w*)", re.I)


def _payments(doc: _Doc) -> tuple:
    """Порядок уплаты премии: (единовременно | в рассрочку | None, график [{"date", "amount"}])."""
    mode = None
    pays = []
    for i, tail in find(doc, _LAB["payment"], max_prefix=8):
        f_line = doc.F[i]
        if mode is None:
            if any(w in f_line for w in _INSTAL):
                mode = "installments"
            elif any(w in f_line for w in _SINGLE):
                mode = "single"
        block = [doc.lines[i]] + _section(doc, i, "", stop_keys=("risks", "exclusions", "franchise", "term",
                                                                  "territory"), max_lines=14)
        for ln in block:
            fl = D.fold(ln)
            if mode is None:
                if any(w in fl for w in _INSTAL):
                    mode = "installments"
                elif any(w in fl for w in _SINGLE):
                    mode = "single"
            table_row = " | " in ln
            if any(lab in fl for lab in _LAB["sum_insured"] + _LAB["object_value"]):
                continue
            for seg in (_SEG_SPLIT.split(ln) if not table_row else [ln]):
                if not seg:
                    continue
                fs = D.fold(seg)
                if not (table_row or any(w in fs for w in _PAY_WORDS) or re.match(r"^\s*(?:[-•–—]|\d{1,2}[.)])",
                                                                                   seg)):
                    continue
                ds = br.dates_in(seg)
                got = money(seg)
                if ds and got:
                    p = {"date": ds[0].isoformat(), "amount": got["value"]}
                    if p not in pays and len(pays) < MAX_PAYMENTS:
                        pays.append(p)
        if pays:
            break
    if not pays:
        pays = _payments_table(doc)
    if mode is None and len(pays) >= 2:
        mode = "installments"
    return mode, pays


_DATE_HEAD = _F(("дата", "срок", "до", "sana", "сана", "muddat", "муддат", "date", "due"))
_AMOUNT_HEAD = _F(("сумма", "размер", "взнос", "платеж", "summa", "сумма взноса", "miqdor", "миқдор", "amount",
                   "instal"))


def _payments_table(doc: _Doc) -> list:
    """График платежей таблицей: колонка даты и колонка суммы (заголовок таблицы — в первых строках)."""
    for t in doc.tables:
        rows = t.get("rows") or []
        for r, row in enumerate(rows[:3]):
            fs = [D.fold(str(c or "")) for c in row]
            dc = next((k for k, f in enumerate(fs) if any(re.search(r"(?<![a-z])%s" % re.escape(w), f)
                                                             for w in _DATE_HEAD)), None)
            ac = next((k for k, f in enumerate(fs) if k != dc and any(w in f for w in _AMOUNT_HEAD)), None)
            if dc is None or ac is None or any(any(lab in f for lab in _SUM_HEAD[:3]) for f in fs):
                continue
            pays = []
            for row2 in rows[r + 1:]:
                D.tick()
                if max(dc, ac) >= len(row2):
                    continue
                ds = br.dates_in(str(row2[dc] or ""))
                got = money(str(row2[ac] or ""))
                if ds and got and len(pays) < MAX_PAYMENTS:
                    pays.append({"date": ds[0].isoformat(), "amount": got["value"]})
            if pays:
                return pays
    return []


def _notice(doc: _Doc) -> Optional[str]:
    """Срок уведомления о страховом случае: строка с «уведомить … в течение N дней/часов»."""
    rx = re.compile(r"(\d{1,3})\s*(?:\([^)]{0,30}\)\s*)?(?:(рабоч\w*|календарн\w*|ish|иш|working|business)\s+)?"
                    r"(дн\w*|дней|суток|сутки|час\w*|kun\w*|кун\w*|soat\w*|соат\w*|days?|hours?)", re.I)
    for i, f in enumerate(doc.F):
        if i % 200 == 0:
            D.tick()
        told = any(w in f for w in _NOTICE_CTX) or (any(w in f for w in _NOTICE_PAIR[0])
                                                     and any(w in f for w in _NOTICE_PAIR[1]))
        if not (told and any(w in f for w in _EVENT_CTX)):
            continue
        m = rx.search(doc.lines[i])
        if m:
            return m.group(0)
    return None


def extract(doc: _Doc) -> dict:
    """Все условия договора строками «как в документе» (та же схема, что у ответа модели)."""
    raw = {}
    title = title_of(doc)
    raw["title"] = title
    raw["contract_no"] = _contract_no(doc, title)
    d, place = _head_date(doc)
    raw["contract_date"] = d.isoformat() if d else None
    raw["place"] = place
    for i, tail in find(doc, _LAB["product"], max_prefix=2, sep=True):
        raw["product_name"] = _strip_sep(tail)[:200] or None
        break
    if not raw.get("product_name") and title:
        raw["product_name"] = product_from_title(title)
    for i, tail in find(doc, _LAB["product_code"], max_prefix=2):
        m = _CODE.search(tail)
        if m:
            raw["product_code"] = m.group(1)
            break
    # «Приложение № … к приказу № …» в шапке — договор по форме, утверждённой приказом компании (шаблон компании)
    head_f = " ".join(doc.head_F[:12])
    raw["template_hint"] = bool(re.search(r"prilojenie", head_f) and re.search(r"prikaz", head_f)) or None
    D.tick()
    party_blank = set()
    raw.update(_parties(doc, party_blank))

    # объект
    for key, sep in (("object", False), ("object_fallback", False)):
        for i, tail in find(doc, _LAB[key], max_prefix=3, sep=sep):
            val = _block(doc, i, re.sub(r"(?i)^\s*(?:являются|является|hisoblanadi|ҳисобланади|is|are)\b", "", tail))
            val = re.sub(r"(?i)^\s*(?:являются|является|hisoblanadi|ҳисобланади)\s*[:\-–—]?\s*", "", val)
            if val and len(re.sub(r"[\W\d_]", "", val)) >= 3:
                raw["object"] = val
                break
        if raw.get("object"):
            break
    # личное страхование: объект — жизнь и здоровье застрахованных лиц (в договоре так прямо не подписано)
    person = personal_hint(raw.get("product_name") or title)
    many = any(w in " " + " ".join(doc.F) + " " for w in _MANY_PERSONS) if person else False
    if person:
        raw["class_hint"] = person
        raw["persons"] = "people" if many else "person"
        if not raw.get("object"):
            raw["object"] = _person_object(raw.get("product_name"), many, doc)
    # страховой случай — как написано в договоре («Страховой случай – травма или смерть … во время соревнований»)
    for i, tail in find(doc, _LAB["insured_event"], max_prefix=2):
        v = _strip_sep(re.sub(r"(?i)^\s*(?:является|признается|признаётся|hisoblanadi|is)\b", "", tail))
        if len(v) >= 10 and _has_sep(tail) or len(v) >= 30:
            v = re.split(r",\s*с\s+наступлением\s+которого|,\s*при\s+наступлении\s+которого", v)[0]
            raw["insured_event"] = v[:MAX_TEXT].strip(" ,;")
            break
    # когда действует защита (личное страхование): «во время спортивного соревнования»
    for ln in doc.lines if person else ():
        m = re.search(r"(?i)во\s+время\s+(?:участия\s+в\s+)?[а-яё\-]+(?:\s+[а-яё\-]+){0,4}", ln)
        if m and re.search(r"(?i)несчастн|страхов", ln):
            raw["cover_period"] = re.split(r"(?i)\s+в\s+течение\s", m.group(0))[0][:120]
            break
    raw["schedule"], raw["persons_listed"] = _schedule(doc)
    for key in ("address", "address_bare"):
        for i, tail in find(doc, _LAB[key], max_prefix=2, sep=(key == "address_bare")):
            if D.is_personal_label(doc.lines[i]):
                continue
            val = _strip_sep(tail) or _strip_sep(_next_line(doc, i))
            if val and re.search(r"[A-Za-zА-Яа-яЁё]{3}", val):
                raw["address"] = val[:300]
                raw["address_bare"] = key == "address_bare"
                break
        if raw.get("address"):
            break
    for i, tail in find(doc, _LAB["cadastre"], max_prefix=4):
        m = br._KADASTR.search(tail + " " + _next_line(doc, i))
        if m:
            raw["cadastre_no"] = m.group(0)
            break
    area_lines = [ln for ln, f in zip(doc.lines, doc.F) if any(w in f for w in ("ploshad", "maydon", " area",
                                                                                 "maydoni"))][:10]
    raw["areas_text"] = "\n".join(area_lines)[:2000] or None
    for key in ("construction", "purpose"):
        for i, tail in find(doc, _LAB[key], max_prefix=2, sep=True):
            v = _strip_sep(tail)
            if v:
                raw[key] = v[:200]
                break
    for i, tail in find(doc, _LAB["year_built"], max_prefix=2):
        y = D.cast("year", tail)
        if y:
            raw["year_built"] = y
            break
    D.tick()

    # деньги
    raw["sum_insured"] = _first_money(doc, ("sum_insured_total", "sum_insured"))
    raw["object_value"] = _first_money(doc, ("object_value",))
    raw["premium"] = _first_money(doc, ("premium",))
    for key, fld in (("sum_insured", "sum_insured"), ("premium", "premium")):
        if raw.get(fld):
            got = money(raw[fld])
            raw[fld + "_currency"] = got["currency"] if got else None
    for i, tail in find(doc, _LAB["tariff"], max_prefix=4):
        v = tariff(tail, _has_sep(tail))
        if v is not None:
            raw["tariff"] = v
            break
    items, total = _items_from_tables(doc)
    if not items:
        items, total = _items_from_lines(doc)
    raw["items"] = items
    raw["items_total"] = total
    raw["payment_mode"], raw["payments"] = _payments(doc)
    # порядок оплаты словами: «единовременно в течение 5 (пяти) банковских дней после подписания …»
    for i, tail in find(doc, _LAB["payment"], max_prefix=8):
        if any(w in doc.F[i] for w in _SINGLE + _INSTAL):
            raw["payment_text"] = _strip_sep(tail)[:200].rstrip(" .;") or None
            break
    D.tick()

    # срок
    for i, tail in find(doc, _LAB["term"], max_prefix=4):
        if _TERM_EXCLUDE.search(doc.lines[i]) and not br.dates_in(tail):
            continue
        txt = tail if len(br.dates_in(tail)) >= 2 else tail + " " + _next_line(doc, i)
        if br.dates_in(txt):
            raw["term"] = txt[:MAX_TEXT]
            break
    from . import analysis_docs as ad
    months, _lab, _note = ad.contract_term(doc.text, doc.tables)
    raw["term_months"] = months
    for i, tail in find(doc, _LAB["liability"], max_prefix=4):
        ds = br.dates_in(tail + " " + _next_line(doc, i))
        if ds:
            raw["liability_from"] = ds[0].isoformat()
            break
    D.tick()

    # франшиза
    for i, tail in find(doc, _LAB["franchise"], max_prefix=4):
        line = doc.lines[i]
        val = _strip_sep(tail)
        if not val or _is_heading(line):
            continue
        fr = br.franchise(val)
        if fr and fr["applied"] and fr["amount"] is not None and fr["amount"] < MIN_MONEY:
            fr["amount"] = None
        if fr and (not fr["applied"] or fr["pct"] or fr["amount"]):
            raw["franchise"] = val[:200]
            fl = doc.F[i]
            raw["franchise_type"] = ("unconditional" if any(w in fl for w in _TYPE_UNCOND) else
                                     "conditional" if any(w in fl for w in _TYPE_COND) else None)
            m = _FR_RISK.search(line)
            raw["franchise_risk"] = m.group(1).strip() if m else None
            break

    # риски, исключения, территория, особые условия, уведомление
    excl_lab = _LAB["exclusions"]
    for i, tail in find(doc, _LAB["risks"], max_prefix=4):
        if any(re.search(r"(?:^|\s)" + re.escape(lab) + r"(?:\s|$)", doc.F[i]) for lab in excl_lab):
            continue
        sec = _section(doc, i, tail, stop_keys=("exclusions",))
        codes = _words_in("\n".join(sec), _RISK_F)
        if codes:
            raw["covered_risks"] = codes[:MAX_LIST]
            break
    for i, tail in find(doc, excl_lab, max_prefix=4):
        sec = _section(doc, i, tail, stop_keys=("risks", "territory", "special", "franchise"))
        codes = _words_in("\n".join(sec), _EXCL_F)
        if codes:
            raw["exclusions"] = codes[:MAX_LIST]
            break
    for i, tail in find(doc, _LAB["territory"], max_prefix=2):
        v = _strip_sep(re.sub(r"(?i)^\s*(?:является|hisoblanadi|is)\b", "", tail))
        if v and (_has_sep(tail) or len(v) > 3):
            raw["territory"] = v[:200]
            break
    for i, tail in find(doc, _LAB["special"], max_prefix=3):
        sec = _section(doc, i, tail, max_lines=MAX_LIST)
        items_t = [re.sub(r"^\s*(?:[-•–—]|\d{1,2}(?:\.\d{1,2})*[.)]?|[a-zа-я]\))\s*", "", x).strip()
                   for x in sec if x.strip()]
        items_t = [x[:160] for x in items_t if len(x) >= 5]
        if items_t:
            raw["special_terms"] = items_t[:MAX_LIST]
            break
    raw["notice"] = _notice(doc)
    raw["blank"] = _blanks(doc, raw, title, party_blank)
    sch = raw.get("schedule")
    if sch and sch["blank"] and "rate" in sch["columns"] and raw.get("tariff") is None:
        raw["blank"].append("tariff_pct")         # ставка — колонка пустой таблицы приложения
    return raw


# ------------------------------------------------------------------ нормализация (общая для текста и модели)

def _iso(v) -> Optional[date]:
    if isinstance(v, date):
        return v
    s = str(v or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return br._date(*m.groups())
    ds = br.dates_in(s)
    return ds[0] if ds else None


def _party_of(v) -> dict:
    """Сторона: строка документа или {"is_legal", "name"} от модели. Гражданин — без имени."""
    if isinstance(v, dict):
        name = v.get("name") or v.get("text") or v.get("value")
        if v.get("is_legal") is False:
            return {"kind": "individual" if name or v.get("present") else None, "name": None}
        v = name
    return br.party(v)


def _money_of(v) -> tuple:
    if v in (None, ""):
        return None, None
    got = money(v)
    return (got["value"], got["currency"]) if got else (None, None)


def _codes(values, table_f, codes) -> list:
    """Список рисков или исключений: коды словаря; незнакомое — {"code": "other", "text": коротко}."""
    out, seen = [], set()
    for v in (values or [])[:MAX_LIST * 2]:
        if isinstance(v, dict):
            v = v.get("code") if v.get("code") in codes and v.get("code") != "other" else v.get("text")
        s = _clean(v)
        if not s:
            continue
        if s in codes and s != "other":
            hit = [s]
        else:
            hit = _words_in(s, table_f)
        if hit:
            for c in hit:
                if c not in seen:
                    seen.add(c)
                    out.append({"code": c, "text": None})
        elif s.lower() not in seen:
            seen.add(s.lower())
            out.append({"code": "other", "text": s[:80]})
        if len(out) >= MAX_LIST:
            break
    return out[:MAX_LIST]


_NUM_WORDS = {"один": 1, "одного": 1, "одному": 1, "два": 2, "двух": 2, "три": 3, "трех": 3, "трёх": 3, "четыре": 4,
              "четырех": 4, "четырёх": 4, "пять": 5, "пяти": 5, "bir": 1, "бир": 1, "ikki": 2, "икки": 2, "uch": 3,
              "уч": 3, "to'rt": 4, "тўрт": 4, "besh": 5, "беш": 5, "one": 1, "two": 2, "three": 3, "four": 4,
              "five": 5}
_PERIOD = re.compile(r"(?<![\w.])(\d{1,3}|[a-zа-яёўқғҳ']+)\s*(?:\([^)]{0,40}\)\s*)?"
                     r"(месяц\w*|мес\.?|ой\b|oy\b|oylik|months?\b|год\w*|лет\b|йил\w*|yil\w*|years?\b)", re.I)


def _months_in(text: str) -> Optional[int]:
    """Срок словами без дат: «12 месяцев», «три года», «3 yil», «36 oy» → месяцы. Разные сроки — None."""
    got = set()
    for m in _PERIOD.finditer(str(text or "").lower().replace("ʻ", "'").replace("’", "'")):
        n = int(m.group(1)) if m.group(1).isdigit() else _NUM_WORDS.get(m.group(1))
        if not n:
            continue
        unit = m.group(2)
        got.add(n if unit.startswith(("мес", "ой", "oy", "month")) else n * 12)
    got = {x for x in got if 1 <= x <= 120}
    return got.pop() if len(got) == 1 else None


def _add_months(d: date, n: int) -> date:
    from .analysis_docs import _add_months as am
    return am(d, n)


def fields(raw: dict, inclusive: bool = True) -> dict:
    """Сырые значения (текст документа или ответ модели) → поля договора с разобранными числами и сроком."""
    g = lambda k: _clean(raw.get(k))                          # noqa: E731
    desc = g("object") or g("object_description")
    facts = br.object_facts(" ".join(x for x in (desc, g("address"), g("areas_text")) if x))
    if not facts["class_hint"] and g("product_name"):
        facts_p = br.object_facts(g("product_name"))
        facts["class_hint"] = facts_p["class_hint"]
    hint = str(raw.get("class_hint") or "").strip().lower() or (personal_hint(g("product_name")) or "")
    if hint in PERSON_HINTS:
        # личное страхование: вид объекта — люди (застрахованные лица), не здание и не техника
        facts["class_hint"] = hint
        facts["object_kind"] = "people" if raw.get("persons") != "person" else "person"
    elif not facts["class_hint"] and hint in ("building", "equipment", "vehicle", "special_machinery", "cargo",
                                              "other"):
        facts["class_hint"] = hint
    areas = dict(facts["areas"])
    for slot, key in (("land_m2", "land_area"), ("useful_m2", "useful_area"), ("total_m2", "total_area")):
        if areas[slot] is None and raw.get(key) not in (None, ""):
            areas[slot] = br.amount(raw.get(key))
    addr = g("address")
    from .analysis_docs import safe_address
    region = D.cast("region", addr) if addr else None
    s_ins, cur_s = _money_of(raw.get("sum_insured"))
    o_val, cur_v = _money_of(raw.get("object_value"))
    prem, cur_p = _money_of(raw.get("premium"))
    cur = g("currency") or cur_s or cur_p or cur_v
    cur = str(cur).upper()[:3] if cur else None
    if cur in ("СУМ", "SOM", "SUM"):
        cur = "UZS"
    tr = raw.get("tariff")
    tr = tr if isinstance(tr, float) else (br.tariff(tr) if tr not in (None, "") else None)
    # срок: даты текста; иначе даты модели; одна дата и месяцы — конец считается; только месяцы — дни
    tm = br.term(g("term"), inclusive) if g("term") else {"from": None, "to": None, "days": None,
                                                           "inclusive": bool(inclusive), "error": "term_not_found"}
    a, b = _iso(raw.get("term_from")), _iso(raw.get("term_to"))
    months = raw.get("term_months") if isinstance(raw.get("term_months"), int) else None
    if months is None and g("term"):
        months = _months_in(g("term"))
    if not tm["days"] and a and b and b >= a and 1 <= br.term_days(a, b, inclusive) <= br.MAX_TERM_DAYS:
        tm.update({"from": a.isoformat(), "to": b.isoformat(), "days": br.term_days(a, b, inclusive), "error": None})
    if not tm["days"] and months and 1 <= months <= 120:
        one = br.dates_in(g("term"))[0] if g("term") and br.dates_in(g("term")) else a
        if one:
            end = _add_months(one, months) - timedelta(days=1 if inclusive else 0)
            tm.update({"from": one.isoformat(), "to": end.isoformat(), "days": br.term_days(one, end, inclusive),
                       "error": None})
        else:
            tm.update({"days": months_days(months), "error": None})
    lf = _iso(raw.get("liability_from"))
    fr = br.franchise(raw.get("franchise")) if raw.get("franchise") not in (None, "") else None
    if isinstance(raw.get("franchise"), dict):
        fd = raw["franchise"]
        fr = br.franchise(fd.get("text")) if fd.get("text") else None
        if fr is None and isinstance(fd.get("applied"), bool):
            fr = {"applied": fd["applied"], "text": None, "pct": None, "amount": None}
        if fr is not None and fd.get("applied") is False:
            fr = {"applied": False, "text": fr.get("text"), "pct": None, "amount": None}
    if fr is not None:
        rf = raw.get("franchise")
        ftype = str((rf.get("type") if isinstance(rf, dict) else None) or raw.get("franchise_type") or "")
        ftype = ftype.strip().lower()
        fr = dict(fr, type=ftype if fr["applied"] and ftype in ("unconditional", "conditional") else None,
                  risk=_clean(raw.get("franchise_risk")) if fr["applied"] else None)
        if fr["applied"] and fr["amount"] is not None and fr["amount"] < MIN_MONEY:
            fr["amount"] = None                   # «4.1» — номер пункта, а не сумма франшизы
        if fr["applied"] and fr["pct"] is None and fr["amount"] is None:
            fr["amount"] = (money(fr.get("text") or "") or {}).get("value")
    items = []
    for it in (raw.get("items") or [])[:MAX_ITEMS]:
        if not isinstance(it, dict):
            continue
        name = _clean(it.get("name"))
        sm, _c = _money_of(it.get("sum"))
        if name and sm:
            items.append({"name": name[:200], "sum": sm})
    pays = []
    for p in (raw.get("payments") or [])[:MAX_PAYMENTS]:
        if not isinstance(p, dict):
            continue
        d, (amt, _c) = _iso(p.get("date")), _money_of(p.get("amount"))
        if d and amt:
            pays.append({"date": d.isoformat(), "amount": amt})
    mode = str(raw.get("payment_mode") or "").strip().lower()
    mode = mode if mode in ("single", "installments") else ("installments" if len(pays) >= 2 else None)
    code = _CODE.search(g("product_code") or "") or _CODE_IN_NAME.search(g("product_name") or "")
    party = {k: _party_of(raw.get(k)) for k in PARTY_CODES}
    notice = g("notice")
    ct = _iso(raw.get("contract_date"))
    out = {
        "contract_no": (g("contract_no") or "")[:40] or None,
        "contract_date": ct.isoformat() if ct else None,
        "place": (g("place") or "")[:60] or None,
        "product_name": (g("product_name") or "")[:200] or None,
        "product_code": code.group(1).zfill(4) if code else None,
        "class_hint": facts["class_hint"],
        **party,
        "has_beneficiary": party["beneficiary"]["kind"] is not None,
        "has_pledger": party["pledger"]["kind"] is not None,
        "object_description": desc, "object_kind": facts["object_kind"],
        "address": safe_address(addr) if addr else None, "region": region,
        "cadastre_no": g("cadastre_no") or facts["cadastre_no"], "areas": areas,
        "construction": g("construction"), "purpose": g("purpose"),
        "year_built": D.cast("year", g("year_built")) if g("year_built") else None,
        "brand": g("brand"), "model": g("model"), "year": D.cast("year", g("year")) if g("year") else None,
        "vin": g("vin"), "serial_no": g("serial_no"), "engine_no": g("engine_no"), "reg_no": g("reg_no"),
        "object_value": o_val, "sum_insured": s_ins, "currency": cur,
        "items": items, "items_total": _money_of(raw.get("items_total"))[0],
        "tariff_pct": tr, "premium": prem, "payment_mode": mode, "payments": pays,
        "term_text": g("term"), "term_from": tm["from"], "term_to": tm["to"], "term_days": tm["days"],
        "term_inclusive": tm["inclusive"], "term_error": tm["error"] if not tm["days"] else None,
        "liability_from": lf.isoformat() if lf and lf.isoformat() != tm["from"] else None,
        "franchise": fr,
        "covered_risks": _codes(raw.get("covered_risks"), _RISK_F, RISK_CODES),
        "exclusions": _codes(raw.get("exclusions"), _EXCL_F, EXCLUSION_CODES),
        "territory": (g("territory") or "")[:200] or None,
        "special_terms": [s[:160] for s in (_clean(x) for x in (raw.get("special_terms") or [])[:MAX_LIST]) if s],
        "notice": notice[:80] if notice else None,
        # личное страхование и бланк договора (30.09.2026)
        "insured_event": (g("insured_event") or "")[:MAX_TEXT] or None,
        "cover_period": (g("cover_period") or "")[:120] or None,
        "payment_text": (g("payment_text") or "")[:200] or None,
        "schedule": _schedule_of(raw.get("schedule")),
        "persons_listed": raw.get("persons_listed") if isinstance(raw.get("persons_listed"), int) else None,
        "template_hint": bool(raw.get("template_hint")),
    }
    blank = [k for k in (raw.get("blank") or []) if isinstance(k, str) and k in BLANK_CODES]
    # пустым считается только то, чего в итоге нет (модель или правила могли найти значение)
    blank = [k for k in blank if k == "place" and not out["place"] or k != "place" and not present(out, k)]
    out["blank"] = blank
    out["is_template"] = sum(1 for k in blank if k in TEMPLATE_KEYS) >= TEMPLATE_MIN
    for k in ("brand", "model", "vin", "serial_no", "engine_no", "reg_no", "construction", "purpose"):
        if out[k]:
            out[k] = out[k][:120]
    return out


def present(f: dict, key: str) -> bool:
    """Есть ли в полях договора ключевое поле (для found/missing и порога ai_assist)."""
    if key == "term":
        return bool(f.get("term_days"))
    if key == "object":
        return bool(f.get("object_description") or f.get("items") or f.get("cadastre_no"))
    if key in PARTY_CODES:
        return (f.get(key) or {}).get("kind") is not None
    v = f.get(key)
    return v not in (None, "", [], {})


def essentials(f: dict) -> list:
    """Существенные условия договора имущественного страхования (ГК РУз, ст. 929): есть ли в договоре.
    Бланк договора (is_template) не проверяется: условия проверяются по заполненному договору — пустой список."""
    if f.get("is_template"):
        return []
    have = {"object": present(f, "object"), "insured_event": bool(f.get("covered_risks") or f.get("insured_event")),
            "sum_insured": present(f, "sum_insured"), "premium": present(f, "premium"), "term": present(f, "term")}
    return [{"code": c, "present": have[c]} for c in ESSENTIALS]


def found_missing(f: dict) -> tuple:
    """Найденные ключевые поля и недостающие; у бланка недостающие — незаполненные поля бланка."""
    found = [k for k in KEY_FIELDS if present(f, k)]
    if f.get("is_template"):
        return found, [k for k in KEY_FIELDS if k in (f.get("blank") or []) and k not in found]
    return found, [k for k in KEY_FIELDS if k not in found]


def parse_text(text: str, tables: Optional[list] = None, inclusive: bool = True, pdf: bool = False,
               force: bool = False) -> Optional[dict]:
    """Файл с текстом → {"fields", "found", "missing", "essentials"} или None, если это не договор.
    pdf — текст из PDF (строки перенесены по ширине страницы — склеиваются); force — разобрать без проверки
    вида (заявление на страхование: те же подписи, но это не договор)."""
    doc = _Doc(text, tables, pdf)
    if not force and not detect(doc):
        return None
    raw = extract(doc)
    f = fields(raw, inclusive)
    # техника и транспорт: марка, модель, год, VIN, госномер — подписями техпаспорта (docparse)
    if f["class_hint"] in ("vehicle", "special_machinery", "equipment") or any(
            w in " ".join(doc.F[:400]) for w in (" vin ", "davlat raqami", "gos nomer", "zavodskoy nomer")):
        D.tick()
        got = D.parse_text("\n".join(doc.lines), D.KIND_PASSPORT, with_reg_no=True, use_llm=False)
        by = {it["поле"]: it.get("значение") for it in got.get("поля") or [] if it.get("значение") is not None}
        for k in ("brand", "model", "year", "engine_no", "reg_no"):
            if not f.get(k) and by.get(k):
                f[k] = str(by[k])[:120]
        for k in ("vin", "body_no", "chassis_no"):
            if not f.get("vin") and by.get(k):
                f["vin"] = str(by[k])[:40]
    found, missing = found_missing(f)
    return {"fields": f, "found": found, "missing": missing, "essentials": essentials(f),
            "is_template": bool(f.get("is_template"))}


def from_model(raw, inclusive: bool = True, min_found: int = 2) -> Optional[dict]:
    """Блок contract из ответа модели → те же поля, что у текстового разбора. Найдено меньше min_found
    ключевых полей (для скана — почти пусто) — None."""
    if not isinstance(raw, dict):
        return None
    if isinstance(raw.get("object"), dict):          # модель могла вложить объект отдельным словарём
        raw = {**raw, **{("object" if k == "description" else k): v for k, v in raw["object"].items()}}
    raw = dict(raw)
    if raw.get("tariff") in (None, "") and raw.get("tariff_pct") not in (None, ""):
        raw["tariff"] = raw.get("tariff_pct")
    if raw.get("tariff") not in (None, "") and not isinstance(raw["tariff"], float):
        raw["tariff"] = br.tariff(raw["tariff"])
    if raw.get("term") in (None, "") and raw.get("term_text"):
        raw["term"] = raw["term_text"]
    f = fields(raw, inclusive)
    found, missing = found_missing(f)
    if len(found) < min_found:
        return None
    return {"fields": f, "found": found, "missing": missing, "essentials": essentials(f)}


# ------------------------------------------------------------------ для акта

def items(f: dict) -> list:
    """Поля договора → записи распознанного [{key, value}] (строки, как у остальных документов)."""
    out = []

    def add(key, value):
        if value not in (None, "") and not any(o["key"] == key for o in out):
            out.append({"key": key, "value": str(value)[:MAX_TEXT]})

    add("product_code", f.get("product_code"))
    for k in ("policyholder", "beneficiary", "pledger"):
        add(k, (f.get(k) or {}).get("name"))
    add("object_type", f.get("object_description"))
    add("cadastre_no", f.get("cadastre_no"))
    ar = f.get("areas") or {}
    for key, slot in (("land_area", "land_m2"), ("useful_area", "useful_m2"), ("total_area", "total_m2")):
        if ar.get(slot):
            add(key, br._num_text(ar[slot]) + " м²")
    uzs = f.get("currency") in (None, "UZS")
    for key in ("object_value", "sum_insured", "premium"):
        if f.get(key) and uzs:
            add(key, br._num_text(f[key]))
    fr = f.get("franchise")
    if fr and fr.get("text"):
        add("franchise", fr["text"])
    if f.get("tariff_pct") is not None:
        add("tariff_pct", "%g" % f["tariff_pct"])
    add("term_from", f.get("term_from"))
    add("term_to", f.get("term_to"))
    if f.get("term_days"):
        add("term_days", f["term_days"])
    add("region", f.get("region"))
    add("construction", f.get("construction"))
    add("year", f.get("year") or f.get("year_built"))
    for k in ("brand", "model", "engine_no", "reg_no"):
        add(k, f.get(k))
    add("serial_no", f.get("vin") or f.get("serial_no"))
    return out


def prefill(f: dict) -> dict:
    """Подсказка для шага 2: код продукта, суммы (только в сумах), срок, регион — если есть в договоре."""
    out = {}
    uzs = f.get("currency") in (None, "UZS")
    for k in ("product_code", "sum_insured", "object_value", "term_days", "term_from", "term_to"):
        if f.get(k) not in (None, "") and (uzs or k not in ("sum_insured", "object_value")):
            out[k] = f[k]
    if f.get("region"):
        out["region"] = f["region"]
    return out


def risk_list(xs: list) -> list:
    """Риски/исключения для optional.contract: код словаря или короткий текст."""
    return [x["code"] if x.get("code") != "other" else x.get("text") for x in xs or [] if x.get("code")]


def request_of(f: dict, source: str) -> dict:
    """Готовый optional.contract для /act/make: поля optional.request и условия договора."""
    out = br.request_of(f, source)
    fr = f.get("franchise")
    if fr:
        out["franchise"] = {k: fr.get(k) for k in ("applied", "text", "pct", "amount", "type")}
    out.update(contract_no=f.get("contract_no"), contract_date=f.get("contract_date"),
               object_value=f.get("object_value"), currency=f.get("currency"),
               object_description=f.get("object_description"),
               covered_risks=risk_list(f.get("covered_risks")), exclusions=risk_list(f.get("exclusions")),
               payment_mode=f.get("payment_mode"), payments=list(f.get("payments") or []),
               items=list(f.get("items") or []),
               # выгодоприобретатель — только название юрлица (банк → проверка 12 сверки договора)
               beneficiary=_legal_party(f.get("beneficiary")))
    return out


def _legal_party(p) -> Optional[dict]:
    """Сторона для акта: юрлицо — {kind, name}; гражданин — {kind: individual} без имени; нет — None."""
    p = p or {}
    if p.get("kind") == "legal" and p.get("name"):
        return {"kind": "legal", "name": str(p["name"])[:120]}
    return {"kind": "individual", "name": None} if p.get("kind") == "individual" else None


# условия договора → поля осмотра (ТЗ 13.10.2026, день 7.1): поле экрана, откуда в разборе договора
CONDITION_KEYS = ("product_code", "sum_insured", "object_value", "tariff_pct", "premium", "term_from", "term_to",
                  "term_days", "franchise", "covered_risks", "policyholder", "beneficiary", "region")


def conditions(f: dict) -> list:
    """Найденные условия договора для полей осмотра: [{field, value}] с пометкой «из договора» у вызывающего.
    Суммы — только в сумах; стороны — только название юрлица (у гражданина имени нет: ПД не извлекаются)."""
    out = []
    uzs = f.get("currency") in (None, "UZS")
    for k in CONDITION_KEYS:
        v = f.get(k)
        if k in ("sum_insured", "object_value", "premium") and not uzs:
            continue
        if k in PARTY_CODES:
            v = (_legal_party(v) or {}).get("name")
        elif k == "franchise":
            v = {x: (v or {}).get(x) for x in ("applied", "text", "pct", "amount", "type")} if v else None
        elif k == "covered_risks":
            v = risk_list(v) or None
        if v not in (None, "", [], {}):
            out.append({"field": k, "value": v})
    return out


# ------------------------------------------------------------------ запрос филиала против договора

CROSS_ITEMS = ("product_code", "sum_insured", "object_value", "tariff_pct", "premium", "term", "franchise", "object")


def object_of(d: dict) -> dict:
    """Кадастровый номер и вид объекта документа; чего нет в полях — из описания объекта (как при загрузке)."""
    facts = br.object_facts(d.get("object_description")) if d.get("object_description") else {}
    return {"cadastre_no": d.get("cadastre_no") or facts.get("cadastre_no"),
            "class_hint": d.get("class_hint") or facts.get("class_hint"),
            "object_kind": d.get("object_kind") or facts.get("object_kind")}


def cross_check(req: Optional[dict], ct: Optional[dict], tol: float = 1000.0) -> Optional[dict]:
    """
    Запрос филиала против договора: коды и числа (слова — app/act_texts.py). req и ct — поля документов
    (branch_request.fields / fields) или проверенные optional.request / optional.contract. Сумма и
    стоимость — до сума, премия — в пределах допуска tol, тариф — точно, срок — даты и дни.
    """
    if not req or not ct:
        return None
    items = []

    def add(code, a, b, same):
        if a in (None, "", []) or b in (None, "", []):
            v = "missing"
        else:
            v = "same" if same else "differs"
        items.append({"code": code, "request": a, "contract": b, "verdict": v})

    add("product_code", req.get("product_code"), ct.get("product_code"),
        str(req.get("product_code")) == str(ct.get("product_code")))
    for k in ("sum_insured", "object_value"):
        a, b = req.get(k), ct.get(k)
        add(k, a, b, a is not None and b is not None and abs(float(a) - float(b)) <= 1.0)
    a, b = req.get("tariff_pct"), ct.get("tariff_pct")
    add("tariff_pct", a, b, a is not None and b is not None and abs(float(a) - float(b)) <= 1e-6)
    a, b = req.get("premium"), ct.get("premium")
    add("premium", a, b, a is not None and b is not None and abs(float(a) - float(b)) <= tol + 1e-9)
    ta = {"from": req.get("term_from"), "to": req.get("term_to"), "days": req.get("term_days")}
    tb = {"from": ct.get("term_from"), "to": ct.get("term_to"), "days": ct.get("term_days")}
    same_t = ta["days"] == tb["days"] and (not (ta["from"] and tb["from"]) or
                                           (ta["from"] == tb["from"] and ta["to"] == tb["to"]))
    add("term", ta if ta["days"] else None, tb if tb["days"] else None, same_t)
    fa, fb = req.get("franchise"), ct.get("franchise")
    if fa and fb:
        same_f = bool(fa.get("applied")) == bool(fb.get("applied")) and (
            not fa.get("applied") or (fa.get("pct") == fb.get("pct") and fa.get("amount") == fb.get("amount")))
    else:
        same_f = False
    add("franchise", fa, fb, same_f)
    # объект: кадастр с кадастром (если есть в обоих), иначе вид объекта с видом; сравнить нечем — missing
    oa, ob = object_of(req), object_of(ct)
    if oa["cadastre_no"] and ob["cadastre_no"]:
        same, by = oa["cadastre_no"] == ob["cadastre_no"], "cadastre"
    elif oa["class_hint"] and ob["class_hint"]:
        same, by = oa["class_hint"] == ob["class_hint"], "kind"
    else:
        same, by = None, None
    has = lambda o: any(o.values())                                     # noqa: E731
    items.append({"code": "object", "request": oa if has(oa) else None, "contract": ob if has(ob) else None,
                  "verdict": "missing" if by is None else ("same" if same else "differs"), "compared": by})
    return {"available": True, "items": items, "differs": sum(1 for i in items if i["verdict"] == "differs"),
            "missing": sum(1 for i in items if i["verdict"] == "missing"), "tolerance": tol}


# ------------------------------------------------------------------ текст договора для модели (ai_assist)

def excerpt(text: str, max_chars: int = 30000) -> tuple:
    """Выдержка договора для модели: начало документа и строки вокруг подписей суммы, премии, срока и объекта.
    Возвращает (текст, обрезан ли). Маскирует ПД вызывающий (llm.mask_pd) — здесь только отбор."""
    text = text or ""
    if len(text) <= max_chars:
        return text, False
    # строки у подписей суммы, премии, срока, объекта — не больше двух третей выдержки; остальное — начало
    first = max_chars // 3
    doc = _Doc(text[first:], None)
    keep = []
    for key in ("sum_insured_total", "sum_insured", "premium", "tariff", "term", "object", "franchise", "risks",
                "exclusions", "payment", "object_value"):
        for i, _t in find(doc, _LAB[key], max_prefix=4):
            keep.extend(range(max(0, i - 1), min(len(doc.lines), i + 4)))
    tail, used = [], 0
    for i in sorted(set(keep)):
        ln = doc.lines[i]
        if used + len(ln) + 1 > max_chars - first:
            break
        tail.append(ln)
        used += len(ln) + 1
    return "\n".join([text[:max_chars - used - 1]] + tail), True


# ------------------------------------------------------------------ дочитывание моделью (ai_assist)

def need_assist(f: dict) -> bool:
    """Разбор правилами нашёл меньше половины ключевых полей (сумма, премия, срок, объект).
    Бланк договора модель не дочитывает: полей в нём нет — обращение было бы впустую."""
    if f.get("is_template"):
        return False
    return sum(1 for k in CORE if present(f, k)) * 2 < len(CORE)


# какие поля договора стоят за записью распознанного (для пометки «прочитано моделью»)
_ITEM_FIELD = {"object_type": "object_description", "land_area": "areas", "useful_area": "areas",
               "total_area": "areas", "franchise": "franchise", "serial_no": "vin", "year": "year"}


def item_field(key: str) -> str:
    return _ITEM_FIELD.get(key, key)


_SKIP_MERGE = ("term_inclusive", "term_error", "has_beneficiary", "has_pledger", "blank", "is_template",
               "template_hint", "schedule", "persons_listed")


def _empty(key: str, v) -> bool:
    if key in PARTY_CODES:
        return (v or {}).get("kind") is None
    if key == "areas":
        return not any((v or {}).values())
    return v in (None, "", [], {})


def merge_missing(f: dict, ai: dict) -> list:
    """Дополняет пустые поля f значениями модели (заполненное правилами не трогается). Возвращает, что дополнено.
    Срок — целиком (с, по, дни), чтобы не смешать даты из разных источников."""
    filled = []
    term_keys = ("term_from", "term_to", "term_days")
    if all(_empty(k, f.get(k)) for k in term_keys) and not _empty("term_days", ai.get("term_days")):
        for k in term_keys:
            f[k] = ai.get(k)
            if f[k] is not None:
                filled.append(k)
        f["term_error"] = None
    for k, v in ai.items():
        if k in _SKIP_MERGE or k in term_keys or k not in f:
            continue
        if _empty(k, f.get(k)) and not _empty(k, v):
            f[k] = v
            filled.append(k)
    f["has_beneficiary"] = (f.get("beneficiary") or {}).get("kind") is not None
    f["has_pledger"] = (f.get("pledger") or {}).get("kind") is not None
    if f.get("blank"):
        f["blank"] = [k for k in f["blank"] if k == "place" and not f.get("place") or k != "place"
                      and not present(f, k)]
        f["is_template"] = sum(1 for k in f["blank"] if k in TEMPLATE_KEYS) >= TEMPLATE_MIN
    return filled
