"""Strict stdlib client for the sole Ollama-backed detector pipeline."""

from __future__ import annotations

import ipaddress
import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_DETECTOR_MODEL = "qwen2.5:7b"
DEFAULT_DETECTOR_TIMEOUT = 30.0
_MAX_RESPONSE_BYTES = 1_048_576
# Docker Desktop exposes a host-local Ollama service through this reserved
# hostname. It is the one non-loopback HTTP endpoint allowed for containers;
# arbitrary remote HTTP detector endpoints must still use HTTPS below.
_LOCAL_CONTAINER_HOSTNAMES = frozenset({"host.docker.internal"})


class LLMUnavailable(RuntimeError):
    """Raised when the required detector runtime or response is unusable."""

    category = "unknown"


class LLMConnectionError(LLMUnavailable):
    """The detector could not connect to the configured Ollama endpoint."""

    category = "connection_error"


class LLMTimeoutError(LLMUnavailable):
    """Ollama did not complete the detector request before its deadline."""

    category = "timeout"


class LLMHTTPError(LLMUnavailable):
    """Ollama returned an unsuccessful HTTP response."""

    category = "http_error"

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"Ollama returned HTTP {status}")


class LLMInvalidJSONError(LLMUnavailable):
    """Ollama returned a response whose JSON content could not be parsed."""

    category = "invalid_json"


class LLMInvalidResponseEnvelopeError(LLMUnavailable):
    """Ollama returned a JSON envelope without assistant message content."""

    category = "invalid_response_envelope"


class LLMResponseSchemaError(LLMUnavailable):
    """The model output does not satisfy the required detector schema."""

    category = "schema_validation"


class LLMContradictoryOutputError(LLMUnavailable):
    """The model output is structurally valid but internally inconsistent."""

    category = "contradictory_output"


def resolve_model(model: str | None = None) -> str:
    value = model or os.environ.get("AGENTGATE_LLM_DETECTOR_MODEL", DEFAULT_DETECTOR_MODEL)
    if not value.strip():
        raise LLMUnavailable("AGENTGATE_LLM_DETECTOR_MODEL must not be empty")
    return value.strip()


def resolve_host(host: str | None = None) -> str:
    value = (host or os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST)).rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise LLMUnavailable("OLLAMA_HOST is not a valid URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LLMUnavailable("OLLAMA_HOST must be a full HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LLMUnavailable("OLLAMA_HOST must not contain credentials, query, or fragment")
    is_loopback = parsed.hostname == "localhost" or parsed.hostname in _LOCAL_CONTAINER_HOSTNAMES
    try:
        is_loopback = is_loopback or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not is_loopback:
        raise LLMUnavailable("Remote OLLAMA_HOST endpoints must use HTTPS")
    return value


def resolve_timeout(timeout: float | None = None) -> float:
    raw: Any = timeout
    if raw is None:
        raw = os.environ.get("AGENTGATE_LLM_DETECTOR_TIMEOUT", str(DEFAULT_DETECTOR_TIMEOUT))
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise LLMUnavailable("AGENTGATE_LLM_DETECTOR_TIMEOUT must be a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise LLMUnavailable("AGENTGATE_LLM_DETECTOR_TIMEOUT must be greater than zero")
    return value


def chat_json(
    system_prompt: str,
    user_content: str,
    *,
    model: str | None = None,
    host: str | None = None,
    timeout: float | None = None,
    extra_options: dict[str, Any] | None = None,
    response_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one validated JSON object from Ollama or raise ``LLMUnavailable``."""
    resolved_model = resolve_model(model)
    resolved_host = resolve_host(host)
    resolved_timeout = resolve_timeout(timeout)
    options: dict[str, Any] = {"temperature": 0}
    if extra_options:
        options.update(extra_options)

    body = {
        "model": resolved_model,
        "stream": False,
        # A JSON Schema activates Ollama Structured Outputs.  The older "json"
        # mode only asks the model for syntactic JSON and cannot ensure the six
        # detector sections, enums, or integer fields are present.
        "format": response_schema if response_schema is not None else "json",
        "options": options,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }

    try:
        request = urllib.request.Request(
            resolved_host + "/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=resolved_timeout) as response:
            raw_response = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw_response) > _MAX_RESPONSE_BYTES:
            raise ValueError("response exceeded the configured size limit")
        try:
            envelope = json.loads(raw_response.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise LLMInvalidJSONError("Ollama response was not valid JSON") from exc
        if not isinstance(envelope, dict):
            raise LLMInvalidResponseEnvelopeError("Ollama response must be an object")
        message = envelope.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise LLMInvalidResponseEnvelopeError("Ollama response is missing message.content")
        try:
            result = json.loads(message["content"])
        except json.JSONDecodeError as exc:
            raise LLMInvalidJSONError("Ollama message.content was not valid JSON") from exc
        if not isinstance(result, dict):
            raise LLMInvalidResponseEnvelopeError("detector response must be a JSON object")
        return result
    except LLMUnavailable:
        raise
    except urllib.error.HTTPError as exc:
        raise LLMHTTPError(exc.code) from exc
    except TimeoutError as exc:
        raise LLMTimeoutError("Ollama detector request timed out") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise LLMTimeoutError("Ollama detector request timed out") from exc
        raise LLMConnectionError("Could not reach the configured Ollama service") from exc
    except OSError as exc:
        raise LLMConnectionError("Could not reach the configured Ollama service") from exc
    except (
        UnicodeDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        raise LLMInvalidResponseEnvelopeError("Ollama returned an invalid response") from exc
