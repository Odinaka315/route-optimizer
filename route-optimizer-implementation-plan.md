# Delivery Route Optimizer: Implementation Plan

## 1. Project Summary

**Goal:** An app where users (primarily delivery riders) add multiple destination addresses and receive the best visiting sequence that minimizes both travel time and travel distance, shown on a map with the actual road route.

**Primary market context:** Lagos, Nigeria (ambiguous addresses, heavy traffic, cost-sensitive, so self-hosted open-source map tooling is preferred over per-request paid APIs).

**Problem class:** Traveling Salesman Problem (TSP), asymmetric variant (A→B cost differs from B→A), extending to Vehicle Routing Problem (VRP) in version 2.

**Key principle:** The *solver* decides the ORDER of stops. A *routing/directions engine* draws the real road path between consecutive stops. These are separate concerns.

---

## 2. Version 1 (MVP Prototype): Scope

A Python command-line tool that:
1. Accepts a list of stops (addresses or lat/lng).
2. Builds a travel time + distance cost matrix from real road data.
3. Computes the best visiting order.
4. Generates the real road geometry for that order.
5. Outputs an HTML map and a printed summary.

### In scope for V1
- Single rider, single route
- Optional fixed start point
- Open-path (no return to start) or closed loop, selectable
- Weighted time/distance cost
- Exact solver for small inputs, heuristic solver for larger inputs
- Offline fallback (straight-line distance) for testing
- Map output via Folium
- Automated tests

### Explicitly out of scope for V1
- Mobile UI, user accounts, database
- Live traffic data
- Time windows, capacities, pickup/dropoff pairs
- Multiple riders/vehicles
- Production OSRM hosting
- Re-optimization mid-route

---

## 3. Algorithm Strategy

| Number of stops | Algorithm | Notes |
|---|---|---|
| ≤ 10 | Held-Karp dynamic programming (exact) | O(n²·2ⁿ). Guaranteed optimal, instant. |
| 11+ | Google OR-Tools routing solver | Greedy first solution (PATH_CHEAPEST_ARC) + Guided Local Search metaheuristic, with a time limit (5-10 s). Typically within 1-5% of optimal. |

**Heuristic building blocks (for reference/understanding):** nearest neighbor and cheapest insertion for initial solutions; 2-opt and Or-opt for local improvement; simulated annealing, tabu search, or guided local search to escape local optima; LKH (Lin-Kernighan-Helsgaun) as the state-of-the-art alternative.

**Combined cost function:**
```
cost[i][j] = w_time * time_seconds[i][j] + w_dist * distance_meters[i][j]
```
Default weights: `w_time = 0.7`, `w_dist = 0.3` (after normalizing both matrices to comparable scales, e.g. divide each by its mean or max). Weights are user-configurable. Presets: "fastest" (1.0 / 0.0), "shortest" (0.0 / 1.0), "balanced" (0.7 / 0.3).

**Open-path handling:** Add a dummy end node whose cost to and from every real stop is 0. The solver then finds the best path ending anywhere. For a fixed start, force the route to begin at the start index.

---

## 4. Project Structure

```
route-optimizer/
├── README.md
├── requirements.txt
├── main.py                # CLI entry point
├── sample_data/
│   └── lagos_stops.json   # ~10 sample stops with lat/lng
├── optimizer/
│   ├── __init__.py
│   ├── geo.py             # geocoding / coordinate parsing
│   ├── matrix.py          # cost matrix builders (OSRM + Haversine)
│   ├── solvers.py         # Held-Karp + OR-Tools
│   ├── route.py           # road geometry via OSRM /route
│   └── render.py          # Folium map output
└── tests/
    ├── test_solvers.py
    ├── test_matrix.py
    └── test_edge_cases.py
```

**Dependencies:** `ortools`, `requests`, `folium`, `pytest`

---

## 5. Module Specifications

### 5.1 `geo.py`: Addresses to coordinates
- Input: list of entries, each either `{"name": str, "address": str}` or `{"name": str, "lat": float, "lng": float}`.
- Entries with lat/lng pass straight through.
- Entries with only an address are geocoded with Nominatim (OpenStreetMap). Respect its usage policy: max 1 request/second, set a descriptive User-Agent.
- Cache geocoding results locally (JSON file) to avoid repeat lookups.
- Raise a clear error listing any addresses that failed to geocode.
- Output: list of `Stop(name, lat, lng)` objects.

### 5.2 `matrix.py`: Cost matrix
Common interface:
```python
class MatrixBackend:
    def build(self, stops: list[Stop]) -> tuple[list[list[float]], list[list[float]]]:
        """Return (time_seconds_matrix, distance_meters_matrix)."""
```
Two implementations:
- **OsrmBackend:** calls `GET {base_url}/table/v1/driving/{lng,lat;...}?annotations=duration,distance`. Base URL configurable (public demo server for testing, self-hosted for production). Handle `null` entries (unreachable pairs) by assigning a very large penalty cost and warning the user.
- **HaversineBackend:** great-circle distance, with an assumed average speed (default 25 km/h to approximate Lagos conditions, configurable) to derive time. Requires no network.

Plus a function `combine(time_m, dist_m, w_time, w_dist) -> cost_matrix` that normalizes and weights.

Note: OSRM's public demo server limits request size and is rate-limited. For more than ~100 stops or any real usage, self-host OSRM.

### 5.3 `solvers.py`: Sequencing engine
Common return shape:
```python
@dataclass
class Solution:
    order: list[int]      # indices of stops in visiting order
    total_cost: float
    solver: str           # "held_karp" or "ortools"
```
- `held_karp(cost, start=0, open_path=True) -> Solution`
- `ortools_solve(cost, start=0, open_path=True, time_limit_s=10) -> Solution`
- `solve(cost, ...)` dispatcher: Held-Karp if n ≤ 10, else OR-Tools.

OR-Tools details: scale float costs to integers (multiply by 100 or 1000 and round), as OR-Tools requires integer arc costs. Use `routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC` and `LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH`.

### 5.4 `route.py`: Road geometry
- For the final ordered stops, call OSRM `GET {base_url}/route/v1/driving/{coords}?overview=full&geometries=geojson&steps=false`.
- Return the full polyline (list of lat/lng), total distance, and total duration.
- Fall back to straight lines between stops if OSRM is unavailable (and flag this clearly in output).

### 5.5 `render.py`: Map output
- Folium map centered on the stops.
- Numbered markers in visit order (start marker highlighted).
- Route polyline drawn from the geometry.
- Info box with total distance, estimated time, solver used, and (optionally) percent saved versus the input order.
- Save as `route_map.html`.

### 5.6 `main.py`: CLI
Example usage:
```
python main.py sample_data/lagos_stops.json --start 0 --open-path --weights 0.7 0.3 --backend osrm --osrm-url http://localhost:5000
```
Flags: `--start`, `--open-path / --closed-loop`, `--weights`, `--backend {osrm,haversine}`, `--osrm-url`, `--time-limit`, `--output`.
Prints the ordered stop list with names, total distance/time, percent improvement over input order, and writes the map file.

---

## 6. Testing Plan

1. **Solver correctness:** on random small instances (n = 5-9), Held-Karp and OR-Tools should return equal (or near-equal within ~1%) total cost.
2. **Improvement check:** the optimized route's cost must be ≤ the input-order cost; report percentage saved.
3. **Edge cases:** 1 stop, 2 stops, duplicate coordinates, a stop with unreachable (null) matrix entries, start index out of range, empty input.
4. **Open vs closed:** open-path cost must be ≤ closed-loop cost for the same data.
5. **Matrix:** Haversine matrix is symmetric with zero diagonal; combined cost respects weight presets.
6. **Matrix backend mocking:** test OSRM parsing with a recorded sample response (no network needed in CI).

---

## 7. Build Order (Milestones)

1. Project scaffold, `requirements.txt`, sample Lagos data.
2. `matrix.py` with Haversine backend + tests.
3. `solvers.py`: Held-Karp first, then OR-Tools; tests comparing both.
4. Open-path and fixed-start support.
5. OSRM matrix backend.
6. `route.py` geometry + `render.py` map.
7. `main.py` CLI wiring.
8. Edge-case tests, README with usage instructions.

---

## 8. Version 2: Planned Improvements

### 8.1 Backend/API
- Wrap the engine in a **FastAPI** service: `POST /optimize` accepting stops + options, returning order, totals, and route geometry as JSON.
- Async job handling for large inputs (return a job ID, poll for result).
- Request validation, rate limiting, API-key auth.

### 8.2 Real-world delivery constraints (move from TSP to VRP)
- **Time windows:** "deliver between 2 pm and 4 pm" per stop (OR-Tools time dimension).
- **Service time per stop:** e.g. 3-5 minutes at each drop-off.
- **Multiple riders:** split stops across N riders, balanced workload (multi-vehicle VRP).
- **Capacity limits:** parcels, weight, or volume per rider.
- **Pickup-and-delivery pairs:** pickup must precede its drop-off.
- **Priority stops:** urgent deliveries weighted or forced earlier.
- **Rider shift limits:** maximum working hours or distance.

### 8.3 Traffic and live conditions
- Time-of-day-aware travel times (historical traffic profiles for Lagos peak hours), or integrate Google Routes API / Mapbox traffic-aware matrix for live traffic.
- **Re-optimization:** re-run from the rider's current GPS position when a stop is added/cancelled or traffic changes.
- Optional detection of deviation from the planned route.

### 8.4 Data quality and infrastructure
- **Self-host OSRM (or Valhalla)** with the Nigeria OpenStreetMap extract; document setup (Docker image, `osrm-extract`/`osrm-partition`/`osrm-customize`, MLD algorithm).
- Better geocoding for Lagos: address autocomplete, "drop a pin to confirm," fuzzy matching, caching, and a fallback geocoder (Photon, Pelias, or Google Places) when Nominatim is ambiguous.
- Matrix caching/precomputation for frequently used areas; chunked matrix requests for large inputs.

### 8.5 Algorithm upgrades
- Evaluate **VROOM** (open-source VRP engine that talks to OSRM/Valhalla) against OR-Tools for speed and quality.
- Optional **LKH** integration for best-quality large single-route solves.
- Clustering (k-means / sweep algorithm) to pre-partition large stop sets before multi-rider solving.
- Benchmark suite: solution quality and solve time vs. stop count.

### 8.6 Frontend (mobile app)
- Flutter or React Native app.
- Map screen with Google Maps SDK or Mapbox GL; draggable pins; numbered stop markers; live rider position.
- Address entry with autocomplete, CSV import of stop lists, and drag-to-manually-reorder with instant cost recalculation.
- Turn-by-turn navigation handoff (open in Google Maps/Waze with waypoints) or in-app navigation.
- Offline tolerance: cache the computed route so it survives poor network.

### 8.7 Product features
- User accounts, saved routes and frequent addresses.
- Route history and analytics (distance and time saved, fuel estimate).
- Proof of delivery (photo/signature), status updates per stop.
- Customer ETA notifications.
- Dispatcher dashboard to manage multiple riders.

### 8.8 Operations
- Dockerize the backend and OSRM; CI pipeline running tests.
- Logging, error monitoring, and cost tracking for any paid APIs.
- Load testing for concurrent optimization requests.

---

## 9. Key Decisions and Assumptions

- **Cost metric:** weighted combination of time and distance; defaults 70/30; user-adjustable.
- **Routing data:** OpenStreetMap via OSRM, chosen for zero per-request cost. Google APIs are the upgrade path if live traffic accuracy is needed.
- **Solver library:** OR-Tools chosen for proven quality, VRP extensibility, and a free license.
- **Asymmetric costs:** the matrix is not assumed symmetric (one-way streets, traffic direction).
- **Straight-line fallback speed:** 25 km/h default for Lagos-like urban conditions (tunable).
- **Language:** Python 3.10+.

---

## 10. Notes for Whoever Continues This Project

- Build in the milestone order in section 7; each step is independently testable.
- Keep the matrix backend and solver behind the interfaces described above so they can be swapped (e.g. OSRM → Google, OR-Tools → VROOM) without touching the rest.
- Do not use the public OSRM demo server for anything beyond development.
- Scale integer costs before passing to OR-Tools, and normalize time and distance before weighting them. These two details commonly cause bad results when missed.

---

## 11. Version 2 as built (FastAPI backend)

**Status:** Built and tested. 204 tests pass on SQLite and 205 on PostgreSQL 16 (the extra one
is the Postgres migration check), including all version 1 tests. A live `uvicorn` server was
also exercised against Postgres end to end. **Not yet run against Neon itself or a real Mapbox
account** (the build environment could not reach either): run `alembic upgrade head` against a
Neon branch first, and try Mapbox with a few real Lagos addresses.

### 11.1 Stack and decisions
- FastAPI, Pydantic v2 (+ pydantic-settings), SQLAlchemy 2.0 async with asyncpg, Alembic (async env).
- Accounts: **email + password** (Argon2 hashing). Email verification required before login
  (switchable). Stateless signed tokens (JWT): 24 h access token; verification and reset links are
  signed, expiring and purpose-bound; reset links are single-use.
- Email: pluggable sender; `console` (development) or any SMTP provider.
- Geocoding: pluggable; Nominatim (development) or **Mapbox Geocoding v6** (production). Mapbox
  results are only stored when requested with `permanent=true`.
- Engine runs in a worker thread, at most `MAX_CONCURRENT_SOLVES` at a time. Saved trips are
  solved by a background task after the response is sent; the app polls.
- Neon: pooled URL for the app, direct URL for migrations. The pooler supports protocol-level
  prepared statements, so asyncpg needs no special setting (an escape hatch exists).
- Correction to an earlier note in this project: it was suggested that the asyncpg statement cache
  must be disabled behind Neon's pooler. Neon's documentation says its PgBouncer supports
  protocol-level prepared statements, so this is off by default.

### 11.2 Schema (migration `0001`)
`users` (id, email unique lowercase, password_hash, display_name, is_active, email_verified_at,
timestamps) · `trips` (id, user_id, title, parent_trip_id, is_starred, open_path, weight_time,
weight_dist, timestamps, deleted_at) · `trip_stops` (id, trip_id, input_position, name,
address_text, lat, lng, geocode_source, place_provider_id, is_start, notes) · `optimization_runs`
(id, trip_id, status queued/running/succeeded/failed, solver, matrix_backend, totals,
percent_saved, route_polyline, geometry_is_fallback, error_message, created_at, finished_at) ·
`run_stops` (run_id, trip_stop_id, visit_order, leg_distance_m, leg_duration_s) ·
`geocode_cache` (provider, country, query_normalized, lat, lng, formatted_address, provider_place_id).

Rules enforced by the database: one start stop per trip; unique stop positions; unique
(run, stop); run status constrained; cascade deletes from user → trips → stops/runs/run_stops;
deleting a parent trip clears (not deletes) its copies' `parent_trip_id`.

### 11.3 Behaviour
- Opening a saved trip reads the stored result; it never re-solves. Re-solving is an explicit
  `POST /trips/{id}/runs`, which adds a run and keeps history.
- Modifying a trip creates a separate trip (copied stops, `parent_trip_id` set). The original is untouched.
- Starring is `is_starred` on the trip (PATCH). Deleting is soft (`deleted_at`).
- A user can only ever see their own trips; another user's trip returns 404.

### 11.4 Version 2 items from section 8 that are done
FastAPI service and `POST /optimize` · request validation · API-key-free JWT auth · saved trips,
history, accounts · geocoding via Mapbox/Nominatim with a database cache · re-optimization of a
saved trip · stop-count and concurrency limits.

### 11.5 Still open (carry into version 3)
Refresh tokens and session management · rate limiting · a real job queue · self-hosted OSRM +
Nigeria extract · live/time-of-day traffic · time windows, service times, capacities, multiple
riders, pickup/delivery pairs · customer ETAs, proof of delivery, dispatcher dashboard ·
Dockerfile and CI · frontend (mobile) app · saved places / address book · trip sharing.


## 12. Refresh tokens (added after V2)

- Table `refresh_tokens` (migration `0002`): `id`, `user_id` (cascade), `family_id` (one login = one session),
  `token_hash` (SHA-256, unique), `created_at`, `expires_at`, `revoked_at`, `replaced_by_id`.
- Access token 15 min (JWT, unchanged); refresh token 30 days, opaque random string, rotated on every use.
- `POST /auth/refresh` (rotate; atomic claim so concurrent refreshes cannot both succeed), `POST /auth/logout`
  (revokes the session's family), `POST /auth/logout-all` (revokes all of the user's tokens).
- Reuse detection: presenting an already-rotated token revokes its whole family.
- Password reset revokes all of the user's refresh tokens.
- Expired tokens are purged at login (per user) and at startup (after a 7-day grace so replays are still recognised).
- Open: access tokens are not revocable (max 15 min of residual access); no per-device names/list endpoint;
  no rate limiting on `/auth/refresh` or `/auth/login`; no short grace window for a client that retries a
  refresh whose response was lost.


## 13. Separate background worker (added after refresh tokens)

- `RUN_MODE=inline` (default, unchanged) or `worker`. In worker mode the API only inserts the queued run
  (and skips the startup sweep); `python -m app.worker` executes it.
- Postgres is the queue: `optimization_runs` gained `attempts`, `run_after` (retry back-off), `started_at`,
  `heartbeat_at` (the lease), `locked_by`, plus index `(status, created_at)` (migration `0003`).
- Claim = `SELECT ... FOR UPDATE SKIP LOCKED` + guarded `UPDATE ... WHERE status='queued'` (portable to SQLite).
- Heartbeat every lease/3 while solving; the result is stored only if the run is still `running` and
  `locked_by` this worker, otherwise it is discarded (another worker owns the run now).
- Lease reaper (every 15 s per worker): expired `running` runs go back to `queued`, or to `failed` when
  `attempts >= RUN_MAX_ATTEMPTS`.
- Retries: `RoutingServiceError`, unexpected exceptions and failed saves retry with back-off
  `attempt x RUN_RETRY_BACKOFF_S`; other `EngineError`s fail immediately.
- Graceful shutdown: stop claiming, wait `WORKER_SHUTDOWN_GRACE_S`, hand unfinished runs back (attempt not counted).
- Open: polling, not LISTEN/NOTIFY (and polling keeps a Neon database awake); no job priorities; no
  worker status endpoint or metrics; `find_active_run` still uses `RUN_TIMEOUT_MINUTES`, so a run queued
  longer than that (no worker running) no longer blocks a new run; no dead-letter view for failed runs;
  Windows has no SIGTERM handler (Ctrl+C works).
