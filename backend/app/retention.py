"""How much of an aircraft's raw track the database keeps, and why that much.

Live detection never reads ``tracks``: it runs on the ingestor's in-memory
buffers. The only reader is the rebuild of a stored landing
(:mod:`app.rebuild`), and it reads one fixed span around the touchdown. The
table used to keep every update of every object anyway -- missiles, shells,
chaff, cruising aircraft -- and passed 87 million rows (about 11 GB) on the
production server, all of it written on the ingest's hot path and almost none
of it ever read back. So the ingestor now writes an aircraft's samples only
around its landings, and over the same span the samples of the ship it
landed on, if any. Statics are still written as they come; they barely
update and the rebuild's ground reference reads them.

Only the ship the landing names, not every ship of the flight: the detector
only looks at a ship within ``carrier_proximity_m`` (800 m) of the aircraft,
so the others cannot change what a rebuild finds -- and keeping every ship
for every landing anywhere kept the ships' whole tracks on a busy server,
which is most of what this module exists to stop.

Everything that decides "which raw samples belong to a landing" -- what the
rebuild reads, what the ingestor writes, what the one-off compaction of an
old database keeps -- derives from the two functions here, so the three
cannot drift apart.
"""

from __future__ import annotations

from app.detection.detector import DetectionConfig

#: How far either side of the touchdown to read beyond the detector's own
#: windows: the ground-speed baseline needs up to 15 s of history before the
#: first sample of the approach, and the outcome needs the climb-out (or the
#: full-stop dwell) after the contact.
REBUILD_MARGIN_S = 60.0

#: Kept beyond what today's rebuild reads, on both sides. Samples that were
#: not kept cannot be brought back, so a detector that one day cuts a wider
#: approach should still find something to cut.
RETENTION_SLACK_S = 60.0

#: How long a bounce sequence may run (first contact to last) and still have
#: its whole window in the ingestor's history when the window is written.
BOUNCE_ALLOWANCE_S = 60.0


def rebuild_window(touchdown_time: float, detection: DetectionConfig) -> tuple[float, float]:
    """The raw span :meth:`app.pipeline.LandingPipeline.rebuild` reads."""
    lead = REBUILD_MARGIN_S + max(
        detection.approach_window_s,
        detection.land_approach_window_s,
        detection.carrier_approach_window_s,
    )
    return touchdown_time - lead, touchdown_time + REBUILD_MARGIN_S


def retention_window(
    first_contact_time: float, touchdown_time: float, detection: DetectionConfig
) -> tuple[float, float]:
    """The raw span kept for one landing.

    What the rebuild reads, taken from the first contact rather than the
    touchdown -- the touchdown is the LAST contact of a bounce sequence and
    walks forward as bounces are absorbed -- plus the slack on either side.
    A caller that only knows the stored touchdown passes it for both.
    """
    start, _ = rebuild_window(first_contact_time, detection)
    _, end = rebuild_window(touchdown_time, detection)
    return start - RETENTION_SLACK_S, end + RETENTION_SLACK_S


def retention_history_s(detection: DetectionConfig) -> float:
    """How much raw history the ingestor must hold per aircraft and carrier.

    A window is written once the stream has passed its end, so at that
    moment the history has to reach back to its start.
    """
    start, end = retention_window(0.0, 0.0, detection)
    return end - start + BOUNCE_ALLOWANCE_S
