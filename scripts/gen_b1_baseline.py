#!/usr/bin/env python3
"""Freeze the live pre-hardening B1 baseline (C3_HARDENING_DESIGN.md I.5.1).

Run this ONCE, at `BASE` - the commit immediately before any AuditBoundary or
boundary-refactor change begins - and commit the fixture it writes. Generated
after the refactor it proves nothing: it would be a photograph of the thing it
is supposed to be checking.

    python scripts/gen_b1_baseline.py [--base-commit SHA] [--force]

**On identifying BASE.** The design specified a deterministic tar digest of the
whole tree, because at the time the live tree was not under version control.
Two things made that unusable as a gate:

1. Batch 0 necessarily adds this script and its fixtures to the tree, so a
   whole-tree digest computed while Batch 0 exists can never equal the pristine
   BASE value.
2. The API container mounts `apps`, `packages`, `workers`, `migrations`, `tests`
   and `scripts` - not the repository root, and not `.git`. Neither the whole
   tree nor a `git` executable is present in there.

So the gate is content-based and environment-independent instead, and this
module spawns no subprocess at all:

* **Integrity (always enforced):** a pure-Python content manifest over
  `packages/` must equal the value recorded at BASE. That covers every module
  able to influence the B1 request, needs neither Git nor tar, and works
  identically on the host and in the container. Being a content check, it is
  strictly stronger than `git diff` for the code under test.
* **The B1 prompt (always enforced):** `b1_selection.txt` must hash to the
  recorded value.
* **Identity (verified where possible, required otherwise):** HEAD is read
  straight out of `.git` in pure Python, so no `git` binary is needed. Where
  `.git` is unavailable an explicit `--base-commit` (or `JOBFORGE_BASE_COMMIT`)
  is required; it is never silently skipped, and a value disagreeing with the
  recorded BASE aborts.
* **Whole-tree provenance:** a pure-Python manifest over the same members the
  recorded tar digest covers, computed only where the full tree is present.
  The tar digest itself stays a recorded constant; `I.5.1` carries the command
  that reproduces it by hand.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.b1_baseline_context import (
    B1_SELECTION_SHA256,
    B1_SELECTION_TXT,
    BASE_COMMIT,
    BASE_MANIFEST_SHA256,
    BASE_PACKAGES_FILE_COUNT,
    BASE_PACKAGES_MANIFEST_SHA256,
    BASE_TREE_DIGEST_SHA256,
    INPUT_FIXTURE,
    LIVE_BASELINE,
    REPO_ROOT,
    REVISION_FIXTURE,
    load_input,
    load_revision,
    packages_manifest,
    read_git_head,
    tree_manifest,
)

#: The tar pipeline that produces BASE_TREE_DIGEST_SHA256. Recorded verbatim as
#: provenance so the value stays reproducible by hand; it is not run from here,
#: because the container has no full tree to run it against.
DIGEST_COMMAND = (
    "LC_ALL=C tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner "
    "--format=posix "
    "--pax-option=exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime,delete=mtime "
    "--exclude-vcs --exclude=./node_modules --exclude=*/node_modules "
    "--exclude=./apps/web/.next --exclude=*/.next --exclude=*/__pycache__ "
    "--exclude=*.pyc --exclude=*/.pytest_cache --exclude=*/.ruff_cache "
    "--exclude=*/.mypy_cache --exclude=./.env --exclude=./backups "
    "--exclude=*/.DS_Store -cf - . | sha256sum"
)


def fail(message: str) -> None:
    sys.stderr.write(f"gen_b1_baseline: {message}\n")
    raise SystemExit(1)


def whole_tree_present() -> bool:
    """True only on the host. The container mounts subtrees, not the root."""
    return all((REPO_ROOT / name).exists() for name in ("docs", "Makefile", "infra"))


def resolve_base_commit(supplied: str | None) -> tuple[str, str]:
    """(commit, how it was established), aborting rather than assuming."""
    if supplied is not None and supplied not in (
        BASE_COMMIT,
        BASE_COMMIT[:7],
        BASE_COMMIT[:8],
        BASE_COMMIT[:12],
    ):
        fail(
            f"--base-commit {supplied} does not match the BASE this design was "
            f"grounded against ({BASE_COMMIT}). Re-ground before regenerating."
        )
    head = read_git_head()
    if head is not None:
        if head != BASE_COMMIT:
            fail(f"HEAD is {head[:7]}, expected BASE {BASE_COMMIT[:7]}")
        return BASE_COMMIT, "resolved from .git"
    if supplied is None:
        fail(
            "cannot resolve HEAD (no readable .git - expected inside the API "
            "container, which mounts subtrees only) and no baseline commit was "
            f"supplied. Pass --base-commit {BASE_COMMIT} or set "
            "JOBFORGE_BASE_COMMIT. This is required, not optional: the identity "
            "of BASE is not something to assume."
        )
    return BASE_COMMIT, "supplied (no readable .git)"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing baseline fixture (requires a reviewed reason)",
    )
    parser.add_argument(
        "--base-commit",
        default=os.environ.get("JOBFORGE_BASE_COMMIT"),
        help=(
            "the BASE commit sha. Required where .git is unreadable (the API "
            "container); verified against HEAD where .git is readable."
        ),
    )
    args = parser.parse_args()

    if LIVE_BASELINE.exists() and not args.force:
        fail(
            f"{LIVE_BASELINE.relative_to(REPO_ROOT)} already exists. The baseline is "
            "append-only by design; regenerating it after the refactor would make "
            "J.13 vacuous. Pass --force only with a reviewed reason."
        )

    # 1. Integrity, enforced in every environment: the code that builds the B1
    #    request must be byte-identical to BASE. Pure Python; no Git, no tar.
    pkg_manifest, pkg_count = packages_manifest()
    if pkg_manifest != BASE_PACKAGES_MANIFEST_SHA256:
        fail(
            f"packages/ manifest {pkg_manifest} != BASE {BASE_PACKAGES_MANIFEST_SHA256} "
            f"({pkg_count} files seen, {BASE_PACKAGES_FILE_COUNT} at BASE). Something "
            "under packages/ has changed since BASE; a baseline taken now would pin "
            "the change instead of the baseline."
        )

    # 2. Identity.
    base_commit, commit_source = resolve_base_commit(args.base_commit)

    # 3. The B1 prompt must be the one the design was grounded against.
    prompt_sha = hashlib.sha256(B1_SELECTION_TXT.read_bytes()).hexdigest()
    if prompt_sha != B1_SELECTION_SHA256:
        fail(
            f"b1_selection.txt is {prompt_sha}, expected {B1_SELECTION_SHA256}. "
            "The prompt changed: re-confirm the design before regenerating."
        )

    # 4. Whole-tree provenance, where the whole tree exists.
    whole_manifest: str | None = None
    whole_count: int | None = None
    if whole_tree_present():
        whole_manifest, whole_count = tree_manifest()

    # 5. Build both arms from the frozen input: no DB, clock or randomness.
    from packages.agents.resume_optimizer.selection import prepare_request

    context, model = load_input()
    plain, _ = prepare_request(context, model)
    revised, _ = prepare_request(context, model, revision=load_revision())

    baseline = {
        "system": plain.system,
        "user": plain.user,
        "prompt_version": plain.prompt_version,
        "digest": plain.digest,
        "revision_arm": {
            "user": revised.user,
            "prompt_version": revised.prompt_version,
            "digest": revised.digest,
        },
        "provenance": {
            "tree": "jobforge-phase5 (live)",
            "vcs": "git",
            "base_commit": base_commit,
            "base_commit_source": commit_source,
            "packages_manifest_sha256": pkg_manifest,
            "packages_file_count": pkg_count,
            # Recorded at rev. 7 finalisation over the pristine BASE tree,
            # before Batch 0 added any file. Provenance, not a gate.
            "base_tree_digest_sha256_at_rev7": BASE_TREE_DIGEST_SHA256,
            "base_manifest_sha256_at_rev7": BASE_MANIFEST_SHA256,
            "base_digest_command": DIGEST_COMMAND,
            # Host-only; null when generated inside the API container, which
            # mounts subtrees rather than the repository root.
            "tree_manifest_sha256_at_generation": whole_manifest,
            "tree_file_count_at_generation": whole_count,
            "b1_selection_txt_path": str(B1_SELECTION_TXT.relative_to(REPO_ROOT)),
            "b1_selection_txt_sha256": prompt_sha,
            "input_fixture": str(INPUT_FIXTURE.relative_to(REPO_ROOT)),
            "revision_fixture": str(REVISION_FIXTURE.relative_to(REPO_ROOT)),
            "model": model,
            "call_signature": "prepare_request(context, model, *, revision=None)",
        },
    }
    LIVE_BASELINE.write_text(
        json.dumps(baseline, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {LIVE_BASELINE.relative_to(REPO_ROOT)}")
    print(f"  base_commit    {base_commit[:7]} ({commit_source})")
    print(f"  packages/      {pkg_manifest}  ({pkg_count} files, matches BASE)")
    if whole_manifest:
        print(f"  tree manifest  {whole_manifest}  (host; {whole_count} files)")
    else:
        print("  tree manifest  skipped (partial mount; provenance only)")
    print(f"  prompt_version {plain.prompt_version}")
    print(f"  digest         {plain.digest}")
    print(f"  revision digest {revised.digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
