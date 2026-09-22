"""Identity and truth models (DATABASE_DESIGN.md §4).

These tables hold the human-authored ground truth. Nothing in this module is
writable by an agent role - see migrations/versions/0003_roles_and_grants.py.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from packages.core.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from packages.core.models.facts import ProfileFact


class User(TimestampMixin, UUIDPrimaryKeyMixin, Base):
    """DATABASE_DESIGN.md §4.1."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(CITEXT(), nullable=False, unique=True)
    password_hash: Mapped[str | None] = mapped_column(Text)
    totp_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    role: Mapped[str] = mapped_column(String(32), nullable=False, server_default="owner")
    last_login_at: Mapped[datetime | None]

    profile: Mapped[Profile | None] = relationship(back_populates="user", uselist=False)

    __table_args__ = (CheckConstraint("role IN ('owner', 'reviewer')", name="role_allowed"),)


class Profile(TimestampMixin, UUIDPrimaryKeyMixin, Base):
    """DATABASE_DESIGN.md §4.2.

    ``work_authorization`` is human-entered and never inferred. It is also
    never sent to an LLM (SECURITY.md §6.2).
    """

    __tablename__ = "profiles"

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    headline: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    location_city: Mapped[str | None] = mapped_column(Text)
    location_state: Mapped[str | None] = mapped_column(Text)
    location_country: Mapped[str | None] = mapped_column(Text)
    work_authorization: Mapped[str | None] = mapped_column(Text)
    willing_to_relocate: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    target_roles: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}"
    )
    min_salary: Mapped[int | None] = mapped_column(Integer)
    max_salary: Mapped[int | None] = mapped_column(Integer)
    #: M9. NULL = no preference, so employment-type filtering is opt-in per
    #: candidate rather than a rule baked into JobForge for everyone. Values
    #: are the same normalized vocabulary
    #: `packages.services.ingestion.normalize.extract_employment_type`
    #: already produces (`full_time`, `part_time`, `contract`, `internship`,
    #: `temporary`), so a job's employment_type can be compared directly
    #: against this array with no separate mapping table.
    accepted_employment_types: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    links: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    user: Mapped[User] = relationship(back_populates="profile")
    experiences: Mapped[list[Experience]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    educations: Mapped[list[Education]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    certifications: Mapped[list[Certification]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    skills: Mapped[list[Skill]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    projects: Mapped[list[Project]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    facts: Mapped[list[ProfileFact]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "min_salary IS NULL OR max_salary IS NULL OR max_salary >= min_salary",
            name="salary_range_ordered",
        ),
    )


class Experience(TimestampMixin, UUIDPrimaryKeyMixin, Base):
    """DATABASE_DESIGN.md §4.3."""

    __tablename__ = "experiences"

    profile_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    company_name: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    employment_type: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    is_remote: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date)
    # PostgreSQL generated column: cannot drift from end_date.
    is_current: Mapped[bool] = mapped_column(
        Boolean, Computed("(end_date IS NULL)", persisted=True)
    )
    description: Mapped[str | None] = mapped_column(Text)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    deleted_at: Mapped[datetime | None]

    profile: Mapped[Profile] = relationship(back_populates="experiences")

    __table_args__ = (
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="dates_ordered"),
        Index("ix_experiences_profile_id", "profile_id"),
    )


class Education(TimestampMixin, UUIDPrimaryKeyMixin, Base):
    """DATABASE_DESIGN.md §4.3."""

    __tablename__ = "educations"

    profile_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    institution: Mapped[str] = mapped_column(Text, nullable=False)
    degree: Mapped[str | None] = mapped_column(Text)
    field_of_study: Mapped[str | None] = mapped_column(Text)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    # Nullable and never inferred: an absent GPA stays absent.
    gpa: Mapped[float | None]
    location: Mapped[str | None] = mapped_column(Text)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    deleted_at: Mapped[datetime | None]

    profile: Mapped[Profile] = relationship(back_populates="educations")

    __table_args__ = (
        CheckConstraint(
            "end_date IS NULL OR start_date IS NULL OR end_date >= start_date", name="dates_ordered"
        ),
        CheckConstraint("gpa IS NULL OR (gpa >= 0 AND gpa <= 5)", name="gpa_range"),
        Index("ix_educations_profile_id", "profile_id"),
    )


class Certification(TimestampMixin, UUIDPrimaryKeyMixin, Base):
    """DATABASE_DESIGN.md §4.3.

    ``status='in_progress'`` certifications are never presented as held; the
    renderer excludes them from the Certifications section.
    """

    __tablename__ = "certifications"

    profile_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    issuer: Mapped[str | None] = mapped_column(Text)
    issued_date: Mapped[date | None] = mapped_column(Date)
    expires_date: Mapped[date | None] = mapped_column(Date)
    credential_id: Mapped[str | None] = mapped_column(Text)
    credential_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    deleted_at: Mapped[datetime | None]

    profile: Mapped[Profile] = relationship(back_populates="certifications")

    __table_args__ = (
        CheckConstraint("status IN ('active', 'expired', 'in_progress')", name="status_allowed"),
        CheckConstraint(
            "status <> 'in_progress' OR issued_date IS NULL",
            name="in_progress_has_no_issue_date",
        ),
        Index("ix_certifications_profile_id", "profile_id"),
    )


class Skill(TimestampMixin, UUIDPrimaryKeyMixin, Base):
    """DATABASE_DESIGN.md §4.3. ``proficiency`` is self-assessed by the human."""

    __tablename__ = "skills"

    profile_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(Text)
    proficiency: Mapped[str | None] = mapped_column(String(16))
    first_used_date: Mapped[date | None] = mapped_column(Date)
    last_used_date: Mapped[date | None] = mapped_column(Date)
    evidence_experience_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False, server_default="{}"
    )
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    deleted_at: Mapped[datetime | None]

    profile: Mapped[Profile] = relationship(back_populates="skills")

    __table_args__ = (
        CheckConstraint(
            "proficiency IS NULL OR proficiency IN ('working', 'proficient', 'expert')",
            name="proficiency_allowed",
        ),
        UniqueConstraint("profile_id", "name", name="uq_skills_profile_id_name"),
        Index("ix_skills_profile_id", "profile_id"),
    )


class Project(TimestampMixin, UUIDPrimaryKeyMixin, Base):
    """DATABASE_DESIGN.md §4.3."""

    __tablename__ = "projects"

    profile_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str | None] = mapped_column(Text)
    tech_stack: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    repo_url: Mapped[str | None] = mapped_column(Text)
    demo_url: Mapped[str | None] = mapped_column(Text)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    deleted_at: Mapped[datetime | None]

    profile: Mapped[Profile] = relationship(back_populates="projects")

    __table_args__ = (
        CheckConstraint(
            "end_date IS NULL OR start_date IS NULL OR end_date >= start_date", name="dates_ordered"
        ),
        Index("ix_projects_profile_id", "profile_id"),
    )


#: Tables a profile_fact may cite as its origin. Enforced by a CHECK on
#: profile_facts.source_table so a fact cannot claim an arbitrary provenance.
FACT_SOURCE_TABLES: tuple[str, ...] = (
    "experiences",
    "educations",
    "certifications",
    "skills",
    "projects",
    "profiles",
)
