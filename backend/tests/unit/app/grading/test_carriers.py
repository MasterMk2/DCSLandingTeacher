"""Tests for per-carrier FLOLS geometry resolution and grading (Issue #3)."""

from __future__ import annotations

import logging
import math
from pathlib import Path

import pytest

from app.detection.detector import analyze_track
from app.detection.geometry import offset_position
from app.grading.carriers import (
    CarrierGeometryBook,
    FlolsGeometry,
    load_carrier_geometry_book,
)
from app.grading.config import load_grading_config
from app.grading.deviations import ApproachAnalysis, build_approach_analysis
from app.grading.lso_grader import grade_carrier_approach
from tests.conftest import GRADING_YAML
from tests.helpers import (
    DECK_ALTITUDE_M,
    LAT0,
    LON0,
    TrackSample,
    make_carrier_state,
)

REPO_ROOT = Path(__file__).resolve().parents[5]
CARRIERS_YAML = REPO_ROOT / "config" / "carriers.yaml"

CONFIG = load_grading_config(GRADING_YAML)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_resolve_known_carriers_by_name() -> None:
    book = load_carrier_geometry_book(CARRIERS_YAML)
    assert len(book) >= 3

    kuznetsov = book.resolve("Kuznetsov")
    assert kuznetsov is not None and kuznetsov.key == "kuznetsov"
    # Kuznetsov has nearly axial landing (2° offset)
    assert kuznetsov.landing_course_offset_deg == pytest.approx(2.0)

    stennis = book.resolve("CVN-74 Stennis")
    assert stennis is not None and stennis.key == "stennis"

    forrestal = book.resolve("Forrestal")
    assert forrestal is not None and forrestal.key == "forrestal"


def test_resolve_by_type_pattern_and_case_insensitivity() -> None:
    book = load_carrier_geometry_book(CARRIERS_YAML)
    by_type = book.resolve(None, "Sea+Watercraft+AircraftCarrier+Kuznetsov")
    assert by_type is not None and by_type.key == "kuznetsov"
    # Name matching must be case-insensitive.
    assert book.resolve("USS STENNIS") is not None


def test_unknown_carrier_falls_back_to_none() -> None:
    book = load_carrier_geometry_book(CARRIERS_YAML)
    assert book.resolve("Unknown CV") is None
    assert book.resolve("Charles de Gaulle", "Sea+Watercraft+AircraftCarrier") is None


def _geom(key: str) -> "FlolsGeometry":
    return FlolsGeometry(
        key=key,
        deck_altitude_m=20.0,
        ramp_along_m=100.0,
        ramp_lateral_m=0.0,
        glideslope_deg=3.5,
        landing_course_offset_deg=9.0,
    )


def test_resolve_prefers_type_over_name() -> None:
    """Issue #37: the standardized Type string must win over the free-form Name."""
    book = CarrierGeometryBook(
        {
            "alpha": (["alpha"], ["type_alpha"], _geom("alpha")),
            "bravo": (["bravo"], ["type_bravo"], _geom("bravo")),
        }
    )
    resolved = book.resolve("alpha", "type_bravo")
    assert resolved is not None and resolved.key == "bravo"
    # Name-only resolution still works when Type is absent.
    assert book.resolve("alpha", None).key == "alpha"


def test_resolve_logs_warning_on_fallback(caplog) -> None:
    book = load_carrier_geometry_book(CARRIERS_YAML)
    with caplog.at_level(logging.WARNING):
        assert book.resolve("Ghost Ship", "Sea+Watercraft+AircraftCarrier+Ghost") is None
    assert any("not in geometry book" in rec.message for rec in caplog.records)


def test_missing_config_file_yields_empty_book(tmp_path) -> None:
    book = load_carrier_geometry_book(tmp_path / "absent.yaml")
    assert len(book) == 0
    assert book.resolve("Kuznetsov") is None


def test_yaml_values_are_documented_as_estimates() -> None:
    """The shipped numbers must be flagged as estimates requiring validation."""
    text = CARRIERS_YAML.read_text(encoding="utf-8")
    assert "UNVERIFIED" in text or "estimate" in text.lower() or "validated" in text.lower()
    assert "PLACEHOLDER" in text or "community" in text.lower() or "derived" in text.lower()


# ---------------------------------------------------------------------------
# Deviation math with geometry
# ---------------------------------------------------------------------------


def _geometry(**overrides) -> FlolsGeometry:
    values = dict(
        key="stennis",
        deck_altitude_m=DECK_ALTITUDE_M,
        ramp_along_m=-130.0,
        ramp_lateral_m=12.0,
        glideslope_deg=3.5,
        landing_course_offset_deg=9.0,
    )
    values.update(overrides)
    return FlolsGeometry(**values)


def _ramp_aligned_event(
    geometry: FlolsGeometry,
    *,
    overshoot_m: float = 0.0,
    gs_offset_m: float = 0.0,
):
    """Approach flown onto the ramp along the angled-deck course."""
    course_deg = geometry.landing_course_offset_deg
    tan_slope = math.tan(math.radians(geometry.glideslope_deg))
    ramp_lat, ramp_lon = offset_position(
        LAT0, LON0, 0.0, geometry.ramp_along_m, geometry.ramp_lateral_m
    )
    td_lat, td_lon = offset_position(ramp_lat, ramp_lon, course_deg, overshoot_m, 0.0)
    speed = 70.0
    samples = []
    for t in range(-55, 26):
        if t < 0:
            dtg = abs(t) * speed + overshoot_m
            lat, lon = offset_position(td_lat, td_lon, course_deg, -dtg, 0.0)
            # Ideal slope referenced to the RAMP.
            agl = max(0.0, (dtg - overshoot_m)) * tan_slope + gs_offset_m
            altitude = geometry.deck_altitude_m + agl
            samples.append(
                TrackSample(
                    time=float(t),
                    latitude=lat,
                    longitude=lon,
                    altitude=altitude,
                    speed=speed,
                    heading=course_deg,
                    on_ground=False,
                )
            )
        else:
            samples.append(
                TrackSample(
                    time=float(t),
                    latitude=td_lat,
                    longitude=td_lon,
                    altitude=geometry.deck_altitude_m,
                    speed=max(5.0, speed - t * 3.0),
                    heading=course_deg,
                    on_ground=True,
                )
            )
    carrier = make_carrier_state(
        type_str="Sea+Watercraft+AircraftCarrier+Stennis",
        altitude=geometry.deck_altitude_m,
    )
    events = analyze_track(samples, DECK_ALTITUDE_M, {"C1": carrier})
    assert len(events) == 1
    return events[0]


def test_geometry_ideal_ramp_approach_has_zero_deviation() -> None:
    geometry = _geometry()
    event = _ramp_aligned_event(geometry)
    analysis = build_approach_analysis(event, 3.5, geometry=geometry)

    assert analysis.geometry is not None
    assert analysis.geometry["key"] == "stennis"
    assert analysis.course_deg == pytest.approx(9.0, abs=0.5)

    inbound = [s for s in analysis.samples if s.time < -10]
    assert inbound
    for sample in inbound:
        assert sample.glideslope_deviation == pytest.approx(0.0, abs=1.5)
        assert sample.centerline_deviation == pytest.approx(0.0, abs=1.5)


def test_geometry_measures_distance_to_the_ramp_not_touchdown() -> None:
    geometry = _geometry()
    event = _ramp_aligned_event(geometry, overshoot_m=80.0)

    with_geo = build_approach_analysis(event, 3.5, geometry=geometry)
    fallback = build_approach_analysis(event, 3.5)

    # The aircraft touches down ~80 m past the ramp, so every inbound
    # sample is ~80 m CLOSER to the ramp reference than to the legacy
    # touchdown-referenced approximation.
    for t in (-55.0, -30.0, -10.0):
        geo_sample = next(s for s in with_geo.samples if s.time == t)
        fb_sample = next(s for s in fallback.samples if s.time == t)
        assert geo_sample.distance_to_go == pytest.approx(fb_sample.distance_to_go - 80.0, abs=5.0)


def test_analysis_roundtrip_preserves_geometry() -> None:
    geometry = _geometry()
    event = _ramp_aligned_event(geometry)
    analysis = build_approach_analysis(event, 3.5, geometry=geometry)

    restored = ApproachAnalysis.from_dict(analysis.as_dict())
    assert restored.geometry == analysis.geometry

    result = grade_carrier_approach(restored, CONFIG)
    assert result.metrics["flols_geometry"]["key"] == "stennis"


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
        Path(__file__).resolve().parents[5] / "config" / "carriers.yaml"
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
