"""Stripe webhook signature verification and durable event processing."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import stripe

from app.config.settings import Settings, get_settings
from app.domains.connector.stripe.repository import StripePaymentRepository, StripePaymentStore


class StripeWebhookMisconfiguredError(Exception):
    """Raised when a webhook secret is unavailable."""


class StripeWebhookVerificationError(Exception):
    """Raised when Stripe signature or payload verification fails."""


class StripeWebhookService:
    def __init__(
        self,
        *,
        settings_factory: Callable[[], Settings] = get_settings,
        payment_store: StripePaymentStore | None = None,
        event_constructor: Callable[..., Any] = stripe.Webhook.construct_event,
    ) -> None:
        self._settings_factory = settings_factory
        self._payments = payment_store or StripePaymentRepository()
        self._event_constructor = event_constructor

    async def handle(self, payload: bytes, signature: str | None) -> dict[str, Any]:
        settings = self._settings_factory()
        if not settings.STRIPE_WEBHOOK_SECRET:
            raise StripeWebhookMisconfiguredError("Stripe webhook secret is not configured")
        if not signature:
            raise StripeWebhookVerificationError("missing Stripe signature")

        try:
            raw_event = self._event_constructor(
                payload,
                signature,
                settings.STRIPE_WEBHOOK_SECRET,
                tolerance=settings.STRIPE_WEBHOOK_TOLERANCE_SEC,
            )
        except (ValueError, stripe.SignatureVerificationError) as exc:
            raise StripeWebhookVerificationError("invalid Stripe webhook") from exc

        event = _event_dict(raw_event)
        if not event.get("id") or not event.get("type"):
            raise StripeWebhookVerificationError("malformed Stripe webhook event")
        processed = await self._payments.process_event(event)
        return {
            "ok": True,
            "status": "processed" if processed else "duplicate",
            "event_id": event["id"],
            "event_type": event["type"],
        }


def _event_dict(event: Any) -> dict[str, Any]:
    if isinstance(event, dict):
        return _plain_value(event)
    to_dict = getattr(event, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
        return _plain_value(value) if isinstance(value, dict) else {}
    return {}


def _plain_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _plain_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain_value(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _plain_value(to_dict())
    return value
