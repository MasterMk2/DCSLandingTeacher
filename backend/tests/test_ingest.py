"""Tests for the minimal ingest pipeline (parser events -> DB)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from app.ingest import TrackIngestor
from app.models.entities import DcsObject, Flight, Track

FIXTURES = Path(__file__).parent / "fixtures"


async def feed_sample(ingestor: TrackIngestor) -> None:
    for line in (FIXTURES / "sample.acmi").read_text(encoding="utf-8").splitlines():
        await ingestor.handle_line(line)


async def test_ingest_persists_flight_objects_tracks(session_factory) -> None:
    ingestor = TrackIngestor(session_factory)
    await feed_sample(ingestor)

    async with session_factory() as session:
        flights = (await session.execute(select(Flight))).scalars().all()
        objects = (
            await session.execute(select(DcsObject).order_by(DcsObject.acmi_id))
        ).scalars().all()
        tracks = (
            await session.execute(select(Track).order_by(Track.mission_time))
        ).scalars().all()

    # One flight created from global-object metadata.
    assert len(flights) == 1
    assert flights[0].reference_time == "2011-06-02T05:00:00Z"
    assert flights[0].data_source == "DCS 2.9.4"

    # Two real objects; the carrier was removed by the stream.
    assert [o.acmi_id for o in objects] == ["101", "102"]
    aircraft = objects[0]
    assert aircraft.type == "Air+FixedWing"
    assert aircraft.name == "C172"
    assert aircraft.pilot == "Viggen"
    assert aircraft.removed is False
    carrier = objects[1]
    assert carrier.type == "Sea+Watercraft+AircraftCarrier"
    assert carrier.removed is True

    # Four track samples total: three for the aircraft (t=0, 47.13, 55.75)
    # plus one for the carrier at t=0.
    assert len(tracks) == 4
    aircraft_tracks = [t for t in tracks if t.object_id == aircraft.id]
    assert len(aircraft_tracks) == 3
    first, last = aircraft_tracks[0], aircraft_tracks[-1]
    assert first.mission_time == pytest.approx(0.0)
    assert first.latitude == pytest.approx(41.5910417)
    assert last.mission_time == pytest.approx(55.75)
    assert last.longitude == pytest.approx(41.63)
    assert last.altitude == pytest.approx(1999.50)


async def test_ingest_ignores_unparsable_lines(session_factory) -> None:
    ingestor = TrackIngestor(session_factory)
    await ingestor.handle_line("#not-a-number")  # must not raise
    await ingestor.handle_line("FileType=text/acmi/tacview")
    await ingestor.handle_line("101,T=41.6|41.5|100")

    async with session_factory() as session:
        tracks = (await session.execute(select(Track))).scalars().all()
    assert len(tracks) == 1
