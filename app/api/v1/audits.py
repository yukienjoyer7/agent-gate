from typing import Any

from fastapi import APIRouter, Query

from app.core.audit_schema import AuditEvent
from app.domains.audit.repositories import get_audit_repository

router = APIRouter(tags=["audits"])


@router.get(
    "/audits",
    response_model=list[AuditEvent],
    summary="List Audit Events",
    description="Query audit event logs with optional filtering by run ID.",
)
async def list_audits(
    run_id: str | None = Query(
        default=None,
        description="Optional run ID to filter audit events",
        examples=["run_634a174c8449"],
    ),
) -> list[AuditEvent]:
    repo = get_audit_repository()
    events = await repo.by_run(run_id) if run_id else await repo.list()
    return events


@router.get(
    "/audits/latest",
    response_model=AuditEvent | dict[str, Any],
    summary="Get Latest Audit Event",
    description="Retrieve the most recently written audit event from the repository, or an empty object if no events exist.",
)
async def latest_audit() -> AuditEvent | dict[str, Any]:
    event = await get_audit_repository().latest()
    return event if event else {}
