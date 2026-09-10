#!/usr/bin/env python3
"""Turn a dlt-capture-runways.lua dump into a config/runways/ seed.

    python scripts/dlt_runways_from_dump.py dlt-runways.json [-o config/runways]

The dump is written by the in-game hook (scripts/dlt-capture-runways.lua) in
the same shape DCSServerBot's REST API returns, so the conversion is the
application's own parser -- not a second implementation that could drift from
the one that grades landings.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.runways.dcssb import _convergence_deg, _parse_airbase  # noqa: E402
from app.runways.provider import CACHE_VERSION  # noqa: E402


def convert(dump: dict) -> dict:
    from app.runways.models import EXACT_FIELDS

    theatre = dump.get("theatre") or "unknown"
    airbases = dump.get("airbases") or []
    runways = []
    for airbase in airbases:
        detail = {"airbase": {"runways": airbase.get("runways") or []}}
        runways.extend(
            _parse_airbase(airbase, detail, _convergence_deg(airbase, airbases))
        )
    records = [r for a in airbases for r in (a.get("runways") or [])]
    return {
        "version": CACHE_VERSION,
        "theatre": theatre,
        # True when every runway was placed from DCS's own lat/lon (hook v2).
        # The provider prefers such a seed over a live DCSServerBot sweep,
        # whose grid->geographic conversion is an approximation.
        "exact": bool(records)
        and all(all(r.get(f) is not None for f in EXACT_FIELDS) for r in records),
        "runways": [r.as_dict() for r in runways],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dump", type=Path, help="dlt-runways.json from the hook")
    parser.add_argument(
        "-o", "--out-dir", type=Path, default=REPO_ROOT / "config" / "runways"
    )
    args = parser.parse_args()

    payload = convert(json.loads(args.dump.read_text(encoding="utf-8")))
    if not payload["runways"]:
        print(f"{args.dump}: no runways in the dump", file=sys.stderr)
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(
        c if c.isalnum() or c in "-_" else "_" for c in payload["theatre"]
    )
    target = args.out_dir / f"runways-{safe}.json"
    target.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    airfields = len({r["airbase"] for r in payload["runways"]})
    print(
        f"{target}: {len(payload['runways'])} runways / {airfields} airbases "
        f"({payload['theatre']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
