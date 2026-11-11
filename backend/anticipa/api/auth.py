"""Auth routes. Sign-in/out happen in Supabase Auth (the frontend uses supabase-js); the API only verifies
bearer tokens."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from pydantic import BaseModel

from anticipa.api.deps import CurrentUserDep
from anticipa.api.envelope import Envelope, ok

router = APIRouter(prefix="/auth", tags=["auth"])


class Me(BaseModel):
    id: uuid.UUID
    email: str | None
    display_name: str | None
    role: str
    permissions: list[str]


@router.get("/me", response_model=Envelope[Me], summary="The authenticated user, role and permissions")
def me(request: Request, user: CurrentUserDep):
    return ok(
        request,
        Me(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            role=user.role,
            permissions=sorted(user.permissions),
        ),
    )
