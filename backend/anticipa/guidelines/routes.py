"""Guideline, rule and eligibility-evaluation APIs (05 §10-11). Mounted under /api/v1 by the module loader.

Permissions: read = guidelines.view / rules.view (observer included); authoring = guidelines.approve OR
processing.execute (no dedicated `guidelines.manage` exists in rbac.py yet); approve/retire = guidelines.approve;
evaluation jobs = processing.execute. Every mutation is audited.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session

from anticipa.api.deps import PageDep, SessionDep
from anticipa.api.envelope import Envelope, PagedEnvelope, PageParams, ok, paged, paginate
from anticipa.api.jobs import JobOut, to_out
from anticipa.core import audit
from anticipa.core.auth import CurrentUser, require_any_permission, require_permission
from anticipa.core.errors import NotFound, ValidationFailed
from anticipa.core.rbac import GUIDELINES_APPROVE, GUIDELINES_VIEW, PROCESSING_EXECUTE, RULES_VIEW
from anticipa.datafabric.models import Resident
from anticipa.guidelines import service
from anticipa.guidelines.evaluator import ENGINE_VERSION, OUTCOME_CLASSES, OUTCOMES
from anticipa.guidelines.models import EligibilityEvaluation, Guideline, GuidelineVersion, RuleVersion
from anticipa.guidelines.population import MAX_CHUNK, resolve_rules
from anticipa.guidelines.rules import RuleValidationError, parse_rule_definition
from anticipa.jobs import service as jobs_service

router = APIRouter(tags=["guidelines"])
ViewGuidelines = Annotated[CurrentUser, Depends(require_permission(GUIDELINES_VIEW))]
ViewRules = Annotated[CurrentUser, Depends(require_permission(RULES_VIEW))]
Author = Annotated[CurrentUser, Depends(require_any_permission(GUIDELINES_APPROVE, PROCESSING_EXECUTE))]
Approver = Annotated[CurrentUser, Depends(require_permission(GUIDELINES_APPROVE))]
Executor = Annotated[CurrentUser, Depends(require_permission(PROCESSING_EXECUTE))]

KEY_PATTERN = r"^[a-z][a-z0-9_\-]{1,79}$"


# ---------------------------------------------------------------- schemas
class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GuidelineIn(_In):
    guideline_key: str = Field(pattern=KEY_PATTERN)
    name: str = Field(min_length=1, max_length=300)
    description: str | None = Field(None, max_length=4000)
    topic: str | None = Field(None, max_length=200)
    authoritative_source: str | None = Field(None, max_length=500)
    source_url: str | None = Field(None, max_length=2000)


class RuleIn(_In):
    rule_key: str = Field(pattern=KEY_PATTERN)
    name: str = Field(min_length=1, max_length=300)
    description: str | None = Field(None, max_length=4000)
    definition: dict[str, Any]


class RuleCreateIn(RuleIn):
    guideline_version_id: uuid.UUID


class VersionIn(_In):
    effective_from: date
    effective_until: date | None = None
    source_reference: str | None = Field(None, max_length=2000)
    source_publication_date: date | None = None
    population_scope: dict[str, Any] = Field(default_factory=dict)
    disclaimer: str | None = Field(None, max_length=1000)
    change_summary: str | None = Field(None, max_length=2000)
    rules: list[RuleIn] = Field(default_factory=list, max_length=50)


class ReasonIn(_In):
    reason: str | None = Field(None, max_length=1000)


class ValidateIn(_In):
    definition: dict[str, Any]


class ResidentScopeIn(_In):
    type: Literal["all", "dataset", "residents"] = "all"
    dataset_version: str | None = Field(None, max_length=100)
    resident_ids: list[uuid.UUID] | None = Field(None, max_length=10000)


class EvaluationJobIn(_In):
    resident_scope: ResidentScopeIn = Field(default_factory=ResidentScopeIn)
    rule_version_ids: list[uuid.UUID] | None = Field(None, max_length=200)
    guideline_version_ids: list[uuid.UUID] | None = Field(None, max_length=50)
    evaluation_mode: Literal["incremental", "full"] = "incremental"
    evaluation_date: date | None = None
    chunk_size: int = Field(1000, ge=1, le=MAX_CHUNK)
    create_cases: bool = False
    auto_approve: bool = True
    idempotency_key: str | None = Field(None, max_length=200)


class RuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    guideline_version_id: uuid.UUID
    rule_key: str
    version: int
    name: str
    description: str | None
    care_activity: str
    priority: str
    rule_language: str
    content_hash: str
    approval_status: str
    created_at: datetime
    rule_definition: dict[str, Any] | None = None


class VersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    guideline_id: uuid.UUID
    version: int
    review_status: str
    source_reference: str | None
    source_publication_date: date | None
    effective_from: date
    effective_until: date | None
    population_scope: dict[str, Any]
    disclaimer: str | None
    change_summary: str | None
    content_hash: str | None
    created_at: datetime
    submitted_at: datetime | None
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    retired_at: datetime | None
    retirement_reason: str | None
    rules: list[RuleOut] | None = None


class GuidelineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    guideline_key: str
    name: str
    description: str | None
    topic: str | None
    authoritative_source: str | None
    source_url: str | None
    created_at: datetime
    updated_at: datetime
    versions: list[VersionOut] = Field(default_factory=list)


class EvaluationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    resident_id: uuid.UUID
    guideline_version_id: uuid.UUID
    rule_version_id: uuid.UUID
    rule_key: str | None = None
    care_activity: str | None = None
    outcome: str
    outcome_class: str
    explanation: dict[str, Any]
    evidence_references: list[Any]
    missing_information: list[Any]
    exclusion_reasons: list[Any]
    input_hash: str
    evaluation_date: date
    evaluated_at: datetime
    execution_version: str
    synthetic_status: str


def _rule_out(r: RuleVersion, *, with_definition: bool) -> RuleOut:
    out = RuleOut.model_validate(r)
    out.rule_definition = dict(r.rule_definition) if with_definition else None
    return out


def _version_out(session: Session, v: GuidelineVersion, *, with_rules: bool) -> VersionOut:
    out = VersionOut.model_validate(v)
    if with_rules:
        out.rules = [_rule_out(r, with_definition=True) for r in service.rules_of(session, v.id)]
    return out


def _guideline_out(session: Session, g: Guideline, versions: list[GuidelineVersion] | None = None) -> GuidelineOut:
    if versions is None:
        versions = list(session.execute(select(GuidelineVersion).where(GuidelineVersion.guideline_id == g.id).order_by(GuidelineVersion.version.desc())).scalars())
    out = GuidelineOut.model_validate(g)
    out.versions = [VersionOut.model_validate(v) for v in versions]
    return out


def _evaluation_out(e: EligibilityEvaluation, rule_key: str | None, care_activity: str | None) -> EvaluationOut:
    out = EvaluationOut.model_validate(e)
    out.rule_key, out.care_activity = rule_key, care_activity
    return out


def _paged_rows(session: Session, stmt, params: PageParams) -> tuple[list[Any], int]:
    total = session.execute(select(func.count()).select_from(stmt.order_by(None).subquery())).scalar_one()
    return list(session.execute(stmt.limit(params.page_size).offset(params.offset)).all()), total


# ---------------------------------------------------------------- guidelines
@router.get("/guidelines", response_model=PagedEnvelope[GuidelineOut], summary="List guidelines with their versions")
def list_guidelines(
    request: Request, session: SessionDep, user: ViewGuidelines, params: PageDep,
    topic: Annotated[str | None, Query(max_length=200)] = None,
    status: Annotated[Literal["draft", "in_review", "approved", "retired"] | None, Query()] = None,
    source: Annotated[str | None, Query(max_length=200)] = None,
):  # fmt: skip
    stmt = select(Guideline).order_by(Guideline.name, Guideline.id)
    if topic:
        stmt = stmt.where(Guideline.topic == topic)
    if source:
        stmt = stmt.where(Guideline.authoritative_source.ilike(f"%{source}%"))
    if status:
        stmt = stmt.where(exists().where(and_(GuidelineVersion.guideline_id == Guideline.id, GuidelineVersion.review_status == status)))
    rows, total = paginate(session, stmt, params)
    versions: dict[uuid.UUID, list[GuidelineVersion]] = {}
    if rows:
        for v in session.execute(select(GuidelineVersion).where(GuidelineVersion.guideline_id.in_([g.id for g in rows])).order_by(GuidelineVersion.version.desc())).scalars():
            versions.setdefault(v.guideline_id, []).append(v)
    return paged(request, [_guideline_out(session, g, versions.get(g.id, [])) for g in rows], params, total)


@router.post("/guidelines", response_model=Envelope[GuidelineOut], status_code=201, summary="Create a guideline (no versions yet)")
def create_guideline(body: GuidelineIn, request: Request, session: SessionDep, user: Author):
    g = service.create_guideline(session, user.actor, **body.model_dump())
    session.commit()
    return ok(request, _guideline_out(session, g, []))


@router.get("/guidelines/{guideline_id}", response_model=Envelope[GuidelineOut], summary="Get a guideline")
def get_guideline(guideline_id: uuid.UUID, request: Request, session: SessionDep, user: ViewGuidelines):
    return ok(request, _guideline_out(session, service.get_guideline(session, guideline_id)))


@router.get("/guidelines/{guideline_id}/versions", response_model=Envelope[list[VersionOut]], summary="List versions")
def list_versions(guideline_id: uuid.UUID, request: Request, session: SessionDep, user: ViewGuidelines):
    service.get_guideline(session, guideline_id)
    versions = session.execute(select(GuidelineVersion).where(GuidelineVersion.guideline_id == guideline_id).order_by(GuidelineVersion.version.desc())).scalars()
    return ok(request, [_version_out(session, v, with_rules=False) for v in versions])


@router.post("/guidelines/{guideline_id}/versions", response_model=Envelope[VersionOut], status_code=201, summary="Create a draft version (optionally with rules)")
def create_version(guideline_id: uuid.UUID, body: VersionIn, request: Request, session: SessionDep, user: Author):
    g = service.get_guideline(session, guideline_id)
    data = body.model_dump(exclude={"rules"})
    v = service.create_version(session, user.actor, g, rules=[r.model_dump() for r in body.rules], **data)
    session.commit()
    return ok(request, _version_out(session, v, with_rules=True))


@router.get("/guidelines/{guideline_id}/versions/{version_id}", response_model=Envelope[VersionOut], summary="Get a version with its rules")
def get_version(guideline_id: uuid.UUID, version_id: uuid.UUID, request: Request, session: SessionDep, user: ViewGuidelines):
    return ok(request, _version_out(session, service.get_version(session, version_id, guideline_id), with_rules=True))


@router.post("/guidelines/{guideline_id}/versions/{version_id}/submit-review", response_model=Envelope[VersionOut], summary="Submit a draft for review")
def submit_review(guideline_id: uuid.UUID, version_id: uuid.UUID, request: Request, session: SessionDep, user: Author):
    v = service.get_version(session, version_id, guideline_id)
    service.submit_for_review(session, user.actor, v)
    session.commit()
    return ok(request, _version_out(session, v, with_rules=True))


@router.post("/guidelines/{guideline_id}/versions/{version_id}/return-to-draft", response_model=Envelope[VersionOut], summary="Send a version in review back to draft")
def return_to_draft(guideline_id: uuid.UUID, version_id: uuid.UUID, request: Request, session: SessionDep, user: Approver, body: ReasonIn | None = None):
    v = service.get_version(session, version_id, guideline_id)
    service.return_to_draft(session, user.actor, v, body.reason if body else None)
    session.commit()
    return ok(request, _version_out(session, v, with_rules=True))


@router.post("/guidelines/{guideline_id}/versions/{version_id}/approve", response_model=Envelope[VersionOut], summary="Approve a version in review (immutable afterwards)")
def approve_version(guideline_id: uuid.UUID, version_id: uuid.UUID, request: Request, session: SessionDep, user: Approver):
    v = service.get_version(session, version_id, guideline_id)
    service.approve(session, user.actor, v)
    session.commit()
    return ok(request, _version_out(session, v, with_rules=True))


@router.post("/guidelines/{guideline_id}/versions/{version_id}/retire", response_model=Envelope[VersionOut], summary="Retire an approved version")
def retire_version(guideline_id: uuid.UUID, version_id: uuid.UUID, request: Request, session: SessionDep, user: Approver, body: ReasonIn | None = None):
    v = service.get_version(session, version_id, guideline_id)
    service.retire(session, user.actor, v, body.reason if body else None)
    session.commit()
    return ok(request, _version_out(session, v, with_rules=True))


# ---------------------------------------------------------------- rules
@router.get("/rules", response_model=PagedEnvelope[RuleOut], summary="List rule versions")
def list_rules(
    request: Request, session: SessionDep, user: ViewRules, params: PageDep,
    guideline_version_id: uuid.UUID | None = None, care_activity: Annotated[str | None, Query(max_length=100)] = None,
    approval_status: Annotated[Literal["draft", "in_review", "approved", "retired"] | None, Query()] = None,
):  # fmt: skip
    stmt = select(RuleVersion).order_by(RuleVersion.care_activity, RuleVersion.rule_key, RuleVersion.created_at.desc())
    if guideline_version_id:
        stmt = stmt.where(RuleVersion.guideline_version_id == guideline_version_id)
    if care_activity:
        stmt = stmt.where(RuleVersion.care_activity == care_activity)
    if approval_status:
        stmt = stmt.where(RuleVersion.approval_status == approval_status)
    rows, total = paginate(session, stmt, params)
    return paged(request, [_rule_out(r, with_definition=False) for r in rows], params, total)


@router.post("/rules", response_model=Envelope[RuleOut], status_code=201, summary="Add a validated rule to a draft guideline version")
def create_rule(body: RuleCreateIn, request: Request, session: SessionDep, user: Author):
    v = service.get_version(session, body.guideline_version_id, lock=True)
    r = service.add_rule(session, user.actor, v, body.rule_key, body.name, body.description, body.definition)
    session.commit()
    return ok(request, _rule_out(r, with_definition=True))


@router.post("/rules/validate", response_model=Envelope[dict[str, Any]], summary="Validate a rule definition without saving it")
def validate_rule(body: ValidateIn, request: Request, user: ViewRules):
    try:
        parsed = parse_rule_definition(body.definition)
    except RuleValidationError as exc:
        return ok(request, {"valid": False, "errors": exc.errors})
    return ok(request, {"valid": True, "errors": [], "care_activity": parsed.care_activity, "content_hash": parsed.content_hash()})


@router.get("/rules/{rule_id}", response_model=Envelope[RuleOut], summary="Get a rule version with its definition")
def get_rule(rule_id: uuid.UUID, request: Request, session: SessionDep, user: ViewRules):
    return ok(request, _rule_out(service.get_rule(session, rule_id), with_definition=True))


@router.delete("/rules/{rule_id}", response_model=Envelope[dict[str, Any]], summary="Remove a rule from a draft guideline version")
def delete_rule(rule_id: uuid.UUID, request: Request, session: SessionDep, user: Author):
    service.delete_rule(session, user.actor, service.get_rule(session, rule_id))
    session.commit()
    return ok(request, {"id": str(rule_id), "deleted": True})


@router.post("/rules/evaluation-jobs", response_model=Envelope[JobOut], status_code=202, summary="Submit a population evaluation job (approved rules only)")
def submit_evaluation_job(body: EvaluationJobIn, request: Request, response: Response, session: SessionDep, user: Executor):
    on = body.evaluation_date or datetime.now(UTC).date()
    scope = body.resident_scope
    if scope.type == "dataset" and not scope.dataset_version:
        raise ValidationFailed("dataset_version is required when resident_scope.type is 'dataset'.")
    if scope.type == "residents" and not scope.resident_ids:
        raise ValidationFailed("resident_ids is required when resident_scope.type is 'residents'.")
    rules = resolve_rules(session, on, rule_version_ids=body.rule_version_ids, guideline_version_ids=body.guideline_version_ids)
    if not rules:
        raise ValidationFailed("No approved rules apply on the evaluation date.")
    input_scope: dict[str, Any] = {
        "evaluation_date": on.isoformat(), "rule_version_ids": [str(r.rule_version_id) for r in rules],
        "mode": body.evaluation_mode, "chunk_size": body.chunk_size, "create_cases": body.create_cases,
        "auto_approve": body.auto_approve,
    }  # fmt: skip
    if scope.type == "dataset":
        input_scope["dataset_version"] = scope.dataset_version
    if scope.type == "residents":
        input_scope["resident_ids"] = [str(x) for x in scope.resident_ids or []]
    key = body.idempotency_key or request.headers.get("Idempotency-Key")
    result = jobs_service.submit_job(
        session, "guidelines.evaluate_population", input_scope, key, user.id, config_version=ENGINE_VERSION
    )
    if result.created:
        audit.record(session, user.actor, "eligibility.job_submit", "processing_job", result.job.id, "success",
                     {"rule_version_ids": input_scope["rule_version_ids"], "evaluation_date": input_scope["evaluation_date"], "mode": body.evaluation_mode})  # fmt: skip
    session.commit()
    response.status_code = 202 if result.created else 200
    return ok(request, to_out(result.job, user))


# ---------------------------------------------------------------- evaluations
def _evaluation_stmt():
    return select(EligibilityEvaluation, RuleVersion.rule_key, RuleVersion.care_activity).join(
        RuleVersion, RuleVersion.id == EligibilityEvaluation.rule_version_id
    )


@router.get("/evaluations", response_model=PagedEnvelope[EvaluationOut], summary="List eligibility evaluations (newest first)")
def list_evaluations(
    request: Request, session: SessionDep, user: ViewRules, params: PageDep,
    resident_id: uuid.UUID | None = None, guideline_version_id: uuid.UUID | None = None,
    rule_version_id: uuid.UUID | None = None,
    outcome: Annotated[str | None, Query(pattern="^(" + "|".join(OUTCOMES) + ")$")] = None,
    outcome_class: Annotated[str | None, Query(pattern="^(" + "|".join(OUTCOME_CLASSES) + ")$")] = None,
):  # fmt: skip
    stmt = _evaluation_stmt().order_by(EligibilityEvaluation.evaluated_at.desc(), EligibilityEvaluation.id.desc())
    for column, value in (
        (EligibilityEvaluation.resident_id, resident_id), (EligibilityEvaluation.guideline_version_id, guideline_version_id),
        (EligibilityEvaluation.rule_version_id, rule_version_id), (EligibilityEvaluation.outcome, outcome),
        (EligibilityEvaluation.outcome_class, outcome_class),
    ):  # fmt: skip
        if value is not None:
            stmt = stmt.where(column == value)
    rows, total = _paged_rows(session, stmt, params)
    return paged(request, [_evaluation_out(*row) for row in rows], params, total)


@router.get("/evaluations/{evaluation_id}", response_model=Envelope[EvaluationOut], summary="Get an evaluation with its criterion-level explanation")
def get_evaluation(evaluation_id: uuid.UUID, request: Request, session: SessionDep, user: ViewRules):
    row = session.execute(_evaluation_stmt().where(EligibilityEvaluation.id == evaluation_id)).first()
    if row is None:
        raise NotFound("Evaluation not found.")
    return ok(request, _evaluation_out(*row))


@router.get("/residents/{resident_id}/evaluations", response_model=PagedEnvelope[EvaluationOut], summary="A resident's evaluation history")
def resident_evaluations(
    resident_id: uuid.UUID, request: Request, session: SessionDep, user: ViewRules, params: PageDep,
    guideline_version_id: uuid.UUID | None = None,
    outcome: Annotated[str | None, Query(pattern="^(" + "|".join(OUTCOMES) + ")$")] = None,
    evaluated_from: date | None = None, evaluated_to: date | None = None,
):  # fmt: skip
    if session.get(Resident, resident_id) is None:
        raise NotFound("Resident not found.")
    stmt = _evaluation_stmt().where(EligibilityEvaluation.resident_id == resident_id).order_by(
        EligibilityEvaluation.evaluated_at.desc(), EligibilityEvaluation.id.desc()
    )
    if guideline_version_id:
        stmt = stmt.where(EligibilityEvaluation.guideline_version_id == guideline_version_id)
    if outcome:
        stmt = stmt.where(EligibilityEvaluation.outcome == outcome)
    if evaluated_from:
        stmt = stmt.where(EligibilityEvaluation.evaluation_date >= evaluated_from)
    if evaluated_to:
        stmt = stmt.where(EligibilityEvaluation.evaluation_date <= evaluated_to)
    rows, total = _paged_rows(session, stmt, params)
    return paged(request, [_evaluation_out(*row) for row in rows], params, total)
