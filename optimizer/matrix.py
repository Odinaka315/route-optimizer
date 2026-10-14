"""Travel time / distance matrices and the combined cost used by the solvers."""

from __future__ import annotations

import math
import warnings
from abc import ABC, abstractmethod

import requests

from .geo import Stop

Matrix = list[list[float]]

EARTH_RADIUS_M = 6_371_000.0

# (w_time, w_dist) presets
PRESETS: dict[str, tuple[float, float]] = {
    "fastest": (1.0, 0.0),
    "shortest": (0.0, 1.0),
    "balanced": (0.7, 0.3),
}


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


class MatrixBackend(ABC):
    @abstractmethod
    def build(self, stops: list[Stop]) -> tuple[Matrix, Matrix]:
        """Return (time_seconds_matrix, distance_meters_matrix)."""


class HaversineBackend(MatrixBackend):
    """Offline estimate from straight-line distance.

    Straight lines underestimate road distance, so distance is multiplied by a
    detour factor (default 1.3, a common urban rule of thumb). Time is derived
    from an assumed average speed (default 25 km/h, Lagos-like conditions).
    """

    def __init__(self, avg_speed_kmh: float = 25.0, detour_factor: float = 1.3):
        if avg_speed_kmh <= 0:
            raise ValueError("avg_speed_kmh must be positive")
        if detour_factor < 1.0:
            raise ValueError("detour_factor must be >= 1.0")
        self.avg_speed_ms = avg_speed_kmh * 1000.0 / 3600.0
        self.detour_factor = detour_factor

    def build(self, stops: list[Stop]) -> tuple[Matrix, Matrix]:
        n = len(stops)
        dist: Matrix = [[0.0] * n for _ in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                d = haversine_m(stops[i].lat, stops[i].lng, stops[j].lat, stops[j].lng)
                d *= self.detour_factor
                dist[i][j] = dist[j][i] = d
        time_m = [[d / self.avg_speed_ms for d in row] for row in dist]
        return time_m, dist


def parse_osrm_table(data: dict) -> tuple[Matrix, Matrix]:
    """Parse an OSRM /table response. Unreachable pairs (null) become inf."""
    if data.get("code") != "Ok":
        raise RuntimeError(f"OSRM table request failed: {data.get('code')} {data.get('message', '')}")
    durations = data.get("durations")
    distances = data.get("distances")
    if durations is None or distances is None:
        raise RuntimeError("OSRM response is missing durations or distances.")

    def clean(m: list[list[float | None]]) -> Matrix:
        return [[math.inf if v is None else float(v) for v in row] for row in m]

    return clean(durations), clean(distances)


class OsrmBackend(MatrixBackend):
    """Real road times and distances from an OSRM server (/table endpoint).

    The public demo server is for development only. Self-host OSRM for real use.
    """

    def __init__(
        self,
        base_url: str = "https://router.project-osrm.org",
        timeout: float = 30.0,
        session: requests.Session | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()

    def build(self, stops: list[Stop]) -> tuple[Matrix, Matrix]:
        coords = ";".join(f"{s.lng},{s.lat}" for s in stops)
        url = f"{self.base_url}/table/v1/driving/{coords}"
        resp = self.session.get(
            url, params={"annotations": "duration,distance"}, timeout=self.timeout
        )
        resp.raise_for_status()
        return parse_osrm_table(resp.json())


def _normalize(m: Matrix) -> Matrix:
    """Divide by the mean of finite off-diagonal entries so time and distance
    are on comparable scales before weighting. inf entries are preserved."""
    n = len(m)
    vals = [m[i][j] for i in range(n) for j in range(n) if i != j and math.isfinite(m[i][j])]
    mean = sum(vals) / len(vals) if vals else 0.0
    scale = mean if mean > 0 else 1.0
    return [[v / scale for v in row] for row in m]


def combine(
    time_m: Matrix, dist_m: Matrix, w_time: float = 0.7, w_dist: float = 0.3
) -> Matrix:
    """Weighted cost = w_time * norm(time) + w_dist * norm(distance).

    Unreachable pairs get a penalty far above any real cost so the solver
    avoids them, and a warning is emitted.
    """
    if w_time < 0 or w_dist < 0 or (w_time == 0 and w_dist == 0):
        raise ValueError("Weights must be non-negative and not both zero.")
    n = len(time_m)
    if n != len(dist_m) or any(len(r) != n for r in time_m) or any(len(r) != n for r in dist_m):
        raise ValueError("time and distance matrices must be square and the same size.")

    t, d = _normalize(time_m), _normalize(dist_m)
    cost: Matrix = [[0.0] * n for _ in range(n)]
    unreachable: list[tuple[int, int]] = []
    finite_vals: list[float] = []

    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            tv = t[i][j] if w_time > 0 else 0.0
            dv = d[i][j] if w_dist > 0 else 0.0
            if math.isinf(tv) or math.isinf(dv):
                unreachable.append((i, j))
                cost[i][j] = math.inf
            else:
                cost[i][j] = w_time * tv + w_dist * dv
                finite_vals.append(cost[i][j])

    if unreachable:
        penalty = max(max(finite_vals, default=1.0), 1.0) * 1000.0
        for i, j in unreachable:
            cost[i][j] = penalty
        warnings.warn(
            f"{len(unreachable)} stop pair(s) are unreachable by road; "
            "they were given a very high cost so routes avoid them.",
            stacklevel=2,
        )
    return cost
