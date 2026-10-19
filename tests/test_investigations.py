"""Исследования карточки: ИФТ, генетика, вирусный статус, аутоиммунный скрининг (миграция 0018)."""
import json
import re

import psycopg
import pytest
from fastapi.testclient import TestClient

from conftest import MIGRATIONS, SCHEMA, TEST_PASSWORD, apply_sql, rejected
from kalypso import investigations as inv
from kalypso import registry
from kalypso.config import Settings
from kalypso.rbac import NotFound
from kalypso.registry import ValidationError
from kalypso.web.app import create_app

R = {"provenance": "retrospective"}


def add(world, pid, **data):
    return registry.add_investigation(world.db, world.doc_a, pid, {**R, **data}).record_id


# --- маркеры ИФТ «как в заключении» -------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    ("CD5+ CD19+ CD20 dim CD23+", [("CD5", "pos"), ("CD19", "pos"), ("CD20", "dim"), ("CD23", "pos")]),
    ("CD10−, CD200+; FMC7-", [("CD10", "neg"), ("CD200", "pos"), ("FMC7", "neg")]),
    ("CD20 bright CD38 слаб. CD30 частично", [("CD20", "bright"), ("CD38", "dim"), ("CD30", "partial")]),
    ("kappa+ lambda−", [("kappa", "pos"), ("lambda", "neg")]),
])
def test_parse_markers(text, expected):
    assert [(m["m"], m["r"]) for m in inv.parse_markers(text)] == expected


def test_parse_markers_rejects_unknown_and_formats():
    with pytest.raises(ValueError, match="не распознано"):
        inv.parse_markers("CD5+ клон большой CD19+")
    assert inv.parse_markers("  ") is None
    assert inv.format_markers(inv.parse_markers("CD5+ CD20 dim CD10−")) == "CD5+ CD20 слаб. CD10−"


# --- ввод и карточка ---------------------------------------------------------------
def test_sections_are_optional_and_records_shown(world):
    pid = world.register().record_id
    card = registry.get_card(world.db, world.doc_a, pid)
    assert all(card["inv"][k] == [] for k in inv.KINDS)
    assert not [f for f in card["flags"]["completeness"] if "inv" in f]        # не проводилось — не пропуск
    add(world, pid, kind="flow", test_code="flow_panel", material="blood", qual_result="positive", value="78.5",
        markers="CD5+ CD19+ CD20 dim CD23+", conclusion="Клональная популяция B-клеток.")
    add(world, pid, kind="virology", test_code="hbv_dna", value="1200")           # единица — из справочника
    add(world, pid, kind="autoimmune", test_code="ana", qual_result="positive", value="320", qual_detail="гомогенное свечение")
    card = registry.get_card(world.db, world.doc_a, pid)
    [flow] = card["inv"]["flow"]
    assert (float(flow["value"]), flow["unit"], flow["material"]) == (78.5, "%", "blood")
    assert inv.format_markers(flow["markers"]) == "CD5+ CD19+ CD20 слаб. CD23+"
    [hbv] = card["inv"]["virology"]
    assert (hbv["unit"], hbv["material"], hbv["qual_result"]) == ("МЕ/мл", None, None)
    assert card["inv"]["autoimmune"][0]["unit"] == "титр 1:"


@pytest.mark.parametrize("data, message", [
    ({"kind": "flow", "test_code": "karyotype", "qual_result": "positive"}, "Исследование"),
    ({"kind": "virology", "test_code": "hiv"}, "Укажите результат"),
    ({"kind": "flow", "test_code": "flow_panel", "markers": "CD5+ много клеток"}, "не распознано"),
    ({"kind": "genetics", "test_code": "karyotype", "qual_result": "positive",
      "conclusion": "пациент 610412412345"}, "ИИН"),
    ({"kind": "virology", "test_code": "hiv", "qual_result": "maybe"}, "Качественный результат"),
    ({"kind": "nope", "test_code": "hiv", "qual_result": "positive"}, "Неизвестный раздел"),
])
def test_validation(world, data, message):
    pid = world.register().record_id
    with pytest.raises(ValidationError) as e:
        add(world, pid, **data)
    assert any(message in err for err in e.value.errors), e.value.errors


def test_genetics_mismatch_with_module_field(world):
    pid = world.register().record_id
    registry.add_marker(world.db, world.doc_a, pid, {**R, "field_key": "del17p", "value": "not_detected",
                                                     "test_date": "2021-03-01"})
    add(world, pid, kind="genetics", test_code="fish_del17p", qual_result="positive", value="42", test_date="2021-03-05")
    card = registry.get_card(world.db, world.doc_a, pid)
    assert "inv_mismatch_del17p" in card["flags"]["quality"]
    from kalypso.i18n import flag
    assert "расходится с полем «del(17p), FISH»" in flag("inv_mismatch_del17p")
    registry.add_marker(world.db, world.doc_a, pid, {**R, "field_key": "del17p", "value": "detected",
                                                     "test_date": "2021-03-10"})
    assert "inv_mismatch_del17p" not in registry.get_card(world.db, world.doc_a, pid)["flags"]["quality"]


def test_edit_keeps_kind_delete_and_branch_isolation(world):
    pid = world.register().record_id
    rid = add(world, pid, kind="virology", test_code="anti_hcv", qual_result="negative")
    registry.update_investigation(world.db, world.doc_a, pid, rid,
                                  {**R, "kind": "flow", "test_code": "hcv_rna", "qual_result": "positive", "value": "5000"})
    rec = registry.get_record(world.db, world.doc_a, pid, "investigation", rid)
    assert (rec["kind"], rec["test_code"], rec["unit"]) == ("virology", "hcv_rna", "МЕ/мл")
    with pytest.raises(NotFound):
        registry.add_investigation(world.db, world.doc_b, pid, {**R, "kind": "virology", "test_code": "hiv",
                                                               "qual_result": "negative"})
    registry.soft_delete(world.db, world.doc_a, pid, "investigation", rid)
    assert registry.get_card(world.db, world.doc_a, pid)["inv"]["virology"] == []


def test_patients_filter_has_investigations(world):
    a, b = world.register().record_id, world.register().record_id
    add(world, a, kind="genetics", test_code="ighv", qual_result="negative")
    ids = lambda f: {r["patient_id"] for r in registry.list_patients(world.db, world.curator, filters=f)}  # noqa: E731
    assert ids({"has": "genetics"}) == {a} and ids({"has": "flow"}) == set() and ids({}) == {a, b}


def test_db_constraints(reg):
    pid = reg.patient("990000000905")
    reg.con.commit()
    for row, match in [({"kind": "imaging"}, "kind_check"), ({"test_code": "HIV!"}, "test_code_check"),
                       ({"qual_result": None}, "has_result"), ({"unit": "МЕ/мл"}, "unit_needs_value"),
                       ({"conclusion": "ИИН 990000000099"}, "text_no_iin")]:
        data = {"patient_id": pid, "kind": "virology", "test_code": "hiv", "qual_result": "positive",
                "source": "manual", "provenance": "retrospective", **row}
        reg.as_app()
        cols = ", ".join(data)
        with rejected(reg.con, match):
            reg.cur.execute(f"INSERT INTO investigation ({cols}) VALUES ({', '.join(['%s'] * len(data))})",
                            list(data.values()))


def test_viral_module_fields_moved_by_migration(make_db):
    """0018 переносит ВИЧ/EBV (ЛБ), HCV (ЛМЗ), ДНК EBV (ПТКЛ) из module_data в «Вирусный статус»."""
    idx = next(i for i, m in enumerate(MIGRATIONS) if m.name.startswith("0018"))
    with make_db() as con:
        apply_sql(con, SCHEMA)
        for m in MIGRATIONS[:idx]:
            apply_sql(con, m)
        con.execute("INSERT INTO branch (branch_code, name_ru) VALUES ('synth_a', 'A')")
        pids = {}
        for nos, icd, mkb, iin in (("burkitt", "9687/3", "C83.7", "990000000910"), ("mzl", "9699/3", "C88.4", "990000000911")):
            pids[nos] = con.execute(
                """INSERT INTO patient (iin, branch, sex, who_haem5, icd_o_3_2, mkb, dx_date, last_contact_date,
                                        provenance, status_last_contact, source)
                   VALUES (%s, 'synth_a', 'M', %s, %s, %s, '2021-03-01', '2024-01-01', 'retrospective', 'alive', 'manual')
                   RETURNING patient_id""", (iin, nos, icd, mkb)).fetchone()[0]
        for nos, key, value, missing in (("burkitt", "hiv", "detected", None), ("burkitt", "ebv", "not_detected", None),
                                         ("mzl", "hcv", None, "not_done"), ("burkitt", "myc_ig", "detected", None)):
            con.execute("""INSERT INTO module_data (patient_id, module, field_key, value, missing, source, provenance)
                           VALUES (%s, %s, %s, %s, %s, 'manual', 'retrospective')""", (pids[nos], nos, key, value, missing))
        apply_sql(con, MIGRATIONS[idx])
        rows = con.execute("SELECT test_code, qual_result, material FROM investigation ORDER BY test_code").fetchall()
        assert rows == [("ebv_eber", "negative", "tissue"), ("hiv", "positive", "serum")]   # код пропуска не переносится
        left = con.execute("SELECT field_key FROM module_data WHERE deleted_at IS NULL").fetchall()
        assert left == [("myc_ig",)]                                                         # остальное модуля — на месте


# --- веб --------------------------------------------------------------------------
def test_web_card_sections_add_and_edit(world):
    app = create_app(Settings(dsn=world.dsn, session_secret=b"s" * 32, secure_cookies=False), world.db)
    c = TestClient(app, follow_redirects=False)
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"login": "doc.a", "password": TEST_PASSWORD, "csrf": token})
    pid = world.register().record_id
    card = c.get(f"/patients/{pid}").text
    assert all(inv.kind_label(k) in card for k in inv.KINDS) and card.count("— не вносились") == 4
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', card).group(1)
    r = c.post(f"/patients/{pid}/investigations", data={**R, "kind": "flow", "test_code": "flow_panel",
                                                         "qual_result": "positive", "markers": "CD5+ CD23+", "csrf": token})
    assert r.status_code == 303
    card = c.get(f"/patients/{pid}").text
    assert "CD5+ CD23+" in card and "клональная популяция выявлена" in card
    rid = re.search(r'/edit/investigation/([0-9a-f-]{36})', card).group(1)
    form = c.get(f"/patients/{pid}/edit/investigation/{rid}").text
    assert 'value="CD5+ CD23+"' in form and "Правка исследования" in form
    r = c.post(f"/patients/{pid}/investigations", data={**R, "kind": "virology", "test_code": "hiv", "csrf": token})
    assert r.status_code == 422 and "Укажите результат" in r.text
    assert c.get("/patients?has=flow").status_code == 200
