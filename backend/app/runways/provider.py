"""Per-theatre runway cache in front of the DCSServerBot sweep.

Sweeping an airbase list costs simulation-thread time on the DCS server (see
:mod:`app.runways.dcssb`), so the result is cached on disk per theatre and
reused for every later landing on that map -- including across restarts and
across sources flying the same theatre.
"""

from __future__ import annotations

import asyncio
import json
from logging import getLogger
from pathlib import Path

from app.runways.dcssb import DcssbClient
from app.runways.models import Runway, match_runway

logger = getLogger(__name__)

#: Bumped when the stored geometry changes meaning. v2 rotates the DCS grid
#: frame onto geographic axes (meridian convergence); v1 caches are ~5 deg
#: out and must be re-swept.
CACHE_VERSION = 2


class RunwayProvider:
    """Resolves the runway a landing belongs to, or ``None`` when unknown."""

    def __init__(
        self,
        client: DcssbClient | None,
        cache_dir: str | Path,
        *,
        server_name: str = "",
    ) -> None:
        self._client = client
        self._cache_dir = Path(cache_dir)
        self._server_name = server_name
        self._memory: dict[str, list[Runway]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def _cache_path(self, theatre: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in theatre)
        return self._cache_dir / f"runways-{safe or 'unknown'}.json"

    def _load_cache(self, theatre: str) -> list[Runway] | None:
        path = self._cache_path(theatre)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if int(data.get("version", 1)) != CACHE_VERSION:
                # Geometry written by an older build. Re-sweeping costs one
                # pass over the theatre; serving a rotated runway silently
                # mis-grades every landing at it.
                logger.info("runway cache is stale (v%s): %s", data.get("version"), path)
                return None
            return [Runway.from_dict(r) for r in data.get("runways", [])]
        except Exception:
            logger.warning("runway cache unreadable: %s", path, exc_info=True)
            return None

    def _store_cache(self, theatre: str, runways: list[Runway]) -> None:
        path = self._cache_path(theatre)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "version": CACHE_VERSION,
                        "theatre": theatre,
                        "runways": [r.as_dict() for r in runways],
                    },
                    indent=1,
                ),
                encoding="utf-8",
            )
        except OSError:
            logger.warning("could not write runway cache: %s", path, exc_info=True)

    async def runways_for(self, theatre: str | None) -> list[Runway]:
        """Runways of ``theatre``; sweeps DCS at most once per theatre."""
        key = (theatre or "").strip() or "unknown"
        if key in self._memory:
            return self._memory[key]

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self._memory:  # filled while we waited
                return self._memory[key]

            cached = self._load_cache(key)
            if cached is not None:
                logger.info("runways: %d from cache (%s)", len(cached), key)
                self._memory[key] = cached
                return cached

            if self._client is None:
                self._memory[key] = []
                return []

            server = self._server_name or await self._client.find_server_for_theatre(
                theatre
            )
            if not server:
                # Do not cache: the right server may simply not be running the
                # theatre yet, and a later landing should retry.
                logger.info("runways: no DCSSB server serving theatre %s", key)
                return []
            try:
                runways = await self._client.fetch_runways(server)
            except Exception:
                logger.warning("runways: sweep failed for %s", key, exc_info=True)
                return []
            if not runways:
                return []
            logger.info("runways: %d from DCS (%s)", len(runways), key)
            self._store_cache(key, runways)
            self._memory[key] = runways
            return runways

    def _cached_theatres(self) -> list[list[Runway]]:
        """Every theatre swept so far, from memory and from disk."""
        pools = list(self._memory.values())
        if not self._cache_dir.is_dir():
            return pools
        for path in sorted(self._cache_dir.glob("runways-*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            theatre = data.get("theatre") or path.stem
            if theatre in self._memory:
                continue
            runways = [Runway.from_dict(r) for r in data.get("runways", [])]
            self._memory[theatre] = runways
            pools.append(runways)
        return pools

    async def resolve(
        self,
        latitude: float,
        longitude: float,
        course_deg: float | None,
    ) -> Runway | None:
        """Runway matching a touchdown, or ``None`` if it cannot be resolved.

        The ACMI stream does not carry the theatre name, so matching is done
        purely on position: a landing simply will not be within a few km of
        a runway on the wrong map. Every already-swept theatre is searched
        first, so importing an old recording from a map that was swept
        earlier still resolves without touching the DCS server at all.
        """
        for pool in self._cached_theatres():
            match = match_runway(pool, latitude, longitude, course_deg)
            if match is not None:
                return match

        if self._client is None:
            return None
        # Nothing cached matches: the live theatre may not have been swept
        # yet. Sweeping is paced against the sim thread, so this happens at
        # most once per map.
        theatre = await self._live_theatre()
        if theatre is None or theatre in self._memory:
            return None
        runways = await self.runways_for(theatre)
        if not runways:
            return None
        return match_runway(runways, latitude, longitude, course_deg)

    async def _live_theatre(self) -> str | None:
        if self._client is None:
            return None
        try:
            servers = await self._client.list_servers()
        except Exception:
            logger.warning("DCSSB: /servers unavailable", exc_info=True)
            return None
        for server in servers:
            theatre = (server.get("mission") or {}).get("theatre")
            if theatre:
                if self._server_name and server.get("name") != self._server_name:
                    continue
                return str(theatre)
        return None
