"""Deterministic detection of payload fields the user must provide.

This is the "sanitize" layer of the reactive agent loop: when a plan step
carries a secret-shaped key with no value yet (``password``, ``api_key``,
...) or an explicit ``{{placeholder}}`` marker, execution pauses and the
user is asked to type the value (see ``POST /chat/execute/{run_id}/respond``
with ``action=input``).
"""

from __future__ import annotations

import re
from typing import Any

# Keys that almost certainly carry secrets / personal data the planner should
# never fabricate. Matched case-insensitively as substrings.
SENSITIVE_KEY_PATTERNS = (
    "password",
    "passwd",
    "pwd",
    "api_key",
    "apikey",
    "token",
    "secret",
    "otp",
    "pin",
    "credential",
    "authorization",
    "auth",
)

_PLACEHOLDER_RE = re.compile(r"^\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")


def is_sensitive_key(key: str) -> bool:
    """True when a payload key looks like it holds a secret."""
    low = key.lower()
    return any(pattern in low for pattern in SENSITIVE_KEY_PATTERNS)


def detect_sensitive_fields(
    payload: dict[str, Any],
    answered: set[str] | None = None,
) -> list[dict[str, str]]:
    """Return the payload fields that need user input, as
    ``[{"key": ..., "label": ...}]``.

    Triggers when:

    - a sensitive key has no value yet (``""`` / ``None``), or
    - a value is an explicit ``{{placeholder}}`` marker (e.g. ``{{password}}``).

    Keys in ``answered`` (already provided by the user) are never re-requested.
    """
    answered = answered or set()
    fields: list[dict[str, str]] = []
    for key, value in (payload or {}).items():
        if key in answered:
            continue
        placeholder = _placeholder_name(value)
        if placeholder is not None:
            fields.append({"key": key, "label": _field_label(placeholder)})
        elif is_sensitive_key(key) and value in ("", None):
            fields.append({"key": key, "label": _field_label(key)})
    return fields


def _placeholder_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = _PLACEHOLDER_RE.match(value.strip())
    return match.group(1) if match else None


def _field_label(key: str) -> str:
    return key.replace("_", " ").replace("-", " ").strip().title()
