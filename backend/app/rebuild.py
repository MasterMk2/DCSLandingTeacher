"""Re-cut a stored landing from the raw ``tracks`` table.

A landing row keeps only what the detector cut out on the day it was graded:
that day's approach window, in that day's frame. The raw samples behind it
stay in ``tracks`` -- every object update the ingestor saw -- so a landing can
be detected and graded again from scratch with the current code.

That is the only way a carrier trap stored before 2026-09-26 gets its
kiss-off. Its stored track is the last 60 s before the touchdown, measured
against a deck frozen at the touchdown instant with a starboard-angled deck
and Tacview's sea-referenced height; a regrade re-reads exactly that, so it
can neither add the break nor fix the frame.

The samples are rebuilt the way live ingest builds them -- ground speed from
:func:`app.ingest.ground_speed_from_history`, position jumps dropped by
:func:`app.ingest.reject_impossible_position`, every carrier of the flight as
:class:`CarrierState` fills it, the ground reference from the nearest ship or
static -- so a row that is already current re-cuts to the same result
(``tests/test_rebuild.py``).
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.detection.classify import ObjectClass, classify_object_type
from app.detection.detector import CarrierState, LandingEvent, TrackSample
from app.detection.geometry import haversine_m
from app.ingest import (
    GROUND_REFERENCE_RADIUS_M,
    ground_speed_from_history,
    reject_impossible_position,
)
from app.models.entities import DcsObject, Track

#: How far either side of the touchdown to read beyond the detector's own
#: windows: the ground-speed baseline needs up to 15 s of history before the
#: first sample of the approach, and the outcome needs the climb-out (or the
#: full-stop dwell) after the contact.
REBUILD_MARGIN_S = 60.0

#: A re-detected touchdown this close to the stored one is the same landing.
#: The detector can move it a sample or two -- a deck height that changed
#: from 19.5 to 20.15 m shifts the first sample inside the contact band --
#: but two different landings of one aircraft are minutes apart.
MATCH_TOLERANCE_S = 2.0


async def load_track_samples(
    session: AsyncSession, object_row_id: int, start: float, end: float
) -> list[TrackSample]:
    """The aircraft's samples in ``[start, end]``, as the live buffer held them."""
    rows = (
        await session.execute(
            select(
                Track.mission_time,
                Track.latitude,
                Track.longitude,
                Track.altitude,
                Track.agl,
                Track.speed,
                Track.heading,
                Track.aoa,
                Track.on_ground,
                Track.roll,
                Track.pitch,
            )
            .where(
                Track.object_id == object_row_id,
                Track.mission_time >= start,
                Track.mission_time <= end,
            )
            .order_by(Track.mission_time, Track.id)
        )
    ).all()
    samples: list[TrackSample] = []
    for row in rows:
        # Live ingest only buffers updates that carry a position.
        if row.latitude is None or row.longitude is None:
            continue
        speed = row.speed
        if speed is None:
            speed = ground_speed_from_history(
                reversed(samples), row.mission_time, row.latitude, row.longitude
            )
        sample = TrackSample(
            time=row.mission_time,
            latitude=row.latitude,
            longitude=row.longitude,
            altitude=row.altitude,
            agl=row.agl,
            speed=speed,
            heading=row.heading,
            aoa=row.aoa,
            on_ground=row.on_ground,
            roll=row.roll,
            pitch=row.pitch,
        )
        samples.append(reject_impossible_position(samples[-1] if samples else None, sample))
    return samples


async def load_carrier_state(
    session: AsyncSession, carrier_row_id: int, start: float, end: float
) -> CarrierState | None:
    """The ship's track in ``[start, end]``, as live ingest fills a CarrierState."""
    ship = await session.get(DcsObject, carrier_row_id)
    if ship is None:
        return None
    rows = (
        await session.execute(
            select(
                Track.mission_time,
                Track.latitude,
                Track.longitude,
                Track.altitude,
                Track.heading,
                Track.speed,
            )
            .where(
                Track.object_id == carrier_row_id,
                Track.mission_time >= start,
                Track.mission_time <= end,
            )
            .order_by(Track.mission_time, Track.id)
        )
    ).all()
    state = CarrierState(obj_id=ship.acmi_id, name=ship.name, type=ship.type)
    for row in rows:
        if row.latitude is None or row.longitude is None or row.altitude is None:
            continue
        state.append(
            (
                row.mission_time,
                row.latitude,
                row.longitude,
                row.altitude,
                row.heading or 0.0,
                row.speed or 0.0,
            )
        )
    return state if state.samples else None


async def load_flight_carriers(
    session: AsyncSession, flight_id: int, start: float, end: float
) -> dict[str, tuple[int, CarrierState]]:
    """Every carrier of the flight with samples in ``[start, end]``.

    Live ingest hands the detector all the carriers it is tracking, and the
    detector decides which deck (if any) is under each sample -- so the ship
    a row names is an output of the detection, not an input to it. Keyed by
    ACMI id like the live dict, with each ship's row id alongside so the row
    can be pointed at the ship the new detection chose.
    """
    ships = (
        await session.execute(
            select(DcsObject.id, DcsObject.type)
            # A superset of both carrier keywords (LIKE also ignores case);
            # the classifier has the last word.
            .where(DcsObject.flight_id == flight_id, DcsObject.type.like("%Carrier%"))
            .order_by(DcsObject.id)
        )
    ).all()
    carriers: dict[str, tuple[int, CarrierState]] = {}
    for ship in ships:
        if classify_object_type(ship.type) != ObjectClass.CARRIER:
            continue
        state = await load_carrier_state(session, ship.id, start, end)
        if state is not None:
            carriers[state.obj_id] = (ship.id, state)
    return carriers


async def ground_altitude_at(
    session: AsyncSession,
    flight_id: int,
    sample: TrackSample,
    carriers: Iterable[CarrierState],
) -> float | None:
    """What live ingest's ``_ground_altitude_for`` answers at ``sample``.

    The altitude of the nearest ship or static object within
    ``GROUND_REFERENCE_RADIUS_M``. The detector only falls back to it where
    a sample carries neither AGL nor OnGround -- a recording exported
    without AGL -- and there it is the only way to tell that the aircraft is
    on the ground. Live ingest asks at its newest sample on every pass; the
    rebuild asks once, at the stored touchdown.
    """
    best: float | None = None
    best_distance = GROUND_REFERENCE_RADIUS_M
    for state in carriers:
        position = state.position_at(sample.time)
        if position is None:
            continue
        distance = haversine_m(sample.latitude, sample.longitude, position[0], position[1])
        if distance <= best_distance:
            best_distance = distance
            best = state.altitude_at(sample.time)
    # Each static where live ingest last saw it before this instant.
    latest = (
        select(Track.object_id, func.max(Track.mission_time).label("time"))
        .join(DcsObject, DcsObject.id == Track.object_id)
        .where(
            DcsObject.flight_id == flight_id,
            DcsObject.type.like("%Static%"),
            Track.mission_time <= sample.time,
            Track.latitude.is_not(None),
            Track.longitude.is_not(None),
            Track.altitude.is_not(None),
        )
        .group_by(Track.object_id)
        .subquery()
    )
    statics = (
        await session.execute(
            select(DcsObject.type, Track.latitude, Track.longitude, Track.altitude)
            .join(
                latest,
                and_(
                    Track.object_id == latest.c.object_id,
                    Track.mission_time == latest.c.time,
                ),
            )
            .join(DcsObject, DcsObject.id == Track.object_id)
        )
    ).all()
    for static in statics:
        if classify_object_type(static.type) != ObjectClass.STATIC:
            continue
        distance = haversine_m(sample.latitude, sample.longitude, static.latitude, static.longitude)
        if distance <= best_distance:
            best_distance = distance
            best = static.altitude
    return best


def pick_event(
    events: list[LandingEvent], touchdown_time: float
) -> LandingEvent | None:
    """The re-detected event that is the stored landing, if there is one.

    A stored touchdown is the LAST contact of its bounce sequence, so it may
    sit anywhere from the event's first contact to its touchdown.
    """
    best: LandingEvent | None = None
    best_gap = float("inf")
    for event in events:
        if not (
            event.first_contact_time - MATCH_TOLERANCE_S
            <= touchdown_time
            <= event.touchdown.time + MATCH_TOLERANCE_S
        ):
            continue
        gap = abs(event.touchdown.time - touchdown_time)
        if gap < best_gap:
            best, best_gap = event, gap
    return best
