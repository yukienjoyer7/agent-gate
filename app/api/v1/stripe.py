from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from app.domains.connector.stripe.webhook import (
    StripeWebhookMisconfiguredError,
    StripeWebhookService,
    StripeWebhookVerificationError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stripe", tags=["stripe"])
stripe_webhook_service = StripeWebhookService()


@router.post("/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
) -> dict[str, Any]:
    # Signature verification requires the exact bytes Stripe sent. Calling
    # request.json() before verification would invalidate the signature.
    payload = await request.body()
    try:
        return await stripe_webhook_service.handle(payload, stripe_signature)
    except StripeWebhookMisconfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except StripeWebhookVerificationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - non-2xx makes Stripe retry delivery
        logger.exception("Stripe webhook persistence failed")
        raise HTTPException(
            status_code=503, detail="Stripe webhook processing unavailable"
        ) from exc
