from fastapi import APIRouter, HTTPException

from app.domains.agent.services import run_guarded_action
from app.domains.audit.repositories import get_audit_repository

router = APIRouter(prefix="/actions", tags=["actions"])


@router.post("/run")
async def run_action(proposal: dict) -> dict:
    event = await run_guarded_action(proposal)
    return event.model_dump(mode="json")


@router.get("/{action_id}")
async def get_action(action_id: str) -> dict:
    event = await get_audit_repository().by_action(action_id)
    if event is None:
        raise HTTPException(status_code=404, detail="action not found")
    return event.model_dump(mode="json")
