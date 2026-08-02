from __future__ import annotations

from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.oauth_token import OAuthToken
from app.database.session import SessionLocal


@dataclass
class StoredToken:
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scope: str | None


@asynccontextmanager
async def _reuse(session: AsyncSession):
    yield session


class OAuthTokenRepository:
    """Persists one token row per provider. session=None opens/closes its
    own AsyncSession per call, same ergonomics as AuditRepositoryDB."""

    def __init__(self, session: AsyncSession | None = None) -> None:
        self._session = session

    def _scope(self) -> AbstractAsyncContextManager[AsyncSession]:
        return _reuse(self._session) if self._session is not None else SessionLocal()

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
