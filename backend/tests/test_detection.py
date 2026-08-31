"""Tests for the landing detection engine (FR-2)."""

from __future__ import annotations

import math

from app.detection.classify import ObjectClass, classify_object_type
from app.detection.detector import CarrierState, DetectionConfig, analyze_track
from tests.helpers import (
    DECK_ALTITUDE_M,
    LAT0,
    LON0,
    make_approach_samples,
    make_carrier_state,
)


def _make_overhead_approach_samples(
    *,
    outcome: str = "full_stop",
    duration_before_s: float = 60.0,
    ground_time_s: float = 25.0,
    deck_altitude_m: float = DECK_ALTITUDE_M,
) -> list:
    """Build an overhead-break approach: initial -> break -> base -> final.

    Timeline (t=0 is touchdown):
    - t=-60..-30: Initial (high altitude, high speed, heading 180 - approaching from south)
    - t=-30..-20: Break turn (sharp right turn from 180 to 270, then to 360/0)
    - t=-20..-10: Base leg (heading 270, descending)
    - t=-10..0: Final (heading 360/0, aligned with runway, stabilized)
    """
    from app.detection.detector import TrackSample

    M_PER_DEG_LAT = 111320.0

    def _lat_offset(meters: float) -> float:
        return meters / M_PER_DEG_LAT

    def _lon_offset(meters: float) -> float:
        return meters / (M_PER_DEG_LAT * math.cos(math.radians(LAT0)))

    samples: list[TrackSample] = []
    n_before = int(duration_before_s)
    n_after = max(int(ground_time_s), 12 if outcome != "full_stop" else int(ground_time_s))

    for t in range(-n_before, n_after + 1):
        time = float(t)

        if t < -30:  # Initial: approaching from south, heading 180 (northbound)
            dtg = 3000.0 + (t + 30) * 150.0  # ~3nm out
            latitude = LAT0 - _lat_offset(dtg)
            longitude = LON0
            altitude = deck_altitude_m + 1500.0  # High altitude
            speed = 200.0
            heading = 180.0
            on_ground = False
        elif t < -20:  # Break: sharp right turn 180 -> 270 -> 360
            progress = (t + 30) / 10.0  # 0..1
            if progress < 0.5:
                heading = 180.0 + progress * 180.0  # 180 -> 270
            else:
                heading = 270.0 + (progress - 0.5) * 180.0  # 270 -> 360/0
            # During break, circle around abeam position
            radius = 1800.0  # ~1nm
            angle = math.radians(heading)
            latitude = LAT0 + _lat_offset(radius * math.cos(angle))
            longitude = LON0 + _lon_offset(radius * math.sin(angle))
            altitude = deck_altitude_m + 800.0
            speed = 180.0
            on_ground = False
        elif t < -10:  # Base leg: heading 270 (west), descending
            dtg = 1800.0 + (t + 20) * 150.0
            angle = math.radians(270.0)
            latitude = LAT0 + _lat_offset(dtg * math.cos(angle))
            longitude = LON0 + _lon_offset(dtg * math.sin(angle))
            altitude = deck_altitude_m + 600.0 + (t + 20) * 30.0  # Descending
            speed = 150.0
            heading = 270.0
            on_ground = False
        elif t < 0:  # Final: heading 0/360 (north), aligned, stabilized
            dtg = abs(t) * 70.0
            ideal_agl = dtg * math.tan(math.radians(3.0))  # 3 deg land glideslope
            latitude = LAT0 - _lat_offset(dtg)
            longitude = LON0
            altitude = deck_altitude_m + ideal_agl
            speed = 70.0
            heading = 0.0
            on_ground = False
        else:  # Ground roll
            latitude = LAT0
            longitude = LON0
            if outcome == "full_stop":
                altitude = deck_altitude_m
                speed = max(5.0, 70.0 - t * 3.0)
                on_ground = True
            else:
                if t <= 3:
                    altitude = deck_altitude_m
                    speed = 70.0
                    on_ground = True
                else:
                    altitude = deck_altitude_m + (t - 3) * 5.0
                    speed = 70.0 + (t - 3) * 1.0
                    on_ground = False

        samples.append(
            TrackSample(
                time=time,
                latitude=latitude,
                longitude=longitude,
                altitude=altitude,
                agl=None,
                speed=speed,
                heading=heading,
                aoa=None,
                on_ground=on_ground,
            )
        )
    return samples


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
    """The walk-back stops at the configured radius, not just the time window."""
    config = DetectionConfig(
        approach_window_s=60.0,
        approach_distance_m=3704.0,
        # Land landings normally use the wider pattern-sized capture; pin it
        # to the carrier figures so this test measures the radius cut alone.
        land_approach_window_s=60.0,
        land_approach_distance_m=3704.0,
    )
    samples = make_approach_samples(duration_before_s=120)
    events = analyze_track(samples, DECK_ALTITUDE_M, carriers={}, config=config)
    assert len(events) == 1
    times = [s.time for s in events[0].approach]
    # 3704 m + margin at 70 m/s ~= 60 s; nothing much older may slip in.
    assert min(times) >= -70.0


def test_land_approach_captures_the_pattern_not_just_the_final() -> None:
    """Land landings must capture far enough back to hold the circuit.

    The pattern is what the overhead grading judges, and it does not fit in
    the carrier-sized 60 s / 2 nm cut: a fighter circuit at 1.5 nm abeam is
    already ~4.5 km from the touchdown point. Carrier passes keep the short
    window -- there is no pattern there and the LSO grader never looks past
    the last few seconds.
    """
    samples = make_approach_samples(duration_before_s=120)

    land = analyze_track(samples, DECK_ALTITUDE_M, carriers={})
    assert len(land) == 1
    # 5556 m + 500 m margin at 70 m/s ~= 86 s of walk-back.
    assert min(s.time for s in land[0].approach) < -70.0

    carrier_samples = make_approach_samples(duration_before_s=120)
    carrier = analyze_track(
        carrier_samples, DECK_ALTITUDE_M, carriers={"C1": make_carrier_state()}
    )
    assert len(carrier) == 1
    assert carrier[0].kind == "carrier"
    assert min(s.time for s in carrier[0].approach) >= -70.0


def test_bounce_sequence_keeps_a_stable_identity_as_it_is_absorbed() -> None:
    """再解析でイベント同一性が変わらないこと (Issue #5 の孤児化対策)。

    `touchdown` はマージしたバウンド列の *最後* の接地なので、ライブ監視で
    バッファが伸びてバウンドを吸収するたびに前へ動く。それをキーにしていたため
    1 回のバウンド着陸が 3 行に割れ、途中の行が provisional のまま孤児化した。
    `first_contact_time` は最初の接地に固定されるので同一性が保たれる。
    """
    from app.detection.detector import TrackSample, analyze_track

    ground = 0.0

    def sample(t: float, agl: float) -> TrackSample:
        return TrackSample(
            time=t,
            latitude=41.6 + t * 1e-5,
            longitude=41.6,
            altitude=ground + agl,
            agl=agl,
            speed=70.0,
            heading=0.0,
            on_ground=agl <= 3.0,
        )

    # 進入 → 接地 → バウンド (頂点 6.4 m) → 再接地 → 小バウンド → 接地して停止。
    approach = [sample(float(t), 60.0 - t * 4.0) for t in range(0, 14)]
    bounce = [
        sample(14.0, 2.6), sample(14.5, 0.7),          # 1 回目の接地
        sample(15.0, 3.1), sample(16.0, 6.4), sample(17.0, 3.4),
        sample(17.5, 2.3), sample(18.0, 1.4),          # 2 回目の接地
        sample(18.5, 3.2), sample(19.0, 3.9),
        sample(20.0, 2.9),                             # 3 回目の接地
    ]
    settled = [sample(20.0 + i * 0.5, 2.3) for i in range(1, 80)]
    full = approach + bounce + settled

    # ライブ監視を模して、バッファが伸びる各時点で解析する。
    identities, touchdowns = set(), set()
    for cut in (16, 19, 21, len(full)):
        events = analyze_track(full[:cut], ground, carriers={}, current_time=full[cut - 1].time)
        assert len(events) == 1, f"cut={cut} で {len(events)} 件に割れた"
        identities.add(round(events[0].first_contact_time, 3))
        touchdowns.add(round(events[0].touchdown.time, 3))

    # 接地時刻は動く (それがバグの原因) が、同一性は 1 つに保たれる。
    assert identities == {14.0}
    assert len(touchdowns) > 1


def test_approach_pattern_overhead_break() -> None:
    """Overhead break pattern should be classified as 'overhead'."""
    samples = _make_overhead_approach_samples(outcome="full_stop")
    events = analyze_track(samples, DECK_ALTITUDE_M, carriers={})

    assert len(events) == 1
    event = events[0]
    assert event.approach_pattern == "overhead"


def test_approach_pattern_straight_in() -> None:
    """Standard straight-in approach should be classified as 'straight_in'."""
    samples = make_approach_samples(outcome="full_stop", duration_before_s=55.0)
    events = analyze_track(samples, DECK_ALTITUDE_M, carriers={})

    assert len(events) == 1
    event = events[0]
    assert event.approach_pattern == "straight_in"


def test_approach_pattern_overhead_touch_and_go() -> None:
    """Overhead break with touch-and-go outcome should still classify as overhead."""
    samples = _make_overhead_approach_samples(outcome="touch_and_go")
    events = analyze_track(samples, DECK_ALTITUDE_M, carriers={})

    assert len(events) == 1
    event = events[0]
    assert event.approach_pattern == "overhead"
    assert event.outcome == "touch_and_go"


def test_approach_pattern_carrier_straight_in() -> None:
    """Carrier approach (straight in) should be classified as straight_in."""
    samples = make_approach_samples(outcome="full_stop", duration_before_s=55.0)
    carriers = {"C1": make_carrier_state()}
    events = analyze_track(samples, DECK_ALTITUDE_M, carriers)

    assert len(events) == 1
    event = events[0]
    assert event.kind == "carrier"
    assert event.approach_pattern == "straight_in"


def test_land_capture_stops_at_the_previous_touchdown() -> None:
    """A circuit starts where the last one ended.

    The land window reaches 300 s / 8 nm back so the recording begins before
    the initial. A closed pattern -- touch and go, round again -- fits inside
    that, so without a stop the capture would run into the previous arrival:
    two loops on the plan view and two downwinds for the leg finder to pick
    between.
    """
    from app.detection.detector import TrackSample

    first = make_approach_samples(outcome="touch_and_go", duration_before_s=60)
    # Fly the same profile again, 100 s later, ending in a full stop.
    gap = max(s.time for s in first) + 100.0
    second = [
        TrackSample(
            time=s.time + gap,
            latitude=s.latitude,
            longitude=s.longitude,
            altitude=s.altitude,
            agl=s.agl,
            heading=s.heading,
            speed=s.speed,
            on_ground=s.on_ground,
        )
        for s in make_approach_samples(outcome="full_stop", duration_before_s=60)
    ]

    events = analyze_track(first + second, DECK_ALTITUDE_M, carriers={})
    assert len(events) == 2
    final_arrival = events[-1]
    earliest = min(s.time for s in final_arrival.approach)
    first_contact = max(
        s.time for s in first if s.on_ground
    )
    assert earliest > first_contact, (
        "the second circuit's capture reached back past the first touchdown"
    )


def test_carrier_history_is_bounded_by_its_retention_window() -> None:
    """Regression: unbounded carrier history wedged the live ingest loop.

    Carriers stream at ~3Hz for the whole session, and position_at() is called
    once per carrier per landing check *and* once per approach sample. With no
    window each carrier held ~500k samples after two days, so one detection
    pass walked millions of them and the event loop stopped returning.
    """
    carrier = CarrierState(obj_id="C1", name="CVN-73", max_age_s=600.0)
    for i in range(10_000):  # ~55 minutes at 3Hz
        t = i / 3.0
        carrier.append((t, 36.0 + t * 1e-6, 140.0, 20.0, 90.0, 10.0))

    assert carrier.samples, "the window must never empty the series"
    span = carrier.samples[-1][0] - carrier.samples[0][0]
    assert span <= 600.0, f"kept {span:.0f}s of history"
    assert len(carrier.samples) < 2_000

    # ...and lookups still work against what is left
    latest = carrier.samples[-1][0]
    assert carrier.position_at(latest) is not None
    assert carrier.heading_at(latest) == 90.0


def test_carrier_lookups_interpolate_and_clamp() -> None:
    """position_at/heading_at read the sample tuples in place, not a copy."""
    carrier = CarrierState(obj_id="C1")
    carrier.append((0.0, 36.0, 140.0, 20.0, 10.0, 0.0))
    carrier.append((10.0, 36.1, 140.2, 20.0, 20.0, 0.0))

    lat, lon = carrier.position_at(5.0)
    assert math.isclose(lat, 36.05, abs_tol=1e-9)
    assert math.isclose(lon, 140.1, abs_tol=1e-9)
    assert carrier.position_at(-5.0) == (36.0, 140.0)  # clamped to the first
    assert carrier.position_at(50.0) == (36.1, 140.2)  # clamped to the last

    # Step function: the most recent heading at or before the time.
    assert carrier.heading_at(-1.0) == 10.0
    assert carrier.heading_at(0.0) == 10.0
    assert carrier.heading_at(9.9) == 10.0
    assert carrier.heading_at(10.0) == 20.0
    assert carrier.heading_at(99.0) == 20.0


def test_carrier_append_ignores_a_repeated_timestamp() -> None:
    carrier = CarrierState(obj_id="C1")
    carrier.append((1.0, 36.0, 140.0, 20.0, 0.0, 0.0))
    carrier.append((1.0, 37.0, 141.0, 20.0, 0.0, 0.0))
    assert carrier.samples == [(1.0, 36.0, 140.0, 20.0, 0.0, 0.0)]
