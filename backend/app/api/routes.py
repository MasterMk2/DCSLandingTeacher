"""REST API routes + WebSocket notifications."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import (
    require_auth,
    ws_connect_authorized,
    ws_extract_token,
    ws_still_authorized,
)
from app.api.errors import AppError
from app.importer import IMPORT_SOURCE_PREFIX
from app.pipeline import _touchdown_epoch
from app.api.schemas import (
    ApproachTrackOut,
    DeviationSampleOut,
    FactorOut,
    LandingDetail,
    LandingListResponse,
    LandingSummary,
    RegradeRequest,
    RegradeResponse,
    SourceInfo,
    TouchdownState,
)
from app.models.entities import DcsObject, Flight, Landing

# No prefix here: the version prefix (/api/v1) and the legacy /api alias are
# applied at include time in app.api.main (Issue #38).
router = APIRouter()

# Landing endpoints live on a separate router that enforces the shared-token
# authentication (Issue #8). /api/health and the WebSocket endpoint stay on
# the public router: health is for liveness monitoring and the WebSocket
# performs its own ?token= check (browsers cannot attach WS headers).
# No prefix here (Issue #38): applied at include time in app.api.main.
protected_router = APIRouter(dependencies=[Depends(require_auth)])


async def get_session(request: Request) -> AsyncSession:
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        yield session


@router.get("/health")
async def health(request: Request) -> dict:
    settings = request.app.state.settings
    client = getattr(request.app.state, "acmi_client", None)
    notifier = getattr(request.app.state, "notifier", None)
    multi_source_manager = getattr(request.app.state, "multi_source_manager", None)
    sources_status = multi_source_manager.get_source_status() if multi_source_manager else []

    database = await _check_database(request)
    # A dead database means the service cannot serve landing data, so probes
    # (k8s/lb) should take it out of rotation (Issue #45).
    if not database["connected"]:
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "version": request.app.version,
                "acmi_enabled": settings.acmi_enabled,
                "acmi_connected": bool(client.connected) if client is not None else False,
                "acmi_sources": sources_status,
                "ws_clients": notifier.client_count if notifier is not None else 0,
                "database": database,
            },
        )

    return {
        "status": "ok",
        "version": request.app.version,
        "acmi_enabled": settings.acmi_enabled,
        "acmi_connected": bool(client.connected) if client is not None else False,
        "acmi_sources": sources_status,  # Issue #13 multi-source support
        "ws_clients": notifier.client_count if notifier is not None else 0,
        "database": database,
    }


async def _check_database(request: Request) -> dict:
    """Verify the database is reachable and report round-trip latency (Issue #45)."""
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        return {"connected": False, "error": "session factory not configured"}
    try:
        start = time.monotonic()
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
        latency_ms = (time.monotonic() - start) * 1000
        return {"connected": True, "latency_ms": round(latency_ms, 1)}
    except Exception as exc:
        return {"connected": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Landings (FR-5 dashboard / detail, FR-7 re-grading)
# ---------------------------------------------------------------------------


def _summary(
    landing: Landing,
    dcs_object: DcsObject | None,
    reference_time: str | None = None,
) -> LandingSummary:
    return LandingSummary(
        id=landing.id,
        flight_id=landing.flight_id,
        kind=landing.kind,
        outcome=landing.outcome,
        outcome_status=landing.outcome_status or "final",
        venue_name=landing.venue_name,
        pilot=dcs_object.pilot if dcs_object else None,
        airframe=dcs_object.name if dcs_object else None,
        touchdown_time=landing.touchdown_time,
        # Wall-clock epoch (Issue D-1): ReferenceTime + mission time.
        touchdown_epoch=_touchdown_epoch(reference_time, landing.touchdown_time),
        grade=landing.grade,
        score=landing.score,
        created_at=landing.created_at,
        source_id=landing.source_id,
        source_name=landing.source_id,  # Could be enhanced to resolve actual name from settings
        approach_pattern=landing.approach_pattern,
    )


@protected_router.get("/landings", response_model=LandingListResponse)
async def list_landings(
    request: Request,
    player: str | None = Query(default=None, description="Filter by pilot name"),
    airframe: str | None = Query(default=None, description="Filter by aircraft name"),
    venue: str | None = Query(default=None, description="Carrier or airbase name"),
    kind: str | None = Query(default=None, pattern="^(carrier|land)$"),
    grade: str | None = Query(default=None),
    outcome: str | None = Query(
        default=None, pattern="^(full_stop|touch_and_go|bolter)$"
    ),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    source: str | None = Query(default=None, description="Filter by Tacview source ID"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> LandingListResponse:
    query = (
        select(Landing, DcsObject, Flight)
        .join(DcsObject, Landing.object_id == DcsObject.id, isouter=True)
        .join(Flight, Landing.flight_id == Flight.id, isouter=True)
        .order_by(Landing.touchdown_time.is_(None), Landing.touchdown_time.desc(), Landing.id.desc())
    )

    if player:
        query = query.where(DcsObject.pilot.ilike(f"%{player}%"))
    if airframe:
        query = query.where(DcsObject.name.ilike(f"%{airframe}%"))
    if venue:
        query = query.where(Landing.venue_name.ilike(f"%{venue}%"))
    if kind:
        query = query.where(Landing.kind == kind)
    if grade:
        query = query.where(Landing.grade == grade)
    if outcome:
        query = query.where(Landing.outcome == outcome)
    if date_from is not None:
        query = query.where(Landing.created_at >= date_from)
    if date_to is not None:
        query = query.where(Landing.created_at <= date_to)
    if source:
        query = query.where(Landing.source_id == source)
    else:
        # Uploaded recordings are scratch data scoped to their own source and
        # are frequently from an unrelated server or theatre; they must not
        # join the shared history unless explicitly asked for by source.
        query = query.where(
            or_(
                Landing.source_id.is_(None),
                ~Landing.source_id.like(f"{IMPORT_SOURCE_PREFIX}%"),
            )
        )

    total_result = await session.execute(
        select(func.count()).select_from(query.subquery())
    )
    total = total_result.scalar_one()

    result = await session.execute(query.limit(limit).offset(offset))
    rows = result.all()
    items = [
        _summary(landing, obj, reference_time=flight.reference_time)
        for landing, obj, flight in rows
    ]

    # Fetch available sources for the filter dropdown (Issue #13)
    sources_result = await session.execute(
        select(Flight.source_id.distinct())
        .where(Flight.source_id.is_not(None))
        .where(~Flight.source_id.like(f"{IMPORT_SOURCE_PREFIX}%"))
    )
    sources = [
        SourceInfo(id=row[0], name=row[0], connected=True)
        for row in sources_result.all()
    ]

    return LandingListResponse(
        items=items, total=total, limit=limit, offset=offset, sources=sources
    )


@protected_router.get("/landings/{landing_id}", response_model=LandingDetail)
async def get_landing(
    landing_id: int,
    session: AsyncSession = Depends(get_session),
) -> LandingDetail:
    result = await session.execute(
        select(Landing, DcsObject, Flight)
        .join(DcsObject, Landing.object_id == DcsObject.id, isouter=True)
        .join(Flight, Landing.flight_id == Flight.id, isouter=True)
        .where(Landing.id == landing_id)
    )
    row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail="landing not found")
    landing, obj, flight = row

    approach = None
    if landing.approach_track:
        data = dict(landing.approach_track)
        samples = [
            DeviationSampleOut(
                time=s["time"],
                distance_to_go=s["distance_to_go"],
                glideslope_deviation=s.get("glideslope_deviation"),
                centerline_deviation=s.get("centerline_deviation"),
                speed=s.get("speed"),
                aoa=s.get("aoa"),
                agl=s.get("agl"),
            )
            for s in data.pop("samples", [])
        ]
        approach = ApproachTrackOut(**data, samples=samples)

    factors = [FactorOut(**f) for f in (landing.factors or [])]
    return LandingDetail(
        **_summary(landing, obj, reference_time=flight.reference_time).model_dump(),
        carrier_object_id=landing.carrier_object_id,
        comment=landing.comment,
        factors=factors,
        metrics=landing.metrics,
        grading_version=landing.grading_version,
        graded_at=landing.graded_at,
        touchdown=TouchdownState(
            latitude=landing.latitude,
            longitude=landing.longitude,
            altitude=landing.altitude,
            heading=landing.heading,
            speed_ms=landing.speed,
            descent_rate_ms=landing.descent_rate,
        ),
        approach_track=approach,
    )


@protected_router.post("/landings/{landing_id}/regrade", response_model=RegradeResponse)
async def regrade_landing(
    landing_id: int,
    request: Request,
    body: RegradeRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> RegradeResponse:
    """Re-apply the current thresholds to a stored approach track (FR-7)."""
    result = await session.execute(select(Landing).where(Landing.id == landing_id))
    landing = result.scalar_one_or_none()
    if landing is None:
        raise HTTPException(status_code=404, detail="landing not found")
    if not landing.approach_track:
        raise HTTPException(
            status_code=409, detail="landing has no stored approach track"
        )

    pipeline = getattr(request.app.state, "pipeline", None)
    if pipeline is None:
        raise AppError(503, "PIPELINE_UNAVAILABLE", "grading pipeline unavailable")

    overrides = body.overrides if body is not None else None
    payload = await pipeline.regrade(landing, overrides=overrides)
    return RegradeResponse(**payload)


@protected_router.post("/config/reload")
async def reload_grading_config(request: Request) -> dict:
    """Hot-reload grading thresholds from disk without a restart (Issue #40).

    New landings (and future regrades) use the fresh values immediately. The
    background poller also reloads on file change; this lets an operator force
    it after an edit. Requires the shared token.
    """
    pipeline = getattr(request.app.state, "pipeline", None)
    if pipeline is None:
        raise HTTPException(status_code=503, detail="grading pipeline unavailable")
    before = getattr(request.app.state, "config_reload_total", 0)
    pipeline.reload_config()
    request.app.state.grading_config = pipeline._config
    request.app.state.config_reload_total = before + 1
    return {
        "reloaded": True,
        "config_reload_total": request.app.state.config_reload_total,
    }


# ---------------------------------------------------------------------------
# WebSocket: real-time landing notifications (FR-5)
# ---------------------------------------------------------------------------


#: Idle interval between authorization re-checks for WebSocket clients. A
#: connection is receive-idle, so without a periodic check a client that was
#: accepted before authentication was enabled (Issue #25) would stay open
#: forever. The re-check also catches server-side token rotation.
WS_AUTH_RECHECK_SECONDS = 30.0


@router.websocket("/ws/landings")
async def ws_landings(websocket: WebSocket) -> None:
    provided = ws_extract_token(websocket)
    if not ws_connect_authorized(websocket, provided):
        # Reject during the handshake (ASGI servers answer with HTTP 403).
        await websocket.close(code=1008)
        return
    notifier = getattr(websocket.app.state, "notifier", None)
    if notifier is None:
        await websocket.close(code=1013)
        return
    await notifier.connect(websocket)
    try:
        while True:
            try:
                message = await asyncio.wait_for(
                    websocket.receive_text(), timeout=WS_AUTH_RECHECK_SECONDS
                )
            except asyncio.TimeoutError:
                # Periodic re-authorization check for idle connections.
                if not ws_still_authorized(websocket, provided):
                    await websocket.close(code=1008)
                    return
                continue
            if not ws_still_authorized(websocket, provided):
                await websocket.close(code=1008)
                return
            if message.strip().lower() == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        notifier.disconnect(websocket)
