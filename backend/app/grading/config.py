"""Loading and validation of ``config/grading.yaml``.

All grading thresholds live in the YAML file so they can be tuned without
code changes (FR-3 / NFR-6). The loader returns a thin typed wrapper; missing
keys fall back to the same defaults documented in the YAML file itself.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("config") / "grading.yaml"

_DEFAULTS: dict[str, Any] = {
    "version": 1,
    "geometry": {
        "carrier_glideslope_deg": 3.5,
        "land_glideslope_deg": 3.0,
        "earth_radius_m": 6371000.0,
    },
    "approach": {"window_s": 60.0, "distance_m": 3704.0},
    "detection": {
        "wow_agl_threshold_m": 3.0,
        "max_touchdown_descent_ms": 8.0,
        "full_stop_dwell_s": 15.0,
        "touch_and_go_max_dwell_s": 45.0,
        "climb_out_vertical_ms": 1.5,
        "carrier_proximity_m": 800.0,
        "sample_buffer_s": 600.0,
    },
    "land_grading": {
        "weights": {
            "descent_rate": 0.30,
            "touchdown_speed": 0.20,
            "glideslope": 0.25,
            "centerline": 0.25,
        },
        "descent_rate_fpm": {"excellent": 120, "good": 250, "fair": 450, "hard": 650},
        "touchdown_speed_ratio": {"good_band": 0.05, "fair_band": 0.12},
        "glideslope_deviation_m": {"good": 1.0, "fair": 2.5, "poor": 5.0},
        "centerline_deviation_m": {"good": 3.0, "fair": 8.0, "poor": 20.0},
        "letters": {"A": 90, "B": 78, "C": 62, "D": 45},
    },
    "lso_grading": {
        "grades": {
            "ok": "OK",
            "ok_minus": "OK-",
            "ok_paren": "(OK)",
            "no_grade": "_NO_GRADE_",
            "cut": "CUT",
        },
        "at_ramp_window_s": 3.0,
        "factors": {},
        "decision": {
            "cut_if_severe_low": True,
            "cut_if_major_count": 3,
            "ok_paren_major_count": 2,
        },
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class GradingConfig:
    """Typed access wrapper around the parsed YAML document."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = _deep_merge(_DEFAULTS, data)

    # -- generic access -----------------------------------------------------
    @property
    def raw(self) -> dict[str, Any]:
        return self._data

    def section(self, name: str) -> dict[str, Any]:
        value = self._data.get(name, {})
        return value if isinstance(value, dict) else {}

    # -- geometry -------------------------------------------------------------
    @property
    def carrier_glideslope_deg(self) -> float:
        return float(self.section("geometry")["carrier_glideslope_deg"])

    @property
    def land_glideslope_deg(self) -> float:
        return float(self.section("geometry")["land_glideslope_deg"])

    def glideslope_for(self, kind: str) -> float:
        return self.carrier_glideslope_deg if kind == "carrier" else self.land_glideslope_deg

    # -- detection ------------------------------------------------------------
    @property
    def detection(self) -> dict[str, Any]:
        return self.section("detection")

    def to_detection_config(self):
        from app.detection.detector import DetectionConfig

        d = self.detection
        return DetectionConfig(
            wow_agl_threshold_m=float(d["wow_agl_threshold_m"]),
            max_touchdown_descent_ms=float(d["max_touchdown_descent_ms"]),
            full_stop_dwell_s=float(d["full_stop_dwell_s"]),
            touch_and_go_max_dwell_s=float(d["touch_and_go_max_dwell_s"]),
            climb_out_vertical_ms=float(d["climb_out_vertical_ms"]),
            carrier_proximity_m=float(d["carrier_proximity_m"]),
            approach_window_s=float(self.section("approach")["window_s"]),
            approach_distance_m=float(self.section("approach")["distance_m"]),
        )

    # -- graders ----------------------------------------------------------------
    @property
    def land_grading(self) -> dict[str, Any]:
        return self.section("land_grading")

    @property
    def lso_grading(self) -> dict[str, Any]:
        return self.section("lso_grading")


def apply_config_overrides(config: GradingConfig, overrides: dict[str, Any]) -> GradingConfig:
    """Return a config copy with nested overrides applied (regrade support)."""
    return GradingConfig(_deep_merge(config.raw, overrides))


def load_grading_config(path: str | Path | None = None) -> GradingConfig:
    """Load the grading configuration; falls back to defaults when absent."""
    target = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not target.is_file():
        return GradingConfig({})
    with open(target, encoding="utf-8") as stream:
        data = yaml.safe_load(stream) or {}
    if not isinstance(data, dict):
        raise ValueError(f"grading config must be a mapping: {target}")
    return GradingConfig(data)
