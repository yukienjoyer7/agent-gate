"""
Sensitive-entity detector for the SANITIZE decision path (F4/F5 Detectors,
per sprint.md / PRD).

Scope note: this is an MVP, pattern-based detector — not a full DLP/PII
engine. It exists to make the SANITIZE branch of the Decision enum
reachable end-to-end, the same way BLOCKED_RISK_HINTS made BLOCK reachable.
Real detection (Presidio, entropy-based secret scanning, etc.) is future
work; see PRD section on MVP boundaries.

Only string values in the payload are scanned. Only the entity TYPE is
recorded in sensitive_entities (never the raw matched value), consistent
with the "only store entity types, never raw values" rule in the audit
schema doc's Data Retention section.
"""

from __future__ import annotations

import re
from typing import Any

# Ordered so higher-specificity patterns (SSN, CREDIT_CARD) are tried before
# looser ones (PHONE) that could otherwise partially match the same digits.
SENSITIVE_PATTERNS: dict[str, re.Pattern[str]] = {
    "EMAIL": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "CREDIT_CARD": re.compile(r"\b(?:\d[ -]?){13,16}\b"),
    "PHONE": re.compile(r"\b(?:\+?\d{1,2}[ -]?)?\(?\d{3}\)?[ -]?\d{3}[ -]?\d{4}\b"),
    "API_KEY": re.compile(r"\b(?:sk|pk|api|token)[-_][A-Za-z0-9]{16,}\b", re.IGNORECASE),
}


def scan_and_sanitize(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """
    Recursively scan a payload for sensitive entities and return a redacted
    copy alongside the sorted list of entity types found.

    Returns (payload, []) unchanged if nothing is found, so callers can
    treat an empty sensitive_entities list as "no sanitization needed".
    """
    found: set[str] = set()
    sanitized = _sanitize_value(payload, found)
    return sanitized, sorted(found)


def _sanitize_value(value: Any, found: set[str]) -> Any:
    if isinstance(value, str):
        return _sanitize_string(value, found)
    if isinstance(value, dict):
        return {key: _sanitize_value(val, found) for key, val in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(item, found) for item in value]
    return value


def _sanitize_string(text: str, found: set[str]) -> str:
    for entity_type, pattern in SENSITIVE_PATTERNS.items():
        if pattern.search(text):
            found.add(entity_type)
            text = pattern.sub(f"[REDACTED_{entity_type}]", text)
    return text
