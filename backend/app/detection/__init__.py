"""Landing / carrier-arrestment event detection (FR-2)."""

from __future__ import annotations

from app.detection.classify import ObjectClass, classify_object_type
from app.detection.detector import (
    DetectionConfig,
    LandingEvent,
    Touchdown,
    analyze_track,
)
from app.detection.geometry import (
    EARTH_RADIUS_M,
    haversine_m,
    to_local_xy,
    transform_to_frame,
)

__all__ = [
    "DetectionConfig",
    "EARTH_RADIUS_M",
    "LandingEvent",
    "ObjectClass",
    "Touchdown",
    "analyze_track",
    "classify_object_type",
    "haversine_m",
    "to_local_xy",
    "transform_to_frame",
]
