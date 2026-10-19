"""Локальный запуск на синтетике: встроенный PostgreSQL + миграции + seed + веб.

    python -m kalypso.dev            # http://127.0.0.1:8765
    PORT=9000 python -m kalypso.dev

Данные и секреты dev-окружения лежат в .devdb/ (в git не попадает).
Учётные записи — kalypso/dev_seed.json. Удалить .devdb/ = начать с чистой БД.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
from pathlib import Path

import psycopg

from kalypso import auth, migrate, synthetic
from kalypso.config import Settings
from kalypso.db import Database

REPO = Path(__file__).resolve().parent.parent
DEVDIR = REPO / ".devdb"
SEED = Path(__file__).resolve().parent / "dev_seed.json"


def _dev_secrets() -> dict:
    path = DEVDIR / "dev_secrets.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if "session_secret" not in data:
        data["session_secret"] = secrets.token_hex(32)
        path.write_text(json.dumps(data), encoding="utf-8")
    return data


def seed_if_empty(db: Database) -> dict | None:
    """Пустая БД — филиалы, пользователи и когорта ХЛЛ; нет пациентов ДВКЛ — когорта ДВКЛ
    (так dev-БД, созданная до модуля ДВКЛ, получает её без пересоздания)."""
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    summary = {}
    with db.tx() as cur:
        cur.execute("SELECT count(*) AS n FROM app_user")
        empty = cur.fetchone()["n"] == 0
    if empty:
        with db.tx() as cur:
            for b in seed["branches"]:
                cur.execute("INSERT INTO branch (branch_code, name_ru) VALUES (%s, %s)",
                            (b["branch_code"], b["name_ru"]))
            for u in seed["users"]:
                cur.execute("""INSERT INTO app_user (login, display_name, password_hash, role_code,
                                                     branch_code, must_change_password)
                               VALUES (%s, %s, %s, %s, %s, false)""",
                            (u["login"], u["display_name"], auth.hash_password(u["password"]),
                             u["role_code"], u["branch_code"]))
    with db.tx() as cur:
        cur.execute("SELECT * FROM app_user WHERE role_code IN ('physician', 'curator') ORDER BY login")
        users = [auth._principal(r) for r in cur.fetchall()]
    physicians = [u for u in users if u.role == "physician"]
    curator = next(u for u in users if u.role == "curator")
    if empty:
        summary["cll_sll"] = synthetic.generate(db, physicians, n=seed["synthetic_patients"])
    with db.tx(curator.user_id) as cur:     # куратор видит все филиалы (RLS)
        cur.execute("SELECT count(*) AS n FROM patient WHERE who_haem5 = 'dlbcl'")
        no_dlbcl = cur.fetchone()["n"] == 0
    if no_dlbcl:
        summary["dlbcl"] = synthetic.generate(db, physicians, n=seed.get("synthetic_patients_dlbcl", 100),
                                              seed=7, duplicates=2, nosology="dlbcl")
    for part in summary.values():
        part.pop("iins")  # синтетические ИИН в консоль не печатаем
    return summary or None


def main() -> None:
    import pgserver
    import uvicorn
    from kalypso.web.app import create_app

    DEVDIR.mkdir(exist_ok=True)
    sec = _dev_secrets()
    try:
        srv = pgserver.get_server(DEVDIR / "pg", cleanup_mode="stop")
    except subprocess.TimeoutExpired:
        # После аварийной остановки PostgreSQL восстанавливается дольше таймаута pgserver (10 с).
        print("PostgreSQL восстанавливается после аварийной остановки — повторная попытка…")
        srv = pgserver.get_server(DEVDIR / "pg", cleanup_mode="stop")
    with psycopg.connect(srv.get_uri(), autocommit=True) as con:
        if not con.execute("SELECT 1 FROM pg_database WHERE datname = 'kalypso_dev'").fetchone():
            con.execute("CREATE DATABASE kalypso_dev")
    dsn = srv.get_uri("kalypso_dev")
    done = migrate.apply_pending(dsn)
    if done:
        print("Применены миграции:", ", ".join(done))

    settings = Settings(dsn=dsn, session_secret=sec["session_secret"].encode(), secure_cookies=False)
    db = Database(dsn)
    summary = seed_if_empty(db)
    if summary:
        print("Синтетика:", summary, "· учётные записи: kalypso/dev_seed.json")
    uvicorn.run(create_app(settings, db), host="127.0.0.1", port=int(os.environ.get("PORT", "8765")))


if __name__ == "__main__":
    main()
