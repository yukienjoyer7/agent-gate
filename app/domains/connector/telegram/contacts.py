"""Storage access for Telegram identities learned by the inbound channel."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.telegram_contact import TelegramContact
from app.database.session import SessionLocal


def normalize_display_name(value: str | None) -> str | None:
    """Normalize a display-name reference without fuzzy matching it."""
    if not value:
        return None
    normalized = " ".join(value.split())
    return normalized or None


def build_display_name(
    first_name: str | None,
    last_name: str | None,
    *,
    fallback: str | None = None,
) -> str | None:
    return normalize_display_name(" ".join(part for part in (first_name, last_name) if part)) or (
        normalize_display_name(fallback)
    )


@dataclass(frozen=True)
class TelegramContactIdentity:
    chat_id: int
    chat_type: str
    username: str | None
    first_name: str | None
    last_name: str | None
    display_name: str | None
    is_active: bool = True
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None


class TelegramContactStore(Protocol):
    async def upsert(
        self,
        *,
        chat_id: int,
        chat_type: str,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
        display_name: str | None,
    ) -> TelegramContactIdentity: ...

    async def find_by_username(self, username: str) -> Sequence[TelegramContactIdentity]: ...

    async def find_by_display_name(
        self, display_name: str
    ) -> Sequence[TelegramContactIdentity]: ...


@asynccontextmanager
async def _reuse(session: AsyncSession):
    yield session


class TelegramContactRepository:
    """PostgreSQL repository with an idempotent ``chat_id`` upsert."""

    def __init__(self, session: AsyncSession | None = None) -> None:
        self._session = session

    def _scope(self) -> AbstractAsyncContextManager[AsyncSession]:
        return _reuse(self._session) if self._session is not None else SessionLocal()

    async def upsert(
        self,
        *,
        chat_id: int,
        chat_type: str,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
        display_name: str | None,
    ) -> TelegramContactIdentity:
        now = datetime.now(UTC)
        username = _normalize_username(username)
        display_name = normalize_display_name(display_name)
        values = {
            "chat_id": chat_id,
            "chat_type": chat_type,
            "username": username,
            "first_name": _clean_optional(first_name),
            "last_name": _clean_optional(last_name),
            "display_name": display_name,
            "is_active": True,
            "first_seen_at": now,
            "last_seen_at": now,
        }
        statement = insert(TelegramContact).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[TelegramContact.chat_id],
            set_={
                "chat_type": statement.excluded.chat_type,
                "username": statement.excluded.username,
                "first_name": statement.excluded.first_name,
                "last_name": statement.excluded.last_name,
                "display_name": statement.excluded.display_name,
                "is_active": True,
                "last_seen_at": statement.excluded.last_seen_at,
            },
        )
        async with self._scope() as session:
            try:
                await session.execute(statement)
                await session.commit()
            except Exception:
                await session.rollback()
                raise
        return TelegramContactIdentity(
            chat_id=chat_id,
            chat_type=chat_type,
            username=username,
            first_name=_clean_optional(first_name),
            last_name=_clean_optional(last_name),
            display_name=display_name,
            is_active=True,
            first_seen_at=now,
            last_seen_at=now,
        )

    async def find_by_username(self, username: str) -> Sequence[TelegramContactIdentity]:
        normalized = _normalize_username(username)
        if not normalized:
            return []
        async with self._scope() as session:
            result = await session.execute(
                select(TelegramContact)
                .where(
                    TelegramContact.is_active.is_(True),
                    func.lower(TelegramContact.username) == normalized,
                )
                .order_by(TelegramContact.id)
            )
            rows = result.scalars().all()
        return [_identity_from_model(row) for row in rows]

    async def find_by_display_name(self, display_name: str) -> Sequence[TelegramContactIdentity]:
        normalized = normalize_display_name(display_name)
        if not normalized:
            return []
        async with self._scope() as session:
            result = await session.execute(
                select(TelegramContact)
                .where(
                    TelegramContact.is_active.is_(True),
                    func.lower(TelegramContact.display_name) == normalized.lower(),
                )
                .order_by(TelegramContact.id)
            )
            rows = result.scalars().all()
        return [_identity_from_model(row) for row in rows]


def _identity_from_model(contact: TelegramContact) -> TelegramContactIdentity:
    return TelegramContactIdentity(
        chat_id=contact.chat_id,
        chat_type=contact.chat_type,
        username=contact.username,
        first_name=contact.first_name,
        last_name=contact.last_name,
        display_name=contact.display_name,
        is_active=contact.is_active,
        first_seen_at=contact.first_seen_at,
        last_seen_at=contact.last_seen_at,
    )


def _clean_optional(value: str | None) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _normalize_username(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lstrip("@").lower()
    return normalized or None
