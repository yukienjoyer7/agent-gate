from __future__ import annotations

import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.domains.connector.telegram.service import (
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
    status: Literal["accepted", "ignored", "duplicate"] = Field(
        ...,
        description="Processing outcome status for the update",
        examples=["accepted"],
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
