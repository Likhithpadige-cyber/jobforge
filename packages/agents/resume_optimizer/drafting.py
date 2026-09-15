"""B2 content drafting only. No resume is persisted; only runtime audit rows.

B1 is the sole input. The caller passes the exact `SelectionResult` B1 produced
for this context; this module never re-selects facts and never sees any fact
that B1 did not already approve. Only the fields needed to phrase each
approved fact are placed in the prompt - not the full profile, and no job
description text, which B2 does not need (B1 already encoded emphasis).

`ProfileFact.structured` is documented as holding every numeral that may
legitimately appear in a resume bullet (packages/core/models/facts.py); the
numeral guard here checks against exactly that field, never against a fact's
free-form text.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
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
from packages.agents.resume_optimizer.context import TailoringContext, TailoringFact
from packages.agents.resume_optimizer.selection import SelectionResult
from packages.core.events import emit
from packages.core.models import AgentRun, AgentRunStep, FactStatus, LlmCall
from packages.shared.clock import utc_now
from packages.shared.errors import ValidationError
from packages.shared.ids import uuid7

PROMPT = Path(__file__).with_name("prompts") / "b2_drafting.txt"

#: A numeral token as it may appear in generated prose or in a fact's
#: structured payload: an optional leading minus sign (a genuine negative,
#: not a hyphenated range like "3-5" - the lookbehind excludes a "-"
#: immediately after a digit), digits, optional thousands separators, an
#: optional decimal part, and at most one trailing unit marker - percent,
#: plus, or a multiplier "x"/"X" (e.g. "8x", "10X") - which is a
#: quantitative claim like any other and must be pool-supported.
NUMERAL_RE = re.compile(r"(?<!\w)-?\d[\d,]*(?:\.\d+)?(?:%|\+|[xX])?(?!\w)")


class UnapprovedFactError(ValidationError):
    """A cited fact was not approved by B1, or is no longer verified."""


class NumeralGuardError(ValidationError):
    """A numeral in generated text is absent from every cited fact's data (G4)."""


class MixedSectionError(ValidationError):
    """A single bullet cites facts from more than one B1 section."""


class DraftedBullet(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    text: str = Field(min_length=1, max_length=600)
    fact_ids: list[str] = Field(min_length=1, max_length=8)
    numerals_used: list[str] = Field(default_factory=list, max_length=32)


class DraftingOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    bullets: list[DraftedBullet]


@dataclass(frozen=True)
class DraftingResult:
    bullets: tuple[DraftedBullet, ...]
    prompt_version: str


def _numeral_value(token: str) -> float:
    return float(token.rstrip("xX").rstrip("+").rstrip("%").replace(",", ""))


def extract_numerals(text: str) -> list[str]:
    return NUMERAL_RE.findall(text)


def _flatten_numeric_values(value: Any) -> set[float]:
    """Every numeral reachable inside a fact's `structured` payload."""
    values: set[float] = set()
    if isinstance(value, bool):
        return values
    if isinstance(value, int | float):
        values.add(float(value))
    elif isinstance(value, str):
        for token in NUMERAL_RE.findall(value):
            try:
                values.add(_numeral_value(token))
            except ValueError:
                continue
    elif isinstance(value, dict):
        for item in value.values():
            values |= _flatten_numeric_values(item)
    elif isinstance(value, list):
        for item in value:
            values |= _flatten_numeric_values(item)
    return values


def _numeral_pool(
    facts_by_id: dict[str, TailoringFact], fact_ids: list[str]
) -> set[float]:
    pool: set[float] = set()
    for fid in fact_ids:
        fact = facts_by_id.get(fid)
        if fact is not None:
            pool |= _flatten_numeric_values(fact.structured)
    return pool


def prepare_request(
    context: TailoringContext, selection: SelectionResult, model: str
) -> Request:
    """Build the B2 request from only the facts B1 already approved.

    No job description or other untrusted text is included: B2 needs none,
    since B1 already fixed which facts, in what order, with what emphasis.
    """
    facts_by_id = {
        str(f.id): f for f in context.facts if f.status == FactStatus.VERIFIED
    }
    ordered = sorted(selection.selections, key=lambda item: item.order)
    approved = [
        {
            "fact_id": item.fact_id,
            "section": item.section,
            "order": item.order,
            "emphasis": item.emphasis,
            "text": prompt_text(facts_by_id[item.fact_id].text),
        }
        for item in ordered
        if item.fact_id in facts_by_id
    ]
    system = PROMPT.read_text()
    version = hashlib.sha256(system.encode()).hexdigest()
    payload = {
        "selected_facts": approved,
        # Bind replay to the exact B1 output actually being drafted, and to
        # the referenced facts' current text/structured payload, without
        # sending any fact B1 did not select.
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
        schema=DraftingOutput.model_json_schema(),
        prompt_version=version,
    )


def validate_drafting(
    body: str,
    context: TailoringContext,
    selection: SelectionResult,
    *,
    prompt_version: str,
) -> DraftingResult:
    def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        json.loads(body, object_pairs_hook=unique_keys)
        output = DraftingOutput.model_validate_json(body)
    except (SchemaError, ValueError, TypeError):
        raise ModelOutputError(
            "B2 output must contain only structured bullets, without extra prose"
        ) from None

    approved = {item.fact_id: item for item in selection.selections}
    verified = {
        str(fact.id): fact
        for fact in context.facts
        if fact.status == FactStatus.VERIFIED
    }

    for bullet in output.bullets:
        sections: set[str] = set()
        for fid in bullet.fact_ids:
            if fid not in approved or fid not in verified:
                raise UnapprovedFactError(
                    "Cited fact was not approved by B1 selection, or is no longer verified"
                )
            sections.add(approved[fid].section)
        if len(sections) > 1:
            raise MixedSectionError(
                "A single bullet may not combine facts from different sections"
            )

        # G2-equivalent on the output side, checked before the numeral guard:
        # a numeral inside unredacted private data (e.g. a street number)
        # must be rejected as PII, not misreported as an unsupported
        # numeral. Reuses the same detector, never a second implementation,
        # applied to generated prose instead of a prompt.
        assert_prompt_safe(bullet.text)

        pool = _numeral_pool(verified, bullet.fact_ids)
        numerals = set(extract_numerals(bullet.text)) | set(bullet.numerals_used)
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

    return DraftingResult(tuple(output.bullets), prompt_version)


async def draft_content(
    session: AsyncSession,
    *,
    context: TailoringContext,
    selection: SelectionResult,
    client: StructuredClient,
) -> DraftingResult:
    """Execute only B2; persist audit metadata and validated bullets, never a resume."""
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
    step = AgentRunStep(
        id=uuid7(), run_id=run.id, step_index=1, step_name="B2", status="running"
    )
    session.add(run)
    await session.flush()
    session.add(step)
    await session.flush()
    try:
        request = prepare_request(context, selection, client.model)
        run.prompt_version = request.prompt_version
        step.input_digest = request.digest
        for attempt in range(2):
            try:
                response = await client.complete(request, spent=run.cost_usd)
            except ReplayMissError:
                if attempt:
                    raise ModelOutputError(
                        "B2 schema repair has no recorded response"
                    ) from None
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
                raise CostLimitError("B2 exceeded its run budget")
            try:
                if response.stop_reason != "tool_use":
                    raise ModelOutputError(
                        "B2 response was truncated or did not use the drafting tool"
                    )
                result = validate_drafting(
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
            safe_output = {"bullets": [item.model_dump() for item in result.bullets]}
            call.response_redacted = canonical(safe_output)
            step.output = safe_output
            client.record(
                request, response.model_copy(update={"body": canonical(safe_output)})
            )
            run.status = step.status = "succeeded"
            run.steps_completed = 1
            return result
        raise ModelOutputError("B2 response validation failed")
    except Exception as exc:
        run.status = step.status = "failed"
        run.error_type = type(exc).__name__
        run.error_message = "B2 content drafting failed"  # never store raw model text
        if isinstance(
            exc,
            UnapprovedFactError
            | NumeralGuardError
            | MixedSectionError
            | PiiInPromptError,
        ):
            pii = isinstance(exc, PiiInPromptError)
            violation = {
                "kind": "pii_in_prompt" if pii else "fabrication",
                "agent": "resume_optimizer",
                "severity": "high" if pii else "critical",
                "aggregate_type": "job",
                "aggregate_id": str(context.job["id"]),
                "detail": (
                    "B2 output contains unredacted private data"
                    if pii
                    else "B2 bullet is not grounded in B1-approved verified facts"
                ),
                "action_taken": "run_failed",
                "step": "B2",
                "run_id": str(run.id),
            }
            step.guardrail_results = {
                **(step.guardrail_results or {}),
                "failure": violation,
            }
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
