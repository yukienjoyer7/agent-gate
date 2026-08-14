from time import perf_counter

from app.config.settings import get_settings
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


def _hint_sets(settings) -> tuple[set[str], set[str]]:
    """BLOCK / NEED_APPROVAL risk_hint sets, sourced from config."""
    return set(settings.GUARDRAIL_BLOCK_HINTS), set(settings.GUARDRAIL_NEED_APPROVAL_HINTS)


def decide_rule(action: ActionRequest) -> DecisionResponse:
    """Deterministic rule-based guardrail decision (~0ms, zero cost).

    The single entry point for the whole guardrail: maps ``risk_hint`` +
    ``domain`` to ALLOW / NEED_APPROVAL / BLOCK via a fixed lookup table.
    The reactive agent loop and async callers use the hybrid
    :func:`app.domains.guardrail.decision.adecide` (rules first, optional
    dedicated LLM judge for non-BLOCK cases); the sync ``decide`` alias
    remains for legacy callers.
    """
    started = perf_counter()
    settings = get_settings()
    block_hints, need_approval_hints = _hint_sets(settings)
    domain = action.domain or settings.DEFAULT_DOMAIN
    risk_hint = action.risk_hint or "unknown"

    base_risk = DOMAIN_RISK.get(domain, RiskLevel.LOW)
    reasons: list[str] = []
    triggered_policies: list[str] = []

    # ── Priority 1: BLOCK (destructive / unauthorized) ────────────
    if risk_hint in block_hints:
        decision = Decision.BLOCK
        risk_level = RiskLevel.CRITICAL
        risk_score = 0.95
        reasons.append(f"blocked: {risk_hint}")
        triggered_policies.append("block_destructive_action")

    # ── Priority 2: NEED_APPROVAL (risky action types / high-risk domains) ────
    elif risk_hint in need_approval_hints or base_risk in (RiskLevel.CRITICAL, RiskLevel.HIGH):
        decision = Decision.NEED_APPROVAL
        risk_level = base_risk
        risk_score = 0.80 if base_risk == RiskLevel.CRITICAL else 0.60
        reasons.append(f"risk_hint={risk_hint}")
        reasons.append(f"domain={domain} has {base_risk.value} risk")
        triggered_policies.append("domain_risk_requires_approval")

    # ── Priority 3: ALLOW (safe) ──────────────────────────────────
    else:
        decision = Decision.ALLOW
        risk_level = RiskLevel.LOW
        risk_score = 0.10
        reasons.append(f"risk_hint={risk_hint}, domain={domain}")

    return DecisionResponse(
        run_id=action.run_id,
        action_id=action.action_id,
        decision=decision,
        risk_level=risk_level,
        risk_score=risk_score,
        reasons=reasons,
        triggered_policies=triggered_policies,
        next_step="approval_queue" if decision == Decision.NEED_APPROVAL else "execute",
        latency_ms=int((perf_counter() - started) * 1000),
    )
