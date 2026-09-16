"""AuditBoundary: evidence that outlives the transaction it describes.

**The gap this closes.** Batch 2 made the *attempt* durable, but the reasons
an attempt went wrong were not. Seven orchestration outcomes were recorded
only by `log.*` - a fenced-out failure report, a failure that could not be
written, a success that could not be recorded, a recovery that raised, a wait
that timed out, and the two reconciliation verdicts. Each of those is the
system saying "I know something went wrong and I am continuing anyway", and
each was invisible to anything but a log aggregator. One of them - the
recovery failure - hid a missing state-machine edge for an entire batch.

**Why a boundary rather than `emit()`.** `emit()` writes into the caller's
session, so its row lives or dies with the caller's transaction. That is
correct for a domain event, which describes a state change and must not
survive a rollback of that change. It is exactly wrong for a diagnostic,
which is usually *about* a rollback. So these rows are written on a separate
connection, in a separate transaction, that commits whatever the domain
session goes on to do.

**What it is not.** Not a logger - `log.*` calls stay exactly where they were,
because an operator tailing logs should not have to query PostgreSQL. Not a
second outbox - `app_audit` holds no UPDATE on `events`, so the relay's
`published` flag remains the relay's business. And not a participant in the
domain transaction: no method takes a domain session, none returns an ORM
instance, and nothing here can make a domain write happen or not happen.

**The allowlist is the API surface.** `ORCHESTRATION_EVENTS` is an explicit
frozenset, and `record()` refuses anything outside it. A string typo would
otherwise produce a row that no consumer queries and no test notices, which is
the failure mode this whole batch exists to stop repeating.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from packages.core.db.audit_session import audit_engine_ready, audit_session_scope
from packages.core.models import Event
from packages.shared.ids import uuid7
from packages.shared.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

log = get_logger(__name__)


class AuditUnavailableError(RuntimeError):
    """The audit write did not land.

    Raised only by `record()`. Callers that must not proceed without evidence
    catch this and stop; callers reporting a failure that is already in flight
    use `try_record()` instead and never see it.
    """


#: Every orchestration outcome the boundary may record, stated once.
#:
#: Names are `revision.attempt.*` because that is the aggregate they are
#: about, even though they are filed against the application (see
#: `AuditContext.as_payload`). A consumer wanting "everything that went wrong
#: with revision attempts" filters on this prefix; a consumer wanting "what
#: happened to this application" filters on `aggregate_id` and gets these
#: interleaved with its status transitions, which is where someone asking why
#: an application is stuck will actually look.
ORCHESTRATION_EVENTS: frozenset[str] = frozenset(
    {
        # T4': the failure report itself failed to persist.
        "revision.attempt.failure_record_failed",
        # T4': the fenced UPDATE matched no row - another worker owns this
        # attempt now and will record its own outcome.
        "revision.attempt.fenced_out_before_failure",
        # T4' seen from the caller: no evidence was written, for either
        # reason above. PO-4 says a failure leaves evidence; this says it did
        # not.
        "revision.attempt.failure_not_recorded",
        # T4: the resume is committed and correct, but the attempt never
        # recorded producing it, so the row is stranded in `running`.
        "revision.attempt.success_not_recorded",
        # T5: recovery raised. The application may still be in
        # `resume_generating` with nothing working on it.
        "revision.attempt.recovery_failed",
        # T2: the bounded wait expired without the holder finishing.
        "revision.attempt.wait_timed_out",
        # T3/T4 crash window: a committed result adopted, or not.
        "revision.attempt.reconciled",
        "revision.attempt.reconcile_skipped",
    }
)


# Separate from orchestration: these diagnose inference evidence failures only.
INFERENCE_AUDIT_EVENTS: frozenset[str] = frozenset(
    {
        "inference.audit.start_failed",
        "inference.audit.evidence_not_recorded",
        "inference.audit.outcome_not_recorded",
    }
)


@dataclass(frozen=True)
class AuditContext:
    """Which attempt, at which epoch, on which application and approval.

    **Primitives only, and frozen.** Every call site is downstream of a
    `session.rollback()` or can become so, and a rollback expires every
    persistent instance in a session regardless of `expire_on_commit`. An ORM
    field here would be a lazy refresh waiting to happen - in async, a
    `MissingGreenlet` raised from inside the handler for the real error, which
    is precisely how the diagnostic would destroy the diagnosis. It is also
    enforced from below: `app_audit` holds no privilege of any kind on
    `resume_revision_attempts`, so the boundary could not load the attempt
    even if it wanted to.

    `lease_epoch` is `None` wherever the caller does not hold one: the wait
    timeout, whose caller never acquired, and both reconciliation outcomes,
    whose epoch is taken and used inside a single guarded statement and never
    surfaced. Recording `0` in either case would be a plausible-looking lie in
    a column operators reason about.
    """

    attempt_id: UUID
    application_id: UUID
    approval_id: UUID
    lease_epoch: int | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "attempt_id": str(self.attempt_id),
            "application_id": str(self.application_id),
            "approval_id": str(self.approval_id),
            "lease_epoch": self.lease_epoch,
        }


class AuditBoundary:
    """Writes audit rows in transactions of its own.

    Stateless and cheap to construct: it holds no session, so there is no
    instance whose lifetime could accidentally span a domain transaction.
    """

    async def record(
        self,
        event_type: str,
        *,
        context: AuditContext,
        detail: Mapping[str, Any] | None = None,
    ) -> UUID:
        """Write one diagnostic and return its id. Raises if it did not land.

        The return is a `UUID`, not an `Event`: handing back an ORM instance
        would let a caller read it after its session closed, and would give the
        domain a reference into audit state it has no business holding.

        Use this where the absence of evidence must stop the caller - audit
        outage policy: no model work starts without a landed audit write.
        """
        if event_type not in ORCHESTRATION_EVENTS:
            # Deliberately not an audit failure: this is a programming error,
            # and it cannot be reached by an operational condition.
            raise ValueError(
                f"{event_type!r} is not an allowlisted audit event. "
                f"Add it to ORCHESTRATION_EVENTS with a comment saying what "
                f"it means, or use one of: {', '.join(sorted(ORCHESTRATION_EVENTS))}"
            )
        if not audit_engine_ready():
            raise AuditUnavailableError(
                "the audit engine is not initialised; no audit row was written"
            )

        payload = context.as_payload()
        if detail:
            # Detail never overwrites context. A caller cannot rewrite which
            # attempt a diagnostic is about by passing a key of the same name.
            payload["detail"] = dict(detail)

        event_id = uuid7()
        try:
            async with audit_session_scope() as session:
                session.add(
                    Event(
                        id=event_id,
                        type=event_type,
                        aggregate_type="application",
                        aggregate_id=context.application_id,
                        correlation_id=context.application_id,
                        # Not `human`: no person asked for this row. The
                        # orchestrator noticed something and said so.
                        actor_type="system",
                        actor_id="resume_rework",
                        payload=payload,
                    )
                )
        except Exception as exc:
            raise AuditUnavailableError(f"audit write failed: {type(exc).__name__}") from exc
        return event_id

    async def try_record(
        self,
        event_type: str,
        *,
        context: AuditContext,
        detail: Mapping[str, Any] | None = None,
    ) -> UUID | None:
        """`record()`, but never raising. Returns None if nothing landed.

        **This is what the orchestration failure paths use, and the reason is
        the whole audit-outage policy in one line: a secondary audit failure
        must never replace the primary exception.** Every call site here is
        already reporting something that went wrong; if the reporting also
        fails, the original error is still the one that matters and the one
        that must reach the caller.

        A programming error is different and is not swallowed - an event type
        outside the allowlist raises here too, because no operational
        condition can produce it and hiding it would defeat the allowlist.
        """
        if event_type not in ORCHESTRATION_EVENTS:
            raise ValueError(f"{event_type!r} is not an allowlisted audit event")
        try:
            return await self.record(event_type, context=context, detail=detail)
        except AuditUnavailableError:
            log.warning(
                "audit.diagnostic_not_recorded",
                event_type=event_type,
                attempt_id=str(context.attempt_id),
                application_id=str(context.application_id),
            )
            return None

    async def try_record_inference(
        self,
        event_type: str,
        *,
        context: AuditContext,
        detail: Mapping[str, Any] | None = None,
    ) -> UUID | None:
        """Best-effort inference diagnostic, never a domain outcome or lifecycle event."""
        if event_type not in INFERENCE_AUDIT_EVENTS:
            raise ValueError(f"{event_type!r} is not an allowlisted inference audit event")
        event_id = uuid7()
        payload = context.as_payload()
        if detail:
            payload["detail"] = dict(detail)
        try:
            async with audit_session_scope() as session:
                session.add(
                    Event(
                        id=event_id,
                        type=event_type,
                        aggregate_type="application",
                        aggregate_id=context.application_id,
                        correlation_id=context.application_id,
                        actor_type="system",
                        actor_id="inference_audit",
                        payload=payload,
                    )
                )
        except Exception:
            log.warning("audit.inference_diagnostic_not_recorded", event_type=event_type)
            return None
        return event_id
