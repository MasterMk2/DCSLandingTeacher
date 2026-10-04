import logging
import json
from pathlib import Path

import pytest

from app.grading.carriers import load_carrier_geometry_book
from app.grading.config import load_grading_config
from app.runways.seeds import resolve_seed_dir
from app.runways.provider import CACHE_VERSION, RunwayProvider
from app.runways.models import Runway
from app import configuration_files


def test_missing_settings_use_bundled_files_with_warnings(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        grading = load_grading_config(tmp_path / "grading.yaml")
        carriers = load_carrier_geometry_book(tmp_path / "carriers.yaml")
        seeds = resolve_seed_dir(tmp_path / "runways")
    assert grading.raw["version"] == 1
    assert len(carriers) > 0
    assert seeds is not None and (seeds / "runways-Caucasus.json").is_file()
    assert len(caplog.records) == 3
    assert all(record.levelno == logging.WARNING for record in caplog.records)


def test_existing_host_settings_are_used_without_warnings(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    grading_path = tmp_path / "grading.yaml"
    _ = grading_path.write_text("version: 42\n", encoding="utf-8")
    carriers_path = tmp_path / "carriers.yaml"
    _ = carriers_path.write_text("carriers: {}\n", encoding="utf-8")
    seeds = tmp_path / "runways"
    seeds.mkdir()
    with caplog.at_level(logging.WARNING):
        assert load_grading_config(grading_path).raw["version"] == 42
        assert len(load_carrier_geometry_book(carriers_path)) == 0
        assert resolve_seed_dir(seeds) == seeds
    assert not caplog.records


def test_partial_host_settings_fall_back_only_for_missing_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    grading_path = tmp_path / "grading.yaml"
    _ = grading_path.write_text("version: 42\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert load_grading_config(grading_path).raw["version"] == 42
        assert len(load_carrier_geometry_book(tmp_path / "carriers.yaml")) > 0
    assert len(caplog.records) == 1
    assert "carriers.yaml" in caplog.records[0].message


def test_missing_host_and_bundled_file_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(configuration_files, "BUNDLED_CONFIG_DIR", tmp_path / "absent")
    with pytest.raises(FileNotFoundError, match="bundled copy missing"):
        _ = load_grading_config(tmp_path / "grading.yaml")


async def test_missing_host_seed_uses_bundled_exact_before_live_cache(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    host = tmp_path / "host"
    bundled = tmp_path / "bundled"
    cache = tmp_path / "cache"
    for directory in (host, bundled, cache):
        directory.mkdir()
    exact = Runway("Exact", "01", 35.0, 140.0, 10.0, 10.0, 2000.0, 50.0)
    live = Runway("Live", "01", 35.1, 140.1, 10.0, 10.0, 2000.0, 50.0)
    filename = "runways-Caucasus.json"
    for directory, runway, is_exact in ((bundled, exact, True), (cache, live, False)):
        _ = (directory / filename).write_text(json.dumps({
            "version": CACHE_VERSION, "theatre": "Caucasus", "exact": is_exact,
            "runways": [runway.as_dict()],
        }), encoding="utf-8")
    provider = RunwayProvider(None, cache, seed_dir=host, fallback_seed_dir=bundled)
    with caplog.at_level(logging.WARNING):
        assert await provider.runways_for("Caucasus") == [exact]
        fresh = RunwayProvider(None, cache, seed_dir=host, fallback_seed_dir=bundled)
        assert await fresh.resolve(35.0, 140.0, 10.0) == exact
    assert any("using bundled copy" in record.message for record in caplog.records)
    _ = (host / filename).write_text((bundled / filename).read_text(encoding="utf-8").replace(
        '"Exact"', '"Host"',
    ), encoding="utf-8")
    caplog.clear()
    provider = RunwayProvider(None, cache, seed_dir=host, fallback_seed_dir=bundled)
    with caplog.at_level(logging.WARNING):
        runways = await provider.runways_for("Caucasus")
        assert runways is not None and runways[0].airbase == "Host"
        fresh = RunwayProvider(None, cache, seed_dir=host, fallback_seed_dir=bundled)
        resolved = await fresh.resolve(35.0, 140.0, 10.0)
        assert resolved is not None and resolved.airbase == "Host"
    assert not caplog.records
