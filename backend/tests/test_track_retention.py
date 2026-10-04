"""Raw track retention: only the landing windows reach ``tracks``.

The ingestor used to write every update of every object; the rebuild of a
stored landing is the only reader and it reads one span around the
touchdown (app/retention.py). Every recording here goes through
``TrackIngestor.handle_line`` twice -- once keeping everything, once with
the default retention -- so the rows retention keeps are checked against the
very rows the old ingest would have written, and the rebuild against the one
it would have made from them.
"""

from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.detection.geometry import offset_position
from app.grading.carriers import load_carrier_geometry_book
from app.grading.config import load_grading_config
from app.ingest import TrackIngestor
from app.models.database import create_engine, create_session_factory
from app.models.entities import DcsObject, Landing, Track
from app.pipeline import LandingPipeline
from app.retention import BOUNCE_ALLOWANCE_S, rebuild_window, retention_window
from tests.case1 import fly_case1
from tests.conftest import GRADING_YAML
from tests.helpers import LAT0, LON0, make_acmi_text, make_approach_samples
from tests.helpers import create_async_test_schema

CARRIERS_YAML = Path(__file__).resolve().parents[2] / "config" / "carriers.yaml"


def pipeline_for(session_factory) -> LandingPipeline:
    return LandingPipeline(
        session_factory,
        load_grading_config(GRADING_YAML),
        carrier_geometry_book=load_carrier_geometry_book(CARRIERS_YAML),
    )


async def ingest(session_factory, lines: list[str], *, keep_all: bool = False) -> TrackIngestor:
    pipeline = pipeline_for(session_factory)
    ingestor = TrackIngestor(
        session_factory,
        landing_listener=pipeline.handle_landing,
        landing_finalize_listener=pipeline.finalize_landing,
        detection_config=pipeline._config.to_detection_config(),  # noqa: SLF001
        deck_altitude_for=pipeline.deck_altitude_for,
        keep_all_tracks=keep_all,
    )
    for line in lines:
        await ingestor.handle_line(line)
    await ingestor.close()
    return ingestor


@pytest.fixture
async def second_db(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'full.db').as_posix()}")
    await create_async_test_schema(engine)
    yield create_session_factory(engine)
    await engine.dispose()


TrackRow = tuple[
    float, float | None, float | None, float | None, float | None, float | None, bool | None
]


async def rows_by_object(session_factory) -> dict[str, list[TrackRow]]:
    async with session_factory() as session:
        result = await session.execute(
            select(
                DcsObject.acmi_id,
                Track.mission_time,
                Track.latitude,
                Track.longitude,
                Track.altitude,
                Track.roll,
                Track.agl,
                Track.on_ground,
            )
            .join(DcsObject, DcsObject.id == Track.object_id)
            .order_by(Track.mission_time, Track.id)
        )
        out: dict[str, list[TrackRow]] = {}
        for row in result.all():
            out.setdefault(row[0], []).append(
                (row[1], row[2], row[3], row[4], row[5], row[6], row[7])
            )
        return out


async def landings(session_factory) -> list[Landing]:
    async with session_factory() as session:
        return list((await session.execute(select(Landing).order_by(Landing.id))).scalars())


def detection():
    return load_grading_config(GRADING_YAML).to_detection_config()


def land_lines() -> list[str]:
    """A 20-minute straight-in to a full stop, with a missile and a hangar.

    Touchdown at t=1000 (base_time); the aircraft is seen from t=-200 --
    far outside any window -- and sits on the runway for five minutes.
    """
    samples = make_approach_samples(duration_before_s=1200.0, ground_time_s=300.0)
    lines = make_acmi_text(samples, include_carrier=False).splitlines()
    out: list[str] = []
    missile_seen = hangar_seen = False
    for line in lines:
        out.append(line)
        if not line.startswith("#"):
            continue
        time = float(line[1:])
        if not hangar_seen:
            out.append(
                f"301,T={LON0 + 0.0165:g}|{LAT0:g}|20,Type=Ground+Static+Building,Name=Hangar"
            )
            hangar_seen = True
        if 700 <= time <= 1100:
            identity = ",Type=Weapon+Missile,Name=AIM_120C" if not missile_seen else ""
            out.append(f"M1,T={LON0 + 0.2 + time / 1e4:g}|{LAT0:g}|3000{identity}")
            missile_seen = True
    return out


async def test_only_the_landing_window_of_an_aircraft_is_kept(session_factory, second_db) -> None:
    lines = land_lines()
    await ingest(session_factory, lines)
    await ingest(second_db, lines, keep_all=True)
    kept = await rows_by_object(session_factory)
    full = await rows_by_object(second_db)
    (landing,) = await landings(session_factory)
    touchdown = landing.touchdown_time
    assert touchdown is not None

    # What the rebuild reads is all there, row for row as the old ingest
    # wrote it; and nothing outside the retention span is.
    start, end = retention_window(touchdown, touchdown, detection())
    assert set(kept["101"]) <= set(full["101"])
    assert {r for r in full["101"] if start <= r[0] <= end} <= set(kept["101"])
    assert all(start - BOUNCE_ALLOWANCE_S <= r[0] <= end for r in kept["101"])
    read_start, read_end = rebuild_window(touchdown, detection())
    assert kept["101"][0][0] <= read_start and kept["101"][-1][0] >= read_end
    assert len(kept["101"]) < len(full["101"]) / 2
    # Nothing reads a missile; the rebuild's ground reference reads statics.
    assert "M1" in full and "M1" not in kept
    assert kept["301"] == full["301"]


async def test_the_rebuild_is_the_same_from_the_windows_as_from_everything(
    session_factory, second_db
) -> None:
    """The retained rows ARE what the rebuild needs: the same landing,
    rebuilt from the windows and from the full history, comes out the
    same -- carrier trap, moving deck, break and all."""
    lines = with_transit(fly_case1().lines)
    await ingest(session_factory, lines)
    await ingest(second_db, lines, keep_all=True)
    (windowed,) = await landings(session_factory)
    (complete,) = await landings(second_db)
    assert windowed.kind == complete.kind == "carrier"

    await pipeline_for(session_factory).rebuild(windowed)
    await pipeline_for(second_db).rebuild(complete)

    (from_windows,) = await landings(session_factory)
    (from_all,) = await landings(second_db)
    assert from_windows.touchdown_time == from_all.touchdown_time
    assert from_windows.grade == from_all.grade
    assert from_windows.factors == from_all.factors
    assert from_windows.metrics == from_all.metrics
    assert from_windows.approach_track == from_all.approach_track

    kept = await rows_by_object(session_factory)
    full = await rows_by_object(second_db)
    assert windowed.touchdown_time is not None
    start, end = retention_window(windowed.touchdown_time, windowed.touchdown_time, detection())
    for acmi_id in ("A1", "C1"):  # the jet and the ship
        assert {r for r in full[acmi_id] if start <= r[0] <= end} <= set(kept[acmi_id])
        assert all(start - BOUNCE_ALLOWANCE_S <= r[0] <= end for r in kept[acmi_id])
    # The transit before the pattern, and the ship steaming meanwhile.
    assert len(kept["C1"]) < len(full["C1"]) / 2


def with_transit(lines: list[str], seconds: float = 1500.0) -> list[str]:
    """Prepend a long transit: the jet inbound from 60 km, the ship steaming.

    The ship's earlier positions continue its recorded course backwards and
    the jet's end where the Case I begins, so nothing jumps.
    """
    header, frames = lines[:3], lines[3:]

    def position(line: str) -> tuple[float, float, float]:
        lon, lat, alt = line.split("T=")[1].split("|")[:3]
        return float(lon), float(lat), float(alt)

    ship = [line for line in frames if line.startswith("C1,")]
    jet = next(line for line in frames if line.startswith("A1,"))
    first_time = float(next(line for line in frames if line.startswith("#"))[1:])
    lon0, lat0, _ = position(ship[0])
    lon1, lat1, _ = position(ship[1])
    heading = float(ship[0].split("|", 5)[5].split(",")[0])
    jet_lon, jet_lat, jet_alt = position(jet)
    far_lat, far_lon = offset_position(jet_lat, jet_lon, 220.0, 60000.0, 0.0)
    early: list[str] = []
    step = 0
    time = first_time - seconds
    while time < first_time - 0.25:
        back = first_time - time
        frac = back / seconds  # 1 at the far end, 0 at the Case I
        early.append(f"#{time:.2f}")
        ship_identity = ",Type=Sea+Watercraft+AircraftCarrier,Name=CVN_73" if step == 0 else ""
        early.append(
            f"C1,T={lon0 - (lon1 - lon0) * back / 0.5:.7f}|"
            f"{lat0 - (lat1 - lat0) * back / 0.5:.7f}|0.0|||{heading:.2f}{ship_identity}"
        )
        if step % 2 == 0:
            identity = ",Type=Air+FixedWing,Name=FA-18C_hornet,Pilot=CaseOne" if step == 0 else ""
            alt = jet_alt + (3000.0 - jet_alt) * frac
            early.append(
                f"A1,T={jet_lon + (far_lon - jet_lon) * frac:.7f}|"
                f"{jet_lat + (far_lat - jet_lat) * frac:.7f}|{alt:.2f}|0.0|0.0|40.0,"
                f"AGL={alt:.2f}{identity}"
            )
        time += 0.5
        step += 1
    # The original first frames re-announce the identities; harmless.
    return header + early + frames


async def test_a_vanishing_aircraft_writes_its_window_at_once(session_factory) -> None:
    """Removed right after the touchdown: its tail is never coming, so the
    window is written with what there is -- before the ingestor closes."""
    samples = make_approach_samples(duration_before_s=600.0, ground_time_s=30.0)
    lines = make_acmi_text(samples, include_carrier=False).splitlines()
    lines += ["#1040", "-101"]
    pipeline = pipeline_for(session_factory)
    ingestor = TrackIngestor(
        session_factory,
        landing_listener=pipeline.handle_landing,
        landing_finalize_listener=pipeline.finalize_landing,
        detection_config=pipeline._config.to_detection_config(),  # noqa: SLF001
    )
    for line in lines:
        await ingestor.handle_line(line)
    # Commit the batch, but deliberately no close(). The removal once staged
    # the rows after the landing pass had closed the batch, and flush() had
    # no session to commit them into -- they sat in memory, and close()
    # would have dropped them the same way.
    await ingestor.flush()

    kept = await rows_by_object(session_factory)
    (landing,) = await landings(session_factory)
    assert landing.touchdown_time is not None
    assert kept["101"][-1][0] == pytest.approx(landing.touchdown_time + 30.0)
    assert kept["101"][0][0] == pytest.approx(
        retention_window(landing.touchdown_time, landing.touchdown_time, detection())[0], abs=1.0
    )
    assert "101" not in ingestor._raw_history  # noqa: SLF001 - freed for the next owner
    await ingestor.close()


async def test_a_session_restart_writes_the_old_sessions_windows(session_factory) -> None:
    samples = make_approach_samples(duration_before_s=600.0, ground_time_s=30.0)
    first = make_acmi_text(samples, include_carrier=False, recording_time="2026-01-01T10:00:00Z")
    second = make_acmi_text(
        samples[:5], include_carrier=False, recording_time="2026-01-01T12:00:00Z"
    )
    pipeline = pipeline_for(session_factory)
    ingestor = TrackIngestor(
        session_factory,
        landing_listener=pipeline.handle_landing,
        landing_finalize_listener=pipeline.finalize_landing,
        detection_config=pipeline._config.to_detection_config(),  # noqa: SLF001
    )
    for line in first.splitlines() + second.splitlines():
        await ingestor.handle_line(line)
    await ingestor.flush()

    async with session_factory() as session:
        object_ids = (await session.execute(select(DcsObject.id).order_by(DcsObject.id))).scalars()
        first_object = next(iter(object_ids))
        rows = (
            await session.execute(
                select(func.count()).select_from(Track).where(Track.object_id == first_object)
            )
        ).scalar_one()
    (landing,) = await landings(session_factory)
    assert landing.object_id == first_object
    # The first session's jet: approach and roll-out up to where it stopped.
    assert rows >= 400
    await ingestor.close()


async def test_a_touch_and_go_then_a_full_stop_write_each_row_once(session_factory) -> None:
    """Two landings 200 s apart have overlapping windows, and the second is
    only detected after the first window was written. No row twice, no gap."""
    touch = make_approach_samples(outcome="touch_and_go", duration_before_s=600.0, ground_time_s=20.0)
    stop = [
        replace(s, time=s.time + 200.0)
        for s in make_approach_samples(duration_before_s=60.0, ground_time_s=30.0)
    ]
    samples = touch + [s for s in stop if s.time > touch[-1].time]
    lines = make_acmi_text(samples, include_carrier=False).splitlines()
    # These tracks carry OnGround but no AGL: without a ground reference the
    # climb-out has no height, and the second contact reads as a bounce.
    lines[3:3] = [
        "#0",
        f"301,T={LON0 + 0.0165:g}|{LAT0:g}|20,Type=Ground+Static+Building,Name=Hangar",
    ]
    await ingest(session_factory, lines)

    stored = await landings(session_factory)
    assert [row.outcome for row in stored] == ["touch_and_go", "full_stop"]
    kept = await rows_by_object(session_factory)
    times = [r[0] for r in kept["101"]]
    assert len(times) == len(set(times))
    assert stored[0].touchdown_time is not None
    assert stored[1].touchdown_time is not None
    first_start, _ = retention_window(stored[0].touchdown_time, stored[0].touchdown_time, detection())
    _, last_end = retention_window(stored[1].touchdown_time, stored[1].touchdown_time, detection())
    expected = [s.time + 1000.0 for s in samples if first_start <= s.time + 1000.0 <= last_end]
    assert times == pytest.approx(expected)


async def test_keep_all_tracks_is_wired_from_the_settings(database_url: str) -> None:
    from app.api.main import create_app
    from app.config import Settings
    from tests.helpers import create_test_schema

    create_test_schema(database_url)
    app = create_app(
        Settings(
            database_url=database_url,
            acmi_enabled=False,
            grading_config_path=str(GRADING_YAML),
            keep_all_tracks=True,
        )
    )
    async with app.router.lifespan_context(app):
        assert app.state.import_manager._keep_all_tracks is True  # noqa: SLF001


def test_the_history_reaches_back_over_a_whole_window() -> None:
    from app.retention import retention_history_s

    config = detection()
    start, end = retention_window(0.0, 0.0, config)
    assert retention_history_s(config) >= end - start
    assert math.isclose(end - start, 360.0 + 60.0 + 2 * 60.0)


def deck_traffic_lines(count: int, spacing_s: float) -> list[str]:
    """``count`` jets landing on one ship ``spacing_s`` apart.

    The ship reports every second for the whole half hour. Its windows
    overlap all the way through, so they merge into one that keeps ending
    two minutes after the latest landing -- long after the first rows have
    left the in-memory history.
    """
    frames: dict[float, list[str]] = {}
    end = (count - 1) * spacing_s + 30.0
    time = -100.0
    while time <= end:
        identity = ",Type=Sea+Watercraft+AircraftCarrier,Name=Unlisted" if time == -100.0 else ""
        frames.setdefault(time, []).append(f"102,T={LON0:g}|{LAT0:g}|20|0|0|0{identity}")
        time += 1.0
    for k in range(count):
        samples = make_approach_samples(duration_before_s=60.0, ground_time_s=20.0)
        for index, sample in enumerate(samples):
            props = [f"T={sample.longitude:g}|{sample.latitude:g}|{sample.altitude:g}|||0"]
            if index == 0:
                props += ["Type=Air+FixedWing", "Name=F/A-18C", f"Pilot=Jet{k}"]
            props += [f"OnGround={'1' if sample.on_ground else '0'}", f"TAS={sample.speed:g}"]
            frames.setdefault(sample.time + k * spacing_s, []).append(f"A{k},{','.join(props)}")
    lines = [
        "FileType=text/acmi/tacview",
        "FileVersion=2.2",
        "0,ReferenceTime=2024-01-01T00:00:00Z,RecordingTime=2024-01-01T00:00:00Z",
    ]
    for time in sorted(frames):
        lines.append(f"#{1000.0 + time:g}")
        lines.extend(frames[time])
    return lines


async def ingest_plain(session_factory, lines: list[str], *, keep_all: bool = False) -> None:
    """Without the geometry book: the unlisted ship has no known deck, and
    OnGround alone says the jets are down."""
    pipeline = LandingPipeline(session_factory, load_grading_config(GRADING_YAML))
    ingestor = TrackIngestor(
        session_factory,
        landing_listener=pipeline.handle_landing,
        landing_finalize_listener=pipeline.finalize_landing,
        detection_config=pipeline._config.to_detection_config(),  # noqa: SLF001
        deck_altitude_for=pipeline.deck_altitude_for,
        keep_all_tracks=keep_all,
    )
    for line in lines:
        await ingestor.handle_line(line)
    await ingestor.close()


async def test_a_busy_deck_keeps_the_ship_for_every_landing(session_factory, second_db) -> None:
    """Landings 90 s apart for half an hour: every one still finds its
    ship's samples. (They used to be written only when the merged window
    ended, and by then the history had long dropped the first 25 minutes.)"""
    lines = deck_traffic_lines(count=20, spacing_s=90.0)
    await ingest_plain(session_factory, lines)
    await ingest_plain(second_db, lines, keep_all=True)
    stored = await landings(session_factory)
    assert len(stored) == 20
    assert all(row.kind == "carrier" and row.carrier_object_id for row in stored)

    kept = await rows_by_object(session_factory)
    full = await rows_by_object(second_db)
    ship = [r[0] for r in kept["102"]]
    assert len(ship) == len(set(ship))
    for landing in stored:
        assert landing.touchdown_time is not None
        start, end = rebuild_window(landing.touchdown_time, detection())
        needed = {r for r in full["102"] if start <= r[0] <= end}
        assert needed <= set(kept["102"]), landing.touchdown_time


async def test_a_ship_the_landing_was_not_on_is_not_kept(session_factory) -> None:
    """A carrier 3 km off the runway has nothing to do with a land landing
    (the detector looks at ships within 800 m), so none of it is written."""
    out: list[str] = []
    for line in land_lines():
        out.append(line)
        if line.startswith("#") and float(line[1:]) % 10 == 0:
            identity = ",Type=Sea+Watercraft+AircraftCarrier,Name=CVN_73" if len(out) < 10 else ""
            out.append(f"C9,T={LON0 + 0.033:g}|{LAT0:g}|0|0|0|0{identity}")
    await ingest(session_factory, out)
    (landing,) = await landings(session_factory)
    assert landing.kind == "land" and landing.carrier_object_id is None
    kept = await rows_by_object(session_factory)
    assert "C9" not in kept
    assert "101" in kept


async def test_a_failed_batch_keeps_the_landing_window_for_the_next(tmp_path, second_db) -> None:
    """The database refuses the batch that carries a landing's window: the
    rows are already out of the window and the history, so dropping them
    with the batch would lose the landing's raw data for good."""
    from sqlalchemy import event, text

    url = f"sqlite+aiosqlite:///{(tmp_path / 'locked.db').as_posix()}"
    engine = create_engine(url)
    await create_async_test_schema(engine)

    @event.listens_for(engine.sync_engine, "connect")
    def _no_wait(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=0")
        cursor.close()

    session_factory = create_session_factory(engine)
    pipeline = pipeline_for(session_factory)
    ingestor = TrackIngestor(
        session_factory,
        landing_listener=pipeline.handle_landing,
        landing_finalize_listener=pipeline.finalize_landing,
        detection_config=pipeline._config.to_detection_config(),  # noqa: SLF001
    )
    lines = land_lines()  # touchdown at t=1000, window due at t=1120
    lock_from, lock_until = lines.index("#1030"), lines.index("#1200")
    for line in lines[:lock_from]:
        await ingestor.handle_line(line)

    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE _lock_probe (x INTEGER)"))
    blocker_engine = create_engine(url)
    blocker = await blocker_engine.connect()
    await blocker.execute(text("INSERT INTO _lock_probe VALUES (1)"))
    failures = 0
    for line in lines[lock_from:lock_until]:
        try:
            await ingestor.handle_line(line)
        except Exception:  # noqa: BLE001 - "database is locked", on purpose
            failures += 1
    await blocker.rollback()
    await blocker.close()
    await blocker_engine.dispose()
    for line in lines[lock_until:]:
        await ingestor.handle_line(line)
    await ingestor.close()

    await ingest(second_db, lines, keep_all=True)
    kept = await rows_by_object(session_factory)
    full = await rows_by_object(second_db)
    await engine.dispose()
    assert failures >= 1
    (landing,) = await landings(second_db)
    assert landing.touchdown_time is not None
    start, end = retention_window(landing.touchdown_time, landing.touchdown_time, detection())
    assert {r for r in full["101"] if start <= r[0] <= end} <= set(kept["101"])
