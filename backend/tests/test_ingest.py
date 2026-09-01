"""Tests for the ingest pipeline (parser events -> DB), incl. batching."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import event, select, text

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


async def test_ingest_derives_ground_speed_when_acmi_omits_speed_properties(
    session_factory,
) -> None:
    """DCS never emits TAS/CAS/IAS (Issue D-2); ingest must derive speed
    from consecutive positions instead of leaving it null."""
    from app.detection.geometry import haversine_m

    ingestor = TrackIngestor(session_factory)
    lines = [
        "FileType=text/acmi/tacview",
        "FileVersion=2.2",
        "0,ReferenceTime=2011-06-02T05:00:00Z",
        "#0.00",
        "301,T=0|0|1000|0|0|90,Type=Air+FixedWing,Name=F-16,Pilot=Test",
        "#10.00",
        "301,T=0|0.01|1000|0|0|90",
    ]
    for line in lines:
        await ingestor.handle_line(line)
    await ingestor.close()

    samples = ingestor._aircraft_buffers["301"].snapshot()  # noqa: SLF001
    assert len(samples) == 2
    # No prior sample to derive a speed from on the very first update.
    assert samples[0].speed is None
    expected_speed = haversine_m(0.0, 0.0, 0.01, 0.0) / 10.0
    assert samples[1].speed == pytest.approx(expected_speed, rel=1e-6)


async def test_ground_speed_ignores_duplicate_position_from_partial_updates(
    session_factory,
) -> None:
    """A partial update (no T=lon|lat) repeats the last known position

    verbatim per the ACMI spec ("omitted transform components keep their
    previous values"). Differentiating against the *immediately* preceding
    buffered sample would divide a near-zero distance by a near-zero dt,
    which live data showed reads as exactly 0 m/s on one pair and spikes
    into the thousands m/s on the next. The estimate must instead walk back
    to a sample at least GROUND_SPEED_MIN_BASELINE_S old.
    """
    from app.detection.geometry import haversine_m

    ingestor = TrackIngestor(session_factory)
    lines = [
        "FileType=text/acmi/tacview",
        "FileVersion=2.2",
        "0,ReferenceTime=2011-06-02T05:00:00Z",
        "#0.00",
        "301,T=0|0|1000|0|0|90,Type=Air+FixedWing,Name=F-16,Pilot=Test",
        "#0.10",
        "301,T=0.001|0|1000",  # real movement
        "#0.15",
        "301,AGL=950",  # partial update, no T= -> position repeats verbatim
        "#0.20",
        "301,AGL=940",  # another partial update, same repeated position
        "#1.20",
        "301,T=0.004|0|1000",  # real movement resumes
    ]
    for line in lines:
        await ingestor.handle_line(line)
    await ingestor.close()

    samples = ingestor._aircraft_buffers["301"].snapshot()  # noqa: SLF001
    assert len(samples) == 5
    # The two partial updates repeat the t=0.10 position verbatim.
    assert samples[2].longitude == pytest.approx(0.001)
    assert samples[3].longitude == pytest.approx(0.001)
    # Not enough baseline yet (< 1.0s since the first sample) -> unknown,
    # not a noisy guess.
    assert samples[1].speed is None
    assert samples[2].speed is None
    assert samples[3].speed is None
    # The final sample has exactly 1.0s of baseline against the last
    # distinct position (t=0.20, which repeats t=0.10's 0.001) -- not the
    # inflated/zeroed reading a naive immediately-previous-sample diff
    # would produce.
    last = samples[-1]
    expected = haversine_m(0.001, 0.0, 0.004, 0.0) / 1.0
    assert last.speed == pytest.approx(expected, rel=1e-6)


async def test_ground_speed_rejects_implausible_spike(session_factory) -> None:
    """Issue #27: a single large position jump over a ~1 s baseline would
    otherwise read as a supersonic spike (observed live: 1364 m/s). The
    implausible estimate must be discarded, not emitted."""
    ingestor = TrackIngestor(session_factory)
    lines = [
        "FileType=text/acmi/tacview",
        "FileVersion=2.2",
        "0,ReferenceTime=2011-06-02T05:00:00Z",
        "#0.00",
        "301,T=0|0|1000|0|0|90,Type=Air+FixedWing,Name=F-16,Pilot=Test",
        "#1.00",
        "301,T=0.02|0|1000",  # ~2226 m jump in 1 s -> far above plausible
    ]
    for line in lines:
        await ingestor.handle_line(line)
    await ingestor.close()

    samples = ingestor._aircraft_buffers["301"].snapshot()  # noqa: SLF001
    # ~2226 m/s exceeds GROUND_SPEED_MAX_PLAUSIBLE_MS -> dropped, not a spike.
    assert samples[-1].speed is None


async def test_ground_speed_rejects_stale_baseline(session_factory) -> None:
    """Issue #27: a baseline older than GROUND_SPEED_MAX_BASELINE_S is too
    stale to trust; the estimate must be dropped rather than derived from
    minutes-old positioning."""
    ingestor = TrackIngestor(session_factory)
    lines = [
        "FileType=text/acmi/tacview",
        "FileVersion=2.2",
        "0,ReferenceTime=2011-06-02T05:00:00Z",
        "#0.00",
        "301,T=0|0|1000|0|0|90,Type=Air+FixedWing,Name=F-16,Pilot=Test",
        "#20.00",
        "301,T=0.001|0|1000",  # only candidate baseline is 20 s old
    ]
    for line in lines:
        await ingestor.handle_line(line)
    await ingestor.close()

    samples = ingestor._aircraft_buffers["301"].snapshot()  # noqa: SLF001
    assert samples[-1].speed is None


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
        # With session-per-batch: 2 commits (batch of 2 + final batch of 1 on close).
        assert commits_before_close == 2
        assert commit_count["n"] == 2
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

        # A *second* batch on the same (reused) session must also stamp its
        # own opening time. Regression: _get_session() only recorded
        # _batch_opened_at when creating a brand new AsyncSession, but the
        # session survives a commit (only the transaction ends), so every
        # batch after the first one was falling back to `close()` alone --
        # exactly what reproduced "database is locked" against a concurrent
        # writer again, even with the age trigger deployed.
        await ingestor.handle_line("#2.00")
        await ingestor.handle_line("101,T=41.62||102")
        await asyncio.sleep(0.1)
        await ingestor.handle_line("#3.00")
        await ingestor.handle_line("101,T=41.63||103")

        async with session_factory() as session:
            tracks = (await session.execute(select(Track))).scalars().all()
        assert len(tracks) == 4
    finally:
        await ingestor.close()
        await engine.dispose()


async def test_ingest_holds_no_write_transaction_between_commits(tmp_path) -> None:
    """定常状態の更新でバッチ中に書き込みロックを取らないこと。

    毎更新で走る `session.get()` が autoflush を誘発して Track の INSERT を
    先に飛ばしてしまい、SQLite の唯一の書き込みトランザクションをバッチ期間
    (実サーバ実測 3.0 秒、空き窓 0.17 秒) ずっと保持していた。その間 import と
    /regrade は "database is locked" で落ちる。INSERT は commit 時にまとめて
    出るのが正しい。

    新規オブジェクトの作成だけは id が外部キーに要るので明示 flush する。
    ここで検証するのは、以後の更新がロックを取らないこと。
    """
    from sqlalchemy.orm import Session  # noqa: F401

    db_path = (tmp_path / "lock.db").as_posix()
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    await init_db(engine)
    session_factory = create_session_factory(engine)

    inserts = {"n": 0}

    def _count(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if statement.lstrip().upper().startswith("INSERT INTO TRACKS"):
            inserts["n"] += 1

    event.listen(engine.sync_engine, "before_cursor_execute", _count)
    ingestor = TrackIngestor(session_factory, max_batch_size=1000)
    try:
        # オブジェクトを登録しきる (ここでの明示 flush は許容)。
        await ingestor.handle_line("FileType=text/acmi/tacview")
        await ingestor.handle_line("#0.00")
        await ingestor.handle_line("101,T=41.60|41.50|100,Type=Air+FixedWing,Name=F-16")
        await ingestor.close()
        inserts["n"] = 0

        # 以降は既知オブジェクトの更新のみ = 定常状態。
        ingestor = TrackIngestor(session_factory, max_batch_size=1000)
        for i in range(1, 40):
            await ingestor.handle_line(f"#{i}.00")
            await ingestor.handle_line(f"101,T=41.6{i:02d}||{100 + i}")

        assert inserts["n"] == 0, "バッチ中に書き込みトランザクションを開いている"

        await ingestor.close()
        assert inserts["n"] > 0  # commit でまとめて出る

        async with session_factory() as session:
            rows = (await session.execute(select(Track))).scalars().all()
        assert len(rows) == 40
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _count)
        await engine.dispose()


async def test_a_failed_write_does_not_wedge_the_ingestor(tmp_path) -> None:
    """A write failure has to discard the batch, not keep it.

    The session.flush() calls that fetch row ids for foreign keys raise before
    _flush() is ever reached, and SQLAlchemy deactivates the transaction on a
    failed flush. Keeping that session makes every later line -- and close()
    itself -- raise PendingRollbackError, and the live source's supervisor
    just calls run() again on the same ingestor, so one "database is locked"
    would wedge ingestion into a permanent reconnect loop storing nothing.
    """
    db_path = (tmp_path / "wedge.db").as_posix()
    url = f"sqlite+aiosqlite:///{db_path}"

    engine = create_engine(url)
    await init_db(engine)
    # Fail immediately instead of waiting out the 5 s busy timeout.
    @event.listens_for(engine.sync_engine, "connect")
    def _no_wait(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=0")
        cursor.close()

    session_factory = create_session_factory(engine)
    ingestor = TrackIngestor(session_factory)
    lines = (FIXTURES / "sample.acmi").read_text(encoding="utf-8").splitlines()

    # Hold SQLite's single write lock from an unrelated connection: an
    # uncommitted write to a scratch table keeps it until we roll back.
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE _lock_probe (x INTEGER)"))
    blocker_engine = create_engine(url)
    blocker = await blocker_engine.connect()
    await blocker.execute(text("INSERT INTO _lock_probe VALUES (1)"))

    with pytest.raises(Exception):
        for line in lines:
            await ingestor.handle_line(line)

    await blocker.rollback()
    await blocker.close()
    await blocker_engine.dispose()

    # The ingestor has to be usable again once the lock is free.
    for line in lines:
        await ingestor.handle_line(line)
    await ingestor.close()

    async with session_factory() as session:
        flights = (await session.execute(select(Flight))).scalars().all()
        tracks = (await session.execute(select(Track))).scalars().all()
    assert len(flights) >= 1
    assert len(tracks) > 0
    await engine.dispose()


async def test_live_ingestor_windows_carrier_history(session_factory) -> None:
    """The retention window must actually be wired through from the ingestor.

    Aircraft were already windowed by RollingTrackBuffer; carriers kept every
    sample for the life of the process until this was passed down.
    """
    ingestor = TrackIngestor(session_factory, sample_buffer_s=30.0)
    for line in (
        "FileType=text/acmi/tacview",
        "0,ReferenceTime=2011-06-02T05:00:00Z",
    ):
        await ingestor.handle_line(line)

    for i in range(600):  # 300s at 2Hz, ten times the window
        t = i / 2.0
        await ingestor.handle_line(f"#{t:.2f}")
        await ingestor.handle_line(
            f"102,T=41.62|{41.58 + i * 1e-5:.5f}|0|0|0|85,"
            "Type=Sea+Watercraft+AircraftCarrier,Name=Kuznetsov"
        )
    await ingestor.close()

    samples = ingestor.carrier_states["102"].samples
    assert samples, "the window must never empty the series"
    assert samples[-1][0] - samples[0][0] <= 30.0


async def test_position_jump_guard_rejects_garbage_coordinates(session_factory) -> None:
    """Samples separated by impossible implied speed have lat/lon nulled."""
    ingestor = TrackIngestor(session_factory)
    from app.detection.detector import TrackSample

    base = dict(altitude=20.0, agl=0.0, speed=0.0, heading=0.0, on_ground=True)
    # Two positions ~780 km apart in 0.14 s = ~5 600 000 m/s >> 1000
    await ingestor.handle_line("FileType=text/acmi/tacview")
    await ingestor.handle_line("#0.00")
    ingestor.record_aircraft_sample(
        "T1", TrackSample(time=100.0, latitude=42.99, longitude=44.77, **base)
    )
    ingestor.record_aircraft_sample(
        "T1", TrackSample(time=100.14, latitude=46.57, longitude=36.57, **base)
    )
    samples = ingestor._aircraft_buffers["T1"].snapshot()
    assert samples[0].latitude == pytest.approx(42.99)
    assert samples[1].latitude is None and samples[1].longitude is None
    assert samples[1].altitude == 20.0  # non-position data preserved
    await ingestor.close()


async def test_position_jump_guard_allows_normal_movement(session_factory) -> None:
    """Normal aircraft speed passes through the guard untouched."""
    ingestor = TrackIngestor(session_factory)
    from app.detection.detector import TrackSample

    base = dict(altitude=1000.0, agl=900.0, speed=80.0, heading=90.0, on_ground=False)
    await ingestor.handle_line("FileType=text/acmi/tacview")
    await ingestor.handle_line("#0.00")
    # ~14 m in 0.2 s = 70 m/s — within guard
    ingestor.record_aircraft_sample(
        "T2", TrackSample(time=100.0, latitude=42.24000, longitude=42.04000, **base)
    )
    ingestor.record_aircraft_sample(
        "T2", TrackSample(time=100.2, latitude=42.24010, longitude=42.04010, **base)
    )
    samples = ingestor._aircraft_buffers["T2"].snapshot()
    assert samples[0].latitude == pytest.approx(42.24000)
    assert samples[1].latitude == pytest.approx(42.24010)
    assert samples[1].longitude == pytest.approx(42.04010)
    await ingestor.close()


async def test_position_jump_guard_accepts_respawn_after_one_rejection(
    session_factory,
) -> None:
    """A respawn teleport costs one rejected sample then resumes normally."""
    ingestor = TrackIngestor(session_factory)
    from app.detection.detector import TrackSample

    base = dict(altitude=100.0, agl=0.0, speed=0.0, heading=0.0, on_ground=True)
    await ingestor.handle_line("FileType=text/acmi/tacview")
    await ingestor.handle_line("#0.00")
    ingestor.record_aircraft_sample(
        "T3", TrackSample(time=100.0, latitude=42.24, longitude=42.04, **base)
    )
    # teleport ~500 km in 0.1 s
    ingestor.record_aircraft_sample(
        "T3", TrackSample(time=100.1, latitude=46.0, longitude=37.0, **base)
    )
    # normal movement from respawned position
    ingestor.record_aircraft_sample(
        "T3", TrackSample(time=100.3, latitude=46.01, longitude=37.01, **base)
    )
    samples = ingestor._aircraft_buffers["T3"].snapshot()
    assert samples[0].latitude == pytest.approx(42.24)
    assert samples[1].latitude is None  # rejected
    assert samples[2].latitude == pytest.approx(46.01)  # baseline updated
    await ingestor.close()
