"""Core data contracts for AgentGate.

These two schemas are the stable interface the whole system is built around:

  ActionRequest   - the standard input AgentGate evaluates (a proposed tool call,
                    normalized). F3 in the PRD: this is the *shared DS/DE contract*.
  DecisionResponse - the standard output AgentGate returns.

Kept as plain dataclasses (stdlib only) so the engine runs with no third-party deps.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class Decision(str, Enum):
    """Supported decisions (PRD section 9)."""

    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    NEED_APPROVAL = "NEED_APPROVAL"
    SANITIZE = "SANITIZE"
    ASK_USER = "ASK_USER"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


# Action vocabulary (PRD "Action Space"). Anything outside this is rejected by the
# Action Space Validator before it ever reaches the guardrail.
ACTION_TYPES = {
    "API_CALL",
    "BROWSER_OPEN",
    "BROWSER_SNAPSHOT",
    "BROWSER_CLICK",
    "BROWSER_TYPE",
    "BROWSER_SELECT",
    "BROWSER_SUBMIT",
    "BROWSER_SCREENSHOT",
    "FILE_READ",
    "FILE_WRITE",
    "FILE_DELETE",
    "ASK_USER",
    "NEED_APPROVAL",
    "SANITIZE",
    "DONE",
    "FAIL",
}


@dataclass
class ActionRequest:
    """Standard input evaluated by AgentGate (PRD section 8).

    Only ``action_type`` is strictly required; everything else is best-effort
    context the planner / runtime supplies. Detectors and the policy engine read
    these fields to make a decision.
    """

    action_type: str
    domain: str = "generic"
    target_system: str = ""
    tool_name: str = ""
    target: str = ""
    payload_summary: str = ""
    content_context: str = ""
    risk_hint: list[str] = field(default_factory=list)
    rollback_available: bool = True
    confidence: float = 1.0
    # Raw payload kept internally for detection/sanitization; never required to be
    # the same as payload_summary (which is the redacted/compact view).
    raw_payload: str = ""

    def __post_init__(self) -> None:
        text_fields = (
            "action_type",
            "domain",
            "target_system",
            "tool_name",
            "target",
            "payload_summary",
            "content_context",
            "raw_payload",
        )
        if any(not isinstance(getattr(self, name), str) for name in text_fields):
            raise ValueError("ActionRequest text fields must be strings")
        if not isinstance(self.rollback_available, bool):
            raise ValueError("rollback_available must be true or false")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not 0 <= float(self.confidence) <= 1
        ):
            raise ValueError("confidence must be a number between 0 and 1")
        if isinstance(self.risk_hint, str):
            self.risk_hint = [self.risk_hint] if self.risk_hint else []
        if not isinstance(self.risk_hint, list) or any(
            not isinstance(hint, str) for hint in self.risk_hint
        ):
            raise ValueError("risk_hint must be a list of strings")
        # The text detectors scan: prefer raw payload, fall back to the summary.
        if not self.raw_payload:
            self.raw_payload = self.payload_summary

    @property
    def scan_text(self) -> str:
        """All free text a detector should inspect, de-duplicated.

        Includes ``target`` (element id, path, URL, or tool name) because several
        detectors legitimately reason about *where* an action points - the secret
        detector wants to see a ``.env`` path, the payment detector a checkout URL.
        Classifiers that read their input purely as prose should use
        ``content_text`` instead.
        """
        return self._joined(
            self.raw_payload, self.payload_summary, self.content_context, self.target
        )

    @property
    def content_text(self) -> str:
        """Free text that is actual content, excluding structural metadata.

        Same as ``scan_text`` minus ``target``. A bare identifier appended as a
        trailing line is not content, and feeding it to a prompt-injection
        classifier reliably destabilizes the verdict: measured against live
        qwen2.5:7b and 3b, a benign booking message classified "benign" on its own
        flipped to "injection" at confidence 1.00 as soon as any target value
        ("1", "2", "send-button") was appended. Empty target stayed benign. That
        turned an expected SANITIZE into a BLOCK on a demo scenario.
        """
        return self._joined(self.raw_payload, self.payload_summary, self.content_context)

    def _joined(self, *parts: str) -> str:
        """Join non-empty parts, de-duplicated on whitespace-normalized content.

        raw_payload and payload_summary are often the same content in different
        forms (payload_summary whitespace-flattens raw_payload for multi-line
        text), so exact-string dedup misses them and the same content gets
        included twice - which, worse than being redundant, makes genuinely benign
        text look deliberately duplicated or obfuscated to an LLM classifier.
        """
        seen: set[str] = set()
        kept: list[str] = []
        for text in parts:
            if not text:
                continue
            normalized = " ".join(text.split())
            if normalized in seen:
                continue
            seen.add(normalized)
            kept.append(text)
        return "\n".join(kept)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionRequest":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SensitiveEntity:
    """A single detected sensitive item."""

    kind: str            # e.g. "EMAIL", "API_KEY", "PRIVATE_KEY", "SOURCE_CODE"
    snippet: str         # short, already-truncated preview
    detector: str        # which detector found it
    severity: str = "MEDIUM"  # LOW / MEDIUM / HIGH / CRITICAL

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DecisionResponse:
    """Standard output returned by AgentGate (PRD section 9)."""

    decision: Decision
    risk_level: RiskLevel
    risk_score: float
    reasons: list[str] = field(default_factory=list)
    triggered_policies: list[str] = field(default_factory=list)
    sensitive_entities: list[SensitiveEntity] = field(default_factory=list)
    sanitized_payload: str | None = None
    next_step: str = ""
    audit_id: str = ""
    evaluation_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["decision"] = self.decision.value
        d["risk_level"] = self.risk_level.value
        return d
