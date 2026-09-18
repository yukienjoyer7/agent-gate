from datetime import datetime

from fastapi import APIRouter, Path
from pydantic import BaseModel, Field

from app.core.action_schema import ExecutionStatus
from app.core.audit_schema import AuditEvent
from app.domains.audit.repositories import get_audit_repository

router = APIRouter(prefix="/runs", tags=["runs"])


class RunSummaryResponse(BaseModel):
    run_id: str = Field(..., description="Unique run identifier", examples=["run_634a174c8449"])
    action_count: int = Field(..., description="Total number of actions executed within this run", examples=[2])
    latest_status: ExecutionStatus = Field(
        ...,
        description="Execution status of the most recent action in the run",
        examples=[ExecutionStatus.SUCCESS],
    )
    updated_at: datetime | None = Field(
        default=None,
        description="Timestamp of the most recently executed action in this run",
    )


@router.get(
    "",
    response_model=list[RunSummaryResponse],
    summary="List Runs",
    description="List all agent execution runs with aggregate action counts, latest execution status, and last update timestamp.",
)
async def list_runs() -> list[RunSummaryResponse]:
    runs: dict[str, dict] = {}
    for event in await get_audit_repository().list():
        current = runs.setdefault(
            event.run_id,
            {"run_id": event.run_id, "action_count": 0, "latest_status": event.execution_status},
        )
        current["action_count"] += 1
        current["latest_status"] = event.execution_status
        current["updated_at"] = event.created_at
    return [RunSummaryResponse(**r) for r in runs.values()]


@router.get(
    "/{run_id}/actions",
    response_model=list[AuditEvent],
    summary="List Run Actions",
    description="Retrieve all audit event records for actions belonging to the specified run ID.",
)
async def list_run_actions(
    run_id: str = Path(..., description="Unique run identifier", examples=["run_634a174c8449"]),
) -> list[AuditEvent]:
    return await get_audit_repository().by_run(run_id)
