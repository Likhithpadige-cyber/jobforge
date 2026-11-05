"""The repository secret check must inspect tracked files, not installed tooling."""

import subprocess
from pathlib import Path

import pytest

from scripts import verify_scaffold

pytestmark = [pytest.mark.unit, pytest.mark.safety]


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)  # noqa: S603, S607 - fixed git command, temporary repository


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    git(tmp_path, "init")
    monkeypatch.setattr(verify_scaffold, "ROOT", tmp_path)
    monkeypatch.setattr(verify_scaffold, "results", [])
    return tmp_path


def fake_key() -> str:
    return "AK" + "IA" + "A" * 16


def test_installed_dependencies_and_local_env_are_not_committed(repository: Path) -> None:
    (repository / ".venv").mkdir()
    (repository / ".venv" / "example.py").write_text(fake_key())
    (repository / ".env").write_text(fake_key())
    (repository / "safe.txt").write_text("synthetic documentation")
    git(repository, "add", "safe.txt")
    verify_scaffold.check_no_committed_secrets()
    assert verify_scaffold.results[0][0] == verify_scaffold.PASS


def test_staged_ignored_secret_is_still_rejected(repository: Path) -> None:
    (repository / ".gitignore").write_text(".env\n")
    (repository / ".env").write_text(fake_key())
    git(repository, "add", "-f", ".env")
    verify_scaffold.check_no_committed_secrets()
    assert verify_scaffold.results[0][0] == verify_scaffold.FAIL
    assert ".env:1" in verify_scaffold.results[0][2]


def test_missing_git_metadata_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify_scaffold, "ROOT", tmp_path)
    monkeypatch.setattr(verify_scaffold, "results", [])
    verify_scaffold.check_no_committed_secrets()
    assert verify_scaffold.results[0][0] == verify_scaffold.FAIL


def test_script_filename_does_not_exempt_tracked_credentials(repository: Path) -> None:
    (repository / "verify_scaffold.py").write_text(fake_key())
    git(repository, "add", "verify_scaffold.py")
    verify_scaffold.check_no_committed_secrets()
    assert verify_scaffold.results[0][0] == verify_scaffold.FAIL
