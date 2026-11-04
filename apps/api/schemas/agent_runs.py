"""M13 Phase 3: agent run metadata for the activity view.

Deliberately narrow. A run's `error_message`, its steps' `output` and
`guardrail_results`, and every LLM call's prompt and response are never part
of these schemas: they can carry applicant data or model text, and the
activity view needs only what ran, how it ended, and what it cost.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel

from packages.core.models import AgentName, RunStatus


class AgentRunRead(BaseModel):
    id: UUID
    agent: AgentName
    pipeline: str
    status: RunStatus
    steps_total: int
    steps_completed: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    duration_ms: int | None
    #: The exception class name only; the message is never returned.
    error_type: str | None
    started_at: datetime
    finished_at: datetime | None
    #: Set only when the run belongs to an application the UI can open.
    application_id: UUID | None


class AgentRunStepRead(BaseModel):
    step_index: int
    step_name: str
    status: RunStatus
    duration_ms: int | None
    started_at: datetime
    finished_at: datetime | None


class AgentRunDetail(AgentRunRead):
    steps: list[AgentRunStepRead]


class AgentRunSummary(BaseModel):
    since: datetime
    runs: int
    by_status: dict[str, int]
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
