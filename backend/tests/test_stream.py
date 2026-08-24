"""Tests for line assembly and the TCP stream client (with handshake)."""

from __future__ import annotations

import asyncio
import gzip
import zlib

import pytest

from app.acmi.handshake import build_client_handshake
from app.acmi.stream import (
    AcmiStreamClient,
    LineAssembler,
    StreamDecodeError,
    StreamDecoder,
)

ACMI_TEXT = (
    "FileType=text/acmi/tacview\n"
    "FileVersion=2.2\n"
    "#1.50\n"
    "101,T=41.6|41.5|100,Type=Air+FixedWing\n"
)


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


# ---------------------------------------------------------------------------
# StreamDecoder: compression auto-detection (Issue #2)
# ---------------------------------------------------------------------------


def _feed_in_chunks(decoder: StreamDecoder, payload: bytes, size: int) -> str:
    return "".join(decoder.feed(payload[i : i + size]) for i in range(0, len(payload), size))


@pytest.mark.parametrize(
    "compress",
    [
        pytest.param(lambda b: b, id="plain"),
        pytest.param(lambda b: gzip.compress(b), id="gzip"),
        pytest.param(lambda b: zlib.compress(b), id="zlib"),
        pytest.param(
            lambda b: (
                lambda c: c.compress(b) + c.flush()
            )(zlib.compressobj(9, zlib.DEFLATED, -15)),
            id="deflate",
        ),
    ],
)
def test_stream_decoder_detects_and_decodes(compress) -> None:
    payload = compress(ACMI_TEXT.encode())
    decoder = StreamDecoder()
    text = _feed_in_chunks(decoder, payload, 7)  # awkward chunk boundaries
    assert text == ACMI_TEXT


def test_stream_decoder_plain_text_commits_early() -> None:
    decoder = StreamDecoder()
    assert decoder.feed(b"FileType=text/ac") == "FileType=text/ac"
    assert decoder.feed(b"mi/tacview\n") == "mi/tacview\n"
    assert decoder.flush() == ""


def test_stream_decoder_binary_junk_falls_back_to_plain() -> None:
    # Neither text nor any known compression format: decoded lossily as
    # plain text (pre-compression behavior) instead of dropping the link.
    decoder = StreamDecoder()
    junk = b"\x07" * 600  # reserved deflate block type, not valid deflate
    assert decoder.feed(junk) == junk.decode("utf-8", errors="replace")


def test_stream_decoder_corrupt_gzip_raises() -> None:
    decoder = StreamDecoder()
    with pytest.raises(StreamDecodeError):
        decoder.feed(b"\x1f\x8b" + b"\xff" * 32)


def test_stream_decoder_deflate_corrupted_midstream_raises() -> None:
    payload = (
        lambda c: c.compress(ACMI_TEXT.encode()) + c.flush()
    )(zlib.compressobj(9, zlib.DEFLATED, -15))
    decoder = StreamDecoder()
    assert decoder.feed(payload[:12]) == "FileType=te"
    # Bit-flipped continuation of a committed deflate stream must fail
    # loudly (reconnect) instead of emitting garbage.
    corrupted = bytes(b ^ 0xA5 for b in payload[12:])
    with pytest.raises(StreamDecodeError):
        decoder.feed(corrupted)


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

    client = AcmiStreamClient(
        "127.0.0.1", port, on_line, initial_delay=0.05, max_delay=0.1
    )
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

    client = AcmiStreamClient(
        "127.0.0.1", port, on_line, initial_delay=0.05, max_delay=0.1
    )
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
