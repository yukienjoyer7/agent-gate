from fastapi import APIRouter, HTTPException

from app.domains.clarification.schemas import UserResponseRequest
from app.domains.clarification.services import decide_pending_question, list_pending_questions

router = APIRouter(tags=["ask-user"])


@router.get("/ask-user")
async def list_ask_user() -> list[dict]:
    return [item.model_dump(mode="json") for item in await list_pending_questions()]


@router.post("/ask-user/{action_id}/respond")
async def respond_ask_user(action_id: str, body: UserResponseRequest) -> dict:
    result = await decide_pending_question(action_id, body)
    if result is None:
        raise HTTPException(status_code=404, detail="pending question not found")

    outcome, payload = result
    if outcome == "expired":
        raise HTTPException(
            status_code=410,
            detail={
                "message": "ask-user window expired; response no longer applies",
                "audit": payload.model_dump(mode="json"),
            },
        )
    if outcome == "still_pending":
        # Not resolved -- still not enough information to evaluate. Back in
        # the pending_user_questions queue for another round; caller should
        # treat this the same as the original ASK_USER response and prompt
        # for the (still) missing details.
        return {"outcome": outcome, **payload.model_dump(mode="json")}
    return payload.model_dump(mode="json")
