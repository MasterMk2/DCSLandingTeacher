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


@dataclass
class DeviationSample:
    time: float
    distance_to_go: float
    glideslope_deviation: float | None
    centerline_deviation: float | None
    speed: float | None = None
    aoa: float | None = None
    agl: float | None = None

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
        }


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
            "samples": [s.as_dict() for s in self.samples],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ApproachAnalysis":
        """Rebuild an analysis from :meth:`as_dict` output (re-grading)."""
        samples = [
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
            for row in data.get("samples", [])
        ]
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
            samples=samples,
        )


def estimate_course_deg(samples: list[TrackSample], touchdown_heading: float | None) -> float:
    """Approach course, preferring the aircraft's own heading at touchdown.

    Real DCS landings are frequently flown as a continuous turn onto short
    final (an overhead break, a tactical initial) rather than a long
    stabilized straight-in. A two-point position bearing taken across the
    whole ~60s/2nm captured approach picks up that turn and can report a
    course tens of degrees off the actual runway heading, which then shows
    up as a large false centerline deviation for samples that were in fact
    close to the centerline. The aircraft's own heading at the moment of
    touchdown is the most direct read of the runway course a successful
    landing implies, so it takes priority whenever ACMI supplied it; the
    position-based bearing is only a fallback for the rare case heading
    telemetry itself is missing.
    """
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
) -> ApproachAnalysis:
    """Compute the deviation time series for one detected landing event.

    With ``geometry`` (Issue #3) the deviations are referenced to the
    carrier's FLOLS ramp: the origin is the ramp position at the touchdown
    instant (ship position + along/lateral offsets), the course is the ship
    heading plus the angled-deck offset, AGL is measured against the deck
    altitude from the geometry and the slope angle comes from the geometry
    as well. Without it the legacy approximation applies: everything is
    referenced to the touchdown point itself.
    """
    touchdown = event.touchdown

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
    else:
        course = estimate_course_deg(event.approach, touchdown.heading)
        ref_lat, ref_lon = touchdown.latitude, touchdown.longitude
        deck_elevation = (
            ground_altitude_m if ground_altitude_m is not None else touchdown.ground_altitude_m
        )
        slope_deg = glideslope_deg
        geometry_payload = None

    analysis = ApproachAnalysis(
        kind=event.kind,
        outcome=event.outcome,
        glideslope_deg=slope_deg,
        course_deg=course,
        touchdown_time=touchdown.time,
        touchdown_speed_ms=touchdown.speed,
        touchdown_descent_rate_ms=touchdown.descent_rate_ms,
        geometry=geometry_payload,
    )

    tan_slope = math.tan(math.radians(slope_deg))
    for sample in event.approach:
        if sample.latitude is None or sample.longitude is None:
            continue
        along, lateral = transform_to_frame(
            sample.latitude, sample.longitude, ref_lat, ref_lon, course
        )
        distance_to_go = max(0.0, -along)

        agl: float | None
        if sample.agl is not None:
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
            )
        )
    return analysis
