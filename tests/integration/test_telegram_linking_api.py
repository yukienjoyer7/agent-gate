from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.api.session_context import OwnerContext, require_browser_session
from app.api.v1 import telegram as telegram_router
from app.domains.connector.telegram.contacts import TelegramSessionContactIdentity
from app.main import app


class _ApiTelegramService:
    def __init__(self) -> None:
        self.connection = SimpleNamespace(
            username="rafi_a",
            display_name="Rafi Ahmad",
            chat_id=987654321,
        )
        self.contacts = [
            TelegramSessionContactIdentity(
                contact_id="tgc_abc",
                session_id="session-a",
                alias="Arsyad",
                username="arsyad",
                display_name="Muhammad Arsyad",
                created_at=datetime.now(UTC),
            )
        ]

    async def create_connection(self, owner_id: str):
        assert owner_id == "owner-a"
        return "https://t.me/agentgate_bot?start=one-time-payload", datetime.now(UTC) + timedelta(
            minutes=10
        )

    async def connection_status(self, owner_id: str):
        assert owner_id == "owner-a"
        return self.connection

    async def disconnect(self, owner_id: str):
        assert owner_id == "owner-a"
        self.connection = None
        return True

    async def create_contact_invitation(self, owner_id: str, alias: str):
        assert owner_id == "session-a"
        assert alias == "Arsyad"
        return "https://t.me/agentgate_bot?start=contact_secret", datetime.now(UTC) + timedelta(
            minutes=10
        )

    async def list_session_contacts(self, owner_id: str):
        assert owner_id == "session-a"
        return self.contacts

    async def delete_session_contact(self, owner_id: str, contact_id: str):
        assert owner_id == "session-a"
        if contact_id != "tgc_abc":
            return False
        self.contacts = []
        return True


def test_connection_api_returns_deep_link_and_hides_chat_id(monkeypatch) -> None:
    service = _ApiTelegramService()
    monkeypatch.setattr(telegram_router, "telegram_service", service)
    client = TestClient(app)

    connect = client.post("/api/v1/telegram/connect", headers={"X-AgentGate-Owner": "owner-a"})
    assert connect.status_code == 200
    assert connect.json()["connect_url"].startswith("https://t.me/agentgate_bot?start=")
    assert "chat_id" not in connect.text

    status = client.get("/api/v1/telegram/connection", headers={"X-AgentGate-Owner": "owner-a"})
    assert status.status_code == 200
    assert status.json() == {
        "connected": True,
        "username": "rafi_a",
        "display_name": "Rafi Ahmad",
    }
    assert "chat_id" not in status.text


def test_disconnect_api_clears_connection_without_exposing_internal_address(monkeypatch) -> None:
    service = _ApiTelegramService()
    monkeypatch.setattr(telegram_router, "telegram_service", service)
    client = TestClient(app)
    response = client.delete(
        "/api/v1/telegram/connection", headers={"X-AgentGate-Owner": "owner-a"}
    )

    assert response.status_code == 200
    assert response.json() == {"connected": False, "username": None, "display_name": None}
    assert "chat_id" not in response.text


def test_session_contact_api_hides_internal_telegram_addresses(monkeypatch) -> None:
    service = _ApiTelegramService()
    monkeypatch.setattr(telegram_router, "telegram_service", service)
    app.dependency_overrides[require_browser_session] = lambda: OwnerContext(
        owner_id="session-a", session_id="session-a"
    )
    client = TestClient(app)
    try:
        invite = client.post("/api/v1/telegram/contact-invitations", json={"alias": "Arsyad"})
        contacts = client.get("/api/v1/telegram/contacts")
        deleted = client.delete("/api/v1/telegram/contacts/tgc_abc")

        assert invite.status_code == 200
        assert invite.json()["invite_url"].endswith("contact_secret")
        assert contacts.status_code == 200
        assert contacts.json()[0]["alias"] == "Arsyad"
        assert "chat_id" not in contacts.text
        assert "telegram_user_id" not in contacts.text
        assert deleted.json() == {"deleted": True}
    finally:
        app.dependency_overrides.pop(require_browser_session, None)
