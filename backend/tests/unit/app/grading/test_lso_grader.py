"""Unit tests for app.grading.lso_grader."""

from __future__ import annotations

from app.grading.config import load_grading_config
from app.grading.deviations import (
    build_approach_analysis,
)
from app.grading.lso_grader import grade_carrier_approach
from tests.conftest import GRADING_YAML
from tests.helpers import make_carrier_landing_event

CONFIG = load_grading_config(GRADING_YAML)


def test_lso_perfect_pass_is_ok() -> None:
    event = make_carrier_landing_event()
    analysis = build_approach_analysis(event, 3.5)
    result = grade_carrier_approach(analysis, CONFIG)

    assert result.grade == "OK"
    assert result.factors == []


def test_lso_high_pass_gets_high_factor() -> None:
    event = make_carrier_landing_event(gs_offset_m=5.0)
    analysis = build_approach_analysis(event, 3.5)
    result = grade_carrier_approach(analysis, CONFIG)

    names = [f.name for f in result.factors]
    assert "HIGH" in names
    high = next(f for f in result.factors if f.name == "HIGH")
    assert high.evidence["mean_glideslope_deviation_m"] > high.evidence["threshold_m"]
    assert result.grade == "OK-"


def test_lso_low_pass_gets_low_factor() -> None:
    event = make_carrier_landing_event(gs_offset_m=-3.0)
    analysis = build_approach_analysis(event, 3.5)
    result = grade_carrier_approach(analysis, CONFIG)

    names = [f.name for f in result.factors]
    assert "LOW" in names
    assert result.grade == "OK-"


def test_lso_dangerously_low_is_cut() -> None:
    event = make_carrier_landing_event(gs_offset_m=-6.0)
    analysis = build_approach_analysis(event, 3.5)
    result = grade_carrier_approach(analysis, CONFIG)

    assert result.grade == "CUT"
    assert any(f.name == "LOW" for f in result.factors)


def test_lso_slow_pass_gets_slow_factor() -> None:
    event = make_carrier_landing_event(touchdown_speed_ms=55.0)  # vs ~70 m/s approach
    analysis = build_approach_analysis(event, 3.5)
    result = grade_carrier_approach(analysis, CONFIG)

    slow = next(f for f in result.factors if f.name == "SLOW")
    assert slow.evidence["speed_ratio"] < slow.evidence["threshold_ratio"]
    assert result.grade == "OK-"


def test_lso_fast_pass_gets_fast_factor() -> None:
    event = make_carrier_landing_event(touchdown_speed_ms=85.0)
    analysis = build_approach_analysis(event, 3.5)
    result = grade_carrier_approach(analysis, CONFIG)

    assert any(f.name == "FAST" for f in result.factors)


def test_lso_offline_pass_gets_offline_factor() -> None:
    event = make_carrier_landing_event(lateral_offset_m=8.0)
    analysis = build_approach_analysis(event, 3.5)
    result = grade_carrier_approach(analysis, CONFIG)

    offline = next(f for f in result.factors if f.name == "OFFLINE")
    assert offline.evidence["max_lateral_deviation_m"] > offline.evidence["threshold_m"]


def test_lso_bolter_is_no_grade() -> None:
    event = make_carrier_landing_event(outcome="touch_and_go")
    assert event.outcome == "bolter"
    analysis = build_approach_analysis(event, 3.5)
    result = grade_carrier_approach(analysis, CONFIG)

    assert result.grade == "_NO_GRADE_"
    assert any(f.name == "BOLTER" for f in result.factors)


def test_lso_multiple_majors_cut() -> None:
    # HIGH + FAST + OFFLINE at once -> three majors -> CUT.
    event = make_carrier_landing_event(
        gs_offset_m=5.0, lateral_offset_m=8.0, touchdown_speed_ms=85.0
    )
    analysis = build_approach_analysis(event, 3.5)
    result = grade_carrier_approach(analysis, CONFIG)

    assert result.grade == "CUT"


def test_lso_disabled_factors_never_emitted() -> None:
    event = make_carrier_landing_event()
    analysis = build_approach_analysis(event, 3.5)
    result = grade_carrier_approach(analysis, CONFIG)

    disabled = {
        name
        for name, cfg in CONFIG.lso_grading["factors"].items()
        if isinstance(cfg, dict) and cfg.get("enabled") is False
    }
    assert disabled  # config declares them
    assert all(f.name not in disabled for f in result.factors)


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

    event = make_carrier_landing_event(pre_touchdown_descent_ms=7.0)
    analysis = build_approach_analysis(event, 3.5)
    result = grade_carrier_approach(analysis, enabled_config)

    burble = next((f for f in result.factors if f.name == "BURBLE"), None)
    assert burble is not None
    assert burble.severity == "minor"
    assert burble.evidence["method"] == "descent_rate_increase_heuristic"
    assert burble.evidence["extra_descent_ms"] >= burble.evidence["threshold_ms"]
    assert burble.evidence["recent_descent_ms"] > burble.evidence["baseline_descent_ms"]


def test_lso_smooth_pass_has_no_burble() -> None:
    event = make_carrier_landing_event()
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

    event = make_carrier_landing_event(pre_touchdown_descent_ms=7.0)
    analysis = build_approach_analysis(event, 3.5)
    result = grade_carrier_approach(analysis, disabled_config)

    assert all(f.name != "BURBLE" for f in result.factors)


def test_lso_burble_disabled_by_default() -> None:
    """BURBLE must be disabled out of the box (Issue #23).

    The heuristic is unvalidated and ACMI 2.2 has no wind data, so it must
    not influence grades until tuned against real DCS approach data.
    """
    event = make_carrier_landing_event(pre_touchdown_descent_ms=7.0)
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
        samples=[
            DeviationSample(
                time=-1.0,
                distance_to_go=70.0,
                glideslope_deviation=0.0,
                centerline_deviation=0.0,
                agl=6.0,
            )
        ],
    )
    assert _detect_burble(analysis, {"enabled": True, "extra_descent_ms": 1.5}) is None


def test_carrier_grading_still_works_when_the_yaml_is_missing() -> None:
    """The production path: no config file at all, only the code defaults.

    Every other LSO test loads the YAML, so an empty ``factors`` table was
    invisible to the suite while being exactly what the live server ran.
    """
    from app.grading.config import GradingConfig

    defaults_only = GradingConfig({})
    assert defaults_only.lso_grading["factors"], "no factor can fire without thresholds"

    high = grade_carrier_approach(_carrier_event_analysis(gs_offset_m=6.0), defaults_only)
    assert [f.name for f in high.factors] == ["HIGH"]
    assert high.grade == "OK-"

    clean = grade_carrier_approach(_carrier_event_analysis(), defaults_only)
    assert clean.factors == []
    assert clean.grade == "OK"


def _carrier_event_analysis(**kwargs):
    event = make_carrier_landing_event(**kwargs)
    return build_approach_analysis(event, CONFIG.carrier_glideslope_deg)


def test_a_carrier_grade_says_which_geometry_produced_it() -> None:
    """Every LSO deviation is measured against the ramp, so the grade is only
    worth what the geometry is -- and both weak cases were invisible in it.

    Nothing pinned this metric when it was added, so the values were free to
    drift or vanish silently.
    """
    from app.grading.carriers import FlolsGeometry

    # No entry for the ship: distances are referenced to the touchdown point.
    fallback = grade_carrier_approach(_carrier_event_analysis(), CONFIG)
    assert fallback.metrics["geometry_confidence"] == "fallback"
    assert fallback.metrics["flols_geometry"]["source"] == "touchdown_reference_fallback"

    geometry = FlolsGeometry(
        key="stennis",
        deck_altitude_m=19.5,
        ramp_along_m=-140.0,
        ramp_lateral_m=-10.0,
        glideslope_deg=3.5,
        landing_course_offset_deg=9.0,
        beam_width_m=12.0,
        validated=False,
    )
    analysis = _carrier_event_analysis()
    analysis.geometry = geometry.as_dict()
    assert grade_carrier_approach(analysis, CONFIG).metrics["geometry_confidence"] == "unvalidated"

    # And the only branch the shipped config can never produce today.
    analysis.geometry = {**geometry.as_dict(), "validated": True}
    assert grade_carrier_approach(analysis, CONFIG).metrics["geometry_confidence"] == "validated"


def test_no_shipped_carrier_entry_claims_to_be_validated() -> None:
    """Guards the comment in lso_grader: "validated" is unreachable from the
    file we ship. If someone adds measured geometry, this test is where they
    find out the claim needs updating."""
    from app.grading.carriers import load_carrier_geometry_book
    from tests.conftest import REPO_ROOT

    book = load_carrier_geometry_book(REPO_ROOT / "config" / "carriers.yaml")
    for _name, _type, geometry in book._entries.values():
        assert not geometry.validated, (
            f"{geometry.key} now claims validated geometry -- update the "
            "geometry_confidence comment in lso_grader.py"
        )
