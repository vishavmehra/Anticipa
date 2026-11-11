"""engagement and loop closure: communication templates/events, resident responses, appointments, completion evidence

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-11

Hand-written (single linear chain: 0001 foundation, 0002 guidelines + care gaps, 0003 planning, 0004 engagement +
closure). All tables live in schema `anticipa`, have Row Level Security enabled with no policies, and have privileges
revoked from the Supabase `anon`/`authenticated` roles when those exist (ADR-058). Communication events, responses and
appointments can only ever be `simulated` (CHECK): no real message or booking exists anywhere in the MVP.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "anticipa"  # a variable so the migration can be smoke-tested in a throw-away schema
TABLES = ("communication_templates", "communication_events", "resident_responses", "appointments", "completion_evidence")
LANGUAGES = ("en", "hi", "ar")
CHANNELS = ("sms", "whatsapp", "phone_call", "email")
DELIVERY_STATES = ("queued", "sent", "delivered", "failed")
RESPONSE_TYPES = ("accept", "decline", "question", "no_response")
APPOINTMENT_STATUSES = ("proposed", "simulated_confirmed", "completed", "no_show", "cancelled")
SOURCE_TYPES = ("manual", "simulation")
EVIDENCE_SOURCES = ("simulated_procedure_record", "clinical_record", "document", "manual_attestation")
VERIFICATION_STATUSES = ("pending", "verified", "rejected")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _id():
    return sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def _simulated():
    return sa.Column("synthetic_status", sa.Text(), server_default="simulated", nullable=False)


def _created(name: str = "created_at"):
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False)


def _ts(name: str):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def _simulated_check(table: str):
    return sa.CheckConstraint(_in("synthetic_status", ("simulated",)), name=op.f(f"ck_{table}_synthetic_status"))


def _fk(table: str, column: str, target: str, *, cascade: bool = False):
    ref_table = target.split(".")[0]
    return sa.ForeignKeyConstraint(
        [column], [f"{SCHEMA}.{target}"], name=op.f(f"fk_{table}_{column}_{ref_table}"),
        ondelete="CASCADE" if cascade else None,
    )  # fmt: skip


def _lock_down(table: str) -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.{table} ENABLE ROW LEVEL SECURITY")
    for role in ("anon", "authenticated"):
        op.execute(
            f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
            f"EXECUTE 'REVOKE ALL ON TABLE {SCHEMA}.{table} FROM {role}'; END IF; END $$"  # noqa: S608  (constants)
        )


def upgrade() -> None:
    op.create_table(
        "communication_templates",
        _id(),
        sa.Column("template_key", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("approved", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        _created(),
        sa.CheckConstraint(_in("language", LANGUAGES), name=op.f("ck_communication_templates_language")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_communication_templates_version_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_communication_templates")),
        sa.UniqueConstraint("template_key", "language", "version", name="uq_communication_templates_key_language_version"),
        schema=SCHEMA,
    )

    op.create_table(
        "communication_events",
        _id(),
        sa.Column("care_gap_case_id", sa.UUID(), nullable=False),
        sa.Column("resident_id", sa.UUID(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("template_id", sa.UUID(), nullable=True),
        sa.Column("template_key", sa.Text(), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("delivery_state", sa.Text(), server_default="queued", nullable=False),
        sa.Column("source_type", sa.Text(), server_default="manual", nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        _ts("sent_at"),
        _ts("delivered_at"),
        _ts("response_received_at"),
        sa.Column("created_by", sa.UUID(), nullable=True),
        _created(),
        _simulated(),
        sa.CheckConstraint(_in("channel", CHANNELS), name=op.f("ck_communication_events_channel")),
        sa.CheckConstraint(_in("language", LANGUAGES), name=op.f("ck_communication_events_language")),
        sa.CheckConstraint(_in("delivery_state", DELIVERY_STATES), name=op.f("ck_communication_events_delivery_state")),
        sa.CheckConstraint(_in("source_type", SOURCE_TYPES), name=op.f("ck_communication_events_source_type")),
        _simulated_check("communication_events"),
        _fk("communication_events", "care_gap_case_id", "care_gap_cases.id", cascade=True),
        _fk("communication_events", "resident_id", "residents.id", cascade=True),
        _fk("communication_events", "template_id", "communication_templates.id"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_communication_events")),
        schema=SCHEMA,
    )
    op.create_index("ix_communication_events_case", "communication_events", ["care_gap_case_id", "created_at"], schema=SCHEMA)
    op.create_index("ix_communication_events_resident_id", "communication_events", ["resident_id"], schema=SCHEMA)
    op.create_index("ix_communication_events_state", "communication_events", ["delivery_state"], schema=SCHEMA)

    op.create_table(
        "resident_responses",
        _id(),
        sa.Column("communication_event_id", sa.UUID(), nullable=False),
        sa.Column("care_gap_case_id", sa.UUID(), nullable=False),
        sa.Column("resident_id", sa.UUID(), nullable=False),
        sa.Column("response_type", sa.Text(), nullable=False),
        sa.Column("response_value", sa.Text(), nullable=True),
        _created("received_at"),
        sa.Column("source_reference", sa.Text(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        _created(),
        _simulated(),
        sa.CheckConstraint(_in("response_type", RESPONSE_TYPES), name=op.f("ck_resident_responses_response_type")),
        _simulated_check("resident_responses"),
        _fk("resident_responses", "communication_event_id", "communication_events.id", cascade=True),
        _fk("resident_responses", "care_gap_case_id", "care_gap_cases.id", cascade=True),
        _fk("resident_responses", "resident_id", "residents.id", cascade=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resident_responses")),
        schema=SCHEMA,
    )
    op.create_index("ix_resident_responses_event", "resident_responses", ["communication_event_id"], schema=SCHEMA)
    op.create_index("ix_resident_responses_case", "resident_responses", ["care_gap_case_id"], schema=SCHEMA)

    op.create_table(
        "appointments",
        _id(),
        sa.Column("care_gap_case_id", sa.UUID(), nullable=False),
        sa.Column("resident_id", sa.UUID(), nullable=False),
        sa.Column("clinic_id", sa.UUID(), nullable=False),
        sa.Column("clinic_service_id", sa.UUID(), nullable=False),
        sa.Column("capacity_slot_id", sa.UUID(), nullable=False),
        sa.Column("planned_allocation_id", sa.UUID(), nullable=False),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("appointment_status", sa.Text(), server_default="proposed", nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("source_type", sa.Text(), server_default="manual", nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        _created(),
        _created("updated_at"),
        _simulated(),
        sa.CheckConstraint(_in("appointment_status", APPOINTMENT_STATUSES), name=op.f("ck_appointments_appointment_status")),
        sa.CheckConstraint(_in("source_type", SOURCE_TYPES), name=op.f("ck_appointments_source_type")),
        sa.CheckConstraint("end_time > start_time", name=op.f("ck_appointments_time_order")),
        _simulated_check("appointments"),
        _fk("appointments", "care_gap_case_id", "care_gap_cases.id", cascade=True),
        _fk("appointments", "resident_id", "residents.id", cascade=True),
        _fk("appointments", "clinic_id", "clinics.id"),
        _fk("appointments", "clinic_service_id", "clinic_services.id"),
        _fk("appointments", "capacity_slot_id", "capacity_slots.id"),
        _fk("appointments", "planned_allocation_id", "planned_allocations.id"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_appointments")),
        sa.UniqueConstraint("planned_allocation_id", name="uq_appointments_planned_allocation"),
        schema=SCHEMA,
    )
    op.create_index("uq_appointments_open_case", "appointments", ["care_gap_case_id"], unique=True, schema=SCHEMA,
                    postgresql_where=sa.text("appointment_status IN ('proposed', 'simulated_confirmed')"))  # fmt: skip
    op.create_index("ix_appointments_case", "appointments", ["care_gap_case_id"], schema=SCHEMA)
    op.create_index("ix_appointments_status", "appointments", ["appointment_status"], schema=SCHEMA)
    op.create_index("ix_appointments_start_time", "appointments", ["start_time"], schema=SCHEMA)

    op.create_table(
        "completion_evidence",
        _id(),
        sa.Column("care_gap_case_id", sa.UUID(), nullable=False),
        sa.Column("activity", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_resource_type", sa.Text(), nullable=True),
        sa.Column("source_resource_id", sa.Text(), nullable=True),
        sa.Column("document_id", sa.UUID(), nullable=True),
        sa.Column("evidence_reference", sa.Text(), nullable=False),
        sa.Column("verification_status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("verification_method", sa.Text(), nullable=True),
        sa.Column("reviewed_by", sa.UUID(), nullable=True),
        sa.Column("reviewer_note", sa.Text(), nullable=True),
        _ts("verified_at"),
        sa.Column("created_by", sa.UUID(), nullable=True),
        _created(),
        _simulated(),
        sa.CheckConstraint(_in("source_type", EVIDENCE_SOURCES), name=op.f("ck_completion_evidence_source_type")),
        sa.CheckConstraint(_in("verification_status", VERIFICATION_STATUSES), name=op.f("ck_completion_evidence_verification_status")),
        sa.CheckConstraint(_in("synthetic_status", ("synthetic", "simulated")), name=op.f("ck_completion_evidence_synthetic_status")),
        sa.CheckConstraint("(verification_status = 'verified') = (verified_at IS NOT NULL)",
                           name=op.f("ck_completion_evidence_verified_at_matches_status")),  # fmt: skip
        _fk("completion_evidence", "care_gap_case_id", "care_gap_cases.id", cascade=True),
        _fk("completion_evidence", "document_id", "documents.id"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_completion_evidence")),
        schema=SCHEMA,
    )
    op.create_index("ix_completion_evidence_case", "completion_evidence", ["care_gap_case_id", "created_at"], schema=SCHEMA)
    op.create_index("ix_completion_evidence_status", "completion_evidence", ["verification_status"], schema=SCHEMA)

    for table in TABLES:
        _lock_down(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table, schema=SCHEMA)  # indexes and constraints go with the table
