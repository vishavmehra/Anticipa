# ruff: noqa: E501
"""FHIR R4 mapping for the HAPI demo load (04 §3, 05 §18): pure functions, no I/O, no database, no network.

Two families of resources are produced:

* Synthea resources, rewritten just enough to be stored in a FHIR server on their own (`prepare_synthea`):
  conditional references (`Practitioner?identifier=...`) become `Reference.identifier`, references to resources that are
  not part of the loaded subset are dropped (the display text is kept), and every resource is tagged synthetic.
* Anticipa write-back and context resources built from database rows (`task`, `detected_issue`, `flag`, `provenance`,
  `communication_*`, `appointment_*`, `location`, `related_person`, `document_reference`, `enrichment_resource`).

Patient ids in HAPI are the Synthea patient ids (`residents.source_patient_id`), so every Synthea reference stays valid
unchanged; the Anticipa resident UUID is carried as `Patient.identifier` (`urn:anticipa:resident-id`). Every
engagement row is SIMULATED and every generated row SYNTHETIC; both are tagged (`urn:anticipa:synthetic-status`).
Resource ids of Anticipa resources are the source row UUIDs, so a re-run overwrites instead of duplicating.
"""

from __future__ import annotations

import base64
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping
from datetime import datetime
from typing import Any

SYNTHETIC_STATUS = "urn:anticipa:synthetic-status"
DATASET = "urn:anticipa:dataset"
RESIDENT_ID = "urn:anticipa:resident-id"
CARE_ACTIVITY = "urn:anticipa:care-activity"
CASE_STATUS = "urn:anticipa:case-status"
CASE_ID = "urn:anticipa:care-gap-case"
CHANNEL = "urn:anticipa:channel"
PRIORITY_NOTE = "Deterministic, versioned guideline rules decided this; it is not a diagnosis."

# ------------------------------------------------------------------------------------------------ helpers


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def tags(synthetic_status: str, dataset_version: str, *extra: tuple[str, str]) -> dict[str, Any]:
    return {
        "tag": [
            {"system": SYNTHETIC_STATUS, "code": synthetic_status},
            {"system": DATASET, "code": dataset_version},
            *({"system": s, "code": c} for s, c in extra),
        ]
    }


def ref(resource_type: str, rid: object) -> dict[str, str]:
    return {"reference": f"{resource_type}/{rid}"}


def _put(resource: dict[str, Any], **optional: Any) -> dict[str, Any]:
    """Add the optional elements that have a value and remove top-level None (FHIR JSON has no nulls)."""
    resource.update({k: v for k, v in optional.items() if v not in (None, "", [], {})})
    return {k: v for k, v in resource.items() if v is not None}


def humanize(activity: str) -> str:
    return activity.replace("_", " ").capitalize()


# ------------------------------------------------------------------------------------------------ Synthea


def _parse_conditional(reference: str) -> dict[str, Any] | None:
    """`Practitioner?identifier=sys|value` -> `{"type", "identifier"}`; None when it is not that shape."""
    resource_type, _, query = reference.partition("?")
    key, _, token = query.partition("=")
    system, bar, value = token.partition("|")
    if key != "identifier" or not bar or not value:
        return None
    return {"type": resource_type, "identifier": {"system": system, "value": value}}


def _clean(node: Any, loaded: Collection[tuple[str, str]], stats: dict[str, int]) -> Any:
    """Rewrite references below `node`. Returns `_DROP` for a reference that is left with nothing to say."""
    if isinstance(node, list):
        out = [_clean(x, loaded, stats) for x in node]
        return [x for x in out if x is not _DROP]
    if not isinstance(node, dict):
        return node
    reference = node.get("reference")
    if isinstance(reference, str):
        rest = {k: v for k, v in node.items() if k != "reference"}
        if "?" in reference:
            parsed = _parse_conditional(reference)
            if parsed is None:
                stats["conditional_dropped"] += 1
                return rest if rest else _DROP
            stats["conditional_to_identifier"] += 1
            return {**parsed, **rest}
        resource_type, _, rid = reference.partition("/")
        if (resource_type, rid) in loaded:
            return node
        stats["dangling_dropped"] += 1
        return rest if rest else _DROP
    cleaned = {}
    for key, value in node.items():
        out = _clean(value, loaded, stats)
        if out is not _DROP:
            cleaned[key] = out
    return cleaned if cleaned or not node else _DROP


_DROP = object()


def prepare_synthea(
    resource: dict[str, Any],
    loaded: Collection[tuple[str, str]],
    stats: dict[str, int],
    dataset_version: str,
) -> dict[str, Any]:
    """A Synthea resource ready for `PUT [type]/[id]`: references rewritten, synthetic tags added."""
    cleaned = _clean(resource, loaded, stats)
    meta = cleaned.setdefault("meta", {})
    # The US Core profiles are not installed on the server, so a `meta.profile` claim could only be reported as an
    # unresolvable profile by `$validate`. The claim is kept as a tag instead.
    claimed = [("urn:anticipa:synthea-profile", p) for p in meta.pop("profile", [])]
    meta["tag"] = [*meta.get("tag", []), *tags("synthetic", dataset_version, *claimed)["tag"]]
    return cleaned


def prepare_patient(
    resource: dict[str, Any],
    resident: Mapping[str, Any],
    loaded: Collection[tuple[str, str]],
    stats: dict[str, int],
    dataset_version: str,
) -> dict[str, Any]:
    """The Synthea Patient plus the Anticipa resident id, district and preferred language."""
    out = prepare_synthea(resource, loaded, stats, dataset_version)
    out["identifier"] = [
        *out.get("identifier", []),
        {"system": RESIDENT_ID, "value": str(resident["id"])},
    ]
    if resident.get("district_code"):
        out["extension"] = [
            *out.get("extension", []),
            {"url": "urn:anticipa:district", "valueCode": resident["district_code"]},
        ]
    if resident.get("preferred_language"):
        out["communication"] = [
            {
                "language": {
                    "coding": [{"system": "urn:ietf:bcp:47", "code": resident["preferred_language"]}]
                },
                "preferred": True,
            }
        ]
    return out


def dependencies(resource: Mapping[str, Any]) -> set[tuple[str, str]]:
    """(type, id) of every `Type/id` reference in the resource."""
    found: set[tuple[str, str]] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            r = node.get("reference")
            if isinstance(r, str) and "/" in r and "?" not in r:
                found.add((r.split("/", 1)[0], r.split("/", 1)[1]))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(resource)
    return found


def dependency_order(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable topological order: a resource comes after every resource it references that is in the list.
    A cycle (not expected) keeps its original relative order at the end."""
    keys = [(r["resourceType"], r["id"]) for r in resources]
    index = {k: i for i, k in enumerate(keys)}
    waiting: dict[int, set[int]] = {}
    children: dict[int, list[int]] = defaultdict(list)
    for i, r in enumerate(resources):
        needs = {index[d] for d in dependencies(r) if d in index and d != keys[i]}
        waiting[i] = needs
        for n in needs:
            children[n].append(i)
    ready = sorted(i for i, needs in waiting.items() if not needs)
    out: list[int] = []
    seen: set[int] = set()
    while ready:
        i = ready.pop(0)
        out.append(i)
        seen.add(i)
        for child in sorted(children[i]):
            waiting[child].discard(i)
            if not waiting[child] and child not in seen and child not in ready:
                ready.append(child)
        ready.sort()
    out.extend(i for i in range(len(resources)) if i not in seen)
    return [resources[i] for i in out]


# ------------------------------------------------------------------------------------------------ enrichment


def enrichment_resource(
    row: Mapping[str, Any], patient_id: str, dataset_version: str
) -> dict[str, Any] | None:
    """Anticipa enrichment rows (`clinical_records.source_system = 'anticipa_enrichment'`) exist only in the
    database; rebuild the Procedure / Observation they stand for. None for other resource types."""
    kind = row["resource_type"]
    coding = {
        k: v
        for k, v in (
            ("system", row.get("code_system")),
            ("code", row.get("code")),
            ("display", row.get("display")),
        )
        if v
    }
    base: dict[str, Any] = {
        "resourceType": kind,
        "id": row["source_resource_id"],
        "meta": {
            **tags("synthetic", dataset_version, ("urn:anticipa:origin", "anticipa_enrichment")),
            "source": "urn:anticipa:enrichment/enrich-1",
        },
        "code": {"coding": [coding], "text": row.get("display") or row.get("code")},
        "subject": ref("Patient", patient_id),
    }
    when = _iso(row.get("effective_start"))
    if kind == "Procedure":
        status = row.get("status") if row.get("status") in PROCEDURE_STATUSES else "completed"
        return _put(base, status=status, performedDateTime=when)
    if kind == "Observation":
        value = row.get("value_json") or {}
        out = _put(
            base,
            status=row.get("status") if row.get("status") in OBSERVATION_STATUSES else "final",
            category=[
                {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                            "code": "laboratory",
                        }
                    ]
                }
            ],
            effectiveDateTime=when,
        )
        concept = value.get("concept")
        if isinstance(concept, dict) and concept.get("code"):
            out["valueCodeableConcept"] = {
                "coding": [{k: concept[k] for k in ("system", "code", "display") if concept.get(k)}],
                "text": concept.get("display") or concept["code"],
            }
        elif isinstance(value.get("value"), int | float):
            q: dict[str, Any] = {"value": value["value"]}
            if value.get("unit"):
                q["unit"] = value["unit"]
            out["valueQuantity"] = q
        return out
    return None


PROCEDURE_STATUSES = {
    "preparation",
    "in-progress",
    "not-done",
    "on-hold",
    "stopped",
    "completed",
    "entered-in-error",
    "unknown",
}
OBSERVATION_STATUSES = {
    "registered",
    "preliminary",
    "final",
    "amended",
    "corrected",
    "cancelled",
    "entered-in-error",
    "unknown",
}

# ------------------------------------------------------------------------------------------------ care-gap cases

TASK_STATUS = {
    "identified": "draft",
    "pending_review": "requested",
    "approved_for_planning": "ready",
    "awaiting_capacity": "on-hold",
    "planned": "accepted",
    "outreach_pending": "in-progress",
    "outreach_completed": "in-progress",
    "appointment_scheduled": "in-progress",
    "awaiting_completion_evidence": "in-progress",
    "completed": "completed",
    "closed_not_completed": "failed",
    "escalated": "in-progress",
    "cancelled": "cancelled",
}
TASK_PRIORITY = {"routine": "routine", "elevated": "urgent", "high": "asap"}
ISSUE_SEVERITY = {"routine": "low", "elevated": "moderate", "high": "high"}
TERMINAL = {"completed", "closed_not_completed", "cancelled"}


def _case_tags(case: Mapping[str, Any], dataset_version: str) -> dict[str, Any]:
    return tags(case.get("synthetic_status") or "synthetic", dataset_version)


def detected_issue(case: Mapping[str, Any], patient_id: str, dataset_version: str) -> dict[str, Any]:
    status = case["status"]
    activity = case["care_activity"]
    return _put(
        {
            "resourceType": "DetectedIssue",
            "id": str(case["id"]),
            "meta": _case_tags(case, dataset_version),
            "identifier": [{"system": CASE_ID, "value": str(case["id"])}],
            "status": "cancelled"
            if status == "cancelled"
            else "preliminary"
            if status in ("identified", "pending_review")
            else "final",
            "code": {
                "coding": [{"system": CARE_ACTIVITY, "code": activity, "display": humanize(activity)}],
                "text": humanize(activity),
            },
            "severity": ISSUE_SEVERITY.get(case["priority"], "low"),
            "patient": ref("Patient", patient_id),
            "identifiedDateTime": _iso(case["opened_at"]),
            "detail": (
                f"Guideline-defined preventive care opportunity: {humanize(activity)}. Created from eligibility outcome "
                f"'{case['source_outcome']}' (guideline version {case['guideline_version_id']}). {PRIORITY_NOTE}"
            ),
        }
    )


def task(case: Mapping[str, Any], patient_id: str, dataset_version: str) -> dict[str, Any]:
    status = case["status"]
    activity = case["care_activity"]
    inputs = [
        {"type": {"text": "guideline_version_id"}, "valueString": str(case["guideline_version_id"])},
        {"type": {"text": "evaluation_id"}, "valueString": str(case["evaluation_id"])},
    ]
    if case.get("rule_version_id"):
        inputs.append({"type": {"text": "rule_version_id"}, "valueString": str(case["rule_version_id"])})
    out = {
        "resourceType": "Task",
        "id": str(case["id"]),
        "meta": _case_tags(case, dataset_version),
        "identifier": [{"system": CASE_ID, "value": str(case["id"])}],
        "status": TASK_STATUS[status],
        "businessStatus": {
            "coding": [{"system": CASE_STATUS, "code": status}],
            "text": status.replace("_", " "),
        },
        "intent": "plan",
        "priority": TASK_PRIORITY.get(case["priority"], "routine"),
        "code": {"coding": [{"system": CARE_ACTIVITY, "code": activity, "display": humanize(activity)}]},
        "description": f"Care-gap case: {humanize(activity)}",
        "focus": ref("DetectedIssue", case["id"]),
        "for": ref("Patient", patient_id),
        "authoredOn": _iso(case["opened_at"]),
        "lastModified": _iso(case["updated_at"]),
        "reasonCode": {
            "coding": [{"system": "urn:anticipa:eligibility-outcome", "code": case["source_outcome"]}]
        },
        "input": inputs,
    }
    if status in TERMINAL and case.get("closed_at"):
        out["executionPeriod"] = {"start": _iso(case["opened_at"]), "end": _iso(case["closed_at"])}
    if case.get("closure_reason"):
        out["statusReason"] = {"text": case["closure_reason"]}
    return _put(out)


def needs_flag(case: Mapping[str, Any]) -> bool:
    return case["priority"] == "high" or case["status"] == "escalated"


def flag(case: Mapping[str, Any], patient_id: str, dataset_version: str) -> dict[str, Any]:
    activity = case["care_activity"]
    escalated = case["status"] == "escalated"
    period: dict[str, str] = {"start": _iso(case["opened_at"])}
    if case["status"] in TERMINAL and case.get("closed_at"):
        period["end"] = _iso(case["closed_at"])
    return {
        "resourceType": "Flag",
        "id": str(case["id"]),
        "meta": _case_tags(case, dataset_version),
        "identifier": [{"system": CASE_ID, "value": str(case["id"])}],
        "status": "inactive" if case["status"] in TERMINAL else "active",
        "category": [
            {
                "coding": [
                    {"system": "http://terminology.hl7.org/CodeSystem/flag-category", "code": "clinical"}
                ]
            }
        ],
        "code": {
            "text": ("Escalated to a clinician: " if escalated else "High priority preventive care gap: ")
            + humanize(activity)
        },
        "subject": ref("Patient", patient_id),
        "period": period,
    }


def provenance(case: Mapping[str, Any], patient_id: str, dataset_version: str) -> dict[str, Any]:
    return {
        "resourceType": "Provenance",
        "id": str(case["id"]),
        "meta": _case_tags(case, dataset_version),
        "target": [ref("DetectedIssue", case["id"]), ref("Task", case["id"])],
        "recorded": _iso(case["opened_at"]),
        "activity": {
            "coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-DataOperation", "code": "CREATE"}]
        },
        "reason": [
            {"coding": [{"system": "urn:anticipa:eligibility-outcome", "code": case["source_outcome"]}]}
        ],
        "agent": [
            {
                "type": {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/provenance-participant-type",
                            "code": "assembler",
                        }
                    ]
                },
                "who": {
                    "identifier": {"system": "urn:anticipa:system", "value": "care-gap-engine"},
                    "display": "Anticipa deterministic care-gap engine (no language model decides eligibility)",
                },
            }
        ],
        "entity": [
            {
                "role": "source",
                "what": {
                    "identifier": {
                        "system": "urn:anticipa:eligibility-evaluation",
                        "value": str(case["evaluation_id"]),
                    },
                    "display": f"Eligibility evaluation, guideline version {case['guideline_version_id']}",
                },
            }
        ],
    }


# ------------------------------------------------------------------------------------------------ engagement

COMM_STATUS = {"queued": "preparation", "sent": "completed", "delivered": "completed", "failed": "not-done"}
APPOINTMENT_STATUS = {
    "proposed": "proposed",
    "simulated_confirmed": "booked",
    "completed": "fulfilled",
    "no_show": "noshow",
    "cancelled": "cancelled",
}
PARTICIPANT_STATUS = {"proposed": "needs-action", "cancelled": "declined"}
ALLOCATION_STATUS = {"proposed": "proposed", "confirmed": "pending", "superseded": "cancelled"}
SIMULATED_SENDER = {"display": "Anticipa outreach (simulated, nothing was sent to a real person)"}
ASSISTANT_SENDER = {"display": "Anticipa engagement assistant (simulated)"}


def communication_event(ev: Mapping[str, Any], patient_id: str, dataset_version: str) -> dict[str, Any]:
    state = ev["delivery_state"]
    return _put(
        {
            "resourceType": "Communication",
            "id": str(ev["id"]),
            "meta": tags(ev.get("synthetic_status") or "simulated", dataset_version),
            "identifier": [{"system": "urn:anticipa:communication-event", "value": str(ev["id"])}],
            "basedOn": [ref("Task", ev["care_gap_case_id"])],
            "status": COMM_STATUS.get(state, "unknown"),
            "category": [
                {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/communication-category",
                            "code": "notification",
                        }
                    ]
                }
            ],
            "medium": [{"coding": [{"system": CHANNEL, "code": ev["channel"]}], "text": ev["channel"]}],
            "subject": ref("Patient", patient_id),
            "recipient": [ref("Patient", patient_id)],
            "sender": SIMULATED_SENDER,
            "payload": [{"contentString": ev["body"]}],
            "language": ev.get("language"),
        },
        statusReason={"text": "simulated delivery failed"} if state == "failed" else None,
        sent=_iso(ev.get("sent_at")),
        received=_iso(ev.get("delivered_at")),
        extension=[
            {"url": "urn:anticipa:template", "valueString": f"{ev['template_key']}@{ev['template_version']}"},
            {"url": "urn:anticipa:delivery-state", "valueCode": state},
        ],
    )


def communication_response(resp: Mapping[str, Any], patient_id: str, dataset_version: str) -> dict[str, Any]:
    return _put(
        {
            "resourceType": "Communication",
            "id": str(resp["id"]),
            "meta": tags(resp.get("synthetic_status") or "simulated", dataset_version),
            "identifier": [{"system": "urn:anticipa:resident-response", "value": str(resp["id"])}],
            "basedOn": [ref("Task", resp["care_gap_case_id"])],
            "inResponseTo": [ref("Communication", resp["communication_event_id"])],
            "status": "completed",
            "subject": ref("Patient", patient_id),
            "sender": ref("Patient", patient_id),
            "payload": [{"contentString": resp.get("response_value") or f"[{resp['response_type']}]"}],
            "sent": _iso(resp.get("received_at")),
            "extension": [{"url": "urn:anticipa:response-type", "valueCode": resp["response_type"]}],
        }
    )


def communication_message(msg: Mapping[str, Any], patient_id: str, dataset_version: str) -> dict[str, Any]:
    from_resident = msg["role"] == "resident"
    extensions = [{"url": "urn:anticipa:message-source", "valueCode": msg["source"]}]
    for key in ("intent", "escalation_state", "escalation_reason"):
        if msg.get(key):
            extensions.append({"url": f"urn:anticipa:{key.replace('_', '-')}", "valueCode": msg[key]})
    return _put(
        {
            "resourceType": "Communication",
            "id": str(msg["id"]),
            "meta": tags("simulated", dataset_version),
            "identifier": [{"system": "urn:anticipa:conversation-message", "value": str(msg["id"])}],
            "basedOn": [ref("Task", msg["care_gap_case_id"])],
            "status": "completed",
            "subject": ref("Patient", patient_id),
            "sender": ref("Patient", patient_id) if from_resident else ASSISTANT_SENDER,
            "recipient": [ASSISTANT_SENDER] if from_resident else [ref("Patient", patient_id)],
            "payload": [{"contentString": msg["body"]}],
            "language": msg.get("language"),
            "sent": _iso(msg.get("created_at")),
            "extension": extensions,
        }
    )


def location(clinic: Mapping[str, Any], dataset_version: str) -> dict[str, Any]:
    address = {
        k: v for k, v in (("text", clinic.get("address")), ("district", clinic.get("district_code"))) if v
    }
    return _put(
        {
            "resourceType": "Location",
            "id": str(clinic["id"]),
            "meta": tags(clinic.get("synthetic_status") or "synthetic", dataset_version),
            "identifier": [{"system": "urn:anticipa:clinic", "value": clinic["clinic_key"]}],
            "status": "active" if clinic["operating_status"] == "open" else "inactive",
            "name": clinic["name"],
            "mode": "instance",
            "type": [{"text": "Preventive care clinic (synthetic)"}],
            "extension": [{"url": "urn:anticipa:timezone", "valueCode": clinic["timezone"]}],
        },
        address=address,
    )


def _appointment(
    rid: str,
    patient_id: str,
    row: Mapping[str, Any],
    clinic_id: object,
    service: Mapping[str, Any] | None,
    status: str,
    start: datetime,
    end: datetime,
    synthetic: str,
    dataset_version: str,
    description: str,
    origin: str,
) -> dict[str, Any]:
    patient_status = PARTICIPANT_STATUS.get(status, "accepted")
    return _put(
        {
            "resourceType": "Appointment",
            "id": rid,
            "meta": tags(synthetic, dataset_version, ("urn:anticipa:origin", origin)),
            "status": status,
            "start": _iso(start),
            "end": _iso(end),
            "description": description,
            "supportingInformation": [ref("Task", row["care_gap_case_id"])],
            "participant": [
                {"actor": ref("Patient", patient_id), "required": "required", "status": patient_status},
                {"actor": ref("Location", clinic_id), "required": "required", "status": "accepted"},
            ],
        },
        serviceType=[
            {
                "coding": [
                    {
                        "system": "urn:anticipa:service",
                        "code": service["service_key"],
                        "display": service["service_name"],
                    }
                ]
            }
        ]
        if service
        else None,
        comment=row.get("status_reason"),
    )


def appointment(
    ap: Mapping[str, Any], patient_id: str, service: Mapping[str, Any] | None, dataset_version: str
) -> dict[str, Any]:
    return _appointment(
        str(ap["id"]), patient_id, ap, ap["clinic_id"], service, APPOINTMENT_STATUS[ap["appointment_status"]],
        ap["start_time"], ap["end_time"], ap.get("synthetic_status") or "simulated", dataset_version,
        "Simulated appointment (no real booking exists)", "appointment",
    )  # fmt: skip


def appointment_for_allocation(
    al: Mapping[str, Any], patient_id: str, service: Mapping[str, Any] | None, dataset_version: str
) -> dict[str, Any]:
    note = f"Planned allocation ({al['allocation_status']}) from the capacity planner; simulated, not offered to the resident"
    return _appointment(
        f"alloc-{al['id']}", patient_id, al, al["clinic_id"], service, ALLOCATION_STATUS[al["allocation_status"]],
        al["proposed_start_time"], al["proposed_end_time"], al.get("synthetic_status") or "simulated", dataset_version,
        note, "planned_allocation",
    )  # fmt: skip


# ------------------------------------------------------------------------------------------------ family, documents

ROLE_CODES = {
    "mother": "MTH", "father": "FTH", "parent": "PRN", "sister": "SIS", "brother": "BRO", "sibling": "SIB",
    "daughter": "DAU", "son": "SON", "child": "CHILD", "spouse": "SPS", "husband": "HUSB", "wife": "WIFE",
    "grandmother": "GRMTH", "grandfather": "GRFTH", "grandparent": "GRPRN", "grandchild": "GRNDCHILD",
    "granddaughter": "GRNDDAU", "grandson": "GRNDSON", "aunt": "AUNT", "uncle": "UNCLE", "cousin": "COUSN",
    "niece": "NIECE", "nephew": "NEPHEW",
}  # fmt: skip
ROLE_SYSTEM = "http://terminology.hl7.org/CodeSystem/v3-RoleCode"


def related_person(
    edge: Mapping[str, Any], patient_id: str, related_patient_id: str | None, dataset_version: str
) -> dict[str, Any]:
    kind = edge["relationship_type"]
    code = ROLE_CODES.get(kind)
    coding = (
        {"system": ROLE_SYSTEM, "code": code, "display": kind}
        if code
        else {"system": "urn:anticipa:relationship-type", "code": kind}
    )
    related = related_patient_id or edge.get("related_person_source_id")
    return _put(
        {
            "resourceType": "RelatedPerson",
            "id": str(edge["id"]),
            "meta": tags(edge.get("synthetic_status") or "synthetic", dataset_version),
            "active": True,
            "patient": ref("Patient", patient_id),
            "relationship": [{"coding": [coding], "text": kind}],
            "extension": [
                {"url": "urn:anticipa:confidence-status", "valueCode": edge["confidence_status"]},
                {"url": "urn:anticipa:relationship-source", "valueCode": edge["source_type"]},
            ],
        },
        identifier=[
            {
                "system": "urn:anticipa:related-patient"
                if related_patient_id
                else "urn:anticipa:related-person",
                "value": related,
            }
        ]
        if related
        else None,
    )


def document_reference(
    doc: Mapping[str, Any], patient_id: str, text: str | None, dataset_version: str
) -> dict[str, Any]:
    attachment: dict[str, Any] = {
        "contentType": "text/plain",
        "title": doc["document_type"].replace("_", " "),
    }
    if doc.get("language"):
        attachment["language"] = doc["language"]
    if text is not None:
        raw = text.encode("utf-8")
        attachment["data"] = base64.b64encode(raw).decode("ascii")
        attachment["size"] = len(raw)
    else:  # text missing from the document store: keep a resolvable pointer, never an empty attachment
        attachment["url"] = f"urn:anticipa:document:{doc['id']}"
    extra = (
        [("urn:anticipa:document-generation", doc["generation_method"])]
        if doc.get("generation_method")
        else []
    )
    return _put(
        {
            "resourceType": "DocumentReference",
            "id": str(doc["id"]),
            "meta": tags(doc.get("synthetic_status") or "synthetic", dataset_version, *extra),
            "identifier": [{"system": "urn:anticipa:document-content-hash", "value": doc["content_hash"]}],
            "status": "current",
            "type": {
                "coding": [{"system": "urn:anticipa:document-type", "code": doc["document_type"]}],
                "text": doc["document_type"].replace("_", " "),
            },
            "subject": ref("Patient", patient_id),
            "date": _iso(doc.get("created_on") or doc.get("registered_at")),
            "description": f"Synthetic {doc['document_type'].replace('_', ' ')} (generated; not a real clinical document)",
            "content": [{"attachment": attachment}],
        }
    )


def consent(fhir: dict[str, Any], patient_id: str, dataset_version: str) -> dict[str, Any]:
    """The existing `consent.service.to_fhir` output, re-pointed at the HAPI Patient id and tagged with the dataset."""
    fhir["patient"] = ref("Patient", patient_id)
    fhir.setdefault(
        "policy", [{"uri": "urn:anticipa:consent-policy/v1"}]
    )  # ppc-1: a patient-privacy Consent needs a policy
    meta = fhir.setdefault("meta", {})
    meta["tag"] = [*meta.get("tag", []), {"system": DATASET, "code": dataset_version}]
    return fhir


def chunked(items: Iterable[Any], size: int) -> Iterable[list[Any]]:
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
