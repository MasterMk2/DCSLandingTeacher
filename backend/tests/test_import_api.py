"""Tests for the ACMI file import API (upload -> pipeline -> DB)."""

from __future__ import annotations

import io
import zipfile

import httpx
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.config import Settings
from tests.helpers import make_acmi_text, make_approach_samples


def _settings(tmp_path, **overrides) -> Settings:
    db_path = (tmp_path / "import.db").as_posix()
    return Settings(
        acmi_enabled=False,
        database_url=f"sqlite+aiosqlite:///{db_path}",
        **overrides,
    )


def _sample_acmi() -> str:
    return make_acmi_text(make_approach_samples(outcome="full_stop"))


async def _wait_for_job(
    http: httpx.AsyncClient, job_id: str, timeout_s: float = 10.0
) -> dict:
    """Poll the job endpoint until it reaches a terminal state."""
    import asyncio

    deadline = asyncio.get_event_loop().time() + timeout_s
    while True:
        response = await http.get(f"/api/imports/{job_id}")
        assert response.status_code == 200
        body = response.json()
        if body["status"] in ("completed", "failed"):
            return body
        assert asyncio.get_event_loop().time() < deadline, "import did not finish"
        await asyncio.sleep(0.01)


async def _open_client(app) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_import_acmi_end_to_end(tmp_path) -> None:
    """Upload -> ingest -> detect -> grade -> DB, then visible in /api/landings."""
    app = create_app(_settings(tmp_path))
    async with app.router.lifespan_context(app):
        async with await _open_client(app) as http:
            response = await http.post(
                "/api/import",
                files={"file": ("session.acmi", _sample_acmi().encode(), "text/plain")},
            )
            assert response.status_code == 202
            start = response.json()
            assert start["status"] in ("pending", "processing", "completed")

            job = await _wait_for_job(http, start["id"])
            assert job["status"] == "completed"
            assert job["error"] is None
            assert job["frames_processed"] > 0
            assert job["landings_detected"] == 1
            assert job["duplicates_skipped"] == 0

            landings = await http.get("/api/landings")
            body = landings.json()
            assert body["total"] == 1
            landing = body["items"][0]
            assert landing["kind"] == "carrier"
            assert landing["grade"] in ("OK", "OK-", "(OK)", "_NO_GRADE_", "CUT")


async def test_import_zip_archive(tmp_path) -> None:
    """A .acmi.zip container is unpacked and processed like plain text."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("session.acmi", _sample_acmi())
    app = create_app(_settings(tmp_path))
    async with app.router.lifespan_context(app):
        async with await _open_client(app) as http:
            response = await http.post(
                "/api/import",
                files={
                    "file": (
                        "session.acmi.zip",
                        buffer.getvalue(),
                        "application/zip",
                    )
                },
            )
            assert response.status_code == 202

            job = await _wait_for_job(http, response.json()["id"])
            assert job["status"] == "completed"
            assert job["landings_detected"] == 1


async def test_import_compressed_plain_acmi(tmp_path) -> None:
    """Tacview stores zip data inside plain .acmi files; magic sniffing handles it."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("data.acmi", _sample_acmi())
    app = create_app(_settings(tmp_path))
    async with app.router.lifespan_context(app):
        async with await _open_client(app) as http:
            response = await http.post(
                "/api/import",
                files={"file": ("tacview.acmi", buffer.getvalue(), "application/octet-stream")},
            )
            assert response.status_code == 202

            job = await _wait_for_job(http, response.json()["id"])
            assert job["status"] == "completed"
            assert job["landings_detected"] == 1


async def test_duplicate_import_is_skipped(tmp_path) -> None:
    """Re-uploading the same recording creates no second landing rows."""
    app = create_app(_settings(tmp_path))
    async with app.router.lifespan_context(app):
        async with await _open_client(app) as http:
            content = _sample_acmi().encode()
            first = await http.post(
                "/api/import", files={"file": ("a.acmi", content, "text/plain")}
            )
            job1 = await _wait_for_job(http, first.json()["id"])
            assert job1["landings_detected"] == 1

            second = await http.post(
                "/api/import", files={"file": ("a.acmi", content, "text/plain")}
            )
            job2 = await _wait_for_job(http, second.json()["id"])
            assert job2["status"] == "completed"
            assert job2["landings_detected"] == 0
            assert job2["duplicates_skipped"] == 1

            landings = (await http.get("/api/landings")).json()
            assert landings["total"] == 1


async def test_import_rejects_unsupported_extension(tmp_path) -> None:
    app = create_app(_settings(tmp_path))
    async with app.router.lifespan_context(app):
        async with await _open_client(app) as http:
            response = await http.post(
                "/api/import",
                files={"file": ("notes.txt.bak", b"hello", "application/octet-stream")},
            )
    assert response.status_code == 400


async def test_import_rejects_oversized_upload(tmp_path) -> None:
    app = create_app(_settings(tmp_path, import_max_upload_mb=0))
    async with app.router.lifespan_context(app):
        async with await _open_client(app) as http:
            response = await http.post(
                "/api/import",
                files={"file": ("big.acmi", b"x" * 1024, "text/plain")},
            )
    assert response.status_code == 413


async def test_list_and_get_import_jobs(tmp_path) -> None:
    app = create_app(_settings(tmp_path))
    async with app.router.lifespan_context(app):
        async with await _open_client(app) as http:
            started = await http.post(
                "/api/import",
                files={"file": ("s.acmi", _sample_acmi().encode(), "text/plain")},
            )
            job_id = started.json()["id"]

            listing = await http.get("/api/imports")
            assert listing.status_code == 200
            items = listing.json()["items"]
            assert [j["id"] for j in items] == [job_id]
            assert items[0]["filename"] == "s.acmi"

            missing = await http.get("/api/imports/does-not-exist")
            assert missing.status_code == 404


async def test_import_requires_authentication(tmp_path) -> None:
    app = create_app(_settings(tmp_path, auth_token="secret"))
    async with app.router.lifespan_context(app):
        async with await _open_client(app) as http:
            denied = await http.post(
                "/api/import",
                files={"file": ("a.acmi", _sample_acmi().encode(), "text/plain")},
            )
            assert denied.status_code == 401

            wrong = await http.post(
                "/api/import",
                files={"file": ("a.acmi", _sample_acmi().encode(), "text/plain")},
                headers={"X-Auth-Token": "nope"},
            )
            assert wrong.status_code == 403

            allowed = await http.post(
                "/api/import",
                files={"file": ("a.acmi", _sample_acmi().encode(), "text/plain")},
                headers={"X-Auth-Token": "secret"},
            )
            assert allowed.status_code == 202

            listing = await http.get(
                "/api/imports", headers={"X-Auth-Token": "secret"}
            )
            assert listing.status_code == 200


def test_ws_notified_of_import_completion(tmp_path) -> None:
    """The completion broadcast rides the existing WebSocket channel."""
    app = create_app(_settings(tmp_path))
    with TestClient(app) as test_client:
        with test_client.websocket_connect("/api/ws/landings") as websocket:
            # Consume the ping/pong handshake capability check first.
            websocket.send_text("ping")
            assert websocket.receive_json() == {"type": "pong"}

            upload = test_client.post(
                "/api/import",
                files={"file": ("ws.acmi", _sample_acmi().encode(), "text/plain")},
            )
            assert upload.status_code == 202

            messages = []
            # Expected frames: landing (provisional), landing_update (final
            # outcome), and the import completion summary.
            for _ in range(3):
                try:
                    messages.append(websocket.receive_json())
                except Exception:
                    break
            types = {m.get("type") for m in messages}
            assert "landing" in types
            assert "import" in types
            import_message = next(m for m in messages if m.get("type") == "import")
            job_payload = import_message["import"]
            assert job_payload["status"] == "completed"
            assert job_payload["landings_detected"] == 1
