"""Unit model tests use an explicitly unpaused fake database authority.

Production StructuredClient calls require a database session. The real authority,
including absent-session refusal, is exercised separately in safety tests.
"""

import pytest


@pytest.fixture(autouse=True)
def unpaused_unit_authority(monkeypatch, request):
    if request.node.path.name == "test_generation_safety.py":
        return

    async def enabled(session):
        return None

    monkeypatch.setattr("packages.agents.llm.runtime.require_generation_enabled", enabled)
