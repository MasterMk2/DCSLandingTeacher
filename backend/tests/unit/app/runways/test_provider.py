"""Tests for cached runway geometry provisioning."""

import json
from pathlib import Path

from app.runways.provider import CACHE_VERSION, DEFAULT_CACHE_DIR, RunwayProvider


def test_default_cache_directory_is_the_docker_image_cache_directory() -> None:
    """The default runway cache uses the Docker image's cache directory."""
    provider = RunwayProvider(None)

    assert provider._cache_path("Caucasus") == Path("/data/cache/runways-Caucasus.json")
    assert DEFAULT_CACHE_DIR == Path("/data/cache")


def test_a_stale_runway_cache_is_ignored(tmp_path) -> None:
    """v1 caches hold grid-framed geometry and must be re-swept, not served."""
    provider = RunwayProvider(None, tmp_path)
    path = tmp_path / "runways-Caucasus.json"
    payload = {
        "theatre": "Caucasus",
        "runways": [
            {
                "airbase": "Sochi",
                "name": "06",
                "threshold_lat": 43.44,
                "threshold_lon": 39.93,
                "elevation_m": 10.0,
                "heading_deg": 62.0,
                "length_m": 2500.0,
                "width_m": 60.0,
            }
        ],
    }
    path.write_text(json.dumps({"version": 1, **payload}), encoding="utf-8")
    assert provider._load_cache("Caucasus") is None

    path.write_text(json.dumps({"version": CACHE_VERSION, **payload}), encoding="utf-8")
    assert provider._load_cache("Caucasus") is not None
