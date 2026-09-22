"""M8 rendering: approved structured content in, bytes out.

This package never generates, rewrites, infers or completes resume content.
M7 owns what a resume says; M8 owns how the already-approved words are laid
out in a file. `document.build_document` is the only place content is touched
at all, and it reorders and groups - nothing else.
"""

from __future__ import annotations

from packages.services.rendering.document import (
    DocumentEntry,
    DocumentSection,
    MalformedResumeContentError,
    ResumeDocument,
    build_document,
)
from packages.services.rendering.docx import DocxResumeRenderer
from packages.services.rendering.keys import (
    download_filename,
    sanitize_filename,
    storage_key,
)
from packages.services.rendering.pdf import PdfResumeRenderer
from packages.services.rendering.protocol import (
    ALLOWED_CONTENT_TYPES,
    RESUME_ARTIFACT_KINDS,
    TEMPLATE_VERSION,
    RenderedArtifact,
    RendererUnavailableError,
    ResumeRenderer,
)


def production_renderers(
    template_version: str = TEMPLATE_VERSION,
) -> tuple[ResumeRenderer, ...]:
    """The formats a real deployment offers, in a stable order.

    The reference text renderer is deliberately absent. A caller that wants it
    must import it from `packages.services.rendering.reference` and pass it in
    explicitly, which keeps "what a production worker renders" a decision made
    in one readable place rather than an accident of what happened to import.
    """
    return (
        DocxResumeRenderer(template_version),
        PdfResumeRenderer(template_version),
    )


__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "RESUME_ARTIFACT_KINDS",
    "TEMPLATE_VERSION",
    "DocumentEntry",
    "DocumentSection",
    "DocxResumeRenderer",
    "MalformedResumeContentError",
    "PdfResumeRenderer",
    "RenderedArtifact",
    "RendererUnavailableError",
    "ResumeDocument",
    "ResumeRenderer",
    "build_document",
    "download_filename",
    "production_renderers",
    "sanitize_filename",
    "storage_key",
]
