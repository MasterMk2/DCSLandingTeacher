"""Separate circuits after a low pass even when WOW never becomes true."""

from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.detection.detector import TrackSample, analyze_track
from app.grading.config import GradingConfig
from app.ingest import TrackIngestor
from app.models.entities import Landing
from app.pipeline import LandingPipeline
from tests.helpers import DECK_ALTITUDE_M, LAT0, LON0, make_carrier_state
from tests.e2e.test_provisional_flow import RecordingNotifier


def _low_pass_then_landing() -> list[TrackSample]:
    """A 5.8 m pass, climb-out, circuit, then a real touchdown at t=200."""
    samples: list[TrackSample] = []
    for t in range(-60, 226):
        if t <= 0:
            height = 5.8 - t * 4.0
        elif t <= 20:
            height = 5.8 + t * 5.0
        elif t < 175:
            height = 105.8
        else:
            height = max(0.0, (200 - t) * 4.0)
        samples.append(TrackSample(
            time=float(t), latitude=LAT0, longitude=LON0,
            altitude=DECK_ALTITUDE_M + height, agl=height,
            speed=70.0, heading=0.0, on_ground=None,
        ))
    return samples


@pytest.mark.parametrize("carrier", [False, True])
def test_low_pass_climb_out_starts_a_new_circuit(carrier: bool) -> None:
    samples = _low_pass_then_landing()
    carriers = {}
    if carrier:
        carriers = {"C1": make_carrier_state(altitude=0.0)}
        # Tacview AGL over water measures to the sea, including deck height.
        samples = [replace(s, agl=s.altitude) for s in samples]
    offline = analyze_track(
        samples, ground_altitude_m=DECK_ALTITUDE_M, carriers=carriers,
        deck_altitude_for=lambda _: DECK_ALTITUDE_M,
    )
    live = analyze_track(
        [s for s in samples if s.time <= 200], current_time=200,
        ground_altitude_m=DECK_ALTITUDE_M, carriers=carriers,
        deck_altitude_for=lambda _: DECK_ALTITUDE_M,
    )

    assert len(offline) == len(live) == 1  # A low pass is not a touchdown.
    for event in (offline[0], live[0]):
        assert event.first_contact_time == event.touchdown.time == 200
        assert event.approach[0].time == 2  # First sample above 15 m on climb-out.
        assert any(s.time == 20 for s in event.approach)  # Keep the departure.
        assert any(s.time == 175 for s in event.approach)  # Keep this final.
    assert not live[0].finalized
    assert offline[0].finalized


@pytest.mark.parametrize("heights", [
    [6.0, 8.0, 10.0, 8.0, 6.0],
    [6.0, 6.5, 7.0, 6.5, 6.0],
    [14.0, 14.8, 15.6, 16.4, 15.6, 14.8, 14.0],
    [6.0, 10.0, 14.0, 18.0, 16.0, 12.0, 8.0],
])
def test_small_low_altitude_recovery_does_not_cut_the_final(heights: list[float]) -> None:
    samples = [
        TrackSample(time=float(t), latitude=LAT0, longitude=LON0,
                    altitude=h, agl=h, on_ground=None)
        for t, h in enumerate([50.0, 45.0, 40.0, 35.0, 30.0, 25.0, 20.0, 15.0,
                               10.0, *heights, 4.0, 0.0, 0.0])
    ]
    event, = analyze_track(samples, ground_altitude_m=0.0)
    assert event.approach[0].time == 0.0


def test_carrier_surface_change_without_a_climb_does_not_cut_the_approach() -> None:
    samples: list[TrackSample] = []
    for sample in _low_pass_then_landing():
        if 0 <= sample.time < 175:
            height = 5.8
        elif sample.time >= 175:
            height = max(0.0, 5.8 * (200 - sample.time) / 25)
        else:
            height = sample.agl
            assert height is not None
        altitude = DECK_ALTITUDE_M + height
        # Leaving the carrier's proximity changes deck-relative height to
        # sea AGL, crossing 15 m with no actual climb. Return for the final.
        latitude = LAT0 + 0.02 if 1 <= sample.time < 150 else LAT0
        samples.append(replace(sample, latitude=latitude, altitude=altitude, agl=altitude))
    event, = analyze_track(
        samples, ground_altitude_m=0.0,
        carriers={"C1": make_carrier_state(altitude=0.0)},
        deck_altitude_for=lambda _: DECK_ALTITUDE_M,
    )
    assert event.kind == "carrier"
    assert event.approach[0].time == -60.0


def test_missing_height_does_not_invent_a_circuit_boundary() -> None:
    samples = [
        replace(s, agl=None, altitude=None, on_ground=s.time >= 200)
        for s in _low_pass_then_landing()
    ]
    event, = analyze_track(samples, ground_altitude_m=None)
    assert event.first_contact_time == 200
    assert event.approach[0].time == -60.0


def test_recent_recovery_does_not_qualify_an_older_slow_climb() -> None:
    heights = [10.0] * 41 + [10.0 + t * 0.5 for t in range(1, 61)]
    heights += [35.0, 30.0, 25.0, 20.0, 15.0, 10.0, 5.0, 9.0,
                13.0, 17.0, 16.0, 12.0, 8.0, 4.0, 0.0, 0.0]
    samples = [
        TrackSample(time=float(t), latitude=LAT0, longitude=LON0,
                    altitude=height, agl=height, on_ground=None)
        for t, height in enumerate(heights)
    ]
    event, = analyze_track(samples, ground_altitude_m=0.0)
    assert event.first_contact_time == 115.0
    assert event.approach[0].time == 0.0


async def test_live_low_pass_then_touch_and_go_keeps_one_separate_record(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    notifier = RecordingNotifier()
    pipeline = LandingPipeline(session_factory, GradingConfig({}), notifier=notifier)
    ingestor = TrackIngestor(
        session_factory, landing_listener=pipeline.handle_landing,
        landing_finalize_listener=pipeline.finalize_landing, sample_buffer_s=600.0,
    )
    try:
        for line in ("FileType=text/acmi/tacview", "FileVersion=2.2",
                     "0,ReferenceTime=2024-01-01T00:00:00Z"):
            await ingestor.handle_line(line)
        for sample in _low_pass_then_landing():
            if sample.time > 203:
                height = (sample.time - 203) * 5.0
                sample = replace(sample, agl=height, altitude=DECK_ALTITUDE_M + height)
            await ingestor.handle_line(f"#{1000 + sample.time:g}")
            identity = ",Type=Air+FixedWing,Name=F/A-18C,Pilot=Test" if sample.time == -60 else ""
            await ingestor.handle_line(
                f"101,T={LON0}|{LAT0}|{sample.altitude}|||0,AGL={sample.agl},TAS=70{identity}"
            )
    finally:
        await ingestor.close()

    async with session_factory() as session:
        rows = list((await session.execute(select(Landing))).scalars())
        assert len(rows) == 1
        row = rows[0]
        assert row.outcome == "touch_and_go"
        assert row.outcome_status == "final"
        assert row.touchdown_time == 1200
        assert row.approach_track["samples"][0]["time"] == 1002
    assert [kind for kind, _ in notifier.messages] == ["landing", "landing_update"]
    assert {payload["id"] for _, payload in notifier.messages} == {row.id}
