"""Alembic environment: one linear history, all objects in the `anticipa` schema (ADR-058)."""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import pool, text

import anticipa.models  # noqa: F401  (registers every module's tables on Base.metadata)
from alembic import context
from anticipa.config import DB_SCHEMA, get_settings
from anticipa.db import HARDEN_SQL, Base, make_engine

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Only manage the `anticipa` schema; ignore anything else a shared database (e.g. Supabase) contains."""
    if type_ == "table":
        return getattr(obj, "schema", None) == DB_SCHEMA
    return True


CONTEXT_OPTS = {
    "target_metadata": target_metadata,
    "version_table_schema": DB_SCHEMA,
    "include_schemas": True,
    "include_object": include_object,
    "compare_type": True,
}


def database_url():
    url = config.attributes.get(
        "url"
    )  # tests/programmatic callers pass a URL here (no ini-file escaping issues)
    return url or get_settings().migration_url()


def _render(url) -> str:
    return url if isinstance(url, str) else url.render_as_string(hide_password=False)


def run_migrations_offline() -> None:
    context.configure(
        url=_render(database_url()), literal_binds=True, dialect_opts={"paramstyle": "named"}, **CONTEXT_OPTS
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = make_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        # The version table lives in the app schema, so the schema must exist before Alembic touches it.
        connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {DB_SCHEMA}"))
        connection.commit()
        context.configure(connection=connection, **CONTEXT_OPTS)
        with context.begin_transaction():
            context.run_migrations()
            connection.execute(text(HARDEN_SQL))  # ADR-058 safety net for tables added by any migration
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
