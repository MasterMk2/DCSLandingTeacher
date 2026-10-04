from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy.engine import make_url

from migration_job.commands import run_migration_job
from migration_job.commands.run_migration_job import alembic_ini_path


def test_alembic_ini_path_uses_the_migration_job_project_file() -> None:
    """ローカル実行では migration-job 自身の Alembic 設定を使う。"""
    expected = Path(__file__).parents[3] / 'alembic.ini'

    assert alembic_ini_path() == expected


def test_main_preserves_reserved_characters_and_runs_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('DB_USER', 'test@user')
    monkeypatch.setenv('DB_PASSWORD', 'test@pass/word:%#?+')
    monkeypatch.setenv('DB_HOST', 'db')
    monkeypatch.setenv('DB_PORT', '5432')
    monkeypatch.setenv('DB_NAME', 'dlt_test')
    upgrade = Mock()
    monkeypatch.setattr(run_migration_job.command, 'upgrade', upgrade)

    run_migration_job.main()

    upgrade.assert_called_once()
    config, revision = upgrade.call_args.args
    assert revision == 'head'
    url = make_url(config.get_main_option('sqlalchemy.url'))
    assert url.username == 'test@user'
    assert url.password == 'test@pass/word:%#?+'
    assert url.host == 'db'
    assert url.port == 5432
    assert url.database == 'dlt_test'
