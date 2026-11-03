"""Existing guardrail vocabulary to durable, idempotent operational incidents."""

from __future__ import annotations

from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import Table
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Alert, AlertSeverity, Event

AUTO_PAUSE_KINDS = frozenset({"fabrication", "numeral_invented", "sensitive_auto_answer_attempt"})
KINDS = AUTO_PAUSE_KINDS | {
    "overreach",
    "prompt_injection",
    "schema_violation",
    "pii_in_prompt",
    "cost_ceiling",
}


async def open_guardrail_alert(session: AsyncSession, event: Event) -> str:
    kind = event.payload.get("kind")
    severity = event.payload.get("severity")
    if (
        not isinstance(kind, str)
        or not isinstance(severity, str)
        or kind not in KINDS
        or severity not in {value.value for value in AlertSeverity}
    ):
        return "ignored:unsupported_guardrail"
    alert_id = uuid5(NAMESPACE_URL, f"jobforge:guardrail-alert:{event.id}")
    detail = {"source_event_id": str(event.id), "category": kind}
    # Never retain arbitrary detail, prompts, exception text or generated content.
    try:
        run_id = UUID(str(event.payload.get("run_id", "")))
    except ValueError:
        pass
    else:
        detail["run_id"] = str(run_id)
    await session.execute(
        insert(cast(Table, Alert.__table__))
        .values(
            id=alert_id,
            kind=kind,
            severity=severity,
            title=f"Guardrail incident: {kind}",
            detail=detail,
            aggregate_type=event.aggregate_type
            if event.aggregate_type
            in {
                "job",
                "application",
                "resume",
                "cover_letter",
                "system",
            }
            else None,
            aggregate_id=event.aggregate_id,
            status="open",
            auto_paused=severity == "critical" and kind in AUTO_PAUSE_KINDS,
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    return f"alert:{alert_id}"
