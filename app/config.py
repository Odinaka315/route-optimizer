"""Application settings, read from environment variables or a local .env file."""

from __future__ import annotations

import re
from email.utils import parseaddr
from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PUBLIC_OSRM_DEMO = "https://router.project-osrm.org"

_ANDROID_PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")
_SHA256_FINGERPRINT_RE = re.compile(r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")
_APPLE_TEAM_ID_RE = re.compile(r"^[A-Z0-9]{10}$")
_URL_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    environment: Literal["development", "test", "production"] = "development"

    # --- database -----------------------------------------------------------
    # Paste your Neon *pooled* connection string here. The app converts it to the
    # async driver format itself (see app.db.normalize_database_url).
    database_url: str
    # Neon recommends the *direct* (non-pooler) string for schema migrations.
    # If unset, Alembic falls back to DATABASE_URL.
    alembic_database_url: str | None = None
    # Escape hatch: set true if you ever see "prepared statement ... does not
    # exist" errors behind a connection pooler.
    db_disable_statement_cache: bool = False
    db_pool_size: int = Field(5, ge=1)
    db_max_overflow: int = Field(5, ge=0)

    # --- auth ---------------------------------------------------------------
    secret_key: str = Field(min_length=32)
    access_token_expire_minutes: int = Field(15, ge=1)  # short: the refresh token keeps people signed in
    refresh_token_expire_days: int = Field(30, ge=1)  # each refresh restarts this clock
    verify_token_expire_hours: int = Field(48, ge=1)
    reset_token_expire_minutes: int = Field(60, ge=1)
    require_email_verification: bool = True

    # --- email --------------------------------------------------------------
    app_base_url: str = "http://localhost:3000"  # base of the links put in emails
    cors_origins: str = ""  # comma-separated, e.g. "http://localhost:3000,https://app.example.com"
    email_backend: Literal["brevo", "console", "smtp"] = "brevo"
    email_from: str = "routeWise <nwolisaodinaka5@gmail.com>"
    email_from_name: str = "routeWise"
    email_from_email: str = "nwolisaodinaka5@gmail.com"
    brevo_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "BREVO_API_KEY",
            "BREVO_SECRET_KEY",
            "brevo_api_key",
            "brevo_secret_key",
        ),
    )
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_starttls: bool = True

    # --- app links (open emailed links in the mobile app) ---------------------
    # The backend publishes the files phones use to confirm that links on this domain
    # belong to your app. Leave blank until the app exists; each endpoint returns 404
    # until it is configured.
    android_package_name: str | None = None  # e.g. com.mycompany.routewise
    android_sha256_fingerprints: str = ""  # comma-separated; debug, release and Play signing keys differ
    apple_team_id: str | None = None  # 10 characters
    ios_bundle_id: str | None = None
    app_scheme: str | None = None  # optional custom scheme (e.g. routewise) for an "Open in the app" button

    # --- engine -------------------------------------------------------------
    matrix_backend: Literal["osrm", "haversine"] = "osrm"
    osrm_url: str = PUBLIC_OSRM_DEMO  # the public demo is for development only
    haversine_speed_kmh: float = Field(25.0, gt=0)
    solver_time_limit_s: int = Field(10, ge=1, le=60)
    max_stops: int = Field(50, ge=2, le=200)
    max_concurrent_solves: int = Field(2, ge=1)
    run_timeout_minutes: int = Field(10, ge=1)
    # inline = the API process solves in the background (simple, one server).
    # worker = the API only queues runs; a separate `python -m app.worker` process solves them.
    run_mode: Literal["inline", "worker"] = "inline"

    # --- worker (only used when you run `python -m app.worker`) ----------------
    worker_poll_interval_s: float = Field(2.0, ge=0.1, le=60)  # how often to look for queued runs
    run_lease_seconds: int = Field(90, ge=10)  # a running job whose heartbeat is older than this is presumed dead
    run_max_attempts: int = Field(3, ge=1, le=10)  # tries per run before it is marked failed
    run_retry_backoff_s: int = Field(10, ge=0)  # wait attempt x this long before a retry
    worker_shutdown_grace_s: int = Field(30, ge=0)  # on SIGTERM, let running solves finish this long

    # --- geocoding ----------------------------------------------------------
    geocoder_provider: Literal["nominatim", "mapbox"] = "nominatim"
    geocoder_country: str = "ng"
    mapbox_access_token: str | None = None
    # Mapbox only lets you store results that were requested with permanent=true
    # (billed differently, needs a card on file). Keep true because trips store
    # coordinates; if false, results are never written to the geocode cache.
    mapbox_permanent: bool = True
    nominatim_user_agent: str = (
        "route-optimizer/0.2 (set NOMINATIM_USER_AGENT to include a contact email)"
    )

    @model_validator(mode="after")
    def _cross_checks(self) -> "Settings":
        if self.geocoder_provider == "mapbox" and not self.mapbox_access_token:
            raise ValueError("MAPBOX_ACCESS_TOKEN is required when GEOCODER_PROVIDER=mapbox")
        if self.email_backend == "smtp" and not self.smtp_host:
            raise ValueError("SMTP_HOST is required when EMAIL_BACKEND=smtp")
        if self.environment == "production" and self.secret_key.lower().startswith("change-me"):
            raise ValueError("SECRET_KEY is still the placeholder value; generate a real one")
        if self.email_backend == "brevo" and not self.brevo_api_key and self.environment == "production":
            raise ValueError("BREVO_API_KEY (or BREVO_SECRET_KEY) is required when EMAIL_BACKEND=brevo in production")
        self._check_app_links()
        return self

    def _check_app_links(self) -> None:
        # Blank values from an empty environment variable mean "not set".
        for name in ("android_package_name", "apple_team_id", "ios_bundle_id", "app_scheme"):
            value = getattr(self, name)
            if value is not None and not value.strip():
                setattr(self, name, None)
            elif value is not None:
                setattr(self, name, value.strip())
        if self.android_package_name and not _ANDROID_PACKAGE_RE.match(self.android_package_name):
            raise ValueError("ANDROID_PACKAGE_NAME must look like com.mycompany.myapp")
        fingerprints = self.android_fingerprint_list
        for fingerprint in fingerprints:
            if not _SHA256_FINGERPRINT_RE.match(fingerprint):
                raise ValueError(
                    "ANDROID_SHA256_FINGERPRINTS must be 32 hex pairs separated by colons "
                    "(AA:BB:...), comma-separated if there are several"
                )
        if bool(self.android_package_name) != bool(fingerprints):
            raise ValueError(
                "Set both ANDROID_PACKAGE_NAME and ANDROID_SHA256_FINGERPRINTS, or neither"
            )
        if self.apple_team_id and not _APPLE_TEAM_ID_RE.match(self.apple_team_id.upper()):
            raise ValueError("APPLE_TEAM_ID must be 10 letters/digits")
        if bool(self.apple_team_id) != bool(self.ios_bundle_id):
            raise ValueError("Set both APPLE_TEAM_ID and IOS_BUNDLE_ID, or neither")
        if self.app_scheme and not _URL_SCHEME_RE.match(self.app_scheme):
            raise ValueError("APP_SCHEME must be lowercase letters/digits, e.g. routewise")

    @property
    def android_fingerprint_list(self) -> list[str]:
        """Normalised (uppercase, trimmed) fingerprints, without blanks or duplicates."""
        seen: dict[str, None] = {}
        for item in self.android_sha256_fingerprints.split(","):
            item = item.strip().upper()
            if item:
                seen[item] = None
        return list(seen)

    @property
    def sender_email(self) -> str:
        _, addr = parseaddr(self.email_from)
        return addr or self.email_from_email

    @property
    def sender_name(self) -> str:
        name, _ = parseaddr(self.email_from)
        return name or self.email_from_name

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
