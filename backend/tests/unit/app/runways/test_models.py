"""Runway geometry from DCS, and the grading it feeds."""

from __future__ import annotations

import pytest

from app.detection.geometry import haversine_m
from app.runways.models import Runway, match_runway


def _runways() -> list[Runway]:
    return [
        Runway("Batumi", "13", 41.615005, 41.590119, 10.0, 125.6, 2070.4, 60.0),
        Runway("Batumi", "31", 41.604189, 41.610353, 10.0, 305.6, 2070.4, 60.0),
    ]


def test_aiming_point_lies_past_the_threshold_along_the_runway() -> None:
    runway_13 = next(runway for runway in _runways() if runway.name == "13")
    lat, lon = runway_13.aiming_point(300.0)
    assert haversine_m(lat, lon, runway_13.threshold_lat, runway_13.threshold_lon) == pytest.approx(
        300.0, rel=0.02
    )
    far = next(runway for runway in _runways() if runway.name == "31")
    assert haversine_m(lat, lon, far.threshold_lat, far.threshold_lon) < haversine_m(
        runway_13.threshold_lat,
        runway_13.threshold_lon,
        far.threshold_lat,
        far.threshold_lon,
    )


def test_match_rejects_the_opposite_end_of_the_same_strip() -> None:
    """Both ends are metres apart in heading terms but invert every deviation.

    Matching on proximity alone would happily pick the reciprocal runway for
    a touchdown near the middle of the strip, which flips the sign of the
    centreline deviation and makes the approach look like it came from
    behind.
    """
    runways = _runways()
    runway_13 = next(r for r in runways if r.name == "13")
    matched = match_runway(
        runways,
        runway_13.threshold_lat,
        runway_13.threshold_lon,
        course_deg=125.0,
    )
    assert matched is not None and matched.name == "13"

    reciprocal = match_runway(
        runways,
        runway_13.threshold_lat,
        runway_13.threshold_lon,
        course_deg=305.0,
    )
    assert reciprocal is not None and reciprocal.name == "31"


def test_match_returns_none_for_a_landing_on_another_map() -> None:
    """A recording from a different theatre must not silently match.

    The ACMI stream carries no theatre name, so this distance check is the
    only thing keeping an NTTR landing from being graded against Caucasus
    runway geometry.
    """
    assert match_runway(_runways(), 36.235, -115.034, course_deg=125.0) is None


def test_runways_are_rotated_from_the_dcs_grid_onto_true_north() -> None:
    """DCS x/z is the map projection's grid, not true north.

    Treating x as due north rotated every runway about its airfield by the
    local meridian convergence -- 4.9 deg at Sochi, 5.7 deg at Batumi on
    Caucasus. It showed up as a consistent ~6 deg offset between the runway
    centreline and the ground track of 13 landings across four airfields,
    all with the same sign; the touchdown-anchored landings, whose course
    comes from the aircraft itself, had no such bias.
    """
    import math

    from app.runways.models import runway_pair_from_dcs

    reference = (43.44, 39.93, 0.0, 0.0)  # lat, lon, x, z of the airfield
    course_rad = -math.radians(62.0)  # DCS reports the negated heading
    kwargs = dict(
        airbase="Sochi",
        dcs_name="06",
        course_rad=course_rad,
        centre_x=0.0,
        centre_z=0.0,
        elevation_m=10.0,
        length_m=2500.0,
        width_m=60.0,
        airbase_ref=reference,
    )

    grid = runway_pair_from_dcs(**kwargs, convergence_deg=0.0)
    rotated = runway_pair_from_dcs(**kwargs, convergence_deg=4.87)

    assert grid[0].heading_deg == pytest.approx(62.0, abs=0.01)
    assert rotated[0].heading_deg == pytest.approx(66.87, abs=0.01)
    # Both ends move together: still a straight strip, just pointing where
    # the aircraft actually flies.
    assert rotated[1].heading_deg == pytest.approx(246.87, abs=0.01)

    # The thresholds rotate with it rather than staying put.
    moved = math.hypot(
        (rotated[0].threshold_lat - grid[0].threshold_lat) * 111_320,
        (rotated[0].threshold_lon - grid[0].threshold_lon)
        * 111_320
        * math.cos(math.radians(43.44)),
    )
    assert moved > 50.0

    # The runway number still comes from DCS, not from the rotated heading:
    # runway designators are magnetic and are not ours to renumber.
    assert rotated[0].name == "06"
