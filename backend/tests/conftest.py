"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from app.models.database import create_engine, create_session_factory, init_db


@pytest.fixture
async def session_factory(tmp_path):
    db_path = (tmp_path / "test.db").as_posix()
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()
