"""Shared LLM HTTP client for all LLM-backed services.

Callers always speak one canonical "OpenAI-shaped" payload/response
(``messages`` with ``role``/``content``/``tool_calls``, ``tools``, optional
``response_format``). The client adapts that shape to the configured provider
(``LLM_TYPE``) and back:

- ``openai`` — OpenAI-compatible ``/chat/completions`` (Bearer auth). Also
  retries once without ``tools`` when the router rejects them with a 400.
- ``anthropic`` — Anthropic Messages API (``x-api-key`` auth): the payload is
  translated (system message → ``system`` field, tools → ``input_schema``,
  ``role: tool`` messages → ``tool_result`` blocks) and the response is
  normalized back to the OpenAI message shape.

Only ``LLM_URL``, ``LLM_MODEL``, ``LLM_API_KEY``, ``LLM_TYPE`` and
``LLM_TIMEOUT`` come from config — nothing provider-specific is hardcoded.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.config.settings import get_settings

logger = logging.getLogger(__name__)

ANTHROPIC_VERSION = "2023-06-01"


def _openai_headers() -> dict[str, str]:
    settings = get_settings()
    return {
        "Authorization": f"Bearer {settings.LLM_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://freebuff.com",
        "X-Title": "AgentGate",
    }


def _anthropic_headers() -> dict[str, str]:
    settings = get_settings()
    return {
        "x-api-key": settings.LLM_API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
        "Content-Type": "application/json",
    }


async def post_chat(
    payload: dict[str, Any],
    fallback_system_prompt: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """POST a canonical chat-completions payload to the configured provider.

    Returns ``(data, tools_rejected)``; ``data`` is normalized to the OpenAI
    response shape (``{"choices": [{"message": {...}}]}``) for every provider.
    ``tools_rejected`` is True only for the openai tools-retry fallback.
    """
    settings = get_settings()
    if settings.LLM_TYPE == "anthropic":
        return await _post_anthropic(payload, settings)
    return await _post_openai(payload, settings, fallback_system_prompt)


# ── OpenAI-compatible (default) ───────────────────────────────────


async def _post_openai(
    payload: dict[str, Any],
    settings: Any,
    fallback_system_prompt: str | None,
) -> tuple[dict[str, Any], bool]:
    url = settings.LLM_URL
    async with httpx.AsyncClient(timeout=httpx.Timeout(settings.LLM_TIMEOUT)) as client:
        response = await client.post(url, json=payload, headers=_openai_headers())
        error_text = getattr(response, "text", "") or ""
        if (
            getattr(response, "status_code", None) == 400
            and payload.get("tools")
            and ("tool" in error_text.lower() or "function" in error_text.lower())
        ):
            logger.warning("LLM router rejected tools parameter (400); retrying without tools")
            fallback = dict(payload)
            fallback.pop("tools", None)
            # Deep-copy messages so the caller's prompt is not mutated.
            fallback["messages"] = [dict(message) for message in (payload.get("messages") or [])]
            if fallback_system_prompt and fallback["messages"]:
                fallback["messages"][0]["content"] = fallback_system_prompt
            fallback["response_format"] = {"type": "json_object"}
            response = await client.post(url, json=fallback, headers=_openai_headers())
            response.raise_for_status()
            return response.json(), True
        response.raise_for_status()
        return response.json(), False


# ── Anthropic Messages API ────────────────────────────────────────


async def _post_anthropic(
    payload: dict[str, Any],
    settings: Any,
) -> tuple[dict[str, Any], bool]:
    body = _to_anthropic(payload, max_tokens=settings.LLM_MAX_TOKENS)
    async with httpx.AsyncClient(timeout=httpx.Timeout(settings.LLM_TIMEOUT)) as client:
        response = await client.post(settings.LLM_URL, json=body, headers=_anthropic_headers())
        response.raise_for_status()
        return _from_anthropic(response.json()), False


def _to_anthropic(payload: dict[str, Any], max_tokens: int) -> dict[str, Any]:
    """Translate the canonical OpenAI-shaped payload into an Anthropic body."""
    messages: list[dict[str, Any]] = []
    system_parts: list[str] = []

    for message in payload.get("messages") or []:
        role = message.get("role")
        if role == "system":
            system_parts.append(str(message.get("content") or ""))
        elif role == "tool":
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": str(message.get("tool_call_id") or ""),
                            "content": str(message.get("content") or ""),
                        }
                    ],
                }
            )
        elif role == "assistant" and message.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            if message.get("content"):
                blocks.append({"type": "text", "text": str(message["content"])})
            for tool_call in message["tool_calls"]:
                function = tool_call.get("function") or {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(tool_call.get("id") or ""),
                        "name": str(function.get("name") or ""),
                        "input": _json_loads(function.get("arguments") or "{}"),
                    }
                )
            messages.append({"role": "assistant", "content": blocks})
        else:
            messages.append({"role": role, "content": str(message.get("content") or "")})

    body: dict[str, Any] = {
        "model": payload["model"],
        "max_tokens": max_tokens,
        "messages": messages,
        "stream": False,
    }
    if system_parts:
        body["system"] = "\n\n".join(part for part in system_parts if part)
    if "temperature" in payload:
        body["temperature"] = payload["temperature"]
    if payload.get("tools"):
        body["tools"] = [
            {
                "name": tool["function"]["name"],
                "description": tool["function"].get("description", ""),
                "input_schema": tool["function"].get("parameters", {"type": "object"}),
            }
            for tool in payload["tools"]
            if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
        ]
    return body


def _from_anthropic(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize an Anthropic response into the OpenAI message shape."""
    content_blocks = data.get("content") or []
    text = "\n".join(
        str(block.get("text") or "") for block in content_blocks if block.get("type") == "text"
    )
    tool_calls: list[dict[str, Any]] = []
    for block in content_blocks:
        if block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": str(block.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name") or ""),
                        "arguments": json.dumps(block.get("input") or {}),
                    },
                }
            )
    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message}]}


def _json_loads(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def extract_message(data: dict[str, Any]) -> dict[str, Any]:
    """Return the assistant message dict from a (normalized) LLM response."""
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"unexpected LLM response shape: {exc}") from exc
    if not isinstance(message, dict):
        raise ValueError("unexpected LLM message shape")
    return message
