from typing import Any, Literal

<<<<<<< HEAD
from app.domains.agent.services import run_guarded_action
=======
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from app.domains.agent.services.browser_prototype_agent import run_browser_prototype_agent
>>>>>>> bb636b9 (feat: adding browser executor demo file and additional recovery feature)
from app.domains.audit.repositories import get_audit_repository

router = APIRouter(prefix="/actions", tags=["actions"])


<<<<<<< HEAD
@router.post("/run")
async def run_action(proposal: dict) -> dict:
    event = await run_guarded_action(proposal)
=======
class BrowserPrototypeRequest(BaseModel):
    user_goal: str = "run browser prototype action"
    url: str
    action: dict[str, Any] | None = Field(
        default=None,
        examples=[{"type": "click", "label": "Continue"}],
    )
    actions: list[dict[str, Any]] | None = Field(
        default=None,
        examples=[
            [
                {"type": "screenshot", "path": "data/browser/screenshots/before.png"},
                {"type": "click", "label": "Continue"},
                {"type": "screenshot", "path": "data/browser/screenshots/after.png"},
            ]
        ],
    )
    risk_hint: str = "unknown"
    timeout_ms: int = Field(default=15_000, ge=1_000, le=60_000)
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = "domcontentloaded"

    @model_validator(mode="after")
    def require_one_action_shape(self) -> "BrowserPrototypeRequest":
        if self.action and self.actions:
            raise ValueError("pass either action or actions, not both")
        return self


@router.post("/prototype/browser")
async def run_browser_prototype(request: BrowserPrototypeRequest) -> dict:
    event = await run_browser_prototype_agent(
        url=request.url,
        action=request.action,
        actions=request.actions,
        user_goal=request.user_goal,
        risk_hint=request.risk_hint,
        timeout_ms=request.timeout_ms,
        wait_until=request.wait_until,
    )
>>>>>>> bb636b9 (feat: adding browser executor demo file and additional recovery feature)
    return event.model_dump(mode="json")


@router.get("/{action_id}")
async def get_action(action_id: str) -> dict:
    event = await get_audit_repository().by_action(action_id)
    if event is None:
        raise HTTPException(status_code=404, detail="action not found")
    return event.model_dump(mode="json")
