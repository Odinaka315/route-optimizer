"""Database operations for trips, stops and runs."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from optimizer.matrix import PRESETS

from ..models import OptimizationRun, RunStop, Trip, TripStop, User, utcnow
from ..schemas.trips import (
    RouteOut,
    RouteStopOut,
    RunDetailOut,
    RunStatusOut,
    StopIn,
    StopOut,
    TripDetailOut,
    TripSummaryOut,
)
from .geocoding import Geocoder, GeocoderError, geocode_best


class StopResolutionError(Exception):
    """Some address-only stops could not be located."""

    def __init__(self, failures: list[str]):
        super().__init__("Could not locate: " + "; ".join(failures))
        self.failures = failures


class GeocoderUnavailable(Exception):
    """The geocoding provider is down or rejected us."""


@dataclass
class ResolvedStop:
    name: str
    address_text: str | None
    lat: float
    lng: float
    geocode_source: str | None
    place_provider_id: str | None
    notes: str | None


def resolve_weights(
    preset: str | None, weight_time: float | None, weight_dist: float | None
) -> tuple[float, float]:
    """Explicit weights win; otherwise the preset (default 'balanced')."""
    if weight_time is not None and weight_dist is not None:
        return weight_time, weight_dist
    return PRESETS[preset or "balanced"]


def default_title(now: datetime | None = None) -> str:
    return f"Trip {(now or utcnow()):%d %b %Y %H:%M}"


async def resolve_stops(
    session: AsyncSession, geocoder: Geocoder, country: str | None, stops: Sequence[StopIn]
) -> list[ResolvedStop]:
    """Turn request stops into coordinates, geocoding address-only ones."""
    resolved: list[ResolvedStop] = []
    failed: list[str] = []
    for position, stop in enumerate(stops):
        if stop.lat is not None and stop.lng is not None:
            resolved.append(
                ResolvedStop(
                    name=stop.name or stop.address or f"Stop {position + 1}",
                    address_text=stop.address,
                    lat=stop.lat,
                    lng=stop.lng,
                    geocode_source=stop.geocode_source,
                    place_provider_id=stop.place_provider_id,
                    notes=stop.notes,
                )
            )
            continue
        assert stop.address  # guaranteed by StopIn validation
        try:
            found = await geocode_best(session, geocoder, country, stop.address)
        except GeocoderError as exc:
            raise GeocoderUnavailable(str(exc)) from exc
        if found is None:
            failed.append(f"'{stop.address}'")
            continue
        resolved.append(
            ResolvedStop(
                name=stop.name or found.name or stop.address,
                address_text=stop.address,
                lat=found.lat,
                lng=found.lng,
                geocode_source=found.provider,
                place_provider_id=found.provider_place_id,
                notes=stop.notes,
            )
        )
    if failed:
        raise StopResolutionError(failed)
    return resolved


def stops_to_resolved(rows: Sequence[TripStop]) -> list[ResolvedStop]:
    return [
        ResolvedStop(r.name, r.address_text, r.lat, r.lng, r.geocode_source, r.place_provider_id, r.notes)
        for r in rows
    ]


async def create_trip(
    session: AsyncSession,
    user: User,
    *,
    title: str | None,
    resolved: Sequence[ResolvedStop],
    start_index: int,
    open_path: bool,
    weight_time: float,
    weight_dist: float,
    parent_trip_id: uuid.UUID | None = None,
) -> Trip:
    trip = Trip(
        user_id=user.id,
        title=title or default_title(),
        parent_trip_id=parent_trip_id,
        open_path=open_path,
        weight_time=weight_time,
        weight_dist=weight_dist,
    )
    session.add(trip)
    await session.flush()
    for position, stop in enumerate(resolved):
        session.add(
            TripStop(
                trip_id=trip.id,
                input_position=position,
                name=stop.name,
                address_text=stop.address_text,
                lat=stop.lat,
                lng=stop.lng,
                geocode_source=stop.geocode_source,
                place_provider_id=stop.place_provider_id,
                is_start=position == start_index,
                notes=stop.notes,
            )
        )
    await session.flush()
    return trip


async def queue_run(session: AsyncSession, trip_id: uuid.UUID) -> OptimizationRun:
    run = OptimizationRun(trip_id=trip_id, status="queued")
    session.add(run)
    await session.flush()
    return run


async def get_trip(session: AsyncSession, user_id: uuid.UUID, trip_id: uuid.UUID) -> Trip | None:
    """A user's own, non-deleted trip. Someone else's trip looks exactly like a missing one."""
    return await session.scalar(
        select(Trip).where(Trip.id == trip_id, Trip.user_id == user_id, Trip.deleted_at.is_(None))
    )


async def get_stops(session: AsyncSession, trip_id: uuid.UUID) -> list[TripStop]:
    result = await session.scalars(
        select(TripStop).where(TripStop.trip_id == trip_id).order_by(TripStop.input_position)
    )
    return list(result)


async def find_active_run(
    session: AsyncSession, trip_id: uuid.UUID, timeout_minutes: int
) -> OptimizationRun | None:
    """A queued/running run that is still plausibly alive (not older than the timeout)."""
    cutoff = utcnow() - timedelta(minutes=timeout_minutes)
    return await session.scalar(
        select(OptimizationRun)
        .where(
            OptimizationRun.trip_id == trip_id,
            OptimizationRun.status.in_(("queued", "running")),
            OptimizationRun.created_at > cutoff,
        )
        .limit(1)
    )


# ------------------------------------------------------------------ building responses


async def build_route_out(session: AsyncSession, run: OptimizationRun) -> RouteOut:
    rows = (
        await session.execute(
            select(RunStop, TripStop)
            .join(TripStop, RunStop.trip_stop_id == TripStop.id)
            .where(RunStop.run_id == run.id)
            .order_by(RunStop.visit_order)
        )
    ).all()
    return RouteOut(
        run_id=run.id,
        finished_at=run.finished_at,
        solver=run.solver or "",
        matrix_backend=run.matrix_backend or "",
        total_distance_m=run.total_distance_m or 0.0,
        total_duration_s=run.total_duration_s or 0.0,
        percent_saved=run.percent_saved or 0.0,
        polyline=run.route_polyline,
        geometry_is_fallback=run.geometry_is_fallback,
        stops=[
            RouteStopOut(
                trip_stop_id=stop.id,
                visit_order=run_stop.visit_order,
                name=stop.name,
                address=stop.address_text,
                lat=stop.lat,
                lng=stop.lng,
                leg_distance_m=run_stop.leg_distance_m,
                leg_duration_s=run_stop.leg_duration_s,
            )
            for run_stop, stop in rows
        ],
    )


async def build_run_detail(session: AsyncSession, run: OptimizationRun) -> RunDetailOut:
    route = await build_route_out(session, run) if run.status == "succeeded" else None
    return RunDetailOut(
        id=run.id,
        status=run.status,  # type: ignore[arg-type]
        error_message=run.error_message,
        created_at=run.created_at,
        finished_at=run.finished_at,
        route=route,
    )


async def build_trip_detail(session: AsyncSession, trip: Trip) -> TripDetailOut:
    stops = await get_stops(session, trip.id)
    latest = await session.scalar(
        select(OptimizationRun)
        .where(OptimizationRun.trip_id == trip.id)
        .order_by(OptimizationRun.created_at.desc())
        .limit(1)
    )
    good = latest
    if latest is not None and latest.status != "succeeded":
        good = await session.scalar(
            select(OptimizationRun)
            .where(OptimizationRun.trip_id == trip.id, OptimizationRun.status == "succeeded")
            .order_by(OptimizationRun.created_at.desc())
            .limit(1)
        )
    return TripDetailOut(
        id=trip.id,
        title=trip.title,
        is_starred=trip.is_starred,
        parent_trip_id=trip.parent_trip_id,
        open_path=trip.open_path,
        weight_time=trip.weight_time,
        weight_dist=trip.weight_dist,
        created_at=trip.created_at,
        updated_at=trip.updated_at,
        stops=[StopOut.model_validate(s) for s in stops],
        route=await build_route_out(session, good) if good is not None else None,
        latest_run=RunStatusOut.model_validate(latest) if latest is not None else None,
    )


async def _latest_runs(session: AsyncSession, trip_ids: Sequence[uuid.UUID], succeeded_only: bool):
    """The newest run per trip (optionally the newest successful one), in one query."""
    rank = (
        func.row_number()
        .over(partition_by=OptimizationRun.trip_id, order_by=OptimizationRun.created_at.desc())
        .label("rn")
    )
    stmt = select(
        OptimizationRun.id,
        OptimizationRun.trip_id,
        OptimizationRun.status,
        OptimizationRun.error_message,
        OptimizationRun.created_at,
        OptimizationRun.finished_at,
        OptimizationRun.total_distance_m,
        OptimizationRun.total_duration_s,
        rank,
    ).where(OptimizationRun.trip_id.in_(trip_ids))
    if succeeded_only:
        stmt = stmt.where(OptimizationRun.status == "succeeded")
    sub = stmt.subquery()
    rows = (await session.execute(select(sub).where(sub.c.rn == 1))).all()
    return {row.trip_id: row for row in rows}


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def list_trips(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    starred: bool | None,
    query: str | None,
    starred_first: bool,
    limit: int,
    offset: int,
) -> tuple[list[TripSummaryOut], int]:
    conditions = [Trip.user_id == user_id, Trip.deleted_at.is_(None)]
    if starred is not None:
        conditions.append(Trip.is_starred == starred)
    if query:
        conditions.append(Trip.title.ilike(f"%{_escape_like(query)}%", escape="\\"))

    total = await session.scalar(select(func.count(Trip.id)).where(*conditions)) or 0

    order = [Trip.created_at.desc()]
    if starred_first:
        order.insert(0, Trip.is_starred.desc())
    trips = list(
        await session.scalars(select(Trip).where(*conditions).order_by(*order).limit(limit).offset(offset))
    )
    if not trips:
        return [], total

    ids = [t.id for t in trips]
    counts = dict(
        (
            await session.execute(
                select(TripStop.trip_id, func.count(TripStop.id))
                .where(TripStop.trip_id.in_(ids))
                .group_by(TripStop.trip_id)
            )
        ).all()
    )
    latest = await _latest_runs(session, ids, succeeded_only=False)
    good = await _latest_runs(session, ids, succeeded_only=True)

    items = []
    for trip in trips:
        last, ok = latest.get(trip.id), good.get(trip.id)
        items.append(
            TripSummaryOut(
                id=trip.id,
                title=trip.title,
                is_starred=trip.is_starred,
                parent_trip_id=trip.parent_trip_id,
                open_path=trip.open_path,
                stop_count=counts.get(trip.id, 0),
                created_at=trip.created_at,
                updated_at=trip.updated_at,
                latest_run=RunStatusOut.model_validate(last) if last is not None else None,
                total_distance_m=ok.total_distance_m if ok is not None else None,
                total_duration_s=ok.total_duration_s if ok is not None else None,
            )
        )
    return items, total
