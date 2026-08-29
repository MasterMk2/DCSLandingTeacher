"""ACMI file import: saved .acmi / .acmi.txt / .acmi.zip -> landing records.

Feeds uploaded files through the exact same ingest -> detection -> grading
pipeline used for the real-time Tacview stream, so imported landings are
indistinguishable from live ones (same grading, same WebSocket notifications).

Jobs are tracked in memory: ``POST /api/import`` returns a job id immediately
and the file is processed in a background task; clients poll
``GET /api/imports/{id}`` for progress.

Duplicate protection: re-importing a file must not create a second copy of
its landings. Before persisting, each detected touchdown is checked against
existing rows by the combination

    Flight.reference_time + Landing.touchdown_time + DcsObject.acmi_id

(the ACMI ``ReferenceTime`` header identifies the recording, and object ids
are stable within it). Matches are skipped and reported in the job summary.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from logging import getLogger
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.acmi.file_reader import iter_acmi_lines
from app.ingest import LandingContext, TrackIngestor
from app.models.entities import DcsObject, Flight, ImportJobRow, Landing, Track
from app.pipeline import LandingPipeline

logger = getLogger(__name__)

#: Touchdown times are floats; treat sub-millisecond differences as equal.
TOUCHDOWN_EPSILON_S = 0.001

#: Yield to the event loop every N parsed lines so a huge import does not
#: starve the HTTP server / WebSocket broadcasts running on the same loop.
YIELD_EVERY_LINES = 500

#: Minimum wall-clock gap between two writes of an import job's progress
#: row. Bounds how often the import competes for SQLite's write lock.
JOB_PERSIST_INTERVAL_S = 2.0

#: Imported recordings are scoped to their own source rather than joining the
#: live servers' records: an upload is very often from an unrelated server (or
#: an unrelated theatre entirely), and mixing it into the shared history would
#: misrepresent what happened on the servers this instance actually watches.
#: Everything under this prefix is treated as scratch data -- excluded from the
#: default listing and discarded, either explicitly or by retention sweep.
IMPORT_SOURCE_PREFIX = "import:"


def import_source_id(job_id: str) -> str:
    return f"{IMPORT_SOURCE_PREFIX}{job_id}"


def is_import_source(source_id: str | None) -> bool:
    return bool(source_id) and source_id.startswith(IMPORT_SOURCE_PREFIX)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(value: datetime | None) -> datetime | None:
    """Coerce a datastore value to a timezone-aware UTC ``datetime``.

    SQLite drops timezone metadata when persisting ``DateTime(timezone=True)``
    columns, so a ``row.created_at`` loaded back is naive; comparing it with
    the aware ``_utcnow()`` cutoff would otherwise raise ``TypeError``.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@dataclass
class ImportJob:
    """State of one ACMI file import (in-memory, not persisted)."""

    id: str
    filename: str
    status: str = "pending"  # pending | processing | completed | failed
    created_at: datetime = field(default_factory=_utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    frames_processed: int = 0
    total_frames: int = 0
    landings_detected: int = 0
    duplicates_skipped: int = 0
    error: str | None = None

    @property
    def progress_percent(self) -> int | None:
        """Progress 0-100, or None if total_frames is unknown."""
        if self.total_frames <= 0:
            return None
        return min(100, max(0, int(self.frames_processed * 100 / self.total_frames)))

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": (
                self.finished_at.isoformat() if self.finished_at else None
            ),
            "frames_processed": self.frames_processed,
            "total_frames": self.total_frames,
            "progress_percent": self.progress_percent,
            "landings_detected": self.landings_detected,
            "duplicates_skipped": self.duplicates_skipped,
            "error": self.error,
        }


class _DuplicateGuard:
    """Landing listener that filters already-recorded touchdowns.

    Wraps the real pipeline: duplicates are counted and skipped, everything
    else flows through unchanged (grading + DB + WebSocket notification).
    """

    def __init__(
        self,
        job: ImportJob,
        session_factory: async_sessionmaker[AsyncSession],
        pipeline: LandingPipeline,
        ingestor: TrackIngestor,
    ) -> None:
        self._job = job
        self._session_factory = session_factory
        self._pipeline = pipeline
        self._ingestor = ingestor
        # Keys already skipped in this run: provisional + final passes see
        # the same touchdown twice; count each duplicate only once.
        self._skipped_keys: set[tuple[str, float]] = set()

    async def handle_landing(self, context: LandingContext) -> int | None:
        if await self._is_duplicate(context):
            # Full-precision timestamp: rounding to 1 ms could otherwise treat
            # the same touchdown as two distinct skips (Issue #30). The actual
            # duplicate decision is made in the database by an epsilon compare.
            key = (context.acmi_object_id, context.event.touchdown.time)
            if key not in self._skipped_keys:
                self._skipped_keys.add(key)
                self._job.duplicates_skipped += 1
            logger.info(
                "import %s: skipping duplicate landing obj=%s t=%.2f",
                self._job.id,
                context.acmi_object_id,
                context.event.touchdown.time,
            )
            return None
        landing_id = await self._pipeline.handle_landing(context)
        if landing_id is not None:
            self._job.landings_detected += 1
        return landing_id

    async def _is_duplicate(self, context: LandingContext) -> bool:
        """Has this exact touchdown already been recorded?

        The identity of a touchdown is (aircraft slot, mission-elapsed time,
        session). All three parts are needed and only the third is hard:
        ACMI object ids are mission unit ids that get reused as slots change
        hands, and ``touchdown_time`` is mission-elapsed seconds, which
        restarts at zero every session.

        ``ReferenceTime`` cannot stand in for the session: it is the .miz's
        in-game date, so every session of the same mission carries the same
        value -- 24 of this server's flights share
        ``2026-06-11T04:30:00Z``. Keyed on that alone, a recording of
        yesterday's sortie counts as "already known" as soon as a slot id
        and a mission clock line up with today's, and the import reports
        nothing found. ``RecordingTime`` is stamped when the recording
        started, so it does identify the session.

        Rows written before that column existed have it NULL; treat those as
        possible matches rather than as non-matches, or re-importing a
        session that IS already in the history would duplicate all of it.
        """
        header = self._ingestor.parser.header
        recording_time = header.get("RecordingTime")
        reference_time = header.get("ReferenceTime")
        touchdown_time = context.event.touchdown.time
        statement = (
            select(Landing.id)
            .join(DcsObject, Landing.object_id == DcsObject.id)
            .join(Flight, Landing.flight_id == Flight.id)
            .where(DcsObject.acmi_id == context.acmi_object_id)
            .where(Landing.touchdown_time.is_not(None))
            .where(
                func.abs(Landing.touchdown_time - touchdown_time)
                < TOUCHDOWN_EPSILON_S
            )
        )
        if recording_time is not None:
            statement = statement.where(
                or_(
                    Flight.recording_time == recording_time,
                    Flight.recording_time.is_(None),
                )
            )
        if reference_time is not None:
            statement = statement.where(Flight.reference_time == reference_time)
        else:
            statement = statement.where(Flight.reference_time.is_(None))
        async with self._session_factory() as session:
            result = await session.execute(statement)
            return result.first() is not None


class ImportJobManager:
    """Creates import jobs and processes them in the background."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        pipeline: LandingPipeline,
        notifier: Any | None = None,
        sample_buffer_s: float = 600.0,
    ) -> None:
        self._session_factory = session_factory
        self._pipeline = pipeline
        self._notifier = notifier
        self._sample_buffer_s = sample_buffer_s
        self._jobs: dict[str, ImportJob] = {}
        # SQLite only supports one writer at a time; running two imports (or
        # an import alongside the live stream) concurrently makes both sides
        # fight over the write lock and fail with "database is locked" once
        # the busy_timeout is exceeded. Serializing imports here removes the
        # import-vs-import case entirely.
        self._run_lock = asyncio.Lock()

    # -- registry -------------------------------------------------------

    def create_job(self, filename: str) -> ImportJob:
        job = ImportJob(id=uuid.uuid4().hex, filename=filename)
        self._jobs[job.id] = job
        return job

    def get_job(self, job_id: str) -> ImportJob | None:
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[ImportJob]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    # -- persistence (Issue #28) ----------------------------------------

    async def _persist_job(self, job: ImportJob) -> None:
        """Upsert the job's current state to the ``import_jobs`` table."""
        row = ImportJobRow(
            id=job.id,
            filename=job.filename,
            status=job.status,
            progress_percent=job.progress_percent,
            frames_processed=job.frames_processed,
            total_frames=job.total_frames,
            landings_detected=job.landings_detected,
            duplicates_skipped=job.duplicates_skipped,
            error=job.error,
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
        )
        async with self._session_factory() as session:
            await session.merge(row)
            await session.commit()

    async def load_persisted(self) -> None:
        """Rebuild in-memory job state from the database after a restart.

        Jobs that were still ``pending``/``processing`` when the server stopped
        can never resume (their background task is gone), so they are marked
        ``failed`` with an "interrupted by server restart" error and persisted
        again, giving an honest status instead of a silent 404.
        """
        from sqlalchemy import select

        async with self._session_factory() as session:
            rows = (await session.execute(select(ImportJobRow))).scalars().all()

        for row in rows:
            job = self._row_to_job(row)
            if job.status in ("pending", "processing"):
                job.status = "failed"
                job.error = "interrupted by server restart"
                job.finished_at = job.finished_at or _utcnow()
                await self._persist_job(job)
            self._jobs[job.id] = job

    @staticmethod
    def _row_to_job(row: ImportJobRow) -> ImportJob:
        return ImportJob(
            id=row.id,
            filename=row.filename,
            status=row.status,
            created_at=_as_aware(row.created_at),
            started_at=_as_aware(row.started_at),
            finished_at=_as_aware(row.finished_at),
            frames_processed=row.frames_processed,
            total_frames=row.total_frames,
            landings_detected=row.landings_detected,
            duplicates_skipped=row.duplicates_skipped,
            error=row.error,
        )

    # -- processing -----------------------------------------------------

    async def run(self, job: ImportJob, file_path: str | Path) -> None:
        """Process one uploaded ACMI file to completion (background task).

        Queues behind any import already running (see ``_run_lock``); the
        job stays "pending" until it actually acquires the lock and starts.
        """
        async with self._run_lock:
            job.status = "processing"
            job.started_at = _utcnow()
            await self._persist_job(job)
            try:
                await self._process(job, Path(file_path))
                job.status = "completed"
                logger.info(
                    "import %s completed: frames=%d landings=%d duplicates=%d",
                    job.id,
                    job.frames_processed,
                    job.landings_detected,
                    job.duplicates_skipped,
                )
            except Exception as exc:
                job.status = "failed"
                job.error = str(exc)
                logger.exception("import %s failed", job.id)
            finally:
                job.finished_at = _utcnow()
                await self._persist_job(job)
                _remove_quietly(file_path)
                await self._notify(job)

    async def _process(self, job: ImportJob, path: Path) -> None:
        # The duplicate guard needs the ingestor (for the ACMI header), so the
        # listener is wired through a late-bound closure.
        holder: list[_DuplicateGuard] = []

        async def guarded_listener(context: LandingContext) -> int | None:
            return await holder[0].handle_landing(context)

        ingestor = TrackIngestor(
            self._session_factory,
            sample_buffer_s=self._sample_buffer_s,
            landing_listener=guarded_listener,
            landing_finalize_listener=self._pipeline.finalize_landing,
            source_id=import_source_id(job.id),
        )
        holder.append(
            _DuplicateGuard(job, self._session_factory, self._pipeline, ingestor)
        )

        # Pre-count time-frame lines (#...) to estimate total frames for progress.
        # This is a fast single pass; the second pass does the actual ingest.
        try:
            total = 0
            for line in iter_acmi_lines(path):
                if line.startswith("#"):
                    total += 1
            job.total_frames = total
        except Exception:
            # If pre-count fails, progress will stay unknown (None).
            job.total_frames = 0

        try:
            lines_since_yield = 0
            last_persist = time.monotonic()
            for line in iter_acmi_lines(path):
                if line.startswith("#"):
                    job.frames_processed += 1
                await ingestor.handle_line(line)
                lines_since_yield += 1
                if lines_since_yield >= YIELD_EVERY_LINES:
                    lines_since_yield = 0
                    await asyncio.sleep(0)
                    # Persist progress so a restart mid-import still shows how
                    # far the job got (Issue #28) -- but on a time budget, not
                    # per YIELD_EVERY_LINES. A 4.8 M-line recording yields
                    # ~9,600 times, and each persist is a separate SELECT +
                    # UPDATE + COMMIT that takes SQLite's single write lock
                    # away from the ingest batches running in the same loop.
                    # Progress is a UI nicety; contending for the write lock
                    # thousands of times to keep it fresher than a couple of
                    # seconds works directly against Issue #21.
                    now = time.monotonic()
                    if now - last_persist >= JOB_PERSIST_INTERVAL_S:
                        last_persist = now
                        await self._persist_job(job)
        finally:
            await ingestor.close()

    async def discard(self, job_id: str) -> bool:
        """Drop an import's job entry and every record it created.

        Flights cascade to their objects, tracks and landings, so removing the
        flights of this import's source removes the whole scratch dataset.
        """
        source_id = import_source_id(job_id)
        async with self._session_factory() as session:
            flight_ids = select(Flight.id).where(Flight.source_id == source_id)
            # Delete the children explicitly. The ON DELETE CASCADE on these
            # foreign keys does nothing here: SQLite only enforces them with
            # PRAGMA foreign_keys=ON, and no ORM relationship cascade is
            # declared either, so relying on it would silently orphan every
            # track and landing of the discarded import.
            deleted = (
                await session.execute(
                    delete(Landing).where(Landing.flight_id.in_(flight_ids))
                )
            ).rowcount or 0
            await session.execute(delete(Track).where(Track.flight_id.in_(flight_ids)))
            await session.execute(
                delete(DcsObject).where(DcsObject.flight_id.in_(flight_ids))
            )
            flights = (
                await session.execute(
                    delete(Flight).where(Flight.source_id == source_id)
                )
            ).rowcount or 0
            # The durable job row goes too (Issue #28 added it). Without this,
            # popping the in-memory entry below is only half a discard: the row
            # survives, load_persisted() resurrects the job on the next
            # restart, and the UI lists a completed import whose data is gone
            # -- which purge_expired then "discards" again, every restart,
            # forever.
            await session.execute(
                delete(ImportJobRow).where(ImportJobRow.id == job_id)
            )
            await session.commit()
        existed = self._jobs.pop(job_id, None) is not None
        if flights or existed:
            logger.info(
                "discarded import %s (%d flight(s), %d landing(s))",
                job_id, flights, deleted,
            )
        return existed or bool(flights)

    async def purge_expired(self, retention_hours: float) -> int:
        """Drop imports older than ``retention_hours``.

        A browser cannot be relied on to announce that it closed, so the
        explicit discard is backed by this sweep; without it an abandoned
        upload would sit in the database forever.
        """
        if retention_hours <= 0:
            return 0
        cutoff = _utcnow() - timedelta(hours=retention_hours)
        stale = [
            job_id
            for job_id, job in self._jobs.items()
            if job.created_at < cutoff
        ]
        # Also catch data whose in-memory job is gone (e.g. after a restart).
        async with self._session_factory() as session:
            orphaned = (
                await session.execute(
                    select(Flight.source_id)
                    .where(Flight.source_id.like(f"{IMPORT_SOURCE_PREFIX}%"))
                    .where(Flight.created_at < cutoff)
                    .distinct()
                )
            ).scalars().all()
        for source_id in orphaned:
            job_id = source_id[len(IMPORT_SOURCE_PREFIX) :]
            if job_id not in stale:
                stale.append(job_id)

        for job_id in stale:
            await self.discard(job_id)
        return len(stale)

    async def _notify(self, job: ImportJob) -> None:
        """Broadcast the job outcome over the existing WebSocket channel."""
        if self._notifier is None:
            return
        try:
            await self._notifier.broadcast_message(
                {"type": "import", "import": job.as_dict()}
            )
        except Exception:
            logger.exception("import notification failed")


def _remove_quietly(path: str | Path | None) -> None:
    if path is None:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        logger.debug("could not remove temp file %s", path, exc_info=True)
