"""M10 Phase 2 - the pure, offline parts of packet generation: the
storage-key binding, the checklist text, and byte-for-byte zip determinism.
No database, no model, no network - mirrors the M8 renderer determinism
suite's shape for the same reason: these are the properties a database-backed
integration test would exercise too slowly to run on every change.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from packages.core.models.enums import ApplicationStatus
from packages.core.services.handoff_packet import (
    _checklist_text,
    _packet_storage_key,
    build_packet_zip,
)

pytestmark = pytest.mark.unit

RESUME_ID = UUID(int=1)
COVER_LETTER_ID = UUID(int=2)
APPLICATION_ID = UUID(int=3)


def test_storage_key_is_deterministic_for_identical_source_material():
    a = _packet_storage_key(
        application_id=APPLICATION_ID,
        resume_id=RESUME_ID,
        resume_version=1,
        cover_letter_id=COVER_LETTER_ID,
        cover_letter_version=1,
        answers_digest="abc123",
    )
    b = _packet_storage_key(
        application_id=APPLICATION_ID,
        resume_id=RESUME_ID,
        resume_version=1,
        cover_letter_id=COVER_LETTER_ID,
        cover_letter_version=1,
        answers_digest="abc123",
    )
    assert a == b


@pytest.mark.parametrize(
    "overrides",
    [
        {"resume_version": 2},
        {"cover_letter_version": 2},
        {"answers_digest": "different"},
    ],
)
def test_storage_key_changes_when_any_source_dimension_changes(overrides):
    base = {
        "application_id": APPLICATION_ID,
        "resume_id": RESUME_ID,
        "resume_version": 1,
        "cover_letter_id": COVER_LETTER_ID,
        "cover_letter_version": 1,
        "answers_digest": "abc123",
    }
    changed = {**base, **overrides}
    assert _packet_storage_key(**base) != _packet_storage_key(**changed)


def test_checklist_manual_handoff_states_human_action_required():
    text = _checklist_text(
        status=ApplicationStatus.MANUAL_HANDOFF,
        method="human_handoff",
        domain="example.invalid",
        blocked_reason=None,
    )
    assert "Human action required" in text
    assert "does not and will not submit it automatically" in text


def test_checklist_policy_blocked_states_automation_is_blocked():
    text = _checklist_text(
        status=ApplicationStatus.POLICY_BLOCKED,
        method="blocked",
        domain="example.invalid",
        blocked_reason="ingestion_method_blocked",
    )
    assert "Automated submission blocked by policy" in text
    assert "ingestion_method_blocked" in text
    assert "will not attempt to submit" in text


def test_checklist_submission_ready_never_implies_permission_to_auto_submit():
    """The one sentence this whole packet exists to make unambiguous."""
    text = _checklist_text(
        status=ApplicationStatus.SUBMISSION_READY,
        method="official_api",
        domain="example.invalid",
        blocked_reason=None,
    )
    assert "not implemented until a later milestone" in text
    assert "not permission to auto-submit" in text


def test_packet_zip_bytes_are_identical_across_repeated_builds():
    kwargs = {
        "resume_pdf": b"%PDF-1.4 fake resume bytes",
        "cover_letter_text": "Dear hiring team,\n\nSynthetic body.\n",
        "answers_text": "No screening questions were prepared for this application.\n",
        "job_metadata": '{"title": "Cloud Engineer"}',
        "checklist": "STATUS: test\n",
    }
    a = build_packet_zip(**kwargs)
    b = build_packet_zip(**kwargs)
    assert a == b


def test_packet_zip_contains_exactly_the_five_expected_members():
    import zipfile
    from io import BytesIO

    data = build_packet_zip(
        resume_pdf=b"pdf-bytes",
        cover_letter_text="letter",
        answers_text="answers",
        job_metadata="{}",
        checklist="checklist",
    )
    names = set(zipfile.ZipFile(BytesIO(data)).namelist())
    assert names == {
        "00-CHECKLIST.txt",
        "resume.pdf",
        "cover_letter.txt",
        "answers.txt",
        "job.json",
    }
