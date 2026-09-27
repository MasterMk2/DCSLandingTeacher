"""The one-off compaction of a database recorded before retention existed.

The database is built the way production's was: Alembic migrations, then a
recording through ``TrackIngestor.handle_line`` writing every sample. After
compaction the rebuild of every landing must come out as it did before --
that is the whole contract of what may be thrown away.
"""

from __future__ import annotations

import io
import os
import sqlite3

import pytest

from app.compact import CompactError, compact, main
from app.models.database import create_engine, create_session_factory
from app.models.migrations import run_migrations
from app.retention import retention_window
from tests.case1 import fly_case1
from tests.conftest import GRADING_YAML
from tests.test_track_retention import (
    detection,
    ingest,
    land_lines,
    landings,
    pipeline_for,
    with_transit,
)


@pytest.fixture(autouse=True)
def grading_config(monkeypatch):
    # compact() reads the grading config the way the app does.
    monkeypatch.setenv("DLT_GRADING_CONFIG_PATH", str(GRADING_YAML))


async def recorded(tmp_path, lines_list):
    path = tmp_path / "dlt.db"
    url = f"sqlite+aiosqlite:///{path.as_posix()}"
    await run_migrations(url)
    engine = create_engine(url)
    session_factory = create_session_factory(engine)
    for lines in lines_list:
        await ingest(session_factory, lines, keep_all=True)
    return path, engine, session_factory


def track_rows(path) -> dict[str, list[tuple]]:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT o.acmi_id, t.mission_time, t.latitude, t.longitude, t.altitude "
            "FROM tracks t JOIN objects o ON o.id = t.object_id ORDER BY t.mission_time, t.id"
        ).fetchall()
    finally:
        connection.close()
    out: dict[str, list[tuple]] = {}
    for acmi_id, *rest in rows:
        out.setdefault(acmi_id, []).append(tuple(rest))
    return out


def table_rows(path, table: str) -> list[tuple]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
    finally:
        connection.close()


async def test_compaction_keeps_what_the_rebuild_reads(tmp_path) -> None:
    path, engine, session_factory = await recorded(
        tmp_path, [land_lines(), with_transit(fly_case1().lines)]
    )
    before_rows = track_rows(path)
    before_landings = await landings(session_factory)
    other_tables = {t: table_rows(path, t) for t in ("flights", "objects", "landings")}
    await engine.dispose()

    out = io.StringIO()
    built = compact(path, swap=False, out=out)

    assert built.name == "dlt.db.compact"
    assert os.path.exists(path)  # untouched without --swap
    assert track_rows(path) == before_rows

    compact(path, swap=True, out=out)

    kept_away = [p for p in os.listdir(tmp_path) if p.startswith("dlt.db.pre-compact-")]
    assert len(kept_away) == 1  # the original, renamed, not deleted
    after_rows = track_rows(path)
    for table, rows in other_tables.items():
        assert table_rows(path, table) == rows, table
    windows: dict[str, list[tuple[float, float]]] = {}
    for landing, acmi_id in zip(before_landings, ("101", "A1")):
        windows.setdefault(acmi_id, []).append(
            retention_window(landing.touchdown_time, landing.touchdown_time, detection())
        )
    windows["C1"] = windows["A1"]  # the ship of the flight with the trap
    # The schema came across whole, trigger included (app.models.entities).
    connection = sqlite3.connect(path)
    try:
        triggers = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
    finally:
        connection.close()
    assert triggers == [("landings_delete_track",)]
    for acmi_id, rows in before_rows.items():
        spans = windows.get(acmi_id)
        if acmi_id == "301":
            assert after_rows[acmi_id] == rows  # statics: all of it
        elif spans is None:
            assert acmi_id not in after_rows, acmi_id  # the missile
        else:
            expected = [r for r in rows if any(s <= r[0] <= e for s, e in spans)]
            assert after_rows[acmi_id] == expected, acmi_id
    assert sum(map(len, after_rows.values())) < sum(map(len, before_rows.values())) / 2
    assert "tracks" in out.getvalue()

    # The compacted file is a database the app opens as it is, and every
    # landing rebuilds from it exactly as it was stored.
    url = f"sqlite+aiosqlite:///{path.as_posix()}"
    await run_migrations(url)
    engine = create_engine(url)
    session_factory = create_session_factory(engine)
    try:
        pipeline = pipeline_for(session_factory)
        for landing in await landings(session_factory):
            await pipeline.rebuild(landing)
        after_landings = await landings(session_factory)
    finally:
        await engine.dispose()
    for before, after in zip(before_landings, after_landings, strict=True):
        assert after.touchdown_time == before.touchdown_time
        assert after.grade == before.grade
        assert after.approach_track == before.approach_track


def test_it_refuses_what_is_not_a_landing_database(tmp_path) -> None:
    other = tmp_path / "other.db"
    sqlite3.connect(other).execute("CREATE TABLE things (x)").connection.close()
    with pytest.raises(CompactError):
        compact(other, swap=True, out=io.StringIO())
    assert main([str(tmp_path / "missing.db")]) == 1
