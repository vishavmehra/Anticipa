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


# ADR-058, idempotent: RLS on every table in the app schema (no policies => non-owner roles see nothing)
# and no privileges for the Supabase Data-API roles. Run by migration 0001 and again by alembic/env.py after
# every upgrade, so a table added by a later migration cannot be left exposed.
HARDEN_SQL = f"""
DO $$
DECLARE t record; r text;
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
END $$;
"""  # noqa: S608  (DB_SCHEMA is a constant)


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
