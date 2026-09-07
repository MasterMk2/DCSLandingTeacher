from __future__ import annotations

import shutil
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from migration_job.commands.create_revision import create_revision

PROJECT_ROOT = Path(__file__).parents[3]
MIGRATIONS_DIR = PROJECT_ROOT / 'migrations'


def _create_test_config(tmp_path: Path) -> Config:
    target_migrations = tmp_path / 'migrations'
    shutil.copytree(MIGRATIONS_DIR, target_migrations)

    config = Config()
    config.set_main_option('script_location', str(target_migrations))
    config.set_main_option('file_template', '%%(rev)s')
    config.set_main_option('timezone', 'UTC')

    return config


def test_create_revision_generates_next_revision_file(tmp_path: Path) -> None:
    """実際のAlembic環境で次番号のリビジョンファイルを生成する。"""
    config = _create_test_config(tmp_path)

    create_revision('test revision', config)

    revision_file = tmp_path / 'migrations' / 'versions' / '0009_test_revision.py'

    assert revision_file.is_file()


def test_create_revision_sets_revision_chain(tmp_path: Path) -> None:
    """生成したリビジョンを既存headの直後に接続する。"""
    config = _create_test_config(tmp_path)

    create_revision('test revision', config)

    script = ScriptDirectory.from_config(config)
    revision = script.get_revision('0009_test_revision')

    assert revision is not None
    assert revision.revision == '0009_test_revision'
    assert revision.down_revision == '0008_landing_identity'


def test_create_revision_moves_head_to_new_revision(tmp_path: Path) -> None:
    """新規リビジョン作成後にheadが新しいリビジョンへ移動する。"""
    config = _create_test_config(tmp_path)

    create_revision('test revision', config)

    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == ['0009_test_revision']


def test_create_revision_preserves_existing_history(tmp_path: Path) -> None:
    """新規リビジョンを追加しても既存のマイグレーション履歴を保持する。"""
    config = _create_test_config(tmp_path)

    create_revision('test revision', config)

    script = ScriptDirectory.from_config(config)
    revisions = {revision.revision: revision.down_revision for revision in script.walk_revisions()}

    assert revisions['0009_test_revision'] == '0008_landing_identity'
    assert revisions['0008_landing_identity'] == '0007_import_jobs'
    assert revisions['0001_baseline'] is None
