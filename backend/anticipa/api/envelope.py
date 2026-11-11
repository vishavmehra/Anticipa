"""Response envelopes and pagination (contract 17 §2): {"data", "meta": {request_id, page, page_size, total,
total_pages}}."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated

from fastapi import Query, Request
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from anticipa.core.logging import request_id_var

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


class Meta(BaseModel):
    request_id: str | None = None


class PageMeta(Meta):
    page: int
    page_size: int
    total: int
    total_pages: int


class Envelope[T](BaseModel):
    data: T
    meta: Meta


class PagedEnvelope[T](BaseModel):
    data: list[T]
    meta: PageMeta


class ErrorDetail(BaseModel):
    field: str | None = None
    reason: str


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[ErrorDetail] = []
    request_id: str | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorBody


def current_request_id(request: Request | None = None) -> str | None:
    if request is not None:
        rid = getattr(request.state, "request_id", None)
        if rid:
            return rid
    return request_id_var.get()


def ok[T](request: Request, data: T) -> Envelope[T]:
    return Envelope(data=data, meta=Meta(request_id=current_request_id(request)))


@dataclass(frozen=True)
class PageParams:
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def page_params(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> PageParams:
    return PageParams(page, page_size)


def paged[T](request: Request, items: Sequence[T], params: PageParams, total: int) -> PagedEnvelope[T]:
    return PagedEnvelope(
        data=list(items),
        meta=PageMeta(
            request_id=current_request_id(request),
            page=params.page,
            page_size=params.page_size,
            total=total,
            total_pages=math.ceil(total / params.page_size) if total else 0,
        ),
    )


def paginate(session: Session, stmt: Select, params: PageParams) -> tuple[list, int]:
    """Run `stmt` (an ordered ORM select) for one page; returns (rows, total)."""
    total = session.execute(select(func.count()).select_from(stmt.order_by(None).subquery())).scalar_one()
    rows = session.execute(stmt.limit(params.page_size).offset(params.offset)).scalars().all()
    return list(rows), total
