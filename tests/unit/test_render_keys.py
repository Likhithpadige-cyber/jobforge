"""Storage keys and download filenames carry identifiers, never people.

A storage key ends up in bucket listings and access logs the candidate never
sees; a download filename ends up in a downloads folder and an email
attachment. Neither may contain a name, an address or anything taken from a
request.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from packages.services.rendering.keys import (
    MAX_FILENAME_LENGTH,
    download_filename,
    sanitize_filename,
    storage_key,
)
from packages.services.rendering.protocol import TEMPLATE_VERSION

pytestmark = pytest.mark.unit

RESUME = UUID("0192f000-0000-7000-8000-000000000001")
APPLICATION = UUID("0192f000-0000-7000-8000-0000000000aa")


# --------------------------------------------------------------- storage key
def test_the_key_is_opaque_and_deterministic() -> None:
    key = storage_key(resume_id=RESUME, template_version=TEMPLATE_VERSION, extension="docx")
    assert key == f"resumes/{RESUME}/ats-single-column-1/resume.docx"
    assert key == storage_key(
        resume_id=str(RESUME), template_version=TEMPLATE_VERSION, extension="docx"
    )


def test_the_key_contains_only_identifiers_and_fixed_words() -> None:
    key = storage_key(resume_id=RESUME, template_version=TEMPLATE_VERSION, extension="pdf")
    assert set(key.split("/")) <= {"resumes", str(RESUME), "ats-single-column-1", "resume.pdf"}


def test_a_template_change_addresses_a_different_object() -> None:
    """Two templates must never write over each other's bytes."""
    first = storage_key(resume_id=RESUME, template_version="a/1", extension="docx")
    second = storage_key(resume_id=RESUME, template_version="a/2", extension="docx")
    assert first != second


def test_a_non_uuid_resume_id_is_refused() -> None:
    """A key is a path; a path built from unvalidated input is a traversal."""
    with pytest.raises(ValueError):
        storage_key(resume_id="../../secrets", template_version="t", extension="docx")


@pytest.mark.parametrize("bad", ["", "/", "..", "///"])
def test_an_unusable_template_version_is_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        storage_key(resume_id=RESUME, template_version=bad, extension="docx")


def test_an_unusable_extension_is_refused() -> None:
    with pytest.raises(ValueError):
        storage_key(resume_id=RESUME, template_version="t", extension="...")


# ----------------------------------------------------------------- filenames
def test_the_download_filename_is_identifiers_and_a_version() -> None:
    name = download_filename(application_id=APPLICATION, version=3, extension="docx")
    assert name == f"jobforge-resume-{APPLICATION}-v3.docx"


def test_the_filename_has_no_branch_that_could_read_a_profile() -> None:
    """Two different resumes differ only by identifier and version."""
    a = download_filename(application_id=APPLICATION, version=1, extension="pdf")
    b = download_filename(application_id=APPLICATION, version=2, extension="pdf")
    assert a != b
    assert a.replace("-v1", "") == b.replace("-v2", "")


@pytest.mark.parametrize(
    "hostile",
    [
        "../../etc/passwd",
        'name"; filename="other.docx',
        "name\r\nX-Injected: 1",
        "name\nSet-Cookie: a=b",
        "....//....//x",
        "\u0000null.docx",
    ],
)
def test_hostile_names_are_neutralised(hostile: str) -> None:
    """Content-Disposition is a header: a newline in it splits the response,
    and a quote escapes the quoted-string the header uses."""
    cleaned = sanitize_filename(hostile + ".docx")
    for char in ('"', "\r", "\n", "/", "\\", "\x00"):
        assert char not in cleaned
    assert not cleaned.startswith((".", "-"))


def test_a_long_name_is_truncated_but_keeps_its_extension() -> None:
    cleaned = sanitize_filename("x" * 400 + ".docx")
    assert len(cleaned) <= MAX_FILENAME_LENGTH
    assert cleaned.endswith(".docx")


def test_a_name_with_nothing_usable_is_refused() -> None:
    with pytest.raises(ValueError):
        sanitize_filename("///")
