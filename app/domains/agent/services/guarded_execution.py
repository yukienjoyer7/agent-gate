from app.core.action_request import build_action_request
from app.core.schemas import ActionTrace, AuditEvent
from app.domains.audit.repositories import get_audit_repository
from app.domains.guardrail.decision import decide
from app.executors import ExecutionRouter
from app.tracing import LatencyTracker, TraceWriter


async def run_guarded_action(
    proposal: dict,
    audit=None,
    traces: TraceWriter | None = None,
) -> AuditEvent:
    latency = LatencyTracker()
    latency.start("action_request")
    request = build_action_request(proposal)
    latency.stop("action_request")

    latency.start("guardrail")
    decision = decide(request)
    latency.stop("guardrail")

    latency.start("executor")
    execution = await ExecutionRouter().route(request, decision)
    latency.stop("executor")

    latency.start("audit_write")
    audit_latency = latency.values()
    audit_latency["audit_write_ms"] = 0
    event = await (audit or get_audit_repository()).write(request, decision, execution, audit_latency)
    latency.stop("audit_write")

    (traces or TraceWriter()).write(
        ActionTrace(
            run_id=request.run_id,
            action_id=request.action_id,
            user_goal=proposal.get("user_goal", ""),
            raw_tool_call=proposal,
            action_request=request.model_dump(mode="json", exclude={"payload"}),
            decision=decision.model_dump(mode="json"),
            execution=execution.model_dump(mode="json"),
            audit=event.model_dump(mode="json"),
            latency=latency.values(),
            final_status=execution.status,
        )
    )
    return event
