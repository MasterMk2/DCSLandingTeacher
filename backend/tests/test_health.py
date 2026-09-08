"""Tests for the /api/health endpoint and app lifespan."""

from __future__ import annotations

import httpx2

from app.api import create_app
from app.config import Settings
from app.models import entities  # noqa: F401
from app.models.base import Base
from app.models.database import create_engine


def make_settings(tmp_path, **overrides) -> Settings:
    db_path = (tmp_path / "health.db").as_posix()
    return Settings(
        acmi_enabled=False,
        database_url=f"sqlite+aiosqlite:///{db_path}",
        **overrides,
    )


async def test_health_endpoint_reports_ok(tmp_path) -> None:
    settings = make_settings(tmp_path)
    schema_engine = create_engine(settings.database_url)
    async with schema_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await schema_engine.dispose()

    app = create_app(settings)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            response = await client.get("/api/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["acmi_enabled"] is False
    assert data["acmi_connected"] is False
