"""Simple land-aerodrome landing grader (FR-4).

Produces a letter grade (A..E) plus a Japanese comment from four weighted
components:

- touchdown descent rate (fpm),
- touchdown speed relative to the mean final-approach speed
  (airframe independent),
- glideslope tracking over the final seconds,
- centerline keeping over the final segment.

Every component score carries its evidence values so the UI can show *why*
the grade was given.
"""

from __future__ import annotations

import math

from dataclasses import dataclass, field
from typing import Any

from app.grading.deviations import ApproachAnalysis, DeviationSample

MS_TO_FPM = 60.0 / 0.3048  # ~196.85
M_TO_FT = 1.0 / 0.3048     # ~3.281


@dataclass
class ComponentScore:
    name: str
    score: float          # 0..100
    weight: float
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class LandGradeResult:
    grade: str            # "A".."E"
    score: float          # weighted total 0..100
    comment: str
    components: list[ComponentScore]
    metrics: dict[str, Any]

    def factors_payload(self) -> list[dict[str, Any]]:
        return [
            {
                "name": c.name,
                "score": round(c.score, 1),
                "weight": c.weight,
                "evidence": c.evidence,
            }
            for c in self.components
        ]


def _descent_rate_score(fpm: float, bands: dict[str, Any]) -> tuple[float, str]:
    if fpm <= bands["excellent"]:
        return 100.0, "very smooth"
    if fpm <= bands["good"]:
        return 80.0, "good"
    if fpm <= bands["fair"]:
        return 55.0, "somewhat hard"
    if fpm <= bands["hard"]:
        return 30.0, "hard"
    return 5.0, "extremely hard"


def _speed_ratio_score(ratio: float | None, bands: dict[str, Any]) -> tuple[float, str]:
    if ratio is None:
        return 50.0, "unknown"
    deviation = abs(ratio - 1.0)
    if deviation <= bands["good_band"]:
        return 100.0, "on speed"
    if deviation <= bands["fair_band"]:
        return 65.0, "slightly off speed"
    return 30.0, "off speed"


def _band_score(value: float, good: float, fair: float, poor: float) -> float:
    """Linear 100 -> 0 score across good/fair/poor thresholds."""
    if value <= good:
        return 100.0
    if value >= poor:
        return 5.0
    if value <= fair:
        # good..fair maps to 100..55
        frac = (value - good) / (fair - good)
        return 100.0 - frac * 45.0
    # fair..poor maps to 55..5
    frac = (value - fair) / (poor - fair)
    return 55.0 - frac * 50.0


def _reference_label(analysis: ApproachAnalysis) -> str:
    """Whether the slope was judged against a real runway or an estimate."""
    geometry = analysis.geometry or {}
    if geometry.get("kind") == "runway":
        return f"runway {geometry.get('airbase', '?')} {geometry.get('name', '?')}"
    return "touchdown-estimated"


def _glideslope_window(
    analysis: ApproachAnalysis, settings: dict[str, Any]
) -> list[DeviationSample]:
    """Samples to judge the glidepath on: the approach up to the threshold.

    Past the threshold the aircraft is flaring, and being *above* a slope
    anchored at the aiming point is then the correct thing to do -- grading
    it against the slope would penalise a good landing. Touchdown quality is
    already covered by the descent-rate component.

    With a resolved runway the cut is the real threshold. Without one, an
    AGL floor (about the usual threshold crossing height) approximates it.

    Returns an empty list when there is no usable approach to judge -- a
    hover-on, a bounce, a recording that only caught the last few metres.
    The caller then scores the component neutrally, which is the honest
    answer: falling back to whatever samples exist would compute an angle
    from a handful of metres of distance and report tens of degrees of
    "error" for a landing nobody flew badly.
    """
    candidates = analysis.window(float(settings.get("glideslope_window_s", 30.0)))
    # Angles derived from a few metres of distance are meaningless whatever
    # the reference, so this floor applies to both paths below.
    min_distance = float(settings.get("glideslope_min_distance_m", 200.0))
    candidates = [s for s in candidates if s.distance_to_go >= min_distance]

    with_threshold = [s for s in candidates if s.distance_to_threshold is not None]
    if with_threshold:
        return [s for s in with_threshold if s.distance_to_threshold > 0.0]
    floor = float(settings.get("glideslope_min_agl_m", 15.0))
    return [s for s in candidates if s.agl is not None and s.agl >= floor]


def _glideslope_errors(
    window: list[DeviationSample], reference_slope_deg: float
) -> tuple[float | None, float | None, str]:
    """Angular glidepath error over ``window``: (mean abs, mean signed, method).

    Two cases, because the honest metric differs:

    - With a resolved runway the reference is anchored at the real aiming
      point, so each sample's angle to it is meaningful and the mean of the
      per-sample errors measures how far off the published glidepath the
      aircraft actually was.

    - Without one, the only anchor available is the touchdown point, which
      sits wherever the aircraft happened to float to. Measuring angles to it
      reports a low approach for every landing with a normal flare -- an
      observed 856 m float alone accounts for a 45 m / 1.5 deg "error" on a
      textbook 2.9 deg approach. So instead fit the glidepath the aircraft
      actually flew (least squares of height against distance) and compare
      *that* angle to the reference. It cannot see a parallel displacement,
      but it is not fabricating one either.
    """
    if not window:
        return None, None, "none"

    if any(s.distance_to_threshold is not None for s in window):
        errors = [
            e
            for e in (s.glideslope_error_deg(reference_slope_deg) for s in window)
            if e is not None
        ]
        if not errors:
            return None, None, "none"
        return (
            sum(abs(e) for e in errors) / len(errors),
            sum(errors) / len(errors),
            "aiming-point",
        )

    points = [
        (s.distance_to_go, s.agl)
        for s in window
        if s.agl is not None and s.distance_to_go > 0
    ]
    if len(points) < 3:
        return None, None, "none"
    count = len(points)
    sum_x = sum(x for x, _ in points)
    sum_y = sum(y for _, y in points)
    sum_xx = sum(x * x for x, _ in points)
    sum_xy = sum(x * y for x, y in points)
    denominator = count * sum_xx - sum_x * sum_x
    if abs(denominator) < 1e-9:
        return None, None, "none"
    slope = (count * sum_xy - sum_x * sum_y) / denominator
    intercept = (sum_y - slope * sum_x) / count
    trend_error = math.degrees(math.atan(slope)) - reference_slope_deg

    # A fit only describes the trend, so on its own it would call an approach
    # that porpoised +/-30 m around a perfect 3 deg line "perfect" -- and the
    # advice a wandering approach needs is "stabilise", not "you were high".
    # Measure the scatter about the fit as an angle too, per sample, since the
    # same metre of wobble matters far more close in than at 2 nm.
    residual_angles = [
        math.degrees(math.atan((y - (slope * x + intercept)) / x)) for x, y in points
    ]
    scatter = math.sqrt(sum(a * a for a in residual_angles) / count)
    # Trend and scatter are independent contributions to being off the slope.
    return math.hypot(trend_error, scatter), trend_error, "path-angle"


def _centerline_overshoot_m(analysis: ApproachAnalysis) -> float | None:
    """How far the aircraft went *through* the centreline before touchdown.

    Recorded, not scored: an overshoot on the roll-out from a turn to final
    is a real pattern error (and the classic overhead-break mistake), but
    there is not yet enough overhead-pattern data here to calibrate a
    threshold, so it would be guesswork to attach points to it.
    """
    values = [
        s.centerline_deviation
        for s in analysis.samples
        if s.centerline_deviation is not None and s.time < analysis.touchdown_time
    ]
    if len(values) < 2:
        return None
    # The side the approach came in on: the aircraft overshoots when it
    # crosses to the opposite side of that.
    approach_side = 1.0 if values[0] >= 0 else -1.0
    excursion = max((-approach_side * v for v in values), default=0.0)
    return round(max(0.0, excursion), 2)


def grade_land_landing(
    analysis: ApproachAnalysis,
    config: Any,
) -> LandGradeResult:
    """Grade a land landing; ``config`` is a :class:`GradingConfig`."""
    settings = config.land_grading
    weights = settings["weights"]

    descent_fpm = analysis.touchdown_descent_rate_ms * MS_TO_FPM
    rate_score, rate_label = _descent_rate_score(descent_fpm, settings["descent_rate_fpm"])

    approach_speeds = [s.speed for s in analysis.samples if s.speed is not None]
    mean_speed = sum(approach_speeds) / len(approach_speeds) if approach_speeds else None
    speed_ratio = (
        analysis.touchdown_speed_ms / mean_speed
        if analysis.touchdown_speed_ms is not None and mean_speed
        else None
    )
    speed_score, speed_label = _speed_ratio_score(
        speed_ratio, settings["touchdown_speed_ratio"]
    )

    gs_window = _glideslope_window(analysis, settings)
    # 絶対値と符号付きを分けて持つ: 絶対値は上下に振れた進入が相殺されて
    # 「完璧」にならないようにするため、符号付きは「高め / 低め」の向きを
    # 述べるため (絶対値平均は常に非負なので向きを判定できない)。
    mean_gs_err, mean_signed_gs_err, gs_method = _glideslope_errors(
        gs_window, analysis.glideslope_deg
    )
    # メートル値も参考として残す (UI の既存表示・過去データとの比較用)。
    gs_devs = [
        s.glideslope_deviation for s in gs_window if s.glideslope_deviation is not None
    ]
    mean_gs_dev = sum(abs(d) for d in gs_devs) / len(gs_devs) if gs_devs else None
    mean_signed_gs_dev = sum(gs_devs) / len(gs_devs) if gs_devs else None
    gs_bands = settings["glideslope_error_deg"]
    gs_score = (
        _band_score(mean_gs_err, gs_bands["good"], gs_bands["fair"], gs_bands["poor"])
        if mean_gs_err is not None
        else 50.0
    )

    # センターラインは接地直前の短い窓で採る。長い窓の max だと、窓の先頭
    # (まだ 1.5km 手前で正当に修正中) の 1 サンプルで点数が決まってしまい、
    # 旋回明けが遅いオーバーヘッド進入を進入方式そのもので減点することになる。
    cl_window_s = float(settings.get("centerline_window_s", 5.0))
    cl_values = [
        abs(s.centerline_deviation)
        for s in analysis.window(cl_window_s)
        if s.centerline_deviation is not None
    ]
    max_cl_dev = max(cl_values) if cl_values else None
    cl_bands = settings["centerline_deviation_m"]
    cl_score = (
        _band_score(max_cl_dev, cl_bands["good"], cl_bands["fair"], cl_bands["poor"])
        if max_cl_dev is not None
        else 50.0
    )
    overshoot_m = _centerline_overshoot_m(analysis)

    components = [
        ComponentScore(
            "descent_rate",
            rate_score,
            weights["descent_rate"],
            {"touchdown_descent_rate_fpm": round(descent_fpm, 1), "verdict": rate_label},
        ),
        ComponentScore(
            "touchdown_speed",
            speed_score,
            weights["touchdown_speed"],
            {
                "touchdown_speed_ms": analysis.touchdown_speed_ms,
                "mean_approach_speed_ms": (
                    round(mean_speed, 2) if mean_speed is not None else None
                ),
                "speed_ratio": round(speed_ratio, 3) if speed_ratio is not None else None,
                "verdict": speed_label,
            },
        ),
        ComponentScore(
            "glideslope",
            gs_score,
            weights["glideslope"],
            {
                # 採点はこの角度誤差で行う (メートルは距離依存で比較不能)。
                "mean_abs_error_deg": (
                    round(mean_gs_err, 3) if mean_gs_err is not None else None
                ),
                # 符号付き: 正 = 理想より上。講評の「高め / 低め」の根拠。
                "mean_signed_error_deg": (
                    round(mean_signed_gs_err, 3)
                    if mean_signed_gs_err is not None
                    else None
                ),
                "mean_abs_deviation_m": (
                    round(mean_gs_dev, 2) if mean_gs_dev is not None else None
                ),
                "mean_signed_deviation_m": (
                    round(mean_signed_gs_dev, 2)
                    if mean_signed_gs_dev is not None
                    else None
                ),
                "glideslope_deg": analysis.glideslope_deg,
                "samples": len(gs_window),
                "method": gs_method,
                "reference": _reference_label(analysis),
            },
        ),
        ComponentScore(
            "centerline",
            cl_score,
            weights["centerline"],
            {
                "max_abs_deviation_m": (
                    round(max_cl_dev, 2) if max_cl_dev is not None else None
                ),
                "window_s": cl_window_s,
                # 記録のみ (採点には未使用)。
                "overshoot_m": overshoot_m,
            },
        ),
    ]

    total = sum(c.score * c.weight for c in components)
    letters = settings["letters"]
    grade = "E"
    for letter in ("A", "B", "C", "D"):
        if total >= letters[letter]:
            grade = letter
            break

    comment = _build_comment(
        grade, rate_label, speed_label, mean_gs_err, mean_signed_gs_err, max_cl_dev
    )
    metrics = {
        "touchdown_descent_rate_fpm": round(descent_fpm, 1),
        "touchdown_speed_ratio": round(speed_ratio, 3) if speed_ratio is not None else None,
        "mean_glideslope_error_deg": (
            round(mean_gs_err, 3) if mean_gs_err is not None else None
        ),
        "mean_signed_glideslope_error_deg": (
            round(mean_signed_gs_err, 3) if mean_signed_gs_err is not None else None
        ),
        "mean_glideslope_deviation_m": (
            round(mean_gs_dev, 2) if mean_gs_dev is not None else None
        ),
        "mean_signed_glideslope_deviation_m": (
            round(mean_signed_gs_dev, 2) if mean_signed_gs_dev is not None else None
        ),
        "max_centerline_deviation_m": round(max_cl_dev, 2) if max_cl_dev is not None else None,
        "centerline_overshoot_m": overshoot_m,
        "glideslope_reference": _reference_label(analysis),
        "glideslope_method": gs_method,
        "outcome": analysis.outcome,
    }
    return LandGradeResult(
        grade=grade,
        score=round(total, 1),
        comment=comment,
        components=components,
        metrics=metrics,
    )


def _build_comment(
    grade: str,
    rate_label: str,
    speed_label: str,
    mean_gs_err: float | None,
    mean_signed_gs_err: float | None,
    max_cl_dev: float | None,
) -> str:
    # 横ずれは ft で述べる (Issue D-4)。グライドスロープは角度で述べる:
    # ft だと同じ操縦精度でも遠いほど大きな数字になり、比較にならない。
    parts: list[str] = []
    parts.append(f"接地は{rate_label}（降下率ベース）")
    parts.append(f"速度は{speed_label}")
    if mean_gs_err is not None and mean_signed_gs_err is not None:
        if abs(mean_signed_gs_err) < mean_gs_err / 2:
            # 上下に振れていて一方向に寄っていない。この状態で「高め」「低め」と
            # 言い切ると、実際にやるべき修正 (安定させること) を取り違えさせる。
            parts.append(
                f"閾値までのグライドスロープは上下にばらついた"
                f"（平均誤差 {mean_gs_err:.2f}°）"
            )
        else:
            direction = "高め" if mean_signed_gs_err > 0 else "低め"
            parts.append(
                f"閾値までのグライドスロープは理想より{direction}"
                f"（平均 {abs(mean_signed_gs_err):.2f}°）"
            )
    if max_cl_dev is not None and max_cl_dev > 5.0:
        parts.append(f"センターラインから最大 {max_cl_dev * M_TO_FT:.0f} ft ずれた")
    verdicts = {
        "A": "見事な着陸です。",
        "B": "良好な着陸です。",
        "C": "まずまずの着陸です。改善点を確認しましょう。",
        "D": "着陸に難があります。進入の安定性を意識しましょう。",
        "E": "着陸は危ういものでした。基本の手順を見直しましょう。",
    }
    return "、".join(parts) + "。" + verdicts[grade]
