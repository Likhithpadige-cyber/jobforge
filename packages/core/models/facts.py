"""The grounding index (DATABASE_DESIGN.md §4.4-4.5).

This is the most important structure in the system. Every sentence a
generated resume contains must trace to a row here, and only a human may
create one. The enforcement is a database grant, not a prompt - see
SECURITY.md §4.1 and migrations/versions/0003_roles_and_grants.py.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from packages.core.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from packages.core.models.enums import FactStatus, FactType, fact_status_enum, fact_type_enum
from packages.core.models.identity import FACT_SOURCE_TABLES

if TYPE_CHECKING:
    from packages.core.models.applications import Artifact
    from packages.core.models.identity import Profile, User

#: Embedding width. Must match the model used by the retrieval step.
EMBEDDING_DIMENSIONS = 1024


class ProfileFact(TimestampMixin, UUIDPrimaryKeyMixin, Base):
    """An atomic, defensible statement about the candidate.

    Only ``status = 'verified'`` facts are citable in generated content.
    ``structured`` holds every numeral that may legitimately appear in a
    resume bullet; the numeral guard checks against exactly this field.
    """

    __tablename__ = "profile_facts"

    profile_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    fact_type: Mapped[FactType] = mapped_column(fact_type_enum, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    structured: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    source_table: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    status: Mapped[FactStatus] = mapped_column(
        fact_status_enum, nullable=False, server_default="draft"
    )
    confidence: Mapped[str | None] = mapped_column(Text)
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    verified_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    verified_at: Mapped[datetime | None]
    deleted_at: Mapped[datetime | None]

    profile: Mapped[Profile] = relationship(back_populates="facts")
    verifier: Mapped[User | None] = relationship()
    evidence: Mapped[list[FactEvidence]] = relationship(
        back_populates="fact", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "source_table IN (" + ", ".join(f"'{name}'" for name in FACT_SOURCE_TABLES) + ")",
            name="source_table_allowed",
        ),
        # A verified fact must record who verified it and when. Without this,
        # "verified" would be a word rather than a fact about a human action.
        CheckConstraint(
            "status <> 'verified' OR (verified_by IS NOT NULL AND verified_at IS NOT NULL)",
            name="verified_requires_actor",
        ),
        CheckConstraint("length(btrim(text)) > 0", name="text_not_blank"),
        CheckConstraint("usage_count >= 0", name="usage_count_non_negative"),
        CheckConstraint(
            "valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from",
            name="validity_ordered",
        ),
        Index(
            "idx_facts_profile_status",
            "profile_id",
            "status",
            postgresql_where=sa_text("deleted_at IS NULL"),
        ),
        Index("idx_facts_type", "fact_type"),
        Index(
            "idx_facts_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index(
            "idx_facts_text_trgm",
            "text",
            postgresql_using="gin",
            postgresql_ops={"text": "gin_trgm_ops"},
        ),
        # DEVIATION D-2 (see docs/DATABASE_IMPLEMENTATION.md): the design's
        # UNIQUE(source_table, source_id, text) is hashed on `text` because a
        # btree index entry is limited to ~2704 bytes and fact text is free-form.
        Index(
            "uq_facts_source",
            "source_table",
            "source_id",
            sa_text("md5(text)"),
            unique=True,
            postgresql_where=sa_text("deleted_at IS NULL"),
        ),
    )

    @property
    def is_citable(self) -> bool:
        """Only verified, undeleted facts may be cited in generated content."""
        return self.status == FactStatus.VERIFIED and self.deleted_at is None


class FactEvidence(UUIDPrimaryKeyMixin, Base):
    """Proof behind a fact, so the review UI can answer "prove it" in one click.

    DATABASE_DESIGN.md §4.5.
    """

    __tablename__ = "fact_evidence"

    fact_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("profile_facts.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    artifact_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("artifacts.id", ondelete="SET NULL")
    )
    url: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    added_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    fact: Mapped[ProfileFact] = relationship(back_populates="evidence")
    artifact: Mapped[Artifact | None] = relationship()

    __table_args__ = (
        CheckConstraint(
            "kind IN ('document', 'url', 'attestation', 'credential')", name="kind_allowed"
        ),
        CheckConstraint(
            "artifact_id IS NOT NULL OR url IS NOT NULL OR note IS NOT NULL",
            name="has_some_evidence",
        ),
        Index("ix_fact_evidence_fact_id", "fact_id"),
    )
