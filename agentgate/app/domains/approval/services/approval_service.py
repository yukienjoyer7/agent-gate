"""
Service layer for the pending-approval flow described in
pending-approval-design.md. Ties together:

  - PendingApprovalRepository (the mutable working queue)
  - the audit repository (the immutable, write-once-per-action record)
  - ExecutionRouter.execute() (the real executor, invoked only once an
    action is actually approved)

so that api/v1/approvals.py stays a thin HTTP layer.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.schemas import ActionRequest, AuditEvent, DecisionResponse, ExecutionResult, ExecutionStatus
from app.database.models.pending_approval import PendingApproval as PendingApprovalRow
from app.domains.approval.repositories.pending_approval_repository import PendingApprovalRepository
from app.domains.approval.schemas.pending_approval import ApprovalDecision, PendingApprovalResponse
from app.domains.audit.repositories import get_audit_repository
from app.executors import ExecutionRouter

_REVIEWER_DECISION = {
    ApprovalDecision.APPROVE: "APPROVED",
    ApprovalDecision.REJECT: "REJECTED",
}


def _to_response(row: PendingApprovalRow) -> PendingApprovalResponse:
    return PendingApprovalResponse(
        run_id=row.run_id,
        action_id=row.action_id,
        request_json=row.request_json,
        decision_json=row.decision_json,
        pending_since=row.pending_since,
        expires_at=row.expires_at,
    )


async def create_pending_approval(
    request: ActionRequest,
    decision: DecisionResponse,
    repo: PendingApprovalRepository | None = None,
) -> PendingApprovalResponse:
    row = await (repo or PendingApprovalRepository()).create(request, decision)
    return _to_response(row)


async def list_pending_approvals(
    repo: PendingApprovalRepository | None = None,
    audit=None,
) -> list[PendingApprovalResponse]:
    """
    GET /approvals. Per design doc step 2: any row whose expires_at has
    already passed is lazily resolved to EXPIRED (one row written to
    audit_logs, removed from the queue) right here, before the list is
    returned -- so expired items never show up as "pending".
    """
    repo = repo or PendingApprovalRepository()
    audit = audit or get_audit_repository()
    now = datetime.now(UTC)

    active: list[PendingApprovalResponse] = []
    for row in await repo.list():
        if row.expires_at <= now:
            await _finalize_expired(row.action_id, repo=repo, audit=audit)
        else:
            active.append(_to_response(row))
    return active


async def decide_pending_approval(
    action_id: str,
    decision: ApprovalDecision,
    repo: PendingApprovalRepository | None = None,
    audit=None,
) -> tuple[str, AuditEvent] | None:
    """
    POST /approvals/{action_id}/decide.

    Returns None if the row doesn't exist -- already resolved by a
    concurrent call, already expired and swept, or never existed --
    caller should return 404 in that case (design doc step 3c).

    Otherwise returns ("resolved", event) for a normal approve/reject
    (3a), or ("expired", event) if the row had already passed expires_at
    by the time this call claimed it (3b) -- caller should return 410 in
    that case.

    repo.claim() is a single atomic DELETE ... RETURNING, so if two
    decide calls race for the same action_id only one gets the row back;
    the other gets None here and returns 404. See the repository's
    docstring (design doc open question 4).
    """
    repo = repo or PendingApprovalRepository()
    audit = audit or get_audit_repository()

    row = await repo.claim(action_id)
    if row is None:
        return None

    now = datetime.now(UTC)
    if row.expires_at <= now:
        event = await _resolve(row, reviewer_decision=None, expired=True, audit=audit)
        return ("expired", event)

    event = await _resolve(row, reviewer_decision=decision, expired=False, audit=audit)
    return ("resolved", event)


async def _finalize_expired(action_id: str, repo: PendingApprovalRepository, audit) -> None:
    row = await repo.claim(action_id)
    if row is None:
        return  # a concurrent decide()/list() call already resolved this one
    await _resolve(row, reviewer_decision=None, expired=True, audit=audit)


async def _resolve(
    row: PendingApprovalRow,
    reviewer_decision: ApprovalDecision | None,
    expired: bool,
    audit,
) -> AuditEvent:
    request = ActionRequest.model_validate(row.request_json)
    decision = DecisionResponse.model_validate(row.decision_json)

    resolved_at = row.expires_at if expired else datetime.now(UTC)
    pending_duration_ms = int((resolved_at - row.pending_since).total_seconds() * 1000)

    resolved_decision = decision.model_copy(
        update={
            "reviewer_decision": (
                _REVIEWER_DECISION[reviewer_decision] if reviewer_decision else None
            ),
            "pending_duration_ms": pending_duration_ms,
        }
    )

    if expired:
        execution = _stub_result(request, ExecutionStatus.EXPIRED, "approval window expired")
    elif reviewer_decision == ApprovalDecision.APPROVE:
        execution = await ExecutionRouter().execute(request)
    else:
        execution = _stub_result(request, ExecutionStatus.REJECTED, "rejected by reviewer")

    return await audit.write(
        request,
        resolved_decision,
        execution,
        latency={"guardrail_ms": decision.latency_ms, "executor_ms": execution.latency_ms},
    )


def _stub_result(request: ActionRequest, status: ExecutionStatus, summary: str) -> ExecutionResult:
    return ExecutionResult(
        run_id=request.run_id,
        action_id=request.action_id,
        executor="approval",
        status=status,
        result_summary=summary,
    )
