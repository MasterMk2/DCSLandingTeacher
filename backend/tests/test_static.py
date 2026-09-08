"""Tests that the backend does not host frontend routes."""

from __future__ import annotations

from pathlib import Path

import httpx2

from app.api import create_app
from app.config import Settings
from tests.helpers import create_test_schema


def make_settings(tmp_path: Path) -> Settings:
    db_path = (tmp_path / "static.db").as_posix()
    settings = Settings(
        acmi_enabled=False,
        database_url=f"sqlite+aiosqlite:///{db_path}",
    )
    create_test_schema(settings.database_url)
    return settings


async def test_backend_does_not_serve_frontend_routes(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path))
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            root = await client.get("/")
            asset = await client.get("/assets/app.js")
            fallback = await client.get("/some/client/route")

    # The frontend is hosted by its own service.
    assert root.status_code == 404
    assert asset.status_code == 404
    assert fallback.status_code == 404


async def test_api_only_mode_when_dist_missing(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path))
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            response = await client.get("/api/health")

    assert response.status_code == 200
