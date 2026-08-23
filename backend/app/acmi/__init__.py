"""ACMI (Tacview ACMI 2.2 Text) parsing package.

The parser is a self-contained implementation based on the official Tacview
ACMI documentation (https://www.tacview.net/documentation/acmi/en/).
It is intentionally decoupled from any transport (TCP) logic so it can be
tested from plain string streams.
"""

from app.acmi.models import (
    AcmiEvent,
    AcmiObject,
    HeaderEvent,
    ObjectRemoveEvent,
    ObjectUpdateEvent,
    TimeEvent,
)
from app.acmi.parser import AcmiParseError, AcmiParser
from app.acmi.stream import AcmiStreamClient, LineAssembler

__all__ = [
    "AcmiEvent",
    "AcmiObject",
    "AcmiParseError",
    "AcmiParser",
    "AcmiStreamClient",
    "HeaderEvent",
    "LineAssembler",
    "ObjectRemoveEvent",
    "ObjectUpdateEvent",
    "TimeEvent",
]
