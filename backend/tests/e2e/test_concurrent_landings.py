"""End-to-end coverage for separating interleaved aircraft landings."""

import math

import pytest
from sqlalchemy import select

from app.grading.config import GradingConfig
from app.ingest import TrackIngestor
from app.models.entities import DcsObject, Landing
from app.pipeline import LandingPipeline
from tests.helpers import LAT0, M_PER_DEG_LAT, make_acmi_text_multi, make_approach_samples


async def test_interleaved_stream_yields_one_record_per_aircraft(session_factory) -> None:
    pipeline = LandingPipeline(session_factory, GradingConfig({}))
    ingestor = TrackIngestor(
        session_factory,
        landing_listener=pipeline.handle_landing,
        landing_finalize_listener=pipeline.finalize_landing,
        sample_buffer_s=600.0,
    )
    text = make_acmi_text_multi(
        [
            {
                "obj_id": "101",
                "samples": make_approach_samples(),
                "name": "F/A-18C",
                "pilot": "Alpha",
            },
            {
                "obj_id": "201",
                "samples": make_approach_samples(offset_east_m=400.0),
                "name": "Su-33",
                "pilot": "Bravo",
            },
        ]
    )
    try:
        for line in text.splitlines():
            await ingestor.handle_line(line)
    finally:
        await ingestor.close()

    async with session_factory() as session:
        landings = (
            (await session.execute(select(Landing).order_by(Landing.touchdown_time)))
            .scalars()
            .all()
        )
        objects = {
            obj.id: obj for obj in (await session.execute(select(DcsObject))).scalars().all()
        }

    assert len(landings) == 2
    pilots = set()
    for landing in landings:
        pilots.add(objects[landing.object_id].pilot)
        assert landing.kind == "carrier"
        assert landing.venue_name == "CV-59"
        assert landing.carrier_object_id is not None
        assert landing.grade in ("OK", "OK-", "(OK)", "_NO_GRADE_", "CUT")
    assert pilots == {"Alpha", "Bravo"}

    lons = sorted(landing.longitude for landing in landings)
    east_m = (lons[1] - lons[0]) * M_PER_DEG_LAT * math.cos(math.radians(LAT0))
    assert east_m == pytest.approx(400.0, abs=50.0)
