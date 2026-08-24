"""US Navy style LSO grader for carrier approaches (FR-3).

Grades: ``OK`` / ``OK-`` / ``(OK)`` / ``_NO_GRADE_`` / ``CUT``.

Factor detection is evidence based: every emitted factor carries the value,
threshold and time window that triggered it. Factors whose underlying data
is not available from ACMI (ARCON, AOC, AOS, INTAKE, IMMAT, T&R, NWS, OPEN,
BURBLE) are declared in ``config/grading.yaml`` with ``enabled: false`` and
are never emitted by this implementation.

Decision rules (evaluated in order):

1. Bolter / no arrestment            -> ``_NO_GRADE_``
2. Deep LOW (below cut threshold)    -> ``CUT``
3. >= ``cut_if_major_count`` majors  -> ``CUT``
4. == ``ok_paren_major_count``       -> ``(OK)``
5. exactly one major                 -> ``OK-``
6. otherwise                         -> ``OK``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.grading.carriers import fallback_geometry_payload
from app.grading.deviations import ApproachAnalysis


@dataclass
class LsoFactor:
    name: str
    severity: str          # "minor" | "major" | "severe"
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "severity": self.severity, "evidence": self.evidence}


@dataclass
class LsoGradeResult:
    grade: str
    factors: list[LsoFactor]
    comment: str
    metrics: dict[str, Any]

    def factors_payload(self) -> list[dict[str, Any]]:
        return [f.as_dict() for f in self.factors]


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _detect_factors(
    analysis: ApproachAnalysis,
    settings: dict[str, Any],
) -> list[LsoFactor]:
    factors: list[LsoFactor] = []
    factor_cfg = settings["factors"]
    at_ramp = analysis.window(float(settings["at_ramp_window_s"]))

    # --- glideslope at the ramp: HIGH / LOW -------------------------------
    high_cfg = factor_cfg.get("HIGH", {})
    low_cfg = factor_cfg.get("LOW", {})
    gs_ramp = [
        s.glideslope_deviation for s in at_ramp if s.glideslope_deviation is not None
    ]
    mean_gs = _mean(gs_ramp)
    if mean_gs is not None and high_cfg.get("gs_deviation_m") is not None:
        threshold = float(high_cfg["gs_deviation_m"])
        if mean_gs > threshold:
            factors.append(
                LsoFactor(
                    "HIGH",
                    str(high_cfg.get("severity", "major")),
                    {
                        "mean_glideslope_deviation_m": round(mean_gs, 2),
                        "threshold_m": threshold,
                        "window_s": settings["at_ramp_window_s"],
                        "times_s": [round(s.time, 2) for s in at_ramp],
                    },
                )
            )
    if mean_gs is not None and low_cfg.get("gs_deviation_m") is not None:
        threshold = float(low_cfg["gs_deviation_m"])
        if mean_gs < threshold:
            factors.append(
                LsoFactor(
                    "LOW",
                    str(low_cfg.get("severity", "major")),
                    {
                        "mean_glideslope_deviation_m": round(mean_gs, 2),
                        "threshold_m": threshold,
                        "window_s": settings["at_ramp_window_s"],
                        "times_s": [round(s.time, 2) for s in at_ramp],
                    },
                )
            )

    # --- speed at touchdown: FAST / SLOW -----------------------------------
    speeds = [s.speed for s in analysis.samples if s.speed is not None]
    mean_speed = _mean(speeds)
    ratio = (
        analysis.touchdown_speed_ms / mean_speed
        if analysis.touchdown_speed_ms is not None and mean_speed
        else None
    )
    fast_cfg = factor_cfg.get("FAST", {})
    slow_cfg = factor_cfg.get("SLOW", {})
    if ratio is not None and fast_cfg.get("speed_ratio") is not None:
        threshold = float(fast_cfg["speed_ratio"])
        if ratio > threshold:
            factors.append(
                LsoFactor(
                    "FAST",
                    str(fast_cfg.get("severity", "major")),
                    {
                        "touchdown_speed_ms": analysis.touchdown_speed_ms,
                        "mean_approach_speed_ms": round(mean_speed, 2),
                        "speed_ratio": round(ratio, 3),
                        "threshold_ratio": threshold,
                    },
                )
            )
    if ratio is not None and slow_cfg.get("speed_ratio") is not None:
        threshold = float(slow_cfg["speed_ratio"])
        if ratio < threshold:
            factors.append(
                LsoFactor(
                    "SLOW",
                    str(slow_cfg.get("severity", "major")),
                    {
                        "touchdown_speed_ms": analysis.touchdown_speed_ms,
                        "mean_approach_speed_ms": round(mean_speed, 2),
                        "speed_ratio": round(ratio, 3),
                        "threshold_ratio": threshold,
                    },
                )
            )

    # --- centerline at the ramp: OFFLINE ------------------------------------
    offline_cfg = factor_cfg.get("OFFLINE", {})
    cl_ramp = [
        abs(s.centerline_deviation)
        for s in at_ramp
        if s.centerline_deviation is not None
    ]
    max_cl = max(cl_ramp) if cl_ramp else None
    if max_cl is not None and offline_cfg.get("lateral_deviation_m") is not None:
        threshold = float(offline_cfg["lateral_deviation_m"])
        if max_cl > threshold:
            factors.append(
                LsoFactor(
                    "OFFLINE",
                    str(offline_cfg.get("severity", "major")),
                    {
                        "max_lateral_deviation_m": round(max_cl, 2),
                        "threshold_m": threshold,
                        "window_s": settings["at_ramp_window_s"],
                    },
                )
            )

    # --- throttle modulation: POWER ------------------------------------------
    power_cfg = factor_cfg.get("POWER", {})
    power_window = analysis.window(20.0)
    power_speeds = [s.speed for s in power_window if s.speed is not None]
    if (
        power_cfg.get("speed_range_ms") is not None
        and len(power_speeds) >= 2
    ):
        speed_range = max(power_speeds) - min(power_speeds)
        threshold = float(power_cfg["speed_range_ms"])
        if speed_range > threshold:
            factors.append(
                LsoFactor(
                    "POWER",
                    str(power_cfg.get("severity", "minor")),
                    {
                        "speed_range_ms": round(speed_range, 2),
                        "threshold_ms": threshold,
                        "window_s": 20.0,
                    },
                )
            )

    # --- bolter ---------------------------------------------------------------
    bolter_cfg = factor_cfg.get("BOLTER", {})
    if analysis.outcome == "bolter" and bolter_cfg:
        factors.append(
            LsoFactor(
                "BOLTER",
                str(bolter_cfg.get("severity", "major")),
                {"outcome": analysis.outcome},
            )
        )

    return factors


def grade_carrier_approach(
    analysis: ApproachAnalysis,
    config: Any,
) -> LsoGradeResult:
    """Grade a carrier approach; ``config`` is a :class:`GradingConfig`."""
    settings = config.lso_grading
    grades = settings["grades"]
    decision = settings["decision"]

    factors = _detect_factors(analysis, settings)
    majors = [f for f in factors if f.severity == "major"]

    grade = grades["ok"]
    comment = "On centerline, on glidepath, on speed."

    if analysis.outcome == "bolter":
        grade = grades["no_grade"]
        comment = "Bolter: no arrestment."
    else:
        deep_low_threshold = decision.get("cut_low_gs_deviation_m")
        deep_low = any(
            f.name == "LOW"
            and f.evidence.get("mean_glideslope_deviation_m", 0.0) < float(deep_low_threshold)
            for f in factors
        ) if deep_low_threshold is not None else False

        cut_major_count = int(decision.get("cut_if_major_count", 3))
        ok_paren_count = int(decision.get("ok_paren_major_count", 2))

        if deep_low and decision.get("cut_if_severe_low", True):
            grade = grades["cut"]
            comment = "Dangerously low at the ramp; wave-off."
        elif len(majors) >= cut_major_count:
            grade = grades["cut"]
            comment = "Multiple major deviations; unsafe pass."
        elif len(majors) == ok_paren_count:
            grade = grades["ok_paren"]
            comment = "Fair pass with significant deviations."
        elif len(majors) == 1:
            grade = grades["ok_minus"]
            comment = "Safe pass with a deviation to fix."
        elif any(f.severity == "minor" for f in factors):
            grade = grades["ok"]
            comment = "Good pass with minor deviations."

    names = ", ".join(f.name for f in factors) or "no factors"
    metrics = {
        "outcome": analysis.outcome,
        "glideslope_deg": analysis.glideslope_deg,
        "course_deg": round(analysis.course_deg, 2),
        "major_factor_count": len(majors),
        "factor_names": names,
        # Which FLOLS geometry produced this grade (Issue #3): the resolved
        # per-carrier entry or the legacy touchdown-referenced fallback.
        "flols_geometry": (
            analysis.geometry
            if analysis.geometry is not None
            else fallback_geometry_payload()
        ),
    }
    return LsoGradeResult(grade=grade, factors=factors, comment=comment, metrics=metrics)
