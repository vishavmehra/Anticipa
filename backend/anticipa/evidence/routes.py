"""Evidence API (05 §9). Mounted at /api/v1 by the router registry.

  POST /evidence/extraction-jobs      processing.execute   queue evidence.extract_batch
  POST /evidence/embedding-jobs       processing.execute   queue evidence.embed_documents
  GET  /evidence/search               evidence.view + (evidence.review | processing.execute)   retrieval only
  GET  /evidence                      evidence.view        filterable list; requires_review = review queue
  GET  /residents/{id}/evidence       evidence.view
  GET  /evidence/{id}                 evidence.view        sources, quotes, offsets, verification, model info
  POST /evidence/{id}/review          evidence.review      accept / reject a queued fact (audited)

The observer (judge) role holds only `*.view`: it reads everything here but can neither submit jobs, review,
nor search (search embeds the query through a paid provider, i.e. it is an AI-triggering action, ADR-060).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from anticipa.ai import EmbedRequest, RouterError
from anticipa.ai.models import ModelInvocation
from anticipa.api.deps import PageDep, SessionDep
from anticipa.api.envelope import Envelope, PagedEnvelope, ok, paged, paginate
from anticipa.core import audit
from anticipa.core.auth import CurrentUser, require_any_permission, require_permission
from anticipa.core.errors import NotFound, ProviderUnavailable, RateLimited, ValidationFailed
from anticipa.core.models import AuditEvent
from anticipa.core.rbac import AUDIT_VIEW, EVIDENCE_REVIEW, EVIDENCE_VIEW, PROCESSING_EXECUTE
from anticipa.datafabric.models import Document, DocumentChunk, Resident
from anticipa.evidence import service
from anticipa.evidence.extract import PROMPT_REF
from anticipa.evidence.models import (
    ASSERTION_STATUSES,
    VALIDATION_STATUSES,
    EvidenceFact,
    EvidenceSource,
)
from anticipa.jobs.service import submit_job

log = logging.getLogger(__name__)
router = APIRouter(tags=["evidence"])

ViewUser = Annotated[CurrentUser, Depends(require_permission(EVIDENCE_VIEW))]
ExecuteUser = Annotated[CurrentUser, Depends(require_permission(PROCESSING_EXECUTE))]
ReviewUser = Annotated[CurrentUser, Depends(require_permission(EVIDENCE_REVIEW))]
AiUser = Annotated[CurrentUser, Depends(require_any_permission(EVIDENCE_REVIEW, PROCESSING_EXECUTE))]

RETRIEVAL_NOTICE = (
    "Semantic retrieval for navigation only. Similarity scores never establish a clinical fact, identity or "
    "screening eligibility."
)
CATEGORIES = (
    "condition", "family_history", "procedure", "observation", "immunization", "diagnostic_finding",
    "symptom", "risk_factor", "medication", "preventive_care",
)  # fmt: skip


def _pattern(values: tuple[str, ...]) -> str:
    return "^(" + "|".join(values) + ")$"


# ------------------------------------------------------------------------------------------------ schemas
class SourceOut(BaseModel):
    id: uuid.UUID
    source_type: str
    document_id: uuid.UUID | None
    quote: str | None
    start_offset: int | None
    end_offset: int | None
    content_hash: str | None
    verification_status: str
    location_ambiguous: bool
    all_spans: list[Any] | None


class FactOut(BaseModel):
    id: uuid.UUID
    resident_id: uuid.UUID
    category: str
    fact_type: str
    value: dict[str, Any]
    subject_type: str
    relationship_type: str | None
    assertion_status: str
    temporal_status: str
    extraction_confidence: str | None
    uncertainty_note: str | None
    validation_status: str
    review_status: str
    review_reasons: list[str]
    extraction_method: str
    model_provider: str | None
    model_id: str | None
    prompt_version: str | None
    invocation_id: uuid.UUID | None
    synthetic_status: str
    extracted_at: datetime
    sources: list[SourceOut]


class InvocationOut(BaseModel):
    task: str
    provider: str
    model_id: str
    prompt_version: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int
    cost_usd: float | None
    latency_ms: int | None
    validation_status: str | None
    fallback_used: bool
    cache_hit: bool
    created_at: datetime


class ReviewOut(BaseModel):
    decision: str
    reviewed_at: datetime
    reviewer_role: str | None
    reviewer_id: uuid.UUID | None  # only for callers allowed to read the audit log
    note: str | None


class ContextOut(BaseModel):
    source_id: uuid.UUID
    before: str
    quote: str
    after: str


class DocumentRef(BaseModel):
    id: uuid.UUID
    document_type: str
    language: str | None
    created_on: datetime | None


class FactDetail(FactOut):
    document: DocumentRef | None = None
    invocation: InvocationOut | None = None
    review: ReviewOut | None = None
    context: list[ContextOut] = Field(default_factory=list)


class ReviewIn(BaseModel):
    decision: Literal["accepted", "rejected"]
    review_note: str | None = Field(None, max_length=1000)


class ReviewResult(BaseModel):
    id: uuid.UUID
    validation_status: str
    review_status: str
    previous_validation_status: str
    changed: bool


class ExtractionJobIn(service.ExtractionScope):
    idempotency_key: str | None = Field(None, max_length=200)


class EmbeddingJobIn(service.EmbeddingScope):
    idempotency_key: str | None = Field(None, max_length=200)


class JobSubmitted(BaseModel):
    job_id: uuid.UUID
    job_type: str
    status: str
    created: bool
    document_count: int


class SearchHit(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    resident_id: uuid.UUID
    chunk_index: int
    start_offset: int
    end_offset: int
    text: str | None
    score: float  # cosine similarity
    document_type: str
    language: str | None
    synthetic_status: str


class SearchOut(BaseModel):
    retrieval_only: bool = True
    notice: str = RETRIEVAL_NOTICE
    query_model: str
    hits: list[SearchHit]


# --------------------------------------------------------------------------------------------- read helpers
def _sources_for(session: Session, fact_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[EvidenceSource]]:
    out: dict[uuid.UUID, list[EvidenceSource]] = {i: [] for i in fact_ids}
    if fact_ids:
        rows = session.execute(
            select(EvidenceSource)
            .where(EvidenceSource.evidence_fact_id.in_(fact_ids))
            .order_by(EvidenceSource.created_at, EvidenceSource.id)
        ).scalars()
        for s in rows:
            out[s.evidence_fact_id].append(s)
    return out


def _source_out(s: EvidenceSource) -> SourceOut:
    return SourceOut(
        id=s.id,
        source_type=s.source_type,
        document_id=s.document_id,
        quote=s.source_quote,
        start_offset=s.source_start_offset,
        end_offset=s.source_end_offset,
        content_hash=s.source_content_hash,
        verification_status=s.verification_status,
        location_ambiguous=s.location_ambiguous,
        all_spans=s.all_spans,
    )


def _fact_fields(f: EvidenceFact, sources: list[EvidenceSource]) -> dict[str, Any]:
    value = dict(f.value or {})
    reasons = value.pop("review_reasons", [])
    return {
        "id": f.id,
        "resident_id": f.resident_id,
        "category": f.category,
        "fact_type": f.fact_type,
        "value": value,
        "subject_type": f.subject_type,
        "relationship_type": f.relationship_type,
        "assertion_status": f.assertion_status,
        "temporal_status": f.temporal_status,
        "extraction_confidence": f.extraction_confidence,
        "uncertainty_note": f.uncertainty_note,
        "validation_status": f.validation_status,
        "review_status": f.review_status,
        "review_reasons": reasons,
        "extraction_method": f.extraction_method,
        "model_provider": f.model_provider,
        "model_id": f.model_id,
        "prompt_version": f.prompt_version,
        "invocation_id": f.invocation_id,
        "synthetic_status": f.synthetic_status,
        "extracted_at": f.extracted_at,
        "sources": [_source_out(s) for s in sources],
    }


def _page_of_facts(session: Session, stmt, params) -> tuple[list[FactOut], int]:
    facts, total = paginate(session, stmt, params)
    sources = _sources_for(session, [f.id for f in facts])
    return [FactOut(**_fact_fields(f, sources[f.id])) for f in facts], total


def _facts_stmt(
    *,
    resident_id: uuid.UUID | None,
    category: str | None,
    assertion_status: str | None,
    validation_status: str | None,
    review_status: str | None,
    relationship: str | None,
    document_id: uuid.UUID | None,
    order: str,
):
    stmt = select(EvidenceFact)
    for column, value in (
        (EvidenceFact.resident_id, resident_id),
        (EvidenceFact.category, category),
        (EvidenceFact.assertion_status, assertion_status),
        (EvidenceFact.validation_status, validation_status),
        (EvidenceFact.review_status, review_status),
        (EvidenceFact.relationship_type, relationship),
    ):
        if value is not None:
            stmt = stmt.where(column == value)
    if document_id is not None:
        stmt = stmt.where(
            EvidenceFact.id.in_(
                select(EvidenceSource.evidence_fact_id).where(EvidenceSource.document_id == document_id)
            )
        )
    first = EvidenceFact.extracted_at.asc() if order == "oldest" else EvidenceFact.extracted_at.desc()
    return stmt.order_by(first, EvidenceFact.id)


Category = Annotated[str | None, Query(pattern=_pattern(CATEGORIES))]
Assertion = Annotated[str | None, Query(pattern=_pattern(ASSERTION_STATUSES))]
Validation = Annotated[str | None, Query(pattern=_pattern(VALIDATION_STATUSES))]
Order = Annotated[Literal["newest", "oldest"], Query()]


@router.get(
    "/evidence",
    response_model=PagedEnvelope[FactOut],
    summary="List evidence; the review queue is validation_status=requires_review",
)
def list_evidence(
    request: Request,
    session: SessionDep,
    _user: ViewUser,
    params: PageDep,
    validation_status: Validation = None,
    category: Category = None,
    assertion_status: Assertion = None,
    review_status: Annotated[str | None, Query(max_length=40)] = None,
    relationship: Annotated[str | None, Query(max_length=40)] = None,
    resident_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    order: Order = "newest",
):
    stmt = _facts_stmt(
        resident_id=resident_id,
        category=category,
        assertion_status=assertion_status,
        validation_status=validation_status,
        review_status=review_status,
        relationship=relationship,
        document_id=document_id,
        order=order,
    )
    items, total = _page_of_facts(session, stmt, params)
    return paged(request, items, params, total)


@router.get(
    "/residents/{resident_id}/evidence",
    response_model=PagedEnvelope[FactOut],
    summary="Evidence for one resident",
)
def resident_evidence(
    resident_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    _user: ViewUser,
    params: PageDep,
    validation_status: Validation = None,
    category: Category = None,
    assertion_status: Assertion = None,
    relationship: Annotated[str | None, Query(max_length=40)] = None,
    document_id: uuid.UUID | None = None,
    order: Order = "newest",
):
    if session.get(Resident, resident_id) is None:
        raise NotFound("Resident not found.")
    stmt = _facts_stmt(
        resident_id=resident_id,
        category=category,
        assertion_status=assertion_status,
        validation_status=validation_status,
        review_status=None,
        relationship=relationship,
        document_id=document_id,
        order=order,
    )
    items, total = _page_of_facts(session, stmt, params)
    return paged(request, items, params, total)


# ---------------------------------------------------------------------------------------------- search
def _ai_router(request: Request) -> Any:
    """One Model Router per API process, writing to model_invocations and enforcing the daily budget."""
    r = getattr(request.app.state, "ai_router", None)
    if r is None:
        r = service.make_router(request.app.state.sessionmaker)
        request.app.state.ai_router = r
    return r


def _read_slice(storage_key: str, start: int, end: int) -> str | None:
    try:
        return service.get_store().read_text(storage_key)[start:end]
    except Exception:  # noqa: BLE001 - the store may be absent in a minimal deployment; text is optional
        log.warning("document text unavailable for a search hit", exc_info=True)
        return None


def _search(
    session: Session, vector: list[float], resident_id: uuid.UUID | None, limit: int
) -> list[SearchHit]:
    distance = DocumentChunk.embedding.cosine_distance(vector).label("distance")
    stmt = (
        select(
            DocumentChunk,
            Document.resident_id,
            Document.document_type,
            Document.language,
            Document.storage_key,
            distance,
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(DocumentChunk.embedding.is_not(None))
        .order_by(distance, DocumentChunk.id)
        .limit(limit)
    )
    if resident_id is not None:
        stmt = stmt.where(Document.resident_id == resident_id)
    return [
        SearchHit(
            chunk_id=c.id,
            document_id=c.document_id,
            resident_id=rid,
            chunk_index=c.chunk_index,
            start_offset=c.start_offset,
            end_offset=c.end_offset,
            text=_read_slice(key, c.start_offset, c.end_offset),
            score=round(1.0 - float(dist), 6),
            document_type=dtype,
            language=lang,
            synthetic_status=c.synthetic_status,
        )
        for c, rid, dtype, lang, key, dist in session.execute(stmt)
    ]


@router.get(
    "/evidence/search",
    response_model=Envelope[SearchOut],
    summary="Semantic chunk retrieval (never eligibility)",
)
async def search_evidence(
    request: Request,
    session: SessionDep,
    _view: ViewUser,
    _ai: AiUser,
    q: Annotated[str, Query(min_length=2, max_length=500)],
    resident_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
):
    try:
        res = await _ai_router(request).embed(
            EmbedRequest(task="embedding.document", texts=[q], metadata={"purpose": "evidence_search"})
        )
    except RouterError as e:
        if e.category == "budget_exceeded":
            raise RateLimited(
                "The daily AI budget is exhausted; search is unavailable until tomorrow (UTC)."
            ) from None
        log.warning("search embedding failed: %s", e.category)
        raise ProviderUnavailable("The embedding provider is unavailable.") from None
    hits = await run_in_threadpool(_search, session, res.vectors[0], resident_id, limit)
    return ok(request, SearchOut(query_model=res.model_id, hits=hits))


# ------------------------------------------------------------------------------------------------ detail
def _context(session: Session, sources: list[EvidenceSource], chars: int) -> list[ContextOut]:
    out: list[ContextOut] = []
    store, texts = None, {}
    for s in sources:
        if s.document_id is None or s.source_start_offset is None or s.source_end_offset is None:
            continue
        try:
            if s.document_id not in texts:
                store = store or service.get_store()
                key = session.execute(
                    select(Document.storage_key).where(Document.id == s.document_id)
                ).scalar_one()
                texts[s.document_id] = store.read_text(key)
            t, a, b = texts[s.document_id], s.source_start_offset, s.source_end_offset
            out.append(
                ContextOut(
                    source_id=s.id, before=t[max(0, a - chars) : a], quote=t[a:b], after=t[b : b + chars]
                )
            )
        except Exception:  # noqa: BLE001 - context is a convenience; never fail the request over it
            log.warning("could not build evidence context", exc_info=True)
    return out


@router.get(
    "/evidence/{evidence_id}",
    response_model=Envelope[FactDetail],
    summary="One fact with its sources and model metadata",
)
def get_evidence(
    evidence_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    user: ViewUser,
    context: Annotated[
        int, Query(ge=0, le=1000, description="characters of document text around each quote")
    ] = 0,
):
    fact = session.get(EvidenceFact, evidence_id)
    if fact is None:
        raise NotFound("Evidence fact not found.")
    sources = _sources_for(session, [fact.id])[fact.id]
    detail = FactDetail(**_fact_fields(fact, sources))
    doc_id = next((s.document_id for s in sources if s.document_id), None)
    if doc_id is not None:
        d = session.get(Document, doc_id)
        if d is not None:
            detail.document = DocumentRef(
                id=d.id, document_type=d.document_type, language=d.language, created_on=d.created_on
            )
    if fact.invocation_id is not None:
        inv = session.get(ModelInvocation, fact.invocation_id)
        if inv is not None:
            detail.invocation = InvocationOut(
                task=inv.task,
                provider=inv.provider,
                model_id=inv.model_id,
                prompt_version=inv.prompt_version,
                input_tokens=inv.input_tokens,
                output_tokens=inv.output_tokens,
                cache_read_tokens=inv.cache_read_tokens,
                cost_usd=float(inv.cost_usd) if inv.cost_usd is not None else None,
                latency_ms=inv.latency_ms,
                validation_status=inv.validation_status,
                fallback_used=inv.fallback_used,
                cache_hit=inv.cache_hit,
                created_at=inv.created_at,
            )
    if fact.review_status.startswith("reviewed_"):
        ev = session.execute(
            select(AuditEvent)
            .where(
                AuditEvent.action == "evidence.review",
                AuditEvent.resource_type == "evidence_fact",
                AuditEvent.resource_id == str(fact.id),
                AuditEvent.outcome == "success",
            )
            .order_by(AuditEvent.occurred_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if ev is not None:
            detail.review = ReviewOut(
                decision=fact.review_status.removeprefix("reviewed_"),
                reviewed_at=ev.occurred_at,
                reviewer_role=ev.actor_role,
                reviewer_id=ev.actor_id if user.can(AUDIT_VIEW) else None,
                note=(ev.details or {}).get("review_note"),
            )
    if context:
        detail.context = _context(session, sources, context)
    return ok(request, detail)


# ------------------------------------------------------------------------------------------------ review
@router.post(
    "/evidence/{evidence_id}/review",
    response_model=Envelope[ReviewResult],
    summary="Accept or reject a queued fact",
)
def review_evidence(
    evidence_id: uuid.UUID, body: ReviewIn, request: Request, session: SessionDep, user: ReviewUser
):
    fact, previous, changed = service.review_fact(session, evidence_id, body.decision)
    if changed:
        audit.record(
            session,
            user.actor,
            "evidence.review",
            "evidence_fact",
            fact.id,
            "success",
            {
                "decision": body.decision,
                "previous_validation_status": previous,
                "category": fact.category,
                "review_note": (body.review_note or "")[:1000] or None,
            },
        )
    session.commit()
    return ok(
        request,
        ReviewResult(
            id=fact.id,
            validation_status=fact.validation_status,
            review_status=fact.review_status,
            previous_validation_status=previous,
            changed=changed,
        ),
    )


# ------------------------------------------------------------------------------------------------- jobs
def _submit(
    request: Request,
    response: Response,
    session: Session,
    user: CurrentUser,
    job_type: str,
    body: ExtractionJobIn | EmbeddingJobIn,
    kind: str,
    audit_action: str,
) -> Envelope[JobSubmitted]:
    if body.is_empty:
        raise ValidationFailed("Select documents with document_ids, resident_ids or limit.")
    scope = body.model_dump(mode="json", exclude={"idempotency_key"})
    pending_only = not getattr(body, "force", False)
    count = service.count_documents(session, body, kind=kind, pending_only=pending_only)
    result = submit_job(
        session,
        job_type,
        scope,
        body.idempotency_key,
        created_by=user.id,
        config_version=PROMPT_REF if kind == "extract" else None,
    )
    if result.created:
        audit.record(
            session,
            user.actor,
            audit_action,
            "processing_job",
            result.job.id,
            "success",
            {"documents": count, "route": scope.get("route"), "force": scope.get("force", False)},
        )
    session.commit()
    response.status_code = 202 if result.created else 200
    return ok(
        request,
        JobSubmitted(
            job_id=result.job.id,
            job_type=job_type,
            status=result.job.status,
            created=result.created,
            document_count=count,
        ),
    )


@router.post(
    "/evidence/extraction-jobs",
    response_model=Envelope[JobSubmitted],
    status_code=202,
    summary="Queue an evidence extraction job",
)
def submit_extraction(
    body: ExtractionJobIn, request: Request, response: Response, session: SessionDep, user: ExecuteUser
):
    return _submit(
        request,
        response,
        session,
        user,
        "evidence.extract_batch",
        body,
        "extract",
        "evidence.extraction.submit",
    )


@router.post(
    "/evidence/embedding-jobs",
    response_model=Envelope[JobSubmitted],
    status_code=202,
    summary="Queue a document embedding job",
)
def submit_embedding(
    body: EmbeddingJobIn, request: Request, response: Response, session: SessionDep, user: ExecuteUser
):
    return _submit(
        request,
        response,
        session,
        user,
        "evidence.embed_documents",
        body,
        "embed",
        "evidence.embedding.submit",
    )
