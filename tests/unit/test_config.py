"""Configuration management tests."""

from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError as PydanticValidationError

from packages.shared.config import (
    EmailTransportMode,
    Environment,
    LLMMode,
    Settings,
    SubmissionMode,
    get_settings,
)

pytestmark = pytest.mark.unit


def test_defaults_are_safe() -> None:
    """The out-of-the-box configuration must not be able to submit anything
    or make a live LLM call."""
    settings = Settings()
    assert settings.submission_mode is SubmissionMode.DRY_RUN
    assert settings.submission_enabled is False
    assert settings.llm_mode is LLMMode.REPLAY
    assert settings.daily_application_quota == 100


def test_email_transport_mode_defaults_to_fake() -> None:
    """M11 Phase 1: never opens a socket unless explicitly configured to."""
    assert Settings().email_transport_mode is EmailTransportMode.FAKE


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
        totp_encryption_key=VALID_TOTP_KEY,
    )
    assert settings.environment is Environment.PRODUCTION
    assert settings.is_local is False


# ------------------------------------------------ M20 step 1: deployment secrets
#: Synthetic key material only; never a real deployment's key.
VALID_TOTP_KEY = base64.b64encode(bytes(range(32))).decode()
BASE = {"postgres_password": "pg-synthetic", "session_secret": "session-synthetic"}
DEPLOYED = (Environment.STAGING, Environment.PRODUCTION)


def _deployed(environment: Environment, **overrides: object) -> Settings:
    # _env_file=None: the developer's .env must not decide these outcomes.
    return Settings(_env_file=None, environment=environment, **{**BASE, **overrides})


@pytest.fixture(autouse=False)
def clean_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ANTHROPIC_API_KEY", "TOTP_ENCRYPTION_KEY", "LLM_MODE", "SESSION_SECRET"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.usefixtures("clean_secret_env")
@pytest.mark.parametrize("environment", DEPLOYED)
def test_replay_deployment_needs_no_model_provider_key(environment: Environment) -> None:
    """Live AI disabled (replay) makes no provider calls, so no paid key."""
    settings = _deployed(environment, totp_encryption_key=VALID_TOTP_KEY)
    assert settings.llm_mode is LLMMode.REPLAY
    assert settings.anthropic_api_key == ""


@pytest.mark.usefixtures("clean_secret_env")
@pytest.mark.parametrize("environment", DEPLOYED)
@pytest.mark.parametrize("mode", [LLMMode.LIVE, LLMMode.RECORD])
@pytest.mark.parametrize("key", ["", "   ", "\t\n"])
def test_provider_calling_modes_still_require_the_key(
    environment: Environment, mode: LLMMode, key: str
) -> None:
    """Not weakened: live and record call the provider and need a real key."""
    with pytest.raises(PydanticValidationError, match="anthropic_api_key"):
        _deployed(
            environment, llm_mode=mode, anthropic_api_key=key, totp_encryption_key=VALID_TOTP_KEY
        )


@pytest.mark.usefixtures("clean_secret_env")
@pytest.mark.parametrize("mode", [LLMMode.LIVE, LLMMode.RECORD])
def test_provider_calling_modes_accept_a_key(mode: LLMMode) -> None:
    settings = _deployed(
        Environment.PRODUCTION,
        llm_mode=mode,
        anthropic_api_key="test-synthetic-key",
        totp_encryption_key=VALID_TOTP_KEY,
    )
    assert settings.llm_mode is mode


@pytest.mark.usefixtures("clean_secret_env")
@pytest.mark.parametrize("environment", DEPLOYED)
@pytest.mark.parametrize("missing", ["postgres_password", "session_secret", "totp_encryption_key"])
@pytest.mark.parametrize("blank", ["", "  "])
def test_core_secrets_are_required_even_in_replay(
    environment: Environment, missing: str, blank: str
) -> None:
    values = {**BASE, "totp_encryption_key": VALID_TOTP_KEY, missing: blank}
    with pytest.raises(PydanticValidationError, match=f"Missing required secrets.*{missing}"):
        Settings(_env_file=None, environment=environment, **values)


INVALID_TOTP_KEYS = {
    "not base64": "not-a-key!!",
    "16 bytes": base64.b64encode(bytes(16)).decode(),
    "31 bytes": base64.b64encode(bytes(31)).decode(),
    "33 bytes": base64.b64encode(bytes(33)).decode(),
    "64 bytes": base64.b64encode(bytes(64)).decode(),
    "urlsafe alphabet": base64.urlsafe_b64encode(b"\xfb" * 32).decode(),
    "missing padding": VALID_TOTP_KEY.rstrip("="),
    "embedded newline": VALID_TOTP_KEY[:20] + "\n" + VALID_TOTP_KEY[20:],
    "hex instead of base64": bytes(32).hex(),
}


@pytest.mark.usefixtures("clean_secret_env")
@pytest.mark.parametrize("environment", DEPLOYED)
@pytest.mark.parametrize("label", sorted(INVALID_TOTP_KEYS))
def test_invalid_totp_key_fails_startup(environment: Environment, label: str) -> None:
    with pytest.raises(PydanticValidationError, match="totp_encryption_key is invalid"):
        _deployed(environment, totp_encryption_key=INVALID_TOTP_KEYS[label])


def test_totp_key_rule_matches_the_crypto_layer() -> None:
    """The startup check and the encrypt/decrypt path agree on every key."""
    from packages.core.crypto import totp_secret
    from packages.shared.config import TOTP_KEY_BYTES, totp_key_is_valid

    assert TOTP_KEY_BYTES == totp_secret._KEY_BYTES
    for key in [VALID_TOTP_KEY, totp_secret.generate_encryption_key(), *INVALID_TOTP_KEYS.values()]:
        try:
            totp_secret.encrypt_totp_secret("JBSWY3DPEHPK3PXP", key)
            usable = True
        except totp_secret.MissingEncryptionKeyError:
            usable = False
        assert totp_key_is_valid(key) is usable, key


@pytest.mark.usefixtures("clean_secret_env")
@pytest.mark.parametrize("environment", [Environment.LOCAL, Environment.TEST])
def test_local_environments_are_unchanged(environment: Environment) -> None:
    """Local/test still start without deployment secrets (crypto fails closed
    at use time instead), so development and the test suite are unaffected."""
    settings = Settings(_env_file=None, environment=environment, llm_mode=LLMMode.LIVE)
    assert settings.totp_encryption_key == ""


@pytest.mark.usefixtures("clean_secret_env")
def test_startup_errors_never_echo_secret_values() -> None:
    """A failed startup prints which setting is wrong, never any value."""
    canaries = {
        "postgres_password": "CANARY-PG-7f3a",
        "session_secret": "CANARY-SESSION-91bc",
        "anthropic_api_key": "CANARY-ANTHROPIC-2d4e",
        "s3_secret_access_key": "CANARY-S3-55aa",
        "redis_password": "CANARY-REDIS-0c1d",
    }
    for bad_totp in ("", "CANARY-TOTP-not-base64"):
        with pytest.raises(PydanticValidationError) as exc:
            Settings(
                _env_file=None,
                environment=Environment.PRODUCTION,
                llm_mode=LLMMode.LIVE,
                totp_encryption_key=bad_totp,
                **canaries,
            )
        # What a crashed process prints (str/repr in the traceback), plus the
        # structured form without input. `.errors()` with input is an explicit
        # opt-in that no startup path calls.
        rendered = f"{exc.value}\n{exc.value!r}\n{exc.value.errors(include_input=False)}"
        for value in [*canaries.values(), "CANARY-TOTP"]:
            assert value not in rendered
        assert "totp_encryption_key" in rendered


@pytest.mark.usefixtures("clean_secret_env")
def test_secrets_from_the_environment_are_not_echoed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("POSTGRES_PASSWORD", "CANARY-ENV-PG-4411")
    monkeypatch.setenv("SESSION_SECRET", "CANARY-ENV-SESSION-8822")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", "CANARY-ENV-TOTP")
    with pytest.raises(PydanticValidationError) as exc:
        Settings(_env_file=None)
    assert "CANARY-ENV" not in f"{exc.value}{exc.value!r}{exc.value.errors(include_input=False)}"


def test_safe_dump_masks_every_secret() -> None:
    settings = Settings(postgres_password="hunter2", anthropic_api_key="sk-real-key")
    dumped = settings.safe_dump()
    serialised = str(dumped)
    assert "hunter2" not in serialised
    assert "sk-real-key" not in serialised
    assert dumped["postgres_password"] == "***set***"
    assert dumped["session_secret"] == "***unset***"


def test_safe_dump_masks_smtp_credentials() -> None:
    """M11 Phase 1: the SMTP credential must never appear in a config dump."""
    settings = Settings(
        smtp_username="mailer",
        smtp_password="s3cr3t-smtp-pw",  # pragma: allowlist secret
    )
    dumped = settings.safe_dump()
    serialised = str(dumped)
    assert "s3cr3t-smtp-pw" not in serialised
    assert dumped["smtp_username"] == "***set***"
    assert dumped["smtp_password"] == "***set***"


def test_safe_dump_reports_unset_smtp_credentials() -> None:
    settings = Settings(smtp_username="", smtp_password="")
    dumped = settings.safe_dump()
    assert dumped["smtp_password"] == "***unset***"


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


def test_submission_retry_defaults() -> None:
    settings = Settings()
    assert settings.submission_max_attempts == 3
    assert settings.submission_retry_delays_seconds == (60, 300)


def test_submission_retry_single_attempt_valid() -> None:
    settings = Settings(submission_max_attempts=1, submission_retry_delays_seconds=())
    assert settings.submission_max_attempts == 1
    assert settings.submission_retry_delays_seconds == ()


def test_submission_retry_validation_invalid_values() -> None:
    # max_attempts < 1
    with pytest.raises(PydanticValidationError, match="submission_max_attempts must be >= 1"):
        Settings(submission_max_attempts=0, submission_retry_delays_seconds=())

    # negative delays
    with pytest.raises(
        PydanticValidationError, match="submission_retry_delays_seconds must be non-negative"
    ):
        Settings(submission_max_attempts=2, submission_retry_delays_seconds=(-10,))

    # length mismatch
    with pytest.raises(PydanticValidationError, match="must equal submission_max_attempts - 1"):
        Settings(submission_max_attempts=3, submission_retry_delays_seconds=(60,))
