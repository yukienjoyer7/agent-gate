from fastapi import APIRouter

from app.domains.audit.repositories import get_audit_repository

router = APIRouter(prefix="/runs", tags=["runs"])


@router.get("")
async def list_runs() -> list[dict]:
    runs: dict[str, dict] = {}
    for event in await get_audit_repository().list():
        current = runs.setdefault(
            event.run_id,
            {"run_id": event.run_id, "action_count": 0, "latest_status": event.execution_status},
        )
        current["action_count"] += 1
        current["latest_status"] = event.execution_status
        current["updated_at"] = event.created_at
    return list(runs.values())


@router.get("/{run_id}/actions")
async def list_run_actions(run_id: str) -> list[dict]:
    return [event.model_dump(mode="json") for event in await get_audit_repository().by_run(run_id)]
