"""軌跡から運動量 (荷重倍数・旋回率) を導く。

ACMI には加速度計の値は入っていない。DCS の Tacview エクスポータが機体に
ついて書くのは位置・姿勢・識別情報だけで、``TAS`` すら無い (Issue D-2)。
だが位置が ~5 Hz で入っていれば、その 2 階微分が加速度であり、加速度から
重力を引いたものが機体に働く比力 (specific force) --- つまり G 計そのもの
である。ここではそれを進入軌跡 (滑走路座標系) に対して行う。

導出は **局所 2 次多項式の最小二乗フィット** で行う。各サンプルについて
前後 ``half_window_s`` 秒のサンプルに ``p(t) = p0 + v*t + a*t^2/2`` を
当て、``v`` と ``a`` を係数として読む。隣接差分を 2 回取る方式は 5 Hz
では位置の量子化 (保存時 1 cm 丸め) が ``a`` に 1 G 単位で化けるので
使えない。窓を取って直線・放物線で均すのはその対策で、Savitzky-Golay
フィルタと同じ考え方 (不等間隔サンプルに対応するため毎回正規方程式を
解いている)。

出力:

- ``load_factor``: 法線荷重倍数 (単位 G)。比力 ``f = a - g_vec`` のうち
  速度ベクトルに直交する成分の大きさを ``g`` で割ったもの。水平定常旋回
  なら ``sqrt(1 + (v*omega/g)^2)`` = ``1/cos(バンク角)``、直線水平飛行なら
  1.0。進行方向の成分 (推力 - 抗力) は入れない。コクピットの G 計が
  読むのはこの法線成分で、ブレイクで「4 G 引いた」と言うときの G もこれ。
- ``turn_rate_deg_s``: 対地トラック角の変化率 (度/秒)。右旋回 (横ずれが
  +側へ増える向き) が正。

計算できない条件 (``None``):

- 窓を ``MAX_HALF_WINDOW_S`` まで広げても 5 点未満、または窓の時間幅が
  ``half_window_s`` 未満 (記録の切れ目、部分更新で位置が重複したサンプル)。
- 速度が ``MIN_SPEED_MS`` 未満 (進行方向が定義できない = ホバリング・
  地上停止)。
- 高度 (``agl``) が無い、または滑走路軸上の位置が ``distance_to_go`` の
  クランプ済み値しか無い (基準点より先が全部 0 に潰れている古い記録)。

検証: ``scripts/validate-load-factor.py`` が実記録の Roll と突き合わせる。
水平定常旋回では ``1/cos(roll)`` と一致するはずで、その一致度がこの
導出の精度の実測値になる (結果は docs/grading-references.md)。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.grading.deviations import ApproachAnalysis, DeviationSample

#: 標準重力加速度 (m/s^2)。荷重倍数の分母。
G0 = 9.80665

#: フィット窓の片側幅 (秒)。両側で 2 秒 = 5 Hz なら 10 点。短いと量子化
#: ノイズが加速度に乗り、長いとブレイクの G 立ち上がり (1-2 秒) が鈍る。
DEFAULT_HALF_WINDOW_S = 1.0

#: サンプルが疎な記録 (1 Hz のエクスポート、合成データ) では既定の窓に
#: 点が足りない。その場合はここまで窓を広げてから諦める: 1 Hz なら
#: ±2 秒で 5 点。広げた分だけ G の立ち上がりは鈍るが、無いよりは良い。
MAX_HALF_WINDOW_S = 3.0

#: 窓に最低限必要な点数。2 次 (3 係数) を当てるので 3 点で解けるが、
#: それでは残差ゼロの当てはめになり平滑化の意味が無い。
MIN_WINDOW_POINTS = 5

#: この速度未満では進行方向が定義できず、法線成分も定義できない。
MIN_SPEED_MS = 5.0

#: 位置の 2 階微分から出る加速度は、サンプル間隔より短い時間の運動は
#: 見えない。この値を超える荷重倍数は物理的に機体の運動ではなく記録の
#: 破綻 (位置の飛び、タイムスタンプの欠落) なので捨てる。有人機の構造
#: 限界 (9 G 級) より十分上に置いてある。
MAX_PLAUSIBLE_LOAD_FACTOR = 15.0

#: 位置の飛びの判定: そのサンプルを除いた前後 1 秒のフィットが予測する
#: 位置から、記録の位置が ``max(OUTLIER_RESIDUAL_M, OUTLIER_RESIDUAL_S *
#: 速度)`` より離れていれば外れ値として捨てる。
#:
#: 本番の 300 記録・264,305 サンプルで測った予測残差 (leave-one-out) は
#: 速度に比例して広がる: p99 が 100 m/s 未満で 4.9 m、100-200 で 7.2 m、
#: 200-300 で 11.4 m、300 超で 28 m。残差を速度で割ると p99 は 0.05-0.10 s
#: に揃うので、正体は位置ではなく **タイムスタンプのジッター** (書かれた
#: 時刻と位置の時刻が数十 ms ずれる) で、機体の運動が 2 次式から外れる分
#: (10 G・200 m/s の旋回でも 3 次項は ~4 m) より大きい。
#:
#: なので閾値は時間で置く: 0.05 秒分 (p99 相当) を超える残差は記録の側の
#: 問題 --- 1 フレーム丸ごと遅れた位置 (0.2 s = 140 m/s で 28 m)、別軌跡の
#: 混入 (#599 では 93 m/s で 109 m = 1.2 s 分)、そして 200 m/s で 10 m 級の
#: スタッターの塊 (#1433: 1 G → 10.7 G → 3 G → 9.6 G → 2 G が 2 秒の中に
#: 並ぶ)。低速機で時間基準が数 m まで縮まないよう 10 m の床を置く (ヘリの
#: p99.9 は 9.6 m)。
#:
#: 300 記録で振った結果 (除外率 / ブレイク最大 G の p99 / 最大):
#: なし 0% / 10.81 / 12.52、0.10 s 0.15% / 10.05 / 12.14、
#: 0.05 s 0.28% / 9.96 / 10.81、床 5 m + 0.05 s 0.83% / 9.96 / 10.81 だが
#: 中央値が 2.71 → 2.62 と動く (正常な点まで捨て始める)。0.05 s / 10 m は
#: 中央値を変えず (2.71 → 2.69) に飛びだけ落とす。
OUTLIER_RESIDUAL_M = 10.0
OUTLIER_RESIDUAL_S = 0.05

#: 飛びは数サンプル続くことがあり (#599 は 4 点)、隣の飛んだ点に支えられて
#: 残差が小さく見える点は 1 回目では残る。捨てたものを窓から外して残差を
#: 測り直すのをこの回数まで繰り返す。
OUTLIER_PASSES = 3


@dataclass(frozen=True)
class Kinematics:
    """1 サンプルの運動量。座標は滑走路座標系 (x = 進入方向、y = 右、z = 上)。"""

    velocity: tuple[float, float, float]
    acceleration: tuple[float, float, float]
    #: フィットが基準時刻に置く位置。記録上の位置との差が「予測残差」で、
    #: 位置の飛びを見つけるのに使う (annotate_kinematics)。
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @property
    def speed(self) -> float:
        return math.sqrt(sum(c * c for c in self.velocity))

    @property
    def ground_speed(self) -> float:
        vx, vy, _ = self.velocity
        return math.hypot(vx, vy)

    @property
    def load_factor(self) -> float | None:
        """法線荷重倍数 (G)。速度が定義できないときは ``None``。"""
        speed = self.speed
        if speed < MIN_SPEED_MS:
            return None
        ax, ay, az = self.acceleration
        # 比力 = 加速度 - 重力加速度ベクトル。重力は -z 向きなので +G0。
        fx, fy, fz = ax, ay, az + G0
        vx, vy, vz = (c / speed for c in self.velocity)
        along = fx * vx + fy * vy + fz * vz
        nx, ny, nz = fx - along * vx, fy - along * vy, fz - along * vz
        return math.sqrt(nx * nx + ny * ny + nz * nz) / G0

    @property
    def turn_rate_deg_s(self) -> float | None:
        """対地トラック角の変化率 (度/秒)。右旋回が正。"""
        vx, vy, _ = self.velocity
        ax, ay, _ = self.acceleration
        horizontal_sq = vx * vx + vy * vy
        if horizontal_sq < MIN_SPEED_MS * MIN_SPEED_MS:
            return None
        return math.degrees((vx * ay - vy * ax) / horizontal_sq)


def _solve3(matrix: list[list[float]], rhs: list[float]) -> list[float] | None:
    """3x3 連立一次方程式をガウス消去 (部分ピボット) で解く。特異なら None。"""
    a = [row[:] + [value] for row, value in zip(matrix, rhs)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            return None
        a[col], a[pivot] = a[pivot], a[col]
        for row in range(3):
            if row == col:
                continue
            factor = a[row][col] / a[col][col]
            for k in range(col, 4):
                a[row][k] -= factor * a[col][k]
    return [a[i][3] / a[i][i] for i in range(3)]


def fit_kinematics(
    times: list[float],
    positions: list[tuple[float, float, float]],
    index: int,
    half_window_s: float = DEFAULT_HALF_WINDOW_S,
    exclude: set[int] | frozenset[int] | None = None,
) -> Kinematics | None:
    """``index`` のサンプルにおける速度・加速度を局所 2 次フィットで求める。

    ``times`` は昇順であること。窓は ``times[index] +- half_window_s`` から
    始め、点が :data:`MIN_WINDOW_POINTS` に足りなければ
    :data:`MAX_HALF_WINDOW_S` まで倍々に広げる。``exclude`` の添字は窓から
    外す (外れ値、または残差を測るために外す当のサンプル自身)。
    """
    t0 = times[index]
    excluded = exclude or ()
    half = half_window_s
    while True:
        lo = index
        while lo > 0 and t0 - times[lo - 1] <= half:
            lo -= 1
        hi = index
        while hi + 1 < len(times) and times[hi + 1] - t0 <= half:
            hi += 1
        members = [j for j in range(lo, hi + 1) if j not in excluded]
        if len(members) >= MIN_WINDOW_POINTS:
            break
        if half >= MAX_HALF_WINDOW_S:
            return None
        half = min(MAX_HALF_WINDOW_S, half * 2.0)
    if times[members[-1]] - times[members[0]] < half_window_s:
        return None

    # 正規方程式。基底は [1, tau, tau^2 / 2] なので 3 番目の係数がそのまま加速度。
    s = [0.0] * 5  # sum of tau^k, k = 0..4
    b = [[0.0, 0.0, 0.0] for _ in range(3)]  # per axis: sum of p * basis_k
    for j in members:
        tau = times[j] - t0
        tau2 = tau * tau
        basis = (1.0, tau, tau2 / 2.0)
        s[0] += 1.0
        s[1] += tau
        s[2] += tau2
        s[3] += tau2 * tau
        s[4] += tau2 * tau2
        px, py, pz = positions[j]
        for k in range(3):
            b[0][k] += px * basis[k]
            b[1][k] += py * basis[k]
            b[2][k] += pz * basis[k]
    matrix = [
        [s[0], s[1], s[2] / 2.0],
        [s[1], s[2], s[3] / 2.0],
        [s[2] / 2.0, s[3] / 2.0, s[4] / 4.0],
    ]
    solved = [_solve3(matrix, b[axis]) for axis in range(3)]
    if any(coefficients is None for coefficients in solved):
        return None
    position = tuple(coefficients[0] for coefficients in solved)  # type: ignore[index]
    velocity = tuple(coefficients[1] for coefficients in solved)  # type: ignore[index]
    acceleration = tuple(coefficients[2] for coefficients in solved)  # type: ignore[index]
    return Kinematics(velocity, acceleration, position)  # type: ignore[arg-type]


def _position_of(sample: "DeviationSample") -> tuple[float, float, float] | None:
    """滑走路座標系での位置 (x = 進入方向に正、y = 右、z = 上)。

    ``distance_to_go`` は 0 でクランプされているので、符号付きの値を持たない
    古い記録では基準点より先 (ブレイク・アップウィンド) が全部同じ x に
    潰れている。そこで微分すると「一瞬で止まった」ように見えるので、
    クランプに掛かった点は位置不明として扱う。
    """
    if sample.agl is None:
        return None
    if sample.signed_distance_to_go is not None:
        along = -sample.signed_distance_to_go
    elif sample.distance_to_go > 0.0:
        along = -sample.distance_to_go
    else:
        return None
    if sample.centerline_deviation is None:
        return None
    return (along, sample.centerline_deviation, sample.agl)


def reject_position_outliers(
    times: list[float],
    positions: list[tuple[float, float, float]],
    half_window_s: float = DEFAULT_HALF_WINDOW_S,
    floor_m: float = OUTLIER_RESIDUAL_M,
    tolerance_s: float = OUTLIER_RESIDUAL_S,
    passes: int = OUTLIER_PASSES,
) -> set[int]:
    """位置の飛びの添字を返す。

    各サンプルを、そのサンプルと既に捨てた点を除いた窓でフィットし、
    予測位置との距離が ``max(floor_m, tolerance_s * 速度)`` を超えれば
    捨てる。捨てた点を窓から外して測り直すのを ``passes`` 回まで
    繰り返すので、数点続く飛びも順に剥がれる。
    """
    rejected: set[int] = set()
    for _ in range(passes):
        # Re-judge EVERY sample against windows that exclude the current
        # rejects, the earlier rejects included. In the first pass a glitch
        # drags the prediction for its clean neighbours off too, so they get
        # flagged with it; once the glitch is out of their windows they fit
        # again and come back. A reject whose window can no longer be fitted
        # at all stays out: nothing shows it was fine.
        found: set[int] = set()
        for index in range(len(times)):
            fit = fit_kinematics(
                times, positions, index, half_window_s, exclude=rejected | {index}
            )
            if fit is None:
                if index in rejected:
                    found.add(index)
                continue
            threshold = max(floor_m, tolerance_s * fit.speed)
            if math.dist(positions[index], fit.position) > threshold:
                found.add(index)
        if found == rejected:
            break
        rejected = found
    return rejected


@dataclass(frozen=True)
class KinematicsReport:
    """:func:`annotate_kinematics` の結果: 付けられた数と、捨てた数。"""

    annotated: int
    rejected: int

    def __int__(self) -> int:
        return self.annotated


def annotate_kinematics(
    analysis: "ApproachAnalysis", half_window_s: float = DEFAULT_HALF_WINDOW_S
) -> KinematicsReport:
    """全サンプルの ``load_factor`` / ``turn_rate_deg_s`` を軌跡から埋める。

    既に値が入っていても **必ず計算し直す**。保存済みの値はそのとき使った
    アルゴリズムの産物なので、再採点のたびに現行の導出で上書きするのが、
    同じ画面に新旧の混じった数字が並ぶことを防ぐ唯一の方法。
    (旋回半径や G の閾値をここで判定はしない --- 採点は pattern 側。)

    位置の飛び (:func:`reject_position_outliers`) は窓から外し、その
    サンプル自身の値は ``None`` のままにする。戻り値は付けられた数と
    捨てた数。
    """
    samples = sorted(analysis.samples, key=lambda s: s.time)
    times: list[float] = []
    positions: list[tuple[float, float, float]] = []
    owners: list[DeviationSample] = []
    previous: tuple[float, float, float] | None = None
    for sample in samples:
        sample.load_factor = None
        sample.turn_rate_deg_s = None
        position = _position_of(sample)
        if position is None:
            continue
        if previous is not None and position == previous and times and (
            sample.time - times[-1] < half_window_s
        ):
            # ACMI の部分更新は直前の位置をそのまま繰り返す。同じ場所に
            # 2 度いたことにするとフィットが「止まった」と読む。
            continue
        if times and sample.time <= times[-1]:
            continue
        times.append(sample.time)
        positions.append(position)
        owners.append(sample)
        previous = position

    rejected = reject_position_outliers(times, positions, half_window_s)
    count = 0
    for index, sample in enumerate(owners):
        if index in rejected:
            continue
        kinematics = fit_kinematics(times, positions, index, half_window_s, exclude=rejected)
        if kinematics is None:
            continue
        load_factor = kinematics.load_factor
        if load_factor is not None and load_factor <= MAX_PLAUSIBLE_LOAD_FACTOR:
            sample.load_factor = round(load_factor, 3)
            count += 1
        turn_rate = kinematics.turn_rate_deg_s
        if turn_rate is not None and abs(turn_rate) <= 90.0:
            sample.turn_rate_deg_s = round(turn_rate, 2)
    return KinematicsReport(annotated=count, rejected=len(rejected))


def bank_from_load_factor(load_factor: float) -> float | None:
    """水平定常旋回を仮定したときのバンク角 (度) = acos(1/n)。

    ACMI に Roll が無い記録のための **推定値**。上昇・降下中の旋回では
    実際のバンクとずれる。Roll がある記録では Roll を使うこと。
    """
    if load_factor < 1.0:
        return 0.0
    return math.degrees(math.acos(1.0 / load_factor))
