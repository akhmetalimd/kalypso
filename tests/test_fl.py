"""Модуль ФЛ: FLIPI / FLIPI-2 / PRIMA-PI, POD24, грейд 3B, аналитика от ориентира, синтетика, веб."""
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import TEST_PASSWORD
from kalypso import analytics, docgen, registry, synthetic
from kalypso.config import Settings
from kalypso.registry import ValidationError
from kalypso.web.app import create_app
from registry_core import MISSING_CODES, flipi, flipi2, pod24, prima_pi

R = {"provenance": "retrospective"}
# 66 лет на диагнозе, Lugano IV, Hb 110 г/л, ЛДГ > ВГН, > 4 зон: FLIPI 5;
# FLIPI-2: возраст, Hb, β2M > ВГН, л/у > 6 см, КМ = 5; PRIMA-PI: β2M 3,5 мг/л > 3 — высокий.
FULL = {"grade_icc": "1_2", "nodal_areas": "gt_4", "largest_node": "gt_6", "bm_involvement": "yes",
        "ldh_uln": "uln_1_3", "b2m_uln": "gt_uln", "gelf": "yes"}
CHEMO = ["BR", "R-CHOP"]


def fl_form(world, **kw):
    base = {"who_haem5": "follicular", "birth_date": "1955-02-01", "dx_date": "2021-03-01",
            "stage_system": "Lugano", "stage_value": "IV", "ecog": "1"}
    return world.patient_form(**{**base, **kw})


def register(world, **kw):
    return registry.register_patient(world.db, world.doc_a, fl_form(world, **kw)).record_id


def add_fields(world, pid, fields, test_date="2021-03-05"):
    for key, val in fields.items():
        result = {"missing": val} if val in MISSING_CODES else {"value": val}
        registry.add_marker(world.db, world.doc_a, pid, {**R, "field_key": key, **result, "test_date": test_date})


def add_labs(world, pid, hb=110, b2m=3.5, obs_date="2021-03-02"):
    for loinc, value, unit in (("8302-2", hb, "g/L"), ("1952-1", b2m, "mg/L")):
        registry.add_lab(world.db, world.doc_a, pid, {**R, "loinc": loinc, "value": str(value), "unit": unit,
                                                      "obs_date": obs_date})


def index(card, name):
    return next(i for i in card["indices"] if i["name"] == name)["result"]


# --- движок ------------------------------------------------------------------
@pytest.mark.parametrize("args, score, group", [
    ((60, False, 130, "le_uln", False), 0, "low"),
    ((61, False, 130, "le_uln", False), 1, "low"),
    ((61, True, 130, "le_uln", False), 2, "intermediate"),
    ((61, True, 119, "le_uln", False), 3, "high"),
    ((61, True, 119, "gt_3_uln", True), 5, "high"),
])
def test_flipi(args, score, group):
    r = flipi(*args)
    assert (r["score"], r["group"]) == (score, group)
    assert flipi(61, True, 120, "le_uln", False)["score"] == 2          # Hb 120 — не «< 120»
    assert flipi(61, True, None, "le_uln", False) is None


@pytest.mark.parametrize("args, group", [
    ((50, 130, False, False, False), "low"),
    ((61, 130, False, False, False), "intermediate"),
    ((61, 110, True, False, False), "high"),
])
def test_flipi2(args, group):
    assert flipi2(*args)["group"] == group


@pytest.mark.parametrize("b2m, bm, group", [(2.0, False, "low"), (2.0, True, "intermediate"),
                                            (3.1, False, "high"), (3.1, None, "high"), (3.0, True, "intermediate")])
def test_prima_pi(b2m, bm, group):
    assert prima_pi(b2m, bm)["group"] == group
    assert prima_pi(2.0, None) is None and prima_pi(None, True) is None


def _pt(last="2025-01-01"):
    return {"last_contact_date": date.fromisoformat(last), "last_contact_precision": "day"}


LINE = [{"line_no": 1, "regimen": "BR", "start_date": date(2021, 4, 1), "start_precision": "day"}]


@pytest.mark.parametrize("obs, last, status", [
    ([{"event_type": "progression", "event_date": date(2022, 6, 1)}], "2025-01-01", "pod24"),
    ([{"event_type": "relapse", "event_date": date(2023, 3, 31)}], "2025-01-01", "pod24"),        # день 729
    ([{"event_type": "progression", "event_date": date(2023, 5, 1)}], "2025-01-01", "no_pod24"),
    ([], "2025-01-01", "no_pod24"),
    ([], "2022-12-01", "undetermined"),                                                         # < 24 мес.
    ([{"event_type": "death", "event_date": date(2022, 1, 1)}], "2022-01-01", "undetermined"),
])
def test_pod24(obs, last, status):
    assert pod24(_pt(last), LINE, obs, CHEMO)["status"] == status


def test_pod24_only_after_chemoimmunotherapy():
    rituximab = [{**LINE[0], "regimen": "Ритуксимаб (монотерапия)"}]
    assert pod24(_pt(), rituximab, [], CHEMO)["status"] == "not_applicable"
    assert pod24(_pt(), [], [], CHEMO)["status"] == "not_applicable"
    assert pod24(_pt(), [{**LINE[0], "regimen": " br "}], [], CHEMO)["status"] == "no_pod24"   # регистр/пробелы


# --- ввод и карточка ----------------------------------------------------------
def test_fl_registration_codes_and_flipi(world):
    pid = register(world, icd_o_3_2="9695/3", mkb="C82.0", index_score="3")
    pt = registry.get_card(world.db, world.doc_a, pid)["patient"]
    assert (pt["index_name"], pt["index_score"], pt["index_group"]) == ("FLIPI", 3, "high")
    with pytest.raises(ValidationError, match="от 0 до 5"):
        register(world, index_score="6")
    with pytest.raises(ValidationError, match="МКБ-10 не соответствует"):
        register(world, mkb="C83.3")


def test_fl_completeness_indices_and_grade_3b(world):
    pid = register(world, index_score="5")
    card = registry.get_card(world.db, world.doc_a, pid)
    assert {"missing_grade_icc", "missing_gelf", "missing_b2m_uln"} <= set(card["flags"]["completeness"])
    assert "missing_transformation" not in card["flags"]["completeness"]      # дополнительное поле
    assert [i["name"] for i in card["indices"]] == ["FLIPI", "FLIPI-2", "PRIMA-PI"]
    add_fields(world, pid, FULL)
    assert index(registry.get_card(world.db, world.doc_a, pid), "FLIPI") is None   # нет Hb — не считаем
    add_labs(world, pid)
    card = registry.get_card(world.db, world.doc_a, pid)
    assert not [f for f in card["flags"]["completeness"] if f.startswith("missing_")]
    assert (index(card, "FLIPI")["score"], index(card, "FLIPI")["group"]) == (5, "high")
    assert (index(card, "FLIPI-2")["score"], index(card, "PRIMA-PI")["group"]) == (5, "high")
    assert "fl_grade_3b" not in card["flags"]["quality"]
    add_fields(world, pid, {"grade_icc": "3B"}, test_date="2021-03-20")
    assert "fl_grade_3b" in registry.get_card(world.db, world.doc_a, pid)["flags"]["quality"]


def test_lab_far_from_diagnosis_not_used(world):
    pid = register(world)
    add_fields(world, pid, FULL)
    add_labs(world, pid, obs_date="2021-10-01")                  # через 7 мес. после диагноза
    card = registry.get_card(world.db, world.doc_a, pid)
    assert index(card, "FLIPI") is None and index(card, "PRIMA-PI") is None


def test_pod24_and_transformation_in_card(world):
    pid = register(world, last_contact_date="2024-06-01")
    lid = registry.add_line(world.db, world.doc_a, pid, {**R, "regimen": "BR", "start_date": "2021-04-01",
                                                         "end_date": "2021-09-01"}).record_id
    card = registry.get_card(world.db, world.doc_a, pid)
    assert card["module_info"][0]["text"].startswith("нет — без прогрессии")
    registry.add_observation(world.db, world.doc_a, pid, {**R, "obs_date": "2022-06-01", "event_type": "progression"})
    add_fields(world, pid, {"transformation": "yes"}, test_date="2022-06-10")
    card = registry.get_card(world.db, world.doc_a, pid)
    pod, tr = card["module_info"]
    assert pod["warn"] and "14.0 мес." in pod["text"]
    assert tr["title"] == "Трансформация в ДВКЛ" and "10.06.2022" in tr["text"]
    board = docgen.tumor_board_text(docgen.patient_summary(card))
    assert "POD24: да" in board and "Трансформация в ДВКЛ: да" in board
    assert str(lid)  # линия на месте
    cll = registry.get_card(world.db, world.doc_a, world.register().record_id)
    assert cll["module_info"] == []


# --- аналитика ---------------------------------------------------------------
@pytest.fixture
def cohort(world):
    """A: BR, прогрессия через 14 мес. (POD24), смерть 2023-06-01; FLIPI 5.
    B: BR, ремиссия (без POD24); 46 лет, стадия II, но Hb 110, ЛДГ > ВГН, > 4 зон — FLIPI 3.
    C: ритуксимаб — POD24 неприменим. D: без лечения (watch & wait). E: ДВКЛ."""
    def treated(regimen, **kw):
        pid = register(world, **kw)
        add_fields(world, pid, FULL)
        add_labs(world, pid)
        registry.add_line(world.db, world.doc_a, pid, {**R, "regimen": regimen, "start_date": "2021-04-01",
                                                       "end_date": "2021-09-01"})
        return pid
    a = treated("BR", index_score="5", last_contact_date="2023-06-01")
    registry.add_observation(world.db, world.doc_a, a, {**R, "obs_date": "2022-06-01", "event_type": "progression"})
    registry.add_observation(world.db, world.doc_a, a, {**R, "obs_date": "2023-06-01", "event_type": "death",
                                                        "death_cause": "lpd_related"})
    b = treated("BR", index_score="3", birth_date="1975-01-01", stage_value="II", last_contact_date="2025-01-01")
    c = treated("Ритуксимаб (монотерапия)", last_contact_date="2025-01-01")
    d = register(world, last_contact_date="2024-01-01")
    e = registry.register_patient(world.db, world.doc_a, world.patient_form(
        who_haem5="dlbcl", stage_system="Lugano", stage_value="IV", ecog="1")).record_id
    return {"a": a, "b": b, "c": c, "d": d, "e": e}


def groups(res):
    return {g["label"]: (g["n"], g["events"]) for g in res["groups"]}


def test_pod24_landmark_analysis(world, cohort):
    res = analytics.explore(world.db, world.curator, "OS", grouping="pod24")
    assert res["nosology"] == "follicular"
    assert groups(res) == {"POD24": (1, 1), "без POD24": (1, 0)}
    a = next(r for r in res["rows"] if r["patient_id"] == cohort["a"])
    assert str(a["start"]) == "2023-04-01" and a["days"] == 61                   # от 24 мес. до смерти
    assert res["landmark"]["missing"] == 2 and "24 мес." in res["landmark"]["text"]   # C и D
    pfs = analytics.explore(world.db, world.curator, "PFS", grouping="pod24")
    assert any("для PFS 1-й линии" in n for n in pfs["notes"])


def test_fl_groupings_and_shared_lugano(world, cohort):
    assert groups(analytics.explore(world.db, world.curator, "OS", grouping="flipi")) == {
        "Высокий": (2, 1), "нет FLIPI": (2, 0)}
    assert set(groups(analytics.explore(world.db, world.curator, "OS", grouping="prima_pi"))) == {
        "Высокий", "не определено на старте"}
    lug = analytics.explore(world.db, world.curator, "OS", grouping="lugano")
    assert lug["n_cohort"] == 5 and any("несколько нозологий" in n for n in lug["notes"])   # ФЛ + ДВКЛ, без ХЛЛ
    assert analytics.explore(world.db, world.curator, "OS", grouping="lugano",
                             filters={**analytics.parse_filters({}), "nosology": "follicular"})["n_cohort"] == 4


def test_fl_dashboard(world, cohort):
    d = analytics.fl_dashboard(world.db, world.curator)
    assert d["n"] == 4 and d["watch_wait"] == (1, 4) and d["pod24_known"] == (1, 2)
    assert dict((k, n) for k, n, _ in d["pod24"]) == {
        "POD24": 1, "без POD24": 1, "1-я линия не иммунохимиотерапия / без лечения": 2}
    assert dict((k, n) for k, n, _ in d["flipi_agree"])["совпадает"] == 2
    assert d["treated"] == (3, 4)


# --- синтетика ---------------------------------------------------------------
def test_synthetic_fl_cohort(world):
    assert synthetic.generate(world.db, [world.doc_a, world.doc_b], n=30, seed=19, duplicates=1,
                              nosology="follicular")["patients"] == 30
    cohort = analytics.load_cohort(world.db, world.curator)
    assert {pt["who_haem5"] for pt in cohort} == {"follicular"}
    d = analytics.fl_dashboard(world.db, world.curator)
    assert "расходится" not in dict((k, n) for k, n, _ in d["flipi_agree"])
    assert 0 < d["watch_wait"][0] < 30 and d["pod24_known"][1] > 0
    statuses = {registry.pod24_status(pt, pt["lines"], pt["observations"])["status"] for pt in cohort}
    assert {"no_pod24", "not_applicable"} <= statuses


# --- веб ---------------------------------------------------------------------
@pytest.fixture
def app(world):
    return create_app(Settings(dsn=world.dsn, session_secret=b"s" * 32, secure_cookies=False), world.db)


def login(app, who):
    c = TestClient(app, follow_redirects=False)
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', c.get("/login").text).group(1)
    assert c.post("/login", data={"login": who, "password": TEST_PASSWORD, "csrf": token}).status_code == 303
    return c


def test_web_fl_pages(app, world, cohort):
    c = login(app, "doc.a")
    assert 'href="/patients/new?who=follicular"' in c.get("/patients/new").text
    form = c.get("/patients/new?who=follicular").text
    assert "Новый пациент — Фолликулярная лимфома" in form and "FLIPI" in form and "9695/3" in form
    card = c.get(f"/patients/{cohort['a']}").text
    assert "FLIPI-2" in card and "PRIMA-PI" in card and "POD24" in card and "Грейд (ICC)" in card
    assert "Обинутузумаб + бендамустин" in card                                      # подсказки режимов
    for url in ("/dashboard/fl", "/survival?grouping=pod24", "/survival?nosology=follicular&grouping=flipi2"):
        assert c.get(url).status_code == 200, url
    assert "Анализ от ориентира" in c.get("/survival?grouping=pod24").text
