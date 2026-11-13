"""Deterministic, seeded SIMULATED clinic network + capacity generator, and a synthetic case generator for tests and
benchmarks. Nothing here is real: every clinic/slot row is stored with `synthetic_status = 'simulated'`.

Identity-stable: clinic, service and slot ids are UUIDv5 of their natural keys and all per-entity randomness is a hash of
(seed, key), so re-running the generator with the same parameters inserts nothing new (ON CONFLICT DO NOTHING) and never
touches slots that were later reserved or edited.
"""

from __future__ import annotations

import hashlib
import random
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from anticipa.core.ids import uuid5_of
from anticipa.planning.domain import PlannableCase, PoolSpec, SlotSpec
from anticipa.planning.models import CapacitySlot, Clinic, ClinicService

# service_key -> (name, appointment minutes, share of clinics offering it)
SERVICE_CATALOGUE: dict[str, tuple[str, int, float]] = {
    "blood_pressure_check": ("Blood pressure check", 15, 0.95),
    "diabetes_hba1c": ("Diabetes HbA1c check", 15, 0.90),
    "lipid_panel": ("Lipid panel", 15, 0.90),
    "immunisation": ("Adult immunisation", 15, 0.85),
    "cervical_screening": ("Cervical screening", 20, 0.65),
    "colorectal_screening": ("Colorectal screening", 20, 0.60),
    "mammography": ("Mammography", 30, 0.55),
    "genetic_counselling": ("Genetic counselling referral", 45, 0.20),
    # India national screening pack: simple checks done at sub centres and health and wellness centres
    "blood_glucose_check": ("Blood glucose check", 15, 0.95),
    "oral_cancer_screening": ("Oral cancer screening", 15, 0.85),
    "clinical_breast_exam": ("Clinical breast examination", 20, 0.80),
}
DISTRICTS = tuple(f"D{i:02d}" for i in range(1, 11))  # same codes as datafabric.assign
LANGUAGES = (("en", 0.60), ("hi", 0.25), ("ar", 0.15))  # same mix as datafabric.assign
PRIORITY_MIX = (("urgent", 0.05), ("high", 0.15), ("medium", 0.30), ("routine", 0.50))


def _unit(*parts: object) -> float:
    """Stable uniform [0, 1) from the given parts (platform- and order-independent)."""
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


@dataclass(frozen=True)
class NetworkParams:
    seed: int = 42
    start: datetime = datetime(2026, 11, 10, tzinfo=UTC)
    days: int = 7
    districts: tuple[str, ...] = DISTRICTS
    clinics_per_district: int = 3
    services: tuple[str, ...] = tuple(SERVICE_CATALOGUE)
    day_start_hour: int = 8
    day_end_hour: int = 17
    operating_weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)
    seats_per_slot: float = 3.0  # mean; scaled by district/clinic factors and per-slot noise
    target_total_seats: int | None = None  # if set, seats_per_slot is rescaled so the network holds ~this many seats
    district_capacity_spread: float = 0.5  # district factor in [1-spread, 1+spread]: creates under-served districts


@dataclass
class Network:
    params: NetworkParams
    clinics: list[dict] = field(default_factory=list)
    services: list[dict] = field(default_factory=list)
    slots: list[dict] = field(default_factory=list)

    def pools(self) -> list[PoolSpec]:
        clinics = {c["id"]: c for c in self.clinics}
        return [PoolSpec(s["id"], s["clinic_id"], clinics[s["clinic_id"]]["clinic_key"], clinics[s["clinic_id"]]["district_code"],
                         s["service_key"], s["appointment_duration_minutes"]) for s in self.services]  # fmt: skip

    def slot_specs(self) -> list[SlotSpec]:
        return [SlotSpec(s["id"], s["clinic_service_id"], s["start_time"], s["end_time"],
                         s["available_capacity"] - s["reserved_capacity"], s["slot_status"]) for s in self.slots]  # fmt: skip

    @property
    def total_seats(self) -> int:
        return sum(s["available_capacity"] for s in self.slots)


MAX_GENERATED_SLOTS = 300_000


def network_params_from_scope(scope: Mapping[str, Any]) -> NetworkParams:
    """Validated NetworkParams from a job input_scope / CLI options (unknown keys are rejected)."""
    allowed = {"seed", "start", "days", "districts", "clinics_per_district", "target_total_seats", "seats_per_slot",
               "district_capacity_spread"}  # fmt: skip
    unknown = set(scope) - allowed
    if unknown:
        raise ValueError(f"unknown capacity generator option(s): {sorted(unknown)}")
    kw: dict[str, Any] = dict(scope)
    if isinstance(kw.get("start"), str):
        kw["start"] = datetime.fromisoformat(kw["start"].replace("Z", "+00:00"))
    if "start" in kw and kw["start"].tzinfo is None:
        kw["start"] = kw["start"].replace(tzinfo=UTC)
    if "districts" in kw:
        kw["districts"] = tuple(kw["districts"])
    p = NetworkParams(**kw)
    if not (1 <= p.days <= 31 and 1 <= p.clinics_per_district <= 20 and 1 <= len(p.districts) <= 50):
        raise ValueError("days must be 1-31, clinics_per_district 1-20, districts 1-50")
    if p.seats_per_slot <= 0 or (p.target_total_seats is not None and p.target_total_seats < 1):
        raise ValueError("seats_per_slot and target_total_seats must be positive")
    estimate = len(p.districts) * p.clinics_per_district * len(p.services) * p.days * (p.day_end_hour - p.day_start_hour)
    if estimate > MAX_GENERATED_SLOTS * 2:
        raise ValueError("requested network is too large for the synthetic generator")
    return p


def generate_network(params: NetworkParams) -> Network:
    p = params
    net = Network(p)
    clinic_keys: list[tuple[str, str]] = []  # (clinic_key, district)
    for d in p.districts:
        for i in range(1, p.clinics_per_district + 1):
            clinic_keys.append((f"{d.lower()}-c{i}", d))
    offered: dict[str, list[str]] = {}
    for key, _ in clinic_keys:
        offered[key] = [s for s in p.services if _unit(p.seed, "offer", key, s) < SERVICE_CATALOGUE[s][2]]
    for s in p.services:  # every service exists somewhere in the network
        if not any(s in v for v in offered.values()):
            offered[clinic_keys[int(_unit(p.seed, "fallback", s) * len(clinic_keys))][0]].append(s)

    days = [p.start + timedelta(days=n) for n in range(p.days)]
    days = [d for d in days if d.weekday() in p.operating_weekdays]
    rows: list[tuple[str, str, str, datetime, datetime]] = []  # clinic_key, district, service, start, end
    for key, district in clinic_keys:
        for service in sorted(offered[key]):
            minutes = SERVICE_CATALOGUE[service][1]
            for day in days:
                t = day.replace(hour=p.day_start_hour, minute=0, second=0, microsecond=0)
                close = day.replace(hour=p.day_end_hour, minute=0, second=0, microsecond=0)
                while t + timedelta(minutes=minutes) <= close:
                    rows.append((key, district, service, t, t + timedelta(minutes=minutes)))
                    t += timedelta(hours=1)
    mean = p.seats_per_slot if p.target_total_seats is None else max(0.1, p.target_total_seats / max(1, len(rows)))

    for key, district in clinic_keys:
        cid = uuid5_of(f"planning:clinic:{key}")
        net.clinics.append({"id": cid, "clinic_key": key, "name": f"Simulated clinic {key.upper()}", "district_code": district,
                            "address": f"{key} (synthetic address, district {district})", "timezone": "UTC",
                            "operating_status": "open", "synthetic_status": "simulated"})  # fmt: skip
        for service in sorted(offered[key]):
            name, minutes, _ = SERVICE_CATALOGUE[service]
            net.services.append({"id": uuid5_of(f"planning:service:{key}:{service}"), "clinic_id": cid, "service_key": service,
                                 "service_name": name, "appointment_duration_minutes": minutes, "active": True,
                                 "synthetic_status": "simulated"})  # fmt: skip
    spread = p.district_capacity_spread
    for key, district, service, start, end in rows:
        factor = (1 - spread + 2 * spread * _unit(p.seed, "district", district)) * (0.75 + 0.5 * _unit(p.seed, "clinic", key))
        seats = max(1, round(mean * factor * (0.5 + _unit(p.seed, "slot", key, service, start.isoformat()))))
        net.slots.append({"id": uuid5_of(f"planning:slot:{key}:{service}:{start.isoformat()}"),
                          "clinic_id": uuid5_of(f"planning:clinic:{key}"),
                          "clinic_service_id": uuid5_of(f"planning:service:{key}:{service}"),
                          "start_time": start, "end_time": end, "available_capacity": seats, "reserved_capacity": 0,
                          "slot_status": "open", "source_type": "synthetic_generator", "synthetic_status": "simulated"})  # fmt: skip
    return net


def persist_network(session: Session, net: Network, *, chunk: int = 2000) -> dict[str, int]:
    """Idempotent bulk insert (caller commits). Returns rows actually inserted per table."""
    inserted = {}
    for model, rows in ((Clinic, net.clinics), (ClinicService, net.services), (CapacitySlot, net.slots)):
        n = 0
        for i in range(0, len(rows), chunk):
            stmt = pg_insert(model).values(rows[i : i + chunk]).on_conflict_do_nothing().returning(model.id)
            n += len(session.execute(stmt).all())  # RETURNING: rowcount is unreliable for multi-row inserts
        inserted[model.__tablename__] = n
    return inserted


def generate_cases(n: int, *, seed: int = 42, districts: tuple[str, ...] = DISTRICTS, services: tuple[str, ...] | None = None,
                   multi_case_share: float = 0.08) -> list[PlannableCase]:  # fmt: skip
    """Synthetic plannable cases (benchmarks/tests). Demand is skewed by district and service; `multi_case_share` of the
    cases belong to a resident who already has another case, to exercise resident time-conflict handling."""
    rng = random.Random(seed)  # noqa: S311  (synthetic benchmark data, not security-sensitive)
    services = services or tuple(SERVICE_CATALOGUE)
    d_w = [0.6 + 0.8 * _unit(seed, "demand-district", d) for d in districts]
    s_w = [0.5 + _unit(seed, "demand-service", s) for s in services]
    ds = rng.choices(districts, d_w, k=n)
    ss = rng.choices(services, s_w, k=n)
    ls = rng.choices([x for x, _ in LANGUAGES], [w for _, w in LANGUAGES], k=n)
    ps = rng.choices([x for x, _ in PRIORITY_MIX], [w for _, w in PRIORITY_MIX], k=n)
    base = datetime(2026, 11, 1, tzinfo=UTC)
    cases: list[PlannableCase] = []
    resident: uuid.UUID = uuid.UUID(int=0)
    for i in range(n):
        if i == 0 or rng.random() >= multi_case_share or cases[-1].service_key == ss[i]:
            resident = uuid5_of(f"bench:{seed}:resident:{i}")
            d, lang = ds[i], ls[i]
        else:  # second case of the previous case's resident: same district/language
            d, lang = cases[-1].district_code, cases[-1].language
        cases.append(PlannableCase(uuid5_of(f"bench:{seed}:case:{i}"), resident, ss[i], ps[i], d, lang, base + timedelta(minutes=i)))
    return cases
