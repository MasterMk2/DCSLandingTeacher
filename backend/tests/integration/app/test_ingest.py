"""Tests for the ingest pipeline (parser events -> DB), incl. batching."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sqlalchemy import event, select, text

from app.ingest import TrackIngestor
from app.models import entities  # noqa: F401
from app.models.database import create_engine, create_session_factory
from app.models.entities import DcsObject, Flight, Track
from tests.helpers import (
    DECK_LATITUDE,
    DECK_LONGITUDE,
    NIMITZ_DECK_ALTITUDE_M,
    create_async_test_schema,
    make_deck_approach_samples,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


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
            (await session.execute(select(DcsObject).order_by(DcsObject.acmi_id))).scalars().all()
        )
        tracks = (await session.execute(select(Track).order_by(Track.mission_time))).scalars().all()

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
    await create_async_test_schema(engine)

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
                (await session.execute(select(Track).order_by(Track.mission_time))).scalars().all()
            )
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
    await create_async_test_schema(engine)
    session_factory = create_session_factory(engine)

    # max_batch_size is high enough that only the age trigger can fire.
    # The threshold starts beyond anything a test batch can reach and is
    # lowered to 0.05s only after the first write: on a loaded CI runner the
    # first handle_line's DB round trips alone can exceed 50 ms, which would
    # age-flush the opening batch (flight + first sample) before the second
    # write arrives and turn this into a 1-track assertion (seen 3x on
    # ubuntu-latest).
    ingestor = TrackIngestor(session_factory, max_batch_size=1000, max_batch_age_s=3600.0)
    try:
        await ingestor.handle_line("FileType=text/acmi/tacview")
        await ingestor.handle_line("#0.00")
        await ingestor.handle_line("101,T=41.60|41.50|100")
        ingestor._max_batch_age_s = 0.05
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
    await create_async_test_schema(engine)
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
    await create_async_test_schema(engine)

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


async def test_the_ingest_gate_lets_a_deck_touchdown_through(session_factory) -> None:
    """The gate has to ask the same question the analysis does.

    ``_maybe_detect_landing`` short-circuits before ``analyze_track`` unless
    the newest sample looks like a fresh ground contact. That cheap check was
    still judging weight-on-wheels against Tacview's sea-referenced AGL, so
    an aircraft settling on a deck never looked like a contact and the
    deck-aware pass behind it never ran. Every test in this module passed
    anyway, because they all call ``analyze_track`` directly -- so this one
    drives the real ingest path, line by line, exactly as the TCP stream does.
    """
    from app.ingest import LandingContext, TrackIngestor

    seen: list[LandingContext] = []

    async def listener(context: LandingContext) -> int | None:
        seen.append(context)
        return len(seen)

    ingestor = TrackIngestor(
        session_factory,
        landing_listener=listener,
        deck_altitude_for=lambda _c: NIMITZ_DECK_ALTITUDE_M,
    )

    lines = [
        "FileType=text/acmi/tacview",
        "FileVersion=2.2",
        "0,ReferenceTime=2024-01-01T00:00:00Z",
    ]
    lines.append("#0")
    lines.append(
        f"C1,T={DECK_LONGITUDE}|{DECK_LATITUDE}|0.0|||0.0,Type=Sea+Watercraft+AircraftCarrier,Name=CVN_73"
    )
    deck_approach = make_deck_approach_samples(final_altitude_m=NIMITZ_DECK_ALTITUDE_M)
    start_time = make_deck_approach_samples(final_altitude_m=0.0)[0].time
    for sample in deck_approach:
        lines.append(f"#{sample.time - start_time:.2f}")
        lines.append(
            f"A1,T={sample.longitude}|{sample.latitude}|{sample.altitude}"
            f"||||||{sample.speed:.1f}|,"
            f"Type=Air+FixedWing,Name=FA-18C_hornet,Pilot=Trap,AGL={sample.agl}"
        )
    for line in lines:
        await ingestor.handle_line(line)
    await ingestor.close()

    assert seen, "the gate swallowed the touchdown before analyze_track ever ran"
    assert seen[-1].event.kind == "carrier"
    assert seen[-1].event.touchdown.surface_is_deck is True
    # Identity must be burned in even on this path (it is what landings.pilot
    # and landings.airframe are for).
    assert seen[-1].airframe == "FA-18C_hornet"
    assert seen[-1].pilot == "Trap"


REF_LAT = 35.0
REF_LON = 140.0
DECK_ALT = 20.0


@dataclass
class Reported:
    """One landing listener call."""

    first_contact: float
    touchdown: float
    outcome: str


@dataclass
class Recorder:
    provisional: list[Reported] = field(default_factory=list)
    finalized: list[tuple[int, Reported]] = field(default_factory=list)
    next_id: int = 1

    async def on_landing(self, context) -> int | None:  # noqa: ANN001
        event = context.event
        self.provisional.append(
            Reported(
                first_contact=event.first_contact_time,
                touchdown=event.touchdown.time,
                outcome=event.outcome,
            )
        )
        row_id = self.next_id
        self.next_id += 1
        return row_id

    async def on_finalize(self, landing_id: int, context) -> None:  # noqa: ANN001
        event = context.event
        self.finalized.append(
            (
                landing_id,
                Reported(
                    first_contact=event.first_contact_time,
                    touchdown=event.touchdown.time,
                    outcome=event.outcome,
                ),
            )
        )


def _header(recording_time: str) -> list[str]:
    return [
        "FileType=text/acmi/tacview",
        "FileVersion=2.2",
        f"0,ReferenceTime=2026-06-11T04:30:00Z,DataSource=Test,RecordingTime={recording_time}",
    ]


def _landing_lines(
    obj_id: str,
    *,
    touchdown_time: float,
    recording_time: str | None = None,
    ground_roll_s: float = 20.0,
    include_header: bool = True,
) -> list[str]:
    """One aircraft flying a short approach and landing at ``touchdown_time``.

    Frames are 1 s: 10 s inbound (AGL 30 m -> 0, ~3 m/s sink), then a
    ``ground_roll_s`` ground roll with OnGround=1, then the aircraft goes
    silent (buffer frozen, like an aircraft that left the mission).
    ``recording_time`` is required whenever ``include_header`` is set.
    """
    lines: list[str] = []
    if include_header:
        assert recording_time is not None
        lines.extend(_header(recording_time))
    identity_sent = False
    for t in range(-10, int(ground_roll_s) + 1):
        absolute = touchdown_time + t
        lines.append(f"#{absolute:g}")
        if t < 0:
            agl = -t * 3.0
            altitude = DECK_ALT + agl
            on_ground = "0"
        else:
            agl = 0.0
            altitude = DECK_ALT
            on_ground = "1"
        # Straight-in from the south onto the origin.
        lat = REF_LAT - (abs(t) * 70.0) / 111320.0 if t < 0 else REF_LAT
        props = [f"T={REF_LON:g}|{lat:g}|{altitude:g}|||0"]
        if not identity_sent:
            identity_sent = True
            props.extend(["Type=Air+FixedWing", "Name=F/A-18C", "Pilot=Tester"])
        props.append(f"OnGround={on_ground}")
        props.append("TAS=70")
        lines.append(f"{obj_id},{','.join(props)}")
    return lines


async def _feed(ingestor: TrackIngestor, lines: list[str]) -> None:
    for line in lines:
        await ingestor.handle_line(line)


async def _flight_count(session_factory) -> int:  # noqa: ANN001
    from sqlalchemy import select

    async with session_factory() as session:
        flights = (await session.execute(select(Flight))).scalars().all()
    return len(flights)


async def test_mission_restart_rotates_session_and_stops_duplicates(
    session_factory,
) -> None:
    """The reported failure mode, end to end.

    Mission 1: obj 503 lands at t=30000 and goes silent (frozen buffer).
    Mission restart: the frame clock resets, the exporter re-sends the
    header with a new RecordingTime, and a NEW aircraft recycles the id
    503 and lands at t=500.

    The old landing must be reported exactly once, the new landing must
    be its own row with its own touchdown, and the two must never merge:
    pre-rotation the new touchdown event absorbed the stale old touchdown
    as a bounce, reporting t=30000 under the new mission's flight.
    """
    recorder = Recorder()
    ingestor = TrackIngestor(
        session_factory,
        landing_listener=recorder.on_landing,
        landing_finalize_listener=recorder.on_finalize,
    )

    await _feed(
        ingestor,
        _landing_lines("503", touchdown_time=30000.0, recording_time="2026-08-31T15:30:20Z"),
    )
    assert len(recorder.provisional) == 1
    assert recorder.provisional[0].touchdown == 30000.0

    # --- mission restart: new header, frame clock back to ~0, id reused ---
    await _feed(
        ingestor,
        _landing_lines("503", touchdown_time=500.0, recording_time="2026-09-01T00:30:20Z"),
    )
    await ingestor.close()

    # Exactly one flight per session.
    assert await _flight_count(session_factory) == 2

    touchdowns = sorted(r.touchdown for r in recorder.provisional)
    assert touchdowns == [500.0, 30000.0], recorder.provisional
    # The mission-2 event must describe the mission-2 touchdown only.
    second = next(r for r in recorder.provisional if r.touchdown == 500.0)
    assert second.first_contact == 500.0
    assert second.outcome == "full_stop"

    # Both landings finalized, once each.
    assert sorted(tid for tid, _ in recorder.finalized) == [1, 2]


async def test_frame_clock_reset_without_header_also_rotates(
    session_factory,
) -> None:
    """Exports that reset the clock without re-sending the header are
    caught by the mission-time regression trigger."""
    recorder = Recorder()
    ingestor = TrackIngestor(
        session_factory,
        landing_listener=recorder.on_landing,
        landing_finalize_listener=recorder.on_finalize,
    )

    await _feed(
        ingestor,
        _landing_lines("403", touchdown_time=30000.0, recording_time="2026-08-31T15:30:20Z"),
    )
    assert len(recorder.provisional) == 1
    # Same session signature (no new global object line), frame clock reset.
    await _feed(
        ingestor,
        _landing_lines("403", touchdown_time=500.0, include_header=False),
    )
    await ingestor.close()

    assert await _flight_count(session_factory) == 2
    touchdowns = sorted(r.touchdown for r in recorder.provisional)
    assert touchdowns == [500.0, 30000.0], recorder.provisional


async def test_small_time_jitter_does_not_rotate(session_factory) -> None:
    """Out-of-order frames jitter by seconds, not minutes; a jitter below
    the regression threshold must not split the session."""
    recorder = Recorder()
    ingestor = TrackIngestor(
        session_factory,
        landing_listener=recorder.on_landing,
        landing_finalize_listener=recorder.on_finalize,
    )
    await _feed(
        ingestor,
        _landing_lines("301", touchdown_time=1000.0, recording_time="2026-08-31T15:30:20Z"),
    )
    # A late frame lands 30 s in the past.
    jittered = [
        "#970.00",
        "301,T=140.0|35.0|20.0|||0,OnGround=1",
    ]
    await _feed(ingestor, jittered)
    await ingestor.close()

    assert await _flight_count(session_factory) == 1
    assert len(recorder.provisional) == 1


async def test_reconnect_with_same_session_signature_keeps_flight(
    session_factory,
) -> None:
    """A reconnect mid-mission re-sends the header with the SAME
    RecordingTime (same recording session): no rotation, no duplicate
    flight, and the already-reported landing is not reported again."""
    recorder = Recorder()
    ingestor = TrackIngestor(
        session_factory,
        landing_listener=recorder.on_landing,
        landing_finalize_listener=recorder.on_finalize,
    )
    recording_time = "2026-08-31T15:30:20Z"
    await _feed(
        ingestor,
        _landing_lines("503", touchdown_time=30000.0, recording_time=recording_time),
    )
    assert len(recorder.provisional) == 1

    # Reconnect: header re-sent unchanged, then the mission continues.
    await _feed(ingestor, _header(recording_time))
    tail = [
        "#30030.00",
        "503,T=140.0|35.0|20.0|||0,OnGround=1,TAS=5",
        "#30040.00",
        "503,T=140.0|35.0|20.0|||0,OnGround=1,TAS=5",
    ]
    await _feed(ingestor, tail)
    await ingestor.close()

    assert await _flight_count(session_factory) == 1
    assert len(recorder.provisional) == 1
