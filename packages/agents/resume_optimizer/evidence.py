"""A2 — requirement-to-profile evidence mapping.

Takes the requirements A1 read out of a posting and the candidate's VERIFIED
facts, and decides which facts support which requirement.

**No LLM. No embeddings. No inference.** Matching is explicit lexical
containment over a small, hand-written alias table. That is a deliberate
limitation rather than a shortcut: a fuzzy matcher that decides "cloud
operations" is close enough to "Kubernetes" would be manufacturing evidence,
and every downstream control in this system exists to stop exactly that.

Three properties the rest of the architecture depends on:

* **Verified facts only.** Draft and retired facts can never support a match.
  The filter is `FactRepository.list_citable`, the same one the resume
  generator uses, so "citable" means one thing everywhere.
* **Every match cites real fact ids.** A status of `met` with no fact behind
  it is downgraded, because `RequirementMatch.counts_as_met` already scores
  an uncited claim as zero and the two must not disagree.
* **Requirement text is data.** It is normalised and regex-escaped before it
  touches a pattern, never formatted into SQL, and never evaluated. A posting
  that says "ignore previous instructions" is matched as the literal words it
  contains and nothing else.

The pgvector `embedding` column exists but is never populated, so semantic
retrieval is not available and is not faked.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any
from uuid import UUID

from packages.agents.resume_optimizer.prefilter import MatchStatus, RequirementMatch
from packages.agents.resume_optimizer.requirements import Necessity, Requirement

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------- vocabulary

#: Words carrying no matching signal. Stripped from requirement text so that
#: "3+ years of hands-on AWS experience" reduces to the term that matters.
STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "or", "the", "of", "in", "on", "at", "to", "for",
        "with", "within", "using", "use", "used", "strong", "solid", "good",
        "excellent", "deep", "proven", "demonstrated", "hands", "hands-on",
        "experience", "experienced", "background", "knowledge", "understanding",
        "familiarity", "familiar", "working", "work", "ability", "able",
        "skills", "skill", "years", "year", "yrs", "yr", "plus", "minimum",
        "least", "must", "have", "has", "is", "are", "be", "you", "your", "we",
        "our", "including", "include", "such", "as", "etc", "e", "g", "ie",
        "preferred", "required", "nice", "bonus", "desirable", "ideally",
        "equivalent", "related", "similar", "environment", "environments",
        "practices", "practice", "level", "senior", "junior", "mid", "team",
        "teams", "across", "large", "scale", "modern", "complex", "real",
        "world", "based", "driven", "focused", "strongly", "highly", "very",
        "will", "can", "should", "would", "this", "that", "these", "those",
        "it", "its", "from", "by", "into", "over", "under", "about",
    }
)

#: Deterministic synonyms. Every entry is hand-written; nothing is inferred.
#: Key is the canonical term, values are forms treated as the same thing.
ALIASES: dict[str, tuple[str, ...]] = {
    "aws": ("amazon web services", "amazon aws"),
    "gcp": ("google cloud", "google cloud platform"),
    "azure": ("microsoft azure",),
    "kubernetes": ("k8s",),
    "terraform": ("hashicorp terraform",),
    "ci/cd": ("cicd", "ci cd", "continuous integration", "continuous delivery",
              "continuous deployment"),
    "iac": ("infrastructure as code",),
    "postgresql": ("postgres",),
    "javascript": ("js",),
    "typescript": ("ts",),
    "python": (),
    "linux": ("rhel", "ubuntu", "centos"),
    "docker": ("containerisation", "containerization", "containers"),
    "sre": ("site reliability engineering", "site reliability"),
    "observability": ("monitoring",),
}

#: Multi-word terms that must be matched whole rather than token by token.
PHRASES: tuple[str, ...] = tuple(
    sorted(
        {
            *(alias for aliases in ALIASES.values() for alias in aliases if " " in alias),
            "site reliability engineering",
            "infrastructure as code",
            "continuous integration",
            "continuous delivery",
            "version control",
            "load balancing",
            "disaster recovery",
            "incident response",
            "cost optimisation",
            "cost optimization",
            "access management",
        },
        key=len,
        reverse=True,
    )
)

#: Reverse index: every alias form -> its canonical term.
_ALIAS_TO_CANONICAL: dict[str, str] = {
    alias: canonical
    for canonical, aliases in ALIASES.items()
    for alias in (canonical, *aliases)
}

MIN_TERM_LENGTH = 2


# ----------------------------------------------------------------- helpers
def normalise(text: str) -> str:
    """Lowercase, de-accent and reduce punctuation to spaces.

    `+`, `#`, `/` and `.` survive so that c++, c#, ci/cd and .net stay intact.
    """
    folded = "".join(
        ch
        for ch in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(ch)
    ).lower()
    cleaned = re.sub(r"[^a-z0-9+#/.\s-]", " ", folded)
    return re.sub(r"\s+", " ", cleaned).strip()


def _canonical(term: str) -> str:
    return _ALIAS_TO_CANONICAL.get(term, term)


def anchor_terms(requirement_text: str) -> list[str]:
    """The terms a requirement actually turns on, in order, deduplicated.

    Returns an empty list when a requirement carries no matchable term - for
    example "Must have the right attitude". Such a row is reported as NOT_MET
    with `assessable=False`, so a reader can tell "nothing supports this" from
    "nothing here could be checked".
    """
    text = normalise(requirement_text)
    if not text:
        return []

    found: list[str] = []
    remaining = text

    # Multi-word phrases first, so "continuous integration" is not split into
    # two meaningless tokens.
    for phrase in PHRASES:
        if re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", remaining):
            found.append(_canonical(phrase))
            remaining = remaining.replace(phrase, " ")

    for raw in remaining.split():
        token = raw.strip("-./")
        # "3+", "5-7" and "10" carry no matching signal: a year count is
        # already held structurally on the requirement itself.
        if token.rstrip("+").replace("-", "").isdigit():
            continue
        if len(token) < MIN_TERM_LENGTH:
            continue
        if token in STOPWORDS:
            continue
        found.append(_canonical(token))

    ordered: list[str] = []
    for term in found:
        if term not in ordered:
            ordered.append(term)
    return ordered


def _forms(term: str) -> tuple[str, ...]:
    """Every spelling of a term that counts as the same thing."""
    canonical = _canonical(term)
    return (canonical, *ALIASES.get(canonical, ()))


def term_in_text(term: str, haystack: str) -> bool:
    """Whole-term containment. `re.escape` keeps requirement text inert."""
    for form in _forms(term):
        if re.search(rf"(?<!\w){re.escape(form)}(?!\w)", haystack):
            return True
    return False


# -------------------------------------------------------------- fact shape
@dataclass(frozen=True)
class FactView:
    """The parts of a verified fact that matching needs.

    A plain value object so the mapper stays pure and testable without a
    database or the ORM.
    """

    id: UUID
    text: str
    structured: dict[str, Any] = field(default_factory=dict)

    @property
    def searchable(self) -> str:
        return normalise(self.text)

    def years_of_evidence(self, today: date | None = None) -> float | None:
        """Duration this fact demonstrates, from its own structured dates.

        Returns None when the fact records no dates - absence of a date is
        never read as a short tenure.
        """
        start = self.structured.get("start_date")
        end = self.structured.get("end_date")
        if not start:
            return None
        try:
            start_date = date.fromisoformat(str(start))
            end_date = date.fromisoformat(str(end)) if end else (today or date.today())  # noqa: DTZ011
        except (ValueError, TypeError):
            return None
        if end_date < start_date:
            return None
        return round((end_date - start_date).days / 365.25, 2)


def fact_views(facts: list[Any]) -> list[FactView]:
    """Adapt ORM `ProfileFact` rows into the value object above.

    Callers are expected to have filtered to verified facts already; this
    does not re-check, because silently dropping rows would hide a caller's
    mistake rather than surface it.
    """
    return [
        FactView(id=f.id, text=f.text, structured=dict(f.structured or {}))
        for f in facts
    ]


# ------------------------------------------------------------------ result
#: Why a row carries no evidence. Metadata only - never a status. The
#: scoring enum (MatchStatus) is a fixed contract shared with A3 and A5;
#: widening it here would change how every score is computed.
NO_MATCHABLE_TERMS = "no_matchable_terms"
NO_SUPPORTING_FACT = "no_supporting_fact"


@dataclass
class RequirementEvidence:
    requirement_text: str
    necessity: str
    kind: str
    status: MatchStatus
    fact_ids: list[str]
    evidence: list[dict[str, str]]
    matched_terms: list[str]
    unmatched_terms: list[str]
    years_required: int | None = None
    years_evidenced: float | None = None
    #: False when the requirement carried no matchable term at all. The
    #: status is still NOT_MET - nothing verified supports it - but this
    #: records that the absence is a limit of lexical matching rather than
    #: a gap in the candidate.
    assessable: bool = True
    no_evidence_reason: str | None = None

    @property
    def required(self) -> bool:
        return self.necessity == Necessity.REQUIRED.value

    def as_dict(self) -> dict[str, Any]:
        return {
            "requirement_text": self.requirement_text,
            "necessity": self.necessity,
            "kind": self.kind,
            "status": str(self.status),
            "fact_ids": list(self.fact_ids),
            "evidence": [dict(item) for item in self.evidence],
            "matched_terms": list(self.matched_terms),
            "unmatched_terms": list(self.unmatched_terms),
            "years_required": self.years_required,
            "years_evidenced": self.years_evidenced,
            "assessable": self.assessable,
            "no_evidence_reason": self.no_evidence_reason,
        }


@dataclass
class MatchMatrix:
    rows: list[RequirementEvidence] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        counts = {str(status): 0 for status in MatchStatus}
        for row in self.rows:
            counts[str(row.status)] += 1
        return {
            "total": len(self.rows),
            **counts,
            # Not a status: a subset of not_met, surfaced so the matrix can
            # distinguish "no support" from "nothing checkable".
            "unassessable": sum(1 for row in self.rows if not row.assessable),
        }

    def as_dict(self) -> dict[str, Any]:
        """The JSON persisted into `JobEvaluation.match_matrix`.

        `requirements` and `status` are named to match what A5 already reads
        when it counts matched requirements.
        """
        return {
            "requirements": [row.as_dict() for row in self.rows],
            "summary": self.summary,
            "method": "deterministic-lexical",
            "evidence_source": "verified_profile_facts",
        }

    def to_requirement_matches(self) -> list[RequirementMatch]:
        """Hand the matrix to A3 in the shape `compute_score` already takes."""
        return [
            RequirementMatch(
                text=row.requirement_text,
                required=row.required,
                status=row.status,
                fact_ids=list(row.fact_ids),
            )
            for row in self.rows
        ]


# ------------------------------------------------------------------- A2
def map_requirement(
    requirement: Requirement, facts: list[FactView], today: date | None = None
) -> RequirementEvidence:
    """Decide which verified facts support one requirement.

    The rules, in order:

    * no matchable term in the requirement  -> NOT_MET, assessable=False
    * no term found in any fact             -> NOT_MET, assessable=True
    * some terms found                      -> PARTIALLY_MET
    * all terms found                       -> MET, unless the requirement
      states a year count that no fact's own dates support, in which case it
      is capped at PARTIALLY_MET

    That last cap is the conservative half of the design: "3+ years of AWS"
    with a fact proving eight months of AWS is genuine partial support, and
    calling it met would overstate the candidate.
    """
    anchors = anchor_terms(requirement.text)

    if not anchors:
        return RequirementEvidence(
            requirement_text=requirement.text,
            necessity=str(requirement.necessity),
            kind=str(requirement.kind),
            status=MatchStatus.NOT_MET,
            fact_ids=[],
            evidence=[],
            matched_terms=[],
            unmatched_terms=[],
            years_required=requirement.years_min,
            assessable=False,
            no_evidence_reason=NO_MATCHABLE_TERMS,
        )

    supporting: dict[str, FactView] = {}
    matched: list[str] = []
    unmatched: list[str] = []

    for term in anchors:
        hits = [fact for fact in facts if term_in_text(term, fact.searchable)]
        if hits:
            matched.append(term)
            for fact in hits:
                supporting[str(fact.id)] = fact
        else:
            unmatched.append(term)

    # Deterministic ordering: same inputs always serialise identically.
    fact_ids = sorted(supporting)
    evidence = [
        # Verbatim stored text only. Nothing is summarised, trimmed to a
        # phrase, or rewritten - that would be synthesising evidence.
        {"fact_id": fact_id, "text": supporting[fact_id].text}
        for fact_id in fact_ids
    ]

    years_evidenced: float | None = None
    if supporting:
        durations = [
            value
            for value in (
                supporting[fact_id].years_of_evidence(today) for fact_id in fact_ids
            )
            if value is not None
        ]
        years_evidenced = max(durations) if durations else None

    reason: str | None = None
    if not matched:
        status = MatchStatus.NOT_MET
        reason = NO_SUPPORTING_FACT
    elif unmatched:
        status = MatchStatus.PARTIALLY_MET
    else:
        status = MatchStatus.MET
        if requirement.years_min is not None and (
            years_evidenced is None or years_evidenced < requirement.years_min
        ):
            status = MatchStatus.PARTIALLY_MET

    # A match with no citation cannot be a match. This mirrors
    # RequirementMatch.counts_as_met, which already scores uncited claims at
    # zero; letting the two disagree would be worse than either rule alone.
    if status in (MatchStatus.MET, MatchStatus.PARTIALLY_MET) and not fact_ids:
        status = MatchStatus.NOT_MET
        reason = NO_SUPPORTING_FACT

    return RequirementEvidence(
        requirement_text=requirement.text,
        necessity=str(requirement.necessity),
        kind=str(requirement.kind),
        status=status,
        fact_ids=fact_ids,
        evidence=evidence,
        matched_terms=matched,
        unmatched_terms=unmatched,
        years_required=requirement.years_min,
        years_evidenced=years_evidenced,
        assessable=True,
        no_evidence_reason=reason,
    )


def build_match_matrix(
    requirements: list[Requirement],
    facts: list[FactView],
    today: date | None = None,
) -> MatchMatrix:
    """Map every requirement. Pure: no I/O, no model, no randomness.

    An empty requirement list yields an empty matrix. An empty fact list
    yields a matrix of honest NOT_MET rows rather than nothing.
    """
    return MatchMatrix(
        rows=[map_requirement(req, facts, today) for req in requirements]
    )


# ------------------------------------------------------ database adapter
async def build_match_matrix_for_profile(
    session: AsyncSession,
    profile_id: UUID,
    requirements: list[Requirement],
    today: date | None = None,
) -> MatchMatrix:
    """Load VERIFIED facts and map the requirements against them.

    The only database-touching function here, and it reads. It does not
    commit, create facts, mutate facts, or bump usage counters - A2 observes
    the profile, it does not change it.
    """
    from packages.core.repositories import FactRepository

    citable = await FactRepository(session).list_citable(profile_id)
    return build_match_matrix(requirements, fact_views(citable), today)


def requirements_from_payload(payload: dict[str, Any] | None) -> list[Requirement]:
    """Rebuild A1 `Requirement` objects from the stored `jobs.requirements`.

    Unknown necessity or kind values are skipped rather than coerced: a row
    we cannot read is not a row we should guess at.
    """
    from packages.agents.resume_optimizer.requirements import RequirementKind

    rows = (payload or {}).get("requirements") or []
    rebuilt: list[Requirement] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("text"):
            continue
        try:
            necessity = Necessity(row.get("necessity", "required"))
            kind = RequirementKind(row.get("kind", "skill"))
        except ValueError:
            continue
        rebuilt.append(
            Requirement(
                text=str(row["text"]),
                necessity=necessity,
                kind=kind,
                years_min=row.get("years_min"),
                years_max=row.get("years_max"),
                section=row.get("section"),
            )
        )
    return rebuilt
