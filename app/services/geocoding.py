"""Address search / geocoding behind one interface (Nominatim or Mapbox).

Both providers are blocking (``requests``), so callers run ``search`` in a worker
thread. Error messages never include the request URL, because Mapbox puts the
access token in the query string.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Protocol

import requests
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..models import GeocodeCache


class GeocoderError(Exception):
    """The provider could not be reached or rejected the request."""


@dataclass(frozen=True)
class GeocodeCandidate:
    name: str
    address: str | None
    lat: float
    lng: float
    provider: str
    provider_place_id: str | None


class Geocoder(Protocol):
    provider: str
    # May results be written to our geocode_cache table? (Mapbox only allows this
    # for "permanent" requests.)
    storable: bool

    def search(
        self,
        query: str,
        limit: int = 5,
        proximity: tuple[float, float] | None = None,  # (lat, lng)
        autocomplete: bool = False,
    ) -> list[GeocodeCandidate]: ...


class NominatimGeocoder:
    """OpenStreetMap's public geocoder. Fine for development; its usage policy
    forbids heavy use and autocomplete, so use Mapbox (or self-host) in production."""

    provider = "nominatim"
    storable = True
    URL = "https://nominatim.openstreetmap.org/search"

    def __init__(
        self,
        user_agent: str,
        country: str | None = "ng",
        session: requests.Session | None = None,
        min_interval_s: float = 1.1,  # policy: at most one request per second
        timeout: float = 10.0,
    ):
        self.user_agent = user_agent
        self.country = country
        self.session = session or requests.Session()
        self.min_interval_s = min_interval_s
        self.timeout = timeout
        self._lock = threading.Lock()
        self._last_request = 0.0

    def _wait_turn(self) -> None:
        with self._lock:
            wait = self.min_interval_s - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def search(self, query, limit=5, proximity=None, autocomplete=False):
        params = {"q": query, "format": "jsonv2", "limit": limit}
        if self.country:
            params["countrycodes"] = self.country
        self._wait_turn()
        try:
            resp = self.session.get(
                self.URL, params=params, headers={"User-Agent": self.user_agent}, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise GeocoderError(f"Nominatim request failed ({exc.__class__.__name__})") from None
        if resp.status_code != 200:
            raise GeocoderError(f"Nominatim returned HTTP {resp.status_code}")

        results = []
        for item in resp.json():
            display = item.get("display_name")
            osm_id = item.get("osm_id")
            place_id = f"{item.get('osm_type', '')}/{osm_id}" if osm_id else str(item.get("place_id", ""))
            results.append(
                GeocodeCandidate(
                    name=item.get("name") or (display.split(",")[0].strip() if display else query),
                    address=display,
                    lat=float(item["lat"]),
                    lng=float(item["lon"]),
                    provider=self.provider,
                    provider_place_id=place_id or None,
                )
            )
        return results


class MapboxGeocoder:
    """Mapbox Geocoding API v6 (forward)."""

    provider = "mapbox"
    URL = "https://api.mapbox.com/search/geocode/v6/forward"

    def __init__(
        self,
        access_token: str,
        country: str | None = "ng",
        permanent: bool = True,
        session: requests.Session | None = None,
        timeout: float = 10.0,
    ):
        self._token = access_token
        self.country = country
        self.permanent = permanent
        self.storable = permanent
        self.session = session or requests.Session()
        self.timeout = timeout

    def search(self, query, limit=5, proximity=None, autocomplete=False):
        # The API accepts at most 256 characters / 20 words and 1-10 results.
        text = " ".join(query.split()[:20])[:256]
        params = {
            "q": text,
            "access_token": self._token,
            "limit": max(1, min(limit, 10)),
            "autocomplete": "true" if autocomplete else "false",
            "permanent": "true" if self.permanent else "false",
        }
        if self.country:
            params["country"] = self.country
        if proximity is not None:
            params["proximity"] = f"{proximity[1]},{proximity[0]}"  # Mapbox wants lng,lat

        try:
            resp = self.session.get(self.URL, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise GeocoderError(f"Mapbox request failed ({exc.__class__.__name__})") from None
        if resp.status_code in (401, 403):
            raise GeocoderError("Mapbox rejected the access token (check MAPBOX_ACCESS_TOKEN)")
        if resp.status_code == 429:
            raise GeocoderError("Mapbox rate limit exceeded")
        if resp.status_code != 200:
            raise GeocoderError(f"Mapbox returned HTTP {resp.status_code}")

        results = []
        for feature in resp.json().get("features", []):
            props = feature.get("properties", {})
            lng, lat = feature["geometry"]["coordinates"][:2]  # GeoJSON order: [lng, lat]
            results.append(
                GeocodeCandidate(
                    name=props.get("name_preferred") or props.get("name") or text,
                    address=props.get("full_address") or props.get("place_formatted"),
                    lat=float(lat),
                    lng=float(lng),
                    provider=self.provider,
                    provider_place_id=props.get("mapbox_id") or feature.get("id"),
                )
            )
        return results


def build_geocoder(settings: Settings) -> Geocoder:
    country = settings.geocoder_country or None
    if settings.geocoder_provider == "mapbox":
        return MapboxGeocoder(
            settings.mapbox_access_token or "", country=country, permanent=settings.mapbox_permanent
        )
    return NominatimGeocoder(settings.nominatim_user_agent, country=country)


def normalize_query(address: str) -> str:
    return " ".join(address.lower().split())


async def geocode_best(
    session: AsyncSession, geocoder: Geocoder, country: str | None, address: str
) -> GeocodeCandidate | None:
    """Best single match for an address, using the geocode_cache table when allowed.

    Returns None if the provider finds nothing; raises GeocoderError if it is down.
    """
    key = normalize_query(address)
    country_key = country or ""
    cached = await session.scalar(
        select(GeocodeCache).where(
            GeocodeCache.provider == geocoder.provider,
            GeocodeCache.country == country_key,
            GeocodeCache.query_normalized == key,
        )
    )
    if cached is not None:
        formatted = cached.formatted_address
        return GeocodeCandidate(
            name=formatted.split(",")[0].strip() if formatted else address,
            address=formatted,
            lat=cached.lat,
            lng=cached.lng,
            provider=geocoder.provider,
            provider_place_id=cached.provider_place_id,
        )

    candidates = await run_in_threadpool(geocoder.search, address, 1, None, False)
    if not candidates:
        return None
    best = candidates[0]

    if geocoder.storable:
        try:
            async with session.begin_nested():
                session.add(
                    GeocodeCache(
                        provider=geocoder.provider,
                        country=country_key,
                        query_normalized=key,
                        lat=best.lat,
                        lng=best.lng,
                        formatted_address=best.address,
                        provider_place_id=best.provider_place_id,
                    )
                )
        except IntegrityError:
            pass  # another request cached the same address first; that's fine
    return best
