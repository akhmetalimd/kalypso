"""Команды эксплуатации.

    KALYPSO_MIGRATE_DSN=... python -m kalypso.cli migrate        # от владельца схемы
    KALYPSO_DSN=...         python -m kalypso.cli create-admin LOGIN "Имя"
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from kalypso import auth, migrate
from kalypso.db import Database


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="kalypso")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="применить недостающие миграции")
    ca = sub.add_parser("create-admin", help="первый администратор (пароль вводится скрыто)")
    ca.add_argument("login")
    ca.add_argument("display_name")
    args = ap.parse_args(argv)

    if args.cmd == "migrate":
        dsn = os.environ.get("KALYPSO_MIGRATE_DSN")
        if not dsn:
            print("Задайте KALYPSO_MIGRATE_DSN (владелец схемы)", file=sys.stderr)
            return 2
        done = migrate.apply_pending(dsn)
        print("Применено:", ", ".join(done) if done else "ничего — схема актуальна")
        return 0

    password = getpass.getpass("Пароль администратора: ")
    if password != getpass.getpass("Ещё раз: "):
        print("Пароли не совпадают", file=sys.stderr)
        return 1
    db = Database(os.environ["KALYPSO_DSN"])
    with db.tx() as cur:
        cur.execute("""INSERT INTO app_user (login, display_name, password_hash, role_code)
                       VALUES (%s, %s, %s, 'admin')""",
                    (args.login.lower(), args.display_name, auth.hash_password(password)))
    db.close()
    print("Администратор создан")
    return 0


if __name__ == "__main__":
    sys.exit(main())
