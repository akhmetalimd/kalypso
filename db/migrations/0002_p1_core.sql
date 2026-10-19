-- ============================================================================
--  KALYPSO — миграция 0002: ядро этапа П1
--  Применяется после schema.sql (baseline = 0001):  psql -1 -f 0002_p1_core.sql
--  Требует PostgreSQL 15+ (NULLS NOT DISTINCT) и прав на CREATE ROLE.
--
--  Закрывает замечания аудита:
--    B1 версионность диагноза        B5 аудит на триггерах, неизменяемый журнал
--    B2 наблюдение -> линия терапии  B6 пользователи, роли, филиалы, журнал входов
--    B3 источник + ретро/проспектив  B7 RESTRICT + мягкое удаление
--       на каждой записи             I1 суррогатный PK + ИИН (уникальный)
--    B4 коды пропусков в module_data I3 запрет 12-значных чисел в свободном тексте
--    I4 коды вместо русских значений I5 согласованность смерти
--    I6 ограничения дат и умных полей I7 маркеры с датой теста
--    I8 служебные поля, source_record_id
--
--  Конвенции для приложения:
--    SET LOCAL app.user_id = '<uuid app_user>';    -- в каждой пишущей транзакции
--    SET LOCAL app.change_reason = '<текст>';      -- обязательно при пересмотре диагноза
-- ============================================================================

-- Миграция перестраивает ключи и коды, поэтому выполняется только на пустой БД.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM patient) OR EXISTS (SELECT 1 FROM therapy_line)
       OR EXISTS (SELECT 1 FROM observation) OR EXISTS (SELECT 1 FROM lab)
       OR EXISTS (SELECT 1 FROM module_data) OR EXISTS (SELECT 1 FROM patient_missing) THEN
        RAISE EXCEPTION '0002: в клинических таблицах есть данные — миграция рассчитана на пустую БД';
    END IF;
END $$;

DROP VIEW v_survival;  -- пересоздаётся в конце под новые ключи и коды

-- ============================================================================
--  Общие типы и функции
-- ============================================================================
-- Источник записи (провенанс). Ретро/проспектив остаётся в типе provenance.
-- Сейчас — только ручной ввод; источники AI и МИС добавятся через ALTER TYPE … ADD VALUE.
CREATE TYPE record_source AS ENUM ('manual');

-- Текущий пользователь приложения (NULL, если правка идёт мимо приложения).
CREATE FUNCTION app_user_id() RETURNS UUID
LANGUAGE sql STABLE AS $$
    SELECT nullif(current_setting('app.user_id', true), '')::uuid
$$;

-- Неизменяемые таблицы: только INSERT.
CREATE FUNCTION forbid_modification() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Таблица % неизменяема: % запрещён', TG_TABLE_NAME, TG_OP;
END $$;

-- ============================================================================
--  B6. Филиалы, роли, пользователи, журнал входов
-- ============================================================================
CREATE TABLE branch (
    branch_code TEXT PRIMARY KEY CHECK (branch_code ~ '^[a-z0-9_]{2,32}$'),
    name_ru     TEXT NOT NULL,
    name_kk     TEXT,
    is_active   BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE role (
    role_code TEXT PRIMARY KEY,
    name_ru   TEXT NOT NULL,
    name_kk   TEXT
);
INSERT INTO role (role_code, name_ru) VALUES
    ('physician', 'Лечащий врач'),
    ('curator',   'Куратор регистра'),
    ('analyst',   'Аналитик / исследователь'),
    ('admin',     'Администратор');

CREATE TABLE app_user (
    user_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    login                TEXT NOT NULL UNIQUE CHECK (login ~ '^[a-z0-9._-]{3,64}$'),
    display_name         TEXT NOT NULL,
    password_hash        TEXT NOT NULL,          -- argon2; в аудит не попадает
    role_code            TEXT NOT NULL REFERENCES role (role_code) ON DELETE RESTRICT,
    branch_code          TEXT REFERENCES branch (branch_code) ON DELETE RESTRICT,
    is_active            BOOLEAN NOT NULL DEFAULT true,
    must_change_password BOOLEAN NOT NULL DEFAULT true,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by           UUID REFERENCES app_user (user_id) DEFAULT app_user_id(),
    last_login_at        TIMESTAMPTZ,
    -- Врач работает в своём филиале, поэтому филиал обязателен.
    CONSTRAINT app_user_physician_branch CHECK (role_code <> 'physician' OR branch_code IS NOT NULL)
);

CREATE TABLE login_log (
    login_id    BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    event       TEXT NOT NULL CHECK (event IN ('login', 'logout')),
    login       TEXT NOT NULL CHECK (length(login) <= 64),   -- как введено
    user_id     UUID,                                        -- без FK: журнал независим
    success     BOOLEAN NOT NULL,
    reason      TEXT NOT NULL CHECK (reason IN
                    ('ok', 'bad_password', 'unknown_user', 'inactive', 'locked')),
    ip          INET,
    user_agent  TEXT
);
CREATE TRIGGER login_log_immutable BEFORE UPDATE OR DELETE ON login_log
    FOR EACH ROW EXECUTE FUNCTION forbid_modification();
CREATE TRIGGER login_log_no_truncate BEFORE TRUNCATE ON login_log
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_modification();

-- ============================================================================
--  I1. Суррогатный PK пациента + ИИН (12 цифр, уникальный — дедупликация между филиалами)
-- ============================================================================
ALTER TABLE patient_missing DROP CONSTRAINT patient_missing_patient_id_fkey;
ALTER TABLE module_data     DROP CONSTRAINT module_data_patient_id_fkey;
ALTER TABLE therapy_line    DROP CONSTRAINT therapy_line_patient_id_fkey;
ALTER TABLE observation     DROP CONSTRAINT observation_patient_id_fkey;
ALTER TABLE lab             DROP CONSTRAINT lab_patient_id_fkey;

ALTER TABLE patient DROP CONSTRAINT patient_pkey;
ALTER TABLE patient RENAME COLUMN patient_id TO iin;
COMMENT ON COLUMN patient.iin IS
    'ИИН пациента (12 цифр; контрольная цифра проверяется приложением). Хранится открыто — '
    'на раннем этапе без шифрования (решение владельца проекта). В интерфейсе — только врачу '
    'филиала и куратору; в выгрузку, аналитику и аудит не попадает.';
ALTER TABLE patient
    ADD CONSTRAINT patient_iin_format CHECK (iin ~ '^[0-9]{12}$'),
    ADD CONSTRAINT patient_iin_key UNIQUE (iin),
    ADD COLUMN patient_id UUID NOT NULL DEFAULT gen_random_uuid(),
    ADD CONSTRAINT patient_pkey PRIMARY KEY (patient_id);

ALTER TABLE patient_missing ALTER COLUMN patient_id TYPE UUID USING patient_id::uuid;
ALTER TABLE module_data     ALTER COLUMN patient_id TYPE UUID USING patient_id::uuid;
ALTER TABLE therapy_line    ALTER COLUMN patient_id TYPE UUID USING patient_id::uuid;
ALTER TABLE observation     ALTER COLUMN patient_id TYPE UUID USING patient_id::uuid;
ALTER TABLE lab             ALTER COLUMN patient_id TYPE UUID USING patient_id::uuid;

-- B7: RESTRICT вместо CASCADE — удаление пациента не стирает его записи.
ALTER TABLE patient_missing ADD CONSTRAINT patient_missing_patient_id_fkey
    FOREIGN KEY (patient_id) REFERENCES patient (patient_id) ON DELETE RESTRICT;
ALTER TABLE module_data ADD CONSTRAINT module_data_patient_id_fkey
    FOREIGN KEY (patient_id) REFERENCES patient (patient_id) ON DELETE RESTRICT;
ALTER TABLE therapy_line ADD CONSTRAINT therapy_line_patient_id_fkey
    FOREIGN KEY (patient_id) REFERENCES patient (patient_id) ON DELETE RESTRICT;
ALTER TABLE observation ADD CONSTRAINT observation_patient_id_fkey
    FOREIGN KEY (patient_id) REFERENCES patient (patient_id) ON DELETE RESTRICT;
ALTER TABLE lab ADD CONSTRAINT lab_patient_id_fkey
    FOREIGN KEY (patient_id) REFERENCES patient (patient_id) ON DELETE RESTRICT;

-- Филиал — из контролируемого списка (B6). patient.branch = филиал первичной регистрации.
ALTER TABLE patient ADD CONSTRAINT patient_branch_fkey
    FOREIGN KEY (branch) REFERENCES branch (branch_code) ON DELETE RESTRICT;

-- Межфилиальное наблюдение: один пациент (один ИИН) может вестись в нескольких
-- филиалах. Доступ врача к пациенту определяется этой таблицей.
CREATE TABLE patient_branch (
    patient_id  UUID NOT NULL REFERENCES patient (patient_id) ON DELETE RESTRICT,
    branch_code TEXT NOT NULL REFERENCES branch (branch_code) ON DELETE RESTRICT,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    added_by    UUID REFERENCES app_user (user_id) DEFAULT app_user_id(),
    PRIMARY KEY (patient_id, branch_code)
);

-- ============================================================================
--  I4. Коды вместо русских значений (подписи — в слое локализации)
-- ============================================================================
ALTER TABLE patient DROP CONSTRAINT patient_sex_check;
ALTER TABLE patient ADD CONSTRAINT patient_sex_check
    CHECK (sex IN ('M', 'F', 'unknown'));
ALTER TABLE patient DROP CONSTRAINT patient_b_symptoms_check;
ALTER TABLE patient ADD CONSTRAINT patient_b_symptoms_check
    CHECK (b_symptoms IN ('yes', 'no', 'unknown'));
ALTER TABLE patient DROP CONSTRAINT patient_status_last_contact_check;
ALTER TABLE patient ADD CONSTRAINT patient_status_last_contact_check
    CHECK (status_last_contact IN ('alive', 'dead', 'lost_to_follow_up'));

ALTER TABLE observation DROP CONSTRAINT observation_response_basis_check;
ALTER TABLE observation ADD CONSTRAINT observation_response_basis_check
    CHECK (response_basis IN ('clinical', 'ct', 'pet_ct', 'morphology'));
ALTER TABLE observation DROP CONSTRAINT observation_event_type_check;
ALTER TABLE observation ADD CONSTRAINT observation_event_type_check
    CHECK (event_type IN ('none', 'progression', 'relapse', 'death', 'new_line_start'));
ALTER TABLE observation ALTER COLUMN event_type SET DEFAULT 'none';
ALTER TABLE observation ALTER COLUMN event_type SET NOT NULL;
ALTER TABLE observation DROP CONSTRAINT observation_death_cause_check;
ALTER TABLE observation ADD CONSTRAINT observation_death_cause_check
    CHECK (death_cause IN ('lpd_related', 'not_related', 'unknown'));

-- ============================================================================
--  B3 + I8. Провенанс и служебные поля на каждой записи
-- ============================================================================
ALTER TABLE patient
    ADD COLUMN source           record_source NOT NULL,
    ADD COLUMN source_record_id TEXT,
    ADD COLUMN created_by       UUID REFERENCES app_user (user_id) DEFAULT app_user_id(),
    ADD COLUMN updated_at       TIMESTAMPTZ,
    ADD COLUMN updated_by       UUID REFERENCES app_user (user_id),
    ADD COLUMN deleted_at       TIMESTAMPTZ,
    ADD COLUMN deleted_by       UUID REFERENCES app_user (user_id);

ALTER TABLE therapy_line
    ADD COLUMN source           record_source NOT NULL,
    ADD COLUMN provenance       provenance NOT NULL,
    ADD COLUMN source_record_id TEXT,
    ADD COLUMN created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN created_by       UUID REFERENCES app_user (user_id) DEFAULT app_user_id(),
    ADD COLUMN updated_at       TIMESTAMPTZ,
    ADD COLUMN updated_by       UUID REFERENCES app_user (user_id),
    ADD COLUMN deleted_at       TIMESTAMPTZ,
    ADD COLUMN deleted_by       UUID REFERENCES app_user (user_id);

ALTER TABLE observation
    ADD COLUMN source           record_source NOT NULL,
    ADD COLUMN provenance       provenance NOT NULL,
    ADD COLUMN source_record_id TEXT,
    ADD COLUMN created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN created_by       UUID REFERENCES app_user (user_id) DEFAULT app_user_id(),
    ADD COLUMN updated_at       TIMESTAMPTZ,
    ADD COLUMN updated_by       UUID REFERENCES app_user (user_id),
    ADD COLUMN deleted_at       TIMESTAMPTZ,
    ADD COLUMN deleted_by       UUID REFERENCES app_user (user_id);

ALTER TABLE lab
    ADD COLUMN source           record_source NOT NULL,
    ADD COLUMN provenance       provenance NOT NULL,
    ADD COLUMN source_record_id TEXT,
    ADD COLUMN created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN created_by       UUID REFERENCES app_user (user_id) DEFAULT app_user_id(),
    ADD COLUMN updated_at       TIMESTAMPTZ,
    ADD COLUMN updated_by       UUID REFERENCES app_user (user_id),
    ADD COLUMN deleted_at       TIMESTAMPTZ,
    ADD COLUMN deleted_by       UUID REFERENCES app_user (user_id);

ALTER TABLE patient_missing
    ADD COLUMN source     record_source NOT NULL,
    ADD COLUMN created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN created_by UUID REFERENCES app_user (user_id) DEFAULT app_user_id();

-- Повторная подгрузка из источника не создаёт дублей (идемпотентный приём, П4).
CREATE UNIQUE INDEX patient_source_record_uq      ON patient      (source, source_record_id) WHERE source_record_id IS NOT NULL;
CREATE UNIQUE INDEX therapy_line_source_record_uq ON therapy_line (source, source_record_id) WHERE source_record_id IS NOT NULL;
CREATE UNIQUE INDEX observation_source_record_uq  ON observation  (source, source_record_id) WHERE source_record_id IS NOT NULL;
CREATE UNIQUE INDEX lab_source_record_uq          ON lab          (source, source_record_id) WHERE source_record_id IS NOT NULL;

-- ============================================================================
--  B4 + I7. Модульные поля: код пропуска, дата теста, несколько значений
-- ============================================================================
ALTER TABLE module_data DROP CONSTRAINT module_data_pkey;
ALTER TABLE module_data
    ADD COLUMN md_id               UUID NOT NULL DEFAULT gen_random_uuid(),
    ADD CONSTRAINT module_data_pkey PRIMARY KEY (md_id),
    ADD COLUMN missing             missing_code,
    ADD COLUMN test_date           DATE,
    ADD COLUMN test_date_precision date_precision,
    ADD COLUMN source              record_source NOT NULL,
    ADD COLUMN provenance          provenance NOT NULL,
    ADD COLUMN source_record_id    TEXT,
    ADD COLUMN created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN created_by          UUID REFERENCES app_user (user_id) DEFAULT app_user_id(),
    ADD COLUMN updated_at          TIMESTAMPTZ,
    ADD COLUMN updated_by          UUID REFERENCES app_user (user_id),
    ADD COLUMN deleted_at          TIMESTAMPTZ,
    ADD COLUMN deleted_by          UUID REFERENCES app_user (user_id),
    -- Либо значение, либо код пропуска — пустой ячейки не бывает.
    ADD CONSTRAINT module_data_value_or_missing CHECK ((value IS NULL) <> (missing IS NULL)),
    ADD CONSTRAINT module_data_test_date_precision CHECK ((test_date IS NULL) = (test_date_precision IS NULL)),
    ADD CONSTRAINT module_data_module_format CHECK (module ~ '^[a-z0-9_]+$');
-- Один результат маркера на дату теста (TP53 перед каждой линией — отдельные записи).
CREATE UNIQUE INDEX module_data_marker_uq ON module_data (patient_id, module, field_key, test_date)
    NULLS NOT DISTINCT WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX module_data_source_record_uq ON module_data (source, source_record_id) WHERE source_record_id IS NOT NULL;

-- ============================================================================
--  B2. Наблюдение привязано к линии терапии того же пациента
-- ============================================================================
ALTER TABLE therapy_line ADD CONSTRAINT therapy_line_line_patient_uq UNIQUE (line_id, patient_id);
ALTER TABLE observation
    ADD COLUMN line_id UUID,
    ADD CONSTRAINT observation_line_fkey FOREIGN KEY (line_id, patient_id)
        REFERENCES therapy_line (line_id, patient_id) ON DELETE RESTRICT;
CREATE INDEX observation_patient_idx ON observation (patient_id, obs_date);
CREATE INDEX observation_line_idx ON observation (line_id) WHERE line_id IS NOT NULL;

-- ============================================================================
--  I6 + I3. Ограничения дат, умных полей и свободного текста
-- ============================================================================
ALTER TABLE patient
    ADD CONSTRAINT patient_last_contact_after_dx CHECK (last_contact_date >= dx_date),
    ADD CONSTRAINT patient_birth_before_dx CHECK (birth_date IS NULL OR birth_date <= dx_date),
    ADD CONSTRAINT patient_cirs_range CHECK (cirs_score IS NULL OR cirs_score BETWEEN 0 AND 56),
    ADD CONSTRAINT patient_charlson_range CHECK (charlson_score IS NULL OR charlson_score >= 0),
    -- Умное поле «стадия» = {значение, система}: обе части или ни одной.
    ADD CONSTRAINT patient_stage_pair CHECK ((stage_value IS NULL) = (stage_system IS NULL)),
    ADD CONSTRAINT patient_stage_rai CHECK (stage_system <> 'Rai' OR stage_value IN ('0', 'I', 'II', 'III', 'IV')),
    ADD CONSTRAINT patient_stage_binet CHECK (stage_system <> 'Binet' OR stage_value IN ('A', 'B', 'C')),
    -- Умное поле «индекс» = {название, балл, группа, компоненты}.
    ADD CONSTRAINT patient_index_pair CHECK ((index_name IS NULL) = (index_score IS NULL)),
    ADD CONSTRAINT patient_index_name CHECK (index_name IN ('CLL-IPI', 'IPI', 'R-IPI', 'FLIPI', 'MIPI', 'IPS')),
    ADD CONSTRAINT patient_index_cll_ipi CHECK (index_name <> 'CLL-IPI' OR (
        index_score BETWEEN 0 AND 10
        AND index_group IN ('low', 'intermediate', 'high', 'very_high'))),
    -- Последний рубеж против ИИН в тексте: 12 цифр подряд отвергаются.
    ADD CONSTRAINT patient_histology_no_iin CHECK (
        histology_report IS NULL OR histology_report !~ '(^|[^0-9])[0-9]{12}([^0-9]|$)');

ALTER TABLE therapy_line
    ADD CONSTRAINT therapy_line_line_no_positive CHECK (line_no >= 1),
    ADD CONSTRAINT therapy_line_cycles_nonneg CHECK (cycles IS NULL OR cycles >= 0),
    ADD CONSTRAINT therapy_line_end_after_start CHECK (end_date IS NULL OR start_date IS NULL OR end_date >= start_date);
-- Длительность считается из дат, а не вводится руками.
ALTER TABLE therapy_line DROP COLUMN duration_days;
ALTER TABLE therapy_line ADD COLUMN duration_days INT GENERATED ALWAYS AS (end_date - start_date) STORED;

ALTER TABLE observation
    -- Умное поле «ответ» = {значение, критерии, дата (obs_date), основание}.
    ADD CONSTRAINT observation_response_complete CHECK (
        (response_value IS NULL AND response_criteria IS NULL AND response_basis IS NULL)
        OR (response_value IS NOT NULL AND response_criteria IS NOT NULL AND response_basis IS NOT NULL)),
    ADD CONSTRAINT observation_response_iwcll CHECK (
        response_criteria <> 'iwCLL 2018'
        OR response_value IN ('CR', 'CRi', 'nPR', 'PR', 'PR-L', 'SD', 'PD')),
    ADD CONSTRAINT observation_response_lugano CHECK (
        response_criteria <> 'Lugano 2014'
        OR response_value IN ('CMR', 'PMR', 'NMR', 'PMD', 'CR', 'PR', 'SD', 'PD')),
    -- Событие имеет дату; «нет события» — без даты.
    ADD CONSTRAINT observation_event_date CHECK ((event_type = 'none') = (event_date IS NULL)),
    -- Причина смерти указывается ровно для события «смерть» (допустимо 'unknown').
    ADD CONSTRAINT observation_death_cause_required CHECK ((event_type = 'death') = (death_cause IS NOT NULL)),
    ADD CONSTRAINT observation_pet_ct_no_iin CHECK (
        pet_ct_report IS NULL OR pet_ct_report !~ '(^|[^0-9])[0-9]{12}([^0-9]|$)');

ALTER TABLE lab
    ADD CONSTRAINT lab_flag_values CHECK (flag IN ('unknown_unit', 'unknown_loinc')),
    -- Каноническое значение есть ровно тогда, когда нет флага ручной проверки.
    ADD CONSTRAINT lab_flag_xor_canonical CHECK ((flag IS NULL) = (value_canonical IS NOT NULL));

-- ============================================================================
--  B1. Версионность диагноза: пересмотр создаёт новую версию, старая остаётся
-- ============================================================================
CREATE TABLE diagnosis_version (
    patient_id        UUID NOT NULL REFERENCES patient (patient_id) ON DELETE RESTRICT,
    version           INT  NOT NULL CHECK (version >= 1),
    who_haem5         TEXT NOT NULL,
    icd_o_3_2         TEXT NOT NULL,
    mkb               TEXT NOT NULL,
    dx_date           DATE NOT NULL,
    dx_date_precision date_precision NOT NULL,
    change_reason     TEXT,
    source            record_source NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by        UUID REFERENCES app_user (user_id) DEFAULT app_user_id(),
    PRIMARY KEY (patient_id, version),
    CONSTRAINT diagnosis_version_reason CHECK (version = 1 OR change_reason IS NOT NULL)
);
CREATE TRIGGER diagnosis_version_immutable BEFORE UPDATE OR DELETE ON diagnosis_version
    FOR EACH ROW EXECUTE FUNCTION forbid_modification();
CREATE TRIGGER diagnosis_version_no_truncate BEFORE TRUNCATE ON diagnosis_version
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_modification();

-- Номер версии ведёт БД; вручную его не выставить.
CREATE FUNCTION patient_dx_version_bump() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.dx_version := 1;
    ELSIF (NEW.who_haem5, NEW.icd_o_3_2, NEW.mkb, NEW.dx_date, NEW.dx_date_precision)
          IS DISTINCT FROM
          (OLD.who_haem5, OLD.icd_o_3_2, OLD.mkb, OLD.dx_date, OLD.dx_date_precision) THEN
        IF nullif(current_setting('app.change_reason', true), '') IS NULL THEN
            RAISE EXCEPTION 'Пересмотр диагноза требует причины (app.change_reason)';
        END IF;
        NEW.dx_version := OLD.dx_version + 1;
    ELSE
        NEW.dx_version := OLD.dx_version;
    END IF;
    RETURN NEW;
END $$;

CREATE FUNCTION patient_dx_version_record() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF TG_OP = 'INSERT' OR NEW.dx_version <> OLD.dx_version THEN
        INSERT INTO diagnosis_version (patient_id, version, who_haem5, icd_o_3_2, mkb,
                                       dx_date, dx_date_precision, change_reason, source)
        VALUES (NEW.patient_id, NEW.dx_version, NEW.who_haem5, NEW.icd_o_3_2, NEW.mkb,
                NEW.dx_date, NEW.dx_date_precision,
                CASE WHEN TG_OP = 'UPDATE' THEN current_setting('app.change_reason', true) END,
                NEW.source);
    END IF;
    RETURN NULL;
END $$;

CREATE TRIGGER patient_dx_version_bump BEFORE INSERT OR UPDATE ON patient
    FOR EACH ROW EXECUTE FUNCTION patient_dx_version_bump();
CREATE TRIGGER patient_dx_version_record AFTER INSERT OR UPDATE ON patient
    FOR EACH ROW EXECUTE FUNCTION patient_dx_version_record();

-- Филиал первичной регистрации сразу получает доступ к пациенту.
CREATE FUNCTION patient_register_branch() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    INSERT INTO patient_branch (patient_id, branch_code) VALUES (NEW.patient_id, NEW.branch);
    RETURN NULL;
END $$;
CREATE TRIGGER patient_register_branch AFTER INSERT ON patient
    FOR EACH ROW EXECUTE FUNCTION patient_register_branch();

-- ============================================================================
--  I5. Согласованность смерти (проверка в конце транзакции)
--  статус dead  <=>  ровно одно действующее наблюдение с событием death
-- ============================================================================
CREATE FUNCTION check_death_consistency() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_status  TEXT;
    v_deleted TIMESTAMPTZ;
    v_deaths  INT;
BEGIN
    SELECT status_last_contact, deleted_at INTO v_status, v_deleted
      FROM patient WHERE patient_id = NEW.patient_id;
    IF v_deleted IS NOT NULL THEN
        RETURN NULL;
    END IF;
    SELECT count(*) INTO v_deaths FROM observation
     WHERE patient_id = NEW.patient_id AND event_type = 'death' AND deleted_at IS NULL;
    IF v_status = 'dead' AND v_deaths = 0 THEN
        RAISE EXCEPTION 'Статус dead требует наблюдения с событием death (дата и причина смерти)';
    ELSIF v_status <> 'dead' AND v_deaths > 0 THEN
        RAISE EXCEPTION 'Есть событие death, но статус пациента %', v_status;
    ELSIF v_deaths > 1 THEN
        RAISE EXCEPTION 'У пациента больше одного события death';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER patient_death_consistency AFTER INSERT OR UPDATE ON patient
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_death_consistency();
CREATE CONSTRAINT TRIGGER observation_death_consistency AFTER INSERT OR UPDATE ON observation
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_death_consistency();

-- ============================================================================
--  I8. updated_at / updated_by выставляет БД
-- ============================================================================
CREATE FUNCTION touch_row() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    NEW.updated_by := app_user_id();
    RETURN NEW;
END $$;
CREATE TRIGGER patient_touch      BEFORE UPDATE ON patient      FOR EACH ROW EXECUTE FUNCTION touch_row();
CREATE TRIGGER therapy_line_touch BEFORE UPDATE ON therapy_line FOR EACH ROW EXECUTE FUNCTION touch_row();
CREATE TRIGGER observation_touch  BEFORE UPDATE ON observation  FOR EACH ROW EXECUTE FUNCTION touch_row();
CREATE TRIGGER lab_touch          BEFORE UPDATE ON lab          FOR EACH ROW EXECUTE FUNCTION touch_row();
CREATE TRIGGER module_data_touch  BEFORE UPDATE ON module_data  FOR EACH ROW EXECUTE FUNCTION touch_row();

-- ============================================================================
--  B5. Аудит-трейл на триггерах; журнал неизменяем
-- ============================================================================
ALTER TABLE audit_log
    ADD COLUMN txid   BIGINT NOT NULL DEFAULT txid_current(),
    ADD COLUMN reason TEXT;
COMMENT ON COLUMN audit_log.actor IS
    'UUID пользователя приложения (app.user_id) либо db:<роль> для правок мимо приложения';
CREATE INDEX audit_log_row_idx ON audit_log (table_name, row_id, ts);

-- Аргументы триггера — имена PK-колонок; row_id = их значения через '|'.
-- INSERT/DELETE пишут снимок строки, UPDATE — по строке на каждое изменённое поле.
-- Хэш пароля и ИИН в журнал не пишутся: факт изменения виден, значение — '***'.
CREATE FUNCTION audit_row() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_old    JSONB;
    v_new    JSONB;
    v_row    TEXT := '';
    v_key    TEXT;
    v_actor  TEXT := coalesce(nullif(current_setting('app.user_id', true), ''), 'db:' || session_user);
    v_reason TEXT := nullif(current_setting('app.change_reason', true), '');
    i        INT;
BEGIN
    IF TG_OP <> 'INSERT' THEN v_old := to_jsonb(OLD); END IF;
    IF TG_OP <> 'DELETE' THEN v_new := to_jsonb(NEW); END IF;
    FOR i IN 0 .. TG_NARGS - 1 LOOP
        v_row := v_row || CASE WHEN i > 0 THEN '|' ELSE '' END
                       || coalesce(coalesce(v_new, v_old) ->> TG_ARGV[i], '');
    END LOOP;

    IF TG_OP = 'UPDATE' THEN
        FOR v_key IN SELECT jsonb_object_keys(v_new) LOOP
            CONTINUE WHEN v_key IN ('updated_at', 'updated_by');
            IF (v_new -> v_key) IS DISTINCT FROM (v_old -> v_key) THEN
                INSERT INTO audit_log (actor, table_name, row_id, action, field, old_value, new_value, reason)
                VALUES (v_actor, TG_TABLE_NAME, v_row, 'update', v_key,
                        CASE WHEN v_key IN ('password_hash', 'iin') THEN '***' ELSE v_old ->> v_key END,
                        CASE WHEN v_key IN ('password_hash', 'iin') THEN '***' ELSE v_new ->> v_key END,
                        v_reason);
            END IF;
        END LOOP;
    ELSE
        INSERT INTO audit_log (actor, table_name, row_id, action, field, old_value, new_value, reason)
        VALUES (v_actor, TG_TABLE_NAME, v_row, lower(TG_OP), NULL,
                (v_old - 'password_hash' - 'iin')::text, (v_new - 'password_hash' - 'iin')::text, v_reason);
    END IF;
    RETURN NULL;
END $$;

CREATE TRIGGER patient_audit         AFTER INSERT OR UPDATE OR DELETE ON patient         FOR EACH ROW EXECUTE FUNCTION audit_row('patient_id');
CREATE TRIGGER patient_missing_audit AFTER INSERT OR UPDATE OR DELETE ON patient_missing FOR EACH ROW EXECUTE FUNCTION audit_row('patient_id', 'field');
CREATE TRIGGER patient_branch_audit  AFTER INSERT OR UPDATE OR DELETE ON patient_branch  FOR EACH ROW EXECUTE FUNCTION audit_row('patient_id', 'branch_code');
CREATE TRIGGER module_data_audit     AFTER INSERT OR UPDATE OR DELETE ON module_data     FOR EACH ROW EXECUTE FUNCTION audit_row('md_id');
CREATE TRIGGER therapy_line_audit    AFTER INSERT OR UPDATE OR DELETE ON therapy_line    FOR EACH ROW EXECUTE FUNCTION audit_row('line_id');
CREATE TRIGGER observation_audit     AFTER INSERT OR UPDATE OR DELETE ON observation     FOR EACH ROW EXECUTE FUNCTION audit_row('obs_id');
CREATE TRIGGER lab_audit             AFTER INSERT OR UPDATE OR DELETE ON lab             FOR EACH ROW EXECUTE FUNCTION audit_row('lab_id');
CREATE TRIGGER app_user_audit        AFTER INSERT OR UPDATE OR DELETE ON app_user        FOR EACH ROW EXECUTE FUNCTION audit_row('user_id');
CREATE TRIGGER branch_audit          AFTER INSERT OR UPDATE OR DELETE ON branch          FOR EACH ROW EXECUTE FUNCTION audit_row('branch_code');

CREATE TRIGGER audit_log_immutable BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION forbid_modification();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_modification();

-- ============================================================================
--  Представление выживаемости под новые ключи и коды.
--  Семантика прежняя (событие = прогрессия/рецидив/смерть, т.е. PFS);
--  раздельные OS / PFS / EFS / TTNT — этап П3.
-- ============================================================================
CREATE VIEW v_survival AS
SELECT
    p.patient_id,
    p.who_haem5,
    p.branch,
    p.dx_date AS time_zero,
    COALESCE(
        (SELECT MIN(o.event_date) FROM observation o
          WHERE o.patient_id = p.patient_id AND o.deleted_at IS NULL
            AND o.event_type IN ('progression', 'relapse', 'death')),
        p.last_contact_date
    ) AS event_or_censor_date,
    CASE WHEN EXISTS (
        SELECT 1 FROM observation o
         WHERE o.patient_id = p.patient_id AND o.deleted_at IS NULL
           AND o.event_type IN ('progression', 'relapse', 'death')
    ) THEN 1 ELSE 0 END AS event
FROM patient p
WHERE p.deleted_at IS NULL;

-- ============================================================================
--  Права. Роль приложения не удаляет клинические записи (мягкое удаление через
--  UPDATE deleted_at) и не пишет в журналы напрямую — только через триггеры.
--  Логин-пользователь сервиса создаётся при развёртывании: GRANT kalypso_app TO <login>.
-- ============================================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kalypso_app') THEN
        CREATE ROLE kalypso_app NOLOGIN;
    END IF;
END $$;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO kalypso_app;
GRANT SELECT, INSERT, UPDATE         ON patient, therapy_line, observation, lab, module_data TO kalypso_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON patient_missing, patient_branch TO kalypso_app;
GRANT SELECT, INSERT, UPDATE         ON app_user, branch TO kalypso_app;
GRANT SELECT                         ON role, diagnosis_version, audit_log, v_survival TO kalypso_app;
GRANT SELECT, INSERT                 ON login_log TO kalypso_app;
GRANT USAGE ON SEQUENCE login_log_login_id_seq TO kalypso_app;
