from __future__ import annotations

from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.oauth_state import OAuthState
from app.database.models.oauth_token import OAuthToken


@dataclass
class StoredToken:
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scope: str | None


class OAuthStateRepository:
    """Persist hashed OAuth state and consume it atomically exactly once."""

    def __init__(self, session: AsyncSession | None = None) -> None:
        self._session = session

    def _scope(self) -> AbstractAsyncContextManager[AsyncSession]:
        if self._session is not None:
            return _reuse(self._session)
        from app.database.session import SessionLocal

        return SessionLocal()

    async def create(self, state_hash: str, provider: str, expires_at: datetime) -> None:
        now = datetime.now(UTC)
        async with self._scope() as session:
            # Remove expired rows and consumed rows after a short diagnostic
            # window so abandoned authorize attempts cannot grow indefinitely.
            await session.execute(
                delete(OAuthState).where(
                    or_(
                        OAuthState.expires_at <= now,
                        OAuthState.used_at < now - timedelta(days=1),
                    )
                )
            )
            session.add(
                OAuthState(
                    state_hash=state_hash,
                    provider=provider,
                    expires_at=expires_at,
                )
            )
            await session.commit()

    async def consume(self, state_hash: str, provider: str, now: datetime) -> bool:
        async with self._scope() as session:
            row = await session.scalar(
                select(OAuthState).where(OAuthState.state_hash == state_hash).with_for_update()
            )
            if (
                row is None
                or row.provider != provider
                or row.used_at is not None
                or row.expires_at <= now
            ):
                await session.rollback()
                return False
            row.used_at = now
            await session.commit()
            return True


@asynccontextmanager
async def _reuse(session: AsyncSession):
    yield session


class OAuthTokenRepository:
    """Persists one token row per provider. session=None opens/closes its
    own AsyncSession per call, same ergonomics as AuditRepositoryDB."""

    def __init__(self, session: AsyncSession | None = None) -> None:
        self._session = session

    def _scope(self) -> AbstractAsyncContextManager[AsyncSession]:
        if self._session is not None:
            return _reuse(self._session)
        from app.database.session import SessionLocal

        return SessionLocal()

    async def get(self, provider: str) -> StoredToken | None:
        async with self._scope() as session:
            row = await session.get(OAuthToken, provider)
            if row is None:
                return None
            return StoredToken(row.access_token, row.refresh_token, row.expires_at, row.scope)

    async def save(
        self,
        provider: str,
        access_token: str,
        refresh_token: str | None,
        expires_at: datetime | None,
        scope: str | None,
    ) -> StoredToken:
        async with self._scope() as session:
            row = await session.get(OAuthToken, provider)
            if row is None:
                row = OAuthToken(provider=provider)
                session.add(row)
            row.access_token = access_token
            if refresh_token:
                row.refresh_token = refresh_token
            row.expires_at = expires_at
            row.scope = scope
            await session.commit()
            return StoredToken(row.access_token, row.refresh_token, row.expires_at, row.scope)
