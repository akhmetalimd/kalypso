"""RLS 0019: быстрые политики чтения — те же права, что у app_patient_visible (0004)."""
import pytest

from test_db_rls import world2  # noqa: F401  (пациенты A и B, куратор, аналитик, неактивный врач)

READ_POLICIES = ["patient", "patient_missing", "module_data", "therapy_line", "observation", "lab",
                 "diagnosis_version", "patient_branch", "investigation"]


@pytest.fixture
def linked(reg, world2):  # noqa: F811
    """Пациент C филиала B, связанный с филиалом A (patient_branch), с анализом и исследованием."""
    pc = reg.patient("990000000303", branch="synth_b")
    reg.con.commit()
    reg.cur.execute("INSERT INTO patient_branch (patient_id, branch_code) VALUES (%s, 'synth_a')", (pc,))
    for pid in (world2["pa"], world2["pb"], pc):
        reg.cur.execute("INSERT INTO investigation (patient_id, kind, test_code, qual_result, source, provenance) "
                        "VALUES (%s, 'virology', 'hiv', 'negative', 'manual', 'retrospective')", (pid,))
    reg.cur.execute("INSERT INTO lab (patient_id, obs_date, loinc, value_raw, unit_raw, value_canonical, unit_canonical, "
                    "source, provenance) VALUES (%s, '2021-03-01', '8302-2', 130, 'g/L', 130, 'g/L', 'manual', "
                    "'retrospective')", (pc,))
    reg.con.commit()
    return {**world2, "pc": pc}


def _users(reg, w):
    return {"врач A": reg.doc, "врач B": reg.doc_b, "куратор": w["cur"], "аналитик": w["ana"],
            "админ": reg.admin, "неактивный врач": w["doc.x"], "без контекста": None}


def _as(reg, user):
    if user is None:
        reg.cur.execute("SET LOCAL ROLE kalypso_app")
    else:
        reg.as_app(user)


def test_same_visibility_as_app_patient_visible(reg, linked):
    every = [linked["pa"], linked["pb"], linked["pc"]]
    for name, user in _users(reg, linked).items():
        _as(reg, user)
        old = {r[0] for r in reg.all("SELECT id FROM unnest(%s::uuid[]) id WHERE app_patient_visible(id)", every)}
        new = {r[0] for r in reg.all("SELECT * FROM app_visible_patient_ids()")}
        reg.con.rollback()
        assert old == new, name


@pytest.mark.parametrize("table", ["patient", "lab", "investigation", "patient_branch"])
def test_rows_visible_per_role(reg, linked, table):
    expect = {"врач A": {linked["pa"], linked["pc"]}, "врач B": {linked["pb"], linked["pc"]},
              "куратор": {linked["pa"], linked["pb"], linked["pc"]}, "аналитик": {linked["pa"], linked["pb"], linked["pc"]},
              "админ": {linked["pa"], linked["pb"], linked["pc"]}, "неактивный врач": set(), "без контекста": set()}
    for name, user in _users(reg, linked).items():
        _as(reg, user)
        seen = {r[0] for r in reg.all(f"SELECT patient_id FROM {table}")}
        reg.con.rollback()
        assert seen == expect[name], (table, name)


def test_read_policies_use_set_function(reg):
    rows = reg.all("""SELECT c.relname, pg_get_expr(p.polqual, p.polrelid)
                      FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid
                      WHERE p.polcmd = 'r' AND p.polname = c.relname || '_select'""")
    quals = dict(rows)
    for table in READ_POLICIES:
        assert "app_visible_patient_ids()" in quals[table], table
        assert "app_patient_visible" not in quals[table], table
    assert "app_branch()" in quals["patient"]          # INSERT … RETURNING при регистрации


def test_function_is_not_public(reg):
    reg.cur.execute("SET LOCAL ROLE kalypso_app")
    assert reg.one("SELECT has_function_privilege('kalypso_app', 'app_visible_patient_ids()', 'EXECUTE')")
    assert not reg.one("SELECT has_function_privilege('public', 'app_visible_patient_ids()', 'EXECUTE')")
    reg.con.rollback()
