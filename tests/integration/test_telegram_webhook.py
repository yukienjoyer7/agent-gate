from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.api.v1 import telegram as telegram_router
from app.domains.agent.services.run_registry import RunSession
from app.domains.connector.telegram.service import TelegramService


class _MemoryContacts:
    def __init__(self) -> None:
        self.contacts: dict[int, dict] = {}

    async def upsert(self, **kwargs):
        self.contacts[kwargs["chat_id"]] = kwargs
        return kwargs

    async def find_by_username(self, username):
        return []

    async def find_by_display_name(self, display_name):
        return []


from app.main import app


def _settings(secret: str):
    return SimpleNamespace(TELEGRAM_WEBHOOK_SECRET=secret)


def _install_service(monkeypatch, secret: str = "secret-token"):
    created: list[RunSession] = []

    def fake_start_run(prompt: str, **kwargs) -> RunSession:
        run = RunSession(prompt, **kwargs)
        created.append(run)
        return run

    service = TelegramService(
        settings_factory=lambda: _settings(secret),
        start_run=fake_start_run,
        background_tasks=False,
        contact_repository=_MemoryContacts(),
    )
    monkeypatch.setattr(telegram_router, "telegram_service", service)
    return created


def _update(text: str = "Read sample.txt") -> dict:
    return {
        "update_id": 300,
        "message": {
            "message_id": 5,
            "from": {"id": 7},
            "chat": {"id": 123, "type": "private"},
            "text": text,
        },
    }


def test_webhook_rejects_missing_secret_header(monkeypatch) -> None:
    _install_service(monkeypatch)
    response = TestClient(app).post("/api/v1/telegram/webhook", json=_update())

    assert response.status_code == 403


def test_webhook_rejects_invalid_secret_header(monkeypatch) -> None:
    _install_service(monkeypatch)
    response = TestClient(app).post(
        "/api/v1/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        json=_update(),
    )

    assert response.status_code == 403


def test_webhook_accepts_correct_secret_and_creates_run(monkeypatch) -> None:
    created = _install_service(monkeypatch)
    response = TestClient(app).post(
        "/api/v1/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret-token"},
        json=_update(),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert len(created) == 1
    assert created[0].prompt == "Read sample.txt"
    assert created[0].channel == "telegram"
    assert created[0].channel_id == "123"


def test_webhook_registers_private_contact_before_starting_run(monkeypatch) -> None:
    _install_service(monkeypatch)
    update = _update(text="/start")
    update["message"]["chat"].update(
        {"username": "rafiahmad", "first_name": "Rafi", "last_name": "Ahmad"}
    )

    response = TestClient(app).post(
        "/api/v1/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret-token"},
        json=update,
    )

    contacts = telegram_router.telegram_service._contacts
    assert response.status_code == 200
    assert contacts.contacts[123]["display_name"] == "Rafi Ahmad"
    assert contacts.contacts[123]["username"] == "rafiahmad"


def test_webhook_empty_configured_secret_fails_safely(monkeypatch) -> None:
    _install_service(monkeypatch, secret="")
    response = TestClient(app).post(
        "/api/v1/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "anything"},
        json=_update(),
    )

    assert response.status_code == 503


def test_webhook_malformed_json_does_not_crash(monkeypatch) -> None:
    _install_service(monkeypatch)
    response = TestClient(app).post(
        "/api/v1/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret-token"},
        content="not json",
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "status": "ignored"}
