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
    """Upload -> ingest -> detect -> grade -> DB, scoped to the import."""
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

            # An upload is usually from an unrelated server, so it must not
            # join the shared history that the default listing shows.
            shared = (await http.get("/api/landings")).json()
            assert shared["total"] == 0
            assert all(
                not s["id"].startswith("import:") for s in (shared["sources"] or [])
            )

            # It is reachable through its own source, which is what the import
            # result view uses.
            source_id = f"import:{start['id']}"
            scoped = (await http.get(f"/api/landings?source={source_id}")).json()
            assert scoped["total"] == 1
            landing = scoped["items"][0]
            assert landing["kind"] == "carrier"
            assert landing["source_id"] == source_id
            assert landing["grade"] in ("OK", "OK-", "(OK)", "_NO_GRADE_", "CUT")


async def test_discarding_an_import_removes_everything_it_created(tmp_path) -> None:
    """Nothing of a discarded upload may survive.

    The foreign keys declare ON DELETE CASCADE, but SQLite ignores those
    unless PRAGMA foreign_keys is on (it is not) and no ORM cascade is
    configured either, so deleting only the flights would leave every track
    and landing of the import orphaned in the database.
    """
    from sqlalchemy import func, select

    from app.models.entities import DcsObject, Flight, Landing, Track

    app = create_app(_settings(tmp_path))
    async with app.router.lifespan_context(app):
        async with await _open_client(app) as http:
            start = (
                await http.post(
                    "/api/import",
                    files={"file": ("s.acmi", _sample_acmi().encode(), "text/plain")},
                )
            ).json()
            await _wait_for_job(http, start["id"])

            session_factory = app.state.session_factory
            async def counts() -> dict[str, int]:
                async with session_factory() as session:
                    out = {}
                    for name, model in (
                        ("flights", Flight), ("objects", DcsObject),
                        ("tracks", Track), ("landings", Landing),
                    ):
                        out[name] = (
                            await session.execute(select(func.count()).select_from(model))
                        ).scalar_one()
                    return out

            before = await counts()
            assert all(v > 0 for v in before.values()), before

            deleted = await http.delete(f"/api/imports/{start['id']}")
            assert deleted.status_code == 204

            after = await counts()
            assert after == {"flights": 0, "objects": 0, "tracks": 0, "landings": 0}
            assert (await http.get("/api/imports")).json()["items"] == []


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

            # Both uploads share a ReferenceTime, so the second is skipped;
            # each import is scoped to its own source, so count there.
            first_scope = (
                await http.get(f"/api/landings?source=import:{first.json()['id']}")
            ).json()
            second_scope = (
                await http.get(f"/api/landings?source=import:{second.json()['id']}")
            ).json()
            assert first_scope["total"] == 1
            assert second_scope["total"] == 0


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


async def test_import_rejects_oversized_upload_via_content_length(tmp_path) -> None:
    """Issue #29: an upload whose (real) Content-Length exceeds the limit is
    rejected with 413 before the body is streamed to disk, not after."""
    app = create_app(_settings(tmp_path, import_max_upload_mb=1))
    async with app.router.lifespan_context(app):
        async with await _open_client(app) as http:
            response = await http.post(
                "/api/import",
                files={
                    "file": (
                        "big.acmi",
                        b"x" * (2 * 1024 * 1024),
                        "text/plain",
                    )
                },
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


async def test_two_sessions_of_the_same_mission_are_both_imported(tmp_path) -> None:
    """A recording of a DIFFERENT sortie must not be swallowed as a duplicate.

    The duplicate key was (slot id, mission-elapsed time, ReferenceTime), and
    ReferenceTime is the .miz's in-game date -- the same for every session of
    a mission. A server flying one mission all week therefore had every
    uploaded recording collide with what it had already recorded live, and
    the import reported "0 landings detected". RecordingTime is stamped per
    recording, so it is what tells the sessions apart.
    """
    app = create_app(_settings(tmp_path))
    async with app.router.lifespan_context(app):
        async with await _open_client(app) as http:
            monday = make_acmi_text(
                make_approach_samples(outcome="full_stop"),
                recording_time="2026-08-25T07:03:07Z",
            ).encode()
            tuesday = make_acmi_text(
                make_approach_samples(outcome="full_stop"),
                recording_time="2026-08-26T07:03:07Z",
            ).encode()

            first = await http.post(
                "/api/import", files={"file": ("mon.acmi", monday, "text/plain")}
            )
            job1 = await _wait_for_job(http, first.json()["id"])
            assert job1["landings_detected"] == 1

            second = await http.post(
                "/api/import", files={"file": ("tue.acmi", tuesday, "text/plain")}
            )
            job2 = await _wait_for_job(http, second.json()["id"])
            assert job2["landings_detected"] == 1, job2
            assert job2["duplicates_skipped"] == 0

            # The same recording twice is still a duplicate.
            again = await http.post(
                "/api/import", files={"file": ("tue.acmi", tuesday, "text/plain")}
            )
            job3 = await _wait_for_job(http, again.json()["id"])
            assert job3["landings_detected"] == 0
            assert job3["duplicates_skipped"] == 1


async def test_import_job_survives_restart(tmp_path) -> None:
    """Issue #28: import job metadata must persist across server restarts,
    not live only in the manager's in-memory dict (which is lost on restart,
    leaving completed jobs un-queryable as 404)."""
    import asyncio

    db_path = (tmp_path / "persist.db").as_posix()
    settings1 = Settings(
        acmi_enabled=False, database_url=f"sqlite+aiosqlite:///{db_path}"
    )
    app1 = create_app(settings1)
    async with app1.router.lifespan_context(app1):
        async with await _open_client(app1) as http:
            response = await http.post(
                "/api/import",
                files={"file": ("session.acmi", _sample_acmi().encode(), "text/plain")},
            )
            assert response.status_code == 202
            job_id = response.json()["id"]
            await _wait_for_job(http, job_id)
            # Let the run() finally block persist the row before teardown.
            await asyncio.sleep(0.05)

    # app1's in-memory manager is gone now. A fresh app against the same
    # database must rebuild the job history from the import_jobs table.
    settings2 = Settings(
        acmi_enabled=False, database_url=f"sqlite+aiosqlite:///{db_path}"
    )
    app2 = create_app(settings2)
    async with app2.router.lifespan_context(app2):
        async with await _open_client(app2) as http:
            job = await http.get(f"/api/imports/{job_id}")
            assert job.status_code == 200
            assert job.json()["status"] == "completed"
            listing = await http.get("/api/imports")
            ids = [j["id"] for j in listing.json()["items"]]
            assert job_id in ids


async def test_importing_same_acmi_twice_deduplicates(tmp_path) -> None:
    """Issue #41: re-importing an ACMI that is already in the database must
    skip the duplicate landing rather than creating a second row.

    This is the same-session case: identical bytes carry an identical session
    header, so it stays a duplicate under the RecordingTime-keyed rule too
    (the sample has no RecordingTime at all, which the guard treats as "may
    match" and falls back to ReferenceTime). The cross-session case, which
    must NOT be skipped, is covered by
    ``test_two_sessions_of_the_same_mission_are_both_imported``.
    """
    app = create_app(_settings(tmp_path))
    async with app.router.lifespan_context(app):
        async with await _open_client(app) as http:
            body = _sample_acmi().encode()

            first = await http.post(
                "/api/import",
                files={"file": ("session.acmi", body, "text/plain")},
            )
            await _wait_for_job(http, first.json()["id"])

            second = await http.post(
                "/api/import",
                files={"file": ("session.acmi", body, "text/plain")},
            )
            job2 = await _wait_for_job(http, second.json()["id"])
            # The second run recognizes the already-recorded touchdown.
            assert job2["duplicates_skipped"] >= 1
            assert job2["landings_detected"] == 0

            # Imports are intentionally kept out of the shared history
            # (GET /api/landings excludes import-sourced rows), so verify the
            # dedupe at the database level: re-importing added no second row.
            import sqlite3

            db = sqlite3.connect(str(tmp_path / "import.db"))
            try:
                count = db.execute("SELECT COUNT(*) FROM landings").fetchone()[0]
            finally:
                db.close()
            assert count == 1


async def test_discarded_import_does_not_come_back_after_a_restart(tmp_path) -> None:
    """A discard has to remove the durable job row too (Issue #28 added it).

    Popping only the in-memory entry leaves the row behind, so the next
    restart's ``load_persisted()`` resurrects the job: the UI lists a
    completed import whose flights, tracks and landings are all gone, and
    the retention sweep "discards" it again on every restart, forever.
    """
    import asyncio

    db_path = (tmp_path / "discard.db").as_posix()
    settings1 = Settings(
        acmi_enabled=False, database_url=f"sqlite+aiosqlite:///{db_path}"
    )
    app1 = create_app(settings1)
    async with app1.router.lifespan_context(app1):
        async with await _open_client(app1) as http:
            response = await http.post(
                "/api/import",
                files={"file": ("session.acmi", _sample_acmi().encode(), "text/plain")},
            )
            job_id = response.json()["id"]
            await _wait_for_job(http, job_id)
            await asyncio.sleep(0.05)
            assert (await http.post(f"/api/imports/{job_id}/discard")).status_code == 204
            assert (await http.get(f"/api/imports/{job_id}")).status_code == 404

    settings2 = Settings(
        acmi_enabled=False, database_url=f"sqlite+aiosqlite:///{db_path}"
    )
    app2 = create_app(settings2)
    async with app2.router.lifespan_context(app2):
        async with await _open_client(app2) as http:
            assert (await http.get(f"/api/imports/{job_id}")).status_code == 404
            listing = await http.get("/api/imports")
            assert [j["id"] for j in listing.json()["items"]] == []


async def test_import_survives_a_progress_write_mid_batch(tmp_path, monkeypatch) -> None:
    """The periodic job-progress write must not deadlock against the import's
    own open ingest batch.

    ``_persist_job`` opens a second connection. If the ingest batch is already
    holding SQLite's write lock -- which it does as soon as a previously
    unseen object is flushed to get its row id for a foreign key -- nothing
    will commit that batch while ``_process`` awaits the progress write, so
    the write sits out its busy_timeout and the whole import fails with
    "database is locked".

    Forced deterministic by persisting at every yield; in production the
    2 s interval only makes it rarer, not safe (the live database averages a
    new object every ~2200 track rows against a 200-row batch).
    """
    import app.importer as importer_module

    monkeypatch.setattr(importer_module, "JOB_PERSIST_INTERVAL_S", 0.0)
    monkeypatch.setattr(importer_module, "YIELD_EVERY_LINES", 1)

    app = create_app(_settings(tmp_path))
    async with app.router.lifespan_context(app):
        async with await _open_client(app) as http:
            response = await http.post(
                "/api/import",
                files={"file": ("session.acmi", _sample_acmi().encode(), "text/plain")},
            )
            assert response.status_code == 202
            job = await _wait_for_job(http, response.json()["id"], timeout_s=60.0)

    assert job["status"] == "completed", job.get("error")
    assert job["landings_detected"] >= 1
