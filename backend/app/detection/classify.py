"""Object classification from ACMI ``Type`` properties (FR-2).

Tacview type strings are hierarchical and ``+``-separated, e.g.::

    Air+FixedWing
    Sea+Watercraft+AircraftCarrier
    Ground+Static+Aircraft

The requirements name ``Carrier+...`` as the carrier marker; real DCS
exports (and our own fixtures) also use ``Sea+Watercraft+AircraftCarrier``,
so both spellings are recognized.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ObjectClass(Enum):
    AIRCRAFT = "aircraft"
    CARRIER = "carrier"
    STATIC = "static"
    OTHER = "other"


@dataclass(frozen=True)
class TypeClassifier:
    """Keyword based classifier for ACMI ``Type`` strings."""

    aircraft_keywords: tuple[str, ...] = ("Air+",)
    carrier_keywords: tuple[str, ...] = (
        "Carrier+",
        "AircraftCarrier",
    )
    static_keywords: tuple[str, ...] = ("Static",)

    def classify(self, type_str: str | None) -> ObjectClass:
        if not type_str:
            return ObjectClass.OTHER
        # Carrier check must run before the generic aircraft/static checks:
        # carriers may embed several of those keywords in their hierarchy.
        for keyword in self.carrier_keywords:
            if keyword in type_str:
                return ObjectClass.CARRIER
        for keyword in self.aircraft_keywords:
            if type_str.startswith(keyword) or keyword in type_str:
                return ObjectClass.AIRCRAFT
        for keyword in self.static_keywords:
            if keyword in type_str:
                return ObjectClass.STATIC
        return ObjectClass.OTHER


DEFAULT_CLASSIFIER = TypeClassifier()


def classify_object_type(type_str: str | None) -> ObjectClass:
    """Classify an ACMI ``Type`` property value."""
    return DEFAULT_CLASSIFIER.classify(type_str)
