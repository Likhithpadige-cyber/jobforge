"""POST /profiles/{id}/master-resumes/extract-text through the real API."""

from __future__ import annotations

import io

import docx
import pytest
from sqlalchemy import func, select

from packages.core.models import Resume, User
from packages.core.services.resume_text_extraction import DOCX_MIME
from packages.shared.ids import uuid7
from tests.integration import test_submission_api as submission_api

pytestmark = [pytest.mark.integration, pytest.mark.safety]

client_factory = submission_api.client_factory


def _docx_bytes() -> bytes:
    document = docx.Document()
    document.add_paragraph("Synthetic Candidate")
    document.add_paragraph("Ran Linux hosts with Ansible.")
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


async def test_owner_gets_text_and_nothing_is_stored(
    client_factory, session, demo_profile, demo_user
) -> None:
    await session.commit()
    url = f"/api/v1/profiles/{demo_profile}/master-resumes/extract-text"
    before = await session.scalar(select(func.count(Resume.id)))
    async with await client_factory(demo_user) as owner:
        ok = await owner.post(url, content=_docx_bytes(), headers={"Content-Type": DOCX_MIME})
        assert ok.status_code == 200, ok.text
        assert ok.json() == {
            "text": "Synthetic Candidate\nRan Linux hosts with Ansible.",
            "lines": 2,
            "source": "docx",
        }
        pdf = await owner.post(
            url, content=b"%PDF-1.7", headers={"Content-Type": "application/pdf"}
        )
        assert pdf.status_code == 422 and "paste PDF text" in pdf.text
        junk = await owner.post(url, content=b"not a zip", headers={"Content-Type": DOCX_MIME})
        assert junk.status_code == 422 and "not a DOCX" in junk.text
        missing = await owner.post(
            f"/api/v1/profiles/{uuid7()}/master-resumes/extract-text",
            content=_docx_bytes(),
            headers={"Content-Type": DOCX_MIME},
        )
        assert missing.status_code == 404
    assert await session.scalar(select(func.count(Resume.id))) == before


async def test_reviewer_and_missing_csrf_are_refused(
    client_factory, session, demo_profile, demo_user
) -> None:
    reviewer = User(id=uuid7(), email=f"reviewer-{uuid7().hex}@example.invalid", role="reviewer")
    session.add(reviewer)
    await session.commit()
    url = f"/api/v1/profiles/{demo_profile}/master-resumes/extract-text"
    headers = {"Content-Type": DOCX_MIME}
    async with await client_factory(reviewer.id) as client:
        assert (await client.post(url, content=_docx_bytes(), headers=headers)).status_code == 403
    async with await client_factory(demo_user, csrf=False) as client:
        assert (await client.post(url, content=_docx_bytes(), headers=headers)).status_code == 403
