from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.core.schemas import ExecutionStatus


class PendingUserQuestionResponse(BaseModel):
    """
    Shape returned for a row still sitting in `pending_user_questions` --
    NOT an AuditEvent, mirroring PendingApprovalResponse: nothing is
    written to audit_logs until the action resolves. Returned by
    GET /ask-user and by run_guarded_action() itself when a proposal
    comes back ASK_USER.
    """

    run_id: str
    action_id: str
    execution_status: ExecutionStatus = ExecutionStatus.PENDING_USER_INPUT
    clarifying_question: str | None = None
    clarification_round: int = 1
    request_json: dict[str, Any]
    decision_json: dict[str, Any]
    pending_since: datetime
    expires_at: datetime


class UserResponseRequest(BaseModel):
    """
    Body for POST /ask-user/{action_id}/respond.

    proceed=False cancels the action outright -- no re-decide, no
    execution, one CANCELLED audit row.

    proceed=True re-decides the action before executing: payload_updates
    (if given) are merged into the original payload first, letting the
    user correct/fill in whatever made the proposal incomplete. proceed
    alone carries no information -- confidence is never touched, and
    nothing is "vouched for" -- only the merged payload can change the
    outcome. Re-running decide() (rather than executing directly) means a
    correction can still hit BLOCK/SANITIZE/NEED_APPROVAL instead of
    executing blindly.
    """

    proceed: bool
    payload_updates: dict[str, Any] | None = Field(default=None)
