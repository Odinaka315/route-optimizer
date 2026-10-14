#!/usr/bin/env python3
"""Command-line entry point: find the best stop order and draw it on a map.

Example:
    python main.py sample_data/lagos_stops.json --start 0 --open-path \
        --weights 0.7 0.3 --backend haversine
"""

from __future__ import annotations

import argparse
import sys
import warnings

import requests

from optimizer.geo import GeocodingError, load_stops_from_file
from optimizer.matrix import PRESETS, HaversineBackend, OsrmBackend, combine
from optimizer.render import render_map
from optimizer.route import DEFAULT_OSRM_URL, get_route
from optimizer.solvers import baseline_order, route_cost, solve


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Find the best visiting order for a list of delivery stops."
    )
    p.add_argument("stops_file", help="JSON file: list of {name, lat, lng} or {name, address}")
    p.add_argument("--start", type=int, default=0, help="index of the starting stop (default 0)")
    loop = p.add_mutually_exclusive_group()
    loop.add_argument(
        "--open-path", dest="open_path", action="store_true", default=True,
        help="finish at the last stop (default)",
    )
    loop.add_argument(
        "--closed-loop", dest="open_path", action="store_false",
        help="return to the starting stop at the end",
    )
    p.add_argument(
        "--weights", nargs=2, type=float, metavar=("W_TIME", "W_DIST"),
        help="cost weights for time and distance (overrides --preset)",
    )
    p.add_argument(
        "--preset", choices=sorted(PRESETS), default="balanced",
        help="weight preset: fastest, shortest or balanced (default)",
    )
    p.add_argument(
        "--backend", choices=["osrm", "haversine"], default="haversine",
        help="travel-cost source: real roads via OSRM, or offline straight-line estimate",
    )
    p.add_argument("--osrm-url", default=None, help=f"OSRM server (default {DEFAULT_OSRM_URL})")
    p.add_argument("--speed", type=float, default=25.0, help="assumed km/h for the haversine backend")
    p.add_argument("--time-limit", type=int, default=10, help="OR-Tools search seconds (11+ stops)")
    p.add_argument("--output", default="route_map.html", help="map file to write")
    return p


def _fmt_duration(seconds: float) -> str:
    minutes = int(round(seconds / 60.0))
    hours, mins = divmod(minutes, 60)
    return f"{hours} h {mins} min" if hours else f"{mins} min"


def run(args: argparse.Namespace) -> int:
    stops = load_stops_from_file(args.stops_file)
    n = len(stops)
    if n == 0:
        raise ValueError("The stops file is empty: add at least one stop.")
    if not (0 <= args.start < n):
        raise ValueError(f"--start {args.start} is out of range; there are {n} stops (0-{n - 1}).")

    w_time, w_dist = args.weights if args.weights else PRESETS[args.preset]

    osrm_url = args.osrm_url or DEFAULT_OSRM_URL
    if args.backend == "osrm":
        backend = OsrmBackend(base_url=osrm_url)
    else:
        backend = HaversineBackend(avg_speed_kmh=args.speed)
    time_m, dist_m = backend.build(stops)
    cost = combine(time_m, dist_m, w_time, w_dist)

    solution = solve(cost, start=args.start, open_path=args.open_path, time_limit_s=args.time_limit)
    order = solution.order

    base = baseline_order(n, args.start)
    base_cost = route_cost(cost, base, args.open_path)
    percent_saved = (base_cost - solution.total_cost) / base_cost * 100.0 if base_cost > 0 else 0.0

    total_time = route_cost(time_m, order, args.open_path)
    total_dist = route_cost(dist_m, order, args.open_path)

    ordered = [stops[i] for i in order]
    path_stops = ordered + [ordered[0]] if (not args.open_path and n > 1) else ordered
    geometry = get_route(
        path_stops,
        base_url=osrm_url if args.backend == "osrm" else None,
        avg_speed_kmh=args.speed,
    )

    out = render_map(
        ordered, geometry, solution.solver, args.output,
        percent_saved=percent_saved, open_path=args.open_path,
    )

    kind = "open path" if args.open_path else "closed loop"
    print(f"Best sequence ({n} stops, {kind}, solver: {solution.solver}):")
    for pos, i in enumerate(order, start=1):
        print(f"  {pos:>2}. {stops[i].name}")
    if not args.open_path and n > 1:
        print(f"  --> back to {stops[order[0]].name}")
    print()
    print(f"Distance : {total_dist / 1000.0:.1f} km")
    print(f"Est. time: {_fmt_duration(total_time)}")
    print(f"Weights  : time={w_time:g}, distance={w_dist:g}")
    print(f"Saved vs. input order: {percent_saved:.1f}%")
    if args.backend == "haversine":
        print("Note: figures are straight-line estimates; use --backend osrm for real roads.")
    if geometry.is_fallback and args.backend == "osrm":
        print("Note: road geometry unavailable, map shows straight lines.")
    print(f"Map written to {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    warnings.simplefilter("always")
    try:
        return run(args)
    except (ValueError, GeocodingError, RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
    except requests.RequestException as exc:
        print(
            f"error: network request failed ({exc.__class__.__name__}). "
            "Try --backend haversine to run offline.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
