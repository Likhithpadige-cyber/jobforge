#!/usr/bin/env python3
"""Freeze the C2 `revision=None` user payload (C3_HARDENING_DESIGN.md I.5.2).

The C2 archive is NOT the pre-hardening baseline. The live tree intentionally
appended C3 guidance to `b1_selection.txt`, so `system`, `prompt_version` and
`digest` all moved relative to C2, and comparing them would compare across an
intended change (I.5.0). What did NOT move is the serialised `user` payload:
the `revision` key is added, never merged, so a first generation serialises
exactly as it did in C2. That single assertion is what this fixture supports.

**Isolation.** The historical `prepare_request` must run against the archive's
own modules — its `TailoringContext`, its `FactStatus`/`FactType`, its
`Request` dataclass. Doing that in-process is not safe: the live `packages.*`
modules are already imported, and clearing `sys.modules` still leaves live
class objects embedded in any value built before the swap, so the archive's
`fact.status != FactStatus.VERIFIED` and `SECTIONS[f.fact_type]` end up
comparing live enum members against historical ones. The first attempt at this
failed exactly there.

So the historical call runs in a **fresh interpreter** whose `sys.path[0]` is
the extracted archive root and which never sees the live tree. The child
builds the context from the archive's classes, calls the archive's
two-positional-argument `prepare_request(context, model)`, and returns JSON on
stdout. Nothing historical is ever imported into this process.

    python scripts/gen_b1_c2_user_payload.py --archive /tmp/c2.tar.gz
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.b1_baseline_context import (
    C2_USER_PAYLOAD,
    INPUT_FIXTURE,
    REPO_ROOT,
)

ARCHIVE_NAME = "jobforge-phase8-c2-resume-review-historyfix.tar.gz"
ARCHIVE_SHA256 = "5a8ea712b4fb45a0ff4d1bd428b6c080e900c1d639620f21d073fa4ef1ba5a16"

#: Historical values from that archive, recorded so the divergence from live is
#: documented rather than rediscovered. They are NOT asserted against live.
C2_B1_SELECTION_SHA256 = "a3112a2914bcee2236237ca06995b73ece14f53c3e51818a9ffd16082f37f2df"

#: Runs inside the extracted archive. Written to the archive root so that
#: `sys.path[0]` is that root and every import below resolves historically.
CHILD = r"""
import json, sys
from pathlib import Path

TREE = Path(__file__).resolve().parent
fixture = Path(sys.argv[1])

# sys.path[0] is TREE because this script lives there. Prove it before importing
# anything, so a misconfigured run fails loudly instead of silently using live code.
if Path(sys.path[0]).resolve() != TREE:
    raise SystemExit(f"sys.path[0] is {sys.path[0]}, expected {TREE}")

from scripts.b1_baseline_context import load_input
from packages.agents.resume_optimizer import selection as c2_selection
from packages.agents.resume_optimizer import context as c2_context
from packages.core.models import enums as c2_enums
from packages.agents.llm import runtime as c2_runtime

resolved = {
    "selection": c2_selection.__file__,
    "context": c2_context.__file__,
    "enums": c2_enums.__file__,
    "runtime": c2_runtime.__file__,
}
for name, path in resolved.items():
    if TREE not in Path(path).resolve().parents:
        raise SystemExit(f"{name} resolved to {path}, outside the extracted archive")

# Built from the archive's own TailoringContext / TailoringFact / enums, so no
# live class or enum member can reach the historical prepare_request.
context, model = load_input(fixture)
if type(context).__module__ != "packages.agents.resume_optimizer.context":
    raise SystemExit("context type came from the wrong module")

# The C2 API takes TWO POSITIONAL ARGUMENTS and has no `revision` parameter.
# The later parameter is never retrofitted here.
import inspect
params = list(inspect.signature(c2_selection.prepare_request).parameters)
if params != ["context", "model"]:
    raise SystemExit(f"C2 prepare_request signature is {params}, expected [context, model]")

request, _ = c2_selection.prepare_request(context, model)
json.dump(
    {
        "user": request.user,
        "prompt_version": request.prompt_version,
        "digest": request.digest,
        "model": model,
        "resolved_modules": resolved,
        "python": sys.version.split()[0],
    },
    sys.stdout,
)
"""


def fail(message: str) -> None:
    sys.stderr.write(f"gen_b1_c2_user_payload: {message}\n")
    raise SystemExit(1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if C2_USER_PAYLOAD.exists() and not args.force:
        fail(f"{C2_USER_PAYLOAD.relative_to(REPO_ROOT)} already exists; pass --force")
    if not INPUT_FIXTURE.is_file():
        fail(f"missing {INPUT_FIXTURE.relative_to(REPO_ROOT)}")

    # 1. Provenance: the archive is identified by name AND digest, before use.
    archive = args.archive.expanduser().resolve()
    if not archive.is_file():
        fail(f"no such archive: {archive}")
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    if sha != ARCHIVE_SHA256:
        fail(f"archive sha256 {sha} != recorded {ARCHIVE_SHA256}")
    if archive.name != ARCHIVE_NAME:
        sys.stderr.write(
            f"note: archive filename is {archive.name}, not {ARCHIVE_NAME}; "
            "the digest matches, so this is a renamed copy and is accepted\n"
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # `tarfile` rather than spawning `tar`: no external binary (the API
        # container has none), and `filter="data"` refuses absolute paths,
        # parent-directory escapes, devices and symlinks pointing outside the
        # destination - the archive is trusted by digest, but extraction is
        # still the place where a tampered archive would act.
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(root, filter="data")
        trees = [p for p in root.iterdir() if p.is_dir()]
        if len(trees) != 1:
            fail(f"expected one top-level directory in the archive, found {len(trees)}")
        tree = trees[0]

        prompt = tree / "packages/agents/resume_optimizer/prompts/b1_selection.txt"
        if not prompt.is_file():
            fail("the archive has no packages/agents/resume_optimizer/prompts/b1_selection.txt")
        c2_prompt_sha = hashlib.sha256(prompt.read_bytes()).hexdigest()
        if c2_prompt_sha != C2_B1_SELECTION_SHA256:
            fail(f"C2 b1_selection.txt is {c2_prompt_sha}, expected {C2_B1_SELECTION_SHA256}")

        # 2. Put the shared loader inside the archive, so there is ONE copy of
        #    the context-building logic and it executes against the archive's
        #    own classes rather than a duplicated, driftable transcription.
        (tree / "scripts").mkdir(exist_ok=True)
        shutil.copy2(
            REPO_ROOT / "scripts" / "b1_baseline_context.py",
            tree / "scripts" / "b1_baseline_context.py",
        )
        child = tree / "_gen_c2_payload_child.py"
        child.write_text(CHILD, encoding="utf-8")

        # 3. A fresh interpreter rooted at the archive. PYTHONPATH is cleared so
        #    nothing the parent environment added can leak in; sys.path[0] is the
        #    archive root because the child script lives there, and the child
        #    asserts it before importing anything.
        env = {**os.environ, "PYTHONPATH": "", "PYTHONDONTWRITEBYTECODE": "1"}
        # S603: the only subprocess here, and it is the isolation mechanism
        # itself - a fresh interpreter is the point. argv is fully controlled:
        # sys.executable (absolute, so no PATH lookup and no S607), a script
        # this process just wrote, and a repository-relative fixture path. No
        # shell, no user-supplied string, nothing interpolated.
        proc = subprocess.run(  # noqa: S603
            [sys.executable, str(child), str(INPUT_FIXTURE.resolve())],
            cwd=str(tree),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            fail("the isolated C2 interpreter failed:\n" + (proc.stderr.strip() or "(no stderr)"))
        historical = json.loads(proc.stdout)

    payload = {
        "user": historical["user"],
        "provenance": {
            "purpose": (
                "C2 revision=None user-payload regression only. system, "
                "prompt_version and digest are NOT compared against live; "
                "see C3_HARDENING_DESIGN.md I.5.0."
            ),
            "source_archive": archive.name,
            "source_archive_sha256": sha,
            "c2_b1_selection_txt_sha256": c2_prompt_sha,
            "c2_prompt_version_historical": historical["prompt_version"],
            "c2_digest_historical": historical["digest"],
            "c2_call_signature": "prepare_request(context, model)",
            "isolation": "subprocess; sys.path[0] = extracted archive root",
            "resolved_modules": historical["resolved_modules"],
            "input_fixture": str(INPUT_FIXTURE.relative_to(REPO_ROOT)),
            "model": historical["model"],
        },
    }
    C2_USER_PAYLOAD.write_text(
        json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {C2_USER_PAYLOAD.relative_to(REPO_ROOT)}")
    print(f"  archive sha256 {sha}")
    print(f"  c2 prompt sha  {c2_prompt_sha}  (historical; not asserted against live)")
    print(f"  isolated in    {historical['resolved_modules']['selection']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
