"""WebSocket connection manager for real-time landing notifications (FR-5)."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from fastapi import WebSocket


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
        # Replay recent events so a fresh client has immediate context.
        for payload in list(self._replay):
            try:
                await websocket.send_json({"type": "landing", "landing": payload})
            except Exception:
                break

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)

    async def broadcast_landing(self, payload: dict[str, Any]) -> None:
        """Push one landing summary to every connected client."""
        self._replay.append(payload)
        message = {"type": "landing", "landing": payload}
        stale: list[WebSocket] = []
        async with self._lock:
            targets = list(self._clients)
        for websocket in targets:
            try:
                await websocket.send_json(message)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            self.disconnect(websocket)
