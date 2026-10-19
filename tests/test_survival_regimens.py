"""«Исследователь выживаемости»: нозология обязательна, режим линии N и класс режима, подгруппы."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from kalypso import analytics, docgen, registry
from kalypso.config import Settings
from kalypso.web.app import create_app

M = {"provenance": "retrospective"}


def groups(res):
    return {g["label"]: (g["n"], g["events"]) for g in res["groups"]}


@pytest.fixture
def treated(world):
    """ХЛЛ (синтетика). A: 70 лет, FCR с 2020-06, прогрессия, смерть. B: 51 год, ибрутиниб с 2021-01, жив.
    C: без лечения. D: ECOG 2, режим не из справочника с 2021-03, 2-я линия BR с 2022-03, смерть."""
    a = world.register(birth_date="1950-05-01", ecog="1", dx_date="2020-01-01", last_contact_date="2022-01-01").record_id
    registry.add_lab(world.db, world.doc_a, a, {**M, "loinc": "1952-1", "value": "5.0", "unit": "mg/L", "obs_date": "2020-01-05"})
    registry.add_line(world.db, world.doc_a, a, {**M, "regimen": "FCR", "start_date": "2020-06-01", "end_date": "2020-12-01"})
    registry.add_observation(world.db, world.doc_a, a, {**M, "obs_date": "2021-06-01", "event_type": "progression"})
    registry.add_observation(world.db, world.doc_a, a, {**M, "obs_date": "2022-01-01", "event_type": "death",
                                                        "death_cause": "lpd_related"})
    b = world.register(birth_date="1970-01-01", ecog="0", dx_date="2020-06-01", last_contact_date="2024-01-01").record_id
    registry.add_line(world.db, world.doc_a, b, {**M, "regimen": "ибрутиниб ", "start_date": "2021-01-01"})
    c = world.register(dx_date="2021-01-01", last_contact_date="2023-01-01").record_id
    d = world.register(birth_date="1960-01-01", ecog="2", dx_date="2020-10-01", last_contact_date="2023-01-01").record_id
    registry.add_line(world.db, world.doc_a, d, {**M, "regimen": "Экспериментальный режим", "start_date": "2021-03-01",
                                                 "end_date": "2021-09-01"})
    registry.add_line(world.db, world.doc_a, d, {**M, "regimen": "BR", "start_date": "2022-03-01"})
    registry.add_observation(world.db, world.doc_a, d, {**M, "obs_date": "2023-01-01", "event_type": "death",
                                                        "death_cause": "lpd_related"})
    return {"a": a, "b": b, "c": c, "d": d}


def explore(world, endpoint="OS", **kw):
    kw.setdefault("filters", {**analytics.parse_filters({}), "nosology": "cll_sll"})
    return analytics.explore(world.db, world.curator, endpoint, **kw)


def test_regimen_class_of_line_n(world, treated):
    res = explore(world, "PFS", grouping="regimen_class")
    assert groups(res) == {"Иммунохимиотерапия": (1, 1), "Ингибиторы BTK": (1, 0), "другое": (1, 1)}
    assert res["regimen_mode"] and res["not_applicable"] == 1                       # C — без 1-й линии
    assert analytics.grouping_label(res["grouping"], res["line_no"]) == "Класс режима 1-й линии"
    second = explore(world, "PFS", line_no=2, grouping="regimen")
    assert groups(second) == {"BR": (1, 1)} and second["not_applicable"] == 3


def test_os_by_regimen_counts_from_line_start(world, treated):
    res = explore(world, "OS", grouping="regimen")
    assert groups(res) == {"FCR": (1, 1), "ибрутиниб": (1, 0), "Экспериментальный режим": (1, 1)}
    row = next(r for r in res["rows"] if r["patient_id"] == treated["a"])
    assert row["start"] == date(2020, 6, 1) and row["days"] == (date(2022, 1, 1) - date(2020, 6, 1)).days
    assert res["not_applicable"] == 1 and not res["baseline_grouping"]
    old = explore(world, "OS", line_no=3, grouping="line1_regimen")                # прежняя ссылка
    assert old["grouping"] == "regimen" and old["line_no"] == 1


def test_ttft_is_not_compared_by_regimen(world, treated):
    res = explore(world, "TTFT", grouping="regimen_class")
    assert res["grouping"] == "none" and not res["regimen_mode"]
    assert any("TTFT" in n and "не сравнивается" in n for n in res["notes"])


def test_subgroup_restricts_comparison(world, treated):
    res = explore(world, "PFS", grouping="regimen_class", sub_by="age65", sub_val="> 65 лет")
    assert groups(res) == {"Иммунохимиотерапия": (1, 1)}
    assert dict(res["subgroup"]["values"]) == {"> 65 лет": 1, "≤ 65 лет": 2}
    ecog = explore(world, "OS", grouping="regimen", sub_by="ecog", sub_val="ECOG ≥ 2")
    assert groups(ecog) == {"Экспериментальный режим": (1, 1)}
    for bad in ("pod24", "regimen", "lab", "nope"):                                 # чужое и не подгруппа
        assert explore(world, "PFS", grouping="regimen", sub_by=bad, sub_val="x")["subgroup"] is None
    empty = explore(world, "PFS", grouping="regimen", sub_by="sex", sub_val="F")
    assert empty["groups"] == [] and any("нет пациентов" in n for n in empty["notes"])


def test_regimen_classes_cover_every_listed_regimen():
    for node in registry.active_nodes():
        for regimen in node.get("regimens", []):
            assert analytics.regimen_class(node["id"], regimen) not in (analytics.OTHER_CLASS, analytics.NO_REGIMEN), \
                (node["id"], regimen)
    assert analytics.regimen_class("follicular", "  обинутузумаб   + chop ") == "Иммунохимиотерапия"
    assert analytics.regimen_class("dlbcl", "FCR") == analytics.OTHER_CLASS     # режим чужой нозологии
    assert analytics.regimen_class("dlbcl", "") == analytics.NO_REGIMEN


def test_menus_are_limited_to_nosology(world, treated):
    menu = dict(analytics.grouping_menu("cll_sll"))
    own = menu[registry.profile("cll_sll")["name"]]
    assert "IGHV" in own and "flipi" not in own and "regimen_class" in menu["Терапия (сравнение режимов)"]
    assert "pod24" not in analytics.subgroup_options("follicular") and "flipi" in analytics.subgroup_options("follicular")
    assert explore(world)["labs_available"] == ["1952-1"]                       # только внесённые у нозологии


def test_abstract_mentions_indication_bias_and_subgroup(world, treated):
    res = explore(world, "PFS", grouping="regimen_class", sub_by="age65", sub_val="> 65 лет")
    text = docgen.cohort_abstract(res, "все доступные филиалы", analytics.grouping_label("regimen_class", 1))
    assert "от старта 1-й линии" in text and "по показаниям" in text and "Подгруппа: возраст" in text


# --- веб -----------------------------------------------------------------------------
@pytest.fixture
def client(world):
    app = create_app(Settings(dsn=world.dsn, session_secret=b"s" * 32, secure_cookies=False, demo_mode=True), world.db)
    c = TestClient(app, follow_redirects=False)
    c.get(c.get("/patients").headers["location"])                               # демо-вход: врач филиала A
    return c


def test_web_requires_nosology(client, treated):
    page = client.get("/survival?endpoint=PFS").text
    assert "Сначала выберите нозологию" in page and '<svg class="viz"' not in page
    assert 'href="/survival?nosology=cll_sll&amp;endpoint=PFS"' in page
    multi = client.get("/survival?grouping=deauville_eot1").text                  # группировка двух нозологий
    assert registry.profile("pmbcl")["name"] in multi and registry.profile("cll_sll")["name"] not in multi
    assert '<svg class="viz"' in client.get("/survival?grouping=IGHV").text       # одна нозология — сама
    assert client.get("/survival.csv?endpoint=OS").status_code == 400
    assert client.get("/survival.csv?endpoint=OS&nosology=cll_sll").status_code == 200


def test_web_regimen_comparison_page(client, treated):
    page = client.get("/survival?nosology=cll_sll&endpoint=PFS&grouping=regimen_class&sub_by=ecog").text
    assert "смещение по показаниям" in page and "Классы режимов (справочник нозологии)" in page
    assert "PFS от старта 1-й линии · Класс режима 1-й линии" in page
    assert 'value="flipi"' not in page and 'value="IGHV"' in page                  # только группировки ХЛЛ
    assert 'name="sub_val"' in page and "ECOG ≥ 2 (n=1)" in page
    assert '<option value="1952-1"' in page and '<option value="718-7"' not in page  # анализы — внесённые
    assert '<option value="">все</option>' not in page.split('name="nosology"')[1].split("</select>")[0]
