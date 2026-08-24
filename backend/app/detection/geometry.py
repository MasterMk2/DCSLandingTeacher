"""Local-plane geometry helpers for approach analysis.

All functions approximate the Earth as a sphere and project onto a local
tangent plane (equirectangular). This is accurate to well under a meter over
the few-kilometer scale of a final approach segment.
"""

from __future__ import annotations

import math

EARTH_RADIUS_M = 6371000.0


def meters_per_degree_latitude(lat_deg: float, radius_m: float = EARTH_RADIUS_M) -> float:
    return math.radians(radius_m)


def meters_per_degree_longitude(lat_deg: float, radius_m: float = EARTH_RADIUS_M) -> float:
    return math.radians(radius_m) * math.cos(math.radians(lat_deg))


def to_local_xy(
    lat_deg: float,
    lon_deg: float,
    origin_lat_deg: float,
    origin_lon_deg: float,
    radius_m: float = EARTH_RADIUS_M,
) -> tuple[float, float]:
    """Project (lat, lon) onto an ENU tangent plane at the origin.

    Returns ``(x_east, y_north)`` in meters.
    """
    x = math.radians(lon_deg - origin_lon_deg) * radius_m * math.cos(
        math.radians(origin_lat_deg)
    )
    y = math.radians(lat_deg - origin_lat_deg) * radius_m
    return x, y


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in meters."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def transform_to_frame(
    lat_deg: float,
    lon_deg: float,
    origin_lat_deg: float,
    origin_lon_deg: float,
    heading_deg: float,
) -> tuple[float, float]:
    """Express (lat, lon) in a body frame anchored at the origin.

    The frame's +X axis points along ``heading_deg`` (true bearing, degrees),
    +Y points 90 degrees to its right. Returns ``(along, lateral)`` meters:

    - ``along``  : positive ahead of the origin along the heading,
    - ``lateral``: positive to the right of the heading.

    Used both for centerline deviation (runway/angle-frame) and for ship-
    relative coordinates when ``origin``/``heading`` come from the carrier
    at the sample time.
    """
    x_east, y_north = to_local_xy(lat_deg, lon_deg, origin_lat_deg, origin_lon_deg)
    theta = math.radians(heading_deg)
    sin_h, cos_h = math.sin(theta), math.cos(theta)
    along = x_east * sin_h + y_north * cos_h
    lateral = x_east * cos_h - y_north * sin_h
    return along, lateral


def offset_position(
    lat_deg: float,
    lon_deg: float,
    heading_deg: float,
    along_m: float,
    lateral_m: float,
) -> tuple[float, float]:
    """Inverse of :func:`transform_to_frame` for small offsets.

    Given an origin and a body frame heading, return the (lat, lon) of the
    point ``along_m`` meters ahead (+) / behind (-) and ``lateral_m`` meters
    right (+) / left (-) of the origin. Accurate for the ~100 m scale of
    ship-deck offsets.
    """
    theta = math.radians(heading_deg)
    sin_h, cos_h = math.sin(theta), math.cos(theta)
    x_east = along_m * sin_h + lateral_m * cos_h
    y_north = along_m * cos_h - lateral_m * sin_h
    lat = lat_deg + math.degrees(y_north / EARTH_RADIUS_M)
    lon = lon_deg + math.degrees(
        x_east / (EARTH_RADIUS_M * math.cos(math.radians(lat_deg)))
    )
    return lat, lon


def interpolate_position(
    track: list[tuple[float, float, float]],
    time: float,
) -> tuple[float, float] | None:
    """Linearly interpolate (lat, lon) from a time-sorted position track.

    ``track`` items are ``(time, lat, lon)``. Returns ``None`` outside the
    covered range or for an empty track.
    """
    if not track:
        return None
    if time <= track[0][0]:
        return track[0][1], track[0][2]
    if time >= track[-1][0]:
        return track[-1][1], track[-1][2]
    lo, hi = 0, len(track) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if track[mid][0] <= time:
            lo = mid
        else:
            hi = mid
    t0, lat0, lon0 = track[lo]
    t1, lat1, lon1 = track[hi]
    if t1 == t0:
        return lat0, lon0
    frac = (time - t0) / (t1 - t0)
    return lat0 + (lat1 - lat0) * frac, lon0 + (lon1 - lon0) * frac
