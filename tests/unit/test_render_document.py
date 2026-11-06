"""The canonical document: deterministic, lossless, and unable to invent text.

Synthetic content only. Nothing in this file resembles a real resume.
"""

from __future__ import annotations

import pytest

from packages.services.rendering.document import (
    MalformedResumeContentError,
    ResumeDocument,
    build_document,
)

pytestmark = pytest.mark.unit

RESUME_ID = "0192f000-0000-7000-8000-000000000001"

#: Deliberately out of order, with an unknown section, to prove ordering is
#: computed rather than inherited from the mapping.
CONTENT = {
    "experience": {
        "2": {"text": "Third approved line.", "fact_ids": ["f3"], "order": 2},
        "0": {"text": "First approved line.", "fact_ids": ["f1"], "order": 0},
        "1": {"text": "Second approved line.", "fact_ids": ["f2"], "order": 1},
    },
    "summary": {"0": {"text": "A synthetic summary.", "fact_ids": ["f0"], "order": 0}},
    "wild_section": {"0": {"text": "An unexpected section.", "fact_ids": ["f4"], "order": 0}},
}


def doc() -> ResumeDocument:
    return build_document(resume_id=RESUME_ID, resume_version=3, content=CONTENT)


# ------------------------------------------------------------------ ordering
def test_sections_follow_the_template_order() -> None:
    assert [s.key for s in doc().sections] == ["summary", "experience", "wild_section"]


def test_an_unknown_section_is_kept_and_sorted_last() -> None:
    """Dropping a section would remove a line a human approved.

    Silently losing approved content is worse than rendering it under a
    guessed heading, so an unrecognised section sorts after the known ones
    rather than disappearing.
    """
    sections = doc().sections
    assert sections[-1].key == "wild_section"
    assert sections[-1].heading == "Wild Section"


def test_entries_are_ordered_by_order_not_by_mapping_order() -> None:
    experience = next(s for s in doc().sections if s.key == "experience")
    assert [e.text for e in experience.entries] == [
        "First approved line.",
        "Second approved line.",
        "Third approved line.",
    ]


def test_building_twice_gives_equal_documents() -> None:
    """The foundation the byte-identity requirement stands on.

    If this step were order-dependent, no care inside a renderer could
    recover determinism.
    """
    assert doc() == doc()


def test_key_insertion_order_does_not_change_the_document() -> None:
    shuffled = {
        "wild_section": CONTENT["wild_section"],
        "experience": {
            "1": CONTENT["experience"]["1"],
            "2": CONTENT["experience"]["2"],
            "0": CONTENT["experience"]["0"],
        },
        "summary": CONTENT["summary"],
    }
    assert build_document(resume_id=RESUME_ID, resume_version=3, content=shuffled) == doc()


# -------------------------------------------------------------- no invention
def test_every_rendered_string_comes_from_the_input() -> None:
    """The anti-fabrication property, asserted rather than asserted-to.

    Headings are template chrome; every other string must be a value the
    persisted content already held.
    """
    source = {entry["text"] for section in CONTENT.values() for entry in section.values()}
    assert set(doc().all_text()) == source


def test_text_preserves_spacing_and_normalises_tabs() -> None:
    odd = {"summary": {"0": {"text": "  spaced   out\ttext  ", "fact_ids": [], "order": 0}}}
    built = build_document(resume_id=RESUME_ID, resume_version=1, content=odd)
    assert built.sections[0].entries[0].text == "  spaced   out text  "


def test_fact_ids_are_carried_through() -> None:
    """Provenance survives rendering; a rendered line still names its facts."""
    experience = next(s for s in doc().sections if s.key == "experience")
    assert [e.fact_ids for e in experience.entries] == [("f1",), ("f2",), ("f3",)]


# ------------------------------------------------------------------ refusals
@pytest.mark.parametrize(
    "content",
    [
        {"summary": "not-an-object"},
        {"summary": {"0": "not-an-object"}},
        {"summary": {"0": {"text": "", "order": 0}}},
        {"summary": {"0": {"text": "   ", "order": 0}}},
        {"summary": {"0": {"order": 0}}},
        {"summary": {"0": {"text": "ok", "order": 0, "fact_ids": "not-a-list"}}},
        {"summary": {"nope": {"text": "ok"}}},
    ],
)
def test_malformed_content_is_refused_not_skipped(content: object) -> None:
    """A skipped entry is a missing line nobody notices.

    The checksum would be perfectly stable over the wrong document, which is
    why every one of these raises instead of dropping the entry.
    """
    with pytest.raises(MalformedResumeContentError):
        build_document(resume_id=RESUME_ID, resume_version=1, content=content)


def test_non_object_content_is_refused() -> None:
    with pytest.raises(MalformedResumeContentError):
        build_document(resume_id=RESUME_ID, resume_version=1, content=[])  # type: ignore[arg-type]


def test_an_order_taken_from_the_index_still_works() -> None:
    """build_content always writes `order`, but the index is the same integer.

    Falling back to it is safe; guessing a position would not be.
    """
    built = build_document(
        resume_id=RESUME_ID,
        resume_version=1,
        content={"summary": {"1": {"text": "b"}, "0": {"text": "a"}}},
    )
    assert [e.text for e in built.sections[0].entries] == ["a", "b"]


def test_an_empty_section_is_dropped() -> None:
    built = build_document(
        resume_id=RESUME_ID,
        resume_version=1,
        content={"summary": {"0": {"text": "a", "order": 0}}, "skills": {}},
    )
    assert [s.key for s in built.sections] == ["summary"]
