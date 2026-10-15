"""The Alembic migration must build exactly the schema the models describe.

Always runs against a temporary SQLite file. To also check PostgreSQL, set
TEST_MIGRATION_DATABASE_URL to an EMPTY scratch database (never your real one).
"""

import asyncio
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.db import build_engine
from app.models import Base

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_TABLES = set(Base.metadata.tables)


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.attributes["database_url"] = url
    return cfg


def read_schema(url: str) -> dict:
    """Inspect the database through the same async driver the app uses."""

    def collect(connection):
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        schema = {"tables": tables, "trips_indexes": set(), "trip_stops_indexes": {}, "refresh_indexes": set()}
        schema["run_columns"], schema["run_indexes"] = set(), set()
        if "optimization_runs" in tables:
            schema["run_columns"] = {c["name"] for c in inspector.get_columns("optimization_runs")}
            schema["run_indexes"] = {i["name"] for i in inspector.get_indexes("optimization_runs")}
        if "refresh_tokens" in tables:
            schema["refresh_indexes"] = {i["name"] for i in inspector.get_indexes("refresh_tokens")}
        if "trips" in tables:
            schema["trips_indexes"] = {i["name"] for i in inspector.get_indexes("trips")}
        if "trip_stops" in tables:
            schema["trip_stops_indexes"] = {i["name"]: i["unique"] for i in inspector.get_indexes("trip_stops")}
        return schema

    async def run():
        engine = build_engine(url, null_pool=True)
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(collect)
        finally:
            await engine.dispose()

    return asyncio.run(run())


urls = [pytest.param("sqlite", id="sqlite")]
if os.environ.get("TEST_MIGRATION_DATABASE_URL"):
    urls.append(pytest.param("env", id="postgres"))


@pytest.fixture
def database_url(request, tmp_path):
    if request.param == "env":
        return os.environ["TEST_MIGRATION_DATABASE_URL"]
    return f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}"


@pytest.mark.parametrize("database_url", urls, indirect=True)
def test_upgrade_matches_models_and_downgrades_cleanly(database_url):
    cfg = alembic_config(database_url)
    command.upgrade(cfg, "head")
    try:
        command.check(cfg)  # raises if the models and the migration disagree

        schema = read_schema(database_url)
        assert EXPECTED_TABLES <= schema["tables"]
        assert {"ix_trips_user_created", "ix_trips_user_starred", "ix_trips_parent_trip_id"} <= schema["trips_indexes"]
        assert bool(schema["trip_stops_indexes"].get("uq_trip_stops_one_start"))  # present and unique
        assert {"ix_refresh_tokens_user_id", "ix_refresh_tokens_family_id"} <= schema["refresh_indexes"]
        assert {"attempts", "run_after", "started_at", "heartbeat_at", "locked_by"} <= schema["run_columns"]
        assert "ix_optimization_runs_status_created" in schema["run_indexes"]
    finally:
        command.downgrade(cfg, "base")

    assert read_schema(database_url)["tables"] - {"alembic_version"} == set()


def test_there_is_a_single_migration_head():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory(str(ROOT / "alembic"))
    assert len(script.get_heads()) == 1
