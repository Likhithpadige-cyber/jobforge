"""B1 content selection only. No resume is written; only runtime audit rows.

B0 is the sole fact source. The caller must pass its authorized, freshly loaded
context. Audit rows and guardrail events use the caller's transaction, like A5;
callers must commit failed-run audit records if retaining a failed attempt.
"""

from __future__ import annotations

import hashlib
import html
import json
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError as SchemaError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.agents.base.injection import ScanResult, scan
from packages.agents.base.pii import PiiInPromptError, assert_prompt_safe
from packages.agents.base.pii import redact_prompt_text as prompt_text
from packages.agents.llm.runtime import (
    CostLimitError,
    ModelOutputError,
    ReplayMissError,
    Request,
    StructuredClient,
    canonical,
)
from packages.agents.resume_optimizer.context import TailoringContext, validate_match_matrix
from packages.core.events import emit
from packages.core.models import AgentRun, AgentRunStep, FactStatus, FactType, LlmCall
from packages.shared.clock import utc_now
from packages.shared.errors import ValidationError
from packages.shared.ids import uuid7

Section = Literal["experience", "projects", "skills", "education", "certifications"]
SECTIONS = {
    FactType.EXPERIENCE: "experience",
    FactType.PROJECT: "projects",
    FactType.SKILL: "skills",
    FactType.EDUCATION: "education",
    FactType.CERTIFICATION: "certifications",
}
PROMPT = Path(__file__).with_name("prompts") / "b1_selection.txt"


class UngroundedSelectionError(ValidationError):
    """A selected ID is absent from B0's verified facts."""


class DuplicateSelectionError(ValidationError):
    """A fact was selected more than once."""


class InvalidSectionError(ValidationError):
    """A selection's section conflicts with its persisted fact category."""


class InvalidOrderingError(ValidationError):
    """Orders must be unique and contiguous, with contiguous sections."""


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    fact_id: str
    section: Section
    order: int
    emphasis: Literal["normal", "high"]


class SelectionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    selections: list[Selection]


@dataclass(frozen=True)
class MaterialGap:
    requirement_index: int
    requirement_text: str
    necessity: str
    status: str


@dataclass(frozen=True)
class SelectionResult:
    selections: tuple[Selection, ...]
    evaluation_gaps: tuple[str, ...]
    material_gaps: tuple[MaterialGap, ...]
    requires_human_review: bool
    prompt_version: str


def prepare_request(
    context: TailoringContext,
    model: str,
    *,
    revision: dict[str, Any] | None = None,
) -> tuple[Request, ScanResult]:
    """Build the B1 request, optionally carrying a C3 revision block.

    `revision` defaults to None and every pre-C3 caller leaves it that way, so
    the payload, the digest and the prompt version are byte-identical to what
    they were - a first generation is not affected by C3 existing.

    When present, the block is placed inside the same `<untrusted>` envelope as
    everything else the caller did not write. That is the whole safety story
    for the reviewer note: this prompt already states that untrusted content is
    data and never instructions, and that only the listed verified fact IDs are
    selectable. A note therefore steers *which verified facts* are chosen, in
    what order and with what emphasis, and cannot widen the set they are chosen
    from. See `packages.agents.resume_optimizer.revision`.
    """
    ids = {fact.id for fact in context.facts}
    if len(ids) != len(context.facts) or any(
        fact.status != FactStatus.VERIFIED for fact in context.facts
    ):
        raise UngroundedSelectionError("B1 requires unique verified B0 facts")
    validate_match_matrix(context.match_matrix, ids)
    system = PROMPT.read_text()
    version = hashlib.sha256(system.encode()).hexdigest()
    # Scan raw job text before escaping. Findings survive in the step audit and
    # outbox; suspicious postings are not rejected by the scanner's contract.
    job_text = "\n".join(str(context.job.get(key) or "") for key in ("title", "description_raw"))
    findings = scan(job_text)
    payload = {
        "verified_facts": [
            {"fact_id": str(f.id), "section": SECTIONS[f.fact_type], "text": prompt_text(f.text)}
            for f in sorted(context.facts, key=lambda f: str(f.id))
            if f.fact_type in SECTIONS
        ],
        "requirements": [
            {
                "requirement_text": prompt_text(row["requirement_text"]),
                "necessity": row["necessity"],
                "status": row["status"],
                "fact_ids": row["fact_ids"],
            }
            for row in context.match_matrix["requirements"]
        ],
        "score": str(context.score),
        "rationale": prompt_text(context.rationale),
        "gaps": [prompt_text(gap) for gap in context.gaps],
        "job_description": prompt_text(findings.text),
        # Bind replay to the actual B0 input without sending unused private data.
        "context_digest": hashlib.sha256(
            canonical(
                {
                    "profile_id": str(context.profile_id),
                    "base_resume_id": str(context.base_resume_id),
                    "base_resume_version": context.base_resume_version,
                    "base_structure": context.base_resume_structure,
                    "evaluation_id": str(context.evaluation_id),
                    "profile_version": context.profile_version,
                    "facts": [
                        {
                            "id": str(f.id),
                            "text": f.text,
                            "structured": f.structured,
                            "type": str(f.fact_type),
                            "status": str(f.status),
                        }
                        for f in context.facts
                    ],
                    "matrix": context.match_matrix,
                }
            ).encode()
        ).hexdigest(),
    }
    if revision is not None:
        # Added to the payload rather than merged into any existing key, so a
        # non-revision request serialises exactly as before and keeps its
        # digest. The reviewer's prose is redacted like every other free-text
        # field that reaches a prompt.
        payload["revision"] = {
            "prior_resume_version": revision["prior_resume_version"],
            "reviewer_instructions": prompt_text(
                str(revision["reviewer_instructions"])
            ),
            "prior_claims": [
                {
                    "section": str(claim["section"]),
                    "path": str(claim["path"]),
                    "claim_text": prompt_text(str(claim["claim_text"])),
                    "fact_ids": list(claim["fact_ids"]),
                }
                for claim in revision["prior_claims"]
            ],
        }
    user = "<untrusted>" + html.escape(canonical(payload), quote=False) + "</untrusted>"
    assert_prompt_safe(system, user)
    return Request(
        model=model,
        system=system,
        user=user,
        schema=SelectionOutput.model_json_schema(),
        prompt_version=version,
    ), findings


def validate_selection(
    body: str,
    context: TailoringContext,
    *,
    prompt_version: str,
    requires_human_review: bool = False,
) -> SelectionResult:
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        json.loads(body, object_pairs_hook=unique_keys)
        output = SelectionOutput.model_validate_json(body)
    except (SchemaError, ValueError, TypeError):
        raise ModelOutputError(
            "B1 output must contain only structured selections, without prose"
        ) from None
    facts = {str(fact.id): fact for fact in context.facts if fact.status == FactStatus.VERIFIED}
    seen: set[str] = set()
    for item in output.selections:
        if item.fact_id not in facts:
            raise UngroundedSelectionError("Selected fact is not a verified B0 fact")
        if item.fact_id in seen:
            raise DuplicateSelectionError("Fact IDs must be selected at most once")
        seen.add(item.fact_id)
        if SECTIONS.get(facts[item.fact_id].fact_type) != item.section:
            raise InvalidSectionError("Section does not match the fact's persisted category")
    ordered = tuple(sorted(output.selections, key=lambda item: item.order))
    if [item.order for item in ordered] != list(range(1, len(ordered) + 1)):
        raise InvalidOrderingError("Selection orders must be unique and contiguous starting at 1")
    sections = [
        item.section
        for i, item in enumerate(ordered)
        if i == 0 or ordered[i - 1].section != item.section
    ]
    if len(sections) != len(set(sections)):
        raise InvalidOrderingError("Selections for each section must be contiguous")
    # These labels are copied by code, never generated or upgraded by the model.
    gaps = tuple(
        MaterialGap(i, row["requirement_text"], row["necessity"], row["status"])
        for i, row in enumerate(context.match_matrix["requirements"])
        if row["status"] != "met"
    )
    return SelectionResult(
        ordered, tuple(context.gaps), gaps, requires_human_review, prompt_version
    )


async def select_content(
    session: AsyncSession,
    *,
    context: TailoringContext,
    client: StructuredClient,
    revision: dict[str, Any] | None = None,
) -> SelectionResult:
    """Execute only B1; persist audit metadata and validated IDs, never a resume.

    `revision` is C3's editorial block and defaults to None. It changes what
    B1 is shown, never what B1 may return: every validation below - the
    verified-fact membership check, the section check, the ordering check -
    runs identically, so a revision cannot produce a selection a first
    generation could not have produced.
    """
    started = monotonic()
    run = AgentRun(
        id=uuid7(),
        agent="resume_optimizer",
        pipeline="tailoring",
        status="running",
        aggregate_type="job",
        aggregate_id=context.job["id"],
        correlation_id=context.job["id"],
        steps_total=1,
        steps_completed=0,
        input_tokens=0,
        output_tokens=0,
        cost_usd=0,
    )
    step = AgentRunStep(id=uuid7(), run_id=run.id, step_index=1, step_name="B1", status="running")
    session.add(run)
    await session.flush()
    session.add(step)
    await session.flush()
    try:
        request, findings = prepare_request(context, client.model, revision=revision)
        run.prompt_version = request.prompt_version
        step.input_digest = request.digest
        # Do not store excerpts, which may contain PII from attacker input.
        guardrail = {k: v for k, v in findings.as_payload().items() if k != "excerpts"}
        step.guardrail_results = guardrail
        if findings.findings:
            await emit(
                session,
                type="guardrail.violation.detected",
                aggregate_type="job",
                aggregate_id=context.job["id"],
                correlation_id=context.job["id"],
                actor_id="resume_optimizer",
                payload=guardrail,
            )
        for attempt in range(2):
            try:
                response = await client.complete(request, spent=run.cost_usd)
            except ReplayMissError:
                if attempt:
                    raise ModelOutputError("B1 schema repair has no recorded response") from None
                raise
            run.input_tokens += response.input_tokens
            run.output_tokens += response.output_tokens
            run.cost_usd += response.cost_usd
            call = LlmCall(
                id=uuid7(),
                run_id=run.id,
                step_id=step.id,
                model=request.model,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cost_usd=response.cost_usd,
                prompt_hash=request.digest,
                stop_reason=response.stop_reason[:32],
                prompt_redacted=None,
                response_redacted=None,
            )
            session.add(call)
            await session.flush()
            if run.cost_usd > client.max_cost:
                raise CostLimitError("B1 exceeded its run budget")
            try:
                if response.stop_reason != "tool_use":
                    raise ModelOutputError(
                        "B1 response was truncated or did not use the selection tool"
                    )
                result = validate_selection(
                    response.body,
                    context,
                    prompt_version=request.prompt_version,
                    requires_human_review=findings.requires_human_review,
                )
            except ModelOutputError:
                # Preserve repair replay without storing rejected model prose.
                client.record(request, response.model_copy(update={"body": "null"}))
                if attempt:
                    raise
                # At most one G3 repair; never echo malformed prose back to the model.
                request = Request(
                    **{
                        **request.__dict__,
                        "user": request.user
                        + "\nReturn only the required schema; the previous output was invalid.",
                    }
                )
                continue
            safe_output = {"selections": [item.model_dump() for item in result.selections]}
            call.response_redacted = canonical(safe_output)
            step.output = {
                **safe_output,
                "evaluation_id": str(context.evaluation_id),
                "gap_requirement_indices": [gap.requirement_index for gap in result.material_gaps],
            }
            client.record(request, response.model_copy(update={"body": canonical(safe_output)}))
            run.status = step.status = "succeeded"
            run.steps_completed = 1
            return result
        raise ModelOutputError("B1 response validation failed")
    except Exception as exc:
        run.status = step.status = "failed"
        run.error_type = type(exc).__name__
        run.error_message = "B1 content selection failed"  # never store raw model text
        if isinstance(exc, UngroundedSelectionError | PiiInPromptError):
            grounding = isinstance(exc, UngroundedSelectionError)
            violation = {
                "kind": "fabrication" if grounding else "pii_in_prompt",
                "agent": "resume_optimizer",
                "severity": "critical" if grounding else "high",
                "aggregate_type": "job",
                "aggregate_id": str(context.job["id"]),
                "detail": "B1 selection is not grounded in verified facts"
                if grounding
                else "B1 prompt contains unredacted private data",
                "action_taken": "run_failed",
                "step": "B1",
                "run_id": str(run.id),
            }
            step.guardrail_results = {**(step.guardrail_results or {}), "failure": violation}
            await emit(
                session,
                type="guardrail.violation.detected",
                aggregate_type="job",
                aggregate_id=context.job["id"],
                correlation_id=context.job["id"],
                actor_id="resume_optimizer",
                payload=violation,
            )
        raise
    finally:
        run.finished_at = step.finished_at = utc_now()
        run.duration_ms = step.duration_ms = int((monotonic() - started) * 1000)
        await session.flush()
