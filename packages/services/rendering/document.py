"""The canonical document M8 renders. Nothing here invents content.

Generated prose comes from `resumes.content`; contact details and structured
record headings come from the exact owner-approved export snapshot. This
module reorders and groups; it never rewrites claims, summarises or infers.
This pure, dependency-free step makes "M8 cannot fabricate" a property that is
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
    heading: bool = False


@dataclass(frozen=True)
class DocumentSection:
    key: str
    heading: str
    entries: tuple[DocumentEntry, ...]


@dataclass(frozen=True)
class ResumeDocument:
    """Everything a renderer is allowed to know.

    Contact lines and structured headings come only from the exact owner-
    approved export snapshot. No renderer receives a live profile, job or
    evaluation, and generated claim text and its fact citations stay intact.
    """

    resume_id: str
    resume_version: int
    sections: tuple[DocumentSection, ...]
    contact_lines: tuple[str, ...] = ()

    @property
    def entry_count(self) -> int:
        return sum(len(section.entries) for section in self.sections)

    def all_text(self) -> tuple[str, ...]:
        """Every renderable string, for the no-fabrication assertion."""
        return self.contact_lines + tuple(
            entry.text for section in self.sections for entry in section.entries
        )


def _section_sort_key(key: str) -> tuple[int, str]:
    try:
        return (SECTION_ORDER.index(key), "")
    except ValueError:
        return (len(SECTION_ORDER), key)


def _heading(key: str) -> str:
    return SECTION_HEADINGS.get(key, key.replace("_", " ").strip().title())


def build_document(
    *,
    resume_id: str,
    resume_version: int,
    content: dict[str, Any],
    export_data: dict[str, Any] | None = None,
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
            sections.append(DocumentSection(key=key, heading=_heading(key), entries=tuple(entries)))

    contact_lines: tuple[str, ...] = ()
    if export_data is not None:
        if export_data.get("schema_version") != 1 or not export_data.get("contact", {}).get("name"):
            raise MalformedResumeContentError(
                "export snapshot needs schema version and candidate name"
            )
        contact = export_data["contact"]
        contact_lines = tuple(
            str(contact[k]) for k in ("name", "email", "phone", "location") if contact.get(k)
        ) + tuple(str(v) for _, v in sorted(export_data.get("links", {}).items()))
        section_map = {section.key: section for section in sections}
        for key in ("experience", "education", "certifications"):
            originals = section_map.get(key)
            entries = list(originals.entries) if originals else []
            records = [r for r in export_data.get("records", []) if r["section"] == key]
            grouped: dict[str, list[DocumentEntry]] = {r["source_id"]: [] for r in records}
            for entry in entries:
                matches = [r for r in records if set(r["fact_ids"]) & set(entry.fact_ids)]
                if len(matches) != 1:
                    raise MalformedResumeContentError(
                        f"{key} claim needs exactly one active source record; "
                        "regenerate or correct evidence"
                    )
                grouped[matches[0]["source_id"]].append(entry)
            structured: list[DocumentEntry] = []
            for record in records:
                fields = record["fields"]
                field_order = {
                    "experience": ("title", "company_name", "start_date", "end_date", "location"),
                    "education": (
                        "degree",
                        "field_of_study",
                        "institution",
                        "start_date",
                        "end_date",
                        "location",
                    ),
                    "certifications": (
                        "name",
                        "issuer",
                        "issued_date",
                        "expires_date",
                        "credential_url",
                    ),
                }[key]
                label = " | ".join(str(fields[f]) for f in field_order if fields.get(f))
                if not label:
                    raise MalformedResumeContentError("empty structured source record")
                structured.append(
                    DocumentEntry(label, len(structured), tuple(record["fact_ids"]), heading=True)
                )
                structured.extend(grouped[record["source_id"]])
            if structured:
                section_map[key] = DocumentSection(key, _heading(key), tuple(structured))
        sections = sorted(section_map.values(), key=lambda section: _section_sort_key(section.key))

    return ResumeDocument(
        resume_id=str(resume_id),
        resume_version=int(resume_version),
        sections=tuple(sections),
        contact_lines=contact_lines,
    )
