"""Log redaction tests.

SECURITY.md §6.2: PII and secrets must never reach a log line. These tests
are the enforcement of that claim.
"""

from __future__ import annotations

import pytest

from packages.shared.logging import REDACTION, redact_processor

pytestmark = [pytest.mark.unit, pytest.mark.safety]


def _process(event: dict[str, object]) -> dict[str, object]:
    return redact_processor(None, "info", event)


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "api_key",
        "anthropic_api_key",
        "phone",
        "email",
        "address",
        "work_authorization",
        "session_secret",
        "authorization",
        "totp_secret_enc",
    ],
)
def test_sensitive_keys_are_redacted(key: str) -> None:
    result = _process({"event": "test", key: "sensitive-value"})
    assert result[key] == REDACTION
    assert "sensitive-value" not in str(result)


def test_nested_sensitive_keys_are_redacted() -> None:
    result = _process({"event": "saved", "profile": {"name": "Ada", "phone": "555-0100"}})
    assert result["profile"]["phone"] == REDACTION  # type: ignore[index]
    assert result["profile"]["name"] == "Ada"  # type: ignore[index]


def test_sensitive_keys_inside_lists_are_redacted() -> None:
    result = _process({"event": "batch", "items": [{"email": "a@b.test"}, {"ok": 1}]})
    assert "a@b.test" not in str(result)


def test_api_key_in_free_text_is_scrubbed() -> None:
    fake_key = "sk-ant-abcdefghijklmnopqrst"  # pragma: allowlist secret
    result = _process({"event": f"call failed with {fake_key}"})
    assert "abcdefghijklmnopqrst" not in str(result)


def test_bearer_token_in_free_text_is_scrubbed() -> None:
    result = _process({"event": "header", "raw": "Bearer eyJhbGciOiJIUzI1NiJ9abc"})
    assert "eyJhbGciOiJIUzI1NiJ9abc" not in str(result)


def test_database_url_password_is_scrubbed() -> None:
    result = _process(
        {"event": "connect", "dsn": "postgresql+asyncpg://user:supersecret@db:5432/jobforge"}
    )
    assert "supersecret" not in str(result)
    assert "db:5432" in str(result)


def test_non_sensitive_fields_survive_untouched() -> None:
    result = _process({"event": "job.evaluated", "job_id": "abc", "score": 78})
    assert result["job_id"] == "abc"
    assert result["score"] == 78
