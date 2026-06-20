from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config.settings import get_settings

settings = get_settings()

# psycopg3 (postgresql+psycopg) supports async natively under SQLAlchemy 2.0.
engine = create_async_engine(settings.DATABASE_URL, echo=False, future=True)

SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an async database session."""
    async with SessionLocal() as session:
        yield session
