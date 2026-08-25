"""Tests for the grading engine: shared deviations, land grader, LSO grader."""

from __future__ import annotations

import pytest

from app.detection.detector import TrackSample, analyze_track
from app.grading.config import load_grading_config
from app.grading.deviations import build_approach_analysis, estimate_course_deg
from app.grading.land_grader import grade_land_landing
from app.grading.lso_grader import grade_carrier_approach
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
    assert estimate_course_deg(samples, 330.0) == pytest.approx(330.0)
    # Sanity check the scenario is meaningful: the position-only fallback
    # really does disagree substantially with the touchdown heading.
    angular_diff = abs(((330.0 - position_bearing + 180) % 360) - 180)
    assert angular_diff > 30


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
    # the characteristic burble sink.
    event = _carrier_event(pre_touchdown_descent_ms=7.0)
    analysis = build_approach_analysis(event, 3.5)
    result = grade_carrier_approach(analysis, CONFIG)

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
