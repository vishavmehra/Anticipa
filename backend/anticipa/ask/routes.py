# ruff: noqa: E501
"""Ask Anticipa API (analytics.view; the read-only observer role holds it).

  POST /ask               {question, history?, filters?} -> grounded answer, server built chart/table, sources, follow ups
  GET  /ask/suggestions   four suggested questions built from live aggregates (no model call)

Aggregates only: no resident-level data is reachable from here. Rate limited per user; every question writes one
`ask.query` audit event holding a hash of the question, never its text."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from anticipa.api.deps import SessionDep
from anticipa.api.envelope import Envelope, ok
from anticipa.ask import service, tools
from anticipa.ask.models import AskData, AskRequest
from anticipa.core.auth import CurrentUser, require_permission
from anticipa.core.rbac import ANALYTICS_VIEW

router = APIRouter(prefix="/ask", tags=["ask"])
AskUser = Annotated[CurrentUser, Depends(require_permission(ANALYTICS_VIEW))]


@router.post("", response_model=Envelope[AskData], summary="Ask a population analytics question (aggregates only, grounded)")
async def ask(request: Request, body: AskRequest, session: SessionDep, user: AskUser):
    return ok(request, await service.answer_question(session, user, body))


@router.get("/suggestions", summary="Suggested questions built from live aggregates (no model call)")
def suggestions(request: Request, session: SessionDep, user: AskUser):
    scope: dict[str, str | None] = {}
    out: list[dict[str, str]] = []
    geo = tools.get_geography(session, tools.GeographyArgs(), scope)["rows"]
    rated = [r for r in geo if r.get("closure_rate") is not None]
    if rated:
        low = min(rated, key=lambda r: r["closure_rate"])
        out.append({"question": f"Why is the closure rate lowest in {low['district']}, and how does it compare with other districts?", "topic": "equity"})
    prog = tools.get_programme_stats(session, tools.ProgrammeArgs(), scope)["rows"]
    if prog:
        top = max(prog, key=lambda r: r["open"])
        out.append({"question": f"What is the closure rate and planning coverage for {str(top['group']).replace('_', ' ')}?", "topic": "programmes"})
    cap = tools.get_capacity_overview(session, tools.CapacityArgs(), scope)["rows"]
    if cap:
        busy = max(cap, key=lambda r: r["utilisation"] or 0)
        out.append({"question": f"How full is clinic capacity in {busy['label']} over the next two weeks?", "topic": "capacity"})
    out.append({"question": "How many care gaps are open, and how many are waiting for outreach?", "topic": "overview"})
    return ok(request, {"suggestions": out[:4], "generated_from": "live aggregates"})
