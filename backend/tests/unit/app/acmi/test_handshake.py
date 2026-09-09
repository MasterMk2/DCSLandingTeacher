"""Tests for the Tacview Real-Time Telemetry handshake."""

from __future__ import annotations

import pytest

from app.acmi.handshake import (
    HandshakeError,
    build_client_handshake,
    crc64_we,
    parse_host_handshake,
    password_hash,
)


def test_crc64_we_check_value() -> None:
    # Official check value for CRC-64/WE from the reveng CRC catalogue
    # (poly 0x42F0E1EBA9EA3693, init/xorout all-ones, no reflection).
    assert crc64_we(b"123456789") == 0x62EC59E3F1A4F00A


def test_password_hash_is_hex_of_utf16_crc() -> None:
    expected = format(crc64_we("secret".encode("utf-16-le")), "X")
    assert password_hash("secret") == expected
    # No leading zeros / lowercase normalization: uppercase hex, no prefix.
    assert not password_hash("x").startswith("0x")


def test_build_client_handshake_structure_no_password() -> None:
    packet = build_client_handshake("MyClient", "").decode()
    lines = packet.split("\n")
    assert lines[0] == "XtraLib.Stream.0"
    assert lines[1] == "Tacview.RealTimeTelemetry.0"
    assert lines[2] == "MyClient"
    # Without a password the literal "0" is sent (per the documentation
    # example and established implementations), NUL-terminated.
    assert lines[3] == "0\0"


def test_build_client_handshake_structure_with_password() -> None:
    packet = build_client_handshake("MyClient", "secret").decode()
    last_entry = packet.split("\n")[3]
    assert last_entry.endswith("\0")
    assert last_entry[:-1] == password_hash("secret")


def test_parse_host_handshake_valid() -> None:
    payload = b"XtraLib.Stream.0\nTacview.RealTimeTelemetry.0\nDCS Host\n\0"
    assert parse_host_handshake(payload) == "DCS Host"


@pytest.mark.parametrize(
    "payload",
    [
        b"OtherLib.Stream.0\nTacview.RealTimeTelemetry.0\nhost\n\0",
        b"XtraLib.Stream.0\nTacview.SomethingElse.0\nhost\n\0",
        b"XtraLib.Stream.0\nTacview.RealTimeTelemetry.0\n\0",  # too few entries
    ],
)
def test_parse_host_handshake_rejects_mismatch(payload: bytes) -> None:
    with pytest.raises(HandshakeError):
        _ = parse_host_handshake(payload)
