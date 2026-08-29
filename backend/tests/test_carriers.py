"""Tests for per-carrier FLOLS geometry resolution and grading (Issue #3)."""

from __future__ import annotations

import logging
import math
from pathlib import Path

import pytest

from app.detection.detector import analyze_track
from app.grading.carriers import (
    CarrierGeometryBook,
    FlolsGeometry,
    fallback_geometry_payload,
    load_carrier_geometry_book,
)
from app.grading.config import load_grading_config
from app.grading.deviations import ApproachAnalysis, build_approach_analysis
from app.grading.lso_grader import grade_carrier_approach
from app.pipeline import LandingPipeline
from app.detection.geometry import offset_position
from tests.conftest import GRADING_YAML
from tests.helpers import (
    DECK_ALTITUDE_M,
    LAT0,
    LON0,
    TrackSample,
    make_approach_samples,
    make_carrier_state,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
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
# Detection -> carrier facts on the event
# ---------------------------------------------------------------------------


def test_landing_event_carries_carrier_facts() -> None:
    carrier = make_carrier_state(type_str="Sea+Watercraft+AircraftCarrier+Stennis")
    events = analyze_track(make_approach_samples(), DECK_ALTITUDE_M, {"C1": carrier})
    assert len(events) == 1
    event = events[0]
    assert event.carrier_type == "Sea+Watercraft+AircraftCarrier+Stennis"
    assert event.carrier_latitude == pytest.approx(LAT0)
    assert event.carrier_longitude == pytest.approx(LON0)
    assert event.carrier_altitude_m == pytest.approx(DECK_ALTITUDE_M)
    assert event.carrier_heading_deg == pytest.approx(0.0)


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
    td_lat, td_lon = offset_position(
        ramp_lat, ramp_lon, course_deg, overshoot_m, 0.0
    )
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
        assert geo_sample.distance_to_go == pytest.approx(
            fb_sample.distance_to_go - 80.0, abs=5.0
        )


def test_analysis_roundtrip_preserves_geometry() -> None:
    geometry = _geometry()
    event = _ramp_aligned_event(geometry)
    analysis = build_approach_analysis(event, 3.5, geometry=geometry)

    restored = ApproachAnalysis.from_dict(analysis.as_dict())
    assert restored.geometry == analysis.geometry

    result = grade_carrier_approach(restored, CONFIG)
    assert result.metrics["flols_geometry"]["key"] == "stennis"


# ---------------------------------------------------------------------------
# Grader metrics record which geometry was used
# ---------------------------------------------------------------------------


def _carrier_context(name: str, type_str: str | None = None):
    from app.ingest import LandingContext

    carrier = make_carrier_state(name=name, type_str=type_str)
    events = analyze_track(make_approach_samples(), DECK_ALTITUDE_M, {"C1": carrier})
    assert len(events) == 1
    return LandingContext(
        flight_id=None,
        acmi_object_id="101",
        pilot=None,
        airframe=None,
        event=events[0],
    )


def test_pipeline_metrics_record_resolved_geometry() -> None:
    book = load_carrier_geometry_book(CARRIERS_YAML)
    pipeline = LandingPipeline(None, CONFIG, carrier_geometry_book=book)
    context = _carrier_context("Stennis", "Sea+Watercraft+AircraftCarrier")

    analysis, result, _score = pipeline._grade(context)  # noqa: SLF001

    assert analysis.geometry is not None
    payload = result.metrics["flols_geometry"]
    assert payload["key"] == "stennis"
    assert payload["source"] == "carriers.yaml"
    # Updated deck altitude based on community research (~64 ft = 19.5m)
    assert payload["deck_altitude_m"] == pytest.approx(19.5)


def test_pipeline_metrics_record_fallback_for_unknown_carrier() -> None:
    book = load_carrier_geometry_book(CARRIERS_YAML)
    pipeline = LandingPipeline(None, CONFIG, carrier_geometry_book=book)
    context = _carrier_context("Mystery CV", "Sea+Watercraft+AircraftCarrier")

    analysis, result, _score = pipeline._grade(context)  # noqa: SLF001

    assert analysis.geometry is None
    assert result.metrics["flols_geometry"] == fallback_geometry_payload()


def test_empty_book_behaves_like_legacy_approximation() -> None:
    pipeline = LandingPipeline(None, CONFIG, carrier_geometry_book=CarrierGeometryBook({}))
    context = _carrier_context("Stennis")

    analysis, result, _score = pipeline._grade(context)  # noqa: SLF001

    assert analysis.geometry is None
    assert result.metrics["flols_geometry"]["source"] == "touchdown_reference_fallback"
