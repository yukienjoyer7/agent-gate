from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.schemas import ExecutionResult, ExecutionStatus
from app.domains.connector.telegram.contacts import TelegramContactIdentity
from app.domains.connector.telegram.service import TelegramService


class _FakeConnector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def get_bot_username(self) -> str:
        return "agentgate_bot"

    async def execute(self, action: str, payload: dict):
        self.calls.append((action, payload))
        return ExecutionResult(
            run_id="link-test",
            action_id="link-test-action",
            executor="telegram",
            status=ExecutionStatus.SUCCESS,
            data={"message_id": 1},
        )


class _LinkStore:
    def __init__(self) -> None:
        self.tokens: dict[str, dict] = {}
        self.connections: dict[str, TelegramContactIdentity] = {}
        self.contacts: dict[int, TelegramContactIdentity] = {}
        self.contact_invites: dict[str, dict] = {}
        self.session_contacts: dict[str, list[TelegramContactIdentity]] = {}

    async def create_link_token(self, owner_id: str, *, token_hash: str, expires_at: datetime):
        self.tokens[token_hash] = {
            "owner_id": owner_id,
            "expires_at": expires_at,
            "used_at": None,
        }

    async def consume_link_token(self, **kwargs):
        token = self.tokens.get(kwargs["token_hash"])
        now = kwargs["now"]
        if token is None or token["used_at"] is not None or token["expires_at"] <= now:
            return False, None, None
        chat_id = kwargs["chat_id"]
        existing = next(
            (
                item
                for item in self.connections.values()
                if item.chat_id == chat_id or item.telegram_user_id == kwargs["telegram_user_id"]
            ),
            None,
        )
        if existing and existing.owner_id != token["owner_id"]:
            return False, "already_connected", None
        contact = TelegramContactIdentity(
            chat_id=chat_id,
            chat_type=kwargs["chat_type"],
            username=kwargs["username"],
            first_name=kwargs["first_name"],
            last_name=kwargs["last_name"],
            display_name=kwargs["display_name"],
            owner_id=token["owner_id"],
            telegram_user_id=kwargs["telegram_user_id"],
            status="connected",
            connected_at=now,
        )
        token["used_at"] = now
        self.connections[token["owner_id"]] = contact
        self.contacts[chat_id] = contact
        return True, token["owner_id"], contact

    async def get_connection(self, owner_id: str):
        return self.connections.get(owner_id)

    async def disconnect(self, owner_id: str) -> bool:
        connection = self.connections.pop(owner_id, None)
        return connection is not None

    async def delete_connection(self, owner_id: str) -> bool:
        return await self.disconnect(owner_id)

    async def create_contact_invite(
        self, session_id: str, alias: str, *, token_hash: str, expires_at: datetime
    ) -> None:
        self.contact_invites[token_hash] = {
            "session_id": session_id,
            "alias": alias,
            "expires_at": expires_at,
            "used_at": None,
        }

    async def consume_contact_invite(self, **kwargs):
        invite = self.contact_invites.get(kwargs["token_hash"])
        if invite is None or invite["used_at"] is not None or invite["expires_at"] <= kwargs["now"]:
            return False, "invalid", None
        owner = self.connections.get(invite["session_id"])
        if owner and owner.telegram_user_id == kwargs["telegram_user_id"]:
            return False, "self_contact", None
        contact = TelegramContactIdentity(
            chat_id=kwargs["chat_id"],
            chat_type="private",
            username=kwargs["username"],
            first_name=kwargs["first_name"],
            last_name=kwargs["last_name"],
            display_name=invite["alias"],
            owner_id=invite["session_id"],
            telegram_user_id=kwargs["telegram_user_id"],
            status="connected",
            alias=invite["alias"],
        )
        invite["used_at"] = kwargs["now"]
        self.session_contacts.setdefault(invite["session_id"], []).append(contact)
        return True, None, contact

    async def list_session_contacts(self, session_id: str):
        return self.session_contacts.get(session_id, [])

    async def delete_session_contact(self, session_id: str, contact_id: str) -> bool:
        return False

    async def delete_session_contact_data(self, session_id: str) -> None:
        self.session_contacts.pop(session_id, None)
        self.contact_invites = {
            key: value
            for key, value in self.contact_invites.items()
            if value["session_id"] != session_id
        }

    async def revoke_link_tokens(self, owner_id: str) -> int:
        count = 0
        for token in self.tokens.values():
            if token["owner_id"] == owner_id and token["used_at"] is None:
                token["used_at"] = datetime.now(UTC)
                count += 1
        return count

    async def upsert(self, **kwargs):
        identity = TelegramContactIdentity(**kwargs)
        self.contacts[identity.chat_id] = identity
        return identity

    async def find_by_username(self, username: str):
        return []

    async def find_by_display_name(self, display_name: str):
        return []


def _service(store: _LinkStore, connector: _FakeConnector | None = None) -> TelegramService:
    return TelegramService(
        connector=connector or _FakeConnector(),
        settings_factory=lambda: SimpleNamespace(
            TELEGRAM_WEBHOOK_SECRET="secret", TELEGRAM_BOT_USERNAME=""
        ),
        contact_repository=store,
        background_tasks=False,
        start_run=lambda prompt, **kwargs: None,
    )


def _start(token: str, *, chat_id: int = 123, user_id: int = 456) -> dict:
    return {
        "update_id": user_id,
        "message": {
            "message_id": 1,
            "from": {"id": user_id, "username": "rafi_a", "first_name": "Rafi"},
            "chat": {"id": chat_id, "type": "private"},
            "text": f"/start {token}",
        },
    }


@pytest.mark.asyncio
async def test_connect_link_is_hashed_and_deep_link_has_expiry() -> None:
    store = _LinkStore()
    service = _service(store)

    url, expires_at = await service.create_connection("owner-a")

    token = url.split("?start=", 1)[1]
    assert url.startswith("https://t.me/agentgate_bot?start=")
    assert expires_at > datetime.now(UTC)
    assert token not in store.tokens
    assert len(store.tokens) == 1


@pytest.mark.asyncio
async def test_valid_start_links_private_identity_once_without_exposing_chat_id() -> None:
    store = _LinkStore()
    connector = _FakeConnector()
    service = _service(store, connector)
    url, _ = await service.create_connection("owner-a")
    token = url.split("?start=", 1)[1]

    result = await service.handle_update(_start(token))
    replay = await service.handle_update(
        _start(token, chat_id=123, user_id=456) | {"update_id": 999}
    )

    assert result["status"] == "accepted"
    assert replay["status"] == "accepted"
    assert store.connections["owner-a"].telegram_user_id == 456
    assert store.connections["owner-a"].status == "connected"
    assert "123" not in connector.calls[-1][1]["text"]
    assert connector.calls[-1][1]["text"] == "Tautan Telegram tidak valid atau sudah kedaluwarsa."


@pytest.mark.asyncio
async def test_plain_start_does_not_consume_link_token() -> None:
    store = _LinkStore()
    service = _service(store)
    url, _ = await service.create_connection("owner-a")
    token = url.split("?start=", 1)[1]
    service._start_run = lambda prompt, **kwargs: SimpleNamespace(run_id="run_plain")

    result = await service.handle_update(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "from": {"id": 456},
                "chat": {"id": 123, "type": "private"},
                "text": "/start",
            },
        }
    )

    assert result["status"] == "accepted"
    assert next(iter(store.tokens.values()))["used_at"] is None
    assert token not in store.connections


@pytest.mark.asyncio
async def test_expired_and_unknown_tokens_fail_without_binding() -> None:
    store = _LinkStore()
    service = _service(store)
    url, _ = await service.create_connection("owner-a")
    token = url.split("?start=", 1)[1]
    row = next(iter(store.tokens.values()))
    row["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)

    await service.handle_update(_start(token))
    await service.handle_update(_start("unknown-token", user_id=999))

    assert store.connections == {}


@pytest.mark.asyncio
async def test_token_owner_is_preserved_and_other_connected_identity_is_rejected() -> None:
    store = _LinkStore()
    service = _service(store)
    first_url, _ = await service.create_connection("owner-a")
    second_url, _ = await service.create_connection("owner-b")
    first = first_url.split("?start=", 1)[1]
    second = second_url.split("?start=", 1)[1]

    await service.handle_update(_start(first, chat_id=123, user_id=456))
    await service.handle_update(_start(second, chat_id=123, user_id=456))

    assert store.connections["owner-a"].owner_id == "owner-a"
    assert "owner-b" not in store.connections


@pytest.mark.asyncio
async def test_telegram_user_cannot_bind_a_second_chat_to_another_owner() -> None:
    store = _LinkStore()
    service = _service(store)
    first_url, _ = await service.create_connection("owner-a")
    second_url, _ = await service.create_connection("owner-b")

    await service.handle_update(_start(first_url.split("?start=", 1)[1], chat_id=123, user_id=456))
    await service.handle_update(_start(second_url.split("?start=", 1)[1], chat_id=124, user_id=456))

    assert store.connections["owner-a"].chat_id == 123
    assert "owner-b" not in store.connections


@pytest.mark.asyncio
async def test_contact_invitation_adds_recipient_without_replacing_owner_connection() -> None:
    store = _LinkStore()
    connector = _FakeConnector()
    service = _service(store, connector)
    owner_url, _ = await service.create_connection("session-a")
    await service.handle_update(_start(owner_url.split("?start=", 1)[1], chat_id=100, user_id=100))

    invite_url, _ = await service.create_contact_invitation("session-a", "Muhammad Arsyad")
    invite_token = invite_url.split("?start=", 1)[1]
    result = await service.handle_update(
        _start(invite_token, chat_id=200, user_id=200) | {"update_id": 200}
    )

    assert result == {"ok": True, "status": "accepted"}
    assert store.connections["session-a"].chat_id == 100
    assert store.session_contacts["session-a"][0].chat_id == 200
    assert store.session_contacts["session-a"][0].alias == "Muhammad Arsyad"
    assert connector.calls[-1][1]["text"].startswith("Kontak Telegram berhasil")


@pytest.mark.asyncio
async def test_ending_session_hard_deletes_contacts_and_pending_invitations() -> None:
    store = _LinkStore()
    service = _service(store)
    owner_url, _ = await service.create_connection("session-a")
    await service.handle_update(_start(owner_url.split("?start=", 1)[1], chat_id=100, user_id=100))
    await service.create_contact_invitation("session-a", "Arsyad")
    store.session_contacts["session-a"] = [store.connections["session-a"]]

    await service.end_browser_owner_session("session-a")

    assert "session-a" not in store.connections
    assert "session-a" not in store.session_contacts
    assert store.contact_invites == {}
