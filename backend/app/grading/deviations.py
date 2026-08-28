"""Shared approach-segment deviation math (FR-3 / FR-4).

Given the cut-out final-approach segment and the touchdown point, computes
per-sample:

- ``distance_to_go``      : meters to the reference point along the course,
- ``glideslope_deviation``: meters above (+) / below (-) the ideal slope,
- ``centerline_deviation``: signed lateral offset, right of course (+).

Two reference frames exist (Issue #3):

- With resolved per-carrier FLOLS geometry the deviations are referenced to
  the RAMP (FLOLS datum on the angled deck): origin = ship position plus
  ramp offsets at the touchdown instant, course = ship heading + angled-deck
  offset, AGL measured against the deck altitude, slope from the geometry.
- Without it (unknown carrier) the legacy approximation applies: everything
  is referenced to the touchdown point itself.

All positions are projected onto a local tangent plane via
:func:`app.detection.geometry.transform_to_frame`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from app.detection.detector import LandingEvent, TrackSample
from app.detection.geometry import offset_position, transform_to_frame
from app.grading.carriers import FlolsGeometry
from app.runways.models import DEFAULT_AIMING_POINT_M, Runway


@dataclass
class DeviationSample:
    time: float
    distance_to_go: float
    glideslope_deviation: float | None
    centerline_deviation: float | None
    speed: float | None = None
    aoa: float | None = None
    agl: float | None = None
    #: Metres still to fly to the runway threshold; negative once the
    #: aircraft is over the runway. ``None`` when no runway is known, which
    #: is what tells the grader to fall back to an AGL-based cut-off.
    distance_to_threshold: float | None = None

    def as_dict(self) -> dict:
        return {
            "time": round(self.time, 3),
            "distance_to_go": round(self.distance_to_go, 2),
            "glideslope_deviation": (
                round(self.glideslope_deviation, 2)
                if self.glideslope_deviation is not None
                else None
            ),
            "centerline_deviation": (
                round(self.centerline_deviation, 2)
                if self.centerline_deviation is not None
                else None
            ),
            "speed": self.speed,
            "aoa": self.aoa,
            "agl": round(self.agl, 2) if self.agl is not None else None,
            "distance_to_threshold": (
                round(self.distance_to_threshold, 2)
                if self.distance_to_threshold is not None
                else None
            ),
        }

    def glideslope_error_deg(self, reference_slope_deg: float) -> float | None:
        """Angular glidepath error, degrees (positive = above the slope).

        Deviation in metres is not comparable between samples: the same
        angular error is a much larger distance far out than close in, so
        grading absolute metres against fixed thresholds effectively judges
        a 1.5 km-out sample by ILS Cat III tolerances. Aviation references
        the slope angularly (PAPI, ILS), so graders do too.
        """
        if self.agl is None or self.distance_to_go <= 1.0:
            return None
        actual = math.degrees(math.atan2(self.agl, self.distance_to_go))
        return actual - reference_slope_deg


@dataclass
class ApproachAnalysis:
    """Deviations and state over the final approach, ready for graders."""

    kind: str                       # "carrier" | "land"
    outcome: str                    # "full_stop" | "touch_and_go" | "bolter"
    glideslope_deg: float
    course_deg: float
    touchdown_time: float
    touchdown_speed_ms: float | None
    touchdown_descent_rate_ms: float
    samples: list[DeviationSample] = field(default_factory=list)
    #: Per-carrier FLOLS geometry used for this analysis (Issue #3).
    #: ``None`` means the legacy touchdown-referenced approximation.
    geometry: dict[str, Any] | None = None
    #: Crosswind crab angle (touchdown heading - stabilized ground track) in
    #: degrees for land landings, or ``None`` when it cannot be derived. A
    #: non-trivial value means the runway course had to be taken from the
    #: ground track rather than the heading (Issue #26).
    crosswind_crab_deg: float | None = None

    def window(self, seconds_before_touchdown: float) -> list[DeviationSample]:
        """Samples strictly before touchdown within the given lookback.

        The touchdown sample itself (distance-to-go == 0, deviation == 0 by
        construction) is excluded so it does not dilute the at-the-ramp
        evaluation.
        """
        limit = self.touchdown_time - seconds_before_touchdown
        return [
            s for s in self.samples if limit <= s.time < self.touchdown_time
        ]

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "outcome": self.outcome,
            "glideslope_deg": self.glideslope_deg,
            "course_deg": round(self.course_deg, 2),
            "touchdown_time": self.touchdown_time,
            "touchdown_speed_ms": self.touchdown_speed_ms,
            "touchdown_descent_rate_ms": round(self.touchdown_descent_rate_ms, 3),
            "geometry": self.geometry,
            "crosswind_crab_deg": self.crosswind_crab_deg,
            "samples": [s.as_dict() for s in self.samples],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ApproachAnalysis":
        """Rebuild an analysis from :meth:`as_dict` output (re-grading).

        Validates the stored JSON up front (Issue #44) so a corrupt
        ``approach_track`` fails with a clear error instead of an opaque
        ``TypeError`` deep in the grader.
        """
        if not isinstance(data, dict):
            raise ValueError("approach_track must be an object")
        raw_samples = data.get("samples")
        if not isinstance(raw_samples, list):
            raise ValueError("approach_track.samples must be a list")
        samples: list[DeviationSample] = []
        for index, row in enumerate(raw_samples):
            if not isinstance(row, dict):
                raise ValueError(f"approach_track.samples[{index}] must be an object")
            try:
                samples.append(
                    DeviationSample(
                        time=float(row["time"]),
                        distance_to_go=float(row["distance_to_go"]),
                        glideslope_deviation=(
                            float(row["glideslope_deviation"])
                            if row.get("glideslope_deviation") is not None
                            else None
                        ),
                        centerline_deviation=(
                            float(row["centerline_deviation"])
                            if row.get("centerline_deviation") is not None
                            else None
                        ),
                        speed=row.get("speed"),
                        aoa=row.get("aoa"),
                        agl=row.get("agl"),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"approach_track.samples[{index}] invalid: {exc}"
                ) from exc
        try:
            return cls(
                kind=data["kind"],
                outcome=data["outcome"],
                glideslope_deg=float(data["glideslope_deg"]),
                course_deg=float(data.get("course_deg", 0.0)),
                touchdown_time=float(data["touchdown_time"]),
                touchdown_speed_ms=(
                    float(data["touchdown_speed_ms"])
                    if data.get("touchdown_speed_ms") is not None
                    else None
                ),
                touchdown_descent_rate_ms=float(data.get("touchdown_descent_rate_ms", 0.0)),
                geometry=data.get("geometry"),
                crosswind_crab_deg=data.get("crosswind_crab_deg"),
                samples=samples,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"approach_track missing/invalid field: {exc}") from exc


#: Final stabilized segment (seconds before touchdown) used to estimate the
#: runway/landing course for land landings. A long two-point bearing across
#: the whole captured approach picks up the turn onto final; the last ~12 s
#: are assumed stabilized on a straight final whose ground track equals the
#: runway heading.
STABILIZED_FINAL_S = 12.0


def _position_bearing(
    lat0: float, lon0: float, lat1: float, lon1: float
) -> float | None:
    if (lat0, lon0) == (lat1, lon1):
        return None
    dx = math.radians(lon1 - lon0) * math.cos(math.radians((lat0 + lat1) / 2))
    dy = math.radians(lat1 - lat0)
    return math.degrees(math.atan2(dx, dy)) % 360.0


def _stabilized_track_course(
    samples: list[TrackSample], window_s: float = STABILIZED_FINAL_S
) -> float | None:
    """Ground-track bearing over the final ``window_s`` of the approach.

    The stabilized final is a straight line whose track equals the runway
    course; using it avoids both the turn-onto-final curvature and the
    crosswind crab angle that would contaminate a heading-based estimate.
    """
    pts = [
        (s.latitude, s.longitude, s.time)
        for s in samples
        if s.latitude is not None and s.longitude is not None
    ]
    if len(pts) < 2:
        return None
    t_last = pts[-1][2]
    t_cut = t_last - window_s
    seg = [p for p in pts if p[2] >= t_cut]
    if len(seg) < 2:
        # Not enough data inside the window; fall back to the whole segment.
        seg = pts
    lat0, lon0, _ = seg[0]
    lat1, lon1, _ = seg[-1]
    return _position_bearing(lat0, lon0, lat1, lon1)


def _angular_diff(a: float, b: float) -> float:
    """Signed smallest difference a - b in degrees, in (-180, 180]."""
    return (a - b + 180.0) % 360.0 - 180.0


def estimate_course_deg(
    samples: list[TrackSample],
    touchdown_heading: float | None,
    kind: str = "carrier",
) -> float:
    """Approach course estimate.

    For land landings the runway course equals the aircraft's *ground track*
    on a stabilized final, not its heading: in a crosswind the aircraft crabs
    into wind so heading diverges from track, and using heading would read as
    a false centerline deviation (Issue #26). The stabilized-final track is
    therefore preferred; the touchdown heading is only a last-resort fallback
    when the track cannot be estimated at all.

    For carrier approaches without FLOLS geometry the touchdown heading is
    kept: the aircraft de-crabs and aligns with the angled deck at the moment
    of touchdown, so heading is the better read of the deck course even
    through a turn onto final.
    """
    if kind == "land":
        track = _stabilized_track_course(samples)
        if track is not None:
            return track
        if touchdown_heading is not None:
            return touchdown_heading
        return 0.0
    if touchdown_heading is not None:
        return touchdown_heading
    points = [
        (s.latitude, s.longitude)
        for s in samples
        if s.latitude is not None and s.longitude is not None
    ]
    if len(points) >= 2:
        # Use the first and last points of the inbound segment.
        lat0, lon0 = points[0]
        lat1, lon1 = points[-1]
        if (lat0, lon0) != (lat1, lon1):
            dx = math.radians(lon1 - lon0) * math.cos(math.radians((lat0 + lat1) / 2))
            dy = math.radians(lat1 - lat0)
            return math.degrees(math.atan2(dx, dy)) % 360.0
    return 0.0


def build_approach_analysis(
    event: LandingEvent,
    glideslope_deg: float,
    ground_altitude_m: float | None = None,
    geometry: FlolsGeometry | None = None,
    runway: Runway | None = None,
    aiming_point_m: float = DEFAULT_AIMING_POINT_M,
) -> ApproachAnalysis:
    """Compute the deviation time series for one detected landing event.

    With ``geometry`` (Issue #3) the deviations are referenced to the
    carrier's FLOLS ramp: the origin is the ramp position at the touchdown
    instant (ship position + along/lateral offsets), the course is the ship
    heading plus the angled-deck offset, AGL is measured against the deck
    altitude from the geometry and the slope angle comes from the geometry
    as well.

    With ``runway`` (land landings) the origin is the runway's *aiming
    point* -- ``aiming_point_m`` past the threshold -- and the course is the
    published runway heading. This matters: anchoring at the touchdown point
    instead makes the reference line move with however long the pilot
    floats in the flare, which reads as a systematic "low" bias on every
    correctly flown approach (a 600 m float alone shifts the whole approach
    down by 600 m * tan 3 deg = 31 m).

    Without either, the legacy approximation applies: everything is
    referenced to the touchdown point and a course estimated from it.
    """
    touchdown = event.touchdown
    threshold_along: float | None = None

    if geometry is not None and event.carrier_latitude is not None:
        course = (
            (event.carrier_heading_deg or 0.0) + geometry.landing_course_offset_deg
        ) % 360.0
        ref_lat, ref_lon = offset_position(
            event.carrier_latitude,
            event.carrier_longitude,
            event.carrier_heading_deg or 0.0,
            geometry.ramp_along_m,
            geometry.ramp_lateral_m,
        )
        deck_elevation: float | None = geometry.deck_altitude_m
        slope_deg = geometry.glideslope_deg
        geometry_payload = geometry.as_dict()
    elif runway is not None:
        course = runway.heading_deg
        ref_lat, ref_lon = runway.aiming_point(aiming_point_m)
        deck_elevation = runway.elevation_m
        slope_deg = glideslope_deg
        geometry_payload = {
            "kind": "runway",
            "aiming_point_m": aiming_point_m,
            **runway.as_dict(),
        }
        # The threshold sits ``aiming_point_m`` short of the origin, so a
        # sample's distance-to-threshold is its distance-to-go minus that.
        threshold_along = aiming_point_m
    else:
        course = estimate_course_deg(
            event.approach, touchdown.heading, kind=event.kind
        )
        ref_lat, ref_lon = touchdown.latitude, touchdown.longitude
        deck_elevation = (
            ground_altitude_m if ground_altitude_m is not None else touchdown.ground_altitude_m
        )
        slope_deg = glideslope_deg
        geometry_payload = None

    crosswind_crab_deg: float | None = None
    if event.kind == "land" and touchdown.heading is not None:
        crosswind_crab_deg = _angular_diff(touchdown.heading, course)

    analysis = ApproachAnalysis(
        kind=event.kind,
        outcome=event.outcome,
        glideslope_deg=slope_deg,
        course_deg=course,
        touchdown_time=touchdown.time,
        touchdown_speed_ms=touchdown.speed,
        touchdown_descent_rate_ms=touchdown.descent_rate_ms,
        geometry=geometry_payload,
        crosswind_crab_deg=crosswind_crab_deg,
    )

    tan_slope = math.tan(math.radians(slope_deg))
    for sample in event.approach:
        if sample.latitude is None or sample.longitude is None:
            continue
        along, lateral = transform_to_frame(
            sample.latitude, sample.longitude, ref_lat, ref_lon, course
        )
        distance_to_go = max(0.0, -along)
        distance_to_threshold = (
            -along - threshold_along if threshold_along is not None else None
        )

        agl: float | None
        if runway is not None and sample.altitude is not None:
            # Height above the *runway*, not above whatever terrain happens
            # to be under the aircraft: DCS's own AGL follows the ground, so
            # over a valley or a ridge on final it does not describe the
            # aircraft's position relative to the landing surface at all.
            agl = sample.altitude - deck_elevation
        elif sample.agl is not None:
            agl = sample.agl
        elif sample.altitude is not None and deck_elevation is not None:
            agl = sample.altitude - deck_elevation
        else:
            agl = None

        gs_dev = agl - (distance_to_go * tan_slope) if agl is not None else None
        analysis.samples.append(
            DeviationSample(
                time=sample.time,
                distance_to_go=distance_to_go,
                glideslope_deviation=gs_dev,
                centerline_deviation=lateral,
                speed=sample.speed,
                aoa=sample.aoa,
                agl=agl,
                distance_to_threshold=distance_to_threshold,
            )
        )
    return analysis
