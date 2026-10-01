"""Copy a stopped SQLite database into an empty, migrated PostgreSQL database."""

from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    LargeBinary,
    MetaData,
    create_engine,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Connection, Engine, make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
TABLES = ("flights", "objects", "tracks", "landings", "landing_tracks", "import_jobs")
SCRIPT_VERSION = "1"


def convert_value(column: Column[object], value: object) -> object:
    """Convert SQLite storage values without changing compressed track payloads."""
    if value is None:
        return None
    if isinstance(column.type, JSON):
        if not isinstance(value, str):
            raise ValueError("JSON source must be text")
        decoded = json.loads(value)
        return JSON.NULL if decoded is None else decoded
    if isinstance(column.type, Boolean):
        if value not in (0, 1):
            raise ValueError("Boolean source must be 0 or 1")
        return bool(value)
    if isinstance(column.type, DateTime):
        if not isinstance(value, str):
            raise ValueError("Datetime source must be text")
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    if isinstance(column.type, LargeBinary) and not isinstance(value, bytes):
        raise ValueError("Track source must be binary")
    return value


def _assert_empty(connection: Connection) -> None:
    names = set(inspect(connection).get_table_names())
    if names - set(TABLES) - {"alembic_version"}:
        raise ValueError("Target contains unexpected tables")
    for name in names & set(TABLES):
        count = connection.execute(text(f'SELECT count(*) FROM "{name}"')).scalar_one()
        if count:
            raise ValueError("Target application tables must be empty")


def _config(engine: Engine) -> Config:
    config = Config(stdout=io.StringIO())
    config.set_main_option("script_location", str(ROOT / "migration-job" / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", engine.url.render_as_string(hide_password=False).replace("%", "%%")
    )
    return config


def _verify_references(connection: Connection, metadata: MetaData) -> None:
    for name in TABLES:
        table = metadata.tables[name]
        for foreign_key in table.foreign_keys:
            parent = foreign_key.column
            child = foreign_key.parent
            query = select(func.count()).select_from(
                table.outerjoin(parent.table, child == parent)
            ).where(child.is_not(None), parent.is_(None))
            if connection.execute(query).scalar_one():
                raise ValueError("Foreign key verification failed")


def _advance_sequences(connection: Connection, metadata: MetaData) -> None:
    for name in TABLES:
        table = metadata.tables[name]
        if "id" not in table.c or name == "import_jobs":
            continue
        sequence = connection.execute(
            text("SELECT pg_get_serial_sequence(:table, 'id')"), {"table": name}
        ).scalar_one()
        if sequence is None:
            raise ValueError("Expected ID sequence is missing")
        maximum = connection.execute(select(func.max(table.c.id))).scalar_one()
        connection.execute(
            text("SELECT setval(CAST(:sequence AS regclass), :value, :called)"),
            {"sequence": sequence, "value": max(maximum or 0, 1), "called": maximum is not None},
        )


def migrate(source_path: Path, target_url: str) -> dict[str, int]:
    """Upgrade an empty target and copy all application rows atomically.

    Args:
        source_path: Stopped SQLite database using the landing_tracks schema.
        target_url: PostgreSQL psycopg SQLAlchemy URL.

    Returns:
        Verified row counts per application table.

    Raises:
        ValueError: Source schema or target contents are unsuitable for migration.
    """
    if not source_path.is_file():
        raise ValueError("Source SQLite file does not exist")
    if make_url(target_url).drivername != "postgresql+psycopg":
        raise ValueError("Target must use postgresql+psycopg")
    source = sqlite3.connect(f"{source_path.resolve().as_uri()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    engine = create_engine(target_url)
    try:
        source.execute("BEGIN")
        if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Source integrity check failed")
        if source.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Source contains broken foreign key references")
        source_tables = {
            row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if source_tables - {"alembic_version", "sqlite_sequence"} != set(TABLES):
            raise ValueError("Source must contain exactly the six supported application tables")
        with engine.connect() as connection:
            _assert_empty(connection)
        config = _config(engine)
        command.upgrade(config, "head")
        command.check(config)
        metadata = MetaData()
        metadata.reflect(engine, only=TABLES)
        for table in metadata.tables.values():
            for column in table.c:
                if isinstance(column.type, JSON):
                    column.type.none_as_null = True
        counts: dict[str, int] = {}
        with engine.begin() as connection:
            connection.execute(text(
                "LOCK TABLE " + ", ".join(f'"{name}"' for name in TABLES)
                + " IN SHARE ROW EXCLUSIVE MODE"
            ))
            _assert_empty(connection)
            head = ScriptDirectory.from_config(config).get_current_head()
            version = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            if version != head:
                raise ValueError("Target Alembic revision does not match head")
            for name in TABLES:
                table = metadata.tables[name]
                rows = source.execute(f'SELECT * FROM "{name}"')
                columns = {item[0] for item in rows.description}
                if columns != set(table.c.keys()):
                    raise ValueError(f"Source columns do not match target for {name}")
                count = 0
                while batch := rows.fetchmany(1000):
                    converted = [
                        {column.name: convert_value(column, row[column.name]) for column in table.c}
                        for row in batch
                    ]
                    connection.execute(table.insert(), converted)
                    count += len(batch)
                counts[name] = count
                actual = connection.execute(select(func.count()).select_from(table)).scalar_one()
                if actual != count:
                    raise ValueError("Row count verification failed")
            _verify_references(connection, metadata)
            _advance_sequences(connection, metadata)
        return counts
    finally:
        source.close()
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Stopped SQLite database (read only)")
    parser.add_argument("--version", action="version", version=SCRIPT_VERSION)
    args = parser.parse_args()
    target_url = os.getenv("DLT_DATABASE_URL")
    if not target_url:
        parser.error("DLT_DATABASE_URL is required")
    try:
        counts = migrate(args.source, target_url)
    except Exception:
        # Driver errors may contain credentials, source values, and local paths.
        parser.exit(
            1, "Migration failed; preserve the source and check the target before retrying.\n"
        )
    print(json.dumps({"script_version": SCRIPT_VERSION, "verified_rows": counts}, sort_keys=True))


if __name__ == "__main__":
    main()
