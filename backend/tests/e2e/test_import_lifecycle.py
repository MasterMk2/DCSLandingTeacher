"""Synthetic acceptance checks for failed, interrupted and discarded imports."""

from __future__ import annotations

import asyncio
import io
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.api.imports as imports_api
from app.api.main import create_app
from app.importer import ImportJob
from app.ingest import TrackIngestor
from app.models.entities import DcsObject, Flight, ImportJobRow, Landing, LandingTrack, Track
from tests.e2e.test_import_api import _wait_for_job
from tests.helpers import make_acmi_text, make_api_settings, make_approach_samples, open_api_client


@pytest.fixture(autouse=True, params=["sqlite", "postgres"])
def import_database(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check scratch cleanup on both migrated PostgreSQL and legacy SQLite."""
    if request.param == "postgres":
        monkeypatch.setenv("DLT_TEST_ACTIVE_POSTGRES_URL", request.getfixturevalue("database_url"))
    else:
        monkeypatch.delenv("DLT_TEST_ACTIVE_POSTGRES_URL", raising=False)


def _recording(recording_time: str | None = "2026-08-25T07:03:07Z") -> bytes:
    return make_acmi_text(
        make_approach_samples(outcome="full_stop"), recording_time=recording_time
    ).encode()


async def _counts(factory: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    async with factory() as session:
        return {
            model.__tablename__: (
                await session.execute(select(func.count()).select_from(model))
            ).scalar_one()
            for model in (Flight, DcsObject, Track, Landing, LandingTrack, ImportJobRow)
        }


def _capture_spools(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    paths: list[Path] = []
    save_upload = imports_api._save_upload

    async def capture(upload: UploadFile, max_bytes: int) -> Path:
        path = await save_upload(upload, max_bytes)
        paths.append(path)
        return path

    monkeypatch.setattr(imports_api, "_save_upload", capture)
    return paths


def _gate_reader(
    monkeypatch: pytest.MonkeyPatch, frame: str = "#970"
) -> tuple[asyncio.Event, asyncio.Event]:
    """Pause a real reader after committing part of the approach, before landing."""
    entered, release = asyncio.Event(), asyncio.Event()
    handle_line = TrackIngestor.handle_line

    async def gated(ingestor: TrackIngestor, line: str) -> None:
        await handle_line(ingestor, line)
        if line == frame and not entered.is_set():
            await ingestor.flush()
            entered.set()
            await release.wait()

    monkeypatch.setattr(TrackIngestor, "handle_line", gated)
    return entered, release


@pytest.mark.parametrize("queued", [False, True], ids=["processing", "pending"])
async def test_discard_stops_reader_and_removes_spool_and_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, queued: bool
) -> None:
    """Repeated DELETE/beacon discards cannot leave a reader recreating scratch data."""
    settings = make_api_settings(tmp_path)
    app = create_app(settings)
    paths = _capture_spools(monkeypatch)
    entered, release = _gate_reader(monkeypatch)
    async with app.router.lifespan_context(app):
        async with open_api_client(app) as http:
            first = asyncio.create_task(
                http.post("/api/v1/import", files={"file": ("active.acmi", _recording())})
            )
            second = None
            try:
                await asyncio.wait_for(entered.wait(), 10)
                active_id = app.state.import_manager.list_jobs()[0].id
                if queued:
                    second = asyncio.create_task(
                        http.post("/api/v1/import", files={"file": ("queued.acmi", _recording())})
                    )
                    async with asyncio.timeout(10):
                        while len(paths) < 2:
                            await asyncio.sleep(0)
                    target = next(
                        j for j in app.state.import_manager.list_jobs() if j.id != active_id
                    )
                    assert target.status == "pending"
                    target_id = target.id
                else:
                    target_id = active_id
                    assert (await _counts(app.state.session_factory))["flights"] > 0

                deleted, beacon = await asyncio.gather(
                    http.delete(f"/api/v1/imports/{target_id}"),
                    http.post(f"/api/v1/imports/{target_id}/discard"),
                )
                assert deleted.status_code == beacon.status_code == 204
                # Cleanup must finish while the active reader is still gated.
                assert not paths[1 if queued else 0].exists()
                assert (await http.get(f"/api/v1/imports/{target_id}")).status_code == 404
            finally:
                release.set()
                await asyncio.wait_for(asyncio.gather(first, *([second] if second else [])), 10)

            if queued:
                assert (await http.delete(f"/api/v1/imports/{active_id}")).status_code == 204
            assert await _counts(app.state.session_factory) == {
                "flights": 0, "objects": 0, "tracks": 0, "landings": 0,
                "landing_tracks": 0, "import_jobs": 0,
            }
            assert all(not path.exists() for path in paths)

    # Neither a durable job nor orphaned data may return on restart. A new
    # import of the same session must work, without a ghost duplicate.
    restarted = create_app(settings)
    async with restarted.router.lifespan_context(restarted):
        async with open_api_client(restarted) as http:
            assert (await http.get("/api/v1/imports")).json()["items"] == []
            response = await http.post(
                "/api/v1/import", files={"file": ("next.acmi", _recording())}
            )
            job = await _wait_for_job(http, response.json()["id"])
            assert job["status"] == "completed"
            assert job["landings_detected"] == 1
            assert job["duplicates_skipped"] == 0


def _archive(case: str) -> bytes:
    if case == "truncated":
        return b"PK\x03\x04broken"
    if case == "invalid-header":
        return b"broken zip header"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        if case == "directory":
            archive.writestr("folder/", b"")
        elif case in ("crc", "encoding"):
            archive.writestr("session.acmi", _recording() if case == "crc" else b"\xff\xfe")
    payload = buffer.getvalue()
    if case == "crc":
        start = payload.index(b"FileType=")
        payload = payload[:start] + b"X" + payload[start + 1:]
    return payload


@pytest.mark.parametrize(
    "case", ["truncated", "invalid-header", "crc", "encoding", "empty", "directory"]
)
async def test_archive_failure_or_zero_result_is_durable_and_cleans_spool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Corruption is failed; valid archives with no frames retain zero-result semantics."""
    settings = make_api_settings(tmp_path)
    paths = _capture_spools(monkeypatch)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with open_api_client(app) as http:
            response = await http.post(
                "/api/v1/import", files={"file": ("archive.acmi.zip", _archive(case))}
            )
            assert response.status_code == 202
            job_id = response.json()["id"]
            job = await _wait_for_job(http, job_id)
            assert job["status"] == ("completed" if case in ("empty", "directory") else "failed")
            assert bool(job["error"]) == (job["status"] == "failed")
            assert job["finished_at"] is not None
            assert job["landings_detected"] == 0
            assert paths and all(not path.exists() for path in paths)
            counts = await _counts(app.state.session_factory)
            assert counts == {name: int(name == "import_jobs") for name in counts}

    restarted = create_app(settings)
    async with restarted.router.lifespan_context(restarted):
        async with open_api_client(restarted) as http:
            restored = (await http.get(f"/api/v1/imports/{job_id}")).json()
            # PostgreSQL may return the same instant in the database server's
            # timezone, whereas the live job uses UTC. Compare instants.
            for field in ("created_at", "started_at", "finished_at"):
                assert datetime.fromisoformat(restored[field]) == datetime.fromisoformat(job[field])
                restored[field] = job[field]
            assert restored == job
            assert (await http.delete(f"/api/v1/imports/{job_id}")).status_code == 204
            assert all(value == 0 for value in (await _counts(restarted.state.session_factory)).values())


async def test_restart_marks_interrupted_active_and_queued_jobs_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lost tasks stay failed on repeated startup; a committed landing still deduplicates."""
    settings = make_api_settings(tmp_path)
    app = create_app(settings)
    paths = [tmp_path / "active.acmi", tmp_path / "pending.acmi"]
    for path in paths:
        path.write_bytes(_recording())
    # This gate is after a real landing was finalized and committed, but
    # before the recording finished. It exercises partial durable results.
    entered, release = _gate_reader(monkeypatch, "#1020")
    async with app.router.lifespan_context(app):
        async with open_api_client(app) as http:
            manager = app.state.import_manager
            active_job = manager.create_job("active.acmi")
            active = asyncio.create_task(manager.run(active_job, paths[0]))
            pending = None
            try:
                await asyncio.wait_for(entered.wait(), 10)
                assert (await _counts(app.state.session_factory))["landings"] == 1
                pending_job = manager.create_job("pending.acmi")
                pending = asyncio.create_task(manager.run(pending_job, paths[1]))
                async with asyncio.timeout(10):
                    while True:
                        async with app.state.session_factory() as session:
                            rows = (await session.execute(select(ImportJobRow))).scalars().all()
                        if {row.status for row in rows} == {"pending", "processing"}:
                            break
                        await asyncio.sleep(0.01)
                ids = {row.filename: row.id for row in rows}
            finally:
                # Simulate disappearance of both background tasks without a
                # discard. Retained DB state is the input to restart recovery.
                active.cancel()
                if pending is not None:
                    pending.cancel()
                await asyncio.gather(active, *([pending] if pending else []), return_exceptions=True)
                release.set()
            assert all(not path.exists() for path in paths)
            before_restart = await _counts(app.state.session_factory)

    restored_jobs = {}
    for restart in range(2):
        restarted = create_app(settings)
        async with restarted.router.lifespan_context(restarted):
            async with open_api_client(restarted) as http:
                assert await _counts(restarted.state.session_factory) == before_restart
                for filename, job_id in ids.items():
                    response = await http.get(f"/api/v1/imports/{job_id}")
                    assert response.status_code == 200
                    job = response.json()
                    assert job["status"] == "failed"
                    assert job["error"] == "interrupted by server restart"
                    assert job["finished_at"] is not None
                    assert job["landings_detected"] == int(filename == "active.acmi")
                    if filename == "active.acmi":
                        assert job["frames_processed"] > 0
                    else:
                        assert job["frames_processed"] == 0
                    if restart == 0:
                        restored_jobs[job_id] = job
                    else:
                        assert job == restored_jobs[job_id]
                if restart == 1:
                    response = await http.post(
                        "/api/v1/import", files={"file": ("retry.acmi", _recording())}
                    )
                    retry = await _wait_for_job(http, response.json()["id"])
                    assert retry["status"] == "completed"
                    assert retry["landings_detected"] == 0
                    assert retry["duplicates_skipped"] == 1
                    assert (await _counts(restarted.state.session_factory))["landings"] == 1


@pytest.mark.parametrize(
    ("first_time", "second_time", "duplicate"),
    [
        (None, "2026-08-25T07:03:07Z", True),
        ("2026-08-25T07:03:07Z", None, True),
        ("2026-08-25T07:03:07Z", "2026-08-26T07:03:07Z", False),
    ],
    ids=["legacy-stored-session", "missing-upload-session", "distinct-sessions"],
)
async def test_session_deduplication_fallback_survives_restart(
    tmp_path: Path, first_time: str | None, second_time: str | None, duplicate: bool
) -> None:
    settings = make_api_settings(tmp_path)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with open_api_client(app) as http:
            response = await http.post(
                "/api/v1/import", files={"file": ("first.acmi", _recording(first_time))}
            )
            first = await _wait_for_job(http, response.json()["id"])
            assert first["status"] == "completed"
            assert first["landings_detected"] == 1

    restarted = create_app(settings)
    async with restarted.router.lifespan_context(restarted):
        async with open_api_client(restarted) as http:
            response = await http.post(
                "/api/v1/import", files={"file": ("renamed.acmi", _recording(second_time))}
            )
            second = await _wait_for_job(http, response.json()["id"])
            assert second["status"] == "completed"
            assert second["landings_detected"] == int(not duplicate)
            assert second["duplicates_skipped"] == int(duplicate)
            assert (await _counts(restarted.state.session_factory))["landings"] == 1 + int(not duplicate)


async def test_discard_after_reader_finishes_removes_landing_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Race a discard with completion before the final job write and notification."""
    app = create_app(make_api_settings(tmp_path))
    paths = _capture_spools(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    async with app.router.lifespan_context(app):
        manager = app.state.import_manager
        process = manager._process

        async def gate_completion(job: ImportJob, path: Path) -> None:
            await process(job, path)
            entered.set()
            await release.wait()

        monkeypatch.setattr(manager, "_process", gate_completion)
        async with open_api_client(app) as http:
            upload = asyncio.create_task(
                http.post("/api/v1/import", files={"file": ("complete.acmi", _recording())})
            )
            try:
                await asyncio.wait_for(entered.wait(), 10)
                counts = await _counts(app.state.session_factory)
                assert counts["landings"] == counts["landing_tracks"] == 1
                job_id = manager.list_jobs()[0].id
                assert (await http.delete(f"/api/v1/imports/{job_id}")).status_code == 204
                assert all(value == 0 for value in (await _counts(app.state.session_factory)).values())
                assert all(not path.exists() for path in paths)
            finally:
                release.set()
                await asyncio.wait_for(upload, 10)


async def test_discard_before_background_run_does_not_start_reader(tmp_path: Path) -> None:
    """A beacon can arrive after job creation but before BackgroundTasks runs it."""
    app = create_app(make_api_settings(tmp_path))
    path = tmp_path / "not-started.acmi"
    path.write_bytes(_recording())
    async with app.router.lifespan_context(app):
        manager = app.state.import_manager
        job = manager.create_job("not-started.acmi")
        async with open_api_client(app) as http:
            assert (await http.post(f"/api/v1/imports/{job.id}/discard")).status_code == 204
            await manager.run(job, path)
            assert not path.exists()
            assert all(value == 0 for value in (await _counts(app.state.session_factory)).values())
            assert (await http.get(f"/api/v1/imports/{job.id}")).status_code == 404
