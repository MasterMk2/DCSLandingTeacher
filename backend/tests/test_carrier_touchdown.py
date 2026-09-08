"""Carrier touchdown detection against a deck that floats 20 m above the sea.

This is the case the detector could not see at all. Measured on the
production database 2026-09-05:

- ``on_ground`` is absent from every one of 87,780,044 track rows, so the
  weight-on-wheels test always falls through to the AGL comparison;
- Tacview's ``AGL`` is height above the TERRAIN, which over water is the sea
  surface -- an aircraft measured on the CVN_73 deck in production
  reported agl 21.99 at 22.00 m MSL;
- ``wow_agl_threshold_m`` is 3 m.

So an aircraft that traps never registered as on-deck, and the five
"carrier landings" the server had recorded were objects that hit the WATER
within 800 m of a ship: touchdown altitudes of -3.75, -2.96, -1.12, -1.03
and +2.78 m, two of them AIM-120 missiles.

The existing carrier fixtures set ``on_ground`` explicitly, which is why the
suite never saw this: they hand the detector the answer that production
never has. These build tracks the way the real data looks -- ``on_ground``
None, ``agl`` measured to the sea.
"""

from __future__ import annotations

import math

import pytest

from app.detection.detector import (
    CarrierState,
    DetectionConfig,
    TrackSample,
    analyze_track,
)

LAT0, LON0 = 35.0, 140.0
DECK_ALTITUDE_M = 19.5  # Nimitz-class, per config/carriers.yaml (`stennis`)
M_PER_DEG_LAT = 111_320.0


def _lat_offset(meters: float) -> float:
    return meters / M_PER_DEG_LAT


def carrier(altitude_m: float = 0.0) -> dict[str, CarrierState]:
    """A stationary ship whose ACMI altitude is the waterline, as DCS reports."""
    return {
        "C1": CarrierState(
            obj_id="C1",
            name="CVN_73",
            type="Sea+Watercraft+AircraftCarrier",
            samples=[(t, LAT0, LON0, altitude_m, 0.0, 0.0) for t in (-120.0, 120.0)],
        )
    }


def approach(*, final_altitude_m: float, speed_ms: float = 65.0) -> list[TrackSample]:
    """A 3.5 deg approach to the ship, levelling at ``final_altitude_m``.

    ``agl`` is filled in the way Tacview does over water: height above the
    SEA, i.e. equal to the MSL altitude. ``on_ground`` is None throughout,
    exactly as every row in the production database has it.
    """
    tan_slope = math.tan(math.radians(3.5))
    samples: list[TrackSample] = []
    for t in range(-40, 21):
        if t < 0:
            distance = abs(t) * speed_ms
            altitude = final_altitude_m + distance * tan_slope
            latitude = LAT0 - _lat_offset(distance)
        else:
            altitude = final_altitude_m
            latitude = LAT0
        samples.append(
            TrackSample(
                time=float(t),
                latitude=latitude,
                longitude=LON0,
                altitude=altitude,
                agl=altitude,  # Tacview measures to the sea, not to the deck
                speed=speed_ms if t < 0 else max(5.0, speed_ms - t * 3.0),
                heading=0.0,
                on_ground=None,
            )
        )
    return samples


def deck_altitude_for(_carrier) -> float:
    return DECK_ALTITUDE_M


def test_a_trap_is_invisible_without_a_deck_reference() -> None:
    """The bug, pinned so it cannot come back.

    With no deck resolver the aircraft is judged against the sea, sits a
    deck height "up" for the whole recording, and no landing is reported.
    """
    events = analyze_track(
        approach(final_altitude_m=DECK_ALTITUDE_M),
        None,
        carrier(),
        config=DetectionConfig(),
    )
    assert events == []


def test_a_trap_on_the_deck_is_detected() -> None:
    events = analyze_track(
        approach(final_altitude_m=DECK_ALTITUDE_M),
        None,
        carrier(),
        config=DetectionConfig(),
        deck_altitude_for=deck_altitude_for,
    )
    assert len(events) == 1
    event = events[0]
    assert event.kind == "carrier"
    assert event.outcome == "full_stop"
    assert event.touchdown.time == pytest.approx(0.0, abs=1.5)
    # The reference recorded with the touchdown is the deck, so everything
    # downstream that asks "how high above the landing surface" gets the
    # answer this whole module exists to produce.
    assert event.touchdown.surface_is_deck is True
    assert event.touchdown.ground_altitude_m == pytest.approx(DECK_ALTITUDE_M)


def test_hitting_the_water_beside_the_ship_is_not_a_trap() -> None:
    """The only thing the old detector ever caught must now be rejected.

    An object that descends to sea level, a deck height below the deck,
    is not landing
    on it; a one-sided "at or below deck height" test would still call this
    an arrestment, so the band has a floor.
    """
    events = analyze_track(
        approach(final_altitude_m=0.0),
        None,
        carrier(),
        config=DetectionConfig(),
        deck_altitude_for=deck_altitude_for,
    )
    assert events == []


def test_an_unknown_hull_detects_nothing_rather_than_guessing() -> None:
    """A ship absent from the geometry book has no known deck height.

    Inventing one would fabricate arrestments; reporting nothing is the
    honest failure, and it is what the resolver returning None must cause.
    """
    events = analyze_track(
        approach(final_altitude_m=DECK_ALTITUDE_M),
        None,
        carrier(),
        config=DetectionConfig(),
        deck_altitude_for=lambda _c: None,
    )
    assert events == []


def test_land_detection_is_untouched_by_the_deck_path() -> None:
    """No carriers in the session: the terrain path must behave exactly as
    before, including trusting Tacview's own AGL."""
    samples = approach(final_altitude_m=0.0)
    events = analyze_track(samples, 0.0, {}, config=DetectionConfig())
    assert len(events) == 1
    assert events[0].kind == "land"
    assert events[0].touchdown.surface_is_deck is False


def test_the_geometry_book_resolves_the_hull_names_dcs_actually_emits() -> None:
    """A deck height the book cannot look up is a fix that does nothing.

    The deck-referencing above is only reachable when
    ``CarrierGeometryBook.resolve`` returns geometry for the ship on the
    wire. It did not: measured in production 2026-09-06, the shipped
    ``carriers.yaml`` matched none of the six hulls in the running mission,
    including CVN_73 -- which is 100% of this server's carrier activity --
    because the patterns said "cvn-74" (hyphen) while DCS writes "CVN_73"
    (underscore), and the type patterns ("AircraftCarrier+Stennis" and the
    like) are not substrings of the "Sea+Watercraft+AircraftCarrier" that
    DCS actually emits. Every test passed over an inert change.

    So this test asserts against the identities observed in the ACMI stream,
    not against the patterns the file happens to contain.
    """
    from pathlib import Path

    from app.grading.carriers import load_carrier_geometry_book

    book = load_carrier_geometry_book(
        Path(__file__).resolve().parents[2] / "config" / "carriers.yaml"
    )
    dcs_type = "Sea+Watercraft+AircraftCarrier"
    for name in ("CVN_73", "CV_1143_5"):
        geometry = book.resolve(name, dcs_type)
        assert geometry is not None, f"{name} does not resolve; deck referencing is inert"
        assert geometry.deck_altitude_m > 0

    # ...and a hull with no entry must still resolve to nothing rather than
    # inheriting someone else's deck. A broad type pattern would break this,
    # because resolve() tries every entry's type patterns before any name.
    assert book.resolve("LHA_Tarawa", dcs_type) is None
    assert book.resolve("USS_Arleigh_Burke_IIa", "Sea+Watercraft+Warship") is None


async def test_the_ingest_gate_lets_a_deck_touchdown_through(session_factory) -> None:
    """The gate has to ask the same question the analysis does.

    ``_maybe_detect_landing`` short-circuits before ``analyze_track`` unless
    the newest sample looks like a fresh ground contact. That cheap check was
    still judging weight-on-wheels against Tacview's sea-referenced AGL, so
    an aircraft settling on a deck never looked like a contact and the
    deck-aware pass behind it never ran. Every test in this module passed
    anyway, because they all call ``analyze_track`` directly -- so this one
    drives the real ingest path, line by line, exactly as the TCP stream does.
    """
    from app.ingest import LandingContext, TrackIngestor

    seen: list[LandingContext] = []

    async def listener(context: LandingContext) -> int | None:
        seen.append(context)
        return len(seen)

    ingestor = TrackIngestor(
        session_factory,
        landing_listener=listener,
        deck_altitude_for=lambda _c: DECK_ALTITUDE_M,
    )

    lines = [
        "FileType=text/acmi/tacview",
        "FileVersion=2.2",
        "0,ReferenceTime=2024-01-01T00:00:00Z",
    ]
    lines.append("#0")
    lines.append(f"C1,T={LON0}|{LAT0}|0.0|||0.0,Type=Sea+Watercraft+AircraftCarrier,Name=CVN_73")
    for sample in approach(final_altitude_m=DECK_ALTITUDE_M):
        lines.append(f"#{sample.time - approach(final_altitude_m=0.0)[0].time:.2f}")
        lines.append(
            f"A1,T={sample.longitude}|{sample.latitude}|{sample.altitude}"
            f"||||||{sample.speed:.1f}|,"
            f"Type=Air+FixedWing,Name=FA-18C_hornet,Pilot=Trap,AGL={sample.agl}"
        )
    for line in lines:
        await ingestor.handle_line(line)
    await ingestor.close()

    assert seen, "the gate swallowed the touchdown before analyze_track ever ran"
    assert seen[-1].event.kind == "carrier"
    assert seen[-1].event.touchdown.surface_is_deck is True
    # Identity must be burned in even on this path (it is what landings.pilot
    # and landings.airframe are for).
    assert seen[-1].airframe == "FA-18C_hornet"
    assert seen[-1].pilot == "Trap"


def test_the_proximity_prefilter_does_not_change_the_answer() -> None:
    """The bounding-box skip is an optimisation, not a different test.

    ``_reference_surfaces`` used to run a haversine per ship per sample over
    the whole rolling buffer on every detection pass, including for land
    recordings that merely share a mission with a carrier group. The latitude
    window added in front of it must reject only samples the haversine would
    have rejected anyway -- so a ship right under the aircraft still deck-
    references exactly the same samples.
    """
    from app.detection.detector import _reference_surfaces

    config = DetectionConfig()
    samples = approach(final_altitude_m=DECK_ALTITUDE_M)

    near = _reference_surfaces(samples, carrier(), config, deck_altitude_for, None)
    assert any(is_deck for _, is_deck in near), "the ship is underneath; it must count"

    # Same ship, two degrees away: every sample falls back to terrain.
    distant = carrier()
    state = distant["C1"]
    state.samples = [
        (t, lat + 2.0, lon + 2.0, alt, hdg, spd) for (t, lat, lon, alt, hdg, spd) in state.samples
    ]
    far = _reference_surfaces(samples, distant, config, deck_altitude_for, None)
    assert not any(is_deck for _, is_deck in far)

    # And the invariant that actually matters: the box must never reject a
    # ship the haversine would accept. The previous version of this check
    # placed the ship at 0.9x the radius, which is 10% inside a margin worth
    # 0.19% -- it would have stayed green through a change that narrowed the
    # window almost to the radius. Sweep bearings instead, right up against
    # the edge, and compare against the haversine itself.
    import math

    from app.detection.geometry import EARTH_RADIUS_M, haversine_m

    def place(lat, lon, distance_m, bearing_deg):
        """Point `distance_m` from (lat, lon) on the sphere haversine_m uses."""
        ang = distance_m / EARTH_RADIUS_M
        br = math.radians(bearing_deg)
        p1 = math.radians(lat)
        l1 = math.radians(lon)
        p2 = math.asin(math.sin(p1) * math.cos(ang) + math.cos(p1) * math.sin(ang) * math.cos(br))
        l2 = l1 + math.atan2(
            math.sin(br) * math.sin(ang) * math.cos(p1),
            math.cos(ang) - math.sin(p1) * math.sin(p2),
        )
        return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0

    radius = config.carrier_proximity_m
    for lat in (0.0, 35.0, 42.0, 60.0, 80.0, 89.0):
        for bearing in range(0, 360, 7):
            for fraction in (0.5, 0.95, 0.999):
                ship_lat, ship_lon = place(lat, 0.0, radius * fraction, bearing)
                assert haversine_m(lat, 0.0, ship_lat, ship_lon) <= radius
                one = [
                    TrackSample(
                        time=0.0,
                        latitude=lat,
                        longitude=0.0,
                        altitude=50.0,
                        agl=50.0,
                        on_ground=None,
                    )
                ]
                state = CarrierState(
                    obj_id="C1",
                    name="CVN_73",
                    type="Sea+Watercraft+AircraftCarrier",
                    samples=[(t, ship_lat, ship_lon, 0.0, 0.0, 0.0) for t in (-9.0, 9.0)],
                )
                surface, is_deck = _reference_surfaces(
                    one, {"C1": state}, config, deck_altitude_for, None
                )[0]
                assert is_deck, (
                    f"prefilter rejected a ship {radius * fraction:.0f} m away "
                    f"on bearing {bearing} at latitude {lat}"
                )

    # Longitude wraps: a ship just across the antimeridian is metres away, not
    # 360 degrees away.
    near_dateline = [
        TrackSample(
            time=0.0,
            latitude=0.0,
            longitude=-179.9995,
            altitude=50.0,
            agl=50.0,
            on_ground=None,
        )
    ]
    across = CarrierState(
        obj_id="C1",
        name="CVN_73",
        type="Sea+Watercraft+AircraftCarrier",
        samples=[(t, 0.0, 179.9995, 0.0, 0.0, 0.0) for t in (-9.0, 9.0)],
    )
    assert haversine_m(0.0, -179.9995, 0.0, 179.9995) < config.carrier_proximity_m
    assert _reference_surfaces(near_dateline, {"C1": across}, config, deck_altitude_for, None)[0][
        1
    ], "the box must wrap longitude the way haversine_m does"
