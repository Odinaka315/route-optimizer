"""Turn user input (addresses or coordinates) into a list of Stop objects."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim's usage policy requires an identifying User-Agent. Replace the
# contact placeholder with your own before using it for anything real.
USER_AGENT = "route-optimizer-prototype/0.1 (contact: your-email@example.com)"
MIN_SECONDS_BETWEEN_REQUESTS = 1.1  # policy: max 1 request per second


@dataclass(frozen=True)
class Stop:
    name: str
    lat: float
    lng: float


class GeocodingError(Exception):
    """Raised when one or more addresses cannot be turned into coordinates."""


def _validate_coords(name: str, lat: float, lng: float) -> None:
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
        raise ValueError(f"Stop '{name}' has out-of-range coordinates: lat={lat}, lng={lng}")


def _load_cache(cache_path: Path | None) -> dict[str, list[float]]:
    if cache_path is None or not cache_path.exists():
        return {}
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache_path: Path | None, cache: dict[str, list[float]]) -> None:
    if cache_path is None:
        return
    cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def geocode_address(
    address: str,
    session: requests.Session | None = None,
    country_codes: str | None = "ng",
    timeout: float = 10.0,
) -> tuple[float, float] | None:
    """Look up one address on Nominatim. Returns (lat, lng) or None if not found."""
    http = session or requests
    params = {"q": address, "format": "json", "limit": 1}
    if country_codes:
        params["countrycodes"] = country_codes
    resp = http.get(
        NOMINATIM_URL,
        params=params,
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
    )
    resp.raise_for_status()
    results = resp.json()
    if not results:
        return None
    return float(results[0]["lat"]), float(results[0]["lon"])


def load_stops(
    entries: list[dict],
    cache_path: str | Path | None = ".geocode_cache.json",
    country_codes: str | None = "ng",
) -> list[Stop]:
    """Convert raw entries into Stops.

    Each entry is either {"name", "lat", "lng"} (passed straight through) or
    {"name", "address"} (geocoded with Nominatim, cached locally). Raises
    GeocodingError listing every address that could not be resolved.
    """
    cache_file = Path(cache_path) if cache_path else None
    cache = _load_cache(cache_file)
    stops: list[Stop] = []
    failed: list[str] = []
    last_request = 0.0
    cache_dirty = False

    for idx, entry in enumerate(entries):
        name = entry.get("name") or f"Stop {idx + 1}"

        if "lat" in entry and "lng" in entry:
            lat, lng = float(entry["lat"]), float(entry["lng"])
            _validate_coords(name, lat, lng)
            stops.append(Stop(name, lat, lng))
            continue

        address = entry.get("address")
        if not address or not str(address).strip():
            raise ValueError(f"Entry {idx + 1} ('{name}') needs either lat/lng or an address.")

        key = f"{country_codes or ''}|{str(address).strip().lower()}"
        if key in cache:
            lat, lng = cache[key]
            stops.append(Stop(name, lat, lng))
            continue

        wait = MIN_SECONDS_BETWEEN_REQUESTS - (time.monotonic() - last_request)
        if wait > 0 and last_request > 0:
            time.sleep(wait)
        try:
            found = geocode_address(str(address), country_codes=country_codes)
        except requests.RequestException as exc:
            failed.append(f"{address} ({exc.__class__.__name__})")
            last_request = time.monotonic()
            continue
        last_request = time.monotonic()

        if found is None:
            failed.append(str(address))
            continue
        cache[key] = [found[0], found[1]]
        cache_dirty = True
        stops.append(Stop(name, found[0], found[1]))

    if cache_dirty:
        _save_cache(cache_file, cache)

    if failed:
        raise GeocodingError(
            "Could not geocode these addresses: "
            + "; ".join(failed)
            + ". Check spelling, add the area/city, or supply lat/lng directly."
        )
    return stops


def load_stops_from_file(path: str | Path, **kwargs) -> list[Stop]:
    """Read a JSON list of entries from disk and resolve it into Stops."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Stops file must contain a JSON list of entries.")
    return load_stops(data, **kwargs)
