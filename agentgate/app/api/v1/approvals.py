from fastapi import APIRouter, HTTPException

from app.domains.approval.schemas import ApprovalDecisionRequest
from app.domains.approval.services import decide_pending_approval, list_pending_approvals

router = APIRouter(tags=["approvals"])


@router.get("/approvals")
async def list_approvals() -> list[dict]:
    return [item.model_dump(mode="json") for item in await list_pending_approvals()]


@router.post("/approvals/{action_id}/decide")
async def decide_approval(action_id: str, body: ApprovalDecisionRequest) -> dict:
    result = await decide_pending_approval(action_id, body.decision)
    if result is None:
        raise HTTPException(status_code=404, detail="pending approval not found")

    outcome, event = result
    if outcome == "expired":
        raise HTTPException(
            status_code=410,
            detail={
                "message": "approval window expired; decision no longer applies",
                "audit": event.model_dump(mode="json"),
            },
        )
    return event.model_dump(mode="json")
