"""Применение schema.sql и миграций на чистой БД; защита 0002 от непустой БД."""
import psycopg
import pytest

from conftest import MIGRATIONS, SCHEMA, apply_sql


def test_schema_and_migrations_apply_on_empty_db(make_db):
    with make_db() as con:
        apply_sql(con, SCHEMA)
        for m in MIGRATIONS:
            apply_sql(con, m)
        con.commit()
        assert con.execute("SELECT count(*) FROM role").fetchone()[0] == 6   # 4 роли людей + ai_worker + ingest_worker


def test_0002_refuses_non_empty_db(make_db):
    with make_db() as con:
        apply_sql(con, SCHEMA)
        con.execute("""INSERT INTO patient (patient_id, branch, sex, who_haem5, icd_o_3_2, mkb,
                           dx_date, last_contact_date, provenance, status_last_contact)
                       VALUES ('abc', 'synth_a', 'М', 'cll_sll', '9823/3', 'C91.1',
                               '2021-01-01', '2022-01-01', 'retrospective', 'жив')""")
        con.commit()
        with pytest.raises(psycopg.Error, match="рассчитана на пустую БД"):
            apply_sql(con, MIGRATIONS[0])
