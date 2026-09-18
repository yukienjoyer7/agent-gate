from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.domains.connector.stripe.webhook import (
    StripeWebhookMisconfiguredError,
    StripeWebhookService,
    StripeWebhookVerificationError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stripe", tags=["stripe"])
stripe_webhook_service = StripeWebhookService()


class StripeWebhookResponse(BaseModel):
    ok: bool = Field(
        default=True,
        description="Whether the webhook was received and handled",
        examples=[True],
    )
    status: Literal["processed", "duplicate"] = Field(
        ...,
        description="Processing outcome status",
        examples=["processed"],
    )
    event_id: str = Field(
        ...,
        description="Stripe event identifier",
        examples=["evt_1N2O3P4Q5R6S7T8U9V0W1X2Y"],
    )
    event_type: str = Field(
        ...,
        description="Stripe event type name",
        examples=["payment_intent.succeeded"],
    )


@router.post(
    "/webhook",
    response_model=StripeWebhookResponse,
    summary="Stripe Webhook Handler",
    description="Receive, verify Stripe HMAC signatures, and process incoming Stripe webhook events.",
    responses={
        400: {"description": "Invalid signature or malformed payload"},
        503: {"description": "Stripe webhook misconfigured or processing unavailable"},
    },
)
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(
        default=None,
        alias="Stripe-Signature",
        description="Stripe webhook signature header (t=...,v1=...)",
    ),
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
