"""Small helpers shared by the trips and optimize routers."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from fastapi import BackgroundTasks, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..schemas.trips import StopIn
from ..services.engine import EngineConfig
from ..services.jobs import execute_run
from ..services.trips import GeocoderUnavailable, ResolvedStop, StopResolutionError, resolve_stops


def check_stop_count(count: int, settings: Settings) -> None:
    if count > settings.max_stops:
        raise HTTPException(
            422,
            f"A trip can have at most {settings.max_stops} stops (got {count}).",
        )


async def resolve_or_http(
    session: AsyncSession, request: Request, settings: Settings, stops: Sequence[StopIn]
) -> list[ResolvedStop]:
    """Resolve stops to coordinates, turning failures into proper HTTP errors."""
    try:
        return await resolve_stops(
            session, request.app.state.geocoder, settings.geocoder_country or None, stops
        )
    except StopResolutionError as exc:
        raise HTTPException(
            422,
            {"message": "Some addresses could not be located.", "unresolved": exc.failures},
        ) from None
    except GeocoderUnavailable:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Address lookup is unavailable right now. Send coordinates (lat/lng) instead, or try again.",
        ) from None


def schedule_run(request: Request, background_tasks: BackgroundTasks, run_id: uuid.UUID) -> None:
    """Run the optimization after the response has been sent (inline mode). In worker
    mode the queued run is already in the database and the worker picks it up."""
    if request.app.state.settings.run_mode == "worker":
        return
    background_tasks.add_task(
        execute_run,
        request.app.state.sessionmaker,
        run_id,
        EngineConfig.from_settings(request.app.state.settings),
        request.app.state.solve_semaphore,
    )
