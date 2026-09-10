"""WebSocket connection manager for real-time landing notifications (FR-5)."""

from __future__ import annotations

import asyncio
from collections import deque
from logging import getLogger
from typing import Any

from fastapi import WebSocket

logger = getLogger(__name__)


class LandingNotifier:
    """Fan-out of new landing events to connected WebSocket clients.

    Keeps a small replay buffer so clients that connect right after an event
    still receive it.
    """

    def __init__(self, replay_size: int = 20) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._replay: deque[dict[str, Any]] = deque(maxlen=replay_size)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._clients.add(websocket)
        # Replay recent messages so a fresh client has immediate context.
        for message in list(self._replay):
            try:
                await websocket.send_json(message)
            except Exception:
                break

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)

    async def broadcast_landing(
        self, payload: dict[str, Any], *, message_type: str = "landing"
    ) -> None:
        """Push one landing summary to every connected client.

        ``message_type`` is ``"landing"`` for newly detected (possibly
        provisional) landings and ``"landing_update"`` when a provisional
        record is confirmed to its final outcome (Issue #5).
        """
        await self.broadcast_message({"type": message_type, "landing": payload})

    async def broadcast_message(self, message: dict[str, Any]) -> None:
        """Send an arbitrary pre-built message to every connected client.

        Used for non-landing notifications such as ACMI import job updates;
        the message is also appended to the replay buffer.
        """
        self._replay.append(message)
        stale: list[WebSocket] = []
        async with self._lock:
            targets = list(self._clients)
        for websocket in targets:
            try:
                await websocket.send_json(message)
            except Exception:
                # A single dead client must not break delivery to the rest.
                stale.append(websocket)
        for websocket in stale:
            logger.warning(
                "dropped WebSocket client after send failure",
                extra={"ctx_clients": len(self._clients)},
            )
            self.disconnect(websocket)
