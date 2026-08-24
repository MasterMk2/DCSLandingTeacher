"""TCP transport for Tacview Real-Time Telemetry streams.

Based on the official protocol documentation:
https://www.tacview.net/documentation/realtime/en/

The real-time protocol transmits ACMI 2.x text over TCP after a short
XtraLib handshake (see :mod:`app.acmi.handshake`). Some hosts emit the
payload compressed (gzip / zlib / raw deflate) right after the handshake;
:class:`StreamDecoder` auto-detects this from the first bytes of the stream
and transparently decompresses it, so the transport layer stays dumb: it
performs the handshake, assembles received bytes into lines, and hands each
complete line to a callback. All parsing lives in
:class:`app.acmi.parser.AcmiParser`, which can be exercised without any
network.

Note on compression: zip-*wrapped* ACMI remains a file format concern (see
:mod:`app.acmi.file_reader`); on the wire the payload is a single continuous
deflate/gzip/zlib stream rather than a zip container.
"""

from __future__ import annotations

import asyncio
import logging
import zlib
from collections.abc import Awaitable, Callable

from app.acmi.handshake import HandshakeError, perform_client_handshake

logger = logging.getLogger(__name__)

READ_CHUNK_SIZE = 65536
CONNECT_TIMEOUT_SECONDS = 10.0

#: Bytes buffered while deciding between plain text and raw deflate, which
#: has no magic number. Well below any meaningful ACMI header size.
DETECTION_BUFFER_LIMIT = 512

GZIP_MAGIC = b"\x1f\x8b"

LineHandler = Callable[[str], Awaitable[None]]


class StreamDecodeError(ConnectionError):
    """Raised when a compressed ACMI stream cannot be decoded.

    Subclasses :class:`ConnectionError` so the client's existing reconnect
    logic treats a decode failure like any other connection loss: the error
    is logged and the client reconnects with backoff.
    """


class LineAssembler:
    """Incrementally assemble decoded text chunks into complete lines.

    Handles arbitrary chunk boundaries (including splits in the middle of a
    line) and both LF and CRLF terminators. Any trailing partial line is kept
    until more data arrives; :meth:`flush` retrieves it at end of stream.
    """

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: str) -> list[str]:
        """Feed a decoded text chunk, returning every complete line found."""
        self._buffer += chunk
        if "\n" not in self._buffer:
            return []
        *lines, remainder = self._buffer.split("\n")
        self._buffer = remainder
        return [line.rstrip("\r") for line in lines]

    def flush(self) -> str | None:
        """Return any unterminated trailing line, if present."""
        remainder = self._buffer
        self._buffer = ""
        return remainder if remainder else None


class StreamDecoder:
    """Auto-detecting decoder for plain / gzip / zlib / raw-deflate streams.

    Detection happens once, from the first bytes received after the
    handshake:

    - ``1f 8b``                      -> gzip (:rfc:`1952`)
    - ``78 xx`` with valid FCHECK    -> zlib (:rfc:`1950`)
    - decodable printable ASCII text -> plain ACMI text (passthrough)
    - anything else                  -> raw deflate (:rfc:`1951`)

    Text detection tolerates chunk boundaries splitting multi-byte UTF-8
    sequences by ignoring up to three trailing bytes while sniffing. If a
    raw-deflate guess turns out to be wrong, :class:`StreamDecodeError` is
    raised and the caller reconnects.
    """

    def __init__(self) -> None:
        self._pending = bytearray()
        self._mode: str | None = None
        self._decomp: zlib._Decompress | None = None

    def feed(self, data: bytes) -> str:
        """Feed raw wire bytes; return decoded text (possibly empty)."""
        if self._mode is None:
            self._pending.extend(data)
            if not self._detect():
                return ""
            data = bytes(self._pending)
            self._pending.clear()
        return self._decode(data)

    def flush(self) -> str:
        """Finalize at end of stream; returns any remaining decoded text."""
        if self._mode is None:
            if not self._pending:
                return ""
            # Stream ended before detection committed: prefer plain text so
            # short plaintext sessions are never misinterpreted.
            self._start("plain")
            data = bytes(self._pending)
            self._pending.clear()
            return self._decode(data)
        if self._decomp is not None:
            try:
                tail = self._decomp.flush()
            except zlib.error:  # pragma: no cover - defensive
                tail = b""
            return tail.decode("utf-8", errors="replace")
        return ""

    # ------------------------------------------------------------------

    def _detect(self) -> bool:
        buf = self._pending
        if len(buf) < 2:
            return False
        if buf[:2] == GZIP_MAGIC:
            self._start("gzip")
            return True
        if buf[0] == 0x78 and ((buf[0] << 8) | buf[1]) % 31 == 0:
            self._start("zlib")
            return True
        if _looks_like_text(buf):
            self._start("plain")
            return True
        # Unknown binary: probe raw deflate against the buffered prefix.
        # (Raw deflate has no magic number, so this is a best-effort guess.)
        trial = zlib.decompressobj(-15)
        try:
            out = trial.decompress(bytes(buf))
        except zlib.error:
            # Not deflate either: fall back to lossy plain decoding, which
            # matches the pre-compression behavior for undecodable payloads.
            logger.debug(
                "ACMI stream is neither text nor a known compression format; "
                "decoding as plain text"
            )
            self._start("plain")
            return True
        if out:
            self._start("deflate")
            return True
        # Deflate header accepted but no output yet (very short/truncated
        # stream): keep buffering up to the detection limit, then commit.
        if len(buf) >= DETECTION_BUFFER_LIMIT:
            self._start("deflate")
            return True
        return False

    def _start(self, mode: str) -> None:
        self._mode = mode
        wbits = {"gzip": 31, "zlib": 15, "deflate": -15}.get(mode)
        self._decomp = zlib.decompressobj(wbits) if wbits is not None else None

    def _decode(self, data: bytes) -> str:
        if self._decomp is None:
            return data.decode("utf-8", errors="replace")
        try:
            out = self._decomp.decompress(data)
        except zlib.error as exc:
            raise StreamDecodeError(
                f"ACMI {self._mode} stream corrupted: {exc}"
            ) from exc
        return out.decode("utf-8", errors="replace")


def _looks_like_text(buf: bytearray | bytes) -> bool:
    """True when ``buf`` is consistent with plain UTF-8 ACMI text."""
    candidate = bytes(buf)
    # Ignore an incomplete trailing multi-byte UTF-8 sequence caused by a
    # chunk boundary inside one character.
    for trim in range(4):
        try:
            text = candidate[: len(candidate) - trim or None].decode("utf-8")
            break
        except UnicodeDecodeError:
            continue
    else:
        return False
    return all(ch.isprintable() or ch in "\t\n\r" for ch in text)


class AcmiStreamClient:
    """Asyncio TCP client for Tacview realtime telemetry streams.

    Behavior:

    - Connects to ``host:port`` and performs the XtraLib client handshake
      (``client_name`` / ``password``) before streaming lines to ``on_line``.
    - On connection loss, handshake failure or connect error, reconnects
      automatically using exponential backoff (``initial_delay`` doubling up
      to ``max_delay``), resetting backoff after each successful connection.
    - :meth:`stop` requests a graceful shutdown; the running task exits after
      the current read loop unwinds.
    """

    def __init__(
        self,
        host: str,
        port: int,
        on_line: LineHandler,
        *,
        client_name: str = "DCSLandingTeacher",
        password: str = "",
        initial_delay: float = 1.0,
        max_delay: float = 30.0,
    ) -> None:
        self.host = host
        self.port = port
        self._on_line = on_line
        self._client_name = client_name
        self._password = password
        self._initial_delay = max(initial_delay, 0.1)
        self._max_delay = max(max_delay, self._initial_delay)
        self._stopping = False
        self.connected = False
        self._stop_event: asyncio.Event | None = None

    async def run(self) -> None:
        """Run the client loop until :meth:`stop` is called."""
        backoff = self._initial_delay
        while not self._stopping:
            reader, writer = await self._connect(backoff)
            if reader is None:
                continue

            try:
                await self._handshake(reader, writer)
            except asyncio.CancelledError:
                raise
            except HandshakeError as exc:
                logger.warning(
                    "ACMI handshake with %s:%d failed: %s", self.host, self.port, exc
                )
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):  # pragma: no cover
                    pass
                await self._sleep(backoff)
                backoff = min(backoff * 2, self._max_delay)
                continue

            backoff = self._initial_delay
            self.connected = True
            logger.info("ACMI stream connected to %s:%d", self.host, self.port)
            try:
                await self._read_loop(reader)
            except asyncio.CancelledError:
                raise
            except (ConnectionError, OSError) as exc:
                logger.warning("ACMI stream error: %s", exc)
            finally:
                self.connected = False
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):  # pragma: no cover - best effort
                    pass

            if self._stopping:
                break
            logger.info(
                "ACMI stream disconnected; reconnecting in %.1fs", backoff
            )
            await self._sleep(backoff)
            backoff = min(backoff * 2, self._max_delay)

    async def stop(self) -> None:
        """Request graceful shutdown of :meth:`run`.

        Wakes any pending backoff sleep so the run loop exits promptly.
        """
        self._stopping = True
        if self._stop_event is not None:
            self._stop_event.set()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _connect(
        self, backoff: float
    ) -> tuple[asyncio.StreamReader | None, asyncio.StreamWriter | None]:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=CONNECT_TIMEOUT_SECONDS,
            )
            return reader, writer
        except asyncio.TimeoutError:
            logger.warning(
                "ACMI connect to %s:%d timed out; retrying in %.1fs",
                self.host,
                self.port,
                backoff,
            )
        except (ConnectionError, OSError) as exc:
            logger.warning(
                "ACMI connect to %s:%d failed (%s); retrying in %.1fs",
                self.host,
                self.port,
                exc,
                backoff,
            )
        await self._sleep(backoff)
        return None, None

    async def _handshake(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        host_name = await perform_client_handshake(
            reader, writer, self._client_name, self._password
        )
        logger.debug("ACMI handshake completed with host %r", host_name)

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        decoder = StreamDecoder()
        assembler = LineAssembler()
        try:
            while not self._stopping:
                chunk = await reader.read(READ_CHUNK_SIZE)
                if not chunk:
                    break
                for line in assembler.feed(decoder.feed(chunk)):
                    await self._on_line(line)
            tail_text = decoder.flush()
            if tail_text:
                for line in assembler.feed(tail_text):
                    await self._on_line(line)
        except StreamDecodeError as exc:
            # Corrupted compressed payload: log and let the caller treat it
            # like a connection loss so the client reconnects (Issue #2).
            logger.error("ACMI stream decode failed: %s", exc)
            raise
        tail = assembler.flush()
        if tail is not None:
            await self._on_line(tail)

    def _get_stop_event(self) -> asyncio.Event:
        # Created lazily so the client binds to the running event loop,
        # even if constructed before the loop started.
        if self._stop_event is None:
            self._stop_event = asyncio.Event()
            if self._stopping:
                self._stop_event.set()
        return self._stop_event

    async def _sleep(self, delay: float) -> None:
        """Sleep for ``delay`` seconds, or return early when stopped."""
        try:
            await asyncio.wait_for(self._get_stop_event().wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
