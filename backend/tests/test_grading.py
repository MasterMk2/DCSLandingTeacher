"""Tests for the grading engine: shared deviations, land grader, LSO grader."""

from __future__ import annotations

import pytest

from app.detection.detector import TrackSample, analyze_track
from app.grading.config import load_grading_config
from app.grading.deviations import build_approach_analysis, estimate_course_deg
from app.grading.land_grader import grade_land_landing
from app.grading.lso_grader import grade_carrier_approach
from app.pipeline import LandingPipeline
from tests.conftest import GRADING_YAML
from tests.helpers import DECK_ALTITUDE_M, make_approach_samples, make_carrier_state

CONFIG = load_grading_config(GRADING_YAML)


def _carrier_event(gs_offset_m: float = 0.0, **kwargs):
    samples = make_approach_samples(gs_offset_m=gs_offset_m, **kwargs)
    carriers = {"C1": make_carrier_state()}
    events = analyze_track(samples, DECK_ALTITUDE_M, carriers)
    assert len(events) == 1
    return events[0]


def _land_event(**kwargs):
    samples = make_approach_samples(
        glideslope_deg=3.0,
        approach_speed_ms=30.0,
        touchdown_speed_ms=None,
        **kwargs,
    )
    events = analyze_track(samples, DECK_ALTITUDE_M, carriers={})
    assert len(events) == 1
    return events[0]


# ---------------------------------------------------------------------------
# Shared deviation math
# ---------------------------------------------------------------------------


def test_deviation_series_ideal_approach() -> None:
    event = _carrier_event()
    analysis = build_approach_analysis(event, CONFIG.carrier_glideslope_deg)

    assert analysis.kind == "carrier"
    assert analysis.course_deg == pytest.approx(0.0, abs=1.0)
    assert len(analysis.samples) == len(event.approach)

    inbound = [s for s in analysis.samples if s.time < -10]
    for sample in inbound:
        assert sample.glideslope_deviation == pytest.approx(0.0, abs=1.5)
        assert sample.centerline_deviation == pytest.approx(0.0, abs=1.0)
        assert sample.distance_to_go > 500.0


def test_window_excludes_touchdown_sample() -> None:
    event = _carrier_event()
    analysis = build_approach_analysis(event, 3.5)
    window = analysis.window(3.0)
    assert window
    assert all(s.time < analysis.touchdown_time for s in window)


def test_estimate_course_deg_prefers_touchdown_heading_over_curved_track() -> None:
    """A continuous turn onto final (overhead break / tactical initial, the
    normal way fighters land in DCS) makes a two-point position bearing
    over the whole captured approach an unreliable course estimate: it
    cuts across the turn instead of reading the runway heading. The
    aircraft's own heading at touchdown must win whenever ACMI supplied
    it (Issue: production landings showed 160-1200m "centerline
    deviation" while touchdown itself was only ~20-30m off centerline --
    a systematic angular bias from this exact 2-point method)."""
    # Quarter-circle-ish turn: well clear of a straight line from the first
    # to the last point.
    samples = [
        TrackSample(time=0.0, latitude=0.0, longitude=0.0),
        TrackSample(time=10.0, latitude=0.01, longitude=0.01),
        TrackSample(time=20.0, latitude=0.02, longitude=0.005),
    ]
    position_bearing = estimate_course_deg(samples, None)
    # Default kind is "carrier": touchdown heading still wins (de-crabbed at
    # the angled deck).
    assert estimate_course_deg(samples, 330.0) == pytest.approx(330.0)
    # Sanity check the scenario is meaningful: the position-only fallback
    # really does disagree substantially with the touchdown heading.
    angular_diff = abs(((330.0 - position_bearing + 180) % 360) - 180)
    assert angular_diff > 30


def test_estimate_course_deg_land_uses_stabilized_track_not_heading() -> None:
    """Issue #26: a land crosswind approach crabs the heading away from the
    runway course. The runway course equals the ground *track* on the
    stabilized final, so a landed course must follow the track (here ~000,
    straight north), not the crabbed touchdown heading (030)."""
    # Straight-in final due north; positions share a longitude.
    samples = [
        TrackSample(time=0.0, latitude=34.990, longitude=140.0),
        TrackSample(time=10.0, latitude=34.995, longitude=140.0),
        TrackSample(time=20.0, latitude=35.000, longitude=140.0),
    ]
    assert estimate_course_deg(samples, 30.0, kind="land") == pytest.approx(0.0, abs=0.5)
    # Without a track (no positions) the heading is the last-resort fallback.
    empty = [TrackSample(time=0.0, latitude=None, longitude=None)]
    assert estimate_course_deg(empty, 30.0, kind="land") == pytest.approx(30.0)


def test_land_course_avoids_crosswind_crab_bias() -> None:
    """End-to-end: a crabbed land approach grades with an unbiased centerline
    and reports the crosswind crab angle for the UI (Issue #26)."""
    samples = make_approach_samples(
        glideslope_deg=3.0,
        approach_speed_ms=30.0,
        touchdown_speed_ms=None,
    )
    # Crab the heading 30 deg while the ground track stays straight down the
    # runway centerline (course 0).
    for s in samples:
        s.heading = 30.0
    events = analyze_track(samples, DECK_ALTITUDE_M, carriers={})
    assert len(events) == 1
    event = events[0]
    analysis = build_approach_analysis(event, CONFIG.land_glideslope_deg)

    assert analysis.kind == "land"
    # Course follows the runway track, not the crabbed heading.
    assert analysis.course_deg == pytest.approx(0.0, abs=1.0)
    assert analysis.crosswind_crab_deg == pytest.approx(30.0, abs=1.0)

    result = grade_land_landing(analysis, CONFIG)
    # The crab must not leak into the centerline component.
    centerline = next(c for c in result.components if c.name == "centerline")
    assert centerline.evidence["max_abs_deviation_m"] == pytest.approx(0.0, abs=1.0)
    assert result.metrics["crosswind_crab_deg"] == pytest.approx(30.0, abs=1.0)


# ---------------------------------------------------------------------------
# Land grader (FR-4)
# ---------------------------------------------------------------------------


def test_land_grader_smooth_landing_scores_high() -> None:
    event = _land_event(pre_touchdown_descent_ms=1.2)
    analysis = build_approach_analysis(event, CONFIG.land_glideslope_deg)
    result = grade_land_landing(analysis, CONFIG)

    assert result.grade in ("A", "B")
    assert result.score >= 78
    assert result.comment
    evidence = {c.name: c.evidence for c in result.components}
    assert evidence["descent_rate"]["touchdown_descent_rate_fpm"] < 250


def test_land_grader_hard_landing_scores_low() -> None:
    event = _land_event(pre_touchdown_descent_ms=4.5)
    analysis = build_approach_analysis(event, CONFIG.land_glideslope_deg)
    result = grade_land_landing(analysis, CONFIG)

    descent = next(c for c in result.components if c.name == "descent_rate")
    assert descent.score <= 30
    assert result.grade in ("C", "D", "E")


def test_land_grader_centerline_uses_final_segment_not_whole_approach() -> None:
    """A wide turn onto final (typical fighter overhead-break entry) puts
    early samples far off the straight-line course estimate; that must not
    tank the centerline score the way it tanks a real off-axis final. Only
    glideslope was windowed to the final 15s -- centerline used every
    sample in the whole ~60s/2nm approach, including the turn."""
    from app.grading.deviations import ApproachAnalysis, DeviationSample

    touchdown_time = 100.0
    samples = [
        DeviationSample(
            time=float(t),
            distance_to_go=3000.0 - t * 10,
            glideslope_deviation=0.0,
            centerline_deviation=800.0,
            speed=70.0,
            agl=300.0,
        )
        for t in range(0, 40, 5)
    ] + [
        DeviationSample(
            time=float(t),
            distance_to_go=(100 - t) * 30,
            glideslope_deviation=0.0,
            centerline_deviation=2.0,
            speed=70.0,
            agl=(100 - t) * 5,
        )
        for t in range(86, 100)
    ]
    analysis = ApproachAnalysis(
        kind="land",
        outcome="full_stop",
        glideslope_deg=3.0,
        course_deg=0.0,
        touchdown_time=touchdown_time,
        touchdown_speed_ms=70.0,
        touchdown_descent_rate_ms=1.0,
        samples=samples,
    )
    result = grade_land_landing(analysis, CONFIG)
    centerline = next(c for c in result.components if c.name == "centerline")
    assert centerline.evidence["max_abs_deviation_m"] == pytest.approx(2.0)
    assert centerline.score >= 90


def test_land_grader_off_centerline_penalized() -> None:
    event = _land_event(lateral_offset_m=25.0, pre_touchdown_descent_ms=1.2)
    analysis = build_approach_analysis(event, CONFIG.land_glideslope_deg)
    result = grade_land_landing(analysis, CONFIG)

    centerline = next(c for c in result.components if c.name == "centerline")
    assert centerline.score < 40
    smooth = grade_land_landing(
        build_approach_analysis(_land_event(pre_touchdown_descent_ms=1.2), 3.0), CONFIG
    )
    assert result.score < smooth.score


def test_land_grader_comment_states_deviations_in_feet() -> None:
    """Issue D-4: 高度・偏差は ft で見せる。コメント文だけメートルが残っていた。"""
    event = _land_event(lateral_offset_m=25.0, gs_offset_m=10.0,
                        pre_touchdown_descent_ms=1.2)
    analysis = build_approach_analysis(event, CONFIG.land_glideslope_deg)
    result = grade_land_landing(analysis, CONFIG)

    assert "ft" in result.comment
    assert " m）" not in result.comment
    assert " m ずれた" not in result.comment

    # 文面の数値が実際に ft へ換算されている (m のまま出ていない) こと。
    centerline = next(c for c in result.components if c.name == "centerline")
    dev_m = centerline.evidence["max_abs_deviation_m"]
    assert f"{dev_m / 0.3048:.0f} ft" in result.comment


def _analysis_with_gs_deviations(devs: list[float]):
    """指定のグライドスロープ偏差を持つ analysis を組む。

    偏差は AGL に埋め込む: 採点は角度 (AGL と距離の比) で行うので、
    ``glideslope_deviation`` だけを差し替えて AGL を放置すると幾何的に
    矛盾したサンプルになり、何をテストしているのか分からなくなる。
    距離は ±30 m の偏差が現実的な範囲に収まるよう最終進入相当に取る。
    """
    import math

    from app.grading.deviations import ApproachAnalysis, DeviationSample

    touchdown_time = 100.0
    tan_slope = math.tan(math.radians(3.0))
    samples = []
    for i, dev in enumerate(devs):
        distance_to_go = 1250.0 + (len(devs) - 1 - i) * 250.0
        samples.append(
            DeviationSample(
                time=touchdown_time - len(devs) + i,
                distance_to_go=distance_to_go,
                glideslope_deviation=dev,
                centerline_deviation=1.0,
                speed=70.0,
                agl=distance_to_go * tan_slope + dev,
            )
        )
    return ApproachAnalysis(
        kind="land",
        outcome="full_stop",
        glideslope_deg=3.0,
        course_deg=0.0,
        touchdown_time=touchdown_time,
        touchdown_speed_ms=70.0,
        touchdown_descent_rate_ms=1.0,
        samples=samples,
    )


def test_land_grader_says_low_when_the_approach_was_below_glideslope() -> None:
    """「低め」が到達不能だった: 向きを絶対値平均から決めていたため、
    実際には下を飛んでいた進入まで一律「高め」と言われていた。"""
    result = grade_land_landing(_analysis_with_gs_deviations([-30.0] * 12), CONFIG)
    assert "低め" in result.comment
    assert "高め" not in result.comment

    high = grade_land_landing(_analysis_with_gs_deviations([30.0] * 12), CONFIG)
    assert "高め" in high.comment
    assert "低め" not in high.comment


def test_land_grader_reports_wander_instead_of_a_direction_when_oscillating() -> None:
    """上下に振れて一方向に寄っていない進入を「高め」「低め」と言い切らない。"""
    result = grade_land_landing(
        _analysis_with_gs_deviations([30.0, -30.0] * 6), CONFIG
    )
    assert "ばらついた" in result.comment
    assert "高め" not in result.comment and "低め" not in result.comment
    # 振れ幅は採点に効いたままである (相殺されて満点にならない)。
    glideslope = next(c for c in result.components if c.name == "glideslope")
    assert glideslope.evidence["mean_abs_deviation_m"] == pytest.approx(30.0)
    assert glideslope.evidence["mean_signed_deviation_m"] == pytest.approx(0.0)
    # 採点に使うのは角度。振れの散らばりが絶対値側に残り、符号付き
    # (トレンド) はそれより十分小さい — これが「一方向に寄っていない」の
    # 判定条件そのもので、講評が向きを言い切らない根拠になっている。
    signed = glideslope.evidence["mean_signed_error_deg"]
    absolute = glideslope.evidence["mean_abs_error_deg"]
    assert absolute > 0.5
    assert abs(signed) < absolute / 2
    assert glideslope.score < 60


def test_pipeline_reload_config_picks_up_file_changes(tmp_path) -> None:
    """Issue #40: editing grading.yaml and reloading applies new thresholds to
    subsequent gradings without a server restart."""
    from pathlib import Path

    config_path = tmp_path / "grading.yaml"
    original = Path(GRADING_YAML).read_text(encoding="utf-8")
    config_path.write_text(original, encoding="utf-8")

    pipeline = LandingPipeline(
        None, load_grading_config(config_path), grading_config_path=config_path
    )
    before = next(
        c
        for c in grade_land_landing(_analysis_with_gs_deviations([2.0] * 12), pipeline._config).components
        if c.name == "glideslope"
    ).score

    # Tighten the poor band so the same offset drops a band.
    modified = original.replace("poor: 5.0", "poor: 2.0")
    assert modified != original
    config_path.write_text(modified, encoding="utf-8")
    pipeline.reload_config()

    after = next(
        c
        for c in grade_land_landing(_analysis_with_gs_deviations([2.0] * 12), pipeline._config).components
        if c.name == "glideslope"
    ).score
    assert after < before


async def test_config_reload_endpoint_requires_and_reloads(tmp_path) -> None:
    """Issue #40: POST /api/config/reload is token-protected and hot-reloads."""
    from pathlib import Path

    from app.api.main import create_app
    from tests.test_auth import make_settings, open_client

    config_path = tmp_path / "grading.yaml"
    original = Path(GRADING_YAML).read_text(encoding="utf-8")
    config_path.write_text(original, encoding="utf-8")

    settings = make_settings(tmp_path, grading_config_path=str(config_path), auth_token="secret")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with await open_client(app) as client:
            # No token -> rejected.
            denied = await client.post("/api/config/reload")
            assert denied.status_code == 401
            # With token -> reloaded.
            ok = await client.post(
                "/api/config/reload", headers={"X-Auth-Token": "secret"}
            )
            assert ok.status_code == 200
            assert ok.json()["reloaded"] is True


def test_land_grader_rms_penalizes_oscillation_more_than_steady_offset() -> None:
    """Issue #35: 同じ絶対値平均でも、振動する進入は RMS でより低く評価される。"""
    steady = grade_land_landing(
        _analysis_with_gs_deviations([2.0] * 12), CONFIG
    )
    # 2m を中心に ±3m 振動する進入。MAD は 2m だが RMS は約 3.6m。
    oscillating = grade_land_landing(
        _analysis_with_gs_deviations([-1.0, 5.0] * 6), CONFIG
    )
    steady_gs = next(c for c in steady.components if c.name == "glideslope")
    osc_gs = next(c for c in oscillating.components if c.name == "glideslope")
    assert osc_gs.evidence["rms_deviation_final_15s_m"] > steady_gs.evidence["rms_deviation_final_15s_m"]
    assert osc_gs.score < steady_gs.score


def test_land_grader_letter_bands() -> None:
    letters = CONFIG.land_grading["letters"]
    assert letters["A"] > letters["B"] > letters["C"] > letters["D"]


# ---------------------------------------------------------------------------
# LSO grader (FR-3)
# ---------------------------------------------------------------------------


def test_lso_perfect_pass_is_ok() -> None:
    event = _carrier_event()
    analysis = build_approach_analysis(event, 3.5)
    result = grade_carrier_approach(analysis, CONFIG)

    assert result.grade == "OK"
    assert result.factors == []


def test_lso_high_pass_gets_high_factor() -> None:
    event = _carrier_event(gs_offset_m=5.0)
    analysis = build_approach_analysis(event, 3.5)
    result = grade_carrier_approach(analysis, CONFIG)

    names = [f.name for f in result.factors]
    assert "HIGH" in names
    high = next(f for f in result.factors if f.name == "HIGH")
    assert high.evidence["mean_glideslope_deviation_m"] > high.evidence["threshold_m"]
    assert result.grade == "OK-"


def test_lso_low_pass_gets_low_factor() -> None:
    event = _carrier_event(gs_offset_m=-3.0)
    analysis = build_approach_analysis(event, 3.5)
    result = grade_carrier_approach(analysis, CONFIG)

    names = [f.name for f in result.factors]
    assert "LOW" in names
    assert result.grade == "OK-"


def test_lso_dangerously_low_is_cut() -> None:
    event = _carrier_event(gs_offset_m=-6.0)
    analysis = build_approach_analysis(event, 3.5)
    result = grade_carrier_approach(analysis, CONFIG)

    assert result.grade == "CUT"
    assert any(f.name == "LOW" for f in result.factors)


def test_lso_slow_pass_gets_slow_factor() -> None:
    event = _carrier_event(touchdown_speed_ms=55.0)  # vs ~70 m/s approach
    analysis = build_approach_analysis(event, 3.5)
    result = grade_carrier_approach(analysis, CONFIG)

    slow = next(f for f in result.factors if f.name == "SLOW")
    assert slow.evidence["speed_ratio"] < slow.evidence["threshold_ratio"]
    assert result.grade == "OK-"


def test_lso_fast_pass_gets_fast_factor() -> None:
    event = _carrier_event(touchdown_speed_ms=85.0)
    analysis = build_approach_analysis(event, 3.5)
    result = grade_carrier_approach(analysis, CONFIG)

    assert any(f.name == "FAST" for f in result.factors)


def test_lso_offline_pass_gets_offline_factor() -> None:
    event = _carrier_event(lateral_offset_m=8.0)
    analysis = build_approach_analysis(event, 3.5)
    result = grade_carrier_approach(analysis, CONFIG)

    offline = next(f for f in result.factors if f.name == "OFFLINE")
    assert offline.evidence["max_lateral_deviation_m"] > offline.evidence["threshold_m"]


def test_lso_bolter_is_no_grade() -> None:
    event = _carrier_event(outcome="touch_and_go")
    assert event.outcome == "bolter"
    analysis = build_approach_analysis(event, 3.5)
    result = grade_carrier_approach(analysis, CONFIG)

    assert result.grade == "_NO_GRADE_"
    assert any(f.name == "BOLTER" for f in result.factors)


def test_lso_multiple_majors_cut() -> None:
    # HIGH + FAST + OFFLINE at once -> three majors -> CUT.
    event = _carrier_event(
        gs_offset_m=5.0, lateral_offset_m=8.0, touchdown_speed_ms=85.0
    )
    analysis = build_approach_analysis(event, 3.5)
    result = grade_carrier_approach(analysis, CONFIG)

    assert result.grade == "CUT"


def test_lso_disabled_factors_never_emitted() -> None:
    event = _carrier_event()
    analysis = build_approach_analysis(event, 3.5)
    result = grade_carrier_approach(analysis, CONFIG)

    disabled = {
        name
        for name, cfg in CONFIG.lso_grading["factors"].items()
        if isinstance(cfg, dict) and cfg.get("enabled") is False
    }
    assert disabled  # config declares them
    assert all(f.name not in disabled for f in result.factors)


# ---------------------------------------------------------------------------
# BURBLE heuristic (Issue #4 / O-3)
# ---------------------------------------------------------------------------


def test_lso_burble_detected_on_sudden_sink() -> None:
    # Steady approach at ~4.3 m/s sink, then ~7 m/s over the last 3 s:
    # the characteristic burble sink. BURBLE is disabled by default
    # (Issue #23: unvalidated heuristic, no wind data in ACMI), so enable it
    # explicitly for this test of the heuristic itself.
    from app.grading.config import apply_config_overrides

    enabled_config = apply_config_overrides(
        CONFIG,
        {"lso_grading": {"factors": {"BURBLE": {"enabled": True}}}},
    )

    event = _carrier_event(pre_touchdown_descent_ms=7.0)
    analysis = build_approach_analysis(event, 3.5)
    result = grade_carrier_approach(analysis, enabled_config)

    burble = next((f for f in result.factors if f.name == "BURBLE"), None)
    assert burble is not None
    assert burble.severity == "minor"
    assert burble.evidence["method"] == "descent_rate_increase_heuristic"
    assert (
        burble.evidence["extra_descent_ms"] >= burble.evidence["threshold_ms"]
    )
    assert (
        burble.evidence["recent_descent_ms"]
        > burble.evidence["baseline_descent_ms"]
    )


def test_lso_smooth_pass_has_no_burble() -> None:
    event = _carrier_event()
    analysis = build_approach_analysis(event, 3.5)
    result = grade_carrier_approach(analysis, CONFIG)

    assert all(f.name != "BURBLE" for f in result.factors)
    assert result.grade == "OK"


def test_lso_burble_respects_enabled_flag() -> None:
    from app.grading.config import apply_config_overrides

    disabled_config = apply_config_overrides(
        CONFIG,
        {"lso_grading": {"factors": {"BURBLE": {"enabled": False}}}},
    )

    event = _carrier_event(pre_touchdown_descent_ms=7.0)
    analysis = build_approach_analysis(event, 3.5)
    result = grade_carrier_approach(analysis, disabled_config)

    assert all(f.name != "BURBLE" for f in result.factors)


def test_lso_burble_disabled_by_default() -> None:
    """BURBLE must be disabled out of the box (Issue #23).

    The heuristic is unvalidated and ACMI 2.2 has no wind data, so it must
    not influence grades until tuned against real DCS approach data.
    """
    event = _carrier_event(pre_touchdown_descent_ms=7.0)
    analysis = build_approach_analysis(event, 3.5)
    result = grade_carrier_approach(analysis, CONFIG)

    assert all(f.name != "BURBLE" for f in result.factors)


def test_lso_burble_insufficient_samples_is_silent() -> None:
    # A very short approach segment cannot support the baseline comparison;
    # the detector must stay silent rather than guess.
    from app.grading.deviations import ApproachAnalysis, DeviationSample
    from app.grading.lso_grader import _detect_burble

    analysis = ApproachAnalysis(
        kind="carrier",
        outcome="full_stop",
        glideslope_deg=3.5,
        course_deg=0.0,
        touchdown_time=0.0,
        touchdown_speed_ms=70.0,
        touchdown_descent_rate_ms=2.0,
        samples=[DeviationSample(time=-1.0, distance_to_go=70.0,
                                 glideslope_deviation=0.0,
                                 centerline_deviation=0.0, agl=6.0)],
    )
    assert _detect_burble(
        analysis, {"enabled": True, "extra_descent_ms": 1.5}
    ) is None


# ---------------------------------------------------------------------------
# Issue D-5: land glideslope reference is fixed at 3 degrees.
# ---------------------------------------------------------------------------


def test_land_glideslope_defaults_to_three_degrees() -> None:
    """Land approaches are graded against a 3-degree path (Issue D-5).

    The default must be 3.0 both in the shipped YAML and in the in-code
    fallback, and ``glideslope_for`` must route land events to it.
    """
    from app.grading.config import GradingConfig

    # Shipped configuration.
    assert CONFIG.land_glideslope_deg == pytest.approx(3.0)
    assert CONFIG.glideslope_for("land") == pytest.approx(3.0)
    # Carrier path stays on the FLOLS 3.5-degree datum.
    assert CONFIG.glideslope_for("carrier") == pytest.approx(3.5)

    # In-code fallback when no YAML is present.
    assert GradingConfig({}).land_glideslope_deg == pytest.approx(3.0)


def test_land_analysis_uses_three_degree_reference() -> None:
    """The deviation series for a land landing is built on the 3.0-degree slope."""
    event = _land_event(pre_touchdown_descent_ms=1.2)
    analysis = build_approach_analysis(event, CONFIG.land_glideslope_deg)
    assert analysis.glideslope_deg == pytest.approx(3.0)


async def test_reap_stale_provisionals_finalizes_old_ones(session_factory) -> None:
    """Issue #36: provisionals older than the max age are force-finalized while
    recent ones stay provisional waiting for their final detection."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app.api.main import _settle_stale_provisionals
    from app.models.entities import DcsObject, Flight, Landing

    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        flight = Flight(source_id="default")
        session.add(flight)
        await session.flush()
        obj_old = DcsObject(flight_id=flight.id, acmi_id="A1", first_seen=0.0, last_seen=1.0)
        obj_new = DcsObject(flight_id=flight.id, acmi_id="A2", first_seen=0.0, last_seen=1.0)
        session.add_all([obj_old, obj_new])
        await session.flush()
        session.add_all(
            [
                Landing(
                    flight_id=flight.id, object_id=obj_old.id,
                    outcome_status="provisional",
                    created_at=now - timedelta(seconds=400),
                ),
                Landing(
                    flight_id=flight.id, object_id=obj_new.id,
                    outcome_status="provisional",
                    created_at=now - timedelta(seconds=10),
                ),
            ]
        )
        await session.commit()

    reaped = await _settle_stale_provisionals(
        session_factory, now - timedelta(seconds=300)
    )
    assert reaped == 1

    async with session_factory() as session:
        result = await session.execute(
            select(Landing).order_by(Landing.object_id)
        )
        rows = result.scalars().all()
    assert rows[0].outcome_status == "final"
    assert rows[1].outcome_status == "provisional"


def test_approach_analysis_from_dict_rejects_malformed_json() -> None:
    """Issue #44: a corrupt stored approach_track must fail loudly, not with an
    opaque TypeError deep in the grader."""
    from app.grading.deviations import ApproachAnalysis

    with pytest.raises(ValueError):
        ApproachAnalysis.from_dict({"samples": "not a list"})
    with pytest.raises(ValueError):
        ApproachAnalysis.from_dict({"samples": [{"time": 1.0}]})


async def test_regrade_malformed_track_returns_structured_error(tmp_path) -> None:
    """Issues #44/#42: a corrupt approach_track yields the standard error
    envelope (422 / MALFORMED_APPROACH_TRACK) instead of a raw 500."""
    from app.api.main import create_app
    from app.models.entities import DcsObject, Flight, Landing
    from tests.test_auth import make_settings, open_client

    settings = make_settings(tmp_path, auth_token="secret")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        sf = app.state.session_factory
        async with sf() as s:
            flight = Flight(source_id="default")
            s.add(flight)
            await s.flush()
            obj = DcsObject(
                flight_id=flight.id, acmi_id="A1", first_seen=0.0, last_seen=1.0
            )
            s.add(obj)
            await s.flush()
            landing = Landing(
                flight_id=flight.id,
                object_id=obj.id,
                outcome_status="final",
                approach_track={"samples": "not a list"},
            )
            s.add(landing)
            await s.flush()
            await s.commit()
            lid = landing.id
        async with await open_client(app) as client:
            resp = await client.post(
                f"/api/landings/{lid}/regrade",
                headers={"X-Auth-Token": "secret"},
            )
            assert resp.status_code == 422
            body = resp.json()
            assert body["error"] == "MALFORMED_APPROACH_TRACK"
            assert "message" in body
