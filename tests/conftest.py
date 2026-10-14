"""Shared fixtures for the API tests.

By default the tests use a throwaway SQLite file. To run them against a real
PostgreSQL instead (recommended before deploying), point TEST_DATABASE_URL at an
EMPTY scratch database, never at your real Neon database: the tests drop and
recreate every table.

    TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/routeopt_test pytest
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio

from app.config import Settings
from app.db import build_engine
from app.emailer import MemoryEmailSender
from app.main import create_app
from app.models import Base
from app.services.geocoding import GeocodeCandidate, GeocoderError

SECRET = "test-secret-key-test-secret-key-0123456789"
PASSWORD = "correct-horse-battery"

# Six real Lagos locations: (name, lat, lng)
LAGOS = [
    ("Depot - Yaba", 6.5095, 3.3711),
    ("Surulere", 6.4969, 3.3568),
    ("Ikeja City Mall", 6.6140, 3.3580),
    ("Victoria Island", 6.4281, 3.4219),
    ("Lekki Phase 1", 6.4478, 3.4723),
    ("Maryland Mall", 6.5681, 3.3649),
]


def stop(i: int) -> dict:
    name, lat, lng = LAGOS[i]
    return {"name": name, "lat": lat, "lng": lng}


def stops(count: int = 6) -> list[dict]:
    return [stop(i) for i in range(count)]


class FakeGeocoder:
    """Stands in for Mapbox/Nominatim: no network, predictable answers."""

    provider = "fake"

    def __init__(self) -> None:
        self.storable = True
        self.fail = False
        self.calls: list[dict] = []
        self.table = {
            "10 allen avenue, ikeja": (6.6018, 3.3515),
            "adeola odeku street, victoria island": (6.4281, 3.4219),
        }

    def search(self, query, limit=5, proximity=None, autocomplete=False):
        self.calls.append(
            {"query": query, "limit": limit, "proximity": proximity, "autocomplete": autocomplete}
        )
        if self.fail:
            raise GeocoderError("provider down")
        wanted = " ".join(query.lower().split())
        return [
            GeocodeCandidate(
                name=key.split(",")[0].title(),
                address=key.title(),
                lat=lat,
                lng=lng,
                provider=self.provider,
                provider_place_id=f"fake:{key}",
            )
            for key, (lat, lng) in self.table.items()
            if wanted in key or key in wanted
        ][:limit]


@dataclass
class Env:
    app: object
    client: httpx.AsyncClient
    emails: MemoryEmailSender
    geocoder: FakeGeocoder

    def last_token(self) -> str:
        match = re.search(r"token=([A-Za-z0-9_\-.]+)", self.emails.outbox[-1].body)
        assert match, "no token link in the last email"
        return match.group(1)

    async def register(self, email: str = "rider@example.com", password: str = PASSWORD) -> dict:
        """Sign up, confirm the email, log in, and return Authorization headers."""
        r = await self.client.post("/auth/signup", json={"email": email, "password": password})
        assert r.status_code == 201, r.text
        if self.app.state.settings.require_email_verification:
            r = await self.client.post("/auth/verify-email", json={"token": self.last_token()})
            assert r.status_code == 200, r.text
        r = await self.client.post("/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _database_url(tmp_path) -> str:
    return os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def make_settings(tmp_path):
    def _make(**overrides) -> Settings:
        values = dict(
            database_url=_database_url(tmp_path),
            secret_key=SECRET,
            environment="test",
            matrix_backend="haversine",
            solver_time_limit_s=1,
            max_stops=12,
        )
        values.update(overrides)
        return Settings(_env_file=None, **values)  # ignore any developer .env

    return _make


@pytest_asyncio.fixture
async def engine(tmp_path):
    engine = build_engine(_database_url(tmp_path))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessionmaker(engine):
    from app.db import build_sessionmaker

    return build_sessionmaker(engine)


@pytest_asyncio.fixture
async def app_factory(engine, make_settings):
    clients: list[httpx.AsyncClient] = []

    async def _make(**overrides) -> Env:
        emails, geocoder = MemoryEmailSender(), FakeGeocoder()
        app = create_app(make_settings(**overrides), engine=engine, email_sender=emails, geocoder=geocoder)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        clients.append(client)
        return Env(app, client, emails, geocoder)

    yield _make
    for client in clients:
        await client.aclose()


@pytest_asyncio.fixture
async def env(app_factory) -> Env:
    return await app_factory()


@pytest_asyncio.fixture
async def auth(env) -> dict:
    """Headers for a verified, logged-in default user."""
    return await env.register()


@pytest_asyncio.fixture
async def other_auth(env, auth) -> dict:
    """Headers for a second, different user."""
    return await env.register("someone.else@example.com")
