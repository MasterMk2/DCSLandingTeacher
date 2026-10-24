"""迎角 (AOA) を軌跡と姿勢から推定し、機体別のオンスピード帯で判定する。

DCS のサーバーから Tacview へ流れるリアルタイムの記録には ``AOA`` が無い
(位置・姿勢・識別情報だけ。本番の着陸から機種別に抜いた 650 件で AOA の
入ったサンプルは 0)。クライアント側の録画には自機の分だけ入っているが、採点の対象になる
記録はほぼ全部サーバー側なので、AOA は **導くしかない**。

迎角は「機体の前後軸」と「機体に当たる空気の流れ (= 対気速度ベクトル)」の
なす角で、両方とも記録から出せる:

- 機体の向き: ACMI の ``Yaw`` / ``Pitch`` / ``Roll`` (姿勢)
- 速度ベクトル: 位置の微分 (:mod:`app.grading.kinematics` の局所 2 次
  フィット。外れ値除去も同じものを通す)

速度ベクトルを機体軸に回して ``atan2(下向き成分, 前向き成分)`` を取れば
機体軸の迎角になる。MOOSE AIRBOSS が ``POSITIONABLE:GetAoA`` で計算して
いるのも同じ量 (機体の x 軸と、機体の x-y 面に射影した対気速度との角度)
で、AIRBOSS の機体別オンスピード帯はこの量に対して書かれている。

**風は引けない。** ACMI に風のデータは無い (``BURBLE`` を無効にしてあるのと
同じ理由) ので、対地速度を対気速度の代わりに使う。向かい風 W・対気速度 V・
降下率 w のとき、対地の経路角は対気の経路角より ``w * W / V^2`` ラジアン
ほど深く出て、そのぶん迎角が大きく出る: 140 kt・向かい風 20 kt・3 度の
降下で約 0.4 度。オンスピード帯の半幅 (Hornet で 0.7 度) より狭い差ではない
ので、帯の外側 (Fast / Slow の手前) までは「やや」として扱い、強く言うのは
その外 (FAST / SLOW) だけにしている。
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.grading.deviations import DeviationSample

#: これより速度が小さいと迎角が定義できない (ホバリング・接地滑走)。
MIN_SPEED_MS = 30.0

#: 姿勢がこれを超えて傾いていたら推定しない。式自体はどの姿勢でも正しいが、
#: 旋回中は横滑りと記録のずれ (姿勢と位置の時刻差) が効き始める。
MAX_ROLL_DEG = 45.0

#: ACMI の姿勢には壊れた値が入ることがある (地上滑走中の Roll が 13,000 度
#: 前後になる記録がある。kinematics.MAX_SANE_ROLL_DEG と同じ理由)。
MAX_SANE_PITCH_DEG = 60.0


def body_aoa_deg(
    velocity: tuple[float, float, float],
    heading_deg: float,
    pitch_deg: float,
    roll_deg: float,
) -> float:
    """機体軸の迎角 (度)。

    ``velocity`` は (x, y, z) = (前方の基準方向, 右, 上) の座標系での速度、
    ``heading_deg`` は同じ基準方向からの機首方位 (右回り正)。姿勢角は
    Tacview の規約 (ピッチ上げ正、右バンク正)。

    北東下 (NED) の速度を ``Rx(roll) Ry(pitch) Rz(yaw)`` で機体軸に回し、
    前向き成分 u と下向き成分 w から ``atan2(w, u)`` を取る。
    """
    vn, ve, vd = velocity[0], velocity[1], -velocity[2]
    psi = math.radians(heading_deg)
    theta = math.radians(pitch_deg)
    phi = math.radians(roll_deg)
    cpsi, spsi = math.cos(psi), math.sin(psi)
    cth, sth = math.cos(theta), math.sin(theta)
    cphi, sphi = math.cos(phi), math.sin(phi)
    u = cth * cpsi * vn + cth * spsi * ve - sth * vd
    w = (
        (cphi * sth * cpsi + sphi * spsi) * vn
        + (cphi * sth * spsi - sphi * cpsi) * ve
        + cphi * cth * vd
    )
    return math.degrees(math.atan2(w, u))


def estimate_aoa_deg(
    velocity: tuple[float, float, float],
    heading_deg: float | None,
    pitch_deg: float | None,
    roll_deg: float | None,
) -> float | None:
    """1 サンプルの迎角の推定値。推定できない条件では ``None``。

    ``heading_deg`` が無い古い記録では、機首が対地トラックの方を向いていた
    (横滑り・クラブ無し) とみなす。迎角は機体の縦の面の量なので、
    クラブ角の分の誤差は小さい (10 度のクラブで迎角 8 度なら 0.1 度未満)。
    """
    if pitch_deg is None or roll_deg is None:
        return None
    if abs(pitch_deg) > MAX_SANE_PITCH_DEG or abs(roll_deg) > MAX_ROLL_DEG:
        return None
    vx, vy, vz = velocity
    if math.sqrt(vx * vx + vy * vy + vz * vz) < MIN_SPEED_MS:
        return None
    heading = heading_deg if heading_deg is not None else math.degrees(math.atan2(vy, vx))
    return body_aoa_deg(velocity, heading, pitch_deg, roll_deg)


#: 迎角を判定するのに要る最低サンプル数 (5 Hz なら 1 秒分)。
MIN_SAMPLES = 5

#: これを外れる値は記録の破綻 (姿勢の壊れた値、位置の飛びの残り) として
#: 捨てる。固定翼の進入でこの外の迎角は飛べない。
PLAUSIBLE_DEG = (-5.0, 30.0)


def aoa_values(samples: Iterable["DeviationSample"]) -> tuple[list[float], str]:
    """迎角の値と、その出どころ ("recorded" / "estimated")。

    記録に AOA があればそれを使う (クライアント側の録画)。無ければ軌跡と
    姿勢からの推定値。混ぜない: 片方が帯の中で片方が外、という区間で
    中央値が 2 種類の量の間を行き来する。
    """
    low, high = PLAUSIBLE_DEG
    pool = list(samples)
    recorded = [s.aoa for s in pool if s.aoa is not None and low <= s.aoa <= high]
    if len(recorded) >= MIN_SAMPLES:
        return recorded, "recorded"
    estimated = [
        s.aoa_est for s in pool if s.aoa_est is not None and low <= s.aoa_est <= high
    ]
    return estimated, "estimated"


# ---------------------------------------------------------------------------
# 機体別のオンスピード帯
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AoaBand:
    """1 機種の進入迎角の帯 (度、機体軸)。

    AIRBOSS の 7 段 (FAST < Fast < OnSpeedMin <= OnSpeed <= OnSpeedMax <
    Slow < SLOW) と同じ並び。``fast`` / ``slow`` はインデクサーのシェブロンが
    「やや」から「大きく」に変わる境目で、帯の外側に置く。
    """

    name: str
    on_speed: float
    on_speed_min: float
    on_speed_max: float
    fast: float
    slow: float
    source: str | None = None

    @classmethod
    def from_config(cls, name: str, data: dict[str, Any]) -> AoaBand:
        return cls(
            name=name,
            on_speed=float(data["on_speed_deg"]),
            on_speed_min=float(data["on_speed_min_deg"]),
            on_speed_max=float(data["on_speed_max_deg"]),
            fast=float(data["fast_deg"]),
            slow=float(data["slow_deg"]),
            source=data.get("source"),
        )


def aoa_band_for(airframe: str | None, table: dict[str, Any] | None) -> AoaBand | None:
    """機体名 (ACMI の Name) に当たる帯。どれにも当たらなければ ``None``。

    ``table`` は ``{帯の名前: {"match": [部分一致の語...], "on_speed_deg": ...}}``。
    照合は ``airframe_classes`` と同じく大文字小文字を無視した部分一致で、
    **書いた順に** 最初に当たったものを使う (F-14 と F-14BU、Su-27 と Su-33
    のように、短い語が長い名前に含まれる組み合わせは長い方を先に書く)。
    """
    if not airframe or not table:
        return None
    name = str(airframe).upper()
    for key, data in table.items():
        if not isinstance(data, dict):
            continue
        for token in data.get("match") or ():
            if str(token).upper() in name:
                return AoaBand.from_config(str(key), data)
    return None


def aoa_verdict(aoa_deg: float, band: AoaBand) -> str:
    """迎角の読み: "on speed" / "slightly fast" / "fast" / "slightly slow" / "slow"。

    迎角が小さい = 速い (FAST)、大きい = 遅い (SLOW)。
    """
    if band.on_speed_min <= aoa_deg <= band.on_speed_max:
        return "on speed"
    if aoa_deg < band.on_speed_min:
        return "slightly fast" if aoa_deg >= band.fast else "fast"
    return "slightly slow" if aoa_deg <= band.slow else "slow"


#: Fast / Slow の境目より外で点を落とす幅の下限 (度)。Hornet の「やや」帯は
#: 0.5 度しかなく、その幅のまま 65 → 30 → 5 と落とすと、推定のばらつき
#: (記録の AOA との差の p90 が +0.5〜0.7 度) と引けない風 (~0.4 度) だけで
#: 60 点動く。
MIN_FAR_SPAN_DEG = 1.0


def aoa_score(aoa_deg: float, band: AoaBand) -> float:
    """0..100。帯の中は 100、Fast / Slow の境目で 65、その先で 30、5。

    陸上の速度比スコア (``_speed_ratio_score``) と同じ形の折れ線。境目の
    外側は「やや」帯と同じ幅ごとに 30・5 まで落とすが、その幅は
    :data:`MIN_FAR_SPAN_DEG` より狭くしない。
    """
    if band.on_speed_min <= aoa_deg <= band.on_speed_max:
        return 100.0
    if aoa_deg < band.on_speed_min:
        excess = band.on_speed_min - aoa_deg
        span = max(band.on_speed_min - band.fast, 1e-6)
    else:
        excess = aoa_deg - band.on_speed_max
        span = max(band.slow - band.on_speed_max, 1e-6)
    if excess <= span:
        return 100.0 - 35.0 * excess / span
    far = max(span, MIN_FAR_SPAN_DEG)
    beyond = excess - span
    if beyond <= far:
        return 65.0 - 35.0 * beyond / far
    return max(5.0, 30.0 - 25.0 * (beyond - far) / far)
