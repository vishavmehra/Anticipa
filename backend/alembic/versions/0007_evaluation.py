"""evaluation runs: one aggregate row per evaluation run (Docs/09 §10.4)

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-11

Hand-written. One table, `evaluation_runs`, in schema `anticipa` with Row Level Security enabled (no policies) and all
privileges revoked from the Supabase `anon`/`authenticated` roles when they exist (ADR-058, same as 0001/0004). The
table holds AGGREGATE metrics, gate results and configuration only: never ground-truth labels, document text or
per-case expected values (ADR-008). Chain: 0005 judge read-only, 0006 guidelines v2, 0007 evaluation.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "anticipa"
TABLE = "evaluation_runs"
RUN_STATUSES = ("running", "complete", "partial", "failed")
RUN_TYPES = ("smoke", "dev", "test_final", "scale")
SPLITS = ("dev", "test", "all", "smoke")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _jsonb(name: str, default: str) -> sa.Column:
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text(f"'{default}'::jsonb"), nullable=False)


def _ts(name: str, *, now: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.text("now()") if now else None, nullable=not now)


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="running", nullable=False),
        sa.Column("run_type", sa.Text(), server_default="dev", nullable=False),
        sa.Column("dataset_version", sa.Text(), nullable=False),
        sa.Column("split", sa.Text(), nullable=False),
        sa.Column("cohort_id", sa.Text(), nullable=True),
        sa.Column("ground_truth_id", sa.Text(), nullable=True),
        sa.Column("metrics_digest", sa.Text(), nullable=True),
        _jsonb("config", "{}"),
        _jsonb("prompt_versions", "[]"),
        _jsonb("model_versions", "[]"),
        _jsonb("metrics", "{}"),
        _jsonb("gates", "[]"),
        _jsonb("gate_summary", "{}"),
        _jsonb("details", "{}"),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        _ts("started_at", now=True),
        _ts("completed_at"),
        _ts("created_at", now=True),
        sa.Column("synthetic_status", sa.Text(), server_default="synthetic", nullable=False),
        sa.CheckConstraint(_in("status", RUN_STATUSES), name=op.f("ck_evaluation_runs_status")),
        sa.CheckConstraint(_in("run_type", RUN_TYPES), name=op.f("ck_evaluation_runs_run_type")),
        sa.CheckConstraint(_in("split", SPLITS), name=op.f("ck_evaluation_runs_split")),
        sa.CheckConstraint(_in("synthetic_status", ("synthetic", "simulated")), name=op.f("ck_evaluation_runs_synthetic_status")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evaluation_runs")),
        sa.UniqueConstraint("run_id", name=op.f("uq_evaluation_runs_run_id")),
        schema=SCHEMA,
    )
    op.create_index("ix_evaluation_runs_created_at", TABLE, ["created_at"], schema=SCHEMA)
    op.create_index("ix_evaluation_runs_dataset_version", TABLE, ["dataset_version"], schema=SCHEMA)
    op.execute(f"ALTER TABLE {SCHEMA}.{TABLE} ENABLE ROW LEVEL SECURITY")
    for role in ("anon", "authenticated"):
        op.execute(
            f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
            f"EXECUTE 'REVOKE ALL ON TABLE {SCHEMA}.{TABLE} FROM {role}'; END IF; END $$"  # noqa: S608  (constants)
        )


def downgrade() -> None:
    op.drop_table(TABLE, schema=SCHEMA)
