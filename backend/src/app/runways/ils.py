"""Where DCS's own ILS glidepath meets each runway.

The land grader anchors its reference glidepath at a point on the runway. The
ICAO default (:data:`app.runways.models.DEFAULT_AIMING_POINT_M`, 300 m past
the threshold) is not where DCS puts its glideslopes: the terrain files site
them a median 228 m past the threshold, and anywhere from ~110 to ~480 m on
the big civil fields. An approach flown down DCS's ILS therefore read as "low"
by a constant 2-10 m against the default, on every runway, which is the
"clean 3-degree approach still shows a deviation" complaint this module
answers.

Each terrain ships a plain-text ``Mods/terrains/<Terrain>/Beacons.lua`` whose
``BEACON_TYPE_ILS_GLIDESLOPE`` entries carry the antenna's geographic position
and the runway direction it serves. The glideslope antenna stands abeam the
glidepath intercept point (GPI), typically 120 m off the centreline, so the
GPI is the antenna's along-track position projected onto the runway axis.

:func:`parse_glideslope_beacons` reads the Lua; :func:`match_glideslopes`
pairs each antenna with a seeded landing direction; the command-line entry
point in ``scripts/dlt_ils_from_beacons.py`` writes the result back into the
``config/runways/`` seeds as ``glidepath_intercept_m``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.detection.geometry import transform_to_frame
from app.runways.models import Runway, heading_difference_deg, normalize_heading

#: Largest disagreement between the direction the beacon file states and the
#: seeded runway heading. The file's ``direction`` points back down the
#: approach (``direction + 180`` is the landing heading); 15 deg absorbs the
#: file's own rounding and a designator stored against a slightly rotated
#: strip, while still separating a runway from anything but its own
#: reciprocal (180 deg off) and crossing strips (60 deg and more).
MAX_HEADING_DIFF_DEG = 15.0
#: The antenna stands beside the runway, never on it: between the edge of a
#: wide strip and a parallel taxiway. Measured 44-217 m on the installed maps.
MIN_LATERAL_M = 30.0
MAX_LATERAL_M = 300.0
#: The GPI lies past the threshold, inside the first part of the runway. Below
#: the floor the "threshold" the seed derived from DCS's runway length is not
#: the landing threshold the ILS serves (a displaced threshold the seed cannot
#: see); above the ceiling the antenna belongs to a longer field's far-end
#: ILS, or the seed length is short. Both are left to the default anchor and
#: reported, rather than written into the seed.
MIN_ALONG_M = 40.0
MAX_ALONG_M = 600.0

_BLOCK_RE = re.compile(r"\{(?P<body>(?:[^{}]|\{[^{}]*\})*)\}", re.S)
_CALLSIGN_RE = re.compile(r"callsign\s*=\s*'([^']*)'")
_GEO_RE = re.compile(
    r"positionGeo\s*=\s*\{\s*latitude\s*=\s*([-+0-9.eE]+)\s*,\s*longitude\s*=\s*([-+0-9.eE]+)"
)
_DIRECTION_RE = re.compile(r"direction\s*=\s*([-+0-9.eE]+)")


@dataclass(frozen=True)
class GlideslopeBeacon:
    """One ILS glideslope antenna as the terrain file states it."""

    callsign: str
    latitude: float
    longitude: float
    #: Landing heading of the runway this glideslope serves, true degrees.
    landing_heading_deg: float | None


@dataclass(frozen=True)
class GlideslopeMatch:
    """An antenna paired with the landing direction it serves."""

    runway: Runway
    beacon: GlideslopeBeacon
    #: Metres past the threshold at which the glidepath meets the runway.
    intercept_m: float
    #: Metres right (+) of the centreline the antenna stands.
    lateral_m: float


@dataclass(frozen=True)
class Unmatched:
    beacon: GlideslopeBeacon
    reason: str


def parse_glideslope_beacons(text: str) -> list[GlideslopeBeacon]:
    """Every ``BEACON_TYPE_ILS_GLIDESLOPE`` entry that carries a position.

    The file is a Lua table literal; each beacon is one ``{ ... };`` block with
    one level of nesting (``position = {}`` / ``positionGeo = {}``). A regular
    expression over that shape is enough, and avoids executing game files.
    Entries without ``positionGeo`` are skipped: the DCS grid position alone
    would need the projection this module exists to avoid approximating.
    """
    beacons: list[GlideslopeBeacon] = []
    for block in _BLOCK_RE.finditer(text):
        body = block.group("body")
        if "BEACON_TYPE_ILS_GLIDESLOPE" not in body:
            continue
        geo = _GEO_RE.search(body)
        if geo is None:
            continue
        callsign = _CALLSIGN_RE.search(body)
        direction = _DIRECTION_RE.search(body)
        heading: float | None = None
        if direction is not None:
            # ``direction`` points from the antenna back down the approach;
            # the landing heading is its reciprocal.
            heading = normalize_heading(float(direction.group(1)) + 180.0)
        beacons.append(
            GlideslopeBeacon(
                callsign=callsign.group(1) if callsign else "",
                latitude=float(geo.group(1)),
                longitude=float(geo.group(2)),
                landing_heading_deg=heading,
            )
        )
    return beacons


def match_glideslopes(
    runways: list[Runway], beacons: list[GlideslopeBeacon]
) -> tuple[list[GlideslopeMatch], list[Unmatched]]:
    """Pair each glideslope antenna with the landing direction it serves.

    A candidate runway must agree with the beacon's stated landing heading
    (which already rules out the reciprocal direction of the same strip) and
    have the antenna beside its first few hundred metres. Among candidates
    the one whose centreline the antenna stands closest to wins -- parallel
    strips sit a few hundred metres apart, so the lateral offset separates
    them even though both pass the along-track test. Each runway takes at
    most one antenna; where two claim the same runway the nearer one stays
    and the other is reported.
    """
    candidates: list[tuple[float, GlideslopeMatch]] = []
    unmatched: list[Unmatched] = []
    for beacon in beacons:
        best: tuple[float, GlideslopeMatch] | None = None
        reasons: list[str] = []
        for runway in runways:
            if beacon.landing_heading_deg is not None and (
                heading_difference_deg(runway.heading_deg, beacon.landing_heading_deg)
                > MAX_HEADING_DIFF_DEG
            ):
                continue
            along, lateral = transform_to_frame(
                beacon.latitude,
                beacon.longitude,
                runway.threshold_lat,
                runway.threshold_lon,
                runway.heading_deg,
            )
            if abs(lateral) > 2000.0 or abs(along) > 3000.0:
                continue  # a different airfield altogether
            if not (MIN_LATERAL_M <= abs(lateral) <= MAX_LATERAL_M):
                reasons.append(f"{runway.airbase} {runway.name}: lateral {lateral:.0f} m")
                continue
            if not (MIN_ALONG_M <= along <= MAX_ALONG_M):
                reasons.append(f"{runway.airbase} {runway.name}: {along:.0f} m past threshold")
                continue
            match = GlideslopeMatch(runway, beacon, along, lateral)
            if best is None or abs(lateral) < best[0]:
                best = (abs(lateral), match)
        if best is None:
            unmatched.append(
                Unmatched(beacon, "; ".join(reasons) if reasons else "no runway nearby")
            )
        else:
            candidates.append(best)

    # One antenna per landing direction: keep the one standing nearest the
    # centreline, report the other.
    by_runway: dict[tuple[str, str], tuple[float, GlideslopeMatch]] = {}
    for key_lateral, match in candidates:
        key = (match.runway.airbase, match.runway.name)
        held = by_runway.get(key)
        if held is None or key_lateral < held[0]:
            if held is not None:
                unmatched.append(
                    Unmatched(held[1].beacon, f"{key[0]} {key[1]} already has a nearer glideslope")
                )
            by_runway[key] = (key_lateral, match)
        else:
            unmatched.append(Unmatched(match.beacon, f"{key[0]} {key[1]} already has a nearer glideslope"))
    matches = [match for _lat, match in by_runway.values()]
    matches.sort(key=lambda m: (m.runway.airbase, m.runway.name))
    return matches, unmatched


def apply_intercepts(runways: list[Runway], matches: list[GlideslopeMatch]) -> list[Runway]:
    """``runways`` with ``glidepath_intercept_m`` set from ``matches``.

    Directions without a match keep ``None`` -- including one that had a
    value from an earlier run, so a re-run against a corrected seed never
    leaves a stale anchor behind.
    """
    from dataclasses import replace

    by_key = {(m.runway.airbase, m.runway.name): m.intercept_m for m in matches}
    return [
        replace(runway, glidepath_intercept_m=by_key.get((runway.airbase, runway.name)))
        for runway in runways
    ]
