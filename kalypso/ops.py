"""Эксплуатация: проверка готовности сервера и загрузка синтетики для обучения.

Пилот идёт только на синтетике (CLAUDE.md, правило 1): seed_synthetic загружает
синтетические когорты ХЛЛ, ДВКЛ и ФЛ и только в пустой регистр.
"""
from __future__ import annotations

import psycopg

from kalypso import auth, migrate, synthetic
from kalypso.config import Settings
from kalypso.db import Database


class OpsError(RuntimeError):
    pass


def run_checks(settings: Settings) -> list[str]:
    """Список проблем (пусто — сервер готов): cookie, роль входа приложения, миграции.

    Логин приложения не должен обходить RLS: не суперпользователь, без BYPASSRLS
    и не владелец таблиц (владелец таблицы RLS не подчиняется)."""
    problems = []
    if not settings.secure_cookies:
        problems.append("KALYPSO_SECURE_COOKIES=0: cookie сессии уходят и по HTTP — только для локальной проверки")
    try:
        con = psycopg.connect(settings.dsn, connect_timeout=5)
    except psycopg.Error as e:
        return problems + [f"Нет подключения к БД по KALYPSO_DSN ({type(e).__name__})"]
    with con:
        role = con.execute("""SELECT current_user AS login, rolsuper, rolbypassrls,
                                     (SELECT tableowner FROM pg_tables
                                       WHERE schemaname = 'public' AND tablename = 'patient') AS owner,
                                     EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kalypso_app') AS app_exists
                              FROM pg_roles WHERE rolname = current_user""").fetchone()
        login, superuser, bypass, owner, app_exists = role
        if superuser:
            problems.append(f"Логин приложения «{login}» — суперпользователь: нужен отдельный логин в роли kalypso_app")
        if bypass:
            problems.append(f"Логин приложения «{login}» имеет BYPASSRLS")
        if owner == login:
            problems.append(f"Логин приложения «{login}» владеет таблицами: владелец не подчиняется RLS")
        if not app_exists:
            problems.append("Роли kalypso_app нет — миграции не применены")
        elif not con.execute("SELECT pg_has_role(current_user, 'kalypso_app', 'MEMBER')").fetchone()[0]:
            problems.append(f"Логин приложения «{login}» не входит в роль kalypso_app")
        try:
            have = {r[0] for r in con.execute("SELECT version FROM schema_migrations")}
        except psycopg.Error:
            con.rollback()
            problems.append("Не прочитать schema_migrations: миграции не применены или нет прав (миграция 0006)")
        else:
            missing = [v for v, _ in migrate.migration_files() if v not in have]
            if missing:
                problems.append("Не применены миграции: " + ", ".join(missing))
    return problems


def seed_synthetic(db: Database, cll: int = 60, dlbcl: int = 40, fl: int = 40) -> dict:
    """Синтетические пациенты для обучения врачей — только в пустой регистр.
    Нужны активные врачи (по ним распределяются пациенты) и куратор (видит все филиалы)."""
    with db.tx() as cur:
        cur.execute("""SELECT * FROM app_user WHERE is_active AND role_code IN ('physician', 'curator')
                       ORDER BY login""")
        users = [auth._principal(r) for r in cur.fetchall()]
    physicians = [u for u in users if u.role == "physician"]
    curator = next((u for u in users if u.role == "curator"), None)
    if not physicians or curator is None:
        raise OpsError("Нужны активные врачи и куратор: создайте филиалы и пользователей в разделе «Пользователи»")
    with db.tx(curator.user_id) as cur:
        cur.execute("SELECT count(*) AS n FROM patient")
        if cur.fetchone()["n"]:
            raise OpsError("В регистре уже есть пациенты — синтетика загружается только в пустой регистр")
    summary = {}
    for nosology, n, seed in (("cll_sll", cll, 42), ("dlbcl", dlbcl, 7), ("follicular", fl, 19)):
        if n > 0:
            part = synthetic.generate(db, physicians, n=n, seed=seed, duplicates=min(3, n // 10), nosology=nosology)
            part.pop("iins")  # синтетические ИИН не печатаем
            summary[nosology] = part
    return summary
