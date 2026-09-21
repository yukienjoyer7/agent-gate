"""Resolve the active browser session or the legacy demo owner header."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, HTTPException

from app.api.owner import owner_id_from_header
from app.domains.sessions.service import touch_browser_session, valid_session_id


@dataclass(frozen=True)
class OwnerContext:
    owner_id: str
    session_id: str | None = None


async def get_owner_context(
    x_agentgate_session: str | None = Header(
        default=None,
        alias="X-AgentGate-Session",
        description="Opaque active browser session ID issued by POST /api/v1/sessions",
    ),
    x_agentgate_owner: str | None = Header(
        default=None,
        alias="X-AgentGate-Owner",
        description="Legacy demo owner label; not authentication",
    ),
) -> OwnerContext:
    """Resolve request scope; a supplied but invalid session never falls back."""
    if x_agentgate_session is not None:
        if not await touch_browser_session(x_agentgate_session):
            raise HTTPException(status_code=401, detail="browser session is missing or ended")
        return OwnerContext(owner_id=x_agentgate_session, session_id=x_agentgate_session)
    owner_id = owner_id_from_header(x_agentgate_owner)
    if valid_session_id(owner_id):
        raise HTTPException(
            status_code=400,
            detail="browser session IDs must be sent with X-AgentGate-Session",
        )
    return OwnerContext(owner_id=owner_id)


async def require_browser_session(
    x_agentgate_session: str | None = Header(
        default=None,
        alias="X-AgentGate-Session",
        description="Opaque active browser session ID issued by POST /api/v1/sessions",
    ),
) -> OwnerContext:
    if x_agentgate_session is None or not await touch_browser_session(x_agentgate_session):
        raise HTTPException(status_code=401, detail="active browser session required")
    return OwnerContext(owner_id=x_agentgate_session, session_id=x_agentgate_session)
