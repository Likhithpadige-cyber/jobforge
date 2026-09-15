"""J.13, J.13b, J.14 — the B1 regression baseline (C3_HARDENING_DESIGN.md I.5).

These three tests are the whole of gate 4, and they are the reason Batch 0 runs
before any AuditBoundary change: they compare the current tree's B1 request
against fixtures frozen at `BASE`, the commit immediately before the refactor
begins. Green here after the refactor means the refactor did not move B1.

What is deliberately NOT asserted: equality with the C2 archive's `system`,
`prompt_version` or `digest`. The live tree appended C3 guidance to
`b1_selection.txt`, so those three moved on purpose and comparing them would
compare across an intended change (I.5.0). J.13b pins the one thing the archive
can still prove.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

import pytest

from scripts.b1_baseline_context import (
    B1_SELECTION_SHA256,
    B1_SELECTION_TXT,
    C2_USER_PAYLOAD,
    LIVE_BASELINE,
    load_input,
    load_revision,
)

if TYPE_CHECKING:
    from pathlib import Path

    from packages.agents.llm.runtime import Request

pytestmark = pytest.mark.unit

_GENERATE = (
    "Run `python scripts/gen_b1_baseline.py` (and "
    "`python scripts/gen_b1_c2_user_payload.py --archive <path>`) at BASE, "
    "before any AuditBoundary change, and commit the fixtures. Batch 0 of "
    "C3_HARDENING_DESIGN.md Q.11 exists to do exactly this."
)


def _fixture(path: Path) -> dict[str, Any]:
    """The frozen fixture at `path`, or a failure that names how to make it."""
    if not path.exists():
        pytest.fail(f"missing baseline fixture {path.name}. {_GENERATE}")
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _request(revision: dict[str, Any] | None = None) -> Request:
    """The current tree's B1 request over the frozen input fixture.

    `prepare_request` is imported inside the call so that collecting this module
    costs nothing when the fixtures are missing and the tests fail early.
    """
    from packages.agents.resume_optimizer.selection import prepare_request

    context, model = load_input()
    request, _ = prepare_request(context, model, revision=revision)
    return request


def test_j13_b1_matches_the_live_pre_hardening_baseline() -> None:
    """J.13 — the four recorded values, field by field, in I.5.1 order.

    `revision=None` exercises the default path on the *current* signature. A
    failure here after the refactor means the AuditBoundary work moved the B1
    request, which is precisely what gate 4 forbids.
    """
    baseline = _fixture(LIVE_BASELINE)
    request = _request()

    assert request.system == baseline["system"], "B1 system prompt moved"
    assert request.user == baseline["user"], "B1 serialised payload moved"
    assert request.prompt_version == baseline["prompt_version"], "B1 prompt_version moved"
    assert request.digest == baseline["digest"], "B1 request digest moved"


def test_j13_the_prompt_file_is_the_one_the_baseline_was_taken_from() -> None:
    """Separates "the prompt changed" from "the request shape changed".

    Without this, an edit to `b1_selection.txt` and a refactor that disturbed
    the payload would both surface as the same red digest.
    """
    baseline = _fixture(LIVE_BASELINE)
    actual = hashlib.sha256(B1_SELECTION_TXT.read_bytes()).hexdigest()
    expected = baseline["provenance"]["b1_selection_txt_sha256"]

    assert actual == expected == B1_SELECTION_SHA256, (
        "b1_selection.txt differs from the file the baseline was frozen against; "
        "the baseline is stale, not the code"
    )


def test_j13b_c2_user_payload_is_unchanged_for_a_first_generation() -> None:
    """J.13b — the one assertion the C2 archive can still support.

    The `revision` block is added as its own key, never merged into an existing
    one, so a first generation serialises exactly as it did in C2. `system`,
    `prompt_version` and `digest` are not compared: they moved on purpose.
    """
    frozen = _fixture(C2_USER_PAYLOAD)
    request = _request()

    assert request.user == frozen["user"], (
        "the B1 payload diverged from C2 for revision=None; the C3 contract is "
        "that the revision block is additive and a first generation is untouched"
    )
    assert "revision" not in request.user


def test_j14_a_revision_differs_in_digest_and_not_in_prompt_version() -> None:
    """J.14 — revision identity rests on `digest`, not on `prompt_version`.

    Two separate assertions with distinct messages on purpose: an accidental new
    B1 prompt file would change `prompt_version`, and must fail loudly rather
    than slipping through on the "digests differ" half.
    """
    baseline = _fixture(LIVE_BASELINE)
    arm = baseline["revision_arm"]
    revised = _request(revision=load_revision())

    assert revised.digest == arm["digest"], "the revision arm's request moved"
    assert (
        revised.digest != baseline["digest"]
    ), "a revision must not produce the same request as a first generation"
    assert revised.prompt_version == baseline["prompt_version"], (
        "prompt_version must be identical across both arms: live builds them "
        "from one b1_selection.txt, so a difference means a new prompt file"
    )
    assert revised.user.startswith("<untrusted>")
    assert revised.user.endswith("</untrusted>")
    assert "revision" in revised.user
