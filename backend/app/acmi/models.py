"""Typed data structures for parsed ACMI content.

Based on the official Tacview ACMI 2.2 documentation:
https://www.tacview.net/documentation/acmi/en/
"""

from __future__ import annotations

from dataclasses import dataclass, field


def to_float(value: str | None) -> float | None:
    """Convert an ACMI property string to float, returning None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


@dataclass
class AcmiObject:
    """A single ACMI object with its accumulated property set.

    Coordinates follow the two systems defined by the ACMI specification:

    - Spherical world: ``Longitude``/``Latitude`` in degrees plus
      ``Altitude`` in meters MSL.
    - Flat/native world: ``U``/``V`` in meters (syntax #2/#4 transforms).

    An object provides one system or the other depending on what the source
    emits; both accessors are exposed so downstream code can handle either.
    """

    obj_id: str
    properties: dict[str, str] = field(default_factory=dict)
    first_seen: float = 0.0
    last_seen: float = 0.0

    # --- identity -----------------------------------------------------------
    @property
    def type(self) -> str | None:
        return self.properties.get("Type")

    @property
    def name(self) -> str | None:
        return self.properties.get("Name")

    @property
    def pilot(self) -> str | None:
        return self.properties.get("Pilot")

    @property
    def group(self) -> str | None:
        return self.properties.get("Group")

    @property
    def country(self) -> str | None:
        return self.properties.get("Country")

    @property
    def parent(self) -> str | None:
        return self.properties.get("Parent")

    # --- spherical-world coordinates ------------------------------------------
    @property
    def latitude(self) -> float | None:
        return to_float(self.properties.get("Latitude"))

    @property
    def longitude(self) -> float | None:
        return to_float(self.properties.get("Longitude"))

    @property
    def altitude(self) -> float | None:
        return to_float(self.properties.get("Altitude"))

    # --- flat-world coordinates -------------------------------------------------
    @property
    def u(self) -> float | None:
        return to_float(self.properties.get("U"))

    @property
    def v(self) -> float | None:
        return to_float(self.properties.get("V"))

    # --- attitude -----------------------------------------------------------------
    @property
    def roll(self) -> float | None:
        return to_float(self.properties.get("Roll"))

    @property
    def pitch(self) -> float | None:
        return to_float(self.properties.get("Pitch"))

    @property
    def yaw(self) -> float | None:
        return to_float(self.properties.get("Yaw"))

    @property
    def heading(self) -> float | None:
        """True heading: ``Heading`` from a #4 transform, or the ``HDG`` property."""
        value = self.properties.get("Heading") or self.properties.get("HDG")
        return to_float(value)

    # --- motion / flight data -------------------------------------------------------
    @property
    def tas(self) -> float | None:
        return to_float(self.properties.get("TAS"))

    @property
    def cas(self) -> float | None:
        return to_float(self.properties.get("CAS"))

    @property
    def ias(self) -> float | None:
        return to_float(self.properties.get("IAS"))

    @property
    def speed(self) -> float | None:
        """Best-available speed in m/s (TAS preferred, then CAS, then IAS)."""
        return self.tas if self.tas is not None else (
            self.cas if self.cas is not None else self.ias
        )

    @property
    def aoa(self) -> float | None:
        return to_float(self.properties.get("AOA"))

    @property
    def aos(self) -> float | None:
        return to_float(self.properties.get("AOS"))

    @property
    def agl(self) -> float | None:
        return to_float(self.properties.get("AGL"))

    @property
    def on_ground(self) -> bool | None:
        value = self.properties.get("OnGround")
        if value is None:
            return None
        return to_float(value) not in (None, 0.0)

    @property
    def landing_gear(self) -> float | None:
        return to_float(self.properties.get("LandingGear"))

    @property
    def tailhook(self) -> float | None:
        return to_float(self.properties.get("Tailhook"))


# ---------------------------------------------------------------------------
# Events emitted by the parser for each significant input line.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AcmiEvent:
    """Base class for parser events."""


@dataclass(frozen=True)
class HeaderEvent(AcmiEvent):
    """A top-of-file mandatory header line.

    Only ``FileType=text/acmi/tacview`` and ``FileVersion=2.2`` take this
    form (no object id prefix).
    """

    key: str
    value: str


@dataclass(frozen=True)
class TimeEvent(AcmiEvent):
    """A ``#<offset>`` frame-time line.

    ``time`` is the absolute mission time in seconds relative to
    ``ReferenceTime``, computed by accumulating per-frame offsets as defined
    by the specification.
    """

    time: float


@dataclass(frozen=True)
class ObjectUpdateEvent(AcmiEvent):
    """An object add/update line (``<id>,Property=Value,...``).

    ``properties`` contains only the properties present on this line. For
    ``T`` (Transform) lines, the pipe-separated components are expanded into
    their canonical property names (``Longitude``, ``Latitude``, ...).
    The global object (id ``0``) carrying mission metadata such as
    ``ReferenceTime`` or ``DataSource`` is reported through this event too.
    """

    obj_id: str
    properties: dict[str, str]
    time: float


@dataclass(frozen=True)
class ObjectRemoveEvent(AcmiEvent):
    """An object removal line (``-<id>``)."""

    obj_id: str
    time: float
