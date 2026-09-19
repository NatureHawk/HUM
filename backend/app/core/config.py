"""Application settings, loaded once from the environment.

Every default here is a *development* convenience. Nothing in production may
depend on one: `DATABASE_URL`, `REDIS_URL`, `SESSION_SECRET` and the allowed
origins all come from the environment, and the app refuses to start in
production without a real session secret.
"""

from __future__ import annotations

import os
import secrets
from functools import lru_cache
from typing import Literal

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_ORIGINS = ("http://localhost:3001", "http://127.0.0.1:3001")


class Settings(BaseSettings):
    """Every knob the backend reads. No module reaches into os.environ directly."""

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "production"] = Field(default="development")

    # postgresql+psycopg:// is psycopg 3. SQLAlchemy picks the sync or async
    # flavour from create_engine vs create_async_engine, so one URL serves both
    # the app (async) and Alembic (sync).
    #
    # Managed providers usually hand out `postgres://` or `postgresql://`; the
    # validator below rewrites those to the driver this app uses, so the value
    # can be pasted in unchanged.
    database_url: str = Field(
        default="postgresql+psycopg://syncboard:syncboard@localhost:5434/syncboard",
    )
    # Separate database for the test suite, so running tests can never touch
    # development data.
    test_database_url: str = Field(
        default="postgresql+psycopg://syncboard:syncboard@localhost:5434/syncboard_test",
    )

    # Managed Redis is usually `rediss://` (TLS). Both schemes work unchanged.
    redis_url: str = Field(default="redis://localhost:6380/0")

    # The deployed frontend's origin, e.g. https://syncboard.vercel.app. Kept
    # separate from `cors_origins` so the common case is one obvious variable.
    frontend_url: str | None = Field(default=None)

    # Extra origins, for preview deployments or a second domain.
    cors_origins: list[str] = Field(default_factory=list)

    # Signs the guest session cookie. MUST be set in production - see
    # get_settings(). Generated per-process in development so a fresh clone runs
    # without ceremony, at the cost of invalidating sessions on restart.
    session_secret: str = Field(default_factory=lambda: secrets.token_urlsafe(32))

    # Guest sessions are cheap and disposable, but a month is long enough that a
    # returning visitor keeps their name and their notes' authorship.
    session_max_age_seconds: int = Field(default=60 * 60 * 24 * 30)

    # Lightweight abuse ceilings for a public demo. Not a security platform -
    # just enough that one visitor cannot flatten the service.
    max_notes_per_board: int = Field(default=500)
    max_request_bytes: int = Field(default=64 * 1024)

    sql_echo: bool = Field(default=False)

    @field_validator("database_url", "test_database_url")
    @classmethod
    def _normalise_driver(cls, value: str) -> str:
        """Accept the URL shapes managed providers actually hand out.

        Render, Railway and Neon emit `postgres://` or `postgresql://`. Pasting
        either straight in would select psycopg2 (not installed) or fail
        outright, which is a miserable first five minutes of a deployment.
        """
        if value.startswith("postgres://"):
            return "postgresql+psycopg://" + value[len("postgres://") :]
        if value.startswith("postgresql://"):
            return "postgresql+psycopg://" + value[len("postgresql://") :]
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Allow a comma-separated list, which is what dashboards give you.

        pydantic-settings parses a list field as JSON, so `a,b` would otherwise
        be a startup crash rather than two origins.
        """
        if isinstance(value, str) and not value.strip().startswith("["):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def allowed_origins(self) -> list[str]:
        """Exactly which origins a browser may call this API from.

        Never "*": the session cookie travels with credentials, and the two
        cannot be combined. In development the local Next server is added
        automatically so nobody has to configure anything to run the app.
        """
        origins = list(self.cors_origins)
        if self.frontend_url:
            origins.append(self.frontend_url.rstrip("/"))
        if not self.is_production:
            origins.extend(DEV_ORIGINS)
        return sorted(set(origins))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached so the env is parsed once per process."""
    settings = Settings()

    if settings.is_production:
        # A generated secret would log every guest out on each restart, and
        # every replica would disagree about who anyone is. Fail loudly at
        # startup rather than mysteriously later.
        if not os.environ.get("SESSION_SECRET"):
            raise RuntimeError(
                "SESSION_SECRET must be set when ENVIRONMENT=production. "
                "Generate one with: "
                'python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
        if not settings.allowed_origins:
            raise RuntimeError(
                "FRONTEND_URL (or CORS_ORIGINS) must be set when "
                "ENVIRONMENT=production, or the browser will refuse every request."
            )
    return settings
