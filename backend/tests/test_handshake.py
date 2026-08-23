"""Tests for the Tacview Real-Time Telemetry handshake."""

from __future__ import annotations

import asyncio

import pytest

from app.acmi.handshake import (
    HandshakeError,
    build_client_handshake,
    crc64_we,
    parse_host_handshake,
    password_hash,
    perform_client_handshake,
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
        parse_host_handshake(payload)


async def test_perform_client_handshake_roundtrip() -> None:
    received: dict[str, bytes] = {}

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"XtraLib.Stream.0\nTacview.RealTimeTelemetry.0\nTestHost\n\0")
        await writer.drain()
        data = b""
        while not data.endswith(b"\0"):
            chunk = await reader.read(1024)
            if not chunk:
                break
            data += chunk
        received["client"] = data
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        host_name = await perform_client_handshake(reader, writer, "MyClient", "")
        assert host_name == "TestHost"
        # Give the server task a moment to finish reading our reply.
        for _ in range(100):
            if "client" in received:
                break
            await asyncio.sleep(0.01)
        assert received["client"] == build_client_handshake("MyClient", "")
        writer.close()
    finally:
        server.close()
        await server.wait_closed()


async def test_perform_client_handshake_times_out_on_silence() -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Never send the host handshake; just hold the connection open.
        await asyncio.sleep(5)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        with pytest.raises(HandshakeError):
            await perform_client_handshake(reader, writer, "MyClient", "")
        writer.close()
    finally:
        server.close()
        await server.wait_closed()
