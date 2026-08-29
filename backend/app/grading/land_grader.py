"""Simple land-aerodrome landing grader (FR-4).

Produces a letter grade (A..E) plus a Japanese comment from weighted
components:

- touchdown descent rate (fpm), judged against airframe-class bands,
- touchdown speed relative to the mean speed of the *stabilized final*,
- glideslope tracking from the roll-out onto final to the threshold,
- centerline keeping over the final seconds,
- (overhead patterns only) the pattern itself: roll-out alignment,
  downwind course, downwind altitude keeping.

Every measurement is taken over the segment it actually describes. That
sounds obvious, but the reason it is spelled out here is that the earlier
version did not: it used "the last 30 seconds" as a stand-in for "the
final approach", and in an overhead pattern -- the way fighters actually
land in DCS -- 30 seconds before touchdown is still the base turn. Fitting
a glidepath through a descending turn reports a 5.4 deg approach for a
landing flown at a textbook 3.1 deg, and averaging "approach speed" across
the 220 kt downwind makes every normal touchdown look slow. Both showed up
as E grades on landings that were flown well (see landing #11).

Every component score carries its evidence values so the UI can show *why*
the grade was given.
"""

from __future__ import annotations

import math

from dataclasses import dataclass, field
from typing import Any

from app.grading.deviations import ApproachAnalysis, DeviationSample
from app.grading.pattern import ApproachSegments, pattern_metrics, segment_approach

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


#: ``verdict`` (機械可読・英語) から講評文用の日本語へ。evidence 側は
#: 英語のままにしてある: 既存データ・テスト・API 利用者がその値で
#: 分岐しており、表示のために意味を変えるべきではない。
#:
#: 値は **述語まるごと** を入れる。語幹だけ持って f-string で活用語尾を
#: 足す作りにすると、形容動詞に i 形容詞の語尾が付いて「急かった」の
#: ような日本語が出る。分岐ごとに完成した文を持つのが確実。
_DESCENT_JA = {
    "very smooth": "非常に滑らかだった",
    "good": "良好だった",
    "somewhat hard": "やや硬かった",
    "hard": "硬かった",
    "extremely hard": "極めて硬かった",
}
_SPEED_JA = {
    "on speed": "適正だった",
    "slightly fast": "やや速かった",
    "fast": "速かった",
    "slightly slow": "やや遅かった",
    "slow": "遅かった",
    "unknown": "測れなかった",
}


def _interpolate(value: float, ladder: list[tuple[float, float]]) -> float:
    """Piecewise-linear lookup over an ascending ``(x, score)`` ladder."""
    if value <= ladder[0][0]:
        return ladder[0][1]
    for (x0, y0), (x1, y1) in zip(ladder, ladder[1:]):
        if value <= x1:
            if x1 <= x0:
                return y1
            return y0 + (value - x0) / (x1 - x0) * (y1 - y0)
    return ladder[-1][1]


def airframe_class(airframe: str | None, classes: dict[str, Any]) -> str:
    """機体名からクラス (fighter / helicopter / ...) を引く。

    ACMI の ``Name`` (``F-16C_50`` / ``AH-64D_BLK_II`` 等) に対する部分一致。
    どれにも当たらなければ ``default``。
    """
    if not airframe:
        return "default"
    name = str(airframe).upper()
    for cls, tokens in (classes or {}).items():
        for token in tokens or ():
            if str(token).upper() in name:
                return cls
    return "default"


def descent_rate_bands(bands: dict[str, Any], cls: str) -> dict[str, Any]:
    """クラス別の接地降下率バンド。

    旧形式 (``excellent`` 等がトップレベルに直接ある) の設定ファイルも
    そのまま受ける。その場合は機体によらず同じバンドになる。
    """
    if "excellent" in bands:
        return bands
    return bands.get(cls) or bands.get("default") or {}


def _descent_rate_score(fpm: float, bands: dict[str, Any]) -> tuple[float, str]:
    """接地降下率スコア。バンド間は線形補間する。

    階段状にすると 450 fpm が 80 点で 451 fpm が 55 点になり、実質同じ
    着陸が 25 点変わる。硬さは連続量なので採点も連続にする。
    """
    excellent = float(bands["excellent"])
    good = float(bands["good"])
    fair = float(bands["fair"])
    hard = float(bands["hard"])
    score = _interpolate(
        fpm,
        [(excellent, 100.0), (good, 80.0), (fair, 55.0), (hard, 30.0), (hard * 1.5, 5.0)],
    )
    if fpm <= excellent:
        label = "very smooth"
    elif fpm <= good:
        label = "good"
    elif fpm <= fair:
        label = "somewhat hard"
    elif fpm <= hard:
        label = "hard"
    else:
        label = "extremely hard"
    return score, label


def _speed_ratio_score(ratio: float | None, bands: dict[str, Any]) -> tuple[float, str]:
    """接地速度スコア。基準はファイナル区間 (フレア直前まで) の平均速度。

    バンドは **非対称**。フレアで数 % 減速して接地するのは正常な操作で、
    対称バンドだと教科書どおりのフレアが減点される (対称 +-5% では
    landing #11 の 0.89 が「off speed」になっていた)。逆に基準より速い
    接地はフロート・オーバーランに直結するので狭くする。
    """
    if ratio is None:
        return 50.0, "unknown"
    slow_good = float(bands.get("slow_good", 0.88))
    slow_fair = float(bands.get("slow_fair", 0.80))
    fast_good = float(bands.get("fast_good", 1.03))
    fast_fair = float(bands.get("fast_fair", 1.10))
    if slow_good <= ratio <= fast_good:
        return 100.0, "on speed"
    if ratio > fast_good:
        span = max(fast_fair - fast_good, 1e-6)
        score = _interpolate(
            ratio - fast_good, [(0.0, 100.0), (span, 65.0), (span + 0.10, 30.0)]
        )
        return score, ("slightly fast" if ratio <= fast_fair else "fast")
    span = max(slow_good - slow_fair, 1e-6)
    score = _interpolate(
        slow_good - ratio, [(0.0, 100.0), (span, 65.0), (span + 0.10, 30.0)]
    )
    return score, ("slightly slow" if ratio >= slow_fair else "slow")


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
    analysis: ApproachAnalysis,
    settings: dict[str, Any],
    segments: ApproachSegments,
) -> list[DeviationSample]:
    """Samples to judge the glidepath on: the approach up to the threshold.

    Past the threshold the aircraft is flaring, and being *above* a slope
    anchored at the aiming point is then the correct thing to do -- grading
    it against the slope would penalise a good landing. Touchdown quality is
    already covered by the descent-rate component.

    With a resolved runway the cut is the real threshold. Without one, an
    AGL floor (about the usual threshold crossing height) approximates it.

    The window runs from the start of final to the threshold, where "start
    of final" is the LATER of the roll-out out of the turn and the
    stabilization gate (see :func:`app.grading.pattern.segment_approach`).
    A fixed lookback fails at both ends: 30 s before touchdown is still the
    descending base turn of an overhead pattern -- landing #11 was flown at
    3.0-3.3 deg on final and reported as "+2.42 deg high" purely because
    8 s of base turn sat inside the window -- and it is only the last
    kilometre of a 3 nm stabilized straight-in, discarding most of the
    approach that was actually flown.

    The fixed window survives only as the fallback for approaches with
    neither anchor: no turn to roll out of and never above the gate, e.g. a
    low slow rotary-wing approach.

    Returns an empty list when there is no usable approach to judge -- a
    hover-on, a bounce, a recording that only caught the last few metres,
    or a final too short to fit a line through. The caller then scores the
    component neutrally, which is the honest answer: falling back to
    whatever samples exist would compute an angle from a handful of metres
    of distance and report tens of degrees of "error" for a landing nobody
    flew badly.
    """
    if segments.final_start_time is not None:
        candidates = [
            s
            for s in analysis.samples
            if segments.final_start_time <= s.time < analysis.touchdown_time
        ]
    else:
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


@dataclass
class GlideslopeError:
    """Angular glidepath error over the graded window."""

    #: Scored composite: how far off the reference glidepath, in degrees.
    abs_error_deg: float
    #: Signed: positive = steep / above. Drives the wording of the comment.
    signed_error_deg: float
    method: str
    #: Mean of the per-sample path angles (path-angle method only).
    mean_angle_deg: float | None = None
    #: Spread of the per-sample angles / errors about their mean.
    spread_deg: float | None = None
    #: Where the flown straight line would have reached the ground, relative
    #: to the touchdown point: positive = short of it (path-angle only).
    aim_offset_m: float | None = None


def _glideslope_errors(
    window: list[DeviationSample], reference_slope_deg: float
) -> GlideslopeError | None:
    """Angular glidepath error over ``window``.

    Two cases, because the honest metric differs:

    - With a resolved runway the reference is anchored at the real aiming
      point, so each sample's angle to it is meaningful and the mean of the
      per-sample errors measures how far off the published glidepath the
      aircraft actually was.

    - Without one, the only anchor available is the touchdown point, which
      sits wherever the aircraft happened to float to. But an ideal path to
      that anchor is still a straight line THROUGH it, and a straight line
      through the origin is exactly the locus of constant path angle. So
      measure the per-sample path angle: its mean against the reference, and
      its spread, which is what "stabilized" means.

      Measuring the per-sample angle to that anchor instead was tried and
      reverted: it IS the anchored measurement, so it brings the float bias
      straight back. The two readings are geometrically identical -- "flew a
      constant angle aimed short, then floated" and "flattened out on the
      way in" produce the same track, and no amount of maths on this data
      separates them. What can be reported is where the flown line was aimed
      (``aim_offset_m``); judging that is the resolved runway's job.
    """
    if not window:
        return None

    if any(s.distance_to_threshold is not None for s in window):
        errors = [
            e
            for e in (s.glideslope_error_deg(reference_slope_deg) for s in window)
            if e is not None
        ]
        if not errors:
            return None
        mean_error = sum(errors) / len(errors)
        spread = math.sqrt(
            sum((e - mean_error) ** 2 for e in errors) / len(errors)
        )
        return GlideslopeError(
            abs_error_deg=sum(abs(e) for e in errors) / len(errors),
            signed_error_deg=mean_error,
            method="aiming-point",
            spread_deg=spread,
        )

    points = [
        (s.distance_to_go, s.agl)
        for s in window
        if s.agl is not None and s.distance_to_go > 0
    ]
    if len(points) < 3:
        return None
    count = len(points)
    sum_x = sum(x for x, _ in points)
    sum_y = sum(y for _, y in points)
    sum_xx = sum(x * x for x, _ in points)
    sum_xy = sum(x * y for x, y in points)
    denominator = count * sum_xx - sum_x * sum_x
    if abs(denominator) < 1e-9:
        return None
    slope = (count * sum_xy - sum_x * sum_y) / denominator
    intercept = (sum_y - slope * sum_x) / count
    trend_angle = math.degrees(math.atan(slope))
    trend_error = trend_angle - reference_slope_deg

    # A fit only describes the trend, so on its own it would call an approach
    # that porpoised +/-30 m around a perfect 3 deg line "perfect" -- and the
    # advice a wandering approach needs is "stabilise", not "you were high".
    # Measure the scatter about the fit as an angle too, per sample, since the
    # same metre of wobble matters far more close in than at 2 nm.
    residual_angles = [
        math.degrees(math.atan((y - (slope * x + intercept)) / x)) for x, y in points
    ]
    scatter = math.sqrt(sum(a * a for a in residual_angles) / count)
    # 飛んだ直線がどこを狙っていたか: 正 = 接地点より手前。フレアで浮いても
    # 狙点を手前に取って寝かせても同じ値になる --- 同じ軌跡なので区別できない。
    # 採点はせず値だけ出す (landing #1 は 489 m 手前狙いで、接地点基準の
    # トレースが +144 → -54 ft と振れて見えるのはこれが理由)。
    aim_offset = -intercept / slope if abs(slope) > 1e-9 else None
    # Trend and scatter are independent contributions to being off the slope.
    return GlideslopeError(
        abs_error_deg=math.hypot(trend_error, scatter),
        signed_error_deg=trend_error,
        method="path-angle",
        mean_angle_deg=trend_angle,
        spread_deg=scatter,
        aim_offset_m=aim_offset,
    )


def _approach_speed_reference(
    analysis: ApproachAnalysis,
    settings: dict[str, Any],
    segments: ApproachSegments,
) -> tuple[float | None, str]:
    """Mean speed the touchdown is judged against, and which segment it came from.

    "Approach speed" means the speed held on the stabilized final, not the
    average of everything in the recording. In an overhead pattern the
    recording also holds the 220 kt downwind and the break, and averaging
    those in drags the reference up by 20-30 kt -- which then reports every
    normal touchdown as slow. Landing #11: 97.4 m/s across the whole track
    vs 95.4 m/s over the final, and the ratio moved from 0.87 ("off speed")
    to 0.89.

    The last seconds before touchdown are excluded as well: that is the
    flare, where losing speed is the point. Including it would make the
    reference chase the very number being judged.
    """
    flare_s = float(settings.get("speed_flare_exclude_s", 4.0))
    cutoff = analysis.touchdown_time - flare_s

    def mean_of(samples: list[DeviationSample]) -> float | None:
        speeds = [s.speed for s in samples if s.speed is not None]
        return sum(speeds) / len(speeds) if speeds else None

    if segments.has_final_cut:
        stabilized = [s for s in segments.final if s.time <= cutoff]
        mean = mean_of(stabilized)
        if mean is not None and len(stabilized) >= 3:
            return mean, "final"
    windowed = [s for s in analysis.samples if s.time <= cutoff]
    mean = mean_of(windowed)
    if mean is not None:
        return mean, "approach"
    return mean_of(analysis.samples), "all"


def _centerline_overshoot_m(analysis: ApproachAnalysis) -> float | None:
    """How far the aircraft went *through* the centreline before touchdown.

    Kept for backwards compatibility of the ``centerline`` factor evidence.
    The scored version of this lives in the ``pattern`` component, which
    also knows which side the pattern was flown from and can therefore tell
    an overshoot from an undershoot (see :mod:`app.grading.pattern`).
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


def _pattern_component(
    analysis: ApproachAnalysis,
    settings: dict[str, Any],
    segments: ApproachSegments,
    weight: float,
) -> tuple[ComponentScore | None, dict[str, Any]]:
    """Score the overhead pattern itself: (component, metrics).

    Three things decide whether an overhead pattern was flown well, and
    none of them are visible on final -- by then the pattern is already
    over and the pilot is either set up or fighting to salvage it:

    - **旋回明けの軸ずれ**: rolling out short of the centerline
      (undershoot, needing an S-turn back) or through it (overshoot, the
      classic break-turn error). Both are the same failure to judge the
      perch, so they share one band; the sign is kept in the evidence.
    - **ダウンウィンドの対地コース**: a downwind that is not parallel to
      the runway guarantees a base turn that cannot roll out on centerline,
      whatever the pilot does at the perch.
    - **ダウンウィンドの高度**: pattern altitude is what makes the perch
      point repeatable. Drifting through it is why one circuit rolls out
      high and the next low with the same technique.
    - **ブレイクの高度**: the break is a LEVEL 180 deg turn. Height lost or
      gained in it is the first thing that puts the downwind off altitude,
      so it shows up before anything else does. Measured 19-294 m across
      this server's overhead landings, i.e. it separates real technique.

    Abeam distance is measured and reported but deliberately not scored:
    the "right" spacing differs per airframe and per squadron habit, and
    there is no calibration data here to justify attaching points to it.

    Returns ``(None, metrics)`` when the recording does not actually hold a
    pattern to judge -- e.g. a landing detected from a track that starts on
    final. Scoring an absent downwind would invent an error.
    """
    metrics = pattern_metrics(analysis, segments)
    bands = settings.get("pattern", {})
    min_downwind_s = float(bands.get("min_downwind_s", 6.0))

    parts: list[tuple[str, float]] = []

    alignment = metrics.get("alignment_error_m")
    if alignment is not None and segments.rollout_time is not None:
        band = bands.get("alignment_error_m", {})
        parts.append(
            (
                "alignment",
                _band_score(
                    alignment,
                    float(band.get("good", 100.0)),
                    float(band.get("fair", 250.0)),
                    float(band.get("poor", 600.0)),
                ),
            )
        )

    break_duration = metrics.get("break_duration_s")
    break_spread = metrics.get("break_altitude_spread_m")
    break_usable = (
        break_duration is not None
        and break_duration >= float(bands.get("min_break_s", 6.0))
        and break_spread is not None
    )
    metrics["break_judged"] = break_usable
    if break_usable:
        band = bands.get("break_altitude_spread_m", {})
        parts.append(
            (
                "break_altitude",
                _band_score(
                    break_spread,
                    float(band.get("good", 30.0)),
                    float(band.get("fair", 75.0)),
                    float(band.get("poor", 180.0)),
                ),
            )
        )

    duration = metrics.get("downwind_duration_s")
    # 脚として短すぎるダウンウィンドで取った平均には意味がないので、
    # 値は残しつつ採点からは外す。
    downwind_usable = duration is not None and duration >= min_downwind_s
    metrics["downwind_judged"] = downwind_usable
    if downwind_usable:
        course_error = metrics.get("downwind_course_error_deg")
        if course_error is not None:
            band = bands.get("downwind_course_error_deg", {})
            parts.append(
                (
                    "downwind_course",
                    _band_score(
                        course_error,
                        float(band.get("good", 8.0)),
                        float(band.get("fair", 18.0)),
                        float(band.get("poor", 35.0)),
                    ),
                )
            )
        spread = metrics.get("downwind_altitude_spread_m")
        if spread is not None:
            band = bands.get("downwind_altitude_spread_m", {})
            parts.append(
                (
                    "downwind_altitude",
                    _band_score(
                        spread,
                        float(band.get("good", 45.0)),
                        float(band.get("fair", 90.0)),
                        float(band.get("poor", 200.0)),
                    ),
                )
            )

    if not parts:
        return None, metrics

    evidence: dict[str, Any] = dict(metrics)
    evidence["sub_scores"] = {name: round(score, 1) for name, score in parts}
    score = sum(score for _, score in parts) / len(parts)
    return ComponentScore("pattern", score, weight, evidence), metrics


def grade_land_landing(
    analysis: ApproachAnalysis,
    config: Any,
) -> LandGradeResult:
    """Grade a land landing; ``config`` is a :class:`GradingConfig`."""
    settings = config.land_grading
    segments = segment_approach(analysis, settings)
    is_overhead = analysis.approach_pattern == "overhead"
    weights = dict(settings["weights"])
    if is_overhead and settings.get("overhead_weights"):
        weights = dict(settings["overhead_weights"])

    descent_fpm = analysis.touchdown_descent_rate_ms * MS_TO_FPM
    frame_class = airframe_class(analysis.airframe, settings.get("airframe_classes", {}))
    rate_bands = descent_rate_bands(settings["descent_rate_fpm"], frame_class)
    rate_score, rate_label = _descent_rate_score(descent_fpm, rate_bands)

    mean_speed, speed_reference = _approach_speed_reference(analysis, settings, segments)
    speed_ratio = (
        analysis.touchdown_speed_ms / mean_speed
        if analysis.touchdown_speed_ms is not None and mean_speed
        else None
    )
    speed_score, speed_label = _speed_ratio_score(
        speed_ratio, settings["touchdown_speed_ratio"]
    )

    gs_window = _glideslope_window(analysis, settings, segments)
    # 絶対値と符号付きを分けて持つ: 絶対値は上下に振れた進入が相殺されて
    # 「完璧」にならないようにするため、符号付きは「高め / 低め」の向きを
    # 述べるため (絶対値平均は常に非負なので向きを判定できない)。
    gs_error = _glideslope_errors(gs_window, analysis.glideslope_deg)
    mean_gs_err = gs_error.abs_error_deg if gs_error else None
    mean_signed_gs_err = gs_error.signed_error_deg if gs_error else None
    gs_method = gs_error.method if gs_error else "none"
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
            {
                "touchdown_descent_rate_fpm": round(descent_fpm, 1),
                "verdict": rate_label,
                # どのバンドで測ったか。同じ 460 fpm でも旅客機なら hard、
                # 戦闘機なら good なので、根拠として出さないと講評が読めない。
                "airframe": analysis.airframe,
                "airframe_class": frame_class,
                "bands_fpm": {k: rate_bands[k] for k in ("excellent", "good", "fair", "hard")},
            },
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
                # "final" = ロールアウト後の安定区間で取った基準速度。
                # "approach" はファイナルを切り出せず進入全体で取った場合。
                "speed_reference": speed_reference,
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
                # 実際に飛んだ経路角 (接地点基準の測り方のときのみ)。
                "mean_path_angle_deg": (
                    round(gs_error.mean_angle_deg, 3)
                    if gs_error and gs_error.mean_angle_deg is not None
                    else None
                ),
                # 直線からの浮き沈み。大きいほど不安定。
                "path_angle_spread_deg": (
                    round(gs_error.spread_deg, 3)
                    if gs_error and gs_error.spread_deg is not None
                    else None
                ),
                # 飛んだ直線が接地点の何 m 手前を狙っていたか (採点には未使用)。
                "aim_offset_m": (
                    round(gs_error.aim_offset_m, 1)
                    if gs_error and gs_error.aim_offset_m is not None
                    else None
                ),
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
                # 記録のみ。採点は pattern 側 (オーバーヘッドのみ)。
                "overshoot_m": overshoot_m,
            },
        ),
    ]

    pattern_component, pattern_values = (None, {})
    if is_overhead:
        pattern_component, pattern_values = _pattern_component(
            analysis, settings, segments, weights.get("pattern", 0.0)
        )
    if pattern_component is not None:
        components.append(pattern_component)

    # 重みは正規化してから合成する。pattern を出せなかった着陸で
    # 合計重みが 1.0 未満のまま加重和を取ると、点数だけが目減りして
    # 「パターンが記録に入っていなかった」ことが減点に化ける。
    weight_sum = sum(c.weight for c in components) or 1.0
    total = sum(c.score * c.weight for c in components) / weight_sum
    letters = settings["letters"]
    grade = "E"
    for letter in ("A", "B", "C", "D"):
        if total >= letters[letter]:
            grade = letter
            break

    comment = _build_comment(
        grade,
        rate_label,
        speed_label,
        mean_gs_err,
        mean_signed_gs_err,
        max_cl_dev,
        pattern_values if pattern_component is not None else None,
        float(gs_bands["good"]),
        gs_error,
        descent_fpm,
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
        "mean_path_angle_deg": (
            round(gs_error.mean_angle_deg, 3)
            if gs_error and gs_error.mean_angle_deg is not None
            else None
        ),
        "path_angle_spread_deg": (
            round(gs_error.spread_deg, 3)
            if gs_error and gs_error.spread_deg is not None
            else None
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
        "approach_pattern": analysis.approach_pattern,
        "airframe_class": frame_class,
        "speed_reference": speed_reference,
        # 旋回明けの時刻 (接地からの秒数)。None = 旋回を検出していない。
        "rollout_before_touchdown_s": (
            round(analysis.touchdown_time - segments.rollout_time, 1)
            if segments.rollout_time is not None
            else None
        ),
        # 実際に採点したファイナルの長さ (秒)。None = 固定窓へフォールバック。
        "final_window_s": (
            round(analysis.touchdown_time - segments.final_start_time, 1)
            if segments.final_start_time is not None
            else None
        ),
        # 起点がどちらで決まったか。長いストレートインでは "gate"。
        "final_start_anchor": (
            None
            if segments.final_start_time is None
            else "rollout"
            if segments.rollout_time is not None
            and segments.final_start_time == segments.rollout_time
            else "gate"
        ),
    }
    if pattern_component is not None:
        metrics.update(
            {f"pattern_{k}": v for k, v in pattern_values.items()}
        )
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
    pattern: dict[str, Any] | None = None,
    gs_good_deg: float = 0.35,
    gs_error: "GlideslopeError | None" = None,
    descent_fpm: float | None = None,
) -> str:
    # 横ずれは ft で述べる (Issue D-4)。グライドスロープは角度で述べる:
    # ft だと同じ操縦精度でも遠いほど大きな数字になり、比較にならない。
    parts: list[str] = []
    descent = _DESCENT_JA.get(rate_label, rate_label)
    if descent_fpm is not None:
        # 「降下率ベース」より実数の方が短くて情報量が多い。
        parts.append(f"接地は{descent}（{descent_fpm:.0f} fpm）")
    else:
        parts.append(f"接地は{descent}")
    parts.append(f"速度は{_SPEED_JA.get(speed_label, speed_label)}")
    gs_method = gs_error.method if gs_error is not None else "none"
    if mean_gs_err is not None and mean_signed_gs_err is not None:
        if mean_gs_err <= gs_good_deg:
            # 良好なものを「高め」「低め」と述べると、直すところが無い
            # 相手に修正指示を出すことになる。
            parts.append(
                f"閾値までのグライドスロープは安定していた"
                f"（平均誤差 {mean_gs_err:.2f}°）"
            )
        elif abs(mean_signed_gs_err) < mean_gs_err / 2:
            # ばらつきが支配的。この状態で「高め」「低め」と言い切ると、
            # 実際にやるべき修正 (安定させること) を取り違えさせる。
            spread = gs_error.spread_deg if gs_error else None
            detail = (
                f"±{spread:.2f}°" if spread is not None else f"平均誤差 {mean_gs_err:.2f}°"
            )
            parts.append(f"進入経路角が安定しなかった（{detail}）")
        elif gs_method == "path-angle":
            # 滑走路が解決できていないので基準は接地点。フレアで浮いた分だけ
            # 経路角は浅く出るので、「低かった」と位置で言い切らず、実測の
            # 平均経路角を数字で述べる。
            slope = "急だった" if mean_signed_gs_err > 0 else "浅かった"
            angle = gs_error.mean_angle_deg if gs_error else None
            detail = (
                f"{angle:.2f}°、接地点基準"
                if angle is not None
                else f"{abs(mean_signed_gs_err):.2f}° 差、接地点基準"
            )
            parts.append(f"進入経路の傾きが理想より{slope}（{detail}）")
        else:
            direction = "高かった" if mean_signed_gs_err > 0 else "低かった"
            parts.append(
                f"閾値までのグライドスロープは理想より{direction}"
                f"（平均 {abs(mean_signed_gs_err):.2f}°）"
            )
    if max_cl_dev is not None and max_cl_dev > 5.0:
        parts.append(f"センターラインから最大 {max_cl_dev * M_TO_FT:.0f} ft ずれた")
    parts.extend(_pattern_comment_parts(pattern))
    verdicts = {
        "A": "見事な着陸です。",
        "B": "良好な着陸です。",
        "C": "まずまずの着陸です。改善点を確認しましょう。",
        "D": "着陸に難があります。進入の安定性を意識しましょう。",
        "E": "着陸は危ういものでした。基本の手順を見直しましょう。",
    }
    return "、".join(parts) + "。" + verdicts[grade]


def _pattern_comment_parts(pattern: dict[str, Any] | None) -> list[str]:
    """オーバーヘッドパターンについて言うべきことがあれば述べる。

    良かった項目は黙る。全項目を毎回並べると、直すべき 1 点が
    3 行の定型文に埋もれて読まれなくなる。
    """
    if not pattern:
        return []
    parts: list[str] = []
    rollout = pattern.get("rollout_offset_m")
    overshoot = pattern.get("overshoot_m") or 0.0
    if overshoot > 60.0:
        parts.append(
            f"旋回明けでセンターラインを {overshoot * M_TO_FT:.0f} ft "
            "オーバーシュートした"
        )
    elif rollout is not None and rollout > 150.0:
        parts.append(
            f"旋回明けでセンターラインまで {rollout * M_TO_FT:.0f} ft "
            "残っていた（アンダーシュート気味）"
        )
    if pattern.get("downwind_judged"):
        course_error = pattern.get("downwind_course_error_deg")
        if course_error is not None and course_error > 12.0:
            parts.append(
                f"ダウンウィンドが滑走路と平行でなかった（方位差 {course_error:.0f}°）"
            )
        spread = pattern.get("downwind_altitude_spread_m")
        if spread is not None and spread > 60.0:
            parts.append(
                f"ダウンウィンドで高度が {spread * M_TO_FT:.0f} ft ふらついた"
            )
    if pattern.get("break_judged"):
        spread = pattern.get("break_altitude_spread_m")
        if spread is not None and spread > 45.0:
            parts.append(
                f"ブレイク中に高度が {spread * M_TO_FT:.0f} ft 動いた"
                "（水平旋回が基本）"
            )
    return parts
