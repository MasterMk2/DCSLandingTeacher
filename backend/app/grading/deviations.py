"""Shared approach-segment deviation math (FR-3 / FR-4).

Given the cut-out final-approach segment and the touchdown point, computes
per-sample:

- ``distance_to_go``      : meters to the touchdown point along the course,
- ``glideslope_deviation``: meters above (+) / below (-) the ideal slope
  referenced to the touchdown elevation ("ramp reference"),
- ``centerline_deviation``: signed lateral offset, right of course (+).

The course is taken from the aircraft heading at touchdown (falling back to
the mean inbound track). All positions are projected onto a local tangent
plane via :func:`app.detection.geometry.transform_to_frame`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.detection.detector import LandingEvent, TrackSample
from app.detection.geometry import transform_to_frame


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
            samples=samples,
        )


def estimate_course_deg(samples: list[TrackSample], fallback_heading: float | None) -> float:
    """Approach course from the inbound track; falls back to TD heading."""
    points = [
        (s.latitude, s.longitude)
        for s in samples
        if s.latitude is not None and s.longitude is not None
    ]
    if len(points) >= 2:
        # Use the first and last points of the inbound segment.
        lat0, lon0 = points[0]
        lat1, lon1 = points[-1]
        dx = math.radians(lon1 - lon0) * math.cos(math.radians((lat0 + lat1) / 2))
        dy = math.radians(lat1 - lat0)
        bearing = math.degrees(math.atan2(dx, dy)) % 360.0
        if (lat0, lon0) != (lat1, lon1):
            return bearing
    return fallback_heading if fallback_heading is not None else 0.0


def build_approach_analysis(
    event: LandingEvent,
    glideslope_deg: float,
    ground_altitude_m: float | None = None,
) -> ApproachAnalysis:
    """Compute the deviation time series for one detected landing event."""
    touchdown = event.touchdown
    deck_elevation = (
        ground_altitude_m if ground_altitude_m is not None else touchdown.ground_altitude_m
    )
    course = estimate_course_deg(event.approach, touchdown.heading)

    analysis = ApproachAnalysis(
        kind=event.kind,
        outcome=event.outcome,
        glideslope_deg=glideslope_deg,
        course_deg=course,
        touchdown_time=touchdown.time,
        touchdown_speed_ms=touchdown.speed,
        touchdown_descent_rate_ms=touchdown.descent_rate_ms,
    )

    tan_slope = math.tan(math.radians(glideslope_deg))
    for sample in event.approach:
        if sample.latitude is None or sample.longitude is None:
            continue
        along, lateral = transform_to_frame(
            sample.latitude, sample.longitude, touchdown.latitude, touchdown.longitude, course
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
