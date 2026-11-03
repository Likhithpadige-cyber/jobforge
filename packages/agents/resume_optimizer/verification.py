"""B5 Claim Verifier. Independent per-claim check; persists audit evidence only.

Fresh, isolated call: this module takes only a single generated claim's text
and the `TailoringFact` rows it cites - never the job description, the
tailoring/selection prompts, B1's rationale or scoring, or any other
candidate fact. It has no dependency on `TailoringContext`, `SelectionResult`,
or the B2/B3 modules at all, so it structurally cannot see anything beyond
what its caller explicitly hands it for one claim.

A `supported` verdict is a normal, recorded result. `overreach` and
`unsupported` are also legitimate, recorded verdicts (the verifier did its
job correctly) but they fail the claim - `ClaimNotSupportedError` - with no
repair attempt, matching G4/G5's fail-closed rule. Only a malformed/
unparseable response goes through the existing schema-repair-once path.
"""

from __future__ import annotations

import hashlib
import html
import json
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Literal
from uuid import UUID

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
from packages.agents.resume_optimizer.context import TailoringFact
from packages.core.db.inference_audit import (
    DomainInferenceAudit,
    InferenceAuditUnavailableError,
    InferenceAuditWriter,
)
from packages.core.events import EventEnvelope
from packages.core.models import ClaimVerdict
from packages.shared.errors import ValidationError
from packages.shared.logging import get_logger

log = get_logger(__name__)

PROMPT = Path(__file__).with_name("prompts") / "b5_verification.txt"

_NEGATIVE_SEVERITY = {
    ClaimVerdict.OVERREACH: "high",
    ClaimVerdict.UNSUPPORTED: "critical",
}


class ClaimNotSupportedError(ValidationError):
    """The verifier returned `overreach` or `unsupported` for this claim.

    Carries the structured result as well as the message. Raising is still the
    fail-closed behavior every existing caller sees, but B6 has to persist
    failed claims and report why, so the verdict and explanation must survive
    the raise instead of being flattened into prose.
    """

    def __init__(
        self,
        detail: str | None = None,
        *,
        result: VerificationResult | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(detail, **extra)
        self.result = result


class VerifierOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    verdict: Literal["supported", "overreach", "unsupported"]
    explanation: str = Field(min_length=1, max_length=500)


@dataclass(frozen=True)
class VerificationResult:
    verdict: ClaimVerdict
    explanation: str
    prompt_version: str


def is_authoritative_failure(verdicts: list[ClaimVerdict]) -> bool:
    """Fail-closed aggregation: any single negative verdict is authoritative.

    Not wired to a rerun loop today (the architecture makes one verifier call
    per claim), but kept as a pure, tested rule so a future rerun cannot be
    implemented by averaging or majority-voting a negative verdict away.
    """
    return any(v is not ClaimVerdict.SUPPORTED for v in verdicts)


def prepare_request(
    claim_text: str, cited_facts: list[TailoringFact], model: str
) -> Request:
    """Build the B5 request from only this claim's text and its own cited
    facts - nothing else reaches the prompt.
    """
    facts_payload = [
        {
            "fact_id": str(fact.id),
            "text": prompt_text(fact.text),
            "structured": fact.structured,
        }
        for fact in cited_facts
    ]
    system = PROMPT.read_text()
    version = hashlib.sha256(system.encode()).hexdigest()
    payload = {
        "claim_text": prompt_text(claim_text),
        "cited_facts": facts_payload,
        "context_digest": hashlib.sha256(
            canonical(
                {
                    "claim_text": claim_text,
                    "facts": [
                        {"id": str(f.id), "text": f.text, "structured": f.structured}
                        for f in cited_facts
                    ],
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
        schema=VerifierOutput.model_json_schema(),
        prompt_version=version,
    )


def validate_verdict(body: str, *, prompt_version: str) -> VerificationResult:
    def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        json.loads(body, object_pairs_hook=unique_keys)
        output = VerifierOutput.model_validate_json(body)
    except (SchemaError, ValueError, TypeError):
        raise ModelOutputError(
            "B5 output must contain only a structured verdict and explanation"
        ) from None

    # The explanation is model-generated prose about a claim that already
    # passed PII screening; still check it before it is ever recorded or
    # surfaced, per the same detector used everywhere else.
    assert_prompt_safe(output.explanation)

    return VerificationResult(
        verdict=ClaimVerdict(output.verdict),
        explanation=output.explanation,
        prompt_version=prompt_version,
    )


async def verify_claim(
    session: AsyncSession,
    *,
    claim_text: str,
    cited_facts: list[TailoringFact],
    aggregate_id: UUID,
    client: StructuredClient,
    writer: InferenceAuditWriter | None = None,
    claim_index: int | None = None,
) -> VerificationResult:
    """Execute B5 for one claim with a separate audit stage and spend ledger."""
    started = monotonic()
    if writer is None:
        writer = DomainInferenceAudit(session)
    stage = await writer.begin_stage(
        agent="resume_optimizer", pipeline="tailoring", step_name="B5",
        aggregate_type="job", aggregate_id=aggregate_id, correlation_id=aggregate_id,
    )
    # C3 supplies an ordinal from its ordered ClaimRef sequence. It is safe
    # attribution and never contains generated claim prose.
    attribution = {"claim_index": claim_index} if claim_index is not None else {}
    result: VerificationResult | None = None
    primary: BaseException | None = None
    evidence_failed = False
    completed = False
    try:
        request = prepare_request(claim_text, cited_facts, client.model)
        await writer.record_request(
            stage, prompt_version=request.prompt_version, input_digest=request.digest,
            guardrail_results=attribution,
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
                    raise ModelOutputError("B5 schema repair has no recorded response") from None
                raise
            call = await writer.record_call(
                stage, model=request.model, input_tokens=response.input_tokens,
                output_tokens=response.output_tokens, cost_usd=response.cost_usd,
                prompt_hash=request.digest, stop_reason=response.stop_reason,
            )
            if writer.spent(stage) > client.max_cost:
                raise CostLimitError("B5 exceeded its run budget")
            try:
                if response.stop_reason != "tool_use":
                    raise ModelOutputError(
                        "B5 response was truncated or did not use the verdict tool"
                    )
                result = validate_verdict(
                    response.body, prompt_version=request.prompt_version
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
            # A valid negative is a completed inference. B6 still receives its
            # structured verdict through ClaimNotSupportedError below.
            safe_output = {
                "verdict": str(result.verdict),
                "explanation": result.explanation,
            }
            await writer.record_call_output(stage, call, response_redacted=canonical(safe_output))
            await writer.record_step_output(stage, output=safe_output)
            client.record(
                request, response.model_copy(update={"body": canonical(safe_output)})
            )
            completed = True
            if result.verdict is not ClaimVerdict.SUPPORTED:
                raise ClaimNotSupportedError(
                    f"Claim verifier returned {result.verdict}: "
                    "not supported by cited evidence",
                    result=result,
                )
            return result
        raise ModelOutputError("B5 response validation failed")
    except BaseException as exc:
        primary = exc
        evidence_failed = isinstance(exc, InferenceAuditUnavailableError)
        if isinstance(exc, ClaimNotSupportedError | PiiInPromptError):
            pii = isinstance(exc, PiiInPromptError)
            verdict = result.verdict if result is not None else ClaimVerdict.UNSUPPORTED
            violation = {
                "kind": "pii_in_prompt" if pii else "fabrication",
                "agent": "resume_optimizer",
                "severity": (
                    "high" if pii else _NEGATIVE_SEVERITY.get(verdict, "critical")
                ),
                "aggregate_type": "job",
                "aggregate_id": str(aggregate_id),
                "detail": (
                    "B5 output contains unredacted private data"
                    if pii
                    else f"B5 verifier found the claim {verdict}"
                ),
                "action_taken": "run_failed",
                "step": "B5",
                "run_id": str(stage.run_id),
            }
            try:
                await writer.record_guardrail(
                    stage, guardrail_results={**attribution, "failure": violation},
                    event=EventEnvelope(
                        type="guardrail.violation.detected", aggregate_type="job",
                        aggregate_id=aggregate_id, correlation_id=aggregate_id,
                        actor_type="agent", actor_id="resume_optimizer", payload=violation,
                    ),
                )
            except BaseException as evidence_exc:
                evidence_failed = True
                if not isinstance(evidence_exc, Exception):
                    raise
                log.warning("b5.guardrail_evidence_failed", run_id=str(stage.run_id))
                # A missing guardrail record must not be mistaken for a valid
                # negative handoff by verify_claim_for_handoff().
                if isinstance(exc, ClaimNotSupportedError):
                    raise evidence_exc from exc
        raise
    finally:
        if not evidence_failed:
            try:
                await writer.finish_stage(
                    stage, status="succeeded" if completed else "failed",
                    steps_completed=1 if completed else 0,
                    duration_ms=int((monotonic() - started) * 1000),
                    error_type=(
                        type(primary).__name__
                        if primary is not None and not completed else None
                    ),
                    error_message=(
                        "B5 claim verification failed"
                        if primary is not None and not completed else None
                    ),
                )
            except Exception:
                if primary is None or completed:
                    raise
                log.warning("b5.outcome_evidence_failed", run_id=str(stage.run_id))
