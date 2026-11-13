"""Resident journey, pedigree and engagement preview: one-payload read models for the judge-facing screens.
Read-only (these functions write nothing and call no AI; the routes audit resident reads). Sections a caller may not see (RBAC) come back as null and
are listed under `restricted`; nothing is inferred: only stored rows are returned.
"""
# ruff: noqa: E501

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from anticipa.analytics.geography import district_map
from anticipa.analytics.places import programme_label
from anticipa.caregaps.models import CareGapCase, CaseStatusHistory
from anticipa.closure.models import CompletionEvidence
from anticipa.core.auth import CurrentUser
from anticipa.core.errors import NotFound, ValidationFailed
from anticipa.core.rbac import (
    APPOINTMENTS_VIEW,
    CAPACITY_VIEW,
    CAREGAPS_VIEW,
    CLOSURE_VIEW,
    ENGAGEMENT_VIEW,
    EVIDENCE_VIEW,
    RECORDS_VIEW,
    RULES_VIEW,
)
from anticipa.datafabric.models import ClinicalRecord, Document, FamilyEdge, Resident
from anticipa.datafabric.routes import _as_of, _resident_or_404, _resident_out, age_on
from anticipa.engagement.models import (
    LANGUAGES,
    Appointment,
    CommunicationEvent,
    CommunicationTemplate,
    ResidentResponse,
)
from anticipa.engagement.templates import (
    DEFAULT_TEMPLATE_KEY,
    DIRECTION,
    VERSIONS,
    activity_label,
    render,
    select_language,
)
from anticipa.evidence import service as evidence_service
from anticipa.evidence.models import EvidenceFact, EvidenceSource
from anticipa.guidelines.models import EligibilityEvaluation, Guideline, GuidelineVersion, RuleVersion
from anticipa.planning.models import Clinic, PlannedAllocation

SYNTHETIC_BANNER = "Demonstration data only: every record is generated or simulated, and no real person is represented. Nothing here is clinical validation."
SOURCES = {
    "resident": "synthetic", "evidence": "synthetic", "evaluations": "synthetic", "cases": "synthetic",
    "allocations": "simulated", "appointments": "simulated", "communications": "simulated", "completion_evidence": "simulated",
}  # fmt: skip
MAX_FACTS = 200
MAX_DOCUMENTS = 100
MAX_EVALUATIONS = 100
MAX_CASES = 50
# A keyword filter on display text / fact type, not a clinical classification of cancer.
CANCER_PATTERN = r"(cancer|carcinoma|neoplasm|malignan|tumou?r|melanoma|lymphoma|leuk(a)?emia|sarcoma)"
DEGREE_OF = {
    "mother": 1, "father": 1, "sister": 1, "brother": 1, "daughter": 1, "son": 1,
    "maternal_grandmother": 2, "maternal_grandfather": 2, "paternal_grandmother": 2, "paternal_grandfather": 2,
    "maternal_aunt": 2, "maternal_uncle": 2, "paternal_aunt": 2, "paternal_uncle": 2,
    "granddaughter": 2, "grandson": 2, "niece": 2, "nephew": 2, "half_sister": 2, "half_brother": 2, "cousin": 3,
}  # fmt: skip
DEGREE_LABEL = {1: "first degree", 2: "second degree", 3: "third degree"}


# ------------------------------------------------------------------------------------------------ shared outputs
def _source_out(s: EvidenceSource) -> dict[str, Any]:
    return {
        "id": s.id, "document_id": s.document_id, "quote": s.source_quote, "start_offset": s.source_start_offset,
        "end_offset": s.source_end_offset, "verification_status": s.verification_status, "location_ambiguous": s.location_ambiguous,
        "all_spans": s.all_spans, "content_hash": s.source_content_hash, "source_reference": s.source_reference,
    }  # fmt: skip


def _sources_by_fact(session: Session, fact_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[EvidenceSource]]:
    out: dict[uuid.UUID, list[EvidenceSource]] = defaultdict(list)
    if fact_ids:
        for s in session.execute(
            select(EvidenceSource).where(EvidenceSource.evidence_fact_id.in_(fact_ids)).order_by(EvidenceSource.created_at, EvidenceSource.id)
        ).scalars():
            out[s.evidence_fact_id].append(s)
    return out


def _fact_out(f: EvidenceFact, sources: list[EvidenceSource]) -> dict[str, Any]:
    value = dict(f.value or {})
    reasons = value.pop("review_reasons", [])
    return {
        "id": f.id, "resident_id": f.resident_id, "category": f.category, "fact_type": f.fact_type, "value": value,
        "subject_type": f.subject_type, "relationship_type": f.relationship_type, "assertion_status": f.assertion_status,
        "temporal_status": f.temporal_status, "age_at_onset": value.get("age_at_onset"), "extraction_confidence": f.extraction_confidence,
        "uncertainty_note": f.uncertainty_note, "validation_status": f.validation_status, "review_status": f.review_status,
        "review_reasons": reasons,
        "extraction": {"method": f.extraction_method, "provider": f.model_provider, "model_id": f.model_id, "prompt_version": f.prompt_version},
        "extracted_at": f.extracted_at, "synthetic_status": f.synthetic_status, "sources": [_source_out(s) for s in sources],
    }  # fmt: skip


def _gate(user: CurrentUser, permission: str, section: str, restricted: list[str]) -> bool:
    if user.can(permission):
        return True
    restricted.append(section)
    return False


# ------------------------------------------------------------------------------------------------ journey
def resident_journey(session: Session, user: CurrentUser, resident_id: uuid.UUID) -> dict[str, Any]:
    resident, ds = _resident_or_404(session, resident_id)
    as_of = _as_of(ds)
    restricted: list[str] = []
    out: dict[str, Any] = {
        "synthetic_banner": SYNTHETIC_BANNER,
        "sources": SOURCES,
        "resident": {
            **_resident_out(resident, ds.dataset_version, as_of),
            "counts": {
                "clinical_records": session.scalar(select(func.count()).select_from(ClinicalRecord).where(ClinicalRecord.resident_id == resident_id)) or 0,
                "family_edges": session.scalar(select(func.count()).select_from(FamilyEdge).where(FamilyEdge.resident_id == resident_id)) or 0,
                "documents": session.scalar(select(func.count()).select_from(Document).where(Document.resident_id == resident_id)) or 0,
                "evidence_facts": session.scalar(select(func.count()).select_from(EvidenceFact).where(EvidenceFact.resident_id == resident_id)) or 0,
            },
        },
        "evidence": _evidence(session, resident_id) if _gate(user, EVIDENCE_VIEW, "evidence", restricted) else None,
        "evaluations": _evaluations(session, resident_id) if _gate(user, RULES_VIEW, "evaluations", restricted) else None,
        "cases": _cases(session, user, resident_id, restricted) if _gate(user, CAREGAPS_VIEW, "cases", restricted) else None,
    }
    out["restricted"] = restricted
    return out


def _evidence(session: Session, resident_id: uuid.UUID) -> dict[str, Any]:
    facts = list(session.execute(
        select(EvidenceFact).where(EvidenceFact.resident_id == resident_id).order_by(EvidenceFact.extracted_at, EvidenceFact.id).limit(MAX_FACTS + 1)
    ).scalars())  # fmt: skip
    truncated = len(facts) > MAX_FACTS
    facts = facts[:MAX_FACTS]
    sources = _sources_by_fact(session, [f.id for f in facts])
    docs = session.execute(
        select(Document).where(Document.resident_id == resident_id).order_by(Document.registered_at, Document.id).limit(MAX_DOCUMENTS)
    ).scalars().all()
    by_validation: dict[str, int] = defaultdict(int)
    for f in facts:
        by_validation[f.validation_status] += 1
    return {
        "definition": "Evidence facts extracted from this resident's documents. Every fact carries its source document id, the exact supporting quote, offsets and the deterministic quote verification status.",
        "source": "synthetic",
        "truncated": truncated,
        "counts_by_validation_status": dict(by_validation),
        "facts": [_fact_out(f, sources[f.id]) for f in facts],
        "documents": [
            {"id": d.id, "document_type": d.document_type, "language": d.language, "created_on": d.created_on, "registered_at": d.registered_at,
             "processing_status": d.processing_status, "content_hash": d.content_hash, "generation_method": d.generation_method,
             "synthetic_status": d.synthetic_status}
            for d in docs
        ],  # fmt: skip
    }


def _evaluations(session: Session, resident_id: uuid.UUID) -> dict[str, Any]:
    e = EligibilityEvaluation
    rows = session.execute(
        select(e, GuidelineVersion, Guideline, RuleVersion)
        .outerjoin(GuidelineVersion, GuidelineVersion.id == e.guideline_version_id)
        .outerjoin(Guideline, Guideline.id == GuidelineVersion.guideline_id)
        .outerjoin(RuleVersion, RuleVersion.id == e.rule_version_id)
        .where(e.resident_id == resident_id)
        .order_by(e.evaluated_at.desc(), e.id.desc())
        .limit(MAX_EVALUATIONS)
    ).all()
    seen: set[uuid.UUID] = set()
    items = []
    for ev, gv, g, rv in rows:
        latest = ev.guideline_version_id not in seen
        seen.add(ev.guideline_version_id)
        items.append({
            "id": ev.id, "is_latest": latest, "outcome": ev.outcome, "outcome_class": ev.outcome_class, "reason_code": ev.reason_code,
            "review_status": ev.review_status, "explanation": ev.explanation, "evidence_references": ev.evidence_references,
            "missing_information": ev.missing_information, "conflicting_information": ev.conflicting_information,
            "source_references": ev.source_references, "exclusion_reasons": ev.exclusion_reasons, "evaluation_date": ev.evaluation_date,
            "evaluated_at": ev.evaluated_at, "execution_version": ev.execution_version, "input_hash": ev.input_hash,
            "synthetic_status": ev.synthetic_status,
            "guideline": None if gv is None else {
                "id": gv.guideline_id, "key": g.guideline_key if g else None, "name": g.name if g else None, "version": gv.version,
                "version_id": gv.id, "care_activity": gv.care_activity, "priority": gv.priority, "review_status": gv.review_status,
                "clinical_review_status": gv.clinical_review_status, "source_reference": gv.source_reference, "disclaimer": gv.disclaimer,
                "content_hash": gv.content_hash,
            },
            "decisive_rule": None if rv is None else {"id": rv.rule_id, "rule_version_id": rv.id, "name": rv.name, "version": rv.version, "rule_type": rv.rule_type},
        })  # fmt: skip
    return {
        "definition": "Append only deterministic guideline evaluations for this resident (newest first); is_latest marks the current one per guideline version. Eligibility is decided by versioned rules, never by an LLM.",
        "source": "synthetic",
        "truncated": len(rows) == MAX_EVALUATIONS,
        "items": items,
    }


def _cases(session: Session, user: CurrentUser, resident_id: uuid.UUID, restricted: list[str]) -> dict[str, Any]:
    cases = session.execute(
        select(CareGapCase).where(CareGapCase.resident_id == resident_id).order_by(CareGapCase.opened_at, CareGapCase.id).limit(MAX_CASES)
    ).scalars().all()
    ids = [c.id for c in cases]
    history: dict[uuid.UUID, list[CaseStatusHistory]] = defaultdict(list)
    if ids:
        for h in session.execute(select(CaseStatusHistory).where(CaseStatusHistory.case_id.in_(ids)).order_by(CaseStatusHistory.occurred_at, CaseStatusHistory.id)).scalars():
            history[h.case_id].append(h)
    allocations = _allocations(session, ids) if _gate(user, CAPACITY_VIEW, "allocations", restricted) else None
    appointments = _appointments(session, ids) if _gate(user, APPOINTMENTS_VIEW, "appointments", restricted) else None
    communications = _communications(session, ids) if _gate(user, ENGAGEMENT_VIEW, "communications", restricted) else None
    evidence = _completion(session, ids) if _gate(user, CLOSURE_VIEW, "completion_evidence", restricted) else None

    def per_case(group: dict[uuid.UUID, list[dict[str, Any]]] | None, cid: uuid.UUID) -> list[dict[str, Any]] | None:
        return None if group is None else group.get(cid, [])

    return {
        "definition": "Care gap cases for this resident with their append only status history, proposed/confirmed planned allocations, appointments, simulated communications (every language) and completion evidence. Planning, outreach and closure data are simulated.",
        "source": "synthetic",
        "items": [
            {
                "id": c.id, "care_activity": c.care_activity, "status": c.status, "priority": c.priority, "source_outcome": c.source_outcome,
                "evaluation_id": c.evaluation_id, "guideline_version_id": c.guideline_version_id, "rule_version_id": c.rule_version_id,
                "planning_run_id": c.planning_run_id, "assigned": c.assigned_coordinator_id is not None, "opened_at": c.opened_at,
                "updated_at": c.updated_at, "closed_at": c.closed_at, "closure_reason": c.closure_reason, "synthetic_status": c.synthetic_status,
                "history": [
                    {"from_status": h.from_status, "to_status": h.to_status, "actor_role": h.actor_role, "reason": h.reason, "details": h.details,
                     "occurred_at": h.occurred_at}  # actor ids are not exposed
                    for h in history[c.id]
                ],
                "allocations": per_case(allocations, c.id),
                "appointments": per_case(appointments, c.id),
                "communications": per_case(communications, c.id),
                "completion_evidence": per_case(evidence, c.id),
            }
            for c in cases
        ],  # fmt: skip
    }


def _group(rows: list[tuple[uuid.UUID, dict[str, Any]]]) -> dict[uuid.UUID, list[dict[str, Any]]]:
    out: dict[uuid.UUID, list[dict[str, Any]]] = defaultdict(list)
    for case_id, item in rows:
        out[case_id].append(item)
    return out


def _clinic(c: Clinic | None) -> dict[str, Any] | None:
    return None if c is None else {"id": c.id, "key": c.clinic_key, "name": c.name, "district_code": c.district_code}


def _allocations(session: Session, case_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[dict[str, Any]]]:
    if not case_ids:
        return {}
    rows = session.execute(
        select(PlannedAllocation, Clinic).outerjoin(Clinic, Clinic.id == PlannedAllocation.clinic_id)
        .where(PlannedAllocation.care_gap_case_id.in_(case_ids)).order_by(PlannedAllocation.created_at, PlannedAllocation.id)
    ).all()  # fmt: skip
    return _group([
        (a.care_gap_case_id, {
            "id": a.id, "planning_run_id": a.planning_run_id, "clinic": _clinic(c), "service_key": a.service_key, "priority": a.priority,
            "proposed_start_time": a.proposed_start_time, "proposed_end_time": a.proposed_end_time, "allocation_status": a.allocation_status,
            "explanation": a.explanation, "confirmed_at": a.confirmed_at, "simulated": True, "synthetic_status": a.synthetic_status,
        })
        for a, c in rows
    ])  # fmt: skip


def _appointments(session: Session, case_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[dict[str, Any]]]:
    if not case_ids:
        return {}
    rows = session.execute(
        select(Appointment, Clinic).outerjoin(Clinic, Clinic.id == Appointment.clinic_id)
        .where(Appointment.care_gap_case_id.in_(case_ids)).order_by(Appointment.start_time, Appointment.id)
    ).all()  # fmt: skip
    return _group([
        (a.care_gap_case_id, {
            "id": a.id, "clinic": _clinic(c), "start_time": a.start_time, "end_time": a.end_time, "appointment_status": a.appointment_status,
            "status_reason": a.status_reason, "planned_allocation_id": a.planned_allocation_id, "simulated": True, "synthetic_status": a.synthetic_status,
        })
        for a, c in rows
    ])  # fmt: skip


def _communications(session: Session, case_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[dict[str, Any]]]:
    if not case_ids:
        return {}
    events = session.execute(
        select(CommunicationEvent).where(CommunicationEvent.care_gap_case_id.in_(case_ids)).order_by(CommunicationEvent.created_at, CommunicationEvent.id)
    ).scalars().all()
    responses: dict[uuid.UUID, list[ResidentResponse]] = defaultdict(list)
    if events:
        for r in session.execute(
            select(ResidentResponse).where(ResidentResponse.communication_event_id.in_([e.id for e in events])).order_by(ResidentResponse.received_at, ResidentResponse.id)
        ).scalars():
            responses[r.communication_event_id].append(r)
    return _group([
        (e.care_gap_case_id, {
            "id": e.id, "channel": e.channel, "language": e.language, "direction": DIRECTION.get(e.language), "template_key": e.template_key,
            "template_version": e.template_version, "body": e.body, "delivery_state": e.delivery_state, "source_type": e.source_type,
            "sent_at": e.sent_at, "delivered_at": e.delivered_at, "response_received_at": e.response_received_at, "created_at": e.created_at,
            "responses": [{"id": r.id, "response_type": r.response_type, "response_value": r.response_value, "received_at": r.received_at} for r in responses[e.id]],
            "simulated": True, "synthetic_status": e.synthetic_status,
        })
        for e in events
    ])  # fmt: skip


def _completion(session: Session, case_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[dict[str, Any]]]:
    if not case_ids:
        return {}
    rows = session.execute(
        select(CompletionEvidence).where(CompletionEvidence.care_gap_case_id.in_(case_ids)).order_by(CompletionEvidence.created_at, CompletionEvidence.id)
    ).scalars().all()
    return _group([
        (x.care_gap_case_id, {
            "id": x.id, "activity": x.activity, "source_type": x.source_type, "source_resource_type": x.source_resource_type,
            "source_resource_id": x.source_resource_id, "document_id": x.document_id, "evidence_reference": x.evidence_reference,
            "verification_status": x.verification_status, "verification_method": x.verification_method, "reviewer_note": x.reviewer_note,
            "verified_at": x.verified_at, "created_at": x.created_at, "simulated": True, "synthetic_status": x.synthetic_status,
        })
        for x in rows
    ])  # fmt: skip


# ------------------------------------------------------------------------------------------------ pedigree
def _is_cancer_fact() -> Any:
    return or_(EvidenceFact.fact_type.op("~*")(CANCER_PATTERN), EvidenceFact.value["concept"].astext.op("~*")(CANCER_PATTERN))


def _record_out(r: ClinicalRecord) -> dict[str, Any]:
    return {
        "id": r.id, "resource_type": r.resource_type, "category": r.category, "display": r.display or r.normalized_display,
        "code_system": r.code_system, "code": r.code, "status": r.status, "effective_start": r.effective_start,
        "effective_end": r.effective_end, "source_reference": r.source_reference, "synthetic_status": r.synthetic_status,
    }  # fmt: skip


def resident_pedigree(session: Session, user: CurrentUser, resident_id: uuid.UUID) -> dict[str, Any]:
    resident, ds = _resident_or_404(session, resident_id)
    as_of = _as_of(ds)
    restricted: list[str] = []
    edges = session.execute(
        select(FamilyEdge).where(FamilyEdge.resident_id == resident_id).order_by(FamilyEdge.relationship_type, FamilyEdge.created_at, FamilyEdge.id)
    ).scalars().all()
    linked_ids = [e.related_resident_id for e in edges if e.related_resident_id]
    linked: dict[uuid.UUID, Resident] = {}
    if linked_ids:
        linked = {r.id: r for r in session.execute(select(Resident).where(Resident.id.in_(linked_ids))).scalars()}

    records: dict[uuid.UUID, list[dict[str, Any]]] = defaultdict(list)
    if linked_ids and _gate(user, RECORDS_VIEW, "clinical_records", restricted):
        condition_match = or_(ClinicalRecord.display.op("~*")(CANCER_PATTERN), ClinicalRecord.normalized_display.op("~*")(CANCER_PATTERN))
        for r in session.execute(
            select(ClinicalRecord).where(ClinicalRecord.resident_id.in_(linked_ids), ClinicalRecord.category == "condition", condition_match)
            .order_by(ClinicalRecord.effective_start, ClinicalRecord.id)
        ).scalars():  # fmt: skip
            records[r.resident_id].append(_record_out(r))

    facts_by_edge: dict[uuid.UUID, list[dict[str, Any]]] = defaultdict(list)
    unattributed: list[dict[str, Any]] = []
    if _gate(user, EVIDENCE_VIEW, "evidence_facts", restricted):
        by_type: dict[str, list[FamilyEdge]] = defaultdict(list)
        for e in edges:
            by_type[e.relationship_type].append(e)
        accepted = (EvidenceFact.validation_status == "accepted", _is_cancer_fact())
        stated = list(session.execute(
            select(EvidenceFact).where(EvidenceFact.resident_id == resident_id, EvidenceFact.subject_type == "family_member", *accepted)
            .order_by(EvidenceFact.extracted_at, EvidenceFact.id).limit(MAX_FACTS)
        ).scalars())  # fmt: skip
        own = list(session.execute(
            select(EvidenceFact).where(EvidenceFact.resident_id.in_(linked_ids), EvidenceFact.subject_type == "resident", *accepted)
            .order_by(EvidenceFact.extracted_at, EvidenceFact.id).limit(MAX_FACTS)
        ).scalars()) if linked_ids else []  # fmt: skip
        sources = _sources_by_fact(session, [f.id for f in (*stated, *own)])
        edge_of_resident = {e.related_resident_id: e for e in edges if e.related_resident_id}
        for f in stated:
            item = {**_fact_out(f, sources[f.id]), "basis": "stated_in_resident_document"}
            same_type = by_type.get(f.relationship_type or "", [])
            target = edge_of_resident.get(f.subject_person_id) if f.subject_person_id else (same_type[0] if len(same_type) == 1 else None)
            if target is not None:
                facts_by_edge[target.id].append(item)
            else:
                item["reason"] = "multiple_family_edges_match_the_relationship" if len(same_type) > 1 else "no_family_edge_for_the_relationship"
                unattributed.append(item)
        for f in own:
            if (edge := edge_of_resident.get(f.resident_id)) is not None:
                facts_by_edge[edge.id].append({**_fact_out(f, sources[f.id]), "basis": "relative_own_document"})

    relatives = []
    for e in edges:
        rel = linked.get(e.related_resident_id) if e.related_resident_id else None
        degree = DEGREE_OF.get(e.relationship_type)
        relatives.append({
            "edge_id": e.id, "relationship": e.relationship_type, "degree": degree, "degree_label": DEGREE_LABEL.get(degree),
            "blood_relative": None if e.relationship_type == "other_relative" else e.relationship_type != "spouse",
            "direction": e.relationship_direction,
            "reading": f"The related person is the resident's {e.relationship_type}." if e.relationship_direction == "related_to_resident" else f"Direction {e.relationship_direction}: relationship type {e.relationship_type}.",
            "linked_resident": None if rel is None else {
                "id": rel.id, "age": age_on(rel.birth_date, as_of), "administrative_sex": rel.administrative_sex, "district_code": rel.district_code,
                "deceased": rel.deceased_date is not None,
            },
            "related_person_source_id": e.related_person_source_id, "source_type": e.source_type, "confidence_status": e.confidence_status,
            "synthetic_status": e.synthetic_status,
            "cancer_history": {
                "clinical_records": None if "clinical_records" in restricted else records.get(e.related_resident_id, []) if e.related_resident_id else [],
                "evidence_facts": None if "evidence_facts" in restricted else facts_by_edge.get(e.id, []),
            },
        })  # fmt: skip
    return {
        "synthetic_banner": SYNTHETIC_BANNER,
        "sources": {"family_edges": "synthetic", "clinical_records": "synthetic", "evidence_facts": "synthetic"},
        "resident": _resident_out(resident, ds.dataset_version, as_of),
        "definition": "Relatives are exactly the stored family_edges rows (resident to relative links built by the seeded family linker); a relative is never inferred from a document. Degree is derived from the relationship type (first = parent/sibling/child, second = grandparent/aunt/uncle/niece/nephew/grandchild/half sibling, third = cousin; spouse is not a blood relative). Cancer history = condition records whose display matches a cancer keyword filter, and accepted evidence facts (fact type or concept matches the same filter) stated in this resident's documents about that relationship or taken from the relative's own documents. Each item names its source.",
        "relatives": relatives,
        "unattributed_family_facts": None if "evidence_facts" in restricted else unattributed,
        "unattributed_note": "Accepted cancer related family member facts that cannot be tied to exactly one stored relative; shown for transparency and NOT added to the pedigree.",
        "counts": {
            "relatives": len(relatives), "linked_relatives": len(linked),
            "relatives_with_cancer_history": sum(1 for r in relatives if r["cancer_history"]["clinical_records"] or r["cancer_history"]["evidence_facts"]),
        },
        "restricted": restricted,
    }


# ------------------------------------------------------------------------------------------------ engagement preview
def parse_languages(raw: str | None) -> list[str]:
    langs = [x.strip().lower() for x in (raw or "").split(",") if x.strip()] or list(LANGUAGES)
    bad = [x for x in langs if x not in LANGUAGES]
    if bad:
        raise ValidationFailed("Unsupported language.", [{"field": "languages", "reason": f"{bad} not in {list(LANGUAGES)}"}])
    return list(dict.fromkeys(langs))


def engagement_preview(session: Session, case_id: uuid.UUID, languages: list[str]) -> dict[str, Any]:
    case = session.get(CareGapCase, case_id)
    if case is None:
        raise NotFound("Care gap case not found.")
    resident = session.get(Resident, case.resident_id)
    preferred = resident.preferred_language if resident else None
    default_language, default_source = select_language(None, preferred)
    key, version = DEFAULT_TEMPLATE_KEY, VERSIONS[DEFAULT_TEMPLATE_KEY]
    stored = {
        t.language: t
        for t in session.execute(
            select(CommunicationTemplate).where(CommunicationTemplate.template_key == key, CommunicationTemplate.version == version)
        ).scalars()
    }
    previews = []
    for lang in languages:
        body = render(key, lang, case.care_activity)
        t = stored.get(lang)
        previews.append({
            "language": lang, "direction": DIRECTION[lang], "activity_label": activity_label(case.care_activity, lang), "body": body,
            "characters": len(body), "constraints_checked": True, "is_resident_preferred_language": lang == preferred,
            "is_default_language": lang == default_language, "template_stored": t is not None, "template_approved": None if t is None else t.approved,
        })  # fmt: skip
    return {
        "care_gap_case_id": case.id, "resident_id": case.resident_id, "care_activity": case.care_activity, "template_key": key,
        "template_version": version, "resident_preferred_language": preferred, "default_language": default_language,
        "default_language_source": default_source, "previews": previews, "ai_calls": 0, "simulated": True, "synthetic_status": "simulated",
        "notice": "Simulated preview of the approved reminder template: nothing is stored, sent or drafted by an AI model.",
    }  # fmt: skip


# ------------------------------------------------------------------------------------------------ profile and evaluations
COVERAGE_OF = {
    "already_completed": ("up_to_date", "Up to date"), "eligible": ("due", "Due"), "requires_clinical_review": ("needs_review", "Needs clinical review"),
    "insufficient_evidence": ("needs_information", "Needs more information"), "not_eligible": ("not_applicable", "Not applicable"),
    "temporarily_excluded": ("deferred", "Deferred"), "evaluation_error": ("error", "Error"),
}  # fmt: skip
PROFILE_NOT_AVAILABLE = {
    "display_name": "Residents are generated without names and are identified by id.",
    "photo": "Residents are generated without photographs.",
    "risk_level": "No resident risk score or stratum exists: eligibility is decided by guideline rules.",
    "risk_score": "No resident risk score or stratum exists: eligibility is decided by guideline rules.",
    "household_size": "Residents carry no household attribute; family links are not households.",
}


def resident_profile(session: Session, user: CurrentUser, resident_id: uuid.UUID) -> dict[str, Any]:
    resident, ds = _resident_or_404(session, resident_id)
    restricted: list[str] = []
    place = next((d for d in district_map()["districts"] if d["code"] == resident.district_code), None)
    coverage = None
    if _gate(user, RULES_VIEW, "coverage", restricted):
        cases: dict[str, str] = {}
        if user.can(CAREGAPS_VIEW):
            for activity, status in session.execute(
                select(CareGapCase.care_activity, CareGapCase.status).where(CareGapCase.resident_id == resident_id).order_by(CareGapCase.opened_at, CareGapCase.id)
            ):
                cases[activity] = status
        rows = []
        for e in _evaluations(session, resident_id)["items"]:
            if not e["is_latest"] or e["guideline"] is None:
                continue
            status, label = COVERAGE_OF.get(e["outcome"], ("unknown", "Unknown"))
            rows.append({
                "care_activity": e["guideline"]["care_activity"], "label": programme_label(e["guideline"]["care_activity"]),
                "guideline": {"key": e["guideline"]["key"], "name": e["guideline"]["name"], "version": e["guideline"]["version"]},
                "outcome": e["outcome"], "status": status, "status_label": label, "evaluated_at": e["evaluated_at"],
                "case_status": cases.get(e["guideline"]["care_activity"]),
            })  # fmt: skip
        rows.sort(key=lambda r: r["care_activity"])
        coverage = {
            "definition": "One row per programme (guideline care activity): the outcome of the latest evaluation as a coverage status. Up to date means the care is already completed; due means it is eligible now.",
            "source": "synthetic", "programmes": rows,
            "counts": {k: sum(1 for r in rows if r["status"] == k) for k in sorted({r["status"] for r in rows})},
        }  # fmt: skip
    return {
        "synthetic_banner": SYNTHETIC_BANNER,
        "resident": _resident_out(resident, ds.dataset_version, _as_of(ds)),
        "place": None if place is None else {
            "district_code": place["code"], "city": place["district"], "state": place["state"], "iso_code": place["iso_3166_2"],
            "definition": "The pilot district the resident is placed in (an assigned place, not a real address).",
        },
        "coverage": coverage,
        "not_available": PROFILE_NOT_AVAILABLE,
        "restricted": restricted,
    }  # fmt: skip


# ------------------------------------------------------------------------------------------------ whole note text
MAX_SPANS = 200


def resident_document_text(session: Session, resident_id: uuid.UUID, document_id: uuid.UUID) -> dict[str, Any]:
    """The whole stored text of one of the resident's documents plus the character spans of the evidence facts that point
    into it (for highlighting). Read only; the text comes from the document store exactly as stored."""
    _resident_or_404(session, resident_id)
    doc = session.get(Document, document_id)
    if doc is None or doc.resident_id != resident_id:
        raise NotFound("Document not found for this resident.")
    try:
        text = evidence_service.get_store().read_text(doc.storage_key)
    except FileNotFoundError:
        raise NotFound("The text of this document is not stored.") from None
    rows = session.execute(
        select(EvidenceFact, EvidenceSource)
        .join(EvidenceSource, EvidenceSource.evidence_fact_id == EvidenceFact.id)
        .where(EvidenceSource.document_id == document_id, EvidenceSource.source_start_offset.is_not(None))
        .order_by(EvidenceSource.source_start_offset, EvidenceSource.id)
        .limit(MAX_SPANS)
    ).all()
    return {
        "synthetic_banner": SYNTHETIC_BANNER,
        "document": {"id": doc.id, "document_type": doc.document_type, "language": doc.language, "created_on": doc.created_on, "registered_at": doc.registered_at,
                     "processing_status": doc.processing_status, "content_hash": doc.content_hash},  # fmt: skip
        "text": text, "characters": len(text), "direction": "rtl" if doc.language == "ar" else "ltr",
        "definition": "The whole stored text of the document and the spans (character offsets into this text) of the evidence facts extracted from it, with their verification status.",
        "source": "synthetic",
        "spans": [
            {"fact_id": f.id, "fact_type": f.fact_type, "category": f.category, "subject_type": f.subject_type, "relationship_type": f.relationship_type,
             "assertion_status": f.assertion_status, "validation_status": f.validation_status, "verification_status": src.verification_status,
             "start_offset": src.source_start_offset, "end_offset": src.source_end_offset, "quote": src.source_quote}
            for f, src in rows
        ],  # fmt: skip
    }
