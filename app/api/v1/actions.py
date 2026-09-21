from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field, model_validator

from app.api.audit_scope import event_belongs_to_owner
from app.api.session_context import OwnerContext, get_owner_context
from app.config.settings import get_settings
from app.core.audit_schema import AuditEvent
from app.domains.agent.services import run_guarded_action
from app.domains.agent.services.browser_prototype_agent import run_browser_prototype_agent
from app.domains.audit.repositories import get_audit_repository

router = APIRouter(prefix="/actions", tags=["actions"])


class ActionProposalRequest(BaseModel):
    target_system: str = Field(
        ...,
        description="Target system or connector (e.g. 'local_file', 'browser', 'github', 'gmail', 'calendar', 'stripe')",
        examples=["local_file"],
    )
    action_type: str = Field(
        ...,
        description="Action type classification (e.g. 'FILE_READ', 'API_CALL', 'BROWSER_CLICK')",
        examples=["FILE_READ"],
    )
    target: str | dict[str, Any] | None = Field(
        default=None,
        description=(
            "Target resource, endpoint, path, or identifier; omitted or null "
            "defaults to target_system"
        ),
        examples=["sample.txt"],
    )
    domain: str | None = Field(
        default=None,
        description="Security policy domain (e.g. 'code_data', 'productivity', 'booking', 'global_safety')",
    )
    source: str = Field(
        default="cli",
        description="Origin source of the action proposal",
        examples=["cli"],
    )
    run_id: str | None = Field(
        default=None,
        description="Optional run ID to group this action under",
        examples=["run_634a174c8449"],
    )
    action_id: str | None = Field(
        default=None,
        description="Optional pre-generated action ID",
        examples=["act_a1b2c3d4e5f6"],
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Action-specific payload parameters",
        examples=[{"action": "read", "path": "sample.txt"}],
    )
    recipient_reference: str | None = Field(
        default=None,
        description="Optional recipient reference (e.g. username, contact handle)",
    )
    resolved_recipient: dict[str, Any] | None = Field(
        default=None,
        description="Optional pre-resolved recipient metadata",
    )
    user_goal: str = Field(
        default="",
        description="User goal or high-level instruction context",
        examples=["read demo file"],
    )
    content_context: str = Field(
        default="",
        description="Surrounding content context for guardrail evaluation",
    )
    payload_summary: str | None = Field(
        default=None,
        description=(
            "Optional precomputed payload summary; omitted or null is "
            "generated from payload"
        ),
    )
    risk_hint: str = Field(
        default="unknown",
        description="Risk classification hint for the guardrail engine",
        examples=["file_read"],
    )
    rollback_available: bool = Field(
        default=False,
        description="Whether a rollback strategy exists for this action",
    )
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Confidence score of the proposed action",
    )

    model_config = {"extra": "allow"}


class BrowserPrototypeRequest(BaseModel):
    user_goal: str = Field(
        default="run browser prototype action",
        description="High-level user goal for the browser interaction",
        examples=["run browser prototype action"],
    )
    url: str = Field(
        ...,
        description="Target webpage URL to navigate to",
        examples=["https://example.com"],
    )
    action: dict[str, Any] | None = Field(
        default=None,
        description="Single browser action specification",
        examples=[{"type": "click", "label": "Continue"}],
    )
    actions: list[dict[str, Any]] | None = Field(
        default=None,
        description="Sequence of browser actions to execute",
        examples=[
            [
                {"type": "screenshot", "path": "data/browser/screenshots/before.png"},
                {"type": "click", "label": "Continue"},
                {"type": "screenshot", "path": "data/browser/screenshots/after.png"},
            ]
        ],
    )
    risk_hint: str = Field(
        default="unknown",
        description="Risk hint for browser action evaluation",
        examples=["unknown"],
    )
    timeout_ms: int = Field(
        default_factory=lambda: get_settings().BROWSER_TIMEOUT_MS,
        ge=1_000,
        le=60_000,
        description="Browser navigation and action timeout in milliseconds",
    )
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = Field(
        default_factory=lambda: get_settings().BROWSER_WAIT_UNTIL,
        description="Page lifecycle event to wait for on navigation",
    )

    @model_validator(mode="after")
    def require_one_action_shape(self) -> "BrowserPrototypeRequest":
        if self.action and self.actions:
            raise ValueError("pass either action or actions, not both")
        return self


@router.post(
    "/run",
    response_model=AuditEvent,
    summary="Run Guarded Action",
    description="Evaluate an action proposal through guardrails, execute it if permitted, and record the complete audit trace.",
)
async def run_action(
    proposal: ActionProposalRequest,
    owner: OwnerContext = Depends(get_owner_context),
) -> AuditEvent:
    proposal_dict = proposal.model_dump(exclude_unset=True)
    proposal_dict["owner_id"] = owner.owner_id
    proposal_dict["session_id"] = owner.session_id
    return await run_guarded_action(proposal_dict)


@router.post(
    "/prototype/browser",
    response_model=AuditEvent,
    summary="Run Browser Prototype Action",
    description="Execute browser prototype actions against a target URL with snapshot inspection, risk classification, and audit logging.",
)
async def run_browser_prototype(
    request: BrowserPrototypeRequest,
    owner: OwnerContext = Depends(get_owner_context),
) -> AuditEvent:
    return await run_browser_prototype_agent(
        url=request.url,
        action=request.action,
        actions=request.actions,
        user_goal=request.user_goal,
        risk_hint=request.risk_hint,
        timeout_ms=request.timeout_ms,
        wait_until=request.wait_until,
        owner_id=owner.owner_id,
        session_id=owner.session_id,
    )


@router.get(
    "/{action_id}",
    response_model=AuditEvent,
    summary="Get Action Audit Event",
    description="Retrieve the recorded audit event for a specific action ID.",
    responses={404: {"description": "Action not found"}},
)
async def get_action(
    action_id: str = Path(..., description="Unique action identifier", examples=["act_a1b2c3d4e5f6"]),
    owner: OwnerContext = Depends(get_owner_context),
) -> AuditEvent:
    event = await get_audit_repository().by_action(action_id)
    if event is None or not event_belongs_to_owner(event, owner):
        raise HTTPException(status_code=404, detail="action not found")
    return event
