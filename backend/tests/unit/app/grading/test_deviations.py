"""Unit tests for app.grading.deviations."""

from __future__ import annotations

import math

import pytest

from app.detection.detector import (
    TrackSample,
    analyze_track,
)
from app.grading.config import load_grading_config
from app.grading.deviations import (
    DeviationSample,
    build_approach_analysis,
    estimate_course_deg,
)
from tests.conftest import GRADING_YAML
from tests.helpers import (
    DECK_ALTITUDE_M,
    make_carrier_landing_event,
    make_approach_samples,
)

CONFIG = load_grading_config(GRADING_YAML)


def test_glideslope_error_is_angular_not_a_fixed_distance() -> None:
    far = DeviationSample(
        time=0.0,
        distance_to_go=1500.0,
        glideslope_deviation=-20.0,
        centerline_deviation=0.0,
        agl=1500.0 * math.tan(math.radians(3.0)) - 20.0,
    )
    near = DeviationSample(
        time=0.0,
        distance_to_go=500.0,
        glideslope_deviation=-20.0,
        centerline_deviation=0.0,
        agl=500.0 * math.tan(math.radians(3.0)) - 20.0,
    )
    far_error = far.glideslope_error_deg(3.0)
    near_error = near.glideslope_error_deg(3.0)
    assert far_error is not None and near_error is not None
    assert far_error == pytest.approx(-0.76, abs=0.05)
    assert near_error == pytest.approx(-2.29, abs=0.05)
    assert abs(near_error) > abs(far_error) * 2.5


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


def test_deviation_series_ideal_approach() -> None:
    event = make_carrier_landing_event()
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
    event = make_carrier_landing_event()
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


def test_land_analysis_uses_three_degree_reference() -> None:
    """The deviation series for a land landing is built on the 3.0-degree slope."""
    event = _land_event(pre_touchdown_descent_ms=1.2)
    analysis = build_approach_analysis(event, CONFIG.land_glideslope_deg)
    assert analysis.glideslope_deg == pytest.approx(3.0)


def test_approach_analysis_from_dict_rejects_malformed_json() -> None:
    """Issue #44: a corrupt stored approach_track must fail loudly, not with an
    opaque TypeError deep in the grader."""
    from app.grading.deviations import ApproachAnalysis

    with pytest.raises(ValueError):
        ApproachAnalysis.from_dict({"samples": "not a list"})
    with pytest.raises(ValueError):
        ApproachAnalysis.from_dict({"samples": [{"time": 1.0}]})


def test_land_course_falls_back_to_heading_when_the_window_is_still_turning() -> None:
    """The stabilized-final track is only trusted while it stays within a
    plausible crab of the touchdown heading. A tight pattern whose last
    seconds are still in the turn produces a track tens of degrees off the
    runway -- exactly the contamination the whole-approach bearing suffered
    from -- so the heading takes over (Issue #26 / MAX_PLAUSIBLE_CRAB_DEG)."""
    # Ground track due north, heading 120: 120 deg apart, far past any crab.
    samples = [
        TrackSample(time=0.0, latitude=34.990, longitude=140.0),
        TrackSample(time=10.0, latitude=34.995, longitude=140.0),
        TrackSample(time=20.0, latitude=35.000, longitude=140.0),
    ]
    assert estimate_course_deg(samples, 120.0, kind="land") == pytest.approx(120.0)

    # A believable 20 deg crab still yields the track, not the heading.
    assert estimate_course_deg(samples, 20.0, kind="land") == pytest.approx(0.0, abs=0.5)


def test_carrier_course_prefers_the_touchdown_heading_over_the_track() -> None:
    """On the boat the aircraft de-crabs onto the angled deck at the ramp, so
    the heading reads the deck course even through a turn onto final."""
    samples = [
        TrackSample(time=0.0, latitude=34.990, longitude=140.0),
        TrackSample(time=10.0, latitude=34.995, longitude=140.0),
        TrackSample(time=20.0, latitude=35.000, longitude=140.0),
    ]
    assert estimate_course_deg(samples, 9.0, kind="carrier") == pytest.approx(9.0)
