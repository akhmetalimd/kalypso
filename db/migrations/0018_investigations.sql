-- ============================================================================
--  KALYPSO — миграция 0018: исследования карточки (ИФТ, генетика, вирусный статус,
--  аутоиммунный скрининг)
-- ============================================================================
-- Одна запись — одно проведённое исследование: справочник — config/investigations.json
-- (kind + test_code), дата, материал, качественный результат (есть / нет / сомнительно
-- и что именно), количественный (число + единица), маркеры ИФТ (как в заключении),
-- заключение. Разделы необязательные: нет записи — исследование не проводилось.
-- Вирусные поля модулей (ВИЧ и EBV у ЛБ, HCV у ЛМЗ, ДНК EBV у ПТКЛ) переносятся сюда.

CREATE TABLE investigation (
    inv_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id          UUID NOT NULL REFERENCES patient (patient_id) ON DELETE RESTRICT,
    kind                TEXT NOT NULL CHECK (kind IN ('flow', 'genetics', 'virology', 'autoimmune')),
    test_code           TEXT NOT NULL CHECK (test_code ~ '^[a-z0-9_]{1,40}$'),
    test_date           DATE,
    test_date_precision date_precision,
    material            TEXT CHECK (material ~ '^[a-z_]{1,30}$'),
    qual_result         TEXT CHECK (qual_result IN ('positive', 'negative', 'equivocal')),
    qual_detail         TEXT CHECK (length(qual_detail) <= 300),
    value               NUMERIC,
    unit                TEXT CHECK (length(unit) <= 30),
    markers             JSONB CHECK (markers IS NULL OR jsonb_typeof(markers) = 'array'),
    conclusion          TEXT CHECK (length(conclusion) <= 4000),
    source              record_source NOT NULL,
    provenance          provenance NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by          UUID REFERENCES app_user (user_id) DEFAULT app_user_id(),
    updated_at          TIMESTAMPTZ,
    updated_by          UUID REFERENCES app_user (user_id),
    deleted_at          TIMESTAMPTZ,
    deleted_by          UUID REFERENCES app_user (user_id),
    CONSTRAINT investigation_has_result CHECK (
        qual_result IS NOT NULL OR value IS NOT NULL OR markers IS NOT NULL OR conclusion IS NOT NULL),
    CONSTRAINT investigation_date_precision CHECK ((test_date IS NULL) = (test_date_precision IS NULL)),
    CONSTRAINT investigation_unit_needs_value CHECK (unit IS NULL OR value IS NOT NULL),
    CONSTRAINT investigation_text_no_iin CHECK (
        coalesce(qual_detail, '') || ' ' || coalesce(conclusion, '') !~ '(^|[^0-9])[0-9]{12}([^0-9]|$)')
);
CREATE INDEX investigation_patient_idx ON investigation (patient_id, kind) WHERE deleted_at IS NULL;

CREATE TRIGGER investigation_touch BEFORE UPDATE ON investigation FOR EACH ROW EXECUTE FUNCTION touch_row();
CREATE TRIGGER investigation_audit AFTER INSERT OR UPDATE OR DELETE ON investigation
    FOR EACH ROW EXECUTE FUNCTION audit_row('inv_id');

-- Те же права и разграничение по филиалам, что у остальных клинических записей (0002, 0004).
GRANT SELECT, INSERT, UPDATE ON investigation TO kalypso_app;
ALTER TABLE investigation ENABLE ROW LEVEL SECURITY;
CREATE POLICY investigation_select ON investigation FOR SELECT TO kalypso_app
    USING (app_patient_visible(patient_id));
CREATE POLICY investigation_insert ON investigation FOR INSERT TO kalypso_app
    WITH CHECK (app_patient_writable(patient_id));
CREATE POLICY investigation_update ON investigation FOR UPDATE TO kalypso_app
    USING (app_patient_writable(patient_id)) WITH CHECK (app_patient_writable(patient_id));

-- Перенос вирусных полей модулей в «Вирусный статус». Коды пропуска не переносятся:
-- нет записи — не проводилось. Прежние строки модуля помечаются удалёнными (аудит сохраняет).
INSERT INTO investigation (patient_id, kind, test_code, test_date, test_date_precision, material,
                           qual_result, source, provenance, created_at, created_by)
SELECT md.patient_id, 'virology',
       CASE md.field_key WHEN 'hiv' THEN 'hiv' WHEN 'ebv' THEN 'ebv_eber'
                         WHEN 'hcv' THEN 'anti_hcv' WHEN 'ebv_dna' THEN 'ebv_dna' END,
       md.test_date, md.test_date_precision,
       CASE md.field_key WHEN 'ebv' THEN 'tissue' WHEN 'ebv_dna' THEN 'blood' ELSE 'serum' END,
       CASE md.value WHEN 'detected' THEN 'positive' ELSE 'negative' END,
       md.source, md.provenance, md.created_at, md.created_by
FROM module_data md
WHERE md.deleted_at IS NULL AND md.value IS NOT NULL
  AND (md.module, md.field_key) IN (('burkitt', 'hiv'), ('burkitt', 'ebv'), ('mzl', 'hcv'), ('ptcl', 'ebv_dna'));

UPDATE module_data SET deleted_at = now(), deleted_by = created_by
WHERE deleted_at IS NULL
  AND (module, field_key) IN (('burkitt', 'hiv'), ('burkitt', 'ebv'), ('mzl', 'hcv'), ('ptcl', 'ebv_dna'));
