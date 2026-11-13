"""guideline engine v2: guideline-level evaluation (08 rules schema v2), review status, conflicts, citations

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-11

Additive changes on top of 0002 (which is already applied to the dev database, so it is never edited):
 - guideline_versions: care_activity, priority, source_version, clinical_review_status, selectors,
   decision_policy, sources,
   version_metadata (everything an 08-format guideline carries beyond its rules);
 - rule_versions: rule_id, rule_type, precedence (the rule's own id, kind and priority number);
 - eligibility_evaluations: one evaluation is now one resident x one guideline version; `rule_version_id`
   becomes the decisive rule (NULL when eligible); reason_code, review_status, conflicting_information,
   source_references
   added;
 - care_gap_cases.rule_version_id becomes nullable for the same reason;
 - the immutability guards are replaced so the new columns of an approved version are frozen too.
Existing rows (if any) are backfilled conservatively; pre-v2 evaluations remain readable and untouched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S = "anticipa"
CLINICAL_REVIEW = ("pending_clinical_review", "clinically_reviewed")
RULE_TYPES = ("prerequisite", "inclusion", "exclusion", "temporal")
PRIORITIES = ("routine", "elevated", "high")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _jsonb(name: str, default: str) -> sa.Column:
    return sa.Column(
        name,
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text(f"'{default}'::jsonb"),
        nullable=False,
    )


VERSION_CONTENT = (
    "guideline_id, version, source_reference, source_publication_date, effective_from, effective_until, "
    "population_scope, disclaimer, change_summary, care_activity, priority, source_version, "
    "clinical_review_status, selectors, decision_policy, sources, version_metadata, content_hash, created_by, "  # noqa: E501
    "created_at, submitted_by, submitted_at, approved_by, approved_at"
)
RULE_CONTENT = (
    "guideline_version_id, rule_key, rule_id, rule_type, precedence, version, name, description, care_activity, "  # noqa: E501
    "priority, rule_language, rule_definition, content_hash, input_schema_version, output_schema_version, created_at"  # noqa: E501
)
OLD_VERSION_CONTENT = (
    "guideline_id, version, source_reference, source_publication_date, effective_from, effective_until, "
    "population_scope, disclaimer, change_summary, content_hash, created_by, created_at, submitted_by, "
    "submitted_at, approved_by, approved_at"
)
OLD_RULE_CONTENT = (
    "guideline_version_id, rule_key, version, name, description, care_activity, priority, rule_language, "
    "rule_definition, content_hash, input_schema_version, output_schema_version, created_at"
)


def _row(prefix: str, cols: str) -> str:
    return "(" + ", ".join(f"{prefix}.{c.strip()}" for c in cols.split(",")) + ")"


def _guard_functions(version_cols: str, rule_cols: str) -> None:
    op.execute(
        f"""CREATE OR REPLACE FUNCTION {S}.guard_guideline_version() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            IF OLD.review_status IN ('approved', 'retired') THEN
              RAISE EXCEPTION 'approved guideline versions cannot be deleted'
              USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN OLD;
          END IF;
          IF OLD.review_status = 'retired' THEN
            RAISE EXCEPTION 'retired guideline versions are immutable'
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          IF OLD.review_status = 'approved' THEN
            IF NEW.review_status NOT IN ('approved', 'retired')
               OR {_row("NEW", version_cols)} IS DISTINCT FROM {_row("OLD", version_cols)} THEN
              RAISE EXCEPTION 'approved guideline versions are immutable (only retirement is allowed)'
              USING ERRCODE = 'integrity_constraint_violation';
            END IF;
          END IF;
          RETURN NEW;
        END $$"""  # noqa: S608 - the interpolated names are module constants, never user input
    )
    op.execute(
        f"""CREATE OR REPLACE FUNCTION {S}.guard_rule_version() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE parent_status text;
        BEGIN
          IF TG_OP = 'INSERT' THEN
            SELECT review_status INTO parent_status FROM {S}.guideline_versions
            WHERE id = NEW.guideline_version_id;
            IF parent_status IS DISTINCT FROM 'draft' THEN
              RAISE EXCEPTION 'rules can only be added to a draft guideline version'
              USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
          END IF;
          IF TG_OP = 'DELETE' THEN
            IF OLD.approval_status IN ('approved', 'retired') THEN
              RAISE EXCEPTION 'approved rule versions cannot be deleted'
              USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN OLD;
          END IF;
          IF OLD.approval_status = 'retired' THEN
            RAISE EXCEPTION 'retired rule versions are immutable'
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          IF OLD.approval_status = 'approved' THEN
            IF NEW.approval_status NOT IN ('approved', 'retired')
               OR {_row("NEW", rule_cols)} IS DISTINCT FROM {_row("OLD", rule_cols)} THEN
              RAISE EXCEPTION 'approved rule versions are immutable (only retirement is allowed)'
              USING ERRCODE = 'integrity_constraint_violation';
            END IF;
          END IF;
          RETURN NEW;
        END $$"""  # noqa: S608 - the interpolated names are module constants, never user input
    )


def upgrade() -> None:
    # --- guideline_versions
    op.execute(
        f"ALTER TABLE {S}.guideline_versions DISABLE TRIGGER guideline_versions_guard"
    )  # backfill touches approved rows
    op.execute(f"ALTER TABLE {S}.rule_versions DISABLE TRIGGER rule_versions_guard")
    op.add_column("guideline_versions", sa.Column("care_activity", sa.Text(), nullable=True), schema=S)
    op.execute(
        "UPDATE anticipa.guideline_versions v SET care_activity = replace(g.guideline_key, '-', '_') "
        "FROM anticipa.guidelines g WHERE g.id = v.guideline_id"
    )
    op.alter_column("guideline_versions", "care_activity", nullable=False, schema=S)
    op.add_column(
        "guideline_versions",
        sa.Column("priority", sa.Text(), server_default="routine", nullable=False),
        schema=S,
    )
    op.add_column("guideline_versions", sa.Column("source_version", sa.Text(), nullable=True), schema=S)
    op.add_column(
        "guideline_versions",
        sa.Column(
            "clinical_review_status", sa.Text(), server_default="pending_clinical_review", nullable=False
        ),
        schema=S,
    )
    for name, default in (
        ("selectors", "{}"),
        ("decision_policy", "{}"),
        ("sources", "[]"),
        ("version_metadata", "{}"),
    ):
        op.add_column("guideline_versions", _jsonb(name, default), schema=S)
    op.create_check_constraint(
        op.f("ck_guideline_versions_priority"), "guideline_versions", _in("priority", PRIORITIES), schema=S
    )
    op.create_check_constraint(
        op.f("ck_guideline_versions_clinical_review_status"),
        "guideline_versions",
        _in("clinical_review_status", CLINICAL_REVIEW),
        schema=S,
    )

    # --- rule_versions
    op.add_column("rule_versions", sa.Column("rule_id", sa.Text(), nullable=True), schema=S)
    op.add_column("rule_versions", sa.Column("rule_type", sa.Text(), nullable=True), schema=S)
    op.add_column("rule_versions", sa.Column("precedence", sa.Integer(), nullable=True), schema=S)
    op.execute(
        "UPDATE anticipa.rule_versions SET rule_id = rule_key, rule_type = 'inclusion', precedence = 0"
    )  # noqa: E501
    op.execute(f"ALTER TABLE {S}.rule_versions ENABLE TRIGGER rule_versions_guard")
    op.execute(f"ALTER TABLE {S}.guideline_versions ENABLE TRIGGER guideline_versions_guard")
    for col in ("rule_id", "rule_type", "precedence"):
        op.alter_column("rule_versions", col, nullable=False, schema=S)
    op.alter_column("rule_versions", "rule_language", server_default="anticipa-08-v1", schema=S)
    op.create_check_constraint(
        op.f("ck_rule_versions_rule_type"), "rule_versions", _in("rule_type", RULE_TYPES), schema=S
    )

    # --- eligibility_evaluations: resident x guideline version; rule_version_id = decisive rule (nullable)
    op.alter_column("eligibility_evaluations", "rule_version_id", nullable=True, schema=S)
    op.add_column("eligibility_evaluations", sa.Column("reason_code", sa.Text(), nullable=True), schema=S)
    op.add_column(
        "eligibility_evaluations",
        sa.Column("review_status", sa.Text(), server_default="pending_clinical_review", nullable=False),
        schema=S,
    )
    op.add_column("eligibility_evaluations", _jsonb("conflicting_information", "[]"), schema=S)
    op.add_column("eligibility_evaluations", _jsonb("source_references", "[]"), schema=S)
    op.create_check_constraint(
        op.f("ck_eligibility_evaluations_review_status"),
        "eligibility_evaluations",
        _in("review_status", CLINICAL_REVIEW),
        schema=S,
    )
    op.drop_index("ix_eligibility_evaluations_latest", table_name="eligibility_evaluations", schema=S)
    op.drop_index("ix_eligibility_evaluations_outcome", table_name="eligibility_evaluations", schema=S)
    op.create_index(
        "ix_eligibility_evaluations_latest",
        "eligibility_evaluations",
        ["guideline_version_id", "resident_id", "evaluated_at"],
        schema=S,
    )
    op.create_index(
        "ix_eligibility_evaluations_outcome",
        "eligibility_evaluations",
        ["guideline_version_id", "outcome"],
        schema=S,
    )

    # --- care_gap_cases: the decisive rule is optional (an eligible resident has none)
    op.alter_column("care_gap_cases", "rule_version_id", nullable=True, schema=S)

    _guard_functions(VERSION_CONTENT, RULE_CONTENT)


def downgrade() -> None:
    _guard_functions(OLD_VERSION_CONTENT, OLD_RULE_CONTENT)
    op.alter_column("care_gap_cases", "rule_version_id", nullable=False, schema=S)
    op.drop_index("ix_eligibility_evaluations_outcome", table_name="eligibility_evaluations", schema=S)
    op.drop_index("ix_eligibility_evaluations_latest", table_name="eligibility_evaluations", schema=S)
    op.create_index(
        "ix_eligibility_evaluations_outcome",
        "eligibility_evaluations",
        ["rule_version_id", "outcome"],
        schema=S,
    )
    op.create_index(
        "ix_eligibility_evaluations_latest",
        "eligibility_evaluations",
        ["rule_version_id", "resident_id", "evaluated_at"],
        schema=S,
    )
    op.drop_constraint(op.f("ck_eligibility_evaluations_review_status"), "eligibility_evaluations", schema=S)
    for col in ("source_references", "conflicting_information", "review_status", "reason_code"):
        op.drop_column("eligibility_evaluations", col, schema=S)
    op.alter_column("eligibility_evaluations", "rule_version_id", nullable=False, schema=S)
    op.drop_constraint(op.f("ck_rule_versions_rule_type"), "rule_versions", schema=S)
    op.alter_column("rule_versions", "rule_language", server_default="anticipa-rule-v1", schema=S)
    for col in ("precedence", "rule_type", "rule_id"):
        op.drop_column("rule_versions", col, schema=S)
    op.drop_constraint(op.f("ck_guideline_versions_clinical_review_status"), "guideline_versions", schema=S)
    op.drop_constraint(op.f("ck_guideline_versions_priority"), "guideline_versions", schema=S)
    for col in (
        "version_metadata",
        "sources",
        "decision_policy",
        "selectors",
        "clinical_review_status",
        "source_version",
        "priority",
        "care_activity",
    ):
        op.drop_column("guideline_versions", col, schema=S)
