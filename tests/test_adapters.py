"""Контрактные тесты адаптеров источника. Тот же набор обязан пройти HisKzAdapter (этап П5)."""
import json
from datetime import datetime, timezone

import pytest

from kalypso.ingest.synthetic_his import SyntheticHisAdapter, build_store
from source_adapter import HisKzAdapter, ManualAdapter, SourceAdapter

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


@pytest.fixture(params=["manual-dict", "manual-file", "synthetic"])
def adapter(request, tmp_path) -> SourceAdapter:
    store = build_store(n=4, seed=9, updated_at=T0)
    if request.param == "manual-dict":
        return ManualAdapter(store)
    if request.param == "manual-file":
        path = tmp_path / "export.json"
        path.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
        return ManualAdapter.from_file(path)
    return SyntheticHisAdapter(store)


def test_contract_shapes(adapter):
    ids = adapter.list_changes(None)
    assert ids and all(isinstance(i, str) for i in ids)
    rec = adapter.get_patient(ids[0])
    assert isinstance(rec["patient"], dict) and isinstance(rec["diagnoses"], list)
    for lab in adapter.get_labs(ids[0]):
        assert {"id", "code", "value", "unit", "date"} <= set(lab)
    for doc in adapter.get_documents(ids[0]):
        assert {"id", "type", "date", "text"} <= set(doc)


def test_contract_incremental(adapter):
    ids = adapter.list_changes(None)
    assert adapter.list_changes(T0) == ids                       # ровно на метке — включительно
    assert adapter.list_changes(datetime(2027, 1, 1, tzinfo=timezone.utc)) == []
    assert adapter.get_labs(ids[0], since=datetime(2027, 1, 1, tzinfo=timezone.utc)) == []


def test_contract_unknown_id_does_not_echo(adapter):
    with pytest.raises(KeyError) as e:
        adapter.get_patient("990101300123")
    assert "990101300123" not in str(e.value)


def test_hiskz_stub_is_honest():
    a = HisKzAdapter(base_url="")
    for call in (lambda: a.get_patient("x"), lambda: a.get_labs("x"), lambda: a.get_documents("x"),
                 lambda: a.list_changes(None)):
        with pytest.raises(NotImplementedError, match="П5"):
            call()
