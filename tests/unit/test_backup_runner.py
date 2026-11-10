"""M21: backup bundle verification, retention planning and destination guards.

The end-to-end backup of a running stack is exercised in the rehearsal drill
(docs/verification/M21_RESTORE_DRILL.md); these tests need no containers.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from scripts import backup
from scripts import backup_crypto as bc

pytestmark = [pytest.mark.unit, pytest.mark.safety]


@pytest.fixture(scope="module")
def public(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    d = tmp_path_factory.mktemp("k")
    return bc.load_public(bc.generate_keypair(d / "k", d / "p", b"synthetic passphrase"))


def _bundle(root: Path, name: str, public) -> Path:  # type: ignore[no-untyped-def]
    bundle = root / name
    bundle.mkdir(parents=True)
    files = {}
    for file in backup.FILES:
        out = io.BytesIO()
        bc.encrypt_stream(io.BytesIO(b"synthetic " + file.encode()), out, public, file)
        (bundle / f"{file}.jfenc").write_bytes(out.getvalue())
        files[file] = {"ciphertext_sha256": hashlib.sha256(out.getvalue()).hexdigest()}
    manifest = {"alembic_revision": "0011_application_tracking", "row_counts": {}, "files": files}
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    return bundle


def test_verify_accepts_an_intact_bundle(tmp_path: Path, public) -> None:  # type: ignore[no-untyped-def]
    bundle = _bundle(tmp_path, "jobforge-backup-20261110T010000Z", public)
    assert backup.verify(bundle)["alembic_revision"] == "0011_application_tracking"


def test_verify_rejects_partial_tampered_and_truncated_bundles(tmp_path: Path, public) -> None:  # type: ignore[no-untyped-def]
    bundle = _bundle(tmp_path, "b", public)
    (bundle / "manifest.json").unlink()
    with pytest.raises(backup.BackupError, match="no manifest"):
        backup.verify(bundle)

    bundle = _bundle(tmp_path, "c", public)
    target = bundle / "database.dump.jfenc"
    data = bytearray(target.read_bytes())
    data[-3] ^= 1
    target.write_bytes(bytes(data))
    with pytest.raises(backup.BackupError, match="recorded hash"):
        backup.verify(bundle)

    bundle = _bundle(tmp_path, "d", public)
    (bundle / "artifacts.tar.jfenc").unlink()
    with pytest.raises(backup.BackupError, match="missing"):
        backup.verify(bundle)


def test_retention_keeps_daily_then_weekly_and_ignores_other_names() -> None:
    names = [f"jobforge-backup-202611{d:02d}T0{h}0000Z" for d in range(1, 31) for h in (1, 2)]
    names += ["manual-copy", ".partial-jobforge-backup-20261130T030000Z", "jobforge-backup-garbage"]
    plan = backup.retention_plan(names)
    # Newest bundle of each of the last 7 days ...
    assert plan.keep[:7] == [f"jobforge-backup-202611{d:02d}T020000Z" for d in range(30, 23, -1)]
    # ... then one per older ISO week, at most four.
    assert len(plan.keep) == 7 + 4
    assert len({backup._bundle_date(n).isocalendar()[:2] for n in plan.keep[7:]}) == 4  # type: ignore[union-attr]
    for untouched in (
        "manual-copy",
        ".partial-jobforge-backup-20261130T030000Z",
        "jobforge-backup-garbage",
    ):
        assert untouched not in plan.delete and untouched not in plan.keep
    assert set(plan.keep).isdisjoint(plan.delete)
    assert len(plan.keep) + len(plan.delete) == 60


def test_retention_is_a_plan_unless_applied(tmp_path: Path, public) -> None:  # type: ignore[no-untyped-def]
    for day in range(1, 11):
        _bundle(tmp_path, f"jobforge-backup-202611{day:02d}T010000Z", public)
    plan = backup.retention(tmp_path, apply=False)
    assert plan.delete and all((tmp_path / n).exists() for n in plan.delete)


def test_retention_never_prunes_when_the_newest_bundle_does_not_verify(
    tmp_path: Path, public
) -> None:  # type: ignore[no-untyped-def]
    for day in range(1, 11):
        _bundle(tmp_path, f"jobforge-backup-202611{day:02d}T010000Z", public)
    (tmp_path / "jobforge-backup-20261110T010000Z" / "globals.sql.jfenc").write_bytes(b"corrupt")
    with pytest.raises(backup.BackupError):
        backup.retention(tmp_path, apply=True)
    assert len(list(tmp_path.iterdir())) == 10  # nothing deleted


def test_retention_apply_deletes_only_planned_bundles(tmp_path: Path, public) -> None:  # type: ignore[no-untyped-def]
    for day in range(1, 11):
        _bundle(tmp_path, f"jobforge-backup-202611{day:02d}T010000Z", public)
    (tmp_path / "notes.txt").write_text("owner notes")
    plan = backup.retention(tmp_path, apply=True)
    remaining = {p.name for p in tmp_path.iterdir()}
    assert remaining == set(plan.keep) | {"notes.txt"}


def test_destination_must_be_outside_the_repository() -> None:
    with pytest.raises(backup.BackupError, match="outside the repository"):
        backup._safe_dest(backup.ROOT / "backups")


def test_cli_reports_errors_as_json_without_tracebacks(capsys: pytest.CaptureFixture[str]) -> None:
    assert backup.main(["verify", "/nonexistent/jobforge-backup-x"]) == 2
    err = capsys.readouterr().err
    assert json.loads(err)["ok"] is False and "Traceback" not in err
