"""Background execution of optimization runs.

A run is created ``queued`` by the API. Two ways to execute it:

* ``RUN_MODE=inline`` (default): ``execute_run`` is scheduled with FastAPI's
  BackgroundTasks and runs in the API process right after the response is sent.
  Simple, but a restart loses the run and there are no retries.
* ``RUN_MODE=worker``: a separate process (``python -m app.worker``, see
  ``services/worker.py``) claims queued runs from the database, keeps a heartbeat
  while solving, retries transient failures and recovers runs whose worker died.
  It calls ``execute_run`` with a ``WorkerContext``.

The database is never held open during the solve (Neon bills compute time and a long
idle transaction is wasteful).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from optimizer.geo import Stop

from ..models import OptimizationRun, RunStop, Trip, TripStop, utcnow
from .engine import EngineConfig, EngineError, RoutingServiceError, run_engine

logger = logging.getLogger(__name__)

MAX_ERROR_LENGTH = 1000


@dataclass(frozen=True)
class WorkerContext:
    """Present when a worker (not the API process) executes the run, which has already
    claimed it (status running, locked_by this worker)."""

    worker_id: str
    lease_seconds: int
    max_attempts: int
    retry_backoff_s: int


def _owned(run_id: uuid.UUID, worker: WorkerContext | None) -> list:
    """WHERE conditions meaning "this run is still mine to finish"."""
    if worker is None:
        return [OptimizationRun.id == run_id]
    return [
        OptimizationRun.id == run_id,
        OptimizationRun.status == "running",
        OptimizationRun.locked_by == worker.worker_id,
    ]


async def _fail_or_retry(
    sessionmaker: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    message: str,
    *,
    retryable: bool,
    worker: WorkerContext | None,
) -> None:
    """Mark the run failed, or (worker mode, transient error, attempts left) put it back
    in the queue after a back-off."""
    async with sessionmaker() as session:
        where = _owned(run_id, worker)
        if worker is not None and retryable:
            attempts = await session.scalar(select(OptimizationRun.attempts).where(*where))
            if attempts is not None and attempts < worker.max_attempts:
                note = f"Attempt {attempts} of {worker.max_attempts} failed, retrying: {message}"
                await session.execute(
                    update(OptimizationRun)
                    .where(*where)
                    .values(
                        status="queued",
                        locked_by=None,
                        heartbeat_at=None,
                        run_after=utcnow() + timedelta(seconds=worker.retry_backoff_s * attempts),
                        error_message=note[:MAX_ERROR_LENGTH],
                    )
                )
                await session.commit()
                return
        await session.execute(
            update(OptimizationRun)
            .where(*where)
            .values(status="failed", error_message=message[:MAX_ERROR_LENGTH], finished_at=utcnow())
        )
        await session.commit()


async def _heartbeat(
    sessionmaker: async_sessionmaker[AsyncSession], run_id: uuid.UUID, worker: WorkerContext
) -> None:
    """Keep the lease alive while the solve runs. Stops if the run was taken away."""
    interval = max(worker.lease_seconds / 3, 1)
    while True:
        await asyncio.sleep(interval)
        try:
            async with sessionmaker() as session:
                result = await session.execute(
                    update(OptimizationRun).where(*_owned(run_id, worker)).values(heartbeat_at=utcnow())
                )
                await session.commit()
            if not result.rowcount:
                logger.warning("Run %s is no longer owned by worker %s", run_id, worker.worker_id)
                return
        except Exception:  # noqa: BLE001 - a missed beat is survivable, the next one may work
            logger.warning("Heartbeat for run %s failed", run_id, exc_info=True)


async def execute_run(
    sessionmaker: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    config: EngineConfig,
    semaphore: asyncio.Semaphore,
    worker: WorkerContext | None = None,
) -> None:
    # Phase 1: claim the run (inline mode) and read what the solver needs.
    async with sessionmaker() as session:
        run = await session.get(OptimizationRun, run_id)
        if worker is None:
            if run is None or run.status != "queued":
                return
        elif run is None or run.status != "running" or run.locked_by != worker.worker_id:
            return  # lost or never owned: someone else is responsible for it
        trip = await session.get(Trip, run.trip_id)
        rows = list(
            await session.scalars(
                select(TripStop).where(TripStop.trip_id == run.trip_id).order_by(TripStop.input_position)
            )
        )
        if worker is None:
            run.status = "running"
            await session.commit()
        stops = [Stop(r.name, r.lat, r.lng) for r in rows]
        stop_ids = [r.id for r in rows]
        start_index = next((i for i, r in enumerate(rows) if r.is_start), 0)
        open_path, weight_time, weight_dist = trip.open_path, trip.weight_time, trip.weight_dist

    beat = asyncio.create_task(_heartbeat(sessionmaker, run_id, worker)) if worker else None
    try:
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
            # A routing-service outage is worth retrying; bad input never is.
            await _fail_or_retry(
                sessionmaker, run_id, str(exc), retryable=isinstance(exc, RoutingServiceError), worker=worker
            )
            return
        except Exception:  # noqa: BLE001 - never leave a run stuck in "running"
            logger.exception("Unexpected error while solving run %s", run_id)
            await _fail_or_retry(
                sessionmaker, run_id, "Unexpected error while optimizing this trip.", retryable=True, worker=worker
            )
            return

        # Phase 3: store the result, but only if the run is still ours.
        try:
            async with sessionmaker() as session:
                claimed = await session.execute(
                    update(OptimizationRun)
                    .where(*_owned(run_id, worker))
                    .values(
                        status="succeeded",
                        error_message=None,
                        solver=result.solver,
                        matrix_backend=result.matrix_backend,
                        total_distance_m=result.total_distance_m,
                        total_duration_s=result.total_duration_s,
                        percent_saved=result.percent_saved,
                        route_polyline=result.polyline,
                        geometry_is_fallback=result.geometry_is_fallback,
                        finished_at=utcnow(),
                    )
                )
                if not claimed.rowcount:
                    await session.rollback()
                    logger.warning("Run %s was reassigned while solving; discarding this result", run_id)
                    return
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
                await session.commit()
        except Exception:  # noqa: BLE001
            logger.exception("Could not store the result of run %s", run_id)
            await _fail_or_retry(
                sessionmaker, run_id, "The route was computed but could not be saved.", retryable=True, worker=worker
            )
    finally:
        if beat is not None:
            beat.cancel()


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
