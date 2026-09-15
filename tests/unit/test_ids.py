"""UUIDv7 generation tests."""

from __future__ import annotations

import time
import uuid

import pytest

from packages.shared.ids import timestamp_ms, uuid7, uuid7_str

pytestmark = pytest.mark.unit


def test_version_and_variant_are_correct() -> None:
    value = uuid7()
    assert value.version == 7
    assert (value.int >> 62) & 0b11 == 0b10


def test_ids_are_unique() -> None:
    values = {uuid7() for _ in range(10_000)}
    assert len(values) == 10_000


def test_ids_are_monotonically_ordered() -> None:
    values = [uuid7() for _ in range(1000)]
    assert values == sorted(values), "UUIDv7 must sort in creation order"


def test_ordering_survives_counter_rollover(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: a counter rollover inside one millisecond must not make
    the next ID fall back to a lower timestamp."""
    import packages.shared.ids as ids_module

    frozen_ns = 1_760_000_000_000 * 1_000_000
    monkeypatch.setattr(ids_module.time, "time_ns", lambda: frozen_ns)
    # Force the counter to the top of its range so the next call rolls over.
    ids_module._LAST_MS = frozen_ns // 1_000_000
    ids_module._COUNTER = 0x0FFE

    values = [ids_module.uuid7() for _ in range(50)]
    assert values == sorted(values), "ordering must survive a counter rollover"


def test_ordering_survives_backwards_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """An NTP step backwards must not produce out-of-order ids."""
    import packages.shared.ids as ids_module

    clock = [1_760_000_000_000 * 1_000_000]
    monkeypatch.setattr(ids_module.time, "time_ns", lambda: clock[0])
    first = [ids_module.uuid7() for _ in range(10)]
    clock[0] -= 5 * 1_000_000  # jump 5ms into the past
    second = [ids_module.uuid7() for _ in range(10)]
    combined = first + second
    assert combined == sorted(combined)


def test_embedded_timestamp_is_current() -> None:
    before = time.time_ns() // 1_000_000
    value = uuid7()
    after = time.time_ns() // 1_000_000
    assert before <= timestamp_ms(value) <= after + 1


def test_timestamp_rejects_non_v7() -> None:
    with pytest.raises(ValueError, match="expected a UUIDv7"):
        timestamp_ms(uuid.uuid4())


def test_str_helper_round_trips() -> None:
    text = uuid7_str()
    assert uuid.UUID(text).version == 7
