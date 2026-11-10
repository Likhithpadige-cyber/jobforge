"""Encrypted backups of a running JobForge private runtime (M21).

    python -m scripts.backup create --project jobforge-prod --public-key PUB --dest DIR
    python -m scripts.backup verify BUNDLE
    python -m scripts.backup retention --dest DIR [--apply]

`create` streams from the running containers straight into the encryptor,
so no plaintext ever touches the host disk:
  - globals.sql   pg_dumpall --globals-only (roles; contains password hashes)
  - database.dump pg_dump --format=custom
  - artifacts.tar the artifact volume, read from the api container
plus a plaintext manifest (sizes, hashes, revision, row counts - no personal
data). The bundle is written under `.partial-…` and renamed only after every
file verifies, so an interrupted run never looks like a backup.

`retention` only prints a plan unless `--apply` is given (deleting backups is
a decision for the owner). Nothing here schedules itself; a launchd job is a
separately approved step. No command prints a secret.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import IO, Any

from scripts import backup_crypto as bc

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_ROOT = "/var/lib/jobforge/artifacts"
FILES = ("globals.sql", "database.dump", "artifacts.tar")
COUNTED_TABLES = (
    "users",
    "profiles",
    "profile_facts",
    "jobs",
    "applications",
    "artifacts",
    "resumes",
)
BUNDLE_PREFIX = "jobforge-backup-"
DOCKER = shutil.which("docker") or "docker"


class BackupError(RuntimeError):
    """A refusal or failure; the message never contains a secret."""


# ------------------------------------------------------------------ docker
def container(project: str, service: str) -> str:
    out = subprocess.run(  # noqa: S603 - fixed argv
        [
            DOCKER,
            "ps",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            f"label=com.docker.compose.service={service}",
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    if len(out) != 1:
        raise BackupError(
            f"expected one running {service} container in {project}, found {len(out)}"
        )
    return out[0]


def _exec_text(cid: str, script: str) -> str:
    result = subprocess.run(  # noqa: S603
        [DOCKER, "exec", cid, "sh", "-c", script], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise BackupError(f"container command failed ({result.returncode})")
    return result.stdout.strip()


def _psql(pg: str, sql: str) -> str:
    return _exec_text(pg, f'psql -tA -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "{sql}"')


# ------------------------------------------------------------------ helpers
class _Hashing:
    """Wraps a stream: hashes and counts plaintext as the encryptor reads it."""

    def __init__(self, raw: IO[bytes]) -> None:
        self.raw, self.sha, self.size, self.head = raw, hashlib.sha256(), 0, b""

    def read(self, n: int) -> bytes:
        block = self.raw.read(n)
        if block:
            self.sha.update(block)
            self.size += len(block)
            if len(self.head) < 8:
                self.head = (self.head + block)[:8]
        return block


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _safe_dest(dest: Path) -> Path:
    dest = dest.expanduser().resolve()
    if dest.is_relative_to(ROOT):
        raise BackupError("the backup destination must be outside the repository")
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    return dest


def _stream_encrypt(argv: list[str], target: Path, public: Any, label: str) -> dict[str, Any]:
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)  # noqa: S603
    assert proc.stdout is not None
    hashing = _Hashing(proc.stdout)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        bc.encrypt_stream(hashing, out, public, label)
        out.flush()
        os.fsync(out.fileno())
    proc.wait()
    if proc.returncode != 0:
        raise BackupError(f"{label}: source command failed ({proc.returncode})")
    return {
        "plaintext_sha256": hashing.sha.hexdigest(),
        "plaintext_bytes": hashing.size,
        "head": hashing.head,
    }


# ------------------------------------------------------------------ create
def create(
    project: str, public_key_path: Path, dest: Path, *, min_free_factor: float = 2.0
) -> Path:
    if not project.startswith("jobforge-"):
        raise BackupError("unexpected project name")
    public_text = public_key_path.read_text()
    public = bc.load_public(public_text)
    dest = _safe_dest(dest)
    pg, api = container(project, "postgres"), container(project, "api")

    db_bytes = int(_psql(pg, "SELECT pg_database_size(current_database())") or 0)
    art_bytes = int(_exec_text(api, f"du -sb {ARTIFACT_ROOT} | cut -f1") or 0)
    free = shutil.disk_usage(dest).free
    needed = int((db_bytes + art_bytes) * min_free_factor) + 64 * 1024 * 1024
    if free < needed:
        raise BackupError(f"not enough free space at the destination ({free} < {needed} bytes)")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    final = dest / f"{BUNDLE_PREFIX}{stamp}"
    partial = dest / f".partial-{BUNDLE_PREFIX}{stamp}"
    if final.exists():
        raise BackupError("a bundle with this timestamp already exists; not overwriting")
    partial.mkdir(mode=0o700)
    try:
        sources = {
            "globals.sql": [
                DOCKER,
                "exec",
                pg,
                "sh",
                "-c",
                'pg_dumpall --globals-only -U "$POSTGRES_USER"',
            ],
            "database.dump": [
                DOCKER,
                "exec",
                pg,
                "sh",
                "-c",
                'pg_dump --format=custom -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
            ],
            "artifacts.tar": [DOCKER, "exec", api, "tar", "-C", ARTIFACT_ROOT, "-cf", "-", "."],
        }
        files: dict[str, Any] = {}
        for name, argv in sources.items():
            info = _stream_encrypt(argv, partial / f"{name}.jfenc", public, name)
            if name == "database.dump" and not info["head"].startswith(b"PGDMP"):
                raise BackupError("database.dump is not a PostgreSQL custom-format archive")
            info.pop("head")
            files[name] = info
        # Table names come from the COUNTED_TABLES constant, never from input.
        counts = {
            t: int(_psql(pg, f"SELECT count(*) FROM {t}"))  # noqa: S608
            for t in COUNTED_TABLES
        }
        manifest = {
            "format": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "project": project,
            "alembic_revision": _psql(pg, "SELECT version_num FROM alembic_version"),
            "row_counts": counts,
            "artifact_files": int(_exec_text(api, f"find {ARTIFACT_ROOT} -type f | wc -l")),
            "public_key_sha256": hashlib.sha256(public_text.strip().encode()).hexdigest(),
            "files": files,
        }
        for name in FILES:
            path = partial / f"{name}.jfenc"
            files[name]["ciphertext_sha256"] = _sha256_file(path)
            files[name]["ciphertext_bytes"] = path.stat().st_size
        (partial / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
        verify(partial)
        partial.rename(final)
    except BaseException:
        # Only this run's own incomplete output; never an existing bundle.
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return final


# ------------------------------------------------------------------ verify
def verify(bundle: Path) -> dict[str, Any]:
    """Keyless: manifest present, ciphertext hashes match, framing complete."""
    manifest_path = bundle / "manifest.json"
    if not manifest_path.is_file():
        raise BackupError("no manifest: not a complete bundle")
    manifest: dict[str, Any] = json.loads(manifest_path.read_text())
    for name in FILES:
        path = bundle / f"{name}.jfenc"
        if not path.is_file():
            raise BackupError(f"{name} is missing")
        if _sha256_file(path) != manifest["files"][name]["ciphertext_sha256"]:
            raise BackupError(f"{name} does not match its recorded hash")
        with path.open("rb") as fh:
            try:
                bc.inspect(fh)
            except bc.BackupCryptoError as exc:
                raise BackupError(f"{name}: {exc}") from exc
    return manifest


# --------------------------------------------------------------- retention
@dataclass(frozen=True)
class Plan:
    keep: list[str]
    delete: list[str]


def _bundle_date(name: str) -> date | None:
    try:
        stamp = datetime.strptime(name[len(BUNDLE_PREFIX) :] + "+0000", "%Y%m%dT%H%M%SZ%z")
    except ValueError:
        return None
    return stamp.astimezone(UTC).date()


def retention_plan(names: list[str], *, daily: int = 7, weekly: int = 4) -> Plan:
    """Keep the newest bundle of each of the `daily` most recent days, then the
    newest bundle of up to `weekly` older ISO weeks not already represented.
    Anything that is not a recognised bundle name is never touched."""
    newest_per_day: dict[date, str] = {}
    for name in sorted((n for n in names if _bundle_date(n)), reverse=True):
        newest_per_day.setdefault(_bundle_date(name), name)  # type: ignore[arg-type]
    ordered = sorted(newest_per_day.items(), reverse=True)
    keep = [name for _, name in ordered[:daily]]
    weeks = {day.isocalendar()[:2] for day, _ in ordered[:daily]}
    weekly_kept = 0
    for day, name in ordered[daily:]:
        week = day.isocalendar()[:2]
        if week not in weeks and weekly_kept < weekly:
            weeks.add(week)
            keep.append(name)
            weekly_kept += 1
    bundles = [n for n in names if _bundle_date(n)]
    return Plan(keep=keep, delete=sorted((n for n in bundles if n not in keep), reverse=True))


def retention(dest: Path, *, apply: bool) -> Plan:
    dest = dest.expanduser().resolve()
    names = [p.name for p in dest.iterdir() if p.is_dir() and p.name.startswith(BUNDLE_PREFIX)]
    plan = retention_plan(names)
    if apply:
        if not plan.keep:
            raise BackupError("refusing to delete: no bundle would remain")
        newest = dest / sorted(plan.keep, reverse=True)[0]
        verify(newest)  # never prune unless the newest backup verifies
        for name in plan.delete:
            shutil.rmtree(dest / name)
    return plan


# --------------------------------------------------------------------- cli
def _iter_print(lines: Iterator[str]) -> None:
    for line in lines:
        print(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.backup")
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("create")
    c.add_argument("--project", default="jobforge-prod")
    c.add_argument("--public-key", type=Path, required=True)
    c.add_argument("--dest", type=Path, required=True)
    v = sub.add_parser("verify")
    v.add_argument("bundle", type=Path)
    r = sub.add_parser("retention")
    r.add_argument("--dest", type=Path, required=True)
    r.add_argument("--apply", action="store_true", help="actually delete (owner decision)")
    sub.add_parser("keygen").add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            bundle = create(args.project, args.public_key, args.dest)
            print(json.dumps({"ok": True, "bundle": str(bundle)}))
        elif args.command == "verify":
            manifest = verify(args.bundle)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "revision": manifest["alembic_revision"],
                        "row_counts": manifest["row_counts"],
                    }
                )
            )
        elif args.command == "retention":
            plan = retention(args.dest, apply=args.apply)
            print(json.dumps({"applied": args.apply, "keep": plan.keep, "delete": plan.delete}))
        else:
            import getpass

            directory = args.directory.expanduser().resolve()
            if directory.is_relative_to(ROOT):
                raise BackupError("keys must live outside the repository")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            phrase = getpass.getpass("Passphrase for the private key (not echoed): ")
            if phrase != getpass.getpass("Repeat: "):
                raise BackupError("passphrases do not match")
            bc.generate_keypair(directory / "backup.key", directory / "backup.pub", phrase.encode())
            _iter_print(
                iter(
                    [
                        f"wrote {directory}/backup.pub (keep on the Mac)",
                        f"wrote {directory}/backup.key (move offline; needed to restore)",
                    ]
                )
            )
    except (BackupError, bc.BackupCryptoError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
