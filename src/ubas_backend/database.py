"""Database engines and typed session factories; SQL stays in SQLAlchemy."""

from sqlalchemy import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

type SessionFactory = async_sessionmaker[AsyncSession]


def postgres_url(value: str) -> URL:
    url = make_url(value)
    if url.drivername not in {"postgres", "postgresql", "postgresql+asyncpg"}:
        raise ValueError("UBAS_DATABASE_URL must point to PostgreSQL")
    return url.set(drivername="postgresql+asyncpg")


def create_query_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(
        postgres_url(database_url),
        isolation_level="READ COMMITTED",
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=0,
        pool_timeout=15,
        connect_args={"timeout": 15, "command_timeout": 30},
    )


def create_sessions(engine: AsyncEngine) -> SessionFactory:
    return async_sessionmaker(engine, expire_on_commit=False)
