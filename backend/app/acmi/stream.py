"""TCP transport for Tacview Real-Time Telemetry streams.

Based on the official protocol documentation:
https://www.tacview.net/documentation/realtime/en/

The real-time protocol transmits *uncompressed* ACMI 2.x text over TCP after
a short XtraLib handshake (see :mod:`app.acmi.handshake`). The transport
layer is intentionally dumb: it performs the handshake, assembles received
bytes into lines, and hands each complete line to a callback. All parsing
lives in :class:`app.acmi.parser.AcmiParser`, which can be exercised without
any network.

Note on compression: zip-wrapped ACMI is a *file* format concern; the
real-time protocol itself is uncompressed by specification. For reading
zip-wrapped ACMI files see :mod:`app.acmi.file_reader`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.acmi.handshake import HandshakeError, perform_client_handshake

logger = logging.getLogger(__name__)

READ_CHUNK_SIZE = 65536
CONNECT_TIMEOUT_SECONDS = 10.0

LineHandler = Callable[[str], Awaitable[None]]


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
        assembler = LineAssembler()
        while not self._stopping:
            chunk = await reader.read(READ_CHUNK_SIZE)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            for line in assembler.feed(text):
                await self._on_line(line)
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
