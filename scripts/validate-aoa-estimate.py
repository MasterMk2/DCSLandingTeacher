#!/usr/bin/env python
"""軌跡と姿勢から推定した迎角を、記録に入っている AOA と突き合わせる。

サーバー側の記録には AOA が無いので、採点は ``app.grading.aoa`` の推定値
(位置の微分で出した速度ベクトルを、Yaw / Pitch / Roll で機体軸に回した
角度) で行う。DCS の **クライアント側** の Tacview 録画には自機の ``AOA``
が入っているので、それと比べればこの推定がどれだけ当たるかを実記録で
測れる。

使い方 (リポジトリのルートから)::

    python scripts/validate-aoa-estimate.py path/to/*.acmi [...]

``.zip.acmi`` / ``.txt.acmi`` のどちらも読む (読み取りのみ)。出力は機種別の
「推定 - 記録」の分布。全体と、進入らしい区間 (高度 600 m 未満・降下中・
バンク 30 度以内・迎角 4-16 度) に分けて出す。風は引けない (ACMI に無い)
ので、録画したミッションの風の分だけ推定は大きめに出る。
"""

from __future__ import annotations

import argparse
import io
import math
import statistics
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from app.acmi.models import AcmiObject, ObjectUpdateEvent, TimeEvent  # noqa: E402
from app.acmi.parser import AcmiParser  # noqa: E402
from app.grading.aoa import estimate_aoa_deg  # noqa: E402
from app.grading.kinematics import fit_kinematics, reject_position_outliers  # noqa: E402

M_PER_DEG_LAT = 111_320.0


def read_lines(path: Path):
    if path.name.endswith(".zip.acmi"):
        with zipfile.ZipFile(path) as archive:
            with archive.open(archive.namelist()[0]) as raw:
                yield from io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace")
    else:
        with open(path, encoding="utf-8-sig", errors="replace") as handle:
            yield from handle


def ids_with_aoa(path: Path) -> set[str]:
    """``AOA=`` を一度でも書かれた物体の id (文字列の走査だけで拾う)。"""
    found: set[str] = set()
    for line in read_lines(path):
        if "AOA=" in line:
            found.add(line.split(",", 1)[0])
    return found


def collect(path: Path) -> dict[str, list[dict[str, float | None]]]:
    """AOA が記録されている物体ごとに、各フレームの状態を集める。

    録画は数百 MB になり、全行をパーサに通すと 1 本で数十分かかる。対象の
    物体の行と、時刻行 (``#``)・全体の行 (``0,``: 座標の基準点を持つ) だけを
    通す。
    """
    targets = ids_with_aoa(path)
    if not targets:
        return {}
    parser = AcmiParser()
    objects: dict[str, AcmiObject] = {}
    rows: dict[str, list[dict[str, float | None]]] = defaultdict(list)
    for line in read_lines(path):
        head = line.split(",", 1)[0]
        if not (line.startswith(("#", "0,", "FileType", "FileVersion")) or head in targets):
            continue
        for event in parser.feed_line(line):
            if isinstance(event, TimeEvent) or not isinstance(event, ObjectUpdateEvent):
                continue
            if event.obj_id == "0":
                continue
            obj = objects.setdefault(event.obj_id, AcmiObject(event.obj_id))
            obj.update_properties(event.properties)
            if "AOA" not in event.properties and not rows.get(event.obj_id):
                continue
            rows[event.obj_id].append(
                {
                    "time": event.time,
                    "lat": obj.latitude,
                    "lon": obj.longitude,
                    "alt": obj.altitude,
                    "roll": obj.roll,
                    "pitch": obj.pitch,
                    "yaw": obj.yaw,
                    "aoa": obj.aoa,
                    "agl": obj.agl,
                    "name": obj.name,  # type: ignore[dict-item]
                }
            )
    return rows


#: 1 回のフィットに渡す区間の最大サンプル数。外れ値除去は区間の長さの
#: 2 乗で効くので、何時間もの録画を 1 本で渡すとメモリが足りなくなる。
CHUNK_SAMPLES = 2000


def compare(rows: list[dict]) -> list[tuple[float, float, bool]]:
    """(推定, 記録, 進入らしいか) の組。記録の切れ目と一定の長さで区切って測る。"""
    out: list[tuple[float, float, bool]] = []
    chunk: list[dict] = []
    for r in rows:
        if chunk and (r["time"] - chunk[-1]["time"] > 3.0 or len(chunk) >= CHUNK_SAMPLES):
            out.extend(_compare_chunk(chunk))
            chunk = []
        chunk.append(r)
    out.extend(_compare_chunk(chunk))
    return out


def _compare_chunk(rows: list[dict]) -> list[tuple[float, float, bool]]:
    usable = [r for r in rows if None not in (r["lat"], r["lon"], r["alt"])]
    if len(usable) < 10:
        return []
    lat0, lon0 = usable[0]["lat"], usable[0]["lon"]
    cos_lat = math.cos(math.radians(lat0))
    times: list[float] = []
    positions: list[tuple[float, float, float]] = []
    kept: list[dict] = []
    for r in usable:
        if times and r["time"] <= times[-1]:
            continue
        times.append(r["time"])
        positions.append(
            (
                (r["lat"] - lat0) * M_PER_DEG_LAT,
                (r["lon"] - lon0) * M_PER_DEG_LAT * cos_lat,
                r["alt"],
            )
        )
        kept.append(r)
    rejected = reject_position_outliers(times, positions)
    out: list[tuple[float, float, bool]] = []
    for index, r in enumerate(kept):
        if index in rejected or r["aoa"] is None:
            continue
        fit = fit_kinematics(times, positions, index, exclude=rejected)
        if fit is None:
            continue
        estimate = estimate_aoa_deg(fit.velocity, r["yaw"], r["pitch"], r["roll"])
        if estimate is None:
            continue
        height = r["agl"] if r["agl"] is not None else r["alt"]
        approach = (
            height < 600.0
            and fit.velocity[2] < -1.0
            and abs(r["roll"] or 0.0) <= 30.0
            and 4.0 <= r["aoa"] <= 16.0
        )
        out.append((estimate, r["aoa"], approach))
    return out


def summarize(label: str, pairs: list[tuple[float, float]]) -> None:
    if len(pairs) < 5:
        print(f"  {label}: n={len(pairs)} (too few)")
        return
    diffs = sorted(e - a for e, a in pairs)
    n = len(diffs)

    def q(p: float) -> float:
        return diffs[min(n - 1, max(0, round(p * (n - 1))))]

    within = lambda tol: sum(1 for d in diffs if abs(d) <= tol) / n * 100.0  # noqa: E731
    print(
        f"  {label}: n={n}  median {statistics.median(diffs):+.2f}  "
        f"mean {statistics.fmean(diffs):+.2f}  p10 {q(0.1):+.2f}  p90 {q(0.9):+.2f}  "
        f"|d|<=0.5: {within(0.5):.0f}%  |d|<=1.0: {within(1.0):.0f}%"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recordings", nargs="+", type=Path)
    args = parser.parse_args()
    by_type: dict[str, list[tuple[float, float, bool]]] = defaultdict(list)
    for path in args.recordings:
        for rows in collect(path).values():
            name = next((r["name"] for r in rows if r["name"]), "?")
            by_type[str(name)].extend(compare(rows))
    for name, triples in sorted(by_type.items()):
        print(name)
        summarize("all airborne", [(e, a) for e, a, _ in triples])
        summarize("approach-like", [(e, a) for e, a, approach in triples if approach])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
