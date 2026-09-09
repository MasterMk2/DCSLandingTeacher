"""Synthetic approach-track builders shared by detection/grading/API tests.

The scenario is a straight-in approach from the south onto a deck/runway at
the origin ``(LAT0, LON0)`` with course ~000 (north). Touchdown happens at
``t == 0``; samples are 1 Hz.
"""

from __future__ import annotations

import math
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TypedDict, Unpack

import httpx2
from fastapi.applications import FastAPI
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.detection.detector import CarrierState, LandingEvent, TrackSample, analyze_track
from app.models.base import Base

LAT0 = 35.0
LON0 = 140.0
DECK_ALTITUDE_M = 20.0
M_PER_DEG_LAT = 111320.0
DECK_LATITUDE = 35.0
DECK_LONGITUDE = 140.0
NIMITZ_DECK_ALTITUDE_M = 19.5


class ApproachSampleOptions(TypedDict, total=False):
    """Optional arguments accepted by :func:`make_approach_samples`."""

    outcome: str
    glideslope_deg: float
    approach_speed_ms: float
    touchdown_speed_ms: float | None
    gs_offset_m: float
    lateral_offset_m: float
    pre_touchdown_descent_ms: float | None
    duration_before_s: float
    ground_time_s: float
    deck_altitude_m: float
    offset_east_m: float
    offset_north_m: float


class ApiSettingsOverrides(TypedDict, total=False):
    """Settings values that API integration scenarios may override."""

    auth_token: str
    grading_config_path: str
    import_max_upload_mb: int


def create_test_schema(database_url: str) -> None:
    """Create the disposable SQLite schema owned by an integration test."""
    engine = create_sync_engine(database_url.replace("sqlite+aiosqlite", "sqlite"))
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


async def create_async_test_schema(engine: AsyncEngine) -> None:
    """Create all application tables in a disposable async test database."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def make_api_settings(
    tmp_path: Path, *, database_filename: str = "api.db", **overrides: Unpack[ApiSettingsOverrides]
) -> Settings:
    """Build API settings backed by a disposable SQLite database.

    Args:
        tmp_path: Test-owned directory in which to create the database.
        database_filename: SQLite filename relative to ``tmp_path``.
        **overrides: Explicit supported ``Settings`` values required by a test scenario.
    """
    database_path = (tmp_path / database_filename).as_posix()
    settings = Settings(
        acmi_enabled=False,
        database_url=f"sqlite+aiosqlite:///{database_path}",
        **overrides,
    )
    create_test_schema(settings.database_url)
    return settings


@asynccontextmanager
async def open_api_client(app: FastAPI) -> AsyncGenerator[httpx2.AsyncClient, None]:
    """Yield an HTTPX client bound to an ASGI application."""
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _lat_offset(meters: float) -> float:
    return meters / M_PER_DEG_LAT


def _lon_offset(meters: float) -> float:
    return meters / (M_PER_DEG_LAT * math.cos(math.radians(LAT0)))


def make_carrier_state(
    obj_id: str = "C1",
    name: str | None = "CV-59",
    lat: float = LAT0,
    lon: float = LON0,
    altitude: float = DECK_ALTITUDE_M,
    heading: float = 0.0,
    type_str: str | None = None,
) -> CarrierState:
    """A stationary carrier at the touchdown point."""
    return CarrierState(
        obj_id=obj_id,
        name=name,
        type=type_str,
        samples=[(0.0, lat, lon, altitude, heading, 0.0)],
    )


def make_approach_samples(
    *,
    outcome: str = "full_stop",
    glideslope_deg: float = 3.5,
    approach_speed_ms: float = 70.0,
    touchdown_speed_ms: float | None = None,
    gs_offset_m: float = 0.0,
    lateral_offset_m: float = 0.0,
    pre_touchdown_descent_ms: float | None = None,
    duration_before_s: float = 55.0,
    ground_time_s: float = 25.0,
    deck_altitude_m: float = DECK_ALTITUDE_M,
    offset_east_m: float = 0.0,
    offset_north_m: float = 0.0,
) -> list[TrackSample]:
    """Build a 1 Hz synthetic approach ending with a landing.

    - ``gs_offset_m``: constant height offset above the ideal slope during
      the whole inbound segment (positive = high).
    - ``lateral_offset_m``: constant lateral offset right of centerline.
    - ``pre_touchdown_descent_ms``: overrides the descent rate over the last
      3 s before contact (None = follow the slope).
    - ``outcome``: ``full_stop`` stays on deck, ``touch_and_go`` climbs out
      again after ~3 s of ground time.
    """
    tan_slope = math.tan(math.radians(glideslope_deg))
    td_speed = approach_speed_ms if touchdown_speed_ms is None else touchdown_speed_ms
    samples: list[TrackSample] = []
    n_before = int(duration_before_s)
    n_after = max(int(ground_time_s), 12 if outcome != "full_stop" else int(ground_time_s))

    east = _lon_offset(offset_east_m)
    north = _lat_offset(offset_north_m)
    for t in range(-n_before, n_after + 1):
        if t < 0:
            dtg = abs(t) * approach_speed_ms
            ideal_agl = dtg * tan_slope + gs_offset_m
            if pre_touchdown_descent_ms is not None and t >= -3:
                # Straight line from (deck + 3*descent) at t=-3 to deck at t=0.
                agl = max(0.0, -t * pre_touchdown_descent_ms)
            else:
                agl = ideal_agl
            latitude = LAT0 - _lat_offset(dtg) + north
            longitude = LON0 + _lon_offset(lateral_offset_m) + east
            altitude = deck_altitude_m + agl
            speed = approach_speed_ms
            on_ground = False
        else:
            dtg = 0.0
            latitude = LAT0 + north
            # The touchdown point sits on the centerline; the lateral offset
            # applies to the inbound segment only, so graders see a real
            # centerline deviation instead of a shifted reference frame.
            longitude = LON0 + east
            if outcome == "full_stop":
                agl = 0.0
                on_ground = True
                speed = max(5.0, td_speed - t * 3.0)
            else:  # touch_and_go / bolter: brief ground contact then climb
                if t <= 3:
                    agl = 0.0
                    on_ground = True
                    speed = td_speed
                else:
                    agl = (t - 3) * 5.0
                    on_ground = False
                    speed = td_speed + (t - 3) * 1.0
            altitude = deck_altitude_m + agl

        samples.append(
            TrackSample(
                time=float(t),
                latitude=latitude,
                longitude=longitude,
                altitude=altitude,
                agl=None,
                speed=speed,
                heading=0.0,
                aoa=None,
                on_ground=on_ground,
            )
        )
    return samples


def make_carrier_landing_event(
    **sample_options: Unpack[ApproachSampleOptions],
) -> LandingEvent:
    """Build the single carrier-landing event from a synthetic approach."""
    samples = make_approach_samples(**sample_options)
    events = analyze_track(samples, DECK_ALTITUDE_M, {"C1": make_carrier_state()})
    assert len(events) == 1
    return events[0]


def make_deck_carrier_states(*, altitude_m: float = 0.0) -> dict[str, CarrierState]:
    """Build a stationary carrier whose ACMI altitude is its waterline."""
    return {
        "C1": CarrierState(
            obj_id="C1",
            name="CVN_73",
            type="Sea+Watercraft+AircraftCarrier",
            samples=[
                (time, DECK_LATITUDE, DECK_LONGITUDE, altitude_m, 0.0, 0.0)
                for time in (-120.0, 120.0)
            ],
        )
    }


def make_deck_approach_samples(
    *, final_altitude_m: float, speed_ms: float = 65.0
) -> list[TrackSample]:
    """Build a sea-referenced 3.5-degree carrier approach that levels at the deck."""
    tan_slope = math.tan(math.radians(3.5))
    samples: list[TrackSample] = []
    for time in range(-40, 21):
        if time < 0:
            distance = abs(time) * speed_ms
            altitude = final_altitude_m + distance * tan_slope
            latitude = DECK_LATITUDE - _lat_offset(distance)
        else:
            altitude = final_altitude_m
            latitude = DECK_LATITUDE
        samples.append(
            TrackSample(
                time=float(time),
                latitude=latitude,
                longitude=DECK_LONGITUDE,
                altitude=altitude,
                agl=altitude,
                speed=speed_ms if time < 0 else max(5.0, speed_ms - time * 3.0),
                heading=0.0,
                on_ground=None,
            )
        )
    return samples


def resolve_nimitz_deck_altitude(_: CarrierState) -> float:
    """Return the deck altitude for the synthetic Nimitz carrier."""
    return NIMITZ_DECK_ALTITUDE_M


def make_acmi_text(
    samples: list[TrackSample],
    *,
    include_carrier: bool = True,
    carrier_obj_id: str = "102",
    aircraft_obj_id: str = "101",
    aircraft_type: str = "Air+FixedWing",
    aircraft_name: str = "F/A-18C",
    pilot: str = "Viggen",
    base_time: float = 1000.0,
    recording_time: str | None = None,
) -> str:
    """Render samples as an ACMI 2.2 text stream (for ingest E2E tests).

    ``recording_time`` is what distinguishes two sessions of the SAME
    mission: ReferenceTime is the .miz's in-game date and is identical
    across them.
    """
    header = "0,ReferenceTime=2024-01-01T00:00:00Z,DataSource=Test,Title=synthetic"
    if recording_time is not None:
        header += f",RecordingTime={recording_time}"
    lines = [
        "FileType=text/acmi/tacview",
        "FileVersion=2.2",
        header,
    ]
    if include_carrier:
        lines.append("#0")
        lines.append(
            f"{carrier_obj_id},Type=Sea+Watercraft+AircraftCarrier,Name=CV-59,T={LON0}|{LAT0}|{DECK_ALTITUDE_M}|0|0|0"
        )
    for sample in samples:
        absolute = base_time + sample.time
        # ``#<seconds>`` is the absolute offset from ReferenceTime, not a
        # delta onto the previous frame (see app/acmi/parser.py).
        lines.append(f"#{absolute:g}")
        transform = (
            f"T={sample.longitude:g}|{sample.latitude:g}|{sample.altitude:g}|||"
            f"{sample.heading or 0:g}"
        )
        properties = [transform]
        if sample.time == samples[0].time:
            # Identity properties are emitted once, on the first update.
            properties.append(f"Type={aircraft_type}")
            properties.append(f"Name={aircraft_name}")
            properties.append(f"Pilot={pilot}")
        if sample.on_ground is not None:
            properties.append(f"OnGround={'1' if sample.on_ground else '0'}")
        if sample.speed is not None:
            properties.append(f"TAS={sample.speed:g}")
        lines.append(f"{aircraft_obj_id},{','.join(properties)}")
    return "\n".join(lines) + "\n"


def make_acmi_text_multi(
    aircraft: list[dict],
    *,
    include_carrier: bool = True,
    carrier_obj_id: str = "102",
    base_time: float = 1000.0,
) -> str:
    """Render several aircraft as ONE interleaved ACMI text stream.

    ``aircraft`` items are dicts with keys ``obj_id`` and ``samples``
    (a :class:`TrackSample` list from :func:`make_approach_samples`) plus
    optional ``name`` / ``pilot`` / ``type``. Updates from all aircraft are
    merged and sorted by absolute time so the stream interleaves both
    tracks (Issue #6 regression coverage).
    """
    lines = [
        "FileType=text/acmi/tacview",
        "FileVersion=2.2",
        "0,ReferenceTime=2024-01-01T00:00:00Z,DataSource=Test,Title=synthetic",
    ]
    if include_carrier:
        lines.append("#0")
        lines.append(
            f"{carrier_obj_id},Type=Sea+Watercraft+AircraftCarrier,Name=CV-59,T={LON0}|{LAT0}|{DECK_ALTITUDE_M}|0|0|0"
        )

    events: list[tuple[float, int, str, TrackSample, dict]] = []
    for index, spec in enumerate(aircraft):
        for _, sample in enumerate(spec["samples"]):
            absolute = base_time + sample.time
            events.append((absolute, index, spec["obj_id"], sample, spec))
    events.sort(key=lambda e: (e[0], e[1]))

    seen_ids: set[str] = set()
    for absolute, _index, obj_id, sample, spec in events:
        # ``#<seconds>`` is the absolute offset from ReferenceTime, not a
        # delta onto the previous frame (see app/acmi/parser.py).
        lines.append(f"#{absolute:g}")
        transform = (
            f"T={sample.longitude:g}|{sample.latitude:g}|{sample.altitude:g}|||"
            f"{sample.heading or 0:g}"
        )
        properties = [transform]
        if obj_id not in seen_ids:
            # Identity properties are emitted once, on the first update.
            seen_ids.add(obj_id)
            properties.append(f"Type={spec.get('type', 'Air+FixedWing')}")
            properties.append(f"Name={spec.get('name', 'F/A-18C')}")
            properties.append(f"Pilot={spec.get('pilot', 'Viggen')}")
        if sample.on_ground is not None:
            properties.append(f"OnGround={'1' if sample.on_ground else '0'}")
        if sample.speed is not None:
            properties.append(f"TAS={sample.speed:g}")
        lines.append(f"{obj_id},{','.join(properties)}")
    return "\n".join(lines) + "\n"


def analysis_with_gs_deviations(devs: list[float]):
    """指定のグライドスロープ偏差を持つ analysis を組む。

    偏差は AGL に埋め込む: 採点は角度 (AGL と距離の比) で行うので、
    ``glideslope_deviation`` だけを差し替えて AGL を放置すると幾何的に
    矛盾したサンプルになり、何をテストしているのか分からなくなる。
    距離は ±30 m の偏差が現実的な範囲に収まるよう最終進入相当に取る。

    滑走路が解決できた進入として組む (``distance_to_threshold`` を入れ、
    geometry を runway にする)。一定オフセットの高低を測れるのは
    照準点基準の測り方だけで、滑走路が無いときの経路角フィットは
    平行移動を原理的に見ない --- そちらで組むと「-30 m 低い進入」が
    誤差ゼロとして通り、何も検証しないテストになる。
    """
    import math

    from app.grading.deviations import ApproachAnalysis, DeviationSample

    touchdown_time = 100.0
    tan_slope = math.tan(math.radians(3.0))
    samples = []
    for i, dev in enumerate(devs):
        distance_to_go = 1250.0 + (len(devs) - 1 - i) * 250.0
        samples.append(
            DeviationSample(
                time=touchdown_time - len(devs) + i,
                distance_to_go=distance_to_go,
                glideslope_deviation=dev,
                centerline_deviation=1.0,
                speed=70.0,
                agl=distance_to_go * tan_slope + dev,
                distance_to_threshold=distance_to_go - 300.0,
            )
        )
    return ApproachAnalysis(
        kind="land",
        outcome="full_stop",
        glideslope_deg=3.0,
        course_deg=0.0,
        touchdown_time=touchdown_time,
        touchdown_speed_ms=70.0,
        touchdown_descent_rate_ms=1.0,
        geometry={"kind": "runway", "airbase": "TEST", "name": "09"},
        samples=samples,
    )
