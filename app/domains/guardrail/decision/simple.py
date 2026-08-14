import re
from time import perf_counter
from typing import Any

from app.core.schemas import ActionRequest, Decision, DecisionResponse, RiskLevel

# Domain → Risk Level mapping (from sprint.md contract: 5 domains)
#   productivity → MEDIUM, booking → CRITICAL, code_protection → HIGH,
#   browser → LOW, filesystem → LOW
DOMAIN_RISK: dict[str, RiskLevel] = {
    "booking": RiskLevel.CRITICAL,
    "code_protection": RiskLevel.HIGH,
    "productivity": RiskLevel.MEDIUM,
    "browser": RiskLevel.LOW,
    "filesystem": RiskLevel.LOW,
}

# Discriminative risk_hints
BLOCK_HINTS = {"destructive", "unauthorized", "data_exfiltration"}
NEED_APPROVAL_HINTS = {"external_send", "payment", "bulk_action", "refund"}
ASK_USER_HINTS = {"ambiguous_target", "missing_target", "clarification_needed"}

# Sensitive-content patterns → SANITIZE. The payload is not rejected; the
# matched content is masked in `sanitized_payload` and the decision becomes
# SANITIZE so the sanitized preview can be reviewed / confirmed.
# (pattern, sensitive_entity label, replacement)
SECRET_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "api_key", "[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws_access_key", "[REDACTED]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), "github_token", "[REDACTED]"),
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\b\s*[=:]\s*\S+"
        ),
        "credential",
        r"\1=[REDACTED]",
    ),
]


def _redact_string(value: str) -> tuple[str, list[str]]:
    found: list[str] = []
    for pattern, entity, replacement in SECRET_PATTERNS:
        if pattern.search(value):
            found.append(entity)
            value = pattern.sub(replacement, value)
    return value, sorted(set(found))


def _redact_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Recursively mask secrets in a payload. Returns (sanitized_payload,
    sensitive_entities); entities is empty when nothing was redacted."""

    def walk(value: Any) -> tuple[Any, list[str]]:
        if isinstance(value, dict):
            redacted: dict[str, Any] = {}
            entities: list[str] = []
            for key, item in value.items():
                new_item, new_entities = walk(item)
                redacted[key] = new_item
                entities.extend(new_entities)
            return redacted, entities
        if isinstance(value, list):
            redacted_items: list[Any] = []
            entities: list[str] = []
            for item in value:
                new_item, new_entities = walk(item)
                redacted_items.append(new_item)
                entities.extend(new_entities)
            return redacted_items, entities
        if isinstance(value, str):
            return _redact_string(value)
        return value, []

    sanitized, entities = walk(payload)
    return sanitized, sorted(set(entities))


def decide(action: ActionRequest) -> DecisionResponse:
    started = perf_counter()
    domain = action.domain or "productivity"
    risk_hint = action.risk_hint or "unknown"

    base_risk = DOMAIN_RISK.get(domain, RiskLevel.LOW)
    reasons: list[str] = []
    triggered_policies: list[str] = []

    sanitized_payload, sensitive_entities = _redact_payload(action.payload)

    # ── Priority 1: BLOCK (destructive / unauthorized) ────────────
    if risk_hint in BLOCK_HINTS:
        decision = Decision.BLOCK
        risk_level = RiskLevel.CRITICAL
        risk_score = 0.95
        reasons.append(f"blocked: {risk_hint}")
        triggered_policies.append("block_destructive_action")

    # ── Priority 2: SANITIZE (sensitive payload content detected) ──
    elif sensitive_entities:
        decision = Decision.SANITIZE
        risk_level = RiskLevel.MEDIUM
        risk_score = 0.50
        reasons.append(f"payload contains sensitive content: {', '.join(sensitive_entities)}")
        triggered_policies.append("redact_sensitive_payload")
        reasons.append("sanitized payload prepared; execute only the sanitized payload")

    # ── Priority 3: ASK_USER (not enough information to decide) ────
    elif risk_hint in ASK_USER_HINTS:
        decision = Decision.ASK_USER
        risk_level = RiskLevel.LOW
        risk_score = 0.30
        reasons.append(f"clarification required: {risk_hint}")
        triggered_policies.append("ask_user_on_ambiguity")
        reasons.append("do not execute until the user provides the missing information")

    # ── Priority 4: NEED_APPROVAL (risky action types / high-risk domains) ────
    elif risk_hint in NEED_APPROVAL_HINTS or base_risk in (RiskLevel.CRITICAL, RiskLevel.HIGH):
        decision = Decision.NEED_APPROVAL
        risk_level = base_risk
        risk_score = 0.80 if base_risk == RiskLevel.CRITICAL else 0.60
        reasons.append(f"risk_hint={risk_hint}")
        reasons.append(f"domain={domain} has {base_risk.value} risk")
        triggered_policies.append("domain_risk_requires_approval")

    # ── Priority 5: ALLOW (safe) ──────────────────────────────────
    else:
        decision = Decision.ALLOW
        risk_level = RiskLevel.LOW
        risk_score = 0.10
        reasons.append(f"risk_hint={risk_hint}, domain={domain}")

    next_step = {
        Decision.BLOCK: "blocked",
        Decision.SANITIZE: "sanitize",
        Decision.ASK_USER: "ask_user",
        Decision.NEED_APPROVAL: "approval_queue",
        Decision.ALLOW: "execute",
    }[decision]

    return DecisionResponse(
        run_id=action.run_id,
        action_id=action.action_id,
        decision=decision,
        risk_level=risk_level,
        risk_score=risk_score,
        reasons=reasons,
        triggered_policies=triggered_policies,
        sanitized_payload=sanitized_payload if sensitive_entities else None,
        sensitive_entities=sensitive_entities,
        next_step=next_step,
        latency_ms=int((perf_counter() - started) * 1000),
    )
