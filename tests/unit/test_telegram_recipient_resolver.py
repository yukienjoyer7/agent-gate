from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domains.connector.telegram.contacts import TelegramContactIdentity
from app.domains.connector.telegram.recipient_resolver import (
    RecipientResolutionStatus,
    TelegramRecipientResolver,
)


class _MemoryContacts:
    def __init__(self) -> None:
        self.contacts: dict[int, TelegramContactIdentity] = {}

    async def upsert(self, **kwargs) -> TelegramContactIdentity:
        previous = self.contacts.get(kwargs["chat_id"])
        now = datetime.now(UTC)
        identity = TelegramContactIdentity(
            **kwargs,
            is_active=True,
            first_seen_at=previous.first_seen_at if previous else now,
            last_seen_at=now,
        )
        self.contacts[identity.chat_id] = identity
        return identity

    async def find_by_username(self, username: str) -> list[TelegramContactIdentity]:
        normalized = username.strip().lstrip("@").lower()
        return [
            contact
            for contact in self.contacts.values()
            if contact.is_active and (contact.username or "").lower() == normalized
        ]

    async def find_by_display_name(self, display_name: str) -> list[TelegramContactIdentity]:
        normalized = " ".join(display_name.split()).lower()
        return [
            contact
            for contact in self.contacts.values()
            if contact.is_active
            and " ".join((contact.display_name or "").split()).lower() == normalized
        ]


async def _add(
    store: _MemoryContacts,
    chat_id: int,
    *,
    username: str | None = "rafiahmad",
    display_name: str | None = "Rafi Ahmad",
) -> TelegramContactIdentity:
    first, _, last = (display_name or "").partition(" ")
    return await store.upsert(
        chat_id=chat_id,
        chat_type="private",
        username=username,
        first_name=first or None,
        last_name=last or None,
        display_name=display_name,
    )


@pytest.mark.asyncio
async def test_explicit_numeric_chat_id_resolves_without_a_contact_lookup() -> None:
    resolution = await TelegramRecipientResolver(_MemoryContacts()).resolve("123456789")

    assert resolution.status == RecipientResolutionStatus.RESOLVED
    assert resolution.chat_id == 123456789


@pytest.mark.asyncio
async def test_exact_username_and_at_username_resolve() -> None:
    store = _MemoryContacts()
    await _add(store, 123456789)
    resolver = TelegramRecipientResolver(store)

    by_username = await resolver.resolve("rafiahmad")
    by_handle = await resolver.resolve("@rafiahmad")

    assert by_username.status == RecipientResolutionStatus.RESOLVED
    assert by_handle.status == RecipientResolutionStatus.RESOLVED
    assert by_username.chat_id == by_handle.chat_id == 123456789


@pytest.mark.asyncio
async def test_exact_case_insensitive_display_name_resolves() -> None:
    store = _MemoryContacts()
    await _add(store, 123456789)

    resolution = await TelegramRecipientResolver(store).resolve("  rAFi   aHMaD ")

    assert resolution.status == RecipientResolutionStatus.RESOLVED
    assert resolution.chat_id == 123456789
    assert resolution.display_label() == "Rafi Ahmad (@rafiahmad)"


@pytest.mark.asyncio
async def test_unknown_recipient_never_fabricates_a_chat_id() -> None:
    resolution = await TelegramRecipientResolver(_MemoryContacts()).resolve("Rafi Ahmad")

    assert resolution.status == RecipientResolutionStatus.NOT_FOUND
    assert resolution.chat_id is None


@pytest.mark.asyncio
async def test_duplicate_display_names_are_ambiguous_not_first_match() -> None:
    store = _MemoryContacts()
    await _add(store, 1, username="rafi_a")
    await _add(store, 2, username="rafi_b")

    resolution = await TelegramRecipientResolver(store).resolve("Rafi Ahmad")

    assert resolution.status == RecipientResolutionStatus.AMBIGUOUS
    assert resolution.chat_id is None
    assert {match.chat_id for match in resolution.matches} == {1, 2}


@pytest.mark.asyncio
async def test_invalid_at_username_is_rejected_without_guessing() -> None:
    resolution = await TelegramRecipientResolver(_MemoryContacts()).resolve("@Rafi Ahmad")

    assert resolution.status == RecipientResolutionStatus.INVALID
    assert resolution.chat_id is None
