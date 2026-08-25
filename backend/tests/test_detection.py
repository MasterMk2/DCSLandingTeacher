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
