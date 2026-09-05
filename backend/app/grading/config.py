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
        "overhead_glideslope_deg": None,
        "earth_radius_m": 6371000.0,
    },
    "approach": {
        "window_s": 60.0,
        "distance_m": 3704.0,
        "land_window_s": 300.0,
        "land_distance_m": 14816.0,
    },
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
        # 採点できた重みがこれ未満なら成績を出さない (grade/score は None)。
        "min_measured_weight": 0.5,
        "overhead_weights": {
            "descent_rate": 0.25,
            "touchdown_speed": 0.15,
            "glideslope": 0.20,
            "centerline": 0.20,
            "pattern": 0.20,
        },
        "airframe_classes": {
            # 部分一致は大文字小文字を無視するが、ハイフンの有無までは
            # 吸収しない。"OH-58" だけでは DCS の "OH58D" に当たらず、
            # カイオワが輸送機バンド (default) で採点されていた。
            "helicopter": [
                "UH-1", "AH-64", "AH-1", "UH-60", "CH-47", "KA-50", "Mi-8",
                "Mi-24", "SA342", "OH-58", "OH58",
            ],
            "fighter": [
                "F-16", "FA-18", "F/A-18", "F-15", "F-14", "F-5", "F-4",
                "F-86", "F-100", "F-117", "A-10", "AV8B", "AJS37", "Viggen",
                "JF-17", "M-2000", "Mirage", "MiG-", "Su-2", "Su-3", "J-11",
                "Tornado", "EF2000",
            ],
        },
        "descent_rate_fpm": {
            "default": {"excellent": 120, "good": 250, "fair": 450, "hard": 650},
            "fighter": {"excellent": 300, "good": 450, "fair": 650, "hard": 850},
            "helicopter": {"excellent": 100, "good": 200, "fair": 350, "hard": 550},
        },
        "touchdown_speed_ratio": {
            "slow_good": 0.88,
            "slow_fair": 0.80,
            "fast_good": 1.03,
            "fast_fair": 1.10,
        },
        "speed_flare_exclude_s": 4.0,
        "speed_reference_window_s": 10.0,
        # 機体クラス別に「測るが採点しない」項目 (rotary-wing の接地速度比と
        # グライドスロープ)。根拠の実測値は grading.yaml 側に書いてある。
        "unscored_by_class": {"helicopter": ["touchdown_speed", "glideslope"]},
        "glideslope_error_deg": {"good": 0.35, "fair": 0.70, "poor": 1.50},
        "glideslope_window_s": 30.0,
        "glideslope_min_agl_m": 15.0,
        "glideslope_min_distance_m": 200.0,
        "centerline_window_s": 5.0,
        "centerline_deviation_m": {"good": 3.0, "fair": 8.0, "poor": 20.0},
        "track_smoothing_s": 2.0,
        "track_min_step_m": 20.0,
        "rollout_align_deg": 15.0,
        "initial_align_deg": 20.0,
        "downwind_cone_deg": 60.0,
        "downwind_max_turn_rate_deg_s": 1.5,
        "stabilization_gate_agl_m": 305.0,
        "pattern": {
            "min_downwind_s": 5.0,
            "min_break_s": 6.0,
            # ダウンウィンド脚が実際に見つかったときだけオーバーヘッド扱い。
            "require_downwind": True,
            "break_altitude_spread_m": {"good": 30.0, "fair": 75.0, "poor": 180.0},
            "alignment_error_m": {"good": 100.0, "fair": 250.0, "poor": 600.0},
            "downwind_course_error_deg": {"good": 8.0, "fair": 18.0, "poor": 35.0},
            "downwind_altitude_spread_m": {"good": 45.0, "fair": 90.0, "poor": 200.0},
        },
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

    def glideslope_for(self, kind: str, approach_pattern: str | None = None) -> float:
        """Reference glidepath for this approach.

        The overhead pattern gets its own knob because its final is flown
        visually and shorter than an instrument final, and USAF ILS
        glideslopes themselves are sited anywhere from 2.5 to 3 degrees
        (AFMAN 11-217) -- so "3.0 for everything on land" is a choice, not a
        fact. Left unset it stays at ``land_glideslope_deg``.
        """
        if kind == "carrier":
            return self.carrier_glideslope_deg
        override = self.section("geometry").get("overhead_glideslope_deg")
        if approach_pattern == "overhead" and override is not None:
            return float(override)
        return self.land_glideslope_deg

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
            land_approach_window_s=float(self.section("approach")["land_window_s"]),
            land_approach_distance_m=float(self.section("approach")["land_distance_m"]),
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
