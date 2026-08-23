"""Minimal ingest pipeline: ACMI parser events -> ORM entities -> SQLite.

Phase 1 scope. Each parsed line is persisted immediately; batching and
bulk inserts are left for a later optimization pass.
"""

from __future__ import annotations

from logging import getLogger

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.acmi.models import AcmiObject, ObjectRemoveEvent, ObjectUpdateEvent
from app.acmi.parser import AcmiParseError, AcmiParser
from app.models.entities import DcsObject, Flight, Track

logger = getLogger(__name__)


class TrackIngestor:
    """Consumes raw ACMI lines, parses them, and stores tracks in the DB."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._parser = AcmiParser()
        self._flight_id: int | None = None
        #: acmi object id -> ``objects.id`` row
        self._object_row_ids: dict[str, int] = {}

    @property
    def parser(self) -> AcmiParser:
        return self._parser

    async def handle_line(self, line: str) -> None:
        """Process one raw line coming from the stream client."""
        try:
            event = self._parser.feed_line(line)
        except AcmiParseError as exc:
            logger.warning("skipping unparsable ACMI line: %s", exc)
            return

        if isinstance(event, ObjectUpdateEvent):
            await self._handle_update(event)
        elif isinstance(event, ObjectRemoveEvent):
            await self._handle_remove(event)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _ensure_flight(self) -> int:
        if self._flight_id is not None:
            return self._flight_id
        header = self._parser.header
        async with self._session_factory() as session:
            flight = Flight(
                reference_time=header.get("ReferenceTime"),
                data_source=header.get("DataSource"),
                data_recorder=header.get("DataRecorder"),
                title=header.get("Title"),
                theater=header.get("Theater"),
            )
            session.add(flight)
            await session.commit()
            self._flight_id = flight.id
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

        async with self._session_factory() as session:
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
            await session.commit()

    async def _handle_remove(self, event: ObjectRemoveEvent) -> None:
        object_row_id = self._object_row_ids.get(event.obj_id)
        if object_row_id is None or self._flight_id is None:
            return
        async with self._session_factory() as session:
            dcs_object = await session.get(DcsObject, object_row_id)
            if dcs_object is not None:
                dcs_object.removed = True
                dcs_object.last_seen = event.time
                await session.commit()

    def _build_track(
        self,
        flight_id: int,
        object_row_id: int,
        source: object | None,
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
