"""analytics snapshots: precomputed aggregates for the Command Center, funnel, geography and programme endpoints

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-12

Hand-written. One table, `analytics_snapshots` (key, payload jsonb, computed_at, duration_ms, refreshed_by) in schema
`anticipa`, Row Level Security enabled (no policies) and privileges revoked from `anon`/`authenticated` when they exist
(ADR-058, same as 0001/0004/0007). Aggregate counts only. Chain: 0006 guidelines v2, 0007 evaluation, 0008 analytics.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "anticipa"
TABLE = "analytics_snapshots"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("duration_ms", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("refreshed_by", sa.Text(), server_default="cli", nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_analytics_snapshots")),
        schema=SCHEMA,
    )
    op.execute(f"ALTER TABLE {SCHEMA}.{TABLE} ENABLE ROW LEVEL SECURITY")
    for role in ("anon", "authenticated"):
        op.execute(
            f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
            f"EXECUTE 'REVOKE ALL ON TABLE {SCHEMA}.{TABLE} FROM {role}'; END IF; END $$"  # noqa: S608  (constants)
        )


def downgrade() -> None:
    op.drop_table(TABLE, schema=SCHEMA)
