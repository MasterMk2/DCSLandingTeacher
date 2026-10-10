"""Synthetic live-stream regressions on legacy SQLite and migrated PostgreSQL."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.acmi.multi_source as multi_source
from app.acmi.models import ObjectUpdateEvent
from app.acmi.multi_source import MultiSourceAcmiManager, SourceContext
from app.acmi.stream import AcmiStreamClient
from app.config import Settings, TacviewSource
from app.grading.config import load_grading_config
from app.importer import ImportJobManager, import_source_id
from app.ingest import TrackIngestor
from app.models.database import create_engine, create_session_factory
from app.models.entities import DcsObject, Flight, Track
from app.pipeline import LandingPipeline
from tests.helpers import create_async_test_schema

RECORDING = (
    "FileType=text/acmi/tacview\nFileVersion=2.2\n"
    "0,ReferenceTime=2026-01-01T00:00:00Z\n"
    "0,RecordingTime=2026-01-01T01:00:00Z\n"
    "#0.00\n101,T=41.60|41.50|10000,Type=Air+FixedWing,Name=Synthetic\n"
)


@pytest.fixture(params=["sqlite", "postgres"])
async def stream_factory(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    url = (
        request.getfixturevalue("database_url")
        if request.param == "postgres"
        else f"sqlite+aiosqlite:///{(tmp_path / 'streams.db').as_posix()}"
    )
    engine = create_engine(url)
    if request.param == "sqlite":
        @event.listens_for(engine.sync_engine, "connect")
        def configure_sqlite(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=1000")
            cursor.close()

    await create_async_test_schema(engine)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


async def wait_until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.005)


class SyntheticWriter:
    def __init__(self, reader: asyncio.StreamReader) -> None:
        self.reader = reader
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.reader.feed_eof()

    async def wait_closed(self) -> None:
        pass


class SyntheticSources:
    """Use the actual client read/reconnect loop without external sockets."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        factory: async_sessionmaker[AsyncSession],
        *,
        interval: float = 0.05,
    ) -> None:
        self.manager = MultiSourceAcmiManager(factory, keep_all_tracks=True)
        self.connections: dict[str, asyncio.Queue[asyncio.StreamReader]] = {}
        self.attempts: dict[str, int] = {}
        self.writers: list[SyntheticWriter] = []
        self.handled: dict[str, asyncio.Event] = {}

        async def connect(client: AcmiStreamClient, _backoff: float):
            self.attempts[client.host] = self.attempts.get(client.host, 0) + 1
            reader = await self.connections[client.host].get()
            writer = SyntheticWriter(reader)
            self.writers.append(writer)
            return reader, cast(asyncio.StreamWriter, cast(object, writer))

        async def handshake(_client, _reader, _writer) -> None:
            pass

        def ingestor(*args, **kwargs):
            # Do not let slower PostgreSQL connection setup expire a batch
            # inside its first line: these tests exercise the client's timer.
            instance = TrackIngestor(*args, max_batch_age_s=60, **kwargs)
            handle_line = instance.handle_line
            source_id = kwargs["source_id"]

            async def tracked_line(line: str) -> None:
                await handle_line(line)
                if line.startswith("101,"):
                    self.handled[source_id].set()

            monkeypatch.setattr(instance, "handle_line", tracked_line)
            return instance

        monkeypatch.setattr(AcmiStreamClient, "_connect", connect)
        monkeypatch.setattr(AcmiStreamClient, "_handshake", handshake)
        monkeypatch.setattr(multi_source, "TrackIngestor", ingestor)
        monkeypatch.setattr(multi_source, "DEFAULT_BATCH_AGE_S", interval)
        self.monkeypatch = monkeypatch

    @asynccontextmanager
    async def running(self, *ids: str) -> AsyncIterator[SyntheticSources]:
        sources = [TacviewSource(id=id_, name=id_, host=id_, idle_timeout=0) for id_ in ids]
        settings = Settings(tacview_sources_json="[" + ",".join(
            source.model_dump_json() for source in sources
        ) + "]")
        self.monkeypatch.setattr(multi_source, "get_settings", lambda: settings)
        self.connections = {id_: asyncio.Queue() for id_ in ids}
        self.handled = {id_: asyncio.Event() for id_ in ids}
        await self.manager.start()
        try:
            yield self
        finally:
            await asyncio.wait_for(self.manager.stop(), 5)
        assert all(writer.closed for writer in self.writers)

    def context(self, id_: str) -> SourceContext:
        context = self.manager.get_source(id_)
        assert context is not None
        return context

    def attach(self, id_: str, payload: str = RECORDING) -> asyncio.StreamReader:
        reader = asyncio.StreamReader()
        reader.feed_data(payload.encode())
        self.connections[id_].put_nowait(reader)
        return reader

    async def received_object(self, id_: str) -> None:
        await asyncio.wait_for(self.handled[id_].wait(), 5)


async def independent_write(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        session.add(Flight(source_id="independent"))
        await asyncio.wait_for(session.commit(), 3)


async def tracks(factory: async_sessionmaker[AsyncSession]) -> list[tuple[str, float]]:
    async with factory() as session:
        return list((await session.execute(
            select(Flight.source_id, Track.mission_time)
            .join(Track, Track.flight_id == Flight.id)
            .order_by(Flight.source_id, Track.mission_time)
        )).tuples().all())


async def test_idle_source_releases_batch_without_another_object_update(
    stream_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = SyntheticSources(monkeypatch, stream_factory)
    async with sources.running("default"):
        sources.attach("default")
        await sources.received_object("default")
        await independent_write(stream_factory)
        await wait_until(lambda: sources.context("default").ingestor._session is None)
        assert sources.context("default").client.connected
        assert await tracks(stream_factory) == [("default", 0.0)]


async def test_eof_flushes_unterminated_tail_before_reconnect_backoff(
    stream_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = SyntheticSources(monkeypatch, stream_factory, interval=60)
    async with sources.running("default"):
        reader = sources.attach("default", RECORDING.rstrip("\n"))
        reader.feed_eof()
        await wait_until(lambda: bool(sources.writers) and sources.writers[0].closed)
        await independent_write(stream_factory)
        assert await tracks(stream_factory) == [("default", 0.0)]


async def test_sources_and_reconnect_keep_same_object_ids_separate(
    stream_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = SyntheticSources(monkeypatch, stream_factory)
    async with sources.running("first", "second"):
        first = sources.attach("first")
        await sources.received_object("first")
        second = sources.attach("second")
        await wait_until(lambda: sources.context("second").ingestor._flight_id is not None)
        first.feed_eof()
        second.feed_eof()
        await wait_until(lambda: sources.attempts.get("first", 0) >= 2)
        await independent_write(stream_factory)
        reconnect = sources.attach("first", RECORDING.replace("#0.00", "#1.00"))
        reconnect.feed_eof()
        await wait_until(lambda: sources.attempts.get("first", 0) >= 3)
        assert await tracks(stream_factory) == [("first", 0.0), ("first", 1.0), ("second", 0.0)]
        async with stream_factory() as session:
            objects = (await session.execute(select(DcsObject))).scalars().all()
        assert len(objects) == 2
        assert len({obj.flight_id for obj in objects}) == 2
        assert {obj.acmi_id for obj in objects} == {"101"}


async def test_import_completes_while_live_source_has_gone_idle(
    stream_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sources = SyntheticSources(monkeypatch, stream_factory)
    pipeline = LandingPipeline(stream_factory, load_grading_config())
    imports = ImportJobManager(stream_factory, pipeline, keep_all_tracks=True)
    path = tmp_path / "synthetic.acmi"
    path.write_text(RECORDING, encoding="utf-8")
    job = imports.create_job(path.name)
    async with sources.running("default"):
        sources.attach("default")
        await sources.received_object("default")
        await asyncio.wait_for(imports.run(job, path), 5)
        assert job.status == "completed", job.error
        assert not path.exists()
        assert await tracks(stream_factory) == [("default", 0.0), (import_source_id(job.id), 0.0)]
        assert await imports.discard(job.id)
        assert await tracks(stream_factory) == [("default", 0.0)]


async def test_stop_commits_finished_batch_and_releases_all_sources(
    stream_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = SyntheticSources(monkeypatch, stream_factory, interval=60)
    async with sources.running("default"):
        sources.attach("default")
        await sources.received_object("default")
    await independent_write(stream_factory)
    assert await tracks(stream_factory) == [("default", 0.0)]


async def test_discard_active_import_while_live_source_is_idle(
    stream_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sources = SyntheticSources(monkeypatch, stream_factory)
    imports = ImportJobManager(
        stream_factory, LandingPipeline(stream_factory, load_grading_config()),
        keep_all_tracks=True,
    )
    entered = asyncio.Event()
    handle_line = TrackIngestor.handle_line

    async def blocked_import(ingestor: TrackIngestor, line: str) -> None:
        await handle_line(ingestor, line)
        if ingestor._source_id.startswith("import:") and line.startswith("101,"):
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(TrackIngestor, "handle_line", blocked_import)
    path = tmp_path / "cancelled.acmi"
    path.write_text(RECORDING, encoding="utf-8")
    job = imports.create_job(path.name)
    async with sources.running("default"):
        sources.attach("default")
        await sources.received_object("default")
        task = asyncio.create_task(imports.run(job, path))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            discarded = await asyncio.wait_for(
                asyncio.gather(imports.discard(job.id), imports.discard(job.id)), 5,
            )
            assert discarded == [True, False]  # the second discard is already absent
            await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert not path.exists()
        assert imports.get_job(job.id) is None
        assert await tracks(stream_factory) == [("default", 0.0)]


async def test_frame_only_traffic_does_not_postpone_batch_release(
    stream_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = SyntheticSources(monkeypatch, stream_factory)
    async with sources.running("default"):
        reader = sources.attach("default")
        await sources.received_object("default")

        async def frames() -> None:
            frame = 1
            while True:
                reader.feed_data(f"#{frame}.00\n".encode())
                frame += 1
                await asyncio.sleep(0.01)

        producer = asyncio.create_task(frames())
        try:
            await independent_write(stream_factory)
            await wait_until(lambda: sources.context("default").ingestor._session is None)
            assert not producer.done()
            assert await tracks(stream_factory) == [("default", 0.0)]
        finally:
            producer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await producer


@pytest.mark.parametrize("disconnect", ["read-error", "socket-timeout", "idle-timeout"])
async def test_disconnect_releases_batch_before_any_new_input(
    stream_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch,
    disconnect: str,
) -> None:
    # A long flush interval proves teardown releases immediately, independently
    # of the periodic timer and of the next connection's RecordingTime.
    sources = SyntheticSources(monkeypatch, stream_factory, interval=60)
    async with sources.running("default"):
        if disconnect == "idle-timeout":
            sources.context("default").client._idle_timeout = 1
        reader = sources.attach("default")
        await sources.received_object("default")
        if disconnect == "read-error":
            reader.set_exception(ConnectionResetError("synthetic disconnect"))
        elif disconnect == "socket-timeout":
            reader.set_exception(TimeoutError(110, "synthetic socket timeout"))
        await wait_until(lambda: sources.writers[0].closed)
        await independent_write(stream_factory)
        assert await tracks(stream_factory) == [("default", 0.0)]


async def test_flush_waits_for_the_current_line_handler(
    stream_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = SyntheticSources(monkeypatch, stream_factory)
    entered, release = asyncio.Event(), asyncio.Event()
    async with sources.running("default"):
        ingestor = sources.context("default").ingestor
        handle_update = ingestor._handle_update

        async def slow_update(update: ObjectUpdateEvent) -> None:
            await handle_update(update)
            if update.obj_id == "101":
                entered.set()
                await release.wait()

        monkeypatch.setattr(ingestor, "_handle_update", slow_update)
        sources.attach("default")
        await asyncio.wait_for(entered.wait(), 5)
        session = ingestor._session
        assert session is not None
        try:
            await asyncio.sleep(0.1)  # longer than the test's batch lifetime
            assert ingestor._session is session
            assert session.in_transaction()
        finally:
            release.set()
        await wait_until(lambda: ingestor._session is None)
        assert await tracks(stream_factory) == [("default", 0.0)]


async def test_cancel_inside_line_handler_rolls_back_incomplete_batch(
    stream_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = SyntheticSources(monkeypatch, stream_factory, interval=60)
    entered = asyncio.Event()
    async with sources.running("default"):
        ingestor = sources.context("default").ingestor
        handle_update = ingestor._handle_update

        async def blocked_update(update: ObjectUpdateEvent) -> None:
            await handle_update(update)
            if update.obj_id == "101":
                entered.set()
                await asyncio.Event().wait()

        monkeypatch.setattr(ingestor, "_handle_update", blocked_update)
        sources.attach("default")
        await asyncio.wait_for(entered.wait(), 5)
    assert ingestor._session is None
    await independent_write(stream_factory)
    assert await tracks(stream_factory) == []
    async with stream_factory() as session:
        assert (await session.execute(select(DcsObject))).scalars().all() == []


async def test_cancel_before_commit_rolls_back_and_closes_detached_session(
    stream_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = SyntheticSources(monkeypatch, stream_factory, interval=60)
    entered = asyncio.Event()
    commit = AsyncSession.commit
    async with sources.running("default"):
        reader = sources.attach("default")
        await sources.received_object("default")
        owner = sources.context("default").ingestor._session
        assert owner is not None

        async def blocked_commit(session: AsyncSession) -> None:
            if session is owner:
                entered.set()
                await asyncio.Event().wait()
            await commit(session)

        monkeypatch.setattr(AsyncSession, "commit", blocked_commit)
        reader.feed_eof()
        await asyncio.wait_for(entered.wait(), 5)
    assert not owner.in_transaction()
    await independent_write(stream_factory)
    assert await tracks(stream_factory) == []


@pytest.mark.parametrize("cleanup_failure", ["rollback", "close"])
async def test_flush_cleanup_error_does_not_replace_cancellation(
    stream_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch,
    cleanup_failure: str,
) -> None:
    ingestor = TrackIngestor(stream_factory, keep_all_tracks=True)
    for line in RECORDING.splitlines():
        await ingestor.handle_line(line)
    owner = ingestor._session
    assert owner is not None
    entered = asyncio.Event()
    cleanup = getattr(AsyncSession, cleanup_failure)

    async def blocked_commit(session: AsyncSession) -> None:
        assert session is owner
        entered.set()
        await asyncio.Event().wait()

    async def failed_cleanup(session: AsyncSession) -> None:
        # Release real resources before simulating the driver's cleanup error.
        await cleanup(session)
        if session is owner:
            raise RuntimeError("synthetic cleanup failure")

    monkeypatch.setattr(AsyncSession, "commit", blocked_commit)
    monkeypatch.setattr(AsyncSession, cleanup_failure, failed_cleanup)
    task = asyncio.create_task(ingestor.flush())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ingestor._session is None
        assert not owner.in_transaction()
        assert await tracks(stream_factory) == []
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await ingestor.close()
