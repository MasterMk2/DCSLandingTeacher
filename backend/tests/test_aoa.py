"""Approach angle of attack: the estimate, the per-airframe bands, the grading."""

from __future__ import annotations

import math

import pytest

from app.grading.aoa import (
    AoaBand,
    aoa_band_for,
    aoa_score,
    aoa_verdict,
    body_aoa_deg,
    estimate_aoa_deg,
)
from app.grading.config import load_grading_config
from app.grading.deviations import ApproachAnalysis, DeviationSample
from app.grading.land_grader import grade_land_landing
from app.grading.lso_grader import grade_carrier_approach
from tests.conftest import GRADING_YAML

CONFIG = load_grading_config(GRADING_YAML)


def _velocity_for(aoa: float, heading: float, pitch: float, roll: float, speed: float):
    """The (x north, y east, z up) velocity of an aircraft flying ``aoa``.

    Built the other way round from :func:`body_aoa_deg`: a body-axis airflow
    at ``aoa`` (no sideslip) turned out to the earth frame with the attitude.
    """
    a = math.radians(aoa)
    u, w = speed * math.cos(a), speed * math.sin(a)
    psi, theta, phi = (math.radians(x) for x in (heading, pitch, roll))
    cpsi, spsi = math.cos(psi), math.sin(psi)
    cth, sth = math.cos(theta), math.sin(theta)
    cphi, sphi = math.cos(phi), math.sin(phi)
    # Body -> NED is the transpose of the NED -> body rotation.
    vn = cth * cpsi * u + (cphi * sth * cpsi + sphi * spsi) * w
    ve = cth * spsi * u + (cphi * sth * spsi - sphi * cpsi) * w
    vd = -sth * u + cphi * cth * w
    return (vn, ve, -vd)


@pytest.mark.parametrize(
    ("aoa", "heading", "pitch", "roll"),
    [
        (8.1, 0.0, 5.1, 0.0),     # Hornet on the 3 deg ball
        (10.4, 37.0, 7.0, 0.0),   # Tomcat, any heading
        (11.0, 250.0, 8.0, 25.0), # F-16 in the base turn
        (13.0, 90.0, -2.0, -40.0),
    ],
)
def test_the_body_axis_aoa_round_trips(aoa, heading, pitch, roll) -> None:
    velocity = _velocity_for(aoa, heading, pitch, roll, 70.0)
    assert body_aoa_deg(velocity, heading, pitch, roll) == pytest.approx(aoa, abs=1e-6)


def test_on_a_wings_level_glidepath_aoa_is_pitch_plus_the_descent_angle() -> None:
    """The textbook relation the full rotation has to reduce to: 5.1 deg nose
    up on a 3 deg path is 8.1 deg of AOA."""
    speed = 70.0
    gamma = math.radians(-3.0)
    velocity = (speed * math.cos(gamma), 0.0, speed * math.sin(gamma))
    assert estimate_aoa_deg(velocity, 0.0, 5.1, 0.0) == pytest.approx(8.1, abs=1e-6)


def test_a_crab_does_not_read_as_aoa() -> None:
    """Crabbed into a crosswind, the ground track is off the nose; the extra
    sideways velocity lies along the wing, not in the plane AOA is measured in.
    With the recorded heading the estimate is unchanged (the fallback that
    assumes the nose follows the track is the only one that would see it)."""
    heading = 10.0
    air = _velocity_for(8.1, heading, 5.1, 0.0, 70.0)
    crosswind = 12.0  # m/s, straight across the nose
    across = math.radians(heading + 90.0)
    ground = (
        air[0] + crosswind * math.cos(across),
        air[1] + crosswind * math.sin(across),
        air[2],
    )
    assert estimate_aoa_deg(ground, heading, 5.1, 0.0) == pytest.approx(8.1, abs=1e-6)
    assert estimate_aoa_deg(ground, None, 5.1, 0.0) == pytest.approx(8.1, abs=0.1)


def test_no_estimate_where_aoa_is_not_defined() -> None:
    assert estimate_aoa_deg((10.0, 0.0, -1.0), 0.0, 5.0, 0.0) is None  # hover
    assert estimate_aoa_deg((70.0, 0.0, -3.0), 0.0, None, 0.0) is None
    assert estimate_aoa_deg((70.0, 0.0, -3.0), 0.0, 5.0, 13000.0) is None  # broken roll


@pytest.mark.parametrize(
    ("name", "band"),
    [
        ("FA-18C_hornet", "hornet"),
        ("EA-18G", "hornet"),
        ("F-14B", "tomcat"),
        ("F-14BU", "tomcat"),
        ("F-14A-135-GR", "tomcat"),
        ("F-16C_50", "falcon"),
        ("M-2000C", "mirage2000"),
        ("AJS37", "viggen"),
        ("AV8BNA", "harrier"),
        # No sourced band: not judged on AOA at all.
        ("F-15C", None),
        ("F-15ESE", None),
        ("Su-33", None),
        ("Su-27", None),
        ("A-10C_2", None),
        ("F-4E-45MC", None),
        ("F-5E-3", None),
        ("Mirage-F1BE", None),
        ("A6E", None),
        ("TF-51D", None),
        ("UH-1H", None),
        (None, None),
    ],
)
def test_airframes_get_the_band_their_manual_gives(name, band) -> None:
    found = aoa_band_for(name, CONFIG.aoa_bands)
    assert (found.name if found else None) == band


HORNET = AoaBand("hornet", 8.1, 7.4, 8.8, 6.9, 9.3)


def test_the_score_is_full_on_speed_and_falls_away_on_both_sides() -> None:
    assert aoa_score(8.1, HORNET) == 100.0
    assert aoa_score(7.4, HORNET) == 100.0
    assert aoa_score(6.9, HORNET) == pytest.approx(65.0)
    assert aoa_score(9.3, HORNET) == pytest.approx(65.0)
    fast_side = [aoa_score(x / 10, HORNET) for x in range(74, 30, -1)]
    assert fast_side == sorted(fast_side, reverse=True)
    assert min(fast_side) == 5.0


def test_a_narrow_slightly_zone_does_not_make_the_far_side_a_cliff() -> None:
    """The Hornet's slightly-fast zone is 0.5 deg; past it the score still
    takes a whole degree to fall from 65 to 30, so the estimate's own spread
    (p90 +0.5 to +0.7 deg against recorded AOA) cannot swing a landing by 60
    points."""
    assert aoa_score(6.9 - 0.5, HORNET) == pytest.approx(65.0 - 17.5)
    assert aoa_score(6.9 - 1.0, HORNET) == pytest.approx(30.0)


def test_verdicts_name_fast_as_low_aoa() -> None:
    assert aoa_verdict(8.0, HORNET) == "on speed"
    assert aoa_verdict(7.1, HORNET) == "slightly fast"
    assert aoa_verdict(6.0, HORNET) == "fast"
    assert aoa_verdict(9.0, HORNET) == "slightly slow"
    assert aoa_verdict(10.0, HORNET) == "slow"


def _straight_in(
    *, aoa: float, airframe: str, kind: str = "land", speed: float = 68.0
) -> ApproachAnalysis:
    """A 60 s, 5 Hz straight-in down 3 deg, flown at a constant ``aoa``.

    The attitude is what such a pass records: pitch = AOA + flight-path
    angle, wings level, nose on the course.
    """
    touchdown = 200.0
    tan3 = math.tan(math.radians(3.0))
    samples = []
    for step in range(301):
        t = touchdown - 60.0 + step * 0.2
        dtg = speed * (touchdown - t)
        samples.append(
            DeviationSample(
                time=t,
                distance_to_go=dtg,
                glideslope_deviation=0.0,
                centerline_deviation=0.0,
                speed=speed,
                agl=dtg * tan3,
                signed_distance_to_go=dtg,
                distance_to_threshold=dtg - 300.0,
                pitch=aoa - 3.0,
                roll=0.0,
                heading=90.0,
            )
        )
    return ApproachAnalysis(
        kind=kind,
        outcome="full_stop",
        glideslope_deg=3.0,
        course_deg=90.0,
        touchdown_time=touchdown,
        touchdown_speed_ms=speed * 0.95,
        touchdown_descent_rate_ms=2.0,
        geometry={"kind": "runway", "airbase": "TEST", "name": "09"} if kind == "land" else None,
        approach_pattern="straight_in",
        airframe=airframe,
        samples=samples,
    )


def test_a_hornet_flown_on_speed_scores_its_aoa_in_full() -> None:
    result = grade_land_landing(_straight_in(aoa=8.1, airframe="FA-18C_hornet"), CONFIG)
    aoa = next(c for c in result.components if c.name == "aoa")
    assert aoa.score == 100.0
    assert aoa.evidence["median_aoa_deg"] == pytest.approx(8.1, abs=0.05)
    assert aoa.evidence["method"] == "estimated"
    assert aoa.evidence["verdict"] == "on speed"
    assert "迎角はオンスピードだった" in result.comment
    assert result.metrics["aoa_band"] == "hornet"


def test_a_fast_hornet_loses_points_and_is_told_why() -> None:
    on_speed = grade_land_landing(_straight_in(aoa=8.1, airframe="FA-18C_hornet"), CONFIG)
    fast = grade_land_landing(_straight_in(aoa=6.0, airframe="FA-18C_hornet"), CONFIG)
    assert fast.score < on_speed.score
    assert next(c for c in fast.components if c.name == "aoa").evidence["verdict"] == "fast"
    assert "ファイナルの迎角は 6.0° でオンスピード（7.4–8.8°）より小さく、速すぎた" in fast.comment


def test_an_airframe_without_a_band_is_graded_exactly_as_before() -> None:
    """No row in aoa_bands -> no component, so the weights renormalise over
    the same four components as before and the score cannot move."""
    analysis = _straight_in(aoa=8.1, airframe="F-15C")
    result = grade_land_landing(analysis, CONFIG)
    assert all(c.name != "aoa" for c in result.components)
    assert result.metrics["aoa_band"] is None

    from app.grading.config import apply_config_overrides

    without = apply_config_overrides(CONFIG, {"aoa_bands": {}})
    assert grade_land_landing(_straight_in(aoa=8.1, airframe="F-15C"), without).score == (
        result.score
    )


def test_no_aoa_component_when_the_record_cannot_give_one() -> None:
    """A Harrier's vertical landing, or a row stored before attitude was kept:
    nothing to estimate from. That is a property of the record, not of the
    flying, so the landing is graded and worded exactly as an airframe
    without a band -- no "could not measure AOA" note, no hedged verdict."""
    analysis = _straight_in(aoa=8.1, airframe="F-16C_50")
    for sample in analysis.samples:
        sample.pitch = None
        sample.roll = None
    result = grade_land_landing(analysis, CONFIG)
    assert all(c.name != "aoa" for c in result.components)
    assert "迎角" not in result.comment
    assert result.metrics["aoa_band"] is None


def test_a_recorded_aoa_wins_over_the_estimate() -> None:
    """Client-side recordings carry AOA; when they do, that is what is judged."""
    analysis = _straight_in(aoa=8.1, airframe="FA-18C_hornet")
    for sample in analysis.samples:
        sample.aoa = 6.0
    aoa = next(
        c for c in grade_land_landing(analysis, CONFIG).components if c.name == "aoa"
    )
    assert aoa.evidence["method"] == "recorded"
    assert aoa.evidence["verdict"] == "fast"


def test_a_carrier_pass_flown_fast_is_called_fast_on_aoa() -> None:
    result = grade_carrier_approach(
        _straight_in(aoa=6.0, airframe="FA-18C_hornet", kind="carrier"), CONFIG
    )
    fast = next(f for f in result.factors if f.name == "FAST")
    assert fast.severity == "major"
    assert fast.evidence["median_aoa_deg"] == pytest.approx(6.0, abs=0.05)
    assert fast.evidence["method"] == "estimated"


def test_a_carrier_pass_a_little_slow_is_a_minor_slow() -> None:
    result = grade_carrier_approach(
        _straight_in(aoa=9.0, airframe="FA-18C_hornet", kind="carrier"), CONFIG
    )
    slow = next(f for f in result.factors if f.name == "SLOW")
    assert slow.severity == "minor"
    assert all(f.name != "FAST" for f in result.factors)


def test_a_carrier_pass_on_speed_has_no_speed_factor_even_if_it_slowed_down() -> None:
    """On-speed AOA is the LSO's "on speed"; the touchdown/approach speed ratio
    that stood in for it no longer fires for airframes with a band."""
    analysis = _straight_in(aoa=8.1, airframe="FA-18C_hornet", kind="carrier")
    analysis.touchdown_speed_ms = analysis.samples[-1].speed * 0.85  # ratio < 0.93
    result = grade_carrier_approach(analysis, CONFIG)
    assert all(f.name not in ("FAST", "SLOW") for f in result.factors)


def test_without_a_band_the_carrier_keeps_the_speed_ratio_rule() -> None:
    analysis = _straight_in(aoa=8.1, airframe="Su-33", kind="carrier")
    analysis.touchdown_speed_ms = analysis.samples[-1].speed * 0.85
    result = grade_carrier_approach(analysis, CONFIG)
    slow = next(f for f in result.factors if f.name == "SLOW")
    assert "speed_ratio" in slow.evidence
