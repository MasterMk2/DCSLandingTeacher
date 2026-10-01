"""Tests for line assembly and the TCP stream client (with handshake)."""

from __future__ import annotations

import asyncio
import gzip

from app.acmi.handshake import build_client_handshake
from app.acmi.stream import (
    AcmiStreamClient,
)

ACMI_TEXT = (
    "FileType=text/acmi/tacview\nFileVersion=2.2\n#1.50\n101,T=41.6|41.5|100,Type=Air+FixedWing\n"
)

# ---------------------------------------------------------------------------
# TCP client integration tests against a local asyncio server
# ---------------------------------------------------------------------------


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("condition not met in time")
        await asyncio.sleep(0.01)


async def _host_handshake_writer(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    host_name: str = "TestHost",
) -> bytes:
    """Send the host handshake and return the client's handshake reply."""
    writer.write(f"XtraLib.Stream.0\nTacview.RealTimeTelemetry.0\n{host_name}\n\0".encode())
    await writer.drain()
    # The client handshake ends with a terminal NUL byte.
    data = b""
    try:
        while not data.endswith(b"\0"):
            chunk = await reader.read(1024)
            if not chunk:
                break
            data += chunk
    except (ConnectionError, OSError):
        pass
    return data


async def test_stream_client_handshakes_and_receives_lines() -> None:
    lines_received: list[str] = []
    client_reply: dict[str, bytes] = {}

    async def on_line(line: str) -> None:
        lines_received.append(line)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client_reply["handshake"] = await _host_handshake_writer(reader, writer)
        writer.write(b"FileType=text/acmi/tacview\n")
        await writer.drain()
        await asyncio.sleep(0.05)
        # Split a line across two writes to exercise chunk reassembly.
        writer.write(b"FileVersion=")
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.write(b"2.2\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    client = AcmiStreamClient(
        "127.0.0.1",
        port,
        on_line,
        client_name="TestClient",
        initial_delay=0.05,
        max_delay=0.1,
    )
    task = asyncio.create_task(client.run())
    try:
        await _wait_until(lambda: len(lines_received) >= 2)
        assert lines_received[0] == "FileType=text/acmi/tacview"
        assert lines_received[1] == "FileVersion=2.2"
        # The client must have answered with a well-formed handshake.
        expected_tail = build_client_handshake("TestClient", "")
        assert client_reply["handshake"].endswith(expected_tail.split(b"\n", 2)[2])
    finally:
        await client.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        server.close()
        await server.wait_closed()


async def test_stream_client_reconnects_after_disconnect() -> None:
    accept_count = 0

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal accept_count
        accept_count += 1
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def on_line(line: str) -> None:  # pragma: no cover - no data sent
        pass

    client = AcmiStreamClient("127.0.0.1", port, on_line, initial_delay=0.05, max_delay=0.1)
    task = asyncio.create_task(client.run())
    try:
        # The server closes each connection before the handshake completes;
        # the client must treat that as a failure and reconnect (backoff).
        await _wait_until(lambda: accept_count >= 2)
        assert accept_count >= 2
    finally:
        await client.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        server.close()
        await server.wait_closed()


async def test_stream_client_receives_compressed_lines() -> None:
    lines_received: list[str] = []

    async def on_line(line: str) -> None:
        lines_received.append(line)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _host_handshake_writer(reader, writer)
        writer.write(gzip.compress(ACMI_TEXT.encode()))
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    client = AcmiStreamClient("127.0.0.1", port, on_line, initial_delay=0.05, max_delay=0.1)
    task = asyncio.create_task(client.run())
    try:
        await _wait_until(lambda: len(lines_received) >= 4)
        assert lines_received[0] == "FileType=text/acmi/tacview"
        assert lines_received[-1].startswith("101,T=")
    finally:
        await client.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        server.close()
        await server.wait_closed()


async def test_stream_client_recovers_from_corrupt_compression() -> None:
    """A corrupted compressed stream must trigger reconnect and recovery."""
    accept_count = 0
    lines_received: list[str] = []

    async def on_line(line: str) -> None:
        lines_received.append(line)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal accept_count
        accept_count += 1
        await _host_handshake_writer(reader, writer)
        if accept_count == 1:
            # Invalid gzip payload: magic header followed by garbage.
            writer.write(b"\x1f\x8b" + b"\xff" * 64)
            await writer.drain()
            await asyncio.sleep(0.05)
            writer.close()
        else:
            writer.write(b"FileVersion=2.2\n")
            await writer.drain()
            await asyncio.sleep(0.05)
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    client = AcmiStreamClient("127.0.0.1", port, on_line, initial_delay=0.05, max_delay=0.1)
    task = asyncio.create_task(client.run())
    try:
        await _wait_until(lambda: "FileVersion=2.2" in lines_received)
        assert accept_count >= 2
    finally:
        await client.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        server.close()
        await server.wait_closed()


async def test_stream_client_stop_wakes_pending_backoff() -> None:
    """stop() during a backoff sleep must end the run loop promptly."""

    async def on_line(line: str) -> None:  # pragma: no cover - no data sent
        pass

    client = AcmiStreamClient(
        "127.0.0.1",
        1,  # nothing listens here; connect fails and backoff begins
        on_line,
        initial_delay=30.0,
        max_delay=30.0,
    )
    task = asyncio.create_task(client.run())
    await asyncio.sleep(0.1)  # let it enter the first backoff sleep
    await client.stop()
    await asyncio.wait_for(task, timeout=2.0)


async def test_stream_client_reconnects_on_idle_timeout() -> None:
    """Client must reconnect when no data is received for idle_timeout seconds."""
    accept_count = 0
    lines_received: list[str] = []

    async def on_line(line: str) -> None:
        lines_received.append(line)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal accept_count
        accept_count += 1
        # Send handshake response
        await _host_handshake_writer(reader, writer)
        # Send initial data
        writer.write(b"FileType=text/acmi/tacview\n")
        await writer.drain()
        # Then stop sending data - client should reconnect after idle_timeout
        await asyncio.sleep(0.5)
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    # Use a short idle_timeout for testing (2 seconds)
    client = AcmiStreamClient(
        "127.0.0.1", port, on_line, initial_delay=0.05, max_delay=0.1, idle_timeout=2.0
    )
    task = asyncio.create_task(client.run())
    try:
        # Wait for first connection and initial data
        await _wait_until(lambda: len(lines_received) >= 1)
        # Wait for reconnect after idle timeout (should take ~2 seconds)
        await _wait_until(lambda: accept_count >= 2, timeout=10.0)
        assert accept_count >= 2
    finally:
        await client.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        server.close()
        await server.wait_closed()


async def test_stream_client_idle_timeout_disabled_when_zero() -> None:
    """idle_timeout=0 should disable idle monitoring (no forced reconnect)."""
    accept_count = 0
    lines_received: list[str] = []

    async def on_line(line: str) -> None:
        lines_received.append(line)

    # Event to signal when test should end
    test_done = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal accept_count
        accept_count += 1
        await _host_handshake_writer(reader, writer)
        # Send data once, then keep connection open but silent
        writer.write(b"FileType=text/acmi/tacview\n")
        await writer.drain()
        # Keep connection open until test signals done
        await test_done.wait()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    # idle_timeout=0 disables the feature
    client = AcmiStreamClient(
        "127.0.0.1", port, on_line, initial_delay=0.05, max_delay=0.1, idle_timeout=0
    )
    task = asyncio.create_task(client.run())
    try:
        # Wait for initial data
        await _wait_until(lambda: len(lines_received) >= 1)
        # Should NOT reconnect within 3 seconds since idle_timeout=0
        await asyncio.sleep(3.0)
        assert accept_count == 1
    finally:
        test_done.set()  # Allow server to close
        await client.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        server.close()
        await server.wait_closed()
