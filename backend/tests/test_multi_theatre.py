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
from tests.helpers import create_test_schema

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


# --- geometry that ships with the build ---------------------------------------


def _write_pool(directory, theatre: str, runways: list[Runway], version=None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"runways-{theatre}.json").write_text(
        json.dumps(
            {
                "version": CACHE_VERSION if version is None else version,
                "theatre": theatre,
                "runways": [r.as_dict() for r in runways],
            }
        ),
        encoding="utf-8",
    )


async def test_a_shipped_map_resolves_with_no_dcs_server_at_all(tmp_path) -> None:
    """An import must not need the map to be loaded somewhere right now.

    Sweeping requires the mission to be running (the terrain files are
    encrypted and ``/airbase`` runs Lua in the loaded mission), so a recording
    from a map nobody is flying could never be graded against a real runway.
    Geometry captured once and shipped with the build closes that: no client,
    no cache, and the landing still resolves.
    """
    seeds = tmp_path / "seeds"
    _write_pool(seeds, "Nevada", _nellis())

    provider = RunwayProvider(None, tmp_path / "cache", seed_dir=seeds)
    left = next(r for r in _nellis() if r.name == "03L")
    lat, lon = offset_position(
        left.threshold_lat, left.threshold_lon, left.heading_deg, 300.0, 0.0
    )

    matched = await provider.resolve(lat, lon, GRID_HEADING)
    assert matched is not None and matched.name == "03L"
    assert [t["origin"] for t in provider.inventory()] == ["shipped"]


async def test_a_local_sweep_wins_over_the_shipped_copy(tmp_path) -> None:
    """Geometry swept on this server describes this server's DCS version."""
    seeds, cache = tmp_path / "seeds", tmp_path / "cache"
    shipped = Runway(
        airbase="Nellis (shipped)",
        name="03L",
        threshold_lat=NELLIS_LAT,
        threshold_lon=NELLIS_LON,
        elevation_m=570.0,
        heading_deg=GRID_HEADING,
        length_m=3050.0,
        width_m=45.0,
    )
    local = Runway(**{**shipped.__dict__, "airbase": "Nellis (swept here)"})
    _write_pool(seeds, "Nevada", [shipped])
    _write_pool(cache, "Nevada", [local])

    provider = RunwayProvider(None, cache, seed_dir=seeds)
    matched = await provider.resolve(NELLIS_LAT, NELLIS_LON, GRID_HEADING)

    assert matched is not None and matched.airbase == "Nellis (swept here)"
    assert [t["origin"] for t in provider.inventory()] == ["swept"]


async def test_an_exact_capture_outranks_a_live_sweep(tmp_path) -> None:
    """DCSServerBot returns runways as grid x/z, so a live sweep is converted
    by approximation; the in-game hook has DCS convert them itself. When both
    exist, the approximation must not win just because it was made locally.
    """
    seeds, cache = tmp_path / "seeds", tmp_path / "cache"
    seeds.mkdir()
    (seeds / "runways-Nevada.json").write_text(
        json.dumps(
            {
                "version": CACHE_VERSION,
                "theatre": "Nevada",
                "exact": True,
                "runways": [_strip("Nellis (exact)", NELLIS_LAT, NELLIS_LON).as_dict()],
            }
        ),
        encoding="utf-8",
    )
    _write_pool(cache, "Nevada", [_strip("Nellis (live sweep)", NELLIS_LAT, NELLIS_LON)])

    # Both entry points: the directory scan resolve() runs, and the per-theatre
    # load runways_for() does on a fresh provider.
    provider = RunwayProvider(None, cache, seed_dir=seeds)
    matched = await provider.resolve(NELLIS_LAT, NELLIS_LON, 90.0)
    assert matched is not None and matched.airbase == "Nellis (exact)"
    assert [t["origin"] for t in provider.inventory()] == ["shipped"]

    fresh = RunwayProvider(None, cache, seed_dir=seeds)
    assert [r.airbase for r in await fresh.runways_for("Nevada")] == ["Nellis (exact)"]


def test_a_zero_length_runway_is_a_ship_not_a_2000_m_runway() -> None:
    """CVN-71 on Marianas comes back from getRunways() with length 0, width 0.

    `record.get("length") or 2000.0` read that 0 as missing and gave the deck
    a 2000 m runway, frozen where the carrier started the mission.
    """
    detail = {
        "airbase": {
            "runways": [
                {"course": 0.0, "Name": "03", "length": 0, "width": 0,
                 "position": {"x": 0.0, "y": 0.0, "z": 0.0}},
            ]
        }
    }
    assert _parse_airbase(NELLIS_AIRBASE, detail) == []


async def test_a_stale_cache_file_is_ignored_by_the_directory_scan(tmp_path) -> None:
    """v1 geometry is ~5 deg rotated and must not be served from anywhere.

    ``_load_cache`` rejected it, but the directory scan that ``resolve`` runs
    first did not look at the version at all -- so the check that existed was
    the one on the path nothing took.
    """
    cache = tmp_path / "cache"
    _write_pool(cache, "Nevada", _nellis(), version=1)

    provider = RunwayProvider(None, cache)
    assert await provider.resolve(NELLIS_LAT, NELLIS_LON, GRID_HEADING) is None
    assert provider.inventory() == []


async def test_sweep_recaptures_a_theatre_that_is_already_cached(tmp_path) -> None:
    """The automatic path never re-sweeps; a cache hit does not re-check.

    So a map whose stored geometry is wrong (or was captured by an older
    build) can only be fixed by asking for it explicitly.
    """
    cache = tmp_path / "cache"
    _write_pool(cache, "Nevada", [])  # cached, and empty

    bot = _FakeBot({"NTTR": "Nevada"}, runways={"Nevada": _nellis()})
    provider = RunwayProvider(bot, cache)
    assert await provider.runways_for("Nevada") == []  # cached: no sweep
    assert bot.swept == []

    result = await provider.sweep("Nevada")
    assert result["swept"] is True and result["runways"] == 4
    assert bot.swept == ["NTTR"]
    assert await provider.resolve(NELLIS_LAT, NELLIS_LON, GRID_HEADING) is not None


async def test_sweep_refuses_when_it_cannot_tell_which_map_is_meant(tmp_path) -> None:
    bot = _FakeBot({"A": "Caucasus", "B": "Nevada"})
    result = await RunwayProvider(bot, tmp_path).sweep()
    assert result["swept"] is False and "several theatres" in result["reason"]


async def test_export_round_trips_into_a_shipped_seed(tmp_path) -> None:
    """What the export endpoint returns must be loadable as a seed verbatim."""
    cache, seeds = tmp_path / "cache", tmp_path / "seeds"
    _write_pool(cache, "Nevada", _nellis())
    payload = RunwayProvider(None, cache).export("Nevada")
    assert payload is not None

    seeds.mkdir()
    (seeds / "runways-Nevada.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    provider = RunwayProvider(None, tmp_path / "empty", seed_dir=seeds)
    assert {r.name for r in await provider.runways_for("Nevada")} == {
        "03L",
        "21R",
        "03R",
        "21L",
    }


async def test_the_api_lists_and_exports_shipped_geometry(tmp_path) -> None:
    """The operator path: see what is covered, then move a capture into git.

    Exercised through the app so the wiring is proved too -- an inventory that
    only works when the provider is handed in by a test proves nothing about a
    server whose provider is built from settings.
    """
    import httpx2 as httpx

    from app.api.main import create_app
    from app.config import Settings

    seeds = tmp_path / "seeds"
    _write_pool(seeds, "Nevada", _nellis())
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'api.db').as_posix()}",
        acmi_enabled=False,
        # No DCSServerBot at all: shipped geometry has to stand on its own.
        dcssb_base_url="",
        runway_cache_dir=str(tmp_path / "cache"),
        runway_seed_dir=str(seeds),
    )
    create_test_schema(settings.database_url)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as http:
            listing = await http.get("/api/v1/runways")
            assert listing.status_code == 200
            body = listing.json()
            assert body["can_sweep"] is False and body["running"] == []
            assert body["theatres"] == [
                {
                    "theatre": "Nevada",
                    "runways": 4,
                    "airbases": 1,
                    "origin": "shipped",
                }
            ]

            export = await http.get("/api/v1/runways/Nevada")
            assert export.status_code == 200
            assert {r["name"] for r in export.json()["runways"]} == {
                "03L",
                "21R",
                "03R",
                "21L",
            }
            assert (await http.get("/api/v1/runways/Syria")).status_code == 404


# --- geometry and names as DCS actually reports them ---------------------------


def test_exact_geometry_uses_the_points_dcs_converted() -> None:
    """With DCS's own lat/lon for the centre and a probe, nothing is modelled.

    The approximate route converts grid metres as if they were ground metres;
    on Caucasus that left thresholds up to 18 m out, growing with distance from
    the projection's central meridian. Here the thresholds are a fixed
    fraction of the centre->probe vector and the heading is its bearing, so
    whatever the projection does is already inside the two points.
    """
    from app.runways.models import _initial_bearing, runway_pair_from_exact

    centre = (41.0, 41.0)
    probe = (41.009, 41.001)
    pair = runway_pair_from_exact(
        airbase="X",
        dcs_name="01",
        course_rad=-math.radians(5.0),
        centre_lat=centre[0],
        centre_lon=centre[1],
        probe_lat=probe[0],
        probe_lon=probe[1],
        probe_grid_m=1000.0,
        elevation_m=10.0,
        length_m=3000.0,
        width_m=45.0,
    )
    by = {r.name: r for r in pair}
    assert set(by) == {"01", "19"}
    # Half of 3000 grid metres is 1.5 probe lengths either side of the centre.
    assert by["01"].threshold_lat == pytest.approx(centre[0] - 1.5 * 0.009)
    assert by["01"].threshold_lon == pytest.approx(centre[1] - 1.5 * 0.001)
    assert by["19"].threshold_lat == pytest.approx(centre[0] + 1.5 * 0.009)
    assert by["19"].threshold_lon == pytest.approx(centre[1] + 1.5 * 0.001)
    bearing = _initial_bearing(*centre, *probe)
    assert by["01"].heading_deg == pytest.approx(bearing, abs=1e-9)
    assert by["19"].heading_deg == pytest.approx((bearing + 180.0) % 360.0, abs=1e-9)


def test_parallel_strips_get_their_side_from_geometry() -> None:
    """Nellis as the live server reports it: "3" and "21", no sides at all.

    Two records, centres ~305 m apart, the same 40 deg grid course, one named
    by its 03 end and the other by its 21 end. Without sides both strips are
    03/21 and cannot be told apart anywhere a runway is shown or counted.
    """
    detail = {
        "airbase": {
            "runways": [
                {"course": -math.radians(40.0), "Name": 3,
                 "position": {"y": 561.0, "x": 0.0, "z": 0.0},
                 "length": 2877.0, "width": 60},
                {"course": -math.radians(40.0), "Name": 21,
                 "position": {"y": 561.0, "x": 195.0, "z": -234.0},
                 "length": 2877.0, "width": 60},
            ]
        }
    }
    runways = _parse_airbase(NELLIS_AIRBASE, detail)
    assert sorted(r.name for r in runways) == ["03L", "03R", "21L", "21R"]
    # Sides mirror: the strip on the left landing 03 is on the right landing 21.
    left = next(r for r in runways if r.name == "03L")
    other_end = min(
        (r for r in runways if r.name.startswith("21")),
        key=lambda r: abs(
            haversine_m(r.threshold_lat, r.threshold_lon,
                        left.threshold_lat, left.threshold_lon) - 2877.0
        ),
    )
    assert other_end.name == "21R"


def test_a_designator_that_fits_neither_end_is_not_used() -> None:
    """DCS names Boulder City's 09/27 strip "15" -- about 50 deg off both ends.

    Taking it gave that strip and the real 15/33 the same label. Ignoring it
    falls back to the heading, which is only ever a number or so out.
    """
    pair = runway_pair_from_dcs(
        airbase="Boulder City",
        dcs_name="15",
        course_rad=-math.radians(278.48),
        centre_x=0.0,
        centre_z=0.0,
        elevation_m=700.0,
        length_m=1128.5,
        width_m=20.0,
        airbase_ref=(35.95, -114.86, 0.0, 0.0),
    )
    assert sorted(r.name for r in pair) == ["10", "28"]


def _records(*rows):
    """Runway records as DCS reports them: (Name, grid heading, x, z, length)."""
    return {
        "airbase": {
            "runways": [
                {"course": -math.radians(grid), "Name": dcs_name,
                 "position": {"y": 10.0, "x": x, "z": z},
                 "length": length, "width": 45}
                for dcs_name, grid, x, z, length in rows
            ]
        }
    }


def _airbase_at(x: float, z: float) -> dict:
    return {"id": "AB", "name": "AB", "lat": 32.0, "lng": 34.9, "alt": 10.0,
            "position": {"y": 10.0, "x": x, "z": z}, "runwayList": []}


def test_names_dcs_hung_on_the_wrong_strip_are_not_believed() -> None:
    """Ben-Gurion (Sinai) as the live server reports it.

    Three strips, 03/21, 08/26 and 12/30, reported as "3", "21" and "8": the
    last two are 48.8 and 40.4 deg off their own strips. Believing them gave
    two runways labelled 08/26 and none labelled 12/30.
    """
    runways = _parse_airbase(
        _airbase_at(217468, 348036),
        _records(("3", 27.8, 217468, 348036, 2571),
                 ("21", 78.8, 218358, 346492, 2571),
                 ("8", 300.4, 217446, 346858, 2897)),
    )
    assert sorted(r.name for r in runways) == ["03", "08", "12", "21", "26", "30"]


def test_a_believable_name_that_clashes_gives_way_to_the_heading() -> None:
    """Beirut (Syria): "16" on the 17/35 strip is only 18.9 deg off.

    That is inside what declination and convergence explain on other maps, so
    no angle threshold rejects it -- but it makes a second 16/34 next to the
    real one. The strip whose name sits further from its heading is renamed.
    """
    runways = _parse_airbase(
        _airbase_at(-131532, -42725),
        _records(("34", 348.9, -131532, -42725, 2884),
                 ("16", 178.9, -131247, -42288, 2884),
                 ("17", 214.8, -133073, -42024, 2169)),
    )
    names = sorted(r.name for r in runways)
    assert len(names) == len(set(names)) == 6
    assert {"16", "34"} <= set(names)
    # The 214.8 deg strip reported as "17" (44.8 deg off) is its 03/21.
    assert {"03", "21"} <= set(names)


def test_a_name_thirty_degrees_off_is_rejected() -> None:
    """Gelendzhik (Caucasus): "1" on a strip at 40.0 deg grid -- its 04/22.

    The production seed held this strip as "1" at one end and "22" at the
    other, a pair that cannot exist.
    """
    pair = runway_pair_from_dcs(
        airbase="Gelendzhik", dcs_name=1, course_rad=-math.radians(40.0),
        centre_x=0.0, centre_z=0.0, elevation_m=22.0, length_m=1661.8,
        width_m=60.0, airbase_ref=(44.57, 38.01, 0.0, 0.0),
    )
    assert sorted(r.name for r in pair) == ["04", "22"]


def test_a_misnamed_parallel_still_gets_its_side() -> None:
    """Tel Nof (Sinai): two parallel 15/33 strips, one reported as "18"."""
    runways = _parse_airbase(
        _airbase_at(198387, 341243),
        _records(("36", 1.7, 198387, 341243, 2205),
                 ("18", 329.7, 198604, 342089, 2205),
                 ("33", 329.7, 198657, 342643, 2224)),
    )
    assert sorted(r.name for r in runways) == ["15L", "15R", "18", "33L", "33R", "36"]


def test_bare_int_designators_are_zero_padded() -> None:
    """DCS sends "Name": 6 for Sochi; the other end always comes out "24"."""
    pair = runway_pair_from_dcs(
        airbase="Sochi",
        dcs_name=6,
        course_rad=-math.radians(62.0),
        centre_x=0.0,
        centre_z=0.0,
        elevation_m=10.0,
        length_m=2500.0,
        width_m=60.0,
        airbase_ref=(43.44, 39.93, 0.0, 0.0),
    )
    assert sorted(r.name for r in pair) == ["06", "24"]


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
