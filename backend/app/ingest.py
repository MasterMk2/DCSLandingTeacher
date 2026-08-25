"""Ingest pipeline: ACMI parser events -> ORM entities -> SQLite.

Lines are parsed incrementally and persisted in batches: a single session is
reused across events and committed once ``max_batch_size`` pending writes
have accumulated, ``max_batch_age_s`` seconds have passed since the batch's
first write, or on :meth:`close`. This avoids one transaction per telemetry
line while keeping memory bounded *and* bounding how long the write lock is
held: SQLite only allows one writer at a time, so a batch left open for
longer than another writer's ``busy_timeout`` (e.g. the ACMI file import,
Issue #18) makes that writer fail with "database is locked" even though it
never touches more than ``max_batch_size`` rows itself. The count-only
version of this trigger held the lock long enough to reproduce that under
light traffic (a handful of objects updating a few times a second easily
takes several seconds to reach 200 pending writes).

Landing detection (FR-2) hooks in here: aircraft samples are mirrored into
rolling buffers, carrier/static objects into lightweight state holders, and
:class:`app.detection.detector.analyze_track` runs after every update. New
landing events are forwarded to the optional ``landing_listener`` callback
(the grading pipeline).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from logging import getLogger

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.acmi.models import (
    AcmiObject,
    MissionEvent,
    ObjectRemoveEvent,
    ObjectUpdateEvent,
)
from app.acmi.parser import AcmiParseError, AcmiParser
from app.detection.classify import ObjectClass, classify_object_type
from app.detection.detector import (
    CarrierState,
    LandingEvent,
    RollingTrackBuffer,
    TrackSample,
    analyze_track,
)
from app.detection.geometry import haversine_m
from app.models.entities import DcsObject, Flight, Track

logger = getLogger(__name__)

#: Radius (m) within which a static object / carrier is used as the ground
#: elevation reference for WOW estimation.
GROUND_REFERENCE_RADIUS_M = 5000.0


@dataclass
class LandingContext:
    """Everything the grading pipeline needs about one detected landing.

    ``object_row_id`` / ``carrier_row_id`` carry the ORM row ids already
    resolved by the ingestor; the pipeline must not re-query them because
    the ingest transaction may not be committed yet.
    """

    flight_id: int | None
    acmi_object_id: str
    pilot: str | None
    airframe: str | None
    event: LandingEvent
    object_row_id: int | None = None
    carrier_row_id: int | None = None
    source_id: str = "default"
    flight_reference_time: str | None = None


#: Called for every newly detected landing. Returns the persisted row id so
#: the ingestor can correlate a later finalized event with the provisional
#: record (Issue #5 two-phase confirmation).
LandingListener = Callable[[LandingContext], Awaitable[int | None]]

#: Called when an already-reported provisional landing is settled; receives
#: the row id returned earlier by ``LandingListener``.
LandingFinalizeListener = Callable[[int, LandingContext], Awaitable[None]]


class TrackIngestor:
    """Consumes raw ACMI lines, parses them, and stores tracks in the DB."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        max_batch_size: int = 200,
        max_batch_age_s: float = 2.0,
        landing_listener: LandingListener | None = None,
        landing_finalize_listener: LandingFinalizeListener | None = None,
        sample_buffer_s: float = 600.0,
        source_id: str = "default",
    ) -> None:
        self._session_factory = session_factory
        self._max_batch_size = max(1, max_batch_size)
        self._max_batch_age_s = max_batch_age_s
        self._source_id = source_id
        self._parser = AcmiParser()
        self._flight_id: int | None = None
        #: acmi object id -> ``objects.id`` row
        self._object_row_ids: dict[str, int] = {}
        self._session: AsyncSession | None = None
        self._pending = 0
        #: monotonic timestamp of the current batch's first pending write.
        self._batch_opened_at: float | None = None

        # --- landing detection state (FR-2) -------------------------------
        self._landing_listener = landing_listener
        self._landing_finalize_listener = landing_finalize_listener
        self._sample_buffer_s = sample_buffer_s
        self._aircraft_buffers: dict[str, RollingTrackBuffer] = {}
        self._carrier_states: dict[str, CarrierState] = {}
        #: static object id -> (lat, lon, altitude)
        self._static_positions: dict[str, tuple[float, float, float]] = {}
        #: (acmi id, touchdown time) pairs already reported as final.
        self._reported_landings: set[tuple[str, float]] = set()
        #: (acmi id, touchdown time) -> landing row id reported provisionally
        #: and still awaiting its final outcome (Issue #5).
        self._provisional_ids: dict[tuple[str, float], int] = {}

    @property
    def parser(self) -> AcmiParser:
        return self._parser

    @property
    def carrier_states(self) -> dict[str, CarrierState]:
        return self._carrier_states

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
                logger.debug(
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
        # The session object is reused across commits (a commit ends the
        # transaction, not the session), so a new batch starting on an
        # *existing* session must still stamp its own opening time -
        # otherwise _batch_opened_at, cleared by the previous _flush(),
        # stays None forever and the age trigger only ever fires once.
        if self._pending == 0:
            self._batch_opened_at = time.monotonic()
        return self._session

    async def _flush(self, force: bool = False) -> None:
        if self._session is None:
            return
        batch_expired = (
            self._batch_opened_at is not None
            and time.monotonic() - self._batch_opened_at >= self._max_batch_age_s
        )
        if not force and self._pending < self._max_batch_size and not batch_expired:
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
            self._batch_opened_at = None

    async def _ensure_flight(self) -> int:
        if self._flight_id is not None:
            return self._flight_id
        header = self._parser.header
        session = self._get_session()
        flight = Flight(
            source_id=self._source_id,
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

        if source is not None:
            self._update_detection_state(event.obj_id, source)
            if classify_object_type(source.type) == ObjectClass.AIRCRAFT:
                await self._maybe_detect_landing(event.obj_id)

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
        # A disappearing aircraft may have been on deck: run a final detection
        # pass so end-of-track landings are still reported. The parser has
        # already dropped the object, so classify from the stored DB row.
        if dcs_object is not None and (
            classify_object_type(dcs_object.type) == ObjectClass.AIRCRAFT
        ):
            await self._maybe_detect_landing(event.obj_id, force_final=True)

    # ------------------------------------------------------------------
    # Landing detection hooks (FR-2)
    # ------------------------------------------------------------------

    def _update_detection_state(self, obj_id: str, source: AcmiObject) -> None:
        obj_class = classify_object_type(source.type)
        if obj_class == ObjectClass.CARRIER:
            state = self._carrier_states.setdefault(
                obj_id, CarrierState(obj_id, source.name, source.type)
            )
            state.name = source.name
            state.type = source.type
            if (
                source.latitude is not None
                and source.longitude is not None
                and source.altitude is not None
            ):
                sample = (
                    source.last_seen,
                    source.latitude,
                    source.longitude,
                    source.altitude,
                    source.heading or 0.0,
                    source.speed or 0.0,
                )
                if not state.samples or state.samples[-1][0] != sample[0]:
                    state.samples.append(sample)
        elif obj_class == ObjectClass.STATIC:
            if (
                source.latitude is not None
                and source.longitude is not None
                and source.altitude is not None
            ):
                self._static_positions[obj_id] = (
                    source.latitude,
                    source.longitude,
                    source.altitude,
                )
        elif obj_class == ObjectClass.AIRCRAFT:
            if source.latitude is not None and source.longitude is not None:
                self.record_aircraft_sample(
                    obj_id,
                    TrackSample(
                        time=source.last_seen,
                        latitude=source.latitude,
                        longitude=source.longitude,
                        altitude=source.altitude,
                        agl=source.agl,
                        speed=source.speed,
                        heading=source.heading,
                        aoa=source.aoa,
                        on_ground=source.on_ground,
                    ),
                )

    def _ground_altitude_for(
        self, latitude: float | None, longitude: float | None
    ) -> float | None:
        """Elevation of the nearest carrier deck / static within range."""
        best: float | None = None
        best_distance = GROUND_REFERENCE_RADIUS_M
        if latitude is None or longitude is None:
            return None
        for state in self._carrier_states.values():
            if not state.samples:
                continue
            t, lat, lon, alt, *_ = state.samples[-1]
            distance = haversine_m(latitude, longitude, lat, lon)
            if distance <= best_distance:
                best_distance = distance
                best = alt
        for lat, lon, alt in self._static_positions.values():
            distance = haversine_m(latitude, longitude, lat, lon)
            if distance <= best_distance:
                best_distance = distance
                best = alt
        return best

    async def _maybe_detect_landing(
        self, obj_id: str, *, force_final: bool = False
    ) -> None:
        buffer = self._aircraft_buffers.get(obj_id)
        if buffer is None:
            buffer = RollingTrackBuffer(self._sample_buffer_s)
            self._aircraft_buffers[obj_id] = buffer

        source = self._parser.objects.get(obj_id)
        samples = buffer.snapshot()
        if not samples:
            return
        last = samples[-1]
        ground_altitude = self._ground_altitude_for(last.latitude, last.longitude)

        events = analyze_track(
            samples,
            ground_altitude,
            self._carrier_states,
            current_time=None if force_final else last.time,
        )
        if not events:
            return

        # Landing events are rare; commit pending ingest writes first so the
        # grading pipeline can open its own write transaction without
        # hitting SQLite's single-writer lock. Doing this only when there is
        # actually an event keeps the common case (every aircraft update,
        # dozens of times a second with several AI aircraft in the mission)
        # from forcing a synchronous disk commit each time.
        await self._flush(force=True)
        for landing in events:
            key = (obj_id, round(landing.touchdown.time, 3))
            context = LandingContext(
                flight_id=self._flight_id,
                acmi_object_id=obj_id,
                pilot=source.pilot if source else None,
                airframe=source.name if source else None,
                event=landing,
                object_row_id=self._object_row_ids.get(obj_id),
                carrier_row_id=(
                    self._object_row_ids.get(landing.carrier_obj_id)
                    if landing.carrier_obj_id
                    else None
                ),
                source_id=self._source_id,
                flight_reference_time=self._parser.header.get("ReferenceTime"),
            )

            if not landing.finalized and not force_final:
                # Two-phase confirmation (Issue #5): report the touchdown
                # immediately as provisional; the final verdict arrives in a
                # later analysis pass once the dwell window has elapsed.
                if key in self._provisional_ids or key in self._reported_landings:
                    continue
                landing_id = await self._dispatch(context, provisional=True)
                if landing_id is not None:
                    self._provisional_ids[key] = landing_id
                continue

            pending_id = self._provisional_ids.pop(key, None)
            if pending_id is not None:
                self._reported_landings.add(key)
                await self._finalize(pending_id, context)
                continue
            if key in self._reported_landings:
                continue
            self._reported_landings.add(key)
            await self._dispatch(context, provisional=False)

    async def _dispatch(self, context: LandingContext, *, provisional: bool) -> int | None:
        event = context.event
        logger.info(
            "landing detected (%s): obj=%s kind=%s outcome=%s t=%.2f",
            "provisional" if provisional else "final",
            context.acmi_object_id,
            event.kind,
            event.outcome,
            event.touchdown.time,
        )
        if self._landing_listener is None:
            return None
        try:
            return await self._landing_listener(context)
        except Exception:
            logger.exception("landing listener failed")
            return None

    async def _finalize(self, landing_id: int, context: LandingContext) -> None:
        event = context.event
        logger.info(
            "landing finalized: id=%d obj=%s kind=%s outcome=%s t=%.2f",
            landing_id,
            context.acmi_object_id,
            event.kind,
            event.outcome,
            event.touchdown.time,
        )
        if self._landing_finalize_listener is None:
            return
        try:
            await self._landing_finalize_listener(landing_id, context)
        except Exception:
            logger.exception("landing finalize listener failed")

    def record_aircraft_sample(self, obj_id: str, sample: TrackSample) -> None:
        """Public hook so tests/hosts can feed detection buffers directly."""
        self._aircraft_buffers.setdefault(
            obj_id, RollingTrackBuffer(self._sample_buffer_s)
        ).append(sample)

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
