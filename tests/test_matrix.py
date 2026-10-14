import math
import warnings

import pytest

from optimizer.geo import Stop
from optimizer.matrix import (
    PRESETS,
    HaversineBackend,
    OsrmBackend,
    combine,
    haversine_m,
    parse_osrm_table,
)

STOPS = [
    Stop("A", 6.5095, 3.3711),
    Stop("B", 6.4281, 3.4219),
    Stop("C", 6.6140, 3.3580),
    Stop("D", 6.4698, 3.5852),
]

# A recorded-style OSRM /table response (3 stops, one unreachable pair).
OSRM_SAMPLE = {
    "code": "Ok",
    "durations": [[0, 600, 900], [620, 0, None], [880, 1200, 0]],
    "distances": [[0, 8000, 12000], [8100, 0, None], [11800, 15000, 0]],
}


def test_haversine_known_distance():
    # One degree of latitude is about 111.2 km.
    assert haversine_m(0, 0, 1, 0) == pytest.approx(111_195, rel=0.001)


def test_haversine_matrices_symmetric_with_zero_diagonal():
    time_m, dist_m = HaversineBackend().build(STOPS)
    n = len(STOPS)
    for i in range(n):
        assert time_m[i][i] == 0 and dist_m[i][i] == 0
        for j in range(n):
            assert time_m[i][j] == pytest.approx(time_m[j][i])
            assert dist_m[i][j] == pytest.approx(dist_m[j][i])


def test_haversine_time_follows_speed_and_detour():
    _, slow = HaversineBackend(avg_speed_kmh=10, detour_factor=1.0).build(STOPS)
    time_slow, _ = HaversineBackend(avg_speed_kmh=10, detour_factor=1.0).build(STOPS)
    time_fast, _ = HaversineBackend(avg_speed_kmh=20, detour_factor=1.0).build(STOPS)
    assert time_slow[0][1] == pytest.approx(2 * time_fast[0][1])
    _, detoured = HaversineBackend(detour_factor=1.5).build(STOPS)
    _, straight = HaversineBackend(detour_factor=1.0).build(STOPS)
    assert detoured[0][1] == pytest.approx(1.5 * straight[0][1])
    assert slow[0][1] == pytest.approx(straight[0][1])


def test_backend_rejects_bad_parameters():
    with pytest.raises(ValueError):
        HaversineBackend(avg_speed_kmh=0)
    with pytest.raises(ValueError):
        HaversineBackend(detour_factor=0.5)


def test_weight_presets_select_time_or_distance():
    time_m, dist_m = HaversineBackend().build(STOPS)
    # Make the two matrices disagree so the presets are distinguishable.
    dist_m = [[v * (1 + 0.3 * ((i + j) % 3)) for j, v in enumerate(row)] for i, row in enumerate(dist_m)]
    fastest = combine(time_m, dist_m, *PRESETS["fastest"])
    shortest = combine(time_m, dist_m, *PRESETS["shortest"])
    only_time = combine(time_m, dist_m, 1.0, 0.0)
    only_dist = combine(time_m, dist_m, 0.0, 1.0)
    assert fastest == only_time
    assert shortest == only_dist
    assert fastest != shortest


def test_combine_normalizes_so_units_do_not_dominate():
    time_m = [[0, 1000, 2000], [1000, 0, 1000], [2000, 1000, 0]]  # seconds
    dist_m = [[0, 1, 2], [1, 0, 1], [2, 1, 0]]  # same shape, tiny scale
    balanced = combine(time_m, dist_m, 0.5, 0.5)
    assert balanced == combine(time_m, dist_m, 1.0, 0.0)


@pytest.mark.parametrize("weights", [(0, 0), (-1, 1), (1, -0.5)])
def test_combine_rejects_bad_weights(weights):
    time_m, dist_m = HaversineBackend().build(STOPS)
    with pytest.raises(ValueError):
        combine(time_m, dist_m, *weights)


def test_combine_rejects_mismatched_sizes():
    with pytest.raises(ValueError):
        combine([[0, 1], [1, 0]], [[0]], 0.5, 0.5)


def test_parse_osrm_table_maps_null_to_inf():
    durations, distances = parse_osrm_table(OSRM_SAMPLE)
    assert durations[0][1] == 600
    assert distances[2][1] == 15000
    assert math.isinf(durations[1][2]) and math.isinf(distances[1][2])


def test_parse_osrm_table_rejects_error_responses():
    with pytest.raises(RuntimeError):
        parse_osrm_table({"code": "InvalidQuery", "message": "bad"})
    with pytest.raises(RuntimeError):
        parse_osrm_table({"code": "Ok"})


def test_unreachable_pairs_get_penalty_and_warning():
    durations, distances = parse_osrm_table(OSRM_SAMPLE)
    with pytest.warns(UserWarning, match="unreachable"):
        cost = combine(durations, distances, 0.7, 0.3)
    finite = [cost[i][j] for i in range(3) for j in range(3) if (i, j) != (1, 2) and i != j]
    assert all(math.isfinite(v) for row in cost for v in row)
    assert cost[1][2] > 100 * max(finite)


def test_no_warning_when_everything_reachable():
    time_m, dist_m = HaversineBackend().build(STOPS)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        combine(time_m, dist_m)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return FakeResponse(self.payload)


def test_osrm_backend_builds_request_and_parses_response():
    session = FakeSession(OSRM_SAMPLE)
    backend = OsrmBackend(base_url="http://osrm.local:5000/", session=session)
    time_m, dist_m = backend.build(STOPS[:3])
    url, params = session.calls[0]
    # OSRM expects lng,lat order, joined by semicolons, and no double slash.
    assert url == (
        "http://osrm.local:5000/table/v1/driving/"
        "3.3711,6.5095;3.4219,6.4281;3.358,6.614"
    )
    assert params == {"annotations": "duration,distance"}
    assert time_m[0][2] == 900 and dist_m[1][0] == 8100
