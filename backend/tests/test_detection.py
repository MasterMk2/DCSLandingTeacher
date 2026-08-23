"""Tests for the landing detection engine (FR-2)."""

from __future__ import annotations

from app.detection.classify import ObjectClass, classify_object_type
from app.detection.detector import DetectionConfig, analyze_track
from tests.helpers import (
    DECK_ALTITUDE_M,
    LAT0,
    LON0,
    make_approach_samples,
    make_carrier_state,
)


def test_classify_object_types() -> None:
    assert classify_object_type("Air+FixedWing") is ObjectClass.AIRCRAFT
    assert classify_object_type("Carrier+FixedWing") is ObjectClass.CARRIER
    assert classify_object_type("Sea+Watercraft+AircraftCarrier") is ObjectClass.CARRIER
    assert classify_object_type("Ground+Static+Aircraft") is ObjectClass.STATIC
    assert classify_object_type("Sea+Watercraft+Destroyer") is ObjectClass.OTHER
    assert classify_object_type(None) is ObjectClass.OTHER


def test_detect_full_stop_carrier_arrestment() -> None:
    samples = make_approach_samples(outcome="full_stop")
    carriers = {"C1": make_carrier_state()}
    events = analyze_track(samples, DECK_ALTITUDE_M, carriers)

    assert len(events) == 1
    event = events[0]
    assert event.kind == "carrier"
    assert event.outcome == "full_stop"
    assert event.carrier_obj_id == "C1"
    assert event.carrier_name == "CV-59"

    touchdown = event.touchdown
    assert touchdown.time == 0.0
    assert touchdown.latitude == LAT0
    assert touchdown.longitude == LON0
    assert 0.0 < touchdown.descent_rate_ms < 8.0

    # Approach segment: ~55 s of inbound samples plus a short tail.
    assert len(event.approach) >= 50
    times = [s.time for s in event.approach]
    assert min(times) <= -50.0
    assert max(times) > 0.0

    # Ship-relative conversion available with sane values.
    assert event.ship_relative
    first = event.ship_relative[0]
    assert first["distance_to_go"] > 3000.0
    assert all(row["lateral"] == 0.0 for row in event.ship_relative)


def test_detect_bolter_on_carrier() -> None:
    samples = make_approach_samples(outcome="touch_and_go")
    carriers = {"C1": make_carrier_state()}
    events = analyze_track(samples, DECK_ALTITUDE_M, carriers)

    assert len(events) == 1
    event = events[0]
    assert event.kind == "carrier"
    # A climb-out from a carrier deck is a bolter, not a touch-and-go.
    assert event.outcome == "bolter"


def test_detect_touch_and_go_on_land() -> None:
    samples = make_approach_samples(outcome="touch_and_go")
    events = analyze_track(samples, DECK_ALTITUDE_M, carriers={})

    assert len(events) == 1
    event = events[0]
    assert event.kind == "land"
    assert event.outcome == "touch_and_go"


def test_detect_full_stop_landing_on_land() -> None:
    samples = make_approach_samples(outcome="full_stop")
    events = analyze_track(samples, DECK_ALTITUDE_M, carriers={})

    assert len(events) == 1
    event = events[0]
    assert event.kind == "land"
    assert event.outcome == "full_stop"


def test_hard_contact_is_not_a_landing() -> None:
    # Descent rate far above the crash threshold: no landing event.
    samples = make_approach_samples(pre_touchdown_descent_ms=20.0)
    events = analyze_track(samples, DECK_ALTITUDE_M, carriers={})
    assert events == []


def test_no_detection_for_pure_airborne_track() -> None:
    samples = make_approach_samples(outcome="full_stop")
    airborne = [s for s in samples if s.time < 0]
    events = analyze_track(airborne, DECK_ALTITUDE_M, carriers={})
    assert events == []


def test_unknown_ground_altitude_uses_on_ground_flag() -> None:
    samples = make_approach_samples(outcome="full_stop")
    events = analyze_track(samples, ground_altitude_m=None, carriers={})
    # The synthetic samples carry an explicit OnGround flag, so detection
    # still works without a known surface elevation.
    assert len(events) == 1


def test_approach_window_respects_distance_limit() -> None:
    config = DetectionConfig(approach_window_s=60.0, approach_distance_m=3704.0)
    samples = make_approach_samples(duration_before_s=120)
    events = analyze_track(samples, DECK_ALTITUDE_M, carriers={}, config=config)
    assert len(events) == 1
    times = [s.time for s in events[0].approach]
    # 3704 m + margin at 70 m/s ~= 60 s; nothing much older may slip in.
    assert min(times) >= -70.0
