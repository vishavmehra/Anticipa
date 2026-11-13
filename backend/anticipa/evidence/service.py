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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.orm import Session

from anticipa.core.errors import InvalidTransition, NotFound
from anticipa.datafabric.models import Document, DocumentChunk
from anticipa.evidence.extract import ExtractionOutcome
from anticipa.evidence.models import EvidenceFact, EvidenceSource
from anticipa.evidence.normalise import apply_to_value, normalise, section_heading

EXTRACTION_METHOD = "ai_assisted"  # 04 §9.1: structured | ai_assisted | other
MAX_DOCUMENTS_PER_JOB = 100_000
MACHINE_REVIEW_STATES = ("not_required", "pending")  # review_status of facts a re-extraction may replace
VERIFIED_STATUSES = ("verified_exact", "verified_normalized")


class DocumentSelector(BaseModel):
    """Which documents a job covers. Explicit ids win; a bare `limit`/filter = "the next N unprocessed".

    Every job is bound to a registered AI cohort (`cohort_id`, see anticipa.evidence.cohort): only
    documents of
    the cohort's residents may be processed. `override_cohort` lifts that, is audited at submission, and
    is the
    only way to go beyond the fixed 10,000-resident cohort."""

    model_config = ConfigDict(extra="forbid")

    cohort_id: str | None = Field(None, max_length=90)
    override_cohort: bool = False
    document_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_DOCUMENTS_PER_JOB)
    resident_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_DOCUMENTS_PER_JOB)
    document_type: str | None = Field(None, max_length=100)
    language: str | None = Field(None, max_length=20)
    limit: int | None = Field(None, ge=1, le=MAX_DOCUMENTS_PER_JOB)

    @property
    def is_empty(self) -> bool:
        return not (self.document_ids or self.resident_ids or self.limit or self.cohort_id)


class ExtractionScope(DocumentSelector):
    prompt_ref: str = Field(
        "evidence.extract@1", pattern=r"^evidence\.extract@[0-9]+$"
    )  # versioned prompt file
    escalate: bool = (
        True  # Haiku first; deterministic triggers may re-run <= 10% of cohort documents on Sonnet
    )
    force: bool = False  # re-extract even when (document hash, prompt, model) was already processed
    concurrency: int = Field(8, ge=1, le=16)


class EmbeddingScope(DocumentSelector):
    concurrency: int = Field(4, ge=1, le=8)
    # Nebius spend approval (USD): must be >= the estimate computed from the selected texts. Falls back to
    # ANTICIPA_NEBIUS_RUN_APPROVED_USD. The run hard-stops when this amount is reached.
    approve_cost_usd: Decimal | None = Field(None, ge=0)


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


def _filter(
    stmt: Select,
    scope: DocumentSelector,
    *,
    kind: str,
    pending_only: bool,
    residents: Sequence[uuid.UUID] | None,
) -> Select:
    if scope.document_ids:
        stmt = stmt.where(Document.id.in_(scope.document_ids))
    if scope.resident_ids:
        stmt = stmt.where(Document.resident_id.in_(scope.resident_ids))
    if residents is not None:  # the AI cohort: no document of any other resident is ever selected
        stmt = stmt.where(Document.resident_id.in_(residents))
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
    session: Session,
    scope: DocumentSelector,
    *,
    kind: str = "extract",
    pending_only: bool = True,
    residents: Sequence[uuid.UUID] | None = None,
) -> int:
    stmt = _filter(
        select(func.count(Document.id)), scope, kind=kind, pending_only=pending_only, residents=residents
    )
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
    residents: Sequence[uuid.UUID] | None = None,
) -> list[DocRow]:
    """Keyset page (ordered by id) of the documents in scope. kind: "extract" | "embed"."""
    stmt = _filter(select(*_COLUMNS), scope, kind=kind, pending_only=pending_only, residents=residents)
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
    try:
        doc_date = date.fromisoformat(doc.created_on) if doc.created_on else None
    except ValueError:
        doc_date = None
    for vf in outcome.facts:
        c, ver = vf.candidate, vf.verification
        fact_id = uuid.uuid4()
        value = _fact_value(c)
        if vf.reasons:
            value["review_reasons"] = list(vf.reasons)
        first = ver.spans[0] if ver.verified and ver.spans else None
        norm = normalise(
            c.category, c.temporal_status, c.assertion_status, c.fact_type, value, doc_date,
            subject_type=c.subject_type, heading=section_heading(text, first[0]) if first else None,
        )  # fmt: skip
        value = apply_to_value(
            value, norm, c.category, c.temporal_status
        )  # keeps the model's originals auditable
        session.add(
            EvidenceFact(
                id=fact_id,
                resident_id=doc.resident_id,
                subject_type=c.subject_type,
                category=norm.category,
                fact_type=c.fact_type,
                value=value,
                assertion_status=c.assertion_status,
                temporal_status=norm.temporal_status,
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


def make_router(
    sessions: Any, *, run_budget: bool | Decimal = True, initial_run_spend: Decimal = Decimal(0)
) -> Any:
    """A Model Router that writes every invocation to model_invocations, enforces the daily budget and
    (for job
    runs) the per-run budget, replays cached outputs, and only routes to enabled providers."""
    from anticipa.ai.router import build_router
    from anticipa.ai.sinks import DbSink

    return build_router(sink=DbSink(sessions), run_budget=run_budget, initial_run_spend=initial_run_spend)


# ------------------------------------------------------------------------------------------------- cohort
def load_cohort_residents(scope: DocumentSelector, store: Any) -> list[uuid.UUID] | None:
    """The residents a job may touch: the cohort manifest's list, or None when the (audited) override applies.

    Raises ValidationFailed when no cohort is named and there is no override, NotFound for an unknown
    cohort."""
    from anticipa.core.errors import ValidationFailed
    from anticipa.evidence.cohort import load_manifest

    if scope.override_cohort:
        return None
    if not scope.cohort_id:
        raise ValidationFailed(
            "Name an AI cohort (cohort_id; create one with POST /evidence/cohorts) "
            "or pass override_cohort=true."
        )
    try:
        return load_manifest(store, scope.cohort_id).resident_ids
    except FileNotFoundError:
        raise NotFound(f"AI cohort {scope.cohort_id!r} does not exist.") from None
    except ValueError as e:
        raise ValidationFailed(str(e)) from None


def outside_cohort(session: Session, scope: DocumentSelector, residents: Sequence[uuid.UUID]) -> int:
    """How many explicitly selected documents/residents fall outside the cohort."""
    n = 0
    if scope.document_ids:
        n += session.execute(
            select(func.count(Document.id)).where(
                Document.id.in_(scope.document_ids), Document.resident_id.not_in(residents)
            )
        ).scalar_one()
    if scope.resident_ids:
        n += len(set(scope.resident_ids) - set(residents))
    return n


# ------------------------------------------------------------------------------------- embedding estimate
def estimate_embedding_scope(
    session: Session,
    scope: DocumentSelector,
    residents: Sequence[uuid.UUID] | None,
    store: Any,
    cfg: Any,
    *,
    page: int = 200,
) -> dict[str, Any]:
    """Read-only twin of the embedding job's planning pass: which UNIQUE chunk texts still lack an
    embedding for
    this model, and what embedding them would cost (tokens estimated from the actual texts). No provider
    call and
    no write, so it is safe for a dry run and for the approval check at submission."""
    from anticipa.evidence.chunking import chunk_text
    from anticipa.evidence.estimate import estimate_embedding

    route = cfg.tasks["embedding.document"]
    model_id = route.candidates[0].model_id
    unique: dict[str, str] = {}
    documents = empty = unreadable = already = 0
    after, selected = None, 0
    while not (scope.limit and selected >= scope.limit):
        n = min(page, scope.limit - selected) if scope.limit else page
        rows = select_documents(session, scope, after=after, batch=n, kind="embed", residents=residents)
        if not rows:
            break
        after, selected = rows[-1].id, selected + len(rows)
        per_doc: list[list[Any]] = []
        for doc in rows:
            documents += 1
            try:
                chunks = chunk_text(store.read_text(doc.storage_key))
            except Exception:  # noqa: BLE001 - the job marks it failed; the estimate just skips it
                unreadable += 1
                continue
            if not chunks:
                empty += 1
            per_doc.append(chunks)
        hashes = {c.content_hash for chunks in per_doc for c in chunks}
        have = set()
        if hashes:
            have = set(
                session.execute(
                    select(DocumentChunk.content_hash)
                    .where(
                        DocumentChunk.content_hash.in_(hashes),
                        DocumentChunk.embedding_model == model_id,
                        DocumentChunk.embedding.is_not(None),
                    )
                    .distinct()
                ).scalars()
            )
        for chunks in per_doc:
            for c in chunks:
                if c.content_hash in have:
                    already += 1
                else:
                    unique.setdefault(c.content_hash, c.text)
    est = estimate_embedding(cfg, list(unique.values()))
    return {
        **est,
        "documents": documents,
        "empty_documents": empty,
        "unreadable_documents": unreadable,
        "chunks_already_embedded": already,
        "unique_texts_to_embed": len(unique),
    }
