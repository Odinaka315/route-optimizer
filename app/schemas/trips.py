from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

Preset = Literal["fastest", "shortest", "balanced"]


def _check_weights(weight_time: float | None, weight_dist: float | None) -> None:
    if (weight_time is None) != (weight_dist is None):
        raise ValueError("weight_time and weight_dist must be given together")
    if weight_time == 0 and weight_dist == 0:
        raise ValueError("weight_time and weight_dist cannot both be zero")


# --------------------------------------------------------------------------- input


class StopIn(BaseModel):
    """A stop is either coordinates (preferred: the app gets them from address search
    or a dropped pin) or just an address, which the server then geocodes."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, max_length=200)
    address: str | None = Field(default=None, max_length=500)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    geocode_source: str | None = Field(default=None, max_length=30)  # mapbox, nominatim, manual_pin, gps
    place_provider_id: str | None = Field(default=None, max_length=255)
    notes: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _needs_a_location(self) -> "StopIn":
        if (self.lat is None) != (self.lng is None):
            raise ValueError("lat and lng must be given together")
        if self.lat is None and not self.address:
            raise ValueError("each stop needs lat/lng or an address")
        return self


class OptimizeRequest(BaseModel):
    stops: list[StopIn] = Field(min_length=2)
    start_index: int = Field(default=0, ge=0, description="Which stop the rider starts from (0-based)")
    open_path: bool = Field(default=True, description="False = return to the starting stop at the end")
    preset: Preset | None = Field(default=None, description="Defaults to 'balanced' (70% time / 30% distance)")
    weight_time: float | None = Field(default=None, ge=0)
    weight_dist: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate(self) -> "OptimizeRequest":
        if self.start_index >= len(self.stops):
            raise ValueError("start_index must point at one of the stops")
        _check_weights(self.weight_time, self.weight_dist)
        return self


class TripCreate(OptimizeRequest):
    title: str | None = Field(default=None, min_length=1, max_length=200)


class TripModify(BaseModel):
    """Anything left out is copied from the original trip."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    stops: list[StopIn] | None = Field(default=None, min_length=2)
    start_index: int | None = Field(default=None, ge=0)
    open_path: bool | None = None
    preset: Preset | None = None
    weight_time: float | None = Field(default=None, ge=0)
    weight_dist: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate(self) -> "TripModify":
        if self.stops is not None and self.start_index is not None and self.start_index >= len(self.stops):
            raise ValueError("start_index must point at one of the stops")
        _check_weights(self.weight_time, self.weight_dist)
        return self


class TripPatch(BaseModel):
    """In-place changes that don't affect the route: rename and star/unstar."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    is_starred: bool | None = None

    @model_validator(mode="after")
    def _something_to_change(self) -> "TripPatch":
        if not self.model_fields_set:
            raise ValueError("provide title and/or is_starred")
        if self.title is None and self.is_starred is None:
            raise ValueError("provide title and/or is_starred")
        return self


# -------------------------------------------------------------------------- output


class StopOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    input_position: int
    name: str
    address: str | None = Field(default=None, validation_alias=AliasChoices("address_text", "address"))
    lat: float
    lng: float
    geocode_source: str | None
    place_provider_id: str | None
    is_start: bool
    notes: str | None


class RouteStopOut(BaseModel):
    trip_stop_id: uuid.UUID | None  # None for one-off /optimize calls
    visit_order: int  # 0 = first stop
    name: str
    address: str | None
    lat: float
    lng: float
    leg_distance_m: float | None  # from the previous stop (None for the first)
    leg_duration_s: float | None


class RouteOut(BaseModel):
    """An optimized route. Draw it with `polyline` (encoded, precision 6); `stops` is
    the visiting order. For closed loops the totals include the leg back to the start."""

    run_id: uuid.UUID | None
    finished_at: datetime | None
    solver: str
    matrix_backend: str
    total_distance_m: float
    total_duration_s: float
    percent_saved: float
    polyline: str | None
    polyline_precision: int = 6
    geometry_is_fallback: bool  # True = straight lines, not real roads
    stops: list[RouteStopOut]


class RunStatusOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: Literal["queued", "running", "succeeded", "failed"]
    error_message: str | None
    created_at: datetime
    finished_at: datetime | None


class RunSummaryOut(RunStatusOut):
    solver: str | None
    matrix_backend: str | None
    total_distance_m: float | None
    total_duration_s: float | None
    percent_saved: float | None
    geometry_is_fallback: bool


class RunDetailOut(RunStatusOut):
    route: RouteOut | None


class TripSummaryOut(BaseModel):
    id: uuid.UUID
    title: str
    is_starred: bool
    parent_trip_id: uuid.UUID | None
    open_path: bool
    stop_count: int
    created_at: datetime
    updated_at: datetime
    latest_run: RunStatusOut | None
    # From the most recent *successful* run, if any.
    total_distance_m: float | None
    total_duration_s: float | None


class TripListOut(BaseModel):
    items: list[TripSummaryOut]
    total: int
    limit: int
    offset: int


class TripDetailOut(BaseModel):
    id: uuid.UUID
    title: str
    is_starred: bool
    parent_trip_id: uuid.UUID | None
    open_path: bool
    weight_time: float
    weight_dist: float
    created_at: datetime
    updated_at: datetime
    stops: list[StopOut]  # in the order the user entered them
    route: RouteOut | None  # the latest successful result, in optimized order
    latest_run: RunStatusOut | None  # may be newer than `route` (queued/running/failed)
