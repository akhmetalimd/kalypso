"""Аудит-трейл, права роли приложения, неизменяемость журналов (B5, B6, B7)."""
import pytest

from conftest import rejected


def test_insert_audited_with_app_user(reg):
    pid = reg.patient("990000000101")
    reg.con.commit()
    assert reg.one("SELECT actor FROM audit_log WHERE table_name = 'patient' "
                   "AND action = 'insert' AND row_id = %s", str(pid)) == str(reg.doc)


def test_update_audited_per_field(reg):
    pid = reg.patient("990000000102")
    reg.con.commit()
    reg.as_app()
    reg.cur.execute("UPDATE patient SET ecog = 1 WHERE patient_id = %s", (pid,))
    reg.con.commit()
    rows = reg.all("SELECT field, old_value, new_value FROM audit_log "
                   "WHERE table_name = 'patient' AND action = 'update' AND row_id = %s", str(pid))
    assert rows == [("ecog", None, "1")]  # updated_at / updated_by в журнал не шумят


def test_revision_reason_in_audit(reg):
    pid = reg.patient("990000000103")
    reg.con.commit()
    reg.as_app(reason="пересмотр по WHO5")
    reg.cur.execute("UPDATE patient SET mkb = 'C91.9' WHERE patient_id = %s", (pid,))
    reg.con.commit()
    assert reg.one("SELECT reason FROM audit_log WHERE field = 'mkb'") == "пересмотр по WHO5"


def test_edit_outside_app_signed_db_role(reg):
    pid = reg.patient("990000000104")
    reg.con.commit()
    reg.cur.execute("UPDATE patient SET ecog = 2 WHERE patient_id = %s", (pid,))
    reg.con.commit()
    assert reg.one("SELECT actor FROM audit_log WHERE field = 'ecog'").startswith("db:")


def test_password_masked_in_audit(reg):
    reg.as_app(reg.admin)
    reg.cur.execute("UPDATE app_user SET password_hash = 'new' WHERE user_id = %s", (reg.doc,))
    reg.con.commit()
    assert reg.one("SELECT old_value, new_value FROM audit_log WHERE field = 'password_hash'") == ("***", "***")
    assert reg.one("SELECT count(*) FROM audit_log WHERE table_name = 'app_user' "
                   "AND new_value LIKE '%%password_hash%%'") == 0


@pytest.mark.parametrize("name, sql", [
    ("удалить пациента", "DELETE FROM patient"),
    ("удалить линию", "DELETE FROM therapy_line"),
    ("удалить лабораторный", "DELETE FROM lab"),
    ("писать в audit_log", "INSERT INTO audit_log (actor, table_name, row_id, action) VALUES ('x', 'x', 'x', 'insert')"),
    ("править diagnosis_version", "UPDATE diagnosis_version SET mkb = 'X'"),
    ("писать в diagnosis_version", "INSERT INTO diagnosis_version SELECT * FROM diagnosis_version"),
    ("править login_log", "UPDATE login_log SET success = true"),
])
def test_app_role_denied(reg, name, sql):
    with rejected(reg.con, "permission denied"):
        reg.as_app()
        reg.cur.execute(sql)


@pytest.mark.parametrize("table", ["audit_log", "diagnosis_version", "login_log"])
@pytest.mark.parametrize("sql", ["UPDATE {t} SET {c} = {c}", "DELETE FROM {t}", "TRUNCATE {t}"])
def test_journals_immutable_even_for_owner(reg, table, sql):
    # Строковые триггеры срабатывают только на существующих строках — наполняем журналы.
    reg.patient("990000000105")
    reg.cur.execute("INSERT INTO login_log (event, login, success, reason) "
                    "VALUES ('login', 'doc.a', true, 'ok')")
    reg.con.commit()
    col = {"audit_log": "actor", "diagnosis_version": "mkb", "login_log": "login"}[table]
    with rejected(reg.con, "неизменяема"):
        reg.cur.execute(sql.format(t=table, c=col))


def test_owner_hard_delete_blocked_by_restrict(reg):
    pid = reg.patient("990000000106")
    reg.con.commit()
    with rejected(reg.con, "foreign key"):
        reg.cur.execute("DELETE FROM patient WHERE patient_id = %s", (pid,))


def test_app_writes_login_log(reg):
    reg.as_app()
    reg.cur.execute("INSERT INTO login_log (event, login, success, reason) "
                    "VALUES ('login', 'doc.a', false, 'bad_password')")
    reg.con.commit()
    assert reg.one("SELECT count(*) FROM login_log") == 1
