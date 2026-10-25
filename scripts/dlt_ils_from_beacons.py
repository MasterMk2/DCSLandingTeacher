#!/usr/bin/env python3
"""Write DCS's ILS glidepath intercept into the runway seeds.

    python scripts/dlt_ils_from_beacons.py --dcs "<DCS World install>" [--theatre Caucasus]

Reads ``Mods/terrains/<Terrain>/Beacons.lua`` from a DCS installation, pairs
every ILS glideslope antenna with the landing direction it serves in
``config/runways/runways-<Theatre>.json`` (app.runways.ils) and stores the
along-track distance from the threshold to the glidepath intercept point as
``glidepath_intercept_m``. Directions without an ILS lose any earlier value.

Why: the grader anchors its reference glidepath here. DCS sites its
glideslopes a median 228 m past the threshold, not at the 300 m default, so
without this an approach flown down the game's own ILS reads as "low".

Antennas that match no seeded direction, or sit outside the plausible range
(40-600 m past the threshold, 30-300 m beside the centreline), are printed and
left out: a seed threshold derived from DCS's runway length can differ from
the landing threshold the ILS serves (displaced thresholds), and writing a
guess there would be worse than the default.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.runways.ils import (  # noqa: E402
    apply_intercepts,
    match_glideslopes,
    parse_glideslope_beacons,
)
from app.runways.models import Runway  # noqa: E402

#: Seed theatre name -> terrain folder, where the two differ.
TERRAIN_FOLDERS = {"SinaiMap": "Sinai"}


def update_seed(seed_path: Path, beacons_path: Path) -> tuple[int, int, list[str]]:
    """Rewrite ``seed_path`` with the intercepts read from ``beacons_path``.

    Returns ``(matched, unmatched, report lines)``.
    """
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    runways = [Runway.from_dict(r) for r in seed.get("runways", [])]
    beacons = parse_glideslope_beacons(beacons_path.read_text(encoding="utf-8", errors="replace"))
    matches, unmatched = match_glideslopes(runways, beacons)
    lines = [
        f"  {m.runway.airbase} {m.runway.name}: ILS {m.beacon.callsign or '?'} "
        f"-> {m.intercept_m:.0f} m past threshold ({m.lateral_m:+.0f} m abeam)"
        for m in matches
    ]
    lines.extend(
        f"  skipped ILS {u.beacon.callsign or '?'} ({u.beacon.latitude:.4f}, "
        f"{u.beacon.longitude:.4f}): {u.reason}"
        for u in unmatched
    )
    seed["runways"] = [r.as_dict() for r in apply_intercepts(runways, matches)]
    # Same layout dlt_runways_from_dump.py writes, so the diff is the new field only.
    seed_path.write_text(json.dumps(seed, indent=1), encoding="utf-8")
    return len(matches), len(unmatched), lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dcs", type=Path, required=True, help="DCS World installation directory")
    parser.add_argument("--seeds", type=Path, default=REPO_ROOT / "config" / "runways")
    parser.add_argument("--theatre", action="append", help="only this seed theatre (repeatable)")
    args = parser.parse_args()

    status = 0
    for seed_path in sorted(args.seeds.glob("runways-*.json")):
        theatre = json.loads(seed_path.read_text(encoding="utf-8")).get("theatre") or seed_path.stem
        if args.theatre and theatre not in args.theatre:
            continue
        folder = TERRAIN_FOLDERS.get(theatre, theatre)
        beacons_path = args.dcs / "Mods" / "terrains" / folder / "Beacons.lua"
        if not beacons_path.is_file():
            print(f"{theatre}: no {beacons_path.name} under {folder}; seed left unchanged", file=sys.stderr)
            status = 1
            continue
        matched, unmatched, lines = update_seed(seed_path, beacons_path)
        print(f"{seed_path.name}: {matched} glidepath intercepts written, {unmatched} antennas skipped")
        print("\n".join(lines))
    return status


if __name__ == "__main__":
    sys.exit(main())
