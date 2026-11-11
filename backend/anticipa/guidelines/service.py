"""Guideline management workflow (08 §4-5): draft -> in_review -> approved -> retired, immutable once approved.

All functions add to the caller's transaction (flush, no commit) and write an audit event for every state change.
The same rules are also enforced by DB triggers (migration 0002), so a bug here cannot rewrite an approved version.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from anticipa.core import audit
from anticipa.core.audit import Actor
from anticipa.core.errors import Conflict, InvalidTransition, NotFound, ValidationFailed
from anticipa.guidelines.models import Guideline, GuidelineVersion, RuleVersion
from anticipa.guidelines.rules import (
    INPUT_SCHEMA_VERSION,
    KEY_RE,
    RULE_LANGUAGE,
    RuleValidationError,
    canonical_hash,
    parse_rule_definition,
)

RULE_DEFINITION_KEYS = ("care_activity", "priority", "missing_data_policy", "inclusion", "exclusions")


def _now() -> datetime:
    return datetime.now(UTC)


def _details_from(err: RuleValidationError) -> list[dict[str, Any]]:
    return [{"field": e["field"], "reason": e["reason"]} for e in err.errors]


# ---------------------------------------------------------------- reads
def get_guideline(session: Session, guideline_id: uuid.UUID) -> Guideline:
    g = session.get(Guideline, guideline_id)
    if g is None:
        raise NotFound("Guideline not found.")
    return g


def get_version(session: Session, version_id: uuid.UUID, guideline_id: uuid.UUID | None = None, *, lock: bool = False) -> GuidelineVersion:
    v = session.get(GuidelineVersion, version_id, with_for_update=lock)
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
            select(RuleVersion).where(RuleVersion.guideline_version_id == version_id).order_by(RuleVersion.rule_key)
        ).scalars()
    )


def _content(source_reference: str | None, published: date | None, effective_from: date, effective_until: date | None,
             scope: dict[str, Any], disclaimer: str | None, rule_hashes: dict[str, str]) -> dict[str, Any]:  # fmt: skip
    return {
        "source_reference": source_reference,
        "source_publication_date": published.isoformat() if published else None,
        "effective_from": effective_from.isoformat(),
        "effective_until": effective_until.isoformat() if effective_until else None,
        "population_scope": scope,
        "disclaimer": disclaimer,
        "rules": dict(sorted(rule_hashes.items())),
    }


def content_signature(version: GuidelineVersion, rules: list[RuleVersion]) -> str:
    """Hash of what a version says (source, window, scope, rules) independent of its number: import idempotency key."""
    return canonical_hash(
        _content(version.source_reference, version.source_publication_date, version.effective_from,
                 version.effective_until, version.population_scope, version.disclaimer,
                 {r.rule_key: r.content_hash for r in rules})  # fmt: skip
    )


def version_hash(version: GuidelineVersion, rules: list[RuleVersion]) -> str:
    """The stored `content_hash`: the signature plus the guideline identity and version number; frozen at approval."""
    return canonical_hash({"guideline_id": str(version.guideline_id), "version": version.version, "signature": content_signature(version, rules)})


def _refresh_hash(session: Session, version: GuidelineVersion) -> None:
    session.flush()
    version.content_hash = version_hash(version, rules_of(session, version.id))
    session.flush()


# ---------------------------------------------------------------- guideline + version drafts
def create_guideline(
    session: Session, actor: Actor, *, guideline_key: str, name: str, description: str | None = None,
    topic: str | None = None, authoritative_source: str | None = None, source_url: str | None = None,
) -> Guideline:  # fmt: skip
    if not KEY_RE.match(guideline_key):
        raise ValidationFailed("guideline_key must be lowercase letters, digits, '_' or '-' (2-80 characters).")
    if session.execute(select(Guideline.id).where(Guideline.guideline_key == guideline_key)).first():
        raise Conflict(f"A guideline with key '{guideline_key}' already exists.")
    g = Guideline(
        guideline_key=guideline_key, name=name, description=description, topic=topic,
        authoritative_source=authoritative_source, source_url=source_url, created_by=actor.id,
    )  # fmt: skip
    session.add(g)
    session.flush()
    audit.record(session, actor, "guideline.create", "guideline", g.id, "success", {"guideline_key": guideline_key})
    return g


def create_version(
    session: Session, actor: Actor, guideline: Guideline, *, effective_from: date, effective_until: date | None = None,
    source_reference: str | None = None, source_publication_date: date | None = None,
    population_scope: dict[str, Any] | None = None, disclaimer: str | None = None, change_summary: str | None = None,
    rules: list[dict[str, Any]] | None = None,
) -> GuidelineVersion:  # fmt: skip
    if effective_until is not None and effective_until < effective_from:
        raise ValidationFailed("effective_until must not be before effective_from.")
    session.execute(select(Guideline.id).where(Guideline.id == guideline.id).with_for_update())  # serialise numbering
    number = (session.execute(select(func.max(GuidelineVersion.version)).where(GuidelineVersion.guideline_id == guideline.id)).scalar() or 0) + 1
    v = GuidelineVersion(
        guideline_id=guideline.id, version=number, review_status="draft", source_reference=source_reference,
        source_publication_date=source_publication_date, effective_from=effective_from, effective_until=effective_until,
        population_scope=population_scope or {}, disclaimer=disclaimer, change_summary=change_summary, created_by=actor.id,
    )  # fmt: skip
    session.add(v)
    session.flush()
    for r in rules or []:
        add_rule(session, actor, v, r["rule_key"], r["name"], r.get("description"), r["definition"], _refresh=False)
    _refresh_hash(session, v)
    audit.record(
        session, actor, "guideline.version_create", "guideline_version", v.id, "success",
        {"guideline_key": guideline.guideline_key, "version": number, "rules": len(rules or [])},
    )  # fmt: skip
    return v


def add_rule(
    session: Session, actor: Actor, version: GuidelineVersion, rule_key: str, name: str, description: str | None,
    definition: Any, *, _refresh: bool = True,
) -> RuleVersion:  # fmt: skip
    if version.review_status != "draft":
        raise InvalidTransition("Rules can only be added to a draft guideline version.")
    if not KEY_RE.match(rule_key):
        raise ValidationFailed("rule_key must be lowercase letters, digits, '_' or '-' (2-80 characters).")
    try:
        parsed = parse_rule_definition(definition)
    except RuleValidationError as exc:
        raise ValidationFailed("The rule definition is invalid.", _details_from(exc)) from exc
    if session.execute(
        select(RuleVersion.id).where(RuleVersion.guideline_version_id == version.id, RuleVersion.rule_key == rule_key)
    ).first():
        raise Conflict(f"Rule '{rule_key}' already exists in this guideline version.")
    previous = session.execute(
        select(func.count())
        .select_from(RuleVersion)
        .join(GuidelineVersion, GuidelineVersion.id == RuleVersion.guideline_version_id)
        .where(GuidelineVersion.guideline_id == version.guideline_id, RuleVersion.rule_key == rule_key)
    ).scalar_one()
    rule = RuleVersion(
        guideline_version_id=version.id, rule_key=rule_key, version=previous + 1, name=name, description=description,
        care_activity=parsed.care_activity, priority=parsed.priority, rule_language=RULE_LANGUAGE,
        rule_definition=parsed.normalized(), content_hash=parsed.content_hash(),
        input_schema_version=INPUT_SCHEMA_VERSION, approval_status="draft",
    )  # fmt: skip
    session.add(rule)
    session.flush()
    if _refresh:
        _refresh_hash(session, version)
        audit.record(
            session, actor, "guideline.rule_add", "rule_version", rule.id, "success",
            {"guideline_version_id": str(version.id), "rule_key": rule_key, "content_hash": rule.content_hash},
        )  # fmt: skip
    return rule


def delete_rule(session: Session, actor: Actor, rule: RuleVersion) -> None:
    version = get_version(session, rule.guideline_version_id, lock=True)
    if version.review_status != "draft":
        raise InvalidTransition("Rules can only be removed from a draft guideline version.")
    audit.record(session, actor, "guideline.rule_delete", "rule_version", rule.id, "success", {"rule_key": rule.rule_key})
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
    if not (version.source_reference or "").strip():
        problems.append({"field": "source_reference", "reason": "source metadata is required before review"})
    if not version.population_scope:
        problems.append({"field": "population_scope", "reason": "a population scope is required before review"})
    for r in rules:
        try:
            parse_rule_definition(r.rule_definition)
        except RuleValidationError as exc:
            problems.extend({"field": f"rules.{r.rule_key}.{e['field']}", "reason": e["reason"]} for e in exc.errors)
    if problems:
        raise ValidationFailed("The guideline version is not ready for review.", problems)
    version.review_status = "in_review"
    version.submitted_by, version.submitted_at = actor.id, _now()
    for r in rules:
        r.approval_status = "in_review"
    _refresh_hash(session, version)
    audit.record(session, actor, "guideline.submit_review", "guideline_version", version.id, "success", {"version": version.version, "content_hash": version.content_hash})  # fmt: skip
    return version


def return_to_draft(session: Session, actor: Actor, version: GuidelineVersion, reason: str | None) -> GuidelineVersion:
    version = get_version(session, version.id, lock=True)
    _transition(version, ("in_review",), "draft")
    version.review_status = "draft"
    for r in rules_of(session, version.id):
        r.approval_status = "draft"
    session.flush()
    audit.record(session, actor, "guideline.return_to_draft", "guideline_version", version.id, "success", {"version": version.version, "reason": reason})  # fmt: skip
    return version


def approve(session: Session, actor: Actor, version: GuidelineVersion) -> GuidelineVersion:
    version = get_version(session, version.id, lock=True)
    _transition(version, ("in_review",), "approved")
    rules = rules_of(session, version.id)
    fresh = version_hash(version, rules)
    if version.content_hash and version.content_hash != fresh:
        raise Conflict("The guideline version content changed after it was submitted for review; return it to draft and resubmit.")
    for r in rules:  # re-validate: an approved rule must always parse under the current engine
        try:
            parse_rule_definition(r.rule_definition)
        except RuleValidationError as exc:
            raise ValidationFailed(f"Rule '{r.rule_key}' no longer validates.", _details_from(exc)) from exc
    version.review_status = "approved"
    version.approved_by, version.approved_at, version.content_hash = actor.id, _now(), fresh
    for r in rules:
        r.approval_status = "approved"
    session.flush()
    audit.record(
        session, actor, "guideline.approve", "guideline_version", version.id, "success",
        {"version": version.version, "content_hash": fresh, "rules": [r.rule_key for r in rules]},
    )  # fmt: skip
    return version


def retire(session: Session, actor: Actor, version: GuidelineVersion, reason: str | None) -> GuidelineVersion:
    version = get_version(session, version.id, lock=True)
    _transition(version, ("approved",), "retired")
    version.review_status = "retired"
    version.retired_by, version.retired_at, version.retirement_reason = actor.id, _now(), reason
    for r in rules_of(session, version.id):
        r.approval_status = "retired"
    session.flush()
    audit.record(session, actor, "guideline.retire", "guideline_version", version.id, "success", {"version": version.version, "reason": reason})  # fmt: skip
    return version


# ---------------------------------------------------------------- import of drafts (CLI + tests); never approves
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
            "guidelines_created": self.guidelines_created, "versions_created": self.versions_created,
            "rules_created": self.rules_created, "skipped_unchanged": self.skipped_unchanged,
            "errors": self.errors, "warnings": self.warnings,
        }  # fmt: skip


def _date(value: Any, what: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValidationFailed(f"{what}: '{value}' is not an ISO date.") from exc


def _rule_entries(raw_rules: Any) -> list[dict[str, Any]]:
    out = []
    for r in raw_rules or []:
        if not isinstance(r, dict):
            raise ValidationFailed("each rule must be an object")
        definition = r.get("rule_definition") or r.get("definition")
        if definition is None:
            definition = {k: r[k] for k in RULE_DEFINITION_KEYS if k in r}
        out.append(
            {"rule_key": r.get("rule_key") or r.get("rule_id"), "name": r.get("name") or r.get("rule_key") or r.get("rule_id"),
             "description": r.get("description"), "definition": definition}  # fmt: skip
        )
    return out


def import_drafts(session: Session, payload: Any, actor: Actor | None = None, *, dry_run: bool = False) -> ImportReport:
    """Import guideline drafts: `{"guidelines": [...]}` or a list. Each guideline: guideline_key, name, description,
    topic, authoritative_source, source_url and `versions` (or `rules` for one implied version); each version:
    source_reference, source_publication_date, effective_from, effective_until, population_scope, disclaimer,
    change_summary, rules[]; each rule: rule_key, name, description + the rule-definition keys. Idempotent: a version
    whose content hash already exists for the guideline is skipped. Everything is created as `draft`."""
    actor = actor or Actor.system()
    report = ImportReport()
    items = payload.get("guidelines") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        report.errors.append({"guideline": None, "reason": "expected a list of guidelines or {'guidelines': [...]}"})
        return report
    for item in items:
        key = item.get("guideline_key") if isinstance(item, dict) else None
        local = ImportReport()
        try:
            with session.begin_nested():  # one guideline is imported completely or not at all
                _import_one(session, actor, item, local, dry_run)
            for k in ("guidelines_created", "versions_created", "rules_created", "skipped_unchanged"):
                setattr(report, k, getattr(report, k) + getattr(local, k))
            report.warnings.extend(local.warnings)
        except ValidationFailed as exc:
            report.errors.append({"guideline": key, "reason": exc.message, "details": exc.details})
        except Conflict as exc:
            report.errors.append({"guideline": key, "reason": exc.message})
    return report


def _import_one(session: Session, actor: Actor, item: Any, report: ImportReport, dry_run: bool) -> None:
    if not isinstance(item, dict) or not item.get("guideline_key") or not item.get("name"):
        raise ValidationFailed("a guideline needs guideline_key and name")
    versions = item.get("versions")
    if versions is None:
        versions = [{k: item.get(k) for k in ("source_reference", "source_publication_date", "effective_from", "effective_until",
                                               "population_scope", "disclaimer", "change_summary", "rules")}]  # fmt: skip
    prepared = []
    for vd in versions:
        status = vd.get("review_status") or vd.get("status")
        if status not in (None, "draft"):
            report.warnings.append(f"{item['guideline_key']}: status '{status}' ignored; drafts only are imported")
        eff_from = _date(vd.get("effective_from"), "effective_from")
        if eff_from is None:
            raise ValidationFailed(f"{item['guideline_key']}: every version needs effective_from")
        rules = _rule_entries(vd.get("rules"))
        problems: list[dict[str, Any]] = []
        for r in rules:
            if not r["rule_key"] or not KEY_RE.match(str(r["rule_key"])):
                problems.append({"field": f"rules.{r['rule_key']}", "reason": "invalid rule_key"})
                continue
            try:
                parse_rule_definition(r["definition"])
            except RuleValidationError as exc:
                problems.extend({"field": f"rules.{r['rule_key']}.{e['field']}", "reason": e["reason"]} for e in exc.errors)
        if problems:
            raise ValidationFailed(f"{item['guideline_key']}: rule validation failed (nothing imported for this guideline)", problems)
        prepared.append((vd, eff_from, rules))

    guideline = session.execute(select(Guideline).where(Guideline.guideline_key == item["guideline_key"])).scalar_one_or_none()
    existing: set[str] = set()
    if guideline is not None:
        for ev in session.execute(select(GuidelineVersion).where(GuidelineVersion.guideline_id == guideline.id)).scalars():
            existing.add(content_signature(ev, rules_of(session, ev.id)))
    fresh = []
    for vd, eff_from, rules in prepared:
        sig = canonical_hash(
            _content(vd.get("source_reference"), _date(vd.get("source_publication_date"), "source_publication_date"), eff_from,
                     _date(vd.get("effective_until"), "effective_until"), vd.get("population_scope") or {}, vd.get("disclaimer"),
                     {r["rule_key"]: parse_rule_definition(r["definition"]).content_hash() for r in rules})  # fmt: skip
        )
        if sig in existing:
            report.skipped_unchanged += 1
        else:
            existing.add(sig)
            fresh.append((vd, eff_from, rules))
    if dry_run:
        report.guidelines_created += guideline is None and bool(fresh)
        report.versions_created += len(fresh)
        report.rules_created += sum(len(r) for _, _, r in fresh)
        return
    if not fresh:
        return
    if guideline is None:
        guideline = create_guideline(
            session, actor, guideline_key=item["guideline_key"], name=item["name"], description=item.get("description"),
            topic=item.get("topic"), authoritative_source=item.get("authoritative_source"), source_url=item.get("source_url"),
        )  # fmt: skip
        report.guidelines_created += 1
    for vd, eff_from, rules in fresh:
        create_version(
            session, actor, guideline, effective_from=eff_from, effective_until=_date(vd.get("effective_until"), "effective_until"),
            source_reference=vd.get("source_reference"), source_publication_date=_date(vd.get("source_publication_date"), "source_publication_date"),
            population_scope=vd.get("population_scope") or {}, disclaimer=vd.get("disclaimer"), change_summary=vd.get("change_summary"),
            rules=rules,
        )  # fmt: skip
        report.versions_created += 1
        report.rules_created += len(rules)
