from time import perf_counter

from app.core.schemas import ActionRequest, Decision, DecisionResponse, RiskLevel


def decide(action: ActionRequest) -> DecisionResponse:
    started = perf_counter()
    risky = action.risk_hint in {"external_send", "payment", "destructive", "bulk_action"}
    return DecisionResponse(
        run_id=action.run_id,
        action_id=action.action_id,
        decision=Decision.NEED_APPROVAL if risky else Decision.ALLOW,
        risk_level=RiskLevel.HIGH if risky else RiskLevel.LOW,
        risk_score=0.8 if risky else 0.1,
        reasons=[f"risk_hint={action.risk_hint}"],
        triggered_policies=["risky_action_requires_approval"] if risky else [],
        next_step="approval_queue" if risky else "execute",
        latency_ms=int((perf_counter() - started) * 1000),
    )
