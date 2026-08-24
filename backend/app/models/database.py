"""SQLAlchemy async engine/session helpers."""

from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

SQLITE_BUSY_TIMEOUT_MS = 5000


def create_engine(database_url: str) -> AsyncEngine:
    engine = create_async_engine(database_url, echo=False)
    if database_url.startswith("sqlite"):
        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            # WAL lets readers (API requests) proceed without blocking on the
            # writer (live ingest / import), and vice versa; only concurrent
            # *writers* still serialize. Persisted in the DB file, so this is
            # a no-op after the first connection, but harmless to repeat.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


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
