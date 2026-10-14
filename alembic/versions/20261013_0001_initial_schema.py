"""initial schema: users, trips, trip_stops, optimization_runs, run_stops, geocode_cache

Revision ID: 0001
Revises:
Create Date: 2026-10-13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _now() -> sa.sql.elements.TextClause:
    return sa.func.now()


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    op.create_table(
        "trips",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("parent_trip_id", sa.Uuid(), nullable=True),
        sa.Column("is_starred", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("open_path", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("weight_time", sa.Float(), server_default="0.7", nullable=False),
        sa.Column("weight_dist", sa.Float(), server_default="0.3", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_trips_user_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["parent_trip_id"],
            ["trips.id"],
            name=op.f("fk_trips_parent_trip_id_trips"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trips")),
    )
    op.create_index("ix_trips_user_created", "trips", ["user_id", "created_at"])
    op.create_index(
        "ix_trips_user_starred",
        "trips",
        ["user_id", "created_at"],
        postgresql_where=sa.text("is_starred AND deleted_at IS NULL"),
        sqlite_where=sa.text("is_starred AND deleted_at IS NULL"),
    )
    op.create_index("ix_trips_parent_trip_id", "trips", ["parent_trip_id"])

    op.create_table(
        "trip_stops",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("trip_id", sa.Uuid(), nullable=False),
        sa.Column("input_position", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("address_text", sa.String(length=500), nullable=True),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lng", sa.Float(), nullable=False),
        sa.Column("geocode_source", sa.String(length=30), nullable=True),
        sa.Column("place_provider_id", sa.String(length=255), nullable=True),
        sa.Column("is_start", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("notes", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["trip_id"], ["trips.id"], name=op.f("fk_trip_stops_trip_id_trips"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trip_stops")),
        sa.UniqueConstraint("trip_id", "input_position", name="uq_trip_stops_trip_position"),
    )
    op.create_index(
        "uq_trip_stops_one_start",
        "trip_stops",
        ["trip_id"],
        unique=True,
        postgresql_where=sa.text("is_start"),
        sqlite_where=sa.text("is_start"),
    )

    op.create_table(
        "optimization_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("trip_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("solver", sa.String(length=20), nullable=True),
        sa.Column("matrix_backend", sa.String(length=20), nullable=True),
        sa.Column("total_distance_m", sa.Float(), nullable=True),
        sa.Column("total_duration_s", sa.Float(), nullable=True),
        sa.Column("percent_saved", sa.Float(), nullable=True),
        sa.Column("route_polyline", sa.Text(), nullable=True),
        sa.Column("geometry_is_fallback", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name=op.f("ck_optimization_runs_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["trip_id"],
            ["trips.id"],
            name=op.f("fk_optimization_runs_trip_id_trips"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_optimization_runs")),
    )
    op.create_index(
        "ix_optimization_runs_trip_created", "optimization_runs", ["trip_id", "created_at"]
    )

    op.create_table(
        "run_stops",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("trip_stop_id", sa.Uuid(), nullable=False),
        sa.Column("visit_order", sa.Integer(), nullable=False),
        sa.Column("leg_distance_m", sa.Float(), nullable=True),
        sa.Column("leg_duration_s", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["optimization_runs.id"],
            name=op.f("fk_run_stops_run_id_optimization_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["trip_stop_id"],
            ["trip_stops.id"],
            name=op.f("fk_run_stops_trip_stop_id_trip_stops"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "visit_order", name="pk_run_stops"),
        sa.UniqueConstraint("run_id", "trip_stop_id", name="uq_run_stops_run_stop"),
    )
    op.create_index("ix_run_stops_trip_stop_id", "run_stops", ["trip_stop_id"])

    op.create_table(
        "geocode_cache",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("country", sa.String(length=10), server_default="", nullable=False),
        sa.Column("query_normalized", sa.String(length=500), nullable=False),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lng", sa.Float(), nullable=False),
        sa.Column("formatted_address", sa.String(length=500), nullable=True),
        sa.Column("provider_place_id", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_geocode_cache")),
        sa.UniqueConstraint(
            "provider", "country", "query_normalized", name="uq_geocode_cache_lookup"
        ),
    )


def downgrade() -> None:
    op.drop_table("geocode_cache")
    op.drop_index("ix_run_stops_trip_stop_id", table_name="run_stops")
    op.drop_table("run_stops")
    op.drop_index("ix_optimization_runs_trip_created", table_name="optimization_runs")
    op.drop_table("optimization_runs")
    op.drop_index("uq_trip_stops_one_start", table_name="trip_stops")
    op.drop_table("trip_stops")
    op.drop_index("ix_trips_parent_trip_id", table_name="trips")
    op.drop_index("ix_trips_user_starred", table_name="trips")
    op.drop_index("ix_trips_user_created", table_name="trips")
    op.drop_table("trips")
    op.drop_table("users")
