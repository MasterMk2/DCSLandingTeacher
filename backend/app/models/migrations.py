"""Programmatic Alembic migration runner (Issue #7).

The application applies pending migrations at startup (configurable via
``DLT_MIGRATIONS_ON_STARTUP``). Databases created before Alembic was
introduced (plain ``create_all`` schema, no ``alembic_version`` table) are
detected and stamped at the baseline revision so subsequent migrations apply
on top of them.
"""

from __future__ import annotations

import asyncio
from logging import getLogger
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

logger = getLogger(__name__)

#: First revision; matches the pre-Alembic ``create_all`` schema.
BASELINE_REVISION = "0001_baseline"
#: Newest revision; kept in sync with migrations/versions/.
HEAD_REVISION = "0004_approach_pattern"

#: Tables that identify an existing (pre- or post-migration) database.
_SCHEMA_TABLES = {"flights", "objects", "tracks", "landings"}

#: Default location of the Alembic scripts, relative to this module:
#: backend/app/models/migrations.py -> backend/migrations
DEFAULT_MIGRATIONS_DIR = str(Path(__file__).resolve().parents[2] / "migrations")


def _sync_url(database_url: str) -> str:
    """Translate the async driver URL to its sync equivalent."""
    if database_url.startswith("sqlite+aiosqlite"):
        return database_url.replace("sqlite+aiosqlite", "sqlite", 1)
    return database_url


def make_alembic_config(
    database_url: str, migrations_dir: str | None = None
) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", migrations_dir or DEFAULT_MIGRATIONS_DIR)
    cfg.set_main_option("sqlalchemy.url", _sync_url(database_url))
    return cfg


def _run_sync(database_url: str, migrations_dir: str | None) -> None:
    cfg = make_alembic_config(database_url, migrations_dir)
    engine = create_engine(_sync_url(database_url))
    try:
        tables = set(inspect(engine).get_table_names())
        has_schema = bool(tables & _SCHEMA_TABLES)
        has_version = "alembic_version" in tables
        if has_schema and not has_version:
            logger.info(
                "pre-migration schema detected; stamping %s", BASELINE_REVISION
            )
            command.stamp(cfg, BASELINE_REVISION)
    finally:
        engine.dispose()
    command.upgrade(cfg, "head")


async def run_migrations(
    database_url: str, migrations_dir: str | None = None
) -> None:
    """Bring the database up to the latest revision.

    An empty database is created from scratch; a legacy ``create_all``
    database is stamped at the baseline first. Safe to run repeatedly.
    """
    await asyncio.to_thread(_run_sync, database_url, migrations_dir)
