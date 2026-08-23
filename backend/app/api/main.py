"""FastAPI application factory."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from logging import getLogger

from fastapi import FastAPI

from app.acmi.stream import AcmiStreamClient
from app.api.routes import router as api_router
from app.config import Settings
from app.ingest import TrackIngestor
from app.models.database import create_engine, create_session_factory, init_db

logger = getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await init_db(engine)

        acmi_client: AcmiStreamClient | None = None
        acmi_task: asyncio.Task | None = None
        if settings.acmi_enabled:
            ingestor = TrackIngestor(session_factory)
            acmi_client = AcmiStreamClient(
                settings.tacview_host,
                settings.tacview_port,
                ingestor.handle_line,
                initial_delay=settings.reconnect_initial_delay,
                max_delay=settings.reconnect_max_delay,
            )
            acmi_task = asyncio.create_task(acmi_client.run(), name="acmi-stream-client")
            logger.info(
                "ACMI client started (target %s:%d)",
                settings.tacview_host,
                settings.tacview_port,
            )
        else:
            logger.info("ACMI client disabled by configuration")

        app.state.settings = settings
        app.state.session_factory = session_factory
        app.state.acmi_client = acmi_client

        yield

        if acmi_client is not None and acmi_task is not None:
            await acmi_client.stop()
            acmi_task.cancel()
            try:
                await acmi_task
            except asyncio.CancelledError:
                pass
        await engine.dispose()

    app = FastAPI(
        title="DCS Landing Teacher",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(api_router)
    return app
