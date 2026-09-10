"""Integration tests for routes defined in :mod:`app.api.routes`."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.main import create_app
from app.detection.detector import analyze_track
from app.ingest import LandingContext
from app.models.entities import DcsObject, Flight, Landing
from tests.conftest import GRADING_YAML
from tests.helpers import (
    DECK_ALTITUDE_M,
    make_api_settings,
    make_approach_samples,
    make_carrier_state,
    open_api_client,
)


async def seed_landing(
    session_factory: async_sessionmaker[AsyncSession],
    pipeline,
    *,
    acmi_id: str = "101",
    kind: str = "carrier",
    outcome: str = "full_stop",
    pilot: str = "Viggen",
    airframe: str = "F/A-18C",
    venue: str | None = "CV-59",
    grade_hint: str | None = None,
) -> int:
    """Insert flight/object rows and run one landing through the pipeline."""
    async with session_factory() as session:
        flight = Flight(reference_time="2024-01-01T00:00:00Z")
        session.add(flight)
        await session.flush()
        flight_id = flight.id
        aircraft = DcsObject(
            flight_id=flight_id,
            acmi_id=acmi_id,
            type="Air+FixedWing",
            name=airframe,
            pilot=pilot,
            first_seen=0.0,
            last_seen=100.0,
        )
        session.add(aircraft)
        if kind == "carrier":
            session.add(
                DcsObject(
                    flight_id=flight_id,
                    acmi_id="102",
                    type="Sea+Watercraft+AircraftCarrier",
                    name=venue,
                    first_seen=0.0,
                    last_seen=100.0,
                )
            )
        await session.commit()

    carriers = {"102": make_carrier_state(obj_id="102", name=venue)} if kind == "carrier" else {}
    samples = make_approach_samples(outcome=outcome)
    events = analyze_track(samples, DECK_ALTITUDE_M, carriers)
    assert len(events) == 1

    context = LandingContext(
        flight_id=flight_id,
        acmi_object_id=acmi_id,
        pilot=pilot,
        airframe=airframe,
        event=events[0],
    )
    landing_id = await pipeline.handle_landing(context)
    assert landing_id is not None
    return landing_id


async def test_list_landings_filters_and_paging(client) -> None:
    http, app = client
    sf = app.state.session_factory
    pipeline = app.state.pipeline

    first = await seed_landing(sf, pipeline, pilot="Alpha")
    await seed_landing(sf, pipeline, acmi_id="201", pilot="Bravo", kind="land")

    response = await http.get("/api/landings")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2

    response = await http.get("/api/landings", params={"player": "Alpha"})
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["pilot"] == "Alpha"

    response = await http.get("/api/landings", params={"kind": "land"})
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["kind"] == "land"

    response = await http.get("/api/landings", params={"limit": 1, "offset": 1})
    body = response.json()
    assert body["total"] == 2
    assert len(body["items"]) == 1

    detail_id = first
    response = await http.get(f"/api/landings/{detail_id}")
    assert response.status_code == 200


async def test_get_landing_detail_includes_approach_track(client) -> None:
    http, app = client
    landing_id = await seed_landing(app.state.session_factory, app.state.pipeline)

    response = await http.get(f"/api/landings/{landing_id}")
    assert response.status_code == 200
    body = response.json()

    assert body["kind"] == "carrier"
    assert body["outcome"] == "full_stop"
    # Offline-seeded landings are final from the start (Issue #5).
    assert body["outcome_status"] == "final"
    assert body["grade"] in ("OK", "OK-", "(OK)", "_NO_GRADE_", "CUT")
    assert body["pilot"] == "Viggen"
    assert body["touchdown"]["latitude"] is not None
    approach = body["approach_track"]
    assert approach is not None
    assert approach["glideslope_deg"] == 3.5
    assert len(approach["samples"]) > 40
    sample = approach["samples"][0]
    assert {"time", "distance_to_go", "glideslope_deviation"} <= set(sample)


async def test_get_landing_404(client) -> None:
    http, _app = client
    response = await http.get("/api/landings/9999")
    assert response.status_code == 404


async def test_regrade_updates_evaluation(client) -> None:
    http, app = client
    sf = app.state.session_factory
    pipeline = app.state.pipeline

    landing_id = await seed_landing(sf, pipeline)

    response = await http.post(f"/api/landings/{landing_id}/regrade")
    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == landing_id
    assert payload["grade"]

    # Tightening the HIGH threshold must flag a pass that was previously OK.
    response = await http.post(
        f"/api/landings/{landing_id}/regrade",
        json={"overrides": {"lso_grading": {"factors": {"HIGH": {"gs_deviation_m": -50.0}}}}},
    )
    assert response.status_code == 200
    names = [f["name"] for f in response.json()["factors"]]
    assert "HIGH" in names


async def test_regrade_without_track_conflicts(client) -> None:
    http, app = client
    # Seed via raw SQL-free path: an empty DB has no landings at all.
    response = await http.post("/api/landings/9999/regrade")
    assert response.status_code == 404


def test_websocket_receives_new_landing_events(settings) -> None:
    app = create_app(settings)
    with TestClient(app) as tc:
        # Events broadcast before connecting are replayed on connect.
        asyncio.run(tc.app.state.notifier.broadcast_landing({"id": 7, "grade": "OK"}))
        with tc.websocket_connect("/api/ws/landings") as ws:
            message = ws.receive_json()
            assert message["type"] == "landing"
            assert message["landing"]["id"] == 7


def test_websocket_receives_landing_update_messages(settings) -> None:
    """Provisional -> final confirmations arrive as ``landing_update``."""
    app = create_app(settings)
    with TestClient(app) as tc:
        asyncio.run(
            tc.app.state.notifier.broadcast_landing(
                {"id": 8, "grade": "OK", "outcome_status": "final"},
                message_type="landing_update",
            )
        )
        with tc.websocket_connect("/api/ws/landings") as ws:
            message = ws.receive_json()
            assert message["type"] == "landing_update"
            assert message["landing"]["id"] == 8
            assert message["landing"]["outcome_status"] == "final"


async def test_list_landings_sorts_by_the_requested_column(client) -> None:
    """The history list can be reordered from the column headers.

    Sorting by "time" means ``created_at``, not ``touchdown_time``: the
    latter is mission-elapsed seconds and restarts every mission, so it
    interleaves days once the history spans more than one.
    """
    http, app = client
    sf = app.state.session_factory
    pipeline = app.state.pipeline

    await seed_landing(sf, pipeline, pilot="Charlie", kind="land")
    await seed_landing(sf, pipeline, acmi_id="202", pilot="Alpha", kind="land")
    await seed_landing(sf, pipeline, acmi_id="203", pilot="Bravo", kind="land")

    body = (await http.get("/api/landings", params={"sort": "pilot", "order": "asc"})).json()
    pilots = [i["pilot"] for i in body["items"]]
    assert pilots == sorted(pilots)

    body = (await http.get("/api/landings", params={"sort": "pilot", "order": "desc"})).json()
    assert [i["pilot"] for i in body["items"]] == sorted(pilots, reverse=True)

    body = (await http.get("/api/landings", params={"sort": "score", "order": "desc"})).json()
    scores = [i["score"] for i in body["items"] if i["score"] is not None]
    assert scores == sorted(scores, reverse=True)

    # An unknown column falls back to the default instead of erroring: a
    # stale bookmark must not break the page.
    assert (await http.get("/api/landings", params={"sort": "nonsense"})).status_code == 200
    assert (await http.get("/api/landings", params={"order": "sideways"})).status_code == 422


async def test_list_landings_filters_by_approach_pattern(client) -> None:
    http, app = client
    sf = app.state.session_factory
    pipeline = app.state.pipeline

    await seed_landing(sf, pipeline, acmi_id="204", kind="land")

    body = (await http.get("/api/landings", params={"pattern": "straight_in"})).json()
    assert all(i["approach_pattern"] == "straight_in" for i in body["items"])
    assert (await http.get("/api/landings", params={"pattern": "sideways"})).status_code == 422


async def test_detail_serves_every_stored_sample_field(client) -> None:
    """The detail endpoint must not quietly drop columns of the track.

    It used to build each sample field by field, so anything added to the
    stored representation afterwards was serialised as null even though it
    was in the database. `signed_distance_to_go` went that way, and the plan
    view -- which needs it to place the break and the upwind leg, everything
    PAST the touchdown point -- fell back to the clamped distance and drew
    142 of landing #54's 515 samples stacked on the threshold line.
    """
    from app.api.schemas import DeviationSampleOut

    http, app = client
    sf = app.state.session_factory
    pipeline = app.state.pipeline
    landing_id = await seed_landing(sf, pipeline, kind="land", acmi_id="301")

    body = (await http.get(f"/api/landings/{landing_id}")).json()
    samples = body["approach_track"]["samples"]
    assert samples

    stored = await _stored_track(sf, landing_id)
    for field in DeviationSampleOut.model_fields:
        served = [s.get(field) for s in samples]
        kept = [s.get(field) for s in stored["samples"]]
        assert len(served) == len(kept)
        if any(v is not None for v in kept):
            assert any(v is not None for v in served), f"{field} dropped by the API"


async def _stored_track(session_factory, landing_id: int) -> dict:
    from app.models.entities import Landing

    async with session_factory() as session:
        landing = await session.get(Landing, landing_id)
        return landing.approach_track


async def test_a_landing_keeps_its_aircraft_when_the_object_row_is_reused(
    client,
) -> None:
    """Tacview reuses object ids, so the ``objects`` row is not the landing's.

    Within one recording DCS hands a freed hex id to the next object, and the
    ingest matches objects on (flight_id, acmi_id) -- so a missile launched
    later overwrites the name and pilot of the very row an earlier landing
    points at. Measured on the production database: 124 landings displayed an
    airframe that disagreed with the one recorded in their own approach
    track, including seven UH-1H landings shown as "AIM_120".

    The aircraft that flew a landing is a fact about that landing, so the
    landing row owns it and the object row is only a fallback for rows
    written before it did.
    """
    from sqlalchemy import select

    from app.models.entities import DcsObject, Landing

    http, app = client
    sf = app.state.session_factory
    landing_id = await seed_landing(
        sf, pipeline=app.state.pipeline, kind="land", pilot="Bunyap", airframe="UH-1H"
    )

    # The id gets reused by an AIM-120 later in the same recording.
    async with sf() as session:
        landing = await session.get(Landing, landing_id)
        obj = (
            await session.execute(select(DcsObject).where(DcsObject.id == landing.object_id))
        ).scalar_one()
        obj.name = "AIM_120"
        obj.type = "Weapon+Missile"
        obj.pilot = None
        await session.commit()

    body = (await http.get(f"/api/landings/{landing_id}")).json()
    assert body["airframe"] == "UH-1H"
    assert body["pilot"] == "Bunyap"

    # ...and the list view, which sorts and filters on the same fields.
    listing = (await http.get("/api/landings", params={"airframe": "UH-1H"})).json()
    assert [item["id"] for item in listing["items"]] == [landing_id]
    assert (await http.get("/api/landings", params={"airframe": "AIM"})).json()["total"] == 0


async def test_the_object_row_still_answers_for_rows_written_before_the_column(
    client,
) -> None:
    """Old landings have no stored identity; they must not go blank."""
    from app.models.entities import Landing

    http, app = client
    sf = app.state.session_factory
    landing_id = await seed_landing(
        sf, pipeline=app.state.pipeline, kind="land", pilot="Wags", airframe="F-16C_50"
    )

    async with sf() as session:
        landing = await session.get(Landing, landing_id)
        landing.pilot = None
        landing.airframe = None
        await session.commit()

    body = (await http.get(f"/api/landings/{landing_id}")).json()
    assert body["airframe"] == "F-16C_50"
    assert body["pilot"] == "Wags"


async def test_regrade_burns_in_a_missing_airframe(client) -> None:
    """Rows the 0008 backfill could not reach must heal on the next regrade.

    The migration could only fill rows whose ``approach_track`` already
    carried an airframe. A row that got its track populated later — by a
    re-grade that resolved the name from the object row — stayed NULL, so it
    kept depending on the mutable row this column exists to stop trusting.
    Two production rows were in exactly that state.
    """
    from app.models.entities import Landing

    http, app = client
    sf = app.state.session_factory
    landing_id = await seed_landing(
        sf, pipeline=app.state.pipeline, kind="land", pilot="Grim", airframe="A-10C_2"
    )

    async with sf() as session:
        landing = await session.get(Landing, landing_id)
        landing.airframe = None
        await session.commit()

    assert (await http.post(f"/api/landings/{landing_id}/regrade")).status_code == 200

    async with sf() as session:
        landing = await session.get(Landing, landing_id)
        assert landing.airframe == "A-10C_2"


async def test_config_reload_endpoint_requires_and_reloads(tmp_path) -> None:
    config_path = tmp_path / "grading.yaml"
    config_path.write_text(Path(GRADING_YAML).read_text(encoding="utf-8"), encoding="utf-8")
    app = create_app(
        make_api_settings(
            tmp_path,
            database_filename="auth.db",
            grading_config_path=str(config_path),
            auth_token="secret",
        )
    )
    async with app.router.lifespan_context(app):
        async with open_api_client(app) as client:
            assert (await client.post("/api/config/reload")).status_code == 401
            response = await client.post("/api/config/reload", headers={"X-Auth-Token": "secret"})

    assert response.status_code == 200
    assert response.json()["reloaded"] is True


async def test_regrade_malformed_track_returns_structured_error(tmp_path) -> None:
    app = create_app(make_api_settings(tmp_path, database_filename="auth.db", auth_token="secret"))
    async with app.router.lifespan_context(app):
        async with app.state.session_factory() as session:
            flight = Flight(source_id="default")
            session.add(flight)
            await session.flush()
            obj = DcsObject(flight_id=flight.id, acmi_id="A1", first_seen=0.0, last_seen=1.0)
            session.add(obj)
            await session.flush()
            landing = Landing(
                flight_id=flight.id,
                object_id=obj.id,
                outcome_status="final",
                approach_track={"samples": "not a list"},
            )
            session.add(landing)
            await session.flush()
            await session.commit()
        async with open_api_client(app) as client:
            response = await client.post(
                f"/api/landings/{landing.id}/regrade", headers={"X-Auth-Token": "secret"}
            )

    assert response.status_code == 422
    assert response.json()["error"] == "MALFORMED_APPROACH_TRACK"
    assert "message" in response.json()
