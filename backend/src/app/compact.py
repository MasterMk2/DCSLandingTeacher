"""Remove old raw tracks that the current landing retention policy would not keep.

Run with the application stopped. The default is a read-only dry run::

    python -m app.compact

Take and verify a PostgreSQL backup before executing. A successful delete is
transactional; PostgreSQL disk space is reclaimed separately with VACUUM or
maintenance appropriate to the deployment.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from sqlalchemy import Connection, create_engine, inspect, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.detection.classify import ObjectClass, classify_object_type
from app.detection.detector import DetectionConfig
from app.grading.config import load_grading_config
from app.retention import retention_window

_ALWAYS = 1e300
_REQUIRED_TABLES = {'alembic_version', 'flights', 'objects', 'tracks', 'landings'}


class CompactError(RuntimeError):
    """The database cannot be compacted safely."""


@dataclass(frozen=True)
class CompactReport:
    before: int
    keep: int
    delete: int
    after: int | None = None


def _detection_config() -> DetectionConfig:
    settings = Settings()
    if not Path(settings.grading_config_path).is_file():
        raise CompactError('grading configuration is missing; retention cannot be determined safely')
    return load_grading_config(settings.grading_config_path).to_detection_config()


def keep_windows(
    connection: Connection, detection: DetectionConfig
) -> dict[int, list[tuple[float, float]]]:
    """Return merged spans for landing aircraft, their carriers, and statics."""
    windows: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for object_id, object_type in connection.execute(text('SELECT id, type FROM objects')):
        if classify_object_type(object_type) == ObjectClass.STATIC:
            windows[object_id].append((-_ALWAYS, _ALWAYS))
    for object_id, carrier_id, touchdown in connection.execute(
        text(
            'SELECT object_id, carrier_object_id, touchdown_time FROM landings '
            'WHERE touchdown_time IS NOT NULL'
        )
    ):
        span = retention_window(touchdown, touchdown, detection)
        windows[object_id].append(span)
        if carrier_id is not None:
            windows[carrier_id].append(span)

    merged: dict[int, list[tuple[float, float]]] = {}
    for object_id, spans in windows.items():
        spans.sort()
        result: list[tuple[float, float]] = []
        for start, end in spans:
            if result and start <= result[-1][1]:
                result[-1] = (result[-1][0], max(result[-1][1], end))
            else:
                result.append((start, end))
        merged[object_id] = result
    return merged


def _verify_schema(connection: Connection) -> None:
    tables = set(inspect(connection).get_table_names())
    missing = _REQUIRED_TABLES - tables
    if missing:
        raise CompactError(f'database schema is incomplete: {sorted(missing)}')
    versions = connection.execute(text('SELECT version_num FROM alembic_version')).scalars().all()
    if versions != ['0009_landing_tracks']:
        raise CompactError('database must be at the supported Alembic revision')


def _orphan_count(connection: Connection) -> int:
    return connection.execute(
        text(
            'SELECT COUNT(*) FROM tracks AS t '
            'LEFT JOIN objects AS o ON o.id = t.object_id '
            'LEFT JOIN flights AS f ON f.id = t.flight_id '
            'WHERE o.id IS NULL OR f.id IS NULL'
        )
    ).scalar_one()


def _install_windows(connection: Connection, detection: DetectionConfig) -> None:
    connection.execute(
        text(
            'CREATE TEMP TABLE compact_keep_windows '
            '(object_id bigint NOT NULL, starts double precision NOT NULL, '
            'ends double precision NOT NULL) ON COMMIT DROP'
        )
    )
    rows = [
        {'object_id': object_id, 'starts': start, 'ends': end}
        for object_id, spans in keep_windows(connection, detection).items()
        for start, end in spans
    ]
    if rows:
        connection.execute(
            text(
                'INSERT INTO compact_keep_windows (object_id, starts, ends) '
                'VALUES (:object_id, :starts, :ends)'
            ),
            rows,
        )
    connection.execute(
        text('CREATE INDEX ON compact_keep_windows (object_id, starts, ends)')
    )
    connection.execute(text('ANALYZE compact_keep_windows'))


_KEPT_TRACK = (
    'EXISTS (SELECT 1 FROM compact_keep_windows AS k '
    'WHERE k.object_id = t.object_id AND t.mission_time BETWEEN k.starts AND k.ends)'
)


def _count_tracks(connection: Connection) -> tuple[int, int]:
    before = connection.execute(text('SELECT COUNT(*) FROM tracks')).scalar_one()
    keep = connection.execute(
        text(f'SELECT COUNT(*) FROM tracks AS t WHERE {_KEPT_TRACK}')
    ).scalar_one()
    if keep > before:
        raise CompactError('retention count exceeds the track count')
    return before, keep


def compact(
    database_url: str,
    *,
    execute: bool = False,
    backup_confirmed: bool = False,
    application_stopped: bool = False,
    max_delete: int | None = None,
    out: TextIO = sys.stdout,
) -> CompactReport:
    """Preview or atomically delete tracks outside the shared retention windows."""
    if make_url(database_url).get_backend_name() != 'postgresql':
        raise CompactError('only PostgreSQL databases are supported')
    if execute and (not backup_confirmed or not application_stopped):
        raise CompactError('execution requires a verified backup and a stopped application')
    if max_delete is not None and max_delete < 0:
        raise CompactError('max_delete must be nonnegative')

    detection = _detection_config()
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            if execute:
                # Refuse an active writer; hold the locks until verification and commit.
                connection.execute(
                    text(
                        'LOCK TABLE flights, objects, landings, tracks '
                        'IN SHARE ROW EXCLUSIVE MODE NOWAIT'
                    )
                )
            _verify_schema(connection)
            if _orphan_count(connection):
                raise CompactError('tracks contain missing flight or object references')
            _install_windows(connection, detection)
            before, keep = _count_tracks(connection)
            delete = before - keep
            print(f'tracks: total={before:,}, keep={keep:,}, delete={delete:,}', file=out)
            keep_sample = connection.execute(
                text(f'SELECT t.id FROM tracks AS t WHERE {_KEPT_TRACK} ORDER BY t.id LIMIT 10')
            ).scalars().all()
            delete_sample = connection.execute(
                text(f'SELECT t.id FROM tracks AS t WHERE NOT {_KEPT_TRACK} ORDER BY t.id LIMIT 10')
            ).scalars().all()
            print(f'keep sample ids: {keep_sample}', file=out)
            print(f'delete sample ids: {delete_sample}', file=out)

            if not execute:
                print('dry run: no tracks deleted', file=out)
                return CompactReport(before, keep, delete)
            if max_delete is not None and delete > max_delete:
                raise CompactError(f'delete count {delete:,} exceeds max_delete {max_delete:,}')
            if before and not keep:
                raise CompactError('all tracks would be deleted; inspect the retention inputs')

            connection.execute(text(f'DELETE FROM tracks AS t WHERE NOT {_KEPT_TRACK}'))
            after = connection.execute(text('SELECT COUNT(*) FROM tracks')).scalar_one()
            if after != keep or _orphan_count(connection):
                raise CompactError('post-delete count or reference verification failed')
            print(f'verified: tracks={after:,}; parent references intact', file=out)
            return CompactReport(before, keep, delete, after)
    except SQLAlchemyError as error:
        raise CompactError('database operation failed; changes were rolled back') from error
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Preview or remove unneeded PostgreSQL tracks')
    parser.add_argument('--execute', action='store_true', help='delete tracks after a fresh count')
    parser.add_argument('--backup-confirmed', action='store_true', help='a verified backup exists')
    parser.add_argument('--application-stopped', action='store_true', help='all application writers stopped')
    parser.add_argument('--max-delete', type=int, help='abort if more than this many tracks would go')
    args = parser.parse_args(argv)
    try:
        database_url = os.getenv('DLT_DATABASE_URL')
        if not database_url:
            raise CompactError('set DLT_DATABASE_URL to the target PostgreSQL database')
        compact(
            database_url,
            execute=args.execute,
            backup_confirmed=args.backup_confirmed,
            application_stopped=args.application_stopped,
            max_delete=args.max_delete,
        )
    except CompactError as error:
        print(f'error: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
