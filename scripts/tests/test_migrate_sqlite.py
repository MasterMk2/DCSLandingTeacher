"""SQLite data conversion contracts."""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import zlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import JSON, Boolean, Column, DateTime, LargeBinary, create_engine, text
from sqlalchemy.engine import Engine, make_url

SCRIPT = Path(__file__).parents[1] / "migrate_sqlite.py"
spec = importlib.util.spec_from_file_location("migrate_sqlite", SCRIPT)
assert spec and spec.loader
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def test_conversion_preserves_json_boolean_utc_and_compressed_bytes() -> None:
    assert migration.convert_value(Column("factors", JSON), '["OK"]') == ["OK"]
    assert migration.convert_value(Column("removed", Boolean), 0) is False
    assert migration.convert_value(Column("removed", Boolean), 1) is True
    converted = migration.convert_value(Column("created_at", DateTime), "2026-01-02 03:04:05")
    assert converted == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    converted = migration.convert_value(Column("graded_at", DateTime), "2026-01-02T12:04:05+09:00")
    assert converted == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    blob = b"compressed track bytes"
    assert migration.convert_value(Column("approach_track", LargeBinary), blob) == blob
    assert migration.convert_value(Column("factors", JSON), None) is None


def test_invalid_boolean_is_rejected() -> None:
    with pytest.raises(ValueError, match="0 or 1"):
        migration.convert_value(Column("removed", Boolean), 2)


@pytest.fixture
def postgres_db() -> Iterator[Engine]:
    url = os.getenv("DLT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("DLT_TEST_POSTGRES_URL is required")
    schema = f"dlt_sqlite_migration_{uuid4().hex}"
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(make_url(url).update_query_dict({"options": f"-csearch_path={schema}"}))
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def sqlite_source(tmp_path: Path) -> Path:
    from app.models import Base

    path = tmp_path / "source.sqlite"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    tables = Base.metadata.tables
    timestamp = datetime(2026, 1, 2, 3, 4, 5)
    with engine.begin() as connection:
        connection.execute(tables["flights"].insert(), {
            "id": 11, "source_id": "fixture", "started_at": timestamp, "created_at": timestamp
        })
        connection.execute(tables["objects"].insert(), [
            {"id": 21, "flight_id": 11, "acmi_id": "a", "first_seen": 0, "last_seen": 2,
             "removed": False},
            {"id": 22, "flight_id": 11, "acmi_id": "b", "first_seen": 0, "last_seen": 2,
             "removed": True},
        ])
        connection.execute(tables["tracks"].insert(), {
            "id": 31, "flight_id": 11, "object_id": 21, "mission_time": 1, "on_ground": True
        })
        connection.execute(tables["landings"].insert(), {
            "id": 41, "flight_id": 11, "object_id": 21, "carrier_object_id": 22,
            "source_id": "fixture", "created_at": timestamp,
            "factors": ["OK"], "metrics": {"score": 10}, "graded_at": timestamp,
        })
        connection.execute(tables["landing_tracks"].insert(), {
            "landing_id": 41, "approach_track": {"samples": [{"time": 1}]}
        })
        connection.execute(tables["import_jobs"].insert(), {
            "id": "fixture-job", "filename": "fixture.acmi", "status": "completed",
            "created_at": timestamp,
        })
    engine.dispose()
    return path


def test_cli_copies_all_tables_and_advances_ids(
    postgres_db: Engine, sqlite_source: Path
) -> None:
    before = sqlite_source.read_bytes()
    env = {**os.environ, "DLT_DATABASE_URL": postgres_db.url.render_as_string(hide_password=False)}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(sqlite_source)], env=env,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["verified_rows"] == {
        "flights": 1, "objects": 2, "tracks": 1, "landings": 1,
        "landing_tracks": 1, "import_jobs": 1,
    }
    with postgres_db.begin() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0009_landing_tracks"
        )
        row = connection.execute(text(
            "SELECT factors, metrics, graded_at, object_id, carrier_object_id FROM landings"
        )).one()
        assert tuple(row) == (["OK"], {"score": 10},
                              datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC), 21, 22)
        removed = connection.execute(
            text("SELECT removed FROM objects ORDER BY id")
        ).scalars().all()
        assert removed == [False, True]
        assert connection.execute(text("SELECT on_ground FROM tracks")).scalar_one() is True
        blob = connection.execute(text("SELECT approach_track FROM landing_tracks")).scalar_one()
        assert json.loads(zlib.decompress(blob)) == {"samples": [{"time": 1}]}
        for name, expected in {"flights": 12, "objects": 23, "tracks": 32, "landings": 42}.items():
            assert connection.execute(text(
                "SELECT nextval(pg_get_serial_sequence(:table, 'id'))"
            ), {"table": name}).scalar_one() == expected
    with pytest.raises(ValueError, match="must be empty"):
        migration.migrate(sqlite_source, env["DLT_DATABASE_URL"])
    assert sqlite_source.read_bytes() == before


def test_failed_copy_rolls_back_and_can_retry(postgres_db: Engine, sqlite_source: Path) -> None:
    url = postgres_db.url.render_as_string(hide_password=False)
    with sqlite3.connect(sqlite_source) as connection:
        connection.execute("UPDATE landings SET factors = 'broken-json'")
    before = sqlite_source.read_bytes()
    with pytest.raises(ValueError):
        migration.migrate(sqlite_source, url)
    assert sqlite_source.read_bytes() == before
    with postgres_db.connect() as connection:
        for name in migration.TABLES:
            assert connection.execute(text(f'SELECT count(*) FROM "{name}"')).scalar_one() == 0
    with sqlite3.connect(sqlite_source) as connection:
        connection.execute("UPDATE landings SET factors = '[\"OK\"]'")
    assert migration.migrate(sqlite_source, url)["landings"] == 1


def test_source_orphans_are_rejected_before_target_preparation(
    postgres_db: Engine, sqlite_source: Path
) -> None:
    with sqlite3.connect(sqlite_source) as connection:
        connection.execute("UPDATE tracks SET object_id = 999")
    with pytest.raises(ValueError, match="foreign key"):
        migration.migrate(sqlite_source, postgres_db.url.render_as_string(hide_password=False))
    with postgres_db.connect() as connection:
        assert connection.execute(text(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = current_schema()"
        )).scalar_one() == 0


def test_sql_null_and_json_null_remain_distinct(postgres_db: Engine, sqlite_source: Path) -> None:
    with sqlite3.connect(sqlite_source) as connection:
        connection.execute("UPDATE landings SET factors = NULL, metrics = 'null'")
    migration.migrate(sqlite_source, postgres_db.url.render_as_string(hide_password=False))
    with postgres_db.connect() as connection:
        assert connection.execute(text(
            "SELECT factors IS NULL, metrics IS NULL, metrics::text = 'null' FROM landings"
        )).one() == (True, False, True)
