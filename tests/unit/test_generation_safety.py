"""Fail closed at the shared call boundary, even for direct/future callers."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from packages.agents.llm.runtime import Prices, Request, StructuredClient
from packages.core.generation_safety import GenerationPausedError
from packages.shared.config import Settings

pytestmark = [pytest.mark.unit, pytest.mark.safety]


@pytest.mark.parametrize("mode", ["replay", "record", "live"])
@pytest.mark.parametrize("missing", [False, True])
async def test_paused_or_missing_authority_never_reaches_inference(mode, missing, tmp_path):
    transport = AsyncMock(side_effect=AssertionError("provider called"))
    client = StructuredClient(Settings(llm_mode=mode), cassette_dir=tmp_path, transport=transport)
    client.prices = Prices(client.model, Decimal("1"), Decimal("1"))
    session = None if missing else AsyncMock()
    if session is not None:
        session.scalar.return_value = True
    request = Request(client.model, "Safe", "{}", {}, "test")
    with pytest.raises(GenerationPausedError):
        await client.complete(request, session=session)
    transport.assert_not_called()
    assert not list(tmp_path.iterdir())


# Captured before the suite-wide network trap: exercise the real transport guard.
from packages.agents.llm.runtime import AnthropicTransport  # noqa: E402

provider_boundary = AnthropicTransport.__call__


async def test_direct_provider_transport_also_requires_durable_authority():
    with pytest.raises(GenerationPausedError):
        await provider_boundary(AnthropicTransport(""), Request("model", "Safe", "{}", {}, "test"))
