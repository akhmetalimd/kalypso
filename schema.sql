-- ============================================================================
--  KALYPSO (Kazakhstan Lymphoma Patients Survival and Outcomes)
--  Схема БД регистра ЛПЗ (PostgreSQL)
--  Реализует трёхуровневую модель: пациент -> линия терапии -> наблюдение,
--  обязательное ядро (словарь v1.0), коды пропусков, провенанс, аудит,
--  нозолого-специфичные поля (EAV) и лабораторные показатели с канонизацией.
--
--  Портируемость: типы держим стандартными. Для SQLite замените JSONB->TEXT,
--  ENUM->TEXT+CHECK (уже так и сделано), gen_random_uuid()->свой генератор.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- для gen_random_uuid()

-- Общие домены -----------------------------------------------------------------
-- Точность даты хранится рядом с датой; недостающая часть подставляется
-- серединой периода ТОЛЬКО при расчёте, не в записи.
CREATE TYPE date_precision AS ENUM ('day', 'month', 'year');
CREATE TYPE provenance     AS ENUM ('retrospective', 'prospective');
-- Коды пропусков — вместо пустой ячейки для «условных» полей.
CREATE TYPE missing_code   AS ENUM ('not_done', 'done_unavailable',
                                    'normal_negative', 'unknown');

-- ============================================================================
--  Уровень 1: ПАЦИЕНТ (обязательное ядро)
-- ============================================================================
CREATE TABLE patient (
    -- Идентификация. patient_id = HMAC-SHA256(ИИН, секрет проекта)[:12].
    -- Сам ИИН в базу не пишется. Одинаковый ИИН -> один id -> автодедупликация.
    patient_id           TEXT PRIMARY KEY,
    branch               TEXT        NOT NULL,             -- контролируемый список филиалов
    sex                  TEXT        NOT NULL CHECK (sex IN ('М','Ж','не указано')),
    birth_date           DATE,
    birth_date_precision date_precision DEFAULT 'day',

    -- Диагноз и кодирование (WHO-HAEM5 -> ICD-O-3.2 -> МКБ).
    who_haem5            TEXT        NOT NULL,             -- id узла дерева нозологий
    icd_o_3_2            TEXT        NOT NULL,             -- морфология M-XXXX/3
    mkb                  TEXT        NOT NULL,             -- код МКБ-10 (готов к МКБ-11)
    dx_version           INT         NOT NULL DEFAULT 1,   -- версия диагноза (пересмотр не затирает)

    -- Даты и провенанс.
    dx_date              DATE        NOT NULL,             -- index date / time-zero
    dx_date_precision    date_precision NOT NULL DEFAULT 'day',
    first_visit_date     DATE,
    first_visit_precision date_precision DEFAULT 'day',
    last_contact_date    DATE        NOT NULL,
    last_contact_precision date_precision NOT NULL DEFAULT 'day',
    entry_date           TIMESTAMPTZ NOT NULL DEFAULT now(),
    provenance           provenance  NOT NULL,

    -- Клинико-прогностическое ядро.
    ecog                 INT         CHECK (ecog BETWEEN 0 AND 4),
    cirs_score           INT,                              -- CIRS-G, каноническая шкала
    cirs_fitness         TEXT        CHECK (cirs_fitness IN ('fit','unfit','frail')),
    charlson_score       INT,                              -- опционально
    b_symptoms           TEXT        CHECK (b_symptoms IN ('да','нет','неизвестно')),
    stage_value          TEXT,
    stage_system         TEXT        CHECK (stage_system IN ('Rai','Binet','Lugano','Ann Arbor')),
    -- Первичные документы: гистология (диагностическая патоморфология).
    histology_report     TEXT,                              -- текст заключения (структурированное поле, вход для AI)
    histology_scan       TEXT,                              -- ссылка/путь на скан-вложение (не WSI)

    index_name           TEXT,
    index_score          NUMERIC,
    index_group          TEXT,
    index_components      JSONB,                           -- компоненты индекса (пересчитываемы)

    -- Статус на последний контакт.
    status_last_contact  TEXT        NOT NULL
        CHECK (status_last_contact IN ('жив','умер','потерян из наблюдения'))
);

-- Коды пропусков для «условных» полей ядра (не пишем NULL молча).
CREATE TABLE patient_missing (
    patient_id  TEXT NOT NULL REFERENCES patient(patient_id) ON DELETE CASCADE,
    field       TEXT NOT NULL,          -- имя поля из словаря
    code        missing_code NOT NULL,
    PRIMARY KEY (patient_id, field)
);

-- ============================================================================
--  Уровень 1a: нозолого-специфичные поля (EAV -> расширяемость без ALTER TABLE)
--  Добавить нозологию = писать её маркеры сюда, а не менять схему.
-- ============================================================================
CREATE TABLE module_data (
    patient_id  TEXT NOT NULL REFERENCES patient(patient_id) ON DELETE CASCADE,
    module      TEXT NOT NULL,          -- 'cll', 'dlbcl', ...
    field_key   TEXT NOT NULL,          -- 'IGHV', 'TP53', ...
    value       TEXT,
    PRIMARY KEY (patient_id, module, field_key)
);

-- ============================================================================
--  Уровень 2: ЛИНИЯ ТЕРАПИИ (0..N на пациента)
-- ============================================================================
CREATE TABLE therapy_line (
    line_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id    TEXT NOT NULL REFERENCES patient(patient_id) ON DELETE CASCADE,
    line_no       INT  NOT NULL,
    regimen       TEXT,
    drug          TEXT,
    dose          TEXT,
    cycles        INT,
    route         TEXT,
    start_date    DATE,
    start_precision date_precision DEFAULT 'day',
    end_date      DATE,
    end_precision date_precision DEFAULT 'day',
    duration_days INT,                  -- time-on-treatment (драйвер затрат)
    stop_reason   TEXT,
    UNIQUE (patient_id, line_no)
);

-- ============================================================================
--  Уровень 3: НАБЛЮДЕНИЕ (ответ / статус / событие; 0..N)
--  Гранулярность события позволяет собрать PFS / modified PFS / EFS / TTNT.
-- ============================================================================
CREATE TABLE observation (
    obs_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      TEXT NOT NULL REFERENCES patient(patient_id) ON DELETE CASCADE,
    obs_date        DATE NOT NULL,
    obs_precision   date_precision NOT NULL DEFAULT 'day',
    response_value    TEXT,
    response_criteria TEXT CHECK (response_criteria IN ('iwCLL 2018','Lugano 2014')),
    response_basis    TEXT CHECK (response_basis IN ('клинически','КТ','ПЭТ-КТ','морфология')),
    event_type      TEXT CHECK (event_type IN
                        ('нет события','прогрессия','рецидив','смерть','старт новой линии')),
    event_date      DATE,
    death_cause     TEXT CHECK (death_cause IN ('связана с ЛПЗ','не связана','неизвестно')),
    -- ПЭТ-КТ: заключение + Deauville + ссылка на исследование в PACS (DICOM не хранить).
    pet_ct_report   TEXT,                   -- текст заключения ПЭТ-КТ
    deauville       INT CHECK (deauville BETWEEN 1 AND 5),  -- шкала Deauville (ЛХ/DLBCL)
    pacs_accession  TEXT                    -- accession number / Study UID для открытия в PACS
);

-- ============================================================================
--  Уровень 3a: ЛАБОРАТОРНЫЕ ПОКАЗАТЕЛИ (динамика + канонизация единиц)
--  Хранятся и исходное (value_raw/unit_raw), и каноническое значение.
-- ============================================================================
CREATE TABLE lab (
    lab_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      TEXT NOT NULL REFERENCES patient(patient_id) ON DELETE CASCADE,
    obs_date        DATE NOT NULL,
    loinc           TEXT NOT NULL,      -- код показателя
    value_raw       NUMERIC NOT NULL,
    unit_raw        TEXT NOT NULL,      -- UCUM, как введено
    value_canonical NUMERIC,           -- приведено (NULL если единица не распознана)
    unit_canonical  TEXT,
    flag            TEXT                -- 'unknown_unit' / 'unknown_loinc' -> ручная проверка
);
CREATE INDEX lab_patient_loinc_idx ON lab (patient_id, loinc, obs_date);

-- ============================================================================
--  Аудит-трейл (кто/когда/что менял) — для провенанса и целостности науки
-- ============================================================================
CREATE TABLE audit_log (
    audit_id    BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor       TEXT NOT NULL,
    table_name  TEXT NOT NULL,
    row_id      TEXT NOT NULL,
    action      TEXT NOT NULL CHECK (action IN ('insert','update','delete')),
    field       TEXT,
    old_value   TEXT,
    new_value   TEXT
);

-- ============================================================================
--  Представление для «Исследователя выживаемости»: пациент + время + событие.
--  time-zero = дата диагноза; событие = первое прогрессирование/рецидив/смерть.
-- ============================================================================
CREATE VIEW v_survival AS
SELECT
    p.patient_id,
    p.who_haem5,
    p.branch,
    p.dx_date AS time_zero,
    COALESCE(
        (SELECT MIN(o.event_date) FROM observation o
          WHERE o.patient_id = p.patient_id
            AND o.event_type IN ('прогрессия','рецидив','смерть')),
        p.last_contact_date
    ) AS event_or_censor_date,
    CASE WHEN EXISTS (
        SELECT 1 FROM observation o
         WHERE o.patient_id = p.patient_id
           AND o.event_type IN ('прогрессия','рецидив','смерть')
    ) THEN 1 ELSE 0 END AS event
FROM patient p;
