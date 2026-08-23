"""Tests for the production SPA static hosting (frontend/dist)."""

from __future__ import annotations

from pathlib import Path

import httpx

from app.api import create_app
from app.config import Settings


def make_settings(tmp_path: Path, dist_dir: Path) -> Settings:
    db_path = (tmp_path / "static.db").as_posix()
    return Settings(
        acmi_enabled=False,
        database_url=f"sqlite+aiosqlite:///{db_path}",
        frontend_dist_dir=dist_dir.as_posix(),
    )


def _write_dist(root: Path) -> None:
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(
        "<!doctype html><html><body>spa</body></html>", encoding="utf-8"
    )
    (root / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")


async def test_spa_root_served_when_dist_exists(tmp_path) -> None:
    dist = tmp_path / "dist"
    _write_dist(dist)

    app = create_app(make_settings(tmp_path, dist))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            root = await client.get("/")
            asset = await client.get("/assets/app.js")
            fallback = await client.get("/some/client/route")
            missing_api = await client.get("/api/does-not-exist")

    assert root.status_code == 200
    assert "text/html" in root.headers["content-type"]
    assert asset.status_code == 200
    assert fallback.status_code == 200
    assert b"spa" in fallback.content
    # Unknown API paths must not fall back to index.html.
    assert missing_api.status_code == 404
    assert missing_api.headers["content-type"].startswith("application/json")


async def test_api_only_mode_when_dist_missing(tmp_path) -> None:
    app = create_app(make_settings(tmp_path, tmp_path / "nope"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            response = await client.get("/api/health")

    assert response.status_code == 200
