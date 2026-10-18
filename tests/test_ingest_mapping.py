"""Маппинг МИС -> регистр (без БД): белый список, перекодировки, проблемы, критерии включения."""
import json

import pytest

from kalypso.ingest.mapping import included, load_mapping, map_section
from kalypso.ingest.synthetic_his import build_store

CFG = load_mapping()


@pytest.fixture(scope="module")
def store():
    return build_store(n=10, seed=4)


def test_whitelist_drops_identifiers(store):
    for rec in store.values():
        out = map_section(CFG, "patient", rec)
        blob = json.dumps(out.fields, ensure_ascii=False)
        assert set(out.fields) <= {"sex", "birth_date", "branch", "last_contact_date"}
        for value in (rec["patient"]["iin"], rec["patient"]["lastName"], rec["patient"]["firstName"],
                      rec["patient"]["address"], rec["patient"]["phone"]):
            assert value not in blob


def test_documents_are_deidentified(store):
    for rec in store.values():
        for doc in rec["documents"]:
            out = map_section(CFG, "document", doc)
            assert not out.issues and rec["patient"]["iin"] not in out.fields["text"]
            assert out.deid_report


@pytest.mark.parametrize("lab, expected", [
    ({"id": "1", "code": "HGB", "value": "12,5", "unit": "г/дл", "date": "01.02.2022"},
     {"loinc": "8302-2", "value": 12.5, "unit": "g/dL", "obs_date": "2022-02-01"}),
    ({"id": "2", "code": "WBC", "value": "45", "unit": "10^9/л", "date": "2022-03-04"},
     {"loinc": "6690-2", "value": 45.0, "unit": "10*9/L", "obs_date": "2022-03-04"}),
])
def test_lab_mapping(lab, expected):
    out = map_section(CFG, "lab", lab)
    assert out.fields == expected and out.issues == []


@pytest.mark.parametrize("lab, problem", [
    ({"code": "XYZ", "value": "1", "unit": "г/л", "date": "01.02.2022"}, "нет в справочнике lab_codes"),
    ({"code": "HGB", "value": "1", "unit": "попугаи", "date": "01.02.2022"}, "нет в справочнике units"),
    ({"code": "HGB", "value": "много", "unit": "г/л", "date": "01.02.2022"}, "не удалось разобрать"),
    ({"code": "HGB", "value": "1", "unit": "г/л"}, "obs_date: нет значения"),
])
def test_lab_problems(lab, problem):
    assert any(problem in i for i in map_section(CFG, "lab", lab).issues)


def test_inclusion_and_diagnosis():
    rec = {"diagnoses": [{"icd10": "I10", "date": "01.01.2010"}, {"icd10": "C83.0", "date": "05.06.2021"}]}
    [dx] = included(CFG, rec)
    assert map_section(CFG, "diagnosis", dx).fields == {"mkb": "C83.0", "who_haem5": "cll_sll",
                                                        "icd_o_3_2": "9670/3", "dx_date": "2021-06-05"}
    assert included(CFG, {"diagnoses": [{"icd10": "C83.3"}]}) == []


def test_mapping_config_is_validated(tmp_path, monkeypatch):
    from kalypso.ingest import mapping
    bad = json.loads((mapping.MAPPING_DIR / "his_kz.json").read_text(encoding="utf-8"))
    bad["lab"]["unit"]["lookup"] = "nope"
    (tmp_path / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
    monkeypatch.setattr(mapping, "MAPPING_DIR", tmp_path)
    with pytest.raises(ValueError, match="нет справочника"):
        mapping.load_mapping("bad")
