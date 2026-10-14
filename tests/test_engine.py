"""The engine wrapper, tested directly (no web layer, no database)."""

import math

import pytest
import requests

from optimizer.geo import Stop
from optimizer.route import RouteGeometry
from app.services import engine as engine_module
from app.services.engine import EngineConfig, EngineError, RoutingServiceError, run_engine
from app.services.polyline import decode_polyline

from .conftest import LAGOS

STOPS = [Stop(n, la, ln) for n, la, ln in LAGOS]
CONFIG = EngineConfig(matrix_backend="haversine", osrm_url="http://osrm.invalid", speed_kmh=25.0, time_limit_s=1)


def solve(stops=STOPS, **kw):
    args = dict(start_index=0, open_path=True, weight_time=0.7, weight_dist=0.3, config=CONFIG)
    args.update(kw)
    return run_engine(stops, **args)


def test_result_is_a_valid_route():
    result = solve()
    assert sorted(result.order) == list(range(6))
    assert result.order[0] == 0
    assert result.solver == "held_karp"
    assert result.matrix_backend == "haversine"
    assert result.total_distance_m > 0 and result.total_duration_s > 0
    assert result.percent_saved >= 0


def test_legs_line_up_with_totals():
    result = solve()
    assert result.legs[0] == (None, None)
    assert len(result.legs) == len(result.order)
    assert sum(d for d, _ in result.legs[1:]) == pytest.approx(result.total_distance_m)
    assert sum(t for _, t in result.legs[1:]) == pytest.approx(result.total_duration_s)


def test_start_index_is_respected():
    assert solve(start_index=3).order[0] == 3


def test_polyline_covers_the_chosen_stops_in_order():
    result = solve()
    points = decode_polyline(result.polyline)
    assert len(points) == 6  # straight-line fallback: one point per stop
    first = STOPS[result.order[0]]
    assert points[0] == (pytest.approx(first.lat, abs=1e-6), pytest.approx(first.lng, abs=1e-6))
    assert result.geometry_is_fallback is True  # haversine backend has no road geometry


def test_closed_loop_returns_to_start_and_costs_more_than_open():
    open_result = solve(open_path=True)
    closed = solve(open_path=False)
    assert closed.total_distance_m > open_result.total_distance_m
    assert len(closed.legs) == len(closed.order)  # the return leg is in the totals only
    points = decode_polyline(closed.polyline)
    assert len(points) == 7 and points[0] == points[-1]


def test_single_stop():
    result = solve(stops=STOPS[:1])
    assert result.order == [0]
    assert result.total_distance_m == 0
    assert result.percent_saved == 0


def test_larger_trips_use_ortools():
    many = [Stop(f"S{i}", 6.4 + i * 0.01, 3.3 + (i % 5) * 0.02) for i in range(14)]
    result = solve(stops=many)
    assert result.solver == "ortools"
    assert sorted(result.order) == list(range(14))


def test_bad_inputs_raise_engine_errors():
    with pytest.raises(EngineError):
        solve(stops=[])
    with pytest.raises(EngineError):
        solve(start_index=6)
    with pytest.raises(EngineError):
        solve(start_index=-1)


class FakeOsrmBackend:
    """Stands in for OSRM with chosen matrices."""

    time_m = [[0, 600, 900], [620, 0, 700], [880, 710, 0]]
    dist_m = [[0, 8000, 12000], [8100, 0, 9000], [11800, 9100, 0]]

    def __init__(self, base_url):
        pass

    def build(self, stops):
        return self.time_m, self.dist_m


OSRM_CONFIG = EngineConfig(matrix_backend="osrm", osrm_url="http://osrm.local", speed_kmh=25.0, time_limit_s=1)


def test_osrm_backend_path_uses_real_matrices_and_geometry(monkeypatch):
    road = RouteGeometry([(6.5, 3.3), (6.51, 3.31), (6.52, 3.32)], 20000.0, 1500.0, False)
    monkeypatch.setattr(engine_module, "OsrmBackend", FakeOsrmBackend)
    monkeypatch.setattr(engine_module, "get_route", lambda *a, **k: road)
    result = run_engine(
        STOPS[:3], start_index=0, open_path=True, weight_time=1.0, weight_dist=0.0, config=OSRM_CONFIG
    )
    assert result.matrix_backend == "osrm"
    assert result.geometry_is_fallback is False
    assert decode_polyline(result.polyline) == road.points
    # Totals come from the matrices: 0->1->2 costs 600+700 s and 8000+9000 m.
    assert result.order == [0, 1, 2]
    assert result.total_duration_s == 1300 and result.total_distance_m == 17000


def test_unreachable_leg_is_reported_with_stop_names(monkeypatch):
    class Unreachable(FakeOsrmBackend):
        time_m = [[0, math.inf, math.inf], [math.inf, 0, 700], [880, 710, 0]]
        dist_m = [[0, math.inf, math.inf], [math.inf, 0, 9000], [11800, 9100, 0]]

    monkeypatch.setattr(engine_module, "OsrmBackend", Unreachable)
    monkeypatch.setattr(engine_module, "get_route", lambda *a, **k: RouteGeometry([], 0, 0, False))
    with pytest.raises(EngineError, match="Depot - Yaba"):
        run_engine(STOPS[:3], start_index=0, open_path=True, weight_time=0.7, weight_dist=0.3, config=OSRM_CONFIG)


def test_routing_service_outage_is_a_retryable_error(monkeypatch):
    class Down:
        def __init__(self, base_url):
            pass

        def build(self, stops):
            raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(engine_module, "OsrmBackend", Down)
    with pytest.raises(RoutingServiceError, match="Try again"):
        run_engine(STOPS[:3], start_index=0, open_path=True, weight_time=0.7, weight_dist=0.3, config=OSRM_CONFIG)


def test_osrm_error_response_is_a_routing_service_error(monkeypatch):
    class Rejecting:
        def __init__(self, base_url):
            pass

        def build(self, stops):
            raise RuntimeError("OSRM table request failed: TooBig")

    monkeypatch.setattr(engine_module, "OsrmBackend", Rejecting)
    with pytest.raises(RoutingServiceError, match="TooBig"):
        run_engine(STOPS[:3], start_index=0, open_path=True, weight_time=0.7, weight_dist=0.3, config=OSRM_CONFIG)
