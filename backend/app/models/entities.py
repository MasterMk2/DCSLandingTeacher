"""ORM entities for DCS Landing Teacher.

Initial schema (Phase 1). ``landings`` is a placeholder design that already
carries the columns the future detection/grading tasks will populate.
"""

from __future__ import annotations

import json
import zlib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    DDL,
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    event,
)
from sqlalchemy.ext.associationproxy import association_proxy
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CompressedJSON(TypeDecorator):
    """JSON stored zlib-compressed in a BLOB.

    For the approach tracks: a few hundred KB of JSON each, which zlib
    shrinks about sevenfold (724 MB -> 106 MB on 1,723 synthetic tracks
    shaped like real ones).
    """

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> bytes | None:
        if value is None:
            return None
        return zlib.compress(json.dumps(value).encode("utf-8"), 6)

    def process_result_value(self, value: bytes | None, dialect: Any) -> Any:
        if value is None:
            return None
        return json.loads(zlib.decompress(value))


class Flight(Base):
    """One ACMI session/mission received from a Tacview stream."""

    __tablename__ = "flights"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True, default="default")
    #: ACMI ``ReferenceTime`` -- the mission's IN-GAME date, taken from the
    #: .miz. Identical for every session of the same mission, so it does not
    #: identify a session.
    reference_time: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: ACMI ``RecordingTime`` -- when this recording started, in real time.
    #: This is what distinguishes one session from another.
    recording_time: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data_source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    data_recorder: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    theater: Mapped[str | None] = mapped_column(String(128), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class DcsObject(Base):
    """Dictionary of DCS objects seen in a flight (aircraft, carriers, ...)."""

    __tablename__ = "objects"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    flight_id: Mapped[int] = mapped_column(ForeignKey("flights.id", ondelete="CASCADE"), index=True)
    acmi_id: Mapped[str] = mapped_column(String(16))
    type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pilot: Mapped[str | None] = mapped_column(String(128), nullable=True)
    group_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    country: Mapped[str | None] = mapped_column(String(8), nullable=True)
    first_seen: Mapped[float] = mapped_column(Float)
    last_seen: Mapped[float] = mapped_column(Float)
    removed: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        Index("ix_objects_flight_acmi_id", "flight_id", "acmi_id", unique=True),
    )


class Track(Base):
    """Time-series position sample for one object."""

    __tablename__ = "tracks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    flight_id: Mapped[int] = mapped_column(ForeignKey("flights.id", ondelete="CASCADE"), index=True)
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id", ondelete="CASCADE"), index=True)
    mission_time: Mapped[float] = mapped_column(Float)

    # Spherical world coordinates (degrees / meters MSL)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    altitude: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Flat/native world coordinates (meters)
    u: Mapped[float | None] = mapped_column(Float, nullable=True)
    v: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Attitude and motion
    roll: Mapped[float | None] = mapped_column(Float, nullable=True)
    pitch: Mapped[float | None] = mapped_column(Float, nullable=True)
    yaw: Mapped[float | None] = mapped_column(Float, nullable=True)
    heading: Mapped[float | None] = mapped_column(Float, nullable=True)
    speed: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Data useful for later landing detection / grading
    on_ground: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    agl: Mapped[float | None] = mapped_column(Float, nullable=True)
    aoa: Mapped[float | None] = mapped_column(Float, nullable=True)


class Landing(Base):
    """Landing / carrier-arrestment event with grading results (FR-2..FR-4)."""

    __tablename__ = "landings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    flight_id: Mapped[int] = mapped_column(ForeignKey("flights.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True, default="default")
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id", ondelete="CASCADE"), index=True)
    carrier_object_id: Mapped[int | None] = mapped_column(
        ForeignKey("objects.id", ondelete="SET NULL"), nullable=True
    )

    kind: Mapped[str | None] = mapped_column(String(16), nullable=True)  # "land" / "carrier"
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # "full_stop" | "touch_and_go" | "bolter"
    touchdown_time: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Two-phase confirmation (Issue #5): a landing detected at touchdown is
    # stored as "provisional" (outcome may still turn into touch_and_go /
    # bolter) and flipped to "final" once the outcome can no longer change.
    outcome_status: Mapped[str] = mapped_column(String(16), default="final")

    # Venue (carrier name or airbase/static object name when known)
    venue_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Touchdown position / state
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    altitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    heading: Mapped[float | None] = mapped_column(Float, nullable=True)
    speed: Mapped[float | None] = mapped_column(Float, nullable=True)
    descent_rate: Mapped[float | None] = mapped_column(Float, nullable=True)  # m/s at touchdown

    # Evaluation results (populated by the grading engine)
    grade: Mapped[str | None] = mapped_column(String(32), nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    comment: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    factors: Mapped[list | None] = mapped_column(JSON, nullable=True)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Raw approach segment + computed deviations, kept for re-evaluation
    # (FR-7). Lives in its own table (LandingTrack); ``approach_track`` reads
    # and writes it as if it were a column. Loaded with the landing unless a
    # query opts out -- the list does (raiseload), since it never shows it.
    track: Mapped[LandingTrack | None] = relationship(
        lazy="selectin", cascade="all, delete-orphan", uselist=False
    )
    approach_track = association_proxy(
        "track", "approach_track", creator=lambda value: LandingTrack(approach_track=value)
    )

    # Approach pattern classification: "overhead" | "straight_in" | "unknown"
    approach_pattern: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Who flew it and in what, captured AT DETECTION TIME.
    #
    # These used to be read from the ``objects`` row instead, which is wrong
    # because that row is mutable and shared: Tacview reuses an object's hex
    # id within a recording, and the ingest matches on (flight_id, acmi_id),
    # so a later object -- a missile, another player's aircraft -- overwrites
    # the name and pilot of the row an earlier landing points at. Measured on
    # this server: 124 landings display an airframe that disagrees with the
    # one recorded in their own ``approach_track``, including 7 UH-1H
    # landings shown as "AIM_120".
    #
    # A landing's aircraft is a fact about the landing and does not change
    # afterwards, so it is stored with the landing. Null on rows written
    # before this column existed; readers fall back to the object row.
    pilot: Mapped[str | None] = mapped_column(String(128), nullable=True)
    airframe: Mapped[str | None] = mapped_column(String(128), nullable=True)

    grading_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    graded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class LandingTrack(Base):
    """One landing's stored approach track, compressed.

    Out of ``landings`` because a big value in the middle of a row is read
    whenever a column after it is: SQLite reaches a later column by walking
    the row's overflow pages, and ``created_at``, ``source_id``, ``pilot``
    and ``airframe`` -- what the list sorts and filters on -- all came after
    ``approach_track``. With 1,723 tracks of ~300 KB (synthetic, measured)
    the list's count and page queries took 320 ms and 960 ms; with the track
    in this table, 1.5 ms and 1 ms.
    """

    __tablename__ = "landing_tracks"

    landing_id: Mapped[int] = mapped_column(
        ForeignKey("landings.id", ondelete="CASCADE"), primary_key=True
    )
    approach_track: Mapped[Any] = mapped_column(CompressedJSON, nullable=True)


#: SQLite does not enforce foreign keys here (no ``PRAGMA foreign_keys``), and
#: landings are also deleted in bulk (the import cleanup). A deleted landing's
#: id is reused by the next insert, which would then collide with the track
#: left behind -- so the track goes with the landing, however it is deleted.
LANDING_TRACK_TRIGGER = (
    "CREATE TRIGGER IF NOT EXISTS landings_delete_track AFTER DELETE ON landings "
    "BEGIN DELETE FROM landing_tracks WHERE landing_id = OLD.id; END"
)
event.listen(LandingTrack.__table__, "after_create", DDL(LANDING_TRACK_TRIGGER))


class ImportJobRow(Base):
    """Persisted ACMI import job (Issue #28).

    Import jobs were previously tracked only in memory, so job metadata (status,
    progress, errors) was lost on every server restart and
    ``GET /api/imports/{id}`` returned 404 for completed jobs. This row makes
    the job history durable across restarts; the in-memory ``ImportJob`` is
    reconstructed from these rows on startup.
    """

    __tablename__ = "import_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    filename: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    progress_percent: Mapped[int | None] = mapped_column(nullable=True)
    frames_processed: Mapped[int] = mapped_column(default=0)
    total_frames: Mapped[int] = mapped_column(default=0)
    landings_detected: Mapped[int] = mapped_column(default=0)
    duplicates_skipped: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

