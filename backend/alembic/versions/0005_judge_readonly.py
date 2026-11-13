"""judge read-only Data API access: SELECT + observer-only RLS policy + public.judge_<table> views

Owner decision: the Supabase Data API is read-only for logged-in judges. Effective only where the Supabase
roles `anon`/`authenticated` and `auth.jwt()` exist (the DO blocks are a no-op on the local Docker Postgres).
The SQL lives in `anticipa.db` because alembic/env.py re-asserts the grants after every upgrade (the ADR-058
hardening revokes them first).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-11
"""

from collections.abc import Sequence

from alembic import op
from anticipa.db import JUDGE_READ_REVOKE_SQL, JUDGE_READ_SQL

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(JUDGE_READ_SQL)


def downgrade() -> None:
    op.execute(JUDGE_READ_REVOKE_SQL)
