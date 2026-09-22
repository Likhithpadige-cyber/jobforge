"""M4 process shutdown never starts more work or leaves a live task behind."""

import asyncio

import pytest

from packages.core.events.runtime import RetryPolicy, lock_key, stream_for
from packages.shared.ids import uuid7
from workers.runtime import serve

pytestmark = [pytest.mark.unit, pytest.mark.safety]


async def test_shutdown_drains_current_work_without_starting_another():
    stop = asyncio.Event()
    calls = []

    async def step():
        calls.append("started")
        stop.set()
        await asyncio.sleep(0)
        calls.append("finished")

    await serve(step, stop, interval=0)
    assert calls == ["started", "finished"]


async def test_shutdown_cancels_hung_task_and_runs_cleanup():
    stop, cleaned = asyncio.Event(), asyncio.Event()

    async def step():
        stop.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    await serve(step, stop, shutdown_timeout=0.01)
    assert cleaned.is_set()


async def test_stopped_worker_never_accepts_work():
    stop = asyncio.Event()
    stop.set()

    async def step():
        pytest.fail("work started after shutdown")

    await serve(step, stop)


def test_routing_uses_event_domain_even_when_aggregate_is_application():
    assert stream_for("resume.generation.requested") == "evt.resumes"
    assert stream_for("approval.granted") == "evt.approvals"
    assert stream_for("agent.run.failed") == "evt.system"


def test_advisory_keys_are_stable_and_group_specific():
    event_id = uuid7()
    assert lock_key(event_id, "cg.optimizer") == lock_key(event_id, "cg.optimizer")
    assert lock_key(event_id, "cg.optimizer") != lock_key(event_id, "cg.monitor")


@pytest.mark.parametrize("options", [
    {"delays": (-1,)}, {"repeated_error_limit": 0}, {"handler_timeout": 0},
])
def test_retry_configuration_rejects_invalid_bounds(options):
    with pytest.raises(ValueError):
        RetryPolicy(**options)
