"""Веб П2: приём документа с предпросмотром, очередь и страница сверки, полнота, метрики."""
import re

import pytest
from fastapi.testclient import TestClient

from conftest import TEST_PASSWORD
from kalypso.ai.pipeline import process_pending
from kalypso.ai.rules import RuleExtractor
from kalypso.config import Settings
from kalypso.web.app import create_app

RAW = ("Пациент: Ахметов Ерлан Серикович, ИИН {iin}.\n"
       "Цитогенетическое исследование (FISH): del(17p): выявлена в 34% ядер; del(13q): не выявлена.")


@pytest.fixture
def app(world):
    settings = Settings(dsn=world.dsn, hmac_key=world.hasher.key, session_secret=b"s" * 32,
                        secure_cookies=False, ai_extractor="rules")
    return create_app(settings, world.db)


def client(app, login):
    c = TestClient(app, follow_redirects=False)
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', c.get("/login").text).group(1)
    assert c.post("/login", data={"login": login, "password": TEST_PASSWORD, "csrf": token}).status_code == 303
    return c


def csrf(c, url):
    return re.search(r'name="csrf" value="([0-9a-f]+)"', c.get(url).text).group(1)


def test_document_flow_to_registry(app, world):
    pid = world.register().record_id
    iin = world.iin()
    doc = client(app, "doc.a")
    new_url = f"/patients/{pid}/documents/new"
    assert doc.get(new_url).status_code == 200

    preview = doc.post(f"/patients/{pid}/documents/preview", data={
        "doc_type": "cytogenetics", "doc_date": "2022-05-01", "provenance": "retrospective",
        "text": RAW.format(iin=iin), "csrf": csrf(doc, new_url)})
    assert preview.status_code == 200
    assert iin not in preview.text and "Ахметов" not in preview.text and "[ФИО]" in preview.text
    clean = re.search(r'<textarea name="text"[^>]*>(.*?)</textarea>', preview.text, re.S).group(1)
    report = re.search(r'name="deid_report" value="([^"]*)"', preview.text).group(1)

    r = doc.post(f"/patients/{pid}/documents", data={
        "doc_type": "cytogenetics", "doc_date": "2022-05-01", "provenance": "retrospective",
        "text": clean.replace("&#34;", '"'), "deid_report": report.replace("&#34;", '"'), "confirm": "1",
        "csrf": csrf(doc, new_url)})
    assert r.status_code == 303 and "m=doc_queued" in r.headers["location"]
    assert "В очереди AI" in doc.get(f"/patients/{pid}").text
    assert doc.get("/review").status_code == 403                 # сверка — не для врача

    process_pending(world.db, RuleExtractor())
    cur = client(app, "curator")
    queue = cur.get("/review").text
    doc_id = re.search(r'href="/review/([0-9a-f-]{36})"', queue).group(1)
    page = cur.get(f"/review/{doc_id}")
    assert page.status_code == 200 and '<mark id="ev1"' in page.text and "Принять все" not in page.text
    for pr_id in re.findall(r'action="/review/proposal/([0-9a-f-]{36})"', page.text):
        token = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)
        r = cur.post(f"/review/proposal/{pr_id}", data={"action": "accept", "doc_id": doc_id, "csrf": token})
        assert r.status_code == 303
    card = doc.get(f"/patients/{pid}").text
    assert "Выявлена" in card and "Сверен" in card
    assert cur.get("/review/metrics").status_code == 200


def test_quality_page(app, world):
    world.register()
    c = client(app, "doc.a")
    page = c.get("/quality")
    assert page.status_code == 200 and "Нет TP53" in page.text
    assert c.get("/quality?flag=missing_TP53").text.count('href="/patients/') >= 1
