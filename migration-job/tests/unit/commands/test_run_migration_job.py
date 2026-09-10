from __future__ import annotations

from pathlib import Path

from migration_job.commands.run_migration_job import alembic_ini_path


def test_alembic_ini_path_uses_the_migration_job_project_file() -> None:
    """ローカル実行では migration-job 自身の Alembic 設定を使う。"""
    expected = Path(__file__).parents[3] / 'alembic.ini'

    assert alembic_ini_path() == expected
