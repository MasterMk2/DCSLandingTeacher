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
from typing import Any

from app.detection.geometry import haversine_m
from app.runways.dcssb import DcssbClient
from app.runways.models import Runway, best_runway_match, match_runway

logger = getLogger(__name__)

#: Bumped when the stored geometry changes meaning. v2 rotates the DCS grid
#: frame onto geographic axes (meridian convergence); v1 caches are ~5 deg
#: out and must be re-swept.
CACHE_VERSION = 2

#: How close a touchdown has to be to one of a theatre's airbases for that
#: theatre to be the one worth sweeping. Generous on purpose: it only has to
#: separate maps, and DCS theatres are whole countries apart. It does not
#: decide which runway a landing is on -- :func:`best_runway_match` does, on
#: much tighter tolerances.
THEATRE_MATCH_RADIUS_M = 50_000.0


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
        #: Airbase listings per server name, used to work out which running
        #: theatre a landing is on. Cheap to fetch but not free, and the
        #: answer only changes when a server loads another mission -- which
        #: also invalidates it, hence the theatre it was fetched for.
        self._airbases: dict[str, tuple[str, list[dict[str, Any]]]] = {}

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

            server = await self._server_for(key)
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

    async def _server_for(self, theatre: str) -> str | None:
        """The server to sweep for ``theatre``, or ``None`` if there is none.

        With ``dcssb_server_name`` set this used to return that server
        unconditionally. ``fetch_runways`` then swept whatever map the server
        happened to be running and the result was stored under the theatre
        that was *asked for*: switch the server from Caucasus to Nevada and
        the Caucasus cache fills with Nellis geometry, permanently, because a
        cache hit never re-checks. A one-map server never showed this, which
        is exactly why it survived into a multi-map change.
        """
        if self._client is None:
            return None
        if not self._server_name:
            return await self._client.find_server_for_theatre(theatre)
        actual = await self._client.theatre_of(self._server_name)
        if actual is None:
            # Unknown, not mismatched: the bot may not report the mission
            # yet. Trust the pin, as before.
            return self._server_name
        if actual.strip().lower() != theatre.strip().lower():
            logger.info(
                "runways: pinned server %s is on %s, not %s -- not sweeping",
                self._server_name,
                actual,
                theatre,
            )
            return None
        return self._server_name

    async def _airbases_of(self, server: str, theatre: str) -> list[dict[str, Any]]:
        """Airbase listing for a running server, memoised per theatre."""
        if self._client is None:
            return []
        cached = self._airbases.get(server)
        if cached is not None and cached[0] == theatre:
            return cached[1]
        try:
            airbases = await self._client.fetch_airbases(server)
        except Exception:
            logger.warning("runways: /airbases failed for %s", server, exc_info=True)
            return []
        self._airbases[server] = (theatre, airbases)
        return airbases

    async def _theatre_for_position(
        self, latitude: float, longitude: float
    ) -> str | None:
        """Which running theatre a touchdown belongs to, or ``None``.

        Asks every server the bot reports and keeps the theatre with an
        airbase nearest the touchdown. The previous version took the first
        server reporting any theatre at all and swept that -- correct only
        while every server ran the same map. Both calls it makes are served
        from the bot's own state, so this costs no simulation-thread time;
        the paced sweep only starts once the map is known.

        Falls back to the first reported theatre when no listing could be
        fetched at all, so a bot that answers ``/servers`` but not
        ``/airbases`` behaves as it did before rather than losing runways.
        """
        if self._client is None:
            return None
        try:
            servers = await self._client.list_servers()
        except Exception:
            logger.warning("DCSSB: /servers unavailable", exc_info=True)
            return None

        best: str | None = None
        best_distance = THEATRE_MATCH_RADIUS_M
        listed_any = False
        fallback: str | None = None
        for server in servers:
            name = str(server.get("name") or "")
            theatre = (server.get("mission") or {}).get("theatre")
            if not name or not theatre:
                continue
            theatre = str(theatre)
            if self._server_name and name != self._server_name:
                continue
            if fallback is None:
                fallback = theatre
            for airbase in await self._airbases_of(name, theatre):
                try:
                    lat, lon = float(airbase["lat"]), float(airbase["lng"])
                except (KeyError, TypeError, ValueError):
                    continue
                listed_any = True
                distance = haversine_m(latitude, longitude, lat, lon)
                if distance < best_distance:
                    best, best_distance = theatre, distance
        if best is not None:
            return best
        return None if listed_any else fallback

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

        The ACMI stream does not carry the theatre name (DCS does not write
        one, verified on a real recording: the ``theater`` column is NULL),
        so matching is done purely on position. Every already-swept theatre
        is searched, so importing an old recording from a map swept earlier
        resolves without touching the DCS server at all.

        All cached maps are searched and the *best* match wins, not the
        first. DCS theatres overlap where they cover neighbouring regions --
        Syria and Iraq share the border area, Iraq and Persian Gulf share
        Kuwait -- and an airfield present on two maps is one geometry per
        map. Returning whichever map happened to be read off disk first
        graded such a landing against the other one's runway.
        """
        best: Runway | None = None
        best_key: tuple[float, float] | None = None
        for pool in self._cached_theatres():
            match = best_runway_match(pool, latitude, longitude, course_deg)
            if match is not None and (best_key is None or match[1] < best_key):
                best, best_key = match
        if best is not None:
            return best

        if self._client is None:
            return None
        # Nothing cached matches: the theatre this landing is on may not have
        # been swept yet. Sweeping is paced against the sim thread, so this
        # happens at most once per map.
        theatre = await self._theatre_for_position(latitude, longitude)
        if theatre is None or theatre in self._memory:
            return None
        runways = await self.runways_for(theatre)
        if not runways:
            return None
        return match_runway(runways, latitude, longitude, course_deg)
