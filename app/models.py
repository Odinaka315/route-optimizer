"""SQLAlchemy models. See the schema section of the implementation plan for the why."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
    text,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Stable, predictable constraint names make later Alembic migrations (and
# dropping/renaming constraints) reliable across PostgreSQL and SQLite.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

RUN_STATUSES = ("queued", "running", "succeeded", "failed")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _created_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), nullable=False)  # stored lowercase
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)


class RefreshToken(Base):
    """A long-lived login credential, stored only as a SHA-256 hash.

    Every use rotates the token: the old row is revoked and points at its replacement
    (``replaced_by_id``). All tokens descended from one login share a ``family_id``, which
    is one "session": logging out revokes the family, and presenting an already-rotated
    token (a sign of theft) revokes it too."""

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    family_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("refresh_tokens.id", ondelete="SET NULL")
    )

    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
        Index("ix_refresh_tokens_user_id", "user_id"),
        Index("ix_refresh_tokens_family_id", "family_id"),
    )


class Trip(Base):
    """One plotted trip. Editing a trip never mutates it: it creates a copy whose
    parent_trip_id points back here (see the modify endpoint)."""

    __tablename__ = "trips"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    parent_trip_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trips.id", ondelete="SET NULL")
    )
    is_starred: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    # Settings the trip is solved with.
    open_path: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    weight_time: Mapped[float] = mapped_column(Float, default=0.7, server_default="0.7", nullable=False)
    weight_dist: Mapped[float] = mapped_column(Float, default=0.3, server_default="0.3", nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_trips_user_created", "user_id", "created_at"),
        Index(
            "ix_trips_user_starred",
            "user_id",
            "created_at",
            postgresql_where=text("is_starred AND deleted_at IS NULL"),
            sqlite_where=text("is_starred AND deleted_at IS NULL"),
        ),
        Index("ix_trips_parent_trip_id", "parent_trip_id"),
    )


class TripStop(Base):
    __tablename__ = "trip_stops"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    trip_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trips.id", ondelete="CASCADE"), nullable=False
    )
    input_position: Mapped[int] = mapped_column(Integer, nullable=False)  # order the user entered
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    address_text: Mapped[str | None] = mapped_column(String(500))
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    geocode_source: Mapped[str | None] = mapped_column(String(30))  # mapbox, nominatim, manual_pin, gps
    place_provider_id: Mapped[str | None] = mapped_column(String(255))
    is_start: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("trip_id", "input_position", name="uq_trip_stops_trip_position"),
        # At most one starting stop per trip.
        Index(
            "uq_trip_stops_one_start",
            "trip_id",
            unique=True,
            postgresql_where=text("is_start"),
            sqlite_where=text("is_start"),
        ),
    )


class OptimizationRun(Base):
    """One execution of the engine for a trip (also the async job record)."""

    __tablename__ = "optimization_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    trip_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trips.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    solver: Mapped[str | None] = mapped_column(String(20))
    matrix_backend: Mapped[str | None] = mapped_column(String(20))
    # For a closed loop these totals include the leg back to the start, which is
    # not stored as a row in run_stops.
    total_distance_m: Mapped[float | None] = mapped_column(Float)
    total_duration_s: Mapped[float | None] = mapped_column(Float)
    percent_saved: Mapped[float | None] = mapped_column(Float)
    route_polyline: Mapped[str | None] = mapped_column(Text)  # encoded polyline, precision 6
    geometry_is_fallback: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')", name="status_valid"
        ),
        Index("ix_optimization_runs_trip_created", "trip_id", "created_at"),
    )


class RunStop(Base):
    """A stop's place in a run's optimized visiting order."""

    __tablename__ = "run_stops"

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("optimization_runs.id", ondelete="CASCADE"), nullable=False
    )
    trip_stop_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trip_stops.id", ondelete="CASCADE"), nullable=False
    )
    visit_order: Mapped[int] = mapped_column(Integer, nullable=False)  # 0 = first stop
    leg_distance_m: Mapped[float | None] = mapped_column(Float)  # from the previous stop
    leg_duration_s: Mapped[float | None] = mapped_column(Float)

    __table_args__ = (
        PrimaryKeyConstraint("run_id", "visit_order", name="pk_run_stops"),
        UniqueConstraint("run_id", "trip_stop_id", name="uq_run_stops_run_stop"),
        Index("ix_run_stops_trip_stop_id", "trip_stop_id"),
    )


class GeocodeCache(Base):
    __tablename__ = "geocode_cache"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    country: Mapped[str] = mapped_column(String(10), default="", server_default="", nullable=False)
    query_normalized: Mapped[str] = mapped_column(String(500), nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    formatted_address: Mapped[str | None] = mapped_column(String(500))
    provider_place_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint(
            "provider", "country", "query_normalized", name="uq_geocode_cache_lookup"
        ),
    )
