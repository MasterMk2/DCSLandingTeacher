"""REST API routes."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api")


@router.get("/health")
async def health(request: Request) -> dict:
    settings = request.app.state.settings
    client = getattr(request.app.state, "acmi_client", None)
    return {
        "status": "ok",
        "version": request.app.version,
        "acmi_enabled": settings.acmi_enabled,
        "acmi_connected": bool(client.connected) if client is not None else False,
    }
