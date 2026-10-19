"""Демо-режим (вход без пароля, переключатель роли), главная, фильтры «Пациентов», группы нозологий."""
import re

import pytest
from fastapi.testclient import TestClient

from kalypso import registry
from kalypso.config import Settings
from kalypso.web.app import create_app


@pytest.fixture
def demo(world):
    return create_app(Settings(dsn=world.dsn, session_secret=b"s" * 32, secure_cookies=False, demo_mode=True), world.db)


@pytest.fixture
def normal(world):
    return create_app(Settings(dsn=world.dsn, session_secret=b"s" * 32, secure_cookies=False), world.db)


def csrf(page: str) -> str:
    return re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)


def enter(app) -> TestClient:
    c = TestClient(app, follow_redirects=False)
    r = c.get("/patients")
    assert r.status_code == 303 and r.headers["location"] == "/demo/enter?next=%2Fpatients"
    r = c.get(r.headers["location"])
    assert r.status_code == 303 and r.headers["location"] == "/patients"
    return c


def test_demo_enters_without_password_and_switches_role(demo, world):
    world.register()                                      # пациент филиала A
    world.register(doc=world.doc_b)                       # и филиала B
    c = enter(demo)
    page = c.get("/patients").text
    assert "Пациенты вашего филиала (synth_a)" in page and 'name="login"' in page   # первый врач, переключатель
    assert "Демо-режим" in page and "Выйти" not in page
    r = c.post("/demo/role", data={"login": "curator", "next": "/patients", "csrf": csrf(page)})
    assert r.status_code == 303 and r.headers["location"] == "/patients"
    page = c.get("/patients").text
    assert "Все филиалы." in page and '<option value="curator" selected>' in page
    assert c.get("/login").headers["location"] == "/"
    r = c.post("/demo/role", data={"login": "nobody", "next": "/", "csrf": csrf(page)})
    assert r.status_code == 404
    with world.db.tx() as cur:                           # вход виден в журнале входов
        cur.execute("SELECT count(*) AS n FROM login_log WHERE user_agent = 'demo-mode'")
        assert cur.fetchone()["n"] == 2


def test_demo_role_switch_requires_csrf(demo):
    c = enter(demo)
    assert c.post("/demo/role", data={"login": "curator", "next": "/"}).status_code == 403


def test_demo_off_by_default(normal):
    c = TestClient(normal, follow_redirects=False)
    assert c.get("/patients").headers["location"] == "/login"
    assert c.get("/demo/enter").status_code == 404


def test_home_page_name_and_decoding(demo):
    c = enter(demo)
    page = c.get("/").text
    assert 'class="home-title"' in page and "KALYPSO" in page
    letters = re.findall(r'<span class="acc">(\w)</span>', page)
    assert "".join(letters) == "KALYPSO"
    assert "zakhstan" in page and "mphoma" in page and "atients" in page and "utcomes" in page


def test_patient_filters(demo, world):
    world.register(dx_date="2019-05-01", stage_system="Binet", stage_value="A")
    world.register(dx_date="2022-05-01", stage_system="Binet", stage_value="C")
    world.register(dx_date="2022-06-01", who_haem5="dlbcl", stage_system="Lugano", stage_value="IV",
                   icd_o_3_2="9680/3", mkb="C83.3")
    c = enter(demo)
    count = lambda url: int(re.search(r"<h1>Пациенты <span class=\"muted\">(\d+)</span>", c.get(url).text).group(1))  # noqa: E731
    assert count("/patients") == 3
    assert count("/patients?nosology=dlbcl") == 1
    assert count("/patients?nosology=cll_sll&stage=Binet:C") == 1
    assert count("/patients?stage=Binet:C") == 3                       # без нозологии стадия не применяется
    assert count("/patients?nosology=cll_sll&year_from=2020") == 1
    assert count("/patients?completeness=complete") + count("/patients?completeness=gaps") == 3
    page = c.get("/patients?nosology=cll_sll").text
    assert 'value="Binet:C"' in page and 'value="Lugano:IV"' not in page   # стадии выбранной нозологии
    assert "<optgroup label=\"Агрессивные B-клеточные\">" in page


def test_filters_have_no_binet_and_dashboards_list_patients(demo, world):
    pid = world.register().record_id
    c = enter(demo)
    for url in ("/dashboard", "/survival", "/dashboard/cll", "/dashboard/m/mcl"):
        page = c.get(url).text
        assert 'name="binet"' not in page, url
    cll = c.get("/dashboard/cll").text
    assert "Пациенты нозологии" in cll and f'/patients/{pid}' in cll and 'value="Binet:A"' in cll
    overview = c.get("/dashboard").text
    assert overview.count('class="nos-group"') == len(registry.nosology_groups())
    assert all(f'href="{url}"' in overview for url in ("/dashboard/cll", "/dashboard/m/ptcl"))


def test_nosology_groups_cover_all_active():
    grouped = [n["id"] for _, nodes in registry.nosology_groups() for n in nodes]
    assert sorted(grouped) == sorted(n["id"] for n in registry.active_nodes())
    assert [t for t, _ in registry.nosology_groups()][:2] == ["Индолентные B-клеточные", "Агрессивные B-клеточные"]
