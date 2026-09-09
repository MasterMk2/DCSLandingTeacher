"""Shared pytest fixtures."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import httpx2
import pytest
from fastapi.applications import FastAPI
from sqlalchemy.ext.asyncio.session import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models.database import create_engine, create_session_factory
from tests.helpers import create_async_test_schema

REPO_ROOT = Path(__file__).resolve().parents[2]
GRADING_YAML = REPO_ROOT / "config" / "grading.yaml"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    test_settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'api.db').as_posix()}",
        acmi_enabled=False,
        grading_config_path=str(GRADING_YAML),
    )
    engine = create_engine(test_settings.database_url)
    asyncio.run(create_async_test_schema(engine))
    asyncio.run(engine.dispose())
    return test_settings


@pytest.fixture
async def session_factory(tmp_path: Path) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    db_path = (tmp_path / "test.db").as_posix()
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")

    await create_async_test_schema(engine)

    yield create_session_factory(engine)

    await engine.dispose()


@pytest.fixture
async def client(settings: Settings) -> AsyncGenerator[tuple[httpx2.AsyncClient, FastAPI], None]:
    """HTTPX async client bound to a live app instance."""
    from app.api.main import create_app

    app = create_app(settings)

    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http, app
