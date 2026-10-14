"""Application settings, read from environment variables or a local .env file."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PUBLIC_OSRM_DEMO = "https://router.project-osrm.org"


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
    access_token_expire_minutes: int = Field(60 * 24, ge=1)
    verify_token_expire_hours: int = Field(48, ge=1)
    reset_token_expire_minutes: int = Field(60, ge=1)
    require_email_verification: bool = True

    # --- email --------------------------------------------------------------
    app_base_url: str = "http://localhost:3000"  # base of the links put in emails
    cors_origins: str = ""  # comma-separated, e.g. "http://localhost:3000,https://app.example.com"
    email_backend: Literal["console", "smtp"] = "console"
    email_from: str = "Route Optimizer <no-reply@example.com>"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_starttls: bool = True

    # --- engine -------------------------------------------------------------
    matrix_backend: Literal["osrm", "haversine"] = "osrm"
    osrm_url: str = PUBLIC_OSRM_DEMO  # the public demo is for development only
    haversine_speed_kmh: float = Field(25.0, gt=0)
    solver_time_limit_s: int = Field(10, ge=1, le=60)
    max_stops: int = Field(50, ge=2, le=200)
    max_concurrent_solves: int = Field(2, ge=1)
    run_timeout_minutes: int = Field(10, ge=1)

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
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
