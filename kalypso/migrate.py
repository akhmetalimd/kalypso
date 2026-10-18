"""Применение схемы: schema.sql (baseline 0001) + db/migrations/NNNN_*.sql по порядку.

Учёт ведётся в schema_migrations; каждая миграция — в своей транзакции.
Запуск: python -m kalypso.cli migrate
"""
from __future__ import annotations

from pathlib import Path

import psycopg

REPO = Path(__file__).resolve().parent.parent
SCHEMA = REPO / "schema.sql"
MIGRATIONS_DIR = REPO / "db" / "migrations"
BASELINE = "0001_baseline"


def migration_files() -> list[tuple[str, Path]]:
    files = [(BASELINE, SCHEMA)]
    files += [(p.stem, p) for p in sorted(MIGRATIONS_DIR.glob("*.sql"))]
    return files


def read_sql(con: psycopg.Connection, path: Path) -> str:
    sql = path.read_text(encoding="utf-8")
    if path == SCHEMA:
        # В PG 13+ gen_random_uuid() встроен; pgcrypto подключаем, только если он есть
        # (во встроенном pgserver contrib не собран).
        has = con.execute(
            "SELECT 1 FROM pg_available_extensions WHERE name = 'pgcrypto'").fetchone()
        if not has:
            sql = sql.replace("CREATE EXTENSION IF NOT EXISTS pgcrypto;", "")
    return sql


def applied(con: psycopg.Connection) -> set[str]:
    con.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                       version    TEXT PRIMARY KEY,
                       applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    con.commit()
    return {r[0] for r in con.execute("SELECT version FROM schema_migrations")}


def apply_pending(dsn: str) -> list[str]:
    """Применяет недостающие миграции; возвращает их версии."""
    done: list[str] = []
    with psycopg.connect(dsn) as con:
        have = applied(con)
        for version, path in migration_files():
            if version in have:
                continue
            with con.transaction():
                con.execute(read_sql(con, path))
                con.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
            done.append(version)
    return done
