"""Маркеры, линии, наблюдения, смерть, лабораторные, v_survival (B2, B4, I5, I6, I7)."""
import pytest

from conftest import rejected


@pytest.fixture
def pid(reg):
    p = reg.patient("990000000201")
    reg.con.commit()
    return p


# --- модульные поля ХЛЛ -----------------------------------------------------
def test_marker_needs_value_or_missing_code(reg, pid):
    with rejected(reg.con, "value_or_missing"):
        reg.marker(pid, "IGHV")
    with rejected(reg.con, "value_or_missing"):
        reg.marker(pid, "IGHV", value="mutated", missing="unknown")


def test_marker_missing_code_and_retests(reg, pid):
    reg.marker(pid, "IGHV", missing="not_done")
    reg.marker(pid, "TP53", value="wt", test_date="2021-03-01")
    reg.marker(pid, "TP53", value="mut", test_date="2023-05-01")  # пересмотр перед 2-й линией
    reg.con.commit()
    assert reg.one("SELECT count(*) FROM module_data WHERE field_key = 'TP53'") == 2


def test_marker_unique_per_test_date(reg, pid):
    reg.marker(pid, "TP53", value="wt", test_date="2023-05-01")
    reg.marker(pid, "IGHV", value="unmutated")
    reg.con.commit()
    with rejected(reg.con, "marker_uq"):
        reg.marker(pid, "TP53", value="mut", test_date="2023-05-01")
    with rejected(reg.con, "marker_uq"):  # без даты — тоже один (NULLS NOT DISTINCT)
        reg.marker(pid, "IGHV", value="mutated")


def test_soft_deleted_marker_can_be_reentered(reg, pid):
    md = reg.marker(pid, "IGHV", value="mutated")
    reg.con.commit()
    reg.as_app()
    reg.cur.execute("UPDATE module_data SET deleted_at = now() WHERE md_id = %s", (md,))
    reg.marker(pid, "IGHV", value="unmutated")
    reg.con.commit()


# --- линии терапии ----------------------------------------------------------
def test_line_duration_computed(reg, pid):
    lid = reg.line(pid, start_date="2021-04-01", end_date="2021-10-01")
    reg.con.commit()
    assert reg.one("SELECT duration_days FROM therapy_line WHERE line_id = %s", lid) == 183
    with rejected(reg.con, "duration_days"):
        reg.as_app()
        reg.cur.execute("UPDATE therapy_line SET duration_days = 5 WHERE line_id = %s", (lid,))


@pytest.mark.parametrize("kw, match", [
    ({"start_date": "2021-04-01", "end_date": "2021-01-01"}, "end_after_start"),
    ({"line_no": 0}, "line_no_positive"),
    ({"cycles": -1}, "cycles_nonneg"),
])
def test_line_constraints(reg, pid, kw, match):
    with rejected(reg.con, match):
        reg.line(pid, **kw)


# --- наблюдения -------------------------------------------------------------
def test_observation_line_must_belong_to_patient(reg, pid):
    lid = reg.line(pid)
    other = reg.patient("990000000202", branch="synth_b")
    reg.con.commit()
    with rejected(reg.con, "observation_line_fkey"):
        reg.obs(other, line_id=lid)


def test_response_per_line_and_progression(reg, pid):
    lid = reg.line(pid, start_date="2021-04-01", end_date="2021-10-01")
    reg.obs(pid, line_id=lid, response_value="CR", response_criteria="iwCLL 2018", response_basis="ct")
    reg.obs(pid, obs_date="2023-04-01", event_type="progression", event_date="2023-04-01")
    reg.con.commit()


@pytest.mark.parametrize("kw, match", [
    ({"response_value": "CMR", "response_criteria": "iwCLL 2018", "response_basis": "ct"}, "response_iwcll"),
    ({"response_value": "CR", "response_criteria": "iwCLL 2018"}, "response_complete"),
    ({"response_value": "CR", "response_criteria": "iwCLL 2018", "response_basis": "КТ"}, "response_basis_check"),
    ({"event_type": "progression"}, "event_date"),
    ({"event_type": "none", "event_date": "2021-11-01"}, "event_date"),
    ({"event_type": "прогрессия", "event_date": "2021-11-01"}, "event_type_check"),
    ({"event_type": "progression", "event_date": "2021-11-01", "death_cause": "unknown"}, "death_cause_required"),
    ({"pet_ct_report": "пациент 990000000201, Deauville 3"}, "pet_ct_no_iin"),
])
def test_observation_constraints(reg, pid, kw, match):
    with rejected(reg.con, match):
        reg.obs(pid, **kw)


# --- согласованность смерти -------------------------------------------------
def test_dead_status_requires_death_event(reg, pid):
    with rejected(reg.con, "требует наблюдения"):
        reg.as_app()
        reg.cur.execute("UPDATE patient SET status_last_contact = 'dead' WHERE patient_id = %s", (pid,))


def test_death_requires_cause(reg, pid):
    with rejected(reg.con, "death_cause_required"):
        reg.obs(pid, event_type="death", event_date="2024-01-10")


def test_death_event_requires_dead_status(reg, pid):
    with rejected(reg.con, "статус пациента"):
        reg.obs(pid, event_type="death", event_date="2024-01-10", death_cause="unknown")


def test_dead_and_death_event_in_one_transaction(reg, pid):
    reg.as_app()
    reg.cur.execute("UPDATE patient SET status_last_contact = 'dead' WHERE patient_id = %s", (pid,))
    reg.obs(pid, obs_date="2024-01-10", event_type="death", event_date="2024-01-10",
            death_cause="lpd_related")
    reg.con.commit()


def test_single_death_event(reg, pid):
    test_dead_and_death_event_in_one_transaction(reg, pid)
    with rejected(reg.con, "больше одного"):
        reg.obs(pid, obs_date="2024-01-11", event_type="death", event_date="2024-01-11",
                death_cause="unknown")


# --- лабораторные -----------------------------------------------------------
def test_lab_canonical_and_flag_exclusive(reg, pid):
    reg.lab(pid, loinc="8302-2", value_raw=14, unit_raw="g/dL", value_canonical=140, unit_canonical="g/L")
    reg.lab(pid, loinc="8302-2", value_raw=14, unit_raw="g/pL", unit_canonical="g/L", flag="unknown_unit")
    reg.con.commit()
    with rejected(reg.con, "flag_xor"):
        reg.lab(pid, loinc="8302-2", value_raw=14, unit_raw="g/dL", value_canonical=140,
                unit_canonical="g/L", flag="unknown_unit")


# --- выживаемость и мягкое удаление -----------------------------------------
def test_v_survival_and_soft_delete(reg, pid):
    other = reg.patient("990000000203", branch="synth_b")
    reg.obs(pid, obs_date="2023-04-01", event_type="progression", event_date="2023-04-01")
    reg.con.commit()
    assert dict(reg.all("SELECT patient_id, event FROM v_survival")) == {pid: 1, other: 0}
    assert reg.one("SELECT event_or_censor_date::text FROM v_survival WHERE patient_id = %s",
                   other) == "2024-06-01"

    reg.as_app()
    reg.cur.execute("UPDATE patient SET deleted_at = now(), deleted_by = %s WHERE patient_id = %s",
                    (reg.doc, other))
    reg.con.commit()
    assert reg.one("SELECT count(*) FROM v_survival") == 1
