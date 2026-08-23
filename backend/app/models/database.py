"""SQLAlchemy async engine/session helpers."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, echo=False)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """Create all tables.

    Initial version uses ``create_all``; migrations can be introduced later
    once the schema stabilizes.
    """
    # Import for table registration side effects.
    from app.models import entities  # noqa: F401

    from app.models.base import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
