"""consent records: permit/deny decisions for outreach and AI processing, and the `consent_denied` reason

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-13

Hand-written. One table, `consent_records` (append-only ledger: resident_id, scope, status, channel,
language, source, effective_from/to, recorded_by, provenance, synthetic_status), in schema `anticipa` with
Row Level Security enabled (no policies) and privileges revoked from `anon`/`authenticated` when they exist
(ADR-058, same as 0004/0008). The table is deliberately not in the judge read list (migration 0005):
consent decisions are only served through the API.

It also widens the CHECK on `unallocated_cases.reason_code` with `consent_denied` (a planned case whose
resident opted out of every outreach channel is left unallocated with that reason instead of getting a
seat). The downgrade deletes those rows before restoring the old CHECK. FHIR Consent mapping: see
anticipa/consent/models.py. ADR-070.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "anticipa"
TABLE = "consent_records"
SCOPES = ("outreach", "data_use_ai_processing")
STATUSES = ("permit", "deny")
CHANNELS = ("sms", "whatsapp", "phone_call", "email")
LANGUAGES = ("en", "hi", "ar")
SOURCES = ("resident_request", "coordinator_entry", "synthetic_seed")
SYNTHETIC_STATUSES = ("synthetic", "simulated")
OLD_REASON_CODES = (
    "invalid_case_data", "service_not_offered", "outside_travel_eligibility", "no_capacity_in_horizon",
    "capacity_exhausted", "resident_time_conflict",
)  # fmt: skip
NEW_REASON_CODES = (*OLD_REASON_CODES, "consent_denied")
REASON_CHECK = "ck_unallocated_cases_reason_code"


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("resident_id", sa.UUID(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "effective_from", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recorded_by", sa.UUID(), nullable=True),
        sa.Column(
            "provenance",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("synthetic_status", sa.Text(), server_default="simulated", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(_in("scope", SCOPES), name=op.f("ck_consent_records_scope")),
        sa.CheckConstraint(_in("status", STATUSES), name=op.f("ck_consent_records_status")),
        sa.CheckConstraint(_in("channel", CHANNELS), name=op.f("ck_consent_records_channel")),
        sa.CheckConstraint(_in("language", LANGUAGES), name=op.f("ck_consent_records_language")),
        sa.CheckConstraint(_in("source", SOURCES), name=op.f("ck_consent_records_source")),
        sa.CheckConstraint(
            _in("synthetic_status", SYNTHETIC_STATUSES), name=op.f("ck_consent_records_synthetic_status")
        ),
        sa.CheckConstraint(
            "scope = 'outreach' OR channel IS NULL", name=op.f("ck_consent_records_channel_only_for_outreach")
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name=op.f("ck_consent_records_period_order"),
        ),
        sa.ForeignKeyConstraint(
            ["resident_id"],
            [f"{SCHEMA}.residents.id"],
            name=op.f("fk_consent_records_resident_id_residents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consent_records")),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_consent_records_resident",
        TABLE,
        ["resident_id", "scope", "channel", "effective_from"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_consent_records_deny",
        TABLE,
        ["resident_id"],
        schema=SCHEMA,
        postgresql_where=sa.text("status = 'deny'"),
    )
    op.execute(f"ALTER TABLE {SCHEMA}.{TABLE} ENABLE ROW LEVEL SECURITY")
    for role in ("anon", "authenticated"):
        op.execute(
            f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "  # noqa: S608
            f"EXECUTE 'REVOKE ALL ON TABLE {SCHEMA}.{TABLE} FROM {role}'; END IF; END $$"  # (constants)
        )
    _set_reason_check(NEW_REASON_CODES)


def downgrade() -> None:
    op.execute(f"DELETE FROM {SCHEMA}.unallocated_cases WHERE reason_code = 'consent_denied'")  # noqa: S608  (constants)
    _set_reason_check(OLD_REASON_CODES)
    op.drop_table(TABLE, schema=SCHEMA)


def _set_reason_check(codes: Sequence[str]) -> None:
    op.drop_constraint(op.f(REASON_CHECK), "unallocated_cases", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        op.f(REASON_CHECK), "unallocated_cases", _in("reason_code", codes), schema=SCHEMA
    )
