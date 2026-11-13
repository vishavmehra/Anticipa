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
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from anticipa.ai import EmbedRequest, RouterError
from anticipa.ai.config import load_config
from anticipa.ai.models import ModelInvocation
from anticipa.ai.settings import get_ai_settings
from anticipa.api.deps import PageDep, SessionDep
from anticipa.api.envelope import Envelope, PagedEnvelope, ok, paged, paginate
from anticipa.core import audit
from anticipa.core.auth import CurrentUser, require_any_permission, require_permission
from anticipa.core.errors import (
    Conflict,
    Forbidden,
    NotFound,
    ProviderUnavailable,
    RateLimited,
    ValidationFailed,
)
from anticipa.core.models import AuditEvent
from anticipa.core.rbac import AUDIT_VIEW, EVIDENCE_REVIEW, EVIDENCE_VIEW, PROCESSING_EXECUTE
from anticipa.datafabric.models import Document, DocumentChunk, Resident
from anticipa.evidence import service
from anticipa.evidence.chunking import Chunk, chunk_text
from anticipa.evidence.cohort import (
    MAX_COHORT_RESIDENTS,
    cohort_key,
    default_cohort_id,
    docgen_difficult_residents,
    load_manifest,
    save_manifest,
    select_cohort,
)
from anticipa.evidence.estimate import escalation_cap, estimate_extraction
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
    dry_run: bool = False  # return the scope check and the cost estimate; queue nothing


class EmbeddingJobIn(service.EmbeddingScope):
    idempotency_key: str | None = Field(None, max_length=200)
    dry_run: bool = False


class JobSubmitted(BaseModel):
    job_id: uuid.UUID | None
    job_type: str
    status: str
    created: bool
    document_count: int
    dry_run: bool = False
    override_cohort: bool = False
    estimate: dict[str, Any] | None = None


class CohortIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seed: int = Field(ge=0, le=2**31 - 1)
    size: int = Field(MAX_COHORT_RESIDENTS, ge=1, le=MAX_COHORT_RESIDENTS)  # fixed at 10,000 by the owner
    dataset_version: str | None = Field(None, max_length=100)
    difficult_share: float = Field(0.20, ge=0, le=0.5)
    include_difficult: bool = (
        True  # reserve a share for residents whose documents carry planted difficult cases
    )
    cohort_id: str | None = Field(None, max_length=90)


class SearchHit(BaseModel):
    chunk_id: uuid.UUID | None  # None for full-text hits (chunks are cut on the fly, not stored)
    document_id: uuid.UUID
    resident_id: uuid.UUID
    chunk_index: int
    start_offset: int
    end_offset: int
    text: str | None
    score: float  # cosine similarity (semantic) or ts_rank_cd (full text)
    document_type: str
    language: str | None
    synthetic_status: str


class SearchOut(BaseModel):
    retrieval_only: bool = True
    notice: str = RETRIEVAL_NOTICE
    mode: Literal["fulltext", "semantic"]
    match: str | None = None  # full text: all_terms | any_term
    query_model: str | None = None
    semantic_available: bool
    scanned_documents: int | None = None
    truncated: bool = False
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
FTS_SCAN_DOCUMENTS = 500  # unscoped full-text search reads at most this many documents (resident-scoped: all)


def _ai_router(request: Request) -> Any:
    """One Model Router per API process (query embeddings only), writing to model_invocations. The Nebius
    credit ledger and the enabled-provider gate live in the router itself."""
    r = getattr(request.app.state, "ai_router", None)
    if r is None:
        r = service.make_router(request.app.state.sessionmaker, run_budget=False)
        request.app.state.ai_router = r
    return r


def _read_slice(storage_key: str, start: int, end: int) -> str | None:
    try:
        return service.get_store().read_text(storage_key)[start:end]
    except Exception:  # noqa: BLE001 - the store may be absent in a minimal deployment; text is optional
        log.warning("document text unavailable for a search hit", exc_info=True)
        return None


def _embeddings_exist(session: Session, resident_id: uuid.UUID | None) -> bool:
    stmt = select(DocumentChunk.id).where(DocumentChunk.embedding.is_not(None)).limit(1)
    if resident_id is not None:
        stmt = stmt.join(Document, Document.id == DocumentChunk.document_id).where(
            Document.resident_id == resident_id
        )
    return session.execute(stmt).first() is not None


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


_FTS_AND = text(
    "SELECT u.i AS i, ts_rank_cd(to_tsvector('simple', u.t), q.q) AS rank "
    "FROM unnest(CAST(:texts AS text[])) WITH ORDINALITY AS u(t, i), plainto_tsquery('simple', :q) AS q(q) "
    "WHERE to_tsvector('simple', u.t) @@ q.q ORDER BY rank DESC, u.i LIMIT :limit"
)
# Same tokenisation (PostgreSQL's own parser), terms OR-ed instead of AND-ed: used when no chunk has
# every term.
_FTS_OR = text(
    "SELECT u.i AS i, ts_rank_cd(to_tsvector('simple', u.t), q.q) AS rank "
    "FROM unnest(CAST(:texts AS text[])) WITH ORDINALITY AS u(t, i), "
    "to_tsquery('simple', replace(plainto_tsquery('simple', :q)::text, '&', '|')) AS q(q) "
    "WHERE to_tsvector('simple', u.t) @@ q.q ORDER BY rank DESC, u.i LIMIT :limit"
)


def _fulltext(
    session: Session, q: str, resident_id: uuid.UUID | None, limit: int, scan_limit: int
) -> tuple[list[SearchHit], str | None, int, bool]:
    """Deterministic PostgreSQL full-text search over document chunks. The 'simple' configuration does
    no stemming
    and drops no stop words, so it behaves the same for English, Hindi and Arabic. Chunks are cut on the
    fly with
    their offsets (no stored text, no embeddings, no provider call); PostgreSQL tokenises and ranks them."""
    stmt = select(
        Document.id, Document.resident_id, Document.document_type, Document.language, Document.storage_key,
        Document.synthetic_status,
    ).order_by(Document.id).limit(scan_limit + 1)  # fmt: skip
    if resident_id is not None:
        stmt = stmt.where(Document.resident_id == resident_id)
    docs = session.execute(stmt).all()
    truncated = len(docs) > scan_limit
    docs = docs[:scan_limit]
    store = service.get_store()
    cuts: list[tuple[Any, Chunk, str]] = []
    for d in docs:
        try:
            doc_text = store.read_text(d.storage_key)
        except Exception:  # noqa: BLE001 - one unreadable document must not break search
            log.warning("full-text search skipped an unreadable document")
            continue
        cuts.extend((d, c, doc_text) for c in chunk_text(doc_text))
    if not cuts:
        return [], None, len(docs), truncated
    texts = [c.text for _, c, _ in cuts]
    match, rows = "all_terms", session.execute(_FTS_AND, {"texts": texts, "q": q, "limit": limit}).all()
    if not rows:
        match, rows = "any_term", session.execute(_FTS_OR, {"texts": texts, "q": q, "limit": limit}).all()
    hits = []
    for r in rows:
        d, c, _ = cuts[r.i - 1]  # WITH ORDINALITY is 1-based
        hits.append(
            SearchHit(
                chunk_id=None,
                document_id=d.id,
                resident_id=d.resident_id,
                chunk_index=c.index,
                start_offset=c.start,
                end_offset=c.end,
                text=c.text,
                score=round(float(r.rank), 6),
                document_type=d.document_type,
                language=d.language,
                synthetic_status=d.synthetic_status,
            )
        )
    return hits, (match if hits else None), len(docs), truncated


@router.get(
    "/evidence/search",
    response_model=Envelope[SearchOut],
    summary="Chunk retrieval: full-text by default, semantic only when opted in (never eligibility)",
)
async def search_evidence(
    request: Request,
    session: SessionDep,
    user: ViewUser,
    q: Annotated[str, Query(min_length=2, max_length=500)],
    resident_id: uuid.UUID | None = None,
    mode: Annotated[Literal["auto", "fulltext", "semantic"], Query()] = "auto",
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
):
    """`auto` = semantic when Nebius is enabled, embeddings exist and the caller may trigger AI, else
    full text.
    The app works fully without Nebius; full text never calls a provider (so every `*.view` role can use
    it)."""
    enabled = "nebius" in get_ai_settings().enabled_providers
    may_trigger_ai = user.can(EVIDENCE_REVIEW) or user.can(PROCESSING_EXECUTE)
    have_vectors = enabled and await run_in_threadpool(_embeddings_exist, session, resident_id)
    if mode == "semantic":
        if not enabled:
            raise Conflict(
                "Semantic search is disabled (opt in with ANTICIPA_ENABLE_NEBIUS=1); use mode=fulltext."
            )
        if not may_trigger_ai:
            raise Forbidden()
        if not have_vectors:
            raise Conflict("No embeddings exist for this scope; use mode=fulltext.")
    semantic = mode == "semantic" or (mode == "auto" and have_vectors and may_trigger_ai)
    if not semantic:
        hits, match, scanned, truncated = await run_in_threadpool(
            _fulltext, session, q, resident_id, limit, FTS_SCAN_DOCUMENTS
        )
        return ok(
            request,
            SearchOut(
                mode="fulltext",
                match=match,
                semantic_available=bool(have_vectors),
                scanned_documents=scanned,
                truncated=truncated,
                hits=hits,
            ),
        )  # fmt: skip
    try:
        res = await _ai_router(request).embed(
            EmbedRequest(task="embedding.document", texts=[q], metadata={"purpose": "evidence_search"})
        )
    except RouterError as e:
        if e.category == "budget_exceeded":
            raise RateLimited("The AI budget or Nebius credit limit is reached; use mode=fulltext.") from None
        log.warning("search embedding failed: %s", e.category)
        raise ProviderUnavailable("The embedding provider is unavailable; use mode=fulltext.") from None
    hits = await run_in_threadpool(_search, session, res.vectors[0], resident_id, limit)
    return ok(
        request, SearchOut(mode="semantic", query_model=res.model_id, semantic_available=True, hits=hits)
    )


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


# ------------------------------------------------------------------------------------------------- cohort
def _cohort_scope(session: Session, body: service.DocumentSelector) -> list[uuid.UUID] | None:
    """The cohort's residents (None with the override); explicit ids outside the cohort are refused."""
    residents = service.load_cohort_residents(body, service.get_store())
    if residents is not None:
        stray = service.outside_cohort(session, body, residents)
        if stray:
            raise ValidationFailed(
                f"{stray} selected document(s)/resident(s) are outside AI cohort {body.cohort_id!r}; "
                "pass override_cohort=true (audited) to go beyond the cohort."
            )
    return residents


@router.post(
    "/evidence/cohorts",
    response_model=Envelope[dict[str, Any]],
    summary="Create (or re-read) the fixed AI cohort manifest: seeded, stratified, at most 10,000 residents",
)
def create_cohort(
    body: CohortIn, request: Request, response: Response, session: SessionDep, user: ExecuteUser
):
    cohort_id = body.cohort_id or default_cohort_id(body.seed, body.size, body.dataset_version)
    try:
        cohort_key(cohort_id)
    except ValueError as e:
        raise ValidationFailed(str(e)) from None
    store = service.get_store()
    try:
        existing = load_manifest(store, cohort_id)
    except FileNotFoundError:
        existing = None
    except ValueError as e:
        raise Conflict(str(e)) from None
    difficult = (
        docgen_difficult_residents(body.dataset_version)
        if body.include_difficult and body.dataset_version
        else None
    )
    manifest = select_cohort(
        session,
        seed=body.seed,
        size=body.size,
        dataset_version=body.dataset_version,
        difficult_resident_ids=difficult,
        difficult_share=body.difficult_share,
        cohort_id=cohort_id,
        now=existing.created_at if existing else None,
    )
    if existing is not None:
        if existing.resident_ids_sha256 != manifest.resident_ids_sha256:
            raise Conflict(
                f"Cohort {cohort_id!r} already exists with a different resident list "
                "(the population changed). "
                "Choose a new cohort_id; a registered cohort is never silently replaced."
            )
        response.status_code = 200
        return ok(request, existing.summary())
    save_manifest(store, manifest)
    audit.record(
        session, user.actor, "evidence.cohort.create", "ai_cohort", cohort_id, "success",
        {"size": manifest.size, "seed": manifest.seed, "sha256": manifest.resident_ids_sha256,
         "dataset_version": manifest.dataset_version, "difficult_count": manifest.difficult_count},
    )  # fmt: skip
    session.commit()
    response.status_code = 201
    return ok(request, manifest.summary())


@router.get(
    "/evidence/cohorts/{cohort_id}",
    response_model=Envelope[dict[str, Any]],
    summary="AI cohort manifest summary",
)
def get_cohort(cohort_id: str, request: Request, _user: ViewUser):
    try:
        manifest = load_manifest(service.get_store(), cohort_id)
    except FileNotFoundError:
        raise NotFound("AI cohort not found.") from None
    except ValueError as e:
        raise ValidationFailed(str(e)) from None
    return ok(request, manifest.summary())


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
        raise ValidationFailed("Select documents with cohort_id, document_ids, resident_ids or limit.")
    settings = get_ai_settings()
    if kind == "embed" and "nebius" not in settings.enabled_providers:
        raise Conflict(
            "Embeddings are disabled (opt in with ANTICIPA_ENABLE_NEBIUS=1); search uses full text."
        )
    residents = _cohort_scope(session, body)
    pending_only = not getattr(body, "force", False)
    count = service.count_documents(session, body, kind=kind, pending_only=pending_only, residents=residents)
    cfg = load_config()
    if kind == "extract":
        cohort_docs = (
            service.count_documents(
                session, service.DocumentSelector(), pending_only=False, residents=residents
            )
            if residents is not None
            else count
        )
        estimate = estimate_extraction(
            cfg,
            count,
            escalation_cap(cohort_docs, 0, settings.anticipa_ai_escalation_share) if body.escalate else 0,
            run_budget=settings.anticipa_ai_run_budget_usd,
        )
        if not estimate["within_run_budget"]:
            raise Conflict(
                f"Expected cost ${estimate['expected_total_usd']} exceeds the per-run budget "
                f"${settings.anticipa_ai_run_budget_usd} (ANTICIPA_AI_RUN_BUDGET_USD). "
                "Split the run with `limit` or "
                "raise the budget deliberately."
            )
    else:
        estimate = service.estimate_embedding_scope(session, body, residents, service.get_store(), cfg)
        approved = (
            body.approve_cost_usd
            if body.approve_cost_usd is not None
            else settings.anticipa_nebius_run_approved_usd
        )
        if not body.dry_run and estimate["unique_texts_to_embed"]:
            need = Decimal(estimate["estimated_cost_usd"])
            if approved is None or approved < need:
                raise Conflict(
                    f"Cost approval required: estimate ${need} for "
                    f"{estimate['unique_texts_to_embed']} unique texts "
                    "(send approve_cost_usd >= estimate, or set ANTICIPA_NEBIUS_RUN_APPROVED_USD)."
                )
    log.info("%s submission estimate: %s", job_type, estimate)
    if body.dry_run:
        response.status_code = 200  # nothing was queued
        return ok(
            request,
            JobSubmitted(
                job_id=None,
                job_type=job_type,
                status="dry_run",
                created=False,
                document_count=count,
                dry_run=True,
                override_cohort=body.override_cohort,
                estimate=estimate,
            ),
        )
    scope = body.model_dump(mode="json", exclude={"idempotency_key", "dry_run"})
    result = submit_job(
        session, job_type, scope, body.idempotency_key, created_by=user.id,
        config_version=PROMPT_REF if kind == "extract" else None,
    )  # fmt: skip
    if result.created:
        audit.record(
            session,
            user.actor,
            audit_action,
            "processing_job",
            result.job.id,
            "success",
            {
                "documents": count,
                "cohort_id": body.cohort_id,
                "override_cohort": body.override_cohort,
                "force": scope.get("force", False),
                "estimate": {
                    k: estimate.get(k) for k in ("expected_total_usd", "estimated_cost_usd") if k in estimate
                },
            },
        )
        if body.override_cohort:  # going beyond the fixed AI cohort is its own audited event
            audit.record(
                session,
                user.actor,
                "evidence.cohort.override",
                "processing_job",
                result.job.id,
                "success",
                {"job_type": job_type, "documents": count},
            )
            log.warning(
                "AI cohort override used: job=%s type=%s documents=%s", result.job.id, job_type, count
            )
    session.commit()
    response.status_code = 202 if result.created else 200
    return ok(
        request,
        JobSubmitted(
            job_id=result.job.id, job_type=job_type, status=result.job.status, created=result.created,
            document_count=count, override_cohort=body.override_cohort, estimate=estimate,
        ),
    )  # fmt: skip


@router.post(
    "/evidence/extraction-jobs",
    response_model=Envelope[JobSubmitted],
    status_code=202,
    summary="Queue an evidence extraction job on the AI cohort (Haiku; <= 10% Sonnet escalation)",
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
    summary="Queue an OPT-IN embedding job (Nebius: needs ANTICIPA_ENABLE_NEBIUS=1 and a cost approval)",
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
