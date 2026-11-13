# ruff: noqa: E501
"""Resident conversation (ADR-071): a SIMULATED two way thread per care gap case. The coordinator types what the resident
says; the assistant answers logistics (when and where, preparation, a proposed new slot, fees as stated) and hands every
health matter to a clinician.

Safety by construction, in this order (each layer is independent of the next):
  1. `safety.classify` reads the message first. Emergency, medical, eligibility dispute, opt out and prompt injection get a
     fixed approved reply and never reach the model. Emergencies and medical questions raise an escalation.
  2. Everything else goes through the Model Router (task `engagement.converse`, Haiku, prompt cached system prompt) with the
     case facts. The model returns a JSON step (intent, needs_clinician, reply, proposed slot ref), validated by the router.
     A hand off intent or `needs_clinician` from the model discards its reply and uses the approved text plus an escalation.
  3. `safety.reply_problems` checks the model's reply (no clinical language, no claim that anything was booked, every number
     from the facts, right language, no contact details, approved length). One corrected retry, then the approved
     "could not prepare a reply" text. Clinical language in a reply is itself treated as a medical question and escalated.
The model never decides eligibility, never books, and never gives clinical advice; a proposal is a recorded suggestion that
a care coordinator must confirm through the existing appointment flow.

Why a JSON step instead of provider tool use: the same reason as Ask Anticipa (ADR-069), the router is provider neutral and
has no native tool surface; structured output is validated on every candidate.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from anticipa.ai.models import ModelInvocation
from anticipa.ai.prompts import get_prompt
from anticipa.ai.router import get_router
from anticipa.ai.settings import get_ai_settings
from anticipa.ai.types import ChatMessage, GenerateRequest, RouterError
from anticipa.caregaps import service as caregaps
from anticipa.caregaps.models import TERMINAL_STATUSES, CareGapCase
from anticipa.consent import service as consent
from anticipa.consent.models import OUTREACH
from anticipa.conversation import safety
from anticipa.conversation.models import ConversationMessage
from anticipa.core import audit
from anticipa.core.auth import CurrentUser
from anticipa.core.errors import Conflict, RateLimited
from anticipa.datafabric.models import Resident
from anticipa.engagement.models import OPEN_APPOINTMENT_STATUSES, Appointment, CommunicationEvent
from anticipa.engagement.service import check_case_scope
from anticipa.engagement.templates import TEMPLATES, activity_label
from anticipa.planning.models import CapacitySlot, Clinic, ClinicService, PlannedAllocation

TASK = "engagement.converse"
PROMPT_REF = "engagement.converse@2"
MAX_OUTPUT_TOKENS = 450
MAX_ATTEMPTS = 2  # the first reply and one corrected retry
THREAD_CONTEXT = 6
MAX_RESIDENT_MESSAGES_PER_CASE = 50
CASE_REASON = {
    "emergency": "Resident message flagged as an emergency (simulated conversation); clinician review required",
    "medical_question": "Resident asked a medical question (simulated conversation); clinician review required",
    "eligibility_dispute": "Resident questioned eligibility or the record (simulated conversation); care team review required",
    "clinician_flag": "Resident message flagged for clinician review (simulated conversation)",
    "unsafe_reply": "Automated reply withheld by a safety check (simulated conversation); clinician review required",
}
INTENTS = ("scheduling", "rescheduling", "location_time", "preparation", "cost", "opt_out", "medical", "emergency", "eligibility_dispute", "out_of_scope", "unclear")
_CORRECTIONS = {
    "booking_claim": "never say anything was or will be booked, confirmed, cancelled, moved or changed; say that a care coordinator will confirm",
    "ungrounded_number": "use only dates, times and numbers copied exactly from FACTS (or written by the resident); no other digits",
    "language": "write in the REPLY LANGUAGE and its script",
    "length": "use one to three short sentences, under 400 characters",
    "contact_info": "include no link, email address or phone number",
    "constraint": "do not mention that the message is simulated and avoid the words urgent and result",
    "empty": "give a short reply, or ask one short question about the appointment",
}


# --------------------------------------------------------------------------------------------- what the model returns
class ConverseOut(BaseModel):
    """One model turn. Provider neutral; validated by the Model Router's structured output."""

    intent: Literal[*INTENTS]  # type: ignore[valid-type]
    needs_clinician: bool = False
    language: Literal["en", "hi", "ar"] = "en"
    reply: str = ""
    proposed_slot_ref: str | None = None


def system_prompt() -> str:
    return get_prompt(PROMPT_REF).text


# --------------------------------------------------------------------------------------------- case facts
def _now() -> datetime:
    return datetime.now(UTC)


def _zone(name: str | None) -> tuple[ZoneInfo | Any, str]:
    try:
        return ZoneInfo(name or "UTC"), name or "UTC"
    except Exception:  # noqa: BLE001  (unknown zone or no tz database on this machine: UTC is the honest fallback)
        return UTC, "UTC"


def _slot_view(start: datetime, end: datetime, clinic: Clinic | None, service: ClinicService | None) -> dict[str, Any]:
    tz, tz_name = _zone(clinic.timezone if clinic else None)
    s, e = start.astimezone(tz), end.astimezone(tz)
    return {
        "date": f"{s:%A} {s.day} {s:%B %Y}", "start": f"{s:%H:%M}", "end": f"{e:%H:%M}", "timezone": tz_name,
        "clinic": clinic.name if clinic else None, "address": clinic.address if clinic else None,
        "service": service.service_name if service else None,
    }  # fmt: skip


@dataclass
class Facts:
    """Everything the assistant may talk about. `data_for(lang)` is the JSON the model sees (the screening label and the
    approved preparation text come in the reply language); the checks use the same data."""

    care_activity: str
    appointment: dict[str, Any] | None = None
    alternatives: list[dict[str, Any]] = field(default_factory=list)  # each has a "ref" (s1, s2, s3) the model may propose
    slot_refs: dict[str, dict[str, Any]] = field(default_factory=dict)  # ref -> slot facts plus the slot id when it came from the database
    preferred_language: str | None = None
    channel: str | None = None

    def __post_init__(self) -> None:
        for a in self.alternatives:
            self.slot_refs.setdefault(a["ref"], {k: v for k, v in a.items() if k != "ref"})

    def data_for(self, lang: str) -> dict[str, Any]:
        return {
            "screening": activity_label(self.care_activity, lang),
            "appointment": self.appointment,
            "alternative_slots": self.alternatives,
            "approved_preparation_text": TEMPLATES["preparation_info"][lang],
            "fees": "No fee information is available. Do not state or guess any fee.",
        }

    def allowed_numbers(self, lang: str) -> set[int]:
        return safety.numbers_in(json.dumps(self.data_for(lang), ensure_ascii=False))

    @property
    def labels(self) -> list[str]:
        return safety.labels_for(self.care_activity)


def build_facts(session: Session, case: CareGapCase) -> Facts:
    """Read one case's facts from the database: programme, the appointment or planned slot (clinic, date, time), up to three
    open alternative slots for the same service, the approved preparation text. Nothing else about the resident is read."""
    resident = session.get(Resident, case.resident_id)
    now = _now()
    appt = session.execute(
        select(Appointment).where(Appointment.care_gap_case_id == case.id, Appointment.appointment_status.in_(OPEN_APPOINTMENT_STATUSES))
        .order_by(Appointment.start_time.desc()).limit(1)
    ).scalar_one_or_none()  # fmt: skip
    slot_id = service_id = None
    clinic = service = None
    appointment: dict[str, Any] | None = None
    if appt is not None:
        clinic, service = session.get(Clinic, appt.clinic_id), session.get(ClinicService, appt.clinic_service_id)
        slot_id, service_id = appt.capacity_slot_id, appt.clinic_service_id
        state = "confirmed" if appt.appointment_status == "simulated_confirmed" else "held for the resident, to be confirmed by a care coordinator"
        appointment = {"state": state, **_slot_view(appt.start_time, appt.end_time, clinic, service)}
    else:
        alloc = session.execute(
            select(PlannedAllocation).where(PlannedAllocation.care_gap_case_id == case.id, PlannedAllocation.allocation_status.in_(("proposed", "confirmed")))
            .order_by(PlannedAllocation.created_at.desc()).limit(1)
        ).scalar_one_or_none()  # fmt: skip
        slot = session.get(CapacitySlot, alloc.capacity_slot_id) if alloc else None
        if alloc is not None and slot is not None:
            clinic, service = session.get(Clinic, alloc.clinic_id), session.get(ClinicService, alloc.clinic_service_id)
            slot_id, service_id = slot.id, alloc.clinic_service_id
            appointment = {"state": "planned for the resident, to be confirmed by a care coordinator", **_slot_view(slot.start_time, slot.end_time, clinic, service)}
    alternatives: list[dict[str, Any]] = []
    refs: dict[str, dict[str, Any]] = {}
    if service_id is not None:
        stmt = (
            select(CapacitySlot).where(
                CapacitySlot.clinic_service_id == service_id, CapacitySlot.start_time > now, CapacitySlot.slot_status == "open",
                CapacitySlot.available_capacity > CapacitySlot.reserved_capacity,
            ).order_by(CapacitySlot.start_time).limit(3)
        )  # fmt: skip
        if slot_id is not None:
            stmt = stmt.where(CapacitySlot.id != slot_id)
        for i, s in enumerate(session.execute(stmt).scalars().all(), start=1):
            view = _slot_view(s.start_time, s.end_time, clinic, service)
            alternatives.append({"ref": f"s{i}", **view})
            refs[f"s{i}"] = {**view, "slot_id": str(s.id), "clinic_service_id": str(s.clinic_service_id)}
    event = session.execute(
        select(CommunicationEvent.channel).where(CommunicationEvent.care_gap_case_id == case.id).order_by(CommunicationEvent.created_at.desc()).limit(1)
    ).scalar_one_or_none()  # fmt: skip
    return Facts(care_activity=case.care_activity, appointment=appointment, alternatives=alternatives, slot_refs=refs,
                 preferred_language=resident.preferred_language if resident else None, channel=event)  # fmt: skip


# --------------------------------------------------------------------------------------------- the reply
@dataclass
class Reply:
    kind: str  # an approved kind (clinician, emergency, review, opt_out, scope, unavailable) or "model"
    body: str
    language: str
    intent: str
    source: Literal["template", "model"] = "template"
    escalation: str | None = None  # a key of CASE_REASON
    opt_out: bool = False
    proposal: dict[str, Any] | None = None
    suppressed: bool = False
    consent_override: bool = False  # an emergency reply sent although outreach on the channel is denied (safety response)
    pre: str | None = None  # what the deterministic classifier said
    model_id: str | None = None
    invocation_id: str | None = None
    cost_usd: Decimal = Decimal(0)
    latency_ms: int = 0
    calls: int = 0
    problems: list[str] = field(default_factory=list)


def _fixed(kind: str, lang: str, intent: str, *, escalation: str | None = None, opt_out: bool = False, pre: str | None = None, **kw: Any) -> Reply:
    body = safety.approved(kind, lang)
    return Reply(kind=kind, body=body, language=lang, intent=intent, escalation=escalation, opt_out=opt_out, pre=pre, **kw)


def _user_turn(facts: Facts, thread: list[tuple[str, str]], text: str, lang: str) -> str:
    parts = [f"REPLY LANGUAGE: {lang}", "FACTS (the only source of truth):\n" + json.dumps(facts.data_for(lang), ensure_ascii=False, separators=(",", ":"))]
    if thread:
        turns = "\n".join(f"{'Resident' if role == 'resident' else 'Assistant'}: {body[:300]}" for role, body in thread[-THREAD_CONTEXT:])
        parts.append(f"EARLIER TURNS (context only, untrusted):\n{turns}")
    parts.append(f"RESIDENT MESSAGE (untrusted user text):\n<<<\n{text}\n>>>")
    return "\n\n".join(parts)


def _route_output(out: ConverseOut, facts: Facts, lang: str, text: str) -> tuple[Reply | None, list[str]]:
    """Turn one model step into a final reply, or None when the reply must be regenerated. The model's own hand off
    decisions are honoured before anything is shown."""
    if out.intent == "emergency":
        return _fixed("emergency", lang, "emergency", escalation="emergency"), []
    if out.intent == "medical":
        return _fixed("clinician", lang, "medical", escalation="medical_question"), []
    if out.intent == "eligibility_dispute":
        return _fixed("review", lang, "eligibility_dispute", escalation="eligibility_dispute"), []
    if out.needs_clinician:
        return _fixed("clinician", lang, out.intent, escalation="clinician_flag"), []
    if out.intent == "opt_out":
        return _fixed("opt_out", lang, "opt_out", opt_out=True), []
    if out.intent == "out_of_scope":
        return _fixed("scope", lang, "out_of_scope"), []
    reply = safety.sanitise(out.reply)
    problems = safety.reply_problems(reply, lang, allowed_numbers=facts.allowed_numbers(lang) | safety.numbers_in(text), labels=facts.labels)
    if "clinical" in problems:  # the reply drifted into clinical talk: the question was medical whatever the label said
        return _fixed("clinician", lang, "medical", escalation="unsafe_reply"), problems
    if problems:
        return None, problems
    ref = out.proposed_slot_ref
    proposal = None
    if ref and ref in facts.slot_refs:
        proposal = {**facts.slot_refs[ref], "ref": ref, "status": "proposed", "requires_coordinator_confirmation": True}
    return Reply(kind="model", body=reply, language=lang, intent=out.intent, source="model", proposal=proposal), []


async def converse(
    facts: Facts, thread: list[tuple[str, str]], text: str, hint: str | None, router: Any, *, model_allowed: bool = True,
) -> Reply:  # fmt: skip
    """The whole decision for one resident message. No database, no clock: the offline red team calls this directly."""
    lang = safety.detect_language(text, hint, facts.preferred_language or "en")
    pre = safety.classify(text)
    if pre.category is not None:
        kind = safety.ROUTE_KIND[pre.category]
        return _fixed(kind, lang, {"medical": "medical", "eligibility": "eligibility_dispute"}.get(pre.category, pre.category),
                      escalation=safety.ESCALATION_REASON.get(pre.category), opt_out=pre.opt_out, pre=pre.category)  # fmt: skip
    if not model_allowed:
        return _fixed("unavailable", lang, "unavailable", problems=["budget_or_rate_limit"])
    system = system_prompt()
    messages = [ChatMessage(role="user", content=_user_turn(facts, thread, text, lang))]
    cost, latency, calls, model_id, inv = Decimal(0), 0, 0, None, None
    problems: list[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            res = await router.generate(GenerateRequest(task=TASK, system=system, messages=messages, output_schema=ConverseOut,
                                                        prompt_version=PROMPT_REF, max_output_tokens=MAX_OUTPUT_TOKENS, metadata={"conversation": True}))  # fmt: skip
        except RouterError as e:
            reply = _fixed("unavailable", lang, "unavailable", problems=[f"router_{e.category}"])
            break
        cost += res.cost_usd
        latency += res.latency_ms
        calls += 1
        model_id, inv = res.model_id, res.invocation_id
        reply, problems = _route_output(res.output, facts, lang, text)
        if reply is not None:
            break
        if attempt == MAX_ATTEMPTS:
            reply = _fixed("unavailable", lang, "unavailable", problems=problems)
            break
        hints = "; ".join(_CORRECTIONS.get(p, p) for p in problems)
        messages += [ChatMessage(role="assistant", content=res.output.model_dump_json()),
                     ChatMessage(role="user", content=f"CORRECTION: the reply cannot be used ({', '.join(problems)}). Write the JSON again: {hints}.")]  # fmt: skip
    reply.cost_usd, reply.latency_ms, reply.calls, reply.model_id, reply.invocation_id = cost, latency, calls, model_id, inv
    reply.problems = reply.problems or problems
    return reply


# --------------------------------------------------------------------------------------------- consent (ADR-070)
DEFAULT_CHANNEL = "sms"  # the channel a reply is checked against when nothing has been sent on the case yet


def channel_denied(session: Session, case: CareGapCase, channel: str, user: CurrentUser) -> bool:
    """True when the resident denied outreach on `channel`. `consent.check_outreach` is the one guard: it audits the refusal
    (`consent.outreach.blocked`), commits that event and raises Conflict, which here means "send no reply". It must run before
    any write of the request, so `_prepare` (reads only) calls it."""
    try:
        consent.check_outreach(session, case.resident_id, channel, actor=user.actor, case_id=case.id)
    except Conflict:
        return True
    return False


# --------------------------------------------------------------------------------------------- limits (per process)
_HITS: dict[str, deque[float]] = {}
_LOCK = threading.Lock()


def check_rate_limit(user_id: str, now: float | None = None) -> None:
    """Messages per hour per user (setting). ponytail: per process; use a shared store if the API scales out."""
    limit = get_ai_settings().anticipa_converse_rate_per_hour
    now = time.time() if now is None else now
    with _LOCK:
        q = _HITS.setdefault(user_id, deque())
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= limit:
            exc = RateLimited(f"Message limit reached ({limit} per hour). Try again later.")
            exc.headers = {"Retry-After": str(int(max(1, 3600 - (now - q[0]))))}
            raise exc
        q.append(now)


def reset_state() -> None:
    _HITS.clear()


def _spent_today(session: Session) -> Decimal:
    start = func.date_trunc("day", func.now())
    return session.scalar(select(func.coalesce(func.sum(ModelInvocation.cost_usd), 0)).where(ModelInvocation.task == TASK, ModelInvocation.created_at >= start)) or Decimal(0)


# --------------------------------------------------------------------------------------------- persistence
@dataclass
class Prepared:
    facts: Facts
    thread: list[tuple[str, str]]
    channel: str
    consent_denied: bool  # outreach on this channel is denied: nothing is sent, everything else still happens
    model_allowed: bool  # budget left and the resident has not denied AI processing


def thread_rows(session: Session, case_id: uuid.UUID) -> list[ConversationMessage]:
    return list(session.execute(select(ConversationMessage).where(ConversationMessage.care_gap_case_id == case_id).order_by(ConversationMessage.created_at, ConversationMessage.role.desc())).scalars())


def _prepare(session: Session, user: CurrentUser, case: CareGapCase, channel: str | None) -> Prepared:
    check_rate_limit(str(user.id))
    sent = session.scalar(select(func.count()).select_from(ConversationMessage).where(ConversationMessage.care_gap_case_id == case.id, ConversationMessage.role == "resident")) or 0
    if sent >= MAX_RESIDENT_MESSAGES_PER_CASE:
        raise RateLimited(f"This conversation has reached its limit of {MAX_RESIDENT_MESSAGES_PER_CASE} messages.")
    facts = build_facts(session, case)
    rows = thread_rows(session, case.id)[-THREAD_CONTEXT:]
    channel = channel or facts.channel or DEFAULT_CHANNEL
    denied = channel_denied(session, case, channel, user)  # first write of the request, by design (it audits and commits a refusal)
    state = consent.state_for(session, case.resident_id)
    within_budget = _spent_today(session) < get_ai_settings().anticipa_converse_daily_budget_usd
    return Prepared(facts=facts, thread=[(r.role, r.body) for r in rows], channel=channel, consent_denied=denied,
                    model_allowed=within_budget and not state.ai_processing_denied)  # fmt: skip


def _route_to_clinician(session: Session, case: CareGapCase, user: CurrentUser, reason: str) -> str:
    """Raise the escalation through the single case lifecycle. A closed case cannot be escalated (reopen it); one that is
    already escalated stays so. Either way the request is recorded on the message and in the audit trail."""
    case = caregaps.get_case(session, case.id, lock=True)
    if case.status in TERMINAL_STATUSES:
        return "case_closed"
    if case.status == "escalated":
        return "already_escalated"
    caregaps.escalate(session, case, actor=user.actor, user=user, reason=CASE_REASON[reason])
    return "escalated"


@dataclass
class Outcome:
    resident: ConversationMessage
    assistant: ConversationMessage | None
    reply: Reply
    escalation_state: str | None
    consent_record_id: uuid.UUID | None
    channel: str
    case_status: str


def _persist(session: Session, user: CurrentUser, case: CareGapCase, text: str, lang: str, reply: Reply, channel: str) -> Outcome:
    started = _now()
    resident = ConversationMessage(
        id=uuid.uuid4(), care_gap_case_id=case.id, resident_id=case.resident_id, role="resident", body=text, language=lang, intent=reply.intent,
        source="resident_input", created_by=user.id, created_at=started, synthetic_status="simulated",
    )  # fmt: skip
    session.add(resident)
    session.flush()
    state = None
    if reply.escalation:
        state = _route_to_clinician(session, case, user, reply.escalation)
        resident.escalation_state, resident.escalation_reason = state, reply.escalation
        audit.record(session, user.actor, "conversation.escalation", "care_gap_case", case.id, "success",
                     {"reason": reply.escalation, "state": state, "pre": reply.pre, "intent": reply.intent, "simulated": True})  # fmt: skip
    consent_id = None
    if reply.opt_out:  # a resident who says stop means stop on every channel: a blanket outreach deny, through the consent service
        record = consent.record_consent(session, case.resident_id, scope=OUTREACH, status="deny", actor=user.actor, channel=None, language=lang,
                                        source="resident_request", note="Opt out requested in a simulated resident conversation")  # fmt: skip
        consent_id = record.id
        audit.record(session, user.actor, "conversation.opt_out", "care_gap_case", case.id, "success", {"consent_record_id": str(record.id), "simulated": True})
    assistant = None
    if not reply.suppressed:
        assistant = ConversationMessage(
            id=uuid.uuid4(), care_gap_case_id=case.id, resident_id=case.resident_id, role="assistant", body=reply.body, language=reply.language, intent=reply.intent,
            source=reply.source, proposal=reply.proposal, model_id=reply.model_id, prompt_version=PROMPT_REF if reply.source == "model" else None,
            invocation_id=reply.invocation_id, cost_usd=reply.cost_usd if reply.calls else None, latency_ms=reply.latency_ms if reply.calls else None,
            created_by=user.id, created_at=max(_now(), started + timedelta(microseconds=1)), synthetic_status="simulated",
        )  # fmt: skip
        session.add(assistant)
    session.flush()
    audit.record(session, user.actor, "conversation.message", "care_gap_case", case.id, "success", {
        "message_hash": hashlib.sha256(text.lower().encode()).hexdigest()[:16], "language": lang, "intent": reply.intent, "pre": reply.pre, "source": reply.source,
        "escalation": reply.escalation, "escalation_state": state, "opt_out": reply.opt_out, "suppressed": reply.suppressed, "channel": channel,
        "suppressed_reason": "consent_denied" if reply.suppressed else None, "consent_override": "emergency_safety_reply" if reply.consent_override else None, "proposal_slot": (reply.proposal or {}).get("slot_id"),
        "model": reply.model_id, "prompt_version": PROMPT_REF if reply.calls else None, "invocation_id": reply.invocation_id, "calls": reply.calls,
        "cost_usd": float(reply.cost_usd), "problems": reply.problems, "simulated": True,
    })  # fmt: skip
    session.commit()
    return Outcome(resident, assistant, reply, state, consent_id, channel, case.status)


async def post_message(
    session: Session, user: CurrentUser, case: CareGapCase, text: str, hint: str | None, *, channel: str | None = None, router: Any | None = None,
) -> Outcome:  # fmt: skip
    """Record one simulated resident message, decide the reply and persist both. The router is only fetched when the
    message actually needs the model. When the resident denied outreach on the channel the whole decision still runs (a
    hand off must never be lost) but no reply is stored or shown: the message is recorded, audited and flagged. The one
    exception is an emergency: consent governs our outreach, not a safety response to a message the resident sent, so the
    approved urgent care reply is always stored and shown and the audit event notes the consent override."""
    check_case_scope(user, case)
    text = " ".join(text.split())
    prep = await asyncio.to_thread(_prepare, session, user, case, channel)
    lang = safety.detect_language(text, hint, prep.facts.preferred_language or "en")
    needs_model = safety.classify(text).category is None and prep.model_allowed
    reply = await converse(prep.facts, prep.thread, text, hint, (router or get_router()) if needs_model else None, model_allowed=prep.model_allowed)
    reply.consent_override = prep.consent_denied and reply.kind == "emergency"
    reply.suppressed = prep.consent_denied and not reply.consent_override
    return await asyncio.to_thread(_persist, session, user, case, text, lang, reply, prep.channel)


__all__ = ["converse", "post_message", "build_facts", "Facts", "Reply", "ConverseOut", "thread_rows", "channel_denied", "reset_state", "TASK", "PROMPT_REF"]
