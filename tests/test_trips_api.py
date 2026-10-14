import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import GeocodeCache, OptimizationRun, Trip, TripStop, utcnow
from app.services import jobs
from app.services.engine import EngineError
from app.services.jobs import sweep_stale_runs
from app.services.polyline import decode_polyline

from .conftest import LAGOS, stop, stops


async def make_trip(env, headers, title="Morning run", **extra) -> dict:
    r = await env.client.post("/trips", headers=headers, json={"title": title, "stops": stops(), **extra})
    assert r.status_code == 201, r.text
    return r.json()


async def get_trip(env, headers, trip_id) -> dict:
    r = await env.client.get(f"/trips/{trip_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# --- creating and opening -----------------------------------------------------------


async def test_create_returns_immediately_then_the_run_completes(env, auth):
    created = await make_trip(env, auth)
    assert created["latest_run"]["status"] == "queued" and created["route"] is None
    assert [s["name"] for s in created["stops"]] == [n for n, _, _ in LAGOS]  # input order
    assert sum(s["is_start"] for s in created["stops"]) == 1 and created["stops"][0]["is_start"]

    trip = await get_trip(env, auth, created["id"])
    assert trip["latest_run"]["status"] == "succeeded"
    route = trip["route"]
    assert route["solver"] == "held_karp" and route["matrix_backend"] == "haversine"
    assert [s["visit_order"] for s in route["stops"]] == list(range(6))
    assert sorted(s["name"] for s in route["stops"]) == sorted(n for n, _, _ in LAGOS)
    assert route["stops"][0]["name"] == "Depot - Yaba"
    assert route["stops"][0]["leg_distance_m"] is None and route["stops"][1]["leg_distance_m"] > 0
    assert route["total_distance_m"] > 0 and route["percent_saved"] >= 0
    assert len(decode_polyline(route["polyline"])) == 6 and route["polyline_precision"] == 6
    assert route["geometry_is_fallback"] is True


async def test_opening_a_trip_does_not_solve_again(env, auth):
    created = await make_trip(env, auth)
    first = await get_trip(env, auth, created["id"])
    second = await get_trip(env, auth, created["id"])
    assert first["route"] == second["route"]
    runs = (await env.client.get(f"/trips/{created['id']}/runs", headers=auth)).json()
    assert len(runs) == 1


async def test_start_index_closed_loop_and_weights(env, auth):
    created = await make_trip(env, auth, start_index=3, open_path=False, preset="fastest")
    trip = await get_trip(env, auth, created["id"])
    assert trip["route"]["stops"][0]["name"] == "Victoria Island"
    assert trip["open_path"] is False and (trip["weight_time"], trip["weight_dist"]) == (1.0, 0.0)
    points = decode_polyline(trip["route"]["polyline"])
    assert points[0] == points[-1]  # the loop comes back to the start

    explicit = await make_trip(env, auth, weight_time=0.2, weight_dist=0.8)
    assert (explicit["weight_time"], explicit["weight_dist"]) == (0.2, 0.8)
    default = await make_trip(env, auth)
    assert (default["weight_time"], default["weight_dist"]) == (0.7, 0.3)


@pytest.mark.parametrize(
    "payload",
    [
        {"stops": stops(1)},  # a route needs two stops
        {"stops": [{"name": "x"}, stop(1)]},  # no location
        {"stops": [{"lat": 6.5}, stop(1)]},  # lat without lng
        {"stops": [{"lat": 123, "lng": 3.4}, stop(1)]},  # impossible latitude
        {"stops": stops(3), "start_index": 3},  # start outside the list
        {"stops": stops(3), "weight_time": 0.5},  # one weight only
        {"stops": stops(3), "weight_time": 0, "weight_dist": 0},
        {"stops": stops(3), "preset": "scenic"},
        {"stops": stops(3), "title": ""},
    ],
)
async def test_invalid_trips_are_rejected(env, auth, payload):
    assert (await env.client.post("/trips", headers=auth, json=payload)).status_code == 422


async def test_stop_limit_comes_from_settings(env, auth):
    too_many = [{"name": f"S{i}", "lat": 6.4 + i * 0.01, "lng": 3.4} for i in range(13)]  # limit is 12
    r = await env.client.post("/trips", headers=auth, json={"stops": too_many})
    assert r.status_code == 422 and "at most 12" in r.json()["detail"]


async def test_address_only_stops_are_geocoded_and_cached(env, auth, sessionmaker):
    payload = {"stops": [stop(0), {"address": "10 Allen Avenue, Ikeja"}]}
    r = await env.client.post("/trips", headers=auth, json=payload)
    assert r.status_code == 201
    located = r.json()["stops"][1]
    assert (located["lat"], located["lng"]) == (6.6018, 3.3515)
    assert located["geocode_source"] == "fake" and located["place_provider_id"].startswith("fake:")
    assert located["name"] == "10 Allen Avenue"  # the geocoder's name; the address itself is kept too
    assert located["address"] == "10 Allen Avenue, Ikeja"

    await env.client.post("/trips", headers=auth, json={"stops": [stop(1), {"address": "  10 allen AVENUE,  ikeja "}]})
    assert len(env.geocoder.calls) == 1  # second trip came from the cache
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count(GeocodeCache.id))) == 1


async def test_addresses_are_not_cached_when_the_provider_forbids_storage(env, auth, sessionmaker):
    env.geocoder.storable = False
    for _ in range(2):
        await env.client.post("/trips", headers=auth, json={"stops": [stop(0), {"address": "10 Allen Avenue, Ikeja"}]})
    assert len(env.geocoder.calls) == 2
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count(GeocodeCache.id))) == 0


async def test_unlocatable_addresses_are_listed_and_nothing_is_saved(env, auth, sessionmaker):
    payload = {"stops": [stop(0), {"address": "Nowhere Street 1"}, {"address": "Imaginary Road 2"}]}
    r = await env.client.post("/trips", headers=auth, json=payload)
    assert r.status_code == 422
    assert r.json()["detail"]["unresolved"] == ["'Nowhere Street 1'", "'Imaginary Road 2'"]
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count(Trip.id))) == 0


async def test_geocoder_outage_gives_a_clear_503(env, auth):
    env.geocoder.fail = True
    r = await env.client.post("/trips", headers=auth, json={"stops": [stop(0), {"address": "10 Allen Avenue, Ikeja"}]})
    assert r.status_code == 503 and "coordinates" in r.json()["detail"]


async def test_default_title_is_generated(env, auth):
    r = await env.client.post("/trips", headers=auth, json={"stops": stops(3)})
    assert r.json()["title"].startswith("Trip ")


# --- listing, starring, renaming, deleting ---------------------------------------------


async def test_list_is_newest_first_with_summaries(env, auth):
    ids = [(await make_trip(env, auth, title=f"T{i}"))["id"] for i in range(3)]
    body = (await env.client.get("/trips", headers=auth)).json()
    assert [t["title"] for t in body["items"]] == ["T2", "T1", "T0"] and body["total"] == 3
    newest = body["items"][0]
    assert newest["id"] == ids[2] and newest["stop_count"] == 6 and newest["is_starred"] is False
    assert newest["latest_run"]["status"] == "succeeded" and newest["total_distance_m"] > 0


async def test_list_pagination(env, auth):
    for i in range(5):
        await make_trip(env, auth, title=f"T{i}")
    page = (await env.client.get("/trips", headers=auth, params={"limit": 2, "offset": 2})).json()
    assert [t["title"] for t in page["items"]] == ["T2", "T1"]
    assert (page["total"], page["limit"], page["offset"]) == (5, 2, 2)
    beyond = (await env.client.get("/trips", headers=auth, params={"offset": 50})).json()
    assert beyond["items"] == [] and beyond["total"] == 5
    assert (await env.client.get("/trips", headers=auth, params={"limit": 0})).status_code == 422


async def test_starring_and_filtering(env, auth):
    a, b, c = [(await make_trip(env, auth, title=t))["id"] for t in ("A", "B", "C")]
    r = await env.client.patch(f"/trips/{a}", headers=auth, json={"is_starred": True})
    assert r.status_code == 200 and r.json()["is_starred"] is True

    starred = (await env.client.get("/trips", headers=auth, params={"starred": True})).json()
    assert [t["title"] for t in starred["items"]] == ["A"] and starred["total"] == 1
    unstarred = (await env.client.get("/trips", headers=auth, params={"starred": False})).json()
    assert [t["title"] for t in unstarred["items"]] == ["C", "B"]
    on_top = (await env.client.get("/trips", headers=auth, params={"starred_first": True})).json()
    assert [t["title"] for t in on_top["items"]] == ["A", "C", "B"]

    await env.client.patch(f"/trips/{a}", headers=auth, json={"is_starred": False})
    assert (await env.client.get("/trips", headers=auth, params={"starred": True})).json()["total"] == 0


async def test_title_search_treats_wildcards_literally(env, auth):
    await make_trip(env, auth, title="100% legit run")
    await make_trip(env, auth, title="snake_case run")
    await make_trip(env, auth, title="Plain trip")
    titles = lambda body: sorted(t["title"] for t in body["items"])  # noqa: E731
    assert titles((await env.client.get("/trips", headers=auth, params={"q": "legit"})).json()) == ["100% legit run"]
    assert titles((await env.client.get("/trips", headers=auth, params={"q": "100%"})).json()) == ["100% legit run"]
    assert titles((await env.client.get("/trips", headers=auth, params={"q": "_"})).json()) == ["snake_case run"]
    assert titles((await env.client.get("/trips", headers=auth, params={"q": "PLAIN"})).json()) == ["Plain trip"]


async def test_rename_and_empty_patch(env, auth):
    trip_id = (await make_trip(env, auth))["id"]
    r = await env.client.patch(f"/trips/{trip_id}", headers=auth, json={"title": "Renamed"})
    assert r.json()["title"] == "Renamed"
    assert (await env.client.patch(f"/trips/{trip_id}", headers=auth, json={})).status_code == 422
    assert (await env.client.patch(f"/trips/{trip_id}", headers=auth, json={"title": ""})).status_code == 422


async def test_delete_is_soft_and_hides_the_trip(env, auth, sessionmaker):
    trip_id = (await make_trip(env, auth))["id"]
    assert (await env.client.delete(f"/trips/{trip_id}", headers=auth)).status_code == 204
    assert (await env.client.get(f"/trips/{trip_id}", headers=auth)).status_code == 404
    assert (await env.client.get("/trips", headers=auth)).json()["total"] == 0
    assert (await env.client.delete(f"/trips/{trip_id}", headers=auth)).status_code == 404
    async with sessionmaker() as session:  # still in the database, so it can be recovered
        trip = await session.get(Trip, uuid.UUID(trip_id))
        assert trip is not None and trip.deleted_at is not None


async def test_unknown_ids_are_404(env, auth):
    missing = uuid.uuid4()
    assert (await env.client.get(f"/trips/{missing}", headers=auth)).status_code == 404
    assert (await env.client.get("/trips/not-a-uuid", headers=auth)).status_code == 422


async def test_people_cannot_see_or_touch_each_others_trips(env, auth, other_auth):
    trip_id = (await make_trip(env, auth))["id"]
    run_id = (await env.client.get(f"/trips/{trip_id}/runs", headers=auth)).json()[0]["id"]
    for method, path, body in [
        ("GET", f"/trips/{trip_id}", None),
        ("PATCH", f"/trips/{trip_id}", {"is_starred": True}),
        ("DELETE", f"/trips/{trip_id}", None),
        ("POST", f"/trips/{trip_id}/modify", {}),
        ("POST", f"/trips/{trip_id}/runs", None),
        ("GET", f"/trips/{trip_id}/runs", None),
        ("GET", f"/trips/{trip_id}/runs/{run_id}", None),
    ]:
        r = await env.client.request(method, path, headers=other_auth, json=body)
        assert r.status_code == 404, (method, path, r.status_code)
    assert (await env.client.get("/trips", headers=other_auth)).json()["total"] == 0
    assert (await get_trip(env, auth, trip_id))["is_starred"] is False  # untouched


# --- modifying (a new, separate trip) -------------------------------------------------------


async def test_modify_saves_a_separate_trip_and_leaves_the_original_alone(env, auth):
    original = await make_trip(env, auth, title="Original")
    before = await get_trip(env, auth, original["id"])

    r = await env.client.post(
        f"/trips/{original['id']}/modify", headers=auth, json={"stops": stops(3), "title": "Shorter"}
    )
    assert r.status_code == 201
    copy = await get_trip(env, auth, r.json()["id"])
    assert copy["id"] != original["id"] and copy["parent_trip_id"] == original["id"]
    assert copy["title"] == "Shorter" and len(copy["stops"]) == 3
    assert copy["route"] is not None and len(copy["route"]["stops"]) == 3

    after = await get_trip(env, auth, original["id"])
    assert after == before  # stops, route, run: nothing about the original changed
    assert (await env.client.get("/trips", headers=auth)).json()["total"] == 2


async def test_modify_without_changes_duplicates_the_trip(env, auth):
    original = await make_trip(env, auth, title="Original", start_index=2, open_path=False, weight_time=0.4, weight_dist=0.6)
    r = await env.client.post(f"/trips/{original['id']}/modify", headers=auth, json={})
    copy = await get_trip(env, auth, r.json()["id"])
    assert copy["title"] == "Original (edited)" and copy["parent_trip_id"] == original["id"]
    assert [s["name"] for s in copy["stops"]] == [n for n, _, _ in LAGOS]
    assert copy["stops"][2]["is_start"] and copy["open_path"] is False
    assert (copy["weight_time"], copy["weight_dist"]) == (0.4, 0.6)
    assert copy["stops"][0]["id"] != original["stops"][0]["id"]  # stops are copied, not shared


async def test_modify_can_change_just_settings(env, auth):
    original = await make_trip(env, auth)
    r = await env.client.post(
        f"/trips/{original['id']}/modify", headers=auth, json={"open_path": False, "preset": "shortest", "start_index": 4}
    )
    copy = await get_trip(env, auth, r.json()["id"])
    assert copy["open_path"] is False and (copy["weight_time"], copy["weight_dist"]) == (0.0, 1.0)
    assert copy["stops"][4]["is_start"] and copy["route"]["stops"][0]["name"] == "Lekki Phase 1"
    assert len(copy["stops"]) == 6


async def test_modify_validation(env, auth):
    trip_id = (await make_trip(env, auth))["id"]
    url = f"/trips/{trip_id}/modify"
    assert (await env.client.post(url, headers=auth, json={"start_index": 6})).status_code == 422
    assert (await env.client.post(url, headers=auth, json={"stops": stops(1)})).status_code == 422
    assert (await env.client.post(url, headers=auth, json={"stops": stops(3), "start_index": 3})).status_code == 422
    assert (await env.client.post(url, headers=auth, json={"weight_time": 1.0})).status_code == 422


async def test_modifications_chain_and_survive_deleting_a_parent(env, auth):
    a = await make_trip(env, auth, title="A")
    b = (await env.client.post(f"/trips/{a['id']}/modify", headers=auth, json={"title": "B"})).json()
    c = (await env.client.post(f"/trips/{b['id']}/modify", headers=auth, json={"title": "C"})).json()
    assert c["parent_trip_id"] == b["id"] and b["parent_trip_id"] == a["id"]
    await env.client.delete(f"/trips/{a['id']}", headers=auth)
    assert (await get_trip(env, auth, b["id"]))["parent_trip_id"] == a["id"]  # link kept
    assert (await env.client.post(f"/trips/{a['id']}/modify", headers=auth, json={})).status_code == 404


# --- re-optimizing and run history ----------------------------------------------------------


async def test_reoptimizing_adds_a_run_and_keeps_history(env, auth):
    trip_id = (await make_trip(env, auth))["id"]
    r = await env.client.post(f"/trips/{trip_id}/runs", headers=auth)
    assert r.status_code == 202 and r.json()["status"] == "queued"

    runs = (await env.client.get(f"/trips/{trip_id}/runs", headers=auth)).json()
    assert len(runs) == 2 and all(run["status"] == "succeeded" for run in runs)
    assert runs[0]["id"] == r.json()["id"]  # newest first
    assert runs[0]["total_distance_m"] > 0 and runs[0]["solver"] == "held_karp"

    detail = (await env.client.get(f"/trips/{trip_id}/runs/{runs[1]['id']}", headers=auth)).json()
    assert detail["status"] == "succeeded" and len(detail["route"]["stops"]) == 6
    assert (await get_trip(env, auth, trip_id))["route"]["run_id"] == runs[0]["id"]  # opens the latest


async def test_run_of_another_trip_is_not_reachable(env, auth):
    a, b = (await make_trip(env, auth))["id"], (await make_trip(env, auth))["id"]
    run_of_a = (await env.client.get(f"/trips/{a}/runs", headers=auth)).json()[0]["id"]
    assert (await env.client.get(f"/trips/{b}/runs/{run_of_a}", headers=auth)).status_code == 404


async def test_a_run_already_in_progress_blocks_a_second_one(env, auth, sessionmaker):
    trip_id = (await make_trip(env, auth))["id"]
    async with sessionmaker() as session:
        session.add(OptimizationRun(trip_id=uuid.UUID(trip_id), status="running"))
        await session.commit()
    assert (await env.client.post(f"/trips/{trip_id}/runs", headers=auth)).status_code == 409


async def test_a_run_stuck_past_the_timeout_no_longer_blocks(env, auth, sessionmaker):
    trip_id = (await make_trip(env, auth))["id"]
    async with sessionmaker() as session:
        session.add(
            OptimizationRun(trip_id=uuid.UUID(trip_id), status="running", created_at=utcnow() - timedelta(hours=1))
        )
        await session.commit()
    assert (await env.client.post(f"/trips/{trip_id}/runs", headers=auth)).status_code == 202


async def test_failed_run_reports_why_and_keeps_the_last_good_route(env, auth, monkeypatch):
    trip_id = (await make_trip(env, auth))["id"]
    good_route = (await get_trip(env, auth, trip_id))["route"]

    def broken(*args, **kwargs):
        raise EngineError("There is no road route between 'A' and 'B'.")

    monkeypatch.setattr(jobs, "run_engine", broken)
    await env.client.post(f"/trips/{trip_id}/runs", headers=auth)

    trip = await get_trip(env, auth, trip_id)
    assert trip["latest_run"]["status"] == "failed" and "no road route" in trip["latest_run"]["error_message"]
    assert trip["route"] == good_route  # the earlier successful result is still shown
    summary = (await env.client.get("/trips", headers=auth)).json()["items"][0]
    assert summary["latest_run"]["status"] == "failed" and summary["total_distance_m"] > 0


async def test_first_run_failing_leaves_no_route(env, auth, monkeypatch):
    def broken(*args, **kwargs):
        raise EngineError("Could not reach the routing service. Try again shortly.")

    monkeypatch.setattr(jobs, "run_engine", broken)
    trip_id = (await make_trip(env, auth))["id"]
    trip = await get_trip(env, auth, trip_id)
    assert trip["route"] is None and trip["latest_run"]["status"] == "failed"
    run = (await env.client.get(f"/trips/{trip_id}/runs/{trip['latest_run']['id']}", headers=auth)).json()
    assert run["route"] is None and run["finished_at"] is not None

    monkeypatch.undo()  # routing service is back: re-optimizing recovers the trip
    await env.client.post(f"/trips/{trip_id}/runs", headers=auth)
    assert (await get_trip(env, auth, trip_id))["route"] is not None


async def test_unexpected_errors_do_not_leak_internals(env, auth, monkeypatch):
    def explode(*args, **kwargs):
        raise ZeroDivisionError("secret internal detail")

    monkeypatch.setattr(jobs, "run_engine", explode)
    trip_id = (await make_trip(env, auth))["id"]
    error = (await get_trip(env, auth, trip_id))["latest_run"]["error_message"]
    assert "secret" not in error and error.startswith("Unexpected error")


async def test_stale_runs_are_swept(env, auth, sessionmaker):
    trip_id = uuid.UUID((await make_trip(env, auth))["id"])
    async with sessionmaker() as session:
        old = OptimizationRun(trip_id=trip_id, status="running", created_at=utcnow() - timedelta(hours=2))
        fresh = OptimizationRun(trip_id=trip_id, status="queued")
        session.add_all([old, fresh])
        await session.commit()
        old_id, fresh_id = old.id, fresh.id

    assert await sweep_stale_runs(sessionmaker, older_than_minutes=10) == 1
    async with sessionmaker() as session:
        assert (await session.get(OptimizationRun, old_id)).status == "failed"
        assert (await session.get(OptimizationRun, fresh_id)).status == "queued"
        assert "Interrupted" in (await session.get(OptimizationRun, old_id)).error_message


async def test_startup_sweeps_stuck_runs(env, auth, sessionmaker):
    trip_id = uuid.UUID((await make_trip(env, auth))["id"])
    async with sessionmaker() as session:
        stuck = OptimizationRun(trip_id=trip_id, status="running", created_at=utcnow() - timedelta(hours=2))
        session.add(stuck)
        await session.commit()
        stuck_id = stuck.id
    async with env.app.router.lifespan_context(env.app):
        pass
    async with sessionmaker() as session:
        assert (await session.get(OptimizationRun, stuck_id)).status == "failed"


async def test_stops_survive_with_their_extra_details(env, auth, sessionmaker):
    payload = {
        "stops": [
            {**stop(0), "geocode_source": "manual_pin", "notes": "Gate B, ask for Ngozi"},
            {**stop(1), "place_provider_id": "mapbox:abc", "geocode_source": "mapbox", "address": "1 Test Road"},
        ]
    }
    created = (await env.client.post("/trips", headers=auth, json=payload)).json()
    first, second = created["stops"]
    assert first["notes"] == "Gate B, ask for Ngozi" and first["geocode_source"] == "manual_pin"
    assert second["place_provider_id"] == "mapbox:abc" and second["address"] == "1 Test Road"
    async with sessionmaker() as session:
        rows = (await session.scalars(select(TripStop).where(TripStop.trip_id == uuid.UUID(created["id"])))).all()
        assert sorted(r.input_position for r in rows) == [0, 1]
