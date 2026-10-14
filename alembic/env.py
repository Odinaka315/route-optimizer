"""Alembic environment (async). Run from the project root: ``alembic upgrade head``."""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig

from alembic import context

from app.db import build_engine, is_pooled_url, normalize_database_url
from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    # Tests (and scripts) can hand a URL in directly via config.attributes.
    explicit = config.attributes.get("database_url")
    if explicit:
        return explicit
    from app.config import get_settings

    settings = get_settings()
    return settings.alembic_database_url or settings.database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of connecting (``alembic upgrade head --sql``)."""
    context.configure(
        url=normalize_database_url(_database_url()),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    url = _database_url()
    if is_pooled_url(url):
        print(
            "warning: running migrations through a Neon pooled (-pooler) connection. "
            "Neon recommends the direct connection string for migrations; set "
            "ALEMBIC_DATABASE_URL to it.",
            file=sys.stderr,
        )
    engine = build_engine(url, null_pool=True)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
