from __future__ import annotations

import pytest

from app.domains.guardrail._vendor.agentgate.detectors.llm_client import (
    LLMFallbackError,
    LLMFallbackHandler,
    LLMTimeoutError,
)


def _handler(chat, unload):
    return LLMFallbackHandler(
        primary_model="qwen2.5:7b",
        fallback_model="gemma-4-E2B-it",
        host="http://localhost:11434",
        timeout=1,
        max_fallback_attempts=1,
        chat_fn=chat,
        unload_fn=unload,
    )


def test_primary_qwen_success_does_not_unload_or_call_fallback():
    calls: list[tuple[str, str]] = []

    def chat(_system, _user, *, model, **_kwargs):
        calls.append(("chat", model))
        return {"decision": "allow"}

    def unload(**_kwargs):
        calls.append(("unload", "qwen2.5:7b"))

    assert _handler(chat, unload).execute("system", "text") == {"decision": "allow"}
    assert calls == [("chat", "qwen2.5:7b")]


def test_qwen_timeout_unloads_before_gemma_success():
    calls: list[tuple[str, str]] = []

    def chat(_system, _user, *, model, **_kwargs):
        calls.append(("chat", model))
        if model == "qwen2.5:7b":
            raise LLMTimeoutError("primary timed out")
        return {"decision": "allow"}

    def unload(*, model, **_kwargs):
        calls.append(("unload", model))

    result = _handler(chat, unload).execute("system", "text")
    assert result == {"decision": "allow"}
    assert calls == [
        ("chat", "qwen2.5:7b"),
        ("unload", "qwen2.5:7b"),
        ("chat", "gemma-4-E2B-it"),
    ]


def test_qwen_timeout_and_gemma_failure_is_bounded():
    calls: list[tuple[str, str]] = []

    def chat(_system, _user, *, model, **_kwargs):
        calls.append(("chat", model))
        if model == "qwen2.5:7b":
            raise LLMTimeoutError("primary timed out")
        raise LLMTimeoutError("fallback timed out")

    def unload(*, model, **_kwargs):
        calls.append(("unload", model))

    with pytest.raises(LLMFallbackError, match="gemma-4-E2B-it.*failed after 1 attempt"):
        _handler(chat, unload).execute("system", "text")

    assert calls == [
        ("chat", "qwen2.5:7b"),
        ("unload", "qwen2.5:7b"),
        ("chat", "gemma-4-E2B-it"),
    ]
