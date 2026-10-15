# Delivery Route Optimizer

Give it a list of delivery stops and it returns the best order to visit them, minimizing a
weighted mix of travel time and distance, then draws the route. Built for delivery riders
(originally targeting Lagos).

- **Version 1** is the engine plus a command-line tool (`main.py`). It still works exactly as before.
- **Version 2** wraps the engine in a FastAPI backend with accounts and saved trips, stored in
  PostgreSQL (Neon) through SQLAlchemy, with Alembic migrations.

```
optimizer/   the engine: geocoding, travel-cost matrix, solvers, road geometry, map (version 1)
app/         the API: routes, schemas, models, auth, background runs   (version 2)
             app/worker.py is the optional separate worker process
alembic/     database migrations
tests/       245 tests
main.py      the command-line tool (version 1)
```

---

## Running the API

### 1. Install

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt                    # use requirements.txt on a server
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env`. The settings that matter first:

| Setting | What to put |
|---|---|
| `DATABASE_URL` | Neon's **pooled** connection string (host contains `-pooler`). Paste it exactly as Neon shows it. |
| `ALEMBIC_DATABASE_URL` | Neon's **direct** connection string (no `-pooler`), used only for migrations. |
| `SECRET_KEY` | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `EMAIL_BACKEND` | `console` for now: verification and reset links are printed in the terminal running the server. Switch to `smtp` (any provider that offers SMTP) before real users. |
| `MATRIX_BACKEND` | `osrm` for real road data, or `haversine` for a rough offline estimate. |

Everything else has a sensible default; see `.env.example` for the full list.

### 3. Create the tables, then start the server

```bash
alembic upgrade head
uvicorn app.main:create_app --factory --reload
```

Open http://localhost:8000/docs for the interactive API page. Sign up, copy the verification
link from the server terminal, verify, then use **Authorize** (your email as the username).

---

## What the API does

| Endpoint | Purpose |
|---|---|
| `POST /auth/signup`, `/auth/verify-email`, `/auth/resend-verification` | Email sign-up with confirmation |
| `POST /auth/login`, `GET /auth/me` | Log in (returns an access token and a refresh token), current user |
| `POST /auth/refresh` | Swap a refresh token for a new access token **and a new refresh token** (the old one stops working) |
| `POST /auth/logout` | End this device's session (send its refresh token) |
| `POST /auth/logout-all` | Sign out of every device (needs the access token) |
| `POST /auth/forgot-password`, `/auth/reset-password` | Password reset by emailed link (each link works once; resetting also signs out every device) |
| `GET /geocode/search?q=` | Address suggestions for the app's search box |
| `POST /optimize` | One-off: stops in, best order out. Nothing is saved |
| `POST /trips` | Save a trip and start optimizing it (returns at once; poll the trip) |
| `GET /trips` | Your trips, newest first. Filters: `starred`, `q`, `starred_first`, paging |
| `GET /trips/{id}` | Open a trip. Returns the **stored** result; nothing is recalculated |
| `PATCH /trips/{id}` | Rename, star or unstar |
| `DELETE /trips/{id}` | Delete (soft: recoverable in the database) |
| `POST /trips/{id}/modify` | Edit by saving a **modified copy**; the original never changes |
| `POST /trips/{id}/runs` | Re-optimize on purpose; adds a run, keeps the old ones |
| `GET /trips/{id}/runs`, `/runs/{run_id}` | Run history and a specific past result |
| `GET /health`, `/health/ready` | Liveness / database check |

**How a trip behaves.** Creating a trip returns immediately with `latest_run.status` of `queued`;
the solve runs in the background and the app polls `GET /trips/{id}` until it is `succeeded`.
The result holds the stops in visiting order (`route.stops`), totals, and the road path as an
encoded polyline (`route.polyline`, precision 6, which Mapbox GL, Google Maps and most map
libraries can decode). A failed run reports why in `latest_run.error_message`, and the last
successful result keeps showing.

**Modifying.** `POST /trips/{id}/modify` takes any of `title`, `stops` (the complete new list),
`start_index`, `open_path`, `preset`/weights. Anything left out is copied. The copy is a separate
trip whose `parent_trip_id` points to the original. Sending nothing duplicates the trip.

**Stops** are `{name, lat, lng}` (what the app sends after the user picks a search result or
drops a pin) or just `{address}`, which the server geocodes and caches.

### Running the solver in a separate worker (optional)

By default (`RUN_MODE=inline`) the API solves trips itself in the background: simplest, fine for one
server. For anything beyond that, run the solver as its own process:

```bash
# API: only queues runs
RUN_MODE=worker uvicorn app.main:create_app --factory
# Worker: same .env, separate process (start more copies to solve more trips at once)
python -m app.worker
```

The `optimization_runs` table is the queue. A worker claims the oldest queued run in one short
transaction (`FOR UPDATE SKIP LOCKED` on PostgreSQL, so several workers never take the same run),
sends a heartbeat every third of `RUN_LEASE_SECONDS` while solving, and writes the result only if
it still owns the run. Behaviour you get over inline mode:

- **Survives restarts and crashes.** A run whose worker stopped heartbeating is put back in the
  queue (or failed once `RUN_MAX_ATTEMPTS` is used up). On SIGTERM/Ctrl+C the worker lets running
  solves finish for `WORKER_SHUTDOWN_GRACE_S`, then hands unfinished ones straight back.
- **Retries** routing-service outages and unexpected errors, waiting attempt x `RUN_RETRY_BACKOFF_S`
  between tries. Bad input (for example "no road route between A and B") is never retried.
- While waiting for a retry the run stays `queued` and `error_message` says why.

Run `alembic upgrade head` first (migration `0003` adds the queue columns). With no worker running,
queued runs simply wait, so keep one running. Note that a polling worker keeps a Neon database awake
(it won't scale to zero while the worker is up); raise `WORKER_POLL_INTERVAL_S` if that costs you.

### Staying logged in (refresh tokens)

`/auth/login` returns `access_token` (15 min) and `refresh_token` (30 days). Send the access
token as `Authorization: Bearer ...`. When the API answers 401, call `POST /auth/refresh` with the
refresh token, then **save both new tokens**: every refresh token works once. Store the refresh
token somewhere safe (Keychain/Keystore on mobile).

Refresh tokens are random strings stored only as SHA-256 hashes in `refresh_tokens`. One login is
one *session* (a chain of tokens). If an already-used token is presented again, the server assumes
it was stolen and signs that session out. Don't retry a refresh whose response you never received
more than once, or you may sign yourself out. Logout ends one session; password reset and
`/auth/logout-all` end all of them.

### Address search: Nominatim or Mapbox

`GEOCODER_PROVIDER=nominatim` (default) is free but its public server forbids heavy use and
autocomplete, so treat it as development-only. For production set `GEOCODER_PROVIDER=mapbox` and
`MAPBOX_ACCESS_TOKEN`. Mapbox only lets you store results requested with `permanent=true`
(billed differently, needs a card on file), which is why `MAPBOX_PERMANENT` defaults to true:
trips store coordinates. Check Mapbox's current pricing and terms before launch, and test a batch
of real Lagos addresses to compare quality.

### Neon notes

- The app converts Neon's connection string itself (`sslmode`/`channel_binding` are translated
  for the asyncpg driver). Use the **pooled** string for `DATABASE_URL` and the **direct** string
  for `ALEMBIC_DATABASE_URL`; Neon recommends direct connections for migrations.
- Neon's pooler supports protocol-level prepared statements, so asyncpg works with its defaults.
  If you ever see `prepared statement ... does not exist`, set `DB_DISABLE_STATEMENT_CACHE=true`.
- Neon can suspend an idle database; the first request afterwards may be slow. The connection
  pool tests connections before use, so this should cause delay, not errors.

---

## Command-line tool (version 1)

```bash
python main.py sample_data/lagos_stops.json --start 0 --open-path
python main.py sample_data/lagos_stops.json --backend osrm --osrm-url http://localhost:5000
```

Options: `--start`, `--open-path` / `--closed-loop`, `--preset fastest|shortest|balanced`,
`--weights T D`, `--backend haversine|osrm`, `--osrm-url`, `--speed`, `--time-limit`, `--output`.
It writes `route_map.html`. Input is a JSON list of `{name, lat, lng}` or `{name, address}`.

## How the engine works

1. Travel times and distances between every pair of stops (OSRM, or an offline estimate).
2. Time and distance are normalized and combined: `cost = w_time × time + w_dist × distance`
   (default 70/30; presets `fastest`, `shortest`, `balanced`).
3. The order is found by exact Held-Karp dynamic programming up to 10 stops, then OR-Tools
   (cheapest-arc start + Guided Local Search) beyond that. Open-path routes use a zero-cost dummy end.
4. The road geometry for the chosen order comes from OSRM (straight lines, clearly flagged, if unavailable).

The offline estimate multiplies straight-line distance by 1.3 and assumes 25 km/h. It is for
development and tests only.

## Tests

```bash
pytest                                    # SQLite, no setup needed
```

For a stricter check, run against a real PostgreSQL. Use an **empty scratch database**, never your
real one: the tests drop and recreate every table.

```bash
TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/routeopt_test \
TEST_MIGRATION_DATABASE_URL=postgresql://user:pass@localhost:5432/routeopt_mig pytest
```

(A free Neon branch also works for this.) The migration test confirms Alembic builds exactly the
schema the models describe and that it downgrades cleanly.

## Known limits (deliberate for now)

- **Inline mode (the default) solves inside the web process.** If the server restarts mid-solve, that
  run is marked failed at the next startup (after `RUN_TIMEOUT_MINUTES`) and can be re-run, and there
  are no retries. Use `RUN_MODE=worker` plus `python -m app.worker` (see above) for restarts,
  retries and several solvers.
- **The worker polls** the database (no push notification), so a new run starts within
  `WORKER_POLL_INTERVAL_S` seconds. It also doesn't prioritise: runs are taken oldest first.
- **OSRM:** the public demo server is for development only. Self-host it with the Nigeria
  OpenStreetMap extract before real use.
- **Access tokens can't be revoked.** They last 15 minutes (`ACCESS_TOKEN_EXPIRE_MINUTES`), so after logout or a password reset an already-issued access token still works until it expires. Refresh tokens last 30 days (`REFRESH_TOKEN_EXPIRE_DAYS`), restart on every refresh, and are revoked immediately.
- **No rate limiting** on login or the other endpoints yet.
- **No live traffic, time windows, multiple riders, or mid-route re-optimization** (version 3).

See `route-optimizer-implementation-plan.md` for the design and the roadmap.
