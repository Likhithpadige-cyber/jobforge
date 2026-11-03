"""The ToS / legality gate (SECURITY.md §7.2, DATABASE_DESIGN.md §5.2).

Every external interaction consults this module first. The central rule:

    the absence of a policy row means BLOCKED

An unknown domain returns a synthetic blocked policy rather than raising, so
there is no exception path that accidentally fails open. That distinction is
the whole point - a lookup that raises invites a `try/except: continue`
somewhere downstream, and then the gate is gone.

The decision function is pure; the lookup that feeds it is the only part
that touches the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from urllib.parse import urlsplit

from packages.core.models import IngestionMethod, SubmissionMethod
from packages.shared.clock import utc_today


class PolicyDecision(StrEnum):
    ALLOWED = "allowed"
    BLOCKED = "blocked"
    REQUIRES_HUMAN = "requires_human"


class BlockReason(StrEnum):
    NO_POLICY = "no_policy_for_domain"
    AUTOMATION_FORBIDDEN = "automation_not_permitted"
    METHOD_BLOCKED = "ingestion_method_blocked"
    REVIEW_EXPIRED = "policy_review_expired"
    LOGIN_REQUIRED = "site_requires_login"
    SOURCE_DISABLED = "source_disabled"


@dataclass(frozen=True)
class PolicySnapshot:
    """The fields of a site_policies row that a decision depends on."""

    domain: str
    automation_allowed: bool = False
    ingestion_method: IngestionMethod = IngestionMethod.BLOCKED
    submission_method: SubmissionMethod = SubmissionMethod.HUMAN_HANDOFF
    max_requests_per_hour: int = 10
    requires_login: bool = True
    reviewed_at: datetime | None = None
    next_review_due: date | None = None
    exists: bool = False


@dataclass(frozen=True)
class PolicyVerdict:
    decision: PolicyDecision
    reason: BlockReason | None
    detail: str
    policy: PolicySnapshot

    @property
    def permitted(self) -> bool:
        return self.decision is PolicyDecision.ALLOWED


#: Methods that involve the system making its own requests to a site. Manual
#: import and extension capture do not: a human is already looking at the page.
AUTOMATED_METHODS = frozenset({IngestionMethod.OFFICIAL_API, IngestionMethod.PUBLIC_FEED})


def extract_domain(url: str) -> str:
    """Registrable host, lowercased, without `www.`."""
    parsed = urlsplit(url.strip())
    netloc = parsed.netloc.lower()
    if not netloc and parsed.scheme == "mailto":
        path = parsed.path.split("?", 1)[0]
        if "@" in path:
            netloc = path.rsplit("@", 1)[1].lower()
    elif not netloc and "@" in url:
        part = url.strip().split("?", 1)[0]
        netloc = part.rsplit("@", 1)[1].lower()
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    netloc = netloc.split(":")[0]
    return netloc[4:] if netloc.startswith("www.") else netloc


def extract_email(url: str) -> str | None:
    """Extract email destination from a mailto: URL or raw email, if present."""
    clean = url.strip()
    if clean.lower().startswith("mailto:"):
        addr = clean[7:].split("?", 1)[0].strip()
        return addr if "@" in addr else None
    return None


def blocked_policy(domain: str) -> PolicySnapshot:
    """The synthetic policy returned for a domain that has no row.

    Every field is the most restrictive value. This is what "fail closed"
    means concretely.
    """
    return PolicySnapshot(domain=domain, exists=False)


def evaluate_ingestion(
    policy: PolicySnapshot,
    requested_method: IngestionMethod,
    *,
    source_enabled: bool = True,
    today: date | None = None,
) -> PolicyVerdict:
    """Decide whether an ingestion may proceed. Pure."""
    today = today or utc_today()

    if not policy.exists:
        return PolicyVerdict(
            PolicyDecision.BLOCKED,
            BlockReason.NO_POLICY,
            f"no site_policies row for {policy.domain!r}; unknown domains are blocked",
            policy,
        )

    if not source_enabled:
        return PolicyVerdict(
            PolicyDecision.BLOCKED,
            BlockReason.SOURCE_DISABLED,
            "source is disabled; enable it only after a ToS review",
            policy,
        )

    if policy.ingestion_method is IngestionMethod.BLOCKED:
        return PolicyVerdict(
            PolicyDecision.BLOCKED,
            BlockReason.METHOD_BLOCKED,
            f"ingestion is blocked for {policy.domain}",
            policy,
        )

    if requested_method != policy.ingestion_method:
        return PolicyVerdict(
            PolicyDecision.BLOCKED,
            BlockReason.METHOD_BLOCKED,
            f"{policy.domain} permits {policy.ingestion_method}, not {requested_method}",
            policy,
        )

    # A human-driven import needs no automation permission: the person is
    # already looking at the page in their own browser.
    if requested_method in AUTOMATED_METHODS:
        if not policy.automation_allowed:
            return PolicyVerdict(
                PolicyDecision.BLOCKED,
                BlockReason.AUTOMATION_FORBIDDEN,
                f"{policy.domain} does not permit automated access",
                policy,
            )
        if policy.requires_login:
            return PolicyVerdict(
                PolicyDecision.BLOCKED,
                BlockReason.LOGIN_REQUIRED,
                f"{policy.domain} requires authentication; automated login is never used",
                policy,
            )
        if policy.next_review_due and policy.next_review_due < today:
            # Terms change. A stale review is not a review.
            return PolicyVerdict(
                PolicyDecision.BLOCKED,
                BlockReason.REVIEW_EXPIRED,
                f"ToS review for {policy.domain} expired on {policy.next_review_due}",
                policy,
            )

    return PolicyVerdict(
        PolicyDecision.ALLOWED,
        None,
        f"{requested_method} permitted for {policy.domain}",
        policy,
    )


def evaluate_submission(policy: PolicySnapshot, *, today: date | None = None) -> PolicyVerdict:
    """Decide how an application may be submitted to this domain.

    Never returns ALLOWED for anything other than an explicitly permitted
    channel. `human_handoff` is REQUIRES_HUMAN, not ALLOWED, so a caller that
    checks `.permitted` can never auto-submit by accident.

    M11 Phase 1 fix: an expired ToS review must not authorise an automated
    send (`EMAIL` or `OFFICIAL_API`) any more than a current one that simply
    forbids automation does. SECURITY.md §7.2 is explicit about the intended
    outcome - "a policy older than its review date... reverts the domain to
    `human_handoff` submission" - so this mirrors `evaluate_ingestion`'s own
    `next_review_due` check in spirit (a stale review is not a review) but
    not in its exact outcome: ingestion has no "do it by hand instead" path,
    so a stale ingestion review is BLOCKED outright, while submission always
    has human handoff as a safe fallback, so a stale submission review
    degrades to REQUIRES_HUMAN - identical to the explicit `human_handoff`
    branch below - rather than to BLOCKED. Either way nothing here is ever
    reported ALLOWED on a stale review; "fail safely" and "match the
    documented behaviour" agree in this case.

    `EMAIL` deliberately has no `automation_allowed` check, unlike
    `OFFICIAL_API` (locked M11 design decision 4): sending an email to a
    published application address is not the kind of automated site
    interaction that flag was built to gate. `EMAIL` still has to name the
    method explicitly (nothing here defaults an unset/other method to email)
    and, like every other automated method, still has to pass the review-
    expiry check above.
    """
    today = today or utc_today()
    if not policy.exists:
        return PolicyVerdict(
            PolicyDecision.BLOCKED,
            BlockReason.NO_POLICY,
            f"no policy for {policy.domain!r}; submission blocked",
            policy,
        )
    if policy.submission_method is SubmissionMethod.BLOCKED:
        return PolicyVerdict(
            PolicyDecision.BLOCKED,
            BlockReason.METHOD_BLOCKED,
            f"submission is blocked for {policy.domain}",
            policy,
        )
    if policy.submission_method is SubmissionMethod.HUMAN_HANDOFF:
        return PolicyVerdict(
            PolicyDecision.REQUIRES_HUMAN,
            None,
            f"{policy.domain} requires a human to submit",
            policy,
        )
    # From here the method is EMAIL or OFFICIAL_API - an automated external
    # action - so a stale review can no longer authorise it.
    if policy.next_review_due and policy.next_review_due < today:
        return PolicyVerdict(
            PolicyDecision.REQUIRES_HUMAN,
            None,
            f"ToS review for {policy.domain} expired on {policy.next_review_due}; "
            "reverting to human handoff until it is re-reviewed",
            policy,
        )
    if policy.submission_method is SubmissionMethod.OFFICIAL_API and not policy.automation_allowed:
        return PolicyVerdict(
            PolicyDecision.BLOCKED,
            BlockReason.AUTOMATION_FORBIDDEN,
            f"{policy.domain} API submission requires automation_allowed",
            policy,
        )
    return PolicyVerdict(
        PolicyDecision.ALLOWED,
        None,
        f"{policy.submission_method} permitted for {policy.domain}",
        policy,
    )
