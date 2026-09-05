"""進入パターンの区間分割と、オーバーヘッドパターン固有の評価。

このモジュールが解く問題は 1 つ:
**「どこからがファイナルか」を知らずに着陸を採点すると、旋回中の姿を
ファイナルの姿として採点してしまう。**

DCS で戦闘機が実際に飛ぶのはオーバーヘッドパターン (イニシャル →
ブレイク → ダウンウィンド → ベースターン → ファイナル) で、接地の
30 秒前はまだベースターン中であることが普通にある。旋回中は当然
コースから外れ、降下角も一定でないので、そこを含む窓でグライドスロープを
直線フィットすれば「理想より 2 度以上高い」という結果が出る --- 実際に
飛んだファイナルが教科書どおりの 3.0 度であっても。

そこで区間分割は **対地トラック** で行う。滑走路座標系での位置差分から
進行方向を出し、コースに整列した最後の時刻をロールアウトとみなす。
機体ヘディングではなく対地トラックを使うのは、

- 横風でクラブを当てていればヘディングとコースは食い違うが、パターンの
  幾何として意味があるのは実際に動いた向き (対地トラック) の方であり、
- ``DeviationSample`` には既に距離・横ずれが入っているので、**過去に
  記録済みの着陸に遡って適用できる** (ヘディングは記録していない)

ため。座標系は ``ApproachAnalysis.course_deg`` と同一なので、投影による
ずれも入らない。

角度の規約 (すべて滑走路コース基準):

- ``0``    : コース方向に真っ直ぐ進んでいる = ファイナル
- ``+-180``: コースの逆方向 = ダウンウィンド
- 符号     : 右にずれていく動きが正
"""

from __future__ import annotations

import math

from dataclasses import dataclass, field
from typing import Any

from app.grading.deviations import ApproachAnalysis, DeviationSample


@dataclass
class TrackPoint:
    """1 サンプルと、そこでの対地トラックのコース基準角度。"""

    sample: DeviationSample
    angle_deg: float


@dataclass
class ApproachSegments:
    """進入を「ダウンウィンド / 旋回 / ファイナル」に切り分けた結果。"""

    #: ファイナルへのロールアウト時刻。``None`` は「一度も整列を外れて
    #: いない」= ストレートインの意。パターンの評価 (旋回明けの軸ずれ) は
    #: この時刻を見る。
    rollout_time: float | None = None
    #: 安定化ゲート (規定 AGL) を最後に切って降りた時刻。長いストレート
    #: インで評価区間を伸ばすための起点。
    gate_time: float | None = None
    #: 採点上のファイナル開始時刻 = 上 2 つの **遅い方**。どちらも取れ
    #: なければ ``None`` で、呼び出し側は固定窓にフォールバックする。
    final_start_time: float | None = None
    #: ``final_start_time`` 以降のサンプル。
    final: list[DeviationSample] = field(default_factory=list)
    #: ダウンウィンドと判定した連続区間 (見つからなければ空)。
    downwind: list[DeviationSample] = field(default_factory=list)
    #: ブレイク = イニシャル (滑走路方向) からダウンウィンドへ入る旋回。
    #: 教科書どおりなら**水平**旋回なので、高度が動いていれば操縦の粗さが
    #: そのまま出る。記録に入っていなければ空。
    break_leg: list[DeviationSample] = field(default_factory=list)
    #: 分割に使った対地トラック角の系列 (デバッグ・再利用用)。
    track: list[TrackPoint] = field(default_factory=list)

    @property
    def has_final_cut(self) -> bool:
        """ファイナル区間を実際に切り出せたか。"""
        return self.final_start_time is not None and len(self.final) >= 3


def track_points(
    analysis: ApproachAnalysis, smoothing_s: float, min_step_m: float
) -> list[TrackPoint]:
    """各サンプルでの対地トラック角 (コース基準、度)。

    差分は ``smoothing_s`` 秒だけ遡ったサンプルとの間で取る。1 サンプル
    間隔 (実測 0.25 秒前後) の差分は位置量子化ノイズに埋もれて数十度
    振れるため、区間判定には使えない。``min_step_m`` 未満しか動いて
    いない場合は角度が定義できないものとして捨てる (ホバリング・地上滑走)。

    位置は :func:`along_of` から取る。クランプ済みの ``distance_to_go`` を
    使うと **接地点より先が全部 0 に潰れる** --- そこはまさにブレイク旋回が
    起きている場所で、角度系列からその区間が丸ごと消えていた
    (landing #54 では t-105 〜 t-74 の 31 秒が空白になり、ブレイクが 2 秒
    しか検出できなかった)。
    """
    usable = [
        s
        for s in analysis.samples
        if s.time < analysis.touchdown_time and s.centerline_deviation is not None
    ]
    points: list[TrackPoint] = []
    head = 0
    for index in range(1, len(usable)):
        current = usable[index]
        # smoothing_s 秒以上前にある最も新しいサンプルまで head を進める。
        while head + 1 < index and usable[head + 1].time <= current.time - smoothing_s:
            head += 1
        anchor = usable[head]
        if anchor is current:
            continue
        # along 軸は「進入方向が正」。along_of は残距離なので符号反転。
        d_along = -(along_of(current) - along_of(anchor))
        d_lateral = (current.centerline_deviation or 0.0) - (
            anchor.centerline_deviation or 0.0
        )
        if math.hypot(d_along, d_lateral) < min_step_m:
            continue
        points.append(TrackPoint(current, math.degrees(math.atan2(d_lateral, d_along))))
    return points


def segment_approach(
    analysis: ApproachAnalysis, settings: dict[str, Any]
) -> ApproachSegments:
    """進入をダウンウィンド / ファイナルに切り分ける。"""
    smoothing_s = float(settings.get("track_smoothing_s", 2.0))
    min_step_m = float(settings.get("track_min_step_m", 20.0))
    align_deg = float(settings.get("rollout_align_deg", 15.0))
    initial_align_deg = float(settings.get("initial_align_deg", 20.0))
    downwind_cone_deg = float(settings.get("downwind_cone_deg", 60.0))
    downwind_max_turn_rate = float(
        settings.get("downwind_max_turn_rate_deg_s", 1.5)
    )
    gate_agl_m = float(settings.get("stabilization_gate_agl_m", 305.0))

    inbound = [s for s in analysis.samples if s.time < analysis.touchdown_time]
    track = track_points(analysis, smoothing_s, min_step_m)
    segments = ApproachSegments(track=track)
    segments.gate_time = _gate_time(inbound, gate_agl_m)
    if not track:
        segments.final_start_time = segments.gate_time
        segments.final = _from(inbound, segments.final_start_time)
        return segments

    # --- ロールアウト: 整列を外れていた最後の時刻 -------------------------
    rollout_index: int | None = None
    for index in range(len(track) - 1, -1, -1):
        if abs(track[index].angle_deg) > align_deg:
            rollout_index = index
            break

    if rollout_index is None:
        # 一度も外れていない = ストレートイン。旋回で切る理由はないので、
        # 起点は安定化ゲートだけで決まる。
        segments.final_start_time = segments.gate_time
        segments.final = _from(inbound, segments.final_start_time)
        return segments

    rollout_time = track[rollout_index].sample.time
    segments.rollout_time = rollout_time
    segments.final_start_time = (
        rollout_time
        if segments.gate_time is None
        else max(rollout_time, segments.gate_time)
    )
    segments.final = _from(inbound, segments.final_start_time)

    run = _downwind_leg(
        track[: rollout_index + 1], downwind_cone_deg, downwind_max_turn_rate
    )
    segments.downwind = [point.sample for point in run]
    if run:
        start = track.index(run[0])
        segments.break_leg = _break_leg(track[:start], initial_align_deg)
    return segments


def _break_leg(
    before_downwind: list[TrackPoint], initial_align_deg: float
) -> list[DeviationSample]:
    """The turn from the initial onto the downwind.

    Walk back from the downwind while the ground track is NOT pointing down
    the landing direction; the moment it is, the initial has been reached
    and the break is everything after it. Defining it by "still turning"
    rather than by a heading band means it works whichever way the pattern
    is flown and however wide the break is.
    """
    leg: list[TrackPoint] = []
    for point in reversed(before_downwind):
        if abs(point.angle_deg) <= initial_align_deg:
            break
        leg.append(point)
    return [point.sample for point in reversed(leg)]


def _wrap180(degrees: float) -> float:
    return (degrees + 180.0) % 360.0 - 180.0


def _downwind_leg(
    track: list[TrackPoint], cone_deg: float, max_turn_rate: float
) -> list[TrackPoint]:
    """ダウンウィンド脚 = 「旋回していない」最長区間。

    脚の**終わり**を「逆方位にどれだけ近いか」で決めてはいけない。それは
    まさに測ろうとしている量なので、判定に使うと自己言及になる:
    ずれの大きいダウンウィンドほど許容帯の残りが少なく、ベースターンの
    入りが脚に混ざり込む。混ざればフィットは旋回側へ引っ張られ、測定値は
    実際より「合っている」方向へ寄る --- **ずれが大きいほど過小評価する**
    という最悪の性質になる。

    landing #27 の実測: 真のダウンウィンドは 9.2 秒間ずっと 168.2-168.9°
    (逆方位から 11.6° ずれ、旋回率 0.1°/s) だったが、+-25° の帯で切ると
    旋回開始後の 6 秒 (旋回率 1.3 → 12°/s) まで脚に入り、直線フィットは
    6.4° を返していた。軌跡ビューに引いた直線が目に見えて track から
    外れていたのはこれ。

    そこで 2 段階にする:

    - ``cone_deg``: 逆方位からこの範囲内 = 「パターンのダウンウィンド側に
      いる」。ファイナルやアップウィンドを除くための粗いゲートで、
      **測定精度には効かせない**ので広く取る。
    - ``max_turn_rate``: トラック角の変化率がこれ以下 = 「旋回していない」。
      脚の端はこちらで決まる。

    条件を満たす連続区間のうち **最も長い時間** のものを脚とする。
    """
    cone_min = 180.0 - cone_deg
    runs: list[list[TrackPoint]] = []
    current: list[TrackPoint] = []
    for point in track:
        if abs(point.angle_deg) < cone_min:
            if current:
                runs.append(current)
                current = []
            continue
        if current:
            previous = current[-1]
            dt = point.sample.time - previous.sample.time
            rate = (
                abs(_wrap180(point.angle_deg - previous.angle_deg)) / dt
                if dt > 0
                else float("inf")
            )
            if rate > max_turn_rate:
                runs.append(current)
                current = [point]
                continue
        current.append(point)
    if current:
        runs.append(current)
    if not runs:
        return []
    # 同じ長さなら後 (ロールアウトに近い方) を採る: パターンを 2 周した
    # 記録では、着陸につながった最後の脚が知りたい。
    return max(
        runs,
        key=lambda run: (run[-1].sample.time - run[0].sample.time, run[-1].sample.time),
    )


def _gate_time(inbound: list[DeviationSample], gate_agl_m: float) -> float | None:
    """安定化ゲートを最後に切って降りた時刻 (規定 AGL 以下に入った瞬間)。

    ファイナルがどこから始まるかは、旋回だけでは決まらない。長い
    ストレートインは旋回を伴わないので、ロールアウトだけを起点にすると
    「窓が無い = 捕捉区間まるごと」か「固定 30 秒」の二択になり、前者は
    巡航からの降下や水平区間まで混ざり、後者は 3nm 飛んだ安定進入を
    最後の 1km だけで採点することになる。

    高度でゲートを切るのは実機の stabilized approach criteria と同じ
    考え方で、**どう飛んだかに依存しない**のが要点。基準スロープからの
    ずれ幅で遡る方式にすると「オンスロープだったサンプルだけ選んで
    オンスロープ度を採点する」自己成就になり、5 度で 2nm 降りて最後の
    1nm だけ 3 度に乗せた進入が満点になる。

    「最後に切った」時刻を取るので、ゲート付近で水平飛行してから降りた
    場合はその水平区間が窓に入る --- それは実際に不安定な進入であり、
    採点対象に入るのが正しい。ゲートまで上がらなかった進入 (低いパターン、
    ヘリの低速進入) では ``None`` を返す。
    """
    latest: float | None = None
    for sample in inbound:
        if sample.agl is not None and sample.agl > gate_agl_m:
            latest = sample.time
    return latest


def _from(
    inbound: list[DeviationSample], start: float | None
) -> list[DeviationSample]:
    if start is None:
        return []
    return [s for s in inbound if s.time >= start]


def along_of(sample: DeviationSample) -> float:
    """滑走路軸上の位置 (残距離)。基準点より先では負。

    ``distance_to_go`` は 0 でクランプされているので、ブレイク〜アップ
    ウィンドが全部同じ値になる。符号付きを持っている記録ではそちらを使う。
    """
    if sample.signed_distance_to_go is not None:
        return sample.signed_distance_to_go
    return sample.distance_to_go


def downwind_course_fit(
    downwind: list[DeviationSample], min_span_m: float = 200.0
) -> tuple[float, float] | None:
    """ダウンウィンド脚に直線を当てて (方位差 [deg], 残差 RMS [m]) を返す。

    「ダウンウィンドの方位が合っているか」は、脚全体を 1 本の直線と見た
    ときの滑走路軸との角度差そのもの --- パイロットがヘディングを何度
    振れば直るか、という量。サンプルごとの進行方向を平均する作りだと、
    ふらつきと系統的なずれが同じ数字に混ざってしまい、しかも絶対値を
    平均するので「どちら向きにずれていたか」が消える。

    残差 RMS はメートルのまま返す (採点には使わない)。角度に換算する
    自然な基準長が無いので、換算すると意味の薄い数字になる。
    """
    points = [
        (along_of(s), s.centerline_deviation)
        for s in downwind
        if s.centerline_deviation is not None
    ]
    if len(points) < 3:
        return None
    xs = [x for x, _ in points]
    if max(xs) - min(xs) < min_span_m:
        return None
    count = len(points)
    sum_x = sum(xs)
    sum_y = sum(y for _, y in points)
    sum_xx = sum(x * x for x in xs)
    sum_xy = sum(x * y for x, y in points)
    denominator = count * sum_xx - sum_x * sum_x
    if abs(denominator) < 1e-9:
        return None
    slope = (count * sum_xy - sum_x * sum_y) / denominator
    intercept = (sum_y - slope * sum_x) / count
    residuals = [y - (slope * x + intercept) for x, y in points]
    rms = math.sqrt(sum(r * r for r in residuals) / count)
    return math.degrees(math.atan(slope)), rms


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def approach_side(analysis: ApproachAnalysis) -> float | None:
    """パターンを回った側 (+1 = 滑走路の右、-1 = 左)。

    ダウンウィンドを回った側が分かると、ロールアウト時の横ずれを
    「まだ届いていない (アンダーシュート)」と「通り越した
    (オーバーシュート)」に符号で区別できる。
    """
    values = [
        s.centerline_deviation
        for s in analysis.samples
        if s.centerline_deviation is not None and s.time < analysis.touchdown_time
    ]
    if not values:
        return None
    extreme = max(values, key=abs)
    if abs(extreme) < 1.0:
        return None
    return 1.0 if extreme > 0 else -1.0


def mark_judged(metrics: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """Fill in ``break_judged`` / ``downwind_judged`` on pattern metrics.

    Which legs are long enough to mean anything is asked in two places --
    when deciding whether this was an overhead pattern at all, and when
    scoring one -- so the rule lives here rather than in either caller. A
    leg too short to read a heading off is reported (so it is visible why)
    but never judged: the mean of 2 seconds of a turn is not a downwind
    course.
    """
    bands = settings.get("pattern", {}) or {}
    break_duration = metrics.get("break_duration_s")
    metrics["break_judged"] = bool(
        break_duration is not None
        and break_duration >= float(bands.get("min_break_s", 6.0))
        and metrics.get("break_altitude_spread_m") is not None
    )
    downwind_duration = metrics.get("downwind_duration_s")
    metrics["downwind_judged"] = bool(
        downwind_duration is not None
        and downwind_duration >= float(bands.get("min_downwind_s", 5.0))
    )
    return metrics


def is_overhead_pattern(metrics: dict[str, Any], settings: dict[str, Any]) -> bool:
    """Did the recording actually hold an overhead pattern?

    An overhead is initial -> break -> **downwind** -> base -> final, and
    the downwind is the leg that makes it one: it is where the pattern is
    set up, and it is what a curving straight-in join does not have. So the
    downwind leg being present in the track IS the test.

    The alternative -- believing the detector's heading-rate heuristic --
    labels every sweeping turn onto a long final, and every helicopter
    arrival, an overhead pattern. See ``pattern.require_downwind`` in
    ``config/grading.yaml`` for the measured effect of that on this
    server's landings.
    """
    bands = settings.get("pattern", {}) or {}
    if not bands.get("require_downwind", True):
        return True
    return bool(metrics.get("downwind_judged"))


def pattern_metrics(
    analysis: ApproachAnalysis,
    segments: ApproachSegments,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """オーバーヘッドパターンの幾何を数値化する (採点は別関数)。

    ``settings`` を渡すと脚の採否 (``break_judged`` / ``downwind_judged``)
    をその閾値で決める。省略時は :data:`app.grading.config` の既定値。
    """
    metrics: dict[str, Any] = {
        "rollout_offset_m": None,
        "overshoot_m": None,
        "alignment_error_m": None,
        # 採点対象: 脚全体を直線と見たときの滑走路軸との角度差の大きさ。
        "downwind_course_error_deg": None,
        # 符号付き。軌跡ビューが引く直線の傾きはこれ。
        "downwind_course_offset_deg": None,
        # 直線からのばらつき (m)。採点には使わないが、方位差ゼロでも
        # S 字に振れていた脚はここに出る。
        "downwind_course_rms_m": None,
        "downwind_altitude_spread_m": None,
        "downwind_abeam_m": None,
        "downwind_samples": len(segments.downwind),
        "downwind_duration_s": None,
        # ブレイクは教科書どおりなら水平旋回。高度が動いていれば粗さが出る。
        "break_altitude_spread_m": None,
        "break_duration_s": None,
        "break_samples": len(segments.break_leg),
        "break_start_time": None,
        "break_end_time": None,
        # 軌跡ビューが脚を色分けするための時刻 (ミッション時間、秒)。
        "rollout_time": segments.rollout_time,
        "downwind_start_time": None,
        "downwind_end_time": None,
    }
    side = approach_side(analysis)

    # --- ロールアウト時の横ずれ / センターライン突き抜け -------------------
    # ここは ``final`` の先頭ではなくロールアウト時刻のサンプルを見る:
    # ファイナルの起点は安定化ゲートで前に伸びることがあり、そのときの
    # 先頭は旋回明けではなく「1000ft を切った瞬間」を指す。
    if segments.rollout_time is not None:
        rollout_lateral = next(
            (
                s.centerline_deviation
                for s in analysis.samples
                if s.time >= segments.rollout_time
                and s.centerline_deviation is not None
            ),
            None,
        )
        if rollout_lateral is not None and side is not None:
            # 正 = パターンを回った側にまだ残っている (アンダーシュート)、
            # 負 = 反対側へ抜けた (オーバーシュート)。
            metrics["rollout_offset_m"] = round(side * rollout_lateral, 2)

    laterals = [
        s.centerline_deviation
        for s in analysis.samples
        if s.centerline_deviation is not None and s.time < analysis.touchdown_time
    ]
    if laterals and side is not None:
        # 反対側へ最も深く入った量。ロールアウト後に膨らんだ分も拾う。
        metrics["overshoot_m"] = round(max(0.0, max(-side * v for v in laterals)), 2)

    rollout_offset = metrics["rollout_offset_m"]
    overshoot = metrics["overshoot_m"]
    if rollout_offset is not None or overshoot is not None:
        # 「旋回明けにどれだけ軸から外れていたか」。アンダーシュートは
        # ロールアウト時の残り、オーバーシュートは突き抜けた深さで、
        # 悪い方を代表値にする。
        metrics["alignment_error_m"] = round(
            max(abs(rollout_offset or 0.0), overshoot or 0.0), 2
        )

    # --- ダウンウィンド --------------------------------------------------
    if segments.downwind:
        times = [s.time for s in segments.downwind]
        metrics["downwind_duration_s"] = round(times[-1] - times[0], 1)
        metrics["downwind_start_time"] = times[0]
        metrics["downwind_end_time"] = times[-1]
        agls = [s.agl for s in segments.downwind if s.agl is not None]
        if len(agls) >= 2:
            metrics["downwind_altitude_spread_m"] = round(max(agls) - min(agls), 2)
        abeams = [
            abs(s.centerline_deviation)
            for s in segments.downwind
            if s.centerline_deviation is not None
        ]
        mean_abeam = _mean(abeams)
        if mean_abeam is not None:
            metrics["downwind_abeam_m"] = round(mean_abeam, 1)

        fit = downwind_course_fit(segments.downwind)
        if fit is not None:
            offset_deg, rms_m = fit
            metrics["downwind_course_offset_deg"] = round(offset_deg, 2)
            metrics["downwind_course_error_deg"] = round(abs(offset_deg), 2)
            metrics["downwind_course_rms_m"] = round(rms_m, 1)

    if segments.break_leg:
        times = [s.time for s in segments.break_leg]
        metrics["break_duration_s"] = round(times[-1] - times[0], 1)
        metrics["break_start_time"] = times[0]
        metrics["break_end_time"] = times[-1]
        agls = [s.agl for s in segments.break_leg if s.agl is not None]
        if len(agls) >= 2:
            metrics["break_altitude_spread_m"] = round(max(agls) - min(agls), 2)
    return mark_judged(metrics, settings or {})


def effective_approach_pattern(
    analysis: ApproachAnalysis,
    settings: dict[str, Any],
    metrics: dict[str, Any] | None = None,
    segments: ApproachSegments | None = None,
) -> str:
    """進入パターンを **軌跡から** 決める ("overhead" / "straight_in" / "unknown")。

    検出器 (``detection.detector._classify_approach_pattern``) のラベルは
    取り込み時にヘディング変化率だけで付けた見込み値なので、ここでは
    ヒントとしてしか使わない。実際に飛んだ幾何を持っているのは採点側で、

    - ダウンウィンド脚が取れていれば **オーバーヘッド**
    - 一度もコースを外れていなければ (ロールアウトが無い) **ストレートイン**
    - どちらでもない (旋回して入ったがパターンは無い = 長いファイナルへの
      旋回進入など) 場合は、検出器がストレートインと言っていればそれを、
      でなければ **unknown**

    と読む。これは表示ラベルであると同時に、オーバーヘッド用の重み配分と
    pattern コンポーネントを出すかどうかの判断そのものでもある。
    """
    if segments is None:
        segments = segment_approach(analysis, settings)
    if metrics is None:
        metrics = pattern_metrics(analysis, segments, settings)
    mark_judged(metrics, settings)
    if is_overhead_pattern(metrics, settings):
        return "overhead"
    if segments.rollout_time is None:
        return "straight_in"
    hint = analysis.approach_pattern
    return "straight_in" if hint == "straight_in" else "unknown"
