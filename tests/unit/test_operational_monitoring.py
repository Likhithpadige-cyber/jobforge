"""M12 Phase 2 rule boundaries, settings and the heartbeat writer, no services."""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError

from packages.core.events.operational_monitoring import (
    AUTO_RESOLVABLE,
    HUMAN_ONLY,
    SEVERITY,
    STALL_STATUSES,
    TITLE,
    backlog_exceeded,
    failure_spike,
    heartbeat_missing,
    lag_recovered,
    lag_sustained,
    stalled,
)
from packages.core.heartbeats import Heartbeat, HeartbeatWriter, RedisOperationsProbe
from packages.core.models import ApplicationStatus
from packages.shared.config import Settings

pytestmark = [pytest.mark.unit, pytest.mark.safety]

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


# ------------------------------------------------- submission failure spike
@pytest.mark.parametrize("count", [0, 1, 9])
def test_spike_needs_a_full_window_before_it_can_say_anything(count):
    assert failure_spike([True] * count, 10, 0.30) is None


@pytest.mark.parametrize(
    ("failures", "expected"),
    [(0, False), (3, False), (4, True), (10, True)],
)
def test_spike_is_strictly_above_thirty_percent_of_ten(failures, expected):
    failed = [True] * failures + [False] * (10 - failures)
    assert failure_spike(failed, 10, 0.30) is expected


def test_spike_counts_only_the_most_recent_window():
    # Newest first: the old failures beyond the window are ignored.
    assert failure_spike([False] * 10 + [True] * 10, 10, 0.30) is False
    assert failure_spike([True] * 4 + [False] * 6 + [False] * 5, 10, 0.30) is True


def test_spike_at_exactly_the_rate_is_not_a_spike():
    assert failure_spike([True] * 3 + [False] * 7, 10, 0.30) is False
    assert failure_spike([True] + [False] * 3, 4, 0.25) is False


# ------------------------------------------------------------- heartbeats
def _beat(age_seconds):
    return Heartbeat("host-1", "submission", NOW - timedelta(seconds=age_seconds))


@pytest.mark.parametrize(("age", "missing"), [(0, False), (89, False), (90, False), (91, True)])
def test_heartbeat_missing_only_strictly_past_the_threshold(age, missing):
    assert heartbeat_missing(_beat(age), NOW, 90) is missing


def test_heartbeat_settings_require_at_least_two_missed_beats():
    assert Settings().monitor_heartbeat_missing_seconds == 90
    with pytest.raises(ValidationError):
        Settings(worker_heartbeat_interval_seconds=60, monitor_heartbeat_missing_seconds=90)
    Settings(worker_heartbeat_interval_seconds=45, monitor_heartbeat_missing_seconds=90)


@pytest.mark.parametrize(
    "overrides",
    [
        {"monitor_submission_failure_rate": 1.0},
        {"monitor_submission_failure_rate": -0.1},
        {"monitor_submission_failure_window": 0},
        {"monitor_approval_backlog": -1},
        {"monitor_stall_seconds": 30},
    ],
)
def test_invalid_monitor_thresholds_are_rejected(overrides):
    with pytest.raises(ValidationError):
        Settings(**overrides)


# ------------------------------------------------- stall / backlog / lag
def test_stall_threshold_is_strict():
    entered = NOW - timedelta(seconds=86_400)
    assert not stalled(entered, NOW, 86_400)
    assert stalled(entered - timedelta(seconds=1), NOW, 86_400)


def test_stall_states_are_system_owned_only():
    assert set(STALL_STATUSES) == {
        ApplicationStatus.CREATED,
        ApplicationStatus.RESUME_GENERATING,
        ApplicationStatus.QUESTIONS_PENDING,
        ApplicationStatus.SUBMITTING,
    }
    # Waiting on a human is a backlog, never a stall.
    for waiting in (
        ApplicationStatus.AWAITING_APPROVAL,
        ApplicationStatus.CHANGES_REQUESTED,
        ApplicationStatus.SUBMISSION_READY,
    ):
        assert waiting not in STALL_STATUSES


@pytest.mark.parametrize(("pending", "exceeded"), [(0, False), (30, False), (31, True)])
def test_backlog_is_strictly_above_threshold(pending, exceeded):
    assert backlog_exceeded(pending, 30) is exceeded


@pytest.mark.parametrize(
    ("current", "earlier", "sustained"),
    [
        (501, 501, True),
        (500, 900, False),
        (900, 500, False),
        (None, 900, False),
        (900, None, False),
    ],
)
def test_lag_must_exceed_in_both_observations(current, earlier, sustained):
    assert lag_sustained(current, earlier, 500) is sustained


def test_unknown_lag_is_never_a_recovery():
    assert lag_recovered(None, 500) is False
    assert lag_recovered(500, 500) is True
    assert lag_recovered(501, 500) is False


def test_resolution_policy_is_partitioned():
    assert AUTO_RESOLVABLE.isdisjoint(HUMAN_ONLY)
    assert {"dead_letter", "submission_ambiguous"} == HUMAN_ONLY
    for kind in AUTO_RESOLVABLE | HUMAN_ONLY:
        assert kind in SEVERITY and kind in TITLE
    # Phase 1 generation-pause kinds are not operational kinds.
    assert {"fabrication", "numeral_invented"}.isdisjoint(AUTO_RESOLVABLE | HUMAN_ONLY)


# ------------------------------------------------------- heartbeat writer
class FakeHashRedis:
    def __init__(self, fail=False):
        self.data, self.fail, self.calls = {}, fail, 0

    async def hset(self, key, field, value):
        self.calls += 1
        if self.fail:
            raise RedisConnectionError("down")
        self.data.setdefault(key, {})[field] = value

    async def hdel(self, key, *fields):
        if self.fail:
            raise RedisConnectionError("down")
        for field in fields:
            self.data.get(key, {}).pop(field, None)

    async def hgetall(self, key):
        return dict(self.data.get(key, {}))

    async def xinfo_groups(self, stream):
        if stream != "evt.jobs":
            raise ResponseError("no such key")
        return [{"name": "cg.optimizer", "lag": 7}, {"name": "cg.monitor", "lag": None}]


async def test_heartbeat_is_throttled_and_carries_no_secrets():
    redis = FakeHashRedis()
    writer = HeartbeatWriter(redis, kind="submission", instance="host-1", interval=60)
    await writer.beat()
    await writer.beat()
    assert redis.calls == 1
    value = json.loads(redis.data["jobforge:worker_heartbeats"]["host-1"])
    assert set(value) == {"kind", "last_seen"} and value["kind"] == "submission"
    assert abs(value["last_seen"] - time.time()) < 5


async def test_heartbeat_failure_never_raises_and_retries_next_step():
    redis = FakeHashRedis(fail=True)
    writer = HeartbeatWriter(redis, kind="relay", instance="host-2", interval=60)
    await writer.beat()
    await writer.beat()  # not throttled after a failure
    await writer.deregister()
    assert redis.calls == 2


async def test_deregister_removes_only_this_instance():
    redis = FakeHashRedis()
    for name in ("a", "b"):
        await HeartbeatWriter(redis, kind="monitor", instance=name, interval=1).beat()
    await HeartbeatWriter(redis, kind="monitor", instance="a", interval=1).deregister()
    assert set(redis.data["jobforge:worker_heartbeats"]) == {"b"}


async def test_probe_skips_malformed_registrations():
    redis = FakeHashRedis()
    redis.data["jobforge:worker_heartbeats"] = {
        "good": json.dumps({"kind": "relay", "last_seen": NOW.timestamp()}),
        "no-kind": json.dumps({"last_seen": NOW.timestamp()}),
        "garbage": "not json",
    }
    beats = await RedisOperationsProbe(redis).heartbeats()
    assert beats == [Heartbeat("good", "relay", NOW)]


async def test_probe_reads_lag_and_skips_missing_streams():
    lags = await RedisOperationsProbe(FakeHashRedis()).consumer_lags()
    assert [(lag.stream, lag.group, lag.lag) for lag in lags] == [
        ("evt.jobs", "cg.monitor", None),
        ("evt.jobs", "cg.optimizer", 7),
    ]


async def test_serve_registers_first_and_beats_during_a_long_step():
    from workers.runtime import serve

    redis = FakeHashRedis()
    writer = HeartbeatWriter(redis, kind="first-draft", instance="host-3", interval=0.01)
    stop = asyncio.Event()
    seen = {}

    async def long_step():
        # A generation step far longer than the beat interval.
        seen["at_start"] = redis.calls
        await asyncio.sleep(0.2)
        seen["at_end"] = redis.calls
        stop.set()

    await serve(long_step, stop, interval=0, heartbeat=writer)
    assert seen["at_start"] >= 1  # registered before the first step ran
    assert seen["at_end"] - seen["at_start"] >= 3  # still alive mid-step
    after = redis.calls
    await asyncio.sleep(0.05)
    assert redis.calls == after  # the pulse stops with serve()
