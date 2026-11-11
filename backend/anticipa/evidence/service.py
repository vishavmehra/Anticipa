"""Evidence persistence, document selection and human review (sync SQLAlchemy; used by jobs and routes).

Rules enforced here (CLAUDE.md §10, docs/17 §6):
- every extracted statement is persisted with its verification outcome, nothing is silently dropped;
- only facts whose quote verified exactly/normalised can be human-accepted; a fact whose quote was not found
  stays
  `rejected` and cannot be revived by a reviewer;
- re-extraction replaces the document's previous *machine* facts atomically but never a human-reviewed one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.orm import Session

from anticipa.core.errors import InvalidTransition, NotFound
from anticipa.datafabric.models import Document, DocumentChunk
from anticipa.evidence.extract import ExtractionOutcome
from anticipa.evidence.models import EvidenceFact, EvidenceSource

EXTRACTION_METHOD = "ai_assisted"  # 04 §9.1: structured | ai_assisted | other
MAX_DOCUMENTS_PER_JOB = 100_000
MACHINE_REVIEW_STATES = ("not_required", "pending")  # review_status of facts a re-extraction may replace
VERIFIED_STATUSES = ("verified_exact", "verified_normalized")
# Family-history / ambiguous document types go to the stronger route in `auto` mode.
COMPLEX_TYPE_MARKERS = ("family", "ambiguous")


class DocumentSelector(BaseModel):
    """Which documents a job covers. Explicit ids win; a bare `limit`/filter = "the next N unprocessed"."""

    model_config = ConfigDict(extra="forbid")

    document_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_DOCUMENTS_PER_JOB)
    resident_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_DOCUMENTS_PER_JOB)
    document_type: str | None = Field(None, max_length=100)
    language: str | None = Field(None, max_length=20)
    limit: int | None = Field(None, ge=1, le=MAX_DOCUMENTS_PER_JOB)

    @property
    def is_empty(self) -> bool:
        return not (self.document_ids or self.resident_ids or self.limit)


class ExtractionScope(DocumentSelector):
    route: Literal["auto", "standard", "complex"] = "auto"
    force: bool = False  # re-extract even when (document hash, prompt, model) was already processed
    concurrency: int = Field(8, ge=1, le=16)


class EmbeddingScope(DocumentSelector):
    concurrency: int = Field(4, ge=1, le=8)


@dataclass(frozen=True)
class DocRow:
    id: uuid.UUID
    resident_id: uuid.UUID
    document_type: str
    language: str | None
    storage_key: str
    content_hash: str
    created_on: str | None
    synthetic_status: str


_COLUMNS = (
    Document.id,
    Document.resident_id,
    Document.document_type,
    Document.language,
    Document.storage_key,
    Document.content_hash,
    Document.created_on,
    Document.synthetic_status,
)


def pick_task(document_type: str, route: str) -> str:
    """Deterministic route choice; model output never influences it."""
    if route == "complex" or (
        route == "auto" and any(m in document_type.lower() for m in COMPLEX_TYPE_MARKERS)
    ):
        return "evidence.extract.complex"
    return "evidence.extract"


def _filter(stmt: Select, scope: DocumentSelector, *, kind: str, pending_only: bool) -> Select:
    if scope.document_ids:
        stmt = stmt.where(Document.id.in_(scope.document_ids))
    if scope.resident_ids:
        stmt = stmt.where(Document.resident_id.in_(scope.resident_ids))
    if scope.document_type:
        stmt = stmt.where(Document.document_type == scope.document_type)
    if scope.language:
        stmt = stmt.where(Document.language == scope.language)
    if pending_only and not (
        scope.document_ids or scope.resident_ids
    ):  # cohort mode: "the next N unprocessed"
        if kind == "extract":
            stmt = stmt.where(Document.processing_status != "extracted")
        else:
            embedded = select(DocumentChunk.id).where(
                DocumentChunk.document_id == Document.id, DocumentChunk.embedding.is_not(None)
            )
            stmt = stmt.where(~embedded.exists())
    return stmt


def count_documents(
    session: Session, scope: DocumentSelector, *, kind: str = "extract", pending_only: bool = True
) -> int:
    stmt = _filter(select(func.count(Document.id)), scope, kind=kind, pending_only=pending_only)
    n = session.execute(stmt).scalar_one()
    return min(n, scope.limit) if scope.limit else n


def select_documents(
    session: Session,
    scope: DocumentSelector,
    *,
    after: uuid.UUID | None,
    batch: int,
    kind: str = "extract",
    pending_only: bool = True,
) -> list[DocRow]:
    """Keyset page (ordered by id) of the documents in scope. kind: "extract" | "embed"."""
    stmt = _filter(select(*_COLUMNS), scope, kind=kind, pending_only=pending_only)
    stmt = stmt.order_by(Document.id).limit(batch)
    if after is not None:
        stmt = stmt.where(Document.id > after)
    return [
        DocRow(
            r.id,
            r.resident_id,
            r.document_type,
            r.language,
            r.storage_key,
            r.content_hash,
            r.created_on.date().isoformat() if r.created_on else None,
            r.synthetic_status,
        )  # fmt: skip
        for r in session.execute(stmt)
    ]


@dataclass
class PersistResult:
    facts: int = 0
    accepted: int = 0
    requires_review: int = 0
    rejected: int = 0
    replaced: int = 0


def _fact_value(c: Any) -> dict[str, Any]:
    v = c.value
    return {
        "concept": v.concept,
        "original_text": v.original_text,
        "detail": v.detail,
        "numeric_value": v.numeric_value,
        "unit": v.unit,
        "event_date": v.event_date,
        "age_at_onset": c.age_at_onset,
    }


def persist_outcome(session: Session, doc: DocRow, outcome: ExtractionOutcome, text: str) -> PersistResult:
    """Replace the document's machine facts with this extraction's (caller commits).

    `source_quote` is the exact slice of the ORIGINAL document at the verified span, so a normalised match
    still
    stores the document's own wording; unverified quotes keep the model's text and no offsets."""
    assert outcome.status == "completed"
    prior = select(EvidenceSource.evidence_fact_id).where(EvidenceSource.document_id == doc.id)
    replaced = session.execute(
        delete(EvidenceFact).where(
            EvidenceFact.id.in_(prior), EvidenceFact.review_status.in_(MACHINE_REVIEW_STATES)
        )
    ).rowcount
    result = PersistResult(replaced=replaced)
    invocation_id = uuid.UUID(outcome.invocation_id) if outcome.invocation_id else None
    for vf in outcome.facts:
        c, ver = vf.candidate, vf.verification
        fact_id = uuid.uuid4()
        value = _fact_value(c)
        if vf.reasons:
            value["review_reasons"] = list(vf.reasons)
        session.add(
            EvidenceFact(
                id=fact_id,
                resident_id=doc.resident_id,
                subject_type=c.subject_type,
                category=c.category,
                fact_type=c.fact_type,
                value=value,
                assertion_status=c.assertion_status,
                temporal_status=c.temporal_status,
                relationship_type=c.relationship,
                extraction_confidence=c.extraction_confidence,
                uncertainty_note=c.uncertainty_note,
                validation_status=vf.decision,
                review_status="pending" if vf.decision == "requires_review" else "not_required",
                extraction_method=EXTRACTION_METHOD,
                model_provider=outcome.provider,
                model_id=outcome.model_id,
                prompt_version=outcome.prompt_ref,
                invocation_id=invocation_id,
                synthetic_status=doc.synthetic_status,
            )
        )
        span = ver.spans[0] if ver.verified and ver.spans else None
        session.add(
            EvidenceSource(
                evidence_fact_id=fact_id,
                source_type="document",
                document_id=doc.id,
                source_quote=text[span[0] : span[1]] if span else c.source_quote,
                source_start_offset=span[0] if span else None,
                source_end_offset=span[1] if span else None,
                source_content_hash=outcome.source_content_hash,
                verification_status=ver.status,
                location_ambiguous=ver.location_ambiguous,
                all_spans=[list(s) for s in ver.spans] if ver.location_ambiguous else None,
                synthetic_status=doc.synthetic_status,
            )
        )
        result.facts += 1
        setattr(result, vf.decision, getattr(result, vf.decision) + 1)
    session.execute(update(Document).where(Document.id == doc.id).values(processing_status="extracted"))
    return result


def mark_extraction_failed(session: Session, document_id: uuid.UUID) -> None:
    session.execute(
        update(Document).where(Document.id == document_id).values(processing_status="extraction_failed")
    )


# --------------------------------------------------------------------------------------------- human review
Decision = Literal["accepted", "rejected"]


def review_fact(session: Session, fact_id: uuid.UUID, decision: Decision) -> tuple[EvidenceFact, str, bool]:
    """Apply a reviewer decision to a fact that is waiting in the review queue. Caller audits and commits.

    Returns (fact, previous_validation_status, changed). Re-submitting the same decision is a no-op; anything
    else on an already-decided or never-queued fact is INVALID_TRANSITION. The original machine output (value,
    quote, offsets, model metadata) is never modified; only validation_status/review_status change."""
    fact = session.get(EvidenceFact, fact_id, with_for_update=True)
    if fact is None:
        raise NotFound("Evidence fact not found.")
    previous = fact.validation_status
    if fact.review_status == f"reviewed_{decision}":
        return fact, previous, False
    if fact.review_status != "pending" or previous != "requires_review":
        raise InvalidTransition("Only facts waiting in the review queue can be reviewed (once).")
    if decision == "accepted":
        verified = session.execute(
            select(func.count())
            .select_from(EvidenceSource)
            .where(
                EvidenceSource.evidence_fact_id == fact_id,
                EvidenceSource.verification_status.in_(VERIFIED_STATUSES),
            )
        ).scalar_one()
        if not verified:
            raise InvalidTransition(
                "The supporting quote is not verified against the source; it cannot be accepted."
            )
    fact.validation_status = decision
    fact.review_status = f"reviewed_{decision}"
    session.flush()
    return fact, previous, True


# ----------------------------------------------------------------------------------------------- factories
def get_store() -> Any:
    """The document store (docgen workstream): `read_text(storage_key) -> str`. Imported lazily so this module
    works before that workstream's file exists; tests monkeypatch this function."""
    from anticipa.datafabric.docstore import get_document_store

    return get_document_store()


def make_router(sessions: Any) -> Any:
    """A Model Router that writes every invocation to model_invocations and enforces the daily budget."""
    from anticipa.ai.router import build_router
    from anticipa.ai.sinks import DbSink

    return build_router(sink=DbSink(sessions))
