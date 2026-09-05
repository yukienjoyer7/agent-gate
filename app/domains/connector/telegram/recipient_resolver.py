"""Deterministic resolution from a Telegram recipient reference to a chat ID."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy.exc import SQLAlchemyError

from app.domains.connector.telegram.contacts import (
    TelegramContactIdentity,
    TelegramContactRepository,
    TelegramContactStore,
    normalize_display_name,
)

_NUMERIC_CHAT_ID = re.compile(r"^-?[0-9]+$")


class RecipientResolutionStatus(StrEnum):
    RESOLVED = "RESOLVED"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    INVALID = "INVALID"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class RecipientResolution:
    status: RecipientResolutionStatus
    reference: str
    contact: TelegramContactIdentity | None = None
    matches: tuple[TelegramContactIdentity, ...] = field(default_factory=tuple)

    @property
    def chat_id(self) -> int | None:
        return self.contact.chat_id if self.contact else None

    def display_label(self) -> str:
        if self.contact is None:
            return self.reference
        name = (
            self.contact.display_name
            or self.contact.username
            or f"Telegram chat {self.contact.chat_id}"
        )
        return f"{name} (@{self.contact.username})" if self.contact.username else name

    def audit_identity(self) -> dict[str, object] | None:
        if self.contact is None:
            return None
        return {
            "chat_id": self.contact.chat_id,
            "chat_type": self.contact.chat_type,
            "display_name": self.contact.display_name,
            "username": self.contact.username,
        }

    def public_identity(self) -> dict[str, str | None] | None:
        if self.contact is None:
            return None
        return {"display_name": self.contact.display_name, "username": self.contact.username}


class TelegramRecipientResolver:
    """Resolve exact, already-known Telegram identities without fuzzy guesses."""

    def __init__(self, contacts: TelegramContactStore | None = None) -> None:
        self._contacts = contacts or TelegramContactRepository()

    async def resolve(self, reference: object) -> RecipientResolution:
        if isinstance(reference, bool) or reference is None:
            return RecipientResolution(RecipientResolutionStatus.INVALID, str(reference or ""))
        raw = str(reference).strip()
        if not raw:
            return RecipientResolution(RecipientResolutionStatus.INVALID, raw)

        chat_id = parse_numeric_chat_id(raw)
        if chat_id is not None:
            # An explicitly supplied numeric Telegram ID is a valid connector
            # address even when the bot has not yet learned a display profile.
            return RecipientResolution(
                RecipientResolutionStatus.RESOLVED,
                raw,
                contact=TelegramContactIdentity(
                    chat_id=chat_id,
                    chat_type="unknown",
                    username=None,
                    first_name=None,
                    last_name=None,
                    display_name=None,
                ),
            )

        if raw.startswith("@"):
            username = raw[1:].strip()
            if not is_valid_username(username):
                return RecipientResolution(RecipientResolutionStatus.INVALID, raw)
            try:
                matches = await self._contacts.find_by_username(username)
            except SQLAlchemyError:
                return RecipientResolution(RecipientResolutionStatus.UNAVAILABLE, raw)
            return self._matches_result(raw, matches)

        # Follow the documented precedence: exact username first, then exact
        # normalized display name. Neither branch ever chooses a first match.
        try:
            username_matches = await self._contacts.find_by_username(raw)
        except SQLAlchemyError:
            return RecipientResolution(RecipientResolutionStatus.UNAVAILABLE, raw)
        if username_matches:
            return self._matches_result(raw, username_matches)

        display_name = normalize_display_name(raw)
        if not display_name:
            return RecipientResolution(RecipientResolutionStatus.INVALID, raw)
        try:
            display_matches = await self._contacts.find_by_display_name(display_name)
        except SQLAlchemyError:
            return RecipientResolution(RecipientResolutionStatus.UNAVAILABLE, raw)
        return self._matches_result(raw, display_matches)

    @staticmethod
    def _matches_result(
        reference: str, matches: Sequence[TelegramContactIdentity]
    ) -> RecipientResolution:
        items = tuple(matches)
        if len(items) == 1:
            return RecipientResolution(
                RecipientResolutionStatus.RESOLVED, reference, contact=items[0]
            )
        if len(items) > 1:
            return RecipientResolution(
                RecipientResolutionStatus.AMBIGUOUS, reference, matches=items
            )
        return RecipientResolution(RecipientResolutionStatus.NOT_FOUND, reference)


def parse_numeric_chat_id(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        candidate = value
    elif isinstance(value, str) and _NUMERIC_CHAT_ID.fullmatch(value.strip()):
        candidate = int(value.strip())
    else:
        return None
    # PostgreSQL BigInteger / Telegram's signed integer address space. Zero is
    # not a usable Telegram chat identifier.
    if candidate == 0 or not -(2**63) <= candidate < 2**63:
        return None
    return candidate


def is_valid_username(value: str) -> bool:
    # Bot API usernames are letters, digits, and underscores.  Do not accept a
    # display name following an @ marker as though it were a Telegram handle.
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", value))
