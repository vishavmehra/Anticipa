"""Liveness and readiness probes (unauthenticated; they expose only coarse status, never connection
details)."""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from anticipa.api.deps import SessionDep
from anticipa.api.envelope import Envelope, ok
from anticipa.config import DB_SCHEMA

log = logging.getLogger(__name__)
router = APIRouter(tags=["health"])

ALEMBIC_DIR = (
    Path(__file__).resolve().parents[2] / "alembic"
)  # backend/alembic (/app/alembic in the container)


class Liveness(BaseModel):
    status: str


class Readiness(BaseModel):
    status: str  # ready | not_ready
    database: str  # ok | unreachable
    migrations: str  # ok | outdated | unknown
    current_revision: str | None = None
    head_revision: str | None = None


@lru_cache
def migration_head() -> str | None:
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    return ScriptDirectory.from_config(cfg).get_current_head()


# GET and HEAD: uptime monitors (e.g. UptimeRobot) probe with HEAD by default.
@router.api_route("/health", methods=["GET", "HEAD"], response_model=Envelope[Liveness], summary="Liveness")
def health(request: Request) -> Envelope[Liveness]:
    return ok(request, Liveness(status="ok"))


@router.api_route(
    "/health/ready",
    methods=["GET", "HEAD"],
    response_model=Envelope[Readiness],
    summary="Readiness: database reachable + migrations at head",
)
def ready(request: Request, session: SessionDep):
    head = migration_head()
    current: str | None = None
    database = "ok"
    try:
        session.execute(text("SELECT 1"))
        conn = session.connection()
        current = MigrationContext.configure(
            conn, opts={"version_table_schema": DB_SCHEMA}
        ).get_current_revision()
    except Exception:
        log.warning("readiness check failed", exc_info=True)
        database = "unreachable"
    if database != "ok":
        migrations = "unknown"
    else:
        migrations = "ok" if current == head else "outdated"
    is_ready = database == "ok" and migrations == "ok"
    payload = Envelope(
        data=Readiness(
            status="ready" if is_ready else "not_ready",
            database=database,
            migrations=migrations,
            current_revision=current,
            head_revision=head,
        ),
        meta={"request_id": getattr(request.state, "request_id", None)},
    )
    if is_ready:
        return payload
    return JSONResponse(payload.model_dump(mode="json"), status_code=503)
