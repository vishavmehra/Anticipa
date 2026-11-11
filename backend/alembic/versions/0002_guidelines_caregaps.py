"""guidelines, rule versions, eligibility evaluations, care-gap cases and case status history

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-07

Hand-written. Besides the tables it installs the integrity guards the specs ask for (08 §4.2, 04 §10.4, 04 §11.3):
approved/retired guideline and rule versions are immutable (except approved -> retired), rules cannot be added to a
non-draft version, evaluations and case history are append-only (UPDATE rejected; DELETE stays possible so a dataset
reset can cascade). Every table gets RLS enabled and anon/authenticated privileges revoked (ADR-058).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S = "anticipa"
TABLES = (
    "guidelines", "guideline_versions", "rule_versions", "eligibility_evaluations", "care_gap_cases",
    "case_status_history",
)  # fmt: skip

OUTCOMES = (
    "eligible", "not_eligible", "already_completed", "insufficient_evidence", "requires_clinical_review",
    "temporarily_excluded", "evaluation_error",
)  # fmt: skip
OUTCOME_CLASSES = ("eligible", "ineligible", "indeterminate", "error")
REVIEW_STATUSES = ("draft", "in_review", "approved", "retired")
CASE_STATUSES = (
    "identified", "pending_review", "approved_for_planning", "awaiting_capacity", "planned", "outreach_pending",
    "outreach_completed", "appointment_scheduled", "awaiting_completion_evidence", "completed", "closed_not_completed",
    "escalated", "cancelled",
)  # fmt: skip
TERMINAL = ("completed", "closed_not_completed", "cancelled")
PRIORITIES = ("routine", "elevated", "high")
SYNTHETIC = ("synthetic", "simulated")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _uuid_pk() -> sa.Column:
    return sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def _ts(name: str, nullable: bool = False) -> sa.Column:
    if nullable:
        return sa.Column(name, sa.DateTime(timezone=True), nullable=True)
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False)


def _jsonb(name: str, default: str) -> sa.Column:
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text(f"'{default}'::jsonb"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "guidelines",
        _uuid_pk(),
        sa.Column("guideline_key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("topic", sa.Text(), nullable=True),
        sa.Column("authoritative_source", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_guidelines")),
        sa.UniqueConstraint("guideline_key", name=op.f("uq_guidelines_guideline_key")),
        schema=S,
    )
    op.create_table(
        "guideline_versions",
        _uuid_pk(),
        sa.Column("guideline_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("review_status", sa.Text(), server_default="draft", nullable=False),
        sa.Column("source_reference", sa.Text(), nullable=True),
        sa.Column("source_publication_date", sa.Date(), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_until", sa.Date(), nullable=True),
        _jsonb("population_scope", "{}"),
        sa.Column("disclaimer", sa.Text(), nullable=True),
        sa.Column("change_summary", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        _ts("created_at"),
        sa.Column("submitted_by", sa.UUID(), nullable=True),
        _ts("submitted_at", True),
        sa.Column("approved_by", sa.UUID(), nullable=True),
        _ts("approved_at", True),
        sa.Column("retired_by", sa.UUID(), nullable=True),
        _ts("retired_at", True),
        sa.Column("retirement_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(_in("review_status", REVIEW_STATUSES), name=op.f("ck_guideline_versions_review_status")),
        sa.CheckConstraint(
            "effective_until IS NULL OR effective_until >= effective_from",
            name=op.f("ck_guideline_versions_effective_window"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_guideline_versions_version_positive")),
        sa.ForeignKeyConstraint(
            ["guideline_id"], [f"{S}.guidelines.id"], name=op.f("fk_guideline_versions_guideline_id_guidelines")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_guideline_versions")),
        sa.UniqueConstraint("guideline_id", "version", name="uq_guideline_versions_version"),
        schema=S,
    )
    op.create_index("ix_guideline_versions_review_status", "guideline_versions", ["review_status"], schema=S)
    op.create_table(
        "rule_versions",
        _uuid_pk(),
        sa.Column("guideline_version_id", sa.UUID(), nullable=False),
        sa.Column("rule_key", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("care_activity", sa.Text(), nullable=False),
        sa.Column("priority", sa.Text(), server_default="routine", nullable=False),
        sa.Column("rule_language", sa.Text(), server_default="anticipa-rule-v1", nullable=False),
        sa.Column("rule_definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("input_schema_version", sa.Text(), server_default="1", nullable=False),
        sa.Column("output_schema_version", sa.Text(), server_default="1", nullable=False),
        sa.Column("approval_status", sa.Text(), server_default="draft", nullable=False),
        _ts("created_at"),
        sa.CheckConstraint(_in("approval_status", REVIEW_STATUSES), name=op.f("ck_rule_versions_approval_status")),
        sa.ForeignKeyConstraint(
            ["guideline_version_id"],
            [f"{S}.guideline_versions.id"],
            name=op.f("fk_rule_versions_guideline_version_id_guideline_versions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rule_versions")),
        sa.UniqueConstraint("guideline_version_id", "rule_key", name="uq_rule_versions_key"),
        schema=S,
    )
    op.create_index("ix_rule_versions_approval_status", "rule_versions", ["approval_status"], schema=S)
    op.create_index("ix_rule_versions_care_activity", "rule_versions", ["care_activity"], schema=S)
    op.create_table(
        "eligibility_evaluations",
        _uuid_pk(),
        sa.Column("resident_id", sa.UUID(), nullable=False),
        sa.Column("guideline_version_id", sa.UUID(), nullable=False),
        sa.Column("rule_version_id", sa.UUID(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("outcome_class", sa.Text(), nullable=False),
        sa.Column("explanation", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        _jsonb("evidence_references", "[]"),
        _jsonb("missing_information", "[]"),
        _jsonb("exclusion_reasons", "[]"),
        sa.Column("input_hash", sa.Text(), nullable=False),
        sa.Column("evaluation_date", sa.Date(), nullable=False),
        _ts("evaluated_at"),
        sa.Column("execution_version", sa.Text(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("synthetic_status", sa.Text(), server_default="synthetic", nullable=False),
        sa.CheckConstraint(_in("outcome", OUTCOMES), name=op.f("ck_eligibility_evaluations_outcome")),
        sa.CheckConstraint(_in("outcome_class", OUTCOME_CLASSES), name=op.f("ck_eligibility_evaluations_outcome_class")),
        sa.CheckConstraint(_in("synthetic_status", SYNTHETIC), name=op.f("ck_eligibility_evaluations_synthetic_status")),
        sa.ForeignKeyConstraint(
            ["resident_id"], [f"{S}.residents.id"], name=op.f("fk_eligibility_evaluations_resident_id_residents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["guideline_version_id"], [f"{S}.guideline_versions.id"],
            name=op.f("fk_eligibility_evaluations_guideline_version_id_guideline_versions"),
        ),
        sa.ForeignKeyConstraint(
            ["rule_version_id"], [f"{S}.rule_versions.id"],
            name=op.f("fk_eligibility_evaluations_rule_version_id_rule_versions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eligibility_evaluations")),
        schema=S,
    )
    op.create_index(
        "ix_eligibility_evaluations_latest", "eligibility_evaluations",
        ["rule_version_id", "resident_id", "evaluated_at"], schema=S,
    )  # fmt: skip
    op.create_index(
        "ix_eligibility_evaluations_resident_id", "eligibility_evaluations", ["resident_id", "evaluated_at"], schema=S
    )
    op.create_index("ix_eligibility_evaluations_outcome", "eligibility_evaluations", ["rule_version_id", "outcome"], schema=S)
    op.create_index("ix_eligibility_evaluations_job_id", "eligibility_evaluations", ["job_id"], schema=S)
    op.create_table(
        "care_gap_cases",
        _uuid_pk(),
        sa.Column("resident_id", sa.UUID(), nullable=False),
        sa.Column("evaluation_id", sa.UUID(), nullable=False),
        sa.Column("guideline_version_id", sa.UUID(), nullable=False),
        sa.Column("rule_version_id", sa.UUID(), nullable=False),
        sa.Column("care_activity", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="identified", nullable=False),
        sa.Column("priority", sa.Text(), server_default="routine", nullable=False),
        sa.Column("source_outcome", sa.Text(), nullable=False),
        sa.Column("assigned_coordinator_id", sa.UUID(), nullable=True),
        sa.Column("planning_run_id", sa.UUID(), nullable=True),
        _ts("opened_at"),
        _ts("updated_at"),
        _ts("closed_at", True),
        sa.Column("closure_reason", sa.Text(), nullable=True),
        sa.Column("synthetic_status", sa.Text(), server_default="synthetic", nullable=False),
        sa.CheckConstraint(_in("status", CASE_STATUSES), name=op.f("ck_care_gap_cases_status")),
        sa.CheckConstraint(_in("priority", PRIORITIES), name=op.f("ck_care_gap_cases_priority")),
        sa.CheckConstraint(_in("synthetic_status", SYNTHETIC), name=op.f("ck_care_gap_cases_synthetic_status")),
        sa.ForeignKeyConstraint(
            ["resident_id"], [f"{S}.residents.id"], name=op.f("fk_care_gap_cases_resident_id_residents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id"], [f"{S}.eligibility_evaluations.id"],
            name=op.f("fk_care_gap_cases_evaluation_id_eligibility_evaluations"),
        ),
        sa.ForeignKeyConstraint(
            ["guideline_version_id"], [f"{S}.guideline_versions.id"],
            name=op.f("fk_care_gap_cases_guideline_version_id_guideline_versions"),
        ),
        sa.ForeignKeyConstraint(
            ["rule_version_id"], [f"{S}.rule_versions.id"], name=op.f("fk_care_gap_cases_rule_version_id_rule_versions")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_care_gap_cases")),
        sa.UniqueConstraint("evaluation_id", name="uq_care_gap_cases_evaluation"),
        schema=S,
    )
    op.create_index(
        "uq_care_gap_cases_open", "care_gap_cases", ["resident_id", "care_activity"], unique=True, schema=S,
        postgresql_where=sa.text("status NOT IN (" + ", ".join(repr(s) for s in TERMINAL) + ")"),
    )  # fmt: skip
    op.create_index("ix_care_gap_cases_status", "care_gap_cases", ["status"], schema=S)
    op.create_index("ix_care_gap_cases_resident_id", "care_gap_cases", ["resident_id"], schema=S)
    op.create_index("ix_care_gap_cases_activity_status", "care_gap_cases", ["care_activity", "status"], schema=S)
    op.create_index("ix_care_gap_cases_assigned", "care_gap_cases", ["assigned_coordinator_id"], schema=S)
    op.create_index("ix_care_gap_cases_opened_at", "care_gap_cases", ["opened_at"], schema=S)
    op.create_table(
        "case_status_history",
        _uuid_pk(),
        sa.Column("case_id", sa.UUID(), nullable=False),
        sa.Column("from_status", sa.Text(), nullable=True),
        sa.Column("to_status", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.Column("actor_role", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        _jsonb("details", "{}"),
        _ts("occurred_at"),
        sa.Column("synthetic_status", sa.Text(), server_default="synthetic", nullable=False),
        sa.CheckConstraint(
            f"from_status IS NULL OR {_in('from_status', CASE_STATUSES)}", name=op.f("ck_case_status_history_from_status")
        ),
        sa.CheckConstraint(_in("to_status", CASE_STATUSES), name=op.f("ck_case_status_history_to_status")),
        sa.CheckConstraint(_in("synthetic_status", SYNTHETIC), name=op.f("ck_case_status_history_synthetic_status")),
        sa.ForeignKeyConstraint(
            ["case_id"], [f"{S}.care_gap_cases.id"], name=op.f("fk_case_status_history_case_id_care_gap_cases"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_case_status_history")),
        schema=S,
    )
    op.create_index("ix_case_status_history_case", "case_status_history", ["case_id", "occurred_at"], schema=S)

    _install_guards()
    for table in TABLES:  # ADR-058: RLS on, no Data-API privileges
        op.execute(f"ALTER TABLE {S}.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""DO $$ BEGIN
              IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'anon') THEN REVOKE ALL ON TABLE {S}.{table} FROM anon; END IF;
              IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'authenticated') THEN REVOKE ALL ON TABLE {S}.{table} FROM authenticated; END IF;
            END $$"""
        )


def _install_guards() -> None:
    version_content = (
        "guideline_id, version, source_reference, source_publication_date, effective_from, effective_until, "
        "population_scope, disclaimer, change_summary, content_hash, created_by, created_at, submitted_by, "
        "submitted_at, approved_by, approved_at"
    )
    rule_content = (
        "guideline_version_id, rule_key, version, name, description, care_activity, priority, rule_language, "
        "rule_definition, content_hash, input_schema_version, output_schema_version, created_at"
    )

    def row(prefix: str, cols: str) -> str:
        return "(" + ", ".join(f"{prefix}.{c.strip()}" for c in cols.split(",")) + ")"

    op.execute(
        f"""CREATE FUNCTION {S}.guard_guideline_version() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            IF OLD.review_status IN ('approved', 'retired') THEN
              RAISE EXCEPTION 'approved guideline versions cannot be deleted' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN OLD;
          END IF;
          IF OLD.review_status = 'retired' THEN
            RAISE EXCEPTION 'retired guideline versions are immutable' USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          IF OLD.review_status = 'approved' THEN
            IF NEW.review_status NOT IN ('approved', 'retired')
               OR {row('NEW', version_content)} IS DISTINCT FROM {row('OLD', version_content)} THEN
              RAISE EXCEPTION 'approved guideline versions are immutable (only retirement is allowed)' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
          END IF;
          RETURN NEW;
        END $$"""
    )
    op.execute(
        f"""CREATE TRIGGER guideline_versions_guard BEFORE UPDATE OR DELETE ON {S}.guideline_versions
        FOR EACH ROW EXECUTE FUNCTION {S}.guard_guideline_version()"""
    )
    op.execute(
        f"""CREATE FUNCTION {S}.guard_rule_version() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE parent_status text;
        BEGIN
          IF TG_OP = 'INSERT' THEN
            SELECT review_status INTO parent_status FROM {S}.guideline_versions WHERE id = NEW.guideline_version_id;
            IF parent_status IS DISTINCT FROM 'draft' THEN
              RAISE EXCEPTION 'rules can only be added to a draft guideline version' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
          END IF;
          IF TG_OP = 'DELETE' THEN
            IF OLD.approval_status IN ('approved', 'retired') THEN
              RAISE EXCEPTION 'approved rule versions cannot be deleted' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN OLD;
          END IF;
          IF OLD.approval_status = 'retired' THEN
            RAISE EXCEPTION 'retired rule versions are immutable' USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          IF OLD.approval_status = 'approved' THEN
            IF NEW.approval_status NOT IN ('approved', 'retired')
               OR {row('NEW', rule_content)} IS DISTINCT FROM {row('OLD', rule_content)} THEN
              RAISE EXCEPTION 'approved rule versions are immutable (only retirement is allowed)' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
          END IF;
          RETURN NEW;
        END $$"""
    )
    op.execute(
        f"""CREATE TRIGGER rule_versions_guard BEFORE INSERT OR UPDATE OR DELETE ON {S}.rule_versions
        FOR EACH ROW EXECUTE FUNCTION {S}.guard_rule_version()"""
    )
    op.execute(
        f"""CREATE FUNCTION {S}.reject_update() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION '% rows are append-only', TG_TABLE_NAME USING ERRCODE = 'integrity_constraint_violation';
        END $$"""
    )
    for table in ("eligibility_evaluations", "case_status_history"):
        op.execute(
            f"""CREATE TRIGGER {table}_append_only BEFORE UPDATE ON {S}.{table}
            FOR EACH ROW EXECUTE FUNCTION {S}.reject_update()"""
        )


def downgrade() -> None:
    for table in ("eligibility_evaluations", "case_status_history"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {S}.{table}")
    op.execute(f"DROP TRIGGER IF EXISTS rule_versions_guard ON {S}.rule_versions")
    op.execute(f"DROP TRIGGER IF EXISTS guideline_versions_guard ON {S}.guideline_versions")
    for fn in ("reject_update", "guard_rule_version", "guard_guideline_version"):
        op.execute(f"DROP FUNCTION IF EXISTS {S}.{fn}()")
    op.drop_table("case_status_history", schema=S)
    op.drop_table("care_gap_cases", schema=S)
    op.drop_table("eligibility_evaluations", schema=S)
    op.drop_table("rule_versions", schema=S)
    op.drop_table("guideline_versions", schema=S)
    op.drop_table("guidelines", schema=S)
