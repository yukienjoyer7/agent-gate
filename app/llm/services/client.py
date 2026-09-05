"""Shared LLM HTTP client for all LLM-backed services.

Callers always speak one canonical "OpenAI-shaped" payload/response:
messages, tools, response_format, etc.

Providers:
- openai: OpenAI-compatible /chat/completions
- anthropic: Anthropic Messages API
- gemini: Gemini Interactions API

All provider responses are normalized back into the canonical
OpenAI-shaped response:
{"choices": [{"message": {...}}]}
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.config.settings import get_settings

logger = logging.getLogger(__name__)

ANTHROPIC_VERSION = "2023-06-01"

# Gemini function-call ID -> Gemini interaction ID.
# This lets the existing AgentGate tool loop continue working without
# exposing Gemini-specific state to the rest of the application.
_gemini_tool_interactions: dict[str, dict[str, str]] = {}


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


def _gemini_headers() -> dict[str, str]:
    settings = get_settings()
    return {
        "x-goog-api-key": settings.LLM_API_KEY,
        "Content-Type": "application/json",
    }


async def post_chat(
    payload: dict[str, Any],
    fallback_system_prompt: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """POST a canonical chat-completions payload to the configured provider."""
    settings = get_settings()

    if settings.LLM_TYPE == "anthropic":
        return await _post_anthropic(payload, settings)

    if settings.LLM_TYPE == "gemini":
        return await _post_gemini(payload, settings)

    return await _post_openai(payload, settings, fallback_system_prompt)


# ── OpenAI-compatible ──────────────────────────────────────────────


async def _post_openai(
    payload: dict[str, Any],
    settings: Any,
    fallback_system_prompt: str | None,
) -> tuple[dict[str, Any], bool]:
    url = settings.LLM_URL

    async with httpx.AsyncClient(timeout=httpx.Timeout(settings.LLM_TIMEOUT)) as client:
        response = await client.post(
            url,
            json=payload,
            headers=_openai_headers(),
        )

        error_text = getattr(response, "text", "") or ""

        if (
            getattr(response, "status_code", None) == 400
            and payload.get("tools")
            and ("tool" in error_text.lower() or "function" in error_text.lower())
        ):
            logger.warning("LLM router rejected tools parameter (400); retrying without tools")

            fallback = dict(payload)
            fallback.pop("tools", None)
            fallback["messages"] = [dict(message) for message in (payload.get("messages") or [])]

            if fallback_system_prompt and fallback["messages"]:
                fallback["messages"][0]["content"] = fallback_system_prompt

            fallback["response_format"] = {"type": "json_object"}

            response = await client.post(
                url,
                json=fallback,
                headers=_openai_headers(),
            )

            response.raise_for_status()
            return response.json(), True

        response.raise_for_status()
        return response.json(), False


# ── Anthropic Messages API ────────────────────────────────────────


async def _post_anthropic(
    payload: dict[str, Any],
    settings: Any,
) -> tuple[dict[str, Any], bool]:
    body = _to_anthropic(
        payload,
        max_tokens=settings.LLM_MAX_TOKENS,
    )

    async with httpx.AsyncClient(timeout=httpx.Timeout(settings.LLM_TIMEOUT)) as client:
        response = await client.post(
            settings.LLM_URL,
            json=body,
            headers=_anthropic_headers(),
        )

        response.raise_for_status()
        return _from_anthropic(response.json()), False


def _to_anthropic(
    payload: dict[str, Any],
    max_tokens: int,
) -> dict[str, Any]:
    """Translate canonical OpenAI-shaped payload into Anthropic body."""
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
                blocks.append(
                    {
                        "type": "text",
                        "text": str(message["content"]),
                    }
                )

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

            messages.append(
                {
                    "role": "assistant",
                    "content": blocks,
                }
            )

        else:
            messages.append(
                {
                    "role": role,
                    "content": str(message.get("content") or ""),
                }
            )

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
                "input_schema": tool["function"].get(
                    "parameters",
                    {"type": "object"},
                ),
            }
            for tool in payload["tools"]
            if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
        ]

    return body


def _from_anthropic(
    data: dict[str, Any],
) -> dict[str, Any]:
    """Normalize Anthropic response into OpenAI message shape."""
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

    message: dict[str, Any] = {
        "role": "assistant",
        "content": text or None,
    }

    if tool_calls:
        message["tool_calls"] = tool_calls

    return {
        "choices": [
            {
                "message": message,
            }
        ]
    }


# ── Gemini Interactions API ───────────────────────────────────────


async def _post_gemini(
    payload: dict[str, Any],
    settings: Any,
) -> tuple[dict[str, Any], bool]:
    """Send a canonical AgentGate payload through Gemini Interactions API.

    Gemini Interactions is stateful for tool continuations. The first
    request creates an interaction. When Gemini returns a function_call,
    its interaction ID is remembered by function-call ID. When AgentGate
    subsequently sends the corresponding tool message, this function
    converts that into a Gemini function_result using previous_interaction_id.
    """
    body = _to_gemini(payload)

    tool_messages = [
        message for message in (payload.get("messages") or []) if message.get("role") == "tool"
    ]

    if tool_messages:
        resolved = [
            (
                message,
                _gemini_tool_interactions.get(str(message.get("tool_call_id") or "")),
            )
            for message in tool_messages
        ]

        # The canonical message history contains tool results from earlier
        # turns too. A Gemini continuation must contain only the result(s)
        # belonging to the latest function-call interaction.
        latest_state = next(
            (state for _, state in reversed(resolved) if state),
            None,
        )
        states = [
            (message, state)
            for message, state in resolved
            if state and latest_state and state["interaction_id"] == latest_state["interaction_id"]
        ]

        interaction_ids = {state["interaction_id"] for _, state in states}

        if len(interaction_ids) == 1 and states:
            previous_interaction_id = next(iter(interaction_ids))
            body["previous_interaction_id"] = previous_interaction_id
            body["input"] = [
                {
                    "type": "function_result",
                    "name": state["name"],
                    "call_id": str(message.get("tool_call_id") or ""),
                    # Gemini 2.5 accepts a JSON value here, but does not
                    # support the typed content-block array used for
                    # multimodal function responses by Gemini 3 models.
                    "result": _gemini_text_function_result(message.get("content")),
                }
                for message, state in states
            ]

            logger.info(
                "Continuing Gemini interaction %s with tool result(s)",
                previous_interaction_id,
            )
        else:
            logger.warning("Could not resolve Gemini interaction state for tool result(s)")

    async with httpx.AsyncClient(timeout=httpx.Timeout(settings.LLM_TIMEOUT)) as client:
        logger.debug("Gemini request body: %s", _safe_log_body(body))

        response = await client.post(
            settings.LLM_URL,
            json=body,
            headers=_gemini_headers(),
        )

        if response.is_error:
            logger.error(
                "Gemini API error %s: %s",
                response.status_code,
                response.text,
            )

        response.raise_for_status()

        data = response.json()
        normalized = _from_gemini(data)

        interaction_id = data.get("id")

        if interaction_id:
            for tool_call in (
                normalized.get("choices", [{}])[0].get("message", {}).get("tool_calls", [])
            ):
                call_id = str(tool_call.get("id") or "")
                if call_id:
                    _gemini_tool_interactions[call_id] = {
                        "interaction_id": str(interaction_id),
                        "name": str((tool_call.get("function") or {}).get("name") or ""),
                    }

            # Prevent the map from growing forever.
            if len(_gemini_tool_interactions) > 1000:
                stale_ids = list(_gemini_tool_interactions)[:-500]
                for call_id in stale_ids:
                    _gemini_tool_interactions.pop(call_id, None)

        return normalized, False


def _to_gemini(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Translate canonical OpenAI-shaped payload into Gemini Interactions."""
    messages = payload.get("messages") or []

    system_parts: list[str] = []
    input_parts: list[dict[str, Any]] = []

    for message in messages:
        role = message.get("role")
        content = message.get("content")

        if role == "system":
            if content:
                system_parts.append(str(content))
            continue

        if role == "user":
            if content:
                input_parts.append(
                    {
                        "type": "text",
                        "text": str(content),
                    }
                )
            continue

        if role == "assistant":
            if content:
                input_parts.append(
                    {
                        "type": "text",
                        "text": str(content),
                    }
                )

            # Only include previous function calls when constructing
            # a stateless request. Stateful continuations replace the
            # entire input with function_result in _post_gemini().
            for tool_call in message.get("tool_calls") or []:
                function = tool_call.get("function") or {}

                input_parts.append(
                    {
                        "type": "function_call",
                        "id": str(tool_call.get("id") or ""),
                        "name": str(function.get("name") or ""),
                        "arguments": _json_loads(function.get("arguments") or "{}"),
                    }
                )

            continue

        if role == "tool":
            input_parts.append(
                {
                    "type": "function_result",
                    "name": str(message.get("name") or ""),
                    "call_id": str(message.get("tool_call_id") or ""),
                    "result": _gemini_text_function_result(message.get("content")),
                }
            )

    body: dict[str, Any] = {
        "model": payload["model"],
        "input": input_parts,
    }

    if system_parts:
        body["system_instruction"] = "\n\n".join(part for part in system_parts if part)

    if payload.get("tools"):
        body["tools"] = _to_gemini_tools(payload["tools"])

    if payload.get("response_format"):
        response_format = payload["response_format"]

        if isinstance(response_format, dict):
            # AgentGate commonly sends {"type": "json_object"}.
            # Gemini Interactions uses JSON Schema type names instead, so
            # translate the OpenAI-compatible alias to Gemini's object type.
            # plain text/tool calls should not receive this field unless
            # it is explicitly usable.
            if response_format.get("type") == "json_object":
                body["response_format"] = {"type": "object"}
            else:
                body["response_format"] = response_format

    generation_config: dict[str, Any] = {}

    if "temperature" in payload:
        generation_config["temperature"] = payload["temperature"]

    if generation_config:
        body["generation_config"] = generation_config

    return body


def _gemini_text_function_result(content: Any) -> Any:
    """Return a text-only function result compatible with Gemini 2.5.

    The Interactions API reserves arrays of typed content blocks for
    multimodal function responses. AgentGate tool output is JSON text, so
    decode it to its underlying JSON value when possible; otherwise preserve
    it as a plain string.
    """
    text = str(content or "")
    try:
        value = json.loads(text)
        # Arrays are reserved by Interactions for typed content blocks.
        # Keep JSON arrays as text so Gemini 2.5 does not classify them as
        # multimodal function responses.
        return text if isinstance(value, list) else value
    except (TypeError, json.JSONDecodeError):
        return text


def _to_gemini_tools(
    tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Translate OpenAI function tools into Gemini function tools."""
    result: list[dict[str, Any]] = []

    for tool in tools:
        if not isinstance(tool, dict):
            continue

        function = tool.get("function")

        if not isinstance(function, dict):
            continue

        name = function.get("name")

        if not name:
            continue

        result.append(
            {
                "type": "function",
                "name": str(name),
                "description": str(function.get("description") or ""),
                "parameters": function.get(
                    "parameters",
                    {"type": "object"},
                ),
            }
        )

    return result


def _from_gemini(
    data: dict[str, Any],
) -> dict[str, Any]:
    """Normalize Gemini Interactions response into OpenAI message shape."""
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    for step in data.get("steps") or []:
        step_type = step.get("type")

        if step_type == "model_output":
            for content in step.get("content") or []:
                if content.get("type") == "text":
                    text_parts.append(str(content.get("text") or ""))

        elif step_type == "function_call":
            tool_calls.append(
                {
                    "id": str(step.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(step.get("name") or ""),
                        "arguments": json.dumps(step.get("arguments") or {}),
                    },
                }
            )

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "\n".join(text_parts) or None,
    }

    if tool_calls:
        message["tool_calls"] = tool_calls

    return {
        "choices": [
            {
                "message": message,
            }
        ]
    }


# ── Helpers ───────────────────────────────────────────────────────


def _json_loads(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _safe_log_body(body: dict[str, Any]) -> dict[str, Any]:
    """Return request body safe for debug logging."""
    return body


def extract_message(
    data: dict[str, Any],
) -> dict[str, Any]:
    """Return assistant message from a normalized LLM response."""
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"unexpected LLM response shape: {exc}") from exc

    if not isinstance(message, dict):
        raise ValueError("unexpected LLM message shape")

    return message
