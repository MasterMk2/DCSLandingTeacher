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
#: out and must be re-swept. v3 changes the names: reciprocal ends come from
#: the DCS designator rather than the heading, designators are zero-padded,
#: parallel strips get L/C/R, and a name that fits neither end is dropped. A v2
#: cache holds the old names -- Beslan's 10/28 stored as "27", Tbilisi's
#: designator hung on the wrong end -- and would keep them forever, because a
#: cache hit never re-checks.
CACHE_VERSION = 3

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
        seed_dir: str | Path | None = None,
    ) -> None:
        self._client = client
        self._cache_dir = Path(cache_dir)
        self._seed_dir = Path(seed_dir) if seed_dir else None
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

    def _seed_path(self, theatre: str) -> Path | None:
        if self._seed_dir is None:
            return None
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in theatre)
        return self._seed_dir / f"runways-{safe or 'unknown'}.json"

    @staticmethod
    def _seed_is_exact(path: Path) -> bool:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        return bool(data.get("exact")) and int(data.get("version", 1)) == CACHE_VERSION

    def _load_cache(self, theatre: str) -> list[Runway] | None:
        path = self._cache_path(theatre)
        seed = self._seed_path(theatre)
        if seed is not None and seed.is_file() and self._seed_is_exact(seed):
            # An exact capture outranks a live sweep; see _cached_theatres.
            path = seed
        elif not path.is_file():
            # Not swept on this server: fall back to the copy shipped with the
            # build. Without this, a theatre that has a seed but no local sweep
            # would start a pointless sweep on every restart -- and fail, if
            # the map is not loaded anywhere.
            seed = self._seed_path(theatre)
            path = seed if seed is not None and seed.is_file() else path
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

    async def sweep(self, theatre: str | None = None) -> dict[str, Any]:
        """Sweep a theatre now, ignoring whatever is already cached.

        The point of a manual sweep is to capture a map the moment somebody
        loads it -- without waiting for a landing to happen on it, which is the
        only thing that triggers the automatic path. It also re-captures a
        theatre whose cached geometry is suspect, which the automatic path will
        never do, because a cache hit never re-checks.

        ``theatre`` defaults to whatever the (single) running server reports.
        Paced against the DCS simulation thread, so this takes tens of seconds.
        """
        if self._client is None:
            return {"theatre": theatre, "swept": False, "reason": "DCSSB not configured"}
        key = (theatre or "").strip()
        if not key:
            running = await self.running_theatres()
            if len(running) != 1:
                return {
                    "theatre": None,
                    "swept": False,
                    "reason": (
                        "no server is running a mission"
                        if not running
                        else f"several theatres are running ({', '.join(sorted(running))}); "
                        "name the one to sweep"
                    ),
                }
            key = next(iter(running))

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            server = await self._server_for(key)
            if not server:
                return {
                    "theatre": key,
                    "swept": False,
                    "reason": "no DCSSB server is running that theatre",
                }
            try:
                runways = await self._client.fetch_runways(server)
            except Exception as exc:
                logger.warning("runways: sweep failed for %s", key, exc_info=True)
                return {"theatre": key, "swept": False, "reason": f"sweep failed: {exc}"}
            if not runways:
                return {"theatre": key, "swept": False, "reason": "no runways returned"}
            self._store_cache(key, runways)
            self._memory[key] = runways
            logger.info("runways: %d swept on demand (%s)", len(runways), key)
            return {
                "theatre": key,
                "swept": True,
                "server": server,
                "runways": len(runways),
                "airbases": len({r.airbase for r in runways}),
            }

    async def running_theatres(self) -> set[str]:
        """Theatres currently loaded on the servers the bot reports."""
        if self._client is None:
            return set()
        try:
            servers = await self._client.list_servers()
        except Exception:
            logger.warning("DCSSB: /servers unavailable", exc_info=True)
            return set()
        return {
            str(theatre)
            for server in servers
            if (theatre := (server.get("mission") or {}).get("theatre"))
            and not (self._server_name and server.get("name") != self._server_name)
        }

    def inventory(self) -> list[dict[str, Any]]:
        """Every theatre that can be resolved, and where its geometry came from."""
        self._cached_theatres()  # fills _memory from both directories
        out: list[dict[str, Any]] = []
        for theatre, runways in sorted(self._memory.items()):
            seed = self._seed_path(theatre)
            shipped = seed is not None and seed.is_file()
            cache = self._cache_path(theatre)
            # Mirror the precedence in _cached_theatres, or the listing names a
            # source that is not the one in use -- a stale v2 sweep sitting on
            # disk would read "swept" while its shipped replacement answers.
            if shipped and self._seed_is_exact(seed):
                origin = "shipped"
            elif cache.is_file() and self._is_current(cache):
                origin = "swept"
            elif shipped:
                origin = "shipped"
            else:
                origin = "memory"
            out.append(
                {
                    "theatre": theatre,
                    "runways": len(runways),
                    "airbases": len({r.airbase for r in runways}),
                    "origin": origin,
                }
            )
        return out

    @staticmethod
    def _is_current(path: Path) -> bool:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        return int(data.get("version", 1)) == CACHE_VERSION

    def export(self, theatre: str) -> dict[str, Any] | None:
        """The cache payload for ``theatre``, in the on-disk format.

        This is how a sweep leaves the machine that captured it: write the body
        to ``config/runways/`` and commit, and every later build resolves that
        map without the DCS server it came from.
        """
        runways = self._memory.get(theatre)
        if runways is None:
            self._cached_theatres()
            runways = self._memory.get(theatre)
        if not runways:
            return None
        return {
            "version": CACHE_VERSION,
            "theatre": theatre,
            "runways": [r.as_dict() for r in runways],
        }

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

    def _read_pool_dir(
        self, directory: Path, *, exact_only: bool = False
    ) -> list[list[Runway]]:
        """Load every cache file in ``directory`` that is not already in memory."""
        pools: list[list[Runway]] = []
        if not directory.is_dir():
            return pools
        for path in sorted(directory.glob("runways-*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if int(data.get("version", 1)) != CACHE_VERSION:
                logger.info(
                    "runway cache is stale (v%s): %s", data.get("version"), path
                )
                continue
            if exact_only and not data.get("exact"):
                continue
            theatre = data.get("theatre") or path.stem
            if theatre in self._memory:
                continue
            runways = [Runway.from_dict(r) for r in data.get("runways", [])]
            self._memory[theatre] = runways
            pools.append(runways)
        return pools

    def _cached_theatres(self) -> list[list[Runway]]:
        """Every theatre this provider can resolve against.

        Precedence, highest first -- whatever is loaded first owns the theatre,
        and later directories skip it:

        1. Shipped seeds marked ``exact``: placed from DCS's own lat/lon by the
           in-game hook, so nothing about the projection is approximated.
        2. Live sweeps on this server. They describe this server's DCS build,
           but DCSServerBot only returns grid x/z for a runway, so the
           conversion to lat/lon is an approximation -- one that measured up
           to 18 m out on Caucasus. It must not displace an exact capture.
        3. Other shipped seeds, for maps this server has never swept -- a map
           that is not loaded anywhere right now cannot be swept at all.
        """
        pools = list(self._memory.values())
        if self._seed_dir is not None:
            pools += self._read_pool_dir(self._seed_dir, exact_only=True)
        pools += self._read_pool_dir(self._cache_dir)
        if self._seed_dir is not None:
            pools += self._read_pool_dir(self._seed_dir)
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
