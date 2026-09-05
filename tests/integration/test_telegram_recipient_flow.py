"""End-to-end guarded Telegram recipient flow with a mocked Bot API."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from app.core.action_request import build_action_request
from app.core.run_schema import RunStatus
from app.core.schemas import AuditEvent, ExecutionStatus
from app.domains.agent.services import agent_loop
from app.domains.agent.services.run_registry import RunRegistry, RunSession
from app.domains.connector.telegram.contacts import TelegramContactIdentity
from app.domains.connector.telegram.recipient_resolver import TelegramRecipientResolver
from app.domains.connector.telegram.service import TelegramService
from app.domains.connector.telegram.telegram import TelegramConnector
from app.executors.api_executor import APIExecutor


class _MemoryContacts:
    def __init__(self) -> None:
        self.contacts: dict[int, TelegramContactIdentity] = {}

    async def upsert(self, **kwargs) -> TelegramContactIdentity:
        previous = self.contacts.get(kwargs["chat_id"])
        now = datetime.now(UTC)
        contact = TelegramContactIdentity(
            **kwargs,
            is_active=True,
            first_seen_at=previous.first_seen_at if previous else now,
            last_seen_at=now,
        )
        self.contacts[contact.chat_id] = contact
        return contact

    async def find_by_username(self, username: str) -> list[TelegramContactIdentity]:
        return [
            contact
            for contact in self.contacts.values()
            if (contact.username or "").lower() == username.strip().lstrip("@").lower()
        ]

    async def find_by_display_name(self, display_name: str) -> list[TelegramContactIdentity]:
        normalized = " ".join(display_name.split()).lower()
        return [
            contact
            for contact in self.contacts.values()
            if " ".join((contact.display_name or "").split()).lower() == normalized
        ]


async def _wait_for(predicate, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise TimeoutError("condition not met")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_webhook_registration_then_guarded_resolved_send(monkeypatch) -> None:
    contacts = _MemoryContacts()
    registry = RunRegistry()
    channel_service = TelegramService(
        connector=TelegramConnector(token="test-token"),
        registry=registry,
        settings_factory=lambda: type("Settings", (), {"TELEGRAM_WEBHOOK_SECRET": "test-secret"})(),
        start_run=lambda prompt, **kwargs: RunSession(prompt, **kwargs),
        background_tasks=False,
        contact_repository=contacts,
    )
    inbound_update = {
        "update_id": 700,
        "message": {
            "message_id": 10,
            "chat": {
                "id": 123456789,
                "type": "private",
                "username": "rafiahmad",
                "first_name": "Rafi",
                "last_name": "Ahmad",
            },
            "text": "/start",
        },
    }
    await channel_service.handle_update(inbound_update)
    assert contacts.contacts[123456789].display_name == "Rafi Ahmad"

    sent: list[dict] = []

    def telegram_api(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content.decode()))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 77, "chat": {"id": 123456789}}},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(telegram_api), base_url="https://api.telegram.test"
    ) as client:
        connector = TelegramConnector(client, token="test-token")
        executor = APIExecutor()
        executor.connectors["telegram"] = connector
        audit_events: list[AuditEvent] = []

        async def fake_plan(prompt: str) -> dict:
            return {
                "plan": [
                    {
                        "action_type": "API_CALL",
                        "target_system": "telegram",
                        "target": "Rafi Ahmad",
                        "domain": "productivity",
                        "risk_hint": "external_send",
                        "payload": {
                            "action": "send_message",
                            "recipient": "Rafi Ahmad",
                            "text": "halo",
                        },
                    }
                ],
                "llm_provider": "test",
                "raw_prompt": prompt,
            }

        async def fake_guarded(proposal, audit=None, traces=None, decision=None):
            request = build_action_request(proposal)
            execution = await executor.execute(request)
            event = AuditEvent(
                run_id=request.run_id,
                action_id=request.action_id,
                request_json=request.model_dump(mode="json", exclude={"payload"}),
                decision_json=decision.model_dump(mode="json"),
                execution_json=execution.model_dump(mode="json"),
                execution_status=execution.status,
            )
            audit_events.append(event)
            return event

        async def fake_replan(prompt: str, context: dict) -> list[dict]:
            return []

        monkeypatch.setattr(agent_loop, "parse_prompt_plan", fake_plan)
        monkeypatch.setattr(
            agent_loop,
            "TelegramRecipientResolver",
            lambda: TelegramRecipientResolver(contacts),
        )
        monkeypatch.setattr(agent_loop, "run_guarded_action", fake_guarded)
        monkeypatch.setattr(agent_loop, "parse_next_steps", fake_replan)

        run = registry.create("kirim pesan telegram 'halo' ke Rafi Ahmad")
        task = asyncio.create_task(agent_loop.run_agent_loop(run))
        await _wait_for(lambda: run.status == RunStatus.WAITING_APPROVAL)

        assert sent == []
        assert run.steps[0].data["payload"]["chat_id"] == 123456789
        assert run.steps[0].data["resolved_recipient"]["username"] == "rafiahmad"

        registry.respond(run, 0, "approve")
        await asyncio.wait_for(task, timeout=5)

    assert run.status == RunStatus.DONE
    assert sent == [{"chat_id": 123456789, "text": "halo"}]
    assert audit_events[0].execution_status == ExecutionStatus.SUCCESS
    assert audit_events[0].request_json["user_goal"] == "kirim pesan telegram 'halo' ke Rafi Ahmad"
    assert audit_events[0].request_json["recipient_reference"] == "Rafi Ahmad"
    assert audit_events[0].request_json["resolved_recipient"]["chat_id"] == 123456789
    assert audit_events[0].decision_json["initial_decision"] == "NEED_APPROVAL"
    assert audit_events[0].decision_json["approval_decision"] == "approved"
    assert audit_events[0].execution_json["data"]["message_id"] == 77
