"""Аналитика на БД: «Исследователь выживаемости», окно 90 дней, границы филиалов, дашборды, документы."""
import csv
import io
import re

import pytest
from fastapi.testclient import TestClient

from conftest import TEST_PASSWORD
from kalypso import analytics, docgen, registry
from kalypso.config import Settings
from kalypso.web.app import create_app

M = {"provenance": "retrospective"}


@pytest.fixture
def cohort(world):
    """A: IGHV немут. на диагнозе, линия + прогрессия, смерть. B: IGHV мут., но тест через 2 года.
    C (филиал B): без маркеров, жив."""
    a = world.register(dx_date="2020-01-01", last_contact_date="2021-01-01").record_id
    registry.add_marker(world.db, world.doc_a, a, {**M, "field_key": "IGHV", "value": "unmutated", "test_date": "2020-01-10"})
    registry.add_marker(world.db, world.doc_a, a, {**M, "field_key": "del17p", "value": "not_detected", "test_date": "2020-01-10"})
    registry.add_lab(world.db, world.doc_a, a, {**M, "loinc": "1952-1", "value": "5.0", "unit": "mg/L", "obs_date": "2019-12-20"})
    registry.add_line(world.db, world.doc_a, a, {**M, "regimen": "BR", "start_date": "2020-06-01", "end_date": "2020-12-01"})
    registry.add_observation(world.db, world.doc_a, a, {**M, "obs_date": "2021-06-01", "event_type": "progression"})
    registry.add_observation(world.db, world.doc_a, a, {**M, "obs_date": "2022-01-01", "event_type": "death",
                                                        "death_cause": "lpd_related"})
    b = world.register(dx_date="2020-01-01", last_contact_date="2024-01-01").record_id
    registry.add_marker(world.db, world.doc_a, b, {**M, "field_key": "IGHV", "value": "mutated", "test_date": "2022-01-01"})
    registry.add_lab(world.db, world.doc_a, b, {**M, "loinc": "1952-1", "value": "2.0", "unit": "mg/L", "obs_date": "2020-01-05"})
    c = world.register(doc=world.doc_b, dx_date="2021-01-01", last_contact_date="2023-01-01").record_id
    return {"a": a, "b": b, "c": c}


def groups(res):
    return {g["label"]: (g["n"], g["events"]) for g in res["groups"]}


def test_os_whole_cohort_by_role(world, cohort):
    assert groups(analytics.explore(world.db, world.curator, "OS")) == {"Вся когорта": (3, 1)}
    assert groups(analytics.explore(world.db, world.doc_a, "OS")) == {"Вся когорта": (2, 1)}
    assert groups(analytics.explore(world.db, world.doc_b, "OS")) == {"Вся когорта": (1, 0)}


def test_grouping_uses_baseline_window(world, cohort):
    res = analytics.explore(world.db, world.curator, "OS", grouping="IGHV")
    assert groups(res) == {"Немутирован": (1, 1), analytics.UNDEFINED: (2, 0)}   # тест B — через 2 года


def test_lab_threshold_grouping(world, cohort):
    res = analytics.explore(world.db, world.curator, "OS", grouping="lab", lab_loinc="1952-1", threshold=3.5)
    assert groups(res) == {"> 3.5": (1, 1), "≤ 3.5": (1, 0), analytics.UNDEFINED: (1, 0)}


def test_line_endpoints_and_applicability(world, cohort):
    res = analytics.explore(world.db, world.curator, "PFS", line_no=1)
    assert groups(res) == {"Вся когорта": (1, 1)} and res["not_applicable"] == 2
    ttft = analytics.explore(world.db, world.curator, "TTFT")
    assert groups(ttft) == {"Вся когорта": (3, 1)}


def test_excluded_contradictory_dates(world):
    pid = world.register(dx_date="2021-01-01", last_contact_date="2022-01-01").record_id
    registry.add_observation(world.db, world.doc_a, pid, {**M, "obs_date": "2020-06-01", "event_type": "death",
                                                          "death_cause": "unknown"})
    res = analytics.explore(world.db, world.doc_a, "OS")
    assert [e["reason"] for e in res["excluded"]] == ["окончание раньше начала отсчёта"]


def test_small_group_warning_and_csv(world, cohort):
    res = analytics.explore(world.db, world.curator, "OS")
    assert any("неустойчивы" in n for n in res["notes"])
    rows = list(csv.DictReader(io.StringIO(analytics.explore_csv(res))))
    assert len(rows) == 3 and set(rows[0]) == {"patient_id", "endpoint", "line_no", "group", "start", "end",
                                               "days", "event", "reason"}


def test_dashboards(world, cohort):
    ov = analytics.overview(world.db, world.curator)
    assert ov["n"] == 3 and ov["tiles"]["dead"] == (1, 3)
    assert dict((k, n) for k, n, _ in ov["by_branch"]) == {"synth_a": 2, "synth_b": 1}
    cll = analytics.cll_dashboard(world.db, world.curator)
    assert dict((k, n) for k, n, _ in cll["markers"]["IGHV"]) == {"Немутирован": 1, "Мутирован": 1, "Не внесено": 1}
    assert cll["treated"] == (1, 3) and cll["deaths"] == [("Связана с ЛПЗ", 1, False)]
    assert analytics.overview(world.db, world.doc_b)["n"] == 1


def test_docgen_tumor_board(world, cohort):
    card = registry.get_card(world.db, world.doc_a, cohort["a"])
    text = docgen.tumor_board_text(docgen.patient_summary(card))
    assert "Черновик" in text and "Линия 1: BR" in text and "Немутирован" in text and "Смерть" in text
    assert str(cohort["a"])[:8] in text


@pytest.fixture
def app(world):
    return create_app(Settings(dsn=world.dsn, session_secret=b"s" * 32,
                               secure_cookies=False), world.db)


def login(app, who):
    c = TestClient(app, follow_redirects=False)
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', c.get("/login").text).group(1)
    assert c.post("/login", data={"login": who, "password": TEST_PASSWORD, "csrf": token}).status_code == 303
    return c


def test_p3_pages(app, world, cohort):
    c = login(app, "doc.a")
    for url in ("/dashboard", "/dashboard/cll", "/survival", "/survival?endpoint=PFS&line_no=1&grouping=IGHV",
                "/survival?grouping=lab&lab=1952-1&threshold=3,5", f"/patients/{cohort['a']}/docs/tumor-board",
                f"/patients/{cohort['a']}/docs/epicrisis"):
        r = c.get(url)
        assert r.status_code == 200, url
    page = c.get("/survival?grouping=IGHV").text
    assert '<svg class="viz"' in page and "Число под риском" in page and "p-value" in page
    assert "Тезисы" in page
    card = c.get(f"/patients/{cohort['a']}").text
    assert "Таймлайн" in card and '<svg class="viz"' in card
    r = c.get("/survival.csv?endpoint=OS")
    assert r.headers["content-type"].startswith("text/csv") and r.text.count("\n") == 3   # заголовок + 2 своих
    assert c.get(f"/patients/{cohort['c']}/docs/tumor-board").status_code == 404        # чужой филиал


def test_results_listed_before_missing(world, cohort):
    ighv = analytics.cll_dashboard(world.db, world.curator)["markers"]["IGHV"]
    muted = [m for _, _, m in ighv]
    assert muted == sorted(muted)          # сначала результаты (False), затем пропуски (True)
