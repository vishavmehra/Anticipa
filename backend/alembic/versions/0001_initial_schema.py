"""foundation schema: contract 17 section 3 tables, RLS, Data-API revokes, append-only audit log (ADR-058)

Revision ID: 0001
Revises:
Create Date: 2026-11-08
"""

# ruff: noqa: E501  (long DDL lines, kept one statement per line for reviewability)
from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op
from anticipa.db import HARDEN_SQL  # ADR-058: RLS on every table, no anon/authenticated access (idempotent)

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "anticipa"
EMBEDDING_DIM = 1024

# pgvector lives in schema `extensions` (as on Supabase); connections use search_path = anticipa, extensions, public.
# If the extension was pre-installed in `public`, move it so the ADR-058 layout holds everywhere.
CREATE_EXTENSION_SQL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};
CREATE SCHEMA IF NOT EXISTS extensions;
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
               WHERE e.extname = 'vector' AND n.nspname = 'public') THEN
        ALTER EXTENSION vector SET SCHEMA extensions;
    END IF;
END $$;
"""  # noqa: S608  (SCHEMA is a constant)

# Contract 17 section 2: audit_events is append-only; the app never updates/deletes and the database enforces it.
AUDIT_IMMUTABLE_SQL = f"""
CREATE FUNCTION {SCHEMA}.audit_events_immutable() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only (% rejected)', TG_OP;
END $$;
CREATE TRIGGER audit_events_no_update_delete BEFORE UPDATE OR DELETE ON {SCHEMA}.audit_events
    FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.audit_events_immutable();
CREATE TRIGGER audit_events_no_truncate BEFORE TRUNCATE ON {SCHEMA}.audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION {SCHEMA}.audit_events_immutable();
"""  # noqa: S608  (SCHEMA is a constant)


def upgrade() -> None:
    op.execute(CREATE_EXTENSION_SQL)
    op.create_table(
        "audit_events",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.Column("actor_role", sa.Text(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.Text(), nullable=False),
        sa.Column("resource_id", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint("outcome IN ('success','failure','denied')", name=op.f("ck_audit_events_outcome")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
        schema=SCHEMA,
    )
    op.create_index("ix_audit_events_actor_id", "audit_events", ["actor_id"], unique=False, schema=SCHEMA)
    op.create_index(
        "ix_audit_events_occurred_at", "audit_events", ["occurred_at"], unique=False, schema=SCHEMA
    )
    op.create_index(
        "ix_audit_events_resource",
        "audit_events",
        ["resource_type", "resource_id"],
        unique=False,
        schema=SCHEMA,
    )
    op.create_table(
        "datasets",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("dataset_version", sa.Text(), nullable=False),
        sa.Column("generator", sa.Text(), nullable=False),
        sa.Column("generator_version", sa.Text(), nullable=True),
        sa.Column("base_seed", sa.BigInteger(), nullable=True),
        sa.Column("config_hash", sa.Text(), nullable=True),
        sa.Column("resident_target", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), server_default="planned", nullable=False),
        sa.Column(
            "manifest",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_datasets")),
        sa.UniqueConstraint("dataset_version", name=op.f("uq_datasets_dataset_version")),
        schema=SCHEMA,
    )
    op.create_table(
        "model_invocations",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("validation_status", sa.Text(), nullable=True),
        sa.Column("error_category", sa.Text(), nullable=True),
        sa.Column("fallback_used", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("cache_hit", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "error_category IS NULL OR error_category IN ('auth', 'rate_limited', 'timeout', 'unavailable', 'invalid_request', 'model_unavailable', 'context_length', 'output_validation', 'refusal', 'budget_exceeded', 'unknown')",
            name=op.f("ck_model_invocations_error_category"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_invocations")),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_model_invocations_created_at", "model_invocations", ["created_at"], unique=False, schema=SCHEMA
    )
    op.create_index(
        "ix_model_invocations_provider_model",
        "model_invocations",
        ["provider", "model_id"],
        unique=False,
        schema=SCHEMA,
    )
    op.create_table(
        "processing_jobs",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("job_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column(
            "input_scope",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("config_version", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("total_items", sa.Integer(), nullable=True),
        sa.Column("processed_items", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("succeeded_items", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("failed_items", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_retries", sa.Integer(), server_default=sa.text("3"), nullable=False),
        sa.Column(
            "checkpoint",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'queued', 'running', 'partially_completed', 'completed', 'failed', 'cancelled')",
            name=op.f("ck_processing_jobs_status"),
        ),
        sa.CheckConstraint(
            "max_retries >= 0 AND retry_count >= 0", name=op.f("ck_processing_jobs_retry_bounds")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_processing_jobs")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_processing_jobs_idempotency_key")),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_processing_jobs_claim",
        "processing_jobs",
        ["status", "next_run_at", "created_at"],
        unique=False,
        schema=SCHEMA,
    )
    op.create_index(
        "ix_processing_jobs_created_at", "processing_jobs", ["created_at"], unique=False, schema=SCHEMA
    )
    op.create_index(
        "ix_processing_jobs_job_type", "processing_jobs", ["job_type"], unique=False, schema=SCHEMA
    )
    op.create_table(
        "user_profiles",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_profiles")),
        schema=SCHEMA,
    )
    op.create_table(
        "processing_items",
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("item_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed', 'skipped')", name=op.f("ck_processing_items_status")
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            [f"{SCHEMA}.processing_jobs.id"],
            name=op.f("fk_processing_items_job_id_processing_jobs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("job_id", "item_key", name=op.f("pk_processing_items")),
        schema=SCHEMA,
    )
    op.create_table(
        "residents",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("dataset_id", sa.UUID(), nullable=False),
        sa.Column("source_system", sa.Text(), nullable=False),
        sa.Column("source_patient_id", sa.Text(), nullable=False),
        sa.Column("birth_date", sa.Date(), nullable=True),
        sa.Column("deceased_date", sa.Date(), nullable=True),
        sa.Column("administrative_sex", sa.Text(), nullable=True),
        sa.Column("preferred_language", sa.Text(), nullable=True),
        sa.Column("district_code", sa.Text(), nullable=True),
        sa.Column("source_version", sa.Text(), nullable=True),
        sa.Column("synthetic_status", sa.Text(), server_default="synthetic", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "synthetic_status IN ('synthetic', 'simulated')", name=op.f("ck_residents_synthetic_status")
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"], [f"{SCHEMA}.datasets.id"], name=op.f("fk_residents_dataset_id_datasets")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_residents")),
        sa.UniqueConstraint("source_system", "source_patient_id", name="uq_residents_source"),
        schema=SCHEMA,
    )
    op.create_index("ix_residents_birth_date", "residents", ["birth_date"], unique=False, schema=SCHEMA)
    op.create_index("ix_residents_district_code", "residents", ["district_code"], unique=False, schema=SCHEMA)
    op.create_table(
        "clinical_records",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("resident_id", sa.UUID(), nullable=False),
        sa.Column("resource_type", sa.Text(), nullable=False),
        sa.Column("source_resource_id", sa.Text(), nullable=False),
        sa.Column("source_system", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("code_system", sa.Text(), nullable=True),
        sa.Column("code", sa.Text(), nullable=True),
        sa.Column("display", sa.Text(), nullable=True),
        sa.Column("normalized_code_system", sa.Text(), nullable=True),
        sa.Column("normalized_code", sa.Text(), nullable=True),
        sa.Column("normalized_display", sa.Text(), nullable=True),
        sa.Column("effective_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("effective_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("value_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source_reference", sa.Text(), nullable=False),
        sa.Column("synthetic_status", sa.Text(), server_default="synthetic", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "synthetic_status IN ('synthetic', 'simulated')",
            name=op.f("ck_clinical_records_synthetic_status"),
        ),
        sa.ForeignKeyConstraint(
            ["resident_id"],
            [f"{SCHEMA}.residents.id"],
            name=op.f("fk_clinical_records_resident_id_residents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_clinical_records")),
        sa.UniqueConstraint(
            "resident_id", "resource_type", "source_resource_id", name="uq_clinical_records_source"
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_clinical_records_code_system_code",
        "clinical_records",
        ["code_system", "code"],
        unique=False,
        schema=SCHEMA,
    )
    op.create_index(
        "ix_clinical_records_resident_category_code",
        "clinical_records",
        ["resident_id", "category", "code"],
        unique=False,
        schema=SCHEMA,
    )
    op.create_table(
        "documents",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("resident_id", sa.UUID(), nullable=False),
        sa.Column("source_resource_id", sa.Text(), nullable=True),
        sa.Column("document_type", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("source_system", sa.Text(), server_default="anticipa", nullable=False),
        sa.Column("generation_method", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("model_id", sa.Text(), nullable=True),
        sa.Column("synthetic_status", sa.Text(), server_default="synthetic", nullable=False),
        sa.Column("processing_status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("created_on", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "registered_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "synthetic_status IN ('synthetic', 'simulated')", name=op.f("ck_documents_synthetic_status")
        ),
        sa.ForeignKeyConstraint(
            ["resident_id"],
            [f"{SCHEMA}.residents.id"],
            name=op.f("fk_documents_resident_id_residents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint("resident_id", "content_hash", name="uq_documents_resident_content"),
        schema=SCHEMA,
    )
    op.create_table(
        "evidence_facts",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("resident_id", sa.UUID(), nullable=False),
        sa.Column("subject_type", sa.Text(), server_default="unknown", nullable=False),
        sa.Column("subject_person_id", sa.UUID(), nullable=True),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("fact_type", sa.Text(), nullable=False),
        sa.Column(
            "value",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("assertion_status", sa.Text(), nullable=False),
        sa.Column("temporal_status", sa.Text(), server_default="unknown", nullable=False),
        sa.Column("relationship_type", sa.Text(), nullable=True),
        sa.Column("extraction_confidence", sa.Text(), nullable=True),
        sa.Column("uncertainty_note", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("validation_status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("review_status", sa.Text(), server_default="not_required", nullable=False),
        sa.Column("extraction_method", sa.Text(), nullable=False),
        sa.Column("model_provider", sa.Text(), nullable=True),
        sa.Column("model_id", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("invocation_id", sa.UUID(), nullable=True),
        sa.Column("synthetic_status", sa.Text(), server_default="synthetic", nullable=False),
        sa.Column(
            "extracted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "assertion_status IN ('positive', 'negative', 'uncertain', 'conditional')",
            name=op.f("ck_evidence_facts_assertion_status"),
        ),
        sa.CheckConstraint(
            "extraction_confidence IS NULL OR extraction_confidence IN ('low', 'medium', 'high')",
            name=op.f("ck_evidence_facts_extraction_confidence"),
        ),
        sa.CheckConstraint(
            "subject_type IN ('resident', 'family_member', 'other', 'unknown')",
            name=op.f("ck_evidence_facts_subject_type"),
        ),
        sa.CheckConstraint(
            "synthetic_status IN ('synthetic', 'simulated')", name=op.f("ck_evidence_facts_synthetic_status")
        ),
        sa.CheckConstraint(
            "temporal_status IN ('current', 'historical', 'planned', 'unknown')",
            name=op.f("ck_evidence_facts_temporal_status"),
        ),
        sa.CheckConstraint(
            "validation_status IN ('pending', 'schema_valid', 'source_verified', 'accepted', 'requires_review', 'rejected', 'failed')",
            name=op.f("ck_evidence_facts_validation_status"),
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name=op.f("ck_evidence_facts_confidence_range"),
        ),
        sa.ForeignKeyConstraint(
            ["resident_id"],
            [f"{SCHEMA}.residents.id"],
            name=op.f("fk_evidence_facts_resident_id_residents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["subject_person_id"],
            [f"{SCHEMA}.residents.id"],
            name=op.f("fk_evidence_facts_subject_person_id_residents"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence_facts")),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_evidence_facts_resident_category",
        "evidence_facts",
        ["resident_id", "category"],
        unique=False,
        schema=SCHEMA,
    )
    op.create_index(
        "ix_evidence_facts_validation_status",
        "evidence_facts",
        ["validation_status"],
        unique=False,
        schema=SCHEMA,
    )
    op.create_table(
        "family_edges",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("resident_id", sa.UUID(), nullable=False),
        sa.Column("related_resident_id", sa.UUID(), nullable=True),
        sa.Column("related_person_source_id", sa.Text(), nullable=True),
        sa.Column("relationship_type", sa.Text(), nullable=False),
        sa.Column("relationship_direction", sa.Text(), nullable=False),
        sa.Column("source_resource_id", sa.Text(), nullable=True),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("confidence_status", sa.Text(), server_default="unknown", nullable=False),
        sa.Column("synthetic_status", sa.Text(), server_default="synthetic", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "confidence_status IN ('verified', 'uncertain', 'unknown')",
            name=op.f("ck_family_edges_confidence_status"),
        ),
        sa.CheckConstraint(
            "synthetic_status IN ('synthetic', 'simulated')", name=op.f("ck_family_edges_synthetic_status")
        ),
        sa.CheckConstraint("resident_id <> related_resident_id", name=op.f("ck_family_edges_no_self_edge")),
        sa.ForeignKeyConstraint(
            ["related_resident_id"],
            [f"{SCHEMA}.residents.id"],
            name=op.f("fk_family_edges_related_resident_id_residents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["resident_id"],
            [f"{SCHEMA}.residents.id"],
            name=op.f("fk_family_edges_resident_id_residents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_family_edges")),
        sa.UniqueConstraint(
            "resident_id", "related_resident_id", "relationship_type", name="uq_family_edges_pair"
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_family_edges_related_resident_id",
        "family_edges",
        ["related_resident_id"],
        unique=False,
        schema=SCHEMA,
    )
    op.create_index(
        "uq_family_edges_external_relative",
        "family_edges",
        ["resident_id", "related_person_source_id", "relationship_type"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("related_resident_id IS NULL"),
    )
    op.create_table(
        "document_chunks",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.Column("embedding_model", sa.Text(), nullable=True),
        sa.Column("synthetic_status", sa.Text(), server_default="synthetic", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "synthetic_status IN ('synthetic', 'simulated')", name=op.f("ck_document_chunks_synthetic_status")
        ),
        sa.CheckConstraint("end_offset >= start_offset", name=op.f("ck_document_chunks_offsets")),
        sa.ForeignKeyConstraint(
            ["document_id"],
            [f"{SCHEMA}.documents.id"],
            name=op.f("fk_document_chunks_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_chunks")),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_document_chunks_document_index"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_document_chunks_embedding_hnsw",
        "document_chunks",
        ["embedding"],
        unique=False,
        schema=SCHEMA,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_with={"m": 16, "ef_construction": 64},
    )
    op.create_table(
        "evidence_sources",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("evidence_fact_id", sa.UUID(), nullable=False),
        sa.Column("source_type", sa.Text(), server_default="document", nullable=False),
        sa.Column("source_resource_type", sa.Text(), nullable=True),
        sa.Column("source_resource_id", sa.Text(), nullable=True),
        sa.Column("document_id", sa.UUID(), nullable=True),
        sa.Column("source_field_path", sa.Text(), nullable=True),
        sa.Column("source_quote", sa.Text(), nullable=True),
        sa.Column("source_start_offset", sa.Integer(), nullable=True),
        sa.Column("source_end_offset", sa.Integer(), nullable=True),
        sa.Column("source_content_hash", sa.Text(), nullable=True),
        sa.Column("source_reference", sa.Text(), nullable=True),
        sa.Column("verification_status", sa.Text(), nullable=False),
        sa.Column("location_ambiguous", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("all_spans", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("synthetic_status", sa.Text(), server_default="synthetic", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "synthetic_status IN ('synthetic', 'simulated')",
            name=op.f("ck_evidence_sources_synthetic_status"),
        ),
        sa.CheckConstraint(
            "verification_status IN ('verified_exact', 'verified_normalized', 'span_mismatch', 'quote_not_found', 'source_missing', 'verification_error')",
            name=op.f("ck_evidence_sources_verification_status"),
        ),
        sa.CheckConstraint(
            "source_end_offset IS NULL OR source_end_offset >= source_start_offset",
            name=op.f("ck_evidence_sources_offsets"),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            [f"{SCHEMA}.documents.id"],
            name=op.f("fk_evidence_sources_document_id_documents"),
        ),
        sa.ForeignKeyConstraint(
            ["evidence_fact_id"],
            [f"{SCHEMA}.evidence_facts.id"],
            name=op.f("fk_evidence_sources_evidence_fact_id_evidence_facts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence_sources")),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_evidence_sources_document_id", "evidence_sources", ["document_id"], unique=False, schema=SCHEMA
    )
    op.create_index(
        "ix_evidence_sources_evidence_fact_id",
        "evidence_sources",
        ["evidence_fact_id"],
        unique=False,
        schema=SCHEMA,
    )
    op.execute(AUDIT_IMMUTABLE_SQL)
    op.execute(HARDEN_SQL)


def downgrade() -> None:
    op.drop_index("ix_evidence_sources_evidence_fact_id", table_name="evidence_sources", schema=SCHEMA)
    op.drop_index("ix_evidence_sources_document_id", table_name="evidence_sources", schema=SCHEMA)
    op.drop_table("evidence_sources", schema=SCHEMA)
    op.drop_index(
        "ix_document_chunks_embedding_hnsw",
        table_name="document_chunks",
        schema=SCHEMA,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_with={"m": 16, "ef_construction": 64},
    )
    op.drop_table("document_chunks", schema=SCHEMA)
    op.drop_index(
        "uq_family_edges_external_relative",
        table_name="family_edges",
        schema=SCHEMA,
        postgresql_where=sa.text("related_resident_id IS NULL"),
    )
    op.drop_index("ix_family_edges_related_resident_id", table_name="family_edges", schema=SCHEMA)
    op.drop_table("family_edges", schema=SCHEMA)
    op.drop_index("ix_evidence_facts_validation_status", table_name="evidence_facts", schema=SCHEMA)
    op.drop_index("ix_evidence_facts_resident_category", table_name="evidence_facts", schema=SCHEMA)
    op.drop_table("evidence_facts", schema=SCHEMA)
    op.drop_table("documents", schema=SCHEMA)
    op.drop_index("ix_clinical_records_resident_category_code", table_name="clinical_records", schema=SCHEMA)
    op.drop_index("ix_clinical_records_code_system_code", table_name="clinical_records", schema=SCHEMA)
    op.drop_table("clinical_records", schema=SCHEMA)
    op.drop_index("ix_residents_district_code", table_name="residents", schema=SCHEMA)
    op.drop_index("ix_residents_birth_date", table_name="residents", schema=SCHEMA)
    op.drop_table("residents", schema=SCHEMA)
    op.drop_table("processing_items", schema=SCHEMA)
    op.drop_table("user_profiles", schema=SCHEMA)
    op.drop_index("ix_processing_jobs_job_type", table_name="processing_jobs", schema=SCHEMA)
    op.drop_index("ix_processing_jobs_created_at", table_name="processing_jobs", schema=SCHEMA)
    op.drop_index("ix_processing_jobs_claim", table_name="processing_jobs", schema=SCHEMA)
    op.drop_table("processing_jobs", schema=SCHEMA)
    op.drop_index("ix_model_invocations_provider_model", table_name="model_invocations", schema=SCHEMA)
    op.drop_index("ix_model_invocations_created_at", table_name="model_invocations", schema=SCHEMA)
    op.drop_table("model_invocations", schema=SCHEMA)
    op.drop_table("datasets", schema=SCHEMA)
    op.drop_index("ix_audit_events_resource", table_name="audit_events", schema=SCHEMA)
    op.drop_index("ix_audit_events_occurred_at", table_name="audit_events", schema=SCHEMA)
    op.drop_index("ix_audit_events_actor_id", table_name="audit_events", schema=SCHEMA)
    op.drop_table("audit_events", schema=SCHEMA)
    op.execute(
        f"DROP FUNCTION IF EXISTS {SCHEMA}.audit_events_immutable()"
    )  # schema, extension and alembic_version stay
