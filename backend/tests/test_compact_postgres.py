"""PostgreSQL track compaction keeps the data needed to rebuild landings."""

from __future__ import annotations

import io
import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url

from app.compact import CompactError, _detection_config, compact
import app.compact as compact_module
from app.retention import retention_window

GRADING_YAML = Path(__file__).resolve().parents[2] / 'config' / 'grading.yaml'


@pytest.fixture
def postgres_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    test_url = os.getenv('DLT_TEST_POSTGRES_URL')
    if not test_url:
        pytest.skip('DLT_TEST_POSTGRES_URL is required for PostgreSQL integration tests')
    monkeypatch.setenv('DLT_GRADING_CONFIG_PATH', str(GRADING_YAML))

    schema = f'dlt_compact_{uuid4().hex}'
    admin = create_engine(test_url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped_url = make_url(test_url).update_query_dict({'options': f'-csearch_path={schema}'})
    engine = create_engine(scoped_url)
    try:
        with engine.begin() as connection:
            connection.execute(text('CREATE TABLE alembic_version (version_num text PRIMARY KEY)'))
            connection.execute(
                text("INSERT INTO alembic_version VALUES ('0009_landing_tracks')")
            )
            connection.execute(text('CREATE TABLE flights (id bigint PRIMARY KEY)'))
            connection.execute(
                text(
                    'CREATE TABLE objects (id bigint PRIMARY KEY, flight_id bigint NOT NULL '
                    'REFERENCES flights(id), type text)'
                )
            )
            connection.execute(
                text(
                    'CREATE TABLE landings (id bigint PRIMARY KEY, object_id bigint NOT NULL '
                    'REFERENCES objects(id), carrier_object_id bigint REFERENCES objects(id), '
                    'touchdown_time double precision)'
                )
            )
            connection.execute(
                text(
                    'CREATE TABLE tracks (id bigint PRIMARY KEY, flight_id bigint NOT NULL '
                    'REFERENCES flights(id), object_id bigint NOT NULL REFERENCES objects(id), '
                    'mission_time double precision NOT NULL)'
                )
            )
        yield scoped_url.render_as_string(hide_password=False)
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _seed_tracks(database_url: str) -> tuple[set[int], int]:
    start, end = retention_window(1000.0, 1000.0, _detection_config())
    engine = create_engine(database_url)
    kept_ids: set[int] = set()
    next_id = 1
    with engine.begin() as connection:
        connection.execute(text('INSERT INTO flights (id) VALUES (1)'))
        connection.execute(
            text(
                'INSERT INTO objects (id, flight_id, type) VALUES '
                "(1, 1, 'Air+FixedWing'), (2, 1, 'Sea+Watercraft+AircraftCarrier'), "
                "(3, 1, 'Ground+Static'), (4, 1, 'Weapon+Missile'), "
                "(5, 1, 'Air+FixedWing')"
            )
        )
        connection.execute(
            text(
                'INSERT INTO landings (id, object_id, carrier_object_id, touchdown_time) '
                'VALUES (1, 1, 2, 1000.0)'
            )
        )
        for object_id, times in (
            (1, (start - 1, start, 1000.0, end, end + 1)),
            (2, (start - 1, start, 1000.0, end, end + 1)),
            (3, (start - 1000, end + 1000)),
            (4, (1000.0,)),
            (5, (1000.0,)),
        ):
            for mission_time in times:
                connection.execute(
                    text(
                        'INSERT INTO tracks (id, flight_id, object_id, mission_time) '
                        'VALUES (:id, 1, :object_id, :mission_time)'
                    ),
                    {'id': next_id, 'object_id': object_id, 'mission_time': mission_time},
                )
                if object_id == 3 or (object_id in (1, 2) and start <= mission_time <= end):
                    kept_ids.add(next_id)
                next_id += 1
    engine.dispose()
    return kept_ids, next_id - 1


def _track_ids(database_url: str) -> set[int]:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return set(connection.execute(text('SELECT id FROM tracks')).scalars())
    finally:
        engine.dispose()


def test_dry_run_counts_retained_tracks_without_deleting(postgres_url: str) -> None:
    kept, total = _seed_tracks(postgres_url)
    output = io.StringIO()

    report = compact(postgres_url, out=output)

    assert report.before == total
    assert report.keep == len(kept)
    assert report.delete == total - len(kept)
    assert report.after is None
    assert len(_track_ids(postgres_url)) == total
    assert 'dry run' in output.getvalue()


def test_missing_grading_configuration_prevents_execution(
    postgres_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_tracks(postgres_url)
    original = _track_ids(postgres_url)
    monkeypatch.setenv('DLT_GRADING_CONFIG_PATH', str(tmp_path / 'missing.yaml'))

    with pytest.raises(CompactError, match='configuration is missing'):
        compact(
            postgres_url, execute=True, backup_confirmed=True,
            application_stopped=True, out=io.StringIO(),
        )

    assert _track_ids(postgres_url) == original


def test_execute_requires_backup_and_stopped_application(postgres_url: str) -> None:
    _seed_tracks(postgres_url)
    before = _track_ids(postgres_url)

    with pytest.raises(CompactError, match='verified backup'):
        compact(postgres_url, execute=True)
    with pytest.raises(CompactError, match='verified backup'):
        compact(postgres_url, execute=True, backup_confirmed=True)
    with pytest.raises(CompactError, match='max_delete'):
        compact(
            postgres_url,
            execute=True,
            backup_confirmed=True,
            application_stopped=True,
            max_delete=0,
            out=io.StringIO(),
        )
    assert _track_ids(postgres_url) == before


def test_execute_keeps_landing_windows_and_every_static_track(postgres_url: str) -> None:
    kept, total = _seed_tracks(postgres_url)

    report = compact(
        postgres_url,
        execute=True,
        backup_confirmed=True,
        application_stopped=True,
        out=io.StringIO(),
    )

    assert report.before == total
    assert report.after == len(kept)
    assert report.delete == total - len(kept)
    assert _track_ids(postgres_url) == kept
    engine = create_engine(postgres_url)
    try:
        with engine.connect() as connection:
            assert connection.execute(text('SELECT COUNT(*) FROM landings')).scalar_one() == 1
            assert connection.execute(text('SELECT COUNT(*) FROM objects')).scalar_one() == 5
    finally:
        engine.dispose()


def test_failed_post_delete_verification_rolls_back(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_tracks(postgres_url)
    original = _track_ids(postgres_url)
    checks = iter((0, 1))
    monkeypatch.setattr(compact_module, '_orphan_count', lambda _: next(checks))

    with pytest.raises(CompactError, match='post-delete'):
        compact(
            postgres_url, execute=True, backup_confirmed=True,
            application_stopped=True, out=io.StringIO(),
        )

    assert _track_ids(postgres_url) == original


def test_active_writer_prevents_deletion(postgres_url: str) -> None:
    _seed_tracks(postgres_url)
    original = _track_ids(postgres_url)
    engine = create_engine(postgres_url)
    try:
        with engine.begin() as writer:
            writer.execute(text('LOCK TABLE tracks IN ROW EXCLUSIVE MODE'))
            with pytest.raises(CompactError, match='rolled back'):
                compact(
                    postgres_url, execute=True, backup_confirmed=True,
                    application_stopped=True, out=io.StringIO(),
                )
        assert _track_ids(postgres_url) == original
    finally:
        engine.dispose()
