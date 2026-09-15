"""C3 - the revision input boundary and the reviewer-note screen.

**The one rule this module exists to enforce.** An LLM may write language;
verified facts decide truth. A reviewer's note is editorial guidance - what to
emphasise, what to drop, what to say more plainly - and it is never evidence.
Nothing a reviewer types can make a claim true.

That rule is not enforced by reading the note carefully. It is enforced by
where the note is allowed to go, and by what stands downstream of it:

* The note reaches exactly one stage, B1 selection, inside the `<untrusted>`
  envelope B1's prompt already hardens against. B1's output is a list of
  `fact_id`s drawn from the verified set; `UngroundedSelectionError` refuses
  anything else. So the strongest thing a note can do is change *which
  verified facts* are selected, in what order, with what emphasis - which is
  precisely the editorial surface a reviewer is entitled to.
* B2 and B3 never see the note at all. Their prompts state that no untrusted
  data is present in the request, and that statement stays true. They redraft
  from B1's revised selection, so phrasing changes arrive as a consequence of
  selection and emphasis rather than by a reviewer dictating sentences.
* Downstream, nothing is relaxed: the numeral guard still rejects any figure
  absent from a cited fact's `structured` payload, B5 still verifies every
  claim independently, `validate_coverage` still demands exact 1:1 binding,
  and B6 still refuses to mark a resume `passed` with a failed claim.

So "Add that I increased revenue by 40%" cannot become a supported claim: no
verified fact carries that figure, so the numeral guard rejects it at B2 if
the model writes it at all, and B5 rejects it after that. "Change my title to
Senior Cloud Architect" has no fact to cite, so B1 cannot select it and B5
cannot support it. The screen below does not create that safety - it reports
it, so the outcome is a visible refusal rather than a silent omission.

`screen_instructions` is therefore *advisory and recorded*, never a gate that
the truth boundary depends on. Its job is to let the system say "these parts
of your note were not applied as fact, and here is why" instead of leaving a
reviewer to wonder why their instruction vanished.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from packages.agents.base.injection import ScanResult, scan
from packages.agents.resume_optimizer.drafting import extract_numerals
from packages.core.models import (
    Application,
    ApplicationStatus,
    Approval,
    ApprovalKind,
    ApprovalStatus,
    Resume,
)
from packages.shared.errors import ConflictError, NotFoundError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

TAILORED = "tailored"


class RevisionNotAvailableError(ConflictError):
    """The application is not in a state a revision can be produced from."""


# ---------------------------------------------------------------------------
# The reviewer-note screen
# ---------------------------------------------------------------------------

#: Instructions that demand a *factual* assertion rather than an editorial
#: change. Written for first-person reviewer prose ("say I have...", "add that
#: I..."), which is the register a reviewer writes in - deliberately separate
#: from `packages.agents.base.injection`, whose fabrication patterns are tuned
#: for third-person attacker text in a job description. Widening that shared
#: G1 scanner to first person would raise its false-positive rate on every job
#: posting in the system to catch a case only this module has.
FACTUAL_DEMAND_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "asserts_experience",
        re.compile(
            r"\b(say|state|claim|add|include|mention|put|write|note)\b"
            r"[^.\n]{0,30}\b(that\s+)?(i|my|me|we)\b[^.\n]{0,60}"
            r"\b(have|has|had|am|was|were|hold|held|led|built|managed|"
            r"increased|reduced|saved|earned|delivered|own|owned)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "changes_title",
        re.compile(
            r"\b(change|update|set|make|rename)\b[^.\n]{0,20}"
            r"\b(my\s+)?(title|role|position|seniority|level)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "adds_unverified_skill",
        re.compile(
            r"\b(add|include|list|put)\b[^.\n]{0,40}"
            r"\beven if\b[^.\n]{0,40}\b(not|isn'?t|aren'?t)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "asserts_duration",
        re.compile(
            r"\b\d+\s*(\+\s*)?(years?|yrs?|months?)\b",
            re.IGNORECASE,
        ),
    ),
)


@dataclass(frozen=True)
class InstructionScreen:
    """What a reviewer's note asks for, split by whether it can be honoured.

    `factual_demands` are the phrases that ask the system to assert something.
    They are recorded and reported, and they are still passed to B1 inside the
    untrusted envelope - removing them would be a second, weaker truth
    boundary competing with the real one, and a reviewer asking for something
    a verified fact *does* support ("say I led the migration", where a fact
    says exactly that) should get it.

    What the flag buys is honesty: if the regenerated resume does not contain
    what the note asked for, the system can point at the specific sentence and
    say it was not supported, rather than silently dropping it.
    """

    instructions: str
    factual_demands: tuple[str, ...]
    numerals: tuple[str, ...]
    scan: ScanResult

    @property
    def demands_facts(self) -> bool:
        return bool(self.factual_demands) or bool(self.numerals)

    def as_payload(self) -> dict[str, Any]:
        """Audit shape, carrying the categories rather than the prose."""
        return {
            "factual_demand_kinds": sorted(set(self.factual_demands)),
            "numerals_in_note": list(self.numerals),
            "injection": {
                k: v for k, v in self.scan.as_payload().items() if k != "excerpts"
            },
        }


def screen_instructions(note: str) -> InstructionScreen:
    """Classify a reviewer note. Deterministic; no model call, no network.

    Also runs the note through the shared G1 injection scanner, because a
    reviewer's note is prose that reaches a prompt and therefore deserves the
    same treatment a job description gets. As there, a finding never rejects
    the note - it is recorded and the text continues as data.
    """
    findings = scan(note)
    text = findings.text
    demands = tuple(
        name for name, pattern in FACTUAL_DEMAND_PATTERNS if pattern.search(text)
    )
    return InstructionScreen(
        instructions=text,
        factual_demands=demands,
        numerals=tuple(extract_numerals(text)),
        scan=findings,
    )


# ---------------------------------------------------------------------------
# The revision input boundary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PriorClaim:
    """One claim from the version under revision, as B1 will be shown it."""

    section: str
    path: str
    claim_text: str
    fact_ids: tuple[str, ...]

    def as_payload(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "path": self.path,
            "claim_text": self.claim_text,
            "fact_ids": list(self.fact_ids),
        }


@dataclass(frozen=True)
class RevisionRequest:
    """Everything a revision is allowed to be built from, loaded from the DB.

    Every field here comes from a persisted row. Nothing in it can be supplied
    by an API caller: the endpoint that will front this takes an application id
    and a session cookie, and this object is assembled from what those two
    identify. That is what makes "the reviewer note" mean the note a human
    actually stored, rather than a string someone posted.

    `screen` carries the reviewer's instructions; `prior_claims` carries the
    text being revised. Neither is evidence - the evidence is the verified
    facts the tailoring context loads separately, exactly as for a first
    generation.
    """

    application_id: UUID
    profile_id: UUID
    job_id: UUID
    prior_resume_id: UUID
    prior_resume_version: int
    approval_id: UUID
    screen: InstructionScreen
    prior_claims: tuple[PriorClaim, ...]
    prior_content: dict[str, Any]

    @property
    def instructions(self) -> str:
        return self.screen.instructions

    @property
    def cited_fact_ids(self) -> tuple[str, ...]:
        """Every fact the prior version cited, de-duplicated, in a stable order.

        Context, not authorisation: B1 re-selects from the *current* verified
        set, so a fact retired since v1 simply is not selectable any more. This
        is here so the prior version can be described accurately, never to
        re-admit a fact the fact store no longer vouches for.
        """
        seen: list[str] = []
        for claim in self.prior_claims:
            for fid in claim.fact_ids:
                if fid not in seen:
                    seen.append(fid)
        return tuple(seen)


def assert_revisable(application: Application) -> None:
    """A revision may only start from `changes_requested`.

    Separated from loading because the two questions are asked at different
    moments. Loading answers "what would this revision be built from", which is
    still answerable after a revision has completed and moved the application
    on; this answers "may a new revision start now", which is not. A caller
    that needs to recognise its own completed work asks the first without the
    second - see `packages.core.services.resume_revision`.
    """
    if application.status is not ApplicationStatus.CHANGES_REQUESTED:
        raise RevisionNotAvailableError(
            "a revision can only be produced from changes_requested, not "
            f"{application.status}"
        )


async def load_revision_request(
    session: AsyncSession,
    application_id: UUID,
    *,
    lock: bool = False,
    require_changes_requested: bool = True,
) -> RevisionRequest:
    """Assemble the revision input from persisted state alone.

    Refuses unless the application is in `changes_requested` with a
    `changes_requested` RESUME approval naming a resume that still exists on
    this application.

    **The prior version is resolved from the approval, not from "the latest".**
    That is deliberate, and it is what makes a retry possible: once a revision
    has produced v2, "the latest" is v2, and resolving that way would make a
    second, identical call look like a stale request for v1 instead of a repeat
    of the revision that produced v2. The approval names the version a human
    actually read, which is the only version a revision can honestly be *of*.

    Whether some *other* newer version has appeared is a separate question with
    a separate answer, and it belongs to the caller that knows which versions
    are its own - see `packages.core.services.resume_revision`.
    """
    stmt = select(Application).where(Application.id == application_id)
    if lock:
        stmt = stmt.with_for_update()
    application = (await session.execute(stmt)).scalar_one_or_none()
    if application is None:
        raise NotFoundError(f"applications {application_id} not found")
    if require_changes_requested:
        assert_revisable(application)

    approval = (
        await session.execute(
            select(Approval)
            .where(
                Approval.application_id == application_id,
                Approval.kind == ApprovalKind.RESUME,
                Approval.status == ApprovalStatus.CHANGES_REQUESTED,
            )
            .order_by(Approval.decided_at.desc(), Approval.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if approval is None:
        raise RevisionNotAvailableError(
            f"application {application_id} has no changes-requested decision to revise"
        )
    note = (approval.changes_requested or approval.decision_note or "").strip()
    if not note:
        # C2 refuses to record a decision without one, so this means the row
        # was written by something else. Guessing what the reviewer meant is
        # not an option.
        raise RevisionNotAvailableError(
            "the changes-requested decision carries no instructions to revise against"
        )

    snapshot = approval.subject_snapshot or {}
    try:
        prior_id = UUID(str(snapshot.get("resume_id")))
    except (TypeError, ValueError):
        raise RevisionNotAvailableError(
            "the changes-requested decision does not name the resume it was about"
        ) from None

    resume = (
        await session.execute(
            select(Resume)
            .options(selectinload(Resume.claims))
            .where(
                Resume.id == prior_id,
                Resume.application_id == application_id,
                Resume.kind == TAILORED,
            )
        )
    ).scalar_one_or_none()
    if resume is None:
        raise RevisionNotAvailableError(
            f"the resume the reviewer decided about ({prior_id}) is not a tailored "
            f"resume of application {application_id}"
        )
    if snapshot.get("resume_version") != resume.version:
        # The snapshot and the row disagree about the same id, which means one
        # of them was rewritten. Neither is safe to prefer.
        raise RevisionNotAvailableError(
            f"the decision records resume {prior_id} at "
            f"v{snapshot.get('resume_version')}, but that row is now "
            f"v{resume.version}"
        )

    claims = tuple(
        PriorClaim(
            section=claim.section,
            path=claim.path,
            claim_text=claim.claim_text,
            fact_ids=tuple(str(fid) for fid in claim.fact_ids),
        )
        for claim in sorted(resume.claims, key=lambda c: c.path)
    )
    return RevisionRequest(
        application_id=application_id,
        profile_id=resume.profile_id,
        job_id=application.job_id,
        prior_resume_id=resume.id,
        prior_resume_version=resume.version,
        approval_id=approval.id,
        screen=screen_instructions(note),
        prior_claims=claims,
        prior_content=dict(resume.content),
    )


def guidance_payload(request: RevisionRequest) -> dict[str, Any]:
    """The `revision` block B1's prompt payload carries, and nothing more.

    Three things: which version is being revised, what the reviewer asked for,
    and what the previous version said. No profile, no job description beyond
    what B1 already receives, no fact text beyond what B1 already receives, and
    no verdicts - a previous `supported` verdict is not a licence to reuse a
    sentence without re-verifying it.
    """
    return {
        "prior_resume_version": request.prior_resume_version,
        "reviewer_instructions": request.instructions,
        "prior_claims": [claim.as_payload() for claim in request.prior_claims],
    }


def revision_fingerprint(request: RevisionRequest) -> str:
    """The idempotency key: which application, revised from what, on whose say-so.

    Deliberately not a client-supplied token. A retry is "the same revision"
    when it is the same application at the same prior version under the same
    recorded decision - which is exactly the triple a caller cannot vary
    without the underlying state having genuinely moved on.
    """
    return (
        f"{request.application_id}:{request.prior_resume_id}:"
        f"{request.prior_resume_version}:{request.approval_id}"
    )
