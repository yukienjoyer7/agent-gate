"""Storage access for Telegram identities learned by the inbound channel."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.telegram_contact import TelegramContact
from app.database.models.telegram_link_token import TelegramLinkToken


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
    owner_id: str | None = None
    telegram_user_id: int | None = None
    status: str = "observed"
    connected_at: datetime | None = None


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
        telegram_user_id: int | None = None,
    ) -> TelegramContactIdentity: ...

    async def find_by_username(
        self, username: str, *, connected_only: bool = True
    ) -> Sequence[TelegramContactIdentity]: ...

    async def find_by_display_name(
        self, display_name: str, *, connected_only: bool = True
    ) -> Sequence[TelegramContactIdentity]: ...

    async def find_by_chat_id(
        self, chat_id: int, *, connected_only: bool = True
    ) -> Sequence[TelegramContactIdentity]: ...

    async def get_connection(self, owner_id: str) -> TelegramContactIdentity | None: ...

    async def disconnect(self, owner_id: str) -> bool: ...

    async def revoke_link_tokens(self, owner_id: str) -> int: ...

    async def create_link_token(
        self, owner_id: str, *, token_hash: str, expires_at: datetime
    ) -> TelegramLinkToken: ...

    async def consume_link_token(
        self,
        *,
        token_hash: str,
        chat_id: int,
        telegram_user_id: int,
        chat_type: str,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
        display_name: str | None,
        now: datetime,
    ) -> tuple[bool, str | None, TelegramContactIdentity | None]: ...


@asynccontextmanager
async def _reuse(session: AsyncSession):
    yield session


class TelegramContactRepository:
    """PostgreSQL repository with an idempotent ``chat_id`` upsert."""

    def __init__(self, session: AsyncSession | None = None) -> None:
        self._session = session

    def _scope(self) -> AbstractAsyncContextManager[AsyncSession]:
        if self._session is not None:
            return _reuse(self._session)
        from app.database.session import SessionLocal

        return SessionLocal()

    async def upsert(
        self,
        *,
        chat_id: int,
        chat_type: str,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
        display_name: str | None,
        telegram_user_id: int | None = None,
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
            "telegram_user_id": telegram_user_id,
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
                "telegram_user_id": func.coalesce(
                    statement.excluded.telegram_user_id, TelegramContact.telegram_user_id
                ),
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
            telegram_user_id=telegram_user_id,
        )

    async def find_by_username(
        self, username: str, *, connected_only: bool = True
    ) -> Sequence[TelegramContactIdentity]:
        normalized = _normalize_username(username)
        if not normalized:
            return []
        async with self._scope() as session:
            filters = [
                TelegramContact.is_active.is_(True),
                func.lower(TelegramContact.username) == normalized,
            ]
            if connected_only:
                filters.append(TelegramContact.status == "connected")
            result = await session.execute(
                select(TelegramContact).where(*filters).order_by(TelegramContact.id)
            )
            rows = result.scalars().all()
        return [_identity_from_model(row) for row in rows]

    async def get_connection(self, owner_id: str) -> TelegramContactIdentity | None:
        async with self._scope() as session:
            result = await session.execute(
                select(TelegramContact).where(
                    TelegramContact.owner_id == owner_id,
                    TelegramContact.status == "connected",
                    TelegramContact.is_active.is_(True),
                )
            )
            row = result.scalar_one_or_none()
        return _identity_from_model(row) if row else None

    async def find_by_chat_id(
        self, chat_id: int, *, connected_only: bool = True
    ) -> Sequence[TelegramContactIdentity]:
        async with self._scope() as session:
            filters = [TelegramContact.chat_id == chat_id, TelegramContact.is_active.is_(True)]
            if connected_only:
                filters.append(TelegramContact.status == "connected")
            result = await session.execute(select(TelegramContact).where(*filters))
            rows = result.scalars().all()
        return [_identity_from_model(row) for row in rows]

    async def disconnect(self, owner_id: str) -> bool:
        async with self._scope() as session:
            result = await session.execute(
                select(TelegramContact)
                .where(
                    TelegramContact.owner_id == owner_id,
                    TelegramContact.status == "connected",
                )
                .with_for_update()
            )
            row = result.scalar_one_or_none()
            if row is None:
                return False
            row.owner_id = None
            row.status = "disconnected"
            row.connected_at = None
            await session.commit()
            return True

    async def revoke_link_tokens(self, owner_id: str) -> int:
        """Invalidate unused Telegram links issued to an ended browser session."""
        async with self._scope() as session:
            result = await session.execute(
                update(TelegramLinkToken)
                .where(
                    TelegramLinkToken.owner_id == owner_id,
                    TelegramLinkToken.used_at.is_(None),
                )
                .values(used_at=datetime.now(UTC))
            )
            await session.commit()
            return max(result.rowcount or 0, 0)

    async def create_link_token(
        self, owner_id: str, *, token_hash: str, expires_at: datetime
    ) -> TelegramLinkToken:
        async with self._scope() as session:
            row = TelegramLinkToken(
                token_hash=token_hash,
                owner_id=owner_id,
                expires_at=expires_at,
            )
            session.add(row)
            await session.commit()
            return row

    async def consume_link_token(
        self,
        *,
        token_hash: str,
        chat_id: int,
        telegram_user_id: int,
        chat_type: str,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
        display_name: str | None,
        now: datetime,
    ) -> tuple[bool, str | None, TelegramContactIdentity | None]:
        """Consume a token and bind its owner to one private Telegram identity.

        The token and contact rows are locked in one transaction. This makes a
        concurrent replay or competing owner binding fail closed.
        """
        async with self._scope() as session:
            token_result = await session.execute(
                select(TelegramLinkToken)
                .where(TelegramLinkToken.token_hash == token_hash)
                .with_for_update()
            )
            token = token_result.scalar_one_or_none()
            if token is None or token.used_at is not None or token.expires_at <= now:
                await session.rollback()
                return False, None, None

            contact_result = await session.execute(
                select(TelegramContact).where(TelegramContact.chat_id == chat_id).with_for_update()
            )
            contact = contact_result.scalar_one_or_none()
            if (
                contact is not None
                and contact.status == "connected"
                and contact.owner_id != token.owner_id
            ):
                await session.rollback()
                return False, "already_connected", None

            user_result = await session.execute(
                select(TelegramContact)
                .where(
                    TelegramContact.telegram_user_id == telegram_user_id,
                    TelegramContact.status == "connected",
                    TelegramContact.chat_id != chat_id,
                )
                .with_for_update()
            )
            user_contact = user_result.scalar_one_or_none()
            if user_contact is not None and user_contact.owner_id != token.owner_id:
                await session.rollback()
                return False, "already_connected", None

            # A user may replace their prior connection, but only after the
            # prior row is explicitly deactivated in this same transaction.
            old_result = await session.execute(
                select(TelegramContact)
                .where(
                    TelegramContact.owner_id == token.owner_id,
                    TelegramContact.status == "connected",
                    TelegramContact.chat_id != chat_id,
                )
                .with_for_update()
            )
            for old_contact in old_result.scalars():
                old_contact.owner_id = None
                old_contact.status = "disconnected"
                old_contact.connected_at = None

            cleaned_username = _normalize_username(username)
            cleaned_first = _clean_optional(first_name)
            cleaned_last = _clean_optional(last_name)
            cleaned_display = normalize_display_name(display_name)
            if contact is None:
                contact = TelegramContact(
                    chat_id=chat_id,
                    chat_type=chat_type,
                    username=cleaned_username,
                    first_name=cleaned_first,
                    last_name=cleaned_last,
                    display_name=cleaned_display,
                    is_active=True,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                session.add(contact)
            contact.chat_type = chat_type
            contact.username = cleaned_username
            contact.first_name = cleaned_first
            contact.last_name = cleaned_last
            contact.display_name = cleaned_display
            contact.telegram_user_id = telegram_user_id
            contact.owner_id = token.owner_id
            contact.status = "connected"
            contact.connected_at = now
            contact.last_seen_at = now
            token.used_at = now
            await session.commit()
            return True, token.owner_id, _identity_from_model(contact)

    async def find_by_display_name(
        self, display_name: str, *, connected_only: bool = True
    ) -> Sequence[TelegramContactIdentity]:
        normalized = normalize_display_name(display_name)
        if not normalized:
            return []
        async with self._scope() as session:
            filters = [
                TelegramContact.is_active.is_(True),
                func.lower(TelegramContact.display_name) == normalized.lower(),
            ]
            if connected_only:
                filters.append(TelegramContact.status == "connected")
            result = await session.execute(
                select(TelegramContact).where(*filters).order_by(TelegramContact.id)
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
        owner_id=contact.owner_id,
        telegram_user_id=contact.telegram_user_id,
        status=contact.status,
        connected_at=contact.connected_at,
    )


def _clean_optional(value: str | None) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _normalize_username(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lstrip("@").lower()
    return normalized or None
