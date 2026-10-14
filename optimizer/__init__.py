"""Delivery route optimizer: stop sequencing over real road costs."""

from .geo import GeocodingError, Stop, load_stops, load_stops_from_file
from .matrix import (
    PRESETS,
    HaversineBackend,
    MatrixBackend,
    OsrmBackend,
    combine,
    haversine_m,
    parse_osrm_table,
)
from .solvers import Solution, baseline_order, held_karp, ortools_solve, route_cost, solve

__all__ = [
    "GeocodingError",
    "Stop",
    "load_stops",
    "load_stops_from_file",
    "PRESETS",
    "HaversineBackend",
    "MatrixBackend",
    "OsrmBackend",
    "combine",
    "haversine_m",
    "parse_osrm_table",
    "Solution",
    "baseline_order",
    "held_karp",
    "ortools_solve",
    "route_cost",
    "solve",
]
