"""C1 — Resume Studio read endpoints.

The endpoint functions are called directly with the test session, the same way
the evaluation-endpoint tests exercise their read path: what matters here is
the read model and the version rule, not FastAPI's routing.

Strictly read-only. Several tests assert that explicitly, because a GET that
mutated would be a serious defect rather than a cosmetic one.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from apps.api.routers.resumes import (
    get_resume,
    latest_resume,
    list_applications,
)
from packages.agents.resume_optimizer.context import load_tailoring_context
from packages.agents.resume_optimizer.drafting import DraftedBullet
from packages.agents.resume_optimizer.handoff import bind, bullet_refs, sentence_refs
from packages.agents.resume_optimizer.resume_persistence import persist_resume
from packages.agents.resume_optimizer.selection import Selection, SelectionResult
from packages.agents.resume_optimizer.summary import SummarySentence
from packages.agents.resume_optimizer.verification import VerificationResult
from packages.core.models import (
    Application,
    ApplicationStatus,
    ApplicationStatusHistory,
    ClaimVerdict,
    Event,
    Resume,
    ResumeClaim,
)
from packages.core.state_machines.application import transition_application
from packages.shared.errors import NotFoundError
from packages.shared.ids import uuid7
from tests.integration import test_tailoring_context

inputs = test_tailoring_context.inputs

pytestmark = pytest.mark.integration


async def _generate(session, demo_profile, demo_job, application_id, facts, *, fail=False):
    """Run B6 once for this application and return its result."""
    context = await load_tailoring_context(session, profile_id=demo_profile, job_id=demo_job)
    fact_id = str(facts[0].id)
    selection = SelectionResult(
        selections=(Selection(fact_id=fact_id, section="experience", order=1, emphasis="normal"),),
        evaluation_gaps=(),
        material_gaps=(),
        requires_human_review=False,
        prompt_version="b1-test",
    )
    bullets = (
        DraftedBullet(
            text="Delivered AWS infrastructure work.",
            fact_ids=[fact_id],
            numerals_used=[],
        ),
    )
    sentences = (
        SummarySentence(text="Cloud engineer with verified AWS depth.", fact_ids=[fact_id]),
        SummarySentence(text="Focused on infrastructure delivery.", fact_ids=[fact_id]),
    )
    refs = bullet_refs(bullets, selection) + sentence_refs(sentences)
    by_id = {str(f.id): f for f in context.facts}
    verified = []
    for index, ref in enumerate(refs):
        verdict, reason = ClaimVerdict.SUPPORTED, "Matches the cited fact."
        if fail and index == 0:
            verdict, reason = ClaimVerdict.UNSUPPORTED, "Invents tooling."
        verified.append(
            bind(
                ref,
                VerificationResult(verdict=verdict, explanation=reason, prompt_version="b5"),
                [by_id[fid] for fid in ref.fact_ids],
            )
        )

    application = (
        await session.execute(select(Application).where(Application.id == application_id))
    ).scalar_one()
    if application.status is not ApplicationStatus.RESUME_GENERATING:
        await transition_application(
            session,
            application,
            ApplicationStatus.RESUME_GENERATING,
            actor_id="resume_optimizer",
        )
    result = await persist_resume(
        session,
        application_id=application_id,
        verified=tuple(verified),
        bullets=bullets,
        sentences=sentences,
        context=context,
        selection=selection,
        model="claude-sonnet-5",
        prompt_version="b2/b3",
    )
    await session.flush()
    return result


async def _reset(session, application_id):
    """Test-only: put the application back for a second generation attempt."""
    application = (
        await session.execute(select(Application).where(Application.id == application_id))
    ).scalar_one()
    application.status = ApplicationStatus.RESUME_GENERATING
    await session.flush()


async def _row_counts(session):
    return (
        (await session.execute(select(func.count(Resume.id)))).scalar_one(),
        (await session.execute(select(func.count(ResumeClaim.id)))).scalar_one(),
        (await session.execute(select(func.count(Event.id)))).scalar_one(),
        (await session.execute(select(func.count(ApplicationStatusHistory.id)))).scalar_one(),
    )


# --- latest-version rule ----------------------------------------------------


async def test_latest_returns_the_highest_version(
    session, demo_profile, demo_job, demo_application, inputs
):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    first = await _generate(session, demo_profile, demo_job, app_id, facts)
    await _reset(session, app_id)
    second = await _generate(session, demo_profile, demo_job, app_id, facts)

    assert (first.version, second.version) == (1, 2)
    read = await latest_resume(app_id, session)
    assert read.version == 2
    assert read.id == second.resume_id


async def test_failed_resume_is_returned_not_hidden(
    session, demo_profile, demo_job, demo_application, inputs
):
    """The newest attempt is shown even when it failed verification."""
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _generate(session, demo_profile, demo_job, app_id, facts)
    await _reset(session, app_id)
    failed = await _generate(session, demo_profile, demo_job, app_id, facts, fail=True)

    read = await latest_resume(app_id, session)
    assert read.id == failed.resume_id
    assert read.version == 2
    assert read.verification_status == "failed"
    assert read.application_status is ApplicationStatus.VERIFICATION_FAILED
    # The text that failed is present, not stripped.
    assert any(c.verdict is ClaimVerdict.UNSUPPORTED for c in read.claims)
    assert any(c.claim_text == "Delivered AWS infrastructure work." for c in read.claims)


# --- read model -------------------------------------------------------------


async def test_claims_map_correctly(session, demo_profile, demo_job, demo_application, inputs):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _generate(session, demo_profile, demo_job, app_id, facts)

    read = await latest_resume(app_id, session)
    assert len(read.claims) == 3
    bullet = next(c for c in read.claims if c.section == "experience")
    assert bullet.path == "/experience/0"
    assert bullet.claim_text == "Delivered AWS infrastructure work."
    assert [str(f) for f in bullet.fact_ids] == [str(facts[0].id)]
    assert bullet.verdict is ClaimVerdict.SUPPORTED
    assert bullet.verdict_reason == "Matches the cited fact."
    assert bullet.verified_at is not None
    # Summary first, then experience - deterministic display order.
    assert [c.section for c in read.claims] == ["summary", "summary", "experience"]


@pytest.mark.parametrize("fail", [False, True])
async def test_counts_are_correct(session, demo_profile, demo_job, demo_application, inputs, fail):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _generate(session, demo_profile, demo_job, app_id, facts, fail=fail)

    read = await latest_resume(app_id, session)
    assert read.counts.total == 3
    assert read.counts.not_checked == 0
    if fail:
        assert (read.counts.supported, read.counts.unsupported) == (2, 1)
    else:
        assert (read.counts.supported, read.counts.unsupported) == (3, 0)
    assert read.counts.overreach == 0
    # Counts are a tally of the rows in the same response, so they cannot
    # disagree with the badges rendered beside them.
    assert read.counts.total == len(read.claims)


async def test_application_status_and_job_context_included(
    session, demo_profile, demo_job, demo_application, inputs
):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _generate(session, demo_profile, demo_job, app_id, facts)

    read = await latest_resume(app_id, session)
    assert read.application_id == app_id
    assert read.application_status is ApplicationStatus.RESUME_READY
    assert read.job_id == demo_job
    assert read.job_title  # title comes from the jobs row


async def test_get_resume_by_id_matches_latest(
    session, demo_profile, demo_job, demo_application, inputs
):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    result = await _generate(session, demo_profile, demo_job, app_id, facts)

    by_id = await get_resume(result.resume_id, session)
    latest = await latest_resume(app_id, session)
    assert by_id.id == latest.id
    assert len(by_id.claims) == len(latest.claims)


# --- leakage ----------------------------------------------------------------


async def test_no_jd_or_profile_leakage(session, demo_profile, demo_job, demo_application, inputs):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _generate(session, demo_profile, demo_job, app_id, facts)

    read = await latest_resume(app_id, session)
    blob = read.model_dump_json()
    assert "Persisted A4 rationale." not in blob
    assert str(demo_profile) not in blob
    # Claims carry fact ids, never fact prose.
    assert facts[0].text not in blob
    assert "description_raw" not in json.dumps(read.content)


# --- not found --------------------------------------------------------------


async def test_unknown_application_raises_not_found(session):
    with pytest.raises(NotFoundError):
        await latest_resume(uuid7(), session)


async def test_application_without_a_resume_raises_not_found(session, demo_application):
    """Distinguished from a missing application by its message, not its code."""
    with pytest.raises(NotFoundError, match="no resume has been generated"):
        await latest_resume(demo_application["id"], session)


async def test_unknown_resume_raises_not_found(session):
    with pytest.raises(NotFoundError):
        await get_resume(uuid7(), session)


# --- listing ----------------------------------------------------------------


async def test_list_applications_reports_resume_state(
    session, demo_profile, demo_job, demo_application, inputs
):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]

    before = await list_applications(session)
    listed = next(a for a in before if a.id == app_id)
    assert listed.resume_count == 0
    assert listed.latest_resume_version is None
    assert listed.latest_verification_status is None

    await _generate(session, demo_profile, demo_job, app_id, facts)
    after = await list_applications(session)
    listed = next(a for a in after if a.id == app_id)
    assert listed.resume_count == 1
    assert listed.latest_resume_version == 1
    assert listed.latest_verification_status == "passed"
    assert listed.status is ApplicationStatus.RESUME_READY
    assert listed.job_id == demo_job


# --- parameter defaults ------------------------------------------------------


def test_query_defaults_are_real_python_values() -> None:
    """Paged endpoints must be callable directly, not only through FastAPI.

    With `limit: int = Query(default=50)` the runtime default is a `Query`
    object, and FastAPI only replaces it while resolving an HTTP request. A
    direct call then hands SQLAlchemy a `Query` for `.limit()`, which raises
    `TypeError: int() argument must be ... not 'Query'`.

    `Annotated[int, Query(...)] = 50` keeps the validation metadata while
    leaving a genuine int as the default. This test pins that for every paged
    endpoint, so the pattern cannot regress back.
    """
    import inspect

    from apps.api.routers.facts import list_facts
    from apps.api.routers.jobs import list_jobs

    expected = {
        list_applications: {"limit": 50, "offset": 0},
        list_jobs: {"limit": 50, "offset": 0},
        list_facts: {"limit": 200, "offset": 0},
    }
    for endpoint, defaults in expected.items():
        params = inspect.signature(endpoint).parameters
        for name, value in defaults.items():
            default = params[name].default
            assert type(default) is int, (
                f"{endpoint.__name__}.{name} default is {type(default).__name__}, "
                "not int - it will break direct invocation"
            )
            assert default == value, f"{endpoint.__name__}.{name} default changed"


def test_query_validation_metadata_is_preserved() -> None:
    """Converting to Annotated must not drop the HTTP bounds.

    The router modules use `from __future__ import annotations`, so
    `inspect.signature(...).annotation` is the *source string*, not the type.
    `get_type_hints(..., include_extras=True)` resolves it, and `include_extras`
    is what keeps the `Annotated[...]` metadata instead of collapsing to `int`.

    The bounds are asserted *behaviourally*, by validating values through the
    resolved annotation. Reading `Query.ge` / `Query.le` would couple this test
    to FastAPI/Pydantic internals - under Pydantic v2 the bounds live in
    `annotated_types` metadata rather than on the `Query` object - while what
    actually matters is that the constraints still reject out-of-range input.
    """
    from typing import get_type_hints

    from pydantic import TypeAdapter, ValidationError

    hints = get_type_hints(list_applications, include_extras=True)

    limit_adapter = TypeAdapter(hints["limit"])
    offset_adapter = TypeAdapter(hints["offset"])

    # In-range values pass through unchanged.
    assert limit_adapter.validate_python(1) == 1
    assert limit_adapter.validate_python(200) == 200
    assert offset_adapter.validate_python(0) == 0

    # limit is bounded to 1..200.
    with pytest.raises(ValidationError):
        limit_adapter.validate_python(0)
    with pytest.raises(ValidationError):
        limit_adapter.validate_python(201)

    # offset is non-negative.
    with pytest.raises(ValidationError):
        offset_adapter.validate_python(-1)


async def test_list_applications_uses_its_default_limit(session, demo_application) -> None:
    """The regression itself: omitting `limit` must reach the database."""
    result = await list_applications(session)
    assert isinstance(result, list)
    assert any(a.id == demo_application["id"] for a in result)


# --- read-only --------------------------------------------------------------


async def test_reads_mutate_nothing(session, demo_profile, demo_job, demo_application, inputs):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    result = await _generate(session, demo_profile, demo_job, app_id, facts)
    await session.flush()
    before = await _row_counts(session)

    await latest_resume(app_id, session)
    await get_resume(result.resume_id, session)
    await list_applications(session)
    await session.flush()

    assert await _row_counts(session) == before
    application = (
        await session.execute(select(Application).where(Application.id == app_id))
    ).scalar_one()
    assert application.status is ApplicationStatus.RESUME_READY
