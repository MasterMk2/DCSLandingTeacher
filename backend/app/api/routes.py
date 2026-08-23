"""REST API routes + WebSocket notifications."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    ApproachTrackOut,
    DeviationSampleOut,
    FactorOut,
    LandingDetail,
    LandingListResponse,
    LandingSummary,
    RegradeRequest,
    RegradeResponse,
    TouchdownState,
)
from app.models.entities import DcsObject, Landing

router = APIRouter(prefix="/api")


async def get_session(request: Request) -> AsyncSession:
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        yield session


@router.get("/health")
async def health(request: Request) -> dict:
    settings = request.app.state.settings
    client = getattr(request.app.state, "acmi_client", None)
    notifier = getattr(request.app.state, "notifier", None)
    return {
        "status": "ok",
        "version": request.app.version,
        "acmi_enabled": settings.acmi_enabled,
        "acmi_connected": bool(client.connected) if client is not None else False,
        "ws_clients": notifier.client_count if notifier is not None else 0,
    }


# ---------------------------------------------------------------------------
# Landings (FR-5 dashboard / detail, FR-7 re-grading)
# ---------------------------------------------------------------------------


def _summary(landing: Landing, dcs_object: DcsObject | None) -> LandingSummary:
    return LandingSummary(
        id=landing.id,
        flight_id=landing.flight_id,
        kind=landing.kind,
        outcome=landing.outcome,
        venue_name=landing.venue_name,
        pilot=dcs_object.pilot if dcs_object else None,
        airframe=dcs_object.name if dcs_object else None,
        touchdown_time=landing.touchdown_time,
        grade=landing.grade,
        score=landing.score,
        created_at=landing.created_at,
    )


@router.get("/landings", response_model=LandingListResponse)
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
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> LandingListResponse:
    query = (
        select(Landing, DcsObject)
        .join(DcsObject, Landing.object_id == DcsObject.id, isouter=True)
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

    total_result = await session.execute(
        select(func.count()).select_from(query.subquery())
    )
    total = total_result.scalar_one()

    result = await session.execute(query.limit(limit).offset(offset))
    rows = result.all()
    items = [_summary(landing, obj) for landing, obj in rows]
    return LandingListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/landings/{landing_id}", response_model=LandingDetail)
async def get_landing(
    landing_id: int,
    session: AsyncSession = Depends(get_session),
) -> LandingDetail:
    result = await session.execute(
        select(Landing, DcsObject)
        .join(DcsObject, Landing.object_id == DcsObject.id, isouter=True)
        .where(Landing.id == landing_id)
    )
    row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail="landing not found")
    landing, obj = row

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
        **_summary(landing, obj).model_dump(),
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


@router.post("/landings/{landing_id}/regrade", response_model=RegradeResponse)
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
        raise HTTPException(status_code=503, detail="grading pipeline unavailable")

    overrides = body.overrides if body is not None else None
    payload = await pipeline.regrade(landing, overrides=overrides)
    return RegradeResponse(**payload)


# ---------------------------------------------------------------------------
# WebSocket: real-time landing notifications (FR-5)
# ---------------------------------------------------------------------------


@router.websocket("/ws/landings")
async def ws_landings(websocket: WebSocket) -> None:
    notifier = getattr(websocket.app.state, "notifier", None)
    if notifier is None:
        await websocket.close(code=1013)
        return
    await notifier.connect(websocket)
    try:
        while True:
            # Clients are receive-idle; any message other than ping closes.
            message = await websocket.receive_text()
            if message.strip().lower() == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        notifier.disconnect(websocket)
