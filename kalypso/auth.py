"""Вход, сессии, пароли, CSRF. Каждая попытка входа пишется в login_log."""
from __future__ import annotations

import hashlib
import ipaddress
import hmac
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from kalypso.db import Database
from kalypso.rbac import Principal

_ph = PasswordHasher()
_DUMMY_HASH = _ph.hash("timing-equalizer")  # выравнивает время ответа для неизвестного логина

MIN_PASSWORD_LEN = 10
LOCKOUT_FAILURES = 5
LOCKOUT_WINDOW = timedelta(minutes=15)


class LoginFailed(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason  # bad_password | unknown_user | inactive | locked


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"Пароль должен быть не короче {MIN_PASSWORD_LEN} символов")
    return _ph.hash(password)


def _verify(password_hash: str, password: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def _token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii")).digest()


def _principal(row: dict) -> Principal:
    return Principal(user_id=row["user_id"], login=row["login"], display_name=row["display_name"],
                     role=row["role_code"], branch_code=row["branch_code"],
                     must_change_password=row["must_change_password"])


def _ip(value: str | None) -> str | None:
    """Адрес клиента для журнала; не-IP (имя хоста, прокси) -> None."""
    try:
        return str(ipaddress.ip_address(value)) if value else None
    except ValueError:
        return None


def _log_attempt(db: Database, login: str, user_id, success: bool, reason: str, ip, ua) -> None:
    with db.tx(user_id) as cur:
        cur.execute("""INSERT INTO login_log (event, login, user_id, success, reason, ip, user_agent)
                       VALUES ('login', %s, %s, %s, %s, %s, %s)""",
                    (login[:64], user_id, success, reason, ip, (ua or "")[:300]))


def _recent_failures(cur, login: str) -> int:
    cur.execute("""SELECT count(*) AS n FROM login_log
                   WHERE login = %s AND event = 'login' AND NOT success
                     AND ts > now() - %s
                     AND ts > coalesce((SELECT max(ts) FROM login_log
                                        WHERE login = %s AND success), '-infinity')""",
                (login, LOCKOUT_WINDOW, login))
    return cur.fetchone()["n"]


def login(db: Database, login_name: str, password: str, *, session_hours: int,
          ip: str | None = None, user_agent: str | None = None) -> tuple[str, Principal]:
    """Возвращает (токен сессии, пользователь) или бросает LoginFailed."""
    login_name = (login_name or "").strip().lower()
    ip = _ip(ip)
    with db.tx() as cur:
        cur.execute("SELECT * FROM app_user WHERE login = %s", (login_name,))
        user = cur.fetchone()
        failures = _recent_failures(cur, login_name)

    if user is None or user["role_code"] == "ai_worker":   # служебная роль не входит в веб
        _verify(_DUMMY_HASH, password)
        reason = "unknown_user"
    elif failures >= LOCKOUT_FAILURES:
        reason = "locked"
    elif not _verify(user["password_hash"], password):
        reason = "bad_password"
    elif not user["is_active"]:
        reason = "inactive"
    else:
        reason = "ok"

    uid = user["user_id"] if user else None
    _log_attempt(db, login_name, uid, reason == "ok", reason, ip, user_agent)
    if reason != "ok":
        raise LoginFailed(reason)

    token = secrets.token_urlsafe(32)
    with db.tx(uid) as cur:
        cur.execute("""INSERT INTO app_session (token_hash, user_id, expires_at, ip, user_agent)
                       VALUES (%s, %s, now() + %s, %s, %s)""",
                    (_token_hash(token), uid, timedelta(hours=session_hours), ip, (user_agent or "")[:300]))
    return token, _principal(user)


def principal_for_token(db: Database, token: str | None) -> Principal | None:
    if not token:
        return None
    with db.tx() as cur:
        cur.execute("""SELECT u.* FROM app_session s JOIN app_user u USING (user_id)
                       WHERE s.token_hash = %s AND s.revoked_at IS NULL
                         AND s.expires_at > now() AND u.is_active""", (_token_hash(token),))
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute("UPDATE app_session SET last_seen_at = now() WHERE token_hash = %s",
                    (_token_hash(token),))
    return _principal(row)


def logout(db: Database, token: str, principal: Principal) -> None:
    with db.tx(principal.user_id) as cur:
        cur.execute("UPDATE app_session SET revoked_at = now() WHERE token_hash = %s",
                    (_token_hash(token),))
        cur.execute("""INSERT INTO login_log (event, login, user_id, success, reason)
                       VALUES ('logout', %s, %s, true, 'ok')""", (principal.login, principal.user_id))


def change_password(db: Database, principal: Principal, old: str, new: str,
                    keep_token: str | None = None) -> None:
    with db.tx(principal.user_id) as cur:
        cur.execute("SELECT password_hash FROM app_user WHERE user_id = %s", (principal.user_id,))
        if not _verify(cur.fetchone()["password_hash"], old):
            raise ValueError("Текущий пароль неверен")
        if old == new:
            raise ValueError("Новый пароль должен отличаться от текущего")
        cur.execute("UPDATE app_user SET password_hash = %s, must_change_password = false "
                    "WHERE user_id = %s", (hash_password(new), principal.user_id))
        # Остальные сессии пользователя закрываются, текущая остаётся.
        keep = _token_hash(keep_token) if keep_token else b""
        cur.execute("UPDATE app_session SET revoked_at = now() WHERE user_id = %s "
                    "AND revoked_at IS NULL AND token_hash <> %s", (principal.user_id, keep))


def csrf_token(secret: bytes, session_token: str) -> str:
    return hmac.new(secret, b"csrf:" + session_token.encode("ascii"), hashlib.sha256).hexdigest()


def csrf_valid(secret: bytes, session_token: str | None, submitted: str | None) -> bool:
    if not session_token or not submitted:
        return False
    return hmac.compare_digest(csrf_token(secret, session_token), submitted)
