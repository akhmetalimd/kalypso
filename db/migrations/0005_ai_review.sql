-- ============================================================================
--  KALYPSO — миграция 0005: П2 — первичные документы, AI-предложения, сверка
--
--  Конвейер (PRD §6.2): документ (деидентифицированный текст) -> AI-прогон ->
--  предложения -> сверка куратором -> запись в регистр с source = 'ai_document'.
--
--  Шлюз качества — куратор. Он пишет в клинические таблицы ТОЛЬКО записи с
--  source = 'ai_document', а в карточке пациента меняет только диагноз,
--  текст гистологии и дату последнего контакта (по принятому предложению).
--  Пакетного принятия нет: каждое предложение решается отдельно.
-- ============================================================================

-- Служебная роль фонового AI-обработчика: читает документы и регистр (для поиска
-- противоречий), пишет прогоны и предложения. Входа в веб-интерфейс у неё нет.
INSERT INTO role (role_code, name_ru) VALUES ('ai_worker', 'AI-обработчик (служебная роль)');
INSERT INTO app_user (login, display_name, password_hash, role_code, must_change_password)
VALUES ('ai-worker', 'AI-обработчик (служебный)', '!no-login', 'ai_worker', false);

CREATE OR REPLACE FUNCTION app_patient_visible(pid UUID) RETURNS BOOLEAN
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT CASE app_role()
        WHEN 'physician' THEN
               EXISTS (SELECT 1 FROM patient p
                        WHERE p.patient_id = pid AND p.branch = app_branch())
            OR EXISTS (SELECT 1 FROM patient_branch pb
                        WHERE pb.patient_id = pid AND pb.branch_code = app_branch())
        WHEN 'curator'   THEN true
        WHEN 'analyst'   THEN true
        WHEN 'admin'     THEN true
        WHEN 'ai_worker' THEN true
        ELSE false
    END
$$;

-- ============================================================================
--  Первичный документ: только деидентифицированный текст
-- ============================================================================
CREATE TABLE source_document (
    doc_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id          UUID NOT NULL REFERENCES patient (patient_id) ON DELETE RESTRICT,
    doc_type            TEXT NOT NULL CHECK (doc_type IN ('histology', 'cytogenetics', 'molecular', 'pet_ct', 'other')),
    doc_date            DATE NOT NULL,
    provenance          provenance NOT NULL,
    source              record_source NOT NULL DEFAULT 'manual',
    source_record_id    TEXT,
    text_deid           TEXT NOT NULL CHECK (length(text_deid) BETWEEN 1 AND 50000),
    deid_report         JSONB NOT NULL,                  -- {вид: число замен}, без самих значений
    deid_passed         BOOLEAN NOT NULL,                -- автопроверка + подтверждение пользователя
    status              TEXT NOT NULL CHECK (status IN
                            ('queued', 'processing', 'proposed', 'manual', 'reviewed')),
    status_note         TEXT,
    attempts            INT NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by          UUID REFERENCES app_user (user_id) DEFAULT app_user_id(),
    updated_at          TIMESTAMPTZ,
    updated_by          UUID REFERENCES app_user (user_id),
    review_started_at   TIMESTAMPTZ,
    review_finished_at  TIMESTAMPTZ,
    deleted_at          TIMESTAMPTZ,
    deleted_by          UUID REFERENCES app_user (user_id),
    -- К модели допускается только прошедший деидентификацию текст.
    CONSTRAINT source_document_ai_needs_deid CHECK (deid_passed OR status IN ('manual', 'reviewed')),
    CONSTRAINT source_document_no_iin CHECK (text_deid !~ '(^|[^0-9])[0-9]{12}([^0-9]|$)')
);
CREATE INDEX source_document_queue_idx ON source_document (status, created_at) WHERE deleted_at IS NULL;
CREATE INDEX source_document_patient_idx ON source_document (patient_id, doc_date);
CREATE TRIGGER source_document_touch BEFORE UPDATE ON source_document FOR EACH ROW EXECUTE FUNCTION touch_row();

-- ============================================================================
--  AI-прогон: что, какой моделью и с каким исходом обработано (неизменяем)
-- ============================================================================
CREATE TABLE ai_run (
    run_id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    doc_id                UUID NOT NULL REFERENCES source_document (doc_id) ON DELETE RESTRICT,
    extractor             TEXT NOT NULL,                 -- claude | rules | fake
    model                 TEXT,
    prompt_version        TEXT NOT NULL,
    status                TEXT NOT NULL CHECK (status IN ('ok', 'error', 'refusal', 'invalid', 'timeout')),
    error                 TEXT,
    request_id            TEXT,
    input_tokens          INT,
    output_tokens         INT,
    cache_read_tokens     INT,
    cache_creation_tokens INT,
    started_at            TIMESTAMPTZ NOT NULL,
    finished_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by            UUID REFERENCES app_user (user_id) DEFAULT app_user_id()
);
CREATE TRIGGER ai_run_immutable BEFORE UPDATE OR DELETE ON ai_run
    FOR EACH ROW EXECUTE FUNCTION forbid_modification();

-- Прогон возможен только по документу, прошедшему деидентификацию.
CREATE FUNCTION ai_run_requires_deid() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM source_document WHERE doc_id = NEW.doc_id AND deid_passed) THEN
        RAISE EXCEPTION 'Документ не прошёл деидентификацию — передача в AI запрещена';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER ai_run_requires_deid BEFORE INSERT ON ai_run
    FOR EACH ROW EXECUTE FUNCTION ai_run_requires_deid();

-- ============================================================================
--  Предложение AI по одному полю + решение куратора
-- ============================================================================
CREATE TABLE ai_proposal (
    proposal_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          UUID NOT NULL REFERENCES ai_run (run_id) ON DELETE RESTRICT,
    doc_id          UUID NOT NULL REFERENCES source_document (doc_id) ON DELETE RESTRICT,
    patient_id      UUID NOT NULL REFERENCES patient (patient_id) ON DELETE RESTRICT,
    target          TEXT NOT NULL,                       -- поле регистра (kalypso/ai/fields.py)
    proposed        JSONB NOT NULL,                      -- {"value": ...}
    current         JSONB,                               -- значение в регистре на момент прогона
    conflict        BOOLEAN NOT NULL DEFAULT false,      -- противоречит внесённому
    evidence        TEXT,                                -- дословная цитата-основание
    evidence_start  INT,
    evidence_end    INT,
    grounded        BOOLEAN NOT NULL,                    -- цитата найдена в тексте документа
    confidence      TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'edited', 'rejected')),
    final           JSONB,                               -- что записано в регистр (accepted/edited)
    review_note     TEXT,
    reviewed_by     UUID REFERENCES app_user (user_id),
    reviewed_at     TIMESTAMPTZ,
    applied_table   TEXT,
    applied_id      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ai_proposal_decision CHECK ((status = 'pending') = (reviewed_at IS NULL)),
    CONSTRAINT ai_proposal_final CHECK ((status IN ('accepted', 'edited')) = (final IS NOT NULL)),
    CONSTRAINT ai_proposal_evidence_span CHECK ((evidence_start IS NULL) = (evidence_end IS NULL))
);
CREATE INDEX ai_proposal_doc_idx ON ai_proposal (doc_id, status);
CREATE INDEX ai_proposal_target_idx ON ai_proposal (target, status);

CREATE TRIGGER source_document_audit AFTER INSERT OR UPDATE OR DELETE ON source_document FOR EACH ROW EXECUTE FUNCTION audit_row('doc_id');
CREATE TRIGGER ai_run_audit          AFTER INSERT OR UPDATE OR DELETE ON ai_run          FOR EACH ROW EXECUTE FUNCTION audit_row('run_id');
CREATE TRIGGER ai_proposal_audit     AFTER INSERT OR UPDATE OR DELETE ON ai_proposal     FOR EACH ROW EXECUTE FUNCTION audit_row('proposal_id');

-- ============================================================================
--  RLS для новых таблиц
-- ============================================================================
ALTER TABLE source_document ENABLE ROW LEVEL SECURITY;
CREATE POLICY source_document_select ON source_document FOR SELECT TO kalypso_app
    USING (app_patient_visible(patient_id));
CREATE POLICY source_document_insert ON source_document FOR INSERT TO kalypso_app
    WITH CHECK (app_patient_writable(patient_id));
-- Статусы меняют: обработчик (обработка), куратор (сверка, повторная отправка),
-- врач своего филиала (документ из ручной очереди внесён вручную).
CREATE POLICY source_document_update ON source_document FOR UPDATE TO kalypso_app
    USING (app_role() IN ('ai_worker', 'curator') OR app_patient_writable(patient_id))
    WITH CHECK (app_role() IN ('ai_worker', 'curator') OR app_patient_writable(patient_id));

ALTER TABLE ai_run ENABLE ROW LEVEL SECURITY;
CREATE POLICY ai_run_select ON ai_run FOR SELECT TO kalypso_app
    USING (EXISTS (SELECT 1 FROM source_document d WHERE d.doc_id = ai_run.doc_id));
CREATE POLICY ai_run_insert ON ai_run FOR INSERT TO kalypso_app
    WITH CHECK (app_role() = 'ai_worker');

ALTER TABLE ai_proposal ENABLE ROW LEVEL SECURITY;
CREATE POLICY ai_proposal_select ON ai_proposal FOR SELECT TO kalypso_app
    USING (app_patient_visible(patient_id));
CREATE POLICY ai_proposal_insert ON ai_proposal FOR INSERT TO kalypso_app
    WITH CHECK (app_role() = 'ai_worker');
CREATE POLICY ai_proposal_update ON ai_proposal FOR UPDATE TO kalypso_app
    USING (app_role() = 'curator') WITH CHECK (app_role() = 'curator');

-- ============================================================================
--  Куратор: запись только для source = 'ai_document'
-- ============================================================================
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['module_data', 'observation', 'lab', 'therapy_line'] LOOP
        EXECUTE format('CREATE POLICY %I ON %I FOR INSERT TO kalypso_app
                            WITH CHECK (app_role() = %L AND source = %L AND app_patient_visible(patient_id))',
                       t || '_insert_curator_ai', t, 'curator', 'ai_document');
        EXECUTE format('CREATE POLICY %I ON %I FOR UPDATE TO kalypso_app
                            USING (app_role() = %L AND source = %L)
                            WITH CHECK (app_role() = %L AND source = %L)',
                       t || '_update_curator_ai', t, 'curator', 'ai_document', 'curator', 'ai_document');
    END LOOP;
END $$;

-- Карточка пациента: куратор может обновить её только по принятому предложению,
-- и только диагноз (с версией), текст гистологии и последний контакт.
CREATE POLICY patient_update_curator ON patient FOR UPDATE TO kalypso_app
    USING (app_role() = 'curator') WITH CHECK (app_role() = 'curator');

CREATE FUNCTION patient_curator_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    allowed TEXT[] := ARRAY['who_haem5', 'icd_o_3_2', 'mkb', 'dx_version', 'histology_report',
                            'last_contact_date', 'updated_at', 'updated_by'];
BEGIN
    IF app_role() = 'curator' AND (to_jsonb(NEW) - allowed) IS DISTINCT FROM (to_jsonb(OLD) - allowed) THEN
        RAISE EXCEPTION 'Куратор меняет в карточке только диагноз, текст гистологии и последний контакт';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER patient_curator_guard BEFORE UPDATE ON patient
    FOR EACH ROW EXECUTE FUNCTION patient_curator_guard();

GRANT SELECT, INSERT, UPDATE ON source_document, ai_proposal TO kalypso_app;
GRANT SELECT, INSERT         ON ai_run TO kalypso_app;
