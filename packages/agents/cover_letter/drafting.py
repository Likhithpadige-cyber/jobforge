"""Cover-letter body drafting. No cover letter is persisted; only audit rows.

Sibling of B3 (packages/agents/resume_optimizer/summary.py), not a fork of
resume tailoring: prose generation for a cover letter differs enough from
resume bullet/summary drafting (longer paragraphs, no section-of-origin
restriction, a different prompt and framing) to deserve its own module, per
the M10 design decision that cover-letter drafting must not be forced through
the resume-tailoring implementation. It still reuses the resume pipeline's
grounding primitives directly rather than forking them: the same
`TailoringContext`/`load_tailoring_context` (B0), the same numeral-provenance
guard, and - one layer up, in `packages.core.services.cover_letters` - the
same independent Claim Verifier (B5) that resumes use, via
`verify_claim_for_handoff`.

Unlike a B2 bullet, a cover-letter paragraph may combine facts from any
section, exactly as a B3 summary sentence may. Unlike B3 - which permits
"no untrusted data" framing because it only ever restates B1's own selection
- there is no B1 step here at all; every verified, citable fact for the
profile is offered, and the model chooses which to ground each paragraph in.

Every paragraph must cite at least one fact (`fact_ids` is non-empty, exactly
like `DraftedBullet`): there is no "connective" paragraph exempt from the
citation requirement, so nothing this module returns can be both unverified
and a claim about the candidate. This module drafts body paragraphs only - no
greeting, salutation or signature. Letterhead and formatting are a rendering
concern for a later phase; M10 Phase 1 persists and reviews grounded content,
not a formatted document.
"""

from __future__ import annotations

import hashlib
import html
import json
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as SchemaError
from sqlalchemy.ext.asyncio import AsyncSession

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
from packages.agents.resume_optimizer.context import TailoringContext
from packages.agents.resume_optimizer.drafting import (
    NumeralGuardError,
    UnapprovedFactError,
    _numeral_pool,
    _numeral_value,
    extract_numerals,
)
from packages.core.db.inference_audit import (
    DomainInferenceAudit,
    InferenceAuditUnavailableError,
    InferenceAuditWriter,
)
from packages.core.events import EventEnvelope
from packages.core.models import FactStatus
from packages.shared.logging import get_logger

log = get_logger(__name__)

PROMPT = Path(__file__).with_name("prompts") / "cover_letter.txt"

#: The section every drafted paragraph belongs to (mirrors B3's `summary`
#: constant - `cover_letter_claims` uses the same `section`/`path` shape as
#: `resume_claims`, one section here since a cover letter has no B1 sections).
BODY_SECTION = "body"


class CoverLetterParagraph(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    text: str = Field(min_length=1, max_length=800)
    fact_ids: list[str] = Field(min_length=1, max_length=8)
    numerals_used: list[str] = Field(default_factory=list, max_length=16)


class CoverLetterDraftOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    paragraphs: list[CoverLetterParagraph] = Field(min_length=3, max_length=6)


@dataclass(frozen=True)
class CoverLetterDraftResult:
    paragraphs: tuple[CoverLetterParagraph, ...]
    prompt_version: str


def prepare_request(context: TailoringContext, *, model: str, note: str | None = None) -> Request:
    """Build the drafting request from every verified, citable fact.

    `note` carries a reviewer's request-changes instructions on a
    regeneration. It rides inside the same trusted payload as the role
    targeting metadata - a reviewer's own words, not untrusted external
    content - and steers which facts are chosen and how the paragraphs are
    framed; it cannot widen the set of citable facts beyond what is listed.
    """
    verified = sorted(
        (f for f in context.facts if f.status == FactStatus.VERIFIED),
        key=lambda f: str(f.id),
    )
    facts_payload = [{"fact_id": str(f.id), "text": prompt_text(f.text)} for f in verified]
    normalized_title = prompt_text(str(context.job.get("normalized_title") or ""))
    seniority = str(context.job.get("seniority") or "")
    system = PROMPT.read_text()
    version = hashlib.sha256(system.encode()).hexdigest()
    payload: dict[str, Any] = {
        "target_role_family": normalized_title,
        "target_seniority": seniority,
        "verified_facts": facts_payload,
        "revision_instructions": prompt_text(note) if note else "",
        "context_digest": hashlib.sha256(
            canonical(
                {
                    "profile_id": str(context.profile_id),
                    "facts": [{"id": f["fact_id"], "text": f["text"]} for f in facts_payload],
                    "normalized_title": normalized_title,
                    "seniority": seniority,
                    "note": note or "",
                }
            ).encode()
        ).hexdigest(),
    }
    user = html.escape(canonical(payload), quote=False)
    assert_prompt_safe(system, user)
    return Request(
        model=model,
        system=system,
        user=user,
        schema=CoverLetterDraftOutput.model_json_schema(),
        prompt_version=version,
    )


def validate_draft(
    body: str,
    context: TailoringContext,
    *,
    prompt_version: str,
) -> CoverLetterDraftResult:
    def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        json.loads(body, object_pairs_hook=unique_keys)
        output = CoverLetterDraftOutput.model_validate_json(body)
    except (SchemaError, ValueError, TypeError):
        raise ModelOutputError(
            "cover-letter draft output must contain only 3-6 structured "
            "paragraphs, without extra prose"
        ) from None

    verified = {str(fact.id): fact for fact in context.facts if fact.status == FactStatus.VERIFIED}

    for paragraph in output.paragraphs:
        for fid in paragraph.fact_ids:
            if fid not in verified:
                raise UnapprovedFactError(
                    "Cited fact is not a currently verified fact for this profile"
                )

        # PII before the numeral guard, same ordering B2/B3 use: unredacted
        # private data must be rejected as PII, not misreported as a numeral.
        assert_prompt_safe(paragraph.text)

        pool = _numeral_pool(verified, paragraph.fact_ids)
        numerals = set(extract_numerals(paragraph.text)) | set(paragraph.numerals_used)
        for token in numerals:
            try:
                value = _numeral_value(token)
            except ValueError:
                raise NumeralGuardError("A reported numeral is not a valid number") from None
            if value not in pool:
                raise NumeralGuardError(
                    "A numeral in generated text is absent from every cited fact's "
                    "structured data"
                )

    return CoverLetterDraftResult(tuple(output.paragraphs), prompt_version)


async def draft_cover_letter(
    session: AsyncSession,
    *,
    context: TailoringContext,
    client: StructuredClient,
    note: str | None = None,
    writer: InferenceAuditWriter | None = None,
) -> CoverLetterDraftResult:
    """Draft the body paragraphs; persist audit metadata and validated output."""
    started = monotonic()
    if writer is None:
        writer = DomainInferenceAudit(session)
    stage = await writer.begin_stage(
        agent="job_application",
        # "packet" (agent_runs.pipeline CHECK, migration 0002) - the closest
        # existing value: AGENT_DESIGN.md places cover-letter generation
        # under Agent 2's packet-assembly responsibility, and inventing a new
        # pipeline value would need a migration this phase does not make.
        pipeline="packet",
        step_name="draft",
        aggregate_type="job",
        aggregate_id=context.job["id"],
        correlation_id=context.job["id"],
    )
    primary: BaseException | None = None
    evidence_failed = False
    succeeded = False
    try:
        request = prepare_request(context, model=client.model, note=note)
        await writer.record_request(
            stage,
            prompt_version=request.prompt_version,
            input_digest=request.digest,
            guardrail_results={},
        )
        for attempt in range(2):
            try:
                response = await client.complete(
                    request,
                    spent=writer.spent(stage),
                    session=session,
                )
            except ReplayMissError:
                if attempt:
                    raise ModelOutputError(
                        "cover-letter schema repair has no recorded response"
                    ) from None
                raise
            call = await writer.record_call(
                stage,
                model=request.model,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cost_usd=response.cost_usd,
                prompt_hash=request.digest,
                stop_reason=response.stop_reason,
            )
            if writer.spent(stage) > client.max_cost:
                raise CostLimitError("cover-letter drafting exceeded its run budget")
            try:
                if response.stop_reason != "tool_use":
                    raise ModelOutputError(
                        "cover-letter draft response was truncated or did not use "
                        "the drafting tool"
                    )
                result = validate_draft(
                    response.body, context, prompt_version=request.prompt_version
                )
            except ModelOutputError:
                client.record(request, response.model_copy(update={"body": "null"}))
                if attempt:
                    raise
                request = Request(
                    **{
                        **request.__dict__,
                        "user": request.user
                        + "\nReturn only the required schema; the previous output was invalid.",
                    }
                )
                continue
            safe_output = {"paragraphs": [item.model_dump() for item in result.paragraphs]}
            await writer.record_call_output(stage, call, response_redacted=canonical(safe_output))
            await writer.record_step_output(stage, output=safe_output)
            client.record(request, response.model_copy(update={"body": canonical(safe_output)}))
            succeeded = True
            return result
        raise ModelOutputError("cover-letter draft response validation failed")
    except BaseException as exc:
        primary = exc
        evidence_failed = isinstance(exc, InferenceAuditUnavailableError)
        if isinstance(exc, UnapprovedFactError | NumeralGuardError | PiiInPromptError):
            pii = isinstance(exc, PiiInPromptError)
            violation = {
                "kind": "pii_in_prompt" if pii else "fabrication",
                "agent": "job_application",
                "severity": "high" if pii else "critical",
                "aggregate_type": "job",
                "aggregate_id": str(context.job["id"]),
                "detail": (
                    "cover-letter draft contains unredacted private data"
                    if pii
                    else "cover-letter paragraph is not grounded in verified facts"
                ),
                "action_taken": "run_failed",
                "step": "cover_letter_draft",
                "run_id": str(stage.run_id),
            }
            try:
                await writer.record_guardrail(
                    stage,
                    guardrail_results={"failure": violation},
                    event=EventEnvelope(
                        type="guardrail.violation.detected",
                        aggregate_type="job",
                        aggregate_id=context.job["id"],
                        correlation_id=context.job["id"],
                        actor_type="agent",
                        actor_id="job_application",
                        payload=violation,
                    ),
                )
            except Exception:
                evidence_failed = True
                log.warning(
                    "cover_letter_draft.guardrail_evidence_failed", run_id=str(stage.run_id)
                )
        raise
    finally:
        if not evidence_failed:
            try:
                await writer.finish_stage(
                    stage,
                    status="succeeded" if succeeded else "failed",
                    steps_completed=1 if succeeded else 0,
                    duration_ms=int((monotonic() - started) * 1000),
                    error_type=type(primary).__name__ if primary is not None else None,
                    error_message=("cover-letter drafting failed" if primary is not None else None),
                )
            except Exception:
                if primary is None:
                    raise
                log.warning("cover_letter_draft.outcome_evidence_failed", run_id=str(stage.run_id))
