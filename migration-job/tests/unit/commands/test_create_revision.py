from __future__ import annotations

from unittest.mock import Mock

import pytest
from alembic.config import Config
from pytest_mock import MockerFixture

import migration_job.commands.create_revision as create_revision_module
from migration_job.commands.create_revision import create_revision


def test_create_revision_uses_next_revision_id(mocker: MockerFixture) -> None:
    """既存の最大リビジョン番号の次を使って新しいリビジョンを作成する。"""
    script = Mock()
    script.get_heads.return_value = ['0008_landing_identity']
    script.walk_revisions.return_value = [
        Mock(revision='0008_landing_identity'),
        Mock(revision='0007_import_jobs'),
        Mock(revision='0006_reconcile_schema'),
        Mock(revision='0001_baseline'),
    ]

    mocker.patch.object(
        create_revision_module.ScriptDirectory,
        'from_config',
        return_value=script,
    )
    revision = mocker.patch.object(create_revision_module.command, 'revision')

    config = Mock(spec=Config)

    create_revision('test revision', config)

    revision.assert_called_once_with(
        config,
        message='test revision',
        rev_id='0009_test_revision',
    )


def test_create_revision_normalizes_revision_message(mocker: MockerFixture) -> None:
    """リビジョンメッセージを正規化してリビジョンIDに使用する。"""
    script = Mock()
    script.get_heads.return_value = ['0008_landing_identity']
    script.walk_revisions.return_value = [
        Mock(revision='0008_landing_identity'),
    ]

    mocker.patch.object(
        create_revision_module.ScriptDirectory,
        'from_config',
        return_value=script,
    )
    revision = mocker.patch.object(create_revision_module.command, 'revision')

    config = Mock(spec=Config)

    create_revision('Add   Landing---Status', config)

    revision.assert_called_once_with(
        config,
        message='Add   Landing---Status',
        rev_id='0009_add_landing_status',
    )


def test_create_revision_rejects_invalid_message(mocker: MockerFixture) -> None:
    """有効なリビジョンIDを生成できないメッセージを拒否する。"""
    script = Mock()
    script.get_heads.return_value = ['0008_landing_identity']
    script.walk_revisions.return_value = [
        Mock(revision='0008_landing_identity'),
    ]

    mocker.patch.object(
        create_revision_module.ScriptDirectory,
        'from_config',
        return_value=script,
    )
    revision = mocker.patch.object(create_revision_module.command, 'revision')

    config = Mock(spec=Config)

    with pytest.raises(
        ValueError,
        match='revision message must contain at least one alphanumeric character',
    ):
        create_revision('---___---', config)

    revision.assert_not_called()


def test_create_revision_rejects_multiple_heads(mocker: MockerFixture) -> None:
    """複数のマイグレーションheadが存在する場合は新規作成を拒否する。"""
    script = Mock()
    script.get_heads.return_value = [
        '0008_first_branch',
        '0008_second_branch',
    ]

    mocker.patch.object(
        create_revision_module.ScriptDirectory,
        'from_config',
        return_value=script,
    )
    revision = mocker.patch.object(create_revision_module.command, 'revision')

    config = Mock(spec=Config)

    with pytest.raises(
        RuntimeError,
        match='expected exactly one migration head, found 2',
    ):
        create_revision('test revision', config)

    revision.assert_not_called()
