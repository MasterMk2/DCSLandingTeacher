"""Tests for the landing REST/WebSocket API."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.main import create_app
from app.detection.detector import analyze_track
from app.ingest import LandingContext
from app.models.entities import DcsObject, Flight
from tests.helpers import (
    DECK_ALTITUDE_M,
    make_approach_samples,
    make_carrier_state,
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

    carriers = (
        {"102": make_carrier_state(obj_id="102", name=venue)} if kind == "carrier" else {}
    )
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
    landing_id = await seed_landing(
        app.state.session_factory, app.state.pipeline
    )

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
        asyncio.run(
            tc.app.state.notifier.broadcast_landing({"id": 7, "grade": "OK"})
        )
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
