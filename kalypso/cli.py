"""Команды эксплуатации.

    KALYPSO_MIGRATE_DSN=... python -m kalypso.cli migrate          # от владельца схемы
    KALYPSO_DSN=...         python -m kalypso.cli check            # готовность перед запуском
    KALYPSO_DSN=...         python -m kalypso.cli serve            # сервер (за nginx)
    KALYPSO_DSN=...         python -m kalypso.cli create-admin LOGIN "Имя"
    KALYPSO_DSN=...         python -m kalypso.cli seed-synthetic   # синтетика для обучения

Переменные сервера (kalypso/config.py): KALYPSO_DSN, KALYPSO_SESSION_SECRET,
KALYPSO_SECURE_COOKIES, KALYPSO_SESSION_HOURS; для serve ещё KALYPSO_HOST (127.0.0.1),
KALYPSO_PORT (8000), KALYPSO_FORWARDED_ALLOW_IPS (127.0.0.1) — адреса прокси, которым
верим в X-Forwarded-For/Proto.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from kalypso import auth, migrate


def _serve(env) -> int:
    import uvicorn

    from kalypso import logsafe
    from kalypso.config import load_settings
    from kalypso.web.app import create_app

    settings = load_settings(env)
    if not settings.secure_cookies:
        print("ВНИМАНИЕ: KALYPSO_SECURE_COOKIES=0 — только для локальной проверки", file=sys.stderr)
    uvicorn.run(create_app(settings), host=env.get("KALYPSO_HOST", "127.0.0.1"),
                port=int(env.get("KALYPSO_PORT", "8000")), proxy_headers=True,
                forwarded_allow_ips=env.get("KALYPSO_FORWARDED_ALLOW_IPS", "127.0.0.1"),
                log_config=logsafe.log_config(), server_header=False)
    return 0


def _check(env) -> int:
    from kalypso.config import ConfigError, load_settings
    from kalypso.ops import run_checks

    try:
        problems = run_checks(load_settings(env))
    except ConfigError as e:
        problems = [str(e)]
    for p in problems:
        print("ПРОБЛЕМА:", p, file=sys.stderr)
    if not problems:
        print("Готово к запуску: настройки, роль входа и миграции в порядке")
    return 1 if problems else 0


def _seed(env, cll: int, dlbcl: int) -> int:
    from kalypso.db import Database
    from kalypso.ops import OpsError, seed_synthetic

    db = Database(env["KALYPSO_DSN"])
    try:
        print("Синтетика загружена:", seed_synthetic(db, cll, dlbcl))
    except OpsError as e:
        print(e, file=sys.stderr)
        return 1
    finally:
        db.close()
    return 0


def _create_admin(env, login: str, display_name: str) -> int:
    from kalypso.db import Database

    password = getpass.getpass("Пароль администратора: ")
    if password != getpass.getpass("Ещё раз: "):
        print("Пароли не совпадают", file=sys.stderr)
        return 1
    db = Database(env["KALYPSO_DSN"])
    try:
        with db.tx() as cur:
            cur.execute("""INSERT INTO app_user (login, display_name, password_hash, role_code)
                           VALUES (%s, %s, %s, 'admin')""",
                        (login.lower(), display_name, auth.hash_password(password)))
    finally:
        db.close()
    print("Администратор создан")
    return 0


def main(argv=None, env=None) -> int:
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(prog="kalypso")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="применить недостающие миграции (KALYPSO_MIGRATE_DSN)")
    sub.add_parser("check", help="проверить настройки, роль входа и миграции")
    sub.add_parser("serve", help="запустить веб-сервер")
    ca = sub.add_parser("create-admin", help="первый администратор (пароль вводится скрыто)")
    ca.add_argument("login")
    ca.add_argument("display_name")
    sd = sub.add_parser("seed-synthetic", help="синтетические пациенты для обучения (только пустой регистр)")
    sd.add_argument("--cll", type=int, default=60)
    sd.add_argument("--dlbcl", type=int, default=40)
    args = ap.parse_args(argv)

    if args.cmd == "migrate":
        dsn = env.get("KALYPSO_MIGRATE_DSN")
        if not dsn:
            print("Задайте KALYPSO_MIGRATE_DSN (владелец схемы)", file=sys.stderr)
            return 2
        done = migrate.apply_pending(dsn)
        print("Применено:", ", ".join(done) if done else "ничего — схема актуальна")
        return 0
    if args.cmd == "check":
        return _check(env)
    if args.cmd == "serve":
        return _serve(env)
    if args.cmd == "seed-synthetic":
        return _seed(env, args.cll, args.dlbcl)
    return _create_admin(env, args.login, args.display_name)


if __name__ == "__main__":
    sys.exit(main())
