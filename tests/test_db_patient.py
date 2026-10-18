"""Пациент: идентификация, коды, умные поля, версионность диагноза (B1, I1, I3, I4, I6)."""
import pytest

from conftest import iin_hash, rejected


def test_patient_created_by_app_role(reg):
    pid = reg.patient("990000000001")
    reg.con.commit()
    assert reg.one("SELECT created_by FROM patient WHERE patient_id = %s", pid) == reg.doc
    # Филиал регистрации сразу получает доступ к пациенту.
    assert reg.all("SELECT branch_code FROM patient_branch WHERE patient_id = %s", pid) == [("synth_a",)]


@pytest.mark.parametrize("name, kw, match", [
    ("короткий хэш", {"iin_hash": "abc123"}, "iin_hash_format"),
    ("русский код пола", {"sex": "М"}, "sex_check"),
    ("русский статус", {"status_last_contact": "жив"}, "status_last_contact_check"),
    ("стадия Rai 'A'", {"stage_system": "Rai", "stage_value": "A"}, "stage_rai"),
    ("стадия Binet 'IV'", {"stage_system": "Binet", "stage_value": "IV"}, "stage_binet"),
    ("стадия без системы", {"stage_value": "B"}, "stage_pair"),
    ("индекс без балла", {"index_name": "CLL-IPI"}, "index_pair"),
    ("CLL-IPI балл 12", {"index_name": "CLL-IPI", "index_score": 12, "index_group": "high"}, "cll_ipi"),
    ("12 цифр в гистологии", {"histology_report": "ИИН 990000000099, ХЛЛ"}, "histology_no_iin"),
    ("последний контакт раньше диагноза", {"last_contact_date": "2020-01-01"}, "last_contact_after_dx"),
    ("рождение после диагноза", {"birth_date": "2022-01-01"}, "birth_before_dx"),
])
def test_patient_constraints(reg, name, kw, match):
    with rejected(reg.con, match):
        reg.patient("990000000002", **kw)


def test_valid_smart_fields_accepted(reg):
    reg.patient("990000000003", stage_system="Binet", stage_value="A",
                index_name="CLL-IPI", index_score=4, index_group="high",
                index_components='{"TP53": 0, "IGHV": 1, "b2m": 2, "stage": 1, "age": 0}',
                histology_report="ХЛЛ, CD5+ CD23+, Ki-67 10%")
    reg.con.commit()


def test_duplicate_iin_rejected_across_branches(reg):
    reg.patient("990000000004")
    reg.con.commit()
    with rejected(reg.con, "iin_hash_key"):
        reg.patient("990000000004", branch="synth_b")


def test_diagnosis_version_1_on_insert(reg):
    pid = reg.patient("990000000005")
    reg.con.commit()
    assert reg.one("SELECT dx_version FROM patient WHERE patient_id = %s", pid) == 1
    assert reg.all("SELECT version, change_reason FROM diagnosis_version WHERE patient_id = %s",
                   pid) == [(1, None)]


def test_diagnosis_revision_requires_reason(reg):
    pid = reg.patient("990000000006")
    reg.con.commit()
    with rejected(reg.con, "требует причины"):
        reg.as_app()
        reg.cur.execute("UPDATE patient SET mkb = 'C91.9' WHERE patient_id = %s", (pid,))


def test_diagnosis_revision_keeps_history(reg):
    pid = reg.patient("990000000007")
    reg.con.commit()
    reg.as_app(reason="пересмотр гистологии")
    reg.cur.execute("UPDATE patient SET mkb = 'C91.9', dx_version = 99 WHERE patient_id = %s", (pid,))
    reg.con.commit()
    assert reg.all("SELECT version, mkb, change_reason FROM diagnosis_version "
                   "WHERE patient_id = %s ORDER BY version", pid) == [
        (1, "C91.1", None), (2, "C91.9", "пересмотр гистологии")]
    # Номер версии ведёт БД: попытка выставить 99 проигнорирована.
    assert reg.one("SELECT dx_version FROM patient WHERE patient_id = %s", pid) == 2


def test_non_diagnosis_edit_keeps_version(reg):
    pid = reg.patient("990000000008")
    reg.con.commit()
    reg.as_app()
    reg.cur.execute("UPDATE patient SET ecog = 1 WHERE patient_id = %s", (pid,))
    reg.con.commit()
    assert reg.one("SELECT dx_version FROM patient WHERE patient_id = %s", pid) == 1
    assert reg.one("SELECT updated_by FROM patient WHERE patient_id = %s", pid) == reg.doc


def test_physician_requires_branch(reg):
    with rejected(reg.con, "physician_branch"):
        reg.cur.execute("INSERT INTO app_user (login, display_name, password_hash, role_code) "
                        "VALUES ('doc.x', 'X', 'x', 'physician')")


def test_unknown_branch_rejected(reg):
    # Мимо RLS (владелец): проверяется именно внешний ключ на справочник филиалов.
    with rejected(reg.con, "patient_branch_fkey"):
        reg.cur.execute("INSERT INTO patient (iin_hash, branch, sex, who_haem5, icd_o_3_2, mkb, dx_date, "
                        "last_contact_date, provenance, status_last_contact, source) VALUES "
                        "(%s, 'nowhere', 'M', 'cll_sll', '9823/3', 'C91.1', '2021-03-01', '2024-06-01', "
                        "'retrospective', 'alive', 'manual')", (iin_hash("990000000009"),))
