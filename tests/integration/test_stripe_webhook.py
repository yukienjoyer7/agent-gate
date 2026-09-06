from fastapi.testclient import TestClient

from app.api.v1 import stripe as stripe_api
from app.main import app


class FakeWebhookService:
    def __init__(self) -> None:
        self.payload: bytes | None = None
        self.signature: str | None = None

    async def handle(self, payload: bytes, signature: str | None) -> dict:
        self.payload = payload
        self.signature = signature
        return {
            "ok": True,
            "status": "processed",
            "event_id": "evt_123",
            "event_type": "checkout.session.completed",
        }


def test_stripe_webhook_route_passes_raw_body_and_signature(monkeypatch):
    service = FakeWebhookService()
    monkeypatch.setattr(stripe_api, "stripe_webhook_service", service)
    body = b'{"id":"evt_123", "untouched" : true}'

    response = TestClient(app).post(
        "/api/v1/stripe/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Stripe-Signature": "t=123,v1=abc",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "processed"
    assert service.payload == body
    assert service.signature == "t=123,v1=abc"
