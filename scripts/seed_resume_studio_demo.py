#!/usr/bin/env python3
"""Seed one complete Resume Studio demo: job, application, tailored resume.

Everything here is fictional. No real person, employer, credential or contact
detail appears in this file, and none ever should: this script is committed,
and the real profile belongs only in your local database (SECURITY.md §6).

    make seed-resume-studio

Run `make seed` first - this builds on the demo profile that creates.

The resume is written by the real B6 path (`persist_resume`), not by INSERTing
into `resumes` and `resume_claims`. That matters: B6 runs the B5 handoff
coverage gate, refuses a claim whose evidence has drifted, moves the
application through the state machine, and emits the lifecycle events. Seeding
around it would produce a row that looks right and proves nothing.

Fixture rows that B0 needs before B6 can run - a source, a company, a job, a
handful of verified facts, a base resume and an A5 evaluation - are created
with the ORM, the same way `scripts/seed.py` builds the demo profile.

Every claim here is genuinely supported by the fact it cites, and the one
numeral that appears is present in that fact's `structured` payload. That is
not decoration: B2/B3's numeral guard and B5's verdicts are what the studio
displays, so inventing an impressive metric would either be caught or would
put a false claim on screen.

Idempotent: re-running detects the demo job and does nothing.
"""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, text

from packages.agents.llm.runtime import canonical
from packages.agents.resume_optimizer.context import load_tailoring_context
from packages.agents.resume_optimizer.drafting import validate_drafting
from packages.agents.resume_optimizer.handoff import bind, bullet_refs, sentence_refs
from packages.agents.resume_optimizer.resume_persistence import persist_resume
from packages.agents.resume_optimizer.selection import Selection, SelectionResult
from packages.agents.resume_optimizer.summary import validate_summary
from packages.agents.resume_optimizer.verification import VerificationResult
from packages.core.db import (
    check_database,
    dispose_engine,
    get_migration_revision,
    init_engine,
    session_scope,
)
from packages.core.models import (
    Application,
    ApplicationStatus,
    ClaimVerdict,
    Company,
    FactStatus,
    FactType,
    IngestionMethod,
    Job,
    JobEvaluation,
    JobStatus,
    ProfileFact,
    Recommendation,
    Resume,
    Source,
)
from packages.core.state_machines.application import transition_application
from packages.shared.clock import utc_now
from packages.shared.config import get_settings
from packages.shared.ids import uuid7

DEMO_EMAIL = "demo.candidate@example.com"
DEMO_JOB_URL = "https://example.invalid/demo/cloud-engineer"
DEMO_SOURCE_SLUG = "demo-local"
DEMO_COMPANY = "Demo Cloud Systems"

#: Verified facts the demo resume draws on. `structured` holds every numeral
#: that may legitimately appear in a bullet - the numeral guard checks against
#: exactly this, so a figure absent here cannot be written.
DEMO_FACTS = [
    {
        "key": "terraform",
        "text": "Managed AWS infrastructure with Terraform across 3 environments.",
        "structured": {"environments": 3},
        "fact_type": "experience",
    },
    {
        "key": "ci",
        "text": "Built CI/CD pipelines in GitHub Actions for containerised services.",
        "structured": {},
        "fact_type": "experience",
    },
    {
        "key": "linux",
        "text": "Linux systems administration and shell scripting.",
        "structured": {},
        "fact_type": "skill",
    },
    {
        "key": "observability",
        "text": "Set up monitoring and alerting with CloudWatch and Prometheus.",
        "structured": {},
        "fact_type": "experience",
    },
    {
        "key": "docker",
        "text": "Containerised applications with Docker and deployed them to ECS.",
        "structured": {},
        "fact_type": "skill",
    },
]


async def _existing_demo_job(session) -> Job | None:
    return (await session.execute(select(Job).where(Job.url == DEMO_JOB_URL))).scalar_one_or_none()


async def _demo_profile(session) -> tuple[UUID, UUID] | None:
    """The profile `make seed` created; this script does not invent one."""
    row = (
        await session.execute(
            text(
                "SELECT p.id, u.id FROM profiles p JOIN users u ON u.id = p.user_id "
                "WHERE u.email = :email"
            ),
            {"email": DEMO_EMAIL},
        )
    ).first()
    return (row[0], row[1]) if row else None


async def _source(session) -> Source:
    source = (
        await session.execute(select(Source).where(Source.slug == DEMO_SOURCE_SLUG))
    ).scalar_one_or_none()
    if source is None:
        source = Source(
            id=uuid7(),
            name="Local Demo Source",
            slug=DEMO_SOURCE_SLUG,
            domain="example.invalid",
            ingestion_method=IngestionMethod.MANUAL_IMPORT,
            enabled=True,
        )
        session.add(source)
        await session.flush()
    return source


async def _company(session) -> Company:
    company = (
        await session.execute(
            select(Company).where(Company.normalized_name == "demo cloud systems")
        )
    ).scalar_one_or_none()
    if company is None:
        company = Company(
            id=uuid7(),
            name=DEMO_COMPANY,
            normalized_name="demo cloud systems",
            domain="example.invalid",
            hq_location="Dallas, TX",
        )
        session.add(company)
        await session.flush()
    return company


async def _facts(session, profile_id: UUID, user_id: UUID) -> dict[str, ProfileFact]:
    """Verified facts, reusing any this script created on an earlier run."""
    created: dict[str, ProfileFact] = {}
    for spec in DEMO_FACTS:
        existing = (
            await session.execute(
                select(ProfileFact).where(
                    ProfileFact.profile_id == profile_id,
                    ProfileFact.text == spec["text"],
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = ProfileFact(
                id=uuid7(),
                profile_id=profile_id,
                fact_type=FactType(spec["fact_type"]),
                text=spec["text"],
                structured=spec["structured"],
                source_table="experiences",
                source_id=uuid7(),
                status=FactStatus.VERIFIED,
                verified_by=user_id,
                verified_at=utc_now(),
            )
            session.add(existing)
            await session.flush()
        created[spec["key"]] = existing
    return created


async def _base_resume(session, profile_id: UUID) -> Resume:
    base = (
        (
            await session.execute(
                select(Resume).where(Resume.profile_id == profile_id, Resume.kind == "base")
            )
        )
        .scalars()
        .first()
    )
    if base is None:
        base = Resume(
            id=uuid7(),
            profile_id=profile_id,
            kind="base",
            version=1,
            content={"sections": [{"name": "Experience"}, {"name": "Skills"}]},
        )
        session.add(base)
        await session.flush()
    return base


def _match_matrix(facts: dict[str, ProfileFact]) -> dict[str, object]:
    """An A2-shaped matrix citing only facts that exist and are verified."""
    return {
        "requirements": [
            {
                "requirement_text": "AWS infrastructure automation",
                "status": "met",
                "necessity": "required",
                "kind": "skill",
                "fact_ids": [str(facts["terraform"].id)],
            },
            {
                "requirement_text": "CI/CD pipelines",
                "status": "met",
                "necessity": "required",
                "kind": "skill",
                "fact_ids": [str(facts["ci"].id)],
            },
            {
                "requirement_text": "Linux administration",
                "status": "met",
                "necessity": "preferred",
                "kind": "skill",
                "fact_ids": [str(facts["linux"].id)],
            },
        ],
        "summary": {"met": 3, "not_met": 0},
    }


def _selection(facts: dict[str, ProfileFact]) -> SelectionResult:
    """The B1 selection the demo resume is built from."""
    return SelectionResult(
        selections=(
            Selection(
                fact_id=str(facts["terraform"].id),
                section="experience",
                order=1,
                emphasis="high",
            ),
            Selection(
                fact_id=str(facts["ci"].id),
                section="experience",
                order=2,
                emphasis="normal",
            ),
            Selection(
                fact_id=str(facts["observability"].id),
                section="experience",
                order=3,
                emphasis="normal",
            ),
            Selection(
                fact_id=str(facts["docker"].id),
                section="skills",
                order=4,
                emphasis="normal",
            ),
            Selection(
                fact_id=str(facts["linux"].id),
                section="skills",
                order=5,
                emphasis="normal",
            ),
        ),
        evaluation_gaps=(),
        material_gaps=(),
        requires_human_review=False,
        prompt_version="demo-b1",
    )


#: The B2 bullets and B3 sentences, as the models would have returned them.
#: Each cites exactly the fact that supports it, and the only numeral -
#: "3 environments" - is present in that fact's structured payload. These go
#: through the real B2/B3 validators below rather than being trusted as
#: written, so a numeral I got wrong fails the seed instead of reaching the UI.
def _drafts(facts: dict[str, ProfileFact]) -> tuple[dict, dict]:
    def bullet(text: str, key: str, numerals: list[str] | None = None) -> dict:
        return {
            "text": text,
            "fact_ids": [str(facts[key].id)],
            "numerals_used": numerals or [],
        }

    b2 = {
        "bullets": [
            bullet(
                "Managed AWS infrastructure with Terraform across 3 environments.",
                "terraform",
                ["3"],
            ),
            bullet(
                "Built CI/CD pipelines in GitHub Actions for containerised services.",
                "ci",
            ),
            bullet(
                "Set up monitoring and alerting with CloudWatch and Prometheus.",
                "observability",
            ),
            bullet(
                "Containerised applications with Docker and deployed them to ECS.",
                "docker",
            ),
            bullet(
                "Administered Linux systems and automated routine work with " "shell scripting.",
                "linux",
            ),
        ]
    }
    b3 = {
        "sentences": [
            {
                "text": (
                    "Cloud engineer with hands-on AWS infrastructure and " "Terraform experience."
                ),
                "fact_ids": [str(facts["terraform"].id)],
                "numerals_used": [],
            },
            {
                "text": "Builds CI/CD pipelines and containerised deployments on Linux.",
                "fact_ids": [
                    str(facts["ci"].id),
                    str(facts["docker"].id),
                    str(facts["linux"].id),
                ],
                "numerals_used": [],
            },
        ]
    }
    return b2, b3


def _verdicts(bullets, sentences, selection, context):
    """Stand in for a B5 run, with every verdict `supported`.

    The demo does not call a model - there is nothing to verify that a human
    has not already checked by hand here. The results are still bound through
    `bind()`, so they carry the same digest and identity B6's coverage gate
    demands; a hand-written verdict cannot skip that check.
    """
    refs = bullet_refs(bullets, selection) + sentence_refs(sentences)
    by_id = {str(fact.id): fact for fact in context.facts}
    return tuple(
        bind(
            ref,
            VerificationResult(
                verdict=ClaimVerdict.SUPPORTED,
                explanation="Every statement in this claim appears in the cited fact.",
                prompt_version="demo-b5",
            ),
            [by_id[fid] for fid in ref.fact_ids],
        )
        for ref in refs
    )


async def build_tailored_application(
    session,
    *,
    profile_id: UUID,
    facts: dict[str, ProfileFact],
    source: Source,
    company: Company,
    url: str,
    title: str,
    description: str,
    actor_id: str = "seed_resume_studio_demo",
) -> tuple[Application, object]:
    """One shortlisted job and an application with a verified tailored resume,
    built through the real B2/B3 validators, B5 verdicts and B6 persistence."""
    job = Job(
        id=uuid7(),
        source_id=source.id,
        company_id=company.id,
        url=url,
        title=title,
        normalized_title=title.lower(),
        seniority="mid",
        location_raw="Dallas, TX",
        location_city="Dallas",
        location_state="TX",
        location_country="US",
        employment_type="full_time",
        status=JobStatus.SHORTLISTED,
        description_raw=description,
    )
    session.add(job)
    await session.flush()

    session.add(
        JobEvaluation(
            id=uuid7(),
            job_id=job.id,
            profile_version=1,
            score=Decimal("78.00"),
            recommendation=Recommendation.STRONG,
            match_matrix=_match_matrix(facts),
            gaps=[],
            rationale=(
                "Verified AWS, Terraform, CI/CD and Linux experience covers the "
                "required skills for this role."
            ),
            score_breakdown={"required_met": 2, "preferred_met": 1},
        )
    )
    application = Application(
        id=uuid7(),
        job_id=job.id,
        profile_id=profile_id,
        status=ApplicationStatus.CREATED,
        priority=1,
    )
    session.add(application)
    await session.flush()

    # Into the state the pipeline enters B6 from - through the state
    # machine, which writes the history row and the status event.
    await transition_application(
        session,
        application,
        ApplicationStatus.RESUME_GENERATING,
        actor_type="system",
        actor_id=actor_id,
        reason="local demo seed",
    )
    await session.flush()

    context = await load_tailoring_context(session, profile_id=profile_id, job_id=job.id)
    selection = _selection(facts)

    # Run the demo drafts through the real B2 and B3 validators. No model
    # is called, but every guard they apply still runs: fact approval,
    # freshness, PII, the mixed-section rule, and the numeral guard. A
    # figure not present in a cited fact fails the seed here rather than
    # appearing in the studio as an unsupported claim.
    b2, b3 = _drafts(facts)
    drafting = validate_drafting(canonical(b2), context, selection, prompt_version="demo-b2")
    summary = validate_summary(canonical(b3), context, selection, prompt_version="demo-b3")
    bullets, sentences = drafting.bullets, summary.sentences
    verified = _verdicts(bullets, sentences, selection, context)

    result = await persist_resume(
        session,
        application_id=application.id,
        verified=verified,
        bullets=bullets,
        sentences=sentences,
        context=context,
        selection=selection,
        model="demo-seed",
        prompt_version="demo-b2/b3",
        cost_usd=Decimal("0"),
        actor_id=actor_id,
    )
    return application, result


async def seed() -> int:
    async with session_scope() as session:
        if await _existing_demo_job(session) is not None:
            print("Resume Studio demo already present; nothing to do")
            print("  (to rebuild it, run `make db-reset && make migrate && make seed`)")
            return 0

        profile = await _demo_profile(session)
        if profile is None:
            print("cannot seed: no demo profile found. Run `make seed` first.")
            return 1
        profile_id, user_id = profile

        source = await _source(session)
        company = await _company(session)
        facts = await _facts(session, profile_id, user_id)
        await _base_resume(session, profile_id)

        application, result = await build_tailored_application(
            session,
            profile_id=profile_id,
            facts=facts,
            source=source,
            company=company,
            url=DEMO_JOB_URL,
            title="Cloud Engineer",
            description=(
                "Demo posting for local development. Cloud Engineer working on "
                "AWS infrastructure, CI/CD and containerised services."
            ),
        )

        print("Resume Studio demo seeded.")
        print(f"  job           : Cloud Engineer at {DEMO_COMPANY} (Dallas, TX)")
        print(f"  application   : {application.id} -> {result.to_status}")
        print(f"  resume        : v{result.version} ({result.verification_status})")
        print(f"  claims        : {result.verified_count}/{result.claim_count} supported")
        print(f"  events        : {', '.join(result.emitted)}")
        print()
        print(f"  open http://localhost:3000/resume-studio/{application.id}")
        return 0


async def main() -> int:
    settings = get_settings()
    print(f"environment      : {settings.environment}")

    await init_engine(settings)
    try:
        db = await check_database()
        revision = await get_migration_revision()
        print(f"postgres         : {db['status']}")
        print(f"migration        : {revision or 'not applied'}")

        if db["status"] != "ok":
            print("\ncannot seed: database unavailable")
            return 1
        if revision is None:
            print("\ncannot seed: run `make migrate` first")
            return 1
        return await seed()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
