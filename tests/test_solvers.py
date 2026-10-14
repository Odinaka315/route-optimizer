import itertools
import random

import pytest

from optimizer.solvers import (
    baseline_order,
    held_karp,
    ortools_solve,
    route_cost,
    solve,
)


def random_cost(n, seed):
    """Asymmetric random cost matrix (A->B differs from B->A)."""
    rng = random.Random(seed)
    return [[0.0 if i == j else rng.uniform(1, 100) for j in range(n)] for i in range(n)]


def brute_force(cost, start, open_path):
    n = len(cost)
    others = [i for i in range(n) if i != start]
    best = min(
        route_cost(cost, [start, *perm], open_path)
        for perm in itertools.permutations(others)
    )
    return best


def assert_valid_order(order, n, start):
    assert order[0] == start
    assert sorted(order) == list(range(n))


@pytest.mark.parametrize("open_path", [True, False])
@pytest.mark.parametrize("n", [3, 5, 7])
def test_held_karp_matches_brute_force(n, open_path):
    cost = random_cost(n, seed=n * 10 + int(open_path))
    sol = held_karp(cost, start=0, open_path=open_path)
    assert_valid_order(sol.order, n, 0)
    assert sol.total_cost == pytest.approx(brute_force(cost, 0, open_path))


@pytest.mark.parametrize("start", [0, 2, 4])
def test_held_karp_respects_start(start):
    cost = random_cost(6, seed=3)
    sol = held_karp(cost, start=start)
    assert_valid_order(sol.order, 6, start)
    assert sol.total_cost == pytest.approx(brute_force(cost, start, True))


@pytest.mark.parametrize("open_path", [True, False])
@pytest.mark.parametrize("n", [5, 7, 9])
def test_ortools_agrees_with_exact_on_small_instances(n, open_path):
    cost = random_cost(n, seed=100 + n)
    exact = held_karp(cost, open_path=open_path)
    heuristic = ortools_solve(cost, open_path=open_path, time_limit_s=1)
    assert_valid_order(heuristic.order, n, 0)
    # Heuristic must never beat the exact optimum, and should land within 1%.
    assert heuristic.total_cost >= exact.total_cost - 1e-6
    assert heuristic.total_cost <= exact.total_cost * 1.01


def test_ortools_respects_start():
    cost = random_cost(8, seed=5)
    sol = ortools_solve(cost, start=3, time_limit_s=1)
    assert_valid_order(sol.order, 8, 3)


@pytest.mark.parametrize("open_path", [True, False])
def test_optimized_route_never_worse_than_input_order(open_path):
    cost = random_cost(8, seed=11)
    sol = solve(cost, open_path=open_path)
    base = route_cost(cost, baseline_order(8, 0), open_path)
    assert sol.total_cost <= base + 1e-9


def test_open_path_never_costs_more_than_closed_loop():
    for seed in range(5):
        cost = random_cost(7, seed=seed)
        open_cost = held_karp(cost, open_path=True).total_cost
        closed_cost = held_karp(cost, open_path=False).total_cost
        assert open_cost <= closed_cost + 1e-9


def test_dispatcher_uses_exact_for_small_and_ortools_for_large():
    assert solve(random_cost(10, 1)).solver == "held_karp"
    big = solve(random_cost(12, 1), time_limit_s=1)
    assert big.solver == "ortools"
    assert_valid_order(big.order, 12, 0)


def test_route_cost_helper():
    cost = [[0, 1, 5], [2, 0, 3], [4, 6, 0]]
    assert route_cost(cost, [0, 1, 2], open_path=True) == 4
    assert route_cost(cost, [0, 1, 2], open_path=False) == 8
    assert route_cost(cost, [0], open_path=False) == 0
