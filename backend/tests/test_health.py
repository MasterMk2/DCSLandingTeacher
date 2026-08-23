"""Tests for the /api/health endpoint and app lifespan."""

from __future__ import annotations

import httpx

from app.api import create_app
from app.config import Settings


def make_settings(tmp_path, **overrides) -> Settings:
    db_path = (tmp_path / "health.db").as_posix()
    return Settings(
        acmi_enabled=False,
        database_url=f"sqlite+aiosqlite:///{db_path}",
        **overrides,
    )


async def test_health_endpoint_reports_ok(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            response = await client.get("/api/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["acmi_enabled"] is False
    assert data["acmi_connected"] is False


async def test_lifespan_creates_database_tables(tmp_path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        # The SQLite file must exist with our schema after startup.
        assert (tmp_path / "health.db").exists()


async def test_settings_defaults_match_requirements() -> None:
    settings = Settings(acmi_enabled=False, _env_file=None)
    assert settings.tacview_port == 31010
    assert settings.reconnect_initial_delay > 0
    assert settings.reconnect_max_delay >= settings.reconnect_initial_delay
