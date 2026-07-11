from fastapi import APIRouter, HTTPException

from app.domains.audit.repositories import get_audit_repository

router = APIRouter(prefix="/actions", tags=["actions"])


@router.get("/{action_id}")
async def get_action(action_id: str) -> dict:
    event = await get_audit_repository().by_action(action_id)
    if event is None:
        raise HTTPException(status_code=404, detail="action not found")
    return event.model_dump(mode="json")
