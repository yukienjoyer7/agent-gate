from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from app.api.session_context import OwnerContext, get_owner_context, require_browser_session
from app.domains.connector.telegram.service import (
    TelegramConnectionRequiredError,
    TelegramService,
    TelegramWebhookAuthError,
    TelegramWebhookMisconfiguredError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/telegram", tags=["telegram"])
telegram_service = TelegramService()


class TelegramUpdate(BaseModel):
    update_id: int | None = Field(
        default=None,
        description="The update's unique identifier",
        examples=[10000],
    )
    message: dict[str, Any] | None = Field(
        default=None,
        description="New incoming message of any kind — text, photo, sticker, etc.",
    )
    callback_query: dict[str, Any] | None = Field(
        default=None,
        description="New incoming callback query from an inline keyboard button",
    )

    model_config = {"extra": "allow"}


class TelegramWebhookResponse(BaseModel):
    ok: bool = Field(
        default=True,
        description="Whether the update was processed or handled safely",
        examples=[True],
    )
    status: Literal[
        "accepted",
        "ignored",
        "duplicate",
        "callback_accepted",
        "callback_rejected",
        "callback_ignored",
        "callback_duplicate",
    ] = Field(
        ...,
        description="Processing outcome status for the update",
        examples=["accepted", "callback_accepted"],
    )
    run_id: str | None = Field(
        default=None,
        description="Agent run identifier if a new run was created or responded to",
        examples=["run_634a174c8449"],
    )
    decision: str | None = Field(
        default=None,
        description="Approval or decline decision from callback query",
        examples=["approve"],
    )
    step_index: int | None = Field(
        default=None,
        description="Step index for callback response",
        examples=[0],
    )


class TelegramConnectResponse(BaseModel):
    connect_url: str
    expires_at: datetime


class TelegramConnectionResponse(BaseModel):
    connected: bool
    username: str | None = None
    display_name: str | None = None


class TelegramContactInvitationRequest(BaseModel):
    alias: str = Field(..., min_length=1, max_length=255)

    @field_validator("alias")
    @classmethod
    def normalize_alias(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("alias must not be blank")
        return normalized


class TelegramContactInvitationResponse(BaseModel):
    invite_url: str
    expires_at: datetime


class TelegramSessionContactResponse(BaseModel):
    contact_id: str
    alias: str
    username: str | None = None
    display_name: str | None = None
    created_at: datetime | None = None


class TelegramContactDeleteResponse(BaseModel):
    deleted: bool


@router.post(
    "/connect",
    response_model=TelegramConnectResponse,
    summary="Create Telegram connection link",
    description="Create a short-lived Telegram deep link for the current AgentGate owner.",
)
async def connect_telegram(
    owner: OwnerContext = Depends(get_owner_context),
) -> TelegramConnectResponse:
    owner_id = owner.owner_id
    try:
        connect_url, expires_at = await telegram_service.create_connection(owner_id)
    except Exception as exc:
        logger.warning(
            "Telegram connection link unavailable",
            extra={"owner_id": "browser_session" if owner.session_id else owner_id},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram connection is not available",
        ) from exc
    return TelegramConnectResponse(connect_url=connect_url, expires_at=expires_at)


@router.get(
    "/connection",
    response_model=TelegramConnectionResponse,
    summary="Get Telegram connection status",
)
async def telegram_connection(
    owner: OwnerContext = Depends(get_owner_context),
) -> TelegramConnectionResponse:
    connection = await telegram_service.connection_status(owner.owner_id)
    return TelegramConnectionResponse(
        connected=connection is not None,
        username=connection.username if connection else None,
        display_name=connection.display_name if connection else None,
    )


@router.delete(
    "/connection",
    response_model=TelegramConnectionResponse,
    summary="Disconnect Telegram",
)
async def disconnect_telegram(
    owner: OwnerContext = Depends(get_owner_context),
) -> TelegramConnectionResponse:
    await telegram_service.disconnect(owner.owner_id)
    return TelegramConnectionResponse(connected=False)


@router.post(
    "/contact-invitations",
    response_model=TelegramContactInvitationResponse,
    summary="Create a session-scoped Telegram contact invitation",
    responses={409: {"description": "The session has no connected Telegram account"}},
)
async def create_contact_invitation(
    body: TelegramContactInvitationRequest,
    owner: OwnerContext = Depends(require_browser_session),
) -> TelegramContactInvitationResponse:
    try:
        invite_url, expires_at = await telegram_service.create_contact_invitation(
            owner.owner_id, body.alias
        )
    except TelegramConnectionRequiredError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("Telegram contact invitation unavailable")
        raise HTTPException(
            status_code=503, detail="Telegram contact invitation is not available"
        ) from exc
    return TelegramContactInvitationResponse(invite_url=invite_url, expires_at=expires_at)


@router.get(
    "/contacts",
    response_model=list[TelegramSessionContactResponse],
    summary="List Telegram contacts in the active browser session",
)
async def list_telegram_contacts(
    owner: OwnerContext = Depends(require_browser_session),
) -> list[TelegramSessionContactResponse]:
    contacts = await telegram_service.list_session_contacts(owner.owner_id)
    return [
        TelegramSessionContactResponse(
            contact_id=contact.contact_id,
            alias=contact.alias,
            username=contact.username,
            display_name=contact.display_name,
            created_at=contact.created_at,
        )
        for contact in contacts
    ]


@router.delete(
    "/contacts/{contact_id}",
    response_model=TelegramContactDeleteResponse,
    summary="Delete a Telegram contact from the active browser session",
)
async def delete_telegram_contact(
    contact_id: str,
    owner: OwnerContext = Depends(require_browser_session),
) -> TelegramContactDeleteResponse:
    deleted = await telegram_service.delete_session_contact(owner.owner_id, contact_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Telegram contact not found")
    return TelegramContactDeleteResponse(deleted=True)


@router.post(
    "/webhook",
    response_model=TelegramWebhookResponse,
    response_model_exclude_none=True,
    summary="Telegram Webhook Handler",
    description="Receive updates from the Telegram Bot API, authenticate via secret header token, and process inbound commands or approval callbacks.",
    responses={
        403: {"description": "Invalid Telegram webhook secret token"},
        503: {"description": "Telegram webhook secret is not configured"},
    },
)
async def telegram_webhook(
    request: Request,
    x_telegram_secret: str | None = Header(
        default=None,
        alias="X-Telegram-Bot-Api-Secret-Token",
        description="Telegram bot secret authentication token header",
    ),
) -> dict[str, Any]:
    try:
        telegram_service.validate_webhook_secret(x_telegram_secret)
    except TelegramWebhookMisconfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except TelegramWebhookAuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    try:
        update = await request.json()
    except json.JSONDecodeError:
        logger.info("Telegram update ignored", extra={"reason": "invalid_json"})
        return {"ok": True, "status": "ignored"}

    if not isinstance(update, dict):
        logger.info("Telegram update ignored", extra={"reason": "malformed"})
        return {"ok": True, "status": "ignored"}
    return await telegram_service.handle_update(update)
