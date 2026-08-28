"""FastAPI application factory."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from logging import getLogger
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy import update
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.acmi.multi_source import MultiSourceAcmiManager
from app.acmi.stream import AcmiStreamClient
from app.api.errors import AppError, error_envelope
from app.api.imports import router as import_router
from app.api.notifier import LandingNotifier
from app.api.routes import protected_router, router as api_router
from app.config import Settings
from app.grading.carriers import load_carrier_geometry_book
from app.grading.config import load_grading_config
from app.importer import ImportJobManager
from app.logging_config import configure_logging
from app.models.database import create_engine, create_session_factory, init_db
from app.models.entities import Landing
from app.models.migrations import run_migrations
from app.pipeline import LandingPipeline
from app.runways.dcssb import DcssbClient
from app.runways.provider import RunwayProvider

logger = getLogger(__name__)

# How often the grading config file is polled for changes (Issue #40). A
# watchdog dependency would be heavier; mtime polling is dependency-free and
# plenty responsive for operator-edited thresholds.
CONFIG_RELOAD_POLL_S = 5.0


async def _poll_grading_config(app: FastAPI, pipeline: LandingPipeline, path: Path) -> None:
    """Reload grading thresholds when the YAML file changes on disk (Issue #40)."""
    last_mtime = path.stat().st_mtime if path.is_file() else None
    while True:
        try:
            await asyncio.sleep(CONFIG_RELOAD_POLL_S)
            if not path.is_file():
                continue
            mtime = path.stat().st_mtime
            if mtime != last_mtime:
                last_mtime = mtime
                pipeline.reload_config()
                app.state.grading_config = pipeline._config
                app.state.config_reload_total = getattr(app.state, "config_reload_total", 0) + 1
                logger.info("grading config hot-reloaded from %s", path)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the watcher alive on any error
            logger.exception("grading config poll failed")


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


# How often stale provisional landings are reaped, and how old they must be
# before reaping (Issue #36). A provisional whose final detection never
# arrives (track ends, crash, restart) would otherwise stay "provisional"
# forever; the reaper force-finalizes them with their recorded outcome.
PROVISIONAL_REAP_INTERVAL_S = 60.0
PROVISIONAL_MAX_AGE_S = 300.0


async def _settle_stale_provisionals(session_factory, cutoff: datetime) -> int:
    """Finalize provisionals older than ``cutoff``; returns rows settled.

    The recorded outcome is kept -- it is the best evidence for a landing whose
    observation was cut short (Issue #36).
    """
    async with session_factory() as session:
        result = await session.execute(
            update(Landing)
            .where(Landing.outcome_status == "provisional")
            .where(Landing.created_at < cutoff)
            .values(outcome_status="final")
        )
        await session.commit()
    return result.rowcount or 0


async def _reap_stale_provisionals_task(
    app: FastAPI, session_factory, interval_s: float, max_age_s: float
) -> None:
    """Background loop that reaps stale provisional landings (Issue #36)."""
    while True:
        try:
            await asyncio.sleep(interval_s)
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_s)
            reaped = await _settle_stale_provisionals(session_factory, cutoff)
            if reaped:
                logger.warning("reaped %d stale provisional landing(s)", reaped)
                app.state.provisional_reaped_total = (
                    getattr(app.state, "provisional_reaped_total", 0) + reaped
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the reaper alive on any error
            logger.exception("stale provisional reaper failed")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    # Structured, machine-parseable logs from the first line (Issue #32).
    configure_logging(json_logs=settings.structured_logs)
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
        runway_provider = _build_runway_provider(settings)
        pipeline = LandingPipeline(
            session_factory,
            grading_config,
            notifier=notifier,
            carrier_geometry_book=carrier_geometry_book,
            runway_provider=runway_provider,
            grading_config_path=settings.grading_config_path,
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
        # Expose the live grading config so operators/reload can read it back
        # (Issue #40).
        app.state.grading_config = grading_config
        app.state.config_reload_total = 0
        app.state.provisional_reaped_total = 0
        # Rebuild import-job history from the database so jobs created before a
        # previous restart remain queryable (Issue #28).
        await import_manager.load_persisted()

        config_path = Path(settings.grading_config_path)
        config_watch_task = None
        if config_path.is_file():
            config_watch_task = asyncio.create_task(
                _poll_grading_config(app, pipeline, config_path)
            )
        # Reap provisional landings whose final detection never arrived
        # (Issue #36).
        reaper_task = asyncio.create_task(
            _reap_stale_provisionals_task(
                app,
                session_factory,
                PROVISIONAL_REAP_INTERVAL_S,
                PROVISIONAL_MAX_AGE_S,
            )
        )

        # Sweep scratch imports left behind by abandoned tabs or a restart.
        purged = await import_manager.purge_expired(settings.import_retention_hours)
        if purged:
            logger.info("purged %d expired import(s)", purged)

        yield

        if config_watch_task is not None:
            config_watch_task.cancel()
            try:
                await config_watch_task
            except asyncio.CancelledError:
                pass
        reaper_task.cancel()
        try:
            await reaper_task
        except asyncio.CancelledError:
            pass
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

    # Standard error envelope (Issue #42) so clients can branch on a stable
    # ``error`` code instead of parsing message text.
    app.add_exception_handler(AppError, _handle_app_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    return app


def _build_runway_provider(settings: Settings) -> RunwayProvider | None:
    """Runway provider, or ``None`` when DCSServerBot is not configured.

    Without it land landings fall back to the touchdown-referenced
    approximation, which is less accurate but needs no external service.
    """
    if not settings.dcssb_base_url:
        logger.info("DCSSB not configured; land grading uses estimated geometry")
        return None
    client = DcssbClient(
        settings.dcssb_base_url,
        api_prefix=settings.dcssb_api_prefix,
        api_key=settings.dcssb_api_key,
        request_spacing_ms=settings.dcssb_request_spacing_ms,
        timeout_s=settings.dcssb_timeout_s,
    )
    logger.info("DCSSB runway source: %s", settings.dcssb_base_url)
    return RunwayProvider(
        client,
        settings.runway_cache_dir,
        server_name=settings.dcssb_server_name,
    )


async def _handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
    return error_envelope(exc.status_code, exc.error_code, exc.message, exc.details)


async def _handle_validation_error(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    return error_envelope(
        422, "VALIDATION_ERROR", "Request validation failed", {"errors": exc.errors()}
    )


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
