"""FastAPI application factory.

Run it with:  uvicorn app.main:create_app --factory --reload
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncEngine

from . import __version__
from .config import PUBLIC_OSRM_DEMO, Settings, get_settings
from .db import build_engine, build_sessionmaker
from .emailer import EmailSender, build_email_sender
from .routers import auth, health, optimize, trips
from .services.geocoding import Geocoder, build_geocoder
from .services.jobs import sweep_stale_runs
from .services.refresh_tokens import purge_expired

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    engine: AsyncEngine | None = None,
    email_sender: EmailSender | None = None,
    geocoder: Geocoder | None = None,
) -> FastAPI:
    """Build the app. The keyword arguments let tests swap in fakes; in production
    everything comes from the settings."""
    settings = settings or get_settings()
    engine = engine or build_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        disable_statement_cache=settings.db_disable_statement_cache,
    )
    sessionmaker = build_sessionmaker(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.environment == "production" and settings.matrix_backend == "osrm":
            if settings.osrm_url.rstrip("/") == PUBLIC_OSRM_DEMO:
                logger.warning(
                    "OSRM_URL points at the public demo server, which is for development only. "
                    "Self-host OSRM before real use."
                )
        try:
            failed = await sweep_stale_runs(sessionmaker, settings.run_timeout_minutes)
            if failed:
                logger.warning("Marked %d stuck optimization run(s) as failed", failed)
        except Exception:  # noqa: BLE001 - e.g. database still waking up; don't block startup
            logger.warning("Could not clean up stale runs at startup", exc_info=True)
        try:
            async with sessionmaker() as session:
                await purge_expired(session)
                await session.commit()
        except Exception:  # noqa: BLE001 - housekeeping only
            logger.warning("Could not purge expired refresh tokens at startup", exc_info=True)
        yield
        await engine.dispose()

    app = FastAPI(
        title="Route Optimizer API",
        version=__version__,
        description="Plot delivery stops and get the best visiting order, saved per user.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = sessionmaker
    app.state.email_sender = email_sender or build_email_sender(settings)
    app.state.geocoder = geocoder or build_geocoder(settings)
    app.state.solve_semaphore = asyncio.Semaphore(settings.max_concurrent_solves)

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(trips.router)
    app.include_router(optimize.router)
    return app
