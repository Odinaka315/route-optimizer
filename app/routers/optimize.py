"""One-off optimization (nothing saved) and address search for the app's search box."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession

from optimizer.geo import Stop

from ..config import Settings
from ..deps import get_current_user, get_session, get_settings_dep
from ..models import User
from ..schemas.geocode import CandidateOut, GeocodeSearchOut
from ..schemas.trips import OptimizeRequest, RouteOut, RouteStopOut
from ..services.engine import EngineConfig, EngineError, RoutingServiceError, run_engine
from ..services.geocoding import GeocoderError
from ..services.trips import resolve_weights
from ._common import check_stop_count, resolve_or_http

router = APIRouter(tags=["optimize"])


@router.post("/optimize", response_model=RouteOut)
async def optimize(
    body: OptimizeRequest,
    request: Request,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> RouteOut:
    """Find the best order for a list of stops and return it right away, without saving
    anything. Can take several seconds for larger trips. To keep the result, create a trip
    with ``POST /trips`` instead."""
    check_stop_count(len(body.stops), settings)
    resolved = await resolve_or_http(session, request, settings, body.stops)
    await session.commit()  # keeps any newly cached geocoding results

    weight_time, weight_dist = resolve_weights(body.preset, body.weight_time, body.weight_dist)
    stops = [Stop(r.name, r.lat, r.lng) for r in resolved]
    try:
        async with request.app.state.solve_semaphore:
            result = await run_in_threadpool(
                run_engine,
                stops,
                start_index=body.start_index,
                open_path=body.open_path,
                weight_time=weight_time,
                weight_dist=weight_dist,
                config=EngineConfig.from_settings(settings),
            )
    except RoutingServiceError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from None
    except EngineError as exc:
        raise HTTPException(422, str(exc)) from None

    return RouteOut(
        run_id=None,
        finished_at=None,
        solver=result.solver,
        matrix_backend=result.matrix_backend,
        total_distance_m=result.total_distance_m,
        total_duration_s=result.total_duration_s,
        percent_saved=result.percent_saved,
        polyline=result.polyline,
        geometry_is_fallback=result.geometry_is_fallback,
        stops=[
            RouteStopOut(
                trip_stop_id=None,
                visit_order=visit_order,
                name=resolved[index].name,
                address=resolved[index].address_text,
                lat=resolved[index].lat,
                lng=resolved[index].lng,
                leg_distance_m=result.legs[visit_order][0],
                leg_duration_s=result.legs[visit_order][1],
            )
            for visit_order, index in enumerate(result.order)
        ],
    )


@router.get("/geocode/search", response_model=GeocodeSearchOut)
async def geocode_search(
    request: Request,
    q: str = Query(min_length=2, max_length=256, description="What the user has typed so far"),
    limit: int = Query(default=5, ge=1, le=10),
    lat: float | None = Query(default=None, ge=-90, le=90, description="Bias results toward this point"),
    lng: float | None = Query(default=None, ge=-180, le=180),
    _user: User = Depends(get_current_user),
) -> GeocodeSearchOut:
    """Address suggestions for the app's search box. The app sends the chosen result's
    coordinates when it saves a trip, so addresses are not looked up twice."""
    if (lat is None) != (lng is None):
        raise HTTPException(422, "lat and lng must be given together")
    proximity = (lat, lng) if lat is not None and lng is not None else None
    try:
        candidates = await run_in_threadpool(
            request.app.state.geocoder.search, q, limit, proximity, True
        )
    except GeocoderError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Address search is unavailable right now."
        ) from None
    return GeocodeSearchOut(
        results=[
            CandidateOut(
                name=c.name,
                address=c.address,
                lat=c.lat,
                lng=c.lng,
                provider=c.provider,
                provider_place_id=c.provider_place_id,
            )
            for c in candidates
        ]
    )
