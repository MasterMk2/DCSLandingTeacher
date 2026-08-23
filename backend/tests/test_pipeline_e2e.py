"""End-to-end pipeline test: ACMI text -> ingest -> detect -> grade -> DB."""

from __future__ import annotations

from sqlalchemy import select

from app.grading.config import GradingConfig
from app.ingest import TrackIngestor
from app.models.entities import Landing
from app.pipeline import LandingPipeline
from tests.helpers import make_acmi_text, make_approach_samples


async def _run(session_factory, samples_text: str) -> list[Landing]:
    pipeline = LandingPipeline(session_factory, GradingConfig({}))
    ingestor = TrackIngestor(
        session_factory,
        landing_listener=pipeline.handle_landing,
        sample_buffer_s=600.0,
    )
    try:
        for line in samples_text.splitlines():
            await ingestor.handle_line(line)
    finally:
        await ingestor.close()

    async with session_factory() as session:
        result = await session.execute(select(Landing).order_by(Landing.id))
        return list(result.scalars().all())


async def test_carrier_arrestment_flows_through_pipeline(session_factory) -> None:
    text = make_acmi_text(make_approach_samples(outcome="full_stop"))
    landings = await _run(session_factory, text)

    assert len(landings) == 1
    landing = landings[0]
    assert landing.kind == "carrier"
    assert landing.outcome == "full_stop"
    assert landing.grade in ("OK", "OK-", "(OK)", "_NO_GRADE_", "CUT")
    assert landing.venue_name == "CV-59"
    assert landing.carrier_object_id is not None
    assert landing.touchdown_time is not None
    # Raw approach track stored for re-evaluation (FR-7).
    assert landing.approach_track is not None
    stored = landing.approach_track
    assert stored["kind"] == "carrier"
    assert len(stored["samples"]) > 40
    assert landing.factors is not None
    assert landing.grading_version


async def test_land_landing_flows_through_pipeline(session_factory) -> None:
    text = make_acmi_text(
        make_approach_samples(outcome="full_stop", pre_touchdown_descent_ms=1.2),
        include_carrier=False,
    )
    landings = await _run(session_factory, text)

    assert len(landings) == 1
    landing = landings[0]
    assert landing.kind == "land"
    assert landing.outcome == "full_stop"
    # Land graders always produce a letter grade and a score.
    assert landing.grade in ("A", "B", "C", "D", "E")
    assert landing.score is not None
    assert landing.comment


async def test_bolter_flows_through_pipeline(session_factory) -> None:
    text = make_acmi_text(make_approach_samples(outcome="touch_and_go"))
    landings = await _run(session_factory, text)

    assert len(landings) == 1
    landing = landings[0]
    assert landing.kind == "carrier"
    assert landing.outcome == "bolter"
    assert landing.grade == "_NO_GRADE_"


async def test_no_landing_without_ground_contact(session_factory) -> None:
    samples = [s for s in make_approach_samples() if s.time < 0]
    text = make_acmi_text(samples)
    landings = await _run(session_factory, text)
    assert landings == []
