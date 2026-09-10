"""Unit tests for app.grading.land_grader."""

from __future__ import annotations

import math

import pytest

from app.detection.detector import analyze_track
from app.grading.config import GradingConfig, load_grading_config
from app.grading.deviations import ApproachAnalysis, DeviationSample, build_approach_analysis
from app.grading.land_grader import _glideslope_errors, grade_land_landing
from tests.conftest import GRADING_YAML
from tests.helpers import DECK_ALTITUDE_M, analysis_with_gs_deviations, make_approach_samples

CONFIG = load_grading_config(GRADING_YAML)


def test_glidepath_is_not_judged_when_there_is_no_approach_to_judge() -> None:
    touchdown_time = 100.0
    samples = [
        DeviationSample(
            time=touchdown_time - 30.0 + i * 0.25,
            distance_to_go=17.0 + i * 0.3,
            glideslope_deviation=8.0,
            centerline_deviation=0.5,
            speed=3.0,
            agl=8.8 - i * 0.05,
        )
        for i in range(100)
    ]
    analysis = ApproachAnalysis(
        kind="land",
        outcome="full_stop",
        glideslope_deg=3.0,
        course_deg=0.0,
        touchdown_time=touchdown_time,
        touchdown_speed_ms=3.0,
        touchdown_descent_rate_ms=0.2,
        samples=samples,
    )
    result = grade_land_landing(analysis, GradingConfig({}))
    glideslope = next(
        component for component in result.components if component.name == "glideslope"
    )
    assert glideslope.evidence["mean_abs_error_deg"] is None
    assert glideslope.evidence["samples"] == 0
    assert glideslope.score is None
    assert glideslope.evidence["unscored_reason"] == "not-measured"
    assert "glideslope" in result.metrics["unmeasured_components"]
    assert result.metrics["measured_weight"] < 1.0


def test_glidepath_without_a_runway_is_judged_by_the_angle_actually_flown() -> None:
    slope = math.tan(math.radians(3.0))
    float_m = 800.0
    window = [
        DeviationSample(
            time=float(i),
            distance_to_go=distance,
            glideslope_deviation=None,
            centerline_deviation=0.0,
            agl=(distance - float_m) * slope,
        )
        for i, distance in enumerate(range(2600, 1000, -100))
    ]
    error = _glideslope_errors(window, 3.0)
    assert error is not None and error.method == "path-angle"
    assert error.signed_error_deg == pytest.approx(0.0, abs=0.05)
    anchored = [sample.glideslope_error_deg(3.0) for sample in window]
    assert sum(value for value in anchored if value is not None) / len(anchored) < -1.0


def test_glidepath_with_a_runway_keeps_the_absolute_aiming_point_error() -> None:
    slope = math.tan(math.radians(3.0))
    window = [
        DeviationSample(
            time=float(i),
            distance_to_go=distance,
            glideslope_deviation=None,
            centerline_deviation=0.0,
            agl=distance * slope - 40.0,
            distance_to_threshold=distance - 300.0,
        )
        for i, distance in enumerate(range(2600, 1000, -100))
    ]
    error = _glideslope_errors(window, 3.0)
    assert error is not None and error.method == "aiming-point"
    assert error.signed_error_deg < -0.8


def _land_event(**kwargs):
    samples = make_approach_samples(
        glideslope_deg=3.0,
        approach_speed_ms=30.0,
        touchdown_speed_ms=None,
        **kwargs,
    )
    events = analyze_track(samples, DECK_ALTITUDE_M, carriers={})
    assert len(events) == 1
    return events[0]


def test_land_course_avoids_crosswind_crab_bias() -> None:
    """End-to-end: a crabbed land approach grades with an unbiased centerline
    and reports the crosswind crab angle for the UI (Issue #26)."""
    samples = make_approach_samples(
        glideslope_deg=3.0,
        approach_speed_ms=30.0,
        touchdown_speed_ms=None,
    )
    # Crab the heading 30 deg while the ground track stays straight down the
    # runway centerline (course 0).
    for s in samples:
        s.heading = 30.0
    events = analyze_track(samples, DECK_ALTITUDE_M, carriers={})
    assert len(events) == 1
    event = events[0]
    analysis = build_approach_analysis(event, CONFIG.land_glideslope_deg)

    assert analysis.kind == "land"
    # Course follows the runway track, not the crabbed heading.
    assert analysis.course_deg == pytest.approx(0.0, abs=1.0)
    assert analysis.crosswind_crab_deg == pytest.approx(30.0, abs=1.0)

    result = grade_land_landing(analysis, CONFIG)
    # The crab must not leak into the centerline component.
    centerline = next(c for c in result.components if c.name == "centerline")
    assert centerline.evidence["max_abs_deviation_m"] == pytest.approx(0.0, abs=1.0)
    assert result.metrics["crosswind_crab_deg"] == pytest.approx(30.0, abs=1.0)


def test_land_grader_smooth_landing_scores_high() -> None:
    event = _land_event(pre_touchdown_descent_ms=1.2)
    analysis = build_approach_analysis(event, CONFIG.land_glideslope_deg)
    result = grade_land_landing(analysis, CONFIG)

    assert result.grade in ("A", "B")
    assert result.score is not None
    assert result.score >= 78
    assert result.comment
    evidence = {c.name: c.evidence for c in result.components}
    assert evidence["descent_rate"]["touchdown_descent_rate_fpm"] < 250


def test_land_grader_hard_landing_scores_low() -> None:
    event = _land_event(pre_touchdown_descent_ms=4.5)
    analysis = build_approach_analysis(event, CONFIG.land_glideslope_deg)
    result = grade_land_landing(analysis, CONFIG)

    descent = next(c for c in result.components if c.name == "descent_rate")
    assert descent.score is not None
    assert descent.score <= 30
    assert result.grade in ("C", "D", "E")


def test_land_grader_centerline_uses_final_segment_not_whole_approach() -> None:
    """A wide turn onto final (typical fighter overhead-break entry) puts
    early samples far off the straight-line course estimate; that must not
    tank the centerline score the way it tanks a real off-axis final. Only
    glideslope was windowed to the final 15s -- centerline used every
    sample in the whole ~60s/2nm approach, including the turn."""
    from app.grading.deviations import ApproachAnalysis, DeviationSample

    touchdown_time = 100.0
    samples = [
        DeviationSample(
            time=float(t),
            distance_to_go=3000.0 - t * 10,
            glideslope_deviation=0.0,
            centerline_deviation=800.0,
            speed=70.0,
            agl=300.0,
        )
        for t in range(0, 40, 5)
    ] + [
        DeviationSample(
            time=float(t),
            distance_to_go=(100 - t) * 30,
            glideslope_deviation=0.0,
            centerline_deviation=2.0,
            speed=70.0,
            agl=(100 - t) * 5,
        )
        for t in range(86, 100)
    ]
    analysis = ApproachAnalysis(
        kind="land",
        outcome="full_stop",
        glideslope_deg=3.0,
        course_deg=0.0,
        touchdown_time=touchdown_time,
        touchdown_speed_ms=70.0,
        touchdown_descent_rate_ms=1.0,
        samples=samples,
    )
    result = grade_land_landing(analysis, CONFIG)
    centerline = next(c for c in result.components if c.name == "centerline")
    assert centerline.evidence["max_abs_deviation_m"] == pytest.approx(2.0)
    assert centerline.score is not None
    assert centerline.score >= 90


def test_land_grader_off_centerline_penalized() -> None:
    event = _land_event(lateral_offset_m=25.0, pre_touchdown_descent_ms=1.2)
    analysis = build_approach_analysis(event, CONFIG.land_glideslope_deg)
    result = grade_land_landing(analysis, CONFIG)

    centerline = next(c for c in result.components if c.name == "centerline")
    assert centerline.score is not None
    assert centerline.score < 40
    smooth = grade_land_landing(
        build_approach_analysis(_land_event(pre_touchdown_descent_ms=1.2), 3.0), CONFIG
    )
    assert result.score is not None
    assert smooth.score is not None
    assert result.score < smooth.score


def test_land_grader_comment_states_deviations_in_feet() -> None:
    """Issue D-4: 高度・偏差は ft で見せる。コメント文だけメートルが残っていた。"""
    event = _land_event(lateral_offset_m=25.0, gs_offset_m=10.0, pre_touchdown_descent_ms=1.2)
    analysis = build_approach_analysis(event, CONFIG.land_glideslope_deg)
    result = grade_land_landing(analysis, CONFIG)

    assert "ft" in result.comment
    assert " m）" not in result.comment
    assert " m ずれた" not in result.comment

    # 文面の数値が実際に ft へ換算されている (m のまま出ていない) こと。
    centerline = next(c for c in result.components if c.name == "centerline")
    dev_m = centerline.evidence["max_abs_deviation_m"]
    assert f"{dev_m / 0.3048:.0f} ft" in result.comment


def test_land_grader_says_low_when_the_approach_was_below_glideslope() -> None:
    """「低め」が到達不能だった: 向きを絶対値平均から決めていたため、
    実際には下を飛んでいた進入まで一律「高め」と言われていた。"""
    result = grade_land_landing(analysis_with_gs_deviations([-30.0] * 12), CONFIG)
    assert "理想より低かった" in result.comment
    assert "高かった" not in result.comment

    high = grade_land_landing(analysis_with_gs_deviations([30.0] * 12), CONFIG)
    assert "理想より高かった" in high.comment
    assert "低かった" not in high.comment


def test_land_grader_reports_wander_instead_of_a_direction_when_oscillating() -> None:
    """上下に振れて一方向に寄っていない進入を「高め」「低め」と言い切らない。"""
    result = grade_land_landing(analysis_with_gs_deviations([30.0, -30.0] * 6), CONFIG)
    assert "安定しなかった" in result.comment
    assert "高め" not in result.comment and "低め" not in result.comment
    # 振れ幅は採点に効いたままである (相殺されて満点にならない)。
    glideslope = next(c for c in result.components if c.name == "glideslope")
    assert glideslope.evidence["mean_abs_deviation_m"] == pytest.approx(30.0)
    assert glideslope.evidence["mean_signed_deviation_m"] == pytest.approx(0.0)
    # 採点に使うのは角度。振れの散らばりが絶対値側に残り、符号付き
    # (トレンド) はそれより十分小さい — これが「一方向に寄っていない」の
    # 判定条件そのもので、講評が向きを言い切らない根拠になっている。
    signed = glideslope.evidence["mean_signed_error_deg"]
    absolute = glideslope.evidence["mean_abs_error_deg"]
    assert absolute > 0.5
    assert abs(signed) < absolute / 2
    assert glideslope.score is not None
    assert glideslope.score < 60


def test_land_grader_letter_bands() -> None:
    letters = CONFIG.land_grading["letters"]
    assert letters["A"] > letters["B"] > letters["C"] > letters["D"]


def _pattern_analysis(
    *,
    downwind_lat_m: float = 2200.0,
    downwind_agl_m: float = 450.0,
    downwind_agl_drift_m: float = 0.0,
    downwind_course_error_deg: float = 0.0,
    final_slope_deg: float = 3.0,
    rollout_lat_m: float = 0.0,
    final_speed_ms: float = 90.0,
    downwind_speed_ms: float = 120.0,
    touchdown_speed_ms: float = 82.0,
    touchdown_descent_ms: float = 2.35,
    approach_pattern: str = "overhead",
    airframe: str | None = "F-16C_50",
    with_downwind: bool = True,
) -> "ApproachAnalysis":
    """Synthetic overhead circuit: downwind -> base turn -> final.

    Built directly in runway coordinates (distance-to-go / lateral offset),
    which is the frame the graders work in, so the test states the geometry
    it means instead of hoping a lat/lon fixture lands on it.
    """
    import math

    from app.grading.deviations import ApproachAnalysis, DeviationSample

    touchdown_time = 120.0
    tan_slope = math.tan(math.radians(final_slope_deg))
    samples: list[DeviationSample] = []

    def add(time: float, dtg: float, lateral: float, agl: float, speed: float) -> None:
        samples.append(
            DeviationSample(
                time=time,
                distance_to_go=max(dtg, 0.0),
                glideslope_deviation=agl - max(dtg, 0.0) * math.tan(math.radians(3.0)),
                centerline_deviation=lateral,
                speed=speed,
                agl=agl,
            )
        )

    final_start_dtg = 2250.0
    base_start_dtg = 2900.0

    if with_downwind:
        # 20 s flying the reciprocal, abeam at downwind_lat_m.
        downwind_travel = downwind_speed_ms * 20.0
        drift_rate = math.tan(math.radians(downwind_course_error_deg))
        for step in range(21):
            fraction = step / 20.0
            travelled = downwind_travel * fraction
            add(
                60.0 + step,
                base_start_dtg - downwind_travel + travelled,
                downwind_lat_m + travelled * drift_rate,
                downwind_agl_m + downwind_agl_drift_m * fraction,
                downwind_speed_ms,
            )

    # 15 s descending base turn onto final.
    turn_start_agl = downwind_agl_m + downwind_agl_drift_m
    final_start_agl = final_start_dtg * tan_slope
    for step in range(1, 16):
        fraction = step / 15.0
        add(
            80.0 + step,
            base_start_dtg + (final_start_dtg - base_start_dtg) * fraction,
            downwind_lat_m + (rollout_lat_m - downwind_lat_m) * fraction,
            turn_start_agl + (final_start_agl - turn_start_agl) * fraction,
            downwind_speed_ms + (final_speed_ms - downwind_speed_ms) * fraction,
        )

    # 25 s stabilized final at the requested slope.
    for step in range(1, 26):
        fraction = step / 25.0
        dtg = final_start_dtg * (1.0 - fraction)
        add(
            95.0 + step,
            dtg,
            rollout_lat_m * (1.0 - fraction),
            dtg * tan_slope,
            final_speed_ms,
        )

    return ApproachAnalysis(
        kind="land",
        outcome="full_stop",
        glideslope_deg=3.0,
        course_deg=0.0,
        touchdown_time=touchdown_time,
        touchdown_speed_ms=touchdown_speed_ms,
        touchdown_descent_rate_ms=touchdown_descent_ms,
        approach_pattern=approach_pattern,
        airframe=airframe,
        samples=samples,
    )


def test_glidepath_is_judged_from_the_rollout_not_a_fixed_lookback() -> None:
    """A textbook 3 deg final must score as one even when a base turn
    precedes it inside the old fixed 30 s window.

    Production landing #11 was flown at 3.0-3.3 deg and graded E: the
    window reached 8 s into the descending base turn, the least-squares
    fit through turn+final came out at 5.4 deg, and the glideslope
    component floored at 5/100.
    """
    analysis = _pattern_analysis()
    result = grade_land_landing(analysis, CONFIG)

    glideslope = next(c for c in result.components if c.name == "glideslope")
    assert glideslope.evidence["mean_abs_error_deg"] < 0.35
    assert glideslope.score is not None
    assert glideslope.score > 90
    # The cut really was made, and at the roll-out (25 s of final here).
    assert result.metrics["rollout_before_touchdown_s"] == pytest.approx(25.0, abs=2.0)


def test_approach_speed_reference_excludes_the_downwind() -> None:
    """ "On speed" means the speed held on final, not an average that
    includes the 120 m/s downwind -- which made every normal touchdown
    look slow (landing #11: ratio 0.87 -> "off speed")."""
    analysis = _pattern_analysis(final_speed_ms=90.0, downwind_speed_ms=120.0)
    result = grade_land_landing(analysis, CONFIG)

    speed = next(c for c in result.components if c.name == "touchdown_speed")
    assert speed.evidence["speed_reference"] == "final"
    assert speed.evidence["mean_approach_speed_ms"] == pytest.approx(90.0, abs=1.0)
    assert speed.evidence["verdict"] == "on speed"


def test_fighter_touchdown_is_not_judged_by_transport_bands() -> None:
    """~460 fpm is a normal F-16 touchdown and a firm airliner one."""
    fighter = _pattern_analysis(touchdown_descent_ms=2.35, airframe="F-16C_50")
    airliner = _pattern_analysis(touchdown_descent_ms=2.35, airframe="Yak-52")

    fighter_rate = next(
        c for c in grade_land_landing(fighter, CONFIG).components if c.name == "descent_rate"
    )
    default_rate = next(
        c for c in grade_land_landing(airliner, CONFIG).components if c.name == "descent_rate"
    )
    assert fighter_rate.evidence["airframe_class"] == "fighter"
    assert default_rate.evidence["airframe_class"] == "default"
    assert fighter_rate.score is not None
    assert fighter_rate.score > 75
    assert default_rate.score is not None
    assert fighter_rate.score is not None
    assert default_rate.score < fighter_rate.score


def test_descent_rate_score_is_continuous_across_bands() -> None:
    """A step function makes two indistinguishable landings differ by 25
    points because one crossed a threshold by 1 fpm."""
    from app.grading.land_grader import _descent_rate_score

    bands = {"excellent": 300, "good": 450, "fair": 650, "hard": 850}
    just_under, _ = _descent_rate_score(449.0, bands)
    just_over, _ = _descent_rate_score(451.0, bands)
    assert abs(just_under - just_over) < 1.0


def test_the_downwind_leg_decides_whether_a_pattern_was_flown() -> None:
    """The pattern is scored when the track HOLDS a pattern, not when the
    detector said so.

    The label on the landing row is a heading-rate heuristic computed at
    ingest, and it is wrong often enough to matter: it called 207 of 403
    production land landings "overhead", 63 of them with no downwind leg in
    the recording at all -- every sweeping turn onto a long final and every
    helicopter arrival. Those were then scored on the roll-out alignment
    alone, which every approach ever flown has. So the geometry decides,
    both ways round: a circuit is judged as one even when the label says
    straight-in, and a track with no downwind gets no pattern component
    even when the label says overhead.
    """
    circuit_labelled_straight_in = grade_land_landing(
        _pattern_analysis(approach_pattern="straight_in"), CONFIG
    )
    no_downwind_labelled_overhead = grade_land_landing(
        _pattern_analysis(with_downwind=False, approach_pattern="overhead"), CONFIG
    )

    pattern = next(c for c in circuit_labelled_straight_in.components if c.name == "pattern")
    assert pattern.evidence["downwind_judged"] is True
    assert pattern.score is not None
    assert pattern.score > 90
    assert circuit_labelled_straight_in.metrics["approach_pattern"] == "overhead"

    assert all(c.name != "pattern" for c in no_downwind_labelled_overhead.components)
    assert no_downwind_labelled_overhead.metrics["approach_pattern"] != "overhead"
    # 検出器のラベルは捨てずに残す (どちらが変わったのか追えるように)。
    assert no_downwind_labelled_overhead.metrics["approach_pattern_detected"] == "overhead"


def test_pattern_component_catches_an_overshot_rollout() -> None:
    """Rolling out through the centerline is the classic break-turn error."""
    clean = grade_land_landing(_pattern_analysis(rollout_lat_m=0.0), CONFIG)
    overshot = grade_land_landing(_pattern_analysis(rollout_lat_m=-400.0), CONFIG)

    clean_pattern = next(c for c in clean.components if c.name == "pattern")
    overshot_pattern = next(c for c in overshot.components if c.name == "pattern")
    assert overshot_pattern.evidence["overshoot_m"] == pytest.approx(400.0, abs=20.0)
    # Rolled out on the far side of the centerline: offset is negative.
    assert overshot_pattern.evidence["rollout_offset_m"] < 0
    assert overshot_pattern.score is not None
    assert clean_pattern.score is not None
    assert overshot_pattern.score < clean_pattern.score
    assert "オーバーシュート" in overshot.comment


def test_pattern_component_catches_a_wandering_downwind() -> None:
    wandering = grade_land_landing(
        _pattern_analysis(downwind_agl_drift_m=-160.0, downwind_course_error_deg=20.0),
        CONFIG,
    )
    pattern = next(c for c in wandering.components if c.name == "pattern")
    assert pattern.evidence["downwind_course_error_deg"] == pytest.approx(20.0, abs=3.0)
    assert pattern.evidence["downwind_altitude_spread_m"] == pytest.approx(160.0, abs=15.0)
    assert pattern.score is not None
    assert pattern.score < 70
    assert "ダウンウィンド" in wandering.comment


def test_a_recording_without_the_downwind_is_not_penalised_for_it() -> None:
    """Older landings were captured with a 60 s window that often cut the
    circuit off. Missing data must drop out of the weighted mean, not
    quietly subtract its weight from the score."""
    full = grade_land_landing(_pattern_analysis(), CONFIG)
    clipped = grade_land_landing(_pattern_analysis(with_downwind=False), CONFIG)

    clipped_pattern = next((c for c in clipped.components if c.name == "pattern"), None)
    if clipped_pattern is not None:
        assert clipped_pattern.evidence["downwind_judged"] is False
    assert clipped.score == pytest.approx(full.score, abs=6.0)


def test_overhead_landing_flown_well_earns_a_high_grade() -> None:
    """End-to-end guard on the calibration: 33 production landings had
    produced no A and no B at all."""
    result = grade_land_landing(_pattern_analysis(touchdown_descent_ms=1.6), CONFIG)
    assert result.grade in ("A", "B")


def _straight_in_analysis(
    *,
    duration_s: float = 120.0,
    start_agl_m: float = 600.0,
    slope_deg: float = 3.0,
    level_off_agl_m: float | None = None,
    speed_ms: float = 90.0,
) -> "ApproachAnalysis":
    """A straight-in with no turn: the roll-out anchor never fires.

    ``level_off_agl_m`` inserts 30 s of level flight at that height before
    the descent resumes -- the case the gate has to get right.
    """
    import math

    from app.grading.deviations import ApproachAnalysis, DeviationSample

    touchdown_time = 300.0
    tan_slope = math.tan(math.radians(slope_deg))
    samples: list[DeviationSample] = []
    start_dtg = start_agl_m / tan_slope

    def add(time: float, dtg: float, agl: float) -> None:
        samples.append(
            DeviationSample(
                time=time,
                distance_to_go=max(dtg, 0.0),
                glideslope_deviation=agl - max(dtg, 0.0) * tan_slope,
                centerline_deviation=0.0,
                speed=speed_ms,
                agl=agl,
                signed_distance_to_go=dtg,
            )
        )

    steps = int(duration_s)
    for step in range(steps + 1):
        fraction = step / steps
        dtg = start_dtg * (1.0 - fraction)
        agl = dtg * tan_slope
        if level_off_agl_m is not None and agl < level_off_agl_m:
            agl = level_off_agl_m
        add(touchdown_time - duration_s + step, dtg, agl)
    if level_off_agl_m is not None:
        # Resume the descent for the last 30 s so it still touches down.
        for step in range(1, 31):
            fraction = step / 30.0
            add(
                touchdown_time - 30.0 + step,
                start_dtg * 0.1 * (1.0 - fraction),
                level_off_agl_m * (1.0 - fraction),
            )

    return ApproachAnalysis(
        kind="land",
        outcome="full_stop",
        glideslope_deg=3.0,
        course_deg=0.0,
        touchdown_time=touchdown_time,
        touchdown_speed_ms=speed_ms * 0.92,
        touchdown_descent_rate_ms=1.2,
        approach_pattern="straight_in",
        airframe="F-16C_50",
        samples=samples,
    )


def test_long_straight_in_is_judged_over_its_whole_stabilized_final() -> None:
    """A 3 nm stabilized final must be judged over the 3 nm it was flown.

    With a fixed 30 s lookback only the last kilometre counted, so most of
    the approach the pilot actually flew never entered the score.
    """
    result = grade_land_landing(_straight_in_analysis(), CONFIG)

    assert result.metrics["final_start_anchor"] == "gate"
    # 1000 ft on a 3 deg path is ~5.8 km; at 90 m/s that is ~65 s.
    assert result.metrics["final_window_s"] > 50.0
    assert result.metrics["rollout_before_touchdown_s"] is None


def test_gate_never_shortens_an_overhead_final() -> None:
    """The gate is crossed on base in a circuit, so the roll-out still wins:
    grading an overhead approach from 1000 ft would drag the base turn back
    into the window, which is the bug this whole segmentation exists for."""
    result = grade_land_landing(_pattern_analysis(), CONFIG)
    assert result.metrics["final_start_anchor"] == "rollout"
    assert result.metrics["final_window_s"] == pytest.approx(25.0, abs=2.0)


def test_a_level_off_on_final_stays_inside_the_window() -> None:
    """Levelling off below the gate and then diving is an unstable approach.

    The gate is defined by height, not by how close to the slope the
    aircraft was, precisely so this shows up. A tolerance-based rule would
    cut the level segment out and report the approach as on slope.
    """
    stable = grade_land_landing(_straight_in_analysis(), CONFIG)
    level_off = grade_land_landing(_straight_in_analysis(level_off_agl_m=270.0), CONFIG)

    stable_gs = next(c for c in stable.components if c.name == "glideslope")
    level_gs = next(c for c in level_off.components if c.name == "glideslope")
    assert level_gs.score is not None
    assert stable_gs.score is not None
    assert level_gs.score < stable_gs.score


def test_low_approach_without_a_gate_falls_back_to_the_fixed_window() -> None:
    """Nothing to roll out of and never above 1000 ft (a slow rotary-wing
    approach): the fixed lookback is all that is left, and it must still
    produce a window rather than nothing."""
    analysis = _straight_in_analysis(duration_s=90.0, start_agl_m=150.0, speed_ms=20.0)
    result = grade_land_landing(analysis, CONFIG)

    assert result.metrics["final_start_anchor"] is None
    assert result.metrics["final_window_s"] is None
    glideslope = next(c for c in result.components if c.name == "glideslope")
    assert glideslope.evidence["samples"] > 0


def test_path_angle_method_does_not_claim_high_or_low() -> None:
    """Without a resolved runway the reference is the touchdown point and
    the metric is the SLOPE of a fitted line, not a position relative to an
    ideal path. Production landing #2 was dragged in flat and low (40 m
    below a 3 deg path a kilometre out) and the comment said "理想より高め",
    because it had descended steeply before flattening and the regression
    trend picked that up. Report the trend as a trend."""
    result = grade_land_landing(_straight_in_analysis(slope_deg=4.5), CONFIG)

    glideslope = next(c for c in result.components if c.name == "glideslope")
    assert glideslope.evidence["method"] == "path-angle"
    assert "傾きが理想より急だった" in result.comment
    assert "高かった" not in result.comment
    assert "低かった" not in result.comment


def test_comment_never_conjugates_a_na_adjective_as_an_i_adjective() -> None:
    """講評文は活用済みの述語を辞書から引く。語幹に語尾を継ぎ足す作りだと
    「急」+「かった」で「急かった」のような日本語が出る (実際に出た)。"""
    from app.grading.land_grader import _DESCENT_JA, _SPEED_JA

    broken = ("急かった", "滑らかかった", "良好かった", "適正かった", "不明かった")
    comments = [
        grade_land_landing(_pattern_analysis(touchdown_descent_ms=rate), CONFIG).comment
        for rate in (0.5, 1.6, 2.35, 3.5, 6.0)
    ]
    comments.append(grade_land_landing(_straight_in_analysis(slope_deg=4.5), CONFIG).comment)
    comments.append(grade_land_landing(_straight_in_analysis(slope_deg=1.8), CONFIG).comment)
    for comment in comments:
        for bad in broken:
            assert bad not in comment, comment
    # 全ての判定値に対応する述語があること (辞書漏れは英語のまま出る)。
    assert all(v.endswith(("った", "不明")) for v in _DESCENT_JA.values())
    assert all(v.endswith(("った", "かった")) for v in _SPEED_JA.values())


def test_downwind_course_is_the_angle_of_the_leg_not_a_mean_of_samples() -> None:
    """方位差は脚全体を 1 本の直線と見たときの滑走路軸との角度差。

    サンプルごとの進行方向を絶対値平均する作りだと、系統的なずれと
    ふらつきが同じ数字に混ざり、しかも「どちら向きにずれていたか」が
    消えるので軌跡ビューに直線を引けない。
    """
    drifting = _pattern_analysis(downwind_course_error_deg=12.0)
    result = grade_land_landing(drifting, CONFIG)
    pattern = next(c for c in result.components if c.name == "pattern")

    assert pattern.evidence["downwind_course_error_deg"] == pytest.approx(12.0, abs=1.5)
    # 符号付きも出す: ビューはこの傾きで直線を引く。
    assert pattern.evidence["downwind_course_offset_deg"] is not None
    assert abs(pattern.evidence["downwind_course_offset_deg"]) == pytest.approx(
        pattern.evidence["downwind_course_error_deg"], abs=0.01
    )
    # 直線に乗っている脚なので残差はほぼゼロ。
    assert pattern.evidence["downwind_course_rms_m"] < 5.0


def _circuit_with_curved_base(
    *,
    downwind_offset_deg: float = 11.5,
    downwind_s: float = 10.0,
    turn_rate_deg_s: float = 8.0,
    speed_ms: float = 90.0,
    step_s: float = 0.25,
    initial_s: float = 0.0,
    break_rate_deg_s: float = 9.0,
    break_sink_ms: float = 0.0,
) -> "ApproachAnalysis":
    """A circuit flown by integrating a heading, not by joining waypoints.

    The other fixtures interpolate straight between waypoints, so their
    turns have zero turn rate everywhere except at one corner. That cannot
    reproduce what landing #27 hit (leg-finding walking into the base turn)
    nor exercise the break at all.

    With ``initial_s`` the track starts on the initial, flies THROUGH the
    aiming point and breaks onto the downwind -- the only way to test the
    break, which lives entirely past the touchdown point.
    """
    import math

    from app.grading.deviations import ApproachAnalysis, DeviationSample

    # Track angle measured from the runway course: 0 is the landing
    # direction, 180 the reciprocal.
    downwind_angle = 180.0 - downwind_offset_deg
    dtg = 800.0
    lateral = 1500.0
    agl = 460.0
    time = 0.0
    samples: list[DeviationSample] = []

    def add() -> None:
        samples.append(
            DeviationSample(
                time=time,
                distance_to_go=max(dtg, 0.0),
                glideslope_deviation=None,
                centerline_deviation=lateral,
                speed=speed_ms,
                agl=agl,
                signed_distance_to_go=dtg,
            )
        )

    def advance(current_angle: float) -> None:
        nonlocal dtg, lateral
        radians = math.radians(current_angle)
        dtg -= speed_ms * step_s * math.cos(radians)
        lateral += speed_ms * step_s * math.sin(radians)

    if initial_s > 0.0:
        # Rewind to a point on the initial that ends up on the downwind
        # after flying in and breaking; the exact start does not matter, only
        # that the aircraft crosses the aiming point before turning.
        dtg = speed_ms * initial_s
        lateral = 0.0
        angle = 0.0
        for _ in range(int(initial_s / step_s)):
            add()
            advance(angle)
            time += step_s
        while angle < downwind_angle:
            add()
            advance(angle)
            angle = min(downwind_angle, angle + break_rate_deg_s * step_s)
            agl -= break_sink_ms * step_s
            time += step_s

    angle = downwind_angle
    for _ in range(int(downwind_s / step_s)):
        add()
        advance(angle)
        time += step_s

    # Turn all the way onto final, then a short stabilized final.
    while angle > 0.0 or angle < -170.0:
        add()
        advance(angle)
        angle = (angle + turn_rate_deg_s * step_s + 180.0) % 360.0 - 180.0
        agl = max(agl - 3.0 * step_s, 60.0)
        time += step_s
    for _ in range(int(20.0 / step_s)):
        add()
        advance(0.0)
        agl = max(agl - 3.0 * step_s, 5.0)
        time += step_s
    touchdown_time = time
    add()

    return ApproachAnalysis(
        kind="land",
        outcome="full_stop",
        glideslope_deg=3.0,
        course_deg=0.0,
        touchdown_time=touchdown_time,
        touchdown_speed_ms=speed_ms * 0.92,
        touchdown_descent_rate_ms=1.6,
        approach_pattern="overhead",
        airframe="F-16C_50",
        samples=samples,
    )


def test_downwind_leg_ends_where_the_turn_starts_not_where_the_cone_does() -> None:
    """The leg's ends must come from turn rate, never from how close to the
    reciprocal it was.

    Closeness to the reciprocal is the quantity being measured, so using it
    to bound the leg is self-referential -- and biased in the worst
    direction: the further off a downwind is, the less band is left before
    the base turn counts as part of it, the harder the fit is pulled toward
    the turn, and the better aligned the leg is reported to be. Landing #27
    flew a rock-steady 11.6 deg off for 9 s and came out as 6.4 deg with a
    40 m residual, so the line drawn on the plan view visibly missed the
    track.
    """
    from app.grading.config import apply_config_overrides
    from app.grading.pattern import pattern_metrics, segment_approach

    analysis = _circuit_with_curved_base(downwind_offset_deg=11.5, downwind_s=10.0)
    segments = segment_approach(analysis, CONFIG.land_grading)
    metrics = pattern_metrics(analysis, segments)

    assert metrics["downwind_course_offset_deg"] == pytest.approx(11.5, abs=1.5)
    # The fitted line actually lies on the leg.
    assert metrics["downwind_course_rms_m"] < 5.0
    # The turn is not part of the leg.
    assert metrics["downwind_duration_s"] == pytest.approx(10.0, abs=2.0)

    # And the bias is gone: a leg that is off by twice as much reads as
    # twice as much, rather than being dragged back toward the reciprocal.
    wider = _circuit_with_curved_base(downwind_offset_deg=23.0, downwind_s=10.0)
    wider_metrics = pattern_metrics(wider, segment_approach(wider, CONFIG.land_grading))
    assert wider_metrics["downwind_course_offset_deg"] == pytest.approx(23.0, abs=2.0)

    # A cone tight enough to clip the leg must not silently shorten it
    # either -- it should simply stop calling it a downwind.
    tight = apply_config_overrides(CONFIG, {"land_grading": {"downwind_cone_deg": 10.0}})
    clipped = pattern_metrics(wider, segment_approach(wider, tight.land_grading))
    assert (clipped["downwind_duration_s"] or 0.0) < 5.0


def test_break_turn_altitude_is_scored() -> None:
    """The break is a LEVEL 180 deg turn; height lost in it is what puts the
    downwind off pattern altitude, so it is graded.

    The break could not even be FOUND until 2026-08-27: the track-angle
    series was built from the clamped ``distance_to_go``, which is zero for
    everything past the aiming point -- exactly where the break happens.
    Landing #54's series had a 31 s hole in it and the break came out as
    2 samples of the 77 it actually had.
    """
    from app.grading.pattern import pattern_metrics, segment_approach

    level = _circuit_with_curved_base(initial_s=20.0, break_sink_ms=0.0)
    segments = segment_approach(level, CONFIG.land_grading)
    metrics = pattern_metrics(level, segments)

    # A 168 deg break at 9 deg/s is ~19 s of turning.
    assert metrics["break_duration_s"] is not None
    assert metrics["break_duration_s"] > 10.0
    assert metrics["break_altitude_spread_m"] == pytest.approx(0.0, abs=2.0)

    graded = grade_land_landing(level, CONFIG)
    pattern = next(c for c in graded.components if c.name == "pattern")
    assert pattern.evidence["break_judged"] is True
    assert pattern.evidence["sub_scores"]["break_altitude"] == pytest.approx(100.0)


def test_a_break_flown_level_beats_one_flown_downhill() -> None:
    from app.grading.pattern import pattern_metrics, segment_approach

    def spread(analysis) -> float:
        return pattern_metrics(analysis, segment_approach(analysis, CONFIG.land_grading))[
            "break_altitude_spread_m"
        ]

    level = _circuit_with_curved_base(initial_s=20.0, break_sink_ms=0.0)
    # 5 m/s of sink through a ~19 s break is ~95 m -- the middle of the
    # 19-294 m range measured on this server's real overhead landings.
    sinking = _circuit_with_curved_base(initial_s=20.0, break_sink_ms=5.0)
    assert spread(sinking) > 60.0
    assert spread(sinking) > spread(level)

    def break_score(analysis) -> float:
        component = next(
            c for c in grade_land_landing(analysis, CONFIG).components if c.name == "pattern"
        )
        return component.evidence["sub_scores"]["break_altitude"]

    assert break_score(sinking) < break_score(level)
    assert "ブレイク中に高度が" in grade_land_landing(sinking, CONFIG).comment


def test_a_recording_that_never_caught_the_break_is_not_judged_on_it() -> None:
    """Older captures start on the downwind. Absent is not the same as bad."""
    from app.grading.pattern import pattern_metrics, segment_approach

    no_break = _circuit_with_curved_base(initial_s=0.0)
    metrics = pattern_metrics(no_break, segment_approach(no_break, CONFIG.land_grading))
    assert metrics["break_duration_s"] is None

    pattern = next(
        c for c in grade_land_landing(no_break, CONFIG).components if c.name == "pattern"
    )
    assert pattern.evidence["break_judged"] is False
    assert "break_altitude" not in pattern.evidence["sub_scores"]


def test_a_component_that_could_not_be_measured_carries_no_score() -> None:
    """Unmeasurable is not "average".

    A neutral 50 reads as a verdict -- "we looked, it was middling" -- on
    something nobody measured, and it lands at a fifth to a quarter of the
    weight. Across 403 production land landings it was doing that to 216
    glideslope components, 82 centerline and 39 speed. The component has to
    drop out of the weighted mean instead, saying why.
    """
    from app.grading.deviations import DeviationSample

    # Short final inside the 200 m distance floor: no approach angle to
    # derive, but a real approach segment exists (40 m AGL, above the
    # 100 ft minimum-flight gate) and a reference speed to judge. The
    # drop-out must not need an airframe class to trigger.
    touchdown_time = 100.0
    analysis = ApproachAnalysis(
        kind="land",
        outcome="full_stop",
        glideslope_deg=3.0,
        course_deg=0.0,
        touchdown_time=touchdown_time,
        touchdown_speed_ms=2.0,
        touchdown_descent_rate_ms=0.5,
        airframe="TF-51D",  # not rotary: the drop-out must not need a class
        samples=[
            DeviationSample(
                time=touchdown_time - 8.0 + i * 0.25,
                distance_to_go=150.0,
                glideslope_deviation=1.0,
                centerline_deviation=0.4,
                speed=2.0,
                agl=40.0,
            )
            for i in range(32)
        ],
    )
    result = grade_land_landing(analysis, CONFIG)

    glideslope = next(c for c in result.components if c.name == "glideslope")
    assert glideslope.score is None
    assert glideslope.evidence["unscored_reason"] == "not-measured"
    assert result.metrics["unmeasured_components"] == ["glideslope"]
    # 残った項目だけで正規化する: 測れなかったことが減点になっていない。
    assert result.metrics["measured_weight"] == pytest.approx(0.75)
    measured = [c for c in result.components if c.score is not None]
    expected = sum(c.score * c.weight for c in measured) / 0.75
    assert result.score == pytest.approx(expected, abs=0.05)


def test_the_touchdown_speed_reference_is_never_the_touchdown_itself() -> None:
    """A recording that caught only the touchdown cannot judge its speed.

    The old fallback averaged "all samples", which includes the touchdown
    sample -- so with a single-sample track the reference WAS the number
    being judged, the ratio came out 1.00, and the component scored 100 for
    a landing whose approach was never recorded. 37 of 403 production
    landings hold exactly one approach sample.
    """
    from app.grading.deviations import DeviationSample

    analysis = ApproachAnalysis(
        kind="land",
        outcome="full_stop",
        glideslope_deg=3.0,
        course_deg=0.0,
        touchdown_time=100.0,
        touchdown_speed_ms=70.0,
        touchdown_descent_rate_ms=1.0,
        airframe="F-16C_50",
        samples=[
            DeviationSample(
                time=100.0,
                distance_to_go=0.0,
                glideslope_deviation=0.0,
                centerline_deviation=0.0,
                speed=70.0,
                agl=0.5,
            )
        ],
    )
    result = grade_land_landing(analysis, CONFIG)

    speed = next(c for c in result.components if c.name == "touchdown_speed")
    assert speed.score is None
    assert speed.evidence["speed_ratio"] is None
    assert speed.evidence["speed_reference"] == "none"


def test_a_landing_nobody_recorded_the_approach_of_gets_no_grade() -> None:
    """Renormalising the weights is right up to a point, and past it the
    number stops being a grade: with only the descent rate left, a smooth
    touchdown scored 100/A for an approach that was never recorded."""
    from app.grading.deviations import DeviationSample

    analysis = ApproachAnalysis(
        kind="land",
        outcome="full_stop",
        glideslope_deg=3.0,
        course_deg=0.0,
        touchdown_time=100.0,
        touchdown_speed_ms=70.0,
        touchdown_descent_rate_ms=0.3,  # very smooth -> descent_rate 100
        airframe="F-16C_50",
        samples=[
            DeviationSample(
                time=100.0,
                distance_to_go=0.0,
                glideslope_deviation=0.0,
                centerline_deviation=None,
                speed=70.0,
                agl=0.5,
            )
        ],
    )
    result = grade_land_landing(analysis, CONFIG)

    assert result.metrics["measured_components"] == ["descent_rate"]
    assert result.metrics["measured_weight"] == pytest.approx(0.30)
    assert result.metrics["graded"] is False
    assert result.grade is None
    assert result.score is None
    assert "成績を付けていません" in result.comment


def test_rotary_wing_speed_and_glidepath_are_measured_but_not_scored() -> None:
    """The touchdown/approach speed ratio and the 3 deg reference slope are
    fixed-wing quantities. A helicopter decelerates to the hover, so the
    ratio is undefined by construction -- production rotary landings ran
    0.17-1.39 with a median of 0.57 and 84 of 130 scored <= 30 -- and its
    approach angle has no published reference to judge against. Both are
    reported and left out of the score."""
    helicopter = grade_land_landing(
        _pattern_analysis(
            airframe="UH-1H",
            downwind_speed_ms=40.0,
            final_speed_ms=25.0,
            touchdown_speed_ms=3.0,
            touchdown_descent_ms=0.4,
        ),
        CONFIG,
    )

    speed = next(c for c in helicopter.components if c.name == "touchdown_speed")
    glideslope = next(c for c in helicopter.components if c.name == "glideslope")
    assert speed.score is None and glideslope.score is None
    for component in (speed, glideslope):
        assert component.evidence["unscored_reason"] == "not-applicable-to-airframe-class"
    # 測定値そのものは根拠として残す (採点しないだけ)。
    assert speed.evidence["speed_ratio"] is not None
    assert "採点していません" in helicopter.comment

    # 同じ軌跡を戦闘機で飛べば、どちらも採点される。
    fighter = grade_land_landing(_pattern_analysis(airframe="F-16C_50"), CONFIG)
    assert all(c.score is not None for c in fighter.components)


def test_the_speed_reference_is_the_speed_held_on_final_not_the_whole_final() -> None:
    """The bands are a tolerance around a speed that was HELD.

    The final now starts at the roll-out or the 1000 ft gate, which on a
    straight-in is a minute and several km out, so averaging all of it
    includes the deceleration onto approach speed and reads high. Measured
    over 165 production fixed-wing landings, the whole-final mean put the
    median ratio at 0.87 -- under the 0.88 "on speed" floor -- against 0.91
    over the last 10 s.
    """
    from app.grading.config import apply_config_overrides
    from app.grading.deviations import DeviationSample

    # A straight-in from above the 1000 ft gate (so the final is cut at the
    # gate, as on a real one), decelerating 120 -> 80 m/s and touching down
    # at 78: on speed against what it settled on, slow against the average
    # of a final that still holds the deceleration.
    touchdown_time = 200.0
    import math

    tan_slope = math.tan(math.radians(3.0))
    samples = []
    for step in range(121):
        fraction = step / 120.0
        dtg = 8000.0 * (1.0 - fraction)
        # 120 kt-ish inbound, slowed onto approach speed in the middle third,
        # then held at 80 for the last ~20 s.
        speed = 120.0 - 40.0 * min(1.0, max(0.0, (fraction - 0.5) / 0.35))
        samples.append(
            DeviationSample(
                time=touchdown_time - 120.0 + step,
                distance_to_go=dtg,
                glideslope_deviation=0.0,
                centerline_deviation=0.0,
                speed=speed,
                agl=dtg * tan_slope,
                signed_distance_to_go=dtg,
            )
        )
    analysis = ApproachAnalysis(
        kind="land",
        outcome="full_stop",
        glideslope_deg=3.0,
        course_deg=0.0,
        touchdown_time=touchdown_time,
        touchdown_speed_ms=78.0,
        touchdown_descent_rate_ms=1.5,
        airframe="F-16C_50",
        samples=samples,
    )

    held = grade_land_landing(analysis, CONFIG)
    speed = next(c for c in held.components if c.name == "touchdown_speed")
    assert speed.evidence["mean_approach_speed_ms"] == pytest.approx(80.0, abs=1.0)
    assert speed.score == pytest.approx(100.0)

    # 窓を長く取れば減速区間が混ざり、同じ着陸が「遅い」に転ぶ。
    whole = grade_land_landing(
        analysis,
        apply_config_overrides(CONFIG, {"land_grading": {"speed_reference_window_s": 300.0}}),
    )
    whole_speed = next(c for c in whole.components if c.name == "touchdown_speed")
    assert whole_speed.evidence["mean_approach_speed_ms"] > 95.0
    assert whole_speed.evidence["verdict"] == "slow"
    assert whole_speed.score is not None
    assert whole_speed.score < 50.0


# ---------------------------------------------------------------------------
# Minimum-flight gate: a hop is not an approach
# ---------------------------------------------------------------------------


def _hop_analysis(*, agl_m: float, airframe: str = "UH-1H") -> "ApproachAnalysis":
    """A helicopter that lifts to ``agl_m`` for a few seconds and sets down.

    Gentle and on the centerline, so descent-rate + centerline alone would
    score 100 -- the exact case that used to come out an A.
    """
    from app.grading.deviations import ApproachAnalysis, DeviationSample

    touchdown_time = 100.0
    return ApproachAnalysis(
        kind="land",
        outcome="full_stop",
        glideslope_deg=3.0,
        course_deg=0.0,
        touchdown_time=touchdown_time,
        touchdown_speed_ms=3.0,
        touchdown_descent_rate_ms=0.4,
        airframe=airframe,
        samples=[
            DeviationSample(
                time=touchdown_time - 5.0 + i * 0.25,
                distance_to_go=10.0,
                glideslope_deviation=1.0,
                centerline_deviation=0.3,
                speed=3.0,
                agl=agl_m,
            )
            for i in range(20)
        ],
    )


def test_a_hop_that_never_reaches_100ft_gets_no_grade() -> None:
    """A few metres up and straight back down is not an approach.

    Before the minimum-flight gate this scored 100/A on descent rate +
    centerline keeping (rotary weight 0.55 clears the 0.5 coverage floor).
    The components and evidence stay; the grade must not.
    """
    result = grade_land_landing(_hop_analysis(agl_m=5.0), CONFIG)

    assert result.metrics["max_agl_m"] == pytest.approx(5.0)
    assert result.metrics["graded"] is False
    assert result.metrics["ungraded_reason"] == "insufficient-flight"
    assert result.grade is None
    assert result.score is None
    # The touchdown itself is still described; only the verdict is withheld.
    assert "成績を付けていません" in result.comment
    assert "100 ft" in result.comment
    # Components are kept for evidence, not zeroed.
    by_name = {c.name: c for c in result.components}
    assert by_name["descent_rate"].score == pytest.approx(100.0)
    assert by_name["centerline"].score == pytest.approx(100.0)


def test_the_same_hop_above_100ft_is_graded() -> None:
    """Control: the gate is about the amount of flying, not the airframe."""
    result = grade_land_landing(_hop_analysis(agl_m=40.0), CONFIG)

    assert result.metrics["max_agl_m"] == pytest.approx(40.0)
    assert result.metrics["graded"] is True
    assert result.metrics["ungraded_reason"] is None
    assert result.grade is not None
    assert result.score is not None


def test_the_minimum_flight_gate_can_be_disabled() -> None:
    """Setting ``min_flight_agl_m`` to 0 restores the old behaviour."""
    from app.grading.config import apply_config_overrides

    config = apply_config_overrides(CONFIG, {"land_grading": {"min_flight_agl_m": 0}})
    result = grade_land_landing(_hop_analysis(agl_m=5.0), config)

    assert result.metrics["graded"] is True
    assert result.grade == "A"
    assert result.score == pytest.approx(100.0)


def test_a_hop_reports_insufficient_flight_even_when_coverage_is_also_short() -> None:
    """The flight gate is the more fundamental reason: with a single sample
    the coverage floor would already withhold the grade, but a hop that
    never flew must say so rather than blame the recording."""
    from app.grading.deviations import ApproachAnalysis, DeviationSample

    touchdown_time = 100.0
    analysis = ApproachAnalysis(
        kind="land",
        outcome="full_stop",
        glideslope_deg=3.0,
        course_deg=0.0,
        touchdown_time=touchdown_time,
        touchdown_speed_ms=70.0,
        touchdown_descent_rate_ms=0.3,
        airframe="F-16C_50",
        samples=[
            DeviationSample(
                time=touchdown_time - 2.0,
                distance_to_go=10.0,
                glideslope_deviation=0.0,
                centerline_deviation=None,
                speed=70.0,
                agl=4.0,
            ),
            DeviationSample(
                time=touchdown_time,
                distance_to_go=0.0,
                glideslope_deviation=0.0,
                centerline_deviation=None,
                speed=70.0,
                agl=0.5,
            ),
        ],
    )
    result = grade_land_landing(analysis, CONFIG)

    assert result.metrics["ungraded_reason"] == "insufficient-flight"
    assert result.grade is None
