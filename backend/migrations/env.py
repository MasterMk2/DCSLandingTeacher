"""Alembic environment for DCS Landing Teacher.

Runs with a plain sync engine (SQLite via the stdlib driver). The database
URL is injected programmatically by :mod:`app.models.migrations` or read
from ``alembic.ini`` / ``-x db_url=...`` for CLI usage.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig as _fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the backend package importable when invoked from the CLI.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

config = context.config

if config.config_file_name is not None:
    _fileConfig(config.config_file_name)

# Allow overriding the URL from the command line: alembic -x db_url=sqlite:///...
x_args = context.get_x_argument(as_dictionary=True)
if x_args.get("db_url"):
    config.set_main_option("sqlalchemy.url", x_args["db_url"])

# Model metadata (used by autogenerate).
from app.models.base import Base  # noqa: E402
from app.models import entities  # noqa: E402, F401

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live connection."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
