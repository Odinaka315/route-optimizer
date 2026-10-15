"""The queue side of the separate worker: claim runs, recover dead ones, run the loop.

The ``optimization_runs`` table *is* the queue. A worker claims the oldest queued run in
one short transaction (``SELECT ... FOR UPDATE SKIP LOCKED`` on PostgreSQL, so many
workers never grab the same run; plus a guarded ``UPDATE`` that also works on SQLite),
heartbeats while it solves, and a lease reaper puts runs back in the queue (or fails
them once attempts are used up) when a worker stops heartbeating.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid
from datetime import timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..models import OptimizationRun, utcnow
from .engine import EngineConfig
from .jobs import WorkerContext, execute_run

logger = logging.getLogger(__name__)

RECLAIM_EVERY_S = 15.0


def default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


async def claim_next_run(
    sessionmaker: async_sessionmaker[AsyncSession], worker_id: str
) -> uuid.UUID | None:
    """Atomically take the oldest runnable queued run, or return None."""
    now = utcnow()
    async with sessionmaker() as session:
        run_id = await session.scalar(
            select(OptimizationRun.id)
            .where(
                OptimizationRun.status == "queued",
                or_(OptimizationRun.run_after.is_(None), OptimizationRun.run_after <= now),
            )
            .order_by(OptimizationRun.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if run_id is None:
            return None
        claimed = await session.execute(
            update(OptimizationRun)
            .where(OptimizationRun.id == run_id, OptimizationRun.status == "queued")
            .values(
                status="running",
                attempts=OptimizationRun.attempts + 1,
                started_at=now,
                heartbeat_at=now,
                locked_by=worker_id,
                run_after=None,
                error_message=None,
                finished_at=None,
            )
        )
        await session.commit()
        return run_id if claimed.rowcount == 1 else None


async def reclaim_expired_runs(
    sessionmaker: async_sessionmaker[AsyncSession], lease_seconds: int, max_attempts: int
) -> tuple[int, int]:
    """Recover runs whose worker stopped heartbeating. Returns (requeued, failed)."""
    now = utcnow()
    cutoff = now - timedelta(seconds=lease_seconds)
    last_sign_of_life = func.coalesce(
        OptimizationRun.heartbeat_at, OptimizationRun.started_at, OptimizationRun.created_at
    )
    stale = (OptimizationRun.status == "running", last_sign_of_life < cutoff)
    async with sessionmaker() as session:
        requeued = await session.execute(
            update(OptimizationRun)
            .where(*stale, OptimizationRun.attempts < max_attempts)
            .values(
                status="queued",
                locked_by=None,
                heartbeat_at=None,
                run_after=None,
                error_message="The worker stopped responding; retrying.",
            )
        )
        failed = await session.execute(
            update(OptimizationRun)
            .where(*stale, OptimizationRun.attempts >= max_attempts)
            .values(
                status="failed",
                locked_by=None,
                finished_at=now,
                error_message="The worker stopped responding and no attempts are left.",
            )
        )
        await session.commit()
        return requeued.rowcount or 0, failed.rowcount or 0


async def release_worker_runs(
    sessionmaker: async_sessionmaker[AsyncSession], worker_id: str
) -> int:
    """On shutdown: hand this worker's unfinished runs straight back to the queue
    (without counting the attempt) instead of making others wait out the lease."""
    async with sessionmaker() as session:
        result = await session.execute(
            update(OptimizationRun)
            .where(OptimizationRun.status == "running", OptimizationRun.locked_by == worker_id)
            .values(
                status="queued",
                attempts=OptimizationRun.attempts - 1,
                locked_by=None,
                heartbeat_at=None,
                error_message=None,
            )
        )
        await session.commit()
        return result.rowcount or 0


class Worker:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        worker_id: str | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.settings = settings
        self.worker_id = worker_id or default_worker_id()
        self.config = EngineConfig.from_settings(settings)
        self.context = WorkerContext(
            worker_id=self.worker_id,
            lease_seconds=settings.run_lease_seconds,
            max_attempts=settings.run_max_attempts,
            retry_backoff_s=settings.run_retry_backoff_s,
        )
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_solves)

    async def _execute(self, run_id: uuid.UUID) -> None:
        try:
            await execute_run(self.sessionmaker, run_id, self.config, self.semaphore, self.context)
        except Exception:  # noqa: BLE001 - one bad run must not stop the worker
            logger.exception("Run %s crashed the executor", run_id)

    async def reclaim(self) -> None:
        requeued, failed = await reclaim_expired_runs(
            self.sessionmaker, self.settings.run_lease_seconds, self.settings.run_max_attempts
        )
        if requeued or failed:
            logger.warning("Recovered runs from dead workers: %d requeued, %d failed", requeued, failed)

    async def process_available(self) -> int:
        """Claim and run queued runs one at a time until none are runnable. For tests
        and one-off drains; ``run_forever`` is the real loop."""
        done = 0
        while (run_id := await claim_next_run(self.sessionmaker, self.worker_id)) is not None:
            await self._execute(run_id)
            done += 1
        return done

    async def run_forever(self, stop: asyncio.Event) -> None:
        logger.info(
            "Worker %s started (concurrency %d, poll %.1fs)",
            self.worker_id,
            self.settings.max_concurrent_solves,
            self.settings.worker_poll_interval_s,
        )
        tasks: set[asyncio.Task] = set()
        last_reclaim = float("-inf")
        while not stop.is_set():
            try:
                if time.monotonic() - last_reclaim >= RECLAIM_EVERY_S:
                    await self.reclaim()
                    last_reclaim = time.monotonic()
                claimed_any = False
                while len(tasks) < self.settings.max_concurrent_solves and not stop.is_set():
                    run_id = await claim_next_run(self.sessionmaker, self.worker_id)
                    if run_id is None:
                        break
                    logger.info("Claimed run %s", run_id)
                    task = asyncio.create_task(self._execute(run_id))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                    claimed_any = True
                if claimed_any:
                    continue
            except Exception:  # noqa: BLE001 - e.g. the database is waking up; keep going
                logger.warning("Worker loop error, will retry", exc_info=True)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.settings.worker_poll_interval_s)
            except asyncio.TimeoutError:
                pass

        logger.info("Worker %s stopping; waiting up to %ds for %d run(s)", self.worker_id,
                    self.settings.worker_shutdown_grace_s, len(tasks))
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self.settings.worker_shutdown_grace_s)
            for task in pending:
                task.cancel()
        try:
            released = await release_worker_runs(self.sessionmaker, self.worker_id)
            if released:
                logger.info("Handed %d unfinished run(s) back to the queue", released)
        except Exception:  # noqa: BLE001
            logger.warning("Could not release unfinished runs; the lease will expire instead", exc_info=True)
