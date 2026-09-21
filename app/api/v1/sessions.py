"""Browser-scoped session lifecycle endpoints for the demo frontend."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.api.session_context import OwnerContext, require_browser_session
from app.config.settings import get_settings
from app.domains.sessions.service import create_browser_session, end_browser_session

router = APIRouter(prefix="/sessions", tags=["sessions"])


class BrowserSessionResponse(BaseModel):
    session_id: str = Field(..., description="Opaque ID for this browser tab session")
    idle_ttl_seconds: int = Field(..., description="Inactivity limit before server-side expiry")


class BrowserSessionHeartbeatResponse(BaseModel):
    active: Literal[True] = True


class BrowserSessionEndRequest(BaseModel):
    session_id: str = Field(..., min_length=43, max_length=43)
    reason: Literal["pagehide", "refresh", "reset", "replaced"] = "pagehide"


class BrowserSessionEndResponse(BaseModel):
    ended: bool


@router.post("", response_model=BrowserSessionResponse, summary="Create Browser Session")
async def create_session(response: Response) -> BrowserSessionResponse:
    session_id = await create_browser_session()
    response.headers["Cache-Control"] = "no-store"
    return BrowserSessionResponse(
        session_id=session_id,
        idle_ttl_seconds=get_settings().BROWSER_SESSION_IDLE_TTL_SEC,
    )


@router.post(
    "/heartbeat",
    response_model=BrowserSessionHeartbeatResponse,
    summary="Keep Browser Session Active",
)
async def heartbeat_session(
    _context: OwnerContext = Depends(require_browser_session),
) -> BrowserSessionHeartbeatResponse:
    return BrowserSessionHeartbeatResponse(active=True)


@router.post(
    "/end",
    response_model=BrowserSessionEndResponse,
    summary="End Browser Session",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": BrowserSessionEndRequest.model_json_schema(),
                },
                "text/plain": {
                    "schema": {"type": "string", "description": "JSON payload sent by sendBeacon"},
                },
            },
        },
    },
)
async def end_session(request: Request) -> BrowserSessionEndResponse:
    try:
        payload = await request.json()
        close_request = BrowserSessionEndRequest.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - beacon payloads may use text/plain
        raise HTTPException(status_code=422, detail="invalid browser session end payload") from exc
    ended = await end_browser_session(close_request.session_id, close_request.reason)
    return BrowserSessionEndResponse(ended=ended)
