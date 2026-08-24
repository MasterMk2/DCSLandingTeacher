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
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from logging import getLogger
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.acmi.file_reader import iter_acmi_lines
from app.ingest import LandingContext, TrackIngestor
from app.models.entities import DcsObject, Flight, Landing
from app.pipeline import LandingPipeline

logger = getLogger(__name__)

#: Touchdown times are floats; treat sub-millisecond differences as equal.
TOUCHDOWN_EPSILON_S = 0.001

#: Yield to the event loop every N parsed lines so a huge import does not
#: starve the HTTP server / WebSocket broadcasts running on the same loop.
YIELD_EVERY_LINES = 500


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
            key = (context.acmi_object_id, round(context.event.touchdown.time, 3))
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
        reference_time = self._ingestor.parser.header.get("ReferenceTime")
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

    # -- processing -----------------------------------------------------

    async def run(self, job: ImportJob, file_path: str | Path) -> None:
        """Process one uploaded ACMI file to completion (background task).

        Queues behind any import already running (see ``_run_lock``); the
        job stays "pending" until it actually acquires the lock and starts.
        """
        async with self._run_lock:
            job.status = "processing"
            job.started_at = _utcnow()
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
            for line in iter_acmi_lines(path):
                if line.startswith("#"):
                    job.frames_processed += 1
                await ingestor.handle_line(line)
                lines_since_yield += 1
                if lines_since_yield >= YIELD_EVERY_LINES:
                    lines_since_yield = 0
                    await asyncio.sleep(0)
        finally:
            await ingestor.close()

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
