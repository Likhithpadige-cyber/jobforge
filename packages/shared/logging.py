"""Structured logging with PII redaction.

SECURITY.md §6.2 requires an allowlist-based redaction processor. This module
strips known-sensitive keys from every log event before it is emitted, so a
careless `log.info("saved", profile=profile_dict)` cannot leak a phone number.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any, cast

import structlog

# Keys whose values are never logged, at any level, from anywhere.
REDACTED_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "password_hash",
        "totp_secret",
        "totp_secret_enc",
        "secret",
        "session_secret",
        "totp_encryption_key",
        "totp_uri",
        "provisioning_uri",
        "csrf_token",
        "session_id",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "anthropic_api_key",
        "authorization",
        "cookie",
        "set_cookie",
        "phone",
        "phone_number",
        "email",
        "address",
        "street_address",
        "date_of_birth",
        "dob",
        "ssn",
        "work_authorization",
        "salary_history",
        "credential_id",
        "s3_secret_access_key",
        "postgres_password",
        "redis_password",
    }
)

REDACTION = "***redacted***"

# Defence in depth: catch secrets that arrive inside free-text messages.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "sk-***redacted***"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{12,}"), "Bearer ***redacted***"),
    (
        re.compile(r"(?i)(postgresql|postgres|redis)(\+\w+)?://[^\s\"']*:[^\s\"'@]+@"),
        r"\1\2://***redacted***@",
    ),
)


def _scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        scrubbed = value
        for pattern, replacement in _PATTERNS:
            scrubbed = pattern.sub(replacement, scrubbed)
        return scrubbed
    if isinstance(value, dict):
        return {
            k: (REDACTION if k.lower() in REDACTED_KEYS else _scrub_value(v))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_scrub_value(item) for item in value]
    return value


def redact_processor(
    _logger: Any, _name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """structlog processor: drop or scrub sensitive fields."""
    result: dict[str, Any] = {}
    for key, value in event_dict.items():
        if key.lower() in REDACTED_KEYS:
            result[key] = REDACTION
        else:
            result[key] = _scrub_value(value)
    return result


def configure_logging(level: str = "INFO", fmt: str = "console") -> None:
    """Configure structlog and the stdlib root logger to match."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        redact_processor,
    ]
    if fmt == "json":
        processors += [
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    # structlog's dynamically configured factory erases the logger type.
    # Quote the type so no runtime module lookup is added to the factory call.
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name))
