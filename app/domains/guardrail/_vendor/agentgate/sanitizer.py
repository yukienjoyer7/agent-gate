"""Sanitizer: produce a redacted payload when safe continuation is possible.

Replaces sensitive spans (emails, cards, secrets, payment links, ...) with typed
placeholders, so an action can sometimes proceed in SANITIZE mode instead of being
blocked outright.

Why this step is pattern-based while *detection* is entirely LLM-driven: redaction has
to replace exact character spans. A classifier reliably answers "is there a secret in
here?", but it cannot be trusted to return character-exact offsets for every occurrence,
and a redaction that misses one span leaks the very value it was meant to hide. So the
detectors decide *whether* something is sensitive, and this module decides *which
characters* get replaced.
"""

from __future__ import annotations

import re

# Order matters: redact the most specific / highest-risk patterns first, so a private
# key block is replaced whole rather than being partially eaten by a narrower rule.
_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    # Full PEM block, including the body, before the header-only rule below.
    (
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----.*?"
            r"-----END (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    # Truncated/unterminated key material still must not survive.
    (
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
        "[REDACTED_PRIVATE_KEY]",
    ),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED_SLACK_TOKEN]"),
    (
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
        ),
        "[REDACTED_JWT]",
    ),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"), "[REDACTED_STRIPE_KEY]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "[REDACTED_GOOGLE_KEY]"),
    # Generic "SOMETHING_SECRET = value" assignments: keep the name, drop the value.
    (
        re.compile(
            r"(?i)\b([a-z0-9_]*(?:api[_-]?key|secret|token|password|passwd|pwd|"
            r"access[_-]?key))\b\s*[:=]\s*[\"']?([^\s\"']{6,})"
        ),
        lambda m: f"{m.group(1)}=[REDACTED]",
    ),
    # Credit-card-like runs of 13-16 digits, optionally grouped.
    (re.compile(r"\b(?:\d[ -]?){13,16}\b"), "[REDACTED_CARD]"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),
    # Loose international/local phone; at least 9 digits to limit false positives.
    (re.compile(r"(?<!\w)(\+?\d[\d\s().-]{8,}\d)(?!\w)"), "[REDACTED_PHONE]"),
    (
        re.compile(r"(?i)https?://\S*(?:pay|invoice|checkout|billing|refund)\S*"),
        "[REDACTED_PAYMENT_LINK]",
    ),
    # Booking references such as BK-001, RES12345, ORDER-2024-99.
    (
        re.compile(r"\b(?:BK|RES|RESV|ORDER|BOOK|PNR)[-_ ]?\d{2,}\b", re.IGNORECASE),
        "[REDACTED_BOOKING_REF]",
    ),
]


def sanitize(text: str) -> str:
    """Return a redacted copy of ``text``."""
    out = text
    for pattern, repl in _REDACTIONS:
        out = pattern.sub(repl, out)
    return out
