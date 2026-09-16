"""B3 summary/headline drafting only. No resume is persisted; only audit rows.

Sibling of B2 (packages/agents/resume_optimizer/drafting.py), not a variant of
it: B1 is the sole input, only B1-approved verified facts and minimal, already
normalized targeting metadata (role family / seniority) reach the prompt - no
full profile, no unselected facts, no raw job description.

Unlike a B2 bullet, a summary sentence may legitimately combine facts from
different sections (a skill and an experience fact in one sentence), so B3
has no section-mixing restriction. Numeral provenance and fact-approval
checks reuse the exact helpers B2 already implements (including the
signed-numeral fix) rather than forking a second copy.
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
from packages.agents.resume_optimizer.selection import SelectionResult
from packages.core.db.inference_audit import (
    DomainInferenceAudit,
    InferenceAuditUnavailableError,
    InferenceAuditWriter,
)
from packages.core.events import EventEnvelope
from packages.core.models import FactStatus
from packages.shared.logging import get_logger

log = get_logger(__name__)

PROMPT = Path(__file__).with_name("prompts") / "b3_summary.txt"


class SummarySentence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    text: str = Field(min_length=1, max_length=400)
    fact_ids: list[str] = Field(min_length=1, max_length=8)
    numerals_used: list[str] = Field(default_factory=list, max_length=16)


class SummaryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    sentences: list[SummarySentence] = Field(min_length=2, max_length=3)


@dataclass(frozen=True)
class SummaryResult:
    sentences: tuple[SummarySentence, ...]
    prompt_version: str


def prepare_request(
    context: TailoringContext, selection: SelectionResult, model: str
) -> Request:
    """Build the B3 request from only the facts B1 already approved, plus
    the job's already-normalized title/seniority - never raw JD text, and
    never any fact outside B1's selection.
    """
    facts_by_id = {
        str(f.id): f for f in context.facts if f.status == FactStatus.VERIFIED
    }
    ordered = sorted(selection.selections, key=lambda item: item.order)
    approved = [
        {
            "fact_id": item.fact_id,
            "section": item.section,
            "emphasis": item.emphasis,
            "text": prompt_text(facts_by_id[item.fact_id].text),
        }
        for item in ordered
        if item.fact_id in facts_by_id
    ]
    normalized_title = prompt_text(str(context.job.get("normalized_title") or ""))
    seniority = str(context.job.get("seniority") or "")
    system = PROMPT.read_text()
    version = hashlib.sha256(system.encode()).hexdigest()
    payload = {
        "target_role_family": normalized_title,
        "target_seniority": seniority,
        "selected_facts": approved,
        # Bind replay to the exact B1 output and targeting metadata actually
        # used, without sending any fact B1 did not select.
        "context_digest": hashlib.sha256(
            canonical(
                {
                    "profile_id": str(context.profile_id),
                    "base_resume_id": str(context.base_resume_id),
                    "base_resume_version": context.base_resume_version,
                    "selections": [item.model_dump() for item in ordered],
                    "facts": [
                        {
                            "id": item["fact_id"],
                            "text": facts_by_id[item["fact_id"]].text,
                            "structured": facts_by_id[item["fact_id"]].structured,
                        }
                        for item in approved
                    ],
                    "normalized_title": normalized_title,
                    "seniority": seniority,
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
        schema=SummaryOutput.model_json_schema(),
        prompt_version=version,
    )


def validate_summary(
    body: str,
    context: TailoringContext,
    selection: SelectionResult,
    *,
    prompt_version: str,
) -> SummaryResult:
    def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        json.loads(body, object_pairs_hook=unique_keys)
        output = SummaryOutput.model_validate_json(body)
    except (SchemaError, ValueError, TypeError):
        raise ModelOutputError(
            "B3 output must contain only 2-3 structured sentences, without extra prose"
        ) from None

    approved = {item.fact_id: item for item in selection.selections}
    verified = {
        str(fact.id): fact
        for fact in context.facts
        if fact.status == FactStatus.VERIFIED
    }

    for sentence in output.sentences:
        for fid in sentence.fact_ids:
            if fid not in approved or fid not in verified:
                raise UnapprovedFactError(
                    "Cited fact was not approved by B1 selection, or is no longer verified"
                )

        # PII before the numeral guard: unredacted private data (e.g. a
        # street number) must be rejected as PII, not misreported as an
        # unsupported numeral. Reuses the same detector as B1/B2.
        assert_prompt_safe(sentence.text)

        pool = _numeral_pool(verified, sentence.fact_ids)
        numerals = set(extract_numerals(sentence.text)) | set(sentence.numerals_used)
        for token in numerals:
            try:
                value = _numeral_value(token)
            except ValueError:
                raise NumeralGuardError(
                    "A reported numeral is not a valid number"
                ) from None
            if value not in pool:
                raise NumeralGuardError(
                    "A numeral in generated text is absent from every cited fact's structured data"
                )

    return SummaryResult(tuple(output.sentences), prompt_version)


async def draft_summary(
    session: AsyncSession,
    *,
    context: TailoringContext,
    selection: SelectionResult,
    client: StructuredClient,
    writer: InferenceAuditWriter | None = None,
) -> SummaryResult:
    """Execute only B3; persist audit metadata and validated output."""
    started = monotonic()
    if writer is None:
        writer = DomainInferenceAudit(session)
    stage = await writer.begin_stage(
        agent="resume_optimizer",
        pipeline="tailoring",
        step_name="B3",
        aggregate_type="job",
        aggregate_id=context.job["id"],
        correlation_id=context.job["id"],
    )
    primary: BaseException | None = None
    evidence_failed = False
    succeeded = False
    try:
        request = prepare_request(context, selection, client.model)
        await writer.record_request(
            stage,
            prompt_version=request.prompt_version,
            input_digest=request.digest,
            guardrail_results={},
        )
        for attempt in range(2):
            try:
                response = await client.complete(request, spent=writer.spent(stage))
            except ReplayMissError:
                if attempt:
                    raise ModelOutputError("B3 schema repair has no recorded response") from None
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
                raise CostLimitError("B3 exceeded its run budget")
            try:
                if response.stop_reason != "tool_use":
                    raise ModelOutputError(
                        "B3 response was truncated or did not use the summary tool"
                    )
                result = validate_summary(
                    response.body,
                    context,
                    selection,
                    prompt_version=request.prompt_version,
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
            safe_output = {"sentences": [item.model_dump() for item in result.sentences]}
            await writer.record_call_output(stage, call, response_redacted=canonical(safe_output))
            await writer.record_step_output(stage, output=safe_output)
            client.record(request, response.model_copy(update={"body": canonical(safe_output)}))
            succeeded = True
            return result
        raise ModelOutputError("B3 response validation failed")
    except BaseException as exc:
        primary = exc
        evidence_failed = isinstance(exc, InferenceAuditUnavailableError)
        if isinstance(exc, UnapprovedFactError | NumeralGuardError | PiiInPromptError):
            pii = isinstance(exc, PiiInPromptError)
            violation = {
                "kind": "pii_in_prompt" if pii else "fabrication",
                "agent": "resume_optimizer",
                "severity": "high" if pii else "critical",
                "aggregate_type": "job",
                "aggregate_id": str(context.job["id"]),
                "detail": (
                    "B3 output contains unredacted private data"
                    if pii
                    else "B3 sentence is not grounded in B1-approved verified facts"
                ),
                "action_taken": "run_failed",
                "step": "B3",
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
                        actor_id="resume_optimizer",
                        payload=violation,
                    ),
                )
            except Exception:
                evidence_failed = True
                log.warning("b3.guardrail_evidence_failed", run_id=str(stage.run_id))
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
                    error_message="B3 summary drafting failed" if primary is not None else None,
                )
            except Exception:
                if primary is None:
                    raise
                log.warning("b3.outcome_evidence_failed", run_id=str(stage.run_id))
