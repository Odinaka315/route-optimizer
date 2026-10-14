"""Database-level guarantees: cascades, uniqueness, constraints. Runs on SQLite and PostgreSQL."""

import uuid

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.models import GeocodeCache, OptimizationRun, RunStop, Trip, TripStop, User


async def seed(session, email="a@example.com"):
    user = User(email=email, password_hash="x")
    session.add(user)
    await session.flush()
    trip = Trip(user_id=user.id, title="T")
    session.add(trip)
    await session.flush()
    stops = [
        TripStop(trip_id=trip.id, input_position=i, name=f"S{i}", lat=6.5 + i * 0.01, lng=3.4, is_start=i == 0)
        for i in range(3)
    ]
    session.add_all(stops)
    run = OptimizationRun(trip_id=trip.id, status="succeeded")
    session.add(run)
    await session.flush()
    session.add_all([RunStop(run_id=run.id, trip_stop_id=s.id, visit_order=i) for i, s in enumerate(stops)])
    await session.commit()
    return user, trip, stops, run


async def count(session, model) -> int:
    return await session.scalar(select(func.count()).select_from(model))


async def test_deleting_a_user_removes_everything_they_own(sessionmaker):
    async with sessionmaker() as session:
        user, *_ = await seed(session)
        await session.execute(delete(User).where(User.id == user.id))
        await session.commit()
        for model in (User, Trip, TripStop, OptimizationRun, RunStop):
            assert await count(session, model) == 0, model.__name__


async def test_deleting_a_trip_removes_its_stops_and_runs(sessionmaker):
    async with sessionmaker() as session:
        _, trip, *_ = await seed(session)
        await session.execute(delete(Trip).where(Trip.id == trip.id))
        await session.commit()
        assert [await count(session, m) for m in (Trip, TripStop, OptimizationRun, RunStop)] == [0, 0, 0, 0]
        assert await count(session, User) == 1


async def test_hard_deleting_a_parent_keeps_the_copy_but_clears_the_link(sessionmaker):
    async with sessionmaker() as session:
        user, parent, *_ = await seed(session)
        child = Trip(user_id=user.id, title="copy", parent_trip_id=parent.id)
        session.add(child)
        await session.commit()
        await session.execute(delete(Trip).where(Trip.id == parent.id))
        await session.commit()
        # Read the column straight from the database; session.get() would return the
        # cached object from before the delete.
        rows = (await session.execute(select(Trip.id, Trip.parent_trip_id).where(Trip.id == child.id))).all()
        assert len(rows) == 1 and rows[0].parent_trip_id is None


async def test_emails_are_unique(sessionmaker):
    async with sessionmaker() as session:
        session.add(User(email="dup@example.com", password_hash="x"))
        await session.commit()
        session.add(User(email="dup@example.com", password_hash="y"))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_a_trip_has_at_most_one_start_stop(sessionmaker):
    async with sessionmaker() as session:
        _, trip, *_ = await seed(session)
        session.add(TripStop(trip_id=trip.id, input_position=9, name="second start", lat=6.5, lng=3.4, is_start=True))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_stop_positions_are_unique_within_a_trip(sessionmaker):
    async with sessionmaker() as session:
        _, trip, *_ = await seed(session)
        session.add(TripStop(trip_id=trip.id, input_position=1, name="dup position", lat=6.5, lng=3.4))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_a_run_cannot_list_the_same_stop_or_position_twice(sessionmaker):
    async with sessionmaker() as session:
        _, _, stops, run = await seed(session)
        session.add(RunStop(run_id=run.id, trip_stop_id=stops[0].id, visit_order=7))  # same stop again
        with pytest.raises(IntegrityError):
            await session.commit()
    async with sessionmaker() as session:
        _, _, stops, run = await seed(session, email="b@example.com")
        session.add(RunStop(run_id=run.id, trip_stop_id=uuid.uuid4(), visit_order=0))  # same slot / unknown stop
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_run_status_is_constrained(sessionmaker):
    async with sessionmaker() as session:
        _, trip, *_ = await seed(session)
        session.add(OptimizationRun(trip_id=trip.id, status="exploded"))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_defaults_are_applied(sessionmaker):
    async with sessionmaker() as session:
        user, trip, stops, run = await seed(session)
        assert user.is_active is True and user.email_verified_at is None and user.created_at is not None
        assert (trip.is_starred, trip.open_path, trip.weight_time, trip.weight_dist) == (False, True, 0.7, 0.3)
        assert stops[1].is_start is False and run.geometry_is_fallback is False


async def test_geocode_cache_lookup_is_unique(sessionmaker):
    row = dict(provider="mapbox", country="ng", query_normalized="x", lat=1.0, lng=2.0)
    async with sessionmaker() as session:
        session.add(GeocodeCache(**row))
        await session.commit()
        session.add(GeocodeCache(**row))
        with pytest.raises(IntegrityError):
            await session.commit()
