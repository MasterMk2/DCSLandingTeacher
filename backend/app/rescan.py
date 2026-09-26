"""Find the landings old raw tracks hold but the database never stored.

Until the ingestor started keeping only landing windows (app/retention.py),
``tracks`` held every sample of every object. Before that history is
compacted down to the windows, it can be searched once more with today's
detector: carrier traps before 2026-09-06 were never detected at all, and
anything a since-fixed detector bug or a failed listener dropped is in there
too. Once compacted, it cannot.

For each aircraft of a flight the whole raw track is read back and walked
with live ingest's own gate (:func:`app.ingest.gate_wow`) to find the ground
contacts live ingest would have analysed. Each contact is re-detected with
the rebuild's routine (:func:`app.rebuild.redetect`), then re-cut once more
around the touchdown it found, so a landing added here is exactly what a
rebuild of it would produce. Landings already stored for the aircraft at
that time are left alone; the rest are graded and stored like a live
detection, without the broadcast.
"""

from __future__ import annotations

import asyncio
import time
from bisect import bisect_right
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import chain
from logging import getLogger
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.detection.classify import ObjectClass, classify_object_type
from app.detection.detector import CarrierState, DetectionConfig, LandingEvent, TrackSample
from app.ingest import LandingContext, deck_altitude_under, gate_wow, nearest_ground_reference
from app.models.entities import DcsObject, Flight, Landing, Track
from app.rebuild import (
    MATCH_TOLERANCE_S,
    fetch_track_rows,
    load_flight_carriers,
    redetect,
    samples_from_rows,
)

logger = getLogger(__name__)


class FlightNotFound(LookupError):
    pass


@dataclass
class FoundLanding:
    """One landing the rescan detected in the raw track."""

    object_id: int
    acmi_id: str
    pilot: str | None
    airframe: str | None
    kind: str
    outcome: str
    first_contact_time: float
    touchdown_time: float
    carrier_name: str | None
    latitude: float
    longitude: float
    #: The stored row this landing already is, if any.
    existing_landing_id: int | None = None
    #: The row the rescan created for it (``apply`` only).
    created_landing_id: int | None = None


@dataclass
class FlightRescan:
    flight_id: int
    source_id: str | None
    applied: bool
    aircraft_scanned: int = 0
    samples_scanned: int = 0
    contacts: int = 0
    landings: list[FoundLanding] = field(default_factory=list)
    #: Stored landings of the flight that no rescanned contact matched --
    #: reported, never touched (e.g. the old "carrier landings" that were
    #: objects hitting the water).
    stored_not_redetected: list[int] = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def new_landings(self) -> list[FoundLanding]:
        return [f for f in self.landings if f.existing_landing_id is None]


class _StaticHistory:
    """Where one static stood over time, answering "last seen before t"."""

    def __init__(self) -> None:
        self.times: list[float] = []
        self.positions: list[tuple[float, float, float]] = []

    def at(self, time_s: float) -> tuple[float, float, float] | None:
        index = bisect_right(self.times, time_s) - 1
        return self.positions[index] if index >= 0 else None


def _sample_time(sample: tuple[float, ...]) -> float:
    return sample[0]


def _carrier_position_before(
    state: CarrierState, time_s: float
) -> tuple[float, float, float] | None:
    """The ship's last sample at or before ``time_s``, as live ingest held it."""
    index = bisect_right(state.samples, time_s, key=_sample_time) - 1
    return state.samples[index][1:4] if index >= 0 else None


def find_contacts(
    samples: list[TrackSample],
    carriers: list[CarrierState],
    statics: list[_StaticHistory],
    config: DetectionConfig,
    deck_altitude_for: Callable[[CarrierState], float | None] | None,
) -> list[float]:
    """Times at which live ingest's gate would have seen a fresh ground contact.

    The gate's own question asked the gate's own way, sample by sample, with
    the ships and statics where live ingest would have had them at that
    instant. Pure CPU; the caller runs it off the event loop.
    """
    decks: dict[str, float | None] = {}

    def cached_deck(carrier: CarrierState) -> float | None:
        # The geometry lookup matches patterns; once per ship is enough.
        if carrier.obj_id not in decks:
            decks[carrier.obj_id] = (
                deck_altitude_for(carrier) if deck_altitude_for is not None else None
            )
        return decks[carrier.obj_id]

    contacts: list[float] = []
    wow_before: bool | None = None
    for sample in samples:
        deck = (
            deck_altitude_under(
                sample, carriers, cached_deck, config.carrier_proximity_m
            )
            if carriers and deck_altitude_for is not None
            else None
        )

        def ground_reference(sample: TrackSample = sample) -> float | None:
            positions: Iterable[tuple[float, float, float] | None] = chain(
                (_carrier_position_before(state, sample.time) for state in carriers),
                (static.at(sample.time) for static in statics),
            )
            return nearest_ground_reference(
                sample.latitude,
                sample.longitude,
                (p for p in positions if p is not None),
            )

        wow = gate_wow(sample, deck, ground_reference, config)
        if wow_before is not True and wow is True:
            contacts.append(sample.time)
        wow_before = wow
    return contacts


async def _load_statics(session: AsyncSession, flight_id: int) -> list[_StaticHistory]:
    rows = (
        await session.execute(
            select(DcsObject.id, DcsObject.type, Track.mission_time, Track.latitude,
                   Track.longitude, Track.altitude)
            .join(Track, Track.object_id == DcsObject.id)
            .where(
                DcsObject.flight_id == flight_id,
                DcsObject.type.like("%Static%"),
                Track.latitude.is_not(None),
                Track.longitude.is_not(None),
                Track.altitude.is_not(None),
            )
            .order_by(DcsObject.id, Track.mission_time, Track.id)
        )
    ).all()
    histories: dict[int, _StaticHistory] = {}
    for row in rows:
        if classify_object_type(row.type) != ObjectClass.STATIC:
            continue
        history = histories.setdefault(row.id, _StaticHistory())
        history.times.append(row.mission_time)
        history.positions.append((row.latitude, row.longitude, row.altitude))
    return list(histories.values())


def _estimated_created_at(
    flight: Flight, first_seen: float | None, touchdown_time: float
) -> datetime | None:
    """When a live detection would have stamped this landing.

    The flight row is created at the first object update of its session,
    i.e. at mission time ``first_seen``; a mission runs in real time, so the
    touchdown is that far after it. An estimate -- the column the list shows
    and sorts by just must not claim the landing happened today.
    """
    if flight.created_at is None or first_seen is None:
        return None
    return flight.created_at + timedelta(seconds=max(0.0, touchdown_time - first_seen))


async def rescan_flight(
    session_factory: async_sessionmaker[AsyncSession],
    pipeline: Any,
    flight_id: int,
    *,
    apply: bool,
) -> FlightRescan:
    """Search one flight's raw tracks for landings; store the new ones if ``apply``."""
    started = time.monotonic()
    detection: DetectionConfig = pipeline._config.to_detection_config()  # noqa: SLF001
    deck_altitude_for = pipeline.deck_altitude_for
    async with session_factory() as session:
        flight = await session.get(Flight, flight_id)
        if flight is None:
            raise FlightNotFound(flight_id)
        # Only the aircraft, and only the columns used: a flight's objects are
        # mostly shells, chaff and missiles, hundreds of thousands of rows on
        # a long mission. LIKE is the superset; the classifier decides.
        aircraft_rows = [
            row
            for row in (
                await session.execute(
                    select(DcsObject.id, DcsObject.acmi_id, DcsObject.type,
                           DcsObject.name, DcsObject.pilot)
                    .where(DcsObject.flight_id == flight_id, DcsObject.type.like("Air+%"))
                    .order_by(DcsObject.id)
                )
            ).all()
            if classify_object_type(row.type) == ObjectClass.AIRCRAFT
        ]
        first_seen = (
            await session.execute(
                select(func.min(DcsObject.first_seen)).where(DcsObject.flight_id == flight_id)
            )
        ).scalar_one_or_none()
        stored = (
            await session.execute(
                select(Landing.id, Landing.object_id, Landing.touchdown_time)
                .where(Landing.flight_id == flight_id)
                .order_by(Landing.id)
            )
        ).all()
        # The whole flight's ships, unwindowed, for the gate; the re-detection
        # reads its own windowed copy exactly as the rebuild does.
        ships = await load_flight_carriers(session, flight_id, None, None)
        statics = await _load_statics(session, flight_id)
    carriers = [state for _, state in ships.values()]
    report = FlightRescan(flight_id=flight_id, source_id=flight.source_id, applied=apply)
    matched: set[int] = set()

    for aircraft in aircraft_rows:
        async with session_factory() as session:
            rows = await fetch_track_rows(session, aircraft.id, None, None)
        samples = await asyncio.to_thread(samples_from_rows, rows)
        del rows
        if len(samples) < 2:
            continue
        report.aircraft_scanned += 1
        report.samples_scanned += len(samples)
        contacts = await asyncio.to_thread(
            find_contacts, samples, carriers, statics, detection, deck_altitude_for
        )
        del samples
        report.contacts += len(contacts)
        stored_here = [(sid, td) for sid, oid, td in stored if oid == aircraft.id]
        found: list[LandingEvent] = []
        for contact in contacts:
            if any(
                e.first_contact_time - MATCH_TOLERANCE_S
                <= contact
                <= e.touchdown.time + MATCH_TOLERANCE_S
                for e in found
            ):
                continue  # a bounce or roll-out flicker of one already found
            event, ship_ids = await _redetect_landing(
                session_factory, aircraft.id, flight_id, contact, detection, deck_altitude_for
            )
            if event is None:
                continue
            found.append(event)
            existing = next(
                (
                    sid
                    for sid, td in stored_here
                    if td is not None
                    and event.first_contact_time - MATCH_TOLERANCE_S
                    <= td
                    <= event.touchdown.time + MATCH_TOLERANCE_S
                ),
                None,
            )
            entry = FoundLanding(
                object_id=aircraft.id,
                acmi_id=aircraft.acmi_id,
                pilot=aircraft.pilot,
                airframe=aircraft.name,
                kind=event.kind,
                outcome=event.outcome,
                first_contact_time=event.first_contact_time,
                touchdown_time=event.touchdown.time,
                carrier_name=event.carrier_name,
                latitude=event.touchdown.latitude,
                longitude=event.touchdown.longitude,
                existing_landing_id=existing,
            )
            if existing is not None:
                matched.add(existing)
            elif apply:
                context = LandingContext(
                    flight_id=flight_id,
                    acmi_object_id=aircraft.acmi_id,
                    pilot=aircraft.pilot,
                    airframe=aircraft.name,
                    event=event,
                    object_row_id=aircraft.id,
                    carrier_row_id=ship_ids.get(event.carrier_obj_id or ""),
                    source_id=flight.source_id,
                    flight_reference_time=flight.reference_time,
                )
                entry.created_landing_id = await pipeline.record_rescanned_landing(
                    context,
                    _estimated_created_at(flight, first_seen, event.touchdown.time),
                )
            report.landings.append(entry)

    report.stored_not_redetected = [sid for sid, _, _ in stored if sid not in matched]
    report.elapsed_s = round(time.monotonic() - started, 2)
    logger.info(
        "rescan flight %d: %d aircraft, %d samples, %d contacts, %d landings (%d new)%s",
        flight_id,
        report.aircraft_scanned,
        report.samples_scanned,
        report.contacts,
        len(report.landings),
        len(report.new_landings),
        " applied" if apply else "",
    )
    return report


async def _redetect_landing(
    session_factory: async_sessionmaker[AsyncSession],
    object_row_id: int,
    flight_id: int,
    contact_time: float,
    detection: DetectionConfig,
    deck_altitude_for: Callable[[CarrierState], float | None] | None,
) -> tuple[LandingEvent | None, dict[str, int]]:
    """The landing at ``contact_time``, cut the way a rebuild of it would be.

    The first pass is keyed on the contact (the landing's first contact);
    the rebuild keys its window on the stored touchdown -- the LAST contact
    of the bounce sequence -- so the landing is cut once more around that,
    and what gets stored is what a later rebuild reproduces.
    """
    async with session_factory() as session:
        first = await redetect(
            session, object_row_id, flight_id, contact_time, detection, deck_altitude_for
        )
        if first.event is None:
            return None, {}
        final = await redetect(
            session,
            object_row_id,
            flight_id,
            first.event.touchdown.time,
            detection,
            deck_altitude_for,
        )
    chosen = final if final.event is not None else first
    return chosen.event, {acmi: row_id for acmi, (row_id, _) in chosen.ships.items()}
