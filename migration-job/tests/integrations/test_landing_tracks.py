"""PostgreSQL migration checks for the landing-track table."""

from __future__ import annotations

import json
import os
import zlib
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / 'migrations'
REVISION_0008 = '0008_landing_identity'
REVISION_0009 = '0009_landing_tracks'


@pytest.fixture
def postgres_db() -> Iterator[tuple[Engine, Config]]:
    """Give each test an isolated schema in the configured test database."""
    test_url = os.getenv('DLT_TEST_POSTGRES_URL')
    if not test_url:
        pytest.skip('DLT_TEST_POSTGRES_URL is required for PostgreSQL integration tests')

    schema = f'dlt_landing_tracks_{uuid4().hex}'
    admin_engine = create_engine(test_url)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    scoped_url = make_url(test_url).update_query_dict({'options': f'-csearch_path={schema}'})
    engine = create_engine(scoped_url)
    config = Config()
    config.set_main_option('script_location', str(MIGRATIONS_DIR))
    config.set_main_option(
        'sqlalchemy.url', scoped_url.render_as_string(hide_password=False).replace('%', '%%')
    )
    try:
        yield engine, config
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


def _seed_landing_at_0008(engine: Engine, config: Config) -> dict[str, object]:
    command.upgrade(config, REVISION_0008)
    approach = {'airframe': 'F-16C', 'samples': [{'time': 1.5, 'agl': 12.25}]}
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO flights (id, source_id, started_at, created_at) "
                "VALUES (1, 'default', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO objects (id, flight_id, acmi_id, first_seen, last_seen, removed) "
                "VALUES (1, 1, '1001', 0.0, 10.0, false)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO landings (id, flight_id, object_id, source_id, kind, outcome, "
                "touchdown_time, approach_track, created_at) "
                "VALUES (1, 1, 1, 'default', 'land', 'full_stop', 1.5, "
                "CAST(:approach AS JSON), CURRENT_TIMESTAMP)"
            ),
            {'approach': json.dumps(approach)},
        )
        connection.execute(
            text(
                "INSERT INTO landings (id, flight_id, object_id, source_id, kind, outcome, "
                "touchdown_time, created_at) "
                "VALUES (2, 1, 1, 'default', 'land', 'full_stop', 2.5, CURRENT_TIMESTAMP)"
            )
        )
    return approach


def test_empty_postgres_database_upgrades_to_head(postgres_db: tuple[Engine, Config]) -> None:
    engine, config = postgres_db

    command.upgrade(config, 'head')

    with engine.connect() as connection:
        version = connection.execute(text('SELECT version_num FROM alembic_version')).scalar_one()
    assert version == REVISION_0009
    tables = set(inspect(engine).get_table_names())
    assert {'flights', 'objects', 'landings', 'landing_tracks'} <= tables


def test_0009_moves_existing_json_to_compressed_track(
    postgres_db: tuple[Engine, Config],
) -> None:
    engine, config = postgres_db
    approach = _seed_landing_at_0008(engine, config)

    command.upgrade(config, 'head')

    landing_columns = {column['name'] for column in inspect(engine).get_columns('landings')}
    assert 'approach_track' not in landing_columns
    with engine.connect() as connection:
        rows = connection.execute(
            text('SELECT landing_id, approach_track FROM landing_tracks ORDER BY landing_id')
        ).all()
    assert len(rows) == 1
    assert rows[0].landing_id == 1
    assert json.loads(zlib.decompress(rows[0].approach_track)) == approach


def test_deleting_landing_cascades_to_its_track(postgres_db: tuple[Engine, Config]) -> None:
    engine, config = postgres_db
    _seed_landing_at_0008(engine, config)
    command.upgrade(config, 'head')

    with engine.begin() as connection:
        connection.execute(text('DELETE FROM landings WHERE id = 1'))
        track_count = connection.execute(text('SELECT COUNT(*) FROM landing_tracks')).scalar_one()
        other_landing_count = connection.execute(
            text('SELECT COUNT(*) FROM landings WHERE id = 2')
        ).scalar_one()
    assert track_count == 0
    assert other_landing_count == 1


def test_0009_downgrade_restores_approach_json(postgres_db: tuple[Engine, Config]) -> None:
    engine, config = postgres_db
    approach = _seed_landing_at_0008(engine, config)
    command.upgrade(config, 'head')

    command.downgrade(config, REVISION_0008)

    assert 'landing_tracks' not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        rows = connection.execute(
            text('SELECT id, approach_track FROM landings ORDER BY id')
        ).all()
    assert rows[0].approach_track == approach
    assert rows[1].approach_track is None
