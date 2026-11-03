"""What the public health endpoint tells an anonymous caller (Issue #45)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from app.api.main import create_app
from tests.helpers import make_api_settings, open_api_client

#: The kind of text a driver puts in a connection error: host, port, user.
_DRIVER_DETAIL = (
    'connection to server at "db.sentinel.invalid", port 5432 failed: '
    'FATAL: password authentication failed for user "sentinel-user"'
)


def _unreachable_database():
    raise OperationalError("SELECT 1", {}, ConnectionError(_DRIVER_DETAIL))


async def test_a_dead_database_is_reported_without_the_driver_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """/health needs no token, so the error text stays in the server log."""
    app = create_app(make_api_settings(tmp_path, database_filename="health.db"))
    # create_app replaces the root log handlers (configure_logging); put
    # pytest's capture back to see what the server logged.
    logging.getLogger().addHandler(caplog.handler)
    async with app.router.lifespan_context(app):
        app.state.session_factory = _unreachable_database
        async with open_api_client(app) as http:
            response = await http.get("/api/v1/health")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["database"] == {"connected": False, "error": "database unavailable"}
    assert "sentinel" not in response.text
    assert any(
        record.name == "app.api.routes" and record.levelno == logging.WARNING
        for record in caplog.records
    )
    assert "sentinel-user" in caplog.text
