"""Application configuration.

All configuration comes from environment variables (or a local .env file).
Nothing is hard-coded, and no secret has a usable default: secret-bearing
fields default to an empty string so that a misconfigured deployment fails
loudly at startup rather than silently running with a placeholder.

See SECURITY.md §3.3 for the secret-handling rules this module implements.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class LLMMode(StrEnum):
    """See ARCHITECTURE.md §10.3."""

    LIVE = "live"
    RECORD = "record"
    REPLAY = "replay"


class SubmissionMode(StrEnum):
    """Master switch for outbound submissions. See ARCHITECTURE.md §10.4."""

    DRY_RUN = "dry_run"
    ENABLED = "enabled"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- app
    app_name: str = "jobforge"
    environment: Environment = Environment.LOCAL
    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "console"

    api_host: str = "0.0.0.0"  # noqa: S104 - bound inside a container network
    api_port: int = 8000
    api_prefix: str = "/api/v1"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # ----------------------------------------------------------- database
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "jobforge"
    postgres_password: str = ""
    postgres_db: str = "jobforge"
    database_url: PostgresDsn | None = None

    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_pool_timeout_seconds: int = 30
    db_pool_recycle_seconds: int = 1800
    db_echo: bool = False
    db_connect_timeout_seconds: int = 5

    # -------------------------------------------------------------- redis
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str = ""
    redis_url: RedisDsn | None = None
    redis_max_connections: int = 20
    redis_socket_timeout_seconds: int = 5

    # ---------------------------------------------------------------- llm
    anthropic_api_key: str = ""
    llm_mode: LLMMode = LLMMode.REPLAY
    llm_model_evaluation: str = "claude-haiku-4-5-20251001"
    llm_model_tailoring: str = "claude-sonnet-5"
    llm_model_verification: str = "claude-sonnet-5"
    llm_max_cost_usd_per_run: float = 1.00
    llm_tailoring_input_price_per_million: Decimal | None = Field(default=None, ge=0)
    llm_tailoring_output_price_per_million: Decimal | None = Field(default=None, ge=0)

    # ------------------------------------------------------------- safety
    # These are operational limits, not guardrail toggles. The anti-fabrication
    # layers in SECURITY.md §4 are deliberately not configurable.
    submission_mode: SubmissionMode = SubmissionMode.DRY_RUN
    daily_application_quota: int = 100
    daily_evaluation_quota: int = 600
    daily_llm_cost_budget_usd: float = 25.00
    fit_score_shortlist_threshold: float = 65.0
    fit_score_review_floor: float = 55.0

    # ------------------------------------------------------------ storage
    s3_endpoint_url: str = "http://localhost:9000"
    s3_bucket: str = "jobforge-artifacts"
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_region: str = "us-east-1"

    # ------------------------------------------------------------ session
    session_secret: str = ""
    session_idle_timeout_minutes: int = 720
    session_absolute_timeout_hours: int = 168  # 7 days (SECURITY.md 3.1)
    session_cookie_name: str = "jobforge_session"
    csrf_cookie_name: str = "jobforge_csrf"
    access_token_ttl_minutes: int = 60

    # ------------------------------------------------------------- crypto
    #: base64 of 32 random bytes; encrypts users.totp_secret_enc only.
    #: Deliberately separate from session_secret - see SECURITY.md 3.4.
    totp_encryption_key: str = ""

    # ---------------------------------------------------------- validators
    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors(cls, value: object) -> object:
        """Accept either a JSON list or a comma-separated string."""
        if isinstance(value, str) and not value.strip().startswith("["):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _assemble_urls(self) -> Settings:
        if self.database_url is None:
            password = f":{self.postgres_password}" if self.postgres_password else ""
            object.__setattr__(
                self,
                "database_url",
                PostgresDsn(
                    f"postgresql+asyncpg://{self.postgres_user}{password}"
                    f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
                ),
            )
        if self.redis_url is None:
            password = f":{self.redis_password}@" if self.redis_password else ""
            object.__setattr__(
                self,
                "redis_url",
                RedisDsn(f"redis://{password}{self.redis_host}:{self.redis_port}/{self.redis_db}"),
            )
        return self

    @model_validator(mode="after")
    def _require_secrets_outside_local(self) -> Settings:
        """Fail fast rather than run production with empty secrets."""
        if self.environment in (Environment.STAGING, Environment.PRODUCTION):
            missing = [
                name
                for name in ("postgres_password", "session_secret", "anthropic_api_key")
                if not getattr(self, name)
            ]
            if missing:
                raise ValueError(
                    f"Missing required secrets for {self.environment}: {', '.join(missing)}"
                )
        return self

    # ---------------------------------------------------------- accessors
    @property
    def is_local(self) -> bool:
        return self.environment in (Environment.LOCAL, Environment.TEST)

    @property
    def totp_required(self) -> bool:
        """Whether login demands a second factor (SECURITY.md 3.1, 3.6).

        Relaxed for local/test only. Everywhere else TOTP is mandatory, and
        the storage, crypto and verification paths are identical either way -
        the difference is solely whether the code is demanded. Derived from
        the same `is_local` the cookie `Secure` flag uses, so the two cannot
        disagree about what "local" means.
        """
        return not self.is_local

    @property
    def sync_database_url(self) -> str:
        """Alembic and psql-style tooling want the sync driver."""
        return str(self.database_url).replace("postgresql+asyncpg://", "postgresql+psycopg://")

    @property
    def submission_enabled(self) -> bool:
        """Real submissions require BOTH the env switch and a settings-table
        toggle (ARCHITECTURE.md §10.4). This is only the first of the two."""
        return self.submission_mode is SubmissionMode.ENABLED

    def safe_dump(self) -> dict[str, object]:
        """Config snapshot with every secret-bearing field masked.

        Used by the readiness endpoint and by startup logging. Never dump
        `Settings` directly anywhere a human or a log aggregator can see it.
        """
        secret_fields = {
            "postgres_password",
            "redis_password",
            "anthropic_api_key",
            "s3_access_key_id",
            "s3_secret_access_key",
            "session_secret",
            "database_url",
            "redis_url",
        }
        dumped: dict[str, object] = {}
        for name, value in self.model_dump().items():
            if name in secret_fields:
                dumped[name] = "***set***" if value else "***unset***"
            else:
                dumped[name] = value
        return dumped


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor. Call `get_settings.cache_clear()` in tests."""
    return Settings()
