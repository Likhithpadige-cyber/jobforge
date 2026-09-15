"""Database enum types.

Mirrors DATABASE_DESIGN.md §3 exactly. These values are also the canonical
state values in ARCHITECTURE.md §7 - if the two ever disagree, that is a bug,
not a choice.

Each enum appears twice:

* a Python ``StrEnum`` used everywhere in application code, and
* a SQLAlchemy ``ENUM`` bound to the same PostgreSQL type name, declared with
  ``create_type=False`` so that the Alembic migration owns type creation and
  ordering (types must exist before the tables that use them).
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy.dialects.postgresql import ENUM


class JobStatus(StrEnum):
    DISCOVERED = "discovered"
    NORMALIZED = "normalized"
    EVALUATED = "evaluated"
    SHORTLISTED = "shortlisted"
    QUEUED = "queued"
    CLAIMED = "claimed"
    APPLIED = "applied"
    DISCARDED = "discarded"
    DUPLICATE = "duplicate"
    EXPIRED = "expired"


class ApplicationStatus(StrEnum):
    CREATED = "created"
    RESUME_GENERATING = "resume_generating"
    RESUME_READY = "resume_ready"
    VERIFICATION_FAILED = "verification_failed"
    QUESTIONS_PENDING = "questions_pending"
    AWAITING_APPROVAL = "awaiting_approval"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    SUBMISSION_READY = "submission_ready"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    SUBMISSION_FAILED = "submission_failed"
    MANUAL_HANDOFF = "manual_handoff"
    POLICY_BLOCKED = "policy_blocked"
    ABANDONED = "abandoned"
    ERROR = "error"


#: Statuses that require a recorded human approver.
#: Mirrors the ``chk_approved_requires_human`` CHECK in DATABASE_DESIGN.md §6.1.
HUMAN_APPROVAL_REQUIRED_STATUSES: frozenset[ApplicationStatus] = frozenset(
    {
        ApplicationStatus.APPROVED,
        ApplicationStatus.SUBMISSION_READY,
        ApplicationStatus.SUBMITTING,
        ApplicationStatus.SUBMITTED,
        ApplicationStatus.MANUAL_HANDOFF,
    }
)


class ApplicationOutcome(StrEnum):
    PENDING = "pending"
    ACKNOWLEDGED = "acknowledged"
    SCREENING = "screening"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NO_RESPONSE = "no_response"
    WITHDRAWN = "withdrawn"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"
    SUPERSEDED = "superseded"


class ApprovalKind(StrEnum):
    APPLICATION = "application"
    RESUME = "resume"
    ANSWERS = "answers"
    COVER_LETTER = "cover_letter"
    POLICY_EXCEPTION = "policy_exception"


class FactType(StrEnum):
    EXPERIENCE = "experience"
    EDUCATION = "education"
    CERTIFICATION = "certification"
    SKILL = "skill"
    PROJECT = "project"
    ACHIEVEMENT = "achievement"
    TOOL = "tool"
    CONTACT = "contact"
    PREFERENCE = "preference"
    AUTHORIZATION = "authorization"


class FactStatus(StrEnum):
    DRAFT = "draft"
    VERIFIED = "verified"
    RETIRED = "retired"


class ClaimVerdict(StrEnum):
    SUPPORTED = "supported"
    OVERREACH = "overreach"
    UNSUPPORTED = "unsupported"
    NOT_CHECKED = "not_checked"


class QuestionClass(StrEnum):
    FACTUAL_SAFE = "factual_safe"
    DERIVED = "derived"
    UNCERTAIN = "uncertain"
    JUDGEMENT = "judgement"
    SENSITIVE = "sensitive"
    UNANSWERABLE = "unanswerable"


#: Classes that may never carry an agent-authored answer (AGENT_DESIGN.md §4.2).
HUMAN_ONLY_QUESTION_CLASSES: frozenset[QuestionClass] = frozenset(
    {
        QuestionClass.UNCERTAIN,
        QuestionClass.JUDGEMENT,
        QuestionClass.SENSITIVE,
        QuestionClass.UNANSWERABLE,
    }
)


class AnswerSource(StrEnum):
    AGENT = "agent"
    HUMAN = "human"
    DERIVED = "derived"
    BLANK = "blank"


class SubmissionMethod(StrEnum):
    OFFICIAL_API = "official_api"
    EMAIL = "email"
    HUMAN_HANDOFF = "human_handoff"
    BLOCKED = "blocked"


class SubmissionResult(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    HANDOFF_REQUIRED = "handoff_required"
    BLOCKED_BY_POLICY = "blocked_by_policy"


class IngestionMethod(StrEnum):
    OFFICIAL_API = "official_api"
    PUBLIC_FEED = "public_feed"
    MANUAL_IMPORT = "manual_import"
    EXTENSION_CAPTURE = "extension_capture"
    BLOCKED = "blocked"


class Recommendation(StrEnum):
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"
    NO = "no"


class AgentName(StrEnum):
    RESUME_OPTIMIZER = "resume_optimizer"
    JOB_APPLICATION = "job_application"
    MONITORING = "monitoring"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABORTED = "aborted"


class AlertSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class RevisionAttemptStatus(StrEnum):
    """C3 revision attempt lifecycle (C3_HARDENING_DESIGN.md C.1).

    About the JOB, never about the content: a B5 negative verdict is a
    SUCCEEDED attempt carrying `RevisionOutcome.FAILED_VERIFICATION`.
    """

    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABANDONED = "abandoned"


class RevisionOutcome(StrEnum):
    """About the CONTENT. Only ever set on a succeeded attempt."""

    PASSED = "passed"
    FAILED_VERIFICATION = "failed_verification"


class RevisionFailureClass(StrEnum):
    """Why a revision attempt failed technically. Never a semantic verdict."""

    MODEL_TIMEOUT = "model_timeout"
    MODEL_OUTPUT_INVALID = "model_output_invalid"
    BUDGET_EXCEEDED = "budget_exceeded"
    EVIDENCE_STALE = "evidence_stale"
    EVIDENCE_LOCKED = "evidence_locked"
    STATE_CONFLICT = "state_conflict"
    PERSISTENCE_FAILED = "persistence_failed"
    LEASE_LOST = "lease_lost"
    WORKER_LOST = "worker_lost"


def _pg(name: str, enum_cls: type[StrEnum]) -> ENUM:
    """Bind a Python enum to an existing PostgreSQL type.

    ``create_type=False``: the migration creates every type up front, in one
    place, so ordering is explicit and repeatable.
    """
    return ENUM(
        enum_cls,
        name=name,
        create_type=False,
        values_callable=lambda cls: [member.value for member in cls],
        native_enum=True,
    )


job_status_enum = _pg("job_status", JobStatus)
application_status_enum = _pg("application_status", ApplicationStatus)
application_outcome_enum = _pg("application_outcome", ApplicationOutcome)
approval_status_enum = _pg("approval_status", ApprovalStatus)
approval_kind_enum = _pg("approval_kind", ApprovalKind)
fact_type_enum = _pg("fact_type", FactType)
fact_status_enum = _pg("fact_status", FactStatus)
claim_verdict_enum = _pg("claim_verdict", ClaimVerdict)
question_class_enum = _pg("question_class", QuestionClass)
answer_source_enum = _pg("answer_source", AnswerSource)
submission_method_enum = _pg("submission_method", SubmissionMethod)
submission_result_enum = _pg("submission_result", SubmissionResult)
ingestion_method_enum = _pg("ingestion_method", IngestionMethod)
recommendation_enum = _pg("recommendation", Recommendation)
agent_name_enum = _pg("agent_name", AgentName)
run_status_enum = _pg("run_status", RunStatus)
alert_severity_enum = _pg("alert_severity", AlertSeverity)
revision_attempt_status_enum = _pg("revision_attempt_status", RevisionAttemptStatus)
revision_outcome_enum = _pg("revision_outcome", RevisionOutcome)
revision_failure_class_enum = _pg("revision_failure_class", RevisionFailureClass)


#: Every PostgreSQL enum type name and its ordered values, used by the
#: migration and by the schema-conformance checker.
ENUM_DEFINITIONS: dict[str, list[str]] = {
    "job_status": [m.value for m in JobStatus],
    "application_status": [m.value for m in ApplicationStatus],
    "application_outcome": [m.value for m in ApplicationOutcome],
    "approval_status": [m.value for m in ApprovalStatus],
    "approval_kind": [m.value for m in ApprovalKind],
    "fact_type": [m.value for m in FactType],
    "fact_status": [m.value for m in FactStatus],
    "claim_verdict": [m.value for m in ClaimVerdict],
    "question_class": [m.value for m in QuestionClass],
    "answer_source": [m.value for m in AnswerSource],
    "submission_method": [m.value for m in SubmissionMethod],
    "submission_result": [m.value for m in SubmissionResult],
    "ingestion_method": [m.value for m in IngestionMethod],
    "recommendation": [m.value for m in Recommendation],
    "agent_name": [m.value for m in AgentName],
    "run_status": [m.value for m in RunStatus],
    "alert_severity": [m.value for m in AlertSeverity],
    "revision_attempt_status": [m.value for m in RevisionAttemptStatus],
    "revision_outcome": [m.value for m in RevisionOutcome],
    "revision_failure_class": [m.value for m in RevisionFailureClass],
}
