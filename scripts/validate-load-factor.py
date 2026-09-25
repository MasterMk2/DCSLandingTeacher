#!/usr/bin/env python
"""軌跡から導いた荷重倍数を、記録に入っている Roll と突き合わせる。

``app.grading.kinematics`` は位置の 2 階微分から法線荷重倍数 n を出す。
水平定常旋回では n = 1 / cos(バンク角) なので、DCS が ACMI に書く Roll と
比べれば、この導出がどれだけ当たっているかを **実記録で** 測れる。
(Roll は ACMI に入っているが、加速度計の値は入っていない --- だから G は
導くしかなく、だからこの検証が要る。)

使い方 (リポジトリのルートから)::

    python scripts/validate-load-factor.py path/to/dlt.db [--limit-objects N]

読むのは ``tracks`` / ``objects`` テーブルだけ (読み取り専用で開く)。
出力は、Roll 20-80 度・ピッチ +-10 度以内・上昇率の小さい「水平旋回らしい」
サンプルについての n / (1/cos roll) の分布。1.0 に近く分散が小さいほど
導出が信頼できる。
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.grading.kinematics import fit_kinematics, reject_position_outliers  # noqa: E402

M_PER_DEG_LAT = 111_320.0


def local_xyz(
    rows: list[tuple[float, float, float, float]],
) -> tuple[list[float], list[tuple[float, float, float]]]:
    lat0 = rows[0][1]
    lon0 = rows[0][2]
    cos_lat = math.cos(math.radians(lat0))
    times: list[float] = []
    positions: list[tuple[float, float, float]] = []
    for t, lat, lon, alt in rows:
        x = (lat - lat0) * M_PER_DEG_LAT
        y = (lon - lon0) * M_PER_DEG_LAT * cos_lat
        if times and t <= times[-1]:
            continue
        times.append(t)
        positions.append((x, y, alt))
    return times, positions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--limit-objects", type=int, default=12)
    parser.add_argument("--half-window", type=float, default=1.0)
    parser.add_argument(
        "--no-outlier-rejection",
        action="store_true",
        help="fit every sample as recorded (what the derivation did before 2026-09-26)",
    )
    args = parser.parse_args()

    con = sqlite3.connect(f"file:{args.database.as_posix()}?mode=ro", uri=True)
    cur = con.cursor()
    objects = cur.execute(
        """
        select o.id, o.name, count(*) as n
        from tracks t join objects o on o.id = t.object_id
        where o.type like 'Air+FixedWing%' and t.roll is not null
        group by o.id order by n desc limit ?
        """,
        (args.limit_objects,),
    ).fetchall()

    ratios: list[float] = []
    per_name: dict[str, list[float]] = {}
    turning_total = 0
    rejected_total = 0
    for object_id, name, _count in objects:
        rows = cur.execute(
            """
            select mission_time, latitude, longitude, altitude, roll, pitch
            from tracks where object_id = ? and latitude is not null
            and longitude is not null and altitude is not null
            order by mission_time
            """,
            (object_id,),
        ).fetchall()
        if len(rows) < 50:
            continue
        times, positions = local_xyz([(r[0], r[1], r[2], r[3]) for r in rows])
        attitude = {r[0]: (r[4], r[5]) for r in rows}
        rejected: set[int] = (
            set()
            if args.no_outlier_rejection
            else reject_position_outliers(times, positions, args.half_window)
        )
        rejected_total += len(rejected)
        for index, t in enumerate(times):
            if index in rejected:
                continue
            roll, pitch = attitude.get(t, (None, None))
            if roll is None or pitch is None:
                continue
            bank = abs(roll)
            if not (20.0 <= bank <= 80.0) or abs(pitch) > 10.0:
                continue
            kin = fit_kinematics(times, positions, index, args.half_window, exclude=rejected)
            if kin is None or kin.load_factor is None:
                continue
            # 水平旋回らしさ: 垂直速度が対気速度の 10% 未満。
            if abs(kin.velocity[2]) > 0.1 * kin.speed:
                continue
            turning_total += 1
            expected = 1.0 / math.cos(math.radians(bank))
            ratio = kin.load_factor / expected
            ratios.append(ratio)
            per_name.setdefault(str(name), []).append(ratio)

    if not ratios:
        print("no level-turn samples with Roll found")
        return 1
    ratios.sort()

    def pct(p: float) -> float:
        return ratios[min(len(ratios) - 1, int(p * len(ratios)))]

    print(f"level-turn samples compared: {len(ratios)} (of {turning_total} turning)")
    print(f"position outliers rejected before fitting: {rejected_total}")
    print(
        f"n_kinematic / (1/cos roll): median {statistics.median(ratios):.3f}, "
        f"p10 {pct(0.10):.3f}, p90 {pct(0.90):.3f}, "
        f"mean {statistics.fmean(ratios):.3f}, stdev {statistics.pstdev(ratios):.3f}"
    )
    within = sum(1 for r in ratios if abs(r - 1.0) <= 0.10) / len(ratios)
    print(f"within +-10%: {within * 100:.1f}%")
    for name, values in sorted(per_name.items(), key=lambda kv: -len(kv[1])):
        print(f"  {name:16s} n={len(values):5d} median {statistics.median(values):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
