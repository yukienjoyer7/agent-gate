from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config.settings import get_settings
from app.database.url import (
    DatabaseConnectionMetadata,
    DatabaseSSLMode,
    asyncpg_connect_args,
    database_connection_metadata,
    normalize_asyncpg_url,
)

POOL_RECYCLE_SECONDS = 300


def create_database_engine(
    database_url: str,
    *,
    pool_size: int,
    max_overflow: int,
    ssl_mode: DatabaseSSLMode = "auto",
) -> AsyncEngine:
    """Create the long-lived application engine from a parsed database URL."""

    normalized_url = normalize_asyncpg_url(database_url)
    engine_options: dict = {
        "echo": False,
        "pool_size": pool_size,
        "max_overflow": max_overflow,
        "pool_pre_ping": True,
        "pool_recycle": POOL_RECYCLE_SECONDS,
    }
    connect_args = asyncpg_connect_args(database_url, ssl_mode)
    if connect_args:
        engine_options["connect_args"] = connect_args
    return create_async_engine(normalized_url, **engine_options)


settings = get_settings()
engine = create_database_engine(
    settings.DATABASE_URL,
    pool_size=settings.DATABASE_POOL_SIZE,
    max_overflow=settings.DATABASE_MAX_OVERFLOW,
    ssl_mode=settings.DATABASE_SSL_MODE,
)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def current_database_connection_metadata() -> DatabaseConnectionMetadata:
    """Expose safe connection metadata for developer diagnostics."""

    return database_connection_metadata(settings.DATABASE_URL)


async def check_database_connection() -> DatabaseConnectionMetadata:
    """Run a minimal, non-mutating database connectivity query."""

    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    return current_database_connection_metadata()


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an async database session."""

    async with SessionLocal() as session:
        yield session
