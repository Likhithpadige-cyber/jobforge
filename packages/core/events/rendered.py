"""Canonical public resume.rendered v2 payload; no storage locators or prose."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RenderedArtifactEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: UUID
    kind: Literal["resume_pdf", "resume_docx"]
    template_version: str = Field(min_length=1)
    size_bytes: int = Field(ge=0, strict=True)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ResumeRenderedV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resume_id: UUID
    application_id: UUID
    resume_version: int = Field(ge=1, strict=True)
    approval_id: UUID
    artifacts: list[RenderedArtifactEntry] = Field(min_length=1)
