import json
import math

import pytest

import optimizer.geo as geo
from optimizer.geo import GeocodingError, Stop, load_stops
from optimizer.matrix import combine, parse_osrm_table
from optimizer.render import render_map
from optimizer.route import straight_line_route
from optimizer.solvers import HELD_KARP_MAX_STOPS, held_karp, ortools_solve, solve

SOLVERS = [
    pytest.param(lambda c, **kw: held_karp(c, **kw), id="held_karp"),
    pytest.param(lambda c, **kw: ortools_solve(c, time_limit_s=1, **kw), id="ortools"),
]


@pytest.mark.parametrize("run", SOLVERS)
@pytest.mark.parametrize("open_path", [True, False])
def test_single_stop(run, open_path):
    sol = run([[0.0]], open_path=open_path)
    assert sol.order == [0]
    assert sol.total_cost == 0


@pytest.mark.parametrize("run", SOLVERS)
def test_two_stops(run):
    cost = [[0, 5], [7, 0]]
    open_sol = run(cost, open_path=True)
    assert open_sol.order == [0, 1] and open_sol.total_cost == 5
    closed_sol = run(cost, open_path=False)
    assert closed_sol.order == [0, 1] and closed_sol.total_cost == 12


@pytest.mark.parametrize("run", SOLVERS)
def test_two_stops_with_start_at_second(run):
    sol = run([[0, 5], [7, 0]], start=1, open_path=True)
    assert sol.order == [1, 0] and sol.total_cost == 7


def test_empty_input_raises():
    for fn in (held_karp, ortools_solve, solve):
        with pytest.raises(ValueError, match="at least one stop"):
            fn([])


@pytest.mark.parametrize("start", [-1, 3, 99])
def test_start_out_of_range_raises(start):
    cost = [[0, 1, 2], [1, 0, 1], [2, 1, 0]]
    for fn in (held_karp, ortools_solve, solve):
        with pytest.raises(ValueError, match="out of range"):
            fn(cost, start=start)


def test_non_square_matrix_raises():
    with pytest.raises(ValueError, match="square"):
        held_karp([[0, 1], [1, 0], [2, 2]])


def test_held_karp_refuses_oversized_input():
    n = HELD_KARP_MAX_STOPS + 1
    cost = [[0.0 if i == j else 1.0 for j in range(n)] for i in range(n)]
    with pytest.raises(ValueError, match="at most"):
        held_karp(cost)


def test_duplicate_coordinates_are_handled():
    # Stops 1 and 2 are at the same place: zero cost between them.
    cost = [
        [0, 10, 10, 30],
        [10, 0, 0, 20],
        [10, 0, 0, 20],
        [30, 20, 20, 0],
    ]
    for fn in (held_karp, lambda c: ortools_solve(c, time_limit_s=1)):
        sol = fn(cost)
        assert sorted(sol.order) == [0, 1, 2, 3]
        assert sol.total_cost == 30  # 0 -> 1 -> 2 -> 3 = 10 + 0 + 20


def test_all_stops_at_same_place():
    cost = [[0.0] * 4 for _ in range(4)]
    sol = solve(cost)
    assert sorted(sol.order) == [0, 1, 2, 3]
    assert sol.total_cost == 0
    assert combine(cost, cost, 0.5, 0.5) == cost  # no division by zero


def test_unreachable_stop_pair_is_avoided():
    # 3 stops: going 1 -> 2 is impossible, so the route must go 0 -> 2 -> 1.
    sample = {
        "code": "Ok",
        "durations": [[0, 600, 900], [620, 0, None], [880, 1200, 0]],
        "distances": [[0, 8000, 12000], [8100, 0, None], [11800, 15000, 0]],
    }
    durations, distances = parse_osrm_table(sample)
    with pytest.warns(UserWarning, match="unreachable"):
        cost = combine(durations, distances)
    sol = solve(cost, start=0, open_path=True)
    assert sol.order == [0, 2, 1]
    assert math.isfinite(sol.total_cost)


def test_ortools_handles_a_dozen_stops_quickly_and_validly():
    n = 12
    cost = [[0.0 if i == j else float(abs(i - j)) for j in range(n)] for i in range(n)]
    sol = ortools_solve(cost, time_limit_s=1)
    # Stops sit on a line, so the optimal open path from 0 is simply 0..11.
    assert sol.order == list(range(n))
    assert sol.total_cost == n - 1


# --- input handling (geo.py) -------------------------------------------------


def test_coordinates_pass_straight_through_without_network(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("geocoder must not be called for lat/lng entries")

    monkeypatch.setattr(geo, "geocode_address", no_network)
    stops = load_stops([{"name": "A", "lat": 6.5, "lng": 3.4}], cache_path=None)
    assert stops == [Stop("A", 6.5, 3.4)]


def test_out_of_range_coordinates_rejected():
    with pytest.raises(ValueError, match="out-of-range"):
        load_stops([{"name": "Bad", "lat": 123.0, "lng": 3.4}], cache_path=None)


def test_entry_without_location_rejected():
    with pytest.raises(ValueError, match="lat/lng or an address"):
        load_stops([{"name": "Nowhere"}], cache_path=None)


def test_failed_geocoding_lists_every_bad_address(monkeypatch):
    monkeypatch.setattr(geo, "geocode_address", lambda *a, **k: None)
    monkeypatch.setattr(geo, "MIN_SECONDS_BETWEEN_REQUESTS", 0)
    entries = [
        {"name": "X", "address": "Nonexistent Street 1"},
        {"name": "Y", "address": "Imaginary Road 2"},
    ]
    with pytest.raises(GeocodingError) as err:
        load_stops(entries, cache_path=None)
    assert "Nonexistent Street 1" in str(err.value)
    assert "Imaginary Road 2" in str(err.value)


def test_geocoding_results_are_cached(monkeypatch, tmp_path):
    calls = []

    def fake(address, **kwargs):
        calls.append(address)
        return (6.45, 3.40)

    monkeypatch.setattr(geo, "geocode_address", fake)
    cache = tmp_path / "cache.json"
    entries = [{"name": "Shop", "address": "  1 Test Road, Lagos "}]

    first = load_stops(entries, cache_path=cache)
    second = load_stops(entries, cache_path=cache)

    assert first == second == [Stop("Shop", 6.45, 3.40)]
    assert len(calls) == 1  # second run served from cache
    assert json.loads(cache.read_text())


def test_default_name_is_assigned():
    stops = load_stops([{"lat": 6.5, "lng": 3.4}], cache_path=None)
    assert stops[0].name == "Stop 1"


# --- map output ----------------------------------------------------------------


def test_render_map_writes_numbered_html(tmp_path):
    stops = [Stop("Depot", 6.5, 3.37), Stop("Shop <b>1</b>", 6.45, 3.42)]
    out = render_map(
        stops, straight_line_route(stops), "held_karp", tmp_path / "m.html", percent_saved=12.5
    )
    html = out.read_text(encoding="utf-8")
    assert "Saved vs. input order" in html and "12.5%" in html
    assert "Straight-line estimate" in html  # fallback is disclosed on the map
    # Stop names are user input, so they must be HTML-escaped everywhere they appear.
    assert "Shop <b>1</b>" not in html
    assert "Shop &lt;b&gt;1&lt;/b&gt;" in html


def test_render_map_requires_stops(tmp_path):
    with pytest.raises(ValueError):
        render_map([], straight_line_route([]), "held_karp", tmp_path / "m.html")
