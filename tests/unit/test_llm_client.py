"""Tests for the provider-aware LLM client (app.llm.services.client).

Covers the Anthropic Messages API adapter (request translation + response
normalization) and the openai/anthropic dispatch — all hermetic, no network.
"""

import asyncio

import httpx

from app.config.settings import get_settings
from app.llm.services import client


def _openai_payload() -> dict:
    return {
        "model": "claude-x",
        "messages": [
            {"role": "system", "content": "You are a judge."},
            {"role": "user", "content": "review this"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "get_accessibility_tree",
                            "arguments": '{"url": "https://x.dev"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": '{"count": 1}'},
        ],
        "temperature": 0.2,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_accessibility_tree",
                    "description": "Inspect a page",
                    "parameters": {"type": "object", "properties": {"url": {"type": "string"}}},
                },
            }
        ],
    }


def test_to_anthropic_translates_messages_and_tools() -> None:
    body = client._to_anthropic(_openai_payload(), max_tokens=4096)

    assert body["model"] == "claude-x"
    assert body["max_tokens"] == 4096
    assert body["system"] == "You are a judge."
    assert body["temperature"] == 0.2

    # assistant tool_call -> tool_use block
    assistant = body["messages"][1]
    assert assistant["role"] == "assistant"
    tool_use = assistant["content"][0]
    assert tool_use["type"] == "tool_use"
    assert tool_use["name"] == "get_accessibility_tree"
    assert tool_use["input"] == {"url": "https://x.dev"}

    # role:tool -> user with tool_result
    tool_result = body["messages"][2]
    assert tool_result["role"] == "user"
    assert tool_result["content"][0]["type"] == "tool_result"
    assert tool_result["content"][0]["tool_use_id"] == "call_1"

    # tools translated to input_schema
    assert body["tools"][0] == {
        "name": "get_accessibility_tree",
        "description": "Inspect a page",
        "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}},
    }


def test_from_anthropic_normalizes_text_and_tool_use() -> None:
    data = {
        "content": [
            {"type": "text", "text": "plan is:"},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "submit_guardrail_decision",
                "input": {"decision": "ALLOW"},
            },
        ],
        "stop_reason": "tool_use",
    }
    normalized = client._from_anthropic(data)

    message = normalized["choices"][0]["message"]
    assert message["content"] == "plan is:"
    assert message["tool_calls"][0]["id"] == "toolu_1"
    assert message["tool_calls"][0]["function"]["name"] == "submit_guardrail_decision"
    assert message["tool_calls"][0]["function"]["arguments"] == '{"decision": "ALLOW"}'


def test_from_anthropic_tool_only_has_none_content() -> None:
    normalized = client._from_anthropic(
        {"content": [{"type": "tool_use", "id": "t", "name": "x", "input": {}}]}
    )
    message = normalized["choices"][0]["message"]
    assert message["content"] is None
    assert len(message["tool_calls"]) == 1


def test_post_chat_anthropic_sends_translated_body(monkeypatch) -> None:
    captured: dict = {}

    async def fake_post(self, url, json=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _Resp(
            {
                "content": [{"type": "text", "text": "{}"}],
                "stop_reason": "end_turn",
            }
        )

    monkeypatch.setenv("LLM_TYPE", "anthropic")
    monkeypatch.setenv("LLM_API_KEY", "sk-ant-test")
    monkeypatch.setenv("LLM_URL", "https://api.anthropic.com/v1/messages")
    monkeypatch.setenv("LLM_MODEL", "claude-x")
    get_settings.cache_clear()
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    try:
        data, tools_rejected = asyncio.run(client.post_chat(_openai_payload()))
    finally:
        get_settings.cache_clear()

    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "sk-ant-test"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in captured["headers"]
    assert captured["json"]["max_tokens"] == 4096
    assert captured["json"]["system"] == "You are a judge."
    assert not tools_rejected
    # response normalized to the OpenAI shape
    assert data["choices"][0]["message"]["content"] == "{}"


def test_post_chat_openai_uses_bearer_and_keeps_payload(monkeypatch) -> None:
    captured: dict = {}

    async def fake_post(self, url, json=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _Resp({"choices": [{"message": {"role": "assistant", "content": '{"plan": []}'}}]})

    monkeypatch.setenv("LLM_TYPE", "openai")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_URL", "https://omni.noctican.my.id/v1/chat/completions")
    get_settings.cache_clear()
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    try:
        data, tools_rejected = asyncio.run(client.post_chat({"model": "m", "messages": []}))
    finally:
        get_settings.cache_clear()

    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["json"]["model"] == "m"
    assert not tools_rejected
    assert data["choices"][0]["message"]["content"] == '{"plan": []}'


class _Resp:
    """Minimal stand-in for an httpx.Response."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._data
