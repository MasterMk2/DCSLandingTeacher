"""Tests for line assembly and the TCP stream client (with handshake)."""

from __future__ import annotations

import asyncio

from app.acmi.handshake import build_client_handshake
from app.acmi.stream import AcmiStreamClient, LineAssembler


def test_line_assembler_reassembles_split_chunks() -> None:
    assembler = LineAssembler()
    assert assembler.feed("FileType=text/acmi") == []
    assert assembler.feed("/tacview\nFileVer") == ["FileType=text/acmi/tacview"]
    assert assembler.feed("sion=2.2\n101,T=1|2|3\n") == [
        "FileVersion=2.2",
        "101,T=1|2|3",
    ]
    assert assembler.flush() is None


def test_line_assembler_handles_crlf() -> None:
    assembler = LineAssembler()
    assert assembler.feed("line1\r\nline2\r\n") == ["line1", "line2"]


def test_line_assembler_flush_returns_partial_tail() -> None:
    assembler = LineAssembler()
    assert assembler.feed("complete\npartial-without-newline") == ["complete"]
    assert assembler.flush() == "partial-without-newline"
    assert assembler.flush() is None


def test_line_assembler_empty_lines_preserved() -> None:
    assembler = LineAssembler()
    assert assembler.feed("\n\nx\n") == ["", "", "x"]


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
    writer.write(
        f"XtraLib.Stream.0\nTacview.RealTimeTelemetry.0\n{host_name}\n\0".encode()
    )
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

    client = AcmiStreamClient(
        "127.0.0.1", port, on_line, initial_delay=0.05, max_delay=0.1
    )
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
