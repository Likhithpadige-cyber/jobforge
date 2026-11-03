"""Synthetic offline corpus has no operational database or open incidents."""

import pytest


@pytest.fixture(autouse=True)
def unpaused_eval_authority(monkeypatch):
    async def enabled(session):
        return None

    monkeypatch.setattr("packages.agents.llm.runtime.require_generation_enabled", enabled)
