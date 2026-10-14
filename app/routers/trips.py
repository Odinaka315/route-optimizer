"""Saved trips: create, list, open, star, modify (as a copy), re-optimize, delete."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..deps import get_current_user, get_session, get_settings_dep
from ..models import OptimizationRun, Trip, User
from ..schemas.trips import (
    RunDetailOut,
    RunStatusOut,
    RunSummaryOut,
    TripCreate,
    TripDetailOut,
    TripListOut,
    TripModify,
    TripPatch,
)
from ..services import trips as svc
from ._common import check_stop_count, resolve_or_http, schedule_run

router = APIRouter(prefix="/trips", tags=["trips"])


async def _own_trip(session: AsyncSession, user: User, trip_id: uuid.UUID) -> Trip:
    trip = await svc.get_trip(session, user.id, trip_id)
    if trip is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trip not found")
    return trip


@router.post("", response_model=TripDetailOut, status_code=status.HTTP_201_CREATED)
async def create_trip(
    body: TripCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> TripDetailOut:
    """Save a trip and start optimizing it. The response comes back immediately with
    ``latest_run.status == "queued"``; poll ``GET /trips/{id}`` until it is ``succeeded``."""
    check_stop_count(len(body.stops), settings)
    resolved = await resolve_or_http(session, request, settings, body.stops)
    weight_time, weight_dist = svc.resolve_weights(body.preset, body.weight_time, body.weight_dist)
    trip = await svc.create_trip(
        session,
        user,
        title=body.title,
        resolved=resolved,
        start_index=body.start_index,
        open_path=body.open_path,
        weight_time=weight_time,
        weight_dist=weight_dist,
    )
    run = await svc.queue_run(session, trip.id)
    await session.commit()
    schedule_run(request, background_tasks, run.id)
    return await svc.build_trip_detail(session, trip)


@router.get("", response_model=TripListOut)
async def list_trips(
    starred: bool | None = Query(default=None, description="true = only starred trips"),
    q: str | None = Query(default=None, max_length=100, description="Search in the title"),
    starred_first: bool = Query(default=False, description="Put starred trips at the top"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TripListOut:
    items, total = await svc.list_trips(
        session,
        user.id,
        starred=starred,
        query=q,
        starred_first=starred_first,
        limit=limit,
        offset=offset,
    )
    return TripListOut(items=items, total=total, limit=limit, offset=offset)


@router.get("/{trip_id}", response_model=TripDetailOut)
async def get_trip(
    trip_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TripDetailOut:
    """Open a saved trip. Returns the stored result as it was last solved; nothing is
    recalculated. Use ``POST /trips/{id}/runs`` to re-optimize on purpose."""
    return await svc.build_trip_detail(session, await _own_trip(session, user, trip_id))


@router.patch("/{trip_id}", response_model=TripDetailOut)
async def patch_trip(
    trip_id: uuid.UUID,
    body: TripPatch,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TripDetailOut:
    """Rename or star/unstar. Changes to stops or settings go through ``/modify``."""
    trip = await _own_trip(session, user, trip_id)
    if body.title is not None:
        trip.title = body.title
    if body.is_starred is not None:
        trip.is_starred = body.is_starred
    await session.commit()
    return await svc.build_trip_detail(session, trip)


@router.delete("/{trip_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_trip(
    trip_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    trip = await _own_trip(session, user, trip_id)
    trip.deleted_at = datetime.now(timezone.utc)  # soft delete: recoverable, copies keep their link
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{trip_id}/modify", response_model=TripDetailOut, status_code=status.HTTP_201_CREATED)
async def modify_trip(
    trip_id: uuid.UUID,
    body: TripModify,
    request: Request,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> TripDetailOut:
    """Edit a trip by saving a modified *copy*. The original is never changed; the copy
    points back to it through ``parent_trip_id``. Anything left out is copied as is.
    Sending no changes at all simply duplicates the trip."""
    parent = await _own_trip(session, user, trip_id)
    parent_stops = await svc.get_stops(session, parent.id)

    if body.stops is not None:
        check_stop_count(len(body.stops), settings)
        resolved = await resolve_or_http(session, request, settings, body.stops)
        start_index = body.start_index or 0
    else:
        resolved = svc.stops_to_resolved(parent_stops)
        parent_start = next((i for i, s in enumerate(parent_stops) if s.is_start), 0)
        start_index = body.start_index if body.start_index is not None else parent_start
        if start_index >= len(resolved):
            raise HTTPException(
                422, "start_index must point at one of the stops"
            )

    if body.preset is not None or body.weight_time is not None:
        weight_time, weight_dist = svc.resolve_weights(body.preset, body.weight_time, body.weight_dist)
    else:
        weight_time, weight_dist = parent.weight_time, parent.weight_dist

    trip = await svc.create_trip(
        session,
        user,
        title=body.title or f"{parent.title} (edited)"[:200],
        resolved=resolved,
        start_index=start_index,
        open_path=body.open_path if body.open_path is not None else parent.open_path,
        weight_time=weight_time,
        weight_dist=weight_dist,
        parent_trip_id=parent.id,
    )
    run = await svc.queue_run(session, trip.id)
    await session.commit()
    schedule_run(request, background_tasks, run.id)
    return await svc.build_trip_detail(session, trip)


@router.post("/{trip_id}/runs", response_model=RunStatusOut, status_code=status.HTTP_202_ACCEPTED)
async def reoptimize_trip(
    trip_id: uuid.UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> RunStatusOut:
    """Solve this trip again (for example after road data changed). Adds a new run and
    keeps the old ones."""
    trip = await _own_trip(session, user, trip_id)
    if await svc.find_active_run(session, trip.id, settings.run_timeout_minutes):
        raise HTTPException(status.HTTP_409_CONFLICT, "This trip is already being optimized")
    run = await svc.queue_run(session, trip.id)
    await session.commit()
    schedule_run(request, background_tasks, run.id)
    return RunStatusOut.model_validate(run)


@router.get("/{trip_id}/runs", response_model=list[RunSummaryOut])
async def list_runs(
    trip_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[RunSummaryOut]:
    trip = await _own_trip(session, user, trip_id)
    runs = await session.scalars(
        select(OptimizationRun)
        .where(OptimizationRun.trip_id == trip.id)
        .order_by(OptimizationRun.created_at.desc())
        .limit(limit)
    )
    return [RunSummaryOut.model_validate(r) for r in runs]


@router.get("/{trip_id}/runs/{run_id}", response_model=RunDetailOut)
async def get_run(
    trip_id: uuid.UUID,
    run_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RunDetailOut:
    trip = await _own_trip(session, user, trip_id)
    run = await session.scalar(
        select(OptimizationRun).where(OptimizationRun.id == run_id, OptimizationRun.trip_id == trip.id)
    )
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    return await svc.build_run_detail(session, run)
