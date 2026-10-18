"""Вход, блокировка, сессии, смена пароля, CSRF, журнал входов."""
import pytest

from conftest import TEST_PASSWORD
from kalypso import auth


def _login(w, login="doc.a", password=TEST_PASSWORD):
    return auth.login(w.db, login, password, session_hours=1, ip="127.0.0.1", user_agent="pytest")


def _log(w):
    with w.db.tx() as cur:
        cur.execute("SELECT login, success, reason FROM login_log ORDER BY login_id")
        return [tuple(r.values()) for r in cur.fetchall()]


def test_login_creates_session(world):
    token, p = _login(world)
    assert p.login == "doc.a" and p.branch_code == "synth_a"
    assert auth.principal_for_token(world.db, token).user_id == p.user_id
    assert _log(world) == [("doc.a", True, "ok")]
    with world.db.tx() as cur:  # в БД — только хэш токена
        cur.execute("SELECT count(*) AS n FROM app_session WHERE token_hash = %s", (token.encode(),))
        assert cur.fetchone()["n"] == 0


@pytest.mark.parametrize("login, password, reason", [
    ("doc.a", "wrong-password", "bad_password"),
    ("nobody", TEST_PASSWORD, "unknown_user"),
])
def test_failed_login_logged(world, login, password, reason):
    with pytest.raises(auth.LoginFailed) as e:
        _login(world, login, password)
    assert e.value.reason == reason
    assert _log(world) == [(login, False, reason)]


def test_inactive_user_cannot_login(world):
    with world.db.tx() as cur:
        cur.execute("UPDATE app_user SET is_active = false WHERE login = 'doc.b'")
    with pytest.raises(auth.LoginFailed, match="inactive"):
        _login(world, "doc.b")


def test_lockout_after_failures(world):
    for _ in range(auth.LOCKOUT_FAILURES):
        with pytest.raises(auth.LoginFailed):
            _login(world, password="wrong-password")
    with pytest.raises(auth.LoginFailed) as e:
        _login(world)  # даже с верным паролем
    assert e.value.reason == "locked"


def test_logout_revokes_session(world):
    token, p = _login(world)
    auth.logout(world.db, token, p)
    assert auth.principal_for_token(world.db, token) is None
    assert _log(world)[-1] == ("doc.a", True, "ok")


def test_change_password_keeps_current_session_only(world):
    t1, p = _login(world)
    t2, _ = _login(world)
    auth.change_password(world.db, p, TEST_PASSWORD, "new-secret-password", keep_token=t1)
    assert auth.principal_for_token(world.db, t1) is not None
    assert auth.principal_for_token(world.db, t2) is None
    _login(world, password="new-secret-password")
    with pytest.raises(ValueError):
        auth.change_password(world.db, p, "wrong", "another-password-1")


def test_password_policy():
    with pytest.raises(ValueError):
        auth.hash_password("short")


def test_csrf():
    tok = auth.csrf_token(b"s" * 32, "session-token")
    assert auth.csrf_valid(b"s" * 32, "session-token", tok)
    assert not auth.csrf_valid(b"s" * 32, "other-session", tok)
    assert not auth.csrf_valid(b"s" * 32, "session-token", None)
