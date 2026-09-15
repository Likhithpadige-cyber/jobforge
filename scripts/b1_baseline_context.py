"""Shared, database-free loader for the frozen B1 baseline inputs.

Imported by `scripts/gen_b1_baseline.py`, `scripts/gen_b1_c2_user_payload.py`
and `tests/unit/test_b1_baseline_identity.py`, so the generator and the test
cannot drift into building two different contexts and calling the difference a
regression.

Nothing here touches a database, a clock, the environment or randomness: every
identifier is `UUID(int=n)` from the fixture, which is what makes the baseline
reproducible on any machine.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"

INPUT_FIXTURE = FIXTURES / "b1_baseline_input.json"
REVISION_FIXTURE = FIXTURES / "b1_baseline_revision.json"
LIVE_BASELINE = FIXTURES / "b1_live_prehardening_baseline.json"
C2_USER_PAYLOAD = FIXTURES / "b1_c2_user_payload.json"

#: The B1 system prompt this design was grounded against. The generators refuse
#: to run against anything else; see C3_HARDENING_DESIGN.md I.5.1 step 2.
B1_SELECTION_TXT = (
    REPO_ROOT / "packages" / "agents" / "resume_optimizer" / "prompts" / "b1_selection.txt"
)
B1_SELECTION_SHA256 = "9a86a79435fc8b076151720c88c42cd588a62b7ab8566af6421e1fcf0ec3b74e"

#: `BASE` — the live pre-hardening commit (C3_HARDENING_DESIGN.md I.5.1).
BASE_COMMIT = "1c6507b88411ad0b4e445e435fe9a0c69c677b99"

#: Whole-tree digests over the pristine BASE tree, recorded at rev. 7
#: finalisation. Host-only provenance: the API container mounts a handful of
#: subtrees, not the repository root, so neither can be computed inside it.
BASE_TREE_DIGEST_SHA256 = "837ed16bbe29fbe1885a443354ac1897e8e1ee88ac054cb171265e363c24c02f"
BASE_MANIFEST_SHA256 = "b632563c4a450311159500bc21c8510e29d111808288fc5b962c98af3b85a77e"

#: Content digest over `packages/` at BASE — every module that can influence
#: the B1 request (the prompt builder, the runtime `Request`, the context and
#: evidence types, the guardrail scanner, the enums). This is the integrity
#: gate that works everywhere: `packages/` is mounted into the API container,
#: it is pure Python to compute, it needs neither Git nor tar, and Batch 0 adds
#: nothing underneath it so it needs no exclusion list. It is a *content*
#: check, so it is strictly stronger than `git diff` for the code under test.
BASE_PACKAGES_MANIFEST_SHA256 = "0fd73a30cd5ce5f6a44119478a5c6668e07749b6af4f0dfec209ece091420ca9"
BASE_PACKAGES_FILE_COUNT = 86


def packages_manifest(root: Path | None = None) -> tuple[str, int]:
    """sha256 over `<sha256>  <path>` lines for every file under `packages/`.

    Sorted by POSIX path, byte-content hashed, `__pycache__` and `*.pyc`
    excluded because they are build output and differ per interpreter.
    """
    top = root or REPO_ROOT
    files = [
        p
        for p in (top / "packages").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    ]
    return _digest_files(files, top)


#: Directories, filenames and suffixes excluded from the whole-tree manifest.
#: These mirror GNU tar's `--exclude-vcs` plus the build/cache/secret excludes
#: in the recorded digest command, so the manifest covers exactly the members
#: that digest covers.
TREE_EXCLUDE_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".next",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "backups",
    }
)
TREE_EXCLUDE_NAMES = frozenset({".env", ".DS_Store", ".gitignore", ".gitattributes", ".gitmodules"})


def _digest_files(files: list[Path], root: Path, prefix: str = "") -> tuple[str, int]:
    """sha256 over `<sha256 of contents>  <prefix><posix path>` lines, path-sorted.

    `prefix` exists because the two manifests answer to different readers.
    `tree_manifest` uses "./" so its value is reproducible by hand with the
    `find . -type f ... | sort | xargs sha256sum | sha256sum` one-liner recorded
    in the design, whose paths carry that prefix. `packages_manifest` is
    internal to this tooling and uses bare repository-relative paths.
    """
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda p: prefix + p.relative_to(root).as_posix()):
        rel = prefix + path.relative_to(root).as_posix()
        digest.update(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {rel}\n".encode())
    return digest.hexdigest(), len(files)


def tree_manifest(root: Path | None = None) -> tuple[str, int]:
    """Whole-tree content manifest over the recorded digest's member set.

    Pure Python, so it needs no `tar` binary and runs anywhere the full tree is
    mounted. Host-only in practice: the API container mounts subtrees.
    """
    base = root or REPO_ROOT
    files: list[Path] = []
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(base)
        if TREE_EXCLUDE_DIRS.intersection(rel.parts[:-1]):
            continue
        if rel.name in TREE_EXCLUDE_NAMES or rel.suffix == ".pyc":
            continue
        files.append(path)
    return _digest_files(files, base, prefix="./")


def read_git_head(root: Path | None = None) -> str | None:
    """Resolve HEAD by reading `.git` directly — no `git` executable needed.

    The API container has neither the binary nor the directory, so this returns
    None there and the caller falls back to the explicitly supplied
    `--base-commit`. On the host it works even in an image without Git.
    """
    git_dir = (root or REPO_ROOT) / ".git"
    if git_dir.is_file():  # a worktree: ".git" is a pointer file
        pointer = git_dir.read_text(encoding="utf-8").strip()
        if not pointer.startswith("gitdir:"):
            return None
        git_dir = Path(pointer.split(":", 1)[1].strip())
    if not git_dir.is_dir():
        return None
    head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    if not head.startswith("ref:"):
        return head or None
    ref = head.split(":", 1)[1].strip()
    loose = git_dir / ref
    if loose.is_file():
        return loose.read_text(encoding="utf-8").strip() or None
    packed = git_dir / "packed-refs"
    if packed.is_file():
        for line in packed.read_text(encoding="utf-8").splitlines():
            if line.startswith(("#", "^")):
                continue
            sha, _, name = line.partition(" ")
            if name.strip() == ref:
                return sha.strip()
    return None


def load_input(path: Path = INPUT_FIXTURE) -> tuple[Any, str]:
    """Build the frozen `TailoringContext` and model id from the fixture.

    Imported lazily so this module can be read by tooling that has not
    installed the application's dependencies.
    """
    from packages.agents.resume_optimizer.context import TailoringContext, TailoringFact
    from packages.core.models import FactStatus, FactType, Recommendation

    raw = json.loads(path.read_text(encoding="utf-8"))
    ctx = raw["context"]

    facts = tuple(
        TailoringFact(
            id=UUID(int=fact["id_int"]),
            text=fact["text"],
            structured=fact["structured"],
            fact_type=FactType(fact["fact_type"]),
            status=FactStatus.VERIFIED,
        )
        for fact in ctx["facts"]
    )
    context = TailoringContext(
        profile_id=UUID(int=ctx["profile_id_int"]),
        base_resume_id=UUID(int=ctx["base_resume_id_int"]),
        base_resume_version=ctx["base_resume_version"],
        base_resume_structure=ctx["base_resume_structure"],
        facts=facts,
        job={
            "id": UUID(int=ctx["job"]["id_int"]),
            "title": ctx["job"]["title"],
            "description_raw": ctx["job"]["description_raw"],
        },
        evaluation_id=UUID(int=ctx["evaluation_id_int"]),
        profile_version=ctx["profile_version"],
        match_matrix=ctx["match_matrix"],
        score=Decimal(ctx["score"]),
        recommendation=Recommendation(ctx["recommendation"]),
        score_breakdown=ctx["score_breakdown"],
        rationale=ctx["rationale"],
        gaps=tuple(ctx["gaps"]),
    )
    return context, raw["model"]


def load_revision(path: Path = REVISION_FIXTURE) -> dict[str, Any]:
    """The frozen `revision` block, in `guidance_payload()` shape.

    `json.loads` is typed `Any`, so the block is narrowed explicitly rather than
    returned straight through. The check is not ceremony: a malformed fixture
    would otherwise reach `prepare_request` and surface as a confusing failure
    inside prompt construction instead of here, where the file is named.
    """
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    revision = raw["revision"]
    if not isinstance(revision, dict):
        raise TypeError(
            f"{path}: 'revision' must be a JSON object, got {type(revision).__name__}"
        )
    return revision
