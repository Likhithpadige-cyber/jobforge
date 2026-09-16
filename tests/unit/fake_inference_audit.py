"""In-memory inference writer for stage unit tests (no database)."""

from decimal import Decimal

from packages.core.db.inference_audit import CallHandle, StageHandle
from packages.core.models import AgentRun, AgentRunStep, Event, LlmCall
from packages.shared.ids import uuid7


class FakeInferenceAudit:
    def __init__(self, session):
        self.session = session
        self.rows = {}

    async def begin_stage(
        self,
        *,
        agent,
        pipeline,
        step_name,
        aggregate_type,
        aggregate_id,
        correlation_id,
        steps_total=1,
        step_index=1,
    ):
        stage = StageHandle(uuid7(), uuid7())
        run = AgentRun(
            id=stage.run_id,
            agent=agent,
            pipeline=pipeline,
            status="running",
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            correlation_id=correlation_id,
            steps_total=steps_total,
            steps_completed=0,
            input_tokens=0,
            output_tokens=0,
            cost_usd=Decimal(0),
        )
        step = AgentRunStep(
            id=stage.step_id,
            run_id=stage.run_id,
            step_index=step_index,
            step_name=step_name,
            status="running",
            guardrail_results={},
        )
        self.session.add(run)
        self.session.add(step)
        self.rows[stage] = (run, step)
        return stage

    async def record_request(self, stage, *, prompt_version, input_digest, guardrail_results):
        run, step = self.rows[stage]
        run.prompt_version = prompt_version
        step.input_digest = input_digest
        step.guardrail_results = dict(guardrail_results)

    async def record_call(
        self, stage, *, model, input_tokens, output_tokens, cost_usd, prompt_hash, stop_reason
    ):
        run, _ = self.rows[stage]
        run.input_tokens += input_tokens
        run.output_tokens += output_tokens
        run.cost_usd += cost_usd
        call = LlmCall(
            id=uuid7(),
            run_id=stage.run_id,
            step_id=stage.step_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            prompt_hash=prompt_hash,
            stop_reason=stop_reason[:32],
            prompt_redacted=None,
            response_redacted=None,
        )
        self.session.add(call)
        self.rows[CallHandle(call.id)] = call
        return CallHandle(call.id)

    async def record_call_output(self, stage, call, *, response_redacted):
        self.rows[call].response_redacted = response_redacted

    async def record_step_output(self, stage, *, output):
        self.rows[stage][1].output = dict(output)

    async def record_guardrail(self, stage, *, guardrail_results, event=None):
        self.rows[stage][1].guardrail_results = dict(guardrail_results)
        if event:
            self.session.add(Event(**event.as_row()))

    async def finish_stage(
        self, stage, *, status, steps_completed, duration_ms, error_type=None, error_message=None
    ):
        run, step = self.rows[stage]
        run.status = step.status = status
        run.steps_completed = steps_completed
        run.duration_ms = step.duration_ms = duration_ms
        run.error_type = error_type
        run.error_message = error_message

    def spent(self, stage):
        return self.rows[stage][0].cost_usd
