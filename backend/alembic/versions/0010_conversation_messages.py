# ruff: noqa: E501, S608
"""resident conversation: the simulated two way thread per care gap case (ADR-071)

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-13

Hand-written. One table, `conversation_messages`, in schema `anticipa` with Row Level Security enabled (no policies) and
privileges revoked from `anon`/`authenticated` when they exist (ADR-058, same as 0004/0008/0009). Every row can only be
`simulated` (CHECK). Deliberately not in the judge read list (migration 0005): the thread is served through the API only.
Chain: 0008 analytics, 0009 consent records, 0010 conversation.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "anticipa"
TABLE = "conversation_messages"
ROLES = ("resident", "assistant")
LANGUAGES = ("en", "hi", "ar")
SOURCES = ("resident_input", "template", "model")
ESCALATION_STATES = ("escalated", "already_escalated", "case_closed")
ESCALATION_REASONS = ("emergency", "medical_question", "eligibility_dispute", "clinician_flag", "unsafe_reply")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("care_gap_case_id", sa.UUID(), nullable=False),
        sa.Column("resident_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("intent", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("escalation_state", sa.Text(), nullable=True),
        sa.Column("escalation_reason", sa.Text(), nullable=True),
        sa.Column("proposal", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("model_id", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("invocation_id", sa.Text(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("synthetic_status", sa.Text(), server_default="simulated", nullable=False),
        sa.CheckConstraint(_in("role", ROLES), name=op.f("ck_conversation_messages_role")),
        sa.CheckConstraint(_in("language", LANGUAGES), name=op.f("ck_conversation_messages_language")),
        sa.CheckConstraint(_in("source", SOURCES), name=op.f("ck_conversation_messages_source")),
        sa.CheckConstraint(
            f"escalation_state IS NULL OR {_in('escalation_state', ESCALATION_STATES)}",
            name=op.f("ck_conversation_messages_escalation_state"),
        ),
        sa.CheckConstraint(
            f"escalation_reason IS NULL OR {_in('escalation_reason', ESCALATION_REASONS)}",
            name=op.f("ck_conversation_messages_escalation_reason"),
        ),
        sa.CheckConstraint(_in("synthetic_status", ("simulated",)), name=op.f("ck_conversation_messages_synthetic_status")),
        sa.ForeignKeyConstraint(
            ["care_gap_case_id"], [f"{SCHEMA}.care_gap_cases.id"],
            name=op.f("fk_conversation_messages_care_gap_case_id_care_gap_cases"), ondelete="CASCADE",
        ),  # fmt: skip
        sa.ForeignKeyConstraint(
            ["resident_id"], [f"{SCHEMA}.residents.id"],
            name=op.f("fk_conversation_messages_resident_id_residents"), ondelete="CASCADE",
        ),  # fmt: skip
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversation_messages")),
        schema=SCHEMA,
    )
    op.create_index("ix_conversation_messages_case", TABLE, ["care_gap_case_id", "created_at"], schema=SCHEMA)
    op.execute(f"ALTER TABLE {SCHEMA}.{TABLE} ENABLE ROW LEVEL SECURITY")
    for role in ("anon", "authenticated"):
        op.execute(
            f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
            f"EXECUTE 'REVOKE ALL ON TABLE {SCHEMA}.{TABLE} FROM {role}'; END IF; END $$"  # noqa: S608  (constants)
        )


def downgrade() -> None:
    op.drop_table(TABLE, schema=SCHEMA)
