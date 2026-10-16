"""Pure unit tests: polyline, security, config, database URLs."""

import random
from datetime import timedelta

import pytest

from app.config import Settings
from app.db import is_pooled_url, normalize_database_url
from app.security import (
    TokenError,
    create_token,
    decode_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    password_fingerprint,
    verify_password,
)
from app.services.polyline import decode_polyline, encode_polyline

SECRET = "x" * 40


# --- polyline ------------------------------------------------------------------


def test_polyline_matches_googles_documented_example():
    points = [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]
    assert encode_polyline(points, precision=5) == "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
    assert decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=5) == points


def test_polyline_round_trip_at_precision_6():
    rng = random.Random(1)
    points = [(round(6.4 + rng.random() * 0.3, 6), round(3.2 + rng.random() * 0.4, 6)) for _ in range(500)]
    assert decode_polyline(encode_polyline(points)) == points


def test_polyline_edge_cases():
    assert encode_polyline([]) == ""
    assert decode_polyline("") == []
    assert decode_polyline(encode_polyline([(6.5, 3.4)])) == [(6.5, 3.4)]
    southwest = [(-33.9, -70.6), (-34.0, -70.7)]  # negative coordinates
    assert decode_polyline(encode_polyline(southwest)) == southwest


# --- security --------------------------------------------------------------------


def test_password_hash_verifies_and_rejects():
    hashed = hash_password("a long enough password")
    assert hashed != "a long enough password"
    assert verify_password(hashed, "a long enough password")
    assert not verify_password(hashed, "wrong password")
    assert not verify_password("not-a-real-hash", "anything")


def test_password_hashes_are_salted():
    assert hash_password("same password") != hash_password("same password")


def test_token_round_trip():
    token = create_token(SECRET, subject="abc", purpose="access", expires_in=timedelta(minutes=5), extra={"k": 1})
    payload = decode_token(SECRET, token, purpose="access")
    assert payload["sub"] == "abc" and payload["k"] == 1


def test_token_rejected_when_expired_tampered_or_wrong_purpose():
    expired = create_token(SECRET, subject="abc", purpose="access", expires_in=timedelta(seconds=-5))
    with pytest.raises(TokenError):
        decode_token(SECRET, expired, purpose="access")

    good = create_token(SECRET, subject="abc", purpose="access", expires_in=timedelta(minutes=5))
    with pytest.raises(TokenError):
        decode_token("y" * 40, good, purpose="access")  # signed with a different secret
    with pytest.raises(TokenError):
        decode_token(SECRET, good + "x", purpose="access")  # tampered
    with pytest.raises(TokenError):
        decode_token(SECRET, good, purpose="reset_password")  # right token, wrong job
    with pytest.raises(TokenError):
        decode_token(SECRET, "garbage", purpose="access")


def test_fingerprint_changes_with_the_hash():
    assert password_fingerprint("hash-one") != password_fingerprint("hash-two")
    assert password_fingerprint("hash-one") == password_fingerprint("hash-one")


def test_refresh_tokens_are_random_and_hash_deterministically():
    a, b = new_refresh_token(), new_refresh_token()
    assert a != b and len(a) >= 64
    assert hash_refresh_token(a) == hash_refresh_token(a) != hash_refresh_token(b)
    assert len(hash_refresh_token(a)) == 64 and a not in hash_refresh_token(a)


# --- database URLs -----------------------------------------------------------------


def test_normalizes_a_neon_connection_string():
    neon = "postgresql://owner:p%40ss@ep-cool-123-pooler.eu-west-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
    out = normalize_database_url(neon)
    assert out.startswith("postgresql+asyncpg://owner:p%40ss@ep-cool-123-pooler.eu-west-2.aws.neon.tech/neondb")
    assert "ssl=require" in out
    assert "sslmode" not in out and "channel_binding" not in out


@pytest.mark.parametrize(
    "given, expected_prefix",
    [
        ("postgres://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
        ("postgresql+asyncpg://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
        ("postgresql+psycopg2://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
        ("sqlite:///x.db", "sqlite+aiosqlite:///x.db"),
        ("sqlite+aiosqlite:///x.db", "sqlite+aiosqlite:///x.db"),
    ],
)
def test_normalizes_driver_names(given, expected_prefix):
    assert normalize_database_url(given).startswith(expected_prefix)


def test_existing_ssl_parameter_is_kept():
    out = normalize_database_url("postgresql://u:p@h/db?sslmode=require&ssl=verify-full")
    assert "ssl=verify-full" in out and "sslmode" not in out


def test_unsupported_database_rejected():
    with pytest.raises(ValueError):
        normalize_database_url("mysql://u:p@h/db")


def test_pooled_url_detection():
    assert is_pooled_url("postgresql://u:p@ep-a-1-pooler.neon.tech/db")
    assert not is_pooled_url("postgresql://u:p@ep-a-1.neon.tech/db")


# --- settings ----------------------------------------------------------------------


def make(**kw) -> Settings:
    base = dict(database_url="sqlite:///x.db", secret_key=SECRET)
    base.update(kw)
    return Settings(_env_file=None, **base)


def test_short_secret_key_rejected():
    with pytest.raises(ValueError):
        make(secret_key="too-short")


def test_mapbox_needs_a_token():
    with pytest.raises(ValueError, match="MAPBOX_ACCESS_TOKEN"):
        make(geocoder_provider="mapbox")
    assert make(geocoder_provider="mapbox", mapbox_access_token="pk.x").geocoder_provider == "mapbox"


def test_smtp_needs_a_host():
    with pytest.raises(ValueError, match="SMTP_HOST"):
        make(email_backend="smtp")


def test_production_rejects_placeholder_secret():
    with pytest.raises(ValueError, match="placeholder"):
        make(environment="production", secret_key="change-me-" + "x" * 30)


def test_token_lifetime_defaults_are_short_access_long_refresh():
    s = make()
    assert s.access_token_expire_minutes == 15 and s.refresh_token_expire_days == 30


def test_cors_origins_parsed_from_comma_list():
    assert make(cors_origins="http://a.test, https://b.test ,").cors_origin_list == [
        "http://a.test",
        "https://b.test",
    ]
    assert make().cors_origin_list == []


# --- console email ---------------------------------------------------------------


def test_console_email_is_visible_in_the_terminal(capsys):
    from app.emailer import ConsoleEmailSender, EmailMessage

    ConsoleEmailSender().send(EmailMessage("a@example.com", "Hi", "Click http://app/verify-email?token=abc"))
    out = capsys.readouterr().out
    assert "a@example.com" in out and "verify-email?token=abc" in out


# --- brevo email -----------------------------------------------------------------


def test_brevo_needs_key_in_production():
    with pytest.raises(ValueError, match="BREVO_API_KEY"):
        make(environment="production", email_backend="brevo", brevo_api_key=None)
    s = make(environment="production", email_backend="brevo", brevo_api_key="xkeysib-test-key-123")
    assert s.brevo_api_key == "xkeysib-test-key-123"


def test_brevo_secret_key_alias():
    s = make(brevo_secret_key="my-brevo-secret")
    assert s.brevo_api_key == "my-brevo-secret"


def test_sender_email_and_name_default():
    s = make()
    assert s.sender_email == "nwolisaodinaka5@gmail.com"
    assert s.sender_name == "routeWise"


def test_brevo_sender_missing_key_raises():
    from app.emailer import BrevoEmailSender, BrevoError, EmailMessage

    sender = BrevoEmailSender(api_key="")
    with pytest.raises(BrevoError, match="not configured"):
        sender.send(EmailMessage("user@example.com", "Subject", "Body"))


def test_brevo_sender_successful_api_call(monkeypatch):
    import requests
    from app.emailer import BrevoEmailSender, EmailMessage

    recorded = {}

    def fake_post(url, headers, json, timeout):
        recorded["url"] = url
        recorded["headers"] = headers
        recorded["json"] = json
        recorded["timeout"] = timeout

        class FakeResponse:
            ok = True
            status_code = 201

            def json(self):
                return {"messageId": "<msg-123@brevo>"}

        return FakeResponse()

    monkeypatch.setattr(requests, "post", fake_post)

    sender = BrevoEmailSender(
        api_key="xkeysib-secret-key-123",
        sender_email="nwolisaodinaka5@gmail.com",
        sender_name="routeWise",
    )
    msg = EmailMessage("recipient@example.com", "Test Subject", "Hello world line 1\n\nLine 2")
    sender.send(msg)

    assert recorded["url"] == "https://api.brevo.com/v3/smtp/email"
    assert recorded["headers"]["api-key"] == "xkeysib-secret-key-123"
    assert recorded["headers"]["content-type"] == "application/json"
    assert recorded["json"]["sender"]["name"] == "routeWise"
    assert recorded["json"]["sender"]["email"] == "nwolisaodinaka5@gmail.com"
    assert recorded["json"]["to"] == [{"email": "recipient@example.com"}]
    assert recorded["json"]["subject"] == "Test Subject"
    assert recorded["json"]["textContent"] == "Hello world line 1\n\nLine 2"
    assert "<p" in recorded["json"]["htmlContent"]
    assert "routeWise" in recorded["json"]["htmlContent"]


def test_brevo_sender_error_handling(monkeypatch):
    import requests
    from app.emailer import BrevoEmailSender, BrevoError, EmailMessage

    def fake_post(url, headers, json, timeout):
        class FakeResponse:
            ok = False
            status_code = 400
            text = "Bad Request"

            def json(self):
                return {"message": "Invalid sender email address"}

        return FakeResponse()

    monkeypatch.setattr(requests, "post", fake_post)

    sender = BrevoEmailSender(api_key="xkeysib-invalid")
    with pytest.raises(BrevoError, match="Invalid sender email address"):
        sender.send(EmailMessage("recipient@example.com", "Test", "Body"))


def test_build_email_sender_brevo():
    from app.emailer import BrevoEmailSender, build_email_sender

    s = make(email_backend="brevo", brevo_api_key="key-123")
    sender = build_email_sender(s)
    assert isinstance(sender, BrevoEmailSender)
    assert sender.api_key == "key-123"
    assert sender.sender_email == "nwolisaodinaka5@gmail.com"
    assert sender.sender_name == "routeWise"

