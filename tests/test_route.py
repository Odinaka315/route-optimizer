import warnings

import pytest
import requests

from optimizer.geo import Stop
from optimizer.route import (
    fetch_route,
    get_route,
    parse_osrm_route,
    straight_line_route,
)

STOPS = [
    Stop("A", 6.5095, 3.3711),
    Stop("B", 6.4281, 3.4219),
    Stop("C", 6.4698, 3.5852),
]

ROUTE_SAMPLE = {
    "code": "Ok",
    "routes": [
        {
            "distance": 25000.0,
            "duration": 2400.0,
            # GeoJSON order is [lng, lat]
            "geometry": {"coordinates": [[3.3711, 6.5095], [3.40, 6.45], [3.4219, 6.4281]]},
        }
    ],
}


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


def test_parse_osrm_route_flips_lng_lat_to_lat_lng():
    geom = parse_osrm_route(ROUTE_SAMPLE)
    assert geom.points[0] == (6.5095, 3.3711)
    assert geom.points[-1] == (6.4281, 3.4219)
    assert geom.distance_m == 25000.0
    assert geom.duration_s == 2400.0
    assert geom.is_fallback is False


def test_parse_osrm_route_rejects_failures():
    with pytest.raises(RuntimeError):
        parse_osrm_route({"code": "NoRoute", "routes": []})


def test_fetch_route_requests_full_geojson_geometry():
    session = FakeSession(ROUTE_SAMPLE)
    geom = fetch_route(STOPS, base_url="http://osrm.local:5000/", session=session)
    url, params = session.calls[0]
    assert url.startswith("http://osrm.local:5000/route/v1/driving/3.3711,6.5095;")
    assert params["overview"] == "full" and params["geometries"] == "geojson"
    assert geom.distance_m == 25000.0


def test_fetch_route_with_fewer_than_two_stops_needs_no_request():
    session = FakeSession({})
    geom = fetch_route(STOPS[:1], session=session)
    assert session.calls == []
    assert geom.distance_m == 0 and len(geom.points) == 1


def test_straight_line_fallback_is_flagged_and_positive():
    geom = straight_line_route(STOPS)
    assert geom.is_fallback is True
    assert len(geom.points) == 3
    assert geom.distance_m > 0 and geom.duration_s > 0


def test_get_route_without_server_uses_straight_lines_quietly():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        geom = get_route(STOPS, base_url=None)
    assert geom.is_fallback is True


def test_get_route_falls_back_with_warning_when_server_fails(monkeypatch):
    def boom(*args, **kwargs):
        raise requests.ConnectionError("no network")

    monkeypatch.setattr("optimizer.route.fetch_route", boom)
    with pytest.warns(UserWarning, match="straight lines"):
        geom = get_route(STOPS, base_url="http://osrm.local:5000")
    assert geom.is_fallback is True
