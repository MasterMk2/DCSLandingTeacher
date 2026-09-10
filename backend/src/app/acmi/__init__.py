"""ACMI (Tacview ACMI 2.2 Text) parsing package.

The parser is a self-contained implementation based on the official Tacview
ACMI documentation (https://www.tacview.net/documentation/acmi/en/) and the
Real-Time Telemetry public protocol documentation
(https://www.tacview.net/documentation/realtime/en/).
It is intentionally decoupled from any transport (TCP) logic so it can be
tested from plain string streams.
"""

from app.acmi.file_reader import iter_acmi_lines
from app.acmi.handshake import (
    HandshakeError,
    build_client_handshake,
    crc64_we,
    parse_host_handshake,
    password_hash,
)
from app.acmi.models import (
    AcmiEvent,
    AcmiObject,
    HeaderEvent,
    MissionEvent,
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
    "HandshakeError",
    "HeaderEvent",
    "LineAssembler",
    "MissionEvent",
    "ObjectRemoveEvent",
    "ObjectUpdateEvent",
    "TimeEvent",
    "build_client_handshake",
    "crc64_we",
    "iter_acmi_lines",
    "parse_host_handshake",
    "password_hash",
]
