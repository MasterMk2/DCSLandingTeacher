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
``approach_window_s`` / ``approach_distance_m`` (2 nm; land landings use
the longer ``land_approach_*`` pair so the pattern fits), plus a short
post-touchdown tail for context.
"""

from __future__ import annotations

import math

from bisect import bisect_right
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from app.detection.geometry import (
    haversine_m,
    interpolate_position,
    meters_per_degree_latitude,
    transform_to_frame,
)


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
    #: Land landings capture further back than the ~2 nm final: the graded
    #: pattern (break -> downwind -> base) simply does not fit in 60 s.
    #: A fighter circuit at 1.5 nm abeam sits ~4.5 km from the touchdown
    #: point, i.e. outside the carrier-sized radius above, and the initial
    #: that precedes the break runs 3-5 nm out -- 3 nm cut it off mid-leg
    #: (landing #54's capture stopped dead at 5.77 km). Carrier passes keep
    #: the short window: there is no pattern to capture and the LSO grader
    #: only ever looks at the last seconds.
    land_approach_window_s: float = 300.0
    land_approach_distance_m: float = 14816.0  # 8 nm
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


def _sample_time(sample: tuple[float, ...]) -> float:
    return sample[0]


@dataclass
class CarrierState:
    """Position/attitude time series of one carrier."""

    obj_id: str
    name: str | None = None
    #: ACMI ``Type`` string, kept so graders can resolve per-carrier
    #: FLOLS geometry (Issue #3).
    type: str | None = None
    #: (time, lat, lon, altitude, heading_deg, speed) tuples, time-sorted.
    samples: list[tuple[float, float, float, float, float, float]] = field(default_factory=list)
    #: Retention window in seconds; ``None`` keeps everything (offline replay).
    max_age_s: float | None = None

    def append(self, sample: tuple[float, float, float, float, float, float]) -> None:
        """Record a sample, dropping history outside the retention window.

        Letting this grow without bound used to wedge the whole ingest loop:
        carriers stream at ~3Hz for the life of the session, and both
        _nearest_carrier() and the carrier deviation track call position_at()
        per landing and per approach sample. After two days each carrier held
        ~500k samples, so a single detection pass walked millions of them and
        the event loop stopped returning. Aircraft were already windowed by
        RollingTrackBuffer; carriers were not.
        """
        if self.samples and self.samples[-1][0] == sample[0]:
            return
        self.samples.append(sample)
        if self.max_age_s is None:
            return
        cutoff = sample[0] - self.max_age_s
        if self.samples[0][0] >= cutoff:
            return
        keep_from = bisect_right(self.samples, cutoff, key=_sample_time)
        if keep_from:
            del self.samples[:keep_from]

    def position_at(self, time: float) -> tuple[float, float] | None:
        return interpolate_position(self.samples, time)

    def heading_at(self, time: float) -> float | None:
        if not self.samples:
            return None
        # Step function: the most recent known heading at or before `time`,
        # falling back to the oldest sample we still hold.
        index = bisect_right(self.samples, time, key=_sample_time) - 1
        return self.samples[max(index, 0)][4]

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


#: Deck altitude (MSL) of a carrier, or ``None`` when the ship is not in the
#: geometry book. Supplied by the caller because the book lives in the
#: grading layer, which the detector deliberately does not import.
DeckAltitudeResolver = Callable[["CarrierState"], float | None]


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
    #: True when ``ground_altitude_m`` is a carrier deck rather than terrain.
    #: Everything downstream that derives a height has to know, because
    #: Tacview's per-sample AGL is measured to the sea and must not be used
    #: as "height above the deck".
    surface_is_deck: bool = False


@dataclass
class LandingEvent:
    """A detected landing / arrestment attempt with its approach segment."""

    touchdown: Touchdown
    kind: str  # "carrier" | "land"
    outcome: str  # "full_stop" | "touch_and_go" | "bolter"
    carrier_obj_id: str | None
    carrier_name: str | None
    approach: list[TrackSample]
    #: Time of the *first* ground contact of this sequence.
    #:
    #: ``touchdown`` is the last contact of a merged bounce sequence, so it
    #: moves forward every time a further bounce is absorbed. During live
    #: monitoring the same landing is re-analysed on a growing buffer, which
    #: means touchdown time is not a stable identity for an in-progress
    #: event; the first contact is. Callers correlating repeated analyses of
    #: one landing (two-phase confirmation) must key on this.
    first_contact_time: float = 0.0
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
    #: Approach pattern classification: "overhead" | "straight_in" | "unknown".
    #: Overhead = initial -> break -> base -> final (typical fighter pattern).
    approach_pattern: str = "unknown"


def compute_agl(
    sample: TrackSample,
    ground_altitude_m: float | None,
    config: DetectionConfig,
    *,
    trust_sample_agl: bool = True,
) -> float | None:
    """Best-effort height above the touchdown surface for one sample.

    ``trust_sample_agl`` must be False when the surface underneath is a
    carrier deck. Tacview's own ``AGL`` is measured to the terrain -- over
    water, to the sea -- and knows nothing about a ship floating on it, so
    an aircraft sitting on a flight deck reports about 20 m AGL and never
    looks like it landed. Deriving the height from ``altitude`` minus the
    deck altitude is the only reading that means "above the deck".
    """
    if trust_sample_agl and sample.agl is not None:
        return sample.agl
    if sample.altitude is not None and ground_altitude_m is not None:
        return sample.altitude - ground_altitude_m
    return None


def is_on_deck(
    sample: TrackSample,
    ground_altitude_m: float | None,
    config: DetectionConfig,
    *,
    trust_sample_agl: bool = True,
) -> bool | None:
    """Three-state WOW estimate: True / False / None (unknown)."""
    if sample.on_ground is not None:
        return sample.on_ground
    agl = compute_agl(sample, ground_altitude_m, config, trust_sample_agl=trust_sample_agl)
    if agl is None:
        return None
    if not trust_sample_agl:
        # Over a deck the test is a BAND, not a ceiling. An aircraft cannot
        # be below the deck it is standing on, and something 22 m below it
        # is in the water beside the ship -- which is the only thing this
        # detector has ever actually caught (all five stored "carrier
        # landings" touched down between -3.75 m and +2.78 m altitude, two
        # of them AIM-120s). The band is symmetric around the deck because
        # the same tolerance covers noise in either direction; nothing here
        # is calibrated beyond that.
        return -config.wow_agl_threshold_m <= agl <= config.wow_agl_threshold_m
    return agl <= config.wow_agl_threshold_m


def _reference_surfaces(
    samples: list[TrackSample],
    carriers: dict[str, CarrierState],
    config: DetectionConfig,
    deck_altitude_for: DeckAltitudeResolver | None,
    ground_altitude_m: float | None,
) -> list[tuple[float | None, bool]]:
    """The surface under each sample: ``(altitude_msl, is_a_deck)``.

    Why this is per-sample rather than one number for the track: an aircraft
    recovering to a carrier is over the sea for the whole approach and over a
    deck 20 m up for the last instant, and it is precisely that instant the
    touchdown test has to get right.

    Without it the carrier case cannot work at all. ``on_ground`` is absent
    from this server's data (0 of 87.8 M track rows carry it), so the WOW
    test falls back to ``agl <= 3 m``; Tacview reports AGL to the sea; a
    Nimitz deck is 22 m up. An aircraft that traps therefore never registers
    as on-deck, and the only "carrier landings" ever recorded here were
    objects that hit the WATER within 800 m of a ship -- measured: all five
    stored ones touch down between -3.75 m and +2.78 m altitude, and two of
    them are AIM-120s.
    """
    if not carriers or deck_altitude_for is None:
        return [(ground_altitude_m, False)] * len(samples)

    #: Cache per carrier so the book is consulted once, not per sample.
    #: Ships the book does not know drop out here rather than per sample.
    known = [
        (obj_id, state, deck)
        for obj_id, state in carriers.items()
        if (deck := deck_altitude_for(state)) is not None
    ]
    if not known:
        return [(ground_altitude_m, False)] * len(samples)

    # Cheap box that bounds the proximity test, so the haversine only runs on
    # candidates that could possibly be inside it. Without this every sample
    # paid a haversine per ship, on every detection pass, over the whole
    # rolling buffer -- a land recording that merely shares a mission with a
    # carrier group paid it for nothing.
    #
    # The two axes need DIFFERENT windows, and getting this backwards makes
    # the filter reject ships that are genuinely in range. A degree of
    # latitude is ~111 km everywhere; a degree of longitude is that times
    # cos(latitude), i.e. SHORTER -- so a given distance spans MORE degrees of
    # longitude, and the longitude window must be the wider one. At this
    # server's operating area (~42 N) 800 m is 0.0072 deg of latitude but
    # 0.0097 deg of longitude: a single latitude-sized window would have
    # discarded any ship more than ~600 m due east or west while still inside
    # the 800 m radius.
    #
    # Derived from the SAME sphere haversine_m measures on, not from a
    # hand-rounded 111_000. Those differ by 195 m per degree, and with the
    # rounded value the box's entire soundness was that 0.19% gap -- narrowing
    # the constant toward the true figure would have silently made the filter
    # reject ships in range, with the suite still green. SLACK is explicit
    # room for floating-point error, so the margin is a stated quantity
    # instead of a side effect of rounding.
    SLACK = 1.001
    window_lat_deg = (config.carrier_proximity_m / meters_per_degree_latitude(0.0)) * SLACK

    surfaces: list[tuple[float | None, bool]] = []
    for sample in samples:
        best: float | None = None
        best_distance = config.carrier_proximity_m
        if sample.latitude is not None and sample.longitude is not None:
            # Near the poles a degree of longitude shrinks to nothing, so the
            # window this produces stops bounding the radius; below cos(lat)
            # 0.01 (|lat| > ~89.4 deg) the box is skipped and every ship goes
            # to the haversine. The earlier version claimed the 1e-6 clamp
            # covered this -- it does not, it only stops the division blowing
            # up, and the box is already unsound well before cos gets that
            # small. No track here goes past 45.3 deg, so this is correctness
            # for its own sake rather than a fix for an observed failure.
            cos_lat = math.cos(math.radians(sample.latitude))
            use_box = cos_lat >= 0.01
            window_lon_deg = window_lat_deg / cos_lat if use_box else 0.0
            for _obj_id, carrier, deck in known:
                pos = carrier.position_at(sample.time)
                if pos is None:
                    continue
                if use_box:
                    # Longitude wraps: a ship at +179.9995 and an aircraft at
                    # -179.9995 are 400 m apart but 359.999 degrees apart by
                    # subtraction, and the raw difference threw them away
                    # while haversine_m (which wraps correctly) would have
                    # accepted them.
                    d_lon = abs(pos[1] - sample.longitude)
                    d_lon = min(d_lon, 360.0 - d_lon)
                    if abs(pos[0] - sample.latitude) > window_lat_deg or d_lon > window_lon_deg:
                        continue
                distance = haversine_m(sample.latitude, sample.longitude, pos[0], pos[1])
                if distance <= best_distance:
                    ship_altitude = carrier.altitude_at(sample.time) or 0.0
                    best = ship_altitude + deck
                    best_distance = distance
        surfaces.append((best, True) if best is not None else (ground_altitude_m, False))
    return surfaces


def _descent_rate_before(
    samples: list[TrackSample], index: int, span_s: float = 3.0
) -> float | None:
    """Mean descent rate (m/s, positive down) over ``span_s`` before index."""
    ref = samples[index]
    for j in range(index - 1, -1, -1):
        prev = samples[j]
        dt = ref.time - prev.time
        if dt >= span_s:
            if prev.altitude is None or ref.altitude is None or dt <= 0:
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


def _classify_approach_pattern(approach: list[TrackSample]) -> str:
    """Classify the approach pattern: 'overhead', 'straight_in', or 'unknown'.

    Overhead pattern (typical fighter):
    - Initial: high altitude, high speed, roughly straight
    - Break: sharp turn (high heading rate) near the airfield
    - Base: perpendicular to final course, descending
    - Final: aligned with runway, stabilized

    Heuristics:
    - Need at least ~30s of approach data
    - Detect a significant heading change (> 90 deg) in the middle portion
    - The turn should be a "break" - high rate, not a gentle curve
    - Final segment (from heading stabilization to touchdown) should be relatively straight
    """
    # Filter out ground roll (post-touchdown) samples; only airborne phase matters for pattern.
    airborne_approach = [s for s in approach if not s.on_ground]
    if len(airborne_approach) < 10:
        return "unknown"

    # Extract samples with valid heading
    headed_samples = [(s.time, s.heading) for s in airborne_approach if s.heading is not None]
    if len(headed_samples) < 10:
        return "unknown"

    times = [s.time for s in airborne_approach]
    total_duration = times[-1] - times[0]
    if total_duration < 20.0:
        return "unknown"

    # --- Determine final approach heading (touchdown heading) ---
    final_heading = airborne_approach[-1].heading
    if final_heading is None:
        return "unknown"

    # --- Quick check for straight-in: mostly aligned throughout ---
    # If >70% of airborne samples are within 20 deg of final heading,
    # it's a straight-in approach (no break turn).
    aligned_count = sum(
        1 for _, h in headed_samples if abs((h - final_heading + 180) % 360 - 180) < 20.0
    )
    if aligned_count / len(headed_samples) > 0.7:
        return "straight_in"

    # --- Find where the aircraft stabilizes on final heading ---
    # Scan backwards from touchdown to find the last point where heading
    # deviated significantly from final heading. Everything after that
    # is the "stabilized final segment".
    ALIGNMENT_THRESHOLD_DEG = 20.0  # degrees from final heading
    final_segment_start_idx = None
    for i in range(len(headed_samples) - 1, -1, -1):
        _, h = headed_samples[i]
        dh = (h - final_heading + 180) % 360 - 180
        if abs(dh) > ALIGNMENT_THRESHOLD_DEG:
            final_segment_start_idx = i + 1
            break
    if final_segment_start_idx is None:
        # Aligned from the beginning (should have been caught by straight-in check)
        final_segment_start_idx = 0

    final_segment_time = headed_samples[final_segment_start_idx][0]
    final_segment = headed_samples[final_segment_start_idx:]

    # --- Middle portion: between initial (first 10s) and final segment ---
    initial_cutoff = times[0] + 10.0
    middle_samples = [(t, h) for t, h in headed_samples if initial_cutoff <= t < final_segment_time]
    if len(middle_samples) < 5:
        return "unknown"

    # Calculate max heading rate in middle portion (the "break" turn)
    max_heading_rate = 0.0
    for i in range(1, len(middle_samples)):
        t0, h0 = middle_samples[i - 1]
        t1, h1 = middle_samples[i]
        dt = t1 - t0
        if dt <= 0:
            continue
        dh = (h1 - h0 + 180) % 360 - 180
        rate = abs(dh) / dt
        max_heading_rate = max(max_heading_rate, rate)

    # Break turn: aggressive turn > 3 deg/s
    # Straight-in: gentle curves < 1.5 deg/s (already handled above)
    if max_heading_rate > 3.0:
        # Verify final segment is stable (heading spread < 30 deg)
        final_headings = [h for _, h in final_segment]
        if len(final_headings) >= 3:
            final_spread = max(final_headings) - min(final_headings)
            if final_spread > 180:
                final_spread = 360 - final_spread
            if final_spread < 30.0:
                return "overhead"

    return "unknown"


def _cut_approach(
    samples: list[TrackSample],
    touchdown_index: int,
    touchdown: Touchdown,
    config: DetectionConfig,
    kind: str = "carrier",
    wow: list[bool | None] | None = None,
) -> list[TrackSample]:
    """Walk backwards from touchdown collecting the final-approach segment.

    The walk stops at the previous ground contact when there is one. A
    circuit begins where the last one ended, so anything before that belongs
    to a different arrival -- and the land window is now wide enough
    (300 s / 8 nm) to reach back into it, which would draw two loops on the
    plan view and give the leg finder two downwinds to choose between.
    """
    window_s = config.approach_window_s
    distance_m = config.approach_distance_m
    if kind == "land":
        window_s = max(window_s, config.land_approach_window_s)
        distance_m = max(distance_m, config.land_approach_distance_m)
    start_index = touchdown_index
    limit_time = touchdown.time - window_s
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
            > distance_m + config.approach_distance_margin_m
        ):
            break
        if wow is not None and wow[j] is True:
            # A previous touchdown / roll-out: this circuit started here.
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
    deck_altitude_for: DeckAltitudeResolver | None = None,
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

    # The surface under each sample, which over a carrier is the deck and
    # not the sea (see _reference_surfaces). Without this the WOW test can
    # never fire for an aircraft that traps.
    surfaces = _reference_surfaces(samples, carriers, config, deck_altitude_for, ground_altitude_m)
    wow = [
        is_on_deck(s, surface, config, trust_sample_agl=not on_deck)
        for s, (surface, on_deck) in zip(samples, surfaces)
    ]
    # Sank THROUGH the deck: it is in the water beside the ship, not on it.
    # An aircraft on a 3.5 deg path crosses deck height on its way down, so
    # the band alone would still call that crossing a touchdown -- and then
    # a full stop, because sinking is not a climb-out and nothing else ends
    # the contact. This is precisely what produced the five bogus carrier
    # "landings" in production. Being below the landing surface is the one
    # unambiguous disqualifier, so it is tracked separately.
    below_deck = [
        on_deck
        and (agl := compute_agl(s, surface, config, trust_sample_agl=False)) is not None
        and agl < -config.wow_agl_threshold_m
        for s, (surface, on_deck) in zip(samples, surfaces)
    ]

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
        # A deck contact that is followed by the object sinking below the
        # deck never happened: it passed the ship's height on its way into
        # the sea. Stop before it becomes a "full stop" arrestment.
        if _sinks_through_deck(below_deck, wow, index):
            continue
        # Merge bounces: absorb a later contact only when the aircraft left
        # the deck in between but never climbed above bounce_merge_agl_m.
        last_index = index
        airborne_since_contact = False
        peak_agl = 0.0
        for j in range(index + 1, len(samples)):
            surface_j, on_deck_j = surfaces[j]
            agl = compute_agl(samples[j], surface_j, config, trust_sample_agl=not on_deck_j)
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

        surface, on_deck = surfaces[last_index]
        touchdown = _make_touchdown(samples, last_index, surface, surface_is_deck=on_deck)
        outcome, climb_index = _classify_outcome(samples, wow, index, last_index, config)

        carrier = _nearest_carrier(touchdown, carriers, config)
        kind = "carrier" if carrier is not None else "land"
        if kind == "carrier" and outcome == "touch_and_go":
            outcome = "bolter"

        approach = _cut_approach(samples, index, touchdown, config, kind, wow)
        approach_pattern = _classify_approach_pattern(approach)
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
            first_contact_time=samples[index].time,
            approach_pattern=approach_pattern,
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


def _sinks_through_deck(
    below_deck: list[bool],
    wow: list[bool | None],
    index: int,
) -> bool:
    """Did this contact end by going UNDER the deck rather than staying on it?

    Scans forward from the contact until the object is clearly airborne
    again (a bolter, which is a real outcome) or the track ends. Reaching
    below-deck first means it never landed on anything.
    """
    for j in range(index, len(below_deck)):
        if below_deck[j]:
            return True
        if wow[j] is False:
            return False  # climbed away: a bolter, judged as one
    return False


def _make_touchdown(
    samples: list[TrackSample],
    index: int,
    ground_altitude_m: float | None,
    *,
    surface_is_deck: bool = False,
) -> Touchdown:
    sample = samples[index]
    return Touchdown(
        surface_is_deck=surface_is_deck,
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
        # "height above the deck" is what this column has always claimed to
        # be; Tacview's own AGL is height above the sea, so it cannot be it.
        agl = compute_agl(
            sample,
            touchdown.ground_altitude_m,
            config,
            trust_sample_agl=not touchdown.surface_is_deck,
        )
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

    def iter_reverse(self) -> Iterator[TrackSample]:
        """Iterate newest-first without copying the buffer (Issue #47).

        ``snapshot`` allocates a full O(n) list; callers that only walk back a
        few samples (e.g. the ground-speed baseline search) pay that per
        update line. ``reversed`` over the deque is O(1) to start and stops
        as soon as the caller is done.
        """
        return reversed(self._samples)

    def last(self) -> TrackSample | None:
        return self._samples[-1] if self._samples else None
