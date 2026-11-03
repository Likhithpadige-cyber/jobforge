"""B5 -> B6 handoff contract. Binds a verdict to the exact claim it judged.

B5 returns a verdict; it does not say *which* claim the verdict belongs to,
because it only ever sees one claim at a time. B6 will persist many claims at
once, and inferring the pairing from position or text similarity is precisely
the kind of guess that turns a verified pipeline into an unverified one. This
module closes that gap before B6 exists.

Two objects and one gate:

* `ClaimRef` - a generated claim's deterministic identity, reusing the
  `resume_claims` convention (`section` + `path`, a JSON pointer into
  `resumes.content` per DATABASE_DESIGN.md §6.2) rather than inventing a
  parallel key. B6 persists these columns directly.
* `VerifiedClaim` - that identity, frozen together with the verdict, the
  explanation, the verification time and a digest of everything the verifier
  actually saw.
* `validate_coverage()` - the gate B6 calls: exact 1:1 coverage between
  generated claims and verdicts, with every mismatch failing closed.

Nothing here persists, transitions, or emits. The digest is what makes staleness
detectable: if a claim's text, its cited fact ids, or the evidence behind those
facts changed after the verifier ran, the recomputed digest differs and the
result is refused rather than reused.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING

from packages.agents.llm.runtime import canonical
from packages.agents.resume_optimizer.context import TailoringContext, TailoringFact
from packages.agents.resume_optimizer.verification import (
    ClaimNotSupportedError,
    VerificationResult,
    verify_claim,
)
from packages.core.db.inference_audit import InferenceAuditWriter
from packages.core.models import ClaimVerdict, FactStatus
from packages.shared.clock import utc_now
from packages.shared.errors import ValidationError

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from packages.agents.llm.runtime import StructuredClient
    from packages.agents.resume_optimizer.drafting import DraftedBullet
    from packages.agents.resume_optimizer.selection import SelectionResult
    from packages.agents.resume_optimizer.summary import SummarySentence

#: The section every B3 sentence belongs to (DATABASE_DESIGN.md §6.2 lists
#: `summary` alongside experience/skills/projects).
SUMMARY_SECTION = "summary"


class HandoffError(ValidationError):
    """The verifier results do not correspond exactly to the generated claims."""


@dataclass(frozen=True)
class ClaimRef:
    """One generated claim's identity, as `resume_claims` will record it."""

    section: str
    path: str
    claim_text: str
    fact_ids: tuple[str, ...]
    numerals_used: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[str, str]:
        """Deterministic identity: the section and JSON pointer B6 persists."""
        return (self.section, self.path)


@dataclass(frozen=True)
class VerifiedClaim:
    """A claim bound to the verdict that was issued for *that* claim."""

    claim: ClaimRef
    verdict: ClaimVerdict
    explanation: str
    verified_at: datetime
    input_digest: str

    @property
    def is_negative(self) -> bool:
        return self.verdict is not ClaimVerdict.SUPPORTED


def _evidence(fact: TailoringFact) -> dict[str, object]:
    """Exactly the evidence B5's prompt carries for one fact."""
    return {"id": str(fact.id), "text": fact.text, "structured": fact.structured}


def canonical_fact_order(facts: list[TailoringFact]) -> list[TailoringFact]:
    """The evidence set in a deterministic order, independent of citation order.

    Citation order carries no meaning for verification: B2/B3 use `fact_ids`
    only for membership and for the numeral pool (a union), and presentation
    order lives in B1's `Selection.order`, a separate field. So the same facts
    cited in a different order are the same verification input, and sorting by
    id is what lets the digest say so.

    Sorting a *sequence*, not a set: duplicates survive, so adding, removing or
    repeating a fact still changes the result.
    """
    return sorted(facts, key=lambda fact: str(fact.id))


def compute_input_digest(claim_text: str, cited_facts: list[TailoringFact]) -> str:
    """Digest of everything the verifier judged, canonically serialized.

    Covers the claim text, the cited fact ids and the evidence behind each of
    them, so a later edit to any of the three is detectable. Facts are hashed
    in canonical id order rather than citation order, so re-citing the same
    evidence in a different sequence is not mistaken for staleness - while an
    added, removed or edited fact still changes the digest.
    """
    return hashlib.sha256(
        canonical(
            {
                "claim_text": claim_text,
                "facts": [_evidence(f) for f in canonical_fact_order(cited_facts)],
            }
        ).encode()
    ).hexdigest()


def _facts_for(
    fact_ids: tuple[str, ...] | list[str], context: TailoringContext
) -> list[TailoringFact]:
    by_id = {str(fact.id): fact for fact in context.facts}
    missing = [fid for fid in fact_ids if fid not in by_id]
    if missing:
        raise HandoffError(
            "Cited fact no longer exists in the tailoring context; "
            "verification cannot be carried forward"
        )
    return [by_id[fid] for fid in fact_ids]


def bullet_refs(
    bullets: tuple[DraftedBullet, ...], selection: SelectionResult
) -> tuple[ClaimRef, ...]:
    """Identify B2 bullets, deriving each section the way B2 validated it.

    B2 already guarantees a bullet's facts share one section, so reading the
    section off the first cited fact is not a guess - it is the invariant B2
    enforced.
    """
    sections = {item.fact_id: item.section for item in selection.selections}
    refs: list[ClaimRef] = []
    for index, bullet in enumerate(bullets):
        if bullet.fact_ids[0] not in sections:
            raise HandoffError(
                "Bullet cites a fact B1 never selected; it has no section to be filed under"
            )
        section = sections[bullet.fact_ids[0]]
        refs.append(
            ClaimRef(
                section=str(section),
                path=f"/{section}/{index}",
                claim_text=bullet.text,
                fact_ids=tuple(bullet.fact_ids),
                numerals_used=tuple(bullet.numerals_used),
            )
        )
    return tuple(refs)


def sentence_refs(sentences: tuple[SummarySentence, ...]) -> tuple[ClaimRef, ...]:
    """Identify B3 sentences; every one of them lives in the summary section."""
    return tuple(
        ClaimRef(
            section=SUMMARY_SECTION,
            path=f"/{SUMMARY_SECTION}/{index}",
            claim_text=sentence.text,
            fact_ids=tuple(sentence.fact_ids),
            numerals_used=tuple(sentence.numerals_used),
        )
        for index, sentence in enumerate(sentences)
    )


def assert_unique_identities(refs: tuple[ClaimRef, ...] | list[ClaimRef]) -> None:
    """Refuse a claim set in which two claims share one identity.

    `bullet_refs`/`sentence_refs` cannot produce a collision - the index makes
    each path unique within a section, and no B1 section is named `summary`.
    The guard exists for claim sets built by hand (B6 assembling a rework set,
    say), where a collision would silently drop one claim's verdict.
    """
    keys = [ref.key for ref in refs]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise HandoffError(f"Two generated claims share one identity: {duplicates}")


def bind(
    claim: ClaimRef, result: VerificationResult, cited_facts: list[TailoringFact]
) -> VerifiedClaim:
    """Freeze a verdict onto the claim it was issued for.

    A `not_checked` verdict is the pre-verification default, never a verifier
    output; accepting one here would let an unverified claim reach B6 wearing a
    verdict-shaped object.
    """
    if result.verdict is ClaimVerdict.NOT_CHECKED:
        raise HandoffError("not_checked is not a verification outcome")
    return VerifiedClaim(
        claim=claim,
        verdict=result.verdict,
        explanation=result.explanation,
        verified_at=utc_now(),
        input_digest=compute_input_digest(claim.claim_text, cited_facts),
    )


async def verify_claim_for_handoff(
    session: AsyncSession,
    *,
    claim: ClaimRef,
    context: TailoringContext,
    aggregate_id: UUID,
    client: StructuredClient,
    writer: InferenceAuditWriter | None = None,
    claim_index: int | None = None,
) -> VerifiedClaim:
    """Run B5 for one claim and keep the structured result either way.

    `verify_claim` raises `ClaimNotSupportedError` on a negative verdict, which
    is correct for its own callers - the claim failed, and failing loudly is the
    fail-closed behavior. But B6 must *persist* failed claims and emit
    `resume.verification.failed` with the reason, so the structured verdict
    cannot be thrown away on the way out.

    This wrapper therefore catches only that one exception and returns the
    verdict it carries. Nothing else changes: the same run/step/LlmCall rows are
    written, the same guardrail event is emitted, there is no repair and no
    redraft, and every other error still propagates.
    """
    cited_facts = _facts_for(claim.fact_ids, context)
    try:
        result = await verify_claim(
            session,
            claim_text=claim.claim_text,
            cited_facts=cited_facts,
            aggregate_id=aggregate_id,
            client=client,
            writer=writer,
            claim_index=claim_index,
        )
    except ClaimNotSupportedError as exc:
        if exc.result is None:  # pragma: no cover - defensive
            raise
        result = exc.result
    return bind(claim, result, cited_facts)


def validate_coverage(
    verified: tuple[VerifiedClaim, ...] | list[VerifiedClaim],
    *,
    bullets: tuple[DraftedBullet, ...],
    sentences: tuple[SummarySentence, ...],
    context: TailoringContext,
    selection: SelectionResult,
) -> tuple[VerifiedClaim, ...]:
    """The gate B6 calls: exact 1:1 coverage, or refuse the whole set.

    Returns the results ordered by claim identity so B6 persists deterministically.
    Every failure mode raises; none of them is recoverable by retrying, because
    each means the verdicts no longer describe the claims in hand.
    """
    expected = bullet_refs(bullets, selection) + sentence_refs(sentences)
    assert_unique_identities(expected)

    keys = [ref.key for ref in expected]
    by_key: dict[tuple[str, str], VerifiedClaim] = {}
    for item in verified:
        if item.claim.key in by_key:
            raise HandoffError(f"Duplicate verification result for {item.claim.key}")
        by_key[item.claim.key] = item

    expected_keys = set(keys)
    orphans = set(by_key) - expected_keys
    if orphans:
        raise HandoffError(
            f"Verification result does not match any generated claim: {sorted(orphans)}"
        )
    uncovered = expected_keys - set(by_key)
    if uncovered:
        raise HandoffError(f"Generated claim has no verification result: {sorted(uncovered)}")

    for ref in expected:
        item = by_key[ref.key]
        if item.claim.claim_text != ref.claim_text:
            raise HandoffError(f"Claim text changed after verification at {ref.path}")
        # Compared as a canonically ordered sequence, matching the digest:
        # a reordering of the same citations is not a change, but an added,
        # removed or substituted fact still is.
        if sorted(item.claim.fact_ids) != sorted(ref.fact_ids):
            raise HandoffError(f"Cited fact_ids changed after verification at {ref.path}")
        if item.verdict is ClaimVerdict.NOT_CHECKED:
            raise HandoffError(f"Claim at {ref.path} was never verified")

        # Facts must still be real and still verified at handoff time, not only
        # when B5 ran: a fact retired in between invalidates the verdict.
        cited = _facts_for(ref.fact_ids, context)
        for fact in cited:
            if fact.status is not FactStatus.VERIFIED:
                raise HandoffError(
                    f"Cited fact {fact.id} is no longer verified; {ref.path} must be re-verified"
                )
        if item.input_digest != compute_input_digest(ref.claim_text, cited):
            raise HandoffError(
                f"Evidence behind {ref.path} changed after verification; result is stale"
            )

    return tuple(by_key[ref.key] for ref in expected)


def normalize_fact_order(claim: ClaimRef, order: tuple[str, ...]) -> ClaimRef:
    """Re-state a claim's fact ids in a different presentation order.

    `ClaimRef.fact_ids` keeps the order B2/B3 cited, because that is what a
    reader sees; this helper exists for a caller that needs a different one.
    It cannot change which facts are cited - only their sequence - and it does
    not affect the digest or the coverage check, both of which compare in
    canonical id order.
    """
    if set(order) != set(claim.fact_ids):
        raise HandoffError("Reordering must contain exactly the same fact ids")
    return replace(claim, fact_ids=tuple(order))
