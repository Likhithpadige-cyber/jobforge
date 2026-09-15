"""Deterministic pre-filter and fit scoring (AGENT_DESIGN.md §3.3, steps A0 and A3).

**No LLM call exists in this module, by design.**

Step A0 rejects obvious non-matches before any model is invoked, and step A3
computes the fit score arithmetically from a match matrix. Both are
deliberately deterministic:

* A0 is what keeps evaluation cost bounded - a posting rejected here never
  costs a token.
* A3 being arithmetic rather than a model judgement is a security property,
  not just a reproducibility one. A prompt injection in a job description can
  influence what the model puts in the match matrix, but every `met` entry
  still requires a real `fact_id`, and the score itself is computed from
  counts the model cannot reach (SECURITY.md §5.2, control 3).

The LLM-dependent step between them - building the match matrix (A2) - is not
implemented here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from packages.core.models import Recommendation

# ---------------------------------------------------------------- pre-filter


class RejectReason(StrEnum):
    TITLE_NOT_TARGETED = "title_not_targeted"
    SENIORITY_TOO_HIGH = "seniority_too_high"
    LOCATION_UNSUITABLE = "location_unsuitable"
    CLEARANCE_REQUIRED = "clearance_required"
    SALARY_BELOW_FLOOR = "salary_below_floor"
    COMPANY_BLOCKLISTED = "company_blocklisted"


#: Role families the candidate is targeting. A posting must match one.
TARGET_ROLE_PATTERNS: tuple[str, ...] = (
    r"\bcloud\s+(engineer|ops|operations)\b",
    r"\baws\s+(cloud\s+)?engineer\b",
    r"\bdevops\s+engineer\b",
    r"\binfrastructure\s+engineer\b",
    r"\bplatform\s+engineer\b",
    r"\bsite\s+reliability\s+engineer\b",
    r"\bcloud\s+support\s+engineer\b",
    r"\bsystems?\s+engineer\b",
)

#: Roles that share vocabulary but are a different job. Checked first.
EXCLUDED_ROLE_PATTERNS: tuple[str, ...] = (
    r"\b(machine\s+learning|ml|data)\s+(scientist|engineer)\b",
    r"\bresearch\s+scientist\b",
    r"\b(sales|account|marketing)\b",
    r"\b(help\s*desk|desktop\s+support|service\s+desk|technician)\b",
    r"\bsolutions?\s+architect\b",
    r"\bengineering\s+manager\b",
    r"\bfrontend|front-end|full[\s-]?stack\b",
)

#: Requirements that cannot be met and are not negotiable.
HARD_BLOCKER_PATTERNS: tuple[tuple[str, RejectReason], ...] = (
    (r"\b(active\s+)?(ts/sci|top\s+secret|security\s+clearance|polygraph)\b",
     RejectReason.CLEARANCE_REQUIRED),
)

SENIORITY_ORDER = {"junior": 0, "mid": 1, "senior": 2, "lead": 3, "unknown": 1}


@dataclass(frozen=True)
class PrefilterResult:
    accepted: bool
    reason: RejectReason | None = None
    detail: str = ""


@dataclass(frozen=True)
class CandidateConstraints:
    """What the human has stated. Never inferred."""

    max_seniority: str = "senior"
    accepts_remote: bool = True
    accepts_hybrid: bool = False
    allowed_states: frozenset[str] = frozenset()
    min_salary: int | None = None


def prefilter(
    *,
    normalized_title: str,
    description: str | None,
    seniority: str,
    is_remote: bool,
    is_hybrid: bool,
    location_state: str | None,
    salary_max: int | None,
    company_blocklisted: bool,
    constraints: CandidateConstraints,
) -> PrefilterResult:
    """Cheap rejection before any model call. Pure."""
    if company_blocklisted:
        return PrefilterResult(False, RejectReason.COMPANY_BLOCKLISTED,
                               "company is blocklisted")

    haystack = normalized_title.lower()
    for pattern in EXCLUDED_ROLE_PATTERNS:
        if re.search(pattern, haystack):
            return PrefilterResult(False, RejectReason.TITLE_NOT_TARGETED,
                                   f"title matches an excluded role family: {pattern}")

    if not any(re.search(pattern, haystack) for pattern in TARGET_ROLE_PATTERNS):
        return PrefilterResult(False, RejectReason.TITLE_NOT_TARGETED,
                               f"title {normalized_title!r} is not a targeted role")

    if SENIORITY_ORDER.get(seniority, 1) > SENIORITY_ORDER[constraints.max_seniority]:
        return PrefilterResult(False, RejectReason.SENIORITY_TOO_HIGH,
                               f"{seniority} exceeds the stated ceiling "
                               f"({constraints.max_seniority})")

    # Clearance requirements appear in the body, not the title.
    body = (description or "")[:4000].lower()
    for pattern, reason in HARD_BLOCKER_PATTERNS:
        if re.search(pattern, body):
            return PrefilterResult(False, reason, "posting states a hard blocker")

    # A stated "no hybrid" is a constraint about the working arrangement, not
    # about geography. Accepting a hybrid role because it happens to sit in an
    # allowed state would override what the human actually said.
    if is_hybrid and not constraints.accepts_hybrid:
        return PrefilterResult(
            False, RejectReason.LOCATION_UNSUITABLE,
            "hybrid role, and hybrid is not accepted",
        )

    location_ok = (
        (is_remote and constraints.accepts_remote)
        or (location_state is not None and location_state in constraints.allowed_states)
    )
    if not location_ok:
        return PrefilterResult(
            False, RejectReason.LOCATION_UNSUITABLE,
            f"remote={is_remote} hybrid={is_hybrid} state={location_state}",
        )

    # Only reject on salary when the posting states a ceiling below the floor.
    # An unstated salary is not evidence of a low one.
    if (
        constraints.min_salary is not None
        and salary_max is not None
        and salary_max < constraints.min_salary
    ):
        return PrefilterResult(False, RejectReason.SALARY_BELOW_FLOOR,
                               f"stated maximum {salary_max} is below the floor "
                               f"{constraints.min_salary}")

    return PrefilterResult(True)


# ------------------------------------------------------------------ scoring


class MatchStatus(StrEnum):
    MET = "met"
    PARTIALLY_MET = "partially_met"
    NOT_MET = "not_met"
    TRANSFERABLE = "transferable"


@dataclass
class RequirementMatch:
    """One row of the match matrix produced by step A2."""

    text: str
    required: bool
    status: MatchStatus
    fact_ids: list[str] = field(default_factory=list)

    @property
    def counts_as_met(self) -> float:
        """Partial credit is visible and labelled, never disguised as met.

        `transferable` earns less than `partially_met` because it describes
        adjacent experience rather than the thing itself.
        """
        if self.status is MatchStatus.MET:
            # An uncited `met` is downgraded: a claim without a fact behind it
            # is exactly what the grounding model exists to prevent.
            return 1.0 if self.fact_ids else 0.0
        if self.status is MatchStatus.PARTIALLY_MET:
            return 0.5 if self.fact_ids else 0.0
        if self.status is MatchStatus.TRANSFERABLE:
            return 0.25 if self.fact_ids else 0.0
        return 0.0


WEIGHTS = {
    "required": Decimal("0.45"),
    "preferred": Decimal("0.15"),
    "seniority": Decimal("0.15"),
    "domain": Decimal("0.15"),
    "location": Decimal("0.10"),
}

BANDS: tuple[tuple[Decimal, Recommendation], ...] = (
    (Decimal("75"), Recommendation.STRONG),
    (Decimal("60"), Recommendation.MODERATE),
    (Decimal("40"), Recommendation.WEAK),
)


@dataclass
class ScoreBreakdown:
    required_ratio: Decimal
    preferred_ratio: Decimal
    seniority_alignment: Decimal
    domain_alignment: Decimal
    location_fit: Decimal
    penalty: Decimal
    score: Decimal
    recommendation: Recommendation
    gaps: list[str]

    def as_dict(self) -> dict[str, object]:
        return {
            "required_ratio": float(self.required_ratio),
            "preferred_ratio": float(self.preferred_ratio),
            "seniority_alignment": float(self.seniority_alignment),
            "domain_alignment": float(self.domain_alignment),
            "location_fit": float(self.location_fit),
            "penalty": float(self.penalty),
            "score": float(self.score),
            "weights": {k: float(v) for k, v in WEIGHTS.items()},
        }


def _ratio(matches: list[RequirementMatch]) -> Decimal | None:
    """Proportion of this requirement class that is met, or None if the
    posting states none of that class.

    None rather than a neutral 0.5: a posting that lists no "preferred"
    requirements offers nothing to fail, so scoring it at half credit would
    systematically under-rate concise postings. The weight is redistributed
    instead (see `compute_score`).
    """
    if not matches:
        return None
    earned = sum(Decimal(str(m.counts_as_met)) for m in matches)
    return earned / Decimal(len(matches))


def compute_score(
    matrix: list[RequirementMatch],
    *,
    seniority_alignment: Decimal | float = 1.0,
    domain_alignment: Decimal | float = 1.0,
    location_fit: Decimal | float = 1.0,
    hard_blockers: int = 0,
) -> ScoreBreakdown:
    """Weighted arithmetic over the match matrix. Reproducible and tunable.

    `hard_blockers` applies a flat 25-point penalty each, after weighting, so
    a posting with an unmeetable requirement cannot reach a strong band on
    the strength of everything else.
    """
    required = [m for m in matrix if m.required]
    preferred = [m for m in matrix if not m.required]

    raw_required = _ratio(required)
    raw_preferred = _ratio(preferred)
    seniority = Decimal(str(seniority_alignment))
    domain = Decimal(str(domain_alignment))
    location = Decimal(str(location_fit))

    # Only score categories the posting actually states, renormalising the
    # weights over those present. Otherwise a posting with no preferred
    # requirements could never exceed 92.5 however well it matched.
    components: list[tuple[Decimal, Decimal]] = [
        (seniority, WEIGHTS["seniority"]),
        (domain, WEIGHTS["domain"]),
        (location, WEIGHTS["location"]),
    ]
    if raw_required is not None:
        components.append((raw_required, WEIGHTS["required"]))
    if raw_preferred is not None:
        components.append((raw_preferred, WEIGHTS["preferred"]))

    total_weight = sum(weight for _, weight in components)
    weighted = (
        sum(value * weight for value, weight in components) / total_weight
    ) * Decimal("100")

    required_ratio = raw_required if raw_required is not None else Decimal("0")
    preferred_ratio = raw_preferred if raw_preferred is not None else Decimal("0")

    penalty = Decimal("25") * Decimal(hard_blockers)
    score = max(Decimal("0"), min(Decimal("100"), weighted - penalty))
    score = score.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    recommendation = Recommendation.NO
    for threshold, band in BANDS:
        if score >= threshold:
            recommendation = band
            break

    # Two honesty caps the arithmetic alone would not apply.
    #
    # 1. Zero required requirements met is a "no", however well the role
    #    aligns on seniority or location. Reporting it as a weak or moderate
    #    match would overstate a candidacy with no demonstrated overlap.
    if required and raw_required == Decimal("0"):
        recommendation = Recommendation.NO

    # 2. A hard blocker is an unmeetable requirement. The flat 25-point
    #    penalty alone is not enough: subtracted from a perfect 100 it lands
    #    exactly on the 75 threshold, so a posting the candidate cannot
    #    actually satisfy would still be reported as a strong match. Cap it.
    if hard_blockers > 0 and recommendation in (
        Recommendation.STRONG,
        Recommendation.MODERATE,
    ):
        recommendation = Recommendation.WEAK

    # 3. If no required requirements could be extracted at all, there is no
    #    evidence of fit - only an absence of contrary evidence. That is not
    #    a strong match, it is an unassessed one.
    if not required:
        if recommendation is Recommendation.STRONG:
            recommendation = Recommendation.MODERATE
        gaps_unassessed = True
    else:
        gaps_unassessed = False

    # Gaps are named plainly. Softening them here would be the exact
    # dishonesty the architecture exists to prevent.
    gaps = [
        m.text
        for m in matrix
        if m.status in (MatchStatus.NOT_MET, MatchStatus.TRANSFERABLE) and m.required
    ]
    if gaps_unassessed:
        gaps.insert(0, "no required requirements could be extracted from this posting")

    return ScoreBreakdown(
        required_ratio=required_ratio,
        preferred_ratio=preferred_ratio,
        seniority_alignment=seniority,
        domain_alignment=domain,
        location_fit=location,
        penalty=penalty,
        score=score,
        recommendation=recommendation,
        gaps=gaps,
    )
