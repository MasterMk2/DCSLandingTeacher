"""Shared-token authentication tests (Issue #8)."""

from __future__ import annotations

import httpx
import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.config import Settings


def make_settings(tmp_path, **overrides) -> Settings:
    db_path = (tmp_path / "auth.db").as_posix()
    return Settings(
        acmi_enabled=False,
        database_url=f"sqlite+aiosqlite:///{db_path}",
        **overrides,
    )


async def open_client(app) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_auth_disabled_allows_anonymous_requests(tmp_path) -> None:
    """Default (DLT_AUTH_TOKEN empty): the API behaves exactly as before."""
    app = create_app(make_settings(tmp_path))
    async with app.router.lifespan_context(app):
        async with await open_client(app) as client:
            health = await client.get("/api/health")
            landings = await client.get("/api/landings")

    assert health.status_code == 200
    assert landings.status_code == 200


async def test_health_stays_public_when_auth_enabled(tmp_path) -> None:
    app = create_app(make_settings(tmp_path, auth_token="secret"))
    async with app.router.lifespan_context(app):
        async with await open_client(app) as client:
            response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_health_reports_database_connectivity(tmp_path) -> None:
    """Issue #45: the health endpoint must surface DB connectivity so probes
    do not pass against a dead/corrupt database."""
    app = create_app(make_settings(tmp_path))
    async with app.router.lifespan_context(app):
        async with await open_client(app) as client:
            response = await client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert "database" in body
    assert body["database"]["connected"] is True
    assert "latency_ms" in body["database"]


async def test_rest_rejects_missing_token_with_401(tmp_path) -> None:
    app = create_app(make_settings(tmp_path, auth_token="secret"))
    async with app.router.lifespan_context(app):
        async with await open_client(app) as client:
            response = await client.get("/api/landings")

    assert response.status_code == 401


async def test_rest_rejects_wrong_token_with_403(tmp_path) -> None:
    app = create_app(make_settings(tmp_path, auth_token="secret"))
    async with app.router.lifespan_context(app):
        async with await open_client(app) as client:
            response = await client.get(
                "/api/landings", headers={"X-Auth-Token": "wrong"}
            )

    assert response.status_code == 403


async def test_rest_accepts_bearer_token(tmp_path) -> None:
    app = create_app(make_settings(tmp_path, auth_token="secret"))
    async with app.router.lifespan_context(app):
        async with await open_client(app) as client:
            response = await client.get(
                "/api/landings", headers={"Authorization": "Bearer secret"}
            )

    assert response.status_code == 200
    assert response.json()["total"] == 0


async def test_rest_accepts_x_auth_token_header(tmp_path) -> None:
    app = create_app(make_settings(tmp_path, auth_token="secret"))
    async with app.router.lifespan_context(app):
        async with await open_client(app) as client:
            response = await client.get(
                "/api/landings", headers={"X-Auth-Token": "secret"}
            )

    assert response.status_code == 200


def test_ws_accepts_valid_query_token(tmp_path) -> None:
    app = create_app(make_settings(tmp_path, auth_token="secret"))
    with TestClient(app) as test_client:
        with test_client.websocket_connect(
            "/api/ws/landings?token=secret"
        ) as websocket:
            websocket.send_text("ping")
            assert websocket.receive_json() == {"type": "pong"}


def test_ws_rejects_missing_or_wrong_token(tmp_path) -> None:
    app = create_app(make_settings(tmp_path, auth_token="secret"))
    with TestClient(app) as test_client:
        with pytest.raises(WebSocketDisconnect):
            with test_client.websocket_connect("/api/ws/landings"):
                pass
        with pytest.raises(WebSocketDisconnect):
            with test_client.websocket_connect("/api/ws/landings?token=wrong"):
                pass


def test_ws_allows_anonymous_when_auth_disabled(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as test_client:
        with test_client.websocket_connect("/api/ws/landings") as websocket:
            websocket.send_text("ping")
            assert websocket.receive_json() == {"type": "pong"}


def test_ws_connection_revoked_when_auth_enabled_after_connect(tmp_path) -> None:
    """Issue #25: enabling auth after a connection was established (while auth
    was off) must not leave the stale connection open."""
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as test_client:
        with test_client.websocket_connect("/api/ws/landings") as websocket:
            websocket.send_text("ping")
            assert websocket.receive_json() == {"type": "pong"}

            # Operator enables authentication at runtime.
            app.state.settings.auth_token = "secret"

            # The next interaction must force re-authentication.
            with pytest.raises(WebSocketDisconnect):
                websocket.send_text("ping")
                websocket.receive_json()


def test_ws_connection_revoked_on_token_rotation(tmp_path) -> None:
    """Issue #25: rotating the server token must invalidate old connections."""
    app = create_app(make_settings(tmp_path, auth_token="old"))
    with TestClient(app) as test_client:
        with test_client.websocket_connect(
            "/api/ws/landings?token=old"
        ) as websocket:
            websocket.send_text("ping")
            assert websocket.receive_json() == {"type": "pong"}

            app.state.settings.auth_token = "new"

            with pytest.raises(WebSocketDisconnect):
                websocket.send_text("ping")
                websocket.receive_json()
