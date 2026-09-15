"""B0: load persisted tailoring inputs without inference or state changes.

The caller supplies the authorized profile and job IDs. Evaluations currently
have no profile_id; their persisted profile_version is preserved as context,
not interpreted as proof of ownership. Every citation must belong to the
selected profile's currently verified, undeleted facts. This is not a prompt:
job text and base content remain data and still require prompt guardrails later.
"""
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from math import isfinite
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from packages.agents.resume_optimizer.evidence import FactView
from packages.agents.resume_optimizer.prefilter import MatchStatus
from packages.agents.resume_optimizer.requirements import Necessity, RequirementKind
from packages.core.models import FactStatus, FactType, Recommendation
from packages.core.repositories import FactRepository, JobEvaluationRepository, JobRepository
from packages.core.repositories.resumes import ResumeRepository
from packages.shared.errors import NotFoundError, ValidationError


class JobNotFoundError(NotFoundError):
    """The selected job does not exist."""


class EvaluationNotFoundError(NotFoundError):
    """The selected job has no persisted evaluation."""


class BaseResumeNotFoundError(NotFoundError):
    """The selected profile has no base resume."""


class MalformedMatchMatrixError(ValidationError):
    """Persisted A2 data is malformed or cites unavailable facts."""


class IncompleteEvaluationError(ValidationError):
    """Required persisted A3/A4 context is unavailable."""


@dataclass(frozen=True, kw_only=True)
class TailoringFact(FactView):
    """Verified B0 fact with its persisted category, never inferred from prose."""

    fact_type: FactType
    status: FactStatus


@dataclass(frozen=True)
class TailoringContext:
    profile_id: UUID
    base_resume_id: UUID
    base_resume_version: int
    base_resume_structure: dict[str, Any]
    facts: tuple[TailoringFact, ...]
    job: dict[str, Any]
    evaluation_id: UUID
    profile_version: int
    match_matrix: dict[str, Any]
    score: Decimal
    recommendation: Recommendation
    score_breakdown: dict[str, Any]
    rationale: str
    gaps: tuple[str, ...]


def validate_match_matrix(matrix: Any, citable_ids: set[UUID]) -> None:
    """Validate without repairing, rescoring, or discarding persisted rows."""
    def invalid() -> None:
        # Never echo potentially private persisted text into errors.
        raise MalformedMatchMatrixError("A2 match matrix is malformed or cites unavailable facts")

    if not isinstance(matrix, dict) or not isinstance(matrix.get("requirements"), list):
        invalid()
    if "summary" in matrix and (
        not isinstance(matrix["summary"], dict)
        or any(type(value) is not int or value < 0 for value in matrix["summary"].values())
    ):
        invalid()
    for row in matrix["requirements"]:
        if not isinstance(row, dict):
            invalid()
        if not isinstance(row.get("requirement_text"), str) or not row["requirement_text"].strip():
            invalid()
        if row.get("status") not in list(MatchStatus):
            invalid()
        if row.get("necessity") not in list(Necessity) or row.get("kind") not in list(RequirementKind):
            invalid()
        ids = row.get("fact_ids")
        if not isinstance(ids, list) or any(not isinstance(value, str) for value in ids):
            invalid()
        try:
            parsed = {UUID(value) for value in ids}
        except ValueError:
            invalid()
        if len(parsed) != len(ids) or not parsed <= citable_ids:
            invalid()
        if row["status"] in (MatchStatus.MET, MatchStatus.PARTIALLY_MET) and not ids:
            invalid()
        for key in ("matched_terms", "unmatched_terms"):
            if key in row and (not isinstance(row[key], list)
                               or any(not isinstance(term, str) for term in row[key])):
                invalid()
        for key in ("years_required", "years_evidenced"):
            value = row.get(key)
            if value is not None and (
                type(value) not in (int, float) or not isfinite(value) or value < 0
                or (key == "years_required" and type(value) is not int)
            ):
                invalid()
        if "assessable" in row and type(row["assessable"]) is not bool:
            invalid()
        if row.get("no_evidence_reason") is not None and not isinstance(row["no_evidence_reason"], str):
            invalid()
        if "evidence" in row:
            if not isinstance(row["evidence"], list):
                invalid()
            for item in row["evidence"]:
                if not isinstance(item, dict) or item.get("fact_id") not in ids or not isinstance(item.get("text"), str):
                    invalid()


async def load_tailoring_context(
    session: AsyncSession, *, profile_id: UUID, job_id: UUID,
) -> TailoringContext:
    """Load B0 using latest evaluation and highest-version base resume.

    Ties are resolved by persisted timestamps and IDs. No autoflush, writes,
    event emission, LLM calls, or external requests occur. Nested values are
    copied so edits to returned context cannot mutate ORM-backed JSON.
    """
    with session.no_autoflush:
        job = await JobRepository(session).get(job_id)
        if job is None:
            raise JobNotFoundError(f"job {job_id} not found")
        evaluation = await JobEvaluationRepository(session).latest_for_job(job_id)
        if evaluation is None:
            raise EvaluationNotFoundError(f"evaluation for job {job_id} not found")
        base = await ResumeRepository(session).latest_base_for_profile(profile_id)
        if base is None:
            raise BaseResumeNotFoundError(f"base resume for profile {profile_id} not found")
        facts = await FactRepository(session).list_citable(profile_id)
        facts.sort(key=lambda fact: str(fact.id))
        validate_match_matrix(evaluation.match_matrix, {fact.id for fact in facts})
        if (evaluation.score is None or not evaluation.score.is_finite()
                or not 0 <= evaluation.score <= 100
                or not isinstance(evaluation.rationale, str) or not evaluation.rationale.strip()):
            raise IncompleteEvaluationError("Persisted A3 score and A4 rationale are required")
        if not isinstance(base.content, dict):
            raise ValidationError("Base resume structure must be an object")
        return TailoringContext(
            profile_id=profile_id, base_resume_id=base.id, base_resume_version=base.version,
            base_resume_structure=deepcopy(base.content),
            facts=tuple(
                TailoringFact(id=fact.id, text=fact.text, structured=deepcopy(fact.structured),
                              fact_type=FactType(fact.fact_type), status=FactStatus(fact.status))
                for fact in facts
            ),
            job={column.key: deepcopy(getattr(job, column.key)) for column in job.__table__.columns},
            evaluation_id=evaluation.id, profile_version=evaluation.profile_version,
            match_matrix=deepcopy(evaluation.match_matrix), score=evaluation.score,
            recommendation=evaluation.recommendation,
            score_breakdown=deepcopy(evaluation.score_breakdown),
            rationale=evaluation.rationale, gaps=tuple(evaluation.gaps),
        )
