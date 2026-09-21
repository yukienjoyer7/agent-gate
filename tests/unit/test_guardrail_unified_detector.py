"""Focused contracts for the Ollama-backed unified detector."""

from __future__ import annotations

import json
import urllib.error
from typing import Any, Self

import pytest

from app.domains.guardrail._vendor.agentgate.decision import DecisionEngine
from app.domains.guardrail._vendor.agentgate.detectors import llm_client
from app.domains.guardrail._vendor.agentgate.detectors.llm_unified import (
    UNIFIED_RESPONSE_SCHEMA,
    LLMUnifiedDetector,
)
from app.domains.guardrail._vendor.agentgate.schemas import ActionRequest, Decision


class _Response:
    def __init__(self, payload: Any) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, _: int) -> bytes:
        return self.payload


class _Audit:
    def record(self, *_: Any) -> None:
        return None


def _request(**overrides: Any) -> ActionRequest:
    values: dict[str, Any] = {
        "action_type": "API_CALL",
        "target_system": "Telegram",
        "target": "self",
        "payload_summary": "Send one Telegram message",
        "content_context": "test AgentGate berhasil",
        "risk_hint": ["external_send"],
    }
    values.update(overrides)
    return ActionRequest(**values)


def _valid_response(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "pii": {"has_pii": False, "items": []},
        "secrets": {"has_secrets": False, "items": []},
        "source_code": {
            "has_code": False,
            "has_codename": False,
            "language": "",
            "confidence": 0.99,
        },
        "payment_phishing": {
            "has_payment": False,
            "has_credential_request": False,
            "has_urgency": False,
            "confidence": 0.99,
        },
        "prompt_injection": {"label": "benign", "confidence": 0.99},
        "action_intent": {
            "is_bulk": False,
            "estimated_count": 1,
            "is_destructive": False,
            "is_external_send": True,
            "confidence": 0.99,
        },
    }
    data.update(overrides)
    return data


def test_chat_json_sends_ollama_schema_and_parses_content(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def urlopen(request: Any, timeout: float) -> _Response:
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data)
        return _Response({"message": {"content": '{"ok": true}'}})

    monkeypatch.setattr(llm_client.urllib.request, "urlopen", urlopen)

    assert llm_client.chat_json(
        "system",
        "user",
        model="qwen2.5:7b",
        host="http://localhost:11434",
        timeout=12,
        response_schema={"type": "object"},
    ) == {"ok": True}
    assert captured["url"] == "http://localhost:11434/api/chat"
    assert captured["timeout"] == 12
    assert captured["body"]["format"] == {"type": "object"}
    assert captured["body"]["options"]["temperature"] == 0
    assert captured["body"]["stream"] is False


@pytest.mark.parametrize(
    ("failure", "error_type", "category"),
    [
        (
            urllib.error.URLError(ConnectionRefusedError("refused")),
            llm_client.LLMConnectionError,
            "connection_error",
        ),
        (TimeoutError("slow"), llm_client.LLMTimeoutError, "timeout"),
        (
            urllib.error.HTTPError("http://localhost", 500, "error", {}, None),
            llm_client.LLMHTTPError,
            "http_error",
        ),
    ],
)
def test_chat_json_categorizes_transport_failures(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, error_type: type[Exception], category: str
) -> None:
    def urlopen(*_: Any, **__: Any) -> _Response:
        raise failure

    monkeypatch.setattr(llm_client.urllib.request, "urlopen", urlopen)
    with pytest.raises(error_type) as raised:
        llm_client.chat_json("system", "user", host="http://localhost:11434")
    assert raised.value.category == category


@pytest.mark.parametrize(
    "payload,error_type",
    [
        ({}, llm_client.LLMInvalidResponseEnvelopeError),
        ({"message": {}}, llm_client.LLMInvalidResponseEnvelopeError),
        ({"message": {"content": "not json"}}, llm_client.LLMInvalidJSONError),
    ],
)
def test_chat_json_rejects_bad_ollama_envelopes(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any], error_type: type[Exception]
) -> None:
    monkeypatch.setattr(
        llm_client.urllib.request, "urlopen", lambda *_args, **_kwargs: _Response(payload)
    )
    with pytest.raises(error_type):
        llm_client.chat_json("system", "user", host="http://localhost:11434")


def test_valid_telegram_send_has_external_send_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_chat_json(*_: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return _valid_response()

    monkeypatch.setattr(llm_client, "chat_json", fake_chat_json)
    finding = LLMUnifiedDetector().scan(_request())

    assert "external_send" in finding.tags
    assert finding.triggered
    assert captured["response_schema"] is UNIFIED_RESPONSE_SCHEMA


@pytest.mark.parametrize(
    "response",
    [
        lambda: {key: value for key, value in _valid_response().items() if key != "pii"},
        lambda: {key: value for key, value in _valid_response().items() if key != "action_intent"},
        lambda: _valid_response(prompt_injection={"label": "other", "confidence": 0.9}),
        lambda: _valid_response(
            source_code={"has_code": False, "has_codename": False, "language": "", "confidence": 2}
        ),
        lambda: _valid_response(
            action_intent={
                "is_bulk": False,
                "estimated_count": "1",
                "is_destructive": False,
                "is_external_send": True,
                "confidence": 0.9,
            }
        ),
    ],
)
def test_unified_detector_rejects_schema_mismatches(
    monkeypatch: pytest.MonkeyPatch, response: Any
) -> None:
    monkeypatch.setattr(llm_client, "chat_json", lambda *_args, **_kwargs: response())
    with pytest.raises(llm_client.LLMResponseSchemaError):
        LLMUnifiedDetector().scan(_request())


@pytest.mark.parametrize(
    "response",
    [
        lambda: _valid_response(
            pii={"has_pii": False, "items": [{"type": "EMAIL", "value": "x", "severity": "LOW"}]}
        ),
        lambda: _valid_response(pii={"has_pii": True, "items": []}),
        lambda: _valid_response(
            secrets={
                "has_secrets": False,
                "items": [{"type": "JWT", "value": "masked", "severity": "HIGH"}],
            }
        ),
        lambda: _valid_response(
            action_intent={
                "is_bulk": False,
                "estimated_count": 20,
                "is_destructive": False,
                "is_external_send": True,
                "confidence": 0.9,
            }
        ),
    ],
)
def test_unified_detector_rejects_semantic_contradictions(
    monkeypatch: pytest.MonkeyPatch, response: Any
) -> None:
    monkeypatch.setattr(llm_client, "chat_json", lambda *_args, **_kwargs: response())
    with pytest.raises(llm_client.LLMContradictoryOutputError):
        LLMUnifiedDetector().scan(_request())


def test_invalid_unified_response_fails_closed_with_safe_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        llm_client,
        "chat_json",
        lambda *_args, **_kwargs: {"pii": {"has_pii": False, "items": []}},
    )
    result = DecisionEngine(detectors=[LLMUnifiedDetector()], audit_store=_Audit()).evaluate(
        _request()
    )

    assert result.decision == Decision.NEED_APPROVAL
    assert (
        result.evaluation_error == "Guardrail LLM detector returned an invalid structured response."
    )


def test_harmless_read_has_no_evaluation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    harmless = _valid_response(
        action_intent={
            "is_bulk": False,
            "estimated_count": 1,
            "is_destructive": False,
            "is_external_send": False,
            "confidence": 0.99,
        }
    )
    monkeypatch.setattr(llm_client, "chat_json", lambda *_args, **_kwargs: harmless)
    result = DecisionEngine(detectors=[LLMUnifiedDetector()], audit_store=_Audit()).evaluate(
        _request(action_type="FILE_READ", target_system="calendar", risk_hint=[])
    )

    assert result.decision == Decision.ALLOW
    assert result.evaluation_error is None
