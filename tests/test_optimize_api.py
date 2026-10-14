"""/optimize, /geocode/search, health and CORS."""

import pytest
from sqlalchemy import func, select

from app.models import Trip
from app.routers import optimize as optimize_router
from app.services.engine import EngineError, RoutingServiceError
from app.services.polyline import decode_polyline

from .conftest import LAGOS, stop, stops


async def test_optimize_returns_the_route_without_saving_anything(env, auth, sessionmaker):
    r = await env.client.post("/optimize", headers=auth, json={"stops": stops(), "start_index": 1})
    assert r.status_code == 200
    route = r.json()
    assert route["run_id"] is None and route["solver"] == "held_karp"
    assert route["stops"][0]["name"] == "Surulere"
    assert [s["visit_order"] for s in route["stops"]] == list(range(6))
    assert all(s["trip_stop_id"] is None for s in route["stops"])
    assert route["total_distance_m"] > 0 and len(decode_polyline(route["polyline"])) == 6
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count(Trip.id))) == 0


async def test_optimize_closed_loop_and_weights(env, auth):
    base = {"stops": stops()}
    open_route = (await env.client.post("/optimize", headers=auth, json=base)).json()
    closed = (await env.client.post("/optimize", headers=auth, json={**base, "open_path": False, "preset": "shortest"})).json()
    assert closed["total_distance_m"] > open_route["total_distance_m"]
    points = decode_polyline(closed["polyline"])
    assert points[0] == points[-1]


async def test_optimize_geocodes_address_only_stops(env, auth):
    r = await env.client.post("/optimize", headers=auth, json={"stops": [stop(0), {"address": "10 Allen Avenue, Ikeja"}]})
    assert r.status_code == 200
    assert {s["address"] for s in r.json()["stops"]} == {None, "10 Allen Avenue, Ikeja"}


async def test_optimize_requires_login_and_valid_input(env, auth):
    assert (await env.client.post("/optimize", json={"stops": stops()})).status_code == 401
    assert (await env.client.post("/optimize", headers=auth, json={"stops": stops(1)})).status_code == 422
    assert (await env.client.post("/optimize", headers=auth, json={"stops": stops(), "start_index": 9})).status_code == 422
    too_many = [{"lat": 6.4 + i * 0.01, "lng": 3.4} for i in range(13)]
    assert (await env.client.post("/optimize", headers=auth, json={"stops": too_many})).status_code == 422


async def test_optimize_error_mapping(env, auth, monkeypatch):
    def unreachable(*a, **k):
        raise EngineError("There is no road route between 'A' and 'B'.")

    monkeypatch.setattr(optimize_router, "run_engine", unreachable)
    r = await env.client.post("/optimize", headers=auth, json={"stops": stops()})
    assert r.status_code == 422 and "no road route" in r.json()["detail"]

    def down(*a, **k):
        raise RoutingServiceError("Could not reach the routing service. Try again shortly.")

    monkeypatch.setattr(optimize_router, "run_engine", down)
    r = await env.client.post("/optimize", headers=auth, json={"stops": stops()})
    assert r.status_code == 502 and "Try again" in r.json()["detail"]


# --- address search -------------------------------------------------------------------


async def test_geocode_search_returns_candidates(env, auth):
    r = await env.client.get("/geocode/search", headers=auth, params={"q": "allen avenue", "limit": 3})
    assert r.status_code == 200
    (hit,) = r.json()["results"]
    assert (hit["lat"], hit["lng"]) == (6.6018, 3.3515) and hit["provider"] == "fake"
    call = env.geocoder.calls[-1]
    assert call["autocomplete"] is True and call["limit"] == 3 and call["proximity"] is None


async def test_geocode_search_passes_the_bias_point_through(env, auth):
    await env.client.get("/geocode/search", headers=auth, params={"q": "allen", "lat": 6.5, "lng": 3.4})
    assert env.geocoder.calls[-1]["proximity"] == (6.5, 3.4)


async def test_geocode_search_validation_and_outage(env, auth):
    assert (await env.client.get("/geocode/search", params={"q": "allen"})).status_code == 401
    assert (await env.client.get("/geocode/search", headers=auth, params={"q": "a"})).status_code == 422
    assert (await env.client.get("/geocode/search", headers=auth, params={"q": "allen", "lat": 6.5})).status_code == 422
    assert (await env.client.get("/geocode/search", headers=auth, params={"q": "allen", "limit": 11})).status_code == 422
    env.geocoder.fail = True
    r = await env.client.get("/geocode/search", headers=auth, params={"q": "allen avenue"})
    assert r.status_code == 503 and "provider down" not in r.text  # internals not leaked


# --- health and CORS -----------------------------------------------------------------------


async def test_health_endpoints(env):
    assert (await env.client.get("/health")).json()["status"] == "ok"
    assert (await env.client.get("/health/ready")).json() == {"status": "ready"}


async def test_readiness_reports_a_dead_database(env, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    async def broken(self, *args, **kwargs):
        raise ConnectionError("db gone")

    monkeypatch.setattr(AsyncSession, "execute", broken)
    assert (await env.client.get("/health/ready")).status_code == 503


@pytest.mark.parametrize("origin, allowed", [("http://localhost:3000", True), ("http://evil.example", False)])
async def test_cors_only_allows_configured_origins(app_factory, origin, allowed):
    env = await app_factory(cors_origins="http://localhost:3000")
    r = await env.client.options(
        "/trips", headers={"Origin": origin, "Access-Control-Request-Method": "GET"}
    )
    assert (r.headers.get("access-control-allow-origin") == origin) is allowed


async def test_no_cors_headers_when_not_configured(env):
    r = await env.client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert "access-control-allow-origin" not in r.headers
