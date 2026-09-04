"""Deterministic detection of payload fields the user must provide.

This is the "sanitize" layer of the reactive agent loop: when a plan step
carries a secret-shaped key with no value yet (``password``, ``api_key``,
...) or an explicit placeholder marker (``{{name}}``, ``<name>``, ``[name]``,
or a fabricated fill-in value like ``YOUR_PASSWORD_HERE``), execution pauses
and the user is asked to type the value (see
``POST /chat/execute/{run_id}/respond`` with ``action=input``).
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

# Explicit marker shapes: {{name}}, <name>, [name], {name}. The first group
# that matches wins; the captured name becomes the field label.
# ``{name}`` (single brace) is included because several free models (e.g.
# Nvidia NIM) emit single-brace placeholders instead of the double-brace form.
_PLACEHOLDER_RE = re.compile(
    r"^\s*(?:"
    r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}"
    r"|<\s*([A-Za-z_][A-Za-z0-9_]*)\s*>"
    r"|\[\s*([A-Za-z_][A-Za-z0-9_ ]*?)\s*\]"
    r"|\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}"
    r")\s*$"
)

# Values models commonly fabricate instead of real secrets: "YOUR_PASSWORD_HERE",
# "your email here", or the bare word itself ("password", "token", ...).
_FABRICATED_VALUE_RE = re.compile(
    r"^\s*(?:"
    r"your[\s_-]+[a-z0-9][a-z0-9\s_-]*"
    r"|(?:password|passwd|pwd|username|email|token|secret|api[_-]?key|otp|pin|login|account)"
    r")\s*$",
    re.IGNORECASE,
)

# Payload keys that are descriptive metadata, never user-supplied input values.
# Fabricated-value detection is skipped for these to avoid false positives
# (e.g. ``label: "Password"`` describes the field, it is not a fill-in;
# ``element_id: "username"`` is the DOM id of the field, not a value to type).
_DESCRIPTIVE_KEYS = frozenset({"label", "role", "element_id"})


def is_sensitive_key(key: str) -> bool:
    """True when a payload key looks like it holds a secret."""
    low = key.lower()
    return any(pattern in low for pattern in SENSITIVE_KEY_PATTERNS)


def detect_sensitive_fields(
    payload: dict[str, Any],
    answered: set[str] | None = None,
    action_type: str | None = None,
) -> list[dict[str, str]]:
    """Return the payload fields that need user input, as
    ``[{"key": ..., "label": ...}]``.

    Triggers when:

    - a value is an explicit placeholder marker (``{{name}}`` / ``<name>`` /
      ``[name]`` or a fabricated fill-in like ``YOUR_PASSWORD_HERE``), or
    - a sensitive key has no value yet (``""`` / ``None``), or
    - a typing action (``BROWSER_TYPE`` / ``BROWSER_SELECT``) carries an empty
      ``value`` — the model left it blank for the user to fill in.

    Keys in ``answered`` (already provided by the user) are never re-requested.
    """
    answered = answered or set()
    fields: list[dict[str, str]] = []
    typing_action = (action_type or "").upper() in ("BROWSER_TYPE", "BROWSER_SELECT")
    for key, value in (payload or {}).items():
        if key in answered:
            continue
        placeholder = _explicit_placeholder_name(value)
        if placeholder is None and key.lower() not in _DESCRIPTIVE_KEYS:
            placeholder = _fabricated_label(value)
        if placeholder is not None:
            fields.append({"key": key, "label": _field_label(placeholder)})
        elif is_sensitive_key(key) and value in ("", None):
            fields.append({"key": key, "label": _field_label(key)})
        elif typing_action and key == "value" and value in ("", None):
            fields.append({"key": key, "label": _field_label(key)})
    return fields


def _placeholder_name(value: Any) -> str | None:
    """Return a readable field name when ``value`` looks like a fill-in marker.

    Covers explicit markers (``{{password}}``, ``<password>``, ``[password]``)
    plus fabricated values (``YOUR_PASSWORD_HERE``, ``password``, ...).
    Returns ``None`` for real values (e.g. ``123456``).
    """
    if not isinstance(value, str):
        return None
    explicit = _explicit_placeholder_name(value)
    return explicit if explicit is not None else _fabricated_label(value)


def _explicit_placeholder_name(value: Any) -> str | None:
    """Name from an explicit marker (``{{name}}``, ``<name>``, ``[name]``)."""
    if not isinstance(value, str):
        return None
    match = _PLACEHOLDER_RE.match(value.strip())
    if not match:
        return None
    for group in match.groups():
        if group is not None:
            return group.strip()
    return None


def _fabricated_label(value: Any) -> str | None:
    """Label for a fabricated fill-in value, or None when it is a real value."""
    if not isinstance(value, str) or not _FABRICATED_VALUE_RE.match(value):
        return None
    cleaned = value.lower()
    cleaned = re.sub(r"^your[\s_-]+", "", cleaned)
    cleaned = re.sub(r"[\s_-]*here$", "", cleaned)
    cleaned = re.sub(r"[\s_-]+", " ", cleaned).strip()
    return cleaned or None


def _field_label(key: str) -> str:
    return key.replace("_", " ").replace("-", " ").strip().title()
