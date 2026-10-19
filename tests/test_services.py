"""Доменные сервисы: регистрация и дедупликация, права, версии диагноза, клинические записи."""
import csv
import io
import logging

import pytest

from kalypso import admin, export, registry, synthetic
from kalypso.rbac import AccessDenied, NotFound
from kalypso.registry import ValidationError


def _all_text(world, skip: tuple = ()) -> str:
    """Всё содержимое таблиц public (кроме skip) как текст — для поиска утечек ИИН."""
    with world.db.tx() as cur:
        cur.execute("RESET ROLE")  # читаем всё, включая служебные таблицы
        cur.execute("SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")
        tables = [r["table_name"] for r in cur.fetchall() if r["table_name"] not in skip]
        parts = []
        for t in tables:
            cur.execute(f"SELECT t::text AS row FROM {t} t")
            parts += [r["row"] for r in cur.fetchall()]
    return "\n".join(parts)


# --- регистрация, ИИН, дедупликация ------------------------------------------
def test_iin_stored_only_in_patient_card(world):
    form = world.patient_form()
    res = registry.register_patient(world.db, world.doc_a, form)
    assert res.status == "created"
    with world.db.tx() as cur:
        cur.execute("RESET ROLE")
        cur.execute("SELECT iin FROM patient WHERE patient_id = %s", (res.record_id,))
        assert cur.fetchone()["iin"] == form["iin"]
    assert form["iin"] not in _all_text(world, skip=("patient",))      # ни в аудите, ни где-то ещё


def test_iin_normalized_and_checked(world):
    form = world.patient_form()
    spaced = f"{form['iin'][:6]} {form['iin'][6:]}"
    pid = registry.register_patient(world.db, world.doc_a, {**form, "iin": spaced}).record_id
    assert registry.find_by_iin(world.db, world.doc_a, form["iin"]) == pid
    bad = form["iin"][:11] + str((int(form["iin"][11]) + 1) % 10)
    with pytest.raises(ValidationError, match="контрольная"):
        registry.find_by_iin(world.db, world.doc_a, bad)


def test_same_branch_duplicate_opens_existing(world):
    form = world.patient_form()
    pid = registry.register_patient(world.db, world.doc_a, form).record_id
    again = registry.register_patient(world.db, world.doc_a, {"iin": form["iin"]})
    assert (again.status, again.record_id) == ("exists_visible", pid)


def test_cross_branch_duplicate_needs_curator(world):
    form = world.patient_form()
    pid = registry.register_patient(world.db, world.doc_a, form).record_id
    res = registry.register_patient(world.db, world.doc_b, {"iin": form["iin"]})
    assert (res.status, res.record_id) == ("exists_requested", None)  # id не раскрывается
    with pytest.raises(NotFound):
        registry.get_card(world.db, world.doc_b, pid)
    assert registry.find_by_iin(world.db, world.doc_b, form["iin"]) is None

    [req] = admin.list_requests(world.db, world.curator)
    admin.decide_request(world.db, world.curator, req["request_id"], approve=True)
    assert registry.get_card(world.db, world.doc_b, pid)["branches"] == ["synth_a", "synth_b"]


def test_repeated_request_is_not_duplicated(world):
    form = world.patient_form()
    registry.register_patient(world.db, world.doc_a, form)
    for _ in range(2):
        registry.register_patient(world.db, world.doc_b, {"iin": form["iin"]})
    assert len(admin.list_requests(world.db, world.curator)) == 1


@pytest.mark.parametrize("kw, message", [
    ({"iin": "123"}, "12 цифр"),
    ({"mkb": "C83.3"}, "МКБ-10 не соответствует"),
    ({"icd_o_3_2": "9670/3"}, "разным вариантам"),
    ({"histology_report": "ХЛЛ. Тел. +7 701 123 45 67"}, "номер телефона"),
    ({"last_contact_date": "2020-01-01"}, "Последний контакт"),
    ({"ecog": "1", "ecog__missing": "unknown"}, "либо значение"),
    ({"status_last_contact": "dead"}, "Дата смерти"),
    ({"sex": ""}, "Пол"),
])
def test_register_validation(world, kw, message):
    with pytest.raises(ValidationError) as e:
        world.register(**kw)
    assert any(message in err for err in e.value.errors), e.value.errors


def test_sll_codes_accepted(world):
    assert world.register(icd_o_3_2="9670/3", mkb="C83.0").status == "created"


def test_missing_codes_and_death_at_registration(world):
    res = world.register(ecog__missing="not_done", status_last_contact="dead",
                         death_date="2024-06-01", death_cause="lpd_related")
    card = registry.get_card(world.db, world.doc_a, res.record_id)
    assert card["missing"] == {"ecog": "not_done"}
    [death] = [o for o in card["observations"] if o["event_type"] == "death"]
    assert death["death_cause"] == "lpd_related"
    assert "empty_ecog" not in card["flags"]["completeness"]


# --- права ------------------------------------------------------------------
def test_branch_isolation_and_roles(world):
    pid = world.register().record_id
    with pytest.raises(NotFound):
        registry.get_card(world.db, world.doc_b, pid)
    assert registry.list_patients(world.db, world.doc_b) == []
    for reader in (world.curator, world.analyst, world.admin):
        assert registry.get_card(world.db, reader, pid)["patient"]["patient_id"] == pid
    for writer in (world.curator, world.analyst, world.admin):
        with pytest.raises(AccessDenied):
            registry.add_lab(world.db, writer, pid, {})
    with pytest.raises(NotFound):  # чужой филиал не может писать
        registry.add_lab(world.db, world.doc_b, pid, {"loinc": "8302-2", "value": "140", "unit": "g/L",
                                                      "obs_date": "2021-03-01", "provenance": "retrospective"})


def test_iin_visible_only_to_physician_and_curator(world):
    form = world.patient_form()
    pid = registry.register_patient(world.db, world.doc_a, form).record_id
    for who in (world.doc_a, world.curator):
        assert registry.get_card(world.db, who, pid)["patient"]["iin"] == form["iin"]
        assert registry.list_patients(world.db, who)[0]["iin"] == form["iin"]
    for who in (world.analyst, world.admin):
        assert "iin" not in registry.get_card(world.db, who, pid)["patient"]
        assert "iin" not in registry.list_patients(world.db, who)[0]


# --- версия диагноза --------------------------------------------------------
def test_diagnosis_revision(world):
    res = world.register()
    card = registry.get_card(world.db, world.doc_a, res.record_id)
    form = {k: v for k, v in card["patient"].items() if v is not None}
    with pytest.raises(ValidationError, match="причину пересмотра"):
        registry.update_patient(world.db, world.doc_a, res.record_id, {**form, "dx_date": "2021-02-01"})
    registry.update_patient(world.db, world.doc_a, res.record_id,
                            {**form, "dx_date": "2021-02-01", "change_reason": "уточнена дата биопсии"})
    card = registry.get_card(world.db, world.doc_a, res.record_id)
    assert [(d["version"], d["change_reason"]) for d in card["diagnoses"]] == [
        (2, "уточнена дата биопсии"), (1, None)]


# --- клинические записи -----------------------------------------------------
LAB = {"obs_date": "2021-03-01", "provenance": "retrospective"}


def test_lab_canonicalized_and_flagged(world):
    pid = world.register().record_id
    assert registry.add_lab(world.db, world.doc_a, pid, {**LAB, "loinc": "8302-2", "value": "14,2", "unit": "g/dL"}).warnings == []
    res = registry.add_lab(world.db, world.doc_a, pid, {**LAB, "loinc": "8302-2", "value": "14", "unit": "g/pL"})
    assert res.warnings == ["lab_unknown_unit"]
    labs = registry.get_card(world.db, world.doc_a, pid)["labs"]
    assert [(float(l["value_raw"]), l["unit_raw"], l["value_canonical"] and float(l["value_canonical"]))
            for l in labs] == [(14.2, "g/dL", 142.0), (14.0, "g/pL", None)]


def test_lines_and_responses(world):
    pid = world.register().record_id
    line = {"regimen": "BR", "start_date": "2021-04-01", "end_date": "2021-10-01", "provenance": "retrospective"}
    l1 = registry.add_line(world.db, world.doc_a, pid, line).record_id
    registry.add_line(world.db, world.doc_a, pid, {**line, "start_date": "2022-01-01", "end_date": "2022-06-01"})
    card = registry.get_card(world.db, world.doc_a, pid)
    assert [l["line_no"] for l in card["lines"]] == [1, 2]
    assert {"missing_response_line_1", "missing_response_line_2"} <= set(card["flags"]["completeness"])

    registry.add_observation(world.db, world.doc_a, pid, {
        "obs_date": "2021-11-01", "line_id": str(l1), "response_value": "CR", "response_basis": "ct",
        "provenance": "retrospective"})
    card = registry.get_card(world.db, world.doc_a, pid)
    assert "missing_response_line_1" not in card["flags"]["completeness"]

    with pytest.raises(ValidationError, match="раньше даты диагноза"):
        registry.add_line(world.db, world.doc_a, pid, {**line, "start_date": "2020-01-01", "end_date": None})
    with pytest.raises(ValidationError, match="наблюдения по этой линии"):
        registry.soft_delete(world.db, world.doc_a, pid, "line", l1)


def test_observation_rules(world):
    pid = world.register().record_id
    obs = {"obs_date": "2022-01-01", "provenance": "retrospective"}
    with pytest.raises(ValidationError, match="пустое"):
        registry.add_observation(world.db, world.doc_a, pid, obs)
    with pytest.raises(ValidationError, match="основание"):
        registry.add_observation(world.db, world.doc_a, pid, {**obs, "response_value": "PR"})
    with pytest.raises(ValidationError, match="ИИН"):
        registry.add_observation(world.db, world.doc_a, pid, {**obs, "pet_ct_report": "№ 610412412345"})
    res = registry.add_observation(world.db, world.doc_a, pid, {**obs, "event_type": "progression",
                                                                "obs_date": "2020-01-01"})
    assert "event_before_diagnosis" in res.warnings  # противоречие — предупреждение, не запрет


def test_death_event_updates_status(world):
    pid = world.register().record_id
    death = {"obs_date": "2025-02-01", "event_type": "death", "death_cause": "unknown", "provenance": "retrospective"}
    registry.add_observation(world.db, world.doc_a, pid, death)
    card = registry.get_card(world.db, world.doc_a, pid)
    assert card["patient"]["status_last_contact"] == "dead"
    assert str(card["patient"]["last_contact_date"]) == "2025-02-01"
    with pytest.raises(ValidationError, match="больше одного"):
        registry.add_observation(world.db, world.doc_a, pid, death)


def test_markers_completeness_and_cll_ipi(world):
    pid = world.register(birth_date="1950-01-01", dx_date="2021-03-01", stage_value="B").record_id
    card = registry.get_card(world.db, world.doc_a, pid)
    assert {"missing_IGHV", "missing_TP53", "missing_del17p"} <= set(card["flags"]["completeness"])
    [cll_ipi] = card["indices"]
    assert cll_ipi["name"] == "CLL-IPI" and cll_ipi["result"] is None

    m = {"provenance": "retrospective", "test_date": "2021-03-01"}
    for key, val in [("IGHV", "unmutated"), ("TP53", "wild_type"), ("del17p", "not_detected")]:
        registry.add_marker(world.db, world.doc_a, pid, {**m, "field_key": key, "value": val})
    for key in ("del11q", "tri12", "del13q"):
        registry.add_marker(world.db, world.doc_a, pid, {**m, "field_key": key, "missing": "not_done"})
    registry.add_lab(world.db, world.doc_a, pid, {**LAB, "loinc": "1952-1", "value": "4.1", "unit": "mg/L"})
    with pytest.raises(ValidationError, match="результат"):
        registry.add_marker(world.db, world.doc_a, pid, {**m, "field_key": "IGHV", "value": "detected"})

    card = registry.get_card(world.db, world.doc_a, pid)
    assert not [f for f in card["flags"]["completeness"] if f.startswith("missing_")]
    # IGHV немут. 2 + β2M > 3.5 2 + Binet B 1 + возраст 71 > 65 1 = 6
    ipi = card["indices"][0]["result"]
    assert ipi["score"] == 6 and ipi["group"] == "high"

    md = next(x for x in card["markers"] if x["field_key"] == "IGHV")
    registry.soft_delete(world.db, world.doc_a, pid, "marker", md["md_id"])
    assert "missing_IGHV" in registry.get_card(world.db, world.doc_a, pid)["flags"]["completeness"]


# --- выгрузка и синтетика ----------------------------------------------------
def test_export_is_deidentified(world):
    form = world.patient_form(histology_report="ХЛЛ, CD5+ CD23+ — синт. заключение")
    registry.register_patient(world.db, world.doc_a, form)
    with pytest.raises(AccessDenied):
        export.patients_csv(world.db, world.doc_a)
    text = export.patients_csv(world.db, world.analyst)
    [row] = list(csv.DictReader(io.StringIO(text)))
    assert row["birth_year"] == "1950" and "birth_date" not in row
    assert form["iin"] not in text and "синт. заключение" not in text
    assert row["pfs_event"] == "0" and row["pfs_days"] == str((1188))


def test_synthetic_iins_only_in_patient_table(world, caplog):
    caplog.set_level(logging.DEBUG)
    summary = synthetic.generate(world.db, [world.doc_a, world.doc_b], n=12, seed=3, duplicates=2)
    assert summary["patients"] == 12 and summary["access_requests"] == 2
    dump = _all_text(world, skip=("patient",))
    leaked = [i for i in summary["iins"] if i in dump or i in caplog.text]
    assert leaked == []
    assert len(registry.list_patients(world.db, world.curator)) == 12


# --- правка клинических записей ---------------------------------------------
def _audit(world, table, rid):
    with world.db.tx() as cur:
        cur.execute("RESET ROLE")
        cur.execute("SELECT field, old_value, new_value, reason FROM audit_log "
                    "WHERE table_name = %s AND row_id = %s AND action = 'update' ORDER BY audit_id", (table, str(rid)))
        return [tuple(r.values()) for r in cur.fetchall()]


LINE = {"regimen": "BR", "start_date": "2021-04-01", "end_date": "2021-10-01", "provenance": "retrospective"}


def test_update_line_audited_with_reason(world):
    pid = world.register().record_id
    lid = registry.add_line(world.db, world.doc_a, pid, LINE).record_id
    registry.update_line(world.db, world.doc_a, pid, lid,
                         {**LINE, "end_date": "2021-12-01", "cycles": "6", "change_reason": "уточнено по выписке"})
    rec = registry.get_record(world.db, world.doc_a, pid, "line", lid)
    assert (str(rec["end_date"]), rec["cycles"], rec["duration_days"], rec["line_no"]) == ("2021-12-01", 6, 244, 1)
    changed = {f: (old, new, why) for f, old, new, why in _audit(world, "therapy_line", lid)}
    assert changed["end_date"] == ("2021-10-01", "2021-12-01", "уточнено по выписке")
    assert changed["cycles"] == (None, "6", "уточнено по выписке")
    with pytest.raises(ValidationError, match="раньше даты диагноза"):
        registry.update_line(world.db, world.doc_a, pid, lid, {**LINE, "start_date": "2020-01-01", "end_date": None})


def test_update_observation_and_death_rules(world):
    pid = world.register().record_id
    obs = {"obs_date": "2022-01-01", "event_type": "progression", "provenance": "retrospective"}
    oid = registry.add_observation(world.db, world.doc_a, pid, obs).record_id
    registry.update_observation(world.db, world.doc_a, pid, oid, {**obs, "event_type": "relapse"})
    assert registry.get_record(world.db, world.doc_a, pid, "observation", oid)["event_type"] == "relapse"

    registry.update_observation(world.db, world.doc_a, pid, oid,
                                {**obs, "obs_date": "2025-01-05", "event_type": "death", "death_cause": "unknown"})
    card = registry.get_card(world.db, world.doc_a, pid)
    assert card["patient"]["status_last_contact"] == "dead"
    assert str(card["patient"]["last_contact_date"]) == "2025-01-05"
    with pytest.raises(ValidationError, match="сменой статуса"):
        registry.update_observation(world.db, world.doc_a, pid, oid, obs)


def test_update_lab_recanonicalizes(world):
    pid = world.register().record_id
    lab = {**LAB, "loinc": "8302-2", "value": "140", "unit": "g/L"}
    rid = registry.add_lab(world.db, world.doc_a, pid, lab).record_id
    res = registry.update_lab(world.db, world.doc_a, pid, rid, {**lab, "value": "12.5", "unit": "g/dL"})
    rec = registry.get_record(world.db, world.doc_a, pid, "lab", rid)
    assert (float(rec["value_raw"]), rec["unit_raw"], float(rec["value_canonical"]), rec["flag"]) == (12.5, "g/dL", 125.0, None)
    assert res.warnings == []
    res = registry.update_lab(world.db, world.doc_a, pid, rid, {**lab, "unit": "g/pL"})
    assert res.warnings == ["lab_unknown_unit"]
    assert registry.get_record(world.db, world.doc_a, pid, "lab", rid)["value_canonical"] is None


def test_update_marker_keeps_key(world):
    pid = world.register().record_id
    m = {"field_key": "TP53", "value": "wild_type", "provenance": "retrospective"}
    rid = registry.add_marker(world.db, world.doc_a, pid, m).record_id
    registry.update_marker(world.db, world.doc_a, pid, rid,
                           {"field_key": "IGHV", "missing": "done_unavailable", "provenance": "retrospective"})
    rec = registry.get_record(world.db, world.doc_a, pid, "marker", rid)
    assert (rec["field_key"], rec["value"], rec["missing"]) == ("TP53", None, "done_unavailable")
    with pytest.raises(ValidationError, match="результат"):
        registry.update_marker(world.db, world.doc_a, pid, rid, {"value": "detected", "provenance": "retrospective"})


def test_edit_access_rules(world):
    pid = world.register().record_id
    rid = registry.add_lab(world.db, world.doc_a, pid, {**LAB, "loinc": "8302-2", "value": "140", "unit": "g/L"}).record_id
    data = {**LAB, "loinc": "8302-2", "value": "141", "unit": "g/L"}
    with pytest.raises(NotFound):  # чужой филиал
        registry.update_lab(world.db, world.doc_b, pid, rid, data)
    with pytest.raises(AccessDenied):
        registry.update_lab(world.db, world.curator, pid, rid, data)
    other = world.register().record_id
    with pytest.raises(NotFound):  # запись другого пациента
        registry.update_lab(world.db, world.doc_a, other, rid, data)
    registry.soft_delete(world.db, world.doc_a, pid, "lab", rid)
    with pytest.raises(NotFound):  # удалённую не правят
        registry.update_lab(world.db, world.doc_a, pid, rid, data)
