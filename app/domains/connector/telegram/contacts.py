"""Storage access for Telegram identities learned by the inbound channel."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.schemas import new_id
from app.database.models.browser_session import BrowserSession
from app.database.models.telegram_contact import TelegramContact
from app.database.models.telegram_link_token import TelegramLinkToken
from app.database.models.telegram_session_contact import (
    TelegramContactInvite,
    TelegramSessionContact,
)


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
    alias: str | None = None


@dataclass(frozen=True)
class TelegramSessionContactIdentity:
    contact_id: str
    session_id: str
    alias: str
    username: str | None
    display_name: str | None
    created_at: datetime | None = None


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
        self,
        username: str,
        *,
        connected_only: bool = True,
        owner_id: str | None = None,
    ) -> Sequence[TelegramContactIdentity]: ...

    async def find_by_display_name(
        self,
        display_name: str,
        *,
        connected_only: bool = True,
        owner_id: str | None = None,
    ) -> Sequence[TelegramContactIdentity]: ...

    async def find_by_chat_id(
        self,
        chat_id: int,
        *,
        connected_only: bool = True,
        owner_id: str | None = None,
    ) -> Sequence[TelegramContactIdentity]: ...

    async def get_connection(self, owner_id: str) -> TelegramContactIdentity | None: ...

    async def disconnect(self, owner_id: str) -> bool: ...

    async def delete_connection(self, owner_id: str) -> bool: ...

    async def revoke_link_tokens(self, owner_id: str) -> int: ...

    async def create_contact_invite(
        self, session_id: str, alias: str, *, token_hash: str, expires_at: datetime
    ) -> None: ...

    async def consume_contact_invite(
        self, **kwargs
    ) -> tuple[bool, str | None, TelegramContactIdentity | None]: ...

    async def list_session_contacts(
        self, session_id: str
    ) -> Sequence[TelegramSessionContactIdentity]: ...

    async def delete_session_contact(self, session_id: str, contact_id: str) -> bool: ...

    async def delete_session_contact_data(self, session_id: str) -> None: ...

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
        self,
        username: str,
        *,
        connected_only: bool = True,
        owner_id: str | None = None,
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
            if owner_id is not None:
                filters.append(TelegramContact.owner_id == owner_id)
            result = await session.execute(
                select(TelegramContact).where(*filters).order_by(TelegramContact.id)
            )
            rows = result.scalars().all()
            session_rows = []
            if owner_id is not None:
                session_result = await session.execute(
                    select(TelegramSessionContact).where(
                        TelegramSessionContact.session_id == owner_id,
                        func.lower(TelegramSessionContact.username) == normalized,
                    )
                )
                session_rows = session_result.scalars().all()
        return _dedupe_identities(
            [
                *(_identity_from_model(row) for row in rows),
                *(_identity_from_session_contact(row) for row in session_rows),
            ]
        )

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
        self,
        chat_id: int,
        *,
        connected_only: bool = True,
        owner_id: str | None = None,
    ) -> Sequence[TelegramContactIdentity]:
        async with self._scope() as session:
            filters = [TelegramContact.chat_id == chat_id, TelegramContact.is_active.is_(True)]
            if connected_only:
                filters.append(TelegramContact.status == "connected")
            if owner_id is not None:
                filters.append(TelegramContact.owner_id == owner_id)
            result = await session.execute(select(TelegramContact).where(*filters))
            rows = result.scalars().all()
            session_rows = []
            if owner_id is not None:
                session_result = await session.execute(
                    select(TelegramSessionContact).where(
                        TelegramSessionContact.session_id == owner_id,
                        TelegramSessionContact.chat_id == chat_id,
                    )
                )
                session_rows = session_result.scalars().all()
        return _dedupe_identities(
            [
                *(_identity_from_model(row) for row in rows),
                *(_identity_from_session_contact(row) for row in session_rows),
            ]
        )

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

    async def delete_connection(self, owner_id: str) -> bool:
        """Hard-delete the Telegram identity attached to an ending demo session."""
        async with self._scope() as session:
            result = await session.execute(
                delete(TelegramContact).where(TelegramContact.owner_id == owner_id)
            )
            await session.commit()
            return bool(result.rowcount)

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

    async def create_contact_invite(
        self, session_id: str, alias: str, *, token_hash: str, expires_at: datetime
    ) -> None:
        async with self._scope() as session:
            session.add(
                TelegramContactInvite(
                    token_hash=token_hash,
                    session_id=session_id,
                    alias=normalize_display_name(alias) or alias,
                    expires_at=expires_at,
                )
            )
            await session.commit()

    async def consume_contact_invite(
        self,
        *,
        token_hash: str,
        chat_id: int,
        telegram_user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
        display_name: str | None,
        now: datetime,
    ) -> tuple[bool, str | None, TelegramContactIdentity | None]:
        """Consume one invitation and add its Telegram identity to that active session."""
        async with self._scope() as session:
            invite = await session.scalar(
                select(TelegramContactInvite)
                .where(TelegramContactInvite.token_hash == token_hash)
                .with_for_update()
            )
            if invite is None or invite.used_at is not None or invite.expires_at <= now:
                await session.rollback()
                return False, "invalid", None
            browser_session = await session.scalar(
                select(BrowserSession)
                .where(BrowserSession.session_id == invite.session_id)
                .with_for_update()
            )
            if browser_session is None or browser_session.ended_at is not None:
                await session.rollback()
                return False, "session_ended", None
            own_connection = await session.scalar(
                select(TelegramContact).where(
                    TelegramContact.owner_id == invite.session_id,
                    TelegramContact.status == "connected",
                )
            )
            if own_connection is not None and (
                own_connection.chat_id == chat_id
                or own_connection.telegram_user_id == telegram_user_id
            ):
                await session.rollback()
                return False, "self_contact", None

            row = await session.scalar(
                select(TelegramSessionContact)
                .where(
                    TelegramSessionContact.session_id == invite.session_id,
                    TelegramSessionContact.chat_id == chat_id,
                )
                .with_for_update()
            )
            cleaned_username = _normalize_username(username)
            cleaned_first = _clean_optional(first_name)
            cleaned_last = _clean_optional(last_name)
            cleaned_display = normalize_display_name(display_name)
            if row is None:
                row = TelegramSessionContact(
                    contact_id=new_id("tgc"),
                    session_id=invite.session_id,
                    chat_id=chat_id,
                    telegram_user_id=telegram_user_id,
                    alias=invite.alias,
                )
                session.add(row)
            row.telegram_user_id = telegram_user_id
            row.username = cleaned_username
            row.first_name = cleaned_first
            row.last_name = cleaned_last
            row.display_name = cleaned_display
            row.alias = invite.alias
            row.updated_at = now
            invite.used_at = now
            await session.commit()
            return True, None, _identity_from_session_contact(row)

    async def list_session_contacts(
        self, session_id: str
    ) -> Sequence[TelegramSessionContactIdentity]:
        async with self._scope() as session:
            rows = (
                (
                    await session.execute(
                        select(TelegramSessionContact)
                        .where(TelegramSessionContact.session_id == session_id)
                        .order_by(
                            func.lower(TelegramSessionContact.alias),
                            TelegramSessionContact.contact_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        return [
            TelegramSessionContactIdentity(
                contact_id=row.contact_id,
                session_id=row.session_id,
                alias=row.alias,
                username=row.username,
                display_name=row.display_name,
                created_at=row.created_at,
            )
            for row in rows
        ]

    async def delete_session_contact(self, session_id: str, contact_id: str) -> bool:
        async with self._scope() as session:
            result = await session.execute(
                delete(TelegramSessionContact).where(
                    TelegramSessionContact.session_id == session_id,
                    TelegramSessionContact.contact_id == contact_id,
                )
            )
            await session.commit()
            return bool(result.rowcount)

    async def delete_session_contact_data(self, session_id: str) -> None:
        """Hard-delete temporary recipients and invitations when a session ends."""
        async with self._scope() as session:
            await session.execute(
                delete(TelegramContactInvite).where(TelegramContactInvite.session_id == session_id)
            )
            await session.execute(
                delete(TelegramSessionContact).where(
                    TelegramSessionContact.session_id == session_id
                )
            )
            await session.commit()

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
        self,
        display_name: str,
        *,
        connected_only: bool = True,
        owner_id: str | None = None,
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
            if owner_id is not None:
                filters.append(TelegramContact.owner_id == owner_id)
            result = await session.execute(
                select(TelegramContact).where(*filters).order_by(TelegramContact.id)
            )
            rows = result.scalars().all()
            session_rows = []
            if owner_id is not None:
                session_result = await session.execute(
                    select(TelegramSessionContact).where(
                        TelegramSessionContact.session_id == owner_id,
                        or_(
                            func.lower(TelegramSessionContact.alias) == normalized.lower(),
                            func.lower(TelegramSessionContact.display_name) == normalized.lower(),
                        ),
                    )
                )
                session_rows = session_result.scalars().all()
        return _dedupe_identities(
            [
                *(_identity_from_model(row) for row in rows),
                *(_identity_from_session_contact(row) for row in session_rows),
            ]
        )


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


def _identity_from_session_contact(contact: TelegramSessionContact) -> TelegramContactIdentity:
    return TelegramContactIdentity(
        chat_id=contact.chat_id,
        chat_type="private",
        username=contact.username,
        first_name=contact.first_name,
        last_name=contact.last_name,
        display_name=contact.alias or contact.display_name,
        is_active=True,
        first_seen_at=contact.created_at,
        last_seen_at=contact.updated_at,
        owner_id=contact.session_id,
        telegram_user_id=contact.telegram_user_id,
        status="connected",
        connected_at=contact.created_at,
        alias=contact.alias,
    )


def _dedupe_identities(
    identities: Sequence[TelegramContactIdentity],
) -> list[TelegramContactIdentity]:
    by_chat_id: dict[int, TelegramContactIdentity] = {}
    for identity in identities:
        by_chat_id[identity.chat_id] = identity
    return list(by_chat_id.values())


def _clean_optional(value: str | None) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _normalize_username(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lstrip("@").lower()
    return normalized or None
