"""Mission-restart session rotation (ACMI timeline regressions).

A DCS mission restart resets the exporter's frame clock to zero while the
ingestor keeps running. Without rotation, the previous mission's frozen
rolling buffers mix into the new mission's timeline: stale touchdowns were
re-detected as duplicate landing rows, and the new mission's touchdown
event absorbed the old one as a "bounce" (rows overwritten with a foreign
touchdown time). These tests pin the rotation behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.ingest import TrackIngestor
from app.models.entities import Flight

REF_LAT = 35.0
REF_LON = 140.0
DECK_ALT = 20.0


@dataclass
class Reported:
    """One landing listener call."""

    first_contact: float
    touchdown: float
    outcome: str


@dataclass
class Recorder:
    provisional: list[Reported] = field(default_factory=list)
    finalized: list[tuple[int, Reported]] = field(default_factory=list)
    next_id: int = 1

    async def on_landing(self, context) -> int | None:  # noqa: ANN001
        event = context.event
        self.provisional.append(
            Reported(
                first_contact=event.first_contact_time,
                touchdown=event.touchdown.time,
                outcome=event.outcome,
            )
        )
        row_id = self.next_id
        self.next_id += 1
        return row_id

    async def on_finalize(self, landing_id: int, context) -> None:  # noqa: ANN001
        event = context.event
        self.finalized.append(
            (
                landing_id,
                Reported(
                    first_contact=event.first_contact_time,
                    touchdown=event.touchdown.time,
                    outcome=event.outcome,
                ),
            )
        )


def _header(recording_time: str) -> list[str]:
    return [
        "FileType=text/acmi/tacview",
        "FileVersion=2.2",
        f"0,ReferenceTime=2026-06-11T04:30:00Z,DataSource=Test,RecordingTime={recording_time}",
    ]


def _landing_lines(
    obj_id: str,
    *,
    touchdown_time: float,
    recording_time: str | None = None,
    ground_roll_s: float = 20.0,
    include_header: bool = True,
) -> list[str]:
    """One aircraft flying a short approach and landing at ``touchdown_time``.

    Frames are 1 s: 10 s inbound (AGL 30 m -> 0, ~3 m/s sink), then a
    ``ground_roll_s`` ground roll with OnGround=1, then the aircraft goes
    silent (buffer frozen, like an aircraft that left the mission).
    ``recording_time`` is required whenever ``include_header`` is set.
    """
    lines: list[str] = []
    if include_header:
        assert recording_time is not None
        lines.extend(_header(recording_time))
    identity_sent = False
    for t in range(-10, int(ground_roll_s) + 1):
        absolute = touchdown_time + t
        lines.append(f"#{absolute:g}")
        if t < 0:
            agl = -t * 3.0
            altitude = DECK_ALT + agl
            on_ground = "0"
        else:
            agl = 0.0
            altitude = DECK_ALT
            on_ground = "1"
        # Straight-in from the south onto the origin.
        lat = REF_LAT - (abs(t) * 70.0) / 111320.0 if t < 0 else REF_LAT
        props = [f"T={REF_LON:g}|{lat:g}|{altitude:g}|||0"]
        if not identity_sent:
            identity_sent = True
            props.extend(["Type=Air+FixedWing", "Name=F/A-18C", "Pilot=Tester"])
        props.append(f"OnGround={on_ground}")
        props.append("TAS=70")
        lines.append(f"{obj_id},{','.join(props)}")
    return lines


async def _feed(ingestor: TrackIngestor, lines: list[str]) -> None:
    for line in lines:
        await ingestor.handle_line(line)


async def _flight_count(session_factory) -> int:  # noqa: ANN001
    from sqlalchemy import select

    async with session_factory() as session:
        flights = (await session.execute(select(Flight))).scalars().all()
    return len(flights)


async def test_mission_restart_rotates_session_and_stops_duplicates(
    session_factory,
) -> None:
    """The reported failure mode, end to end.

    Mission 1: obj 503 lands at t=30000 and goes silent (frozen buffer).
    Mission restart: the frame clock resets, the exporter re-sends the
    header with a new RecordingTime, and a NEW aircraft recycles the id
    503 and lands at t=500.

    The old landing must be reported exactly once, the new landing must
    be its own row with its own touchdown, and the two must never merge:
    pre-rotation the new touchdown event absorbed the stale old touchdown
    as a bounce, reporting t=30000 under the new mission's flight.
    """
    recorder = Recorder()
    ingestor = TrackIngestor(
        session_factory,
        landing_listener=recorder.on_landing,
        landing_finalize_listener=recorder.on_finalize,
    )

    await _feed(
        ingestor,
        _landing_lines("503", touchdown_time=30000.0, recording_time="2026-08-31T15:30:20Z"),
    )
    assert len(recorder.provisional) == 1
    assert recorder.provisional[0].touchdown == 30000.0

    # --- mission restart: new header, frame clock back to ~0, id reused ---
    await _feed(
        ingestor,
        _landing_lines("503", touchdown_time=500.0, recording_time="2026-09-01T00:30:20Z"),
    )
    await ingestor.close()

    # Exactly one flight per session.
    assert await _flight_count(session_factory) == 2

    touchdowns = sorted(r.touchdown for r in recorder.provisional)
    assert touchdowns == [500.0, 30000.0], recorder.provisional
    # The mission-2 event must describe the mission-2 touchdown only.
    second = next(r for r in recorder.provisional if r.touchdown == 500.0)
    assert second.first_contact == 500.0
    assert second.outcome == "full_stop"

    # Both landings finalized, once each.
    assert sorted(tid for tid, _ in recorder.finalized) == [1, 2]


async def test_frame_clock_reset_without_header_also_rotates(
    session_factory,
) -> None:
    """Exports that reset the clock without re-sending the header are
    caught by the mission-time regression trigger."""
    recorder = Recorder()
    ingestor = TrackIngestor(
        session_factory,
        landing_listener=recorder.on_landing,
        landing_finalize_listener=recorder.on_finalize,
    )

    await _feed(
        ingestor,
        _landing_lines("403", touchdown_time=30000.0, recording_time="2026-08-31T15:30:20Z"),
    )
    assert len(recorder.provisional) == 1
    # Same session signature (no new global object line), frame clock reset.
    await _feed(
        ingestor,
        _landing_lines("403", touchdown_time=500.0, include_header=False),
    )
    await ingestor.close()

    assert await _flight_count(session_factory) == 2
    touchdowns = sorted(r.touchdown for r in recorder.provisional)
    assert touchdowns == [500.0, 30000.0], recorder.provisional


async def test_small_time_jitter_does_not_rotate(session_factory) -> None:
    """Out-of-order frames jitter by seconds, not minutes; a jitter below
    the regression threshold must not split the session."""
    recorder = Recorder()
    ingestor = TrackIngestor(
        session_factory,
        landing_listener=recorder.on_landing,
        landing_finalize_listener=recorder.on_finalize,
    )
    await _feed(
        ingestor,
        _landing_lines("301", touchdown_time=1000.0, recording_time="2026-08-31T15:30:20Z"),
    )
    # A late frame lands 30 s in the past.
    jittered = [
        "#970.00",
        "301,T=140.0|35.0|20.0|||0,OnGround=1",
    ]
    await _feed(ingestor, jittered)
    await ingestor.close()

    assert await _flight_count(session_factory) == 1
    assert len(recorder.provisional) == 1


async def test_reconnect_with_same_session_signature_keeps_flight(
    session_factory,
) -> None:
    """A reconnect mid-mission re-sends the header with the SAME
    RecordingTime (same recording session): no rotation, no duplicate
    flight, and the already-reported landing is not reported again."""
    recorder = Recorder()
    ingestor = TrackIngestor(
        session_factory,
        landing_listener=recorder.on_landing,
        landing_finalize_listener=recorder.on_finalize,
    )
    recording_time = "2026-08-31T15:30:20Z"
    await _feed(
        ingestor,
        _landing_lines("503", touchdown_time=30000.0, recording_time=recording_time),
    )
    assert len(recorder.provisional) == 1

    # Reconnect: header re-sent unchanged, then the mission continues.
    await _feed(ingestor, _header(recording_time))
    tail = [
        "#30030.00",
        "503,T=140.0|35.0|20.0|||0,OnGround=1,TAS=5",
        "#30040.00",
        "503,T=140.0|35.0|20.0|||0,OnGround=1,TAS=5",
    ]
    await _feed(ingestor, tail)
    await ingestor.close()

    assert await _flight_count(session_factory) == 1
    assert len(recorder.provisional) == 1
