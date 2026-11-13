"""Guideline management workflow (08 §4-5): draft -> in_review -> approved -> retired, immutable once
approved.

All functions add to the caller's transaction (flush, no commit) and write an audit event for every state
change.
The same rules are also enforced by DB triggers (migration 0002), so a bug here cannot rewrite an approved
version.
Approval does not by itself mean clinical review: `clinical_review_status` stays `pending_clinical_review`
(and is copied
onto every evaluation) unless the approver explicitly attests otherwise.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from anticipa.core import audit
from anticipa.core.audit import Actor
from anticipa.core.errors import Conflict, InvalidTransition, NotFound, ValidationFailed
from anticipa.guidelines.evaluator import REVIEW_DONE, REVIEW_PENDING
from anticipa.guidelines.models import Guideline, GuidelineVersion, RuleVersion
from anticipa.guidelines.rules import (
    DEFAULT_DECISION_POLICY,
    INPUT_SCHEMA_VERSION,
    KEY_RE,
    RULE_LANGUAGE,
    GuidelineDefinition,
    RuleValidationError,
    canonical_hash,
    parse_guideline_definition,
    parse_rule,
)

_ACTIVITY_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def _now() -> datetime:
    return datetime.now(UTC)


def _details(err: RuleValidationError) -> list[dict[str, Any]]:
    return [{"field": e["field"], "reason": e["reason"]} for e in err.errors]


# ---------------------------------------------------------------- reads
def get_guideline(session: Session, guideline_id: uuid.UUID) -> Guideline:
    g = session.get(Guideline, guideline_id)
    if g is None:
        raise NotFound("Guideline not found.")
    return g


def get_version(
    session: Session, version_id: uuid.UUID, guideline_id: uuid.UUID | None = None, *, lock: bool = False
) -> GuidelineVersion:
    v = session.get(GuidelineVersion, version_id, with_for_update=lock, populate_existing=lock)
    if v is None or (guideline_id is not None and v.guideline_id != guideline_id):
        raise NotFound("Guideline version not found.")
    return v


def get_rule(session: Session, rule_id: uuid.UUID) -> RuleVersion:
    r = session.get(RuleVersion, rule_id)
    if r is None:
        raise NotFound("Rule version not found.")
    return r


def rules_of(session: Session, version_id: uuid.UUID) -> list[RuleVersion]:
    return list(
        session.execute(
            select(RuleVersion)
            .where(RuleVersion.guideline_version_id == version_id)
            .order_by(RuleVersion.precedence, RuleVersion.rule_key),
            execution_options={"populate_existing": True},  # never judge content from stale in-session copies
        ).scalars()
    )


def definition_of(version: GuidelineVersion, rules: list[RuleVersion]) -> GuidelineDefinition:
    """The validated, executable definition of a stored version (raises RuleValidationError)."""
    return parse_guideline_definition(
        [r.rule_definition for r in rules], version.selectors, version.decision_policy or None
    )


def _content(version: GuidelineVersion, rule_hashes: dict[str, str]) -> dict[str, Any]:
    return {
        "source_reference": version.source_reference,
        "source_publication_date": version.source_publication_date.isoformat()
        if version.source_publication_date
        else None,
        "effective_from": version.effective_from.isoformat(),
        "effective_until": version.effective_until.isoformat() if version.effective_until else None,
        "population_scope": version.population_scope,
        "disclaimer": version.disclaimer,
        "care_activity": version.care_activity,
        "priority": version.priority,
        "source_version": version.source_version,
        "selectors": version.selectors,
        "decision_policy": version.decision_policy,
        "sources": version.sources,
        "metadata": version.version_metadata,
        "rules": dict(sorted(rule_hashes.items())),
    }


def content_signature(version: GuidelineVersion, rules: list[RuleVersion]) -> str:
    """Hash of what a version says, independent of its number: the import-idempotency key."""
    return canonical_hash(_content(version, {r.rule_id: r.content_hash for r in rules}))


def version_hash(version: GuidelineVersion, rules: list[RuleVersion]) -> str:
    """The stored `content_hash`: the signature plus the guideline identity and version number; frozen at
    approval."""  # noqa: E501
    return canonical_hash(
        {
            "guideline_id": str(version.guideline_id),
            "version": version.version,
            "signature": content_signature(version, rules),
        }
    )


def _refresh_hash(session: Session, version: GuidelineVersion) -> None:
    session.flush()
    version.content_hash = version_hash(version, rules_of(session, version.id))
    session.flush()


# ---------------------------------------------------------------- guideline + version drafts
def create_guideline(
    session: Session,
    actor: Actor,
    *,
    guideline_key: str,
    name: str,
    description: str | None = None,
    topic: str | None = None,
    authoritative_source: str | None = None,
    source_url: str | None = None,
) -> Guideline:
    if not KEY_RE.match(guideline_key):
        raise ValidationFailed(
            "guideline_key must be lowercase letters, digits, '_' or '-' (2-80 characters)."
        )
    if session.execute(select(Guideline.id).where(Guideline.guideline_key == guideline_key)).first():
        raise Conflict(f"A guideline with key '{guideline_key}' already exists.")
    g = Guideline(
        guideline_key=guideline_key,
        name=name,
        description=description,
        topic=topic,
        authoritative_source=authoritative_source,
        source_url=source_url,
        created_by=actor.id,
    )
    session.add(g)
    session.flush()
    audit.record(
        session, actor, "guideline.create", "guideline", g.id, "success", {"guideline_key": guideline_key}
    )
    return g


def create_version(
    session: Session,
    actor: Actor,
    guideline: Guideline,
    *,
    effective_from: date,
    effective_until: date | None = None,
    source_reference: str | None = None,
    source_publication_date: date | None = None,
    population_scope: dict[str, Any] | None = None,
    disclaimer: str | None = None,
    change_summary: str | None = None,
    care_activity: str | None = None,
    priority: str = "routine",
    source_version: str | None = None,
    selectors: dict[str, Any] | None = None,
    decision_policy: dict[str, Any] | None = None,
    sources: list[Any] | None = None,
    metadata: dict[str, Any] | None = None,
    rules: list[dict[str, Any]] | None = None,
) -> GuidelineVersion:
    """Create the next draft version. `rules` are 08-format rule objects; they are validated together with the
    selector
    library and decision policy, and nothing is created when anything is invalid."""
    if effective_until is not None and effective_until < effective_from:
        raise ValidationFailed("effective_until must not be before effective_from.")
    activity = care_activity or guideline.guideline_key.replace("-", "_")
    if not _ACTIVITY_RE.match(activity):
        raise ValidationFailed("care_activity must be snake_case (2-64 characters).")
    if priority not in ("routine", "elevated", "high"):
        raise ValidationFailed("priority must be routine, elevated or high.")
    policy = decision_policy or DEFAULT_DECISION_POLICY
    if rules:
        try:
            parsed = parse_guideline_definition(rules, selectors or {}, policy)
        except RuleValidationError as exc:
            raise ValidationFailed("The guideline definition is invalid.", _details(exc)) from exc
    session.execute(
        select(Guideline.id).where(Guideline.id == guideline.id).with_for_update()
    )  # serialise numbering
    number = (
        session.execute(
            select(func.max(GuidelineVersion.version)).where(GuidelineVersion.guideline_id == guideline.id)
        ).scalar()
        or 0
    ) + 1
    v = GuidelineVersion(
        guideline_id=guideline.id,
        version=number,
        review_status="draft",
        source_reference=source_reference,
        source_publication_date=source_publication_date,
        effective_from=effective_from,
        effective_until=effective_until,
        population_scope=population_scope or {},
        disclaimer=disclaimer,
        change_summary=change_summary,
        care_activity=activity,
        priority=priority,
        source_version=source_version,
        clinical_review_status=REVIEW_PENDING,
        selectors=selectors or {},
        decision_policy=policy,
        sources=sources or [],
        version_metadata=metadata or {},
        created_by=actor.id,
    )
    session.add(v)
    session.flush()
    for rule in parsed.rules if rules else []:
        _insert_rule(session, v, guideline.id, rule)
    _refresh_hash(session, v)
    audit.record(
        session,
        actor,
        "guideline.version_create",
        "guideline_version",
        v.id,
        "success",
        {"guideline_key": guideline.guideline_key, "version": number, "rules": len(rules or [])},
    )
    return v


def _insert_rule(
    session: Session, version: GuidelineVersion, guideline_id: uuid.UUID, rule: Any
) -> RuleVersion:
    previous = session.execute(
        select(func.count())
        .select_from(RuleVersion)
        .join(GuidelineVersion, GuidelineVersion.id == RuleVersion.guideline_version_id)
        .where(GuidelineVersion.guideline_id == guideline_id, RuleVersion.rule_id == rule.rule_id)
    ).scalar_one()
    rv = RuleVersion(
        guideline_version_id=version.id,
        rule_key=rule.key,
        rule_id=rule.rule_id,
        rule_type=rule.rule_type,
        precedence=rule.priority,
        version=previous + 1,
        name=rule.name,
        description=(rule.model_extra or {}).get("description"),
        care_activity=version.care_activity,
        priority=version.priority,
        rule_language=RULE_LANGUAGE,
        rule_definition=rule.normalized(),
        content_hash=rule.content_hash(),
        input_schema_version=INPUT_SCHEMA_VERSION,
        approval_status="draft",
    )
    session.add(rv)
    session.flush()
    return rv


def add_rule(session: Session, actor: Actor, version: GuidelineVersion, definition: Any) -> RuleVersion:
    """Add one 08-format rule object to a draft version (validated against the version's selector library)."""
    version = get_version(session, version.id, lock=True)
    if version.review_status != "draft":
        raise InvalidTransition("Rules can only be added to a draft guideline version.")
    existing = rules_of(session, version.id)
    try:
        rule = parse_rule(definition)
        if any(r.rule_id == rule.rule_id or r.rule_key == rule.key for r in existing):
            raise Conflict(f"Rule '{rule.rule_id}' already exists in this guideline version.")
        parse_guideline_definition(
            [*(r.rule_definition for r in existing), rule.normalized()],
            version.selectors,
            version.decision_policy or None,
        )
    except RuleValidationError as exc:
        raise ValidationFailed("The rule definition is invalid.", _details(exc)) from exc
    rv = _insert_rule(session, version, version.guideline_id, rule)
    _refresh_hash(session, version)
    audit.record(
        session,
        actor,
        "guideline.rule_add",
        "rule_version",
        rv.id,
        "success",
        {"guideline_version_id": str(version.id), "rule_id": rule.rule_id, "content_hash": rv.content_hash},
    )
    return rv


def delete_rule(session: Session, actor: Actor, rule: RuleVersion) -> None:
    version = get_version(session, rule.guideline_version_id, lock=True)
    if version.review_status != "draft":
        raise InvalidTransition("Rules can only be removed from a draft guideline version.")
    audit.record(
        session, actor, "guideline.rule_delete", "rule_version", rule.id, "success", {"rule_id": rule.rule_id}
    )
    session.delete(rule)
    _refresh_hash(session, version)


# ---------------------------------------------------------------- workflow
def _transition(version: GuidelineVersion, allowed_from: tuple[str, ...], to: str) -> None:
    if version.review_status not in allowed_from:
        raise InvalidTransition(f"A {version.review_status} guideline version cannot become {to}.")


def submit_for_review(session: Session, actor: Actor, version: GuidelineVersion) -> GuidelineVersion:
    version = get_version(session, version.id, lock=True)
    _transition(version, ("draft",), "in_review")
    rules = rules_of(session, version.id)
    problems: list[dict[str, Any]] = []
    if not rules:
        problems.append({"field": "rules", "reason": "at least one rule is required"})
    if not (version.source_reference or "").strip() and not version.sources:
        problems.append({"field": "source_reference", "reason": "source metadata is required before review"})
    if not version.population_scope:
        problems.append(
            {"field": "population_scope", "reason": "a population scope is required before review"}
        )
    if rules:
        try:
            definition_of(version, rules)
        except RuleValidationError as exc:
            problems.extend(_details(exc))
    if problems:
        raise ValidationFailed("The guideline version is not ready for review.", problems)
    version.review_status = "in_review"
    version.submitted_by, version.submitted_at = actor.id, _now()
    for r in rules:
        r.approval_status = "in_review"
    _refresh_hash(session, version)
    audit.record(
        session,
        actor,
        "guideline.submit_review",
        "guideline_version",
        version.id,
        "success",
        {"version": version.version, "content_hash": version.content_hash},
    )
    return version


def return_to_draft(
    session: Session, actor: Actor, version: GuidelineVersion, reason: str | None
) -> GuidelineVersion:
    version = get_version(session, version.id, lock=True)
    _transition(version, ("in_review",), "draft")
    version.review_status = "draft"
    for r in rules_of(session, version.id):
        r.approval_status = "draft"
    session.flush()
    audit.record(
        session,
        actor,
        "guideline.return_to_draft",
        "guideline_version",
        version.id,
        "success",
        {"version": version.version, "reason": reason},
    )
    return version


def approve(
    session: Session, actor: Actor, version: GuidelineVersion, *, clinically_reviewed: bool = False
) -> GuidelineVersion:
    """Approve a version in review. `clinically_reviewed=True` is an explicit attestation by the approver;
    without it the
    version is approved for use but evaluations keep carrying `pending_clinical_review`."""
    version = get_version(session, version.id, lock=True)
    _transition(version, ("in_review",), "approved")
    rules = rules_of(session, version.id)
    fresh = version_hash(version, rules)
    if version.content_hash and version.content_hash != fresh:
        raise Conflict(
            "The guideline version content changed after it was submitted for review; return it to draft and resubmit."  # noqa: E501
        )
    try:
        definition_of(version, rules)  # an approved version must always parse under the current engine
    except RuleValidationError as exc:
        raise ValidationFailed("The guideline version no longer validates.", _details(exc)) from exc
    version.review_status = "approved"
    version.approved_by, version.approved_at, version.content_hash = actor.id, _now(), fresh
    version.clinical_review_status = REVIEW_DONE if clinically_reviewed else REVIEW_PENDING
    for r in rules:
        r.approval_status = "approved"
    session.flush()
    audit.record(
        session,
        actor,
        "guideline.approve",
        "guideline_version",
        version.id,
        "success",
        {
            "version": version.version,
            "content_hash": fresh,
            "rules": len(rules),
            "clinical_review_status": version.clinical_review_status,
        },
    )
    return version


def retire(session: Session, actor: Actor, version: GuidelineVersion, reason: str | None) -> GuidelineVersion:
    version = get_version(session, version.id, lock=True)
    _transition(version, ("approved",), "retired")
    version.review_status = "retired"
    version.retired_by, version.retired_at, version.retirement_reason = actor.id, _now(), reason
    for r in rules_of(session, version.id):
        r.approval_status = "retired"
    session.flush()
    audit.record(
        session,
        actor,
        "guideline.retire",
        "guideline_version",
        version.id,
        "success",
        {"version": version.version, "reason": reason},
    )
    return version


# ---------------------------------------------------------------- import of drafts (CLI + tests); never
# approves
@dataclass
class ImportReport:
    guidelines_created: int = 0
    versions_created: int = 0
    rules_created: int = 0
    skipped_unchanged: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "guidelines_created": self.guidelines_created,
            "versions_created": self.versions_created,
            "rules_created": self.rules_created,
            "skipped_unchanged": self.skipped_unchanged,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def _date(value: Any, what: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValidationFailed(f"{what}: '{value}' is not an ISO date.") from exc


# keys the importer consumes itself; every OTHER key of a guideline (applicability, risk-assessment tool,
# eligible_explanation,
# jurisdiction, notes...) is stored verbatim as version metadata and never executed
_CONSUMED_KEYS = {
    "guideline_key",
    "name",
    "description",
    "rules",
    "test_cases",
    "sources",
    "decision_policy",
    "selectors",
    "effective_from",
    "effective_until",
    "status",
    "version",
    "source_reference",
    "label",
    "source_organization",
    "population_scope",
    "disclaimer",
    "care_activity",
    "case_priority",
    "change_summary",
    "source_publication_date",
    "topic",
    "authoritative_source",
    "source_url",
}


def _selectors_for(item: dict[str, Any], library: dict[str, Any]) -> dict[str, Any]:
    """The guideline's own `selectors`, else the shared library restricted to what its rules reference."""
    if isinstance(item.get("selectors"), dict):
        return item["selectors"]
    names: set[str] = set()
    for raw in item.get("rules", []) or []:
        try:
            names |= parse_rule(raw).selectors_used()
        except RuleValidationError:
            pass  # reported by the definition validation that follows
    return {n: library[n] for n in sorted(names) if n in library}


def _prepare(item: Any, library: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    if not isinstance(item, dict) or not item.get("guideline_key") or not item.get("name"):
        raise ValidationFailed("a guideline needs guideline_key and name")
    key = item["guideline_key"]
    status = item.get("status") or (item.get("version") or {}).get("approval_status")
    if status not in (None, "draft"):
        warnings.append(f"{key}: status '{status}' ignored; drafts only are imported")
    eff_from = _date(item.get("effective_from"), "effective_from")
    if eff_from is None:
        raise ValidationFailed(f"{key}: effective_from is required")
    version_info = item.get("version") if isinstance(item.get("version"), dict) else {}
    sources = item.get("sources") or []
    scope = item.get("population_scope")
    kwargs: dict[str, Any] = {
        "effective_from": eff_from,
        "effective_until": _date(item.get("effective_until"), "effective_until"),
        "source_reference": item.get("source_reference")
        or (sources[0].get("title") if sources and isinstance(sources[0], dict) else None),
        "source_publication_date": _date(
            version_info.get("publication_date") or item.get("source_publication_date"), "publication_date"
        ),
        "population_scope": scope if isinstance(scope, dict) else ({"description": scope} if scope else {}),
        "disclaimer": item.get("label") or item.get("disclaimer"),
        "change_summary": version_info.get("change_summary") or item.get("change_summary"),
        "care_activity": item.get("care_activity") or key.replace("-", "_"),
        "priority": item.get("case_priority", "routine"),
        "source_version": version_info.get("source_version") or version_info.get("version_number"),
        "selectors": _selectors_for(item, library),
        "decision_policy": item.get("decision_policy"),
        "sources": sources,
        "metadata": {k: v for k, v in item.items() if k not in _CONSUMED_KEYS}
        | ({"description": item["description"]} if item.get("description") else {}),
        "rules": item.get("rules") or [],
    }
    try:
        parsed = parse_guideline_definition(kwargs["rules"], kwargs["selectors"], kwargs["decision_policy"])
    except RuleValidationError as exc:
        raise ValidationFailed(
            f"{key}: rule validation failed (nothing imported for this guideline)", _details(exc)
        ) from exc
    kwargs["_rule_hashes"] = {r.rule_id: r.content_hash() for r in parsed.rules}
    return kwargs


def import_drafts(
    session: Session, payload: Any, actor: Actor | None = None, *, dry_run: bool = False
) -> ImportReport:
    """Import guideline drafts from the research file format: `{"selector_library": {...}, "guidelines":
    [...]}` (or a
    bare list of guidelines carrying their own `selectors`). Each guideline: guideline_key, name,
    effective_from, sources,
    decision_policy, rules[] (08 §6.1) and optional test_cases (ignored here; they live in the regression
    suite).
    Idempotent: a version whose content already exists is skipped. Everything is created as `draft`,
    always."""
    actor = actor or Actor.system()
    report = ImportReport()
    library = payload.get("selector_library", {}) if isinstance(payload, dict) else {}
    items = payload.get("guidelines") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        report.errors.append(
            {"guideline": None, "reason": "expected a list of guidelines or {'guidelines': [...]}"}
        )
        return report
    for item in items:
        key = item.get("guideline_key") if isinstance(item, dict) else None
        local = ImportReport()
        try:
            with session.begin_nested():  # one guideline is imported completely or not at all
                _import_one(session, actor, item, library, local, dry_run)
            for k in ("guidelines_created", "versions_created", "rules_created", "skipped_unchanged"):
                setattr(report, k, getattr(report, k) + getattr(local, k))
            report.warnings.extend(local.warnings)
        except ValidationFailed as exc:
            report.errors.append({"guideline": key, "reason": exc.message, "details": exc.details})
        except Conflict as exc:
            report.errors.append({"guideline": key, "reason": exc.message})
    return report


def _import_one(
    session: Session, actor: Actor, item: Any, library: dict[str, Any], report: ImportReport, dry_run: bool
) -> None:
    kwargs = _prepare(item, library, report.warnings)
    hashes = kwargs.pop("_rule_hashes")
    guideline = session.execute(
        select(Guideline).where(Guideline.guideline_key == item["guideline_key"])
    ).scalar_one_or_none()
    if guideline is not None:
        for ev in session.execute(
            select(GuidelineVersion).where(GuidelineVersion.guideline_id == guideline.id)
        ).scalars():
            stub = GuidelineVersion(
                **{
                    c: getattr(ev, c)
                    for c in (
                        "source_reference",
                        "source_publication_date",
                        "effective_from",
                        "effective_until",
                        "population_scope",
                        "disclaimer",
                        "care_activity",
                        "priority",
                        "source_version",
                        "selectors",
                        "decision_policy",
                        "sources",
                        "version_metadata",
                    )
                }
            )
            incoming = GuidelineVersion(
                source_reference=kwargs["source_reference"],
                source_publication_date=kwargs["source_publication_date"],
                effective_from=kwargs["effective_from"],
                effective_until=kwargs["effective_until"],
                population_scope=kwargs["population_scope"],
                disclaimer=kwargs["disclaimer"],
                care_activity=kwargs["care_activity"],
                priority=kwargs["priority"],
                source_version=kwargs["source_version"],
                selectors=kwargs["selectors"],
                decision_policy=kwargs["decision_policy"] or DEFAULT_DECISION_POLICY,
                sources=kwargs["sources"],
                version_metadata=kwargs["metadata"],
            )
            existing_hashes = {r.rule_id: r.content_hash for r in rules_of(session, ev.id)}
            if canonical_hash(_content(stub, existing_hashes)) == canonical_hash(_content(incoming, hashes)):
                report.skipped_unchanged += 1
                return
    if dry_run:
        report.guidelines_created += guideline is None
        report.versions_created += 1
        report.rules_created += len(kwargs["rules"])
        return
    if guideline is None:
        guideline = create_guideline(
            session,
            actor,
            guideline_key=item["guideline_key"],
            name=item["name"],
            description=item.get("description"),
            topic=item.get("clinical_domain") or item.get("topic"),
            authoritative_source=item.get("source_organization") or item.get("authoritative_source"),
            source_url=(item.get("sources") or [{}])[0].get("url")
            if item.get("sources")
            else item.get("source_url"),
        )
        report.guidelines_created += 1
    create_version(session, actor, guideline, **kwargs)
    report.versions_created += 1
    report.rules_created += len(kwargs["rules"])
