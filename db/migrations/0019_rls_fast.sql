-- ============================================================================
--  KALYPSO — миграция 0019: быстрые политики чтения RLS (те же права, что в 0004)
--
--  Раньше политика чтения вызывала app_patient_visible(patient_id) на КАЖДУЮ строку:
--  функция SECURITY DEFINER не встраивается в план, и роль пользователя искалась заново
--  для каждой записи — чтение когорты (дашборды, «Исследователь») шло в 7–8 раз дольше,
--  чем без RLS. Теперь список видимых пациентов считается один раз на запрос
--  (app_visible_patient_ids, хэшированный подзапрос), правила — прежние:
--
--    врач                        пациенты своего филиала (филиал регистрации или patient_branch)
--    куратор / аналитик / админ  все пациенты
--    неактивный / без контекста  никто
--
--  Политики записи (app_patient_writable) не меняются: записей за раз немного.
--  app_patient_visible остаётся — её используют app_patient_writable и проверки.
-- ============================================================================

CREATE FUNCTION app_visible_patient_ids() RETURNS SETOF UUID
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT p.patient_id FROM patient p
     WHERE app_role() IN ('curator', 'analyst', 'admin')
    UNION
    SELECT p.patient_id FROM patient p
     WHERE app_role() = 'physician' AND p.branch = app_branch()
    UNION
    SELECT pb.patient_id FROM patient_branch pb
     WHERE app_role() = 'physician' AND pb.branch_code = app_branch()
$$;

REVOKE ALL ON FUNCTION app_visible_patient_ids() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_visible_patient_ids() TO kalypso_app;

-- branch = app_branch() остаётся: нужен для INSERT … RETURNING (строка patient_branch
-- создаётся триггером уже после проверки политики), как в 0004.
ALTER POLICY patient_select ON patient
    USING (branch = app_branch() OR patient_id IN (SELECT app_visible_patient_ids()));

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['patient_missing', 'module_data', 'therapy_line', 'observation', 'lab',
                             'diagnosis_version', 'patient_branch', 'investigation'] LOOP
        EXECUTE format('ALTER POLICY %I ON %I USING (patient_id IN (SELECT app_visible_patient_ids()))',
                       t || '_select', t);
    END LOOP;
END $$;
