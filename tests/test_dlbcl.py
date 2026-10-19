"""Модуль ДВКЛ: ввод, индексы IPI / R-IPI / NCCN-IPI, Lugano 2014, аналитика, синтетика, веб."""
import re

import pytest
from fastapi.testclient import TestClient

from conftest import TEST_PASSWORD
from kalypso import analytics, docgen, export, registry, synthetic
from kalypso.config import Settings
from kalypso.registry import ValidationError
from kalypso.web.app import create_app
from registry_core import MISSING_CODES

R = {"provenance": "retrospective"}
# 70 лет на диагнозе, ECOG 2, Lugano IV, ЛДГ > 1–3 × ВГН, ≥ 2 экстранодальных: IPI 5;
# NCCN-IPI: возраст 2 + ЛДГ 1 + стадия 1 + КМ/ЦНС 1 + ECOG 1 = 6.
FULL = {"coo_hans": "non_GCB", "myc_r": "not_detected", "bcl2_r": "detected", "bcl6_r": "not_done",
        "ldh_uln": "uln_1_3", "extranodal_n": "2plus", "extranodal_nccn": "yes", "bulky": "no"}


def dlbcl_form(world, **kw):
    base = {"who_haem5": "dlbcl", "birth_date": "1951-02-01", "dx_date": "2021-03-01",
            "stage_system": "Lugano", "stage_value": "IV", "ecog": "2"}
    return world.patient_form(**{**base, **kw})


def add_fields(world, pid, fields, test_date="2021-03-05"):
    for key, val in fields.items():
        result = {"missing": val} if val in MISSING_CODES else {"value": val}
        registry.add_marker(world.db, world.doc_a, pid, {**R, "field_key": key, **result, "test_date": test_date})


def index(card, name):
    return next(i for i in card["indices"] if i["name"] == name)


# --- ввод и проверки ---------------------------------------------------------
def test_dlbcl_registration_uses_dlbcl_settings(world):
    res = registry.register_patient(world.db, world.doc_a, dlbcl_form(world, index_score="5"))
    pt = registry.get_card(world.db, world.doc_a, res.record_id)["patient"]
    assert (pt["icd_o_3_2"], pt["mkb"], pt["stage_system"], pt["stage_value"]) == ("9680/3", "C83.3", "Lugano", "IV")
    assert (pt["index_name"], pt["index_score"], pt["index_group"]) == ("IPI", 5, "high")   # группа — из балла


@pytest.mark.parametrize("kw, message", [
    ({"stage_system": "Binet", "stage_value": "A"}, "Система стадирования"),
    ({"stage_value": "IIIE"}, "Стадия по Lugano"),
    ({"index_score": "6"}, "от 0 до 5"),
    ({"index_score": "2", "index_group": "high"}, "не соответствует баллу"),
    ({"index_group": "high"}, "укажите балл"),
    ({"mkb": "C91.1"}, "МКБ-10 не соответствует"),
])
def test_dlbcl_validation(world, kw, message):
    with pytest.raises(ValidationError) as e:
        registry.register_patient(world.db, world.doc_a, dlbcl_form(world, **kw))
    assert any(message in err for err in e.value.errors), e.value.errors


def test_cll_rejects_dlbcl_staging(world):
    with pytest.raises(ValidationError, match="Система стадирования"):
        world.register(stage_system="Lugano", stage_value="IV")


def test_module_fields_per_nosology(world):
    pid = registry.register_patient(world.db, world.doc_a, dlbcl_form(world)).record_id
    with pytest.raises(ValidationError, match="Маркер"):              # маркер ХЛЛ у пациента ДВКЛ
        registry.add_marker(world.db, world.doc_a, pid, {**R, "field_key": "IGHV", "value": "mutated"})
    with pytest.raises(ValidationError, match="результат"):
        registry.add_marker(world.db, world.doc_a, pid, {**R, "field_key": "ldh_uln", "value": "detected"})
    rid = registry.add_marker(world.db, world.doc_a, pid, {**R, "field_key": "coo_hans", "value": "GCB"}).record_id
    assert registry.get_record(world.db, world.doc_a, pid, "marker", rid)["module"] == "dlbcl"
    registry.update_marker(world.db, world.doc_a, pid, rid, {**R, "value": "non_GCB"})
    assert registry.get_record(world.db, world.doc_a, pid, "marker", rid)["value"] == "non_GCB"


def test_completeness_and_indices(world):
    pid = registry.register_patient(world.db, world.doc_a, dlbcl_form(world, index_score="4")).record_id
    card = registry.get_card(world.db, world.doc_a, pid)
    assert {"missing_coo_hans", "missing_ldh_uln", "missing_extranodal_n"} <= set(card["flags"]["completeness"])
    assert "missing_coo_gep" not in card["flags"]["completeness"]        # дополнительное поле
    assert [i["name"] for i in card["indices"]] == ["IPI", "R-IPI", "NCCN-IPI"]
    assert all(i["result"] is None for i in card["indices"])           # нет ЛДГ и очагов — не считаем

    add_fields(world, pid, FULL)
    card = registry.get_card(world.db, world.doc_a, pid)
    assert not [f for f in card["flags"]["completeness"] if f.startswith("missing_")]
    assert (index(card, "IPI")["result"]["score"], index(card, "IPI")["result"]["group"]) == (5, "high")
    assert index(card, "R-IPI")["result"]["group"] == "poor"
    assert (index(card, "NCCN-IPI")["result"]["score"], index(card, "NCCN-IPI")["result"]["group"]) == (6, "high")
    assert "dlbcl_myc_bcl2" not in card["flags"]["quality"]

    add_fields(world, pid, {"myc_r": "detected"}, test_date="2021-03-20")
    assert "dlbcl_myc_bcl2" in registry.get_card(world.db, world.doc_a, pid)["flags"]["quality"]


def test_lugano_responses(world):
    pid = registry.register_patient(world.db, world.doc_a, dlbcl_form(world)).record_id
    lid = registry.add_line(world.db, world.doc_a, pid, {**R, "regimen": "R-CHOP", "start_date": "2021-03-20",
                                                         "end_date": "2021-07-20"}).record_id
    obs = {**R, "obs_date": "2021-08-20", "line_id": str(lid)}
    for data, message in [
        ({"response_value": "CMR", "response_basis": "pet_ct"}, "Deauville"),
        ({"response_value": "CMR", "response_basis": "pet_ct", "deauville": "4"}, "CMR — Deauville 1–3"),
        ({"response_value": "CR", "response_basis": "pet_ct", "deauville": "2"}, "метаболический"),
        ({"response_value": "CRi", "response_basis": "ct"}, "недопустимое"),
    ]:
        with pytest.raises(ValidationError, match=message):
            registry.add_observation(world.db, world.doc_a, pid, {**obs, **data})
    oid = registry.add_observation(world.db, world.doc_a, pid, {**obs, "response_value": "CMR",
                                                                "response_basis": "pet_ct", "deauville": "2"}).record_id
    rec = registry.get_record(world.db, world.doc_a, pid, "observation", oid)
    assert (rec["response_criteria"], rec["response_value"], rec["deauville"]) == ("Lugano 2014", "CMR", 2)
    with pytest.raises(ValidationError, match="Deauville"):
        registry.update_observation(world.db, world.doc_a, pid, oid, {**obs, "response_value": "PMR",
                                                                      "response_basis": "pet_ct", "deauville": "2"})


def test_cll_keeps_iwcll(world):
    pid = world.register().record_id
    with pytest.raises(ValidationError, match="недопустимое"):
        registry.add_observation(world.db, world.doc_a, pid, {**R, "obs_date": "2022-01-01",
                                                              "response_value": "CMR", "response_basis": "pet_ct"})


def test_export_and_documents(world):
    pid = registry.register_patient(world.db, world.doc_a, dlbcl_form(world, index_score="5")).record_id
    add_fields(world, pid, FULL)
    text = export.patients_csv(world.db, world.analyst)
    header = text.splitlines()[0].split(",")
    assert {"IGHV", "coo_hans", "ldh_uln"} <= set(header)
    board = docgen.tumor_board_text(docgen.patient_summary(registry.get_card(world.db, world.doc_a, pid)))
    assert "IPI: в карточке 5 (Высокий)" in board and "NCCN-IPI: расчёт по данным 6" in board
    assert "Клеточное происхождение (Hans, ИГХ): non-GCB" in board and "GEP" not in board


# --- аналитика ---------------------------------------------------------------
@pytest.fixture
def mixed(world):
    """ДВКЛ: A — IPI 5, R-CHOP, ПЭТ-КТ Deauville 4 после линии, прогрессия, смерть;
    B — IPI 1, ПЭТ-КТ Deauville 2, ремиссия. ХЛЛ: C — для проверки разделения нозологий."""
    a = registry.register_patient(world.db, world.doc_a, dlbcl_form(world, index_score="5",
                                                                    last_contact_date="2022-06-01")).record_id
    add_fields(world, a, FULL)
    la = registry.add_line(world.db, world.doc_a, a, {**R, "regimen": "R-CHOP", "start_date": "2021-03-20",
                                                      "end_date": "2021-07-20"}).record_id
    registry.add_observation(world.db, world.doc_a, a, {**R, "obs_date": "2021-08-20", "line_id": str(la),
                                                        "response_value": "PMR", "response_basis": "pet_ct", "deauville": "4"})
    registry.add_observation(world.db, world.doc_a, a, {**R, "obs_date": "2021-11-01", "event_type": "progression"})
    registry.add_observation(world.db, world.doc_a, a, {**R, "obs_date": "2022-06-01", "event_type": "death",
                                                        "death_cause": "lpd_related"})
    b = registry.register_patient(world.db, world.doc_a, dlbcl_form(
        world, birth_date="1975-01-01", stage_value="II", ecog="0", index_score="0",
        last_contact_date="2024-06-01")).record_id
    add_fields(world, b, {**FULL, "ldh_uln": "le_uln", "extranodal_n": "0", "extranodal_nccn": "no", "bcl2_r": "not_detected"})
    lb = registry.add_line(world.db, world.doc_a, b, {**R, "regimen": "Pola-R-CHP", "start_date": "2021-03-20",
                                                      "end_date": "2021-07-20"}).record_id
    registry.add_observation(world.db, world.doc_a, b, {**R, "obs_date": "2021-08-25", "line_id": str(lb),
                                                        "response_value": "CMR", "response_basis": "pet_ct", "deauville": "2"})
    c = world.register(dx_date="2020-01-01", last_contact_date="2023-01-01").record_id
    return {"a": a, "b": b, "c": c}


def groups(res):
    return {g["label"]: (g["n"], g["events"]) for g in res["groups"]}


def test_dlbcl_grouping_narrows_cohort(world, mixed):
    res = analytics.explore(world.db, world.curator, "OS", grouping="ipi")
    assert res["nosology"] == "dlbcl" and res["n_cohort"] == 2
    assert groups(res) == {"Высокий": (1, 1), "Низкий": (1, 0)}
    assert any("сужена" in n for n in res["notes"])
    mixed_res = analytics.explore(world.db, world.curator, "OS")
    assert mixed_res["n_cohort"] == 3 and any("несколько нозологий" in n for n in mixed_res["notes"])
    assert analytics.explore(world.db, world.curator, "OS", grouping="IGHV")["nosology"] == "cll_sll"


def test_computed_index_groupings(world, mixed):
    assert groups(analytics.explore(world.db, world.curator, "OS", grouping="nccn_ipi")) == {
        "Высокий": (1, 1), "Низкий": (1, 0)}
    assert groups(analytics.explore(world.db, world.curator, "OS", grouping="r_ipi")) == {
        "Плохой": (1, 1), "Очень хороший": (1, 0)}
    assert groups(analytics.explore(world.db, world.curator, "OS", grouping="myc_bcl2")) == {
        "только BCL2": (1, 1), "без перестроек MYC и BCL2": (1, 0)}
    assert set(groups(analytics.explore(world.db, world.curator, "OS", grouping="lugano"))) == {"III–IV", "I–II"}


def test_deauville_landmark(world, mixed):
    res = analytics.explore(world.db, world.curator, "PFS", line_no=1, grouping="deauville_eot1")
    assert groups(res) == {"Deauville 4–5": (1, 1), "Deauville 1–3": (1, 0)}
    a = next(r for r in res["rows"] if r["patient_id"] == mixed["a"])
    assert str(a["start"]) == "2021-08-20" and a["days"] == 73      # отсчёт от ПЭТ-КТ до прогрессии
    assert res["landmark"] == {"missing": 0, "dropped": 0}
    registry.add_observation(world.db, world.doc_a, mixed["b"], {**R, "obs_date": "2021-08-22", "event_type": "relapse"})
    res = analytics.explore(world.db, world.curator, "PFS", line_no=1, grouping="deauville_eot1")
    assert res["landmark"]["dropped"] == 1                              # событие до ориентира — вне анализа


def test_dlbcl_dashboard(world, mixed):
    d = analytics.dlbcl_dashboard(world.db, world.curator)
    assert d["n"] == 2 and d["treated"] == (2, 2) and d["cr1"] == (1, 2)
    assert dict((k, n) for k, n, _ in d["ipi_agree"]) == {"совпадает": 2}
    assert dict((k, n) for k, n, _ in d["eot_deauville"]) == {"Deauville 2": 1, "Deauville 4": 1}
    assert d["responses"] == [("Линия 1", {"PR": 1, "CR": 1})]
    assert analytics.cll_dashboard(world.db, world.curator)["n"] == 1


# --- синтетика ---------------------------------------------------------------
def test_synthetic_dlbcl_cohort(world):
    summary = synthetic.generate(world.db, [world.doc_a, world.doc_b], n=25, seed=11, duplicates=1, nosology="dlbcl")
    assert summary["patients"] == 25
    cohort = analytics.load_cohort(world.db, world.curator)
    assert {pt["who_haem5"] for pt in cohort} == {"dlbcl"}
    assert all(pt["stage_system"] == "Lugano" for pt in cohort)
    d = analytics.dlbcl_dashboard(world.db, world.curator)
    assert d["treated"][0] >= 20 and d["responses"]
    assert "расходится" not in dict((k, n) for k, n, _ in d["ipi_agree"])   # IPI в карточке = расчёту
    computed = [registry.computed_indices(pt, registry._marker_map(pt["markers"]), pt["labs"]) for pt in cohort]
    assert sum(1 for ix in computed if ix[0]["result"]) >= 15        # у большинства IPI считается по данным


# --- веб ---------------------------------------------------------------------
@pytest.fixture
def app(world):
    return create_app(Settings(dsn=world.dsn, session_secret=b"s" * 32, secure_cookies=False), world.db)


def login(app, who):
    c = TestClient(app, follow_redirects=False)
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', c.get("/login").text).group(1)
    assert c.post("/login", data={"login": who, "password": TEST_PASSWORD, "csrf": token}).status_code == 303
    return c


def csrf(c, url):
    return re.search(r'name="csrf" value="([0-9a-f]+)"', c.get(url).text).group(1)


def test_web_new_patient_flow(app, world):
    c = login(app, "doc.a")
    chooser = c.get("/patients/new").text
    assert 'href="/patients/new?who=dlbcl"' in chooser and 'href="/patients/new?who=cll_sll"' in chooser
    page = c.get("/patients/new?who=dlbcl").text
    assert "Новый пациент — ДВКЛ" in page and 'value="IIE"' in page and "IPI" in page and 'value="A"' not in page
    form = dlbcl_form(world, index_score="5")
    r = c.post("/patients/new", data={**form, "csrf": csrf(c, "/patients/new?who=dlbcl")})
    assert r.status_code == 303, r.text
    card_url = r.headers["location"].split("?")[0]
    card = c.get(card_url).text
    assert "R-IPI" in card and "NCCN-IPI" in card and "Клеточное происхождение (Hans, ИГХ)" in card
    assert "CMR — полный метаболический ответ" in card and "R-CHOP" in card     # форма ответа и подсказки режимов
    assert "iwCLL" not in card


@pytest.mark.parametrize("url", ["/dashboard", "/dashboard/dlbcl", "/survival?grouping=nccn_ipi",
                                 "/survival?endpoint=PFS&grouping=deauville_eot1", "/survival?nosology=dlbcl"])
def test_web_dlbcl_pages(app, world, mixed, url):
    page = login(app, "curator").get(url)
    assert page.status_code == 200, url
    if "deauville_eot1" in url:
        assert "Анализ от ориентира" in page.text
