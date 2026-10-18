"""П4 на БД: коннектор -> промежуточная зона -> решения куратора; ПДн не утекают; инкремент; права."""
import re
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from conftest import TEST_PASSWORD
from kalypso import registry
from kalypso.config import Settings
from kalypso.ingest import apply as ingest
from kalypso.ingest.connector import run
from kalypso.ingest.mapping import load_mapping
from kalypso.ingest.synthetic_his import SyntheticHisAdapter, build_store
from kalypso.rbac import AccessDenied
from kalypso.registry import ValidationError
from kalypso.web.app import create_app

CFG = load_mapping()
T1 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _sql(world, sql, *params):
    with world.db.tx() as cur:
        cur.execute("RESET ROLE")
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else None


def _dump(world) -> str:
    tables = [r["table_name"] for r in _sql(world, "SELECT table_name FROM information_schema.tables "
                                                   "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")]
    return "\n".join(str(r) for t in tables for r in _sql(world, f"SELECT t::text AS row FROM {t} t"))


@pytest.fixture
def src(world):
    """3 пациента с ХЛЛ/ЛМЛ (первый уже в регистре) + 1 вне критериев; учреждения филиалов A и B."""
    known = world.patient_form()
    known_pid = registry.register_patient(world.db, world.hasher, world.doc_a, known).record_id
    store = build_store(n=3, seed=21, known_iins=[known["iin"]], excluded=1,
                        updated_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    for i, rec in enumerate(store.values()):
        rec["patient"]["facilityCode"] = ["F-ALM-01", "F-AST-02", "F-ALM-01", "F-AST-02"][i]
    # У известного пациента — тот же диагноз, что в регистре (C91.1)
    first = next(iter(store.values()))
    first["diagnoses"][0]["icd10"] = "C91.1"
    return {"adapter": SyntheticHisAdapter(store), "store": store, "known_pid": known_pid}


def items(world, **where):
    cond = " AND ".join(f"{k} = %s" for k in where) or "TRUE"
    return _sql(world, f"SELECT * FROM ingest_item WHERE {cond} ORDER BY created_at, kind", *where.values())


def test_run_stages_only_and_leaks_nothing(world, src):
    res = run(world.db, src["adapter"], CFG, world.hasher, now=T1)
    c = res["counts"]
    assert res["status"] == "ok"
    assert c["skipped_inclusion"] == 1 and c["patient_new"] == 2 and c["diagnosis_new"] == 1
    assert c["lab_new"] == 18 and c["document_new"] == 3
    assert _sql(world, "SELECT count(*) AS n FROM lab")[0]["n"] == 0            # в регистр ничего не записано
    assert _sql(world, "SELECT count(*) AS n FROM patient")[0]["n"] == 1
    dump = _dump(world)
    for rec in src["store"].values():
        p = rec["patient"]
        for secret in (p["iin"], p["address"], p["phone"], f"{p['lastName']} {p['firstName']}"):
            assert secret not in dump, secret
        for x in rec["labs"] + rec["documents"] + rec["diagnoses"]:
            assert f"'{x['id']}'" not in dump and f'"{x["id"]}"' not in dump       # внешние ID — только HMAC


def test_incremental_duplicates_and_updates(world, src):
    run(world.db, src["adapter"], CFG, world.hasher, now=T1)
    again = run(world.db, src["adapter"], CFG, world.hasher, now=T1 + timedelta(hours=1))
    assert again["counts"] == {}                                               # с прошлой метки ничего не менялось
    sid = next(iter(src["store"]))
    src["adapter"].touch_lab(sid, 0, "99,9", T1 + timedelta(hours=2))
    third = run(world.db, src["adapter"], CFG, world.hasher, now=T1 + timedelta(hours=3))
    c = third["counts"]
    assert c["lab_update"] == 1 and "lab_duplicate" not in c        # неизменённые анализы источник не отдаёт
    assert c["document_duplicate"] == 1 and c["diagnosis_duplicate"] == 1   # документы/диагнозы — без since
    assert len(items(world, status="superseded")) == 1


def test_source_failures(world, src):
    class Down(SyntheticHisAdapter):
        def list_changes(self, since=None):
            raise ConnectionError("нет связи")
    res = run(world.db, Down(src["store"]), CFG, world.hasher, now=T1)
    assert res["status"] == "failed" and _sql(world, "SELECT count(*) AS n FROM ingest_state")[0]["n"] == 0

    bad_sid = list(src["store"])[1]

    class Flaky(SyntheticHisAdapter):
        def get_labs(self, source_id, since=None):
            if source_id == bad_sid:
                raise TimeoutError()
            return super().get_labs(source_id, since)
    res = run(world.db, Flaky(src["store"]), CFG, world.hasher, now=T1)
    assert res["status"] == "ok" and res["counts"]["error"] == 1 and res["counts"]["patient_new"] == 1


def test_curator_applies_new_patient_then_data(world, src):
    run(world.db, src["adapter"], CFG, world.hasher, now=T1)
    new_patient = next(i for i in items(world, kind="patient") if i["payload"]["patient"]["branch"] == "synth_b")
    labs = [i for i in items(world, kind="lab", iin_hash=new_patient["iin_hash"])]
    with pytest.raises(ValidationError, match="сначала примите"):
        ingest.decide(world.db, world.curator, labs[0]["item_id"], True)
    with pytest.raises(AccessDenied):
        ingest.decide(world.db, world.doc_b, new_patient["item_id"], True)

    ingest.decide(world.db, world.curator, new_patient["item_id"], True)
    [pt] = _sql(world, "SELECT * FROM patient WHERE iin_hash = %s", new_patient["iin_hash"])
    assert (pt["branch"], pt["source"], pt["provenance"]) == ("synth_b", "his_structured", "retrospective")
    assert [r["patient_id"] for r in registry.list_patients(world.db, world.doc_b)] == [pt["patient_id"]]

    ingest.decide(world.db, world.curator, labs[0]["item_id"], True, note="сверено")
    [lab] = _sql(world, "SELECT * FROM lab WHERE patient_id = %s", pt["patient_id"])
    assert lab["source"] == "his_structured" and lab["source_record_id"] == labs[0]["record_key"]
    assert lab["value_canonical"] is not None
    with pytest.raises(ValidationError, match="уже есть решение"):
        ingest.decide(world.db, world.curator, labs[0]["item_id"], False)

    doc = next(i for i in items(world, kind="document", iin_hash=new_patient["iin_hash"]))
    with pytest.raises(ValidationError, match="Подтвердите"):
        ingest.decide(world.db, world.curator, doc["item_id"], True)
    ingest.decide(world.db, world.curator, doc["item_id"], True, confirm_text=True)
    [sd] = _sql(world, "SELECT * FROM source_document WHERE patient_id = %s", pt["patient_id"])
    assert sd["deid_passed"] and sd["source"] == "his_structured" and sd["status"] == "manual"


def test_update_item_edits_same_lab(world, src):
    run(world.db, src["adapter"], CFG, world.hasher, now=T1)
    sid = next(iter(src["store"]))        # известный пациент
    first = items(world, kind="lab", patient_id=src["known_pid"])[0]
    ingest.decide(world.db, world.curator, first["item_id"], True)
    src["adapter"].touch_lab(sid, 0, "77,7", T1 + timedelta(hours=2))
    run(world.db, src["adapter"], CFG, world.hasher, now=T1 + timedelta(hours=3))
    [upd] = [i for i in items(world, kind="lab", status="pending") if i["change"] == "update"]
    ingest.decide(world.db, world.curator, upd["item_id"], True)
    rows = _sql(world, "SELECT value_raw FROM lab WHERE patient_id = %s AND source_record_id = %s",
                src["known_pid"], first["record_key"])
    assert [float(r["value_raw"]) for r in rows] == [77.7]


def test_diagnosis_needs_reason_and_issues_block_apply(world, src):
    src["store"][next(iter(src["store"]))]["labs"][1]["code"] = "XYZ"      # неизвестный код
    run(world.db, src["adapter"], CFG, world.hasher, now=T1)
    [dx] = items(world, kind="diagnosis")
    with pytest.raises(ValidationError, match="причину"):
        ingest.decide(world.db, world.curator, dx["item_id"], True)
    ingest.decide(world.db, world.curator, dx["item_id"], True, note="совпадает с регистром")
    bad = next(i for i in items(world, kind="lab") if i["issues"])
    with pytest.raises(ValidationError, match="маппинга"):
        ingest.decide(world.db, world.curator, bad["item_id"], True)
    ingest.decide(world.db, world.curator, bad["item_id"], False, note="код уточняется")


def test_connector_role_cannot_touch_registry(world, src):
    [w] = _sql(world, "SELECT user_id FROM app_user WHERE login = 'his-connector'")
    with world.db.tx(w["user_id"]) as cur:
        cur.execute("SELECT count(*) AS n FROM patient")
        assert cur.fetchone()["n"] == 0                                         # не видит пациентов
    with world.db.tx(w["user_id"]) as cur, pytest.raises(psycopg.Error, match="row-level security"):
        cur.execute("""INSERT INTO lab (patient_id, obs_date, loinc, value_raw, unit_raw, value_canonical,
                       unit_canonical, source, provenance) VALUES (%s, '2021-03-01', '8302-2', 1, 'g/L', 1, 'g/L',
                       'his_structured', 'retrospective')""", (src["known_pid"],))
    with world.db.tx(world.curator.user_id) as cur, pytest.raises(psycopg.Error, match="row-level security"):
        cur.execute("""INSERT INTO lab (patient_id, obs_date, loinc, value_raw, unit_raw, value_canonical,
                       unit_canonical, source, provenance) VALUES (%s, '2021-03-01', '8302-2', 1, 'g/L', 1, 'g/L',
                       'manual', 'retrospective')""", (src["known_pid"],))


def test_ingest_page(world, src):
    run(world.db, src["adapter"], CFG, world.hasher, now=T1)
    app = create_app(Settings(dsn=world.dsn, hmac_key=world.hasher.key, session_secret=b"s" * 32,
                              secure_cookies=False), world.db)

    def login(who):
        c = TestClient(app, follow_redirects=False)
        tok = re.search(r'name="csrf" value="([0-9a-f]+)"', c.get("/login").text).group(1)
        assert c.post("/login", data={"login": who, "password": TEST_PASSWORD, "csrf": tok}).status_code == 303
        return c
    assert login("doc.a").get("/ingest").status_code == 403
    cur = login("curator")
    page = cur.get("/ingest?kind=patient")
    assert page.status_code == 200 and "Новый пациент" in page.text and "Применить" in page.text
    for rec in src["store"].values():
        assert rec["patient"]["iin"] not in page.text and rec["patient"]["lastName"] not in page.text
    item_id = re.search(r'action="/ingest/item/([0-9a-f-]{36})"', page.text).group(1)
    tok = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)
    r = cur.post(f"/ingest/item/{item_id}", data={"decision": "apply", "kind": "patient", "csrf": tok})
    assert r.status_code == 303
    assert login("admin").get("/ingest").status_code == 200        # админ видит журнал прогонов
