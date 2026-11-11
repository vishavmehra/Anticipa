"""FastAPI app factory. Run: `uvicorn anticipa.main:app` (API) / `anticipa worker` (background jobs).

`app` is built on first access (module `__getattr__`), so importing this module never needs a database
configuration.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version

from fastapi import FastAPI

from anticipa.ai import DbSink, configure_router
from anticipa.api.errors import install_exception_handlers
from anticipa.api.middleware import RequestContextMiddleware
from anticipa.api.router import build_api_router
from anticipa.config import Settings, get_settings
from anticipa.core.logging import configure_logging
from anticipa.core.modules import load_module_files
from anticipa.db import make_engine, make_sessionmaker
from anticipa.jobs.worker import EmbeddedWorker, WorkerConfig

log = logging.getLogger(__name__)

API_PREFIX = "/api/v1"
try:
    __version__ = version("anticipa")
except PackageNotFoundError:  # running from a source tree without an installed distribution
    __version__ = "0.0.0+local"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    configure_router(sink=DbSink(app.state.sessionmaker))  # every model call is logged to model_invocations
    worker: EmbeddedWorker | None = None
    if settings.embedded_worker:
        worker = EmbeddedWorker(app.state.sessionmaker, WorkerConfig.from_settings(settings))
        worker.start()
        log.info("embedded job worker started")
    app.state.embedded_worker = worker
    try:
        yield
    finally:
        if worker is not None:
            worker.stop()  # blocking join (bounded), acceptable at shutdown


def create_app(settings: Settings | None = None, *, engine=None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(
        title="Anticipa API",
        version=__version__,
        summary="Population-level preventive-care coordination (synthetic data only).",
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=None,
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine or make_engine(settings.sqlalchemy_url(), pool_size=10, max_overflow=10)
    app.state.sessionmaker = make_sessionmaker(app.state.engine)
    app.state.embedded_worker = None
    app.add_middleware(RequestContextMiddleware)
    install_exception_handlers(app)
    load_module_files(
        "handlers"
    )  # job handlers registered by modules (so the API can validate/describe them)
    app.include_router(build_api_router(), prefix=API_PREFIX)
    return app


_app: FastAPI | None = None


def __getattr__(name: str) -> FastAPI:  # PEP 562: `uvicorn anticipa.main:app`
    global _app
    if name != "app":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if _app is None:
        _app = create_app()
    return _app
