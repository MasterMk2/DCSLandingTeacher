"""Two-phase landing confirmation tests (Issue #5).

A touchdown observed live is persisted and broadcast immediately as
``provisional``; once the outcome can no longer change (full-stop dwell
elapsed, climb-out observed, or the object disappears) the same row is
updated in place and a ``landing_update`` message is broadcast.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.grading.config import GradingConfig
from app.ingest import TrackIngestor
from app.models.entities import Landing
from app.pipeline import LandingPipeline
from tests.helpers import make_acmi_text, make_approach_samples


class RecordingNotifier:
    """Captures broadcast messages for assertions."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, Any]]] = []

    async def broadcast_landing(
        self, payload: dict[str, Any], *, message_type: str = "landing"
    ) -> None:
        self.messages.append((message_type, payload))


class StreamPlayer:
    """Feeds a rendered ACMI text into an ingestor frame by frame.

    ``feed_through`` keeps sending frames until the aircraft's detection
    buffer contains a sample at or beyond the requested helper-sample time,
    so it stays correct regardless of how frame deltas map to parser times.
    """

    def __init__(
        self,
        ingestor: TrackIngestor,
        text: str,
        *,
        first_sample_time: float,
    ) -> None:
        self._ingestor = ingestor
        #: Offset between helper-sample times and parser mission times.
        self._offset = -first_sample_time
        lines = text.splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith("#"))
        self._header = lines[:start]
        self._frames: list[list[str]] = []
        current: list[str] | None = None
        for line in lines[start:]:
            if line.startswith("#"):
                current = [line]
                self._frames.append(current)
            else:
                assert current is not None
                current.append(line)
        self._sent_header = False
        self._next_frame = 0

    def _buffer_time(self) -> float:
        buffer = self._ingestor._aircraft_buffers.get("101")
        if buffer is None:
            return float("-inf")
        samples = buffer.snapshot()
        return samples[-1].time if samples else float("-inf")

    async def feed_through(self, sample_time: float) -> None:
        target = sample_time + self._offset
        if not self._sent_header:
            for line in self._header:
                await self._ingestor.handle_line(line)
            self._sent_header = True
        while self._next_frame < len(self._frames):
            if self._buffer_time() >= target:
                break
            frame = self._frames[self._next_frame]
            self._next_frame += 1
            for line in frame:
                await self._ingestor.handle_line(line)


def _player(
    session_factory,
    notifier: RecordingNotifier,
    *,
    outcome: str,
) -> tuple[TrackIngestor, StreamPlayer]:
    pipeline = LandingPipeline(session_factory, GradingConfig({}), notifier=notifier)
    ingestor = TrackIngestor(
        session_factory,
        landing_listener=pipeline.handle_landing,
        landing_finalize_listener=pipeline.finalize_landing,
        sample_buffer_s=600.0,
    )
    samples = make_approach_samples(outcome=outcome)
    text = make_acmi_text(samples)
    return ingestor, StreamPlayer(ingestor, text, first_sample_time=samples[0].time)


async def _landings(session_factory) -> list[Landing]:
    async with session_factory() as session:
        result = await session.execute(select(Landing).order_by(Landing.id))
        return list(result.scalars().all())


async def test_touchdown_is_reported_provisionally_then_finalized(
    session_factory,
) -> None:
    notifier = RecordingNotifier()
    ingestor, player = _player(session_factory, notifier, outcome="full_stop")
    try:
        # Feed up to touchdown: the event must be visible immediately.
        await player.feed_through(0.0)

        rows = await _landings(session_factory)
        assert len(rows) == 1
        provisional = rows[0]
        assert provisional.outcome_status == "provisional"
        assert provisional.grade  # graded from the approach segment already

        types = [m[0] for m in notifier.messages]
        assert types == ["landing"]
        payload = notifier.messages[0][1]
        assert payload["outcome_status"] == "provisional"
        assert payload["id"] == provisional.id

        # Feed t up to ~10s after touchdown: still within the full-stop dwell
        # window; no new rows and no duplicate notifications.
        await player.feed_through(10.0)
        rows = await _landings(session_factory)
        assert len(rows) == 1
        assert [m[0] for m in notifier.messages] == ["landing"]

        # Feed t>=15: dwell elapsed -> the same row flips to final.
        await player.feed_through(25.0)
    finally:
        await ingestor.close()

    rows = await _landings(session_factory)
    assert len(rows) == 1
    final = rows[0]
    assert final.id == provisional.id
    assert final.outcome_status == "final"
    assert final.outcome == "full_stop"

    types = [m[0] for m in notifier.messages]
    assert types == ["landing", "landing_update"]
    update_payload = notifier.messages[1][1]
    assert update_payload["id"] == provisional.id
    assert update_payload["outcome_status"] == "final"


async def test_provisional_full_stop_is_corrected_to_bolter(session_factory) -> None:
    notifier = RecordingNotifier()
    ingestor, player = _player(session_factory, notifier, outcome="touch_and_go")
    try:
        # On deck through t=3, then climbs out again (bolter).
        await player.feed_through(3.0)
        rows = await _landings(session_factory)
        assert len(rows) == 1
        assert rows[0].outcome_status == "provisional"

        await player.feed_through(12.0)
    finally:
        await ingestor.close()

    rows = await _landings(session_factory)
    assert len(rows) == 1
    final = rows[0]
    assert final.outcome == "bolter"
    assert final.outcome_status == "final"
    assert final.grade == "_NO_GRADE_"

    types = [m[0] for m in notifier.messages]
    assert types == ["landing", "landing_update"]
    assert notifier.messages[1][1]["outcome"] == "bolter"


async def test_object_removal_finalizes_pending_provisional(session_factory) -> None:
    notifier = RecordingNotifier()
    ingestor, player = _player(session_factory, notifier, outcome="full_stop")
    try:
        await player.feed_through(2.0)
        rows = await _landings(session_factory)
        assert len(rows) == 1
        assert rows[0].outcome_status == "provisional"

        # The aircraft leaves the mission while still on deck: the pending
        # provisional record must be finalized by the removal pass.
        await ingestor.handle_line("-101")
    finally:
        await ingestor.close()

    rows = await _landings(session_factory)
    assert len(rows) == 1
    assert rows[0].outcome_status == "final"
    assert [m[0] for m in notifier.messages] == ["landing", "landing_update"]


async def test_full_stream_ingestion_ends_with_a_single_final_row(
    session_factory,
) -> None:
    """Whatever the interim states, the stored end state is one final row."""
    notifier = RecordingNotifier()
    pipeline = LandingPipeline(session_factory, GradingConfig({}), notifier=notifier)
    ingestor = TrackIngestor(
        session_factory,
        landing_listener=pipeline.handle_landing,
        landing_finalize_listener=pipeline.finalize_landing,
        sample_buffer_s=600.0,
    )
    try:
        text = make_acmi_text(make_approach_samples(outcome="full_stop"))
        for line in text.splitlines():
            await ingestor.handle_line(line)
    finally:
        await ingestor.close()

    rows = await _landings(session_factory)
    assert len(rows) == 1
    assert rows[0].outcome_status == "final"
    assert [m[0] for m in notifier.messages] == ["landing", "landing_update"]
