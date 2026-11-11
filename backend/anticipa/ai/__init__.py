"""AI layer: Model Router (Claude + Nebius only), prompt registry, invocation accounting."""

from .catalogue import CatalogueError, is_meta_model
from .router import ModelRouter, build_router, configure_router, get_router
from .sinks import DbSink, InMemorySink, InvocationRecord, InvocationSink
from .types import (
    ChatMessage,
    EmbedRequest,
    EmbedResult,
    GenerateRequest,
    GenerateResult,
    RouterError,
    Usage,
)

__all__ = [
    "CatalogueError",
    "ChatMessage",
    "DbSink",
    "EmbedRequest",
    "EmbedResult",
    "GenerateRequest",
    "GenerateResult",
    "InMemorySink",
    "InvocationRecord",
    "InvocationSink",
    "ModelRouter",
    "RouterError",
    "Usage",
    "build_router",
    "configure_router",
    "get_router",
    "is_meta_model",
]
