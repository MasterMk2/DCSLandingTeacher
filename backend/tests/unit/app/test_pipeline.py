"""Unit tests for carrier-geometry handling in app.pipeline."""

from pathlib import Path

import pytest

from app.detection.detector import analyze_track
from app.grading.carriers import (
    CarrierGeometryBook,
    fallback_geometry_payload,
    load_carrier_geometry_book,
)
from app.grading.config import load_grading_config
from app.pipeline import LandingPipeline
from tests.conftest import GRADING_YAML
from tests.helpers import DECK_ALTITUDE_M, make_approach_samples, make_carrier_state

REPO_ROOT = Path(__file__).resolve().parents[4]
CARRIERS_YAML = REPO_ROOT / "config" / "carriers.yaml"
CONFIG = load_grading_config(GRADING_YAML)


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
