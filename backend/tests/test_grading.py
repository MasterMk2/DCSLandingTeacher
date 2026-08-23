"""Tests for the grading engine: shared deviations, land grader, LSO grader."""

from __future__ import annotations

import pytest

from app.detection.detector import analyze_track
from app.grading.config import load_grading_config
from app.grading.deviations import build_approach_analysis
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
