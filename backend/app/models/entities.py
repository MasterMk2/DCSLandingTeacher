"""ORM entities for DCS Landing Teacher.

Initial schema (Phase 1). ``landings`` is a placeholder design that already
carries the columns the future detection/grading tasks will populate.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Flight(Base):
    """One ACMI session/mission received from a Tacview stream."""

    __tablename__ = "flights"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    reference_time: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
    """Landing / carrier-arrestment event.

    Placeholder for Phase 1: detection and grading tasks will fill in the
    evaluation columns.
    """

    __tablename__ = "landings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    flight_id: Mapped[int] = mapped_column(ForeignKey("flights.id", ondelete="CASCADE"), index=True)
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id", ondelete="CASCADE"), index=True)

    kind: Mapped[str | None] = mapped_column(String(16), nullable=True)  # "land" / "carrier"
    touchdown_time: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Touchdown position / state
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    altitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    heading: Mapped[float | None] = mapped_column(Float, nullable=True)
    speed: Mapped[float | None] = mapped_column(Float, nullable=True)
    descent_rate: Mapped[float | None] = mapped_column(Float, nullable=True)  # m/s at touchdown

    # Evaluation results (populated by grading tasks)
    grade: Mapped[str | None] = mapped_column(String(32), nullable=True)
    factors: Mapped[list | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
