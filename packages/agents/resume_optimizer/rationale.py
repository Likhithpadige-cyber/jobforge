"""A4 — deterministic rationale.

Turns an A2 match matrix and an A3 score into a short, honest explanation of
why a job scored what it scored.

**No LLM. No inference. No new facts.** Every sentence is assembled from
counts and text that already exist in the inputs, and every number in the
output is derived arithmetically from them. The rationale explains a
decision that has already been made; it never participates in making it.

Two consequences worth stating plainly:

* **Nothing is persisted twice.** `JobEvaluation.rationale` is a TEXT column,
  so only the prose goes in it. The structured form is a pure function of
  `match_matrix`, `score`, `recommendation` and `gaps` - all four already
  stored on the evaluation row - so it can be rebuilt at any time rather than
  duplicated into JSONB. No schema change is needed and none is made.

* **The rationale cannot flatter.** It reports met requirements only where
  the matrix cites fact ids, names every unmet required item, and always
  states when something could not be assessed. A rationale that reads better
  than the evidence would be the exact failure this architecture exists to
  prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from packages.agents.resume_optimizer.prefilter import ScoreBreakdown

#: How many requirement texts to name before summarising the remainder.
#: Keeps the prose bounded without truncating a sentence mid-word.
MAX_NAMED_ITEMS = 3

#: Band phrasing. Mirrors the thresholds in prefilter.BANDS; the wording is
#: about what the candidate should do, not how the system feels about it.
BAND_PHRASE: dict[str, str] = {
    "strong": "Strong match",
    "moderate": "Moderate match",
    "weak": "Weak match",
    "no": "Not a match",
}

MET_STATUSES = ("met", "partially_met")


def _rows(match_matrix: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = (match_matrix or {}).get("requirements") or []
    return [row for row in rows if isinstance(row, dict)]


def _name_list(items: list[str], limit: int = MAX_NAMED_ITEMS) -> str:
    """Join item texts, summarising anything beyond `limit`.

    The count in "and 4 more" is a real count of the remaining items, not an
    estimate.
    """
    if not items:
        return ""
    shown = items[:limit]
    remainder = len(items) - len(shown)
    if len(shown) == 1:
        joined = shown[0]
    elif len(shown) == 2:
        joined = f"{shown[0]} and {shown[1]}"
    else:
        joined = f"{', '.join(shown[:-1])} and {shown[-1]}"
    if remainder > 0:
        joined += f", and {remainder} more"
    return joined


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return singular if count == 1 else (plural or f"{singular}s")


@dataclass
class Rationale:
    """A structured explanation, plus the prose actually persisted."""

    headline: str
    strengths: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        """The text stored in `JobEvaluation.rationale`.

        Sentences in a fixed order - headline, strengths, gaps, caveats - so
        two evaluations of the same job read identically and can be diffed.
        """
        parts = [self.headline, *self.strengths, *self.gaps, *self.caveats]
        return " ".join(part.strip() for part in parts if part and part.strip())

    def as_dict(self) -> dict[str, Any]:
        return {
            "headline": self.headline,
            "strengths": list(self.strengths),
            "gaps": list(self.gaps),
            "caveats": list(self.caveats),
            "counts": dict(self.counts),
            "summary": self.summary,
            "method": "deterministic",
        }


def build_rationale(
    match_matrix: dict[str, Any] | None,
    *,
    score: Decimal | float,
    recommendation: str,
    gaps: list[str] | None = None,
) -> Rationale:
    """Explain a scored job. Pure: no I/O, no model, no randomness.

    Takes the persisted column values rather than an object, so a rationale
    can be rebuilt from a stored evaluation exactly as it was first written.
    """
    rows = _rows(match_matrix)
    score_value = Decimal(str(score))
    band = BAND_PHRASE.get(str(recommendation), "Match")

    required = [r for r in rows if r.get("necessity") == "required"]
    preferred = [r for r in rows if r.get("necessity") == "preferred"]

    def cited(candidates: list[dict[str, Any]], statuses: tuple[str, ...]) -> list[dict[str, Any]]:
        # A row only counts as supporting evidence if it actually cites a
        # fact. This mirrors RequirementMatch.counts_as_met, which scores an
        # uncited claim at zero - the prose must not disagree with the score.
        return [
            r for r in candidates
            if r.get("status") in statuses and (r.get("fact_ids") or [])
        ]

    met_required = cited(required, ("met",))
    partial_required = cited(required, ("partially_met",))
    met_preferred = cited(preferred, ("met", "partially_met"))

    unmet_required = [
        r for r in required
        if r.get("status") not in MET_STATUSES or not (r.get("fact_ids") or [])
    ]
    unassessable = [r for r in rows if r.get("assessable") is False]
    unmet_assessable = [r for r in unmet_required if r.get("assessable") is not False]

    counts = {
        "requirements": len(rows),
        "required": len(required),
        "preferred": len(preferred),
        "required_met": len(met_required),
        "required_partial": len(partial_required),
        "required_unmet": len(unmet_required),
        "preferred_met": len(met_preferred),
        "unassessable": len(unassessable),
    }

    # ---------------------------------------------------------- headline
    if not rows:
        return Rationale(
            headline=(
                f"{band} at {score_value:g} out of 100. No requirements could be "
                "read from this posting, so nothing was matched against your "
                "verified profile."
            ),
            caveats=[
                "The score reflects role, seniority and location alignment only."
            ],
            counts=counts,
        )

    headline = (
        f"{band} at {score_value:g} out of 100. "
        f"Your verified profile covers {len(met_required)} of "
        f"{len(required)} essential {_plural(len(required), 'requirement')}"
    )
    if partial_required:
        headline += f", with {len(partial_required)} partly covered"
    headline += "."

    # --------------------------------------------------------- strengths
    strengths: list[str] = []
    if met_required:
        texts = [str(r.get("requirement_text", "")) for r in met_required]
        strengths.append(f"Fully covered: {_name_list(texts)}.")
    if partial_required:
        texts = [str(r.get("requirement_text", "")) for r in partial_required]
        strengths.append(f"Partly covered: {_name_list(texts)}.")
    if met_preferred:
        strengths.append(
            f"You also cover {len(met_preferred)} preferred "
            f"{_plural(len(met_preferred), 'item')}."
        )

    # -------------------------------------------------------------- gaps
    gap_sentences: list[str] = []
    if unmet_assessable:
        texts = [str(r.get("requirement_text", "")) for r in unmet_assessable]
        gap_sentences.append(f"Not covered: {_name_list(texts)}.")
    elif not unmet_required and required:
        gap_sentences.append("Nothing essential is missing.")

    # ----------------------------------------------------------- caveats
    caveats: list[str] = []

    years_short = [
        r for r in partial_required
        if r.get("years_required") is not None
        and (r.get("years_evidenced") is None
             or float(r.get("years_evidenced") or 0) < float(r["years_required"]))
    ]
    if years_short:
        caveats.append(
            f"{len(years_short)} {_plural(len(years_short), 'requirement')} "
            f"{_plural(len(years_short), 'asks', 'ask')} for more years than your "
            "verified facts evidence."
        )

    if unassessable:
        caveats.append(
            f"{len(unassessable)} {_plural(len(unassessable), 'requirement')} "
            f"could not be checked automatically and "
            f"{_plural(len(unassessable), 'needs', 'need')} your judgement."
        )

    if not any(r.get("fact_ids") for r in rows):
        caveats.append(
            "No verified fact supported any requirement — confirming more "
            "facts on your profile would give this a fairer assessment."
        )

    # Passed in from A3 and named verbatim; not recomputed here.
    extra_gaps = [g for g in (gaps or []) if g and g not in {
        str(r.get("requirement_text")) for r in rows
    }]
    if extra_gaps:
        gap_sentences.append(f"Also flagged: {_name_list(extra_gaps)}.")

    return Rationale(
        headline=headline,
        strengths=strengths,
        gaps=gap_sentences,
        caveats=caveats,
        counts=counts,
    )


def rationale_from_breakdown(
    match_matrix: dict[str, Any] | None, breakdown: ScoreBreakdown
) -> Rationale:
    """Convenience for the live path, where A3's object is already in hand."""
    return build_rationale(
        match_matrix,
        score=breakdown.score,
        recommendation=str(breakdown.recommendation),
        gaps=list(breakdown.gaps),
    )


def rationale_text(
    match_matrix: dict[str, Any] | None, breakdown: ScoreBreakdown
) -> str:
    """The string to hand to A5's existing `rationale` parameter."""
    return rationale_from_breakdown(match_matrix, breakdown).summary
