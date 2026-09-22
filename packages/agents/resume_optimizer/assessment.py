"""M5 evidence sufficiency gate. No inference, provider calls or default credit."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from packages.agents.resume_optimizer.evidence import FactView, MatchMatrix, build_match_matrix
from packages.agents.resume_optimizer.prefilter import (
    SENIORITY_ORDER,
    ScoreBreakdown,
    compute_score,
)
from packages.agents.resume_optimizer.requirements import Requirement


@dataclass(frozen=True)
class Alignment:
    """An explicit human preference compared with a stated job attribute."""

    value: bool | None
    reason: str
    fact_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {"status": "unknown" if self.value is None else (
            "match" if self.value else "mismatch"
        ), "reason": self.reason, "fact_ids": list(self.fact_ids)}


@dataclass(frozen=True)
class Assessment:
    matrix: MatchMatrix
    alignments: dict[str, Alignment]
    reasons: list[str]
    breakdown: ScoreBreakdown | None

    @property
    def status(self) -> Literal["assessed", "insufficient_evidence"]:
        return "insufficient_evidence" if self.reasons else "assessed"

    def evidence(self) -> dict[str, object]:
        result = self.matrix.as_dict()
        # Preserve the legacy lexical matrix verbatim. This separate decision
        # layer explicitly distinguishes missing support from actual mismatch.
        result["assessment"] = {
            "status": self.status,
            "reasons": self.reasons,
            "alignments": {key: item.as_dict() for key, item in self.alignments.items()},
            "requirements": [
                {"text": row.requirement_text, "status": (
                    "unknown" if requirement_unknown(row.assessable, row.unmatched_terms,
                                                     row.fact_ids, row.years_required,
                                                     row.years_evidenced)
                    else "match" if str(row.status) == "met" else "partial_match"
                )}
                for row in self.matrix.rows
            ],
        }
        return result


def requirement_unknown(assessable: bool, unmatched: list[str], fact_ids: list[str],
                        years_required: int | None, years_evidenced: float | None) -> bool:
    return (not assessable or bool(unmatched) or not fact_ids
            or (years_required is not None and years_evidenced is None))


def assess(requirements: list[Requirement], facts: list[FactView], *,
           alignments: dict[str, Alignment], today: date,
           extraction_confident: bool) -> Assessment:
    matrix = build_match_matrix(requirements, facts, today)
    reasons: list[str] = []
    if not requirements:
        reasons.append("no requirements extracted from the job description")
    elif not any(row.required for row in matrix.rows):
        reasons.append("no required requirements available for assessment")
    if not extraction_confident:
        reasons.append("deterministic extraction is incomplete or low confidence")
    for index, row in enumerate(matrix.rows):
        if requirement_unknown(row.assessable, row.unmatched_terms, row.fact_ids,
                               row.years_required, row.years_evidenced):
            reasons.append(f"requirement {index + 1}: evidence is incomplete")
    for key in ("seniority", "domain", "location"):
        item = alignments.get(key)
        if item is None or item.value is None:
            reasons.append(f"{key} alignment is unknown")
    breakdown = None
    if not reasons:
        breakdown = compute_score(
            matrix.to_requirement_matches(),
            seniority_alignment=int(alignments["seniority"].value is True),
            domain_alignment=int(alignments["domain"].value is True),
            location_fit=int(alignments["location"].value is True),
        )
    return Assessment(matrix, alignments, reasons, breakdown)


def preference(facts: list[FactView], key: str) -> tuple[object, tuple[str, ...]]:
    """Only explicit structured, verified statements; conflicting values are unknown."""
    supplied = [fact for fact in facts if key in fact.structured]
    if not supplied or any(type(f.structured[key]) is not type(supplied[0].structured[key])
                            or f.structured[key] != supplied[0].structured[key] for f in supplied):
        return None, ()
    return supplied[0].structured[key], tuple(str(f.id) for f in supplied)


def align(facts: list[FactView], *, seniority: str, title: str,
          target_roles: list[str], is_remote: bool, is_hybrid: bool,
          location_state: str | None, location_country: str | None = None,
          profile_country: str | None = None) -> dict[str, Alignment]:
    """Compare stated preferences, never infer work authorization or willingness.

    Structured preference keys use the existing CandidateConstraints vocabulary.
    Missing/conflicting values deliberately do not inherit that class's defaults.
    Exact role equality is supported; lexical non-equality is not a proven mismatch.
    """
    result = {name: Alignment(None, "no unambiguous stated comparison available")
              for name in ("seniority", "domain", "location")}
    ceiling, ids = preference(facts, "max_seniority")
    if (isinstance(ceiling, str) and ceiling in SENIORITY_ORDER and ceiling != "unknown"
            and seniority in SENIORITY_ORDER and seniority != "unknown"):
        result["seniority"] = Alignment(
            SENIORITY_ORDER[seniority] <= SENIORITY_ORDER[ceiling],
            "job seniority compared with verified stated ceiling", ids,
        )
    normalized = " ".join(title.casefold().split())
    if normalized and normalized in {" ".join(role.casefold().split()) for role in target_roles}:
        result["domain"] = Alignment(True, "exact match to human-authored profile target role")
    if (not location_country or not profile_country
            or location_country.casefold() != profile_country.casefold()):
        result["location"] = Alignment(None, "geographic fit is not established")
        return result
    # Remote/hybrid flags do not justify guessing geographic eligibility or visa
    # answers. Only the stated working-arrangement preference is compared here.
    if is_hybrid or is_remote:
        key = "accepts_hybrid" if is_hybrid else "accepts_remote"
        accepted, ids = preference(facts, key)
        if isinstance(accepted, bool):
            result["location"] = Alignment(accepted, f"verified {key} preference", ids)
    elif location_state:
        states, ids = preference(facts, "allowed_states")
        if isinstance(states, list) and states and all(isinstance(s, str) for s in states):
            result["location"] = Alignment(
                location_state.casefold() in {s.casefold() for s in states},
                "job state compared with verified allowed states", ids,
            )
    return result
