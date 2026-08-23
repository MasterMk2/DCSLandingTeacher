"""TCP transport for ACMI streams.

The transport layer is intentionally dumb: it only assembles received bytes
into lines and hands each complete line to a callback. All parsing lives in
:class:`app.acmi.parser.AcmiParser`, which can be exercised without any
network.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

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

    - Connects to ``host:port`` and streams lines to ``on_line``.
    - On connection loss or failure, reconnects automatically using
      exponential backoff (``initial_delay`` doubling up to ``max_delay``),
      resetting backoff after each successful connection.
    - :meth:`stop` requests a graceful shutdown; the running task exits after
      the current read loop unwinds.

    Note on compression: Tacview can serve zip-compressed ACMI streams.
    Text streams are handled here; transparent zip decompression is planned —
    see TODO below.
    """

    # TODO(acmi-zip): detect zip-compressed streams (e.g. via a wrapper
    # decoder injected between the socket reader and the LineAssembler) and
    # decompress incrementally before line assembly.

    def __init__(
        self,
        host: str,
        port: int,
        on_line: LineHandler,
        *,
        initial_delay: float = 1.0,
        max_delay: float = 30.0,
    ) -> None:
        self.host = host
        self.port = port
        self._on_line = on_line
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

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        assembler = LineAssembler()
        while not self._stopping:
            chunk = await reader.read(READ_CHUNK_SIZE)
            if not chunk:
                break
            # TODO(acmi-zip): route through a decompression wrapper when the
            # stream turns out to be zip-compressed.
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
