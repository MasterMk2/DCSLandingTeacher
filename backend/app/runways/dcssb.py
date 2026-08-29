"""Minimal DCSServerBot RestAPI client for runway geometry.

Endpoints used (all under the configured prefix, ``/stats`` by default):

- ``GET /servers``                                -> server list incl. theatre
- ``GET /airbases?server_name=``                  -> airbase list (cheap, cached by the bot)
- ``GET /airbase?server_name=&airbase_name=``     -> runways for one airbase

The last one executes ``Airbase.getRunways()`` **on the DCS simulation
thread**, so a full sweep is paced by ``request_spacing_ms``. Lowering that
spacing directly costs server frame time; don't.
"""

from __future__ import annotations

import asyncio
import math
from logging import getLogger
from typing import Any

import httpx

from app.runways.models import Runway, runway_pair_from_dcs

logger = getLogger(__name__)


class DcssbClient:
    """Fetches runway geometry for a whole theatre."""

    def __init__(
        self,
        base_url: str,
        *,
        api_prefix: str = "/stats",
        api_key: str = "",
        request_spacing_ms: int = 1500,
        timeout_s: float = 10.0,
    ) -> None:
        self._base = base_url.rstrip("/") + api_prefix
        self._api_key = api_key
        self._spacing_s = max(0.0, request_spacing_ms / 1000.0)
        self._timeout = timeout_s

    @property
    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._api_key} if self._api_key else {}

    async def _get(self, client: httpx.AsyncClient, endpoint: str, **params: Any) -> Any:
        response = await client.get(
            self._base + endpoint,
            params=params,
            headers=self._headers,
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.json()

    async def list_servers(self) -> list[dict[str, Any]]:
        async with httpx.AsyncClient() as client:
            data = await self._get(client, "/servers")
        return data if isinstance(data, list) else []

    async def find_server_for_theatre(self, theatre: str | None) -> str | None:
        """Name of a running server on ``theatre`` (any server if unknown)."""
        try:
            servers = await self.list_servers()
        except Exception:
            logger.warning("DCSSB: /servers unavailable", exc_info=True)
            return None
        for server in servers:
            mission = server.get("mission") or {}
            if theatre and str(mission.get("theatre", "")).lower() != theatre.lower():
                continue
            name = server.get("name")
            if name:
                return str(name)
        return None

    async def fetch_runways(self, server_name: str) -> list[Runway]:
        """Sweep every airbase of ``server_name``'s current theatre.

        Paced by ``request_spacing_ms``; a Caucasus sweep is ~21 airbases.
        """
        runways: list[Runway] = []
        async with httpx.AsyncClient() as client:
            listing = await self._get(client, "/airbases", server_name=server_name)
            airbases = [
                a
                for a in (listing or {}).get("airbases", [])
                if a.get("runwayList")
            ]
            logger.info(
                "DCSSB: sweeping %d airbases on %s (~%ds)",
                len(airbases),
                server_name,
                int(len(airbases) * self._spacing_s),
            )
            for index, airbase in enumerate(airbases):
                if index:
                    await asyncio.sleep(self._spacing_s)
                # `/airbase` matches on the *display* name, not the id: asking
                # for "Anapa" returns no runways while "Anapa-Vityazevo" does.
                # Most airbases have both equal, which is what makes this an
                # easy thing to get wrong and only notice as partial coverage.
                name = airbase.get("name") or airbase.get("id")
                try:
                    detail = await self._get(
                        client,
                        "/airbase",
                        server_name=server_name,
                        airbase_name=name,
                    )
                except Exception:
                    logger.warning("DCSSB: /airbase failed for %s", name, exc_info=True)
                    continue
                runways.extend(
                    _parse_airbase(airbase, detail, _convergence_deg(airbase, airbases))
                )
        return runways


def _convergence_deg(
    airbase: dict[str, Any], airbases: list[dict[str, Any]]
) -> float:
    """Angle from DCS grid north to true north at ``airbase``.

    DCS's x/z is the theatre's map projection, whose north only coincides
    with true north on the central meridian. Rather than hard-coding each
    theatre's projection, measure it: every airbase comes with BOTH its
    lat/lng and its x/z, so the bearing to a neighbour computed each way
    differs by exactly the local convergence.

    Checked against ``(lon - 33) * sin(lat)`` -- the closed form for
    Caucasus's transverse Mercator -- across 26 airbases from 37.3 to
    41.6 E: agreement within 0.1 deg, 3.17 to 5.66 deg.

    Returns 0.0 when there is no usable neighbour, which leaves the previous
    (uncorrected) behaviour rather than inventing a rotation.
    """
    try:
        lat = float(airbase["lat"])
        lon = float(airbase["lng"])
        position = airbase["position"]
        x = float(position["x"])
        z = float(position["z"])
    except (KeyError, TypeError, ValueError):
        return 0.0

    best: tuple[float, float, float, float, float] | None = None
    for other in airbases:
        if other is airbase:
            continue
        try:
            other_pos = other["position"]
            ox, oz = float(other_pos["x"]), float(other_pos["z"])
            olat, olon = float(other["lat"]), float(other["lng"])
        except (KeyError, TypeError, ValueError):
            continue
        distance = math.hypot(ox - x, oz - z)
        # Close neighbours make the bearing noisy; far ones average the
        # convergence over a span of longitude it actually varies across.
        if distance < 5000.0:
            continue
        if best is None or distance < best[0]:
            best = (distance, olat, olon, ox, oz)
    if best is None:
        return 0.0

    _, olat, olon, ox, oz = best
    lat1, lat2 = math.radians(lat), math.radians(olat)
    delta = math.radians(olon - lon)
    geographic = math.degrees(
        math.atan2(
            math.sin(delta) * math.cos(lat2),
            math.cos(lat1) * math.sin(lat2)
            - math.sin(lat1) * math.cos(lat2) * math.cos(delta),
        )
    )
    grid = math.degrees(math.atan2(oz - z, ox - x))
    return (geographic - grid + 180.0) % 360.0 - 180.0


def _parse_airbase(
    airbase: dict[str, Any], detail: Any, convergence_deg: float = 0.0
) -> list[Runway]:
    payload = detail.get("airbase") if isinstance(detail, dict) else None
    if not isinstance(payload, dict):
        return []
    position = airbase.get("position") or {}
    try:
        reference = (
            float(airbase["lat"]),
            float(airbase["lng"]),
            float(position["x"]),
            float(position["z"]),
        )
    except (KeyError, TypeError, ValueError):
        return []

    name = str(airbase.get("id") or airbase.get("name") or "?")
    out: list[Runway] = []
    for record in payload.get("runways") or []:
        pos = record.get("position") or {}
        try:
            out.extend(
                runway_pair_from_dcs(
                    airbase=name,
                    dcs_name=record.get("Name"),
                    course_rad=float(record["course"]),
                    centre_x=float(pos["x"]),
                    centre_z=float(pos["z"]),
                    elevation_m=float(pos.get("y", airbase.get("alt", 0.0)) or 0.0),
                    length_m=float(record.get("length") or 2000.0),
                    width_m=float(record.get("width") or 45.0),
                    airbase_ref=reference,
                    convergence_deg=convergence_deg,
                )
            )
        except (KeyError, TypeError, ValueError):
            logger.debug("DCSSB: unusable runway record at %s", name, exc_info=True)
    return out
