"""Администрирование: пользователи, филиалы, аудит, запросы межфилиального доступа."""
from __future__ import annotations

from kalypso import auth
from kalypso.db import Database
from kalypso.rbac import MATRIX, NotFound, Principal
from kalypso.registry import Form, ValidationError, _db_call


# --- филиалы ----------------------------------------------------------------
def list_branches(db: Database, p: Principal) -> list[dict]:
    with db.tx(p.user_id) as cur:
        cur.execute("SELECT * FROM branch ORDER BY branch_code")
        return cur.fetchall()


def create_branch(db: Database, p: Principal, data: dict) -> None:
    p.require("users.manage")
    f = Form(data)
    code = (f.text("branch_code", "Код филиала", required=True, maxlen=32) or "").lower()
    name = f.text("name_ru", "Название", required=True, maxlen=200)
    f.check()

    def write():
        with db.tx(p.user_id) as cur:
            cur.execute("INSERT INTO branch (branch_code, name_ru, name_kk) VALUES (%s, %s, %s)",
                        (code, name, f.text("name_kk", "Название (каз.)", maxlen=200)))
    _db_call(write)


# --- пользователи -----------------------------------------------------------
def list_users(db: Database, p: Principal) -> list[dict]:
    p.require("users.manage")
    with db.tx(p.user_id) as cur:
        cur.execute("""SELECT u.user_id, u.login, u.display_name, u.role_code, u.branch_code,
                              u.is_active, u.must_change_password, u.created_at,
                              (SELECT max(ts) FROM login_log l
                                WHERE l.user_id = u.user_id AND l.success AND l.event = 'login') AS last_login
                       FROM app_user u ORDER BY u.login""")
        return cur.fetchall()


def create_user(db: Database, p: Principal, data: dict) -> None:
    p.require("users.manage")
    f = Form(data)
    login = (f.text("login", "Логин", required=True, maxlen=64) or "").lower()
    name = f.text("display_name", "Имя", required=True, maxlen=200)
    role = f.choice("role_code", "Роль", list(MATRIX), required=True)
    branch = f.raw("branch_code")
    password = f.raw("password") or ""
    if role == "physician" and not branch:
        f.errors.append("Для врача обязателен филиал")
    try:
        pw_hash = auth.hash_password(password)
    except ValueError as e:
        f.errors.append(str(e))
    f.check()

    def write():
        with db.tx(p.user_id) as cur:
            cur.execute("""INSERT INTO app_user (login, display_name, password_hash, role_code, branch_code)
                           VALUES (%s, %s, %s, %s, %s)""", (login, name, pw_hash, role, branch or None))
    _db_call(write)


def set_active(db: Database, p: Principal, user_id, active: bool) -> None:
    p.require("users.manage")
    if str(user_id) == str(p.user_id) and not active:
        raise ValidationError(["Нельзя отключить собственную учётную запись"])
    with db.tx(p.user_id) as cur:
        cur.execute("UPDATE app_user SET is_active = %s WHERE user_id = %s", (active, user_id))
        if not active:
            cur.execute("UPDATE app_session SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
                        (user_id,))


def reset_password(db: Database, p: Principal, user_id, new_password: str) -> None:
    """Временный пароль; при входе пользователь обязан сменить его."""
    p.require("users.manage")
    try:
        pw_hash = auth.hash_password(new_password)
    except ValueError as e:
        raise ValidationError([str(e)]) from None
    with db.tx(p.user_id) as cur:
        cur.execute("UPDATE app_user SET password_hash = %s, must_change_password = true WHERE user_id = %s",
                    (pw_hash, user_id))
        cur.execute("UPDATE app_session SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
                    (user_id,))


# --- аудит ------------------------------------------------------------------
def list_audit(db: Database, p: Principal, table: str | None = None, row_id: str | None = None,
               limit: int = 300) -> list[dict]:
    p.require("audit.read")
    where, params = ["TRUE"], []
    if table:
        where.append("a.table_name = %s")
        params.append(table)
    if row_id:
        where.append("a.row_id = %s")
        params.append(row_id)
    with db.tx(p.user_id) as cur:
        cur.execute(f"""SELECT a.*, u.login AS actor_login FROM audit_log a
                        LEFT JOIN app_user u ON u.user_id::text = a.actor
                        WHERE {' AND '.join(where)} ORDER BY a.audit_id DESC LIMIT %s""", [*params, limit])
        return cur.fetchall()


def list_logins(db: Database, p: Principal, limit: int = 200) -> list[dict]:
    p.require("audit.read")
    with db.tx(p.user_id) as cur:
        cur.execute("SELECT * FROM login_log ORDER BY login_id DESC LIMIT %s", (limit,))
        return cur.fetchall()


# --- запросы межфилиального доступа ------------------------------------------
def list_requests(db: Database, p: Principal, status: str = "pending") -> list[dict]:
    p.require("access.decide")
    with db.tx(p.user_id) as cur:
        cur.execute("""SELECT r.*, u.login AS requested_by_login, pt.who_haem5, pt.dx_date, pt.branch AS home_branch,
                              (SELECT string_agg(pb.branch_code, ', ' ORDER BY pb.branch_code)
                                 FROM patient_branch pb WHERE pb.patient_id = r.patient_id) AS branches
                       FROM branch_access_request r
                       JOIN app_user u ON u.user_id = r.requested_by
                       JOIN patient pt ON pt.patient_id = r.patient_id
                       WHERE r.status = %s ORDER BY r.requested_at""", (status,))
        return cur.fetchall()


def decide_request(db: Database, p: Principal, request_id, approve: bool, note: str | None = None) -> None:
    p.require("access.decide")

    def write():
        with db.tx(p.user_id) as cur:
            cur.execute("""UPDATE branch_access_request
                           SET status = %s, decided_by = %s, decided_at = now(), decision_note = %s
                           WHERE request_id = %s AND status = 'pending'
                           RETURNING patient_id, branch_code""",
                        ("approved" if approve else "rejected", p.user_id, note or None, request_id))
            row = cur.fetchone()
            if row is None:
                raise NotFound()
            if approve:
                cur.execute("INSERT INTO patient_branch (patient_id, branch_code) VALUES (%s, %s) "
                            "ON CONFLICT DO NOTHING", (row["patient_id"], row["branch_code"]))
    _db_call(write)
