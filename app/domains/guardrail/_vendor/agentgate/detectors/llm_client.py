"""Strict stdlib client for the sole Ollama-backed detector pipeline."""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_DETECTOR_MODEL = "qwen2.5:7b"
DEFAULT_FALLBACK_MODEL = "gemma-4-E2B-it"
DEFAULT_DETECTOR_TIMEOUT = 30.0
DEFAULT_FALLBACK_ATTEMPTS = 1
_MAX_RESPONSE_BYTES = 1_048_576
# Docker Desktop exposes a host-local Ollama service through this reserved
# hostname. It is the one non-loopback HTTP endpoint allowed for containers;
# arbitrary remote HTTP detector endpoints must still use HTTPS below.
_LOCAL_CONTAINER_HOSTNAMES = frozenset({"host.docker.internal"})
_LOGGER = logging.getLogger(__name__)
_FALLBACK_LOCK = threading.Lock()


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


class LLMFallbackError(LLMUnavailable):
    """The primary timed out but the fallback path could not complete."""

    category = "fallback_failed"


def resolve_model(model: str | None = None) -> str:
    value = model or os.environ.get("AGENTGATE_LLM_DETECTOR_MODEL", DEFAULT_DETECTOR_MODEL)
    if not value.strip():
        raise LLMUnavailable("AGENTGATE_LLM_DETECTOR_MODEL must not be empty")
    return value.strip()


def resolve_fallback_model(model: str | None = None) -> str:
    value = model or os.environ.get("AGENTGATE_LLM_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL)
    if not value.strip():
        raise LLMUnavailable("AGENTGATE_LLM_FALLBACK_MODEL must not be empty")
    return value.strip()


def resolve_fallback_attempts(attempts: int | None = None) -> int:
    raw: Any = attempts
    if raw is None:
        raw = os.environ.get("AGENTGATE_LLM_FALLBACK_ATTEMPTS", str(DEFAULT_FALLBACK_ATTEMPTS))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise LLMUnavailable("AGENTGATE_LLM_FALLBACK_ATTEMPTS must be an integer") from exc
    # A fallback is deliberately bounded to one request. This prevents an
    # unavailable secondary model from creating an unbounded retry loop.
    if value < 0 or value > 1:
        raise LLMUnavailable("AGENTGATE_LLM_FALLBACK_ATTEMPTS must be between 0 and 1")
    return value


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


def unload_model(
    *,
    model: str | None = None,
    host: str | None = None,
    timeout: float | None = None,
) -> None:
    """Wait for Ollama to unload ``model`` from memory.

    Ollama performs an unload when a generate request carries ``keep_alive: 0``.
    Reading the complete response makes this return only after Ollama has
    acknowledged the unload request, so fallback inference starts afterwards.
    """
    resolved_model = resolve_model(model)
    resolved_host = resolve_host(host)
    resolved_timeout = resolve_timeout(timeout)
    body = {
        "model": resolved_model,
        "prompt": "",
        "stream": False,
        "keep_alive": 0,
    }
    try:
        request = urllib.request.Request(
            resolved_host + "/api/generate",
            data=json.dumps(body).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=resolved_timeout) as response:
            response.read(_MAX_RESPONSE_BYTES)
    except LLMUnavailable:
        raise
    except urllib.error.HTTPError as exc:
        raise LLMHTTPError(exc.code) from exc
    except TimeoutError as exc:
        raise LLMTimeoutError("Ollama model unload request timed out") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise LLMTimeoutError("Ollama model unload request timed out") from exc
        raise LLMConnectionError("Could not reach Ollama to unload the primary model") from exc
    except OSError as exc:
        raise LLMConnectionError("Could not reach Ollama to unload the primary model") from exc


class LLMFallbackHandler:
    """Run a primary detector request with one bounded timeout fallback.

    Only ``LLMTimeoutError`` from the primary request activates this path. The
    unload and fallback request are serialized so concurrent detector timeouts
    cannot race while clearing the primary model from Ollama.
    """

    def __init__(
        self,
        *,
        primary_model: str | None = None,
        fallback_model: str | None = None,
        host: str | None = None,
        timeout: float | None = None,
        max_fallback_attempts: int | None = None,
        chat_fn: Callable[..., dict[str, Any]] | None = None,
        unload_fn: Callable[..., None] | None = None,
    ) -> None:
        self.primary_model = resolve_model(primary_model)
        self.fallback_model = resolve_fallback_model(fallback_model)
        self.host = resolve_host(host)
        self.timeout = resolve_timeout(timeout)
        self.max_fallback_attempts = resolve_fallback_attempts(max_fallback_attempts)
        self._chat_fn = chat_fn or chat_json
        self._unload_fn = unload_fn or unload_model

    def execute(
        self,
        system_prompt: str,
        user_content: str,
        *,
        extra_options: dict[str, Any] | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            return self._chat_fn(
                system_prompt,
                user_content,
                model=self.primary_model,
                host=self.host,
                timeout=self.timeout,
                extra_options=extra_options,
                response_schema=response_schema,
            )
        except LLMTimeoutError:
            _LOGGER.warning(
                "Guardrail primary model timeout: model=%s fallback=%s",
                self.primary_model,
                self.fallback_model,
            )

        with _FALLBACK_LOCK:
            _LOGGER.info(
                "Guardrail primary model unload started: model=%s",
                self.primary_model,
            )
            try:
                self._unload_fn(
                    model=self.primary_model,
                    host=self.host,
                    timeout=self.timeout,
                )
            except Exception as unload_error:
                _LOGGER.error(
                    "Guardrail primary model unload failed; fallback was not started: model=%s error=%s",
                    self.primary_model,
                    unload_error,
                )
                raise LLMFallbackError(
                    f"Primary model {self.primary_model!r} timed out and could not be unloaded "
                    f"before fallback: {unload_error}"
                ) from unload_error
            _LOGGER.info(
                "Guardrail primary model unload completed: model=%s",
                self.primary_model,
            )

            last_error: Exception | None = None
            for attempt in range(1, self.max_fallback_attempts + 1):
                _LOGGER.info(
                    "Guardrail fallback request started: model=%s attempt=%d/%d",
                    self.fallback_model,
                    attempt,
                    self.max_fallback_attempts,
                )
                try:
                    result = self._chat_fn(
                        system_prompt,
                        user_content,
                        model=self.fallback_model,
                        host=self.host,
                        timeout=self.timeout,
                        extra_options=extra_options,
                        response_schema=response_schema,
                    )
                    _LOGGER.info(
                        "Guardrail fallback request completed: model=%s attempt=%d/%d",
                        self.fallback_model,
                        attempt,
                        self.max_fallback_attempts,
                    )
                    return result
                except Exception as fallback_error:
                    last_error = fallback_error
                    _LOGGER.error(
                        "Guardrail fallback request failed: model=%s attempt=%d/%d error=%s",
                        self.fallback_model,
                        attempt,
                        self.max_fallback_attempts,
                        fallback_error,
                    )

            raise LLMFallbackError(
                f"Primary model {self.primary_model!r} timed out; fallback model "
                f"{self.fallback_model!r} failed after {self.max_fallback_attempts} attempt(s): "
                f"{last_error}"
            ) from last_error


def chat_json_with_fallback(
    system_prompt: str,
    user_content: str,
    *,
    model: str | None = None,
    fallback_model: str | None = None,
    host: str | None = None,
    timeout: float | None = None,
    max_fallback_attempts: int | None = None,
    extra_options: dict[str, Any] | None = None,
    response_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call the primary model and use the unload-then-fallback policy."""
    handler = LLMFallbackHandler(
        primary_model=model,
        fallback_model=fallback_model,
        host=host,
        timeout=timeout,
        max_fallback_attempts=max_fallback_attempts,
    )
    return handler.execute(
        system_prompt,
        user_content,
        extra_options=extra_options,
        response_schema=response_schema,
    )
