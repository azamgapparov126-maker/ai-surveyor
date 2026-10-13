-- База ИИ-сюрвейера. SQLite; переносится на PostgreSQL без переписывания логики.
-- Справочники -> расчёт -> проверки -> журнал. Всё, что меняется со временем, версионируется.

PRAGMA foreign_keys = ON;

-- ============ СПРАВОЧНИКИ ============

-- Учётные группы для резерва незаработанной премии (Положение 1882, п. 10)
CREATE TABLE IF NOT EXISTS groups (
    code        TEXT PRIMARY KEY,           -- 1..4
    name        TEXT NOT NULL,
    rnp_method  TEXT NOT NULL,              -- как считается РНП
    rnp_share   REAL                        -- доля базы; NULL = по дням (pro rata)
);

-- Классы страхования (Закон о страховой деятельности)
CREATE TABLE IF NOT EXISTS classes (
    code        TEXT PRIMARY KEY,           -- '1'..'18', '13з', '16у'
    name        TEXT NOT NULL,
    -- учётная группа РНП (Положение 1882, п. 10); у класса 18 (медицинское, ПКМ № 80, прил. 6) её в Положении нет
    -- — NULL. Старые базы доводит app/db.py (_classes_group_nullable)
    group_code  TEXT REFERENCES groups(code),
    branch      TEXT NOT NULL,              -- 'общее' | 'жизнь'
    kind        TEXT NOT NULL               -- 'имущество' | 'ответственность' | 'финансы' | 'личное'
);

-- Страховые риски (опасности) по классам: то, что агент включает и выключает
CREATE TABLE IF NOT EXISTS perils (
    code            TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    class_code      TEXT NOT NULL REFERENCES classes(code),
    is_catastrophic INTEGER NOT NULL DEFAULT 0,
    base_share      REAL,                   -- доля в нетто-ставке класса, 0..1
    note            TEXT
);

-- Версии тарифов: и компании, и регулятора. Старые расчёты сохраняют свою версию.
CREATE TABLE IF NOT EXISTS tariff_versions (
    id             INTEGER PRIMARY KEY,
    level          TEXT NOT NULL,           -- 'компания' | 'регулятор'
    name           TEXT NOT NULL,
    document_ref   TEXT,
    effective_from TEXT NOT NULL,
    effective_to   TEXT
);

-- Продукты тарифной политики
CREATE TABLE IF NOT EXISTS products (
    code            TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    rate_text       TEXT,                   -- как записано в тарифной политике
    commission_text TEXT,
    commission_pct  REAL,
    pricing_mode    TEXT NOT NULL,          -- 'ставка' | 'программа' | 'по согласованию' | 'генеральный договор' | 'нормативный акт'
    is_general      INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'действует',
    note            TEXT
);

-- Продукт может состоять из частей, каждая в своём классе (Положение 1882, п. 11)
CREATE TABLE IF NOT EXISTS product_classes (
    product_code TEXT NOT NULL REFERENCES products(code),
    class_code   TEXT NOT NULL REFERENCES classes(code),
    part_no      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (product_code, class_code)
);

-- Минимальные ставки: нижний порог расчёта
CREATE TABLE IF NOT EXISTS min_rates (
    id                INTEGER PRIMARY KEY,
    tariff_version_id INTEGER NOT NULL REFERENCES tariff_versions(id),
    product_code      TEXT REFERENCES products(code),
    class_code        TEXT REFERENCES classes(code),
    payer_type        TEXT,                 -- 'юр' | 'физ' | NULL для всех
    min_rate_pct      REAL NOT NULL
);

-- Правки минимальной ставки страховщика (app/min_rates.py, 01.10.2026): у каждой правки администратора или
-- импорта из Excel — своя версия тарифа (tariff_versions) и строка min_rates; здесь — тип ставки, кто, когда,
-- примечание. Старые версии не удаляются: акт берёт минимум, действующий на свою дату.
CREATE TABLE IF NOT EXISTS min_rate_versions (
    tariff_version_id INTEGER PRIMARY KEY REFERENCES tariff_versions(id),
    product_code      TEXT NOT NULL,
    min_rate_pct      REAL NOT NULL,
    rate_type         TEXT NOT NULL DEFAULT 'annual',   -- annual (годовая) | fixed (на весь срок)
    source            TEXT NOT NULL,                    -- admin | import
    created_at        TEXT NOT NULL,
    created_by        TEXT,
    note              TEXT
);

-- Базовая ставка продукта по версиям (импорт продуктов из Excel, app/product_import.py). Каждая загрузка —
-- своя версия тарифа с датой вступления; products.rate_text — только текущий текст, история — здесь.
CREATE TABLE IF NOT EXISTS product_rate_versions (
    tariff_version_id INTEGER PRIMARY KEY REFERENCES tariff_versions(id),
    product_code      TEXT NOT NULL,
    base_rate_pct     REAL NOT NULL,
    rate_type         TEXT NOT NULL DEFAULT 'annual',   -- annual (годовая) | fixed (на весь срок)
    rate_text_before  TEXT,                             -- текст тарифа продукта до загрузки
    source            TEXT NOT NULL,                    -- import
    created_at        TEXT NOT NULL,
    created_by        TEXT
);
CREATE INDEX IF NOT EXISTS ix_product_rate_versions_product ON product_rate_versions(product_code);

-- Коэффициенты андеррайтинга: фактор -> вариант -> множитель
CREATE TABLE IF NOT EXISTS coefficients (
    id          INTEGER PRIMARY KEY,
    factor_code TEXT NOT NULL,
    factor_name TEXT NOT NULL,
    class_code  TEXT REFERENCES classes(code),
    option_code TEXT NOT NULL,
    option_name TEXT NOT NULL,
    multiplier  REAL NOT NULL,
    calibrated  INTEGER NOT NULL DEFAULT 0, -- 0 = экспертное значение, не на своей статистике
    source      TEXT
);

-- Базовые нетто-ставки по классу и типу объекта
CREATE TABLE IF NOT EXISTS base_rates (
    id           INTEGER PRIMARY KEY,
    class_code   TEXT NOT NULL REFERENCES classes(code),
    object_type  TEXT NOT NULL,
    net_rate_pct REAL NOT NULL,
    frequency    REAL,                      -- частота страховых случаев q
    severity     REAL,                      -- средняя тяжесть k
    calibrated   INTEGER NOT NULL DEFAULT 0,
    source       TEXT
);

-- Состав нагрузки (без РПМ — компания его не ведёт)
CREATE TABLE IF NOT EXISTS load_components (
    code     TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    share    REAL NOT NULL                  -- доля в брутто-ставке
);

-- Чек-листы документов
CREATE TABLE IF NOT EXISTS checklists (
    id          INTEGER PRIMARY KEY,
    scope_type  TEXT NOT NULL,              -- 'всегда' | 'класс' | 'продукт' | 'тип_объекта'
    scope_code  TEXT,
    doc_name    TEXT NOT NULL,
    required    INTEGER NOT NULL DEFAULT 1,
    condition   TEXT,                       -- когда запрашивать дополнительно
    -- зачем документ нужен: 'анализ' — для оценки риска, 'оформление' — только для заключения
    -- договора (заявление-анкета и прочая канцелярия). Анализ риска строки 'оформление' не спрашивает.
    scope       TEXT NOT NULL DEFAULT 'анализ'
);

-- Правила проверки: то, что система не даст нарушить
CREATE TABLE IF NOT EXISTS rules (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    severity    TEXT NOT NULL,              -- 'стоп' | 'предупреждение' | 'подсказка'
    legal_ref   TEXT,
    description TEXT NOT NULL,
    -- LAWWATCH-01: если изменился акт, на который ссылается правило, робот ставит
    -- «требует пересмотра». Снимает пометку только человек (юрист) — см. app/lawwatch.py.
    review_status TEXT NOT NULL DEFAULT 'ok',   -- 'ok' | 'требует пересмотра'
    review_reason TEXT,                         -- «изменён акт pol_1806, редакция 23.06.2025 → 11.10.2026»
    review_since  TEXT                          -- когда поставлена пометка
);

-- Финансовые показатели компании: для лимита 20% на один риск
-- own_funds и reserves идут в лимит (Положение 1806 п. 15); остальные колонки — разбивка и происхождение цифр
-- (с 03.10.2026: заполняются из рэнкинга snsratings.uz, tools/ranking_parse.py, до подтверждения бухгалтерией)
CREATE TABLE IF NOT EXISTS company_financials (
    report_date    TEXT PRIMARY KEY,
    own_funds      REAL NOT NULL,          -- собственные средства, сум (из рэнкинга — совокупный капитал)
    reserves       REAL NOT NULL,          -- страховые резервы, сум (из рэнкинга — чистые, за вычетом доли перестраховщиков)
    source         TEXT,
    total_capital  REAL,                   -- совокупный капитал, сум
    share_capital  REAL,                   -- акционерный капитал, сум
    reserves_gross REAL,                   -- страховые резервы брутто, сум
    reserves_net   REAL,                   -- страховые резервы чистые, сум
    total_assets   REAL,                   -- совокупные активы, сум
    basis          TEXT,                   -- что взято в own_funds и reserves
    source_url     TEXT,
    confirmed      INTEGER                 -- 1 — подтверждено бухгалтерией; 0 — нет; NULL — не указано
);

-- Предупредительные мероприятия: что предписать страхователю при определённом факторе риска.
-- Не путать с РПМ (резервом): резерв компания не ведёт, а предписания выдаёт.
CREATE TABLE IF NOT EXISTS preventive_measures (
    code         TEXT PRIMARY KEY,
    class_code   TEXT REFERENCES classes(code),
    trigger_kind TEXT NOT NULL,             -- 'factor' | 'peril' | 'sum_over'
    trigger_key  TEXT NOT NULL,             -- factor_code | peril_code | порог суммы
    trigger_val  TEXT,                      -- option_code (для factor) или NULL
    measure      TEXT NOT NULL,             -- что сделать
    why          TEXT NOT NULL,             -- зачем, простыми словами
    effect_option TEXT,                     -- какой вариант фактора получится после выполнения
    mandatory_over REAL,                    -- обязательно как условие договора при страховой сумме выше этой
    deadline_days INTEGER                   -- срок выполнения, если условие договора
);

-- Рыночная статистика НАПП: временной ряд по строкам листа «классы страхования».
-- Значения премий и выплат — нарастающим итогом с начала года (как в отчёте); обязательства — на дату.
CREATE TABLE IF NOT EXISTS market_stats (
    report_date   TEXT NOT NULL,            -- дата среза, ГГГГ-ММ-ДД
    row_key       TEXT NOT NULL,            -- нормализованный ключ строки: total, cls8, cls8_9 ...
    row_name      TEXT NOT NULL,            -- как в отчёте
    premiums_ytd  REAL,
    payouts_ytd   REAL,
    liabilities   REAL,
    source_file   TEXT,
    loaded_at     TEXT NOT NULL,
    PRIMARY KEY (report_date, row_key)
);
-- индексы частых запросов (задача 123, подобраны по EXPLAIN QUERY PLAN)
CREATE INDEX IF NOT EXISTS ix_market_stats_row ON market_stats(row_key, report_date); -- ряд и последний срез
-- Пометки к строкам market_stats (01.10.2026, tools/market_stats.py): например, итог комплексного страхования
-- (multi_general) не равен сумме пакетов в отчёте — в market_stats взята сумма пакетов, здесь — почему.
-- Пересобирается целиком при каждой загрузке; показывается в /market/series (/stats) и в CSV-выгрузках.
CREATE TABLE IF NOT EXISTS market_stats_notes (
    report_date TEXT NOT NULL,
    row_key     TEXT NOT NULL,
    note        TEXT NOT NULL,
    source_file TEXT,
    loaded_at   TEXT NOT NULL,
    PRIMARY KEY (report_date, row_key)
);

-- Претензии (страховые требования) и договоры из отчётов НАПП (01.10.2026, tools/market_stats.py):
-- регионы — листы 3.5 (претензии, общее страхование) + 3.4 (договоры, общее страхование) + 3.2 (выплаты, общее);
-- страховщики — листы 2.10 (претензии) + 2.7 (договоры) + 2.5 (выплаты), всё по общему страхованию.
-- Претензии — числа с начала года на дату среза; договоры действующие — на дату, новые — с начала года.
CREATE TABLE IF NOT EXISTS napp_claims (
    report_date       TEXT NOT NULL,        -- дата среза, ГГГГ-ММ-ДД
    scope             TEXT NOT NULL,        -- 'region' | 'company'
    key               TEXT NOT NULL,        -- 'total' | 'region:TOSHKENT' | 'company:INSON AJ' (как в market_stats)
    name              TEXT NOT NULL,
    claims_received   REAL,                 -- Kelib tushgan sug'urta da'volarning umumiy soni (заявлено)
    claims_paid       REAL,                 -- To'langan (оплачено)
    claims_refused    REAL,                 -- Rad etilgan (отказано)
    claims_unsettled  REAL,                 -- Qoniqtirilmagan sug'urta da'volari soni (не урегулировано)
    contracts_active  REAL,                 -- Amalda bo'lgan shartnomalar soni (действующие на дату)
    contracts_new     REAL,                 -- Yangi tuzilgan shartnomalar soni (заключены с начала года)
    payouts_mln       REAL,                 -- Sug'urta to'lovlari, общее страхование, млн сум
    source_file       TEXT,
    loaded_at         TEXT NOT NULL,
    PRIMARY KEY (report_date, scope, key)
);
-- Обособленные подразделения (alohida bo'linmalar): листы 2.12 (премии), 2.13 (выплаты), 2.14 (договоры) —
-- компания × регион, только текущий срез отчёта. region_key 'total' — итог компании, company_key 'total' — рынок.
CREATE TABLE IF NOT EXISTS napp_branches (
    report_date   TEXT NOT NULL,
    company_key   TEXT NOT NULL,            -- 'company:INSON AJ' | 'total'
    company_name  TEXT NOT NULL,
    sphere        TEXT,                     -- 'general' (Umumiy sug'urta sohasi) | 'life' (Hayotni sug'urta qilish)
    region_key    TEXT NOT NULL,            -- 'region:TOSHKENT' | 'total'
    region_name   TEXT NOT NULL,
    premiums_mln  REAL,
    payouts_mln   REAL,
    contracts     REAL,                     -- договоров (действующих на дату — итог совпадает с листом 3.4)
    source_file   TEXT,
    loaded_at     TEXT NOT NULL,
    PRIMARY KEY (report_date, company_key, region_key)
);

-- Отчётность по резервам (Положение 1882): на отчётную дату, по учётной группе или виду
CREATE TABLE IF NOT EXISTS reserve_reports (
    report_date  TEXT NOT NULL,
    scope_type   TEXT NOT NULL,             -- 'группа' | 'класс' | 'вид' | 'итого'
    scope_code   TEXT NOT NULL,             -- '1'..'4', код класса, 'ОСГО', 'итого'
    rnp          REAL DEFAULT 0,            -- резерв незаработанной премии
    rzu          REAL DEFAULT 0,            -- заявленные, неурегулированные
    rpnu         REAL DEFAULT 0,            -- произошедшие, незаявленные
    stab         REAL DEFAULT 0,            -- стабилизационные (ОСГО/ОСГОР/ОСГОП)
    cat_reserve  REAL DEFAULT 0,            -- резерв катастроф
    other        REAL DEFAULT 0,
    base_premium_12m REAL,                  -- базовая премия за 12 мес. (для нормы РПНУ ≥ 10%)
    source       TEXT,
    PRIMARY KEY (report_date, scope_type, scope_code)
);

-- Отчёты о резервах по периодам из админки (app/finance.py): год + квартал или годовая отчётность.
-- Отдельно от reserve_reports: у Q4 и годового отчёта одна дата 31.12, а ключ там — дата.
-- Строки не удаляются: замена и удаление меняют status, старая версия остаётся в истории.
CREATE TABLE IF NOT EXISTS reserve_periods (
    id               INTEGER PRIMARY KEY,
    period_year      INTEGER NOT NULL,          -- 2000..2100
    period_type      TEXT NOT NULL,             -- 'Q1'..'Q4' | 'Y' (годовая отчётность)
    report_date      TEXT NOT NULL,             -- конец квартала или 31.12 — для совместимости с report_date
    total            REAL NOT NULL,             -- сумма всех резервов, сум
    base_premium_12m REAL,                      -- базовая премия за 12 мес. (норма РПНУ ≥ 10%, 1882 п. 23)
    source           TEXT NOT NULL,             -- 'вручную' | 'файл'
    file_name        TEXT,
    status           TEXT NOT NULL DEFAULT 'действует', -- 'действует' | 'заменён' | 'удалён'
    created_by       TEXT,                      -- логин, не ФИО
    created_at       TEXT NOT NULL,
    closed_by        TEXT,                      -- кто заменил или удалил
    closed_at        TEXT,
    replaced_by      INTEGER REFERENCES reserve_periods(id)
);
-- один действующий отчёт на (год, период); история замен не мешает
CREATE UNIQUE INDEX IF NOT EXISTS ux_reserve_periods_active ON reserve_periods(period_year, period_type) WHERE status = 'действует';
CREATE INDEX IF NOT EXISTS ix_reserve_periods_date ON reserve_periods(status, report_date);

-- Строки отчёта: вид резерва (колонки reserve_reports) × учётная группа (1882 п. 10) или вид ОСГО/ОСГОР/ОСГОП
CREATE TABLE IF NOT EXISTS reserve_period_lines (
    period_id    INTEGER NOT NULL REFERENCES reserve_periods(id),
    reserve_code TEXT NOT NULL,                 -- 'rnp' | 'rzu' | 'rpnu' | 'stab' | 'cat_reserve' | 'other'
    group_code   TEXT NOT NULL DEFAULT '',      -- '1'..'4' для РНП; 'ОСГО' | 'ОСГОР' | 'ОСГОП' для стабилизационных; '' — без группы
    amount       REAL NOT NULL,                 -- сум, ≥ 0
    PRIMARY KEY (period_id, reserve_code, group_code)
);

-- Выделенные активы под резервы (1882, гл. III–IV)
CREATE TABLE IF NOT EXISTS allocated_assets (
    report_date  TEXT NOT NULL,
    category     TEXT NOT NULL,             -- 'госбумаги' | 'депозиты' | 'деньги' | 'ценные бумаги' | 'недвижимость' | 'прочее'
    amount       REAL NOT NULL,
    is_liquid    INTEGER NOT NULL DEFAULT 1,-- входит в норму «не менее 70%» (п. 38)
    PRIMARY KEY (report_date, category)
);

-- Показатели платёжеспособности (1806): для маржи и ёмкости
CREATE TABLE IF NOT EXISTS solvency_reports (
    report_date        TEXT PRIMARY KEY,
    own_funds          REAL NOT NULL,       -- итог раздела I пассива
    deductions         REAL DEFAULT 0,      -- вычеты по п. 14
    premiums_12m       REAL,                -- начисленные премии за 12 мес. (за вычетом возвратов)
    claims_36m         REAL,                -- начисленные возмещения за 36 мес.
    claims_36m_net     REAL,                -- то же за вычетом доли перестраховщиков
    min_capital        REAL,                -- минимальный уставный капитал по закону
    top5_liabilities   REAL,                -- обязательства по пяти крупнейшим рискам
    source             TEXT
);

-- ============ РАБОЧИЕ ДАННЫЕ ============

CREATE TABLE IF NOT EXISTS agents (
    id             INTEGER PRIMARY KEY,
    eais_id        TEXT UNIQUE,             -- ID из Единой системы
    name           TEXT NOT NULL,
    kind           TEXT NOT NULL,           -- 'физ' | 'юр' | 'банк'
    commission_pct REAL,
    trained_at     TEXT,                    -- курс не менее 12 часов
    monitored_at   TEXT,                    -- мониторинг раз в полгода
    status         TEXT NOT NULL DEFAULT 'активен'
);

CREATE TABLE IF NOT EXISTS requests (
    id            INTEGER PRIMARY KEY,
    external_no   TEXT,                     -- номер договора в учётной системе
    branch        TEXT,
    product_code  TEXT REFERENCES products(code),
    policyholder  TEXT,
    beneficiary   TEXT,
    agent_id      INTEGER REFERENCES agents(id),
    created_by_user_id INTEGER REFERENCES users(id),   -- кто подал запрос из приложения (роль «сотрудник» не привязана к ЕАИС)
    created_at    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'новый',
    -- коллективное согласование (app/approvals.py): 'не требуется'|'на согласовании'|'согласован'|'отклонён'
    approval_status TEXT NOT NULL DEFAULT 'не требуется',
    -- по какому генеральному соглашению идёт запрос; без REFERENCES: general_agreements
    -- описана ниже по файлу, а PostgreSQL ссылку вперёд при CREATE TABLE не примет
    general_agreement_id INTEGER
);

CREATE TABLE IF NOT EXISTS objects (
    id           INTEGER PRIMARY KEY,
    request_id   INTEGER NOT NULL REFERENCES requests(id),
    object_type  TEXT NOT NULL,
    address      TEXT,
    region       TEXT,
    seismic_zone INTEGER,
    value_amount REAL,                      -- страховая стоимость
    sum_insured  REAL,
    franchise    REAL DEFAULT 0,
    attributes   TEXT                       -- JSON: материал, год, площадь, защита
);

CREATE TABLE IF NOT EXISTS object_perils (
    object_id  INTEGER NOT NULL REFERENCES objects(id),
    peril_code TEXT NOT NULL REFERENCES perils(code),
    included   INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (object_id, peril_code)
);

CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY,
    request_id  INTEGER NOT NULL REFERENCES requests(id),
    doc_name    TEXT NOT NULL,
    file_path   TEXT,
    received    INTEGER NOT NULL DEFAULT 0,
    extracted   TEXT                        -- JSON: что ИИ извлёк из файла
);

CREATE TABLE IF NOT EXISTS calculations (
    id                INTEGER PRIMARY KEY,
    request_id        INTEGER NOT NULL REFERENCES requests(id),
    object_id         INTEGER REFERENCES objects(id),
    tariff_version_id INTEGER REFERENCES tariff_versions(id),
    net_rate_pct      REAL,                 -- нетто
    risk_load_pct     REAL,                 -- рисковая надбавка
    cat_load_pct      REAL,                 -- катастрофическая надбавка
    gross_rate_pct    REAL,                 -- техническая брутто-ставка
    min_rate_pct      REAL,                 -- сработавший минимум
    applied_rate_pct  REAL,                 -- что поставил агент
    premium           REAL,
    pml_amount        REAL,
    verdict           TEXT,                 -- 'ок' | 'на утверждение' | 'отклонено'
    explanation       TEXT,                 -- JSON: вклад каждого коэффициента
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS check_results (
    id             INTEGER PRIMARY KEY,
    calculation_id INTEGER NOT NULL REFERENCES calculations(id),
    rule_code      TEXT NOT NULL REFERENCES rules(code),
    status         TEXT NOT NULL,           -- 'пройдено' | 'нарушено'
    detail         TEXT
);

CREATE TABLE IF NOT EXISTS recommendations (
    id             INTEGER PRIMARY KEY,
    calculation_id INTEGER NOT NULL REFERENCES calculations(id),
    kind           TEXT NOT NULL,           -- 'франшиза' | 'риск' | 'защита' | 'страховая сумма'
    text           TEXT NOT NULL,
    premium_delta  REAL                     -- на сколько изменится премия
);

CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY,
    ts       TEXT NOT NULL,
    who      TEXT,
    action   TEXT NOT NULL,
    entity   TEXT,
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit(ts);                               -- журнал за сутки (доклад)

-- ============ ПОРТФЕЛЬНЫЙ АУДИТ ============
-- Загрузка выгрузки договоров из учётной системы и массовая проверка движком (app/portfolio.py)

CREATE TABLE IF NOT EXISTS portfolio_batches (
    id           INTEGER PRIMARY KEY,
    imported_at  TEXT NOT NULL,
    file_name    TEXT,
    rows_total   INTEGER NOT NULL DEFAULT 0,
    rows_ok      INTEGER NOT NULL DEFAULT 0,
    rows_warn    INTEGER NOT NULL DEFAULT 0,
    rows_stop    INTEGER NOT NULL DEFAULT 0,
    mapping      TEXT                        -- JSON: поле -> найденная колонка
);

CREATE TABLE IF NOT EXISTS portfolio_reviews (
    id                 INTEGER PRIMARY KEY,
    batch_id           INTEGER NOT NULL REFERENCES portfolio_batches(id),
    imported_at        TEXT NOT NULL,
    file_name          TEXT,
    external_no        TEXT,                 -- номер договора из выгрузки
    product_code       TEXT,
    policyholder       TEXT,
    branch             TEXT,
    sum_insured        REAL,
    value_amount       REAL,
    premium_file       REAL,                 -- премия по выгрузке
    premium_calc       REAL,                 -- премия по расчётной ставке
    applied_rate_pct   REAL,
    technical_rate_pct REAL,
    min_rate_pct       REAL,
    verdict            TEXT,                 -- 'ок' | 'на утверждение' | 'отклонено' | 'не проверен'
    violations         TEXT,                 -- JSON: [{rule, status, title}]
    row_json           TEXT                  -- JSON: исходная строка выгрузки
);

CREATE INDEX IF NOT EXISTS ix_portfolio_batch ON portfolio_reviews(batch_id);

-- Портфель по шаблону отчётности (GET /portfolio/template.xlsx, 22.09.2026): все поля договора,
-- нужные сводке, аналитике риска и резервам. ФИО не храним вовсе; ИНН — только у юрлиц.
-- Выгрузки старого вида (без шаблона) тоже кладутся сюда — теми полями, что в них нашлись.
CREATE TABLE IF NOT EXISTS portfolio_contracts (
    id             INTEGER PRIMARY KEY,
    batch_id       INTEGER NOT NULL REFERENCES portfolio_batches(id),
    row_no         INTEGER,                  -- строка в файле
    external_no    TEXT,                     -- номер договора
    date_signed    TEXT,                     -- дата заключения, ГГГГ-ММ-ДД
    date_from      TEXT,
    date_to        TEXT,
    product_code   TEXT,
    class_code     TEXT,                     -- из файла, иначе первый класс продукта по справочнику
    branch         TEXT,
    region         TEXT,
    holder_type    TEXT,                     -- 'юрлицо' | 'физлицо'
    holder_inn     TEXT,                     -- только для юрлиц; у физлиц всегда NULL
    object_type    TEXT,
    sum_insured    REAL,
    value_amount   REAL,
    premium        REAL,                     -- начисленная
    premium_paid   REAL,                     -- оплаченная
    franchise      REAL,
    rate_pct       REAL,
    seismic_zone   INTEGER,
    currency       TEXT NOT NULL DEFAULT 'UZS',
    status         TEXT,                     -- 'действует' | 'расторгнут' | 'истёк'
    claims_count   INTEGER,
    claims_claimed REAL,
    claims_paid    REAL,
    last_loss_date TEXT
);
CREATE INDEX IF NOT EXISTS ix_portfolio_contracts_batch ON portfolio_contracts(batch_id);

-- Убытки из листа «Убытки» того же шаблона. Отдельно от claims (там убытки по запросам сюрвейера).
CREATE TABLE IF NOT EXISTS portfolio_claims (
    id            INTEGER PRIMARY KEY,
    batch_id      INTEGER NOT NULL REFERENCES portfolio_batches(id),
    row_no        INTEGER,
    contract_no   TEXT NOT NULL,
    claim_no      TEXT,
    event_date    TEXT,
    reported_date TEXT,
    claimed       REAL,
    paid          REAL,
    status        TEXT,                     -- 'заявлен' | 'оплачен' | 'отказ'
    cause         TEXT
);
CREATE INDEX IF NOT EXISTS ix_portfolio_claims_batch ON portfolio_claims(batch_id);

-- ============ КАЛИБРОВКА ТАРИФОВ ============

-- Убытки по договорам: основа для burning cost и частоты (учебник CII M97).
-- Пока выгрузок компании нет, таблица пустая; синтетика — только в тестах на копии базы.
CREATE TABLE IF NOT EXISTS claims (
    id            INTEGER PRIMARY KEY,
    request_id    INTEGER REFERENCES requests(id),  -- договор (запрос), по которому убыток
    external_no   TEXT,                     -- номер убытка в учётной системе
    class_code    TEXT,                     -- класс страхования (если не задан — берётся из договора)
    object_type   TEXT,                     -- тип объекта (если не задан — из договора)
    event_date    TEXT,                     -- дата события, ГГГГ-ММ-ДД
    reported_date TEXT,                     -- дата заявления
    paid_date     TEXT,                     -- дата выплаты
    cause         TEXT,                     -- причина: пожар, кража, ...
    claimed       REAL,                     -- заявлено
    paid          REAL,                     -- выплачено
    status        TEXT NOT NULL DEFAULT 'заявлен', -- 'заявлен' | 'оплачен' | 'урегулирован' | 'в работе' | 'отказ'
    source        TEXT,                     -- откуда запись: учётная система, ручной ввод, Excel
    -- поля загрузки из Excel (app/claims_import.py); ФИО страхователя не храним
    product_code  TEXT,                     -- код продукта (если убыток не привязан к запросу сюрвейера)
    contract_no   TEXT,                     -- номер договора в учётной системе
    branch        TEXT,                     -- филиал
    region        TEXT,                     -- регион
    sum_insured   REAL,                     -- страховая сумма по договору
    premium       REAL,                     -- премия по договору
    batch_id      INTEGER,                  -- последняя загрузка, которая создала или обновила строку (claim_batches)
    updated_at    TEXT
);

-- Загрузки страховых случаев из Excel: одна строка — один файл
CREATE TABLE IF NOT EXISTS claim_batches (
    id         INTEGER PRIMARY KEY,
    file_name  TEXT,
    rows       INTEGER NOT NULL DEFAULT 0,  -- строк в файле
    created    INTEGER NOT NULL DEFAULT 0,  -- новых случаев
    updated    INTEGER NOT NULL DEFAULT 0,  -- обновлено по номеру дела
    skipped    INTEGER NOT NULL DEFAULT 0,  -- строк с ошибками (не записаны)
    loaded_by  TEXT,
    loaded_at  TEXT NOT NULL
);

-- Прогоны калибровки: каждое предложение хранится целиком, применяется только через утверждение
CREATE TABLE IF NOT EXISTS calibration_runs (
    id          INTEGER PRIMARY KEY,
    run_at      TEXT NOT NULL,
    period_from TEXT NOT NULL,
    period_to   TEXT NOT NULL,
    contracts   INTEGER,                    -- договоров в выборке
    claims      INTEGER,                    -- убытков в выборке
    method      TEXT,                       -- как считали (burning cost + теория доверия)
    credibility REAL,                       -- доверие Z по всей выборке
    proposals   TEXT,                       -- JSON: сводка и список предложений
    status      TEXT NOT NULL DEFAULT 'предложено', -- 'предложено' | 'утверждено' | 'отклонено'
    approved_by TEXT,
    approved_at TEXT,
    note        TEXT
);

CREATE INDEX IF NOT EXISTS ix_claims_request ON claims(request_id);
CREATE INDEX IF NOT EXISTS ix_claims_event ON claims(event_date);
CREATE INDEX IF NOT EXISTS ix_claims_external_no ON claims(external_no);   -- повторная загрузка дела — обновление

-- ============ ПОЛЬЗОВАТЕЛИ И ДОСТУП (app/auth.py) ============

-- Учётные записи людей. Заявка -> сверка с реестром агентов (таблица agents, ID из ЕАИС по Положению 3845 п. 4)
-- -> подтверждение админом -> вход. Пароль хранится только как PBKDF2-HMAC-SHA256 с солью на пользователя.
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    login         TEXT NOT NULL UNIQUE,
    full_name     TEXT NOT NULL,
    phone         TEXT,
    role          TEXT NOT NULL DEFAULT 'агент',   -- 'агент' | 'андеррайтер' | 'актуарий' | 'админ'
    branch        TEXT,
    agent_eais_id TEXT,                            -- для роли «агент» обязателен: без ID в ЕАИС агент работать не может
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'ожидает подтверждения', -- | 'активен' | 'заблокирован'
    telegram_id   TEXT,                            -- если задан — при входе нужен код из Telegram
    position      TEXT,                            -- должность со слов самого человека (регистрация в мини-приложении)
    department    TEXT,                            -- департамент со слов самого человека (свободный текст с подсказками)
    google_sub    TEXT,                            -- вход через Google: вечный идентификатор аккаунта (app/google_auth.py)
    email         TEXT,                            -- почта из аккаунта Google (адрес подтверждён самим Google)
    unit          TEXT,                            -- отдел внутри департамента (заводит администратор вручную)
    must_change_password INTEGER NOT NULL DEFAULT 0, -- 1 = временный пароль от администратора: сменить при первом входе
    created_at    TEXT NOT NULL,
    approved_by   TEXT,                            -- логин админа, подтвердившего заявку
    approved_at   TEXT,
    last_login    TEXT
);

-- Сессии: случайный токен в httpOnly-cookie «sid», 12 часов, продлевается при активности
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ip         TEXT,
    user_agent TEXT
);

-- Одноразовые коды второго шага входа (6 цифр, 5 минут)
CREATE TABLE IF NOT EXISTS login_codes (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    code       TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    channel    TEXT NOT NULL DEFAULT 'telegram',  -- 'telegram' | 'sms' | 'email'
    attempts   INTEGER NOT NULL DEFAULT 0         -- неверных попыток; после 5 код сгорает
);

CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS ix_login_codes_user ON login_codes(user_id);
-- один аккаунт Google — одна учётная запись. Частичный индекс: пустой google_sub у всех,
-- кто входит по паролю или через Telegram, уникальности не мешает.
-- Синтаксис общий для SQLite и PostgreSQL.
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_google_sub ON users(google_sub) WHERE google_sub IS NOT NULL;
-- индексы частых запросов (задача 123, подобраны по EXPLAIN QUERY PLAN)
CREATE INDEX IF NOT EXISTS ix_users_telegram ON users(telegram_id);                -- вход из Telegram, бот
CREATE INDEX IF NOT EXISTS ix_users_phone ON users(phone);                         -- регистрация по телефону

-- ---------------------------------------------------------------------------
-- Вход через Google (app/google_auth.py, OAuth 2.0 + PKCE).
-- Начатые входы: state живёт 10 минут и гасится при первом же возврате от Google.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS google_oauth_states (
    state         TEXT PRIMARY KEY,           -- 32 случайных байта, он же лежит в cookie «gstate»
    code_verifier TEXT NOT NULL,              -- секрет PKCE: Google получит только его отпечаток
    next_url      TEXT,                       -- куда вернуть человека после входа (только свой путь)
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,              -- 10 минут
    ip            TEXT
);

-- Одноразовые коды после возврата от Google: код обмена (2 минуты) и ключ анкеты (30 минут).
-- В базе только отпечаток sha256 — из таблицы код не восстановить.
CREATE TABLE IF NOT EXISTS google_login_codes (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,              -- 'обмен' | 'анкета'
    code_hash     TEXT NOT NULL,              -- sha256 от выданного кода
    google_sub    TEXT NOT NULL,
    email         TEXT,
    profile_json  TEXT,                       -- имя и фамилия из аккаунта — подставляются в анкету
    user_id       INTEGER REFERENCES users(id),   -- заполнен, если учётная запись уже есть
    session_token TEXT,                       -- сессия, созданная в callback: отдаётся на обмене
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    used_at       TEXT,                       -- сгорел при первом использовании
    ip            TEXT
);

CREATE INDEX IF NOT EXISTS ix_google_login_codes_hash ON google_login_codes(code_hash);

CREATE INDEX IF NOT EXISTS ix_requests_created ON requests(created_at);
CREATE INDEX IF NOT EXISTS ix_calc_request ON calculations(request_id);
CREATE INDEX IF NOT EXISTS ix_objects_request ON objects(request_id);
CREATE INDEX IF NOT EXISTS ix_perils_class ON perils(class_code);
-- индексы частых запросов (задача 123, подобраны по EXPLAIN QUERY PLAN)
CREATE INDEX IF NOT EXISTS ix_requests_status ON requests(status);                 -- список запросов по статусу
CREATE INDEX IF NOT EXISTS ix_requests_branch ON requests(branch);                 -- ... по филиалу
CREATE INDEX IF NOT EXISTS ix_requests_agent ON requests(agent_id);                -- «свои» запросы агента
CREATE INDEX IF NOT EXISTS ix_requests_author ON requests(created_by_user_id);     -- «свои» запросы автора
CREATE INDEX IF NOT EXISTS ix_requests_approval ON requests(approval_status);      -- ждут согласования
CREATE INDEX IF NOT EXISTS ix_check_results_calc ON check_results(calculation_id); -- карточка запроса
CREATE INDEX IF NOT EXISTS ix_recommendations_calc ON recommendations(calculation_id);
CREATE INDEX IF NOT EXISTS ix_documents_request ON documents(request_id);

-- ============ ДЕЛЕГИРОВАНИЕ И ОТЧЁТЫ ============

-- Задачи агентов: заказчик ставит задачу → руководитель разбивает и раздаёт → исполнители отчитываются
CREATE TABLE IF NOT EXISTS agent_tasks (
    id          INTEGER PRIMARY KEY,
    parent_id   INTEGER REFERENCES agent_tasks(id),   -- подзадача родительской
    created_at  TEXT NOT NULL,
    author      TEXT NOT NULL,                        -- кто поставил: 'заказчик' | код агента
    assignee    TEXT NOT NULL,                        -- код агента-исполнителя
    title       TEXT NOT NULL,
    detail      TEXT,
    status      TEXT NOT NULL DEFAULT 'новая',        -- 'новая' | 'в работе' | 'сделана' | 'ждёт заказчика' | 'отменена'
    result      TEXT,
    started_at  TEXT,
    done_at     TEXT
);

-- Ежедневные отчёты: собираются сервером в 08:00, хранятся как markdown
CREATE TABLE IF NOT EXISTS daily_reports (
    report_date TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    body_md     TEXT NOT NULL,
    stats       TEXT                                   -- JSON с цифрами, из которых собран текст
);

-- ============ ЗНАНИЯ КОМАНДЫ ============
-- Пробелы в знаниях и их закрытие. Перестрахование в продукт не входит (решение заказчика 20.09.2026),
-- но понятие о нём команда обязана иметь — поэтому оно живёт здесь как тема, а не как модуль расчёта.

CREATE TABLE IF NOT EXISTS knowledge_topics (
    id          INTEGER PRIMARY KEY,
    topic       TEXT NOT NULL UNIQUE,
    area        TEXT NOT NULL,                       -- 'закон'|'актуарий'|'рынок'|'перестрахование'|'андеррайтинг'|'такафул'|'ит'
    status      TEXT NOT NULL DEFAULT 'пробел',      -- 'пробел' | 'изучено'
    priority    INTEGER NOT NULL DEFAULT 5,          -- 1 — самый срочный
    source_hint TEXT,                                -- где искать: учебник, норма, сайт
    created_at  TEXT NOT NULL,
    learned_at  TEXT,
    note_path   TEXT                                 -- путь к заметке в docs/Знания
);

CREATE TABLE IF NOT EXISTS knowledge_log (
    id       INTEGER PRIMARY KEY,
    ts       TEXT NOT NULL,
    agent    TEXT NOT NULL,                          -- код агента: law | actuary | data | backend | ui | reviewer | lead
    topic_id INTEGER REFERENCES knowledge_topics(id),
    what     TEXT NOT NULL,                          -- что именно понято
    source   TEXT,                                   -- откуда
    minutes  INTEGER                                 -- сколько времени заняло
);

CREATE INDEX IF NOT EXISTS ix_knowledge_topics_status ON knowledge_topics(status, priority);
CREATE INDEX IF NOT EXISTS ix_knowledge_log_ts ON knowledge_log(ts);

-- ============ ФОТОГРАФИИ ОБЪЕКТА ============
-- Файлы лежат в data/photos/<request_id>/, в базе только путь относительно корня проекта.
-- Имя файла генерируем сами: клиентскому имени не доверяем (оно хранится отдельно как filename).

CREATE TABLE IF NOT EXISTS photos (
    id          INTEGER PRIMARY KEY,
    request_id  INTEGER NOT NULL REFERENCES requests(id),
    path        TEXT NOT NULL,             -- относительный путь: data/photos/12/ab12cd.jpg
    filename    TEXT,                      -- как файл назывался у агента (только для показа)
    mime        TEXT NOT NULL,             -- image/jpeg | image/png | image/webp
    size_bytes  INTEGER NOT NULL,
    uploaded_at TEXT NOT NULL,
    uploaded_by TEXT,
    note        TEXT,
    -- вид файла: 'фото объекта' (по умолчанию) | 'техпаспорт' | 'кадастр'.
    -- PDF принимается только для документов, для фото объекта — нет.
    doc_kind    TEXT NOT NULL DEFAULT 'фото объекта',
    -- результат разбора документа: храним, чтобы не разбирать один и тот же файл дважды
    parse_status TEXT,                     -- 'разобран' | 'частично' | 'нужно распознавание' | 'ошибка'
    parsed_at    TEXT,
    parsed_json  TEXT                      -- JSON: поля, подписи, уверенность (без персональных данных)
);

CREATE INDEX IF NOT EXISTS ix_photos_request ON photos(request_id);
CREATE INDEX IF NOT EXISTS ix_photos_kind ON photos(request_id, doc_kind);

-- ============ ОЦЕНКА СТОИМОСТИ ОБЪЕКТА ============
-- Каркас: движок оценки пишет актуарий. Здесь только хранение норм, настроек и результатов.

-- Нормы амортизации (износа). Редактируются через админку.
-- rate_pct — процент износа за год; residual_min_pct — ниже какой доли от новой цены не опускаемся.
CREATE TABLE IF NOT EXISTS depreciation_norms (
    code              TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    rate_pct          REAL NOT NULL,
    residual_min_pct  REAL,
    calibrated        INTEGER NOT NULL DEFAULT 0,   -- 0 — экспертное значение, статистикой не подтверждено
    source            TEXT,
    note              TEXT
);

-- Настройки оценки: ключ — значение. Держим текстом, тип разбирает модуль (перенос на PostgreSQL без изменений).
CREATE TABLE IF NOT EXISTS valuation_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    name        TEXT NOT NULL,
    unit        TEXT,
    calibrated  INTEGER NOT NULL DEFAULT 0,
    source      TEXT,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS valuations (
    id                      INTEGER PRIMARY KEY,
    created_at              TEXT NOT NULL,
    created_by              TEXT,
    request_id              INTEGER REFERENCES requests(id),
    object_type             TEXT,
    params_json             TEXT,          -- JSON: что ввёл агент (год, пробег, площадь, состояние)
    declared_value          REAL,          -- заявленная страхователем стоимость
    ai_value                REAL,          -- что получилось у системы
    method                  TEXT,          -- как считали: 'объявления' | 'износ' | 'смешанный'
    method_version          TEXT,          -- версия методики — старые оценки должны воспроизводиться
    spread_json             TEXT,          -- JSON: вилка (минимум, медиана, максимум)
    explanation             TEXT,          -- JSON: из чего сложилась цифра
    confirmed_by_underwriter TEXT,
    confirmed_at            TEXT
);

-- По одной строке на источник данных. status обязателен: даже «нет данных» — это результат.
CREATE TABLE IF NOT EXISTS valuation_sources (
    id           INTEGER PRIMARY KEY,
    valuation_id INTEGER NOT NULL REFERENCES valuations(id),
    source       TEXT NOT NULL,
    status       TEXT NOT NULL,            -- 'ок' | 'нет данных' | 'источник недоступен' | 'запрещено robots.txt'
    url          TEXT,
    ads_count    INTEGER,
    median       REAL,
    q1           REAL,
    q3           REAL,
    samples_json TEXT,
    fetched_at   TEXT
);

CREATE INDEX IF NOT EXISTS ix_valuations_request ON valuations(request_id);
CREATE INDEX IF NOT EXISTS ix_valuation_sources_val ON valuation_sources(valuation_id);

-- ============ ОТКРЫТЫЕ ДАННЫЕ АГЕНТСТВА (КОМИТЕТА) СТАТИСТИКИ ============
-- Заполняется модулем app/stat_sources.py (реестр DATASETS).
-- Правила: каждая строка обязана нести ссылку на страницу набора (url);
-- история не перезаписывается — новые периоды добавляются, старые остаются.
-- Повторная загрузка того же периода не плодит дубли: первичный ключ
-- (source, dataset_id, key, region, period). Если значение изменилось
-- (статистика пересчитала ряд) — прежнее значение уходит в stat_series_revisions,
-- а в stat_series остаётся действующее. Так ряд всегда актуален, но видно,
-- что и когда пересчитали: расчёт прошлых лет можно воспроизвести.

CREATE TABLE IF NOT EXISTS stat_series (
    source      TEXT NOT NULL,             -- 'stat.uz' | 'data.egov.uz'
    dataset_id  TEXT NOT NULL,             -- наш ключ набора: 'population', 'housing_fund_area' ...
    key         TEXT NOT NULL,             -- ключ строки у источника (код СОАТО, код КИПЦ, вид ЧС)
    region      TEXT NOT NULL DEFAULT '',  -- 'region:ANDIJON' как в market_stats; 'total' = вся республика; '' = разреза нет
    period      TEXT NOT NULL,             -- '2025' | '2025-Q2' | '2026-M08'
    value       REAL,                      -- NULL = у источника нет значения (не придумываем)
    unit        TEXT,                      -- единица измерения как у источника
    fetched_at  TEXT NOT NULL,             -- когда мы забрали
    url         TEXT NOT NULL,             -- страница набора у источника (обязательна)
    PRIMARY KEY (source, dataset_id, key, region, period)
);

-- Журнал пересчётов: прежние значения тех же точек ряда.
CREATE TABLE IF NOT EXISTS stat_series_revisions (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    dataset_id  TEXT NOT NULL,
    key         TEXT NOT NULL,
    region      TEXT NOT NULL DEFAULT '',
    period      TEXT NOT NULL,
    old_value   REAL,
    new_value   REAL,
    old_fetched_at TEXT,
    replaced_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_stat_series_dataset ON stat_series(dataset_id, region, period);
CREATE INDEX IF NOT EXISTS ix_stat_series_period  ON stat_series(period);
CREATE INDEX IF NOT EXISTS ix_stat_revisions_key  ON stat_series_revisions(dataset_id, region, period);
-- индексы частых запросов (задача 123, подобраны по EXPLAIN QUERY PLAN)
CREATE INDEX IF NOT EXISTS ix_stat_series_fetched ON stat_series(fetched_at);      -- что обновилось за сутки

-- ---------------------------------------------------------------------------
-- Коллективное согласование запросов (несколько человек на один запрос).
-- Поводом служит, в частности, генеральное соглашение с банком-партнёром.
-- ---------------------------------------------------------------------------

-- Генеральные соглашения с партнёрами (банк, лизинг и т. п.).
CREATE TABLE IF NOT EXISTS general_agreements (
    id           INTEGER PRIMARY KEY,
    partner      TEXT NOT NULL,                 -- «Asia Alliance Bank»
    product_code TEXT REFERENCES products(code),
    terms        TEXT,                          -- условия соглашения как есть, текстом
    default_reviewers TEXT,                     -- JSON-список user_id (или ФИО, если пользователя ещё нет)
    status       TEXT NOT NULL DEFAULT 'черновик', -- 'черновик' | 'действует' | 'расторгнуто'
    created_at   TEXT NOT NULL,
    note         TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_gen_agr_partner_product
    ON general_agreements(partner, COALESCE(product_code, ''));

-- Состав согласующих по запросу и их решения.
-- ФИО и должность копируются из users на момент назначения: состав решения
-- должен читаться спустя годы, даже если пользователя переименовали или уволили.
CREATE TABLE IF NOT EXISTS request_reviewers (
    id          INTEGER PRIMARY KEY,
    request_id  INTEGER NOT NULL REFERENCES requests(id),
    user_id     INTEGER NOT NULL REFERENCES users(id),
    full_name   TEXT NOT NULL,
    position    TEXT,                           -- роль/должность на момент назначения
    order_no    INTEGER NOT NULL DEFAULT 1,
    status      TEXT NOT NULL DEFAULT 'ожидает',-- 'ожидает' | 'одобрил' | 'отклонил' | 'вопрос'
    comment     TEXT,
    assigned_at TEXT NOT NULL,
    assigned_by TEXT,
    decided_at  TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_request_reviewers ON request_reviewers(request_id, user_id);
CREATE INDEX IF NOT EXISTS ix_request_reviewers_user ON request_reviewers(user_id, status);
CREATE INDEX IF NOT EXISTS ix_request_reviewers_decided ON request_reviewers(decided_at);

-- ============ НАСТРОЙКИ, ИИ И ЗАПУСК НА СЕРВЕРЕ ============
-- Настройки приложения. Имеют приоритет над переменными окружения (.env): заказчик
-- меняет их на странице /admin/deploy, не заходя на сервер. Ключи наружу отдаются маской.
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,               -- LLM_PROVIDER | LLM_BASE_URL | LLM_MODEL | LLM_API_KEY |
                                               -- TELEGRAM_BOT_TOKEN | PD_MODE | SERVER_URL
    value      TEXT,
    updated_at TEXT
);

-- Журнал обращений к ИИ. Содержимого запроса и персональных данных здесь нет —
-- только назначение вызова, метрики и текст ошибки (правило проекта № 8).
CREATE TABLE IF NOT EXISTS llm_calls (
    id                INTEGER PRIMARY KEY,
    ts                TEXT NOT NULL,
    purpose           TEXT NOT NULL,           -- 'объяснение расчёта' | 'поля документа' | 'проверка связи' | ...
    provider          TEXT,
    model             TEXT,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    total_tokens      INTEGER,
    ms                INTEGER,
    ok                INTEGER NOT NULL DEFAULT 0,
    error             TEXT
);

CREATE INDEX IF NOT EXISTS ix_llm_calls_ts ON llm_calls(ts);

-- Чек-лист готовности к промышленной эксплуатации (страница /admin/deploy)
CREATE TABLE IF NOT EXISTS deploy_checklist (
    key        TEXT PRIMARY KEY,               -- postgres | domain | https | backup | admin
    title      TEXT NOT NULL,
    done       INTEGER NOT NULL DEFAULT 0,
    note       TEXT,
    updated_at TEXT
);

-- ---------------------------------------------------------------------------
-- СЛЕЖЕНИЕ ЗА ЗАКОНОДАТЕЛЬСТВОМ (app/lawwatch.py)
-- Робот раз в сутки смотрит страницы актов на lex.uz и ленты новых документов
-- и новостей. Решение о том, что менять в движке, принимает человек — робот
-- только приносит событие. Исходные данные — docs/Отслеживаемые акты.json.
-- ---------------------------------------------------------------------------

-- Реестр отслеживаемых актов. Наполняется из docs/Отслеживаемые акты.json (tools/db_build.py).
CREATE TABLE IF NOT EXISTS watched_acts (
    code            TEXT PRIMARY KEY,          -- 'gk_ch52' | 'pol_1806' | ...
    title           TEXT NOT NULL,
    kind            TEXT,                      -- 'закон' | 'кодекс' | 'положение' | 'постановление' | 'внутренний акт'
    lex_url         TEXT,                      -- NULL, если адрес не установлен (ничего не выдумываем)
    redaction       TEXT,                      -- дата редакции из реестра, ДД.ММ.ГГГГ
    priority        TEXT,                      -- 'высокий' | 'средний' | 'низкий'
    why             TEXT,                      -- зачем следим: что от акта зависит
    rules_refs      TEXT,                      -- JSON-список кодов правил движка
    text_hash       TEXT,                      -- sha256 нормализованного текста страницы
    text_len        INTEGER,                   -- длина нормализованного текста (страницы-«оболочки» короткие)
    last_checked_at TEXT,
    last_changed_at TEXT,
    status          TEXT NOT NULL DEFAULT 'следим'
        -- 'следим' | 'изменился' | 'источник недоступен' | 'адрес не найден' | 'следит юрист'
);

CREATE INDEX IF NOT EXISTS ix_watched_acts_status ON watched_acts(status, priority);

-- Журнал событий: что робот увидел. seen=0 — юрист ещё не разбирал.
CREATE TABLE IF NOT EXISTS law_events (
    id           INTEGER PRIMARY KEY,
    created_at   TEXT NOT NULL,
    kind         TEXT NOT NULL,                -- 'новая редакция' | 'новый документ' | 'новость'
    act_code     TEXT,                         -- пусто для новых документов и новостей
    title        TEXT NOT NULL,
    source       TEXT NOT NULL,                -- 'lex.uz' | 'napp.uz'
    url          TEXT,
    published_at TEXT,
    summary      TEXT,                         -- изложение системы, не текст акта
    seen         INTEGER NOT NULL DEFAULT 0,
    note         TEXT
);

CREATE INDEX IF NOT EXISTS ix_law_events_created ON law_events(created_at);
CREATE INDEX IF NOT EXISTS ix_law_events_seen ON law_events(seen, id);
CREATE INDEX IF NOT EXISTS ix_law_events_act ON law_events(act_code, id);

-- ---------------------------------------------------------------------------
-- Телеграм-бот (app/tgbot.py): приём обновлений, регистрация, согласование.
-- ПЕРСОНАЛЬНЫХ ДАННЫХ В ЭТИХ ТАБЛИЦАХ НЕТ (правило проекта № 8):
-- ни текстов сообщений, ни ФИО, ни телефонов — только идентификаторы и тип события.
-- ---------------------------------------------------------------------------

-- Журнал обмена с ботом: только «кто, куда, какого типа событие и получилось ли».
CREATE TABLE IF NOT EXISTS tg_messages (
    id          INTEGER PRIMARY KEY,
    update_id   INTEGER,                      -- номер обновления Telegram (для входящих)
    telegram_id TEXT,                         -- идентификатор чата/пользователя в Telegram
    user_id     INTEGER,                      -- наш пользователь, если он уже известен
    direction   TEXT NOT NULL,                -- 'in' | 'out'
    kind        TEXT NOT NULL,                -- 'start' | 'команда' | 'callback' | 'уведомление' | 'отказ' | 'ошибка'
    ok          INTEGER NOT NULL DEFAULT 1,
    error       TEXT,                         -- техническая причина (без текста сообщения)
    request_id  INTEGER,                      -- по какому запросу событие, если применимо
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_tg_messages_created ON tg_messages(created_at);
CREATE INDEX IF NOT EXISTS ix_tg_messages_tg ON tg_messages(telegram_id, id);

-- Обработанные обновления: Telegram повторяет доставку, второй раз обрабатывать нельзя.
CREATE TABLE IF NOT EXISTS tg_updates (
    update_id   INTEGER PRIMARY KEY,
    received_at TEXT NOT NULL
);

-- Состояние пошагового диалога регистрации. Хранится в базе, а не в памяти процесса:
-- перезапуск сервера не теряет начатую заявку.
CREATE TABLE IF NOT EXISTS tg_dialogs (
    telegram_id TEXT PRIMARY KEY,
    step        TEXT NOT NULL,                -- 'согласие' | 'фио' | 'филиал' | 'должность' | 'роль' | 'id агента' | 'вопрос'
    draft       TEXT,                         -- JSON-черновик заявки; удаляется вместе со строкой после завершения
    updated_at  TEXT NOT NULL
);

-- Согласия на обработку персональных данных: факт, версия текста, канал и дата.
-- Самого текста согласия и данных человека здесь нет — только ссылка на версию.
CREATE TABLE IF NOT EXISTS pd_consents (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER,                      -- заполняется, когда заявка создана
    telegram_id TEXT NOT NULL,
    version     TEXT NOT NULL,                -- consent_version у юриста: 'ПД-1', 'ПД-2', ...
    channel     TEXT NOT NULL DEFAULT 'telegram',
    created_at  TEXT NOT NULL,                -- given_at у юриста: момент согласия
    -- раздел 7 docs/Регистрация и роли.md: доказательство, что текст не подменили задним числом
    consent_text_hash TEXT,
    scope       TEXT NOT NULL DEFAULT 'основное',   -- 'основное' | 'телефон' (телефон — отдельная строка)
    revoked_at  TEXT                          -- отзыв согласия: строки не удаляем, ставим дату (ст. 17 и 21 ЗРУ-547)
);

CREATE INDEX IF NOT EXISTS ix_pd_consents_tg ON pd_consents(telegram_id, id);

-- ---------------------------------------------------------------------------
-- Регистрация в мини-приложении по номеру телефона и коду из чата бота
-- (задача заказчика от 21.09.2026). Телефон здесь НЕ хранится: только его отпечаток —
-- нужен, чтобы на шаге проверки кода убедиться, что номер тот же. Сам номер живёт
-- единственный раз — в users.phone. Код тоже хранится только отпечатком.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reg_codes (
    id           INTEGER PRIMARY KEY,
    telegram_id  TEXT NOT NULL,
    phone_hash   TEXT NOT NULL,              -- sha256 от нормализованного номера
    code_hash    TEXT NOT NULL,              -- sha256 от кода, индивидуальной соли и telegram_id
    salt         TEXT,                       -- своя соль на каждый код: 6 цифр по общему хэшу перебираются мгновенно
    created_at   TEXT NOT NULL,              -- момент последней отправки (ограничение «не чаще раза в минуту»)
    expires_at   TEXT NOT NULL,              -- 10 минут с момента отправки
    attempts     INTEGER NOT NULL DEFAULT 0, -- неверных попыток; после 5 код сгорает
    verified_at  TEXT,                       -- код подтверждён, анкету можно отправлять
    -- защита от перебора (раздел 7 docs/Регистрация и роли.md)
    sent_total   INTEGER NOT NULL DEFAULT 0, -- сколько кодов выдано за сутки на этот telegram_id
    first_sent_at TEXT,                      -- начало суточного окна
    exhausted    INTEGER NOT NULL DEFAULT 0, -- сколько раз подряд исчерпаны попытки
    blocked_until TEXT                       -- после второй исчерпанной серии — пауза 15 минут
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_reg_codes_tg ON reg_codes(telegram_id);

-- ---------------------------------------------------------------------------
-- Вход из обычного браузера через Telegram: «обратный код» (app/tg_link.py).
-- Браузер получает одноразовый код INS-XXXX, человек отправляет его боту, бот привязывает
-- к заявке свой telegram_id, и страница входит сама. Самого кода в базе нет — только отпечаток
-- sha256(код + перец из app_settings): из выгрузки таблицы код не восстановить (правило № 8).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tg_link_codes (
    id           INTEGER PRIMARY KEY,
    link_id      TEXT NOT NULL,             -- анонимный ключ ожидания: знает только вкладка браузера
    code_hash    TEXT NOT NULL,             -- отпечаток кода; открытого кода нигде не хранится
    telegram_id  TEXT,                      -- кто прислал код боту (из обновления Telegram, не из текста)
    tg_user_json TEXT,                      -- id, username, имя — для link_or_request и remember_owner
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,             -- 10 минут с выдачи
    used_at      TEXT,                      -- код сработал: второй раз не принимается
    ip           TEXT                       -- адрес, откуда просили код: ограничение 5 кодов за 10 минут
);

CREATE INDEX IF NOT EXISTS ix_tg_link_codes_link ON tg_link_codes(link_id);
CREATE INDEX IF NOT EXISTS ix_tg_link_codes_hash ON tg_link_codes(code_hash);
CREATE INDEX IF NOT EXISTS ix_tg_link_codes_created ON tg_link_codes(created_at);

-- ---------------------------------------------------------------------------
-- Вероятность подтверждения запроса и то, чем дело кончилось на самом деле.
-- Одна таблица на прогноз и на факт: пишет её модуль app/analysis.py (актуарий),
-- читают app/outcomes.py, карточки и выгрузки. Состав полей — как в analysis.SCHEMA_SQL,
-- добавлена только result_json (полный ответ модуля: summary и «что повысит»).
-- probability хранится в процентах, 0..100 — ровно то число, что видел человек.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decision_outcomes (
    id             INTEGER PRIMARY KEY,
    request_id     INTEGER REFERENCES requests(id),      -- по какому запросу
    calculation_id INTEGER REFERENCES calculations(id),  -- какой расчёт показывали
    product_code   TEXT,                     -- продукт на момент отправки (разрез статистики)
    branch         TEXT,                     -- филиал (второй разрез)
    class_code     TEXT,
    probability    REAL NOT NULL,            -- 0..100 — что показали человеку
    verdict        TEXT,                     -- вердикт движка тогда же
    factors_json   TEXT,                     -- JSON: минусы и плюсы, как их посчитали
    model_version  TEXT,                     -- версия правил (сейчас 'prob-1')
    sent_at        TEXT NOT NULL,            -- когда запрос ушёл на согласование
    decision       TEXT,                     -- факт: 'согласован' | 'отклонён' | NULL пока нет
    decided_at     TEXT,
    decided_by     TEXT,
    comment        TEXT,
    result_json    TEXT                      -- полный ответ модуля: summary, how_to_raise, stat
);

CREATE INDEX IF NOT EXISTS ix_decision_outcomes_request ON decision_outcomes(request_id);
CREATE INDEX IF NOT EXISTS ix_decision_outcomes_scope
    ON decision_outcomes(product_code, branch, decision);

-- ---------------------------------------------------------------------------
-- Единое представление загруженного документа (app/ingest.py, задача заказчика 21.09.2026).
-- Любой файл (PDF, DOCX, XLSX) превращается в структуру: нормализованный текст, таблицы,
-- поля и факты. Остальные модули читают ЭТУ таблицу, а не сырой файл.
-- Сам файл лежит в photos (photo_id) — здесь дублируется только путь и имя для показа.
-- Персональные данные (ЗРУ-547, правило PD-01) в text/fields/facts не попадают:
-- ФИО, адрес физлица, паспорт и ПИНФЛ отбрасываются на этапе извлечения.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS document_extracts (
    id          INTEGER PRIMARY KEY,
    request_id  INTEGER NOT NULL REFERENCES requests(id),
    -- ON DELETE CASCADE: удалили файл в photos — представление уходит вместе с ним,
    -- иначе удаление фото упиралось бы во внешний ключ
    photo_id    INTEGER REFERENCES photos(id) ON DELETE CASCADE,
    file        TEXT,                            -- относительный путь: data/photos/12/ab12cd.docx
    filename    TEXT,                            -- как файл назывался у агента (только для показа)
    mime        TEXT,
    -- 'техпаспорт' | 'кадастр' | 'отчёт оценщика' | 'договор' | 'выписка' |
    -- 'штатное расписание' | 'прочее'
    kind        TEXT,
    kind_confidence  REAL,
    -- 'ru' | 'uz-latn' | 'uz-cyrl' | 'en' | 'mixed' | NULL (не определён)
    language    TEXT,
    language_confidence REAL,
    -- 'разобран' | 'частично' | 'нужно распознавание' | 'не поддерживается' | 'ошибка'
    status      TEXT NOT NULL,
    text        TEXT,                            -- нормализованный текст документа
    tables      TEXT,                            -- JSON: [{"name":..., "rows":[[...]]}]
    fields      TEXT,                            -- JSON: [{"key","name","value","method",...}]
    facts       TEXT,                            -- JSON: [{"key","value","source"}]
    confidence  REAL,
    method      TEXT,                            -- 'regex' | 'llm' | 'manual'
    created_at  TEXT NOT NULL,
    updated_at  TEXT
);

CREATE INDEX IF NOT EXISTS ix_document_extracts_request ON document_extracts(request_id, id);
CREATE INDEX IF NOT EXISTS ix_document_extracts_kind ON document_extracts(kind, id);
-- индексы частых запросов (задача 123, подобраны по EXPLAIN QUERY PLAN)
CREATE INDEX IF NOT EXISTS ix_document_extracts_photo ON document_extracts(photo_id); -- удаление фото (каскад)

-- ============ ОСГОР: КЛАССИФИКАЦИЯ ВИДОВ ДЕЯТЕЛЬНОСТИ ============
-- Классификация видов деятельности работодателя и коэффициенты страховых тарифов (КСТ).
-- Прил. № 9 к Правилам ПКМ № 177, разд. I, п. 3 (ред. ПКМ № 443 от 15.07.2025): 934 вида
-- деятельности, 20 категорий профессионального риска, КСТ от 0,571 до 7,714.
-- Вид деятельности вне перечня → КСТ 3,400 (разд. I, п. 6), отдельной строкой не хранится.
CREATE TABLE IF NOT EXISTS osgor_activities (
    no        INTEGER PRIMARY KEY,       -- № позиции в перечне, 1..934
    category  INTEGER NOT NULL,          -- категория профессионального риска, 1..20
    kst       REAL NOT NULL,             -- коэффициент страхового тарифа
    okved     TEXT,                      -- код ОКЭД (IFUT)
    name      TEXT NOT NULL,             -- наименование вида деятельности
    act_ref   TEXT NOT NULL DEFAULT 'ПКМ № 177, прил. № 9, разд. I, п. 3 (ред. ПКМ № 443 от 15.07.2025)'
);
CREATE INDEX IF NOT EXISTS ix_osgor_activities_okved ON osgor_activities(okved);

-- ============ ОСГОР: БАЗОВАЯ РАСЧЁТНАЯ ВЕЛИЧИНА (БРВ) ============
-- Размер БРВ вводит администратор (PUT /osgor/brv, app/osgor.py): минимум премии 0,25 БРВ
-- (п. 23 Правил ПКМ № 177), погребение до 3 БРВ (п. 43). Значение не засевается — пока админ
-- не ввёл, таблица пуста (вопрос 110 заказчику). Строки не правятся и не удаляются: исправление —
-- новая строка; действует строка с наибольшей effective_from ≤ даты расчёта, при равенстве — последняя
-- по id (правило 9 CLAUDE.md: старый расчёт воспроизводится по дате).
CREATE TABLE IF NOT EXISTS osgor_brv (
    id             INTEGER PRIMARY KEY,
    value          REAL NOT NULL,             -- сум
    effective_from TEXT NOT NULL,             -- ГГГГ-ММ-ДД, с какого дня действует
    source         TEXT NOT NULL,             -- акт (указ/постановление) или ссылка на него
    note           TEXT,
    entered_by     TEXT NOT NULL,             -- логин админа
    entered_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_osgor_brv_date ON osgor_brv(effective_from, id);

-- ============ АНАЛИТИКА РИСКА: ПОРОГИ УРОВНЯ РИСКА ============
-- app/risk_analytics.py (вкладка «Аналитика» мини-аппа). Правка порогов админом — новая строка,
-- старые остаются в истории (правило 9: расчёты должны воспроизводиться). Действует последняя по id.
-- В thresholds_json только отличия от DEFAULT_THRESHOLDS. Шкала экспертная, calibrated = 0.
CREATE TABLE IF NOT EXISTS risk_thresholds (
    id              INTEGER PRIMARY KEY,
    created_at      TEXT NOT NULL,
    created_by      TEXT,
    thresholds_json TEXT NOT NULL,
    calibrated      INTEGER NOT NULL DEFAULT 0,
    note            TEXT
);

-- ============ АНАЛИТИКА РИСКА: ПОСЛЕДНИЙ ВЫБОР ФОРМЫ ============
-- Быстрый режим (22.09.2026): форма запоминает выбор пользователя — класс, продукт, тип объекта,
-- регион, срок, конструкция, деятельность, тип ТС, защита, сейсмозона (ra.LAST_ALLOWED).
-- Ни страховых сумм, ни персональных данных здесь нет; читает и пишет только сам пользователь.
CREATE TABLE IF NOT EXISTS risk_last_input (
    user_id    INTEGER PRIMARY KEY,
    updated_at TEXT NOT NULL,
    data_json  TEXT NOT NULL
);

-- ============ АНАЛИТИКА РИСКА: ДОГОВОР ДЛЯ АНАЛИЗА ============
-- app/analysis_docs.py (задача 150): договор, загруженный во вкладке «Аналитика» без запроса.
-- id — случайный токен (не подбирается перебором), доступ только владельцу (user_id).
-- Файл — DATA_DIR/analysis/<id>/, живёт 24 часа (expires_at), затем удаляется вместе со строкой.
-- Полного текста документа и ПД здесь нет: в fields_json только замаскированные поля, факты,
-- подстановка в форму (prefill) и пояснения.
CREATE TABLE IF NOT EXISTS analysis_docs (
    id          TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    filename    TEXT,
    mime        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    kind        TEXT,
    language    TEXT,
    status      TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_analysis_docs_user ON analysis_docs(user_id, expires_at);

-- ============ ЮРИДИЧЕСКИЕ ОТВЕТЫ (app/legal.py) ============
-- Поисковый индекс по библиотеке законодательства, заметкам проекта и правилам движка.
-- FTS5: полнотекстовый поиск встроен в SQLite, отдельных пакетов не нужно. Токенайзер unicode61
-- с remove_diacritics 2 — узбекская латиница с диакритикой (gʻ, sh, oʻ) ищется как без неё;
-- апострофы (ʻ ‘ ’ ʼ ` ') модуль снимает и при индексации, и в запросе, поэтому «sugʻurta»,
-- «sug'urta» и «sugurta» дают один результат.
-- Индексируемые колонки: act, unit, title, text. Остальные — UNINDEXED (только фильтр и выдача).
-- Таблица пересобирается целиком (app/legal.reindex), поэтому DROP/CREATE здесь не нужен.
CREATE VIRTUAL TABLE IF NOT EXISTS legal_chunks USING fts5(
    act,                    -- «ГК РУз глава 52 Страхование»
    act_code UNINDEXED,     -- слаг акта: gk_ruz_glava_52
    language UNINDEXED,     -- ru | uz | en
    unit,                   -- «ст. 938» | «п. 4» | «modda 938»
    title,                  -- заголовок статьи/пункта
    text,                   -- текст для поиска: апострофы сняты
    raw UNINDEXED,          -- он же как есть — из него берётся цитата для человека
    url UNINDEXED,          -- ссылка на источник (lex.uz), если известна
    path UNINDEXED,         -- файл-источник относительно корня проекта
    official UNINDEXED,     -- 1 — официальный текст, 0 — перевод/заметка
    tokenize = 'unicode61 remove_diacritics 2'
);

-- Какие файлы уже в индексе: пересобираем только при изменении mtime/размера.
CREATE TABLE IF NOT EXISTS legal_files (
    path       TEXT PRIMARY KEY,
    mtime      REAL NOT NULL,
    size       INTEGER NOT NULL,
    language   TEXT,
    act        TEXT,
    act_code   TEXT,
    chunks     INTEGER NOT NULL DEFAULT 0,
    indexed_at TEXT NOT NULL
);

-- Журнал вопросов: САМ ТЕКСТ ВОПРОСА НЕ ХРАНИМ (в нём могут быть персональные данные) —
-- только отпечаток, чтобы видеть частые вопросы без ответа и пополнять ими FAQ.
CREATE TABLE IF NOT EXISTS legal_questions (
    id         INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    lang       TEXT NOT NULL,
    q_hash     TEXT NOT NULL,
    source     TEXT NOT NULL,          -- faq | passages | none
    confidence REAL,
    took_ms    INTEGER,
    found      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_legal_questions_hash ON legal_questions(q_hash);

-- Кэш живого поиска «ИИ специалиста» на lex.uz (app/legal_live.py): выдача поиска и страницы
-- актов на 24 часа, чтобы повторный вопрос не шёл на сайт. body — страница, сжатая zlib.
-- Адрес выдачи поиска НЕ хранится (в нём слова вопроса) — только отпечаток в key.
CREATE TABLE IF NOT EXISTS lex_live_cache (
    key        TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,          -- search | act
    url        TEXT,                   -- только для страниц актов
    created_at REAL NOT NULL,          -- время записи, секунды эпохи
    body       BLOB
);

-- ---------------------------------------------------------------------------
-- Способы входа в один профиль (задача заказчика 23.09.2026, «как на daykon.uz»).
-- Профиль (users) один, а войти в него можно любым привязанным способом: Telegram или Google.
-- Столбцы users.telegram_id / users.google_sub остаются зеркалом последней привязки — на них
-- опирается старый код входа; список способов ведётся здесь.
-- external_id: telegram_id для Telegram, sub аккаунта для Google (почту человек может сменить,
-- sub — нет). display — что показать человеку: «@username» или почта; это не ключ входа.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS login_links (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider      TEXT NOT NULL,             -- 'telegram' | 'google'
    external_id   TEXT NOT NULL,             -- telegram_id | google sub
    display       TEXT,                      -- '@username' | почта — только для показа
    linked_at     TEXT NOT NULL,
    last_login_at TEXT
);
-- один аккаунт провайдера — не больше чем в одном профиле (иначе «второй способ входа»
-- уводил бы в чужой профиль); и не больше одного аккаунта каждого провайдера на профиль
CREATE UNIQUE INDEX IF NOT EXISTS ux_login_links_external ON login_links(provider, external_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_login_links_user ON login_links(user_id, provider);

-- ---------------------------------------------------------------------------
-- Запросы доступа в админку (уточнение заказчика 23.09.2026: «только мне придёт запрос,
-- и я подтверждаю»). Сотрудник нажимает кнопку — владелец подтверждает в Telegram или в
-- разделе «Пользователи». Автоматически админом не становится никто.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS admin_requests (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status     TEXT NOT NULL DEFAULT 'ожидает',   -- 'ожидает' | 'подтверждён' | 'отклонён'
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,                               -- логин владельца, принявшего решение
    note       TEXT
);
-- второй раз «ожидает» на того же человека не создаётся (повторный запрос не дублирует)
CREATE UNIQUE INDEX IF NOT EXISTS ux_admin_requests_pending ON admin_requests(user_id)
    WHERE status = 'ожидает';
CREATE INDEX IF NOT EXISTS ix_admin_requests_status ON admin_requests(status);

-- ---------------------------------------------------------------------------
-- Сюрвейерский акт, лёгкая версия (ТЗ 2.0 от 29.09.2026; app/act.py, app/act_engine.py).
-- Те же определения создаются при каждом обращении к /act/* (act.ensure_tables) — сервер
-- с уже существующей базой работает без tools/db_build.py.
-- ---------------------------------------------------------------------------
-- пороги лёгкого движка: уровень риска, поправки 0/20/50 %, износ; новая правка — новая строка
CREATE TABLE IF NOT EXISTS act_settings (
    id            INTEGER PRIMARY KEY,
    created_at    TEXT NOT NULL,
    created_by    TEXT,
    settings_json TEXT NOT NULL,
    calibrated    INTEGER NOT NULL DEFAULT 0,   -- 0 = экспертные значения
    note          TEXT
);
-- загрузка фото и снимков документов: живёт 24 часа, файлы — DATA_DIR/act/<id>/.
-- result_json — ракурсы, распознанные поля объекта и повреждения; ПД сюда не попадают
CREATE TABLE IF NOT EXISTS act_uploads (
    id           TEXT PRIMARY KEY,               -- случайный токен
    owner_key    TEXT NOT NULL,                  -- 'u:<id>' | 'g:<guest_id>' (app/guest.py)
    user_id      INTEGER NOT NULL DEFAULT 0,
    files_json   TEXT NOT NULL DEFAULT '[]',
    result_json  TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);
-- акт: живёт 7 дней; act_json — структурированные данные, текст собирается по языку при выдаче
CREATE TABLE IF NOT EXISTS acts (
    id                TEXT PRIMARY KEY,          -- 16 случайных hex-знаков
    owner_key         TEXT NOT NULL,
    user_id           INTEGER NOT NULL DEFAULT 0,
    lang              TEXT NOT NULL DEFAULT 'ru',
    tariff_version_id INTEGER,                   -- версия тарифа, по которой считали
    settings_id       INTEGER,                   -- версия act_settings (NULL = по умолчанию)
    act_json          TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    expires_at        TEXT NOT NULL
);
-- шаблоны анализа по классам (ТЗ — Приложение А; app/class_templates.py): одна строка на версию шаблона класса,
-- действует наибольшая версия. Источник — docs/act_class_templates.json (заполняет tools/db_build.py, на сервере
-- доводит class_templates.ensure при старте); правка администратора — новая строка (source = 'admin'), история
-- сохраняется. Доли рисков и параметры — экспертные, calibrated = 0.
CREATE TABLE IF NOT EXISTS class_templates (
    id           INTEGER PRIMARY KEY,
    class_code   TEXT NOT NULL,                  -- 1..17 (13з → 14, 16у → 16 — по aliases файла)
    version      TEXT NOT NULL,                  -- «1.0» из файла, правки администратора — 1.1, 1.2, …
    json         TEXT NOT NULL,                  -- шаблон класса (8 полей приложения А + то, что нужно акту)
    source       TEXT NOT NULL DEFAULT 'file',   -- 'file' | 'admin'
    file_version TEXT,                           -- версия файла, от которой строка произошла
    updated_at   TEXT NOT NULL,
    updated_by   TEXT,
    calibrated   INTEGER NOT NULL DEFAULT 0,
    note         TEXT,
    UNIQUE (class_code, version)
);
CREATE INDEX IF NOT EXISTS idx_class_templates_class ON class_templates (class_code, id);
CREATE INDEX IF NOT EXISTS idx_act_uploads_expires ON act_uploads (expires_at);
CREATE INDEX IF NOT EXISTS idx_acts_expires ON acts (expires_at);
CREATE INDEX IF NOT EXISTS idx_acts_owner ON acts (owner_key, created_at);

-- ---------------------------------------------------------------------------
-- Биржевые цены УзРТСБ (uzex.uz): сделки и котировки сырья, материалов, топлива (02.10.2026).
-- Загрузка — app/uzex_sources.py, выдача и расписание — app/uzex.py, справка в акте — act_analytics.exchange_background.
-- Ключ: страница + номер контракта + день загрузки. У реестра сделок (/Trade/List) один контракт даёт много сделок
-- за день, поэтому вместо голого номера — deal_key = номер|дата|количество|сумма|порядковый номер одинаковых
-- сделок; у котировочного листа deal_key = номер контракта. История не затирается: каждая загрузка — свой день.
-- price_lot — как у источника (реестр: сумма сделки за всё количество; лист: цена лота «Bazis narxi»),
-- price_unit = price_lot / lot_qty, price_unit_norm — за приведённую единицу (кг → т, декалитр → л).
-- trade_date NULL = у страницы нет даты сделки, котировка «на дату загрузки» (date_basis). Людей в таблице нет:
-- продавцы в названиях лотов — юридические лица.
CREATE TABLE IF NOT EXISTS exchange_quotes (
    id              INTEGER PRIMARY KEY,
    page            TEXT NOT NULL,          -- 'List' | 'ContractsSumNew' | 'ContractsCurrencyNew'
    contract_no     TEXT,                   -- номер контракта биржи (Shartnoma №)
    deal_key        TEXT NOT NULL,
    name            TEXT,                   -- товар как у биржи
    grp             TEXT NOT NULL,          -- группа товара (uzex_sources.GROUPS)
    lot_qty         REAL,                   -- количество сделки / размер лота
    unit            TEXT,                   -- единица у биржи: тонна, килограмм, литр …
    unit_norm       TEXT,                   -- приведённая: т, л, г, м², м³, шт
    price_raw       REAL,                   -- число как на странице
    price_lot       REAL,
    price_unit      REAL,
    price_unit_norm REAL,
    price_basis     TEXT,                   -- что значит цена (проверено сверкой страниц)
    currency        TEXT,
    warehouse       TEXT,                   -- место склада (лист котировок)
    trade_date      TEXT,                   -- дата сделки ISO; NULL — даты у источника нет
    date_basis      TEXT,                   -- 'дата сделки' | 'на дату загрузки'
    contract_type   TEXT,                   -- внутренний | экспортный | импортный
    deal_status     TEXT,
    fetched_at      TEXT NOT NULL,
    fetched_date    TEXT NOT NULL,
    url             TEXT NOT NULL,          -- страница источника (обязательна)
    UNIQUE (page, deal_key, fetched_date)
);
CREATE INDEX IF NOT EXISTS ix_exchange_grp_date ON exchange_quotes(grp, trade_date);
CREATE INDEX IF NOT EXISTS ix_exchange_page_key ON exchange_quotes(page, deal_key);

-- Рэнкинг страховых компаний snsratings.uz (по данным openinfo.uz и napp.uz): tools/ranking_parse.py.
-- Один показатель одной компании за период; строка итога — company='ВСЕГО'. Суммы — в единице unit
-- (млн сум, млрд сум, кол-во, %, сум). change_pct и growth_impact_pp — как в документе («Изменение, %» и
-- «Влияние на рост рынка, п.п.»); change_pp — изменение доли (share_cur − share_prev), для относительных
-- показателей в % — value_cur − value_prev, п.п.
CREATE TABLE IF NOT EXISTS company_rankings (
    report_period    TEXT NOT NULL,          -- '2026-Q2'
    indicator_code   TEXT NOT NULL,          -- total_assets, total_capital, reserves_net, roe, claims_refused …
    indicator_name   TEXT NOT NULL,          -- по-русски, как в содержании документа
    section          TEXT,                   -- суммарные | количественные | относительные
    company          TEXT NOT NULL,          -- как ключ market_stats без префикса: 'INSON AJ'; итог — 'ВСЕГО'
    company_raw      TEXT,                   -- как в документе
    is_life          INTEGER NOT NULL DEFAULT 0,
    value_prev       REAL,
    share_prev       REAL,
    rank_prev        INTEGER,
    value_cur        REAL,
    share_cur        REAL,
    rank_cur         INTEGER,
    change_pct       REAL,
    change_pp        REAL,
    growth_impact_pp REAL,
    unit             TEXT,
    page             INTEGER,
    source_file      TEXT,
    source_sha256    TEXT,                   -- отпечаток текста: изменился файл — сервер перегружает при старте
    loaded_at        TEXT,
    PRIMARY KEY (report_period, indicator_code, company)
);
CREATE INDEX IF NOT EXISTS ix_company_rankings_company ON company_rankings(company, report_period);

-- ---------------------------------------------------------------------------
-- Части продуктов (ТЗ разработчику 13.10.2026, день 2.1; app/product_parts.py). Строка — часть продукта в своём
-- классе для типа клиента (fiz | yur | both): своя базовая и минимальная ставка, тип ставки, база суммы, доля, лимит,
-- франшиза. Версия — как у tariff_versions: valid_from + source + created_at; новая правка — новая версия тарифа
-- (tariff_versions, уровень «компания») и строка product_part_versions, старые не меняются (правило проекта № 9).
-- Строки тарифной политики (source = policy, tools/product_parts_migrate.py) ссылаются на версию политики.
-- «limit» и «order» — служебные слова SQL, поэтому колонки limit_amount и sort_order.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS product_part_versions (
    tariff_version_id INTEGER PRIMARY KEY REFERENCES tariff_versions(id),
    product_code      TEXT NOT NULL,
    valid_from        TEXT NOT NULL,
    source            TEXT NOT NULL,                    -- admin | import | tz_example
    created_at        TEXT NOT NULL,
    created_by        TEXT,
    note              TEXT
);
CREATE TABLE IF NOT EXISTS product_parts (
    id                INTEGER PRIMARY KEY,
    tariff_version_id INTEGER NOT NULL REFERENCES tariff_versions(id),
    product_code      TEXT NOT NULL REFERENCES products(code),
    part_no           INTEGER NOT NULL,
    class_code        TEXT NOT NULL REFERENCES classes(code),
    perils            TEXT NOT NULL DEFAULT '[]',       -- JSON: коды рисков части
    client_type       TEXT NOT NULL DEFAULT 'both',     -- fiz | yur | both
    base_rate_pct     REAL,                             -- NULL — ставки части нет, расчёт прежним путём
    min_rate_pct      REAL,
    rate_type         TEXT NOT NULL DEFAULT 'annual',   -- annual | fixed
    sum_basis         TEXT NOT NULL DEFAULT 'value',    -- value | limit | share
    default_share     REAL,                             -- доля страховой суммы договора, 0..1; NULL — не задана
    limit_amount      REAL,                             -- лимит части, сум
    deductible        REAL,                             -- франшиза части, % страховой суммы
    mandatory         INTEGER NOT NULL DEFAULT 1,
    sort_order        INTEGER NOT NULL DEFAULT 1,
    valid_from        TEXT NOT NULL,
    rules_ref         TEXT,
    source            TEXT NOT NULL,                    -- policy | tz_example | admin | import
    created_at        TEXT NOT NULL,
    created_by        TEXT,
    note              TEXT,
    UNIQUE (tariff_version_id, product_code, part_no, client_type)
);
CREATE INDEX IF NOT EXISTS ix_product_parts_product ON product_parts(product_code, valid_from);

-- ---------------------------------------------------------------------------
-- Актуарный отчёт по субпортфелям (ТЗ 13.10.2026, день 2.3; app/actuarial.py, загрузка — tools/actuarial_load.py).
-- Суммы — млн сум, брутто. load_L — доля расходов на ведение дела в заработанной премии (0..1).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS actuarial_reports (
    year            INTEGER NOT NULL,
    subportfolio    TEXT NOT NULL,          -- vehicle | aviation | property | liability | financial | osgovts | osgor | other
    name            TEXT NOT NULL,          -- как в отчёте
    premium_written REAL,
    premium_earned  REAL NOT NULL,
    payouts         REAL NOT NULL DEFAULT 0,
    expenses        REAL,                   -- расходы на ведение дела
    expenses_pct    REAL,                   -- к заработанной брутто-премии, %
    commission_pct  REAL,                   -- агентская комиссия, % премии
    risk_margin     REAL,                   -- рисковая надбавка в составе нетто-премии; NULL — не оценивалась
    load_L          REAL,
    unit            TEXT NOT NULL DEFAULT 'млн сум',
    pages           TEXT,
    source          TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    updated_by      TEXT,
    PRIMARY KEY (year, subportfolio)
);
