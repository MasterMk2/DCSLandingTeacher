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

from dataclasses import dataclass, field
from typing import Any

from app.grading.deviations import ApproachAnalysis

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


def grade_land_landing(
    analysis: ApproachAnalysis,
    config: Any,
) -> LandGradeResult:
    """Grade a land landing; ``config`` is a :class:`GradingConfig`."""
    settings = config.land_grading
    weights = settings["weights"]

    descent_fpm = analysis.touchdown_descent_rate_ms * MS_TO_FPM
    rate_score, rate_label = _descent_rate_score(descent_fpm, settings["descent_rate_fpm"])

    window = analysis.window(15.0)
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

    gs_devs = [
        s.glideslope_deviation for s in window if s.glideslope_deviation is not None
    ]
    # 採点は絶対値の平均で行う。上下に振れた進入が相殺されて「完璧」に
    # ならないようにするため。
    mean_gs_dev = sum(abs(d) for d in gs_devs) / len(gs_devs) if gs_devs else None
    # 高め / 低めの向きは符号付き平均でしか判定できない。絶対値平均は常に
    # 非負なので、それで判定すると「低め」に到達できない。
    mean_signed_gs_dev = sum(gs_devs) / len(gs_devs) if gs_devs else None
    gs_bands = settings["glideslope_deviation_m"]
    gs_score = (
        _band_score(mean_gs_dev, gs_bands["good"], gs_bands["fair"], gs_bands["poor"])
        if mean_gs_dev is not None
        else 50.0
    )

    cl_values = [
        abs(s.centerline_deviation)
        for s in window
        if s.centerline_deviation is not None
    ]
    max_cl_dev = max(cl_values) if cl_values else None
    cl_bands = settings["centerline_deviation_m"]
    cl_score = (
        _band_score(max_cl_dev, cl_bands["good"], cl_bands["fair"], cl_bands["poor"])
        if max_cl_dev is not None
        else 50.0
    )

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
                "mean_abs_deviation_final_15s_m": (
                    round(mean_gs_dev, 2) if mean_gs_dev is not None else None
                ),
                # 符号付き: 正 = 理想より上。講評の「高め / 低め」の根拠。
                "mean_signed_deviation_final_15s_m": (
                    round(mean_signed_gs_dev, 2)
                    if mean_signed_gs_dev is not None
                    else None
                ),
                "glideslope_deg": analysis.glideslope_deg,
            },
        ),
        ComponentScore(
            "centerline",
            cl_score,
            weights["centerline"],
            {"max_abs_deviation_m": round(max_cl_dev, 2) if max_cl_dev is not None else None},
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
        grade, rate_label, speed_label, mean_gs_dev, mean_signed_gs_dev, max_cl_dev
    )
    metrics = {
        "touchdown_descent_rate_fpm": round(descent_fpm, 1),
        "touchdown_speed_ratio": round(speed_ratio, 3) if speed_ratio is not None else None,
        "mean_glideslope_deviation_final_15s_m": (
            round(mean_gs_dev, 2) if mean_gs_dev is not None else None
        ),
        "mean_signed_glideslope_deviation_final_15s_m": (
            round(mean_signed_gs_dev, 2) if mean_signed_gs_dev is not None else None
        ),
        "max_centerline_deviation_m": round(max_cl_dev, 2) if max_cl_dev is not None else None,
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
    mean_gs_dev: float | None,
    mean_signed_gs_dev: float | None,
    max_cl_dev: float | None,
) -> str:
    # 偏差は ft で述べる (Issue D-4: 高度・偏差は ft、距離は nm)。しきい値の
    # 比較は SI のまま行い、変換するのは文面に出す値だけ。
    parts: list[str] = []
    parts.append(f"接地は{rate_label}（降下率ベース）")
    parts.append(f"速度は{speed_label}")
    if mean_gs_dev is not None and mean_signed_gs_dev is not None:
        if abs(mean_signed_gs_dev) < mean_gs_dev / 2:
            # 上下に振れていて一方向に寄っていない。この状態で「高め」「低め」と
            # 言い切ると、実際にやるべき修正 (安定させること) を取り違えさせる。
            parts.append(
                f"最終進入のグライドスロープは上下にばらついた"
                f"（平均偏差 {mean_gs_dev * M_TO_FT:.0f} ft）"
            )
        else:
            direction = "高め" if mean_signed_gs_dev > 0 else "低め"
            parts.append(
                f"最終進入のグライドスロープは理想より{direction}"
                f"（平均 {abs(mean_signed_gs_dev) * M_TO_FT:.0f} ft）"
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
