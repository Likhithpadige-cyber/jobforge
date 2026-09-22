"""Inference evidence writers for B1, B2, B3, and per-claim B5.

Every independent operation commits once, on the audit connection. The domain
writer uses one savepoint per operation and never commits the caller's transaction.
Only primitive handles and an event envelope cross the public boundary.

Evidence operations are single-attempt, NON-RETRYABLE, including uncertain commits.
A failed operation poisons the writer: stop the stage, never retry a model call to
repair evidence. Observed provider spend is counted before persistence and remains
available after failure. Unknown provider outcomes must not be passed as zero usage.

C3 execution context is stored in agent_run_steps.guardrail_results.c3_execution;
run/step/call foreign keys provide attribution without new columns or grants.
The namespace is reserved, immutable, and never added to guardrail event payloads.
Callers supply already-redacted findings, outputs and events, exactly as today.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.db.audit_boundary import AuditBoundary, AuditContext
from packages.core.db.audit_session import audit_session_scope
from packages.core.events.envelope import EventEnvelope
from packages.core.models import AgentRun, AgentRunStep, Event, LlmCall
from packages.shared.clock import utc_now
from packages.shared.ids import uuid7
from packages.shared.logging import get_logger

log = get_logger(__name__)


class InferenceAuditUnavailableError(RuntimeError):
    """Evidence failed. Stop the stage; do not invoke or repair a model call."""


@dataclass(frozen=True)
class StageHandle:
    """One `agent_runs` row and its one `agent_run_steps` row, by id."""

    run_id: UUID
    step_id: UUID


@dataclass(frozen=True)
class CallHandle:
    """One `llm_calls` row, by id."""

    call_id: UUID


class InferenceAuditWriter(Protocol):
    """What a stage may ask for. Deliberately small and append-shaped.

    Every method takes primitives and returns primitives. There is no
    `session` parameter anywhere, so a stage cannot reach the connection
    underneath and cannot be handed the wrong one by a helpful caller.
    """

    async def begin_stage(
        self,
        *,
        agent: str,
        pipeline: str,
        step_name: str,
        aggregate_type: str,
        aggregate_id: UUID,
        correlation_id: UUID,
        steps_total: int = 1,
        step_index: int = 1,
    ) -> StageHandle: ...

    async def record_request(
        self,
        stage: StageHandle,
        *,
        prompt_version: str | None,
        input_digest: str | None,
        guardrail_results: Mapping[str, Any],
    ) -> None: ...

    async def record_call(
        self,
        stage: StageHandle,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: Decimal,
        prompt_hash: str | None,
        stop_reason: str | None,
    ) -> CallHandle: ...

    async def record_call_output(
        self, stage: StageHandle, call: CallHandle, *, response_redacted: str
    ) -> None: ...

    async def record_step_output(
        self, stage: StageHandle, *, output: Mapping[str, Any]
    ) -> None: ...

    async def record_guardrail(
        self,
        stage: StageHandle,
        *,
        guardrail_results: Mapping[str, Any],
        event: EventEnvelope | None = None,
    ) -> None: ...

    async def finish_stage(
        self,
        stage: StageHandle,
        *,
        status: str,
        steps_completed: int,
        duration_ms: int,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None: ...

    def spent(self, stage: StageHandle) -> Decimal: ...


def _json(value: Mapping[str, Any]) -> str:
    """Strict JSON: never stringify private objects, ORM instances or NaN."""
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


class _SpendLedger:
    def __init__(self) -> None:
        self._totals: dict[StageHandle, tuple[int, int, Decimal]] = {}

    def open(self, stage: StageHandle) -> None:
        self._totals[stage] = (0, 0, Decimal("0"))

    def require(self, stage: StageHandle) -> tuple[int, int, Decimal]:
        if stage not in self._totals:
            raise InferenceAuditUnavailableError("unknown stage handle")
        return self._totals[stage]

    def add(
        self, stage: StageHandle, input_tokens: int, output_tokens: int, cost: Decimal
    ) -> tuple[int, int, Decimal]:
        i, o, c = self.require(stage)
        total = (i + input_tokens, o + output_tokens, c + cost)
        self._totals[stage] = total
        return total

    def cost(self, stage: StageHandle) -> Decimal:
        return self.require(stage)[2]


async def _update(session: AsyncSession, sql: str, params: dict[str, Any]) -> None:
    """All statements name only granted columns and RETURNING verifies a match."""
    result = await session.execute(text(sql), params)
    if result.scalar_one_or_none() is None:
        raise InferenceAuditUnavailableError("audit update matched no row")


class DomainInferenceAudit:
    """Atomic savepoints in the caller's transaction; no independent durability.

    Releasing a savepoint does not commit evidence. The domain caller still owns
    commit/rollback. B1, B2, B3, and B5 use this writer by default for
    non-C3 calls.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._ledger = _SpendLedger()
        self._failed = False

    def _check(self, stage: StageHandle | None) -> None:
        if self._failed:
            raise InferenceAuditUnavailableError("writer stopped after failure; do not retry")
        if stage is not None:
            self._ledger.require(stage)

    @asynccontextmanager
    async def _operation(
        self, operation: str, stage: StageHandle | None = None
    ) -> AsyncIterator[AsyncSession]:
        self._check(stage)
        try:
            async with self._session.begin_nested():
                yield self._session
                await self._session.flush()
        except BaseException:
            self._failed = True
            raise

    def _findings(self, findings: Mapping[str, Any]) -> str:
        return _json(findings)

    async def begin_stage(
        self,
        *,
        agent: str,
        pipeline: str,
        step_name: str,
        aggregate_type: str,
        aggregate_id: UUID,
        correlation_id: UUID,
        steps_total: int = 1,
        step_index: int = 1,
    ) -> StageHandle:
        stage = StageHandle(uuid7(), uuid7())
        async with self._operation("begin_stage") as session:
            session.add(
                AgentRun(
                    id=stage.run_id,
                    agent=agent,
                    pipeline=pipeline,
                    status="running",
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    correlation_id=correlation_id,
                    steps_total=steps_total,
                    steps_completed=0,
                    input_tokens=0,
                    output_tokens=0,
                    cost_usd=Decimal("0"),
                )
            )
            await session.flush()
            session.add(
                AgentRunStep(
                    id=stage.step_id,
                    run_id=stage.run_id,
                    step_index=step_index,
                    step_name=step_name,
                    status="running",
                    guardrail_results=json.loads(self._findings({})),
                )
            )
        self._ledger.open(stage)
        return stage

    async def record_request(
        self,
        stage: StageHandle,
        *,
        prompt_version: str | None,
        input_digest: str | None,
        guardrail_results: Mapping[str, Any],
    ) -> None:
        async with self._operation("record_request", stage) as session:
            await _update(
                session,
                "UPDATE agent_runs SET prompt_version = :pv WHERE id = :run_id RETURNING id",
                {"pv": prompt_version, "run_id": stage.run_id},
            )
            await _update(
                session,
                "UPDATE agent_run_steps SET input_digest = :digest, "
                "guardrail_results = CAST(:g AS jsonb) "
                "WHERE id = :step_id AND run_id = :run_id RETURNING id",
                {
                    "digest": input_digest,
                    "g": self._findings(guardrail_results),
                    "step_id": stage.step_id,
                    "run_id": stage.run_id,
                },
            )

    async def record_call(
        self,
        stage: StageHandle,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: Decimal,
        prompt_hash: str | None,
        stop_reason: str | None,
    ) -> CallHandle:
        # Account even when opening the audit session itself fails. Check first
        # so a forbidden retry cannot count the same measured response twice.
        self._check(stage)
        totals = self._ledger.add(stage, input_tokens, output_tokens, cost_usd)
        async with self._operation("record_call", stage) as session:
            await _update(
                session,
                "UPDATE agent_runs SET input_tokens = :i, output_tokens = :o, cost_usd = :c "
                "WHERE id = :run_id AND EXISTS (SELECT 1 FROM agent_run_steps "
                "WHERE id = :step_id AND run_id = :run_id) RETURNING id",
                {
                    "i": totals[0],
                    "o": totals[1],
                    "c": totals[2],
                    "run_id": stage.run_id,
                    "step_id": stage.step_id,
                },
            )
            call = CallHandle(uuid7())
            session.add(
                LlmCall(
                    id=call.call_id,
                    run_id=stage.run_id,
                    step_id=stage.step_id,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd,
                    prompt_hash=prompt_hash,
                    stop_reason=(stop_reason or "")[:32] or None,
                    prompt_redacted=None,
                    response_redacted=None,
                )
            )
        return call

    async def record_call_output(
        self, stage: StageHandle, call: CallHandle, *, response_redacted: str
    ) -> None:
        async with self._operation("record_call_output", stage) as session:
            await _update(
                session,
                "UPDATE llm_calls SET response_redacted = :body "
                "WHERE id = :call_id AND run_id = :run_id AND step_id = :step_id RETURNING id",
                {
                    "body": response_redacted,
                    "call_id": call.call_id,
                    "run_id": stage.run_id,
                    "step_id": stage.step_id,
                },
            )

    async def record_step_output(self, stage: StageHandle, *, output: Mapping[str, Any]) -> None:
        async with self._operation("record_step_output", stage) as session:
            await _update(
                session,
                "UPDATE agent_run_steps SET output = CAST(:out AS jsonb) "
                "WHERE id = :step_id AND run_id = :run_id RETURNING id",
                {"out": _json(output), "step_id": stage.step_id, "run_id": stage.run_id},
            )

    async def record_guardrail(
        self,
        stage: StageHandle,
        *,
        guardrail_results: Mapping[str, Any],
        event: EventEnvelope | None = None,
    ) -> None:
        """Findings and optional existing event, atomically and exactly once.

        The caller supplies the existing redacted event envelope, including its
        id. We do not reconstruct its payload from findings or add C3 metadata.
        When requesting this event, the caller must not also emit it separately.
        """
        async with self._operation("record_guardrail", stage) as session:
            await _update(
                session,
                "UPDATE agent_run_steps SET guardrail_results = CAST(:g AS jsonb) "
                "WHERE id = :step_id AND run_id = :run_id RETURNING id",
                {
                    "g": self._findings(guardrail_results),
                    "step_id": stage.step_id,
                    "run_id": stage.run_id,
                },
            )
            if event is not None:
                if event.type != "guardrail.violation.detected":
                    raise ValueError("only guardrail.violation.detected is accepted")
                row = event.as_row()
                row["payload"] = json.loads(_json(event.payload))
                session.add(Event(**row))

    async def finish_stage(
        self,
        stage: StageHandle,
        *,
        status: str,
        steps_completed: int,
        duration_ms: int,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        async with self._operation("finish_stage", stage) as session:
            params = {
                "status": status,
                "done": steps_completed,
                "ms": duration_ms,
                "at": utc_now(),
                "etype": error_type,
                "emsg": error_message,
                "run_id": stage.run_id,
                "step_id": stage.step_id,
            }
            await _update(
                session,
                "UPDATE agent_runs SET status = CAST(:status AS run_status), "
                "steps_completed = :done, duration_ms = :ms, finished_at = :at, "
                "error_type = COALESCE(:etype, error_type), "
                "error_message = COALESCE(:emsg, error_message) "
                "WHERE id = :run_id RETURNING id",
                params,
            )
            await _update(
                session,
                "UPDATE agent_run_steps SET status = CAST(:status AS run_status), "
                "duration_ms = :ms, finished_at = :at "
                "WHERE id = :step_id AND run_id = :run_id RETURNING id",
                params,
            )

    def spent(self, stage: StageHandle) -> Decimal:
        return self._ledger.cost(stage)


class IndependentInferenceAudit(DomainInferenceAudit):
    """One fresh audit transaction per operation; no domain session is retained.

    Full context is required before any evidence can be recorded. Diagnostics
    use AuditBoundary separately after a failed transaction. They cannot replace
    the primary error. A poisoned writer cannot retry an uncertain commit.
    """

    def __init__(self, *, context: AuditContext) -> None:
        if context.lease_epoch is None:
            raise ValueError("independent inference evidence requires a lease_epoch")
        self._context = context
        self._ledger = _SpendLedger()
        self._failed = False

    def _findings(self, findings: Mapping[str, Any]) -> str:
        payload = dict(findings)
        if "c3_execution" in payload and payload["c3_execution"] != self._context.as_payload():
            raise ValueError("c3_execution is reserved for immutable execution context")
        payload["c3_execution"] = self._context.as_payload()
        return _json(payload)

    @asynccontextmanager
    async def _operation(
        self, operation: str, stage: StageHandle | None = None
    ) -> AsyncIterator[AsyncSession]:
        self._check(stage)
        try:
            async with audit_session_scope() as session:
                yield session
        except BaseException as exc:
            self._failed = True
            # Cancellation stays cancellation; a timeout has no fabricated usage.
            # Session close rolls back on BaseException, as well as normal errors.
            if not isinstance(exc, Exception):
                raise
            suffix = (
                "start_failed"
                if operation in {"begin_stage", "record_request"}
                else "outcome_not_recorded"
                if operation == "finish_stage"
                else "evidence_not_recorded"
            )
            try:
                await AuditBoundary().try_record_inference(
                    f"inference.audit.{suffix}",
                    context=self._context,
                    detail={
                        "operation": operation,
                        "error_type": type(exc).__name__,
                        "run_id": str(stage.run_id) if stage else None,
                        "step_id": str(stage.step_id) if stage else None,
                    },
                )
            except Exception:
                # Secondary diagnostics must not replace even a serialization error.
                log.warning("inference_audit.diagnostic_failed", operation=operation)
            raise InferenceAuditUnavailableError(
                f"inference audit {operation} failed: {type(exc).__name__}"
            ) from exc


class FirstDraftInferenceAudit(DomainInferenceAudit):
    """Independent first-draft evidence, attributed without inventing a C3 lease.

    Each operation commits separately. Any uncertain evidence write poisons this
    writer; M4 records the failed delivery and never retries first-draft inference.
    """

    def __init__(self, *, event_id: UUID, application_id: UUID, profile_id: UUID) -> None:
        self._execution = {"event_id": str(event_id), "application_id": str(application_id),
                           "profile_id": str(profile_id)}
        self._ledger = _SpendLedger()
        self._failed = False

    def _findings(self, findings: Mapping[str, Any]) -> str:
        payload = dict(findings)
        if "first_draft_execution" in payload:
            raise ValueError("first_draft_execution is reserved")
        payload["first_draft_execution"] = self._execution
        return _json(payload)

    @asynccontextmanager
    async def _operation(
        self, operation: str, stage: StageHandle | None = None
    ) -> AsyncIterator[AsyncSession]:
        self._check(stage)
        try:
            async with audit_session_scope() as session:
                yield session
        except BaseException as exc:
            self._failed = True
            if not isinstance(exc, Exception):
                raise
            raise InferenceAuditUnavailableError(
                f"first-draft audit {operation} failed"
            ) from exc
