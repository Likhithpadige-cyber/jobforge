"""Pydantic schemas for the profile endpoints.

Request models are strict: unknown fields are rejected rather than silently
ignored, so a typo in a field name fails loudly instead of quietly dropping
data the user believed they had saved.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NonBlank = Annotated[str, Field(min_length=1, max_length=500)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------- profile
class ProfileBase(StrictModel):
    full_name: NonBlank
    headline: str | None = Field(default=None, max_length=300)
    email: str | None = None
    phone: str | None = None
    location_city: str | None = None
    location_state: str | None = None
    location_country: str | None = None
    work_authorization: str | None = Field(
        default=None,
        description=(
            "Human-entered only. Never inferred, never sent to an LLM, "
            "never auto-answered on an application."
        ),
    )
    willing_to_relocate: bool = False
    target_roles: list[str] = Field(default_factory=list, max_length=20)
    min_salary: int | None = Field(default=None, ge=0)
    max_salary: int | None = Field(default=None, ge=0)
    links: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _salary_ordered(self) -> ProfileBase:
        if self.min_salary is not None and self.max_salary is not None:
            if self.max_salary < self.min_salary:
                raise ValueError("max_salary must be greater than or equal to min_salary")
        return self


class ProfileUpdate(ProfileBase):
    #: Optimistic concurrency. Omit to skip the check.
    version: int | None = None


class ProfileRead(ORMModel):
    id: UUID
    user_id: UUID
    full_name: str
    headline: str | None
    email: str | None
    phone: str | None
    location_city: str | None
    location_state: str | None
    location_country: str | None
    work_authorization: str | None
    willing_to_relocate: bool
    target_roles: list[str]
    min_salary: int | None
    max_salary: int | None
    links: dict[str, Any]
    version: int
    created_at: datetime
    updated_at: datetime


# ------------------------------------------------------------ experiences
class ExperienceCreate(StrictModel):
    company_name: NonBlank
    title: NonBlank
    employment_type: str | None = None
    location: str | None = None
    is_remote: bool = False
    start_date: date
    end_date: date | None = None
    description: str | None = Field(default=None, max_length=8000)
    display_order: int = 0

    @model_validator(mode="after")
    def _dates_ordered(self) -> ExperienceCreate:
        if self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        return self


class ExperienceUpdate(StrictModel):
    company_name: str | None = None
    title: str | None = None
    employment_type: str | None = None
    location: str | None = None
    is_remote: bool | None = None
    start_date: date | None = None
    end_date: date | None = None
    description: str | None = None
    display_order: int | None = None


class ExperienceRead(ORMModel):
    id: UUID
    profile_id: UUID
    company_name: str
    title: str
    employment_type: str | None
    location: str | None
    is_remote: bool
    start_date: date
    end_date: date | None
    is_current: bool
    description: str | None
    display_order: int
    created_at: datetime


# ------------------------------------------------------------- educations
class EducationCreate(StrictModel):
    institution: NonBlank
    degree: str | None = None
    field_of_study: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    gpa: float | None = Field(default=None, ge=0, le=5)
    location: str | None = None
    display_order: int = 0


class EducationUpdate(StrictModel):
    institution: str | None = None
    degree: str | None = None
    field_of_study: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    gpa: float | None = Field(default=None, ge=0, le=5)
    location: str | None = None
    display_order: int | None = None


class EducationRead(ORMModel):
    id: UUID
    profile_id: UUID
    institution: str
    degree: str | None
    field_of_study: str | None
    start_date: date | None
    end_date: date | None
    gpa: float | None
    location: str | None
    display_order: int
    created_at: datetime


# --------------------------------------------------------- certifications
class CertificationCreate(StrictModel):
    name: NonBlank
    issuer: str | None = None
    issued_date: date | None = None
    expires_date: date | None = None
    credential_id: str | None = None
    credential_url: str | None = None
    status: Literal["active", "expired", "in_progress"] = "active"
    display_order: int = 0

    @model_validator(mode="after")
    def _in_progress_has_no_issue_date(self) -> CertificationCreate:
        # A certification you are studying for is not one you hold. Refusing
        # an issue date here is what stops it being rendered as held.
        if self.status == "in_progress" and self.issued_date is not None:
            raise ValueError("an in_progress certification cannot have an issued_date")
        return self


class CertificationUpdate(StrictModel):
    name: str | None = None
    issuer: str | None = None
    issued_date: date | None = None
    expires_date: date | None = None
    credential_id: str | None = None
    credential_url: str | None = None
    status: Literal["active", "expired", "in_progress"] | None = None
    display_order: int | None = None


class CertificationRead(ORMModel):
    id: UUID
    profile_id: UUID
    name: str
    issuer: str | None
    issued_date: date | None
    expires_date: date | None
    credential_id: str | None
    credential_url: str | None
    status: str
    display_order: int
    created_at: datetime


# ------------------------------------------------------------------ skills
class SkillCreate(StrictModel):
    name: NonBlank
    category: str | None = None
    proficiency: Literal["working", "proficient", "expert"] | None = None
    first_used_date: date | None = None
    last_used_date: date | None = None
    evidence_experience_ids: list[UUID] = Field(default_factory=list)
    display_order: int = 0


class SkillUpdate(StrictModel):
    name: str | None = None
    category: str | None = None
    proficiency: Literal["working", "proficient", "expert"] | None = None
    first_used_date: date | None = None
    last_used_date: date | None = None
    evidence_experience_ids: list[UUID] | None = None
    display_order: int | None = None


class SkillRead(ORMModel):
    id: UUID
    profile_id: UUID
    name: str
    category: str | None
    proficiency: str | None
    first_used_date: date | None
    last_used_date: date | None
    evidence_experience_ids: list[UUID]
    display_order: int
    created_at: datetime


# ---------------------------------------------------------------- projects
class ProjectCreate(StrictModel):
    name: NonBlank
    summary: str | None = Field(default=None, max_length=4000)
    role: str | None = None
    tech_stack: list[str] = Field(default_factory=list, max_length=40)
    repo_url: str | None = None
    demo_url: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    display_order: int = 0


class ProjectUpdate(StrictModel):
    name: str | None = None
    summary: str | None = None
    role: str | None = None
    tech_stack: list[str] | None = None
    repo_url: str | None = None
    demo_url: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    display_order: int | None = None


class ProjectRead(ORMModel):
    id: UUID
    profile_id: UUID
    name: str
    summary: str | None
    role: str | None
    tech_stack: list[str]
    repo_url: str | None
    demo_url: str | None
    start_date: date | None
    end_date: date | None
    display_order: int
    created_at: datetime


class ProfileDetail(ProfileRead):
    experiences: list[ExperienceRead] = Field(default_factory=list)
    educations: list[EducationRead] = Field(default_factory=list)
    certifications: list[CertificationRead] = Field(default_factory=list)
    skills: list[SkillRead] = Field(default_factory=list)
    projects: list[ProjectRead] = Field(default_factory=list)
