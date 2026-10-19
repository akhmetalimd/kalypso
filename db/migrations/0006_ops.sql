-- ============================================================================
--  KALYPSO — миграция 0006: эксплуатация
-- ============================================================================
-- `python -m kalypso.cli check` от логина приложения сверяет применённые миграции.
-- schema_migrations создаёт kalypso/migrate.py до первой миграции; при ручном
-- применении файлов (psql -f, тесты миграций) её может не быть — тогда пропускаем.
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        GRANT SELECT ON schema_migrations TO kalypso_app;
    END IF;
END
$$;
