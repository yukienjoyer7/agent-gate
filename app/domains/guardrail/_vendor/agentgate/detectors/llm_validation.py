"""Schema helpers for deterministic validation of detector model output."""

from __future__ import annotations

import math
from typing import Any

from .llm_client import LLMUnavailable


def require_bool(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise LLMUnavailable(f"detector field {key!r} must be a boolean")
    return value


def require_string(data: dict[str, Any], key: str, allowed: set[str] | None = None) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise LLMUnavailable(f"detector field {key!r} must be text")
    if allowed is not None and value not in allowed:
        raise LLMUnavailable(f"detector field {key!r} has an unsupported value")
    return value


def require_confidence(data: dict[str, Any], key: str = "confidence") -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LLMUnavailable(f"detector field {key!r} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise LLMUnavailable(f"detector field {key!r} must be between 0 and 1")
    return result


def require_items(data: dict[str, Any], key: str = "items") -> list[dict[str, Any]]:
    value = data.get(key)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise LLMUnavailable(f"detector field {key!r} must be a list of objects")
    return value


def require_nonnegative_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LLMUnavailable(f"detector field {key!r} must be a non-negative integer")
    return value
