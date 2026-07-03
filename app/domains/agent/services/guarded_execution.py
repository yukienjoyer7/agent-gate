from app.core.action_request import build_action_request
from app.core.schemas import AuditEvent
from app.domains.audit.repositories import AuditRepository
from app.domains.guardrail.decision import decide
from app.executors import ExecutionRouter


async def run_guarded_action(proposal: dict, audit: AuditRepository | None = None) -> AuditEvent:
    request = build_action_request(proposal)
    decision = decide(request)
    execution = await ExecutionRouter().route(request, decision)
    return (audit or AuditRepository()).write(request, decision, execution)
