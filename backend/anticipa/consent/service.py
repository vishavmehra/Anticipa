"""Consent workflows over the database (ADR-070): what is in force for a resident, recording a decision, the
guards other modules call, the FHIR Consent shape and the synthetic opt-out seed. Routes and CLIs stay thin.

Rules:
  * `evaluate` is the single definition of "what is in force" (pure, takes rows). Per (scope, channel) the
    newest record that is currently effective wins; an effective `deny` at the blanket level (channel NULL)
    or on a channel blocks that channel (deny wins). No record means permit. Outreach is blocked for a
    resident only when every channel is denied.
  * Guards fail closed: a missing table or an unreadable decision raises, it never silently allows contact.
  * Functions add to the caller's transaction (flush, no commit), except `check_outreach`, which commits its
    audit event before refusing (same pattern as planning's refused confirmation) and so must be called
    before any other write.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from anticipa.consent.models import AI_PROCESSING, OUTREACH, SCOPES, SOURCES, STATUSES, ConsentRecord
from anticipa.core import audit
from anticipa.core.audit import Actor
from anticipa.core.errors import Conflict, NotFound, ValidationFailed
from anticipa.core.ids import uuid5_of
from anticipa.core.logging import request_id_var
from anticipa.datafabric.models import Dataset, Resident
from anticipa.engagement.models import CHANNELS, LANGUAGES

log = logging.getLogger(__name__)

API_SOURCES = tuple(
    s for s in SOURCES if s != "synthetic_seed"
)  # the seed is an operator tool, never an API input
NOTE_MAX = 300


def _now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------ what is in force
@dataclass(frozen=True)
class ConsentState:
    denied_channels: frozenset[str] = frozenset()
    ai_processing_denied: bool = False
    in_force: tuple[uuid.UUID, ...] = ()  # the records that decide (newest effective per scope and channel)

    @property
    def permitted_channels(self) -> tuple[str, ...]:
        return tuple(c for c in CHANNELS if c not in self.denied_channels)

    @property
    def outreach_blocked(self) -> bool:
        return not self.permitted_channels


def evaluate(records: Iterable[Any], now: datetime | None = None) -> ConsentState:
    """Pure: the state implied by one resident's records at `now`."""
    now = now or _now()
    latest: dict[tuple[str, str | None], Any] = {}
    for r in sorted(records, key=lambda r: (r.effective_from, r.created_at or r.effective_from, str(r.id))):
        if r.effective_from <= now and (r.effective_to is None or now < r.effective_to):
            latest[r.scope, r.channel] = r  # ascending order: a newer record overwrites an older one
    blanket = latest.get((OUTREACH, None))
    denied = frozenset(
        c
        for c in CHANNELS
        if any(x is not None and x.status == "deny" for x in (blanket, latest.get((OUTREACH, c))))
    )
    ai = latest.get((AI_PROCESSING, None))
    return ConsentState(denied, ai is not None and ai.status == "deny", tuple(r.id for r in latest.values()))


def state_for(session: Session, resident_id: uuid.UUID, now: datetime | None = None) -> ConsentState:
    rows = (
        session.execute(select(ConsentRecord).where(ConsentRecord.resident_id == resident_id)).scalars().all()
    )
    return evaluate(rows, now)


def states_with_denials(session: Session, now: datetime | None = None) -> dict[uuid.UUID, ConsentState]:
    """State of every resident with at least one `deny` record (the only ones that differ from the default).
    ponytail: loads all records of those residents; fine to ~1e5 rows, move the evaluation into SQL beyond."""
    has_deny = select(ConsentRecord.resident_id).where(ConsentRecord.status == "deny")
    by_resident: dict[uuid.UUID, list[ConsentRecord]] = defaultdict(list)
    for r in session.execute(select(ConsentRecord).where(ConsentRecord.resident_id.in_(has_deny))).scalars():
        by_resident[r.resident_id].append(r)
    return {rid: evaluate(rows, now) for rid, rows in by_resident.items()}


def unreachable_residents(session: Session, now: datetime | None = None) -> set[uuid.UUID]:
    """Residents who denied every outreach channel (planning leaves their cases `consent_denied`)."""
    return {rid for rid, st in states_with_denials(session, now).items() if st.outreach_blocked}


# ------------------------------------------------------------ guards
def check_outreach(
    session: Session,
    resident_id: uuid.UUID,
    channel: str,
    *,
    actor: Actor,
    case_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> None:
    """409 when the resident denied `channel`. The refusal is audited (outcome `denied`) and committed
    first, so call it before any other write of the request."""
    if channel not in state_for(session, resident_id, now).denied_channels:
        return
    audit_outreach_blocked(session, actor, resident_id, channel, case_id=case_id)
    session.commit()
    raise Conflict(
        f"Outreach by {channel} is blocked: the resident has not consented to it (consent_denied).",
        [{"field": "channel", "reason": "consent_denied"}],
    )


def audit_outreach_blocked(
    session: Session,
    actor: Actor,
    resident_id: uuid.UUID,
    channel: str | None,
    *,
    case_id: uuid.UUID | None = None,
    **extra: Any,
) -> None:
    audit.record(
        session,
        actor,
        "consent.outreach.blocked",
        "resident",
        resident_id,
        "denied",
        {
            "channel": channel,
            "care_gap_case_id": str(case_id) if case_id else None,
            "reason": "consent_denied",
            "simulated": True,
            **extra,
        },
    )


def ai_processing_denied(
    sessions: Callable[[], Session],
    resident_id: uuid.UUID,
    *,
    job_id: uuid.UUID | None,
    task: str,
    cache: dict[uuid.UUID, bool],
) -> bool:
    """True when the resident denied AI processing of their documents. `cache` is per job (one lookup per
    resident); the first skip of a resident in a job is audited and logged (no document text, no identifiers
    beyond the uuid). Earlier runs are untouched: this guards only documents a job is about to send to a
    model."""
    if resident_id not in cache:
        with sessions() as s:
            cache[resident_id] = state_for(s, resident_id).ai_processing_denied
            if cache[resident_id]:
                audit.record(
                    s,
                    Actor.system(),
                    "consent.ai_processing.skipped",
                    "resident",
                    resident_id,
                    "denied",
                    {"task": task, "job_id": str(job_id) if job_id else None, "reason": "consent_denied"},
                )
                s.commit()
                log.info(
                    "skipped AI processing: resident %s denied data_use_ai_processing (task=%s job=%s)",
                    resident_id,
                    task,
                    job_id,
                )
    return cache[resident_id]


# ------------------------------------------------------------ recording a decision
def record_consent(
    session: Session,
    resident_id: uuid.UUID,
    *,
    scope: str,
    status: str,
    actor: Actor,
    channel: str | None = None,
    language: str | None = None,
    source: str = "coordinator_entry",
    effective_to: datetime | None = None,
    note: str | None = None,
    now: datetime | None = None,
) -> ConsentRecord:
    """Append one decision (effective now). Withdrawing = recording the opposite status; nothing is edited."""
    if scope not in SCOPES or status not in STATUSES:
        raise ValidationFailed("Invalid consent scope or status.")
    if source not in API_SOURCES:
        raise ValidationFailed(
            "Invalid consent source.", [{"field": "source", "reason": f"one of {list(API_SOURCES)}"}]
        )
    if channel is not None and (scope != OUTREACH or channel not in CHANNELS):
        raise ValidationFailed(
            "A channel applies to outreach consent only.",
            [{"field": "channel", "reason": f"outreach, one of {list(CHANNELS)}"}],
        )
    if language is not None and language not in LANGUAGES:
        raise ValidationFailed(
            "Invalid language.", [{"field": "language", "reason": f"one of {list(LANGUAGES)}"}]
        )
    now = now or _now()
    if effective_to is not None and effective_to <= now:
        raise ValidationFailed(
            "effective_to must be in the future.", [{"field": "effective_to", "reason": "in the past"}]
        )
    if session.get(Resident, resident_id) is None:
        raise NotFound("Resident not found.")
    provenance: dict[str, Any] = {"actor_role": actor.role, "request_id": request_id_var.get()}
    if note and note.strip():
        provenance["note"] = note.strip()[:NOTE_MAX]
    record = ConsentRecord(
        id=uuid.uuid4(),
        resident_id=resident_id,
        scope=scope,
        status=status,
        channel=channel,
        language=language,
        source=source,
        effective_from=now,
        effective_to=effective_to,
        recorded_by=actor.id,
        provenance=provenance,
        synthetic_status="simulated",
        created_at=now,
    )
    session.add(record)
    session.flush()
    audit.record(
        session,
        actor,
        "consent.record",
        "consent_record",
        record.id,
        "success",
        {
            "resident_id": str(resident_id),
            "scope": scope,
            "status": status,
            "channel": channel,
            "source": source,
            "effective_to": effective_to.isoformat() if effective_to else None,
            "simulated": True,
        },
    )
    return record


def list_records(
    session: Session, resident_id: uuid.UUID, now: datetime | None = None
) -> tuple[list[ConsentRecord], ConsentState]:
    """Newest first, with the state they imply."""
    rows = (
        session.execute(
            select(ConsentRecord)
            .where(ConsentRecord.resident_id == resident_id)
            .order_by(
                ConsentRecord.effective_from.desc(), ConsentRecord.created_at.desc(), ConsentRecord.id.desc()
            )
        )
        .scalars()
        .all()
    )
    return list(rows), evaluate(rows, now)


# ------------------------------------------------------------ FHIR Consent
PURPOSE_SYSTEM = "urn:anticipa:consent-scope"
CHANNEL_EXTENSION = "urn:anticipa:consent-channel"
SCOPE_SYSTEM = "http://terminology.hl7.org/CodeSystem/consentscope"
CATEGORY_SYSTEM = "http://loinc.org"


def to_fhir(r: ConsentRecord, *, in_force: bool) -> dict[str, Any]:
    """FHIR R4 Consent for one record (mapping in consent/models.py). Not validated against a FHIR server."""
    period = {
        "start": r.effective_from.isoformat(),
        **({"end": r.effective_to.isoformat()} if r.effective_to else {}),
    }
    provision: dict[str, Any] = {
        "type": r.status,
        "period": period,
        "purpose": [{"system": PURPOSE_SYSTEM, "code": r.scope}],
    }
    if r.channel:
        provision["extension"] = [{"url": CHANNEL_EXTENSION, "valueCode": r.channel}]
    out: dict[str, Any] = {
        "resourceType": "Consent",
        "id": str(r.id),
        "status": "active" if in_force else "inactive",
        "meta": {"tag": [{"system": "urn:anticipa:synthetic-status", "code": r.synthetic_status}]},
        "scope": {"coding": [{"system": SCOPE_SYSTEM, "code": "patient-privacy"}]},
        "category": [
            {"coding": [{"system": CATEGORY_SYSTEM, "code": "59284-0", "display": "Consent Document"}]}
        ],
        "patient": {"reference": f"Patient/{r.resident_id}"},
        "dateTime": r.created_at.isoformat(),
        "provision": provision,
    }
    if r.language:
        out["language"] = r.language
    return out


# ------------------------------------------------------------ synthetic opt-out seed
SEED_EFFECTIVE_FROM = datetime(2026, 11, 1, tzinfo=UTC)
SEED_BANDS = (  # share of residents, cumulative: blanket opt-out, one channel, no AI processing
    ("outreach_all", 0.005),
    ("outreach_channel", 0.015),
    ("ai_processing", 0.005),
)
SEED_CHANNELS = ("sms", "whatsapp", "phone_call")
CHUNK = 2000


def _draw(seed: int, resident_id: uuid.UUID) -> tuple[float, int]:
    """Two stable numbers per resident (no PYTHONHASHSEED dependence): a position in [0, 1) and a pick."""
    h = hashlib.sha256(f"anticipa.consent:{seed}:{resident_id}".encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**64, h[8]


def seed_rows(resident_id: uuid.UUID, language: str | None, seed: int) -> list[dict[str, Any]]:
    """The synthetic decisions for one resident (empty for most): a pure function of (seed, resident id)."""
    u, pick = _draw(seed, resident_id)
    edge, kind = 0.0, None
    for name, share in SEED_BANDS:
        edge += share
        if u < edge:
            kind = name
            break
    if kind is None:
        return []
    scope = AI_PROCESSING if kind == "ai_processing" else OUTREACH
    channel = SEED_CHANNELS[pick % len(SEED_CHANNELS)] if kind == "outreach_channel" else None
    return [
        {
            "id": uuid5_of(f"consent-seed:{seed}:{resident_id}:{scope}:{channel}"),
            "resident_id": resident_id,
            "scope": scope,
            "status": "deny",
            "channel": channel,
            "language": language if language in LANGUAGES else None,
            "source": "synthetic_seed",
            "effective_from": SEED_EFFECTIVE_FROM,
            "effective_to": None,
            "recorded_by": None,
            "provenance": {"seed": seed, "kind": kind, "note": "synthetic opt-out (demo)"},
            "synthetic_status": "synthetic",
        }
    ]


def seed_synthetic_optouts(
    session: Session, *, seed: int = 20261113, dataset_version: str | None = None, dry_run: bool = False
) -> dict[str, int]:
    """Insert the deterministic opt-out set (idempotent: derived ids, conflicts skipped). Caller commits."""
    stmt = select(Resident.id, Resident.preferred_language).order_by(Resident.id)
    if dataset_version:
        stmt = stmt.join(Dataset, Dataset.id == Resident.dataset_id).where(
            Dataset.dataset_version == dataset_version
        )
    counts: dict[str, int] = defaultdict(int)
    counts["residents"] = 0
    batch: list[dict[str, Any]] = []
    inserted = 0

    def flush() -> None:
        nonlocal inserted
        if batch and not dry_run:
            table = ConsentRecord.__table__
            ins = pg_insert(table).values(batch).on_conflict_do_nothing(index_elements=["id"])
            inserted += len(session.execute(ins.returning(table.c.id)).all())  # rowcount is -1 here
        batch.clear()

    for rid, language in session.execute(stmt).all():  # ids only: ~30 MB at 200k residents
        counts["residents"] += 1
        for row in seed_rows(rid, language, seed):
            counts[row["provenance"]["kind"]] += 1
            batch.append(row)
        if len(batch) >= CHUNK:
            flush()
    flush()
    counts["inserted"] = inserted
    if not dry_run:
        audit.record(
            session,
            Actor.system(),
            "consent.seed",
            "consent_records",
            None,
            "success",
            {**counts, "seed": seed, "dataset_version": dataset_version, "synthetic": True},
        )
    return dict(counts)


def remove_seeded(session: Session) -> int:
    """Delete the synthetic seed rows only (never a decision a person recorded). Caller commits."""
    n = session.execute(delete(ConsentRecord).where(ConsentRecord.source == "synthetic_seed")).rowcount or 0
    audit.record(
        session, Actor.system(), "consent.seed.removed", "consent_records", None, "success", {"deleted": n}
    )
    return n
