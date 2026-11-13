"""Appointment schedule for one day across clinics: capacity slots grouped by clinic and service with seats, reserved seats
(confirmed allocations) and held appointments. Read only, from the simulated capacity network (indexed on slot start time).
"""
# ruff: noqa: E501

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from anticipa.analytics.places import scope_info
from anticipa.analytics.service import SOURCES, SYNTHETIC_BANNER, _now, _ratio, cached
from anticipa.engagement.models import OPEN_APPOINTMENT_STATUSES, Appointment
from anticipa.planning.models import CapacitySlot, Clinic, ClinicService

SCHEDULE_TTL_S = 30.0
MAX_SLOTS = 5000


def schedule(session: Session, *, day: date | None = None, district: str | None = None, state: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    day = day or now.date()
    scope = scope_info(district, state)
    return cached(("schedule", day, scope["district"], scope["state"]), SCHEDULE_TTL_S, lambda: _schedule(session, day, scope, now))


def _schedule(session: Session, day: date, scope: dict[str, Any], now: datetime) -> dict[str, Any]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    held = (
        select(Appointment.capacity_slot_id.label("slot_id"), func.count().label("n"))
        .where(Appointment.appointment_status.in_(OPEN_APPOINTMENT_STATUSES))
        .group_by(Appointment.capacity_slot_id)
        .subquery()
    )
    stmt = (
        select(CapacitySlot, Clinic, ClinicService, func.coalesce(held.c.n, 0))
        .join(Clinic, Clinic.id == CapacitySlot.clinic_id)
        .join(ClinicService, ClinicService.id == CapacitySlot.clinic_service_id)
        .outerjoin(held, held.c.slot_id == CapacitySlot.id)
        .where(CapacitySlot.start_time >= start, CapacitySlot.start_time < start + timedelta(days=1), CapacitySlot.slot_status == "open")
        .order_by(Clinic.name, ClinicService.service_name, CapacitySlot.start_time)
        .limit(MAX_SLOTS + 1)
    )
    if scope["districts"] is not None:
        stmt = stmt.where(Clinic.district_code.in_(scope["districts"]))
    rows = session.execute(stmt).all()
    truncated = len(rows) > MAX_SLOTS
    clinics: dict[Any, dict[str, Any]] = {}
    services: dict[str, dict[str, Any]] = {}
    for slot, clinic, service, n_held in rows[:MAX_SLOTS]:
        c = clinics.setdefault(clinic.id, {"clinic_id": clinic.id, "clinic_key": clinic.clinic_key, "name": clinic.name, "district_code": clinic.district_code, "services": {}})
        sv = c["services"].setdefault(service.service_key, {"service_key": service.service_key, "service_name": service.service_name, "slots": [], "seats": 0, "reserved": 0, "held_appointments": 0})
        sv["slots"].append({"slot_id": slot.id, "start_time": slot.start_time, "end_time": slot.end_time, "seats": slot.available_capacity, "reserved": slot.reserved_capacity, "held_appointments": n_held})
        for target in (sv, services.setdefault(service.service_key, {"service_key": service.service_key, "service_name": service.service_name, "seats": 0, "reserved": 0, "held_appointments": 0})):
            target["seats"] += slot.available_capacity
            target["reserved"] += slot.reserved_capacity
            target["held_appointments"] += n_held
    seats = sum(s["seats"] for s in services.values())
    reserved = sum(s["reserved"] for s in services.values())
    return {
        "generated_at": now, "cache_ttl_seconds": SCHEDULE_TTL_S, "synthetic_banner": SYNTHETIC_BANNER, "sources": SOURCES, "scope": scope, "day": day,
        "definition": "Open simulated capacity slots starting on the day (UTC), grouped by clinic and service. seats = available capacity; reserved = seats taken by confirmed allocations; held_appointments = proposed or simulated confirmed appointments on the slot.",
        "source": "simulated", "truncated": truncated,
        "totals": {"clinics": len(clinics), "slots": min(len(rows), MAX_SLOTS), "seats": seats, "reserved": reserved, "held_appointments": sum(s["held_appointments"] for s in services.values()),
                   "utilisation": _ratio(reserved, seats)},  # fmt: skip
        "by_service": [{**v, "utilisation": _ratio(v["reserved"], v["seats"])} for v in sorted(services.values(), key=lambda x: x["service_name"])],
        "clinics": [
            {**{k: v for k, v in c.items() if k != "services"}, "services": [{**sv, "utilisation": _ratio(sv["reserved"], sv["seats"])} for sv in c["services"].values()]}
            for c in clinics.values()
        ],
    }  # fmt: skip
