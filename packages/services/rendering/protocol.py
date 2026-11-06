"""The renderer contract, and the template version that binds bytes to it.

A renderer is a pure function from `ResumeDocument` to bytes. It takes no
session, no store, no settings and no clock: anything that varies between two
calls with the same document is a determinism bug, and keeping the signature
this narrow is what makes that statement checkable rather than aspirational.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from packages.shared.errors import AppError

#: Bump whenever rendered output changes for unchanged content - a layout
#: tweak, a font change, a library upgrade that moves a glyph. It is part of
#: the artifact's identity and of its uniqueness key, so a bump produces a new
#: artifact rather than silently replacing the file a human already downloaded.
TEMPLATE_VERSION = "ats-single-column/2"

#: The only content types M8 will ever store. Asserted, never inferred from a
#: filename or echoed from a request.
ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/pdf",
        "text/plain; charset=utf-8",
    }
)

#: Artifact kinds M8 produces, and the `artifacts.kind` value each maps to.
RESUME_ARTIFACT_KINDS: frozenset[str] = frozenset({"resume_docx", "resume_pdf"})


class RendererUnavailableError(AppError):
    """The renderer's dependency is not installed.

    A 503 and never a fallback. If `python-docx` is missing, the honest answer
    is "this deployment cannot produce DOCX", not a text file with a .docx
    name that Word will refuse and an ATS will parse as garbage. Silently
    degrading the format of a document a human is about to send to an employer
    is the worst available outcome, so the production path fails closed.
    """

    status_code = 503
    title = "Renderer Unavailable"
    error_type = "https://jobforge.local/errors/renderer-unavailable"


@dataclass(frozen=True)
class RenderedArtifact:
    """Bytes plus everything the artifact row needs to describe them."""

    kind: str
    content_type: str
    extension: str
    template_version: str
    data: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def size_bytes(self) -> int:
        return len(self.data)

    def __post_init__(self) -> None:
        if self.kind not in RESUME_ARTIFACT_KINDS:
            raise ValueError(f"unsupported artifact kind {self.kind!r}")
        if self.content_type not in ALLOWED_CONTENT_TYPES:
            raise ValueError(f"content type {self.content_type!r} is not on the allowlist")
        if not self.data:
            raise ValueError("a rendered artifact may not be empty")


@runtime_checkable
class ResumeRenderer(Protocol):
    """One output format.

    `production` separates a renderer that may serve a real download from one
    that exists to exercise the pipeline. The service refuses a non-production
    renderer unless it was handed one explicitly, so a test double cannot
    reach a real artifact by being the only thing registered.
    """

    kind: str
    extension: str
    content_type: str
    template_version: str
    production: bool

    #: False while a renderer is a declared dependency boundary whose body has
    #: not been written and executed yet. Deliberately separate from
    #: `available()`: a library can be installed in a process whose renderer
    #: body is still unwritten, and conflating the two would let an unwritten
    #: renderer look ready merely because its import succeeded.
    implemented: bool

    def available(self) -> bool:
        """False when the dependency is missing, without importing it eagerly."""
        ...

    def render(self, document: object) -> bytes:
        """Bytes for this document. Pure: same document, same bytes."""
        ...
