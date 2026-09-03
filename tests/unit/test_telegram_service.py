from types import SimpleNamespace

import pytest

from app.core.run_schema import StepStatus
from app.core.schemas import ExecutionResult, ExecutionStatus
from app.domains.agent.services.run_registry import RunRegistry, RunSession, StepState
from app.domains.connector.telegram.service import (
    TelegramService,
    TelegramWebhookAuthError,
    TelegramWebhookMisconfiguredError,
)


class _FakeConnector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, action: str, payload: dict) -> ExecutionResult:
        self.calls.append((action, payload))
        return ExecutionResult(
            run_id=payload.get("run_id", "run_fake"),
            action_id=payload.get("action_id", "act_fake"),
            executor="telegram",
            status=ExecutionStatus.SUCCESS,
            data={"message_id": 99},
        )


def _settings(secret: str = "secret-token"):
    return SimpleNamespace(TELEGRAM_WEBHOOK_SECRET=secret)


def _message_update(update_id: int = 100, text: str = "Read sample.txt") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 10,
            "from": {"id": 7},
            "chat": {"id": 123, "type": "private"},
            "text": text,
        },
    }


def _callback_update(run_id: str, step_index: int, decision: str = "approve") -> dict:
    return {
        "update_id": 200,
        "callback_query": {
            "id": "cb_1",
            "from": {"id": 7},
            "message": {"message_id": 55, "chat": {"id": 123}},
            "data": f"ag:{decision}:{run_id}:{step_index}",
        },
    }


def _waiting_run(registry: RunRegistry) -> RunSession:
    run = registry.create(
        "send message",
        channel="telegram",
        channel_id="123",
        metadata={"user_id": 7},
    )
    run.steps.append(
        StepState(
            index=0,
            data={
                "action_type": "API_CALL",
                "target_system": "telegram",
                "target": "123",
                "risk_hint": "external_send",
                "payload": {"action": "send_message", "chat_id": "123", "text": "hello"},
            },
            action_id="act_1",
            status=StepStatus.WAITING_APPROVAL,
            decision={"reasons": ["risk_hint=external_send"]},
        )
    )
    return run


def test_validate_webhook_secret_rejects_missing_and_invalid() -> None:
    service = TelegramService(settings_factory=lambda: _settings())

    with pytest.raises(TelegramWebhookAuthError):
        service.validate_webhook_secret(None)
    with pytest.raises(TelegramWebhookAuthError):
        service.validate_webhook_secret("wrong")


def test_validate_webhook_secret_accepts_correct_secret() -> None:
    service = TelegramService(settings_factory=lambda: _settings())

    service.validate_webhook_secret("secret-token")


def test_empty_configured_webhook_secret_fails_closed() -> None:
    service = TelegramService(settings_factory=lambda: _settings(""))

    with pytest.raises(TelegramWebhookMisconfiguredError):
        service.validate_webhook_secret("anything")


@pytest.mark.asyncio
async def test_text_message_creates_one_agent_run_with_channel_metadata() -> None:
    created: list[dict] = []

    def fake_start_run(prompt: str, *, channel: str, channel_id: str, metadata: dict) -> RunSession:
        created.append(
            {
                "prompt": prompt,
                "channel": channel,
                "channel_id": channel_id,
                "metadata": metadata,
            }
        )
        return RunSession(prompt, channel=channel, channel_id=channel_id, metadata=metadata)

    service = TelegramService(
        connector=_FakeConnector(),
        settings_factory=lambda: _settings(),
        start_run=fake_start_run,
        background_tasks=False,
    )

    result = await service.handle_update(_message_update())

    assert result["status"] == "accepted"
    assert len(created) == 1
    assert created[0]["prompt"] == "Read sample.txt"
    assert created[0]["channel"] == "telegram"
    assert created[0]["channel_id"] == "123"
    assert created[0]["metadata"]["update_id"] == 100
    assert created[0]["metadata"]["message_id"] == 10
    assert created[0]["metadata"]["user_id"] == 7


@pytest.mark.asyncio
async def test_non_text_update_is_safely_ignored() -> None:
    called = False

    def fake_start_run(*args, **kwargs) -> RunSession:
        nonlocal called
        called = True
        return RunSession("unused")

    service = TelegramService(
        connector=_FakeConnector(),
        settings_factory=lambda: _settings(),
        start_run=fake_start_run,
        background_tasks=False,
    )

    result = await service.handle_update({"update_id": 101, "message": {"chat": {"id": 123}}})

    assert result == {"ok": True, "status": "ignored"}
    assert called is False


@pytest.mark.asyncio
async def test_malformed_update_does_not_crash() -> None:
    service = TelegramService(
        connector=_FakeConnector(),
        settings_factory=lambda: _settings(),
        background_tasks=False,
    )

    assert await service.handle_update([]) == {"ok": True, "status": "ignored"}
    assert await service.handle_update({"update_id": 102}) == {"ok": True, "status": "ignored"}


@pytest.mark.asyncio
async def test_duplicate_update_id_does_not_create_duplicate_run() -> None:
    created = 0

    def fake_start_run(prompt: str, **kwargs) -> RunSession:
        nonlocal created
        created += 1
        return RunSession(prompt, **kwargs)

    service = TelegramService(
        connector=_FakeConnector(),
        settings_factory=lambda: _settings(),
        start_run=fake_start_run,
        background_tasks=False,
    )

    first = await service.handle_update(_message_update(update_id=777))
    second = await service.handle_update(_message_update(update_id=777))

    assert first["status"] == "accepted"
    assert second["status"] == "duplicate"
    assert created == 1


@pytest.mark.asyncio
async def test_approve_callback_resumes_waiting_step_and_is_acknowledged() -> None:
    registry = RunRegistry()
    run = _waiting_run(registry)
    connector = _FakeConnector()
    service = TelegramService(
        connector=connector,
        registry=registry,
        settings_factory=lambda: _settings(),
        background_tasks=False,
    )

    result = await service.handle_update(_callback_update(run.run_id, 0, "approve"))

    assert result["status"] == "callback_accepted"
    assert run.pending_responses[0]["action"] == "approve"
    assert [call[0] for call in connector.calls] == [
        "answer_callback_query",
        "edit_message_reply_markup",
    ]


@pytest.mark.asyncio
async def test_decline_callback_resumes_waiting_step() -> None:
    registry = RunRegistry()
    run = _waiting_run(registry)
    connector = _FakeConnector()
    service = TelegramService(
        connector=connector,
        registry=registry,
        settings_factory=lambda: _settings(),
        background_tasks=False,
    )

    result = await service.handle_update(_callback_update(run.run_id, 0, "decline"))

    assert result["status"] == "callback_accepted"
    assert run.pending_responses[0]["action"] == "decline"


@pytest.mark.asyncio
async def test_invalid_run_id_callback_rejected_gracefully() -> None:
    connector = _FakeConnector()
    service = TelegramService(
        connector=connector,
        registry=RunRegistry(),
        settings_factory=lambda: _settings(),
        background_tasks=False,
    )

    result = await service.handle_update(_callback_update("run_missing", 0))

    assert result["status"] == "callback_rejected"
    assert connector.calls[0][0] == "answer_callback_query"


@pytest.mark.asyncio
async def test_invalid_step_index_callback_rejected_gracefully() -> None:
    registry = RunRegistry()
    run = _waiting_run(registry)
    service = TelegramService(
        connector=_FakeConnector(),
        registry=registry,
        settings_factory=lambda: _settings(),
        background_tasks=False,
    )

    result = await service.handle_update(_callback_update(run.run_id, 99))

    assert result["status"] == "callback_rejected"


@pytest.mark.asyncio
async def test_repeated_callback_does_not_respond_twice() -> None:
    registry = RunRegistry()
    run = _waiting_run(registry)
    service = TelegramService(
        connector=_FakeConnector(),
        registry=registry,
        settings_factory=lambda: _settings(),
        background_tasks=False,
    )

    first = await service.handle_update(_callback_update(run.run_id, 0))
    second_update = _callback_update(run.run_id, 0)
    second_update["update_id"] = 201
    second_update["callback_query"]["id"] = "cb_2"
    second = await service.handle_update(second_update)

    assert first["status"] == "callback_accepted"
    assert second["status"] == "callback_duplicate"
    assert run.pending_responses[0]["action"] == "approve"
