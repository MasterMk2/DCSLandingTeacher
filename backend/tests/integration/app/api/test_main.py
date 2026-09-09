"""Integration tests for app construction and lifecycle in :mod:`app.api.main`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx2
from sqlalchemy import select

from app.api import create_app
from app.api.main import API_V1, API_VERSION, _settle_stale_provisionals
from app.config import Settings
from app.models import entities  # noqa: F401
from app.models.base import Base
from app.models.database import create_engine
from app.models.entities import DcsObject, Flight, Landing
from tests.helpers import create_test_schema


def make_static_settings(tmp_path: Path) -> Settings:
    db_path = (tmp_path / "static.db").as_posix()
    settings = Settings(acmi_enabled=False, database_url=f"sqlite+aiosqlite:///{db_path}")
    create_test_schema(settings.database_url)
    return settings


async def test_backend_does_not_serve_frontend_routes(tmp_path: Path) -> None:
    app = create_app(make_static_settings(tmp_path))
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            root = await client.get("/")
            asset = await client.get("/assets/app.js")
            fallback = await client.get("/some/client/route")

    assert root.status_code == 404
    assert asset.status_code == 404
    assert fallback.status_code == 404


async def test_api_only_mode_when_dist_missing(tmp_path: Path) -> None:
    app = create_app(make_static_settings(tmp_path))
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            response = await client.get("/api/health")

    assert response.status_code == 200


def make_health_settings(tmp_path, **overrides) -> Settings:
    db_path = (tmp_path / "health.db").as_posix()
    return Settings(acmi_enabled=False, database_url=f"sqlite+aiosqlite:///{db_path}", **overrides)


async def test_health_endpoint_reports_ok(tmp_path) -> None:
    settings = make_health_settings(tmp_path)
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


async def test_version_endpoint_under_v1(client):
    http, _ = client
    response = await http.get("/api/v1/version")
    assert response.status_code == 200
    assert response.json() == {"api": API_V1, "version": API_VERSION}


async def test_version_endpoint_under_legacy_alias(client):
    http, _ = client
    response = await http.get("/api/version")
    assert response.status_code == 200
    assert response.json()["api"] == API_V1


def _collect_paths(routes) -> set[str]:
    """Collect every route path including nested router wrappers."""
    paths: set[str] = set()
    for route in routes:
        path = getattr(route, "path", "")
        if path:
            paths.add(path)
        nested = getattr(route, "routes", None)
        if nested:
            paths |= _collect_paths(nested)
        original = getattr(route, "original_router", None)
        if original is not None:
            paths |= _collect_paths(original.routes)
    return paths


async def test_routes_are_registered_under_v1_and_legacy_alias(client):
    _, app = client
    paths = set(app.openapi()["paths"].keys())
    assert "/api/v1/landings" in paths
    assert "/api/landings" in paths
    assert "/api/v1/version" in paths
    assert "/ws/landings" in _collect_paths(app.router.routes)


async def test_settle_stale_provisionals_finalizes_old_ones(session_factory) -> None:
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        flight = Flight(source_id="default")
        session.add(flight)
        await session.flush()
        old = DcsObject(flight_id=flight.id, acmi_id="A1", first_seen=0.0, last_seen=1.0)
        recent = DcsObject(flight_id=flight.id, acmi_id="A2", first_seen=0.0, last_seen=1.0)
        session.add_all([old, recent])
        await session.flush()
        session.add_all(
            [
                Landing(
                    flight_id=flight.id,
                    object_id=old.id,
                    outcome_status="provisional",
                    created_at=now - timedelta(seconds=400),
                ),
                Landing(
                    flight_id=flight.id,
                    object_id=recent.id,
                    outcome_status="provisional",
                    created_at=now - timedelta(seconds=10),
                ),
            ]
        )
        await session.commit()

    assert await _settle_stale_provisionals(session_factory, now - timedelta(seconds=300)) == 1

    async with session_factory() as session:
        rows = (await session.execute(select(Landing).order_by(Landing.object_id))).scalars().all()
    assert [row.outcome_status for row in rows] == ["final", "provisional"]
