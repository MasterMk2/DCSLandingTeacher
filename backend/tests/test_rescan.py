"""Rescanning old raw tracks for landings the database never stored.

The history is recorded the way the old ingestor wrote it -- every sample,
through ``TrackIngestor.handle_line`` with ``keep_all_tracks`` -- and the
rescan runs over it with the production pipeline, as it will once before
the database is compacted.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import delete, func, select

from app.grading.config import load_grading_config
from app.ingest import TrackIngestor
from app.models.entities import DcsObject, Flight, Landing
from app.pipeline import LandingPipeline
from app.rescan import FlightNotFound, rescan_flight
from tests.case1 import fly_case1
from tests.conftest import GRADING_YAML
from tests.helpers import make_acmi_text, make_approach_samples
from tests.test_track_retention import CARRIERS_YAML, ingest, landings, pipeline_for


async def flight_id(session_factory) -> int:
    async with session_factory() as session:
        return (await session.execute(select(Flight.id))).scalar_one()


async def test_a_landing_that_was_never_stored_is_found_and_stored(session_factory) -> None:
    lines = make_acmi_text(
        make_approach_samples(duration_before_s=900.0, ground_time_s=60.0), include_carrier=False
    ).splitlines()
    await ingest(session_factory, lines, keep_all=True)
    (original,) = await landings(session_factory)
    async with session_factory() as session:
        await session.execute(delete(Landing))
        await session.commit()
    flight = await flight_id(session_factory)
    pipeline = pipeline_for(session_factory)

    dry = await rescan_flight(session_factory, pipeline, flight, apply=False)

    assert dry.aircraft_scanned == 1
    assert [(f.kind, f.outcome) for f in dry.landings] == [("land", "full_stop")]
    (found,) = dry.new_landings
    assert found.touchdown_time == original.touchdown_time
    assert found.created_landing_id is None
    assert await landings(session_factory) == []  # a dry run writes nothing

    applied = await rescan_flight(session_factory, pipeline, flight, apply=True)

    (created,) = await landings(session_factory)
    assert applied.landings[0].created_landing_id == created.id
    # The same landing, graded the same way live ingest graded it.
    assert (created.kind, created.outcome) == (original.kind, original.outcome)
    assert created.touchdown_time == original.touchdown_time
    assert created.grade == original.grade
    assert created.score == original.score
    assert created.airframe == "F/A-18C" and created.pilot == "Viggen"
    assert created.outcome_status == "final"
    # Dated when it happened, not when it was found: the flight row was
    # created at the first sample (t=100), the touchdown is 900 s later.
    async with session_factory() as session:
        flight_row = await session.get(Flight, flight)
    assert created.created_at == flight_row.created_at + timedelta(seconds=900.0)

    again = await rescan_flight(session_factory, pipeline, flight, apply=True)

    assert again.new_landings == []
    assert again.landings[0].existing_landing_id == created.id
    assert len(await landings(session_factory)) == 1


async def test_a_trap_live_ingest_could_not_see_is_found(session_factory) -> None:
    """The case this exists for. Until 2026-09-06 the deck height was never
    known to the detector, so a jet standing on CVN_73 read 22 m above the
    sea and no trap was ever stored. The raw track still holds it."""
    blind = LandingPipeline(session_factory, load_grading_config(GRADING_YAML))
    ingestor = TrackIngestor(
        session_factory,
        landing_listener=blind.handle_landing,
        landing_finalize_listener=blind.finalize_landing,
        detection_config=blind._config.to_detection_config(),  # noqa: SLF001
        deck_altitude_for=blind.deck_altitude_for,
        keep_all_tracks=True,
    )
    for line in fly_case1().lines:
        await ingestor.handle_line(line)
    await ingestor.close()
    assert await landings(session_factory) == []

    pipeline = pipeline_for(session_factory)  # with carriers.yaml
    report = await rescan_flight(
        session_factory, pipeline, await flight_id(session_factory), apply=True
    )

    (found,) = report.landings
    assert (found.kind, found.carrier_name) == ("carrier", "CVN_73")
    (trap,) = await landings(session_factory)
    async with session_factory() as session:
        ship = (
            await session.execute(select(DcsObject.id).where(DcsObject.acmi_id == "C1"))
        ).scalar_one()
    assert trap.carrier_object_id == ship
    assert trap.metrics["deck_frame"] == "moving_deck"
    assert trap.metrics["pattern_entry"] == "initial"

    # Stored exactly as a rebuild cuts it: rebuilding it changes nothing.
    await pipeline.rebuild(trap)
    (rebuilt,) = await landings(session_factory)
    assert rebuilt.touchdown_time == trap.touchdown_time
    assert rebuilt.grade == trap.grade
    assert rebuilt.approach_track == trap.approach_track


async def test_stored_rows_it_cannot_find_are_reported_not_touched(session_factory) -> None:
    lines = make_acmi_text(
        make_approach_samples(duration_before_s=300.0, ground_time_s=30.0), include_carrier=False
    ).splitlines()
    await ingest(session_factory, lines, keep_all=True)
    (real,) = await landings(session_factory)
    # An old row at an instant the jet was flying at a few hundred metres.
    async with session_factory() as session:
        junk = Landing(
            flight_id=real.flight_id,
            object_id=real.object_id,
            source_id=real.source_id,
            kind="carrier",
            outcome="full_stop",
            touchdown_time=real.touchdown_time - 200.0,
            grade="CUT",
        )
        session.add(junk)
        await session.commit()
        junk_id = junk.id

    report = await rescan_flight(
        session_factory, pipeline_for(session_factory), real.flight_id, apply=True
    )

    assert report.new_landings == []
    assert report.landings[0].existing_landing_id == real.id
    assert report.stored_not_redetected == [junk_id]
    async with session_factory() as session:
        assert (await session.get(Landing, junk_id)).grade == "CUT"
        assert (await session.execute(select(func.count()).select_from(Landing))).scalar_one() == 2


async def test_an_unknown_flight(session_factory) -> None:
    with pytest.raises(FlightNotFound):
        await rescan_flight(session_factory, pipeline_for(session_factory), 42, apply=False)


async def test_the_flights_and_rescan_endpoints(tmp_path) -> None:
    from app.api.main import create_app
    from app.config import Settings

    app = create_app(
        Settings(
            database_url=f"sqlite+aiosqlite:///{(tmp_path / 'api.db').as_posix()}",
            acmi_enabled=False,
            grading_config_path=str(GRADING_YAML),
            carriers_config_path=str(CARRIERS_YAML),
        )
    )
    lines = make_acmi_text(
        make_approach_samples(duration_before_s=300.0, ground_time_s=30.0), include_carrier=False
    ).splitlines()
    async with app.router.lifespan_context(app):
        await ingest(app.state.session_factory, lines, keep_all=True)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            flights = await http.get("/api/v1/flights")
            (flight,) = flights.json()
            dry = await http.post(f"/api/v1/flights/{flight['id']}/rescan")
            missing = await http.post("/api/v1/flights/999/rescan")

    assert flights.status_code == 200
    assert flight["landings"] == 1
    assert dry.status_code == 200, dry.text
    body = dry.json()
    assert body["applied"] is False
    assert body["new_landings"] == 0
    assert body["landings"][0]["existing_landing_id"] is not None
    assert body["samples_scanned"] > 300
    assert missing.status_code == 404


async def test_two_applied_rescans_of_one_flight_store_a_landing_once(tmp_path) -> None:
    """A client that timed out and retried while the first rescan was still
    running: each decides "already stored?" from what it read at the start,
    so run side by side they would both store the landing."""
    import asyncio

    from app.api.main import create_app
    from app.config import Settings

    app = create_app(
        Settings(
            database_url=f"sqlite+aiosqlite:///{(tmp_path / 'api.db').as_posix()}",
            acmi_enabled=False,
            grading_config_path=str(GRADING_YAML),
            carriers_config_path=str(CARRIERS_YAML),
        )
    )
    lines = make_acmi_text(
        make_approach_samples(duration_before_s=300.0, ground_time_s=30.0), include_carrier=False
    ).splitlines()
    async with app.router.lifespan_context(app):
        await ingest(app.state.session_factory, lines, keep_all=True)
        async with app.state.session_factory() as session:
            await session.execute(delete(Landing))
            await session.commit()
        flight = await flight_id(app.state.session_factory)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            first, second = await asyncio.gather(
                http.post(f"/api/v1/flights/{flight}/rescan?apply=true"),
                http.post(f"/api/v1/flights/{flight}/rescan?apply=true"),
            )
        stored = await landings(app.state.session_factory)

    assert first.status_code == second.status_code == 200
    assert len(stored) == 1
    assert sorted(r.json()["new_landings"] for r in (first, second)) == [0, 1]
