from fastapi import APIRouter, Depends

from app.api.audit_scope import event_belongs_to_owner
from app.api.session_context import OwnerContext, get_owner_context
from app.core.action_schema import ExecutionStatus
from app.core.audit_schema import AuditEvent
from app.domains.audit.repositories import get_audit_repository

router = APIRouter(tags=["approvals"])


@router.get(
    "/approvals",
    response_model=list[AuditEvent],
    summary="List Pending Approvals",
    description="List all audit event records currently in PENDING_APPROVAL status awaiting human decision.",
)
async def list_pending_approvals(
    owner: OwnerContext = Depends(get_owner_context),
) -> list[AuditEvent]:
    return [
        event
        for event in await get_audit_repository().list()
        if event.execution_status == ExecutionStatus.PENDING_APPROVAL
        and event_belongs_to_owner(event, owner)
    ]
