"""Case I recoveries to a moving CVN_73, rendered the way production sees them.

Everything about the wire format follows what the live server was measured
to send: Tacview's AGL is height above the SEA (the jet reads ~22 m standing
on the deck), there is no OnGround and no TAS, the ship reports its waterline
altitude and steams 15 m/s on 040, and the Hornet's reference point sits 2 m
above the deck once it is down.

The pattern is laid out in the SHIP's frame -- x forward of the ship's ACMI
position, y to starboard, metres -- because that is how a Case I is flown,
then carried along with the ship into latitude / longitude:

- initial: 3 nm astern on the ship's centreline, 800 ft, 170 m/s relative;
- break: the kiss-off at ``kissoff_x``, a LEVEL 180 to the left at a constant
  ``break_g`` -- at 3 G the 1042 m radius puts the downwind 2084 m
  (1.13 nm) abeam;
- downwind: slow to 72 m/s over the ground, descend to 600 ft;
- the 180: a descending left turn onto the angled deck's final bearing;
- groove: ``groove_s`` down the 3.5 deg glideslope to the 3-wire (the
  mission clock reads ``BASE_TIME`` where the last pass reaches it);
- trap: arrested in 2.5 s, then parked on the deck.

From the downwind on the jet holds 72 m/s over the GROUND (calm air), so
relative to the deck it closes at ~57 m/s in the groove and ~87 m/s on the
downwind -- a real pattern to a moving deck, not a pattern to a parked one.

:func:`fly_bolter_then_trap` and :func:`fly_waveoff_then_trap` add a second
circuit flown the way it is after a bolter or a wave-off: up the angled deck
to 600 ft, a level turn onto a closer downwind, no initial and no kiss-off.

The deck numbers are AIRBOSS's Nimitz-class supercarrier (the values in
``config/carriers.yaml``); the tests resolve CVN_73 through that file, so a
mismatch between the two is a failure, not a silently different frame.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, field

from app.detection.geometry import offset_position

LAT0, LON0 = 42.0, 38.0
BASE_TIME = 1000.0
G0 = 9.80665
FT = 0.3048

DECK_M = 20.15
REFERENCE_HEIGHT_M = 2.0
SLOPE_DEG = 3.5
OFFSET_DEG = -9.1359
RAMP_XY = (-162.49, 9.38)
TARGET_M = 79.0

INITIAL_X = -3.0 * 1852.0
INITIAL_REL_MS = 170.0
INITIAL_ALT_M = 800.0 * FT
DOWNWIND_ALT_M = 600.0 * FT
GROUND_SPEED_MS = 72.0
DECEL_S = 5.0
DESCENT_S = 10.0
ARREST_S = 2.5
PARK_S = 25.0
DT = 0.02
#: The second circuit (after a bolter / wave-off) flies a closer downwind, so
#: a test can tell which circuit the pattern was read from.
SECOND_DOWNWIND_Y = -1852.0


@dataclass
class Case1:
    """The ACMI text plus what the pattern was built to be (mission times)."""

    lines: list[str]
    expect: dict[str, float] = field(default_factory=dict)


@dataclass
class _Point:
    t: float
    x: float
    y: float
    alt: float
    roll: float


def _unit(bearing_deg: float) -> tuple[float, float]:
    """(x, y) unit vector for a bearing measured from +x toward +y."""
    b = math.radians(bearing_deg)
    return math.cos(b), math.sin(b)


class _Flight:
    """A ship-frame path built leg by leg, then rendered as ACMI."""

    def __init__(self, ship_speed_ms: float, ship_heading_deg: float) -> None:
        self.ship_speed_ms = ship_speed_ms
        self.ship_heading_deg = ship_heading_deg
        self.path: list[_Point] = []
        self.t = 0.0
        self.marks: dict[str, float] = {}
        self.tan_slope = math.tan(math.radians(SLOPE_DEG))
        self.fb = _unit(OFFSET_DEG)
        self.end = (RAMP_XY[0] + TARGET_M * self.fb[0], RAMP_XY[1] + TARGET_M * self.fb[1])
        self.on_deck = DECK_M + REFERENCE_HEIGHT_M
        self.groove_speed = self.rel_speed(OFFSET_DEG)

    def rel_speed(self, heading_deg: float) -> float:
        """Speed relative to the deck that keeps GROUND_SPEED_MS over the ground."""
        h = math.radians(heading_deg)
        return -self.ship_speed_ms * math.cos(h) + math.sqrt(
            GROUND_SPEED_MS**2 - (self.ship_speed_ms * math.sin(h)) ** 2
        )

    def ground_heading(self, heading_deg: float, speed: float) -> float:
        """Heading over the GROUND (relative to BRC) of a deck-relative motion."""
        h = math.radians(heading_deg)
        return math.degrees(
            math.atan2(speed * math.sin(h), speed * math.cos(h) + self.ship_speed_ms)
        )

    def add(self, x: float, y: float, alt: float, roll: float = 0.0) -> None:
        self.path.append(_Point(self.t, x, y, alt, roll))
        self.t += DT

    def along_fb(self, origin: tuple[float, float], metres: float) -> tuple[float, float]:
        return origin[0] + metres * self.fb[0], origin[1] + metres * self.fb[1]

    # -- legs -----------------------------------------------------------------
    def initial(self, kissoff_x: float) -> None:
        x = INITIAL_X
        while x < kissoff_x:
            self.add(x, 0.0, INITIAL_ALT_M)
            x += INITIAL_REL_MS * DT
        self.marks["kissoff"] = self.t

    def level_break(self, kissoff_x: float, g: float) -> float:
        """The kiss-off: a level 180 to the left. Returns the downwind's y."""
        radius = INITIAL_REL_MS**2 / (G0 * math.sqrt(g * g - 1.0))
        bank = -math.degrees(math.acos(1.0 / g))
        rate = math.degrees(INITIAL_REL_MS / radius)
        phi = 0.0
        while phi < 180.0:
            ux, uy = _unit(90.0 - phi)
            self.add(kissoff_x + radius * ux, -radius + radius * uy, INITIAL_ALT_M, bank)
            phi += rate * DT
        return -2.0 * radius

    def downwind_180_groove(
        self,
        start_x: float,
        downwind_y: float,
        start_speed: float,
        start_alt: float,
        groove_s: float,
        *,
        flare: bool = False,
        waveoff_remaining_m: float | None = None,
    ) -> tuple[float, float]:
        """Downwind, the 180 and the groove. Returns where it stops descending.

        ``waveoff_remaining_m`` ends the groove that far short of the 3-wire
        (the LSO's wave-off) instead of at the deck.
        """
        groove_length = groove_s * self.groove_speed
        groove_start = self.along_fb(self.end, -groove_length)
        groove_alt = self.on_deck + groove_length * self.tan_slope
        # Left from heading 180 to the final bearing, joining the groove line
        # at its start. The centre sits left of the final heading.
        turn = 180.0 - OFFSET_DEG
        left_at_end = _unit(OFFSET_DEG - 90.0)
        radius = (groove_start[1] - downwind_y) / (1.0 - left_at_end[1])
        centre = (
            groove_start[0] + radius * left_at_end[0],
            groove_start[1] + radius * left_at_end[1],
        )
        self.marks["downwind"] = self.t
        x = start_x
        downwind_speed = self.rel_speed(180.0)
        elapsed = 0.0
        while x > centre[0]:
            frac = min(elapsed / DESCENT_S, 1.0)
            alt = start_alt - (start_alt - DOWNWIND_ALT_M) * (1 - math.cos(math.pi * frac)) / 2
            self.add(x, downwind_y, alt)
            speed = (
                start_speed + (downwind_speed - start_speed) * elapsed / DECEL_S
                if elapsed < DECEL_S
                else downwind_speed
            )
            x -= speed * DT
            elapsed += DT
        self.marks["turn"] = self.t
        # The 180: radius-bearing = heading + 90 with heading = 180 - phi.
        phi = 0.0
        for key in ("ninety", "wake"):
            self.marks.pop(key, None)
        while phi < turn:
            heading = 180.0 - phi
            ux, uy = _unit(270.0 - phi)
            px, py = centre[0] + radius * ux, centre[1] + radius * uy
            alt = DOWNWIND_ALT_M + (groove_alt - DOWNWIND_ALT_M) * phi / turn
            speed = self.rel_speed(heading)
            bank = -math.degrees(math.atan(speed**2 / (radius * G0)))
            # NATOPS's 90 is the HEADING perpendicular to BRC, i.e. over the
            # ground (calm air), not the track relative to the moving deck.
            if "ninety" not in self.marks and self.ground_heading(heading, speed) <= 90.0:
                self.marks["ninety"] = self.t
                self.marks["ninety_alt"] = alt
            if "wake" not in self.marks and py >= 0.0:
                self.marks["wake"] = self.t
                self.marks["wake_alt"] = alt
            self.add(px, py, alt, bank)
            phi += math.degrees(speed / radius) * DT
        self.marks["rollout"] = self.t
        # Groove down the glideslope. A flared pass leaves the slope 1.5 s out
        # and sinks at 30% of it from there, so it floats past the target
        # before it reaches the deck -- the long, flat arrival an LSO calls out.
        flare_from = groove_length - 1.5 * self.groove_speed
        travelled = 0.0
        alt = groove_alt
        while True:
            remaining = groove_length - travelled
            if waveoff_remaining_m is not None and remaining <= waveoff_remaining_m:
                break
            if flare and travelled >= flare_from:
                alt -= 0.3 * self.groove_speed * self.tan_slope * DT
            else:
                alt = self.on_deck + remaining * self.tan_slope
            if alt <= self.on_deck:
                break
            self.add(*self.along_fb(groove_start, travelled), alt)
            travelled += self.groove_speed * DT
        self.marks["target"] = self.t
        return self.along_fb(groove_start, travelled)

    def bolter(self, touch: tuple[float, float]) -> tuple[float, float]:
        """A second on the deck rolling down the landing area, hook skipping."""
        travelled = 0.0
        while travelled < self.groove_speed * 1.0:
            self.add(*self.along_fb(touch, travelled), self.on_deck)
            travelled += self.groove_speed * DT
        self.marks["bolter"] = self.t
        return self.along_fb(touch, travelled)

    def climb_out(self, start: tuple[float, float], distance_m: float) -> tuple[float, float]:
        """Off the angled deck (or the waved-off groove) climbing to 600 ft."""
        alt = self.path[-1].alt
        travelled = 0.0
        while travelled < distance_m:
            alt = min(alt + 8.0 * DT, DOWNWIND_ALT_M)
            self.add(*self.along_fb(start, travelled), alt)
            travelled += self.groove_speed * DT
        return self.along_fb(start, travelled)

    def turn_downwind(self, start: tuple[float, float], downwind_y: float) -> float:
        """Level left turn at 600 ft from the final bearing onto the downwind.

        No initial, no kiss-off: the circuit after a bolter or a wave-off.
        Returns the x the downwind starts at.
        """
        speed = 75.0
        h0 = OFFSET_DEG
        left = _unit(h0 - 90.0)
        radius = (start[1] - downwind_y) / (1.0 - left[1])
        centre = (start[0] + radius * left[0], start[1] + radius * left[1])
        phi = 0.0
        while phi < 180.0 + h0:
            ux, uy = _unit(h0 - phi + 90.0)
            bank = -math.degrees(math.atan(speed**2 / (radius * G0)))
            self.add(centre[0] + radius * ux, centre[1] + radius * uy, DOWNWIND_ALT_M, bank)
            phi += math.degrees(speed / radius) * DT
        return centre[0]

    def trap(self, touch: tuple[float, float]) -> None:
        """Arrested in ARREST_S, then parked long enough to be a full stop."""
        travelled, speed = 0.0, self.groove_speed
        while speed > 0.0:
            self.add(*self.along_fb(touch, travelled), self.on_deck)
            travelled += speed * DT
            speed -= self.groove_speed / ARREST_S * DT
        stop = self.path[-1]
        while self.t < self.marks["target"] + ARREST_S + PARK_S:
            self.add(stop.x, stop.y, self.on_deck)

    def finish(self, expect: dict[str, float]) -> Case1:
        """Shift the clock so the last pass reaches the wire at BASE_TIME."""
        shift = BASE_TIME - self.marks["target"]
        for point in self.path:
            point.t += shift
        timed = {
            key: value + shift
            for key, value in expect.items()
            if key.endswith("_time")
        }
        return Case1(
            lines=_render(self.path, self.ship_speed_ms, self.ship_heading_deg),
            expect={**expect, **timed, "first_sample_time": self.path[0].t},
        )


def fly_case1(
    *,
    ship_speed_ms: float = 15.0,
    ship_heading_deg: float = 40.0,
    kissoff_x: float = 1200.0,
    break_g: float = 3.0,
    groove_s: float = 16.0,
    flare: bool = False,
) -> Case1:
    """Build the recovery. ``flare`` stops the sink over the last second."""
    flight = _Flight(ship_speed_ms, ship_heading_deg)
    flight.initial(kissoff_x)
    downwind_y = flight.level_break(kissoff_x, break_g)
    touch = flight.downwind_180_groove(
        kissoff_x, downwind_y, INITIAL_REL_MS, INITIAL_ALT_M, groove_s, flare=flare
    )
    flight.trap(touch)
    marks = flight.marks
    return flight.finish(
        {
            "kissoff_time": marks["kissoff"],
            "kissoff_x": kissoff_x,
            "break_g": break_g,
            "downwind_start_time": marks["downwind"],
            "abeam_m": -downwind_y,
            "downwind_alt_m": DOWNWIND_ALT_M,
            "turn_start_time": marks["turn"],
            "ninety_time": marks["ninety"],
            "ninety_alt_m": marks["ninety_alt"],
            "wake_time": marks["wake"],
            "wake_alt_m": marks["wake_alt"],
            "rollout_time": marks["rollout"],
            "groove_s": groove_s,
            "target_time": marks["target"],
            "groove_sink_fpm": flight.groove_speed * flight.tan_slope * 60.0 / FT,
        }
    )


def fly_bolter_then_trap(*, ship_speed_ms: float = 15.0, ship_heading_deg: float = 40.0) -> Case1:
    """Case I, a bolter, then the bolter pattern at 600 ft and a trap."""
    flight = _Flight(ship_speed_ms, ship_heading_deg)
    flight.initial(1200.0)
    downwind_y = flight.level_break(1200.0, 3.0)
    touch = flight.downwind_180_groove(1200.0, downwind_y, INITIAL_REL_MS, INITIAL_ALT_M, 16.0)
    first_touch = flight.marks["target"]
    airborne = flight.bolter(touch)
    turn_from = flight.climb_out(airborne, 1800.0)
    downwind_x = flight.turn_downwind(turn_from, SECOND_DOWNWIND_Y)
    touch = flight.downwind_180_groove(
        downwind_x, SECOND_DOWNWIND_Y, 75.0, DOWNWIND_ALT_M, 16.0
    )
    flight.trap(touch)
    return flight.finish(
        {
            "bolter_time": first_touch,
            "abeam_m": -SECOND_DOWNWIND_Y,
            "first_abeam_m": -downwind_y,
            "target_time": flight.marks["target"],
        }
    )


def fly_waveoff_then_trap(*, ship_speed_ms: float = 15.0, ship_heading_deg: float = 40.0) -> Case1:
    """Case I, waved off 300 m short of the wire, around again, and a trap.

    No deck contact separates the two passes, so the second pass's record
    reaches back over the first. Its circuit is flown with a SHORTER
    downwind than the first, so a reader that picks "the longest downwind in
    the recording" reads the waved-off pass instead of this one.
    """
    flight = _Flight(ship_speed_ms, ship_heading_deg)
    flight.initial(1200.0)
    downwind_y = flight.level_break(1200.0, 3.0)
    waveoff = flight.downwind_180_groove(
        1200.0, downwind_y, INITIAL_REL_MS, INITIAL_ALT_M, 16.0, waveoff_remaining_m=300.0
    )
    waveoff_time = flight.marks["target"]
    turn_from = flight.climb_out(waveoff, 1000.0)
    downwind_x = flight.turn_downwind(turn_from, SECOND_DOWNWIND_Y)
    touch = flight.downwind_180_groove(
        downwind_x, SECOND_DOWNWIND_Y, 75.0, DOWNWIND_ALT_M, 16.0
    )
    flight.trap(touch)
    return flight.finish(
        {
            "waveoff_time": waveoff_time,
            "abeam_m": -SECOND_DOWNWIND_Y,
            "first_abeam_m": -downwind_y,
            "target_time": flight.marks["target"],
        }
    )


def fly_straight_in(
    *,
    ship_speed_ms: float = 15.0,
    ship_heading_deg: float = 40.0,
    final_s: float = 120.0,
) -> Case1:
    """A Case III-style arrival: a 30 deg intercept, then a long straight final.

    No break, no downwind, no 180 -- so there is no Case I groove to time,
    and the last moment the track was 10 deg off the final bearing is two
    minutes out.
    """
    flight = _Flight(ship_speed_ms, ship_heading_deg)
    speed = 57.0
    length = final_s * speed
    start = flight.along_fb(flight.end, -length)
    intercept = _unit(OFFSET_DEG + 30.0)
    # 20 s on a 30 deg intercept, joining the final bearing at ``start``.
    for step in range(int(20.0 / DT)):
        back = (20.0 - step * DT) * speed
        flight.add(
            start[0] - back * intercept[0],
            start[1] - back * intercept[1],
            flight.on_deck + length * flight.tan_slope,
        )
    travelled = 0.0
    while travelled < length:
        flight.add(
            *flight.along_fb(start, travelled),
            flight.on_deck + (length - travelled) * flight.tan_slope,
        )
        travelled += speed * DT
    flight.marks["target"] = flight.t
    stop = flight.path[-1]
    while flight.t < flight.marks["target"] + PARK_S:
        flight.add(stop.x, stop.y, flight.on_deck)
    return flight.finish({"target_time": flight.marks["target"]})


def _render(path: list[_Point], ship_speed_ms: float, ship_heading_deg: float) -> list[str]:
    """ACMI text for a ship-frame path, with the ship steaming under it."""

    def ship_at(time: float) -> tuple[float, float]:
        return offset_position(
            LAT0, LON0, ship_heading_deg, ship_speed_ms * (time - BASE_TIME), 0.0
        )

    times = [p.t for p in path]

    def aircraft_at(time: float) -> tuple[float, float, float, float]:
        i = min(max(bisect_right(times, time), 1), len(path) - 1)
        a, b = path[i - 1], path[i]
        frac = (time - a.t) / (b.t - a.t) if b.t > a.t else 0.0
        x = a.x + (b.x - a.x) * frac
        y = a.y + (b.y - a.y) * frac
        alt = a.alt + (b.alt - a.alt) * frac
        roll = a.roll + (b.roll - a.roll) * frac
        ship_lat, ship_lon = ship_at(time)
        lat, lon = offset_position(ship_lat, ship_lon, ship_heading_deg, x, y)
        return lat, lon, alt, roll

    start, finish = path[0].t, path[-1].t
    frames: dict[float, list[str]] = {}
    step = 0
    ship_time = start - 1.0
    while ship_time <= finish + 1.0:
        lat, lon = ship_at(ship_time)
        identity = ",Type=Sea+Watercraft+AircraftCarrier,Name=CVN_73" if step == 0 else ""
        frames.setdefault(round(ship_time, 2), []).append(
            f"C1,T={lon:.7f}|{lat:.7f}|0.0|||{ship_heading_deg:.2f}{identity}"
        )
        ship_time += 0.5
        step += 1
    aircraft_time = start
    step = 0
    while aircraft_time <= finish:
        lat, lon, alt, roll = aircraft_at(aircraft_time)
        ahead = aircraft_at(aircraft_time + 0.2)
        dx = math.radians(ahead[1] - lon) * math.cos(math.radians(lat))
        dy = math.radians(ahead[0] - lat)
        yaw = math.degrees(math.atan2(dx, dy)) % 360.0 if (dx or dy) else 0.0
        identity = ",Type=Air+FixedWing,Name=FA-18C_hornet,Pilot=CaseOne" if step == 0 else ""
        frames.setdefault(round(aircraft_time, 2), []).append(
            f"A1,T={lon:.7f}|{lat:.7f}|{alt:.2f}|{roll:.1f}|0.0|{yaw:.1f}"
            f",AGL={alt:.2f}{identity}"
        )
        aircraft_time += 0.2
        step += 1

    lines = [
        "FileType=text/acmi/tacview",
        "FileVersion=2.2",
        "0,ReferenceTime=2026-09-01T00:00:00Z,RecordingTime=2026-09-26T00:00:00Z",
    ]
    for time in sorted(frames):
        lines.append(f"#{time:.2f}")
        lines.extend(frames[time])
    return lines
