"""Репетиция развёртывания на чистом кластере PostgreSQL (как при первом `docker compose up`).

Проверяем то, что исполняет сервер, без Docker: SQL первичной настройки из
deploy/postgres/init/10-kalypso.sh, миграции от владельца схемы (не суперпользователь),
check от логина приложения, синтетика, затем копия pg_dump -Fc и восстановление
pg_restore во временную БД — как в deploy/scripts/backup.sh и restore-check.sh.
Сами Docker, nginx и systemd здесь не проверяются (см. docs/deploy.md, «Первый запуск»).
"""
import re
import subprocess
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from conftest import REPO
from kalypso import auth, migrate, ops
from kalypso.config import Settings
from kalypso.db import Database

INIT = REPO / "deploy" / "postgres" / "init" / "10-kalypso.sh"


def _bin(name: str) -> str:
    import pgserver
    exe = Path(pgserver.__file__).parent / "pginstall" / "bin" / f"{name}.exe"
    return str(exe if exe.exists() else exe.with_suffix(""))


def _cli_args(dsn: str) -> list[str]:
    p = conninfo_to_dict(dsn)
    return ["-h", p.get("host", "127.0.0.1"), "-p", str(p.get("port", 5432)), "-U", p.get("user", "postgres")]


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    """Отдельный кластер: роли kalypso_* создаются «с нуля», как в новом контейнере."""
    import os
    if os.environ.get("KALYPSO_TEST_DSN"):
        pytest.skip("репетиция развёртывания — только на встроенном PostgreSQL")
    import pgserver
    srv = pgserver.get_server(tmp_path_factory.mktemp("deploy_pg"), cleanup_mode="stop")
    yield srv.get_uri()
    srv.cleanup()


def test_fresh_deploy_rehearsal(cluster, tmp_path):
    # 1. Первичная настройка — тот же SQL, что выполняет образ postgres.
    sql = re.search(r"<<'SQL'\n(.*?)\nSQL", INIT.read_text(encoding="utf-8"), re.S).group(1)
    subprocess.run([_bin("psql"), *_cli_args(cluster), "-d", "postgres", "-v", "ON_ERROR_STOP=1",
                    "-v", "owner_pw=owner-test-pw", "-v", "web_pw=web-test-pw"],
                   input=sql, text=True, check=True, capture_output=True)
    owner_dsn = make_conninfo(cluster, user="kalypso_owner", dbname="kalypso")
    web_dsn = make_conninfo(cluster, user="kalypso_web", dbname="kalypso")

    # 2. Миграции от владельца схемы — без прав суперпользователя.
    with psycopg.connect(owner_dsn) as con:
        assert con.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0] is False
    assert migrate.apply_pending(owner_dsn) == [v for v, _ in migrate.migration_files()]
    assert migrate.apply_pending(owner_dsn) == []                       # повторный запуск — ничего

    # 3. check от логина приложения — ни одной проблемы.
    secret = b"x" * 32
    assert ops.run_checks(Settings(dsn=web_dsn, session_secret=secret)) == []
    assert ops.run_checks(Settings(dsn=owner_dsn, session_secret=secret)) == [
        "Логин приложения «kalypso_owner» владеет таблицами: владелец не подчиняется RLS",
        "Логин приложения «kalypso_owner» не входит в роль kalypso_app"]

    # 4. Первые пользователи (как create-admin и раздел «Пользователи») и синтетика.
    db = Database(web_dsn, min_size=1, max_size=2)
    try:
        with db.tx() as cur:
            cur.execute("INSERT INTO branch (branch_code, name_ru) VALUES ('synth_a', 'Синт. A')")
            for login, role, branch in [("admin", "admin", None), ("curator", "curator", None),
                                        ("doc.a", "physician", "synth_a")]:
                cur.execute("""INSERT INTO app_user (login, display_name, password_hash, role_code, branch_code)
                               VALUES (%s, %s, %s, %s, %s)""",
                            (login, login, auth.hash_password("rehearsal-password"), role, branch))
        summary = ops.seed_synthetic(db, cll=5, dlbcl=3)
        assert summary["cll_sll"]["patients"] == 5 and summary["dlbcl"]["patients"] == 3
        with db.tx() as cur:                       # без пользователя RLS не показывает пациентов
            cur.execute("SELECT count(*) AS n FROM patient")
            assert cur.fetchone()["n"] == 0
    finally:
        db.close()

    # 5. Резервная копия и восстановление во временную БД.
    dump = tmp_path / "kalypso.dump"
    admin = _cli_args(cluster)
    subprocess.run([_bin("pg_dump"), *admin, "-d", "kalypso", "--format=custom", "-f", str(dump)],
                   check=True, capture_output=True)
    subprocess.run([_bin("createdb"), *admin, "-O", "kalypso_owner", "kalypso_restore_check"],
                   check=True, capture_output=True)
    subprocess.run([_bin("pg_restore"), *admin, "-d", "kalypso_restore_check", "--exit-on-error", str(dump)],
                   check=True, capture_output=True)
    counts = "SELECT (SELECT count(*) FROM patient), (SELECT count(*) FROM audit_log), (SELECT count(*) FROM schema_migrations)"
    with psycopg.connect(make_conninfo(cluster, dbname="kalypso")) as live, \
            psycopg.connect(make_conninfo(cluster, dbname="kalypso_restore_check")) as restored:
        assert restored.execute(counts).fetchone() == live.execute(counts).fetchone()
        assert live.execute(counts).fetchone()[0] == 8
