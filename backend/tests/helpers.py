"""Synthetic approach-track builders shared by detection/grading/API tests.

The scenario is a straight-in approach from the south onto a deck/runway at
the origin ``(LAT0, LON0)`` with course ~000 (north). Touchdown happens at
``t == 0``; samples are 1 Hz.
"""

from __future__ import annotations

import math

from app.detection.detector import CarrierState, TrackSample

LAT0 = 35.0
LON0 = 140.0
DECK_ALTITUDE_M = 20.0
M_PER_DEG_LAT = 111320.0


def _lat_offset(meters: float) -> float:
    return meters / M_PER_DEG_LAT


def _lon_offset(meters: float) -> float:
    return meters / (M_PER_DEG_LAT * math.cos(math.radians(LAT0)))


def make_carrier_state(
    obj_id: str = "C1",
    name: str | None = "CV-59",
    lat: float = LAT0,
    lon: float = LON0,
    altitude: float = DECK_ALTITUDE_M,
    heading: float = 0.0,
    type_str: str | None = None,
) -> CarrierState:
    """A stationary carrier at the touchdown point."""
    return CarrierState(
        obj_id=obj_id,
        name=name,
        type=type_str,
        samples=[(0.0, lat, lon, altitude, heading, 0.0)],
    )


def make_approach_samples(
    *,
    outcome: str = "full_stop",
    glideslope_deg: float = 3.5,
    approach_speed_ms: float = 70.0,
    touchdown_speed_ms: float | None = None,
    gs_offset_m: float = 0.0,
    lateral_offset_m: float = 0.0,
    pre_touchdown_descent_ms: float | None = None,
    duration_before_s: float = 55.0,
    ground_time_s: float = 25.0,
    deck_altitude_m: float = DECK_ALTITUDE_M,
    offset_east_m: float = 0.0,
    offset_north_m: float = 0.0,
) -> list[TrackSample]:
    """Build a 1 Hz synthetic approach ending with a landing.

    - ``gs_offset_m``: constant height offset above the ideal slope during
      the whole inbound segment (positive = high).
    - ``lateral_offset_m``: constant lateral offset right of centerline.
    - ``pre_touchdown_descent_ms``: overrides the descent rate over the last
      3 s before contact (None = follow the slope).
    - ``outcome``: ``full_stop`` stays on deck, ``touch_and_go`` climbs out
      again after ~3 s of ground time.
    """
    tan_slope = math.tan(math.radians(glideslope_deg))
    td_speed = (
        approach_speed_ms if touchdown_speed_ms is None else touchdown_speed_ms
    )
    samples: list[TrackSample] = []
    n_before = int(duration_before_s)
    n_after = max(int(ground_time_s), 12 if outcome != "full_stop" else int(ground_time_s))

    east = _lon_offset(offset_east_m)
    north = _lat_offset(offset_north_m)
    for t in range(-n_before, n_after + 1):
        if t < 0:
            dtg = abs(t) * approach_speed_ms
            ideal_agl = dtg * tan_slope + gs_offset_m
            if pre_touchdown_descent_ms is not None and t >= -3:
                # Straight line from (deck + 3*descent) at t=-3 to deck at t=0.
                agl = max(0.0, -t * pre_touchdown_descent_ms)
            else:
                agl = ideal_agl
            latitude = LAT0 - _lat_offset(dtg) + north
            longitude = LON0 + _lon_offset(lateral_offset_m) + east
            altitude = deck_altitude_m + agl
            speed = approach_speed_ms
            on_ground = False
        else:
            dtg = 0.0
            latitude = LAT0 + north
            # The touchdown point sits on the centerline; the lateral offset
            # applies to the inbound segment only, so graders see a real
            # centerline deviation instead of a shifted reference frame.
            longitude = LON0 + east
            if outcome == "full_stop":
                agl = 0.0
                on_ground = True
                speed = max(5.0, td_speed - t * 3.0)
            else:  # touch_and_go / bolter: brief ground contact then climb
                if t <= 3:
                    agl = 0.0
                    on_ground = True
                    speed = td_speed
                else:
                    agl = (t - 3) * 5.0
                    on_ground = False
                    speed = td_speed + (t - 3) * 1.0
            altitude = deck_altitude_m + agl

        samples.append(
            TrackSample(
                time=float(t),
                latitude=latitude,
                longitude=longitude,
                altitude=altitude,
                agl=None,
                speed=speed,
                heading=0.0,
                aoa=None,
                on_ground=on_ground,
            )
        )
    return samples


def make_acmi_text(
    samples: list[TrackSample],
    *,
    include_carrier: bool = True,
    carrier_obj_id: str = "102",
    aircraft_obj_id: str = "101",
    aircraft_type: str = "Air+FixedWing",
    aircraft_name: str = "F/A-18C",
    pilot: str = "Viggen",
    base_time: float = 1000.0,
) -> str:
    """Render samples as an ACMI 2.2 text stream (for ingest E2E tests)."""
    lines = [
        "FileType=text/acmi/tacview",
        "FileVersion=2.2",
        "0,ReferenceTime=2024-01-01T00:00:00Z,DataSource=Test,Title=synthetic",
    ]
    if include_carrier:
        lines.append("#0")
        lines.append(
            f"{carrier_obj_id},Type=Sea+Watercraft+AircraftCarrier,Name=CV-59,"
            f"T={LON0}|{LAT0}|{DECK_ALTITUDE_M}|0|0|0"
        )
    for sample in samples:
        absolute = base_time + sample.time
        # ``#<seconds>`` is the absolute offset from ReferenceTime, not a
        # delta onto the previous frame (see app/acmi/parser.py).
        lines.append(f"#{absolute:g}")
        transform = (
            f"T={sample.longitude:g}|{sample.latitude:g}|{sample.altitude:g}|||"
            f"{sample.heading or 0:g}"
        )
        properties = [transform]
        if sample.time == samples[0].time:
            # Identity properties are emitted once, on the first update.
            properties.append(f"Type={aircraft_type}")
            properties.append(f"Name={aircraft_name}")
            properties.append(f"Pilot={pilot}")
        if sample.on_ground is not None:
            properties.append(f"OnGround={'1' if sample.on_ground else '0'}")
        if sample.speed is not None:
            properties.append(f"TAS={sample.speed:g}")
        lines.append(f"{aircraft_obj_id},{','.join(properties)}")
    return "\n".join(lines) + "\n"


def make_acmi_text_multi(
    aircraft: list[dict],
    *,
    include_carrier: bool = True,
    carrier_obj_id: str = "102",
    base_time: float = 1000.0,
) -> str:
    """Render several aircraft as ONE interleaved ACMI text stream.

    ``aircraft`` items are dicts with keys ``obj_id`` and ``samples``
    (a :class:`TrackSample` list from :func:`make_approach_samples`) plus
    optional ``name`` / ``pilot`` / ``type``. Updates from all aircraft are
    merged and sorted by absolute time so the stream interleaves both
    tracks (Issue #6 regression coverage).
    """
    lines = [
        "FileType=text/acmi/tacview",
        "FileVersion=2.2",
        "0,ReferenceTime=2024-01-01T00:00:00Z,DataSource=Test,Title=synthetic",
    ]
    if include_carrier:
        lines.append("#0")
        lines.append(
            f"{carrier_obj_id},Type=Sea+Watercraft+AircraftCarrier,Name=CV-59,"
            f"T={LON0}|{LAT0}|{DECK_ALTITUDE_M}|0|0|0"
        )

    events: list[tuple[float, int, str, TrackSample, dict]] = []
    for index, spec in enumerate(aircraft):
        for order, sample in enumerate(spec["samples"]):
            absolute = base_time + sample.time
            events.append((absolute, index, spec["obj_id"], sample, spec))
    events.sort(key=lambda e: (e[0], e[1]))

    seen_ids: set[str] = set()
    for absolute, _index, obj_id, sample, spec in events:
        # ``#<seconds>`` is the absolute offset from ReferenceTime, not a
        # delta onto the previous frame (see app/acmi/parser.py).
        lines.append(f"#{absolute:g}")
        transform = (
            f"T={sample.longitude:g}|{sample.latitude:g}|{sample.altitude:g}|||"
            f"{sample.heading or 0:g}"
        )
        properties = [transform]
        if obj_id not in seen_ids:
            # Identity properties are emitted once, on the first update.
            seen_ids.add(obj_id)
            properties.append(f"Type={spec.get('type', 'Air+FixedWing')}")
            properties.append(f"Name={spec.get('name', 'F/A-18C')}")
            properties.append(f"Pilot={spec.get('pilot', 'Viggen')}")
        if sample.on_ground is not None:
            properties.append(f"OnGround={'1' if sample.on_ground else '0'}")
        if sample.speed is not None:
            properties.append(f"TAS={sample.speed:g}")
        lines.append(f"{obj_id},{','.join(properties)}")
    return "\n".join(lines) + "\n"
