"""Sequencing engine: Held-Karp (exact) and OR-Tools (heuristic)."""

from __future__ import annotations

import math
from dataclasses import dataclass

Matrix = list[list[float]]

HELD_KARP_MAX_STOPS = 13  # hard cap: memory/time grow as 2^n
DEFAULT_EXACT_THRESHOLD = 10


@dataclass
class Solution:
    order: list[int]  # stop indices in visiting order (starts at `start`)
    total_cost: float
    solver: str  # "held_karp" or "ortools"


def _check_inputs(cost: Matrix, start: int) -> int:
    n = len(cost)
    if n == 0:
        raise ValueError("Cannot solve an empty problem: provide at least one stop.")
    if any(len(row) != n for row in cost):
        raise ValueError("Cost matrix must be square.")
    if not (0 <= start < n):
        raise ValueError(f"start index {start} is out of range for {n} stops.")
    return n


def route_cost(matrix: Matrix, order: list[int], open_path: bool = True) -> float:
    """Total of `matrix` along `order`. For a closed loop, includes the return leg."""
    total = 0.0
    for a, b in zip(order, order[1:]):
        total += matrix[a][b]
    if not open_path and len(order) > 1:
        total += matrix[order[-1]][order[0]]
    return total


def baseline_order(n: int, start: int = 0) -> list[int]:
    """The user's input order, rotated so it begins at `start`."""
    return [start] + [i for i in range(n) if i != start]


def held_karp(cost: Matrix, start: int = 0, open_path: bool = True) -> Solution:
    """Exact solution by dynamic programming over subsets: O(n^2 * 2^n)."""
    n = _check_inputs(cost, start)
    if n > HELD_KARP_MAX_STOPS:
        raise ValueError(
            f"Held-Karp supports at most {HELD_KARP_MAX_STOPS} stops (got {n}); "
            "use ortools_solve instead."
        )
    if n == 1:
        return Solution([start], 0.0, "held_karp")

    others = [i for i in range(n) if i != start]
    m = len(others)
    size = 1 << m
    inf = math.inf
    # dp[mask][j]: cheapest way to leave `start`, visit the set `mask`, end at others[j]
    dp = [[inf] * m for _ in range(size)]
    parent = [[-1] * m for _ in range(size)]
    for j in range(m):
        dp[1 << j][j] = cost[start][others[j]]

    for mask in range(1, size):
        for j in range(m):
            if not (mask >> j) & 1:
                continue
            cur = dp[mask][j]
            if cur == inf:
                continue
            for k in range(m):
                if (mask >> k) & 1:
                    continue
                nmask = mask | (1 << k)
                cand = cur + cost[others[j]][others[k]]
                if cand < dp[nmask][k]:
                    dp[nmask][k] = cand
                    parent[nmask][k] = j

    full = size - 1
    best, best_j = inf, -1
    for j in range(m):
        total = dp[full][j] + (0.0 if open_path else cost[others[j]][start])
        if total < best:
            best, best_j = total, j

    seq: list[int] = []
    mask, j = full, best_j
    while j != -1:
        seq.append(others[j])
        prev = parent[mask][j]
        mask ^= 1 << j
        j = prev
    seq.reverse()
    order = [start] + seq
    return Solution(order, route_cost(cost, order, open_path), "held_karp")


def ortools_solve(
    cost: Matrix,
    start: int = 0,
    open_path: bool = True,
    time_limit_s: int = 10,
) -> Solution:
    """Heuristic solution: PATH_CHEAPEST_ARC first guess, then Guided Local Search.

    OR-Tools needs integer arc costs, so floats are scaled before solving. The
    returned total_cost is recomputed from the original float matrix.
    """
    from ortools.constraint_solver import pywrapcp, routing_enums_pb2

    n = _check_inputs(cost, start)
    if n == 1:
        return Solution([start], 0.0, "ortools")
    if n == 2:
        order = [start, 1 - start]
        return Solution(order, route_cost(cost, order, open_path), "ortools")

    positives = [cost[i][j] for i in range(n) for j in range(n) if i != j and cost[i][j] > 0]
    mean = sum(positives) / len(positives) if positives else 1.0
    scale = 10_000.0 / mean  # mean arc cost ~ 10,000 integer units

    size = n + 1 if open_path else n
    int_cost = [[0] * size for _ in range(size)]
    for i in range(n):
        for j in range(n):
            int_cost[i][j] = int(round(cost[i][j] * scale))
    # Open path: dummy end node (index n) costs 0 to reach from any stop,
    # so the route may finish wherever is cheapest.

    end_node = n if open_path else start
    manager = pywrapcp.RoutingIndexManager(size, 1, [start], [end_node])
    routing = pywrapcp.RoutingModel(manager)

    def arc_cost(from_index: int, to_index: int) -> int:
        return int_cost[manager.IndexToNode(from_index)][manager.IndexToNode(to_index)]

    transit = routing.RegisterTransitCallback(arc_cost)
    routing.SetArcCostEvaluatorOfAllVehicles(transit)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    params.time_limit.FromSeconds(max(1, int(time_limit_s)))

    solution = routing.SolveWithParameters(params)
    if solution is None:
        raise RuntimeError("OR-Tools could not find a route for these stops.")

    order: list[int] = []
    index = routing.Start(0)
    while not routing.IsEnd(index):
        order.append(manager.IndexToNode(index))
        index = solution.Value(routing.NextVar(index))
    return Solution(order, route_cost(cost, order, open_path), "ortools")


def solve(
    cost: Matrix,
    start: int = 0,
    open_path: bool = True,
    time_limit_s: int = 10,
    exact_threshold: int = DEFAULT_EXACT_THRESHOLD,
) -> Solution:
    """Held-Karp for small inputs, OR-Tools beyond `exact_threshold` stops."""
    n = _check_inputs(cost, start)
    if n <= exact_threshold:
        return held_karp(cost, start, open_path)
    return ortools_solve(cost, start, open_path, time_limit_s)
