from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.api.v1 import telegram as telegram_router
from app.main import app


class _ApiTelegramService:
    def __init__(self) -> None:
        self.connection = SimpleNamespace(
            username="rafi_a",
            display_name="Rafi Ahmad",
            chat_id=987654321,
        )

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
