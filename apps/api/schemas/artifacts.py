"""Artifact metadata as a client sees it.

`storage_key` and `bucket` are deliberately absent. They are internal
addresses; publishing one tells a caller the shape of the namespace and
invites an attempt to reach the object directly, which the private bucket
would refuse but which nobody should be able to try in the first place.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ArtifactRead(BaseModel):
    """One rendered artifact, bound to the version and decision behind it."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    kind: str
    content_type: str | None
    size_bytes: int | None
    sha256: str | None
    resume_id: UUID | None
    resume_version: int | None
    approval_id: UUID | None
    template_version: str | None
    created_at: datetime
