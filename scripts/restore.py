"""Restore an encrypted JobForge backup into a NEW, isolated stack (M21).

    python -m scripts.restore BUNDLE --private-key KEY --target-project jobforge-restoretest \
        --env-file ENV --port 8482 --release TAG [--passphrase-file FILE]

Never restores over an existing stack: the target must be a
`jobforge-restore*` project with no volumes yet, and production
(`jobforge-prod`) is refused outright. Steps: keyless bundle verification →
fresh postgres/redis → roles (then the owner password is reset from the
target env) → database (pg_restore --exit-on-error) → artifact volume →
`init` (marker + runtime role passwords from the target env) → full stack →
preflight → row-count and artifact comparison with the manifest. Every
decrypted stream is authenticated and its plaintext SHA-256 compared with
the manifest; any mismatch fails the restore. Prints a JSON report; never a
secret. A failed restore leaves the isolated target in place for inspection
(removing it is the owner's decision).
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from scripts import backup_crypto as bc
from scripts.backup import COUNTED_TABLES, DOCKER, ROOT, BackupError, container, verify

TARGET = re.compile(r"^jobforge-restore[a-z0-9-]*$")
COMPOSE_FILE = ROOT / "docker-compose.prod.yml"


def _compose(project: str, env_file: Path) -> list[str]:
    return [DOCKER, "compose", "-p", project, "-f", str(COMPOSE_FILE), "--env-file", str(env_file)]


def _run(argv: list[str], *, env: dict[str, str]) -> None:
    result = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)  # noqa: S603
    if result.returncode != 0:
        raise BackupError(f"step failed: {' '.join(argv[3:6])}… ({result.returncode})")


def _wait_healthy(cid: str, seconds: int = 180) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = subprocess.run(  # noqa: S603
            [DOCKER, "inspect", "-f", "{{.State.Health.Status}}", cid],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if state == "healthy":
            return
        time.sleep(2)
    raise BackupError("a service did not become healthy in time")


def _pipe_decrypted(bundle: Path, name: str, private: Any, argv: list[str], expected: str) -> None:
    """Decrypt into a consumer's stdin; authenticate and hash-check it all."""
    # stderr is discarded, not piped: an unread pipe can deadlock a chatty
    # pg_restore, and its messages may quote restored data rows.
    proc = subprocess.Popen(  # noqa: S603 - fixed argv built here
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdin is not None
    digest = hashlib.sha256()
    try:
        with (bundle / f"{name}.jfenc").open("rb") as fh:
            for chunk in bc.decrypt_stream(fh, private):
                digest.update(chunk)
                proc.stdin.write(chunk)
    finally:
        proc.stdin.close()
        proc.wait()
    if digest.hexdigest() != expected:
        raise BackupError(f"{name}: decrypted content does not match the manifest")
    if proc.returncode != 0:
        raise BackupError(f"{name}: restore command failed ({proc.returncode})")


def _refuse_existing(project: str) -> None:
    volumes = subprocess.run(  # noqa: S603
        [DOCKER, "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    if volumes:
        raise BackupError(f"{project} already has volumes; restore only into a new project")


def restore(
    bundle: Path, *, private: Any, project: str, env_file: Path, port: int, release: str
) -> dict[str, Any]:
    started = time.monotonic()
    if not TARGET.match(project):
        raise BackupError("the target must be a new jobforge-restore* project")
    if port in (8480, 3000, 3100, 3383, 8000, 8100, 8383):
        raise BackupError("that port belongs to another stack")
    env_file = env_file.expanduser().resolve()
    if env_file.is_relative_to(ROOT) or (env_file.stat().st_mode & 0o077):
        raise BackupError("the target env file must be outside the repository with mode 600")
    manifest = verify(bundle)
    _refuse_existing(project)

    env = {
        **os.environ,
        "JOBFORGE_PROD_ENV_FILE": str(env_file),
        "JOBFORGE_RELEASE": release,
        "JOBFORGE_PROD_PORT": str(port),
    }
    compose = _compose(project, env_file)
    _run([*compose, "up", "-d", "postgres", "redis"], env=env)
    pg = container(project, "postgres")
    _wait_healthy(pg)

    files = manifest["files"]
    _pipe_decrypted(
        bundle,
        "globals.sql",
        private,
        [
            DOCKER,
            "exec",
            "-i",
            pg,
            "sh",
            "-c",
            'psql -q -v ON_ERROR_STOP=0 -U "$POSTGRES_USER" -d postgres',
        ],
        files["globals.sql"]["plaintext_sha256"],
    )
    # The roles dump carries the SOURCE owner's password; restore the target's.
    _run(
        [
            DOCKER,
            "exec",
            pg,
            "sh",
            "-c",
            'psql -q -U "$POSTGRES_USER" -d postgres -c '
            '"ALTER ROLE \\"$POSTGRES_USER\\" PASSWORD \'$POSTGRES_PASSWORD\'"',
        ],
        env=env,
    )
    _pipe_decrypted(
        bundle,
        "database.dump",
        private,
        [
            DOCKER,
            "exec",
            "-i",
            pg,
            "sh",
            "-c",
            'pg_restore --exit-on-error -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
        ],
        files["database.dump"]["plaintext_sha256"],
    )
    _pipe_decrypted(
        bundle,
        "artifacts.tar",
        private,
        [
            *compose,
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            "tar",
            "api",
            "-C",
            "/var/lib/jobforge/artifacts",
            "-xf",
            "-",
        ],
        files["artifacts.tar"]["plaintext_sha256"],
    )
    _run(
        [
            *compose,
            "--profile",
            "release",
            "run",
            "--rm",
            "migrate",
            "python",
            "-m",
            "scripts.prod_env",
            "init",
        ],
        env=env,
    )
    _run([*compose, "up", "-d"], env=env)
    _wait_healthy(container(project, "edge"))

    api = container(project, "api")
    pre = subprocess.run(  # noqa: S603
        [DOCKER, "exec", api, "python", "-m", "scripts.prod_env", "preflight"],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        report = json.loads(pre.stdout)
    except ValueError:
        report = {"ok": False, "checks": []}
    counts = {}
    for table in COUNTED_TABLES:
        out = subprocess.run(  # noqa: S603
            [
                DOCKER,
                "exec",
                pg,
                "sh",
                "-c",
                # table comes from the COUNTED_TABLES constant only
                f'psql -tA -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*) FROM {table}"',  # noqa: S608
            ],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        counts[table] = int(out or -1)
    files_now = int(
        subprocess.run(  # noqa: S603
            [DOCKER, "exec", api, "sh", "-c", "find /var/lib/jobforge/artifacts -type f | wc -l"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        or -1
    )
    failed_checks = [
        c["name"] for c in report.get("checks", []) if not c["ok"] and c.get("fatal", True)
    ]
    ok = (
        counts == manifest["row_counts"]
        and files_now == manifest["artifact_files"]
        and not failed_checks
    )
    return {
        "ok": ok,
        "target_project": project,
        "seconds": round(time.monotonic() - started, 1),
        "revision": manifest["alembic_revision"],
        "row_counts_match": counts == manifest["row_counts"],
        "artifact_files": {"expected": manifest["artifact_files"], "restored": files_now},
        "preflight_failed_checks": failed_checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.restore")
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--passphrase-file", type=Path)
    parser.add_argument("--target-project", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--release", required=True, help="image tag to run the restored stack with")
    args = parser.parse_args(argv)
    try:
        if args.passphrase_file:
            passphrase = args.passphrase_file.read_bytes().strip()
        else:
            passphrase = getpass.getpass("Backup key passphrase (not echoed): ").encode()
        private = bc.load_private(args.private_key.read_bytes(), passphrase)
        report = restore(
            args.bundle,
            private=private,
            project=args.target_project,
            env_file=args.env_file,
            port=args.port,
            release=args.release,
        )
    except (BackupError, bc.BackupCryptoError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
