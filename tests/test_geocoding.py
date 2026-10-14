"""Geocoding providers (with fake HTTP) and the address cache."""

import pytest
import requests
from sqlalchemy import func, select

from app.config import Settings
from app.models import GeocodeCache
from app.services.geocoding import (
    GeocoderError,
    MapboxGeocoder,
    NominatimGeocoder,
    build_geocoder,
    geocode_best,
    normalize_query,
)

from .conftest import FakeGeocoder, SECRET

TOKEN = "pk.super-secret-token"


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        if self.error:
            raise self.error
        return self.response


# Shaped like the documented Mapbox Geocoding v6 forward response.
MAPBOX_SAMPLE = {
    "type": "FeatureCollection",
    "features": [
        {
            "id": "dXJuOm1ieGFkcjoxMjM",
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [3.3515, 6.6018]},  # [lng, lat]
            "properties": {
                "mapbox_id": "dXJuOm1ieGFkcjoxMjM",
                "feature_type": "address",
                "name": "10 Allen Avenue",
                "name_preferred": "10 Allen Avenue",
                "place_formatted": "Ikeja, Lagos, Nigeria",
                "full_address": "10 Allen Avenue, Ikeja, Lagos, Nigeria",
                "coordinates": {"longitude": 3.3515, "latitude": 6.6018, "accuracy": "rooftop"},
            },
        }
    ],
    "attribution": "...",
}

NOMINATIM_SAMPLE = [
    {
        "place_id": 99,
        "osm_type": "node",
        "osm_id": 12345,
        "lat": "6.6018",
        "lon": "3.3515",
        "name": "Allen Avenue",
        "display_name": "Allen Avenue, Ikeja, Lagos, Nigeria",
    }
]


# --- Mapbox ------------------------------------------------------------------------


def test_mapbox_request_and_parsing():
    session = FakeSession(FakeResponse(200, MAPBOX_SAMPLE))
    geocoder = MapboxGeocoder(TOKEN, country="ng", permanent=True, session=session)
    (hit,) = geocoder.search("10 Allen Avenue", limit=3, proximity=(6.5, 3.4), autocomplete=True)

    params = session.calls[0]["params"]
    assert session.calls[0]["url"] == "https://api.mapbox.com/search/geocode/v6/forward"
    assert params["q"] == "10 Allen Avenue" and params["country"] == "ng" and params["limit"] == 3
    assert params["permanent"] == "true" and params["autocomplete"] == "true"
    assert params["proximity"] == "3.4,6.5"  # Mapbox wants longitude first
    assert params["access_token"] == TOKEN

    assert (hit.lat, hit.lng) == (6.6018, 3.3515)  # GeoJSON [lng, lat] flipped correctly
    assert hit.name == "10 Allen Avenue" and hit.address == "10 Allen Avenue, Ikeja, Lagos, Nigeria"
    assert hit.provider == "mapbox" and hit.provider_place_id == "dXJuOm1ieGFkcjoxMjM"


def test_mapbox_limits_and_storage_flags():
    session = FakeSession(FakeResponse(200, {"features": []}))
    geocoder = MapboxGeocoder(TOKEN, permanent=False, session=session)
    assert geocoder.storable is False and MapboxGeocoder(TOKEN, permanent=True).storable is True
    geocoder.search(" ".join(["word"] * 40), limit=99)
    params = session.calls[0]["params"]
    assert params["limit"] == 10 and len(params["q"].split()) == 20  # API maximums
    assert params["permanent"] == "false" and params["autocomplete"] == "false"
    assert "proximity" not in params


@pytest.mark.parametrize(
    "status, expected",
    [(401, "rejected the access token"), (403, "rejected the access token"), (429, "rate limit"), (500, "HTTP 500")],
)
def test_mapbox_errors_never_contain_the_token(status, expected):
    geocoder = MapboxGeocoder(TOKEN, session=FakeSession(FakeResponse(status)))
    with pytest.raises(GeocoderError, match=expected) as err:
        geocoder.search("anywhere")
    assert TOKEN not in str(err.value)


def test_mapbox_network_failure_does_not_leak_the_url():
    failing = requests.ConnectionError(f"HTTPSConnectionPool: https://api.mapbox.com/...&access_token={TOKEN}")
    geocoder = MapboxGeocoder(TOKEN, session=FakeSession(error=failing))
    with pytest.raises(GeocoderError) as err:
        geocoder.search("anywhere")
    assert TOKEN not in str(err.value) and "ConnectionError" in str(err.value)


# --- Nominatim -------------------------------------------------------------------------


def test_nominatim_request_and_parsing():
    session = FakeSession(FakeResponse(200, NOMINATIM_SAMPLE))
    geocoder = NominatimGeocoder("my-app/1.0 (me@example.com)", country="ng", session=session, min_interval_s=0)
    (hit,) = geocoder.search("Allen Avenue", limit=2)
    call = session.calls[0]
    assert call["headers"]["User-Agent"] == "my-app/1.0 (me@example.com)"
    assert call["params"]["countrycodes"] == "ng" and call["params"]["limit"] == 2
    assert (hit.lat, hit.lng) == (6.6018, 3.3515)
    assert hit.name == "Allen Avenue" and hit.provider_place_id == "node/12345" and hit.provider == "nominatim"


def test_nominatim_name_falls_back_to_the_first_part_of_the_address():
    item = {**NOMINATIM_SAMPLE[0], "name": ""}
    geocoder = NominatimGeocoder("ua", session=FakeSession(FakeResponse(200, [item])), min_interval_s=0)
    assert geocoder.search("x")[0].name == "Allen Avenue"


def test_nominatim_errors():
    assert NominatimGeocoder("ua", session=FakeSession(FakeResponse(200, [])), min_interval_s=0).search("x") == []
    with pytest.raises(GeocoderError, match="HTTP 429"):
        NominatimGeocoder("ua", session=FakeSession(FakeResponse(429)), min_interval_s=0).search("x")
    with pytest.raises(GeocoderError, match="Timeout"):
        NominatimGeocoder("ua", session=FakeSession(error=requests.Timeout()), min_interval_s=0).search("x")


def test_nominatim_waits_between_requests(monkeypatch):
    import app.services.geocoding as module

    sleeps = []
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    geocoder = NominatimGeocoder("ua", session=FakeSession(FakeResponse(200, [])), min_interval_s=5.0)
    geocoder.search("a")
    geocoder.search("b")
    assert len(sleeps) >= 1 and 0 < sleeps[-1] <= 5.0


def test_build_geocoder_follows_settings():
    base = dict(database_url="sqlite:///x.db", secret_key=SECRET)
    nominatim = build_geocoder(Settings(_env_file=None, **base))
    assert isinstance(nominatim, NominatimGeocoder) and nominatim.storable
    mapbox = build_geocoder(
        Settings(_env_file=None, geocoder_provider="mapbox", mapbox_access_token="pk.x", mapbox_permanent=False, **base)
    )
    assert isinstance(mapbox, MapboxGeocoder) and mapbox.storable is False


# --- the cache -------------------------------------------------------------------------------


def test_query_normalization():
    assert normalize_query("  10 Allen   AVENUE,\tIkeja ") == "10 allen avenue, ikeja"


async def test_geocode_best_caches_and_reuses(sessionmaker):
    geocoder = FakeGeocoder()
    async with sessionmaker() as session:
        first = await geocode_best(session, geocoder, "ng", "10 Allen Avenue, Ikeja")
        await session.commit()
    async with sessionmaker() as session:
        again = await geocode_best(session, geocoder, "ng", "  10 allen avenue,   IKEJA")
        assert await session.scalar(select(func.count(GeocodeCache.id))) == 1
    assert len(geocoder.calls) == 1
    assert (again.lat, again.lng) == (first.lat, first.lng) == (6.6018, 3.3515)
    assert again.provider_place_id == first.provider_place_id


async def test_cache_is_keyed_by_provider_and_country(sessionmaker):
    geocoder = FakeGeocoder()
    async with sessionmaker() as session:
        await geocode_best(session, geocoder, "ng", "10 Allen Avenue, Ikeja")
        await session.commit()
        await geocode_best(session, geocoder, "gh", "10 Allen Avenue, Ikeja")  # different country: new lookup
        await session.commit()
    assert len(geocoder.calls) == 2


async def test_geocode_best_returns_none_when_nothing_found_and_does_not_cache_it(sessionmaker):
    geocoder = FakeGeocoder()
    async with sessionmaker() as session:
        assert await geocode_best(session, geocoder, "ng", "Nowhere Street") is None
        await session.commit()
        assert await session.scalar(select(func.count(GeocodeCache.id))) == 0


async def test_geocode_best_propagates_provider_outages(sessionmaker):
    geocoder = FakeGeocoder()
    geocoder.fail = True
    async with sessionmaker() as session:
        with pytest.raises(GeocoderError):
            await geocode_best(session, geocoder, "ng", "10 Allen Avenue, Ikeja")
