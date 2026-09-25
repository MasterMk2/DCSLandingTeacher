"""荷重倍数・旋回率の導出 (app.grading.kinematics)。

合成軌跡は物理を積分して作る: バンク角 phi の水平定常旋回は
``omega = g * tan(phi) / v`` で回り、法線荷重倍数は ``1 / cos(phi)``。
位置は保存時と同じ 1 cm に丸め、サンプル間隔は実記録と同じ 0.21 秒前後の
不等間隔にして、量子化ノイズが加速度に化けないことをここで押さえる。
"""

from __future__ import annotations

import math
import random

import pytest

from app.grading.deviations import ApproachAnalysis, DeviationSample
from app.grading.kinematics import (
    G0,
    annotate_kinematics,
    bank_from_load_factor,
    fit_kinematics,
)


def _track(
    *,
    bank_deg: float,
    speed_ms: float = 100.0,
    duration_s: float = 30.0,
    climb_ms: float = 0.0,
    step_s: float = 0.21,
    jitter_s: float = 0.03,
    quantize_m: float | None = 0.01,
    seed: int = 1,
) -> tuple[list[float], list[tuple[float, float, float]]]:
    """Level (or steadily climbing) turn at constant bank, integrated exactly."""
    rng = random.Random(seed)
    omega = G0 * math.tan(math.radians(bank_deg)) / speed_ms if bank_deg else 0.0
    times: list[float] = []
    positions: list[tuple[float, float, float]] = []
    t = 0.0
    while t <= duration_s:
        heading = omega * t
        if omega:
            x = speed_ms / omega * math.sin(heading)
            y = speed_ms / omega * (1.0 - math.cos(heading))
        else:
            x, y = speed_ms * t, 0.0
        z = 300.0 + climb_ms * t
        if quantize_m:
            x, y, z = (round(c / quantize_m) * quantize_m for c in (x, y, z))
        times.append(round(t, 3))
        positions.append((x, y, z))
        t += step_s + rng.uniform(-jitter_s, jitter_s)
    return times, positions


def _mid(times: list[float]) -> int:
    return len(times) // 2


@pytest.mark.parametrize("bank_deg", [30.0, 45.0, 60.0, 70.0])
def test_level_turn_load_factor_is_one_over_cos_bank(bank_deg: float) -> None:
    times, positions = _track(bank_deg=bank_deg)
    kin = fit_kinematics(times, positions, _mid(times))
    assert kin is not None
    expected = 1.0 / math.cos(math.radians(bank_deg))
    assert kin.load_factor == pytest.approx(expected, rel=0.02)
    # Right-hand turn: the track angle grows toward +y.
    omega_deg = math.degrees(G0 * math.tan(math.radians(bank_deg)) / 100.0)
    assert kin.turn_rate_deg_s == pytest.approx(omega_deg, rel=0.02)


def test_straight_and_level_reads_one_g_and_no_turn() -> None:
    times, positions = _track(bank_deg=0.0)
    kin = fit_kinematics(times, positions, _mid(times))
    assert kin is not None
    assert kin.load_factor == pytest.approx(1.0, abs=0.01)
    assert kin.turn_rate_deg_s == pytest.approx(0.0, abs=0.05)
    assert kin.ground_speed == pytest.approx(100.0, rel=0.01)


def test_a_steady_climb_does_not_add_load() -> None:
    """Constant vertical speed is not acceleration; only the direction of the
    lift vector changes, so n stays cos(gamma) ~ 1."""
    times, positions = _track(bank_deg=0.0, climb_ms=10.0)
    kin = fit_kinematics(times, positions, _mid(times))
    assert kin is not None
    assert kin.load_factor == pytest.approx(1.0, abs=0.01)


def test_quantisation_does_not_leak_into_the_acceleration() -> None:
    """1 cm rounding on a 5 Hz track, differentiated twice naively, is ~1 G
    of noise. The windowed fit must keep it far below that."""
    times, positions = _track(bank_deg=0.0, quantize_m=0.01, jitter_s=0.05)
    loads = [
        fit_kinematics(times, positions, i).load_factor  # type: ignore[union-attr]
        for i in range(5, len(times) - 5)
    ]
    assert max(abs(n - 1.0) for n in loads) < 0.02


def test_too_few_points_or_too_short_a_window_yields_nothing() -> None:
    times, positions = _track(bank_deg=45.0, duration_s=0.6)
    assert fit_kinematics(times, positions, 1) is None
    # Four points spanning a second is still below the point floor, however
    # far the window is allowed to widen.
    assert fit_kinematics([0.0, 0.4, 0.8, 1.2], [(0, 0, 0)] * 4, 1) is None


def test_a_sparse_one_hertz_track_widens_the_window_instead_of_giving_up() -> None:
    """Some exports (and every synthetic fixture) sample at 1 Hz: three points
    in the default +-1 s window. The fit widens to +-2 s and still reads the
    turn, a little smoothed."""
    times, positions = _track(bank_deg=60.0, step_s=1.0, jitter_s=0.0)
    kin = fit_kinematics(times, positions, _mid(times))
    assert kin is not None
    assert kin.load_factor == pytest.approx(2.0, rel=0.05)


def _analysis(
    times: list[float],
    positions: list[tuple[float, float, float]],
    *,
    signed: bool = True,
) -> ApproachAnalysis:
    samples = [
        DeviationSample(
            time=t,
            distance_to_go=max(-x, 0.0),
            glideslope_deviation=None,
            centerline_deviation=y,
            agl=z,
            signed_distance_to_go=-x if signed else None,
        )
        for t, (x, y, z) in zip(times, positions)
    ]
    return ApproachAnalysis(
        kind="land",
        outcome="full_stop",
        glideslope_deg=3.0,
        course_deg=0.0,
        touchdown_time=times[-1] + 1.0,
        touchdown_speed_ms=None,
        touchdown_descent_rate_ms=0.0,
        samples=samples,
    )


def test_annotate_fills_every_sample_it_can_and_recomputes_on_repeat() -> None:
    times, positions = _track(bank_deg=60.0)
    analysis = _analysis(times, positions)
    # A stale value from an older derivation must not survive.
    analysis.samples[_mid(times)].load_factor = 9.9
    report = annotate_kinematics(analysis)
    assert report.annotated >= len(times) - 4
    assert report.rejected == 0
    mid = analysis.samples[_mid(times)]
    assert mid.load_factor == pytest.approx(2.0, rel=0.02)
    assert mid.turn_rate_deg_s is not None and mid.turn_rate_deg_s > 0
    payload = mid.as_dict()
    assert payload["load_factor"] == mid.load_factor
    assert payload["turn_rate_deg_s"] == mid.turn_rate_deg_s
    rebuilt = ApproachAnalysis.from_dict(analysis.as_dict())
    assert rebuilt.samples[_mid(times)].load_factor == mid.load_factor


def test_a_clamped_track_past_the_reference_point_is_not_differentiated() -> None:
    """Old rows only hold ``distance_to_go``, clamped at zero past the aiming
    point -- which is where the break lives. A derivative through a run of
    zeros reads as a dead stop and a violent deceleration; refuse instead."""
    times, positions = _track(bank_deg=60.0)
    # Shift the whole turn to the far side of the reference point.
    shifted = [(x + 5000.0, y, z) for x, y, z in positions]
    analysis = _analysis(times, shifted, signed=False)
    annotate_kinematics(analysis)
    assert all(s.load_factor is None for s in analysis.samples)
    # With the signed position the same track is fully usable.
    annotate_kinematics(signed := _analysis(times, shifted, signed=True))
    assert signed.samples[_mid(times)].load_factor == pytest.approx(2.0, rel=0.02)


def test_repeated_positions_from_partial_updates_are_skipped() -> None:
    times, positions = _track(bank_deg=45.0)
    # Duplicate every fourth position onto the next timestamp, the way an
    # ACMI partial update repeats the last known transform.
    for i in range(4, len(positions), 4):
        positions[i] = positions[i - 1]
    analysis = _analysis(times, positions)
    annotate_kinematics(analysis)
    mid = _mid(times)
    loads = [
        s.load_factor for s in analysis.samples[mid - 5 : mid + 5] if s.load_factor
    ]
    assert loads
    assert max(loads) == pytest.approx(1.0 / math.cos(math.radians(45.0)), rel=0.05)


def test_a_position_glitch_is_rejected_instead_of_read_as_g() -> None:
    """Landing #599: three samples inside a 2 G turn belong to some other
    trajectory (100 m below, 30 m along). Fitted as recorded they read as
    9-12 G; rejected, the turn reads as the 2 G it was and the glitch
    samples carry no value at all."""
    times, positions = _track(bank_deg=60.0)
    mid = _mid(times)
    for i in (mid, mid + 1, mid + 2):
        x, y, z = positions[i]
        positions[i] = (x + 30.0, y, z - 100.0)
    analysis = _analysis(times, positions)
    report = annotate_kinematics(analysis)
    assert report.rejected == 3
    assert all(analysis.samples[i].load_factor is None for i in (mid, mid + 1, mid + 2))
    loads = [s.load_factor for s in analysis.samples if s.load_factor is not None]
    assert max(loads) == pytest.approx(2.0, rel=0.03)
    assert min(loads) == pytest.approx(2.0, rel=0.03)


def test_timestamp_jitter_is_not_rejected() -> None:
    """The normal noise of a real recording is time-like: positions are true
    but stamped a few tens of ms off (measured p99 ~0.05 s). At 100 m/s that
    is metres of residual, well inside the tolerance, and the derived G must
    stay honest without throwing samples away."""
    rng = random.Random(7)
    times, positions = _track(bank_deg=45.0, quantize_m=None)
    jittered = [round(t + rng.uniform(-0.03, 0.03), 3) for t in times]
    analysis = _analysis(jittered, positions)
    report = annotate_kinematics(analysis)
    assert report.rejected == 0
    loads = [s.load_factor for s in analysis.samples if s.load_factor is not None]
    expected = 1.0 / math.cos(math.radians(45.0))
    off = sorted(abs(n - expected) / expected for n in loads)
    # 90% of samples within 10%, none wildly off.
    assert off[int(0.9 * len(off))] < 0.10
    assert off[-1] < 0.5


def test_a_single_frame_lag_at_high_speed_is_rejected() -> None:
    """A sample whose position is one 0.2 s frame stale at 250 m/s sits 50 m
    behind the trajectory: the commonest stutter in the stream."""
    times, positions = _track(bank_deg=30.0, speed_ms=250.0)
    mid = _mid(times)
    positions[mid] = positions[mid - 1]
    # _track de-duplication does not apply here; make it a near-duplicate.
    x, y, z = positions[mid]
    positions[mid] = (x + 0.5, y, z)
    report = annotate_kinematics(_analysis(times, positions))
    assert report.rejected == 1


def test_bank_from_load_factor_inverts_the_level_turn_relation() -> None:
    assert bank_from_load_factor(2.0) == pytest.approx(60.0)
    assert bank_from_load_factor(1.0) == 0.0
    assert bank_from_load_factor(0.8) == 0.0
