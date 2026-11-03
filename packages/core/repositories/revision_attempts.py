"""Durable revision attempts: every state change as one guarded statement.

C3_HARDENING_DESIGN.md D.2-D.6. Each method below issues a single
`UPDATE ... WHERE <predicate>` and returns whether it matched. That shape is
the whole design:

* **The predicate is the lock.** No read-then-write, so two workers cannot
  both observe "this attempt is free" and both act on it. The database decides.
* **0 rows is not an error.** It means the caller was fenced out - another
  worker took the lease, or the state moved on. The correct response is to
  stop writing, never to retry the write.
* **The decision and the transition it authorises are atomic.** D.6 evaluates
  "may this be retried?" and consumes the evidence-stale budget in the same
  statement, so there is no window between deciding and recording.

**This repository never commits.** Transaction boundaries belong to
`packages.core.services.resume_rework`, which owns T1-T5 explicitly; every
other repository here follows the same rule (routes commit, repositories do
not). Mixing the two is how a "helper" ends up committing half a workflow.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select, text

from packages.core.models import (
    ResumeRevisionAttempt,
    RevisionAttemptStatus,
    RevisionFailureClass,
    RevisionOutcome,
)
from packages.core.repositories.base import BaseRepository
from packages.core.revision_attempt_policy import retryable_sql
from packages.shared.errors import ConflictError
from packages.shared.ids import uuid7

if TYPE_CHECKING:  # pragma: no cover - typing only
    from packages.agents.resume_optimizer.revision import RevisionRequest

#: Fixed diagnostic written only by `release_for_generation_pause`.
GENERATION_PAUSE_DETAIL = "GenerationPausedError: released for operational pause"


class AttemptSourceConflictError(ConflictError):
    """A live attempt exists for this approval, bound to a different source.

    Never reused. The partial unique index guarantees one live attempt per
    approval, but "there is a row" is not the same as "it is this request's
    row": the source bindings are immutable by trigger, so an attempt bound to
    v1 cannot be re-pointed at v2, and quietly treating it as good enough
    would attach this request's result to another request's audit record.
    """


class RevisionAttemptRepository(BaseRepository[ResumeRevisionAttempt]):
    model = ResumeRevisionAttempt

    # ------------------------------------------------------------- T1
    async def resolve_or_create(
        self,
        request: RevisionRequest,
        *,
        requested_by: UUID,
        instructions_digest: str,
    ) -> ResumeRevisionAttempt:
        """The attempt for this approval: the existing live one, or a new one.

        `ON CONFLICT DO NOTHING` against `uq_revision_attempt_per_approval`
        means two concurrent first-clicks produce one row, with the loser
        falling through to the select rather than raising. The conflict target
        is the partial index, so an abandoned predecessor does not block a
        replacement.

        A returned existing row is validated against the request's immutable
        bindings before it is handed back - see `AttemptSourceConflictError`.
        """
        await self.session.execute(
            text(
                "INSERT INTO resume_revision_attempts ("
                "  id, application_id, source_resume_id, source_resume_version,"
                "  source_approval_id, instructions_snapshot, instructions_digest,"
                "  requested_by"
                ") VALUES ("
                "  :id, :application_id, :source_resume_id, :source_resume_version,"
                "  :source_approval_id, :instructions_snapshot, :instructions_digest,"
                "  :requested_by"
                ") ON CONFLICT DO NOTHING"
            ),
            {
                "id": uuid7(),
                "application_id": request.application_id,
                "source_resume_id": request.prior_resume_id,
                "source_resume_version": request.prior_resume_version,
                "source_approval_id": request.approval_id,
                "instructions_snapshot": request.instructions,
                "instructions_digest": instructions_digest,
                "requested_by": requested_by,
            },
        )
        attempt = await self.live_for_approval(request.approval_id)
        if attempt is None:  # pragma: no cover - the insert just guaranteed one
            raise ConflictError(
                f"no live revision attempt for approval {request.approval_id}"
            )
        self._assert_source_matches(attempt, request, instructions_digest)
        return attempt

    async def live_for_approval(self, approval_id: UUID) -> ResumeRevisionAttempt | None:
        """The one non-abandoned attempt for this approval, if any."""
        return (
            await self.session.execute(
                select(ResumeRevisionAttempt).where(
                    ResumeRevisionAttempt.source_approval_id == approval_id,
                    ResumeRevisionAttempt.status != RevisionAttemptStatus.ABANDONED,
                )
            )
        ).scalar_one_or_none()

    @staticmethod
    def _assert_source_matches(
        attempt: ResumeRevisionAttempt,
        request: RevisionRequest,
        instructions_digest: str,
    ) -> None:
        """Refuse to adopt an attempt that is not this request's.

        Each field here is immutable by trigger, so a mismatch can only mean
        the caller is asking about a different revision than the one on
        record - a state conflict, not a reusable row.
        """
        mismatches = [
            name
            for name, existing, incoming in (
                ("application_id", attempt.application_id, request.application_id),
                ("source_resume_id", attempt.source_resume_id, request.prior_resume_id),
                (
                    "source_resume_version",
                    attempt.source_resume_version,
                    request.prior_resume_version,
                ),
                ("instructions_digest", attempt.instructions_digest, instructions_digest),
            )
            if existing != incoming
        ]
        if mismatches:
            raise AttemptSourceConflictError(
                f"revision attempt {attempt.id} is bound to a different source "
                f"({', '.join(mismatches)} differ); it cannot be reused for this "
                "request. Release it explicitly if it should be replaced."
            )

    # ------------------------------------------------------------- T2
    async def acquire(
        self, attempt_id: UUID, *, owner: str, lease_seconds: int, max_attempts: int
    ) -> int | None:
        """Take the lease and return the epoch it was taken at, else None.

        Covers C.1 rows 2 and 3 in one statement: a fresh acquisition from
        `accepted` and a takeover of a `running` attempt whose lease expired
        are the same write. `lease_epoch + 1` is the fence - the previous
        owner's epoch no longer matches, so every statement it issues from
        here on matches 0 rows.

        **Returns the epoch, not the row, and that is the whole point.** This
        used to hand back `get_or_404(attempt_id)` and let the caller read
        `.lease_epoch` off it. That is a stale read: the UPDATE above is raw
        SQL, so it does not touch the instance already in the identity map,
        and with `expire_on_commit=False` a subsequent SELECT returns that
        same instance with its *pre-acquisition* attributes rather than
        overwriting them. The caller then fenced every later statement with
        the epoch from before it acquired, so `fail()` and `succeed()` matched
        zero rows and the attempt was silently stranded in `running` with a
        live lease.

        The epoch the fence needs is already in `RETURNING`. Reading it from
        there makes the whole class of mistake unrepresentable: there is no
        ORM instance in the protocol to go stale.
        """
        # An attempt handed back by `release_for_generation_pause` resumes the
        # acquisition the pause interrupted: no new count, no ceiling check.
        # Fencing (epoch + 1, live-lease predicate) is unchanged.
        row = (
            await self.session.execute(
                text(
                    "UPDATE resume_revision_attempts SET "
                    "  status = 'running',"
                    "  lease_owner = :owner,"
                    "  lease_epoch = lease_epoch + 1,"
                    "  lease_expires_at = now() + make_interval(secs => :lease_seconds),"
                    "  attempt_count = attempt_count + CASE WHEN status = 'accepted'"
                    "    AND failure_detail = :paused THEN 0 ELSE 1 END,"
                    "  failure_detail = CASE WHEN status = 'accepted'"
                    "    AND failure_detail = :paused THEN NULL ELSE failure_detail END,"
                    "  started_at = COALESCE(started_at, now()) "
                    "WHERE id = :id "
                    "  AND status IN ('accepted', 'running') "
                    "  AND (lease_expires_at IS NULL OR lease_expires_at < now()) "
                    "  AND (attempt_count < :max_attempts"
                    "       OR (status = 'accepted' AND failure_detail = :paused)) "
                    "RETURNING id, lease_epoch"
                ),
                {
                    "id": attempt_id,
                    "owner": owner,
                    "lease_seconds": lease_seconds,
                    "max_attempts": max_attempts,
                    "paused": GENERATION_PAUSE_DETAIL,
                },
            )
        ).one_or_none()
        return None if row is None else int(row[1])

    async def renew(
        self, attempt_id: UUID, *, owner: str, epoch: int, lease_seconds: int
    ) -> bool:
        """Extend the lease without touching epoch, count or status (D.5).

        Provided because it is part of the repository's contract and the
        fencing story is incomplete without it. Nothing schedules it yet:
        generation runs in-process, so there is no heartbeat to drive. The
        tests exercise it directly.
        """
        result = await self.session.execute(
            text(
                "UPDATE resume_revision_attempts SET "
                "  lease_expires_at = now() + make_interval(secs => :lease_seconds) "
                "WHERE id = :id AND status = 'running' "
                "  AND lease_owner = :owner AND lease_epoch = :epoch "
                "  AND lease_expires_at > now() "
                "RETURNING id"
            ),
            {
                "id": attempt_id,
                "owner": owner,
                "epoch": epoch,
                "lease_seconds": lease_seconds,
            },
        )
        return result.scalar_one_or_none() is not None

    # ------------------------------------------------------------- T4
    async def succeed(
        self,
        attempt_id: UUID,
        *,
        owner: str,
        epoch: int,
        result_resume_id: UUID,
        result_version: int,
        outcome: RevisionOutcome,
    ) -> bool:
        """Record the result and close the attempt, fenced.

        All four result columns land together because
        `ck_resume_revision_attempts_succeeded_requires_result` refuses a
        succeeded row without them - there is no ordering in which a
        half-linked success is durable.
        """
        result = await self.session.execute(
            text(
                "UPDATE resume_revision_attempts SET "
                "  status = 'succeeded',"
                "  result_resume_id = :result_resume_id,"
                "  result_version = :result_version,"
                "  outcome = CAST(:outcome AS revision_outcome),"
                "  finished_at = now(),"
                "  lease_owner = NULL,"
                "  lease_expires_at = NULL "
                "WHERE id = :id AND status = 'running' "
                "  AND lease_owner = :owner AND lease_epoch = :epoch "
                "RETURNING id"
            ),
            {
                "id": attempt_id,
                "owner": owner,
                "epoch": epoch,
                "result_resume_id": result_resume_id,
                "result_version": result_version,
                "outcome": outcome.value,
            },
        )
        return result.scalar_one_or_none() is not None

    # ------------------------------------------------------------ T4'
    async def fail(
        self,
        attempt_id: UUID,
        *,
        owner: str,
        epoch: int,
        failure_class: RevisionFailureClass,
        detail: str,
        max_attempts: int,
        evidence_stale_budget: int,
    ) -> str | None:
        """Report a technical failure. Returns the status written, or None.

        One statement decides retryability and applies it, so the budget
        cannot be spent twice by two reports racing. A retryable failure
        returns the attempt to `accepted` with the lease cleared;
        `attempt_count` is NOT incremented here - acquisition is the only
        place that moves - and the evidence-stale counter advances only for
        the class that is budgeted.

        A terminal failure sets `retry_exhausted` when the ceiling is what
        stopped it, which is diagnostic: the class alone does not say whether
        this was "never retryable" or "out of budget".
        """
        retryable = retryable_sql()
        params: dict[str, Any] = {
            "id": attempt_id,
            "owner": owner,
            "epoch": epoch,
            "failure_class": failure_class.value,
            "detail": detail,
            "max_attempts": max_attempts,
            "evidence_stale_budget": evidence_stale_budget,
        }
        row = (
            await self.session.execute(
                # retryable_sql() uses fixed internal enums and default bind names;
                # no user input is interpolated. Runtime values remain bound below.
                text(
                    "UPDATE resume_revision_attempts SET "  # noqa: S608
                    f"  status = CASE WHEN {retryable} THEN 'accepted'::revision_attempt_status "
                    "         ELSE 'failed'::revision_attempt_status END,"
                    "  failure_class = CAST(:failure_class AS revision_failure_class),"
                    "  failure_detail = :detail,"
                    f"  retry_exhausted = (NOT ({retryable}) AND attempt_count >= :max_attempts),"
                    "  evidence_stale_retry_count = evidence_stale_retry_count + CASE "
                    f"     WHEN :failure_class = 'evidence_stale' AND {retryable} THEN 1 "
                    "     ELSE 0 END,"
                    "  evidence_stale_retried_at = CASE "
                    f"     WHEN :failure_class = 'evidence_stale' AND {retryable} THEN now() "
                    "     ELSE evidence_stale_retried_at END,"
                    f"  finished_at = CASE WHEN {retryable} THEN finished_at ELSE now() END,"
                    "  lease_owner = NULL,"
                    "  lease_expires_at = NULL "
                    "WHERE id = :id AND status = 'running' "
                    "  AND lease_owner = :owner AND lease_epoch = :epoch "
                    "RETURNING status"
                ),
                params,
            )
        ).one_or_none()
        return None if row is None else str(row[0])

    async def release_for_generation_pause(
        self, attempt_id: UUID, *, owner: str, epoch: int
    ) -> bool:
        """Hand a running attempt back to `accepted` for an M12 generation pause.

        Not a failure report: an operational pause says nothing about the
        provider, the content or the request, so no `failure_class` is written
        and no retry budget is decided. Fenced exactly like `fail()` - only the
        current owner at the current epoch matches; a stale worker matches 0
        rows. The fixed `failure_detail` marks the row so the next `acquire()`
        continues this acquisition instead of charging a new one.
        """
        result = await self.session.execute(
            text(
                "UPDATE resume_revision_attempts SET "
                "  status = 'accepted',"
                "  failure_detail = :paused,"
                "  lease_owner = NULL,"
                "  lease_expires_at = NULL "
                "WHERE id = :id AND status = 'running' "
                "  AND lease_owner = :owner AND lease_epoch = :epoch "
                "RETURNING id"
            ),
            {
                "id": attempt_id,
                "owner": owner,
                "epoch": epoch,
                "paused": GENERATION_PAUSE_DETAIL,
            },
        )
        return result.scalar_one_or_none() is not None

    # --------------------------------------------- T3/T4 reconciliation
    async def reconcile_succeeded(
        self,
        attempt_id: UUID,
        *,
        owner: str,
        result_resume_id: UUID,
        result_version: int,
        outcome: RevisionOutcome,
    ) -> bool:
        """Close an attempt whose result was committed but never recorded.

        The crash window: T3 commits the resume, the process dies, T4 never
        runs. The domain result is durable and correct; the attempt is stranded
        in `running` (or `accepted`, if T4' got as far as reporting a retryable
        failure). Re-entry must adopt that result rather than generate a second
        version of the same request.

        Two statements because the trigger permits no shortcut: `accepted` may
        not go straight to `succeeded`, so the row is claimed into `running`
        first. Both transitions are in C.1; nothing here weakens the trigger.

        Deliberately does NOT increment `attempt_count` and does NOT check the
        ceiling. Reconciliation is bookkeeping for work that already happened,
        not a new attempt - charging it an acquisition would let a crash on the
        final permitted attempt strand a perfectly good resume for ever.

        Still fenced: only an attempt with no live lease is claimed. A lease
        that has not expired means the original worker may yet be alive and
        about to run its own T4, and two writers must not both decide.
        """
        claimed = (
            await self.session.execute(
                text(
                    "UPDATE resume_revision_attempts SET "
                    "  status = 'running',"
                    "  lease_owner = :owner,"
                    "  lease_epoch = lease_epoch + 1,"
                    "  lease_expires_at = now() + interval '1 minute' "
                    "WHERE id = :id "
                    "  AND status IN ('accepted', 'running') "
                    "  AND (lease_expires_at IS NULL OR lease_expires_at < now()) "
                    "RETURNING lease_epoch"
                ),
                {"id": attempt_id, "owner": owner},
            )
        ).one_or_none()
        if claimed is None:
            return False
        return await self.succeed(
            attempt_id,
            owner=owner,
            epoch=int(claimed[0]),
            result_resume_id=result_resume_id,
            result_version=result_version,
            outcome=outcome,
        )

    # ------------------------------------------------------------- T5
    async def recovery_state(self, attempt_id: UUID) -> tuple[str, bool] | None:
        """Does this attempt still hold the application? Read fresh, in SQL.

        Two questions that T5 must not conflate: *which* terminal bookkeeping
        applies (that is `status`, and only `failed` earns `recovered_at`), and
        *whether the application is still owned by this attempt*. A retryable
        failure answers "no" to the second while answering "accepted" to the
        first: `fail()` cleared the lease, so nothing is working on the
        application any more and leaving it in `resume_generating` strands it
        until a worker that does not exist in this batch picks it up.

        `releases_application` is therefore true for a terminal `failed` row
        AND for an `accepted` row that carries a recorded failure and holds no
        live lease. It is false for `running`, which is the guard that stops a
        live worker's application being recovered out from under it, and false
        for a never-run `accepted` row, which never took the application in the
        first place.

        Deliberately a raw SELECT returning scalars rather than an ORM load.
        Every writer above uses `text()` UPDATE, which does not refresh
        instances in the identity map, and with `expire_on_commit=False` an ORM
        read here could hand back values from before the failure was recorded.
        The lease comparison also belongs in SQL: `now()` must be the database
        clock, not this process's.
        """
        row = (
            await self.session.execute(
                text(
                    "SELECT status::text, ("
                    "  status = 'failed'"
                    "  OR (status = 'accepted'"
                    "      AND failure_class IS NOT NULL"
                    "      AND lease_owner IS NULL"
                    "      AND (lease_expires_at IS NULL OR lease_expires_at < now()))"
                    ") FROM resume_revision_attempts WHERE id = :id"
                ),
                {"id": attempt_id},
            )
        ).one_or_none()
        return None if row is None else (str(row[0]), bool(row[1]))

    async def mark_recovered(self, attempt_id: UUID) -> bool:
        """Stamp `recovered_at`, once (G.6).

        The guard is `recovered_at IS NULL`, so a second recovery matches 0
        rows and the application is transitioned exactly once however many
        times recovery is attempted.
        """
        result = await self.session.execute(
            text(
                "UPDATE resume_revision_attempts SET recovered_at = now() "
                "WHERE id = :id AND status = 'failed' AND recovered_at IS NULL "
                "RETURNING id"
            ),
            {"id": attempt_id},
        )
        return result.scalar_one_or_none() is not None
