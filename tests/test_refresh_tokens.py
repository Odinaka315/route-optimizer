"""Refresh tokens: login pair, rotation, replay detection, logout, logout-all, password reset."""

from datetime import timedelta

from sqlalchemy import select, update

from app.models import RefreshToken, utcnow
from app.security import hash_refresh_token, new_refresh_token

from .conftest import PASSWORD


async def login(env, email="a@example.com", password=PASSWORD) -> dict:
    r = await env.client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


async def refresh(env, token: str):
    return await env.client.post("/auth/refresh", json={"refresh_token": token})


def bearer(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def rows(sessionmaker) -> list[RefreshToken]:
    async with sessionmaker() as session:
        return list((await session.scalars(select(RefreshToken))).all())


async def test_login_returns_a_token_pair_with_a_short_access_lifetime(env):
    await env.register("a@example.com")
    tokens = await login(env)
    assert set(tokens) == {"access_token", "token_type", "expires_in", "refresh_token", "refresh_expires_in"}
    assert tokens["expires_in"] == 15 * 60
    assert tokens["refresh_expires_in"] == 30 * 24 * 3600
    assert tokens["refresh_token"] != tokens["access_token"]


async def test_form_login_also_returns_a_refresh_token(env):
    await env.register("a@example.com")
    r = await env.client.post("/auth/token", data={"username": "a@example.com", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["refresh_token"]


async def test_only_the_hash_is_stored(env, sessionmaker):
    await env.register("a@example.com")
    tokens = await login(env)
    stored = await rows(sessionmaker)
    assert all(row.token_hash != tokens["refresh_token"] for row in stored)
    assert hash_refresh_token(tokens["refresh_token"]) in {row.token_hash for row in stored}


async def test_refresh_returns_new_working_tokens_and_retires_the_old_one(env):
    await env.register("a@example.com")
    first = await login(env)
    r = await refresh(env, first["refresh_token"])
    assert r.status_code == 200
    second = r.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert (await env.client.get("/auth/me", headers=bearer(second))).status_code == 200
    # The chain keeps going.
    third = await refresh(env, second["refresh_token"])
    assert third.status_code == 200
    # Rotation: the old refresh token is dead.
    assert (await refresh(env, first["refresh_token"])).status_code == 401


async def test_replaying_a_rotated_token_signs_out_the_whole_session(env):
    await env.register("a@example.com")
    first = await login(env)
    second = (await refresh(env, first["refresh_token"])).json()
    assert (await refresh(env, first["refresh_token"])).status_code == 401  # replay
    # The legitimate holder of the newest token is cut off too.
    assert (await refresh(env, second["refresh_token"])).status_code == 401


async def test_a_replay_does_not_touch_other_devices(env):
    await env.register("a@example.com")
    phone, laptop = await login(env), await login(env)
    rotated = (await refresh(env, phone["refresh_token"])).json()
    await refresh(env, phone["refresh_token"])  # replay on the phone's chain
    assert (await refresh(env, rotated["refresh_token"])).status_code == 401
    assert (await refresh(env, laptop["refresh_token"])).status_code == 200


async def test_refresh_rejects_garbage_unknown_and_expired_tokens(env, sessionmaker):
    await env.register("a@example.com")
    assert (await refresh(env, "garbage")).status_code == 401
    assert (await refresh(env, new_refresh_token())).status_code == 401
    assert (await env.client.post("/auth/refresh", json={})).status_code == 422
    assert (await env.client.post("/auth/refresh", json={"refresh_token": ""})).status_code == 422

    tokens = await login(env)
    async with sessionmaker() as session:
        await session.execute(update(RefreshToken).values(expires_at=utcnow() - timedelta(seconds=1)))
        await session.commit()
    r = await refresh(env, tokens["refresh_token"])
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"


async def test_an_access_token_is_not_a_refresh_token(env):
    await env.register("a@example.com")
    tokens = await login(env)
    assert (await refresh(env, tokens["access_token"])).status_code == 401
    r = await env.client.get("/auth/me", headers={"Authorization": f"Bearer {tokens['refresh_token']}"})
    assert r.status_code == 401


async def test_refresh_refuses_a_deactivated_account(env, sessionmaker):
    from app.models import User

    await env.register("a@example.com")
    tokens = await login(env)
    async with sessionmaker() as session:
        await session.execute(update(User).values(is_active=False))
        await session.commit()
    assert (await refresh(env, tokens["refresh_token"])).status_code == 401


async def test_simultaneous_refreshes_only_one_wins(env):
    import asyncio

    await env.register("a@example.com")
    tokens = await login(env)
    results = await asyncio.gather(*[refresh(env, tokens["refresh_token"]) for _ in range(4)])
    assert sorted(r.status_code for r in results).count(200) <= 1


async def test_logout_ends_only_that_session(env):
    await env.register("a@example.com")
    phone, laptop = await login(env), await login(env)
    newest = (await refresh(env, phone["refresh_token"])).json()

    r = await env.client.post("/auth/logout", json={"refresh_token": newest["refresh_token"]})
    assert r.status_code == 200
    assert (await refresh(env, newest["refresh_token"])).status_code == 401
    assert (await refresh(env, laptop["refresh_token"])).status_code == 200


async def test_logout_needs_no_access_token_and_is_safe_to_repeat(env):
    await env.register("a@example.com")
    tokens = await login(env)
    for _ in range(2):
        r = await env.client.post("/auth/logout", json={"refresh_token": tokens["refresh_token"]})
        assert r.status_code == 200
    assert (await env.client.post("/auth/logout", json={"refresh_token": "never-existed"})).status_code == 200


async def test_logout_all_ends_every_session(env, sessionmaker):
    await env.register("a@example.com")
    await env.register("b@example.com")
    sessions = [await login(env) for _ in range(3)]
    other = await login(env, "b@example.com")

    r = await env.client.post("/auth/logout-all", headers=bearer(sessions[0]))
    assert r.status_code == 200
    for tokens in sessions:
        assert (await refresh(env, tokens["refresh_token"])).status_code == 401
    assert (await refresh(env, other["refresh_token"])).status_code == 200  # someone else's


async def test_logout_all_requires_login(env):
    assert (await env.client.post("/auth/logout-all")).status_code == 401


async def test_password_reset_cancels_refresh_tokens(env):
    await env.register("a@example.com")
    phone, laptop = await login(env), await login(env)
    await env.client.post("/auth/forgot-password", json={"email": "a@example.com"})
    r = await env.client.post(
        "/auth/reset-password", json={"token": env.last_token(), "new_password": "a-brand-new-password"}
    )
    assert r.status_code == 200
    assert (await refresh(env, phone["refresh_token"])).status_code == 401
    assert (await refresh(env, laptop["refresh_token"])).status_code == 401
    # Logging in again works and starts a fresh session.
    fresh = await login(env, password="a-brand-new-password")
    assert (await refresh(env, fresh["refresh_token"])).status_code == 200


async def test_failed_reset_leaves_sessions_alone(env):
    await env.register("a@example.com")
    tokens = await login(env)
    r = await env.client.post("/auth/reset-password", json={"token": "garbage", "new_password": PASSWORD})
    assert r.status_code == 400
    assert (await refresh(env, tokens["refresh_token"])).status_code == 200


async def test_expired_rows_are_purged_at_login(env, sessionmaker):
    await env.register("a@example.com")
    old = await login(env)
    async with sessionmaker() as session:
        await session.execute(update(RefreshToken).values(expires_at=utcnow() - timedelta(days=8)))
        await session.commit()
    await login(env)
    stored = await rows(sessionmaker)
    assert len(stored) == 1 and stored[0].token_hash != hash_refresh_token(old["refresh_token"])


async def test_deleting_a_user_removes_their_tokens(env, sessionmaker):
    from sqlalchemy import delete

    from app.models import User

    await env.register("a@example.com")
    await login(env)
    async with sessionmaker() as session:
        await session.execute(delete(User))
        await session.commit()
    assert await rows(sessionmaker) == []
