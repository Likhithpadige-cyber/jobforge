"""DEVELOPMENT AND TEST ONLY. Not a resume format anyone should send.

This renderer exists so the M8 pipeline - approval revalidation, ordering,
checksums, idempotency, storage, streaming, authorization - can be exercised
end to end in an environment where the DOCX and PDF libraries are not
installed. It emits UTF-8 plain text and says so: `content_type` is
`text/plain`, `extension` is `txt`, and `production` is False.

`production = False` is load-bearing, not documentation. The render service
refuses any renderer whose `production` flag is False unless the caller
passed it in explicitly, so this cannot become the thing that serves a real
download by being the only renderer that happens to import. A resume handed
to an employer must be a real DOCX or a real PDF, and a plain-text file
wearing a .docx name would be worse than no artifact at all.
"""

from __future__ import annotations

from packages.services.rendering.document import ResumeDocument

#: Rendered with \n only. A platform-dependent line ending would make the
#: bytes - and therefore the checksum - depend on where the worker runs, which
#: is precisely the class of bug the determinism harness is built to catch.
NEWLINE = "\n"


class ReferenceTextRenderer:
    """Deterministic by construction: no clock, no locale, no randomness."""

    #: Borrowed so the pipeline can be exercised against a real artifact kind.
    #: A reference artifact never reaches the database in production, because
    #: the service will not accept this renderer there.
    kind = "resume_docx"
    extension = "txt"
    content_type = "text/plain; charset=utf-8"
    production = False
    implemented = True

    def __init__(self, template_version: str) -> None:
        self.template_version = template_version

    def available(self) -> bool:
        return True

    def render(self, document: object) -> bytes:
        if not isinstance(document, ResumeDocument):
            raise TypeError("reference renderer requires a ResumeDocument")
        lines: list[str] = []
        for section in document.sections:
            lines.append(section.heading.upper())
            lines.extend(f"- {entry.text}" for entry in section.entries)
            lines.append("")
        # No trailing-newline ambiguity: one terminator, always.
        body = NEWLINE.join(lines).rstrip(NEWLINE) + NEWLINE
        return body.encode("utf-8")
