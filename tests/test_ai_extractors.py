"""Извлекатели без сети: правила на синтетике; Claude — с подменённым клиентом."""
import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from kalypso.ai import evaluate
from kalypso.ai.base import ExtractionFailed
from kalypso.ai.claude import FALLBACK_BETA, ClaudeExtractor
from kalypso.ai.rules import RuleExtractor


# --- правила ----------------------------------------------------------------
def test_rules_never_wrong_on_synthetic_set():
    items = evaluate.prepare(evaluate.generate(160, seed=5))
    report = evaluate.score(items, evaluate.run_local(RuleExtractor(), items))
    assert report["deid_clean"] == report["docs"]
    for field, s in report["fields"].items():
        assert s["wrong"] == 0, field              # базовая линия ошибается пропусками, не значениями
        assert s["grounded_rate"] in (None, 1.0)
    assert report["fields"]["IGHV"]["accuracy"] < 1.0   # «идентичность 95,1 %» правилам не по силам


@pytest.mark.parametrize("doc_type, text, expected", [
    ("cytogenetics", "del(17p): не выявлена; del(11q): выявлена в 22% ядер.", {"del17p": "not_detected", "del11q": "detected"}),
    ("cytogenetics", "Делеция 13q обнаружена (40% ядер). Трисомия 12 не обнаружена.", {"del13q": "detected", "tri12": "not_detected"}),
    ("molecular", "Мутаций в гене TP53 не выявлено.", {"TP53": "wild_type"}),
    ("molecular", "TP53: выявлена мутация c.743G>A, VAF 23%.", {"TP53": "mutated"}),
    ("molecular", "Мутационный статус IGHV: немутированный (99,1% гомологии).", {"IGHV": "unmutated"}),
    ("pet_ct", "Стабилизация, без признаков прогрессирования. По шкале Довиль — 4 балла.", {"response": "SD", "deauville": "4"}),
])
def test_rules_cases(doc_type, text, expected):
    got = {f.target: f.value for f in RuleExtractor().extract(doc_type, text).findings}
    assert got == expected


# --- Claude с подменённым клиентом ------------------------------------------
REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


class FakeMessages:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.kwargs = response, error, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.response


def _client(response=None, error=None):
    msgs = FakeMessages(response, error)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


def _resp(payload=None, stop_reason="end_turn", text=None):
    body = text if text is not None else json.dumps(payload, ensure_ascii=False)
    return SimpleNamespace(
        stop_reason=stop_reason, model="claude-opus-5-5", _request_id="req_test",
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=body)],
        usage=SimpleNamespace(input_tokens=1200, output_tokens=300, cache_read_input_tokens=900,
                              cache_creation_input_tokens=0),
        stop_details=SimpleNamespace(category="bio") if stop_reason == "refusal" else None)


DOC = "IGHV: идентичность 95,1%. TP53: мутации не обнаружены."


def test_claude_request_shape():
    client, msgs = _client(_resp({"findings": []}))
    ClaudeExtractor(client=client).extract("molecular", DOC)
    kw = msgs.kwargs
    assert kw["model"] == "claude-opus-5-5"
    assert kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}       # стабильная часть кэшируется
    schema = kw["output_config"]["format"]["schema"]
    assert schema["properties"]["findings"]["items"]["properties"]["target"]["enum"] == ["IGHV", "TP53"]
    assert kw["output_config"]["effort"] == "medium"
    assert DOC in kw["messages"][0]["content"] and DOC not in kw["system"][0]["text"]   # документ — после кэша


def test_claude_parses_findings_and_usage():
    payload = {"findings": [
        {"target": "IGHV", "value": "mutated", "evidence": "идентичность 95,1%", "confidence": "medium"},
        {"target": "TP53", "value": "wild_type", "evidence": "мутации не обнаружены", "confidence": "high"}]}
    client, _ = _client(_resp(payload))
    ex = ClaudeExtractor(client=client).extract("molecular", DOC)
    assert [(f.target, f.value) for f in ex.findings] == [("IGHV", "mutated"), ("TP53", "wild_type")]
    assert ex.request_id == "req_test" and ex.usage["cache_read_input_tokens"] == 900


@pytest.mark.parametrize("response, error, status", [
    (_resp(stop_reason="refusal", payload={}), None, "refusal"),
    (_resp(stop_reason="max_tokens", payload={}), None, "invalid"),
    (_resp(text="не JSON"), None, "invalid"),
    (_resp({"items": []}), None, "invalid"),
    (None, anthropic.APITimeoutError(request=REQ), "timeout"),
    (None, anthropic.APIConnectionError(request=REQ), "error"),
    (None, anthropic.APIStatusError("server", response=httpx2.Response(500, request=REQ), body=None), "error"),
])
def test_claude_failures_are_typed(response, error, status):
    client, _ = _client(response, error)
    with pytest.raises(ExtractionFailed) as e:
        ClaudeExtractor(client=client).extract("molecular", DOC)
    assert e.value.status == status


def test_estimate_does_not_call_api(monkeypatch):
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: pytest.fail("API не должен вызываться"))
    est = evaluate.estimate(evaluate.prepare(evaluate.generate(20, seed=2)))
    assert est["docs"] == 20 and est["usd_batch_upper"] > 0
