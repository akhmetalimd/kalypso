"""Нозологии на общей основе (MCL, ЛХ, WM, MZL, PTCL): один набор проверок на каждую.

CASES — данные нозологии: форма пациента, поля модуля и анализы, при которых индексы
считаются известным образом; индекс карточки и его ограничение в БД. Общие проверки:
ввод и полнота, расчёт индексов, отказ БД, синтетика (карточка = расчёт), аналитика, веб.
"""
import re

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from conftest import TEST_PASSWORD, rejected
from kalypso import analytics, registry, synthetic
from kalypso.config import Settings
from kalypso.registry import ValidationError
from kalypso.web.app import create_app
from registry_core import INDEXES, MISSING_CODES, find_node, NOSOLOGY_TREE

R = {"provenance": "retrospective"}

CASES = {
    # 71 год, ECOG 2, ЛДГ/ВГН 1,0–1,49, лейкоциты 12: sMIPI 3 + 2 + 2 + 2 = 9 (высокий); Ki-67 ≥ 30 % — MIPI-c 3.
    "mcl": {
        "form": {"birth_date": "1950-02-01", "stage_system": "Lugano", "stage_value": "IV", "ecog": "2"},
        "fields": {"ldh_ratio": "100_149", "ki67": "ge_30", "tp53_mcl": "not_detected", "blastoid": "no"},
        "labs": [("6690-2", "12", "10*9/L")],
        "expected": {"sMIPI": (9, "high"), "MIPI-c": (3, "high")},
        "stored": "9", "bad_score": "12", "db": ("sMIPI", 12, "high", "index_smipi"),
        "optional": ["leukemic_nn", "ccnd1_neg"],
    },
    # 31 год, мужчина, стадия IV; альбумин 35, Hb 100, лейкоциты 16, лимфоциты 0,5: IPS 1+1+1+0+1+1+1 = 6;
    # стадия IV — GHSG «распространённые» (2) независимо от факторов.
    "hodgkin": {
        "form": {"birth_date": "1990-02-01", "stage_system": "Lugano", "stage_value": "IV", "ecog": "0"},
        "fields": {"hl_subtype": "ns", "bulky_med": "yes", "esr_raised": "yes", "nodal_areas_hl": "ge_3",
                   "extranodal_hl": "no"},
        "labs": [("1751-7", "35", "g/L"), ("8302-2", "100", "g/L"), ("6690-2", "16", "10*9/L"), ("731-0", "0.5", "10*9/L")],
        "expected": {"IPS": (6, "high"), "GHSG": (2, "advanced")},
        "stored": "6", "bad_score": "8", "db": ("IPS", 9, "high", "index_ips"),
        "optional": [],
    },
}
NOSOLOGIES = list(CASES)


def form(world, nos, **kw):
    case = CASES[nos]
    node = find_node(NOSOLOGY_TREE, nos)
    base = {"who_haem5": nos, "dx_date": "2021-03-01", "icd_o_3_2": node["codes"]["icd_o_3_2"],
            "mkb": node["codes"]["mkb10"], **case["form"]}
    base.setdefault("stage_system", "")
    base.setdefault("stage_value", "")
    return world.patient_form(**{**base, **kw})


def register(world, nos, **kw):
    return registry.register_patient(world.db, world.doc_a, form(world, nos, **kw)).record_id


def fill(world, nos, pid, fields=None, test_date="2021-03-05"):
    for key, val in (fields or CASES[nos]["fields"]).items():
        result = {"missing": val} if val in MISSING_CODES else {"value": val}
        registry.add_marker(world.db, world.doc_a, pid, {**R, "field_key": key, **result, "test_date": test_date})
    for loinc, value, unit in CASES[nos]["labs"] if fields is None else []:
        registry.add_lab(world.db, world.doc_a, pid, {**R, "loinc": loinc, "value": value, "unit": unit,
                                                      "obs_date": "2021-03-02"})


@pytest.mark.parametrize("nos", NOSOLOGIES)
def test_entry_completeness_and_indices(world, nos):
    case, node = CASES[nos], find_node(NOSOLOGY_TREE, nos)
    pid = register(world, nos, index_score=case["stored"])
    card = registry.get_card(world.db, world.doc_a, pid)
    required = [f["key"] for f in node["core_fields"] if not f.get("optional")]
    assert {f"missing_{k}" for k in required} <= set(card["flags"]["completeness"])
    assert not {f"missing_{k}" for k in case["optional"]} & set(card["flags"]["completeness"])
    assert [i["name"] for i in card["indices"]] == list(case["expected"])
    if node.get("index") in INDEXES:
        assert card["patient"]["index_name"] == node["index"]
    fill(world, nos, pid)
    card = registry.get_card(world.db, world.doc_a, pid)
    assert not [f for f in card["flags"]["completeness"] if f.startswith("missing_")]
    got = {i["name"]: (i["result"]["score"], i["result"]["group"]) if i["result"] else None for i in card["indices"]}
    assert got == case["expected"]


@pytest.mark.parametrize("nos", NOSOLOGIES)
def test_stored_index_validated(world, nos):
    top = INDEXES[find_node(NOSOLOGY_TREE, nos)["index"]]["max"]
    with pytest.raises(ValidationError, match=f"от 0 до {top}"):
        register(world, nos, index_score=CASES[nos]["bad_score"])


@pytest.mark.parametrize("nos", NOSOLOGIES)
def test_db_rejects_bad_stored_index(reg, nos):
    name, score, group, constraint = CASES[nos]["db"]
    node = find_node(NOSOLOGY_TREE, nos)
    with rejected(reg.con, constraint):
        reg.patient("990000000901", who_haem5=nos, icd_o_3_2=node["codes"]["icd_o_3_2"], mkb=node["codes"]["mkb10"],
                    index_name=name, index_score=score, index_group=group)


@pytest.mark.parametrize("nos", NOSOLOGIES)
def test_synthetic_dashboard_and_groupings(world, nos):
    assert synthetic.generate(world.db, [world.doc_a, world.doc_b], n=20, seed=5, duplicates=1,
                              nosology=nos)["patients"] == 20
    d = analytics.module_dashboard(world.db, world.curator, nos)
    assert d["n"] == 20 and d["treated"][0] > 0
    agree = dict(next(items for title, items in d["indices"] if "карточка и расчёт" in title)[i][:2]
                 for i in range(len(next(items for title, items in d["indices"] if "карточка и расчёт" in title))))
    assert "расходится" not in agree and agree.get("совпадает", 0) > 0          # карточка = расчёт
    for code in analytics.GROUPING_SETS[nos]:
        res = analytics.explore(world.db, world.curator, "OS", grouping=code)
        assert res["grouping"] == code and res["nosology"] in (nos, None), code


@pytest.fixture
def app(world):
    return create_app(Settings(dsn=world.dsn, session_secret=b"s" * 32, secure_cookies=False), world.db)


def login(app, who="doc.a"):
    c = TestClient(app, follow_redirects=False)
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', c.get("/login").text).group(1)
    assert c.post("/login", data={"login": who, "password": TEST_PASSWORD, "csrf": token}).status_code == 303
    return c


@pytest.mark.parametrize("nos", NOSOLOGIES)
def test_web_pages(app, world, nos):
    pid = register(world, nos, index_score=CASES[nos]["stored"])
    fill(world, nos, pid)
    c = login(app)
    assert f'href="/patients/new?who={nos}"' in c.get("/patients/new").text
    page = c.get(f"/patients/new?who={nos}").text
    node = find_node(NOSOLOGY_TREE, nos)
    assert node["name"] in page and (node["index"] in page if node.get("index") else True)
    card = c.get(f"/patients/{pid}").text
    assert all(name in card for name in CASES[nos]["expected"])
    assert all(str(escape(f["label_ru"])) in card for f in node["core_fields"])
    assert node["regimens"][0] in card
    for url in (f"/dashboard/m/{nos}", "/dashboard", f"/survival?nosology={nos}&grouping={analytics.GROUPING_SETS[nos][0]}"):
        r = c.get(url)
        assert r.status_code == 200, url
    assert c.get("/dashboard/m/cll_sll").headers["location"] == "/dashboard/cll"
    assert c.get("/dashboard/m/nope").status_code == 404


# --- особенности нозологий -------------------------------------------------------
def test_mcl_high_risk_panel(world):
    pid = register(world, "mcl")
    assert registry.get_card(world.db, world.doc_a, pid)["module_info"] == []
    fill(world, "mcl", pid, {"tp53_mcl": "detected", "blastoid": "yes"})
    [info] = registry.get_card(world.db, world.doc_a, pid)["module_info"]
    assert info["warn"] and "TP53" in info["text"] and "бластоидный" in info["text"]


def test_hodgkin_early_stage_ghsg_and_pet2_landmark(world):
    pid = register(world, "hodgkin", stage_value="II", b_symptoms="no", last_contact_date="2024-06-01")
    fill(world, "hodgkin", pid, {"hl_subtype": "ns", "bulky_med": "no", "esr_raised": "no",
                                 "nodal_areas_hl": "lt_3", "extranodal_hl": "no"})
    ghsg = next(i for i in registry.get_card(world.db, world.doc_a, pid)["indices"] if i["name"] == "GHSG")
    assert ghsg["result"]["group"] == "early_favorable"
    lid = registry.add_line(world.db, world.doc_a, pid, {**R, "regimen": "ABVD + ЛТ", "start_date": "2021-03-15",
                                                         "end_date": "2021-07-15"}).record_id
    registry.add_observation(world.db, world.doc_a, pid, {**R, "obs_date": "2021-05-10", "line_id": str(lid),
                                                          "pet_ct_report": "ПЭТ-2: Deauville 2", "deauville": "2"})
    res = analytics.explore(world.db, world.curator, "PFS", grouping="deauville_pet2")
    assert res["nosology"] == "hodgkin" and {g["label"] for g in res["groups"]} == {"ПЭТ-2: Deauville 1–3"}
    row = res["rows"][0]
    assert str(row["start"]) == "2021-05-10"                                  # отсчёт от ПЭТ-2

