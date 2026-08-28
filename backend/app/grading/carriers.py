"""Per-carrier FLOLS geometry: loading and resolution (Issue #3 / O-2).

``config/carriers.yaml`` declares, for each known DCS carrier, the deck
height above sea level, the ramp (FLOLS datum) position relative to the
ship's ACMI position, the glideslope angle and the landing-course offset.

All numeric values shipped in the YAML are UNVERIFIED ESTIMATES from
public descriptions; see the comments there. Resolution falls back to
``None`` for unknown ships, which makes the graders use the legacy
touchdown-referenced approximation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CARRIERS_PATH = Path("config") / "carriers.yaml"

_FALLBACK_GEOMETRY_SOURCE = "touchdown_reference_fallback"


@dataclass(frozen=True)
class FlolsGeometry:
    """FLOLS geometry of one carrier class (Issue #3).

    Coordinates follow the conventions documented in
    ``config/carriers.yaml``:

    - ``deck_altitude_m``          : landing-area deck height above sea level.
    - ``ramp_along_m``             : meters forward (+ bow) of the ship's
      ACMI position.
    - ``ramp_lateral_m``           : meters right (+) of the ship heading.
    - ``glideslope_deg``           : FLOLS glideslope angle.
    - ``landing_course_offset_deg``: landing course bearing relative to the
      ship heading.
    - ``beam_width_m``             : informational FLOLS beam width.
    - ``validated``                : whether geometry was validated with real data.
    """

    key: str
    deck_altitude_m: float
    ramp_along_m: float
    ramp_lateral_m: float
    glideslope_deg: float
    landing_course_offset_deg: float
    beam_width_m: float | None = None
    validated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "source": "carriers.yaml",
            "validated": self.validated,
            "deck_altitude_m": self.deck_altitude_m,
            "ramp_along_m": self.ramp_along_m,
            "ramp_lateral_m": self.ramp_lateral_m,
            "glideslope_deg": self.glideslope_deg,
            "landing_course_offset_deg": self.landing_course_offset_deg,
            "beam_width_m": self.beam_width_m,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FlolsGeometry":
        return cls(
            key=str(data["key"]),
            deck_altitude_m=float(data["deck_altitude_m"]),
            ramp_along_m=float(data["ramp_along_m"]),
            ramp_lateral_m=float(data["ramp_lateral_m"]),
            glideslope_deg=float(data["glideslope_deg"]),
            landing_course_offset_deg=float(data["landing_course_offset_deg"]),
            beam_width_m=(
                float(data["beam_width_m"])
                if data.get("beam_width_m") is not None
                else None
            ),
            validated=bool(data.get("validated", False)),
        )


def fallback_geometry_payload() -> dict[str, Any]:
    """Metrics payload recorded when no per-carrier geometry matched."""
    return {"source": _FALLBACK_GEOMETRY_SOURCE}


def _matches(patterns: list[str], value: str | None) -> bool:
    if not value:
        return False
    lowered = value.lower()
    return any(pattern.lower() in lowered for pattern in patterns)


class CarrierGeometryBook:
    """Resolves carrier name/type pairs to :class:`FlolsGeometry`."""

    def __init__(self, entries: dict[str, FlolsGeometry]) -> None:
        # Keep declaration order so the first match wins.
        self._entries = entries

    def resolve(
        self, name: str | None, type_str: str | None = None
    ) -> FlolsGeometry | None:
        """Find the geometry for a ship; ``None`` when unknown (fallback).

        DCS ``Type`` strings are standardized, so they are tried before the
        free-form ``Name`` field (Issue #37). When nothing matches we fall back
        to the touchdown-referenced approximation and log it so the operator
        knows the grade is approximate.
        """
        for _patterns_name, patterns_type, geometry in self._entries.values():
            if _matches(patterns_type, type_str):
                return geometry
        for patterns_name, _patterns_type, geometry in self._entries.values():
            if _matches(patterns_name, name):
                return geometry
        logger.warning(
            "Carrier %r (%r) not in geometry book; using fallback approximation",
            name, type_str,
        )
        return None

    def __len__(self) -> int:
        return len(self._entries)


def load_carrier_geometry_book(path: str | Path | None = None) -> CarrierGeometryBook:
    """Load ``carriers.yaml``; an empty book when the file is absent."""
    target = Path(path) if path is not None else DEFAULT_CARRIERS_PATH
    if not target.is_file():
        return CarrierGeometryBook({})
    with open(target, encoding="utf-8") as stream:
        data = yaml.safe_load(stream) or {}
    if not isinstance(data, dict):
        raise ValueError(f"carrier config must be a mapping: {target}")

    defaults = data.get("defaults") or {}
    entries: dict[str, tuple[list[str], list[str], FlolsGeometry]] = {}
    for key, raw in (data.get("carriers") or {}).items():
        if not isinstance(raw, dict):
            continue
        geometry = FlolsGeometry(
            key=str(key),
            deck_altitude_m=float(raw["deck_altitude_m"]),
            ramp_along_m=float(raw["ramp_along_m"]),
            ramp_lateral_m=float(raw["ramp_lateral_m"]),
            glideslope_deg=float(raw.get("glideslope_deg", defaults.get("glideslope_deg", 3.5))),
            landing_course_offset_deg=float(
                raw.get("landing_course_offset_deg", defaults.get("landing_course_offset_deg", 9.0))
            ),
            beam_width_m=(
                float(raw["beam_width_m"])
                if raw.get("beam_width_m") is not None
                else (
                    float(defaults["beam_width_m"])
                    if defaults.get("beam_width_m") is not None
                    else None
                )
            ),
            validated=bool(raw.get("validated", False)),
        )
        entries[str(key)] = (
            [str(p) for p in raw.get("name_patterns", [])],
            [str(p) for p in raw.get("type_patterns", [])],
            geometry,
        )
    return CarrierGeometryBook(entries)
