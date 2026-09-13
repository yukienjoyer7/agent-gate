"""Sanitize local output and persistence at a single reusable boundary."""

from __future__ import annotations

import re
from typing import Any

from app.domains.guardrail.decision.simple import _redact_string
from app.domains.guardrail.sensitive import is_sensitive_key

_EMAIL = re.compile(r"\b[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+\b")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")


class Sanitizer:
    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def remember(self, value: str | None) -> None:
        if value:
            self._secrets.add(value)

    def text(self, value: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        value, _ = _redact_string(value)
        value = _JWT.sub("[REDACTED]", value)
        value = _EMAIL.sub("[EMAIL REDACTED]", value)
        return _CONTROL.sub("", value)

    def clean(self, value: Any, *, _typing: bool = False) -> Any:
        if isinstance(value, dict):
            typing = str(value.get("action_type") or value.get("type") or "").lower()
            browser_input = _typing or typing in {"fill", "type", "browser_type", "browser_select"}
            return {
                str(key): (
                    "[REDACTED]"
                    if (
                        is_sensitive_key(str(key))
                        or str(key) in {"customer_email", "raw_prompt"}
                        or (browser_input and str(key) in {"value", "query", "text"})
                    )
                    and item not in (None, "")
                    else self.clean(item, _typing=browser_input)
                )
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.clean(item, _typing=_typing) for item in value]
        if isinstance(value, str):
            return self.text(value)
        return value
