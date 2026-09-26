"""Compact an existing database down to the raw tracks retention keeps.

The ingestor now writes an aircraft's samples only around its landings
(app/retention.py), but a database recorded before that still holds every
sample of every object -- the production one passed 87 million rows. This
copies it into a new file keeping, of ``tracks``, only what retention would
have kept: each landing's window for the aircraft and for the ship it landed
on, and every static. Every other table is copied whole.

Run it with the application STOPPED, after the rescan (app/rescan.py) has
had its one look at the full history -- windows are all that survive::

    python -m app.compact /data/dlt.db           # build + verify <db>.compact
    python -m app.compact /data/dlt.db --swap    # ...and put it in place

Without ``--swap`` the original is never touched. With it, the original is
renamed to ``<db>.pre-compact-<UTC time>`` (not deleted: removing it is the
operator's decision) and the compact copy takes its name.

Plain ``sqlite3`` rather than the async engine: one connection, one pass,
no event loop. The ``tracks`` pass is a single sequential scan with a lookup
into a small in-memory table of windows per row, all inside SQLite.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings
from app.detection.classify import ObjectClass, classify_object_type
from app.detection.detector import DetectionConfig
from app.grading.config import load_grading_config
from app.grading.packaged import resolve_config_path
from app.retention import retention_window

#: Stand-in for "all of time" in the windows table (SQLite REAL).
_ALWAYS = 1e300


class CompactError(RuntimeError):
    pass


def _detection_config() -> DetectionConfig:
    """The detection windows the running app (and so the rebuild) uses."""
    settings = Settings()
    path = resolve_config_path(settings.grading_config_path, "grading.yaml")
    return load_grading_config(path).to_detection_config()


def keep_windows(
    connection: sqlite3.Connection, detection: DetectionConfig
) -> dict[int, list[tuple[float, float]]]:
    """object row id -> merged spans of ``tracks`` to keep.

    What the ingestor keeps: each landing's window for its aircraft and for
    the ship it names, and every static whole.
    """
    windows: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for object_id, type_ in connection.execute("SELECT id, type FROM objects"):
        if classify_object_type(type_) == ObjectClass.STATIC:
            windows[object_id].append((-_ALWAYS, _ALWAYS))
    landings = connection.execute(
        "SELECT object_id, carrier_object_id, touchdown_time FROM landings "
        "WHERE touchdown_time IS NOT NULL"
    ).fetchall()
    for object_id, carrier_object_id, touchdown in landings:
        # The stored touchdown stands in for the first contact too; the
        # retention slack covers a bounce sequence many times over.
        span = retention_window(touchdown, touchdown, detection)
        windows[object_id].append(span)
        if carrier_object_id is not None:
            windows[carrier_object_id].append(span)
    merged: dict[int, list[tuple[float, float]]] = {}
    for object_id, spans in windows.items():
        spans.sort()
        out: list[tuple[float, float]] = []
        for start, end in spans:
            if out and start <= out[-1][1]:
                out[-1] = (out[-1][0], max(out[-1][1], end))
            else:
                out.append((start, end))
        merged[object_id] = out
    return merged


def _schema(connection: sqlite3.Connection) -> tuple[list[tuple[str, str]], list[str]]:
    """(table name, CREATE TABLE) pairs, and the CREATE INDEX / CREATE TRIGGER
    statements to run once the data is in -- all verbatim."""
    rows = connection.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
    ).fetchall()
    tables = [(name, sql) for type_, name, sql in rows if type_ == "table"]
    after_copy = [sql for type_, _name, sql in rows if type_ in ("index", "trigger")]
    others = [name for type_, name, _sql in rows if type_ not in ("table", "index", "trigger")]
    if others:
        raise CompactError(f"unexpected schema objects (views?): {others}")
    return tables, after_copy


def compact(source: Path, *, swap: bool, out=sys.stdout) -> Path:
    started = time.monotonic()
    if not source.is_file():
        raise CompactError(f"{source} does not exist")
    target = source.with_name(source.name + ".compact")
    for leftover in (target, Path(f"{target}-wal"), Path(f"{target}-shm")):
        leftover.unlink(missing_ok=True)

    detection = _detection_config()
    src = sqlite3.connect(source, isolation_level=None)
    try:
        busy, _log, _done = src.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if busy:
            raise CompactError("the database is in use: stop the application first")
        tables, after_copy = _schema(src)
        names = [name for name, _ in tables]
        if "tracks" not in names or "landings" not in names:
            raise CompactError(f"{source} is not a DCS Landing Teacher database")

        dest = sqlite3.connect(target, isolation_level=None)
        for _name, sql in tables:
            dest.execute(sql)
        dest.close()

        windows = keep_windows(src, detection)
        src.execute("ATTACH DATABASE ? AS compact", (str(target),))
        src.execute("CREATE TEMP TABLE keep_windows (object_id INTEGER, start REAL, finish REAL)")
        src.executemany(
            "INSERT INTO temp.keep_windows VALUES (?, ?, ?)",
            [(oid, start, end) for oid, spans in windows.items() for start, end in spans],
        )
        src.execute("CREATE INDEX temp.ix_keep ON keep_windows (object_id, start)")

        # Hold the write lock for the whole copy: nothing may change the
        # source between the counts below and the copy they verify.
        src.execute("BEGIN IMMEDIATE")
        before = {name: src.execute(f'SELECT COUNT(*) FROM main."{name}"').fetchone()[0]
                  for name in names}
        for name in names:
            if name == "tracks":
                continue
            src.execute(f'INSERT INTO compact."{name}" SELECT * FROM main."{name}"')
        print(f"tracks: scanning {before['tracks']:,} rows ...", file=out, flush=True)
        src.execute(
            'INSERT INTO compact."tracks" SELECT t.* FROM main."tracks" AS t NOT INDEXED '
            "WHERE EXISTS (SELECT 1 FROM temp.keep_windows AS k "
            "WHERE k.object_id = t.object_id AND t.mission_time BETWEEN k.start AND k.finish)"
        )
        src.execute("COMMIT")
        src.execute("DETACH DATABASE compact")
    finally:
        src.close()

    dest = sqlite3.connect(target, isolation_level=None)
    try:
        for sql in after_copy:
            dest.execute(sql)
        after = {name: dest.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                 for name in names}
        problems = [
            f"{name}: {before[name]} -> {after[name]}"
            for name in names
            if name != "tracks" and before[name] != after[name]
        ]
        integrity = dest.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            problems.append(f"integrity_check: {integrity}")
        # Reported, not fatal: the copy is row-for-row, so any orphan was
        # already in the original (checking that one would scan all of it).
        orphans = dest.execute("PRAGMA foreign_key_check").fetchall()
        if orphans:
            print(
                f"note: {len(orphans)} rows reference a missing parent, as in the "
                f"original (e.g. {orphans[:3]})",
                file=out,
            )
        dest.execute("PRAGMA journal_mode=WAL")
    finally:
        dest.close()
    if problems:
        raise CompactError("verification failed, nothing swapped: " + "; ".join(problems))

    source_mb = os.path.getsize(source) / 1e6
    target_mb = os.path.getsize(target) / 1e6
    print(
        f"tracks {before['tracks']:,} -> {after['tracks']:,} rows; "
        f"file {source_mb:,.1f} MB -> {target_mb:,.1f} MB; "
        f"other tables copied whole ({', '.join(n for n in names if n != 'tracks')}); "
        f"{time.monotonic() - started:.0f} s",
        file=out,
    )
    if not swap:
        print(f"built {target} (original untouched; --swap to put it in place)", file=out)
        return target

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    kept = source.with_name(f"{source.name}.pre-compact-{stamp}")
    # The checkpoint emptied the WAL; its files still belong to the old
    # database and must not be picked up by the new one under the same name.
    for suffix in ("", "-wal", "-shm"):
        old = Path(f"{source}{suffix}")
        if old.exists():
            old.rename(Path(f"{kept}{suffix}"))
    target.rename(source)
    print(f"swapped: {source} is the compact copy; the original is {kept}", file=out)
    return source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("database", type=Path, help="path of the SQLite database file")
    parser.add_argument(
        "--swap",
        action="store_true",
        help="replace the database with the compact copy (the original is kept, renamed)",
    )
    args = parser.parse_args(argv)
    try:
        compact(args.database, swap=args.swap)
    except CompactError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
