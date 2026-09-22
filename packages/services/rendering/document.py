"""The canonical document M8 renders. Nothing here invents content.

Every string that reaches a renderer comes out of `resumes.content`, which B6
wrote and a human approved. This module reorders and groups; it never edits,
summarises, infers or completes. That is the whole reason it exists as a
separate, pure, dependency-free step: the property "M8 cannot fabricate" is
provable by reading one file, and `tests/unit/test_render_document.py` asserts
it against the persisted shape rather than trusting the claim.

The input shape is fixed by `resume_optimizer.resume_persistence.build_content`:

    {"<section>": {"<index>": {"text": str,
                               "fact_ids": [str],
                               "numerals_used": [str],
                               "order": int}}}

Index-keyed rather than a list, because `ResumeClaim.path` is
`/<section>/<index>` with a run-wide index and has to resolve into this
document. Dict iteration order in JSONB is not a contract, so ordering is
recomputed here from `order` and never read off the mapping.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Sections in the order an ATS-friendly single-column resume reads them.
#: A section outside this list is not dropped - dropping content silently is
#: exactly the failure this module must not have - it sorts after these,
#: alphabetically, so the output stays deterministic whatever B2 emits.
SECTION_ORDER: tuple[str, ...] = (
    "summary",
    "skills",
    "experience",
    "projects",
    "education",
    "certifications",
)

#: Human-facing headings. A section with no entry here is title-cased from its
#: own key, so an unknown section still renders with a readable heading.
SECTION_HEADINGS: dict[str, str] = {
    "summary": "Summary",
    "skills": "Skills",
    "experience": "Experience",
    "projects": "Projects",
    "education": "Education",
    "certifications": "Certifications",
}


class MalformedResumeContentError(ValueError):
    """The persisted content does not match the documented shape.

    Raised rather than skipped. A renderer that quietly drops a malformed
    entry produces a resume missing a line the reviewer approved, and nothing
    downstream would notice - the checksum would be perfectly stable over the
    wrong document.
    """


@dataclass(frozen=True)
class DocumentEntry:
    """One approved line. `text` is copied verbatim, never reflowed."""

    text: str
    order: int
    fact_ids: tuple[str, ...]


@dataclass(frozen=True)
class DocumentSection:
    key: str
    heading: str
    entries: tuple[DocumentEntry, ...]


@dataclass(frozen=True)
class ResumeDocument:
    """Everything a renderer is allowed to know.

    Deliberately small. There is no profile, no job, no evaluation and no
    contact block: a renderer that cannot see them cannot leak them into a
    document, and cannot be tempted to fill a gap with something plausible.
    """

    resume_id: str
    resume_version: int
    sections: tuple[DocumentSection, ...]

    @property
    def entry_count(self) -> int:
        return sum(len(section.entries) for section in self.sections)

    def all_text(self) -> tuple[str, ...]:
        """Every renderable string, for the no-fabrication assertion."""
        return tuple(entry.text for section in self.sections for entry in section.entries)


def _section_sort_key(key: str) -> tuple[int, str]:
    try:
        return (SECTION_ORDER.index(key), "")
    except ValueError:
        return (len(SECTION_ORDER), key)


def _heading(key: str) -> str:
    return SECTION_HEADINGS.get(key, key.replace("_", " ").strip().title())


def build_document(
    *, resume_id: str, resume_version: int, content: dict[str, Any]
) -> ResumeDocument:
    """Turn persisted content into the canonical document.

    Deterministic by construction: sections are sorted by the fixed order then
    by key, entries by `order` then by index, and an empty section is dropped
    only when it has no entries at all. Two calls on equal content produce
    equal documents, which is the foundation the byte-identity requirement
    rests on - if this step were order-dependent, no amount of care in the
    renderers could recover it.
    """
    if not isinstance(content, dict):
        raise MalformedResumeContentError("resume content must be an object")

    sections: list[DocumentSection] = []
    for key in sorted(content, key=_section_sort_key):
        raw = content[key]
        if not isinstance(raw, dict):
            raise MalformedResumeContentError(f"section {key!r} must be an object")

        entries: list[DocumentEntry] = []
        for index in raw:
            item = raw[index]
            if not isinstance(item, dict):
                raise MalformedResumeContentError(f"entry {key}/{index} must be an object")
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                raise MalformedResumeContentError(f"entry {key}/{index} has no text")
            order = item.get("order")
            if not isinstance(order, int):
                # Fall back to the mapping key, which build_content guarantees
                # is the same integer. Guessing a position would reorder a
                # human-approved document.
                try:
                    order = int(index)
                except (TypeError, ValueError):
                    raise MalformedResumeContentError(
                        f"entry {key}/{index} has no usable order"
                    ) from None
            fact_ids = item.get("fact_ids") or []
            if not isinstance(fact_ids, list):
                raise MalformedResumeContentError(f"entry {key}/{index} has malformed fact_ids")
            entries.append(
                DocumentEntry(
                    text=text,
                    order=order,
                    fact_ids=tuple(str(f) for f in fact_ids),
                )
            )

        if entries:
            entries.sort(key=lambda e: (e.order, e.text))
            sections.append(
                DocumentSection(key=key, heading=_heading(key), entries=tuple(entries))
            )

    return ResumeDocument(
        resume_id=str(resume_id),
        resume_version=int(resume_version),
        sections=tuple(sections),
    )
