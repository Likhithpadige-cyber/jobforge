"""M9 - prepare a structured application package from verified evidence.

`prepare_application` is the whole pipeline in one call: classify every
question, ground what can be grounded in verified evidence, draft what may be
auto-answered, leave everything else an honest blank, persist, transition
`resume_ready -> questions_pending`, and emit. It never submits anything -
submission is M10's, and nothing here chooses a submission method or touches
`application.ready`.

**Where each answer's provenance comes from, and why it is never fabricated**
(the rule this whole module exists to keep, stated once so every function
below can be read against it):

* `source = 'agent'` - grounded in one or more `profile_facts` rows, cited by
  real `fact_ids`. Every fact is re-checked (belongs to this profile,
  `status = 'verified'`, `deleted_at IS NULL`) at draft time. Never a
  candidate preference, never a profile column - `chk_application_answers_agent_requires_facts`
  makes an uncited `agent` answer a database error, not just a code review
  finding.
* `source = 'derived'` - deterministically computed from something that is
  not a `profile_facts` row: a direct `profiles` column (email, city,
  willing_to_relocate), a candidate preference
  (`profiles.accepted_employment_types`), or a simple arithmetic derivation
  (years of experience from `experiences` date ranges). `fact_ids` stays
  empty - citing one would be inventing a fact for something that is not
  one - and `value_structured["derived_from"]` names what grounded it
  instead, so "why was this answer produced" is answerable without a fact
  citation to point at.
* `source = 'human'` - a person typed it, via `edit_answer`.
* `source = 'blank'` - unresolved. `value` is `NULL`, never a sentinel
  string. This is the answer for: a sensitive question (always, no
  exception), a judgement call, an unanswerable question (no fact exists),
  and anything the classifier could not confidently place.

**Ordering, matching M8's `_revalidate`-immediately-before-persist
discipline**: every cited fact is checked once at draft time and again
immediately before the `answers` approval's subject snapshot is built
(`revalidate_cited_facts`), because drafting and approval are not the same
moment and a fact can be retired or unverified in between.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.events import EventEnvelope, emit, record_event
from packages.core.models import (
    Application,
    ApplicationAnswer,
    ApplicationQuestion,
    ApplicationStatus,
    Approval,
    ApprovalKind,
    ApprovalStatus,
    Experience,
    Job,
    Profile,
    ProfileFact,
    User,
)
from packages.core.models.enums import AnswerSource, FactStatus, QuestionClass
from packages.core.policies.sensitive_questions import is_sensitive
from packages.core.state_machines.application import transition_application
from packages.shared.clock import utc_now, utc_today
from packages.shared.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from packages.shared.ids import uuid7
from packages.shared.logging import get_logger

log = get_logger(__name__)

#: Question classes that never carry a system-produced value at all - the
#: draft is a blank, full stop, regardless of any pattern match.
_ALWAYS_BLANK = frozenset({QuestionClass.SENSITIVE, QuestionClass.JUDGEMENT})

#: Statuses only reachable once `prepare_application` has already committed
#: for the application's current `resume_ready` episode. Used to detect a
#: replay without depending on whether that run produced any rows.
_ALREADY_PREPARED = frozenset(
    {
        ApplicationStatus.QUESTIONS_PENDING,
        ApplicationStatus.AWAITING_APPROVAL,
        ApplicationStatus.APPROVED,
    }
)

#: `profiles` columns a question may be directly grounded in, keyed by the
#: pattern that recognises the question. Checked in order; the first match
#: wins. Deliberately small and explicit rather than a generic field-name
#: matcher, so every entry is a reviewable decision about what is safe to
#: auto-answer from a profile column.
_PROFILE_FIELD_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), field_name)
    for pattern, field_name in (
        (r"\bfull(?: legal)? name\b|\byour name\b", "full_name"),
        (r"\be-?mail\b", "email"),
        (r"\bphone(?: number)?\b", "phone"),
        (r"\bcity\b", "location_city"),
        (r"\bstate\b|\bprovince\b", "location_state"),
        (r"\bcountry\b", "location_country"),
        (r"\bwilling(?:ness)? to relocate\b|\brelocat", "willing_to_relocate"),
    )
)

_YEARS_OF_EXPERIENCE = re.compile(r"\byears? of experience\b", re.IGNORECASE)

#: Forward-looking preference/opinion questions. Distinct from the hard-coded
#: `sensitive` list (AGENT_DESIGN.md §4.2): these still require a human, but
#: for a different reason - there is no fact to be wrong about, only a
#: decision only the candidate can make.
_JUDGEMENT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bsalary expect",
        r"\bexpected (salary|compensation)\b",
        r"\bdesired salary\b",
        r"\bstart date\b",
        r"\bavailable to start\b",
        r"\bnotice period\b",
        r"\bwhy (do you want|are you interested)\b",
        r"\bwhy this (company|role|position)\b",
        r"\btell us about\b",
        r"\bwhat interests you\b",
    )
)

#: A fact matches a question when the question text contains this many
#: characters of the fact's own text, case-insensitively - a deliberately
#: crude, deterministic, explainable match. No embedding search, no model:
#: a keyword a human can see is the whole point of "explain why an answer
#: was produced".
_MIN_KEYWORD_OVERLAP = 4


@dataclass(frozen=True)
class DraftedAnswer:
    """Everything `prepare_application` needs to build one `ApplicationAnswer`."""

    source: AnswerSource
    value: str | None
    value_structured: dict[str, Any]
    fact_ids: list[UUID]
    confidence: Decimal | None
    requires_human: bool


def _blank(reason: str) -> DraftedAnswer:
    return DraftedAnswer(
        source=AnswerSource.BLANK,
        value=None,
        value_structured={"reason": reason},
        fact_ids=[],
        confidence=None,
        requires_human=True,
    )


# --------------------------------------------------------------------------
# Classification - pure, no database access. Given only the question's own
# text and declared input type, decide which of the six classes it is.
# --------------------------------------------------------------------------


def classify_question(question_text: str, input_type: str) -> QuestionClass:
    """The classification gate (AGENT_DESIGN.md §4.2).

    Sensitive is checked first and unconditionally: nothing below this line
    can move a question out of `sensitive` once the hard-coded classifier
    has placed it there. Everything else is a best-effort guess at whether
    grounding will succeed; `draft_answer` makes the final call once it has
    tried.
    """
    if is_sensitive(question_text):
        return QuestionClass.SENSITIVE
    if any(pattern.search(question_text) for pattern, _ in _PROFILE_FIELD_PATTERNS):
        return QuestionClass.FACTUAL_SAFE
    if _YEARS_OF_EXPERIENCE.search(question_text):
        return QuestionClass.DERIVED
    if any(pattern.search(question_text) for pattern in _JUDGEMENT_PATTERNS):
        return QuestionClass.JUDGEMENT
    if input_type in ("textarea",):
        # An open essay field with no recognised judgement phrasing is still
        # a judgement call by default (AGENT_DESIGN.md §4.2): free text is
        # never auto-drafted in M9's scope.
        return QuestionClass.JUDGEMENT
    return QuestionClass.UNCERTAIN


# --------------------------------------------------------------------------
# Grounding - touches the database. Given a question already classified,
# find (or fail to find) the evidence that would let it be auto-answered.
# --------------------------------------------------------------------------


def _profile_field_value(profile: Profile, field_name: str) -> str | None:
    value: str | bool | None = getattr(profile, field_name)
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _ground_profile_field(question_text: str, profile: Profile) -> DraftedAnswer | None:
    for pattern, field_name in _PROFILE_FIELD_PATTERNS:
        if not pattern.search(question_text):
            continue
        value = _profile_field_value(profile, field_name)
        if value is None:
            return None
        return DraftedAnswer(
            source=AnswerSource.DERIVED,
            value=value,
            value_structured={"derived_from": f"profile.{field_name}"},
            fact_ids=[],
            confidence=Decimal("1.000"),
            requires_human=False,
        )
    return None


def _years_of_experience(experiences: list[Experience]) -> Decimal | None:
    """Earliest start to latest end (or today), in whole years.

    A deliberately simple span, not a sum of non-overlapping intervals: two
    concurrent roles are not double-counted because this takes the outer
    bound, not the total. Precise enough to be honest, simple enough to
    verify by eye - and named as an approximation in `value_structured`
    rather than presented as exact.
    """
    active = [e for e in experiences if e.deleted_at is None]
    if not active:
        return None
    earliest = min(e.start_date for e in active)
    latest = max(e.end_date or utc_today() for e in active)
    days = (latest - earliest).days
    if days <= 0:
        return None
    return Decimal(days) / Decimal(365)


def _ground_years_of_experience(experiences: list[Experience]) -> DraftedAnswer | None:
    years = _years_of_experience(experiences)
    if years is None:
        return None
    rounded = years.quantize(Decimal("0.1"))
    return DraftedAnswer(
        source=AnswerSource.DERIVED,
        value=str(rounded),
        value_structured={
            "derived_from": "experiences",
            "computation": "earliest_start_to_latest_end_years",
        },
        fact_ids=[],
        confidence=Decimal("1.000"),
        requires_human=False,
    )


def _ground_verified_fact(question_text: str, facts: list[ProfileFact]) -> DraftedAnswer | None:
    """The only path that produces `source = 'agent'`.

    Every candidate fact was already loaded pre-filtered to this exact
    profile, `status = 'verified'` and not soft-deleted (see
    `_load_citable_facts`) - grounding never queries facts on the fly,
    so there is one place, not several, where "verified, active, belongs to
    this candidate" is enforced.
    """
    question_lower = question_text.lower()
    for fact in facts:
        text_lower = fact.text.lower()
        # A crude but explainable overlap: at least one word from the fact's
        # own text, long enough not to match on noise, appears in the
        # question. Good enough for "do you have AWS certification" against
        # a fact reading "AWS Certified Solutions Architect"; deliberately
        # not a fuzzy or embedding match, because every accepted match must
        # be one a human reviewing the citation can immediately see.
        words = [
            w for w in re.findall(r"[a-zA-Z0-9]+", text_lower) if len(w) >= _MIN_KEYWORD_OVERLAP
        ]
        if any(word in question_lower for word in words):
            return DraftedAnswer(
                source=AnswerSource.AGENT,
                value=fact.text,
                value_structured={},
                fact_ids=[fact.id],
                confidence=Decimal("0.900"),
                requires_human=False,
            )
    return None


def draft_answer(
    question_text: str,
    question_class: QuestionClass,
    *,
    profile: Profile,
    experiences: list[Experience],
    facts: list[ProfileFact],
) -> DraftedAnswer:
    """Ground and draft, or return an honest blank. Never guesses.

    `question_class` has already ruled sensitive and judgement questions out
    (`_ALWAYS_BLANK`) before this is reached; what remains is an attempt to
    find real evidence, in a fixed order, and a blank the moment that
    attempt fails rather than a lower-confidence guess.
    """
    if question_class in _ALWAYS_BLANK:
        reason = (
            "sensitive: no auto-answer path exists"
            if question_class is QuestionClass.SENSITIVE
            else "judgement call: requires a human decision"
        )
        return _blank(reason)

    if question_class is QuestionClass.FACTUAL_SAFE:
        drafted = _ground_profile_field(question_text, profile)
        if drafted is not None:
            return drafted
        # The pattern matched a known field, but the profile does not have
        # a value there (e.g. phone is blank). Not a guess - a blank.
        return _blank("recognised field is unset on the profile")

    if question_class is QuestionClass.DERIVED:
        drafted = _ground_years_of_experience(experiences)
        if drafted is not None:
            return drafted
        return _blank("no experience rows to derive a span from")

    # UNCERTAIN and UNANSWERABLE both attempt a fact citation as a last
    # resort - a question the pattern classifier could not place might still
    # be a straightforward "do you have X certification" a human simply
    # phrased differently. A hit here promotes the answer; nothing here can
    # ever demote a SENSITIVE or JUDGEMENT question, because they returned
    # above.
    drafted = _ground_verified_fact(question_text, facts)
    if drafted is not None:
        return drafted
    reason = (
        "no verified fact supports this claim"
        if question_class is QuestionClass.UNANSWERABLE
        else "no confident classification or grounding was found"
    )
    return _blank(reason)


# --------------------------------------------------------------------------
# The employment-type candidate-preference gate.
# --------------------------------------------------------------------------

#: `display_order` reserved for the synthetic gate question, so it always
#: sorts first regardless of how many real ATS questions follow it.
EMPLOYMENT_TYPE_QUESTION_TEXT = (
    "Does this job's employment type match your accepted employment types?"
)


def draft_employment_type_answer(
    *, job_employment_type: str | None, accepted_employment_types: list[str]
) -> DraftedAnswer:
    """One candidate preference, never inferred, never hard-coded globally.

    `accepted_employment_types` is only ever non-empty when a candidate has
    explicitly configured it (`profiles.accepted_employment_types`,
    NULL = unrestricted; callers of this function never call it at all in
    that case - see `_extract_and_draft`). Given that a preference exists:

    * the job's own `employment_type` is in the accepted list -> may advance,
      `requires_human = False`;
    * it is set to something else -> a confident "no", but whether to
      proceed anyway is the candidate's call, not this function's, so it is
      `judgement` and requires a human;
    * it is missing or the ingestion normalizer could not place it (neither
      of those is "not full-time" - it is "unknown") -> `uncertain`,
      requires a human. Never inferred as full-time merely because a
      posting looks like an ordinary salaried role.
    """
    structured = {
        "derived_from": "profile.accepted_employment_types",
        "job_employment_type": job_employment_type,
        "accepted_employment_types": accepted_employment_types,
    }
    if job_employment_type is None:
        return DraftedAnswer(
            source=AnswerSource.DERIVED,
            value=None,
            value_structured=structured,
            fact_ids=[],
            confidence=Decimal("1.000"),
            requires_human=True,
        )
    if job_employment_type in accepted_employment_types:
        return DraftedAnswer(
            source=AnswerSource.DERIVED,
            value="true",
            value_structured=structured,
            fact_ids=[],
            confidence=Decimal("1.000"),
            requires_human=False,
        )
    return DraftedAnswer(
        source=AnswerSource.DERIVED,
        value="false",
        value_structured=structured,
        fact_ids=[],
        confidence=Decimal("1.000"),
        requires_human=True,
    )


def _employment_type_question_class(drafted: DraftedAnswer) -> QuestionClass:
    if drafted.value is None:
        return QuestionClass.UNCERTAIN
    if not drafted.requires_human:
        return QuestionClass.DERIVED
    return QuestionClass.JUDGEMENT


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class ApplicationStateError(ConflictError):
    """The application is not in a state this M9 action allows."""


class QuestionNotFoundError(NotFoundError):
    pass


class AnswerNotFoundError(NotFoundError):
    pass


class SensitiveAnswerRefusedError(ValidationError):
    """An agent-sourced value was attempted for a sensitive/judgement answer.

    Defensive: `chk_sensitive_human_only` and `is_sensitive`'s escalate-only
    design already make this unreachable through `prepare_application`, but
    `edit_answer` accepts a human-supplied `source` override and must refuse
    the same shape a forged request would attempt, not merely rely on the
    database catching it after the fact.
    """


class IncompleteAnswersError(ConflictError):
    """The answers package still has an unresolved, human-required item."""


class NoPendingAnswersApprovalError(ConflictError):
    pass


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


async def _load_application(
    session: AsyncSession, application_id: UUID, *, lock: bool
) -> Application:
    stmt = select(Application).where(Application.id == application_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    application = (await session.execute(stmt)).scalar_one_or_none()
    if application is None:
        raise NotFoundError(f"applications {application_id} not found")
    return application


async def _load_citable_facts(session: AsyncSession, profile_id: UUID) -> list[ProfileFact]:
    """Verified, active facts belonging to this exact profile - and nothing
    else. This is the one place `agent`-sourced grounding is allowed to look;
    a fact for a different profile, a draft fact, or a soft-deleted one is
    never visible to it."""
    return list(
        (
            await session.execute(
                select(ProfileFact).where(
                    ProfileFact.profile_id == profile_id,
                    ProfileFact.status == FactStatus.VERIFIED,
                    ProfileFact.deleted_at.is_(None),
                )
            )
        ).scalars()
    )


async def existing_questions(
    session: AsyncSession, application_id: UUID
) -> list[ApplicationQuestion]:
    return list(
        (
            await session.execute(
                select(ApplicationQuestion)
                .options(selectinload(ApplicationQuestion.answer))
                .where(ApplicationQuestion.application_id == application_id)
                .order_by(ApplicationQuestion.display_order)
            )
        ).scalars()
    )


@dataclass(frozen=True)
class RawQuestion:
    """One caller-supplied question definition.

    M9 does not scrape a live ATS form - there is no site-specific
    automation in this milestone, and AGENT_DESIGN.md §4.1 lists that
    capability as explicitly out of scope everywhere in JobForge. Questions
    arrive as an owner-supplied capture (one of the three sourcing methods
    AGENT_DESIGN.md §4.1 names), matching how M7's structured master-resume
    import is owner-supplied structured data rather than a live scrape.
    """

    question_text: str
    input_type: str
    external_field_id: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    is_required: bool = False
    max_length: int | None = None


ALLOWED_INPUT_TYPES = frozenset(
    {"text", "textarea", "select", "multiselect", "boolean", "file", "date", "number"}
)


@dataclass(frozen=True)
class PrepareRequest:
    application_id: UUID
    event_id: UUID


async def request_application_questions(
    session: AsyncSession,
    *,
    actor: User,
    application_id: UUID,
    questions: list[RawQuestion],
) -> PrepareRequest:
    """Owner-only trigger: `POST /applications/{id}/prepare`.

    Mirrors `request_first_draft`/`resume_review.approve`'s
    `resume.render.requested`: an explicit, owner-initiated event rather than
    inline work in the request handler, so the worker's idempotency/replay
    machinery (`DeliveryRuntime`, `processed_events`) governs execution
    rather than the HTTP request's own lifetime. Refuses if the application
    is not `resume_ready` (the same precondition `prepare_application` itself
    enforces) or if a request has already been made for it.
    """
    if actor.role != "owner":
        raise ForbiddenError("owner role required")
    application = await _load_application(session, application_id, lock=True)
    if application.status is not ApplicationStatus.RESUME_READY:
        raise ApplicationStateError(
            f"application {application_id} must be resume_ready to request "
            f"question preparation, not {application.status}"
        )
    already = await existing_questions(session, application_id)
    if already:
        raise ApplicationStateError(f"application {application_id} already has prepared questions")

    event = await record_event(
        session,
        EventEnvelope(
            type="application.questions.requested",
            aggregate_type="application",
            aggregate_id=application.id,
            correlation_id=application.id,
            actor_type="human",
            actor_id=str(actor.id),
            payload={
                "application_id": str(application.id),
                "questions": [
                    {
                        "question_text": q.question_text,
                        "input_type": q.input_type,
                        "external_field_id": q.external_field_id,
                        "options": q.options,
                        "is_required": q.is_required,
                        "max_length": q.max_length,
                    }
                    for q in questions
                ],
            },
        ),
    )
    log.info(
        "applications.prepare_requested",
        application_id=str(application_id),
        event_id=str(event.id),
        question_count=len(questions),
    )
    return PrepareRequest(application_id=application.id, event_id=event.id)


def questions_from_event_payload(payload: dict[str, Any]) -> list[RawQuestion]:
    """Reconstruct the owner-supplied capture from `application.questions.requested`'s
    payload - the shape `request_application_questions` wrote it in."""
    return [
        RawQuestion(
            question_text=entry["question_text"],
            input_type=entry["input_type"],
            external_field_id=entry.get("external_field_id"),
            options=entry.get("options") or {},
            is_required=bool(entry.get("is_required", False)),
            max_length=entry.get("max_length"),
        )
        for entry in payload.get("questions", [])
    ]


# --------------------------------------------------------------------------
# The pipeline
# --------------------------------------------------------------------------


async def prepare_application(
    session: AsyncSession,
    *,
    application_id: UUID,
    questions: list[RawQuestion],
    causation_id: UUID | None = None,
) -> list[ApplicationQuestion]:
    """Extract, classify, ground, draft, persist, transition, emit.

    Idempotent by state, not by row count: a zero-question preparation is
    legitimate (a job whose external form has no custom fields), and gating
    replay-detection on "do any `ApplicationQuestion` rows exist" would make
    that exact case fail to short-circuit, since there is nothing to find.
    `_ALREADY_PREPARED` names the statuses only reachable by this function
    having already committed - a replay of the event, or of a retried
    worker attempt after a crash, lands here and its existing (possibly
    empty) rows are returned untouched: no second set of questions, no
    second transition, no second event. Everything before a status leaves
    `resume_ready` runs inside the caller's transaction, so a crash midway
    commits nothing: there is no separate "inference" step with its own
    durability need the way a paid LLM call has one, because nothing here
    calls one.

    Re-preparing after a resume rework (a second `resume_ready` episode for
    the same application) is refused rather than silently mixing a new
    round's rows with a prior round's, when the prior round produced any -
    `ApplicationQuestion` has no resume-version binding of its own to tell
    them apart, unlike `resumes.version`. A prior round with zero questions
    carries nothing to conflict with, so that case is unaffected. Versioning
    prepared questions the way resumes are versioned is out of scope here;
    see the M9 completion report.
    """
    application = await _load_application(session, application_id, lock=True)

    if application.status in _ALREADY_PREPARED:
        already = await existing_questions(session, application_id)
        log.info(
            "applications.prepare.idempotent",
            application_id=str(application_id),
            question_count=len(already),
        )
        return already

    if application.status is not ApplicationStatus.RESUME_READY:
        raise ApplicationStateError(
            f"application {application_id} must be resume_ready to prepare "
            f"questions, not {application.status}"
        )

    stale = await existing_questions(session, application_id)
    if stale:
        raise ApplicationStateError(
            f"application {application_id} already has {len(stale)} prepared "
            "question(s) from an earlier resume_ready episode; re-preparing "
            "after a resume rework is not supported"
        )

    profile = await session.get(Profile, application.profile_id)
    if profile is None:
        raise NotFoundError(f"profile for application {application_id} not found")
    job = await session.get(Job, application.job_id)
    if job is None:
        raise NotFoundError(f"job for application {application_id} not found")
    experiences = list(
        (
            await session.execute(select(Experience).where(Experience.profile_id == profile.id))
        ).scalars()
    )
    facts = await _load_citable_facts(session, profile.id)

    # Tracked in parallel rather than read back off `question.answer`: that
    # relationship is not eagerly loaded for objects this function itself
    # just added, and touching it here would mean an unawaited lazy-load in
    # a sync context. `existing_questions` (used both for the replay branch
    # above and to build this function's own return value below) always
    # eager-loads it instead - one path, not two.
    created: list[ApplicationQuestion] = []
    created_answers: list[ApplicationAnswer] = []
    order = 0

    if profile.accepted_employment_types is not None:
        drafted = draft_employment_type_answer(
            job_employment_type=job.employment_type,
            accepted_employment_types=profile.accepted_employment_types,
        )
        question = ApplicationQuestion(
            id=uuid7(),
            application_id=application.id,
            external_field_id=None,
            question_text=EMPLOYMENT_TYPE_QUESTION_TEXT,
            input_type="boolean",
            options={},
            is_required=False,
            display_order=order,
            question_class=_employment_type_question_class(drafted),
            class_reason="employment-type candidate preference gate",
        )
        answer = _answer_row(question.id, application.id, drafted)
        session.add(question)
        session.add(answer)
        created.append(question)
        created_answers.append(answer)
        order += 1

    for raw in questions:
        input_type = raw.input_type if raw.input_type in ALLOWED_INPUT_TYPES else "text"
        question_class = classify_question(raw.question_text, input_type)
        drafted = draft_answer(
            raw.question_text,
            question_class,
            profile=profile,
            experiences=experiences,
            facts=facts,
        )
        question = ApplicationQuestion(
            id=uuid7(),
            application_id=application.id,
            external_field_id=raw.external_field_id,
            question_text=raw.question_text,
            input_type=input_type,
            options=raw.options,
            is_required=raw.is_required,
            max_length=raw.max_length,
            display_order=order,
            question_class=question_class,
            class_reason=None,
        )
        answer = _answer_row(question.id, application.id, drafted)
        session.add(question)
        session.add(answer)
        created.append(question)
        created_answers.append(answer)
        order += 1

    await session.flush()

    await transition_application(
        session,
        application,
        ApplicationStatus.QUESTIONS_PENDING,
        actor_type="agent",
        actor_id="application_agent",
        reason="questions extracted and drafted",
        correlation_id=application.id,
        causation_id=causation_id,
    )

    by_class: dict[str, int] = dict.fromkeys((str(c) for c in QuestionClass), 0)
    for question in created:
        by_class[str(question.question_class)] += 1
    await emit(
        session,
        type="application.questions.extracted",
        aggregate_type="application",
        aggregate_id=application.id,
        correlation_id=application.id,
        causation_id=causation_id,
        actor_type="agent",
        actor_id="application_agent",
        payload={
            "application_id": str(application.id),
            "total": len(created),
            "by_class": by_class,
        },
    )

    auto_answered = sum(1 for a in created_answers if not a.requires_human)
    human_required = sum(1 for a in created_answers if a.requires_human and a.value is not None)
    blanks = sum(1 for a in created_answers if a.value is None)
    await emit(
        session,
        type="application.answers.drafted",
        aggregate_type="application",
        aggregate_id=application.id,
        correlation_id=application.id,
        causation_id=causation_id,
        actor_type="agent",
        actor_id="application_agent",
        payload={
            "application_id": str(application.id),
            "auto_answered": auto_answered,
            "human_required": human_required,
            "blanks": blanks,
        },
    )

    log.info(
        "applications.prepared",
        application_id=str(application_id),
        question_count=len(created),
        by_class=by_class,
    )
    # Re-fetched rather than returning `created` directly: this makes the
    # return value's `.answer` eager-loaded and safe to access regardless of
    # which branch produced it, matching the replay branch above exactly.
    return await existing_questions(session, application_id)


def _answer_row(
    question_id: UUID, application_id: UUID, drafted: DraftedAnswer
) -> ApplicationAnswer:
    return ApplicationAnswer(
        id=uuid7(),
        question_id=question_id,
        application_id=application_id,
        value=drafted.value,
        value_structured=drafted.value_structured,
        source=drafted.source,
        fact_ids=drafted.fact_ids,
        confidence=drafted.confidence,
        requires_human=drafted.requires_human,
        answered_by=None,
    )


# --------------------------------------------------------------------------
# Human answer editing
# --------------------------------------------------------------------------


async def edit_answer(
    session: AsyncSession,
    *,
    application_id: UUID,
    answer_id: UUID,
    actor: User,
    value: str | None,
    value_structured: dict[str, Any] | None = None,
) -> ApplicationAnswer:
    """A human sets or overwrites one answer.

    Always `source = 'human'`, always `answered_by = actor.id` - there is no
    parameter that lets a caller claim any other source, which is what makes
    this the one path `chk_sensitive_human_only` and the sensitive
    classifier's escalate-only rule cannot be bypassed through. The agent's
    original proposal, if there was one, is preserved in
    `edited_from_agent_value` rather than overwritten silently.
    """
    answer = (
        await session.execute(
            select(ApplicationAnswer).where(
                ApplicationAnswer.id == answer_id,
                ApplicationAnswer.application_id == application_id,
            )
        )
    ).scalar_one_or_none()
    if answer is None:
        raise AnswerNotFoundError(f"answer {answer_id} not found")

    application = await _load_application(session, application_id, lock=False)
    if application.status is not ApplicationStatus.AWAITING_APPROVAL:
        raise ApplicationStateError(
            f"answers may only be edited while awaiting_approval, not " f"{application.status}"
        )

    if answer.source != AnswerSource.HUMAN and answer.value is not None:
        answer.edited_from_agent_value = answer.value
    answer.value = value
    if value_structured is not None:
        answer.value_structured = value_structured
    answer.source = AnswerSource.HUMAN
    answer.fact_ids = []
    answer.requires_human = value is None
    answer.answered_by = actor.id
    await session.flush()
    log.info(
        "applications.answer_edited",
        application_id=str(application_id),
        answer_id=str(answer_id),
        actor_id=str(actor.id),
    )
    return answer


# --------------------------------------------------------------------------
# The answers approval - a separate, independently decided ApprovalKind.ANSWERS
# record. Does not replace or gate the existing resume approval: the
# `jobforge_require_real_approval` trigger accepts any approved kind, so
# `resume_review.approve()` remains exactly as M7/M8 left it.
# --------------------------------------------------------------------------


async def revalidate_cited_facts(
    session: AsyncSession, questions: list[ApplicationQuestion]
) -> None:
    """Re-check every `agent`-sourced citation immediately before the
    approval snapshot is built - the same discipline `render_resume`'s
    second `_revalidate` applies to a resume's approval, immediately before
    persistence rather than trusting the state at draft time."""
    fact_ids: set[UUID] = set()
    for question in questions:
        if question.answer is not None:
            fact_ids.update(question.answer.fact_ids)
    if not fact_ids:
        return
    verified = set(
        (
            await session.execute(
                select(ProfileFact.id).where(
                    ProfileFact.id.in_(fact_ids),
                    ProfileFact.status == FactStatus.VERIFIED,
                    ProfileFact.deleted_at.is_(None),
                )
            )
        ).scalars()
    )
    stale = fact_ids - verified
    if stale:
        raise ApplicationStateError(
            "the answers package cites facts that are no longer verified: "
            f"{sorted(str(f) for f in stale)}"
        )


def _build_answers_snapshot(
    application: Application, questions: list[ApplicationQuestion]
) -> dict[str, Any]:
    """Exactly what the reviewer was shown - full content, not identifiers
    only. Unlike a resume (whose text lives in an immutable-once-versioned
    row), an `application_answers` row can still be edited after this
    snapshot is taken, so the snapshot has to carry the content itself to
    remain a durable record of what was actually decided on."""
    return {
        "application_id": str(application.id),
        "questions": [
            {
                "question_id": str(q.id),
                "question_text": q.question_text,
                "input_type": q.input_type,
                "question_class": str(q.question_class),
                "answer_id": str(q.answer.id) if q.answer else None,
                "value": q.answer.value if q.answer else None,
                "value_structured": q.answer.value_structured if q.answer else {},
                "source": str(q.answer.source) if q.answer else None,
                "fact_ids": [str(f) for f in q.answer.fact_ids] if q.answer else [],
                "confidence": (
                    str(q.answer.confidence)
                    if q.answer and q.answer.confidence is not None
                    else None
                ),
                "requires_human": q.answer.requires_human if q.answer else True,
                "answered_by": (
                    str(q.answer.answered_by) if q.answer and q.answer.answered_by else None
                ),
            }
            for q in questions
        ],
    }


def answers_snapshot_matches(
    snapshot: dict[str, Any], application: Application, questions: list[ApplicationQuestion]
) -> bool:
    """Does this approval still describe the *current* answers content?

    M10 Phase 2's readiness gate needs this the same way
    `resume_review.snapshot_matches`/`cover_letters.cover_letter_snapshot_matches`
    let it check a resume or cover-letter approval against the current
    version - except an answers approval has no version to compare, because
    `edit_answer` can change an `application_answers` row after its approval
    was granted (unlike a resume or cover-letter row, which is immutable once
    created). So staleness here means exact content equality against a fresh
    snapshot, not an id/version match.
    """
    return snapshot == _build_answers_snapshot(application, questions)


def _unresolved(questions: list[ApplicationQuestion]) -> list[ApplicationQuestion]:
    return [
        q
        for q in questions
        if q.answer is not None and q.answer.requires_human and q.answer.value is None
    ]


async def _decide_answers(
    session: AsyncSession,
    *,
    application_id: UUID,
    actor: User,
    status: ApprovalStatus,
    decision_note: str | None,
    changes_requested: str | None,
) -> Approval:
    application = await _load_application(session, application_id, lock=True)
    if application.status is not ApplicationStatus.AWAITING_APPROVAL:
        raise ApplicationStateError(
            f"the answers package can only be decided while awaiting_approval, "
            f"not {application.status}"
        )
    questions = await existing_questions(session, application_id)
    if not questions:
        raise NoPendingAnswersApprovalError(
            f"application {application_id} has no prepared questions to decide"
        )

    if status is ApprovalStatus.APPROVED:
        unresolved = _unresolved(questions)
        if unresolved:
            raise IncompleteAnswersError(
                f"{len(unresolved)} question(s) still require a human answer "
                f"before the package may be approved"
            )
        await revalidate_cited_facts(session, questions)

    decided_at = utc_now()
    approval = Approval(
        id=uuid7(),
        application_id=application.id,
        kind=ApprovalKind.ANSWERS,
        status=status,
        subject_snapshot=_build_answers_snapshot(application, questions),
        decision_note=decision_note,
        changes_requested=changes_requested,
        requested_at=decided_at,
        decided_at=decided_at,
        decided_by=actor.id,
    )
    session.add(approval)
    await session.flush()

    await emit(
        session,
        type="approval.requested",
        aggregate_type="approval",
        aggregate_id=approval.id,
        correlation_id=application.id,
        actor_type="human",
        actor_id=str(actor.id),
        payload={
            "approval_id": str(approval.id),
            "application_id": str(application.id),
            "kind": str(ApprovalKind.ANSWERS),
            "question_count": len(questions),
            "requested_at": approval.requested_at.isoformat(),
        },
    )
    event_type = (
        "approval.granted" if status is ApprovalStatus.APPROVED else "approval.changes_requested"
    )
    payload: dict[str, Any] = {
        "approval_id": str(approval.id),
        "application_id": str(application.id),
        "kind": str(ApprovalKind.ANSWERS),
        "decided_by": str(actor.id),
    }
    if status is ApprovalStatus.APPROVED and decision_note:
        payload["decision_note"] = decision_note
    if status is ApprovalStatus.CHANGES_REQUESTED:
        payload["instructions"] = changes_requested
    decided_event = await emit(
        session,
        type=event_type,
        aggregate_type="approval",
        aggregate_id=approval.id,
        correlation_id=application.id,
        actor_type="human",
        actor_id=str(actor.id),
        payload=payload,
    )

    if status is ApprovalStatus.APPROVED:
        # M10 Phase 2. The completion gate: awaiting_approval -> approved
        # only if RESUME and COVER_LETTER are also current-approved - this
        # decision alone is never sufficient. Local import: review_completion
        # imports back from this module for answers_snapshot_matches/
        # existing_questions.
        from packages.core.services.review_completion import (
            maybe_mark_application_approved,
        )

        await maybe_mark_application_approved(
            session, application, actor=actor, causation_id=decided_event.id
        )

    log.info(
        "applications.answers_decided",
        application_id=str(application_id),
        approval_id=str(approval.id),
        status=str(status),
    )
    return approval


async def approve_answers(
    session: AsyncSession,
    *,
    application_id: UUID,
    actor: User,
    decision_note: str | None = None,
) -> Approval:
    """Create and decide a granted `ApprovalKind.ANSWERS` row in one call.

    Unlike the resume approval, this needs no separate "open" step: there is
    no regeneration pipeline waiting on a pending row, so open-and-decide
    happen together, atomically, the moment a human is ready to approve.
    Refuses if any human-required answer is still unresolved
    (`IncompleteAnswersError`) or if a cited fact is no longer verified
    (`revalidate_cited_facts`).
    """
    return await _decide_answers(
        session,
        application_id=application_id,
        actor=actor,
        status=ApprovalStatus.APPROVED,
        decision_note=decision_note,
        changes_requested=None,
    )


async def request_changes_answers(
    session: AsyncSession, *, application_id: UUID, actor: User, note: str
) -> Approval:
    """Create and decide a `changes_requested` `ApprovalKind.ANSWERS` row.

    Records the decision only - it does not move `applications.status`. A
    resume "changes requested" must restart generation (a new draft needs a
    fresh verification pass), but an answers "changes requested" means
    "edit the flagged answers directly" (`edit_answer`), which needs no
    pipeline restart and leaves the application in `awaiting_approval`
    throughout.
    """
    instructions = (note or "").strip()
    if not instructions:
        raise ValidationError("requesting changes needs a note saying what should change")
    return await _decide_answers(
        session,
        application_id=application_id,
        actor=actor,
        status=ApprovalStatus.CHANGES_REQUESTED,
        decision_note=instructions,
        changes_requested=instructions,
    )
