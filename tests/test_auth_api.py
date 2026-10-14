from datetime import timedelta

from app.security import create_token

from .conftest import PASSWORD, SECRET


async def test_signup_creates_unverified_account_and_sends_email(env):
    r = await env.client.post(
        "/auth/signup", json={"email": "Rider@Example.com", "password": PASSWORD, "display_name": "Ada"}
    )
    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"id", "email", "display_name", "email_verified", "created_at"}
    assert body["email"] == "rider@example.com" and body["email_verified"] is False
    assert len(env.emails.outbox) == 1
    message = env.emails.outbox[0]
    assert message.to == "rider@example.com" and "/verify-email?token=" in message.body


async def test_duplicate_email_is_rejected_case_insensitively(env):
    await env.client.post("/auth/signup", json={"email": "a@example.com", "password": PASSWORD})
    r = await env.client.post("/auth/signup", json={"email": "A@EXAMPLE.COM", "password": PASSWORD})
    assert r.status_code == 409


async def test_signup_validation(env):
    assert (await env.client.post("/auth/signup", json={"email": "a@example.com", "password": "short"})).status_code == 422
    assert (await env.client.post("/auth/signup", json={"email": "not-an-email", "password": PASSWORD})).status_code == 422
    assert (await env.client.post("/auth/signup", json={"password": PASSWORD})).status_code == 422


async def test_cannot_log_in_until_email_is_verified(env):
    await env.client.post("/auth/signup", json={"email": "a@example.com", "password": PASSWORD})
    r = await env.client.post("/auth/login", json={"email": "a@example.com", "password": PASSWORD})
    assert r.status_code == 403 and "not verified" in r.json()["detail"]


async def test_verify_then_login_then_me(env):
    headers = await env.register("a@example.com")
    me = await env.client.get("/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["email"] == "a@example.com" and me.json()["email_verified"] is True


async def test_verifying_twice_is_harmless(env):
    await env.client.post("/auth/signup", json={"email": "a@example.com", "password": PASSWORD})
    token = env.last_token()
    assert (await env.client.post("/auth/verify-email", json={"token": token})).status_code == 200
    assert (await env.client.post("/auth/verify-email", json={"token": token})).status_code == 200


async def test_bad_credentials_give_the_same_answer_for_unknown_email_and_wrong_password(env):
    await env.register("a@example.com")
    wrong_password = await env.client.post("/auth/login", json={"email": "a@example.com", "password": "nope-nope-nope"})
    unknown = await env.client.post("/auth/login", json={"email": "ghost@example.com", "password": PASSWORD})
    assert wrong_password.status_code == unknown.status_code == 401
    assert wrong_password.json() == unknown.json()


async def test_verify_rejects_garbage_and_tokens_meant_for_other_things(env):
    assert (await env.client.post("/auth/verify-email", json={"token": "garbage"})).status_code == 400
    await env.register("a@example.com")
    me = (await env.client.post("/auth/login", json={"email": "a@example.com", "password": PASSWORD})).json()
    r = await env.client.post("/auth/verify-email", json={"token": me["access_token"]})
    assert r.status_code == 400  # an access token is not a verification token


async def test_resend_verification_only_emails_unverified_accounts(env):
    await env.client.post("/auth/signup", json={"email": "a@example.com", "password": PASSWORD})
    env.emails.outbox.clear()

    ghost = await env.client.post("/auth/resend-verification", json={"email": "ghost@example.com"})
    assert ghost.status_code == 202 and env.emails.outbox == []

    real = await env.client.post("/auth/resend-verification", json={"email": "a@example.com"})
    assert real.status_code == 202 and len(env.emails.outbox) == 1
    assert real.json() == ghost.json()  # indistinguishable from outside

    await env.client.post("/auth/verify-email", json={"token": env.last_token()})
    env.emails.outbox.clear()
    await env.client.post("/auth/resend-verification", json={"email": "a@example.com"})
    assert env.emails.outbox == []  # already verified


async def test_password_reset_flow(env):
    await env.register("a@example.com")
    env.emails.outbox.clear()

    ghost = await env.client.post("/auth/forgot-password", json={"email": "ghost@example.com"})
    assert ghost.status_code == 202 and env.emails.outbox == []

    known = await env.client.post("/auth/forgot-password", json={"email": "a@example.com"})
    assert known.status_code == 202 and known.json() == ghost.json()
    assert "/reset-password?token=" in env.emails.outbox[0].body

    token = env.last_token()
    new_password = "a-brand-new-password"
    r = await env.client.post("/auth/reset-password", json={"token": token, "new_password": new_password})
    assert r.status_code == 200

    old = await env.client.post("/auth/login", json={"email": "a@example.com", "password": PASSWORD})
    new = await env.client.post("/auth/login", json={"email": "a@example.com", "password": new_password})
    assert old.status_code == 401 and new.status_code == 200

    # The link works once only.
    again = await env.client.post("/auth/reset-password", json={"token": token, "new_password": "yet-another-password"})
    assert again.status_code == 400


async def test_reset_rejects_bad_tokens_and_weak_passwords(env):
    assert (await env.client.post("/auth/reset-password", json={"token": "garbage", "new_password": PASSWORD})).status_code == 400
    await env.register("a@example.com")
    await env.client.post("/auth/forgot-password", json={"email": "a@example.com"})
    r = await env.client.post("/auth/reset-password", json={"token": env.last_token(), "new_password": "short"})
    assert r.status_code == 422
    verification_style = create_token(SECRET, subject="x", purpose="verify_email", expires_in=timedelta(hours=1))
    assert (await env.client.post("/auth/reset-password", json={"token": verification_style, "new_password": PASSWORD})).status_code == 400


async def test_reset_also_confirms_the_email(env):
    await env.client.post("/auth/signup", json={"email": "a@example.com", "password": PASSWORD})
    await env.client.post("/auth/forgot-password", json={"email": "a@example.com"})
    await env.client.post("/auth/reset-password", json={"token": env.last_token(), "new_password": "a-brand-new-password"})
    r = await env.client.post("/auth/login", json={"email": "a@example.com", "password": "a-brand-new-password"})
    assert r.status_code == 200


async def test_form_login_endpoint_for_the_docs_page(env):
    await env.register("a@example.com")
    r = await env.client.post("/auth/token", data={"username": "a@example.com", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["token_type"] == "bearer"


async def test_protected_endpoints_need_a_valid_access_token(env):
    assert (await env.client.get("/auth/me")).status_code == 401
    assert (await env.client.get("/auth/me", headers={"Authorization": "Bearer garbage"})).status_code == 401
    assert (await env.client.get("/trips")).status_code == 401
    wrong_kind = create_token(SECRET, subject="x", purpose="verify_email", expires_in=timedelta(hours=1))
    assert (await env.client.get("/auth/me", headers={"Authorization": f"Bearer {wrong_kind}"})).status_code == 401
    expired = create_token(SECRET, subject="x", purpose="access", expires_in=timedelta(seconds=-1))
    assert (await env.client.get("/auth/me", headers={"Authorization": f"Bearer {expired}"})).status_code == 401


async def test_token_for_a_deleted_account_stops_working(env, auth, sessionmaker):
    from sqlalchemy import delete

    from app.models import User

    async with sessionmaker() as session:
        await session.execute(delete(User))
        await session.commit()
    assert (await env.client.get("/auth/me", headers=auth)).status_code == 401


async def test_verification_can_be_switched_off(app_factory):
    env = await app_factory(require_email_verification=False)
    headers = await env.register("a@example.com")
    assert env.emails.outbox == []
    assert (await env.client.get("/auth/me", headers=headers)).status_code == 200


async def test_failing_email_provider_does_not_break_signup(app_factory):
    env = await app_factory()

    class Broken:
        def send(self, message):
            raise OSError("smtp down")

    env.app.state.email_sender = Broken()
    r = await env.client.post("/auth/signup", json={"email": "a@example.com", "password": PASSWORD})
    assert r.status_code == 201
