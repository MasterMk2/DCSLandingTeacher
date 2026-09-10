"""Integration tests for the Tacview Real-Time Telemetry handshake."""

from __future__ import annotations

import asyncio

import pytest

from app.acmi.handshake import HandshakeError, build_client_handshake, perform_client_handshake


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
