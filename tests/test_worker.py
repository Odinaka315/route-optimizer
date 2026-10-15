"""The separate worker: claiming, retries, crash recovery, and the API in worker mode."""

import asyncio
import uuid
from datetime import timedelta

from sqlalchemy import select, update

from app.models import OptimizationRun, RunStop, utcnow
from app.services import jobs
from app.services.engine import EngineError, RoutingServiceError
from app.services.worker import (
    Worker,
    claim_next_run,
    reclaim_expired_runs,
    release_worker_runs,
)

from .conftest import stops


async def make_trip(env, headers, title="Morning run") -> dict:
    r = await env.client.post("/trips", headers=headers, json={"title": title, "stops": stops()})
    assert r.status_code == 201, r.text
    return r.json()


async def get_trip(env, headers, trip_id) -> dict:
    return (await env.client.get(f"/trips/{trip_id}", headers=headers)).json()


async def run_row(sessionmaker, run_id) -> OptimizationRun:
    async with sessionmaker() as session:
        return await session.get(OptimizationRun, uuid.UUID(str(run_id)))


async def worker_env(app_factory, **overrides):
    return await app_factory(run_mode="worker", **overrides)


def make_worker(env, sessionmaker) -> Worker:
    return Worker(sessionmaker, env.app.state.settings, worker_id="test-worker")


# --- the API only queues in worker mode -------------------------------------------------


async def test_api_in_worker_mode_leaves_the_run_queued_until_a_worker_takes_it(
    app_factory, sessionmaker
):
    env = await worker_env(app_factory)
    auth = await env.register()
    created = await make_trip(env, auth)
    trip = await get_trip(env, auth, created["id"])
    assert trip["latest_run"]["status"] == "queued" and trip["route"] is None

    assert await make_worker(env, sessionmaker).process_available() == 1

    trip = await get_trip(env, auth, created["id"])
    assert trip["latest_run"]["status"] == "succeeded"
    assert len(trip["route"]["stops"]) == 6 and trip["route"]["polyline"]


async def test_inline_mode_is_unchanged(env, auth, sessionmaker):
    created = (await env.client.post("/trips", headers=auth, json={"title": "x", "stops": stops()})).json()
    assert (await get_trip(env, auth, created["id"]))["latest_run"]["status"] == "succeeded"
    assert await claim_next_run(sessionmaker, "w") is None  # nothing left for a worker


async def test_rerun_and_modify_also_queue_for_the_worker(app_factory, sessionmaker):
    env = await worker_env(app_factory)
    auth = await env.register()
    trip_id = (await make_trip(env, auth))["id"]
    worker = make_worker(env, sessionmaker)
    await worker.process_available()

    assert (await env.client.post(f"/trips/{trip_id}/runs", headers=auth)).status_code == 202
    copy = (await env.client.post(f"/trips/{trip_id}/modify", headers=auth, json={"title": "copy"})).json()
    assert await worker.process_available() == 2
    assert (await get_trip(env, auth, copy["id"]))["latest_run"]["status"] == "succeeded"
    assert len((await env.client.get(f"/trips/{trip_id}/runs", headers=auth)).json()) == 2


# --- claiming -------------------------------------------------------------------------------


async def seed_queued(env, auth, sessionmaker, count=1):
    ids = []
    for i in range(count):
        trip = await make_trip(env, auth, title=f"t{i}")
        ids.append(trip["latest_run"]["id"])
    return ids


async def test_claim_takes_the_oldest_and_marks_it_running(app_factory, sessionmaker):
    env = await worker_env(app_factory)
    auth = await env.register()
    first, second = await seed_queued(env, auth, sessionmaker, 2)
    run_id = await claim_next_run(sessionmaker, "w1")
    assert str(run_id) == first
    row = await run_row(sessionmaker, first)
    assert row.status == "running" and row.locked_by == "w1" and row.attempts == 1
    assert row.started_at is not None and row.heartbeat_at is not None
    assert str(await claim_next_run(sessionmaker, "w2")) == second
    assert await claim_next_run(sessionmaker, "w3") is None


async def test_concurrent_workers_never_claim_the_same_run(app_factory, sessionmaker):
    env = await worker_env(app_factory)
    auth = await env.register()
    ids = await seed_queued(env, auth, sessionmaker, 3)
    results = await asyncio.gather(*[claim_next_run(sessionmaker, f"w{i}") for i in range(8)])
    claimed = [str(r) for r in results if r is not None]
    assert len(claimed) == len(set(claimed)) <= 3
    assert set(claimed) <= set(ids)


async def test_a_run_waiting_for_its_retry_time_is_not_claimable(app_factory, sessionmaker):
    env = await worker_env(app_factory)
    auth = await env.register()
    (run_id,) = await seed_queued(env, auth, sessionmaker)
    async with sessionmaker() as session:
        await session.execute(update(OptimizationRun).values(run_after=utcnow() + timedelta(minutes=5)))
        await session.commit()
    assert await claim_next_run(sessionmaker, "w") is None
    async with sessionmaker() as session:
        await session.execute(update(OptimizationRun).values(run_after=utcnow() - timedelta(seconds=1)))
        await session.commit()
    assert str(await claim_next_run(sessionmaker, "w")) == run_id


# --- retries ---------------------------------------------------------------------------------


async def test_routing_outage_is_retried_then_succeeds(app_factory, sessionmaker, monkeypatch):
    env = await worker_env(app_factory, run_retry_backoff_s=0)
    auth = await env.register()
    trip_id = (await make_trip(env, auth))["id"]
    real, calls = jobs.run_engine, []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) < 3:
            raise RoutingServiceError("The routing service could not compute travel times: timeout")
        return real(*a, **kw)

    monkeypatch.setattr(jobs, "run_engine", flaky)
    await make_worker(env, sessionmaker).process_available()

    trip = await get_trip(env, auth, trip_id)
    assert trip["latest_run"]["status"] == "succeeded" and trip["latest_run"]["error_message"] is None
    assert len(calls) == 3
    assert (await run_row(sessionmaker, trip["latest_run"]["id"])).attempts == 3


async def test_retry_note_is_visible_while_waiting(app_factory, sessionmaker, monkeypatch):
    env = await worker_env(app_factory, run_retry_backoff_s=60)
    auth = await env.register()
    trip_id = (await make_trip(env, auth))["id"]

    def down(*a, **kw):
        raise RoutingServiceError("service down")

    monkeypatch.setattr(jobs, "run_engine", down)
    assert await make_worker(env, sessionmaker).process_available() == 1  # then waits out the back-off
    latest = (await get_trip(env, auth, trip_id))["latest_run"]
    assert latest["status"] == "queued" and "retrying" in latest["error_message"]


async def test_gives_up_after_the_last_attempt(app_factory, sessionmaker, monkeypatch):
    env = await worker_env(app_factory, run_retry_backoff_s=0, run_max_attempts=2)
    auth = await env.register()
    trip_id = (await make_trip(env, auth))["id"]
    calls = []

    def down(*a, **kw):
        calls.append(1)
        raise RoutingServiceError("service down")

    monkeypatch.setattr(jobs, "run_engine", down)
    await make_worker(env, sessionmaker).process_available()
    latest = (await get_trip(env, auth, trip_id))["latest_run"]
    assert len(calls) == 2 and latest["status"] == "failed" and "service down" in latest["error_message"]


async def test_bad_input_is_not_retried(app_factory, sessionmaker, monkeypatch):
    env = await worker_env(app_factory, run_retry_backoff_s=0)
    auth = await env.register()
    trip_id = (await make_trip(env, auth))["id"]
    calls = []

    def impossible(*a, **kw):
        calls.append(1)
        raise EngineError("There is no road route between 'A' and 'B'.")

    monkeypatch.setattr(jobs, "run_engine", impossible)
    await make_worker(env, sessionmaker).process_available()
    assert len(calls) == 1
    assert (await get_trip(env, auth, trip_id))["latest_run"]["status"] == "failed"


async def test_unexpected_errors_retry_without_leaking_internals(app_factory, sessionmaker, monkeypatch):
    env = await worker_env(app_factory, run_retry_backoff_s=0, run_max_attempts=2)
    auth = await env.register()
    trip_id = (await make_trip(env, auth))["id"]

    def explode(*a, **kw):
        raise RuntimeError("secret internals")

    monkeypatch.setattr(jobs, "run_engine", explode)
    await make_worker(env, sessionmaker).process_available()
    latest = (await get_trip(env, auth, trip_id))["latest_run"]
    assert latest["status"] == "failed" and "secret" not in latest["error_message"]


# --- crash recovery ------------------------------------------------------------------------------


async def stale_running(env, auth, sessionmaker, attempts=1, age_s=600):
    (run_id,) = await seed_queued(env, auth, sessionmaker)
    async with sessionmaker() as session:
        await session.execute(
            update(OptimizationRun).values(
                status="running",
                locked_by="dead-worker",
                attempts=attempts,
                started_at=utcnow() - timedelta(seconds=age_s),
                heartbeat_at=utcnow() - timedelta(seconds=age_s),
            )
        )
        await session.commit()
    return run_id


async def test_dead_workers_run_is_requeued_and_then_finished(app_factory, sessionmaker):
    env = await worker_env(app_factory)
    auth = await env.register()
    run_id = await stale_running(env, auth, sessionmaker)
    assert await reclaim_expired_runs(sessionmaker, lease_seconds=90, max_attempts=3) == (1, 0)
    row = await run_row(sessionmaker, run_id)
    assert row.status == "queued" and row.locked_by is None
    await make_worker(env, sessionmaker).process_available()
    assert (await run_row(sessionmaker, run_id)).status == "succeeded"


async def test_dead_run_with_no_attempts_left_fails(app_factory, sessionmaker):
    env = await worker_env(app_factory)
    auth = await env.register()
    run_id = await stale_running(env, auth, sessionmaker, attempts=3)
    assert await reclaim_expired_runs(sessionmaker, 90, 3) == (0, 1)
    row = await run_row(sessionmaker, run_id)
    assert row.status == "failed" and "no attempts are left" in row.error_message and row.finished_at


async def test_healthy_runs_keep_their_lease(app_factory, sessionmaker):
    env = await worker_env(app_factory)
    auth = await env.register()
    run_id = await stale_running(env, auth, sessionmaker, age_s=5)
    assert await reclaim_expired_runs(sessionmaker, 90, 3) == (0, 0)
    assert (await run_row(sessionmaker, run_id)).status == "running"


async def test_shutdown_hands_unfinished_runs_back_without_losing_an_attempt(app_factory, sessionmaker):
    env = await worker_env(app_factory)
    auth = await env.register()
    (run_id,) = await seed_queued(env, auth, sessionmaker)
    await claim_next_run(sessionmaker, "me")
    assert await release_worker_runs(sessionmaker, "someone-else") == 0
    assert await release_worker_runs(sessionmaker, "me") == 1
    row = await run_row(sessionmaker, run_id)
    assert row.status == "queued" and row.attempts == 0 and row.locked_by is None


async def test_a_result_from_a_worker_that_lost_the_run_is_discarded(app_factory, sessionmaker, monkeypatch):
    env = await worker_env(app_factory)
    auth = await env.register()
    trip_id = (await make_trip(env, auth))["id"]
    real = jobs.run_engine

    def slow_and_stolen(*a, **kw):
        result = real(*a, **kw)
        # While "solving", the lease expired and a second worker took the run over.
        import asyncio as _a

        async def steal():
            async with sessionmaker() as session:
                await session.execute(update(OptimizationRun).values(locked_by="worker-2"))
                await session.commit()

        _a.run_coroutine_threadsafe(steal(), loop).result()
        return result

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(jobs, "run_engine", slow_and_stolen)
    await make_worker(env, sessionmaker).process_available()

    latest = (await get_trip(env, auth, trip_id))["latest_run"]
    assert latest["status"] == "running"  # still worker-2's to finish
    async with sessionmaker() as session:
        assert (await session.scalars(select(RunStop))).first() is None


# --- the loop ---------------------------------------------------------------------------------------


async def test_run_forever_processes_runs_and_stops_cleanly(app_factory, sessionmaker):
    env = await worker_env(app_factory, worker_poll_interval_s=0.1, worker_shutdown_grace_s=5)
    auth = await env.register()
    ids = [(await make_trip(env, auth, title=f"t{i}"))["id"] for i in range(3)]
    worker = make_worker(env, sessionmaker)
    stop = asyncio.Event()
    loop_task = asyncio.create_task(worker.run_forever(stop))
    try:
        for _ in range(100):
            states = [(await get_trip(env, auth, i))["latest_run"]["status"] for i in ids]
            if set(states) == {"succeeded"}:
                break
            await asyncio.sleep(0.1)
        assert set(states) == {"succeeded"}
    finally:
        stop.set()
        await asyncio.wait_for(loop_task, timeout=10)


async def test_loop_survives_a_database_hiccup(app_factory, sessionmaker, monkeypatch):
    from app.services import worker as worker_module

    env = await worker_env(app_factory, worker_poll_interval_s=0.1)
    auth = await env.register()
    trip_id = (await make_trip(env, auth))["id"]
    real, calls = worker_module.claim_next_run, []

    async def flaky(sm, worker_id):
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("database waking up")
        return await real(sm, worker_id)

    monkeypatch.setattr(worker_module, "claim_next_run", flaky)
    worker, stop = make_worker(env, sessionmaker), asyncio.Event()
    loop_task = asyncio.create_task(worker.run_forever(stop))
    try:
        for _ in range(100):
            if (await get_trip(env, auth, trip_id))["latest_run"]["status"] == "succeeded":
                break
            await asyncio.sleep(0.1)
        assert (await get_trip(env, auth, trip_id))["latest_run"]["status"] == "succeeded"
    finally:
        stop.set()
        await asyncio.wait_for(loop_task, timeout=10)


async def test_heartbeat_extends_the_lease_while_solving(app_factory, sessionmaker, monkeypatch):
    import time

    env = await worker_env(app_factory, run_lease_seconds=10)
    auth = await env.register()
    trip_id = (await make_trip(env, auth))["id"]
    real = jobs.run_engine
    monkeypatch.setattr(jobs, "run_engine", lambda *a, **kw: (time.sleep(3.6), real(*a, **kw))[1])
    started = utcnow()
    await make_worker(env, sessionmaker).process_available()
    row = await run_row(sessionmaker, (await get_trip(env, auth, trip_id))["latest_run"]["id"])
    assert row.status == "succeeded"
    # The heartbeat (every lease/3 = 3.3s) ran at least once during the 3.6s solve.
    assert row.heartbeat_at is not None and row.started_at is not None
    beat, begun = row.heartbeat_at, row.started_at
    beat = beat if beat.tzinfo else beat.replace(tzinfo=started.tzinfo)
    begun = begun if begun.tzinfo else begun.replace(tzinfo=started.tzinfo)
    assert beat > begun


def test_entry_point_imports_and_is_runnable_as_a_module():
    import importlib

    module = importlib.import_module("app.worker")
    assert callable(module.run) and callable(module.main)
