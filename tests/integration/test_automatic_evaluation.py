"""M5 ingestion/outbox/worker evaluation, using real PostgreSQL and Redis."""

from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.agents.llm.runtime import StructuredClient
from packages.agents.resume_optimizer.evaluation import evaluation_handler
from packages.agents.resume_optimizer.persistence import Thresholds
from packages.core.events import emit
from packages.core.events.runtime import DeliveryRuntime, RetryPolicy, StreamConsumer, publish_event
from packages.core.models import (
    Event,
    FactStatus,
    FactType,
    Job,
    JobEvaluation,
    JobStatus,
    Profile,
    ProfileFact,
    User,
)
from packages.services.ingestion import IngestionPipeline
from packages.shared.clock import utc_now
from tests.integration import test_idempotent_claim as claims
from tests.integration import test_ingestion as ingestion
from tests.integration import test_worker_runtime as workers

pytestmark = [pytest.mark.integration, pytest.mark.safety]
committed_job = claims.committed_job
permitted_source = ingestion.permitted_source
bus = workers.bus


@pytest.fixture
async def candidate(engine, committed_job):
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions.begin() as db:
        user = User(email=f"m5-{uuid4().hex}@example.invalid", role="owner")
        db.add(user)
        await db.flush()
        profile = Profile(
            user_id=user.id,
            full_name="Synthetic M5 Candidate",
            target_roles=["Cloud Engineer"],
            location_country="US",
        )
        db.add(profile)
        await db.flush()
        db.add(
            ProfileFact(
                profile_id=profile.id,
                fact_type=FactType.SKILL,
                text="AWS and Terraform",
                source_table="profiles",
                source_id=profile.id,
                status=FactStatus.VERIFIED,
                verified_by=user.id,
                verified_at=utc_now(),
                structured={},
            )
        )
        db.add(
            ProfileFact(
                profile_id=profile.id,
                fact_type=FactType.PREFERENCE,
                text="Explicit preferences",
                source_table="profiles",
                source_id=profile.id,
                status=FactStatus.VERIFIED,
                verified_by=user.id,
                verified_at=utc_now(),
                structured={"max_seniority": "senior", "accepts_remote": True},
            )
        )
        job = await db.get(Job, committed_job)
        job.description_raw = "Requirements\n- Experience with AWS\n- Experience with Terraform"
        job.normalized_title = "Cloud Engineer"
        job.seniority = "mid"
        job.is_remote = True
        job.location_country = "US"
        event = await emit(
            db,
            type="job.normalized",
            aggregate_type="job",
            aggregate_id=job.id,
            correlation_id=job.id,
            actor_type="system",
            payload={"job_id": str(job.id)},
        )
    return sessions, profile.id, committed_job, event.id


def runtime(candidate, sessions=None):
    return DeliveryRuntime(
        sessions or candidate[0],
        f"test.m5.{uuid4().hex}",
        {
            "job.normalized": evaluation_handler(candidate[1], Thresholds()),
        },
        retry=RetryPolicy(delays=(0, 0)),
    )


@pytest.mark.parametrize("scenario", ["match", "empty", "missing_alignment", "missing_fact"])
async def test_real_delivery_is_idempotent_and_never_calls_provider(
    candidate,
    monkeypatch,
    scenario,
):
    sessions, _, job_id, event_id = candidate
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("M5 must never call a provider")

    monkeypatch.setattr(StructuredClient, "complete", forbidden)
    async with sessions.begin() as db:
        job = await db.get(Job, job_id)
        if scenario == "empty":
            job.description_raw = None
        elif scenario == "missing_alignment":
            job.seniority = None
        elif scenario == "missing_fact":
            job.description_raw += "\n- Experience with Kubernetes"
    consumer = runtime(candidate)
    assert await consumer.process(event_id) == "completed"
    assert await consumer.process(event_id) == "duplicate"
    async with sessions() as db:
        rows = list(
            (
                await db.scalars(
                    select(JobEvaluation).where(
                        JobEvaluation.job_id == job_id,
                    )
                )
            ).all()
        )
        assert len(rows) == 1
        job = await db.get(Job, job_id)
        row = rows[0]
        assert row.match_matrix["profile_id"] == str(candidate[1])
        if scenario == "match":
            assert row.assessment_status == "assessed"
            assert row.score == 100
            assert job.status == JobStatus.SHORTLISTED
        else:
            assert row.assessment_status == "insufficient_evidence"
            assert row.score is row.recommendation is None
            assert job.status == JobStatus.EVALUATED
            assert row.score_breakdown["insufficient_reasons"]
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Event)
                .where(Event.causation_id == event_id, Event.type == "job.evaluated")
            )
            == 1
        )
    assert not calls


async def test_agent_role_can_evaluate_and_redis_redelivery_stays_idempotent(
    candidate,
    agent_engine,
    bus,
):
    sessions = async_sessionmaker(agent_engine, expire_on_commit=False)
    worker = runtime(candidate, sessions)
    redis, prefix = bus
    consumer = StreamConsumer(redis, worker, "m5", prefix=prefix)
    await consumer.initialize(recover=False)
    async with candidate[0].begin() as db:
        await publish_event(redis, await db.get(Event, candidate[3]), prefix)
    await consumer.step()
    assert await worker.process(candidate[3]) == "duplicate"
    async with sessions() as db:
        evaluation = await db.scalar(
            select(JobEvaluation).where(
                JobEvaluation.job_id == candidate[2],
            )
        )
        assert evaluation.assessment_status == "assessed"


async def test_missing_candidate_is_observable_and_retryable(candidate):
    sessions, _, job_id, event_id = candidate
    worker = DeliveryRuntime(
        sessions,
        f"test.m5.{uuid4().hex}",
        {
            "job.normalized": evaluation_handler(uuid4(), Thresholds()),
        },
    )
    assert await worker.process(event_id) == "retry"
    async with sessions() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(JobEvaluation)
                .where(JobEvaluation.job_id == job_id)
            )
            == 0
        )
        failure = await db.scalar(
            select(Event).where(Event.causation_id == event_id, Event.type == "agent.run.failed")
        )
        assert failure.payload["will_retry"] is True
        assert (await db.get(Job, job_id)).status == JobStatus.NORMALIZED


async def test_distinct_normalized_events_do_not_duplicate_a_completed_evaluation(candidate):
    worker = runtime(candidate)
    assert await worker.process(candidate[3]) == "completed"
    async with candidate[0].begin() as db:
        second = await emit(
            db,
            type="job.normalized",
            aggregate_type="job",
            aggregate_id=candidate[2],
            correlation_id=candidate[2],
            actor_type="system",
            payload={},
        )
    assert await worker.process(second.id) == "completed"
    async with candidate[0]() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(JobEvaluation)
                .where(JobEvaluation.job_id == candidate[2])
            )
            == 1
        )


async def test_ingestion_emits_once_and_reaches_evaluation(session, permitted_source, demo_profile):
    pipeline = IngestionPipeline(session)
    posting = ingestion.posting(external_id=uuid4().hex)
    report = await pipeline.ingest_batch(permitted_source["slug"], [posting])
    job = await session.scalar(select(Job).where(Job.source_id == permitted_source["id"]))
    assert report.created == 1
    events = list(
        (
            await session.scalars(
                select(Event).where(Event.aggregate_id == job.id, Event.type == "job.normalized")
            )
        ).all()
    )
    assert len(events) == 1
    await evaluation_handler(demo_profile, Thresholds())(session, events[0])
    evaluation = await session.scalar(select(JobEvaluation).where(JobEvaluation.job_id == job.id))
    assert evaluation.assessment_status == "insufficient_evidence"
    assert job.status == JobStatus.EVALUATED
    await pipeline.ingest_batch(permitted_source["slug"], [posting])
    assert (
        await session.scalar(
            select(func.count())
            .select_from(Event)
            .where(Event.aggregate_id == job.id, Event.type == "job.normalized")
        )
        == 1
    )


@pytest.mark.parametrize("unresolved", [False, True])
async def test_http_read_serializes_both_states(candidate, db_app_client, unresolved):
    if unresolved:
        async with candidate[0].begin() as db:
            (await db.get(Job, candidate[2])).description_raw = None
    assert await runtime(candidate).process(candidate[3]) == "completed"
    response = await db_app_client.get(f"/api/v1/jobs/{candidate[2]}/evaluation")
    assert response.status_code == 200
    body = response.json()
    assert body["assessment_status"] == ("insufficient_evidence" if unresolved else "assessed")
    if unresolved:
        assert body["score"] is body["recommendation"] is None
        assert body["score_breakdown"]["insufficient_reasons"]
    else:
        assert body["score"] == 100
        assert body["recommendation"] == "strong"


async def test_supported_partial_match_and_explicit_preferences_can_discard(candidate):
    async with candidate[0].begin() as db:
        job = await db.get(Job, candidate[2])
        job.seniority = "lead"
        job.description_raw = "Requirements\n- 5 years of AWS experience\n- 5 years of Terraform"
        facts = list(
            (
                await db.scalars(select(ProfileFact).where(ProfileFact.profile_id == candidate[1]))
            ).all()
        )
        # Follow the real human edit lifecycle; verified substance is immutable.
        for fact in facts:
            fact.status = FactStatus.DRAFT
        await db.flush()
        for fact in facts:
            if fact.fact_type == FactType.SKILL:
                fact.structured = {"start_date": "2024-01-01", "end_date": "2025-01-01"}
            else:
                fact.structured = {"max_seniority": "junior", "accepts_remote": False}
        await db.flush()
        for fact in facts:
            fact.status = FactStatus.VERIFIED
            fact.verified_at = utc_now()
    assert await runtime(candidate).process(candidate[3]) == "completed"
    async with candidate[0]() as db:
        row = await db.scalar(select(JobEvaluation).where(JobEvaluation.job_id == candidate[2]))
        assert row.assessment_status == "assessed"
        assert row.score < 55
        assert (await db.get(Job, candidate[2])).status == JobStatus.DISCARDED


@pytest.mark.parametrize("change", ["authorization", "geography", "unverified", "expired"])
async def test_material_unknowns_remain_unresolved(candidate, change):
    from datetime import date

    async with candidate[0].begin() as db:
        job = await db.get(Job, candidate[2])
        if change == "authorization":
            job.description_raw += "\nApplicants must have work authorization."
        elif change == "geography":
            job.location_country = None
        else:
            fact = await db.scalar(
                select(ProfileFact).where(
                    ProfileFact.profile_id == candidate[1],
                    ProfileFact.fact_type == FactType.SKILL,
                )
            )
            if change == "unverified":
                fact.status = FactStatus.DRAFT
            else:
                fact.valid_to = date(2020, 1, 1)
    assert await runtime(candidate).process(candidate[3]) == "completed"
    async with candidate[0]() as db:
        row = await db.scalar(select(JobEvaluation).where(JobEvaluation.job_id == candidate[2]))
        assert row.assessment_status == "insufficient_evidence"
        assert row.score is row.recommendation is None
        assert (await db.get(Job, candidate[2])).status == JobStatus.EVALUATED
