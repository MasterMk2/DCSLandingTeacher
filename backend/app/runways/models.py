"""Runway geometry as used by the land grader.

One :class:`Runway` is a single *landing direction*: DCS reports one entry
per physical strip (centre + course + length), which yields two runways --
one per end -- because an approach to 13 and an approach to 31 are graded
against opposite thresholds.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

#: Standard aiming point: a 3-degree glidepath is flown to a point this far
#: past the threshold, which is what puts the aircraft over the threshold at
#: the usual ~15 m (50 ft) threshold crossing height. Anchoring the reference
#: line here (rather than at the touchdown point, which moves with however
#: long the pilot floats) is what makes the deviation comparable between
#: landings.
DEFAULT_AIMING_POINT_M = 300.0


@dataclass(frozen=True)
class Runway:
    """One landing direction of one runway."""

    airbase: str
    name: str
    #: Threshold (approach end) position.
    threshold_lat: float
    threshold_lon: float
    elevation_m: float
    #: True heading of the landing direction, degrees.
    heading_deg: float
    length_m: float
    width_m: float

    def aiming_point(
        self, aiming_point_m: float = DEFAULT_AIMING_POINT_M
    ) -> tuple[float, float]:
        """Lat/lon of the aiming point, ``aiming_point_m`` past the threshold."""
        from app.detection.geometry import offset_position

        return offset_position(
            self.threshold_lat,
            self.threshold_lon,
            self.heading_deg,
            aiming_point_m,
            0.0,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "airbase": self.airbase,
            "name": self.name,
            "threshold_lat": round(self.threshold_lat, 7),
            "threshold_lon": round(self.threshold_lon, 7),
            "elevation_m": round(self.elevation_m, 2),
            "heading_deg": round(self.heading_deg, 2),
            "length_m": round(self.length_m, 1),
            "width_m": round(self.width_m, 1),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Runway:
        return cls(
            airbase=data["airbase"],
            name=data["name"],
            threshold_lat=float(data["threshold_lat"]),
            threshold_lon=float(data["threshold_lon"]),
            elevation_m=float(data["elevation_m"]),
            heading_deg=float(data["heading_deg"]),
            length_m=float(data["length_m"]),
            width_m=float(data["width_m"]),
        )


def heading_difference_deg(a: float, b: float) -> float:
    """Smallest absolute difference between two headings, in degrees."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def best_runway_match(
    runways: list[Runway],
    latitude: float,
    longitude: float,
    course_deg: float | None,
    *,
    max_distance_m: float = 4000.0,
    max_heading_diff_deg: float = 25.0,
) -> tuple[Runway, tuple[float, float]] | None:
    """Best runway for a touchdown, with the key it was ranked by.

    Candidates must be within ``max_distance_m`` of the threshold and, when
    a course estimate is available, aligned with it -- otherwise a touchdown
    would happily match the *opposite* end of the same strip, which would
    invert every deviation.

    Among the survivors the *lateral* offset from the extended centreline
    decides, and only then the distance to the threshold. Ranking by
    threshold distance alone is enough while an airfield has one strip, but
    it decides parallel runways by how long the pilot floated: at Nellis the
    two strips are ~275 m apart, so a touchdown 300 m past 03R is 300 m from
    its own threshold and 407 m from 03L's -- while one 2000 m past (a long
    landing, or a touch-and-go rolled well down the runway) is 2000 m versus
    2019 m, and the wrong strip wins on noise. The lateral offset does not
    move as the aircraft rolls down the centreline, which is exactly the
    property the choice needs.

    The returned key is comparable across theatres, so a caller holding
    several swept maps can pick the single best match rather than the first
    map that happened to produce one.
    """
    from app.detection.geometry import haversine_m, transform_to_frame

    best: Runway | None = None
    best_key: tuple[float, float] | None = None
    for runway in runways:
        if course_deg is not None and (
            heading_difference_deg(runway.heading_deg, course_deg)
            > max_heading_diff_deg
        ):
            continue
        distance = haversine_m(
            latitude, longitude, runway.threshold_lat, runway.threshold_lon
        )
        # A touchdown happens *past* the threshold, so allow the whole strip
        # plus a margin rather than requiring proximity to the threshold.
        if distance > max_distance_m:
            continue
        _, lateral = transform_to_frame(
            latitude,
            longitude,
            runway.threshold_lat,
            runway.threshold_lon,
            runway.heading_deg,
        )
        key = (abs(lateral), distance)
        if best_key is None or key < best_key:
            best, best_key = runway, key
    if best is None or best_key is None:
        return None
    return best, best_key


def match_runway(
    runways: list[Runway],
    latitude: float,
    longitude: float,
    course_deg: float | None,
    *,
    max_distance_m: float = 4000.0,
    max_heading_diff_deg: float = 25.0,
) -> Runway | None:
    """Pick the runway a touchdown at ``latitude``/``longitude`` belongs to."""
    match = best_runway_match(
        runways,
        latitude,
        longitude,
        course_deg,
        max_distance_m=max_distance_m,
        max_heading_diff_deg=max_heading_diff_deg,
    )
    return match[0] if match is not None else None


def normalize_heading(deg: float) -> float:
    return deg % 360.0


def runway_pair_from_dcs(
    airbase: str,
    dcs_name: Any,
    course_rad: float,
    centre_x: float,
    centre_z: float,
    elevation_m: float,
    length_m: float,
    width_m: float,
    airbase_ref: tuple[float, float, float, float],
    convergence_deg: float = 0.0,
) -> list[Runway]:
    """Expand one DCS runway record into its two landing directions.

    ``airbase_ref`` is ``(lat, lon, x, z)`` for the airbase itself, used as a
    local tangent-plane origin: a runway is at most ~2 km from it, so a flat
    conversion there is exact to well under a metre and avoids needing the
    theatre's map projection.

    DCS reports ``course`` in radians measured opposite to compass heading
    (verified against the sister DCSWebGCA project and DCS's own runway
    numbering, e.g. Batumi's single strip yields 305.6 deg / 125.6 deg for
    runways 31 and 13).

    ``convergence_deg`` rotates the DCS frame onto the geographic one. DCS's
    x/z is the map projection's GRID, whose north is meridian convergence
    away from true north -- 4.9 deg at Sochi on Caucasus, 5.7 deg at Batumi.
    Treating x as due north (which this did) silently rotated every runway
    about its airfield by that much, so an aircraft flying the runway
    heading measured as 6 deg off centreline the whole way down final. It
    showed on 13 landings across four airfields, all with the same sign.
    """
    from app.detection.geometry import (
        meters_per_degree_latitude,
        meters_per_degree_longitude,
    )

    lat_ref, lon_ref, x_ref, z_ref = airbase_ref
    grid_heading = normalize_heading(math.degrees(-course_rad))
    m_per_deg_lat = meters_per_degree_latitude(lat_ref)
    m_per_deg_lon = max(meters_per_degree_longitude(lat_ref), 1e-6)
    convergence = math.radians(convergence_deg)
    cos_c, sin_c = math.cos(convergence), math.sin(convergence)

    named_end = _designator_end(dcs_name, grid_heading)

    runways: list[Runway] = []
    for index, grid_head in enumerate(
        (grid_heading, normalize_heading(grid_heading + 180.0))
    ):
        rad = math.radians(grid_head)
        # The threshold of a landing direction sits half a length *behind*
        # the centre, along that direction. DCS x is grid north, z grid east.
        x = centre_x - math.cos(rad) * (length_m / 2.0)
        z = centre_z - math.sin(rad) * (length_m / 2.0)
        # Rotate the grid offset onto geographic axes before it becomes a
        # latitude and a longitude.
        north = (x - x_ref) * cos_c - (z - z_ref) * sin_c
        east = (x - x_ref) * sin_c + (z - z_ref) * cos_c
        lat = lat_ref + north / m_per_deg_lat
        lon = lon_ref + east / m_per_deg_lon
        head = normalize_heading(grid_head + convergence_deg)
        runways.append(
            Runway(
                airbase=airbase,
                name=_runway_name(dcs_name, grid_head, primary=index == named_end),
                threshold_lat=lat,
                threshold_lon=lon,
                elevation_m=elevation_m,
                heading_deg=head,
                length_m=length_m,
                width_m=width_m,
            )
        )
    return runways


#: Parallel strips are suffixed L / C / R from the point of view of the
#: pilot on approach, so the *left* of a pair landing 03 is the *right* one
#: landing 21. The mapping is therefore a mirror, not a copy.
_SUFFIX_MIRROR = {"L": "R", "R": "L", "C": "C"}

#: A runway designator as DCS reports it: one or two digits, optionally with
#: a position suffix. DCS sends the number as an int for most airfields
#: ("Name": 31) and as a string when there is a suffix ("Name": "03L").
_DESIGNATOR_RE = re.compile(r"^(\d{1,2})\s*([LCR]?)$")


def reciprocal_designator(designator: str) -> str:
    """``"03L"`` -> ``"21R"``, ``"13"`` -> ``"31"``; ``""`` when unparsable.

    Derived from the designator DCS itself reports rather than re-derived
    from the heading, for two reasons. Runway numbers are *magnetic* and
    rounded to ten degrees, so a heading-derived number lands on the wrong
    side of the rounding wherever declination is a few degrees -- and the
    heading here is the grid one, off by the meridian convergence on top.
    More importantly a heading carries no L/C/R, so both strips of a
    parallel pair came out with the same name: at Nellis (03L/21R and
    03R/21L) the two runways were indistinguishable in the UI, in the venue
    filter, and in anything counting landings per runway. Caucasus has no
    parallel strips, which is why this held for as long as it did.
    """
    match = _DESIGNATOR_RE.match(designator)
    if match is None:
        return ""
    number, suffix = int(match.group(1)), match.group(2)
    if not 1 <= number <= 36:
        return ""
    opposite = (number + 17) % 36 + 1
    return f"{opposite:02d}{_SUFFIX_MIRROR.get(suffix, '')}"


def _designator_end(dcs_name: Any, grid_heading_deg: float) -> int:
    """Which of the two ends the designator DCS reported actually names.

    ``course`` and ``Name`` agree on every airfield checked so far (Batumi's
    "31" points at 305.6 deg grid), but they are independent fields, and a
    designator hung on the wrong end names both directions backwards --
    which, unlike a wrong number, inverts the landing direction the pilot
    reads off the row without anything looking odd. The designator is
    magnetic and the heading is grid, so they differ by declination plus
    convergence; only the 0-vs-180 question is asked here and 90 deg of
    slack answers it on any map.
    """
    designator = "" if dcs_name in (None, "") else str(dcs_name).strip().upper()
    match = _DESIGNATOR_RE.match(designator)
    if match is None:
        return 0
    number = int(match.group(1))
    if not 1 <= number <= 36:
        return 0
    return 0 if heading_difference_deg(number * 10.0, grid_heading_deg) <= 90.0 else 1


def _runway_name(dcs_name: Any, heading_deg: float, *, primary: bool) -> str:
    """Prefer the name DCS reports for the primary end; derive the other."""
    designator = "" if dcs_name in (None, "") else str(dcs_name).strip().upper()
    if primary and designator:
        return designator
    if designator and (reciprocal := reciprocal_designator(designator)):
        return reciprocal
    number = int(round(heading_deg / 10.0)) or 36
    return f"{number:02d}"
