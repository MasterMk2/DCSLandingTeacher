"""Alembic migration tests (Issue #7).

Covers the three startup paths:

- empty database -> ``upgrade head`` creates the full schema,
- legacy ``create_all`` database (no ``alembic_version``) -> stamped at the
  baseline revision, then migrated forward with data preserved,
- repeated runs are idempotent.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from sqlalchemy import create_engine, inspect, text

from app.models.migrations import (
    BASELINE_REVISION,
    HEAD_REVISION,
    make_alembic_config,
    run_migrations,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
GRADING_YAML = REPO_ROOT / "config" / "grading.yaml"


def _engine(url: str):
    return create_engine(url)


def _table_names(url: str) -> set[str]:
    engine = _engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _columns(url: str, table: str) -> set[str]:
    engine = _engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


def _version(url: str) -> str | None:
    engine = _engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        engine.dispose()


async def test_empty_db_upgrades_to_head(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'fresh.db').as_posix()}"
    await run_migrations(url)

    tables = _table_names(url)
    assert {"flights", "objects", "tracks", "landings", "alembic_version"} <= tables
    assert "outcome_status" in _columns(url, "landings")
    assert _version(url) == HEAD_REVISION


async def test_legacy_db_is_stamped_then_upgraded(tmp_path: Path) -> None:
    """A pre-Alembic database gains the new column without losing data."""
    url = f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}"

    # Simulate an old installation: baseline schema only, no alembic_version.
    cfg = make_alembic_config(url)
    command.upgrade(cfg, BASELINE_REVISION)
    assert "outcome_status" not in _columns(url, "landings")

    # Seed one landing row the way the old app would have.
    engine = _engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO flights (id, started_at, created_at) "
                    "VALUES (1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO objects (id, flight_id, acmi_id, first_seen, "
                    "last_seen, removed) "
                    "VALUES (1, 1, '101', 0.0, 100.0, 0)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO landings (id, flight_id, object_id, outcome, "
                    "created_at) "
                    "VALUES (1, 1, 1, 'full_stop', CURRENT_TIMESTAMP)"
                )
            )
    finally:
        engine.dispose()

    await run_migrations(url)

    assert "outcome_status" in _columns(url, "landings")
    assert _version(url) == HEAD_REVISION

    engine = _engine(url)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT outcome, outcome_status FROM landings WHERE id = 1")
            ).one()
    finally:
        engine.dispose()
    assert row.outcome == "full_stop"
    assert row.outcome_status == "final"


async def test_repeated_runs_are_idempotent(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'idem.db').as_posix()}"
    await run_migrations(url)
    await run_migrations(url)
    assert _version(url) == HEAD_REVISION


async def test_app_lifespan_applies_migrations(tmp_path: Path) -> None:
    """The default settings path migrates instead of create_all."""
    from app.api.main import create_app
    from app.config import Settings

    db_path = (tmp_path / "app.db").as_posix()
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{db_path}",
        acmi_enabled=False,
        grading_config_path=str(GRADING_YAML),
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        url = f"sqlite:///{db_path}"
        assert "landings" in _table_names(url)
        assert _version(url) == HEAD_REVISION


async def test_downgrade_to_baseline_drops_added_columns(tmp_path: Path) -> None:
    """Every migration has a working downgrade path (Issue #24).

    Upgrading to head then downgrading back to the baseline must remove the
    columns added by 0002..0004 and leave the baseline schema intact, so a
    failed production deploy can be rolled back.
    """
    url = f"sqlite:///{(tmp_path / 'rollback.db').as_posix()}"
    cfg = make_alembic_config(url)

    command.upgrade(cfg, "head")
    assert "outcome_status" in _columns(url, "landings")
    assert "source_id" in _columns(url, "flights")
    assert "approach_pattern" in _columns(url, "landings")

    command.downgrade(cfg, BASELINE_REVISION)

    assert "outcome_status" not in _columns(url, "landings")
    assert "source_id" not in _columns(url, "flights")
    assert "source_id" not in _columns(url, "landings")
    assert "approach_pattern" not in _columns(url, "landings")
    # Baseline schema survives.
    assert _table_names(url) >= {"flights", "objects", "tracks", "landings"}
    # The import_jobs table added by 0006 is gone after full downgrade.
    assert "import_jobs" not in _table_names(url)
    assert _version(url) == BASELINE_REVISION


async def test_0008_backfills_the_airframe_from_the_approach_track(
    tmp_path: Path,
) -> None:
    """The backfill is the whole point of 0008, and nothing exercised it.

    A landing's aircraft used to be read from the mutable ``objects`` row,
    which Tacview lets a later object overwrite; 124 production landings
    displayed an airframe disagreeing with the one in their own
    ``approach_track``. The migration repairs them from that track, so the
    repair itself has to be pinned -- including the rows it must NOT invent
    a value for.
    """
    import json

    url = f"sqlite:///{(tmp_path / 'backfill.db').as_posix()}"
    # Only up to 0008: from 0009 on the tracks are no longer in ``landings``.
    command.upgrade(make_alembic_config(url), "0008_landing_identity")

    engine = _engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM landings"))
            conn.execute(
                text(
                    "INSERT INTO flights (id, source_id, started_at, created_at) "
                    "VALUES (1, 'default', '2024-01-01', '2024-01-01')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO objects (id, flight_id, acmi_id, type, name, "
                    "first_seen, last_seen, removed) VALUES "
                    "(1, 1, '1001', 'Air+Rotorcraft', 'AIM_120', 0.0, 1.0, 0)"
                )
            )
            for landing_id, track in (
                (1, json.dumps({"airframe": "UH-1H", "samples": []})),
                (2, json.dumps({"samples": []})),  # no airframe recorded
                (3, None),                          # no track at all
            ):
                conn.execute(
                    text(
                        "INSERT INTO landings (id, flight_id, object_id, source_id, "
                        "kind, outcome, touchdown_time, approach_track, created_at) "
                        "VALUES (:i, 1, 1, 'default', 'land', 'full_stop', 0.0, :t, "
                        "'2024-01-01')"
                    ),
                    {"i": landing_id, "t": track},
                )
            # Roll back to before 0008 and forward again, so the backfill runs
            # over these rows.
            conn.execute(
                text("UPDATE alembic_version SET version_num = '0007_import_jobs'")
            )
            conn.execute(text("ALTER TABLE landings DROP COLUMN pilot"))
            conn.execute(text("ALTER TABLE landings DROP COLUMN airframe"))
    finally:
        engine.dispose()

    await run_migrations(url)

    engine = _engine(url)
    try:
        with engine.connect() as conn:
            rows = dict(
                conn.execute(text("SELECT id, airframe FROM landings")).all()
            )
    finally:
        engine.dispose()

    assert rows[1] == "UH-1H", "a recorded airframe must be restored"
    assert rows[2] is None, "no airframe in the track -> nothing to invent"
    assert rows[3] is None, "no track -> nothing to invent"
    assert {"pilot", "airframe"} <= _columns(url, "landings")


async def test_0008_survives_columns_that_already_exist(tmp_path: Path) -> None:
    """SQLite commits the ADD COLUMNs independently of the version stamp, so
    an interrupted run leaves the columns behind. Re-running must not die on
    "duplicate column name"."""
    url = f"sqlite:///{(tmp_path / 'partial.db').as_posix()}"
    command.upgrade(make_alembic_config(url), "0008_landing_identity")

    engine = _engine(url)
    try:
        with engine.begin() as conn:
            # The half-applied state: columns present, version rolled back.
            conn.execute(
                text("UPDATE alembic_version SET version_num = '0007_import_jobs'")
            )
    finally:
        engine.dispose()

    await run_migrations(url)
    assert _version(url) == HEAD_REVISION
    assert {"pilot", "airframe"} <= _columns(url, "landings")


def _landings_at_0008(url: str, tracks: dict[int, str | None]) -> None:
    """A database at 0008 whose landings carry these approach-track texts."""
    command.upgrade(make_alembic_config(url), "0008_landing_identity")
    engine = _engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO flights (id, source_id, started_at, created_at) "
                    "VALUES (1, 'default', '2024-01-01', '2024-01-01')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO objects (id, flight_id, acmi_id, type, name, "
                    "first_seen, last_seen, removed) VALUES "
                    "(1, 1, '1001', 'Air+FixedWing', 'F-16C', 0.0, 1.0, 0)"
                )
            )
            for landing_id, track in tracks.items():
                conn.execute(
                    text(
                        "INSERT INTO landings (id, flight_id, object_id, source_id, "
                        "kind, outcome, touchdown_time, approach_track, created_at) "
                        "VALUES (:i, 1, 1, 'default', 'land', 'full_stop', 0.0, :t, "
                        "'2024-01-01')"
                    ),
                    {"i": landing_id, "t": track},
                )
    finally:
        engine.dispose()


def _stored_tracks(url: str) -> dict[int, str | None]:
    import zlib

    engine = _engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT landing_id, approach_track FROM landing_tracks"))
            return {
                landing_id: None if blob is None else zlib.decompress(blob).decode("utf-8")
                for landing_id, blob in rows
            }
    finally:
        engine.dispose()


async def test_0009_moves_the_tracks_out_of_landings_unchanged(tmp_path: Path) -> None:
    import json

    url = f"sqlite:///{(tmp_path / 'tracks.db').as_posix()}"
    tracks = {
        1: json.dumps({"airframe": "F-16C", "samples": [{"time": 1.5, "agl": 12.25}]}),
        2: json.dumps({"samples": []}),
        3: None,
    }
    _landings_at_0008(url, tracks)

    await run_migrations(url)

    assert _version(url) == HEAD_REVISION
    assert "approach_track" not in _columns(url, "landings")
    # Byte for byte the JSON that was there; no row for a landing without one.
    assert _stored_tracks(url) == {1: tracks[1], 2: tracks[2]}

    # And the model reads them back through Landing.approach_track.
    from sqlalchemy import select

    from app.models.database import create_engine as create_async_engine
    from app.models.database import create_session_factory
    from app.models.entities import Landing

    engine = create_async_engine(url.replace("sqlite:", "sqlite+aiosqlite:", 1))
    try:
        async with create_session_factory(engine)() as session:
            rows = (await session.execute(select(Landing).order_by(Landing.id))).scalars().all()
            assert [row.approach_track for row in rows] == [
                json.loads(tracks[1]),
                json.loads(tracks[2]),
                None,
            ]
    finally:
        await engine.dispose()


async def test_0009_finishes_a_run_that_was_cut_short(tmp_path: Path) -> None:
    """A deploy that stops waiting for the health check can kill the copy
    half way; SQLite commits the table on its own. Re-running copies what is
    left, once, and drops the column."""
    url = f"sqlite:///{(tmp_path / 'cut.db').as_posix()}"
    tracks = {i: f'{{"samples": [], "n": {i}}}' for i in range(1, 251)}
    _landings_at_0008(url, tracks)
    command.upgrade(make_alembic_config(url), "0009_landing_tracks")
    engine = _engine(url)
    try:
        with engine.begin() as conn:
            # Back to the state after the table and the first rows: the column
            # is back with its data, a third of the rows are copied, version 0008.
            conn.execute(text("ALTER TABLE landings ADD COLUMN approach_track JSON"))
            for landing_id, track in tracks.items():
                conn.execute(
                    text("UPDATE landings SET approach_track = :t WHERE id = :i"),
                    {"i": landing_id, "t": track},
                )
            conn.execute(text("DELETE FROM landing_tracks WHERE landing_id > 80"))
            conn.execute(
                text("UPDATE alembic_version SET version_num = '0008_landing_identity'")
            )
    finally:
        engine.dispose()

    await run_migrations(url)

    assert _version(url) == HEAD_REVISION
    assert "approach_track" not in _columns(url, "landings")
    assert _stored_tracks(url) == tracks


async def test_0009_keeps_the_batches_it_copied_when_it_dies(tmp_path: Path, monkeypatch) -> None:
    """Killed in the middle of the copy, the batches already copied stay
    copied, and the next run carries on. (Inside alembic's transaction they
    all rolled back with the failure, so each retry started from zero.)"""
    import zlib

    url = f"sqlite:///{(tmp_path / 'killed.db').as_posix()}"
    tracks = {i: f'{{"samples": [], "n": {i}}}' for i in range(1, 351)}
    _landings_at_0008(url, tracks)
    real_compress = zlib.compress
    calls = {"n": 0}

    def dies_at_250(data, level=-1):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 250:
            raise RuntimeError("killed")
        return real_compress(data, level)

    monkeypatch.setattr(zlib, "compress", dies_at_250)
    try:
        await run_migrations(url)
    except RuntimeError:
        pass
    monkeypatch.setattr(zlib, "compress", real_compress)

    assert _version(url) == "0008_landing_identity"
    assert len(_stored_tracks(url)) == 200  # two batches of 100, committed
    assert "approach_track" in _columns(url, "landings")

    await run_migrations(url)

    assert _version(url) == HEAD_REVISION
    assert _stored_tracks(url) == tracks


async def test_0009_downgrades_back_into_landings(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'down.db').as_posix()}"
    tracks = {1: '{"samples": [1, 2, 3]}', 2: None}
    _landings_at_0008(url, tracks)
    await run_migrations(url)

    command.downgrade(make_alembic_config(url), "0008_landing_identity")

    assert "landing_tracks" not in _table_names(url)
    engine = _engine(url)
    try:
        with engine.connect() as conn:
            rows = dict(conn.execute(text("SELECT id, approach_track FROM landings")).all())
    finally:
        engine.dispose()
    assert rows == tracks


async def test_a_deleted_landing_takes_its_track_along(tmp_path: Path) -> None:
    """Foreign keys are not enforced here and the import cleanup deletes
    landings in bulk. SQLite reuses a deleted landing's id, so a track left
    behind would make the next landing's insert fail on its primary key."""
    url = f"sqlite:///{(tmp_path / 'delete.db').as_posix()}"
    _landings_at_0008(url, {1: '{"samples": []}'})
    await run_migrations(url)
    engine = _engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM landings"))
            remaining = conn.execute(text("SELECT COUNT(*) FROM landing_tracks")).scalar_one()
    finally:
        engine.dispose()
    assert remaining == 0
