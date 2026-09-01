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
:class:`app.detection.detector.analyze_track` runs when the detection state
can actually have changed (Issue #47): a fresh airborne -> on-deck contact,
a still-unfinalized landing, or a forced final pass. Running the full
buffer scan on *every* update line made long recordings quadratic -- a
cruising aircraft contributed a full O(n log n) re-analysis per update for
nothing, since no new touchdown is possible while the newest sample is
airborne. New landing events are forwarded to the optional
``landing_listener`` callback (the grading pipeline).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from logging import getLogger
from typing import Any

from sqlalchemy import bindparam, insert, select
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
    DetectionConfig,
    LandingEvent,
    RollingTrackBuffer,
    TrackSample,
    analyze_track,
    is_on_deck,
)
from app.detection.geometry import haversine_m
from app.models.entities import DcsObject, Flight, Track

logger = getLogger(__name__)

#: Radius (m) within which a static object / carrier is used as the ground
#: elevation reference for WOW estimation.
GROUND_REFERENCE_RADIUS_M = 5000.0

#: Default maximum age of a buffered ACMI batch before it is flushed (Issue #43).
DEFAULT_BATCH_AGE_S = 2.0

#: A mission-time jump backwards by more than this many seconds means the
#: exporter restarted its session: a DCS mission restart resets the ACMI
#: frame clock to zero while this ingestor keeps running. Everything keyed
#: on mission time (flight row, object rows, rolling buffers, dedup keys)
#: describes the previous mission and has to be rotated (Issue #48 follow-up:
#: duplicates like landings #280/#285/#288 and cross-mission finalize
#: corruption). Small negative jitter from buffering stays well below this.
SESSION_REGRESSION_S = 120.0


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
        max_batch_age_s: float = DEFAULT_BATCH_AGE_S,
        landing_listener: LandingListener | None = None,
        landing_finalize_listener: LandingFinalizeListener | None = None,
        sample_buffer_s: float = 600.0,
        source_id: str = "default",
        detection_config: DetectionConfig | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._max_batch_size = max(1, max_batch_size)
        self._max_batch_age_s = max_batch_age_s
        self._source_id = source_id
        self._parser = AcmiParser()
        self._flight_id: int | None = None
        #: acmi object id -> ``objects.id`` row
        self._object_row_ids: dict[str, int] = {}
        #: acmi object id -> identity fields already persisted for that row.
        #: Updating type/name/pilot/... on every update line made each line a
        #: SELECT + ORM UPDATE; tracking the last persisted tuple means the
        #: row is only touched when identity actually changes (Issue #47).
        self._object_meta: dict[str, tuple[str | None, ...]] = {}
        #: ``objects.id`` -> last seen time to be stamped at the next flush.
        #: ``last_seen`` changes on every line, but per-batch accuracy is
        #: plenty for a "when was this seen last" column; deferring turns one
        #: UPDATE per line into one per touched object per batch (Issue #47).
        self._dirty_last_seen: dict[int, float] = {}
        self._session: AsyncSession | None = None
        self._pending = 0
        #: Track samples of the open batch, as plain column dicts. They go to
        #: the DB as a single executemany INSERT at flush time (Issue #47)
        #: instead of one ORM instance per line.
        self._pending_tracks: list[dict[str, Any]] = []
        #: monotonic timestamp of the current batch's first pending write.
        self._batch_opened_at: float | None = None

        # --- landing detection state (FR-2) -------------------------------
        self._landing_listener = landing_listener
        self._landing_finalize_listener = landing_finalize_listener
        self._sample_buffer_s = sample_buffer_s
        self._detection_config = detection_config or DetectionConfig()
        self._aircraft_buffers: dict[str, RollingTrackBuffer] = {}
        self._carrier_states: dict[str, CarrierState] = {}
        #: static object id -> (lat, lon, altitude)
        self._static_positions: dict[str, tuple[float, float, float]] = {}
        #: (acmi id, touchdown time) pairs already reported as final.
        self._reported_landings: set[tuple[str, float]] = set()
        #: (acmi id, touchdown time) -> landing row id reported provisionally
        #: and still awaiting its final outcome (Issue #5).
        self._provisional_ids: dict[tuple[str, float], int] = {}
        #: Aircraft with provisional events outstanding: the detection pass
        #: keeps running for these until they finalize (Issue #47 gate).
        self._provisional_aircraft: set[str] = set()
        #: Newest sample's WOW estimate per aircraft; a transition to
        #: on-deck is what can create a new touchdown candidate.
        self._last_wow: dict[str, bool | None] = {}
        #: Highest mission time seen in the current ACMI session.
        self._session_time_high: float | None = None
        #: (ReferenceTime, RecordingTime) of the current ACMI session.
        #: The exporter re-sends both in a fresh global object line whenever
        #: its session restarts. ReferenceTime alone is the .miz in-game date
        #: and is identical across sessions of the same mission; RecordingTime
        #: is when the recording session started and is what actually
        #: distinguishes them.
        self._session_signature: tuple[str, str] | None = None

    @property
    def parser(self) -> AcmiParser:
        return self._parser

    @property
    def carrier_states(self) -> dict[str, CarrierState]:
        return self._carrier_states

    async def handle_line(self, line: str) -> None:
        """Process one raw line coming from the stream client.

        ``feed_line`` is called once per ACMI line and is deliberately NOT
        offloaded to a worker thread (Issue #31 proposed ``asyncio.to_thread``
        here). One line is microseconds of pure-Python parsing, while a
        ``to_thread`` hop costs tens of microseconds of scheduling and, because
        each call is awaited before the next, buys no parallelism -- and the
        GIL would prevent it even if it did. On a 4.8 M-line recording that is
        millions of round trips added to an import that is already the slowest
        path in the system. The event loop is yielded instead on a line count
        by the caller (see ``ImportJobManager._process``), which is where the
        latency actually needs bounding.
        """
        try:
            events = self._parser.feed_line(line)
        except AcmiParseError as exc:
            logger.warning("skipping unparsable ACMI line: %s", exc)
            return

        try:
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
        except Exception:
            # Any write failure has to take the session with it, not just a
            # failure at commit time. The explicit session.flush() calls that
            # fetch row ids for foreign keys raise *before* _flush() is ever
            # reached, and SQLAlchemy deactivates the transaction on a failed
            # flush -- so leaving the session in place makes every subsequent
            # line raise PendingRollbackError, including close(). The live
            # source's supervisor catches that, sleeps a second and calls
            # run() again on the same ingestor, so a single "database is
            # locked" would otherwise wedge ingestion into a permanent
            # reconnect loop that stores nothing until the container restarts.
            await self._discard_session()
            raise

    async def flush(self) -> None:
        """Commit the open batch and release SQLite's write lock.

        A caller that is driving this ingestor in a loop and wants to write to
        the same database from another session has to call this first. The
        batch may already hold the write lock -- ``_ensure_flight`` and the
        new-object path both ``session.flush()`` to obtain row ids for foreign
        keys -- and nothing will commit it while the caller is awaiting its own
        write, so the second writer would sit out its ``busy_timeout`` and fail
        with "database is locked".
        """
        await self._flush(force=True)

    async def close(self) -> None:
        """Flush pending writes and release the session."""
        await self._flush(force=True)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_session(self) -> AsyncSession:
        """Create a new short-lived session for the current batch.

        Using a new session per batch (instead of reusing one across commits)
        ensures the SQLite write lock is released between batches. This allows
        concurrent writers (API / import / regrade) to acquire the lock during
        the gap between ingest batches.
        """
        if self._session is None:
            self._session = self._session_factory(autoflush=False)
            self._pending = 0
            self._batch_opened_at = time.monotonic()
        return self._session

    async def _discard_session(self) -> None:
        """Drop the current batch after a write failure; never raises."""
        session, self._session = self._session, None
        self._pending = 0
        self._batch_opened_at = None
        # The batch's samples were never committed; dropping them matches the
        # pre-#47 ORM behaviour where the discarded session took its pending
        # objects with it.
        self._pending_tracks.clear()
        if session is None:
            return
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001 - already unwinding another error
            logger.debug("rollback failed while discarding batch", exc_info=True)
        try:
            await session.close()
        except Exception:  # noqa: BLE001 - same
            logger.debug("close failed while discarding batch", exc_info=True)

    async def _flush(self, force: bool = False) -> None:
        if self._session is None:
            return
        batch_expired = (
            self._batch_opened_at is not None
            and time.monotonic() - self._batch_opened_at >= self._max_batch_age_s
        )
        if not force and self._pending < self._max_batch_size and not batch_expired:
            return
        # Detach the session before touching it, so the cleanup below cannot be
        # confused by a partially torn-down state. Closing it releases SQLite's
        # write lock immediately; the next batch gets a fresh one from
        # _get_session().
        #
        # The cleanup deliberately does not run through a `finally` that reads
        # self._session: an earlier revision cleared self._session in the
        # `except` branch and then dereferenced it again in `finally`, so a
        # commit failure surfaced as `AttributeError: 'NoneType' object has no
        # attribute 'close'` and the real error was discarded. "database is
        # locked" is precisely the failure this batching scheme exists to
        # avoid, so it has to arrive at the caller as itself.
        session, self._session = self._session, None
        self._pending = 0
        self._batch_opened_at = None
        dirty_last_seen = self._dirty_last_seen
        pending_tracks = self._pending_tracks
        try:
            # Track samples as one executemany INSERT instead of one ORM
            # object per line (Issue #47): the ORM unit-of-work state per
            # row cost ~0.5 ms per sample on a 275k-line recording, which
            # dominated everything else once the detection gate removed the
            # quadratic scan.
            if pending_tracks:
                self._pending_tracks = []
                await session.execute(insert(Track), pending_tracks)
            # Deferred object last_seen stamps (Issue #47): one executemany
            # UPDATE for every touched object of the batch instead of one
            # statement per update line. Built on the Core table object: the
            # ORM-enabled update() would route executemany into its bulk
            # semantics, which demand per-row primary keys in the params.
            if dirty_last_seen:
                objects_table = DcsObject.__table__
                await session.execute(
                    objects_table.update()
                    .where(objects_table.c.id == bindparam("b_row_id"))
                    .values(last_seen=bindparam("last_seen")),
                    [
                        {"b_row_id": row_id, "last_seen": seen_at}
                        for row_id, seen_at in dirty_last_seen.items()
                    ],
                )
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        else:
            dirty_last_seen.clear()
        finally:
            await session.close()

    async def _ensure_flight(self) -> int:
        if self._flight_id is not None:
            return self._flight_id
        header = self._parser.header
        session = self._get_session()
        flight = Flight(
            source_id=self._source_id,
            reference_time=header.get("ReferenceTime"),
            recording_time=header.get("RecordingTime"),
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
        await self._maybe_rotate_session(event)
        if event.obj_id == "0":
            # Global object carries mission metadata only; the flight row is
            # created lazily on the first real object update so that header
            # values have already arrived.
            return

        flight_id = await self._ensure_flight()
        source = self._parser.objects.get(event.obj_id)

        session = self._get_session()
        object_row_id = self._object_row_ids.get(event.obj_id)
        meta = (
            (source.type, source.name, source.pilot, source.group, source.country)
            if source is not None
            else None
        )
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
                    type=meta[0] if meta else None,
                    name=meta[1] if meta else None,
                    pilot=meta[2] if meta else None,
                    group_name=meta[3] if meta else None,
                    country=meta[4] if meta else None,
                )
                session.add(dcs_object)
                await session.flush()
            elif source is not None:
                # Row left over from an earlier ingestor for this flight:
                # refresh its identity once, like the pre-#47 code did.
                dcs_object.type = meta[0]
                dcs_object.name = meta[1]
                dcs_object.pilot = meta[2]
                dcs_object.group_name = meta[3]
                dcs_object.country = meta[4]
                dcs_object.last_seen = source.last_seen
            object_row_id = dcs_object.id
            self._object_row_ids[event.obj_id] = object_row_id
            self._object_meta[event.obj_id] = meta
        elif source is not None and meta != self._object_meta.get(event.obj_id):
            # Identity changed (renamed unit, slot handover, ...): rare, so a
            # SELECT + ORM update is affordable here. The old per-line
            # session.get() was the steady-state path and dominated the
            # ingest's DB round trips (Issue #47).
            dcs_object = await session.get(DcsObject, object_row_id)
            if dcs_object is not None:
                dcs_object.type = meta[0]
                dcs_object.name = meta[1]
                dcs_object.pilot = meta[2]
                dcs_object.group_name = meta[3]
                dcs_object.country = meta[4]
                dcs_object.last_seen = source.last_seen
            self._object_meta[event.obj_id] = meta
        elif source is not None:
            # Unchanged identity: defer the last_seen stamp to the batch
            # flush (Issue #47).
            self._dirty_last_seen[object_row_id] = source.last_seen

        track = self._build_track(flight_id, object_row_id, source, event.time)
        if track is not None:
            self._pending_tracks.append(track)
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
    # Session rotation (mission restart handling)
    # ------------------------------------------------------------------

    async def _maybe_rotate_session(self, event: ObjectUpdateEvent) -> None:
        """Start a new ingest session when the ACMI timeline restarts.

        Two signals, either of which is sufficient:

        - The global object re-announces a different ``RecordingTime`` (or
          ``ReferenceTime``): the exporter began a new recording session --
          a DCS mission restart re-sends the header block. ReferenceTime is
          NOT enough on its own: it is the mission file's in-game date and
          stays identical across restarts of the same .miz (verified on a
          live server: 50 flight rows, one ReferenceTime).
        - Mission time regresses by more than ``SESSION_REGRESSION_S``:
          catches exporters that restart the frame clock without re-sending
          the header.

        Without the rotation, the previous mission's state leaks into the
        new one: frozen rolling buffers kept the old mission's landings
        alive for hours (re-detected as duplicate rows whenever the object
        id was recycled), stale provisional keys were popped by the new
        mission's events (rows overwritten with a foreign touchdown), and
        object rows changed pilot/type mid-life. Reported-landing keys are
        deliberately kept across rotations: they are keyed by
        (obj id, first contact time), so a stale entry can only ever
        suppress a re-detection, never a new event.
        """
        header = self._parser.header
        recording_time = header.get("RecordingTime")
        rotated = False
        if recording_time:
            signature = (header.get("ReferenceTime") or "", recording_time)
            if self._session_signature is not None and signature != self._session_signature:
                await self._rotate_session(
                    f"session signature changed {self._session_signature} -> {signature}"
                )
                rotated = True
            self._session_signature = signature
        if (
            not rotated
            and self._session_time_high is not None
            and event.time < self._session_time_high - SESSION_REGRESSION_S
        ):
            await self._rotate_session(
                f"mission time regressed from t={self._session_time_high:.2f} to t={event.time:.2f}"
            )
            rotated = True
        if rotated:
            # Re-seed from the next (new-session) update: this event's time
            # still belongs to whichever side of the restart it arrived on.
            self._session_time_high = None
        elif self._session_time_high is None or event.time > self._session_time_high:
            self._session_time_high = event.time

    async def _rotate_session(self, reason: str) -> None:
        """Flush the old session and drop every per-session detection state.

        The pending batch belongs to the previous mission and is committed
        first. Object rows are NOT reused across the rotation even for the
        same ACMI id: ids are recycled by the next mission (a "503" can be
        an F/A-18 in one mission and an Apache in the next), so the new
        session gets fresh rows with correct identity and first_seen.
        """
        logger.warning("ACMI session restart detected (%s); rotating", reason)
        await self._flush(force=True)
        self._flight_id = None
        self._object_row_ids.clear()
        self._object_meta.clear()
        self._dirty_last_seen.clear()
        self._aircraft_buffers.clear()
        self._carrier_states.clear()
        self._static_positions.clear()
        self._last_wow.clear()
        self._provisional_aircraft.clear()

    # ------------------------------------------------------------------
    # Landing detection hooks (FR-2)
    # ------------------------------------------------------------------

    def _update_detection_state(self, obj_id: str, source: AcmiObject) -> None:
        obj_class = classify_object_type(source.type)
        if obj_class == ObjectClass.CARRIER:
            state = self._carrier_states.setdefault(
                obj_id,
                CarrierState(
                    obj_id,
                    source.name,
                    source.type,
                    max_age_s=self._sample_buffer_s,
                ),
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
                state.append(sample)
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
                        speed=self._ground_speed_ms(obj_id, source),
                        heading=source.heading,
                        aoa=source.aoa,
                        on_ground=source.on_ground,
                    ),
                )

    #: Minimum baseline (s) for the two-point ground-speed estimate below.
    #: ACMI partial updates that omit T=lon|lat frequently repeat the last
    #: known position verbatim (spec-correct "unchanged" semantics), so the
    #: *immediately* preceding buffered sample is often a near-duplicate of
    #: the current one. Differentiating against it divides a near-zero
    #: distance by a near-zero dt, which reads as exactly 0 m/s on one pair
    #: and spikes into the thousands on the next (observed live: 1364 m/s).
    #: Matches the existing pattern used for vertical speed
    #: (_vertical_speed / _descent_rate_before below) instead of a fixed
    #: 0.05s jitter guard.
    GROUND_SPEED_MIN_BASELINE_S = 1.0
    #: A baseline older than this is too stale to trust: the aircraft may have
    #: travelled a very different path in between, so the resulting speed is
    #: noise rather than a measurement (observed live: 1364 m/s spikes). This
    #: bounds a previously unbounded lookback that could reach minutes back.
    GROUND_SPEED_MAX_BASELINE_S = 15.0
    #: Below this horizontal displacement over the baseline the estimate is
    #: dominated by ACMI partial-update duplication (the last known position
    #: repeated verbatim) and quantization noise, not real motion.
    GROUND_SPEED_MIN_DISTANCE_M = 5.0
    #: Above this ground speed the estimate is implausible for crewed aircraft
    #: and must be discarded instead of skewing FAST/SLOW factor detection.
    GROUND_SPEED_MAX_PLAUSIBLE_MS = 1000.0

    def _ground_speed_ms(self, obj_id: str, source: AcmiObject) -> float | None:
        """Best-available horizontal speed in m/s (Issue D-2 / #27).

        DCS's own Tacview export never emits ``TAS``/``CAS``/``IAS`` (verified
        against a live server: real aircraft object lines carry only
        position/attitude/identity properties), so ``AcmiObject.speed`` is
        always ``None`` for real games. Fall back to a ground speed estimate
        against this aircraft's own position from at least
        ``GROUND_SPEED_MIN_BASELINE_S`` seconds ago, walking further back in
        the buffer as needed -- but never past ``GROUND_SPEED_MAX_BASELINE_S``
        seconds of stale data (Issue #27), and only when the displacement is
        large enough to be real motion.
        """
        if source.speed is not None:
            return source.speed
        if source.latitude is None or source.longitude is None:
            return None
        buffer = self._aircraft_buffers.get(obj_id)
        if buffer is None:
            return None
        for previous in buffer.iter_reverse():
            if previous.latitude is None or previous.longitude is None:
                continue
            dt = source.last_seen - previous.time
            if dt < self.GROUND_SPEED_MIN_BASELINE_S:
                continue
            if dt > self.GROUND_SPEED_MAX_BASELINE_S:
                # Everything further back is even staler; no usable baseline.
                return None
            distance = haversine_m(
                previous.latitude, previous.longitude, source.latitude, source.longitude
            )
            if distance < self.GROUND_SPEED_MIN_DISTANCE_M:
                # ACMI repeats the last position on partial updates; a tiny
                # delta is noise, not a standstill -- keep walking back within
                # the fresh window for a sample that actually moved.
                continue
            speed = distance / dt
            if speed > self.GROUND_SPEED_MAX_PLAUSIBLE_MS:
                return None
            return speed
        return None

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
            return
        last = buffer.last()
        if last is None:
            return

        # --- detection gate (Issue #47) ------------------------------------
        # analyze_track re-scans and re-sorts the whole rolling buffer, so
        # running it on every update line made long recordings quadratic:
        # an aircraft cruising at altitude contributed a full O(n log n)
        # pass per update that could never find anything, because a
        # candidate touchdown requires an airborne -> on-deck transition.
        # Estimate WOW for the newest sample -- O(1), without copying the
        # buffer -- and only run the full pass when it just became on-deck
        # (fresh contact), when the aircraft still has provisional events to
        # finalize (bounce merging / two-phase confirmation, Issue #5), or
        # when forced. The ground reference is only needed when the sample
        # itself cannot answer the WOW question, so skip the haversine walk
        # otherwise.
        if last.on_ground is None and last.agl is None:
            gate_ground_altitude = self._ground_altitude_for(
                last.latitude, last.longitude
            )
        else:
            gate_ground_altitude = None
        wow_now = is_on_deck(last, gate_ground_altitude, self._detection_config)
        wow_before = self._last_wow.get(obj_id)
        self._last_wow[obj_id] = wow_now
        new_contact = wow_before is not True and wow_now is True
        if (
            not force_final
            and not new_contact
            and obj_id not in self._provisional_aircraft
        ):
            return

        # Full pass: snapshot only now, after the gate admitted this update.
        samples = buffer.snapshot()
        # The whole buffer is re-analysed with the current ground reference,
        # so always recompute it here even if the gate skipped it.
        ground_altitude = self._ground_altitude_for(last.latitude, last.longitude)

        events = analyze_track(
            samples,
            ground_altitude,
            self._carrier_states,
            config=self._detection_config,
            current_time=None if force_final else last.time,
        )
        if not events:
            self._settle_provisional_state(obj_id)
            return

        # Landing events are rare; commit pending ingest writes first so the
        # grading pipeline can open its own write transaction without
        # hitting SQLite's single-writer lock. Doing this only when there is
        # actually an event keeps the common case (every aircraft update,
        # dozens of times a second with several AI aircraft in the mission)
        # from forcing a synchronous disk commit each time.
        await self._flush(force=True)
        source = self._parser.objects.get(obj_id)
        for landing in events:
            # Key on the first ground contact, not the touchdown: the
            # touchdown is the *last* contact of a merged bounce sequence and
            # therefore walks forward as the live buffer grows and further
            # bounces are absorbed. Keying on it made every bounce look like
            # a brand-new landing (one bouncy arrival produced three rows)
            # and orphaned the earlier provisional rows at "provisional"
            # forever, because their key never came back to be popped.
            # Use the full-precision timestamp (not a rounded value): rounding
            # to 1 ms can split the provisional and final keys of the same
            # landing when floating-point representation differs by a hair,
            # defeating the two-phase correlation (Issue #30).
            key = (obj_id, landing.first_contact_time)
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
                    self._provisional_aircraft.add(obj_id)
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

        self._settle_provisional_state(obj_id)

    def _settle_provisional_state(self, obj_id: str) -> None:
        """Stop re-running detection for ``obj_id`` once nothing is pending.

        The detection gate keeps the full pass alive for aircraft with
        provisional events; once every pending key has been finalized (or
        vanished), there is nothing left to re-classify until the next
        on-deck transition, so the aircraft can leave the gate again.
        """
        if obj_id not in self._provisional_aircraft:
            return
        if any(key[0] == obj_id for key in self._provisional_ids):
            return
        self._provisional_aircraft.discard(obj_id)

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
        buffer = self._aircraft_buffers.setdefault(
            obj_id, RollingTrackBuffer(self._sample_buffer_s)
        )
        sample = self._reject_impossible_position(obj_id, buffer, sample)
        buffer.append(sample)

    #: Above this implied speed between consecutive position samples the new
    #: position is physically impossible for a crewed aircraft and its
    #: coordinates are discarded (alt/AGL/speed/heading are kept). A corrupted
    #: recording (observed live: a TacView client capture whose lat/lon cycled
    #: across the whole map while altitude stayed coherent) otherwise wrecks
    #: every chart and grading built on the track. The rejected sample still
    #: becomes the next baseline, so a legitimate teleport (respawn) costs one
    #: rejected sample instead of poisoning the rest of the track.
    POSITION_JUMP_SPEED_MS = 1000.0

    def _reject_impossible_position(
        self, obj_id: str, buffer: RollingTrackBuffer, sample: TrackSample
    ) -> TrackSample:
        last = buffer.last()
        if (
            last is None
            or sample.latitude is None
            or sample.longitude is None
            or last.latitude is None
            or last.longitude is None
        ):
            return sample
        dt = sample.time - last.time
        if dt <= 0:
            return sample
        speed = (
            haversine_m(last.latitude, last.longitude, sample.latitude, sample.longitude)
            / dt
        )
        if speed <= self.POSITION_JUMP_SPEED_MS:
            return sample
        logger.debug(
            "position jump rejected: obj=%s %.0f m/s over %.2fs",
            obj_id,
            speed,
            dt,
        )
        return replace(sample, latitude=None, longitude=None)

    def _build_track(
        self,
        flight_id: int,
        object_row_id: int,
        source: AcmiObject | None,
        mission_time: float,
    ) -> dict[str, Any] | None:
        """One track sample as a plain column dict for executemany INSERT.

        The ORM ``Track`` instance was dropped for the bulk write path
        (Issue #47): samples arrive at tens of lines per second and the ORM
        unit-of-work bookkeeping per instance outweighed the SQL itself.
        """
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
        return {
            "flight_id": flight_id,
            "object_id": object_row_id,
            "mission_time": mission_time,
            "latitude": source.latitude,
            "longitude": source.longitude,
            "altitude": source.altitude,
            "u": source.u,
            "v": source.v,
            "roll": source.roll,
            "pitch": source.pitch,
            "yaw": source.yaw,
            "heading": source.heading,
            "speed": source.speed,
            "on_ground": source.on_ground,
            "agl": source.agl,
            "aoa": source.aoa,
        }
