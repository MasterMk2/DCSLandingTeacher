"""Tests for the ingest pipeline (parser events -> DB), incl. batching."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import event, select

from app.ingest import TrackIngestor
from app.models.database import create_engine, create_session_factory, init_db
from app.models.entities import DcsObject, Flight, Track

FIXTURES = Path(__file__).parent / "fixtures"


async def feed_sample(ingestor: TrackIngestor) -> None:
    for line in (FIXTURES / "sample.acmi").read_text(encoding="utf-8").splitlines():
        await ingestor.handle_line(line)


async def test_ingest_persists_flight_objects_tracks(session_factory) -> None:
    ingestor = TrackIngestor(session_factory)
    await feed_sample(ingestor)
    await ingestor.close()

    async with session_factory() as session:
        flights = (await session.execute(select(Flight))).scalars().all()
        objects = (
            await session.execute(select(DcsObject).order_by(DcsObject.acmi_id))
        ).scalars().all()
        tracks = (
            await session.execute(select(Track).order_by(Track.mission_time))
        ).scalars().all()

    # One flight created from global-object metadata.
    assert len(flights) == 1
    assert flights[0].reference_time == "2011-06-02T05:00:00Z"
    assert flights[0].data_source == "DCS 2.9.4"

    # Two real objects; the carrier was removed by the stream.
    assert [o.acmi_id for o in objects] == ["101", "102"]
    aircraft = objects[0]
    assert aircraft.type == "Air+FixedWing"
    assert aircraft.name == "C172"
    assert aircraft.pilot == "Viggen"
    assert aircraft.removed is False
    carrier = objects[1]
    assert carrier.type == "Sea+Watercraft+AircraftCarrier"
    assert carrier.removed is True

    # Four track samples total: three for the aircraft (t=0, 47.13, 55.75)
    # plus one for the carrier at t=0.
    assert len(tracks) == 4
    aircraft_tracks = [t for t in tracks if t.object_id == aircraft.id]
    assert len(aircraft_tracks) == 3
    first, last = aircraft_tracks[0], aircraft_tracks[-1]
    assert first.mission_time == pytest.approx(0.0)
    assert first.latitude == pytest.approx(41.5910417)
    assert last.mission_time == pytest.approx(55.75)
    assert last.longitude == pytest.approx(41.63)
    assert last.altitude == pytest.approx(1999.50)


async def test_ingest_ignores_unparsable_lines(session_factory) -> None:
    ingestor = TrackIngestor(session_factory)
    await ingestor.handle_line("#not-a-number")  # must not raise
    await ingestor.handle_line("FileType=text/acmi/tacview")
    await ingestor.handle_line("101,T=41.6|41.5|100")
    await ingestor.close()

    async with session_factory() as session:
        tracks = (await session.execute(select(Track))).scalars().all()
    assert len(tracks) == 1


async def test_ingest_batches_commits(tmp_path) -> None:
    """Pending writes are committed only when max_batch_size is reached."""
    from sqlalchemy.orm import Session

    db_path = (tmp_path / "batch.db").as_posix()
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    await init_db(engine)

    commit_count = {"n": 0}

    def _count_commit(session):  # noqa: ANN001
        commit_count["n"] += 1

    event.listen(Session, "after_commit", _count_commit)

    session_factory = create_session_factory(engine)
    ingestor = TrackIngestor(session_factory, max_batch_size=2)
    try:
        # Three object updates -> two commits expected at batch size 2,
        # plus one pending write flushed by close().
        await ingestor.handle_line("FileType=text/acmi/tacview")
        await ingestor.handle_line("FileVersion=2.2")
        await ingestor.handle_line("#0.00")
        await ingestor.handle_line("101,T=41.60|41.50|100")
        await ingestor.handle_line("#1.00")
        await ingestor.handle_line("101,T=41.61||101")
        await ingestor.handle_line("#2.00")
        await ingestor.handle_line("101,T=41.62||102")

        commits_before_close = commit_count["n"]
        await ingestor.close()

        async with session_factory() as session:
            tracks = (
                await session.execute(select(Track).order_by(Track.mission_time))
            ).scalars().all()
        assert len(tracks) == 3
        # Batching: fewer commits than writes.
        assert commits_before_close == 2
        assert commit_count["n"] == 3  # final flush on close
    finally:
        event.remove(Session, "after_commit", _count_commit)
        await engine.dispose()


async def test_ingest_close_without_data_is_noop(session_factory) -> None:
    ingestor = TrackIngestor(session_factory)
    await ingestor.close()  # must not raise even though nothing was written


async def test_ingest_flushes_on_batch_age_even_below_batch_size(tmp_path) -> None:
    """A batch commits once ``max_batch_age_s`` elapses, not just at count.

    Regression for Issue #18/#20: with a count-only trigger, a handful of
    objects updating a few times a second can hold the write transaction
    open far longer than another writer's SQLite busy_timeout (e.g. the
    ACMI file import), which then fails with "database is locked" even
    though it never touches more than max_batch_size rows itself.
    """
    import asyncio

    db_path = (tmp_path / "batch_age.db").as_posix()
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    await init_db(engine)
    session_factory = create_session_factory(engine)

    # max_batch_size is high enough that only the age trigger can fire.
    ingestor = TrackIngestor(session_factory, max_batch_size=1000, max_batch_age_s=0.05)
    try:
        await ingestor.handle_line("FileType=text/acmi/tacview")
        await ingestor.handle_line("#0.00")
        await ingestor.handle_line("101,T=41.60|41.50|100")
        await asyncio.sleep(0.1)  # exceed max_batch_age_s
        # Any subsequent write re-checks the batch age and flushes it.
        await ingestor.handle_line("#1.00")
        await ingestor.handle_line("101,T=41.61||101")

        async with session_factory() as session:
            tracks = (await session.execute(select(Track))).scalars().all()
        # Both writes committed by the age trigger (checked on the second
        # write, since the trigger is only evaluated when a flush runs),
        # not by close() below.
        assert len(tracks) == 2
    finally:
        await ingestor.close()
        await engine.dispose()
