"""Sync SQLAlchemy 2 + psycopg3 engine/session plumbing and the shared declarative Base.

All application tables live in the dedicated `anticipa` schema (ADR-058). The engine sets
`search_path = anticipa, extensions, public` on every new connection so the pgvector `vector` type resolves
both on the local docker image and on Supabase (where extensions live in the `extensions` schema).
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from fastapi import Request
from sqlalchemy import MetaData, create_engine, event, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from anticipa.config import DB_SCHEMA, get_settings

SEARCH_PATH = f"{DB_SCHEMA}, extensions, public"

# Stable constraint names so Alembic autogenerate/downgrade are deterministic.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(schema=DB_SCHEMA, naming_convention=NAMING_CONVENTION)


# Shared helpers for table definitions in every module's models.py
UUID_PK = {"server_default": text("gen_random_uuid()")}  # **UUID_PK in mapped_column(primary_key=True, ...)


def sql_in(column: str, values: tuple[str, ...]) -> str:
    """CHECK expression `column IN ('a','b')` for enum-like text columns (no native PG enums)."""
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


# Owner decision (Supabase Data API = read-only for logged-in judges; migration 0005): synthetic domain
# tables that the `observer` role may SELECT through the Data API. Everything else (audit_events,
# user_profiles, model_invocations, processing_jobs/items, document_chunks, alembic_version, ...) is never
# granted.
JUDGE_READ_TABLES = (
    "residents", "clinical_records", "family_edges", "documents", "evidence_facts", "evidence_sources",
    "guidelines", "guideline_versions", "rule_versions", "eligibility_evaluations", "care_gap_cases",
    "case_status_history", "clinics", "clinic_services", "capacity_slots", "planning_runs",
    "planned_allocations", "unallocated_cases", "communication_templates", "communication_events",
    "resident_responses", "appointments", "completion_evidence", "datasets",
)  # fmt: skip
JUDGE_VIEW_PREFIX = "judge_"  # public.judge_<table>: what PostgREST serves
_JUDGE_TABLES_SQL = "ARRAY[" + ", ".join(f"'{t}'" for t in JUDGE_READ_TABLES) + "]"

# ADR-058, idempotent: RLS on every table in the app schema (no policies => non-owner roles see nothing)
# and no privileges for the Supabase Data-API roles. Run by migration 0001 and again by alembic/env.py after
# every upgrade, so a table added by a later migration cannot be left exposed. Where the judge_read policy is
# installed (migration 0005) the judge SELECT grants are re-asserted, because the revokes above remove them.
HARDEN_SQL = f"""
DO $$
DECLARE t record; r text; tbl text;
BEGIN
    FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = '{DB_SCHEMA}' LOOP
        EXECUTE format('ALTER TABLE {DB_SCHEMA}.%I ENABLE ROW LEVEL SECURITY', t.tablename);
    END LOOP;
    FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('REVOKE ALL ON SCHEMA {DB_SCHEMA} FROM %I', r);
            EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA {DB_SCHEMA} FROM %I', r);
            EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA {DB_SCHEMA} FROM %I', r);
            EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {DB_SCHEMA} FROM %I', r);
            EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA {DB_SCHEMA} REVOKE ALL ON TABLES FROM %I', r);
            EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA {DB_SCHEMA} REVOKE ALL ON SEQUENCES FROM %I',
                           r);
            EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA {DB_SCHEMA} REVOKE ALL ON FUNCTIONS FROM %I',
                           r);
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated')
       AND EXISTS (SELECT 1 FROM pg_policies
                   WHERE schemaname = '{DB_SCHEMA}' AND policyname = 'judge_read') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA {DB_SCHEMA} TO authenticated';
        FOREACH tbl IN ARRAY {_JUDGE_TABLES_SQL} LOOP
            IF to_regclass(format('{DB_SCHEMA}.%I', tbl)) IS NOT NULL THEN
                EXECUTE format('GRANT SELECT ON {DB_SCHEMA}.%I TO authenticated', tbl);
            END IF;
        END LOOP;
    END IF;
END $$;
"""  # noqa: S608  (DB_SCHEMA and the table list are constants)

# Migration 0005: read-only Data-API access for logged-in judges, only where Supabase's roles and
# `auth.jwt()` exist (a no-op on the local Docker Postgres). Per table: SELECT for `authenticated`, an RLS
# policy that admits only `app_metadata.anticipa_role = 'observer'` (user_metadata is never trusted; a
# self-registered user has no role and sees nothing), and a security_invoker view public.judge_<table> so
# PostgREST serves it while the base table's RLS still applies. Nothing for `anon`; no INSERT/UPDATE/DELETE
# for anyone. Idempotent.
JUDGE_READ_SQL = f"""
DO $$
DECLARE tbl text;
BEGIN
    IF NOT (EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated')
            AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon')
            AND to_regprocedure('auth.jwt()') IS NOT NULL) THEN
        RETURN;
    END IF;
    EXECUTE 'GRANT USAGE ON SCHEMA {DB_SCHEMA} TO authenticated';
    FOREACH tbl IN ARRAY {_JUDGE_TABLES_SQL} LOOP
        IF to_regclass(format('{DB_SCHEMA}.%I', tbl)) IS NULL THEN
            CONTINUE;
        END IF;
        EXECUTE format('ALTER TABLE {DB_SCHEMA}.%I ENABLE ROW LEVEL SECURITY', tbl);
        EXECUTE format('GRANT SELECT ON {DB_SCHEMA}.%I TO authenticated', tbl);
        EXECUTE format('DROP POLICY IF EXISTS judge_read ON {DB_SCHEMA}.%I', tbl);
        EXECUTE format(
            $p$CREATE POLICY judge_read ON {DB_SCHEMA}.%I FOR SELECT TO authenticated
               USING (((SELECT auth.jwt()) -> 'app_metadata' ->> 'anticipa_role') = 'observer')$p$, tbl);
        EXECUTE format('DROP VIEW IF EXISTS public.%I', '{JUDGE_VIEW_PREFIX}' || tbl);
        EXECUTE format('CREATE VIEW public.%I WITH (security_invoker = true) AS SELECT * FROM {DB_SCHEMA}.%I',
                       '{JUDGE_VIEW_PREFIX}' || tbl, tbl);
        EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC, anon, authenticated',
                       '{JUDGE_VIEW_PREFIX}' || tbl);
        EXECUTE format('GRANT SELECT ON public.%I TO authenticated', '{JUDGE_VIEW_PREFIX}' || tbl);
    END LOOP;
END $$;
"""  # noqa: S608  (DB_SCHEMA, the view prefix and the table list are constants)

# Reverse of JUDGE_READ_SQL (migration 0005 downgrade). Safe where the roles or policies do not exist.
JUDGE_READ_REVOKE_SQL = f"""
DO $$
DECLARE tbl text;
BEGIN
    FOREACH tbl IN ARRAY {_JUDGE_TABLES_SQL} LOOP
        EXECUTE format('DROP VIEW IF EXISTS public.%I', '{JUDGE_VIEW_PREFIX}' || tbl);
        IF to_regclass(format('{DB_SCHEMA}.%I', tbl)) IS NOT NULL THEN
            EXECUTE format('DROP POLICY IF EXISTS judge_read ON {DB_SCHEMA}.%I', tbl);
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                EXECUTE format('REVOKE ALL ON {DB_SCHEMA}.%I FROM authenticated', tbl);
            END IF;
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'REVOKE ALL ON SCHEMA {DB_SCHEMA} FROM authenticated';
    END IF;
END $$;
"""  # noqa: S608  (constants only)


def _on_connect(dbapi_connection, _record) -> None:
    # Run as session-level SET (not a startup `options` parameter: poolers such as Supavisor can reject
    # those).
    previous = dbapi_connection.autocommit
    dbapi_connection.autocommit = True
    try:
        with dbapi_connection.cursor() as cur:
            cur.execute(f"SET search_path = {SEARCH_PATH}")
            cur.execute("SET timezone = 'UTC'")
    finally:
        dbapi_connection.autocommit = previous


def make_engine(url: str | URL, **kwargs) -> Engine:
    kwargs.setdefault("pool_pre_ping", True)
    engine = create_engine(url, **kwargs)
    event.listen(engine, "connect", _on_connect)
    return engine


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


@lru_cache
def get_engine() -> Engine:
    """Process-wide engine for the CLI/worker (the API uses `app.state.sessionmaker`)."""
    return make_engine(get_settings().sqlalchemy_url(), pool_size=10, max_overflow=10)


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return make_sessionmaker(get_engine())


def get_session(request: Request) -> Iterator[Session]:
    """FastAPI dependency: one Session per request. Endpoints commit explicitly; close rolls back."""
    with request.app.state.sessionmaker() as session:
        yield session
