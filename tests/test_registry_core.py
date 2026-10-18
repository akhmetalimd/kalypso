"""Движок registry_core: канонизация, Каплан–Майер, валидаторы (включая E1–E4)."""
from datetime import date, datetime

import pytest

from registry_core import (
    canonicalize_lab, completeness_flags, find_node, kaplan_meier, resolve_codes,
    survival_by_threshold, validate_codes, validate_record, NOSOLOGY_TREE,
)

CLL_FULL = {"IGHV": "mutated", "TP53": "wt", "del17p": "normal_negative",
            "del11q": "not_done", "tri12": "not_done", "del13q": "positive"}


# --- канонизация ------------------------------------------------------------
@pytest.mark.parametrize("loinc, value, unit, expected", [
    ("8302-2", 14, "g/dL", 140.0),
    ("8302-2", 140, "g/L", 140.0),
    ("2160-0", 1.0, "mg/dL", 88.4),
    ("731-0", 25000, "/uL", 25.0),
])
def test_canonicalize(loinc, value, unit, expected):
    c = canonicalize_lab(loinc, value, unit)
    assert c.value_canonical == expected and c.flag is None
    assert (c.value_raw, c.unit_raw) == (value, unit)  # исходное сохраняется


def test_unknown_unit_flagged_not_converted():
    c = canonicalize_lab("8302-2", 14, "g/pL")
    assert c.flag == "unknown_unit" and c.value_canonical is None and c.unit_canonical == "g/L"


def test_unit_is_case_sensitive_ucum():
    assert canonicalize_lab("8302-2", 14, "G/dL").flag == "unknown_unit"


@pytest.mark.parametrize("loinc", ["0000-0", "_comment"])  # E2: служебный ключ конфига
def test_unknown_loinc_flagged(loinc):
    assert canonicalize_lab(loinc, 1, "x").flag == "unknown_loinc"


# --- Каплан–Майер -----------------------------------------------------------
def test_km_textbook_example():
    km = kaplan_meier([5, 10, 10, 20, 25], [1, 0, 1, 1, 0])
    assert [p["survival"] for p in km["curve"]] == [0.8, 0.6, 0.3]
    assert km["median"] == 20 and km["n"] == 5


def test_km_all_censored_has_no_median():
    assert kaplan_meier([5, 10], [0, 0]) == {"curve": [], "median": None, "n": 2}


@pytest.mark.parametrize("durations, events", [
    ([-30, 10], [1, 0]),  # E3: событие раньше time-zero
    ([10, 20], [1, 2]),
    ([10, 20], [1]),      # zip раньше молча обрезал
])
def test_km_rejects_bad_input(durations, events):
    with pytest.raises(ValueError):
        kaplan_meier(durations, events)


def test_survival_by_threshold_groups():
    sb = survival_by_threshold(
        [{"value": 8, "duration": 12, "event": 1}, {"value": 3, "duration": 30, "event": 0},
         {"value": 9, "duration": 6, "event": 1}, {"value": None, "duration": 9, "event": 1}],
        "IL-6", 5.0)
    assert sb["high"]["n"] == 2 and sb["low"]["n"] == 1


# --- validate_record (E1) ---------------------------------------------------
@pytest.mark.parametrize("dx, ev", [
    (date(2020, 1, 1), "2019-01-01"),
    ("2020-01-01", date(2019, 1, 1)),
    (datetime(2020, 1, 1, 10, 0), date(2019, 1, 1)),
])
def test_validate_record_mixed_date_types(dx, ev):
    assert "event_before_diagnosis" in validate_record({"dx_date": dx, "event_date": ev})


def test_validate_record_rejects_garbage_date():
    with pytest.raises(ValueError):
        validate_record({"dx_date": "01.02.2020"})


def test_validate_record_flags_on_codes():
    flags = validate_record({"dx_date": date(2020, 1, 1), "event_date": date(2019, 1, 1),
                             "event_type": "death", "who_haem5": "cll_sll", "stage_system": "Lugano"})
    assert {"event_before_diagnosis", "death_cause_missing", "staging_system_mismatch"} <= set(flags)


# --- validate_codes (E4) ----------------------------------------------------
@pytest.mark.parametrize("icd_o, mkb", [
    ("9823/3", "C91.1"),
    ("M-9823/3", "c91.1"),
    ("9670/3", "C83.0"),  # ЛМЛ — альтернативная пара узла ХЛЛ/ЛМЛ
])
def test_codes_consistent(icd_o, mkb):
    assert validate_codes({"who_haem5": "cll_sll", "icd_o_3_2": icd_o, "mkb": mkb}) == []


@pytest.mark.parametrize("rec, flag", [
    ({"who_haem5": "cll_sll", "icd_o_3_2": "9823/3", "mkb": "C83.3"}, "mkb_mismatch"),
    ({"who_haem5": "cll_sll", "icd_o_3_2": "9680/3", "mkb": "C91.1"}, "icd_o_mismatch"),
    ({"who_haem5": "cll_sll", "icd_o_3_2": "9670/3", "mkb": "C91.1"}, "code_pair_mismatch"),
    ({"who_haem5": "nope", "icd_o_3_2": "9823/3", "mkb": "C91.1"}, "unknown_who_haem5"),
    ({"icd_o_3_2": "9823/3", "mkb": "C91.1"}, "who_haem5_missing"),
    ({"who_haem5": "dlbcl", "icd_o_3_2": "9680/3", "mkb": "C83.3"}, "nosology_not_active"),
])
def test_codes_flags(rec, flag):
    assert flag in validate_codes(rec)


def test_mkb_prefix_match_for_category_node():
    assert "mkb_mismatch" not in validate_codes(
        {"who_haem5": "follicular", "icd_o_3_2": "9690/3", "mkb": "C82.1"})


def test_resolve_codes_inherits_from_parent():
    assert resolve_codes("cll_sll") == {"icd_o_3_2": "9823/3", "mkb10": "C91.1"}
    assert resolve_codes("lymphoid") == {}
    assert resolve_codes("nope") is None


# --- completeness_flags (E4, FR-7) ------------------------------------------
def test_cll_core_complete_with_missing_codes():
    assert completeness_flags({"who_haem5": "cll_sll", "module_data": CLL_FULL}) == []


def test_cll_core_missing_markers_flagged():
    md = {k: v for k, v in CLL_FULL.items() if k not in ("TP53", "del17p")}
    md["IGHV"] = ""
    assert completeness_flags({"who_haem5": "cll_sll", "module_data": md}) == [
        "missing_IGHV", "missing_TP53", "missing_del17p"]


def test_finished_line_without_response_flagged():
    rec = {"who_haem5": "cll_sll", "module_data": CLL_FULL,
           "therapy_lines": [{"line_id": "L1", "line_no": 1, "end_date": "2021-10-01"},
                             {"line_id": "L2", "line_no": 2, "end_date": "2023-01-01"},
                             {"line_id": "L3", "line_no": 3, "end_date": None}],
           "observations": [{"line_id": "L1", "response_value": "CR"}]}
    assert completeness_flags(rec) == ["missing_response_line_2"]


@pytest.mark.parametrize("observations, flagged", [
    ([], True),
    ([{"event_type": "death", "death_cause": None}], True),
    ([{"event_type": "death", "death_cause": "unknown"}], False),
])
def test_dead_requires_death_cause(observations, flagged):
    rec = {"who_haem5": "cll_sll", "module_data": CLL_FULL, "status_last_contact": "dead",
           "observations": observations}
    assert ("missing_death_cause" in completeness_flags(rec)) is flagged


def test_cll_node_declares_core_fields():
    node = find_node(NOSOLOGY_TREE, "cll_sll")
    assert [f["key"] for f in node["core_fields"]] == list(CLL_FULL)
