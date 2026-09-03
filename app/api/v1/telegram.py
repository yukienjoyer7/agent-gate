from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from app.domains.connector.telegram.service import (
    TelegramService,
    TelegramWebhookAuthError,
    TelegramWebhookMisconfiguredError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/telegram", tags=["telegram"])
telegram_service = TelegramService()


@router.post("/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_secret: str | None = Header(
        default=None,
        alias="X-Telegram-Bot-Api-Secret-Token",
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
