"""Rebuilding a stored landing from the raw ``tracks`` table.

A regrade re-reads the approach a row stored, so it can never give a carrier
trap stored with the old 60 s window the break it never kept. The rebuild
reads the aircraft's (and its ship's) raw samples back from ``tracks`` and
runs today's detector and graders on them.

Every row here is produced the way production produces it -- ACMI lines
through ``TrackIngestor.handle_line`` -- so the raw samples the rebuild reads
are the ones the ingestor actually wrote, not a fixture shaped to fit.
"""

from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.api.errors import AppError
from app.grading.carriers import load_carrier_geometry_book
from app.grading.config import apply_config_overrides, load_grading_config
from app.ingest import TrackIngestor
from app.models.entities import DcsObject, Landing, Track
from app.pipeline import LandingPipeline
from tests.case1 import fly_case1
from tests.conftest import GRADING_YAML
from tests.helpers import (
    DECK_ALTITUDE_M,
    LAT0,
    LON0,
    make_acmi_text,
    make_approach_samples,
)

CARRIERS_YAML = Path(__file__).resolve().parents[2] / "config" / "carriers.yaml"

#: What carriers were captured with until 2026-09-26.
LEGACY_CAPTURE = {"approach": {"carrier_window_s": 60.0, "carrier_distance_m": 3704.0}}


def _pipeline(session_factory, overrides=None) -> LandingPipeline:
    config = load_grading_config(GRADING_YAML)
    if overrides:
        config = apply_config_overrides(config, overrides)
    return LandingPipeline(
        session_factory, config, carrier_geometry_book=load_carrier_geometry_book(CARRIERS_YAML)
    )


async def _ingest(session_factory, lines: list[str], pipeline: LandingPipeline) -> None:
    ingestor = TrackIngestor(
        session_factory,
        landing_listener=pipeline.handle_landing,
        landing_finalize_listener=pipeline.finalize_landing,
        detection_config=pipeline._config.to_detection_config(),  # noqa: SLF001
        deck_altitude_for=pipeline.deck_altitude_for,
    )
    try:
        for line in lines:
            await ingestor.handle_line(line)
    finally:
        await ingestor.close()


async def _rows(session_factory) -> list[Landing]:
    async with session_factory() as session:
        return list(
            (await session.execute(select(Landing).order_by(Landing.id))).scalars().all()
        )


async def test_a_carrier_row_stored_with_60_s_gets_its_kissoff_back(session_factory) -> None:
    case = fly_case1()
    await _ingest(session_factory, case.lines, _pipeline(session_factory, LEGACY_CAPTURE))
    (old,) = await _rows(session_factory)
    stored = old.approach_track["samples"]
    # The legacy capture: the last minute, starting well after the kiss-off.
    assert stored[0]["time"] > case.expect["kissoff_time"] + 30.0
    # Nothing the rebuild reads is on the row: drop what it stored entirely.
    async with session_factory() as session:
        row = await session.get(Landing, old.id)
        row.approach_track = None
        row.grading_version = "6"
        await session.commit()

    payload = await _pipeline(session_factory).rebuild(old)

    (row,) = await _rows(session_factory)
    samples = row.approach_track["samples"]
    assert payload["approach_start_time"] == samples[0]["time"]
    assert samples[0]["time"] <= case.expect["kissoff_time"] - 30.0
    m = row.metrics
    assert m["deck_frame"] == "moving_deck"
    assert m["pattern_entry"] == "initial"
    assert m["pattern_break_along_ship_m"] == pytest.approx(case.expect["kissoff_x"], abs=80.0)
    assert m["pattern_abeam_distance_m"] == pytest.approx(case.expect["abeam_m"], abs=30.0)
    assert row.grading_version == "7"
    assert row.grade == "OK"
    assert "キスオフ" in row.comment


def _same(a: dict, b: dict, key: str, tolerance: float) -> None:
    left, right = a.get(key), b.get(key)
    if left is None or right is None:
        assert left == right, key
    else:
        assert math.isclose(left, right, abs_tol=tolerance), (key, left, right)


async def test_rebuilding_a_current_carrier_row_changes_nothing(session_factory) -> None:
    """The raw samples rebuild to what live ingest saw: same speeds, same cut.

    If the rebuild derived ground speed or dropped position jumps any
    differently from the ingestor, FAST / SLOW and the G would move on a row
    that was only being re-cut.
    """
    await _ingest(session_factory, fly_case1().lines, _pipeline(session_factory))
    (before,) = await _rows(session_factory)

    await _pipeline(session_factory).rebuild(before)

    (after,) = await _rows(session_factory)
    assert after.touchdown_time == before.touchdown_time
    assert after.grade == before.grade
    assert after.factors == before.factors
    assert len(after.approach_track["samples"]) == len(before.approach_track["samples"])
    for key, tolerance in (
        ("pattern_break_max_load_factor", 0.02),
        ("pattern_break_along_ship_m", 1.0),
        ("pattern_abeam_distance_m", 1.0),
        ("pattern_groove_time_s", 0.11),
        ("touchdown_descent_rate_fpm", 0.5),
        ("ramp_sink_ratio", 0.01),
    ):
        _same(before.metrics, after.metrics, key, tolerance)
    speeds = [(s["time"], s["speed"]) for s in before.approach_track["samples"]]
    assert speeds == [(s["time"], s["speed"]) for s in after.approach_track["samples"]]


async def test_a_position_glitch_is_dropped_again_on_the_rebuild(session_factory) -> None:
    """``tracks`` keeps the glitched fix as it arrived; live ingest dropped
    its coordinates before the detector saw it. Unless the rebuild drops it
    again, the re-cut downwind carries an 18 km spike -- and the ground
    speed and G derived around it."""
    lines = list(fly_case1().lines)
    aircraft = [i for i, line in enumerate(lines) if line.startswith("A1,T=")]
    glitch = aircraft[len(aircraft) // 2]
    longitude, rest = lines[glitch][len("A1,T="):].split("|", 1)
    lines[glitch] = f"A1,T={float(longitude) + 0.2:.7f}|{rest}"
    await _ingest(session_factory, lines, _pipeline(session_factory))
    (before,) = await _rows(session_factory)

    await _pipeline(session_factory).rebuild(before)

    (after,) = await _rows(session_factory)
    samples_before = before.approach_track["samples"]
    samples_after = after.approach_track["samples"]
    assert len(samples_after) == len(samples_before)
    assert [s["speed"] for s in samples_after] == [s["speed"] for s in samples_before]
    _same(before.metrics, after.metrics, "pattern_break_max_load_factor", 0.02)


async def test_rebuilding_a_land_row_changes_nothing(session_factory) -> None:
    text = make_acmi_text(
        make_approach_samples(outcome="full_stop", pre_touchdown_descent_ms=1.2),
        include_carrier=False,
    )
    await _ingest(session_factory, text.splitlines(), _pipeline(session_factory))
    (before,) = await _rows(session_factory)
    assert before.kind == "land"

    payload = await _pipeline(session_factory).rebuild(before)

    (after,) = await _rows(session_factory)
    assert payload["kind"] == "land"
    assert after.score == before.score
    assert after.grade == before.grade
    assert after.approach_track["samples"] == before.approach_track["samples"]


async def test_a_row_with_no_landing_in_its_raw_track_is_left_alone(session_factory) -> None:
    """Like the objects that hit the water beside a ship and were once stored
    as "carrier landings": today's detector finds nothing there, and the row
    must come out of a rebuild exactly as it went in."""
    await _ingest(session_factory, fly_case1().lines, _pipeline(session_factory))
    (landing,) = await _rows(session_factory)
    async with session_factory() as session:
        row = await session.get(Landing, landing.id)
        # Thirty seconds before the trap the jet was in the 180: no contact
        # there. The trap itself IS inside the window read back, and it must
        # not be taken for this row -- rewriting a row with a different
        # landing is worse than leaving it alone.
        row.touchdown_time = landing.touchdown_time - 30.0
        await session.commit()
    (before,) = await _rows(session_factory)

    with pytest.raises(AppError) as raised:
        await _pipeline(session_factory).rebuild(before)

    assert raised.value.status_code == 409
    assert raised.value.error_code == "REBUILD_NO_MATCH"
    (after,) = await _rows(session_factory)
    assert after.approach_track == before.approach_track
    assert after.grade == before.grade
    assert after.touchdown_time == before.touchdown_time


async def test_a_parachutist_row_is_not_rebuilt_into_a_landing(session_factory) -> None:
    """Both "landings" in the local real recording are ejected pilots.

    They were stored before the classifier required a LEADING "Air+"
    (``Ground+Light+Human+Air+Parachutist`` contains it halfway down). The
    detector itself does not look at types, so re-cutting such a row would
    re-create the landing live ingest now refuses.
    """
    await _ingest(session_factory, fly_case1().lines, _pipeline(session_factory))
    (landing,) = await _rows(session_factory)
    async with session_factory() as session:
        obj = await session.get(DcsObject, landing.object_id)
        obj.type = "Ground+Light+Human+Air+Parachutist"
        await session.commit()

    with pytest.raises(AppError) as raised:
        await _pipeline(session_factory).rebuild(landing)

    assert raised.value.error_code == "NOT_AN_AIRCRAFT"
    (after,) = await _rows(session_factory)
    assert after.approach_track == landing.approach_track


async def test_the_ship_comes_from_the_raw_data_not_from_the_row(session_factory) -> None:
    """Live ingest gives the detector every carrier it tracks, and the
    detector decides which deck is under the jet. The row's ship is that
    decision's output: a rebuild that only offered the ship the row already
    named could never correct it, and with no ship named would read a trap
    as a jet sitting 20 m above the sea."""
    await _ingest(session_factory, fly_case1().lines, _pipeline(session_factory))
    (landing,) = await _rows(session_factory)
    ship_row_id = landing.carrier_object_id
    assert ship_row_id is not None
    async with session_factory() as session:
        row = await session.get(Landing, landing.id)
        row.carrier_object_id = None
        await session.commit()
    (before,) = await _rows(session_factory)

    payload = await _pipeline(session_factory).rebuild(before)

    (after,) = await _rows(session_factory)
    assert payload["kind"] == "carrier"
    assert after.carrier_object_id == ship_row_id
    assert after.metrics["deck_frame"] == "moving_deck"


def _without_wow(samples):
    return [replace(sample, on_ground=None) for sample in samples]


async def test_a_recording_without_agl_rebuilds_on_the_ground_reference(
    session_factory,
) -> None:
    """A recording with neither AGL nor OnGround (the local real one is
    like that) only shows a landing through the height above the nearest
    static object. Live ingest measures from it, so the rebuild must too,
    or every such row comes back as "no landing here"."""
    samples = _without_wow(make_approach_samples(outcome="full_stop"))
    lines = make_acmi_text(samples, include_carrier=False).splitlines()
    # A hangar 1.5 km off the touchdown, at the field's elevation.
    hangar = (
        f"301,T={LON0 + 0.0165:g}|{LAT0:g}|{DECK_ALTITUDE_M:g},"
        "Type=Ground+Static+Building,Name=Hangar"
    )
    lines[3:3] = ["#0", hangar]
    await _ingest(session_factory, lines, _pipeline(session_factory))
    (before,) = await _rows(session_factory)
    assert before.kind == "land"

    await _pipeline(session_factory).rebuild(before)

    (after,) = await _rows(session_factory)
    assert after.touchdown_time == before.touchdown_time
    assert after.grade == before.grade
    assert after.approach_track["samples"] == before.approach_track["samples"]


async def test_a_row_whose_raw_track_is_gone_says_so(session_factory) -> None:
    await _ingest(session_factory, fly_case1().lines, _pipeline(session_factory))
    (landing,) = await _rows(session_factory)
    async with session_factory() as session:
        await session.execute(delete(Track).where(Track.object_id == landing.object_id))
        await session.commit()

    with pytest.raises(AppError) as raised:
        await _pipeline(session_factory).rebuild(landing)

    assert raised.value.status_code == 409
    assert raised.value.error_code == "NO_RAW_TRACK"


async def test_the_rebuild_endpoint(tmp_path) -> None:
    import httpx

    from app.api.main import create_app
    from app.config import Settings

    app = create_app(
        Settings(
            database_url=f"sqlite+aiosqlite:///{(tmp_path / 'api.db').as_posix()}",
            acmi_enabled=False,
            grading_config_path=str(GRADING_YAML),
            carriers_config_path=str(CARRIERS_YAML),
        )
    )
    async with app.router.lifespan_context(app):
        await _ingest(app.state.session_factory, fly_case1().lines, app.state.pipeline)
        async with app.state.session_factory() as session:
            landing_id = (await session.execute(select(Landing.id))).scalar_one()
            aircraft = (
                await session.execute(select(DcsObject).where(DcsObject.acmi_id == "A1"))
            ).scalar_one()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            ok = await http.post(f"/api/v1/landings/{landing_id}/rebuild")
            missing = await http.post("/api/v1/landings/999999/rebuild")

    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["kind"] == "carrier"
    assert body["approach_samples"] > 700
    assert body["metrics"]["pattern_entry"] == "initial"
    assert aircraft.name == "FA-18C_hornet"
    assert missing.status_code == 404
