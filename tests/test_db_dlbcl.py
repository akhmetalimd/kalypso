"""Миграция 0005 (модуль ДВКЛ): стадия Lugano, IPI, ответ Lugano 2014 и балл Deauville."""
import pytest

from conftest import rejected

DLBCL = dict(who_haem5="dlbcl", icd_o_3_2="9680/3", mkb="C83.3")
LUGANO = dict(response_criteria="Lugano 2014")


@pytest.fixture
def pid(reg):
    pid = reg.patient("990000000501", **DLBCL, stage_system="Lugano", stage_value="IIE",
                      index_name="IPI", index_score=2, index_group="low_intermediate")
    reg.con.commit()
    return pid


@pytest.mark.parametrize("kw, match", [
    ({"stage_system": "Lugano", "stage_value": "B"}, "stage_lugano"),
    ({"stage_system": "Lugano", "stage_value": "IIIE"}, "stage_lugano"),
    ({"index_name": "IPI", "index_score": 6, "index_group": "high"}, "index_ipi"),
    ({"index_name": "IPI", "index_score": 2, "index_group": "intermediate"}, "index_ipi"),
])
def test_dlbcl_patient_constraints(reg, kw, match):
    with rejected(reg.con, match):
        reg.patient("990000000502", **DLBCL, **kw)


def test_dlbcl_markers_are_module_rows(reg, pid):
    reg.as_app()
    reg.cur.execute("""INSERT INTO module_data (patient_id, module, field_key, value, source, provenance)
                       VALUES (%s, 'dlbcl', 'coo_hans', 'GCB', 'manual', 'retrospective')""", (pid,))
    reg.con.commit()
    assert reg.one("SELECT value FROM module_data WHERE patient_id = %s AND module = 'dlbcl'", pid) == "GCB"


@pytest.mark.parametrize("kw", [
    {"response_value": "CMR", "response_basis": "pet_ct", "deauville": 3},
    {"response_value": "PMR", "response_basis": "pet_ct", "deauville": 4},
    {"response_value": "PMD", "response_basis": "pet_ct", "deauville": 5},
    {"response_value": "CR", "response_basis": "ct"},
    {"response_value": "PD", "response_basis": "clinical"},
])
def test_lugano_response_accepted(reg, pid, kw):
    reg.obs(pid, **LUGANO, **kw)
    reg.con.commit()


@pytest.mark.parametrize("kw, match", [
    ({"response_value": "CR", "response_basis": "pet_ct", "deauville": 2}, "lugano_basis"),
    ({"response_value": "CMR", "response_basis": "ct", "deauville": 2}, "lugano_basis"),
    ({"response_value": "CMR", "response_basis": "pet_ct"}, "lugano_deauville"),
    ({"response_value": "CMR", "response_basis": "pet_ct", "deauville": 4}, "lugano_deauville"),
    ({"response_value": "NMR", "response_basis": "pet_ct", "deauville": 3}, "lugano_deauville"),
    ({"response_value": "CRi", "response_basis": "ct"}, "response_lugano"),
])
def test_lugano_response_constraints(reg, pid, kw, match):
    with rejected(reg.con, match):
        reg.obs(pid, **LUGANO, **kw)


def test_pet_without_response_needs_no_criteria(reg, pid):
    """ПЭТ-КТ без оценки ответа (только заключение и Deauville) — допустима."""
    reg.obs(pid, pet_ct_report="ПЭТ-КТ промежуточная: Deauville 2", deauville=2)
    reg.con.commit()
