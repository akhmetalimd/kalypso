"""Общие фикстуры: PostgreSQL с применёнными миграциями и помощник для синтетики.

Сервер: KALYPSO_TEST_DSN (суперпользователь или роль с CREATEDB + CREATEROLE,
база postgres) либо встроенный PostgreSQL из pgserver. Миграции применяются один
раз в шаблонную БД; каждый тест получает свою копию (CREATE DATABASE … TEMPLATE).

Только синтетика: «ИИН» здесь — произвольные 12 цифр, ключ HMAC — тестовый.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SCHEMA = REPO / "schema.sql"
MIGRATIONS = sorted((REPO / "db" / "migrations").glob("*.sql"))
TEST_HMAC_KEY = b"kalypso-synthetic-test-key"


def iin_hash(iin: str) -> str:
    return hmac.new(TEST_HMAC_KEY, iin.encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Сервер и базы
# ---------------------------------------------------------------------------
class PgCluster:
    def __init__(self, tmp_dir: Path):
        dsn = os.environ.get("KALYPSO_TEST_DSN")
        self._srv = None
        if dsn:
            self._base = dsn
        else:
            import pgserver
            self._srv = pgserver.get_server(tmp_dir, cleanup_mode="stop")
            self._base = self._srv.get_uri()

    def dsn(self, dbname: str = "postgres") -> str:
        return make_conninfo(self._base, dbname=dbname)

    def admin(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn(), autocommit=True)

    def create_db(self, name: str, template: str | None = None) -> None:
        with self.admin() as con:
            tpl = f" TEMPLATE {template}" if template else ""
            con.execute(f"CREATE DATABASE {name}{tpl}")

    def drop_db(self, name: str) -> None:
        with self.admin() as con:
            con.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")

    def stop(self) -> None:
        if self._srv is not None:
            self._srv.cleanup()


def apply_sql(con: psycopg.Connection, path: Path) -> None:
    sql = path.read_text(encoding="utf-8")
    if path == SCHEMA:
        # В PG 13+ gen_random_uuid() встроен; pgcrypto нужен только если он есть
        # (во встроенном pgserver contrib не собран).
        has = con.execute(
            "SELECT 1 FROM pg_available_extensions WHERE name = 'pgcrypto'").fetchone()
        if not has:
            sql = sql.replace("CREATE EXTENSION IF NOT EXISTS pgcrypto;", "")
    con.execute(sql)


@pytest.fixture(scope="session")
def pg(tmp_path_factory) -> PgCluster:
    cluster = PgCluster(tmp_path_factory.mktemp("pgdata"))
    yield cluster
    cluster.stop()


@pytest.fixture(scope="session")
def template_db(pg: PgCluster) -> str:
    name = f"kalypso_tpl_{os.getpid()}"
    pg.drop_db(name)
    pg.create_db(name)
    with psycopg.connect(pg.dsn(name)) as con:
        apply_sql(con, SCHEMA)
        for m in MIGRATIONS:
            apply_sql(con, m)
    yield name
    pg.drop_db(name)


@pytest.fixture
def make_db(pg: PgCluster):
    """Фабрика пустых БД для тестов самих миграций."""
    created = []

    def _make() -> psycopg.Connection:
        name = f"t_{uuid.uuid4().hex[:12]}"
        pg.create_db(name)
        created.append(name)
        return psycopg.connect(pg.dsn(name))

    yield _make
    for name in created:
        pg.drop_db(name)


@pytest.fixture
def conn(pg: PgCluster, template_db: str) -> psycopg.Connection:
    name = f"t_{uuid.uuid4().hex[:12]}"
    pg.create_db(name, template=template_db)
    con = psycopg.connect(pg.dsn(name))
    yield con
    con.close()
    pg.drop_db(name)


@contextmanager
def rejected(con: psycopg.Connection, match: str):
    """Ожидает отказ БД (в том числе отложенный — на commit), затем откатывает."""
    with pytest.raises(psycopg.Error, match=match):
        yield
        con.commit()
    con.rollback()


# ---------------------------------------------------------------------------
# Помощник: синтетические филиалы, пользователи и записи
# ---------------------------------------------------------------------------
class Registry:
    PATIENT = dict(branch="synth_a", sex="M", who_haem5="cll_sll", icd_o_3_2="9823/3",
                   mkb="C91.1", dx_date="2021-03-01", last_contact_date="2024-06-01",
                   provenance="retrospective", status_last_contact="alive", source="manual")

    def __init__(self, con: psycopg.Connection):
        self.con = con
        self.cur = con.cursor()
        self.cur.execute("INSERT INTO branch (branch_code, name_ru) VALUES "
                         "('synth_a', 'Синтетический филиал A'), ('synth_b', 'Синтетический филиал B')")
        self.admin = self.one("INSERT INTO app_user (login, display_name, password_hash, role_code) "
                              "VALUES ('admin', 'Админ', 'x', 'admin') RETURNING user_id")
        self.doc = self.one("INSERT INTO app_user (login, display_name, password_hash, role_code, branch_code) "
                            "VALUES ('doc.a', 'Врач A', 'x', 'physician', 'synth_a') RETURNING user_id")
        con.commit()

    def one(self, sql: str, *params):
        row = self.cur.execute(sql, params).fetchone()
        return None if row is None else (row[0] if len(row) == 1 else row)

    def all(self, sql: str, *params) -> list:
        return self.cur.execute(sql, params).fetchall()

    def as_app(self, user=None, reason: str | None = None) -> None:
        """Роль приложения до конца транзакции + автор правки (и причина)."""
        self.cur.execute("SET LOCAL ROLE kalypso_app")
        self.cur.execute("SELECT set_config('app.user_id', %s, true)", (str(user or self.doc),))
        if reason:
            self.cur.execute("SELECT set_config('app.change_reason', %s, true)", (reason,))

    def _insert(self, table: str, row: dict, returning: str):
        cols = ", ".join(row)
        ph = ", ".join(["%s"] * len(row))
        return self.one(f"INSERT INTO {table} ({cols}) VALUES ({ph}) RETURNING {returning}",
                        *row.values())

    def patient(self, iin: str, **kw):
        self.as_app()
        return self._insert("patient", {**self.PATIENT, "iin_hash": iin_hash(iin), **kw}, "patient_id")

    def line(self, pid, line_no: int = 1, **kw):
        self.as_app()
        row = dict(patient_id=pid, line_no=line_no, source="manual", provenance="retrospective")
        return self._insert("therapy_line", {**row, **kw}, "line_id")

    def obs(self, pid, obs_date: str = "2021-11-01", **kw):
        self.as_app()
        row = dict(patient_id=pid, obs_date=obs_date, source="manual", provenance="retrospective")
        return self._insert("observation", {**row, **kw}, "obs_id")

    def marker(self, pid, key: str, value=None, missing=None, test_date=None):
        self.as_app()
        row = dict(patient_id=pid, module="cll", field_key=key, value=value, missing=missing,
                   test_date=test_date, test_date_precision="day" if test_date else None,
                   source="manual", provenance="retrospective")
        return self._insert("module_data", row, "md_id")

    def lab(self, pid, **kw):
        self.as_app()
        row = dict(patient_id=pid, obs_date="2021-03-01", source="manual", provenance="retrospective")
        return self._insert("lab", {**row, **kw}, "lab_id")


@pytest.fixture
def reg(conn) -> Registry:
    return Registry(conn)
