"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.models.database import create_engine, create_session_factory, init_db

REPO_ROOT = Path(__file__).resolve().parents[2]
GRADING_YAML = REPO_ROOT / "config" / "grading.yaml"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'api.db').as_posix()}",
        acmi_enabled=False,
        grading_config_path=str(GRADING_YAML),
    )


@pytest.fixture
async def session_factory(tmp_path):
    db_path = (tmp_path / "test.db").as_posix()
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
async def client(settings):
    """HTTPX async client bound to a live app instance."""
    from app.api.main import create_app

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as http:
            yield http, app
