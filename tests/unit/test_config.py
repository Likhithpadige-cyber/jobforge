"""Configuration management tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from packages.shared.config import Environment, LLMMode, Settings, SubmissionMode, get_settings

pytestmark = pytest.mark.unit


def test_defaults_are_safe() -> None:
    """The out-of-the-box configuration must not be able to submit anything
    or make a live LLM call."""
    settings = Settings()
    assert settings.submission_mode is SubmissionMode.DRY_RUN
    assert settings.submission_enabled is False
    assert settings.llm_mode is LLMMode.REPLAY
    assert settings.daily_application_quota == 100


def test_no_secret_has_a_usable_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """No secret-bearing field may have a usable DEFAULT.

    The environment has to be cleared first: pydantic-settings reads the real
    process environment and .env, so inside the container this would otherwise
    assert against the configured value rather than the default.
    """
    for name in (
        "ANTHROPIC_API_KEY",
        "POSTGRES_PASSWORD",
        "SESSION_SECRET",
        "S3_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    # _env_file=None, or .env is re-read and undoes the delenv above.
    settings = Settings(_env_file=None)
    assert settings.anthropic_api_key == ""
    assert settings.postgres_password == ""
    assert settings.session_secret == ""
    assert settings.s3_secret_access_key == ""


def test_database_url_is_assembled_from_parts() -> None:
    settings = Settings(
        postgres_host="db",
        postgres_port=5432,
        postgres_user="jobforge",
        postgres_password="localdev",
        postgres_db="jobforge",
    )
    url = str(settings.database_url)
    assert url.startswith("postgresql+asyncpg://")
    assert "@db:5432/jobforge" in url


def test_explicit_database_url_wins() -> None:
    settings = Settings(database_url="postgresql+asyncpg://u:p@example:5555/other")
    assert "example:5555" in str(settings.database_url)


def test_sync_database_url_uses_psycopg_driver() -> None:
    settings = Settings(postgres_host="db")
    assert settings.sync_database_url.startswith("postgresql+psycopg://")


def test_redis_url_is_assembled() -> None:
    settings = Settings(redis_host="cache", redis_port=6379, redis_db=2)
    assert str(settings.redis_url).startswith("redis://cache:6379")


def test_cors_origins_accepts_comma_separated_string() -> None:
    settings = Settings(cors_origins="http://a.test, http://b.test")
    assert settings.cors_origins == ["http://a.test", "http://b.test"]


def test_production_requires_secrets() -> None:
    with pytest.raises(PydanticValidationError) as exc:
        Settings(environment=Environment.PRODUCTION)
    assert "Missing required secrets" in str(exc.value)


def test_production_accepts_complete_secrets() -> None:
    settings = Settings(
        environment=Environment.PRODUCTION,
        postgres_password="x",
        session_secret="y",
        anthropic_api_key="z",
    )
    assert settings.environment is Environment.PRODUCTION
    assert settings.is_local is False


def test_safe_dump_masks_every_secret() -> None:
    settings = Settings(postgres_password="hunter2", anthropic_api_key="sk-real-key")
    dumped = settings.safe_dump()
    serialised = str(dumped)
    assert "hunter2" not in serialised
    assert "sk-real-key" not in serialised
    assert dumped["postgres_password"] == "***set***"
    assert dumped["session_secret"] == "***unset***"


def test_environment_overrides_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAILY_APPLICATION_QUOTA", "42")
    monkeypatch.setenv("FIT_SCORE_SHORTLIST_THRESHOLD", "80.5")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.daily_application_quota == 42
    assert settings.fit_score_shortlist_threshold == 80.5


def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()
    assert get_settings() is get_settings()
