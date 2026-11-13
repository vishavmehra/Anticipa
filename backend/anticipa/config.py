"""Application settings, read from the repo-root `.env` and process environment (env wins).

Secrets are `SecretStr` and are never logged or serialised. No credential has a default in code: the database
comes
from DATABASE_URL or, locally, from POSTGRES_PASSWORD (+ optional POSTGRES_USER/DB/HOST/PORT) in `.env`/the
environment.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

ROOT_DIR = Path(__file__).resolve().parents[2]  # repo root (contains .env) locally; /app in the container
ENV_FILE = ROOT_DIR / ".env"

DB_SCHEMA = (
    "anticipa"  # every app table lives here, never in `public` (Supabase exposes `public` via its Data API)
)


class ConfigError(RuntimeError):
    """Missing or inconsistent configuration (raised with an actionable message, never with a secret
    value)."""


def normalize_db_url(url: str) -> str:
    """Accept provider-style URLs (`postgres://`, `postgresql://`) and force the psycopg3 driver."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def prefer_ipv4_loopback(url: str | URL) -> str | URL:
    """`localhost` -> `127.0.0.1`. On Windows + Docker Desktop, `localhost` resolves to ::1 first, where the
    published
    Postgres port does not answer, so every new connection stalls ~10 s before falling back to IPv4."""
    parsed = make_url(url) if isinstance(url, str) else url
    return parsed.set(host="127.0.0.1") if parsed.host == "localhost" else url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    env: str = Field("local", validation_alias="ANTICIPA_ENV")
    log_level: str = Field("INFO", validation_alias="ANTICIPA_LOG_LEVEL")

    # --- database: DATABASE_URL (app/worker; Supabase session-pooler URL with sslmode=require in the cloud)
    # ---
    database_url: SecretStr | None = Field(None, validation_alias="DATABASE_URL")
    migration_database_url: SecretStr | None = Field(None, validation_alias="MIGRATION_DATABASE_URL")
    postgres_user: str = Field("anticipa", validation_alias="POSTGRES_USER")
    postgres_password: SecretStr | None = Field(None, validation_alias="POSTGRES_PASSWORD")
    postgres_db: str = Field("anticipa", validation_alias="POSTGRES_DB")
    postgres_host: str = Field("127.0.0.1", validation_alias="POSTGRES_HOST")
    postgres_port: int = Field(5432, validation_alias="POSTGRES_PORT")

    # --- Supabase Auth (ADR-058): bearer-token verification + the seed-users Admin API ---
    supabase_url: str | None = Field(None, validation_alias="SUPABASE_URL")
    supabase_publishable_key: SecretStr | None = Field(None, validation_alias="SUPABASE_PUBLISHABLE_KEY")
    supabase_secret_key: SecretStr | None = Field(None, validation_alias="SUPABASE_SECRET_KEY")
    demo_password: SecretStr | None = Field(
        None, validation_alias="ANTICIPA_DEMO_PASSWORD"
    )  # `anticipa seed-users` only
    # Password of the shareable read-only judge login. Keep it different from ANTICIPA_DEMO_PASSWORD (the
    # privileged demo users): the example-domain emails are public, so one shared password would let any
    # holder of the judge password sign in as the system admin.
    judge_password: SecretStr | None = Field(None, validation_alias="ANTICIPA_JUDGE_PASSWORD")
    profile_touch_seconds: float = Field(300.0, ge=0)  # throttle for user_profiles.last_seen_at upserts
    # Owner decision 2026-11-12: Google sign-ins without an explicit anticipa_role get read-only observer
    # access (synthetic data only). "" disables the fallback. Only "observer" is accepted.
    google_default_role: str = Field(
        "observer", validation_alias="ANTICIPA_GOOGLE_DEFAULT_ROLE", pattern="^(observer)?$"
    )

    # --- integrations (health only here; adapters live in their own modules) ---
    hapi_fhir_base_url: str = Field("http://127.0.0.1:8090/fhir", validation_alias="HAPI_FHIR_BASE_URL")
    anthropic_api_key: SecretStr | None = Field(None, validation_alias="ANTHROPIC_API_KEY")
    nebius_api_key: SecretStr | None = Field(None, validation_alias="NEBIUS_API_KEY")
    integration_timeout_seconds: float = Field(3.0, gt=0, le=30)

    # --- jobs ---
    job_poll_interval_seconds: float = Field(1.0, gt=0, validation_alias="ANTICIPA_JOB_POLL_SECONDS")
    job_heartbeat_seconds: float = Field(10.0, gt=0, validation_alias="ANTICIPA_JOB_HEARTBEAT_SECONDS")
    job_stale_after_seconds: float = Field(90.0, gt=0, validation_alias="ANTICIPA_JOB_STALE_SECONDS")
    job_backoff_base_seconds: float = Field(5.0, ge=0, validation_alias="ANTICIPA_JOB_BACKOFF_SECONDS")
    job_backoff_cap_seconds: float = Field(300.0, ge=0)
    # Run the job-claim loop inside the API process (single-service hosting, e.g. Render Free). Local dev uses
    # `anticipa worker`; running both is safe because claims use FOR UPDATE SKIP LOCKED.
    embedded_worker: bool = Field(False, validation_alias="ANTICIPA_EMBEDDED_WORKER")

    @model_validator(mode="after")
    def _finalise(self) -> Settings:
        if self.env != "local" and self.database_url is None:
            raise ValueError("DATABASE_URL is required when ANTICIPA_ENV != local")
        if self.supabase_url:
            self.supabase_url = self.supabase_url.strip().rstrip("/")
        return self

    @property
    def is_local(self) -> bool:
        return self.env == "local"

    @property
    def supabase_issuer(self) -> str | None:
        return f"{self.supabase_url}/auth/v1" if self.supabase_url else None

    @property
    def supabase_jwks_url(self) -> str | None:
        return f"{self.supabase_url}/auth/v1/.well-known/jwks.json" if self.supabase_url else None

    def sqlalchemy_url(self) -> str | URL:
        """Runtime (app/worker) URL for SQLAlchemy + psycopg3."""
        if self.database_url is not None:
            return prefer_ipv4_loopback(normalize_db_url(self.database_url.get_secret_value()))
        if self.postgres_password is None:
            raise ConfigError(
                "No database configured: set DATABASE_URL, or POSTGRES_PASSWORD for the local container "
                "(see .env.example)."
            )
        return URL.create(
            "postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )

    def migration_url(self) -> str | URL:
        """Alembic URL: MIGRATION_DATABASE_URL (e.g. Supabase direct/session connection), else the runtime
        URL."""
        if self.migration_database_url is not None:
            return prefer_ipv4_loopback(normalize_db_url(self.migration_database_url.get_secret_value()))
        return self.sqlalchemy_url()


@lru_cache
def get_settings() -> Settings:
    return Settings()
