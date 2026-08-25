"""FastAPI application factory."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from logging import getLogger
from pathlib import Path

from fastapi import FastAPI, HTTPException
from sqlalchemy import update
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.acmi.multi_source import MultiSourceAcmiManager
from app.acmi.stream import AcmiStreamClient
from app.api.imports import router as import_router
from app.api.notifier import LandingNotifier
from app.api.routes import protected_router, router as api_router
from app.config import Settings
from app.grading.carriers import load_carrier_geometry_book
from app.grading.config import load_grading_config
from app.ingest import TrackIngestor
from app.importer import ImportJobManager
from app.models.database import create_engine, create_session_factory, init_db
from app.models.entities import Landing
from app.models.migrations import run_migrations
from app.pipeline import LandingPipeline

logger = getLogger(__name__)


async def settle_orphaned_provisionals(session_factory) -> int:
    """Mark leftover ``provisional`` landings as final at startup (Issue #5).

    Two-phase confirmation tracks pending rows in the ingestor's in-memory
    map, so a row still ``provisional`` when the process starts can never be
    confirmed: nothing is left to correlate it with. Without this it shows
    "評価中" in the UI forever.

    The recorded outcome is kept as-is -- it is the best evidence available
    for a landing whose observation was cut short.
    """
    async with session_factory() as session:
        result = await session.execute(
            update(Landing)
            .where(Landing.outcome_status == "provisional")
            .values(outcome_status="final")
        )
        await session.commit()
    settled = result.rowcount or 0
    if settled:
        logger.warning(
            "settled %d landing(s) left provisional by a previous run", settled
        )
    return settled


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    grading_config = load_grading_config(settings.grading_config_path)
    carrier_geometry_book = load_carrier_geometry_book(settings.carriers_config_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.migrations_on_startup:
            await run_migrations(
                settings.database_url, settings.migrations_dir or None
            )
        else:
            await init_db(engine)

        await settle_orphaned_provisionals(session_factory)

        notifier = LandingNotifier()
        pipeline = LandingPipeline(
            session_factory,
            grading_config,
            notifier=notifier,
            carrier_geometry_book=carrier_geometry_book,
        )

        multi_source_manager: MultiSourceAcmiManager | None = None
        acmi_client: AcmiStreamClient | None = None  # Legacy compatibility
        if settings.acmi_enabled and settings.tacview_enabled:
            multi_source_manager = MultiSourceAcmiManager(
                session_factory,
                landing_listener=pipeline.handle_landing,
                landing_finalize_listener=pipeline.finalize_landing,
            )
            await multi_source_manager.start()
            # Legacy compatibility: expose first source's client as acmi_client
            first_source = multi_source_manager.get_source("default")
            if first_source is not None:
                acmi_client = first_source.client
            logger.info("Multi-source ACMI manager started with %d source(s)", len(multi_source_manager._sources))
        else:
            logger.info("ACMI client disabled by configuration")

        import_manager = ImportJobManager(
            session_factory,
            pipeline,
            notifier=notifier,
            sample_buffer_s=float(
                grading_config.detection.get("sample_buffer_s", 600.0)
            ),
        )

        app.state.settings = settings
        app.state.session_factory = session_factory
        app.state.acmi_client = acmi_client
        app.state.multi_source_manager = multi_source_manager
        app.state.notifier = notifier
        app.state.pipeline = pipeline
        app.state.import_manager = import_manager

        yield

        if multi_source_manager is not None:
            await multi_source_manager.stop()
        await engine.dispose()

    app = FastAPI(
        title="DCS Landing Teacher",
        version="0.3.0",
        lifespan=lifespan,
    )

    # CORS: only enabled when explicitly configured. The default (empty list)
    # is the single-container deployment where this app also serves the
    # frontend, so no cross-origin requests are expected.
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

    app.include_router(api_router)
    # Token-protected landing endpoints (Issue #8); see app.api.auth.
    app.include_router(protected_router)
    # Token-protected ACMI file import endpoints (background jobs).
    app.include_router(import_router)
    _mount_frontend(app, Path(settings.frontend_dist_dir))
    return app


def _mount_frontend(app: FastAPI, dist_dir: Path) -> None:
    """Serve the built frontend (SPA) when ``frontend/dist`` exists.

    Vite emits ``index.html`` plus an ``assets/`` directory. Static assets are
    served via StaticFiles; every other non-API GET path falls back to
    ``index.html`` so client-side routing keeps working.
    """
    if not dist_dir.is_dir():
        logger.info("Frontend dist not found at %s; API-only mode", dist_dir)
        return

    assets_dir = dist_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    dist_root = str(dist_dir.resolve())

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> FileResponse:
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404)
        candidate = (dist_dir / full_path).resolve()
        if full_path and candidate.is_file() and str(candidate).startswith(dist_root):
            return FileResponse(candidate)
        return FileResponse(dist_dir / "index.html")

    logger.info("Serving frontend from %s", dist_dir)
