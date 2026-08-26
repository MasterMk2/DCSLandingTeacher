"""Multi-source ACMI stream manager for simultaneous Tacview connections.

Manages multiple independent AcmiStreamClient instances, each connected to
a different Tacview Real-Time Telemetry source, with separate ingestion pipelines.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from logging import getLogger
from typing import Any

from app.acmi.stream import AcmiStreamClient
from app.config import TacviewSource, get_settings
from app.ingest import (
    LandingFinalizeListener,
    LandingListener,
    TrackIngestor,
)

logger = getLogger(__name__)


@dataclass
class SourceContext:
    """Runtime context for a single Tacview source."""

    source: TacviewSource
    client: AcmiStreamClient
    ingestor: TrackIngestor
    task: asyncio.Task | None = None
    connected: bool = False


class MultiSourceAcmiManager:
    """Manages multiple Tacview ACMI stream connections concurrently."""

    def __init__(
        self,
        session_factory,
        landing_listener: LandingListener | None = None,
        landing_finalize_listener: LandingFinalizeListener | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._landing_listener = landing_listener
        self._landing_finalize_listener = landing_finalize_listener
        self._sources: dict[str, SourceContext] = {}
        self._running = False

    async def start(self) -> None:
        """Start connections for all enabled sources."""
        settings = get_settings()
        self._running = True

        for source in settings.tacview_sources:
            if not source.enabled:
                logger.info("Skipping disabled source: %s (%s)", source.name, source.id)
                continue

            await self._start_source(source)

    async def _start_source(self, source: TacviewSource) -> None:
        """Start a single source's connection and ingestion pipeline."""
        # Create a dedicated ingestor for this source
        ingestor = TrackIngestor(
            self._session_factory,
            landing_listener=self._landing_listener,
            landing_finalize_listener=self._landing_finalize_listener,
            source_id=source.id,
        )

        # Line handler that tags events with source_id
        async def on_line(line: str) -> None:
            await ingestor.handle_line(line)

        client = AcmiStreamClient(
            host=source.host,
            port=source.port,
            on_line=on_line,
            client_name=source.client_name,
            password=source.password,
            idle_timeout=source.idle_timeout,
        )

        ctx = SourceContext(
            source=source,
            client=client,
            ingestor=ingestor,
        )
        ctx.task = asyncio.create_task(self._run_source(ctx))
        self._sources[source.id] = ctx
        logger.info("Started ACMI client for source: %s (%s:%d)", source.name, source.host, source.port)

    async def _run_source(self, ctx: SourceContext) -> None:
        """Run loop for a single source (handles reconnections)."""
        while self._running:
            try:
                await ctx.client.run()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Source %s client error: %s", ctx.source.id, exc)
            if not self._running:
                break
            # Brief pause before reconnect attempt (backoff handled by client)
            await asyncio.sleep(1.0)
        ctx.connected = False

    async def stop(self) -> None:
        """Stop all sources gracefully."""
        self._running = False
        for ctx in self._sources.values():
            await ctx.client.stop()
            if ctx.task:
                ctx.task.cancel()
                try:
                    await ctx.task
                except asyncio.CancelledError:
                    pass
            await ctx.ingestor.close()
        self._sources.clear()

    def get_source_status(self) -> list[dict[str, Any]]:
        """Get status of all sources for health checks / monitoring."""
        return [
            {
                "id": ctx.source.id,
                "name": ctx.source.name,
                "host": ctx.source.host,
                "port": ctx.source.port,
                "connected": ctx.client.connected,
                "enabled": ctx.source.enabled,
            }
            for ctx in self._sources.values()
        ]

    def get_source(self, source_id: str) -> SourceContext | None:
        """Get context for a specific source by ID."""
        return self._sources.get(source_id)