"""Glue between the API and the optimizer package: stops in, ordered route out.

``run_engine`` is plain blocking code (network calls to OSRM plus a CPU-heavy
solve), so callers run it in a worker thread, never directly on the event loop.
"""

from __future__ import annotations

import logging
import math
import warnings
from collections.abc import Sequence
from dataclasses import dataclass

import requests

from optimizer.geo import Stop
from optimizer.matrix import HaversineBackend, OsrmBackend, combine
from optimizer.route import get_route
from optimizer.solvers import baseline_order, route_cost, solve

from ..config import Settings
from .polyline import encode_polyline

logger = logging.getLogger(__name__)


class EngineError(Exception):
    """The route could not be computed. The message is safe to show to the user."""


class RoutingServiceError(EngineError):
    """The routing service (OSRM) is unreachable or answered badly, so retrying later may work."""


@dataclass(frozen=True)
class EngineConfig:
    matrix_backend: str  # "osrm" or "haversine"
    osrm_url: str
    speed_kmh: float
    time_limit_s: int

    @classmethod
    def from_settings(cls, settings: Settings) -> "EngineConfig":
        return cls(
            matrix_backend=settings.matrix_backend,
            osrm_url=settings.osrm_url,
            speed_kmh=settings.haversine_speed_kmh,
            time_limit_s=settings.solver_time_limit_s,
        )


@dataclass
class EngineResult:
    order: list[int]  # indices into the input stops, in visiting order
    solver: str
    matrix_backend: str
    total_distance_m: float
    total_duration_s: float
    percent_saved: float  # versus visiting the stops in the order they were entered
    legs: list[tuple[float | None, float | None]]  # (distance_m, duration_s) from previous stop
    polyline: str
    geometry_is_fallback: bool


def run_engine(
    stops: Sequence[Stop],
    *,
    start_index: int,
    open_path: bool,
    weight_time: float,
    weight_dist: float,
    config: EngineConfig,
) -> EngineResult:
    n = len(stops)
    if n == 0:
        raise EngineError("There are no stops to optimize.")
    if not 0 <= start_index < n:
        raise EngineError(f"Start position {start_index} is outside the {n} stops.")
    stops = list(stops)

    # 1. Travel times and distances between every pair of stops.
    try:
        if config.matrix_backend == "osrm":
            backend = OsrmBackend(base_url=config.osrm_url)
        else:
            backend = HaversineBackend(avg_speed_kmh=config.speed_kmh)
        time_m, dist_m = backend.build(stops)
    except requests.RequestException as exc:
        raise RoutingServiceError(
            f"Could not reach the routing service ({exc.__class__.__name__}). Try again shortly."
        ) from exc
    except RuntimeError as exc:
        raise RoutingServiceError(f"The routing service could not compute travel times: {exc}") from exc

    # 2. Combine into one cost and find the best order. Unreachable pairs get a huge
    #    penalty in `combine` (its warning is expected); they are rejected below if the
    #    chosen route still needs one.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cost = combine(time_m, dist_m, weight_time, weight_dist)
    solution = solve(cost, start=start_index, open_path=open_path, time_limit_s=config.time_limit_s)
    order = solution.order

    # 3. Per-leg numbers and totals from the real matrices (not the weighted cost).
    legs: list[tuple[float | None, float | None]] = [(None, None)]
    pairs = list(zip(order, order[1:]))
    if not open_path and n > 1:
        pairs.append((order[-1], order[0]))  # the leg back to the start
    total_distance = total_duration = 0.0
    for position, (a, b) in enumerate(pairs):
        distance, duration = dist_m[a][b], time_m[a][b]
        if not (math.isfinite(distance) and math.isfinite(duration)):
            raise EngineError(
                f"There is no road route between '{stops[a].name}' and '{stops[b].name}'. "
                "Check that both locations are on or near a drivable road."
            )
        total_distance += distance
        total_duration += duration
        if position < len(order) - 1:
            legs.append((distance, duration))

    baseline_cost = route_cost(cost, baseline_order(n, start_index), open_path)
    percent_saved = (
        (baseline_cost - solution.total_cost) / baseline_cost * 100.0 if baseline_cost > 0 else 0.0
    )

    # 4. The actual road geometry for the chosen order.
    ordered = [stops[i] for i in order]
    path = ordered + [ordered[0]] if (not open_path and n > 1) else ordered
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        geometry = get_route(
            path,
            base_url=config.osrm_url if config.matrix_backend == "osrm" else None,
            avg_speed_kmh=config.speed_kmh,
        )
    for warning in caught:
        logger.warning("route geometry: %s", warning.message)

    return EngineResult(
        order=order,
        solver=solution.solver,
        matrix_backend=config.matrix_backend,
        total_distance_m=total_distance,
        total_duration_s=total_duration,
        percent_saved=max(0.0, percent_saved),
        legs=legs,
        polyline=encode_polyline(geometry.points),
        geometry_is_fallback=geometry.is_fallback,
    )
