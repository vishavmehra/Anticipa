# ruff: noqa: E501, S608  (fixed test SQL; names come from constants)
"""Migration 0005: read-only Data-API access for logged-in judges (observer), simulated locally.

Supabase's roles (`anon`, `authenticated`), the `auth.jwt()` function and its default privileges on `public`
are
created INSIDE the test transaction (rolled back afterwards, so nothing leaks into the shared cluster), then
the
migration SQL is installed and exercised with `SET LOCAL ROLE` + `request.jwt.claims`, exactly how PostgREST
works.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import psycopg.errors
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from anticipa.config import DB_SCHEMA
from anticipa.db import (
    HARDEN_SQL,
    JUDGE_READ_REVOKE_SQL,
    JUDGE_READ_SQL,
    JUDGE_READ_TABLES,
    JUDGE_VIEW_PREFIX,
)

NEVER_GRANTED = (
    "audit_events",
    "user_profiles",
    "model_invocations",
    "processing_jobs",
    "processing_items",
    "document_chunks",
    "alembic_version",
)
PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
OBSERVER = {"app_metadata": {"anticipa_role": "observer"}, "user_metadata": {"anticipa_role": "observer"}}
VIEWS = [f"{JUDGE_VIEW_PREFIX}{t}" for t in JUDGE_READ_TABLES]

SIMULATE_ROLES = [
    """DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN CREATE ROLE anon NOLOGIN; END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            CREATE ROLE authenticated NOLOGIN;
        END IF;
    END $$""",
    # Supabase hands every new object in `public` to the API roles; the view revokes must undo that.
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO anon, authenticated",
]
SIMULATE_AUTH = [
    "CREATE SCHEMA IF NOT EXISTS auth",
    """CREATE OR REPLACE FUNCTION auth.jwt() RETURNS jsonb LANGUAGE sql STABLE AS $$
        SELECT coalesce(nullif(current_setting('request.jwt.claims', true), ''), '{}')::jsonb $$""",
    "GRANT USAGE ON SCHEMA auth TO anon, authenticated",
    "GRANT EXECUTE ON FUNCTION auth.jwt() TO anon, authenticated",
]


def run(session, statements):
    for statement in statements:
        session.execute(text(statement))


@pytest.fixture
def supabase_roles(ddl_session):
    run(ddl_session, SIMULATE_ROLES)
    return ddl_session


@pytest.fixture
def judge(supabase_roles):
    """Roles + auth.jwt() + migration 0005 SQL + the post-upgrade hardening hook, plus three synthetic residents."""
    s = supabase_roles
    run(s, SIMULATE_AUTH)
    s.execute(text(JUDGE_READ_SQL))
    s.execute(text(HARDEN_SQL))  # alembic/env.py runs this after every upgrade; it must keep the judge grants
    dataset = s.execute(
        text(
            f"INSERT INTO {DB_SCHEMA}.datasets (dataset_version, generator) VALUES ('judge-test', 'test') RETURNING id"
        )
    ).scalar_one()
    for i in range(3):
        s.execute(
            text(
                f"INSERT INTO {DB_SCHEMA}.residents (dataset_id, source_system, source_patient_id) "
                "VALUES (:d, 'judge-test', :p)"
            ),
            {"d": dataset, "p": f"p{i}"},
        )
    return s


@contextmanager
def acting_as(session, role, claims=None):
    session.execute(text(f"SET LOCAL ROLE {role}"))
    session.execute(
        text("SELECT set_config('request.jwt.claims', :c, true)"), {"c": json.dumps(claims or {})}
    )
    try:
        yield
    finally:
        session.execute(text("RESET ROLE"))


def count(session, relation, where="TRUE"):
    return session.execute(text(f"SELECT count(*) FROM {relation} WHERE {where}")).scalar_one()


def denied(session, statement):
    """True when PostgreSQL refuses the statement for lack of privilege (the savepoint keeps the test going)."""
    try:
        with session.begin_nested():
            session.execute(text(statement))
    except DBAPIError as exc:
        assert isinstance(exc.orig, psycopg.errors.InsufficientPrivilege), exc
        return True
    return False


def has_priv(session, role, relation, privilege):
    return session.execute(
        text("SELECT has_table_privilege(:r, :t, :p)"), {"r": role, "t": relation, "p": privilege}
    ).scalar_one()


# ---------------------------------------------------------------------------------------------- allowed


def test_observer_reads_every_judge_view(judge):
    with acting_as(judge, "authenticated", OBSERVER):
        for view in VIEWS:
            count(judge, f"public.{view}")  # must not raise
        assert count(judge, "public.judge_residents", "source_system = 'judge-test'") == 3
        assert count(judge, "public.judge_datasets", "dataset_version = 'judge-test'") == 1
        assert (
            count(judge, f"{DB_SCHEMA}.residents", "source_system = 'judge-test'") == 3
        )  # base table RLS admits too


# ---------------------------------------------------------------------------------------------- denied


@pytest.mark.parametrize(
    "claims",
    [
        {},
        {"sub": "u1", "role": "authenticated"},
        {"app_metadata": {}},
        {"app_metadata": {"anticipa_role": "population_admin"}},
        {"app_metadata": {"anticipa_role": "OBSERVER"}},
        {"user_metadata": {"anticipa_role": "observer"}},  # user-writable: never trusted
    ],
    ids=["no-claims", "no-role", "empty-app-metadata", "other-role", "case-mismatch", "user-metadata-only"],
)
def test_logged_in_user_without_observer_role_sees_nothing(judge, claims):
    with acting_as(judge, "authenticated", claims):
        assert count(judge, "public.judge_residents") == 0
        assert count(judge, "public.judge_datasets") == 0
        assert count(judge, f"{DB_SCHEMA}.residents") == 0


def test_anon_is_denied_everywhere(judge):
    with acting_as(judge, "anon", OBSERVER):  # even a forged observer claim does nothing without privileges
        assert denied(judge, "SELECT count(*) FROM public.judge_residents")
        assert denied(judge, f"SELECT count(*) FROM {DB_SCHEMA}.residents")
        assert denied(judge, f"SELECT count(*) FROM {DB_SCHEMA}.datasets")
    relations = [f"public.{v}" for v in VIEWS] + [f"{DB_SCHEMA}.{t}" for t in JUDGE_READ_TABLES]
    for relation in relations:
        for privilege in PRIVILEGES:
            assert not has_priv(judge, "anon", relation, privilege), (relation, privilege)
    assert not judge.execute(
        text("SELECT has_schema_privilege('anon', :s, 'USAGE')"), {"s": DB_SCHEMA}
    ).scalar_one()


def test_nobody_can_write_through_views_or_tables(judge):
    with acting_as(judge, "authenticated", OBSERVER):
        for statement in (
            "INSERT INTO public.judge_residents (dataset_id, source_system, source_patient_id) "
            "SELECT dataset_id, 'x', 'x' FROM public.judge_residents LIMIT 1",
            "UPDATE public.judge_residents SET source_system = 'x'",
            "DELETE FROM public.judge_residents",
            f"UPDATE {DB_SCHEMA}.residents SET source_system = 'x'",
            f"DELETE FROM {DB_SCHEMA}.residents",
            f"TRUNCATE {DB_SCHEMA}.residents",
            f"INSERT INTO {DB_SCHEMA}.datasets (dataset_version, generator) VALUES ('hack', 'x')",
            "DROP VIEW public.judge_residents",
            "CREATE VIEW public.judge_extra AS SELECT 1",
            f"CREATE TABLE {DB_SCHEMA}.hack (id int)",
        ):
            assert denied(judge, statement), statement
    assert count(judge, f"{DB_SCHEMA}.residents", "source_system = 'judge-test'") == 3  # untouched


def test_sensitive_tables_are_unreachable_and_have_no_view(judge):
    with acting_as(judge, "authenticated", OBSERVER):
        for table in NEVER_GRANTED:
            assert denied(judge, f"SELECT * FROM {DB_SCHEMA}.{table}"), table
    judge.execute(text("RESET ROLE"))
    views = {
        v for (v,) in judge.execute(text("SELECT viewname FROM pg_views WHERE viewname LIKE 'judge\\_%'"))
    }
    assert views == set(VIEWS)
    for table in NEVER_GRANTED:
        assert f"{JUDGE_VIEW_PREFIX}{table}" not in views


# ---------------------------------------------------------------------------------------------- exact shape


def test_exact_privileges_after_install_and_hardening(judge):
    tables = [
        t
        for (t,) in judge.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = :s"), {"s": DB_SCHEMA}
        )
    ]
    assert set(NEVER_GRANTED) <= set(tables)
    for table in tables:
        for privilege in PRIVILEGES:
            expected = privilege == "SELECT" and table in JUDGE_READ_TABLES
            assert has_priv(judge, "authenticated", f"{DB_SCHEMA}.{table}", privilege) is expected, (
                table,
                privilege,
            )
            assert not has_priv(judge, "anon", f"{DB_SCHEMA}.{table}", privilege), (table, privilege)
    schema_usage = "SELECT has_schema_privilege(:r, :s, 'USAGE')"
    assert judge.execute(text(schema_usage), {"r": "authenticated", "s": DB_SCHEMA}).scalar_one()
    assert not judge.execute(text(schema_usage), {"r": "anon", "s": DB_SCHEMA}).scalar_one()
    for view in VIEWS:
        for privilege in PRIVILEGES:
            assert has_priv(judge, "authenticated", f"public.{view}", privilege) is (privilege == "SELECT")
            assert not has_priv(judge, "anon", f"public.{view}", privilege)
        acl = judge.execute(
            text("SELECT relacl::text FROM pg_class WHERE relname = :v"), {"v": view}
        ).scalar_one()
        assert "{=" not in acl and ",=" not in acl, acl  # nothing granted to PUBLIC


def test_policies_and_views_are_shaped_as_specified(judge):
    policies = judge.execute(
        text(
            "SELECT tablename, cmd, roles::text, qual FROM pg_policies WHERE schemaname = :s AND policyname = 'judge_read'"
        ),
        {"s": DB_SCHEMA},
    ).all()
    assert {p.tablename for p in policies} == set(JUDGE_READ_TABLES)
    for p in policies:
        assert p.cmd == "SELECT" and p.roles == "{authenticated}"
        assert "app_metadata" in p.qual and "anticipa_role" in p.qual and "observer" in p.qual
        assert "user_metadata" not in p.qual
    rls = judge.execute(
        text(
            "SELECT relname FROM pg_class WHERE relnamespace = CAST(:s AS regnamespace) AND relkind = 'r' AND NOT relrowsecurity"
        ),
        {"s": DB_SCHEMA},
    ).all()
    assert rls == []
    options = judge.execute(
        text(
            "SELECT relname, reloptions::text FROM pg_class WHERE relname LIKE 'judge\\_%' AND relkind = 'v'"
        )
    ).all()
    assert len(options) == len(VIEWS) and all("security_invoker=true" in o.reloptions for o in options)


def test_install_is_idempotent(judge):
    judge.execute(text(JUDGE_READ_SQL))
    judge.execute(text(HARDEN_SQL))
    assert judge.execute(
        text("SELECT count(*) FROM pg_policies WHERE policyname = 'judge_read'")
    ).scalar_one() == len(JUDGE_READ_TABLES)
    with acting_as(judge, "authenticated", OBSERVER):
        assert count(judge, "public.judge_residents", "source_system = 'judge-test'") == 3


def test_downgrade_removes_everything(judge):
    judge.execute(text(JUDGE_READ_REVOKE_SQL))
    judge.execute(text(HARDEN_SQL))
    assert (
        judge.execute(text("SELECT count(*) FROM pg_views WHERE viewname LIKE 'judge\\_%'")).scalar_one() == 0
    )
    assert (
        judge.execute(text("SELECT count(*) FROM pg_policies WHERE policyname = 'judge_read'")).scalar_one()
        == 0
    )
    for table in JUDGE_READ_TABLES:
        assert not has_priv(judge, "authenticated", f"{DB_SCHEMA}.{table}", "SELECT")
    assert not judge.execute(
        text("SELECT has_schema_privilege('authenticated', :s, 'USAGE')"), {"s": DB_SCHEMA}
    ).scalar_one()


# ---------------------------------------------------------------------------------------------- hardening /
# no-op


def test_hardening_alone_grants_nothing_and_removes_stray_grants(supabase_roles):
    s = supabase_roles
    s.execute(text(f"GRANT USAGE ON SCHEMA {DB_SCHEMA} TO authenticated"))
    s.execute(text(f"GRANT SELECT ON {DB_SCHEMA}.audit_events TO authenticated, anon"))
    s.execute(text(f"GRANT SELECT ON {DB_SCHEMA}.residents TO authenticated"))
    s.execute(text(HARDEN_SQL))  # no judge_read policy installed => nothing may remain
    assert not has_priv(s, "authenticated", f"{DB_SCHEMA}.residents", "SELECT")
    assert not has_priv(s, "authenticated", f"{DB_SCHEMA}.audit_events", "SELECT")
    assert not has_priv(s, "anon", f"{DB_SCHEMA}.audit_events", "SELECT")
    assert not s.execute(
        text("SELECT has_schema_privilege('authenticated', :s, 'USAGE')"), {"s": DB_SCHEMA}
    ).scalar_one()


def test_hardening_never_regrants_audit_even_with_the_judge_policy_installed(judge):
    judge.execute(
        text(f"GRANT SELECT ON {DB_SCHEMA}.audit_events TO authenticated")
    )  # a mistake someone could make
    judge.execute(text(HARDEN_SQL))
    for table in NEVER_GRANTED:
        assert not has_priv(judge, "authenticated", f"{DB_SCHEMA}.{table}", "SELECT"), table


def test_migration_sql_is_a_noop_without_supabase_auth(supabase_roles):
    s = supabase_roles
    if s.execute(text("SELECT to_regprocedure('auth.jwt()') IS NOT NULL")).scalar_one():
        pytest.skip("auth.jwt() exists in the test database")
    s.execute(text(JUDGE_READ_SQL))  # roles exist, auth.jwt() does not => nothing happens
    assert s.execute(text("SELECT count(*) FROM pg_views WHERE viewname LIKE 'judge\\_%'")).scalar_one() == 0
    assert (
        s.execute(text("SELECT count(*) FROM pg_policies WHERE policyname = 'judge_read'")).scalar_one() == 0
    )
    assert not has_priv(s, "authenticated", f"{DB_SCHEMA}.residents", "SELECT")


def test_judge_table_list_is_the_documented_one():
    assert set(JUDGE_READ_TABLES) == {
        "residents", "clinical_records", "family_edges", "documents", "evidence_facts", "evidence_sources",
        "guidelines", "guideline_versions", "rule_versions", "eligibility_evaluations", "care_gap_cases",
        "case_status_history", "clinics", "clinic_services", "capacity_slots", "planning_runs",
        "planned_allocations", "unallocated_cases", "communication_templates", "communication_events",
        "resident_responses", "appointments", "completion_evidence", "datasets",
    }  # fmt: skip
    assert len(JUDGE_READ_TABLES) == 24 and not set(JUDGE_READ_TABLES) & set(NEVER_GRANTED)


def test_judges_see_neither_internal_tables_nor_internal_api_permissions():
    """Owner decision 2026-11-12: jobs, audit, integrations and AI spend are operator-only, in the Data API and the REST API."""
    from anticipa.core import rbac

    assert not set(NEVER_GRANTED) & set(JUDGE_READ_TABLES)
    observer = rbac.permissions_for("observer")
    assert not observer & {
        "jobs.view",
        "integrations.view",
        "audit.view",
        "audit.view_scoped",
        "audit.view_redacted",
        "system.configure",
    }
