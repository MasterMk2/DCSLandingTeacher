"""Attitude and derived kinematics have to survive the REAL ingest path.

``ingest.py`` builds the detector's ``TrackSample`` from the parsed ACMI
object; ``build_approach_analysis`` copies it into the stored track. Both
hops are two-line pass-throughs that a test calling ``analyze_track`` or
``grade_land_landing`` directly never exercises -- which is precisely how
earlier changes shipped inert. So this drives ACMI text through
``TrackIngestor.handle_line`` and reads the landing row back.
"""

from __future__ import annotations

import re

from sqlalchemy import select

from app.api.schemas import DeviationSampleOut
from app.grading.config import GradingConfig
from app.grading.deviations import ApproachAnalysis
from app.ingest import TrackIngestor
from app.models.entities import Landing
from app.pipeline import GRADING_VERSION, LandingPipeline
from tests.helpers import make_acmi_text, make_approach_samples


def _with_attitude(
    acmi_text: str, roll: float, pitch: float, yaw: float | None = None
) -> str:
    """Fill the empty Roll / Pitch slots of every aircraft transform.

    ``make_acmi_text`` writes ``T=lon|lat|alt|||heading``; the two empty
    slots are exactly Roll and Pitch in ACMI 2.2's 6-component transform,
    and the last one is Yaw -- replaced too when ``yaw`` is given.
    """
    return re.sub(
        r"(T=[^|,]+\|[^|,]+\|[^|,]+)\|\|\|([^|,\s]*)",
        lambda m: f"{m.group(1)}|{roll:g}|{pitch:g}|"
        + (f"{yaw:g}" if yaw is not None else m.group(2)),
        acmi_text,
    )


async def _store_one_landing(session_factory, text: str) -> Landing:
    pipeline = LandingPipeline(session_factory, GradingConfig({}))
    ingestor = TrackIngestor(
        session_factory,
        landing_listener=pipeline.handle_landing,
        landing_finalize_listener=pipeline.finalize_landing,
        sample_buffer_s=600.0,
    )
    try:
        for line in text.splitlines():
            await ingestor.handle_line(line)
    finally:
        await ingestor.close()

    async with session_factory() as session:
        return (await session.execute(select(Landing))).scalars().one()


async def test_attitude_and_load_factor_reach_the_stored_track(session_factory) -> None:
    text = _with_attitude(
        make_acmi_text(
            make_approach_samples(outcome="full_stop", pre_touchdown_descent_ms=1.2),
            include_carrier=False,
        ),
        roll=-3.5,
        pitch=2.25,
    )
    assert "|-3.5|2.25|" in text

    landing = await _store_one_landing(session_factory, text)

    assert landing.grading_version == GRADING_VERSION
    samples = landing.approach_track["samples"]
    inbound = [s for s in samples if s["time"] < landing.touchdown_time]
    assert inbound
    # The recorded attitude is stored as recorded...
    assert all(s["roll"] == -3.5 and s["pitch"] == 2.25 for s in inbound)
    # ...and the derived series is present even though this fixture is
    # sampled at 1 Hz (the fit widens its window). A straight-in at constant
    # speed down a 3.5 deg slope is a 1 G, zero-turn-rate track -- up to the
    # flare, where the fixture breaks the descent rate to 1.2 m/s and a real
    # pull shows up.
    loads = [s["load_factor"] for s in inbound if s["load_factor"] is not None]
    assert len(loads) >= len(inbound) - 6
    steady = [
        s["load_factor"]
        for s in inbound
        if s["load_factor"] is not None and s["time"] < landing.touchdown_time - 8.0
    ]
    assert steady and all(abs(n - 1.0) < 0.05 for n in steady)
    rates = [s["turn_rate_deg_s"] for s in inbound if s["turn_rate_deg_s"] is not None]
    assert rates and all(abs(r) < 0.5 for r in rates)


async def test_recorded_heading_survives_storage_and_regrade(session_factory) -> None:
    """The 3D view draws the nose from ``heading``, not from the track.

    A crab of a few degrees is exactly what the track direction cannot
    show, so the fixture flies due north (its ground track) with the nose
    held 4.5 deg right: the stored value must be the recorded Yaw, and it
    must come back out of a re-grade, which rebuilds the samples from JSON.
    """
    text = _with_attitude(
        make_acmi_text(make_approach_samples(outcome="full_stop"), include_carrier=False),
        roll=0.0,
        pitch=1.0,
        yaw=4.5,
    )
    assert "|0|1|4.5" in text

    landing = await _store_one_landing(session_factory, text)

    inbound = [
        s for s in landing.approach_track["samples"] if s["time"] < landing.touchdown_time
    ]
    assert inbound and all(s["heading"] == 4.5 for s in inbound)

    restored = ApproachAnalysis.from_dict(landing.approach_track)
    assert [s.heading for s in restored.samples] == [
        s["heading"] for s in landing.approach_track["samples"]
    ]
    served = DeviationSampleOut(**restored.samples[0].as_dict())
    assert served.heading == restored.samples[0].heading
