"""M19 application tracking: outcome, applied date, follow-up date, notes.

Orthogonal to the pipeline status (ARCHITECTURE.md §7.3): recording that
an application was sent, or that an interview was offered, never moves
`applications.status`, never touches `submitted_at` (which only the approved
submission pipeline writes) and never sends anything. Everything here is a
human record, owner-only to write. An outcome change emits
`application.outcome.updated` (source "human"); every column change is also
captured by the `trg_applications_audit` trigger.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, timedelta
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from sqlalchemy import ColumnElement, not_, or_, select

from packages.core.events import EventEnvelope, record_event
from packages.core.models import Application, ApplicationOutcome, ApplicationStatus, Company, Job
from packages.shared.clock import utc_now, utc_today
from packages.shared.errors import ForbiddenError, NotFoundError, ValidationError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from packages.core.models import User

#: Outcomes after which no follow-up is due.
CLOSED_OUTCOMES: frozenset[ApplicationOutcome] = frozenset(
    {ApplicationOutcome.ACCEPTED, ApplicationOutcome.REJECTED, ApplicationOutcome.WITHDRAWN}
)
MAX_NOTES = 4000
EARLIEST = date(2000, 1, 1)
#: One day of slack so an owner ahead of UTC can record "today".
APPLIED_SLACK = timedelta(days=1)
MAX_FOLLOW_UP_AHEAD = timedelta(days=730)
_MISSING = "application not found"

#: The fields a caller may change. Absent means unchanged; None clears.
TRACKED_FIELDS = ("outcome", "applied_on", "follow_up_on", "notes")

AppliedVia = Literal["jobforge", "manual"]


def applied_via(application: Application) -> AppliedVia | None:
    """Who sent it: JobForge's approved submission pipeline (`submitted_at`
    is set), or the owner by hand (only an owner-recorded `applied_on`)."""
    if application.submitted_at is not None:
        return "jobforge"
    if application.applied_on is not None:
        return "manual"
    return None


def effective_applied_on(application: Application) -> date | None:
    """The owner's date if recorded, else the pipeline's submission date."""
    if application.applied_on is not None:
        return application.applied_on
    submitted = application.submitted_at
    return submitted.astimezone(UTC).date() if submitted is not None else None


def applied_clause(applied: bool) -> ColumnElement[bool]:
    """SQL form of "has been applied to" for list filters."""
    sent = or_(Application.applied_on.is_not(None), Application.submitted_at.is_not(None))
    return sent if applied else not_(sent)


@dataclass(frozen=True)
class FollowUp:
    application_id: UUID
    job_title: str
    company_name: str | None
    outcome: ApplicationOutcome
    applied_on: date | None
    applied_via: AppliedVia | None
    follow_up_on: date
    days_overdue: int


def _clean_notes(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if len(text) > MAX_NOTES:
        raise ValidationError(f"notes must be at most {MAX_NOTES} characters")
    return text or None


def _validate(values: dict[str, Any], today: date) -> None:
    applied, follow_up, outcome = values["applied_on"], values["follow_up_on"], values["outcome"]
    if applied is not None and not EARLIEST <= applied <= today + APPLIED_SLACK:
        raise ValidationError("the applied date cannot be in the future")
    if follow_up is not None and not EARLIEST <= follow_up <= today + MAX_FOLLOW_UP_AHEAD:
        raise ValidationError("the follow-up date must be within the next two years")
    if outcome is not ApplicationOutcome.PENDING and applied is None:
        raise ValidationError(f"record the date you applied before marking it {outcome.value}")


async def update_tracking(
    session: AsyncSession,
    *,
    actor: User,
    application_id: UUID,
    changes: dict[str, Any],
) -> Application:
    """Apply the given subset of TRACKED_FIELDS under a row lock."""
    if actor.role != "owner":
        raise ForbiddenError("owner role required")
    unknown = set(changes) - set(TRACKED_FIELDS)
    if unknown:
        raise ValidationError(f"unknown tracking fields: {sorted(unknown)}")
    application = (
        await session.execute(
            select(Application)
            .where(Application.id == application_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if application is None:
        raise NotFoundError(_MISSING)

    # Build and check the result first: a refused change must leave the
    # loaded row untouched, or the session would autoflush it later.
    if "outcome" in changes and changes["outcome"] is None:
        raise ValidationError("outcome cannot be cleared; use pending")
    values: dict[str, Any] = {name: getattr(application, name) for name in TRACKED_FIELDS}
    values.update(changes)
    values["outcome"] = ApplicationOutcome(values["outcome"])
    values["notes"] = _clean_notes(values["notes"]) if "notes" in changes else application.notes
    # A JobForge-submitted application was applied to on its submission
    # date; recording an outcome for it should not require retyping that.
    if (
        values["outcome"] is not ApplicationOutcome.PENDING
        and values["applied_on"] is None
        and "applied_on" not in changes
        and application.submitted_at is not None
    ):
        values["applied_on"] = effective_applied_on(application)
    _validate(values, utc_today())

    before = application.outcome
    for name, value in values.items():
        setattr(application, name, value)
    if application.outcome is not before:
        application.outcome_updated_at = utc_now()
        await record_event(
            session,
            EventEnvelope(
                type="application.outcome.updated",
                aggregate_type="application",
                aggregate_id=application.id,
                correlation_id=application.id,
                actor_type="human",
                actor_id=str(actor.id),
                payload={
                    "application_id": str(application.id),
                    "from_outcome": before.value,
                    "to_outcome": application.outcome.value,
                    "source": "human",
                },
            ),
        )
    await session.flush()
    return application


async def due_follow_ups(
    session: AsyncSession, *, on: date, within_days: int = 0, limit: int = 100
) -> list[FollowUp]:
    """Open applications whose follow-up date is on or before `on +
    within_days`, earliest first. Closed outcomes and abandoned applications
    are never due."""
    horizon = on + timedelta(days=within_days)
    rows = (
        await session.execute(
            select(Application, Job.title, Company.name)
            .join(Job, Job.id == Application.job_id)
            .outerjoin(Company, Company.id == Job.company_id)
            .where(
                Application.follow_up_on.is_not(None),
                Application.follow_up_on <= horizon,
                Application.outcome.not_in(CLOSED_OUTCOMES),
                Application.status != ApplicationStatus.ABANDONED,
            )
            .order_by(Application.follow_up_on, Application.id)
            .limit(limit)
        )
    ).all()
    out: list[FollowUp] = []
    for application, title, company in rows:
        follow_up = application.follow_up_on
        assert follow_up is not None  # filtered above
        out.append(
            FollowUp(
                application_id=application.id,
                job_title=title,
                company_name=company,
                outcome=application.outcome,
                applied_on=effective_applied_on(application),
                applied_via=applied_via(application),
                follow_up_on=follow_up,
                days_overdue=(on - follow_up).days,
            )
        )
    return out
