"""Google/Mapbox "encoded polyline" format, used to store route geometry compactly.

A route through Lagos can have thousands of points; as an encoded string it is
roughly a tenth of the size of the equivalent JSON. Precision 6 matches OSRM's
``polyline6`` and Mapbox; most map SDKs can decode it directly.
"""

from __future__ import annotations

from collections.abc import Iterable


def _encode_value(value: int) -> str:
    value = ~(value << 1) if value < 0 else value << 1
    chunks: list[str] = []
    while value >= 0x20:
        chunks.append(chr((0x20 | (value & 0x1F)) + 63))
        value >>= 5
    chunks.append(chr(value + 63))
    return "".join(chunks)


def encode_polyline(points: Iterable[tuple[float, float]], precision: int = 6) -> str:
    """Encode (lat, lng) pairs."""
    factor = 10**precision
    out: list[str] = []
    prev_lat = prev_lng = 0
    for lat, lng in points:
        lat_i, lng_i = round(lat * factor), round(lng * factor)
        out.append(_encode_value(lat_i - prev_lat))
        out.append(_encode_value(lng_i - prev_lng))
        prev_lat, prev_lng = lat_i, lng_i
    return "".join(out)


def decode_polyline(encoded: str, precision: int = 6) -> list[tuple[float, float]]:
    """Inverse of encode_polyline. Returns (lat, lng) pairs."""
    factor = 10**precision
    coords: list[tuple[float, float]] = []
    index = lat = lng = 0
    while index < len(encoded):
        deltas = []
        for _ in range(2):
            shift = result = 0
            while True:
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat += deltas[0]
        lng += deltas[1]
        coords.append((lat / factor, lng / factor))
    return coords
