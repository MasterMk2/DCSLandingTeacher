"""Ingest pipeline: ACMI parser events -> ORM entities -> SQLite.

Phase 1 scope. Lines are parsed incrementally and persisted in batches:
a single session is reused across events and committed once
``max_batch_size`` pending writes have accumulated (or on :meth:`close`).
This avoids one transaction per telemetry line while keeping memory bounded.
"""

from __future__ import annotations

from logging import getLogger

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.acmi.models import (
    AcmiEvent,
    AcmiObject,
    MissionEvent,
    ObjectRemoveEvent,
    ObjectUpdateEvent,
)
from app.acmi.parser import AcmiParseError, AcmiParser
from app.models.entities import DcsObject, Flight, Track

logger = getLogger(__name__)


class TrackIngestor:
    """Consumes raw ACMI lines, parses them, and stores tracks in the DB."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        max_batch_size: int = 200,
    ) -> None:
        self._session_factory = session_factory
        self._max_batch_size = max(1, max_batch_size)
        self._parser = AcmiParser()
        self._flight_id: int | None = None
        #: acmi object id -> ``objects.id`` row
        self._object_row_ids: dict[str, int] = {}
        self._session: AsyncSession | None = None
        self._pending = 0

    @property
    def parser(self) -> AcmiParser:
        return self._parser

    async def handle_line(self, line: str) -> None:
        """Process one raw line coming from the stream client."""
        try:
            events = self._parser.feed_line(line)
        except AcmiParseError as exc:
            logger.warning("skipping unparsable ACMI line: %s", exc)
            return

        for event in events:
            if isinstance(event, ObjectUpdateEvent):
                await self._handle_update(event)
            elif isinstance(event, ObjectRemoveEvent):
                await self._handle_remove(event)
            elif isinstance(event, MissionEvent):
                # Landing detection (next subtask) will consume Landed /
                # TakenOff / Destroyed events; log them for now.
                logger.info(
                    "mission event %s at t=%.2f ids=%s text=%r",
                    event.event_type,
                    event.time,
                    list(event.object_ids),
                    event.text,
                )

    async def close(self) -> None:
        """Flush pending writes and release the session."""
        await self._flush(force=True)
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_session(self) -> AsyncSession:
        if self._session is None:
            self._session = self._session_factory()
            self._pending = 0
        return self._session

    async def _flush(self, force: bool = False) -> None:
        if self._session is None or (not force and self._pending < self._max_batch_size):
            return
        try:
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            await self._session.close()
            self._session = None
            raise
        finally:
            self._pending = 0

    async def _ensure_flight(self) -> int:
        if self._flight_id is not None:
            return self._flight_id
        header = self._parser.header
        session = self._get_session()
        flight = Flight(
            reference_time=header.get("ReferenceTime"),
            data_source=header.get("DataSource"),
            data_recorder=header.get("DataRecorder"),
            title=header.get("Title"),
            theater=header.get("Theater"),
        )
        session.add(flight)
        await session.flush()
        self._flight_id = flight.id
        self._pending += 1
        logger.info("created flight record id=%d", self._flight_id)
        return self._flight_id

    async def _handle_update(self, event: ObjectUpdateEvent) -> None:
        if event.obj_id == "0":
            # Global object carries mission metadata only; the flight row is
            # created lazily on the first real object update so that header
            # values have already arrived.
            return

        flight_id = await self._ensure_flight()
        source = self._parser.objects.get(event.obj_id)

        session = self._get_session()
        object_row_id = self._object_row_ids.get(event.obj_id)
        if object_row_id is None:
            result = await session.execute(
                select(DcsObject).where(
                    DcsObject.flight_id == flight_id,
                    DcsObject.acmi_id == event.obj_id,
                )
            )
            dcs_object = result.scalar_one_or_none()
            if dcs_object is None:
                dcs_object = DcsObject(
                    flight_id=flight_id,
                    acmi_id=event.obj_id,
                    first_seen=source.first_seen if source else event.time,
                    last_seen=source.last_seen if source else event.time,
                )
                session.add(dcs_object)
                await session.flush()
            object_row_id = dcs_object.id
            self._object_row_ids[event.obj_id] = object_row_id
        else:
            dcs_object = await session.get(DcsObject, object_row_id)

        if dcs_object is not None and source is not None:
            dcs_object.type = source.type
            dcs_object.name = source.name
            dcs_object.pilot = source.pilot
            dcs_object.group_name = source.group
            dcs_object.country = source.country
            dcs_object.last_seen = source.last_seen

        track = self._build_track(flight_id, object_row_id, source, event.time)
        if track is not None:
            session.add(track)
            self._pending += 1
        await self._flush()

    async def _handle_remove(self, event: ObjectRemoveEvent) -> None:
        object_row_id = self._object_row_ids.get(event.obj_id)
        if object_row_id is None or self._flight_id is None:
            return
        session = self._get_session()
        dcs_object = await session.get(DcsObject, object_row_id)
        if dcs_object is not None:
            dcs_object.removed = True
            dcs_object.last_seen = event.time
            self._pending += 1
            await self._flush()

    def _build_track(
        self,
        flight_id: int,
        object_row_id: int,
        source: AcmiObject | None,
        mission_time: float,
    ) -> Track | None:
        if not isinstance(source, AcmiObject):
            return None
        has_position = any(
            value is not None
            for value in (
                source.latitude,
                source.longitude,
                source.altitude,
                source.u,
                source.v,
            )
        )
        if not has_position:
            return None
        return Track(
            flight_id=flight_id,
            object_id=object_row_id,
            mission_time=mission_time,
            latitude=source.latitude,
            longitude=source.longitude,
            altitude=source.altitude,
            u=source.u,
            v=source.v,
            roll=source.roll,
            pitch=source.pitch,
            yaw=source.yaw,
            heading=source.heading,
            speed=source.speed,
            on_ground=source.on_ground,
            agl=source.agl,
            aoa=source.aoa,
        )
