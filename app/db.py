"""Async database engine and session factory (Neon / PostgreSQL, SQLite for tests)."""

from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool


def normalize_database_url(url: str) -> str:
    """Turn a connection string as Neon shows it into one SQLAlchemy+asyncpg accepts.

    Neon gives ``postgresql://user:pass@host/db?sslmode=require&channel_binding=require``.
    asyncpg uses ``ssl=`` instead of ``sslmode=`` and does not understand
    ``channel_binding``, so both are translated/dropped. SQLite URLs get the async
    driver. URLs that already name an async driver are left alone.
    """
    parsed = make_url(url)
    backend = parsed.get_backend_name()

    if backend == "sqlite":
        if parsed.drivername == "sqlite":
            parsed = parsed.set(drivername="sqlite+aiosqlite")
        return parsed.render_as_string(hide_password=False)

    if backend in ("postgres", "postgresql"):
        if parsed.drivername in ("postgres", "postgresql", "postgresql+psycopg2"):
            parsed = parsed.set(drivername="postgresql+asyncpg")
        query = dict(parsed.query)
        sslmode = query.pop("sslmode", None)
        query.pop("channel_binding", None)
        if sslmode and "ssl" not in query:
            query["ssl"] = sslmode
        parsed = parsed.set(query=query)
        return parsed.render_as_string(hide_password=False)

    raise ValueError(f"Unsupported database type: {backend!r} (use PostgreSQL/Neon)")


def is_pooled_url(url: str) -> bool:
    """True for Neon pooler endpoints (host contains ``-pooler``)."""
    host = make_url(url).host or ""
    return "-pooler" in host


def build_engine(
    url: str,
    *,
    pool_size: int = 5,
    max_overflow: int = 5,
    disable_statement_cache: bool = False,
    null_pool: bool = False,
    echo: bool = False,
) -> AsyncEngine:
    normalized = normalize_database_url(url)
    parsed = make_url(normalized)
    kwargs: dict = {"echo": echo}

    if parsed.get_backend_name() == "sqlite":
        kwargs["connect_args"] = {"check_same_thread": False}
        if parsed.database in (None, "", ":memory:"):
            kwargs["poolclass"] = StaticPool  # one shared in-memory database
    else:
        if null_pool:
            kwargs["poolclass"] = NullPool  # migrations: one short-lived connection
        else:
            kwargs.update(
                pool_size=pool_size,
                max_overflow=max_overflow,
                # Neon suspends idle compute and pooled connections can go stale:
                # test a connection before handing it out, and recycle regularly.
                pool_pre_ping=True,
                pool_recycle=300,
            )
        if disable_statement_cache:
            kwargs["connect_args"] = {"statement_cache_size": 0}

    engine = create_async_engine(normalized, **kwargs)

    if parsed.get_backend_name() == "sqlite":
        # SQLite ignores foreign keys unless asked; tests rely on ON DELETE rules.
        @event.listens_for(engine.sync_engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _record):  # pragma: no cover
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
