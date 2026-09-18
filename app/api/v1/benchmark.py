from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.core.action_schema import ExecutionStatus
from app.domains.audit.repositories import get_audit_repository

router = APIRouter(tags=["benchmark"])


class BenchmarkSummaryResponse(BaseModel):
    action_count: int = Field(
        ...,
        description="Total number of evaluated actions recorded in the audit repository",
        examples=[10],
    )
    avg_total_ms: int = Field(
        ...,
        description="Average execution latency across all actions in milliseconds",
        examples=[45],
    )
    latest_status: ExecutionStatus | None = Field(
        default=None,
        description="Execution status of the most recent action, or null if no actions exist",
        examples=[ExecutionStatus.SUCCESS],
    )


@router.get(
    "/benchmark",
    response_model=BenchmarkSummaryResponse,
    summary="Get Benchmark Latency Summary",
    description="Return aggregate execution counts, average latency, and latest execution status across all recorded audit actions.",
)
async def benchmark_summary() -> BenchmarkSummaryResponse:
    events = await get_audit_repository().list()
    totals = [event.latency.get("total_ms", 0) for event in events]
    return BenchmarkSummaryResponse(
        action_count=len(events),
        avg_total_ms=int(sum(totals) / len(totals)) if totals else 0,
        latest_status=events[-1].execution_status if events else None,
    )
