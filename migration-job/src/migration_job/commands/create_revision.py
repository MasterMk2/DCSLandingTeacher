from __future__ import annotations

import re
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

ALEMBIC_INI = Path(__file__).parents[3] / 'alembic.ini'

REVISION_PREFIX = re.compile(r'^(\d{4})_')


def _next_revision_number(script: ScriptDirectory) -> int:
    numbers = []

    for revision in script.walk_revisions():
        match = REVISION_PREFIX.match(revision.revision)
        if match:
            numbers.append(int(match.group(1)))

    return max(numbers, default=0) + 1


def _slugify(message: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '_', message.lower()).strip('_')

    if not slug:
        raise ValueError('revision message must contain at least one alphanumeric character')

    return slug


def create_revision(message: str, config: Config) -> None:
    script = ScriptDirectory.from_config(config)

    heads = script.get_heads()
    if len(heads) != 1:
        raise RuntimeError(f'expected exactly one migration head, found {len(heads)}: {heads}')

    number = _next_revision_number(script)
    slug = _slugify(message)
    revision_id = f'{number:04d}_{slug}'

    command.revision(
        config,
        message=message,
        rev_id=revision_id,
    )


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit('usage: migration-job-revision "revision message"')


if __name__ == '__main__':
    main()
