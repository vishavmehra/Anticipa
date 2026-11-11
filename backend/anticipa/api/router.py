"""Router registry: core routers plus any `anticipa/<module>/routes.py` (exposing `router: APIRouter`)."""

from __future__ import annotations

import logging

from fastapi import APIRouter

from anticipa.api import audit, auth, health, integrations, jobs
from anticipa.core.modules import load_module_files

log = logging.getLogger(__name__)


def build_api_router() -> APIRouter:
    api = APIRouter()
    for core in (health, auth, integrations, jobs, audit):
        api.include_router(core.router)
    for mod in load_module_files("routes"):
        router = getattr(mod, "router", None)
        if isinstance(router, APIRouter):
            api.include_router(router)
        else:
            log.error("module routes file has no `router: APIRouter`: %s", mod.__name__)
    return api
