"""RLS (0004): второй рубеж — БД сама отсекает чужой филиал, даже если запрос приложения без фильтра."""
import pytest

from conftest import iin_hash, rejected

CHILD_TABLES = ["lab", "module_data", "therapy_line", "observation", "diagnosis_version", "patient_branch"]


@pytest.fixture
def world2(reg):
    """Пациент A (synth_a) и B (synth_b) с записями; куратор, аналитик, неактивный врач."""
    pa = reg.patient("990000000301")
    pb = reg.patient("990000000302", branch="synth_b")
    reg.con.commit()
    for pid in (pa, pb):  # записи — от владельца, мимо RLS
        reg.cur.execute("INSERT INTO lab (patient_id, obs_date, loinc, value_raw, unit_raw, value_canonical, "
                        "unit_canonical, source, provenance) VALUES (%s, '2021-03-01', '8302-2', 140, 'g/L', 140, "
                        "'g/L', 'manual', 'retrospective')", (pid,))
        reg.cur.execute("INSERT INTO module_data (patient_id, module, field_key, value, source, provenance) "
                        "VALUES (%s, 'cll', 'IGHV', 'mutated', 'manual', 'retrospective')", (pid,))
        reg.cur.execute("INSERT INTO therapy_line (patient_id, line_no, regimen, start_date, source, provenance) "
                        "VALUES (%s, 1, 'BR', '2021-04-01', 'manual', 'retrospective')", (pid,))
        reg.cur.execute("INSERT INTO observation (patient_id, obs_date, event_type, event_date, source, provenance) "
                        "VALUES (%s, '2023-01-01', 'progression', '2023-01-01', 'manual', 'retrospective')", (pid,))
    users = {}
    for login, role, branch, active in [("cur", "curator", None, True), ("ana", "analyst", None, True),
                                        ("doc.x", "physician", "synth_a", False)]:
        users[login] = reg.one("INSERT INTO app_user (login, display_name, password_hash, role_code, branch_code, "
                               "is_active) VALUES (%s, %s, 'x', %s, %s, %s) RETURNING user_id",
                               login, login, role, branch, active)
    reg.con.commit()
    return {"pa": pa, "pb": pb, **users}


def _ids(reg, user, table="patient"):
    if user is None:
        reg.cur.execute("SET LOCAL ROLE kalypso_app")  # без app.user_id
    else:
        reg.as_app(user)
    rows = {r[0] for r in reg.all(f"SELECT patient_id FROM {table}")}
    reg.con.rollback()
    return rows


@pytest.mark.parametrize("table", ["patient", *CHILD_TABLES])
def test_physician_sees_only_own_branch(reg, world2, table):
    assert _ids(reg, reg.doc, table) == {world2["pa"]}
    assert _ids(reg, reg.doc_b, table) == {world2["pb"]}


@pytest.mark.parametrize("who", ["cur", "ana", "admin"])
def test_readers_see_all_branches(reg, world2, who):
    user = reg.admin if who == "admin" else world2[who]
    assert _ids(reg, user) == {world2["pa"], world2["pb"]}
    assert _ids(reg, user, "lab") == {world2["pa"], world2["pb"]}


def test_no_context_or_inactive_user_sees_nothing(reg, world2):
    assert _ids(reg, None) == set()
    assert _ids(reg, world2["doc.x"]) == set()


def test_v_survival_respects_rls(reg, world2):
    assert _ids(reg, reg.doc, "v_survival") == {world2["pa"]}


def test_physician_cannot_touch_other_branch(reg, world2):
    reg.as_app(reg.doc)
    reg.cur.execute("UPDATE patient SET ecog = 1 WHERE patient_id = %s", (world2["pb"],))
    assert reg.cur.rowcount == 0  # строка просто не видна
    reg.cur.execute("UPDATE lab SET deleted_at = now() WHERE patient_id = %s", (world2["pb"],))
    assert reg.cur.rowcount == 0
    reg.con.rollback()
    with rejected(reg.con, "row-level security"):
        reg.lab(world2["pb"], loinc="8302-2", value_raw=1, unit_raw="g/L", value_canonical=1, unit_canonical="g/L")
    with rejected(reg.con, "row-level security"):
        reg.patient("990000000303", user=reg.doc, branch="synth_b")


@pytest.mark.parametrize("who", ["cur", "ana", "admin"])
def test_readers_cannot_write_clinical_data(reg, world2, who):
    user = reg.admin if who == "admin" else world2[who]
    if who == "cur":   # куратор видит карточку для сверки AI (0005), но страж пускает только диагноз/гистологию
        with rejected(reg.con, "только диагноз"):
            reg.as_app(user)
            reg.cur.execute("UPDATE patient SET ecog = 1 WHERE patient_id = %s", (world2["pa"],))
    else:
        reg.as_app(user)
        reg.cur.execute("UPDATE patient SET ecog = 1 WHERE patient_id = %s", (world2["pa"],))
        assert reg.cur.rowcount == 0
        reg.con.rollback()
    with rejected(reg.con, "row-level security"):
        reg.as_app(user)
        reg.cur.execute("INSERT INTO lab (patient_id, obs_date, loinc, value_raw, unit_raw, value_canonical, "
                        "unit_canonical, source, provenance) VALUES (%s, '2021-03-01', '8302-2', 1, 'g/L', 1, "
                        "'g/L', 'manual', 'retrospective')", (world2["pa"],))


def test_only_curator_links_branches(reg, world2):
    with rejected(reg.con, "row-level security"):
        reg.as_app(reg.doc)
        reg.cur.execute("INSERT INTO patient_branch (patient_id, branch_code) VALUES (%s, 'synth_a')", (world2["pb"],))
    reg.as_app(world2["cur"])
    reg.cur.execute("INSERT INTO patient_branch (patient_id, branch_code) VALUES (%s, 'synth_a')", (world2["pb"],))
    reg.con.commit()
    assert _ids(reg, reg.doc) == {world2["pa"], world2["pb"]}


def test_hash_lookup_reveals_only_id_to_physician(reg, world2):
    reg.as_app(reg.doc)
    assert reg.one("SELECT patient_id_by_hash(%s)", iin_hash("990000000302")) == world2["pb"]
    reg.con.rollback()
    reg.as_app(world2["cur"])
    assert reg.one("SELECT patient_id_by_hash(%s)", iin_hash("990000000302")) is None
    reg.con.rollback()


def test_access_requests_visibility(reg, world2):
    with rejected(reg.con, "row-level security"):  # только для своего филиала
        reg.as_app(reg.doc)
        reg.cur.execute("INSERT INTO branch_access_request (patient_id, branch_code) VALUES (%s, 'synth_b')",
                        (world2["pb"],))
    reg.as_app(reg.doc)
    reg.cur.execute("INSERT INTO branch_access_request (patient_id, branch_code) VALUES (%s, 'synth_a')",
                    (world2["pb"],))
    reg.con.commit()
    count = "SELECT count(*) FROM branch_access_request"
    for user, n in ((reg.doc, 1), (reg.doc_b, 0), (world2["cur"], 1)):
        reg.as_app(user)
        assert reg.one(count) == n
        reg.con.rollback()
