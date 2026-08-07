from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from app.core.schemas import ExecutionStatus


class PendingApprovalResponse(BaseModel):
    """
    Shape returned for a row still sitting in `pending_approvals` --
    NOT an AuditEvent, since (per the design) nothing is written to
    audit_logs until the action resolves. Returned by GET /approvals and
    by run_guarded_action() itself when a proposal comes back
    NEED_APPROVAL, so callers of run_guarded_action always get *something*
    with .model_dump(), whether or not the action has resolved yet.
    """

    run_id: str
    action_id: str
    execution_status: ExecutionStatus = ExecutionStatus.PENDING_APPROVAL
    request_json: dict[str, Any]
    decision_json: dict[str, Any]
    pending_since: datetime
    expires_at: datetime


class ApprovalDecision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class ApprovalDecisionRequest(BaseModel):
    decision: ApprovalDecision
