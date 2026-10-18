"""App-link files, the web fallback pages, and token redaction in logs."""

import logging
import re

import pytest

from app.config import Settings
from app.logging_utils import RedactTokensFilter, install_token_redaction, redact_tokens

FP1 = ":".join(["ab"] * 32)
FP2 = ":".join(["cd"] * 32)


def settings_kwargs(**kw):
    base = dict(database_url="sqlite:///x.db", secret_key="x" * 40, email_backend="console")
    base.update(kw)
    return base


def make(**kw) -> Settings:
    return Settings(_env_file=None, **settings_kwargs(**kw))


# --- configuration ----------------------------------------------------------------------


def test_app_link_settings_default_to_unconfigured():
    s = make()
    assert s.android_package_name is None and s.android_fingerprint_list == []
    assert s.apple_team_id is None and s.ios_bundle_id is None and s.app_scheme is None


def test_blank_values_from_empty_environment_variables_mean_not_set():
    s = make(android_package_name="  ", apple_team_id="", ios_bundle_id=" ", app_scheme="", android_sha256_fingerprints=" , ")
    assert s.android_package_name is None and s.apple_team_id is None and s.app_scheme is None
    assert s.android_fingerprint_list == []


def test_fingerprints_are_normalised_trimmed_and_deduplicated():
    s = make(android_package_name="com.a.b", android_sha256_fingerprints=f" {FP1} ,{FP2},{FP1.upper()}")
    assert s.android_fingerprint_list == [FP1.upper(), FP2.upper()]


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"android_package_name": "notapackage", "android_sha256_fingerprints": FP1}, "ANDROID_PACKAGE_NAME"),
        ({"android_package_name": "com.a.b", "android_sha256_fingerprints": "12:34"}, "32 hex pairs"),
        ({"android_package_name": "com.a.b"}, "both ANDROID_PACKAGE_NAME"),
        ({"android_sha256_fingerprints": FP1}, "both ANDROID_PACKAGE_NAME"),
        ({"apple_team_id": "SHORT", "ios_bundle_id": "com.a.b"}, "APPLE_TEAM_ID"),
        ({"apple_team_id": "ABCDE12345"}, "both APPLE_TEAM_ID"),
        ({"app_scheme": "Bad Scheme!"}, "APP_SCHEME"),
    ],
)
def test_invalid_app_link_settings_are_rejected_at_startup(kwargs, message):
    with pytest.raises(ValueError, match=message):
        make(**kwargs)


# --- well-known files ---------------------------------------------------------------------


async def test_well_known_files_are_404_until_configured(app_factory):
    env = await app_factory()
    assert (await env.client.get("/.well-known/assetlinks.json")).status_code == 404
    assert (await env.client.get("/.well-known/apple-app-site-association")).status_code == 404


async def test_assetlinks_json_matches_the_android_format(app_factory):
    env = await app_factory(android_package_name="com.mycompany.routewise", android_sha256_fingerprints=f"{FP1},{FP2}")
    r = await env.client.get("/.well-known/assetlinks.json")
    assert r.status_code == 200  # no redirect: Android refuses redirected files
    assert r.headers["content-type"].startswith("application/json")
    assert r.json() == [
        {
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                "package_name": "com.mycompany.routewise",
                "sha256_cert_fingerprints": [FP1.upper(), FP2.upper()],
            },
        }
    ]


async def test_apple_app_site_association_matches_the_ios_format(app_factory):
    env = await app_factory(apple_team_id="abcde12345", ios_bundle_id="com.mycompany.routewise")
    r = await env.client.get("/.well-known/apple-app-site-association")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    detail = r.json()["applinks"]["details"][0]
    assert detail["appIDs"] == ["ABCDE12345.com.mycompany.routewise"]
    assert detail["components"] == [{"/": "/verify-email*"}, {"/": "/reset-password*"}]


async def test_ios_and_android_are_independent(app_factory):
    env = await app_factory(android_package_name="com.a.b", android_sha256_fingerprints=FP1)
    assert (await env.client.get("/.well-known/assetlinks.json")).status_code == 200
    assert (await env.client.get("/.well-known/apple-app-site-association")).status_code == 404


async def test_none_of_these_routes_appear_in_the_api_docs(env):
    paths = (await env.client.get("/openapi.json")).json()["paths"]
    assert not any(p.startswith("/.well-known") for p in paths)
    assert "/verify-email" not in paths and "/reset-password" not in paths
    assert "/auth/verify-email" in paths  # the real JSON endpoints are still documented


async def test_paths_match_the_links_in_the_emails(env):
    from app.routers.app_links import APP_LINK_PATHS

    await env.client.post("/auth/signup", json={"email": "a@example.com", "password": "correct-horse-battery"})
    link = re.search(r"https?://\S+", env.emails.outbox[-1].body).group(0)
    assert any(link.split("?")[0].endswith(path) for path in APP_LINK_PATHS)
    await env.client.post("/auth/forgot-password", json={"email": "a@example.com"})
    link = re.search(r"https?://\S+", env.emails.outbox[-1].body).group(0)
    assert any(link.split("?")[0].endswith(path) for path in APP_LINK_PATHS)


# --- web pages ------------------------------------------------------------------------------


@pytest.mark.parametrize("path, marker", [("/verify-email", "Confirming your email"), ("/reset-password", "Choose a new password")])
async def test_pages_render_with_hardened_headers(env, path, marker):
    r = await env.client.get(path, params={"token": "abc"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert marker in r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-content-type-options"] == "nosniff"
    nonce = re.search(r"script-src 'nonce-([^']+)'", r.headers["content-security-policy"]).group(1)
    assert f'<script nonce="{nonce}">' in r.text and f'<style nonce="{nonce}">' in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "connect-src 'self'" in r.headers["content-security-policy"]


async def test_each_page_load_gets_a_fresh_nonce(env):
    one = (await env.client.get("/verify-email")).headers["content-security-policy"]
    two = (await env.client.get("/verify-email")).headers["content-security-policy"]
    assert one != two


async def test_the_token_is_never_reflected_into_the_html(env):
    hostile = "<script>alert(1)</script>"
    for path in ("/verify-email", "/reset-password"):
        r = await env.client.get(path, params={"token": hostile, "x": '"><img src=x>'})
        assert hostile not in r.text and "alert(1)" not in r.text and "<img src=x>" not in r.text
        assert "%3Cscript" not in r.text


async def test_pages_call_the_real_api_endpoints_and_scrub_the_token_from_the_url(env):
    verify = (await env.client.get("/verify-email")).text
    assert '"/auth/verify-email"' in verify and "history.replaceState" in verify
    reset = (await env.client.get("/reset-password")).text
    assert '"/auth/reset-password"' in reset and "new_password" in reset and "history.replaceState" in reset


async def test_open_in_app_button_uses_the_configured_scheme_only(app_factory):
    without = await app_factory()
    assert 'data-scheme=""' in (await without.client.get("/verify-email")).text
    with_scheme = await app_factory(app_scheme="routewise")
    assert 'data-scheme="routewise"' in (await with_scheme.client.get("/verify-email")).text


async def test_pages_complete_the_real_flow_through_the_api(env):
    """What the page's script does: POST the token to the JSON endpoints."""
    await env.client.post("/auth/signup", json={"email": "a@example.com", "password": "correct-horse-battery"})
    token = env.last_token()
    assert (await env.client.post("/auth/verify-email", json={"token": token})).status_code == 200
    await env.client.post("/auth/forgot-password", json={"email": "a@example.com"})
    r = await env.client.post("/auth/reset-password", json={"token": env.last_token(), "new_password": "a-brand-new-password"})
    assert r.status_code == 200


# --- token redaction ----------------------------------------------------------------------------


def test_redact_tokens_in_urls():
    assert redact_tokens("GET /verify-email?token=abc.def-ghi HTTP/1.1") == "GET /verify-email?token=[redacted] HTTP/1.1"
    assert redact_tokens("/x?a=1&token=SECRET&b=2") == "/x?a=1&token=[redacted]&b=2"
    assert redact_tokens("/health") == "/health"


def test_filter_redacts_uvicorn_style_access_records():
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", "/reset-password?token=TOPSECRET", "1.1", 200), None,
    )
    assert RedactTokensFilter().filter(record) is True
    assert "TOPSECRET" not in record.getMessage() and "token=[redacted]" in record.getMessage()


def test_install_is_idempotent():
    install_token_redaction()
    install_token_redaction()
    filters = [f for f in logging.getLogger("uvicorn.access").filters if isinstance(f, RedactTokensFilter)]
    assert len(filters) == 1
