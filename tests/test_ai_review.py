"""П2 на БД: приём документов, конвейер с деградацией, сверка куратором, RLS куратора."""
import json

import psycopg
import pytest

from kalypso import documents, registry, review
from kalypso.ai.base import ExtractionFailed, FakeExtractor, Finding
from kalypso.ai.pipeline import process_pending
from kalypso.ai.rules import RuleExtractor
from kalypso.rbac import AccessDenied
from kalypso.registry import ValidationError

MOLECULAR = ("Пациент: Ахметов Ерлан Серикович, ИИН {iin}, тел. +7 701 555 12 34.\n"
             "IGHV: перестройка IGHV3-23, идентичность с зародышевой линией 94,1% — мутированный статус.\n"
             "TP53: выявлена мутация c.743G>A (p.Arg248Gln), VAF 23%.")
HISTOLOGY = ("Пациентка: Иванова Е.П.\nИГХ: CD5+, CD23+, cyclin D1-. Ki-67 10%.\n"
             "Заключение: Лимфома из малых лимфоцитов (ЛМЛ).\nВрач-патологоанатом: Ким А.С.")
PET = "ПЭТ-КТ с 18F-ФДГ. Заключение: Частичный метаболический ответ. Deauville 4."


def _sql(world, sql, *params):
    with world.db.tx() as cur:
        cur.execute("RESET ROLE")
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else None


def add_doc(world, pid, doc_type, raw, doctor=None, confirm=True, ai=True, doc_date="2022-02-01"):
    clean = documents.preview(raw)
    return documents.add_document(world.db, doctor or world.doc_a, pid, {
        "doc_type": doc_type, "doc_date": doc_date, "provenance": "retrospective", "text": clean.text,
        "deid_report": json.dumps(clean.report), "confirm": "1" if confirm else ""}, ai_enabled=ai)


def proposals(world, doc_id):
    return {r["target"]: r for r in _sql(world, "SELECT * FROM ai_proposal WHERE doc_id = %s", doc_id)}


@pytest.fixture
def pid(world):
    return world.register().record_id


# --- приём ------------------------------------------------------------------
def test_only_clean_text_is_stored(world, pid):
    iin = world.iin()
    res = add_doc(world, pid, "molecular", MOLECULAR.format(iin=iin))
    [doc] = _sql(world, "SELECT * FROM source_document WHERE doc_id = %s", res.record_id)
    assert res.status == "queued" and doc["deid_passed"]
    assert iin not in doc["text_deid"] and "Ахметов" not in doc["text_deid"] and "555 12 34" not in doc["text_deid"]
    assert doc["deid_report"]["name"] == 1 and doc["deid_report"]["iin"] == 1
    dump = "\n".join(str(r) for r in _sql(world, "SELECT a.* FROM audit_log a"))
    assert iin not in dump and "Ахметов" not in dump


def test_unconfirmed_or_ai_off_goes_manual(world, pid):
    assert add_doc(world, pid, "pet_ct", PET, confirm=False).status == "manual"
    assert add_doc(world, pid, "pet_ct", PET, ai=False).status == "manual"
    assert process_pending(world.db, RuleExtractor()) == 0          # в AI не попали


def test_text_is_rescrubbed_on_save(world, pid):
    # Текст из формы предпросмотра мог быть отредактирован — при сохранении он очищается повторно.
    res = documents.add_document(world.db, world.doc_a, pid, {
        "doc_type": "other", "doc_date": "2022-01-01", "provenance": "retrospective",
        "text": "Заключение. Контакт врача: x@y.kz, ИИН 520315400123", "confirm": "1",
        "deid_report": '{"name": 2, "<script>": "x"}'}, ai_enabled=True)
    [doc] = _sql(world, "SELECT text_deid, deid_report FROM source_document WHERE doc_id = %s", res.record_id)
    assert "x@y.kz" not in doc["text_deid"] and "520315400123" not in doc["text_deid"]
    assert doc["deid_report"] == {"name": 2, "email": 1, "iin": 1}     # мусор из скрытого поля отброшен


def test_db_blocks_ai_run_on_undeidentified_doc(world, pid):
    doc_id = add_doc(world, pid, "pet_ct", PET, confirm=False).record_id
    with pytest.raises(psycopg.Error, match="деидентификацию"):
        _sql(world, "INSERT INTO ai_run (doc_id, extractor, prompt_version, status, started_at) "
                    "VALUES (%s, 'fake', 'x', 'ok', now())", doc_id)


def test_physician_cannot_add_document_to_other_branch(world, pid):
    with pytest.raises(Exception):
        add_doc(world, pid, "pet_ct", PET, doctor=world.doc_b)


# --- конвейер ---------------------------------------------------------------
def test_pipeline_rules_proposals_and_conflict(world, pid):
    registry.add_marker(world.db, world.doc_a, pid, {"field_key": "TP53", "value": "wild_type",
                                                     "provenance": "retrospective"})
    doc_id = add_doc(world, pid, "molecular", MOLECULAR.format(iin=world.iin())).record_id
    assert process_pending(world.db, RuleExtractor()) == 1
    props = proposals(world, doc_id)
    assert {t: p["proposed"]["value"] for t, p in props.items()} == {"IGHV": "mutated", "TP53": "mutated"}
    assert props["TP53"]["conflict"] and props["TP53"]["current"] == {"value": "wild_type"}
    assert not props["IGHV"]["conflict"] and all(p["grounded"] for p in props.values())
    [doc] = _sql(world, "SELECT status FROM source_document WHERE doc_id = %s", doc_id)
    assert doc["status"] == "proposed"
    [run] = _sql(world, "SELECT * FROM ai_run WHERE doc_id = %s", doc_id)
    assert run["status"] == "ok" and run["extractor"] == "rules"


@pytest.mark.parametrize("error, status", [
    (ExtractionFailed("timeout", "таймаут"), "timeout"),
    (ExtractionFailed("refusal", "отказ"), "refusal"),
    (RuntimeError("bug"), "error"),
])
def test_pipeline_degrades_to_manual(world, pid, error, status):
    doc_id = add_doc(world, pid, "pet_ct", PET).record_id

    class Boom(FakeExtractor):
        def extract(self, doc_type, text):
            raise error
    assert process_pending(world.db, Boom()) == 1
    [doc] = _sql(world, "SELECT status, status_note FROM source_document WHERE doc_id = %s", doc_id)
    assert doc["status"] == "manual" and "вручную" in doc["status_note"]
    assert _sql(world, "SELECT status FROM ai_run WHERE doc_id = %s", doc_id)[0]["status"] == status
    documents.requeue(world.db, world.curator, doc_id)              # AI снова доступен — повторная отправка
    process_pending(world.db, RuleExtractor())
    assert _sql(world, "SELECT status FROM source_document WHERE doc_id = %s", doc_id)[0]["status"] == "proposed"


def test_pipeline_checks_values_and_grounding(world, pid):
    doc_id = add_doc(world, pid, "pet_ct", PET).record_id
    fake = FakeExtractor([
        Finding("deauville", "7", "Deauville 7", "high"),                      # недопустимое значение
        Finding("TP53", "mutated", "TP53", "high"),                             # не для ПЭТ-КТ
        Finding("response", "PR", "частичный ответ по данным КТ", "high"),      # цитаты нет в тексте
    ])
    process_pending(world.db, fake)
    props = proposals(world, doc_id)
    assert list(props) == ["response"]
    assert props["response"]["grounded"] is False and props["response"]["confidence"] == "low"
    assert "недопустимое" in _sql(world, "SELECT error FROM ai_run WHERE doc_id = %s", doc_id)[0]["error"]


def test_nothing_found_goes_manual(world, pid):
    doc_id = add_doc(world, pid, "pet_ct", "ПЭТ-КТ: без очагов патологического накопления.").record_id
    process_pending(world.db, FakeExtractor([]))
    assert _sql(world, "SELECT status FROM source_document WHERE doc_id = %s", doc_id)[0]["status"] == "manual"


# --- сверка -----------------------------------------------------------------
def _processed(world, pid, doc_type, raw):
    doc_id = add_doc(world, pid, doc_type, raw).record_id
    process_pending(world.db, RuleExtractor())
    return doc_id, proposals(world, doc_id)


def test_accept_edit_reject_marker(world, pid):
    doc_id, props = _processed(world, pid, "molecular", MOLECULAR.format(iin=world.iin()))
    review.get_review(world.db, world.curator, doc_id)                         # начало сверки
    review.decide(world.db, world.curator, props["IGHV"]["proposal_id"], "accept")
    review.decide(world.db, world.curator, props["TP53"]["proposal_id"], "edit", "wild_type", "проверено по NGS")
    markers = {m["field_key"]: m for m in registry.get_card(world.db, world.doc_a, pid)["markers"]}
    assert markers["IGHV"]["value"] == "mutated" and markers["IGHV"]["source"] == "ai_document"
    assert markers["IGHV"]["source_record_id"] == str(props["IGHV"]["proposal_id"])
    assert str(markers["IGHV"]["test_date"]) == "2022-02-01"
    assert markers["TP53"]["value"] == "wild_type"
    after = proposals(world, doc_id)
    assert (after["IGHV"]["status"], after["TP53"]["status"]) == ("accepted", "edited")
    assert after["TP53"]["review_note"] == "проверено по NGS"
    [doc] = _sql(world, "SELECT status, review_started_at, review_finished_at FROM source_document WHERE doc_id = %s", doc_id)
    assert doc["status"] == "reviewed" and doc["review_started_at"] and doc["review_finished_at"]
    with pytest.raises(ValidationError, match="уже есть решение"):
        review.decide(world.db, world.curator, props["IGHV"]["proposal_id"], "reject")
    m = review.metrics(world.db, world.curator)
    assert {r["target"]: r["accuracy"] for r in m["by_field"]} == {"IGHV": 1.0, "TP53": 0.0}


def test_reject_writes_nothing(world, pid):
    doc_id, props = _processed(world, pid, "molecular", MOLECULAR.format(iin=world.iin()))
    for p in props.values():
        review.decide(world.db, world.curator, p["proposal_id"], "reject", note="не тот пациент")
    assert registry.get_card(world.db, world.doc_a, pid)["markers"] == []


def test_only_curator_decides(world, pid):
    _, props = _processed(world, pid, "molecular", MOLECULAR.format(iin=world.iin()))
    for who in (world.doc_a, world.analyst, world.admin):
        with pytest.raises(AccessDenied):
            review.decide(world.db, who, props["IGHV"]["proposal_id"], "accept")


def test_histology_diagnosis_and_text(world, pid):
    doc_id, props = _processed(world, pid, "histology", HISTOLOGY)
    assert props["diagnosis"]["proposed"]["value"] == "sll" and props["diagnosis"]["conflict"]
    review.decide(world.db, world.curator, props["diagnosis"]["proposal_id"], "accept")
    review.decide(world.db, world.curator, props["histology_report"]["proposal_id"], "accept")
    card = registry.get_card(world.db, world.doc_a, pid)
    assert (card["patient"]["icd_o_3_2"], card["patient"]["mkb"]) == ("9670/3", "C83.0")
    assert card["diagnoses"][0]["version"] == 2 and "AI-предложение" in card["diagnoses"][0]["change_reason"]
    assert "Лимфома из малых лимфоцитов" in card["patient"]["histology_report"]
    assert "Иванова" not in card["patient"]["histology_report"]


def test_pet_ct_builds_one_observation(world, pid):
    doc_id, props = _processed(world, pid, "pet_ct", PET)
    review.decide(world.db, world.curator, props["deauville"]["proposal_id"], "accept")
    review.decide(world.db, world.curator, props["response"]["proposal_id"], "accept")
    [obs] = registry.get_card(world.db, world.doc_a, pid)["observations"]
    assert (obs["deauville"], obs["response_value"], obs["response_basis"], obs["source"]) == (4, "PR", "pet_ct", "ai_document")
    assert obs["pet_ct_report"] and obs["source_record_id"] == f"doc:{doc_id}"


def test_curator_cannot_overwrite_manual_marker(world, pid):
    registry.add_marker(world.db, world.doc_a, pid, {"field_key": "IGHV", "value": "unmutated",
                                                     "test_date": "2022-02-01", "provenance": "retrospective"})
    _, props = _processed(world, pid, "molecular", MOLECULAR.format(iin=world.iin()))
    with pytest.raises(ValidationError, match="уже внесён"):
        review.decide(world.db, world.curator, props["IGHV"]["proposal_id"], "accept")
    assert proposals(world, props["IGHV"]["doc_id"])["IGHV"]["status"] == "pending"


def test_curator_rls_and_card_guard(world, pid):
    lab = {"loinc": "8302-2", "value": "140", "unit": "g/L", "obs_date": "2021-03-01", "provenance": "retrospective"}
    with pytest.raises(AccessDenied):                     # ручной ввод — не право куратора
        registry.add_lab(world.db, world.curator, pid, lab)
    with world.db.tx(world.curator.user_id) as cur, pytest.raises(psycopg.Error, match="row-level security"):
        cur.execute("""INSERT INTO lab (patient_id, obs_date, loinc, value_raw, unit_raw, value_canonical,
                       unit_canonical, source, provenance) VALUES (%s, '2021-03-01', '8302-2', 1, 'g/L', 1, 'g/L',
                       'manual', 'retrospective')""", (pid,))
    with world.db.tx(world.curator.user_id) as cur, pytest.raises(psycopg.Error, match="только диагноз"):
        cur.execute("UPDATE patient SET ecog = 3 WHERE patient_id = %s", (pid,))


def test_close_manual_by_physician(world, pid):
    doc_id = add_doc(world, pid, "pet_ct", PET, ai=False).record_id
    documents.close_manual(world.db, world.doc_a, doc_id)
    assert _sql(world, "SELECT status FROM source_document WHERE doc_id = %s", doc_id)[0]["status"] == "reviewed"
