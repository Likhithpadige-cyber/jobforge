"""Approval-relevant material versions exclude reversible verification bookkeeping."""

from copy import deepcopy
from datetime import date
from uuid import UUID

import pytest

from packages.core.models import FactStatus, FactType, ProfileFact
from packages.core.services.resume_export import fact_fingerprint

pytestmark = [pytest.mark.unit, pytest.mark.safety]


def fact():
    return ProfileFact(
        id=UUID(int=1),
        profile_id=UUID(int=2),
        fact_type=FactType.EXPERIENCE,
        text="Maintained résumé evidence",
        structured={"count": 3},
        source_table="experiences",
        source_id=UUID(int=3),
        valid_from=date(2020, 1, 1),
        valid_to=date(2030, 1, 1),
        confidence="Owner attestation",
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", UUID(int=4)),
        ("profile_id", UUID(int=5)),
        ("text", "Corrected claim"),
        ("structured", {"count": 4}),
        ("fact_type", FactType.SKILL),
        ("source_table", "projects"),
        ("source_id", UUID(int=6)),
        ("valid_from", date(2021, 1, 1)),
        ("valid_to", date(2029, 1, 1)),
        ("confidence", "Corrected confidence"),
    ],
)
def test_each_approval_relevant_field_changes_material_version(field, value):
    original = fact()
    before = deepcopy(fact_fingerprint(original))
    setattr(original, field, value)
    after = fact_fingerprint(original)
    assert before["version"] != after["version"] and before["sha256"] != after["sha256"]
    assert len(after["version"]) == len(after["sha256"]) == 64


def test_audit_counters_and_equivalent_unicode_do_not_change_material_version():
    original = fact()
    before = fact_fingerprint(original)
    original.status = FactStatus.DRAFT
    original.usage_count = 20
    original.verified_by = UUID(int=99)
    original.text = "Maintained re\u0301sume\u0301 evidence"
    assert fact_fingerprint(original) == before
