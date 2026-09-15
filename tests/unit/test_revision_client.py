"""The API client honors replay and requires explicit live prices."""

from decimal import Decimal

import pytest

from apps.api.deps import revision_client
from packages.agents.llm.runtime import ModelCallError, Request
from packages.shared.config import Settings


def test_explicit_model_prices_are_passed_to_client():
    client = revision_client(
        Settings(
            llm_tailoring_input_price_per_million=Decimal("1.25"),
            llm_tailoring_output_price_per_million=Decimal("5"),
        )
    )
    assert client.prices.model == client.model
    assert client.prices.input_per_million == Decimal("1.25")
    assert client.prices.output_per_million == Decimal("5")
    assert client.mode == "replay"


async def test_live_without_prices_fails_before_network():
    client = revision_client(Settings(llm_mode="live"))
    with pytest.raises(ModelCallError, match="Explicit prices"):
        await client.complete(
            Request(
                model=client.model,
                system="Select facts.",
                user="{}",
                schema={},
                prompt_version="test",
            )
        )
