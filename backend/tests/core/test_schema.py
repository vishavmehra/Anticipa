"""ADR-058 hardening and contract-17 schema facts, checked on the migrated test database `anticipa_test`."""

# ruff: noqa: E501, S608  (fixed test SQL; DB_SCHEMA is a constant)
from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

import anticipa.models  # noqa: F401  (registers every table)
from anticipa.api.health import migration_head
from anticipa.config import DB_SCHEMA
from anticipa.db import Base


def test_database_is_at_the_migration_head(db_session):
    version = db_session.execute(text(f"SELECT version_num FROM {DB_SCHEMA}.alembic_version")).scalar_one()
    assert version == migration_head()


# Tables created by migration 0001. Other workstreams own the drift check of their own tables (see handoff).
FOUNDATION_TABLES = {
    "audit_events", "user_profiles", "processing_jobs", "processing_items", "model_invocations", "datasets",
    "residents", "clinical_records", "family_edges", "documents", "document_chunks", "evidence_facts",
    "evidence_sources",
}  # fmt: skip


def _foundation_only(obj, name, type_, reflected, compare_to) -> bool:
    table = obj if type_ == "table" else getattr(obj, "table", None)
    return getattr(table, "name", None) in FOUNDATION_TABLES


def test_foundation_models_match_migration_0001(db_session):
    """Every table/column/index/constraint declared in the foundation models exists in the migrated schema."""
    ctx = MigrationContext.configure(
        db_session.connection(),
        opts={
            "version_table_schema": DB_SCHEMA,
            "include_schemas": True,
            "compare_type": False,
            "include_object": _foundation_only,
        },
    )
    diff = [d for d in compare_metadata(ctx, Base.metadata) if d[0] != "remove_index"]
    # Alembic reports every schema-qualified FK as remove+add (reflection drops the schema qualifier when it is
    # on the search_path); a same-name pair is therefore not drift.
    names = {k: {d[1].name for d in diff if d[0] == k} for k in ("add_fk", "remove_fk")}
    both = names["add_fk"] & names["remove_fk"]
    diff = [d for d in diff if not (d[0] in names and d[1].name in both)]
    assert not diff, diff


def test_every_table_has_row_level_security(db_session):
    rows = db_session.execute(
        text(
            "SELECT relname, relrowsecurity FROM pg_class WHERE relnamespace = CAST(:s AS regnamespace) AND relkind = 'r'"
        ),
        {"s": DB_SCHEMA},
    ).all()
    assert len(rows) >= 13
    assert [name for name, rls in rows if not rls] == []


def test_vector_lives_in_extensions_and_search_path_resolves_it(db_session):
    schema = db_session.execute(
        text(
            "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace WHERE extname = 'vector'"
        )
    ).scalar_one()
    assert schema == "extensions"
    assert (
        db_session.execute(text("SHOW search_path")).scalar_one().replace(" ", "")
        == "anticipa,extensions,public"
    )
    assert db_session.execute(text("SELECT '[1,2,3]'::vector <=> '[1,2,3]'::vector")).scalar_one() == 0


def test_no_anon_or_authenticated_privileges_when_those_roles_exist(db_session):
    roles = [
        r
        for (r,) in db_session.execute(
            text("SELECT rolname FROM pg_roles WHERE rolname IN ('anon','authenticated')")
        )
    ]
    for role in roles:  # exists on Supabase, usually not on the local image
        assert not db_session.execute(
            text("SELECT has_schema_privilege(:r, :s, 'USAGE')"), {"r": role, "s": DB_SCHEMA}
        ).scalar_one()
        assert not db_session.execute(
            text(
                "SELECT count(*) FROM information_schema.role_table_grants WHERE grantee = :r AND table_schema = :s"
            ),
            {"r": role, "s": DB_SCHEMA},
        ).scalar_one()


def test_audit_events_are_append_only(db_session):
    db_session.execute(
        text(
            f"INSERT INTO {DB_SCHEMA}.audit_events (action, resource_type, outcome) VALUES ('t', 't', 'success')"
        )
    )
    for stmt in ("UPDATE {t} SET action = 'x'", "DELETE FROM {t}", "TRUNCATE {t}"):
        with pytest.raises(DBAPIError, match="append-only"):
            with db_session.begin_nested():
                db_session.execute(text(stmt.format(t=f"{DB_SCHEMA}.audit_events")))
    assert (
        db_session.execute(
            text(f"SELECT count(*) FROM {DB_SCHEMA}.audit_events WHERE action = 't'")
        ).scalar_one()
        == 1
    )


def test_audit_outcome_is_constrained(db_session):
    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.execute(
            text(
                f"INSERT INTO {DB_SCHEMA}.audit_events (action, resource_type, outcome) VALUES ('t','t','maybe')"
            )
        )


def test_model_invocations_have_cache_hit_and_accept_every_router_error_category(db_session):
    from anticipa.ai.models import ERROR_CATEGORIES

    assert "budget_exceeded" in ERROR_CATEGORIES
    for category in (*ERROR_CATEGORIES, None):
        with db_session.begin_nested():
            db_session.execute(
                text(
                    f"INSERT INTO {DB_SCHEMA}.model_invocations (task, provider, model_id, status, error_category, cache_hit) "
                    "VALUES ('t', 'p', 'm', 's', :c, true)"
                ),
                {"c": category},
            )
    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.execute(
            text(
                f"INSERT INTO {DB_SCHEMA}.model_invocations (task, provider, model_id, status, error_category) VALUES ('t','p','m','s','bogus')"
            )
        )


def test_evidence_vocabularies_follow_contract_17_section_6(db_session):
    """Constraint values (07 evidence vocabularies): the schema accepts them and rejects anything else."""
    names = {c: db_session.execute(text(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n AND conrelid::regclass::text LIKE '%evidence%'"
    ), {"n": f"ck_{t}_{c}"}).scalar_one() for t, c in (
        ("evidence_facts", "assertion_status"), ("evidence_facts", "temporal_status"),
        ("evidence_facts", "subject_type"), ("evidence_facts", "extraction_confidence"),
        ("evidence_sources", "verification_status"),
    )}  # fmt: skip
    assert all(v in names["assertion_status"] for v in ("positive", "negative", "uncertain", "conditional"))
    assert all(v in names["temporal_status"] for v in ("current", "historical", "planned", "unknown"))
    assert all(v in names["subject_type"] for v in ("resident", "family_member", "other", "unknown"))
    assert all(v in names["extraction_confidence"] for v in ("low", "medium", "high"))
    assert all(
        v in names["verification_status"]
        for v in (
            "verified_exact",
            "verified_normalized",
            "span_mismatch",
            "quote_not_found",
            "source_missing",
            "verification_error",
        )
    )
    cols = {c for (c,) in db_session.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = :s AND table_name = 'evidence_sources'"
    ), {"s": DB_SCHEMA})}  # fmt: skip
    assert {
        "location_ambiguous",
        "all_spans",
        "source_quote",
        "source_start_offset",
        "source_end_offset",
    } <= cols


def test_real_data_is_impossible_by_schema(db_session):
    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.execute(
            text(f"INSERT INTO {DB_SCHEMA}.datasets (dataset_version, generator) VALUES ('t', 'g')")
        )
        db_session.execute(text(
            f"INSERT INTO {DB_SCHEMA}.residents (dataset_id, source_system, source_patient_id, synthetic_status) "
            f"SELECT id, 's', 'p', 'real' FROM {DB_SCHEMA}.datasets WHERE dataset_version = 't'"
        ))  # fmt: skip
