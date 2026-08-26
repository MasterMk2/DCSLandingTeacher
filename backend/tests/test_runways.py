"""Runway geometry from DCS, and the grading it feeds."""

from __future__ import annotations

import asyncio
import math

import pytest

from app.detection.geometry import haversine_m
from app.grading.deviations import DeviationSample
from app.runways.dcssb import _parse_airbase
from app.runways.models import Runway, match_runway

# Batumi (UGSB) as DCSServerBot reports it for a live Caucasus mission.
BATUMI_AIRBASE = {
    "id": "Batumi",
    "lat": 41.60959684622,
    "lng": 41.600236917191,
    "alt": 10.044037372519,
    "position": {"y": 10.044037372519, "x": -355810.703125, "z": 617386.1875},
    "runwayList": ["13", "31"],
}
BATUMI_DETAIL = {
    "airbase": {
        "runways": [
            {
                "course": 0.95013099908829,
                "Name": 31,
                "position": {
                    "y": 10.044037818909,
                    "x": -355810.6875,
                    "z": 617386.1875,
                },
                "length": 2070.3959960938,
                "width": 60,
            }
        ]
    }
}


def _batumi() -> list[Runway]:
    return _parse_airbase(BATUMI_AIRBASE, BATUMI_DETAIL)


def test_dcs_runway_expands_into_both_landing_directions() -> None:
    runways = _batumi()
    assert {r.name for r in runways} == {"31", "13"}
    headings = sorted(round(r.heading_deg, 1) for r in runways)
    # DCS reports `course` opposite to compass heading, so 0.95013 rad is
    # 305.6 deg (runway 31) and its reciprocal 125.6 deg (runway 13).
    assert headings == [125.6, 305.6]


def test_threshold_position_matches_independently_computed_reference() -> None:
    """Cross-check against the sister DCSWebGCA project's own computation.

    It derives Batumi 13's threshold as (41.615005, 41.590119) from the same
    DCS data through an independent implementation; agreeing to a couple of
    metres confirms both the course convention and the local flat-earth
    conversion off the airbase reference point.
    """
    runway_13 = next(r for r in _batumi() if r.name == "13")
    assert (
        haversine_m(
            runway_13.threshold_lat, runway_13.threshold_lon, 41.615005, 41.590119
        )
        < 5.0
    )


def test_thresholds_sit_a_full_runway_length_apart() -> None:
    runways = _batumi()
    a, b = runways[0], runways[1]
    spacing = haversine_m(
        a.threshold_lat, a.threshold_lon, b.threshold_lat, b.threshold_lon
    )
    assert spacing == pytest.approx(2070.4, rel=0.01)


def test_aiming_point_lies_past_the_threshold_along_the_runway() -> None:
    runway_13 = next(r for r in _batumi() if r.name == "13")
    lat, lon = runway_13.aiming_point(300.0)
    assert (
        haversine_m(lat, lon, runway_13.threshold_lat, runway_13.threshold_lon)
        == pytest.approx(300.0, rel=0.02)
    )
    # ...and towards the far end, not back down the approach.
    far = next(r for r in _batumi() if r.name == "31")
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
    runways = _batumi()
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


def test_airbase_is_queried_by_display_name_not_id() -> None:
    """DCSSB's /airbase matches the display name; the id returns nothing.

    Most Caucasus airbases have both equal, so querying by id still works
    for them -- it only shows up as *partial* coverage (8 of 21 airbases on
    the first live sweep), which is easy to miss.
    """
    from app.runways.dcssb import DcssbClient

    client = DcssbClient("http://example.invalid")
    asked: list[str] = []

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"airbases": [{"id": "Anapa", "name": "Anapa-Vityazevo",
                                  "runwayList": ["04"], "lat": 45.0, "lng": 37.3,
                                  "position": {"x": 0.0, "z": 0.0}}]}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None, **kwargs):
            if url.endswith("/airbase"):
                asked.append(params["airbase_name"])
                raise RuntimeError("stop after recording the name")
            return _Response()

    import app.runways.dcssb as module

    original = module.httpx.AsyncClient
    module.httpx.AsyncClient = _Client
    try:
        asyncio.run(client.fetch_runways("srv"))
    finally:
        module.httpx.AsyncClient = original

    assert asked == ["Anapa-Vityazevo"]


def test_match_returns_none_for_a_landing_on_another_map() -> None:
    """A recording from a different theatre must not silently match.

    The ACMI stream carries no theatre name, so this distance check is the
    only thing keeping an NTTR landing from being graded against Caucasus
    runway geometry.
    """
    assert match_runway(_batumi(), 36.235, -115.034, course_deg=125.0) is None


def test_glideslope_error_is_angular_not_a_fixed_distance() -> None:
    """The same metre deviation is a very different error at different ranges.

    This is why the component is scored in degrees: 20 m low at 1.5 km is a
    routine correction, while 20 m low over the threshold is not, and a
    fixed metre threshold cannot express that.
    """
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
    # 1500 m: still on a normal-looking approach. 500 m: barely above ground.
    assert far_error == pytest.approx(-0.76, abs=0.05)
    assert near_error == pytest.approx(-2.29, abs=0.05)
    assert abs(near_error) > abs(far_error) * 2.5


def test_glidepath_is_not_judged_when_there_is_no_approach_to_judge() -> None:
    """A hover-on has no glidepath, and must not be scored as if it did.

    Observed on real data: a landing whose whole captured window sat at
    ~17 m from touchdown and ~8 m up (an aircraft creeping onto the spot)
    produced a reported mean glidepath error of 37.9 degrees, because the
    angle was being computed from a handful of metres of distance. The
    component has to report "unknown" and score neutrally instead.
    """
    from app.grading.config import GradingConfig
    from app.grading.deviations import ApproachAnalysis
    from app.grading.land_grader import grade_land_landing

    touchdown_time = 100.0
    samples = [
        DeviationSample(
            time=touchdown_time - 30.0 + i * 0.25,
            distance_to_go=17.0 + i * 0.3,
            glideslope_deviation=8.0,
            centerline_deviation=0.5,
            speed=3.0,
            agl=8.8 - i * 0.05,
        )
        for i in range(100)
    ]
    analysis = ApproachAnalysis(
        kind="land",
        outcome="full_stop",
        glideslope_deg=3.0,
        course_deg=0.0,
        touchdown_time=touchdown_time,
        touchdown_speed_ms=3.0,
        touchdown_descent_rate_ms=0.2,
        samples=samples,
    )
    result = grade_land_landing(analysis, GradingConfig({}))
    glideslope = next(c for c in result.components if c.name == "glideslope")
    assert glideslope.evidence["mean_abs_error_deg"] is None
    assert glideslope.evidence["samples"] == 0
    assert glideslope.score == pytest.approx(50.0)
