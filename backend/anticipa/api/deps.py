"""Shared FastAPI dependencies and annotated aliases for route modules."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from anticipa.api.envelope import PageParams, page_params
from anticipa.config import Settings
from anticipa.core.auth import CurrentUser, get_current_user, require_permission
from anticipa.db import get_session


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]
PageDep = Annotated[PageParams, Depends(page_params)]

__all__ = [
    "CurrentUser",
    "CurrentUserDep",
    "PageDep",
    "SessionDep",
    "SettingsDep",
    "get_current_user",
    "require_permission",
]
