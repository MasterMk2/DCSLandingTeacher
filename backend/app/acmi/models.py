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
    #: Memoized numeric parses of ``properties``. Real recordings re-read the
    #: same numeric properties on every update line (ingest + detection +
    #: track persistence), and re-parsing the raw strings each time dominated
    #: the parse budget (Issue #47). Invalidated wholesale whenever properties
    #: are merged; direct ``properties`` mutation is parser-internal only.
    _float_cache: dict[str, float | None] = field(
        default_factory=dict, repr=False, compare=False
    )

    def update_properties(self, values: dict[str, str]) -> None:
        """Merge new properties and drop the numeric parse cache."""
        self.properties.update(values)
        self._float_cache.clear()

    def _cached_float(self, key: str) -> float | None:
        cache = self._float_cache
        try:
            return cache[key]
        except KeyError:
            value = to_float(self.properties.get(key))
            cache[key] = value
            return value

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
        return self._cached_float("Latitude")

    @property
    def longitude(self) -> float | None:
        return self._cached_float("Longitude")

    @property
    def altitude(self) -> float | None:
        return self._cached_float("Altitude")

    # --- flat-world coordinates -------------------------------------------------
    @property
    def u(self) -> float | None:
        return self._cached_float("U")

    @property
    def v(self) -> float | None:
        return self._cached_float("V")

    # --- attitude -----------------------------------------------------------------
    @property
    def roll(self) -> float | None:
        return self._cached_float("Roll")

    @property
    def pitch(self) -> float | None:
        return self._cached_float("Pitch")

    @property
    def yaw(self) -> float | None:
        return self._cached_float("Yaw")

    @property
    def grid_heading(self) -> float | None:
        """Heading in the simulator's flat/native world (transform slot 9).

        This is **not** true heading. The flat world is a map projection, so
        its north differs from true north by the meridian convergence at the
        object's position. Measured against a live DCS Caucasus server:
        ``Yaw - Heading`` fits ``sin(latitude) * (longitude - 33.05)`` to
        within 0.2 deg over the whole map (2-8 deg of error, zero only on the
        projection's central meridian).

        Only meaningful together with :attr:`u` / :attr:`v`, which live in the
        same projected frame. Never mix it with latitude/longitude.
        """
        key = "Heading" if "Heading" in self.properties else "HDG"
        return self._cached_float(key)

    @property
    def heading(self) -> float | None:
        """True heading in degrees from true north.

        ``Yaw`` is referenced to true north and so is the only heading that
        matches the latitude/longitude tangent-plane geometry the detector and
        graders use. Verified against real approaches: the lat/lon ground
        track agrees with ``Yaw`` to within the crab angle, while the
        flat-world :attr:`grid_heading` agrees with the ``u``/``v`` ground
        track by the same margin.

        Falls back to :attr:`grid_heading` only when the source omits Yaw;
        that reintroduces the convergence error but beats having no heading.
        """
        yaw = self.yaw
        return yaw if yaw is not None else self.grid_heading

    # --- motion / flight data -------------------------------------------------------
    @property
    def tas(self) -> float | None:
        return self._cached_float("TAS")

    @property
    def cas(self) -> float | None:
        return self._cached_float("CAS")

    @property
    def ias(self) -> float | None:
        return self._cached_float("IAS")

    @property
    def speed(self) -> float | None:
        """Best-available speed in m/s (TAS preferred, then CAS, then IAS)."""
        return self.tas if self.tas is not None else (
            self.cas if self.cas is not None else self.ias
        )

    @property
    def aoa(self) -> float | None:
        return self._cached_float("AOA")

    @property
    def aos(self) -> float | None:
        return self._cached_float("AOS")

    @property
    def agl(self) -> float | None:
        return self._cached_float("AGL")

    @property
    def on_ground(self) -> bool | None:
        value = self._cached_float("OnGround")
        if value is None:
            return None
        return value != 0.0

    @property
    def landing_gear(self) -> float | None:
        return self._cached_float("LandingGear")

    @property
    def tailhook(self) -> float | None:
        return self._cached_float("Tailhook")


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


@dataclass(frozen=True)
class MissionEvent(AcmiEvent):
    """A mission event declared through the ``Event`` property.

    Per the specification, events are declared like properties but may be
    repeated within the same frame without overriding each other::

        Event = EventType | FirstObjectId | ... | EventText

    Known event types include ``Message``, ``Bookmark``, ``Debug``,
    ``LeftArea``, ``Destroyed``, ``TakenOff``, ``Landed`` and ``Timeout``.
    ``Landed`` / ``TakenOff`` are of particular interest for the future
    landing-detection task.
    """

    event_type: str
    object_ids: tuple[str, ...]
    text: str
    time: float
