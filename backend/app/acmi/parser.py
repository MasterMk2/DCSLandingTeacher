"""Self-contained parser for Tacview ACMI 2.2 Text format.

Implements the specification published at
https://www.tacview.net/documentation/acmi/en/

Line kinds handled:

- Mandatory header: ``FileType=text/acmi/tacview`` / ``FileVersion=2.2``
- Frame time:       ``#<seconds>`` (offset accumulated onto current time)
- Object update:    ``<id>,Property=Value,...`` (id in hexadecimal)
- Object removal:   ``-<id>``
- Mission events:   ``Event=Type|Id|...|Text`` property (repeatable per frame)
- Comments:         ``// ...`` (ignored)

The special ``T`` (Transform) property supports all four documented position
syntaxes and is expanded into canonical property names so consumers can work
with typed accessors regardless of which notation the source uses:

- ``T = Longitude | Latitude | Altitude``
- ``T = Longitude | Latitude | Altitude | U | V``
- ``T = Longitude | Latitude | Altitude | Roll | Pitch | Yaw``
- ``T = Longitude | Latitude | Altitude | Roll | Pitch | Yaw | U | V | Heading``

Omitted (empty) transform components keep their previous values, as per the
specification. Text values may escape commas as ``\\,``.

The parser is transport-independent: it consumes one line at a time and can
therefore be tested from plain string streams without any network.
"""

from __future__ import annotations

from app.acmi.models import (
    AcmiEvent,
    AcmiObject,
    HeaderEvent,
    MissionEvent,
    ObjectRemoveEvent,
    ObjectUpdateEvent,
    TimeEvent,
)

FILE_TYPE_KEY = "FileType"
FILE_VERSION_KEY = "FileVersion"

EVENT_KEY = "Event"

#: Canonical property names for the 9 transform slots, in documented order.
_TRANSFORM_FIELDS = (
    "Longitude",
    "Latitude",
    "Altitude",
    "Roll",
    "Pitch",
    "Yaw",
    "U",
    "V",
    "Heading",
)


class AcmiParseError(ValueError):
    """Raised when a line cannot be interpreted as valid ACMI text."""


def split_unescaped(text: str, separator: str) -> list[str]:
    """Split on ``separator``, honoring backslash escapes (``\\,`` etc.).

    Escape sequences are resolved in the returned parts (e.g. ``\\,``
    becomes ``,``), matching how Tacview escapes delimiters inside values.
    """
    parts: list[str] = []
    buffer: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\" and i + 1 < len(text):
            buffer.append(text[i + 1])
            i += 2
            continue
        if char == separator:
            parts.append("".join(buffer))
            buffer = []
            i += 1
            continue
        buffer.append(char)
        i += 1
    parts.append("".join(buffer))
    return parts


def expand_transform(value: str) -> dict[str, str]:
    """Expand a ``T=`` transform value into canonical property names.

    Empty components mean "unchanged since last update" and are omitted from
    the result so they do not overwrite previous values.
    """
    components = value.split("|")
    expanded: dict[str, str] = {}
    for name, component in zip(_TRANSFORM_FIELDS, components):
        component = component.strip()
        if component != "":
            expanded[name] = component
    return expanded


def parse_event_value(value: str) -> tuple[str, tuple[str, ...], str]:
    """Parse an ``Event`` property value into ``(type, object_ids, text)``.

    Structure per the specification:
    ``Event = EventType | FirstObjectId | ... | EventText``
    The event text is always the last element.
    """
    parts = split_unescaped(value, "|")
    event_type = parts[0].strip()
    if len(parts) == 1:
        return event_type, (), ""
    return event_type, tuple(part.strip() for part in parts[1:-1]), parts[-1]


def parse_properties(
    properties_text: str,
) -> tuple[dict[str, str], list[tuple[str, tuple[str, ...], str]]]:
    """Parse the comma-separated ``Property=Value`` part of an object line.

    Returns the plain properties plus any mission events found on the line.
    Events are kept separate because several may be declared in the same
    frame without overriding each other.
    """
    properties: dict[str, str] = {}
    events: list[tuple[str, tuple[str, ...], str]] = []
    for pair in split_unescaped(properties_text, ","):
        if not pair:
            continue
        key, sep, value = pair.partition("=")
        if not sep:
            # Not a valid property assignment; skip rather than abort the
            # whole stream (real-time telemetry should be resilient).
            continue
        stripped_key = key.strip()
        if stripped_key == EVENT_KEY:
            events.append(parse_event_value(value))
        else:
            properties[stripped_key] = value
    return properties, events


def normalize_object_id(raw_id: str) -> str:
    """Normalize a hexadecimal object id (no prefix, no leading zeros)."""
    return raw_id.strip().upper()


class AcmiParser:
    """Stateful incremental parser for an ACMI text stream."""

    def __init__(self) -> None:
        self.time: float = 0.0
        #: Mission metadata from the global object (id ``0``):
        #: ReferenceTime, DataSource, DataRecorder, Author, Title, ...
        self.header: dict[str, str] = {}
        #: Live objects keyed by normalized hexadecimal id.
        self.objects: dict[str, AcmiObject] = {}
        self._first_line = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def feed_line(self, raw_line: str) -> list[AcmiEvent]:
        """Consume one line and return every event it produced.

        A single line may yield multiple events (e.g. an object update that
        also declares one or more ``Event`` properties). Returns an empty
        list for blank/comment lines.

        Raises :class:`AcmiParseError` for structurally invalid lines
        (malformed frame time, unknown header, unparsable object id).
        """
        line = raw_line.rstrip("\r\n")
        if self._first_line:
            # The specification recommends prefixing text data with a UTF-8 BOM.
            line = line.lstrip("\ufeff")
            self._first_line = False

        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            return []

        if stripped.startswith("#"):
            return [self._handle_time(stripped)]

        first, sep, rest = line.partition(",")

        # Removal lines ("-<id>") may appear without any trailing comma.
        if first.startswith("-"):
            return [self._handle_remove(first, rest if sep else "")]

        if not sep:
            return [self._handle_headerless_line(stripped)]

        return self._handle_update(first, rest)

    def feed(self, text: str) -> list[AcmiEvent]:
        """Convenience helper: parse a multi-line chunk of ACMI text."""
        events: list[AcmiEvent] = []
        for line in text.splitlines():
            events.extend(self.feed_line(line))
        return events

    # ------------------------------------------------------------------
    # Line handlers
    # ------------------------------------------------------------------

    def _handle_time(self, stripped: str) -> TimeEvent:
        try:
            offset = float(stripped[1:])
        except ValueError as exc:
            raise AcmiParseError(f"invalid frame time line: {stripped!r}") from exc
        self.time += offset
        return TimeEvent(time=self.time)

    def _handle_headerless_line(self, stripped: str) -> AcmiEvent:
        key, eq, value = stripped.partition("=")
        if eq and key in (FILE_TYPE_KEY, FILE_VERSION_KEY):
            self.header[key] = value
            return HeaderEvent(key=key, value=value)
        raise AcmiParseError(f"unrecognized line: {stripped!r}")

    def _handle_remove(self, first: str, rest: str) -> ObjectRemoveEvent:
        obj_id = normalize_object_id(first[1:])
        if not obj_id:
            raise AcmiParseError(f"missing object id in removal line: {first!r}")
        self.objects.pop(obj_id, None)
        return ObjectRemoveEvent(obj_id=obj_id, time=self.time)

    def _handle_update(self, first: str, rest: str) -> list[AcmiEvent]:
        obj_id = normalize_object_id(first)
        if not obj_id:
            raise AcmiParseError(f"missing object id in update line: {first!r}")

        properties, mission_events = parse_properties(rest)
        if "T" in properties:
            transform = expand_transform(properties.pop("T"))
            properties.update(transform)

        if obj_id == "0":
            # Global object doubles as mission metadata container.
            self.header.update(properties)

        obj = self.objects.get(obj_id)
        if obj is None:
            obj = AcmiObject(obj_id=obj_id, first_seen=self.time)
            self.objects[obj_id] = obj
        obj.properties.update(properties)
        obj.last_seen = self.time

        events: list[AcmiEvent] = [
            ObjectUpdateEvent(obj_id=obj_id, properties=dict(properties), time=self.time)
        ]
        for event_type, object_ids, text in mission_events:
            events.append(
                MissionEvent(
                    event_type=event_type,
                    object_ids=object_ids,
                    text=text,
                    time=self.time,
                )
            )
        return events
