"""Runway handling on maps other than the one everything was built against.

Every airfield the geometry was verified on is on Caucasus, and Caucasus has
no parallel runways and was the only theatre the deployment ever ran. Both
facts hid assumptions that only break on a second map, so the cases here are
deliberately about the *shape* of another map -- parallel strips, several
running theatres, two maps covering the same ground -- rather than about
surveyed values for any particular airfield.
"""

from __future__ import annotations

import json
import math

import pytest

from app.detection.geometry import haversine_m, offset_position
from app.runways.dcssb import _parse_airbase
from app.runways.models import (
    Runway,
    match_runway,
    reciprocal_designator,
    runway_pair_from_dcs,
)
from app.runways.provider import CACHE_VERSION, RunwayProvider

# --- a Nellis-shaped airfield -------------------------------------------------
# Two parallel strips, 03L/21R and 03R/21L. The spacing and the stagger are
# plausible for a base of that layout but they are constructed, not surveyed:
# what is being tested is the naming and matching rules, not KLSV.
NELLIS_LAT, NELLIS_LON = 36.235, -115.034
NELLIS_ELEVATION_M = 570.0
GRID_HEADING = 30.0
STRIP_SPACING_M = 275.0
#: 03R starts this much further down the 03 direction than 03L. Staggered
#: thresholds are common and are what make "nearest threshold wins" pick the
#: wrong strip.
STRIP_STAGGER_M = 400.0
STRIP_LENGTH_M = 3050.0

NELLIS_AIRBASE = {
    "id": "Nellis",
    "name": "Nellis AFB",
    "lat": NELLIS_LAT,
    "lng": NELLIS_LON,
    "alt": NELLIS_ELEVATION_M,
    "position": {"y": NELLIS_ELEVATION_M, "x": 0.0, "z": 0.0},
    "runwayList": ["03L", "03R", "21L", "21R"],
}


def _nellis_detail() -> dict:
    along = math.radians(GRID_HEADING)
    right = math.radians(GRID_HEADING + 90.0)
    return {
        "airbase": {
            "runways": [
                {
                    # DCS reports `course` negated, so this is a 030 strip.
                    "course": -along,
                    "Name": "03L",
                    "position": {"y": NELLIS_ELEVATION_M, "x": 0.0, "z": 0.0},
                    "length": STRIP_LENGTH_M,
                    "width": 45,
                },
                {
                    "course": -along,
                    "Name": "03R",
                    "position": {
                        "y": NELLIS_ELEVATION_M,
                        # DCS x is grid north, z grid east.
                        "x": math.cos(right) * STRIP_SPACING_M
                        + math.cos(along) * STRIP_STAGGER_M,
                        "z": math.sin(right) * STRIP_SPACING_M
                        + math.sin(along) * STRIP_STAGGER_M,
                    },
                    "length": STRIP_LENGTH_M,
                    "width": 45,
                },
            ]
        }
    }


def _nellis() -> list[Runway]:
    return _parse_airbase(NELLIS_AIRBASE, _nellis_detail())


# --- naming -------------------------------------------------------------------


def test_parallel_strips_keep_their_side_on_the_reciprocal_end() -> None:
    """03L's other end is 21R, not "21".

    Deriving the reciprocal from the heading dropped the L/C/R suffix, so
    both strips of a parallel pair came out named "03"/"21" and were
    indistinguishable in the row, in the venue filter, and in anything
    counting landings per runway.
    """
    assert {r.name for r in _nellis()} == {"03L", "21R", "03R", "21L"}


@pytest.mark.parametrize(
    ("designator", "expected"),
    [
        ("13", "31"),
        ("31", "13"),
        ("03L", "21R"),
        ("21R", "03L"),
        ("09C", "27C"),
        ("36", "18"),
        ("18", "36"),
        # Nothing recognisable: the caller falls back to the heading rather
        # than inventing a number.
        ("", ""),
        ("RWY", ""),
        ("41", ""),
    ],
)
def test_reciprocal_designator(designator: str, expected: str) -> None:
    assert reciprocal_designator(designator) == expected


def test_the_dcs_designator_is_hung_on_the_end_it_points_at() -> None:
    """``Name`` and ``course`` are independent fields; trust the course.

    Assuming the name always belongs to the end ``course`` describes is
    right on every airfield checked so far, but if it ever is not, both
    directions come out reversed -- and a reversed name is a runway a pilot
    lines up on backwards, with nothing on the row looking odd.
    """
    pair = runway_pair_from_dcs(
        airbase="Nellis",
        dcs_name="21R",  # named for the end `course` does NOT describe
        course_rad=-math.radians(GRID_HEADING),
        centre_x=0.0,
        centre_z=0.0,
        elevation_m=NELLIS_ELEVATION_M,
        length_m=STRIP_LENGTH_M,
        width_m=45.0,
        airbase_ref=(NELLIS_LAT, NELLIS_LON, 0.0, 0.0),
    )
    by_name = {r.name: r.heading_deg for r in pair}
    assert by_name["03L"] == pytest.approx(30.0, abs=0.01)
    assert by_name["21R"] == pytest.approx(210.0, abs=0.01)


# --- matching -----------------------------------------------------------------


def test_a_long_landing_stays_on_the_strip_it_actually_touched() -> None:
    """Parallel strips are decided by the centreline, not by the threshold.

    Ranking candidates by distance to the threshold decides a parallel pair
    by how far down the runway the pilot floated: with staggered thresholds
    a touchdown well down 03L is *nearer* to 03R's threshold than to its
    own, and the deviations are then measured against the wrong strip --
    which shifts the whole approach ~275 m off centreline.
    """
    runways = _nellis()
    left = next(r for r in runways if r.name == "03L")
    right = next(r for r in runways if r.name == "03R")

    lat, lon = offset_position(
        left.threshold_lat, left.threshold_lon, left.heading_deg, 2000.0, 0.0
    )

    # The premise: the wrong strip's threshold really is the nearer one.
    to_left = haversine_m(lat, lon, left.threshold_lat, left.threshold_lon)
    to_right = haversine_m(lat, lon, right.threshold_lat, right.threshold_lon)
    assert to_right < to_left

    matched = match_runway(runways, lat, lon, course_deg=GRID_HEADING)
    assert matched is not None and matched.name == "03L"


# --- theatre selection --------------------------------------------------------


class _FakeBot:
    """DCSServerBot, as much of it as :class:`RunwayProvider` uses."""

    def __init__(
        self,
        servers: dict[str, str],
        airbases: dict[str, list[dict]] | None = None,
        runways: dict[str, list[Runway]] | None = None,
    ) -> None:
        self._servers = servers  # server name -> theatre it is running
        self._airbases = airbases or {}  # theatre -> airbase listing
        self._runways = runways or {}  # theatre -> swept runways
        self.swept: list[str] = []
        self.listed: list[str] = []

    async def list_servers(self) -> list[dict]:
        return [
            {"name": name, "mission": {"theatre": theatre}}
            for name, theatre in self._servers.items()
        ]

    async def theatre_of(self, server_name: str) -> str | None:
        return self._servers.get(server_name)

    async def find_server_for_theatre(self, theatre: str | None) -> str | None:
        for name, running in self._servers.items():
            if theatre is None or running.lower() == theatre.lower():
                return name
        return None

    async def fetch_airbases(self, server_name: str) -> list[dict]:
        self.listed.append(server_name)
        return self._airbases.get(self._servers.get(server_name, ""), [])

    async def fetch_runways(self, server_name: str) -> list[Runway]:
        self.swept.append(server_name)
        return self._runways.get(self._servers.get(server_name, ""), [])


async def test_a_pinned_server_is_not_swept_for_a_map_it_is_not_running(
    tmp_path,
) -> None:
    """``dcssb_server_name`` pins WHICH server, not WHAT it is running.

    Sweeping it regardless stored whatever map it happened to be on under
    the theatre that was asked for, and a cache hit never re-checks: one
    map change and every later landing on the old map is matched against
    the new map's geometry, permanently.
    """
    bot = _FakeBot({"Main": "Nevada"}, runways={"Nevada": _nellis()})
    provider = RunwayProvider(bot, tmp_path, server_name="Main")

    assert await provider.runways_for("Caucasus") == []
    assert bot.swept == []
    assert list(tmp_path.glob("runways-*.json")) == []


async def test_the_theatre_swept_is_the_one_the_landing_is_on(tmp_path) -> None:
    """With several servers up, position decides which map to sweep.

    Taking the first server that reported any theatre at all was correct
    only while every server ran the same map. ``/servers`` and ``/airbases``
    are both answered from the bot's own state, so asking all of them costs
    no simulation-thread time -- unlike the sweep it steers.
    """
    bot = _FakeBot(
        # The Caucasus server is listed first on purpose: it is the one the
        # previous "first live theatre" rule would have swept.
        {"Caucasus Main": "Caucasus", "NTTR Training": "Nevada"},
        airbases={
            "Caucasus": [{"lat": 41.61, "lng": 41.60, "runwayList": ["13"]}],
            "Nevada": [
                {"lat": NELLIS_LAT, "lng": NELLIS_LON, "runwayList": ["03L"]}
            ],
        },
        runways={"Nevada": _nellis()},
    )
    provider = RunwayProvider(bot, tmp_path)

    left = next(r for r in _nellis() if r.name == "03L")
    lat, lon = offset_position(
        left.threshold_lat, left.threshold_lon, left.heading_deg, 300.0, 0.0
    )
    matched = await provider.resolve(lat, lon, GRID_HEADING)

    assert matched is not None and matched.airbase == "Nellis"
    assert bot.swept == ["NTTR Training"]
    assert (tmp_path / "runways-Nevada.json").is_file()


def _write_cache(cache_dir, theatre: str, runways: list[Runway]) -> None:
    (cache_dir / f"runways-{theatre}.json").write_text(
        json.dumps(
            {
                "version": CACHE_VERSION,
                "theatre": theatre,
                "runways": [r.as_dict() for r in runways],
            }
        ),
        encoding="utf-8",
    )


def _strip(airbase: str, lat: float, lon: float) -> Runway:
    return Runway(
        airbase=airbase,
        name="09",
        threshold_lat=lat,
        threshold_lon=lon,
        elevation_m=100.0,
        heading_deg=90.0,
        length_m=3000.0,
        width_m=45.0,
    )


async def test_overlapping_maps_resolve_to_the_nearer_airfield(tmp_path) -> None:
    """Several DCS maps cover the same ground; the best match wins, not the first.

    Syria and Iraq share the border region, Iraq and Persian Gulf share
    Kuwait, and an airfield on two maps is one geometry per map. Returning
    the first cached theatre that produced any match graded such a landing
    against whichever file the directory listing happened to yield first --
    here ``runways-Iraq.json``, purely because I sorts before S.
    """
    _write_cache(tmp_path, "Iraq", [_strip("Al Asad (Iraq copy)", 33.000, 42.0)])
    _write_cache(tmp_path, "Syria", [_strip("Al Asad (Syria copy)", 33.002, 42.0)])

    provider = RunwayProvider(None, tmp_path)
    matched = await provider.resolve(33.002, 42.0, course_deg=90.0)

    assert matched is not None
    assert matched.threshold_lat == pytest.approx(33.002)


# --- the row says where -------------------------------------------------------


class _FixedRunway:
    """A runway provider that always resolves to the same strip."""

    def __init__(self, runway: Runway) -> None:
        self._runway = runway

    async def resolve(self, latitude, longitude, course_deg):  # noqa: ANN001
        return self._runway


async def test_a_land_landing_records_which_airfield_it_was(session_factory) -> None:
    """"空港" is not an answer once more than one map is flown.

    Carrier landings have always carried the ship's name; land landings
    carried nothing, so every airfield on every map rendered as the same
    placeholder and the venue filter could not tell them apart.
    """
    from sqlalchemy import select

    from app.grading.config import GradingConfig
    from app.ingest import TrackIngestor
    from app.models.entities import Landing
    from app.pipeline import LandingPipeline
    from tests.helpers import LAT0, LON0, make_acmi_text, make_approach_samples

    # The shared fixture lands north-bound at (LAT0, LON0) off a 20 m deck.
    runway = Runway(
        airbase="Nellis",
        name="03L",
        threshold_lat=LAT0 - 300.0 / 111_320.0,
        threshold_lon=LON0,
        elevation_m=20.0,
        heading_deg=0.0,
        length_m=3050.0,
        width_m=45.0,
    )
    pipeline = LandingPipeline(
        session_factory, GradingConfig({}), runway_provider=_FixedRunway(runway)
    )
    ingestor = TrackIngestor(
        session_factory,
        landing_listener=pipeline.handle_landing,
        landing_finalize_listener=pipeline.finalize_landing,
        sample_buffer_s=600.0,
    )
    text = make_acmi_text(
        make_approach_samples(outcome="full_stop", pre_touchdown_descent_ms=1.2),
        include_carrier=False,
    )
    try:
        for line in text.splitlines():
            await ingestor.handle_line(line)
    finally:
        await ingestor.close()

    async with session_factory() as session:
        landings = list((await session.execute(select(Landing))).scalars().all())

    assert len(landings) == 1
    assert landings[0].kind == "land"
    assert landings[0].venue_name == "Nellis 03L"
