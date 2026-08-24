"""Touchdown detection and approach-segment extraction (FR-2).

The detector is a pure function over a complete time-sorted track so it can
be used both for real-time monitoring (feeding growing buffers) and offline
re-analysis of stored raw tracks.

Detection model
---------------
Weight-on-wheels (WOW) equivalent:

- If the source provides an explicit ``OnGround`` flag it is used directly.
- Otherwise AGL is derived either from the ``AGL`` property or from
  ``altitude - ground_altitude`` supplied by the caller (the elevation of
  the nearby airfield / carrier deck). AGL at or below
  ``wow_agl_threshold_m`` counts as "on deck".

A *touchdown* is an airborne -> on-deck transition whose descent rate just
before contact stays below ``max_touchdown_descent_ms`` (harder contacts are
treated as crashes, not landings). Consecutive contacts separated by a low
bounce (< 15 m AGL between them) are merged into a single event.

Outcome classification:

- climbs out again within ``touch_and_go_max_dwell_s``:
  ``bolter`` on a carrier, ``touch_and_go`` on land;
- otherwise (ground dwell >= ``full_stop_dwell_s``, or track ends on deck):
  ``full_stop``.

The final-approach segment is cut backwards from touchdown up to
``approach_window_s`` / ``approach_distance_m`` (2 nm), plus a short
post-touchdown tail for context.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from app.detection.geometry import haversine_m, interpolate_position, transform_to_frame


@dataclass
class DetectionConfig:
    wow_agl_threshold_m: float = 3.0
    max_touchdown_descent_ms: float = 8.0
    full_stop_dwell_s: float = 15.0
    touch_and_go_max_dwell_s: float = 45.0
    climb_out_vertical_ms: float = 1.5
    carrier_proximity_m: float = 800.0
    approach_window_s: float = 60.0
    approach_distance_m: float = 3704.0
    #: Extra horizontal slack when walking backwards along the approach.
    approach_distance_margin_m: float = 500.0
    #: Seconds of post-touchdown samples kept in the stored approach track.
    post_touchdown_tail_s: float = 5.0
    #: Bounces below this AGL between contacts are merged into one event.
    bounce_merge_agl_m: float = 15.0


@dataclass
class TrackSample:
    """One positional sample of an aircraft (subset of ``Track`` columns)."""

    time: float
    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = None
    agl: float | None = None
    speed: float | None = None
    heading: float | None = None
    aoa: float | None = None
    on_ground: bool | None = None


@dataclass
class CarrierState:
    """Position/attitude time series of one carrier."""

    obj_id: str
    name: str | None = None
    #: ACMI ``Type`` string, kept so graders can resolve per-carrier
    #: FLOLS geometry (Issue #3).
    type: str | None = None
    #: (time, lat, lon, altitude, heading_deg, speed) tuples, time-sorted.
    samples: list[tuple[float, float, float, float, float, float]] = field(
        default_factory=list
    )

    def position_at(self, time: float) -> tuple[float, float] | None:
        return interpolate_position(
            [(t, lat, lon) for t, lat, lon, *_ in self.samples], time
        )

    def heading_at(self, time: float) -> float | None:
        if not self.samples:
            return None
        # Step function: use the most recent known heading.
        best = self.samples[0][4]
        for sample in self.samples:
            if sample[0] <= time:
                best = sample[4]
            else:
                break
        return best

    def altitude_at(self, time: float) -> float | None:
        """Interpolated ship altitude (MSL) at ``time``."""
        if not self.samples:
            return None
        if time <= self.samples[0][0]:
            return self.samples[0][3]
        if time >= self.samples[-1][0]:
            return self.samples[-1][3]
        lo, hi = 0, len(self.samples) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self.samples[mid][0] <= time:
                lo = mid
            else:
                hi = mid
        t0, _, _, alt0, _, _ = self.samples[lo]
        t1, _, _, alt1, _, _ = self.samples[hi]
        if t1 == t0:
            return alt0
        frac = (time - t0) / (t1 - t0)
        return alt0 + (alt1 - alt0) * frac


@dataclass
class Touchdown:
    time: float
    latitude: float
    longitude: float
    altitude: float
    heading: float | None
    speed: float | None
    aoa: float | None
    descent_rate_ms: float
    ground_altitude_m: float


@dataclass
class LandingEvent:
    """A detected landing / arrestment attempt with its approach segment."""

    touchdown: Touchdown
    kind: str                      # "carrier" | "land"
    outcome: str                   # "full_stop" | "touch_and_go" | "bolter"
    carrier_obj_id: str | None
    carrier_name: str | None
    approach: list[TrackSample]
    #: Ship-relative representation of the approach (carrier events only):
    #: list of dicts with time, along, lateral, gs-relevant altitude data.
    ship_relative: list[dict] = field(default_factory=list)
    #: Carrier facts at the touchdown instant (Issue #3): used by the
    #: grading pipeline to resolve per-carrier FLOLS geometry and to anchor
    #: the ramp-referenced deviation frame on a moving deck.
    carrier_type: str | None = None
    carrier_latitude: float | None = None
    carrier_longitude: float | None = None
    carrier_altitude_m: float | None = None
    carrier_heading_deg: float | None = None
    #: True once the outcome can no longer change (climb-out observed, or
    #: the full-stop dwell has elapsed). Live monitoring should only report
    #: finalized events; offline analysis always yields finalized events.
    finalized: bool = True


def compute_agl(
    sample: TrackSample,
    ground_altitude_m: float | None,
    config: DetectionConfig,
) -> float | None:
    """Best-effort height above the touchdown surface for one sample."""
    if sample.agl is not None:
        return sample.agl
    if sample.altitude is not None and ground_altitude_m is not None:
        return sample.altitude - ground_altitude_m
    return None


def is_on_deck(
    sample: TrackSample,
    ground_altitude_m: float | None,
    config: DetectionConfig,
) -> bool | None:
    """Three-state WOW estimate: True / False / None (unknown)."""
    if sample.on_ground is not None:
        return sample.on_ground
    agl = compute_agl(sample, ground_altitude_m, config)
    if agl is None:
        return None
    return agl <= config.wow_agl_threshold_m


def _descent_rate_before(samples: list[TrackSample], index: int, span_s: float = 3.0) -> float | None:
    """Mean descent rate (m/s, positive down) over ``span_s`` before index."""
    ref = samples[index]
    for j in range(index - 1, -1, -1):
        prev = samples[j]
        dt = ref.time - prev.time
        if dt >= span_s:
            if (
                prev.altitude is None
                or ref.altitude is None
                or dt <= 0
            ):
                return None
            return (prev.altitude - ref.altitude) / dt
    return None


def _vertical_speed(samples: list[TrackSample], index: int, span_s: float = 2.0) -> float | None:
    """Signed vertical speed (m/s, positive up) around ``index``."""
    ref = samples[index]
    for j in range(index - 1, -1, -1):
        prev = samples[j]
        dt = ref.time - prev.time
        if dt >= span_s:
            if prev.altitude is None or ref.altitude is None or dt <= 0:
                return None
            return (ref.altitude - prev.altitude) / dt
    return None


def _nearest_carrier(
    touchdown: Touchdown,
    carriers: dict[str, CarrierState],
    config: DetectionConfig,
) -> CarrierState | None:
    best: CarrierState | None = None
    best_distance = config.carrier_proximity_m
    for carrier in carriers.values():
        pos = carrier.position_at(touchdown.time)
        if pos is None:
            continue
        distance = haversine_m(touchdown.latitude, touchdown.longitude, pos[0], pos[1])
        if distance <= best_distance:
            best = carrier
            best_distance = distance
    return best


def _cut_approach(
    samples: list[TrackSample],
    touchdown_index: int,
    touchdown: Touchdown,
    config: DetectionConfig,
) -> list[TrackSample]:
    """Walk backwards from touchdown collecting the final-approach segment."""
    start_index = touchdown_index
    limit_time = touchdown.time - config.approach_window_s
    for j in range(touchdown_index - 1, -1, -1):
        sample = samples[j]
        if sample.time < limit_time:
            break
        if (
            sample.latitude is not None
            and sample.longitude is not None
            and haversine_m(
                sample.latitude, sample.longitude, touchdown.latitude, touchdown.longitude
            )
            > config.approach_distance_m + config.approach_distance_margin_m
        ):
            break
        start_index = j
    tail_limit = touchdown.time + config.post_touchdown_tail_s
    end_index = touchdown_index
    for j in range(touchdown_index + 1, len(samples)):
        if samples[j].time > tail_limit:
            break
        end_index = j
    return samples[start_index : end_index + 1]


def analyze_track(
    samples: list[TrackSample],
    ground_altitude_m: float | None,
    carriers: dict[str, CarrierState] | None = None,
    config: DetectionConfig | None = None,
    current_time: float | None = None,
) -> list[LandingEvent]:
    """Detect landing events in a complete, time-sorted aircraft track.

    ``current_time`` is the mission time during live monitoring; a full-stop
    event whose dwell has not yet elapsed is reported as not finalized (it
    could still turn into a touch-and-go). Offline analysis omits it and
    marks every event finalized.
    """
    config = config or DetectionConfig()
    carriers = carriers or {}
    samples = sorted(samples, key=lambda s: s.time)
    if len(samples) < 2:
        return []

    wow = [is_on_deck(s, ground_altitude_m, config) for s in samples]

    # Candidate touchdown indices: airborne/on-deck transitions.
    candidates: list[int] = []
    for i in range(1, len(samples)):
        was = wow[i - 1]
        now = wow[i]
        if was is False and now is True:
            rate = _descent_rate_before(samples, i)
            if rate is not None and rate > config.max_touchdown_descent_ms:
                continue  # too hard: crash, not a landing
            candidates.append(i)

    events: list[LandingEvent] = []
    used_through: int = -1
    for index in candidates:
        if index <= used_through:
            continue
        # Merge bounces: absorb a later contact only when the aircraft left
        # the deck in between but never climbed above bounce_merge_agl_m.
        last_index = index
        airborne_since_contact = False
        peak_agl = 0.0
        for j in range(index + 1, len(samples)):
            agl = compute_agl(samples[j], ground_altitude_m, config)
            if wow[j] is False:
                airborne_since_contact = True
                peak_agl = max(peak_agl, agl or 0.0)
                continue
            if not airborne_since_contact:
                continue  # still the same continuous ground roll
            if peak_agl > config.bounce_merge_agl_m:
                break
            rate = _descent_rate_before(samples, j)
            if rate is not None and rate > config.max_touchdown_descent_ms:
                break
            last_index = j
            used_through = j
            airborne_since_contact = False
            peak_agl = 0.0

        touchdown = _make_touchdown(samples, last_index, ground_altitude_m)
        outcome, climb_index = _classify_outcome(samples, wow, index, last_index, config)

        carrier = _nearest_carrier(touchdown, carriers, config)
        kind = "carrier" if carrier is not None else "land"
        if kind == "carrier" and outcome == "touch_and_go":
            outcome = "bolter"

        approach = _cut_approach(samples, index, touchdown, config)
        if outcome != "full_stop" or current_time is None:
            finalized = True
        else:
            finalized = (current_time - touchdown.time) >= config.full_stop_dwell_s
        event = LandingEvent(
            touchdown=touchdown,
            kind=kind,
            outcome=outcome,
            carrier_obj_id=carrier.obj_id if carrier else None,
            carrier_name=carrier.name if carrier else None,
            approach=approach,
            finalized=finalized,
        )
        if carrier is not None:
            # Carrier state at the touchdown instant (Issue #3).
            event.carrier_type = carrier.type
            pos = carrier.position_at(touchdown.time)
            if pos is not None:
                event.carrier_latitude, event.carrier_longitude = pos
            event.carrier_altitude_m = carrier.altitude_at(touchdown.time)
            event.carrier_heading_deg = carrier.heading_at(touchdown.time)
        if carrier is not None:
            event.ship_relative = to_ship_relative(approach, carrier, touchdown, config)
        events.append(event)
        used_through = max(used_through, climb_index if outcome != "full_stop" else last_index)

    return events


def _make_touchdown(
    samples: list[TrackSample],
    index: int,
    ground_altitude_m: float | None,
) -> Touchdown:
    sample = samples[index]
    return Touchdown(
        time=sample.time,
        latitude=sample.latitude or 0.0,
        longitude=sample.longitude or 0.0,
        altitude=sample.altitude if sample.altitude is not None else (ground_altitude_m or 0.0),
        heading=sample.heading,
        speed=sample.speed,
        aoa=sample.aoa,
        descent_rate_ms=_descent_rate_before(samples, index) or 0.0,
        ground_altitude_m=ground_altitude_m or 0.0,
    )


def _classify_outcome(
    samples: list[TrackSample],
    wow: list[bool | None],
    first_contact: int,
    last_contact: int,
    config: DetectionConfig,
) -> tuple[str, int]:
    """Return ``(outcome, index_reached)`` for a touchdown sequence."""
    dwell_end = samples[last_contact].time
    for j in range(last_contact + 1, len(samples)):
        elapsed = samples[j].time - dwell_end
        if wow[j] is False:
            vertical = _vertical_speed(samples, j)
            climbing = vertical is not None and vertical > config.climb_out_vertical_ms
            if climbing and elapsed <= config.touch_and_go_max_dwell_s:
                return ("touch_and_go", j)
            if elapsed > config.touch_and_go_max_dwell_s:
                break
        if elapsed > config.touch_and_go_max_dwell_s:
            break
    if dwell_end - samples[first_contact].time >= config.full_stop_dwell_s:
        return ("full_stop", last_contact)
    # Track ended while still on deck: assume the landing completed.
    return ("full_stop", last_contact)


def to_ship_relative(
    approach: list[TrackSample],
    carrier: CarrierState,
    touchdown: Touchdown,
    config: DetectionConfig,
) -> list[dict]:
    """Convert an approach segment into the carrier's reference frame.

    Each sample is transformed with the carrier's interpolated position and
    heading *at that sample's time*, so a moving deck yields a stable
    relative picture (FR-2). Output rows carry:

    - ``time``: mission time,
    - ``along``: meters ahead of the carrier (+) / behind (-),
    - ``lateral``: meters right of the ship's heading (+),
    - ``agl``: height above the deck,
    - ``distance_to_go``: horizontal distance to the touchdown point.
    """
    rows: list[dict] = []
    for sample in approach:
        if sample.latitude is None or sample.longitude is None:
            continue
        pos = carrier.position_at(sample.time)
        heading = carrier.heading_at(sample.time)
        if pos is None or heading is None:
            continue
        along, lateral = transform_to_frame(
            sample.latitude, sample.longitude, pos[0], pos[1], heading
        )
        agl = compute_agl(sample, touchdown.ground_altitude_m, config)
        distance_to_go = haversine_m(
            sample.latitude, sample.longitude, touchdown.latitude, touchdown.longitude
        )
        rows.append(
            {
                "time": round(sample.time, 3),
                "along": round(along, 2),
                "lateral": round(lateral, 2),
                "agl": round(agl, 2) if agl is not None else None,
                "distance_to_go": round(distance_to_go, 2),
                "speed": sample.speed,
                "aoa": sample.aoa,
                "heading": sample.heading,
            }
        )
    return rows


class RollingTrackBuffer:
    """Bounded, time-ordered buffer of recent samples for live detection."""

    def __init__(self, max_age_s: float) -> None:
        self._max_age_s = max_age_s
        self._samples: deque[TrackSample] = deque()

    def append(self, sample: TrackSample) -> None:
        self._samples.append(sample)
        cutoff = sample.time - self._max_age_s
        while self._samples and self._samples[0].time < cutoff:
            self._samples.popleft()

    def snapshot(self) -> list[TrackSample]:
        return list(self._samples)
