"""capacity planning: clinics, services, slots, planning runs, planned allocations, unallocated cases

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-08

Hand-written (single linear chain: 0001 foundation, 0002 guidelines + care gaps, 0003 planning). All tables live in
schema `anticipa`, have Row Level Security enabled with no policies, and have privileges revoked from the Supabase
`anon`/`authenticated` roles when those exist (ADR-058). `care_gap_case_id` / `resident_id` are deliberately plain UUID
columns (no FK): the care-gap tables belong to another workstream and a plan snapshot must outlive its case.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "anticipa"  # a variable so the migration can be smoke-tested in a throw-away schema
TABLES = ("clinics", "clinic_services", "capacity_slots", "planning_runs", "planned_allocations", "unallocated_cases")
REASON_CODES = (
    "invalid_case_data", "service_not_offered", "outside_travel_eligibility", "no_capacity_in_horizon",
    "capacity_exhausted", "resident_time_conflict",
)  # fmt: skip


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _id():
    return sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def _synthetic():
    return sa.Column("synthetic_status", sa.Text(), server_default="simulated", nullable=False)


def _created():
    return sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False)


def _synthetic_check(table: str):
    return sa.CheckConstraint(_in("synthetic_status", ("synthetic", "simulated")), name=op.f(f"ck_{table}_synthetic_status"))


def _json(name: str):
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False)


def _lock_down(table: str) -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.{table} ENABLE ROW LEVEL SECURITY")
    for role in ("anon", "authenticated"):
        op.execute(
            f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
            f"EXECUTE 'REVOKE ALL ON TABLE {SCHEMA}.{table} FROM {role}'; END IF; END $$"
        )


def upgrade() -> None:
    op.create_table(
        "clinics",
        _id(),
        sa.Column("clinic_key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("district_code", sa.Text(), nullable=False),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("timezone", sa.Text(), server_default="UTC", nullable=False),
        sa.Column("operating_status", sa.Text(), server_default="open", nullable=False),
        _synthetic(),
        _created(),
        sa.CheckConstraint(_in("operating_status", ("open", "closed")), name=op.f("ck_clinics_operating_status")),
        _synthetic_check("clinics"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_clinics")),
        sa.UniqueConstraint("clinic_key", name=op.f("uq_clinics_clinic_key")),
        schema=SCHEMA,
    )
    op.create_index("ix_clinics_district_code", "clinics", ["district_code"], schema=SCHEMA)

    op.create_table(
        "clinic_services",
        _id(),
        sa.Column("clinic_id", sa.UUID(), nullable=False),
        sa.Column("service_key", sa.Text(), nullable=False),
        sa.Column("service_name", sa.Text(), nullable=False),
        sa.Column("appointment_duration_minutes", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        _synthetic(),
        _created(),
        sa.CheckConstraint("appointment_duration_minutes > 0", name=op.f("ck_clinic_services_duration_positive")),
        _synthetic_check("clinic_services"),
        sa.ForeignKeyConstraint(["clinic_id"], [f"{SCHEMA}.clinics.id"], name=op.f("fk_clinic_services_clinic_id_clinics"),
                                ondelete="CASCADE"),  # fmt: skip
        sa.PrimaryKeyConstraint("id", name=op.f("pk_clinic_services")),
        sa.UniqueConstraint("clinic_id", "service_key", name="uq_clinic_services_clinic_service"),
        schema=SCHEMA,
    )

    op.create_table(
        "capacity_slots",
        _id(),
        sa.Column("clinic_id", sa.UUID(), nullable=False),
        sa.Column("clinic_service_id", sa.UUID(), nullable=False),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_capacity", sa.Integer(), nullable=False),
        sa.Column("reserved_capacity", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("slot_status", sa.Text(), server_default="open", nullable=False),
        sa.Column("source_type", sa.Text(), server_default="manual", nullable=False),
        _synthetic(),
        _created(),
        sa.CheckConstraint("end_time > start_time", name=op.f("ck_capacity_slots_time_order")),
        sa.CheckConstraint("available_capacity >= 0", name=op.f("ck_capacity_slots_available_non_negative")),
        sa.CheckConstraint("reserved_capacity >= 0", name=op.f("ck_capacity_slots_reserved_non_negative")),
        sa.CheckConstraint("reserved_capacity <= available_capacity", name=op.f("ck_capacity_slots_reserved_within_available")),
        sa.CheckConstraint(_in("slot_status", ("open", "closed")), name=op.f("ck_capacity_slots_slot_status")),
        sa.CheckConstraint(_in("source_type", ("synthetic_generator", "manual", "api")), name=op.f("ck_capacity_slots_source_type")),
        _synthetic_check("capacity_slots"),
        sa.ForeignKeyConstraint(["clinic_id"], [f"{SCHEMA}.clinics.id"], name=op.f("fk_capacity_slots_clinic_id_clinics"),
                                ondelete="CASCADE"),  # fmt: skip
        sa.ForeignKeyConstraint(["clinic_service_id"], [f"{SCHEMA}.clinic_services.id"],
                                name=op.f("fk_capacity_slots_clinic_service_id_clinic_services"), ondelete="CASCADE"),  # fmt: skip
        sa.PrimaryKeyConstraint("id", name=op.f("pk_capacity_slots")),
        sa.UniqueConstraint("clinic_service_id", "start_time", name="uq_capacity_slots_service_start"),
        schema=SCHEMA,
    )
    op.create_index("ix_capacity_slots_start_time", "capacity_slots", ["start_time"], schema=SCHEMA)
    op.create_index("ix_capacity_slots_clinic_id", "capacity_slots", ["clinic_id"], schema=SCHEMA)

    op.create_table(
        "planning_runs",
        _id(),
        sa.Column("planning_period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planning_period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("configuration_version", sa.Text(), nullable=False),
        sa.Column("algorithm_version", sa.Text(), nullable=False),
        sa.Column("input_snapshot_hash", sa.Text(), nullable=True),
        _json("config"),
        _json("solver_parameters"),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        _json("case_filters"),
        _json("metrics"),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        _synthetic(),
        _created(),
        sa.CheckConstraint(_in("status", ("queued", "running", "completed", "failed", "cancelled")), name=op.f("ck_planning_runs_status")),
        sa.CheckConstraint("planning_period_end > planning_period_start", name=op.f("ck_planning_runs_period_order")),
        _synthetic_check("planning_runs"),
        sa.ForeignKeyConstraint(["job_id"], [f"{SCHEMA}.processing_jobs.id"], name=op.f("fk_planning_runs_job_id_processing_jobs"),
                                ondelete="SET NULL"),  # fmt: skip
        sa.PrimaryKeyConstraint("id", name=op.f("pk_planning_runs")),
        schema=SCHEMA,
    )
    op.create_index("ix_planning_runs_created_at", "planning_runs", ["created_at"], schema=SCHEMA)
    op.create_index("ix_planning_runs_job_id", "planning_runs", ["job_id"], schema=SCHEMA)

    op.create_table(
        "planned_allocations",
        _id(),
        sa.Column("planning_run_id", sa.UUID(), nullable=False),
        sa.Column("care_gap_case_id", sa.UUID(), nullable=False),
        sa.Column("resident_id", sa.UUID(), nullable=False),
        sa.Column("clinic_id", sa.UUID(), nullable=False),
        sa.Column("clinic_service_id", sa.UUID(), nullable=False),
        sa.Column("capacity_slot_id", sa.UUID(), nullable=False),
        sa.Column("clinic_key", sa.Text(), nullable=False),
        sa.Column("service_key", sa.Text(), nullable=False),
        sa.Column("priority", sa.Text(), nullable=True),
        sa.Column("district_code", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("proposed_start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("proposed_end_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("allocation_status", sa.Text(), server_default="proposed", nullable=False),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_by", sa.UUID(), nullable=True),
        _synthetic(),
        _created(),
        sa.CheckConstraint(_in("allocation_status", ("proposed", "confirmed", "superseded")),
                           name=op.f("ck_planned_allocations_allocation_status")),  # fmt: skip
        _synthetic_check("planned_allocations"),
        sa.ForeignKeyConstraint(["planning_run_id"], [f"{SCHEMA}.planning_runs.id"],
                                name=op.f("fk_planned_allocations_planning_run_id_planning_runs"), ondelete="CASCADE"),  # fmt: skip
        sa.ForeignKeyConstraint(["clinic_id"], [f"{SCHEMA}.clinics.id"], name=op.f("fk_planned_allocations_clinic_id_clinics")),
        sa.ForeignKeyConstraint(["clinic_service_id"], [f"{SCHEMA}.clinic_services.id"],
                                name=op.f("fk_planned_allocations_clinic_service_id_clinic_services")),  # fmt: skip
        sa.ForeignKeyConstraint(["capacity_slot_id"], [f"{SCHEMA}.capacity_slots.id"],
                                name=op.f("fk_planned_allocations_capacity_slot_id_capacity_slots")),  # fmt: skip
        sa.PrimaryKeyConstraint("id", name=op.f("pk_planned_allocations")),
        sa.UniqueConstraint("planning_run_id", "care_gap_case_id", name="uq_planned_allocations_run_case"),
        schema=SCHEMA,
    )
    op.create_index("ix_planned_allocations_run_status", "planned_allocations", ["planning_run_id", "allocation_status"], schema=SCHEMA)
    op.create_index("ix_planned_allocations_slot", "planned_allocations", ["capacity_slot_id"], schema=SCHEMA)
    op.create_index("ix_planned_allocations_case", "planned_allocations", ["care_gap_case_id"], schema=SCHEMA)
    op.create_index("uq_planned_allocations_confirmed_case", "planned_allocations", ["care_gap_case_id"], unique=True,
                    schema=SCHEMA, postgresql_where=sa.text("allocation_status = 'confirmed'"))  # fmt: skip

    op.create_table(
        "unallocated_cases",
        _id(),
        sa.Column("planning_run_id", sa.UUID(), nullable=False),
        sa.Column("care_gap_case_id", sa.UUID(), nullable=False),
        sa.Column("resident_id", sa.UUID(), nullable=False),
        sa.Column("service_key", sa.Text(), nullable=True),
        sa.Column("priority", sa.Text(), nullable=True),
        sa.Column("district_code", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        _json("details"),
        _synthetic(),
        _created(),
        sa.CheckConstraint(_in("reason_code", REASON_CODES), name=op.f("ck_unallocated_cases_reason_code")),
        _synthetic_check("unallocated_cases"),
        sa.ForeignKeyConstraint(["planning_run_id"], [f"{SCHEMA}.planning_runs.id"],
                                name=op.f("fk_unallocated_cases_planning_run_id_planning_runs"), ondelete="CASCADE"),  # fmt: skip
        sa.PrimaryKeyConstraint("id", name=op.f("pk_unallocated_cases")),
        sa.UniqueConstraint("planning_run_id", "care_gap_case_id", name="uq_unallocated_cases_run_case"),
        schema=SCHEMA,
    )
    op.create_index("ix_unallocated_cases_run_reason", "unallocated_cases", ["planning_run_id", "reason_code"], schema=SCHEMA)

    for table in TABLES:
        _lock_down(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table, schema=SCHEMA)  # indexes and constraints go with the table
