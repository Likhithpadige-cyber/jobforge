"""Storage keys and download filenames. Neither may carry candidate data.

Two different strings with two different threat models:

* the **storage key** is what the object store sees. It is opaque - UUIDs and
  fixed words only - so an operator with bucket listing, a log line, or a
  leaked metrics label learns which resume version exists and nothing about
  whose it is.
* the **download filename** is what the browser saves. It must be readable
  enough to be useful and must still contain no name, no email and nothing
  taken from a request.

A candidate's name never reaches either. That is not fastidiousness: a
filename ends up in a downloads folder, an email attachment and a recruiter's
inbox, and a storage key ends up in access logs the candidate never sees.
"""

from __future__ import annotations

import re
from uuid import UUID

#: Anything outside this set is dropped from a filename component.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

#: A path component that is entirely dots. `.` and `..` survive `_UNSAFE`
#: untouched - dots are legal in a filename - and `resumes/<id>/../resume.docx`
#: addresses an object one level up from where the key claims to point. Caught
#: by tests/unit/test_render_keys.py before it reached anything real.
_DOTS_ONLY = re.compile(r"^\.+$")

#: Leading dots and dashes would produce a hidden file or an argument-looking
#: name when the download is later handled by a script.
_LEADING = re.compile(r"^[.\-]+")

MAX_FILENAME_LENGTH = 100


def storage_key(*, resume_id: UUID | str, template_version: str, extension: str) -> str:
    """`resumes/<resume-uuid>/<template>/resume.<ext>`.

    Deterministic, so re-rendering the same resume under the same template
    addresses the same object instead of accumulating near-duplicates, and the
    uniqueness constraint in the database and the key in the store agree by
    construction rather than by convention.

    The resume id is validated as a UUID rather than interpolated as a string:
    a key is a path, and a path assembled from unvalidated input is how a
    traversal gets written.
    """
    resume = resume_id if isinstance(resume_id, UUID) else UUID(str(resume_id))
    template = _UNSAFE.sub("-", template_version).strip("-.")
    if not template or _DOTS_ONLY.match(template):
        raise ValueError("template version has no usable characters")
    ext = _UNSAFE.sub("", extension).lstrip(".").lower()
    if not ext:
        raise ValueError("extension has no usable characters")
    return f"resumes/{resume}/{template}/resume.{ext}"


def download_filename(*, application_id: UUID | str, version: int, extension: str) -> str:
    """`jobforge-resume-<application-uuid>-v<version>.<ext>`.

    Identifiers and a version, nothing else. There is no branch that reads a
    profile, a job title or a company name, so there is no branch that can
    leak one into a Content-Disposition header.
    """
    application = application_id if isinstance(application_id, UUID) else UUID(str(application_id))
    ext = _UNSAFE.sub("", extension).lstrip(".").lower()
    if not ext:
        raise ValueError("extension has no usable characters")
    name = f"jobforge-resume-{application}-v{int(version)}.{ext}"
    return sanitize_filename(name)


def sanitize_filename(name: str) -> str:
    """Reduce any string to something safe to put in Content-Disposition.

    Separators, control characters, quotes and newlines all go: a newline in
    this header is a response-splitting primitive, and a quote breaks out of
    the quoted-string the header uses. Exported so a caller that must build a
    name from another source cannot reach for its own weaker version.
    """
    cleaned = _UNSAFE.sub("-", name.replace("/", "-").replace("\\", "-"))
    cleaned = _LEADING.sub("", cleaned).strip("-")
    if not cleaned or _DOTS_ONLY.match(cleaned):
        raise ValueError("filename has no usable characters")
    if len(cleaned) > MAX_FILENAME_LENGTH:
        stem, _, ext = cleaned.rpartition(".")
        keep = MAX_FILENAME_LENGTH - len(ext) - 1
        cleaned = f"{stem[:keep]}.{ext}" if stem and keep > 0 else cleaned[:MAX_FILENAME_LENGTH]
    return cleaned
