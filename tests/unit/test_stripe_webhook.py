import asyncio
import json
import time

import stripe

from app.config.settings import Settings
from app.domains.connector.stripe.webhook import (
    StripeWebhookService,
    StripeWebhookVerificationError,
)


class FakePaymentStore:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def record_checkout_session(self, session_data: dict) -> None:
        return None

    async def process_event(self, event: dict) -> bool:
        if any(existing["id"] == event["id"] for existing in self.events):
            return False
        self.events.append(event)
        return True


def webhook_settings() -> Settings:
    return Settings(
        STRIPE_WEBHOOK_SECRET="whsec_test_secret",
        STRIPE_WEBHOOK_TOLERANCE_SEC=300,
    )


def signed_event() -> tuple[bytes, str]:
    event = {
        "id": "evt_123",
        "object": "event",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_test_123",
                "object": "checkout.session",
                "payment_status": "paid",
            }
        },
    }
    payload_text = json.dumps(event, separators=(",", ":"))
    signature = stripe.WebhookSignature.generate_signature_header(
        payload_text,
        webhook_settings().STRIPE_WEBHOOK_SECRET,
        timestamp=int(time.time()),
    )
    return payload_text.encode(), signature


def test_webhook_verifies_signature_and_deduplicates_event():
    payload, signature = signed_event()
    store = FakePaymentStore()
    service = StripeWebhookService(
        settings_factory=webhook_settings,
        payment_store=store,
    )

    first = asyncio.run(service.handle(payload, signature))
    second = asyncio.run(service.handle(payload, signature))

    assert first["status"] == "processed"
    assert second["status"] == "duplicate"
    assert store.events[0]["data"]["object"]["payment_status"] == "paid"


def test_webhook_rejects_invalid_signature():
    payload, _ = signed_event()
    service = StripeWebhookService(
        settings_factory=webhook_settings,
        payment_store=FakePaymentStore(),
    )

    try:
        asyncio.run(service.handle(payload, "t=1,v1=invalid"))
    except StripeWebhookVerificationError as exc:
        assert str(exc) == "invalid Stripe webhook"
    else:
        raise AssertionError("invalid webhook signature was accepted")
