"""Handshake for the Tacview Real-Time Telemetry public protocol.

Based on the official documentation:
https://www.tacview.net/documentation/realtime/en/

The third-party application acting as a *client* (data receiver) must, right
after connecting, exchange a short handshake with the host (data generator,
e.g. a DCS Tacview exporter):

Host sends::

    XtraLib.Stream.0\\n
    Tacview.RealTimeTelemetry.0\\n
    Host username\\n
    \\0

Client answers::

    XtraLib.Stream.0\\n
    Tacview.RealTimeTelemetry.0\\n
    Client username\\n
    <password hash>\\0

The password hash is the CRC-64/WE of the UTF-16 (LE) password text without
the terminating ``\\0``, rendered in hexadecimal. When no password is
required the hash of the string ``"0"`` is sent.
"""

from __future__ import annotations

import asyncio

HANDSHAKE_TIMEOUT_SECONDS = 10.0

LOW_LEVEL_PROTOCOL = "XtraLib.Stream.0"
HIGH_LEVEL_PROTOCOL = "Tacview.RealTimeTelemetry.0"

_CRC64_POLY = 0x42F0E1EBA9EA3693
_CRC64_MASK = 0xFFFFFFFFFFFFFFFF


class HandshakeError(Exception):
    """Raised when the protocol handshake fails or is malformed."""


def crc64_we(data: bytes) -> int:
    """CRC-64/WE: poly 0x42F0E1EBA9EA3693, init/xorout all-ones, no reflection.

    Check value: ``crc64_we(b"123456789") == 0x6C40DF5F0B497347``.
    """
    crc = _CRC64_MASK
    for byte in data:
        crc ^= byte << 56
        for _ in range(8):
            if crc & 0x8000000000000000:
                crc = ((crc << 1) ^ _CRC64_POLY) & _CRC64_MASK
            else:
                crc = (crc << 1) & _CRC64_MASK
    return crc ^ _CRC64_MASK


def password_hash(password: str) -> str:
    """Hexadecimal CRC-64/WE of the UTF-16 (LE) password text."""
    return format(crc64_we(password.encode("utf-16-le")), "X")


def client_password_field(password: str) -> str:
    """Value of the last handshake entry for the given password.

    Per the protocol documentation example (and matching established
    implementations such as OpenRadar), an empty password is sent as the
    literal string ``"0"``. Non-empty passwords are sent as the hexadecimal
    CRC-64/WE of their UTF-16 (LE) text.
    """
    if not password:
        return "0"
    return password_hash(password)


def build_client_handshake(client_name: str, password: str) -> bytes:
    """Build the handshake packet a client must send after connecting."""
    packet = (
        f"{LOW_LEVEL_PROTOCOL}\n"
        f"{HIGH_LEVEL_PROTOCOL}\n"
        f"{client_name}\n"
        f"{client_password_field(password)}\0"
    )
    return packet.encode("utf-8")


def parse_host_handshake(payload: bytes) -> str:
    """Validate the host handshake packet and return the host username."""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HandshakeError(f"host handshake is not valid UTF-8: {exc}") from exc

    lines = text.rstrip("\0").split("\n")
    if len(lines) != 4:
        raise HandshakeError(
            f"host handshake must contain 4 entries, got {len(lines)}"
        )
    if lines[0] != LOW_LEVEL_PROTOCOL:
        raise HandshakeError(f"unsupported low-level protocol: {lines[0]!r}")
    if lines[1] != HIGH_LEVEL_PROTOCOL:
        raise HandshakeError(f"unsupported high-level protocol: {lines[1]!r}")
    return lines[2]


async def perform_client_handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    client_name: str,
    password: str,
) -> str:
    """Run the client side of the handshake; returns the host username.

    Raises :class:`HandshakeError` on protocol mismatch or premature close;
    callers should treat that like any other connection failure and retry
    with backoff.
    """
    try:
        payload = await asyncio.wait_for(
            reader.readuntil(b"\0"), timeout=HANDSHAKE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError as exc:
        raise HandshakeError("host handshake timed out") from exc
    except asyncio.IncompleteReadError as exc:
        raise HandshakeError("connection closed during host handshake") from exc
    except asyncio.LimitOverrunError as exc:
        raise HandshakeError("host handshake exceeded line limit") from exc

    host_name = parse_host_handshake(payload)

    writer.write(build_client_handshake(client_name, password))
    await writer.drain()
    return host_name
