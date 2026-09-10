"""Tests for DCSServerBot runway retrieval."""

import asyncio

import pytest

from app.detection.geometry import haversine_m
from app.runways.dcssb import DcssbClient, _parse_airbase

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
                "position": {"y": 10.044037818909, "x": -355810.6875, "z": 617386.1875},
                "length": 2070.3959960938,
                "width": 60,
            }
        ]
    }
}


def _batumi():
    return _parse_airbase(BATUMI_AIRBASE, BATUMI_DETAIL)


def test_dcs_runway_expands_into_both_landing_directions() -> None:
    runways = _batumi()
    assert {r.name for r in runways} == {"31", "13"}
    headings = sorted(round(r.heading_deg, 1) for r in runways)
    assert headings == [125.6, 305.6]


def test_threshold_position_matches_independently_computed_reference() -> None:
    runway_13 = next(r for r in _batumi() if r.name == "13")
    assert haversine_m(runway_13.threshold_lat, runway_13.threshold_lon, 41.615005, 41.590119) < 5.0


def test_thresholds_sit_a_full_runway_length_apart() -> None:
    runways = _batumi()
    a, b = runways[0], runways[1]
    spacing = haversine_m(a.threshold_lat, a.threshold_lon, b.threshold_lat, b.threshold_lon)
    assert spacing == pytest.approx(2070.4, rel=0.01)


def test_airbase_is_queried_by_display_name_not_id() -> None:
    """DCSSB's /airbase matches the display name; the id returns nothing."""
    client = DcssbClient("http://example.invalid")
    asked: list[str] = []

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "airbases": [
                    {
                        "id": "Anapa",
                        "name": "Anapa-Vityazevo",
                        "runwayList": ["04"],
                        "lat": 45.0,
                        "lng": 37.3,
                        "position": {"x": 0.0, "z": 0.0},
                    }
                ]
            }

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

    original = module.httpx2.AsyncClient
    module.httpx2.AsyncClient = _Client
    try:
        asyncio.run(client.fetch_runways("srv"))
    finally:
        module.httpx2.AsyncClient = original

    assert asked == ["Anapa-Vityazevo"]
