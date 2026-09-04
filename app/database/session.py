from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config.settings import get_settings

settings = get_settings()

# asyncpg does not accept libpq's ``sslmode``/``channel_binding`` query
# arguments as driver kwargs. Keep the normal DATABASE_URL format usable for
# migrations and other tooling, but translate the Neon connection URL for the
# SQLAlchemy asyncpg engine.
database_url = settings.DATABASE_URL
if database_url.startswith("postgresql+asyncpg://"):
    database_url = database_url.replace("sslmode=require", "ssl=require")
    database_url = database_url.replace("&channel_binding=require", "")

engine = create_async_engine(database_url, echo=False, future=True)

SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an async database session."""
    async with SessionLocal() as session:
        yield session
