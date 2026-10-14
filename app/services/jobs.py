"""Background execution of optimization runs.

A run is created ``queued`` by the API, then ``execute_run`` is scheduled with
FastAPI's BackgroundTasks, so it runs in this same process right after the response
is sent. That's simple and fine for one server; if you later run several workers or
need runs to survive restarts, swap this for a real queue (ARQ, Celery, ...). The
database is never held open during the solve (Neon bills compute time and a long
idle transaction is wasteful).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from optimizer.geo import Stop

from ..models import OptimizationRun, RunStop, Trip, TripStop, utcnow
from .engine import EngineConfig, EngineError, run_engine

logger = logging.getLogger(__name__)

MAX_ERROR_LENGTH = 1000


async def _mark_failed(
    sessionmaker: async_sessionmaker[AsyncSession], run_id: uuid.UUID, message: str
) -> None:
    async with sessionmaker() as session:
        await session.execute(
            update(OptimizationRun)
            .where(OptimizationRun.id == run_id)
            .values(status="failed", error_message=message[:MAX_ERROR_LENGTH], finished_at=utcnow())
        )
        await session.commit()


async def execute_run(
    sessionmaker: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    config: EngineConfig,
    semaphore: asyncio.Semaphore,
) -> None:
    # Phase 1: claim the run and read what the solver needs.
    async with sessionmaker() as session:
        run = await session.get(OptimizationRun, run_id)
        if run is None or run.status != "queued":
            return
        trip = await session.get(Trip, run.trip_id)
        rows = list(
            await session.scalars(
                select(TripStop).where(TripStop.trip_id == run.trip_id).order_by(TripStop.input_position)
            )
        )
        run.status = "running"
        await session.commit()
        stops = [Stop(r.name, r.lat, r.lng) for r in rows]
        stop_ids = [r.id for r in rows]
        start_index = next((i for i, r in enumerate(rows) if r.is_start), 0)
        open_path, weight_time, weight_dist = trip.open_path, trip.weight_time, trip.weight_dist

    # Phase 2: solve in a worker thread, limited to a few at a time.
    try:
        async with semaphore:
            result = await run_in_threadpool(
                run_engine,
                stops,
                start_index=start_index,
                open_path=open_path,
                weight_time=weight_time,
                weight_dist=weight_dist,
                config=config,
            )
    except EngineError as exc:
        await _mark_failed(sessionmaker, run_id, str(exc))
        return
    except Exception:  # noqa: BLE001 - never leave a run stuck in "running"
        logger.exception("Unexpected error while solving run %s", run_id)
        await _mark_failed(sessionmaker, run_id, "Unexpected error while optimizing this trip.")
        return

    # Phase 3: store the result.
    try:
        async with sessionmaker() as session:
            run = await session.get(OptimizationRun, run_id)
            for visit_order, stop_index in enumerate(result.order):
                leg_distance, leg_duration = result.legs[visit_order]
                session.add(
                    RunStop(
                        run_id=run_id,
                        trip_stop_id=stop_ids[stop_index],
                        visit_order=visit_order,
                        leg_distance_m=leg_distance,
                        leg_duration_s=leg_duration,
                    )
                )
            run.status = "succeeded"
            run.solver = result.solver
            run.matrix_backend = result.matrix_backend
            run.total_distance_m = result.total_distance_m
            run.total_duration_s = result.total_duration_s
            run.percent_saved = result.percent_saved
            run.route_polyline = result.polyline
            run.geometry_is_fallback = result.geometry_is_fallback
            run.finished_at = utcnow()
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("Could not store the result of run %s", run_id)
        await _mark_failed(sessionmaker, run_id, "The route was computed but could not be saved.")


async def sweep_stale_runs(
    sessionmaker: async_sessionmaker[AsyncSession], older_than_minutes: int
) -> int:
    """Fail runs stuck in queued/running longer than the timeout (e.g. the server was
    restarted mid-solve). Returns how many were marked failed."""
    cutoff = utcnow() - timedelta(minutes=older_than_minutes)
    async with sessionmaker() as session:
        result = await session.execute(
            update(OptimizationRun)
            .where(OptimizationRun.status.in_(("queued", "running")), OptimizationRun.created_at < cutoff)
            .values(
                status="failed",
                error_message="Interrupted: the server restarted or the job timed out before it finished.",
                finished_at=utcnow(),
            )
        )
        await session.commit()
        return result.rowcount or 0
