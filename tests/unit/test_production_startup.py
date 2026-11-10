"""M20 step 1: deployed processes fail closed on bad secrets, without leaking them.

Each case starts a real interpreter with a production environment and only
synthetic canary values, then checks that the API module and the worker
entrypoint refuse to start before touching any database, and that nothing
they print contains a secret value. Nothing here can reach a network: no
database or Redis is listening on the configured ports, and replay mode
makes no provider calls.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.safety]

ROOT = Path(__file__).resolve().parents[2]
VALID_TOTP_KEY = base64.b64encode(bytes(range(32))).decode()
CANARIES = {
    "POSTGRES_PASSWORD": "CANARY-PG-a1b2",
    "SESSION_SECRET": "CANARY-SESSION-c3d4",
    "REDIS_PASSWORD": "CANARY-REDIS-e5f6",
    "S3_SECRET_ACCESS_KEY": "CANARY-S3-0a0b",
    "APP_API_PASSWORD": "CANARY-APPAPI-1c1d",
}


def _env(**overrides: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR")}
    env.update(
        ENVIRONMENT="production",
        LLM_MODE="replay",
        SUBMISSION_MODE="dry_run",
        EMAIL_TRANSPORT_MODE="fake",
        POSTGRES_HOST="127.0.0.1",
        POSTGRES_PORT="1",  # nothing listens here; a process that got this far fails
        REDIS_HOST="127.0.0.1",
        REDIS_PORT="1",
        WORKER_DATABASE_URL="postgresql+asyncpg://app_agent:CANARY-WORKER-DSN@127.0.0.1:1/x",
        PYTHONDONTWRITEBYTECODE="1",
        **CANARIES,
    )
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, our own modules
        [sys.executable, *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _assert_no_secret(result: subprocess.CompletedProcess[str]) -> None:
    output = result.stdout + result.stderr
    for value in [*CANARIES.values(), "CANARY-TOTP", "CANARY-WORKER-DSN", "CANARY-KEY"]:
        assert value not in output, f"{value} leaked into process output"


API_IMPORT = ["-c", "import apps.api.main"]
WORKER = ["-m", "workers.application"]

BAD_TOTP = {
    "missing": "",
    "whitespace": "   ",
    "not base64": "CANARY-TOTP-not-a-key",
    "16 bytes": base64.b64encode(bytes(16)).decode(),
}


@pytest.mark.parametrize("label", sorted(BAD_TOTP))
def test_api_refuses_to_start_without_a_valid_totp_key(label: str) -> None:
    result = _run(API_IMPORT, _env(TOTP_ENCRYPTION_KEY=BAD_TOTP[label]))
    assert result.returncode != 0
    assert "totp_encryption_key" in result.stderr
    _assert_no_secret(result)


@pytest.mark.parametrize("label", sorted(BAD_TOTP))
def test_worker_refuses_to_start_without_a_valid_totp_key(label: str) -> None:
    result = _run(WORKER, _env(TOTP_ENCRYPTION_KEY=BAD_TOTP[label]))
    assert result.returncode == 1
    # The worker logs only the error type (no traceback), before any connection.
    assert "worker.startup_failed" in result.stdout + result.stderr
    assert "ValidationError" in result.stdout + result.stderr
    _assert_no_secret(result)


def test_api_starts_in_replay_without_any_model_provider_key() -> None:
    """Positive control: live AI disabled needs no paid API key."""
    env = _env(TOTP_ENCRYPTION_KEY=VALID_TOTP_KEY)
    env.pop("ANTHROPIC_API_KEY", None)
    result = _run(
        [
            "-c",
            "import apps.api.main as m; s = m.get_settings(); "
            "print(s.environment, s.llm_mode, bool(s.anthropic_api_key))",
        ],
        env,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().endswith("production replay False")
    _assert_no_secret(result)


@pytest.mark.parametrize("mode", ["live", "record"])
def test_api_refuses_live_ai_without_a_key(mode: str) -> None:
    """Not weakened: enabling a provider-calling mode still needs the key."""
    result = _run(API_IMPORT, _env(TOTP_ENCRYPTION_KEY=VALID_TOTP_KEY, LLM_MODE=mode))
    assert result.returncode != 0
    assert "anthropic_api_key" in result.stderr
    _assert_no_secret(result)


def test_api_with_live_ai_and_a_key_passes_validation() -> None:
    env = _env(
        TOTP_ENCRYPTION_KEY=VALID_TOTP_KEY, LLM_MODE="live", ANTHROPIC_API_KEY="CANARY-KEY-zz"
    )
    result = _run(["-c", "import apps.api.main"], env)
    assert result.returncode == 0, result.stderr[-2000:]
    _assert_no_secret(result)
