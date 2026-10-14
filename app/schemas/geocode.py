from __future__ import annotations

from pydantic import BaseModel


class CandidateOut(BaseModel):
    name: str
    address: str | None
    lat: float
    lng: float
    provider: str
    provider_place_id: str | None


class GeocodeSearchOut(BaseModel):
    results: list[CandidateOut]
