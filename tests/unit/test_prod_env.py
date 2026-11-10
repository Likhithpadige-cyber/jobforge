"""M20 step 3: scripts/prod_env.py - env generation and preflight checks.

Pure checks only; the database checks run in
tests/integration/test_prod_preflight.py against the throwaway server.
"""

from __future__ import annotations

import base64
import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from packages.shared.config import Settings, totp_key_is_valid
from scripts import prod_env

pytestmark = [pytest.mark.unit, pytest.mark.safety]

ROOT = Path(__file__).resolve().parents[2]


def _prod(**over: object) -> Settings:
    values: dict[str, object] = {
        "environment": "production",
        "postgres_password": "synthetic",
        "session_secret": "s" * 48,
        "totp_encryption_key": base64.b64encode(bytes(32)).decode(),
        "artifact_storage_backend": "filesystem",
        "cors_origins": ["http://127.0.0.1:8480"],
    }
    values.update(over)
    return Settings(_env_file=None, **values)


def _parse(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if line and not line.startswith("#")
    )


def test_generated_env_is_private_complete_and_dsn_safe(tmp_path: Path) -> None:
    target = tmp_path / "jobforge" / "prod.env"
    names = prod_env.generate_env(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    values = _parse(target)
    assert set(names) == set(values)
    assert values["ENVIRONMENT"] == "production" and values["LLM_MODE"] == "replay"
    assert values["ANTHROPIC_API_KEY"] == "" and values["SUBMISSION_MODE"] == "dry_run"
    assert values["POSTGRES_DB"] == prod_env.PROD_DATABASE
    assert totp_key_is_valid(values["TOTP_ENCRYPTION_KEY"])
    secret_names = [k for k in values if k.endswith("_PASSWORD") or k == "SESSION_SECRET"]
    assert len({values[k] for k in secret_names}) == len(secret_names)  # all distinct
    for name in secret_names:
        assert len(values[name]) >= 32
        assert not set(values[name]) & set(":@/?#%"), name


def test_generated_env_is_accepted_by_settings_and_the_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "prod.env"
    prod_env.generate_env(target)
    for key, value in _parse(target).items():
        monkeypatch.setenv(key, value)
    settings = Settings(_env_file=None)
    assert all(c.ok for c in prod_env.config_checks(settings) if c.name != "artifact_backend")
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR")}
    result = subprocess.run(  # noqa: S603 - our own script
        ["/bin/bash", str(ROOT / "scripts" / "prod.sh"), "start", "--help-not-run"],
        cwd=ROOT,
        env={
            **env,
            "JOBFORGE_PROD_ENV_FILE": str(target),
            "JOBFORGE_RELEASE": "test",
            "DOCKER_HOST": "unix:///nonexistent.sock",
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    # Every wrapper validation passed; only docker (pointed nowhere) failed.
    assert "refusing" not in result.stderr, result.stderr


def test_generated_env_loads_through_pydantic_settings_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (found in the M20 rehearsal): values must parse exactly as
    the containers read them - from environment variables, not kwargs."""
    target = tmp_path / "prod.env"
    prod_env.generate_env(target)
    for key, value in _parse(target).items():
        monkeypatch.setenv(key, value)
    settings = Settings(_env_file=None)
    assert settings.cors_origins == ["http://127.0.0.1:8480"]
    assert settings.environment.value == "production"


def test_generate_env_refuses_unsafe_destinations(tmp_path: Path) -> None:
    with pytest.raises(prod_env.ProdGuardError, match="outside the repository"):
        prod_env.generate_env(ROOT / "prod.env")
    existing = tmp_path / "prod.env"
    existing.write_text("keep me\n")
    with pytest.raises(prod_env.ProdGuardError, match="overwrite"):
        prod_env.generate_env(existing)
    assert existing.read_text() == "keep me\n"


def test_cli_never_prints_generated_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "prod.env"
    assert prod_env.main(["generate-env", str(target)]) == 0
    printed = capsys.readouterr()
    for name, value in _parse(target).items():
        if len(value) >= 16:
            assert value not in printed.out + printed.err, name


def test_config_checks_pass_for_the_safe_production_shape() -> None:
    checks = {c.name: c for c in prod_env.config_checks(_prod())}
    assert all(c.ok for c in checks.values()), [c for c in checks.values() if not c.ok]


@pytest.mark.parametrize(
    ("over", "failing"),
    [
        ({"session_secret": "short-but-present"}, "session_secret_strength"),
        ({"cors_origins": ["http://jobforge.example"]}, "cors_origins"),
        ({"artifact_storage_backend": "s3"}, "artifact_backend"),
        ({"llm_mode": "live", "anthropic_api_key": "synthetic-key"}, "live_ai_disabled"),
        ({"email_transport_mode": "smtp"}, "email_fake"),
    ],
)
def test_config_checks_fail_closed(over: dict[str, object], failing: str) -> None:
    checks = {c.name: c for c in prod_env.config_checks(_prod(**over))}
    assert checks[failing].ok is False and checks[failing].fatal is True


def test_artifact_root_must_be_private_and_writable(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    assert not prod_env.artifact_root_checks(root)[0].ok  # missing
    root.mkdir(mode=0o700)
    assert all(c.ok for c in prod_env.artifact_root_checks(root))
    root.chmod(0o755)
    assert not {c.name: c for c in prod_env.artifact_root_checks(root)}["artifact_root_private"].ok
    root.chmod(0o700)


def test_unverifiable_objects_counts_missing_mismatched_and_escaping_keys(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    (root / "resumes").mkdir(parents=True)
    good = b"%PDF synthetic"
    (root / "resumes" / "a.pdf").write_bytes(good)
    (root / "resumes" / "b.pdf").write_bytes(b"tampered")
    outside = tmp_path / "secret.txt"
    outside.write_bytes(good)
    digest = hashlib.sha256(good).hexdigest()
    rows = [
        ("resumes/a.pdf", digest),  # ok
        ("resumes/b.pdf", digest),  # mismatched
        ("resumes/missing.pdf", digest),  # missing
        ("../secret.txt", digest),  # escapes the root: never trusted
    ]
    assert prod_env.unverifiable_objects(rows, root) == 3
    assert prod_env.unverifiable_objects(rows[:1], root) == 0


def test_backup_readiness_is_a_warning_until_m21(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOBFORGE_LAST_VERIFIED_BACKUP", raising=False)
    check = prod_env.backup_check()
    assert check.ok is False and check.fatal is False


def test_preflight_cli_fails_without_tracebacks_or_values() -> None:
    """Run as a real process (as in the container): a bad TOTP key stops the
    preflight before any check, naming the setting but no value."""
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR")}
    env.update(
        ENVIRONMENT="production",
        POSTGRES_PASSWORD="CANARY-PREFLIGHT-PG",
        SESSION_SECRET="CANARY-PREFLIGHT-SESSION",
        TOTP_ENCRYPTION_KEY="CANARY-PREFLIGHT-bad",
    )
    result = subprocess.run(  # noqa: S603 - our own module
        [sys.executable, "-m", "scripts.prod_env", "preflight"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 1
    out = result.stdout + result.stderr
    assert "Traceback" not in out and "CANARY-PREFLIGHT" not in out
    assert "preflight failed: invalid configuration" in result.stderr
    assert "totp_encryption_key" in result.stderr
