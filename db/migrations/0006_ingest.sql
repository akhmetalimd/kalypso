-- ============================================================================
--  KALYPSO — миграция 0006: П4 — задел под his.kz (конвейер приёма из МИС)
--
--  Коннектор (служебный пользователь his-connector, роль ingest_worker) работает
--  внутри сети клиники. Он НЕ пишет в клинические таблицы: только раскладывает
--  данные МИС после деидентификации и маппинга в промежуточную зону ingest_item.
--  Всё структурное и документы применяет куратор (решение владельца проекта):
--  записи получают source = 'his_structured'. Сырые ИИН, ФИО и внешние ID
--  записей МИС в БД не попадают (только HMAC).
-- ============================================================================

INSERT INTO role (role_code, name_ru) VALUES ('ingest_worker', 'Коннектор МИС (служебная роль)');
INSERT INTO app_user (login, display_name, password_hash, role_code, must_change_password)
VALUES ('his-connector', 'Коннектор his.kz (служебный)', '!no-login', 'ingest_worker', false);

-- Коннектору нужно знать, есть ли пациент в регистре (по хэшу) — только id.
CREATE OR REPLACE FUNCTION patient_id_by_hash(h TEXT) RETURNS UUID
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT patient_id FROM patient WHERE iin_hash = h AND app_role() IN ('physician', 'ingest_worker', 'curator')
$$;

-- ============================================================================
--  Журнал прогонов, метка инкремента, промежуточная зона
-- ============================================================================
CREATE TABLE ingest_run (
    run_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_system   TEXT NOT NULL,
    adapter         TEXT NOT NULL,
    mapping_version TEXT NOT NULL,
    since           TIMESTAMPTZ,
    until           TIMESTAMPTZ NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'ok', 'failed')),
    counts          JSONB NOT NULL DEFAULT '{}'::jsonb,
    error           TEXT,
    created_by      UUID REFERENCES app_user (user_id) DEFAULT app_user_id()
);

CREATE TABLE ingest_state (
    source_system TEXT PRIMARY KEY,
    last_until    TIMESTAMPTZ NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE ingest_item (
    item_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          UUID NOT NULL REFERENCES ingest_run (run_id) ON DELETE RESTRICT,
    source_system   TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('patient', 'diagnosis', 'lab', 'document')),
    record_key      TEXT NOT NULL CHECK (record_key ~ '^[0-9a-f]{64}$'),   -- HMAC внешнего ID записи МИС
    content_hash    TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    iin_hash        TEXT NOT NULL CHECK (iin_hash ~ '^[0-9a-f]{64}$'),
    patient_id      UUID REFERENCES patient (patient_id) ON DELETE RESTRICT,  -- если уже в регистре
    payload         JSONB NOT NULL,                       -- поля регистра после маппинга (белый список)
    issues          JSONB NOT NULL DEFAULT '[]'::jsonb,   -- проблемы маппинга: такие элементы не применяются
    change          TEXT NOT NULL CHECK (change IN ('new', 'update')),
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'applied', 'rejected', 'superseded')),
    decided_by      UUID REFERENCES app_user (user_id),
    decided_at      TIMESTAMPTZ,
    decision_note   TEXT,
    applied_table   TEXT,
    applied_id      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ingest_item_unique UNIQUE (source_system, record_key, content_hash),
    CONSTRAINT ingest_item_decision CHECK ((status IN ('pending', 'superseded')) = (decided_at IS NULL)),
    CONSTRAINT ingest_item_no_iin CHECK (payload::text !~ '(^|[^0-9])[0-9]{12}([^0-9]|$)')
);
CREATE INDEX ingest_item_queue_idx ON ingest_item (status, kind, created_at);
CREATE INDEX ingest_item_record_idx ON ingest_item (source_system, record_key);
CREATE INDEX ingest_item_iin_idx ON ingest_item (iin_hash);

CREATE TRIGGER ingest_run_audit  AFTER INSERT OR UPDATE OR DELETE ON ingest_run  FOR EACH ROW EXECUTE FUNCTION audit_row('run_id');
CREATE TRIGGER ingest_item_audit AFTER INSERT OR UPDATE OR DELETE ON ingest_item FOR EACH ROW EXECUTE FUNCTION audit_row('item_id');

ALTER TABLE ingest_run ENABLE ROW LEVEL SECURITY;
CREATE POLICY ingest_run_select ON ingest_run FOR SELECT TO kalypso_app
    USING (app_role() IN ('ingest_worker', 'curator', 'admin'));
CREATE POLICY ingest_run_insert ON ingest_run FOR INSERT TO kalypso_app WITH CHECK (app_role() = 'ingest_worker');
CREATE POLICY ingest_run_update ON ingest_run FOR UPDATE TO kalypso_app
    USING (app_role() = 'ingest_worker') WITH CHECK (app_role() = 'ingest_worker');

ALTER TABLE ingest_state ENABLE ROW LEVEL SECURITY;
CREATE POLICY ingest_state_all ON ingest_state FOR ALL TO kalypso_app
    USING (app_role() = 'ingest_worker') WITH CHECK (app_role() = 'ingest_worker');
CREATE POLICY ingest_state_read ON ingest_state FOR SELECT TO kalypso_app
    USING (app_role() IN ('curator', 'admin'));

ALTER TABLE ingest_item ENABLE ROW LEVEL SECURITY;
CREATE POLICY ingest_item_select ON ingest_item FOR SELECT TO kalypso_app
    USING (app_role() IN ('ingest_worker', 'curator', 'admin'));
CREATE POLICY ingest_item_insert ON ingest_item FOR INSERT TO kalypso_app WITH CHECK (app_role() = 'ingest_worker');
-- Коннектор только помечает устаревшие элементы; решения принимает куратор.
CREATE POLICY ingest_item_update ON ingest_item FOR UPDATE TO kalypso_app
    USING (app_role() IN ('ingest_worker', 'curator')) WITH CHECK (app_role() IN ('ingest_worker', 'curator'));

-- ============================================================================
--  Куратор применяет данные МИС: те же права, что для AI, плюс source his_structured
-- ============================================================================
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['module_data', 'observation', 'lab', 'therapy_line'] LOOP
        EXECUTE format('DROP POLICY %I ON %I', t || '_insert_curator_ai', t);
        EXECUTE format('DROP POLICY %I ON %I', t || '_update_curator_ai', t);
        EXECUTE format('CREATE POLICY %I ON %I FOR INSERT TO kalypso_app
                            WITH CHECK (app_role() = %L AND source IN (%L, %L) AND app_patient_visible(patient_id))',
                       t || '_insert_curator', t, 'curator', 'ai_document', 'his_structured');
        EXECUTE format('CREATE POLICY %I ON %I FOR UPDATE TO kalypso_app
                            USING (app_role() = %L AND source IN (%L, %L))
                            WITH CHECK (app_role() = %L AND source IN (%L, %L))',
                       t || '_update_curator', t, 'curator', 'ai_document', 'his_structured',
                       'curator', 'ai_document', 'his_structured');
    END LOOP;
END $$;

-- Новый пациент из МИС — только по решению куратора и только с источником his_structured.
CREATE POLICY patient_insert_curator_his ON patient FOR INSERT TO kalypso_app
    WITH CHECK (app_role() = 'curator' AND source = 'his_structured');

-- Документ из МИС создаёт куратор после проверки очищенного текста.
CREATE POLICY source_document_insert_curator_his ON source_document FOR INSERT TO kalypso_app
    WITH CHECK (app_role() = 'curator' AND source = 'his_structured');

GRANT SELECT, INSERT, UPDATE ON ingest_run, ingest_state, ingest_item TO kalypso_app;
