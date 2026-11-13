"""Read-only analytics APIs for the Command Center and the judge-facing screens (no AI calls):

  GET /analytics/command-center           KPIs, gaps by priority, closure trend, equity, activity, alerts
  GET /analytics/funnel                   residents -> evaluated -> due -> case -> planned -> outreach -> closed
  GET /analytics/processing               live pipeline / jobs / AI-spend monitor (cached 10 s)
  GET /analytics/impact                   proven impact: rules only baseline and first come planner baseline (stored report)
  GET /residents/{id}/journey             one payload: facts, evaluations, cases, planning, messages, closure
  GET /residents/{id}/pedigree            stored family edges with each relative's cancer history and sources
  GET /engagement/preview                 the approved reminder template rendered per language (simulated)

Journey and pedigree are resident-level reads and write one `resident.read` audit event each (details.view =
journey | pedigree), exactly like GET /residents/{id}; every other endpoint here writes nothing.

Permissions: analytics.view (or analytics.view_limited, which omits equity and recent activity) for the first
two (job and audit entries appear in the Command Center only for system.configure holders), system.configure
(system_admin only: internal jobs, pipeline and AI spend) for processing, residents.view for journey/pedigree (sections the caller lacks permission
for come back null and are listed under `restricted`) and engagement.view for the preview. The read-only
`observer` role holds all of them.
"""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.encoders import jsonable_encoder

from anticipa.analytics import geography as geo
from anticipa.analytics import impact, journey, schedule, service
from anticipa.api.deps import SessionDep
from anticipa.api.envelope import current_request_id, ok
from anticipa.core import audit
from anticipa.core.auth import CurrentUser, require_any_permission, require_permission
from anticipa.core.errors import NotFound
from anticipa.core.rbac import (
    ANALYTICS_VIEW,
    ANALYTICS_VIEW_LIMITED,
    CAPACITY_VIEW,
    DOCUMENTS_VIEW,
    ENGAGEMENT_VIEW,
    RESIDENTS_VIEW,
    SYSTEM_CONFIGURE,
)

router = APIRouter(tags=["analytics"])
OverviewUser = Annotated[CurrentUser, Depends(require_any_permission(ANALYTICS_VIEW, ANALYTICS_VIEW_LIMITED))]
AnalyticsUser = Annotated[CurrentUser, Depends(require_permission(ANALYTICS_VIEW))]
OperatorUser = Annotated[CurrentUser, Depends(require_permission(SYSTEM_CONFIGURE))]  # system_admin only: internal data
ResidentsUser = Annotated[CurrentUser, Depends(require_permission(RESIDENTS_VIEW))]
CapacityUser = Annotated[CurrentUser, Depends(require_permission(CAPACITY_VIEW))]
DocumentsUser = Annotated[CurrentUser, Depends(require_permission(DOCUMENTS_VIEW))]
ScopeUser = Annotated[CurrentUser, Depends(require_any_permission(ANALYTICS_VIEW, ANALYTICS_VIEW_LIMITED, RESIDENTS_VIEW))]
EngagementUser = Annotated[CurrentUser, Depends(require_permission(ENGAGEMENT_VIEW))]
PRODUCT_CACHE = "private, max-age=30, stale-while-revalidate=120"  # precomputed aggregates: the browser may reuse them
BADGE_CACHE = "private, max-age=15, stale-while-revalidate=60"  # the alert count: a little fresher
LIVE_CACHE = "private, no-cache"  # the processing monitor: always revalidate (a 304 saves the body)


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    tags = [t.strip().removeprefix("W/") for t in header.split(",")]
    return "*" in tags or etag.removeprefix("W/") in tags


VOLATILE_KEYS = ("generated_at", "snapshot")  # change on every recompute without the figures changing


def respond(request: Request, data: Any, cache_control: str) -> Response:
    """The standard envelope with Cache-Control and a weak content ETag (hash of the figures: not the request id, the
    generation time or the snapshot age); 304 with no body when If-None-Match matches. Per user (private, Vary)."""
    encoded = jsonable_encoder(data)
    stable = {k: v for k, v in encoded.items() if k not in VOLATILE_KEYS} if isinstance(encoded, dict) else encoded
    etag = 'W/"' + hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32] + '"'
    data_json = json.dumps(encoded, separators=(",", ":"))
    headers = {"ETag": etag, "Cache-Control": cache_control, "Vary": "Authorization"}
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    meta = json.dumps({"request_id": current_request_id(request)})
    return Response(content=f'{{"data":{data_json},"meta":{meta}}}', media_type="application/json", headers=headers)


def _audited_read(session, user: CurrentUser, resident_id: uuid.UUID, view: str, data: dict):
    """Resident-level reads are audited like GET /residents/{id} (`resident.read`), naming the view; a 404 is not."""
    audit.record(session, user.actor, "resident.read", "resident", resident_id, "success", {"view": view})
    session.commit()
    return data


@router.get("/analytics/command-center", summary="Command Center: KPIs, priority, closure trend, equity, activity, alerts")
def command_center(
    request: Request, session: SessionDep, user: OverviewUser,
    days: Annotated[int, Query(ge=1, le=90, description="closure trend window in days")] = 30,
    compare: Annotated[str | None, Query(pattern="^previous$", description="previous: change versus the period before; days must be 7, 14, 30, 60 or 90")] = None,
    district: Annotated[str | None, Query(max_length=8, description="filter every widget to one pilot district (D01 to D10)")] = None,
    state: Annotated[str | None, Query(max_length=8, description="filter every widget to a state (LGD or ISO code, e.g. 23 or MP)")] = None,
):  # fmt: skip
    data = service.command_center(
        session, limited=not user.can(ANALYTICS_VIEW), internal=user.can(SYSTEM_CONFIGURE), trend_days=days, compare=compare == "previous",
        district=district, state=state, sessions=request.app.state.sessionmaker,
    )  # fmt: skip
    return respond(request, data, PRODUCT_CACHE)


@router.get("/analytics/funnel", summary="Care gap funnel with definitions")
def funnel(
    request: Request, session: SessionDep, user: OverviewUser,
    district: Annotated[str | None, Query(max_length=8, description="pilot district D01 to D10")] = None,
    state: Annotated[str | None, Query(max_length=8, description="state (LGD or ISO code)")] = None,
    compare: Annotated[str | None, Query(pattern="^previous$", description="previous: stage counts as they stood `days` ago and the change since")] = None,
    days: Annotated[int, Query(ge=1, le=90, description="comparison period: 7, 14, 30, 60 or 90")] = 30,
):  # fmt: skip
    data = service.funnel(session, district=district, state=state, compare_days=days if compare else None, sessions=request.app.state.sessionmaker)
    return respond(request, data, PRODUCT_CACHE)


@router.get("/analytics/geography", summary="India map: states and pilot districts with care gap measures")
def geography(
    request: Request, session: SessionDep, user: AnalyticsUser,
    level: Annotated[str, Query(pattern="^(country|state|district)$")] = "country",
    code: Annotated[str | None, Query(max_length=40, description="state: LGD or ISO code (23, MP, IN-MP); district: D01 to D10")] = None,
):  # fmt: skip
    data = geo.geography(session, level=level, code=code, sessions=request.app.state.sessionmaker)
    return respond(request, data, PRODUCT_CACHE)


@router.get("/analytics/population-explorer", summary="Population by age band and sex, sliced by district, age band and sex")
def population_explorer(
    request: Request, session: SessionDep, user: AnalyticsUser,
    district: Annotated[str | None, Query(max_length=8, description="pilot district code D01 to D10")] = None,
    age_band: Annotated[str | None, Query(max_length=60, description="one or more age bands, comma separated, for example 50-59,60-69,70-79,80+")] = None,
    sex: Annotated[str | None, Query(pattern="^(female|male|other|unknown)(,(female|male|other|unknown))*$", description="one or more of female, male, other, unknown")] = None,
):  # fmt: skip
    data = geo.population_explorer(session, district=district, age_band=age_band, sex=sex, sessions=request.app.state.sessionmaker)
    return respond(request, data, PRODUCT_CACHE)


@router.get("/analytics/demand-summary", summary="Target population and appointments required for approved cases")
def demand_summary(
    request: Request, session: SessionDep, user: AnalyticsUser,
    district: Annotated[str | None, Query(max_length=8, description="pilot district D01 to D10")] = None,
    state: Annotated[str | None, Query(max_length=8, description="state (LGD or ISO code)")] = None,
):  # fmt: skip
    data = service.demand_summary(session, district=district, state=state, sessions=request.app.state.sessionmaker)
    return respond(request, data, PRODUCT_CACHE)


@router.get("/analytics/schedule", summary="Appointment schedule of one day across clinics, by service")
def appointment_schedule(
    request: Request, session: SessionDep, user: CapacityUser,
    day: Annotated[date | None, Query(description="UTC day, default today")] = None,
    district: Annotated[str | None, Query(max_length=8)] = None,
    state: Annotated[str | None, Query(max_length=8)] = None,
):  # fmt: skip
    return respond(request, schedule.schedule(session, day=day, district=district, state=state), PRODUCT_CACHE)


@router.get("/analytics/alerts/summary", summary="Active alert count for a badge")
def alerts_summary(request: Request, session: SessionDep, user: OverviewUser):
    data = service.alerts_summary(session, internal=user.can(SYSTEM_CONFIGURE), sessions=request.app.state.sessionmaker)
    return respond(request, data, BADGE_CACHE)


@router.get("/meta/scope", summary="The country, states, districts and facilities this deployment serves")
def meta_scope(request: Request, session: SessionDep, user: ScopeUser):
    return respond(request, geo.scope(session), PRODUCT_CACHE)


@router.get("/analytics/programmes", summary="Programme (care activity) statistics")
def programmes(
    request: Request, session: SessionDep, user: OverviewUser,
    district: Annotated[str | None, Query(max_length=8, description="pilot district D01 to D10")] = None,
    state: Annotated[str | None, Query(max_length=8, description="state (LGD or ISO code)")] = None,
):  # fmt: skip
    data = geo.programmes(session, district=district, state=state, sessions=request.app.state.sessionmaker)
    return respond(request, data, PRODUCT_CACHE)


@router.get("/analytics/impact", summary="Proven impact: rules only and first come baselines versus Anticipa (stored report, zero model spend)")
def impact_report(request: Request, session: SessionDep, user: AnalyticsUser):
    try:
        data = impact.load_report(session)
    except FileNotFoundError as exc:
        raise NotFound(str(exc)) from exc
    return respond(request, data, PRODUCT_CACHE)


@router.get("/analytics/processing", summary="Live processing monitor: pipeline, jobs, AI spend, documents")
def processing(
    request: Request, session: SessionDep, user: OperatorUser,
    dataset_version: Annotated[str | None, Query(pattern=r"^syn-\d{8}-s\d+$", description="default: the most recently updated manifest")] = None,
):  # fmt: skip
    return respond(request, service.processing(session, dataset_version=dataset_version), LIVE_CACHE)


@router.get("/residents/{resident_id}/journey", summary="Resident journey: evidence to closure in one payload")
def resident_journey(resident_id: uuid.UUID, request: Request, session: SessionDep, user: ResidentsUser):
    data = journey.resident_journey(session, user, resident_id)
    return ok(request, _audited_read(session, user, resident_id, "journey", data))


@router.get("/residents/{resident_id}/profile", summary="Resident profile: place and programme coverage")
def resident_profile(resident_id: uuid.UUID, request: Request, session: SessionDep, user: ResidentsUser):
    data = journey.resident_profile(session, user, resident_id)
    return ok(request, _audited_read(session, user, resident_id, "profile", data))


@router.get("/residents/{resident_id}/documents/{document_id}/text", summary="Whole note text with the evidence spans")
def resident_document_text(
    resident_id: uuid.UUID, document_id: uuid.UUID, request: Request, session: SessionDep, user: ResidentsUser, _documents: DocumentsUser
):
    data = journey.resident_document_text(session, resident_id, document_id)
    return ok(request, _audited_read(session, user, resident_id, "document", data))


@router.get("/residents/{resident_id}/pedigree", summary="Pedigree: stored relatives and their cancer history with sources")
def resident_pedigree(resident_id: uuid.UUID, request: Request, session: SessionDep, user: ResidentsUser):
    data = journey.resident_pedigree(session, user, resident_id)
    return ok(request, _audited_read(session, user, resident_id, "pedigree", data))


@router.get("/engagement/preview", summary="Approved reminder template per language (simulated; no AI calls)")
def engagement_preview(
    request: Request, session: SessionDep, user: EngagementUser, case_id: uuid.UUID,
    languages: Annotated[str | None, Query(max_length=40, description="comma-separated subset of en,hi,ar (default all)")] = None,
):  # fmt: skip
    return ok(request, journey.engagement_preview(session, case_id, journey.parse_languages(languages)))
