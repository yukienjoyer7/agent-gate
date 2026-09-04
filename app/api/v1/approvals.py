from fastapi import APIRouter

from app.domains.audit.repositories import get_audit_repository

router = APIRouter(tags=["approvals"])


@router.get("/approvals")
async def list_pending_approvals() -> list[dict]:
    return [
        event.model_dump(mode="json")
        for event in await get_audit_repository().list()
        if event.execution_status == "PENDING_APPROVAL"
    ]
