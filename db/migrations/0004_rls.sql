-- ============================================================================
--  KALYPSO — миграция 0004: RLS — второй рубеж разграничения по филиалам
--
--  Первый рубеж — проверки прав в сервисах приложения. Здесь та же матрица
--  (config/roles.json) повторена в БД: даже если в приложении ошибка в запросе,
--  роль kalypso_app не прочитает и не изменит пациента чужого филиала.
--
--  Контекст — app.user_id (SET LOCAL в каждой транзакции). Без него роль
--  приложения не видит ни одного пациента. Владелец схемы RLS не подчиняется
--  (миграции, триггеры SECURITY DEFINER, аудит).
--
--    врач (physician)         чтение и запись: пациенты своего филиала
--                             (филиал регистрации или patient_branch)
--    куратор / аналитик / админ  чтение всех; клинические записи не пишут
--    куратор                  связывает филиал с пациентом (patient_branch)
-- ============================================================================

-- Контекст пользователя. SECURITY DEFINER: функции вызываются из политик и не
-- должны сами упираться в RLS (иначе — рекурсия политик patient_branch).
CREATE FUNCTION app_role() RETURNS TEXT
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT role_code FROM app_user WHERE user_id = app_user_id() AND is_active
$$;

CREATE FUNCTION app_branch() RETURNS TEXT
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT branch_code FROM app_user WHERE user_id = app_user_id() AND is_active
$$;

CREATE FUNCTION app_patient_visible(pid UUID) RETURNS BOOLEAN
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT CASE app_role()
        WHEN 'physician' THEN
               EXISTS (SELECT 1 FROM patient p
                        WHERE p.patient_id = pid AND p.branch = app_branch())
            OR EXISTS (SELECT 1 FROM patient_branch pb
                        WHERE pb.patient_id = pid AND pb.branch_code = app_branch())
        WHEN 'curator' THEN true
        WHEN 'analyst' THEN true
        WHEN 'admin'   THEN true
        ELSE false
    END
$$;

CREATE FUNCTION app_patient_writable(pid UUID) RETURNS BOOLEAN
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT app_role() = 'physician' AND app_patient_visible(pid)
$$;

-- Дедупликация: врач должен узнать, что пациент с этим хэшем уже есть, не видя
-- его данных. Функция отдаёт только идентификатор и только врачу.
CREATE FUNCTION patient_id_by_hash(h TEXT) RETURNS UUID
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT patient_id FROM patient WHERE iin_hash = h AND app_role() = 'physician'
$$;

REVOKE ALL ON FUNCTION app_role(), app_branch(), app_patient_visible(UUID),
                       app_patient_writable(UUID), patient_id_by_hash(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_role(), app_branch(), app_patient_visible(UUID),
                          app_patient_writable(UUID), patient_id_by_hash(TEXT) TO kalypso_app;

-- Проверка смерти должна видеть все наблюдения пациента независимо от RLS.
ALTER FUNCTION check_death_consistency() SECURITY DEFINER SET search_path = public, pg_temp;

-- --- пациент ----------------------------------------------------------------
ALTER TABLE patient ENABLE ROW LEVEL SECURITY;
-- branch = app_branch() нужен для INSERT … RETURNING: строка patient_branch
-- создаётся триггером уже после проверки политики.
CREATE POLICY patient_select ON patient FOR SELECT TO kalypso_app
    USING (branch = app_branch() OR app_patient_visible(patient_id));
CREATE POLICY patient_insert ON patient FOR INSERT TO kalypso_app
    WITH CHECK (app_role() = 'physician' AND branch = app_branch());
CREATE POLICY patient_update ON patient FOR UPDATE TO kalypso_app
    USING (app_patient_writable(patient_id))
    WITH CHECK (app_patient_writable(patient_id));

-- --- клинические записи пациента ---------------------------------------------
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['patient_missing', 'module_data', 'therapy_line', 'observation', 'lab'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY %I ON %I FOR SELECT TO kalypso_app
                            USING (app_patient_visible(patient_id))', t || '_select', t);
        EXECUTE format('CREATE POLICY %I ON %I FOR INSERT TO kalypso_app
                            WITH CHECK (app_patient_writable(patient_id))', t || '_insert', t);
        EXECUTE format('CREATE POLICY %I ON %I FOR UPDATE TO kalypso_app
                            USING (app_patient_writable(patient_id))
                            WITH CHECK (app_patient_writable(patient_id))', t || '_update', t);
    END LOOP;
END $$;
CREATE POLICY patient_missing_delete ON patient_missing FOR DELETE TO kalypso_app
    USING (app_patient_writable(patient_id));

-- --- версии диагноза (пишет только триггер владельца) -------------------------
ALTER TABLE diagnosis_version ENABLE ROW LEVEL SECURITY;
CREATE POLICY diagnosis_version_select ON diagnosis_version FOR SELECT TO kalypso_app
    USING (app_patient_visible(patient_id));

-- --- филиалы пациента (регистрация — триггером, связывание — куратором) -----
ALTER TABLE patient_branch ENABLE ROW LEVEL SECURITY;
CREATE POLICY patient_branch_select ON patient_branch FOR SELECT TO kalypso_app
    USING (app_patient_visible(patient_id));
CREATE POLICY patient_branch_insert ON patient_branch FOR INSERT TO kalypso_app
    WITH CHECK (app_role() = 'curator');
CREATE POLICY patient_branch_delete ON patient_branch FOR DELETE TO kalypso_app
    USING (app_role() IN ('curator', 'admin'));

-- --- запросы межфилиального доступа -----------------------------------------
ALTER TABLE branch_access_request ENABLE ROW LEVEL SECURITY;
CREATE POLICY branch_access_request_select ON branch_access_request FOR SELECT TO kalypso_app
    USING (app_role() IN ('curator', 'admin') OR requested_by = app_user_id());
CREATE POLICY branch_access_request_insert ON branch_access_request FOR INSERT TO kalypso_app
    WITH CHECK (app_role() = 'physician' AND branch_code = app_branch()
                AND requested_by = app_user_id());
CREATE POLICY branch_access_request_update ON branch_access_request FOR UPDATE TO kalypso_app
    USING (app_role() = 'curator') WITH CHECK (app_role() = 'curator');

-- Представление читает таблицы с правами вызывающего — RLS действует и здесь.
ALTER VIEW v_survival SET (security_invoker = true);
