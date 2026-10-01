from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import URL


def alembic_ini_path() -> Path:
    """Return the Alembic configuration bundled with this migration project."""
    project_file = Path(__file__).parents[3] / 'alembic.ini'
    if project_file.is_file():
        return project_file

    return Path('/app/alembic.ini')


def main() -> None:
    database_url = URL.create(
        drivername='postgresql+psycopg',
        username=os.environ['DB_USER'],
        password=os.environ['DB_PASSWORD'],
        host=os.environ.get('DB_HOST', 'db'),
        port=int(os.environ.get('DB_PORT', '5432')),
        database=os.environ['DB_NAME'],
    )

    config = Config(str(alembic_ini_path()))

    # Alembic's ConfigParser treats "%" specially, so escape percent signs
    # that may appear in URL-encoded passwords/usernames.
    url = database_url.render_as_string(hide_password=False)
    config.set_main_option('sqlalchemy.url', url.replace('%', '%%'))

    command.upgrade(config, 'head')


if __name__ == '__main__':
    main()
