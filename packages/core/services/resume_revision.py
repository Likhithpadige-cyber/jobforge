"""C3 - regenerate a resume after a reviewer requested changes.

One function does the work, and almost all of it is delegation. That is the
point: a revision is not a special kind of generation with relaxed rules, it is
the *same* generation - B1, B2, B3, B5, B6, in that order, with the same
validators - started from a different place and shown one extra block of
editorial guidance at B1.

    changes_requested -> resume_generating -> resume_ready
                                           -> verification_failed

Nothing here re-implements selection, drafting, verification, coverage or
persistence. If a rule about what may appear on a resume is being enforced, it
is being enforced by the module that has always enforced it.

**What is genuinely new, and therefore what this module is responsible for:**

* Establishing the revision input from persisted rows alone
  (`packages.agents.resume_optimizer.revision`), so the reviewer's note is the
  one a human stored rather than a string a caller supplied.
* Moving the application into `resume_generating` through the state machine, so
  B6 - which refuses to run anywhere else - can do its job.
* Idempotency. A retry must not produce v3.
* Leaving v1 exactly as it was, whatever happens.

**What is deliberately absent.** No auto-approval: a successful v2 lands in
`resume_ready` with no approval of any kind, and a human must start a fresh
review to create one bound to v2. No inheritance: v1's approval stays bound to
v1 and is never mutated, superseded or re-pointed. No auto-redraft: a v2 that
fails verification stops in `verification_failed` and waits for a human, the
same as a failed first attempt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from packages.agents.resume_optimizer.context import load_tailoring_context
from packages.agents.resume_optimizer.drafting import draft_content
from packages.agents.resume_optimizer.handoff import (
    bullet_refs,
    sentence_refs,
    verify_claim_for_handoff,
)
from packages.agents.resume_optimizer.resume_persistence import (
    PersistResult,
    persist_resume,
)
from packages.agents.resume_optimizer.revision import (
    RevisionNotAvailableError,
    RevisionRequest,
    assert_revisable,
    guidance_payload,
    revision_fingerprint,
)
from packages.agents.resume_optimizer.selection import select_content
from packages.agents.resume_optimizer.summary import draft_summary
from packages.core.db.inference_audit import InferenceAuditWriter
from packages.core.events import emit
from packages.core.models import Application, ApplicationStatus, Resume
from packages.core.state_machines.application import transition_application
from packages.shared.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from packages.agents.llm.runtime import StructuredClient

log = get_logger(__name__)

TAILORED = "tailored"

#: Recorded on the revised resume's `diff_vs_base`, and the thing a retry
#: recognises. `diff_vs_base` is the column DATABASE_DESIGN.md gives to "how
#: this version differs from what came before", which is precisely what a
#: revision is - so the provenance lives on the row it describes rather than in
#: a new table invented to hold it.
FINGERPRINT_KEY = "revision_fingerprint"


@dataclass(frozen=True)
class RevisionResult:
    """The outcome of a revision attempt, or of recognising a completed one."""

    application_id: UUID
    prior_resume_id: UUID
    prior_resume_version: int
    resume_id: UUID
    version: int
    verification_status: str
    application_status: ApplicationStatus
    claim_count: int
    #: True when this call recognised an existing v2 rather than producing one.
    reused: bool = False
    #: Reviewer-note categories that ask for factual authority, recorded so the
    #: outcome can be explained rather than silently differing from the ask.
    unsupported_instruction_kinds: tuple[str, ...] = ()


def _provenance(request: RevisionRequest) -> dict[str, object]:
    """What this version was revised from, and on whose instruction."""
    return {
        FINGERPRINT_KEY: revision_fingerprint(request),
        "revised_from_resume_id": str(request.prior_resume_id),
        "revised_from_version": request.prior_resume_version,
        "changes_requested_approval_id": str(request.approval_id),
        "instruction_screen": request.screen.as_payload(),
    }


async def _existing_revision(
    session: AsyncSession, request: RevisionRequest
) -> Resume | None:
    """A resume already produced for this exact revision, if there is one.

    Matching on the fingerprint rather than on "is there a newer version"
    distinguishes the two cases that matter: a retry of *this* revision, which
    must be recognised, and a later revision of a later version, which must
    not be.
    """
    fingerprint = revision_fingerprint(request)
    candidates = (
        (
            await session.execute(
                select(Resume)
                # Claims are eager-loaded because the reuse path reports a
                # claim count, and a lazy load on an async session raises
                # rather than quietly issuing a query.
                .options(selectinload(Resume.claims))
                .where(
                    Resume.application_id == request.application_id,
                    Resume.kind == TAILORED,
                    Resume.version > request.prior_resume_version,
                )
                .order_by(Resume.version.desc())
            )
        )
        .scalars()
        .all()
    )
    for resume in candidates:
        if (resume.diff_vs_base or {}).get(FINGERPRINT_KEY) == fingerprint:
            return resume
    return None


async def completed_revision(
    session: AsyncSession, *, application_id: UUID, request: RevisionRequest
) -> RevisionResult | None:
    """The result this revision already produced, if it produced one (T1).

    Called BEFORE the attempt is leased and before the application moves into
    `resume_generating`, because both of those are writes and a retry of a
    finished revision must not perform any. Splitting this out of the pipeline
    is what lets the transaction boundaries sit where C3_HARDENING_DESIGN.md
    puts them: T1 decides whether there is work, T2 commits to doing it.

    Also raises `RevisionNotAvailableError` when a NEWER version exists that
    this revision did not produce - a stale request, not a retry.
    """
    application = (
        await session.execute(
            select(Application).where(Application.id == application_id)
        )
    ).scalar_one()

    existing = await _existing_revision(session, request)
    if existing is not None:
        log.info(
            "resume_revision.idempotent",
            application_id=str(application_id),
            resume_id=str(existing.id),
            version=existing.version,
        )
        return RevisionResult(
            application_id=application_id,
            prior_resume_id=request.prior_resume_id,
            prior_resume_version=request.prior_resume_version,
            resume_id=existing.id,
            version=existing.version,
            verification_status=existing.verification_status,
            application_status=application.status,
            claim_count=len(existing.claims),
            reused=True,
            unsupported_instruction_kinds=request.screen.factual_demands,
        )

    # Only now, having ruled out a retry of this revision, does a newer version
    # mean something is wrong.
    newest = (
        await session.execute(
            select(Resume)
            .where(
                Resume.application_id == application_id,
                Resume.kind == TAILORED,
                Resume.version > request.prior_resume_version,
            )
            .order_by(Resume.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if newest is not None:
        raise RevisionNotAvailableError(
            f"resume v{newest.version} already exists for this application, and it "
            f"was not produced by this revision of v{request.prior_resume_version}"
        )
    return None


async def begin_generation(
    session: AsyncSession,
    *,
    application_id: UUID,
    request: RevisionRequest,
    actor_id: UUID,
) -> None:
    """T2. Move the application into generation and announce it.

    Separated from the pipeline so it commits WITH the lease acquisition. The
    pair is the point: an application in `resume_generating` always has a
    leased attempt explaining why, and a leased attempt always has an
    application that says work is underway. Committing them apart would allow
    a window where one is true and the other is not.

    Takes the actor's id, not the `User`. The orchestrator reaches this after
    `_acquire_or_wait` may have rolled back one or more times while waiting for
    a lease, and a rollback expires every persistent instance in the session
    regardless of `expire_on_commit=False`. A `User` argument would therefore
    be an expired instance whose first attribute read is a lazy refresh -
    `MissingGreenlet` in async - on the takeover path only, which is the kind
    of bug that ships. The id is all this function ever wanted.
    """
    application = (
        await session.execute(
            select(Application).where(Application.id == application_id)
        )
    ).scalar_one()
    assert_revisable(application)
    await transition_application(
        session,
        application,
        ApplicationStatus.RESUME_GENERATING,
        actor_type="human",
        actor_id=str(actor_id),
        reason=f"revision requested from v{request.prior_resume_version}",
        correlation_id=application_id,
    )
    await emit(
        session,
        type="resume.generation.requested",
        aggregate_type="application",
        aggregate_id=application_id,
        correlation_id=application_id,
        actor_type="human",
        actor_id=str(actor_id),
        payload={
            "application_id": str(application_id),
            "revised_from_resume_id": str(request.prior_resume_id),
            "revised_from_version": request.prior_resume_version,
            "changes_requested_approval_id": str(request.approval_id),
            # Categories, never the reviewer's prose: the note itself lives on
            # the approvals row, which is the record of what a human said.
            "instruction_screen": request.screen.as_payload(),
        },
    )


async def recover_to_changes_requested(
    session: AsyncSession,
    *,
    application: Application,
    actor_id: UUID,
    attempt_id: UUID,
) -> None:
    """T5. Return a stranded application to `changes_requested` (G.6, PO-1).

    Technical failures only. A semantic negative never reaches here: it is a
    SUCCEEDED attempt whose content did not verify, and its application ends
    at `verification_failed` by B6's own route.

    Takes the actor's id for the reason in `begin_generation` above, and more
    acutely: this function runs *only* after a rollback, so a `User` argument
    is always expired here. `application` is different - the caller loads it
    fresh under `FOR UPDATE` after that rollback, which is why it may stay an
    ORM instance.
    """
    await transition_application(
        session,
        application,
        ApplicationStatus.CHANGES_REQUESTED,
        actor_type="human",
        actor_id=str(actor_id),
        reason=f"revision attempt {attempt_id} failed technically; returned for retry",
        correlation_id=application.id,
    )
    log.info(
        "resume_revision.recovered",
        application_id=str(application.id),
        attempt_id=str(attempt_id),
    )


async def generate_revision(
    session: AsyncSession,
    *,
    application_id: UUID,
    client: StructuredClient,
    request: RevisionRequest,
    writer: InferenceAuditWriter,
) -> RevisionResult:
    """T3. Generate, verify and persist the next resume version.

    Renamed from `regenerate_after_changes` deliberately. That name described
    a composite that loaded its own request, detected a completed revision and
    moved the application into generation; this does none of those. Keeping
    the old name for a function with a new contract is how a caller ends up
    assuming idempotency it no longer has - generation does not check whether
    the work is already done, because by the time it runs, orchestration has
    established that it is not.

    **No actor argument.** It had one, unused: T2 already recorded who asked
    for the revision, and the pipeline attributes its own work to the agent.
    An unused `User` parameter is not free here - `_acquire_or_wait` may have
    rolled back while waiting for a lease, which expires every instance in the
    session, so the parameter was a live `MissingGreenlet` waiting for the
    first line that read it.

    Assumes T1 ruled out a retry and T2 took the lease and moved the
    application into `resume_generating`. `request` is passed in rather than
    re-loaded so every stage of one attempt reasons about the same source
    cycle, whatever has happened to "the latest" since.

    The caller must supply the inference writer explicitly. C3 orchestration
    constructs it from the committed T2 lease; there is no domain-writer default.
    Only B1 uses it in this slice. B2/B3/B5 keep their existing session behavior.

    Does not commit: the caller owns this boundary, and the resume, its claims
    and its verification status must land together or not at all.
    """
    (
        await session.execute(
            select(Application).where(Application.id == application_id)
        )
    ).scalar_one()

    # B0. Loaded fresh, from the current verified facts - not from a snapshot
    # taken when v1 was generated. A fact retired since then is gone, and a
    # fact verified since then is available, which is the correct behaviour
    # for a resume that is about to be re-verified anyway.
    context = await load_tailoring_context(
        session, profile_id=request.profile_id, job_id=request.job_id
    )

    # B1 with the editorial block; B2 and B3 exactly as a first generation runs
    # them, from B1's revised selection and nothing else.
    selection = await select_content(
        session,
        context=context,
        client=client,
        revision=guidance_payload(request),
        writer=writer,
    )
    drafting = await draft_content(
        session, context=context, selection=selection, client=client
    )
    summary = await draft_summary(
        session, context=context, selection=selection, client=client
    )

    # B5 on every claim, with no reuse of v1's verdicts. A sentence that is
    # word-for-word what v1 said is verified again: the fact behind it may have
    # changed, and a verdict is about a claim at a moment, not for ever.
    refs = bullet_refs(drafting.bullets, selection) + sentence_refs(summary.sentences)
    verified = [
        await verify_claim_for_handoff(
            session,
            claim=ref,
            context=context,
            aggregate_id=request.job_id,
            client=client,
        )
        for ref in refs
    ]

    # B6: coverage gate, append-only version, claims, status move, events.
    result: PersistResult = await persist_resume(
        session,
        application_id=application_id,
        verified=tuple(verified),
        bullets=drafting.bullets,
        sentences=summary.sentences,
        context=context,
        selection=selection,
        model=client.model,
        prompt_version=f"{selection.prompt_version}/{drafting.prompt_version}",
    )

    # Provenance is written after B6 so it describes a row that exists. It is
    # additive metadata on the new version only; v1 is not touched.
    revised = (
        await session.execute(select(Resume).where(Resume.id == result.resume_id))
    ).scalar_one()
    revised.diff_vs_base = {**(revised.diff_vs_base or {}), **_provenance(request)}
    await session.flush()

    log.info(
        "resume_revision.completed",
        application_id=str(application_id),
        prior_version=request.prior_resume_version,
        version=result.version,
        verification_status=result.verification_status,
    )
    return RevisionResult(
        application_id=application_id,
        prior_resume_id=request.prior_resume_id,
        prior_resume_version=request.prior_resume_version,
        resume_id=result.resume_id,
        version=result.version,
        verification_status=result.verification_status,
        application_status=result.to_status,
        claim_count=result.claim_count,
        unsupported_instruction_kinds=request.screen.factual_demands,
    )


__all__ = [
    "FINGERPRINT_KEY",
    "RevisionNotAvailableError",
    "RevisionResult",
    "begin_generation",
    "completed_revision",
    "generate_revision",
    "recover_to_changes_requested",
]
