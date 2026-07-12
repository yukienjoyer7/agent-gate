from fastapi import APIRouter

from app.domains.audit.repositories import get_audit_repository

router = APIRouter(tags=["benchmark"])


@router.get("/benchmark")
async def benchmark_summary() -> dict:
    events = await get_audit_repository().list()
    totals = [event.latency.get("total_ms", 0) for event in events]
    return {
        "action_count": len(events),
        "avg_total_ms": int(sum(totals) / len(totals)) if totals else 0,
        "latest_status": events[-1].execution_status if events else None,
    }
