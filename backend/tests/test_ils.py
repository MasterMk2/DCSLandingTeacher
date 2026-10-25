"""DCS ILS glideslopes -> per-runway glidepath intercept (app.runways.ils)."""

from __future__ import annotations

import pytest

from app.detection.geometry import offset_position
from app.runways.ils import (
    GlideslopeBeacon,
    apply_intercepts,
    match_glideslopes,
    parse_glideslope_beacons,
)
from app.runways.models import Runway

# The shape DCS writes (Caucasus Beacons.lua, abridged): a localizer, a
# glideslope with a geographic position, and a glideslope without one.
BEACONS_LUA = """
beacons = {
	{
		display_name = _('');
		beaconId = 'airfield22_0';
		type = BEACON_TYPE_ILS_LOCALIZER;
		callsign = 'ILU';
		frequency = 110300000.000000;
		position = { -355692.093750, 10.044037, 617290.187500 };
		direction = -54.415131;
		positionGeo = { latitude = 41.606510, longitude = 41.609613 };
		sceneObjects = {'t:443842560'};
		chartOffsetX = 2355.000000;
	};
	{
		display_name = _('');
		beaconId = 'airfield22_1';
		type = BEACON_TYPE_ILS_GLIDESLOPE;
		callsign = 'ILU';
		frequency = 110300000.000000;
		position = { -355396.812500, 10.044037, 616600.500000 };
		direction = -54.415131;
		positionGeo = { latitude = 41.613925, longitude = 41.601774 };
		sceneObjects = {'t:443908096'};
	};
	{
		display_name = _('');
		beaconId = 'airfield99_1';
		type = BEACON_TYPE_ILS_GLIDESLOPE;
		callsign = 'XXX';
		frequency = 109900000.000000;
		position = { -1.0, 2.0, 3.0 };
		direction = 10.0;
	};
}
"""


def test_parse_reads_glideslopes_with_a_geographic_position_only() -> None:
    beacons = parse_glideslope_beacons(BEACONS_LUA)
    assert [b.callsign for b in beacons] == ["ILU"]
    beacon = beacons[0]
    assert beacon.latitude == pytest.approx(41.613925)
    assert beacon.longitude == pytest.approx(41.601774)
    # ``direction`` points back down the approach; the runway is its reciprocal.
    assert beacon.landing_heading_deg == pytest.approx(125.58, abs=0.01)


def _strip(airbase: str, lat: float, lon: float, heading: float, length: float = 2400.0) -> list[Runway]:
    """Both directions of one strip whose first threshold is at (lat, lon)."""
    far_lat, far_lon = offset_position(lat, lon, heading, length, 0.0)
    return [
        Runway(airbase, "13", lat, lon, 10.0, heading, length, 60.0),
        Runway(airbase, "31", far_lat, far_lon, 10.0, (heading + 180.0) % 360.0, length, 60.0),
    ]


def _antenna(runway: Runway, along_m: float, lateral_m: float, heading: float | None = None) -> GlideslopeBeacon:
    lat, lon = offset_position(
        runway.threshold_lat, runway.threshold_lon, runway.heading_deg, along_m, lateral_m
    )
    return GlideslopeBeacon(
        "GS", lat, lon, runway.heading_deg if heading is None else heading
    )


def test_glideslope_pairs_with_the_direction_it_serves_not_the_reciprocal() -> None:
    """The antenna stands 200 m past the threshold of 13, which is 2200 m
    past the threshold of 31 and pointing the other way: only 13 may take it."""
    runways = _strip("Test", 41.6, 41.6, 125.6)
    matches, unmatched = match_glideslopes(runways, [_antenna(runways[0], 200.0, 120.0)])
    assert unmatched == []
    assert [(m.runway.name, round(m.intercept_m), round(m.lateral_m)) for m in matches] == [
        ("13", 200, 120)
    ]


def test_an_antenna_whose_heading_disagrees_is_not_taken() -> None:
    runways = _strip("Test", 41.6, 41.6, 125.6)
    crossing = _antenna(runways[0], 200.0, 120.0, heading=60.0)
    matches, unmatched = match_glideslopes(runways, [crossing])
    assert matches == []
    assert len(unmatched) == 1


@pytest.mark.parametrize(
    ("along", "lateral"),
    [
        (-170.0, 120.0),  # before the seed threshold: a displaced threshold the seed cannot see
        (950.0, 120.0),   # a longer field's other ILS, or a short seed length
        (200.0, 10.0),    # on the runway: not a glideslope antenna position
        (200.0, 900.0),   # a parallel strip's antenna
    ],
)
def test_implausible_positions_fall_back_to_the_default_and_are_reported(
    along: float, lateral: float
) -> None:
    runways = _strip("Test", 41.6, 41.6, 125.6)
    matches, unmatched = match_glideslopes(runways, [_antenna(runways[0], along, lateral)])
    assert matches == []
    assert len(unmatched) == 1 and unmatched[0].reason


def test_parallel_strips_take_the_antenna_nearest_their_own_centreline() -> None:
    """Nellis-style: two strips 275 m apart, both passing the along-track test.
    The antenna 120 m right of the left strip is 155 m left of the right one."""
    right_lat, right_lon = offset_position(36.23, -115.03, 30.0, 0.0, 275.0)
    runways = [
        Runway("Nellis", "03L", 36.23, -115.03, 10.0, 30.0, 2400.0, 60.0),
        Runway("Nellis", "03R", right_lat, right_lon, 10.0, 30.0, 2400.0, 60.0),
    ]
    antenna = _antenna(runways[0], 250.0, 120.0)
    matches, unmatched = match_glideslopes(runways, [antenna])
    assert unmatched == []
    assert [m.runway.name for m in matches] == ["03L"]
    assert matches[0].intercept_m == pytest.approx(250.0, abs=1.0)


def test_apply_intercepts_writes_matches_and_clears_stale_values() -> None:
    runways = _strip("Test", 41.6, 41.6, 125.6)
    stale = [
        runways[0],
        Runway(
            runways[1].airbase, runways[1].name, runways[1].threshold_lat,
            runways[1].threshold_lon, 10.0, runways[1].heading_deg, 2400.0, 60.0,
            glidepath_intercept_m=999.0,
        ),
    ]
    matches, _ = match_glideslopes(stale, [_antenna(stale[0], 206.0, 118.0)])
    updated = apply_intercepts(stale, matches)
    by_name = {r.name: r for r in updated}
    assert by_name["13"].glidepath_intercept_m == pytest.approx(206.0, abs=1.0)
    assert by_name["31"].glidepath_intercept_m is None
    # Round trip through the seed file shape.
    assert Runway.from_dict(by_name["13"].as_dict()).glidepath_intercept_m == pytest.approx(
        206.0, abs=1.0
    )
    assert "glidepath_intercept_m" not in by_name["31"].as_dict()
