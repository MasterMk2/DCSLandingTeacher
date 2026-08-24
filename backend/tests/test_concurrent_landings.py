"""Regression tests for concurrent / consecutive landing separation (Issue #6).

The detector state is already fully per-aircraft (one rolling buffer and
dedupe key per ACMI object id); these tests pin that behavior down:

- two aircraft approaching the same carrier simultaneously must produce
  independent, uncontaminated events/records;
- one aircraft flying consecutive approaches must be recorded as separate
  sessions;
- both aircraft must bind to the same carrier instance even while the ship
  is moving.
"""

from __future__ import annotations

import math
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.detection.detector import CarrierState, analyze_track
from app.grading.config import GradingConfig
from app.ingest import TrackIngestor
from app.models.entities import DcsObject, Landing
from app.pipeline import LandingPipeline
from tests.helpers import (
    DECK_ALTITUDE_M,
    LAT0,
    LON0,
    make_acmi_text_multi,
    make_approach_samples,
    make_carrier_state,
)

M_PER_DEG_LAT = 111320.0


# ---------------------------------------------------------------------------
# Detector level: simultaneous approaches by two aircraft
# ---------------------------------------------------------------------------


def test_two_aircraft_same_carrier_produce_isolated_events() -> None:
    samples_a = make_approach_samples(outcome="full_stop")
    # Aircraft B traps 400 m east of A: still the same carrier (< 800 m).
    samples_b = make_approach_samples(outcome="full_stop", offset_east_m=400.0)
    carriers = {"C1": make_carrier_state()}

    events_a = analyze_track(samples_a, DECK_ALTITUDE_M, carriers)
    events_b = analyze_track(samples_b, DECK_ALTITUDE_M, carriers)

    assert len(events_a) == 1
    assert len(events_b) == 1
    a, b = events_a[0], events_b[0]
    assert a.kind == "carrier" and b.kind == "carrier"
    assert a.carrier_obj_id == "C1" and b.carrier_obj_id == "C1"

    # Touchdowns are distinct and each approach contains only its own track.
    assert b.touchdown.longitude > a.touchdown.longitude
    for event, own in ((a, samples_a), (b, samples_b)):
        own_times = {s.time for s in own}
        assert all(s.time in own_times for s in event.approach)


def test_simultaneous_bolter_and_trap_do_not_cross_contaminate() -> None:
    samples_a = make_approach_samples(outcome="touch_and_go")  # bolter
    samples_b = make_approach_samples(outcome="full_stop", offset_east_m=400.0)
    carriers = {"C1": make_carrier_state()}

    events_a = analyze_track(samples_a, DECK_ALTITUDE_M, carriers)
    events_b = analyze_track(samples_b, DECK_ALTITUDE_M, carriers)

    assert len(events_a) == 1 and len(events_b) == 1
    assert events_a[0].outcome == "bolter"
    assert events_b[0].outcome == "full_stop"


# ---------------------------------------------------------------------------
# Same aircraft: consecutive approaches are separate sessions
# ---------------------------------------------------------------------------


def test_consecutive_approaches_recorded_as_two_events() -> None:
    # First pass: bolter (touch-and-go on the carrier), cut right after the
    # climb-out starts.
    first = [
        s
        for s in make_approach_samples(outcome="touch_and_go", ground_time_s=3)
        if s.time <= 8
    ]
    # Second pass: a fresh full-stop approach shifted +64 s so its inbound
    # segment starts right after the first climb-out.
    shift = 64.0
    second = [replace(s, time=s.time + shift) for s in make_approach_samples()]
    samples = sorted(first + second, key=lambda s: s.time)
    carriers = {"C1": make_carrier_state()}

    events = analyze_track(samples, DECK_ALTITUDE_M, carriers)

    assert len(events) == 2
    bolter, trap = events
    assert bolter.outcome == "bolter"
    assert bolter.touchdown.time == pytest.approx(0.0)
    assert trap.outcome == "full_stop"
    assert trap.touchdown.time == pytest.approx(shift)
    # The second approach segment must not contain first-pass samples.
    assert all(s.time >= shift - 60.0 for s in trap.approach)


# ---------------------------------------------------------------------------
# Moving carrier: both aircraft bind to the same ship instance
# ---------------------------------------------------------------------------


def test_both_aircraft_bind_to_same_moving_carrier() -> None:
    speed_ms = 5.0
    # The ship steams north at 5 m/s; position sampled every 30 s.
    carrier = CarrierState(
        obj_id="C1",
        name="CV-59",
        samples=[
            (
                float(t),
                LAT0 + (t * speed_ms) / M_PER_DEG_LAT,
                LON0,
                DECK_ALTITUDE_M,
                0.0,
                speed_ms,
            )
            for t in range(0, 121, 30)
        ],
    )
    carriers = {"C1": carrier}

    # Aircraft A traps at t=0 abeam of the ship's initial position,
    # aircraft B traps at t=60 when the ship has moved ~300 m north.
    samples_a = make_approach_samples(outcome="full_stop", ground_time_s=10)
    samples_a = [s for s in samples_a if s.time <= 10]
    samples_b = make_approach_samples(
        outcome="full_stop",
        ground_time_s=10,
        offset_north_m=60 * speed_ms,
    )
    samples_b = [replace(s, time=s.time + 60.0) for s in samples_b]
    samples_b = [s for s in samples_b if s.time <= 70]

    events_a = analyze_track(samples_a, DECK_ALTITUDE_M, carriers)
    events_b = analyze_track(samples_b, DECK_ALTITUDE_M, carriers)

    assert len(events_a) == 1 and len(events_b) == 1
    a, b = events_a[0], events_b[0]
    assert a.carrier_obj_id == "C1" and b.carrier_obj_id == "C1"
    # Each touchdown sits next to the ship's interpolated position at that
    # moment (~300 m apart), proving per-time anchoring on the moving deck.
    north_offset = (b.touchdown.latitude - a.touchdown.latitude) * M_PER_DEG_LAT
    assert north_offset == pytest.approx(300.0, abs=50.0)
    # Ship-relative rows exist for both and start far out on the approach.
    assert a.ship_relative and b.ship_relative
    assert a.ship_relative[0]["distance_to_go"] > 3000.0
    assert b.ship_relative[0]["distance_to_go"] > 3000.0


# ---------------------------------------------------------------------------
# Ingest E2E: interleaved stream keeps records separated per aircraft
# ---------------------------------------------------------------------------


async def test_interleaved_stream_yields_one_record_per_aircraft(session_factory):
    pipeline = LandingPipeline(session_factory, GradingConfig({}))
    ingestor = TrackIngestor(
        session_factory,
        landing_listener=pipeline.handle_landing,
        landing_finalize_listener=pipeline.finalize_landing,
        sample_buffer_s=600.0,
    )
    text = make_acmi_text_multi(
        [
            {
                "obj_id": "101",
                "samples": make_approach_samples(outcome="full_stop"),
                "name": "F/A-18C",
                "pilot": "Alpha",
            },
            {
                "obj_id": "201",
                "samples": make_approach_samples(
                    outcome="full_stop", offset_east_m=400.0
                ),
                "name": "Su-33",
                "pilot": "Bravo",
            },
        ]
    )
    try:
        for line in text.splitlines():
            await ingestor.handle_line(line)
    finally:
        await ingestor.close()

    async with session_factory() as session:
        landings = (
            await session.execute(select(Landing).order_by(Landing.touchdown_time))
        ).scalars().all()
        objects = {
            obj.id: obj
            for obj in (
                await session.execute(select(DcsObject))
            ).scalars().all()
        }

    # One record per aircraft, neither contaminated by the other.
    assert len(landings) == 2
    pilots = set()
    for landing in landings:
        obj = objects[landing.object_id]
        pilots.add(obj.pilot)
        assert landing.kind == "carrier"
        assert landing.venue_name == "CV-59"
        assert landing.carrier_object_id is not None
        assert landing.grade in ("OK", "OK-", "(OK)", "_NO_GRADE_", "CUT")
    assert pilots == {"Alpha", "Bravo"}

    # Touchdown positions differ by the 400 m east offset (the ACMI text
    # format rounds coordinates to 6 significant digits, hence the slack).
    lons = sorted(landing.longitude for landing in landings)
    east_m = (lons[1] - lons[0]) * M_PER_DEG_LAT * math.cos(math.radians(LAT0))
    assert east_m == pytest.approx(400.0, abs=50.0)
