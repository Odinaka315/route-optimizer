"""Real road geometry for an already-ordered list of stops (OSRM /route)."""

from __future__ import annotations

import warnings
from dataclasses import dataclass

import requests

from .geo import Stop
from .matrix import haversine_m

DEFAULT_OSRM_URL = "https://router.project-osrm.org"


@dataclass
class RouteGeometry:
    points: list[tuple[float, float]]  # (lat, lng) along the road
    distance_m: float
    duration_s: float
    is_fallback: bool  # True when drawn as straight lines between stops


def straight_line_route(
    stops: list[Stop], avg_speed_kmh: float = 25.0, detour_factor: float = 1.3
) -> RouteGeometry:
    """Fallback: straight segments between consecutive stops, with estimated totals."""
    points = [(s.lat, s.lng) for s in stops]
    dist = sum(
        haversine_m(a.lat, a.lng, b.lat, b.lng) * detour_factor
        for a, b in zip(stops, stops[1:])
    )
    return RouteGeometry(points, dist, dist / (avg_speed_kmh * 1000.0 / 3600.0), True)


def parse_osrm_route(data: dict) -> RouteGeometry:
    """Parse an OSRM /route response (geometries=geojson)."""
    if data.get("code") != "Ok" or not data.get("routes"):
        raise RuntimeError(f"OSRM route request failed: {data.get('code')} {data.get('message', '')}")
    route = data["routes"][0]
    # GeoJSON is [lng, lat]; the map wants (lat, lng).
    points = [(lat, lng) for lng, lat in route["geometry"]["coordinates"]]
    return RouteGeometry(points, float(route["distance"]), float(route["duration"]), False)


def fetch_route(
    stops: list[Stop],
    base_url: str = DEFAULT_OSRM_URL,
    timeout: float = 30.0,
    session: requests.Session | None = None,
) -> RouteGeometry:
    """Ask OSRM for the driving route through `stops` in the given order."""
    if len(stops) < 2:
        return RouteGeometry([(s.lat, s.lng) for s in stops], 0.0, 0.0, False)
    coords = ";".join(f"{s.lng},{s.lat}" for s in stops)
    http = session or requests
    resp = http.get(
        f"{base_url.rstrip('/')}/route/v1/driving/{coords}",
        params={"overview": "full", "geometries": "geojson", "steps": "false"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return parse_osrm_route(resp.json())


def get_route(
    stops: list[Stop],
    base_url: str | None = DEFAULT_OSRM_URL,
    timeout: float = 30.0,
    avg_speed_kmh: float = 25.0,
) -> RouteGeometry:
    """OSRM geometry when a server is given and reachable, else straight lines.

    The fallback is flagged via `is_fallback` and a warning, never silent.
    """
    if base_url:
        try:
            return fetch_route(stops, base_url, timeout)
        except (requests.RequestException, RuntimeError, KeyError, ValueError) as exc:
            warnings.warn(
                f"Could not fetch road geometry ({exc.__class__.__name__}: {exc}); "
                "drawing straight lines between stops instead.",
                stacklevel=2,
            )
    return straight_line_route(stops, avg_speed_kmh)
