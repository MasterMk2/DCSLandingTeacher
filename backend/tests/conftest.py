"""Shared pytest fixtures."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import create_engine as create_sync_engine, text
from sqlalchemy.engine import make_url
from uuid import uuid4
from fastapi.applications import FastAPI
from sqlalchemy.ext.asyncio.session import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models.database import create_engine, create_session_factory
from tests.helpers import create_async_test_schema

REPO_ROOT = Path(__file__).resolve().parents[2]
GRADING_YAML = REPO_ROOT / "config" / "grading.yaml"


def pytest_asyncio_loop_factories():
    """Use the selector loop supported by psycopg on Windows."""
    return {"selector": asyncio.SelectorEventLoop}


@pytest.fixture
def database_url(tmp_path: Path) -> Iterator[str]:
    """Isolate every PostgreSQL test in a disposable schema."""
    test_url = os.getenv("DLT_TEST_POSTGRES_URL")
    if not test_url:
        yield f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
        return
    schema = f"dlt_backend_{uuid4().hex}"
    admin = create_sync_engine(test_url)
    with admin.begin() as connection:
        _ = connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped_url = make_url(test_url).update_query_dict({"options": f"-csearch_path={schema}"})
    try:
        yield scoped_url.render_as_string(hide_password=False)
    finally:
        with admin.begin() as connection:
            _ = connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def settings(database_url: str) -> Settings:
    test_settings = Settings(
        database_url=database_url,
        acmi_enabled=False,
        grading_config_path=str(GRADING_YAML),
    )
    engine = create_engine(test_settings.database_url)
    asyncio.run(create_async_test_schema(engine))
    asyncio.run(engine.dispose())
    return test_settings


@pytest.fixture
async def session_factory(database_url: str) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_engine(database_url)

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
