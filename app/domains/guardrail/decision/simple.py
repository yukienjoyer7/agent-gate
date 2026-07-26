from time import perf_counter

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


def decide(action: ActionRequest) -> DecisionResponse:
    started = perf_counter()
    domain = action.domain or "productivity"
    risk_hint = action.risk_hint or "unknown"

    base_risk = DOMAIN_RISK.get(domain, RiskLevel.LOW)
    reasons: list[str] = []
    triggered_policies: list[str] = []

    # ── Priority 1: BLOCK (destructive / unauthorized) ────────────
    if risk_hint in BLOCK_HINTS:
        decision = Decision.BLOCK
        risk_level = RiskLevel.CRITICAL
        risk_score = 0.95
        reasons.append(f"blocked: {risk_hint}")
        triggered_policies.append("block_destructive_action")

    # ── Priority 2: NEED_APPROVAL (risky action types / high-risk domains) ────
    elif risk_hint in NEED_APPROVAL_HINTS or base_risk in (RiskLevel.CRITICAL, RiskLevel.HIGH):
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
