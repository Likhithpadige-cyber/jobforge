"""Deterministic requirement extraction (AGENT_DESIGN.md §3.3, step A1).

Parses a job description into structured requirements so that step A2 has a
concrete list to match facts against, rather than a wall of prose.

**No LLM call exists in this module.** A1 is specified as deterministic, with
a model used *only* when the parser's confidence is low. This module reports
that confidence and sets `needs_llm_fallback`; it does not invoke anything.

Why deterministic matters here beyond cost: this is where attacker-controlled
text becomes structure. A parser that cannot be talked into inventing a
requirement is one fewer place an injection can act, and every requirement
produced here is a literal substring of the posting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class Necessity(StrEnum):
    REQUIRED = "required"
    PREFERRED = "preferred"


class RequirementKind(StrEnum):
    SKILL = "skill"
    CERTIFICATION = "certification"
    EDUCATION = "education"
    EXPERIENCE = "experience"
    RESPONSIBILITY = "responsibility"


#: Section headings, mapped to the necessity they imply.
SECTION_HEADINGS: tuple[tuple[re.Pattern[str], Necessity | None], ...] = (
    (re.compile(r"^\s*(minimum|basic|required|must[\s-]?have)s?\b.*", re.I), Necessity.REQUIRED),
    (re.compile(r"^\s*(requirements?|qualifications?)\b.*", re.I), Necessity.REQUIRED),
    (re.compile(r"^\s*(what you.{0,10}ll need|who you are)\b.*", re.I), Necessity.REQUIRED),
    (
        re.compile(r"^\s*(preferred|desired|nice[\s-]?to[\s-]?have|bonus|plus)\b.*", re.I),
        Necessity.PREFERRED,
    ),
    (re.compile(r"^\s*(responsibilities|what you.{0,10}ll do|the role|duties)\b.*", re.I), None),
    # Sections that contain no requirements at all.
    (
        re.compile(
            r"^\s*(benefits?|perks?|compensation|about (us|the company)|"
            r"equal opportunity|eeo|how to apply)\b.*",
            re.I,
        ),
        None,
    ),
)

NON_REQUIREMENT_SECTIONS = re.compile(
    r"^\s*(benefits?|perks?|compensation|about (us|the company)|"
    r"equal opportunity|eeo|how to apply|salary)\b",
    re.I,
)
RESPONSIBILITY_SECTION = re.compile(
    r"^\s*(responsibilities|what you.{0,10}ll do|the role|duties)\b", re.I
)

#: Inline cues that override the section's necessity.
PREFERRED_CUES = re.compile(
    r"\b(preferred|a plus|nice to have|bonus|desirable|ideally|would be great|"
    r"not required|optional)\b",
    re.I,
)
REQUIRED_CUES = re.compile(
    r"\b(must have|must be|required|requires|essential|mandatory|minimum of)\b", re.I
)

BULLET = re.compile(r"^\s*(?:[-*•·▪◦‣]|\d+[.)]|\(\d+\))\s+(.*\S)\s*$")

YEARS = re.compile(
    r"(\d+)\s*(?:\+|plus)?\s*(?:-|\u2013|to)?\s*(\d+)?\s*\+?\s*(?:years?|yrs?)\b", re.I
)

CERTIFICATION_CUES = re.compile(
    r"\b(certifi(ed|cation)|credential|aws\s+certified|azure\s+certified|"
    r"cka|ckad|rhcsa|rhce|comptia|security\+|pmp|cissp|terraform\s+associate)\b",
    re.I,
)
EDUCATION_CUES = re.compile(
    r"\b(bachelor|master|phd|doctorate|b\.?s\.?|m\.?s\.?|degree|diploma|"
    r"computer science|engineering degree)\b",
    re.I,
)
EXPERIENCE_CUES = re.compile(
    r"\b(years?|yrs?|experience|background|track record|hands[\s-]?on|worked with)\b", re.I
)
RESPONSIBILITY_VERBS = re.compile(
    r"^\s*(you will|design|build|maintain|own|lead|collaborate|partner|drive|"
    r"support|manage|operate|deliver|participate|contribute|work with)\b",
    re.I,
)

#: Boilerplate that is not a requirement even when it appears as a bullet.
BOILERPLATE = re.compile(
    r"\b(equal opportunity|without regard to|drug[\s-]free|background check|"
    r"e-?verify|competitive salary|health insurance|401\(?k\)?|paid time off|"
    r"we are an|reasonable accommodation)\b",
    re.I,
)

MIN_REQUIREMENT_LENGTH = 8
MAX_REQUIREMENT_LENGTH = 400


@dataclass
class Requirement:
    """One extracted requirement. `text` is always a substring of the posting."""

    text: str
    necessity: Necessity
    kind: RequirementKind
    years_min: int | None = None
    years_max: int | None = None
    section: str | None = None

    @property
    def required(self) -> bool:
        return self.necessity is Necessity.REQUIRED

    def as_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "necessity": str(self.necessity),
            "kind": str(self.kind),
            "years_min": self.years_min,
            "years_max": self.years_max,
            "section": self.section,
        }


@dataclass
class ExtractionResult:
    requirements: list[Requirement] = field(default_factory=list)
    confidence: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def needs_llm_fallback(self) -> bool:
        """A1 defers to a model only when the parser did poorly.

        The threshold is deliberately generous: an unnecessary LLM call costs
        a fraction of a cent, while a silently empty requirement list would
        make every job look like a perfect match to step A3.
        """
        return self.confidence < 0.5

    @property
    def required(self) -> list[Requirement]:
        return [r for r in self.requirements if r.required]

    @property
    def preferred(self) -> list[Requirement]:
        return [r for r in self.requirements if not r.required]

    def as_dict(self) -> dict[str, object]:
        return {
            "requirements": [r.as_dict() for r in self.requirements],
            "confidence": round(self.confidence, 3),
            "needs_llm_fallback": self.needs_llm_fallback,
            "counts": {
                "total": len(self.requirements),
                "required": len(self.required),
                "preferred": len(self.preferred),
            },
            "warnings": self.warnings,
        }


def _parse_years(text: str) -> tuple[int | None, int | None]:
    match = YEARS.search(text)
    if not match:
        return None, None
    low = int(match.group(1))
    high = int(match.group(2)) if match.group(2) else None
    if high is not None and high < low:
        low, high = high, low
    # A posting demanding 60 years is a parse error, not a requirement.
    if low > 50 or (high is not None and high > 50):
        return None, None
    return low, high


def _classify(text: str, in_responsibility_section: bool) -> RequirementKind:
    if CERTIFICATION_CUES.search(text):
        return RequirementKind.CERTIFICATION
    if EDUCATION_CUES.search(text):
        return RequirementKind.EDUCATION
    if in_responsibility_section or RESPONSIBILITY_VERBS.match(text):
        return RequirementKind.RESPONSIBILITY
    if EXPERIENCE_CUES.search(text):
        return RequirementKind.EXPERIENCE
    return RequirementKind.SKILL


def _necessity(text: str, section_default: Necessity) -> Necessity:
    """Inline cues beat the section heading.

    "AWS experience (Kubernetes a plus)" inside a Requirements section is
    still a preferred item for the Kubernetes half, and treating it as
    required would manufacture a gap that the posting did not state.
    """
    if PREFERRED_CUES.search(text):
        return Necessity.PREFERRED
    if REQUIRED_CUES.search(text):
        return Necessity.REQUIRED
    return section_default


def _is_heading(line: str) -> bool:
    stripped = line.strip().rstrip(":")
    if not stripped or len(stripped) > 60:
        return False
    if BULLET.match(line):
        return False
    return line.rstrip().endswith(":") or (stripped.isupper() and len(stripped.split()) <= 6)


def extract_requirements(description: str | None) -> ExtractionResult:
    """Parse a job description into structured requirements. Pure.

    Every returned `text` is a substring of the input: this function selects
    and labels, it never rephrases or infers.
    """
    result = ExtractionResult()
    if not description or not description.strip():
        result.warnings.append("empty description")
        return result

    lines = description.splitlines()
    section_name: str | None = None
    section_default = Necessity.REQUIRED
    in_responsibility = False
    skip_section = False
    bullets_seen = 0

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue

        if _is_heading(line):
            section_name = line.strip().rstrip(":")
            skip_section = bool(NON_REQUIREMENT_SECTIONS.match(section_name))
            in_responsibility = bool(RESPONSIBILITY_SECTION.match(section_name))
            # A responsibility describes the job, not a qualification the
            # candidate must already hold. Counting "design and maintain cloud
            # infrastructure" as a REQUIRED requirement would make A3 score it
            # as an unmet gap, penalising the candidate for not having already
            # done the job they are applying for. An explicit inline "must
            # have" still promotes it.
            section_default = Necessity.PREFERRED if in_responsibility else Necessity.REQUIRED
            if not in_responsibility:
                for pattern, necessity in SECTION_HEADINGS:
                    if pattern.match(section_name):
                        if necessity is not None:
                            section_default = necessity
                        break
            continue

        if skip_section:
            continue

        bullet = BULLET.match(line)
        if not bullet:
            continue
        bullets_seen += 1

        text = bullet.group(1).strip()
        if len(text) < MIN_REQUIREMENT_LENGTH or len(text) > MAX_REQUIREMENT_LENGTH:
            continue
        if BOILERPLATE.search(text):
            continue

        years_min, years_max = _parse_years(text)
        result.requirements.append(
            Requirement(
                text=text,
                necessity=_necessity(text, section_default),
                kind=_classify(text, in_responsibility),
                years_min=years_min,
                years_max=years_max,
                section=section_name,
            )
        )

    result.confidence = _confidence(result.requirements, bullets_seen, description)
    if not result.requirements:
        result.warnings.append("no requirements extracted; posting may be unstructured prose")
    if not result.required:
        result.warnings.append("no required requirements identified")
    return result


def _confidence(requirements: list[Requirement], bullets_seen: int, description: str) -> float:
    """How much to trust this parse, in [0, 1].

    Low confidence is not a failure - it is the signal that A1 should defer
    to a model. Reporting it honestly is the whole point.
    """
    if not requirements:
        return 0.0

    score = 0.3  # something was extracted at all

    # Bullets are the strongest structural signal.
    if bullets_seen >= 3:
        score += 0.3
    elif bullets_seen >= 1:
        score += 0.15

    # A posting that separates required from preferred was parsed properly.
    if any(r.required for r in requirements) and any(not r.required for r in requirements):
        score += 0.2
    elif any(r.required for r in requirements):
        score += 0.1

    # A spread of kinds suggests classification worked rather than defaulting.
    if len({r.kind for r in requirements}) >= 3:
        score += 0.1

    # If most of the posting never became a requirement, we probably missed
    # structure the model would catch.
    covered = sum(len(r.text) for r in requirements)
    if covered < len(description) * 0.1:
        score -= 0.2

    return max(0.0, min(1.0, score))
