"""Демо-режим (вход без пароля, переключатель роли), главная, фильтры «Пациентов», группы нозологий;
тема, небольшой JS и анимации (прогрессивное улучшение)."""
import html
import json
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


# --- ретро/проспектив автоматически, ошибки у полей, шапка, сводка карточки ---------
def _form(world, **kw):
    f = world.patient_form(**kw)
    f.pop("provenance", None)                              # в формах этого поля больше нет
    return f


def test_provenance_is_derived_and_inherited(world):
    old = registry.register_patient(world.db, world.doc_a, _form(world, dx_date="2021-03-01")).record_id
    new = registry.register_patient(world.db, world.doc_a, _form(world, dx_date="2024-02-01",
                                                                 last_contact_date="2025-01-01")).record_id
    get = lambda pid: registry.get_card(world.db, world.doc_a, pid)  # noqa: E731
    assert get(old)["patient"]["provenance"] == "retrospective" and get(new)["patient"]["provenance"] == "prospective"
    registry.add_line(world.db, world.doc_a, new, {"regimen": "BR", "start_date": "2024-03-01"})
    registry.add_lab(world.db, world.doc_a, new, {"loinc": "8302-2", "value": "120", "unit": "g/L", "obs_date": "2024-02-02"})
    card = get(new)
    assert card["lines"][0]["provenance"] == card["labs"][0]["provenance"] == "prospective"
    assert registry.REGISTRY_START.isoformat() == "2024-01-01"


def test_validation_errors_are_bound_to_fields(world, demo):
    from kalypso.registry import ValidationError
    with pytest.raises(ValidationError) as e:
        registry.register_patient(world.db, world.doc_a, _form(world, ecog="9", sex=""))
    assert {"ecog", "sex"} <= set(e.value.fields) and "ECOG: от 0 до 4" in e.value.fields["ecog"]
    c = enter(demo)
    page = c.get("/patients/new?who=cll_sll").text
    r = c.post("/patients/new", data={**_form(world, ecog="9"), "who_haem5": "cll_sll", "csrf": csrf(page)})
    assert r.status_code == 422
    assert re.search(r'name="ecog"[^>]*>\{?\{?[^<]*<small class="field-err" role="alert">ECOG: от 0 до 4', r.text) or \
        '<small class="field-err" role="alert">ECOG: от 0 до 4</small>' in r.text
    assert 'name="provenance"' not in page


def test_header_card_summary_and_index_help(demo, world):
    pid = world.register(index_score="3").record_id
    c = enter(demo)
    patients = c.get("/patients").text
    assert '<a href="/patients" class="active" aria-current="page">Пациенты</a>' in patients
    assert "Синт. A" in patients and 'class="button primary nav-new"' in patients
    card = c.get(f"/patients/{pid}").text
    assert 'class="summary"' in card and 'class="toc"' in card and 'href="#s-inv"' in card and 'id="s-inv"' in card
    assert 'class="info" title="TP53 — 4' in card and 'class="to-top"' in card


# --- список пациентов: сортировка, плашки фильтров, короткие нозологии; «Ещё» в дашбордах ---
def test_patient_list_sort_chips_and_short_names(demo, world):
    world.register(dx_date="2019-05-01")
    world.register(dx_date="2022-05-01")
    c = enter(demo)
    asc = c.get("/patients?sort=dx").text
    desc = c.get("/patients?sort=-dx").text
    dates = lambda page: re.findall(r"<td>(\d\d\.\d\d\.\d{4})</td>\s*<td>", page)  # noqa: E731
    assert dates(asc)[0] == "01.05.2019" and dates(desc)[0] == "01.05.2022"
    assert 'aria-sort="ascending"' in asc and "Диагноз ▲" in asc and "sort=-dx" in asc
    filtered = c.get("/patients?nosology=cll_sll&stage=Binet:A&sort=dx").text
    chips = re.findall(r'<a class="chip" href="([^"]+)"[^>]*>([^<]+)<', filtered)
    assert [t.strip() for _, t in chips] == ["ХЛЛ / ЛМЛ", "стадия Binet A"]
    assert chips[0][0] == "/patients"                     # без нозологии — и без её стадии
    assert '<span class="nos-tag">ХЛЛ/ЛМЛ</span>' in c.get("/patients").text
    assert '<details class="filters-box" open>' in filtered


def test_dashboards_hide_details_under_more(demo, world):
    world.register()
    c = enter(demo)
    assert '<details class="more"><summary>Ещё: маркеры и иерархия FISH</summary>' in c.get("/dashboard/cll").text
    assert "Ещё: поля модуля" in c.get("/dashboard/m/mcl").text
    dl = c.get("/dashboard/dlbcl").text
    assert dl.count("<section") == dl.count("</section>")


def test_theme_toggle_script_and_no_inline_js(demo, world):
    """Тема: по системе + переключатель (виден только с JS); скрипт — внешний файл (CSP default-src 'self')."""
    c = enter(demo)
    r = c.get("/patients")
    page = r.text
    assert '<script src="/static/kalypso.js"></script>' in page
    assert re.findall(r"<script(?![^>]*\bsrc=)", page) == [] and " onclick=" not in page   # без встроенного JS
    assert re.search(r'<button type="button" id="theme-toggle"[^>]*hidden', page)
    assert "'unsafe-inline'" not in r.headers["content-security-policy"]
    js = c.get("/static/kalypso.js")
    assert js.status_code == 200 and "localStorage" in js.text and "IntersectionObserver" in js.text
    css = c.get("/static/style.css").text
    assert "var(--card)" not in css
    assert css.count(':root[data-theme="dark"]') >= 2 and css.count(':root:not([data-theme="light"])') == 2
    assert "@media (prefers-reduced-motion: no-preference)" in css and "[hidden] { display: none !important; }" in css


def test_charts_are_marked_for_draw_in_animation():
    import registry_core as core
    from kalypso import charts
    km = core.km_estimate([30, 60, 90], [1, 0, 1])
    svg = charts.km_chart([{"label": "A", "km": km}], "KM")
    assert '<path class="line s1" pathLength="1"' in svg
    assert 'class="bar s1 col"' in charts.column_chart([("2021", 3)], "Годы")


def test_home_art_and_progressive_filters(demo, world):
    c = enter(demo)
    home = c.get("/").text
    assert 'class="home-art"' in home and 'aria-hidden="true"' in home and 'class="km a" pathLength="1"' in home
    page = c.get("/patients").text
    stages = json.loads(html.unescape(re.search(r"data-stages='([^']+)'", page).group(1)))
    assert stages["follicular"]["Lugano"][0] == "I" and stages["hcl"] == {}
    assert 'class="stage-pick"' in page                                   # без нозологии — подсказка
    pid = world.register().record_id
    form = c.get(f"/patients/{pid}").text
    units = {}
    for raw in re.findall(r"data-units='([^']+)'", form):
        units.update(json.loads(html.unescape(raw)))
    assert units["hbv_dna"] == "МЕ/мл" and units["flow_panel"] == "%"
