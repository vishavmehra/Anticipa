"""Data-fabric tables (contract 17 §3): datasets, residents, clinical_records, family_edges, documents,
chunks.

Table definitions only; behaviour lives in the data workstream. Ground truth never enters these tables
(ADR-008).
Columns beyond the contract minimum are nullable or defaulted so contract-shaped inserts always succeed.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from anticipa.db import UUID_PK, Base, sql_in

SYNTHETIC_STATUSES = ("synthetic", "simulated")  # `real` is impossible by schema (ADR-003/034)
CONFIDENCE_STATUSES = ("verified", "uncertain", "unknown")
EMBEDDING_DIM = 1024


def _synthetic_status() -> Mapped[str]:
    return mapped_column(Text, nullable=False, server_default="synthetic", default="synthetic")


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Dataset(Base):
    __tablename__ = "datasets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, **UUID_PK)
    dataset_version: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    generator: Mapped[str] = mapped_column(Text, nullable=False)
    generator_version: Mapped[str | None] = mapped_column(Text)
    base_seed: Mapped[int | None] = mapped_column(BigInteger)
    config_hash: Mapped[str | None] = mapped_column(Text)
    resident_target: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="planned", default="planned")
    manifest: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    created_at: Mapped[datetime] = _created_at()


class Resident(Base):
    __tablename__ = "residents"
    __table_args__ = (
        UniqueConstraint("source_system", "source_patient_id", name="uq_residents_source"),
        CheckConstraint(sql_in("synthetic_status", SYNTHETIC_STATUSES), name="synthetic_status"),
        Index("ix_residents_district_code", "district_code"),
        Index("ix_residents_birth_date", "birth_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, **UUID_PK)
    dataset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("datasets.id"), nullable=False)
    source_system: Mapped[str] = mapped_column(Text, nullable=False)
    source_patient_id: Mapped[str] = mapped_column(Text, nullable=False)
    birth_date: Mapped[date | None] = mapped_column(Date)
    deceased_date: Mapped[date | None] = mapped_column(Date)
    administrative_sex: Mapped[str | None] = mapped_column(Text)
    preferred_language: Mapped[str | None] = mapped_column(Text)
    district_code: Mapped[str | None] = mapped_column(Text)
    source_version: Mapped[str | None] = mapped_column(Text)
    synthetic_status: Mapped[str] = _synthetic_status()
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class ClinicalRecord(Base):
    __tablename__ = "clinical_records"
    __table_args__ = (
        UniqueConstraint(
            "resident_id", "resource_type", "source_resource_id", name="uq_clinical_records_source"
        ),
        CheckConstraint(sql_in("synthetic_status", SYNTHETIC_STATUSES), name="synthetic_status"),
        Index("ix_clinical_records_resident_category_code", "resident_id", "category", "code"),
        Index("ix_clinical_records_code_system_code", "code_system", "code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, **UUID_PK)
    resident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("residents.id", ondelete="CASCADE"), nullable=False
    )
    resource_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_resource_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_system: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    code_system: Mapped[str | None] = mapped_column(Text)
    code: Mapped[str | None] = mapped_column(Text)
    display: Mapped[str | None] = mapped_column(Text)
    # 04 §6.3: keep the original coding and add the normalised one beside it (terminology workstream fills
    # these)
    normalized_code_system: Mapped[str | None] = mapped_column(Text)
    normalized_code: Mapped[str | None] = mapped_column(Text)
    normalized_display: Mapped[str | None] = mapped_column(Text)
    effective_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str | None] = mapped_column(Text)
    value_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    source_reference: Mapped[str] = mapped_column(Text, nullable=False)
    synthetic_status: Mapped[str] = _synthetic_status()
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class FamilyEdge(Base):
    __tablename__ = "family_edges"
    __table_args__ = (
        CheckConstraint("resident_id <> related_resident_id", name="no_self_edge"),
        CheckConstraint(sql_in("confidence_status", CONFIDENCE_STATUSES), name="confidence_status"),
        CheckConstraint(sql_in("synthetic_status", SYNTHETIC_STATUSES), name="synthetic_status"),
        UniqueConstraint(
            "resident_id", "related_resident_id", "relationship_type", name="uq_family_edges_pair"
        ),
        # UNIQUE ignores NULLs, so relatives without a resident identity need their own guard against
        # duplicates.
        Index(
            "uq_family_edges_external_relative",
            "resident_id",
            "related_person_source_id",
            "relationship_type",
            unique=True,
            postgresql_where=text("related_resident_id IS NULL"),
        ),
        Index("ix_family_edges_related_resident_id", "related_resident_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, **UUID_PK)
    resident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("residents.id", ondelete="CASCADE"), nullable=False
    )
    related_resident_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("residents.id", ondelete="CASCADE")
    )
    related_person_source_id: Mapped[str | None] = mapped_column(Text)
    relationship_type: Mapped[str] = mapped_column(Text, nullable=False)
    relationship_direction: Mapped[str] = mapped_column(Text, nullable=False)
    source_resource_id: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="unknown", default="unknown"
    )
    synthetic_status: Mapped[str] = _synthetic_status()
    created_at: Mapped[datetime] = _created_at()


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("resident_id", "content_hash", name="uq_documents_resident_content"),
        CheckConstraint(sql_in("synthetic_status", SYNTHETIC_STATUSES), name="synthetic_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, **UUID_PK)
    resident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("residents.id", ondelete="CASCADE"), nullable=False
    )
    source_resource_id: Mapped[str | None] = mapped_column(Text)
    document_type: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(Text)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"), default=1)
    source_system: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="anticipa", default="anticipa"
    )
    generation_method: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(Text)
    model_id: Mapped[str | None] = mapped_column(Text)
    synthetic_status: Mapped[str] = _synthetic_status()
    # lifecycle values are owned by the evidence workstream (no CHECK until they are settled)
    processing_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="pending", default="pending"
    )
    created_on: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_document_chunks_document_index"),
        CheckConstraint("end_offset >= start_offset", name="offsets"),
        CheckConstraint(sql_in("synthetic_status", SYNTHETIC_STATUSES), name="synthetic_status"),
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, **UUID_PK)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[Any | None] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str | None] = mapped_column(Text)
    synthetic_status: Mapped[str] = _synthetic_status()
    created_at: Mapped[datetime] = _created_at()
