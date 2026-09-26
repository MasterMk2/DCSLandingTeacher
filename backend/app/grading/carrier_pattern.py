"""空母 Case I パターンの読み取り: キスオフからワイヤーまで。

LSO のグレードは「パス」(グルーブ〜接地) に付くもので、パターンは
デブリーフの材料である。ここはその材料を作る: **測定と講評だけで、
グレードには一切効かない** (このサーバの実トラップで較正した数字が
無いので、点数を付ける根拠が無い)。

何を測るか (高度はすべて MSL = 海面から。洋上の高度計が読む値で、
NATOPS / AIRBOSS の基準もこれで書かれている):

- **ブレイク (キスオフ)**: イニシャル (BRC 上、800 ft) から艦の前方で
  取る水平 180 度旋回。どこで割ったか (艦の基準点から前方何 nm)、何 G で
  回ったか、水平だったか。陸上のブレイク解析 (pattern.py) をそのまま使う。
- **ダウンウィンド / アビーム**: BRC と平行に 600 ft、艦から 1.1-1.3 nm。
  アビーム = ランプ (LSO プラットフォーム) の真横を通過した点。
- **90 / ウェイク (45)**: ファイナル旋回で、地面に対する向きが BRC と直角に
  なった点 (500 ft)、艦の航跡 (中心線の延長) を横切った点 (370 ft)。
- **グルーブ**: アングルドデッキの最終方位に乗ってから接地まで
  (CV NATOPS 15-18 秒、AIRBOSS は 15-19 秒を OK)。
- **接地の沈下**: 艦載機は硬く降りるのが前提 (フレアしない)。だから
  「硬い」ことは減点しない。逆に最後の 1 秒で沈下を止めた (フレアした)
  ことの方を指摘する --- 艦上ではそれがボルターの原因になる。

**座標系が 2 つ要る**のがこの問題の核心。グルーブはアングルドデッキ
(BRC から左舷へ 9.14 度) に沿って飛ぶが、パターンは艦の針路 (BRC) に
沿って飛ぶ。着艦区域の座標系のままダウンウィンドを測ると、正しく BRC と
平行に飛んだ脚が「9 度ずれている」と出る。そこで艦自身の座標系
(x = 艦首方向、y = 右舷) に移してから、陸上と同じパターン解析に掛ける。
2 つの座標系はどちらも甲板と一緒に動くので、変換は進入全体で一定の
剛体変換で済む (:class:`ShipFrame`)。

この変換は、甲板と一緒に動く座標系で記録された解析
(``geometry["frame"] == "moving_deck"``) にしか使えない。それ以前の
記録は接地時刻で艦を止めた座標系 (しかもアングルドデッキの向きが逆) で、
接地前 60 秒しか入っていないので、パターンは読まない。

キスオフは **イニシャルからのブレイク** にしか無い。ボルター・タッチ
アンドゴー・ウェーブオフの後は、アングルドデッキ沿いに上がってから
ダウンウィンドへ旋回する (:func:`_entry` が "turn" と判定し、講評は
キスオフと呼ばない)。ウェーブオフの後は甲板接触が無いので記録が前の
パスまで遡っており、最後の低空通過より後だけを測る (:func:`_last_low_pass`)。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.grading.deviations import (
    MOVING_DECK_FRAME,
    ApproachAnalysis,
    DeviationSample,
)
from app.grading.pattern import (
    MAX_SANE_ROLL_DEG,
    ApproachSegments,
    along_of,
    is_overhead_pattern,
    pattern_metrics,
    segment_approach,
    track_points,
)

M_PER_FT = 0.3048
M_PER_NM = 1852.0
MS_TO_FPM = 60.0 / 0.3048

#: 陸上のパターン解析が出すが、空母では意味を持たないキー。どれも
#: 「滑走路中心線に対して旋回明けがどこにあったか」で、艦の座標系では
#: 艦の中心線 (BRC) からの距離になる --- グルーブはそこから 9 度ずれた
#: アングルドデッキに乗るので、正しく飛んでも値が出てしまう。空母の
#: 旋回明けは :func:`_groove` がアングルドデッキに対して測る。
_LAND_ONLY_KEYS = (
    "rollout_offset_m",
    "overshoot_m",
    "alignment_error_m",
    "break_start_along_m",
)


@dataclass(frozen=True)
class ShipFrame:
    """着艦区域の座標系 -> 艦自身の座標系 (x = 艦首方向、y = 右舷)。

    原点は艦の ACMI 位置 (ニミッツ級ではほぼ艦の中央)。着艦区域の座標系の
    原点はグライドスロープの終点 (目標ワイヤー)、軸は最終方位
    (= 艦首方位 + ``offset_deg``)。
    """

    offset_deg: float
    end_x: float
    end_y: float
    ramp_x: float
    ramp_y: float
    #: 甲板からの高さ (``agl``) に足すと MSL になる量 = 艦の高度 + 甲板高。
    deck_msl_m: float

    @classmethod
    def from_geometry(cls, geometry: dict[str, Any] | None) -> "ShipFrame | None":
        if not geometry or geometry.get("frame") != MOVING_DECK_FRAME:
            return None
        try:
            offset = float(geometry["landing_course_offset_deg"])
            ramp_x = float(geometry["ramp_along_m"])
            ramp_y = float(geometry["ramp_lateral_m"])
            target = float(geometry.get("touchdown_target_m") or 0.0)
            deck = float(geometry["deck_altitude_m"]) + float(
                geometry.get("ship_altitude_m") or 0.0
            )
        except (KeyError, TypeError, ValueError):
            return None
        theta = math.radians(offset)
        return cls(
            offset_deg=offset,
            end_x=ramp_x + target * math.cos(theta),
            end_y=ramp_y + target * math.sin(theta),
            ramp_x=ramp_x,
            ramp_y=ramp_y,
            deck_msl_m=deck,
        )

    def to_ship(self, sample: DeviationSample) -> tuple[float, float] | None:
        """``(x 前方 +, y 右舷 +)`` [m]。横ずれが無いサンプルは ``None``。"""
        if sample.centerline_deviation is None:
            return None
        return self.point(along_of(sample), sample.centerline_deviation)

    def point(self, signed_distance_to_go: float, lateral: float) -> tuple[float, float]:
        """着艦区域の座標 (残距離、右 +) を艦の座標 (前方 +、右舷 +) へ。"""
        along = -signed_distance_to_go  # 着艦区域の座標系で、艦首側へ +
        theta = math.radians(self.offset_deg)
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        return (
            self.end_x + along * cos_t - lateral * sin_t,
            self.end_y + along * sin_t + lateral * cos_t,
        )


def ship_frame_view(analysis: ApproachAnalysis, frame: ShipFrame) -> ApproachAnalysis:
    """同じ進入を艦の座標系で見直した解析 (パターン解析への入力)。

    ``pattern.py`` は「滑走路の手前ほど ``signed_distance_to_go`` が大きい、
    中心線の右が ``centerline_deviation`` 正」という座標系を読む。そこへ
    x を「艦の基準点までの残距離」(艦尾側が正)、y を右舷として渡すと、
    イニシャル = BRC 方向、ダウンウィンド = BRC の逆方向として、陸上と
    全く同じ手続きで脚が切り出せる。高さは MSL にしておく
    (パターン高度の基準が MSL なので)。
    """
    samples: list[DeviationSample] = []
    for s in analysis.samples:
        position = frame.to_ship(s)
        if position is None:
            continue
        x, y = position
        samples.append(
            DeviationSample(
                time=s.time,
                distance_to_go=max(0.0, -x),
                glideslope_deviation=None,
                centerline_deviation=y,
                speed=s.speed,
                aoa=s.aoa,
                agl=(s.agl + frame.deck_msl_m) if s.agl is not None else None,
                signed_distance_to_go=-x,
                roll=s.roll,
                pitch=s.pitch,
                load_factor=s.load_factor,
                turn_rate_deg_s=s.turn_rate_deg_s,
            )
        )
    heading = (analysis.geometry or {}).get("ship_heading_deg")
    return ApproachAnalysis(
        kind=analysis.kind,
        outcome=analysis.outcome,
        glideslope_deg=analysis.glideslope_deg,
        course_deg=float(heading) if heading is not None else analysis.course_deg,
        touchdown_time=analysis.touchdown_time,
        touchdown_speed_ms=analysis.touchdown_speed_ms,
        touchdown_descent_rate_ms=analysis.touchdown_descent_rate_ms,
        samples=samples,
        approach_pattern=analysis.approach_pattern,
        airframe=analysis.airframe,
    )


@dataclass
class CarrierPattern:
    """:func:`analyze_carrier_pattern` の結果。"""

    #: ``pattern_`` を付けずに持つ (呼び出し側で付ける)。
    metrics: dict[str, Any]
    #: 講評に足す日本語の文 (句点なし)。良かった項目については何も言わない。
    comment_parts: list[str]
    #: "overhead" (Case I のダウンウィンドが取れた) / "straight_in" / "unknown"。
    approach_pattern: str


def analyze_carrier_pattern(
    analysis: ApproachAnalysis,
    segmentation: dict[str, Any],
    settings: dict[str, Any],
) -> CarrierPattern | None:
    """Case I パターンを測る。甲板追従の解析でなければ ``None``。

    ``segmentation`` は脚の切り出しの設定 (``land_grading`` と共通:
    トラック角の平滑化、ダウンウィンドの判定、ブレイクの範囲)、
    ``settings`` は ``lso_grading.pattern`` (基準値と講評のしきい値)。
    荷重倍数は呼び出し側が :func:`annotate_kinematics` で付けておくこと。
    """
    frame = ShipFrame.from_geometry(analysis.geometry)
    if frame is None:
        return None
    view = ship_frame_view(analysis, frame)
    # A wave-off leaves no deck contact to stop the capture at, so the record
    # holds the waved-off pass as well; measure the circuit that ended in
    # THIS touchdown, not the longest one in the recording.
    low_pass = _last_low_pass(view, frame)
    if low_pass is not None:
        view.samples = [s for s in view.samples if s.time >= low_pass]
    segments = segment_approach(view, segmentation)
    metrics = pattern_metrics(view, segments, segmentation)
    for key in _LAND_ONLY_KEYS:
        metrics.pop(key, None)
    metrics["low_pass_time"] = low_pass
    # The capture stops at the previous deck contact, so a record that opens
    # a few metres above the deck began at a bolter, a touch-and-go or a
    # launch: its first turn is onto the downwind, not a break.
    first = next((s for s in analysis.samples if s.agl is not None), None)
    metrics["starts_from_deck"] = bool(
        first is not None and first.agl < FROM_DECK_MAX_HEIGHT_M
    )

    if segments.break_leg:
        x, _ = _ship_xy(segments.break_leg[0])
        metrics["break_along_ship_m"] = round(x, 1)
    metrics["entry"] = _entry(segments, frame, segmentation)

    side = _pattern_side(view, segments)
    # Groove time is a Case I / II measure: the 15-19 s it is judged against
    # is the pass from the 180 to the wires. A Case III (or any long straight
    # final) has no 180, and "the last time it was 10 deg off" can be minutes
    # out -- a 2-minute "LIG" for an approach flown exactly as intended.
    groove = (
        _groove(analysis, segmentation, settings)
        if metrics.get("downwind_judged")
        else _groove_unmeasured()
    )
    metrics.update(groove)
    groove_start = groove.get("groove_start_time")
    # 陸上の「旋回明け」は空母ではグルーブ開始。平面図・プロファイル図が
    # 同じキーでファイナルの境目を引くので、ここで置き換える。
    metrics["rollout_time"] = groove_start
    metrics.update(
        _checkpoints(analysis, view, segments, frame, side, groove_start, segmentation)
    )
    metrics.update(_sink(analysis, groove_start))

    geometry = analysis.geometry or {}
    metrics["ship_speed_ms"] = geometry.get("ship_speed_ms")
    metrics["ship_heading_change_deg"] = geometry.get("ship_heading_change_deg")

    if is_overhead_pattern(metrics, segmentation):
        pattern = "overhead"
    elif groove_start is None and segments.rollout_time is None:
        pattern = "straight_in"
    else:
        pattern = "unknown"
    return CarrierPattern(
        metrics=metrics,
        comment_parts=_comment_parts(metrics, settings),
        approach_pattern=pattern,
    )


def _ship_xy(sample: DeviationSample) -> tuple[float, float]:
    """艦の座標系の解析サンプルから (x 前方 +, y 右舷 +) を読む。"""
    return -along_of(sample), sample.centerline_deviation or 0.0


#: 記録の最初のサンプルがこれより低ければ、甲板を離れた直後から始まった記録
#: (取り込みは前回の甲板接触で止まる: ボルター・タッチアンドゴー・発艦)。
FROM_DECK_MAX_HEIGHT_M = 10.0

#: ウェーブオフとみなす低空通過: ランプの前後位置を艦首方向へ横切ったとき、
#: 着艦区域の中心線付近 (艦の中心線から横 300 m 以内) を 500 ft 未満で
#: 飛んでいた。800 ft のイニシャルは艦の上を通っても高いので当たらない。
LOW_PASS_MAX_ALTITUDE_M = 150.0
LOW_PASS_MAX_OFFSET_M = 300.0
#: 最後のパス自身もランプを越えて接地するので、接地前のこの秒数は見ない。
LOW_PASS_GUARD_S = 10.0


def _last_low_pass(view: ApproachAnalysis, frame: ShipFrame) -> float | None:
    """接地より前に、接地せずランプの上を低く通り過ぎた最後の時刻。

    ウェーブオフ (や、接地しなかったパス) の後は甲板との接触が無いので、
    取り込みはそこで止まらず 300 秒前まで遡る。そのまま脚を探すと、記録の
    中でいちばん長いダウンウィンド --- 手前のパスのもの --- を拾い、
    「このトラップのパターン」として前のパスを講評してしまう。
    """
    latest: float | None = None
    samples = [
        s for s in view.samples if s.time < view.touchdown_time - LOW_PASS_GUARD_S
    ]
    for before, after in zip(samples, samples[1:]):
        x0, _ = _ship_xy(before)
        x1, y1 = _ship_xy(after)
        if (
            x0 < frame.ramp_x <= x1
            and abs(y1) <= LOW_PASS_MAX_OFFSET_M
            and after.agl is not None
            and after.agl < LOW_PASS_MAX_ALTITUDE_M
        ):
            latest = after.time
    return latest


#: キスオフ (イニシャルからのブレイク) とみなす条件: ブレイク開始前のこの
#: 秒数の間に、艦の後方 (ランプより後ろ)、中心線から 1 nm 以内を、BRC の
#: 向き (``initial_align_deg`` 以内) に 500 ft 以上で飛んでいたこと。ブレイクは
#: 艦の前方 4 nm までで取るので (CV NATOPS)、170 m/s なら 45 秒以内に収まる。
INITIAL_LOOKBACK_S = 60.0
INITIAL_MIN_ALTITUDE_M = 150.0
INITIAL_MAX_OFFSET_M = 1852.0


def _entry(
    segments: ApproachSegments, frame: ShipFrame, segmentation: dict[str, Any]
) -> str | None:
    """ダウンウィンドへどう入ったか: "initial" / "turn" / ``None`` (旋回なし)。

    "initial" だけが Case I のブレイク = キスオフ。ボルターやタッチアンド
    ゴーの後は、取り込みが甲板との接触から始まり、アングルドデッキ沿いに
    上昇してからダウンウィンドへ旋回する。ウェーブオフの後も同じ形になる。
    その旋回を「キスオフ」と呼び、600 ft の正しいパターン高度を 800 ft の
    イニシャル基準で指摘していた (レビューで再現)。
    """
    if not segments.break_leg:
        return None
    start = segments.break_leg[0].time
    align = float(segmentation.get("initial_align_deg", 20.0))
    for point in segments.track:
        if not start - INITIAL_LOOKBACK_S <= point.sample.time < start:
            continue
        x, y = _ship_xy(point.sample)
        if (
            abs(point.angle_deg) <= align
            and x < frame.ramp_x
            and abs(y) <= INITIAL_MAX_OFFSET_M
            and (point.sample.agl or 0.0) >= INITIAL_MIN_ALTITUDE_M
        ):
            return "initial"
    return "turn"


def _pattern_side(view: ApproachAnalysis, segments: ApproachSegments) -> float:
    """パターンを回った舷 (-1 = 左舷、+1 = 右舷)。Case I は左舷。"""
    lateral = [s.centerline_deviation for s in segments.downwind if s.centerline_deviation]
    if not lateral:
        lateral = [
            s.centerline_deviation
            for s in view.samples
            if s.centerline_deviation is not None and s.time < view.touchdown_time
        ]
    if not lateral:
        return -1.0
    extreme = max(lateral, key=abs)
    return 1.0 if extreme > 0 else -1.0


def _groove_unmeasured() -> dict[str, Any]:
    return {
        "groove_start_time": None,
        "groove_time_s": None,
        "groove_verdict": None,
        "groove_start_method": None,
        "groove_start_distance_m": None,
        "groove_start_lineup_m": None,
    }


def _groove(
    analysis: ApproachAnalysis,
    segmentation: dict[str, Any],
    settings: dict[str, Any],
) -> dict[str, Any]:
    """グルーブ: ファイナル旋回を終えて翼を水平にした時刻から接地まで。

    まず着艦区域の座標系 (軸 = 最終方位) で対地トラック角を取り、
    ``groove_align_deg`` を最後に外れていた時刻 = まだ旋回中だった最後の
    時刻を探す。グルーブはその後で **最初に翼が水平になった** 点から
    始める (記録の Roll が ``groove_wings_level_deg`` 以内。AIRBOSS も
    グルーブの時計を翼が水平になった時点で起動する)。

    トラック角だけで決めない理由: 角度は 2 秒の弦から取るので約 1 秒
    遅れ、しかも許容幅の分だけ旋回の途中で「乗った」ことになる。合成
    データでは実際のロールアウトより 2.3 秒早く始まり、16 秒のグルーブを
    18 秒と読んだ。Roll が無い記録だけそちらへ落ちる
    (``groove_start_method`` = "track")。

    一度も外れていない (旋回して入っていない = Case III の直線進入など)
    ときはグルーブ時間を出さない: その基準 (15-18 秒) は Case I の
    旋回から乗るパスのものだから。
    """
    out = _groove_unmeasured()
    align = float(settings.get("groove_align_deg", 10.0))
    wings_level = float(settings.get("groove_wings_level_deg", 5.0))
    track = track_points(
        analysis,
        float(segmentation.get("track_smoothing_s", 2.0)),
        float(segmentation.get("track_min_step_m", 20.0)),
    )
    last_off: int | None = None
    for index in range(len(track) - 1, -1, -1):
        if abs(track[index].angle_deg) > align:
            last_off = index
            break
    if last_off is None or last_off + 1 >= len(track):
        return out
    turning = track[last_off].sample.time
    rolls = [
        s
        for s in analysis.samples
        if turning <= s.time < analysis.touchdown_time
        and s.roll is not None
        and abs(s.roll) <= MAX_SANE_ROLL_DEG
    ]
    level = next((s for s in rolls if abs(s.roll) <= wings_level), None)
    if level is not None:
        start, method = level, "wings_level"
    else:
        start, method = track[last_off + 1].sample, "track"
    duration = analysis.touchdown_time - start.time
    ok_min, ok_max = (float(v) for v in settings.get("groove_time_ok_s", (15.0, 19.0)))
    out["groove_start_time"] = start.time
    out["groove_time_s"] = round(duration, 1)
    out["groove_verdict"] = (
        "NESA" if duration < ok_min else "LIG" if duration > ok_max else "OK"
    )
    out["groove_start_method"] = method
    out["groove_start_distance_m"] = round(start.distance_to_go, 1)
    if start.centerline_deviation is not None:
        out["groove_start_lineup_m"] = round(start.centerline_deviation, 1)
    return out


def _ground_track(
    analysis: ApproachAnalysis, frame: ShipFrame, segmentation: dict[str, Any]
) -> list[tuple[DeviationSample, float]]:
    """``(サンプル, 対地トラック角)``。角度は艦首方位 (BRC) 基準、右回り正。

    艦に対する相対トラックではなく、地面に対する向き。NATOPS の「90」は
    機首方位が BRC と直角になった点で、空気 (ここでは地面) に対する向き
    だから。15 m/s の艦に対する相対トラックが 90 度になる瞬間、機体は
    まだ BRC から 78 度しか回っていない (レビューで計算、合成データで
    高度 21 ft の差)。地面座標 ``fixed_*`` は接地時刻の着艦コースを軸に
    しているので、アングルドデッキの角度を足して BRC 基準に直す。
    """
    smoothing_s = float(segmentation.get("track_smoothing_s", 2.0))
    min_step_m = float(segmentation.get("track_min_step_m", 20.0))
    usable = [
        s
        for s in analysis.samples
        if s.time < analysis.touchdown_time
        and s.fixed_along is not None
        and s.fixed_lateral is not None
    ]
    out: list[tuple[DeviationSample, float]] = []
    head = 0
    for index in range(1, len(usable)):
        current = usable[index]
        while head + 1 < index and usable[head + 1].time <= current.time - smoothing_s:
            head += 1
        anchor = usable[head]
        d_along = current.fixed_along - anchor.fixed_along
        d_lateral = current.fixed_lateral - anchor.fixed_lateral
        if math.hypot(d_along, d_lateral) < min_step_m:
            continue
        angle = math.degrees(math.atan2(d_lateral, d_along)) + frame.offset_deg
        out.append((current, (angle + 180.0) % 360.0 - 180.0))
    return out


def _checkpoints(
    analysis: ApproachAnalysis,
    view: ApproachAnalysis,
    segments: ApproachSegments,
    frame: ShipFrame,
    side: float,
    groove_start: float | None,
    segmentation: dict[str, Any],
) -> dict[str, Any]:
    """アビーム・90・ウェイクの位置と高度 (艦の座標系、高度 MSL)。"""
    out: dict[str, Any] = {
        "abeam_time": None,
        "abeam_distance_m": None,
        "abeam_altitude_m": None,
        "ninety_time": None,
        "ninety_altitude_m": None,
        "wake_time": None,
        "wake_altitude_m": None,
    }
    end = groove_start if groove_start is not None else view.touchdown_time
    if segments.downwind:
        begin = segments.downwind[0].time
    elif segments.break_leg:
        begin = segments.break_leg[-1].time
    else:
        return out
    inbound = [
        s
        for s in view.samples
        if begin <= s.time <= end and s.centerline_deviation is not None
    ]

    # アビーム: パターン側の舷で、ランプの前後位置を艦尾方向へ横切った点。
    abeam = _crossing(
        inbound,
        lambda s: _ship_xy(s)[0] - frame.ramp_x,
        lambda s: side * _ship_xy(s)[1] > 0.0,
    )
    if abeam is not None:
        before, after, frac = abeam
        out["abeam_time"] = round(_lerp(before.time, after.time, frac), 2)
        out["abeam_distance_m"] = round(
            abs(_lerp(_ship_xy(before)[1], _ship_xy(after)[1], frac)), 1
        )
        altitude = _lerp_optional(before.agl, after.agl, frac)
        if altitude is not None:
            out["abeam_altitude_m"] = round(altitude, 1)
        begin = out["abeam_time"]

    # 90: ファイナル旋回で、地面に対する向きが BRC と直角になった点。
    turning = [
        (s, angle)
        for s, angle in _ground_track(analysis, frame, segmentation)
        if begin <= s.time <= end
    ]
    for (_, earlier), (point, angle) in zip(turning, turning[1:]):
        if abs(earlier) > 90.0 >= abs(angle):
            out["ninety_time"] = point.time
            if point.agl is not None:
                out["ninety_altitude_m"] = round(point.agl + frame.deck_msl_m, 1)
            begin = point.time
            break

    # ウェイク: 艦の中心線の延長 (y = 0) を、艦尾より後ろで横切った点。
    wake = _crossing(
        [s for s in inbound if s.time >= begin],
        lambda s: side * _ship_xy(s)[1],
        lambda s: _ship_xy(s)[0] < frame.ramp_x,
    )
    if wake is not None:
        before, after, frac = wake
        out["wake_time"] = round(_lerp(before.time, after.time, frac), 2)
        altitude = _lerp_optional(before.agl, after.agl, frac)
        if altitude is not None:
            out["wake_altitude_m"] = round(altitude, 1)
    return out


def _crossing(samples, value, where):
    """``value`` が正から 0 以下へ変わる最初の区間 ``(前, 後, 割合)``。

    ``where`` を満たすサンプルの組だけを見る。見つからなければ ``None``。
    """
    for before, after in zip(samples, samples[1:]):
        if not (where(before) and where(after)):
            continue
        v0, v1 = value(before), value(after)
        if v0 > 0.0 >= v1:
            frac = v0 / (v0 - v1) if v0 != v1 else 0.0
            return before, after, frac
    return None


def _lerp(a: float, b: float, frac: float) -> float:
    return a + (b - a) * frac


def _lerp_optional(a: float | None, b: float | None, frac: float) -> float | None:
    if a is None or b is None:
        return a if b is None else b
    return _lerp(a, b, frac)


def _sink_rate(samples: list[DeviationSample]) -> float | None:
    """高さの最小二乗の傾き (m/s、降下が正)。3 点未満なら ``None``。"""
    points = [(s.time, s.agl) for s in samples if s.agl is not None]
    if len(points) < 3:
        return None
    n = len(points)
    mean_t = sum(t for t, _ in points) / n
    mean_h = sum(h for _, h in points) / n
    var = sum((t - mean_t) ** 2 for t, _ in points)
    if var <= 0.0:
        return None
    slope = sum((t - mean_t) * (h - mean_h) for t, h in points) / var
    return -slope


#: 「ランプでの沈下」を測る窓 (接地前、秒)。5 Hz で 6 点ほど。
RAMP_SINK_WINDOW_S = 1.2
#: グルーブの沈下を測る窓の終わり (接地前、秒)。ここから先はランプの判定。
GROOVE_SINK_END_S = 2.0
#: グルーブ開始が取れなかったときの窓の始まり (接地前、秒)。
GROOVE_SINK_FALLBACK_S = 12.0


def _sink(analysis: ApproachAnalysis, groove_start: float | None) -> dict[str, Any]:
    """接地の沈下。艦載機は硬く降りるのが前提なので、見るのはフレアの方。

    ``touchdown_descent_rate_fpm`` は陸上と同じ定義 (接触前 3 秒の平均)。
    グルーブの沈下と最後の 1.2 秒の沈下を別に測り、その比が小さければ
    ランプで沈下を止めた = フレアした、と読む。
    """
    td = analysis.touchdown_time
    start = groove_start if groove_start is not None else td - GROOVE_SINK_FALLBACK_S
    groove = _sink_rate(
        [s for s in analysis.samples if start <= s.time <= td - GROOVE_SINK_END_S]
    )
    ramp = _sink_rate(
        [s for s in analysis.samples if td - RAMP_SINK_WINDOW_S <= s.time <= td]
    )
    ratio = ramp / groove if ramp is not None and groove and groove > 0.5 else None
    return {
        "touchdown_descent_rate_fpm": round(
            analysis.touchdown_descent_rate_ms * MS_TO_FPM, 1
        ),
        "groove_descent_rate_fpm": (
            round(groove * MS_TO_FPM, 1) if groove is not None else None
        ),
        "ramp_descent_rate_fpm": round(ramp * MS_TO_FPM, 1) if ramp is not None else None,
        "ramp_sink_ratio": round(ratio, 2) if ratio is not None else None,
    }


def _ft(meters: float) -> str:
    return f"{meters / M_PER_FT:,.0f} ft"


def _nm(meters: float) -> str:
    return f"{meters / M_PER_NM:.2f} nm"


def _comment_parts(metrics: dict[str, Any], settings: dict[str, Any]) -> list[str]:
    """講評の文。ブレイクと接地は毎回事実を述べ、他は外れたときだけ言う。"""
    parts: list[str] = []
    tolerance_m = float(settings.get("altitude_tolerance_ft", 100.0)) * M_PER_FT

    if metrics.get("low_pass_time") is not None:
        parts.append("ウェーブオフ後の周回（記録の途中で接地せずにランプの上を通過）")
    elif metrics.get("starts_from_deck"):
        parts.append("甲板を離れてからの周回（ボルター・タッチアンドゴー・発艦の後）")
    kissoff = metrics.get("entry") == "initial"
    if metrics.get("break_duration_s") is not None:
        clauses: list[str] = []
        where = metrics.get("break_along_ship_m")
        if where is not None:
            side = "前方" if where >= 0 else "後方"
            clauses.append(f"艦の{side} {_nm(abs(where))} で開始")
        peak = metrics.get("break_max_load_factor")
        if peak is not None:
            detail = []
            sustained = metrics.get("break_sustained_load_factor")
            std = metrics.get("break_load_factor_std")
            if sustained is not None and std is not None:
                detail.append(f"定常 {sustained:.1f} G ± {std:.2f}")
            bank = metrics.get("break_max_bank_deg")
            if bank is not None:
                detail.append(f"バンク最大 {bank:.0f}°")
            suffix = f"（{'、'.join(detail)}）" if detail else ""
            clauses.append(f"最大 {peak:.1f} G{suffix}")
        # ボルター・タッチアンドゴー・ウェーブオフの後は、イニシャルを
        # 飛ばずに上昇からダウンウィンドへ旋回する。それはキスオフではなく、
        # 800 ft の基準も水平旋回の決まりも当てはまらない。
        subject = "ブレイク（キスオフ）" if kissoff else "ダウンウィンドへの旋回"
        if clauses:
            parts.append(f"{subject}は" + "し".join(clauses))
        entry = metrics.get("break_entry_agl_m")
        reference = float(settings.get("initial_altitude_ft", 800.0)) * M_PER_FT
        if kissoff and entry is not None and abs(entry - reference) > tolerance_m:
            parts.append(f"ブレイク進入高度 {_ft(entry)}（基準 {_ft(reference)}）")
        spread = metrics.get("break_altitude_spread_m")
        level = float(settings.get("break_altitude_spread_m", 45.0))
        if kissoff and metrics.get("break_judged") and spread is not None and spread > level:
            parts.append(f"ブレイク中に高度が {_ft(spread)} 動いた（水平旋回が基本）")
    elif metrics.get("downwind_judged"):
        # ダウンウィンドはあるのにブレイクが無い = 記録がブレイクの途中から
        # 始まっている。直線進入 (Case III) ではそもそも無いので黙る。
        parts.append("ダウンウィンドより前のブレイク（キスオフ）は記録から切り出せなかった")

    distance = metrics.get("abeam_distance_m")
    low, high = (float(v) * M_PER_NM for v in settings.get("abeam_distance_nm", (1.0, 1.3)))
    if distance is not None and not low <= distance <= high:
        word = "広い" if distance > high else "近い"
        parts.append(
            f"アビームが{word}（{_nm(distance)}、基準 {_nm(low)}〜{_nm(high)}）"
        )
    for key, label, ref_key, default_ft in (
        ("abeam_altitude_m", "アビーム高度", "abeam_altitude_ft", 600.0),
        ("ninety_altitude_m", "90 の高度", "ninety_altitude_ft", 500.0),
        ("wake_altitude_m", "ウェイク通過高度", "wake_altitude_ft", 370.0),
    ):
        value = metrics.get(key)
        reference = float(settings.get(ref_key, default_ft)) * M_PER_FT
        if value is not None and abs(value - reference) > tolerance_m:
            parts.append(f"{label} {_ft(value)}（基準 {_ft(reference)}）")

    groove = metrics.get("groove_time_s")
    if groove is not None:
        ok_min, ok_max = (float(v) for v in settings.get("groove_time_ok_s", (15.0, 19.0)))
        verdict = metrics.get("groove_verdict")
        note = (
            ""
            if verdict == "OK"
            else f"（{verdict}: 基準 {ok_min:.0f}〜{ok_max:.0f} 秒）"
        )
        parts.append(f"グルーブ {groove:.1f} 秒{note}")

    touchdown = metrics.get("touchdown_descent_rate_fpm")
    ratio = metrics.get("ramp_sink_ratio")
    hard = float(settings.get("hard_touchdown_fpm", 1400.0))
    flare = float(settings.get("flare_sink_ratio", 0.5))
    if touchdown is not None:
        if touchdown > hard:
            parts.append(
                f"接地 {touchdown:.0f} fpm は艦載機の脚の設計値（約 {hard:,.0f} fpm）を超える硬さ"
            )
        elif ratio is not None and ratio < flare:
            ramp = metrics.get("ramp_descent_rate_fpm")
            groove_rate = metrics.get("groove_descent_rate_fpm")
            parts.append(
                f"ランプで沈下を止めた（最後の 1 秒 {ramp:.0f} fpm、グルーブ中 "
                f"{groove_rate:.0f} fpm。艦上ではフレアせずワイヤーへ降ろす）"
            )
        else:
            parts.append(f"接地 {touchdown:.0f} fpm（フレアなしの接地で適正）")

    turned = metrics.get("ship_heading_change_deg")
    if turned is not None and abs(turned) > 10.0:
        parts.append(f"艦がこの間に {abs(turned):.0f}° 旋回していた")
    return parts
