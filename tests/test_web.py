"""Веб-интерфейс через TestClient: вход, CSRF, права, ввод пациента, ИИН не уходит в ответы."""
import re

import pytest
from fastapi.testclient import TestClient

from conftest import TEST_PASSWORD
from kalypso.config import Settings
from kalypso.web.app import COOKIE, create_app


@pytest.fixture
def app(world):
    settings = Settings(dsn=world.dsn, hmac_key=world.hasher.key, session_secret=b"s" * 32,
                        secure_cookies=False)
    return create_app(settings, world.db)


def login_post(c: TestClient, login: str, password: str = TEST_PASSWORD):
    """Вход как в браузере: GET формы (cookie + CSRF), затем POST."""
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', c.get("/login").text).group(1)
    return c.post("/login", data={"login": login, "password": password, "csrf": token})


def client(app, login=None) -> TestClient:
    c = TestClient(app, follow_redirects=False)
    if login:
        r = login_post(c, login)
        assert r.status_code == 303, r.text
    return c


def csrf(c: TestClient, url: str = "/patients") -> str:
    return re.search(r'name="csrf" value="([0-9a-f]+)"', c.get(url).text).group(1)


def test_requires_login(app):
    c = client(app)
    assert c.get("/patients").headers["location"] == "/login"
    r = login_post(c, "doc.a", "wrong")
    assert r.status_code == 401 and "Неверный логин или пароль" in r.text


def test_security_headers_and_cookie(app):
    c = TestClient(app, follow_redirects=False)
    r = login_post(c, "doc.a")
    cookie = r.headers["set-cookie"]
    assert COOKIE in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    page = c.get("/patients")
    assert page.headers["X-Frame-Options"] == "DENY" and "default-src 'self'" in page.headers["Content-Security-Policy"]


def test_post_without_csrf_rejected(app, world):
    c = client(app, "doc.a")
    r = c.post("/patients/new", data=world.patient_form())
    assert r.status_code == 403 and "Форма устарела" in r.text


def test_create_patient_flow(app, world):
    c = client(app, "doc.a")
    form = world.patient_form(histology_report="ХЛЛ, CD5+ CD23+")
    r = c.post("/patients/new", data={**form, "csrf": csrf(c)})
    assert r.status_code == 303 and r.headers["location"].startswith("/patients/")
    card_url = r.headers["location"].split("?")[0]
    card = c.get(card_url)
    assert card.status_code == 200 and "ХЛЛ, CD5+ CD23+" in card.text
    assert form["iin"] not in card.text and world.hasher.hash(form["iin"]) not in card.text

    # Поиск по ИИН — POST, результат — переход в карточку.
    r = c.post("/patients/search", data={"iin": form["iin"], "csrf": csrf(c)})
    assert r.headers["location"] == card_url

    # Анализ через форму карточки: канонизация видна.
    r = c.post(f"{card_url}/labs", data={"loinc": "8302-2", "value": "13.5", "unit": "g/dL",
                                         "obs_date": "2021-03-01", "provenance": "retrospective",
                                         "csrf": csrf(c, card_url)})
    assert r.status_code == 303
    assert "135.0 g/L" in c.get(card_url).text


def test_validation_error_does_not_echo_iin(app, world):
    c = client(app, "doc.a")
    form = world.patient_form(mkb="C83.3")
    r = c.post("/patients/new", data={**form, "csrf": csrf(c)})
    assert r.status_code == 422 and "МКБ-10 не соответствует" in r.text
    assert form["iin"] not in r.text


def test_other_branch_gets_404(app, world):
    pid = world.register().record_id
    c = client(app, "doc.b")
    assert c.get(f"/patients/{pid}").status_code == 404
    assert c.get("/patients/not-a-uuid").status_code == 404


@pytest.mark.parametrize("login, url, status", [
    ("doc.a", "/admin/users", 403),
    ("doc.a", "/audit", 403),
    ("doc.a", "/export/patients.csv", 403),
    ("analyst", "/patients/new", 403),
    ("curator", "/requests", 200),
    ("admin", "/admin/users", 200),
    ("admin", "/audit", 200),
    ("admin", "/audit/logins", 200),
    ("analyst", "/export", 200),
])
def test_role_pages(app, login, url, status):
    assert client(app, login).get(url).status_code == status


def test_analyst_export_csv(app, world):
    world.register()
    r = client(app, "analyst").get("/export/patients.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "patient_id,branch,sex,birth_year" in r.text


def test_must_change_password_redirect(app, world):
    with world.db.tx() as cur:
        cur.execute("UPDATE app_user SET must_change_password = true WHERE login = 'doc.b'")
    c = TestClient(app, follow_redirects=False)
    r = login_post(c, "doc.b")
    assert r.headers["location"] == "/password"
    assert c.get("/patients").headers["location"] == "/password"
    r = c.post("/password", data={"old": TEST_PASSWORD, "new": "brand-new-password", "new2": "brand-new-password",
                                  "csrf": csrf(c, "/password")})
    assert r.status_code == 303
    assert c.get("/patients").status_code == 200


def test_curator_approves_request_via_web(app, world):
    form = world.patient_form()
    world.register(iin=form["iin"])
    c = client(app, "doc.b")
    r = c.post("/patients/new", data={"iin": form["iin"], "csrf": csrf(c)})
    assert "m=exists_requested" in r.headers["location"]
    cur = client(app, "curator")
    page = cur.get("/requests").text
    rid = re.search(r'action="/requests/([0-9a-f-]+)"', page).group(1)
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)
    assert cur.post(f"/requests/{rid}", data={"decision": "approve", "csrf": token}).status_code == 303
    assert len(re.findall(r'href="/patients/[0-9a-f-]{36}"', c.get("/patients").text)) == 1


def test_login_requires_csrf(app):
    c = TestClient(app, follow_redirects=False)
    r = c.post("/login", data={"login": "doc.a", "password": TEST_PASSWORD})
    assert r.status_code == 403 and "Форма входа устарела" in r.text
    assert f"{COOKIE}=" not in r.headers.get("set-cookie", "")  # сессия не выдана
    forged = TestClient(app, follow_redirects=False)  # токен из чужой формы без своей cookie
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', c.get("/login").text).group(1)
    assert forged.post("/login", data={"login": "doc.a", "password": TEST_PASSWORD,
                                       "csrf": token}).status_code == 403


def test_edit_lab_via_web(app, world):
    from kalypso import registry
    pid = world.register().record_id
    rid = registry.add_lab(world.db, world.doc_a, pid, {"loinc": "8302-2", "value": "140", "unit": "g/L",
                                                        "obs_date": "2021-03-01", "provenance": "retrospective"}).record_id
    c = client(app, "doc.a")
    url = f"/patients/{pid}/edit/lab/{rid}"
    page = c.get(url)
    assert page.status_code == 200 and 'value="140' in page.text
    assert f'href="{url}"' in c.get(f"/patients/{pid}").text  # ссылка «изменить» в карточке
    r = c.post(url, data={"loinc": "8302-2", "value": "13", "unit": "g/dL", "obs_date": "2021-03-01",
                          "provenance": "retrospective", "change_reason": "опечатка", "csrf": csrf(c, url)})
    assert r.status_code == 303
    assert "130.0 g/L" in c.get(f"/patients/{pid}").text
    r = c.post(url, data={"loinc": "8302-2", "value": "", "unit": "g/dL", "obs_date": "2021-03-01",
                          "provenance": "retrospective", "csrf": csrf(c, url)})
    assert r.status_code == 422 and "Значение: обязательное поле" in r.text
    assert client(app, "doc.b").get(url).status_code == 404
