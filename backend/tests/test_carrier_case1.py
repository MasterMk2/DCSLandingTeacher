"""A Case I trap, from the kiss-off to the wires, through the production path.

What went wrong before, and why these tests look the way they do:

- The carrier capture was 60 s / 2 nm, so no stored trap contained its break
  (the lead's kiss-off): a Case I from the kiss-off to the wires takes
  1.5-2 minutes.
- Carrier deviations were measured against the deck FROZEN at the touchdown
  instant, from Tacview's AGL (height above the SEA, ~20 m on the deck), on
  an angled deck pointing to STARBOARD, anchored at the ramp instead of the
  target wire. Each of those alone moves the LSO factors.

Every test here drives ``TrackIngestor.handle_line`` with ACMI text shaped
like production (see ``tests/case1.py``) and resolves the ship through the
real ``config/carriers.yaml`` and ``config/grading.yaml``: tests that inject
their own deck resolver or call the grader directly are how this repo has
shipped inert carrier fixes before.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from sqlalchemy import select

from app.detection.detector import CarrierState, LandingEvent, Touchdown, TrackSample
from app.detection.geometry import offset_position
from app.grading.carriers import load_carrier_geometry_book
from app.grading.config import load_grading_config
from app.grading.deviations import ApproachAnalysis, build_approach_analysis
from app.grading.kinematics import G0, annotate_kinematics
from app.ingest import TrackIngestor
from app.models.entities import Landing
from app.pipeline import LandingPipeline
from tests.case1 import (
    FT,
    Case1,
    fly_bolter_then_trap,
    fly_case1,
    fly_straight_in,
    fly_waveoff_then_trap,
)
from tests.conftest import GRADING_YAML

CARRIERS_YAML = Path(__file__).resolve().parents[2] / "config" / "carriers.yaml"


async def _trap(
    session_factory, case: Case1 | None = None, **kwargs
) -> tuple[Landing, dict[str, float], LandingPipeline]:
    case = case or fly_case1(**kwargs)
    config = load_grading_config(GRADING_YAML)
    pipeline = LandingPipeline(
        session_factory,
        config,
        carrier_geometry_book=load_carrier_geometry_book(CARRIERS_YAML),
    )
    # Wired exactly as app.api.main wires the live source.
    ingestor = TrackIngestor(
        session_factory,
        landing_listener=pipeline.handle_landing,
        landing_finalize_listener=pipeline.finalize_landing,
        detection_config=config.to_detection_config(),
        deck_altitude_for=pipeline.deck_altitude_for,
    )
    try:
        for line in case.lines:
            await ingestor.handle_line(line)
    finally:
        await ingestor.close()
    async with session_factory() as session:
        rows = (await session.execute(select(Landing))).scalars().all()
    assert len(rows) == 1, f"expected one trap, got {len(rows)}"
    return rows[0], case.expect, pipeline


async def test_the_record_starts_before_the_kissoff(session_factory) -> None:
    landing, expect, _ = await _trap(session_factory)

    assert landing.kind == "carrier"
    assert landing.venue_name == "CVN_73"
    assert landing.outcome == "full_stop"
    assert landing.outcome_status == "final"
    samples = landing.approach_track["samples"]
    # The whole recovery is on record: the initial, the kiss-off 1.9 min
    # before the trap, and everything after. The old 60 s capture started
    # in the 180.
    assert samples[0]["time"] <= expect["kissoff_time"] - 30.0
    assert expect["target_time"] - expect["kissoff_time"] > 100.0


async def test_the_case_i_pattern_is_read_relative_to_the_moving_ship(session_factory) -> None:
    landing, expect, _ = await _trap(session_factory)
    m = landing.metrics

    assert landing.approach_pattern == "overhead"
    assert m["deck_frame"] == "moving_deck"
    # The kiss-off: where, when, and how hard. The break detector starts a
    # few samples into the turn, i.e. a few tens of metres late.
    assert m["pattern_break_start_time"] == pytest.approx(expect["kissoff_time"], abs=1.0)
    assert m["pattern_break_along_ship_m"] == pytest.approx(expect["kissoff_x"], abs=80.0)
    assert m["pattern_break_heading_change_deg"] == pytest.approx(-180.0, abs=10.0)
    assert m["pattern_break_max_load_factor"] == pytest.approx(expect["break_g"], abs=0.2)
    assert m["pattern_break_max_bank_deg"] == pytest.approx(
        math.degrees(math.acos(1.0 / expect["break_g"])), abs=1.0
    )
    assert m["pattern_break_entry_agl_m"] == pytest.approx(800.0 * FT, abs=5.0)
    # Downwind parallel to the ship's heading -- not 9 deg off it, which is
    # what measuring it along the angled deck would report.
    assert m["pattern_downwind_course_error_deg"] < 1.0
    assert m["pattern_downwind_abeam_m"] == pytest.approx(expect["abeam_m"], abs=30.0)
    assert m["pattern_abeam_distance_m"] == pytest.approx(expect["abeam_m"], abs=30.0)
    assert m["pattern_abeam_altitude_m"] == pytest.approx(expect["downwind_alt_m"], abs=5.0)
    # The 90 is the HEADING over the ground perpendicular to the BRC (NATOPS),
    # not the track relative to the moving deck: at 15 m/s the latter comes
    # ~6 s early and reads ~9 m lower on this pass.
    assert m["pattern_ninety_altitude_m"] == pytest.approx(expect["ninety_alt_m"], abs=3.5)
    assert m["pattern_wake_altitude_m"] == pytest.approx(expect["wake_alt_m"], abs=3.0)
    # Groove: from wings level after the 180 to the touchdown, which the
    # detector places a quarter second short of the 3-wire.
    assert m["pattern_groove_start_method"] == "wings_level"
    assert m["pattern_groove_start_time"] == pytest.approx(expect["rollout_time"], abs=0.3)
    assert m["pattern_groove_time_s"] == pytest.approx(expect["groove_s"] - 0.3, abs=0.4)
    assert m["pattern_groove_verdict"] == "OK"
    # The plan view colours the final from here.
    assert m["pattern_rollout_time"] == m["pattern_groove_start_time"]


async def test_a_pass_flown_on_the_ball_grades_ok(session_factory) -> None:
    """Every frame error shows up here as a false factor.

    Sea-referenced AGL reads ~20 m HIGH; the starboard angled deck puts the
    groove 18 deg off the axis (OFFLINE); a deck frozen at the touchdown
    instant reads LOW; averaging the 350 kt initial into "approach speed"
    reads SLOW. A pass built to sit on the ball must come out clean.
    """
    landing, _, _ = await _trap(session_factory)

    assert landing.factors == []
    assert landing.grade == "OK"
    samples = landing.approach_track["samples"]
    at_touchdown = min(samples, key=lambda s: abs(s["time"] - landing.touchdown_time))
    # Height above the DECK: the jet's reference point is ~2-3 m up when the
    # wheels touch, not the ~22 m Tacview reports over the sea.
    assert 1.0 < at_touchdown["agl"] < 3.5
    groove = [
        s for s in samples
        if landing.metrics["pattern_groove_start_time"] <= s["time"] < landing.touchdown_time
    ]
    assert max(abs(s["glideslope_deviation"]) for s in groove) < 0.5
    assert max(abs(s["centerline_deviation"]) for s in groove) < 1.0


async def test_a_firm_trap_is_what_a_carrier_jet_is_built_for(session_factory) -> None:
    """No flare on the boat: ~690 fpm on the ball is correct, not "hard"."""
    landing, expect, _ = await _trap(session_factory)
    m = landing.metrics

    assert m["touchdown_descent_rate_fpm"] == pytest.approx(expect["groove_sink_fpm"], rel=0.05)
    assert m["ramp_sink_ratio"] == pytest.approx(1.0, abs=0.1)
    assert "フレアなしの接地で適正" in landing.comment
    assert "硬" not in landing.comment


async def test_a_flare_at_the_ramp_is_called_out_but_not_graded(session_factory) -> None:
    landing, _, _ = await _trap(session_factory, flare=True)
    m = landing.metrics

    assert m["ramp_sink_ratio"] < 0.5
    assert "ランプで沈下を止めた" in landing.comment
    # Measured and spoken to, never scored: nothing calibrates it here.
    assert landing.grade == "OK"
    assert all(f["name"] != "FLARE" for f in landing.factors)


@pytest.mark.parametrize(
    ("groove_s", "verdict"), [(11.0, "NESA"), (24.0, "LIG")]
)
async def test_groove_time_outside_15_to_19_s_is_named(
    session_factory, groove_s: float, verdict: str
) -> None:
    landing, _, _ = await _trap(session_factory, groove_s=groove_s)

    assert landing.metrics["pattern_groove_verdict"] == verdict
    assert f"（{verdict}: 基準 15〜19 秒）" in landing.comment


async def test_a_straight_in_has_no_case_i_groove_to_time(session_factory) -> None:
    """A Case III-style final is not a long groove.

    Timed from "the last moment the track was 10 deg off the final bearing",
    this two-minute straight-in would read as a 120 s groove and be called
    LIG. There is no 180 and no downwind, so there is no Case I groove, and
    nothing about a missing break either -- none was flown.
    """
    landing, _, _ = await _trap(session_factory, fly_straight_in())
    m = landing.metrics

    assert landing.kind == "carrier"
    assert m["deck_frame"] == "moving_deck"
    assert m["pattern_groove_time_s"] is None
    assert m["pattern_downwind_judged"] is False
    assert landing.approach_pattern != "overhead"
    assert "グルーブ" not in landing.comment
    assert "LIG" not in landing.comment
    assert "ブレイク" not in landing.comment
    assert landing.grade == "OK"


async def _traps(session_factory, case: Case1) -> list[Landing]:
    config = load_grading_config(GRADING_YAML)
    pipeline = LandingPipeline(
        session_factory,
        config,
        carrier_geometry_book=load_carrier_geometry_book(CARRIERS_YAML),
    )
    ingestor = TrackIngestor(
        session_factory,
        landing_listener=pipeline.handle_landing,
        landing_finalize_listener=pipeline.finalize_landing,
        detection_config=config.to_detection_config(),
        deck_altitude_for=pipeline.deck_altitude_for,
    )
    try:
        for line in case.lines:
            await ingestor.handle_line(line)
    finally:
        await ingestor.close()
    async with session_factory() as session:
        return list(
            (await session.execute(select(Landing).order_by(Landing.touchdown_time)))
            .scalars()
            .all()
        )


async def test_the_circuit_after_a_bolter_has_no_kissoff(session_factory) -> None:
    """After a bolter the jet climbs off the angled deck and turns downwind.

    That turn is not a break: there is no initial, and 600 ft is the right
    height for it. It used to be written up as "the kiss-off" and marked
    against the 800 ft initial.
    """
    case = fly_bolter_then_trap()
    bolter, trap = await _traps(session_factory, case)

    assert bolter.outcome == "bolter"
    assert bolter.metrics["pattern_entry"] == "initial"
    assert "ブレイク（キスオフ）" in bolter.comment

    m = trap.metrics
    assert trap.outcome == "full_stop"
    assert m["pattern_starts_from_deck"] is True
    assert m["pattern_entry"] == "turn"
    assert m["pattern_abeam_distance_m"] == pytest.approx(case.expect["abeam_m"], abs=30.0)
    assert "キスオフ" not in trap.comment
    assert "基準 800 ft" not in trap.comment
    assert "ダウンウィンドへの旋回" in trap.comment
    assert "甲板を離れてからの周回" in trap.comment


async def test_the_circuit_after_a_waveoff_is_the_one_measured(session_factory) -> None:
    """A wave-off leaves no deck contact, so the record holds both passes.

    The waved-off circuit has the LONGER downwind, and "the longest downwind
    in the recording" would describe it -- abeam 1.13 nm and an initial with a
    kiss-off -- as the pattern of a trap that was flown 1.0 nm abeam off a
    go-around turn.
    """
    case = fly_waveoff_then_trap()
    (trap,) = await _traps(session_factory, case)
    m = trap.metrics

    assert m["pattern_low_pass_time"] == pytest.approx(case.expect["waveoff_time"], abs=6.0)
    assert m["pattern_entry"] == "turn"
    assert m["pattern_abeam_distance_m"] == pytest.approx(case.expect["abeam_m"], abs=30.0)
    assert m["pattern_downwind_abeam_m"] == pytest.approx(case.expect["abeam_m"], abs=30.0)
    assert "ウェーブオフ後の周回" in trap.comment
    assert "キスオフ" not in trap.comment
    assert trap.grade == "OK"


async def test_a_regrade_reads_the_same_pattern_back(session_factory) -> None:
    """Everything the pattern needs is in the stored track (FR-7)."""
    landing, _, pipeline = await _trap(session_factory)
    before = dict(landing.metrics)

    payload = await pipeline.regrade(landing)

    # The stored track is rounded (1 cm, 1 ms), so the re-derivation may
    # move the last digit; nothing may move more than that.
    for key, tolerance in (
        ("pattern_break_max_load_factor", 0.02),
        ("pattern_break_along_ship_m", 1.0),
        ("pattern_abeam_distance_m", 1.0),
        ("pattern_groove_time_s", 0.11),
        ("touchdown_descent_rate_fpm", 0.5),
    ):
        assert payload["metrics"][key] == pytest.approx(before[key], abs=tolerance), key
    assert payload["grade"] == landing.grade
    assert payload["approach_pattern"] == "overhead"


async def test_the_detail_api_serves_the_pattern_in_the_ships_frame(tmp_path) -> None:
    """The plan view draws a Case I up the ship's heading, from these."""
    import httpx

    from app.api.main import create_app
    from app.config import Settings

    # The default carriers path is relative to the repo root and does not
    # resolve from backend/; without the book no deck exists and no trap
    # can be detected at all, so name the real file.
    app = create_app(
        Settings(
            database_url=f"sqlite+aiosqlite:///{(tmp_path / 'api.db').as_posix()}",
            acmi_enabled=False,
            grading_config_path=str(GRADING_YAML),
            carriers_config_path=str(CARRIERS_YAML),
        )
    )
    async with app.router.lifespan_context(app):
        await _replay_into_app(app)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            listing = (
                await http.get("/api/v1/landings", params={"kind": "carrier"})
            ).json()
            detail = (
                await http.get(f"/api/v1/landings/{listing['items'][0]['id']}")
            ).json()
    samples = detail["approach_track"]["samples"]
    downwind = [
        s for s in samples
        if detail["metrics"]["pattern_downwind_start_time"] + 8.0
        <= s["time"]
        <= detail["metrics"]["pattern_downwind_end_time"]
    ]
    assert downwind
    # 1.13 nm to PORT of the ship's centreline, in the ship's own frame.
    for s in downwind:
        assert s["ship_lateral"] == pytest.approx(-2083.8, abs=30.0)


async def _replay_into_app(app) -> None:
    """Feed the Case I into the app's own pipeline, as an import would."""
    case = fly_case1()
    pipeline = app.state.pipeline
    ingestor = TrackIngestor(
        app.state.session_factory,
        landing_listener=pipeline.handle_landing,
        landing_finalize_listener=pipeline.finalize_landing,
        detection_config=pipeline._config.to_detection_config(),  # noqa: SLF001
        deck_altitude_for=pipeline.deck_altitude_for,
    )
    try:
        for line in case.lines:
            await ingestor.handle_line(line)
    finally:
        await ingestor.close()


def test_a_turning_ship_does_not_bend_the_g() -> None:
    """Load factor comes from an earth-fixed frame, not the moving deck.

    The deck frame rotates with a turning ship, and differentiating a
    position in a rotating frame adds Coriolis and centrifugal terms. Here
    the jet flies a level 3 G circle over the ground while the ship turns
    1 deg/s underneath it: the G read off the moving-deck coordinates is
    visibly wrong, the one read off the earth-fixed copy is 3.
    """
    lat0, lon0 = 42.0, 38.0
    speed, g = 150.0, 3.0
    radius = speed**2 / (G0 * math.sqrt(g * g - 1.0))
    omega = speed / radius
    ship: list[tuple[float, float, float, float, float, float]] = []
    samples: list[TrackSample] = []
    for step in range(0, 151):
        t = step * 0.2
        heading = (40.0 + 1.0 * t) % 360.0
        ship_lat, ship_lon = offset_position(lat0, lon0, 40.0, 5.0 * t, 0.0)
        ship.append((t, ship_lat, ship_lon, 0.0, heading, 0.0))
        angle = omega * t
        east, north = radius * math.sin(angle), -radius * math.cos(angle) + radius
        lat, lon = offset_position(lat0, lon0, 0.0, 3000.0 + north, -1500.0 + east)
        samples.append(
            TrackSample(time=t, latitude=lat, longitude=lon, altitude=250.0, speed=speed)
        )
    touchdown = Touchdown(
        time=30.0, latitude=lat0, longitude=lon0, altitude=22.0, heading=40.0,
        speed=70.0, aoa=None, descent_rate_ms=3.5, ground_altitude_m=20.15,
        surface_is_deck=True,
    )
    event = LandingEvent(
        touchdown=touchdown, kind="carrier", outcome="full_stop",
        carrier_obj_id="C1", carrier_name="CVN_73", approach=samples,
        carrier_track=ship, carrier_latitude=lat0, carrier_longitude=lon0,
        carrier_altitude_m=0.0, carrier_heading_deg=40.0,
    )
    geometry = load_carrier_geometry_book(CARRIERS_YAML).resolve("CVN_73")
    analysis = build_approach_analysis(event, 3.5, geometry=geometry)

    annotate_kinematics(analysis)
    earth = [s.load_factor for s in analysis.samples[10:-10] if s.load_factor is not None]
    assert earth and max(abs(n - g) for n in earth) < 0.05

    moving = ApproachAnalysis.from_dict(analysis.as_dict())
    for s in moving.samples:
        s.fixed_along = s.fixed_lateral = None
    annotate_kinematics(moving)
    deck = [s.load_factor for s in moving.samples[10:-10] if s.load_factor is not None]
    assert max(abs(n - g) for n in deck) > 0.3, "the demonstration lost its point"


def test_the_ships_own_track_rides_on_the_event() -> None:
    """The detector hands the grader the deck's track, not just its last fix."""
    from app.detection.detector import analyze_track

    ship = CarrierState(
        obj_id="C1", name="CVN_73", type="Sea+Watercraft+AircraftCarrier",
        samples=[(float(t), 42.0 + t * 1e-5, 38.0, 0.0, 0.0, 0.0) for t in range(-400, 60)],
    )
    samples = [
        TrackSample(time=float(t), latitude=42.0 + t * 1e-5 - 0.0005, longitude=38.0,
                    altitude=22.0 if t >= 0 else 22.0 + 3.0 * -t, agl=None, speed=60.0)
        for t in range(-100, 40)
    ]
    events = analyze_track(
        samples, None, {"C1": ship}, deck_altitude_for=lambda _c: 20.15
    )
    assert len(events) == 1
    track = events[0].carrier_track
    assert track[0][0] <= events[0].approach[0].time
    assert track[-1][0] >= events[0].approach[-1].time
